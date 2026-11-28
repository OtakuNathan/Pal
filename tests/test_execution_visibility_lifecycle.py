"""Execution visibility must be turn-local and must not survive restoration."""
from tests.core_factory import build_core
from pal.core.capabilities import register_with_core as register_core
from pal.core.turns import TurnContinuation
from pal.execution import register_with_core as register_execution
from pal.memory import MemoryService, register_with_core as register_memory


def setup_core(tmp_path):
    core = build_core()
    register_core(core)
    register_execution(core.context)
    core.context.execution_runtime.configure_runtime_root(tmp_path)
    memory = MemoryService()
    register_memory(core.context, memory)
    return core, memory


def begin(core, memory, turn_id, text):
    memory.begin_l1_turn(turn_id, user_text=text)
    continuation = TurnContinuation(turn_id, iter(()), turn_id)
    core._begin_tool_result_turn(continuation)
    return continuation


def test_turn_exit_clears_only_its_visibility(tmp_path):
    core, memory = setup_core(tmp_path)
    sessions = core.context.execution_runtime.execution_sessions
    sessions.set_visible_results('job', ('read-one',))
    sessions.set_visible_results('chat', ('read-two',))
    core.turn_manager._mark_turn_exited('job')
    assert sessions.visible_results('job') == frozenset()
    assert sessions.visible_results('chat') == frozenset({'read-two'})


def test_request_visibility_is_not_restored_as_delivery_proof(tmp_path):
    from pal.execution.runtime_state import ExecutionRuntimeStatePort
    core, memory = setup_core(tmp_path)
    begin(core, memory, 'job', 'task')
    runtime = core.context.execution_runtime
    port = ExecutionRuntimeStatePort(runtime)
    runtime.execution_sessions.set_visible_results('job', ('old-read',))
    prepared = port.prepare_restore_state(port.snapshot_state())
    port.install_prepared_state(prepared)
    assert runtime.execution_sessions.visible_results('job') == frozenset()
