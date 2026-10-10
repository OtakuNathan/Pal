"""Recovery obligations and stable semantic identities across retries."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from pal.bunshin.contracts import ActionEnvelope, AggregateType
from pal.bunshin.orchestration import BunshinOutboxProcessor
from pal.bunshin.service import BunshinWorkflowService
from tests.test_bunshin_direct import direct_workflow, node_for


def new_workflow(tmp_path, mode):
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "app.py").write_text("print(1)\n")
    service = BunshinWorkflowService(tmp_path / "runtime")
    task = service.create_task({"title": "Recovery", "objective": "Check recovery",
        "profile": "software_engineering.v2_coder",
        "workspace": {"kind": "existing_repo", "repo_path": str(repo)}})
    result = service.start_workflow({"task_id": task["task_id"], "goal": "Check recovery", "execution_mode": mode,
        "task_spec": {"objective": "Check recovery"},
        "delivery_binding": {"channel_id": "test", "channel_kind": "socket", "reply_target": {"session_id": "test"}}})
    return service, BunshinOutboxProcessor(service), result["workflow_id"]


@pytest.mark.parametrize("mode", ["direct", "planned"])
@pytest.mark.parametrize("started", [False, True])
def test_early_pause_resume_restores_start_obligation(tmp_path, mode, started):
    async def scenario():
        service, processor, workflow_id = new_workflow(tmp_path, mode)
        if started:
            await processor.process_once(limit=1)  # START_WORKFLOW, before routing.
        service.control_workflow(workflow_id=workflow_id, command="pause", actor="test", source_channel="test")
        for _ in range(10):
            if not (await processor.process_once(limit=1))["claimed"]:
                break
        workflow = service.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        assert workflow.state == "PAUSED"
        assert all(item.aggregate_type == AggregateType.WORKFLOW
                   for item in service.repository.queries.list_workflow_snapshots(workflow_id))
        service.resume_workflow(workflow_id=workflow_id, actor="test", source_channel="test")
        effects = service.repository.outbox_claims.claim_outbox(processor.worker_id, limit=1, lease_seconds=60)
        assert len(effects) == 1 and effects[0]["effect_type"] == "propagate_resume"
        assert await processor._process_effect(effects[0]) == "completed"
        workflow = service.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        assert workflow.state == "ACTIVE"
        child_field = "execution_epoch_id" if mode == "direct" else "architecture_revision_id"
        assert workflow.payload[child_field]
        children = service.repository.queries.list_workflow_snapshots(workflow_id)
        # Reconciliation replay must reuse the same children and startup work.
        await processor._execute_mechanical(effects[0])
        assert service.repository.queries.list_workflow_snapshots(workflow_id) == children
        processor._route_workflow({**effects[0], "effect_key": "late-original-start"})
        assert service.repository.queries.list_workflow_snapshots(workflow_id) == children
        pending = service.repository.outbox_claims.claim_outbox(processor.worker_id, limit=20, lease_seconds=60)
        assert any(item["effect_type"] in {"schedule_ready_nodes", "admit_implementation_role", "admit_architect_role"}
                   for item in pending)
    asyncio.run(scenario())


def failure_receipts(round_number, output="Expected 2, got 1"):
    failure = {"kind": "command", "tool_name": "op_exec_shell", "ok": False,
        "output_text": output, "output_sha256": f"envelope-{round_number}",
        "evidence_ref_id": f"receipt-{round_number}", "structured": {"exit_code": 1, "pid": round_number}}
    return [failure] * (round_number + 1) + [
        {"kind": "command", "ok": True, "output_text": f"successful-check-{round_number}"},
        {"kind": "test_write", "ok": True, "output_sha256": f"write-{round_number}"},
    ]


def test_unchanged_failures_trip_no_progress_after_three_rounds(tmp_path):
    from pal.bunshin.semantic_orchestration.verification_settlement import _publish_repair_evidence
    from pal.bunshin.verification import VerificationService, VerificationStatus

    service, _, workflow_id = direct_workflow(tmp_path)
    node_id = node_for(service, workflow_id).aggregate_id
    artifacts = service.artifacts
    ref = artifacts.put_json({"candidate_tree_sha": "same-tree"}, artifact_type="CandidateArtifact")

    def dispatch(action, payload):
        node = node_for(service, workflow_id)
        return service.repository.transitions.dispatch(ActionEnvelope(action_type=action, workflow_id=workflow_id,
            aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=node_id, expected_version=node.version,
            actor="test", idempotency_key=f"{action}:{node.version}", payload=payload)).snapshot

    if node_for(service, workflow_id).state == "BLOCKED_BY_DEPS":
        dispatch("DEPENDENCIES_ACCEPTED", {"accepted_producer_dependency_node_ids": [], "epoch_frozen": False})
    for iteration in range(3):
        dispatch("START_PRODUCING" if not iteration else "START_REPAIR", {"fencing_token": 1})
        dispatch("SUBMIT_CANDIDATE", {"fencing_token": 1})
        dispatch("QUIESCE_COMPLETED", {"fencing_token": 1, "process_group_reaped": True,
            "exclusive_workspace_lock": True, "workspace_fingerprint": "same-tree"})
        dispatch("CANDIDATE_SNAPSHOTTED", {"candidate_ref": ref.to_dict(), "candidate_digest": "candidate",
            "workspace_fingerprint": "same-tree"})
        dispatch("VERIFICATION_DEPENDENCIES_ACCEPTED", {"accepted_dependency_node_ids": [], "epoch_frozen": False})
        dispatch("START_REVIEW", {"fencing_token": 2})
        dispatch("SUBMIT_SEMANTIC_VERIFICATION", {"pending_verification_ref": ref.to_dict()})
        node = dispatch("VERIFIER_QUIESCED", {"fencing_token": 2, "process_group_reaped": True,
            "exclusive_workspace_lock": True, "workspace_fingerprint": "same-tree"})
        findings = [{"finding_id": f"finding-{iteration}", "finding_kind": "module_defect",
            "summary": "Expected 2, got 1", "locations": [{"scope": "workspace", "file": "app.py", "line": 1}]}]
        defect, fingerprint, _, _, repair, _ = _publish_repair_evidence(
            artifacts, service.repository, {"candidate_tree_sha": "same-tree"}, "candidate", ref,
            [], findings, node, "repair", failure_receipts(iteration), ref, ref, VerificationStatus.FAIL, {})
        result = VerificationService(service.repository, artifacts).submit_verdict(
            node=node, verification_ref=ref, status=VerificationStatus.FAIL, actor="test", repair_bill_ref=repair,
            finding_fingerprint_value=fingerprint, candidate_tree_hash="same-tree", defect_kind=defect)
        assert result.snapshot.state == ("TRIAGE_REQUIRED" if iteration == 2 else "REPAIR_QUEUED")
    assert result.snapshot.payload["blocker"] == {"kind": "no_progress", "rounds": 3}


@pytest.mark.parametrize("change", ["finding", "output", "tree"])
def test_real_failure_or_tree_changes_are_progress(change):
    from pal.bunshin.semantic_orchestration.verification_settlement import _repair_failure_fingerprint
    from pal.bunshin.verification import no_progress_detected

    findings = [{"finding_kind": "module_defect", "summary": "Expected 2, got 1"}]
    fingerprint = _repair_failure_fingerprint("repair", findings, failure_receipts(0))
    history = [{"finding_fingerprint": fingerprint, "candidate_tree_hash": "tree"}] * 2
    changed = [{**findings[0], "summary": "A different failure"}] if change == "finding" else findings
    receipts = failure_receipts(2, "A different output") if change == "output" else failure_receipts(2)
    history.append({"finding_fingerprint": _repair_failure_fingerprint("repair", changed, receipts),
                    "candidate_tree_hash": "changed-tree" if change == "tree" else "tree"})
    assert not no_progress_detected(history)


def test_failure_identity_ignores_finding_order_and_manager_ids():
    from pal.bunshin.semantic_orchestration.verification_settlement import _repair_failure_fingerprint

    findings = [{"finding_id": "first", "finding_kind": "module_defect", "summary": "Wrong output"},
                {"finding_id": "second", "finding_kind": "module_defect", "summary": "Missing cleanup"}]
    reordered = [{**item, "finding_id": f"new-{index}"} for index, item in enumerate(reversed(findings))]
    assert _repair_failure_fingerprint("repair", findings, failure_receipts(0)) == (
        _repair_failure_fingerprint("repair", reordered, failure_receipts(3)))


def test_real_runtime_snapshot_delivery_keeps_failure_identity(tmp_path):
    """Oversized reruns deliver output through fresh random snapshot files."""
    from pal.execution.runtime import ExecutionRuntime
    from pal.execution.result_snapshots import file_preview, render_snapshot_hint
    from pal.bunshin.review_receipts import _review_tool_evidence_ref
    from pal.bunshin.semantic_orchestration.verification_settlement import _repair_failure_fingerprint
    from pal.shared import RuntimeStatus, ToolExecutionResult
    from pal.shared.tool_protocol import new_tool_call

    runtime = ExecutionRuntime(runtime_root=tmp_path)

    def rerun_failing_command(round_index):
        body = "AssertionError: Expected 2, got 1\n" * 600  # exceeds the preview budget
        ref = runtime.result_snapshots.capture(body, call_id=f"shell-{round_index}", lifetime="verification")
        delivered = file_preview(ref, 1000) + "\n\n" + render_snapshot_hint(ref)
        call = new_tool_call(name="op_exec_shell", args={"cmd": "pytest -q tests/router"},
                             call_id=f"shell-{round_index}")
        result = ToolExecutionResult(name="op_exec_shell", ok=True, text=delivered, llm_text=delivered,
                                     structured={"returncode": 1, "signal": 0, "stdout": "", "stderr": ""},
                                     status=RuntimeStatus.OK, call_id=call.call_id)
        receipt = _review_tool_evidence_ref("op_exec_shell", call, result)
        # record_verification_execution marks a nonzero command exit failed.
        receipt["ok"] = False
        return receipt

    try:
        first, second = rerun_failing_command(0), rerun_failing_command(1)
        assert "Output snapshot:" in first["output_text"]
        # Each rerun delivers through a fresh random snapshot file, so both the
        # raw text and the legacy output_sha256 identity drift per round.
        assert first["output_text"] != second["output_text"]
        assert first["output_sha256"] != second["output_sha256"]
        findings = [{"finding_kind": "module_defect", "summary": "Expected 2, got 1"}]
        assert _repair_failure_fingerprint("repair", findings, [first]) == (
            _repair_failure_fingerprint("repair", findings, [second]))
        changed = [{"finding_kind": "module_defect", "summary": "A different failure"}]
        assert _repair_failure_fingerprint("repair", findings, [first]) != (
            _repair_failure_fingerprint("repair", changed, [first]))
    finally:
        runtime.shutdown()


def test_clarification_delivery_retry_reuses_persisted_answer(tmp_path, monkeypatch):
    from pal.bunshin.manager import BunshinManager, BunshinRunState
    from pal.shared import BunshinInvocationPack

    async def scenario():
        service, processor, workflow_id = new_workflow(tmp_path, "planned")
        await processor.process_once(limit=1)
        await processor.process_once(limit=1)
        revision = next(item for item in service.repository.queries.list_workflow_snapshots(workflow_id)
                        if item.aggregate_type == AggregateType.ARCHITECTURE_REVISION)
        service.repository.transitions.dispatch(ActionEnvelope(action_type="START_ARCHITECT", workflow_id=workflow_id,
            aggregate_type=revision.aggregate_type, aggregate_id=revision.aggregate_id, actor="test",
            expected_version=revision.version, idempotency_key="start-architect",
            payload={"active_worker_id": "architect", "fencing_token": 1}))
        manager = BunshinManager(service.runtime_root)
        manager.workflow_service = service
        state = BunshinRunState(bunshin_id="architect", run_id="run", pack=BunshinInvocationPack(
            invocation_id="architect", metadata={"bunshin_v2": {"workflow_id": workflow_id,
                "aggregate_type": "architecture_revision", "aggregate_id": revision.aggregate_id, "role": "architect"}}),
            pending_clarification={"clarification_id": "question-1", "title": "API",
                "questions": [{"question_id": "api", "question": "Which API?"}]}, status="clarification_pending")
        manager.runs[state.run_id] = state
        send = AsyncMock(side_effect=[False, True])
        monkeypatch.setattr(manager.semantic_orchestrator, "send_worker_control", send)
        clock = {"now": "2026-10-10T12:00:00Z"}
        monkeypatch.setattr("pal.bunshin.manager.utc_now", lambda: clock["now"])
        request = {"clarification_id": "question-1", "run_id": "run",
                   "answers": [{"question_id": "api", "answer": "  Existing API.\n"}]}
        with pytest.raises(RuntimeError, match="no longer available"):
            await manager.send_clarification(request)
        first = service.repository.snapshots.read_snapshot(revision.aggregate_type, revision.aggregate_id)
        assert state.pending_clarification
        clock["now"] = "2026-10-10T12:00:01Z"
        result = await manager.send_clarification(request)
        assert result["clarification"]["task_revision"]["duplicate"] is True
        second = service.repository.snapshots.read_snapshot(revision.aggregate_type, revision.aggregate_id)
        assert second.version == first.version
        ledger = service.artifacts.read_json(second.payload["requirements_ref"])
        assert len(ledger["revisions"]) == 1
        assert ledger["revisions"][0]["authority"]["answer"] == "  Existing API.\n"
        assert ledger["revisions"][0]["authority"]["observed_at"] == "2026-10-10T12:00:00Z"
        assert send.await_count == 2
        assert not state.pending_clarification
        with pytest.raises(ValueError, match="different answer"):
            service.append_architect_clarification({"workflow_id": workflow_id,
                "architecture_revision_id": revision.aggregate_id, "worker_id": "architect",
                "clarification_id": "question-1", "title": "API", "question": "Which API?",
                "answer": "Different API.", "observed_at": clock["now"]})
    asyncio.run(scenario())
