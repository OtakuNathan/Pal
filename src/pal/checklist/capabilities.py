from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from pal.checklist.service import ChecklistService
from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
from pal.execution.contracts import CapabilityCall, CapabilityResult
from pal.execution.tool_facade import StrictToolModel, ToolGuidance
from pal.execution.tool_semantics import DIRECT_LOCAL_READ, DIRECT_LOCAL_WRITE
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    OPERATION_NAMESPACE,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    capability_action,
    capability_node,
)
from pal.shared.result_rendering import render_titled_structured_for_llm

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


class ChecklistStepModel(StrictToolModel):
    step: str = Field(min_length=1, max_length=1000)
    status: Literal["pending", "in_progress", "completed"] = "pending"


class ChecklistUpsertInput(StrictToolModel):
    plan: list[ChecklistStepModel] = Field(min_length=1, max_length=64)


class ChecklistCheckInput(StrictToolModel):
    step: str = Field(min_length=1, max_length=1000)


def _snapshot_payload(snapshot: Any) -> dict[str, Any]:
    return {
        "active": bool(snapshot.active),
        "plan": [dict(item) for item in snapshot.plan],
        "done": int(snapshot.done),
        "total": int(snapshot.total),
        "markdown": str(snapshot.markdown),
    }


@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:checklist",
    target_kind="module",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:checklist",
    target_kind="module",
)
@dataclass
class ChecklistIntrospectionProvider:
    service: ChecklistService
    module_id: str = "checklist"

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="checklist",
        action_name="upsert",
        description="Open or replace Pal's own task checklist. Land preflight plans here as visible steps, tick them off as you finish, and clear after re-verifying.",
        guidance=ToolGuidance(
            purpose="Open or replace Pal's own task checklist.",
            use_when="Use when the task feels like it is becoming multi-step or a bit complex, especially once side effects (writing, mutating, sending, executing, creating) are involved — land the action path as visible steps with the step where side effects begin marked as the boundary. If work grows multi-step or complex mid-task after starting without a checklist, open one at that point and mark already-finished steps as completed. If the task is too complex for a scratchpad (large, long-running, or needing architect/coder/verifier gates), skip the checklist and use minion directly.",
            do_not_use_when="Single-step or conversational work. Durable project plans (use memory). Minion task ledger work (that is Manager-owned). Anything needing gating or enforcement — this is a scratchpad, not a cursor.",
            failure_next_steps="Pass a non-empty plan of 1..64 steps, each with a non-empty step string and an optional status of pending/in_progress/completed.",
        ),
        InputModel=ChecklistUpsertInput,
        execution=DIRECT_LOCAL_WRITE,
        metadata={"canonical_path": "op_checklist_upsert"},
        aliases=("checklist_upsert",),
    )
    def upsert(self, call: CapabilityCall) -> CapabilityResult:
        try:
            snapshot = self.service.upsert(list(call.args.get("plan") or []))
        except ValueError as exc:
            return CapabilityResult(
                status=RuntimeStatus.ERROR,
                text=str(exc),
                structured={"error": str(exc)},
                llm_text=f"Checklist upsert rejected: {exc}",
            )
        payload = _snapshot_payload(snapshot)
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="checklist upserted",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Checklist upserted", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="checklist",
        action_name="check",
        description="Mark one checklist step completed and echo the updated checklist to the user's channel.",
        guidance=ToolGuidance(
            purpose="Tick one step of the active checklist as completed.",
            use_when="Pal finished one concrete step of an open checklist and wants the user to see progress (core fans the echo out to the channel). When the last step is checked, remember to call checklist_clear once the work is re-verified.",
            do_not_use_when="No active checklist (upsert first). Inventing completion for work not actually finished — check only what was really done. Using it as evidence or a submission gate.",
            failure_next_steps="If no active checklist, call checklist_upsert first. If the step string does not match an open step exactly, re-check the exact step text from checklist_show.",
        ),
        InputModel=ChecklistCheckInput,
        execution=DIRECT_LOCAL_WRITE,
        metadata={"canonical_path": "op_checklist_check"},
        aliases=("checklist_check",),
    )
    def check(self, call: CapabilityCall) -> CapabilityResult:
        step = str(call.args.get("step") or "").strip()
        outcome = self.service.check(step)
        if outcome.snapshot is None:
            return CapabilityResult(
                status=RuntimeStatus.ERROR,
                text="no active checklist",
                structured={"changed": False, "step": step, "error": "no_active_checklist"},
                llm_text="No active checklist. Call checklist_upsert first to open one.",
            )
        if not outcome.found:
            return CapabilityResult(
                status=RuntimeStatus.ERROR,
                text="checklist step not found",
                structured={"changed": False, "step": step, "error": "step_not_found"},
                llm_text=(
                    f"Step {step!r} is not in the active checklist. "
                    "Use checklist_show to see the exact step texts."
                ),
            )
        payload = {
            "changed": outcome.changed,
            "step": step,
            **_snapshot_payload(outcome.snapshot),
        }
        if outcome.changed:
            payload["echo"] = {
                "markdown": str(outcome.snapshot.markdown),
                "dedupe_key": f"checklist:check:{step}",
            }
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="checklist step checked" if outcome.changed else "checklist step already completed",
            structured=payload,
            llm_text=render_titled_structured_for_llm(
                "Checklist step checked" if outcome.changed else "Checklist step unchanged",
                payload,
            ),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="checklist",
        action_name="show",
        description="Show the active checklist (steps, statuses, markdown).",
        guidance=ToolGuidance(
            purpose="Inspect Pal's own active checklist.",
            use_when="Pal needs to recall exact step texts, confirm what remains, or re-check progress mid-task.",
            do_not_use_when="Reading the user's durable memory (use recall_memory). Minion task ledger state (use minion status tools).",
            failure_next_steps="Read-only. If inactive, no checklist is open.",
        ),
        execution=DIRECT_LOCAL_READ,
        metadata={"canonical_path": "op_checklist_show"},
        aliases=("checklist_show",),
    )
    def show(self, call: CapabilityCall) -> CapabilityResult:
        _ = call
        snapshot = self.service.show()
        if snapshot is None:
            return CapabilityResult(
                status=RuntimeStatus.OK,
                text="no active checklist",
                structured={"active": False, "plan": [], "done": 0, "total": 0, "markdown": ""},
                llm_text="No active checklist.",
            )
        payload = _snapshot_payload(snapshot)
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="checklist snapshot",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Checklist snapshot", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="checklist",
        action_name="clear",
        description="Discard the active checklist.",
        guidance=ToolGuidance(
            purpose="Tear down Pal's own task checklist.",
            use_when="Every step is completed and Pal re-verified the work against the list. Before delivering the result, audit the checklist one more time for anything missed; only then clear it. Also clear when the task changed and the list is stale.",
            do_not_use_when="The task is still in progress. Clearing before the final audit skips the delivery self-check this tool exists to support.",
            failure_next_steps="Read-only-ish. If inactive, nothing to clear.",
        ),
        execution=DIRECT_LOCAL_WRITE,
        metadata={"canonical_path": "op_checklist_clear"},
        aliases=("checklist_clear",),
    )
    def clear(self, call: CapabilityCall) -> CapabilityResult:
        _ = call
        cleared = self.service.clear()
        payload = {"cleared": cleared}
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="checklist cleared" if cleared else "no active checklist",
            structured=payload,
            llm_text=render_titled_structured_for_llm(
                "Checklist cleared" if cleared else "No active checklist",
                payload,
            ),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        family="checklist",
        action_name="show",
        description="Show the active checklist state (introspection).",
        guidance=ToolGuidance(
            purpose="Inspect the checklist module's current state.",
            use_when="Diagnosing checklist state or verifying the module is mounted.",
            do_not_use_when="Managing the checklist as Pal (use checklist_show/upsert/check/clear).",
            failure_next_steps="Read-only. If inactive, no checklist is open.",
        ),
        execution=DIRECT_LOCAL_READ,
        aliases=("checklist_inspect",),
    )
    def show_introspection(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        snapshot = self.service.show()
        payload = (
            _snapshot_payload(snapshot)
            if snapshot is not None
            else {"active": False, "plan": [], "done": 0, "total": 0, "markdown": ""}
        )
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="checklist module snapshot",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Checklist module snapshot", payload),
        )


def register_with_core(context: "MainContext", service: ChecklistService) -> ModuleHandle:
    provider = ChecklistIntrospectionProvider(service=service)
    handle = ModuleHandle(
        module_id="checklist",
        tier=MODULE_TIER_CORE_FOUNDATION,
        detachable=False,
        introspection_provider=provider,
        ports={"checklist": service},
    )
    context.register_module(handle)
    return handle
