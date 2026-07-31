from __future__ import annotations

from typing import Any, Mapping

from pal.minion.v2.work_items import (
    ADD_FINDING_CAPABILITY,
    ADD_FINDING_EXAMPLES,
    ADD_FINDING_TOOL_SPEC,
    FINDING_DISPOSITIONS,
    FINDING_KINDS,
    FINDING_PRIORITIES,
    PRIORITY_TO_SEVERITY,
    add_finding_tool_result,
    findings_from_work_items,
    normalize_finding,
)


def is_review_finding_capability(name: str) -> bool:
    return str(name or "") == ADD_FINDING_CAPABILITY


def review_finding_draft_kind(workspace: Mapping[str, Any]) -> str:
    del workspace
    return "work_items"


def partition_findings(
    findings: list[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blocking: list[dict[str, Any]] = []
    advisories: list[dict[str, Any]] = []
    for raw in findings:
        source = dict(raw)
        finding_id = str(source.get("finding_id") or "").strip()
        item = {
            **normalize_finding(_without_manager_identity(source)),
            **({"finding_id": finding_id} if finding_id else {}),
        }
        if item["disposition"] == "advisory":
            advisories.append(item)
        else:
            blocking.append(item)
    return blocking, advisories


def structured_advisories(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = list(payload.get("advisories") or [])
    blocking, advisories = partition_findings(
        structured_findings({"findings": raw})
    )
    if blocking:
        raise ValueError("compiled advisories must use disposition=advisory")
    return advisories


def structured_findings(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in list(payload.get("findings") or []):
        source = dict(raw or {})
        finding_id = str(source.get("finding_id") or "").strip()
        finding = normalize_finding(_without_manager_identity(source))
        identity = finding_id or _semantic_identity(finding)
        if identity in seen:
            raise ValueError(f"duplicate finding in review artifact: {identity}")
        seen.add(identity)
        values.append(
            {
                **finding,
                **({"finding_id": finding_id} if finding_id else {}),
            }
        )
    return sorted(
        values,
        key=lambda item: (
            FINDING_PRIORITIES.index(str(item["priority"])),
            str(item.get("finding_id") or item["summary"]),
        ),
    )


def empty_review_draft() -> dict[str, Any]:
    return {
        "definitions": {},
        "evidence": {"cases": {}},
        "summary": {},
    }


def finding_severity(finding: Mapping[str, Any]) -> str:
    return PRIORITY_TO_SEVERITY[str(finding.get("priority") or "p1")]


def _without_manager_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result.pop("finding_id", None)
    return result


def _semantic_identity(value: Mapping[str, Any]) -> str:
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "ADD_FINDING_CAPABILITY",
    "ADD_FINDING_EXAMPLES",
    "ADD_FINDING_TOOL_SPEC",
    "FINDING_DISPOSITIONS",
    "FINDING_KINDS",
    "FINDING_PRIORITIES",
    "PRIORITY_TO_SEVERITY",
    "add_finding_tool_result",
    "empty_review_draft",
    "finding_severity",
    "findings_from_work_items",
    "is_review_finding_capability",
    "normalize_finding",
    "partition_findings",
    "review_finding_draft_kind",
    "structured_advisories",
    "structured_findings",
]
