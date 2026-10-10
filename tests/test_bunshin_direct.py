from pathlib import Path

import pytest

from pal.bunshin.contracts import ActionEnvelope, AggregateType
from pal.bunshin.orchestration import BunshinOutboxProcessor
from pal.bunshin.service import BunshinWorkflowService


def direct_workflow(tmp_path: Path, *, kind="existing_repo", references=None):
    repo = tmp_path / "source"
    repo.mkdir()
    if kind == "existing_repo":
        (repo / "app.py").write_text("print('hello')\n")
    if references:
        from pal.bunshin.skeleton import _git
        _git(repo, "init")
        _git(repo, "add", "app.py")
        _git(repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "Baseline")
    service = BunshinWorkflowService(tmp_path / "runtime")
    task = service.create_task({"title": "Investigate", "objective": "Find the cause",
        "profile": "software_engineering.v2_coder", "workspace": {
            "kind": kind, "repo_path": str(repo), "primary_language": "python"}})
    result = service.start_workflow({"task_id": task["task_id"], "goal": "Investigate",
        "delivery_binding": {"channel_id": "test", "channel_kind": "socket", "reply_target": {"session_id": "test"}},
        "execution_mode": "direct", "references": references or [], "task_spec": {"objective": "Find cause", "deliverable_paths": ["report.md"]}})
    workflow_id = result["workflow_id"]
    snapshot = service.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
    service.repository.transitions.dispatch(ActionEnvelope(action_type="START_WORKFLOW",
        workflow_id=workflow_id, aggregate_type=AggregateType.WORKFLOW, aggregate_id=workflow_id,
        actor="test", expected_version=snapshot.version, idempotency_key="start"))
    processor = BunshinOutboxProcessor(service)
    effect = {"aggregate_type": "workflow", "aggregate_id": workflow_id, "workflow_id": workflow_id,
              "effect_key": "route", "payload": {}}
    processor._route_workflow(effect)
    return service, processor, workflow_id


def test_direct_compiles_one_repository_node_without_architecture(tmp_path):
    service, processor, workflow_id = direct_workflow(tmp_path)
    snapshots = service.repository.queries.list_workflow_snapshots(workflow_id)
    assert not any(s.aggregate_type == AggregateType.ARCHITECTURE_REVISION for s in snapshots)
    nodes = [s for s in snapshots if s.aggregate_type == AggregateType.DAG_NODE_RUN]
    assert len(nodes) == 1
    assert nodes[0].payload["execution_mode"] == "direct"
    assert nodes[0].payload["module_name"] == "repository"
    assert Path(nodes[0].payload["workspace_path"], "app.py").read_text() == "print('hello')\n"
    assert service.repository.cycles.read_plan_cycle(workflow_id=workflow_id) is None


def node_for(service, workflow_id):
    return next(s for s in service.repository.queries.list_workflow_snapshots(workflow_id)
                if s.aggregate_type == AggregateType.DAG_NODE_RUN)


def test_clarification_rebinds_task_preserving_workspace(tmp_path):
    from pal.bunshin.work_views import UnitWorkViewBuilder
    from pal.bunshin.workflow_runtime import WorkflowCoordinator
    service, processor, workflow_id = direct_workflow(tmp_path)
    node = node_for(service, workflow_id)
    workspace = Path(node.payload["workspace_path"])
    (workspace / "in-progress.txt").write_text("preserve this draft")
    old_view = UnitWorkViewBuilder(service.contracts).build(node)
    finding = service.artifacts.put_json({"summary": "Which log interval?"}, artifact_type="ModuleCoderReport")
    WorkflowCoordinator(service.repository).require_node_triage(workflow_id=workflow_id, node_name="repository")
    service.repository.transitions.dispatch(ActionEnvelope(action_type="ENTER_TRIAGE", workflow_id=workflow_id,
        aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=node.aggregate_id, actor="test",
        expected_version=node.version, idempotency_key="blocker",
        payload={"blocker": {"kind": "task_requirement", "finding_ref": finding.to_dict()}}))
    answer = "  Use October 1–3.\nKeep timestamps.  "
    result = service.resolve_triage(workflow_id=workflow_id, actor="pal", source_channel="test", resolution=answer)
    assert result["state"] == "QUEUED"
    resumed = node_for(service, workflow_id)
    assert resumed.aggregate_id == node.aggregate_id
    assert resumed.payload["workspace_path"] == str(workspace)
    assert (workspace / "in-progress.txt").read_text() == "preserve this draft"
    view = UnitWorkViewBuilder(service.contracts).build(resumed)
    assert view.sha256 != old_view.sha256
    ledger = service.artifacts.read_json(service.artifacts.read_json(view)["requirements_ref"])
    assert ledger["revisions"][-1]["authority"]["answer"] == answer
    execution = service.repository.cycles.read_graph_execution(workflow_id=workflow_id)
    assert execution.graph.generation == 2
    assert execution.cycles["repository"].state == "PRODUCER_READY"
    assert execution.cycles["repository"].last_verdict is None


@pytest.mark.parametrize("path,allowed", [
    ("app.py", True), ("src/deep/module.py", True), ("README.md", True),
    ("tests/test_app.py", True), ("tests/repository/developer/test_case.py", True),
    (".git/config", False), ("inputs/log.txt", False), ("../escape", False),
    ("/tmp/escape", False), ("tests/repository/verifier/test_case.py", False),
    ("coder_report.json", False), (".pal-bunshin-architect/architect.yaml", False),
])
def test_repository_scope_protects_control_and_verifier_paths(path, allowed):
    from pal.bunshin.workspace_paths import path_scope_matches
    assert path_scope_matches(path, {"kind": "repository", "path": "."}) is allowed


def test_direct_delivery_reads_verified_commit_and_retains_code_patch(tmp_path):
    from pal.bunshin.delivery import DeliveryService, DirectDeliveryReceipt
    from pal.bunshin.skeleton import _git
    service, _, workflow_id = direct_workflow(tmp_path)
    node = node_for(service, workflow_id)
    repo = Path(node.payload["workspace_path"])
    manifest = service.artifacts.read_json(node.payload["architecture_manifest_ref"])
    snapshot = service.artifacts.read_json(manifest["workspace_snapshot_ref"])
    (repo / "report.md").write_text("# Verified cause\nEvidence: trace 123.\n")
    _git(repo, "add", "report.md")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "Report")
    commit = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "report.md").write_text("unverified dirty content")
    verification = service.artifacts.put_json({"status": "pass"}, artifact_type="VerificationArtifact")
    delivery = DeliveryService(service.runtime_root, service.artifacts)
    args = dict(workflow_id=workflow_id, workflow_key="direct", task_title="Investigation", repository=repo,
                commit_sha=commit, source_snapshot=snapshot, verification_ref=verification,
                deliverable_paths=["report.md"])
    ref = delivery.publish_direct(**args)
    receipt = DirectDeliveryReceipt.model_validate(service.artifacts.read_json(ref))
    assert receipt.patch_receipt is None
    assert service.artifacts.read_bytes(receipt.files[0].artifact_ref).startswith(b"# Verified cause")
    (repo / "app.py").write_text("print('fixed')\n")
    _git(repo, "add", "app.py")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "Fix")
    args["commit_sha"] = _git(repo, "rev-parse", "HEAD").strip()
    args["workflow_id"] = workflow_id + "-second-delivery"
    combined = DirectDeliveryReceipt.model_validate(service.artifacts.read_json(delivery.publish_direct(**args)))
    assert combined.patch_receipt is not None
    assert combined.files[0].content_sha256 == receipt.files[0].content_sha256
    events = []
    workflow = service.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
    service.repository.transitions.dispatch(ActionEnvelope(action_type="MARK_COMPLETED", workflow_id=workflow_id,
        aggregate_type=AggregateType.WORKFLOW, aggregate_id=workflow_id, actor="test", expected_version=workflow.version,
        idempotency_key="completed", payload={"result_artifact_ref": ref.to_dict()}))
    processor = BunshinOutboxProcessor(service, publish_workflow_event=events.append)
    (repo / "report.md").unlink()
    processor._publish_terminal_workflow_if_any(workflow_id)
    assert len(events[0]["attachments"]) == 1
    attachment = events[0]["attachments"][0]
    assert attachment["file_name"] == "report.md"
    assert Path(attachment["path"]).read_text().startswith("# Verified cause")


def test_direct_final_delivery_pins_accepted_candidate_when_head_moves(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace
    from pal.bunshin.delivery import DirectDeliveryReceipt
    from pal.bunshin.semantic_orchestration.final_delivery import FinalDelivery
    from pal.bunshin.workspace_git import _git

    service, _, workflow_id = direct_workflow(tmp_path)
    node = node_for(service, workflow_id)
    repo = Path(node.payload["workspace_path"])
    report = repo / "report.md"
    report.write_text("Verified evidence\n")
    _git(repo, "add", "report.md")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "Verified report")
    accepted_commit = _git(repo, "rev-parse", "HEAD").strip()
    accepted = replace(node, state="ACCEPTED", payload={**node.payload, "candidate_digest": accepted_commit})
    report.write_text("Later unverified evidence\n")
    _git(repo, "add", "report.md")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "Later edit")
    epoch = service.repository.snapshots.read_snapshot(AggregateType.EXECUTION_EPOCH, node.payload["epoch_id"])
    verification = service.artifacts.put_json({"status": "pass"}, artifact_type="VerificationArtifact")
    publisher = FinalDelivery(effect_reads=None, workflow_facts=None, artifacts=service.artifacts,
        repository=service.repository, requests=SimpleNamespace(read=lambda _: {"goal": "Investigate"}),
        runtime_root=service.runtime_root)
    ref = publisher.publish_verified_git_delivery(epoch=epoch, delivery_node=accepted,
        repository=repo, verification_ref=verification)
    receipt = DirectDeliveryReceipt.model_validate(service.artifacts.read_json(ref))
    assert receipt.commit_sha == accepted_commit
    assert service.artifacts.read_bytes(receipt.files[0].artifact_ref) == b"Verified evidence\n"
    with pytest.raises(ValueError, match="accepted Candidate commit"):
        publisher.publish_verified_git_delivery(epoch=epoch, delivery_node=node,
            repository=repo, verification_ref=verification)


def test_direct_replay_is_idempotent_and_uses_original_source(tmp_path):
    service, processor, workflow_id = direct_workflow(tmp_path)
    node = node_for(service, workflow_id)
    (tmp_path / "source" / "app.py").write_text("source changed")
    processor._route_workflow({"aggregate_type": "workflow", "aggregate_id": workflow_id,
        "workflow_id": workflow_id, "effect_key": "route", "payload": {}})
    assert node_for(service, workflow_id).aggregate_id == node.aggregate_id
    assert Path(node.payload["workspace_path"], "app.py").read_text() == "print('hello')\n"


def test_profiles_share_engineering_discipline_but_direct_task_is_authority(tmp_path):
    from pal.bunshin.profiles import BunshinProfile
    from pal.bunshin.semantic_orchestration.role_inputs import _role_mode_profile_payload
    service, _, workflow_id = direct_workflow(tmp_path)
    workflow = service.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
    binding = service.artifacts.read_json(workflow.payload["family_binding_ref"])
    for role, mode in (("implementation", "produce"), ("verifier", "module")):
        pinned = binding["role_bindings"][role]["role_profile"]
        direct = _role_mode_profile_payload(pinned, mode=mode, execution_mode="direct")
        profile = BunshinProfile.from_dict(direct)
        shared = pinned["metadata"]["engineering_fragment"].strip()
        assert shared in profile.behavior_fragment
        assert "Accepted Skeleton" not in profile.behavior_fragment
        assert direct["role"]["truth_sources"][0]["authority"] == "normative"
        assert direct["role"]["truth_sources"][0]["source"] == "task_ledger"
        assert profile.behavior_fragment.count(shared) == 1
        legacy = {**pinned, "metadata": {}}
        with pytest.raises(ValueError, match="does not support"):
            _role_mode_profile_payload(legacy, mode=mode, execution_mode="direct")


def test_direct_report_policy_requires_evidence_without_fabricated_entrypoint():
    from pal.bunshin.verification_builder import effective_verification_policy
    policy = effective_verification_policy(work_view={"execution_mode": "direct", "graph_sink": True},
        verification_policy={"require_warning_clean": True, "lsp_policy": "when_available"},
        system_delivery_view={"report_only": True})
    assert policy["require_public_surface_dogfood"] is False
    assert "focused_tests" in policy["allowed_obligations"]
    assert "lsp" not in policy["allowed_obligations"]


@pytest.mark.parametrize("defect", ["requirements_defect", "contract_defect", "architecture_defect"])
def test_direct_verifier_task_defect_returns_to_pal(tmp_path, defect):
    from pal.bunshin.verification import VerificationService, VerificationStatus, DefectKind
    service, _, workflow_id = direct_workflow(tmp_path)
    node = node_for(service, workflow_id)
    report = service.artifacts.put_json({"findings": [{"finding_kind": defect, "summary": "conflicting requirement"}]},
                                      artifact_type="VerificationArtifact")
    repair = service.artifacts.put_json({"verification_ref": report.to_dict()}, artifact_type="RepairPacketArtifact")
    result = VerificationService(service.repository, service.artifacts).submit_verdict(
        node=node, verification_ref=report, status=VerificationStatus.FAIL, actor="test",
        repair_bill_ref=repair, finding_fingerprint_value="finding", defect_kind=DefectKind(defect))
    assert result.snapshot.state == "TRIAGE_REQUIRED"
    assert result.snapshot.payload["blocker"]["kind"] == "task_requirement"
    assert service.repository.cycles.read_plan_cycle(workflow_id=workflow_id) is None


def test_direct_source_finding_routes_to_coder_and_reverification(tmp_path):
    from pal.bunshin.cycle_protocol import AssignmentKind, CycleSlot
    from pal.bunshin.graph_executor import FindingClass
    from pal.bunshin.semantic_orchestration.verification_policy import _verification_repair_scope
    from pal.bunshin.swe_verification import infer_repair_target_modules, verification_finding_route_errors
    from pal.bunshin.workflow_runtime import WorkflowCoordinator

    service, _, workflow_id = direct_workflow(tmp_path)
    node = node_for(service, workflow_id)
    scope = _verification_repair_scope(service.repository, node)
    finding = {"finding_key": "wrong-output", "finding_kind": "module_defect",
               "locations": [{"scope": "workspace", "file": "app.py", "line": 1}]}
    assert infer_repair_target_modules([finding], scope["repair_path_owners"]) == ["repository"]
    assert verification_finding_route_errors([finding], scope) == []
    coordinator = WorkflowCoordinator(service.repository)
    for iteration, kind in enumerate((AssignmentKind.INITIAL, AssignmentKind.REPAIR)):
        coordinator.start_assignment(workflow_id=workflow_id, node_name="repository",
            slot=CycleSlot.PRODUCER, kind=kind, input_fingerprint=f"task-{iteration}")
        coordinator.producer_submitted(workflow_id=workflow_id, node_name="repository",
            product_ref=f"candidate-{iteration}")
        coordinator.start_assignment(workflow_id=workflow_id, node_name="repository",
            slot=CycleSlot.CHECKER, kind=kind, input_fingerprint=f"candidate-{iteration}")
        coordinator.checker_verdict(workflow_id=workflow_id, node_name="repository",
            accepted=bool(iteration), finding_refs=() if iteration else ("wrong-output",),
            finding_class=None if iteration else FindingClass.MODULE_DEFECT)
        if not iteration:
            assignments = coordinator.runnable_assignments(workflow_id=workflow_id)
            assert len(assignments) == 1
            assert assignments[0].node_name == "repository"
            assert assignments[0].slot == CycleSlot.PRODUCER
    assert coordinator.published_sink_ref(workflow_id=workflow_id) == "candidate-1"
    assert service.repository.cycles.read_plan_cycle(workflow_id=workflow_id) is None


def test_direct_restart_uses_direct_request_and_original_baseline(tmp_path):
    from pal.bunshin.direct_execution import prepare_direct_execution
    from pal.bunshin.service import workflow_request_from_snapshot
    service, _, workflow_id = direct_workflow(tmp_path)
    node = node_for(service, workflow_id)
    old_manifest = service.artifacts.read_json(node.payload["architecture_manifest_ref"])
    (tmp_path / "source" / "app.py").write_text("changed since original")
    service.restart_execution_from_architecture(workflow_id=workflow_id, actor="pal", source_channel="test", reason="retry")
    workflow = service.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
    restart = workflow.payload["restart_execution_request"]
    assert restart["execution_mode"] == "direct"
    assert restart["operation"] == "new_requirement"
    request = workflow_request_from_snapshot(service, workflow)
    new = prepare_direct_execution(service, workflow_id + "-replacement", request, base_artifact=old_manifest)
    manifest = service.artifacts.read_json(new)
    assert manifest["workspace_snapshot_ref"] == old_manifest["workspace_snapshot_ref"]
    assert manifest["base_commit_sha"] == old_manifest["base_commit_sha"]


def test_coder_and_verifier_receive_identical_original_task(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace
    from pal.bunshin.semantic_orchestration.implementation_run import ImplementationRun
    from pal.bunshin.semantic_orchestration.verification_run import VerificationRun
    from pal.bunshin.semantic_orchestration.workflow_facts import WorkflowFacts
    from pal.bunshin.skeleton import _git
    from pal.bunshin.adapters import SOFTWARE_GIT_ADAPTER
    log = tmp_path / "evidence.log"
    log.write_text("original trace")
    service, _, workflow_id = direct_workflow(tmp_path, references=[{"name": "trace", "path": str(log)}])
    log.unlink()
    node = node_for(service, workflow_id)
    context = SimpleNamespace(contracts=service.contracts, artifacts=service.artifacts,
                              workflow_facts=WorkflowFacts(service.artifacts, service.repository))
    _, _, _, coder_refs, view_ref, _ = ImplementationRun.prepare_implementation_inputs(context, node, False, True)
    workspace = Path(node.payload["workspace_path"])
    head = _git(workspace, "rev-parse", "HEAD").strip()
    candidate = service.artifacts.put_json({"base_sha": head, "changed_paths": []}, artifact_type="CandidateArtifact")
    node = replace(node, payload={**node.payload, "candidate_ref": candidate.to_dict(), "candidate_digest": head,
                                  "unit_work_view_ref": view_ref.to_dict()})
    _, _, verifier_refs, _ = VerificationRun.prepare_verifier_inputs(context, SOFTWARE_GIT_ADAPTER, head, candidate, node, workspace)
    assert coder_refs["task"] == verifier_refs["task"]
    assert coder_refs["module_work_view"] == verifier_refs["module_work_view"]
    assert coder_refs["source_0_trace"] == verifier_refs["source_0_trace"]
    assert service.artifacts.read_bytes(coder_refs["source_0_trace"]) == b"original trace"


def test_direct_capabilities_expose_task_blocker_without_architect_escalation():
    from pal.shared import BunshinInvocationPack
    from pal.bunshin.semantic_orchestration.role_policy import apply_role_capability_policy
    from pal.bunshin.role_contracts import OrchestrationRole, RoleActivation, RoleMode
    activation = RoleActivation(OrchestrationRole.IMPLEMENTATION, RoleMode.PRODUCE)
    pack = BunshinInvocationPack(invocation_id="test", goal="fix", profile_group="software_engineering",
        workspace={"execution_mode": "direct"})
    direct = apply_role_capability_policy(pack, activation=activation)
    assert "op_bunshin_candidate_report_task_blocker" in direct.allowed_capabilities
    assert "op_bunshin_candidate_report_architecture_defect" not in direct.allowed_capabilities
    assert "op_bunshin_candidate_request_module_split" not in direct.allowed_capabilities


@pytest.mark.parametrize("path", ["../report.md", "/tmp/report.md", ".git/config", "inputs/log.txt", "tests/repository/verifier/test.py"])
def test_deliverable_paths_reject_unsafe_and_reserved_targets(path):
    from pal.bunshin.direct_contract import deliverable_paths
    with pytest.raises(ValueError):
        deliverable_paths({"deliverable_paths": [path]})


def test_direct_new_project_creates_repository_workspace(tmp_path):
    service, _, workflow_id = direct_workflow(tmp_path, kind="new_project")
    node = node_for(service, workflow_id)
    assert node.payload["execution_mode"] == "direct"
    assert Path(node.payload["workspace_path"]).is_dir()
    assert not list(Path(node.payload["workspace_path"]).glob("architect.yaml"))


def test_direct_restart_rebinds_captured_inputs_without_recapturing_changed_source(tmp_path):
    service, _, workflow_id = direct_workflow(tmp_path, references=[{"name": "evidence", "path": "app.py"}])
    node = node_for(service, workflow_id)
    manifest_ref = node.payload["architecture_manifest_ref"]
    manifest = service.artifacts.read_json(manifest_ref)
    original = service.artifacts.read_json(manifest["input_binding_ref"])
    (tmp_path / "source" / "app.py").unlink()
    task = service.create_task({"title": "Replacement", "objective": "Investigate", "profile": "software_engineering.v2_coder",
        "workspace": {"kind": "existing_repo", "repo_path": str(tmp_path / "source"), "primary_language": "python"}})
    result = service.start_workflow({"task_id": task["task_id"], "goal": "Investigate", "execution_mode": "direct",
        "artifact_ref": manifest_ref, "requirements_ref": manifest["requirements_ref"],
        "references": [{"name": "evidence", "path": "app.py"}],
        "delivery_binding": {"channel_id": "test", "channel_kind": "socket", "reply_target": {"session_id": "test"}}})
    workflow = service.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, result["workflow_id"])
    rebound = service.artifacts.read_json(workflow.payload["input_binding_ref"])
    assert rebound["workflow_id"] == workflow.workflow_id
    assert rebound["inputs"] == original["inputs"]


def test_source_attachment_does_not_waive_code_checks():
    from pal.bunshin.direct_contract import report_only_changes
    assert not report_only_changes(["app.py"], ["app.py"])
    assert report_only_changes(["report.md"], ["report.md", "tests/repository/verifier/test_probe.py"])
