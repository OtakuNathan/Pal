"""Real generation migration, concurrent mutations, publication faults and recovery."""
import asyncio
import sqlite3
import subprocess
import sys
import threading
from itertools import product
from pathlib import Path
from dataclasses import replace

import pytest

from pal.memory.contracts import L3CommitRequest, L3CorrectRequest, L3DeleteRequest, MemoryQuery
from pal.memory.embedding import HashingEmbedder
from pal.memory.service import MemoryService
from pal.memory.storage import MemoryStorage
from pal.memory.swap_store import SwapStore
from pal.memory.swap_formal import render_swap_relation
from pal.memory.swap_state import SwapEvent, SwapFacts, SwapPhase, can_publish, reduce_swap
from pal.memory.capabilities import register_with_core as register_memory
from pal.plugins.l3.capabilities import register_with_core as register_l3
from pal.plugins.l3.sqlite_vec import SQLiteVecL3Plugin
from tests.core_factory import build_core


@pytest.fixture
def swap_runtime(tmp_path):
    storage = MemoryStorage(tmp_path)
    storage.create_initial()
    core = build_core()
    memory = MemoryService()
    handle = register_memory(core.context, memory)
    source = SQLiteVecL3Plugin(memory, repository=storage.open(), embedder=HashingEmbedder())
    target = SQLiteVecL3Plugin(memory, repository=storage.open(), provider_id="replacement", embedder=HashingEmbedder(dimension=16))
    register_l3(core.context, source)
    register_l3(core.context, target)
    def command(operation, **params):
        return core.context.configuration_operations[f"memory.{operation}"][1](params)
    yield core, memory, storage, source, target, command
    asyncio.run(handle.quiesce_async())
    for provider in (source, target):
        provider.repository.close()
    core.close()
    core.context.execution_runtime.shutdown()


def fact(summary, **kwargs):
    return L3CommitRequest(kind="fact", title=summary, summary=summary, search_text=summary, **kwargs)


async def settled(command, job_id):
    async with asyncio.timeout(15):
        while True:
            state = await command("swap-status", job_id=job_id)
            if not state["state"]["running"]:
                return state
            await asyncio.sleep(0.005)


def test_online_snapshot_replays_updates_deletes_and_receipts_before_swap(swap_runtime, monkeypatch):
    core, memory, storage, source, target, command = swap_runtime
    first_request = fact("original", mutation_id="original-request")
    first = source.commit(first_request)
    removed = source.commit(fact("remove this"))
    old = storage.current()
    storage.pin("old-reader")
    reader = storage.open(old, read_only=True)
    preparing, release = threading.Event(), threading.Event()
    original = target.prepare_generation
    first_pass = True
    def prepare(*args, **kwargs):
        nonlocal first_pass
        original(*args, **kwargs)
        if first_pass:
            first_pass = False
            preparing.set()
            assert release.wait(10)
    monkeypatch.setattr(target, "prepare_generation", prepare)
    async def scenario():
        started = await command("swap", name="replacement")
        job_id = started["state"]["job_id"]
        try:
            assert await asyncio.to_thread(preparing.wait, 5)
            storage.create_initial()
            with storage.connection() as catalog:
                assert catalog.execute("SELECT count(*) FROM generation_bindings b JOIN generations g "
                    "ON g.generation_id=b.generation_id WHERE g.published_at IS NULL").fetchone()[0] == 0
            added = source.commit(fact("added during copying", mutation_id="during-copy"))
            updated = source.correct(L3CorrectRequest(first.document_id, summary="updated during copying", mutation_id="update-during-copy"))
            assert source.delete(L3DeleteRequest(removed.document_id)).status == "ok"
        finally:
            release.set()
        result = await settled(command, job_id)
        assert result["status"] == "ok", result
        assert result["state"]["phase"] == "complete"
        assert storage.current() != old
        assert memory.l3_selector.active_provider_id == "replacement"
        assert storage.active_provider_id() == "replacement"
        assert target.repository.get_document(added.document_id)["summary"] == added.hit["summary"]
        assert target.repository.get_document(updated.document_id)["summary"] == "updated during copying"
        assert target.repository.get_document(first.document_id) is None
        assert target.recall(MemoryQuery(mem_ref=removed.document_id)).metadata["lookup_status"] == "deleted"
        assert target.commit(first_request).document_id == first.document_id
        assert storage.pinned("old-reader") == old
        assert source.commit(fact("late old writer")).status == "unavailable"
        with sqlite3.connect(storage.path(old)) as stale:
            with pytest.raises(sqlite3.IntegrityError, match="cannot accept writes"):
                stale.execute("UPDATE memory_facts SET summary='late' WHERE fact_id=?", (added.document_id.split(":", 1)[1],))
        post = target.commit(fact("after cutover"))
        assert reader.get_document(post.document_id) is None
        assert not core.memory_maintenance_busy()
    try:
        asyncio.run(scenario())
    finally:
        release.set()
        reader.close()


def test_precommit_failure_keeps_source_writable_and_resume_recaptures(swap_runtime, monkeypatch):
    core, memory, storage, source, target, command = swap_runtime
    old = storage.current()
    source.commit(fact("before failure"))
    publish = SwapStore.publish
    def fail(*args):
        raise OSError("publication interrupted")
    monkeypatch.setattr(SwapStore, "publish", fail)
    async def scenario():
        job = (await command("swap", name="replacement"))["state"]["job_id"]
        failed = await settled(command, job)
        assert failed["status"] == "error"
        assert storage.current() == old
        assert memory.l3_selector.active_provider_id == source.provider_id
        assert not source.repository.frozen
        assert not core.memory_maintenance_busy()
        added = source.commit(fact("after failure"))
        assert added.status == "ok"
        monkeypatch.setattr(SwapStore, "publish", publish)
        await command("swap-resume", job_id=job)
        completed = await settled(command, job)
        assert completed["state"]["phase"] == "complete", completed
        assert target.repository.get_document(added.document_id) is not None
    asyncio.run(scenario())


def test_lost_commit_acknowledgement_never_republishes_source(swap_runtime, monkeypatch):
    _, memory, storage, source, target, command = swap_runtime
    saved = source.commit(fact("survive acknowledgement loss"))
    publish = SwapStore.publish
    calls = []
    def lose_ack(*args):
        calls.append("commit")
        publish(*args)
        raise OSError("commit acknowledgement lost")
    monkeypatch.setattr(SwapStore, "publish", lose_ack)
    async def scenario():
        job = (await command("swap", name="replacement"))["state"]["job_id"]
        failed = await settled(command, job)
        committed = storage.current()
        assert failed["state"]["phase"] == "committed"
        assert failed["activation"] == "live"
        assert storage.active_provider_id() == "replacement"
        assert memory.l3_selector.active_provider_id == "replacement"
        written = target.commit(fact("written after a lost acknowledgement"))
        assert written.status == "ok"
        monkeypatch.setattr(SwapStore, "publish", publish)
        await command("swap-resume", job_id=job)
        result = await settled(command, job)
        assert result["state"]["phase"] == "complete", result
        assert storage.current() == committed
        assert calls == ["commit"]
        assert memory.l3_selector.active_provider_id == "replacement"
        assert target.recall(MemoryQuery(mem_ref=saved.document_id)).hits
        assert target.repository.get_document(written.document_id) is not None
    asyncio.run(scenario())


def test_index_failure_keeps_published_data_readable_and_resumes_in_place(swap_runtime, monkeypatch):
    _, memory, storage, source, target, command = swap_runtime
    saved = source.commit(fact("lexical recovery survives unavailable embeddings"))
    embed = target.embedding_provider.embed_documents
    def unavailable(_texts):
        raise OSError("embedding transport unavailable")
    monkeypatch.setattr(target.embedding_provider, "embed_documents", unavailable)
    async def scenario():
        job = (await command("swap", name="replacement"))["state"]["job_id"]
        failed = await settled(command, job)
        generation = storage.current()
        assert failed["state"]["phase"] == "indexing"
        assert memory.l3_selector.active_provider_id == "replacement"
        assert target.recall(MemoryQuery(mem_ref=saved.document_id)).hits
        assert target.recall(MemoryQuery(queries=["lexical recovery"])).hits
        later = target.commit(fact("written while indexing is pending"))
        assert later.status == "ok"
        monkeypatch.setattr(target.embedding_provider, "embed_documents", embed)
        await command("swap-resume", job_id=job)
        completed = await settled(command, job)
        assert completed["state"]["phase"] == "complete", completed
        assert storage.current() == generation
        assert target.repository.get_document(later.document_id) is not None
    asyncio.run(scenario())


def test_abandoned_database_fence_recovers_only_the_current_generation(tmp_path):
    storage = MemoryStorage(tmp_path)
    storage.create_initial()
    script = """import os,sys
from pal.memory.storage import MemoryStorage
storage=MemoryStorage(sys.argv[1])
repo=storage.open()
repo.freeze()
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", script, str(tmp_path)], check=True, timeout=10)
    with sqlite3.connect(storage.path(storage.current())) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO memory_facts(fact_id) VALUES ('blocked')")
    recovered = MemoryStorage(tmp_path)
    recovered.recover_abandoned_fence()
    repo = recovered.open()
    try:
        provider = SQLiteVecL3Plugin(MemoryService(), repository=repo, embedder=HashingEmbedder())
        assert provider.commit(fact("after process recovery")).status == "ok"
    finally:
        repo.close()


def test_generated_relation_uses_every_production_guard_input():
    assert Path("spec/memory/MemorySwapReducer.tla").read_text() == render_swap_relation()
    for values in product((False, True), repeat=5):
        facts = SwapFacts(*values)
        for phase in SwapPhase:
            if can_publish(phase, facts):
                assert phase is SwapPhase.READY
                assert facts.source_current and facts.caught_up and facts.fenced and not facts.published
            for event in SwapEvent:
                try:
                    target = reduce_swap(phase, event, facts)
                except ValueError:
                    continue
                if target is SwapPhase.READY:
                    assert facts.source_current and facts.caught_up and facts.fenced
                if target is SwapPhase.COMMITTED:
                    assert facts.published


@pytest.mark.parametrize('cancel_caller', [False, True])
def test_cancel_joins_snapshot_worker_before_releasing_module_owner(swap_runtime, monkeypatch, cancel_caller):
    core, memory, storage, source, target, command = swap_runtime
    old = storage.current()
    source.commit(fact("before cancellation"))
    copying, release = threading.Event(), threading.Event()
    capture = SwapStore.capture
    def capture_then_wait(*args):
        candidate = capture(*args)
        copying.set()
        assert release.wait(10)
        return candidate
    monkeypatch.setattr(SwapStore, "capture", capture_then_wait)
    async def scenario():
        job = (await command("swap", name="replacement"))["state"]["job_id"]
        assert await asyncio.to_thread(copying.wait, 5)
        with pytest.raises(RuntimeError, match="background work"):
            await command("detach", name="replacement")
        assert target.mounted
        cancellation = asyncio.create_task(command("swap-cancel", job_id=job))
        try:
            await asyncio.sleep(0.03)
            if cancel_caller:
                cancellation.cancel()
                await asyncio.sleep(0)
                cancellation.cancel()
                await asyncio.sleep(0.02)
            assert not cancellation.done()
            with pytest.raises(RuntimeError, match="background work"):
                core.context.unregister_module(core.context.module_registry.require(source.module_id))
        finally:
            release.set()
        if cancel_caller:
            with pytest.raises(asyncio.CancelledError):
                await cancellation
            paused = await command('swap-status', job_id=job)
        else:
            paused = await cancellation
        assert not paused["state"]["running"]
        assert paused['state']['paused']
        assert storage.current() == old
        assert not core.memory_maintenance_busy()
        monkeypatch.setattr(SwapStore, "capture", capture)
        # Backend registration/startup scans must not undo an explicit cancel.
        await core.run_until_idle_async()
        scanned = await command("swap-status", job_id=job)
        assert not scanned["state"]["running"], scanned
        assert storage.current() == old
        await command("swap-resume", job_id=job)
        result = await settled(command, job)
        assert result["state"]["phase"] == "complete", result
    try:
        asyncio.run(scenario())
    finally:
        release.set()


@pytest.mark.parametrize("pause", [False, True])
def test_restart_rebinds_committed_identity_and_resumes_index_job(swap_runtime, monkeypatch, pause):
    core, memory, storage, source, target, command = swap_runtime
    old = storage.current()
    storage.pin("pinned-workflow")
    saved = source.commit(fact("persisted migration across owner restart"))
    def unavailable(_texts):
        raise OSError("embedding transport unavailable")
    monkeypatch.setattr(target.embedding_provider, "embed_documents", unavailable)
    async def first_owner():
        job_id = (await command("swap", name="replacement"))["state"]["job_id"]
        failed = await settled(command, job_id)
        assert failed["state"]["phase"] == "indexing"
        if pause:
            await command("swap-cancel", job_id=job_id)
        await core.context.module_registry.require("memory").quiesce_async()
        return job_id
    job = asyncio.run(first_owner())
    source.repository.close()
    target.repository.close()
    new_core = build_core()
    new_memory = MemoryService()
    handle = register_memory(new_core.context, new_memory)
    current = SQLiteVecL3Plugin(new_memory, repository=storage.open(), provider_id="replacement", embedder=HashingEmbedder(dimension=16))
    register_l3(new_core.context, current)
    async def recovered_owner():
        try:
            assert new_memory.l3_selector.active_provider_id == "replacement"
            assert current.recall(MemoryQuery(mem_ref=saved.document_id)).hits
            # The actual module-owned startup timer discovers the durable job.
            await new_core.run_until_idle_async()
            operation = new_core.context.configuration_operations["memory.swap-status"][1]
            if pause:
                paused = await operation({"job_id": job})
                assert paused["state"]["paused"] and not paused["state"]["running"]
                assert paused["state"]["phase"] == "indexing"
                resume = new_core.context.configuration_operations["memory.swap-resume"][1]
                await resume({"job_id": job})
            async with asyncio.timeout(15):
                while True:
                    result = await operation({"job_id": job})
                    if result["state"]["phase"] == "complete":
                        break
                    await asyncio.sleep(0.005)
            assert storage.pinned("pinned-workflow") == old
            assert storage.provider_for_generation(old) == "sqlite_vec_l3"
            assert storage.provider_for_generation(storage.current()) == "replacement"
            pinned_memory = MemoryService()
            pinned = SQLiteVecL3Plugin(pinned_memory, repository=storage.open(old, read_only=True),
                                     read_only=True, embedder=HashingEmbedder())
            pinned_memory.register_l3_provider(pinned)
            assert pinned_memory.l3_selector.active_provider_id == "sqlite_vec_l3"
            assert pinned.recall(MemoryQuery(mem_ref=saved.document_id)).hits
            pinned.repository.close()
        finally:
            await handle.quiesce_async()
    try:
        asyncio.run(recovered_owner())
    finally:
        current.repository.close()
        new_core.close()
        new_core.context.execution_runtime.shutdown()


def test_old_index_job_cannot_restore_a_later_generation(swap_runtime, monkeypatch):
    _, memory, storage, source, target, command = swap_runtime
    source.commit(fact("source for consecutive swaps"))
    assert source.refresh_indexes()["refreshed"] == 1
    assert source.recall(MemoryQuery(queries=["source for consecutive swaps"])).hits
    embed = target.embedding_provider.embed_documents
    def unavailable(_texts):
        raise OSError("temporary embedding failure")
    monkeypatch.setattr(target.embedding_provider, "embed_documents", unavailable)
    async def scenario():
        old_job = (await command("swap", name="replacement"))["state"]["job_id"]
        failed_index = await settled(command, old_job)
        assert failed_index["state"]["phase"] == "indexing", failed_index["state"]["error"]
        assert source.recall(MemoryQuery(queries=["source for consecutive swaps"])).hits
        first_generation = storage.current()
        monkeypatch.setattr(target.embedding_provider, "embed_documents", embed)
        new_job = (await command("swap", name="replacement"))["state"]["job_id"]
        result = await settled(command, new_job)
        assert result["state"]["phase"] == "complete", result["state"]["error"]
        current = storage.current()
        assert current != first_generation
        added = target.commit(fact("written in the latest generation"))
        await command("swap-resume", job_id=old_job)
        retired = await settled(command, old_job)
        assert retired["state"]["phase"] == "superseded", retired
        assert storage.current() == current
        assert target.repository.generation_id == current
        assert target.repository.get_document(added.document_id) is not None
    asyncio.run(scenario())


def test_superseded_job_does_not_require_its_unloaded_backend(swap_runtime, monkeypatch):
    core, memory, storage, source, target, command = swap_runtime
    source.commit(fact("survives migration back to the original backend"))
    def unavailable(_texts):
        raise OSError("temporary embedding failure")
    monkeypatch.setattr(target.embedding_provider, "embed_documents", unavailable)
    async def scenario():
        old_job = (await command("swap", name="replacement"))["state"]["job_id"]
        assert (await settled(command, old_job))["state"]["phase"] == "indexing"
        new_job = (await command("swap", name=source.provider_id))["state"]["job_id"]
        complete = await settled(command, new_job)
        assert complete["state"]["phase"] == "complete", complete
        current = storage.current()
        core.context.unregister_module(core.context.module_registry.require(target.module_id))
        await command("swap-resume", job_id=old_job)
        old = await settled(command, old_job)
        assert old["state"]["phase"] == "superseded", old
        assert storage.current() == current
        assert memory.l3_selector.active_provider_id == source.provider_id
    asyncio.run(scenario())


@pytest.mark.parametrize("lost_acknowledgement", [False, True])
def test_dreaming_rebinds_ready_vectors_after_a_failed_swap_index(swap_runtime, monkeypatch, lost_acknowledgement):
    from pal.control.runtime_contracts import CORE_CONTROL
    from pal.memory.dreaming.contracts import DreamingConfig
    from pal.memory.dreaming.service import DreamingService
    from pal.memory.embedding_space import read_space
    from tests.test_memory_dreaming import ReviewingLLM

    core, memory, storage, source, _, command = swap_runtime
    request = fact("Nathan prefers explicit public APIs", topics=["API"])
    source.commit(request)
    source.commit(replace(request, title="API design"))
    async def scenario():
        def unavailable(_texts):
            raise OSError("temporary embedding outage")
        with monkeypatch.context() as failing:
            failing.setattr(source.embedding_provider, "embed_documents", unavailable)
            job = (await command("swap", name=source.provider_id))["state"]["job_id"]
            assert (await settled(command, job))["state"]["phase"] == "indexing"
        previous = storage.current()
        service = DreamingService(storage=storage, provider=source, memory=memory,
            llm=ReviewingLLM(), config=DreamingConfig(), host=core.context.require_port(CORE_CONTROL))
        publish = SwapStore.publish
        if lost_acknowledgement:
            def lose_ack(*args, **kwargs):
                publish(*args, **kwargs)
                raise OSError("lost Dreaming publication acknowledgement")
            monkeypatch.setattr(SwapStore, "publish", lose_ack)
        result = await service.run()
        assert result["status"] == "completed" and result["report"]["outcome"] == "merged", result
        current = storage.current()
        assert current != previous
        assert source.repository.generation_id == current
        assert not storage.indexing_pending(current)
        assert not core.memory_maintenance_busy()
        assert read_space(source.repository.database.connection()).profile == source.migration_identity
        recalled = source.recall(MemoryQuery(queries=[request.summary]))
        assert "vector" in recalled.metadata["retrieval_mode"], recalled.metadata
        await command("swap-resume", job_id=job)
        assert (await settled(command, job))["state"]["phase"] == "superseded"
        assert storage.current() == current
        assert "vector" in source.recall(MemoryQuery(queries=[request.summary])).metadata["retrieval_mode"]
    asyncio.run(scenario())
