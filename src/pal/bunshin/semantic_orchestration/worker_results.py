from __future__ import annotations
from pal.bunshin.failure_diagnostics import append_failure_diagnostic
from pal.foundation.diagnostics import diagnostic_text
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def _primary_json_output(terminal: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(terminal.get("payload") or {})
    artifacts = [dict(item) for item in list(payload.get("artifacts") or []) if isinstance(item, Mapping)]
    primary = dict(payload.get("primary_artifact") or {})
    if not primary:
        primary = next((item for item in artifacts if str(item.get("role") or "") == "primary"), {})
    path = Path(str(primary.get("path") or ""))
    if not path.is_file():
        raise ValueError("semantic worker did not produce a readable primary artifact")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("semantic worker primary artifact must be a JSON object")
    return value


def _named_json_output(terminal: Mapping[str, Any], filename: str) -> dict[str, Any]:
    payload = dict(terminal.get("payload") or {})
    artifacts = [dict(item) for item in list(payload.get("artifacts") or []) if isinstance(item, Mapping)]
    artifact = next(
        (
            item
            for item in artifacts
            if filename
            in {
                Path(str(item.get("relative_path") or "")).name,
                Path(str(item.get("path") or "")).name,
            }
        ),
        None,
    )
    if artifact is None:
        raise ValueError(f"semantic worker did not produce {filename}")
    path = Path(str(artifact.get("path") or ""))
    if not path.is_file():
        raise ValueError(f"semantic worker produced unreadable {filename}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"semantic worker output {filename} must be a JSON object")
    return value


def _terminal_nonretryable_blocker(payload: Mapping[str, Any]) -> str:
    kind = str(payload.get("blocker_kind") or "")
    return kind if kind in {
        "completion_gate_stalled",
        "output_length_recovery_exhausted",
    } else ""


def _worker_stderr_failures(stderr: str) -> tuple[list[Mapping[str, Any]], str]:
    """Recover required messages whose stdout write failed in the worker."""
    events: list[Mapping[str, Any]] = []
    worker_error = ""
    for line in stderr.splitlines():
        if not line.startswith('{"kind": "worker_event_fallback",'):
            continue
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            continue
        if not isinstance(value, dict) or value.get("kind") != "worker_event_fallback":
            continue
        message = value.get("message")
        if not isinstance(message, dict):
            continue
        if message.get("delivery_error"):
            worker_error = "\n".join(filter(None, (
                worker_error, "Worker event delivery failed: " + str(message["delivery_error"]),
            )))
        if message.get("kind") == "worker_error":
            error = append_failure_diagnostic(
                str(message.get("error") or ""), message.get("failure_diagnostic"),
            )
            worker_error = "\n".join(filter(None, (worker_error, error)))
        elif message.get("kind") == "event":
            event = message.get("event")
            if isinstance(event, dict) and event.get("event_kind") == "terminal":
                events.append(event)
    return events, diagnostic_text(worker_error, limit=None)


def _terminal_failure_details(payload: Mapping[str, Any]) -> str:
    parts = []
    for key in ("details", "summary", "error"):
        value = str(payload.get(key) or "").strip()
        if value and not any(value in part for part in parts):
            parts.append(value)
    details = append_failure_diagnostic("\n".join(parts), payload.get("failure_diagnostic"))
    for key in ("cleanup_error", "process_error", "diagnostics"):
        value = payload.get(key)
        if value:
            details += f"\n{key}: " + json.dumps(value, ensure_ascii=False, default=str)
    return diagnostic_text(details, limit=None)


def _worker_terminal_failure(
    events: list[Mapping[str, Any]],
) -> tuple[str, str, str]:
    """Return the structured failure emitted by the worker, when present."""

    terminal = next(
        (
            item
            for item in reversed(events)
            if str(item.get("event_kind") or "") == "terminal"
        ),
        None,
    )
    if terminal is None:
        return "", "", ""
    payload = dict(terminal.get("payload") or {})
    status = str(payload.get("status") or "")
    blocker = _terminal_nonretryable_blocker(payload)
    if status == "blocked" and blocker:
        # A cleanup failure can turn a blocked worker's exit code nonzero.
        # Preserve the same permanent blocker classification as the zero-exit
        # terminal-validation path, rather than spending another model attempt.
        return (
            blocker,
            _terminal_failure_details(payload) or blocker.replace("_", " "),
            "do_not_retry",
        )
    if status != "failed":
        return "", "", ""
    error_kind = str(
        payload.get("error_kind")
        or payload.get("error_type")
        or "worker_terminal_failed"
    ).strip()
    details = _terminal_failure_details(payload)
    retry_directive = str(payload.get("retry_directive") or "").strip()
    return error_kind, details, retry_directive


def _worker_event_timing(events: list[Mapping[str, Any]]) -> dict[str, int | float]:
    llm_started: dict[str, datetime] = {}
    tool_started: dict[str, datetime] = {}
    llm_seconds = 0.0
    tool_seconds = 0.0
    input_tokens = 0
    output_tokens = 0
    cost = 0.0
    timestamps: list[datetime] = []
    for event in events:
        created_at = _event_datetime(event.get("created_at"))
        if created_at is None:
            continue
        timestamps.append(created_at)
        if str(event.get("event_kind") or "") != "progress":
            continue
        payload = dict(event.get("payload") or {})
        phase = str(payload.get("phase") or "")
        if phase == "llm_round_started":
            llm_started[str(payload.get("round") or len(llm_started) + 1)] = created_at
        elif phase == "llm_round_completed":
            started = llm_started.pop(str(payload.get("round") or ""), None)
            if started is not None:
                llm_seconds += max(0.0, (created_at - started).total_seconds())
            input_tokens += max(0, int(payload.get("input_tokens") or 0))
            output_tokens += max(0, int(payload.get("output_tokens") or 0))
            cost += max(0.0, float(payload.get("cost") or 0.0))
        elif phase == "tool_call_started":
            key = f"{payload.get('round')}:{payload.get('tool_call_index')}"
            tool_started[key] = created_at
        elif phase in {"tool_call_completed", "tool_call_failed"}:
            key = f"{payload.get('round')}:{payload.get('tool_call_index')}"
            started = tool_started.pop(key, None)
            if started is not None:
                tool_seconds += max(0.0, (created_at - started).total_seconds())
    wall_seconds = max(0.0, (max(timestamps) - min(timestamps)).total_seconds()) if timestamps else 0.0
    return {
        "llm_time_ms": int(llm_seconds * 1000),
        "tool_time_ms": int(tool_seconds * 1000),
        "worker_time_ms": int(wall_seconds * 1000),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost": cost,
    }


def _recorded_role_metrics(terminal: Mapping[str, Any]) -> dict[str, int | float]:
    timing = dict(dict(terminal.get("payload") or {}).get("v2_timing") or {})
    return {
        "input_tokens": max(0, int(timing.get("input_tokens") or 0)),
        "output_tokens": max(0, int(timing.get("output_tokens") or 0)),
        "cost": max(0.0, float(timing.get("cost") or 0.0)),
        "latency_ms": max(0, int(timing.get("llm_time_ms") or 0)),
        "tool_latency_ms": max(0, int(timing.get("tool_time_ms") or 0)),
        "wall_latency_ms": max(0, int(timing.get("worker_time_ms") or 0)),
    }


def _role_session_turn_index(terminal: Mapping[str, Any]) -> int:
    payload = dict(terminal.get("payload") or {})
    return max(1, int(payload.get("session_turn_index") or 1))


def _event_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
