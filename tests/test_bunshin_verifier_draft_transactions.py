"""SQLite refinement: receipt is the linearization point for both draft rows."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import hashlib
import threading

import pytest

from pal.bunshin.contracts import AggregateType
from pal.bunshin.role_protocol import RoleAssignmentRequest
from pal.bunshin.submission_drafts import AUTHORING_CONTRACT_VERSION, SubmissionDraftContext, SubmissionDraftStore
from pal.bunshin.work_items import submission_work_items
from pal.bunshin.review_findings import partition_findings
from pal.bunshin.semantic_evidence import recorded_cases
from tests import test_bunshin_v2_role_gateway as gateway_tests


@pytest.fixture
def gateway():
    fixture = gateway_tests.BunshinV2RoleGatewayTests()
    fixture.setUp()
    repo = fixture.service.repository
    repo.role_sessions.ensure_role_session(session_id="draft-verifier-session", workflow_id="workflow-router",
        aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id="node-router", role="verifier", mode="module",
        role_profile_id="software_engineering.v2_verifier", family_binding_sha="binding", scope_kind="module", subject_key="router")
    assignment = repo.role_assignments.create_role_assignment(RoleAssignmentRequest(
        assignment_key="draft-verifier", session_id="draft-verifier-session", workflow_id="workflow-router",
        aggregate_type=AggregateType.DAG_NODE_RUN.value, aggregate_id="node-router", role="verifier", mode="module",
        role_profile_id="software_engineering.v2_verifier", family_binding_sha="binding", input_fingerprint="draft-input",
        required_inputs=(), input_refs={}, execution_spec={"effect_type": "run_verifier_role", "evaluation_generation": 4}, submission_kind="verification"))
    attempt = repo.role_assignments.claim_role_assignment(assignment["assignment_id"])
    resource = "assignment:" + assignment["assignment_id"]
    fence = repo.leases.claim_lease(resource, attempt["attempt_id"], ttl_seconds=120).fencing_token
    external = {"kind": "finding", "summary": "External review history", "origin": "verifier:module",
                "finding": {"finding_kind": "module_defect", "priority": "p1", "summary": "External review history"}}
    prompt = fixture.service.artifacts.put_json({"workspace": {"repo_path": str(fixture.workspace),
        "bunshin_v2": {"work_item_seed": [external]}}}, artifact_type="RolePromptPackArtifact")
    repo.role_attempts.start_role_attempt(assignment_id=assignment["assignment_id"], attempt_id_value=attempt["attempt_id"],
        lease_resource_key=resource, fencing_token=fence, prompt_pack_ref=prompt.to_dict())
    token = repo.role_access.issue_role_attempt_access_token(assignment_id=assignment["assignment_id"],
        attempt_id_value=attempt["attempt_id"], fencing_token=fence)
    context = SubmissionDraftContext(workflow_id="workflow-router", invocation_id=attempt["attempt_id"],
        lease_resource_key=resource, fencing_token=fence, role="verifier", mode="module", draft_kind="verification",
        input_fingerprint="draft-input", authoring_contract_version=AUTHORING_CONTRACT_VERSION)
    def call(method, **params):
        return fixture.gateway.call(method, {"access_token": token, **params})
    store = SubmissionDraftStore(fixture.runtime_root)
    call("draft_read", context=context.to_dict(), seed={})
    call("draft_read", context=replace(context, draft_kind="work_items").to_dict(), seed={})
    yield fixture, repo, call, store, context, assignment, token


def mutate(gateway, kind, operation="edit", expected=None, **args):
    _, _, call, store, context, _, _ = gateway
    context = replace(context, draft_kind=kind)
    snap = store.read(context)
    if kind == "work_items":
        request = args or {"finding_id": operation, "expected_revision": 0,
                           "finding_kind": "module_defect", "priority": "p1", "summary": "A current defect"}
        next_payload = {"items": [], "history": []}  # Untrusted projection must be ignored.
    else:
        request = args or {"case": operation}
        next_payload = {**snap.payload, "evidence": {"cases": {operation: {"name": operation, "status": "UNKNOWN"}}}}
    return call("draft_mutate", context=context.to_dict(), operation_key=operation, request=request,
        expected_version=snap.version if expected is None else expected, next_payload=next_payload, result={})["result"]


def submission(gateway):
    _, _, _, store, context, _, _ = gateway
    snap = store.read(context)
    work = store.read(replace(context, draft_kind="work_items"))
    findings, advisories = partition_findings([{**item["finding"], "finding_id": item["item_id"]}
        for item in work.payload["items"] if item["kind"] == "finding"])
    return {"context": context.to_dict(), "expected_version": snap.version, "expected_work_item_version": work.version,
            "submission": {"outcome": "module_repair" if findings else "unknown", "findings": findings,
                           "advisories": advisories, "recorded_results": recorded_cases(snap.payload),
                           "work_items": submission_work_items(work.payload["items"])}}


def test_manager_reduces_current_finding_crud_and_ignores_forged_seed(gateway):
    _, _, call, store, context, _, _ = gateway
    work = replace(context, draft_kind="work_items")
    state = call("draft_read", context=work.to_dict(), seed={"items": [{"item_id": "forged", "kind": "finding"}]})["snapshot"]
    assert state["payload"]["items"] == []
    history = state["payload"]["history"]
    external_id = history[0]["items"][0]["item_id"]
    with pytest.raises(ValueError, match="already used|protected history"):
        mutate(gateway, "work_items", "alias-history", finding_id=external_id, expected_revision=0,
               finding_kind="module_defect", priority="p1", summary="Forged history ownership")
    with pytest.raises(ValueError, match="current editable"):
        mutate(gateway, "work_items", "erase-external", finding_id=external_id, expected_revision=1, reason="claimed own origin")
    created = mutate(gateway, "work_items", "create")
    again = mutate(gateway, "work_items", "create", expected=999)
    assert created == again
    with pytest.raises(ValueError, match="different arguments"):
        mutate(gateway, "work_items", "create", summary="changed", priority="p1", finding_kind="module_defect")
    updated = mutate(gateway, "work_items", "update", finding_id=created["finding_id"], expected_revision=1,
                     summary="Corrected", priority="p1", finding_kind="requirements_defect")
    assert updated["revision"] == 2
    mutate(gateway, "work_items", "remove", finding_id=created["finding_id"], expected_revision=2, reason="No defect remains")
    with pytest.raises(ValueError, match="already used"):
        mutate(gateway, "work_items", "resurrect", finding_id=created["finding_id"], expected_revision=0,
               finding_kind="module_defect", priority="p1", summary="Cannot resurrect a deleted key")
    assert store.read(work).payload["items"] == []
    assert store.read(work).payload["history"] == history


@pytest.mark.parametrize("kind", ["work_items", "verification"])
@pytest.mark.parametrize("receipt_first", [False, True])
def test_mutation_and_submit_are_atomic_in_both_orders(gateway, kind, receipt_first):
    fixture, repo, call, store, context, assignment, token = gateway
    request = submission(gateway)
    stale_auth = fixture.gateway.authorize(token)
    reached, release = threading.Event(), threading.Event()
    original = repo.role_submissions.record_role_submission
    def paused(**kwargs):
        receipt = original(**kwargs) if receipt_first else None
        reached.set()
        assert release.wait(10)
        return receipt if receipt_first else original(**kwargs)
    repo.role_submissions.record_role_submission = paused
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(call, "draft_submit", **request)
        assert reached.wait(10)
        try:
            if receipt_first:
                target = replace(context, draft_kind=kind)
                with pytest.raises(ValueError, match="frozen"):
                    fixture.gateway._draft_mutate(stale_auth, {"context": target.to_dict(),
                        "operation_key": "after-receipt", "request": {"plan": [{"step": "audit", "status": "completed"}]},
                        "expected_version": 0, "next_payload": {}, "result": {}})
            else:
                mutate(gateway, kind, "before-receipt")
        finally:
            release.set()
        if receipt_first:
            assert future.result()["submitted"]
        else:
            with pytest.raises(ValueError, match="CAS conflict"):
                future.result()
            assert not repo.role_assignments.read_role_assignment(assignment["assignment_id"])["submission_artifact_ref"]


def test_submission_cannot_omit_current_findings(gateway):
    _, _, call, _, _, _, _ = gateway
    mutate(gateway, "work_items")
    request = submission(gateway)
    request["submission"].update(outcome="pass", findings=[])
    with pytest.raises(ValueError, match="authoritative"):
        call("draft_submit", **request)


def test_pending_receipt_freezes_both_local_drafts_before_projection(gateway):
    _, repo, call, store, context, assignment, _ = gateway
    request = submission(gateway)
    original = repo.role_submissions.record_role_submission
    def lose_ack(**kwargs):
        original(**kwargs)
        raise OSError("lost receipt acknowledgment")
    repo.role_submissions.record_role_submission = lose_ack
    with pytest.raises(OSError):
        call("draft_submit", **request)
    for kind in ("work_items", "verification"):
        target = replace(context, draft_kind=kind)
        assert store.read(target).status == "active"
        with pytest.raises(ValueError, match="frozen"):
            store.mutate(target, operation_key="late", request={}, reducer=lambda payload: ({}, {}))
    repo.role_submissions.record_role_submission = original
    receipt = call("draft_submit", **request)
    assert receipt["submitted"]
    with pytest.raises(ValueError, match="authoritative submission receipt"):
        store.mark_submitted(context, expected_version=0, submission_artifact_ref=receipt["submission_artifact_ref"],
                             submission_payload_hash="wrong")
    store.mark_submitted(context, expected_version=0, submission_artifact_ref=receipt["submission_artifact_ref"],
                         submission_payload_hash=receipt["submission_payload_hash"])
    assert store.read(context).status == "submitted"
    request["submission"]["reason"] = "conflicting payload"
    with pytest.raises(ValueError, match="different submission"):
        call("draft_submit", **request)


def replacement_attempt(gateway, *, generation=4, pending_receipt=False):
    fixture, repo, call, store, context, assignment, _ = gateway
    if pending_receipt:
        request = submission(gateway)
        original = repo.role_submissions.record_role_submission
        def receipt_without_projection(**kwargs):
            original(**kwargs)
            raise OSError("projection interrupted")
        repo.role_submissions.record_role_submission = receipt_without_projection
        with pytest.raises(OSError):
            call("draft_submit", **request)
        repo.role_submissions.record_role_submission = original
    repo.leases.release_lease(context.lease_resource_key, context.invocation_id, context.fencing_token)
    # Fixture recovery boundary: old worker quiesced; durable source/receipt is retained.
    with repo.database.write_connection() as connection:
        connection.execute("UPDATE bunshin_v2_role_assignments SET state = 'cancelled' WHERE assignment_id = ?",
                           (assignment["assignment_id"],))
        connection.execute("UPDATE bunshin_v2_role_sessions SET status = 'suspended' WHERE session_id = ?",
                           (assignment["session_id"],))
    next_assignment = repo.role_assignments.create_role_assignment(RoleAssignmentRequest(
        assignment_key="continued-same-evaluation", session_id=assignment["session_id"], workflow_id=assignment["workflow_id"],
        aggregate_type=assignment["aggregate_type"], aggregate_id=assignment["aggregate_id"], role="verifier", mode="module",
        role_profile_id=assignment["role_profile_id"], family_binding_sha=assignment["family_binding_sha"],
        input_fingerprint=assignment["input_fingerprint"], required_inputs=(), input_refs={},
        execution_spec={"effect_type": "run_verifier_role", "evaluation_generation": generation}, submission_kind="verification"))
    attempt = repo.role_assignments.claim_role_assignment(next_assignment["assignment_id"])
    resource = "assignment:" + next_assignment["assignment_id"]
    fence = repo.leases.claim_lease(resource, attempt["attempt_id"], ttl_seconds=120).fencing_token
    original_attempt = repo.role_attempts.read_role_attempt(context.invocation_id)
    repo.role_attempts.start_role_attempt(assignment_id=next_assignment["assignment_id"], attempt_id_value=attempt["attempt_id"],
        lease_resource_key=resource, fencing_token=fence, prompt_pack_ref=original_attempt["prompt_pack_ref"])
    return replace(context, invocation_id=attempt["attempt_id"], lease_resource_key=resource, fencing_token=fence)


@pytest.mark.parametrize("generation,pending,editable", [(4, False, True), (5, False, False), (4, True, False)])
def test_trusted_logical_evaluation_owns_retry_but_receipt_or_new_generation_is_history(
    gateway, generation, pending, editable,
):
    _, _, _, store, context, _, _ = gateway
    finding = mutate(gateway, "work_items", "source-finding")
    next_context = replacement_attempt(gateway, generation=generation, pending_receipt=pending)
    inherited = store.read(replace(next_context, draft_kind="work_items"))
    ids = [item["item_id"] for item in inherited.payload["items"] if item["kind"] == "finding"]
    assert (finding["finding_id"] in ids) is editable
    if editable:
        from pal.bunshin.work_items import prepare_work_item_mutation
        workspace = {"runtime_root": str(store.runtime_root), "bunshin_v2": {
            **next_context.to_dict(), "authoring_input_fingerprint": next_context.input_fingerprint}}
        args = {"finding_id": finding["finding_id"], "expected_revision": 1, "reason": "Same evaluation correction"}
        store.mutate(replace(next_context, draft_kind="work_items"), operation_key="continued-remove", request=args,
                     reducer=prepare_work_item_mutation(workspace, args, "continued-remove"))
    else:
        assert finding["finding_id"] in json.dumps(inherited.payload["history"])
    # No edit to the copied draft can erase the source row.
    with store._transaction() as connection:
        source = connection.execute("SELECT payload_json FROM bunshin_v2_submission_drafts WHERE draft_key = ?",
                                    (replace(context, draft_kind="work_items").draft_key,)).fetchone()
        assert finding["finding_id"] in source[0]


def test_gateway_rejects_stale_identity_and_cas(gateway):
    _, _, call, store, context, _, _ = gateway
    for stale in (replace(context, role="reviewer", mode="standalone"), replace(context, input_fingerprint="old"),
                  replace(context, fencing_token=context.fencing_token + 1)):
        with pytest.raises(ValueError):
            call("draft_mutate", context=stale.to_dict(), operation_key="stale", request={}, expected_version=0,
                 next_payload={}, result={})
    mutate(gateway, "verification", "first-case")
    with pytest.raises(RuntimeError, match="CAS conflict"):
        mutate(gateway, "verification", "second-case", expected=0)


def test_remote_explicit_cas_is_not_rebased_and_lost_ack_requires_exact_payload(gateway):
    _, _, call, _, context, _, _ = gateway
    class Remote:
        def request_sync(self, method, params):
            return call(method, **params)
    remote = SubmissionDraftStore(gateway[0].runtime_root)
    remote._role_gateway = Remote()
    snapshot = remote.read(context)
    mutate(gateway, "verification", "concurrent-case")
    with pytest.raises(RuntimeError, match="CAS conflict"):
        remote.mutate(context, operation_key="stale-execution", request={}, expected_version=snapshot.version,
                      reducer=lambda payload: (payload, {}))
    request = submission(gateway)
    receipt = call("draft_submit", **request)
    class LostReply(Remote):
        def request_sync(self, method, params):
            if method == "draft_submit":
                raise OSError("lost response")
            return super().request_sync(method, params)
    remote._role_gateway = LostReply()
    assert remote.mark_submitted(context, expected_version=request["expected_version"],
        submission_payload=request["submission"])["submission_payload_hash"] == receipt["submission_payload_hash"]
    with pytest.raises(OSError, match="lost response"):
        remote.mark_submitted(context, expected_version=request["expected_version"],
                              submission_payload={**request["submission"], "reason": "different"})


@pytest.mark.parametrize("kind", ["verification", "work_items"])
def test_retry_retirement_after_authentication_rejects_mutation_inside_transaction(gateway, kind):
    fixture, repo, _, store, context, assignment, token = gateway
    authenticated = fixture.gateway.authorize(token)
    target = replace(context, draft_kind=kind)
    before = store.read(target)
    repo.role_retries.queue_role_attempt_retry(assignment_id=assignment["assignment_id"],
        attempt_id_value=context.invocation_id, error_kind="worker_lost", error_text="test retirement")
    with pytest.raises(ValueError, match="no longer running"):
        fixture.gateway._draft_mutate(authenticated, {"context": target.to_dict(), "operation_key": "retired",
            "request": {"finding_kind": "module_defect", "priority": "p1", "summary": "retired write"},
            "expected_version": before.version, "next_payload": {}, "result": {}})
    with pytest.raises(ValueError, match="no longer running"):
        store.assert_editable(target)
    assert store.read(target).version == before.version


def test_new_local_submission_rejects_retired_attempt(gateway):
    fixture, repo, call, store, context, assignment, _ = gateway
    request = submission(gateway)
    ref = fixture.service.artifacts.put_json(request["submission"], artifact_type="VerifierRoleSubmissionArtifact")
    repo.role_retries.queue_role_attempt_retry(assignment_id=assignment["assignment_id"],
        attempt_id_value=context.invocation_id, error_kind="worker_lost", error_text="retired before local submit")
    with pytest.raises(ValueError, match="no longer running"):
        store.mark_submitted(context, expected_version=0, submission_artifact_ref=ref.to_dict(), submission_payload_hash=ref.sha256)
    assert store.read(context).status == "active"


def test_upsert_deleted_identity_remains_reserved_after_same_evaluation_retry(gateway):
    _, _, _, store, _, _, _ = gateway
    created = mutate(gateway, "work_items", "reserved-key")
    mutate(gateway, "work_items", "delete", finding_id=created["finding_id"], expected_revision=1, reason="Mistaken")
    retry = replacement_attempt(gateway)
    from pal.bunshin.work_items import prepare_work_item_mutation
    workspace = {"runtime_root": str(store.runtime_root), "bunshin_v2": {
        **retry.to_dict(), "authoring_input_fingerprint": retry.input_fingerprint}}
    args = {"finding_id": created["finding_id"], "expected_revision": 0,
            "finding_kind": "module_defect", "priority": "p1", "summary": "Same key reuse"}
    with pytest.raises(ValueError, match="already used"):
        store.mutate(replace(retry, draft_kind="work_items"), operation_key="resurrect-retry", request=args,
                     reducer=prepare_work_item_mutation(workspace, args, "resurrect-retry"))


@pytest.mark.parametrize("delete_first", [False, True])
def test_legacy_add_allocates_fresh_key_when_caller_upsert_reserved_its_default(gateway, delete_first):
    _, _, _, _, context, _, _ = gateway
    work = replace(context, draft_kind="work_items")
    collision = "finding_" + hashlib.sha256((work.draft_key + ":legacy-call").encode()).hexdigest()[:16]
    mutate(gateway, "work_items", "canonical-create", finding_id=collision, expected_revision=0,
           finding_kind="module_defect", priority="p1", summary="Canonical concern")
    if delete_first:
        mutate(gateway, "work_items", "canonical-delete", finding_id=collision, expected_revision=1, reason="Mistaken")
    legacy = mutate(gateway, "work_items", "legacy-call", finding_kind="module_defect", priority="p1", summary="Legacy concern")
    assert legacy["finding_id"] != collision


def test_concurrent_revision_zero_inserts_for_one_key_have_exactly_one_winner(gateway):
    _, _, call, store, context, _, _ = gateway
    work = replace(context, draft_kind="work_items")
    ready = threading.Barrier(2)
    def insert(operation):
        ready.wait(timeout=10)
        try:
            return call("draft_mutate", context=work.to_dict(), operation_key=operation,
                request={"finding_id": "shared-new-key", "expected_revision": 0,
                         "finding_kind": "module_defect", "priority": "p1", "summary": "One concern"},
                expected_version=0, next_payload={}, result={})
        except (ValueError, RuntimeError) as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(insert, ("insert-a", "insert-b")))
    assert sum(isinstance(result, dict) for result in results) == 1
    rejected = next(result for result in results if isinstance(result, Exception))
    assert "CAS conflict" in str(rejected) or "already used" in str(rejected)
    snapshot = store.read(work)
    assert snapshot.version == 1
    assert [(item["item_id"], item["revision"]) for item in snapshot.payload["items"]] == [("shared-new-key", 1)]


@pytest.mark.parametrize("field,value", [("case_binding", {"corpus": [["probe", "new"]]}),
                                        ("status", "PASS"), ("definition_fingerprint", "forged")])
def test_precomputed_mutation_cannot_rewrite_existing_execution_proof(gateway, field, value):
    _, _, call, store, context, _, _ = gateway
    case = {"name": "required regression", "status": "FAIL", "execution_id": "executed-once",
            "case_binding": {"corpus": [["probe", "old"]]}, "definition_fingerprint": "original"}
    call("draft_mutate", context=context.to_dict(), operation_key="record", request={"execute": "once"},
         expected_version=0, next_payload={"evidence": {"cases": {case["name"]: case}}}, result={})
    original = store.read(context)
    changed = {**case, field: value}
    with pytest.raises(ValueError, match="execution proof is immutable"):
        call("draft_mutate", context=context.to_dict(), operation_key="rewrite", request={"rewrite": field},
             expected_version=original.version, next_payload={**original.payload,
                "evidence": {"cases": {case["name"]: changed}}}, result={})
    assert store.read(context) == original


@pytest.mark.parametrize("recover", [False, True])
def test_removed_execution_proof_remains_immutable_through_owned_retry(gateway, recover):
    _, _, call, store, context, _, _ = gateway
    case = {"name": "required regression", "status": "FAIL", "execution_id": "withdrawn-execution",
            "case_binding": {"corpus": [["probe", "old"]]}, "definition_fingerprint": "original"}
    call("draft_mutate", context=context.to_dict(), operation_key="record", request={}, expected_version=0,
         next_payload={"evidence": {"cases": {case["name"]: case}}}, result={})
    snap = store.read(context)
    store.mutate(context, operation_key="withdraw", request={"remove": case["name"]},
                 reducer=lambda payload: ({**payload, "evidence": {"cases": {}}}, {}))
    if recover:
        context = replacement_attempt(gateway)
    snap = store.read(context)
    with pytest.raises(ValueError, match="execution proof is immutable"):
        store.mutate_precomputed(context, operation_key="rebind", request={}, expected_version=snap.version,
            next_payload={**snap.payload, "evidence": {"cases": {case["name"]: {
                **case, "case_binding": {"corpus": [["probe", "new"]]}}}}}, result={})
    assert store.read(context) == snap


def test_one_execution_id_cannot_describe_two_different_proofs(gateway):
    _, _, call, store, context, _, _ = gateway
    case = {"name": "first", "status": "FAIL", "execution_id": "ambiguous"}
    with pytest.raises(ValueError, match="execution proof is immutable"):
        call("draft_mutate", context=context.to_dict(), operation_key="two-proofs", request={}, expected_version=0,
             next_payload={"evidence": {"cases": {"first": case, "second": {**case, "status": "PASS"}}}}, result={})
    assert store.read(context).version == 0
