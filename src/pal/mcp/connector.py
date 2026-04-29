from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from pal.mcp.model import McpProtocolError, McpPromptSpec, McpServerConfig, McpToolSpec
from pal.mcp.normalize import normalize_prompt_payload, normalize_tool_payload


class McpConnector(Protocol):
    server_info: dict[str, Any]
    server_capabilities: dict[str, Any]

    async def initialize(self) -> None:
        ...

    async def list_tools_all(self) -> tuple[McpToolSpec, ...]:
        ...

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        ...

    async def list_prompts_all(self) -> tuple[McpPromptSpec, ...]:
        ...

    async def get_prompt(self, prompt_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        ...

    async def close(self) -> None:
        ...


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
    _stderr_task: asyncio.Task[None] | None = None
    _closed: bool = False
    _stderr_lines: list[str] = field(default_factory=list)

    async def initialize(self) -> None:
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
        self.server_info = dict(result.get("serverInfo") or {})
        self.server_capabilities = dict(result.get("capabilities") or {})
        if result.get("protocolVersion") is None:
            raise McpProtocolError("initialize response missing protocolVersion")
        await self._notify("notifications/initialized")

    async def list_tools_all(self) -> tuple[McpToolSpec, ...]:
        tools: list[McpToolSpec] = []
        cursor: str | None = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = await self._request("tools/list", params)
            for item in list(result.get("tools") or []):
                if isinstance(item, dict):
                    tools.append(normalize_tool_payload(item))
            cursor = str(result.get("nextCursor") or "").strip() or None
            if not cursor:
                break
        return tuple(tools)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._request("tools/call", {"name": tool_name, "arguments": dict(arguments or {})})

    async def list_prompts_all(self) -> tuple[McpPromptSpec, ...]:
        prompts: list[McpPromptSpec] = []
        cursor: str | None = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = await self._request("prompts/list", params)
            for item in list(result.get("prompts") or []):
                if isinstance(item, dict):
                    prompts.append(normalize_prompt_payload(item))
            cursor = str(result.get("nextCursor") or "").strip() or None
            if not cursor:
                break
        return tuple(prompts)

    async def get_prompt(self, prompt_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._request("prompts/get", {"name": prompt_name, "arguments": dict(arguments or {})})

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                with _suppress_all():
                    await task
        for request_id, future in list(self._pending.items()):
            if not future.done():
                future.set_exception(McpProtocolError("MCP connector closed"))
            self._pending.pop(request_id, None)
        process = self.process
        if process is None:
            return
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=max(self.config.shutdown_timeout_ms, 1) / 1000.0)
            except Exception:
                if self.config.kill_on_close and process.returncode is None:
                    process.kill()
                    with _suppress_all():
                        await process.wait()
        self.process = None

    async def _start(self) -> None:
        if self.process is not None and self.process.returncode is None:
            return
        if not self.config.command:
            raise McpProtocolError("MCP command is required")
        self._closed = False
        env = None if not self.config.env else {**os.environ, **dict(self.config.env)}
        self.process = await asyncio.create_subprocess_exec(
            *list(self.config.command),
            cwd=self.config.cwd or None,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_stdout_loop(), name=f"mcp-{self.config.server_id}-stdout")
        self._stderr_task = asyncio.create_task(self._drain_stderr_loop(), name=f"mcp-{self.config.server_id}-stderr")

    async def _request(self, method: str, params: dict[str, Any] | None = None, *, timeout_ms: int | None = None) -> dict[str, Any]:
        request_id = await self._allocate_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params or {})})
        try:
            response = await asyncio.wait_for(future, timeout=max(timeout_ms or self.config.request_timeout_ms, 1) / 1000.0)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise McpProtocolError(f"MCP request timed out: {method}") from exc
        if "error" in response:
            raise McpProtocolError(f"MCP request failed: {method}: {response['error']}")
        result = response.get("result")
        return dict(result or {})

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
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    continue
                request_id = payload.get("id")
                if request_id is None:
                    continue
                try:
                    request_id_int = int(request_id)
                except (TypeError, ValueError):
                    continue
                future = self._pending.pop(request_id_int, None)
                if future is not None and not future.done():
                    future.set_result(payload)
        finally:
            self._fail_pending(McpProtocolError("MCP process stdout closed"))

    async def _drain_stderr_loop(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        while True:
            line = await process.stderr.readline()
            if not line:
                break
            stripped = line.decode("utf-8", errors="replace").rstrip("\n")
            if stripped:
                self._stderr_lines.append(stripped)
                if len(self._stderr_lines) > 100:
                    self._stderr_lines = self._stderr_lines[-100:]

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
