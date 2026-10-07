from __future__ import annotations
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.artifacts import ArtifactRef
from pal.bunshin.contracts import AggregateSnapshot
from pal.bunshin.skeleton import SkeletonReviewFinding, SkeletonReviewResult
from pal.bunshin.review_findings import structured_advisories, structured_findings
from pal.bunshin.verification import VerificationCaseKind, VerificationCaseSpec, VerificationStatus
from pal.bunshin.semantic_orchestration.verification_receipts import _semantic_locations
from pal.bunshin.semantic_orchestration.verification_receipts import _semantic_requirement_refs


def _parse_architecture_review(payload: Mapping[str, Any]) -> SkeletonReviewResult:
    verdict = str(payload.get("verdict") or "").strip().upper()
    if verdict not in {"PASS", "FAIL"}:
        raise ValueError("architecture review verdict must be PASS or FAIL")
    structured_advisories(payload)
    findings = tuple(
        SkeletonReviewFinding(
            finding_key=str(
                item.get("finding_id")
                or item.get("finding_key")
                or ""
            ),
            finding_kind=str(item["finding_kind"]),
            priority=str(item["priority"]),
            summary=str(item["summary"]),
            locations=tuple(dict(location) for location in list(item.get("locations") or [])),
        )
        for item in structured_findings(payload)
    )
    if verdict == "PASS" and findings:
        raise ValueError("PASS architecture review cannot contain findings")
    if verdict == "FAIL" and not findings:
        raise ValueError("FAIL architecture review requires typed findings")
    return SkeletonReviewResult(verdict=verdict, findings=findings)


def _parse_skeleton_review(payload: Mapping[str, Any]) -> SkeletonReviewResult:
    verdict = str(payload.get("verdict") or "").strip().upper()
    if verdict not in {"PASS", "FAIL"}:
        raise ValueError("architecture skeleton review verdict must be PASS or FAIL")
    structured_advisories(payload)
    raw_findings = structured_findings(payload)
    findings = tuple(
        SkeletonReviewFinding(
            finding_key=str(
                item.get("finding_id")
                or item.get("finding_key")
                or ""
            ),
            finding_kind=str(item["finding_kind"]),
            priority=str(item["priority"]),
            summary=str(item["summary"]),
            locations=tuple(dict(location) for location in list(item.get("locations") or [])),
        )
        for item in raw_findings
    )
    if verdict == "PASS" and findings:
        raise ValueError("PASS architecture review cannot contain findings")
    if verdict == "FAIL" and not findings:
        raise ValueError("FAIL architecture review requires findings")
    return SkeletonReviewResult(verdict=verdict, findings=findings)


def _ref_from_mapping(value: Any) -> ArtifactRef:
    if not isinstance(value, Mapping):
        raise ValueError("artifact ref is required")
    return ArtifactRef.from_mapping(value)


def _bind_architecture_edit_instruction_for_review(
    references: dict[str, ArtifactRef],
    revision: AggregateSnapshot,
) -> bool:
    """Bind a human architecture-edit repair bill to every Reviewer attempt."""

    value = revision.payload.get("edit_instruction_ref")
    if not value:
        return False
    references["edit_instruction"] = _ref_from_mapping(value)
    return True


def _path_pseudo_ref(path: str, name: str) -> ArtifactRef:
    import hashlib

    digest = hashlib.sha256(str(Path(path).expanduser().resolve()).encode("utf-8")).hexdigest()
    return ArtifactRef(
        sha256=digest,
        artifact_type="LocalPathReference",
        schema_version="1",
        media_type=str(Path(path).expanduser().resolve()),
        byte_size=0,
        durable=True,
    )


def _append_ref(existing: Any, value: Any) -> list[dict[str, Any]]:
    result = [dict(item) for item in list(existing or []) if isinstance(item, Mapping)]
    if isinstance(value, Mapping) and value.get("sha256"):
        digest = str(value.get("sha256"))
        if all(str(item.get("sha256") or "") != digest for item in result):
            result.append(dict(value))
    return result


def _verification_case_specs(value: Any) -> list[VerificationCaseSpec]:
    cases = [_verification_case_spec(item) for item in list(value or [])]
    names = [item.case_name for item in cases]
    if len(set(names)) != len(names):
        raise ValueError("verification case names must be unique semantic names")
    return cases


def _verification_case_spec(value: Any) -> VerificationCaseSpec:
    if not isinstance(value, Mapping):
        raise ValueError("verification case must be an object")
    name = str(value.get("name") or "").strip()
    if not name:
        raise ValueError("verification case requires a semantic name")
    command = tuple(str(item) for item in list(value.get("command") or []) if str(item))
    if not command:
        raise ValueError(f"verification case {name!r} requires a command argv")
    requirements = _semantic_requirement_refs(value.get("requirements"), owner=f"case {name!r}")
    locations = _semantic_locations(value.get("locations"), owner=f"case {name!r}")
    invariants = tuple(str(item).strip() for item in list(value.get("invariants") or []) if str(item).strip())
    if not (requirements or locations or invariants):
        raise ValueError(
            f"verification case {name!r} requires Requirement text, a source location, or an invariant"
        )
    case_kind = VerificationCaseKind(str(value.get("case_kind") or ""))
    expected_exit_codes = tuple(int(item) for item in list(value.get("expected_exit_codes") or [0]))
    case_key = hashlib.sha256(
        json.dumps(
            {
                "name": name,
                "case_kind": case_kind.value,
                "command": command,
                "expected_exit_codes": expected_exit_codes,
                "requirements": requirements,
                "locations": locations,
                "invariants": invariants,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return VerificationCaseSpec(
        case_id=f"case_{case_key[:20]}",
        case_name=name,
        case_kind=case_kind,
        command=command,
        expected_exit_codes=expected_exit_codes,
        requirements=requirements,
        locations=locations,
        invariants=invariants,
        description=str(value.get("description") or ""),
    )


def _compile_standalone_review_markdown(report: Mapping[str, Any]) -> str:
    """Render the semantic review result without leaking Manager-owned identities."""

    status = str(report.get("status") or VerificationStatus.UNKNOWN)
    lines = ["# Standalone Review", "", f"**Status:** {status}"]
    summary = str(report.get("reviewer_summary") or "").strip()
    if summary:
        lines.extend(("", summary))

    findings = [dict(item or {}) for item in list(report.get("findings") or [])]
    lines.extend(("", "## Findings"))
    if not findings:
        lines.append("- No findings.")
    for index, finding in enumerate(findings, start=1):
        severity = str(finding.get("priority") or "p1").upper()
        section = str(finding.get("finding_kind") or "finding")
        finding_summary = str(finding.get("summary") or "Finding").strip()
        lines.extend(("", f"### {index}. [{severity}] {finding_summary}", f"- Area: {section}"))
        lines.append(f"- Key: {str(finding.get('finding_key') or '')}")
        for location in list(finding.get("locations") or []):
            item = dict(location or {})
            label = str(item.get("file") or "") + f":{int(item.get('line') or 1)}"
            if item.get("symbol"):
                label += f"::{str(item['symbol'])}"
            lines.append(f"- Location: {label}")

    advisories = [
        dict(item or {}) for item in list(report.get("advisories") or [])
    ]
    if advisories:
        lines.extend(("", "## Optional Advisories"))
        for advisory in advisories:
            summary_text = str(advisory.get("summary") or "Advisory").strip()
            lines.append(f"- {summary_text}")

    cases = [dict(item or {}) for item in list(report.get("cases") or [])]
    lines.extend(("", "## Verification Cases"))
    if not cases:
        lines.append("- No executable cases were required.")
    for case in cases:
        name = str(case.get("name") or "unnamed case")
        case_status = str(case.get("status") or VerificationStatus.UNKNOWN)
        command = json.dumps(list(case.get("command") or []), ensure_ascii=False)
        lines.append(f"- **{name}**: {case_status}; command `{command}`")

    for heading, key in (
        ("Test Gaps", "test_gaps"),
        ("Unreviewed Surfaces", "unreviewed_surfaces"),
        ("Residual Risk", "residual_risk"),
    ):
        values = [str(item) for item in list(report.get(key) or []) if str(item).strip()]
        if not values:
            continue
        lines.extend(("", f"## {heading}", *(f"- {item}" for item in values)))
    return "\n".join(lines).strip() + "\n"
