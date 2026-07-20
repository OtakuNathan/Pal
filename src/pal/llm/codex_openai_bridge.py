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
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from collections.abc import Iterable
from typing import Any


_BRIDGE_DEVELOPER_GUARD = (
    "You are serving as the language model behind Pal's local OpenAI-compatible Codex bridge. "
    "Pal owns memory, capability lookup, tool execution, approvals, scheduling, and plugin policy. "
    "Do not use built-in Codex tools to read local files, execute shell commands, edit files, or browse. "
    "Those actions are available only through Pal-provided dynamic tools when present. "
    "If a dynamic tool such as shell is provided, it is the authorized shell path for this turn; use it instead of claiming shell access is unavailable. "
    "If a needed dynamic tool is absent, use Pal discovery tools when provided, or state that the specific Pal capability is unavailable only after checking the current tool surface. "
    "Answer from the provided conversation or request one of the provided dynamic tools."
)
DEFAULT_CODEX_BRIDGE_API_KEY_ENV = "PAL_CODEX_BRIDGE_API_KEY"
DEFAULT_CODEX_BRIDGE_MODELS_ENV = "PAL_CODEX_BRIDGE_MODELS"
DEFAULT_CODEX_BRIDGE_MAX_CONCURRENCY_ENV = "PAL_CODEX_BRIDGE_MAX_CONCURRENCY"
LEGACY_CODEX_BRIDGE_API_KEY_ENV = "PAL_CODEX_PROXY_API_KEY"
LEGACY_CODEX_BRIDGE_MODELS_ENV = "PAL_CODEX_PROXY_MODELS"
LEGACY_CODEX_BRIDGE_MAX_CONCURRENCY_ENV = "PAL_CODEX_PROXY_MAX_CONCURRENCY"
DEFAULT_CODEX_BRIDGE_MAX_CONCURRENCY = 3
DEFAULT_CODEX_BRIDGE_MODELS = (
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
)
_CODEX_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}


class CodexBridgeError(RuntimeError):
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


@dataclass(frozen=True)
class _PendingCodexToolRequest:
    request_id: Any
    thread_id: str | None
    turn_id: str | None
    call_id: str
    tool_name: str
    arguments: Any


@dataclass
class _ActiveCodexTurn:
    model: str
    thread_id: str
    turn_id: str
    text_parts: list[str] = field(default_factory=list)
    agent_message_completed: bool = False


def _chat_completion_id() -> str:
    return f"chatcmpl-pal-codex-{int(time.time())}"


def _response_id() -> str:
    return f"resp_pal_codex_{int(time.time())}"


def _strip_openai_prefix(model: str) -> str:
    text = str(model or "").strip()
    for prefix in ("openai/", "hosted_vllm/", "lm_studio/", "llamafile/"):
        if text.startswith(prefix):
            return text.removeprefix(prefix) or "gpt-5.4"
    return text or "gpt-5.4"


def _parse_model_list(raw: str | None) -> tuple[str, ...]:
    text = str(raw or "").strip()
    if not text:
        return DEFAULT_CODEX_BRIDGE_MODELS
    items: list[str] = []
    seen: set[str] = set()
    for chunk in text.replace("\n", ",").split(","):
        model = _strip_openai_prefix(chunk)
        if not model or model in seen:
            continue
        seen.add(model)
        items.append(model)
    return tuple(items) or DEFAULT_CODEX_BRIDGE_MODELS


def _models_payload(model_ids: tuple[str, ...]) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": model_id, "object": "model", "owned_by": "openai"} for model_id in model_ids],
    }


def _parse_max_concurrency(raw: str | int | None) -> int:
    try:
        parsed = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_CODEX_BRIDGE_MAX_CONCURRENCY
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


def _codex_cli_config_effort(effort: str | None) -> str:
    normalized = str(effort or "").strip().lower()
    if normalized in {"high", "xhigh"}:
        return normalized
    if normalized in {"none", "minimal", "low"}:
        return "low"
    return "medium"


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


def _tool_result_content_for_call(messages: list[dict[str, Any]], call_id: str) -> str | None:
    expected = str(call_id or "").strip()
    if not expected:
        return None
    normalized_expected = expected.casefold()
    for message in reversed(list(messages or [])):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").strip() != "tool":
            continue
        actual = str(message.get("tool_call_id") or "").strip()
        if actual != expected and actual.casefold() != normalized_expected:
            continue
        return _message_content_text(message.get("content"))
    return None


def _dynamic_tool_response_from_text(text: str, *, success: bool = True) -> dict[str, Any]:
    return {
        "contentItems": [{"type": "inputText", "text": str(text or "")}],
        "success": bool(success),
    }


def _messages_to_codex_turn(messages: list[dict[str, Any]]) -> tuple[str, str]:
    developer_instructions, input_items = _messages_to_codex_input(messages)
    text = "\n\n".join(str(item.get("text") or "") for item in input_items if item.get("type") == "text").strip()
    return developer_instructions, text or "Continue."


def _messages_to_codex_input(messages: list[dict[str, Any]], *, artifact_manager: Any = None) -> tuple[str, list[dict[str, Any]]]:
    developer_parts = [_BRIDGE_DEVELOPER_GUARD]
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
                "inputSchema": function.get("input_schema")
                or function.get("parameters")
                or {"type": "object"},
            }
        )
    return dynamic_tools


def _responses_tools_to_dynamic_tools(tools: Any) -> list[dict[str, Any]]:
    dynamic_tools: list[dict[str, Any]] = []
    for tool in list(tools or []):
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        dynamic_tools.append(
            {
                "name": name,
                "description": str(tool.get("description") or name),
                "inputSchema": tool.get("input_schema") or tool.get("parameters") or {"type": "object"},
            }
        )
    return dynamic_tools


def _responses_payload_to_codex_input(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    developer_parts = [_BRIDGE_DEVELOPER_GUARD]
    instructions = str(payload.get("instructions") or "").strip()
    if instructions:
        developer_parts.append(instructions)
    transcript: list[str] = []
    input_items: list[dict[str, Any]] = []

    def flush_text() -> None:
        text = "\n\n".join(part for part in transcript if part).strip()
        if text:
            input_items.append({"type": "text", "text": text})
        transcript.clear()

    raw_input = payload.get("input")
    if isinstance(raw_input, str):
        transcript.append(f"User:\n{raw_input}")
    else:
        for item in list(raw_input or []):
            if isinstance(item, str):
                transcript.append(f"User:\n{item}")
                continue
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").strip()
            role = str(item.get("role") or "").strip()
            if role in {"system", "developer"}:
                text, _ = _responses_content_text_and_images(item.get("content"))
                if text:
                    developer_parts.append(text)
                continue
            if item_type == "function_call_output":
                transcript.append(
                    f"Tool result ({item.get('call_id') or 'tool'}):\n{item.get('output') or ''}"
                )
                continue
            if item_type == "function_call":
                transcript.append(
                    "Assistant requested tool calls:\n"
                    f"- {item.get('name')}: {item.get('arguments')}"
                )
                continue
            text, image_items = _responses_content_text_and_images(item.get("content"))
            if role == "assistant" or item_type == "message" and role == "assistant":
                if text:
                    transcript.append(f"Assistant:\n{text}")
            else:
                if text:
                    transcript.append(f"User:\n{text}")
                if image_items:
                    flush_text()
                    input_items.extend(image_items)
    flush_text()
    if not input_items:
        input_items.append({"type": "text", "text": "Continue."})
    return "\n\n".join(developer_parts), input_items


def _responses_content_text_and_images(content: Any) -> tuple[str, list[dict[str, Any]]]:
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
        if kind in {"text", "input_text", "output_text"}:
            text_parts.append(str(item.get("text") or ""))
            continue
        if kind in {"image_url", "input_image"}:
            image = item.get("image_url")
            if isinstance(image, dict):
                image = image.get("url")
            if image:
                image_items.append({"type": "image", "url": str(image)})
    return "\n".join(part for part in text_parts if part), image_items


def _responses_input_tool_messages(raw_input: Any) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for item in list(raw_input or []):
        if not isinstance(item, dict) or item.get("type") != "function_call_output":
            continue
        call_id = str(item.get("call_id") or "").strip()
        if not call_id:
            continue
        messages.append({"role": "tool", "tool_call_id": call_id, "content": str(item.get("output") or "")})
    return messages


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


def _responses_payload(completion: CodexCompletion) -> dict[str, Any]:
    created = int(time.time())
    response_id = _response_id()
    output: list[dict[str, Any]] = []
    if completion.tool_call is not None:
        output.append(
            {
                "id": f"fc_{completion.tool_call.call_id}",
                "type": "function_call",
                "status": "completed",
                "call_id": completion.tool_call.call_id,
                "name": completion.tool_call.name,
                "arguments": json.dumps(completion.tool_call.arguments, ensure_ascii=False),
            }
        )
    else:
        output.append(
            {
                "id": f"msg_{created}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": completion.text,
                        "annotations": [],
                    }
                ],
            }
        )
    return {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": "completed",
        "model": completion.model,
        "output": output,
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
class CodexCliBridge:
    codex_bin: str = ""
    timeout_seconds: int = 45
    cwd: str | None = None
    default_effort: str = "medium"
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _proc: subprocess.Popen[str] | None = field(default=None, init=False, repr=False)
    _seq: int = field(default=0, init=False, repr=False)
    _initialized: bool = field(default=False, init=False, repr=False)
    _process_effort: str | None = field(default=None, init=False, repr=False)
    _active_turn: _ActiveCodexTurn | None = field(default=None, init=False, repr=False)
    _pending_tool_request: _PendingCodexToolRequest | None = field(default=None, init=False, repr=False)

    def _start_process(self, *, effort: str | None = None) -> subprocess.Popen[str]:
        codex_bin = self.codex_bin or _default_codex_command()
        command = [
            codex_bin,
            "app-server",
            "-c",
            f'model_reasoning_effort="{_codex_cli_config_effort(effort or self.default_effort)}"',
            "--listen",
            "stdio://",
        ]
        proc = subprocess.Popen(
            command,
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
        messages = list(payload.get("messages") or [])
        developer_instructions, input_items = _messages_to_codex_input(messages)
        dynamic_tools = _openai_tools_to_dynamic_tools(payload.get("tools"))
        effort = _codex_effort_from_payload(payload)
        return self.invoke_turn(
            model=model,
            developer_instructions=developer_instructions,
            input_items=input_items,
            dynamic_tools=dynamic_tools,
            effort=effort,
            messages=messages,
        )

    def invoke_responses(self, payload: dict[str, Any]) -> CodexCompletion:
        model = _strip_openai_prefix(str(payload.get("model") or ""))
        developer_instructions, input_items = _responses_payload_to_codex_input(payload)
        dynamic_tools = _responses_tools_to_dynamic_tools(payload.get("tools"))
        effort = _codex_effort_from_payload(payload)
        return self.invoke_turn(
            model=model,
            developer_instructions=developer_instructions,
            input_items=input_items,
            dynamic_tools=dynamic_tools,
            effort=effort,
            messages=_responses_input_tool_messages(payload.get("input")),
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
        messages: list[dict[str, Any]] | None = None,
    ) -> CodexCompletion:
        with self._lock:
            proc = self._ensure_process(effort=effort)
            try:
                if self._pending_tool_request is not None:
                    if not self._resume_pending_tool_request(proc, messages or []):
                        pending = self._pending_tool_request
                        raise CodexBridgeError(
                            "Codex app-server is waiting for a Pal tool result "
                            f"for call_id={pending.call_id!r}"
                        )
                    if self._active_turn is None:
                        raise CodexBridgeError("Codex app-server lost active turn after tool response")
                    deadline = time.time() + max(1, int(self.timeout_seconds))
                    return self._continue_process_turn(proc, self._active_turn, deadline=deadline)
                return self._invoke_process(
                    proc,
                    model=model,
                    developer_instructions=developer_instructions,
                    input_text=input_text,
                    input_items=input_items,
                    dynamic_tools=dynamic_tools,
                    effort=effort,
                )
            except Exception:
                self._reset_process()
                raise

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
        with self._lock:
            proc = self._ensure_process(effort=effort)
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
            except GeneratorExit:
                self._reset_process()
                raise
            except Exception:
                self._reset_process()
                raise

    def close(self) -> None:
        with self._lock:
            self._reset_process()

    def _ensure_process(self, *, effort: str | None = None) -> subprocess.Popen[str]:
        process_effort = _codex_cli_config_effort(effort or self.default_effort)
        proc = self._proc
        if proc is not None and proc.poll() is None:
            if self._process_effort != process_effort:
                self._reset_process()
            else:
                if not self._initialized:
                    self._initialize_process(proc)
                    self._initialized = True
                return proc

        self._reset_process()
        proc = self._start_process(effort=process_effort)
        self._proc = proc
        self._seq = 0
        self._initialized = False
        self._process_effort = process_effort
        self._initialize_process(proc)
        self._initialized = True
        return proc

    def _reset_process(self) -> None:
        proc = self._proc
        self._proc = None
        self._seq = 0
        self._initialized = False
        self._process_effort = None
        self._active_turn = None
        self._pending_tool_request = None
        if proc is not None:
            self._stop_process(proc)

    def _send_request(self, proc: subprocess.Popen[str], method: str, params: dict[str, Any] | None = None) -> int:
        self._seq += 1
        request_id = self._seq
        message: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        self._write_message(proc, message)
        _debug_log(f"codex send id={request_id} method={method}")
        return request_id

    def _write_notification(self, proc: subprocess.Popen[str], method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        self._write_message(proc, message)
        _debug_log(f"codex send notification method={method}")

    @staticmethod
    def _write_message(proc: subprocess.Popen[str], message: dict[str, Any]) -> None:
        if proc.stdin is None:
            raise CodexBridgeError("Codex app-server stdin is not available")
        try:
            proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise CodexBridgeError("failed writing to Codex app-server") from exc

    def _initialize_process(self, proc: subprocess.Popen[str]) -> None:
        deadline = time.time() + max(1, int(self.timeout_seconds))
        initialize_id = self._send_request(
            proc,
            "initialize",
            {
                "clientInfo": {"name": "pal-codex-bridge", "title": "Pal Codex Bridge", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        self._write_notification(proc, "initialized", {})
        while time.time() < deadline:
            item = self._read_message(proc, deadline=deadline)
            _debug_log(f"codex init recv id={item.get('id')} method={item.get('method')}")
            if item.get("error"):
                raise CodexBridgeError(str(item["error"]))
            if item.get("id") == initialize_id:
                return
        raise CodexBridgeError("timed out initializing Codex app-server")

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
        thread_params: dict[str, Any] = {
            "ephemeral": True,
            "developerInstructions": developer_instructions,
            "model": model,
        }
        if dynamic_tools:
            thread_params["dynamicTools"] = dynamic_tools
        thread_id_request = self._send_request(proc, "thread/start", thread_params)
        turn_id_request: int | None = None
        thread_id: str | None = None
        turn_id: str | None = None
        deadline = time.time() + max(1, int(self.timeout_seconds))

        while time.time() < deadline:
            item = self._read_message(proc, deadline=deadline)
            _debug_log(f"codex recv id={item.get('id')} method={item.get('method')}")
            if item.get("error"):
                raise CodexBridgeError(str(item["error"]))
            if item.get("id") == thread_id_request:
                thread_id = ((item.get("result") or {}).get("thread") or {}).get("id")
                if not thread_id:
                    raise CodexBridgeError(f"thread/start returned no thread id: {item}")
                turn_id_request = self._send_request(
                    proc,
                    "turn/start",
                    _turn_start_params(thread_id=thread_id, input_text=input_text, input_items=input_items, model=model, effort=effort),
                )
                continue
            if turn_id_request is not None and item.get("id") == turn_id_request:
                turn_id = ((item.get("result") or {}).get("turn") or {}).get("id")
                if not thread_id or not turn_id:
                    raise CodexBridgeError(f"turn/start returned no turn id: {item}")
                active = _ActiveCodexTurn(model=model, thread_id=thread_id, turn_id=turn_id)
                self._active_turn = active
                return self._continue_process_turn(proc, active, deadline=deadline)
            if "method" in item and "id" in item:
                self._respond_to_unsupported_server_request(proc, item)
        raise CodexBridgeError("timed out waiting for Codex app-server response")

    def _continue_process_turn(
        self,
        proc: subprocess.Popen[str],
        active: _ActiveCodexTurn,
        *,
        deadline: float,
    ) -> CodexCompletion:
        while time.time() < deadline:
            item = self._read_message(proc, deadline=deadline)
            _debug_log(f"codex recv id={item.get('id')} method={item.get('method')}")
            if item.get("error"):
                raise CodexBridgeError(str(item["error"]))
            method = str(item.get("method") or "")
            params = item.get("params") or {}
            if method and not self._matches_current_turn(params, thread_id=active.thread_id, turn_id=active.turn_id):
                continue
            if method == "item/tool/call":
                return self._capture_tool_call(item, params, active)
            if "id" in item:
                if method:
                    self._respond_to_unsupported_server_request(proc, item)
                continue
            if method == "item/agentMessage/delta":
                active.text_parts.append(str(params.get("delta") or params.get("text") or ""))
            elif method == "item/completed":
                completed_item = params.get("item") or {}
                if completed_item.get("type") == "agentMessage":
                    active.agent_message_completed = True
                    completed_text = str(completed_item.get("text") or "")
                    if completed_text:
                        active.text_parts = [completed_text]
            elif method == "thread/status/changed":
                status = params.get("status") or {}
                if status.get("type") == "idle" and active.agent_message_completed:
                    return self._finish_active_turn(active)
            elif method == "turn/completed":
                return self._finish_active_turn(active)
        raise CodexBridgeError("timed out waiting for Codex app-server response")

    def _capture_tool_call(
        self,
        item: dict[str, Any],
        params: dict[str, Any],
        active: _ActiveCodexTurn,
    ) -> CodexCompletion:
        call_id = str(params.get("callId") or f"call_{int(time.time())}")
        tool_name = str(params.get("tool") or "")
        request_id = item.get("id")
        active.text_parts = []
        active.agent_message_completed = False
        if request_id is not None:
            self._pending_tool_request = _PendingCodexToolRequest(
                request_id=request_id,
                thread_id=str(params.get("threadId") or active.thread_id),
                turn_id=str(params.get("turnId") or active.turn_id),
                call_id=call_id,
                tool_name=tool_name,
                arguments=params.get("arguments"),
            )
            self._active_turn = active
        return CodexCompletion(
            model=active.model,
            tool_call=CodexToolCall(
                call_id=call_id,
                name=tool_name,
                arguments=params.get("arguments"),
            ),
        )

    def _finish_active_turn(self, active: _ActiveCodexTurn) -> CodexCompletion:
        text = "".join(active.text_parts).strip()
        self._active_turn = None
        self._pending_tool_request = None
        return CodexCompletion(model=active.model, text=text)

    def _resume_pending_tool_request(self, proc: subprocess.Popen[str], messages: list[dict[str, Any]]) -> bool:
        pending = self._pending_tool_request
        if pending is None:
            return False
        content = _tool_result_content_for_call(messages, pending.call_id)
        if content is None:
            return False
        self._write_message(
            proc,
            {
                "id": pending.request_id,
                "result": _dynamic_tool_response_from_text(content),
            },
        )
        _debug_log(f"codex dynamic tool response id={pending.request_id} call_id={pending.call_id}")
        self._pending_tool_request = None
        if self._active_turn is not None:
            self._active_turn.text_parts = []
            self._active_turn.agent_message_completed = False
        return True

    def _respond_to_unsupported_server_request(
        self,
        proc: subprocess.Popen[str],
        item: dict[str, Any],
    ) -> None:
        request_id = item.get("id")
        method = str(item.get("method") or "")
        if request_id is None or not method:
            return
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            result: dict[str, Any] = {"decision": "decline"}
        else:
            result = {}
        self._write_message(proc, {"id": request_id, "result": result})

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
        thread_params: dict[str, Any] = {
            "ephemeral": True,
            "developerInstructions": developer_instructions,
            "model": model,
        }
        if dynamic_tools:
            thread_params["dynamicTools"] = dynamic_tools
        thread_id_request = self._send_request(proc, "thread/start", thread_params)
        turn_id_request: int | None = None
        thread_id: str | None = None
        turn_id: str | None = None
        turn_started = False
        agent_message_completed = False
        deadline = time.time() + max(1, int(self.timeout_seconds))
        text_parts: list[str] = []

        while time.time() < deadline:
            item = self._read_message(proc, deadline=deadline)
            _debug_log(f"codex stream recv id={item.get('id')} method={item.get('method')}")
            if item.get("error"):
                raise CodexBridgeError(str(item["error"]))
            if item.get("id") == thread_id_request:
                thread_id = ((item.get("result") or {}).get("thread") or {}).get("id")
                if not thread_id:
                    raise CodexBridgeError(f"thread/start returned no thread id: {item}")
                turn_id_request = self._send_request(
                    proc,
                    "turn/start",
                    _turn_start_params(thread_id=thread_id, input_text=input_text, input_items=input_items, model=model, effort=effort),
                )
                continue
            if turn_id_request is not None and item.get("id") == turn_id_request:
                turn_id = ((item.get("result") or {}).get("turn") or {}).get("id")
                turn_started = True
                continue
            if "id" in item:
                continue
            if not turn_started:
                continue
            method = str(item.get("method") or "")
            params = item.get("params") or {}
            if not self._matches_current_turn(params, thread_id=thread_id, turn_id=turn_id):
                continue
            if method == "item/tool/call":
                tool_call = CodexToolCall(
                    call_id=str(params.get("callId") or f"call_{int(time.time())}"),
                    name=str(params.get("tool") or ""),
                    arguments=params.get("arguments"),
                )
                self._drain_ready_messages(proc)
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
                    agent_message_completed = True
                    if completed_text:
                        text_parts = [completed_text]
            elif method == "thread/status/changed":
                status = params.get("status") or {}
                if status.get("type") == "idle" and agent_message_completed:
                    text = "".join(text_parts).strip()
                    if text:
                        yield _stream_delta_payload(model, role="assistant")
                        yield _stream_delta_payload(model, content=text)
                    yield _stream_done_payload(model, "stop")
                    return
            elif method == "turn/completed":
                text = "".join(text_parts).strip()
                if text:
                    yield _stream_delta_payload(model, role="assistant")
                    yield _stream_delta_payload(model, content=text)
                yield _stream_done_payload(model, "stop")
                return
        raise CodexBridgeError("timed out waiting for Codex app-server stream")

    def _drain_ready_messages(self, proc: subprocess.Popen[str], *, seconds: float = 0.05) -> None:
        stdout = getattr(proc, "stdout", None)
        if stdout is None:
            return
        streams = [stdout]
        stderr = getattr(proc, "stderr", None)
        if stderr is not None:
            streams.append(stderr)
        deadline = time.time() + max(0.0, seconds)
        while time.time() < deadline:
            try:
                ready, _, _ = select.select(streams, [], [], max(0.0, deadline - time.time()))
            except (OSError, ValueError):
                return
            if not ready:
                return
            if stderr is not None and stderr in ready:
                line = stderr.readline()
                if line:
                    _debug_log(f"codex stderr: {line.rstrip()[:1000]}")
                continue
            if stdout in ready:
                line = stdout.readline()
                if not line:
                    return
                _debug_log(f"codex drained method={_safe_json_method(line)}")

    @staticmethod
    def _matches_current_turn(params: dict[str, Any], *, thread_id: str | None, turn_id: str | None) -> bool:
        event_thread_id = params.get("threadId")
        if thread_id is not None and event_thread_id is not None and event_thread_id != thread_id:
            return False
        event_turn_id = params.get("turnId")
        if turn_id is not None and event_turn_id is not None and event_turn_id != turn_id:
            return False
        return True

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
                    raise CodexBridgeError("Codex app-server exited without output")
                try:
                    return json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CodexBridgeError(f"invalid Codex app-server JSON line: {line[:500]}") from exc
        raise CodexBridgeError(
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
    print(f"[pal-codex-bridge] {message}", flush=True)


def _debug_log(message: str) -> None:
    if os.environ.get("PAL_CODEX_BRIDGE_DEBUG") == "1" or os.environ.get("PAL_CODEX_PROXY_DEBUG") == "1":
        _log(message)


def _safe_json_method(line: str) -> str:
    try:
        item = json.loads(line)
    except json.JSONDecodeError:
        return "<invalid>"
    return str(item.get("method") or item.get("id") or "<unknown>")


def _make_handler(
    bridge: CodexCliBridge,
    *,
    api_key: str | None = None,
    model_ids: tuple[str, ...] = DEFAULT_CODEX_BRIDGE_MODELS,
    semaphore: threading.BoundedSemaphore | None = None,
):
    class CodexOpenAIBridgeHandler(BaseHTTPRequestHandler):
        server_version = "PalCodexBridge/0.1"

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
            path = self.path.rstrip("/")
            if path not in {"/v1/chat/completions", "/v1/responses"}:
                _send_json(self, 404, {"error": {"message": "not found"}})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
                _log(f"reading request body bytes={length}")
                payload = json.loads(self.rfile.read(length) or b"{}")
                _log(f"request parsed stream={bool(payload.get('stream'))} model={payload.get('model')}")
                if path == "/v1/responses":
                    if payload.get("stream"):
                        _send_json(self, 400, {"error": {"message": "streaming responses are not supported yet"}})
                        return
                    _log("waiting for codex concurrency slot")
                    with semaphore or contextlib.nullcontext():
                        _log("starting codex responses invocation")
                        completion = bridge.invoke_responses(payload)
                    _log("codex responses invocation completed")
                    _send_json(self, 200, _responses_payload(completion))
                    return
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

    return CodexOpenAIBridgeHandler


def run_codex_openai_bridge_cli(
    *,
    host: str,
    port: int,
    codex_bin: str | None = None,
    timeout_seconds: int = 120,
    api_key_env: str = DEFAULT_CODEX_BRIDGE_API_KEY_ENV,
    models_env: str = DEFAULT_CODEX_BRIDGE_MODELS_ENV,
    max_concurrency: int | None = None,
    max_concurrency_env: str = DEFAULT_CODEX_BRIDGE_MAX_CONCURRENCY_ENV,
) -> int:
    bridge = CodexCliBridge(codex_bin=codex_bin or _default_codex_command(), timeout_seconds=timeout_seconds)
    api_key = _env_value(
        api_key_env,
        legacy_env=LEGACY_CODEX_BRIDGE_API_KEY_ENV if api_key_env == DEFAULT_CODEX_BRIDGE_API_KEY_ENV else None,
    )
    model_ids = _parse_model_list(
        _env_value(
            models_env,
            legacy_env=LEGACY_CODEX_BRIDGE_MODELS_ENV if models_env == DEFAULT_CODEX_BRIDGE_MODELS_ENV else None,
        )
    )
    concurrency = _parse_max_concurrency(
        max_concurrency
        if max_concurrency is not None
        else _env_value(
            max_concurrency_env,
            legacy_env=(
                LEGACY_CODEX_BRIDGE_MAX_CONCURRENCY_ENV
                if max_concurrency_env == DEFAULT_CODEX_BRIDGE_MAX_CONCURRENCY_ENV
                else None
            ),
        )
    )
    semaphore = threading.BoundedSemaphore(concurrency)
    server = ThreadingHTTPServer(
        (host, port),
        _make_handler(bridge, api_key=api_key, model_ids=model_ids, semaphore=semaphore),
    )
    print(f"Pal Codex OpenAI bridge listening on http://{host}:{port}/v1")
    print(f"Using Codex command: {bridge.codex_bin}")
    print(f"Advertised models: {', '.join(model_ids)}")
    print(f"Max concurrent Codex requests: {concurrency}")
    print(f"API key auth: {'enabled' if api_key else 'disabled'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close()
        server.server_close()
    return 0


def _env_value(primary_env: str | None, *, legacy_env: str | None = None) -> str:
    if primary_env:
        value = os.environ.get(primary_env, "").strip()
        if value:
            return value
    if legacy_env:
        return os.environ.get(legacy_env, "").strip()
    return ""
