"""One admission, sleep, publication and recovery lifetime for Memory generations."""
from __future__ import annotations

from collections.abc import Callable

from pal.control.contracts import ControlDelivery
from pal.control.runtime_contracts import ControlRuntimePort
from pal.foundation.workers import run_worker
from pal.memory.repository import MemoryDurableRepository
from pal.memory.storage import MemoryStorage
from pal.memory.swap_state import can_sleep, can_wake
from pal.memory.swap_store import SwapJob, SwapStore
from pal.shared.maintenance import MaintenanceToken

BACKEND_SLEEP_NOTICE = (
    "切换记忆后端将触发 Pal 睡眠。睡眠期间暂不接收新对话，完成切换后会恢复。"
)


class GenerationMaintenance:
    def __init__(self, *, storage: MemoryStorage, source: MemoryDurableRepository,
                 host: ControlRuntimePort | None, bind: Callable[[], None],
                 bound: Callable[[], bool], component: str, announce: bool = False,
                 select_source: Callable[[], MemoryDurableRepository] | None = None,
                 retain_source: bool = False) -> None:
        self.storage = storage
        self.source = source
        self.host = host
        self.bind = bind
        self.bound = bound
        self.component = component
        self.announce = announce
        self.select_source = select_source
        self.retain_source = retain_source
        self.release_source: Callable[[], None] | None = None
        self.admission_token: MaintenanceToken | None = None
        self.candidate_generation = ""

    async def enter(self) -> None:
        host = self.host
        if host is not None:
            while self.admission_token is None:
                self.admission_token = await host.try_enter_memory_maintenance_async()
                if self.admission_token is None:
                    await host.wait_for_memory_maintenance_async()
        if self.select_source is not None:
            self.source = self.select_source()
        if self.storage.current() != self.source.generation_id:
            raise RuntimeError("Memory maintenance source is no longer current")
        if host is not None and self.retain_source:
            self.release_source = host.retain_modules((f"l3.{self.storage.active_provider_id()}",))
        if self.source.frozen and not self.source.owns_write_fence:
            await run_worker(self.storage.recover_abandoned_fence)
        route = host.last_user_route() if self.announce and host is not None else None
        delivered = False
        if route is not None and host is not None:
            delivered = await host.deliver(ControlDelivery(delivery_kind="reply", route=route,
                                                           text=BACKEND_SLEEP_NOTICE), require_provider=True, wait=True)
            if delivered:
                await host.flush(route)
        if not can_sleep(route is not None, delivered):
            raise RuntimeError("Could not deliver the backend-change sleep notice; migration remains unpublished")
        await run_worker(self.source.freeze)
        if host is not None:
            assert self.admission_token is not None
            host.begin_memory_sleep(self.admission_token, component=self.component)

    async def publish(self, store: SwapStore, job: SwapJob, candidate: MemoryDurableRepository) -> None:
        self.candidate_generation = job.candidate_generation
        await run_worker(lambda: store.publish(job, self.source, candidate))
        await run_worker(self.bind)

    async def close(self) -> None:
        try:
            await self._close()
        finally:
            if self.release_source is not None:
                self.release_source()
                self.release_source = None

    async def _close(self) -> None:
        published = bool(self.candidate_generation and self.storage.is_published(self.candidate_generation))
        try:
            if published and not self.bound():
                # A committed generation is authoritative even after a lost
                # acknowledgement. Mount it before reopening ordinary ingress.
                await run_worker(self.bind)
        finally:
            try:
                if self.source.owns_write_fence:
                    await run_worker(self.source.thaw)
            finally:
                if self.admission_token is not None and can_wake(published, self.bound() if published else False,
                                                                 not self.source.owns_write_fence):
                    assert self.host is not None
                    await self.host.leave_memory_maintenance_async(self.admission_token)
                    self.admission_token = None
        if published and not self.bound():
            raise RuntimeError("Published Memory generation is not mounted; Pal remains asleep")
