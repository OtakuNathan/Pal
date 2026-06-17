from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pal.foundation import utc_now
from pal.lsp.config import LspServerConfig


class LspProtocolError(RuntimeError):
    pass


@dataclass
class AsyncLspConnector:
    config: LspServerConfig
    workspace_root: Path
    process: asyncio.subprocess.Process | None = None
    initialized: bool = False
    server_info: dict[str, Any] = field(default_factory=dict)
    server_capabilities: dict[str, Any] = field(default_factory=dict)
    _next_id: int = 1
    _diagnostics: dict[str, dict[str, Any]] = field(default_factory=dict)
    _open_hashes: dict[str, str] = field(default_factory=dict)
    _stderr_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _stderr_tail: str = field(default="", init=False, repr=False)
    _stderr_tail_limit: int = field(default=4000, init=False, repr=False)

    async def initialize(self) -> None:
        if self.initialized and self.process is not None and self.process.returncode is None:
            return
        await self.close()
        self.process = await asyncio.create_subprocess_exec(
            *self.config.command,
            *self.config.args,
            cwd=str(self.workspace_root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_tail = ""
        self._stderr_task = asyncio.create_task(self._drain_stderr(), name=f"lsp-{self.config.server_id}-stderr")
        result = await asyncio.wait_for(
            self.request(
                "initialize",
                {
                    "processId": None,
                    "rootUri": self.workspace_root.resolve().as_uri(),
                    "capabilities": {},
                    "workspaceFolders": [{"uri": self.workspace_root.resolve().as_uri(), "name": self.workspace_root.name}],
                },
            ),
            timeout=max(0.1, self.config.startup_timeout_ms / 1000),
        )
        self.server_info = dict(result.get("serverInfo") or result.get("server_info") or {})
        self.server_capabilities = dict(result.get("capabilities") or {})
        await self.notify("initialized", {})
        self.initialized = True

    async def close(self) -> None:
        process = self.process
        stderr_task = self._stderr_task
        self.process = None
        self._stderr_task = None
        self.initialized = False
        if process is None:
            await self._cancel_stderr_task(stderr_task)
            return
        if process.returncode is None:
            with contextlib.suppress(Exception):
                await self.request("shutdown", {})
            with contextlib.suppress(Exception):
                await self.notify("exit", {})
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=1.0)
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
        await self._cancel_stderr_task(stderr_task)

    def stderr_tail_text(self) -> str:
        return self._stderr_tail.strip()

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        ident = self._next_id
        self._next_id += 1
        await self._write({"jsonrpc": "2.0", "id": ident, "method": method, "params": dict(params or {})})
        while True:
            message = await asyncio.wait_for(self._read(), timeout=max(0.1, self.config.request_timeout_ms / 1000))
            if "method" in message and "id" not in message:
                self._handle_notification(message)
                continue
            if message.get("id") != ident:
                continue
            if message.get("error"):
                raise LspProtocolError(str(message.get("error")))
            result = message.get("result")
            return dict(result or {}) if isinstance(result, dict) else {"value": result}

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": dict(params or {})})

    async def ensure_document_open(self, file_path: Path, *, language_id: str) -> dict[str, Any]:
        path = Path(file_path).resolve()
        text = path.read_text(encoding="utf-8", errors="replace")
        digest = _sha256_text(text)
        uri = path.as_uri()
        if self._open_hashes.get(uri) == digest:
            return {"uri": uri, "file_sha256": digest, "text": text}
        if uri in self._open_hashes:
            await self.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": 2},
                    "contentChanges": [{"text": text}],
                },
            )
        else:
            await self.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": language_id,
                        "version": 1,
                        "text": text,
                    }
                },
            )
        self._open_hashes[uri] = digest
        return {"uri": uri, "file_sha256": digest, "text": text}

    async def diagnostics(self, file_path: Path, *, language_id: str) -> dict[str, Any]:
        doc = await self.ensure_document_open(file_path, language_id=language_id)
        uri = str(doc["uri"])
        deadline = asyncio.get_running_loop().time() + max(0.1, self.config.diagnostics_timeout_ms / 1000)
        while asyncio.get_running_loop().time() < deadline:
            if uri in self._diagnostics:
                return dict(self._diagnostics[uri])
            try:
                message = await asyncio.wait_for(self._read(), timeout=0.05)
            except asyncio.TimeoutError:
                continue
            self._handle_notification(message)
        return {"status": "pending", "uri": uri, "diagnostics": [], "diagnostics_state": "timed_out"}

    async def _write(self, payload: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            raise LspProtocolError("language server is not running")
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        process.stdin.write(b"Content-Length: " + str(len(raw)).encode("ascii") + b"\r\n\r\n" + raw)
        await process.stdin.drain()

    async def _read(self) -> dict[str, Any]:
        process = self.process
        if process is None or process.stdout is None:
            raise LspProtocolError("language server is not running")
        content_length = 0
        while True:
            line = await process.stdout.readline()
            if not line:
                raise LspProtocolError("language server stdout closed")
            stripped = line.strip()
            if not stripped:
                break
            key, _, value = stripped.partition(b":")
            if key.lower() == b"content-length":
                content_length = int(value.strip())
        if content_length <= 0:
            raise LspProtocolError("LSP message lacks Content-Length")
        raw = await process.stdout.readexactly(content_length)
        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise LspProtocolError("LSP payload must be an object")
        return decoded

    async def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                chunk = await process.stderr.read(4096)
                if not chunk:
                    return
                self._append_stderr_tail(chunk.decode("utf-8", errors="replace"))
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _cancel_stderr_task(self, task: asyncio.Task[None] | None) -> None:
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _append_stderr_tail(self, text: str) -> None:
        if not text:
            return
        self._stderr_tail = (self._stderr_tail + text)[-self._stderr_tail_limit :]

    def _handle_notification(self, message: dict[str, Any]) -> None:
        if str(message.get("method") or "") != "textDocument/publishDiagnostics":
            return
        params = dict(message.get("params") or {})
        uri = str(params.get("uri") or "")
        self._diagnostics[uri] = {
            "status": "ok",
            "uri": uri,
            "diagnostics": list(params.get("diagnostics") or []),
            "diagnostics_state": "fresh",
            "timestamp": utc_now(),
        }


def _sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
