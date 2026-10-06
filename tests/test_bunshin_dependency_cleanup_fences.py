from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pal.bunshin.v2.artifacts import ContentAddressedArtifactStore
from pal.bunshin.v2.background_assignments import BackgroundAssignments
from pal.bunshin.v2.contracts import AggregateSnapshot, AggregateType, DeferredEffectError, SubmissionInvariantError
from pal.bunshin.v2.process_lifecycle import WorkerProcessOwner, WorkerProcessReapError
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.role_protocol import RoleAssignmentRequest
from pal.bunshin.v2.semantic_orchestration.effect_reads import EffectReads
from pal.bunshin.v2.semantic_orchestration.attempt_execution import AttemptExecution
from pal.bunshin.v2.semantic_orchestration.role_cleanup import RoleCleanup
from pal.bunshin.v2.semantic_orchestration.role_leases import RoleLeases
from pal.bunshin.v2.storage.role_assignments import semantic_business_lease
from pal.bunshin.v2.worker_processes import WorkerProcesses
from pal.bunshin.v2.workspace_resources import WorkspaceLockRegistry


class DependencyCleanupTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_profile_owns_only_its_setup_task_before_row_creation(self):
        background = BackgroundAssignments()
        began, finishing, allow_finish = (asyncio.Event() for _ in range(3))
        async def setup(**kwargs):
            began.set()
            try:
                await asyncio.Event().wait()
            finally:
                finishing.set()
                await allow_finish.wait()
        async def heartbeat(*args):
            await asyncio.Event().wait()
        attempt = SimpleNamespace(
            background=background,
            repository=SimpleNamespace(leases=SimpleNamespace(renew_lease=Mock())),
            role_leases=SimpleNamespace(bind_background_business_lease=Mock(), lease_heartbeat=heartbeat),
            supervisor=SimpleNamespace(release_process_slot=AsyncMock()),
            run_profile_inner=setup,
        )
        attempt.run_profile = MethodType(AttemptExecution.run_profile, attempt)
        caller = asyncio.create_task(attempt.run_profile(
            effect={'effect_key': 'inline'}, snapshot=None, invocation_id='session',
            lease_resource='node:review', fencing_token=1, profile='profile', activation=None,
            instruction='', reference_refs={},
        ))
        await began.wait()
        owned = background.task('inline')
        self.assertIsNotNone(owned)
        self.assertIsNot(owned, caller)
        cleanup = RoleCleanup(WorkerProcesses(), Path('/unused'), background)
        retirement = asyncio.create_task(cleanup.retire_incarnation(
            effect_key='inline', invocation_id='session', lease_resource_key='node:review', fencing_token=1,
        ))
        await finishing.wait()
        self.assertFalse(retirement.done())
        allow_finish.set()
        await retirement
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertTrue(owned.done())

    async def test_freeze_joins_setup_before_assignment_exists(self):
        background = BackgroundAssignments()
        cleanup = RoleCleanup(WorkerProcesses(), Path('/unused'), background)
        setup_started, setup_cleanup, finish_cleanup = (asyncio.Event() for _ in range(3))
        reached_assignment = False

        async def setup():
            nonlocal reached_assignment
            setup_started.set()
            try:
                await asyncio.Event().wait()
                reached_assignment = True
            finally:
                setup_cleanup.set()
                await finish_cleanup.wait()
            return {}

        task = asyncio.create_task(setup())
        background.track('old-effect', task)
        await setup_started.wait()
        retirement = asyncio.create_task(cleanup.retire_incarnation(
            effect_key='old-effect', invocation_id='session',
            lease_resource_key='node:review', fencing_token=1,
        ))
        await setup_cleanup.wait()
        self.assertFalse(retirement.done())
        self.assertEqual(background.assignment_id('old-effect'), '')
        finish_cleanup.set()
        self.assertEqual(await retirement, '')
        self.assertTrue(task.done())
        self.assertFalse(reached_assignment)
        with self.assertRaises(DeferredEffectError):
            background.bind('old-effect', 'late-assignment')
        with self.assertRaises(DeferredEffectError):
            await background.launch({'effect_key': 'old-effect'}, AsyncMock(), AsyncMock())

    async def test_cancel_during_spawn_retains_and_reaps_child_before_ack(self):
        background, processes = BackgroundAssignments(), WorkerProcesses()
        spawn_started, finish_spawn, exited = (asyncio.Event() for _ in range(3))
        process = SimpleNamespace(pid=901, returncode=None, stdin=None, stdout=None, stderr=None)

        async def wait():
            await exited.wait()
            return process.returncode

        process.wait = wait

        async def spawn(*args, **kwargs):
            spawn_started.set()
            await finish_spawn.wait()
            return process

        def kill(_pid, _signal):
            process.returncode = -9
            exited.set()

        tmp = tempfile.TemporaryDirectory(prefix='spawn-workspace-')
        self.addCleanup(tmp.cleanup)
        workspace = Path(tmp.name) / 'worktree'
        workspace.mkdir()
        locks = WorkspaceLockRegistry()
        owner = WorkerProcessOwner(
            argv=('fake',), env={}, invocation_id='session', run_id='stable-run',
            workspace=workspace, workspace_locks=locks,
            on_started=lambda owner: None, on_reserved=processes.register,
            on_registered=processes.register, on_unregistered=processes.unregister,
            effect_key='old-effect', business_lease_resource_key='node:review',
            business_fencing_token=1, assignment_id='old-assignment', attempt_id='old-attempt',
        )

        async def run():
            async with owner:
                await asyncio.Event().wait()
            return {}

        with patch('asyncio.create_subprocess_exec', side_effect=spawn), patch('os.killpg', side_effect=kill) as killpg:
            task = asyncio.create_task(run())
            background.track('old-effect', task)
            background.bind('old-effect', 'old-assignment')
            await spawn_started.wait()
            cleanup = RoleCleanup(processes, Path('/unused'), background)
            retiring = asyncio.create_task(cleanup.retire_incarnation(
                effect_key='old-effect', invocation_id='session', lease_resource_key='node:review',
                fencing_token=1, assignment_id='old-assignment', attempt_id='old-attempt',
            ))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(retiring.done())
            self.assertTrue(processes.contains('session'))
            self.assertFalse(owner.resources_released)
            self.assertTrue(locks.is_held(owner.lock_key))
            # A second cancellation also cannot detach the in-flight spawn.
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            finish_spawn.set()
            self.assertEqual(await retiring, 'old-assignment')
            self.assertTrue(owner.resources_released)
            self.assertFalse(locks.is_held(owner.lock_key))
            self.assertFalse(processes.contains('session'))
            killpg.assert_called_once()

    async def test_old_effect_replay_does_not_cancel_rebound_task_or_owner(self):
        background, processes = BackgroundAssignments(), WorkerProcesses()
        replacement = asyncio.create_task(asyncio.Event().wait())
        background.track('effect', replacement)
        background.bind_lease('effect', 'session', 'node:review', 2)
        owner = SimpleNamespace(
            invocation_id='session', run_id='stable-run', effect_key='effect',
            business_lease_resource_key='node:review', business_fencing_token=2,
            assignment_id='new-assignment', attempt_id='new-attempt',
            _close_shielded=AsyncMock(),
        )
        processes.register(owner)
        try:
            cleanup = RoleCleanup(processes, Path('/unused'), background)
            await cleanup.retire_incarnation(
                effect_key='effect', invocation_id='session', lease_resource_key='node:review',
                fencing_token=1, assignment_id='old-assignment', attempt_id='old-attempt',
            )
            self.assertFalse(replacement.done())
            owner._close_shielded.assert_not_awaited()
            background.assert_not_retired('effect')
        finally:
            replacement.cancel()
            await asyncio.gather(replacement, return_exceptions=True)

    async def test_cleanup_failure_cannot_acknowledge(self):
        processes = WorkerProcesses()
        owner = SimpleNamespace(
            invocation_id='session', run_id='run', effect_key='effect',
            business_lease_resource_key='node:review', business_fencing_token=1,
            assignment_id='assignment', attempt_id='attempt',
            _close_shielded=AsyncMock(side_effect=WorkerProcessReapError('still alive')),
        )
        processes.register(owner)
        cleanup = RoleCleanup(processes, Path('/unused'), BackgroundAssignments())
        with self.assertRaisesRegex(WorkerProcessReapError, 'still alive'):
            await cleanup.retire_incarnation(
                effect_key='effect', invocation_id='session', lease_resource_key='node:review',
                fencing_token=1, assignment_id='assignment', attempt_id='attempt',
            )
        self.assertTrue(processes.contains('session'))


class DependencyCleanupStorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dependency-cleanup-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = BunshinV2Repository(self.root)
        self.repo.database.ensure_schema()
        self.artifacts = ContentAddressedArtifactStore(self.root, self.repo.artifacts)
        self.prompt = self.artifacts.put_json({'prompt': 'test'}, artifact_type='RolePromptPackArtifact')
        self.result = self.artifacts.put_json({'result': 'test'}, artifact_type='CandidateRoleSubmissionArtifact')
        self.lease = self.repo.leases.claim_lease('node:review', 'session', ttl_seconds=120)
        self.node = AggregateSnapshot(
            aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id='node', workflow_id='workflow',
            state='PRODUCING', version=1, payload={
                'module_name': 'module', 'active_worker_id': 'session',
                'lease_resource_key': 'node:review', 'fencing_token': self.lease.fencing_token,
            }, created_at='2026-01-01T00:00:00+00:00', updated_at='2026-01-01T00:00:00+00:00',
        )
        with self.repo.database.write_connection() as connection:
            self.repo.snapshots.write_snapshot_locked(connection, None, self.node)
        self.repo.role_sessions.ensure_role_session(
            session_id='session', workflow_id='workflow', aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id='node', role='implementation', mode='produce',
            role_profile_id='software_engineering.v2_coder', family_binding_sha='binding',
            scope_kind='module', subject_key='module',
        )
        self.binding = semantic_business_lease(
            self.node, owner_id='session', resource_key='node:review', fencing_token=self.lease.fencing_token,
        )
        self.request = RoleAssignmentRequest(
            assignment_key='effect', session_id='session', workflow_id='workflow',
            aggregate_type=AggregateType.DAG_NODE_RUN.value, aggregate_id='node',
            role='implementation', mode='produce', role_profile_id='software_engineering.v2_coder',
            family_binding_sha='binding', input_fingerprint='input', required_inputs=(), input_refs={},
            execution_spec={'effect_type': 'run_implementation_role', 'effect_key': 'effect', 'business_lease': self.binding}, submission_kind='candidate',
        )
        self.role_leases = RoleLeases(
            EffectReads(self.repo), RoleCleanup(WorkerProcesses(), self.root), None,
            WorkerProcesses(), self.repo, WorkspaceLockRegistry(),
        )
        self.effect = {'effect_key': 'effect', 'aggregate_type': AggregateType.DAG_NODE_RUN.value,
                       'aggregate_id': 'node', 'payload': {'_causal_context': {
                           'active_worker_id': 'session', 'lease_resource_key': 'node:review',
                           'fencing_token': self.lease.fencing_token}}}

    def update_node(self, **changes):
        current = self.repo.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, 'node')
        updated = replace(current, version=current.version + 1, **changes)
        with self.repo.database.write_connection() as connection:
            self.repo.snapshots.write_snapshot_locked(connection, current, updated)
        return updated

    def assignment(self):
        return self.repo.role_assignments.create_role_assignment(self.request)

    def start(self, assignment):
        attempt = self.repo.role_assignments.claim_role_assignment(assignment['assignment_id'])
        lease = self.repo.leases.claim_lease('assignment:' + assignment['assignment_id'], attempt['attempt_id'])
        self.repo.role_attempts.start_role_attempt(
            assignment_id=assignment['assignment_id'], attempt_id_value=attempt['attempt_id'],
            lease_resource_key=lease.resource_key, fencing_token=lease.fencing_token,
            prompt_pack_ref=self.prompt.to_dict(),
        )
        return attempt, lease

    def cancel(self, assignment):
        return self.repo.role_cancellation.cancel_role_assignments(
            workflow_id='workflow', aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id='node',
            reason='dependency freeze', assignment_ids=(assignment['assignment_id'],),
        )

    def submit(self, assignment, attempt, lease):
        return self.repo.role_submissions.record_role_submission(
            assignment_id=assignment['assignment_id'], attempt_id_value=attempt['attempt_id'],
            fencing_token=lease.fencing_token, artifact_ref=self.result.to_dict(),
            payload_hash='payload', settlement_action={'action_type': 'SUBMIT_CANDIDATE'},
        )

    def test_freeze_rejects_create_and_return_of_existing_assignment(self):
        self.assignment()
        self.update_node(state='CANCEL_REQUESTED')
        for request in (self.request, replace(self.request, assignment_key='late-effect')):
            with self.assertRaises(DeferredEffectError):
                self.repo.role_assignments.create_role_assignment(request)
        self.assertEqual(len(self.repo.role_assignments.list_role_assignments()), 1)

    def test_freeze_after_capacity_wait_rejects_claim_and_start(self):
        assignment = self.assignment()
        self.update_node(state='CANCEL_REQUESTED')
        with self.assertRaises(DeferredEffectError):
            self.repo.role_assignments.claim_role_assignment(assignment['assignment_id'])
        self.update_node(state='PRODUCING')
        attempt = self.repo.role_assignments.claim_role_assignment(assignment['assignment_id'])
        lease = self.repo.leases.claim_lease('assignment:' + assignment['assignment_id'], attempt['attempt_id'])
        self.update_node(state='CANCEL_REQUESTED')
        with self.assertRaises(DeferredEffectError):
            self.repo.role_attempts.start_role_attempt(
                assignment_id=assignment['assignment_id'], attempt_id_value=attempt['attempt_id'],
                lease_resource_key=lease.resource_key, fencing_token=lease.fencing_token,
                prompt_pack_ref=self.prompt.to_dict(),
            )

    async def test_late_result_winning_cancellation_is_settled_and_receipt_preserved(self):
        assignment = self.assignment()
        attempt, lease = self.start(assignment)
        self.update_node(state='CANCEL_REQUESTED')
        result_recorded = asyncio.Event()
        async def submit_before_close():
            receipt = self.submit(assignment, attempt, lease)
            result_recorded.set()
            return receipt
        async def close_after_result():
            await result_recorded.wait()
            return self.cancel(assignment)[0]
        receipt, closed = await asyncio.gather(submit_before_close(), close_after_result())
        self.assertEqual(closed['state'], 'settled')
        self.assertEqual(closed['submission_payload_hash'], receipt.payload_hash)
        self.assertEqual(closed['submission_artifact_ref'], receipt.artifact_ref)
        self.assertEqual(self.submit(assignment, attempt, lease), receipt)

    async def test_cancellation_winning_late_result_rejects_submission(self):
        assignment = self.assignment()
        attempt, lease = self.start(assignment)
        self.update_node(state='CANCEL_REQUESTED')
        cut = asyncio.Event()
        async def close_before_result():
            self.assertEqual(self.cancel(assignment)[0]['state'], 'cancelled')
            cut.set()
        async def submit_after_close():
            await cut.wait()
            with self.assertRaisesRegex(ValueError, 'not accepting'):
                self.submit(assignment, attempt, lease)
        await asyncio.gather(close_before_result(), submit_after_close())

    def test_replayed_old_cancellation_does_not_close_replacement_assignment(self):
        old = self.assignment()
        self.cancel(old)
        replacement = self.repo.role_assignments.create_role_assignment(replace(self.request, assignment_key='new-effect'))
        self.assertEqual(self.cancel(old), ())
        self.assertEqual(self.repo.role_assignments.read_role_assignment(replacement['assignment_id'])['state'], 'queued')
        self.assertEqual(self.repo.role_cancellation.cancel_role_assignments(
            workflow_id='workflow', aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id='node',
            reason='empty capture', assignment_ids=(),
        ), ())

    def test_old_effect_cleanup_does_not_release_new_business_fence(self):
        self.repo.leases.release_lease('node:review', 'session', self.lease.fencing_token)
        replacement = self.repo.leases.claim_lease('node:review', 'session')
        self.update_node(payload={**self.node.payload, 'fencing_token': replacement.fencing_token})
        self.role_leases.release_background_business_lease(self.effect)
        self.repo.leases.assert_fencing_token('node:review', 'session', replacement.fencing_token)

    def test_same_submission_keeps_snapshot_lease_and_cleanup_release_is_checked(self):
        self.update_node(state='REVIEW_SNAPSHOTTING')
        self.role_leases.release_background_business_lease(self.effect)
        self.repo.leases.assert_fencing_token('node:review', 'session', self.lease.fencing_token)
        with patch.object(self.repo.leases, 'release_lease', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'still holds'):
                self.role_leases.release_business_lease(
                    resource_key='node:review', owner_id='session', fencing_token=self.lease.fencing_token,
                )
        self.role_leases.release_business_lease(
            resource_key='node:review', owner_id='session', fencing_token=self.lease.fencing_token,
        )
        self.assertEqual(self.repo.leases.read_lease('node:review')['owner_id'], '')

    def test_missing_causal_identity_never_releases_latest_lease(self):
        self.role_leases.release_background_business_lease({**self.effect, 'payload': {}})
        self.repo.leases.assert_fencing_token('node:review', 'session', self.lease.fencing_token)

    def test_explicit_current_binding_permits_recovery_without_rewriting_original(self):
        assignment = self.assignment()
        self.repo.leases.release_lease('node:review', 'session', self.lease.fencing_token)
        rebound = self.repo.leases.claim_lease('node:review', 'session')
        current = self.update_node(payload={**self.node.payload, 'fencing_token': rebound.fencing_token})
        with self.assertRaises(DeferredEffectError):
            self.repo.role_assignments.claim_role_assignment(assignment['assignment_id'])
        binding = semantic_business_lease(
            current, owner_id='session', resource_key='node:review', fencing_token=rebound.fencing_token,
        )
        attempt = self.repo.role_assignments.claim_role_assignment(
            assignment['assignment_id'], business_lease=binding,
        )
        lease = self.repo.leases.claim_lease('assignment:' + assignment['assignment_id'], attempt['attempt_id'])
        self.repo.role_attempts.start_role_attempt(
            assignment_id=assignment['assignment_id'], attempt_id_value=attempt['attempt_id'],
            lease_resource_key=lease.resource_key, fencing_token=lease.fencing_token,
            prompt_pack_ref=self.prompt.to_dict(), business_lease=binding,
        )
        stored = self.repo.role_assignments.read_role_assignment(assignment['assignment_id'])
        self.assertEqual(stored['execution_spec']['business_lease'], self.binding)
        self.assertEqual(stored['state'], 'running')

    async def test_capacity_wait_cannot_claim_after_freeze(self):
        assignment = self.assignment()
        waiting, capacity = asyncio.Event(), asyncio.Event()
        async def admit():
            waiting.set()
            await capacity.wait()
            return self.repo.role_assignments.claim_role_assignment(assignment['assignment_id'])
        task = asyncio.create_task(admit())
        await waiting.wait()
        self.update_node(state='CANCEL_REQUESTED')
        capacity.set()
        with self.assertRaises(DeferredEffectError):
            await task
        self.assertEqual(self.repo.role_attempts.list_role_attempts(assignment['assignment_id']), ())

    async def test_old_task_finally_cannot_borrow_replacement_lease(self):
        old_ready, replacement_bound = asyncio.Event(), asyncio.Event()
        async def old_task():
            self.role_leases.bind_background_business_lease(
                self.effect, owner_id='session', resource_key='node:review', fencing_token=self.lease.fencing_token,
            )
            old_ready.set()
            await replacement_bound.wait()
            self.role_leases.release_background_business_lease(self.effect)
        async def replacement_task():
            await old_ready.wait()
            self.repo.leases.release_lease('node:review', 'session', self.lease.fencing_token)
            rebound = self.repo.leases.claim_lease('node:review', 'session')
            self.update_node(payload={**self.node.payload, 'fencing_token': rebound.fencing_token})
            self.role_leases.bind_background_business_lease(
                self.effect, owner_id='session', resource_key='node:review', fencing_token=rebound.fencing_token,
            )
            replacement_bound.set()
            return rebound
        _, rebound = await asyncio.gather(old_task(), replacement_task())
        self.repo.leases.assert_fencing_token('node:review', 'session', rebound.fencing_token)

    def test_attempt_claim_persists_actual_binding_across_start_restart_and_release(self):
        assignment = self.assignment()
        first = self.repo.role_assignments.claim_role_assignment(assignment['assignment_id'])
        first_binding = self.repo.role_attempts.read_role_attempt_business_lease(first['attempt_id'])
        self.assertEqual(first_binding['business_lease'], self.binding)
        self.repo.role_retries.queue_role_attempt_retry(
            assignment_id=assignment['assignment_id'], attempt_id_value=first['attempt_id'],
            error_kind='test_recovery', error_text='first process is gone',
        )
        self.repo.leases.release_lease('node:review', 'session', self.lease.fencing_token)
        rebound = self.repo.leases.claim_lease('node:review', 'session')
        node = self.update_node(payload={**self.node.payload, 'fencing_token': rebound.fencing_token})
        binding = semantic_business_lease(node, owner_id='session', resource_key='node:review', fencing_token=rebound.fencing_token)
        second = self.repo.role_assignments.claim_role_assignment(assignment['assignment_id'], business_lease=binding)
        actual = self.repo.role_attempts.read_role_attempt_business_lease(second['attempt_id'])
        self.assertEqual(actual['business_lease'], binding)
        self.assertEqual(actual['attempt_id'], second['attempt_id'])
        self.assertEqual(actual['assignment_id'], assignment['assignment_id'])
        self.assertNotEqual(actual['artifact_ref'], first_binding['artifact_ref'])
        lease = self.repo.leases.claim_lease('assignment:' + assignment['assignment_id'], second['attempt_id'])
        self.repo.role_attempts.start_role_attempt(
            assignment_id=assignment['assignment_id'], attempt_id_value=second['attempt_id'],
            lease_resource_key=lease.resource_key, fencing_token=lease.fencing_token,
            prompt_pack_ref=self.prompt.to_dict(), business_lease=binding,
        )
        self.repo.leases.release_lease('node:review', 'session', rebound.fencing_token)
        replacement = self.repo.leases.claim_lease('node:review', 'session')
        self.update_node(payload={**self.node.payload, 'fencing_token': replacement.fencing_token})
        restarted = BunshinV2Repository(self.root)
        self.assertEqual(restarted.role_attempts.read_role_attempt_business_lease(second['attempt_id']), actual)
        self.assertEqual(restarted.role_assignments.read_role_assignment(assignment['assignment_id'])['execution_spec']['business_lease'], self.binding)
        with self.repo.database.read_connection() as connection:
            self.assertIsNotNone(connection.execute(
                'SELECT 1 FROM bunshin_v2_artifact_refs WHERE parent_sha256 = ? AND child_sha256 = ? AND relation = ?',
                (self.prompt.sha256, actual['artifact_ref']['sha256'], 'role_attempt_business_lease'),
            ).fetchone())

    def test_start_cannot_change_claimed_attempt_business_identity(self):
        assignment = self.assignment()
        attempt = self.repo.role_assignments.claim_role_assignment(assignment['assignment_id'])
        self.repo.leases.release_lease('node:review', 'session', self.lease.fencing_token)
        rebound = self.repo.leases.claim_lease('node:review', 'session')
        node = self.update_node(payload={**self.node.payload, 'fencing_token': rebound.fencing_token})
        binding = semantic_business_lease(node, owner_id='session', resource_key='node:review', fencing_token=rebound.fencing_token)
        lease = self.repo.leases.claim_lease('assignment:' + assignment['assignment_id'], attempt['attempt_id'])
        with self.assertRaisesRegex(SubmissionInvariantError, 'cannot change its claimed business lease'):
            self.repo.role_attempts.start_role_attempt(
                assignment_id=assignment['assignment_id'], attempt_id_value=attempt['attempt_id'],
                lease_resource_key=lease.resource_key, fencing_token=lease.fencing_token,
                prompt_pack_ref=self.prompt.to_dict(), business_lease=binding,
            )
        self.assertEqual(self.repo.role_attempts.read_role_attempt(attempt['attempt_id'])['status'], 'starting')
        self.assertEqual(self.repo.role_attempts.read_role_attempt_business_lease(attempt['attempt_id'])['business_lease'], self.binding)


    def test_attempt_ownership_reader_rejects_non_durable_artifact(self):
        assignment = self.assignment()
        attempt = self.repo.role_assignments.claim_role_assignment(assignment['assignment_id'])
        ownership = self.repo.role_attempts.read_role_attempt_business_lease(attempt['attempt_id'])
        with self.repo.database.write_connection() as connection:
            connection.execute('UPDATE bunshin_v2_artifacts SET durable = 0 WHERE sha256 = ?',
                               (ownership['artifact_ref']['sha256'],))
        with self.assertRaisesRegex(SubmissionInvariantError, 'not durable'):
            self.repo.role_attempts.read_role_attempt_business_lease(attempt['attempt_id'])



class DependencyRecoveryIncarnationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_recovered_attempt_is_closed_using_its_actual_business_fence(self):
        import os
        import sys
        from tests.test_bunshin_dependency_repair_runtime import RuntimeCase

        case = RuntimeCase()
        task = None
        repairing = None
        finish = asyncio.Event()
        try:
            await case.start_checker('archive_verify')
            peer = await case.start_checker('checksum_verify')
            repository = case.repository
            assignment, old_attempt, old_node = peer['assignment'], peer['attempt'], peer['node']
            repository.role_retries.queue_role_attempt_retry(
                assignment_id=assignment['assignment_id'], attempt_id_value=old_attempt['attempt_id'],
                error_kind='test_recovery', error_text='old process disappeared',
            )
            old_attempt_lease = peer['attempt_lease']
            repository.leases.release_lease(old_attempt_lease.resource_key, old_attempt['attempt_id'], old_attempt_lease.fencing_token)
            resource, session = old_node.payload['lease_resource_key'], old_node.payload['active_worker_id']
            repository.leases.release_lease(resource, session, old_node.payload['fencing_token'])
            rebound = repository.leases.claim_lease(resource, session, ttl_seconds=120)
            node = case.dispatch('checksum_verify', 'REBIND_REVIEWER', {
                'active_worker_id': session, 'lease_resource_key': resource, 'fencing_token': rebound.fencing_token,
            })
            binding = semantic_business_lease(node, owner_id=session, resource_key=resource, fencing_token=rebound.fencing_token)
            attempt = repository.role_assignments.claim_role_assignment(assignment['assignment_id'], business_lease=binding)
            attempt_lease = repository.leases.claim_lease('attempt:' + attempt['attempt_id'], attempt['attempt_id'], ttl_seconds=120)
            workspace = {**peer['workspace'], 'bunshin_v2': {**peer['workspace']['bunshin_v2'],
                'invocation_id': attempt['attempt_id'], 'lease_resource_key': attempt_lease.resource_key,
                'fencing_token': attempt_lease.fencing_token}}
            prompt = case.artifacts.put_json({'workspace': workspace, 'metadata': {'agent_session': {'session_id': session}}},
                                            artifact_type='RolePromptPackArtifact')
            repository.role_attempts.start_role_attempt(
                assignment_id=assignment['assignment_id'], attempt_id_value=attempt['attempt_id'],
                lease_resource_key=attempt_lease.resource_key, fencing_token=attempt_lease.fencing_token,
                prompt_pack_ref=prompt.to_dict(), business_lease=binding,
            )
            peer.update(attempt=attempt, attempt_lease=attempt_lease, workspace=workspace, node=node)
            entered, cancelling = asyncio.Event(), asyncio.Event()
            effect_key = peer['effect']['effect_key']
            owner = WorkerProcessOwner(
                argv=(sys.executable, '-c', 'import time; time.sleep(60)'), env=dict(os.environ),
                invocation_id=session, run_id='recovered-checker', workspace=case.workspaces['checksum_verify'],
                workspace_locks=case.worker.workspace_locks, on_started=lambda owner: None,
                on_reserved=case.worker.processes.register, on_registered=case.worker.processes.register,
                on_unregistered=case.worker.processes.unregister, effect_key=effect_key,
                assignment_id=assignment['assignment_id'], attempt_id=attempt['attempt_id'],
                business_lease_resource_key=resource, business_fencing_token=rebound.fencing_token,
            )
            async def running():
                async with owner:
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelling.set()
                        await finish.wait()
                return {}
            task = asyncio.create_task(running())
            case.worker.background.track(effect_key, task)
            case.worker.background.bind(effect_key, assignment['assignment_id'])
            case.worker.background.bind_lease(effect_key, session, resource, rebound.fencing_token)
            await asyncio.wait_for(entered.wait(), 10)
            await case.prepare_source()
            frontier = case.graph().dependency_repairs.pending.frontier
            frozen = next(item for item in frontier.values() if item.node_name == 'checksum_verify')
            self.assertEqual(frozen.fencing_token, rebound.fencing_token)
            self.assertEqual(frozen.attempt_id, attempt['attempt_id'])
            self.assertEqual(frozen.snapshot_fencing_token, 0)
            self.assertEqual(repository.role_assignments.read_role_assignment(assignment['assignment_id'])['execution_spec']['business_lease']['fencing_token'], old_node.payload['fencing_token'])
            repairing = asyncio.create_task(case.process('archive_verify', 'reconcile_dependency_repairs'))
            await asyncio.wait_for(cancelling.wait(), 10)
            self.assertFalse(repairing.done())
            self.assertTrue(case.worker.workspace_locks.is_held(owner.lock_key))
            finish.set()
            await asyncio.wait_for(repairing, 10)
            self.assertTrue(task.done())
            self.assertTrue(owner.resources_released)
            self.assertFalse(case.worker.processes.contains(session))
            self.assertFalse(case.worker.workspace_locks.is_held(owner.lock_key))
            self.assertEqual(repository.leases.read_lease(resource)['owner_id'], '')
            self.assertEqual(case.node('checksum_verify').state, 'STALE')
            self.assertIsNone(case.graph().dependency_repairs.pending)
        finally:
            finish.set()
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if repairing is not None:
                if not repairing.done():
                    repairing.cancel()
                await asyncio.gather(repairing, return_exceptions=True)
            case.close()
