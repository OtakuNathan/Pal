"""Verifier draft policy and freshness validation shared by both submit formats."""
from __future__ import annotations
from typing import Any, Mapping
from pal.bunshin.draft_values import recorded_cases, partition_findings
from pal.bunshin.work_items import findings_from_work_items
from pal.bunshin.submission_preflight import bound_reference_payload
from pal.bunshin.verification_readiness import verification_case_errors
from pal.bunshin.verification_lsp_policy import lsp_policy_errors
from pal.bunshin.verification import historical_repair_checklist_items, validate_verification_case_order


def _verification_submission_errors(
    value: Mapping[str, Any], workspace: Mapping[str, Any]
) -> tuple[list[str], tuple[str, ...]]:
    errors: list[str] = []
    reference_warnings: tuple[str, ...] = ()
    work_view = bound_reference_payload(workspace, "module_work_view", required=False)
    if work_view:
        reference_warnings = ()
    historical = list(work_view.get("historical_repair_bills") or []) or list(
        work_view.get("historical_repair_bill_refs") or []
    )
    required_historical = historical_repair_checklist_items(work_view)
    recorded_results = [dict(item) for item in list(value.get("recorded_results") or [])]
    errors.extend(verification_case_errors(recorded_results, outcome="unknown", workspace=workspace))
    try:
        validate_verification_case_order(
            [str(item.get("case_kind") or "") for item in recorded_results],
            historical_required=bool(historical),
        )
    except ValueError as exc:
        errors.append(str(exc))
    if required_historical:
        historical_status = {
            str(item.get("name") or ""): str(item.get("status") or "")
            for item in recorded_results
            if str(item.get("case_kind") or "") == "historical_regression"
        }
        missing = [
            str(item["case"])
            for item in required_historical
            if str(item["case"]) not in historical_status
        ]
        if missing:
            errors.append(
                "verification must replay every historical RepairBill case before submit: "
                + ", ".join(missing)
            )
    policy = bound_reference_payload(workspace, "verification_policy", required=False)
    if not policy:
        return errors, reference_warnings
    tags = {str(tag) for item in list(value.get("recorded_results") or []) for tag in list(dict(item).get("obligation_tags") or [])}
    exceptions = dict(value.get("policy_exceptions") or {})
    obligations = (
        ("require_focused_tests", "focused_tests"),
        ("require_warning_clean", "warning_clean"),
        ("require_consumer_probe", "consumer_probe"),
        ("require_public_surface_dogfood", "public_surface_dogfood"),
        ("require_platform_probe", "platform_probe"),
        ("require_candidate_delta_review", "candidate_delta_review"),
    )
    for policy_key, tag in obligations:
        if bool(policy.get(policy_key, False)) and tag not in tags and not str(exceptions.get(tag) or "").strip():
            errors.append(f"VerificationPolicy requires {tag} evidence or an explicit UNKNOWN reason")
    if bool(policy.get("require_historical_regressions", False)) and historical and "historical_regressions" not in tags:
        errors.append("VerificationPolicy requires historical RepairBill regression evidence")
    errors.extend(lsp_policy_errors(policy, recorded_results, exceptions))
    allowed_obligations = {
        str(item) for item in list(policy.get("allowed_obligations") or []) if str(item)
    }
    unexpected = tags - allowed_obligations if allowed_obligations else set()
    if unexpected:
        errors.append(
            "verification submission contains obligations outside this node's scope: "
            + ", ".join(sorted(unexpected))
        )
    failed_cases = [
        str(item.get("name") or "")
        for item in list(value.get("recorded_results") or [])
        if str(item.get("status") or "") == "FAIL"
    ]
    if failed_cases and not list(value.get("findings") or []):
        errors.append(
            "FAIL evidence requires at least one blocking update_finding call; "
            "advisory findings do not reconcile FAIL: "
            + ", ".join(sorted(failed_cases))
        )
    return errors, reference_warnings


def semantic_verification_draft_errors(
    payload: Mapping[str, Any],
    workspace: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return policy errors for the current assignment-local verifier Draft."""

    cases = recorded_cases(payload)
    findings, _advisories = partition_findings(
        findings_from_work_items(workspace)
    )
    errors, _warnings = _verification_submission_errors(
        {
            "recorded_results": cases,
            "findings": findings,
            "policy_exceptions": _policy_exceptions(cases),
        },
        workspace,
    )
    return tuple(dict.fromkeys(errors))


def _policy_exceptions(cases: list[Mapping[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in cases:
        if str(item.get("status") or "") != "UNKNOWN":
            continue
        for tag in list(item.get("obligation_tags") or []):
            result[str(tag)] = str(item.get("summary") or "UNKNOWN")
    return result
