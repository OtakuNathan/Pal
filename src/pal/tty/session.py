"""Socket session and event demultiplexing for the interactive TTY."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pal.channel.endpoints.socket_protocol import pack_socket_message


class SocketSessionError(RuntimeError):
    """Base class for client-side socket session failures."""


class SocketDisconnected(SocketSessionError):
    """The peer closed the socket or a read failed."""


class SocketProtocolError(SocketSessionError):
    """A framed socket message could not be decoded."""


@dataclass
class SocketSession:
    """One open socket connection with request-scoped event demultiplexing."""

    reader: Any
    writer: Any
    read_message: Callable[[Any], Awaitable[dict[str, Any]]]
    request_id_factory: Callable[[], str]
    pack_message: Callable[[dict[str, Any]], bytes] = pack_socket_message
    _response_queues: dict[str, asyncio.Queue[dict[str, Any]]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _notifications: asyncio.Queue[dict[str, Any]] = field(
        default_factory=asyncio.Queue,
        init=False,
        repr=False,
    )
    _reader_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _delivery_ack_advertised: bool = field(default=False, init=False, repr=False)

    async def send(self, text: str) -> str:
        request_id = str(self.request_id_factory())
        message_type = "slash_command" if text.startswith("/") else "user_message"
        await self._write(
            {
                "type": message_type,
                "request_id": request_id,
                "text": text,
            }
        )
        return request_id

    async def send_interaction_result(
        self,
        *,
        interaction_id: str,
        button_token: str,
    ) -> str:
        request_id = str(self.request_id_factory())
        await self._write(
            {
                "type": "interaction_result",
                "request_id": request_id,
                "interaction_id": str(interaction_id),
                "button_token": str(button_token),
            }
        )
        return request_id

    async def _write(self, payload: dict[str, Any]) -> None:
        try:
            self._advertise_delivery_ack()
            self.writer.write(self.pack_message(payload))
            await self.writer.drain()
        except (ConnectionError, OSError) as exc:
            raise SocketDisconnected(f"socket write failed: {exc}") from exc

    async def stream_response(
        self,
        request_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield matching events through one terminal response event."""

        self._ensure_reader()
        queue = self._response_queues.setdefault(request_id, asyncio.Queue())
        while True:
            payload = await queue.get()
            self._raise_reader_error(payload)
            yield payload
            payload_type = str(payload.get("type") or "")
            if payload_type == "llm_done":
                finish_reason = str(payload.get("finish_reason") or "")
                if finish_reason and finish_reason != "tool_calls":
                    return
            elif payload_type in {"done", "error"}:
                return

    async def stream_notifications(self) -> AsyncIterator[dict[str, Any]]:
        """Yield unsolicited Task delivery frames without consuming turn replies."""

        self._ensure_reader()
        while True:
            payload = await self._notifications.get()
            self._raise_reader_error(payload)
            yield payload

    def _ensure_reader(self) -> None:
        self._advertise_delivery_ack()
        if self._reader_task is None or self._reader_task.done():
            self._reader_task = asyncio.create_task(
                self._reader_loop(),
                name="pal-tty-socket-reader",
            )

    async def _reader_loop(self) -> None:
        try:
            while True:
                payload = await self._read()
                delivery_id = str(payload.get("_pal_delivery_id") or "")
                request_id = str(payload.get("request_id") or "")
                if request_id.startswith("task-notification:"):
                    await self._notifications.put(payload)
                else:
                    queue = self._response_queues.setdefault(
                        request_id,
                        asyncio.Queue(),
                    )
                    await queue.put(payload)
                if delivery_id:
                    await self._write_delivery_ack(delivery_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = {"__reader_error__": exc}
            await self._notifications.put(failure)
            for queue in self._response_queues.values():
                await queue.put(failure)

    async def _write_delivery_ack(self, delivery_id: str) -> None:
        try:
            self.writer.write(
                self.pack_message(
                    {"type": "delivery_ack", "delivery_id": delivery_id}
                )
            )
            await self.writer.drain()
        except (ConnectionError, OSError) as exc:
            raise SocketDisconnected(f"socket acknowledgement failed: {exc}") from exc

    def _advertise_delivery_ack(self) -> None:
        if self._delivery_ack_advertised:
            return
        self.writer.write(
            self.pack_message(
                {"type": "session_ready", "delivery_ack_v1": True}
            )
        )
        self._delivery_ack_advertised = True

    @staticmethod
    def _raise_reader_error(payload: dict[str, Any]) -> None:
        error = payload.get("__reader_error__")
        if isinstance(error, Exception):
            raise error

    async def _read(self) -> dict[str, Any]:
        try:
            return await self.read_message(self.reader)
        except asyncio.IncompleteReadError as exc:
            raise SocketDisconnected("socket closed by peer") from exc
        except (ConnectionError, OSError) as exc:
            raise SocketDisconnected(f"socket read failed: {exc}") from exc
        except ValueError as exc:
            raise SocketProtocolError(f"malformed socket frame: {exc}") from exc

    async def aclose(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None
        try:
            self.writer.close()
        except Exception:
            pass
        try:
            await self.writer.wait_closed()
        except Exception:
            pass
