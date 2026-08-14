from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR

from pal.shared.tool_protocol import new_tool_call

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pal.bunshin.profiles import filter_bunshin_allowed_capabilities, is_bunshin_capability_denied
from pal.shared import RuntimeStatus, ToolExecutionResult


CapabilityNameResolver = Callable[[object], str]


@dataclass(frozen=True)
class BunshinToolAdmission:
    call: ToolCallIR
    target_name: str
    ok: bool
    reason: str = ""
    message: str = ""
    capability: str = ""

    def to_result(self) -> ToolExecutionResult:
        message = self.message or "capability is not allowed for this bunshin run"
        return ToolExecutionResult(
            name=self.call.name,
            ok=False,
            text=message,
            structured={"reason": self.reason or "capability_not_allowed", "capability": self.capability or self.target_name},
            call_id=self.call.call_id,
            llm_text=message,
            status=RuntimeStatus.ERROR,
        )


def resolve_bunshin_tool_call(
    call: ToolCallIR,
    resolve_name: CapabilityNameResolver,
) -> ToolCallIR:
    name = str(resolve_name(call.name) or "").strip()
    args = dict(call.args or {})
    if name == "op_tool_call":
        if str(args.get("name") or "").strip():
            args["name"] = str(resolve_name(args["name"]) or "").strip()
    if name == call.name and args == dict(call.args or {}):
        return call
    return new_tool_call(name=name, args=args, call_id=call.call_id)


def effective_bunshin_capability_name(call: ToolCallIR) -> str:
    if call.name == "op_tool_call":
        args = dict(call.args or {})
        return str(args.get("name") or call.name).strip()
    return call.name


def effective_bunshin_tool_args(call: ToolCallIR) -> dict[str, Any]:
    if call.name == "op_tool_call" and isinstance((call.args or {}).get("args"), dict):
        return dict((call.args or {}).get("args") or {})
    return dict(call.args or {})


def admit_bunshin_tool_call(
    call: ToolCallIR,
    allowed_capabilities: list[str] | tuple[str, ...],
    *,
    resolve_name: CapabilityNameResolver,
    require_effective_target: bool = True,
) -> BunshinToolAdmission:
    allowed = set(filter_bunshin_allowed_capabilities(_visible_names(list(allowed_capabilities or ()))))
    call = resolve_bunshin_tool_call(call, resolve_name)
    target_name = effective_bunshin_capability_name(call)
    denied_name = call.name if is_bunshin_capability_denied(call.name) else ""
    if not denied_name and is_bunshin_capability_denied(target_name):
        denied_name = target_name
    if denied_name:
        return BunshinToolAdmission(
            call=call,
            target_name=target_name,
            ok=False,
            reason="capability_denied_by_bunshin_policy",
            message="capability is denied by bunshin policy",
            capability=target_name,
        )
    if call.name not in allowed or (require_effective_target and target_name not in allowed):
        return BunshinToolAdmission(
            call=call,
            target_name=target_name,
            ok=False,
            reason="capability_not_allowed",
            message="capability is not allowed for this bunshin run",
            capability=target_name,
        )
    return BunshinToolAdmission(call=call, target_name=target_name, ok=True)


def _visible_names(values: list[str] | tuple[str, ...]) -> list[str]:
    return list(dict.fromkeys(str(item).strip() for item in values if str(item or "").strip()))
