from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.minion.profiles import filter_minion_allowed_capabilities, is_minion_capability_denied
from pal.shared import RuntimeStatus, llm_tool_name
from pal.shared.tool_aliases import EXPLICIT_LLM_TOOL_ALIASES


_LLM_TOOL_ALIAS_TO_CANONICAL = {alias: canonical for canonical, alias in EXPLICIT_LLM_TOOL_ALIASES.items()}
_MINION_PROFILE_SHORT_ALIASES = {
    "shell": "op_exec_shell",
    "shell_exec": "op_exec_shell",
    "run_shell": "op_exec_shell",
    "tree": "op_tree",
    "search": "op_search",
    "git": "op_git",
    "artifact_write": "op_minion_artifact_write",
    "artifact_edit": "op_minion_artifact_edit",
    "memory_candidate_write": "op_minion_memory_candidate_write",
    "checkpoint_commit": "op_minion_checkpoint_commit",
    "gate_contract_submit": "op_minion_gate_contract_submit",
    "review_gate_submit": "op_minion_review_gate_submit",
    "review_checkpoint": "op_minion_review_checkpoint",
}


@dataclass(frozen=True)
class MinionToolAdmission:
    call: CanonicalToolCall
    target_name: str
    ok: bool
    reason: str = ""
    message: str = ""
    capability: str = ""

    def to_result(self) -> CanonicalToolResult:
        message = self.message or "capability is not allowed for this minion run"
        return CanonicalToolResult(
            name=self.call.name,
            ok=False,
            text=message,
            structured={"reason": self.reason or "capability_not_allowed", "capability": self.capability or self.target_name},
            call_id=self.call.call_id,
            llm_text=message,
            status=RuntimeStatus.ERROR,
        )


def normalize_minion_capability_name(name: object, *, visible_names: list[str] | tuple[str, ...] = ()) -> str:
    raw = str(name or "").strip()
    if not raw:
        return ""
    if raw.startswith("op_") or raw.startswith("intro_"):
        return raw
    if raw in {"shell", "shell_exec", "run_shell"}:
        return "op_exec_shell"
    if raw in {"web_search", "search_web"}:
        return "op_web_search"
    if raw in {"web_read", "read_web"}:
        return "op_web_read"
    alias = _MINION_PROFILE_SHORT_ALIASES.get(raw) or _LLM_TOOL_ALIAS_TO_CANONICAL.get(raw)
    if alias:
        return alias
    if raw.startswith("minion_"):
        return f"op_{raw}"
    if raw.startswith(("plan_", "repair_bill_", "checklist_")):
        return f"op_minion_{raw}"
    if raw.startswith("lsp_"):
        return f"op_{raw}"
    visible = _visible_names(visible_names)
    if raw.startswith("plan_"):
        candidate = f"op_minion_{raw}"
        if candidate in visible:
            return candidate
    if raw.startswith("minion_plan_"):
        candidate = f"op_{raw}"
        if candidate in visible:
            return candidate
    matches = [candidate for candidate in visible if candidate != raw and llm_tool_name(candidate) == raw]
    if len(matches) == 1:
        return matches[0]
    return raw


def normalize_minion_allowed_capabilities(
    values: list[str] | tuple[str, ...],
    *,
    extra_visible_names: list[str] | tuple[str, ...] = (),
) -> list[str]:
    visible = _visible_names([*list(values or ()), *list(extra_visible_names or ())])
    normalized = [normalize_minion_capability_name(item, visible_names=visible) for item in list(values or ())]
    return filter_minion_allowed_capabilities(normalized)


def resolve_minion_tool_call_alias(
    call: CanonicalToolCall,
    allowed_capabilities: list[str] | tuple[str, ...],
    *,
    workspace_tool_names: list[str] | tuple[str, ...] = (),
) -> CanonicalToolCall:
    visible = _visible_names([*list(allowed_capabilities or ()), *list(workspace_tool_names or ())])
    name = normalize_minion_capability_name(call.name, visible_names=visible)
    args = dict(call.args or {})
    if name in {"op_tool_call", "call_tool"}:
        for key in ("name", "capability", "tool"):
            if str(args.get(key) or "").strip():
                args[key] = normalize_minion_capability_name(args.get(key), visible_names=visible)
                break
    if name == call.name and args == dict(call.args or {}):
        return call
    return CanonicalToolCall(name=name, args=args, call_id=call.call_id)


def effective_minion_capability_name(call: CanonicalToolCall) -> str:
    if call.name in {"op_tool_call", "call_tool"}:
        args = dict(call.args or {})
        return str(args.get("name") or args.get("capability") or args.get("tool") or call.name).strip()
    return call.name


def effective_minion_tool_args(call: CanonicalToolCall) -> dict[str, Any]:
    if call.name in {"op_tool_call", "call_tool"} and isinstance((call.args or {}).get("args"), dict):
        return dict((call.args or {}).get("args") or {})
    return dict(call.args or {})


def admit_minion_tool_call(
    call: CanonicalToolCall,
    allowed_capabilities: list[str] | tuple[str, ...],
    *,
    require_effective_target: bool = True,
) -> MinionToolAdmission:
    normalized_allowed = normalize_minion_allowed_capabilities(list(allowed_capabilities or ()))
    call = resolve_minion_tool_call_alias(call, normalized_allowed)
    allowed = set(_visible_names(normalized_allowed))
    target_name = effective_minion_capability_name(call)
    denied_name = call.name if is_minion_capability_denied(call.name) else ""
    if not denied_name and is_minion_capability_denied(target_name):
        denied_name = target_name
    if denied_name:
        return MinionToolAdmission(
            call=call,
            target_name=target_name,
            ok=False,
            reason="capability_denied_by_minion_policy",
            message="capability is denied by minion policy",
            capability=target_name,
        )
    if call.name not in allowed or (require_effective_target and target_name not in allowed):
        return MinionToolAdmission(
            call=call,
            target_name=target_name,
            ok=False,
            reason="capability_not_allowed",
            message="capability is not allowed for this minion run",
            capability=target_name,
        )
    return MinionToolAdmission(call=call, target_name=target_name, ok=True)


def _visible_names(values: list[str] | tuple[str, ...]) -> list[str]:
    return list(dict.fromkeys(str(item).strip() for item in values if str(item or "").strip()))
