"""Delivery and lease ownership for durable workflow effects."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from pal.bunshin.contracts import DeferredEffectError
from pal.bunshin.repository import BunshinRepository


class WorkflowEffectHandler(Protocol):
    @property
    def repository(self) -> BunshinRepository: ...

    async def execute_effect(self, effect: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def settle_failed_effect(self, effect: Mapping[str, Any], exc: Exception, worker_id: str) -> str: ...


@dataclass
class WorkflowEffectDispatcher:
    handlers: WorkflowEffectHandler
    worker_id: str = "bunshin-outbox"
    effect_lease_seconds: int = 120
    _background_tasks: set[asyncio.Task[str]] = field(default_factory=set, init=False, repr=False)

    @property
    def repository(self) -> BunshinRepository:
        return self.handlers.repository

    @property
    def active_background_count(self) -> int:
        return sum(not task.done() for task in self._background_tasks)

    async def process_once(self, *, limit: int = 10) -> dict[str, Any]:
        claimed = self.repository.outbox_claims.claim_outbox(
            self.worker_id,
            limit=max(1, int(limit)),
            lease_seconds=self.effect_lease_seconds,
        )
        if not claimed:
            return {"status": "idle", "claimed": 0, "completed": 0, "failed": 0}
        results = await asyncio.gather(*(self._process_effect(effect) for effect in claimed))
        return {
            "status": "processed",
            "claimed": len(claimed),
            "completed": sum(item == "completed" for item in results),
            "failed": sum(item != "completed" for item in results),
        }

    def start_available(self, *, max_concurrency: int) -> int:
        self._background_tasks = {task for task in self._background_tasks if not task.done()}
        available = max(0, int(max_concurrency) - len(self._background_tasks))
        if available == 0:
            return 0
        claimed = self.repository.outbox_claims.claim_outbox(
            self.worker_id,
            limit=available,
            lease_seconds=self.effect_lease_seconds,
        )
        for effect in claimed:
            task = asyncio.create_task(
                self._process_effect(effect),
                name=f"bunshin-effect-{str(effect['effect_id'])[-12:]}",
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        return len(claimed)

    async def stop_background(self) -> None:
        tasks = tuple(self._background_tasks)
        self._background_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _process_effect(self, effect: Mapping[str, Any]) -> str:
        effect_id = str(effect["effect_id"])
        heartbeat = asyncio.create_task(self._heartbeat(effect_id))
        try:
            result = await self.handlers.execute_effect(effect)
            result_ref = dict(result.get("result_artifact_ref") or {}) if isinstance(result, Mapping) else {}
            provider_request_id = str(result.get("provider_request_id") or "") if isinstance(result, Mapping) else ""
            self.repository.outbox_results.complete_outbox_effect(
                effect_id,
                worker_id=self.worker_id,
                provider_request_id=provider_request_id,
                result_artifact_ref=result_ref,
            )
            return "completed"
        except DeferredEffectError as exc:
            self.repository.outbox_results.defer_outbox_effect(
                effect_id,
                worker_id=self.worker_id,
                reason=str(exc) or "effect deferred at a durable restart safe point",
                attempt_was_incremented=bool(effect.get("claim_incremented_attempt", True)),
            )
            return "deferred"
        except Exception as exc:
            return self.handlers.settle_failed_effect(effect, exc, self.worker_id)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    async def _heartbeat(self, effect_id: str) -> None:
        interval = max(1.0, self.effect_lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            self.repository.outbox_claims.renew_outbox_claim(
                effect_id,
                worker_id=self.worker_id,
                lease_seconds=self.effect_lease_seconds,
            )
