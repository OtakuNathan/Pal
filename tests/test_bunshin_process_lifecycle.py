from __future__ import annotations

import asyncio
from contextlib import contextmanager
import os
import signal
import socket
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pal.bunshin.manager import BunshinManager, BunshinRunState
from pal.bunshin.contracts import LeaseConflict
from pal.bunshin.coroutine_runtime import CoroutineRunSemaphore
from pal.bunshin.workspace_resources import WorkspaceLockRegistry
from pal.bunshin.process_lifecycle import RoleProcessShell, WorkerProcessOwner, WorkerProcessReapError
from pal.shared import BunshinInvocationPack


class WorkerProcessOwnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal-worker-owner-"))
        self.workspace = self.root / "worktree"
        self.workspace.mkdir()
        subprocess.run(
            ["git", "init", "-q"],
            cwd=self.workspace,
            check=True,
        )
        self.locks = WorkspaceLockRegistry()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def owner(
        self,
        *,
        invocation_id: str,
        script: str,
        events: list[str],
    ) -> WorkerProcessOwner:
        def registered(_owner: WorkerProcessOwner) -> None:
            events.append("registered")

        def unregistered(owner: WorkerProcessOwner) -> None:
            self.assertTrue(owner.process_group_reaped)
            self.assertTrue(self.locks.is_held(owner.lock_key))
            self.assertEqual(owner.pid, 0)
            events.append("unregistered")

        return WorkerProcessOwner(
            argv=(sys.executable, "-c", script),
            env=os.environ,
            invocation_id=invocation_id,
            run_id=f"run-{invocation_id}",
            workspace=self.workspace,
            workspace_locks=self.locks,
            on_started=lambda _owner: events.append("started"),
            on_registered=registered,
            on_unregistered=unregistered,
            reap_timeout_seconds=1.0,
        )

    async def test_normal_leader_exit_clears_process_authority_before_unregister(self) -> None:
        events: list[str] = []
        owner = self.owner(
            invocation_id="normal-exit",
            script="pass",
            events=events,
        )

        async with owner:
            self.assertGreater(owner.pid, 0)
            self.assertEqual(await owner.wait(), 0)
            self.assertEqual(owner.pid, 0)

        self.assertEqual(events, ["started", "registered", "unregistered"])
        self.assertFalse(self.locks.is_held(owner.lock_key))

    async def test_worker_exit_terminates_descendants_before_capacity_release(self) -> None:
        for inherit_pipes in (False, True):
            with self.subTest(inherit_pipes=inherit_pipes):
                pid_path = self.root / "descendant.pid"
                pipe_options = "" if inherit_pipes else (
                    ", stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL"
                )
                script = (
                    "import subprocess, sys; from pathlib import Path; "
                    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']"
                    f"{pipe_options}); Path({str(pid_path)!r}).write_text(str(p.pid)); "
                    "print('worker-output', flush=True); sys.exit(7)"
                )
                owner = self.owner(invocation_id="descendant", script=script, events=[])
                semaphore = CoroutineRunSemaphore(1)
                async with RoleProcessShell(owner, semaphore, owner.run_id):
                    try:
                        # Status is independent of stdout/stderr EOF. An orphan
                        # retaining those pipes must not stall natural exit.
                        self.assertEqual(await asyncio.wait_for(owner.wait(), 5), 7)
                        self.assertTrue(owner.process_group_reaped)
                        descendant = int(pid_path.read_text())
                        state = subprocess.run(
                            ["ps", "-p", str(descendant), "-o", "stat="],
                            capture_output=True, text=True, check=False,
                        ).stdout.strip()
                        self.assertTrue(not state or state.startswith("Z"), state)
                        self.assertEqual([line async for line in owner.stdout_lines()], [b"worker-output\n"])
                        self.assertEqual(semaphore.active_count, 1)
                        self.assertTrue(self.locks.is_held(owner.lock_key))
                    finally:
                        # Ensure even a regressed implementation cannot leak
                        # this test's child or hang context-manager cleanup.
                        if pid_path.exists():
                            try:
                                os.kill(int(pid_path.read_text()), signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                self.assertEqual(semaphore.active_count, 0)
                self.assertTrue(owner.resources_released)
                self.assertFalse(self.locks.is_held(owner.lock_key))

    async def test_group_observation_failure_retains_ownership_and_can_retry(self) -> None:
        owner = self.owner(invocation_id="group-fenced", script="pass", events=[])
        semaphore = CoroutineRunSemaphore(1)
        shell = RoleProcessShell(owner, semaphore, owner.run_id)
        await shell.__aenter__()
        # Isolate the group-observation fault from native spawn/exit timing.
        # Under load, a 10 ms deadline cannot also cover supervisor startup.
        await asyncio.wait_for(asyncio.shield(owner._leader_exit_task), 5)
        owner.reap_timeout_seconds = .01
        with patch("pal.bunshin.process_lifecycle._process_group_live", return_value=True):
            with self.assertRaisesRegex(WorkerProcessReapError, "group remains live"):
                await shell.close()
        self.assertFalse(owner.process_group_reaped)
        self.assertTrue(self.locks.is_held(owner.lock_key))
        self.assertEqual(semaphore.active_count, 1)
        await shell.close()
        self.assertTrue(owner.resources_released)
        self.assertEqual(semaphore.active_count, 0)

    async def test_supervisor_owner_disconnect_terminates_running_worker(self) -> None:
        from pal.bunshin import process_group_supervisor

        parent, child = socket.socketpair()
        process = None
        worker_pid = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, process_group_supervisor.__file__, str(child.fileno()),
                sys.executable, "-c",
                "import os, time; print(os.getpid(), flush=True); time.sleep(60)",
                pass_fds=(child.fileno(),), start_new_session=True,
                stdout=asyncio.subprocess.PIPE,
            )
            child.close()
            worker_pid = int(await asyncio.wait_for(process.stdout.readline(), 5))
            parent.close()
            self.assertEqual(await asyncio.wait_for(process.wait(), 5), -signal.SIGKILL)
            state = subprocess.run(
                ["ps", "-p", str(worker_pid), "-o", "stat="],
                capture_output=True, text=True, check=False,
            ).stdout.strip()
            self.assertTrue(not state or state.startswith("Z"), state)
        finally:
            parent.close()
            child.close()
            if process is not None and process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
            if worker_pid is not None:
                try:
                    os.kill(worker_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process is not None:
                await process.wait()

    async def test_supervised_worker_spawn_error_keeps_original_stderr(self) -> None:
        owner = self.owner(invocation_id="bad-executable", script="pass", events=[])
        owner.argv = (str(self.root / "missing-worker"),)
        async with owner:
            self.assertEqual(await asyncio.wait_for(owner.wait(), 5), 125)
        self.assertIn(b"FileNotFoundError", owner.stderr)
        self.assertIn(b"missing-worker", owner.stderr)
        self.assertTrue(owner.resources_released)

    async def test_supervisor_status_write_failure_terminates_descendants(self) -> None:
        from pal.bunshin import process_group_supervisor

        parent, child = socket.socketpair()
        process = None
        descendant = None
        supervisor = str(process_group_supervisor.__file__)
        wrapper = (
            "import runpy, socket, sys\n"
            "def failed_sendall(self, data):\n"
            "    raise BrokenPipeError('lost exit status')\n"
            "socket.socket.sendall = failed_sendall\n"
            "sys.argv = sys.argv[1:]\n"
            "runpy.run_path(sys.argv[0], run_name='__main__')\n"
        )
        worker = (
            "import subprocess, sys; "
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
            "print(p.pid, flush=True)"
        )
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-c", wrapper, supervisor, str(child.fileno()),
                sys.executable, "-c", worker,
                pass_fds=(child.fileno(),), start_new_session=True,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            child.close()
            descendant = int(await asyncio.wait_for(process.stdout.readline(), 5))
            self.assertEqual(await asyncio.wait_for(process.wait(), 5), -signal.SIGKILL)
            self.assertIn(b"BrokenPipeError: lost exit status", await process.stderr.read())
            state = subprocess.run(
                ["ps", "-p", str(descendant), "-o", "stat="],
                capture_output=True, text=True, check=False,
            ).stdout.strip()
            self.assertTrue(not state or state.startswith("Z"), state)
        finally:
            parent.close()
            child.close()
            if process is not None and process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
            if descendant is not None:
                try:
                    os.kill(descendant, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process is not None:
                await process.wait()

    async def test_supervised_worker_preserves_control_and_output_pipes(self) -> None:
        owner = self.owner(
            invocation_id="supervised-control",
            script=(
                "import sys; message = sys.stdin.readline(); "
                "print(message.strip(), flush=True); "
                "print('worker-stderr', file=sys.stderr, flush=True)"
            ),
            events=[],
        )
        async with owner:
            self.assertTrue(await owner.write_control(b'{"kind":"cancel"}\n'))
            self.assertEqual(
                await asyncio.wait_for(anext(owner.stdout_lines()), 5),
                b'{"kind":"cancel"}\n',
            )
            self.assertEqual(await asyncio.wait_for(owner.wait(), 5), 0)
            self.assertFalse(await owner.write_control(b"late control\n"))
        self.assertEqual(owner.stderr, b"worker-stderr\n")

    async def test_close_drains_paused_stdout_before_releasing_capacity(self) -> None:
        owner = self.owner(
            invocation_id="stdout-full",
            script=(
                "import sys, time; sys.stdout.write('x' * 1000000); "
                "sys.stdout.flush(); time.sleep(60)"
            ),
            events=[],
        )
        semaphore = CoroutineRunSemaphore(1)
        shell = RoleProcessShell(owner, semaphore, owner.run_id)
        await shell.__aenter__()
        try:
            # Observe actual asyncio backpressure instead of relying on how
            # long subprocess startup takes on this platform.
            async def backpressure():
                while not owner._stdout._paused:
                    await asyncio.sleep(.01)
            await asyncio.wait_for(backpressure(), 5)
            await asyncio.wait_for(shell.close(), 5)
            self.assertTrue(owner.resources_released)
            self.assertTrue(owner._stdout_drain_task.done())
            self.assertEqual(semaphore.active_count, 0)
        finally:
            if not owner.resources_released and owner._stdout is not None:
                await owner._stdout.read()
            await shell.close()

    async def test_close_coordinates_with_active_stdout_consumer(self) -> None:
        owner = self.owner(
            invocation_id="stdout-consumer",
            script="import time; print('ready', flush=True); time.sleep(60)",
            events=[],
        )
        await owner.__aenter__()
        lines = owner.stdout_lines()
        self.assertEqual(await asyncio.wait_for(anext(lines), 5), b"ready\n")
        pending_read = asyncio.create_task(anext(lines))
        await asyncio.sleep(0)
        try:
            await asyncio.wait_for(owner.close(), 5)
            with self.assertRaises(StopAsyncIteration):
                await pending_read
            self.assertTrue(owner.resources_released)
        finally:
            await owner.close()
            await lines.aclose()

    async def test_worktree_cannot_be_reassigned_until_owner_closes(self) -> None:
        first_events: list[str] = []
        first = self.owner(
            invocation_id="first",
            script="import time; time.sleep(60)",
            events=first_events,
        )
        await first.__aenter__()
        self.addAsyncCleanup(first.close)

        blocked = self.owner(
            invocation_id="blocked",
            script="pass",
            events=[],
        )
        with self.assertRaises(BlockingIOError):
            await blocked.__aenter__()

        await first.__aexit__(None, None, None)
        replacement_events: list[str] = []
        replacement = self.owner(
            invocation_id="replacement",
            script="pass",
            events=replacement_events,
        )
        async with replacement:
            self.assertEqual(await replacement.wait(), 0)
        self.assertEqual(
            replacement_events,
            ["started", "registered", "unregistered"],
        )

    async def test_failed_start_accounting_reaps_and_releases_worktree(self) -> None:
        def reject_registration(_owner: WorkerProcessOwner) -> None:
            raise RuntimeError("registration failed")

        owner = WorkerProcessOwner(
            argv=(sys.executable, "-c", "import time; time.sleep(60)"),
            env=os.environ,
            invocation_id="failed-registration",
            run_id="run-failed-registration",
            workspace=self.workspace,
            workspace_locks=self.locks,
            on_started=lambda _owner: None,
            on_registered=reject_registration,
            on_unregistered=lambda _owner: None,
            reap_timeout_seconds=1.0,
        )

        with self.assertRaisesRegex(RuntimeError, "registration failed"):
            await owner.__aenter__()
        self.assertTrue(owner.process_group_reaped)
        self.assertFalse(self.locks.is_held(owner.lock_key))

    async def test_role_shell_releases_capacity_only_after_owner_cleanup(self) -> None:
        events: list[str] = []
        owner = self.owner(
            invocation_id="shell",
            script="pass",
            events=events,
        )
        semaphore = CoroutineRunSemaphore(1)
        shell = RoleProcessShell(
            owner=owner,
            semaphore=semaphore,
            run_id="run-shell",
        )

        async with shell as running:
            self.assertEqual(semaphore.active_run_ids, frozenset({"run-shell"}))
            self.assertEqual(await running.wait(), 0)

        self.assertTrue(owner.resources_released)
        self.assertEqual(semaphore.active_count, 0)
        self.assertEqual(events, ["started", "registered", "unregistered"])

    async def test_role_shell_consumes_preacquired_permit_without_second_admission(self) -> None:
        events: list[str] = []
        owner = self.owner(
            invocation_id="preacquired-shell",
            script="pass",
            events=events,
        )
        semaphore = CoroutineRunSemaphore(1)
        permit = await semaphore.acquire("admitted-attempt")
        shell = RoleProcessShell(
            owner=owner,
            semaphore=semaphore,
            run_id="worker-run-id",
            preacquired_permit=permit,
        )

        async with shell as running:
            self.assertEqual(
                semaphore.active_run_ids,
                frozenset({"admitted-attempt"}),
            )
            self.assertEqual(await running.wait(), 0)

        self.assertTrue(permit.released)
        self.assertEqual(semaphore.active_count, 0)
        self.assertEqual(events, ["started", "registered", "unregistered"])

    async def test_cancel_signals_current_group_once_and_never_reuses_pid(self) -> None:
        events: list[str] = []
        owner = self.owner(
            invocation_id="cancel-once",
            script="import time; time.sleep(60)",
            events=events,
        )
        await owner.__aenter__()
        pid = owner.pid
        calls: list[tuple[int, int]] = []
        original_killpg = os.killpg

        def killpg(process_group: int, signal_number: int) -> None:
            calls.append((process_group, signal_number))
            original_killpg(process_group, signal_number)

        with patch(
            "pal.bunshin.process_lifecycle.os.killpg",
            side_effect=killpg,
        ):
            await owner.close()
            await owner.close()

        self.assertEqual(calls, [(pid, signal.SIGKILL)])
        self.assertEqual(owner.pid, 0)
        self.assertTrue(owner.resources_released)


    def attach_memory_lease(self, owner, events):
        @contextmanager
        def lease():
            self.assertEqual(owner.pid, 0)
            events.append("memory-acquired")
            try:
                yield
            finally:
                self.assertTrue(owner.process_group_reaped)
                self.assertIsNone(owner._stdin)
                self.assertIsNone(owner._stdout)
                if owner._stderr_task is not None:
                    self.assertTrue(owner._stderr_task.done())
                events.append("memory-released")
        owner.memory_read_lease_factory = lease

    async def test_memory_lease_precedes_spawn_and_outlives_pipes(self):
        events = []
        owner = self.owner(invocation_id="memory-normal", script="pass", events=events)
        self.attach_memory_lease(owner, events)
        async with owner:
            self.assertEqual(events, ["memory-acquired", "started", "registered"])
            await owner.wait()
            self.assertNotIn("memory-released", events)
        self.assertEqual(events, ["memory-acquired", "started", "registered", "memory-released", "unregistered"])
        await owner.close()
        self.assertEqual(events.count("memory-released"), 1)

    async def test_memory_lease_released_on_spawn_failure(self):
        events = []
        owner = self.owner(invocation_id="memory-spawn-fail", script="pass", events=events)
        self.attach_memory_lease(owner, events)
        with patch("pal.bunshin.process_lifecycle.asyncio.create_subprocess_exec", new=AsyncMock(side_effect=OSError("spawn failed"))):
            with self.assertRaisesRegex(OSError, "spawn failed"):
                await owner.__aenter__()
        self.assertEqual(events, ["memory-acquired", "memory-released"])
        self.assertTrue(owner.resources_released)
        self.assertFalse(self.locks.is_held(owner.lock_key))

    async def test_failed_memory_acquisition_does_not_spawn_or_hold_capacity(self):
        events = []
        owner = self.owner(invocation_id="memory-acquire-fail", script="pass", events=events)
        @contextmanager
        def lease():
            raise OSError("lease failed")
            yield
        owner.memory_read_lease_factory = lease
        semaphore = CoroutineRunSemaphore(1)
        shell = RoleProcessShell(owner, semaphore, owner.run_id)
        with patch("pal.bunshin.process_lifecycle.asyncio.create_subprocess_exec", new=AsyncMock()) as spawn:
            with self.assertRaisesRegex(OSError, "lease failed"):
                await shell.__aenter__()
            spawn.assert_not_called()
        self.assertTrue(owner.resources_released)
        self.assertEqual(semaphore.active_count, 0)
        self.assertFalse(self.locks.is_held(owner.lock_key))

    async def test_cancelled_worker_releases_memory_after_reap(self):
        events = []
        owner = self.owner(invocation_id="memory-cancel", script="import time; time.sleep(60)", events=events)
        self.attach_memory_lease(owner, events)
        started = asyncio.Event()
        async def run():
            async with owner:
                started.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(run())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(owner.resources_released)
        self.assertEqual(events, ["memory-acquired", "started", "registered", "memory-released", "unregistered"])

    async def test_partial_start_reap_failure_retains_memory_until_child_exits(self):
        events = []
        exited = asyncio.Event()
        class Process:
            pid = 12345
            returncode = None
            stdin = stdout = stderr = None
            async def wait(self):
                await exited.wait()
                self.returncode = -9
                return self.returncode
        owner = self.owner(invocation_id="memory-reap-fail", script="pass", events=events)
        self.attach_memory_lease(owner, events)
        owner.reap_timeout_seconds = .01
        def fail_start(_owner):
            raise RuntimeError("start callback failed")
        owner.on_started = fail_start
        semaphore = CoroutineRunSemaphore(1)
        shell = RoleProcessShell(owner, semaphore, owner.run_id)
        with patch("pal.bunshin.process_lifecycle.asyncio.create_subprocess_exec", new=AsyncMock(return_value=Process())), patch("pal.bunshin.process_lifecycle.os.killpg") as kill:
            with self.assertRaises(WorkerProcessReapError):
                await shell.__aenter__()
            self.assertEqual(events, ["memory-acquired"])
            self.assertEqual(semaphore.active_count, 1)
            self.assertTrue(self.locks.is_held(owner.lock_key))
            self.assertFalse(owner.resources_released)
            retry = asyncio.create_task(shell.close())
            await asyncio.sleep(.02)
            self.assertFalse(retry.done())
            self.assertEqual(events, ["memory-acquired"])
            self.assertFalse(owner.process_group_reaped)
            exited.set()
            await asyncio.wait_for(retry, 1)
            kill.assert_called_once()
        self.assertEqual(events, ["memory-acquired", "memory-released"])
        self.assertTrue(owner.resources_released)
        self.assertEqual(semaphore.active_count, 0)

    async def test_workspace_acquire_failure_releases_shell_capacity_only(self):
        existing = self.owner(invocation_id="same-owner", script="import time; time.sleep(60)", events=[])
        async with existing:
            blocked = self.owner(invocation_id="same-owner", script="pass", events=[])
            semaphore = CoroutineRunSemaphore(1)
            shell = RoleProcessShell(blocked, semaphore, blocked.run_id)
            with self.assertRaisesRegex(RuntimeError, "already holds a lock"):
                await shell.__aenter__()
            self.assertTrue(blocked.resources_released)
            self.assertEqual(semaphore.active_count, 0)
            self.assertTrue(self.locks.is_held(existing.lock_key))


class ManagerWorkerAccountingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal-manager-owner-"))
        self.manager = BunshinManager(self.runtime_root)

    def tearDown(self) -> None:
        shutil.rmtree(self.runtime_root, ignore_errors=True)

    def process_shell(self) -> RoleProcessShell:
        orchestrator = self.manager.semantic_orchestrator
        pack = BunshinInvocationPack(invocation_id="inv-accounting")
        run_id = "run-accounting"
        owner = WorkerProcessOwner(
            argv=(sys.executable, "-c", "pass"),
            env=os.environ,
            invocation_id=pack.invocation_id,
            run_id=run_id,
            workspace=None,
            workspace_locks=orchestrator.workspace_locks,
            on_reserved=orchestrator.processes.register,
            on_started=lambda _owner: None,
            on_registered=lambda owned: self.manager._register_broker_run(
                run_id, pack.invocation_id, pack, owned
            ),
            on_unregistered=lambda owned: orchestrator.processes.unregister(
                owned, before_remove=self.manager._unregister_broker_run
            ),
        )
        self.manager.workflow_service.repository.role_events.record_worker_event = lambda _event: None
        self.manager._queue_task_delivery_event = lambda *args, **kwargs: None
        self.manager.events.queue_event = lambda _event: None
        return orchestrator.supervisor.process_shell(owner, run_id=run_id)

    async def reply_to_pending_control(self, state: BunshinRunState, kind: str) -> dict:
        if kind == "approval":
            return await self.manager.send_decision(
                {"approval_id": "request", "decision": "accept"}
            )
        return await self.manager.send_clarification(
            {"run_id": state.run_id, "clarification_id": "request", "answers": []}
        )

    async def test_blocked_terminal_releases_manager_accounting_after_cleanup(self) -> None:
        for late_terminal in (False, True):
            with self.subTest(late_terminal=late_terminal):
                shell = self.process_shell()
                async with shell:
                    state = self.manager.runs["run-accounting"]
                    if late_terminal:
                        await shell.owner.wait()
                        await shell.close()
                    await self.manager._publish_worker_event(
                        {
                            "event_kind": "terminal",
                            "run_id": state.run_id,
                            "payload": {"status": "blocked"},
                        }
                    )
                    if not late_terminal:
                        self.assertEqual(state.status, "exiting")
                        self.assertTrue(state.summary()["run_active"])
                        self.assertEqual(self.manager.health()["active_count"], 1)
                        self.assertEqual(self.manager.semantic_orchestrator.active_process_count, 1)
                        await shell.owner.wait()

                self.assertTrue(shell.owner.resources_released)
                self.assertEqual(state.status, "blocked")
                self.assertFalse(state.summary()["run_active"])
                self.assertEqual(self.manager.health()["active_count"], 0)
                self.assertEqual(self.manager.semantic_orchestrator.active_process_count, 0)
                self.assertFalse(self.manager.semantic_orchestrator.processes.contains(state.bunshin_id))
                await self.manager.close_all()

    async def test_control_reply_does_not_reactivate_finishing_worker(self) -> None:
        for kind in ("approval", "clarification"):
            for phase in ("exiting", "completed", "failed"):
                with self.subTest(kind=kind, phase=phase):
                    shell = self.process_shell()
                    async with shell:
                        state = self.manager.runs["run-accounting"]
                        state.status = f"{kind}_pending"
                        setattr(state, f"pending_{kind}", {f"{kind}_id": "request"})

                        async def finish_during_control_write(_run_id, _message):
                            if phase != "failed":
                                await self.manager._publish_worker_event(
                                    {
                                        "event_kind": "terminal",
                                        "run_id": state.run_id,
                                        "payload": {"status": "completed"},
                                    }
                                )
                            if phase != "exiting":
                                await shell.owner.wait()
                                await shell.close()
                            self.assertEqual(state.status, phase)
                            return True

                        self.manager.semantic_orchestrator.send_worker_control = finish_during_control_write
                        reply = await self.reply_to_pending_control(state, kind)
                        self.assertEqual(state.status, phase)
                        self.assertEqual(reply["run"]["run_active"], phase == "exiting")
                        self.assertEqual(getattr(state, f"pending_{kind}"), {})
                        await shell.owner.wait()

                    self.assertTrue(shell.owner.resources_released)
                    self.assertEqual(self.manager.health()["active_count"], 0)
                    self.assertEqual(self.manager.semantic_orchestrator.active_process_count, 0)
                    await self.manager.close_all()

    async def test_control_reply_only_resolves_the_matching_pending_request(self) -> None:
        for kind in ("approval", "clarification"):
            for replacement in (False, True):
                with self.subTest(kind=kind, replacement=replacement):
                    state = BunshinRunState(
                        bunshin_id="inv-control",
                        run_id="run-control",
                        pack=BunshinInvocationPack(invocation_id="inv-control"),
                        status=f"{kind}_pending",
                    )
                    self.manager.runs[state.run_id] = state
                    setattr(state, f"pending_{kind}", {f"{kind}_id": "request"})

                    async def accept_control(_run_id, _message):
                        if replacement:
                            setattr(state, f"pending_{kind}", {f"{kind}_id": "next-request"})
                        return True

                    self.manager.semantic_orchestrator.send_worker_control = accept_control
                    reply = await self.reply_to_pending_control(state, kind)
                    self.assertTrue(reply["ok"])
                    self.assertEqual(state.status, f"{kind}_pending" if replacement else "running")
                    self.assertEqual(
                        getattr(state, f"pending_{kind}"),
                        {f"{kind}_id": "next-request"} if replacement else {},
                    )

    async def test_terminal_event_stays_active_until_process_owner_cleanup(self) -> None:
        process = SimpleNamespace(pid=123, returncode=0)
        state = BunshinRunState(
            bunshin_id="inv-owner",
            run_id="run-owner",
            pack=BunshinInvocationPack(invocation_id="inv-owner"),
            process=process,
        )
        self.manager.runs[state.run_id] = state
        self.manager.workflow_service.repository.role_events.record_worker_event = lambda _event: None
        self.manager.events.queue_event = lambda _event: None

        await self.manager._publish_worker_event(
            {
                "event_kind": "terminal",
                "run_id": state.run_id,
                "payload": {"status": "completed"},
            }
        )

        self.assertEqual(state.status, "exiting")
        self.assertTrue(state.summary()["run_active"])
        self.assertEqual(state.pending_terminal_status, "completed")
        with self.assertRaisesRegex(RuntimeError, "process owner completes cleanup"):
            self.manager._unregister_broker_run(state.run_id, False)

        self.manager._unregister_broker_run(state.run_id, True)
        self.assertEqual(state.status, "completed")
        self.assertFalse(state.summary()["run_active"])
        self.assertTrue(state.ended_at)

    async def test_worker_event_history_is_written_only_for_logged_role_run(self) -> None:
        recorded: list[dict[str, object]] = []
        self.manager.workflow_service.repository.role_events.record_worker_event = (
            lambda event: recorded.append(dict(event))
        )
        self.manager.events.queue_event = lambda _event: None
        quiet = BunshinRunState(
            bunshin_id="inv-quiet",
            run_id="run-quiet",
            pack=BunshinInvocationPack(
                invocation_id="inv-quiet",
                metadata={"prompt_log_enabled": False},
            ),
        )
        logged = BunshinRunState(
            bunshin_id="inv-logged",
            run_id="run-logged",
            pack=BunshinInvocationPack(
                invocation_id="inv-logged",
                metadata={"prompt_log_enabled": True},
            ),
        )
        self.manager.runs[quiet.run_id] = quiet
        self.manager.runs[logged.run_id] = logged

        for state in (quiet, logged):
            await self.manager._publish_worker_event(
                {
                    "event_kind": "progress",
                    "run_id": state.run_id,
                    "invocation_id": state.bunshin_id,
                    "payload": {"phase": "llm_round_started", "round": 1},
                }
            )

        self.assertEqual(
            [item["invocation_id"] for item in recorded],
            ["inv-logged"],
        )

    async def test_quiet_round_completion_persists_only_efficiency_fields(
        self,
    ) -> None:
        recorded: list[dict[str, object]] = []
        self.manager.workflow_service.repository.role_events.record_worker_event = (
            lambda event: recorded.append(dict(event))
        )
        self.manager.events.queue_event = lambda _event: None
        state = BunshinRunState(
            bunshin_id="inv-quiet-round",
            run_id="run-quiet-round",
            pack=BunshinInvocationPack(
                invocation_id="inv-quiet-round",
                metadata={"prompt_log_enabled": False},
            ),
        )
        self.manager.runs[state.run_id] = state

        await self.manager._publish_worker_event(
            {
                "event_kind": "progress",
                "run_id": state.run_id,
                "invocation_id": state.bunshin_id,
                "payload": {
                    "phase": "llm_round_completed",
                    "round": 7,
                    "tool_call_count": 3,
                    "text_preview": "must not persist",
                    "tool_calls": [{"tool_name": "read_file"}],
                    "control_route": {"endpoint_id": "private"},
                },
            }
        )

        self.assertEqual(len(recorded), 1)
        self.assertEqual(
            recorded[0]["payload"],
            {
                "phase": "llm_round_completed",
                "round": 7,
                "tool_call_count": 3,
            },
        )

    async def test_late_terminal_receipt_after_reap_preserves_terminal_status(self) -> None:
        process = SimpleNamespace(pid=124, returncode=0)
        state = BunshinRunState(
            bunshin_id="inv-late-terminal",
            run_id="run-late-terminal",
            pack=BunshinInvocationPack(invocation_id="inv-late-terminal"),
            process=process,
        )
        self.manager.runs[state.run_id] = state
        self.manager.workflow_service.repository.role_events.record_worker_event = lambda _event: None
        self.manager.events.queue_event = lambda _event: None

        self.manager._unregister_broker_run(state.run_id, True)
        self.assertEqual(state.status, "failed")
        await self.manager._publish_worker_event(
            {
                "event_kind": "terminal",
                "run_id": state.run_id,
                "payload": {"status": "completed"},
            }
        )

        self.assertEqual(state.status, "completed")
        self.assertFalse(state.summary()["run_active"])

    async def test_leader_returncode_does_not_make_owned_worker_reusable(self) -> None:
        orchestrator = self.manager.semantic_orchestrator
        orchestrator.processes.register(SimpleNamespace(
            invocation_id="inv-owned", run_id="run-owned",
            process=SimpleNamespace(returncode=0)
        ))
        orchestrator.repository.leases.assert_fencing_token = (
            lambda _resource, _owner, _token: None
        )

        with self.assertRaisesRegex(LeaseConflict, "already active"):
            await orchestrator.components.role_leases.reuse_or_retire_effect_lease(
                resource_key="node:owned:writer",
                owner_id="inv-owned",
                fencing_token=1,
                worker_label="owned worker",
            )

    async def test_close_all_delegates_process_shutdown_to_process_owner(self) -> None:
        class Leader:
            returncode: int | None = None

            def terminate(self) -> None:
                raise AssertionError("Manager must not terminate a raw leader")

        process = Leader()
        state = BunshinRunState(
            bunshin_id="inv-close",
            run_id="run-close",
            pack=BunshinInvocationPack(invocation_id="inv-close"),
            process=process,
        )
        self.manager.runs[state.run_id] = state
        calls: list[str] = []

        async def stop_background_workers(*, timeout_seconds: float) -> None:
            self.assertGreaterEqual(timeout_seconds, 0)
            calls.append("owner-close")
            process.returncode = -15
            self.manager._unregister_broker_run(state.run_id, True)

        self.manager.semantic_orchestrator.stop_background_workers = (
            stop_background_workers
        )

        await self.manager.close_all()

        self.assertEqual(calls, ["owner-close"])
        self.assertEqual(state.status, "failed")
