from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from pal.minion.repository import MinionTaskingRepository
from pal.minion.validation import MinionWorkOrderValidationError, normalize_milestones
from pal.shared import TaskContextPack


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


def validate_spawn_args(args: dict[str, Any], *, repository: MinionTaskingRepository) -> None:
    for field in _LEGACY_PUBLIC_SPAWN_FIELDS:
        if field in args:
            raise MinionWorkOrderValidationError(
                f"{field} is not accepted by op_minion_spawn; pass a semantic DispatchRequest instead",
                field=field,
            )
    draft_id = str(args.get("draft_id") or "").strip()
    if draft_id:
        validate_promote_work_order_args(repository, draft_id, reviewed_candidate=dict(args.get("reviewed_candidate") or {}) or None)
    if args.get("supporting_artifacts") is not None:
        normalize_supporting_artifacts(args.get("supporting_artifacts"), repository=repository)
    if args.get("plan_ref") is not None:
        repository.load_accepted_plan_ref(args.get("plan_ref"))
    if args.get("feedback_gate_ref") is not None:
        validate_plan_revision_gate_ref(args.get("feedback_gate_ref"), repository=repository)
    if (
        not any(str(args.get(key) or "").strip() for key in ("draft_id", "work_order_id", "task_query", "query"))
        and args.get("plan_ref") is None
        and args.get("feedback_gate_ref") is None
    ):
        raise MinionWorkOrderValidationError(
            "op_minion_spawn requires plan_ref, feedback_gate_ref, draft_id, work_order_id, or task_query",
            field="dispatch_source",
        )


def normalize_top_level_review_gate_args(args: dict[str, Any], *, repository: MinionTaskingRepository) -> dict[str, Any]:
    verdict = str(args.get("verdict") or "").strip().lower()
    if verdict != "pass":
        return args
    metadata = dict(args.get("metadata") or {}) if isinstance(args.get("metadata"), dict) else {}
    if metadata.get("tool_evidence_refs"):
        raise MinionWorkOrderValidationError(
            "top-level review_gate_submit cannot claim runner tool_evidence_refs; use the minion runner gate path",
            field="metadata.tool_evidence_refs",
        )
    human_override = metadata.get("human_override")
    if isinstance(human_override, dict):
        raise MinionWorkOrderValidationError(
            "top-level review_gate_submit does not accept human_override; use a control/UI action",
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


_LEGACY_PUBLIC_SPAWN_FIELDS = frozenset(
    {
        "artifact_refs",
        "minion_profile",
        "task_context_pack",
        "task_json",
        "final_plan_artifact",
        "milestones",
        "module_id",
        "metadata",
        "prompt_view",
        "planner_work_order",
        "coder_work_order",
        "reviewer_work_order",
        "plan_artifact",
        "plan_validation",
        "module_execution",
        "plan_execution",
        "allowed_capabilities",
        "resolved_profile",
    }
)


def validate_pack_milestones(pack: TaskContextPack) -> TaskContextPack:
    metadata = dict(pack.metadata or {})
    if isinstance(metadata.get("plan_artifact"), dict):
        milestones = normalize_milestones(metadata.get("milestones"))
        metadata["milestones"] = milestones
    else:
        metadata.pop("milestones", None)
    return TaskContextPack.from_dict({**pack.to_dict(), "metadata": metadata})


def pack_from_args(args: dict[str, Any], *, repository: MinionTaskingRepository | None = None) -> TaskContextPack:
    draft_id = str(args.get("draft_id") or "").strip()
    if draft_id and repository is not None:
        promoted = repository.promote_work_order_draft(
            draft_id,
            reviewed_candidate=dict(args.get("reviewed_candidate") or {}) or None,
        )
        return apply_spawn_pack_overrides(
            TaskContextPack.from_dict(dict(promoted.get("task_context_pack") or {})),
            args,
            supporting_artifacts=[],
        )
    supporting_artifacts = (
        normalize_supporting_artifacts(args.get("supporting_artifacts"), repository=repository)
        if args.get("supporting_artifacts") is not None
        else []
    )
    feedback_gate_ref = dict(args.get("feedback_gate_ref") or {}) if isinstance(args.get("feedback_gate_ref"), dict) else {}
    if feedback_gate_ref:
        if repository is None:
            raise ValueError("repository is required to spawn from feedback_gate_ref")
        gate_ref = validate_plan_revision_gate_ref(feedback_gate_ref, repository=repository)
        metadata = dispatch_metadata_from_args(args, supporting_artifacts=supporting_artifacts)
        metadata["feedback_gate_ref"] = dict(gate_ref)
        return repository.build_planner_revision_pack_from_review_gate(
            gate_ref,
            work_order_id=str(args.get("work_order_id") or ""),
            workspace=dict(args.get("workspace") or {}),
            metadata=metadata,
            goal=str(args.get("goal") or ""),
            instruction=str(args.get("instruction") or ""),
        )
    if args.get("plan_ref") is not None:
        if repository is None:
            raise ValueError("repository is required to spawn from plan_ref")
        loaded = repository.load_accepted_plan_ref(args.get("plan_ref"))
        metadata = dispatch_metadata_from_args(args, supporting_artifacts=supporting_artifacts)
        metadata.update({"plan_ref": loaded["plan_ref"], "plan_validation": loaded["plan_validation"]})
        profile_group = str(args.get("profile_group") or "").strip()
        profile_name = str(args.get("profile_name") or "").strip()
        if profile_group:
            metadata["dispatch_profile_group"] = profile_group
        if profile_name:
            metadata["dispatch_profile_name"] = profile_name
        return repository.build_plan_parent_pack_from_plan(
            dict(loaded.get("plan_artifact") or {}),
            work_order_id=str(args.get("work_order_id") or ""),
            workspace=dict(args.get("workspace") or {}),
            metadata=metadata,
            goal=str(args.get("goal") or ""),
            instruction=str(args.get("instruction") or ""),
            profile_group=str(args.get("profile_group") or ""),
            profile_name=str(args.get("profile_name") or ""),
        )
    work_order_id = str(args.get("work_order_id") or "").strip()
    if work_order_id:
        if repository is None:
            raise ValueError("repository is required to spawn from work_order_id")
        try:
            return repository.pack_for_work_order(work_order_id, overrides=dispatch_pack_overrides(args, supporting_artifacts=supporting_artifacts))
        except KeyError:
            raise MinionWorkOrderValidationError(f"unknown work order: {work_order_id}", field="work_order_id")
    query = str(args.get("task_query") or args.get("query") or "").strip()
    if query and repository is not None:
        result = repository.search_work_orders(query, limit=5)
        candidates = list(result.get("items") or [])
        if len(candidates) == 1:
            work_order_id = str(candidates[0].get("work_order_id") or "")
            return repository.pack_for_work_order(
                work_order_id,
                overrides=dispatch_pack_overrides(args, supporting_artifacts=supporting_artifacts, metadata_extra={"resolved_from_task_query": query}),
            )
        raise MinionSpawnResolutionError(query=query, candidates=candidates)
    raise MinionWorkOrderValidationError(
        "op_minion_spawn requires plan_ref, feedback_gate_ref, draft_id, work_order_id, or task_query",
        field="dispatch_source",
    )


def dispatch_pack_overrides(
    args: dict[str, Any],
    *,
    supporting_artifacts: list[dict[str, Any]],
    metadata_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for key in ("goal", "instruction", "acceptance_criteria", "workspace", "allowed_skills", "approval_policy", "profile_group", "profile_name"):
        if key in args and args.get(key):
            overrides[key] = args[key]
    metadata = dispatch_metadata_from_args(args, supporting_artifacts=supporting_artifacts)
    metadata.update(dict(metadata_extra or {}))
    if metadata:
        overrides["metadata"] = metadata
    return overrides


def apply_spawn_pack_overrides(pack: TaskContextPack, args: dict[str, Any], *, supporting_artifacts: list[dict[str, Any]]) -> TaskContextPack:
    payload = pack.to_dict()
    overrides = dispatch_pack_overrides(args, supporting_artifacts=supporting_artifacts)
    metadata = dict(payload.get("metadata") or {})
    override_metadata = dict(overrides.pop("metadata", {}) or {})
    if override_metadata:
        metadata.update(override_metadata)
    for key, value in overrides.items():
        if value:
            payload[key] = value
    payload["metadata"] = metadata
    return TaskContextPack.from_dict(payload)


def dispatch_metadata_from_args(args: dict[str, Any], *, supporting_artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    title = str(args.get("title") or "").strip()
    if title:
        metadata["task_title"] = title
        metadata["work_order_title"] = title
    if supporting_artifacts:
        metadata["supporting_artifacts"] = [dict(item) for item in supporting_artifacts]
    return metadata


def normalize_supporting_artifacts(raw: Any, *, repository: MinionTaskingRepository | None) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise MinionWorkOrderValidationError("supporting_artifacts must be an array", field="supporting_artifacts")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise MinionWorkOrderValidationError(f"supporting_artifacts[{index}] must be an object", field="supporting_artifacts")
        kind = str(item.get("kind") or item.get("type") or "").strip().lower()
        if not kind:
            raise MinionWorkOrderValidationError(f"supporting_artifacts[{index}].kind is required", field="supporting_artifacts")
        if kind in {"final_plan_artifact", "plan", "plan_ref"}:
            raise MinionWorkOrderValidationError(
                f"supporting_artifacts[{index}] cannot contain executable plans; use top-level plan_ref",
                field="supporting_artifacts",
            )
        normalized = {"kind": kind, "role": str(item.get("role") or "").strip()}
        if kind in {"review_report", "research_report", "nutrition_log", "spec_doc", "source_report"}:
            normalized.update(validated_file_artifact_ref(item, repository=repository, field=f"supporting_artifacts[{index}]"))
        elif kind == "checkpoint":
            checkpoint_id = str(item.get("checkpoint_id") or item.get("id") or "").strip()
            if not checkpoint_id:
                raise MinionWorkOrderValidationError(f"supporting_artifacts[{index}].checkpoint_id is required", field="supporting_artifacts")
            normalized["checkpoint_id"] = checkpoint_id
        elif kind in {"review_gate", "plan_review_gate", "review_gate_ref"}:
            kind = "review_gate"
            gate_id = str(item.get("gate_id") or item.get("id") or "").strip()
            ref = dict(item.get("ref") or {}) if isinstance(item.get("ref"), dict) else {}
            if gate_id and "gate_id" not in ref:
                ref["gate_id"] = gate_id
            if not str(ref.get("gate_id") or "").strip():
                raise MinionWorkOrderValidationError(f"supporting_artifacts[{index}].gate_id is required", field="supporting_artifacts")
            if repository is not None:
                try:
                    gate = repository.load_review_gate(ref)
                except Exception as exc:
                    raise MinionWorkOrderValidationError(str(exc), field="supporting_artifacts") from exc
                ref = dict(gate.get("review_gate_ref") or ref)
            normalized["kind"] = "review_gate"
            normalized["ref"] = ref
        else:
            raise MinionWorkOrderValidationError(f"unsupported supporting_artifacts[{index}].kind: {kind}", field="supporting_artifacts")
        result.append(normalized)
    return result


def validate_plan_revision_gate_ref(value: Any, *, repository: MinionTaskingRepository) -> dict[str, Any]:
    ref = dict(value or {}) if isinstance(value, dict) else {}
    if not str(ref.get("gate_id") or "").strip():
        raise MinionWorkOrderValidationError("feedback_gate_ref.gate_id is required", field="feedback_gate_ref")
    try:
        gate = repository.load_review_gate(ref)
    except Exception as exc:
        raise MinionWorkOrderValidationError(str(exc), field="feedback_gate_ref") from exc
    payload = dict(gate.get("review_gate") or {})
    if str(payload.get("gate_kind") or "").strip().lower() != "plan_acceptance":
        raise MinionWorkOrderValidationError("feedback_gate_ref must reference a plan_acceptance gate", field="feedback_gate_ref")
    if str(payload.get("verdict") or "").strip().lower() == "pass":
        raise MinionWorkOrderValidationError("feedback_gate_ref must reference a failing or partial gate", field="feedback_gate_ref")
    return dict(gate.get("review_gate_ref") or ref)


def artifact_ref_payload(item: dict[str, Any]) -> dict[str, Any]:
    ref = dict(item.get("ref") or {}) if isinstance(item.get("ref"), dict) else {}
    for key in ("path", "artifact_path", "relative_path", "sha256", "accepted", "acceptance_marker_path", "accepted_latest_marker_path", "plan_id", "task_id", "plan_revision"):
        if key in item and key not in ref:
            ref[key] = item[key]
    return ref


def validated_file_artifact_ref(item: dict[str, Any], *, repository: MinionTaskingRepository | None, field: str) -> dict[str, Any]:
    path_text = str(item.get("path") or item.get("artifact_path") or item.get("relative_path") or "").strip()
    if not path_text:
        raise MinionWorkOrderValidationError(f"{field}.path is required", field="supporting_artifacts")
    payload = {"path": path_text}
    sha256 = str(item.get("sha256") or "").strip()
    if sha256:
        payload["sha256"] = sha256
    if repository is None:
        return payload
    path = Path(path_text)
    resolved = path if path.is_absolute() else repository.runtime_root / path
    resolved = resolved.resolve()
    root = repository.runtime_root.resolve()
    if not resolved.is_relative_to(root):
        raise MinionWorkOrderValidationError(f"{field}.path must be under runtime_root", field="supporting_artifacts")
    if not resolved.is_file():
        raise MinionWorkOrderValidationError(f"{field}.path does not exist", field="supporting_artifacts")
    digest = sha256_file(resolved)
    if sha256 and sha256 != digest:
        raise MinionWorkOrderValidationError(f"{field}.sha256 mismatch", field="supporting_artifacts")
    payload["path"] = str(resolved)
    payload["sha256"] = digest
    return payload


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inject_spawn_bonus_skill_refs(pack: TaskContextPack, args: dict[str, Any]) -> TaskContextPack:
    bonus_refs = _string_list(args.get("spawn_bonus_skill_refs") or args.get("bonus_skill_refs"))
    if not bonus_refs:
        return pack
    metadata = dict(pack.metadata or {})
    metadata["spawn_bonus_skill_refs"] = _string_list(
        [
            *_string_list(metadata.get("spawn_bonus_skill_refs")),
            *bonus_refs,
        ]
    )
    return TaskContextPack.from_dict({**pack.to_dict(), "metadata": metadata})


class MinionSpawnResolutionError(ValueError):
    def __init__(self, *, query: str, candidates: list[dict[str, Any]]) -> None:
        super().__init__("minion task query did not resolve to exactly one work order")
        self.payload = {"query": query, "candidates": candidates, "candidate_count": len(candidates)}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
