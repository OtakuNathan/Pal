from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from pal.checklist.service import ChecklistService
from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
from pal.execution.contracts import CapabilityCall, CapabilityResult
from pal.execution.tool_facade import NextToolHint, StrictToolModel, ToolGuidance
from pal.execution.tool_semantics import (
    DIRECT_LOCAL_WRITE,
    INDIRECT_LOCAL_READ,
    INDIRECT_LOCAL_WRITE,
)
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
        guidance=ToolGuidance(
            purpose="Create or replace Pal's active checklist.",
            use_when="The task-flow guidance calls for a checklist, or its concrete steps have materially changed.",
            do_not_use_when="The active checklist already matches the work.",
            failure_next_steps="Pass a non-empty plan of 1..64 steps, each with a non-empty step string and an optional status of pending/in_progress/completed.",
            next_tool_hints=(
                NextToolHint(
                    name="checklist_check",
                    use_when="One concrete checklist step has actually been completed.",
                ),
                NextToolHint(
                    name="checklist_show",
                    use_when="Exact step text or remaining progress must be recovered.",
                ),
            ),
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
        guidance=ToolGuidance(
            purpose="Mark one exact step in Pal's active checklist as completed.",
            use_when="That concrete step has actually completed.",
            do_not_use_when="The step is still pending or no checklist is active.",
            failure_next_steps="If no checklist is active, call checklist_upsert. If the step does not match exactly, use checklist_show to recover its text.",
            next_tool_hints=(
                NextToolHint(
                    name="checklist_show",
                    use_when="The exact remaining step text or overall progress must be inspected.",
                ),
                NextToolHint(
                    name="checklist_clear",
                    use_when="Every step is complete and the work has been re-verified.",
                ),
            ),
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
        guidance=ToolGuidance(
            purpose="Read Pal's active checklist and exact step text.",
            use_when="Exact step text or current progress is needed.",
            do_not_use_when="The runtime reminder already provides enough checklist state.",
            failure_next_steps="If inactive, no checklist is open.",
            next_tool_hints=(
                NextToolHint(
                    name="checklist_check",
                    use_when="The snapshot identifies a step that has now been completed.",
                ),
                NextToolHint(
                    name="checklist_clear",
                    use_when="The snapshot is complete and the work has been re-verified.",
                ),
            ),
        ),
        execution=INDIRECT_LOCAL_READ,
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
        guidance=ToolGuidance(
            purpose="Remove Pal's active checklist.",
            use_when="The checklist has reached a terminal state defined by the task-flow guidance.",
            do_not_use_when="Checklist work remains in progress.",
            failure_next_steps="If inactive, this is an idempotent no-op. If uncertain, use checklist_show to inspect the current state.",
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
        guidance=ToolGuidance(
            purpose="Inspect the checklist module's current state.",
            use_when="Diagnosing checklist state or verifying the module is mounted.",
            do_not_use_when="Managing checklist work as Pal (use checklist_show, checklist_upsert, checklist_check, or checklist_clear).",
            failure_next_steps="Read-only. If inactive, no checklist is open.",
        ),
        execution=INDIRECT_LOCAL_READ,
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
    from pal.checklist.prompt import ChecklistPromptFragmentProvider

    provider = ChecklistIntrospectionProvider(service=service)
    prompt_provider = ChecklistPromptFragmentProvider(service=service)
    handle = ModuleHandle(
        module_id="checklist",
        tier=MODULE_TIER_CORE_FOUNDATION,
        detachable=False,
        introspection_provider=provider,
        prompt_fragment_providers=[prompt_provider],
        ports={"checklist": service},
    )
    context.register_module(handle)
    context.prompt_fragment_registry.register(prompt_provider)
    return handle
