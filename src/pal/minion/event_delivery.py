from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

from pal.foundation.sidecar import pack_sidecar_message, read_sidecar_message


@dataclass
class MinionEventDelivery:
    queue: list[dict[str, Any]] = field(default_factory=list)
    subscribers: list[asyncio.StreamWriter] = field(default_factory=list)

    async def handle_subscription(
        self,
        request: dict[str, Any],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        shutdown_event: asyncio.Event,
    ) -> None:
        request_id = str(request.get("id") or "")
        backlog = list(self.queue)
        self.queue.clear()
        self.subscribers.append(writer)
        try:
            writer.write(
                pack_sidecar_message(
                    {
                        "type": "response",
                        "id": request_id,
                        "ok": True,
                        "result": {"subscribed": True, "backlog_count": len(backlog)},
                    }
                )
            )
            for event in backlog:
                writer.write(pack_sidecar_message({"type": "event", "event": event}))
            await writer.drain()
            while not shutdown_event.is_set():
                try:
                    message = await read_sidecar_message(reader)
                except asyncio.IncompleteReadError:
                    return
                if str(message.get("method") or "") == "unsubscribe_events":
                    return
        finally:
            with contextlib.suppress(ValueError):
                self.subscribers.remove(writer)

    def queue_event(self, event: dict[str, Any]) -> None:
        if not self.subscribers:
            self.queue.append(event)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.queue.append(event)
            return
        loop.create_task(self._push_event_to_subscribers(dict(event)))

    async def _push_event_to_subscribers(self, event: dict[str, Any]) -> None:
        delivered = False
        for writer in list(self.subscribers):
            try:
                writer.write(pack_sidecar_message({"type": "event", "event": event}))
                await writer.drain()
                delivered = True
            except Exception:
                with contextlib.suppress(ValueError):
                    self.subscribers.remove(writer)
                with contextlib.suppress(Exception):
                    writer.close()
        if not delivered:
            self.queue.append(event)

    async def close(self) -> None:
        for writer in list(self.subscribers):
            with contextlib.suppress(Exception):
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=0.2)
        self.subscribers.clear()
