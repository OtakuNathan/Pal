"""Trusted LSP applicability agrees across status, local and Manager gates."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.bunshin.semantic_orchestration.attempt_verifier_context import VerifierContext
from pal.bunshin.semantic_orchestration.role_inputs import _semantic_role_input_refs
from pal.bunshin.submission_drafts import authoring_input_fingerprint
from pal.bunshin.swe_verification import (
    semantic_verification_submission_errors,
    verification_outcome_readiness,
)
from pal.bunshin.verification_builder import compile_verification_invocation_tool_contract
from pal.bunshin.verification_lsp_policy import lsp_evidence_required
from tests.test_bunshin_historical_contract import modern_bill
from tests.test_bunshin_verifier_tool_feedback import verifier, payload, run_delta, status


def preparation(state):
    if state is None:
        return None
    return {"lsp_workspace_preparation": {"status": state, "servers": []}}


@pytest.mark.parametrize("source,state,required", [
    ({"lsp_policy": "when_available"}, "ok", True),
    ({"lsp_policy": "when_available"}, "unavailable", False),
    ({"lsp_policy": "when_available"}, None, True),
    ({"lsp_policy": "when_available"}, "partial", True),
    ({"lsp_policy": "when_available"}, "skipped", True),
    ({"lsp_policy": "when_available"}, "error", True),
    ({"lsp_policy": "required"}, "unavailable", True),
    ({"lsp_policy": "when_available", "require_lsp": True}, "unavailable", True),
    ({"lsp_policy": "never", "require_lsp": True}, "unavailable", True),
    ({"lsp_policy": "unrecognized"}, "unavailable", True),
    ({"lsp_policy": "never"}, None, False),
])
def test_only_trusted_optional_unavailability_removes_lsp(source, state, required):
    contract = compile_verification_invocation_tool_contract(
        work_view={"module_name": "router"}, verification_policy=source,
        workspace_preparation=preparation(state),
    )
    policy = contract["verification_policy"]
    assert lsp_evidence_required(policy) is required
    skipped = source.get("lsp_policy") == "when_available" and not required
    assert ("lsp" not in contract["allowed_obligations"]) is skipped
    assert ("op_bunshin_verification_run_lsp_check" not in contract["allowed_capabilities"]) is skipped
    assert policy["lsp_applicability"]["source"] == "manager_workspace_preparation.v1"


def test_legacy_policy_and_untrusted_source_claims_do_not_discharge_lsp():
    assert lsp_evidence_required({"lsp_policy": "when_available"})
    contract = compile_verification_invocation_tool_contract(
        work_view={"module_name": "router"}, verification_policy={
            "lsp_policy": "when_available", "require_lsp": False,
            "lsp_applicability": {"source": "manager_workspace_preparation.v1",
                                  "availability": "unavailable", "required": False},
        },
    )
    assert lsp_evidence_required(contract["verification_policy"])
    assert contract["verification_policy"]["lsp_applicability"]["availability"] == "unknown"


def bind_policy(verifier, source, state):
    _fixture, workspace, _probe, _call, _runtime, view = verifier
    contract = compile_verification_invocation_tool_contract(
        work_view=view, verification_policy=source, workspace_preparation=preparation(state),
    )
    path = next(item["path"] for item in workspace["reference_paths"] if item["name"] == "verification_policy")
    Path(path).write_text(json.dumps(contract["verification_policy"]))
    workspace["bunshin_v2"]["verification_tool_contract"] = contract
    return contract


def manager_errors(verifier, cases, *, outcome="pass", workspace=None):
    _, bound_workspace, _, _, _, view = verifier
    return semantic_verification_submission_errors(
        {"outcome": outcome, "reason": "Required check unavailable; rerun after recovery." if outcome == "unknown" else "",
         "findings": [], "recorded_results": cases,
         "tool_receipts": bound_workspace["review_tool_evidence_refs"]},
        work_view=view, changed_paths=[], current_case_paths=["tests/router/verifier/probe.py"],
        corpus_scope=view["verification_corpus"], scratch_only=False,
        workspace=workspace if workspace is not None else bound_workspace,
    )


@pytest.mark.parametrize("source,state,required", [
    ({"lsp_policy": "when_available"}, "ok", True),
    ({"lsp_policy": "when_available"}, "unavailable", False),
    ({"lsp_policy": "when_available"}, None, True),
    ({"lsp_policy": "when_available"}, "partial", True),
    ({"lsp_policy": "required"}, "unavailable", True),
    ({"lsp_policy": "when_available", "require_lsp": True}, "unavailable", True),
])
def test_status_local_and_authenticated_manager_agree(verifier, source, state, required):
    _, workspace, _, call, _, _ = verifier
    bind_policy(verifier, source, state)
    delta = payload(run_delta(verifier))["case"]
    draft_status = status(verifier)
    assert ("lsp" in draft_status["remaining_policy_obligations"]) is required
    assert draft_status["ready_by_outcome"]["pass"] is (not required)
    errors = manager_errors(verifier, [delta])
    assert any("requires LSP evidence" in item for item in errors) is required
    # Manager sees authenticated pack metadata, not the worker's /pal mount.
    manager_workspace = copy.deepcopy(workspace)
    manager_workspace["reference_paths"] = [{"name": "verification_policy", "path": "/pal/missing"}]
    assert manager_errors(verifier, [delta], workspace=manager_workspace) == errors
    result = call("submit_verification_pass")
    assert result.ok is (not required)
    if required:
        assert "requires LSP evidence" in result.llm_text


def test_optional_unavailable_cannot_be_reintroduced_by_a_role_tool_claim(verifier):
    fixture, _workspace, _probe, call, _, _ = verifier
    bind_policy(verifier, {"lsp_policy": "when_available"}, "unavailable")
    before = len(fixture.adapter.calls)
    result = call("record_unavailable_verification", name="invented mandatory LSP", obligation="lsp",
                  reason="The role says LSP is mandatory")
    assert not result.ok and "outside the bound node contract" in result.llm_text
    result = call("run_verification_lsp_check", name="optional diagnostics", file="router.py")
    assert not result.ok
    assert len(fixture.adapter.calls) == before
    assert not status(verifier)["cases"]


def test_required_unavailable_is_unknown_not_pass(verifier):
    _, workspace, _, call, _, _ = verifier
    bind_policy(verifier, {"lsp_policy": "required"}, "unavailable")
    delta = payload(run_delta(verifier))["case"]
    gap = payload(call("record_unavailable_verification", name="required LSP", obligation="lsp",
                       reason="Required language-server service unavailable; rerun after recovery."))["case"]
    # Recording a new case changes the corpus revision; refresh the final execution.
    delta = payload(run_delta(verifier))["case"]
    state = status(verifier)
    assert not state["ready_by_outcome"]["pass"]
    assert state["ready_by_outcome"]["unknown"]
    assert any("UNKNOWN cases" in item for item in manager_errors(verifier, [delta, gap]))
    assert not manager_errors(verifier, [delta, gap], outcome="unknown")
    assert not call("submit_verification_pass").ok
    assert payload(call("submit_verification_unknown", reason="Required LSP unavailable; rerun after recovery."))["submitted"]


def test_available_successful_lsp_closes_exact_required_obligation(verifier):
    fixture, _, _, call, _, _ = verifier
    bind_policy(verifier, {"lsp_policy": "when_available"}, "ok")
    delta = payload(run_delta(verifier))["case"]
    fixture.adapter.lsp_structured = {"status": "ok", "operation": "diagnostics",
                                     "result": {"status": "ok", "diagnostics_state": "fresh", "diagnostics": []}}
    lsp = payload(call("run_verification_lsp_check", name="prepared diagnostics", file="router.py"))["case"]
    assert lsp["status"] == "PASS"
    assert status(verifier)["ready_by_outcome"]["pass"]
    assert not manager_errors(verifier, [delta, lsp])
    assert payload(call("submit_verification_pass"))["submitted"]


@pytest.mark.parametrize("case_status", ["UNKNOWN", "FAIL"])
def test_optional_policy_never_reinterprets_existing_unresolved_evidence(verifier, case_status):
    _, workspace, _, _, _, _ = verifier
    bind_policy(verifier, {"lsp_policy": "when_available"}, "unavailable")
    delta = payload(run_delta(verifier))["case"]
    previous = {"name": "existing LSP observation", "case_kind": "lsp", "status": case_status,
                "obligation_tags": ["lsp"], "input_fingerprint": workspace["bunshin_v2"]["authoring_input_fingerprint"]}
    cases = [delta, previous]
    draft = {"evidence": {"cases": {item["name"]: item for item in cases}}}
    local = verification_outcome_readiness(workspace, draft, outcome="pass")
    assert not local["ready"]
    assert any("failed or UNKNOWN cases" in item for item in local["blockers"])
    assert any("failed or UNKNOWN cases" in item for item in manager_errors(verifier, cases))
    assert previous["status"] == case_status


def test_optional_lsp_does_not_remove_other_required_checks(verifier):
    _, workspace, _, _, _, view = verifier
    view["historical_repair_bills"] = [modern_bill()]
    contract = bind_policy(verifier, {"lsp_policy": "when_available", "require_focused_tests": True,
                                     "require_warning_clean": True}, "unavailable")
    policy = contract["verification_policy"]
    assert policy["require_historical_regressions"]
    assert policy["require_focused_tests"] and policy["require_warning_clean"]
    assert {"compile", "consumer_probe", "historical_regressions", "candidate_delta_review"} <= set(policy["allowed_obligations"])
    state = status(verifier)
    assert {"focused_tests", "warning_clean", "historical_regressions", "candidate_delta_review"} <= set(state["remaining_policy_obligations"])
    assert not state["ready_by_outcome"]["pass"]
    assert "lsp" not in state["remaining_policy_obligations"]


def test_manager_context_compiles_bound_preparation_into_new_semantic_identity(tmp_path):
    artifacts = ContentAddressedArtifactStore(tmp_path, BunshinRepository(tmp_path).artifacts)
    view = artifacts.put_json({"module_name": "router"}, artifact_type="ModuleWorkViewArtifact")
    old = artifacts.put_json({"lsp_policy": "when_available"}, artifact_type="VerificationPolicyArtifact")
    initial_refs = {"module_work_view": view.to_dict(), "verification_policy": old.to_dict()}
    identities = {authoring_input_fingerprint(_semantic_role_input_refs(initial_refs))}
    for state, required in [("unavailable", False), ("ok", True)]:
        prepared = artifacts.put_json(preparation(state), artifact_type="WorkspacePreparationArtifact")
        refs = {"module_work_view": view, "workspace_preparation": prepared}
        stage = SimpleNamespace(binding={"family_id": "software_engineering"}, bound_reference_refs=refs,
                                family_policies={"verification": {"lsp_policy": "when_available"}}, role="verifier")
        command = SimpleNamespace(activation=RoleActivation(OrchestrationRole.VERIFIER, RoleMode.MODULE))
        result = asyncio.run(VerifierContext(artifacts).execute(command, stage))
        policy = artifacts.read_json(refs["verification_policy"])
        assert result.verification_tool_contract["verification_policy"] == policy
        assert lsp_evidence_required(policy) is required
        identities.add(authoring_input_fingerprint(_semantic_role_input_refs({key: ref.to_dict() for key, ref in refs.items()})))
    assert len(identities) == 3
    assert artifacts.read_json(old) == {"lsp_policy": "when_available"}
    assert lsp_evidence_required(artifacts.read_json(old))
