from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from pal.execution import CapabilityCall
from pal.foundation import utc_now
from pal.foundation.service_logging import current_service_log_sink_description
from pal.foundation.sidecar import dispatch_sidecar_request, pack_sidecar_message, read_sidecar_message
from pal.llm.runtime import scoped_llm_event_sink
from pal.minion.catalog import MinionCatalogService
from pal.minion.config import effective_minion_runtime_config
from pal.minion.event_delivery import MinionEventDelivery
from pal.minion.ipc import (
    cleanup_manager_endpoint,
    cleanup_role_gateway_endpoint,
    start_manager_server,
    start_role_gateway_server,
)
from pal.minion.llm_broker import (
    llm_outcome_to_payload,
    llm_request_from_payload,
    preflight_advice_to_payload,
    preflight_request_from_payload,
    stream_event_to_payload,
)
from pal.minion.web_broker import web_result_to_payload
from pal.minion.v2.contracts import AggregateType
from pal.minion.v2.orchestration import MinionV2OutboxProcessor
from pal.minion.v2.recovery import MinionV2Recovery
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.semantic_orchestration import SemanticOrchestrator
from pal.minion.v2.role_gateway import RoleAssignmentGateway
from pal.shared import MinionApprovalDecision, MinionInvocationPack, RuntimeStatus


_DEFAULT_MAX_PARALLEL_NODES = 5
_DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 600.0
_TERMINAL_RUN_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "killed", "timeout", "suspended", "interrupted"}
)


def _pending_clarification_status(payload: Mapping[str, Any]) -> dict[str, Any]:
    questions = [
        dict(item)
        for item in list(payload.get("questions") or [])
        if isinstance(item, Mapping)
    ]
    question = questions[0] if questions else {}
    options = [
        {
            "label": str(option.get("label") or option.get("answer") or "").strip(),
            "description": str(
                option.get("description")
                or option.get("label")
                or option.get("answer")
                or ""
            ).strip(),
        }
        for option in list(question.get("options") or [])
        if isinstance(option, Mapping)
    ]
    return {
        "title": str(
            question.get("title") or payload.get("title") or "Architecture question"
        ).strip(),
        "question": str(
            question.get("question")
            or payload.get("question")
            or payload.get("summary")
            or "Minion needs clarification."
        ).strip(),
        "options": options[:3],
    }


@dataclass
class MinionRunState:
    minion_id: str
    run_id: str
    pack: MinionInvocationPack
    process: asyncio.subprocess.Process | None = None
    status: str = "running"
    started_at: str = field(default_factory=utc_now)
    ended_at: str = ""
    last_error: str = ""
    last_event: dict[str, Any] = field(default_factory=dict)
    last_event_at: str = ""
    pending_approval: dict[str, Any] = field(default_factory=dict)
    pending_clarification: dict[str, Any] = field(default_factory=dict)
    pending_terminal_status: str = ""
    process_group_reaped: bool = False

    def summary(self) -> dict[str, Any]:
        active = self.status not in _TERMINAL_RUN_STATUSES
        return {
            "minion_id": self.minion_id,
            "run_id": self.run_id,
            "invocation_id": self.pack.invocation_id,
            "minion_profile": self.pack.minion_profile,
            "status": self.status,
            "run_active": active,
            "pid": self.process.pid if active and self.process is not None else None,
            "returncode": self.process.returncode if self.process is not None else None,
            "instruction": self.pack.instruction,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "last_error": self.last_error,
            "last_event_at": self.last_event_at,
            "last_event": dict(self.last_event),
            "pending_approval": dict(self.pending_approval),
            "pending_clarification": dict(self.pending_clarification),
            "pending_terminal_status": self.pending_terminal_status,
            "process_group_reaped": self.process_group_reaped,
        }

    def detail(self) -> dict[str, Any]:
        return {**self.summary(), "task_context_pack": self.pack.to_dict()}


@dataclass
class MinionManager:
    runtime_root: Path
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("pal.minion.manager"))
    max_parallel_modules: int | None = None
    graceful_shutdown_timeout_seconds: float = _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    server: asyncio.base_events.Server | None = None
    role_server: asyncio.base_events.Server | None = None
    endpoint_info: dict[str, Any] = field(default_factory=dict)
    role_endpoint_info: dict[str, Any] = field(default_factory=dict)
    runs: dict[str, MinionRunState] = field(default_factory=dict)
    started_at: str = field(default_factory=utc_now)
    catalog: MinionCatalogService = field(init=False)
    catalog_bootstrap: dict[str, Any] = field(init=False, default_factory=dict)
    events: MinionEventDelivery = field(init=False)
    v2_service: MinionV2WorkflowService = field(init=False)
    v2_outbox: MinionV2OutboxProcessor = field(init=False)
    v2_semantic_orchestrator: SemanticOrchestrator = field(init=False)
    role_gateway: RoleAssignmentGateway = field(init=False)
    _host_broker_bundle: Any | None = field(default=None, init=False, repr=False)
    _shutdown_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _drain_requested: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _v2_wake_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _v2_outbox_task: asyncio.Task[Any] | None = field(default=None, init=False, repr=False)
    _drain_task: asyncio.Task[Any] | None = field(default=None, init=False, repr=False)
    _shutdown_reason: str = field(default="", init=False)
    _shutdown_started_at: str = field(default="", init=False)
    _skill_database: Any | None = field(default=None, init=False, repr=False)
    _skill_inject_tool: Any | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.catalog = MinionCatalogService(Path(self.runtime_root))
        self.catalog_bootstrap = self.catalog.bootstrap()
        config = effective_minion_runtime_config(Path(self.runtime_root))
        configured = config.get("max_parallel_llm_nodes", config.get("max_parallel_modules", _DEFAULT_MAX_PARALLEL_NODES))
        self.max_parallel_modules = max(1, int(self.max_parallel_modules or configured or _DEFAULT_MAX_PARALLEL_NODES))
        self.events = MinionEventDelivery()
        self.v2_service = MinionV2WorkflowService(Path(self.runtime_root))
        self.role_gateway = RoleAssignmentGateway(self.v2_service)
        self.v2_semantic_orchestrator = SemanticOrchestrator(
            self.v2_service,
            max_parallel_workers=self.max_parallel_modules,
            publish_human_review=self._publish_v2_human_review,
            publish_worker_event=self._publish_v2_worker_event,
            register_broker_run=self._register_v2_broker_run,
            unregister_broker_run=self._unregister_v2_broker_run,
            inject_skill=self._inject_skill_for_role,
        )
        self.v2_outbox = MinionV2OutboxProcessor(
            self.v2_service,
            semantic_effects=self.v2_semantic_orchestrator,
            max_parallel_nodes=self.max_parallel_modules,
        )

    @property
    def event_queue(self) -> list[dict[str, Any]]:
        return self.events.queue

    @property
    def event_subscribers(self) -> list[asyncio.StreamWriter]:
        return self.events.subscribers

    async def run(self) -> None:
        recovery = await asyncio.to_thread(MinionV2Recovery(self.v2_service).recover)
        self.server, self.endpoint_info = await start_manager_server(self.runtime_root, self._handle_client)
        self.role_server, self.role_endpoint_info = await start_role_gateway_server(
            self.runtime_root,
            self._handle_role_client,
        )
        self.logger.info(
            "minion manager listening: %s role_gateway=%s recovery=%s",
            self.endpoint_info,
            self.role_endpoint_info,
            recovery,
        )
        remove_signals = self._install_signal_handlers()
        async with self.server, self.role_server:
            serve_task = asyncio.create_task(self.server.serve_forever(), name="minion-manager-serve")
            role_serve_task = asyncio.create_task(
                self.role_server.serve_forever(),
                name="minion-role-gateway-serve",
            )
            recovered_assignments = await self.v2_semantic_orchestrator.recover_background_assignments()
            if recovered_assignments:
                self.logger.info(
                    "recovered %s durable role assignment(s)",
                    recovered_assignments,
                )
            self._v2_outbox_task = asyncio.create_task(self._run_v2_outbox(), name="minion-v2-outbox")
            try:
                await self._shutdown_event.wait()
            finally:
                remove_signals()
                self.v2_semantic_orchestrator.request_stop()
                if self._v2_outbox_task is not None:
                    self._v2_outbox_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._v2_outbox_task
                await self.v2_outbox.stop_background()
                # Keep both IPC endpoints alive while active workers reach a
                # safe point and persist their final receipt or continuation.
                await self.close_all()
                serve_task.cancel()
                role_serve_task.cancel()
                self.server.close()
                self.role_server.close()
                await self.server.wait_closed()
                await self.role_server.wait_closed()
                with contextlib.suppress(asyncio.CancelledError):
                    await serve_task
                with contextlib.suppress(asyncio.CancelledError):
                    await role_serve_task
                await self.events.close()
                if self._skill_database is not None:
                    with contextlib.suppress(Exception):
                        self._skill_database.close()
                    self._skill_database = None
                    self._skill_inject_tool = None
                await cleanup_manager_endpoint(self.runtime_root)
                await cleanup_role_gateway_endpoint(self.runtime_root)

    def _inject_skill_for_role(self, skill_id: str) -> dict[str, str]:
        if self._skill_inject_tool is None:
            from pal.foundation import PalV2Database
            from pal.skill.repository import SkillRepository
            from pal.skill.service import SkillService
            from pal.skill.tools import SkillInjectTool
            from pal.behavior.models import BehaviorSkillModel

            self._skill_database = PalV2Database(
                db_path=Path(self.runtime_root) / "pal.sqlite3",
                read_only=True,
            )
            self._skill_database.initialize((BehaviorSkillModel,))
            self._skill_inject_tool = SkillInjectTool(
                service=SkillService(
                    repository=SkillRepository(),
                    runtime_root=Path(self.runtime_root),
                )
            )
        result = self._skill_inject_tool.invoke({"skill_id": str(skill_id)})
        if result.status != RuntimeStatus.OK:
            reason = str(
                dict(result.structured or {}).get("reason")
                or result.text
                or "skill_injection_failed"
            )
            raise ValueError(f"{skill_id}: {reason}")
        reminder = str(result.llm_text or "").strip()
        if not reminder:
            raise ValueError(f"{skill_id}: skill injection returned no reminder")
        return {
            "skill_id": str(dict(result.structured or {}).get("skill_id") or skill_id),
            "system_reminder": reminder,
        }

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while not reader.at_eof():
                try:
                    request = await read_sidecar_message(reader)
                except asyncio.IncompleteReadError:
                    return
                if str(request.get("method") or "") == "subscribe_events":
                    await self.events.handle_subscription(request, reader, writer, shutdown_event=self._shutdown_event)
                    return
                writer.write(pack_sidecar_message(await self._dispatch(request)))
                await writer.drain()
        except (ConnectionError, OSError, ValueError):
            return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        return await dispatch_sidecar_request(
            request,
            self._call_method,
            error_kind=lambda _exc: "manager",
            logger=self.logger,
        )

    async def _handle_role_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while not reader.at_eof():
                try:
                    request = await read_sidecar_message(reader)
                except asyncio.IncompleteReadError:
                    return
                writer.write(
                    pack_sidecar_message(
                        await dispatch_sidecar_request(
                            request,
                            self._call_worker_method,
                            error_kind=lambda _exc: "role_gateway",
                            logger=self.logger,
                        )
                    )
                )
                await writer.drain()
        except (ConnectionError, OSError, ValueError):
            return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _call_worker_method(
        self,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        broker_handlers = {
            "llm_preflight": self.llm_broker_preflight,
            "llm_generate": self.llm_broker_generate,
            "llm_generate_stream": self.llm_broker_generate_stream,
            "llm_resolve_max_output_tokens": self.llm_broker_resolve_max_output_tokens,
            "llm_resolve_endpoint_facts": self.llm_broker_resolve_endpoint_facts,
            "web_search": self.web_broker_search,
            "web_read": self.web_broker_read,
        }
        if method in broker_handlers:
            payload = dict(params or {})
            token = str(payload.pop("access_token", ""))
            authenticated = await asyncio.to_thread(
                self.role_gateway.authorize,
                token,
            )
            run_id = str(payload.get("run_id") or "")
            run = self.runs.get(run_id)
            assignment = dict(authenticated.get("assignment") or {})
            if run is None or run.minion_id != str(assignment.get("session_id") or ""):
                raise PermissionError(
                    "role assignment token does not own the requested broker run"
                )
            return await broker_handlers[method](payload)
        return await asyncio.to_thread(self.role_gateway.call, method, params)

    async def _call_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "llm_preflight": self.llm_broker_preflight,
            "llm_generate": self.llm_broker_generate,
            "llm_generate_stream": self.llm_broker_generate_stream,
            "llm_resolve_max_output_tokens": self.llm_broker_resolve_max_output_tokens,
            "llm_resolve_endpoint_facts": self.llm_broker_resolve_endpoint_facts,
            "send_decision": self.send_decision,
            "send_clarification": self.send_clarification,
            "answer_workflow_question": self.answer_workflow_question,
        }
        if method in handlers:
            payload = dict(params.get("decision") or {}) if method == "send_decision" else dict(params.get("clarification") or {}) if method == "send_clarification" else params
            return await handlers[method](payload)
        if method == "health":
            return self.health()
        if method == "reload_runtime_config":
            return self.reload_runtime_config()
        if method == "catalog_snapshot":
            return self.catalog.snapshot(
                kind=str(params.get("kind") or "all"),
                query=str(params.get("query") or ""),
                include_definitions=bool(params.get("include_definitions", False)),
            )
        if method == "catalog_refresh":
            return self.catalog.refresh(actor=str(params.get("actor") or "pal"))
        if method == "catalog_set_profile_override":
            return self.catalog.set_profile_override(
                profile=str(params.get("profile") or ""),
                changes=dict(params.get("changes") or {}),
                actor=str(params.get("actor") or "pal"),
                if_generation=str(params.get("if_generation") or ""),
            )
        if method == "catalog_reset_profile_override":
            return self.catalog.reset_profile_override(
                profile=str(params.get("profile") or ""),
                actor=str(params.get("actor") or "pal"),
                if_generation=str(params.get("if_generation") or ""),
            )
        if method == "catalog_set_family_override":
            return self.catalog.set_family_override(
                family=str(params.get("family") or ""),
                changes=dict(params.get("changes") or {}),
                actor=str(params.get("actor") or "pal"),
                if_generation=str(params.get("if_generation") or ""),
            )
        if method == "catalog_reset_family_override":
            return self.catalog.reset_family_override(
                family=str(params.get("family") or ""),
                actor=str(params.get("actor") or "pal"),
                if_generation=str(params.get("if_generation") or ""),
            )
        if method == "v2_wake":
            self._v2_wake_event.set()
            return {"ok": True, "status": "woken"}
        if method == "v2_workflow_status":
            return self._v2_workflow_status(
                str(params.get("workflow_id") or ""),
                view=str(params.get("view") or "status"),
            )
        if method == "list_runs":
            return {"items": [item.summary() for item in sorted(self.runs.values(), key=lambda run: run.started_at)]}
        if method == "read_run":
            run_id = str(params.get("run_id") or "")
            if run_id not in self.runs:
                raise KeyError(f"unknown minion run: {run_id}")
            return self.runs[run_id].detail()
        if method == "shutdown":
            return self.request_shutdown(
                reason=str(params.get("reason") or "manager_shutdown"),
                timeout_seconds=float(params.get("timeout_seconds") or self.graceful_shutdown_timeout_seconds),
                graceful=bool(params.get("graceful", True)),
            )
        raise ValueError(f"unknown Minion V2 manager method: {method}")

    async def _run_v2_outbox(self) -> None:
        while not self._shutdown_event.is_set():
            if self._drain_requested.is_set():
                await asyncio.sleep(0.05)
                continue
            try:
                await self.v2_semantic_orchestrator.recover_background_assignments()
                if self.v2_outbox.start_available(max_concurrency=self.max_parallel_modules + 8):
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
        standalone = bool(payload.get("standalone_review_id"))
        self.events.queue_event(
            {
                "event_kind": "standalone_review_completed" if standalone else "architecture_review_pending",
                "minion_id": "",
                "run_id": "",
                "workflow_id": str(payload.get("workflow_id") or ""),
                "minion_profile": "minion_v2.reviewer",
                "role_mode": "standalone" if standalone else "architecture",
                "payload": {**dict(payload), "minion_v2": True, **({"status": "completed"} if standalone else {})},
                "created_at": utc_now(),
            }
        )

    async def _publish_v2_worker_event(self, event: Mapping[str, Any]) -> None:
        item = dict(event)
        run_id = str(item.get("run_id") or "")
        state = self.runs.get(run_id)
        if state is not None:
            payload = dict(item.get("payload") or {})
            kind = str(item.get("event_kind") or "")
            binding = dict(dict(state.pack.metadata or {}).get("minion_v2") or {})
            workflow_id = str(binding.get("workflow_id") or "")
            if workflow_id and not item.get("workflow_id"):
                item["workflow_id"] = workflow_id
            if kind in {"approval_requested", "clarification_requested"}:
                control_route = dict(binding.get("control_route") or {})
                if workflow_id:
                    workflow = self.v2_service.repository.read_snapshot(
                        AggregateType.WORKFLOW,
                        workflow_id,
                    )
                    if workflow is not None:
                        current_route = dict(
                            workflow.payload.get("control_route") or {}
                        )
                        if current_route:
                            control_route = current_route
                if control_route:
                    payload["control_route"] = control_route
            item["payload"] = payload
            if kind == "approval_requested":
                state.pending_approval = payload
                state.status = "approval_pending"
            elif kind == "clarification_requested":
                state.pending_clarification = payload
                state.status = "clarification_pending"
            elif kind == "terminal":
                # A terminal IPC receipt means the worker has finished its
                # logical work, not that its process group is gone.  Keep the
                # run active until the RAII process owner confirms reap.
                terminal_status = str(payload.get("status") or "completed")
                if state.process_group_reaped:
                    state.status = terminal_status
                    state.pending_terminal_status = ""
                    state.ended_at = state.ended_at or utc_now()
                else:
                    state.pending_terminal_status = terminal_status
                    state.status = "exiting"
            state.last_event = item
            state.last_event_at = str(item.get("created_at") or utc_now())
        self.v2_service.repository.record_worker_event(item)
        self.events.queue_event(item)

    def _register_v2_broker_run(
        self,
        run_id: str,
        minion_id: str,
        pack: MinionInvocationPack,
        process: asyncio.subprocess.Process,
    ) -> None:
        self.runs[run_id] = MinionRunState(minion_id=minion_id, run_id=run_id, pack=pack, process=process)

    def _unregister_v2_broker_run(
        self,
        run_id: str,
        process_group_reaped: bool,
    ) -> None:
        if not process_group_reaped:
            raise RuntimeError(
                "cannot unregister worker before its process group is reaped"
            )
        state = self.runs.get(run_id)
        if state is not None:
            state.process_group_reaped = True
            state.status = (
                state.pending_terminal_status
                # A clean OS exit without a terminal IPC receipt is still a
                # worker protocol failure.
                or "failed"
            )
            state.pending_terminal_status = ""
            state.ended_at = utc_now()

    async def send_decision(self, payload: dict[str, Any]) -> dict[str, Any]:
        decision = MinionApprovalDecision.from_dict(payload)
        state = next(
            (
                item
                for item in self.runs.values()
                if str(item.pending_approval.get("approval_id") or "") == decision.approval_id
            ),
            None,
        )
        if state is None:
            raise KeyError(f"unknown approval target: {decision.approval_id}")
        if not await self.v2_semantic_orchestrator.send_worker_control(
            state.run_id,
            {"type": "decision", "decision": decision.to_dict()},
        ):
            raise RuntimeError("V2 worker is no longer available for approval")
        state.pending_approval = {}
        state.status = "running"
        return {"ok": True, "run": state.summary(), "decision": decision.to_dict()}

    async def send_clarification(self, payload: dict[str, Any]) -> dict[str, Any]:
        clarification_id = str(payload.get("clarification_id") or "")
        run_id = str(payload.get("run_id") or "")
        state = self.runs.get(run_id) if run_id else next(
            (
                item
                for item in self.runs.values()
                if str(item.pending_clarification.get("clarification_id") or "") == clarification_id
            ),
            None,
        )
        if state is None:
            raise KeyError(f"unknown clarification target: {clarification_id or run_id}")
        pending_id = str(state.pending_clarification.get("clarification_id") or "")
        if not clarification_id or clarification_id != pending_id:
            raise ValueError("clarification response does not match the pending question")
        clarification = dict(payload)
        task_revision = await asyncio.to_thread(
            self._append_architect_clarification,
            state,
            clarification,
        )
        if task_revision:
            clarification["task_revision"] = task_revision
        agent_session = dict(
            dict(state.pack.metadata or {}).get("agent_session") or {}
        )
        logical_session_id = str(agent_session.get("session_id") or "").strip()
        if logical_session_id:
            await asyncio.to_thread(
                self.v2_service.repository.begin_role_execution_input,
                logical_session_id,
                input_id=f"clarification:{clarification_id}",
            )
        if not await self.v2_semantic_orchestrator.send_worker_control(
            state.run_id,
            {"type": "clarification", "clarification": clarification},
        ):
            raise RuntimeError("V2 worker is no longer available for clarification")
        state.pending_clarification = {}
        state.status = "running"
        return {"ok": True, "run": state.summary(), "clarification": clarification}

    def _append_architect_clarification(
        self,
        state: MinionRunState,
        clarification: Mapping[str, Any],
    ) -> dict[str, Any]:
        binding = dict(dict(state.pack.metadata or {}).get("minion_v2") or {})
        if (
            str(binding.get("role") or "") != "architect"
            or str(binding.get("aggregate_type") or "") != "architecture_revision"
        ):
            return {}
        questions = [
            dict(item)
            for item in list(state.pending_clarification.get("questions") or [])
            if isinstance(item, Mapping)
        ]
        answers = [
            dict(item)
            for item in list(clarification.get("answers") or [])
            if isinstance(item, Mapping)
        ]
        if len(questions) != 1 or not answers:
            raise ValueError(
                "Architect task-ledger clarification requires one question and one answer"
            )
        question = questions[0]
        question_id = str(question.get("question_id") or question.get("id") or "")
        answer_item = next(
            (
                item
                for item in answers
                if not question_id
                or str(item.get("question_id") or item.get("id") or "") == question_id
            ),
            answers[0],
        )
        answer = str(answer_item.get("answer") or "")
        if not answer.strip():
            raise ValueError("Architect task-ledger clarification answer is blank")
        return self.v2_service.append_architect_clarification(
            {
                "workflow_id": str(binding.get("workflow_id") or ""),
                "architecture_revision_id": str(binding.get("aggregate_id") or ""),
                "worker_id": state.pack.invocation_id,
                "clarification_id": str(clarification.get("clarification_id") or ""),
                "title": str(
                    question.get("title")
                    or state.pending_clarification.get("title")
                    or "Architecture clarification"
                ),
                "question": str(question.get("question") or ""),
                "answer": answer,
                "observed_at": utc_now(),
            }
        )

    def _pending_clarification_runs(self, workflow_id: str) -> list[MinionRunState]:
        matches: list[MinionRunState] = []
        for state in self.runs.values():
            binding = dict(dict(state.pack.metadata or {}).get("minion_v2") or {})
            if (
                state.status not in _TERMINAL_RUN_STATUSES
                and state.pending_clarification
                and str(binding.get("workflow_id") or "") == workflow_id
            ):
                matches.append(state)
        return matches

    def _v2_workflow_status(self, workflow_id: str, *, view: str = "status") -> dict[str, Any]:
        status = self.v2_service.workflow_status(workflow_id, view=view)
        matches = self._pending_clarification_runs(workflow_id)
        if status.get("status") != "ok" or not matches:
            return status
        return {
            **status,
            "active_worker": "",
            "active_worker_role": "",
            "next_legal_action": ["answer_question", "control_workflow:cancel"],
            "waiting_for_user": True,
            "liveness": "human_wait",
            "pending_question_count": len(matches),
            "pending_question": _pending_clarification_status(
                matches[0].pending_clarification
            ),
        }

    async def answer_workflow_question(self, payload: dict[str, Any]) -> dict[str, Any]:
        workflow_id = str(payload.get("workflow_id") or "").strip()
        answer = str(payload.get("answer") or "").strip()
        if not workflow_id or not answer:
            raise ValueError("workflow_id and answer are required")
        matches = self._pending_clarification_runs(workflow_id)
        if len(matches) != 1:
            raise ValueError(
                f"workflow has {len(matches)} pending worker questions; expected exactly one"
            )
        state = matches[0]
        pending = dict(state.pending_clarification)
        questions = [
            dict(item) for item in list(pending.get("questions") or []) if isinstance(item, dict)
        ]
        question_id = str(
            (questions[0] if questions else {}).get("question_id")
            or (questions[0] if questions else {}).get("id")
            or "question-1"
        )
        return await self.send_clarification(
            {
                "clarification_id": str(pending.get("clarification_id") or ""),
                "run_id": state.run_id,
                "minion_id": state.minion_id,
                "answers": [{"question_id": question_id, "answer": answer}],
            }
        )

    async def llm_broker_preflight(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_broker_run(params)
        advice = await (await self._llm_broker_runtime()).apreflight(
            preflight_request_from_payload(dict(params.get("request") or {}))
        )
        return {"ok": True, "advice": preflight_advice_to_payload(advice)}

    async def llm_broker_generate(self, params: dict[str, Any]) -> dict[str, Any]:
        state = self._require_broker_run(params)
        runtime = await self._llm_broker_runtime()
        with scoped_llm_event_sink(self._llm_progress_sink(state)):
            outcome = await runtime.agenerate(llm_request_from_payload(dict(params.get("request") or {})))
        return {"ok": True, "outcome": llm_outcome_to_payload(outcome)}

    async def llm_broker_generate_stream(self, params: dict[str, Any]) -> dict[str, Any]:
        state = self._require_broker_run(params)
        runtime = await self._llm_broker_runtime()
        with scoped_llm_event_sink(self._llm_progress_sink(state)):
            events = await runtime.agenerate_stream(llm_request_from_payload(dict(params.get("request") or {})))
        return {"ok": True, "events": [stream_event_to_payload(item) for item in list(events or [])]}

    async def llm_broker_resolve_max_output_tokens(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_broker_run(params)
        runtime = await self._llm_broker_runtime()
        value = await asyncio.to_thread(
            runtime.resolve_max_output_tokens,
            preferred_endpoint_id=str(params.get("preferred_endpoint_id") or "") or None,
            preferred_endpoint_source=str(params.get("preferred_endpoint_source") or "") or None,
        )
        return {"ok": True, "max_output_tokens": value}

    async def llm_broker_resolve_endpoint_facts(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_broker_run(params)
        runtime = await self._llm_broker_runtime()
        return await asyncio.to_thread(
            runtime.resolve_endpoint_facts,
            preferred_endpoint_id=str(params.get("preferred_endpoint_id") or "") or None,
            preferred_endpoint_source=str(params.get("preferred_endpoint_source") or "") or None,
        )

    def _require_broker_run(self, params: Mapping[str, Any]) -> MinionRunState:
        run_id = str(params.get("run_id") or "")
        state = self.runs.get(run_id)
        if state is None:
            raise KeyError(f"unknown minion run: {run_id}")
        if state.status in _TERMINAL_RUN_STATUSES:
            raise RuntimeError(f"minion run is terminal: {run_id}")
        return state

    async def _llm_broker_runtime(self) -> Any:
        return (await self._host_broker_runtime_bundle()).llm_runtime

    async def _host_broker_runtime_bundle(self) -> Any:
        if self._host_broker_bundle is None:
            from pal.minion.runner import build_slim_minion_runtime

            self._host_broker_bundle = await asyncio.to_thread(
                build_slim_minion_runtime,
                self.runtime_root,
            )
        return self._host_broker_bundle

    async def web_broker_search(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._web_broker_call(
            params,
            canonical_path="op_web_search",
            alias="search_web",
        )

    async def web_broker_read(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._web_broker_call(
            params,
            canonical_path="op_web_read",
            alias="read_web",
        )

    async def _web_broker_call(
        self,
        params: dict[str, Any],
        *,
        canonical_path: str,
        alias: str,
    ) -> dict[str, Any]:
        state = self._require_broker_run(params)
        allowed = {
            str(item or "").strip()
            for item in list(state.pack.allowed_capabilities or [])
            if str(item or "").strip()
        }
        if canonical_path not in allowed:
            raise PermissionError(
                f"role assignment does not allow {canonical_path}"
            )
        bundle = await self._host_broker_runtime_bundle()
        runtime = bundle.execution_runtime
        record = runtime.registry_generation.direct_aliases.get(alias)
        if record is None or record.canonical_path != canonical_path:
            raise RuntimeError(f"host web broker capability is unavailable: {alias}")
        if record.input_model is None:
            raise RuntimeError(f"host web broker has no input contract: {alias}")
        validated = record.input_model.model_validate(
            dict(params.get("args") or {}),
            strict=True,
        )
        result = await runtime.call_registered_async(
            CapabilityCall(
                name=canonical_path,
                args=validated.model_dump(mode="python", exclude_none=True),
                meta={"broker_run_id": state.run_id},
            )
        )
        return {"ok": True, "result": web_result_to_payload(result)}

    def _llm_progress_sink(self, state: MinionRunState):
        loop = asyncio.get_running_loop()

        def sink(event: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(
                self.events.queue_event,
                {
                    "event_kind": "progress",
                    "run_id": state.run_id,
                    "minion_id": state.minion_id,
                    "invocation_id": state.pack.invocation_id,
                    "payload": dict(event),
                    "created_at": utc_now(),
                },
            )

        return sink

    def health(self) -> dict[str, Any]:
        active = [item for item in self.runs.values() if item.status not in _TERMINAL_RUN_STATUSES]
        return {
            "ok": True,
            "health_source": "minion_v2_manager",
            "lifecycle_protocol": "plugin_raii.v1",
            "manager_pid": os.getpid(),
            "started_at": self.started_at,
            "run_count": len(self.runs),
            "active_count": len(active),
            "active_runs": [item.summary() for item in active],
            "shutdown_requested": self._drain_requested.is_set() or self._shutdown_event.is_set(),
            "draining": self._drain_requested.is_set() and not self._shutdown_event.is_set(),
            "shutdown_reason": self._shutdown_reason,
            "max_parallel_llm_nodes": self.max_parallel_modules,
            "pending_event_count": len(self.event_queue),
            "event_subscriber_count": len(self.event_subscribers),
            "minion_db_path": str(self.v2_service.repository.db_path),
            "log_sink": current_service_log_sink_description(),
            "catalog_generation": str(self.catalog.snapshot()["generation"]),
            **dict(self.endpoint_info),
        }

    def reload_runtime_config(self) -> dict[str, Any]:
        config = effective_minion_runtime_config(self.runtime_root)
        self.max_parallel_modules = max(
            1,
            int(config.get("max_parallel_llm_nodes", config.get("max_parallel_modules", self.max_parallel_modules)) or self.max_parallel_modules),
        )
        self.v2_outbox.max_parallel_nodes = self.max_parallel_modules
        self.v2_semantic_orchestrator.max_parallel_workers = self.max_parallel_modules
        self._v2_wake_event.set()
        return {"ok": True, "status": "ok", "config": config, "max_parallel_llm_nodes": self.max_parallel_modules}

    def request_shutdown(
        self,
        *,
        reason: str,
        timeout_seconds: float,
        graceful: bool = True,
    ) -> dict[str, Any]:
        self._shutdown_reason = reason
        self._shutdown_started_at = self._shutdown_started_at or utc_now()
        self.graceful_shutdown_timeout_seconds = max(0.0, timeout_seconds)
        self.v2_semantic_orchestrator.request_stop()
        if graceful:
            self._drain_requested.set()
            if self._drain_task is None or self._drain_task.done():
                self._drain_task = asyncio.create_task(
                    self._drain_for_shutdown(),
                    name="minion-manager-graceful-drain",
                )
        else:
            self._drain_requested.set()
            self._shutdown_event.set()
        return {
            "ok": True,
            "status": "draining" if graceful else "shutdown_requested",
            "reason": reason,
            "timeout_seconds": self.graceful_shutdown_timeout_seconds,
            "started_at": self._shutdown_started_at,
        }

    async def _drain_for_shutdown(self) -> None:
        deadline = asyncio.get_running_loop().time() + self.graceful_shutdown_timeout_seconds
        notified_runs: set[str] = set()
        while True:
            for run_id, state in tuple(self.runs.items()):
                process = state.process
                if (
                    run_id in notified_runs
                    or state.status in _TERMINAL_RUN_STATUSES
                    or process is None
                    or process.returncode is not None
                ):
                    continue
                sent = await self.v2_semantic_orchestrator.send_worker_control(
                    run_id,
                    {
                        "type": "restart_requested",
                        "payload": {
                            "reason": self._shutdown_reason or "manager_restart_requested",
                            "summary": (
                                "Manager restart requested; suspend after the current durable "
                                "LLM/tool safe point."
                            ),
                        },
                    },
                )
                if sent:
                    notified_runs.add(run_id)
            active_runs = [
                state
                for state in self.runs.values()
                if state.process is not None
                and state.process.returncode is None
                and state.status not in _TERMINAL_RUN_STATUSES
            ]
            background_count = (
                self.v2_outbox.active_background_count
                + self.v2_semantic_orchestrator.active_background_count
            )
            if not active_runs and background_count == 0:
                break
            if asyncio.get_running_loop().time() >= deadline:
                self.logger.warning(
                    "minion manager graceful drain timed out active_runs=%s background_effects=%s",
                    len(active_runs),
                    background_count,
                )
                break
            await asyncio.sleep(0.05)
        self._shutdown_event.set()

    async def close_all(self) -> None:
        # The semantic orchestrator owns worker processes.  Cancelling its
        # logical tasks enters each process owner's RAII close path, which
        # reaps the complete process group before unregistering the run.
        await self.v2_semantic_orchestrator.stop_background_workers(
            timeout_seconds=self.graceful_shutdown_timeout_seconds,
        )
        active = [
            state.run_id
            for state in self.runs.values()
            if state.status not in _TERMINAL_RUN_STATUSES
            or (
                state.process is not None
                and state.process.returncode is None
            )
        ]
        if active:
            raise RuntimeError(
                "manager shutdown retained active worker accounting: "
                + ", ".join(sorted(active))
            )
        if self._host_broker_bundle is not None:
            close = getattr(self._host_broker_bundle, "close", None)
            if callable(close):
                await close()
            self._host_broker_bundle = None

    def _install_signal_handlers(self):
        loop = asyncio.get_running_loop()
        installed: list[signal.Signals] = []

        def request_stop() -> None:
            self.request_shutdown(
                reason="signal",
                timeout_seconds=self.graceful_shutdown_timeout_seconds,
                graceful=True,
            )

        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, request_stop)
                installed.append(sig)

        def remove() -> None:
            for sig in installed:
                with contextlib.suppress(NotImplementedError):
                    loop.remove_signal_handler(sig)

        return remove
