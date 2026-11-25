"""Prepared generations are owned by the Workflow before activation."""
from __future__ import annotations
import asyncio
import json
import pytest
from contextlib import nullcontext
from pal.bunshin.contracts import DeferredEffectError
from unittest.mock import patch
from tests import test_bunshin_execution as fixtures
from tests.test_bunshin_execution import GraphCompiler, GraphCompileBindings, RoleBinding, ARTIFACT_BUNDLE_ADAPTER, _SelectiveTestFamilyProjector
from pal.bunshin.contracts import ActionEnvelope, AggregateType
from pal.bunshin.service import BunshinWorkflowService
from pal.bunshin.workflow_runtime import WorkflowCoordinator
from pal.bunshin.workflow_control import reconcile_control_requests
from pal.bunshin.orchestration import WorkflowEffects, BunshinOutboxProcessor, _execution_epoch_id
from pal.bunshin.graph_executor import GraphExecutionState, FindingClass
from pal.bunshin.cycle_protocol import CycleSlot, AssignmentKind, CycleAction
from pal.bunshin.dag_scheduling import NodeScheduler
from pal.bunshin.graph_replanning import GraphReplanning
from pal.bunshin.semantic_orchestration.orchestrator import SemanticOrchestrator

@pytest.mark.parametrize("control", ["pause", "cancel"])
@pytest.mark.parametrize("cut", ["before-prepare", "inside-prepare", "before-activate", "after-activate"])
def test_control_closes_preparation_and_outbox_replay(control, cut):
    fixture = fixtures.BunshinExecutionTests()
    fixture.setUp()
    try:
        repo = fixture.repository
        wf = "audit-preparation"
        def snap(kind,id):
            return repo.snapshots.read_snapshot(kind,id)
        def dispatch(kind,id,action,payload=None):
            before=snap(kind,id)
            return repo.transitions.dispatch(ActionEnvelope(
                action_type=action, workflow_id=wf, aggregate_type=kind, aggregate_id=id,
                expected_version=before.version if before else 0, actor="audit", payload=payload or {},
            )).snapshot
        fixture._bind_workflow(wf)
        dispatch(AggregateType.WORKFLOW,wf,"START_WORKFLOW")
        manifest=fixture._manifest()
        old=fixture._compile_epoch(workflow_id=wf,epoch_id="source",manifest_ref=manifest)
        # Drive the predecessor through the actual paired node/cycle
        # transitions, including the compiler's real replan route.
        service=BunshinWorkflowService(fixture.runtime_root)
        coord=WorkflowCoordinator(repo)
        node_id=old.unit_node_ids["a"]
        NodeScheduler(repo).schedule_ready_nodes(workflow_id=wf,epoch_id=old.epoch_id)
        coord.start_assignment(workflow_id=wf,node_name="a",slot=CycleSlot.PRODUCER,
                               kind=AssignmentKind.INITIAL,input_fingerprint="produce-a")
        candidate=fixture.store.put_json({"candidate":"a"},artifact_type="TestArtifact")
        finding=fixture.store.put_json({"defect_kind":"contract_defect","finding_fingerprint":"contract-revision",
                                        "module_name":"a","finding_summary":"Contract revision required",
                                        "severity":"major","locations":[{"path":"a.txt"}]},
                                        artifact_type="RepairBillArtifact")
        dispatch(AggregateType.DAG_NODE_RUN,node_id,"START_PRODUCING",{"fencing_token":1})
        dispatch(AggregateType.DAG_NODE_RUN,node_id,"SUBMIT_CANDIDATE",{"fencing_token":1})
        dispatch(AggregateType.DAG_NODE_RUN,node_id,"QUIESCE_COMPLETED",{
            "fencing_token":1,"process_group_reaped":True,"exclusive_workspace_lock":True,
            "workspace_fingerprint":"tree"})
        dispatch(AggregateType.DAG_NODE_RUN,node_id,"CANDIDATE_SNAPSHOTTED",{
            "candidate_ref":candidate.to_dict(),"candidate_digest":candidate.sha256,"workspace_fingerprint":"tree"})
        coord.producer_submitted(workflow_id=wf,node_name="a",product_ref=candidate.sha256)
        NodeScheduler(repo).schedule_ready_nodes(workflow_id=wf,epoch_id=old.epoch_id)
        dispatch(AggregateType.DAG_NODE_RUN,node_id,"START_REVIEW",{"fencing_token":2})
        coord.start_assignment(workflow_id=wf,node_name="a",slot=CycleSlot.CHECKER,
                               kind=AssignmentKind.INITIAL,input_fingerprint="check-a")
        dispatch(AggregateType.DAG_NODE_RUN,node_id,"SUBMIT_SEMANTIC_VERIFICATION",
                 {"pending_verification_ref":finding.to_dict()})
        dispatch(AggregateType.DAG_NODE_RUN,node_id,"VERIFIER_QUIESCED",{
            "fencing_token":2,"process_group_reaped":True,"exclusive_workspace_lock":True,
            "workspace_fingerprint":"review-tree"})
        dispatch(AggregateType.DAG_NODE_RUN,node_id,"CONTRACT_DEFECT",{"repair_bill_ref":finding.to_dict()})
        coord.checker_verdict(workflow_id=wf,node_name="a",accepted=False,finding_refs=[finding.sha256],
                              finding_class=FindingClass.CONTRACT_DEFECT)
        replanning=GraphReplanning(service)
        replanning.request_epoch_replan({"aggregate_type":"dag_node_run","aggregate_id":node_id,
                                        "effect_key":"finding"})
        replanning.freeze_epoch_for_replan({"aggregate_type":"execution_epoch","aggregate_id":"source",
                                             "effect_key":"freeze"})
        assert snap(AggregateType.EXECUTION_EPOCH,"source").state=="REPLAN_REQUIRED"
        assert repo.cycles.read_graph_execution(workflow_id=wf).state==GraphExecutionState.REPLAN_REQUIRED
        # Authorize generation two via actual review/decision transitions,
        # retaining the real durable submit_action effect across interruption.
        payload=fixture.store.read_json(manifest)
        graph=GraphCompiler().compile(payload["contract"],graph_id=wf,generation=2,
            bindings=GraphCompileBindings(producer=RoleBinding("profile","generic"),
                checker=RoleBinding("profile","general.verifier"),execution_adapter=ARTIFACT_BUNDLE_ADAPTER),
            satellite_projector=_SelectiveTestFamilyProjector(),source_ref="architect.yaml",workspace_authority_rules=())
        new_manifest=fixture.store.put_json({**payload,"graph_ir":graph.to_dict()},artifact_type="ContractArtifact")
        successor_id=_execution_epoch_id(workflow_id=wf,manifest_sha=new_manifest.sha256,source_epoch_id="source")
        revision_id="approved-revision"
        dispatch(AggregateType.ARCHITECTURE_REVISION,revision_id,"CREATE_ARCHITECTURE_REVISION",
            {"source_execution_epoch_id":"source","requirements_ref":payload["requirements_ref"]})
        dispatch(AggregateType.WORKFLOW,wf,"LINK_ARCHITECTURE_REVISION",{"architecture_revision_id":revision_id})
        coord.start_plan_assignment(workflow_id=wf,slot=CycleSlot.PRODUCER,kind=AssignmentKind.INITIAL,input_fingerprint="plan")
        dispatch(AggregateType.ARCHITECTURE_REVISION,revision_id,"START_ARCHITECT",{"fencing_token":1})
        dispatch(AggregateType.ARCHITECTURE_REVISION,revision_id,"DATA_ARCHITECT_COMPLETED",
            {"requirements_ref":payload["requirements_ref"],"architecture_manifest_ref":new_manifest.to_dict()})
        coord.submit_plan_product(workflow_id=wf,product_ref=new_manifest.sha256)
        coord.start_plan_assignment(workflow_id=wf,slot=CycleSlot.CHECKER,kind=AssignmentKind.INITIAL,input_fingerprint="review")
        dispatch(AggregateType.ARCHITECTURE_REVISION,revision_id,"START_ARCHITECTURE_REVIEW",{"fencing_token":2})
        dispatch(AggregateType.ARCHITECTURE_REVISION,revision_id,"ARCHITECTURE_REVIEW_PASSED",
            {"review_artifact_ref":finding.to_dict(),"architecture_manifest_ref":new_manifest.to_dict()})
        coord.submit_plan_verdict(workflow_id=wf,accepted=True)
        token=repo.human_decisions.issue_human_decision_token(workflow_id=wf,architecture_revision_id=revision_id,
                                                             manifest_sha=new_manifest.sha256,actor_id="audit")
        dispatch(AggregateType.ARCHITECTURE_REVISION,revision_id,"HUMAN_ACCEPT",
            {"decision_token":token,"architecture_manifest_ref":new_manifest.to_dict()})
        coord.transition_plan(workflow_id=wf,action=CycleAction.HUMAN_ACCEPTED)
        with repo.database.read_connection() as connection:
            row=connection.execute("SELECT * FROM bunshin_outbox WHERE aggregate_id=? AND effect_type='submit_action'",(revision_id,)).fetchone()
            compile_effect=dict(row);compile_effect["payload"]=json.loads(compile_effect.pop("payload_json"))
        assert compile_effect["payload"]["action_type"]=="START_EXECUTION"
        effects=WorkflowEffects(service)
        def interrupt_after_install(self, *args, **kwargs):
            original_install(self, *args, **kwargs)
            raise RuntimeError("injected preparation crash")

        original_install = WorkflowCoordinator.install_graph
        if cut == "before-prepare":
            interruption = patch.object(WorkflowCoordinator, "begin_graph_activation",
                                        side_effect=RuntimeError("injected preparation crash"))
        elif cut == "inside-prepare":
            interruption = patch.object(WorkflowCoordinator, "install_graph", interrupt_after_install)
        elif cut == "before-activate":
            interruption = patch("pal.bunshin.data_contract_compilation.reconcile_module_identities",
                                 side_effect=RuntimeError("injected preparation crash"))
        else:
            interruption = nullcontext()
        with interruption:
            if cut == "after-activate":
                asyncio.run(effects.execute_effect(compile_effect))
            else:
                with pytest.raises(RuntimeError, match="injected preparation crash"):
                    asyncio.run(effects.execute_effect(compile_effect))
        if cut in {"before-prepare", "inside-prepare"}:
            assert snap(AggregateType.EXECUTION_EPOCH, successor_id) is None
            assert repo.cycles.read_graph_execution(workflow_id=wf).graph.generation == 1
        elif cut == "before-activate":
            assert snap(AggregateType.EXECUTION_EPOCH, successor_id).state == "STARTING"
        else:
            assert snap(AggregateType.EXECUTION_EPOCH, successor_id).state == "RUNNING"
        service=BunshinWorkflowService(fixture.runtime_root)
        repo=service.repository
        service.control_workflow(workflow_id=wf, command=control, actor="audit", source_channel="test")
        if cut == "before-activate":
            # Even before propagation, the parent is the activation fence.
            with pytest.raises(DeferredEffectError, match="fences graph preparation"):
                WorkflowCoordinator(repo).activate_graph(
                    workflow_id=wf, epoch_id=successor_id, actor="audit", module_identity_delta={},
                    source_epoch_id="source",
                )
        reconcile_control_requests(repo, wf)
        semantic = SemanticOrchestrator(service)
        for node in repo.queries.list_workflow_snapshots(wf):
            if node.aggregate_type == AggregateType.DAG_NODE_RUN and node.state in {"PAUSE_REQUESTED", "CANCEL_REQUESTED"}:
                asyncio.run(semantic.execute_semantic_effect({
                    "effect_type": "pause_role" if node.state == "PAUSE_REQUESTED" else "cancel_role",
                    "workflow_id": wf, "aggregate_type": "dag_node_run", "aggregate_id": node.aggregate_id,
                    "effect_key": "control:" + node.aggregate_id,
                }))
        reconcile_control_requests(repo, wf)
        terminal = "PAUSED" if control == "pause" else "CANCELLED"
        assert snap(AggregateType.WORKFLOW, wf).state == terminal
        prepared = snap(AggregateType.EXECUTION_EPOCH, successor_id)
        if prepared is not None:
            assert prepared.state == terminal
        claim = next(e for e in repo.outbox_claims.claim_outbox("audit-recovery", limit=1000)
                     if e["effect_id"] == compile_effect["effect_id"])
        processor = BunshinOutboxProcessor(service, semantic_effects=semantic, worker_id="audit-recovery")
        result = asyncio.run(processor._process_effect(claim))
        expected = "deferred" if control == "pause" and cut != "after-activate" else "completed"
        assert result == expected
        assert snap(AggregateType.EXECUTION_EPOCH, successor_id) == prepared
        if control == "pause":
            service.resume_workflow(workflow_id=wf, actor="audit", source_channel="test")
            if cut != "after-activate":
                with pytest.raises(DeferredEffectError):
                    asyncio.run(processor.execute_effect(compile_effect))
            reconcile_control_requests(repo, wf)
            if cut != "after-activate":
                asyncio.run(processor.execute_effect(compile_effect))
            active = snap(AggregateType.WORKFLOW, wf)
            assert active.state == "ACTIVE"
            assert active.payload["execution_epoch_id"] == successor_id
            assert snap(AggregateType.EXECUTION_EPOCH, successor_id).state == "RUNNING"
            assert snap(AggregateType.EXECUTION_EPOCH, "source").state == "SUPERSEDED"
        else:
            assert all(item.state != "RUNNING" for item in repo.queries.list_workflow_snapshots(wf)
                       if item.aggregate_type == AggregateType.EXECUTION_EPOCH)
    finally:
        fixture.tearDown()
