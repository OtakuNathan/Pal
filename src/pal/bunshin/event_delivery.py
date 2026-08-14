from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Any

from pal.foundation.sidecar import pack_sidecar_message, read_sidecar_message


@dataclass
class BunshinEventDelivery:
    queue: list[dict[str, Any]] = field(default_factory=list)
    subscribers: list[asyncio.StreamWriter] = field(default_factory=list)
    load_backlog: Callable[[], list[dict[str, Any]]] | None = None

    async def handle_subscription(
        self,
        request: dict[str, Any],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        shutdown_event: asyncio.Event,
    ) -> None:
        request_id = str(request.get("id") or "")
        backlog = [*list(self.queue), *(
            list(self.load_backlog() or []) if self.load_backlog is not None else []
        )]
        deduplicated: dict[str, dict[str, Any]] = {}
        for event in backlog:
            key = str(event.get("delivery_id") or "") or repr(event)
            deduplicated[key] = event
        backlog = list(deduplicated.values())
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
            while True:
                read_task = asyncio.create_task(read_sidecar_message(reader))
                shutdown_task = asyncio.create_task(shutdown_event.wait())
                try:
                    done, pending = await asyncio.wait(
                        {read_task, shutdown_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    if shutdown_task in done and shutdown_event.is_set():
                        return
                    message = read_task.result()
                except (asyncio.CancelledError, asyncio.IncompleteReadError):
                    return
                finally:
                    for task in (read_task, shutdown_task):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(read_task, shutdown_task, return_exceptions=True)
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
