from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
from uuid import uuid4

from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
from pal.execution.contracts import CapabilityResult
from pal.execution.tool_facade import ToolGuidance
from pal.execution.tool_semantics import INDIRECT_LOCAL_READ
from pal.foundation.diagnostics import diagnostic_summary, diagnostic_value, exception_summary
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
from pal.shared.inspection import inspection_snapshot


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

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="show",
        guidance=ToolGuidance(
            search_objects=('state', 'report', 'reports', 'error', 'errors', 'history'),
            search_terms=('status', 'show', 'get', 'list', 'summary', 'recent', 'delivery', 'blocker', 'diagnosis'),
            purpose="Inspect failure tracking counts and the last 16 structured failure summaries, with a file containing full report details and evidence.",
            use_when="Investigating recent recorded failures, blockers, attempted repairs and verification outcomes. Takes no arguments; returns one fixed recent window, not a complete error history or a health probe.",
            do_not_use_when="Checking LLM token or cost metrics (use inspect_llm). Checking proactive task execution history (use list_proactive_runs). Unrecorded transport errors still require the affected service logs.",
            failure_next_steps="An empty list means no retained reports, not that every service is healthy. Use rg/read_file on reports_file.file_path for full evidence or truncated summaries. If output storage is unavailable, the summaries remain available and full reports remain in the resident owner; no file path is invented.",
        ),
        execution=INDIRECT_LOCAL_READ,
        aliases=("inspect_failure",),
    )
    def show(self, call: IntrospectionCall) -> CapabilityResult:
        retained = tuple(self.runtime.recent_reports)
        reports = retained[-16:]
        payload = inspection_snapshot({
            **self.runtime.show_summary(),
            "recent_report_count": len(retained),
            "latest_report_id": retained[-1].report_id if retained else None,
            "history_scope": "resident_recent_reports",
            "returned_report_count": len(reports),
            "omitted_report_count": len(retained) - len(reports),
            "report_limit": 16,
            "recent_diagnostics": [_report_summary(report.__dict__) for report in reports],
        })
        refs = ()
        if reports:
            try:
                execution = call.meta.get("execution_runtime")
                if execution is None:
                    raise RuntimeError("Output storage is unavailable for this caller")
                tool_call = call.meta.get("tool_call")
                call_id = tool_call.call_id if tool_call is not None else uuid4().hex
                lifetime = execution.logical_context_for_turn(call.meta.get("turn_id") or call_id).execution_lifetime_id
                text = json.dumps(diagnostic_value({"reports": [report.__dict__ for report in reports]}),
                                  ensure_ascii=False, indent=2, allow_nan=False)
                ref = execution.result_snapshots.capture(text, call_id=call_id, lifetime=lifetime,
                                                        coverage="complete returned failure reports (credentials redacted)")
                refs = (ref,)
                payload["reports_file"] = {"file_path": ref.path, "size_bytes": ref.size_bytes,
                                           "sha256": ref.digest, "read_only": True, "format": "json"}
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                payload["reports_file"] = {"status": "unavailable", "error": _brief(exception_summary(exc), 400)}
                payload["diagnostics"]["partial"] = True
                payload["diagnostics"]["unavailable_sections"]["reports_file"] = "output_storage_unavailable"
        else:
            payload["reports_file"] = {"status": "not_needed", "reason": "no_recent_reports"}
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="failure runtime snapshot",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Failure runtime snapshot", payload),
            snapshot_refs=refs,
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


def _brief(value: Any, limit: int) -> str:
    text = " ".join(diagnostic_summary(str(value)).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _report_summary(report: dict[str, Any]) -> dict[str, Any]:
    limits = {
        "report_id": 128, "subsystem": 96, "component": 128, "severity": 32,
        "failure_kind": 96, "why_blocked": 240, "current_blocker": 240,
        "impact": 160, "verification_status": 32, "recommended_next_step": 240,
    }
    payload: dict[str, Any] = {}
    truncated = []
    for key, limit in limits.items():
        text = " ".join(diagnostic_summary(str(report.get(key, ""))).split())
        payload[key] = text if len(text) <= limit else text[:limit - 1] + "…"
        if len(text) > limit:
            truncated.append(key)
    actions = report.get("attempted_actions", [])
    payload["attempted_actions"] = []
    if len(actions) > 4:
        truncated.append("attempted_actions")
    for action in actions[:4]:
        text = " ".join(diagnostic_summary(str(action)).split())
        payload["attempted_actions"].append(text if len(text) <= 120 else text[:119] + "…")
        if len(text) > 120 and "attempted_actions" not in truncated:
            truncated.append("attempted_actions")
    payload["safe_to_retry"] = report.get("safe_to_retry", False)
    payload["requires_developer_action"] = report.get("requires_developer_action", True)
    payload["truncated_fields"] = truncated
    return payload


def register_with_core(core, runtime: FailureRuntime) -> ModuleHandle:
    from pal.failure.handler import FailureEventHandler
    from pal.shared import EventKind

    provider = FailureIntrospectionProvider(runtime=runtime)
    event_handler = FailureEventHandler(core=core)
    handle = ModuleHandle(
        module_id="failure",
        tier=MODULE_TIER_CORE_FOUNDATION,
        detachable=False,
        introspection_provider=provider,
        event_handlers={EventKind.REPLY_FAILED: [event_handler]},
        ports={"failure": runtime},
    )
    core.context.register_module(handle)
    core.context.event_handler_registry.register(EventKind.REPLY_FAILED, event_handler, module_id="failure")
    return handle
