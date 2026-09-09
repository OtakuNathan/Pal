from __future__ import annotations

import asyncio
import gc
import os
from pathlib import Path
import shlex
import signal
import sys
import tempfile
import threading
import time
import unittest
import weakref

import _pal_shell_runtime as native
from pal_shell_prototype import ShellRejected, ShellRuntime, TERMINAL


def python(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(source)}"


async def until(predicate, *, seconds=5):
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.01)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A dead grandchild may briefly remain a zombie while init reaps it.
    stat = Path(f"/proc/{pid}/stat")
    return not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"


class NativeShellTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = ShellRuntime()
        self.tmp = tempfile.TemporaryDirectory(prefix="pal-native-shell-")
        self.root = Path(self.tmp.name)

    async def asyncTearDown(self):
        await asyncio.wait_for(self.runtime.close(), 10)
        self.tmp.cleanup()

    async def finish(self, session):
        result = await self.runtime.read(session, wait_ms=5000)
        self.assertIn(result["status"], TERMINAL, result)
        return result

    async def test_oneshot_failure_cwd_and_spawn_error(self):
        result = await self.runtime.run("printf hello; printf bad >&2; exit 7", cwd=str(self.root))
        self.assertEqual((result["session_id"], result["returncode"], result["stdout"], result["stderr"]),
                         (0, 7, "hello", "bad"))
        result = await self.runtime.run("pwd", cwd=str(self.root))
        self.assertEqual(Path(result["stdout"].strip()).resolve(), self.root.resolve())
        result = await self.runtime.run("true", cwd=str(self.root / "missing"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["session_id"], 0)
        self.assertIn("spawn", result["error"])
        self.assertEqual((await self.runtime.run("true"))["returncode"], 0)

    async def test_raw_output_is_complete(self):
        result = await self.runtime.run(python("import os;os.write(1,b'head\\x00\\xff'+b'x'*2200000+b'tail')"))
        self.assertFalse(result["truncated"])
        self.assertEqual(len(result["stdout_bytes"]), 2200010)
        self.assertTrue(result["stdout_bytes"].startswith(b"head\x00\xff"))
        self.assertTrue(result["stdout_bytes"].endswith(b"tail"))
        self.assertEqual(result["stdout_total"], 2200010)
        self.assertIn("\ufffd", result["stdout"])

    async def test_complete_output_preserves_every_byte(self):
        payload = bytes(range(251)) * 10000
        command = python("import os\npayload=bytes(range(251))*10000\n"
                         "for offset in range(0,len(payload),8191): os.write(1,payload[offset:offset+8191])")
        result = await self.runtime.run(command)
        self.assertEqual(result["stdout_total"], len(payload))
        self.assertEqual(result["stdout_bytes"], payload)

    async def test_large_pty_output_is_complete_and_release_removes_files(self):
        result = await self.runtime.run(python("import os;os.write(1,b'x'*1200000+b'MIDDLE'+b'y'*1200000)"),
                                        tty=True, wait_ms=10000, retain_output=True)
        self.assertEqual(result["stdout_bytes"], b"x" * 1200000 + b"MIDDLE" + b"y" * 1200000)
        path = Path(result["stdout_path"])
        self.assertTrue(path.exists())
        await self.runtime.release_output(result)
        self.assertFalse(path.exists())

    async def test_background_wait_keeps_process_and_single_notification(self):
        ready, finish = self.root / "ready", self.root / "finish"
        command = python(f"import pathlib,time\npathlib.Path({str(ready)!r}).touch()\n"
                         f"while not pathlib.Path({str(finish)!r}).exists(): time.sleep(.01)\nprint('done')")
        result = await self.runtime.run(command, wait_ms=0, turn_id="origin")
        sid = result["session_id"]
        self.assertNotEqual(sid, 0)
        await until(ready.exists)
        with self.assertRaisesRegex(ShellRejected, "write_busy"):
            await self.runtime.run("true")
        with self.assertRaisesRegex(ShellRejected, "write_busy"):
            async with self.runtime.tool_admission("local_write"):
                self.fail("write was admitted")
        async with self.runtime.tool_admission("local_read"):
            self.assertEqual((await self.runtime.read(sid))["status"], "running")
        finish.touch()
        await until(lambda: bool(self.runtime._completions))
        events = self.runtime.drain_completions()
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].origin_turn, (await self.runtime.materialize(events[0].result))["stdout"]), ("origin", "done\n"))
        await self.runtime.acknowledge_completion(events[0])
        self.assertEqual(self.runtime.drain_completions(), [])
        self.assertEqual((await self.runtime.run("true"))["returncode"], 0)

    async def test_cancel_wait_does_not_cancel_session(self):
        result = await self.runtime.run("sleep 60", wait_ms=0)
        sid = result["session_id"]
        waiter = asyncio.create_task(self.runtime.read(sid, wait_ms=300000))
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual((await self.runtime.read(sid))["status"], "running")
        await self.runtime.terminate(sid)
        self.assertEqual((await self.finish(sid))["status"], "cancelled")

    async def test_cancel_pipeline_ignoring_term_preserves_output(self):
        pidfile = self.root / "pid"
        source = (f"import os,pathlib,signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                  f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()));print('partial');time.sleep(60)")
        result = await self.runtime.run(python(source) + " | cat", wait_ms=0)
        sid = result["session_id"]
        await until(pidfile.exists)
        pid = int(pidfile.read_text())
        await self.runtime.terminate(sid)
        await self.runtime.terminate(sid)
        result = await self.finish(sid)
        self.assertEqual(result["status"], "cancelled")
        self.assertIn("partial", result["stdout"])
        await until(lambda: not alive(pid))
        self.assertEqual((await self.runtime.run("printf next"))["stdout"], "next")

    async def test_hard_deadline_is_not_wait_budget(self):
        result = await self.runtime.run("printf ready; sleep 60", timeout_ms=100)
        self.assertEqual((result["status"], result["session_id"]), ("timed_out", 0))
        self.assertEqual(result["stdout"], "ready")

    async def test_outer_shell_exit_retires_background_group(self):
        pidfile = self.root / "pid"
        source = f"import os,pathlib,time;pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()));time.sleep(60)"
        command = python(source) + f" & while [ ! -f {shlex.quote(str(pidfile))} ]; do sleep .01; done; exit 0"
        result = await asyncio.wait_for(self.runtime.run(command), 5)
        self.assertEqual(result["returncode"], 0)
        await until(lambda: not alive(int(pidfile.read_text())))

    async def test_pty_controlling_terminal_input_and_resize(self):
        source = ("import os;print(os.isatty(0),os.tcgetpgrp(0)==os.getpgrp(),flush=True);"
                  "s=input();print('received:'+s);print(os.get_terminal_size(0))")
        result = await self.runtime.run(python(source), tty=True)
        sid = result["session_id"]
        self.assertNotEqual(sid, 0)
        self.assertIn("True True", result["stdout"])
        self.assertEqual(result["stderr"], "")
        await self.runtime.resize(sid, 40, 100)
        await self.runtime.write(sid, "hello\n")
        result = await self.finish(sid)
        self.assertIn("received:hello", result["stdout"])
        self.assertIn("columns=100, lines=40", result["stdout"])
        with self.assertRaisesRegex(ShellRejected, "stdin_closed"):
            await self.runtime.write(sid, "again")

    async def test_pty_ctrl_c_is_input_not_session_termination(self):
        pidfile = self.root / "pid"
        source = (f"import os,pathlib,signal,time;signal.signal(signal.SIGINT,lambda *a:print('INT',flush=True));"
                  f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()));time.sleep(60)")
        result = await self.runtime.run(python(source), tty=True, wait_ms=0)
        sid = result["session_id"]
        await until(pidfile.exists)
        await self.runtime.write(sid, "\x03")
        result = await self.runtime.read(sid, wait_ms=100)
        self.assertIn("INT", result["stdout"])
        self.assertEqual(result["status"], "running")
        await self.runtime.terminate(sid)
        await self.finish(sid)

    async def test_foreground_interrupt_and_background_independence(self):
        pidfile = self.root / "pid"
        source = f"import os,pathlib,time;pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()));time.sleep(60)"
        task = asyncio.create_task(self.runtime.run(python(source), turn_id="foreground"))
        await until(pidfile.exists)
        await self.runtime.interrupt_turn("foreground")
        self.assertTrue(task.cancelled())
        await until(lambda: not alive(int(pidfile.read_text())))
        result = await self.runtime.run("sleep 60", wait_ms=0, turn_id="background")
        await self.runtime.interrupt_turn("background")
        self.assertEqual((await self.runtime.read(result["session_id"]))["status"], "running")

    async def test_write_lease_reserves_same_context_both_directions(self):
        async with self.runtime.tool_admission("local_write"):
            with self.assertRaisesRegex(ShellRejected, "write_busy"):
                await self.runtime.run("true")
            with self.assertRaisesRegex(ShellRejected, "write_busy"):
                async with self.runtime.tool_admission("external_write"):
                    self.fail("concurrent write")
        self.assertEqual((await self.runtime.run("true"))["returncode"], 0)

    async def test_contexts_are_independent_and_handles_are_scoped(self):
        other = ShellRuntime()
        try:
            result = await self.runtime.run("sleep 60", wait_ms=0)
            self.assertEqual((await other.run("true"))["returncode"], 0)
            with self.assertRaisesRegex(ShellRejected, "invalid_session"):
                await other.read(result["session_id"])
        finally:
            await other.close()

    async def test_reset_invalidates_handles_and_reaps_child(self):
        result = await self.runtime.run("sleep 60", wait_ms=0)
        self.runtime = await self.runtime.reset()
        with self.assertRaisesRegex(ShellRejected, "invalid_session"):
            await self.runtime.read(result["session_id"])
        self.assertEqual((await self.runtime.run("true"))["returncode"], 0)

    async def test_event_loop_remains_responsive_and_delivery_thread_is_correct(self):
        task = asyncio.create_task(self.runtime.run("sleep .2; printf done"))
        ticks = 0
        while not task.done():
            await asyncio.sleep(.01)
            ticks += 1
        self.assertGreater(ticks, 3)
        self.assertEqual((await task)["stdout"], "done")
        self.assertEqual(self.runtime.delivery_threads, {threading.get_ident()})

    async def test_completion_timeout_race_does_not_duplicate_initial_result(self):
        for wait in (0, 1, 10, 100):
            result = await self.runtime.run("printf race", wait_ms=wait)
            if result["session_id"]:
                terminal = await self.finish(result["session_id"])
                self.assertEqual(terminal["stdout"], "race")
            else:
                self.assertEqual(result["stdout"], "race")
        await asyncio.sleep(0)
        self.assertEqual(self.runtime.drain_completions(), [])

    async def test_cancel_during_session_handoff_reaps_before_return(self):
        await self.runtime.close()
        self.runtime = ShellRuntime(completed_capacity=1)
        original = self.runtime._request
        handed_off = asyncio.Event()
        async def paused(operation, *args, **kwargs):
            result = await original(operation, *args, **kwargs)
            if operation == "acknowledge" and not kwargs.get("cleanup"):
                handed_off.set()
                await asyncio.Event().wait()
            return result
        self.runtime._request = paused
        task = asyncio.create_task(self.runtime.run("sleep 60", wait_ms=0))
        await asyncio.wait_for(handed_off.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        self.runtime._request = original
        self.assertEqual((await self.runtime.run("printf recovered"))["stdout"], "recovered")
        self.assertEqual(self.runtime.drain_completions(), [])

    async def test_callback_cannot_close_its_own_worker(self):
        runtime = native.Runtime(32)
        errors = []
        def callback(event):
            try:
                runtime.close()
            except RuntimeError as exc:
                errors.append(str(exc))
        native.connect(runtime, callback)
        try:
            runtime.run(1, "/bin/bash", "true", "", False, 1000, 0, 0)
            await until(lambda: bool(errors))
            self.assertIn("host thread", errors[0])
        finally:
            await asyncio.to_thread(runtime.close)

    async def test_consumed_snapshots_do_not_accumulate_in_completion_queue(self):
        for _ in range(5):
            result = await self.runtime.run("sleep .01; printf done", wait_ms=0)
            await self.finish(result["session_id"])
            self.assertFalse(self.runtime._completions)

    async def test_inline_budget_boundary_and_file_ownership(self):
        runtime = native.Runtime(32)
        events = []
        native.connect(runtime, events.append)
        try:
            runtime.run(1, "/bin/bash", "printf abc", "", False, 1000, 0, 3)
            await until(lambda: len(events) >= 1)
            inline = events[-1]
            self.assertEqual(inline["stdout_bytes"], b"abc")
            runtime.run(2, "/bin/bash", "printf abcd", "", False, 1000, 0, 3)
            await until(lambda: len(events) >= 2)
            file_result = events[-1]
            self.assertNotIn("stdout_bytes", file_result)
            path = Path(file_result["stdout_path"])
            self.assertEqual(path.read_bytes(), b"abcd")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            runtime.release_output(3, file_result["output_id"])
            await until(lambda: len(events) >= 3)
            self.assertFalse(path.exists())
            retained = Path(inline["stdout_path"])
            self.assertTrue(retained.exists())
        finally:
            await asyncio.to_thread(runtime.close)
        self.assertFalse(retained.exists())

    async def test_callback_references_released_at_close(self):
        runtime = native.Runtime(32)
        class Callback:
            def __call__(self, event):
                pass
        callback = Callback()
        reference = weakref.ref(callback)
        native.connect(runtime, callback)
        del callback
        self.assertIsNotNone(reference())
        await asyncio.to_thread(runtime.close)
        gc.collect()
        self.assertIsNone(reference())

    async def test_failed_notification_keeps_terminal_result(self):
        runtime = native.Runtime(32)
        received = []
        def callback(event):
            if event["request_id"] == 0:
                raise ValueError("intentional notification failure")
            received.append(event)
        native.connect(runtime, callback)
        try:
            runtime.run(1, "/bin/bash", "sleep .05; printf retained", "", False, 0, 0, 0)
            await until(lambda: bool(received))
            sid = received[0]["session_id"]
            await until(lambda: runtime.callback_errors() == 1)
            runtime.read(2, sid, 0)
            await until(lambda: len(received) == 2)
            self.assertEqual(Path(received[1]["stdout_path"]).read_bytes(), b"retained")
        finally:
            await asyncio.to_thread(runtime.close)

    async def test_completed_capacity_does_not_evict_unacknowledged_results(self):
        runtime = native.Runtime(1)
        events = []
        native.connect(runtime, events.append)
        try:
            runtime.run(1, "/bin/bash", "sleep .02", "", False, 0, 0, 0)
            await until(lambda: any(e["request_id"] == 0 for e in events))
            sid = next(e["session_id"] for e in events if e["request_id"] == 1)
            runtime.run(2, "/bin/bash", "true", "", False, 1000, 0, 0)
            await until(lambda: any(e["request_id"] == 2 for e in events))
            self.assertIn("result_capacity", next(e["error"] for e in events if e["request_id"] == 2))
            runtime.acknowledge(3, sid, True)
            runtime.run(4, "/bin/bash", "true", "", False, 1000, 0, 0)
            await until(lambda: any(e["request_id"] == 4 for e in events))
            self.assertEqual(next(e["status"] for e in events if e["request_id"] == 4), "exited")
        finally:
            await asyncio.to_thread(runtime.close)

    async def test_close_cancels_foreground_and_removes_output_files(self):
        before = set(Path("/tmp").glob("pal-native-shell-output-*"))
        task = asyncio.create_task(self.runtime.run("printf partial; sleep 60"))
        await until(lambda: bool(set(Path("/tmp").glob("pal-native-shell-output-*")) - before))
        created = set(Path("/tmp").glob("pal-native-shell-output-*")) - before
        await asyncio.wait_for(self.runtime.close(), 5)
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(all(not path.exists() for path in created))

    async def test_repeated_close_and_no_descriptor_growth(self):
        fdroot = Path("/dev/fd")
        before = len(list(fdroot.iterdir()))
        for _ in range(12):
            runtime = ShellRuntime()
            await runtime.run("true")
            await asyncio.gather(runtime.close(), runtime.close())
        gc.collect()
        self.assertLessEqual(len(list(fdroot.iterdir())), before + 1)


if __name__ == "__main__":
    unittest.main()
