from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pal.minion.manager import MinionManager, MinionRunState
from pal.minion.v2.contracts import LeaseConflict
from pal.minion.v2.execution import WorkspaceLockRegistry
from pal.minion.v2.process_lifecycle import WorkerProcessOwner
from pal.shared import MinionInvocationPack


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
            with self.assertRaises(ProcessLookupError):
                os.killpg(owner.process_group_id, 0)
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

    async def test_normal_leader_exit_reaps_descendants_before_unregister(self) -> None:
        child_pid_path = self.root / "child.pid"
        script = (
            "import pathlib, subprocess; "
            "child=subprocess.Popen(['sleep','60'], stdout=subprocess.DEVNULL, "
            "stderr=subprocess.DEVNULL); "
            f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid))"
        )
        events: list[str] = []
        owner = self.owner(
            invocation_id="normal-exit",
            script=script,
            events=events,
        )

        async with owner:
            process = owner.process
            self.assertIsNotNone(process)
            await process.wait()

        self.assertEqual(events, ["started", "registered", "unregistered"])
        self.assertFalse(self.locks.is_held(owner.lock_key))
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

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
            process = replacement.process
            self.assertIsNotNone(process)
            await process.wait()
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


class ManagerWorkerAccountingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal-manager-owner-"))
        self.manager = MinionManager(self.runtime_root)

    def tearDown(self) -> None:
        shutil.rmtree(self.runtime_root, ignore_errors=True)

    async def test_terminal_event_stays_active_until_process_group_reap(self) -> None:
        process = SimpleNamespace(pid=123, returncode=0)
        state = MinionRunState(
            minion_id="inv-owner",
            run_id="run-owner",
            pack=MinionInvocationPack(invocation_id="inv-owner"),
            process=process,
        )
        self.manager.runs[state.run_id] = state
        self.manager.v2_service.repository.record_worker_event = lambda _event: None
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
        with self.assertRaisesRegex(RuntimeError, "before its process group"):
            self.manager._unregister_v2_broker_run(state.run_id, False)

        self.manager._unregister_v2_broker_run(state.run_id, True)
        self.assertEqual(state.status, "completed")
        self.assertFalse(state.summary()["run_active"])
        self.assertTrue(state.ended_at)

    async def test_late_terminal_receipt_after_reap_preserves_terminal_status(self) -> None:
        process = SimpleNamespace(pid=124, returncode=0)
        state = MinionRunState(
            minion_id="inv-late-terminal",
            run_id="run-late-terminal",
            pack=MinionInvocationPack(invocation_id="inv-late-terminal"),
            process=process,
        )
        self.manager.runs[state.run_id] = state
        self.manager.v2_service.repository.record_worker_event = lambda _event: None
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
        orchestrator._process_owners["inv-owned"] = SimpleNamespace(
            process=SimpleNamespace(returncode=0)
        )
        orchestrator.repository.assert_fencing_token = (
            lambda _resource, _owner, _token: None
        )

        with self.assertRaisesRegex(LeaseConflict, "already active"):
            await orchestrator._reuse_or_retire_effect_lease(
                resource_key="node:owned:writer",
                owner_id="inv-owned",
                fencing_token=1,
                worker_label="owned worker",
            )

    async def test_close_all_delegates_process_shutdown_to_raii_owner(self) -> None:
        class Leader:
            returncode: int | None = None

            def terminate(self) -> None:
                raise AssertionError("Manager must not terminate a raw leader")

        process = Leader()
        state = MinionRunState(
            minion_id="inv-close",
            run_id="run-close",
            pack=MinionInvocationPack(invocation_id="inv-close"),
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
