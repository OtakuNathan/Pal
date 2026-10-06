"""Current RepairPacket history and invocation-local tool projection agree."""
from __future__ import annotations

import asyncio
import copy
import json

import pytest

from pal.bunshin.scoped_execution import BunshinScopedExecutionRuntime
from pal.execution.runtime import ExecutionRuntime
from pal.bunshin.v2.artifacts import ContentAddressedArtifactStore
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.review_findings import structured_findings
from pal.bunshin.v2.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.bunshin.v2.semantic_orchestration.role_policy import apply_v2_role_capability_policy
from pal.bunshin.v2.submission_drafts import AUTHORING_CONTRACT_VERSION, authoring_input_fingerprint
from pal.bunshin.v2.swe_verification import swe_verification_tool_result
from pal.bunshin.v2.verification import (
    historical_repair_checklist_items,
    repair_bill_semantic_view,
    repair_checklist_items,
)
from pal.bunshin.v2.verification_builder import (
    VERIFICATION_BUILDER_TOOL_SPECS,
    compile_verification_invocation_tool_contract,
    verification_builder_tool_result,
)
from pal.bunshin.v2.work_items import update_checklist_tool_result
from pal.shared import BunshinInvocationPack, RuntimeStatus, ToolExecutionResult
from pal.shared.tool_protocol import new_tool_call


HISTORICAL = "op_bunshin_verification_run_historical_regression"
HISTORICAL_ALIAS = "run_verification_historical_regression"


def modern_bill():
    return {
        "artifact_kind": "semantic_repair_packet",
        "module_name": "router",
        "findings": structured_findings({"findings": [
            {"finding_id": identity, "finding_kind": "module_defect",
             "priority": "p1", "summary": f"Prior defect {identity}"}
            for identity in ("finding_first", "finding_second")
        ]}),
    }


class RecordedCommands:
    def __init__(self):
        self.calls = []

    async def execute_tool_async(self, call, **_kwargs):
        self.calls.append(call)
        return ToolExecutionResult(
            name=call.name, call_id=call.call_id, ok=True,
            text="synthetic command completed", llm_text="synthetic command completed",
            status=RuntimeStatus.OK,
            structured={"returncode": 0, "stdout": "passed", "stderr": ""},
        )


def test_current_repair_packet_preserves_every_regression_through_submit(tmp_path):
    repository = BunshinV2Repository(tmp_path)
    artifacts = ContentAddressedArtifactStore(tmp_path, repository.artifacts)
    packet = artifacts.put_json(modern_bill(), artifact_type="RepairPacketArtifact")
    view = {"module_name": "router", "historical_repair_bills": [
        repair_bill_semantic_view(artifacts, packet),
    ]}
    contract = compile_verification_invocation_tool_contract(work_view=view, verification_policy={})
    assert [item["case"] for item in contract["required_historical_regressions"]] == [
        "finding_first", "finding_second",
    ]
    assert HISTORICAL in contract["allowed_capabilities"]
    assert contract["verification_policy"]["require_historical_regressions"] is True
    assert len(historical_repair_checklist_items({
        **view, "historical_repair_bills": view["historical_repair_bills"] * 2,
    })) == 2

    view_path = tmp_path / "module_work_view.json"
    policy_path = tmp_path / "verification_policy.json"
    view_path.write_text(json.dumps(view))
    policy_path.write_text(json.dumps(contract["verification_policy"]))
    repo = tmp_path / "repo"
    corpus = repo / "tests/router/verifier"
    corpus.mkdir(parents=True)
    (corpus / "test_regression.py").write_text("def test_regression():\n    assert True\n")
    lease = repository.leases.claim_lease("test-verifier", "attempt-current", ttl_seconds=60)
    workspace = {
        "runtime_root": str(tmp_path), "repo_path": str(repo),
        "artifact_dir": str(tmp_path / "outputs"),
        "artifact_stage_dir": str(tmp_path / "stage"),
        "review_scratch_dir": str(tmp_path / "scratch"),
        "write_path_scopes": [{"kind": "directory", "path": "tests/router/verifier"}],
        "reference_paths": [
            {"name": "module_work_view", "path": str(view_path)},
            {"name": "verification_policy", "path": str(policy_path)},
        ],
        "bunshin_v2": {
            "workflow_id": "workflow-test", "invocation_id": "attempt-current",
            "lease_resource_key": lease.resource_key, "fencing_token": lease.fencing_token,
            "role": "verifier", "mode": "module", "authoring_input_fingerprint": "candidate-current",
            "authoring_contract_version": AUTHORING_CONTRACT_VERSION,
            "verification_tool_contract": contract,
        },
    }
    checklist = update_checklist_tool_result(new_tool_call(
        name="op_bunshin_update_checklist", call_id="checklist",
        args={"plan": [{"step": "verify candidate", "status": "completed"}]},
    ), workspace)
    assert checklist.ok, checklist.llm_text
    adapter = RecordedCommands()
    call_number = 0

    def run(name, case):
        nonlocal call_number
        call_number += 1
        return asyncio.run(verification_builder_tool_result(new_tool_call(
            name=name, args={"name": case, "command": "synthetic-regression"},
            call_id=f"case-{call_number}",
        ), workspace, [], original_adapter=adapter))

    delta = "op_bunshin_verification_run_diff_risk"
    blocked = run(delta, "current delta")
    assert not blocked.ok and "finding_first" in blocked.llm_text
    assert adapter.calls == []
    first = run(HISTORICAL, "finding_first")
    assert first.ok, first.llm_text
    blocked = run(delta, "current delta")
    assert not blocked.ok and "finding_second" in blocked.llm_text
    assert len(adapter.calls) == 1
    second = run(HISTORICAL, "finding_second")
    assert second.ok, second.llm_text
    checked = run(delta, "current delta")
    assert checked.ok, checked.llm_text
    assert len(adapter.calls) == 3

    # Ordinary final-corpus evidence is separately required by SWE outcomes.
    workspace["review_tool_evidence_refs"] = [{"kind": "command", "ok": True}]
    submitted = swe_verification_tool_result(new_tool_call(
        name="op_bunshin_verification_pass", args={}, call_id="submit",
    ), workspace, [])
    assert submitted.ok, submitted.llm_text
    result = json.loads((tmp_path / "stage/verification_submission.json").read_text())
    assert result["outcome"] == "pass"
    assert [case["name"] for case in result["recorded_results"]] == [
        "finding_first", "finding_second", "current delta",
    ]


@pytest.mark.parametrize(("finding", "bill_case", "expected"), [
    ({"finding_key": "key", "case": "case", "case_name": "name", "finding_id": "id"}, "bill", "key"),
    ({"case": "case", "case_name": "name", "finding_id": "id"}, "bill", "case"),
    ({"case_name": "name", "finding_id": "id"}, "bill", "name"),
    ({"finding_id": "id"}, "bill", "bill"),
    ({"finding_id": "id"}, "", "id"),
])
def test_modern_identity_is_only_a_legacy_compatible_fallback(finding, bill_case, expected):
    before = copy.deepcopy(finding)
    items = repair_checklist_items({"case_name": bill_case, "findings": [finding]})
    assert [item["case"] for item in items] == [expected]
    assert finding == before


def scoped_for(view, *, guidance=None, contract=None):
    if contract is None:
        contract = compile_verification_invocation_tool_contract(work_view=view, verification_policy={})
    workspace = {"bunshin_v2": {"verification_tool_contract": contract}}
    pack = apply_v2_role_capability_policy(BunshinInvocationPack(
        invocation_id="projection-test", workspace=workspace,
    ), activation=RoleActivation(OrchestrationRole.VERIFIER, RoleMode.MODULE))
    runtime = BunshinScopedExecutionRuntime(
        ExecutionRuntime(), list(pack.allowed_capabilities), workspace=workspace,
        capability_guidance_overrides=guidance or {},
    )
    return pack, runtime, contract


def providers(runtime):
    return {item["function"]["name"]: item["function"] for item in runtime.build_llm_tool_contracts()}


@pytest.mark.parametrize("view", [
    {"module_name": "router", "historical_repair_bills": []},
    {"module_name": "router", "historical_repair_bills": [modern_bill()]},
    {"module_name": "delivery", "graph_sink": True, "entrypoints": [{"kind": "cli"}]},
    {"module_name": "delivery", "graph_sink": True, "entrypoints": [{"kind": "platform_probe"}]},
])
def test_evidence_provider_discovery_and_admission_match_bound_contract(view):
    pack, runtime, contract = scoped_for(view)
    advertised = providers(runtime)
    try:
        expected = set(pack.allowed_capabilities) & set(contract["allowed_capabilities"])
        for canonical, spec in VERIFICATION_BUILDER_TOOL_SPECS.items():
            alias = spec["alias"]
            assert (alias in advertised) == (canonical in expected)
            assert (runtime.get_capability_spec(alias) is not None) == (canonical in expected)
            assert (canonical in runtime.allowed_capabilities) == (canonical in expected)
        assert {"submit_verification_pass", "submit_verification_unknown",
                "request_verification_module_repair", "update_checklist", "add_finding",
                "read_verification_draft_status", "run_verification_diff_risk"} <= set(advertised)
        assert "submit_verification" not in advertised
        if HISTORICAL not in expected:
            result = asyncio.run(runtime.execute_tool_async(new_tool_call(
                name=HISTORICAL_ALIAS, args={"name": "unbound", "command": "never-run"},
            )))
            assert not result.ok
            assert result.structured["reason"] == "capability_not_allowed"
    finally:
        runtime.base_runtime.runtime.shutdown()


def test_node_evidence_guidance_reaches_provider_without_erasing_other_profile_fields():
    _, runtime, _ = scoped_for(
        {"module_name": "router", "historical_repair_bills": [modern_bill()]},
        guidance={HISTORICAL: {"use_when": "stale profile applicability",
                               "failure_next_steps": "Retain this profile recovery instruction."}},
    )
    try:
        description = providers(runtime)[HISTORICAL_ALIAS]["description"]
        assert "finding_first" in description and "finding_second" in description
        assert "stale profile applicability" not in description
        assert runtime.capability_guidance_overrides[HISTORICAL]["failure_next_steps"] == (
            "Retain this profile recovery instruction."
        )
        assert "before submit_verification" not in description
        assert "bound verification outcome tool" in description
    finally:
        runtime.base_runtime.runtime.shutdown()


def test_scoped_projection_does_not_silently_rewrite_a_pinned_contract():
    view = {"module_name": "router", "historical_repair_bills": [modern_bill()]}
    old = compile_verification_invocation_tool_contract(work_view={"module_name": "router"}, verification_policy={})
    before = copy.deepcopy(old)
    _, resumed, _ = scoped_for(view, contract=old)
    _, new_assignment, _ = scoped_for(view)
    try:
        assert old == before
        assert HISTORICAL_ALIAS not in providers(resumed)
        assert HISTORICAL_ALIAS in providers(new_assignment)
    finally:
        resumed.base_runtime.runtime.shutdown()
        new_assignment.base_runtime.runtime.shutdown()


def test_roles_without_a_node_contract_keep_their_existing_surface():
    runtime = BunshinScopedExecutionRuntime(ExecutionRuntime(), [HISTORICAL], workspace={})
    try:
        assert HISTORICAL_ALIAS in providers(runtime)
    finally:
        runtime.base_runtime.runtime.shutdown()


def test_rebuilding_corrected_history_policy_changes_semantic_input_identity(tmp_path):
    repository = BunshinV2Repository(tmp_path)
    artifacts = ContentAddressedArtifactStore(tmp_path, repository.artifacts)
    view = {"module_name": "router", "historical_repair_bills": [modern_bill()]}
    view_ref = artifacts.put_json(view, artifact_type="ModuleWorkViewArtifact")
    corrected = compile_verification_invocation_tool_contract(work_view=view, verification_policy={})
    # The previously derived policy discarded current-format history. It is
    # immutable; a supported new semantic assignment must bind a new policy.
    old = compile_verification_invocation_tool_contract(work_view={"module_name": "router"}, verification_policy={})
    old_ref = artifacts.put_json(old["verification_policy"], artifact_type="VerificationPolicyArtifact")
    corrected_ref = artifacts.put_json(corrected["verification_policy"], artifact_type="VerificationPolicyArtifact")
    assert old_ref.sha256 != corrected_ref.sha256
    identities = [authoring_input_fingerprint({
        "role": "verifier", "mode": "module",
        "references": {"module_work_view": view_ref.to_dict(), "verification_policy": policy.to_dict()},
        "architecture_revision_base_submission": None,
        "architecture_revision_scope": None,
        "evaluation_generation": 0,
    }) for policy in (old_ref, corrected_ref)]
    assert identities[0] != identities[1]
    assert artifacts.read_json(old_ref)["require_historical_regressions"] is False
    assert artifacts.read_json(corrected_ref)["require_historical_regressions"] is True
