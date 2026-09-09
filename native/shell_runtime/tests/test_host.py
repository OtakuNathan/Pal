from __future__ import annotations

import asyncio
import json
import shlex
import sys
from dataclasses import replace
import tempfile
from pathlib import Path
import unittest

from pal.llm.ir import LLMMessageIR, MessageRole
from pal.shared.tool_protocol import ToolResultIR, new_tool_call
from pal.execution.tool_facade import InvocationMode
from pal.execution.contracts import ToolCallBudget
from pal_shell_host import PrototypeHost, PrototypeExecutionRuntime


class HostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.observed = []
        async def consume(core, event):
            self.observed.append(event)
        self.host = PrototypeHost(consume)
        self.runtime = self.host.core.context.execution_runtime

    async def asyncTearDown(self):
        await self.host.close()

    async def wait_for_completion(self):
        # Synchronize with native delivery, not a machine-dependent sleep.
        async def wait():
            while not self.host.shell._completions:
                await asyncio.sleep(.01)
        await asyncio.wait_for(wait(), timeout=5)

    async def tool(self, alias, **args):
        return await self.runtime.execute_tool_async(new_tool_call(name=alias, args=args), turn_id="origin")

    async def session(self, sid, action="read", **args):
        return await self.tool("call_tool", name="shell_session", args={"session_id": sid, "action": action, **args})

    async def test_session_schema_is_discovered_from_entry_without_resident_expansion(self):
        generation = self.runtime.registry_generation
        self.assertNotIn("shell_session", generation.direct_aliases)
        self.assertIn("shell_session", generation.indirect_aliases)
        description = generation.record_for_alias("prototype_run_shell").compiled_description
        self.assertIn('read_tool(name="shell_session")', description)
        self.assertIn('call_tool(name="shell_session", args=...)', description)
        schema = await self.tool("read_tool", name="shell_session")
        self.assertTrue(schema.ok, schema.text)
        self.assertIn("terminate", str(schema.structured))
        direct = await self.tool("shell_session", session_id=1)
        self.assertFalse(direct.ok)
        oneshot = await self.tool("prototype_run_shell", cmd="true")
        self.assertFalse(oneshot.invocation_result.affordances)

    async def test_live_session_controls_survive_output_paging(self):
        with tempfile.TemporaryDirectory() as root:
            ready = Path(root) / "ready"
            started = await self.runtime.execute_tool_async(new_tool_call(name="prototype_run_shell", args={
                "cmd": f"head -c 20000 /dev/zero; touch {shlex.quote(str(ready))}; sleep 60", "wait_ms": 0}),
                turn_id="origin", budget=ToolCallBudget(max_output_chars=10, preview_chars=10))
            self.assertTrue(started.ok, started.text)
            self.assertIn("result_handle", started.structured)
            control = next(a for a in started.invocation_result.affordances if a.tool == "call_tool")
            sid = control.arguments["args"]["session_id"]
            async def wait():
                while not ready.exists():
                    await asyncio.sleep(.01)
            await asyncio.wait_for(wait(), 5)
            result = await self.runtime.execute_tool_async(new_tool_call(name="call_tool", args={
                "name": "shell_session", "args": {"session_id": sid, "action": "read"}}),
                turn_id="origin", budget=ToolCallBudget(max_output_chars=1000, preview_chars=500))
            self.assertTrue(result.ok, result.text)
            self.assertIn("result_handle", result.structured)
            actions = result.invocation_result.affordances
            self.assertTrue(any(a.tool == "read_tool_result" for a in actions))
            self.assertTrue(any(a.tool == "call_tool" and a.arguments["args"] == {"session_id": sid, "action": "terminate"} for a in actions))
            self.assertFalse(any(a.tool == "read_tool" for a in actions), "non-PTY must not suggest input")
            read = next(a for a in actions if a.tool == "call_tool" and a.arguments["args"]["action"] == "read")
            self.assertEqual(read.arguments["args"]["wait_ms"], 300000)
            stopped = await self.session(sid, "terminate")
            self.assertTrue(stopped.ok, stopped.text)
            if stopped.structured["status"] == "terminating":
                self.assertEqual(len(stopped.invocation_result.affordances), 1)
                final = await self.session(sid, wait_ms=5000)
                self.assertTrue(final.ok, final.text)
                self.assertEqual(final.structured["status"], "cancelled")

    async def test_interactive_input_and_resize_via_indirect_tool(self):
        started = await self.tool("prototype_run_shell", cmd="read -r line; printf 'received:%s' \"$line\"", tty=True, wait_ms=0)
        sid = started.structured["session_id"]
        self.assertTrue(any(a.tool == "read_tool" for a in started.invocation_result.affordances))
        resized = await self.session(sid, "resize", rows=32, columns=100)
        self.assertTrue(resized.ok, resized.text)
        written = await self.session(sid, "write", text="hello\n")
        self.assertTrue(written.ok, written.text)
        self.assertEqual(written.structured["status"], "input_accepted")
        final = await self.session(sid, wait_ms=5000)
        self.assertTrue(final.ok, final.text)
        self.assertIn("received:hello", final.structured["stdout"])
        self.assertFalse(final.invocation_result.affordances)
        stale = await self.session(sid)
        self.assertFalse(stale.ok)
        self.assertIn("invalid_session", stale.text)
        self.assertIn("Do not automatically rerun", stale.text)

    async def test_session_preconditions_reject_without_stopping_live_command(self):
        started = await self.tool("prototype_run_shell", cmd="sleep 60", wait_ms=0)
        sid = started.structured["session_id"]
        for args in ({"session_id": 0}, {"session_id": sid, "action": "write"},
                     {"session_id": sid, "action": "read", "text": "ignored?"},
                     {"session_id": sid, "action": "resize", "rows": 0, "columns": 80},
                     {"session_id": sid, "wait_ms": 300001}):
            rejected = await self.tool("call_tool", name="shell_session", args=args)
            self.assertFalse(rejected.ok)
        for action, args in (("write", {"text": "no stdin"}), ("release", {})):
            rejected = await self.session(sid, action, **args)
            self.assertFalse(rejected.ok)
        live = await self.session(sid)
        self.assertTrue(live.ok, live.text)
        self.assertEqual(live.structured["status"], "running")

    async def test_terminal_tool_read_consumes_already_queued_completion(self):
        started = await self.tool("prototype_run_shell", cmd="sleep .03; printf completed", wait_ms=0)
        sid = started.structured["session_id"]
        self.host.core.state.active_turn_id = "busy"
        self.host.core.state.active_turns["busy"] = object()
        await self.wait_for_completion()
        await self.host.pump()
        self.assertTrue(self.host.pending)
        final = await self.session(sid)
        self.assertTrue(final.ok, final.text)
        self.assertEqual(final.structured["stdout"], "completed")
        self.host.core.state.active_turn_id = None
        self.host.core.state.active_turns.pop("busy")
        await self.host.pump()
        self.assertFalse(self.observed)
        self.assertFalse(self.host.pending)
        self.assertFalse(self.host.failures)

    async def test_terminal_session_pager_failure_retains_unconsumed_output(self):
        started = await self.tool("prototype_run_shell", cmd="sleep .03; head -c 5000 /dev/zero", wait_ms=0)
        sid = started.structured["session_id"]
        await self.wait_for_completion()
        store = self.runtime.tool_result_pager.store
        def fail(**kwargs):
            raise OSError("pager unavailable")
        self.runtime.tool_result_pager.store = fail
        call = new_tool_call(name="call_tool", args={"name": "shell_session", "args": {"session_id": sid}})
        budget = ToolCallBudget(max_output_chars=1000, preview_chars=500)
        result = await self.runtime.execute_tool_async(call, budget=budget, turn_id="origin")
        self.assertFalse(result.ok)
        self.assertNotIn(sid, self.host.shell._consumed)
        path = Path(self.runtime.pending_outputs[call.call_id][2]["stdout_path"])
        self.assertTrue(path.exists())
        self.runtime.tool_result_pager.store = store
        recovered = await self.runtime.retry_output(call.call_id, budget=budget, turn_id="origin")
        self.assertTrue(recovered.ok, recovered.text)
        self.assertFalse(path.exists())
        await self.host.pump()
        self.assertFalse(self.observed)

    async def test_explicit_release_of_completed_session_prevents_completion_delivery(self):
        started = await self.tool("prototype_run_shell", cmd="sleep .03", wait_ms=0)
        sid = started.structured["session_id"]
        await self.wait_for_completion()
        result = await self.session(sid, "release")
        self.assertTrue(result.ok, result.text)
        self.assertEqual(result.structured["status"], "released")
        self.assertFalse(result.invocation_result.affordances)
        await self.host.pump()
        self.assertFalse(self.observed)

    async def test_compiled_native_tool_and_original_tool_are_separate(self):
        names = self.runtime.registry_generation.direct_aliases
        self.assertIn("run_shell", names)
        self.assertIn("prototype_run_shell", names)
        result = await self.tool("prototype_run_shell", cmd="printf native")
        self.assertTrue(result.ok, result.text)
        self.assertEqual(result.structured["stdout"], "native")
        self.assertEqual(result.structured["session_id"], 0)
        original = await self.tool("run_shell", cmd="printf original")
        self.assertTrue(original.ok, original.text)
        self.assertEqual(original.structured["stdout"], "original")

    async def test_real_file_write_is_rejected_while_native_shell_runs(self):
        result = await self.tool("prototype_run_shell", cmd="sleep 60", wait_ms=0)
        self.assertTrue(result.ok, result.text)
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "blocked.txt")
            spec = self.runtime.registry_generation.record_for_alias("write_file")
            # Match actual registry schema; use a valid write so rejection proves admission.
            fields = spec.input_schema["properties"]
            args = {("file_path" if "file_path" in fields else "path"): path, "content": "bad"}
            write = await self.tool("write_file", **args)
            self.assertFalse(write.ok)
            self.assertIn("shell_write_busy", str(write.structured))
            self.assertFalse(Path(path).exists())
        blocked_shell = await self.tool("run_shell", cmd="printf should-not-run")
        self.assertFalse(blocked_shell.ok)
        self.assertIn("shell_write_busy", str(blocked_shell.structured))

    async def test_indirect_registry_projection_cannot_bypass_write_gate(self):
        # Exercise call_tool's recursive resolution with an indirect projection
        # of a real binding, the same immutable-record mechanism used by roles.
        generation = self.runtime.registry_generation
        descriptor = generation.record_for_alias("run_shell").binding.descriptor
        descriptor = replace(descriptor, execution=descriptor.execution.model_copy(
            update={"invocation_mode": InvocationMode.INDIRECT}))
        binding = replace(generation.record_for_alias("run_shell").binding, descriptor=descriptor)
        # Projection construction uses the public registry compiler through the
        # runtime's mount machinery in production; here replace one immutable view.
        record = replace(generation.record_for_alias("run_shell"), execution=descriptor.execution,
                         binding=binding)
        direct = dict(generation.direct_aliases); direct.pop("run_shell")
        indirect = dict(generation.indirect_aliases); indirect["run_shell"] = record
        projected = replace(generation, direct_aliases=direct, indirect_aliases=indirect)
        self.runtime._registry_generation = projected
        await self.host.shell.run("sleep 60", wait_ms=0)
        result = await self.tool("call_tool", name="run_shell", args={"cmd": "true"})
        self.assertFalse(result.ok)
        self.assertIn("shell_write_busy", str(result.structured))

    async def test_completion_is_separate_from_closed_tool_protocol(self):
        call = new_tool_call(name="prototype_run_shell", args={"cmd": "sleep .05; printf done", "wait_ms": 0})
        memory = self.host.memory
        memory.begin_l1_turn("origin", user_text="build")
        memory.upsert_l1_assistant("origin", LLMMessageIR(role=MessageRole.ASSISTANT, parts=(call,)))
        result = await self.runtime.execute_tool_async(call, turn_id="origin")
        self.assertTrue(result.ok, result.text)
        memory.append_l1_tool_result("origin", ToolResultIR(call_id=call.call_id, name=call.name, content=result.text))
        settled = memory.settle_l1_turn("origin")
        before = settled.messages
        # A different active turn prevents a completion from being dispatched.
        self.host.core.state.active_turn_id = "busy"
        self.host.core.state.active_turns["busy"] = object()
        await self.wait_for_completion()
        await self.host.pump()
        self.assertEqual(self.observed, [])
        self.host.core.state.active_turn_id = None
        self.host.core.state.active_turns.pop("busy")
        await self.host.pump()
        self.assertEqual(len(self.observed), 1)
        self.assertEqual(self.observed[0].origin_turn, "origin")
        self.assertEqual(memory.l1_store.turns.get("origin").messages, before)
        completion_turn = memory.l1_store.turns.get(f"native-shell-completion:{result.structured['session_id']}")
        self.assertEqual(completion_turn.messages[0].semantic_kind, "runtime_context_artifact")
        self.assertFalse(any(isinstance(part, ToolResultIR) for message in completion_turn.messages for part in message.parts))
        await self.host.pump()
        self.assertEqual(len(self.observed), 1)

    async def test_full_output_uses_existing_pager_and_cleans_files_after_store(self):
        payload = "x" * 1200000 + "MIDDLE-IS-RETAINED" + "y" * 1200000
        source = "import sys;sys.stdout.write('x'*1200000+'MIDDLE-IS-RETAINED'+'y'*1200000)"
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"
        paths = []
        original = self.host.shell.materialize
        async def capture(event):
            self.assertNotIn("stdout_bytes", event)
            paths.append(Path(event["stdout_path"]))
            self.assertTrue(paths[-1].exists())
            return await original(event)
        self.host.shell.materialize = capture
        call = new_tool_call(name="prototype_run_shell", args={"cmd": command})
        budget = ToolCallBudget(max_output_chars=4096, preview_chars=4096)
        result = await self.runtime.execute_tool_async(call, budget=budget, turn_id="large")
        self.assertTrue(result.ok, result.text)
        handle = result.structured["result_handle"]
        self.assertTrue(paths and all(not path.exists() for path in paths))
        rendered = "".join(self.runtime.read_tool_result_page(
            result_ref=handle["result_ref"], page=page, turn_id="large").content
            for page in range(1, handle["page_count"] + 1))
        self.assertEqual(json.loads(rendered)["stdout"], payload)
        middle_page = rendered.index("MIDDLE-IS-RETAINED") // handle["page_size"] + 1
        later = await self.runtime.execute_tool_async(new_tool_call(name="read_tool_result", args={
            "result_ref": handle["result_ref"], "page": middle_page}), turn_id="large")
        self.assertTrue(later.ok, later.text)
        self.assertIn("MIDDLE-IS-RETAINED", later.llm_text)
        self.assertNotIn("stdout_path", rendered)

    async def test_pager_failure_retains_file_and_retry_does_not_repeat_command(self):
        with tempfile.TemporaryDirectory() as root:
            counter = Path(root) / "counter"
            command = f"printf once >> {shlex.quote(str(counter))}; head -c 5000 /dev/zero"
            call = new_tool_call(name="prototype_run_shell", args={"cmd": command})
            budget = ToolCallBudget(max_output_chars=1000, preview_chars=500)
            original = self.runtime.tool_result_pager.store
            def fail(**kwargs):
                raise OSError("pager unavailable")
            self.runtime.tool_result_pager.store = fail
            result = await self.runtime.execute_tool_async(call, budget=budget, turn_id="retry")
            self.assertFalse(result.ok)
            output = self.runtime.pending_outputs[call.call_id][2]
            path = Path(output["stdout_path"])
            self.assertTrue(path.exists())
            self.runtime.tool_result_pager.store = original
            recovered = await self.runtime.retry_output(call.call_id, budget=budget, turn_id="retry")
            self.assertTrue(recovered.ok, recovered.text)
            self.assertFalse(path.exists())
            self.assertEqual(counter.read_text(), "once")

    async def test_file_read_failure_can_retry_handoff_without_reexecution(self):
        call = new_tool_call(name="prototype_run_shell", args={"cmd": "printf preserved"})
        original = self.host.shell.materialize
        async def fail(event):
            raise OSError("read unavailable")
        self.host.shell.materialize = fail
        budget = ToolCallBudget(max_output_chars=4, preview_chars=4)
        result = await self.runtime.execute_tool_async(call, budget=budget, turn_id="read-retry")
        self.assertFalse(result.ok)
        output = self.runtime.pending_outputs[call.call_id][2]
        path = Path(output["stdout_path"])
        self.assertEqual(path.read_bytes(), b"preserved")
        self.host.shell.materialize = original
        recovered = await self.runtime.retry_output(call.call_id, budget=budget, turn_id="read-retry")
        self.assertTrue(recovered.ok, recovered.text)
        self.assertFalse(path.exists())

    async def test_background_completion_inherits_budget_and_releases_file(self):
        budget = ToolCallBudget(max_output_chars=1200, preview_chars=600)
        result = await self.runtime.execute_tool_async(new_tool_call(name="prototype_run_shell", args={
            "cmd": "sleep .05; head -c 20000 /dev/zero; printf END", "wait_ms": 0}),
            budget=budget, turn_id="background")
        sid = result.structured["session_id"]
        await self.wait_for_completion()
        await self.host.pump()
        self.assertFalse(self.host.failures, self.host.failures)
        observed = self.observed[0]
        self.assertEqual(len(observed.result["stdout_bytes"]), 20003)
        self.assertFalse(Path(observed.result["stdout_path"]).exists())
        turn = f"native-shell-completion:{sid}"
        message = self.host.memory.l1_store.turns.get(turn).messages[0]
        handle = message.metadata["result_handle"]
        self.assertEqual(handle["page_size"], 600)
        # Full rendered JSON remains available after native session/file retirement.
        rendered = "".join(self.runtime.read_tool_result_page(result_ref=handle["result_ref"],
            page=p, turn_id=turn).content for p in range(1, handle["page_count"] + 1))
        self.assertTrue(json.loads(rendered)["stdout"].endswith("END"))

    async def test_bunshin_overlay_uses_same_native_write_owner(self):
        from pal.bunshin.scoped_execution import _ExecutionOverlay
        original = self.runtime.registry_generation.record_for_alias("run_shell")
        overlay = _ExecutionOverlay(self.runtime, [original.canonical_path], guidance_overrides={})
        guarded = PrototypeExecutionRuntime.project_view(overlay.runtime, self.host.shell)
        await self.host.shell.run("sleep 60", wait_ms=0)
        result = await guarded.execute_tool_async(new_tool_call(name="run_shell", args={"cmd": "true"}))
        self.assertFalse(result.ok)
        self.assertIn("shell_write_busy", str(result.structured))

    async def test_acknowledgement_retry_does_not_repeat_consumed_observation(self):
        original = self.host.shell.acknowledge_completion
        async def fail(event):
            raise RuntimeError("ack failed")
        self.host.shell.acknowledge_completion = fail
        result = await self.tool("prototype_run_shell", cmd="sleep .03", wait_ms=0)
        await self.wait_for_completion()
        await self.host.pump()
        sid = result.structured["session_id"]
        self.assertEqual(self.host.failures[sid], "ack failed")
        self.assertEqual(len(self.observed), 1)
        self.host.shell.acknowledge_completion = original
        self.host.retry_completion(sid)
        await self.host.pump()
        self.assertEqual(len(self.observed), 1)
        self.assertEqual(len(self.host.pending), 0)

    async def test_lost_reply_after_file_release_can_retry_acknowledgement(self):
        original = self.host.shell.acknowledge_completion
        async def lose_reply(event):
            await original(event)
            raise OSError("release reply lost")
        self.host.shell.acknowledge_completion = lose_reply
        result = await self.tool("prototype_run_shell", cmd="sleep .03; printf done", wait_ms=0)
        await self.wait_for_completion()
        await self.host.pump()
        sid = result.structured["session_id"]
        self.assertEqual(self.host.failures[sid], "release reply lost")
        self.assertEqual(len(self.observed), 1)
        self.assertFalse(Path(self.observed[0].result["stdout_path"]).exists())
        self.host.shell.acknowledge_completion = original
        self.host.retry_completion(sid)
        await self.host.pump()
        self.assertFalse(self.host.failures)
        self.assertFalse(self.host.pending)
        self.assertEqual(len(self.observed), 1)

    async def test_failed_consumer_keeps_event_without_automatic_retry(self):
        async def fail(core, event):
            raise RuntimeError("consumer failed")
        self.host.on_completion = fail
        result = await self.tool("prototype_run_shell", cmd="sleep .03", wait_ms=0)
        await self.wait_for_completion()
        await self.host.pump()
        sid = result.structured["session_id"]
        self.assertEqual(self.host.failures[sid], "consumer failed")
        self.assertEqual(len(self.host.pending), 1)
        async def recovered(core, event):
            self.observed.append(event)
        self.host.on_completion = recovered
        self.host.retry_completion(sid)
        await self.host.pump()
        self.assertEqual(len(self.observed), 1)
        self.assertEqual(len(self.host.pending), 0)


if __name__ == "__main__":
    unittest.main()
