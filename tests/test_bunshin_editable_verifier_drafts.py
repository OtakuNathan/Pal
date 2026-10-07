"""Checked VerifierDraftLifecycle conformance at the consumed tool boundary."""
from __future__ import annotations

import asyncio
import json
import sys
from itertools import count
from dataclasses import replace

import pytest

from pal.bunshin.v2.submission_drafts import SubmissionDraftContext, SubmissionDraftStore
from pal.bunshin.v2.work_items import FINDING_KINDS, read_work_items
from pal.shared.tool_protocol import new_tool_call
from tests.test_bunshin_verifier_tool_feedback import payload, run_delta, status, verifier


_finding_keys = count(1)


def finding(call, summary="The probe's expected result was mistaken.", kind="module_defect"):
    return payload(call("update_finding", finding_id=f"concern-{next(_finding_keys)}", expected_revision=0,
                        finding_kind=kind, priority="p1", summary=summary))


@pytest.mark.parametrize("kind", sorted(FINDING_KINDS))
def test_current_finding_create_update_delete_and_fresh_pass(verifier, kind):
    fixture, workspace, _, call, _, _ = verifier
    original = finding(call, kind=kind)
    assert original["revision"] == 1
    changed = payload(call("update_finding", finding_id=original["finding_id"], expected_revision=1,
                           finding_kind="verification_defect", priority="p2", disposition="advisory",
                           summary="The corrected oracle proves this concern does not apply."))
    assert changed["finding_id"] == original["finding_id"] and changed["revision"] == 2
    state = status(verifier)
    assert state["advisories"][0]["revision"] == 2
    assert not call("remove_finding", finding_id=original["finding_id"], expected_revision=1,
                    reason="stale edit").ok
    payload(call("remove_finding", finding_id=original["finding_id"], expected_revision=2,
                 reason="Corrected the mistaken oracle and rechecked the requirement."))
    assert status(verifier)["findings"] == status(verifier)["advisories"] == []
    assert not call("submit_verification_pass").ok
    payload(run_delta(verifier))
    assert payload(call("submit_verification_pass"))["submitted"] is True
    assert not call("update_finding", finding_id="too-late", expected_revision=0,
                    finding_kind="module_defect", priority="p1", summary="too late").ok
    assert not call("remove_verification_case", name="current delta", reason="too late").ok
    with fixture.repository.database.read_connection() as connection:
        audits = [json.loads(row[0])["_draft_audit"] for row in connection.execute(
            "SELECT result_json FROM bunshin_v2_submission_draft_ops WHERE draft_key = ?",
            (SubmissionDraftContext.from_workspace(workspace, draft_kind="work_items").draft_key,),
        )]
    assert any("mistaken" in json.dumps(audit) for audit in audits)


def test_removing_wrong_finding_does_not_clear_real_failure(verifier):
    _, _, _, call, _, _ = verifier
    wrong = finding(call)
    real = finding(call, "A real remaining product failure is reproduced.")
    payload(call("remove_finding", finding_id=wrong["finding_id"], expected_revision=1, reason="Wrong oracle."))
    payload(run_delta(verifier))
    assert [item["finding_id"] for item in status(verifier)["findings"]] == [real["finding_id"]]
    assert not status(verifier)["ready_by_outcome"]["pass"]
    assert not call("submit_verification_pass").ok


def test_deleted_identity_is_not_reused_and_replay_is_semantic(verifier):
    _, _, _, call, runtime, _ = verifier
    first = finding(call)
    remove = new_tool_call(name="remove_finding", args={"finding_id": first["finding_id"],
        "expected_revision": 1, "reason": "Wrong oracle."}, call_id="stable-remove")
    one = payload(asyncio.run(runtime.execute_tool_async(remove)))
    two = payload(asyncio.run(runtime.execute_tool_async(remove)))
    assert one == two
    conflict = new_tool_call(name="remove_finding", args={**remove.args, "expected_revision": 2}, call_id="stable-remove")
    assert not asyncio.run(runtime.execute_tool_async(conflict)).ok
    recreated = finding(call)
    assert recreated["finding_id"] != first["finding_id"]
    assert not call("remove_finding", finding_id=first["finding_id"], expected_revision=1, reason="Old ID").ok


def test_case_removal_invalidates_receipt_and_old_call_cannot_refresh_it(verifier):
    fixture, workspace, _, call, runtime, _ = verifier
    original = new_tool_call(name="run_verification_diff_risk", args={"name": "current delta",
        "command": "exit 0"}, call_id="original-execution")
    payload(asyncio.run(runtime.execute_tool_async(original)))
    payload(call("run_verification_adversarial_case", name="unrelated", command="exit 0"))
    payload(call("remove_verification_case", name="unrelated", reason="Duplicate coverage"))
    count = len(fixture.adapter.calls)
    receipt_count = len(workspace["review_tool_evidence_refs"])
    assert not status(verifier)["ready_by_outcome"]["pass"]
    replay = payload(asyncio.run(runtime.execute_tool_async(original)))
    assert replay["replayed"]
    assert len(fixture.adapter.calls) == count
    assert len(workspace["review_tool_evidence_refs"]) == receipt_count
    assert not status(verifier)["ready_by_outcome"]["pass"]
    payload(call("run_verification_diff_risk", **original.args))
    assert len(fixture.adapter.calls) == count + 1
    assert status(verifier)["ready_by_outcome"]["pass"]


def test_same_evaluation_fence_retry_keeps_current_findings_editable(verifier):
    fixture, workspace, _, call, _, _ = verifier
    created = finding(call)
    retry = fixture._advance_worker_fence(workspace)
    from pal.bunshin.v2.work_items import edit_finding_tool_result
    result = edit_finding_tool_result(new_tool_call(name="op_bunshin_remove_finding", args={
        "finding_id": created["finding_id"], "expected_revision": 1, "reason": "Corrected in same evaluation",
    }, call_id="retry-remove"), retry)
    assert result.ok, result.llm_text
    assert not [item for item in read_work_items(retry)["items"] if item["kind"] == "finding"]


def test_local_submit_freezes_sibling_and_submitted_inheritance_is_history(verifier):
    fixture, workspace, _, call, _, _ = verifier
    created = finding(call)
    store = SubmissionDraftStore(fixture.runtime_root)
    verification = SubmissionDraftContext.from_workspace(workspace, draft_kind="verification")
    snapshot = store.read(verification)
    store.mark_submitted(verification, expected_version=snapshot.version)
    with pytest.raises(ValueError, match="frozen"):
        store.mutate(replace(verification, draft_kind="work_items"), operation_key="late", request={},
                     reducer=lambda payload: ({"items": []}, {}))
    retry = fixture._advance_worker_fence(workspace)
    inherited = read_work_items(retry)
    assert not [item for item in inherited["items"] if item["kind"] == "finding"]
    assert created["finding_id"] in json.dumps(inherited["history"])


@pytest.mark.parametrize("verifier", [True], indirect=True)
def test_unrelated_fresh_command_cannot_revive_stale_historical_cases(verifier):
    fixture, workspace, probe, call, _, view = verifier
    command = f"{sys.executable} tests/router/verifier/probe.py"
    for name in ("finding_first", "finding_second"):
        payload(call("run_verification_historical_regression", name=name, command=command))
    payload(run_delta(verifier))
    probe.write_text("assert False\n")
    payload(call("run_verification_diff_risk", name="unrelated current check", command="true"))
    state = status(verifier)
    assert not state["ready_by_outcome"]["pass"]
    assert any("finding_first" in error and "stale" in error for error in state["blockers_by_outcome"]["pass"])
    assert not call("submit_verification_pass").ok
    from pal.bunshin.v2.swe_verification import semantic_verification_submission_errors
    from pal.bunshin.v2.semantic_evidence import recorded_cases
    context = SubmissionDraftContext.from_workspace(workspace, draft_kind="verification")
    draft = SubmissionDraftStore(fixture.runtime_root).read(context)
    errors = semantic_verification_submission_errors({"outcome": "pass", "findings": [],
        "recorded_results": recorded_cases(draft.payload), "tool_receipts": workspace["review_tool_evidence_refs"]},
        work_view=view, changed_paths=[], current_case_paths=["tests/router/verifier/probe.py"],
        corpus_scope=view["verification_corpus"], scratch_only=False, workspace=workspace)
    assert any("stale recorded cases" in error for error in errors)
    probe.write_text("assert 2 + 2 == 4\n")
    for name in ("finding_first", "finding_second"):
        payload(call("run_verification_historical_regression", name=name, command=command))
    payload(call("remove_verification_case", name="unrelated current check", reason="Not useful coverage"))
    payload(run_delta(verifier))
    assert payload(call("submit_verification_pass"))["submitted"]
