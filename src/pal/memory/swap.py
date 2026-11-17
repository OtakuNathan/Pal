"""Memory-owned background migration, catalog cutover and deferred indexing."""
from __future__ import annotations

import asyncio
import fcntl
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pal.foundation.diagnostics import exception_report
from pal.memory.backend_contracts import SqliteGenerationPort
from pal.memory.repository import MemoryDurableRepository
from pal.memory.swap_state import PRECOMMIT_PHASES, SwapEvent, SwapLaunch, SwapPhase, can_wake, decide_launch
from pal.memory.generation_maintenance import BACKEND_SLEEP_NOTICE, GenerationMaintenance
from pal.memory.swap_store import SwapJob, SwapStore
from pal.foundation.workers import join_owned_task, run_worker

if TYPE_CHECKING:
    from pal.control.runtime_contracts import ControlRuntimePort
    from pal.memory.service import MemoryService
    from pal.shared.ports import ServiceRef


class MemorySwapManager:
    def __init__(self, service: MemoryService, host: ServiceRef[ControlRuntimePort]) -> None:
        self.service = service
        self.host = host
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.closing = False

    def _backend(self, name: str) -> SqliteGenerationPort:
        provider = self.service.providers.require(name)
        port = provider.generation_port
        if not provider.mounted or port is None:
            raise ValueError(f"Provider {name} does not expose managed generation migration")
        return port

    def _store(self, job_id: str) -> SwapStore:
        seen: set[Path] = set()
        for provider in self.service.providers.plugins.values():
            port = provider.generation_port
            if port is None or port.storage.catalog_path in seen:
                continue
            seen.add(port.storage.catalog_path)
            store = SwapStore(port.storage)
            try:
                store.load(job_id)
            except ValueError:
                continue
            return store
        raise ValueError(f"Unknown memory swap job: {job_id}")

    def start(self, target_name: str) -> dict[str, Any]:
        assert self.service.l3_selector is not None
        source_name = self.service.l3_selector.active_provider_id
        source, target = self._backend(source_name), self._backend(target_name)
        if source.storage.catalog_path != target.storage.catalog_path:
            raise ValueError("Backend migration requires the shared Memory generation catalog")
        store = SwapStore(source.storage)
        job = store.create(source_name, target_name, target.migration_identity,
                           source_generation=source.repository.generation_id)
        self._launch(store, job)
        return {**self.status(job.job_id), "message": BACKEND_SLEEP_NOTICE}

    def resume(self, job_id: str) -> dict[str, Any]:
        store = self._store(job_id)
        self._launch(store, store.load(job_id), resume=True)
        return self.status(job_id)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        store = self._store(job_id)
        return await join_owned_task(asyncio.create_task(self._cancel(store, job_id), name=f"memory-pause:{job_id}"))

    async def _cancel(self, store: SwapStore, job_id: str) -> dict[str, Any]:
        task = self.tasks.get(job_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        job = store.load(job_id)
        if job.phase not in (SwapPhase.COMPLETE, SwapPhase.SUPERSEDED):
            job.paused = True
            job.error = "Migration paused; resume this job to continue"
            store.save(job)
        return self.status(job_id)

    def _launch(self, store: SwapStore, job: SwapJob, *, resume: bool = False) -> None:
        if self.closing:
            raise RuntimeError("Memory migration owner is stopping")
        if job.job_id in self.tasks:
            return
        facts = store.facts(job)
        decision = decide_launch(job.phase, facts, paused=job.paused and not resume)
        if decision is SwapLaunch.IGNORE:
            return
        if decision is SwapLaunch.SUPERSEDE:
            job.paused = False
            store.advance(job, SwapEvent.SUPERSEDE)
            return
        target = self._backend(job.target_provider)
        if target.migration_identity != job.profile:
            raise RuntimeError("Migration target embedding identity changed; restore its declared configuration before resuming")
        names: tuple[str, ...] = (f"l3.{job.target_provider}",)
        if job.phase in PRECOMMIT_PHASES and not store.facts(job).published:
            names += (f"l3.{job.source_provider}",)
        release = self.host.current.retain_modules(names)
        try:
            if job.paused:
                job.paused = False
                job.error = ""
                store.save(job)
            task = asyncio.create_task(self._run(store, job.job_id), name=f"memory:{job.job_id}")
        except BaseException:
            release()
            raise
        self.tasks[job.job_id] = task
        def complete(done: asyncio.Task[None]) -> None:
            release()
            if self.tasks.get(job.job_id) is done:
                self.tasks.pop(job.job_id)
            if not done.cancelled():
                error = done.exception()
                if error is not None:
                    self.host.current.record_diagnostic("memory_swap_owner_failed", {
                        "job_id": job.job_id, "error": exception_report(error)})
        task.add_done_callback(complete)

    def status(self, job_id: str) -> dict[str, Any]:
        store = self._store(job_id)
        job = store.load(job_id)
        head = store.storage.current()
        return {"status": "error" if job.error else "ok", "activation": "live" if head == job.candidate_generation else "pending",
                "state": {**asdict(job), "running": job_id in self.tasks,
                          "active_generation": head, "active_provider_id": store.storage.active_provider_id()}}

    async def recover_pending(self) -> None:
        if self.closing:
            return
        stores: dict[Path, SwapStore] = {}
        for provider in tuple(self.service.providers.plugins.values()):
            port = provider.generation_port
            if port is not None:
                stores[port.storage.catalog_path] = SwapStore(port.storage)
        for store in stores.values():
            for job in store.pending():
                try:
                    if job.dreaming_run_id and job.phase in PRECOMMIT_PHASES and not store.facts(job).published:
                        with (store.storage.path(job.source_generation).parent / "writer.lock").open("a+b") as owner:
                            try:
                                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                continue
                            store.advance(job, SwapEvent.ABANDON)
                        continue
                    self._launch(store, job)
                except (KeyError, RuntimeError, ValueError) as exc:
                    job.error = exception_report(exc)
                    store.save(job)

    async def _run(self, store: SwapStore, job_id: str) -> None:
        job = store.load(job_id)
        try:
            if job.phase in PRECOMMIT_PHASES:
                if store.facts(job).published:
                    store.advance(job, SwapEvent.RECOVER)
                else:
                    await self._migrate(store, job)
            await self._index(store, store.load(job_id))
        except BaseException as exc:
            # Read the committed record again: a lost acknowledgement cannot
            # turn an already published generation back into a failed candidate.
            latest = store.load(job_id)
            facts = store.facts(latest)
            if not facts.published and (latest.phase in (SwapPhase.COMMITTED, SwapPhase.INDEXING)
                                       or (latest.phase in PRECOMMIT_PHASES and not facts.source_current)):
                store.advance(latest, SwapEvent.SUPERSEDE)
                return
            latest.error = ("Migration interrupted; resume this job" if isinstance(exc, asyncio.CancelledError)
                            else exception_report(exc))
            store.save(latest)
            if isinstance(exc, asyncio.CancelledError):
                raise

    async def _migrate(self, store: SwapStore, job: SwapJob) -> None:
        source_port, target_port = self._backend(job.source_provider), self._backend(job.target_provider)
        source = source_port.repository
        if source.generation_id != job.source_generation:
            raise RuntimeError("Migration source binding changed")
        acquired: list[MemoryDurableRepository] = []
        maintenance = GenerationMaintenance(storage=store.storage, source=source, host=self.host.current,
            bind=lambda: self.service.adopt_l3_generation(store.storage.active_provider_id(), store.storage.current()),
            bound=lambda: self.service.is_l3_generation_bound(store.storage.active_provider_id(), store.storage.current()),
            component=job.component, announce=True)
        try:
            if job.phase is not SwapPhase.PREPARING:
                store.advance(job, SwapEvent.RETRY)
            store.advance(job, SwapEvent.SNAPSHOT)
            def capture() -> None:
                acquired.append(store.capture(job))
            await run_worker(capture)
            candidate = acquired[0]
            await run_worker(lambda: target_port.prepare_generation(candidate))
            store.advance(job, SwapEvent.COPIED)
            documents = await run_worker(lambda: store.catch_up(job, candidate))
            await run_worker(lambda: target_port.prepare_generation(candidate, tuple(documents)))
            await maintenance.enter()
            documents = await run_worker(lambda: store.catch_up(job, candidate))
            await run_worker(lambda: target_port.prepare_generation(candidate, tuple(documents)))
            await run_worker(lambda: store.verify(job, source, candidate))
            await maintenance.publish(store, job, candidate)
            await run_worker(lambda: source_port.retire_generation(job.source_generation))
        finally:
            async def finish() -> None:
                try:
                    await maintenance.close()
                finally:
                    if acquired:
                        await run_worker(acquired[0].close)
            await join_owned_task(asyncio.create_task(finish()))

    async def _index(self, store: SwapStore, job: SwapJob) -> None:
        if job.phase in (SwapPhase.COMPLETE, SwapPhase.SUPERSEDED):
            return
        target = self._backend(job.target_provider)
        if not store.facts(job).published:
            store.advance(job, SwapEvent.SUPERSEDE)
            return
        await run_worker(lambda: self.service.adopt_l3_generation(job.target_provider, job.candidate_generation))
        snapshot = self.host.current.snapshot()
        if (snapshot.memory_maintenance and snapshot.memory_maintenance_component == job.component
                and snapshot.memory_maintenance_token is not None
                and can_wake(True, self.service.is_l3_generation_bound(job.target_provider, store.storage.current()),
                             not store.storage.is_frozen(job.source_generation))):
            await self.host.current.leave_memory_maintenance_async(snapshot.memory_maintenance_token)
        if job.phase is SwapPhase.COMMITTED:
            store.advance(job, SwapEvent.INDEX)
        while store.facts(job).published:
            result = await run_worker(lambda: target.index_generation(job.candidate_generation, limit=32))
            if result.get("last_embedding_error") or result.get("read_only"):
                raise RuntimeError(str(result.get("last_embedding_error") or "Memory index writer is unavailable"))
            if not result["pending"]:
                job.indexes_ready = True
                store.advance(job, SwapEvent.INDEXED, indexed=True)
                target.finish_indexing(job.candidate_generation)
                await run_worker(store.storage.collect)
                return
            if not result.get("refreshed"):
                raise RuntimeError("Memory indexing made no progress; resume after repairing the embedding backend")
            await asyncio.sleep(0)
        store.advance(job, SwapEvent.SUPERSEDE)

    async def close(self) -> None:
        self.closing = True
        tasks = tuple(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
