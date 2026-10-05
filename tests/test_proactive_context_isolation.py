"""Proactive model isolation and lossless one-way L1 handoff; no network."""
import asyncio
from unittest.mock import AsyncMock

from pal.core import PalCore, register_with_core as register_core
from pal.core.turns import TurnContinuation, MemoryCompactEffect, LLMPreflightEffect
from pal.execution import register_with_core as register_execution
from pal.execution.file_capabilities import _session_file_tools
from pal.execution.file_read import FileReadTool
from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
from pal.memory import MemoryService, register_with_core as register_memory
from pal.shared import PromptAssemblyContext, IntrospectionCall
from pal.shared.tool_protocol import new_tool_call, ToolResultIR
from tests.test_v3_n3_vertical_trace import _runtime, CapturingTransport
from tests.test_projection_two_segment import _receipt, _cursor


def setup_core(tmp_path):
    core = PalCore()
    register_core(core)
    register_execution(core.context)
    core.context.execution_runtime.configure_runtime_root(tmp_path)
    memory = MemoryService()
    register_memory(core.context, memory)
    runtime = _runtime(CapturingTransport([]))
    core.context.port_registry['llm:llm'] = runtime
    return core, memory, runtime


def begin(core, memory, turn_id, text):
    memory.begin_l1_turn(turn_id, user_text=text)
    continuation = TurnContinuation(turn_id, iter(()), turn_id)
    core._begin_tool_result_turn(continuation)
    return continuation


def prompt(core, continuation, proactive=False):
    return core.turn_executor.build_turn_prompt(continuation, PromptAssemblyContext(
        turn_kind='proactive_trigger' if proactive else 'chat'), max_output_tokens=1024)


def test_warm_chat_cannot_leak_into_proactive_and_completed_work_is_visible_to_chat(tmp_path):
    core, memory, runtime = setup_core(tmp_path)
    chat = begin(core, memory, 'chat', 'LAST_USER_SENTINEL')
    memory.upsert_l1_assistant('chat', LLMMessageIR(MessageRole.ASSISTANT, (TextPartIR('OLD_REPLY'),)))
    old = prompt(core, chat)
    packed = core.turn_executor._prepare_turn_projection(runtime, old)
    assert packed is not None
    session = packed[2]['session']
    session.observe_commit(_receipt(packed[2]['attempt'], session.frontier, _cursor(1, 'c' * 64)),
                           span_message_ids=packed[2]['tail_ids'])
    memory.settle_l1_turn('chat')
    job = begin(core, memory, 'job', '<proactive_trigger>QUIZ_TASK</proactive_trigger>')
    request = prompt(core, job, True)
    assert 'LAST_USER_SENTINEL' not in repr(request.messages)
    assert request.logical_scope_id != old.logical_scope_id
    assert request.metadata["artifact_scope_key"] == old.metadata["artifact_scope_key"]
    assert core.turn_executor._prepare_turn_projection(runtime, request) is None
    # The cold codec receives exactly the isolated model request, not root history.
    from pal.llm.shapes import codec_for_shape
    from pal.llm.shapes.base import ShapeContext
    endpoint = runtime.active_endpoint()
    wire = codec_for_shape(endpoint.wire_shape).encode(request, ShapeContext(
        endpoint.wire_shape, endpoint.endpoint_id, endpoint.model_id)).payload
    assert 'LAST_USER_SENTINEL' not in repr(wire)
    assert 'QUIZ_TASK' in repr(wire)
    call = new_tool_call('job-read', 'read_file', {'file_path': '/example'})
    memory.upsert_l1_assistant('job', LLMMessageIR(MessageRole.ASSISTANT, (call,)))
    memory.append_l1_tool_result('job', ToolResultIR('job-read', 'read_file', 'FULL_FILE_CONTENT_SENTINEL'))
    memory.upsert_l1_assistant('job', LLMMessageIR(MessageRole.ASSISTANT, (TextPartIR('JOB_DONE'),)))
    memory.settle_l1_turn('job')
    chat2 = begin(core, memory, 'chat2', 'continue')
    handed_off = prompt(core, chat2)
    assert 'FULL_FILE_CONTENT_SENTINEL' in repr(handed_off.messages)
    assert 'JOB_DONE' in repr(handed_off.messages)
    assert sum(m.turn_id == 'job' for m in memory.history.turns) == 1
    job2 = begin(core, memory, 'job2', '<proactive_trigger>NEXT_JOB</proactive_trigger>')
    next_request = prompt(core, job2, True)
    assert next_request.logical_scope_id != request.logical_scope_id
    assert 'FULL_FILE_CONTENT_SENTINEL' not in repr(next_request.messages)
    assert 'LAST_USER_SENTINEL' not in repr(next_request.messages)


def test_read_visibility_follows_request_and_completed_job_grants_remain_shared(tmp_path):
    core, memory, _ = setup_core(tmp_path)
    path = tmp_path / 'source.txt'
    path.write_text('actual source text\n')
    runtime = core.context.execution_runtime
    def reader(turn_id):
        call = IntrospectionCall(name="read_file", args={'file_path': str(path)}, meta={'turn_id': turn_id, 'execution_runtime': runtime})
        state, visibility, _, _ = _session_file_tools(None, call)
        return FileReadTool(cache=state, visibility_cache=visibility, defer_delivery=True)
    job = begin(core, memory, 'job', 'read source')
    prompt(core, job, True)
    result = reader('job').invoke({'file_path': str(path)})
    call = new_tool_call('job-read', 'read_file', {'file_path': str(path)})
    memory.upsert_l1_assistant('job', LLMMessageIR(MessageRole.ASSISTANT, (call,)))
    memory.append_l1_tool_result('job', ToolResultIR('job-read', 'read_file', result.text))
    runtime.commit_tool_delivery(turn_id='job', context_delivery=result.context_delivery, result_id='job-read')
    prompt(core, job, True)
    assert reader('job').invoke({'file_path': str(path)}).structured['unchanged']
    # A concurrent independent run cannot borrow the first run's visibility.
    job2 = begin(core, memory, 'job2', 'read same source')
    prompt(core, job2, True)
    fresh = reader('job2').invoke({'file_path': str(path)})
    assert not fresh.structured['unchanged']
    assert 'actual source text' in fresh.text
    second_call = new_tool_call('job2-read', 'read_file', {'file_path': str(path)})
    memory.upsert_l1_assistant('job2', LLMMessageIR(MessageRole.ASSISTANT, (second_call,)))
    memory.append_l1_tool_result('job2', ToolResultIR('job2-read', 'read_file', fresh.text))
    runtime.commit_tool_delivery(turn_id='job2', context_delivery=fresh.context_delivery, result_id='job2-read')
    prompt(core, job2, True)
    assert reader('job2').invoke({'file_path': str(path)}).structured['unchanged']
    memory.settle_l1_turn('job')
    chat = begin(core, memory, 'chat', 'edit what the task read')
    request = prompt(core, chat)
    assert 'actual source text' in repr(request.messages)
    assert reader('chat').invoke({'file_path': str(path)}).structured['unchanged']
    from pal.execution.file_edit import FileEditTool
    call = IntrospectionCall(name="read_file", args={}, meta={'turn_id': 'chat', 'execution_runtime': runtime})
    state, _, _, _ = _session_file_tools(None, call)
    edited = FileEditTool(cache=state).invoke({'file_path': str(path), 'edits': [
        {'old_string': 'actual source text', 'new_string': 'updated source text'}]})
    assert str(edited.status) == 'ok'
    assert 'updated source text' in path.read_text()


def test_proactive_does_not_consume_pending_user_interjections(tmp_path):
    from pal.llm.contracts import LLMPreflightAdvice
    core, memory, runtime = setup_core(tmp_path)
    job = begin(core, memory, 'job', 'task')
    job.llm_round_index = 1
    core.state.pending_channel_turns.append(object())
    core.turn_executor._inject_pending = AsyncMock(return_value=True)
    runtime.apreflight = AsyncMock(return_value=LLMPreflightAdvice(status='ready'))
    asyncio.run(core.turn_executor._handle_llm_preflight(LLMPreflightEffect(
        assembly_context=PromptAssemblyContext(turn_kind='proactive_trigger')), job))
    core.turn_executor._inject_pending.assert_not_awaited()
    assert len(core.state.pending_channel_turns) == 1


def test_proactive_compaction_does_not_touch_resident_history(tmp_path):
    core, memory, _ = setup_core(tmp_path)
    job = begin(core, memory, 'job', 'task')
    core.turn_executor.compact_memory_async = AsyncMock()
    result = asyncio.run(core.turn_executor._handle_memory_compact(MemoryCompactEffect(
        assembly_context=PromptAssemblyContext(turn_kind='proactive_trigger')), job))
    assert str(result.status) == 'error'
    core.turn_executor.compact_memory_async.assert_not_awaited()


def test_turn_exit_clears_only_its_visibility(tmp_path):
    core, memory, _ = setup_core(tmp_path)
    sessions = core.context.execution_runtime.execution_sessions
    sessions.set_visible_results('job', ('read-one',))
    sessions.set_visible_results('chat', ('read-two',))
    core.turn_manager._mark_turn_exited('job')
    assert sessions.visible_results('job') == frozenset()
    assert sessions.visible_results('chat') == frozenset({'read-two'})


def test_request_visibility_is_not_restored_as_delivery_proof(tmp_path):
    from pal.execution.runtime_state import ExecutionRuntimeStatePort
    core, memory, _ = setup_core(tmp_path)
    begin(core, memory, 'job', 'task')
    runtime = core.context.execution_runtime
    port = ExecutionRuntimeStatePort(runtime)
    runtime.execution_sessions.set_visible_results('job', ('old-read',))
    prepared = port.prepare_restore_state(port.snapshot_state())
    port.install_prepared_state(prepared)
    assert runtime.execution_sessions.visible_results('job') == frozenset()
