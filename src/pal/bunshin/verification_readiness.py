"""Content-bound verifier execution receipts (never authored by tool callers)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from pal.bunshin.repair_precheck import suite_command
from pal.bunshin.verification_report import VERIFICATION_REPORT_FILENAME
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
        current_input = str(dict(workspace.get("bunshin") or {}).get("authoring_input_fingerprint") or "")
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
    binding = dict(workspace.get("bunshin") or {})
    if binding.get("role") != "verifier" or not workspace.get("runtime_root") or not binding.get("invocation_id"):
        return 0
    from pal.bunshin.submission_drafts import SubmissionDraftContext, SubmissionDraftStore
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
    if outcome == "pass" and not any(item.get("kind") == "command" and item.get("ok") for item in checks):
        errors.append("PASS requires a successful final command receipt")
    return errors


def final_verification_checks(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    last_write = max((i for i, item in enumerate(receipts)
                      if item.get("kind") == "test_write"), default=-1)
    return [item for i, item in enumerate(receipts)
            if i > last_write and item.get("kind") in {"command", "lsp"}
            and not item.get("stale")]


def verdict_pass_errors(
    workspace: Mapping[str, Any], receipts: list[dict[str, Any]], *,
    scope: Mapping[str, Any], case_paths: list[str], system_test_calls: Any, note: Any,
) -> list[str]:
    checks = final_verification_checks(receipts)
    errors = []
    # A newer failure of the same execution supersedes its earlier green run.
    latest = {}
    for item in checks:
        args = item.get("args") or {}
        if item.get("tool_name") == "op_exec_shell" and args.get("target", 0) == 0:
            key = (args.get("cmd"), args.get("cwd") or workspace.get("repo_path"), args.get("stdin"))
            latest[key] = item
    if workspace.get("repair_suite_required"):
        command = suite_command(scope, case_paths)
        if not command:
            errors.append("Add a discoverable verifier test or a run-tests.sh that exercises the complete verifier corpus.")
        elif not latest.get((command, workspace.get("repo_path"), None), {}).get("ok"):
            errors.append(f"Run the complete verifier suite successfully from the repository root: {command}")
    elif workspace.get("domain") != "general" and (not any(path != f"review_scratch/{VERIFICATION_REPORT_FILENAME}" for path in case_paths) or not any(
        item.get("ok") for item in latest.values()
    )):
        errors.append("PASS requires a durable verifier case and a fresh successful command.")
    if workspace.get("verification_verdict_scope") == "sink" and not system_test_calls:
        errors.append("Exercise the delivered entrypoint and cite its successful call ID in system_test_calls.")
    if not isinstance(system_test_calls, list) or any(not isinstance(i, str) or not i for i in system_test_calls):
        errors.append("system_test_calls must contain execution call IDs")
    else:
        for call_id in system_test_calls:
            matches = [item for item in receipts if item.get("call_id") == call_id]
            if len(matches) != 1 or matches[0] not in latest.values() or not matches[0].get("ok"):
                errors.append(f"system test {call_id!r} needs a fresh successful execution on this Candidate.")
    diagnostics = {}
    for item in checks:
        if item.get("tool_name") == "op_lsp_diagnostics":
            args = {key: val for key, val in (item.get("args") or {}).items() if key != "timeout_ms"}
            diagnostics[json.dumps(args, sort_keys=True)] = item
    if any(not item.get("ok") for item in diagnostics.values()) and not (isinstance(note, str) and note.strip()):
        errors.append("Explain the failed or unavailable LSP diagnostic in note.")
    return errors


def report_evidence_errors(
    report: Mapping[str, Any], receipts: list[dict[str, Any]], workspace: Mapping[str, Any],
) -> list[str]:
    """Bind each claimed PASS to a fresh run, retaining the latest outcome per check."""
    latest = {}
    last_write = -1
    for index, receipt in enumerate(receipts):
        if receipt.get("kind") == "test_write":
            last_write = index
        if receipt.get("kind") not in {"command", "lsp"} or receipt.get("stale"):
            continue
        tool = receipt.get("tool_name")
        if receipt.get("kind") == "lsp" and tool != "op_lsp_diagnostics":
            continue
        args = dict(receipt.get("args") or {})
        args.pop("timeout_ms", None)
        key = (tool, json.dumps(args, sort_keys=True))
        latest[key] = (index, receipt)

    errors = []
    successful = set()
    diagnostics = set()
    failed_lsp = False
    for index, receipt in latest.values():
        command = str((receipt.get("args") or {}).get("cmd") or "").strip()
        call_id = str(receipt.get("call_id") or "")
        if not receipt.get("ok"):
            if receipt.get("kind") == "lsp":
                failed_lsp = True
                continue
            errors.append(f"unresolved failed verification check: {command or call_id}; rerun this check successfully")
            continue
        if index <= last_write:
            continue
        if receipt.get("kind") == "lsp":
            diagnostics.add(call_id)
        else:
            successful.update(value for value in (command, call_id) if value)

    areas = report.get("focus_areas")
    if not isinstance(areas, Mapping):
        areas = {}
    lsp = areas.get("lsp")
    if failed_lsp and not (isinstance(lsp, Mapping) and str(lsp.get("notes") or "").strip()):
        errors.append("explain the failed LSP check in focus_areas.lsp.notes")
    required = workspace.get("verification_report_focus_areas") or ("regressions", "candidate_delta", "adversarial_checks")
    for name in required:
        entry = areas.get(name)
        if not isinstance(entry, Mapping):
            errors.append(f"verification_report.yaml is missing required focus area {name}")
        elif name not in {"adversarial_checks", "lsp"} and str(entry.get("status") or "").strip().lower() != "pass":
            errors.append(f"required focus area {name} needs passing execution evidence")
    for name, entry in areas.items():
        if not isinstance(entry, Mapping) or str(entry.get("status") or "").strip().lower() != "pass":
            continue
        commands = entry.get("commands")
        passing_receipts = diagnostics if name == "lsp" else successful
        if not isinstance(commands, list) or not commands or any(
            not isinstance(command, str) or command.strip() not in passing_receipts for command in commands
        ):
            if name == "lsp":
                errors.append("focus area lsp must cite a fresh passing LSP diagnostics call")
            else:
                errors.append(f"focus area {name} must cite exact commands or call IDs with fresh passing receipts")
    return errors


def record_verification_execution(
    workspace: Mapping[str, Any], call: Any, result: Any,
    before: Mapping[str, Any],
) -> None:
    """Record the delegated execution, never a claimed receipt in its output."""
    from pal.bunshin.review_receipts import _review_tool_evidence_ref

    receipt = _review_tool_evidence_ref(call.name, call, result, workspace)
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
    from pal.bunshin.verification_corpus import verification_corpus_snapshot as snapshot
    return snapshot({**dict(workspace), "verification_case_revision": verification_case_revision(workspace)})
