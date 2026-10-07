"""Pure verifier draft values and projections, independent of stores and tools."""
from __future__ import annotations
import hashlib
import json
from pathlib import PurePosixPath
from typing import Any, Literal, Mapping
from pydantic import Field
from pal.execution.tool_facade import StrictToolModel


class BunshinReviewFindingLocation(StrictToolModel):
    scope: Literal["task_ledger", "workspace"]
    file: str = Field(min_length=1)
    line: int = Field(ge=1)
    symbol: str | None = Field(default=None, min_length=1)


class BunshinAddFindingInput(StrictToolModel):
    finding_kind: Literal[
        "requirements_defect",
        "module_defect",
        "dependency_defect",
        "contract_defect",
        "architecture_defect",
        "sink_defect",
        "verification_defect",
    ]
    priority: Literal["p0", "p1", "p2"]
    disposition: Literal["blocking", "advisory"] = "blocking"
    summary: str = Field(min_length=1, max_length=4000)
    locations: list[BunshinReviewFindingLocation] | None = Field(
        default=None,
        max_length=8,
    )


class BunshinUpdateFindingInput(BunshinAddFindingInput):
    finding_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=0)


class BunshinRemoveFindingInput(StrictToolModel):
    finding_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)


def submission_work_items(value: Any) -> list[dict[str, str]]:
    """Project the Manager ledger into role-handoff checklist semantics.

    Ledger identities and ordering metadata stay inside the Manager-owned
    WorkItem store.  Role submissions only need the meaning and completion
    state of each item.
    """

    return [
        {
            "kind": str(item.get("kind") or "task"),
            "status": str(item.get("status") or ""),
            "summary": str(item.get("summary") or ""),
        }
        for raw in list(value or [])
        if isinstance(raw, Mapping)
        for item in (dict(raw),)
    ]


def normalize_finding(value: Mapping[str, Any]) -> dict[str, Any]:
    validated = BunshinAddFindingInput.model_validate(value, strict=True)
    finding = validated.model_dump(mode="python", exclude_none=True)
    if finding["disposition"] == "advisory" and finding["priority"] != "p2":
        raise ValueError("advisory findings must use priority p2")
    locations: list[dict[str, Any]] = []
    for raw in list(finding.get("locations") or []):
        item = dict(raw)
        file_name = str(item.get("file") or "").replace("\\", "/").strip()
        path = PurePosixPath(file_name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("finding location file must be a safe relative path")
        item["file"] = str(path)
        locations.append(item)
    finding["locations"] = locations
    return finding


def _finding_hash(finding: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(finding),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


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


def _without_manager_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result.pop("finding_id", None)
    return result


def recorded_cases(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    cases = dict(dict(payload.get("evidence") or {}).get("cases") or {})
    recorded = [dict(item) for item in cases.values()]
    if recorded and all(_recorded_sequence(item) > 0 for item in recorded):
        return sorted(recorded, key=lambda item: (_recorded_sequence(item), str(item.get("name") or "")))
    return sorted(
        recorded,
        key=lambda item: (
            _CASE_KIND_ORDER.get(str(item.get("case_kind") or ""), 99),
            str(item.get("name") or ""),
        ),
    )

_CASE_KIND_ORDER = {
    "historical_regression": 0,
    "contract_adversarial": 1,
    "diff_risk": 2,
    "compile": 3,
    "lsp": 3,
    "unit": 3,
    "consumer_probe": 3,
    "platform_assumption": 4,
}


def _recorded_sequence(value: Mapping[str, Any]) -> int:
    sequence = value.get("recorded_sequence")
    return int(sequence) if type(sequence) is int and sequence > 0 else 0
