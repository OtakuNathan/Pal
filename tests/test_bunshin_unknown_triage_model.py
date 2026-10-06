"""Deterministic implementation traces for OperatorTriageRecovery.

The traces exercise the real transition table, durable lease fence, and receipt
reuse predicate. They are bounded regression/parity checks, not a Python
translation of TLA+ or evidence that TLC ran. Optional subprocess tests run the
positive models and deliberately unsafe receipt-reuse model only when a pinned
TLA2TOOLS_JAR has been supplied; they never download a checker.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from pal.bunshin.v2 import (
    ActionEnvelope, AggregateSnapshot, AggregateType, BunshinV2Repository,
    build_default_transition_engine,
)
from pal.bunshin.v2.contracts import StaleFencingToken, UnknownTransitionError
from pal.bunshin.v2.semantic_orchestration.assignment_identity import AssignmentIdentity
from pal.bunshin.v2.sessions import module_verifier_session_id, node_role_generation


ROOT = Path(__file__).resolve().parents[1]
SPEC_ROOT = ROOT / "spec" / "bunshin_v2"
GENERATION = "verifier_evaluation_generation"
PENDING = {"sha256": "unknown-pending", "artifact_type": "PendingVerificationArtifact"}
REPORT = {"sha256": "unknown-report", "artifact_type": "VerificationArtifact"}
SNAPSHOT_FIELDS = {
    "pending_verification_ref": PENDING,
    "process_group_reaped": True,
    "exclusive_workspace_lock": True,
    "workspace_fingerprint": "same-candidate-tree",
    "workspace_lock_path": "/deterministic/worktree.lock",
}
IDENTITY_FIELDS = {
    "candidate_ref": {"sha256": "candidate", "artifact_type": "CandidateArtifact"},
    "candidate_digest": "same-candidate-tree",
    "module_name": "router",
    "role_session_generation": 4,
    "architecture_review_generation": 7,
    "failure_history": [{"finding_fingerprint": "historic-finding"}],
}


def snapshot(generation=None):
    payload = deepcopy({**IDENTITY_FIELDS, **SNAPSHOT_FIELDS})
    if generation is not None:
        payload[GENERATION] = generation
    return AggregateSnapshot(
        aggregate_type=AggregateType.DAG_NODE_RUN,
        aggregate_id="node-router", workflow_id="workflow-router",
        state="REVIEW_SNAPSHOTTING", version=5, payload=payload,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def step(engine, node, action, **payload):
    return engine.transition(node, ActionEnvelope(
        action_type=action, workflow_id=node.workflow_id,
        aggregate_type=node.aggregate_type, aggregate_id=node.aggregate_id,
        actor="operator" if action == "RESOLVE_TRIAGE" else "verifier",
        payload=payload, expected_version=node.version,
    ))


def checkpoint(node):
    return (str(node.state), int(node.payload.get(GENERATION) or 0),
            node.payload.get("pending_verification_ref"))


def session_id(node):
    return module_verifier_session_id(
        node.workflow_id, node.payload["module_name"],
        generation=node_role_generation(node.payload),
    )


def reuse_lookup(receipts, generation):
    """Use the production identity predicate with a deterministic receipt list."""
    identity = AssignmentIdentity(
        effect_reads=None, background=None,
        repository=SimpleNamespace(role_assignments=SimpleNamespace(
            list_role_assignments=lambda **_: tuple(receipts))),
    )
    return identity.reusable_role_assignment(
        workflow_id="workflow-router", aggregate_type=AggregateType.DAG_NODE_RUN.value,
        aggregate_id="node-router", role="verifier", mode="module",
        submission_kind="verification", input_refs={
            "candidate_diff": {"sha256": "candidate"},
            "module_work_view": {"sha256": "same-policy"},
        }, evaluation_generation=generation,
    )


def settled_receipt(generation, verdict="UNKNOWN"):
    return {
        "assignment_id": f"verification-{generation}",
        "workflow_id": "workflow-router", "aggregate_type": AggregateType.DAG_NODE_RUN.value,
        "aggregate_id": "node-router", "role": "verifier", "mode": "module",
        "submission_kind": "verification", "state": "settled",
        "submission_artifact_ref": {
            "sha256": f"receipt-{generation}-{verdict}",
            "artifact_type": "VerifierRoleSubmissionArtifact",
        },
        "input_refs": {
            "candidate_diff": {"sha256": "candidate"},
            "module_work_view": {"sha256": "same-policy"},
        },
        "execution_spec": {"evaluation_generation": generation},
    }


@pytest.mark.parametrize("initial_generation", [None, 0, 2], ids=["legacy", "zero", "later"])
def test_terminal_unknown_operator_retry_trace(initial_generation, tmp_path):
    """UNKNOWN -> explicit resolve -> new generation -> fresh PASS; same inputs."""
    engine = build_default_transition_engine()
    initial = snapshot(initial_generation)
    generation = int(initial_generation or 0)
    receipts = [settled_receipt(generation)]
    immutable_history = deepcopy(receipts)
    identity_before = session_id(initial)
    trace = [checkpoint(initial)]

    # FinalizeSnapshot: the immutable report distinguishes a terminal verdict.
    triaged = step(engine, initial, "ENTER_TRIAGE", verification_artifact_ref=REPORT,
                   unknown_blocking=True, blocker={"kind": "blocking_unknown"}).snapshot
    trace.append(checkpoint(triaged))
    assert triaged.payload["triage_resume_state"] == "REVIEW_SNAPSHOTTING"
    assert reuse_lookup(receipts, generation)["assignment_id"] == receipts[0]["assignment_id"]
    # No ordinary background start/resume/accept action can escape the blocker.
    for action in ("START_REVIEW", "RESUME", "REVIEW_PASSED"):
        with pytest.raises(UnknownTransitionError):
            step(engine, triaged, action, fencing_token=1, verification_artifact_ref=REPORT)

    resolved = step(engine, triaged, "RESOLVE_TRIAGE")
    queued = resolved.snapshot
    trace.append(checkpoint(queued))
    assert [effect.effect_type for effect in resolved.effects] == [
        "reconcile_semantic_state", "schedule_ready_nodes"]
    for field in (*SNAPSHOT_FIELDS, "blocker", "unknown_blocking"):
        assert field not in queued.payload
    for field, value in IDENTITY_FIELDS.items():
        assert queued.payload[field] == value
    assert queued.payload["verification_artifact_ref"] == REPORT
    assert session_id(queued) == identity_before
    assert receipts == immutable_history
    assert reuse_lookup(receipts, generation + 1) is None

    # The real durable lease store advances fences; old attempts remain stale.
    repository = BunshinV2Repository(tmp_path)
    old_lease = repository.leases.claim_lease("verifier", "old-attempt")
    repository.leases.release_lease("verifier", "old-attempt", old_lease.fencing_token)
    new_lease = repository.leases.claim_lease("verifier", "new-attempt")
    assert new_lease.fencing_token > old_lease.fencing_token
    with pytest.raises(StaleFencingToken):
        repository.leases.assert_fencing_token("verifier", "old-attempt", old_lease.fencing_token)
    repository.leases.assert_fencing_token("verifier", "new-attempt", new_lease.fencing_token)

    running = step(engine, queued, "START_REVIEW", fencing_token=new_lease.fencing_token).snapshot
    trace.append(checkpoint(running))
    fresh_pending = {"sha256": "fresh-pending", "artifact_type": "PendingVerificationArtifact"}
    quiescing = step(engine, running, "SUBMIT_SEMANTIC_VERIFICATION",
                     pending_verification_ref=fresh_pending).snapshot
    trace.append(checkpoint(quiescing))
    snapshotted = step(engine, quiescing, "VERIFIER_QUIESCED",
                       fencing_token=new_lease.fencing_token, process_group_reaped=True,
                       exclusive_workspace_lock=True, workspace_fingerprint="same-candidate-tree").snapshot
    trace.append(checkpoint(snapshotted))
    passed = step(engine, snapshotted, "REVIEW_PASSED",
                  verification_artifact_ref={"sha256": "fresh-pass-report"}).snapshot
    trace.append(checkpoint(passed))
    receipts.append(settled_receipt(generation + 1, "PASS"))
    assert reuse_lookup(receipts, generation + 1)["assignment_id"] == f"verification-{generation + 1}"
    assert receipts[:1] == immutable_history
    assert session_id(passed) == identity_before
    assert trace == [
        ("REVIEW_SNAPSHOTTING", generation, PENDING),
        ("TRIAGE_REQUIRED", generation, PENDING),
        ("REVIEW_QUEUED", generation + 1, None),
        ("REVIEWING", generation + 1, None),
        ("REVIEW_QUIESCING", generation + 1, fresh_pending),
        ("REVIEW_SNAPSHOTTING", generation + 1, fresh_pending),
        ("ACCEPTED", generation + 1, fresh_pending),
    ]


@pytest.mark.parametrize("blocker", ["snapshot_interrupted", "blocking_unknown"])
def test_interrupted_snapshot_replay_trace_keeps_original_generation(blocker):
    """A blocker label without a terminal report must not manufacture a retry."""
    engine = build_default_transition_engine()
    initial = snapshot(2)
    triaged = step(engine, initial, "ENTER_TRIAGE", blocker={"kind": blocker}).snapshot
    resumed = step(engine, triaged, "RESOLVE_TRIAGE").snapshot
    assert [checkpoint(node) for node in (initial, triaged, resumed)] == [
        ("REVIEW_SNAPSHOTTING", 2, PENDING),
        ("TRIAGE_REQUIRED", 2, PENDING),
        ("REVIEW_SNAPSHOTTING", 2, PENDING),
    ]
    for field, value in {**SNAPSHOT_FIELDS, **IDENTITY_FIELDS}.items():
        assert resumed.payload[field] == value
    # Once the original snapshot settles UNKNOWN, a later explicit resolve is
    # the first transition that retires its pointer and starts generation 3.
    terminal = step(engine, resumed, "ENTER_TRIAGE", verification_artifact_ref=REPORT,
                    unknown_blocking=True, blocker={"kind": "blocking_unknown"}).snapshot
    fresh = step(engine, terminal, "RESOLVE_TRIAGE").snapshot
    assert checkpoint(fresh) == ("REVIEW_QUEUED", 3, None)
    assert fresh.payload["verification_artifact_ref"] == REPORT


def test_nonterminal_unknown_label_preserves_generic_resume_boundary():
    engine = build_default_transition_engine()
    initial = replace(snapshot(2), state="REVIEWING")
    triaged = step(engine, initial, "ENTER_TRIAGE", verification_artifact_ref=REPORT,
                   blocker={"kind": "blocking_unknown"}).snapshot
    resumed = step(engine, triaged, "RESOLVE_TRIAGE").snapshot
    assert resumed.state == "REVIEW_QUEUED"
    assert resumed.payload[GENERATION] == 2


def test_operator_triage_model_config_checks_recovery_contract():
    """Wiring only: this assertion deliberately makes no formal-proof claim."""
    config = (SPEC_ROOT / "OperatorTriageRecovery.cfg").read_text()
    for invariant in (
        "NoOldUnknownReplay", "ConsumedEvidenceIsCurrent", "AcceptedRequiresCurrentPass",
        "PendingReceiptIsImmutable", "FreshEvaluationHasNoPendingSnapshot",
        "EachEvaluationWasOperatorRequested", "ReceiptHasMatchingDraft", "ReceiptIdentityIsUnique",
    ):
        assert f"INVARIANT {invariant}\n" in config
    for prop in (
        "HistoryNeverShrinks", "GenerationAdvanceRequiresOperator", "TerminalUnknownWaitsForOperator",
        "InterruptedSnapshotReplaysExactly", "OperatorRetryPreservesInputsAndHistory",
        "NewReceiptRequiresCurrentFence",
    ):
        assert f"PROPERTY {prop}\n" in config
    script = (ROOT / "scripts" / "check_bunshin_v2_tla.sh").read_text()
    assert "OperatorTriageRecoveryUnsafe.cfg" in script
    assert "Invariant NoOldUnknownReplay is violated" in script


@pytest.mark.parametrize("module,unsafe", [
    ("ModuleLifecycle", False), ("OperatorTriageRecovery", False),
    ("OperatorTriageRecovery", True),
], ids=["module", "operator-triage", "old-receipt-mutant"])
def test_unknown_triage_tlc(tmp_path, module, unsafe):
    jar_value = os.environ.get("TLA2TOOLS_JAR")
    if not jar_value:
        pytest.skip("TLC not run: set TLA2TOOLS_JAR to a pinned tla2tools.jar")
    jar = Path(jar_value).expanduser().resolve()
    assert jar.is_file(), f"TLA2TOOLS_JAR does not exist: {jar}"
    java = shutil.which("java")
    assert java is not None, "TLA2TOOLS_JAR is set but Java is unavailable"
    config = module + ("Unsafe" if unsafe else "") + ".cfg"
    shutil.copyfile(SPEC_ROOT / f"{module}.tla", tmp_path / f"{module}.tla")
    shutil.copyfile(SPEC_ROOT / config, tmp_path / config)
    result = subprocess.run(
        [java, "-XX:+UseParallelGC", "-jar", str(jar), "-workers", "1",
         "-cleanup", "-config", config, f"{module}.tla"],
        cwd=tmp_path, capture_output=True, text=True, timeout=180, check=False,
    )
    output = result.stdout + result.stderr
    if unsafe:
        assert result.returncode != 0, output
        assert "Invariant NoOldUnknownReplay is violated" in output
    else:
        assert result.returncode == 0, output
        assert "Model checking completed. No error has been found." in output
