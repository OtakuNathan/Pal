from __future__ import annotations

from collections.abc import Iterable
import re


EXPLICIT_LLM_TOOL_ALIASES: dict[str, str] = {
    "op_artifact_info": "artifact_info",
    "op_artifact_list": "list_artifacts",
    "op_artifact_read": "read_artifact",
    "op_artifact_search": "search_artifacts",
    "op_behavior_advise": "advise_behavior",
    "op_behavior_affordance_delete": "delete_behavior_affordance",
    "op_behavior_affordance_update": "update_behavior_affordance",
    "op_behavior_save": "save_behavior",
    "op_channel_send_attachment": "send_channel_attachment",
    "op_exec_shell": "run_shell",
    "op_file_edit": "edit_file",
    "op_file_read": "read_file",
    "op_file_state": "file_state",
    "op_file_write": "write_file",
    "op_git": "git",
    "op_memory_delete": "delete_memory",
    "op_memory_recall": "recall_memory",
    "op_memory_update": "update_memory",
    "op_memory_write": "write_memory",
    "op_path_delete": "delete_path",
    "op_minion_artifact_edit": "artifact_edit",
    "op_minion_artifact_write": "artifact_write",
    "op_minion_checkpoint_commit": "checkpoint_commit",
    "op_minion_gate_contract_submit": "gate_contract_submit",
    "op_minion_memory_candidate_write": "memory_candidate_write",
    "op_minion_plan_add_acceptance_criterion": "plan_add_acceptance_criterion",
    "op_minion_plan_add_acceptance_criteria_batch": "plan_add_acceptance_criteria_batch",
    "op_minion_plan_add_constraint": "plan_add_constraint",
    "op_minion_plan_add_constraints_batch": "plan_add_constraints_batch",
    "op_minion_plan_add_design_decision": "plan_add_design_decision",
    "op_minion_plan_add_gate_check": "plan_add_gate_check",
    "op_minion_plan_add_milestone_outline": "plan_add_milestone_outline",
    "op_minion_plan_add_module_interface": "plan_add_module_interface",
    "op_minion_plan_add_module_outline": "plan_add_module_outline",
    "op_minion_plan_begin": "plan_begin",
    "op_minion_plan_checkout": "plan_checkout",
    "op_minion_plan_begin_milestone": "plan_begin_milestone",
    "op_minion_plan_begin_module": "plan_begin_module",
    "op_minion_plan_delete_acceptance_criterion": "plan_delete_acceptance_criterion",
    "op_minion_plan_delete_gate_check": "plan_delete_gate_check",
    "op_minion_plan_delete_milestone": "plan_delete_milestone",
    "op_minion_plan_delete_module": "plan_delete_module",
    "op_minion_plan_end_milestone": "plan_end_milestone",
    "op_minion_plan_end_module": "plan_end_module",
    "op_minion_plan_find": "plan_find",
    "op_minion_plan_finalize": "plan_finalize",
    "op_minion_plan_get": "plan_get",
    "op_minion_plan_merge_modules": "plan_merge_modules",
    "op_minion_plan_move_milestone": "plan_move_milestone",
    "op_minion_plan_read": "plan_read",
    "op_minion_plan_replace_milestone_acceptance_criteria": "plan_replace_milestone_acceptance_criteria",
    "op_minion_plan_submit_for_review": "plan_submit_for_review",
    "op_minion_plan_update_acceptance_criterion": "plan_update_acceptance_criterion",
    "op_minion_plan_update_gate_check": "plan_update_gate_check",
    "op_minion_plan_update_milestone": "plan_update_milestone",
    "op_minion_plan_update_module": "plan_update_module",
    "op_minion_plan_update_plan": "plan_update_plan",
    "op_minion_plan_validate": "plan_validate",
    "op_minion_plan_validate_and_submit_for_review": "plan_validate_and_submit_for_review",
    "op_minion_review_checkpoint": "review_checkpoint",
    "op_minion_review_gate_submit": "review_gate_submit",
    "op_tool_call": "call_tool",
    "op_tool_read": "read_tool",
    "op_tool_result_page": "read_tool_result_page",
    "op_tool_search": "search_tools",
    "op_web_read": "read_web",
    "op_web_search": "search_web",
    "op_web_screenshot": "screenshot_web",
}

_INTERNAL_TOOL_NAME_RE = re.compile(r"\b(?:op|intro)_[A-Za-z0-9_]+\b")

DEDICATED_TOOL_ROUTE_HINTS: tuple[tuple[str, str], ...] = (
    ("op_tree", "structured repo listings"),
    ("op_search", "repository text search"),
    ("op_file_read", "reading repo text files"),
    ("op_file_edit", "precise repo text edits"),
    ("op_file_write", "creating, overwriting, or appending repo text files"),
    ("op_path_delete", "deleting repo paths"),
    ("op_minion_checkpoint_commit", "milestone checkpoint commits"),
    ("op_git", "git status, diff, log, show, and conservative audited git mutations"),
    ("op_minion_plan_read", "reading submitted plan drafts"),
    ("op_minion_plan_find", "finding plan nodes by handle"),
    ("op_minion_plan_get", "inspecting one plan node by handle"),
    ("op_minion_plan_validate", "validating plan topology and gate coverage"),
    ("op_minion_plan_validate_and_submit_for_review", "validating and submitting planner drafts for review"),
    ("op_minion_review_gate_submit", "submitting reviewer gate verdicts"),
)

RUN_SHELL_SCOPE_HINT = (
    "Keep run_shell for tests, builds, scripts, process probes, package commands, "
    "and process inspection."
)


def llm_tool_name(name: object) -> str:
    text = str(name or "").strip()
    if not text:
        return ""
    base, suffix = _split_instance_suffix(text)
    mapped = _llm_tool_base_name(base)
    return f"{mapped}{suffix}" if suffix else mapped


def _llm_tool_base_name(text: str) -> str:
    explicit = EXPLICIT_LLM_TOOL_ALIASES.get(text)
    if explicit:
        return explicit
    if text.startswith("intro_module_"):
        return text[len("intro_module_") :]
    if text.startswith("intro_provider_"):
        surface_action = text[len("intro_provider_") :]
        parts = surface_action.split("_", 1)
        if len(parts) == 2 and parts[0] and parts[1]:
            return f"{parts[0]}_provider_{parts[1]}"
        return f"provider_{surface_action}" if surface_action else "provider"
    if text.startswith("intro_"):
        return text[6:]
    if text.startswith("op_"):
        operation = text[3:]
        for marker in ("_mgmt_", "_lifecycle_"):
            if marker in operation:
                prefix, action = operation.split(marker, 1)
                if prefix and action:
                    return f"{prefix}_{action}"
        return operation
    return text


def _split_instance_suffix(text: str) -> tuple[str, str]:
    if "::" not in text:
        return text, ""
    base, suffix = text.split("::", 1)
    return base, f"::{suffix}"


def dedicated_tool_route_hints(allowed: Iterable[str] | None = None) -> tuple[str, ...]:
    allowed_set = (
        {str(item).strip() for item in allowed or () if str(item).strip()}
        if allowed is not None
        else None
    )
    return tuple(
        f"{llm_tool_name(tool_name)} for {purpose}"
        for tool_name, purpose in DEDICATED_TOOL_ROUTE_HINTS
        if allowed_set is None or tool_name in allowed_set
    )


def format_dedicated_tool_route_hints(allowed: Iterable[str] | None = None) -> str:
    return "; ".join(dedicated_tool_route_hints(allowed))


def replace_internal_tool_names(text: object) -> str:
    raw = str(text or "")
    if not raw:
        return ""
    return _INTERNAL_TOOL_NAME_RE.sub(lambda match: llm_tool_name(match.group(0)), raw)


def replace_internal_tool_names_in_value(value: object) -> object:
    if isinstance(value, str):
        return replace_internal_tool_names(value)
    if isinstance(value, dict):
        return {key: replace_internal_tool_names_in_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [replace_internal_tool_names_in_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(replace_internal_tool_names_in_value(item) for item in value)
    return value
