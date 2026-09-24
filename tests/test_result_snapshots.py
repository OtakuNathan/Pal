"""Immutable output copies follow explicit L1 ownership, not elapsed turns."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from pal.core.continuity_compaction import ContinuityCompactionPolicy, CONTINUITY_FIELDS
from pal.core.compaction import CompactionSnapshot, CompactionClockKind
from pal.execution.result_snapshots import ResultSnapshotStore, capture_stream_files
from pal.execution.runtime import ExecutionRuntime
from pal.execution.contracts import ToolCallBudget
from pal.execution.tool_facade import ToolHandlerResult
from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
from pal.llm.serde import message_to_payload, message_from_payload
from pal.memory.service import MemoryService
from pal.memory.turn_ir import L1TurnIR, L1TurnState
from pal.shared.tool_protocol import ToolResultIR, new_tool_call
from tests.capability_fixture import mount_test_capability
from tests.test_immutable_tool_facade import _echo_kwargs


def result_turn(ref, identity="t"):
    return L1TurnIR(turn_id=identity, state=L1TurnState.SETTLED, messages=(LLMMessageIR(
        role=MessageRole.USER, parts=(TextPartIR("output copy"),),
        metadata={"result_snapshots": [ref.to_dict()]}),))


def test_l1_transfer_multiple_owners_and_inflight_pin(tmp_path):
    store = ResultSnapshotStore(tmp_path)
    memory = MemoryService()
    history = memory.l1_store.turns
    store.bind_history(history)
    ref = store.capture("hello", call_id="c", lifetime="s")
    path = Path(ref.path)
    history.append(result_turn(ref))
    store.finish_delivery(lifetime="s", call_id="c")
    history.append(result_turn(ref, "t2"))
    history.replace_all([history.get("t2")])
    assert path.read_text() == "hello"
    with store.pin((ref,)):
        history.clear()
        assert path.exists()
    assert not path.exists()


def test_atomic_summary_transfer_does_not_unlink_retained_file(tmp_path):
    store = ResultSnapshotStore(tmp_path)
    memory = MemoryService()
    history = memory.l1_store.turns
    store.bind_history(history)
    keep = store.capture("KEEP", call_id="k", lifetime="s")
    drop = store.capture("DROP", call_id="d", lifetime="s")
    history.append(result_turn(keep, "k"))
    history.append(result_turn(drop, "d"))
    store.finish_references((keep, drop))
    history.replace_all([result_turn(keep, "summary")])
    assert Path(keep.path).read_text() == "KEEP"
    assert not Path(drop.path).exists()
    history.clear()
    assert not Path(keep.path).exists()


def test_uncommitted_result_is_deleted_and_text_paths_are_not_owners(tmp_path):
    store = ResultSnapshotStore(tmp_path)
    memory = MemoryService()
    store.bind_history(memory.l1_store.turns)
    ref = store.capture("copy", call_id="c", lifetime="s")
    memory.l1_store.turns.append(L1TurnIR(turn_id="t", state=L1TurnState.SETTLED,
        messages=(LLMMessageIR(role=MessageRole.USER, parts=(TextPartIR(ref.path),)),)))
    store.finish_delivery(lifetime="s", call_id="c")
    assert not Path(ref.path).exists()


def test_replayed_finish_is_idempotent_and_unicode_bytes_are_exact(tmp_path):
    store = ResultSnapshotStore(tmp_path)
    ref = store.capture("中文😀\r\n", call_id="c", lifetime="s")
    store.own("consumer", (ref,))
    for _ in range(3):
        store.finish_delivery(lifetime="s", call_id="c")
    assert Path(ref.path).read_bytes() == "中文😀\r\n".encode()
    store.release("consumer")
    assert not Path(ref.path).exists()


def test_snapshot_reference_roundtrips_without_becoming_provider_content(tmp_path):
    store = ResultSnapshotStore(tmp_path)
    ref = store.capture("evidence", call_id="c", lifetime="s")
    message = LLMMessageIR(role=MessageRole.TOOL, parts=(ToolResultIR(
        call_id="c", name="echo", content="preview", snapshot_refs=(ref,)),))
    restored = message_from_payload(message_to_payload(message))
    assert restored == message
    assert restored.text == "preview"


def test_capture_streams_observes_exact_boundaries_and_never_aliases_live_file(tmp_path):
    store = ResultSnapshotStore(tmp_path / "runtime")
    source = tmp_path / "stdout"
    source.write_bytes(b"oldNEWlater")
    ref = capture_stream_files(store, [("stdout", str(source), 3, 6)], call_id="c", lifetime="s")
    source.write_bytes(b"replacement")
    assert Path(ref.path).read_text() == "stdout:\nNEW\n"
    with pytest.raises(OSError):
        capture_stream_files(store, [("stdout", str(source), 0, 100)], call_id="bad", lifetime="s")
    assert not list(store.root.glob("*.pending"))


def test_generic_result_spills_exact_text_and_keeps_machine_control(tmp_path):
    runtime = ExecutionRuntime(runtime_root=tmp_path)
    text = "first\n" + "字" * 9000 + "\nlast"
    try:
        mount_test_capability(runtime, **_echo_kwargs(handler=lambda _: ToolHandlerResult(
            output={"echo": text}, llm_text=text)))
        result = runtime.invoke_indirect_tool(new_tool_call(name="echo", args={"value": "x"}, call_id="c"),
            budget=ToolCallBudget(max_output_chars=800, preview_chars=300), turn_id="t")
        assert result.kind == "complete"
        assert result.output["echo"] == text
        assert Path(result.snapshot_refs[0].path).read_text() == text
        assert "first" in result.llm_text and "last" in result.llm_text
        assert len(result.llm_text) < 800
        assert "read_tool_result" not in result.llm_text
    finally:
        runtime.shutdown()


def test_compaction_accepts_only_source_references_and_keeps_typed_ownership(tmp_path):
    store = ResultSnapshotStore(tmp_path)
    memory = MemoryService()
    ref = store.capture("source", call_id="c", lifetime="s")
    memory.l1_store.turns.append(result_turn(ref))
    snapshot = CompactionSnapshot(target_input_budget=4000, reserved_output_tokens=1000,
        clock_kind=CompactionClockKind.USER_TURN, clock_value=1,
        memory_items=tuple(tuple(t) for t in memory.l1_store.items))
    policy = ContinuityCompactionPolicy("pal")
    payload = {"schema": policy.policy_id, "kind": "pal", "summary": {"summary": "review unfinished"},
               "continuity": {key: [] for key in CONTINUITY_FIELDS}, "retained_result_refs": [ref.snapshot_id]}
    entry = policy.validate_checkpoint(json.dumps(payload), snapshot)
    assert entry.payload["result_snapshots"] == [ref.to_dict()]
    assert ref.path in entry.rendered
    payload["retained_result_refs"] = ["made-up"]
    with pytest.raises(ValueError, match="unknown snapshot"):
        policy.validate_checkpoint(json.dumps(payload), snapshot)


def test_restore_preserves_live_reference_and_collects_orphans(tmp_path):
    first = ResultSnapshotStore(tmp_path)
    ref = first.capture("saved", call_id="c", lifetime="s")
    orphan = first.capture("unused", call_id="o", lifetime="s")
    second = ResultSnapshotStore(tmp_path)
    second.restore_refs(first.snapshot_state())
    memory = MemoryService()
    memory.l1_store.turns.append(result_turn(ref))
    second.bind_history(memory.l1_store.turns)
    second.finish_restore()
    assert Path(ref.path).exists()
    assert not Path(orphan.path).exists()


def test_invalid_reference_rejects_before_l1_replacement(tmp_path):
    store = ResultSnapshotStore(tmp_path)
    memory = MemoryService()
    history = memory.l1_store.turns
    store.bind_history(history)
    ref = store.capture('keep', call_id='c', lifetime='s')
    original = result_turn(ref)
    history.append(original)
    store.finish_references((ref,))
    with pytest.raises(ValueError, match='snapshot path'):
        history.replace_all([result_turn(replace(ref, path=str(tmp_path / 'outside')), 'bad')])
    assert history.turns == (original,)
    assert Path(ref.path).read_text() == 'keep'


def test_restore_detaches_old_root_and_collects_only_managed_files(tmp_path):
    store = ResultSnapshotStore(tmp_path)
    old, new = MemoryService(), MemoryService()
    ref = store.capture('old', call_id='c', lifetime='s')
    old.l1_store.turns.append(result_turn(ref))
    store.bind_history(old.l1_store.turns)
    store.finish_references((ref,))
    unrelated = store.root / 'notes.txt'
    unrelated.write_text('keep')
    store.restore_refs(store.snapshot_state())
    store.detach_histories()
    store.bind_history(new.l1_store.turns)
    store.finish_restore()
    assert not Path(ref.path).exists()
    assert unrelated.read_text() == 'keep'
    old.l1_store.turns.clear()
    assert not store.references()


def test_builtin_shell_streams_snapshot_and_reports_save_failure_honestly(tmp_path):
    from pal.execution.shell_exec import ShellExecTool
    runtime = ExecutionRuntime(runtime_root=tmp_path)
    budget = ToolCallBudget(max_output_chars=1000, preview_chars=300)
    async def run():
        result = await ShellExecTool().ainvoke({'cmd': 'printf HEAD; head -c 200000 /dev/zero; printf TAIL'},
            runtime=runtime, budget=budget, turn_id='t', call_id='c')
        assert len(result.llm_text) < 1200
        assert not result.structured['stdout']
        assert b'\0' * 200000 in Path(result.snapshot_refs[0].path).read_bytes()
        counter = tmp_path / 'counter'
        def fail(*args, **kwargs):
            raise OSError('disk full')
        runtime.result_snapshots.capture_chunks = fail
        failed = await ShellExecTool().ainvoke({'cmd': f"printf once >> '{counter}'; head -c 5000 /dev/zero"},
            runtime=runtime, budget=budget, turn_id='t', call_id='bad')
        assert failed.structured['error_code'] == 'output_snapshot_failed'
        assert failed.structured['returncode'] == 0
        assert 'could not start' not in failed.text
        assert counter.read_text() == 'once'
    try:
        asyncio.run(run())
    finally:
        runtime.shutdown()


def test_snapshot_read_is_not_source_edit_authority_and_copy_is_not_editable(tmp_path):
    from pal.core import PalCore
    from pal.execution import register_with_core
    core = PalCore()
    runtime = core.context.execution_runtime
    runtime.configure_runtime_root(tmp_path / 'runtime')
    register_with_core(core.context)
    core.publish_module_capabilities('execution')
    source = tmp_path / 'source.txt'
    source.write_text('old value\n')
    ref = runtime.result_snapshots.capture(source.read_text(), call_id='output', lifetime='s')
    try:
        read = runtime.execute_tool(new_tool_call(name='read_file', args={'file_path': ref.path}), turn_id='t')
        assert read.ok, read.text
        assert read.context_delivery is None
        denied = runtime.execute_tool(new_tool_call(name='edit_file', args={'file_path': str(source),
            'edits': [{'old_string': 'old', 'new_string': 'new'}]}), turn_id='t')
        assert not denied.ok
        assert source.read_text() == 'old value\n'
        copy_edit = runtime.execute_tool(new_tool_call(name='edit_file', args={'file_path': ref.path,
            'edits': [{'old_string': 'old', 'new_string': 'new'}]}), turn_id='t')
        assert not copy_edit.ok
        assert copy_edit.invocation_result.error_code == 'immutable_result_snapshot'
    finally:
        runtime.shutdown()


def test_coordinator_restore_rebuilds_l1_owners_then_reset_retires_files(tmp_path):
    from pal.core.module_registry import ModuleHandle, ModuleRegistry
    from pal.core.runtime_state import RuntimeSnapshotCoordinator, RuntimeSnapshotIdentity
    from pal.memory.runtime_state import MemoryRuntimeStatePort
    from pal.execution.runtime_state import ExecutionRuntimeStatePort
    runtime, memory = ExecutionRuntime(runtime_root=tmp_path), MemoryService()
    ref = runtime.result_snapshots.capture('persisted', call_id='c', lifetime='s')
    memory.l1_store.turns.append(result_turn(ref))
    runtime.bind_result_history(memory)
    runtime.result_snapshots.finish_references((ref,))
    registry = ModuleRegistry()
    for port in (MemoryRuntimeStatePort(memory), ExecutionRuntimeStatePort(runtime)):
        registry.register(ModuleHandle(module_id=port.module_id, tier='test', runtime_state_port=port))
    coordinator = RuntimeSnapshotCoordinator(registry)
    identity = RuntimeSnapshotIdentity('role', 'workflow', 'stage', 1, 1, 'spec')
    async def run():
        state = await coordinator.snapshot(identity)
        runtime.result_snapshots.pin_history_request(memory.l1_store.turns, 'old-request')
        late = runtime.result_snapshots.capture('abandoned', call_id='late', lifetime='s')
        old_history = memory.l1_store.turns
        await coordinator.restore(state, expected_identity=identity)
        assert memory.l1_store.turns is not old_history
        assert Path(ref.path).read_text() == 'persisted'
        assert not Path(late.path).exists()
        old_history.clear()
        assert Path(ref.path).exists()
        await coordinator.reset('explicit reset')
        assert not Path(ref.path).exists()
    try:
        asyncio.run(run())
    finally:
        runtime.shutdown()


def test_source_read_grants_only_exact_head_tail_preview_offsets(tmp_path):
    from pal.core import PalCore
    from pal.execution import register_with_core
    from pal.execution.session_state import FileDeliveryManifest
    core = PalCore()
    runtime = core.context.execution_runtime
    runtime.configure_runtime_root(tmp_path / 'runtime')
    register_with_core(core.context)
    core.publish_module_capabilities('execution')
    source = tmp_path / 'source.txt'
    lines = [f'line{i:03d} ' + 'x' * 80 for i in range(100)]
    source.write_text('\n'.join(lines) + '\n')
    try:
        result = runtime.execute_tool(new_tool_call(name='read_file', args={'file_path': str(source)}),
            turn_id='t', budget=ToolCallBudget(max_output_chars=1500, preview_chars=600))
        assert result.ok, result.text
        manifest = FileDeliveryManifest.from_dict(result.context_delivery)
        assert manifest and manifest.spans
        assert not manifest.complete_file
        assert not any(span.start_line == 50 for span in manifest.spans)
        for span in manifest.spans:
            assert result.invocation_result.llm_text[span.start_offset:span.end_offset] == (
                (f'{span.start_line:6d}\t' + lines[span.start_line - 1])[span.visible_start_in_line:span.visible_end_in_line])
    finally:
        runtime.shutdown()


def test_complete_result_and_builtin_definition_obey_output_budget(tmp_path):
    from pal.core import PalCore
    from pal.execution import register_with_core
    from pal.execution.tool_facade import CompleteResult, EffectOutcome
    from pydantic import create_model, Field
    from tests.test_immutable_tool_facade import EchoInput
    core = PalCore()
    register_with_core(core.context)
    core.publish_module_capabilities('execution')
    runtime = core.context.execution_runtime
    budget = ToolCallBudget(max_output_chars=1000, preview_chars=200)
    try:
        text = 'DIRECT' * 4000
        kwargs = _echo_kwargs(handler=lambda _: CompleteResult(output={'echo': text}, effect=EffectOutcome.NONE, llm_text=text))
        kwargs['InputModel'] = create_model('LargeSchema', __base__=EchoInput, value=(str, Field(description=text)))
        mount_test_capability(runtime, **kwargs)
        result = runtime.invoke_indirect_tool(new_tool_call(name='echo', args={'value': 'x'}, call_id='direct'), budget=budget, turn_id='t')
        assert len(result.llm_text) <= 1000
        assert Path(result.snapshot_refs[0].path).read_text() == text
        result = runtime.invoke_direct_tool(new_tool_call(name='read_tool', args={'name': 'echo'}, call_id='definition'), budget=budget, turn_id='t')
        assert result.kind == 'complete', result
        assert len(result.llm_text) <= 1000
        assert text in Path(result.snapshot_refs[0].path).read_text()
    finally:
        core.close()


@pytest.mark.parametrize("direct_result", [False, True])
def test_save_failure_keeps_host_control_and_context_messages(tmp_path, monkeypatch, direct_result):
    from pal.execution.tool_facade import CompleteResult, EffectOutcome
    from pal.shared.tool_protocol import ToolContextMessageIR
    runtime = ExecutionRuntime(runtime_root=tmp_path)
    output = {'channel_event': {'action': 'clear'}, 'echo': 'X' * 5000}
    context = (ToolContextMessageIR(content='independent fact', semantic_kind='reference'),)
    try:
        from pal.execution.contracts import CapabilityResult
        from pal.execution.tool_facade import StructuredToolOutput
        from pal.shared import RuntimeStatus
        raw = (CompleteResult(output=output, effect=EffectOutcome.APPLIED, llm_text='X' * 5000, context_messages=context)
            if direct_result else CapabilityResult(status=RuntimeStatus.OK, structured=output,
                llm_text='X' * 5000, context_messages=context))
        kwargs = _echo_kwargs(handler=lambda _: raw)
        kwargs['OutputModel'] = StructuredToolOutput
        mount_test_capability(runtime, **kwargs)
        def fail(*args, **kwargs):
            raise OSError('disk full')
        monkeypatch.setattr(runtime.result_snapshots, 'capture_chunks', fail)
        result = runtime.invoke_indirect_tool(new_tool_call(name='echo', args={'value': 'x'}),
            budget=ToolCallBudget(max_output_chars=1000, preview_chars=200), turn_id='t')
        assert result.kind == 'complete'
        assert result.output == output
        assert result.context_messages == context
        assert result.output_error == 'disk full'
        canonical = runtime._canonical_result_from_invocation('echo', 'c', result)
        assert canonical.structured['channel_event'] == output['channel_event']
        assert canonical.context_messages == context
        assert 'disk full' in result.llm_text
        assert len(result.llm_text) <= 1000
    finally:
        runtime.shutdown()


def test_long_source_line_has_conditional_shell_edit_guidance(tmp_path):
    from pal.core import PalCore
    from pal.execution import register_with_core
    core = PalCore()
    register_with_core(core.context)
    core.publish_module_capabilities('execution')
    runtime = core.context.execution_runtime
    path = tmp_path / 'long.json'
    path.write_text('x' * 10000)
    try:
        result = runtime.execute_tool(new_tool_call(name='read_file', args={'file_path': str(path)}, call_id='long'),
            budget=ToolCallBudget(max_output_chars=1500, preview_chars=300), turn_id='long')
        assert 'focused shell edit' in result.llm_text
        assert not result.context_delivery['complete_file']
        path.write_text('short\n')
        result = runtime.execute_tool(new_tool_call(name='read_file', args={'file_path': str(path)}, call_id='short'),
            budget=ToolCallBudget(max_output_chars=1500, preview_chars=300), turn_id='short')
        assert 'focused shell edit' not in result.llm_text
    finally:
        core.close()


@pytest.mark.parametrize('disk_full', [False, True])
def test_only_delivered_source_lines_grant_edits(tmp_path, monkeypatch, disk_full):
    from pal.core import PalCore
    from pal.execution import register_with_core
    core = PalCore()
    register_with_core(core.context)
    core.publish_module_capabilities('execution')
    runtime = core.context.execution_runtime
    path = tmp_path / 'source.txt'
    path.write_text(''.join(f'line-{n:04d}\n' for n in range(1, 1001)))
    if disk_full:
        def fail(*args, **kwargs):
            raise OSError('disk full')
        monkeypatch.setattr(runtime.result_snapshots, 'capture_chunks', fail)
    try:
        result = runtime.execute_tool(new_tool_call(name='read_file', args={'file_path': str(path), 'limit': 1000}, call_id='read'),
            budget=ToolCallBudget(max_output_chars=1400, preview_chars=300), turn_id='t')
        runtime.commit_tool_delivery(turn_id='t', result_id='read', context_delivery=dict(result.context_delivery))
        context = runtime.logical_context_for_turn('t')
        grant = runtime.logical_state.file_grant(execution_lifetime_id=context.execution_lifetime_id,
            file_key=str(path.resolve()), digest=result.context_delivery['digest'])
        assert grant is not None and not grant.complete
        assert not any(start <= 500 <= end for start, end in grant.covered_ranges)
        assert all(f'line-{n:04d}' in result.llm_text for start, end in grant.covered_ranges for n in range(start, end + 1))
        hidden_edit = runtime.invoke_direct_tool(new_tool_call(name='edit_file', args={'file_path': str(path),
            'edits': [{'old_string': 'line-0500', 'new_string': 'hidden-change'}]}), turn_id='t')
        assert 'PARTIAL_READ' in hidden_edit.llm_text
        assert 'line-0500' in path.read_text()
    finally:
        core.close()
