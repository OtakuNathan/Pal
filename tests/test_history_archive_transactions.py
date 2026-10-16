"""Archive boundaries, crash recovery and legacy-layout transitions."""
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
from pal.memory.persistence.conversation import ConversationTextJournal
from pal.memory.persistence.archive import HistoryArchiveStore
from tests.test_resident_history import (
    NOW, app, begin, entry, frozen, read_messages, fixed_conversation_clock,
)


def prepare(instance, run_id="compact"):
    memory = instance.handle.memory_service
    for turn in memory.history.turns:
        if turn.state.value == "active":
            memory.settle_l1_turn(turn.turn_id)
    source = frozen(memory, run_id)
    reference = instance.history.prepare_history_reference(source)
    reference.stage()
    return source, reference


async def compact(instance, run_id="compact"):
    source, reference = prepare(instance, run_id)
    instance.handle.memory_service.compact_left(source.run_id, reference.decorate(entry()))
    assert await instance.history.checkpoint_async("compact", rebase=True)
    assert not instance.history.archive_error
    return reference


def append_user(memory, turn_id, message_id, text, timestamp=NOW):
    memory.begin_l1_turn(turn_id, user_message=LLMMessageIR(
        MessageRole.USER, (TextPartIR(text),), message_id=message_id, semantic_kind="user_request",
        metadata={"conversation_timestamp": timestamp, "conversation_origin": "user"}),
        metadata={"conversation_tracking": True})


def test_prepared_archive_is_private_and_abandoned_compact_can_retry(tmp_path):
    async def scenario():
        instance = app(tmp_path)
        memory = begin(instance, "original that must remain private until compact")
        source, reference = prepare(instance)
        paths = list(instance.history.archives.directory.glob("*.json"))
        assert len(paths) == 1
        assert b"original that must remain private" not in paths[0].read_bytes()
        assert not reference.paths[0].exists()
        assert not await instance.history.reconcile_async()  # Still owned by the live producer.
        assert paths[0].exists()
        memory.cancel_active_compaction(reason="test cancellation")
        assert await instance.history.reconcile_async()
        assert not paths[0].exists()
        assert memory.history.turns[0].messages[0].text.startswith("original")
        reference = await compact(instance, "retry")
        assert len(read_messages(reference.paths)) == 1
    asyncio.run(scenario())


def test_crash_before_work_commit_restores_originals_without_publishing_history(tmp_path):
    async def scenario():
        instance = app(tmp_path)
        memory = begin(instance)
        memory.settle_l1_turn("t")
        assert await instance.history.checkpoint_async("round")
        source, reference = prepare(instance)
        memory.compact_left(source.run_id, reference.decorate(entry()))
        # The RAM install was never written to the recovery journal.
        restored = app(tmp_path)
        await restored._restore_checkpoint_async()
        assert restored.last_checkpoint_status == "restored"
        assert restored.handle.memory_service.history.turns[0].messages[0].text == "原话\n第二行"
        assert not reference.paths[0].exists()
        assert restored.history.archives.pending_count() == 1
        assert await restored.history.reconcile_async()
        assert restored.history.archives.pending_count() == 0
        assert not reference.paths[0].exists()
        reference = await compact(restored, "after-recovery")
        assert [row["text"] for row in read_messages(reference.paths)] == ["原话\n第二行"]
    asyncio.run(scenario())


def test_multiday_partial_publication_retries_without_duplicating_first_day(tmp_path):
    async def scenario():
        instance = app(tmp_path)
        memory = begin(instance, "earlier compact")
        await compact(instance, "first")  # Finish the one-time layout migration.
        append_user(memory, "later", "later-1", "first day")
        memory.append_l1_user("later", LLMMessageIR(
            MessageRole.USER, (TextPartIR("second day"),), message_id="later-2", semantic_kind="user_interjection",
            metadata={"conversation_timestamp": "2000-01-02T01:00:00+00:00"}))
        source, reference = prepare(instance, "multiday")
        memory.compact_left(source.run_id, reference.decorate(entry()))
        original = instance.history.text_store._append
        def fail_second_day(path, rows):
            if path.name == "2000-01-02.jsonl":
                raise OSError("second day unavailable")
            original(path, rows)
        with patch.object(instance.history.text_store, "_append", side_effect=fail_second_day):
            assert await instance.history.checkpoint_async("compact", rebase=True)
        assert instance.history.archive_error
        first_day = reference.paths[0]
        before = first_day.read_bytes()
        assert "first day" in before.decode()
        assert not reference.paths[1].exists()
        restored = app(tmp_path)
        await restored._restore_checkpoint_async()
        assert first_day.read_bytes() == before
        assert not reference.paths[1].exists()
        assert await restored.history.reconcile_async()
        rows = read_messages(reference.paths)
        assert len(rows) == len({(r["conversation_id"], r["message_id"]) for r in rows}) == 3
        assert first_day.read_bytes() == before
        assert {r["text"] for r in rows} == {"earlier compact", "first day", "second day"}
    asyncio.run(scenario())


def test_failure_retiring_published_batch_does_not_duplicate_history(tmp_path):
    async def scenario():
        instance = app(tmp_path)
        begin(instance)
        source, reference = prepare(instance)
        instance.handle.memory_service.compact_left(source.run_id, reference.decorate(entry()))
        with patch.object(instance.history.archives, "_retire", side_effect=OSError("cleanup unavailable")):
            assert await instance.history.checkpoint_async("compact", rebase=True)
        before = reference.paths[0].read_bytes()
        assert instance.history.archives.pending_count() == 1
        restored = app(tmp_path)
        await restored._restore_checkpoint_async()
        assert restored.history.archives.pending_count() == 1
        assert await restored.history.reconcile_async()
        assert reference.paths[0].read_bytes() == before
        assert restored.history.archives.pending_count() == 0
    asyncio.run(scenario())


def test_unknown_batch_blocks_next_destructive_boundary(tmp_path):
    async def scenario():
        instance = app(tmp_path)
        memory = begin(instance)
        instance.history.archives.stage({
            "operation_id": "unknown", "kind": "reset", "source_incarnation": "another-conversation",
            "timezone": "UTC", "records": [{"type": "message", "role": "user", "text": "unresolved original",
                "conversation_id": "another-conversation", "message_id": "other", "timestamp": NOW}],
        })
        assert not await instance.history.reconcile_async()
        with pytest.raises(OSError):
            await instance.history.reset_async()
        assert memory.history.turns[0].messages[0].text == "原话\n第二行"
        assert instance.history.archives.pending_count() == 1
        assert not instance.history.text_store.directory.exists()
    asyncio.run(scenario())


def test_failed_reset_can_retry_without_duplicating_originals(tmp_path):
    async def scenario():
        instance = app(tmp_path)
        memory = begin(instance)
        assert await instance.history.checkpoint_async("round")
        with patch.object(instance.history.store, "save", side_effect=OSError("cannot save reset")):
            with pytest.raises(OSError):
                await instance.history.reset_async()
        assert instance.history.archives.pending_count() == 1
        assert memory.history.turns[0].messages[0].text == "原话\n第二行"
        assert not instance.history.text_store.directory.exists()
        await instance.history.reset_async()
        assert instance.history.archives.pending_count() == 0
        assert memory.history.turns == ()
        assert [row["text"] for row in read_messages((instance.history.text_store.path,))] == ["原话\n第二行"]
    asyncio.run(scenario())


def test_reset_journal_commit_survives_failed_rebase_and_defers_archive(tmp_path):
    async def scenario():
        instance = app(tmp_path)
        begin(instance)
        assert await instance.history.checkpoint_async("round")
        save = instance.history.store.save
        def fail_rebase(snapshot, *, rebase=False):
            if rebase:
                raise OSError("baseline unavailable")
            return save(snapshot, rebase=False)
        with patch.object(instance.history.store, "save", side_effect=fail_rebase):
            await instance.history.reset_async()
        assert instance.handle.memory_service.history.turns == ()
        assert instance.history.archives.pending_count() == 1
        restored = app(tmp_path)
        await restored._restore_checkpoint_async()
        assert restored.last_checkpoint_status == "restored"
        assert restored.handle.memory_service.history.turns == ()
        assert not restored.history.text_store.directory.exists()
        assert await restored.history.reconcile_async()
        assert [row["text"] for row in read_messages((restored.history.text_store.path,))] == ["原话\n第二行"]
    asyncio.run(scenario())


def test_cancelled_compact_staging_after_reset_is_reconciled_by_archived_identity(tmp_path):
    async def scenario():
        instance = app(tmp_path)
        memory = begin(instance)
        source, reference = prepare(instance)
        memory.cancel_active_compaction(reason="reset")
        await instance.history.reset_async()
        path = instance.history.text_store.path
        before = path.read_bytes()
        # A late producer may finish staging after reset has already published.
        reference.stage()
        assert await instance.history.reconcile_async()
        assert instance.history.archives.pending_count() == 0
        assert path.read_bytes() == before
    asyncio.run(scenario())


def write_legacy(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in [header, *rows]))


def test_first_compact_migrates_legacy_duplicates_and_keeps_tool_previews(tmp_path):
    async def scenario():
        instance = app(tmp_path)
        memory = begin(instance, "moved original")
        source, reference = prepare(instance)
        incarnation = memory.history_root.incarnation
        append_user(memory, "right", "r", "still working")
        directory = instance.history.text_store.directory
        old = {"type": "message", "role": "user", "message_id": "old", "turn_id": "past",
               "timestamp": NOW, "text": "old version", "redacted": False}
        write_legacy(directory / "compact_legacy.jsonl", {
            "type": "header", "schema_version": 1, "kind": "compact_archive", "conversation_id": incarnation,
            "archived_at": NOW}, [old, {**old, "message_id": "tool", "role": "tool", "tool_name": "run_shell",
                "text": "retained preview", "status": "ok"},
                {**old, "message_id": "undated", "timestamp": None, "text": "legacy without clock"}])
        write_legacy(directory / "current_legacy.jsonl", {
            "type": "header", "schema_version": 1, "kind": "current_dialogue", "conversation_id": incarnation,
            "recorded_at": NOW}, [{**old, "message_id": "u", "text": "moved original"},
                                   {**old, "message_id": "r", "text": "still working"}])
        write_legacy(directory / "2000-01-01.jsonl", {
            "type": "header", "schema_version": 2, "kind": "daily_dialogue", "date": "2000-01-01",
            "recorded_at": NOW}, [{**old, "conversation_id": incarnation},
                                   {**old, "conversation_id": incarnation, "text": "latest version"}])
        originals = {path.name: path.read_bytes() for path in directory.glob("*.jsonl")}
        # Ordinary saves must leave the old layout untouched until compact.
        assert await instance.history.checkpoint_async("round")
        assert {p.name: p.read_bytes() for p in directory.glob("*.jsonl")} == originals
        memory.compact_left(source.run_id, reference.decorate(entry()))
        assert await instance.history.checkpoint_async("compact", rebase=True)
        assert not instance.history.archive_error
        rows = read_messages(sorted(directory.glob("*.jsonl")))
        by_id = {row["message_id"]: row for row in rows}
        assert len(by_id) == len(rows) == 4
        assert "r" not in by_id
        assert by_id["old"]["text"] == "latest version"
        assert by_id["tool"]["text"] == "retained preview"
        assert by_id["tool"]["role"] == "tool"
        assert by_id["u"]["text"] == "moved original"
        assert by_id["u"]["archive_op_id"] == source.run_id
        assert (directory / "undated.jsonl").exists()
        backups = list((tmp_path / "data/history-backups").iterdir())
        assert len(backups) == 1
        assert {p.name: p.read_bytes() for p in backups[0].glob("*.jsonl")} == originals
        assert memory.history.turns[-1].messages[0].text == "still working"
    asyncio.run(scenario())


def test_migration_directory_switch_resumes_at_next_compact_boundary(tmp_path):
    import os
    async def scenario():
        instance = app(tmp_path)
        begin(instance)
        directory = instance.history.text_store.directory
        write_legacy(directory / "old.jsonl", {"type": "header", "schema_version": 1,
            "kind": "compact_archive", "conversation_id": "past", "archived_at": NOW},
            [{"type": "message", "role": "user", "conversation_id": "past", "turn_id": "past",
              "message_id": "past", "timestamp": NOW, "text": "preserved legacy"}])
        source, reference = prepare(instance)
        instance.handle.memory_service.compact_left(source.run_id, reference.decorate(entry()))
        replace = os.replace
        def fail_install(source, target):
            if Path(source).name.startswith(".history-migration-") and Path(target) == directory:
                raise OSError("directory install interrupted")
            return replace(source, target)
        with patch("pal.memory.persistence.migration.os.replace", side_effect=fail_install):
            assert await instance.history.checkpoint_async("compact", rebase=True)
        assert not directory.exists()
        assert instance.history.archive_error
        restored = app(tmp_path)
        await restored._restore_checkpoint_async()
        assert restored.last_checkpoint_status == "restored"
        assert not directory.exists()  # Startup intentionally does no history repair.
        assert await restored.history.reconcile_async()
        rows = read_messages(sorted(directory.glob("*.jsonl")))
        assert {row["text"] for row in rows} == {"preserved legacy", "原话\n第二行"}
        assert restored.history.archives.pending_count() == 0
    asyncio.run(scenario())


def test_corrupt_legacy_complete_line_is_preserved_and_blocks_publication(tmp_path):
    async def scenario():
        instance = app(tmp_path)
        begin(instance)
        directory = instance.history.text_store.directory
        directory.mkdir(parents=True)
        bad = directory / "bad.jsonl"
        bad.write_bytes(b'{"type":"header","schema_version":2,"kind":"daily_dialogue"}\ninvalid\n')
        before = bad.read_bytes()
        source, reference = prepare(instance)
        instance.handle.memory_service.compact_left(source.run_id, reference.decorate(entry()))
        assert await instance.history.checkpoint_async("compact", rebase=True)
        assert instance.history.archive_error
        assert not await instance.history.reconcile_async()
        assert bad.read_bytes() == before
        assert instance.history.archives.pending_count() == 1
    asyncio.run(scenario())


def test_pending_batch_preserves_timezone_and_exposes_precise_ranges(tmp_path):
    async def scenario():
        instance = app(tmp_path)
        memory = begin(instance)
        source, reference = prepare(instance)
        assert reference.header["ranges"][0]["first_message_id"] == "u"
        assert reference.header["ranges"][0]["last_message_id"] == "u"
        assert reference.header["ranges"][0]["start"] == NOW
        memory.compact_left(source.run_id, reference.decorate(entry()))
        with patch("pal.memory.persistence.archive.migrate_history", side_effect=OSError("unavailable")):
            assert await instance.history.checkpoint_async("compact", rebase=True)
        restored = app(tmp_path)
        await restored._restore_checkpoint_async()
        restored.history.text_store = ConversationTextJournal(tmp_path, "Pacific/Kiritimati")
        restored.history.archives = HistoryArchiveStore(tmp_path, restored.history.text_store)
        assert await restored.history.reconcile_async()
        assert reference.paths[0].name == "2000-01-01.jsonl"
        assert reference.paths[0].exists()
        assert not (reference.paths[0].parent / "2000-01-02.jsonl").exists()
        row = read_messages(reference.paths)[0]
        assert row["archive_op_id"] == source.run_id
        assert row["timestamp"] == NOW
    asyncio.run(scenario())
