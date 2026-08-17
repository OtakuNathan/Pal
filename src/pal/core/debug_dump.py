from __future__ import annotations

import asyncio
import json
import linecache
import re
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pal.foundation.log_paths import pal_debug_log_path

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "authorization",
    "bot_token",
    "password",
    "secret",
    "source_url",
    "telegram_file_path",
    "token",
)
_BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_TELEGRAM_FILE_RE = re.compile(r"https://api\.telegram\.org/file/bot[^/\s]+", re.IGNORECASE)


def write_runtime_debug_dump(
    handle: Any,
    *,
    app_snapshot: Mapping[str, Any] | None = None,
    path: Path | None = None,
) -> Path:
    dump_path = path or _default_debug_path(handle)
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = build_runtime_debug_snapshot(handle, app_snapshot=app_snapshot)
    with dump_path.open("a", encoding="utf-8") as file_obj:
        file_obj.write("\n")
        file_obj.write("=" * 80)
        file_obj.write("\n")
        file_obj.write(f"PAL DEBUG DUMP {snapshot['created_at_utc']}\n")
        file_obj.write("=" * 80)
        file_obj.write("\n")
        file_obj.write(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True))
        file_obj.write("\n")
    return dump_path


def build_runtime_debug_snapshot(
    handle: Any,
    *,
    app_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    core = getattr(handle, "core", None)
    channel_runtime = getattr(handle, "channel_runtime", None)
    proactive_manager = getattr(handle, "proactive_manager", None)
    proactive_repository = getattr(handle, "proactive_repository", None)
    return {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "monotonic": time.monotonic(),
        "runtime_app": _redact(app_snapshot or {}),
        "asyncio": _asyncio_snapshot(),
        "core": _core_snapshot(core),
        "llm": _llm_snapshot(core),
        "channel": _channel_snapshot(channel_runtime),
        "proactive": _proactive_snapshot(proactive_manager, proactive_repository),
    }


def _default_debug_path(handle: Any) -> Path:
    runtime_root = _runtime_root(handle)
    return pal_debug_log_path(runtime_root)


def _runtime_root(handle: Any) -> Path:
    registration = getattr(handle, "registration", None)
    runtime = getattr(registration, "runtime", None)
    runtime_root = getattr(runtime, "runtime_root", None)
    if runtime_root is None:
        return Path(".")
    return Path(runtime_root)


def _asyncio_snapshot() -> dict[str, Any]:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return {"running_loop": False, "tasks": []}
    current = asyncio.current_task(loop=loop)
    tasks = sorted(asyncio.all_tasks(loop=loop), key=_task_sort_key)
    return {
        "running_loop": True,
        "task_count": len(tasks),
        "tasks": [_task_snapshot(task, current_task=current) for task in tasks],
    }


def _task_sort_key(task: asyncio.Task[Any]) -> tuple[str, str]:
    return (str(task.get_name()), _coro_name(task))


def _task_snapshot(task: asyncio.Task[Any], *, current_task: asyncio.Task[Any] | None) -> dict[str, Any]:
    stack = task.get_stack(limit=12)
    return {
        "name": str(task.get_name()),
        "coro": _coro_name(task),
        "done": task.done(),
        "cancelled": task.cancelled(),
        "current_debug_dump_task": task is current_task,
        "stack": _format_stack(stack),
    }


def _coro_name(task: asyncio.Task[Any]) -> str:
    coro = task.get_coro()
    qualname = getattr(coro, "__qualname__", None)
    if qualname:
        return str(qualname)
    code = getattr(coro, "cr_code", None)
    if code is not None:
        return str(getattr(code, "co_name", ""))
    return type(coro).__name__


def _format_stack(stack: Sequence[Any]) -> list[str]:
    formatted: list[str] = []
    for frame in stack[-12:]:
        filename = frame.f_code.co_filename
        lineno = frame.f_lineno
        function = frame.f_code.co_name
        source = linecache.getline(filename, lineno).strip()
        line = f'{filename}:{lineno} in {function}'
        if source:
            line = f"{line} | {source}"
        formatted.append(line)
    return formatted


def _core_snapshot(core: Any) -> dict[str, Any]:
    if core is None:
        return {"available": False}
    context = getattr(core, "context", None)
    state = getattr(core, "state", None)
    main_loop = getattr(core, "main_loop", None)
    event_sources = getattr(getattr(context, "event_source_registry", None), "sources", {}) or {}
    handlers = getattr(getattr(context, "event_handler_registry", None), "handlers", {}) or {}
    queue = getattr(main_loop, "queue", ())
    return {
        "available": True,
        "main_loop_queue_size": _safe_len(queue),
        "event_sources": sorted(str(key) for key in getattr(event_sources, "keys", lambda: [])()),
        "event_handlers": {str(key): len(value or []) for key, value in getattr(handlers, "items", lambda: [])()},
        "state": _core_state_snapshot(state),
    }


def _llm_snapshot(core: Any) -> dict[str, Any]:
    from pal.foundation.fd_lease import fd_lease_snapshot

    context = getattr(core, "context", None)
    registry = getattr(context, "port_registry", {}) or {}
    runtime = getattr(registry, "get", lambda _key: None)("llm:llm")
    return {
        "available": runtime is not None,
        "active_endpoint_id": str(
            getattr(runtime, "active_endpoint_id", "") or ""
        ),
        "last_endpoint_id": str(getattr(runtime, "last_endpoint_id", "") or ""),
        "detached_stream_task_count": _safe_len(
            getattr(runtime, "_detached_stream_tasks", ())
        ),
        "fd_leases": fd_lease_snapshot(),
    }


def _core_state_snapshot(state: Any) -> dict[str, Any]:
    if state is None:
        return {}
    active_turns = getattr(state, "active_turns", {}) or {}
    turn_tasks = getattr(state, "turn_tasks", {}) or {}
    control_scopes = getattr(state, "control_scopes", {}) or {}
    return {
        "active_turn_count": len(active_turns),
        "active_turns": [_turn_snapshot(turn_id, continuation) for turn_id, continuation in active_turns.items()],
        "active_turn_id": str(getattr(state, "active_turn_id", "") or ""),
        "resident_quiescing": bool(getattr(state, "resident_quiescing", False)),
        "pending_channel_turn_count": _safe_len(getattr(state, "pending_channel_turns", ())),
        "turn_task_count": len(turn_tasks),
        "turn_tasks": [_turn_task_snapshot(turn_id, task) for turn_id, task in turn_tasks.items()],
        "control_scope_count": len(control_scopes),
        "control_scopes": [_control_scope_snapshot(key, value) for key, value in control_scopes.items()],
        "detached_modules": sorted(str(item) for item in (getattr(state, "detached_modules", set()) or set())),
        "diagnostics_count": _safe_len(getattr(state, "diagnostics", ())),
        "prompt_log_enabled": bool(getattr(state, "prompt_log_enabled", False)),
        "mode": str(getattr(state, "mode", "") or ""),
    }


def _turn_snapshot(turn_id: Any, continuation: Any) -> dict[str, Any]:
    return {
        "turn_id": str(turn_id),
        "class": type(continuation).__name__,
        "started": bool(getattr(continuation, "started", False)),
        "waiting_effect_id": str(getattr(continuation, "waiting_effect_id", "") or ""),
        "control_scope_key": str(getattr(continuation, "control_scope_key", "") or ""),
        "interrupted": bool(getattr(continuation, "interrupted", False)),
        "interrupt_reason": str(getattr(continuation, "interrupt_reason", "") or ""),
        "finalization_only": bool(getattr(continuation, "finalization_only", False)),
        "tool_batch_count": int(getattr(continuation, "tool_batch_count", 0) or 0),
        "pending_tool_call_count": _safe_len(getattr(continuation, "pending_tool_call_batch", ())),
        "pending_tool_result_count": _safe_len(getattr(continuation, "pending_tool_results", ())),
        "tool_observation_count": _safe_len(getattr(continuation, "tool_observations", ())),
    }


def _turn_task_snapshot(turn_id: Any, task: Any) -> dict[str, Any]:
    return {
        "turn_id": str(turn_id),
        "task_name": str(task.get_name()) if hasattr(task, "get_name") else "",
        "done": bool(task.done()) if hasattr(task, "done") else False,
        "cancelled": bool(task.cancelled()) if hasattr(task, "cancelled") else False,
    }


def _control_scope_snapshot(key: Any, scope: Any) -> dict[str, Any]:
    return {
        "scope_key": str(key),
        "pending_request_count": _safe_len(getattr(scope, "pending_requests", {})),
    }


def _channel_snapshot(channel_runtime: Any) -> dict[str, Any]:
    if channel_runtime is None:
        return {"available": False}
    endpoints = []
    list_endpoints = getattr(channel_runtime, "list_endpoints", None)
    if callable(list_endpoints):
        endpoints = list(list_endpoints())
    return {
        "available": True,
        "runtime_queues": {
            "mailbox_size": _safe_len(getattr(getattr(channel_runtime, "mailbox", None), "peek_all", lambda: [])()),
            "outbox_size": _safe_len(getattr(channel_runtime, "outbox", ())),
            "attachment_outbox_size": _safe_len(getattr(channel_runtime, "attachment_outbox", ())),
            "status_outbox_size": _safe_len(getattr(channel_runtime, "status_outbox", ())),
            "stream_update_outbox_size": _safe_len(getattr(channel_runtime, "stream_update_outbox", ())),
        },
        "endpoints": [_endpoint_snapshot(endpoint) for endpoint in endpoints],
    }


def _endpoint_snapshot(endpoint: Any) -> dict[str, Any]:
    config = getattr(endpoint, "endpoint", None)
    health = _safe_call(getattr(endpoint, "inspect_health", None), default={})
    backlog = _safe_call(getattr(endpoint, "inspect_backlog", None), default={})
    return {
        "endpoint_id": str(getattr(config, "endpoint_id", "") or ""),
        "channel_kind": str(getattr(config, "channel_kind", "") or ""),
        "class": type(endpoint).__name__,
        "enabled": bool(getattr(endpoint, "enabled", False)),
        "attached": bool(getattr(endpoint, "attached", False)),
        "paired": bool(getattr(endpoint, "paired", False)),
        "mailbox_size": _safe_len(getattr(getattr(endpoint, "mailbox", None), "peek_all", lambda: [])()),
        "outbox_size": _safe_len(getattr(endpoint, "outbox", ())),
        "attachment_outbox_size": _safe_len(getattr(endpoint, "attachment_outbox", ())),
        "status_outbox_size": _safe_len(getattr(endpoint, "status_outbox", ())),
        "stream_update_outbox_size": _safe_len(getattr(endpoint, "stream_update_outbox", ())),
        "health": _redact(health),
        "backlog": _redact(backlog),
        "last_delivery_error": _redact(getattr(endpoint, "last_delivery_error", "")),
    }


def _proactive_snapshot(proactive_manager: Any, proactive_repository: Any) -> dict[str, Any]:
    if proactive_manager is None:
        return {"available": False}
    registered = getattr(proactive_manager, "registered", {}) or {}
    trigger_mailbox = getattr(proactive_manager, "trigger_mailbox", None)
    schedule_engine = getattr(proactive_manager, "schedule_engine", None)
    next_due = getattr(schedule_engine, "next_due_by_proactive_id", {}) or {}
    proactive_tasks = []
    for proactive_id, definition in sorted(registered.items()):
        latest = _safe_call(
            getattr(proactive_repository, "latest_run", None),
            str(proactive_id),
            default=None,
        )
        proactive_tasks.append(
            {
                "proactive_id": str(proactive_id),
                "enabled": bool(getattr(definition, "enabled", False)),
                "next_due_at_utc": _redact(next_due.get(proactive_id)),
                "latest_run": _proactive_run_snapshot(latest),
            }
        )
    return {
        "available": True,
        "registered_count": len(registered),
        "trigger_mailbox_size": _safe_len(getattr(trigger_mailbox, "peek_all", lambda: [])()),
        "proactive_tasks": proactive_tasks,
    }


def _proactive_run_snapshot(run: Any) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "proactive_run_id": str(getattr(run, "proactive_run_id", "") or ""),
        "proactive_id": str(getattr(run, "proactive_id", "") or ""),
        "trigger_kind": str(getattr(run, "trigger_kind", "") or ""),
        "status": str(getattr(run, "status", "") or ""),
        "turn_id": str(getattr(run, "turn_id", "") or ""),
        "started_at": str(getattr(run, "started_at", "") or ""),
        "completed_at": str(getattr(run, "completed_at", "") or ""),
        "error_text": _redact(str(getattr(run, "error_text", "") or "")),
    }


def _safe_call(fn: Any, *args: Any, default: Any = None) -> Any:
    if not callable(fn):
        return default
    try:
        return fn(*args)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {_redact(str(exc))}"}


def _safe_len(value: Any) -> int:
    try:
        return len(value)
    except Exception:
        return 0


def _redact(value: Any, *, _depth: int = 0) -> Any:
    if _depth > 8:
        return "<max-depth>"
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                redacted[key_text] = "<redacted>"
            else:
                redacted[key_text] = _redact(item, _depth=_depth + 1)
        return redacted
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact(item, _depth=_depth + 1) for item in list(value)[:100]]
    if isinstance(value, str):
        text = _TELEGRAM_FILE_RE.sub("https://api.telegram.org/file/bot<redacted>", value)
        text = _BOT_TOKEN_RE.sub("bot<redacted>", text)
        if len(text) > 1000:
            return f"{text[:1000]}...<truncated>"
        return text
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    if len(text) > 1000:
        return f"{text[:1000]}...<truncated>"
    return text


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)
