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
from pal.channel.contracts import ChannelDeliveryError, ChannelStreamUpdate, EndpointConfig, ResponseHandle
from pal.control.contracts import InteractionButtonSpec, InteractionMessageSpec
from pal.foundation import AttachmentSpec
from pal.channel.endpoints.socket_protocol import (
    DEFAULT_SOCKET_FILENAME,
    pack_socket_message,
    read_socket_message,
)
from pal.shared import EventKind, ChannelStreamUpdateKind

logger = logging.getLogger(__name__)


class SocketSessionClosed(ChannelDeliveryError):
    def __init__(
        self,
        message: str = "socket session is closed",
        *,
        permanent: bool = True,
    ) -> None:
        super().__init__(message, permanent=permanent)


@dataclass
class _SocketSession:
    session_id: str
    writer: asyncio.StreamWriter
    outbound: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    request_ids: set[str] = field(default_factory=set)
    closed: bool = False
    writer_task: asyncio.Task[None] | None = None
def _stream_payload(response_handle: ResponseHandle, update: ChannelStreamUpdate) -> dict[str, Any]:
    payload_type = str(update.kind)
    if update.kind == ChannelStreamUpdateKind.DONE:
        payload_type = "llm_done"
    elif update.kind == ChannelStreamUpdateKind.ERROR:
        payload_type = "llm_error"
    elif update.kind == ChannelStreamUpdateKind.PROGRESS:
        payload_type = "text_delta"
    payload: dict[str, Any] = {
        "type": payload_type,
        "request_id": str(response_handle.reply_target.get("request_id") or ""),
    }
    if update.text:
        if update.kind == ChannelStreamUpdateKind.DONE:
            # A full terminal projection lets reconnecting presentation
            # clients repair a missing transport delta without replaying or
            # persisting the endpoint's session lifecycle.
            payload["final_text"] = update.text
        else:
            payload["text"] = update.text
    if update.reasoning_text:
        payload["reasoning_text"] = update.reasoning_text
    if update.tool_call is not None:
        payload["op_tool_call"] = {
            "name": update.tool_call.name,
            "args": dict(update.tool_call.args),
        }
    if update.finish_reason:
        payload["finish_reason"] = str(update.finish_reason)
    if update.error_text:
        payload["error_text"] = update.error_text
    return payload


@dataclass
class SocketChannelEndpoint(ChannelEndpointQueueBase):
    socket_path: Path | None = None
    server: asyncio.base_events.Server | None = None
    sessions: dict[str, _SocketSession] = field(default_factory=dict)
    _streamed_text_handles: set[int] = field(default_factory=set)
    _streamed_text_keys: set[tuple[str, str, str]] = field(default_factory=set)
    _stream_handle_ids_by_key: dict[tuple[str, str, str], int] = field(default_factory=dict)
    _client_tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False, repr=False)
    _retired_session_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _session_replacements: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _allow_single_session_rebind: bool = field(default=False, init=False, repr=False)
    _owns_socket_path: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.socket_path is None:
            self.socket_path = Path(self.endpoint.binding_key)

    def supports_stream_delivery(self) -> bool:
        return True

    async def start_async(self) -> None:
        assert self.socket_path is not None
        await self._prepare_socket_path()
        self.server = await asyncio.start_unix_server(self._handle_client, path=str(self.socket_path))
        self._owns_socket_path = True
        logger.info("socket endpoint listening on %s", self.socket_path)

    async def stop_async(self) -> None:
        owns_socket_path = self._owns_socket_path
        self._owns_socket_path = False
        server = self.server
        self.server = None
        if server is not None:
            server.close()
            close_clients = getattr(server, "close_clients", None)
            if callable(close_clients):
                close_clients()
        sessions = list(self.sessions.values())
        if len(sessions) == 1:
            retired_id = sessions[0].session_id
            self._retired_session_ids.add(retired_id)
            self._allow_single_session_rebind = True
        self.sessions.clear()
        for session in sessions:
            session.closed = True
            if session.writer_task is not None:
                session.writer_task.cancel()
            session.writer.close()
        client_tasks = [
            task
            for task in self._client_tasks
            if task is not asyncio.current_task() and not task.done()
        ]
        for task in client_tasks:
            task.cancel()
        if client_tasks:
            done, pending = await asyncio.wait(client_tasks, timeout=1.0)
            self._client_tasks.difference_update(done)
            for task in pending:
                task.cancel()
        if sessions:
            await asyncio.gather(
                *(self._close_writer_async(session.writer) for session in sessions),
                return_exceptions=True,
            )
        if server is not None:
            try:
                await asyncio.wait_for(asyncio.shield(server.wait_closed()), timeout=0.5)
            except TimeoutError:
                abort_clients = getattr(server, "abort_clients", None)
                if callable(abort_clients):
                    abort_clients()
        if owns_socket_path and self.socket_path is not None and self.socket_path.exists():
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
        client_task = asyncio.current_task()
        if client_task is not None:
            self._client_tasks.add(client_task)
        session_id = str(uuid4())
        session = _SocketSession(session_id=session_id, writer=writer)
        session.writer_task = asyncio.create_task(self._writer_loop(session))
        self.sessions[session_id] = session
        self._adopt_single_replacement_session(session_id)
        if self.on_ready is not None:
            self.on_ready()
        logger.info("socket session %s connected (%d active)", session_id, len(self.sessions))
        try:
            while True:
                incoming = await read_socket_message(reader)
                payload_type = str(incoming.get("type") or "user_message")
                if payload_type == "interaction_result":
                    self._accept_interaction_result(session, incoming)
                    continue
                text = str(incoming.get("text") or "").strip()
                attachments = list(incoming.get("attachments") or [])
                if not text and not attachments and payload_type != "slash_command":
                    continue
                request_id = str(incoming.get("request_id") or uuid4())
                session.request_ids.add(request_id)
                event_kind = self._resolve_event_kind(payload_type, text)
                self.accept_raw(
                    {
                        "text": text,
                        "request_id": request_id,
                        "session_id": session_id,
                        "attachments": attachments,
                    },
                    event_kind=event_kind,
                    correlation_id=request_id,
                    reply_target={
                        **self._session_reply_target(session, request_id),
                    },
                )
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ValueError):
            logger.debug("socket session %s disconnected", session_id)
        finally:
            session.closed = True
            self.sessions.pop(session_id, None)
            if session.writer_task is not None:
                session.writer_task.cancel()
            await self._close_writer_async(writer)
            if client_task is not None:
                self._client_tasks.discard(client_task)

    async def _close_writer_async(self, writer: asyncio.StreamWriter) -> None:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=0.25)
        except (TimeoutError, ConnectionError, OSError):
            transport = getattr(writer, "transport", None)
            if transport is not None:
                transport.abort()

    def _adopt_single_replacement_session(self, session_id: str) -> None:
        if not self._allow_single_session_rebind or len(self.sessions) != 1:
            return
        for retired_id in self._retired_session_ids:
            self._session_replacements[retired_id] = session_id
        self._allow_single_session_rebind = False

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
        if not bool(response_handle.reply_target.get("_pal_turn_continues")):
            session.outbound.put_nowait({"type": "done", "request_id": request_id, "finish_reason": "stop"})

    def open_or_update_interaction(
        self,
        response_handle: ResponseHandle,
        *,
        spec: InteractionMessageSpec,
        allow_update: bool,
    ) -> None:
        self.prune_interactive_messages()
        self.remember_interaction_message(
            spec,
            {"reply_target": dict(response_handle.reply_target)},
        )
        self._send_interaction(
            response_handle,
            kind="interactive_update" if allow_update else "interactive_open",
            spec=spec,
        )

    def apply_interaction_status(
        self,
        response_handle: ResponseHandle,
        *,
        kind: str,
        spec: InteractionMessageSpec,
        payload: dict[str, Any],
    ) -> None:
        if kind == "interactive_expire":
            self.forget_interaction_message(spec.interaction_id)
            self._send_interaction(
                response_handle,
                kind=kind,
                spec=spec,
            )
            return
        super().apply_interaction_status(
            response_handle,
            kind=kind,
            spec=spec,
            payload=payload,
        )

    def resolve_interaction(
        self,
        response_handle: ResponseHandle,
        *,
        spec: InteractionMessageSpec,
    ) -> None:
        self.forget_interaction_message(spec.interaction_id)
        self._send_interaction(
            response_handle,
            kind="interactive_resolve",
            spec=spec,
        )

    def _send_interaction(
        self,
        response_handle: ResponseHandle,
        *,
        kind: str,
        spec: InteractionMessageSpec,
    ) -> None:
        session = self._require_session(response_handle)
        request_id = str(response_handle.reply_target.get("request_id") or "")
        rows: list[list[dict[str, str]]] = []
        button_index = 0
        for row in spec.buttons:
            projected_row: list[dict[str, str]] = []
            for button in row:
                if not isinstance(button, InteractionButtonSpec):
                    continue
                token = self.interaction_button_token(button_index)
                button_index += 1
                label = str(button.label or "").strip()
                if not label:
                    continue
                projected_row.append(
                    {
                        "label": label,
                        "token": token,
                    }
                )
            if projected_row:
                rows.append(projected_row)
        session.outbound.put_nowait(
            {
                "type": kind,
                "request_id": request_id,
                "interaction": {
                    "interaction_id": spec.interaction_id,
                    "interaction_kind": spec.interaction_kind,
                    "text": spec.text,
                    "buttons": rows,
                    "expires_at": spec.expires_at,
                },
            }
        )
        session.outbound.put_nowait(
            {
                "type": "done",
                "request_id": request_id,
                "finish_reason": "stop",
            }
        )

    def _accept_interaction_result(
        self,
        session: _SocketSession,
        incoming: dict[str, Any],
    ) -> None:
        request_id = str(incoming.get("request_id") or uuid4())
        session.request_ids.add(request_id)
        interaction_id = str(incoming.get("interaction_id") or "").strip()
        button_token = str(incoming.get("button_token") or "").strip()
        metadata = self._interactive_messages.get(interaction_id)
        reply_target = metadata.get("reply_target") if isinstance(metadata, dict) else None
        owner_session_id = (
            str(reply_target.get("session_id") or "")
            if isinstance(reply_target, dict)
            else ""
        )
        current_owner_session_id = self._session_replacements.get(
            owner_session_id,
            owner_session_id,
        )
        result = None
        if current_owner_session_id == session.session_id:
            result = self.interaction_result_from_token(
                interaction_id,
                button_token,
            )
        if result is None:
            session.outbound.put_nowait(
                {
                    "type": "error",
                    "request_id": request_id,
                    "error_text": "interaction is unavailable or the selection is invalid",
                    "finish_reason": "error",
                }
            )
            return
        self.emit_interaction_result(
            result,
            correlation_id=request_id,
            reply_target=self._session_reply_target(session, request_id),
        )

    def _session_reply_target(
        self,
        session: _SocketSession,
        request_id: str,
    ) -> dict[str, str]:
        return {
            "session_id": session.session_id,
            "request_id": str(request_id),
            "control_scope_key": (
                f"socket:{self.endpoint.endpoint_id}:{session.session_id}"
            ),
        }

    def queue_stream_update(
        self,
        update: ChannelStreamUpdate,
        *,
        response_handle: ResponseHandle | None = None,
    ) -> str:
        handle = response_handle or self.build_response_handle()
        self._mark_stream_update_queued(handle, update)
        return super().queue_stream_update(update, response_handle=handle)

    def send_stream_update(self, response_handle: ResponseHandle, update: ChannelStreamUpdate) -> None:
        session = self._require_session(response_handle)
        if update.kind == ChannelStreamUpdateKind.PROGRESS:
            session.outbound.put_nowait(_stream_payload(response_handle, update))
            return
        if update.kind == ChannelStreamUpdateKind.MESSAGE:
            # Generic socket clients receive the required text fallback. A
            # specialized socket-backed endpoint may override this method and
            # project the tagged message into its own wire protocol.
            session.outbound.put_nowait(
                _stream_payload(
                    response_handle,
                    ChannelStreamUpdate(
                        kind=ChannelStreamUpdateKind.PROGRESS,
                        text=(update.message.text if update.message is not None else update.text),
                    ),
                )
            )
            return
        stream_session = self._stream_sessions.get(id(response_handle)) or {}
        prior_text = str(stream_session.get("text") or "")
        if (
            update.kind == ChannelStreamUpdateKind.DONE
            and update.text
            and update.text.startswith(prior_text)
        ):
            missing = update.text[len(prior_text):]
            if missing:
                session.outbound.put_nowait(
                    _stream_payload(
                        response_handle,
                        ChannelStreamUpdate(
                            kind=ChannelStreamUpdateKind.TEXT_DELTA,
                            text=missing,
                        ),
                    )
                )
        super().send_stream_update(response_handle, update)
        self._mark_stream_update_queued(response_handle, update)
        session.outbound.put_nowait(_stream_payload(response_handle, update))
        if update.kind == ChannelStreamUpdateKind.TEXT_DELTA:
            self.mark_stream_text_delivered(response_handle, update)
        if update.kind in {
            ChannelStreamUpdateKind.DONE,
            ChannelStreamUpdateKind.ERROR,
        }:
            self._clear_stream_tracking(response_handle)

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

    def _mark_stream_update_queued(self, response_handle: ResponseHandle, update: ChannelStreamUpdate) -> None:
        stream_key = self._stream_key(response_handle)
        self._stream_handle_ids_by_key[stream_key] = id(response_handle)
        if update.kind == ChannelStreamUpdateKind.TEXT_DELTA and update.text:
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
            if self._allow_single_session_rebind and len(self.sessions) == 1:
                self._adopt_single_replacement_session(next(iter(self.sessions)))
            replacement_id = self._session_replacements.get(session_id)
            session = self.sessions.get(replacement_id or "")
        if session is None or session.closed:
            logger.debug("socket session %s not found or closed (active: %s)", session_id, list(self.sessions.keys()))
            raise SocketSessionClosed(
                permanent=not (
                    self._allow_single_session_rebind
                    and session_id in self._retired_session_ids
                )
            )
        return session
