from __future__ import annotations

import contextlib
import json
import hmac
import os
import select
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from collections.abc import Iterable
from typing import Any


_PROXY_DEVELOPER_GUARD = (
    "You are serving as the language model behind Pal's local OpenAI-compatible proxy. "
    "Pal owns memory, capability lookup, tool execution, approvals, scheduling, and plugin policy. "
    "Do not read local files, execute shell commands, edit files, browse, or use built-in Codex tools. "
    "Answer from the provided conversation or request one of the provided dynamic tools."
)
DEFAULT_CODEX_PROXY_API_KEY_ENV = "PAL_CODEX_PROXY_API_KEY"
DEFAULT_CODEX_PROXY_MODELS_ENV = "PAL_CODEX_PROXY_MODELS"
DEFAULT_CODEX_PROXY_MAX_CONCURRENCY_ENV = "PAL_CODEX_PROXY_MAX_CONCURRENCY"
DEFAULT_CODEX_PROXY_MAX_CONCURRENCY = 3
DEFAULT_CODEX_PROXY_MODELS = (
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
)
_CODEX_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}


class CodexProxyError(RuntimeError):
    pass


@dataclass(frozen=True)
class CodexToolCall:
    call_id: str
    name: str
    arguments: Any


@dataclass(frozen=True)
class CodexCompletion:
    model: str
    text: str = ""
    tool_call: CodexToolCall | None = None


def _chat_completion_id() -> str:
    return f"chatcmpl-pal-codex-{int(time.time())}"


def _strip_openai_prefix(model: str) -> str:
    text = str(model or "").strip()
    for prefix in ("openai/", "hosted_vllm/", "lm_studio/", "llamafile/"):
        if text.startswith(prefix):
            return text.removeprefix(prefix) or "gpt-5.4"
    return text or "gpt-5.4"


def _parse_model_list(raw: str | None) -> tuple[str, ...]:
    text = str(raw or "").strip()
    if not text:
        return DEFAULT_CODEX_PROXY_MODELS
    items: list[str] = []
    seen: set[str] = set()
    for chunk in text.replace("\n", ",").split(","):
        model = _strip_openai_prefix(chunk)
        if not model or model in seen:
            continue
        seen.add(model)
        items.append(model)
    return tuple(items) or DEFAULT_CODEX_PROXY_MODELS


def _models_payload(model_ids: tuple[str, ...]) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": model_id, "object": "model", "owned_by": "openai"} for model_id in model_ids],
    }


def _parse_max_concurrency(raw: str | int | None) -> int:
    try:
        parsed = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_CODEX_PROXY_MAX_CONCURRENCY
    return max(1, min(parsed, 32))


def _codex_effort_from_payload(payload: dict[str, Any]) -> str | None:
    value = payload.get("reasoning_effort")
    if isinstance(value, dict):
        value = value.get("effort")
    if value is None and isinstance(payload.get("reasoning"), dict):
        value = payload["reasoning"].get("effort")
    text = str(value or "").strip().lower()
    if text in _CODEX_REASONING_EFFORTS:
        return text
    return None


def _turn_start_params(
    *,
    thread_id: str,
    model: str,
    effort: str | None,
    input_text: str = "",
    input_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    inputs = [dict(item) for item in list(input_items or []) if isinstance(item, dict)]
    if not inputs:
        inputs = [{"type": "text", "text": input_text}]
    params: dict[str, Any] = {
        "threadId": thread_id,
        "input": inputs,
        "model": model,
        "approvalPolicy": "never",
        "sandboxPolicy": {"type": "readOnly"},
    }
    if effort is not None:
        params["effort"] = effort
    return params


def _message_content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                kind = str(item.get("type") or "")
                if kind in {"text", "input_text"}:
                    parts.append(str(item.get("text") or ""))
                elif kind in {"image_url", "input_image"}:
                    image = item.get("image_url")
                    if isinstance(image, dict):
                        image = image.get("url")
                    if image:
                        parts.append(f"[image: {image}]")
        return "\n".join(part for part in parts if part)
    return str(content)


def _content_text_and_images(content: Any, *, artifact_manager: Any = None) -> tuple[str, list[dict[str, Any]]]:
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return str(content), []

    text_parts: list[str] = []
    image_items: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            text_parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        if kind in {"text", "input_text"}:
            text_parts.append(str(item.get("text") or ""))
            continue
        if kind in {"transcript", "input_transcript"}:
            text = str(item.get("text") or item.get("transcript") or "")
            if text:
                text_parts.append(f"Transcript:\n{text}")
            continue
        if kind in {"image_url", "input_image"}:
            image = item.get("image_url")
            if isinstance(image, dict):
                image = image.get("url")
            if image:
                image_items.append({"type": "image", "url": str(image)})
            continue
        if kind == "artifact_image":
            source_url = str(item.get("source_url") or "").strip()
            if source_url.startswith(("http://", "https://", "data:")):
                image_items.append({"type": "image", "url": source_url})
                continue
            if artifact_manager is None:
                text_parts.append("[image unavailable]")
                continue
            to_data_url = getattr(artifact_manager, "to_data_url", None)
            if not callable(to_data_url):
                text_parts.append("[image unavailable]")
                continue
            data_url = to_data_url(str(item.get("representation_id") or ""))
            if data_url:
                image_items.append({"type": "image", "url": str(data_url)})
            else:
                text_parts.append("[image unavailable]")
            continue
        if kind == "local_image":
            path = str(item.get("path") or "").strip()
            if path:
                image_items.append({"type": "localImage", "path": path})
    return "\n".join(part for part in text_parts if part), image_items


def _tool_result_name(message: dict[str, Any]) -> str:
    return str(message.get("name") or message.get("tool_call_id") or "tool")


def _messages_to_codex_turn(messages: list[dict[str, Any]]) -> tuple[str, str]:
    developer_instructions, input_items = _messages_to_codex_input(messages)
    text = "\n\n".join(str(item.get("text") or "") for item in input_items if item.get("type") == "text").strip()
    return developer_instructions, text or "Continue."


def _messages_to_codex_input(messages: list[dict[str, Any]], *, artifact_manager: Any = None) -> tuple[str, list[dict[str, Any]]]:
    developer_parts = [_PROXY_DEVELOPER_GUARD]
    transcript: list[str] = []

    def flush_text(input_items: list[dict[str, Any]]) -> None:
        text = "\n\n".join(part for part in transcript if part).strip()
        if text:
            input_items.append({"type": "text", "text": text})
        transcript.clear()

    input_items: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content, image_items = _content_text_and_images(message.get("content"), artifact_manager=artifact_manager)
        if role in {"system", "developer"}:
            if content:
                developer_parts.append(content)
            continue
        if role == "tool":
            transcript.append(f"Tool result ({_tool_result_name(message)}):\n{content}")
            continue
        if role == "assistant":
            if content:
                transcript.append(f"Assistant:\n{content}")
            tool_calls = list(message.get("tool_calls") or [])
            if tool_calls:
                lines = []
                for call in tool_calls:
                    function = (call or {}).get("function") or {}
                    lines.append(f"- {function.get('name')}: {function.get('arguments')}")
                transcript.append("Assistant requested tool calls:\n" + "\n".join(lines))
            continue
        label = "User" if role == "user" else role.title()
        if content:
            transcript.append(f"{label}:\n{content}")
        if image_items:
            flush_text(input_items)
            input_items.extend(image_items)
    flush_text(input_items)
    if not input_items:
        input_items.append({"type": "text", "text": "Continue."})
    return "\n\n".join(developer_parts), input_items


def _openai_tools_to_dynamic_tools(tools: Any) -> list[dict[str, Any]]:
    dynamic_tools: list[dict[str, Any]] = []
    for tool in list(tools or []):
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        dynamic_tools.append(
            {
                "name": name,
                "description": str(function.get("description") or name),
                "inputSchema": function.get("parameters") or {"type": "object"},
            }
        )
    return dynamic_tools


def _request_api_key(headers: Any) -> str:
    authorization = str(headers.get("Authorization") or headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    for key in ("X-API-Key", "x-api-key", "api-key", "Api-Key"):
        value = str(headers.get(key) or "").strip()
        if value:
            return value
    return ""


def _request_authorized(headers: Any, expected_api_key: str | None) -> bool:
    expected = str(expected_api_key or "").strip()
    if not expected:
        return True
    supplied = _request_api_key(headers)
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def _completion_payload(completion: CodexCompletion) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": completion.text}
    finish_reason = "stop"
    if completion.tool_call is not None:
        finish_reason = "tool_calls"
        message["content"] = None
        message["tool_calls"] = [
            {
                "id": completion.tool_call.call_id,
                "type": "function",
                "function": {
                    "name": completion.tool_call.name,
                    "arguments": json.dumps(completion.tool_call.arguments, ensure_ascii=False),
                },
            }
        ]
    created = int(time.time())
    return {
        "id": _chat_completion_id(),
        "object": "chat.completion",
        "created": created,
        "model": completion.model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }


def _stream_delta_payload(model: str, *, content: str | None = None, role: str | None = None) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    if role:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    return {
        "id": _chat_completion_id(),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }


def _stream_tool_call_payload(model: str, tool_call: CodexToolCall) -> dict[str, Any]:
    return {
        "id": _chat_completion_id(),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": tool_call.call_id,
                            "type": "function",
                            "function": {
                                "name": tool_call.name,
                                "arguments": json.dumps(tool_call.arguments, ensure_ascii=False),
                            },
                        }
                    ]
                },
                "finish_reason": None,
            }
        ],
    }


def _stream_done_payload(model: str, finish_reason: str) -> dict[str, Any]:
    return {
        "id": _chat_completion_id(),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
    }


def _default_codex_command() -> str:
    found = shutil.which("codex")
    if found:
        return found
    nvm_root = Path.home() / ".nvm" / "versions" / "node"
    candidates = sorted(nvm_root.glob("*/bin/codex"), reverse=True)
    if candidates:
        return str(candidates[0])
    return "codex"


def _codex_env(codex_bin: str) -> dict[str, str]:
    env = dict(os.environ)
    if "/" in codex_bin:
        raw_path = Path(codex_bin).expanduser()
        path_entries = [str(raw_path.parent)]
        resolved_parent = str(raw_path.resolve().parent)
        if resolved_parent not in path_entries:
            path_entries.append(resolved_parent)
        env["PATH"] = os.pathsep.join([*path_entries, env.get("PATH", "")])
    return env


@dataclass
class CodexAppServerBridge:
    codex_bin: str = ""
    timeout_seconds: int = 120
    cwd: str | None = None

    def _start_process(self) -> subprocess.Popen[str]:
        codex_bin = self.codex_bin or _default_codex_command()
        proc = subprocess.Popen(
            [codex_bin, "app-server", "--listen", "stdio://"],
            cwd=self.cwd or None,
            env=_codex_env(codex_bin),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        _debug_log(f"started codex app-server pid={proc.pid} bin={codex_bin}")
        return proc

    def invoke(self, payload: dict[str, Any]) -> CodexCompletion:
        model = _strip_openai_prefix(str(payload.get("model") or ""))
        developer_instructions, input_items = _messages_to_codex_input(list(payload.get("messages") or []))
        dynamic_tools = _openai_tools_to_dynamic_tools(payload.get("tools"))
        effort = _codex_effort_from_payload(payload)
        return self.invoke_turn(
            model=model,
            developer_instructions=developer_instructions,
            input_items=input_items,
            dynamic_tools=dynamic_tools,
            effort=effort,
        )

    def invoke_turn(
        self,
        *,
        model: str,
        developer_instructions: str,
        dynamic_tools: list[dict[str, Any]],
        effort: str | None,
        input_text: str = "",
        input_items: list[dict[str, Any]] | None = None,
    ) -> CodexCompletion:
        proc = self._start_process()
        try:
            return self._invoke_process(
                proc,
                model=model,
                developer_instructions=developer_instructions,
                input_text=input_text,
                input_items=input_items,
                dynamic_tools=dynamic_tools,
                effort=effort,
            )
        finally:
            self._stop_process(proc)

    def iter_stream(self, payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
        model = _strip_openai_prefix(str(payload.get("model") or ""))
        developer_instructions, input_items = _messages_to_codex_input(list(payload.get("messages") or []))
        dynamic_tools = _openai_tools_to_dynamic_tools(payload.get("tools"))
        effort = _codex_effort_from_payload(payload)
        yield from self.iter_turn_stream(
            model=model,
            developer_instructions=developer_instructions,
            input_items=input_items,
            dynamic_tools=dynamic_tools,
            effort=effort,
        )

    def iter_turn_stream(
        self,
        *,
        model: str,
        developer_instructions: str,
        dynamic_tools: list[dict[str, Any]],
        effort: str | None,
        input_text: str = "",
        input_items: list[dict[str, Any]] | None = None,
    ) -> Iterable[dict[str, Any]]:
        proc = self._start_process()
        try:
            yield from self._iter_process_stream(
                proc,
                model=model,
                developer_instructions=developer_instructions,
                input_text=input_text,
                input_items=input_items,
                dynamic_tools=dynamic_tools,
                effort=effort,
            )
        finally:
            self._stop_process(proc)

    def _invoke_process(
        self,
        proc: subprocess.Popen[str],
        *,
        model: str,
        developer_instructions: str,
        input_text: str,
        input_items: list[dict[str, Any]] | None,
        dynamic_tools: list[dict[str, Any]],
        effort: str | None,
    ) -> CodexCompletion:
        assert proc.stdin is not None
        seq = 0

        def send(method: str, params: dict[str, Any] | None = None) -> int:
            nonlocal seq
            seq += 1
            message: dict[str, Any] = {"method": method, "id": seq}
            if params is not None:
                message["params"] = params
            proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            _debug_log(f"codex send id={seq} method={method}")
            return seq

        initialize_id = send(
            "initialize",
            {
                "clientInfo": {"name": "pal-codex-proxy", "title": "Pal Codex Proxy", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        proc.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
        proc.stdin.flush()
        thread_params: dict[str, Any] = {
            "ephemeral": True,
            "developerInstructions": developer_instructions,
            "model": model,
        }
        if dynamic_tools:
            thread_params["dynamicTools"] = dynamic_tools
        thread_id_request: int | None = None
        deadline = time.time() + max(1, int(self.timeout_seconds))
        text_parts: list[str] = []

        while time.time() < deadline:
            item = self._read_message(proc, deadline=deadline)
            _debug_log(f"codex recv id={item.get('id')} method={item.get('method')}")
            if item.get("error"):
                raise CodexProxyError(str(item["error"]))
            if item.get("id") == initialize_id:
                thread_id_request = send("thread/start", thread_params)
                continue
            if thread_id_request is not None and item.get("id") == thread_id_request:
                thread_id = ((item.get("result") or {}).get("thread") or {}).get("id")
                if not thread_id:
                    raise CodexProxyError(f"thread/start returned no thread id: {item}")
                send(
                    "turn/start",
                    _turn_start_params(thread_id=thread_id, input_text=input_text, input_items=input_items, model=model, effort=effort),
                )
                continue
            method = str(item.get("method") or "")
            params = item.get("params") or {}
            if method == "item/tool/call":
                return CodexCompletion(
                    model=model,
                    tool_call=CodexToolCall(
                        call_id=str(params.get("callId") or f"call_{int(time.time())}"),
                        name=str(params.get("tool") or ""),
                        arguments=params.get("arguments"),
                    ),
                )
            if method == "item/agentMessage/delta":
                text_parts.append(str(params.get("delta") or params.get("text") or ""))
            elif method == "item/completed":
                completed_item = params.get("item") or {}
                if completed_item.get("type") == "agentMessage":
                    completed_text = str(completed_item.get("text") or "")
                    if completed_text:
                        text_parts = [completed_text]
            elif method == "turn/completed":
                return CodexCompletion(model=model, text="".join(text_parts).strip())
        raise CodexProxyError("timed out waiting for Codex app-server response")

    def _iter_process_stream(
        self,
        proc: subprocess.Popen[str],
        *,
        model: str,
        developer_instructions: str,
        input_text: str,
        input_items: list[dict[str, Any]] | None,
        dynamic_tools: list[dict[str, Any]],
        effort: str | None,
    ) -> Iterable[dict[str, Any]]:
        assert proc.stdin is not None
        seq = 0

        def send(method: str, params: dict[str, Any] | None = None) -> int:
            nonlocal seq
            seq += 1
            message: dict[str, Any] = {"method": method, "id": seq}
            if params is not None:
                message["params"] = params
            proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            _debug_log(f"codex stream send id={seq} method={method}")
            return seq

        initialize_id = send(
            "initialize",
            {
                "clientInfo": {"name": "pal-codex-proxy", "title": "Pal Codex Proxy", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        proc.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
        proc.stdin.flush()
        thread_params: dict[str, Any] = {
            "ephemeral": True,
            "developerInstructions": developer_instructions,
            "model": model,
        }
        if dynamic_tools:
            thread_params["dynamicTools"] = dynamic_tools
        thread_id_request: int | None = None
        deadline = time.time() + max(1, int(self.timeout_seconds))
        text_parts: list[str] = []

        while time.time() < deadline:
            item = self._read_message(proc, deadline=deadline)
            _debug_log(f"codex stream recv id={item.get('id')} method={item.get('method')}")
            if item.get("error"):
                raise CodexProxyError(str(item["error"]))
            if item.get("id") == initialize_id:
                thread_id_request = send("thread/start", thread_params)
                continue
            if thread_id_request is not None and item.get("id") == thread_id_request:
                thread_id = ((item.get("result") or {}).get("thread") or {}).get("id")
                if not thread_id:
                    raise CodexProxyError(f"thread/start returned no thread id: {item}")
                send(
                    "turn/start",
                    _turn_start_params(thread_id=thread_id, input_text=input_text, input_items=input_items, model=model, effort=effort),
                )
                continue
            method = str(item.get("method") or "")
            params = item.get("params") or {}
            if method == "item/tool/call":
                tool_call = CodexToolCall(
                    call_id=str(params.get("callId") or f"call_{int(time.time())}"),
                    name=str(params.get("tool") or ""),
                    arguments=params.get("arguments"),
                )
                yield _stream_tool_call_payload(model, tool_call)
                yield _stream_done_payload(model, "tool_calls")
                return
            if method == "item/agentMessage/delta":
                delta = str(params.get("delta") or params.get("text") or "")
                if delta:
                    text_parts.append(delta)
            elif method == "item/completed":
                completed_item = params.get("item") or {}
                completed_text = str(completed_item.get("text") or "")
                if completed_item.get("type") == "agentMessage":
                    if completed_text:
                        text_parts = [completed_text]
            elif method == "turn/completed":
                text = "".join(text_parts).strip()
                if text:
                    yield _stream_delta_payload(model, role="assistant")
                    yield _stream_delta_payload(model, content=text)
                yield _stream_done_payload(model, "stop")
                return
        raise CodexProxyError("timed out waiting for Codex app-server stream")

    def _read_message(self, proc: subprocess.Popen[str], *, deadline: float) -> dict[str, Any]:
        assert proc.stdout is not None
        streams = [proc.stdout]
        if proc.stderr is not None:
            streams.append(proc.stderr)
        while time.time() < deadline:
            remaining = max(0.0, deadline - time.time())
            ready, _, _ = select.select(streams, [], [], remaining)
            if not ready:
                break
            if proc.stderr is not None and proc.stderr in ready:
                stderr_line = proc.stderr.readline()
                if stderr_line:
                    _debug_log(f"codex stderr: {stderr_line.rstrip()[:1000]}")
                    continue
            if proc.stdout in ready:
                line = proc.stdout.readline()
                if not line:
                    raise CodexProxyError("Codex app-server exited without output")
                try:
                    return json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CodexProxyError(f"invalid Codex app-server JSON line: {line[:500]}") from exc
        raise CodexProxyError(
            f"timed out waiting for Codex app-server output pid={proc.pid} returncode={proc.poll()}"
        )

    @staticmethod
    def _stop_process(proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def _send_json(
    handler: BaseHTTPRequestHandler,
    status: int,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


def _send_sse_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "close")
    handler.end_headers()


def _write_sse(handler: BaseHTTPRequestHandler, payload: dict[str, Any] | str) -> None:
    if isinstance(payload, str):
        data = payload
    else:
        data = json.dumps(payload, ensure_ascii=False)
    handler.wfile.write(f"data: {data}\n\n".encode("utf-8"))
    handler.wfile.flush()


def _log(message: str) -> None:
    print(f"[pal-codex-proxy] {message}", flush=True)


def _debug_log(message: str) -> None:
    if os.environ.get("PAL_CODEX_PROXY_DEBUG") == "1":
        _log(message)


def _make_handler(
    bridge: CodexAppServerBridge,
    *,
    api_key: str | None = None,
    model_ids: tuple[str, ...] = DEFAULT_CODEX_PROXY_MODELS,
    semaphore: threading.BoundedSemaphore | None = None,
):
    class CodexProxyHandler(BaseHTTPRequestHandler):
        server_version = "PalCodexProxy/0.1"

        def _require_authorized(self) -> bool:
            if _request_authorized(self.headers, api_key):
                return True
            _send_json(
                self,
                401,
                {"error": {"message": "missing or invalid API key", "type": "authentication_error"}},
                headers={"WWW-Authenticate": "Bearer"},
            )
            return False

        def do_GET(self) -> None:
            if not self._require_authorized():
                return
            if self.path.rstrip("/") == "/v1/models":
                _send_json(self, 200, _models_payload(model_ids))
                return
            _send_json(self, 404, {"error": {"message": "not found"}})

        def do_POST(self) -> None:
            _log(f"POST {self.path}")
            if not self._require_authorized():
                return
            if self.path.rstrip("/") != "/v1/chat/completions":
                _send_json(self, 404, {"error": {"message": "not found"}})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
                _log(f"reading request body bytes={length}")
                payload = json.loads(self.rfile.read(length) or b"{}")
                _log(f"request parsed stream={bool(payload.get('stream'))} model={payload.get('model')}")
                if payload.get("stream"):
                    _send_sse_headers(self)
                    try:
                        _log("waiting for codex concurrency slot")
                        with semaphore or contextlib.nullcontext():
                            _log("starting streaming codex invocation")
                            for chunk in bridge.iter_stream(payload):
                                _write_sse(self, chunk)
                        _write_sse(self, "[DONE]")
                        self.close_connection = True
                        _log("streaming codex invocation completed")
                    except Exception as exc:
                        _log(f"streaming codex invocation failed: {type(exc).__name__}: {exc}")
                        _write_sse(self, {"error": {"message": str(exc), "type": type(exc).__name__}})
                        _write_sse(self, "[DONE]")
                        self.close_connection = True
                    return
                _log("waiting for codex concurrency slot")
                with semaphore or contextlib.nullcontext():
                    _log("starting codex invocation")
                    completion = bridge.invoke(payload)
                _log("codex invocation completed")
                _send_json(self, 200, _completion_payload(completion))
            except Exception as exc:
                _log(f"codex invocation failed: {type(exc).__name__}: {exc}")
                _send_json(self, 500, {"error": {"message": str(exc), "type": type(exc).__name__}})

        def log_message(self, format: str, *args: Any) -> None:
            return

    return CodexProxyHandler


def run_codex_proxy_cli(
    *,
    host: str,
    port: int,
    codex_bin: str | None = None,
    timeout_seconds: int = 120,
    api_key_env: str = DEFAULT_CODEX_PROXY_API_KEY_ENV,
    models_env: str = DEFAULT_CODEX_PROXY_MODELS_ENV,
    max_concurrency: int | None = None,
    max_concurrency_env: str = DEFAULT_CODEX_PROXY_MAX_CONCURRENCY_ENV,
) -> int:
    bridge = CodexAppServerBridge(codex_bin=codex_bin or _default_codex_command(), timeout_seconds=timeout_seconds)
    api_key = os.environ.get(api_key_env, "").strip() if api_key_env else ""
    model_ids = _parse_model_list(os.environ.get(models_env, "") if models_env else "")
    concurrency = _parse_max_concurrency(
        max_concurrency if max_concurrency is not None else os.environ.get(max_concurrency_env, "")
    )
    semaphore = threading.BoundedSemaphore(concurrency)
    server = ThreadingHTTPServer(
        (host, port),
        _make_handler(bridge, api_key=api_key, model_ids=model_ids, semaphore=semaphore),
    )
    print(f"Pal Codex proxy listening on http://{host}:{port}/v1")
    print(f"Using Codex command: {bridge.codex_bin}")
    print(f"Advertised models: {', '.join(model_ids)}")
    print(f"Max concurrent Codex requests: {concurrency}")
    print(f"API key auth: {'enabled' if api_key else 'disabled'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
