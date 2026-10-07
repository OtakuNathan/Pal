"""Transaction-local verifier authoring invariants and immutable audit records."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from pal.bunshin.v2.contracts import SubmissionInvariantError
from pal.bunshin.v2.role_input_identity import _semantic_role_input_refs


def receipt_freezes_authoring(connection: sqlite3.Connection, context: Any) -> bool:
    """A receipt freezes all sibling drafts, even before their projections catch up."""
    assignment = connection.execute(
        """SELECT a.state, a.submission_artifact_ref_json, a.submission_payload_hash
           FROM bunshin_v2_role_attempts t
           JOIN bunshin_v2_role_assignments a ON a.assignment_id = t.assignment_id
           WHERE t.attempt_id = ?""", (context.invocation_id,),
    ).fetchone()
    if assignment is not None and (
        str(assignment["submission_payload_hash"] or "")
        or str(assignment["state"]) in {"result_recorded", "settled"}
    ):
        return True
    return connection.execute(
        """SELECT 1 FROM bunshin_v2_submission_drafts
           WHERE workflow_id = ? AND invocation_id = ? AND fencing_token = ?
             AND role = ? AND mode = ? AND input_fingerprint = ? AND status = 'submitted'
           LIMIT 1""",
        (context.workflow_id, context.invocation_id, context.fencing_token,
         context.role, context.mode, context.input_fingerprint),
    ).fetchone() is not None


def assert_authoring_open(connection: sqlite3.Connection, context: Any) -> None:
    if receipt_freezes_authoring(connection, context):
        raise ValueError("submission Draft is already frozen; role assignment receipt already froze authoring")
    attempt = connection.execute(
        """SELECT a.state, t.status FROM bunshin_v2_role_attempts t
           JOIN bunshin_v2_role_assignments a ON a.assignment_id = t.assignment_id
           WHERE t.attempt_id = ?""", (context.invocation_id,),
    ).fetchone()
    if attempt is not None and (attempt["state"] != "running" or attempt["status"] != "running"):
        raise ValueError("submission Draft attempt is no longer running")


def assert_local_submission_authority(connection: sqlite3.Connection, context: Any,
                                      artifact_ref: Mapping[str, Any], payload_hash: str) -> None:
    receipt = connection.execute(
        """SELECT a.submission_artifact_ref_json, a.submission_payload_hash
           FROM bunshin_v2_role_attempts t JOIN bunshin_v2_role_assignments a
           ON a.assignment_id = t.assignment_id WHERE t.attempt_id = ?""", (context.invocation_id,),
    ).fetchone()
    if receipt is not None and receipt["submission_payload_hash"]:
        if (dict(artifact_ref) != json.loads(receipt["submission_artifact_ref_json"])
                or payload_hash != receipt["submission_payload_hash"]):
            raise ValueError("Draft projection must match its authoritative submission receipt")
        return
    assert_authoring_open(connection, context)


def reserved_finding_ids(connection: sqlite3.Connection, draft_key: str) -> set[str]:
    """Reserve current, historical and deleted keys from trusted stored lineage/audit."""
    reserved: set[str] = set()
    def collect(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key in {"finding_id", "item_id"} and isinstance(item, str):
                    reserved.add(item)
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    seen = set()
    while draft_key and draft_key not in seen:
        seen.add(draft_key)
        row = connection.execute(
            "SELECT payload_json, source_draft_key FROM bunshin_v2_submission_drafts WHERE draft_key = ?", (draft_key,),
        ).fetchone()
        if row is None:
            break
        collect(json.loads(row["payload_json"]))
        for operation in connection.execute(
            "SELECT result_json FROM bunshin_v2_submission_draft_ops WHERE draft_key = ?", (draft_key,),
        ):
            collect(json.loads(operation["result_json"]))
        draft_key = str(row["source_draft_key"] or "")
    return reserved


_EXECUTION_PROOF_FIELDS = (
    "status", "exit_code", "input_fingerprint", "case_binding", "definition_fingerprint",
)


def recorded_execution_proofs(connection: sqlite3.Connection, draft_key: str) -> dict[str, dict[str, Any]]:
    """Keep execution proof identity through withdrawal and owned-draft recovery."""
    proofs: dict[str, dict[str, Any]] = {}
    def collect(value: Any) -> None:
        if isinstance(value, Mapping):
            if value.get("execution_id") and "status" in value:
                _remember_execution_proof(proofs, value)
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    seen = set()
    while draft_key and draft_key not in seen:
        seen.add(draft_key)
        row = connection.execute(
            "SELECT payload_json, source_draft_key FROM bunshin_v2_submission_drafts WHERE draft_key = ?", (draft_key,),
        ).fetchone()
        if row is None:
            break
        collect(json.loads(row["payload_json"]))
        for row_op in connection.execute(
            "SELECT result_json FROM bunshin_v2_submission_draft_ops WHERE draft_key = ?", (draft_key,),
        ):
            audit = json.loads(row_op["result_json"]).get("_draft_audit", {})
            collect(audit.get("changes", {}))
        draft_key = str(row["source_draft_key"] or "")
    return proofs


def _remember_execution_proof(proofs: dict[str, dict[str, Any]], case: Mapping[str, Any]) -> None:
    identity = str(case.get("execution_id") or "")
    if not identity:
        return
    proof = {key: case.get(key) for key in _EXECUTION_PROOF_FIELDS}
    proof["environment"] = {key: dict(case.get("environment") or {}).get(key) for key in (
        "cwd", "runner", "workspace_root", "primary_language", "environment_fingerprint",
    )}
    for key in ("stdout_ref", "stderr_ref"):
        proof[key] = dict(case.get(key) or {}).get("sha256")
    if identity in proofs and proofs[identity] != proof:
        raise ValueError("recorded execution proof is immutable; rerun the case with a new execution ID")
    proofs[identity] = proof


def prepare_mutation(context: Any, previous: Mapping[str, Any], proposed: Mapping[str, Any],
                     *, execution_proofs: Mapping[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    payload = dict(proposed)
    if context.role != "verifier":
        return payload
    if payload.get("history", []) != previous.get("history", []):
        raise ValueError("external and submitted Draft history is immutable")
    if context.draft_kind == "verification":
        before = dict(dict(previous.get("evidence") or {}).get("cases") or {})
        after = dict(dict(payload.get("evidence") or {}).get("cases") or {})
        executions = dict(execution_proofs or {})
        for case in [*before.values(), *after.values()]:
            _remember_execution_proof(executions, case)
        payload["case_revision"] = int(previous.get("case_revision") or 0) + int(case_content(before) != case_content(after))
    return payload


def case_content(cases: Mapping[str, Any]) -> set[str]:
    """Case definitions/results change authority; formatting and duplicate storage do not."""
    values = []
    for case in cases.values():
        content = {key: case.get(key) for key in (
            "name", "case_kind", "command", "status", "exit_code", "input_fingerprint", "execution_id",
        )}
        for key in ("obligation_tags", "expected_exit_codes", "invariants"):
            content[key] = sorted({json.dumps(item, sort_keys=True) for item in case.get(key, [])})
        for key, fields in (("locations", ("path", "symbol", "section")),
                            ("requirements", ("section", "requirement"))):
            content[key] = sorted({json.dumps({field: item.get(field) for field in fields}, sort_keys=True)
                                   for item in case.get(key, [])})
        content["environment"] = {key: dict(case.get("environment") or {}).get(key) for key in
            ("cwd", "runner", "workspace_root", "primary_language", "environment_fingerprint")}
        for key in ("stdout_ref", "stderr_ref"):
            content[key] = dict(case.get(key) or {}).get("sha256")
        values.append(json.dumps(content, sort_keys=True))
    return set(values)


def audited_result(result: Mapping[str, Any], request: Mapping[str, Any], previous: Mapping[str, Any],
                   payload: Mapping[str, Any], version: int) -> dict[str, Any]:
    return {**dict(result), "_draft_audit": {
        "request": dict(request), "previous_version": version,
        "changes": {key: {"before": previous.get(key), "after": payload.get(key)}
                    for key in sorted(set(previous) | set(payload))
                    if previous.get(key) != payload.get(key)},
    }}


def public_operation_result(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "_draft_audit"}


def assert_draft_versions(connection: sqlite3.Connection, versions: Mapping[str, int]) -> None:
    for key, expected in versions.items():
        row = connection.execute(
            "SELECT version, status FROM bunshin_v2_submission_drafts WHERE draft_key = ?", (key,),
        ).fetchone()
        if row is None or int(row["version"]) != int(expected) or row["status"] != "active":
            from pal.bunshin.v2.submission_errors import SubmissionValidationError
            raise SubmissionValidationError("submission Draft CAS conflict before receipt acceptance")


def _stored_retry_inputs(assignment: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], int]:
    try:
        references = json.loads(assignment["input_refs_json"])
        execution = json.loads(assignment["execution_spec_json"])
        if not isinstance(execution, Mapping):
            raise ValueError("execution spec must be an object")
        generation = execution.get("evaluation_generation", 0)
        if type(generation) is not int or generation < 0:
            raise ValueError("evaluation generation must be a nonnegative integer")
        return _semantic_role_input_refs(references, role=str(assignment["role"]), mode=str(assignment["mode"])), generation
    except (TypeError, ValueError) as exc:
        raise SubmissionInvariantError(f"verifier draft source identity is malformed: {exc}") from exc


def source_is_owned_retry(connection: sqlite3.Connection, context: Any, source: Mapping[str, Any]) -> bool:
    attempts = connection.execute(
        """SELECT t.attempt_id, a.* FROM bunshin_v2_role_attempts t
           JOIN bunshin_v2_role_assignments a ON a.assignment_id = t.assignment_id
           WHERE t.attempt_id IN (?, ?)""",
        (context.invocation_id, source["invocation_id"]),
    ).fetchall()
    identities = {row["attempt_id"]: dict(row) for row in attempts}
    if identities:
        if context.invocation_id not in identities or source["invocation_id"] not in identities:
            return False
        current, prior = identities[context.invocation_id], identities[source["invocation_id"]]
        current_inputs, current_generation = _stored_retry_inputs(current)
        prior_inputs, prior_generation = _stored_retry_inputs(prior)
        return (all(current[key] == prior[key] for key in (
            "session_id", "workflow_id", "aggregate_type", "aggregate_id", "role", "mode",
            "input_fingerprint", "submission_kind",
        )) and current_inputs == prior_inputs and current_generation == prior_generation)
    # Local authoring without a role assignment may recover only its own lease lineage.
    return (context.invocation_id == source["invocation_id"]
            or context.lease_resource_key == source["lease_resource_key"])


def assert_verifier_projection(submission: Mapping[str, Any], draft: Mapping[str, Any],
                              work_items: Mapping[str, Any]) -> None:
    from pal.bunshin.v2.draft_values import partition_findings
    from pal.bunshin.v2.draft_values import recorded_cases
    from pal.bunshin.v2.draft_values import submission_work_items
    from pal.bunshin.v2.submission_errors import SubmissionValidationError
    findings, advisories = partition_findings([
        {**dict(item.get("finding") or {}), "finding_id": item["item_id"]}
        for item in work_items.get("items", []) if item.get("kind") == "finding"
    ])
    for name, expected in (("findings", findings), ("advisories", advisories),
                           ("recorded_results", recorded_cases(draft)),
                           ("work_items", submission_work_items(work_items.get("items")))):
        if submission.get(name, []) != expected:
            raise SubmissionValidationError(f"submission {name} do not match the authoritative current draft")
    binding = submission.get("verification_binding")
    if isinstance(binding, Mapping) and binding.get("case_revision") != int(draft.get("case_revision") or 0):
        raise SubmissionValidationError("submission testcase revision is stale")


def inherit_verifier_payload(payload: Mapping[str, Any], *, source: Mapping[str, Any], frozen: bool, reason: str = "external_source") -> dict[str, Any]:
    """Ownership is supplied by stored source identity/receipt, never payload origin."""
    copied = json.loads(json.dumps(dict(payload)))
    if not frozen:
        return copied
    history = list(copied.get("history") or [])
    history.append({"source_draft_key": str(source["draft_key"]),
                    "source_status": str(source["status"]), "classification": reason, "payload": {
                        key: value for key, value in copied.items() if key != "history"}})
    if source["draft_kind"] == "work_items":
        return {"items": [item for item in copied.get("items", []) if item.get("kind") != "finding"],
                "history": history}
    return {"definitions": copied.get("definitions", {}), "evidence": {"cases": {}},
            "findings": [], "summary": {}, "case_revision": 0, "history": history}
