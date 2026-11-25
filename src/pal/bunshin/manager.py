from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from pal.execution import CapabilityCall
from pal.foundation import utc_now
from pal.foundation.lifecycle_gate import WriterPreferredRWGate
from pal.foundation.workers import join_owned_task
from pal.foundation.service_logging import current_service_log_sink_description
from pal.foundation.sidecar import dispatch_sidecar_request, pack_sidecar_message, read_sidecar_message
from pal.foundation.fd_lease import fd_lease_snapshot
from pydantic import ValidationError
from pal.bunshin.verifier_tool_diagnostics import VerifierToolDiagnostic, is_verifier_pack
from pal.bunshin.producer_tool_diagnostics import (
    MAX_PRODUCER_TOOL_DIAGNOSTICS, ProducerToolDiagnostic, is_producer_pack,
)
from pal.bunshin.catalog import BunshinCatalogService
from pal.bunshin.llm_admission import authorize_role_request
from pal.bunshin.llm_proxy import LLMProxy, serve_llm_stream
from pal.bunshin.run_ledger import BunshinRunState, ProcessStatusView, RunLedger
from pal.bunshin.config import effective_bunshin_runtime_config
from pal.bunshin.event_delivery import BunshinEventDelivery
from pal.bunshin.ipc import (
    cleanup_manager_endpoint,
    cleanup_role_gateway_endpoint,
    start_manager_server,
    start_role_gateway_server,
)
from pal.bunshin.harnesses import builtin_harness_generation
from pal.bunshin.web_broker import web_result_to_payload
from pal.bunshin.contracts import AggregateType
from pal.bunshin.coroutine_runtime import CoroutineRunSemaphore
from pal.bunshin.dag_scheduling import NodeScheduler
from pal.bunshin.effect_dispatcher import WorkflowEffectDispatcher
from pal.bunshin.orchestration import WorkflowEffects
from pal.bunshin.recovery import BunshinRecovery
from pal.bunshin.workflow_status import pending_clarification_status
from pal.bunshin.service import BunshinWorkflowService
from pal.bunshin.semantic_orchestration import SemanticOrchestrator
from pal.bunshin.interaction_gateway import record_clarification_answer
from pal.bunshin.role_gateway import RoleAssignmentGateway
from pal.bunshin.submission_errors import role_gateway_error_kind
from pal.shared import BunshinApprovalDecision, BunshinInvocationPack, RuntimeStatus


_DEFAULT_MAX_PARALLEL_NODES = 5
_DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 600.0


@dataclass
class BunshinManager:
    runtime_root: Path
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("pal.bunshin.manager"))
    max_parallel_modules: int | None = None
    runtime_db_path: Path | None = None
    graceful_shutdown_timeout_seconds: float = _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    server: asyncio.base_events.Server | None = None
    role_server: asyncio.base_events.Server | None = None
    endpoint_info: dict[str, Any] = field(default_factory=dict)
    role_endpoint_info: dict[str, Any] = field(default_factory=dict)
    runs: dict[str, BunshinRunState] = field(default_factory=dict)
    started_at: str = field(default_factory=utc_now)
    catalog: BunshinCatalogService = field(init=False)
    catalog_bootstrap: dict[str, Any] = field(init=False, default_factory=dict)
    events: BunshinEventDelivery = field(init=False)
    workflow_service: BunshinWorkflowService = field(init=False)
    capacity_pool: CoroutineRunSemaphore = field(init=False)
    workflow_outbox: WorkflowEffectDispatcher = field(init=False)
    semantic_orchestrator: SemanticOrchestrator = field(init=False)
    role_gateway: RoleAssignmentGateway = field(init=False)
    _host_tool_bundle: Any | None = field(default=None, init=False, repr=False)
    _host_tool_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _host_tool_gate: WriterPreferredRWGate = field(default_factory=WriterPreferredRWGate, init=False, repr=False)
    _host_tools_closing: bool = field(default=False, init=False, repr=False)
    _close_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    run_ledger: RunLedger = field(init=False, repr=False)
    llm_proxy: LLMProxy = field(init=False, repr=False)
    _shutdown_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _drain_requested: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _wake_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _outbox_task: asyncio.Task[Any] | None = field(default=None, init=False, repr=False)
    _drain_task: asyncio.Task[Any] | None = field(default=None, init=False, repr=False)
    _shutdown_reason: str = field(default="", init=False)
    _shutdown_started_at: str = field(default="", init=False)
    _skill_database: Any | None = field(default=None, init=False, repr=False)
    _skill_inject_tool: Any | None = field(default=None, init=False, repr=False)
    prompt_log_enabled: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.runtime_root = Path(self.runtime_root)
        if self.runtime_db_path is not None:
            self.runtime_db_path = Path(self.runtime_db_path)
        self.run_ledger = RunLedger(self.runs)
        self.llm_proxy = LLMProxy(self.runtime_root)
        self.catalog = BunshinCatalogService(Path(self.runtime_root))
        self.catalog_bootstrap = self.catalog.bootstrap()
        config = effective_bunshin_runtime_config(Path(self.runtime_root))
        configured = config.get("max_parallel_llm_nodes", config.get("max_parallel_modules", _DEFAULT_MAX_PARALLEL_NODES))
        self.max_parallel_modules = max(1, int(self.max_parallel_modules or configured or _DEFAULT_MAX_PARALLEL_NODES))
        self.workflow_service = BunshinWorkflowService(Path(self.runtime_root))
        try:
            self.workflow_service.repository.role_maintenance.reconcile_terminal_role_runtime()
            self.workflow_service.repository.role_maintenance.reconcile_role_session_checkpoints()
        except Exception:
            # Reconciliation is a leak repair, never a reason to make the
            # Manager unavailable. Durable state remains authoritative and a
            # later startup can retry the idempotent cleanup.
            self.logger.exception("bunshin terminal runtime reconciliation failed")
        self.events = BunshinEventDelivery(
            load_backlog=self._pending_task_delivery_events,
            may_send=self.workflow_service.repository.deliveries.may_send_interaction_delivery,
        )
        self.role_gateway = RoleAssignmentGateway(self.workflow_service)
        node_scheduler = NodeScheduler(self.workflow_service.repository)
        self.capacity_pool = CoroutineRunSemaphore(
            self.max_parallel_modules, node_scheduler.retain_node_slot, node_scheduler.predecessor_settled,
        )
        self.semantic_orchestrator = SemanticOrchestrator(
            self.workflow_service,
            max_parallel_workers=self.max_parallel_modules,
            capacity_pool=self.capacity_pool,
            runtime_db_path=self.runtime_db_path,
            publish_human_review=self._publish_human_review,
            publish_worker_event=self._publish_worker_event,
            publish_workflow_event=self._publish_workflow_event,
            register_broker_run=self._register_broker_run,
            unregister_broker_run=self._unregister_broker_run,
            inject_skill=self._inject_skill_for_role,
        )
        handlers = WorkflowEffects(
            self.workflow_service,
            semantic_effects=self.semantic_orchestrator,
            publish_workflow_event=self._publish_workflow_event,
        )

        self.workflow_outbox = WorkflowEffectDispatcher(handlers)

    @property
    def event_queue(self) -> list[dict[str, Any]]:
        return self.events.queue

    @property
    def event_subscribers(self) -> list[asyncio.StreamWriter]:
        return self.events.subscribers

    async def run(self) -> None:
        from pal.bunshin.memory_binding import prepare_workflow_memory_references, reconcile_workflow_memory_references
        from pal.foundation.workers import run_worker
        await run_worker(lambda: prepare_workflow_memory_references(self.runtime_root))
        recovery = await asyncio.to_thread(BunshinRecovery(self.workflow_service).recover)
        await run_worker(lambda: reconcile_workflow_memory_references(self.runtime_root))
        workspaces = await asyncio.to_thread(self.workflow_service.repository.leases.inherited_role_process_groups)
        await self.capacity_pool.recover_occupancy(workspaces)
        self.server, self.endpoint_info = await start_manager_server(self.runtime_root, self._handle_client)
        self.role_server, self.role_endpoint_info = await start_role_gateway_server(
            self.runtime_root,
            self._handle_role_client,
        )
        self.logger.info(
            "bunshin manager listening: %s role_gateway=%s recovery=%s",
            self.endpoint_info,
            self.role_endpoint_info,
            recovery,
        )
        remove_signals = self._install_signal_handlers()
        async with self.server, self.role_server:
            serve_task = asyncio.create_task(self.server.serve_forever(), name="bunshin-manager-serve")
            role_serve_task = asyncio.create_task(
                self.role_server.serve_forever(),
                name="bunshin-role-gateway-serve",
            )
            recovered_assignments = await self.semantic_orchestrator.recover_background_assignments()
            if recovered_assignments:
                self.logger.info(
                    "recovered %s durable role assignment(s)",
                    recovered_assignments,
                )
            self._outbox_task = asyncio.create_task(self._run_outbox(), name="bunshin-outbox")
            try:
                await self._shutdown_event.wait()
            finally:
                remove_signals()
                self.semantic_orchestrator.request_stop()
                if self._outbox_task is not None:
                    self._outbox_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._outbox_task
                await self.workflow_outbox.stop_background()
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
            from pal.skill.models import SkillModel

            self._skill_database = PalV2Database(
                db_path=self.runtime_db_path or Path(self.runtime_root) / "pal.sqlite3",
                read_only=True,
            )
            self._skill_database.initialize((SkillModel,))
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
        context_messages = tuple(result.context_messages)
        if len(context_messages) != 1:
            raise ValueError(f"{skill_id}: skill injection returned no user context")
        user_context = str(context_messages[0].content or "").strip()
        if not user_context:
            raise ValueError(f"{skill_id}: skill injection returned empty user context")
        return {
            "skill_id": str(dict(result.structured or {}).get("skill_id") or skill_id),
            "user_context": user_context,
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
                if bool(request.get("stream")):
                    await self._serve_llm_stream_request(
                        request,
                        reader,
                        writer,
                        worker=False,
                    )
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
                if bool(request.get("stream")):
                    await self._serve_llm_stream_request(
                        request,
                        reader,
                        writer,
                        worker=True,
                    )
                    return
                writer.write(
                    pack_sidecar_message(
                        await dispatch_sidecar_request(
                            request,
                            self._call_worker_method,
                            error_kind=role_gateway_error_kind,
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
            "llm_usage_receipt": self.llm_usage_receipt,
            "web_search": self.web_broker_search,
            "web_read": self.web_broker_read,
        }
        if method in broker_handlers:
            payload = await self._authorize_worker_broker_params(params)
            return await broker_handlers[method](payload)
        result = await asyncio.to_thread(self.role_gateway.call, method, params)
        if method == "interaction_park" and "delivery" in result:
            self.events.queue_event(_delivery_event_from_row(result.pop("delivery")))
        return result

    async def _call_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "llm_usage_receipt": self.llm_usage_receipt,
            "refresh_llm_endpoints": self.refresh_llm_endpoints,
            "send_decision": self.send_decision,
            "send_clarification": self.send_clarification,
            "answer_workflow_question": self.answer_workflow_question,
            "set_prompt_log_enabled": self.set_prompt_log_enabled,
        }
        if method in handlers:
            payload = dict(params.get("decision") or {}) if method == "send_decision" else dict(params.get("clarification") or {}) if method == "send_clarification" else params
            return await handlers[method](payload)
        if method == "health":
            return self.health()
        if method == "reload_runtime_config":
            return self.reload_runtime_config()
        if method == "replace_harness_registry":
            # Older core processes synchronize their registry during attachment.
            return {"ok": True, "mode": "builtin_only", "generation": builtin_harness_generation().to_dict()}
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
        workflow_method = method.removeprefix("v2_")  # Older client processes can finish pending deliveries.
        if workflow_method == "wake":
            self._wake_event.set()
            return {"ok": True, "status": "woken"}
        if workflow_method == "task_status":
            return self._task_status(
                str(params.get("task_id") or ""),
                workflow_id=str(params.get("workflow_id") or ""),
                view=str(params.get("view") or "status"),
            )
        if workflow_method == "start_workflow":
            from pal.foundation.workers import run_worker
            result = await run_worker(lambda: self.workflow_service.start_workflow(params))
            self._wake_event.set()
            return result
        if workflow_method == "rebind_task_delivery":
            result = self.workflow_service.repository.delivery_bindings.rebind_task_delivery(
                task_id=str(params.get("task_id") or ""),
                binding=dict(params.get("binding") or {}),
            )
            if bool(result.get("changed")):
                self._replay_waiting_task_deliveries(
                    str(params.get("task_id") or ""),
                    binding_version=int(result.get("binding_version") or 0),
                )
            self._wake_event.set()
            return result
        if workflow_method == "ack_task_delivery":
            return {
                "acknowledged": self.workflow_service.repository.deliveries.acknowledge_task_delivery(
                    str(params.get("delivery_id") or "")
                )
            }
        if workflow_method == "validate_memory_proposal_source":
            return {"valid": self._validate_memory_proposal_source(dict(params.get("source") or {}))}
        if workflow_method == "list_task_delivery_parts":
            return {
                "parts": list(
                    self.workflow_service.repository.deliveries.delivered_task_delivery_parts(
                        str(params.get("delivery_id") or "")
                    )
                )
            }
        if workflow_method == "ack_task_delivery_part":
            return {
                "acknowledged": self.workflow_service.repository.deliveries.acknowledge_task_delivery_part(
                    str(params.get("delivery_id") or ""),
                    str(params.get("part_key") or ""),
                )
            }
        if workflow_method == "defer_task_delivery":
            return {
                "deferred": self.workflow_service.repository.deliveries.defer_task_delivery(
                    str(params.get("delivery_id") or ""),
                    error=str(params.get("error") or ""),
                )
            }
        if method == "list_runs":
            return {"items": self.run_ledger.summaries()}
        if method == "read_run":
            run_id = str(params.get("run_id") or "")
            if run_id not in self.runs:
                raise KeyError(f"unknown bunshin run: {run_id}")
            return self.runs[run_id].detail()
        if method == "shutdown":
            return self.request_shutdown(
                reason=str(params.get("reason") or "manager_shutdown"),
                timeout_seconds=float(params.get("timeout_seconds") or self.graceful_shutdown_timeout_seconds),
                graceful=bool(params.get("graceful", True)),
            )
        raise ValueError(f"unknown Bunshin manager method: {method}")

    async def _authorize_worker_broker_params(
        self,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload = dict(params or {})
        token = str(payload.pop("access_token", ""))
        authenticated = await asyncio.to_thread(
            self.role_gateway.authorize,
            token,
        )
        run_id = str(payload.get("run_id") or "")
        run = self.runs.get(run_id)
        assignment = dict(authenticated.get("assignment") or {})
        if run is None or run.bunshin_id != str(assignment.get("session_id") or ""):
            raise PermissionError(
                "role assignment token does not own the requested broker run"
            )
        if run.attempt_id and run.attempt_id != authenticated.get("attempt_id"):
            raise PermissionError("role assignment token does not own the current broker attempt")
        payload["_broker_attempt_id"] = run.attempt_id
        return payload

    async def _serve_llm_stream_request(
        self,
        request: dict[str, Any],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        worker: bool,
    ) -> None:
        async def open_stream() -> AsyncGenerator[dict[str, Any], None]:
            method = request.get("method")
            if method != "llm_transport_stream":
                raise ValueError(f"sidecar method is not streamable: {method}")
            params = dict(request.get("params") or {})
            if worker:
                params = await self._authorize_worker_broker_params(params)
            return self.llm_transport_stream_frames(params)

        await serve_llm_stream(
            reader, writer, open_stream, request_id=str(request.get("id") or ""),
            error_kind="role_gateway" if worker else "manager", logger=self.logger,
        )

    async def _run_outbox(self) -> None:
        while not self._shutdown_event.is_set():
            if self._drain_requested.is_set():
                await asyncio.sleep(0.05)
                continue
            try:
                from pal.bunshin.memory_binding import reconcile_workflow_memory_references
                from pal.foundation.workers import run_worker
                await run_worker(lambda: reconcile_workflow_memory_references(self.runtime_root))
                if self.capacity_pool.capacity != self.max_parallel_modules:
                    await self.capacity_pool.resize(self.max_parallel_modules)
                await self.capacity_pool.recheck_occupancy()
                await self.capacity_pool.reconcile_reservations()
                await self.semantic_orchestrator.recover_background_assignments()
                if self.workflow_outbox.start_available(max_concurrency=self.max_parallel_modules + 8):
                    await asyncio.sleep(0)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("Bunshin outbox tick failed")
            self._wake_event.clear()
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=0.25)
            except TimeoutError:
                pass

    async def _publish_human_review(self, payload: Mapping[str, Any]) -> None:
        standalone = bool(payload.get("standalone_review_id"))
        event = {
            "event_kind": "standalone_review_completed" if standalone else "architecture_review_pending",
            "bunshin_id": "",
            "run_id": "",
            "workflow_id": str(payload.get("workflow_id") or ""),
            "bunshin_profile": "bunshin.reviewer",
            "role_mode": "standalone" if standalone else "architecture",
            "payload": {**dict(payload), "bunshin": True, **({"status": "completed"} if standalone else {})},
            "created_at": utc_now(),
        }
        self._queue_task_delivery_event(
            event,
            dedup_key=(
                f"human-review:{event['workflow_id']}:"
                f"{payload.get('architecture_revision_id') or payload.get('standalone_review_id') or ''}"
            ),
        )

    def _publish_workflow_event(self, payload: Mapping[str, Any]) -> None:
        workflow_id = str(payload.get("workflow_id") or "").strip()
        event_kind = str(payload.get("event_kind") or "workflow_terminal").strip()
        if event_kind == "architecture_review_resolved":
            revision_id = str(
                payload.get("architecture_revision_id") or ""
            ).strip()
            event = {
                "event_kind": event_kind,
                "bunshin_id": "",
                "run_id": "",
                "workflow_id": workflow_id,
                "bunshin_profile": "bunshin.reviewer",
                "role_mode": "architecture",
                "payload": {**dict(payload), "bunshin": True},
                "created_at": str(payload.get("resolved_at") or utc_now()),
            }
            self._queue_task_delivery_event(
                event,
                dedup_key=(
                    f"architecture-review-resolved:{workflow_id}:{revision_id}"
                ),
            )
            return
        status = str(payload.get("status") or "completed").strip().lower()
        event = {
            "event_kind": "workflow_terminal",
            "bunshin_id": "",
            "run_id": "",
            "workflow_id": workflow_id,
            "bunshin_profile": "bunshin.workflow",
            "role_mode": "workflow",
            "payload": {**dict(payload), "bunshin": True},
            "created_at": str(payload.get("terminal_at") or utc_now()),
        }
        self._queue_task_delivery_event(
            event,
            dedup_key=f"workflow-terminal:{workflow_id}:{status}",
        )

    async def _publish_worker_event(self, event: Mapping[str, Any]) -> None:
        item = dict(event)
        delivery_attempt_id = str(item.pop("_attempt_id", "") or "")
        owner_run_id = item.pop("_owner_run_id", None)
        run_id = str(item.get("run_id") or "")
        state = self.runs.get(run_id)
        if item.get("event_kind") == "git_gateway_diagnostic":
            # This record is emitted only within the authenticated Manager
            # gateway. Worker-supplied lookalikes have no recording authority.
            return
        if item.get("event_kind") == "producer_tool_diagnostic":
            # Only the Manager's role binding supplies tool authority and
            # identity. Keep this operational record out of chat and role state.
            state = self.runs.get(owner_run_id) if isinstance(owner_run_id, str) else None
            if (state is None or not delivery_attempt_id
                    or (state.attempt_id and state.attempt_id != delivery_attempt_id)
                    or state.producer_diagnostic_count >= MAX_PRODUCER_TOOL_DIAGNOSTICS):
                return
            try:
                diagnostic = ProducerToolDiagnostic.model_validate(item.get("payload"))
                if not is_producer_pack(state.pack, diagnostic.tool_alias):
                    return
                diagnostic = ProducerToolDiagnostic.model_validate({
                    **diagnostic.model_dump(), "attempt_id": delivery_attempt_id,
                })
            except ValidationError:
                return
            state.producer_diagnostic_count += 1
            try:
                self.workflow_service.repository.role_events.record_worker_event({
                    "event_kind": "producer_tool_diagnostic",
                    "invocation_id": state.pack.invocation_id,
                    "payload": diagnostic.model_dump(),
                })
            except Exception:
                pass
            return
        if state is not None and delivery_attempt_id and state.attempt_id and state.attempt_id != delivery_attempt_id:
            return
        if item.get("event_kind") == "verifier_tool_diagnostic":
            # This operational record has its own strict contract even with
            # prompt logging enabled. Do not route it to chat or role state.
            if state is None or not delivery_attempt_id or not is_verifier_pack(state.pack):
                return
            try:
                diagnostic = VerifierToolDiagnostic.model_validate(item.get("payload"))
                diagnostic = VerifierToolDiagnostic.model_validate({
                    **diagnostic.model_dump(), "attempt_id": delivery_attempt_id,
                })
            except ValidationError:
                return
            try:
                self.workflow_service.repository.role_events.record_worker_event({
                    "event_kind": "verifier_tool_diagnostic",
                    "invocation_id": state.pack.invocation_id,
                    "payload": diagnostic.model_dump(),
                })
            except Exception:
                # A diagnostic-only storage failure cannot abort worker IPC.
                pass
            return
        if state is not None:
            payload = dict(item.get("payload") or {})
            payload.pop("route", None)
            payload.pop("control_route", None)
            binding = dict(dict(state.pack.metadata or {}).get("bunshin") or {})
            # Assignment identity is Manager-owned; worker output cannot change it.
            payload.pop("source_dependencies", None)
            if payload.get("memory_candidates"):
                dependencies = {}
                if str(binding.get("aggregate_type") or "") == AggregateType.DAG_NODE_RUN.value:
                    dependencies = {"node_run_id": str(binding.get("aggregate_id") or "")}
                    node = self.workflow_service.repository.snapshots.read_snapshot(
                        AggregateType.DAG_NODE_RUN, str(binding.get("aggregate_id") or ""))
                    if node is not None:
                        dependencies = {"node_run_id": node.aggregate_id,
                            "epoch_id": str(node.payload.get("epoch_id") or "")}
                payload["source_dependencies"] = dependencies
            workflow_id = str(binding.get("workflow_id") or "")
            if workflow_id and not item.get("workflow_id"):
                item["workflow_id"] = workflow_id
            item["payload"] = payload
            if not state.record_event(item, payload):
                return
        kind = str(item.get("event_kind") or "")
        event_payload = dict(item.get("payload") or {})
        debug_enabled = (
            bool(dict(state.pack.metadata or {}).get("prompt_log_enabled"))
            if state is not None
            else bool(self.prompt_log_enabled)
        )
        if debug_enabled:
            self.workflow_service.repository.role_events.record_worker_event(item)
        elif (
            kind == "progress"
            and str(event_payload.get("phase") or "")
            == "llm_round_completed"
        ):
            # Efficiency accounting is ordinary operational telemetry, not
            # prompt logging. Persist only the non-content round fields when
            # debug history is disabled so batching metrics remain available
            # without retaining prompts, previews, tool arguments, or routes.
            self.workflow_service.repository.role_events.record_worker_event(
                {
                    **item,
                    "payload": {
                        "phase": "llm_round_completed",
                        "round": max(0, int(event_payload.get("round") or 0)),
                        "tool_call_count": max(
                            0,
                            int(event_payload.get("tool_call_count") or 0),
                        ),
                    },
                }
            )
        if kind == "terminal" and event_payload.get("status") == "suspended" and event_payload.get("interaction_id"):
            return
        if kind in {"approval_requested", "clarification_requested", "terminal"}:
            self._queue_task_delivery_event(
                item,
                dedup_key="|".join(
                    (
                        kind,
                        str(item.get("workflow_id") or ""),
                        str(item.get("invocation_id") or item.get("run_id") or ""),
                        delivery_attempt_id,
                        str(event_payload.get("approval_id") or item.get("approval_id") or ""),
                        str(event_payload.get("clarification_id") or item.get("clarification_id") or ""),
                        str(event_payload.get("status") or item.get("status") or ""),
                    )
                ),
            )
        else:
            self.events.queue_event(item)

    def _validate_memory_proposal_source(self, source):
        dependency = source.get("source_dependencies")
        if not isinstance(dependency, dict):
            return False
        repository = self.workflow_service.repository
        workflow = repository.snapshots.read_snapshot(AggregateType.WORKFLOW, str(source.get("workflow_id") or ""))
        if workflow is None or str(workflow.payload.get("task_id") or "") != str(source.get("task_id") or ""):
            return False
        epoch_id = str(dependency.get("epoch_id") or "")
        if epoch_id and str(workflow.payload.get("execution_epoch_id") or "") != epoch_id:
            return False
        node_id = str(dependency.get("node_run_id") or "")
        if node_id:
            node = repository.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
            if node is None or node.workflow_id != workflow.aggregate_id or node.state in {"STALE", "SUPERSEDED", "CANCELLED"}:
                return False
            if epoch_id and str(node.payload.get("epoch_id") or "") != epoch_id:
                return False
            if dependency.get("requires_accepted") and node.state != "ACCEPTED":
                return False
        return True

    def _queue_task_delivery_event(
        self,
        event: Mapping[str, Any],
        *,
        dedup_key: str,
    ) -> None:
        item = dict(event)
        workflow_id = str(item.get("workflow_id") or "")
        workflow = self.workflow_service.repository.snapshots.read_snapshot(
            AggregateType.WORKFLOW,
            workflow_id,
        )
        task_id = str((workflow.payload if workflow is not None else {}).get("task_id") or "")
        if not task_id:
            self.logger.error(
                "cannot deliver Bunshin event without Task binding: workflow=%s kind=%s",
                workflow_id,
                item.get("event_kind"),
            )
            return
        item["task_id"] = task_id
        row = self.workflow_service.repository.deliveries.enqueue_task_delivery(
            task_id=task_id,
            workflow_id=workflow_id,
            event_kind=str(item.get("event_kind") or ""),
            payload=item,
            dedup_key=str(dedup_key),
        )
        self.events.queue_event(_delivery_event_from_row(row))

    def _pending_task_delivery_events(self) -> list[dict[str, Any]]:
        return [
            _delivery_event_from_row(row)
            for row in self.workflow_service.repository.deliveries.list_pending_task_deliveries(limit=200)
        ]

    def _replay_waiting_task_deliveries(
        self,
        task_id: str,
        *,
        binding_version: int,
    ) -> None:
        candidates: set[tuple[str, str]] = {
            (workflow_id, "architecture_review_pending")
            for workflow_id in self.workflow_service.repository.delivery_bindings.pending_human_review_workflows(
                task_id
            )
        }
        for state in self.runs.values():
            if not state.pending_clarification and not state.pending_approval:
                continue
            binding = dict(dict(state.pack.metadata or {}).get("bunshin") or {})
            workflow_id = str(binding.get("workflow_id") or "")
            workflow = self.workflow_service.repository.snapshots.read_snapshot(
                AggregateType.WORKFLOW,
                workflow_id,
            )
            if str((workflow.payload if workflow is not None else {}).get("task_id") or "") != task_id:
                continue
            if state.pending_clarification:
                candidates.add((workflow_id, "clarification_requested"))
            if state.pending_approval:
                candidates.add((workflow_id, "approval_requested"))
        deliveries = self.workflow_service.repository.deliveries
        sources = list(deliveries.pending_interaction_deliveries(task_id))
        for workflow_id, event_kind in sorted(candidates):
            source = deliveries.latest_task_delivery(
                task_id=task_id, workflow_id=workflow_id, event_kind=event_kind,
            )
            if source:
                sources.append(source)
        for source in sources:
            if source["status"] == "pending":
                continue
            replay = self.workflow_service.repository.deliveries.replay_task_delivery(
                delivery_id=str(source["delivery_id"]),
                dedup_key=(
                    f"rebind-replay:{task_id}:{binding_version}:"
                    f"{source['delivery_id']}"
                ),
            )
            self.events.queue_event(_delivery_event_from_row(replay))

    def _register_broker_run(
        self,
        run_id: str,
        bunshin_id: str,
        pack: BunshinInvocationPack,
        process: ProcessStatusView,
    ) -> None:
        self.run_ledger.register(run_id, bunshin_id, pack, process)

    def _unregister_broker_run(
        self,
        run_id: str,
        process_group_reaped: bool,
    ) -> None:
        # ``process_group_reaped`` is a legacy event/state key. It now means
        # that the in-memory process owner completed direct-child cleanup; no
        # numeric process-group identity is persisted or recovered.
        if not process_group_reaped:
            raise RuntimeError(
                "cannot unregister worker before its process owner completes cleanup"
            )
        self.run_ledger.reap(run_id)

    async def send_decision(self, payload: dict[str, Any]) -> dict[str, Any]:
        decision = BunshinApprovalDecision.from_dict(payload)
        waits = self.workflow_service.repository.interaction_waits
        wait = waits.get(decision.approval_id)
        if wait is not None:
            if wait["kind"] != "approval" or (decision.run_id and decision.run_id != wait["request"]["run_id"]):
                raise ValueError("approval target does not match the suspended role")
            waits.answer(decision.approval_id, {"decision": {"approval_id": decision.approval_id, "decision": decision.decision}})
            self._wake_event.set()
            return {"ok": True, "decision": decision.to_dict()}
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
        if not await self.semantic_orchestrator.send_worker_control(
            state.run_id,
            {"type": "decision", "decision": decision.to_dict()},
        ):
            raise RuntimeError("Bunshin worker is no longer available for approval")
        state.acknowledge("approval", decision.approval_id)
        return {"ok": True, "run": state.summary(), "decision": decision.to_dict()}

    async def send_clarification(self, payload: dict[str, Any]) -> dict[str, Any]:
        clarification_id = str(payload.get("clarification_id") or "")
        run_id = str(payload.get("run_id") or "")
        waits = self.workflow_service.repository.interaction_waits
        wait = waits.get(clarification_id)
        if wait is not None:
            if wait["kind"] != "clarification" or (run_id and wait["request"].get("run_id") != run_id):
                raise ValueError("clarification target does not match the suspended role")
            answers = list(payload.get("answers") or ())
            if not answers or any(not str(item.get("answer") or "").strip() for item in answers):
                raise ValueError("clarification requires a non-empty answer")
            wanted = [q.get("id") or q.get("question_id") for q in wait["request"]["questions"]]
            if [item.get("question_id") for item in answers] != wanted:
                raise ValueError("clarification answer does not match the requested questions")
            answer = {"clarification_id": clarification_id, "answers": answers}
            delivery = record_clarification_answer(self.workflow_service, clarification_id, answer)
            if delivery:
                self.events.queue_event(_delivery_event_from_row(delivery))
            self._wake_event.set()
            return {"ok": True, "clarification": answer}
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
        if not await self.semantic_orchestrator.send_worker_control(
            state.run_id,
            {"type": "clarification", "clarification": clarification},
        ):
            raise RuntimeError("Bunshin worker is no longer available for clarification")
        state.acknowledge("clarification", clarification_id)
        return {"ok": True, "run": state.summary(), "clarification": clarification}

    def _append_architect_clarification(
        self,
        state: BunshinRunState,
        clarification: Mapping[str, Any],
    ) -> dict[str, Any]:
        binding = dict(dict(state.pack.metadata or {}).get("bunshin") or {})
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
        return self.workflow_service.append_architect_clarification(
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

    def _pending_clarification_runs(self, workflow_id: str) -> list[BunshinRunState]:
        matches: list[BunshinRunState] = []
        for state in self.runs.values():
            binding = dict(dict(state.pack.metadata or {}).get("bunshin") or {})
            if (
                state.active
                and state.pending_clarification
                and str(binding.get("workflow_id") or "") == workflow_id
            ):
                matches.append(state)
        return matches

    def _task_status(
        self,
        task_id: str,
        *,
        workflow_id: str = "",
        view: str = "status",
    ) -> dict[str, Any]:
        status = self.workflow_service.task_status(
            task_id,
            workflow_id=workflow_id,
            view=view,
        )
        workflow = dict(status.get("workflow") or {})
        if workflow.get("status") != "ok" or not workflow_id:
            return status
        matches = self._pending_clarification_runs(workflow_id)
        if not matches:
            return status
        return {
            **status,
            "workflow": {
                **workflow,
                "active_worker": "",
                "active_worker_role": "",
                "active_role_progress": {},
                "next_legal_action": ["answer_question", "control_workflow:cancel"],
                "waiting_for_user": True,
                "liveness": "human_wait",
                "pending_question_count": len(matches),
                "pending_question": pending_clarification_status(
                    matches[0].pending_clarification
                ),
            },
        }

    async def answer_workflow_question(self, payload: dict[str, Any]) -> dict[str, Any]:
        workflow_id = str(payload.get("workflow_id") or "").strip()
        answer = str(payload.get("answer") or "").strip()
        if not workflow_id or not answer:
            raise ValueError("workflow_id and answer are required")
        durable = [wait for wait in self.workflow_service.repository.interaction_waits.pending(workflow_id=workflow_id)
                   if wait["kind"] == "clarification"]
        matches = self._pending_clarification_runs(workflow_id)
        if len(durable) + len(matches) != 1:
            raise ValueError(
                f"workflow has {len(durable) + len(matches)} pending worker questions; expected exactly one"
            )
        if durable:
            pending = durable[0]["request"]
            run_id = pending["run_id"]
            bunshin_id = pending.get("bunshin_id", "")
        else:
            pending = matches[0].pending_clarification
            run_id = matches[0].run_id
            bunshin_id = matches[0].bunshin_id
        questions = [
            dict(item) for item in list(pending.get("questions") or []) if isinstance(item, dict)
        ]
        question_id = str(
            (questions[0] if questions else {}).get("question_id")
            or (questions[0] if questions else {}).get("id")
            or "question-1"
        )
        result = await self.send_clarification(
            {
                "clarification_id": str(pending.get("clarification_id") or ""),
                "run_id": run_id,
                "bunshin_id": bunshin_id,
                "answers": [{"question_id": question_id, "answer": answer}],
            }
        )
        if durable:
            return result
        event = {
            "event_kind": "clarification_resolved",
            "bunshin_id": bunshin_id,
            "run_id": run_id,
            "workflow_id": workflow_id,
            "bunshin_profile": "",
            "payload": {
                "bunshin": True,
                "clarification_id": str(pending.get("clarification_id") or ""),
                "summary": "Bunshin clarification recorded.",
            },
            "created_at": utc_now(),
        }
        self._queue_task_delivery_event(
            event,
            dedup_key=(
                f"clarification-resolved:{workflow_id}:"
                f"{pending.get('clarification_id') or ''}"
            ),
        )
        return result

    async def llm_transport_stream_frames(self, params: dict[str, Any]) -> AsyncGenerator[dict[str, Any], None]:
        state = self._require_broker_run(params)
        endpoint, request = authorize_role_request(state.pack.metadata, params)
        async with contextlib.aclosing(self.llm_proxy.stream(state.run_id, state.attempt_id, endpoint, request)) as stream:
            async for frame in stream:
                yield frame

    async def llm_usage_receipt(self, params: dict[str, Any]) -> dict[str, Any]:
        state = self._require_broker_run(params)
        return self.llm_proxy.record_usage(state.run_id, state.attempt_id, params)

    async def refresh_llm_endpoints(self, _params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        refreshed = await self.llm_proxy.retire_transport()
        return {"ok": True, "runtime_loaded": False, "refreshed": refreshed}

    def _require_broker_run(self, params: Mapping[str, Any]) -> BunshinRunState:
        state = self.run_ledger.require_active(str(params.get("run_id") or ""))
        if params.get("_broker_attempt_id", state.attempt_id) != state.attempt_id:
            raise PermissionError("broker attempt changed after authorization")
        return state

    async def _host_tool_runtime_bundle(self) -> Any:
        async with self._web_request_admission():
            return await self._admitted_host_tool_runtime_bundle()

    async def _admitted_host_tool_runtime_bundle(self) -> Any:
        from pal.bunshin.runner_components.host_web_runtime import build_host_web_runtime
        from pal.foundation.workers import run_worker
        async with self._host_tool_lock:
            if self._host_tool_bundle is None:
                def build():
                    # Transfer ownership before awaiting the worker result, so
                    # cancellation still leaves a bundle that close_all owns.
                    self._host_tool_bundle = build_host_web_runtime(self.runtime_root)
                await run_worker(build)
            return self._host_tool_bundle

    async def web_broker_search(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._web_broker_call(
            params,
            canonical_path="op_web_search",
            alias="search_web",
        )

    async def web_broker_read(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._web_broker_call(
            params,
            canonical_path="op_browser_read",
            alias="read_browser_page",
        )

    @contextlib.asynccontextmanager
    async def _web_request_admission(self):
        if self._host_tools_closing:
            raise RuntimeError("Manager web broker is closing")
        async with self._host_tool_gate.read_async():
            if self._host_tools_closing:
                raise RuntimeError("Manager web broker is closing")
            yield

    async def _web_broker_call(
        self,
        params: dict[str, Any],
        *,
        canonical_path: str,
        alias: str,
    ) -> dict[str, Any]:
        async with self._web_request_admission():
            return await self._admitted_web_broker_call(params, canonical_path=canonical_path, alias=alias)

    async def _admitted_web_broker_call(
        self, params: dict[str, Any], *, canonical_path: str, alias: str,
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
        bundle = await self._admitted_host_tool_runtime_bundle()
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

    def health(self) -> dict[str, Any]:
        active = self.run_ledger.active()
        return {
            "ok": True,
            "health_source": "bunshin_manager",
            "lifecycle_protocol": "plugin_raii.v1",
            "manager_pid": os.getpid(),
            "started_at": self.started_at,
            "run_count": len(self.runs),
            "active_count": len(active),
            "active_runs": [item.summary() for item in active],
            "shutdown_requested": self._drain_requested.is_set() or self._shutdown_event.is_set(),
            "draining": self._drain_requested.is_set() and not self._shutdown_event.is_set(),
            "shutdown_reason": self._shutdown_reason,
            "materialized_role_count": self.semantic_orchestrator.processes.materialized_count,
            "inherited_role_count": self.capacity_pool.inherited_count,
            "reserved_node_count": len(self.capacity_pool.reserved_nodes),
            "max_parallel_nodes": self.max_parallel_modules,
            "max_parallel_llm_nodes": self.max_parallel_modules,
            "prompt_log_enabled": self.prompt_log_enabled,
            "pending_event_count": len(self.event_queue),
            **self.llm_proxy.health(),
            "fd_leases": fd_lease_snapshot(resource_kind=None),
            "event_subscriber_count": len(self.event_subscribers),
            "bunshin_db_path": str(self.workflow_service.repository.database.db_path),
            "log_sink": current_service_log_sink_description(),
            "catalog_generation": str(self.catalog.snapshot()["generation"]),
            "harness_generation": (
                builtin_harness_generation().generation_hash
            ),
            "harnesses": [
                spec.to_dict()
                for spec in builtin_harness_generation().specs
            ],
            **dict(self.endpoint_info),
        }

    def reload_runtime_config(self) -> dict[str, Any]:
        config = effective_bunshin_runtime_config(self.runtime_root)
        self.max_parallel_modules = max(
            1,
            int(config.get("max_parallel_llm_nodes", config.get("max_parallel_modules", self.max_parallel_modules)) or self.max_parallel_modules),
        )
        self.semantic_orchestrator.max_parallel_workers = self.max_parallel_modules
        self._wake_event.set()
        return {"ok": True, "status": "ok", "config": config, "max_parallel_llm_nodes": self.max_parallel_modules}

    async def set_prompt_log_enabled(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Set the debug policy snapshot used by future role processes."""

        self.prompt_log_enabled = bool(params.get("enabled"))
        self.semantic_orchestrator.set_prompt_log_enabled(self.prompt_log_enabled)
        return {"ok": True, "enabled": self.prompt_log_enabled}

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
        self.semantic_orchestrator.request_stop()
        if graceful:
            self._drain_requested.set()
            if self._drain_task is None or self._drain_task.done():
                self._drain_task = asyncio.create_task(
                    self._drain_for_shutdown(),
                    name="bunshin-manager-graceful-drain",
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
                    or not state.active
                    or process is None
                    or process.returncode is not None
                ):
                    continue
                sent = await self.semantic_orchestrator.send_worker_control(
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
                and state.active
            ]
            background_count = (
                self.workflow_outbox.active_background_count
                + self.semantic_orchestrator.active_background_count
            )
            if not active_runs and background_count == 0:
                break
            if asyncio.get_running_loop().time() >= deadline:
                self.logger.warning(
                    "bunshin manager graceful drain timed out active_runs=%s background_effects=%s",
                    len(active_runs),
                    background_count,
                )
                break
            await asyncio.sleep(0.05)
        self._shutdown_event.set()

    async def close_all(self) -> None:
        self._host_tools_closing = True
        if (self._close_task is None or (self._close_task.done()
                and (self._close_task.cancelled() or self._close_task.exception() is not None))):
            self._close_task = asyncio.create_task(self._close_all(), name="bunshin-manager-close")
        await join_owned_task(self._close_task)

    async def _close_all(self) -> None:
        # The semantic orchestrator owns worker processes.  Cancelling its
        # logical tasks enters each process owner's close path, which withdraws
        # process authority, signals once when needed, and reaps the child.
        await self.semantic_orchestrator.stop_background_workers(
            timeout_seconds=self.graceful_shutdown_timeout_seconds,
        )
        active = [
            state.run_id
            for state in self.runs.values()
            if state.active
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
        async with self._host_tool_gate.write_async():
            async with self._host_tool_lock:
                if self._host_tool_bundle is not None:
                    await self._host_tool_bundle.close()
                    self._host_tool_bundle = None
        await self.llm_proxy.retire_transport()

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


def _delivery_event_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    event = dict(row.get("payload") or {})
    event["delivery_id"] = str(row.get("delivery_id") or "")
    event["task_id"] = str(row.get("task_id") or "")
    return event
