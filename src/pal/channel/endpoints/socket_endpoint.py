from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pal.channel.channel_endpoint_queue_base import ChannelEndpointQueueBase
from pal.channel.contracts import ChannelDeliveryError, EndpointConfig, ResponseHandle
from pal.foundation import AttachmentSpec
from pal.channel.endpoints.socket_protocol import (
    DEFAULT_SOCKET_FILENAME,
    pack_socket_message,
    read_socket_message,
)
from pal.shared import EventKind, LLMStreamEventKind
from pal.stream_events import NormalizedLLMStreamEvent

logger = logging.getLogger(__name__)


class SocketSessionClosed(ChannelDeliveryError):
    def __init__(self, message: str = "socket session is closed") -> None:
        super().__init__(message, permanent=True)


@dataclass
class _SocketSession:
    session_id: str
    writer: asyncio.StreamWriter
    outbound: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    request_ids: set[str] = field(default_factory=set)
    closed: bool = False
    writer_task: asyncio.Task[None] | None = None
def _stream_payload(response_handle: ResponseHandle, event: NormalizedLLMStreamEvent) -> dict[str, Any]:
    payload_type = str(event.event_kind)
    if event.event_kind == LLMStreamEventKind.DONE:
        payload_type = "llm_done"
    elif event.event_kind == LLMStreamEventKind.ERROR:
        payload_type = "llm_error"
    payload: dict[str, Any] = {
        "type": payload_type,
        "request_id": str(response_handle.reply_target.get("request_id") or ""),
    }
    if event.text:
        payload["text"] = event.text
    if event.reasoning_text:
        payload["reasoning_text"] = event.reasoning_text
    if event.tool_call is not None:
        payload["op_tool_call"] = {
            "name": event.tool_call.name,
            "args": dict(event.tool_call.args),
        }
    if event.finish_reason:
        payload["finish_reason"] = str(event.finish_reason)
    if event.error_text:
        payload["error_text"] = event.error_text
    return payload


@dataclass
class SocketChannelEndpoint(ChannelEndpointQueueBase):
    socket_path: Path | None = None
    server: asyncio.base_events.Server | None = None
    sessions: dict[str, _SocketSession] = field(default_factory=dict)
    _streamed_text_handles: set[int] = field(default_factory=set)
    _streamed_text_keys: set[tuple[str, str, str]] = field(default_factory=set)
    _stream_handle_ids_by_key: dict[tuple[str, str, str], int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.socket_path is None:
            self.socket_path = Path(self.endpoint.binding_key)

    async def start_async(self) -> None:
        assert self.socket_path is not None
        await self._prepare_socket_path()
        self.server = await asyncio.start_unix_server(self._handle_client, path=str(self.socket_path))
        logger.info("socket endpoint listening on %s", self.socket_path)

    async def stop_async(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        sessions = list(self.sessions.values())
        self.sessions.clear()
        for session in sessions:
            session.closed = True
            if session.writer_task is not None:
                session.writer_task.cancel()
            session.writer.close()
            try:
                await session.writer.wait_closed()
            except Exception:
                pass
        if self.socket_path is not None and self.socket_path.exists():
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.socket_path)

    async def _prepare_socket_path(self) -> None:
        assert self.socket_path is not None
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.socket_path.exists():
            return
        if await self._socket_in_use():
                raise RuntimeError(f"socket already in use: {self.socket_path}")
        os.unlink(self.socket_path)

    async def _socket_in_use(self) -> bool:
        assert self.socket_path is not None
        try:
            reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        except (FileNotFoundError, ConnectionRefusedError, ConnectionError, OSError):
            return False
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session_id = str(uuid4())
        session = _SocketSession(session_id=session_id, writer=writer)
        session.writer_task = asyncio.create_task(self._writer_loop(session))
        self.sessions[session_id] = session
        logger.info("socket session %s connected (%d active)", session_id, len(self.sessions))
        try:
            while True:
                incoming = await read_socket_message(reader)
                payload_type = str(incoming.get("type") or "user_message")
                text = str(incoming.get("text") or "").strip()
                if not text and payload_type != "slash_command":
                    continue
                request_id = str(incoming.get("request_id") or uuid4())
                session.request_ids.add(request_id)
                event_kind = self._resolve_event_kind(payload_type, text)
                self.accept_raw(
                    {
                        "text": text,
                        "request_id": request_id,
                        "session_id": session_id,
                        "attachments": list(incoming.get("attachments") or []),
                    },
                    event_kind=event_kind,
                    correlation_id=request_id,
                    reply_target={
                        "session_id": session_id,
                        "request_id": request_id,
                        "control_scope_key": f"socket:{self.endpoint.endpoint_id}:{session_id}",
                    },
                )
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ValueError):
            logger.debug("socket session %s disconnected", session_id)
        finally:
            session.closed = True
            self.sessions.pop(session_id, None)
            if session.writer_task is not None:
                session.writer_task.cancel()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _writer_loop(self, session: _SocketSession) -> None:
        try:
            while True:
                payload = await session.outbound.get()
                session.writer.write(pack_socket_message(payload))
                await session.writer.drain()
        except (asyncio.CancelledError, ConnectionError, OSError):
            session.closed = True
            logger.debug("socket writer loop ended for session %s", session.session_id)

    def _resolve_event_kind(self, payload_type: str, text: str) -> str:
        if payload_type == "slash_command":
            return EventKind.SLASH_COMMAND
        if text.startswith("/"):
            return EventKind.SLASH_COMMAND
        return EventKind.USER_MESSAGE

    def normalize_raw(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {"text": str(payload)}
        return {
            "text": str(payload.get("text") or ""),
            "request_id": str(payload.get("request_id") or ""),
            "session_id": str(payload.get("session_id") or ""),
            "attachments": list(payload.get("attachments") or []),
        }

    def send_reply(self, response_handle: ResponseHandle, text: str) -> None:
        session = self._require_session(response_handle)
        request_id = str(response_handle.reply_target.get("request_id") or "")
        session.outbound.put_nowait({"type": "text_delta", "request_id": request_id, "text": text})
        session.outbound.put_nowait({"type": "done", "request_id": request_id, "finish_reason": "stop"})

    def queue_stream_event(
        self,
        event: NormalizedLLMStreamEvent,
        *,
        response_handle: ResponseHandle | None = None,
    ) -> str:
        handle = response_handle or self.build_response_handle()
        self._mark_stream_event_queued(handle, event)
        return super().queue_stream_event(event, response_handle=handle)

    def send_stream_event(self, response_handle: ResponseHandle, event: NormalizedLLMStreamEvent) -> None:
        session = self._require_session(response_handle)
        super().send_stream_event(response_handle, event)
        self._mark_stream_event_queued(response_handle, event)
        session.outbound.put_nowait(_stream_payload(response_handle, event))

    def send_attachment(self, response_handle: ResponseHandle, attachment: AttachmentSpec) -> None:
        session = self._require_session(response_handle)
        request_id = str(response_handle.reply_target.get("request_id") or "")
        session.outbound.put_nowait(
            {
                "type": "attachment",
                "request_id": request_id,
                "path": attachment.path,
                "file_name": attachment.file_name,
                "caption": attachment.caption,
                "mime_type": attachment.mime_type,
            }
        )

    def abort_stream(self, response_handle: ResponseHandle, *, reason: str = "interrupted") -> None:
        super().abort_stream(response_handle, reason=reason)
        self._streamed_text_handles.discard(id(response_handle))
        self._clear_stream_tracking(response_handle)
        with contextlib.suppress(SocketSessionClosed):
            session = self._require_session(response_handle)
            request_id = str(response_handle.reply_target.get("request_id") or "")
            session.outbound.put_nowait({"type": "llm_error", "request_id": request_id, "error_text": str(reason)})
            session.outbound.put_nowait({"type": "llm_done", "request_id": request_id, "finish_reason": str(reason)})

    def prepare_final_reply(self, response_handle: ResponseHandle, text: str) -> str | None:
        stream_key = self._stream_key(response_handle)
        prior_handle_id = self._stream_handle_ids_by_key.pop(stream_key, None)
        if prior_handle_id is not None and prior_handle_id != id(response_handle):
            self._stream_sessions.pop(prior_handle_id, None)
            self._streamed_text_handles.discard(prior_handle_id)
        streamed_text = (
            id(response_handle) in self._streamed_text_handles
            or stream_key in self._streamed_text_keys
        )
        prepared = super().prepare_final_reply(response_handle, text)
        self._streamed_text_keys.discard(stream_key)
        if streamed_text:
            self._streamed_text_handles.discard(id(response_handle))
            return None
        return prepared

    def _clear_stream_tracking(self, response_handle: ResponseHandle) -> None:
        stream_key = self._stream_key(response_handle)
        prior_handle_id = self._stream_handle_ids_by_key.pop(stream_key, None)
        if prior_handle_id is not None:
            self._stream_sessions.pop(prior_handle_id, None)
            self._streamed_text_handles.discard(prior_handle_id)
        self._streamed_text_keys.discard(stream_key)

    @staticmethod
    def _stream_key(response_handle: ResponseHandle) -> tuple[str, str, str]:
        reply_target = response_handle.reply_target or {}
        return (
            str(response_handle.endpoint_id or ""),
            str(reply_target.get("session_id") or ""),
            str(reply_target.get("request_id") or ""),
        )

    def _mark_stream_event_queued(self, response_handle: ResponseHandle, event: NormalizedLLMStreamEvent) -> None:
        stream_key = self._stream_key(response_handle)
        self._stream_handle_ids_by_key[stream_key] = id(response_handle)
        if event.text:
            self._streamed_text_handles.add(id(response_handle))
            self._streamed_text_keys.add(stream_key)

    def inspect_health(self) -> dict[str, Any]:
        return {
            "healthy": self.server is not None,
            "socket_path": str(self.socket_path or ""),
            "session_count": len(self.sessions),
        }

    def inspect_auth_state(self) -> dict[str, Any]:
        return {
            "paired": True,
            "authorized": True,
            "socket_path": str(self.socket_path or ""),
        }

    def _require_session(self, response_handle: ResponseHandle) -> _SocketSession:
        session_id = str(response_handle.reply_target.get("session_id") or "")
        session = self.sessions.get(session_id)
        if session is None or session.closed:
            logger.warning("socket session %s not found or closed (active: %s)", session_id, list(self.sessions.keys()))
            raise SocketSessionClosed()
        return session
