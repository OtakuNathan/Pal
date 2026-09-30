from __future__ import annotations
import json
from typing import Any


def _progress_summary(phase: str, payload: dict[str, Any]) -> str:
    phase_name = str(phase or "progress").strip() or "progress"
    if phase_name == "llm_round_started":
        return f"LLM round {payload.get('round')} started"
    if phase_name == "llm_round_completed":
        calls = list(payload.get("tool_calls") or [])
        if calls:
            names = ", ".join(str(item.get("target_name") or item.get("tool_name") or "") for item in calls[:4] if isinstance(item, dict))
            extra = "..." if len(calls) > 4 else ""
            return f"LLM round {payload.get('round')} requested tools: {names}{extra}".strip()
        return f"LLM round {payload.get('round')} produced final text"
    if phase_name == "llm_endpoint_attempt_failed":
        endpoint = payload.get("endpoint_id") or payload.get("model_id") or "endpoint"
        return f"LLM endpoint {endpoint} attempt {payload.get('attempt')}/{payload.get('max_attempts')} failed: {payload.get('error_kind') or 'error'}"
    if phase_name == "llm_endpoint_retry_scheduled":
        endpoint = payload.get("endpoint_id") or payload.get("model_id") or "endpoint"
        return f"LLM endpoint {endpoint} retry {payload.get('next_attempt')}/{payload.get('max_attempts')} scheduled"
    if phase_name == "llm_endpoint_exhausted":
        endpoint = payload.get("endpoint_id") or payload.get("model_id") or "endpoint"
        next_endpoint = str(payload.get("next_endpoint_id") or "").strip()
        suffix = f"; falling back to {next_endpoint}" if next_endpoint else ""
        return f"LLM endpoint {endpoint} exhausted after {payload.get('attempt')}/{payload.get('max_attempts')}{suffix}"
    if phase_name == "llm_endpoint_fallback_started":
        endpoint = payload.get("endpoint_id") or payload.get("model_id") or "endpoint"
        return f"LLM fallback started: {endpoint}"
    if phase_name == "llm_endpoint_fallback_succeeded":
        endpoint = payload.get("endpoint_id") or payload.get("model_id") or "endpoint"
        return f"LLM fallback succeeded: {endpoint}"
    if phase_name == "llm_endpoint_skipped":
        endpoint = payload.get("endpoint_id") or payload.get("model_id") or "endpoint"
        return f"LLM endpoint skipped: {endpoint} ({payload.get('reason') or 'skipped'})"
    if phase_name == "tool_call_started":
        return f"Tool started: {payload.get('target_name') or payload.get('tool_name')}"
    if phase_name == "tool_call_completed":
        status = "ok" if bool(payload.get("ok")) else "error"
        return f"Tool completed: {payload.get('target_name') or payload.get('tool_name')} ({status})"
    if phase_name == "tool_call_failed":
        return f"Tool failed: {payload.get('target_name') or payload.get('tool_name')}"
    if phase_name == "invocation_finalizing":
        return "Milestone finalizing"
    return phase_name.replace("_", " ")


def _json_preview(value: Any, *, limit: int = 500) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        text = str(value)
    return _preview_text(text, limit=limit)


def _preview_text(value: Any, *, limit: int = 400) -> str:
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
