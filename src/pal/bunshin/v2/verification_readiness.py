"""Content-bound verifier execution receipts (never authored by tool callers)."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from pal.shared.tool_protocol import FailedResult


def shell_execution_output(result: Any) -> dict[str, Any]:
    """Unwrap the actual runtime's failed-command envelope, not stdout JSON."""
    if isinstance(result.invocation_result, FailedResult):
        return {**dict(result.invocation_result.details),
                "_runtime_error_code": result.invocation_result.error_code}
    return dict(result.structured or {})


def verification_case_errors(
    cases: list[dict[str, Any]], *, outcome: str, workspace: Mapping[str, Any] | None,
    allow_legacy: bool = False,
) -> list[str]:
    """Shared local/Manager status and assignment-freshness gate."""
    errors = []
    if outcome == "pass":
        unresolved = [str(item.get("name") or "") for item in cases
                      if item.get("status") != "PASS"]
        if unresolved:
            errors.append("PASS requires resolved PASS evidence; failed or UNKNOWN cases (including unrecognized statuses): " + ", ".join(unresolved))
    if workspace is not None:
        current_input = str(dict(workspace.get("bunshin_v2") or {}).get("authoring_input_fingerprint") or "")
        stale = [str(item.get("name") or "") for item in cases
                 if item.get("input_fingerprint") and item["input_fingerprint"] != current_input]
        if stale:
            errors.append("rerun evidence for the current Candidate: " + ", ".join(stale))
        executed = [item for item in cases if item.get("status") in {"PASS", "FAIL"}]
        current = case_corpus_binding(verification_corpus_snapshot(workspace)) if executed else {}
        stale_cases = [str(item.get("name") or "") for item in executed if (
                           (not allow_legacy or "case_binding" in item) and item.get("case_binding") != current
                           or (not allow_legacy or "definition_fingerprint" in item)
                           and item.get("definition_fingerprint") != case_definition_fingerprint(item))]
        if stale_cases:
            errors.append("rerun stale recorded cases for the current Candidate, corpus and case definition: "
                          + ", ".join(stale_cases))
    return errors


def case_corpus_binding(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in snapshot.items() if key != "case_revision"}


def case_definition_fingerprint(case: Mapping[str, Any]) -> str:
    definition = {key: case.get(key) for key in (
        "name", "case_kind", "command", "expected_exit_codes", "requirements",
        "locations", "invariants", "obligation_tags",
    )}
    import json
    return hashlib.sha256(json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verification_case_revision(workspace: Mapping[str, Any]) -> int:
    if "verification_case_revision" in workspace:
        return int(workspace["verification_case_revision"])
    binding = dict(workspace.get("bunshin_v2") or {})
    if binding.get("role") != "verifier" or not workspace.get("runtime_root") or not binding.get("invocation_id"):
        return 0
    from pal.bunshin.v2.submission_drafts import SubmissionDraftContext, SubmissionDraftStore
    context = SubmissionDraftContext.from_workspace(workspace, draft_kind="verification")
    snapshot = SubmissionDraftStore(Path(str(workspace["runtime_root"]))).read(context)
    return int(snapshot.payload.get("case_revision") or 0)


def bind_recorded_case_execution(workspace: Mapping[str, Any], before: Mapping[str, Any], revision: int) -> None:
    """Attach this execution to the case revision it just recorded; never revive old runs."""
    refs = workspace.get("review_tool_evidence_refs")
    if not isinstance(refs, list) or not refs:
        return
    receipt = refs[-1]
    if receipt.get("verification_binding") != dict(before):
        raise ValueError("case execution receipt changed before recording")
    current = verification_corpus_snapshot(workspace)
    expected = {**dict(before), "case_revision": revision}
    receipt["verification_binding"] = expected
    receipt["stale"] = bool(receipt.get("stale")) or expected != current


def lsp_verification_status(result: Any) -> str:
    """A completed RPC with missing/version-unknown diagnostics is not PASS."""
    output = dict(result.structured or {})
    data = dict(output.get("result") or output)
    evidence = dict(output.get("evidence") or {})
    if (not result.ok or output.get("operation") not in {None, "diagnostics"}
            or str(output.get("status") or "") not in {"", "ok"}
            or str(data.get("status") or "") not in {"", "ok"}
            or data.get("diagnostics_state") != "fresh"
            or evidence.get("freshness") not in {None, "fresh"}
            or output.get("cancelled") or data.get("cancelled")
            or output.get("timed_out") or data.get("timed_out")
            or not isinstance(data.get("diagnostics"), list)
            or any(not isinstance(item, Mapping) for item in data.get("diagnostics", []))):
        return "UNKNOWN"
    return "FAIL" if any(isinstance(item, Mapping)
                        and str(item.get("severity") or "").lower() in {"1", "error"}
                        for item in data["diagnostics"]) else "PASS"


def current_verification_receipts(
    receipts: list[dict[str, Any]], workspace: Mapping[str, Any],
    *, allow_legacy: bool = False,
) -> list[dict[str, Any]]:
    """Invalidate bound receipts on content/assignment changes, without re-stamping."""
    current = verification_corpus_snapshot(workspace)
    result = []
    for item in receipts:
        receipt = dict(item)
        bound = receipt.get("verification_binding")
        # Legacy receipts remain readable history, but cannot establish which
        # Candidate/corpus ran. Only a new execution can supply that proof.
        expected = (case_corpus_binding(current) if allow_legacy and isinstance(bound, Mapping)
                    and "case_revision" not in bound else current)
        if not isinstance(bound, Mapping) or dict(bound) != expected:
            receipt["ok"] = False
            receipt["stale"] = True
        result.append(receipt)
    return result


def final_verification_errors(receipts: list[dict[str, Any]], *, outcome: str, changed: bool) -> list[str]:
    checks = final_verification_checks(receipts)
    errors = []
    if not checks and any(item.get("stale") for item in receipts):
        errors.append("fresh validation required: prior execution receipts do not prove the current Candidate and corpus")
    if changed and not checks:
        errors.append("run verification again after the final test edit")
    if outcome == "pass" and not any(bool(item.get("ok")) for item in checks):
        errors.append("PASS requires a successful final command or LSP receipt")
    return errors


def final_verification_checks(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    last_write = max((i for i, item in enumerate(receipts)
                      if item.get("kind") == "test_write"), default=-1)
    return [item for i, item in enumerate(receipts)
            if i > last_write and item.get("kind") in {"command", "lsp"}
            and not item.get("stale")]


def record_verification_execution(
    workspace: Mapping[str, Any], call: Any, result: Any,
    before: Mapping[str, Any],
) -> None:
    """Record the delegated execution, never a claimed receipt in its output."""
    from pal.bunshin.v2.review_receipts import _review_tool_evidence_ref

    receipt = _review_tool_evidence_ref(call.name, call, result)
    if not receipt:
        return
    receipt["verification_binding"] = dict(before)
    receipt["stale"] = dict(before) != verification_corpus_snapshot(workspace)
    # A handler completing is not proof that its command succeeded. In
    # particular expected nonzero semantic cases cannot manufacture final PASS.
    if receipt["kind"] == "command":
        output = shell_execution_output(result)
        receipt["ok"] = (bool(result.ok) and type(output.get("returncode")) is int and output["returncode"] == 0
                         and not output.get("timed_out") and not output.get("cancelled"))
    elif receipt["kind"] == "lsp":
        receipt["ok"] = call.name == "op_lsp_diagnostics" and lsp_verification_status(result) == "PASS"
    refs = workspace.get("review_tool_evidence_refs")
    if refs is None and isinstance(workspace, dict):
        refs = workspace.setdefault("review_tool_evidence_refs", [])
    if isinstance(refs, list):
        refs.append(receipt)


def verification_corpus_snapshot(workspace: Mapping[str, Any]) -> dict[str, Any]:
    from pal.bunshin.v2.verification_corpus import verification_corpus_snapshot as snapshot
    return snapshot({**dict(workspace), "verification_case_revision": verification_case_revision(workspace)})
