from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from uuid import uuid4

from pal.foundation import utc_now
from pal.foundation.service_logging import current_service_log_sink_description
from pal.foundation.sidecar import (
    dispatch_sidecar_request,
    pack_sidecar_message,
    read_sidecar_message,
)
from pal.llm.runtime import scoped_llm_event_sink
from pal.minion.config import effective_minion_runtime_config
from pal.minion.dag_advancer import dag_state_to_runtime_dict as _dag_state_to_runtime_dict
from pal.minion.event_delivery import MinionEventDelivery
from pal.minion.debug_log import minion_debug_log_enabled
from pal.minion.gates import (
    GATE_TRIGGER_AFTER_EACH_MILESTONE,
    GATE_TRIGGER_BEFORE_PLAN,
    SOURCE_CONTRACT_GATE,
    GateSpec,
    gate_specs_from_pack,
    normalize_gate_policy,
)
from pal.minion.git_env import finalize_work_order_branch, prepare_task_workspace
from pal.minion.inflight import InflightTracker
from pal.minion.ipc import cleanup_manager_endpoint, minion_runner_log_path, start_manager_server
from pal.minion.lifecycle import ACTIVE_RUN_STATUSES as _ACTIVE_RUN_STATUSES
from pal.minion.lifecycle import TERMINAL_RUN_STATUSES as _TERMINAL_RUN_STATUSES
from pal.minion.lifecycle import transition_run_status
from pal.minion.lsp_prewarm import prewarm_workspace_lsp
from pal.minion.plan_builder import (
    compile_final_plan_from_staged_artifacts,
    initialize_plan_builder_stage_draft,
    load_stage_artifact_ref,
    validate_module_detail_artifact,
    validate_plan_sketch_artifact,
)
from pal.minion.llm_broker import (
    llm_outcome_to_payload,
    llm_request_from_payload,
    preflight_advice_to_payload,
    preflight_request_from_payload,
    stream_event_to_payload,
)
from pal.minion.profiles import MinionProfileRegistry, SOURCE_CONTRACT_REVIEWER_CAPABILITIES
from pal.minion.repository import MinionTaskingRepository
from pal.minion.review_orchestrator import ReviewOrchestrator
from pal.minion.sandbox import with_minion_sandbox_metadata
from pal.minion.serial_scheduler import SerialMilestoneScheduler
from pal.minion.step_executor_runner import StepExecutorRunnerSupervisor
from pal.minion.step_process_supervisor import StepProcessSupervisor
from pal.minion.step_runner import ModuleStepRunner
from pal.minion.turns import sanitize_runner_session_pack
from pal.minion.utils import coerce_int as _coerce_int
from pal.minion.utils import coerce_bool as _coerce_bool
from pal.minion.utils import dedupe_strings as _dedupe_strings
from pal.minion.utils import safe_token
from pal.minion.utils import string_list as _string_list
from pal.minion.workflow import (
    NONE_PROFILE,
    append_workflow_step,
    resolve_workflow_next,
    split_profile_ref,
    update_current_workflow_step,
)
from pal.minion.v2.orchestration import MinionV2OutboxProcessor
from pal.minion.v2.contracts import AggregateType
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.workers import MinionV2SemanticWorker
from pal.minion.v2.recovery import MinionV2Recovery
from pal.minion.work_order import ReviewerWorkOrder, build_planner_work_order, prompt_view_for_reviewer
from pal.shared import MinionApprovalDecision, TaskContextPack


_DEFAULT_MANAGER_TURN_TIMEOUT_SECONDS = 3600
_DEFAULT_MAX_PARALLEL_LLM_NODES = 5
_DEFAULT_MAX_PARALLEL_MODULES = _DEFAULT_MAX_PARALLEL_LLM_NODES
_DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS = 30.0
_GRACEFUL_SHUTDOWN_POLL_SECONDS = 0.2
_RUN_MEMORY_LEDGER_LIMIT = 500
_RUNNER_TELEMETRY_EVENT_KINDS = {"phase_started", "progress"}
_WORKFLOW_RESUMABLE_STATUSES = {"blocked", "failed", "killed", "timeout"}
_WORKFLOW_RESUME_FALLBACK_REASONS = {
    "not_plan_parent",
    "not_plan_module_child",
    "parent_not_plan_parent",
    "parent_missing_plan_artifact",
}
_ARCHIVED_V1_WRITE_METHODS = {
    "spawn",
    "kill",
    "send_decision",
    "send_clarification",
    "finalize_work_order",
    "tick_parent_dag",
    "submit_repair_bill",
    "request_logical_slot",
    "wait_logical_slot",
    "release_logical_slot",
    "dispatch_accepted_plan",
    "dispatch_plan_revision",
    "recover_work_order",
    "resume_work_order",
    "retry_checkpoint_review",
    "destroy_work_order_run",
    "pause_work_order",
    "finish_work_order",
}
_KEY_LLM_ENDPOINT_PROGRESS_PHASES = {
    "llm_endpoint_attempt_failed",
    "llm_endpoint_exhausted",
    "llm_endpoint_fallback_started",
    "llm_endpoint_fallback_succeeded",
    "llm_endpoint_skipped",
}


@dataclass
class MinionRunState:
    minion_id: str
    run_id: str
    pack: TaskContextPack
    process: asyncio.subprocess.Process | None = None
    runner_kind: str = "step_process"
    returncode: int | None = None
    control_queue: asyncio.Queue[dict[str, Any]] | None = None
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
    wait_task: asyncio.Task[None] | None = None

    def summary(self) -> dict[str, Any]:
        run_active = self.status not in _TERMINAL_RUN_STATUSES
        executor_pid = self.process.pid if self.process is not None else None
        executor_returncode = self.process.returncode if self.process is not None else None
        run_returncode = self.returncode if self.returncode is not None else (executor_returncode if run_active else None)
        return {
            "minion_id": self.minion_id,
            "run_id": self.run_id,
            "work_order_id": self.pack.work_order_id,
            "minion_profile": self.pack.minion_profile,
            "profile_display_name": str((self.pack.resolved_profile or {}).get("display_name") or self.pack.minion_profile),
            "status": self.status,
            "run_active": run_active,
            "coroutine_status": "active" if run_active else "terminal",
            "runner_kind": self.runner_kind,
            "pid": executor_pid if run_active else None,
            "executor_pid": executor_pid,
            "returncode": run_returncode,
            "executor_returncode": executor_returncode,
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
    max_parallel_modules: int | None = None
    auto_resume_ready_modules: bool | None = None
    graceful_shutdown_timeout_seconds: float = _DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS
    tasking_repository: MinionTaskingRepository = field(init=False)
    server: asyncio.base_events.Server | None = None
    endpoint_info: dict[str, Any] = field(default_factory=dict)
    runs: dict[str, MinionRunState] = field(default_factory=dict)
    started_at: str = field(default_factory=utc_now)
    events: MinionEventDelivery = field(init=False)
    reviews: ReviewOrchestrator = field(init=False)
    step_executor: StepExecutorRunnerSupervisor = field(init=False)
    serial_scheduler: SerialMilestoneScheduler = field(init=False)
    step_runner: ModuleStepRunner = field(init=False)
    step_processes: StepProcessSupervisor = field(init=False)
    _llm_broker_bundle: Any | None = field(default=None, init=False, repr=False)
    _shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)
    _shutdown_graceful: bool = field(default=True, init=False)
    _shutdown_timeout_seconds: float = field(default=_DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS, init=False)
    _shutdown_reason: str = field(default="", init=False)
    _shutdown_started_at: str = field(default="", init=False)
    _draining: bool = field(default=False, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _serial_turns_inflight: InflightTracker = field(default_factory=InflightTracker)
    _logical_slots: dict[str, dict[str, Any]] = field(default_factory=dict)
    _logical_slot_generation: int = 0
    _logical_slot_event: asyncio.Event = field(default_factory=asyncio.Event)
    _module_detail_scheduler_tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict, init=False, repr=False)
    v2_service: MinionV2WorkflowService = field(init=False, repr=False)
    v2_outbox: MinionV2OutboxProcessor = field(init=False, repr=False)
    v2_semantic_worker: MinionV2SemanticWorker = field(init=False, repr=False)
    _v2_outbox_task: asyncio.Task[Any] | None = field(default=None, init=False, repr=False)
    _v2_wake_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    def __post_init__(self) -> None:
        runtime_config = effective_minion_runtime_config(self.runtime_root)
        if self.max_parallel_modules is None:
            self.max_parallel_modules = max(
                1,
                _coerce_int(
                    runtime_config.get("max_parallel_llm_nodes", runtime_config.get("max_parallel_modules")),
                    _DEFAULT_MAX_PARALLEL_LLM_NODES,
                )
                or _DEFAULT_MAX_PARALLEL_LLM_NODES,
            )
        else:
            self.max_parallel_modules = max(1, _coerce_int(self.max_parallel_modules, _DEFAULT_MAX_PARALLEL_LLM_NODES) or _DEFAULT_MAX_PARALLEL_LLM_NODES)
        if self.auto_resume_ready_modules is None:
            self.auto_resume_ready_modules = _coerce_bool(runtime_config.get("auto_resume_ready_modules", True))
        else:
            self.auto_resume_ready_modules = _coerce_bool(self.auto_resume_ready_modules)
        self.tasking_repository = MinionTaskingRepository(runtime_root=self.runtime_root)
        self.tasking_repository.ensure_schema()
        self.events = MinionEventDelivery()
        self.reviews = ReviewOrchestrator(self)
        self.step_executor = StepExecutorRunnerSupervisor(self)
        self.serial_scheduler = SerialMilestoneScheduler(self)
        self.step_runner = ModuleStepRunner(self)
        self.step_processes = StepProcessSupervisor(self)
        self.v2_service = MinionV2WorkflowService(self.runtime_root)
        self.v2_semantic_worker = MinionV2SemanticWorker(
            self.v2_service,
            publish_human_review=self._publish_v2_human_review,
            publish_worker_event=self._publish_v2_worker_event,
            register_broker_run=self._register_v2_broker_run,
            unregister_broker_run=self._unregister_v2_broker_run,
        )
        self.v2_outbox = MinionV2OutboxProcessor(
            self.v2_service,
            semantic_effects=self.v2_semantic_worker,
            max_parallel_nodes=max(1, int(self.max_parallel_modules or 1)),
        )

    @property
    def event_queue(self) -> list[dict[str, Any]]:
        return self.events.queue

    @property
    def event_subscribers(self) -> list[asyncio.StreamWriter]:
        return self.events.subscribers

    def _transition_run_status(self, state: MinionRunState, status: str) -> None:
        state.status = transition_run_status(state.status, status)

    async def run(self) -> None:
        startup_recovery = {"status": "archived", "reason": "V1 workflow recovery is disabled"}
        v2_startup_recovery = await asyncio.to_thread(MinionV2Recovery(self.v2_service).recover)
        self.server, self.endpoint_info = await start_manager_server(self.runtime_root, self._handle_client)
        self.logger.info("minion manager listening: %s", self.endpoint_info)
        remove_signal_handlers = self._install_signal_handlers()
        async with self.server:
            serve_task = asyncio.create_task(self.server.serve_forever(), name="minion-manager-serve")
            self._v2_outbox_task = asyncio.create_task(self._run_v2_outbox(), name="minion-v2-outbox")
            try:
                try:
                    startup_schedule = {"status": "archived", "scheduled_count": 0}
                    staged_schedule = {"status": "archived", "scheduled_count": 0}
                    if (
                        int(startup_recovery.get("recovered_count") or 0) > 0
                        or int(startup_schedule.get("scheduled_count") or 0) > 0
                        or int(staged_schedule.get("scheduled_count") or 0) > 0
                    ):
                        self.logger.info(
                            "minion manager startup recovery=%s v2_recovery=%s schedule=%s staged_schedule=%s",
                            startup_recovery,
                            v2_startup_recovery,
                            startup_schedule,
                            staged_schedule,
                        )
                except Exception:
                    self.logger.exception("minion manager startup auto-resume failed")
                await self._shutdown_event.wait()
            finally:
                remove_signal_handlers()
                if self._v2_outbox_task is not None:
                    self._v2_outbox_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._v2_outbox_task
                    self._v2_outbox_task = None
                await self.v2_outbox.stop_background()
                serve_task.cancel()
                self.server.close()
                await self.server.wait_closed()
                with contextlib.suppress(asyncio.CancelledError):
                    await serve_task
                if self._shutdown_graceful:
                    drain_result = await self._drain_active_runs_before_shutdown(
                        timeout_seconds=self._shutdown_timeout_seconds,
                        reason=self._shutdown_reason or "manager_shutdown",
                    )
                    if str(drain_result.get("status") or "") == "timeout":
                        self.logger.warning("minion manager graceful shutdown drain timed out: %s", drain_result)
                    else:
                        self.logger.info("minion manager graceful shutdown drain: %s", drain_result)
                await self.close_all()
                await self.events.close()
                await cleanup_manager_endpoint(self.runtime_root)
                self.logger.info("minion manager stopped")

    def _recover_stale_modules_on_startup(self) -> dict[str, Any]:
        return self.tasking_repository.recover_stale_running_modules(
            active_child_work_order_ids=set(),
            reason="manager startup recovered stale running module",
        )

    async def _schedule_ready_modules_on_startup(self) -> dict[str, Any]:
        if not _coerce_bool(self.auto_resume_ready_modules):
            return {"status": "skipped", "reason": "auto_resume_ready_modules_disabled", "scheduled_count": 0}
        return await self.tick_ready_plan_dags(reason="manager_startup_recovery")

    def _schedule_waiting_module_details_on_startup(self) -> dict[str, Any]:
        parent_ids = self.tasking_repository.resumable_staged_module_detail_parent_work_order_ids()
        scheduled: list[str] = []
        for parent_work_order_id in parent_ids:
            snapshot = self.tasking_repository.read_work_order(parent_work_order_id)
            if snapshot.get("status") != "ok":
                continue
            metadata = dict((snapshot.get("work_order") or {}).get("metadata") or {})
            staged = dict(metadata.get("staged_planning") or {})
            recovered_modules: dict[str, dict[str, Any]] = {}
            for module_id, raw in dict(staged.get("detail_modules") or {}).items():
                if not isinstance(raw, dict):
                    continue
                item = dict(raw)
                status = str(item.get("status") or "").strip().lower()
                if status == "running":
                    item.update(
                        {
                            "status": "waiting_for_slot",
                            "run_id": "",
                            "minion_id": "",
                            "recovered_at": utc_now(),
                            "recovery_reason": "manager startup recovered module-detail run without an active runner",
                        }
                    )
                recovered_modules[str(module_id)] = item
            if recovered_modules:
                self.tasking_repository.merge_work_order_metadata(
                    parent_work_order_id,
                    {
                        "staged_planning": {
                            "detail_modules": recovered_modules,
                            "updated_at": utc_now(),
                        }
                    },
                    work_order_status="active",
                )
            self._schedule_waiting_module_detail_runs(parent_work_order_id, reason="manager_startup_recovery")
            scheduled.append(parent_work_order_id)
        return {
            "status": "scheduled" if scheduled else "idle",
            "scheduled_count": len(scheduled),
            "parent_work_order_ids": scheduled,
        }

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
        if method == "health":
            return self.health()
        self._reconcile_runs()
        if method == "reload_runtime_config":
            result = self.reload_runtime_config()
            if _coerce_bool(result.get("auto_resume_ready_modules", self.auto_resume_ready_modules)):
                result["ready_schedule"] = await self.tick_ready_plan_dags(reason="runtime_config_reload")
            return result
        if method == "v2_wake":
            self._v2_wake_event.set()
            return {"ok": True, "status": "woken"}
        if method == "v2_workflow_status":
            return self.v2_service.workflow_status(str(params.get("workflow_id") or ""))
        if method == "list_runs":
            return {"items": [state.summary() for state in sorted(self.runs.values(), key=lambda item: item.started_at)]}
        if method == "read_run":
            return self.read_run(str(params.get("run_id") or ""))
        if method in _ARCHIVED_V1_WRITE_METHODS:
            raise ValueError(
                f"Minion V1 write RPC '{method}' is archived; use the seven Minion V2 workflow capabilities"
            )
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
        if method == "llm_generate_stream":
            return await self.llm_broker_generate_stream(dict(params))
        if method == "llm_resolve_max_output_tokens":
            return await self.llm_broker_resolve_max_output_tokens(dict(params))
        if method == "llm_resolve_endpoint_facts":
            return await self.llm_broker_resolve_endpoint_facts(dict(params))
        if method == "finalize_work_order":
            return self.finalize_work_order(dict(params))
        if method == "tick_parent_dag":
            return await self.tick_parent_dag(str(params.get("work_order_id") or ""), reason=str(params.get("reason") or "manager_rpc"))
        if method == "submit_repair_bill":
            return await self.submit_repair_bill(dict(params))
        if method == "request_logical_slot":
            return self.request_logical_slot(dict(params))
        if method == "wait_logical_slot":
            return await self.wait_logical_slot(dict(params))
        if method == "release_logical_slot":
            return self.release_logical_slot(dict(params))
        if method == "dispatch_accepted_plan":
            return await self.dispatch_accepted_plan(dict(params))
        if method == "dispatch_plan_revision":
            return await self.dispatch_plan_revision(dict(params))
        if method == "recover_work_order":
            return self.recover_work_order(str(params.get("work_order_id") or ""), str(params.get("reason") or ""))
        if method == "resume_work_order":
            return await self.resume_work_order(
                str(params.get("work_order_id") or ""),
                module_id=str(params.get("module_id") or ""),
                child_work_order_id=str(params.get("child_work_order_id") or ""),
                reason=str(params.get("reason") or ""),
                preferred_endpoint_id=str(params.get("preferred_endpoint_id") or ""),
            )
        if method == "retry_checkpoint_review":
            return await self.reviews.retry_checkpoint_review(
                checkpoint_id=str(params.get("checkpoint_id") or ""),
                work_order_id=str(params.get("work_order_id") or ""),
            )
        if method == "destroy_work_order_run":
            return await self.destroy_work_order_run(str(params.get("work_order_id") or ""), str(params.get("reason") or ""))
        if method == "pause_work_order":
            return self.pause_work_order(str(params.get("work_order_id") or ""), str(params.get("reason") or ""))
        if method == "finish_work_order":
            return self.finish_work_order(str(params.get("work_order_id") or ""), str(params.get("reason") or ""))
        if method == "shutdown":
            return self.request_shutdown(
                reason=str(params.get("reason") or "manager_shutdown"),
                graceful=_coerce_bool(params.get("graceful", True)),
                timeout_seconds=_coerce_float(
                    params.get("timeout_seconds"),
                    default=float(self.graceful_shutdown_timeout_seconds or _DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS),
                ),
            )
        raise ValueError(f"unknown minion manager method: {method}")

    async def _run_v2_outbox(self) -> None:
        while not self._shutdown_event.is_set():
            try:
                started = self.v2_outbox.start_available(
                    max_concurrency=max(1, int(self.max_parallel_modules or 1)) + 8
                )
                if started > 0:
                    await asyncio.sleep(0)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("minion V2 outbox tick failed")
            self._v2_wake_event.clear()
            try:
                await asyncio.wait_for(self._v2_wake_event.wait(), timeout=0.25)
            except TimeoutError:
                pass

    async def _publish_v2_human_review(self, payload: Mapping[str, Any]) -> None:
        if payload.get("standalone_review_id"):
            self.events.queue_event(
                {
                    "event_kind": "work_order_completed",
                    "minion_id": "",
                    "run_id": "",
                    "work_order_id": str(payload.get("workflow_id") or ""),
                    "minion_profile": "minion_v2.verifier",
                    "payload": {**dict(payload), "minion_v2": True, "status": "completed"},
                    "created_at": utc_now(),
                }
            )
            return
        self.events.queue_event(
            {
                "event_kind": "plan_acceptance_pending",
                "minion_id": "",
                "run_id": "",
                "work_order_id": str(payload.get("workflow_id") or ""),
                "minion_profile": "minion_v2.architecture_reviewer",
                "payload": {**dict(payload), "minion_v2": True},
                "created_at": utc_now(),
            }
        )

    async def _publish_v2_worker_event(self, event: Mapping[str, Any]) -> None:
        item = dict(event)
        run_id = str(item.get("run_id") or "")
        state = self.runs.get(run_id)
        payload = dict(item.get("payload") or {})
        if state is not None:
            v2 = dict(state.pack.metadata.get("minion_v2") or {})
            workflow_id = str(v2.get("workflow_id") or "")
            workflow = self.v2_service.repository.read_snapshot(AggregateType.WORKFLOW, workflow_id) if workflow_id else None
            metadata = dict(payload.get("metadata") or {})
            if workflow is not None and workflow.payload.get("control_route"):
                metadata["control_route"] = dict(workflow.payload.get("control_route") or {})
            if metadata:
                payload["metadata"] = metadata
            if str(item.get("event_kind") or "") == "approval_requested":
                state.pending_approval = {**payload, "run_id": run_id, "minion_id": state.minion_id}
                state.status = "approval_pending"
            elif str(item.get("event_kind") or "") == "clarification_requested":
                state.pending_clarification = {**payload, "run_id": run_id, "minion_id": state.minion_id}
                state.status = "clarification_pending"
            state.last_event = {**item, "payload": payload}
            state.last_event_at = str(item.get("created_at") or utc_now())
        self.events.queue_event({**item, "payload": payload})

    def _register_v2_broker_run(
        self,
        run_id: str,
        minion_id: str,
        pack: TaskContextPack,
        process: asyncio.subprocess.Process,
    ) -> None:
        self.runs[run_id] = MinionRunState(
            minion_id=minion_id,
            run_id=run_id,
            pack=pack,
            process=process,
            runner_kind="v2_process",
            status="running",
        )

    def _unregister_v2_broker_run(self, run_id: str) -> None:
        self.runs.pop(run_id, None)

    def request_shutdown(self, *, reason: str = "manager_shutdown", graceful: bool = True, timeout_seconds: float | None = None) -> dict[str, Any]:
        if not self._shutdown_started_at:
            self._shutdown_started_at = utc_now()
        self._shutdown_reason = str(reason or "manager_shutdown")
        self._shutdown_graceful = bool(graceful)
        self._shutdown_timeout_seconds = max(
            0.0,
            float(
                timeout_seconds
                if timeout_seconds is not None
                else self.graceful_shutdown_timeout_seconds
                or _DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS
            ),
        )
        self._draining = True
        self._shutdown_event.set()
        self._notify_logical_slot_available(reason="shutdown")
        return {
            "ok": True,
            "status": "shutdown_requested",
            "reason": self._shutdown_reason,
            "graceful": self._shutdown_graceful,
            "timeout_seconds": self._shutdown_timeout_seconds,
            "started_at": self._shutdown_started_at,
        }

    def health(self) -> dict[str, Any]:
        active = [state for state in self.runs.values() if state.status in _ACTIVE_RUN_STATUSES]
        active_run_summaries = [state.summary() for state in active]
        active_module_count = self._active_module_child_count_from_ledger()
        allocated_logical_slots = self._allocated_logical_slot_count()
        active_llm_node_count = len(self._active_llm_node_slot_keys_from_memory_ledger())
        available_llm_node_slots = max(0, int(self.max_parallel_modules or _DEFAULT_MAX_PARALLEL_LLM_NODES) - active_llm_node_count)
        return {
            "ok": True,
            "health_source": "manager_memory_ledger",
            "ledger_only": True,
            "started_at": self.started_at,
            "run_count": len(self.runs),
            "active_count": len(active),
            "active_runs": active_run_summaries,
            "shutdown_requested": self._shutdown_event.is_set(),
            "draining": self._draining,
            "shutdown_reason": self._shutdown_reason,
            "shutdown_started_at": self._shutdown_started_at,
            "graceful_shutdown_timeout_seconds": float(self.graceful_shutdown_timeout_seconds or _DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS),
            "minion_db_path": str(self.tasking_repository.db_path),
            "max_parallel_llm_nodes": int(self.max_parallel_modules or _DEFAULT_MAX_PARALLEL_LLM_NODES),
            "max_parallel_modules": int(self.max_parallel_modules or _DEFAULT_MAX_PARALLEL_MODULES),
            "auto_resume_ready_modules": bool(self.auto_resume_ready_modules),
            "active_llm_node_count": active_llm_node_count,
            "active_module_count": active_module_count,
            "allocated_logical_slots": allocated_logical_slots,
            "available_llm_node_slots": available_llm_node_slots,
            "available_module_slots": available_llm_node_slots,
            "step_executors": self.step_executor.executor_statuses(),
            "logical_slot_generation": self._logical_slot_generation,
            "pending_event_count": len(self.event_queue),
            "event_subscriber_count": len(self.event_subscribers),
            "log_sink": current_service_log_sink_description(),
            **dict(self.endpoint_info),
        }

    def reload_runtime_config(self) -> dict[str, Any]:
        config = effective_minion_runtime_config(self.runtime_root)
        previous = {
            "max_parallel_llm_nodes": int(self.max_parallel_modules or _DEFAULT_MAX_PARALLEL_LLM_NODES),
            "max_parallel_modules": int(self.max_parallel_modules or _DEFAULT_MAX_PARALLEL_MODULES),
            "auto_resume_ready_modules": bool(self.auto_resume_ready_modules),
        }
        self.max_parallel_modules = max(
            1,
            _coerce_int(
                config.get("max_parallel_llm_nodes", config.get("max_parallel_modules")),
                _DEFAULT_MAX_PARALLEL_LLM_NODES,
            )
            or _DEFAULT_MAX_PARALLEL_LLM_NODES,
        )
        self.auto_resume_ready_modules = _coerce_bool(config.get("auto_resume_ready_modules", True))
        self._notify_logical_slot_available(reason="runtime_config_reload")
        return {
            "ok": True,
            "status": "ok",
            "config": config,
            "previous": previous,
            "max_parallel_llm_nodes": int(self.max_parallel_modules or _DEFAULT_MAX_PARALLEL_LLM_NODES),
            "max_parallel_modules": int(self.max_parallel_modules or _DEFAULT_MAX_PARALLEL_MODULES),
            "auto_resume_ready_modules": bool(self.auto_resume_ready_modules),
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
        specs = [item.to_dict() for item in gate_specs_from_pack(pack)]
        if not specs:
            return pack, {}
        metadata["gate_specs"] = specs
        updates: dict[str, Any] = {"gate_specs": specs}
        if metadata.get("manager_turn_timeout_seconds") is None:
            has_milestone_gate = bool(gate_specs_from_pack(pack, trigger=GATE_TRIGGER_AFTER_EACH_MILESTONE))
            if not has_milestone_gate:
                return TaskContextPack.from_dict({**pack.to_dict(), "metadata": metadata}), updates
            metadata["manager_turn_timeout_seconds"] = _DEFAULT_MANAGER_TURN_TIMEOUT_SECONDS
            updates["manager_turn_timeout_seconds"] = _DEFAULT_MANAGER_TURN_TIMEOUT_SECONDS
        return TaskContextPack.from_dict({**pack.to_dict(), "metadata": metadata}), updates

    async def spawn(self, pack_payload: dict[str, Any]) -> dict[str, Any]:
        if self._shutdown_event.is_set() or self._draining:
            return {
                "status": "shutdown",
                "reason": self._shutdown_reason or "manager_shutdown",
                "summary": "minion manager is shutting down and is not accepting new runs",
            }
        pack = TaskContextPack.from_dict(pack_payload)
        profile_registry = MinionProfileRegistry(runtime_root=self.runtime_root)
        pack = profile_registry.resolve_pack(pack)
        pack = self.tasking_repository.prepare_pack_for_spawn(pack)
        pack = profile_registry.resolve_pack(pack)
        if _is_plan_parent_pack(pack):
            ticked = await self.tick_parent_dag(pack.work_order_id, reason="plan_parent_spawn")
            child_runs = _runs_from_dag_tick(ticked)
            first_run = child_runs[0] if child_runs else {}
            return {
                "work_order_id": pack.work_order_id,
                "minion_profile": pack.minion_profile,
                "status": str(ticked.get("status") or "ok"),
                "run_id": str(first_run.get("run_id") or ""),
                "minion_id": str(first_run.get("minion_id") or ""),
                "plan_parent": True,
                "dag_tick": ticked,
                "continuation": {
                    "run": first_run,
                    "runs": child_runs,
                },
                "child_runs": child_runs,
                "child_run_ids": [str(item.get("run_id") or "") for item in child_runs if str(item.get("run_id") or "").strip()],
                "child_work_order_id": str(ticked.get("child_work_order_id") or ""),
                "child_work_order_ids": [str(item) for item in list(ticked.get("child_work_order_ids") or []) if str(item).strip()],
                "module_id": str(ticked.get("module_id") or ""),
                "module_ids": [str(item) for item in list(ticked.get("module_ids") or []) if str(item).strip()],
            }
        pack, metadata_updates = self._with_profile_gate_policy(pack)
        if metadata_updates:
            self.tasking_repository.merge_work_order_metadata(pack.work_order_id, metadata_updates)
        pre_plan_result = await self._maybe_spawn_pre_plan_contract_compiler(pack)
        if pre_plan_result:
            return pre_plan_result
        slot_admission = self._admit_llm_node_for_spawn(pack)
        if str(slot_admission.get("status") or "") != "admitted":
            return slot_admission
        pack = self._inject_skill_manual_context(pack)
        minion_id = f"minion_{uuid4().hex[:10]}"
        run_id = f"run_{uuid4().hex[:12]}"
        metadata = dict(pack.metadata)
        metadata["run_id"] = run_id
        metadata["minion_id"] = minion_id
        metadata["llm_node_slot"] = dict(slot_admission)
        pack = TaskContextPack.from_dict({**pack.to_dict(), "metadata": metadata})
        try:
            pack = prepare_task_workspace(self.runtime_root, pack, run_id=run_id)
            pack = self._with_staged_plan_builder_seed(pack)
            pack = await self._with_lsp_prewarm(pack)
            self.tasking_repository.update_work_order_workspace(pack.work_order_id, dict(pack.workspace))
            self.tasking_repository.merge_work_order_metadata(pack.work_order_id, dict(pack.metadata or {}))
            pack = self._with_runner_debug_log(pack)
            debug_log = dict((pack.metadata or {}).get("debug_log") or {})
            if bool(debug_log.get("enabled")):
                self.tasking_repository.merge_work_order_metadata(pack.work_order_id, {"debug_log": debug_log})
            pack = sanitize_runner_session_pack(pack)
            pack = with_minion_sandbox_metadata(self.runtime_root, pack, run_id=run_id)
        except Exception as exc:
            self._record_spawn_failure(pack, exc, phase="workspace_preparation")
            raise
        state = MinionRunState(minion_id=minion_id, run_id=run_id, pack=pack)
        async with self._lock:
            slot_admission = self._admit_llm_node_for_spawn(pack)
            if str(slot_admission.get("status") or "") != "admitted":
                return slot_admission
            metadata = dict(pack.metadata)
            metadata["llm_node_slot"] = dict(slot_admission)
            pack = TaskContextPack.from_dict({**pack.to_dict(), "metadata": metadata})
            state.pack = pack
            self.runs[run_id] = state
        try:
            await self._start_runner(state)
        except Exception as exc:
            self._transition_run_status(state, "blocked")
            state.ended_at = utc_now()
            self._record_spawn_failure(state.pack, exc, phase="runner_dispatch")
            raise
        self._record_event(
            state,
            {
                "event_kind": "phase_started",
                "payload": {"phase": "spawned", "summary": "minion runner spawned"},
                "created_at": utc_now(),
            },
        )
        return state.summary()

    def _record_spawn_failure(self, pack: TaskContextPack, exc: Exception, *, phase: str) -> None:
        error = f"{exc.__class__.__name__}: {exc}"
        blocker = {
            "reason": "spawn_failed",
            "phase": str(phase or "spawn"),
            "summary": "minion spawn failed before runner execution",
            "error": error,
            "error_type": exc.__class__.__name__,
            "work_order_id": pack.work_order_id,
            "minion_profile": pack.minion_profile,
            "updated_at": utc_now(),
        }
        metadata = dict(pack.metadata or {})
        updates: dict[str, Any] = {"spawn_failure": blocker}
        workflow = dict(metadata.get("workflow") or {}) if isinstance(metadata.get("workflow"), dict) else {}
        if workflow:
            workflow.update({"status": "blocked", "blocker": blocker, "updated_at": utc_now()})
            steps = [dict(item) for item in list(workflow.get("steps") or []) if isinstance(item, dict)]
            current_step_id = str(workflow.get("current_step_id") or "")
            if steps:
                target_index = next(
                    (index for index, item in enumerate(steps) if str(item.get("step_id") or "") == current_step_id),
                    len(steps) - 1,
                )
                steps[target_index] = {**steps[target_index], "status": "blocked", "blocker": blocker}
                workflow["steps"] = steps
            updates["workflow"] = workflow
        plan_review = dict(metadata.get("plan_review") or {}) if isinstance(metadata.get("plan_review"), dict) else {}
        if str(plan_review.get("status") or "") == "revision_in_progress":
            plan_review.update(
                {
                    "status": "revision_blocked",
                    "next_action": "retry_revision",
                    "blocker": blocker,
                    "updated_at": utc_now(),
                }
            )
            updates["plan_review"] = plan_review
        self.tasking_repository.merge_work_order_metadata(pack.work_order_id, updates, work_order_status="blocked")
        event = {
            "event_kind": "terminal",
            "minion_id": str(metadata.get("minion_id") or ""),
            "run_id": str(metadata.get("run_id") or ""),
            "work_order_id": pack.work_order_id,
            "minion_profile": pack.minion_profile,
            "payload": {
                "status": "blocked",
                "summary": "minion spawn failed before runner execution",
                "reason": "spawn_failed",
                "phase": str(phase or "spawn"),
                "error": error,
                "error_type": exc.__class__.__name__,
                "force_ledger": True,
            },
            "created_at": utc_now(),
        }
        self._queue_event_delivery(event)
        self.tasking_repository.record_minion_event(event)

    async def _maybe_spawn_pre_plan_contract_compiler(self, pack: TaskContextPack) -> dict[str, Any]:
        spec = _pre_plan_gate_spec(pack)
        if spec is None:
            return {}
        if _is_pre_plan_contract_compiler_pack(pack):
            return {}
        contract_state = _pre_plan_contract_state(pack)
        if _gate_contract_from_pack(pack):
            return {}
        status = str(contract_state.get("status") or "").strip().lower()
        if status == "running":
            return {
                "work_order_id": pack.work_order_id,
                "minion_profile": pack.minion_profile,
                "status": "pre_plan_contract_running",
                "planner_deferred": True,
                "pre_plan_contract": contract_state,
            }
        compiler_pack = _build_pre_plan_contract_compiler_pack(pack, spec)
        compiler_work_order_id = compiler_pack.work_order_id
        running_state = {
            "status": "running",
            "summary": "pre-plan source contract compiler spawned",
            "compiler_work_order_id": compiler_work_order_id,
            "gate_spec": spec.to_dict(),
            "updated_at": utc_now(),
        }
        self.tasking_repository.merge_work_order_metadata(
            pack.work_order_id,
            {
                "pre_plan_contract": running_state,
                "pre_plan_contract_source_pack": pack.to_dict(),
            },
        )
        try:
            spawned = await self.spawn(compiler_pack.to_dict())
        except Exception as exc:
            failed = {
                **running_state,
                "status": "spawn_failed",
                "summary": "manager failed to spawn pre-plan source contract compiler",
                "error": f"{exc.__class__.__name__}: {exc}",
                "updated_at": utc_now(),
            }
            self.tasking_repository.merge_work_order_metadata(pack.work_order_id, {"pre_plan_contract": failed})
            raise
        event = {
            "event_kind": "pre_plan_contract_started",
            "minion_id": str(spawned.get("minion_id") or ""),
            "run_id": str(spawned.get("run_id") or ""),
            "work_order_id": pack.work_order_id,
            "minion_profile": str(spawned.get("minion_profile") or compiler_pack.minion_profile),
            "payload": {
                "status": "running",
                "summary": "pre-plan source contract compiler spawned; planner is deferred until the contract is compiled",
                "compiler_work_order_id": compiler_work_order_id,
                "compiler_run_id": str(spawned.get("run_id") or ""),
                "gate_spec": spec.to_dict(),
            },
            "created_at": utc_now(),
        }
        if self._should_queue_event_delivery(event):
            self._queue_event_delivery(event)
        self.tasking_repository.record_minion_event(event)
        return {
            "work_order_id": pack.work_order_id,
            "minion_profile": pack.minion_profile,
            "status": "pre_plan_contract_started",
            "planner_deferred": True,
            "pre_plan_contract": dict(event["payload"]),
            "contract_compiler": spawned,
        }

    async def _with_lsp_prewarm(self, pack: TaskContextPack) -> TaskContextPack:
        metadata = dict(pack.metadata or {})
        workspace = dict(pack.workspace or {})
        if bool(metadata.get("lsp_prewarm_disabled")) or bool(workspace.get("lsp_prewarm_disabled")):
            return pack
        try:
            result = await asyncio.to_thread(
                prewarm_workspace_lsp,
                runtime_root=self.runtime_root,
                workspace=workspace,
            )
        except Exception as exc:
            result = {
                "status": "error",
                "error": f"{exc.__class__.__name__}: {exc}",
            }
        if str(result.get("status") or "") == "skipped":
            return pack
        lsp_setup = dict(workspace.get("lsp_setup") or {})
        lsp_setup["prewarm"] = result
        workspace["lsp_setup"] = lsp_setup
        return TaskContextPack.from_dict({**pack.to_dict(), "workspace": workspace})

    def _with_staged_plan_builder_seed(self, pack: TaskContextPack) -> TaskContextPack:
        metadata = dict(pack.metadata or {})
        staged = dict(metadata.get("staged_planning") or {})
        stage = str(staged.get("stage") or pack.workspace.get("plan_builder_stage") or "").strip()
        if stage not in {"architecture_sketch", "module_detail"}:
            return pack
        workspace = dict(pack.workspace or {})
        workspace.setdefault("task_id", str(metadata.get("task_id") or ""))
        workspace.setdefault("goal", str(pack.goal or pack.instruction or ""))
        workspace.setdefault("planning_depth", str(staged.get("planning_depth") or metadata.get("planning_depth") or "sketch_only"))
        workspace["plan_builder_stage"] = stage
        if stage == "module_detail":
            module_id = str(staged.get("module_id") or workspace.get("plan_builder_module_id") or "").strip()
            if module_id:
                workspace["plan_builder_module_id"] = module_id
                workspace["bound_module_id"] = module_id
        try:
            seed = initialize_plan_builder_stage_draft(workspace, metadata)
        except Exception as exc:
            staged["seed_error"] = f"{exc.__class__.__name__}: {exc}"
            metadata["staged_planning"] = staged
            return TaskContextPack.from_dict({**pack.to_dict(), "workspace": workspace, "metadata": metadata})
        if not seed:
            return TaskContextPack.from_dict({**pack.to_dict(), "workspace": workspace, "metadata": metadata})
        staged.update(seed)
        metadata["staged_planning"] = staged
        workspace["plan_builder_plan_handle"] = str(seed.get("plan_handle") or "")
        workspace["planning_depth"] = str(seed.get("planning_depth") or workspace.get("planning_depth") or "sketch_only")
        if str(seed.get("module_id") or "").strip():
            workspace["plan_builder_module_id"] = str(seed.get("module_id") or "").strip()
            workspace["bound_module_id"] = str(seed.get("module_id") or "").strip()
        planner_work_order = dict(metadata.get("planner_work_order") or {})
        planning_requirements = dict(planner_work_order.get("planning_requirements") or {})
        planning_requirements["bound_plan_handle"] = workspace["plan_builder_plan_handle"]
        if str(workspace.get("plan_builder_module_id") or "").strip():
            planning_requirements["bound_module_id"] = str(workspace.get("plan_builder_module_id") or "")
        planner_work_order["planning_requirements"] = planning_requirements
        if planner_work_order:
            metadata["planner_work_order"] = planner_work_order
            if isinstance(metadata.get("architect_work_order"), dict):
                metadata["architect_work_order"] = dict(planner_work_order)
        return TaskContextPack.from_dict({**pack.to_dict(), "workspace": workspace, "metadata": metadata})

    async def kill(self, run_id: str, reason: str = "") -> dict[str, Any]:
        state = self._require_run(run_id)
        already_terminal = state.status not in _ACTIVE_RUN_STATUSES
        await self.step_executor.stop_runner(state)
        if state.status not in _ACTIVE_RUN_STATUSES:
            return state.summary()
        if already_terminal:
            return state.summary()
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
        result = self.tasking_repository.recover_stale_running_modules(
            active_child_work_order_ids=self._active_runner_work_order_ids(),
            work_order_id=str(work_order_id or ""),
            reason=reason or "manager recovered stale running module",
        )
        resumes: list[dict[str, Any]] = []
        for item in list(result.get("recovered") or []):
            if not isinstance(item, dict):
                continue
            module_id = str(item.get("module_id") or "").strip()
            child_id = str(item.get("child_work_order_id") or "").strip()
            parent_id = str(item.get("parent_work_order_id") or "").strip()
            if not parent_id or not child_id:
                continue
            resumed = self.tasking_repository.resume_plan_module(
                parent_id,
                module_id=module_id,
                child_work_order_id=child_id,
                reason=reason or "manager recovered stale running module",
            )
            if str(resumed.get("status") or "") != "skipped":
                resumes.append(dict(resumed))
        if resumes:
            return {**dict(result), "resumes": resumes, "resume_count": len(resumes)}
        return result

    async def resume_work_order(
        self,
        work_order_id: str = "",
        *,
        module_id: str = "",
        child_work_order_id: str = "",
        reason: str = "",
        preferred_endpoint_id: str = "",
    ) -> dict[str, Any]:
        self._reconcile_runs()
        staged_resume = await self._resume_staged_planning_post_processing(
            work_order_id,
            reason=reason or "manager resumed staged planning post-processing",
            preferred_endpoint_id=preferred_endpoint_id,
        )
        if staged_resume is not None:
            return staged_resume
        resumed = self.tasking_repository.resume_plan_module(
            work_order_id,
            module_id=module_id,
            child_work_order_id=child_work_order_id,
            reason=reason or "manager resumed module cursor",
        )
        if not _should_try_workflow_resume_after_module_resume(resumed):
            if preferred_endpoint_id and str(resumed.get("status") or "").strip().lower() not in {"invalid", "not_found", "skipped"}:
                parent_id = str(resumed.get("parent_work_order_id") or resumed.get("work_order_id") or work_order_id).strip()
                if parent_id:
                    endpoint_override = self.tasking_repository.merge_work_order_metadata(
                        parent_id,
                        {
                            "preferred_endpoint_id": str(preferred_endpoint_id).strip(),
                            "preferred_endpoint_source": "resume_work_order",
                        },
                    )
                    return {**dict(resumed), "endpoint_override": endpoint_override}
            return resumed
        workflow_resume = await self._resume_workflow_step(
            work_order_id,
            reason=reason or "manager resumed workflow step",
            preferred_endpoint_id=preferred_endpoint_id,
        )
        if str(workflow_resume.get("status") or "") == "skipped" and str(workflow_resume.get("reason") or "") == "not_workflow":
            return resumed
        return {**dict(workflow_resume), "module_resume": resumed}

    async def _resume_staged_planning_post_processing(
        self,
        work_order_id: str,
        *,
        reason: str,
        preferred_endpoint_id: str = "",
    ) -> dict[str, Any] | None:
        target = str(work_order_id or "").strip()
        if not target:
            return {"status": "invalid", "error": "work_order_id is required"}
        snapshot = self.tasking_repository.read_work_order(target)
        if snapshot.get("status") != "ok":
            return {"status": "not_found", "work_order_id": target}
        work_order = dict(snapshot.get("work_order") or {})
        metadata = dict(work_order.get("metadata") or {})
        staged = dict(metadata.get("staged_planning") or {})
        blocker = dict(staged.get("blocker") or {})
        if (
            str(staged.get("status") or "").strip().lower() != "blocked"
            or str(blocker.get("reason") or "").strip() != "staged_planning_post_failed"
        ):
            return None
        expected = [str(item).strip() for item in list(staged.get("module_ids") or []) if str(item).strip()]
        detail_modules = {
            str(key): dict(value)
            for key, value in dict(staged.get("detail_modules") or {}).items()
            if isinstance(value, dict)
        }
        if not expected or any(
            str((detail_modules.get(module_id) or {}).get("status") or "").strip().lower() != "completed"
            for module_id in expected
        ):
            return None

        now = utc_now()
        resume_history = [dict(item) for item in list(staged.get("resume_history") or []) if isinstance(item, dict)]
        resume_history.append(
            {
                "stage": "final_plan_compile",
                "reason": reason,
                "previous_blocker": blocker,
                "resumed_at": now,
            }
        )
        staged.update(
            {
                "status": "final_compiling",
                "blocked_stage": None,
                "blocker": None,
                "resume_history": resume_history,
                "updated_at": now,
            }
        )
        workflow = dict(metadata.get("workflow") or {})
        if workflow:
            workflow.update({"status": "final_compiling", "blocker": None, "updated_at": now})
        updates: dict[str, Any] = {
            "staged_planning": staged,
            **({"workflow": workflow} if workflow else {}),
        }
        if preferred_endpoint_id:
            updates.update(
                {
                    "preferred_endpoint_id": str(preferred_endpoint_id).strip(),
                    "preferred_endpoint_source": "resume_work_order",
                }
            )
        self.tasking_repository.merge_work_order_metadata(target, updates, work_order_status="active")
        try:
            compiled_plan_ref = dict(staged.get("compiled_plan_ref") or {})
            compiled_plan_validation = dict(staged.get("compiled_plan_validation") or {})
            if compiled_plan_ref and compiled_plan_validation:
                await self._schedule_compiled_plan_review(target, compiled_plan_ref, compiled_plan_validation)
                staged.update({"status": "final_plan_compiled", "updated_at": utc_now()})
                if workflow:
                    workflow.update({"status": "post_gate_pending", "post_gate": "plan_acceptance", "updated_at": utc_now()})
                self.tasking_repository.merge_work_order_metadata(
                    target,
                    {"staged_planning": staged, **({"workflow": workflow} if workflow else {})},
                    work_order_status="active",
                )
                resumed_stage = "plan_acceptance"
            else:
                await self._compile_and_review_staged_plan(target)
                resumed_stage = "final_plan_compile"
        except Exception as exc:
            self.logger.exception("staged planning post resume failed: %s", target)
            parent_pack = self.tasking_repository.pack_for_work_order(target)
            await self._mark_staged_planning_blocked(
                MinionRunState(minion_id="", run_id="", pack=parent_pack),
                stage="final_plan_compile",
                reason="staged_planning_post_failed",
                summary=f"{exc.__class__.__name__}: {exc}",
            )
            return {
                "status": "blocked",
                "reason": "staged_plan_post_resume_failed",
                "work_order_id": target,
                "error": f"{exc.__class__.__name__}: {exc}",
            }
        return {
            "status": "resumed",
            "reason": "staged_plan_post_resume",
            "work_order_id": target,
            "stage": resumed_stage,
            "detail_modules_reused": list(expected),
        }

    async def _resume_workflow_step(
        self,
        work_order_id: str,
        *,
        reason: str = "",
        preferred_endpoint_id: str = "",
    ) -> dict[str, Any]:
        target = str(work_order_id or "").strip()
        if not target:
            return {"status": "invalid", "error": "work_order_id is required"}
        snapshot = self.tasking_repository.read_work_order(target)
        if snapshot.get("status") != "ok":
            return {"status": "not_found", "work_order_id": target}
        active_state = self._find_active_run_for_work_order(target)
        if active_state is not None:
            return {
                "status": "skipped",
                "reason": "workflow_step_already_running",
                "work_order_id": target,
                "run_id": active_state.run_id,
            }
        work_order = dict(snapshot.get("work_order") or {})
        metadata = dict(work_order.get("metadata") or {})
        workflow = dict(metadata.get("workflow") or {})
        if not workflow:
            return {"status": "skipped", "reason": "not_workflow", "work_order_id": target}
        steps = [dict(item) for item in list(workflow.get("steps") or []) if isinstance(item, dict)]
        step_index = _current_workflow_resume_step_index(workflow, steps)
        if step_index < 0:
            return {"status": "skipped", "reason": "workflow_missing_current_step", "work_order_id": target}
        step = dict(steps[step_index])
        profile = str(step.get("profile") or workflow.get("current_profile") or work_order.get("minion_profile") or "").strip()
        if not profile or profile == NONE_PROFILE:
            return {
                "status": "skipped",
                "reason": "workflow_step_has_no_resumable_profile",
                "work_order_id": target,
                "step_id": str(step.get("step_id") or ""),
            }
        workflow_status = str(workflow.get("status") or "").strip().lower()
        step_status = str(step.get("status") or "").strip().lower()
        work_order_status = str(work_order.get("status") or "").strip().lower()
        if not ({workflow_status, step_status, work_order_status} & _WORKFLOW_RESUMABLE_STATUSES):
            return {
                "status": "skipped",
                "reason": "workflow_not_blocked",
                "work_order_id": target,
                "workflow_status": workflow_status,
                "step_status": step_status,
                "work_order_status": work_order_status,
            }
        metadata, workflow, resume_entry = _workflow_resume_metadata(
            metadata,
            workflow,
            step_index=step_index,
            reason=reason or "manager resumed workflow step",
            work_order_status=work_order_status,
            preferred_endpoint_id=preferred_endpoint_id,
        )
        group, name = split_profile_ref(profile)
        if _is_architect_workflow_step(profile, metadata):
            metadata = _with_architect_workflow_resume_metadata(
                metadata,
                goal=str(work_order.get("goal") or work_order.get("instruction") or ""),
                work_order_id=target,
                reason=reason or "manager resumed workflow step",
            )
        metadata["workflow"] = workflow
        metadata["prompt_view"] = None
        pack = self.tasking_repository.pack_for_work_order(
            target,
            overrides={
                "instruction": f"Resume workflow step {profile} after an interrupted or blocked attempt.",
                "profile_group": group or "general",
                "profile_name": name,
                "minion_profile": profile,
                "metadata": metadata,
            },
        )
        spawned = await self.spawn(pack.to_dict())
        event = {
            "event_kind": "workflow_step_resumed",
            "minion_id": str(spawned.get("minion_id") or ""),
            "run_id": str(spawned.get("run_id") or ""),
            "work_order_id": target,
            "minion_profile": profile,
            "payload": {
                "status": "spawned",
                "reason": reason or "manager resumed workflow step",
                "step_id": str(resume_entry.get("step_id") or ""),
                "profile": profile,
                "resume": dict(resume_entry),
                "run": dict(spawned),
            },
            "created_at": utc_now(),
        }
        if self._should_queue_event_delivery(event):
            self._queue_event_delivery(event)
        self.tasking_repository.record_minion_event(event)
        return {
            "status": "resumed",
            "reason": "workflow_step_resume",
            "work_order_id": target,
            "step_id": str(resume_entry.get("step_id") or ""),
            "minion_profile": profile,
            "run": dict(spawned),
        }

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
        if state.runner_kind == "v2_process":
            sent = await self.v2_semantic_worker.send_worker_control(
                state.run_id,
                {"type": "decision", "decision": decision.to_dict()},
            )
            if not sent:
                raise RuntimeError("V2 worker is no longer available for approval")
            state.pending_approval = {}
            state.status = "running"
            return {"ok": True, "run": state.summary(), "decision": decision.to_dict()}
        await self._send_runner_control(state, {"type": "decision", "decision": decision.to_dict()})
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
        response = dict(payload)
        response.setdefault("run_id", state.run_id)
        response.setdefault("minion_id", state.minion_id)
        response.setdefault("work_order_id", state.pack.work_order_id)
        if state.runner_kind == "v2_process":
            sent = await self.v2_semantic_worker.send_worker_control(
                state.run_id,
                {"type": "clarification", "clarification": response},
            )
            if not sent:
                raise RuntimeError("V2 worker is no longer available for clarification")
            state.pending_clarification = {}
            state.status = "running"
            return {"ok": True, "run": state.summary(), "clarification": response}
        await self._send_runner_control(state, {"type": "clarification", "clarification": response})
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
        if state.control_queue is not None:
            await state.control_queue.put(dict(message))
            return {"ok": True, "run_id": state.run_id, "message_type": str(message.get("type") or "")}
        return await self.step_executor.send_runner_control(state, message)

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
        if self._should_queue_event_delivery(event):
            self._queue_event_delivery(event)
        self.tasking_repository.record_minion_event(event)
        return {"status": "ok" if result.get("status") in {"committed", "no_changes"} else "error", "work_order_id": work_order_id, **result}

    async def tick_parent_dag(self, work_order_id: str, *, reason: str = "") -> dict[str, Any]:
        return await self.step_runner.tick_parent_dag(work_order_id, reason=reason)

    async def tick_ready_plan_dags(self, *, preferred_work_order_id: str = "", reason: str = "") -> dict[str, Any]:
        return await self.step_runner.tick_ready_plan_dags(preferred_work_order_id=preferred_work_order_id, reason=reason)

    def request_logical_slot(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = str(params.get("run_id") or "").strip()
        work_order_id = str(params.get("work_order_id") or "").strip()
        if not work_order_id and run_id in self.runs:
            work_order_id = str(self.runs[run_id].pack.work_order_id or "").strip()
        if not run_id:
            raise ValueError("run_id is required")
        if not work_order_id:
            raise ValueError("work_order_id is required")
        state = SimpleNamespace(run_id=run_id, pack=TaskContextPack(work_order_id=work_order_id, goal="logical slot owner"))
        return self._request_logical_slot(
            state,
            resource=str(params.get("resource") or "logical_minion_slot"),
            module_id=str(params.get("module_id") or ""),
            reason=str(params.get("reason") or "manager_rpc"),
        )

    async def wait_logical_slot(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = str(params.get("run_id") or "").strip()
        work_order_id = str(params.get("work_order_id") or "").strip()
        if not work_order_id and run_id in self.runs:
            work_order_id = str(self.runs[run_id].pack.work_order_id or "").strip()
        if not run_id:
            raise ValueError("run_id is required")
        if not work_order_id:
            raise ValueError("work_order_id is required")
        state = SimpleNamespace(run_id=run_id, pack=TaskContextPack(work_order_id=work_order_id, goal="logical slot waiter"))
        return await self._wait_logical_slot(
            state,
            resource=str(params.get("resource") or "logical_minion_slot"),
            module_id=str(params.get("module_id") or ""),
            reason=str(params.get("reason") or "manager_rpc"),
            timeout_seconds=params.get("timeout_seconds"),
        )

    def release_logical_slot(self, params: dict[str, Any]) -> dict[str, Any]:
        slot_id = str(params.get("slot_id") or "").strip()
        run_id = str(params.get("run_id") or "").strip()
        return self._release_logical_slot(slot_id, run_id=run_id, reason=str(params.get("reason") or "manager_rpc"))

    async def dispatch_accepted_plan(self, params: dict[str, Any]) -> dict[str, Any]:
        work_order_id = str(params.get("work_order_id") or "").strip()
        if not work_order_id:
            raise ValueError("work_order_id is required")
        plan_ref = params.get("plan_ref")
        if not plan_ref:
            raise ValueError("plan_ref is required")
        loaded = self.tasking_repository.load_accepted_plan_ref(plan_ref)
        snapshot = self.tasking_repository.read_work_order(work_order_id)
        if snapshot.get("status") != "ok":
            raise KeyError(f"unknown work order: {work_order_id}")
        work_order = dict(snapshot.get("work_order") or {})
        source_metadata = dict(work_order.get("metadata") or {})
        workspace = dict(source_metadata.get("workspace") or {})
        workflow = dict(source_metadata.get("workflow") or {})
        plan_review = dict(source_metadata.get("plan_review") or {})
        source_pack = self.tasking_repository.pack_for_work_order(work_order_id)
        source_pack = MinionProfileRegistry(runtime_root=self.runtime_root).resolve_pack(source_pack)
        plan_artifact = dict(loaded.get("plan_artifact") or {}) if isinstance(loaded.get("plan_artifact"), dict) else {}
        next_step = resolve_workflow_next(
            source_pack,
            {"plan_ref": dict(loaded.get("plan_ref") or {}), "plan_artifact": plan_artifact},
        )
        if next_step.get("status") != "ok":
            raise ValueError(str(next_step.get("reason") or "workflow next profile is invalid"))
        next_profile = str(next_step.get("next_profile") or NONE_PROFILE)
        next_group, next_name = split_profile_ref(next_profile)
        if next_profile == NONE_PROFILE:
            workflow = update_current_workflow_step(
                workflow,
                status="completed",
                output_artifact={"plan_ref": dict(loaded.get("plan_ref") or {})},
                next_profile=NONE_PROFILE,
            )
            workflow.update({"status": "completed", "accepted_plan_ref": dict(loaded.get("plan_ref") or {}), "updated_at": utc_now()})
            self.tasking_repository.merge_work_order_metadata(work_order_id, {"workflow": workflow}, work_order_status="completed")
            return {
                "status": "completed",
                "work_order_id": work_order_id,
                "plan_ref": dict(loaded.get("plan_ref") or {}),
                "next_profile": NONE_PROFILE,
            }
        if not next_group or not next_name or next_name == NONE_PROFILE:
            raise ValueError("accepted plan workflow_next must resolve to a concrete executor profile")
        auto_advance = params.get("auto_advance_modules")
        if auto_advance is None:
            auto_advance = workflow.get("auto_advance_modules", plan_review.get("auto_advance_modules", True))
        profile_family = str(workflow.get("profile_family") or source_metadata.get("profile_family") or next_group).strip() or next_group
        workflow.setdefault("profile_family", profile_family)
        workflow = update_current_workflow_step(
            workflow,
            status="accepted",
            output_artifact={"plan_ref": dict(loaded.get("plan_ref") or {})},
            next_profile=next_profile,
        )
        workflow = append_workflow_step(
            workflow,
            profile=next_profile,
            input_artifact={"plan_ref": dict(loaded.get("plan_ref") or {})},
            adapter=str(next_step.get("adapter") or "accepted_plan"),
        )
        workflow.update(
            {
                "status": "executing",
                "accepted_plan_ref": dict(loaded.get("plan_ref") or {}),
                "next_profile": next_profile,
                "updated_at": utc_now(),
            }
        )
        plan_review.update(
            {
                "status": "accepted",
                "plan_ref": dict(loaded.get("plan_ref") or {}),
                "review_gate_ref": dict(params.get("review_gate_ref") or plan_review.get("review_gate_ref") or {}),
                "accepted_at": utc_now(),
                "next_action": "dispatch_plan_parent",
            }
        )
        metadata = {
            "task_id": str(source_metadata.get("task_id") or work_order.get("task_id") or ""),
            "project_name": str(source_metadata.get("project_name") or workspace.get("project_name") or ""),
            "task_title": str(source_metadata.get("task_title") or work_order.get("title") or ""),
            "work_order_title": str(source_metadata.get("work_order_title") or work_order.get("title") or "Plan implementation"),
            "workflow": workflow,
            "plan_review": plan_review,
            "plan_ref": dict(loaded.get("plan_ref") or {}),
            "plan_validation": dict(loaded.get("plan_validation") or {}),
            "profile_family": profile_family,
            "dispatch_profile_group": next_group,
            "dispatch_profile_name": next_name,
            "plan_execution": {"auto_advance_modules": bool(auto_advance)},
        }
        for key in (
            "control_route",
            "preferred_endpoint_id",
            "preferred_endpoint_source",
            "minion_debug_log_enabled",
            "debug_log",
            "prompt_observation_tag",
            "approval_policy",
        ):
            if key in source_metadata:
                metadata[key] = source_metadata[key]
        parent_pack = self.tasking_repository.build_plan_parent_pack_from_plan(
            dict(loaded.get("plan_artifact") or {}),
            work_order_id=work_order_id,
            workspace=workspace,
            metadata=metadata,
            goal=str(work_order.get("goal") or ""),
            instruction="Execute the accepted structured plan one module at a time.",
            profile_group=next_group,
            profile_name=next_name,
        )
        spawned = await self.spawn(parent_pack.to_dict())
        event = {
            "event_kind": "plan_dispatch_started",
            "minion_id": str((spawned.get("continuation") or {}).get("run", {}).get("minion_id") or ""),
            "run_id": str((spawned.get("continuation") or {}).get("run", {}).get("run_id") or ""),
            "work_order_id": work_order_id,
            "minion_profile": str((spawned.get("continuation") or {}).get("run", {}).get("minion_profile") or ""),
            "payload": {
                "status": "dispatched",
                "plan_ref": dict(loaded.get("plan_ref") or {}),
                "next_profile": next_profile,
                "auto_advance_modules": bool(auto_advance),
                "dispatch": dict(spawned),
            },
            "created_at": utc_now(),
        }
        self._queue_event_delivery(event)
        self.tasking_repository.record_minion_event(event)
        return {
            "status": "dispatched",
            "work_order_id": work_order_id,
            "plan_ref": dict(loaded.get("plan_ref") or {}),
            "next_profile": next_profile,
            "auto_advance_modules": bool(auto_advance),
            "dispatch": dict(spawned),
        }

    async def dispatch_plan_revision(self, params: dict[str, Any]) -> dict[str, Any]:
        source_work_order_id = str(params.get("work_order_id") or "").strip()
        if not source_work_order_id:
            raise ValueError("work_order_id is required")
        plan_ref = params.get("plan_ref")
        if not plan_ref:
            raise ValueError("plan_ref is required")
        snapshot = self.tasking_repository.read_work_order(source_work_order_id)
        if snapshot.get("status") != "ok":
            raise KeyError(f"unknown work order: {source_work_order_id}")
        work_order = dict(snapshot.get("work_order") or {})
        source_metadata = dict(work_order.get("metadata") or {})
        workspace = dict(source_metadata.get("workspace") or {})
        metadata = {
            "source_work_order_id": source_work_order_id,
            "task_title": str(source_metadata.get("task_title") or work_order.get("title") or ""),
            "control_route": dict(source_metadata.get("control_route") or {}) if isinstance(source_metadata.get("control_route"), dict) else {},
            "plan_review": {
                **dict(source_metadata.get("plan_review") or {}),
                "source_work_order_id": source_work_order_id,
            },
        }
        for key in (
            "preferred_endpoint_id",
            "preferred_endpoint_source",
            "minion_debug_log_enabled",
            "debug_log",
            "prompt_observation_tag",
        ):
            if key in source_metadata:
                metadata[key] = source_metadata[key]
        review_gate_ref = params.get("review_gate_ref")
        pack: TaskContextPack
        if review_gate_ref:
            try:
                pack = self.tasking_repository.build_planner_revision_pack_from_review_gate(
                    review_gate_ref,
                    workspace=workspace,
                    metadata=metadata,
                    goal=str(params.get("goal") or ""),
                    instruction=str(params.get("instruction") or ""),
                )
            except ValueError:
                pack = self.tasking_repository.build_planner_revision_pack_from_plan_decision(
                    plan_ref,
                    workspace=workspace,
                    metadata=metadata,
                    reason=str(params.get("reason") or ""),
                    edit_instruction=str(params.get("edit_instruction") or ""),
                )
        else:
            pack = self.tasking_repository.build_planner_revision_pack_from_plan_decision(
                plan_ref,
                workspace=workspace,
                metadata=metadata,
                reason=str(params.get("reason") or ""),
                edit_instruction=str(params.get("edit_instruction") or ""),
            )
        try:
            spawned = await self.spawn(pack.to_dict())
        except Exception as exc:
            failure = {
                "status": "error",
                "reason": "revision_spawn_failed",
                "summary": "manager failed to spawn plan revision architect",
                "error": f"{exc.__class__.__name__}: {exc}",
                "error_type": exc.__class__.__name__,
                "work_order_id": source_work_order_id,
                "revision_work_order_id": pack.work_order_id,
                "source_plan_ref": dict(plan_ref) if isinstance(plan_ref, dict) else plan_ref,
            }
            event = {
                "event_kind": "plan_revision_spawn_failed",
                "minion_id": "",
                "run_id": "",
                "work_order_id": source_work_order_id,
                "minion_profile": pack.minion_profile,
                "payload": {**failure, "force_ledger": True},
                "created_at": utc_now(),
            }
            self._queue_event_delivery(event)
            self.tasking_repository.record_minion_event(event)
            return failure
        event = {
            "event_kind": "plan_revision_spawned",
            "minion_id": str(spawned.get("minion_id") or ""),
            "run_id": str(spawned.get("run_id") or ""),
            "work_order_id": source_work_order_id,
            "minion_profile": str(spawned.get("minion_profile") or pack.minion_profile),
            "payload": {
                "status": "spawned",
                "source_plan_ref": dict(plan_ref) if isinstance(plan_ref, dict) else plan_ref,
                "revision_work_order_id": pack.work_order_id,
                "revision_run": dict(spawned),
            },
            "created_at": utc_now(),
        }
        self._queue_event_delivery(event)
        self.tasking_repository.record_minion_event(event)
        return {
            "status": "spawned",
            "work_order_id": source_work_order_id,
            "revision_work_order_id": pack.work_order_id,
            "run": dict(spawned),
        }

    async def auto_tick_parent_dag(self, work_order_id: str, *, reason: str = "") -> dict[str, Any]:
        normalized = str(work_order_id or "").strip()
        if not normalized:
            return {"status": "skipped", "reason": "work_order_id_required"}
        snapshot = self.tasking_repository.read_work_order(normalized)
        metadata = dict((snapshot.get("work_order") or {}).get("metadata") or {}) if snapshot.get("status") == "ok" else {}
        plan_execution = dict(metadata.get("plan_execution") or {})
        if str(plan_execution.get("mode") or "") != "module_parent_milestones":
            return {"status": "skipped", "reason": "not_plan_parent", "work_order_id": normalized}
        if str(plan_execution.get("status") or "").strip().lower() not in {"awaiting_continue", "running_module"}:
            return {
                "status": str(plan_execution.get("status") or snapshot.get("status") or "not_available"),
                "reason": "not_awaiting_continue",
                "work_order_id": normalized,
            }
        if not bool(plan_execution.get("auto_advance_modules", True)):
            return {"status": "skipped", "reason": "auto_advance_modules_disabled", "work_order_id": normalized}
        result = await self.tick_parent_dag(normalized, reason=reason or "module_completed")
        global_schedule = await self.tick_ready_plan_dags(preferred_work_order_id=normalized, reason=reason or "module_completed")
        event = {
            "event_kind": "plan_parent_auto_dag_tick",
            "minion_id": "",
            "run_id": "",
            "work_order_id": normalized,
            "minion_profile": "",
            "payload": {"reason": reason or "module_completed", **dict(result), "global_schedule": dict(global_schedule)},
            "created_at": utc_now(),
        }
        self._queue_event_delivery(event)
        self.tasking_repository.record_minion_event(event)
        return result

    async def submit_repair_bill(self, params: dict[str, Any]) -> dict[str, Any]:
        bill = dict(params.get("repair_bill") or params)
        preferred_endpoint_id = str(params.get("preferred_endpoint_id") or bill.get("preferred_endpoint_id") or "").strip()
        result = self.tasking_repository.submit_repair_bill(bill)
        cancel_reason = (
            "architecture_defect"
            if str(result.get("reason") or result.get("blocked_reason") or "").strip().lower() == "architecture_defect"
            else "repair_bill_replay"
        )
        cooperative_cancels = await self._request_cooperative_cancel_for_work_orders(
            _string_list(result.get("invalidated_child_work_order_ids")),
            reason=cancel_reason,
            payload={
                "parent_work_order_id": str(result.get("parent_work_order_id") or bill.get("parent_work_order_id") or ""),
                "bill_id": str(result.get("bill_id") or bill.get("bill_id") or ""),
                "summary": "repair bill invalidated this module run; stop at the next safe point",
            },
        )
        if cooperative_cancels:
            result = {**dict(result), "cooperative_cancel_requests": cooperative_cancels}
        parent_id = str(result.get("parent_work_order_id") or bill.get("parent_work_order_id") or "").strip()
        global_schedule: dict[str, Any] = {}
        if parent_id:
            endpoint_override: dict[str, Any] = {}
            if preferred_endpoint_id:
                endpoint_override = self.tasking_repository.merge_work_order_metadata(
                    parent_id,
                    {
                        "preferred_endpoint_id": preferred_endpoint_id,
                        "preferred_endpoint_source": "repair_bill_override",
                    },
                )
                result = {**dict(result), "endpoint_override": endpoint_override}
            snapshot = self.tasking_repository.read_work_order(parent_id)
            metadata = dict((snapshot.get("work_order") or {}).get("metadata") or {}) if snapshot.get("status") == "ok" else {}
            plan_execution = dict(metadata.get("plan_execution") or {})
            auto_advance = bool(plan_execution.get("auto_advance_modules", True))
            ready_modules = _string_list(result.get("ready_module_ids") or _plan_execution_dag_state(plan_execution).get("ready_modules"))
            if auto_advance and ready_modules and str(result.get("status") or "").strip().lower() in {"awaiting_continue", "running_module"}:
                global_schedule = await self.tick_ready_plan_dags(
                    preferred_work_order_id=parent_id,
                    reason="repair_bill_submitted",
                )
        event = {
            "event_kind": "repair_bill_submitted",
            "minion_id": "",
            "run_id": "",
            "work_order_id": parent_id,
            "minion_profile": "",
            "payload": {
                "status": str(result.get("status") or ""),
                "summary": str(result.get("summary") or bill.get("summary") or "repair bill submitted"),
                "bill_id": str(result.get("bill_id") or bill.get("bill_id") or ""),
                "restart_required": bool(result.get("restart_required", False)),
                "requires_plan_review": bool(result.get("requires_plan_review", False)),
                "current_dag_epoch": result.get("current_dag_epoch", ""),
                "replacement_dag_epoch": result.get("replacement_dag_epoch", ""),
                "result": dict(result),
                "global_schedule": dict(global_schedule),
            },
            "created_at": utc_now(),
        }
        if parent_id:
            self._queue_event_delivery(event)
        return {**dict(result), "global_schedule": dict(global_schedule)}

    async def _request_cooperative_cancel_for_work_orders(
        self,
        work_order_ids: list[str],
        *,
        reason: str,
        payload: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for work_order_id in _dedupe_strings(_string_list(work_order_ids)):
            state = self._find_active_run_for_work_order(work_order_id) or next(
                (
                    item
                    for item in self.runs.values()
                    if str(item.pack.work_order_id or "") == work_order_id
                    and item.status in _ACTIVE_RUN_STATUSES
                    and item.control_queue is not None
                ),
                None,
            )
            if state is None:
                results.append({"status": "not_active", "child_work_order_id": work_order_id})
                continue
            module_id = str(state.pack.metadata.get("parent_module_id") or state.pack.metadata.get("module_id") or "")
            message_payload = {
                **dict(payload or {}),
                "reason": reason,
                "child_work_order_id": work_order_id,
                "module_id": module_id,
            }
            sent = await self._send_runner_control_or_record(
                state,
                {
                    "type": "cancel_requested",
                    "payload": message_payload,
                },
            )
            results.append(
                {
                    "status": "requested" if bool(sent.get("ok")) else "skipped",
                    "child_work_order_id": work_order_id,
                    "run_id": state.run_id,
                    "module_id": module_id,
                    "control": dict(sent),
                }
            )
        return results

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
        scheduler_tasks = [task for task in self._module_detail_scheduler_tasks.values() if not task.done()]
        for task in scheduler_tasks:
            task.cancel()
        if scheduler_tasks:
            await asyncio.gather(*scheduler_tasks, return_exceptions=True)
        self._module_detail_scheduler_tasks.clear()
        await self.step_processes.close_all()
        await self.step_executor.close_all()
        if self._llm_broker_bundle is not None:
            close = getattr(self._llm_broker_bundle, "close", None)
            if callable(close):
                await close()
            self._llm_broker_bundle = None

    async def _start_runner(self, state: MinionRunState) -> None:
        await self.step_executor.start_runner(state)

    async def llm_broker_preflight(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_broker_run(params)
        request = preflight_request_from_payload(dict(params.get("request") or {}))
        runtime = await self._llm_broker_runtime()
        advice = await runtime.apreflight(request)
        return {"ok": True, "advice": preflight_advice_to_payload(advice)}

    async def llm_broker_generate(self, params: dict[str, Any]) -> dict[str, Any]:
        state = self._require_broker_run(params)
        request = llm_request_from_payload(dict(params.get("request") or {}))
        runtime = await self._llm_broker_runtime()
        with scoped_llm_event_sink(self._llm_broker_progress_sink(state)):
            outcome = await runtime.agenerate(request)
        await asyncio.sleep(0)
        return {"ok": True, "outcome": llm_outcome_to_payload(outcome)}

    async def llm_broker_generate_stream(self, params: dict[str, Any]) -> dict[str, Any]:
        state = self._require_broker_run(params)
        request = llm_request_from_payload(dict(params.get("request") or {}))
        runtime = await self._llm_broker_runtime()
        with scoped_llm_event_sink(self._llm_broker_progress_sink(state)):
            events = await runtime.agenerate_stream(request)
        await asyncio.sleep(0)
        return {"ok": True, "events": [stream_event_to_payload(event) for event in list(events or [])]}

    def _llm_broker_progress_sink(self, state: MinionRunState):
        loop = asyncio.get_running_loop()

        def sink(event: dict[str, Any]) -> None:
            payload = _llm_endpoint_progress_payload(event)

            def record() -> None:
                current = self.runs.get(state.run_id)
                if current is None or current.status in _TERMINAL_RUN_STATUSES:
                    return
                self._record_event(
                    current,
                    {
                        "event_kind": "progress",
                        "payload": payload,
                        "created_at": utc_now(),
                    },
                )

            loop.call_soon_threadsafe(record)

        return sink

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

    def _reconcile_runs(self) -> None:
        self.step_executor.reconcile_runs()

    def _active_runner_work_order_ids(self) -> set[str]:
        return self.step_executor.active_runner_work_order_ids()

    def _active_module_child_work_order_ids(self) -> set[str]:
        active = set(self.tasking_repository.running_plan_module_child_work_order_ids())
        for state in self.runs.values():
            if state.runner_kind == "logical":
                continue
            if state.status not in _ACTIVE_RUN_STATUSES:
                continue
            metadata = dict(state.pack.metadata or {})
            if not str(metadata.get("parent_work_order_id") or "").strip():
                continue
            if not str(metadata.get("parent_module_id") or metadata.get("module_id") or "").strip():
                continue
            work_order_id = str(state.pack.work_order_id or "").strip()
            if work_order_id:
                with contextlib.suppress(Exception):
                    snapshot = self.tasking_repository.read_work_order(work_order_id)
                    status = str((snapshot.get("work_order") or {}).get("status") or "").strip().lower()
                    if status in _TERMINAL_RUN_STATUSES:
                        continue
                active.add(work_order_id)
        return active

    def _active_module_child_count(self) -> int:
        return len(self._active_module_child_work_order_ids())

    def _active_module_child_count_from_ledger(self) -> int:
        count = 0
        for state in self.runs.values():
            if state.runner_kind == "logical":
                continue
            if state.status not in _ACTIVE_RUN_STATUSES:
                continue
            metadata = dict(state.pack.metadata or {})
            if not str(metadata.get("parent_work_order_id") or "").strip():
                continue
            if not str(metadata.get("parent_module_id") or metadata.get("module_id") or "").strip():
                continue
            if _state_last_reported_final_milestone_completed(state):
                continue
            count += 1
        return count

    def _allocated_logical_slot_count(self) -> int:
        return len(self._logical_slots)

    def _active_llm_node_slot_count_from_ledger(self) -> int:
        return len(self._active_llm_node_slot_keys_from_ledger())

    def _active_llm_node_slot_keys_from_ledger(self) -> set[str]:
        keys: set[str] = set()
        for work_order_id in self.tasking_repository.running_plan_module_child_work_order_ids():
            normalized = str(work_order_id or "").strip()
            if normalized:
                keys.add(f"work_order:{normalized}")
        keys.update(self._active_llm_node_slot_keys_from_memory_ledger())
        return keys

    def _active_llm_node_slot_keys_from_memory_ledger(self) -> set[str]:
        keys: set[str] = set()
        for state in self.runs.values():
            if state.runner_kind == "logical":
                continue
            if state.status not in _ACTIVE_RUN_STATUSES:
                continue
            metadata = dict(state.pack.metadata or {})
            is_module_child = bool(
                str(metadata.get("parent_work_order_id") or "").strip()
                and str(metadata.get("parent_module_id") or metadata.get("module_id") or "").strip()
            )
            if is_module_child and _state_last_reported_final_milestone_completed(state):
                continue
            key = self._llm_node_slot_key_for_pack(state.pack)
            if key:
                keys.add(key)
        for slot in self._logical_slots.values():
            key = self._llm_node_slot_key_for_logical_slot(slot)
            if key:
                keys.add(key)
        return keys

    def _llm_node_slot_key_for_logical_slot(self, slot: dict[str, Any]) -> str:
        work_order_id = str(slot.get("work_order_id") or "").strip()
        if work_order_id:
            return f"work_order:{work_order_id}"
        slot_id = str(slot.get("slot_id") or "").strip()
        if slot_id:
            return f"logical:{slot_id}"
        run_id = str(slot.get("run_id") or "").strip()
        return f"logical:{run_id}" if run_id else ""

    def _llm_node_slot_key_for_pack(self, pack: TaskContextPack) -> str:
        metadata = dict(pack.metadata or {})
        workspace = dict(pack.workspace or {})
        for key in (
            "checkpoint_review_for_work_order_id",
            "plan_review_for_work_order_id",
            "pre_plan_contract_source_work_order_id",
            "source_work_order_id",
        ):
            value = str(metadata.get(key) or workspace.get(key) or "").strip()
            if value:
                return f"work_order:{value}"
        review_target = metadata.get("review_target")
        if isinstance(review_target, dict):
            for key in (
                "planner_work_order_id",
                "source_work_order_id",
                "source_work_order",
                "work_order_id",
            ):
                value = str(review_target.get(key) or "").strip()
                if value:
                    return f"work_order:{value}"
        work_order_id = str(pack.work_order_id or "").strip()
        return f"work_order:{work_order_id}" if work_order_id else ""

    def _admit_llm_node_for_spawn(self, pack: TaskContextPack) -> dict[str, Any]:
        slot_key = self._llm_node_slot_key_for_pack(pack)
        active_keys = self._active_llm_node_slot_keys_from_ledger()
        shared = bool(slot_key and slot_key in active_keys)
        available = max(0, int(self.max_parallel_modules or _DEFAULT_MAX_PARALLEL_LLM_NODES) - len(active_keys))
        if not shared and available <= 0:
            return {
                "status": "waiting_for_slot",
                "reason": "global_parallel_limit",
                "limit_kind": "llm_node",
                "summary": "global LLM node concurrency limit reached",
                "work_order_id": str(pack.work_order_id or ""),
                "minion_profile": str(pack.minion_profile or ""),
                "slot_key": slot_key,
                "max_parallel_llm_nodes": int(self.max_parallel_modules or _DEFAULT_MAX_PARALLEL_LLM_NODES),
                "max_parallel_modules": int(self.max_parallel_modules or _DEFAULT_MAX_PARALLEL_MODULES),
                "active_llm_node_count": len(active_keys),
                "available_llm_node_slots": 0,
                "available_module_slots": 0,
                "logical_slot_generation": self._logical_slot_generation,
            }
        return {
            "status": "admitted",
            "limit_kind": "llm_node",
            "slot_key": slot_key,
            "shared_slot": shared,
            "max_parallel_llm_nodes": int(self.max_parallel_modules or _DEFAULT_MAX_PARALLEL_LLM_NODES),
            "max_parallel_modules": int(self.max_parallel_modules or _DEFAULT_MAX_PARALLEL_MODULES),
            "active_llm_node_count": len(active_keys),
            "available_llm_node_slots": available if shared else max(0, available - 1),
            "available_module_slots": available if shared else max(0, available - 1),
            "logical_slot_generation": self._logical_slot_generation,
        }

    def _available_llm_node_slots_from_ledger(self) -> int:
        return max(
            0,
            int(self.max_parallel_modules or _DEFAULT_MAX_PARALLEL_LLM_NODES)
            - self._active_llm_node_slot_count_from_ledger(),
        )

    def _available_llm_node_slots(self) -> int:
        return self._available_llm_node_slots_from_ledger()

    def _available_module_slots_from_ledger(self, *, active_module_count: int | None = None) -> int:
        _ = active_module_count
        return self._available_llm_node_slots_from_ledger()

    def _available_module_slots(self) -> int:
        return self._available_llm_node_slots()

    def _find_active_run_for_work_order(self, work_order_id: str) -> MinionRunState | None:
        return self.step_executor.find_active_run_for_work_order(work_order_id)

    def _install_signal_handlers(self):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return lambda: None
        installed: list[signal.Signals] = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(
                    sig,
                    lambda sig=sig: self.request_shutdown(
                        reason=f"signal_{sig.name.lower()}",
                        graceful=True,
                        timeout_seconds=float(self.graceful_shutdown_timeout_seconds or _DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS),
                    ),
                )
                installed.append(sig)

        def remove() -> None:
            for sig in installed:
                with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                    loop.remove_signal_handler(sig)

        return remove

    async def _drain_active_runs_before_shutdown(self, *, timeout_seconds: float | None = None, reason: str = "manager_shutdown") -> dict[str, Any]:
        self._reconcile_runs()
        active = self._active_states_for_shutdown()
        if not active:
            return {"status": "idle", "requested_count": 0, "remaining_count": 0}
        timeout = max(
            0.0,
            float(
                timeout_seconds
                if timeout_seconds is not None
                else self.graceful_shutdown_timeout_seconds
                or _DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS
            ),
        )
        requested: list[dict[str, Any]] = []
        for state in active:
            payload = {
                "reason": str(reason or "manager_shutdown"),
                "summary": "minion manager shutdown requested; stop at the next safe point",
                "graceful_shutdown": True,
                "run_id": state.run_id,
                "work_order_id": state.pack.work_order_id,
            }
            try:
                control = await self._send_runner_control_or_record(
                    state,
                    {
                        "type": "cancel_requested",
                        "payload": payload,
                    },
                )
            except Exception as exc:
                control = {
                    "ok": False,
                    "run_id": state.run_id,
                    "message_type": "cancel_requested",
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            requested.append(
                {
                    "run_id": state.run_id,
                    "work_order_id": state.pack.work_order_id,
                    "status": "requested" if bool(control.get("ok")) else "skipped",
                    "control": dict(control),
                }
            )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            self._reconcile_runs()
            remaining = self._active_states_for_shutdown()
            if not remaining:
                return {
                    "status": "drained",
                    "requested_count": len(requested),
                    "remaining_count": 0,
                    "requests": requested,
                }
            now = loop.time()
            if now >= deadline:
                return {
                    "status": "timeout",
                    "requested_count": len(requested),
                    "remaining_count": len(remaining),
                    "remaining_run_ids": [state.run_id for state in remaining],
                    "requests": requested,
                }
            await asyncio.sleep(min(_GRACEFUL_SHUTDOWN_POLL_SECONDS, max(0.0, deadline - now)))

    def _active_states_for_shutdown(self) -> list[MinionRunState]:
        return [state for state in self.runs.values() if state.status in _ACTIVE_RUN_STATUSES]

    def _record_runner_stderr_line(self, state: MinionRunState, line: str) -> None:
        self.step_executor.record_runner_stderr_line(state, line)

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
        event_recorded = False
        if event_kind == "terminal":
            terminal_status = str(event["payload"].get("status") or "completed")
            self._transition_run_status(state, terminal_status)
            state.ended_at = utc_now()
            state.pending_approval = {}
            state.pending_clarification = {}
            self._release_logical_slots_for_run(state.run_id, reason="runner_terminal")
            self._notify_logical_slot_available(reason="runner_terminal")
            event_recorded = self._maybe_release_parent_module_on_terminal_failure(state, event)
        if event_kind == "resource_request":
            self._schedule_resource_request(state, event)
        if event_kind == "resource_release":
            release_payload = self._release_logical_slot(
                str(event["payload"].get("slot_id") or ""),
                run_id=state.run_id,
                reason=str(event["payload"].get("reason") or "runner_release"),
            )
            event["payload"] = {**dict(event["payload"]), "release": release_payload}
        if event_kind == "progress":
            self._update_progress_state(state, event)
        state.last_event = event
        state.last_event_at = str(event.get("created_at") or utc_now())
        state.ledger.append(event)
        if len(state.ledger) > _RUN_MEMORY_LEDGER_LIMIT:
            del state.ledger[:-_RUN_MEMORY_LEDGER_LIMIT]
        if self._should_queue_event_delivery(event):
            self._queue_event_delivery(event)
        if _should_log_runner_event(state.pack, event):
            self.logger.info(
                "minion event run=%s kind=%s status=%s phase=%s summary=%s",
                state.run_id,
                event_kind,
                event["payload"].get("status", ""),
                event["payload"].get("phase", ""),
                str(event["payload"].get("summary") or "")[:240],
            )
        if _should_record_runner_event(state.pack, event) and not event_recorded:
            try:
                self.tasking_repository.record_minion_event(event)
            except Exception:
                self.logger.exception("failed to record minion tasking event: %s", state.run_id)
                return
        if event_kind == "checkpoint":
            self.reviews.schedule_event_gates(state, event)
        if event_kind == "terminal":
            self.reviews.schedule_reviewer_terminal_reconciliation(state, event)
            if self._is_staged_planning_run(state):
                self._schedule_staged_planning_terminal_post(state, event)
            else:
                self._schedule_workflow_terminal_post(state, event)
            self._schedule_ready_modules_after_terminal(state, event)
        if event_kind == "milestone_completed":
            self.serial_scheduler.schedule(state, event)

    def _maybe_release_parent_module_on_terminal_failure(self, state: MinionRunState, event: dict[str, Any]) -> bool:
        payload = dict(event.get("payload") or {})
        terminal_status = str(payload.get("status") or "").strip().lower()
        if terminal_status not in {"blocked", "failed"}:
            return False
        metadata = dict(state.pack.metadata or {})
        parent_work_order_id = str(metadata.get("parent_work_order_id") or "").strip()
        parent_module_id = str(metadata.get("parent_module_id") or metadata.get("module_id") or "").strip()
        child_work_order_id = str(state.pack.work_order_id or event.get("work_order_id") or "").strip()
        if not parent_work_order_id or not parent_module_id or not child_work_order_id:
            return False
        recorded = False
        try:
            if _should_record_runner_event(state.pack, event):
                self.tasking_repository.record_minion_event(event)
                recorded = True
            release = self.tasking_repository.release_running_module_parent(
                child_work_order_id,
                child_terminal_status=terminal_status,
                reason=str(payload.get("summary") or f"module {parent_module_id} ended with status {terminal_status}"),
            )
        except Exception:
            self.logger.exception("failed to release parent module on terminal failure: %s", state.run_id)
            return recorded
        payload["parent_module_release"] = dict(release)
        event["payload"] = payload
        parent_status = str(release.get("parent_status") or release.get("status") or "").strip().lower()
        if parent_status == "blocked":
            parent_event = {
                "event_kind": "plan_parent_blocked",
                "minion_id": "",
                "run_id": state.run_id,
                "work_order_id": parent_work_order_id,
                "minion_profile": state.pack.minion_profile,
                "payload": {
                    "status": "blocked",
                    "reason": "module_terminal_failure",
                    "module_id": parent_module_id,
                    "child_work_order_id": child_work_order_id,
                    "child_terminal_status": terminal_status,
                    "summary": str(payload.get("summary") or f"module {parent_module_id} ended with status {terminal_status}"),
                    "release": dict(release),
                },
                "created_at": utc_now(),
            }
            self._queue_event_delivery(parent_event)
            try:
                self.tasking_repository.record_minion_event(parent_event)
            except Exception:
                self.logger.exception("failed to record parent block event after terminal failure: %s", parent_work_order_id)
        return recorded

    def _schedule_ready_modules_after_terminal(self, state: MinionRunState, event: dict[str, Any]) -> None:
        _ = state, event
        if self._shutdown_event.is_set():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.tick_ready_plan_dags(reason="runner_terminal"), name=f"minion-terminal-dag-tick-{state.run_id}")

    def _schedule_resource_request(self, state: MinionRunState, event: dict[str, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._handle_resource_request_async(state, event), name=f"minion-resource-request-{state.run_id}")

    async def _handle_resource_request_async(self, state: MinionRunState, event: dict[str, Any]) -> None:
        payload = dict(event.get("payload") or {})
        grant = self._request_logical_slot(
            state,
            resource=str(payload.get("resource") or "logical_minion_slot"),
            module_id=str(payload.get("module_id") or payload.get("logical_module_id") or ""),
            reason=str(payload.get("reason") or "runner_request"),
        )
        message_type = "resource_grant" if grant.get("status") == "granted" else "resource_denied"
        await self._send_runner_control_or_record(state, {"type": message_type, "payload": grant})
        self._record_event(
            state,
            {
                "event_kind": message_type,
                "payload": grant,
                "created_at": utc_now(),
            },
        )

    def _request_logical_slot(self, state: MinionRunState, *, resource: str, module_id: str = "", reason: str = "") -> dict[str, Any]:
        return self.step_runner.request_logical_slot(state, resource=resource, module_id=module_id, reason=reason)

    async def _wait_logical_slot(
        self,
        state: MinionRunState,
        *,
        resource: str,
        module_id: str = "",
        reason: str = "",
        timeout_seconds: Any = None,
    ) -> dict[str, Any]:
        return await self.step_runner.wait_logical_slot(
            state,
            resource=resource,
            module_id=module_id,
            reason=reason,
            timeout_seconds=timeout_seconds,
        )

    def _release_logical_slot(self, slot_id: str, *, run_id: str = "", reason: str = "") -> dict[str, Any]:
        return self.step_runner.release_logical_slot(slot_id, run_id=run_id, reason=reason)

    def _release_logical_slots_for_run(self, run_id: str, *, reason: str = "") -> list[dict[str, Any]]:
        return self.step_runner.release_logical_slots_for_run(run_id, reason=reason)

    def _notify_logical_slot_available(self, *, reason: str = "") -> None:
        _ = reason
        self._logical_slot_generation += 1
        event = self._logical_slot_event
        self._logical_slot_event = asyncio.Event()
        event.set()

    def _schedule_workflow_terminal_post(self, state: MinionRunState, event: dict[str, Any]) -> None:
        if self._shutdown_event.is_set():
            return
        metadata = self._work_order_metadata_for_state(state)
        workflow = dict(metadata.get("workflow") or {})
        if not workflow:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._handle_workflow_terminal_post_async(state, event), name=f"minion-workflow-post-{state.run_id}")

    def _is_staged_planning_run(self, state: MinionRunState) -> bool:
        metadata = self._work_order_metadata_for_state(state)
        staged = dict(metadata.get("staged_planning") or (state.pack.metadata or {}).get("staged_planning") or {})
        return str(staged.get("stage") or "").strip() in {"architecture_sketch", "module_detail"}

    def _schedule_staged_planning_terminal_post(self, state: MinionRunState, event: dict[str, Any]) -> None:
        if self._shutdown_event.is_set():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._handle_staged_planning_terminal_post_async(state, event), name=f"minion-staged-planning-post-{state.run_id}")

    async def _handle_staged_planning_terminal_post_async(self, state: MinionRunState, event: dict[str, Any]) -> None:
        metadata = self._work_order_metadata_for_state(state)
        staged = dict(metadata.get("staged_planning") or {})
        stage = str(staged.get("stage") or "").strip()
        payload = dict(event.get("payload") or {})
        terminal_status = str(payload.get("status") or "").strip().lower()
        if terminal_status != "completed":
            await self._mark_staged_planning_blocked(
                state,
                stage=stage,
                reason=terminal_status or "failed",
                summary=str(payload.get("summary") or "staged planning run did not complete"),
            )
            return
        try:
            if stage == "architecture_sketch":
                await self._handle_architecture_sketch_completed(state, payload)
            elif stage == "module_detail":
                await self._handle_module_detail_completed(state, payload)
        except Exception as exc:
            self.logger.exception("staged planning post failed: %s", state.run_id)
            await self._mark_staged_planning_blocked(
                state,
                stage=stage,
                reason="staged_planning_post_failed",
                summary=f"{exc.__class__.__name__}: {exc}",
            )

    async def _mark_staged_planning_blocked(self, state: MinionRunState, *, stage: str, reason: str, summary: str) -> None:
        metadata = self._work_order_metadata_for_state(state)
        staged = dict(metadata.get("staged_planning") or (state.pack.metadata or {}).get("staged_planning") or {})
        parent_work_order_id = str(staged.get("parent_work_order_id") or state.pack.work_order_id).strip()
        if not parent_work_order_id:
            parent_work_order_id = state.pack.work_order_id
        parent_snapshot = self.tasking_repository.read_work_order(parent_work_order_id)
        parent_metadata = dict(((parent_snapshot.get("work_order") or {}).get("metadata") or {}) if parent_snapshot.get("status") == "ok" else metadata)
        parent_staged = dict(parent_metadata.get("staged_planning") or {})
        parent_staged.update(
            {
                "status": "blocked",
                "blocked_stage": stage,
                "blocker": {"reason": reason, "summary": summary, "run_id": state.run_id, "work_order_id": state.pack.work_order_id},
                "updated_at": utc_now(),
            }
        )
        workflow = dict(parent_metadata.get("workflow") or {})
        if workflow:
            workflow.update({"status": "blocked", "blocker": dict(parent_staged["blocker"]), "updated_at": utc_now()})
        self.tasking_repository.merge_work_order_metadata(
            parent_work_order_id,
            {"staged_planning": parent_staged, **({"workflow": workflow} if workflow else {})},
            work_order_status="blocked",
        )
        event_payload = {
            "status": "blocked",
            "stage": stage,
            "reason": reason,
            "summary": summary,
            "source_work_order_id": state.pack.work_order_id,
        }
        blocked_event = {
            "event_kind": "staged_planning_blocked",
            "minion_id": state.minion_id,
            "run_id": state.run_id,
            "work_order_id": parent_work_order_id,
            "minion_profile": state.pack.minion_profile,
            "payload": event_payload,
            "created_at": utc_now(),
        }
        self._queue_event_delivery(blocked_event)
        self.tasking_repository.record_minion_event(blocked_event)

    async def _handle_architecture_sketch_completed(self, state: MinionRunState, payload: dict[str, Any]) -> None:
        artifact_ref = _stage_artifact_from_payload(payload)
        sketch = load_stage_artifact_ref(artifact_ref)
        validation = validate_plan_sketch_artifact(sketch)
        parent_work_order_id = state.pack.work_order_id
        metadata = self._work_order_metadata_for_state(state)
        staged = dict(metadata.get("staged_planning") or {})
        planning_depth = str(staged.get("planning_depth") or sketch.get("planning_depth") or "sketch_only").strip() or "sketch_only"
        staged.update(
            {
                "status": "sketch_completed",
                "stage": "architecture_sketch",
                "planning_depth": planning_depth,
                "sketch_artifact": dict(artifact_ref),
                "sketch_validation": dict(validation),
                "module_ids": [str(module.get("module_id") or "") for module in list(sketch.get("modules") or []) if isinstance(module, dict)],
                "updated_at": utc_now(),
            }
        )
        workflow = dict(metadata.get("workflow") or {})
        if workflow:
            workflow = update_current_workflow_step(
                workflow,
                status="completed",
                output_artifact={"sketch_artifact": dict(artifact_ref), "sketch_validation": dict(validation)},
                next_profile="software_engineering.architect_module_detail" if planning_depth == "module_detail" else "manager.final_plan_compiler",
            )
            workflow.update(
                {
                    "status": "detail_planning" if planning_depth == "module_detail" else "final_compiling",
                    "blocker": None,
                    "updated_at": utc_now(),
                }
            )
        self.tasking_repository.merge_work_order_metadata(
            parent_work_order_id,
            {"staged_planning": staged, **({"workflow": workflow} if workflow else {})},
            work_order_status="active",
        )
        if planning_depth == "module_detail":
            await self._spawn_module_detail_runs(parent_work_order_id, sketch, artifact_ref)
            return
        await self._compile_and_review_staged_plan(parent_work_order_id)

    async def _spawn_module_detail_runs(self, parent_work_order_id: str, sketch: dict[str, Any], sketch_ref: dict[str, Any]) -> None:
        snapshot = self.tasking_repository.read_work_order(parent_work_order_id)
        parent_work_order = dict(snapshot.get("work_order") or {}) if snapshot.get("status") == "ok" else {}
        parent_metadata = dict(parent_work_order.get("metadata") or {})
        parent_workspace = dict(parent_work_order.get("workspace") or parent_metadata.get("workspace") or {})
        staged = dict(parent_metadata.get("staged_planning") or {})
        detail_modules: dict[str, dict[str, Any]] = {}
        spawned_runs: list[dict[str, Any]] = []
        for module in [dict(item) for item in list(sketch.get("modules") or []) if isinstance(item, dict)]:
            module_id = str(module.get("module_id") or "").strip()
            if not module_id:
                continue
            work_order_id = f"wo_detail_{safe_token(parent_work_order_id)}_{safe_token(module_id)}"
            detail_metadata = {
                "task_id": str(parent_metadata.get("task_id") or sketch.get("task_id") or ""),
                "task_title": str(parent_metadata.get("task_title") or parent_work_order.get("title") or ""),
                "work_order_title": f"Detail module {module_id}",
                "profile_family": "software_engineering",
                "parent_work_order_id": parent_work_order_id,
                "module_id": module_id,
                "parent_module_id": module_id,
                "step_executor_key": work_order_id,
                "requirements_brief": dict(parent_metadata.get("requirements_brief") or {}),
                "staged_planning": {
                    "stage": "module_detail",
                    "planning_depth": "module_detail",
                    "parent_work_order_id": parent_work_order_id,
                    "module_id": module_id,
                    "plan_id": str(sketch.get("plan_id") or ""),
                    "task_id": str(sketch.get("task_id") or parent_metadata.get("task_id") or ""),
                    "sketch_ref": dict(sketch_ref),
                    "sketch_artifact": dict(sketch_ref),
                },
                "planner_work_order": {
                    "role": "architect",
                    "goal": f"Fill module detail for {module_id}",
                    "task_id": str(sketch.get("task_id") or parent_metadata.get("task_id") or ""),
                    "work_order_id": work_order_id,
                    "planning_requirements": {
                        "required_stage": "module_detail",
                        "planning_depth": "module_detail",
                        "stage_artifact_required": "ModuleDetailArtifact",
                        "bound_module_id": module_id,
                        "dispatchable_plan_required": False,
                        "stage_focus": [
                            "Fill only the bound module's internal implementation design.",
                            "Divide the module into coder-sized milestones that satisfy the accepted sketch contracts.",
                            "Add module-local AC, tests, positive cases, negative cases, and evidence expectations.",
                            "Tie every milestone back to sketch interfaces, lifecycle rules, ownership rules, state-machine rules, invariants, or first-layer gates.",
                        ],
                        "stage_non_goals": [
                            "Do not change global topology, module boundaries, dependency direction, or owned_area.",
                            "Do not redesign other modules.",
                            "Do not implement code.",
                        ],
                    },
                },
                "milestones": [
                    {
                        "milestone_id": f"detail_{module_id}",
                        "title": f"Fill module detail for {module_id}",
                        "summary": (
                            f"Design {module_id}'s internal milestones, AC, tests, positive/negative cases, and evidence "
                            "so the module satisfies the accepted sketch contracts."
                        ),
                        "acceptance": [
                            "Submit a valid ModuleDetailArtifact.",
                            "Do not change module boundaries or topology.",
                            "Every milestone maps to accepted sketch contracts, interfaces, lifecycle/state/ownership rules, invariants, or first-layer gates.",
                            "Every milestone has concrete AC, tests, evidence expectations, and negative cases.",
                        ],
                    }
                ],
            }
            if isinstance(parent_metadata.get("control_route"), dict):
                detail_metadata["control_route"] = dict(parent_metadata.get("control_route") or {})
            for key in ("preferred_endpoint_id", "preferred_endpoint_source", "prompt_observation_tag", "approval_policy"):
                if key in parent_metadata:
                    detail_metadata[key] = parent_metadata[key]
            detail_workspace = {
                **parent_workspace,
                "task_id": str(sketch.get("task_id") or parent_metadata.get("task_id") or ""),
                "goal": f"Fill module detail for {module_id}",
                "planning_depth": "module_detail",
                "plan_builder_stage": "module_detail",
                "plan_builder_module_id": module_id,
                "bound_module_id": module_id,
            }
            pack = TaskContextPack.from_dict(
                {
                    "work_order_id": work_order_id,
                    "goal": f"Fill module detail for {module_id}",
                    "instruction": (
                        f"Fill only the bound module detail for {module_id}. "
                        "Design module-local implementation milestones, AC, tests, positive/negative cases, and evidence that satisfy "
                        "the accepted sketch contracts. Do not change global topology, module boundaries, dependency direction, or owned_area. "
                        "Use staged plan builder tools and submit ModuleDetailArtifact."
                    ),
                    "acceptance_criteria": [
                        "Submit a valid ModuleDetailArtifact.",
                        "Do not change global module boundaries, owned_area, or topology.",
                        "Tie module-local milestones and AC back to sketch interfaces, lifecycle/state/ownership rules, invariants, and first-layer gates.",
                        "Include module-local tests, evidence expectations, positive cases, and negative cases.",
                    ],
                    "workspace": detail_workspace,
                    "profile_group": "software_engineering",
                    "profile_name": "architect_module_detail",
                    "metadata": detail_metadata,
                }
            )
            spawned = await self.spawn(pack.to_dict())
            spawn_status = str(spawned.get("status") or "").strip().lower()
            run_id = str(spawned.get("run_id") or "")
            detail_modules[module_id] = {
                "status": "running" if run_id else (spawn_status or "waiting_for_slot"),
                "work_order_id": work_order_id,
                "module_id": module_id,
                "run_id": run_id,
                "minion_id": str(spawned.get("minion_id") or ""),
                "slot_admission": dict(spawned) if spawn_status == "waiting_for_slot" else {},
            }
            spawned_runs.append(dict(spawned))
        if not detail_modules:
            raise ValueError("module_detail planning requires at least one sketch module")
        staged.update(
            {
                "status": "module_detail_running",
                "detail_modules": detail_modules,
                "detail_spawned_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        workflow = dict(parent_metadata.get("workflow") or {})
        if workflow:
            workflow.update({"status": "module_detail_running", "blocker": None, "updated_at": utc_now()})
        self.tasking_repository.merge_work_order_metadata(
            parent_work_order_id,
            {"staged_planning": staged, **({"workflow": workflow} if workflow else {})},
            work_order_status="active",
        )
        event = {
            "event_kind": "module_detail_dispatched",
            "minion_id": "",
            "run_id": "",
            "work_order_id": parent_work_order_id,
            "minion_profile": "software_engineering.architect_module_detail",
            "payload": {"status": "running", "modules": detail_modules, "runs": spawned_runs},
            "created_at": utc_now(),
        }
        self._queue_event_delivery(event)
        self.tasking_repository.record_minion_event(event)
        if any(str(item.get("status") or "") == "waiting_for_slot" for item in detail_modules.values()):
            self._schedule_waiting_module_detail_runs(parent_work_order_id, reason="module_detail_dispatched")

    def _schedule_waiting_module_detail_runs(self, parent_work_order_id: str, *, reason: str = "") -> None:
        normalized = str(parent_work_order_id or "").strip()
        if not normalized or self._shutdown_event.is_set():
            return
        current = self._module_detail_scheduler_tasks.get(normalized)
        if current is not None and not current.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(
            self._run_waiting_module_detail_scheduler(normalized, reason=reason),
            name=f"minion-module-detail-scheduler-{safe_token(normalized)}",
        )
        self._module_detail_scheduler_tasks[normalized] = task
        task.add_done_callback(
            lambda completed, work_order_id=normalized: self._forget_module_detail_scheduler_task(
                work_order_id,
                completed,
            )
        )

    def _forget_module_detail_scheduler_task(self, parent_work_order_id: str, task: asyncio.Task[Any]) -> None:
        if self._module_detail_scheduler_tasks.get(parent_work_order_id) is task:
            self._module_detail_scheduler_tasks.pop(parent_work_order_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.logger.error(
                "module-detail scheduler failed parent=%s: %s: %s",
                parent_work_order_id,
                error.__class__.__name__,
                error,
            )

    async def _run_waiting_module_detail_scheduler(self, parent_work_order_id: str, *, reason: str = "") -> None:
        try:
            while not self._shutdown_event.is_set():
                wake_event = self._logical_slot_event
                result = await self._advance_waiting_module_detail_runs(parent_work_order_id, reason=reason)
                status = str(result.get("status") or "").strip().lower()
                if status in {"idle", "completed", "blocked", "not_found"}:
                    return
                if int(result.get("started_count") or 0) > 0:
                    continue
                if wake_event is not self._logical_slot_event or wake_event.is_set():
                    continue
                await wake_event.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.exception("module-detail scheduler activation failed: %s", parent_work_order_id)
            try:
                parent_pack = self.tasking_repository.pack_for_work_order(parent_work_order_id)
                await self._mark_staged_planning_blocked(
                    MinionRunState(minion_id="", run_id="", pack=parent_pack),
                    stage="module_detail",
                    reason="module_detail_scheduler_failed",
                    summary=f"{exc.__class__.__name__}: {exc}",
                )
            except Exception:
                self.logger.exception("failed to close module-detail scheduler state: %s", parent_work_order_id)

    async def _advance_waiting_module_detail_runs(self, parent_work_order_id: str, *, reason: str = "") -> dict[str, Any]:
        snapshot = self.tasking_repository.read_work_order(parent_work_order_id)
        if snapshot.get("status") != "ok":
            return {"status": "not_found", "parent_work_order_id": parent_work_order_id}
        work_order = dict(snapshot.get("work_order") or {})
        metadata = dict(work_order.get("metadata") or {})
        staged = dict(metadata.get("staged_planning") or {})
        staged_status = str(staged.get("status") or "").strip().lower()
        if staged_status != "module_detail_running":
            return {"status": staged_status or "idle", "parent_work_order_id": parent_work_order_id}
        detail_modules = {
            str(key): dict(value)
            for key, value in dict(staged.get("detail_modules") or {}).items()
            if isinstance(value, dict)
        }
        waiting_ids = [
            module_id
            for module_id, item in detail_modules.items()
            if str(item.get("status") or "").strip().lower() in {"pending", "waiting_for_slot"}
            or (
                str(item.get("status") or "").strip().lower() == "running"
                and not str(item.get("run_id") or "").strip()
            )
        ]
        if not waiting_ids:
            return {"status": "idle", "parent_work_order_id": parent_work_order_id, "started_count": 0}

        for item in detail_modules.values():
            run_id = str(item.get("run_id") or "").strip()
            run = self.runs.get(run_id) if run_id else None
            if run is not None and run.status in {"blocked", "failed", "killed", "timeout"}:
                return {
                    "status": "blocked",
                    "parent_work_order_id": parent_work_order_id,
                    "failed_run_id": run_id,
                    "failed_status": run.status,
                    "started_count": 0,
                }

        started: list[dict[str, Any]] = []
        for module_id in waiting_ids:
            item = dict(detail_modules[module_id])
            child_work_order_id = str(item.get("work_order_id") or "").strip()
            if not child_work_order_id:
                continue
            active = self._find_active_run_for_work_order(child_work_order_id)
            if active is not None:
                spawned = active.summary()
            else:
                pack = self.tasking_repository.pack_for_work_order(child_work_order_id)
                spawned = await self.spawn(pack.to_dict())
            spawn_status = str(spawned.get("status") or "").strip().lower()
            run_id = str(spawned.get("run_id") or "").strip()
            if run_id:
                item.update(
                    {
                        "status": "running",
                        "run_id": run_id,
                        "minion_id": str(spawned.get("minion_id") or ""),
                        "started_at": utc_now(),
                        "slot_admission": {},
                    }
                )
                started.append(dict(spawned))
            else:
                item.update(
                    {
                        "status": spawn_status or "waiting_for_slot",
                        "run_id": "",
                        "minion_id": "",
                        "slot_admission": dict(spawned),
                        "waiting_since": str(item.get("waiting_since") or utc_now()),
                    }
                )
            detail_modules[module_id] = item
            self.tasking_repository.merge_work_order_metadata(
                parent_work_order_id,
                {
                    "staged_planning": {
                        "detail_modules": {module_id: item},
                        "updated_at": utc_now(),
                    }
                },
                work_order_status="active",
            )
            if not run_id:
                break

        remaining = [
            module_id
            for module_id, item in detail_modules.items()
            if str(item.get("status") or "").strip().lower() in {"pending", "waiting_for_slot"}
            or (
                str(item.get("status") or "").strip().lower() == "running"
                and not str(item.get("run_id") or "").strip()
            )
        ]
        return {
            "status": "waiting_for_slot" if remaining else "running",
            "parent_work_order_id": parent_work_order_id,
            "reason": reason,
            "started_count": len(started),
            "started": started,
            "waiting_module_ids": remaining,
        }

    async def _handle_module_detail_completed(self, state: MinionRunState, payload: dict[str, Any]) -> None:
        metadata = self._work_order_metadata_for_state(state)
        staged = dict(metadata.get("staged_planning") or {})
        parent_work_order_id = str(staged.get("parent_work_order_id") or "").strip()
        if not parent_work_order_id:
            raise ValueError("module_detail run is missing parent_work_order_id")
        artifact_ref = _stage_artifact_from_payload(payload)
        detail = load_stage_artifact_ref(artifact_ref)
        validation = validate_module_detail_artifact(detail)
        module_id = str(detail.get("module_id") or staged.get("module_id") or "").strip()
        snapshot = self.tasking_repository.read_work_order(parent_work_order_id)
        parent_metadata = dict(((snapshot.get("work_order") or {}).get("metadata") or {}) if snapshot.get("status") == "ok" else {})
        parent_staged = dict(parent_metadata.get("staged_planning") or {})
        detail_modules = {
            str(key): dict(value)
            for key, value in dict(parent_staged.get("detail_modules") or {}).items()
            if isinstance(value, dict)
        }
        current = dict(detail_modules.get(module_id) or {})
        current.update(
            {
                "status": "completed",
                "work_order_id": state.pack.work_order_id,
                "module_id": module_id,
                "artifact": dict(artifact_ref),
                "validation": dict(validation),
                "completed_at": utc_now(),
                "run_id": state.run_id,
                "minion_id": state.minion_id,
            }
        )
        detail_modules[module_id] = current
        parent_staged.update({"detail_modules": detail_modules, "updated_at": utc_now()})
        expected = [str(item) for item in list(parent_staged.get("module_ids") or []) if str(item).strip()]
        completed = [module for module in expected if str((detail_modules.get(module) or {}).get("status") or "") == "completed"]
        parent_staged["status"] = "module_detail_completed" if expected and len(completed) == len(expected) else "module_detail_running"
        self.tasking_repository.merge_work_order_metadata(parent_work_order_id, {"staged_planning": parent_staged}, work_order_status="active")
        if expected and len(completed) == len(expected):
            await self._compile_and_review_staged_plan(parent_work_order_id)
        else:
            self._schedule_waiting_module_detail_runs(parent_work_order_id, reason="module_detail_completed")

    async def _compile_and_review_staged_plan(self, parent_work_order_id: str) -> None:
        snapshot = self.tasking_repository.read_work_order(parent_work_order_id)
        if snapshot.get("status") != "ok":
            raise KeyError(f"unknown work order: {parent_work_order_id}")
        work_order = dict(snapshot.get("work_order") or {})
        metadata = dict(work_order.get("metadata") or {})
        staged = dict(metadata.get("staged_planning") or {})
        sketch_ref = dict(staged.get("sketch_artifact") or {})
        sketch = load_stage_artifact_ref(sketch_ref)
        detail_artifacts: list[dict[str, Any]] = []
        for item in dict(staged.get("detail_modules") or {}).values():
            if not isinstance(item, dict) or str(item.get("status") or "") != "completed":
                continue
            detail_artifacts.append(load_stage_artifact_ref(item.get("artifact")))
        plan = compile_final_plan_from_staged_artifacts(sketch, detail_artifacts)
        submitted = self.tasking_repository.submit_plan_ref(plan, submission_notes="compiled from staged architect sketch/detail artifacts")
        plan_ref = dict(submitted.get("plan_ref") or {})
        plan_validation = dict(submitted.get("plan_validation") or {})
        staged.update(
            {
                "status": "final_plan_compiled",
                "blocked_stage": None,
                "blocker": None,
                "compiled_plan_ref": plan_ref,
                "compiled_plan_validation": plan_validation,
                "compiled_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        workflow = dict(metadata.get("workflow") or {})
        if workflow:
            workflow.update({"status": "post_gate_pending", "post_gate": "plan_acceptance", "blocker": None, "updated_at": utc_now()})
        self.tasking_repository.merge_work_order_metadata(
            parent_work_order_id,
            {
                "staged_planning": staged,
                "plan_ref": plan_ref,
                "plan_validation": plan_validation,
                **({"workflow": workflow} if workflow else {}),
            },
            work_order_status="active",
        )
        event = {
            "event_kind": "staged_plan_compiled",
            "minion_id": "",
            "run_id": "",
            "work_order_id": parent_work_order_id,
            "minion_profile": "manager.final_plan_compiler",
            "payload": {
                "status": "compiled",
                "summary": "manager compiled staged architect artifacts into FinalPlanArtifact",
                "plan_ref": plan_ref,
                "plan_validation": plan_validation,
            },
            "created_at": utc_now(),
        }
        self._queue_event_delivery(event)
        self.tasking_repository.record_minion_event(event)
        await self._schedule_compiled_plan_review(parent_work_order_id, plan_ref, plan_validation)

    async def _schedule_compiled_plan_review(self, work_order_id: str, plan_ref: dict[str, Any], plan_validation: dict[str, Any]) -> None:
        pack = self.tasking_repository.pack_for_work_order(work_order_id)
        pack = MinionProfileRegistry(runtime_root=self.runtime_root).resolve_pack(pack)
        gate_specs = normalize_gate_policy(
            {
                "gates": ["plan_acceptance"],
                "reviewer_profile": "software_engineering.reviewer",
            }
        )
        if not gate_specs:
            raise ValueError("plan_acceptance gate spec could not be constructed")
        fake_state = MinionRunState(
            minion_id="manager",
            run_id=f"run_staged_compile_{safe_token(work_order_id)}",
            pack=pack,
            status="completed",
        )
        event = {
            "event_kind": "checkpoint",
            "minion_id": fake_state.minion_id,
            "run_id": fake_state.run_id,
            "work_order_id": work_order_id,
            "minion_profile": "manager.final_plan_compiler",
            "payload": {
                "status": "completed",
                "summary": "compiled staged plan is ready for plan acceptance review",
                "plan_ref": dict(plan_ref),
                "plan_validation": dict(plan_validation),
            },
            "created_at": utc_now(),
        }
        self.reviews._schedule_plan_review(fake_state, event, gate_specs[0])

    def _work_order_metadata_for_state(self, state: MinionRunState) -> dict[str, Any]:
        try:
            snapshot = self.tasking_repository.read_work_order(state.pack.work_order_id)
        except Exception:
            return dict(state.pack.metadata or {})
        if snapshot.get("status") != "ok":
            return dict(state.pack.metadata or {})
        return dict((snapshot.get("work_order") or {}).get("metadata") or {})

    async def _handle_workflow_terminal_post_async(self, state: MinionRunState, event: dict[str, Any]) -> None:
        payload = dict(event.get("payload") or {})
        terminal_status = str(payload.get("status") or "").strip().lower()
        metadata = self._work_order_metadata_for_state(state)
        workflow = dict(metadata.get("workflow") or {})
        if not workflow:
            return
        if terminal_status != "completed":
            workflow = update_current_workflow_step(
                workflow,
                status=terminal_status or "failed",
                output_artifact=_workflow_output_artifact_from_payload(payload),
                next_profile=NONE_PROFILE,
            )
            workflow.update({"status": terminal_status or "failed", "updated_at": utc_now()})
            self.tasking_repository.merge_work_order_metadata(
                state.pack.work_order_id,
                {"workflow": workflow},
                work_order_status=terminal_status or "failed",
            )
            return
        next_step = resolve_workflow_next(state.pack, payload)
        if next_step.get("status") != "ok":
            workflow = update_current_workflow_step(
                workflow,
                status="blocked",
                output_artifact=_workflow_output_artifact_from_payload(payload),
                next_profile=str(next_step.get("next_profile") or NONE_PROFILE),
            )
            workflow.update({"status": "blocked", "blocker": dict(next_step), "updated_at": utc_now()})
            self.tasking_repository.merge_work_order_metadata(
                state.pack.work_order_id,
                {"workflow": workflow},
                work_order_status="blocked",
            )
            return
        adapter = str(next_step.get("adapter") or "").strip()
        next_profile = str(next_step.get("next_profile") or NONE_PROFILE)
        output_artifact = _workflow_output_artifact_from_payload(payload)
        if adapter == "accepted_plan":
            workflow = update_current_workflow_step(
                workflow,
                status="post_gate_pending",
                output_artifact=output_artifact,
                next_profile=next_profile,
            )
            workflow.update({"status": "post_gate_pending", "post_gate": "plan_acceptance", "updated_at": utc_now()})
            self.tasking_repository.merge_work_order_metadata(
                state.pack.work_order_id,
                {"workflow": workflow},
                work_order_status="active",
            )
            return
        if next_profile == NONE_PROFILE:
            workflow = update_current_workflow_step(
                workflow,
                status="completed",
                output_artifact=output_artifact,
                next_profile=NONE_PROFILE,
            )
            workflow.update({"status": "completed", "updated_at": utc_now()})
            self.tasking_repository.merge_work_order_metadata(
                state.pack.work_order_id,
                {"workflow": workflow},
                work_order_status="completed",
            )
            return
        workflow = update_current_workflow_step(
            workflow,
            status="completed",
            output_artifact=output_artifact,
            next_profile=next_profile,
        )
        workflow = append_workflow_step(workflow, profile=next_profile, input_artifact=output_artifact, adapter=adapter or "direct_profile")
        self.tasking_repository.merge_work_order_metadata(
            state.pack.work_order_id,
            {"workflow": workflow, "workflow_input_artifact": output_artifact},
            work_order_status="active",
        )
        next_group, next_name = split_profile_ref(next_profile)
        next_metadata = dict(metadata)
        next_metadata.update(
            {
                "workflow": workflow,
                "workflow_input_artifact": output_artifact,
                "work_order_title": str(metadata.get("work_order_title") or state.pack.goal or "Workflow step"),
            }
        )
        next_metadata.pop("prompt_view", None)
        next_pack = TaskContextPack.from_dict(
            {
                **state.pack.to_dict(),
                "instruction": f"Run workflow step {next_profile} using metadata.workflow_input_artifact as input.",
                "profile_group": next_group or "general",
                "profile_name": next_name,
                "minion_profile": next_profile,
                "metadata": next_metadata,
            }
        )
        spawned = await self.spawn(next_pack.to_dict())
        self.tasking_repository.record_minion_event(
            {
                "event_kind": "workflow_next_dispatched",
                "minion_id": str(spawned.get("minion_id") or ""),
                "run_id": str(spawned.get("run_id") or ""),
                "work_order_id": state.pack.work_order_id,
                "minion_profile": next_profile,
                "payload": {
                    "status": "spawned",
                    "next_profile": next_profile,
                    "adapter": adapter or "direct_profile",
                    "input_artifact": output_artifact,
                    "run": dict(spawned),
                },
                "created_at": utc_now(),
            }
        )

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
        if state.control_queue is not None:
            return ""
        return self.step_executor.runner_control_unavailable_reason(state)

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

    def _should_queue_event_delivery(self, event: dict[str, Any]) -> bool:
        event_kind = str((event or {}).get("event_kind") or "")
        if event_kind in _RUNNER_TELEMETRY_EVENT_KINDS and not self.event_subscribers:
            return False
        return True

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
                "error": payload.get("error"),
                "text_preview": payload.get("text_preview"),
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
    return minion_debug_log_enabled(pack.metadata)


def _event_debug_log_requested(pack: TaskContextPack, event: dict[str, Any]) -> bool:
    metadata = dict(pack.metadata or {})
    payload = dict(event.get("payload") or {})
    payload_metadata = payload.get("metadata")
    if isinstance(payload_metadata, dict):
        metadata.update(dict(payload_metadata))
    return minion_debug_log_enabled(metadata)


def _should_record_runner_event(pack: TaskContextPack, event: dict[str, Any]) -> bool:
    event_kind = str(event.get("event_kind") or "")
    if _is_key_llm_endpoint_progress_event(event):
        return True
    if event_kind in _RUNNER_TELEMETRY_EVENT_KINDS and not _event_debug_log_requested(pack, event):
        return False
    return True


def _should_log_runner_event(pack: TaskContextPack, event: dict[str, Any]) -> bool:
    event_kind = str(event.get("event_kind") or "")
    if _is_key_llm_endpoint_progress_event(event):
        return True
    if event_kind in _RUNNER_TELEMETRY_EVENT_KINDS and not _event_debug_log_requested(pack, event):
        return False
    return True


def _is_key_llm_endpoint_progress_event(event: dict[str, Any]) -> bool:
    if str((event or {}).get("event_kind") or "") != "progress":
        return False
    payload = dict((event or {}).get("payload") or {})
    return str(payload.get("phase") or "").strip() in _KEY_LLM_ENDPOINT_PROGRESS_PHASES


def _llm_endpoint_progress_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = dict(event or {})
    phase = str(payload.get("phase") or "llm_endpoint_event").strip() or "llm_endpoint_event"
    payload["phase"] = phase
    payload["force_ledger"] = True
    payload.setdefault("summary", _llm_endpoint_progress_summary(phase, payload))
    return payload


def _llm_endpoint_progress_summary(phase: str, payload: dict[str, Any]) -> str:
    endpoint = payload.get("endpoint_id") or payload.get("model_id") or "endpoint"
    if phase == "llm_endpoint_attempt_failed":
        return f"LLM endpoint {endpoint} attempt {payload.get('attempt')}/{payload.get('max_attempts')} failed: {payload.get('error_kind') or 'error'}"
    if phase == "llm_endpoint_exhausted":
        next_endpoint = str(payload.get("next_endpoint_id") or "").strip()
        suffix = f"; falling back to {next_endpoint}" if next_endpoint else ""
        return f"LLM endpoint {endpoint} exhausted after {payload.get('attempt')}/{payload.get('max_attempts')}{suffix}"
    if phase == "llm_endpoint_fallback_started":
        return f"LLM fallback started: {endpoint}"
    if phase == "llm_endpoint_fallback_succeeded":
        return f"LLM fallback succeeded: {endpoint}"
    if phase == "llm_endpoint_skipped":
        return f"LLM endpoint skipped: {endpoint} ({payload.get('reason') or 'skipped'})"
    return f"LLM endpoint event: {endpoint}"


def _is_plan_parent_pack(pack: TaskContextPack) -> bool:
    metadata = dict(pack.metadata or {})
    plan_execution = dict(metadata.get("plan_execution") or {})
    return str(plan_execution.get("mode") or "") == "module_parent_milestones"


def _state_last_reported_final_milestone_completed(state: MinionRunState) -> bool:
    events = list(state.ledger or [])
    if state.last_event and (not events or dict(events[-1]) != dict(state.last_event)):
        events.append(dict(state.last_event))
    for raw_event in reversed(events):
        event = dict(raw_event or {})
        if str(event.get("event_kind") or "") != "milestone_completed":
            continue
        return _event_reports_final_milestone_completed(state, event)
    return False


def _event_reports_final_milestone_completed(state: MinionRunState, event: dict[str, Any]) -> bool:
    if str(event.get("event_kind") or "") != "milestone_completed":
        return False
    payload = dict(event.get("payload") or {})
    if str(payload.get("status") or "").strip().lower() != "completed":
        return False
    try:
        milestone_index = int(payload.get("milestone_index") or 0)
    except (TypeError, ValueError):
        milestone_index = 0
    milestones = list((state.pack.metadata or {}).get("milestones") or [])
    if milestones:
        return milestone_index >= len(milestones) - 1
    prompt_view = dict((state.pack.metadata or {}).get("prompt_view") or {})
    current = dict(prompt_view.get("milestone") or state.pack.continuity.get("current_milestone") or {})
    if not current:
        return True
    try:
        return milestone_index >= int(current.get("milestone_index") or 0)
    except (TypeError, ValueError):
        return True


def _plan_execution_dag_state(plan_execution: dict[str, Any]) -> dict[str, Any]:
    return _dag_state_to_runtime_dict(dict(plan_execution.get("dag_state") or plan_execution.get("module_dag") or {}))


def _pre_plan_gate_spec(pack: TaskContextPack) -> GateSpec | None:
    if not _pre_plan_contract_requested(pack):
        return None
    for spec in gate_specs_from_pack(pack, trigger=GATE_TRIGGER_BEFORE_PLAN):
        if spec.strategy == "reviewer" and spec.target_kind in {"", "work_order"}:
            return spec
    for spec in _explicit_pre_plan_gate_specs(pack):
        if spec.trigger == GATE_TRIGGER_BEFORE_PLAN and spec.strategy == "reviewer" and spec.target_kind in {"", "work_order"}:
            return spec
    return None


def _explicit_pre_plan_gate_specs(pack: TaskContextPack) -> list[GateSpec]:
    for source in (dict(pack.metadata or {}), dict(pack.workspace or {})):
        policy = source.get("pre_plan_gate_policy") or source.get("source_contract_gate_policy")
        if isinstance(policy, dict):
            return normalize_gate_policy(policy)
    return normalize_gate_policy({"gates": [SOURCE_CONTRACT_GATE]})


def _pre_plan_contract_requested(pack: TaskContextPack) -> bool:
    metadata = dict(pack.metadata or {})
    workspace = dict(pack.workspace or {})
    if _coerce_bool(metadata.get("skip_pre_plan_contract")) or _coerce_bool(workspace.get("skip_pre_plan_contract")):
        return False
    for source in (metadata, workspace):
        for key in ("enable_pre_plan_contract", "require_pre_plan_contract", "pre_plan_contract_required"):
            if _coerce_bool(source.get(key)):
                return True
        state = source.get("pre_plan_contract")
        if isinstance(state, dict):
            if _coerce_bool(state.get("enabled")) or _coerce_bool(state.get("required")):
                return True
            if str(state.get("status") or "").strip():
                return True
            if str(state.get("compiler_work_order_id") or "").strip():
                return True
    return False


def _is_pre_plan_contract_compiler_pack(pack: TaskContextPack) -> bool:
    metadata = dict(pack.metadata or {})
    return bool(metadata.get("pre_plan_contract_compiler"))


def _pre_plan_contract_state(pack: TaskContextPack) -> dict[str, Any]:
    metadata = dict(pack.metadata or {})
    state = metadata.get("pre_plan_contract")
    return dict(state) if isinstance(state, dict) else {}


def _gate_contract_from_pack(pack: TaskContextPack) -> dict[str, Any]:
    for value in (
        pack.workspace.get("gate_contract"),
        pack.workspace.get("source_gate_contract"),
        pack.metadata.get("gate_contract"),
        _pre_plan_contract_state(pack).get("gate_contract"),
    ):
        if isinstance(value, dict) and _active_gate_contract_checks(value):
            return dict(value)
    return {}


def _active_gate_contract_checks(contract: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in list(contract.get("checks") or contract.get("checklist") or contract.get("items") or [])
        if isinstance(item, dict) and not bool(item.get("deleted"))
    ]


def _build_pre_plan_contract_compiler_pack(pack: TaskContextPack, spec: GateSpec) -> TaskContextPack:
    source_contract = _source_contract_from_pack(pack)
    compiler_work_order_id = f"wo_pre_plan_contract_{safe_token(pack.work_order_id)}_{uuid4().hex[:8]}"
    workspace = dict(pack.workspace or {})
    repo_path = str(workspace.get("repo_path") or workspace.get("source_repo") or "").strip()
    artifact_dir = str(workspace.get("artifact_dir") or "").strip()
    review_target = {
        "gate_kind": "source_contract",
        "work_order_id": pack.work_order_id,
        "source_work_order_id": pack.work_order_id,
        "source_profile": pack.minion_profile,
        "source_contract": source_contract,
        "gate_spec": spec.to_dict(),
        "repo_path": repo_path,
        "artifact_dir": artifact_dir,
    }
    acceptance_criteria = list(spec.required_checks) or [
        "Compile the source task into indexed semantic-first gate_contract checks before planner work starts.",
        "Hard user/work-order requirements must be preserved exactly and outrank planning heuristics.",
        "Use mechanical predicates only for dispatch/topology invariants Pal can verify and that must block plan execution; keep API, type, implementation, test-quality, and behavior requirements semantic.",
        "Submit gate_contract_submit with the compiled gate_contract.",
    ]
    review_workspace = {
        "repo_path": repo_path,
        "artifact_dir": artifact_dir,
        "workspace_policy": {"mode": "read_only_repo"},
        "pre_plan_contract_source_work_order_id": pack.work_order_id,
        "review_source_work_order_id": pack.work_order_id,
        "review_target_gate_kind": "source_contract",
        "review_target_source_contract": source_contract,
        "review_target_gate_spec": spec.to_dict(),
    }
    reviewer_order = ReviewerWorkOrder(
        work_order_id=compiler_work_order_id,
        task_id=f"compile_contract_{safe_token(pack.work_order_id)}",
        review_target=review_target,
        acceptance_criteria=acceptance_criteria,
        allowed_capabilities=[],
        output_contract={"must_submit": "op_minion_gate_contract_submit"},
        metadata={"workspace": review_workspace},
    )
    metadata = {
        "task_id": reviewer_order.task_id,
        "task_title": f"Compile source contract for {pack.work_order_id}",
        "work_order_title": f"Compile source contract for {pack.work_order_id}",
        "review_target": review_target,
        "reviewer_work_order": reviewer_order.to_dict(),
        "prompt_view": prompt_view_for_reviewer(reviewer_order),
        "milestones": ["Compile source contract and submit gate_contract"],
        "pre_plan_contract_compiler": True,
        "pre_plan_contract_source_work_order_id": pack.work_order_id,
        "pre_plan_contract_source_pack": pack.to_dict(),
        "gate_spec": spec.to_dict(),
    }
    if isinstance(pack.metadata.get("control_route"), dict):
        metadata["control_route"] = dict(pack.metadata.get("control_route") or {})
    for key in ("preferred_endpoint_id", "preferred_endpoint_source", "prompt_observation_tag"):
        if key in (pack.metadata or {}):
            metadata[key] = (pack.metadata or {})[key]
    reviewer_group = str(spec.reviewer_profile_group or "").strip()
    reviewer_name = str(spec.reviewer_profile_name or "").strip()
    if not reviewer_group or not reviewer_name:
        raise ValueError("source-contract gate requires an explicit reviewer profile")
    return TaskContextPack.from_dict(
        {
            "work_order_id": compiler_work_order_id,
            "goal": f"Compile source contract for {pack.work_order_id}",
            "instruction": (
                "Compile the original work order into a structured gate_contract before planner work starts. "
                "Do not create an implementation plan and do not modify the source repository. "
                "Convert each hard user/work-order requirement into a semantic gate check by default. "
                "Use kind=mechanical or hybrid only for dispatch/topology invariants Pal can verify and that must block plan execution, "
                "such as dangling refs, impossible dependency order, or an explicitly binding structural bound. "
                "Do not encode public API symbols, label sets, type contracts, test-count quality, or implementation behavior as mechanical count checks; keep those semantic with concrete evidence expectations. "
                "Submit the result with gate_contract_submit before completing."
            ),
            "acceptance_criteria": acceptance_criteria,
            "workspace": review_workspace,
            "profile_group": reviewer_group,
            "profile_name": reviewer_name,
            "allowed_capabilities": list(SOURCE_CONTRACT_REVIEWER_CAPABILITIES),
            "metadata": metadata,
        }
    )


def _source_contract_from_pack(pack: TaskContextPack) -> dict[str, Any]:
    metadata = dict(pack.metadata or {})
    prompt_view = dict(metadata.get("prompt_view") or {})
    criteria: list[str] = []
    criteria.extend(_string_list(pack.acceptance_criteria))
    criteria.extend(_string_list(metadata.get("acceptance_criteria")))
    criteria.extend(_string_list(prompt_view.get("acceptance_criteria")))
    contract = {
        "goal": str(pack.goal or "").strip(),
        "instruction": str(pack.instruction or "").strip(),
        "acceptance_criteria": _dedupe_strings(criteria),
    }
    overall = _dedupe_strings(
        [
            *_string_list(metadata.get("overall_acceptance_criteria")),
            *_string_list(prompt_view.get("overall_acceptance_criteria")),
        ]
    )
    if overall:
        contract["overall_acceptance_criteria"] = overall
    for key in ("task_id", "task_title", "work_order_title"):
        value = str(metadata.get(key) or "").strip()
        if value:
            contract[key] = value
    return {key: value for key, value in contract.items() if value not in ("", [], {})}


def _workflow_output_artifact_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    primary = payload.get("primary_artifact")
    artifacts = [dict(item) for item in list(payload.get("artifacts") or []) if isinstance(item, dict)]
    declared_next = payload.get("workflow_next")
    if isinstance(primary, dict):
        result = {"primary_artifact": dict(primary), "artifacts": artifacts}
        if isinstance(declared_next, dict):
            result["workflow_next"] = dict(declared_next)
        return result
    plan_ref = payload.get("plan_ref")
    if isinstance(plan_ref, dict):
        result: dict[str, Any] = {"plan_ref": dict(plan_ref)}
        if isinstance(payload.get("plan_validation"), dict):
            result["plan_validation"] = dict(payload.get("plan_validation") or {})
        if isinstance(declared_next, dict):
            result["workflow_next"] = dict(declared_next)
        return result
    if artifacts:
        result = {"artifacts": artifacts}
        if isinstance(declared_next, dict):
            result["workflow_next"] = dict(declared_next)
        return result
    return {"summary": str(payload.get("summary") or ""), "status": str(payload.get("status") or "")}


def _stage_artifact_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    primary = payload.get("primary_artifact")
    if isinstance(primary, dict) and (primary.get("path") or primary.get("relative_path") or primary.get("stage_path")):
        return dict(primary)
    artifact = payload.get("artifact")
    if isinstance(artifact, dict) and (artifact.get("path") or artifact.get("relative_path") or artifact.get("stage_path")):
        return dict(artifact)
    artifacts = [dict(item) for item in list(payload.get("artifacts") or []) if isinstance(item, dict)]
    for item in artifacts:
        if item.get("path") or item.get("relative_path") or item.get("stage_path"):
            return item
    raise ValueError("staged planning terminal payload is missing a primary artifact")


def _should_try_workflow_resume_after_module_resume(result: dict[str, Any]) -> bool:
    if str(result.get("status") or "").strip().lower() != "skipped":
        return False
    return str(result.get("reason") or "").strip() in _WORKFLOW_RESUME_FALLBACK_REASONS


def _current_workflow_resume_step_index(workflow: dict[str, Any], steps: list[dict[str, Any]]) -> int:
    current_step_id = str(workflow.get("current_step_id") or "").strip()
    if current_step_id:
        for index, step in enumerate(steps):
            if str(step.get("step_id") or "").strip() == current_step_id:
                return index
    for index in range(len(steps) - 1, -1, -1):
        if str(steps[index].get("status") or "").strip().lower() in _WORKFLOW_RESUMABLE_STATUSES:
            return index
    return len(steps) - 1 if steps else -1


def _workflow_resume_metadata(
    metadata: dict[str, Any],
    workflow: dict[str, Any],
    *,
    step_index: int,
    reason: str,
    work_order_status: str,
    preferred_endpoint_id: str = "",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    updated_metadata = dict(metadata or {})
    updated_workflow = dict(workflow or {})
    steps = [dict(item) for item in list(updated_workflow.get("steps") or []) if isinstance(item, dict)]
    previous_step = dict(steps[step_index])
    now = utc_now()
    profile = str(previous_step.get("profile") or updated_workflow.get("current_profile") or "").strip()
    step_id = str(previous_step.get("step_id") or f"step_{step_index}").strip()
    resume_count = int(_coerce_int(previous_step.get("resume_count"), default=0)) + 1
    resume_entry: dict[str, Any] = {
        "resumed_at": now,
        "reason": str(reason or "manager resumed workflow step"),
        "step_id": step_id,
        "profile": profile,
        "attempt": resume_count,
        "previous_workflow_status": str(updated_workflow.get("status") or ""),
        "previous_step_status": str(previous_step.get("status") or ""),
        "previous_work_order_status": str(work_order_status or ""),
    }
    for source_key in ("blocker",):
        source = updated_workflow.get(source_key)
        if isinstance(source, dict) and source:
            resume_entry[source_key] = dict(source)
    for source_key in ("output_artifact", "next_profile", "completed_at", "started_at"):
        if source_key not in previous_step:
            continue
        value = previous_step.get(source_key)
        resume_entry[f"previous_{source_key}"] = dict(value) if isinstance(value, dict) else value

    resumed_step = dict(previous_step)
    resumed_step.update(
        {
            "step_id": step_id,
            "profile": profile,
            "status": "running",
            "started_at": now,
            "resumed_at": now,
            "resume_count": resume_count,
        }
    )
    for stale_key in ("completed_at", "output_artifact", "next_profile"):
        resumed_step.pop(stale_key, None)
    steps[step_index] = resumed_step

    history = [dict(item) for item in list(updated_workflow.get("resume_history") or []) if isinstance(item, dict)]
    history.append(resume_entry)
    updated_workflow.update(
        {
            "steps": steps,
            "current_step_id": step_id,
            "current_profile": profile,
            "status": "running",
            "updated_at": now,
            "resume_history": history[-50:],
            "blocker": None,
        }
    )
    updated_metadata["workflow"] = updated_workflow
    updated_metadata["workflow_resume"] = dict(resume_entry)
    updated_metadata["prompt_view"] = None
    input_artifact = resumed_step.get("input_artifact")
    if isinstance(input_artifact, dict) and input_artifact:
        updated_metadata["workflow_input_artifact"] = dict(input_artifact)
    endpoint = str(preferred_endpoint_id or "").strip()
    if endpoint:
        updated_metadata["preferred_endpoint_id"] = endpoint
        updated_metadata["preferred_endpoint_source"] = "resume_work_order"
    return updated_metadata, updated_workflow, resume_entry


def _is_architect_workflow_step(profile: str, metadata: dict[str, Any]) -> bool:
    profile_ref = str(profile or "").strip().lower()
    if profile_ref == "architect" or profile_ref.endswith(".architect"):
        return True
    planner_work_order = metadata.get("planner_work_order")
    if isinstance(planner_work_order, dict) and str(planner_work_order.get("role") or "").strip().lower() == "architect":
        return True
    return isinstance(metadata.get("architect_work_order"), dict)


def _with_architect_workflow_resume_metadata(
    metadata: dict[str, Any],
    *,
    goal: str,
    work_order_id: str,
    reason: str,
) -> dict[str, Any]:
    updated = dict(metadata or {})
    planner_work_order = dict(updated.get("planner_work_order") or updated.get("architect_work_order") or {})
    if not planner_work_order:
        planner_work_order = build_planner_work_order(
            goal=goal,
            task_id=str(updated.get("task_id") or ""),
            work_order_id=work_order_id,
            turn_index=0,
            plan_revision=0,
        )
    planner_work_order["role"] = "architect"
    planner_work_order["turn_index"] = int(_coerce_int(planner_work_order.get("turn_index"), default=0)) + 1
    planner_work_order["plan_revision"] = int(_coerce_int(planner_work_order.get("plan_revision"), default=0)) + 1
    planner_work_order["resume_reason"] = str(reason or "manager resumed workflow step")
    planner_work_order["resumed_at"] = utc_now()
    updated["planner_work_order"] = planner_work_order
    if isinstance(updated.get("architect_work_order"), dict):
        updated["architect_work_order"] = dict(planner_work_order)
    return updated


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


def _coerce_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    return parsed


def _debug_log_path_from_pack(pack: TaskContextPack) -> str:
    debug_log = dict((pack.metadata or {}).get("debug_log") or {})
    if not bool(debug_log.get("enabled")):
        return ""
    return str(debug_log.get("path") or "")


def _runs_from_dag_tick(ticked: dict[str, Any]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if not isinstance(value, dict):
            return
        run_id = str(value.get("run_id") or "").strip()
        if not run_id or run_id in seen:
            return
        seen.add(run_id)
        runs.append(dict(value))

    add(ticked.get("run"))
    for item in list(ticked.get("runs") or []):
        add(item)
    return runs
