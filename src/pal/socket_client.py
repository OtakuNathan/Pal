from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from pal.channel.endpoints.socket_protocol import read_socket_message, pack_socket_message
from pal.channel.endpoints import DEFAULT_SOCKET_FILENAME


@dataclass
class SocketClientTranscript:
    request_id: str
    text_parts: list[str] = field(default_factory=list)
    reasoning_parts: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = ""


async def send_message(socket_path: Path, text: str) -> SocketClientTranscript:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    request_id = str(uuid4())
    transcript = SocketClientTranscript(request_id=request_id)
    try:
        writer.write(
            pack_socket_message(
                {
                    "type": "slash_command" if text.startswith("/") else "user_message",
                    "request_id": request_id,
                    "text": text,
                }
            )
        )
        await writer.drain()
        reasoning_open = False
        while True:
            payload = await read_socket_message(reader)
            if str(payload.get("request_id") or "") != request_id:
                continue
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
    finally:
        writer.close()
        await writer.wait_closed()


async def run_tty(socket_path: Path, *, input_fn: Callable[[str], str] | None = None) -> None:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    print(f"connected to {socket_path}", file=sys.stderr, flush=True)
    print("type /exit or Ctrl-D to quit", file=sys.stderr, flush=True)
    try:
        while True:
            try:
                if input_fn is None:
                    text = await asyncio.to_thread(input, "pal> ")
                else:
                    text = input_fn("pal> ")
            except EOFError:
                print(file=sys.stderr, flush=True)
                return
            text = text.strip()
            if not text:
                continue
            if text in {"/exit", "/quit"}:
                return
            request_id = str(uuid4())
            writer.write(
                pack_socket_message(
                    {
                        "type": "slash_command" if text.startswith("/") else "user_message",
                        "request_id": request_id,
                        "text": text,
                    }
                )
            )
            await writer.drain()
            await _read_response(reader, request_id)
    finally:
        writer.close()
        await writer.wait_closed()


async def _read_response(reader: asyncio.StreamReader, request_id: str) -> SocketClientTranscript:
    transcript = SocketClientTranscript(request_id=request_id)
    reasoning_open = False
    while True:
        payload = await read_socket_message(reader)
        if str(payload.get("request_id") or "") != request_id:
            continue
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


def default_socket_path(runtime_root: Path) -> Path:
    return runtime_root / DEFAULT_SOCKET_FILENAME
