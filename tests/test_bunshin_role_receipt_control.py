"""Control fences must not retire a result which won the receipt commit."""
import asyncio
import pytest
from unittest.mock import patch
from pathlib import Path
from tests.test_bunshin_unchanged_dependency_baseline import ExistingRepositoryCase
from pal.bunshin.contracts import ActionEnvelope, AggregateType, DeferredEffectError
from pal.bunshin.role_protocol import RoleAssignmentRequest, stable_hash
from pal.bunshin.storage.role_assignments import semantic_business_lease
from pal.bunshin.workflow_control import reconcile_control_requests
from pal.bunshin.semantic_orchestration.orchestrator import SemanticOrchestrator
from pal.bunshin.service import BunshinWorkflowService

@pytest.mark.parametrize("outcome", ["failure", "success"])
@pytest.mark.parametrize("first", ["receipt", "pause", "cancel"])
def test_role_result_and_control_ordering(tmp_path, outcome, first):
    asyncio.run(run(tmp_path, outcome, first))


async def run(root, outcome, first):
    case=ExistingRepositoryCase(root)
    repo=case.repository
    name="manifest_model"
    node_id=case.node_ids[name]
    wf=case.workflow_id
    case.scheduler.schedule_ready_nodes(workflow_id=wf,epoch_id=case.epoch_id)
    await case.process(node_id,"admit_implementation_role")
    node=case.node(name)
    assert node.state=="PRODUCING"
    effect=case.stored(node_id,"run_implementation_role")
    invocation=node.payload["active_worker_id"]
    family=case.workflow().payload["family_binding_ref"]["sha256"]
    profile=case.worker.components.workflow_facts.profile_for_role(wf,"implementation")
    repo.role_sessions.ensure_role_session(session_id=invocation,workflow_id=wf,
        aggregate_type=AggregateType.DAG_NODE_RUN,aggregate_id=node_id,
        role="implementation",mode="produce",role_profile_id=profile,family_binding_sha=family,
        scope_kind="module",subject_key=name)
    business=semantic_business_lease(node,owner_id=invocation,
        resource_key=node.payload["lease_resource_key"],fencing_token=node.payload["fencing_token"])
    req=RoleAssignmentRequest(assignment_key="audit-failed",session_id=invocation,workflow_id=wf,
        aggregate_type="dag_node_run",aggregate_id=node_id,role="implementation",mode="produce",
        role_profile_id=profile,family_binding_sha=family,input_fingerprint="audit-inputs",
        required_inputs=(),input_refs={},execution_spec={"effect_type":"run_implementation_role",
            "effect_key":effect["effect_key"], "workflow_id":wf, "aggregate_type":"dag_node_run",
            "aggregate_id":node_id, "payload":effect["payload"], "business_lease":business},submission_kind="candidate")
    assignment=repo.role_assignments.create_role_assignment(req)
    aid=assignment["assignment_id"]
    attempt=repo.role_assignments.claim_role_assignment(aid,business_lease=business)
    lease=repo.leases.claim_lease("assignment:"+aid,attempt["attempt_id"])
    prompt=case.artifacts.put_json({"instruction":"audit fixture"},artifact_type="RolePromptPackArtifact")
    repo.role_attempts.start_role_attempt(assignment_id=aid,attempt_id_value=attempt["attempt_id"],
        lease_resource_key=lease.resource_key,fencing_token=lease.fencing_token,prompt_pack_ref=prompt.to_dict())
    payload={"kind":"role_assignment_failed","role":"implementation","attempt_count":3,
        "error":"worker exhausted","exhausted":True,"error_kind":"attempt_budget_exhausted"}
    failure=case.artifacts.put_json(payload,artifact_type="RoleAssignmentFailureArtifact")
    def record():
        if outcome == "failure":
            return repo.role_retries.record_role_failure_result(
                assignment_id=aid, attempt_id_value=attempt["attempt_id"],
                error_kind=payload["error_kind"], error_text=payload["error"],
                failure_artifact_ref=failure.to_dict(), payload_hash=stable_hash(payload),
                settlement_action={"action_type": "ROLE_FAILED"},
            )
        candidate = {"status": "candidate_ready"}
        ref = case.artifacts.put_json(candidate, artifact_type="CandidateRoleSubmissionArtifact")
        return repo.role_submissions.record_role_submission(
            assignment_id=aid, attempt_id_value=attempt["attempt_id"],
            fencing_token=lease.fencing_token, artifact_ref=ref.to_dict(),
            payload_hash=stable_hash(candidate), settlement_action={"action_type": "SETTLE_ROLE_SUBMISSION"},
        )

    receipt = None
    if first != "pause":
        receipt = record()
        assert repo.queries.has_pending_role_receipt(node)
        # Native process already retired; the durable result is the executor.
        if outcome == "success":
            repo.leases.release_lease(lease.resource_key, attempt["attempt_id"], lease.fencing_token)
    command = "cancel" if first == "cancel" else "pause"
    case.service.control_workflow(workflow_id=wf, command=command, actor="audit", source_channel="test")
    if first == "receipt":
        # Direct callers cannot bypass the application-level drain decision.
        with pytest.raises(DeferredEffectError, match="drain the durable role receipt"):
            repo.transitions.dispatch(ActionEnvelope(
                action_type="REQUEST_PAUSE", workflow_id=wf, aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=node_id, actor="audit", expected_version=node.version,
            ))
    reconcile_control_requests(repo, wf)
    if first == "pause":
        assert case.node(name).state == "PAUSE_REQUESTED"
        with pytest.raises(DeferredEffectError, match="fenced the role result"):
            record()
        assert not repo.role_assignments.read_role_assignment(aid)["submission_payload_hash"]
    elif first == "receipt":
        assert case.node(name).state == "PRODUCING"
        assert repo.cycles.read_graph_execution(workflow_id=wf).cycles[name].state.value == "PRODUCING"
        assert repo.role_assignments.read_role_assignment(aid)["state"] == "result_recorded"
        assert case.workflow().state == "PAUSE_REQUESTED"
        # Restart the semantic supervisor. For a failure, the original
        # durable result must take the actual background recovery path.
        service = BunshinWorkflowService(case.service.runtime_root)
        recovered = SemanticOrchestrator(service)
        components = recovered.components
        if outcome == "failure":
            with patch.object(components.attempt_execution.workspace_preparation, "execute",
                              side_effect=AssertionError("receipt replay must never start a worker")):
                assert await recovered.recover_background_assignments() == 1
                task = components.assignment_retries.background.task(effect["effect_key"])
                assert task is not None
                result = await task
                assert result["status"] == "triage_required"
            assert case.node(name).state == "TRIAGE_REQUIRED"
            assert case.node(name).payload["failure_artifact_ref"] == failure.to_dict()
            assert repo.cycles.read_graph_execution(workflow_id=wf).cycles[name].state.value == "TRIAGE_REQUIRED"
        else:
            # The real semantic submission transaction closes the receipt;
            # the earlier business-result drain tests cover its snapshotter.
            current = case.node(name)
            repo.transitions.dispatch(ActionEnvelope(
                action_type="SUBMIT_CANDIDATE", workflow_id=wf, aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=node_id, actor="audit", expected_version=current.version,
                payload={"fencing_token": current.payload["fencing_token"],
                         "producer_report_ref": receipt.artifact_ref},
            ), role_assignment_id=aid, role_submission_payload_hash=receipt.payload_hash)
            assert case.node(name).state == "QUIESCING"
            reconcile_control_requests(repo, wf)
            assert case.node(name).state == "QUIESCING"
            assert repo.role_assignments.read_role_assignment(aid)["state"] == "settled"
            return

    for child in case.nodes().values():
        if child.state in {"PAUSE_REQUESTED", "CANCEL_REQUESTED"}:
            await case.worker.execute_semantic_effect({
                "effect_type": "pause_role" if child.state == "PAUSE_REQUESTED" else "cancel_role",
                "workflow_id": wf, "aggregate_type": "dag_node_run", "aggregate_id": child.aggregate_id,
                "effect_key": "control:" + child.aggregate_id,
            })
    reconcile_control_requests(repo, wf)
    if first == "cancel":
        assert case.workflow().state == "CANCELLED"
        assert repo.role_assignments.read_role_assignment(aid)["state"] == "settled"
    elif first == "pause":
        assert case.workflow().state == "PAUSED"
        assert not repo.role_assignments.read_role_assignment(aid)["submission_payload_hash"]
    else:
        assert case.workflow().state == "PAUSED"
        assert case.node(name).state == "TRIAGE_REQUIRED"
        assert repo.role_assignments.read_role_assignment(aid)["state"] == "settled"
        case.service.resume_workflow(workflow_id=wf, actor="audit", source_channel="test")
        reconcile_control_requests(repo, wf)
        assert case.node(name).state == "TRIAGE_REQUIRED"
        assert repo.cycles.read_graph_execution(workflow_id=wf).cycles[name].state.value == "TRIAGE_REQUIRED"


def test_architect_receipt_drains_through_snapshot_before_plan_pause(tmp_path):
    from pal.bunshin.cycle_protocol import AssignmentKind, CycleSlot, PlanCycleState
    from pal.bunshin.workflow_runtime import WorkflowCoordinator

    service = BunshinWorkflowService(tmp_path)
    repo = service.repository
    wf, revision_id, session = "plan-workflow", "revision", "architect-session"
    coordinator = WorkflowCoordinator(repo)

    def dispatch(kind, identity, action, payload=None, *, work=None, **settlement):
        store = work or repo
        current = store.snapshots.read_snapshot(kind, identity)
        return store.transitions.dispatch(ActionEnvelope(
            action_type=action, workflow_id=wf, aggregate_type=kind, aggregate_id=identity,
            actor="test", expected_version=current.version if current else 0,
            payload=payload or {},
        ), **settlement).snapshot

    dispatch(AggregateType.WORKFLOW, wf, "CREATE_WORKFLOW")
    dispatch(AggregateType.WORKFLOW, wf, "START_WORKFLOW")
    ref = service.artifacts.put_json({"fixture": "plan"}, artifact_type="ContractArtifact")
    dispatch(AggregateType.ARCHITECTURE_REVISION, revision_id, "CREATE_ARCHITECTURE_REVISION")
    dispatch(AggregateType.WORKFLOW, wf, "LINK_ARCHITECTURE_REVISION", {"architecture_revision_id": revision_id})
    coordinator.start_plan_assignment(workflow_id=wf, slot=CycleSlot.PRODUCER,
                                      kind=AssignmentKind.INITIAL, input_fingerprint="plan")
    business = repo.leases.claim_lease("architecture:revision", session)
    revision = dispatch(AggregateType.ARCHITECTURE_REVISION, revision_id, "START_ARCHITECT", {
        "active_worker_id": session, "lease_resource_key": business.resource_key,
        "fencing_token": business.fencing_token,
    })
    repo.role_sessions.ensure_role_session(session_id=session, workflow_id=wf,
        aggregate_type=revision.aggregate_type, aggregate_id=revision_id, role="architect",
        mode="author", role_profile_id="architect", family_binding_sha="binding",
        scope_kind="architecture_cycle", subject_key=revision_id)
    assignment = repo.role_assignments.create_role_assignment(RoleAssignmentRequest(
        assignment_key="plan-receipt", session_id=session, workflow_id=wf,
        aggregate_type=revision.aggregate_type.value, aggregate_id=revision_id,
        role="architect", mode="author", role_profile_id="architect", family_binding_sha="binding",
        input_fingerprint="plan", required_inputs=(), input_refs={},
        execution_spec={"effect_type": "run_architect_role", "effect_key": "plan-role",
                        "workflow_id": wf, "aggregate_type": revision.aggregate_type.value, "aggregate_id": revision_id},
        submission_kind="contract"))
    aid = assignment["assignment_id"]
    attempt = repo.role_assignments.claim_role_assignment(aid)
    lease = repo.leases.claim_lease("assignment:" + aid, attempt["attempt_id"])
    repo.role_attempts.start_role_attempt(assignment_id=aid, attempt_id_value=attempt["attempt_id"],
        lease_resource_key=lease.resource_key, fencing_token=lease.fencing_token, prompt_pack_ref=ref.to_dict())
    receipt = repo.role_submissions.record_role_submission(assignment_id=aid, attempt_id_value=attempt["attempt_id"],
        fencing_token=lease.fencing_token, artifact_ref=ref.to_dict(), payload_hash=stable_hash({"fixture": "plan"}),
        settlement_action={"action_type": "SETTLE_ROLE_SUBMISSION"})

    service.control_workflow(workflow_id=wf, command="pause", actor="test", source_channel="test")
    reconcile_control_requests(repo, wf)
    assert repo.snapshots.read_snapshot(revision.aggregate_type, revision_id).state == "ARCHITECT_RUNNING"
    assert repo.cycles.read_plan_cycle(workflow_id=wf).state == PlanCycleState.PRODUCING
    dispatch(revision.aggregate_type, revision_id, "ARCHITECT_SUBMITTED", {
        "requirements_ref": ref.to_dict(), "pending_architecture_submission_ref": ref.to_dict(),
        "architecture_workspace_path": str(tmp_path), "fencing_token": business.fencing_token,
    }, role_assignment_id=aid, role_submission_payload_hash=receipt.payload_hash)
    reconcile_control_requests(repo, wf)
    assert repo.snapshots.read_snapshot(revision.aggregate_type, revision_id).state == "ARCHITECT_QUIESCING"
    assert repo.cycles.read_plan_cycle(workflow_id=wf).state == PlanCycleState.PRODUCING
    dispatch(revision.aggregate_type, revision_id, "ARCHITECT_QUIESCED", {
        "fencing_token": business.fencing_token, "process_group_reaped": True,
        "exclusive_workspace_lock": True, "workspace_fingerprint": "tree",
    })
    reconcile_control_requests(repo, wf)
    assert repo.cycles.read_plan_cycle(workflow_id=wf).state == PlanCycleState.PRODUCING
    with repo.transaction() as work:
        dispatch(revision.aggregate_type, revision_id, "ARCHITECTURE_SNAPSHOTTED", {
            "requirements_ref": ref.to_dict(), "architecture_manifest_ref": ref.to_dict(),
        }, work=work)
        coordinator.submit_plan_product(workflow_id=wf, product_ref=ref.sha256, unit_of_work=work)
    reconcile_control_requests(repo, wf)
    cycle = repo.cycles.read_plan_cycle(workflow_id=wf)
    assert cycle.state == PlanCycleState.PAUSED
    assert cycle.resume_state == PlanCycleState.CHECKER_READY
    assert repo.role_assignments.read_role_assignment(aid)["state"] == "settled"
