from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import signal
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from pal.shared.diagnostics import diagnostic_text, exception_summary

from pal.mcp.model import McpProtocolError, McpRemoteError, McpPromptSpec, McpServerConfig, McpToolSpec
from pal.mcp.normalize import normalize_prompt_payload, normalize_tool_payload
from pal.mcp.protocol import PROTOCOL_VERSION, validate_message


from pal.mcp.contracts import McpConnector as McpConnector

_ENV_PATTERN = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def _expand_env_vars(value: str, env: dict[str, str]) -> str:
    """Expand ``${VAR}`` and ``$VAR`` references using the merged env dict."""

    def replacer(match: re.Match[str]) -> str:
        key = match.group(1) or match.group(2)
        return env.get(key, match.group(0))

    return _ENV_PATTERN.sub(replacer, value)


@dataclass
class AsyncStdioMcpConnector:
    config: McpServerConfig
    process: asyncio.subprocess.Process | None = None
    server_info: dict[str, Any] = field(default_factory=dict)
    server_capabilities: dict[str, Any] = field(default_factory=dict)
    _next_id: int = 1
    _pending: dict[int, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    _id_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _reader_task: asyncio.Task[None] | None = None
    _closed: bool = False
    _fatal_error: McpProtocolError | None = None
    stderr_path: str = ""
    exit_code: int | None = None
    process_id: int | None = None
    _lifecycle_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def stderr_tail(self, limit: int = 20) -> tuple[str, ...]:
        if not self.stderr_path or limit <= 0:
            return ()
        try:
            with Path(self.stderr_path).open("rb") as log:
                log.seek(0, os.SEEK_END)
                log.seek(max(0, log.tell() - 8192))
                text = diagnostic_text(log.read().decode("utf-8", errors="replace"), limit=None)
            return tuple(text.splitlines()[-limit:])
        except OSError as exc:
            return (f"Stderr log unavailable at {self.stderr_path}: {exception_summary(exc)}",)

    def diagnostics(self) -> dict[str, Any]:
        return {"process_id": self.process_id, "stderr_path": self.stderr_path, "stderr_tail": list(self.stderr_tail()),
                "exit_code": self.process.returncode if self.process is not None else self.exit_code}

    async def initialize(self) -> None:
        async with self._lifecycle_lock:
            if self.config.protocol_version != PROTOCOL_VERSION:
                raise McpProtocolError(f"Unsupported MCP version: {self.config.protocol_version}; supported: {PROTOCOL_VERSION}")
            await self._start()
            result = await self._request(
                "initialize",
                {
                    "protocolVersion": self.config.protocol_version,
                    "capabilities": {},
                    "clientInfo": {"name": "Pal", "version": "0.1.0"},
                },
                timeout_ms=self.config.startup_timeout_ms,
            )
            validate_message(result, "InitializeResult")
            if result["protocolVersion"] != PROTOCOL_VERSION:
                raise McpProtocolError("MCP negotiated an unsupported protocol version", payload={"raw_response": result})
            self.server_info = dict(result["serverInfo"])
            self.server_capabilities = dict(result.get("capabilities") or {})
            await self._notify("notifications/initialized")

    async def list_tools_all(self) -> tuple[McpToolSpec, ...]:
        tools: list[McpToolSpec] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = await self._request("tools/list", params)
            validate_message(result, "ListToolsResult")
            tools.extend(normalize_tool_payload(item) for item in result["tools"])
            cursor = result.get("nextCursor")
            if cursor is None:
                break
            if cursor in seen_cursors:
                raise McpProtocolError("MCP pagination repeated a cursor", payload={"raw_response": result})
            seen_cursors.add(cursor)
        return tuple(tools)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._request("tools/call", {"name": tool_name, "arguments": dict(arguments or {})})

    async def list_prompts_all(self) -> tuple[McpPromptSpec, ...]:
        prompts: list[McpPromptSpec] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = await self._request("prompts/list", params)
            validate_message(result, "ListPromptsResult")
            prompts.extend(normalize_prompt_payload(item) for item in result["prompts"])
            cursor = result.get("nextCursor")
            if cursor is None:
                break
            if cursor in seen_cursors:
                raise McpProtocolError("MCP pagination repeated a cursor", payload={"raw_response": result})
            seen_cursors.add(cursor)
        return tuple(prompts)

    async def get_prompt(self, prompt_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._request("prompts/get", {"name": prompt_name, "arguments": dict(arguments or {})})

    async def close(self) -> None:
        async with self._lifecycle_lock:
            if self._closed and self.process is None:
                return
            self._closed = True
            process = self.process
            # Withdraw the only discoverable process authority before cleanup.
            self.process = None
            if process is not None:
                self.exit_code = process.returncode
            tasks = (self._reader_task,)
            self._reader_task = None
            for task in tasks:
                if task is not None:
                    task.cancel()
            for task in tasks:
                if task is not None:
                    with _suppress_all():
                        await task
            self._fail_pending(McpProtocolError("MCP connector closed"))
            if process is None:
                return
            _terminate_async_process_once(process, force=self.config.kill_on_close)
            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=max(self.config.shutdown_timeout_ms, 1) / 1000.0,
                )
                self.exit_code = process.returncode
            except asyncio.TimeoutError as exc:
                raise McpProtocolError(
                    "MCP process did not exit after its one-shot termination; replacement is fenced"
                ) from exc

    async def _start(self) -> None:
        if self.process is not None and self.process.returncode is None:
            return
        if not self.config.command:
            raise McpProtocolError("MCP command is required")
        self._closed = False
        merged_env = {**os.environ, **dict(self.config.env)}
        env = merged_env if self.config.env else None
        command_args = list(self.config.command)
        # Resolve the executable for ordinary PATH/config use, but never copy
        # environment secrets into argv.  Wrappers such as mcp-remote resolve
        # ${VAR} header placeholders from their child environment themselves.
        command_args[0] = _expand_env_vars(command_args[0], merged_env)
        fd, self.stderr_path = tempfile.mkstemp(prefix="pal-mcp-stderr-", suffix=".log")
        # A regular file retains all stderr, including long lines, without
        # backpressure or descendant-held pipes blocking connector shutdown.
        with os.fdopen(fd, "wb") as stderr:
            self.process = await asyncio.create_subprocess_exec(
                *command_args, cwd=self.config.cwd or None, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=stderr, start_new_session=os.name != "nt",
                limit=16 * 1024 * 1024,
            )
        self.process_id = self.process.pid
        self._reader_task = asyncio.create_task(self._read_stdout_loop(), name=f"mcp-{self.config.server_id}-stdout")

    async def _request(self, method: str, params: dict[str, Any] | None = None, *, timeout_ms: int | None = None) -> dict[str, Any]:
        if self._fatal_error is not None:
            raise self._fatal_error
        request_id = await self._allocate_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        try:
            await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params or {})})
            response = await asyncio.wait_for(future, timeout=max(timeout_ms or self.config.request_timeout_ms, 1) / 1000.0)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise McpProtocolError(f"MCP request timed out: {method}") from exc
        finally:
            self._pending.pop(request_id, None)
        validate_message(response, "JSONRPCError" if "error" in response else "JSONRPCResponse")
        if (response.get("jsonrpc") != "2.0" or type(response.get("id")) is not int or response.get("id") != request_id
                or ("error" in response) == ("result" in response)):
            raise McpProtocolError(f"Invalid MCP response envelope: {method}", payload={"raw_response": response})
        if "error" in response:
            raise McpRemoteError(f"MCP request failed: {method}: {response['error']}", payload={"raw_response": response})
        result = response["result"]
        if not isinstance(result, dict):
            raise McpProtocolError(f"MCP {method} result must be an object", payload={"raw_response": response})
        return result

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = dict(params)
        await self._write(payload)

    async def _write(self, payload: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            raise McpProtocolError("MCP process is not running")
        data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._write_lock:
            process.stdin.write(data)
            await process.stdin.drain()

    async def _allocate_id(self) -> int:
        async with self._id_lock:
            value = self._next_id
            self._next_id += 1
            return value

    async def _read_stdout_loop(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                try:
                    text = line.decode("utf-8").strip()
                except UnicodeDecodeError as exc:
                    raise McpProtocolError("MCP stdout is not UTF-8", payload={
                        "raw_response_base64": base64.b64encode(line).decode("ascii")}) from exc
                if not text:
                    continue
                try:
                    def reject_constant(value):
                        raise ValueError(f"Non-JSON numeric constant: {value}")
                    payload = json.loads(text, parse_constant=reject_constant)
                except ValueError as exc:
                    raise McpProtocolError("Invalid JSON on MCP stdout", payload={"raw_response": text}) from exc
                if not isinstance(payload, dict):
                    raise McpProtocolError("MCP response must be an object", payload={"raw_response": payload})
                if "method" in payload:
                    # Notifications are allowed; server requests require capabilities
                    # Pal does not advertise and are rejected explicitly.
                    if "id" in payload and payload.get("method") == "ping":
                        validate_message(payload, "JSONRPCRequest")
                        await self._write({"jsonrpc": "2.0", "id": payload["id"], "result": {}})
                        continue
                    if "id" in payload:
                        raise McpProtocolError("Unsupported MCP server request", payload={"raw_response": payload})
                    validate_message(payload, "JSONRPCNotification")
                    continue
                request_id = payload.get("id")
                if type(request_id) is not int or request_id not in self._pending:
                    raise McpProtocolError("MCP response has an unexpected request id", payload={"raw_response": payload})
                future = self._pending.pop(request_id)
                if not future.done():
                    future.set_result(payload)
        except Exception as exc:
            self._fatal_error = exc if isinstance(exc, McpProtocolError) else McpProtocolError(
                "MCP stdout reader failed", payload={"error": exception_summary(exc)})
            self._fail_pending(self._fatal_error)
        finally:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), .2)
            self.exit_code = process.returncode
            self._fail_pending(McpProtocolError("MCP process stdout closed", payload={
                "stderr_path": self.stderr_path, "stderr_tail": list(self.stderr_tail()),
                "exit_code": self.exit_code,
            }))

    def _fail_pending(self, exc: Exception) -> None:
        for request_id, future in list(self._pending.items()):
            if not future.done():
                future.set_exception(exc)
            self._pending.pop(request_id, None)


class _suppress_all:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return True


def _terminate_async_process_once(
    process: asyncio.subprocess.Process,
    *,
    force: bool,
) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        if os.name == "nt":
            process.kill() if force else process.terminate()
        else:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
