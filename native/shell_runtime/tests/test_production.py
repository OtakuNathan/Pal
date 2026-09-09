from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from pal.core import PalCore
from pal.core.main_context import MainContext
from pal.core.turns import TurnContinuation, LLMPreflightEffect, LLMRequestEffect, MailboxReplyEffect, EffectResult
from pal.execution import register_with_core
from pal.execution.contracts import ToolCallBudget
from pal.execution.native_shell.runtime import NativeExecutionRuntime
from pal.execution.native_shell.events import attach_completion_source
from pal.foundation import EventEnvelope
from pal.llm import LLMPreflightAdvice, generation_result_from_values
from pal.llm.ir import LLMMessageIR, MessageRole
from pal.memory import MemoryService, register_with_core as register_memory
from pal.shared import ChannelEnvelope, TurnDeliveryBinding, EndpointConfig, ResponseHandle, RuntimeStatus
from pal.shared.tool_protocol import new_tool_call, ToolResultIR


class ProductionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = NativeExecutionRuntime()
        self.core = PalCore(context=MainContext(execution_runtime=self.runtime))
        self.memory = MemoryService()
        register_with_core(self.core.context)
        register_memory(self.core.context, self.memory)
        self.core.publish_module_capabilities("execution")
        attach_completion_source(self.core, self.runtime)
        self.owner = self.runtime.shell_owner
        self.replies = []
        self.model_inputs = []
        original = self.core._execute_turn_effect_async
        async def fake_model(continuation, effect):
            if isinstance(effect, LLMPreflightEffect):
                return EffectResult(status="ok", payload=LLMPreflightAdvice(status="ready"))
            if isinstance(effect, LLMRequestEffect):
                self.model_inputs.append(effect.assembly_context.event.payload)
                return EffectResult(status="ok", payload=generation_result_from_values(text="completion received"))
            if isinstance(effect, MailboxReplyEffect):
                self.replies.append((continuation.delivery_binding, effect.text))
                return EffectResult(status=RuntimeStatus.QUEUED, text=effect.text)
            return await original(continuation, effect)
        self.core._execute_turn_effect_async = fake_model
        self.core.main_loop.bind_async_loop()

    async def asyncTearDown(self):
        await self.runtime.shutdown_async()
        self.core.close()

    async def tool(self, name, args=None, *, budget=None, turn_id="origin"):
        return await self.runtime.execute_tool_async(new_tool_call(name=name, args=args or {}), budget=budget, turn_id=turn_id)

    async def session(self, sid, **args):
        return await self.tool("call_tool", {"name": "shell_session", "args": {"session_id": sid, **args}})

    def origin(self, call):
        event = EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "build"}, event_id="origin")
        binding = TurnDeliveryBinding(endpoint=EndpointConfig(endpoint_id="test", channel_kind="socket", binding_key="test"),
            response_handle=ResponseHandle(endpoint_id="test", reply_target={"connection_id": "original"}), control_scope_key="test:original")
        continuation = TurnContinuation(turn_id="origin", program=iter(()), correlation_id="origin", opening_event=event, delivery_binding=binding)
        self.core.state.active_turns["origin"] = continuation
        self.memory.begin_l1_turn("origin", user_text="build")
        self.memory.upsert_l1_assistant("origin", LLMMessageIR(role=MessageRole.ASSISTANT, parts=(call,)))
        return continuation

    async def commit_origin(self, continuation, call, result):
        await self.core.turn_executor._append_l1_tool_result_async(continuation, call, result)
        self.memory.settle_l1_turn("origin")
        self.core.state.active_turns.pop("origin", None)

    async def wait_completion(self):
        async def wait():
            while not self.owner.shell._completions:
                await asyncio.sleep(.01)
        await asyncio.wait_for(wait(), 5)

    async def pump(self):
        self.core.main_loop.drain_ready_sources(self.core.context)
        while event := self.core.main_loop.pop():
            await self.core.main_loop.dispatcher.dispatch_async(event, self.core.context)
        await asyncio.gather(*list(self.owner.events.tasks))
        await asyncio.sleep(0)

    async def test_real_entry_registration_and_pty(self):
        generation = self.runtime.registry_generation
        self.assertIn("wait_ms", generation.record_for_alias("run_shell").input_schema["properties"])
        self.assertNotIn("shell_session", generation.direct_aliases)
        self.assertIn('read_tool(name="shell_session")', generation.record_for_alias("run_shell").compiled_description)
        started = await self.tool("run_shell", {"cmd": "read -r line; printf '%s' \"$line\"", "tty": True, "wait_ms": 0})
        self.assertTrue(started.ok, started.text)
        sid = started.structured["session_id"]
        write = await self.session(sid, action="write", text="hello\n")
        self.assertTrue(write.ok, write.text)
        final = await self.session(sid, wait_ms=5000)
        self.assertTrue(final.ok, final.text)
        self.assertIn("hello", final.structured["stdout"])
        status = await self.tool("call_tool", {"name": "shell_status", "args": {}})
        self.assertEqual(status.structured["backend"], "native")
        self.assertFalse(status.structured["sessions"])

    async def test_completion_runs_real_continuation_after_committed_result(self):
        call = new_tool_call(name="run_shell", args={"cmd": "sleep .03; printf completion", "wait_ms": 0})
        origin = self.origin(call)
        result = await self.runtime.execute_tool_async(call, turn_id="origin", budget=ToolCallBudget(max_output_chars=1000, preview_chars=500))
        self.assertTrue(result.ok, result.text)
        sid = result.structured["session_id"]
        self.assertFalse(self.owner.sessions[sid]["committed"])
        await self.wait_completion()
        await self.pump()
        self.assertFalse(self.model_inputs)
        await self.commit_origin(origin, call, result)
        before = self.memory.l1_store.turns.get("origin").messages
        finishing = asyncio.create_task(asyncio.Event().wait())
        self.core.state.turn_tasks["post-commit"] = finishing
        await self.pump()
        self.assertFalse(self.model_inputs, "notification must wait for post-turn checkpoint work")
        finishing.cancel()
        await asyncio.gather(finishing, return_exceptions=True)
        self.core.state.turn_tasks.pop("post-commit")
        await self.pump()
        self.assertFalse(self.owner.events.failures)
        self.assertEqual(len(self.model_inputs), 1)
        message = self.model_inputs[0]
        self.assertEqual(message.semantic_kind, "runtime_context_artifact")
        self.assertIn("completion", message.text)
        self.assertEqual(self.replies[0][0], origin.delivery_binding)
        self.assertEqual(self.memory.l1_store.turns.get("origin").messages, before)
        self.assertFalse(any(isinstance(part, ToolResultIR) for part in message.parts))
        self.assertFalse(self.owner.sessions)
        self.assertFalse(self.core.state.active_turns)

    async def test_interrupt_before_l1_delivery_reaps_session(self):
        call = new_tool_call(name="run_shell", args={"cmd": "sleep 60", "wait_ms": 0})
        self.origin(call)
        result = await self.runtime.execute_tool_async(call, turn_id="origin")
        sid = result.structured["session_id"]
        path = Path(self.owner.pending[call.call_id].result["stdout_path"])
        await self.runtime.interrupt_turn("origin")
        self.assertFalse(path.exists())
        self.assertNotIn(sid, self.owner.sessions)
        stale = await self.session(sid)
        self.assertFalse(stale.ok)

    async def test_failed_completion_ack_retry_does_not_repeat_model_turn(self):
        call = new_tool_call(name="run_shell", args={"cmd": "sleep .03; printf done", "wait_ms": 0})
        origin = self.origin(call)
        result = await self.runtime.execute_tool_async(call, turn_id="origin")
        sid = result.structured["session_id"]
        await self.commit_origin(origin, call, result)
        await self.wait_completion()
        acknowledge = self.owner.shell.acknowledge_completion
        async def lose_reply(event):
            await acknowledge(event)
            raise OSError("lost acknowledgement reply")
        self.owner.shell.acknowledge_completion = lose_reply
        await self.pump()
        self.assertEqual(len(self.model_inputs), 1)
        self.assertIn(sid, self.owner.events.failures)
        self.owner.shell.acknowledge_completion = acknowledge
        retried = await self.session(sid, action="retry_notification")
        self.assertTrue(retried.ok, retried.text)
        await self.pump()
        self.assertFalse(self.owner.events.failures)
        self.assertEqual(len(self.model_inputs), 1)
        self.assertFalse(self.core.state.active_turns)

    async def test_interrupt_after_l1_delivery_preserves_background(self):
        call = new_tool_call(name="run_shell", args={"cmd": "sleep 60", "wait_ms": 0})
        origin = self.origin(call)
        result = await self.runtime.execute_tool_async(call, turn_id="origin")
        await self.commit_origin(origin, call, result)
        await self.runtime.interrupt_turn("origin")
        live = await self.session(result.structured["session_id"])
        self.assertTrue(live.ok, live.text)
        self.assertEqual(live.structured["status"], "running")

    async def test_oneshot_files_wait_for_actual_l1_commit(self):
        call = new_tool_call(name="run_shell", args={"cmd": "printf preserved"})
        origin = self.origin(call)
        result = await self.runtime.execute_tool_async(call, turn_id="origin")
        path = Path(self.owner.pending[call.call_id].result["stdout_path"])
        self.assertTrue(path.exists())
        await self.commit_origin(origin, call, result)
        self.assertFalse(path.exists())

    async def test_output_recovery_is_an_actual_indirect_tool(self):
        store = self.runtime.tool_result_pager.store
        def fail(**kwargs):
            raise OSError("test pager failure")
        self.runtime.tool_result_pager.store = fail
        with tempfile.TemporaryDirectory() as root:
            counter = Path(root) / "counter"
            call = new_tool_call(name="run_shell", args={"cmd": f"printf once >> '{counter}'; head -c 8000 /dev/zero"})
            budget = ToolCallBudget(max_output_chars=1000, preview_chars=500)
            result = await self.runtime.execute_tool_async(call, budget=budget)
            self.assertFalse(result.ok)
            self.assertTrue(any(a.arguments.get("name") == "shell_recover_output" for a in result.invocation_result.affordances))
            self.runtime.tool_result_pager.store = store
            recovered = await self.tool("call_tool", {"name": "shell_recover_output", "args": {"call_id": call.call_id}}, budget=budget)
            self.assertTrue(recovered.ok, recovered.text)
            self.assertIn("result_handle", recovered.structured)
            self.assertEqual(counter.read_text(), "once")
            self.assertFalse(self.owner.pending)

    async def test_repeated_output_recovery_retires_all_aliases(self):
        store = self.runtime.tool_result_pager.store
        def fail(**kwargs):
            raise OSError("pager unavailable")
        self.runtime.tool_result_pager.store = fail
        budget = ToolCallBudget(max_output_chars=100, preview_chars=50)
        call = new_tool_call(name="run_shell", args={"cmd": "printf preserved"})
        result = await self.runtime.execute_tool_async(call, budget=budget)
        self.assertFalse(result.ok)
        retry = new_tool_call(name="call_tool", args={"name": "shell_recover_output", "args": {"call_id": call.call_id}})
        result = await self.runtime.execute_tool_async(retry, budget=budget)
        self.assertFalse(result.ok)
        self.runtime.tool_result_pager.store = store
        recovered = await self.tool("call_tool", {"name": "shell_recover_output", "args": {"call_id": retry.call_id}}, budget=budget)
        self.assertTrue(recovered.ok, recovered.text)
        self.assertFalse(self.owner.pending)
        port = self.core.context.module_registry.require("execution").runtime_state_port
        self.assertIn("logical_execution", port.snapshot_state())

    async def test_explicit_session_release_discards_failed_output_handoffs(self):
        started = await self.tool("run_shell", {"cmd": "sleep .03; printf done", "wait_ms": 0})
        sid = started.structured["session_id"]
        await self.wait_completion()
        materialize = self.owner.shell.materialize
        async def fail(event):
            raise OSError("cannot read output")
        self.owner.shell.materialize = fail
        result = await self.session(sid)
        self.assertFalse(result.ok)
        self.assertTrue(self.owner.pending)
        self.owner.shell.materialize = materialize
        discarded = await self.session(sid, action="release")
        self.assertTrue(discarded.ok, discarded.text)
        self.assertFalse(self.owner.pending)
        self.assertFalse(self.owner.sessions)

    async def test_recovery_can_use_materialized_output_after_lost_release_reply(self):
        call = new_tool_call(name="run_shell", args={"cmd": "printf preserved"})
        origin = self.origin(call)
        result = await self.runtime.execute_tool_async(call, turn_id="origin", budget=ToolCallBudget(max_output_chars=1, preview_chars=1))
        self.assertNotIn("stdout_bytes", self.owner.pending[call.call_id].result)
        release = self.owner.shell.release_output
        async def lose_reply(output):
            await release(output)
            raise OSError("release reply lost")
        self.owner.shell.release_output = lose_reply
        with self.assertRaises(OSError):
            await self.commit_origin(origin, call, result)
        self.owner.shell.release_output = release
        self.core.state.active_turns.pop("origin")
        recovered = await self.tool("call_tool", {"name": "shell_recover_output", "args": {"call_id": call.call_id}})
        self.assertTrue(recovered.ok, recovered.text)
        self.assertEqual(recovered.structured["stdout"], "preserved")
        self.assertFalse(self.owner.pending)

    async def test_completion_resumes_user_message_queued_during_model_response(self):
        call = new_tool_call(name="run_shell", args={"cmd": "sleep .03; printf done", "wait_ms": 0})
        origin = self.origin(call)
        result = await self.runtime.execute_tool_async(call, turn_id="origin")
        await self.commit_origin(origin, call, result)
        await self.wait_completion()
        entered, resume = asyncio.Event(), asyncio.Event()
        delegate = self.core._execute_turn_effect_async
        async def pause(continuation, effect):
            if isinstance(effect, LLMRequestEffect) and continuation.opening_event.event_kind == "execution.shell.completed":
                entered.set()
                await resume.wait()
            return await delegate(continuation, effect)
        self.core._execute_turn_effect_async = pause
        pump = asyncio.create_task(self.pump())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            event = EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "queued question"})
            envelope = ChannelEnvelope(event=event, endpoint=origin.delivery_binding.endpoint,
                                       response_handle=origin.delivery_binding.response_handle)
            await self.core.schedule_channel_turn_async(envelope)
            self.assertEqual(len(self.core.state.pending_channel_turns), 1)
            resume.set()
            await asyncio.wait_for(pump, 5)
            # Drain a genuinely scheduled user turn, not another notification pump.
            await asyncio.gather(*list(self.core.state.turn_tasks.values()))
            self.assertFalse(self.core.state.pending_channel_turns)
            self.assertEqual(len(self.model_inputs), 2)
            self.assertIn("queued question", self.model_inputs[-1].text)
        finally:
            resume.set()
            await asyncio.gather(pump, return_exceptions=True)

    async def test_reset_closes_sessions_and_accepts_new_commands(self):
        result = await self.tool("run_shell", {"cmd": "sleep 60", "wait_ms": 0})
        sid = result.structured["session_id"]
        port = self.core.context.module_registry.require("execution").runtime_state_port
        with self.assertRaises(RuntimeError):
            port.snapshot_state()
        await port.reset_state("soft_reset")
        stale = await self.session(sid)
        self.assertFalse(stale.ok)
        new = await self.tool("run_shell", {"cmd": "printf reset"})
        self.assertTrue(new.ok, new.text)
        self.assertEqual(new.structured["stdout"], "reset")

    async def test_production_overlay_shares_native_write_gate(self):
        from pal.bunshin.scoped_execution import _ExecutionOverlay
        record = self.runtime.registry_generation.record_for_alias("run_shell")
        overlay = _ExecutionOverlay(self.runtime, [record.canonical_path], guidance_overrides={})
        self.assertIs(overlay.runtime.shell_owner, self.owner)
        await self.tool("run_shell", {"cmd": "sleep 60", "wait_ms": 0})
        blocked = await overlay.runtime.execute_tool_async(new_tool_call(name="run_shell", args={"cmd": "true"}))
        self.assertFalse(blocked.ok)
        self.assertIn("shell_write_busy", blocked.text)

    async def test_shutdown_quiesces_native_before_execution_snapshot(self):
        await self.tool("run_shell", {"cmd": "sleep 60", "wait_ms": 0})
        await self.runtime.prepare_shutdown_async()
        port = self.core.context.module_registry.require("execution").runtime_state_port
        self.assertIn("logical_execution", port.snapshot_state())
        self.assertTrue(self.owner.closed)

    async def test_snapshot_rejects_foreground_before_session_handoff(self):
        task = asyncio.create_task(self.tool("run_shell", {"cmd": "sleep 60"}))
        try:
            async def started():
                while self.owner._shell is None or not self.owner.shell._foreground:
                    await asyncio.sleep(.01)
            await asyncio.wait_for(started(), 5)
            port = self.core.context.module_registry.require("execution").runtime_state_port
            self.assertFalse(self.owner.sessions)
            with self.assertRaises(RuntimeError):
                port.snapshot_state()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
