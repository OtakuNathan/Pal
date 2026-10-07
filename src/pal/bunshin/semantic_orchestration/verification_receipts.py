from __future__ import annotations
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.verification import DefectKind, VerificationCaseResult, VerificationCaseSpec, VerificationStatus
from pal.bunshin.submission_drafts import SubmissionDraftStore
from pal.bunshin.semantic_evidence import recorded_cases


def _recorded_verification_case_results(
    plan: Mapping[str, Any],
    *,
    cases: list[VerificationCaseSpec],
    artifacts: Any,
    runtime_root: Path,
    workflow_id: str,
    invocation_id: str,
    lease_resource_key: str,
    fencing_token: int,
    role: str,
    mode: str,
    draft_kind: str,
) -> list[VerificationCaseResult]:
    internal = dict(plan.get("internal_context") or {})
    evidence_invocation_id = str(internal.get("invocation_id") or "").strip()
    if not evidence_invocation_id:
        raise ValueError("recorded verification evidence has no fenced invocation")
    input_fingerprint = str(internal.get("input_fingerprint") or "").strip()
    if not input_fingerprint:
        raise ValueError("recorded verification evidence has no bound input fingerprint")
    draft_key = str(internal.get("draft_key") or "").strip()
    if not draft_key:
        raise ValueError("recorded verification evidence has no Draft binding")
    durable = SubmissionDraftStore(runtime_root).read_submitted(draft_key)
    if (
        durable.workflow_id != workflow_id
        or durable.invocation_id != evidence_invocation_id
        or durable.role != role
        or durable.mode != mode
        or durable.draft_kind != draft_kind
        or durable.input_fingerprint != input_fingerprint
        or durable.fencing_token != int(internal.get("fencing_token") or 0)
    ):
        raise ValueError("recorded verification evidence Draft binding is invalid")
    if evidence_invocation_id != invocation_id:
        repository = BunshinRepository(runtime_root)
        attempt = repository.role_attempts.read_role_attempt(evidence_invocation_id)
        assignment = (
            repository.role_assignments.read_role_assignment(str(attempt.get("assignment_id") or ""))
            if attempt is not None
            else None
        )
        if (
            attempt is None
            or assignment is None
            or str(assignment.get("session_id") or "") != invocation_id
            or str(assignment.get("workflow_id") or "") != workflow_id
            or str(assignment.get("role") or "") != role
            or str(assignment.get("input_fingerprint") or "") != input_fingerprint
            or str(attempt.get("lease_resource_key") or "")
            != durable.lease_resource_key
            or int(attempt.get("fencing_token") or 0) != durable.fencing_token
        ):
            raise ValueError(
                "recorded verification evidence is not owned by the current logical role session"
            )
    durable_plan = artifacts.read_json(durable.submission_artifact_ref)
    if json.dumps(durable_plan, ensure_ascii=False, sort_keys=True) != json.dumps(
        dict(plan), ensure_ascii=False, sort_keys=True
    ):
        raise ValueError("verification artifact does not match its durable submission receipt")
    recorded = recorded_cases(durable.payload)
    submitted = [dict(item or {}) for item in list(plan.get("recorded_results") or [])]
    if json.dumps(recorded, ensure_ascii=False, sort_keys=True) != json.dumps(
        submitted,
        ensure_ascii=False,
        sort_keys=True,
    ):
        raise ValueError("verification artifact does not match the fenced durable Draft")
    by_name = {str(item.get("name") or ""): item for item in recorded}
    if set(by_name) != {case.case_name for case in cases}:
        raise ValueError("recorded verification results do not match declared case names")
    results: list[VerificationCaseResult] = []
    for case in cases:
        item = by_name[case.case_name]
        if str(item.get("input_fingerprint") or "") != input_fingerprint:
            raise ValueError(f"case {case.case_name!r} was recorded against different immutable inputs")
        if tuple(str(value) for value in list(item.get("command") or [])) != case.command:
            raise ValueError(f"case {case.case_name!r} command differs from its recorded execution")
        status = VerificationStatus(str(item.get("status") or ""))
        exit_code = item.get("exit_code")
        if status == VerificationStatus.PASS and (
            exit_code is None or int(exit_code) not in case.expected_exit_codes
        ):
            raise ValueError(f"case {case.case_name!r} has an impossible PASS result")
        if status == VerificationStatus.FAIL and (
            exit_code is None or int(exit_code) in case.expected_exit_codes
        ):
            raise ValueError(f"case {case.case_name!r} has an impossible FAIL result")
        stdout_ref = dict(item.get("stdout_ref") or {})
        stderr_ref = dict(item.get("stderr_ref") or {})
        if status != VerificationStatus.UNKNOWN or stdout_ref:
            artifacts.read_bytes(stdout_ref)
        if status != VerificationStatus.UNKNOWN or stderr_ref:
            artifacts.read_bytes(stderr_ref)
        results.append(
            VerificationCaseResult(
                case_id=case.case_id,
                case_name=case.case_name,
                case_kind=case.case_kind,
                status=status,
                command=case.command,
                exit_code=int(exit_code) if exit_code is not None else None,
                stdout_ref=stdout_ref,
                stderr_ref=stderr_ref,
                environment=dict(item.get("environment") or {}),
                summary=str(item.get("summary") or ""),
                requirements=case.requirements,
                locations=case.locations,
                invariants=case.invariants,
            )
        )
    return results


def _verification_findings(
    plan: Mapping[str, Any],
    cases: list[VerificationCaseSpec],
) -> list[dict[str, Any]]:
    cases_by_name = {item.case_name: item for item in cases}
    findings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in list(plan.get("findings") or []):
        if not isinstance(raw, Mapping):
            raise ValueError("verification finding must be an object")
        item = dict(raw)
        case_name = str(item.get("case") or "").strip()
        section = str(item.get("finding_section") or "implementation").strip()
        if not case_name or case_name not in cases_by_name:
            raise ValueError("verification finding must reference a declared semantic case name")
        if section not in _FINDING_SECTIONS:
            raise ValueError(f"verification finding for {case_name!r} has invalid finding_section: {section}")
        summary = str(item.get("summary") or "").strip()
        failure_reason = str(item.get("failure_reason") or "").strip()
        if not summary or not failure_reason:
            raise ValueError(f"verification finding for {case_name!r} requires summary and failure_reason")
        case = cases_by_name[case_name]
        defect_kind = str(item.get("defect_kind") or "").strip()
        if defect_kind and defect_kind not in {value.value for value in DefectKind}:
            raise ValueError(
                f"verification finding for {case_name!r} has invalid defect_kind: {defect_kind}"
            )
        finding = {
            "case_id": case.case_id,
            "case_name": case_name,
            "finding_section": section,
            "summary": summary,
            "failure_reason": failure_reason,
            "requirements": list(
                _semantic_requirement_refs(item.get("requirements"), owner=f"finding for {case_name!r}")
                or case.requirements
            ),
            "locations": list(
                _semantic_locations(item.get("locations"), owner=f"finding for {case_name!r}")
                or case.locations
            ),
            "invariants": [
                str(value).strip()
                for value in list(item.get("invariants") or case.invariants)
                if str(value).strip()
            ],
            "severity": str(item.get("severity") or "major"),
            "suggested_repair_boundary": [
                str(value) for value in list(item.get("suggested_repair_boundary") or [])
            ],
            **({"defect_kind": defect_kind} if defect_kind else {}),
            **(
                {"target_module": str(item.get("target_module") or "").strip()}
                if str(item.get("target_module") or "").strip()
                else {}
            ),
        }
        if not (finding["requirements"] or finding["locations"] or finding["invariants"]):
            raise ValueError(
                f"verification finding for {case_name!r} requires Requirement text, a source location, or an invariant"
            )
        finding_key = hashlib.sha256(
            json.dumps(finding, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if finding_key in seen:
            raise ValueError(f"duplicate semantic verification finding for {case_name!r}")
        findings.append(finding)
        seen.add(finding_key)
    return findings


def _finding_for_case(findings: list[dict[str, Any]], case_id: str) -> dict[str, Any]:
    return next((item for item in findings if str(item.get("case_id") or "") == case_id), {})


def _confirmed_verification_findings(
    findings: list[dict[str, Any]],
    cases: list[VerificationCaseSpec],
    results: list[Any],
) -> list[dict[str, Any]]:
    blocking = {
        item.case_id: item
        for item in results
        if item.status in {VerificationStatus.FAIL, VerificationStatus.UNKNOWN}
    }
    confirmed = [item for item in findings if str(item.get("case_id") or "") in blocking]
    described = {str(item.get("case_id") or "") for item in confirmed}
    specs = {item.case_id: item for item in cases}
    for case_id, result in blocking.items():
        if case_id in described or result.status != VerificationStatus.FAIL:
            continue
        spec = specs[case_id]
        confirmed.append(
            {
                "case_id": case_id,
                "case_name": spec.case_name,
                "finding_section": "implementation",
                "summary": spec.description or f"Verification case {spec.case_name!r} did not pass",
                "failure_reason": result.summary,
                "requirements": [dict(item) for item in spec.requirements],
                "locations": [dict(item) for item in spec.locations],
                "invariants": list(spec.invariants),
                "severity": "major",
                "suggested_repair_boundary": [],
            }
        )
    return confirmed


def _semantic_requirement_refs(value: Any, *, owner: str) -> tuple[dict[str, str], ...]:
    result: list[dict[str, str]] = []
    for raw in list(value or []):
        item = dict(raw or {})
        section = str(item.get("section") or "").strip()
        requirement = str(item.get("requirement") or "").strip()
        if not section or not requirement:
            raise ValueError(f"{owner} Requirement references require section and original requirement text")
        result.append({"section": section, "requirement": requirement})
    return tuple(result)


def _semantic_locations(value: Any, *, owner: str) -> tuple[dict[str, str], ...]:
    result: list[dict[str, str]] = []
    for raw in list(value or []):
        item = dict(raw or {})
        path = str(item.get("path") or "").strip()
        if not path:
            raise ValueError(f"{owner} source locations require path")
        result.append(
            {
                "path": path,
                **({"symbol": str(item.get("symbol") or "").strip()} if item.get("symbol") else {}),
                **({"section": str(item.get("section") or "").strip()} if item.get("section") else {}),
            }
        )
    return tuple(result)


_FINDING_SECTIONS = frozenset(
    {
        "ownership",
        "lifecycle",
        "state_machine",
        "invariant",
        "interface",
        "compatibility",
        "delivery",
        "implementation",
    }
)
