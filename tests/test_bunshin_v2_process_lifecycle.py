from __future__ import annotations

import asyncio
from contextlib import contextmanager
import os
import signal
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

    async def test_terminal_event_stays_active_until_process_owner_cleanup(self) -> None:
        process = SimpleNamespace(pid=123, returncode=0)
        state = BunshinRunState(
            bunshin_id="inv-owner",
            run_id="run-owner",
            pack=BunshinInvocationPack(invocation_id="inv-owner"),
            process=process,
        )
        self.manager.runs[state.run_id] = state
        self.manager.v2_service.repository.role_events.record_worker_event = lambda _event: None
        self.manager.events.queue_event = lambda _event: None

        await self.manager._publish_v2_worker_event(
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
            self.manager._unregister_v2_broker_run(state.run_id, False)

        self.manager._unregister_v2_broker_run(state.run_id, True)
        self.assertEqual(state.status, "completed")
        self.assertFalse(state.summary()["run_active"])
        self.assertTrue(state.ended_at)

    async def test_worker_event_history_is_written_only_for_logged_role_run(self) -> None:
        recorded: list[dict[str, object]] = []
        self.manager.v2_service.repository.role_events.record_worker_event = (
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
            await self.manager._publish_v2_worker_event(
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
        self.manager.v2_service.repository.role_events.record_worker_event = (
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

        await self.manager._publish_v2_worker_event(
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
        self.manager.v2_service.repository.role_events.record_worker_event = lambda _event: None
        self.manager.events.queue_event = lambda _event: None

        self.manager._unregister_v2_broker_run(state.run_id, True)
        self.assertEqual(state.status, "failed")
        await self.manager._publish_v2_worker_event(
            {
                "event_kind": "terminal",
                "run_id": state.run_id,
                "payload": {"status": "completed"},
            }
        )

        self.assertEqual(state.status, "completed")
        self.assertFalse(state.summary()["run_active"])

    async def test_leader_returncode_does_not_make_owned_worker_reusable(self) -> None:
        orchestrator = self.manager.v2_semantic_orchestrator
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
            self.manager._unregister_v2_broker_run(state.run_id, True)

        self.manager.v2_semantic_orchestrator.stop_background_workers = (
            stop_background_workers
        )

        await self.manager.close_all()

        self.assertEqual(calls, ["owner-close"])
        self.assertEqual(state.status, "failed")
