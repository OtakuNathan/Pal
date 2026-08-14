from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from pal.bunshin.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.v2.contracts import AggregateSnapshot


ARCHITECTURE_FINDING_BATCH_ARTIFACT = "ArchitectureFindingBatchArtifact"
ARCHITECTURE_FINDING_BATCH_VIEW_ARTIFACT = "ArchitectureFindingBatchSemanticViewArtifact"
_ARCHITECTURE_DEFECT_ACTIONS = frozenset(
    {"CONTRACT_DEFECT", "ARCHITECTURE_DEFECT", "PRODUCER_ARCHITECTURE_DEFECT"}
)


@dataclass(frozen=True)
class ArchitectureFindingBatch:
    artifact_ref: ArtifactRef
    finding_fingerprints: tuple[str, ...]


def architecture_revision_finding_value(payload: Mapping[str, Any]) -> Any:
    return (
        payload.get("finding_artifact_ref")
        or payload.get("replan_finding_batch_ref")
        or payload.get("replan_finding_ref")
    )


def collect_architecture_finding_batch(
    artifacts: ContentAddressedArtifactStore,
    *,
    epoch: AggregateSnapshot,
    nodes: Sequence[AggregateSnapshot],
) -> ArchitectureFindingBatch:
    sources: dict[str, dict[str, Any]] = {}

    def add_source(
        finding_value: Any,
        *,
        source_node: str = "",
    ) -> None:
        if not isinstance(finding_value, Mapping) or not finding_value.get("sha256"):
            return
        finding_ref = dict(finding_value)
        digest = str(finding_ref["sha256"])
        current = sources.setdefault(
            digest,
            {
                "finding_artifact_ref": finding_ref,
                "source_nodes": set(),
            },
        )
        if source_node:
            current["source_nodes"].add(source_node)

    for pending in list(epoch.payload.get("pending_replan_findings") or []):
        item = dict(pending or {})
        add_source(
            item.get("finding_artifact_ref"),
            source_node=str(item.get("source_node") or ""),
        )
    for node in nodes:
        if str(node.payload.get("last_action_type") or "") not in _ARCHITECTURE_DEFECT_ACTIONS:
            continue
        add_source(
            node.payload.get("repair_bill_ref") or node.payload.get("finding_artifact_ref"),
            source_node=str(node.payload.get("module_name") or node.payload.get("unit_id") or ""),
        )
    if not sources:
        raise ValueError("replan collection has no persisted architecture findings")

    grouped: dict[str, dict[str, Any]] = {}
    child_refs: list[tuple[str, str]] = []
    for source_digest in sorted(sources):
        source = sources[source_digest]
        finding_ref = ArtifactRef.from_mapping(source["finding_artifact_ref"])
        finding_payload = dict(artifacts.read_json(finding_ref))
        normalized_findings = _normalized_findings(finding_payload)
        if not normalized_findings:
            continue
        fingerprint = _finding_fingerprint(finding_payload, source_digest)
        group = grouped.setdefault(
            fingerprint,
            {
                "finding_fingerprint": fingerprint,
                "defect_kind": _finding_kind(finding_payload),
                "repair_bill_refs": [],
                "source_nodes": set(),
                "findings": [],
            },
        )
        group["repair_bill_refs"].append(finding_ref.to_dict())
        group["source_nodes"].update(source["source_nodes"])
        group["findings"].extend(normalized_findings)
        child_refs.append((finding_ref.sha256, "architecture_finding"))

    if not grouped:
        raise ValueError("replan collection has no actionable FAIL findings")

    finding_groups: list[dict[str, Any]] = []
    flattened_findings: list[dict[str, Any]] = []
    for fingerprint in sorted(grouped):
        group = grouped[fingerprint]
        findings = _dedupe_semantic_findings(group["findings"])
        finding_groups.append(
            {
                "finding_fingerprint": fingerprint,
                "defect_kind": str(group["defect_kind"] or "architecture_defect"),
                "repair_bill_refs": sorted(
                    group["repair_bill_refs"], key=lambda item: str(item.get("sha256") or "")
                ),
                "source_nodes": sorted(group["source_nodes"]),
                "findings": findings,
            }
        )
        flattened_findings.extend(findings)
    payload = {
        "schema_version": "1",
        "source_execution_epoch_id": epoch.aggregate_id,
        "base_architecture_manifest_ref": dict(epoch.payload.get("architecture_manifest_ref") or {}),
        "finding_groups": finding_groups,
        "findings": flattened_findings,
    }
    artifact_ref = artifacts.put_json(
        payload,
        artifact_type=ARCHITECTURE_FINDING_BATCH_ARTIFACT,
        provenance={"owner": "bunshin-v2-manager", "source": "execution-replan"},
        child_refs=tuple(child_refs),
    )
    return ArchitectureFindingBatch(
        artifact_ref=artifact_ref,
        finding_fingerprints=tuple(sorted(grouped)),
    )


def architecture_finding_semantic_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    findings = _dedupe_semantic_findings(_normalized_findings(payload))
    return {
        "findings": findings,
        "finding_count": len(findings),
        "instruction": "Resolve every listed finding against the unchanged Requirements and accepted architecture baseline.",
    }


def compile_architecture_finding_markdown(payload: Mapping[str, Any]) -> str:
    view = architecture_finding_semantic_view(payload)
    lines = ["## Replan Findings", ""]
    for finding in list(view.get("findings") or []):
        item = dict(finding or {})
        module = str(item.get("module_name") or "")
        affected = ", ".join(str(value) for value in list(item.get("affected_modules") or []))
        scope = module or affected or "cross-module"
        lines.append(
            f"- **{str(item.get('finding_kind') or 'architecture_defect')} / "
            f"{str(item.get('severity') or 'error')} / {scope}**: "
            f"{str(item.get('summary') or 'Architecture finding')}"
        )
        locations = [
            str(
                dict(location or {}).get("path")
                or dict(location or {}).get("file")
                or ""
            )
            for location in list(item.get("locations") or [])
            if str(
                dict(location or {}).get("path")
                or dict(location or {}).get("file")
                or ""
            )
        ]
        if locations:
            lines.append(f"  - Locations: {', '.join(sorted(set(locations)))}")
    return "\n".join(lines).rstrip() + "\n"


def _normalized_findings(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    nested = [dict(item or {}) for item in list(payload.get("findings") or [])]
    had_nested_findings = bool(nested)
    case_statuses = _repair_bill_case_statuses(payload)
    if "FAIL" in set(case_statuses.values()):
        nested = [
            item
            for item in nested
            if case_statuses.get(str(item.get("case_name") or "")) != "UNKNOWN"
        ]
    source = nested if had_nested_findings else [dict(payload)]
    defaults = {
        "finding_kind": _finding_kind(payload),
        "summary": str(
            payload.get("finding_summary")
            or payload.get("summary")
            or payload.get("failure_reason")
            or payload.get("reason")
            or "Architecture finding"
        ),
        "severity": str(payload.get("severity") or "error"),
        "module_name": str(payload.get("module_name") or ""),
        "affected_modules": list(payload.get("affected_modules") or []),
        "requirements": list(payload.get("requirements") or []),
        "locations": list(payload.get("locations") or []),
        "suggested_repair_boundary": list(payload.get("suggested_repair_boundary") or []),
        "revision_targets": list(payload.get("revision_targets") or []),
        "expected": payload.get("expected"),
        "actual": payload.get("actual"),
    }
    normalized: list[dict[str, Any]] = []
    for raw in source:
        item: dict[str, Any] = {}
        for key, default in defaults.items():
            value = raw.get(key)
            item[key] = default if value in (None, "", [], {}) else value
        item["finding_kind"] = str(
            raw.get("finding_kind") or raw.get("defect_kind") or defaults["finding_kind"]
        )
        item["summary"] = str(
            raw.get("summary")
            or raw.get("finding_summary")
            or raw.get("failure_reason")
            or defaults["summary"]
        )
        raw_key = str(raw.get("finding_key") or "").strip()
        item["finding_key"] = (
            raw_key
            if re.fullmatch(r"[a-z][a-z0-9_]{2,95}", raw_key)
            else _generated_finding_key(item)
        )
        item["priority"] = _normalized_finding_priority(raw)
        item["disposition"] = str(
            raw.get("disposition") or "blocking"
        ).strip()
        normalized.append(item)
    return normalized


def _generated_finding_key(finding: Mapping[str, Any]) -> str:
    """Give Manager-originated findings the same stable semantic handle as role findings."""

    digest = hashlib.sha256(
        json.dumps(
            dict(finding),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"finding_{digest}"


def _normalized_finding_priority(finding: Mapping[str, Any]) -> str:
    priority = str(finding.get("priority") or "").strip().lower()
    if priority in {"p0", "p1", "p2"}:
        return priority
    severity = str(finding.get("severity") or "").strip().lower()
    if severity in {"blocker", "critical", "fatal"}:
        return "p0"
    if severity in {"minor", "low"}:
        return "p2"
    return "p1"


def _repair_bill_case_statuses(payload: Mapping[str, Any]) -> dict[str, str]:
    actual = payload.get("actual")
    if not isinstance(actual, Mapping):
        return {}
    return {
        str(item.get("name") or ""): str(item.get("status") or "")
        for raw in list(actual.get("cases") or [])
        if isinstance(raw, Mapping)
        for item in (dict(raw),)
        if str(item.get("name") or "") and str(item.get("status") or "")
    }


def _dedupe_semantic_findings(findings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for raw in findings:
        item = dict(raw)
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        unique.setdefault(key, item)
    return [unique[key] for key in sorted(unique)]


def _finding_kind(payload: Mapping[str, Any]) -> str:
    return str(payload.get("finding_kind") or payload.get("defect_kind") or "architecture_defect")


def _finding_fingerprint(payload: Mapping[str, Any], source_digest: str) -> str:
    explicit = str(payload.get("finding_fingerprint") or "").strip()
    if explicit:
        return explicit
    semantic = architecture_finding_semantic_view({"findings": _normalized_findings(payload)})
    encoded = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded or source_digest.encode("utf-8")).hexdigest()
