from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pal.foundation import utc_now
from pal.foundation.sidecar import (
    dispatch_sidecar_request,
    pack_sidecar_message,
    read_sidecar_message,
)
from pal.minion.git_env import finalize_work_order_branch, prepare_task_workspace
from pal.minion.ipc import cleanup_manager_endpoint, minion_log_path, minion_runner_log_path, start_manager_server
from pal.minion.ipc import python_subprocess_env
from pal.minion.profiles import MinionProfileRegistry
from pal.minion.repository import MinionTaskingRepository
from pal.minion.turns import apply_minion_turn_to_pack, sanitize_runner_session_pack
from pal.minion.work_order import ReviewerWorkOrder, prompt_view_for_reviewer
from pal.shared import MinionApprovalDecision, TaskContextPack


_ACTIVE_RUN_STATUSES = {"starting", "running", "approval_pending", "clarification_pending"}
_TERMINAL_RUN_STATUSES = {"completed", "failed", "blocked", "killed"}


def _runner_stderr_line_is_error(line: str) -> bool:
    text = str(line or "").strip()
    if not text:
        return False
    if text.startswith("[tool call]"):
        return False
    if text.startswith("[tool result]"):
        return " ok=False " in text or " status=error " in text or " status=failed " in text
    lowered = text.lower()
    return (
        text.startswith("Traceback")
        or "traceback (most recent call last)" in lowered
        or lowered.startswith("error:")
        or " error " in lowered
        or "exception" in lowered
    )


def _safe_token(value: str) -> str:
    normalized = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(value or "").strip())
    return normalized.strip("_")[:96] or uuid4().hex[:12]


def _review_scratch_dir(runtime_root: Path, work_order_id: str) -> Path:
    path = Path(runtime_root) / "data" / "minion" / "review_scratch" / _safe_token(work_order_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _prepare_review_scratch(runtime_root: Path, work_order_id: str, *, repo_path: str = "") -> dict[str, str]:
    scratch = _review_scratch_dir(runtime_root, work_order_id)
    payload = {"review_scratch_dir": str(scratch)}
    source = Path(str(repo_path or "")).resolve() if str(repo_path or "").strip() else None
    if source is not None and source.exists() and source.is_dir():
        copy_path = scratch / "source"
        if not copy_path.exists():
            shutil.copytree(
                source,
                copy_path,
                ignore=_review_scratch_ignore,
            )
        payload["review_scratch_repo_path"] = str(copy_path)
    return payload


def _review_scratch_ignore(directory: str, names: list[str]) -> set[str]:
    ignored = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    root = Path(directory)
    for name in names:
        if name in ignored:
            continue
        try:
            if (root / name).is_symlink():
                ignored.add(name)
        except OSError:
            ignored.add(name)
    return ignored.intersection(set(names))


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _review_gate_repair_note(gate: dict[str, Any]) -> str:
    parts = [f"Reviewer verdict: {str(gate.get('verdict') or 'fail')}"]
    summary = str(gate.get("summary") or "").strip()
    if summary:
        parts.append(f"Summary: {summary}")
    findings = [dict(item) for item in list(gate.get("findings") or []) if isinstance(item, dict)]
    fixes = [dict(item) for item in list(gate.get("required_fixes") or []) if isinstance(item, dict)]
    if findings:
        rendered = []
        for item in findings[:8]:
            rendered.append(
                "- "
                + str(item.get("severity") or "finding")
                + ": "
                + str(item.get("summary") or item.get("message") or item)
            )
        parts.append("Findings:\n" + "\n".join(rendered))
    if fixes:
        rendered = ["- " + str(item.get("summary") or item.get("fix") or item) for item in fixes[:8]]
        parts.append("Required fixes:\n" + "\n".join(rendered))
    parts.append("After repairing, rerun relevant verification and call op_minion_checkpoint_commit again.")
    return "\n\n".join(parts)


def _review_gate_target(gate: dict[str, Any]) -> dict[str, Any]:
    target = gate.get("target")
    return dict(target or {}) if isinstance(target, dict) else {}


def _plan_review_key(plan_ref: dict[str, Any]) -> str:
    plan_id = str(plan_ref.get("plan_id") or "").strip()
    task_id = str(plan_ref.get("task_id") or "").strip()
    revision = str(plan_ref.get("plan_revision") if plan_ref.get("plan_revision") is not None else "").strip()
    sha = str(plan_ref.get("sha256") or "").strip()
    path = str(plan_ref.get("path") or "").strip()
    return ":".join(part for part in (task_id, plan_id, revision, sha or path) if part)


@dataclass
class MinionRunState:
    minion_id: str
    run_id: str
    pack: TaskContextPack
    process: asyncio.subprocess.Process | None = None
    status: str = "starting"
    started_at: str = field(default_factory=utc_now)
    ended_at: str = ""
    last_error: str = ""
    last_event: dict[str, Any] = field(default_factory=dict)
    last_event_at: str = ""
    last_phase: str = ""
    last_tool_call: dict[str, Any] = field(default_factory=dict)
    llm_round_count: int = 0
    tool_call_count: int = 0
    pending_approval: dict[str, Any] = field(default_factory=dict)
    pending_clarification: dict[str, Any] = field(default_factory=dict)
    ledger: list[dict[str, Any]] = field(default_factory=list)
    stderr_tail: list[str] = field(default_factory=list)
    stdout_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    wait_task: asyncio.Task[None] | None = None
    manager_liveness_write_fd: int | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "minion_id": self.minion_id,
            "run_id": self.run_id,
            "work_order_id": self.pack.work_order_id,
            "minion_profile": self.pack.minion_profile,
            "profile_display_name": str((self.pack.resolved_profile or {}).get("display_name") or self.pack.minion_profile),
            "status": self.status,
            "pid": self.process.pid if self.process is not None else None,
            "returncode": self.process.returncode if self.process is not None else None,
            "instruction": self.pack.instruction,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "last_error": self.last_error,
            "last_event_at": self.last_event_at,
            "last_phase": self.last_phase,
            "last_tool_call": dict(self.last_tool_call),
            "llm_round_count": self.llm_round_count,
            "tool_call_count": self.tool_call_count,
            "debug_log_path": _debug_log_path_from_pack(self.pack),
            "current_milestone": dict((self.pack.continuity or {}).get("current_milestone") or {}),
            "stderr_tail": list(self.stderr_tail[-20:]),
            "last_event": dict(self.last_event),
            "pending_approval": dict(self.pending_approval),
            "pending_clarification": dict(self.pending_clarification),
        }

    def detail(self) -> dict[str, Any]:
        payload = self.summary()
        payload["task_context_pack"] = self.pack.to_dict()
        payload["ledger"] = list(self.ledger[-100:])
        return payload


@dataclass
class MinionManager:
    runtime_root: Path
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("pal.minion.manager"))
    tasking_repository: MinionTaskingRepository = field(init=False)
    server: asyncio.base_events.Server | None = None
    endpoint_info: dict[str, Any] = field(default_factory=dict)
    runs: dict[str, MinionRunState] = field(default_factory=dict)
    started_at: str = field(default_factory=utc_now)
    event_queue: list[dict[str, Any]] = field(default_factory=list)
    event_subscribers: list[asyncio.StreamWriter] = field(default_factory=list)
    _shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _serial_turns_inflight: set[str] = field(default_factory=set)
    _checkpoint_reviews_inflight: set[str] = field(default_factory=set)
    _plan_reviews_inflight: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.tasking_repository = MinionTaskingRepository(runtime_root=self.runtime_root)
        self.tasking_repository.ensure_schema()

    async def run(self) -> None:
        self.tasking_repository.recover_stale_running_modules(
            active_child_work_order_ids=set(),
            reason="manager startup recovered stale running module",
        )
        self.server, self.endpoint_info = await start_manager_server(self.runtime_root, self._handle_client)
        self.logger.info("minion manager listening: %s", self.endpoint_info)
        remove_signal_handlers = self._install_signal_handlers()
        async with self.server:
            serve_task = asyncio.create_task(self.server.serve_forever(), name="minion-manager-serve")
            try:
                await self._shutdown_event.wait()
            finally:
                remove_signal_handlers()
                serve_task.cancel()
                self.server.close()
                await self.server.wait_closed()
                with contextlib.suppress(asyncio.CancelledError):
                    await serve_task
                await self.close_all()
                await self._close_event_subscribers()
                await cleanup_manager_endpoint(self.runtime_root)
                self.logger.info("minion manager stopped")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    request = await read_sidecar_message(reader)
                except asyncio.IncompleteReadError:
                    return
                if str(request.get("method") or "") == "subscribe_events":
                    await self._handle_event_subscription(request, reader, writer)
                    return
                response = await self._dispatch(request)
                writer.write(pack_sidecar_message(response))
                await writer.drain()
        except (ConnectionError, OSError, ValueError):
            return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        return await dispatch_sidecar_request(request, self._call_method, error_kind=lambda exc: "manager", logger=self.logger)

    async def _handle_event_subscription(self, request: dict[str, Any], reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_id = str(request.get("id") or "")
        backlog = list(self.event_queue)
        self.event_queue.clear()
        self.event_subscribers.append(writer)
        try:
            writer.write(
                pack_sidecar_message(
                    {
                        "type": "response",
                        "id": request_id,
                        "ok": True,
                        "result": {"subscribed": True, "backlog_count": len(backlog)},
                    }
                )
            )
            for event in backlog:
                writer.write(pack_sidecar_message({"type": "event", "event": event}))
            await writer.drain()
            while not self._shutdown_event.is_set():
                try:
                    message = await read_sidecar_message(reader)
                except asyncio.IncompleteReadError:
                    return
                if str(message.get("method") or "") == "unsubscribe_events":
                    return
        finally:
            with contextlib.suppress(ValueError):
                self.event_subscribers.remove(writer)

    async def _call_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._reconcile_runs()
        if method == "health":
            return self.health()
        if method == "list_runs":
            return {"items": [state.summary() for state in sorted(self.runs.values(), key=lambda item: item.started_at)]}
        if method == "read_run":
            return self.read_run(str(params.get("run_id") or ""))
        if method == "spawn":
            return await self.spawn(dict(params.get("task_context_pack") or {}))
        if method == "kill":
            return await self.kill(str(params.get("run_id") or ""), str(params.get("reason") or ""))
        if method == "send_decision":
            return await self.send_decision(dict(params.get("decision") or {}))
        if method == "send_clarification":
            return await self.send_clarification(dict(params.get("clarification") or {}))
        if method == "finalize_work_order":
            return self.finalize_work_order(dict(params))
        if method == "continue_work_order":
            return await self.continue_work_order(str(params.get("work_order_id") or ""))
        if method == "recover_work_order":
            return self.recover_work_order(str(params.get("work_order_id") or ""), str(params.get("reason") or ""))
        if method == "destroy_work_order_run":
            return await self.destroy_work_order_run(str(params.get("work_order_id") or ""), str(params.get("reason") or ""))
        if method == "pause_work_order":
            return self.pause_work_order(str(params.get("work_order_id") or ""), str(params.get("reason") or ""))
        if method == "finish_work_order":
            return self.finish_work_order(str(params.get("work_order_id") or ""), str(params.get("reason") or ""))
        if method == "shutdown":
            self._shutdown_event.set()
            return {"ok": True}
        raise ValueError(f"unknown minion manager method: {method}")

    def health(self) -> dict[str, Any]:
        active = [state for state in self.runs.values() if state.status in _ACTIVE_RUN_STATUSES]
        return {
            "ok": True,
            "started_at": self.started_at,
            "run_count": len(self.runs),
            "active_count": len(active),
            "pending_event_count": len(self.event_queue),
            "event_subscriber_count": len(self.event_subscribers),
            "log_path": str(minion_log_path(self.runtime_root)),
            **dict(self.endpoint_info),
        }

    def read_run(self, run_id: str) -> dict[str, Any]:
        state = self._require_run(run_id)
        detail = state.detail()
        detail["work_order_snapshot"] = self.tasking_repository.read_work_order(
            state.pack.work_order_id,
            active_runs=[item.summary() for item in self.runs.values()],
        )
        return detail

    def _with_default_coder_checkpoint_review(self, pack: TaskContextPack) -> tuple[TaskContextPack, dict[str, Any]]:
        metadata = dict(pack.metadata or {})
        module_execution = dict(metadata.get("module_execution") or {})
        if module_execution.get("checkpoint_review") is not None or metadata.get("checkpoint_review") is not None:
            return pack, {}
        if not _is_coder_pack(pack):
            return pack, {}
        completion_policy = dict((pack.workspace or {}).get("completion_policy") or {})
        if str(completion_policy.get("evidence") or "").strip().lower() != "git_commit":
            return pack, {}
        checkpoint_review = {
            "enabled": True,
            "reviewer_profile": "software_engineering.reviewer",
            "max_repair_attempts": 5,
            "scope": "bare_coder_checkpoint",
        }
        metadata["checkpoint_review"] = checkpoint_review
        updates: dict[str, Any] = {"checkpoint_review": checkpoint_review}
        if metadata.get("manager_turn_timeout_seconds") is None:
            metadata["manager_turn_timeout_seconds"] = 300
            updates["manager_turn_timeout_seconds"] = 300
        return TaskContextPack.from_dict({**pack.to_dict(), "metadata": metadata}), updates

    async def spawn(self, pack_payload: dict[str, Any]) -> dict[str, Any]:
        pack = TaskContextPack.from_dict(pack_payload)
        if not pack.resolved_profile:
            pack = MinionProfileRegistry(runtime_root=self.runtime_root).resolve_pack(pack)
        pack = self.tasking_repository.prepare_pack_for_spawn(pack)
        if _is_plan_parent_pack(pack):
            continued = await self.continue_work_order(pack.work_order_id)
            return {
                "work_order_id": pack.work_order_id,
                "minion_profile": pack.minion_profile,
                "status": str(continued.get("status") or "ok"),
                "plan_parent": True,
                "continuation": continued,
            }
        pack = self._inject_skill_manual_context(pack)
        minion_id = f"minion_{uuid4().hex[:10]}"
        run_id = f"run_{uuid4().hex[:12]}"
        metadata = dict(pack.metadata)
        metadata["run_id"] = run_id
        metadata["minion_id"] = minion_id
        pack = TaskContextPack.from_dict({**pack.to_dict(), "metadata": metadata})
        pack = prepare_task_workspace(self.runtime_root, pack, run_id=run_id)
        pack, metadata_updates = self._with_default_coder_checkpoint_review(pack)
        if metadata_updates:
            self.tasking_repository.merge_work_order_metadata(pack.work_order_id, metadata_updates)
        self.tasking_repository.update_work_order_workspace(pack.work_order_id, dict(pack.workspace))
        pack = self._with_runner_debug_log(pack)
        pack = sanitize_runner_session_pack(pack)
        state = MinionRunState(minion_id=minion_id, run_id=run_id, pack=pack)
        async with self._lock:
            self.runs[run_id] = state
        await self._start_runner(state)
        self._record_event(
            state,
            {
                "event_kind": "phase_started",
                "payload": {"phase": "spawned", "summary": "minion runner spawned"},
                "created_at": utc_now(),
            },
        )
        return state.summary()

    async def kill(self, run_id: str, reason: str = "") -> dict[str, Any]:
        state = self._require_run(run_id)
        self._close_liveness_pipe(state)
        process = state.process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        state.status = "killed"
        state.ended_at = utc_now()
        self._record_event(
            state,
            {
                "event_kind": "terminal",
                "payload": {"status": "killed", "summary": reason or "minion killed"},
                "created_at": utc_now(),
            },
        )
        return state.summary()

    def recover_work_order(self, work_order_id: str = "", reason: str = "") -> dict[str, Any]:
        self._reconcile_runs()
        return self.tasking_repository.recover_stale_running_modules(
            active_child_work_order_ids=self._active_runner_work_order_ids(),
            work_order_id=str(work_order_id or ""),
            reason=reason or "manager recovered stale running module",
        )

    async def destroy_work_order_run(self, work_order_id: str, reason: str = "") -> dict[str, Any]:
        normalized = str(work_order_id or "").strip()
        if not normalized:
            raise ValueError("work_order_id is required")
        self._reconcile_runs()
        target_work_order_id = normalized
        snapshot = self.tasking_repository.read_work_order(normalized)
        if snapshot.get("status") == "ok":
            metadata = dict((snapshot.get("work_order") or {}).get("metadata") or {})
            plan_execution = dict(metadata.get("plan_execution") or {})
            if str(plan_execution.get("status") or "").strip().lower() == "running_module":
                target_work_order_id = str(plan_execution.get("active_child_work_order_id") or normalized).strip() or normalized
        state = self._find_active_run_for_work_order(target_work_order_id) or self._find_active_run_for_work_order(normalized)
        killed: dict[str, Any] | None = None
        if state is not None:
            killed = await self.kill(state.run_id, reason or "destroy work order run")
        released = self.tasking_repository.release_running_module_parent(
            normalized,
            child_terminal_status="killed",
            reason=reason or "destroyed active work order run",
        )
        if str(released.get("status") or "") == "not_found" and target_work_order_id != normalized:
            released = self.tasking_repository.release_running_module_parent(
                target_work_order_id,
                child_terminal_status="killed",
                reason=reason or "destroyed active work order run",
            )
        return {
            "status": "destroyed" if killed or str(released.get("status") or "") == "released" else str(released.get("status") or "not_found"),
            "work_order_id": normalized,
            "target_work_order_id": target_work_order_id,
            "killed_run": killed or {},
            "release": released,
        }

    async def send_decision(self, payload: dict[str, Any]) -> dict[str, Any]:
        decision = MinionApprovalDecision.from_dict(payload)
        state = self._find_run_for_decision(decision)
        if state is None:
            raise KeyError(f"unknown approval target: {decision.approval_id}")
        if state.process is None or state.process.stdin is None or state.process.returncode is not None:
            raise RuntimeError(f"minion is not accepting decisions: {state.run_id}")
        state.process.stdin.write(pack_sidecar_message({"type": "decision", "decision": decision.to_dict()}))
        await state.process.stdin.drain()
        state.pending_approval = {}
        self._record_event(
            state,
            {
                "event_kind": "decision_received",
                "payload": decision.to_dict(),
                "created_at": utc_now(),
            },
        )
        return {"ok": True, "run": state.summary(), "decision": decision.to_dict()}

    async def send_clarification(self, payload: dict[str, Any]) -> dict[str, Any]:
        clarification_id = str(payload.get("clarification_id") or "").strip()
        state = self._find_run_for_clarification(payload)
        if state is None:
            raise KeyError(f"unknown clarification target: {clarification_id or payload.get('run_id') or ''}")
        if state.process is None or state.process.stdin is None or state.process.returncode is not None:
            raise RuntimeError(f"minion is not accepting clarifications: {state.run_id}")
        response = dict(payload)
        response.setdefault("run_id", state.run_id)
        response.setdefault("minion_id", state.minion_id)
        response.setdefault("work_order_id", state.pack.work_order_id)
        state.process.stdin.write(pack_sidecar_message({"type": "clarification", "clarification": response}))
        await state.process.stdin.drain()
        state.pending_clarification = {}
        state.status = "running"
        self._record_event(
            state,
            {
                "event_kind": "clarification_received",
                "payload": response,
                "created_at": utc_now(),
            },
        )
        for answer in list(response.get("answers") or []):
            if isinstance(answer, dict):
                with contextlib.suppress(Exception):
                    self.tasking_repository.record_clarification_answer(state.pack.work_order_id, dict(answer))
        return {"ok": True, "run": state.summary(), "clarification": response}

    async def _send_runner_control(self, state: MinionRunState, message: dict[str, Any]) -> dict[str, Any]:
        if state.process is None or state.process.stdin is None or state.process.returncode is not None:
            raise RuntimeError(f"minion is not accepting manager control messages: {state.run_id}")
        state.process.stdin.write(pack_sidecar_message(dict(message)))
        await state.process.stdin.drain()
        return {"ok": True, "run_id": state.run_id, "message_type": str(message.get("type") or "")}

    def finalize_work_order(self, params: dict[str, Any]) -> dict[str, Any]:
        work_order_id = str(params.get("work_order_id") or "").strip()
        if not work_order_id:
            raise ValueError("work_order_id is required")
        snapshot = self.tasking_repository.read_work_order(work_order_id)
        if snapshot.get("status") == "not_found":
            raise KeyError(f"unknown work order: {work_order_id}")
        metadata = dict((snapshot.get("work_order") or {}).get("metadata") or {})
        workspace = dict(metadata.get("workspace") or {})
        repo_path = Path(str(params.get("repo_path") or workspace.get("repo_path") or ""))
        branch = str(params.get("work_order_branch") or workspace.get("work_order_branch") or "").strip()
        target = str(params.get("merge_target") or workspace.get("merge_target") or "").strip()
        message = str(params.get("message") or f"minion({work_order_id}): finalize work order")
        result = finalize_work_order_branch(repo_path, work_order_branch=branch, merge_target=target, message=message)
        event = {
            "event_kind": "finalized",
            "minion_id": "",
            "run_id": "",
            "work_order_id": work_order_id,
            "minion_profile": "",
            "payload": result,
            "created_at": utc_now(),
        }
        self._queue_event_delivery(event)
        self.tasking_repository.record_minion_event(event)
        return {"status": "ok" if result.get("status") in {"committed", "no_changes"} else "error", "work_order_id": work_order_id, **result}

    async def continue_work_order(self, work_order_id: str) -> dict[str, Any]:
        normalized = str(work_order_id or "").strip()
        if not normalized:
            raise ValueError("work_order_id is required")
        pack = self.tasking_repository.next_plan_module_pack(normalized, allow_paused=True)
        if pack is None:
            snapshot = self.tasking_repository.read_work_order(normalized)
            metadata = dict((snapshot.get("work_order") or {}).get("metadata") or {}) if snapshot.get("status") == "ok" else {}
            plan_execution = dict(metadata.get("plan_execution") or {})
            status = str(plan_execution.get("status") or snapshot.get("status") or "not_available")
            active_child_work_order_id = str(plan_execution.get("active_child_work_order_id") or "").strip()
            return {
                "status": status,
                "work_order_id": normalized,
                "reason": "active_child_running" if status == "running_module" and active_child_work_order_id else "no_next_module",
                "active_child_work_order_id": active_child_work_order_id,
            }
        if not pack.resolved_profile:
            pack = MinionProfileRegistry(runtime_root=self.runtime_root).resolve_pack(pack)
        run = await self.spawn(pack.to_dict())
        return {
            "status": "running_module",
            "work_order_id": normalized,
            "child_work_order_id": pack.work_order_id,
            "module_id": str(pack.metadata.get("module_id") or pack.metadata.get("parent_module_id") or ""),
            "run": run,
        }

    def pause_work_order(self, work_order_id: str, reason: str = "") -> dict[str, Any]:
        return self.tasking_repository.set_plan_parent_status(work_order_id, "paused", reason=reason)

    def finish_work_order(self, work_order_id: str, reason: str = "") -> dict[str, Any]:
        return self.tasking_repository.set_plan_parent_status(work_order_id, "completed", reason=reason)

    def _inject_skill_manual_context(self, pack: TaskContextPack) -> TaskContextPack:
        skill_sources = _skill_ref_sources_for_pack(pack)
        skill_refs = _dedupe_strings(
            [
                *skill_sources["pack_allowed_skill_refs"],
                *skill_sources["profile_skill_refs"],
                *skill_sources["work_order_skill_refs"],
                *skill_sources["spawn_bonus_skill_refs"],
            ]
        )
        if not skill_refs:
            return pack
        metadata = dict(pack.metadata)
        existing = _coerce_skill_manual_context(metadata.get("skill_manual_context"))
        existing_ids = {str(item.get("skill_id") or "").strip() for item in existing}
        loaded, unresolved = self._load_skill_manual_context(
            [skill_id for skill_id in skill_refs if skill_id not in existing_ids],
        )
        metadata["profile_skill_refs"] = skill_sources["profile_skill_refs"]
        metadata["pack_allowed_skill_refs"] = skill_sources["pack_allowed_skill_refs"]
        metadata["work_order_skill_refs"] = skill_sources["work_order_skill_refs"]
        metadata["spawn_bonus_skill_refs"] = skill_sources["spawn_bonus_skill_refs"]
        if unresolved:
            metadata["unresolved_skill_refs"] = unresolved
        if not existing and not loaded:
            return TaskContextPack.from_dict({**pack.to_dict(), "metadata": metadata})
        context_items = [*existing, *loaded]
        metadata["skill_manual_context"] = context_items
        metadata["injected_skill_refs"] = [str(item.get("skill_id") or "") for item in context_items if str(item.get("skill_id") or "")]
        return TaskContextPack.from_dict({**pack.to_dict(), "metadata": metadata})

    def _load_skill_manual_context(self, skill_refs: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
        if not skill_refs:
            return [], list(skill_refs)
        db_path = self.runtime_root / "pal.sqlite3"
        if not db_path.exists():
            return [], list(skill_refs)
        result: list[dict[str, Any]] = []
        unresolved: list[str] = []
        try:
            with sqlite3.connect(str(db_path)) as db:
                db.row_factory = sqlite3.Row
                for skill_id in skill_refs:
                    row = db.execute(
                        """
                        SELECT skill_id, title, summary, manual_text, capability_refs_blob, metadata_blob, enabled
                        FROM behavior_skills
                        WHERE skill_id = ?
                        """,
                        (skill_id,),
                    ).fetchone()
                    if row is None or not bool(row["enabled"]):
                        unresolved.append(skill_id)
                        continue
                    metadata = _loads_json_object(row["metadata_blob"])
                    if str(metadata.get("status") or "active").strip() != "active":
                        unresolved.append(skill_id)
                        continue
                    manual_text = str(row["manual_text"] or "").strip()
                    if not manual_text:
                        unresolved.append(skill_id)
                        continue
                    result.append(
                        {
                            "skill_id": str(row["skill_id"] or ""),
                            "title": str(row["title"] or ""),
                            "summary": str(row["summary"] or ""),
                            "manual_text": manual_text,
                            "use_when": str(metadata.get("use_when") or ""),
                            "avoid_when": str(metadata.get("avoid_when") or ""),
                            "capability_refs": _loads_json_list(row["capability_refs_blob"]),
                        }
                    )
        except sqlite3.Error:
            return [], list(skill_refs)
        return result, unresolved

    async def close_all(self) -> None:
        for state in list(self.runs.values()):
            self._close_liveness_pipe(state)
            if state.process is not None and state.process.returncode is None:
                with contextlib.suppress(Exception):
                    state.process.terminate()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(state.process.wait(), timeout=1.0)
                if state.process.returncode is None:
                    with contextlib.suppress(Exception):
                        state.process.kill()
                    with contextlib.suppress(Exception):
                        await state.process.wait()
            with contextlib.suppress(Exception):
                await self._wait_for_stream_tasks(state)
            if state.status in _ACTIVE_RUN_STATUSES:
                self._record_event(
                    state,
                    {
                        "event_kind": "terminal",
                        "payload": {"status": "killed", "summary": "minion manager shutdown", "reason": "manager_shutdown"},
                        "created_at": utc_now(),
                    },
                )

    async def _start_runner(self, state: MinionRunState) -> None:
        log_dir = self.runtime_root / "data" / "minion" / "runs"
        log_dir.mkdir(parents=True, exist_ok=True)
        read_fd, write_fd = os.pipe()
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "pal.minion.runner_main",
                "--runtime-root",
                str(self.runtime_root),
                "--task-json",
                state.pack.to_json(),
                "--minion-id",
                state.minion_id,
                "--run-id",
                state.run_id,
                "--manager-liveness-fd",
                str(read_fd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=python_subprocess_env(),
                pass_fds=(read_fd,),
            )
        except Exception:
            with contextlib.suppress(OSError):
                os.close(read_fd)
            with contextlib.suppress(OSError):
                os.close(write_fd)
            raise
        with contextlib.suppress(OSError):
            os.close(read_fd)
        state.process = process
        state.manager_liveness_write_fd = write_fd
        state.status = "running"
        state.stdout_task = asyncio.create_task(self._read_runner_stdout(state), name=f"minion-stdout-{state.run_id}")
        state.stderr_task = asyncio.create_task(self._read_runner_stderr(state), name=f"minion-stderr-{state.run_id}")
        state.wait_task = asyncio.create_task(self._wait_runner(state), name=f"minion-wait-{state.run_id}")

    def _with_runner_debug_log(self, pack: TaskContextPack) -> TaskContextPack:
        if not _debug_log_requested(pack):
            return pack
        log_path = minion_runner_log_path(self.runtime_root, pack.work_order_id, pack.minion_profile)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        metadata = dict(pack.metadata)
        metadata["debug_log"] = {
            "enabled": True,
            "path": str(log_path),
            "filename": log_path.name,
            "mode": "append",
            "managed_by": "minion.manager",
        }
        return TaskContextPack.from_dict({**pack.to_dict(), "metadata": metadata})

    async def _read_runner_stdout(self, state: MinionRunState) -> None:
        process = state.process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                payload = await read_sidecar_message(process.stdout)
                if str(payload.get("type") or "") != "event":
                    continue
                self._record_event(state, payload)
        except asyncio.IncompleteReadError:
            return
        except Exception as exc:
            state.last_error = f"{exc.__class__.__name__}: {exc}"
            self.logger.exception("failed to read minion stdout: %s", state.run_id)

    async def _read_runner_stderr(self, state: MinionRunState) -> None:
        process = state.process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    return
                line_text = line.decode("utf-8", errors="replace").rstrip()
                self._record_runner_stderr_line(state, line_text)
                self.logger.info("minion %s stderr: %s", state.run_id, line_text)
        except Exception:
            return

    async def _wait_runner(self, state: MinionRunState) -> None:
        process = state.process
        if process is None:
            return
        returncode = await process.wait()
        await self._wait_for_stream_tasks(state)
        self._record_runner_exit(state, returncode)

    async def _wait_for_stream_tasks(self, state: MinionRunState) -> None:
        current = asyncio.current_task()
        tasks = [
            task
            for task in (state.stdout_task, state.stderr_task)
            if task is not None and task is not current and not task.done()
        ]
        if tasks:
            done, _pending = await asyncio.wait(tasks, timeout=1.0)
            for task in done:
                self._consume_task_result(state, task, "stream")

    def _record_runner_exit(self, state: MinionRunState, returncode: int | None) -> None:
        self._close_liveness_pipe(state)
        if state.status in _TERMINAL_RUN_STATUSES:
            return
        status = "completed" if returncode == 0 else "failed"
        payload: dict[str, Any] = {
            "status": status,
            "summary": f"minion exited with code {returncode}",
            "returncode": returncode,
        }
        if state.last_error:
            payload["error"] = state.last_error
        if state.stderr_tail:
            payload["stderr_tail"] = list(state.stderr_tail[-20:])
        self._record_event(
            state,
            {
                "event_kind": "terminal",
                "payload": payload,
                "created_at": utc_now(),
            },
        )

    def _reconcile_runs(self) -> None:
        for state in list(self.runs.values()):
            for task_name, task in (
                ("stdout", state.stdout_task),
                ("stderr", state.stderr_task),
                ("wait", state.wait_task),
            ):
                self._consume_task_result(state, task, task_name)
                if task is not None and task.done():
                    setattr(state, f"{task_name}_task", None)
            process = state.process
            returncode = getattr(process, "returncode", None) if process is not None else None
            if returncode is not None and state.status in _ACTIVE_RUN_STATUSES:
                self._record_runner_exit(state, returncode)

    def _active_runner_work_order_ids(self) -> set[str]:
        return {
            str(state.pack.work_order_id)
            for state in self.runs.values()
            if state.status in _ACTIVE_RUN_STATUSES
            and state.process is not None
            and state.process.returncode is None
            and str(state.pack.work_order_id or "").strip()
        }

    def _find_active_run_for_work_order(self, work_order_id: str) -> MinionRunState | None:
        wanted = str(work_order_id or "").strip()
        if not wanted:
            return None
        for state in self.runs.values():
            if str(state.pack.work_order_id or "") != wanted:
                continue
            if state.status in _ACTIVE_RUN_STATUSES and state.process is not None and state.process.returncode is None:
                return state
        return None

    def _consume_task_result(self, state: MinionRunState, task: asyncio.Task[None] | None, task_name: str) -> None:
        if task is None or not task.done():
            return
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            state.last_error = f"{exc.__class__.__name__}: {exc}"
            self.logger.exception("minion %s background %s task failed", state.run_id, task_name)
            if task_name == "wait" and state.status in _ACTIVE_RUN_STATUSES:
                self._record_event(
                    state,
                    {
                        "event_kind": "terminal",
                        "payload": {
                            "status": "failed",
                            "summary": "minion wait task failed",
                            "error": state.last_error,
                        },
                        "created_at": utc_now(),
                    },
        )

    def _close_liveness_pipe(self, state: MinionRunState) -> None:
        fd = state.manager_liveness_write_fd
        if fd is None:
            return
        state.manager_liveness_write_fd = None
        with contextlib.suppress(OSError):
            os.close(fd)

    def _install_signal_handlers(self):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return lambda: None
        installed: list[signal.Signals] = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, self._shutdown_event.set)
                installed.append(sig)

        def remove() -> None:
            for sig in installed:
                with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                    loop.remove_signal_handler(sig)

        return remove

    def _record_runner_stderr_line(self, state: MinionRunState, line: str) -> None:
        normalized = str(line or "").strip()
        if not normalized:
            return
        state.stderr_tail.append(normalized)
        if len(state.stderr_tail) > 20:
            del state.stderr_tail[:-20]
        if _runner_stderr_line_is_error(normalized):
            state.last_error = normalized

    def _record_event(self, state: MinionRunState, payload: dict[str, Any]) -> None:
        event_kind = str(payload.get("event_kind") or "")
        event = {
            "event_kind": event_kind,
            "minion_id": str(payload.get("minion_id") or state.minion_id),
            "run_id": str(payload.get("run_id") or state.run_id),
            "work_order_id": str(payload.get("work_order_id") or state.pack.work_order_id),
            "minion_profile": str(payload.get("minion_profile") or state.pack.minion_profile),
            "payload": dict(payload.get("payload") or {}),
            "created_at": str(payload.get("created_at") or utc_now()),
        }
        if event_kind == "approval_requested":
            state.status = "approval_pending"
            approval_payload = dict(event["payload"])
            approval_payload.setdefault("minion_id", state.minion_id)
            approval_payload.setdefault("run_id", state.run_id)
            approval_payload.setdefault("work_order_id", state.pack.work_order_id)
            metadata = dict(approval_payload.get("metadata") or {})
            if "control_route" not in metadata and isinstance(state.pack.metadata.get("control_route"), dict):
                metadata["control_route"] = dict(state.pack.metadata.get("control_route") or {})
            if metadata:
                approval_payload["metadata"] = metadata
            state.pending_approval = approval_payload
            event["payload"] = approval_payload
        elif event_kind == "clarification_requested":
            state.status = "clarification_pending"
            clarification_payload = dict(event["payload"])
            clarification_payload.setdefault("minion_id", state.minion_id)
            clarification_payload.setdefault("run_id", state.run_id)
            clarification_payload.setdefault("work_order_id", state.pack.work_order_id)
            metadata = dict(clarification_payload.get("metadata") or {})
            if "control_route" not in metadata and isinstance(state.pack.metadata.get("control_route"), dict):
                metadata["control_route"] = dict(state.pack.metadata.get("control_route") or {})
            if metadata:
                clarification_payload["metadata"] = metadata
            state.pending_clarification = clarification_payload
            event["payload"] = clarification_payload
        elif isinstance(state.pack.metadata.get("control_route"), dict):
            event_payload = dict(event["payload"])
            metadata = dict(event_payload.get("metadata") or {})
            metadata.setdefault("control_route", dict(state.pack.metadata.get("control_route") or {}))
            event_payload["metadata"] = metadata
            event["payload"] = event_payload
        if event_kind == "terminal":
            terminal_status = str(event["payload"].get("status") or "completed")
            state.status = terminal_status
            state.ended_at = utc_now()
            state.pending_approval = {}
            state.pending_clarification = {}
        if event_kind == "progress":
            self._update_progress_state(state, event)
        state.last_event = event
        state.last_event_at = str(event.get("created_at") or utc_now())
        state.ledger.append(event)
        self._queue_event_delivery(event)
        self.logger.info(
            "minion event run=%s kind=%s status=%s phase=%s summary=%s",
            state.run_id,
            event_kind,
            event["payload"].get("status", ""),
            event["payload"].get("phase", ""),
            str(event["payload"].get("summary") or "")[:240],
        )
        try:
            self.tasking_repository.record_minion_event(event)
        except Exception:
            self.logger.exception("failed to record minion tasking event: %s", state.run_id)
            return
        if event_kind == "checkpoint":
            self._schedule_plan_review(state, event)
            self._schedule_checkpoint_review(state, event)
        if event_kind == "terminal":
            self._schedule_reviewer_terminal_reconciliation(state, event)
        if event_kind == "milestone_completed":
            self._schedule_serial_module_turn(state, event)

    def _schedule_plan_review(self, state: MinionRunState, event: dict[str, Any]) -> None:
        payload = dict(event.get("payload") or {})
        if str(payload.get("status") or "").strip().lower() != "completed":
            return
        plan_ref = payload.get("plan_ref")
        if not isinstance(plan_ref, dict):
            return
        plan_validation = dict(payload.get("plan_validation") or {})
        if str(plan_validation.get("status") or "").strip().lower() not in {"valid", "ok"}:
            return
        profile_text = " ".join(
            [
                str(state.pack.minion_profile or ""),
                str((state.pack.resolved_profile or {}).get("canonical_profile_id") or ""),
                str((state.pack.resolved_profile or {}).get("profile_id") or ""),
            ]
        ).lower()
        if "planner" not in profile_text:
            return
        metadata = dict(state.pack.metadata or {})
        plan_review_policy = dict(metadata.get("plan_review") or {})
        if plan_review_policy.get("enabled") is False:
            return
        review_key = _plan_review_key(plan_ref)
        if not review_key or review_key in self._plan_reviews_inflight:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._plan_reviews_inflight.add(review_key)
        loop.create_task(self._spawn_plan_reviewer(state, event, dict(plan_ref), review_key), name=f"minion-plan-review-{_safe_token(review_key)}")

    async def _spawn_plan_reviewer(
        self,
        planner_state: MinionRunState,
        event: dict[str, Any],
        plan_ref: dict[str, Any],
        review_key: str,
    ) -> None:
        try:
            payload = dict(event.get("payload") or {})
            workspace = dict(planner_state.pack.workspace or {})
            repo_path = str(workspace.get("repo_path") or workspace.get("source_repo") or "").strip()
            artifact_dir = str(workspace.get("artifact_dir") or "").strip()
            review_target = {
                "plan_ref": dict(plan_ref),
                "plan_validation": dict(payload.get("plan_validation") or {}),
                "planner_work_order_id": planner_state.pack.work_order_id,
                "planner_run_id": planner_state.run_id,
                "planner_minion_id": planner_state.minion_id,
                "repo_path": repo_path,
                "artifact_dir": artifact_dir,
                "summary": str(payload.get("summary") or ""),
            }
            review_work_order_id = f"wo_plan_review_{_safe_token(review_key)}"
            review_scratch = _prepare_review_scratch(self.runtime_root, review_work_order_id, repo_path=repo_path)
            review_target.update(review_scratch)
            reviewer_order = ReviewerWorkOrder(
                work_order_id=review_work_order_id,
                task_id=f"review_plan_{_safe_token(review_key)}",
                review_target=review_target,
                acceptance_criteria=[
                    "Verify the plan is dispatchable and topology/module ordering is valid.",
                    "Verify referenced files, modules, and claimed APIs with source, LSP, docs, build, or explicit not-applicable evidence.",
                    "Verify the test strategy is executable for the repo and each milestone has concrete acceptance criteria.",
                    "Submit op_minion_review_gate_submit with gate_kind=plan_acceptance and target.plan_ref.",
                ],
                allowed_capabilities=[],
                output_contract={"must_submit": "op_minion_review_gate_submit"},
                metadata={
                    "workspace": {
                        "repo_path": repo_path,
                        **review_scratch,
                        "workspace_policy": {"mode": "read_only_repo"},
                    }
                },
            )
            metadata = {
                "task_id": reviewer_order.task_id,
                "task_title": f"Review plan {plan_ref.get('plan_id') or review_key}",
                "work_order_title": f"Review plan {plan_ref.get('plan_id') or review_key}",
                "review_target": review_target,
                "reviewer_work_order": reviewer_order.to_dict(),
                "prompt_view": prompt_view_for_reviewer(reviewer_order),
                "milestones": ["Review plan and submit gate"],
                "plan_review_for_run_id": planner_state.run_id,
                "plan_review_for_work_order_id": planner_state.pack.work_order_id,
                "plan_review_key": review_key,
            }
            if isinstance((planner_state.pack.metadata or {}).get("control_route"), dict):
                metadata["control_route"] = dict((planner_state.pack.metadata or {}).get("control_route") or {})
            if isinstance((planner_state.pack.metadata or {}).get("plan_review"), dict):
                metadata["plan_review"] = dict((planner_state.pack.metadata or {}).get("plan_review") or {})
            pack = TaskContextPack.from_dict(
                {
                    "work_order_id": review_work_order_id,
                    "goal": f"Review plan {plan_ref.get('plan_id') or review_key}",
                    "instruction": (
                        "Review the referenced planner FinalPlanArtifact. Do not modify the source repository. "
                        "You may create temporary probes only under /tmp, $TMPDIR, or your isolated minion artifact workspace. "
                        "You must submit a structured gate through op_minion_review_gate_submit before completing."
                    ),
                    "workspace": {
                        "repo_path": repo_path,
                        "artifact_dir": artifact_dir,
                        **review_scratch,
                        "workspace_policy": {"mode": "read_only_repo"},
                        "review_target_plan_ref": dict(plan_ref),
                    },
                    "minion_profile": "software_engineering.reviewer",
                    "metadata": metadata,
                }
            )
            if not pack.resolved_profile:
                pack = MinionProfileRegistry(runtime_root=self.runtime_root).resolve_pack(pack)
            await self.spawn(pack.to_dict())
        except Exception:
            self.logger.exception("failed to spawn plan reviewer: %s", review_key)
            self._record_event(
                planner_state,
                {
                    "event_kind": "plan_review_failed",
                    "payload": {
                        "status": "failed",
                        "summary": "manager failed to spawn plan reviewer",
                        "plan_ref": plan_ref,
                    },
                    "created_at": utc_now(),
                },
            )
            self._plan_reviews_inflight.discard(review_key)

    def _schedule_checkpoint_review(self, state: MinionRunState, event: dict[str, Any]) -> None:
        payload = dict(event.get("payload") or {})
        if str(payload.get("status") or "").strip().lower() != "claimed":
            return
        metadata = dict(state.pack.metadata or {})
        module_execution = dict(metadata.get("module_execution") or {})
        review_policy = dict(module_execution.get("checkpoint_review") or metadata.get("checkpoint_review") or {})
        if review_policy.get("enabled") is not True:
            return
        checkpoint_id = str(payload.get("checkpoint_id") or "").strip()
        if not checkpoint_id or checkpoint_id in self._checkpoint_reviews_inflight:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._checkpoint_reviews_inflight.add(checkpoint_id)
        loop.create_task(self._spawn_checkpoint_reviewer(state, event, checkpoint_id), name=f"minion-review-{checkpoint_id}")

    async def _spawn_checkpoint_reviewer(self, coder_state: MinionRunState, event: dict[str, Any], checkpoint_id: str) -> None:
        try:
            payload = dict(event.get("payload") or {})
            workspace = dict(coder_state.pack.workspace or {})
            repo_path = str(workspace.get("repo_path") or "").strip()
            review_gate_kind = self._checkpoint_review_gate_kind(coder_state, payload)
            review_target = {
                "checkpoint_id": checkpoint_id,
                "gate_kind": review_gate_kind,
                "work_order_id": coder_state.pack.work_order_id,
                "run_id": coder_state.run_id,
                "minion_id": coder_state.minion_id,
                "module_id": str(payload.get("module_id") or ""),
                "milestone_id": str(payload.get("milestone_id") or ""),
                "milestone_index": payload.get("milestone_index"),
                "acceptance_criteria": [str(item) for item in list(payload.get("acceptance_criteria") or [])],
                "commit_sha": str(payload.get("commit_sha") or ""),
                "repo_path": repo_path,
                "summary": str(payload.get("summary") or ""),
            }
            review_work_order_id = f"wo_review_{_safe_token(checkpoint_id)}"
            review_scratch = _prepare_review_scratch(self.runtime_root, review_work_order_id, repo_path=repo_path)
            review_target.update(review_scratch)
            reviewer_order = ReviewerWorkOrder(
                work_order_id=review_work_order_id,
                task_id=f"review_{_safe_token(checkpoint_id)}",
                review_target=review_target,
                acceptance_criteria=[
                    "Verify the checkpoint matches the milestone contract.",
                    "Run or inspect relevant tests when possible.",
                    "Verify claimed APIs with source, LSP, docs, build, or explicit not-verified findings.",
                    f"Submit op_minion_review_gate_submit with gate_kind={review_gate_kind}.",
                ],
                allowed_capabilities=[],
                output_contract={"must_submit": "op_minion_review_gate_submit"},
                metadata={
                    "workspace": {
                        "repo_path": repo_path,
                        **review_scratch,
                        "workspace_policy": {"mode": "read_only_repo"},
                    }
                },
            )
            metadata = {
                "task_id": reviewer_order.task_id,
                "task_title": f"Review checkpoint {checkpoint_id}",
                "work_order_title": f"Review checkpoint {checkpoint_id}",
                "review_target": review_target,
                "reviewer_work_order": reviewer_order.to_dict(),
                "prompt_view": prompt_view_for_reviewer(reviewer_order),
                "milestones": ["Review checkpoint and submit gate"],
                "checkpoint_review_for_run_id": coder_state.run_id,
                "checkpoint_review_for_work_order_id": coder_state.pack.work_order_id,
            }
            if isinstance((coder_state.pack.metadata or {}).get("control_route"), dict):
                metadata["control_route"] = dict((coder_state.pack.metadata or {}).get("control_route") or {})
            pack = TaskContextPack.from_dict(
                {
                    "work_order_id": review_work_order_id,
                    "goal": f"Review checkpoint {checkpoint_id}",
                    "instruction": (
                        "Review the referenced milestone checkpoint. Do not modify the coder workspace. "
                        f"You must submit a structured gate through op_minion_review_gate_submit with gate_kind={review_gate_kind} before completing."
                    ),
                    "workspace": {
                        "repo_path": repo_path,
                        **review_scratch,
                        "workspace_policy": {"mode": "read_only_repo"},
                    },
                    "minion_profile": "software_engineering.reviewer",
                    "metadata": metadata,
                }
            )
            if not pack.resolved_profile:
                pack = MinionProfileRegistry(runtime_root=self.runtime_root).resolve_pack(pack)
            await self.spawn(pack.to_dict())
        except Exception:
            self.logger.exception("failed to spawn checkpoint reviewer: %s", checkpoint_id)
            await self._send_runner_control_or_record(
                coder_state,
                {
                    "type": "blocked",
                    "payload": {
                        "status": "blocked",
                        "summary": "manager failed to spawn checkpoint reviewer",
                        "checkpoint_id": checkpoint_id,
                    },
                },
            )

    def _checkpoint_review_gate_kind(self, coder_state: MinionRunState, payload: dict[str, Any]) -> str:
        expected = str(payload.get("expected_review_gate_kind") or "").strip().lower()
        if expected in {"checkpoint_verification", "repair_verification"}:
            return expected
        if isinstance(payload.get("repair_attempt"), dict):
            return "repair_verification"
        metadata = dict(coder_state.pack.metadata or {})
        module_execution = dict(metadata.get("module_execution") or {})
        last = dict(module_execution.get("last_repair_attempt") or {})
        if last:
            milestone_index = _coerce_int(payload.get("milestone_index"), _coerce_int(module_execution.get("current_milestone_index"), 0))
            if _coerce_int(last.get("milestone_index"), -1) == milestone_index:
                return "repair_verification"
        return "checkpoint_verification"

    def _schedule_reviewer_terminal_reconciliation(self, state: MinionRunState, event: dict[str, Any]) -> None:
        metadata = dict(state.pack.metadata or {})
        review_target = dict(metadata.get("review_target") or {})
        plan_ref = review_target.get("plan_ref")
        if isinstance(plan_ref, dict):
            review_key = str(metadata.get("plan_review_key") or _plan_review_key(plan_ref)).strip()
            if review_key:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    return
                loop.create_task(self._reconcile_plan_review(state, dict(plan_ref), review_key), name=f"minion-plan-review-reconcile-{_safe_token(review_key)}")
                return
        checkpoint_id = str(review_target.get("checkpoint_id") or "").strip()
        coder_run_id = str(review_target.get("run_id") or metadata.get("checkpoint_review_for_run_id") or "").strip()
        if not checkpoint_id or not coder_run_id:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._reconcile_checkpoint_review(state, checkpoint_id, coder_run_id), name=f"minion-review-reconcile-{checkpoint_id}")

    def _record_work_order_event(
        self,
        *,
        work_order_id: str,
        event_kind: str,
        payload: dict[str, Any],
        minion_id: str = "",
        run_id: str = "",
        minion_profile: str = "",
    ) -> None:
        normalized = str(work_order_id or "").strip()
        if not normalized:
            return
        event = {
            "event_kind": event_kind,
            "minion_id": str(minion_id or ""),
            "run_id": str(run_id or ""),
            "work_order_id": normalized,
            "minion_profile": str(minion_profile or ""),
            "payload": dict(payload or {}),
            "created_at": utc_now(),
        }
        self._queue_event_delivery(event)
        self.tasking_repository.record_minion_event(event)

    def _merge_plan_review_state(self, work_order_id: str, payload: dict[str, Any]) -> None:
        if not str(work_order_id or "").strip():
            return
        self.tasking_repository.merge_work_order_metadata(work_order_id, {"plan_review": dict(payload or {})})

    async def _reconcile_plan_review(self, reviewer_state: MinionRunState, plan_ref: dict[str, Any], review_key: str) -> None:
        try:
            latest = self.tasking_repository.latest_review_gate_for_plan_ref(plan_ref)
            metadata = dict(reviewer_state.pack.metadata or {})
            review_target = dict(metadata.get("review_target") or {})
            source_work_order_id = str(metadata.get("plan_review_for_work_order_id") or review_target.get("planner_work_order_id") or "")
            if latest.get("status") != "ok":
                self._merge_plan_review_state(
                    source_work_order_id,
                    {
                        "status": "gate_missing",
                        "plan_ref": dict(plan_ref),
                        "summary": "reviewer finished without submitting a plan_acceptance gate",
                        "updated_at": utc_now(),
                    },
                )
                event = {
                    "event_kind": "plan_review_failed",
                    "minion_id": reviewer_state.minion_id,
                    "run_id": reviewer_state.run_id,
                    "work_order_id": source_work_order_id or reviewer_state.pack.work_order_id,
                    "minion_profile": reviewer_state.pack.minion_profile,
                    "payload": {
                        "status": "failed",
                        "summary": "reviewer finished without submitting a plan_acceptance gate",
                        "plan_ref": dict(plan_ref),
                    },
                    "created_at": utc_now(),
                }
                self._queue_event_delivery(event)
                self.tasking_repository.record_minion_event(event)
                return
            gate = dict(latest.get("review_gate") or {})
            verdict = str(gate.get("verdict") or "").strip().lower()
            event_kind = {
                "pass": "plan_review_passed",
                "fail": "plan_review_failed",
                "partial": "plan_review_partial",
            }.get(verdict, "plan_review_failed")
            payload = {
                "status": verdict or "failed",
                "summary": gate.get("summary") or f"plan review {verdict or 'failed'}",
                "plan_ref": dict(plan_ref),
                "review_gate": gate,
                "review_gate_ref": dict(latest.get("review_gate_ref") or {}),
            }
            review_state = {
                "status": {
                    "pass": "acceptance_pending",
                    "fail": "revision_required",
                    "partial": "human_decision_required",
                }.get(verdict, "failed"),
                "plan_ref": dict(plan_ref),
                "review_gate_ref": dict(latest.get("review_gate_ref") or {}),
                "review_gate": gate,
                "updated_at": utc_now(),
                "next_action": {
                    "pass": "accept_plan",
                    "fail": "revise_plan",
                    "partial": "human_decision",
                }.get(verdict, "inspect_review"),
            }
            plan_review_policy = dict(metadata.get("plan_review") or {})
            if verdict == "pass" and _coerce_bool(plan_review_policy.get("auto_accept") or plan_review_policy.get("auto_accept_reviewed_plan")):
                try:
                    acceptance = self.tasking_repository.accept_plan_ref(
                        plan_ref,
                        review_gate_ref=latest.get("review_gate_ref") or {},
                        reason=str(plan_review_policy.get("acceptance_reason") or "plan reviewer gate passed"),
                    )
                    payload["acceptance"] = acceptance
                    review_state["status"] = "accepted"
                    review_state["accepted_plan_ref"] = dict(acceptance.get("plan_ref") or {})
                    review_state["acceptance"] = acceptance
                    self._record_work_order_event(
                        work_order_id=source_work_order_id,
                        event_kind="plan_accepted",
                        minion_id=reviewer_state.minion_id,
                        run_id=reviewer_state.run_id,
                        minion_profile=reviewer_state.pack.minion_profile,
                        payload={
                            "status": "accepted",
                            "summary": "plan reviewer gate passed and manager accepted the plan",
                            "plan_ref": dict(acceptance.get("plan_ref") or {}),
                            "review_gate_ref": dict(latest.get("review_gate_ref") or {}),
                        },
                    )
                except Exception as exc:
                    payload["acceptance_error"] = f"{exc.__class__.__name__}: {exc}"
                    review_state["status"] = "acceptance_failed"
                    review_state["acceptance_error"] = payload["acceptance_error"]
                    self._record_work_order_event(
                        work_order_id=source_work_order_id,
                        event_kind="plan_acceptance_failed",
                        minion_id=reviewer_state.minion_id,
                        run_id=reviewer_state.run_id,
                        minion_profile=reviewer_state.pack.minion_profile,
                        payload={
                            "status": "failed",
                            "summary": "manager could not accept reviewed plan",
                            "plan_ref": dict(plan_ref),
                            "review_gate_ref": dict(latest.get("review_gate_ref") or {}),
                            "error": payload["acceptance_error"],
                        },
                    )
            self._merge_plan_review_state(source_work_order_id, review_state)
            event = {
                "event_kind": event_kind,
                "minion_id": reviewer_state.minion_id,
                "run_id": reviewer_state.run_id,
                "work_order_id": source_work_order_id or reviewer_state.pack.work_order_id,
                "minion_profile": reviewer_state.pack.minion_profile,
                "payload": payload,
                "created_at": utc_now(),
            }
            self._queue_event_delivery(event)
            self.tasking_repository.record_minion_event(event)
            if verdict == "pass" and review_state.get("status") == "acceptance_pending":
                self._record_work_order_event(
                    work_order_id=source_work_order_id,
                    event_kind="plan_acceptance_pending",
                    minion_id=reviewer_state.minion_id,
                    run_id=reviewer_state.run_id,
                    minion_profile=reviewer_state.pack.minion_profile,
                    payload={
                        "status": "pending",
                        "summary": "plan review passed; op_minion_accept_plan or explicit policy is required before dispatch",
                        "plan_ref": dict(plan_ref),
                        "review_gate_ref": dict(latest.get("review_gate_ref") or {}),
                    },
                )
            elif verdict == "fail":
                auto_revision_spawned: dict[str, Any] = {}
                if _plan_auto_revision_allowed(
                    plan_review_policy,
                    spawned_count=self.tasking_repository.count_ledger_events(source_work_order_id, "plan_revision_spawned"),
                ):
                    auto_revision_spawned = await self._spawn_plan_revision_from_gate(
                        source_work_order_id=source_work_order_id,
                        reviewer_state=reviewer_state,
                        plan_ref=plan_ref,
                        review_gate_ref=dict(latest.get("review_gate_ref") or {}),
                        plan_review_policy=plan_review_policy,
                    )
                    if auto_revision_spawned.get("status") == "spawned":
                        review_state["status"] = "revision_spawned"
                        review_state["revision_spawn"] = dict(auto_revision_spawned)
                        self._merge_plan_review_state(source_work_order_id, review_state)
                self._record_work_order_event(
                    work_order_id=source_work_order_id,
                    event_kind="plan_revision_spawned" if auto_revision_spawned.get("status") == "spawned" else "plan_revision_required",
                    minion_id=reviewer_state.minion_id,
                    run_id=reviewer_state.run_id,
                    minion_profile=reviewer_state.pack.minion_profile,
                    payload={
                        "status": "revision_spawned" if auto_revision_spawned.get("status") == "spawned" else "revision_required",
                        "summary": (
                            "plan reviewer requested revision and manager spawned a revision planner"
                            if auto_revision_spawned.get("status") == "spawned"
                            else gate.get("summary") or "plan reviewer requested revision"
                        ),
                        "source_plan_ref": dict(plan_ref),
                        "review_gate_ref": dict(latest.get("review_gate_ref") or {}),
                        "review_gate": gate,
                        "next_action": "wait_for_revision" if auto_revision_spawned.get("status") == "spawned" else "revise_plan",
                        "auto_revision": dict(auto_revision_spawned),
                    },
                )
            elif verdict == "partial":
                self._record_work_order_event(
                    work_order_id=source_work_order_id,
                    event_kind="plan_review_human_decision_required",
                    minion_id=reviewer_state.minion_id,
                    run_id=reviewer_state.run_id,
                    minion_profile=reviewer_state.pack.minion_profile,
                    payload={
                        "status": "human_decision_required",
                        "summary": gate.get("summary") or "plan review was partial",
                        "source_plan_ref": dict(plan_ref),
                        "review_gate_ref": dict(latest.get("review_gate_ref") or {}),
                        "review_gate": gate,
                        "next_action": "human_decision",
                    },
                )
        finally:
            self._plan_reviews_inflight.discard(review_key)

    async def _spawn_plan_revision_from_gate(
        self,
        *,
        source_work_order_id: str,
        reviewer_state: MinionRunState,
        plan_ref: dict[str, Any],
        review_gate_ref: dict[str, Any],
        plan_review_policy: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            attempt = self.tasking_repository.count_ledger_events(source_work_order_id, "plan_revision_spawned") + 1
            metadata = {
                "plan_review": {
                    **dict(plan_review_policy),
                    "auto_revision_attempt": attempt,
                    "source_work_order_id": source_work_order_id,
                }
            }
            if isinstance((reviewer_state.pack.metadata or {}).get("control_route"), dict):
                metadata["control_route"] = dict((reviewer_state.pack.metadata or {}).get("control_route") or {})
            pack = self.tasking_repository.build_planner_revision_pack_from_review_gate(
                review_gate_ref,
                metadata=metadata,
                workspace={
                    key: value
                    for key, value in {
                        "repo_path": (reviewer_state.pack.workspace or {}).get("repo_path"),
                        "source_repo": (reviewer_state.pack.workspace or {}).get("source_repo"),
                        "artifact_dir": (reviewer_state.pack.workspace or {}).get("artifact_dir"),
                    }.items()
                    if str(value or "").strip()
                },
            )
            if not pack.resolved_profile:
                pack = MinionProfileRegistry(runtime_root=self.runtime_root).resolve_pack(pack)
            await self.spawn(pack.to_dict())
            return {
                "status": "spawned",
                "work_order_id": pack.work_order_id,
                "task_id": str((pack.metadata or {}).get("task_id") or ""),
                "source_plan_ref": dict(plan_ref),
                "review_gate_ref": dict(review_gate_ref),
                "auto_revision_attempt": attempt,
            }
        except Exception as exc:
            self.logger.exception("failed to spawn plan revision planner")
            self._record_work_order_event(
                work_order_id=source_work_order_id,
                event_kind="plan_revision_spawn_failed",
                minion_id=reviewer_state.minion_id,
                run_id=reviewer_state.run_id,
                minion_profile=reviewer_state.pack.minion_profile,
                payload={
                    "status": "failed",
                    "summary": "manager failed to spawn plan revision planner",
                    "source_plan_ref": dict(plan_ref),
                    "review_gate_ref": dict(review_gate_ref),
                    "error": f"{exc.__class__.__name__}: {exc}",
                },
            )
            return {
                "status": "failed",
                "error": f"{exc.__class__.__name__}: {exc}",
                "source_plan_ref": dict(plan_ref),
                "review_gate_ref": dict(review_gate_ref),
            }

    async def _reconcile_checkpoint_review(self, reviewer_state: MinionRunState, checkpoint_id: str, coder_run_id: str) -> None:
        try:
            latest = self.tasking_repository.latest_review_gate_for_checkpoint(checkpoint_id)
            if latest.get("status") != "ok":
                coder_state = self.runs.get(coder_run_id)
                if coder_state is not None:
                    await self._send_runner_control_or_record(
                        coder_state,
                        {"type": "blocked", "payload": {"status": "blocked", "summary": "reviewer finished without submitting a checkpoint gate", "checkpoint_id": checkpoint_id}},
                    )
                return
            gate = dict(latest.get("review_gate") or {})
            coder_state = self.runs.get(coder_run_id)
            if coder_state is None:
                return
            verdict = str(gate.get("verdict") or "").strip().lower()
            if verdict == "pass":
                closure = self.tasking_repository.close_checkpoint_from_review_gate(latest.get("review_gate_ref") or gate)
                payload = dict(closure.get("payload") or {})
                if not payload:
                    payload = {"status": "completed", "checkpoint_id": checkpoint_id, **dict(gate.get("target") or {})}
                metadata = dict(coder_state.pack.metadata or {})
                module_execution = dict(metadata.get("module_execution") or {})
                if str(module_execution.get("mode") or "") == "serial_module_milestones":
                    await self._send_serial_module_turn(
                        coder_state,
                        {"work_order_id": coder_state.pack.work_order_id, "payload": payload},
                        f"{coder_state.pack.work_order_id}:{checkpoint_id}:review_pass",
                    )
                else:
                    await self._send_runner_control_or_record(
                        coder_state,
                        {
                            "type": "complete",
                            "completion": {
                                "status": "completed",
                                "summary": gate.get("summary") or "checkpoint review passed",
                                "checkpoint_id": checkpoint_id,
                                "review_gate": gate,
                                "review_gate_ref": dict(latest.get("review_gate_ref") or {}),
                            },
                        },
                    )
                return
            if verdict == "fail":
                await self._send_checkpoint_repair_turn(coder_state, gate)
                return
            await self._send_runner_control_or_record(
                coder_state,
                {"type": "blocked", "payload": {"status": "blocked", "summary": gate.get("summary") or "checkpoint review was partial", "review_gate": gate}},
            )
        finally:
            self._checkpoint_reviews_inflight.discard(checkpoint_id)

    async def _send_checkpoint_repair_turn(self, coder_state: MinionRunState, gate: dict[str, Any]) -> None:
        current = dict((coder_state.pack.continuity or {}).get("current_milestone") or {})
        if not current:
            current = dict((coder_state.pack.metadata.get("prompt_view") or {}).get("milestone") or {})
        if not current:
            target = _review_gate_target(gate)
            milestone_index = _coerce_int(target.get("milestone_index"), 0)
            current = {
                "milestone_index": milestone_index,
                "milestone_id": str(target.get("milestone_id") or f"m{milestone_index}"),
                "title": "Repair checkpoint",
                "task": "Repair the checkpoint according to reviewer findings.",
            }
        repair_state = self._claim_checkpoint_repair_attempt(coder_state, gate, current)
        if str(repair_state.get("status") or "") == "blocked":
            payload = {
                "status": "blocked",
                "summary": str(repair_state.get("summary") or "checkpoint review failed too many times"),
                "reason": "repair_attempt_limit_exceeded",
                "review_gate": gate,
                "repair": repair_state,
            }
            self._record_event(
                coder_state,
                {
                    "event_kind": "review_repair_blocked",
                    "payload": payload,
                    "created_at": utc_now(),
                },
            )
            await self._send_runner_control_or_record(coder_state, {"type": "blocked", "payload": payload})
            return
        summary = str(gate.get("summary") or "checkpoint review failed; repair the current milestone").strip()
        repair_note = _review_gate_repair_note(gate)
        module_execution = dict(repair_state.get("module_execution") or {})
        turn = {
            "type": "repair_turn",
            "turn_kind": "milestone_repair",
            "work_order_id": coder_state.pack.work_order_id,
            "goal": coder_state.pack.goal,
            "instruction": f"Repair the current milestone according to reviewer findings.\n\n{repair_note}",
            "acceptance_criteria": list(coder_state.pack.acceptance_criteria),
            "current_milestone": current,
            "prompt_view": dict((coder_state.pack.metadata.get("prompt_view") or {})),
            "metadata_updates": {
                "review_feedback": gate,
                "module_execution": module_execution,
            },
            "workspace_updates": dict(coder_state.pack.workspace or {}),
        }
        coder_state.pack = apply_minion_turn_to_pack(coder_state.pack, turn, checkpoint_payload={})
        await self._send_runner_control_or_record(coder_state, {"type": "repair_turn", "turn": turn, "summary": summary})

    def _claim_checkpoint_repair_attempt(
        self,
        coder_state: MinionRunState,
        gate: dict[str, Any],
        current_milestone: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = dict(coder_state.pack.metadata or {})
        module_execution = dict(metadata.get("module_execution") or {})
        review_policy = dict(module_execution.get("checkpoint_review") or metadata.get("checkpoint_review") or {})
        limit = max(0, min(10, _coerce_int(review_policy.get("max_repair_attempts"), 5)))
        target = _review_gate_target(gate)
        milestone_index = _coerce_int(
            target.get("milestone_index"),
            _coerce_int(current_milestone.get("milestone_index"), _coerce_int(module_execution.get("current_milestone_index"), 0)),
        )
        milestone_id = str(target.get("milestone_id") or current_milestone.get("milestone_id") or f"m{milestone_index}").strip()
        repair_key = str(milestone_index)
        attempts = {
            str(key): _coerce_int(value, 0)
            for key, value in dict(module_execution.get("repair_attempts_by_milestone") or {}).items()
        }
        current_attempts = max(0, attempts.get(repair_key, 0))
        if current_attempts >= limit:
            return {
                "status": "blocked",
                "attempt": current_attempts,
                "max_repair_attempts": limit,
                "milestone_index": milestone_index,
                "milestone_id": milestone_id,
                "summary": f"checkpoint review failed after {current_attempts}/{limit} automatic repair attempts",
                "module_execution": module_execution,
            }
        next_attempt = current_attempts + 1
        attempts[repair_key] = next_attempt
        module_execution["repair_attempts_by_milestone"] = attempts
        module_execution["last_repair_attempt"] = {
            "attempt": next_attempt,
            "max_repair_attempts": limit,
            "milestone_index": milestone_index,
            "milestone_id": milestone_id,
            "review_gate_ref": {
                key: gate.get(key)
                for key in ("gate_id", "gate_kind", "verdict", "target_kind", "target_key")
                if gate.get(key) not in (None, "", [])
            },
            "created_at": utc_now(),
        }
        metadata["module_execution"] = module_execution
        coder_state.pack = TaskContextPack.from_dict({**coder_state.pack.to_dict(), "metadata": metadata})
        self.tasking_repository.merge_work_order_metadata(coder_state.pack.work_order_id, {"module_execution": module_execution})
        return {
            "status": "repair_assigned",
            "attempt": next_attempt,
            "max_repair_attempts": limit,
            "milestone_index": milestone_index,
            "milestone_id": milestone_id,
            "module_execution": module_execution,
        }

    def _schedule_serial_module_turn(self, state: MinionRunState, event: dict[str, Any]) -> None:
        payload = dict(event.get("payload") or {})
        if str(payload.get("status") or "").strip().lower() != "completed":
            return
        metadata = dict(state.pack.metadata or {})
        module_execution = dict(metadata.get("module_execution") or {})
        if str(module_execution.get("mode") or "") != "serial_module_milestones":
            return
        if not bool(module_execution.get("auto_advance")):
            return
        work_order_id = str(event.get("work_order_id") or state.pack.work_order_id)
        milestone_index = str(payload.get("milestone_index") if payload.get("milestone_index") is not None else "")
        inflight_key = f"{work_order_id}:{milestone_index or state.run_id}"
        if not work_order_id or inflight_key in self._serial_turns_inflight:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._serial_turns_inflight.add(inflight_key)
        loop.create_task(
            self._send_serial_module_turn(state, event, inflight_key),
            name=f"minion-serial-turn-{work_order_id}-{milestone_index or 'next'}",
        )

    async def _send_serial_module_turn(self, state: MinionRunState, event: dict[str, Any], inflight_key: str) -> None:
        work_order_id = str(event.get("work_order_id") or state.pack.work_order_id)
        payload = dict(event.get("payload") or {})
        try:
            turn = self.tasking_repository.next_serial_module_turn(work_order_id)
            if turn is not None:
                state.pack = apply_minion_turn_to_pack(state.pack, turn, checkpoint_payload=payload)
                with contextlib.suppress(Exception):
                    self.tasking_repository.update_work_order_workspace(work_order_id, dict(state.pack.workspace))
                await self._send_runner_control_or_record(state, {"type": "next_turn", "turn": turn})
                self.logger.info(
                    "minion serial module sent next turn run=%s work_order=%s milestone=%s",
                    state.run_id,
                    work_order_id,
                    str((turn.get("current_milestone") or {}).get("milestone_id") or ""),
                )
                return
            completion = self.tasking_repository.mark_serial_module_completed(work_order_id)
            event_payload: dict[str, Any] = dict(completion)
            event_work_order_id = work_order_id
            if str(completion.get("status") or "") == "completed":
                parent_completion = self.tasking_repository.record_plan_module_completion(work_order_id, completion)
                if str(parent_completion.get("status") or "") in {"awaiting_continue", "completed"}:
                    event_work_order_id = str(parent_completion.get("parent_work_order_id") or work_order_id)
                    event_payload = {**completion, **parent_completion}
            elif str(completion.get("status") or "") == "already_completed":
                event_payload = {**completion, "summary": completion.get("summary") or "serial module was already completed"}
            if str(completion.get("status") or "") in {"completed", "already_completed"}:
                module_event = {
                    "event_kind": "module_completed",
                    "minion_id": "",
                    "run_id": state.run_id,
                    "work_order_id": event_work_order_id,
                    "minion_profile": state.pack.minion_profile,
                    "payload": event_payload,
                    "created_at": utc_now(),
                }
                self._queue_event_delivery(module_event)
                self.tasking_repository.record_minion_event(module_event)
            await self._send_runner_control_or_record(state, {"type": "complete", "completion": event_payload})
            self.logger.info("minion serial module sent completion run=%s work_order=%s", state.run_id, work_order_id)
        except Exception:
            self.logger.exception("failed to send serial minion turn: %s", work_order_id)
        finally:
            self._serial_turns_inflight.discard(inflight_key)

    async def _send_runner_control_or_record(self, state: MinionRunState, message: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._send_runner_control(state, message)
        except Exception as exc:
            self._record_event(
                state,
                {
                    "event_kind": "manager_control_failed",
                    "payload": {
                        "status": "failed",
                        "summary": f"manager failed to send {str(message.get('type') or 'control')} to minion runner",
                        "message_type": str(message.get("type") or ""),
                        "error": f"{exc.__class__.__name__}: {exc}",
                    },
                    "created_at": utc_now(),
                },
            )
            raise

    def _queue_event_delivery(self, event: dict[str, Any]) -> None:
        if not self.event_subscribers:
            self.event_queue.append(event)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.event_queue.append(event)
            return
        loop.create_task(self._push_event_to_subscribers(dict(event)))

    async def _push_event_to_subscribers(self, event: dict[str, Any]) -> None:
        delivered = False
        for writer in list(self.event_subscribers):
            try:
                writer.write(pack_sidecar_message({"type": "event", "event": event}))
                await writer.drain()
                delivered = True
            except Exception:
                with contextlib.suppress(ValueError):
                    self.event_subscribers.remove(writer)
                with contextlib.suppress(Exception):
                    writer.close()
        if not delivered:
            self.event_queue.append(event)

    async def _close_event_subscribers(self) -> None:
        for writer in list(self.event_subscribers):
            with contextlib.suppress(Exception):
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=0.2)
        self.event_subscribers.clear()

    def _update_progress_state(self, state: MinionRunState, event: dict[str, Any]) -> None:
        payload = dict(event.get("payload") or {})
        phase = str(payload.get("phase") or "").strip()
        if phase:
            state.last_phase = phase
        round_value = payload.get("round")
        if phase in {"llm_round_started", "llm_round_completed"}:
            with contextlib.suppress(TypeError, ValueError):
                state.llm_round_count = max(state.llm_round_count, int(round_value or 0))
        if phase in {"tool_call_started", "tool_call_completed", "tool_call_failed"}:
            state.last_tool_call = {
                "phase": phase,
                "tool_name": str(payload.get("tool_name") or ""),
                "target_name": str(payload.get("target_name") or ""),
                "round": payload.get("round"),
                "tool_call_index": payload.get("tool_call_index"),
                "ok": payload.get("ok"),
                "status": payload.get("status"),
                "error_type": payload.get("error_type"),
            }
            if phase == "tool_call_completed":
                state.tool_call_count += 1

    def _require_run(self, run_id: str) -> MinionRunState:
        state = self.runs.get(run_id)
        if state is None:
            raise KeyError(f"unknown minion run: {run_id}")
        return state

    def _find_run_for_decision(self, decision: MinionApprovalDecision) -> MinionRunState | None:
        if decision.run_id and decision.run_id in self.runs:
            state = self.runs[decision.run_id]
            if str(state.pending_approval.get("approval_id") or "") == decision.approval_id:
                return state
            return None
        for state in self.runs.values():
            if str(state.pending_approval.get("approval_id") or "") == decision.approval_id:
                return state
        return None

    def _find_run_for_clarification(self, payload: dict[str, Any]) -> MinionRunState | None:
        run_id = str(payload.get("run_id") or "").strip()
        clarification_id = str(payload.get("clarification_id") or "").strip()
        if run_id and run_id in self.runs:
            return self.runs[run_id]
        for state in self.runs.values():
            if clarification_id and str(state.pending_clarification.get("clarification_id") or "") == clarification_id:
                return state
        return None


def _debug_log_requested(pack: TaskContextPack) -> bool:
    metadata = dict(pack.metadata or {})
    debug_log = metadata.get("debug_log")
    if isinstance(debug_log, dict) and "enabled" in debug_log:
        return bool(debug_log.get("enabled"))
    return bool(metadata.get("minion_debug_log_enabled") or metadata.get("prompt_log_enabled"))


def _is_plan_parent_pack(pack: TaskContextPack) -> bool:
    metadata = dict(pack.metadata or {})
    plan_execution = dict(metadata.get("plan_execution") or {})
    return str(plan_execution.get("mode") or "") == "module_parent_milestones"


def _is_coder_pack(pack: TaskContextPack) -> bool:
    profile = dict(pack.resolved_profile or {})
    profile_text = " ".join(
        [
            str(pack.minion_profile or ""),
            str(profile.get("profile_id") or ""),
            str(profile.get("canonical_profile_id") or ""),
            str(profile.get("display_name") or ""),
        ]
    ).lower()
    return "coder" in profile_text


def _plan_auto_revision_allowed(policy: dict[str, Any], *, spawned_count: int) -> bool:
    if not _coerce_bool(policy.get("auto_revise") or policy.get("auto_revise_plan")):
        return False
    max_attempts = max(0, _coerce_int(policy.get("max_auto_revision_attempts") or policy.get("max_auto_revisions"), 1))
    return max(0, int(spawned_count or 0)) < max_attempts


def _skill_refs_for_pack(pack: TaskContextPack) -> list[str]:
    sources = _skill_ref_sources_for_pack(pack)
    return _dedupe_strings(
        [
            *sources["pack_allowed_skill_refs"],
            *sources["profile_skill_refs"],
            *sources["work_order_skill_refs"],
            *sources["spawn_bonus_skill_refs"],
        ]
    )


def _skill_ref_sources_for_pack(pack: TaskContextPack) -> dict[str, list[str]]:
    metadata = dict(pack.metadata or {})
    prompt_view = dict(metadata.get("prompt_view") or {})
    milestone = prompt_view.get("milestone")
    work_order_refs: list[str] = []
    work_order_refs.extend(_string_list(metadata.get("skill_refs")))
    work_order_refs.extend(_string_list(prompt_view.get("skill_refs")))
    if isinstance(milestone, dict):
        work_order_refs.extend(_string_list(milestone.get("skill_refs")))
    bonus_refs = [
        *_string_list(metadata.get("spawn_bonus_skill_refs")),
        *_string_list(metadata.get("bonus_skill_refs")),
    ]
    values: list[str] = []
    values.extend(_string_list(pack.allowed_skills))
    profile = dict(pack.resolved_profile or {})
    profile_refs = _string_list(profile.get("effective_skill_refs") or profile.get("skill_refs"))
    return {
        "pack_allowed_skill_refs": _dedupe_strings(values),
        "profile_skill_refs": _dedupe_strings(profile_refs),
        "work_order_skill_refs": _dedupe_strings(work_order_refs),
        "spawn_bonus_skill_refs": _dedupe_strings(bonus_refs),
    }


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = []
    return _dedupe_strings([str(item) for item in values])


def _coerce_skill_manual_context(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in list(value or []):
        if not isinstance(item, dict):
            continue
        skill_id = str(item.get("skill_id") or "").strip()
        manual_text = str(item.get("manual_text") or "").strip()
        if skill_id and manual_text:
            result.append(dict(item))
    return result


def _loads_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(loaded) if isinstance(loaded, dict) else {}


def _loads_json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return _dedupe_strings([str(item) for item in value])
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, list):
        return []
    return _dedupe_strings([str(item) for item in loaded])


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _debug_log_path_from_pack(pack: TaskContextPack) -> str:
    debug_log = dict((pack.metadata or {}).get("debug_log") or {})
    if not bool(debug_log.get("enabled")):
        return ""
    return str(debug_log.get("path") or "")
