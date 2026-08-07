from __future__ import annotations

from dataclasses import dataclass

from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
from pal.execution.tool_facade import ToolGuidance
from pal.failure.runtime import FailureRuntime
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    capability_action,
    capability_node,
)
from pal.shared.result_rendering import render_titled_structured_for_llm


@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:failure",
    target_kind="module",
)
@dataclass
class FailureIntrospectionProvider:
    runtime: FailureRuntime
    module_id: str = "failure"

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="module", action_name="show", description="Show failure runtime summary",
        guidance=ToolGuidance(
            purpose="Show failure runtime summary.",
            use_when="Diagnosing reply delivery failures or checking failure tracking health.",
            do_not_use_when="Checking LLM errors (use llm_usage). Checking proactive task failures (use proactive_list_runs).",
            failure_next_steps="Read-only diagnostic. Use failure_recent_reports for specific failure details.",
        ), aliases=("failure_show",))
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        summary = self.runtime.show_summary()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="failure runtime summary",
            structured=summary,
            llm_text=render_titled_structured_for_llm("Failure runtime summary", summary),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="recent_reports",
        description="List recent structured failure reports",
        guidance=ToolGuidance(
            purpose="List recent structured failure reports.",
            use_when="Investigating why replies or deliveries failed recently.",
            do_not_use_when="Module-level health (use failure_show).",
            failure_next_steps="Read-only. Returns the last 16 reports. If empty, no failures have been recorded.",
        ),
        aliases=("failure_recent_reports",),
    )
    def recent_reports(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        items = [report.__dict__ for report in self.runtime.recent_reports[-16:]]
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="recent failure reports",
            structured={"items": items},
            llm_text=render_titled_structured_for_llm("Recent failure reports", {"items": items}),
        )


def register_with_core(core, runtime: FailureRuntime) -> ModuleHandle:
    from pal.failure.handler import FailureEventHandler
    from pal.shared import EventKind

    provider = FailureIntrospectionProvider(runtime=runtime)
    handle = ModuleHandle(
        module_id="failure",
        tier=MODULE_TIER_CORE_FOUNDATION,
        detachable=False,
        introspection_provider=provider,
        ports={"failure": runtime},
    )
    core.context.register_module(handle)
    core.context.event_handler_registry.register(EventKind.REPLY_FAILED, FailureEventHandler(core=core))
    return handle
