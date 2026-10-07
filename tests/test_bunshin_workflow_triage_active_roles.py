"""Offline outbox recovery for workflow triage with live semantic roles.

Aggregate/cycle transitions, Git worktrees, leases, role receipts, background
ownership, and process cleanup are real. A Python child waiting on stdin stands
in for each model worker; only the exhausted infrastructure failure is injected.
"""
from __future__ import annotations

import asyncio
import sys
from unittest.mock import patch

import pytest

from pal.bunshin.v2.contracts import AggregateType, StaleFencingToken
from pal.bunshin.v2.cycle_protocol import CycleSlot, NodeCycleState
from pal.bunshin.v2.process_lifecycle import WorkerProcessOwner
from pal.bunshin.v2.role_protocol import RoleAssignmentRequest, stable_hash
from pal.bunshin.v2.storage.role_assignments import semantic_business_lease
from pal.bunshin.v2.workflow_runtime import WorkflowCoordinator
from tests.test_bunshin_unchanged_dependency_baseline import ExistingRepositoryCase


class ActiveWorkflowCase(ExistingRepositoryCase):
    name = "archive_verify"

    def __init__(self, root):
        super().__init__(root)
        self.roles = []
        self.pause_effects = {}
        # Keep the real effect/background/attempt ownership paths while replacing
        # only model execution with a deterministic child owned by the manager.
        self.worker.components.effect_dispatch.handlers.update({
            "run_implementation_role": self.offline_role,
            "run_verifier_role": self.offline_role,
        })

    def cycle(self):
        return self.coordinator.execution(workflow_id=self.workflow_id).cycles[self.name]

    async def start(self, slot):
        if slot == CycleSlot.CHECKER:
            provider = await self.ready()
            await self.process(provider.aggregate_id, "notify_node_accepted")
            await self.process(self.node_ids[self.name], "admit_verifier_role")
        else:
            self.scheduler.schedule_ready_nodes(workflow_id=self.workflow_id, epoch_id=self.epoch_id)
            await self.process(self.node_ids[self.name], "admit_implementation_role")
        return await self.run_current_role()

    async def run_current_role(self):
        effect_type = (
            "run_verifier_role" if self.node(self.name).state == "REVIEWING"
            else "run_implementation_role"
        )
        effect = await self.process(self.node_ids[self.name], effect_type)
        role = self.roles[-1]
        assert role["effect"]["effect_id"] == effect["effect_id"]
        assert not role["task"].done()
        assert role["owner"].pid > 0
        assert self.worker.active_background_count == 1
        return role

    async def offline_role(self, effect):
        node = self.node(self.name)
        role, mode = node.payload["active_role"], node.payload["active_role_mode"]
        session = node.payload["active_worker_id"]
        profile = "software_engineering.v2_" + ("verifier" if role == "verifier" else "coder")
        binding = self.workflow().payload["family_binding_ref"]["sha256"]
        self.repository.role_sessions.ensure_role_session(
            session_id=session, workflow_id=self.workflow_id,
            aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=node.aggregate_id,
            role=role, mode=mode, role_profile_id=profile,
            family_binding_sha=binding, scope_kind="module", subject_key=self.name,
        )
        business_lease = semantic_business_lease(
            node, owner_id=session, resource_key=node.payload["lease_resource_key"],
            fencing_token=node.payload["fencing_token"],
        )
        assignment = self.repository.role_assignments.create_role_assignment(RoleAssignmentRequest(
            assignment_key=effect["effect_key"], session_id=session,
            workflow_id=self.workflow_id, aggregate_type=AggregateType.DAG_NODE_RUN.value,
            aggregate_id=node.aggregate_id, role=role, mode=mode,
            role_profile_id=profile, family_binding_sha=binding,
            input_fingerprint=self.cycle().active_assignment.input_fingerprint,
            required_inputs=(), input_refs={"unit_contract": node.payload["unit_contract_ref"]},
            execution_spec={
                "effect_type": effect["effect_type"], "effect_key": effect["effect_key"],
                "effect_id": effect["effect_id"], "business_lease": business_lease,
            },
            submission_kind="verification" if role == "verifier" else "implementation",
        ))
        attempt = self.repository.role_assignments.claim_role_assignment(assignment["assignment_id"])
        attempt_id = attempt["attempt_id"]
        lease = self.repository.leases.claim_lease("attempt:" + attempt_id, attempt_id, ttl_seconds=120)
        prompt = self.artifacts.put_json({"offline": True}, artifact_type="RolePromptPackArtifact")
        self.repository.role_attempts.start_role_attempt(
            assignment_id=assignment["assignment_id"], attempt_id_value=attempt_id,
            lease_resource_key=lease.resource_key, fencing_token=lease.fencing_token,
            prompt_pack_ref=prompt.to_dict(),
        )
        owner = WorkerProcessOwner(
            argv=(sys.executable, "-B", "-c", "import sys; sys.stdin.buffer.read()"),
            env={}, invocation_id=session, run_id=attempt_id,
            workspace=self.workspaces[self.name], workspace_locks=self.worker.workspace_locks,
            on_started=lambda _: None, on_registered=self.worker.processes.register,
            on_unregistered=self.worker.processes.unregister,
            effect_key=effect["effect_key"], assignment_id=assignment["assignment_id"],
            attempt_id=attempt_id, business_lease_resource_key=business_lease["resource_key"],
            business_fencing_token=business_lease["fencing_token"],
        )
        self.roles.append({
            "assignment": assignment, "attempt": attempt, "lease": lease,
            "node": node, "effect": effect, "owner": owner, "task": asyncio.current_task(),
        })
        async with owner:
            self.worker.background.signal_ready(effect, assignment["assignment_id"])
            await owner.wait()
        return {"provider_request_id": assignment["assignment_id"]}

    def exhaust(self, effect):
        with self.repository.database.write_connection() as connection:
            connection.execute(
                "UPDATE bunshin_v2_outbox SET max_attempts = 1 WHERE effect_id = ?",
                (effect["effect_id"],),
            )
        effect["max_attempts"] = 1
        return effect

    async def freeze_for_workflow_failure(self, *, expected_node_state="PAUSED"):
        effect = self.exhaust(self.claim(self.workflow_id, "route_workflow"))
        with patch.object(
            self.processor, "_route_workflow",
            side_effect=ValueError("offline workflow routing failure"),
        ):
            assert await self.processor._process_effect(effect) == "failed"
        assert self.workflow().state == "TRIAGE_REQUIRED"
        await self.process(self.workflow_id, "freeze_workflow_children")
        for name in self.graph.nodes:
            if self.node(name).state == "PAUSE_REQUESTED":
                self.pause_effects[name] = await self.process(self.node_ids[name], "pause_role")
        assert self.node(self.name).state == expected_node_state
        assert self.repository.snapshots.read_snapshot(
            AggregateType.EXECUTION_EPOCH, self.epoch_id,
        ).state == "PAUSED"
        return effect

    async def resolve_workflow(self):
        response = self.service.resume_workflow(
            workflow_id=self.workflow_id, actor="operator", source_channel="test",
        )
        assert response["status"] == "triage_requires_resolution"
        response = self.service.resolve_triage(
            workflow_id=self.workflow_id, actor="operator", source_channel="test",
            subject="phase:workflow", resolution="Offline routing fault repaired; resume existing execution.",
        )
        assert response["status"] == "triage_resolved"
        await self.process(self.workflow_id, "reconcile_workflow")
        await self.process(self.epoch_id, "reconcile_execution_epoch")

    def record_submission(self, role):
        payload = {"offline_result": role["assignment"]["assignment_id"]}
        ref = self.artifacts.put_json(payload, artifact_type="OfflineRoleSubmissionArtifact")
        return self.repository.role_submissions.record_role_submission(
            assignment_id=role["assignment"]["assignment_id"],
            attempt_id_value=role["attempt"]["attempt_id"],
            fencing_token=role["lease"].fencing_token, artifact_ref=ref.to_dict(),
            payload_hash=stable_hash(payload),
            settlement_action={"action_type": "SUBMIT_CANDIDATE"},
        )

    async def assert_retired(self, role):
        await asyncio.wait_for(asyncio.shield(role["task"]), timeout=5)
        assert role["task"].done()
        assert role["owner"].returncode is not None
        assert role["owner"].resources_released
        assert not self.worker.processes.contains(role["node"].payload["active_worker_id"])
        assert self.worker.active_background_count == 0
        assignment = self.repository.role_assignments.read_role_assignment(role["assignment"]["assignment_id"])
        assert assignment["state"] == "cancelled"
        attempt = self.repository.role_attempts.read_role_attempt(role["attempt"]["attempt_id"])
        assert attempt["status"] == "cancelled"
        assert attempt["finished_at"]
        assert attempt["access_token_hash"] == ""
        with pytest.raises(StaleFencingToken):
            self.repository.leases.assert_fencing_token(
                role["lease"].resource_key, role["attempt"]["attempt_id"], role["lease"].fencing_token,
            )
        node = role["node"]
        with pytest.raises(StaleFencingToken):
            self.repository.leases.assert_fencing_token(
                node.payload["lease_resource_key"], node.payload["active_worker_id"], node.payload["fencing_token"],
            )
        with pytest.raises(ValueError, match="not accepting a submission"):
            self.record_submission(role)

    async def assert_resumed_once(self, old, old_cycle, candidate, edit_path, edit, admission):
        fresh = self.node(self.name)
        assert fresh.state == old["node"].state
        assert fresh.payload["epoch_id"] == self.epoch_id
        assert self.workflow().payload["execution_epoch_id"] == self.epoch_id
        assert fresh.payload.get("candidate_ref") == candidate
        assert edit_path.read_bytes() == edit
        assert fresh.payload["active_worker_id"] == old["node"].payload["active_worker_id"]
        assert fresh.payload["fencing_token"] > old["node"].payload["fencing_token"]
        cycle = self.cycle()
        assert cycle.active_assignment.slot == old_cycle.active_assignment.slot
        assert cycle.active_assignment.input_fingerprint != old_cycle.active_assignment.input_fingerprint
        assert cycle.generation == old_cycle.generation
        new = await self.run_current_role()
        assert new["assignment"]["assignment_id"] != old["assignment"]["assignment_id"]
        assert new["attempt"]["attempt_id"] != old["attempt"]["attempt_id"]
        # Replaying a completed old claim must neither duplicate the new role
        # nor use the reusable logical session to close its newer incarnation.
        for effect in (old["effect"], self.pause_effects[self.name], admission):
            assert await self.processor._process_effect(effect) == "completed"
            self.assert_receipt(effect)
        assert len(self.roles) == 2
        assignments = self.repository.role_assignments.list_role_assignments(workflow_id=self.workflow_id)
        assert [item["state"] for item in assignments].count("running") == 1
        assert len(assignments) == 2
        assert not new["task"].done()
        assert new["owner"].returncode is None
        assert self.node(self.name) == fresh
        assert self.cycle() == cycle
        with pytest.raises(ValueError, match="not accepting a submission"):
            self.record_submission(old)
        receipt = self.record_submission(new)
        assert receipt == self.record_submission(new)
        assert receipt.assignment_id == new["assignment"]["assignment_id"]

    async def close(self):
        await self.worker.processes.close_all()
        await asyncio.gather(*(role["task"] for role in self.roles), return_exceptions=True)
        for node_id in self.node_ids.values():
            self.worker.workspace_locks.release(node_id)
            self.worker.workspace_locks.release("verification:" + node_id)


@pytest.mark.parametrize("slot", [CycleSlot.PRODUCER, CycleSlot.CHECKER])
def test_workflow_outbox_triage_retires_and_resumes_active_role(tmp_path, slot):
    async def run():
        case = ActiveWorkflowCase(tmp_path)
        try:
            old = await case.start(slot)
            old_cycle = case.cycle()
            candidate = old["node"].payload.get("candidate_ref")
            edit_path = case.workspaces[case.name] / (
                "archive_verify.py" if slot == CycleSlot.PRODUCER else "tests/archive_verify/verifier/test_retained.py"
            )
            edit_path.parent.mkdir(parents=True, exist_ok=True)
            edit = b"# unfinished role work must survive workflow triage\n"
            edit_path.write_bytes(edit)
            failure = await case.freeze_for_workflow_failure()
            await case.assert_retired(old)
            assert case.cycle().state == NodeCycleState.PAUSED
            assert case.cycle().active_assignment is None
            failed_attempts = case.repository.outbox_claims.list_effect_attempts(failure["effect_id"])
            assert [item["status"] for item in failed_attempts] == ["failed"]
            await case.resolve_workflow()
            admission = await case.process(case.node_ids[case.name], "resume_semantic_state")
            await case.assert_resumed_once(old, old_cycle, candidate, edit_path, edit, admission)
            assert case.repository.outbox_claims.list_effect_attempts(failure["effect_id"]) == failed_attempts
            assert case.stored(case.workflow_id, "route_workflow")["status"] == "failed"
        finally:
            await case.close()
    asyncio.run(run())


@pytest.mark.parametrize("slot", [CycleSlot.PRODUCER, CycleSlot.CHECKER])
def test_legacy_resume_conflict_recovers_through_public_module_triage(tmp_path, slot):
    async def run():
        case = ActiveWorkflowCase(tmp_path)
        try:
            old = await case.start(slot)
            old_cycle = case.cycle()
            candidate = old["node"].payload.get("candidate_ref")
            edit_path = case.workspaces[case.name] / "retained-incomplete-work.txt"
            edit = b"candidate workspace retained across existing module triage\n"
            edit_path.write_bytes(edit)
            # Reproduce the pre-fix crash projection: aggregate cleanup happened
            # without sending the matching pause intent to the graph owner.
            with patch.object(WorkflowCoordinator, "request_workflow_pause", return_value=None):
                await case.freeze_for_workflow_failure()
            await case.assert_retired(old)
            assert case.cycle() == old_cycle
            await case.resolve_workflow()
            failed_resume = case.exhaust(case.claim(case.node_ids[case.name], "resume_semantic_state"))
            assert await case.processor._process_effect(failed_resume) == "failed"
            assert "already runs a different" in case.stored(case.node_ids[case.name], "resume_semantic_state")["last_error"]
            assert case.node(case.name).state == "TRIAGE_REQUIRED"
            assert case.cycle().state == NodeCycleState.TRIAGE_REQUIRED
            await case.process(case.node_ids[case.name], "quiesce_role_for_triage")
            response = case.service.resolve_triage(
                workflow_id=case.workflow_id, actor="operator", source_channel="test",
                subject="module:" + case.name,
                resolution="Retired stale worker confirmed; resume the preserved candidate in this epoch.",
            )
            assert response["status"] == "triage_resolved"
            admission = await case.process(case.node_ids[case.name], "reconcile_semantic_state")
            await case.assert_resumed_once(old, old_cycle, candidate, edit_path, edit, admission)
        finally:
            await case.close()
    asyncio.run(run())


def test_nested_checker_triage_preserves_checker_recovery_across_workflow_freeze(tmp_path):
    async def run():
        case = ActiveWorkflowCase(tmp_path)
        try:
            provider = await case.ready()
            await case.process(provider.aggregate_id, "notify_node_accepted")
            await case.process(case.node_ids[case.name], "admit_verifier_role")
            candidate = case.node(case.name).payload["candidate_ref"]
            failed_run = case.exhaust(case.claim(case.node_ids[case.name], "run_verifier_role"))
            with patch.object(
                case.worker, "execute_semantic_effect",
                side_effect=ValueError("offline checker startup unavailable"),
            ):
                assert await case.processor._process_effect(failed_run) == "failed"
            await case.process(case.node_ids[case.name], "quiesce_role_for_triage")
            triaged = case.cycle()
            assert triaged.state == NodeCycleState.TRIAGE_REQUIRED
            assert triaged.resume_state == NodeCycleState.CHECKER_READY
            await case.freeze_for_workflow_failure(expected_node_state="TRIAGE_REQUIRED")
            assert case.cycle() == triaged
            await case.resolve_workflow()
            assert case.node(case.name).state == "TRIAGE_REQUIRED"
            assert case.cycle() == triaged
            response = case.service.resolve_triage(
                workflow_id=case.workflow_id, actor="operator", source_channel="test",
                subject="module:" + case.name,
                resolution="Checker startup repaired; finish checking the same candidate.",
            )
            assert response["status"] == "triage_resolved"
            assert case.cycle().state == NodeCycleState.CHECKER_READY
            await case.process(case.node_ids[case.name], "reconcile_semantic_state")
            assert case.node(case.name).state == "REVIEWING"
            assert case.node(case.name).payload["candidate_ref"] == candidate
            assert case.workflow().payload["execution_epoch_id"] == case.epoch_id
            assert case.cycle().active_assignment.slot == CycleSlot.CHECKER
            new = await case.run_current_role()
            assert len(case.roles) == 1
            assert case.record_submission(new).assignment_id == new["assignment"]["assignment_id"]
        finally:
            await case.close()
    asyncio.run(run())
