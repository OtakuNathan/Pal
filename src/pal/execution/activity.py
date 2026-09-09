"""Best-effort execution observations, separate from tool results and L1."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import re
import time
from typing import Any, Callable
from uuid import uuid4

ARGS_BYTES = 8 * 1024
PATCH_BYTES = 64 * 1024
_SECRET_KEYS = frozenset({
    "password", "passwd", "secret", "token", "accesstoken", "refreshtoken",
    "apikey", "authorization", "proxyauthorization", "cookie", "setcookie",
    "otp", "verificationcode", "smscode", "验证码",
})


def bounded_text(text: str, limit: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    return encoded[:limit].decode("utf-8", errors="ignore"), len(encoded) > limit


def display_arguments(value: Any) -> tuple[str, bool]:
    clipped = False

    def clean(item: Any, depth: int = 0) -> Any:
        nonlocal clipped
        if depth > 12:
            clipped = True
            return "[omitted: nesting limit]"
        if isinstance(item, dict):
            result = {}
            for index, (key, child) in enumerate(item.items()):
                if index >= 100:
                    clipped = True
                    result["…"] = "[omitted]"
                    break
                normalized = re.sub(r"[\s_-]", "", str(key)).lower()
                result[str(key)] = "[redacted]" if normalized in _SECRET_KEYS else clean(child, depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            clipped |= len(item) > 100
            return [clean(child, depth + 1) for child in item[:100]]
        if isinstance(item, str):
            text, truncated = bounded_text(item, ARGS_BYTES)
            clipped |= truncated
            return text
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return "[non-JSON value]"

    text, truncated = bounded_text(json.dumps(clean(value), ensure_ascii=False, indent=2), ARGS_BYTES)
    return text, clipped or truncated


@dataclass
class _Capture:
    tool: str
    extra: dict[str, Any] = field(default_factory=dict)

    def observe(self, alias: str, raw: Any) -> None:
        if alias != self.tool:
            return
        payload = getattr(raw, "structured", None)
        if not isinstance(payload, dict):
            payload = getattr(raw, "output", None)
        if not isinstance(payload, dict):
            return
        if alias in {"edit_file", "write_file"} and isinstance(payload.get("patch"), str):
            patch, truncated = bounded_text(payload["patch"], PATCH_BYTES)
            self.extra.update(patch=patch, patch_truncated=truncated)
        if alias in {"run_shell", "shell_session", "shell_recover_output"}:
            status = payload.get("status")
            if status in {"running", "terminating"}:
                self.extra.update(background=True, session_id=payload.get("session_id"))


_CAPTURE: ContextVar[_Capture | None] = ContextVar("tool_activity_capture", default=None)


def capture_activity_output(alias: str, raw: Any) -> None:
    capture = _CAPTURE.get()
    if capture is not None:
        try:
            capture.observe(alias, raw)
        except Exception:
            pass  # Presentation must not affect normalization or delivery.


@dataclass
class ExecutionActivityDecorator:
    # Resolving a sink captures the originating route, never the latest channel.
    open_sink: Callable[[str], Callable[[dict[str, Any]], None] | None]

    async def invoke(self, call: Any, delegate: Callable[[], Any], *, turn_id: str | None) -> Any:
        if _CAPTURE.get() is not None:
            return await delegate()
        try:
            sink = self.open_sink(str(turn_id or ""))
        except Exception:
            sink = None
        if sink is None:
            return await delegate()
        name, arguments = call.name, call.args
        if name == "call_tool" and isinstance(arguments, dict) and isinstance(arguments.get("name"), str):
            name, arguments = arguments["name"], arguments.get("args", {})
        try:
            text, truncated = display_arguments(arguments)
            record = {"action": "call", "turn_id": str(turn_id or ""),
                      "call_id": call.call_id or uuid4().hex, "tool": name,
                      "arguments": text, "arguments_truncated": truncated, "status": "running"}
        except Exception:
            return await delegate()

        def emit(payload: dict[str, Any]) -> None:
            try:
                sink(payload)
            except Exception:
                pass

        emit(record)
        capture = _Capture(name)
        token = _CAPTURE.set(capture)
        started = time.monotonic()
        status = "failed"
        try:
            result = await delegate()
            status = "succeeded" if result.ok else "failed"
            if result.ok and capture.extra.get("background"):
                status = "background"
            return result
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            _CAPTURE.reset(token)
            emit({**record, **capture.extra, "status": status,
                  "elapsed_ms": round((time.monotonic() - started) * 1000)})
