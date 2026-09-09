from __future__ import annotations

import asyncio
import os
import json
import shutil
import subprocess
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pal.bunshin.runner import BunshinRunner, build_slim_bunshin_runtime
from pal.bunshin.scoped_execution import BunshinScopedExecutionRuntime
from pal.bunshin.shell_sessions import BunshinShellSessions
from pal.core import PalCore
from pal.core.main_context import MainContext
from pal.core.turns import TurnContinuation
from pal.execution import register_with_core
from pal.execution.contracts import ToolCallBudget
from pal.execution.native_shell.runtime import NativeExecutionRuntime
from pal.llm.ir import LLMMessageIR, MessageRole
from pal.llm import generation_result_from_values
from pal.memory import MemoryService
from pal.shared import BunshinInvocationPack
from pal.shared.tool_protocol import new_tool_call, ToolResultIR


async def noop(*args, **kwargs):
    pass


class BunshinNativeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runtime = NativeExecutionRuntime(runtime_root=self.root)
        self.core = PalCore(context=MainContext(execution_runtime=self.runtime))
        register_with_core(self.core.context)
        self.core.publish_module_capabilities("execution")
        self.host = BunshinShellSessions(self.runtime)
        self.memory = MemoryService()
        self.memory.begin_l1_turn("role", user_text="test")
        self.scoped = BunshinScopedExecutionRuntime(self.runtime, ["op_exec_shell", "op_file_write"],
            {"invocation_id": "role", "repo_path": str(self.root)})
        self.scoped.begin_tool_result_turn(turn_id="role", scope_key="role")

    async def asyncTearDown(self):
        await self.runtime.shutdown_async()
        self.core.close()
        self.tmp.cleanup()

    async def tool(self, name, args, *, commit=True, budget=None):
        call = new_tool_call(name=name, args=args)
        result = await self.scoped.execute_tool_async(call, turn_id="role", budget=budget)
        if commit:
            await self.scoped.acknowledge_tool_result_async(call.call_id, "role")
        return call, result

    def messages(self):
        return self.memory.active_l1_turn("role").messages

    async def test_role_schema_indirect_controls_and_delivery_ack(self):
        generation = self.scoped.registry_generation
        self.assertIn("wait_ms", generation.record_for_alias("run_shell").input_schema["properties"])
        self.assertIn("tty", generation.record_for_alias("run_shell").input_schema["properties"])
        self.assertNotIn("shell_session", generation.direct_aliases)
        call, result = await self.tool("run_shell", {"cmd": "sleep .02; printf role-done", "wait_ms": 0}, commit=False)
        self.assertTrue(result.ok, result.text)
        sid = result.structured["session_id"]
        self.assertFalse(self.host.owner.sessions[sid]["committed"])
        await self.host.before_model(self.memory, "role", noop, wait_seconds=0)
        self.assertFalse(any("role-done" in m.text for m in self.messages()))
        await self.scoped.acknowledge_tool_result_async(call.call_id, "role")
        await self.host.before_model(self.memory, "role", noop, wait_seconds=2)
        self.assertFalse(self.host.has_work)
        self.assertTrue(any("role-done" in m.text and m.semantic_kind == "runtime_context_artifact" for m in self.messages()))
        self.assertFalse(any(isinstance(p, ToolResultIR) for m in self.messages() for p in m.parts))

    async def test_background_output_pages_and_blocks_mutation_until_delivered(self):
        _, result = await self.tool("run_shell", {"cmd": "sleep .02; printf '%02000d' 1", "wait_ms": 0},
            budget=ToolCallBudget(max_output_chars=400, preview_chars=200))
        self.assertTrue(result.ok, result.text)
        await asyncio.sleep(.05)
        _, blocked = await self.tool("write_file", {"file_path": str(self.root / "must-not-exist"), "content": "bad"})
        self.assertFalse(blocked.ok)
        self.assertIn("shell_write_busy", blocked.llm_text)
        self.assertFalse((self.root / "must-not-exist").exists())
        await self.host.before_model(self.memory, "role", noop, wait_seconds=2)
        self.assertFalse(self.host.has_work)
        notification = next(m.text for m in self.messages() if "result_handle" in m.text)
        metadata = json.loads(notification.split("Tool result metadata: ", 1)[1])
        _, page = await self.tool("read_tool_result", {"result_ref": metadata["result_handle"]["result_ref"], "page_size": 10000})
        self.assertTrue(page.ok, page.llm_text)
        self.assertIn("0" * 1999 + "1", page.llm_text)

    async def test_pty_control_is_available_without_waiting_for_input(self):
        _, result = await self.tool("run_shell", {"cmd": "read -r line; printf '%s' \"$line\"", "tty": True, "wait_ms": 0})
        self.assertTrue(result.ok, result.text)
        await asyncio.wait_for(self.host.before_model(self.memory, "role", noop), .5)
        _, written = await self.tool("call_tool", {"name": "shell_session", "args": {
            "session_id": result.structured["session_id"], "action": "write", "text": "hello-role\n"}})
        self.assertTrue(written.ok, written.text)
        await asyncio.sleep(.05)
        await self.host.before_model(self.memory, "role", noop, wait_seconds=0)
        self.assertFalse(self.host.has_work)

    async def test_no_shell_permission_does_not_grant_session_controls(self):
        scope = BunshinScopedExecutionRuntime(self.runtime, ["op_file_write"], {"invocation_id": "other"})
        self.assertIsNone(scope.registry_generation.record_for_alias("shell_session"))

    async def test_failed_completion_can_recover_without_replaying(self):
        _, result = await self.tool("run_shell", {"cmd": "sleep .02; printf recovery", "wait_ms": 0})
        with patch.object(self.runtime, "_normalize_invocation_result", side_effect=OSError("pager unavailable")):
            await self.host.before_model(self.memory, "role", noop, wait_seconds=2)
        self.assertTrue(self.host.failures)
        call_id = next(key for key in self.host.owner.pending if key.startswith("shell-completion:"))
        _, recovered = await self.tool("call_tool", {"name": "shell_recover_output", "args": {"call_id": call_id}})
        self.assertTrue(recovered.ok, recovered.text)
        self.assertEqual(recovered.structured["stdout"], "recovery")
        self.assertFalse(self.host.has_work)

    async def test_cancel_while_waiting_reaps_before_role_terminal(self):
        _, result = await self.tool("run_shell", {"cmd": "sleep 60", "wait_ms": 0})
        sid = result.structured["session_id"]
        snapshot = await self.host.owner.shell.session_snapshot(sid)
        path = Path(snapshot["stdout_path"])
        async def cancel():
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.host.before_model(self.memory, "role", cancel)
        await BunshinRunner._close_native_shell_work(SimpleNamespace(execution_runtime=self.runtime))
        self.assertFalse(path.exists())
        self.assertFalse(self.host.has_work)

    async def test_pending_work_defers_restart_and_final_reply(self):
        runner = BunshinRunner(runtime_root=self.root, pack=BunshinInvocationPack(invocation_id="role"), bunshin_id="role", run_id="role",
            write_event=noop, read_decision=noop)
        runner._shell_sessions = self.host
        continuation = TurnContinuation(turn_id="role", program=iter(()), correlation_id="role")
        await self.tool("run_shell", {"cmd": "sleep .02; printf done", "wait_ms": 0})
        self.assertFalse(runner._continuation_is_restart_safe(continuation, self.memory))
        self.assertIn("cannot finish", runner._build_bunshin_retry_note(None, [], 2))
        await self.host.before_model(self.memory, "role", noop, wait_seconds=2)
        self.assertTrue(runner._continuation_is_restart_safe(continuation, self.memory))

    async def test_slim_builder_honors_native_backend_and_closes(self):
        with patch.dict(os.environ, {"PAL_SHELL_BACKEND": "native"}):
            bundle = build_slim_bunshin_runtime(self.root / "slim", llm_authority="none")
        try:
            self.assertIsInstance(bundle.execution_runtime, NativeExecutionRuntime)
            result = await bundle.execution_runtime.execute_tool_async(new_tool_call(name="run_shell", args={"cmd": "printf slim"}), turn_id="slim")
            self.assertTrue(result.ok, result.text)
            self.assertEqual(result.structured["stdout"], "slim")
        finally:
            await bundle.close()
        self.assertTrue(bundle.execution_runtime.shell_owner.closed)

    async def test_full_role_loop_observes_background_completion_without_polling(self):
        requests = []
        class Model:
            supports_streaming = False
            async def agenerate(inner, request):
                requests.append(request)
                if len(requests) == 1:
                    return generation_result_from_values(tool_calls=[new_tool_call(name="run_shell", args={
                        "cmd": "sleep .02; printf FULL_ROLE_COMPLETION", "wait_ms": 0})], finish_reason="tool_calls")
                return generation_result_from_values(text="role complete")
        with patch.dict(os.environ, {"PAL_SHELL_BACKEND": "native"}):
            bundle = build_slim_bunshin_runtime(self.root / "loop", llm_authority="none")
        bundle.llm_runtime = Model()
        runner = BunshinRunner(runtime_root=self.root / "loop", pack=BunshinInvocationPack(
            invocation_id="role-loop", instruction="run the command", allowed_capabilities=["op_exec_shell"],
            workspace={"repo_path": str(self.root)}, metadata={"max_tool_rounds": 3}),
            bunshin_id="role-loop", run_id="role-loop", write_event=noop, read_decision=noop)
        try:
            reply = await runner._run_agent_loop(bundle)
            self.assertEqual(reply, "role complete")
            self.assertEqual(len(requests), 2)
            self.assertTrue(any(m.semantic_kind == "runtime_context_artifact" and "FULL_ROLE_COMPLETION" in m.text
                                for m in requests[1].messages))
            self.assertEqual(runner._observed_tool_call_count, 1)
            self.assertFalse(bundle.execution_runtime.shell_owner.has_work)
        finally:
            await bundle.close()

    async def test_manager_cancel_interrupts_foreground_shell_before_terminal(self):
        class Model:
            supports_streaming = False
            async def agenerate(inner, request):
                return generation_result_from_values(tool_calls=[new_tool_call(name="run_shell", args={
                    "cmd": "sleep 60"})], finish_reason="tool_calls")
        with patch.dict(os.environ, {"PAL_SHELL_BACKEND": "native"}):
            bundle = build_slim_bunshin_runtime(self.root / "cancel-loop", llm_authority="none")
        bundle.llm_runtime = Model()
        owner = bundle.execution_runtime.shell_owner
        events = []
        async def control(timeout=None):
            if owner._shell is not None and owner._shell._foreground:
                return {"type": "cancel_requested", "payload": {"reason": "test cancellation"}}
        async def event(value):
            if value.get("event_kind") == "terminal":
                self.assertTrue(owner.closed)
                self.assertFalse(owner.has_work)
            events.append(value)
        runner = BunshinRunner(runtime_root=self.root / "cancel-loop", pack=BunshinInvocationPack(
            invocation_id="cancel-loop", instruction="run the command", allowed_capabilities=["op_exec_shell"],
            workspace={"repo_path": str(self.root)}, metadata={"max_tool_rounds": 3}),
            bunshin_id="cancel-loop", run_id="cancel-loop", write_event=event, read_decision=control,
            runtime_bundle=bundle)
        self.assertEqual(await asyncio.wait_for(runner.run(), 5), 0)
        self.assertTrue(owner.closed)
        self.assertTrue(any(e.get("event_kind") == "terminal" for e in events), events)

    async def test_one_role_cannot_control_another_roles_session(self):
        other = NativeExecutionRuntime()
        core = PalCore(context=MainContext(execution_runtime=other))
        register_with_core(core.context)
        core.publish_module_capabilities("execution")
        try:
            _, result = await self.tool("run_shell", {"cmd": "sleep 60", "wait_ms": 0})
            read = await other.execute_tool_async(new_tool_call(name="call_tool", args={"name": "shell_session", "args": {
                "session_id": result.structured["session_id"]}}), turn_id="other")
            self.assertFalse(read.ok)
            self.assertIn("invalid_session", read.llm_text)
        finally:
            await other.shutdown_async()
            core.close()

    async def test_verification_wrapper_waits_for_terminal_and_records_evidence(self):
        from pal.bunshin.v2.repository import BunshinV2Repository
        from pal.bunshin.v2.submission_drafts import AUTHORING_CONTRACT_VERSION
        from pal.bunshin.v2.work_items import update_checklist_tool_result
        repository = BunshinV2Repository(self.root)
        repository.ensure_schema()
        lease = repository.claim_lease("verify", "role", ttl_seconds=60)
        workspace = {"runtime_root": str(self.root), "repo_path": str(self.root), "invocation_id": "role",
            "review_scratch_dir": str(self.root / "scratch"), "artifact_dir": str(self.root / "artifacts"),
            "artifact_stage_dir": str(self.root / "stage"), "bunshin_v2": {
                "workflow_id": "wf", "invocation_id": "role", "lease_resource_key": "verify",
                "fencing_token": lease.fencing_token, "role": "verifier", "mode": "module",
                "authoring_input_fingerprint": "input", "authoring_contract_version": AUTHORING_CONTRACT_VERSION}}
        initialized = update_checklist_tool_result(new_tool_call(name="op_bunshin_update_checklist", args={
            "plan": [{"step": "verify", "status": "completed"}]}), workspace)
        self.assertTrue(initialized.ok, initialized.text)
        scope = BunshinScopedExecutionRuntime(self.runtime, ["op_bunshin_verification_run_compile_check"], workspace)
        self.assertIsNotNone(scope.registry_generation.record_for_alias("shell_recover_output"))
        original_run = self.host.owner.shell.run
        entered = asyncio.Event()
        proceed = asyncio.Event()
        async def background(*args, **kwargs):
            entered.set()
            await proceed.wait()
            return await original_run(*args, **{**kwargs, "wait_ms": 0})
        call = new_tool_call(name="verification_run_compile_check", args={"name": "compile", "command": "sleep .02; printf verified", "path": "src/test.c"})
        with patch.object(self.host.owner.shell, "run", side_effect=background):
            task = asyncio.create_task(scope.execute_tool_async(call, turn_id="role"))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                _, blocked = await self.tool("run_shell", {"cmd": "printf must-not-run"})
                self.assertFalse(blocked.ok)
                self.assertIn("shell_write_busy", blocked.llm_text)
            finally:
                proceed.set()
            result = await task
        self.assertTrue(result.ok, result.text)
        payload = result.structured.get("payload", result.structured)
        self.assertEqual(payload["case"]["status"], "PASS", payload)
        self.assertEqual(payload["execution"]["stdout"], "verified")
        self.assertTrue(self.host.has_work, "keep output until the outer evidence result is committed")
        await scope.acknowledge_tool_result_async(call.call_id, "role")
        self.assertFalse(self.host.has_work)

    async def test_linux_sandbox_imports_only_extension_and_runs_native_shell(self):
        if sys.platform != "linux" or not shutil.which("bwrap"):
            self.skipTest("Linux bubblewrap integration")
        from pal.bunshin.sandbox import build_sandboxed_runner_invocation
        import _pal_shell_runtime
        endpoint = self.root / "data/bunshin-role/role.sock"
        endpoint.parent.mkdir(parents=True)
        endpoint.write_text("test endpoint")
        repo = self.root / "repo"
        repo.mkdir()
        hidden = self.root / "resident-private.txt"
        hidden.write_text("must stay hidden")
        pack = BunshinInvocationPack(invocation_id="sandbox", workspace={"repo_path": str(repo)}, metadata={
            "sandbox": {"enabled": True, "backend": "bwrap", "run_id": "sandbox", "scratch_dir": str(self.root / "scratch")}})
        script = """import asyncio, os
from pal.core import PalCore
from pal.execution.native_shell.adapter import ShellRuntime
async def main():
    runtime = ShellRuntime()
    try:
        result = await runtime.run('printf SANDBOX_NATIVE_OK')
        print(result['stdout'])
        assert not os.path.exists(os.environ['PAL_TEST_HIDDEN'])
    finally:
        await runtime.close()
asyncio.run(main())
"""
        argv, env = build_sandboxed_runner_invocation(runtime_root=self.root, pack=pack,
            argv=[sys.executable, "-c", script], env={"PATH": os.environ["PATH"], "PAL_SHELL_BACKEND": "native",
                "PYTHONPATH": str(Path(_pal_shell_runtime.__file__).parent), "PAL_TEST_HIDDEN": str(hidden)})
        result = await asyncio.to_thread(subprocess.run, argv, env=env, cwd=repo, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SANDBOX_NATIVE_OK", result.stdout)
