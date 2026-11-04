"""Failure owner projections for model recovery; Core only transports them."""
from __future__ import annotations

import json
from typing import Any

from pal.failure.contracts import FailureDraft
from pal.foundation.diagnostics import diagnostic_reason, diagnostic_text, public_diagnostics


def _failure_draft_debug_payload(draft: FailureDraft) -> dict[str, Any]:
    return {
        "subsystem": draft.subsystem,
        "component": draft.component,
        "failure_kind": draft.failure_kind,
        "severity": draft.severity,
        "primary_blocker": diagnostic_reason(draft.primary_blocker, limit=None),
        "secondary_issues": [diagnostic_reason(issue, limit=None) for issue in draft.secondary_issues],
        "attempted_actions": list(draft.attempted_actions),
        "documents_checked": list(draft.documents_checked),
        "maintenance_outcomes": [_project_maintenance_outcome(item) for item in list(draft.maintenance_outcomes)],
        "repair_domain": draft.repair_domain,
        "evidence": _project_failure_evidence(draft.evidence),
        "related_ids": dict(draft.related_ids),
        "safe_to_retry": draft.safe_to_retry,
    }


def _render_failure_primary_input(
    draft: FailureDraft,
    *,
    allowed_tools: list[dict[str, Any]],
    observations: list[Any],
    stage: str,
) -> str:
    allowed_names = []
    for tool in allowed_tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = str((function or {}).get("name") or "").strip()
        if name:
            allowed_names.append(name)
    failure = _failure_draft_debug_payload(draft)
    recorded_calls = {item["call_id"] for item in failure["maintenance_outcomes"] if item["call_id"]}
    payload = {
        "stage": stage,
        "failure": failure,
        "allowed_capabilities": allowed_names,
        "recent_observations": [
            {
                "tool_name": item.tool_name,
                "call_id": item.call_id,
                "ok": item.ok,
                "summary": diagnostic_text(item.summary, limit=None),
                "structured_summary": _project_tool_structured_summary(item.structured),
            }
            for item in observations
            if not item.call_id or item.call_id not in recorded_calls
        ],
        "instructions": [
            "Work only on the primary blocker in this sanitized failure packet.",
            "Use introspection capabilities first to inspect live Pal runtime state.",
            "Use dedicated built-in operation or management capabilities only when they directly repair the blocker.",
            "Do not directly edit code, config, databases, runtime files, or user data.",
            "Do not restart Pal itself.",
            "Do not answer the user's original request.",
            "When verifying, return a JSON object with verification_status and explanatory fields.",
        ],
    }
    return json.dumps(payload, ensure_ascii=True)


def _project_maintenance_outcome(item: dict[str, Any]) -> dict[str, Any]:
    structured = item.get("structured") if isinstance(item, dict) else None
    return {
        "action_name": str(item.get("action_name") or "").strip(),
        "call_id": str(item.get("call_id") or ""),
        "status": str(item.get("status") or "").strip(),
        "ok": bool(item.get("ok")),
        "text": diagnostic_text(str(item.get("text") or ""), limit=None),
        "structured_summary": _project_tool_structured_summary(structured),
    }


def _project_failure_evidence(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _project_diagnostic_value(value)


def _project_tool_structured_summary(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        return {}
    return _project_diagnostic_value(value)


def _project_diagnostic_value(value: Any) -> Any:
    """Preserve operation facts, with brief causes and redacted credentials.

    A shape-only preview cannot establish recovery: nested causes, per-item
    failures and false/zero status values are all evidence for the verdict.
    """
    return public_diagnostics(value)
