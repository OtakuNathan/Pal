from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import sqlite3
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
from pal.minion.event_delivery import MinionEventDelivery
from pal.minion.git_env import finalize_work_order_branch, prepare_task_workspace
from pal.minion.inflight import InflightTracker
from pal.minion.ipc import cleanup_manager_endpoint, minion_log_path, minion_runner_log_path, start_manager_server
from pal.minion.lifecycle import ACTIVE_RUN_STATUSES as _ACTIVE_RUN_STATUSES
from pal.minion.lifecycle import TERMINAL_RUN_STATUSES as _TERMINAL_RUN_STATUSES
from pal.minion.lifecycle import transition_run_status
from pal.minion.lsp_prewarm import prewarm_workspace_lsp
from pal.minion.llm_broker import (
    llm_outcome_to_payload,
    llm_request_from_payload,
    preflight_advice_to_payload,
    preflight_request_from_payload,
)
from pal.minion.profiles import MinionProfileRegistry
from pal.minion.repository import MinionTaskingRepository
from pal.minion.review_orchestrator import ReviewOrchestrator
from pal.minion.runner_process import RunnerProcessSupervisor
from pal.minion.sandbox import with_minion_sandbox_metadata
from pal.minion.serial_scheduler import SerialMilestoneScheduler
from pal.minion.turns import sanitize_runner_session_pack
from pal.minion.utils import coerce_int as _coerce_int
from pal.minion.utils import dedupe_strings as _dedupe_strings
from pal.minion.utils import string_list as _string_list
from pal.shared import MinionApprovalDecision, TaskContextPack


_DEFAULT_MANAGER_TURN_TIMEOUT_SECONDS = 1200


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
    events: MinionEventDelivery = field(init=False)
    reviews: ReviewOrchestrator = field(init=False)
    runner_process: RunnerProcessSupervisor = field(init=False)
    serial_scheduler: SerialMilestoneScheduler = field(init=False)
    _llm_broker_bundle: Any | None = field(default=None, init=False, repr=False)
    _shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _serial_turns_inflight: InflightTracker = field(default_factory=InflightTracker)

    def __post_init__(self) -> None:
        self.tasking_repository = MinionTaskingRepository(runtime_root=self.runtime_root)
        self.tasking_repository.ensure_schema()
        self.events = MinionEventDelivery()
        self.reviews = ReviewOrchestrator(self)
        self.runner_process = RunnerProcessSupervisor(self)
        self.serial_scheduler = SerialMilestoneScheduler(self)

    @property
    def event_queue(self) -> list[dict[str, Any]]:
        return self.events.queue

    @property
    def event_subscribers(self) -> list[asyncio.StreamWriter]:
        return self.events.subscribers

    def _transition_run_status(self, state: MinionRunState, status: str) -> None:
        state.status = transition_run_status(state.status, status)

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
                await self.events.close()
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
        await self.events.handle_subscription(request, reader, writer, shutdown_event=self._shutdown_event)

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
        if method == "llm_preflight":
            return await self.llm_broker_preflight(dict(params))
        if method == "llm_generate":
            return await self.llm_broker_generate(dict(params))
        if method == "llm_resolve_max_output_tokens":
            return await self.llm_broker_resolve_max_output_tokens(dict(params))
        if method == "llm_resolve_endpoint_facts":
            return await self.llm_broker_resolve_endpoint_facts(dict(params))
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

    def _with_profile_gate_policy(self, pack: TaskContextPack) -> tuple[TaskContextPack, dict[str, Any]]:
        metadata = dict(pack.metadata or {})
        module_execution = dict(metadata.get("module_execution") or {})
        if module_execution.get("checkpoint_review") is not None or metadata.get("checkpoint_review") is not None:
            return pack, {}
        profile = dict(pack.resolved_profile or {})
        gate_policy = dict(profile.get("effective_gate_policy") or profile.get("gate_policy") or {})
        gates = {str(item).strip().lower() for item in list(gate_policy.get("after_each_milestone") or [])}
        if "reviewer_gate" not in gates:
            return pack, {}
        reviewer_group = str(gate_policy.get("reviewer_profile_group") or "software_engineering").strip() or "software_engineering"
        reviewer_name = str(gate_policy.get("reviewer_profile_name") or "reviewer").strip() or "reviewer"
        scope = str(gate_policy.get("scope") or "").strip()
        if not scope:
            scope = "module_checkpoint" if str(module_execution.get("mode") or "").strip() else "bare_coder_checkpoint"
        checkpoint_review = {
            "enabled": True,
            "scope": scope,
            "reviewer_profile_group": reviewer_group,
            "reviewer_profile_name": reviewer_name,
            "reviewer_profile": f"{reviewer_group}.{reviewer_name}",
            "max_repair_attempts": _coerce_int(gate_policy.get("max_repair_attempts"), 5),
            "require_test_or_blocker": bool(gate_policy.get("require_test_or_blocker")),
            "require_api_evidence": bool(gate_policy.get("require_api_evidence")),
            "require_lsp_when_applicable": bool(gate_policy.get("require_lsp_when_applicable")),
            "check_public_declarations_have_implementation": bool(gate_policy.get("check_public_declarations_have_implementation")),
            "source": "profile_gate_policy",
        }
        module_execution["checkpoint_review"] = checkpoint_review
        metadata["module_execution"] = module_execution
        updates: dict[str, Any] = {"module_execution": module_execution}
        if metadata.get("manager_turn_timeout_seconds") is None:
            metadata["manager_turn_timeout_seconds"] = _DEFAULT_MANAGER_TURN_TIMEOUT_SECONDS
            updates["manager_turn_timeout_seconds"] = _DEFAULT_MANAGER_TURN_TIMEOUT_SECONDS
        return TaskContextPack.from_dict({**pack.to_dict(), "metadata": metadata}), updates

    async def spawn(self, pack_payload: dict[str, Any]) -> dict[str, Any]:
        pack = TaskContextPack.from_dict(pack_payload)
        profile_registry = MinionProfileRegistry(runtime_root=self.runtime_root)
        pack = profile_registry.resolve_pack(pack)
        pack = self.tasking_repository.prepare_pack_for_spawn(pack)
        pack = profile_registry.resolve_pack(pack)
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
        pack = await self._with_lsp_prewarm(pack)
        pack, metadata_updates = self._with_profile_gate_policy(pack)
        if metadata_updates:
            self.tasking_repository.merge_work_order_metadata(pack.work_order_id, metadata_updates)
        self.tasking_repository.update_work_order_workspace(pack.work_order_id, dict(pack.workspace))
        pack = self._with_runner_debug_log(pack)
        pack = sanitize_runner_session_pack(pack)
        pack = with_minion_sandbox_metadata(self.runtime_root, pack, run_id=run_id)
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

    async def _with_lsp_prewarm(self, pack: TaskContextPack) -> TaskContextPack:
        metadata = dict(pack.metadata or {})
        if bool(metadata.get("lsp_prewarm_disabled")):
            return pack
        try:
            result = await asyncio.to_thread(
                prewarm_workspace_lsp,
                runtime_root=self.runtime_root,
                workspace=dict(pack.workspace or {}),
            )
        except Exception as exc:
            result = {
                "status": "error",
                "error": f"{exc.__class__.__name__}: {exc}",
            }
        if str(result.get("status") or "") == "skipped":
            return pack
        workspace = dict(pack.workspace or {})
        lsp_setup = dict(workspace.get("lsp_setup") or {})
        lsp_setup["prewarm"] = result
        workspace["lsp_setup"] = lsp_setup
        return TaskContextPack.from_dict({**pack.to_dict(), "workspace": workspace})

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
        self._transition_run_status(state, "killed")
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
        self._transition_run_status(state, "running")
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
        self._transition_run_status(state, "running")
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
        await self.runner_process.close_all()
        if self._llm_broker_bundle is not None:
            close = getattr(self._llm_broker_bundle, "close", None)
            if callable(close):
                await close()
            self._llm_broker_bundle = None

    async def _start_runner(self, state: MinionRunState) -> None:
        await self.runner_process.start_runner(state)

    async def llm_broker_preflight(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_broker_run(params)
        request = preflight_request_from_payload(dict(params.get("request") or {}))
        runtime = await self._llm_broker_runtime()
        advice = await runtime.apreflight(request)
        return {"ok": True, "advice": preflight_advice_to_payload(advice)}

    async def llm_broker_generate(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_broker_run(params)
        request = llm_request_from_payload(dict(params.get("request") or {}))
        runtime = await self._llm_broker_runtime()
        outcome = await runtime.agenerate(request)
        return {"ok": True, "outcome": llm_outcome_to_payload(outcome)}

    async def llm_broker_resolve_max_output_tokens(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_broker_run(params)
        runtime = await self._llm_broker_runtime()
        value = await asyncio.to_thread(
            runtime.resolve_max_output_tokens,
            preferred_endpoint_id=str(params.get("preferred_endpoint_id") or "").strip() or None,
            preferred_endpoint_source=str(params.get("preferred_endpoint_source") or "").strip() or None,
        )
        return {"ok": True, "max_output_tokens": value}

    async def llm_broker_resolve_endpoint_facts(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_broker_run(params)
        runtime = await self._llm_broker_runtime()
        return await asyncio.to_thread(
            runtime.resolve_endpoint_facts,
            preferred_endpoint_id=str(params.get("preferred_endpoint_id") or "").strip() or None,
            preferred_endpoint_source=str(params.get("preferred_endpoint_source") or "").strip() or None,
        )

    def _require_broker_run(self, params: dict[str, Any]) -> MinionRunState:
        run_id = str(params.get("run_id") or "").strip()
        if not run_id:
            raise ValueError("run_id is required for minion LLM broker requests")
        state = self._require_run(run_id)
        if state.status in _TERMINAL_RUN_STATUSES:
            raise RuntimeError(f"minion run is terminal: {run_id}")
        return state

    async def _llm_broker_runtime(self) -> Any:
        if self._llm_broker_bundle is None:
            from pal.minion.runner import build_slim_minion_runtime

            self._llm_broker_bundle = await asyncio.to_thread(build_slim_minion_runtime, self.runtime_root)
        return self._llm_broker_bundle.llm_runtime

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
        await self.runner_process.read_runner_stdout(state)

    async def _read_runner_stderr(self, state: MinionRunState) -> None:
        await self.runner_process.read_runner_stderr(state)

    async def _wait_runner(self, state: MinionRunState) -> None:
        await self.runner_process.wait_runner(state)

    async def _wait_for_stream_tasks(self, state: MinionRunState) -> None:
        await self.runner_process.wait_for_stream_tasks(state)

    def _record_runner_exit(self, state: MinionRunState, returncode: int | None) -> None:
        self.runner_process.record_runner_exit(state, returncode)

    def _reconcile_runs(self) -> None:
        self.runner_process.reconcile_runs()

    def _active_runner_work_order_ids(self) -> set[str]:
        return self.runner_process.active_runner_work_order_ids()

    def _find_active_run_for_work_order(self, work_order_id: str) -> MinionRunState | None:
        return self.runner_process.find_active_run_for_work_order(work_order_id)

    def _consume_task_result(self, state: MinionRunState, task: asyncio.Task[None] | None, task_name: str) -> None:
        self.runner_process.consume_task_result(state, task, task_name)

    def _close_liveness_pipe(self, state: MinionRunState) -> None:
        self.runner_process.close_liveness_pipe(state)

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
        self.runner_process.record_runner_stderr_line(state, line)

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
            self._transition_run_status(state, "approval_pending")
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
            self._transition_run_status(state, "clarification_pending")
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
            self._transition_run_status(state, terminal_status)
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
            self.reviews.schedule_plan_review(state, event)
            self.reviews.schedule_checkpoint_review(state, event)
        if event_kind == "terminal":
            self.reviews.schedule_reviewer_terminal_reconciliation(state, event)
        if event_kind == "milestone_completed":
            self.serial_scheduler.schedule(state, event)

    async def _send_runner_control_or_record(self, state: MinionRunState, message: dict[str, Any]) -> dict[str, Any]:
        unavailable_reason = self.runner_control_unavailable_reason(state)
        if unavailable_reason:
            return self.record_runner_control_skipped(state, message, reason=unavailable_reason)
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

    def runner_control_unavailable_reason(self, state: MinionRunState) -> str:
        if state.status in _TERMINAL_RUN_STATUSES:
            return f"run status is terminal: {state.status}"
        if state.process is None:
            return ""
        if state.process.stdin is None:
            return "runner stdin is not available"
        if state.process.returncode is not None:
            return f"runner process exited with returncode {state.process.returncode}"
        return ""

    def record_runner_control_skipped(self, state: MinionRunState, message: dict[str, Any], *, reason: str) -> dict[str, Any]:
        payload = {
            "status": "skipped",
            "summary": f"manager skipped {str(message.get('type') or 'control')} because the minion runner is not accepting control messages",
            "message_type": str(message.get("type") or ""),
            "reason": str(reason or "runner unavailable"),
        }
        self._record_event(
            state,
            {
                "event_kind": "manager_control_skipped",
                "payload": payload,
                "created_at": utc_now(),
            },
        )
        return {"ok": False, "skipped": True, "run_id": state.run_id, "message_type": payload["message_type"], "reason": payload["reason"]}

    def _queue_event_delivery(self, event: dict[str, Any]) -> None:
        self.events.queue_event(event)

    async def _send_serial_module_turn(self, state: MinionRunState, event: dict[str, Any], inflight_key: str) -> None:
        await self.serial_scheduler._send_serial_module_turn(state, event, inflight_key)

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


def _debug_log_path_from_pack(pack: TaskContextPack) -> str:
    debug_log = dict((pack.metadata or {}).get("debug_log") or {})
    if not bool(debug_log.get("enabled")):
        return ""
    return str(debug_log.get("path") or "")
