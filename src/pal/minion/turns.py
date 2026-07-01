from __future__ import annotations

from typing import Any

from pal.shared import TaskContextPack


_RUNNER_PRESERVED_METADATA_KEYS = {
    "active_gate_todo",
    "allow_text_only_completion",
    "checkpoint_repair",
    "checkpoint_review",
    "clarification_answers",
    "control_route",
    "debug_log",
    "defer_experience_until_module_complete",
    "execution_contract",
    "gate_specs",
    "heartbeat_interval_seconds",
    "injected_skill_refs",
    "llm_round_timeout_seconds",
    "manager_turn_timeout_seconds",
    "max_output_tokens",
    "minion_debug_log_enabled",
    "module_execution",
    "module_id",
    "module_name",
    "original_source_contract",
    "parent_module_id",
    "parent_module_name",
    "parent_work_order_id",
    "plan_review",
    "planner_work_order",
    "pack_allowed_skill_refs",
    "plan_ref",
    "plan_revision",
    "preferred_endpoint_id",
    "preferred_endpoint_source",
    "pre_plan_contract",
    "pre_plan_contract_compiler",
    "pre_plan_contract_source_pack",
    "pre_plan_contract_source_work_order_id",
    "profile_skill_refs",
    "prompt_log_enabled",
    "prompt_view",
    "repair_context",
    "requirements_brief",
    "review_target",
    "review_feedback",
    "reviewer_work_order",
    "review_gate",
    "review_gate_ref",
    "skill_manual_context",
    "source_contract",
    "source_plan_artifact",
    "source_plan_ref",
    "spawn_bonus_skill_refs",
    "supporting_artifacts",
    "task_id",
    "task_title",
    "timeout_seconds",
    "unresolved_skill_refs",
    "work_order_skill_refs",
    "workspace",
}

_TURN_METADATA_KEYS = {
    "active_gate_todo",
    "checkpoint_repair",
    "execution_contract",
    "gate_specs",
    "module_execution",
    "module_id",
    "original_source_contract",
    "plan_ref",
    "plan_revision",
    "planner_work_order",
    "prompt_view",
    "repair_context",
    "requirements_brief",
    "review_feedback",
    "review_gate",
    "review_gate_ref",
    "source_contract",
    "source_plan_artifact",
    "source_plan_ref",
    "task_id",
    "workspace",
}


def sanitize_runner_session_pack(pack: TaskContextPack) -> TaskContextPack:
    """Keep the runner pack scoped to the live minion session, not manager planning state."""
    metadata = {
        key: value
        for key, value in dict(pack.metadata or {}).items()
        if key in _RUNNER_PRESERVED_METADATA_KEYS
    }
    return TaskContextPack.from_dict({**pack.to_dict(), "metadata": metadata})


def build_minion_turn_from_pack(pack: TaskContextPack) -> dict[str, Any]:
    metadata = dict(pack.metadata or {})
    prompt_view = dict(metadata.get("prompt_view") or {})
    current_milestone = dict(prompt_view.get("milestone") or {}) if prompt_view else {}
    if not current_milestone:
        current_milestone = dict((pack.continuity or {}).get("current_milestone") or {})
    metadata_updates = {
        key: value
        for key, value in metadata.items()
        if key in _TURN_METADATA_KEYS
    }
    if prompt_view:
        metadata_updates["prompt_view"] = prompt_view
    workspace_updates = dict(pack.workspace or {})
    return {
        "type": "next_turn",
        "turn_kind": "milestone",
        "work_order_id": pack.work_order_id,
        "goal": pack.goal,
        "instruction": pack.instruction,
        "acceptance_criteria": list(pack.acceptance_criteria),
        "current_milestone": current_milestone,
        "prompt_view": prompt_view,
        "metadata_updates": metadata_updates,
        "workspace_updates": workspace_updates,
    }


def apply_minion_turn_to_pack(
    pack: TaskContextPack,
    turn: dict[str, Any],
    *,
    checkpoint_payload: dict[str, Any] | None = None,
) -> TaskContextPack:
    payload = dict(turn or {})
    metadata = dict(pack.metadata or {})
    for key, value in dict(payload.get("metadata_updates") or {}).items():
        if key in _RUNNER_PRESERVED_METADATA_KEYS:
            metadata[key] = value
    if isinstance(payload.get("prompt_view"), dict):
        metadata["prompt_view"] = dict(payload.get("prompt_view") or {})
    if payload.get("current_milestone"):
        current_milestone = dict(payload.get("current_milestone") or {})
    else:
        current_milestone = dict((metadata.get("prompt_view") or {}).get("milestone") or {})
    continuity = dict(pack.continuity or {})
    if current_milestone:
        continuity["current_milestone"] = current_milestone
    workspace = dict(pack.workspace or {})
    workspace.update(dict(payload.get("workspace_updates") or {}))
    checkpoint = dict(checkpoint_payload or {})
    commit_sha = str(checkpoint.get("commit_sha") or "").strip()
    if commit_sha:
        workspace["base_sha"] = commit_sha
        metadata_workspace = dict(metadata.get("workspace") or {})
        metadata_workspace["base_sha"] = commit_sha
        metadata["workspace"] = metadata_workspace
    goal = str(payload.get("goal") or pack.goal)
    instruction = str(payload.get("instruction") or pack.instruction or goal)
    acceptance = list(payload.get("acceptance_criteria") or pack.acceptance_criteria)
    updated = TaskContextPack.from_dict(
        {
            **pack.to_dict(),
            "goal": goal,
            "instruction": instruction,
            "acceptance_criteria": acceptance,
            "workspace": workspace,
            "continuity": continuity,
            "metadata": metadata,
        }
    )
    return sanitize_runner_session_pack(updated)
