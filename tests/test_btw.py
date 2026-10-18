from __future__ import annotations

import asyncio
from copy import copy
from dataclasses import replace

import pytest

from pal.core import MainContext, PalCore
from pal.control.contracts import ControlAction, ControlEvent, ControlRoute
from pal.control.service import ControlPlane
from pal.core.btw import BTW_INSTRUCTION, cancel_btw, start_btw
from pal.llm.contracts import LLMGenerationResult, LLMPreflightAdvice, LLMSideTurn, LLM_SIDE_TURNS
from pal.llm.runtime import LLMRuntime
from pal.llm.capabilities import register_with_core as register_llm
from pal.llm.ir import LLMFinishReason, LLMMessageIR, LLMResponseIR, MessageRole, TextPartIR
from pal.memory.persistence.contracts import RESIDENT_HISTORY
from pal.memory.service import MemoryService
from pal.memory.turn_ir import L1TurnIR, L1TurnState
from pal.shared import EventKind, ToolExecutionResult
from pal.shared.tool_protocol import new_tool_call
from tests.llm_fakes import NonStreamingLLM
from tests.test_interjection_injection import _make_envelope


ROUTE = ControlRoute("voice", "memory", {"request_id": "req", "session_id": "phone"}, "voice:phone")


class Output:
    def __init__(self):
        self.replies = []

    def get_endpoint(self, _):
        return None

    def queue_reply(self, binding, message):
        self.replies.append((binding, message))
        return "reply"

    def queue_status(self, binding, kind, *, payload=None):
        return "status"


class Model(NonStreamingLLM):
    def __init__(self, *, blocked=False, tool=False, overflow=False, error=False):
        self.requests = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.tool = tool
        self.overflow = overflow
        self.error = error

    def preflight(self, request):
        return LLMPreflightAdvice(status="compact_required" if self.overflow else "ready")

    def active_endpoint(self):
        return None

    def create_side_turn(self, *, scope_id, branch_id, message_ids):
        child = copy(self)
        endpoint = child.active_endpoint()
        return LLMSideTurn(child, lambda: None,
            endpoint.endpoint_id if endpoint is not None else None,
            endpoint.model_id if endpoint is not None else None)

    async def agenerate(self, request, **options):
        self.requests.append(request)
        self.entered.set()
        await self.release.wait()
        if self.error:
            raise RuntimeError("model unavailable")
        parts = ((new_tool_call(name="read_fixture", args={}, call_id="read1"),)
                 if self.tool and len(self.requests) == 1 else (TextPartIR("side answer"),))
        return LLMGenerationResult(response=LLMResponseIR(
            message=LLMMessageIR(role=MessageRole.ASSISTANT, parts=parts),
            finish_reason=LLMFinishReason.TOOL_CALLS if self.tool and len(self.requests) == 1 else LLMFinishReason.STOP,
        ))


def make_core(model):
    context = MainContext()
    context.port_registry["memory:memory"] = MemoryService()
    if isinstance(model, LLMRuntime):
        register_llm(context, model)
    else:
        context.port_registry["llm:llm"] = model
        context.port_registry[LLM_SIDE_TURNS.name] = model
    context.port_registry["channel:channel"] = Output()
    core = PalCore(context=context)
    events = []
    context.core_event_bus.subscribe("turn.start", lambda topic, payload: events.append((topic, payload)))
    context.core_event_bus.subscribe("turn.end", lambda topic, payload: events.append((topic, payload)))
    return core, events


def action(text="side question", route=ROUTE):
    return ControlAction("start_btw", "runtime", args={"text": text}, route=route)


def seed_left(memory):
    turn = L1TurnIR(turn_id="old", state=L1TurnState.SETTLED, messages=(
        LLMMessageIR(role=MessageRole.USER, parts=(TextPartIR("old question"),), message_id="old-u"),
        LLMMessageIR(role=MessageRole.ASSISTANT, parts=(TextPartIR("old answer"),), message_id="old-a"),
    ))
    memory.history.append(turn)
    memory.history_root.promote()
    return turn


def test_slash_preserves_question_text_and_rejects_empty():
    plane = ControlPlane()
    text = 'explain "two words"\n  and this line'
    parsed = plane.parse_event(ControlEvent(EventKind.SLASH_COMMAND, "channel", {"text": "/btw " + text}, route=ROUTE))
    assert parsed.action_kind == "start_btw"
    assert parsed.args["text"] == text
    assert parsed.route == ROUTE
    assert plane.parse_event(ControlEvent(EventKind.SLASH_COMMAND, "channel", {"text": "/btw"})).action_kind == "invalid_command"
    assert "/btw <question>" in plane.render_panel_text()


def test_side_turn_uses_only_left_and_never_writes_resident_history():
    async def scenario():
        core, events = make_core(Model())
        memory = core.context.require_port("memory:memory")
        left = seed_left(memory)
        main = core.turn_manager.start(_make_envelope(turn_id="main", text="main request in R"))
        memory.begin_l1_turn("main", user_text="main request in R")
        before = memory.history.turns
        class ForbiddenHistory:
            def __getattr__(self, name):
                raise AssertionError("side turn touched resident history: " + name)
        core.context.port_registry[RESIDENT_HISTORY.name] = ForbiddenHistory()
        await start_btw(core, action())
        branch = core._btw_turn
        assert core.state.active_turn_id == main.turn_id
        assert branch.execution.logical_state is not core.context.execution_runtime.logical_state
        await branch.task
        assert memory.history.turns == before
        assert memory.history.turns[0] is left
        requests = core.context.require_port("llm:llm").requests
        assert requests, core.state.diagnostics
        body = "\n".join(m.text for m in requests[0].messages)
        assert "old answer" in body and "main request in R" not in body
        assert BTW_INSTRUCTION in body
        assert requests[0].logical_scope_id == "pal:resident"
        assert requests[0].metadata["transient_history"]
        assert core.state.btw_turn_id is None
        assert core.state.active_turn_id == "main"
        assert [topic for topic, _ in events] == ["turn.start", "turn.end"]
        assert events[-1][1]["status"] == "success"
        output = core.context.require_port("channel:channel")
        assert output.replies[-1][0].response_handle.reply_target == ROUTE.reply_target
        assert output.replies[-1][1].text == "side answer"
        assert id(branch.memory.history) not in branch.snapshots._histories
        core.close()
    asyncio.run(scenario())


def test_native_side_read_leaves_main_completion_for_resident(tmp_path):
    import shlex
    native = pytest.importorskip("pal_shell_native.runtime")
    from pal.execution import register_with_core as register_execution
    from pal_shell_native.events import attach_completion_source

    gate = tmp_path / "finish-main"
    marker = "MAIN_UNDELIVERED_OUTPUT"

    class ReadingModel(Model):
        main_rounds = 0
        side_rounds = 0
        sid = None

        async def agenerate(self, request, **options):
            self.requests.append(request)
            call = None
            if request.metadata.get("transient_history"):
                self.side_rounds += 1
                if self.side_rounds == 1:
                    call = new_tool_call(name="call_tool", args={
                        "name": "read_shell_session", "args": {"session_id": self.sid}})
            else:
                self.main_rounds += 1
                if self.main_rounds == 1:
                    call = new_tool_call(name="run_shell", args={
                        "cmd": f"while [ ! -f {shlex.quote(str(gate))} ]; do sleep .01; done; printf {marker}",
                        "wait_ms": 1})
            return LLMGenerationResult(response=LLMResponseIR(
                message=LLMMessageIR(role=MessageRole.ASSISTANT,
                    parts=(call,) if call else (TextPartIR("answer"),)),
                finish_reason=LLMFinishReason.TOOL_CALLS if call else LLMFinishReason.STOP))

    async def scenario():
        model = ReadingModel()
        execution = native.NativeExecutionRuntime()
        context = MainContext(execution_runtime=execution)
        memory = MemoryService()
        context.port_registry["memory:memory"] = memory
        context.port_registry["llm:llm"] = model
        context.port_registry[LLM_SIDE_TURNS.name] = model
        context.port_registry["channel:channel"] = Output()
        core = PalCore(context=context)
        register_execution(context)
        core.publish_module_capabilities("execution")
        attach_completion_source(core, execution)
        owner = execution.shell_owner
        try:
            await core._schedule_admitted_channel_turn_async(_make_envelope(turn_id="main-origin", text="run inspection"))
            await asyncio.wait_for(core.state.turn_tasks["main-origin"], timeout=5)
            model.sid = next(iter(owner.sessions))
            before = memory.history.turns
            gate.touch()
            await owner.shell.session_snapshot(model.sid, wait_ms=5000)
            await start_btw(core, action("Read the inspection output"))
            await asyncio.wait_for(core._btw_turn.task, timeout=5)
            assert model.sid in owner.sessions
            assert memory.history.turns == before
            assert any(marker in m.text for r in model.requests if r.metadata.get("transient_history")
                       for m in r.messages if m.role == MessageRole.TOOL)
            owner.observations.collect()
            await asyncio.gather(*tuple(owner.observations.preparing.values()))
            await core._schedule_admitted_channel_turn_async(_make_envelope(turn_id="main-followup", text="Continue"))
            await asyncio.wait_for(core.state.turn_tasks["main-followup"], timeout=5)
            main_request = [r for r in model.requests if not r.metadata.get("transient_history")][-1]
            assert any(marker in m.text and m.semantic_kind == "runtime_context_artifact" for m in main_request.messages)
            await asyncio.gather(*tuple(owner.observations.acking.values()))
            assert model.sid not in owner.sessions
        finally:
            gate.touch()
            await execution.shutdown_async()
            core.close()
    asyncio.run(scenario())


def test_busy_slot_cancellation_and_reuse():
    async def scenario():
        model = Model(blocked=True)
        core, events = make_core(model)
        await start_btw(core, action())
        branch = core._btw_turn
        await asyncio.wait_for(model.entered.wait(), timeout=5)
        await start_btw(core, action("second"))
        assert core._btw_turn is branch
        assert len(model.requests) == 1
        assert "already running" in core.context.require_port("channel:channel").replies[-1][1]
        await cancel_btw(core)
        assert core.state.btw_turn_id is None
        assert events[-1][1]["status"] == "interrupted"
        model.release.set()
        await start_btw(core, action("third"))
        await core._btw_turn.task
        assert len(model.requests) == 2
        core.close()
    asyncio.run(scenario())


def test_cancel_before_task_start_releases_slot_and_pins():
    async def scenario():
        core, events = make_core(Model())
        await start_btw(core, action())
        branch = core._btw_turn
        await cancel_btw(core)
        assert core.state.btw_turn_id is None
        assert not core.state.active_turns
        assert id(branch.memory.history) not in branch.snapshots._histories
        assert events[-1][1]["status"] == "interrupted"
        core.close()
    asyncio.run(scenario())


def test_shutdown_checkpoint_cancels_side_and_drains_mainloop():
    from types import SimpleNamespace
    from pal.runtime_app import PalRuntimeApp

    async def scenario():
        model = Model(blocked=True)
        core, events = make_core(model)
        await start_btw(core, action())
        branch = core._btw_turn
        await asyncio.wait_for(model.entered.wait(), timeout=5)
        idle = asyncio.create_task(core.run_until_idle_async(max_iterations=128))

        async def publish():
            assert branch.task.done()
            assert core.state.btw_turn_id is None

        app = SimpleNamespace(handle=SimpleNamespace(core=core), _publish_checkpoint_async=publish)
        core.state.resident_drained_event.set()
        await asyncio.wait_for(PalRuntimeApp._checkpoint_for_shutdown_async(app), timeout=5)
        await asyncio.wait_for(idle, timeout=5)
        assert branch.task.cancelled()
        assert app.last_checkpoint_status == "l1_saved"
        assert events[-1][1]["status"] == "interrupted"
        core.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["cancel", "reset", "shutdown"])
def test_cancellation_during_cleanup_keeps_slot_and_leases_until_cleanup_exits(operation):
    from types import SimpleNamespace
    from pal.runtime_app import PalRuntimeApp

    async def scenario():
        core, events = make_core(Model())
        memory = core.context.require_port("memory:memory")
        seed_left(memory)
        before = memory.history.turns
        entered, release = asyncio.Event(), asyncio.Event()
        cleanup_calls = []
        await start_btw(core, action())
        branch = core._btw_turn

        async def cleanup(turn_id):
            cleanup_calls.append(turn_id)
            entered.set()
            await release.wait()

        branch.execution.finish_transient_turn = cleanup
        await asyncio.wait_for(entered.wait(), timeout=5)
        if operation == "reset":
            request = asyncio.create_task(core._execute_soft_reset_async(None))
        elif operation == "shutdown":
            async def publish():
                assert branch.task.done()
            app = SimpleNamespace(handle=SimpleNamespace(core=core), _publish_checkpoint_async=publish)
            core.state.resident_drained_event.set()
            request = asyncio.create_task(PalRuntimeApp._checkpoint_for_shutdown_async(app))
        else:
            request = asyncio.create_task(cancel_btw(core))
        try:
            await asyncio.sleep(0)
            # A second cancellation must not cancel the cleanup owner either.
            branch.task.cancel()
            await asyncio.sleep(0)
            assert not request.done()
            assert core.state.btw_turn_id == branch.turn_id
            assert core._btw_turn is branch
            assert id(branch.memory.history) in branch.snapshots._histories
            assert memory.history.turns == before
            assert not any(topic == "turn.end" for topic, _ in events)
            release.set()
            await asyncio.wait_for(request, timeout=5)
            assert branch.task.cancelled()
            assert cleanup_calls == [branch.turn_id]
            assert core.state.btw_turn_id is None
            assert id(branch.memory.history) not in branch.snapshots._histories
            assert events[-1][1]["status"] == "interrupted"
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)
            core.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("overflow,error", [(True, False), (False, True)])
def test_failure_does_not_compact_main_or_keep_slot(overflow, error):
    async def scenario():
        model = Model(overflow=overflow, error=error)
        core, events = make_core(model)
        memory = core.context.require_port("memory:memory")
        seed_left(memory)
        before = memory.history.turns
        await start_btw(core, action())
        await core._btw_turn.task
        assert core.state.btw_turn_id is None
        assert events[-1][1]["status"] == "failed"
        assert memory.history.turns == before
        assert memory.history_root.active_run is None
        if overflow:
            assert not model.requests
        core.close()
    asyncio.run(scenario())


def test_tool_round_is_local_and_keeps_main_tool_schemas():
    async def scenario():
        model = Model(tool=True)
        core, _ = make_core(model)
        memory = core.context.require_port("memory:memory")
        seed_left(memory)
        before = memory.history.turns
        schemas = [{"type": "function", "function": {"name": "read_fixture", "description": "Read a fixture.",
                    "parameters": {"type": "object", "properties": {}}}}]
        core.turn_executor._build_llm_tool_contracts = lambda: schemas
        await start_btw(core, action())
        branch = core._btw_turn
        async def read(call, **kwargs):
            return ToolExecutionResult(name=call.name, call_id=call.call_id, ok=True, text="fixture content", llm_text="fixture content")
        branch.execution.execute_tool_async = read
        await branch.task
        assert len(model.requests) == 2, core.state.diagnostics
        assert "fixture content" in "\n".join(m.text for m in model.requests[-1].messages)
        assert [t.name for t in model.requests[0].tools] == ["read_fixture"]
        assert model.requests[0].tools == model.requests[1].tools
        assert memory.history.turns == before
        core.close()
    asyncio.run(scenario())


def test_streamed_side_reply_uses_original_route_and_leaves_no_history():
    from pal.llm.ir import LLMResponseDeltaKind, LLMResponseUpdate

    class StreamingModel(Model):
        def supports_streaming(self, request=None):
            return True

        async def astream(self, request, **options):
            self.requests.append(request)
            response = LLMResponseIR(LLMMessageIR(role=MessageRole.ASSISTANT,
                parts=(TextPartIR("streamed answer"),)), LLMFinishReason.STOP)
            yield LLMResponseUpdate(response, LLMResponseDeltaKind.TEXT, text_delta="streamed answer")
            yield LLMResponseUpdate(response, LLMResponseDeltaKind.STATE)

    class StreamOutput(Output):
        def __init__(self):
            super().__init__()
            self.updates = []

        def supports_stream_delivery(self, binding):
            return True

        def queue_stream_update(self, binding, update):
            self.updates.append((binding, update))
            return "update"

    async def scenario():
        core, events = make_core(StreamingModel())
        output = StreamOutput()
        core.context.port_registry["channel:channel"] = output
        memory = core.context.require_port("memory:memory")
        seed_left(memory)
        before = memory.history.turns
        await start_btw(core, action())
        await core._btw_turn.task
        assert events[-1][1]["status"] == "success", core.state.diagnostics
        assert output.updates
        assert all(binding.response_handle.reply_target == ROUTE.reply_target for binding, _ in output.updates)
        assert any(update.text == "streamed answer" for _, update in output.updates)
        assert memory.history.turns == before
        assert not output.replies  # no duplicate terminal reply after streaming
        core.close()
    asyncio.run(scenario())


def test_snapshot_lease_survives_main_history_retirement():
    async def scenario():
        model = Model(blocked=True)
        core, _ = make_core(model)
        memory = core.context.require_port("memory:memory")
        store = core.context.execution_runtime.result_snapshots
        store.bind_history(memory.history)
        ref = store.capture("original evidence", call_id="old-result", lifetime="main")
        left = seed_left(memory)
        memory.history.replace(replace(left, revision=left.revision + 1, messages=(left.messages[0], replace(
            left.messages[1], metadata={"result_snapshots": [ref.to_dict()]}))))
        store.finish_delivery(lifetime="main", call_id="old-result")
        await start_btw(core, action())
        branch = core._btw_turn
        await asyncio.wait_for(model.entered.wait(), timeout=5)
        memory.history.clear()
        from pathlib import Path
        assert Path(ref.path).read_text() == "original evidence"
        model.release.set()
        await branch.task
        assert not Path(ref.path).exists()
        assert id(memory.history) in store._histories
        core.close()
    asyncio.run(scenario())


def test_main_and_side_model_requests_progress_independently():
    class ConcurrentModel(Model):
        async def agenerate(self, request, **options):
            if request.metadata.get("transient_history"):
                return await super().agenerate(request, **options)
            self.requests.append(request)
            return LLMGenerationResult(response=LLMResponseIR(
                message=LLMMessageIR(role=MessageRole.ASSISTANT, parts=(TextPartIR("main answer"),)),
                finish_reason=LLMFinishReason.STOP))

    async def scenario():
        model = ConcurrentModel(blocked=True)
        core, events = make_core(model)
        await start_btw(core, action())
        branch = core._btw_turn
        await asyncio.wait_for(model.entered.wait(), timeout=5)
        await core._schedule_admitted_channel_turn_async(_make_envelope(turn_id="main-live", text="main question"))
        main_task = core.state.turn_tasks["main-live"]
        await asyncio.wait_for(main_task, timeout=5)
        assert not branch.task.done()
        assert any(topic == "turn.end" and payload["turn_id"] == "main-live" for topic, payload in events)
        assert any("main answer" in m.text for t in core.context.require_port("memory:memory").history.turns for m in t.messages)
        model.release.set()
        await branch.task
        assert not any("side answer" in m.text for t in core.context.require_port("memory:memory").history.turns for m in t.messages)
        core.close()
    asyncio.run(scenario())


def test_reset_cancels_side_before_clearing_resident_memory():
    async def scenario():
        model = Model(blocked=True)
        core, events = make_core(model)
        seed_left(core.context.require_port("memory:memory"))
        await core.handle_control_action_async(action())
        branch = core._btw_turn
        await asyncio.wait_for(model.entered.wait(), timeout=5)
        assert await core._execute_soft_reset_async(None)
        assert branch.task.cancelled()
        assert core.state.btw_turn_id is None
        assert not core.context.require_port("memory:memory").history.turns
        assert events[-1][1]["status"] == "interrupted"
        assert not core.state.resident_quiescing
        core.close()
    asyncio.run(scenario())


def test_interrupt_still_targets_main_while_side_is_running():
    class BlockBoth(Model):
        def __init__(self):
            super().__init__(blocked=True)
            self.main_entered = asyncio.Event()

        async def agenerate(self, request, **options):
            if not request.metadata.get("transient_history"):
                self.main_entered.set()
            return await super().agenerate(request, **options)

    async def scenario():
        model = BlockBoth()
        core, events = make_core(model)
        await core._schedule_admitted_channel_turn_async(_make_envelope(turn_id="main-interrupt", text="main"))
        main_task = core.state.turn_tasks["main-interrupt"]
        await asyncio.wait_for(model.main_entered.wait(), timeout=5)
        model.entered.clear()
        await start_btw(core, action())
        branch = core._btw_turn
        await asyncio.wait_for(model.entered.wait(), timeout=5)
        await core._handle_interrupt_turn_async(ControlAction("interrupt_turn", "runtime", route=ROUTE))
        await asyncio.gather(main_task, return_exceptions=True)
        assert not branch.task.done()
        assert any(topic == "turn.end" and payload["turn_id"] == "main-interrupt"
                   and payload["status"] == "interrupted" for topic, payload in events)
        model.release.set()
        await branch.task
        assert events[-1][1]["status"] == "success"
        core.close()
    asyncio.run(scenario())


def test_idle_side_turn_refreshes_model_selection_before_forking():
    from types import SimpleNamespace

    class SelectedModel(Model):
        selected = "old-endpoint"

        def refresh_runtime_settings(self):
            self.selected = "selected-endpoint"

        def active_endpoint(self):
            return SimpleNamespace(endpoint_id=self.selected, model_id=self.selected)

    async def scenario():
        model = SelectedModel()
        core, _ = make_core(model)
        model.selected = "old-endpoint"
        await start_btw(core, action())
        branch = core._btw_turn
        await branch.task
        assert model.requests[0].metadata["preferred_endpoint_id"] == "selected-endpoint"
        core.close()
    asyncio.run(scenario())


def test_provider_without_side_turn_port_returns_unavailable_without_starting_turn():
    async def scenario():
        model = Model()
        core, events = make_core(model)
        core.context.port_registry.pop(LLM_SIDE_TURNS.name)
        await start_btw(core, action())
        assert not model.requests
        assert not events
        assert core.state.btw_turn_id is None
        assert "does not support side questions" in core.context.require_port("channel:channel").replies[-1][1]
        core.close()
    asyncio.run(scenario())


def test_side_turn_accepts_published_immutable_tool_schemas():
    from pal.execution import register_with_core as register_execution

    async def scenario():
        model = Model()
        core, events = make_core(model)
        register_execution(core.context)
        core.publish_module_capabilities("execution")
        contracts = core.turn_executor._build_llm_tool_contracts()
        assert contracts
        await start_btw(core, action())
        assert core._btw_turn is not None, core.context.require_port("channel:channel").replies
        await core._btw_turn.task
        assert model.requests[0].tools
        assert core.turn_executor._build_llm_tool_contracts() == contracts
        assert events[-1][1]["status"] == "success", core.state.diagnostics
        core.close()
    asyncio.run(scenario())


def test_side_turn_leaves_unsolicited_main_execution_updates_unconsumed():
    from pal.execution.runtime import ExecutionRuntime

    class LiveExecution(ExecutionRuntime):
        claims = 0
        refreshes = 0

        def prepare_model_context(self, memory, continuation, *, context_view=None):
            self.claims += 1
            memory.append_l1_user_contexts(continuation.turn_id, (
                LLMMessageIR(role=MessageRole.USER, message_id="main-update",
                             parts=(TextPartIR("main-only live execution output"),)),
            ))

        def model_response_received(self, continuation):
            self.refreshes += 1

    async def scenario():
        model = Model()
        core, _ = make_core(model)
        core.context.execution_runtime = LiveExecution()
        await start_btw(core, action())
        branch = core._btw_turn
        await branch.task
        assert model.requests, core.state.diagnostics
        assert "main-only live execution output" not in "\n".join(m.text for m in model.requests[0].messages)
        assert branch.execution.claims == 0
        assert branch.execution.refreshes == 0
        # Ordinary admission still owns and receives the waiting observation.
        await core._schedule_admitted_channel_turn_async(_make_envelope(turn_id="main-updated", text="main question"))
        await core.state.turn_tasks["main-updated"]
        assert "main-only live execution output" in "\n".join(m.text for m in model.requests[-1].messages)
        assert core.context.execution_runtime.claims > 0
        assert core.context.execution_runtime.refreshes > 0
        core.close()
    asyncio.run(scenario())


def test_native_shell_side_result_is_not_written_to_main_history():
    native = pytest.importorskip("pal_shell_native.runtime")
    from pal.execution import register_with_core as register_execution
    from pal_shell_native.events import attach_completion_source

    class NativeModel(Model):
        async def agenerate(self, request, **options):
            self.requests.append(request)
            call = len(self.requests) == 1
            parts = ((new_tool_call(name="run_shell", args={"cmd": "printf btw-native", "wait_ms": 5000}, call_id="native-read"),)
                     if call else (TextPartIR("side answer"),))
            return LLMGenerationResult(response=LLMResponseIR(
                message=LLMMessageIR(role=MessageRole.ASSISTANT, parts=parts),
                finish_reason=LLMFinishReason.TOOL_CALLS if call else LLMFinishReason.STOP))

    async def scenario():
        model = NativeModel()
        execution = native.NativeExecutionRuntime()
        context = MainContext(execution_runtime=execution)
        context.port_registry["memory:memory"] = MemoryService()
        context.port_registry["llm:llm"] = model
        context.port_registry[LLM_SIDE_TURNS.name] = model
        context.port_registry["channel:channel"] = Output()
        core = PalCore(context=context)
        register_execution(context)
        core.publish_module_capabilities("execution")
        attach_completion_source(core, execution)
        before = execution.logical_state.snapshot_state()
        try:
            await start_btw(core, action())
            assert core._btw_turn is not None, context.require_port("channel:channel").replies
            await asyncio.wait_for(core._btw_turn.task, timeout=15)
            assert len(model.requests) == 2, core.state.diagnostics
            assert "btw-native" in "\n".join(m.text for m in model.requests[1].messages)
            assert not context.require_port("memory:memory").history.turns
            assert execution.logical_state.snapshot_state() == before
            # Native output acknowledgement runs independently after delivery.
            await asyncio.wait_for(asyncio.gather(*execution.shell_owner.observations.acking.values()), timeout=5)
            assert not execution.shell_owner.pending
        finally:
            await execution.shutdown_async()
            core.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("finished,cancel", [(True, False), (False, False), (False, True), (False, "cleanup")])
def test_native_side_sessions_do_not_notify_main_and_are_retired(finished, cancel):
    native = pytest.importorskip("pal_shell_native.runtime")
    from pal.execution import register_with_core as register_execution
    from pal_shell_native.events import attach_completion_source

    class SessionModel(Model):
        side_rounds = 0
        main_rounds = 0

        async def agenerate(self, request, **options):
            self.requests.append(request)
            side = request.metadata.get("transient_history")
            if side:
                self.side_rounds += 1
                first = self.side_rounds == 1
                if not first:
                    self.entered.set()
                    await self.release.wait()
            else:
                self.main_rounds += 1
                first = self.main_rounds == 1
            delay = "0.1" if side and finished else "30"
            marker = "side-private-result" if side else "main-result"
            parts = ((new_tool_call(name="run_shell", args={
                "cmd": f"sleep {delay}; printf {marker}", "wait_ms": 1,
            }, call_id="side-session" if side else "main-session"),)
                if first else (TextPartIR("side answer" if side else "main answer"),))
            return LLMGenerationResult(response=LLMResponseIR(
                message=LLMMessageIR(role=MessageRole.ASSISTANT, parts=parts),
                finish_reason=LLMFinishReason.TOOL_CALLS if first else LLMFinishReason.STOP))

    async def scenario():
        model = SessionModel(blocked=True)
        execution = native.NativeExecutionRuntime()
        context = MainContext(execution_runtime=execution)
        context.port_registry["memory:memory"] = MemoryService()
        context.port_registry["llm:llm"] = model
        context.port_registry[LLM_SIDE_TURNS.name] = model
        context.port_registry["channel:channel"] = Output()
        core = PalCore(context=context)
        register_execution(context)
        core.publish_module_capabilities("execution")
        attach_completion_source(core, execution)
        cleanup_entered = asyncio.Event()
        cleanup_release = asyncio.Event()
        try:
            await start_btw(core, action())
            branch = core._btw_turn
            await asyncio.wait_for(model.entered.wait(), timeout=5)
            side_sid = next(iter(execution.shell_owner.sessions))
            if finished:
                await asyncio.sleep(0.2)
            execution.shell_owner.observations.collect()
            await core._schedule_admitted_channel_turn_async(_make_envelope(turn_id="main-live", text="main question"))
            await asyncio.wait_for(core.state.turn_tasks["main-live"], timeout=5)
            main_requests = [r for r in model.requests if not r.metadata.get("transient_history")]
            assert len(main_requests) == 2, core.state.diagnostics
            assert all(m.metadata.get("session_id") != side_sid for r in main_requests for m in r.messages)
            assert "side-private-result" not in "\n".join(m.text for r in main_requests for m in r.messages)
            memory = context.require_port("memory:memory")
            before = memory.history.turns
            if cancel == "cleanup":
                original_snapshot = execution.shell_owner.shell.session_snapshot

                async def gated_snapshot(sid, **options):
                    if sid == side_sid and options.get("action") == "terminate":
                        cleanup_entered.set()
                        await cleanup_release.wait()
                    return await original_snapshot(sid, **options)

                execution.shell_owner.shell.session_snapshot = gated_snapshot
                model.release.set()
                await asyncio.wait_for(cleanup_entered.wait(), timeout=5)
                cancellation = asyncio.create_task(cancel_btw(core))
                await asyncio.sleep(0)
                assert not cancellation.done()
                assert core._btw_turn is branch
                assert side_sid in execution.shell_owner.sessions
                cleanup_release.set()
                await asyncio.wait_for(cancellation, timeout=5)
            elif cancel:
                await asyncio.wait_for(cancel_btw(core), timeout=5)
            else:
                model.release.set()
                await asyncio.wait_for(branch.task, timeout=5)
            assert side_sid not in execution.shell_owner.sessions
            assert execution.shell_owner.sessions  # main-owned work survives
            assert all(s["origin_turn"] == "main-live" for s in execution.shell_owner.sessions.values())
            assert not any(p.turn_id == branch.turn_id for p in execution.shell_owner.pending.values())
            assert memory.history.turns == before
            assert core.state.btw_turn_id is None
            assert not execution.shell_owner.events.prepare(context)
        finally:
            cleanup_release.set()
            await execution.shutdown_async()
            core.close()
    asyncio.run(scenario())
