"""Public client facade for a running Pal Unix socket."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pal.channel.endpoints import DEFAULT_SOCKET_FILENAME
from pal.channel.endpoints.socket_protocol import read_socket_message
from pal.tty.session import SocketSession
from pal.tty.ui import TtyRepl


@dataclass
class SocketClientTranscript:
    request_id: str
    text_parts: list[str] = field(default_factory=list)
    reasoning_parts: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = ""


async def send_message(socket_path: Path, text: str) -> SocketClientTranscript:
    """Send one message and preserve the non-interactive plain-text contract."""

    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    session = SocketSession(
        reader,
        writer,
        read_message=read_socket_message,
        request_id_factory=uuid4,
    )
    request_id = await session.send(text)
    transcript = SocketClientTranscript(request_id=request_id)
    reasoning_open = False
    try:
        async for payload in session.stream_response(request_id):
            payload_type = str(payload.get("type") or "")
            if payload_type == "reasoning_delta":
                chunk = str(payload.get("reasoning_text") or payload.get("text") or "")
                if chunk:
                    transcript.reasoning_parts.append(chunk)
                    if not reasoning_open:
                        print("[thinking]", flush=True)
                        reasoning_open = True
                    print(chunk, end="", flush=True)
                continue
            if payload_type == "text_delta":
                chunk = str(payload.get("text") or "")
                if chunk:
                    transcript.text_parts.append(chunk)
                    print(chunk, end="", flush=True)
                continue
            if payload_type == "op_tool_call":
                tool_call = dict(payload.get("op_tool_call") or {})
                transcript.tool_calls.append(tool_call)
                tool_name = str(tool_call.get("name") or "tool")
                print(f"\n[tool] {tool_name}", file=sys.stderr, flush=True)
                continue
            if payload_type == "llm_done":
                finish_reason = str(payload.get("finish_reason") or "")
                if finish_reason and finish_reason != "tool_calls":
                    transcript.finish_reason = finish_reason
                    print(flush=True)
                    return transcript
                continue
            if payload_type == "llm_error":
                chunk = str(payload.get("error_text") or payload.get("text") or "unknown error")
                if chunk:
                    print(f"\n[llm-error] {chunk}", file=sys.stderr, flush=True)
                continue
            if payload_type == "error":
                chunk = str(payload.get("error_text") or payload.get("text") or "unknown error")
                if chunk:
                    print(f"\n[error] {chunk}", file=sys.stderr, flush=True)
                transcript.finish_reason = str(payload.get("finish_reason") or "error")
                return transcript
            if payload_type == "done":
                transcript.finish_reason = str(payload.get("finish_reason") or "stop")
                print(flush=True)
                return transcript
        return transcript
    finally:
        await session.aclose()


async def run_tty(
    socket_path: Path,
    *,
    input_fn: Callable[[str], str] | None = None,
) -> None:
    """Run an async Prompt Toolkit and Rich interactive socket session."""

    repl = TtyRepl(
        socket_path,
        open_unix_connection=asyncio.open_unix_connection,
        read_message=read_socket_message,
        request_id_factory=uuid4,
    )
    await repl.run(input_fn=input_fn)


def default_socket_path(runtime_root: Path) -> Path:
    return runtime_root / DEFAULT_SOCKET_FILENAME
