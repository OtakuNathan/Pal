"""Socket session and event demultiplexing for the interactive TTY."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
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
            self.writer.write(self.pack_message(payload))
            await self.writer.drain()
        except (ConnectionError, OSError) as exc:
            raise SocketDisconnected(f"socket write failed: {exc}") from exc

    async def stream_response(
        self,
        request_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield matching events through one terminal response event."""

        while True:
            payload = await self._read()
            if str(payload.get("request_id") or "") != request_id:
                continue
            yield payload
            payload_type = str(payload.get("type") or "")
            if payload_type == "llm_done":
                finish_reason = str(payload.get("finish_reason") or "")
                if finish_reason and finish_reason != "tool_calls":
                    return
            elif payload_type in {"done", "error"}:
                return

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
        try:
            self.writer.close()
        except Exception:
            pass
        try:
            await self.writer.wait_closed()
        except Exception:
            pass
