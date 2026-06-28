from __future__ import annotations

from typing import Any

from pal.minion.repository import MinionTaskingRepository
from pal.minion.validation import MinionWorkOrderValidationError, normalize_milestones


def validate_draft_work_order_args(args: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(args)
    milestones = normalize_milestones(normalized.get("milestones"))
    normalized["milestones"] = milestones
    metadata = dict(normalized.get("metadata") or {})
    metadata["milestones"] = milestones
    normalized["metadata"] = metadata
    return normalized


def validate_promote_work_order_args(
    repository: MinionTaskingRepository,
    draft_id: str,
    *,
    reviewed_candidate: dict[str, Any] | None = None,
) -> None:
    snapshot = repository.read_work_order_draft(str(draft_id or ""))
    if snapshot.get("status") == "not_found":
        raise MinionWorkOrderValidationError(f"unknown work order draft: {draft_id}", field="draft_id")
    base_candidate = dict(snapshot.get("work_order_candidate") or {})
    raw_milestones = (
        dict(reviewed_candidate or {}).get("milestones")
        if isinstance(reviewed_candidate, dict) and "milestones" in reviewed_candidate
        else base_candidate.get("milestones")
    )
    normalize_milestones(raw_milestones)
    if reviewed_candidate is not None and "milestones" in reviewed_candidate:
        reviewed_candidate["milestones"] = normalize_milestones(reviewed_candidate.get("milestones"))


def normalize_top_level_review_gate_args(args: dict[str, Any], *, repository: MinionTaskingRepository) -> dict[str, Any]:
    verdict = str(args.get("verdict") or "").strip().lower()
    if verdict != "pass":
        return args
    metadata = dict(args.get("metadata") or {}) if isinstance(args.get("metadata"), dict) else {}
    if metadata.get("tool_evidence_refs"):
        raise MinionWorkOrderValidationError(
            "top-level op_minion_review_gate_submit cannot claim runner tool_evidence_refs; use the minion runner gate path",
            field="metadata.tool_evidence_refs",
        )
    human_override = metadata.get("human_override")
    if isinstance(human_override, dict):
        raise MinionWorkOrderValidationError(
            "top-level op_minion_review_gate_submit does not accept human_override; use a control/UI action",
            field="metadata.human_override",
        )
    external_ref = metadata.get("external_verification_ref") or metadata.get("external_evidence_ref")
    if has_external_verification_ref(external_ref):
        try:
            metadata["external_verification_ref"] = repository.validate_external_verification_ref(external_ref)
        except ValueError as exc:
            raise MinionWorkOrderValidationError(str(exc), field="metadata.external_verification_ref") from exc
        args["metadata"] = metadata
        return args
    external_refs = metadata.get("external_verification_refs")
    if isinstance(external_refs, list):
        normalized_refs: list[dict[str, Any]] = []
        for item in external_refs:
            if not has_external_verification_ref(item):
                continue
            try:
                normalized_refs.append(repository.validate_external_verification_ref(item))
            except ValueError as exc:
                raise MinionWorkOrderValidationError(str(exc), field="metadata.external_verification_refs") from exc
        if normalized_refs:
            metadata["external_verification_refs"] = normalized_refs
            args["metadata"] = metadata
            return args
    raise MinionWorkOrderValidationError(
        "top-level pass review gates require a valid metadata.external_verification_ref",
        field="metadata",
    )


def has_external_verification_ref(value: Any) -> bool:
    if isinstance(value, dict):
        for key in ("ref", "id", "uri", "url", "path", "artifact_ref", "summary"):
            if str(value.get(key) or "").strip():
                return True
    if isinstance(value, str):
        return bool(value.strip())
    return False
