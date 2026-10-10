from __future__ import annotations
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.artifacts import ArtifactRef
from pal.bunshin.contracts import AggregateSnapshot
from pal.bunshin.skeleton import SkeletonReviewFinding, SkeletonReviewResult
from pal.bunshin.review_findings import structured_advisories, structured_findings
from pal.bunshin.verification import VerificationStatus


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
