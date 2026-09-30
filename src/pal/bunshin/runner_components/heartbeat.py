from __future__ import annotations
import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any
from pal.shared import BunshinInvocationPack
from pal.bunshin.runner_components.reporter import Reporter


@dataclass
class Heartbeat:
    reporter: Reporter
    pack: BunshinInvocationPack

    async def await_with_progress_heartbeat(self, awaitable, *, phase: str, **payload: Any):
        interval = self.heartbeat_interval_seconds()
        if interval <= 0:
            return await awaitable
        task = asyncio.create_task(awaitable)
        heartbeat_count = 0
        try:
            while True:
                done, _pending = await asyncio.wait({task}, timeout=interval)
                if task in done:
                    return await task
                heartbeat_count += 1
                await self.reporter.emit_progress(phase, heartbeat_count=heartbeat_count, **payload)
        except BaseException:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            raise

    def heartbeat_interval_seconds(self) -> float:
        metadata = self.pack.metadata if isinstance(self.pack.metadata, dict) else {}
        raw = metadata.get("heartbeat_interval_seconds")
        if raw is None:
            return 30.0
        try:
            interval = float(raw)
        except (TypeError, ValueError):
            return 30.0
        if interval <= 0:
            return 0.0
        return max(0.01, interval)
