from __future__ import annotations

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
    "op_tool_call": "call_tool",
    "op_tool_read": "read_tool",
    "op_tool_result_page": "read_tool_result_page",
    "op_tool_search": "search_tools",
    "op_web_read": "read_web",
    "op_web_search": "search_web",
    "op_web_screenshot": "screenshot_web",
}

_INTERNAL_TOOL_NAME_RE = re.compile(r"\b(?:op|intro)_[A-Za-z0-9_]+\b")


def llm_tool_name(name: object) -> str:
    text = str(name or "").strip()
    if not text:
        return ""
    explicit = EXPLICIT_LLM_TOOL_ALIASES.get(text)
    if explicit:
        return explicit
    if text.startswith("op_"):
        return text[3:]
    if text.startswith("intro_"):
        return text[6:]
    return text


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
