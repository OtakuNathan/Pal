from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from pal.checklist.service import ChecklistService
from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
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


def _checklist_clear_event() -> dict[str, Any]:
    # Control delivery is independent of the full snapshot and its display budget.
    return {"tag": "checklist", "text": "Checklist cleared.",
            "payload": {"action": "clear", "active": False}}


def _checklist_echo(action: str, snapshot: Any) -> dict[str, Any]:
    plan = snapshot.plan
    cursor = next((index for index, item in enumerate(plan) if item["status"] != "completed"), 0)
    start = max(0, cursor - 2)
    end = min(len(plan), start + 8)
    lines = [f"Checklist progress {snapshot.done}/{snapshot.total}"]
    if start:
        lines.append(f"… {start} earlier steps")
    for item in plan[start:end]:
        step = " ".join(item["step"].split())
        if len(step) > 160:
            step = step[:159] + "…"
        mark = "✅" if item["status"] == "completed" else "⬜"
        lines.append(f"{mark} {step}")
    if end < len(plan):
        lines.append(f"… {len(plan) - end} more steps")
    return {
        "markdown": "\n".join(lines),
        "tag": "checklist",
        "payload": {"action": action, "active": snapshot.active,
                    "done": snapshot.done, "total": snapshot.total},
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
            purpose="Create or replace Pal's active execution-cursor checklist, including multiple status updates in one call. A fully completed plan closes automatically.",
            use_when=(
                "Unless the user specifies otherwise, use before the first mutation in work with multiple delivery phases, long execution, or resumption needs"
                "; update when phases materially change. Batch creation or updates with independent useful calls. Routine edit-and-test work needs no checklist."
            ),
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
        previous = self.service.show()
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
        changed = previous is None or previous.plan != snapshot.plan
        payload["changed"] = changed
        payload["cleared"] = not snapshot.active
        if not snapshot.active:
            payload["channel_event"] = _checklist_clear_event()
        elif changed:
            payload["echo"] = _checklist_echo("upsert", snapshot)
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="checklist upserted",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Checklist upserted", {key: payload[key] for key in ("changed", "active", "done", "total", "cleared")}),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="checklist",
        action_name="check",
        guidance=ToolGuidance(
            purpose="Mark one exact step as completed; checking the last unfinished step automatically closes the checklist.",
            use_when="The step is already confirmed complete. Prefer calling alongside the next useful tools in the same response, rather than in a separate bookkeeping round.",
            do_not_use_when="Completion depends on results from tools in the same batch; wait for those results first. No checklist is active.",
            failure_next_steps="If no checklist is active, it may already have closed; open a new one only if work remains. If the step does not match exactly, use checklist_show to recover its text.",
            next_tool_hints=(
                NextToolHint(
                    name="checklist_show",
                    use_when="The exact remaining step text or overall progress must be inspected.",
                ),
                NextToolHint(
                    name="checklist_clear",
                    use_when="Cancel or retire remaining work. Both the last check and a fully completed upsert close automatically.",
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
                llm_text="No active checklist; it may already have completed and closed. Use checklist_upsert only if work remains.",
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
            "cleared": outcome.cleared,
            "step": step,
            **_snapshot_payload(outcome.snapshot),
        }
        if outcome.cleared:
            payload["channel_event"] = _checklist_clear_event()
        elif outcome.changed:
            payload["echo"] = _checklist_echo("check", outcome.snapshot)
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="checklist completed and closed" if outcome.cleared else "checklist step checked" if outcome.changed else "checklist step already completed",
            structured=payload,
            llm_text=render_titled_structured_for_llm(
                "Checklist completed and closed" if outcome.cleared else "Checklist step checked" if outcome.changed else "Checklist step unchanged",
                {key: payload[key] for key in ("changed", "step", "done", "total", "cleared")},
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
                    use_when=(
                        "The task is complete, cancelled, replaced, or made stale; close the progress cursor without additional work."
                    ),
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
            purpose="Close Pal's checklist and return its recorded progress.",
            use_when=(
                "The task is complete, cancelled, replaced, or made stale. Closing adds no verification requirement; "
                "describe actual execution evidence and the verification scope already performed."
            ),
            do_not_use_when="The active task is still expected to continue and checklist work remains in progress.",
            failure_next_steps="If inactive, this is an idempotent no-op. If uncertain, use checklist_show to inspect the current state.",
        ),
        execution=DIRECT_LOCAL_WRITE,
        metadata={"canonical_path": "op_checklist_clear"},
        aliases=("checklist_clear",),
    )
    def clear(self, call: CapabilityCall) -> CapabilityResult:
        _ = call
        retired = self.service.show()
        cleared = self.service.clear()
        payload = {"cleared": cleared}
        if cleared:
            if retired is not None:
                retired_payload = _snapshot_payload(retired)
                retired_payload["active"] = False
                payload["retired_checklist"] = retired_payload
            payload["channel_event"] = _checklist_clear_event()
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="checklist cleared" if cleared else "no active checklist",
            structured=payload,
            llm_text=render_titled_structured_for_llm(
                "Checklist cleared" if cleared else "No active checklist",
                {"cleared": cleared},
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
    from pal.checklist.runtime_state import ChecklistRuntimeStatePort

    provider = ChecklistIntrospectionProvider(service=service)
    prompt_provider = ChecklistPromptFragmentProvider(service=service)
    handle = ModuleHandle(
        module_id="checklist",
        tier=MODULE_TIER_DETACHABLE,
        detachable=True,
        introspection_provider=provider,
        prompt_fragment_providers=[prompt_provider],
        ports={"checklist": service},
        runtime_state_port=ChecklistRuntimeStatePort(service),
    )
    context.register_module(handle)
    context.prompt_fragment_registry.register(prompt_provider)
    return handle
