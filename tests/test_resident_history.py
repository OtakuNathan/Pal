"""Resident continuity and historical evidence, with isolated runtime roots."""
import asyncio
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from pal.memory.persistence.conversation import ConversationHistoryReference, history_records
from pal.memory.persistence.journal import ResidentJournalStore
from pal.memory.persistence.owner import ResidentHistoryPromptProvider
from pal.core.compaction import CompactionEngine, CompactionSnapshot, CompactionClockKind
from pal.core.pal_compaction import PalCompactionPolicy
from pal.execution.result_snapshots import render_snapshot_hint
from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR, ReasoningPartIR, ReplayEnvelope, WireShape
from pal.memory.contracts import L2Entry
from pal.shared import PromptAssemblyContext
from pal.shared.tool_protocol import ToolCallIR, ToolResultIR
from tests.test_resident_checkpoint import _build_app

NOW = "2000-01-01T12:00:00+00:00"


@pytest.fixture(autouse=True)
def fixed_conversation_clock(monkeypatch):
    from pal.memory.persistence import owner

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(NOW).astimezone(tz)

    monkeypatch.setattr(owner, "datetime", FixedDatetime)
    monkeypatch.setattr("pal.memory.service.utc_now", lambda: NOW)


def read_messages(paths):
    return [record for path in paths for line in path.read_text().splitlines()
            if (record := json.loads(line))["type"] == "message"]


def app(root):
    instance = _build_app(root)
    instance.handle.core.context.execution_runtime.configure_runtime_root(root)
    instance.handle.core.context.execution_runtime.bind_result_history(instance.handle.memory_service)
    return instance


def begin(instance, text="原话\n第二行"):
    memory = instance.handle.memory_service
    memory.begin_l1_turn("t", user_message=LLMMessageIR(
        role=MessageRole.USER, parts=(TextPartIR(text),), message_id="u", semantic_kind="user_request",
        metadata={"conversation_origin": "user", "conversation_timestamp": NOW}),
        metadata={"conversation_tracking": True, "conversation_input_kind": "user"})
    return memory


def answer(memory, text="回复", message_id="a"):
    memory.stream_l1_assistant("t", LLMMessageIR(
        role=MessageRole.ASSISTANT, message_id=message_id,
        parts=(ReasoningPartIR("PRIVATE REASONING"), TextPartIR(text)),
        replay=ReplayEnvelope(WireShape.OPENAI_COMPLETION, "e", "m",
                             {"message": {"reasoning_content": "PRIVATE REASONING", "signature": "opaque"}})))


def entry(text="continuity"):
    return L2Entry(entry_id="summary", kind="summary", scope="session", title="Summary", summary=text, rendered=text)


def frozen(memory, run_id="compact"):
    memory.history_root.promote()
    return memory.begin_left_compaction(run_id, reason="manual")


def test_round_tail_restores_without_another_request_and_keeps_native_reasoning(tmp_path):
    async def scenario():
        source = app(tmp_path)
        memory = begin(source)
        assert await source.history.checkpoint_async("submission")
        prepared = memory.history_root.prepare_submission(("u",))
        assert memory.history_root.commit_submission(prepared, ("u",))
        answer(memory)
        source.history.record_reply("t", "回复")
        assert await source.history.checkpoint_async("round")
        assert memory.history_root.right_messages()[0].message_id == "a"
        disk_text = source.history.store.path.read_text()
        assert "PRIVATE REASONING" not in disk_text and "回复" not in disk_text
        target = app(tmp_path)
        await target._restore_checkpoint_async()
        assert target.last_checkpoint_status == "restored", target.last_checkpoint_error
        recovered = target.handle.memory_service.history.turns[0]
        assert recovered.state.value == "interrupted"
        assert recovered.messages[-1].reasoning_text == "PRIVATE REASONING"
        assert recovered.messages[-1].replay.payload["message"]["signature"] == "opaque"
        assert target.handle.memory_service.history_root.right_messages()[-1].text == "回复"
        again = app(tmp_path)
        await again._restore_checkpoint_async()
        assert again.last_checkpoint_status == "restored"
    asyncio.run(scenario())


def test_incremental_round_does_not_rewrite_old_text_or_base(tmp_path):
    async def scenario():
        instance = app(tmp_path)
        memory = begin(instance, "old text " * 12000)
        assert await instance.history.checkpoint_async("round")
        old_base = instance.history.store.base.path.read_bytes()
        answer(memory, "new small reply")
        assert await instance.history.checkpoint_async("round")
        assert instance.history.store.base.path.read_bytes() == old_base
        assert instance.history.store.last_bytes < len(old_base) // 10
        sequence = instance.history.store.sequence
        assert await instance.history.checkpoint_async("round")
        assert instance.history.store.sequence == sequence
        assert instance.history.store.last_bytes == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("wire_shape", list(WireShape))
def test_multiday_clocks_survive_recovery_and_archive_but_never_enter_model_wire(tmp_path, wire_shape):
    from pal.llm.projection_session import codec_for_shape
    from pal.llm.shapes.base import ShapeContext
    from pal.shared.json_values import thaw_json
    from tests.test_v3_c9cb2d2_review_fixes import _model_view_request

    later = "2000-01-03T12:00:00+00:00"

    def check_wire(memory):
        request = _model_view_request(memory)
        encoded = codec_for_shape(wire_shape).encode(request, ShapeContext(
            wire_shape=wire_shape, endpoint_id="e", model_id="m"))
        payload = json.dumps(thaw_json(encoded.payload), ensure_ascii=False)
        assert "第一天" in payload and "第三天" in payload
        assert NOW not in payload and later not in payload
        assert "conversation_timestamp" not in payload

    async def scenario():
        source = app(tmp_path)
        memory = begin(source, "第一天")
        memory.append_l1_user("t", LLMMessageIR(
            role=MessageRole.USER, parts=(TextPartIR("第三天"),), message_id="u2",
            semantic_kind="user_interjection", metadata={"conversation_timestamp": later}))
        check_wire(memory)
        assert await source.history.checkpoint_async("round")
        target = app(tmp_path)
        await target._restore_checkpoint_async()
        assert target.last_checkpoint_status == "restored", target.last_checkpoint_error
        restored = target.handle.memory_service
        check_wire(restored)
        archive = target.history.prepare_history_reference(frozen(restored))
        assert [record["timestamp"] for record in archive.records] == [NOW, later]
        assert archive.header["conversation_start"] == NOW
        assert archive.header["conversation_end"] == later
        archive.publish()
        records = read_messages(archive.paths)
        assert [path.name for path in archive.paths] == ["2000-01-01.jsonl", "2000-01-03.jsonl"]
        assert [record["timestamp"] for record in records] == [NOW, later]

    asyncio.run(scenario())


def test_failed_save_preserves_ram_and_last_point_then_retries(tmp_path):
    async def scenario():
        instance = app(tmp_path)
        memory = begin(instance)
        await instance.history.checkpoint_async("round")
        old_sequence = instance.history.store.sequence
        answer(memory, "new reply")
        with patch.object(instance.history.store, "save", side_effect=OSError("private body must not be logged")):
            assert not await instance.history.checkpoint_async("round")
        assert instance.history.status == "degraded"
        assert instance.history.store.sequence == old_sequence
        assert "private body" not in instance.history.last_error
        assert memory.history.turns[0].messages[-1].text == "new reply"
        saved = ResidentJournalStore(tmp_path).read()
        assert len(saved["modules"]["memory"]["payload"]["l1_turns"][0]["messages"]) == 1
        assert await instance.history.checkpoint_async("round")
        assert instance.history.status == "saved"
        assert instance.history.store.sequence == old_sequence + 1
    asyncio.run(scenario())


def test_retry_after_replace_fsync_failure_does_not_reuse_a_covered_sequence(tmp_path):
    async def scenario():
        instance = app(tmp_path)
        memory = begin(instance)
        await instance.history.checkpoint_async("round")
        answer(memory, "reply before replace failure")
        with patch("pal.memory.persistence.checkpoint._fsync_directory", side_effect=OSError("sync failed")):
            assert not await instance.history.checkpoint_async("compact", rebase=True)
        # The replacement is visible, but its directory sync was not
        # acknowledged. A retry must use that actual disk cursor.
        answer(memory, "newest R must survive", message_id="newest")
        assert await instance.history.checkpoint_async("round")
        saved = ResidentJournalStore(tmp_path).read()
        messages = saved["modules"]["memory"]["payload"]["l1_turns"][0]["messages"]
        assert messages[-1]["message_id"] == "newest"
        assert saved["sequence"] == 3
    asyncio.run(scenario())


def test_partial_write_rolls_back_then_next_boundary_repairs_the_point(tmp_path):
    import os
    async def scenario():
        instance = app(tmp_path)
        memory = begin(instance)
        await instance.history.checkpoint_async("round")
        answer(memory)
        write = os.write
        calls = 0
        def broken(descriptor, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                return write(descriptor, data[:8])
            raise OSError("disk unavailable")
        with patch("pal.memory.persistence.journal.os.write", side_effect=broken):
            assert not await instance.history.checkpoint_async("round")
        assert ResidentJournalStore(tmp_path).read()["sequence"] == 1
        assert await instance.history.checkpoint_async("round")
        saved = ResidentJournalStore(tmp_path).read()
        assert saved["sequence"] == 2
        assert saved["modules"]["memory"]["payload"]["l1_turns"][0]["messages"][-1]["message_id"] == "a"
    asyncio.run(scenario())


def test_executor_automatically_persists_last_round_without_turn_settlement(tmp_path):
    from types import SimpleNamespace
    from pal.core.turns import LLMRequestEffect
    from tests.test_v3_n3_vertical_trace import _runtime, _executor, _request_for, CapturingTransport
    async def scenario():
        instance = app(tmp_path)
        memory = instance.handle.memory_service
        memory.begin_l1_turn("T", user_message=LLMMessageIR(MessageRole.USER, (TextPartIR("question"),),
            message_id="u", semantic_kind="user_request"), metadata={"conversation_tracking": True})
        executor = _executor(memory, _runtime(CapturingTransport(["last reply"])))
        executor.context.port_registry["memory:persistence"] = instance.history
        executor.build_turn_prompt = lambda *args, **kwargs: _request_for(memory, ())
        continuation = SimpleNamespace(budget_failure_feedback_text="", llm_round_index=0,
            preferred_llm_endpoint_id=None, preferred_llm_model_id=None, finalization_only=False,
            tool_observations=[], last_response_mode=None, turn_id="T", delivery_binding=None)
        await executor._handle_llm_request(LLMRequestEffect(assembly_context=PromptAssemblyContext()), continuation)
        saved = ResidentJournalStore(tmp_path).read()
        turn = saved["modules"]["memory"]["payload"]["l1_turns"][0]
        assert turn["state"] == "active"
        assert turn["messages"][-1]["parts"][0]["text"] == "last reply"
        if instance.history.tasks:
            await asyncio.gather(*tuple(instance.history.tasks))
    asyncio.run(scenario())


def test_partial_journal_tail_is_ignored_and_trimmed_before_append(tmp_path):
    async def scenario():
        instance = app(tmp_path)
        memory = begin(instance)
        await instance.history.checkpoint_async("round")
        answer(memory)
        await instance.history.checkpoint_async("round")
        with instance.history.store.path.open("ab") as stream:
            stream.write(b'{"ciphertext":"incomplete')
        restored = ResidentJournalStore(tmp_path)
        snapshot = restored.read()
        assert snapshot["sequence"] == 2
        snapshot["stage_key"] = "resident_changed"
        restored.save(snapshot)
        assert ResidentJournalStore(tmp_path).read()["sequence"] == 3
    asyncio.run(scenario())


def test_complete_corrupt_journal_is_not_overwritten_by_later_saves(tmp_path):
    async def scenario():
        source = app(tmp_path)
        memory = begin(source)
        await source.history.checkpoint_async("round")
        answer(memory)
        await source.history.checkpoint_async("round")
        source.history.store.path.write_bytes(b'{"ciphertext":"bad","schema_version":1}\n')
        old_bytes = source.history.store.path.read_bytes()
        target = app(tmp_path)
        await target._restore_checkpoint_async()
        assert target.last_checkpoint_status == "restore_failed"
        assert not await target.history.checkpoint_async("round")
        assert target.history.store.path.read_bytes() == old_bytes
        await target.history.reset_async()
        assert not target.history.restore_failed
        assert ResidentJournalStore(tmp_path).read()["modules"]["memory"]["payload"]["l1_turns"] == []
    asyncio.run(scenario())


def test_archive_is_jsonl_with_original_dialogue_and_first_preview_only(tmp_path):
    instance = app(tmp_path)
    memory = begin(instance)
    result_store = instance.handle.core.context.execution_runtime.result_snapshots
    result = result_store.capture("FULL OUTPUT SHOULD NOT BE ARCHIVED", call_id="c", lifetime="t")
    memory.stream_l1_assistant("t", LLMMessageIR(
        MessageRole.ASSISTANT, (ReasoningPartIR("PRIVATE REASONING"), ToolCallIR("c", "read", {})), message_id="call"))
    memory.append_l1_tool_result("t", ToolResultIR("c", "read", "first preview\n" + render_snapshot_hint(result),
        snapshot_refs=(result,), replay_result_ref="HANDLER", structured={"full": "SECRET STRUCTURED"}))
    memory.append_l1_user_contexts("t", (LLMMessageIR(MessageRole.USER, (TextPartIR("INJECTED CONTEXT"),),
                                                    semantic_kind="pal_prompt_context"),))
    answer(memory)
    instance.history.record_reply("t", "回复")
    memory.settle_l1_turn("t")
    source = frozen(memory)
    archive = instance.history.prepare_history_reference(source)
    archive.publish()
    archive.publish()
    lines = archive.paths[0].read_text().splitlines()
    records = [json.loads(line) for line in lines]
    body = archive.paths[0].read_text()
    assert records[0]["date"] == "2000-01-01"
    assert [item["role"] for item in records[1:]] == ["user", "tool", "assistant"]
    assert records[1]["text"] == "原话\n第二行"
    for excluded in ["PRIVATE REASONING", "INJECTED CONTEXT", "HANDLER", "SECRET STRUCTURED",
                     "FULL OUTPUT SHOULD NOT BE ARCHIVED", result.path]:
        assert excluded not in body
    assert "first preview" in body
    assert archive.paths[0].stat().st_mode & 0o777 == 0o600
    assert archive.paths[0].parent.stat().st_mode & 0o777 == 0o700
    decorated = archive.decorate(entry())
    assert str(archive.paths[0]) in decorated.rendered
    assert "search miss" in decorated.rendered
    assert list(decorated.payload["conversation_history"]["paths"]) == [str(path) for path in archive.paths]


def test_current_text_is_redacted_but_full_recovery_preserves_original(tmp_path):
    async def scenario():
        instance = app(tmp_path)
        original = "第一行\napi_key=private-value\nAuthorization: Bearer another-value"
        memory = begin(instance, original)
        answer(memory)
        instance.history.record_reply("t", "已记录\npassword=reply-value")
        assert await instance.history.checkpoint_async("round")
        path = instance.history.text_store.path
        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert [record["role"] for record in records[1:]] == ["user", "assistant"]
        assert records[1]["timestamp"] == NOW
        assert records[1]["redacted"] and records[2]["redacted"]
        for secret in ("private-value", "another-value", "reply-value", "PRIVATE REASONING", "signature"):
            assert secret not in path.read_text()
        saved = ResidentJournalStore(tmp_path).read()
        assert saved["modules"]["memory"]["payload"]["l1_turns"][0]["messages"][0]["parts"][0]["text"] == original
        restored = app(tmp_path)
        await restored._restore_checkpoint_async()
        assert restored.last_checkpoint_status == "restored", restored.last_checkpoint_error
        turn = restored.handle.memory_service.history.turns[0]
        assert turn.messages[0].text == original
        assert turn.messages[-1].reasoning_text == "PRIVATE REASONING"
        assert turn.messages[-1].replay.payload["message"]["signature"] == "opaque"
        archive = restored.history.prepare_history_reference(frozen(restored.handle.memory_service))
        archive.publish()
        assert archive.paths[0].parent == path.parent
        assert all(secret not in archive.paths[0].read_text() for secret in ("private-value", "another-value", "reply-value"))
    asyncio.run(scenario())


def test_current_text_appends_new_records_without_rewriting_old_rounds(tmp_path):
    async def scenario():
        instance = app(tmp_path)
        memory = begin(instance)
        await instance.history.checkpoint_async("round")
        path = instance.history.text_store.path
        previous = path.read_bytes()
        answer(memory)
        instance.history.record_reply("t", "新回复")
        await instance.history.checkpoint_async("round")
        assert path.read_bytes().startswith(previous)
        assert len(path.read_text().splitlines()) == 3
        await instance.history.checkpoint_async("round")
        assert instance.history.text_store.last_bytes == 0
        assert len(path.read_text().splitlines()) == 3
    asyncio.run(scenario())


def test_compact_and_reset_keep_daily_history_without_rewriting_it(tmp_path):
    async def scenario():
        instance = app(tmp_path)
        memory = begin(instance, "旧话")
        answer(memory)
        instance.history.record_reply("t", "旧回复")
        memory.settle_l1_turn("t")
        await instance.history.checkpoint_async("round")
        current = instance.history.text_store.path
        source = frozen(memory)
        memory.begin_l1_turn("right", user_message=LLMMessageIR(
            MessageRole.USER, (TextPartIR("正在进行"),), message_id="r", semantic_kind="user_request",
            metadata={"conversation_origin": "user", "conversation_timestamp": NOW}),
            metadata={"conversation_tracking": True})
        await instance.history.checkpoint_async("round")
        before_compact = current.read_bytes()
        archive = instance.history.prepare_history_reference(source)
        archive.publish()
        memory.compact_left(source.run_id, archive.decorate(entry()))
        await instance.history.checkpoint_async("compact", rebase=True)
        assert current.read_bytes() == before_compact
        assert sorted(path.name for path in current.parent.glob("*.jsonl")) == ["2000-01-01.jsonl"]
        assert archive.paths[0].parent == current.parent
        assert "旧话" in archive.paths[0].read_text() and "旧回复" in archive.paths[0].read_text()
        assert "旧话" in current.read_text() and "旧回复" in current.read_text()
        assert "正在进行" in current.read_text()
        await instance.history.reset_async()
        assert "正在进行" in current.read_text()
        assert archive.paths[0].exists()
    asyncio.run(scenario())


def test_searchable_write_failure_does_not_break_full_recovery_and_retries(tmp_path):
    async def scenario():
        instance = app(tmp_path)
        begin(instance, "survives searchable write failure")
        with patch.object(instance.history.text_store, "sync_memory", side_effect=OSError("private error")):
            assert await instance.history.checkpoint_async("round")
        assert instance.history.status == "degraded"
        assert instance.history.take_degraded_notice()
        assert not instance.history.restore_failed
        assert "private error" not in instance.history.last_error
        restored = app(tmp_path)
        await restored._restore_checkpoint_async()
        assert restored.last_checkpoint_status == "restored", restored.last_checkpoint_error
        assert "survives searchable write failure" in restored.history.text_store.path.read_text()
    asyncio.run(scenario())


def test_current_text_repairs_incomplete_tail_without_repeating_old_messages(tmp_path):
    from pal.memory.persistence.conversation import ConversationTextJournal, records_from_memory_payload
    async def scenario():
        instance = app(tmp_path)
        memory = begin(instance)
        await instance.history.checkpoint_async("round")
        current = instance.history.text_store.path
        with current.open("ab") as stream:
            stream.write(b'{"text":"partial')
        answer(memory)
        instance.history.record_reply("t", "新回复")
        snapshot = await instance.history._capture("round")
        payload = snapshot["modules"]["memory"]["payload"]
        writer = ConversationTextJournal(tmp_path)
        writer.sync(records_from_memory_payload(payload), memory.history_root.incarnation)
        records = [json.loads(line) for line in current.read_text().splitlines()]
        assert len(records) == 3
        assert [record["role"] for record in records[1:]] == ["user", "assistant"]
    asyncio.run(scenario())


def test_failed_first_text_append_retries_with_a_valid_header(tmp_path):
    import os
    async def scenario():
        instance = app(tmp_path)
        begin(instance)
        write = os.write
        calls = 0
        def broken(descriptor, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                return write(descriptor, data[:8])
            raise OSError("text write unavailable")
        with patch("pal.memory.persistence.conversation.os.write", side_effect=broken):
            assert await instance.history.checkpoint_async("round")
        assert instance.history.status == "degraded"
        assert await instance.history.checkpoint_async("round")
        records = [json.loads(line) for line in instance.history.text_store.path.read_text().splitlines()]
        assert len(records) == 2
        assert records[0]["type"] == "header" and records[1]["role"] == "user"
    asyncio.run(scenario())


def test_durable_recovery_holds_output_until_new_compact_base_is_saved(tmp_path):
    async def scenario():
        instance = app(tmp_path)
        result_store = instance.handle.core.context.execution_runtime.result_snapshots
        memory = begin(instance)
        ref = result_store.capture("saved output", call_id="c", lifetime="t")
        memory.stream_l1_assistant("t", LLMMessageIR(MessageRole.ASSISTANT, (ToolCallIR("c", "read", {}),), message_id="a"))
        memory.append_l1_tool_result("t", ToolResultIR("c", "read", "preview", snapshot_refs=(ref,)))
        result_store.finish_delivery(lifetime="t", call_id="c")
        memory.settle_l1_turn("t")
        await instance.history.checkpoint_async("round")
        source = frozen(memory)
        archive = instance.history.prepare_history_reference(source)
        archive.publish()
        memory.compact_left(source.run_id, archive.decorate(entry()))
        assert Path(ref.path).exists()
        with patch.object(instance.history.store, "save", side_effect=OSError("disk unavailable")):
            assert not await instance.history.checkpoint_async("compact", rebase=True)
        assert Path(ref.path).exists()
        recover_old = app(tmp_path)
        await recover_old._restore_checkpoint_async()
        assert recover_old.last_checkpoint_status == "restored", recover_old.last_checkpoint_error
        assert any(message.role == MessageRole.TOOL for turn in recover_old.handle.memory_service.history.turns for message in turn.messages)
        # Use the original live host again to advance the recovery point;
        # no second process is started against a real runtime in this test.
        assert await instance.history.checkpoint_async("compact", rebase=True)
        assert not Path(ref.path).exists()
        assert archive.paths[0].exists()
    asyncio.run(scenario())


def test_reset_keeps_archive_and_cannot_resurrect_old_context(tmp_path):
    async def scenario():
        instance = app(tmp_path)
        memory = begin(instance)
        answer(memory)
        instance.history.record_reply("t", "回复")
        memory.settle_l1_turn("t")
        old_incarnation = memory.history_root.owner_state()["incarnation"]
        source = frozen(memory)
        archive = instance.history.prepare_history_reference(source)
        archive.publish()
        memory.compact_left(source.run_id, archive.decorate(entry()))
        await instance.history.checkpoint_async("compact", rebase=True)
        await instance.history.reset_async()
        assert memory.history_root.owner_state()["incarnation"] != old_incarnation
        assert archive.paths[0].exists()
        restored = app(tmp_path)
        await restored._restore_checkpoint_async()
        assert restored.last_checkpoint_status == "restored"
        assert restored.handle.memory_service.history.turns == ()
    asyncio.run(scenario())


def test_failed_reset_restores_live_state_without_reporting_success(tmp_path):
    async def scenario():
        instance = app(tmp_path)
        memory = begin(instance)
        memory.settle_l1_turn("t")
        await instance.history.checkpoint_async("round")
        old_incarnation = memory.history_root.owner_state()["incarnation"]
        with patch.object(instance.history.store, "save", side_effect=OSError("disk unavailable")):
            with pytest.raises(OSError):
                await instance.history.reset_async()
        assert memory.history_root.owner_state()["incarnation"] == old_incarnation
        assert memory.history.turns[0].messages[0].text == "原话\n第二行"
    asyncio.run(scenario())


def test_reset_reuses_daily_file_and_distinguishes_same_message_ids(tmp_path):
    async def scenario():
        instance = app(tmp_path)
        begin(instance, "first incarnation")
        assert await instance.history.checkpoint_async("round")
        path = instance.history.text_store.path
        first_id = instance.handle.memory_service.history_root.incarnation
        await instance.history.reset_async()
        begin(instance, "second incarnation")
        assert await instance.history.checkpoint_async("round")
        assert instance.history.text_store.path == path
        rows = read_messages((path,))
        assert [row["text"] for row in rows] == ["first incarnation", "second incarnation"]
        assert [row["message_id"] for row in rows] == ["u", "u"]
        assert rows[0]["conversation_id"] == first_id
        assert rows[0]["conversation_id"] != rows[1]["conversation_id"]
        restored = app(tmp_path)
        await restored._restore_checkpoint_async()
        assert restored.last_checkpoint_status == "restored"
        assert read_messages((path,)) == rows
    asyncio.run(scenario())


def test_reset_does_not_clear_dialogue_if_searchable_preservation_fails(tmp_path):
    async def scenario():
        instance = app(tmp_path)
        memory = begin(instance)
        await instance.history.checkpoint_async("round")
        incarnation = memory.history_root.incarnation
        answer(memory, "keep unsaved progress")
        with patch.object(instance.history.text_store, "sync_memory", side_effect=OSError("disk unavailable")):
            with pytest.raises(OSError):
                await instance.history.reset_async()
        assert memory.history_root.incarnation == incarnation
        assert memory.history.turns[0].messages[-1].text == "keep unsaved progress"
    asyncio.run(scenario())


def test_stream_fragments_do_not_write_and_partial_reply_is_archived(tmp_path):
    instance = app(tmp_path)
    memory = begin(instance)
    for text in ["可见", "的", "部分回复"]:
        instance.history.record_stream("t", "text_delta", text)
    assert not instance.history.store.base.path.exists()
    assert memory.history.turns[0].metadata.get("conversation_replies") is None
    instance.history.finish_partial_stream("t")
    memory.settle_l1_turn("t")
    records = history_records(frozen(memory))
    assert records[-1]["text"] == "可见的部分回复"
    assert records[-1]["partial"]


def test_affordance_explains_jq_and_is_absent_from_bunshin(tmp_path):
    provider = ResidentHistoryPromptProvider(tmp_path)
    fragments = provider.build_prompt_fragments(PromptAssemblyContext())
    text = "\n".join(fragment.content for fragment in fragments)
    assert "jq -r" in text and "type=header" in text and "rg -n" in text
    assert str(tmp_path / "data" / "conversation-history") in text
    assert "YYYY-MM-DD.jsonl" in text and "one pass" in text
    assert "undated.jsonl" in text and "conversation_id" in text
    assert "redacted" in text
    assert provider.build_prompt_fragments(PromptAssemblyContext(core_mode="bunshin")) == []


def test_archive_failure_prevents_compact_install_and_right_is_preserved(tmp_path):
    from tests.test_runtime_compaction import _valid_pal_payload
    from tests.test_two_segment_executor_flow import _Transport, _ScriptedLLM
    async def scenario():
        instance = app(tmp_path)
        memory = begin(instance)
        answer(memory)
        memory.settle_l1_turn("t")
        memory.begin_l1_turn("right", user_text="UNSENT RIGHT")
        source = frozen(memory)
        before = memory.history.turns
        archive = instance.history.prepare_history_reference(source)
        snapshot = CompactionSnapshot.capture_left(memory, source, target_input_budget=100000,
            reserved_output_tokens=1024, clock_kind=CompactionClockKind.USER_TURN, clock_value=1)
        with patch.object(ConversationHistoryReference, "publish", side_effect=OSError("disk unavailable")):
            result = await CompactionEngine(PalCompactionPolicy(), max_attempts=1).run(snapshot,
                memory_service=memory, llm_runtime=_Transport(_ScriptedLLM(_valid_pal_payload("summary"))), commit_hooks=archive)
        assert result.status == "commit_failed"
        assert memory.history.turns == before
        assert memory.history_root.right_turns()[-1].messages[0].text == "UNSENT RIGHT"
    asyncio.run(scenario())
