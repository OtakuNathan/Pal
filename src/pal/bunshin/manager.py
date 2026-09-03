from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import signal
import threading
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

from pal.execution import CapabilityCall
from pal.foundation import utc_now
from pal.foundation.service_logging import current_service_log_sink_description
from pal.foundation.sidecar import dispatch_sidecar_request, pack_sidecar_message, read_sidecar_message
from pal.llm.credentials import LLMCredentialResolver
from pal.llm.endpoint_spec import endpoint_spec_fingerprint
from pal.llm.ir import LLMUsageIR, WireShape
from pal.llm.repository import LLMEndpointRepository, RuntimeSettingRepository
from pal.llm.secret_store import EncryptedFileSecretStore
from pal.llm.transport import (
    DirectSDKTransport,
    EncodedTransportRequest,
    LLMStreamControl,
)
from pal.foundation.fd_lease import fd_lease_snapshot
from pal.llm.usage import LLMUsageLedger
from pal.bunshin.catalog import BunshinCatalogService
from pal.bunshin.config import effective_bunshin_runtime_config
from pal.bunshin.event_delivery import BunshinEventDelivery
from pal.bunshin.ipc import (
    cleanup_manager_endpoint,
    cleanup_role_gateway_endpoint,
    start_manager_server,
    start_role_gateway_server,
)
from pal.bunshin.harnesses import BunshinHarnessRegistry
from pal.bunshin.web_broker import web_result_to_payload
from pal.bunshin.v2.contracts import AggregateType
from pal.bunshin.v2.orchestration import BunshinV2OutboxProcessor
from pal.bunshin.v2.recovery import BunshinV2Recovery
from pal.bunshin.v2.service import BunshinV2WorkflowService
from pal.bunshin.v2.semantic_orchestration import SemanticOrchestrator
from pal.bunshin.v2.role_gateway import RoleAssignmentGateway
from pal.shared import BunshinApprovalDecision, BunshinInvocationPack, RuntimeStatus
from pal.shared.json_values import thaw_json


_DEFAULT_MAX_PARALLEL_NODES = 5
_DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 600.0
_TERMINAL_RUN_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "killed", "timeout", "suspended", "interrupted"}
)
_LLM_TRANSPORT_REQUEST_TTL_SECONDS = 30.0 * 60.0
_LLM_TRANSPORT_REQUEST_MAX_ENTRIES = 2048
_LLM_TRANSPORT_MAX_TIMEOUT_SECONDS = 3900.0


class _ManagerTransportError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: str = "manager_transport",
        provider_started: bool = False,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.provider_started = bool(provider_started)


@dataclass
class _LLMTransportRequestState:
    request_id: str
    run_id: str
    endpoint_id: str
    model_id: str
    provider: str
    created_at: float = field(default_factory=time.monotonic)
    provider_started: bool = False
    transport_terminal: bool = False
    usage_received: bool = False
    receipt_fingerprint: str = ""


@dataclass(frozen=True)
class _TransportWorkerResult:
    error: BaseException | None = None
    close_error: Exception | None = None


def _consume_transport_iterator(
    iterator: Iterator[Any],
    publish: Callable[[str, Any], bool],
) -> _TransportWorkerResult:
    """Consume and close one provider iterator on one stable owner thread."""

    error: BaseException | None = None
    close_error: Exception | None = None
    try:
        for frame in iterator:
            if not publish("frame", frame):
                break
    except BaseException as exc:
        error = exc
    close = getattr(iterator, "close", None)
    if callable(close):
        try:
            close()
        except Exception as exc:
            close_error = exc
    result = _TransportWorkerResult(error=error, close_error=close_error)
    publish("terminal", result)
    return result


def _bounded_transport_timeout(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise _ManagerTransportError("LLM transport timeout must be numeric") from exc
    if parsed <= 0 or parsed > _LLM_TRANSPORT_MAX_TIMEOUT_SECONDS:
        raise _ManagerTransportError(
            "LLM transport timeout must be within "
            f"(0, {_LLM_TRANSPORT_MAX_TIMEOUT_SECONDS:g}]"
        )
    return parsed


def _validate_transport_payload_authority(
    endpoint: Any,
    payload: Mapping[str, Any],
    extra_body: Mapping[str, Any],
) -> None:
    if str(payload.get("model") or "").strip() != str(endpoint.model_id):
        raise _ManagerTransportError(
            "encoded LLM payload changed endpoint model identity"
        )
    if "extra_body" in payload:
        raise _ManagerTransportError(
            "encoded LLM payload must use the separate extra_body field"
        )
    protected = {
        "model",
        "max_output_tokens",
        "max_completion_tokens",
        "max_tokens",
        "stream",
    }
    overridden = sorted(protected.intersection(extra_body))
    if overridden:
        raise _ManagerTransportError(
            f"encoded LLM extra_body overrides transport authority: {overridden}"
        )
    maximum = getattr(endpoint, "max_output_tokens", None)
    if maximum is None:
        return
    for key in ("max_output_tokens", "max_completion_tokens", "max_tokens"):
        if key not in payload:
            continue
        try:
            requested = int(payload[key])
        except (TypeError, ValueError) as exc:
            raise _ManagerTransportError(
                f"encoded LLM {key} must be an integer"
            ) from exc
        if requested <= 0 or requested > int(maximum):
            raise _ManagerTransportError(
                f"encoded LLM {key} exceeds endpoint output authority"
            )


def _normalize_usage_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    integer_fields = (
        "input_tokens",
        "uncached_input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_tokens",
    )
    for key in integer_fields:
        try:
            parsed = int(value.get(key, 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"LLM usage receipt {key} must be an integer") from exc
        if parsed < 0:
            raise ValueError(f"LLM usage receipt {key} must be non-negative")
        normalized[key] = parsed
    try:
        cost = float(value.get("cost", 0.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("LLM usage receipt cost must be numeric") from exc
    if cost < 0:
        raise ValueError("LLM usage receipt cost must be non-negative")
    normalized["cost"] = cost
    normalized["reported"] = bool(value.get("reported", False))
    normalized["reasoning_tokens_reported"] = bool(
        value.get("reasoning_tokens_reported", False)
    )
    return normalized


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
            or "Bunshin needs clarification."
        ).strip(),
        "options": options[:3],
    }


@dataclass
class BunshinRunState:
    bunshin_id: str
    run_id: str
    pack: BunshinInvocationPack
    process: "ProcessStatusView | None" = None
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
            "bunshin_id": self.bunshin_id,
            "run_id": self.run_id,
            "invocation_id": self.pack.invocation_id,
            "bunshin_profile": self.pack.bunshin_profile,
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


class ProcessStatusView(Protocol):
    """Read-only scalar process projection; carries no I/O authority."""

    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...

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
    v2_service: BunshinV2WorkflowService = field(init=False)
    v2_outbox: BunshinV2OutboxProcessor = field(init=False)
    v2_semantic_orchestrator: SemanticOrchestrator = field(init=False)
    role_gateway: RoleAssignmentGateway = field(init=False)
    harness_registry: BunshinHarnessRegistry = field(init=False)
    _host_tool_bundle: Any | None = field(default=None, init=False, repr=False)
    _llm_json_transport: DirectSDKTransport | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _llm_transport_requests: OrderedDict[str, _LLMTransportRequestState] = field(
        default_factory=OrderedDict,
        init=False,
        repr=False,
    )
    _llm_transport_workers: set[asyncio.Task[_TransportWorkerResult]] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    _llm_usage_ledger: LLMUsageLedger = field(
        default_factory=lambda: LLMUsageLedger(scope="bunshin_manager_transport"),
        init=False,
        repr=False,
    )
    _shutdown_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _drain_requested: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _v2_wake_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _v2_outbox_task: asyncio.Task[Any] | None = field(default=None, init=False, repr=False)
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
        self.catalog = BunshinCatalogService(Path(self.runtime_root))
        self.catalog_bootstrap = self.catalog.bootstrap()
        config = effective_bunshin_runtime_config(Path(self.runtime_root))
        configured = config.get("max_parallel_llm_nodes", config.get("max_parallel_modules", _DEFAULT_MAX_PARALLEL_NODES))
        self.max_parallel_modules = max(1, int(self.max_parallel_modules or configured or _DEFAULT_MAX_PARALLEL_NODES))
        self.v2_service = BunshinV2WorkflowService(Path(self.runtime_root))
        try:
            self.v2_service.repository.reconcile_terminal_role_runtime()
            self.v2_service.repository.reconcile_role_session_checkpoints()
        except Exception:
            # Reconciliation is a leak repair, never a reason to make the
            # Manager unavailable. Durable state remains authoritative and a
            # later startup can retry the idempotent cleanup.
            self.logger.exception("bunshin terminal runtime reconciliation failed")
        self.events = BunshinEventDelivery(
            load_backlog=self._pending_task_delivery_events,
        )
        self.role_gateway = RoleAssignmentGateway(self.v2_service)
        self.harness_registry = BunshinHarnessRegistry(include_pal=True)
        self.v2_semantic_orchestrator = SemanticOrchestrator(
            self.v2_service,
            harness_registry=self.harness_registry,
            max_parallel_workers=self.max_parallel_modules,
            runtime_db_path=self.runtime_db_path,
            publish_human_review=self._publish_v2_human_review,
            publish_worker_event=self._publish_v2_worker_event,
            publish_workflow_event=self._publish_v2_workflow_event,
            register_broker_run=self._register_v2_broker_run,
            unregister_broker_run=self._unregister_v2_broker_run,
            inject_skill=self._inject_skill_for_role,
        )
        self.v2_outbox = BunshinV2OutboxProcessor(
            self.v2_service,
            semantic_effects=self.v2_semantic_orchestrator,
            publish_workflow_event=self._publish_v2_workflow_event,
        )

    @property
    def event_queue(self) -> list[dict[str, Any]]:
        return self.events.queue

    @property
    def event_subscribers(self) -> list[asyncio.StreamWriter]:
        return self.events.subscribers

    async def run(self) -> None:
        recovery = await asyncio.to_thread(BunshinV2Recovery(self.v2_service).recover)
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
            recovered_assignments = await self.v2_semantic_orchestrator.recover_background_assignments()
            if recovered_assignments:
                self.logger.info(
                    "recovered %s durable role assignment(s)",
                    recovered_assignments,
                )
            self._v2_outbox_task = asyncio.create_task(self._run_v2_outbox(), name="bunshin-v2-outbox")
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
                db_path=self.runtime_db_path or Path(self.runtime_root) / "pal.sqlite3",
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
            "llm_usage_receipt": self.llm_usage_receipt,
            "web_search": self.web_broker_search,
            "web_read": self.web_broker_read,
        }
        if method in broker_handlers:
            payload = await self._authorize_worker_broker_params(params)
            return await broker_handlers[method](payload)
        return await asyncio.to_thread(self.role_gateway.call, method, params)

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
            generation = self.harness_registry.replace_external(
                dict(params.get("generation") or {})
            )
            self._v2_wake_event.set()
            return {
                "ok": True,
                "generation": generation.to_dict(),
            }
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
        if method == "v2_task_status":
            return self._v2_task_status(
                str(params.get("task_id") or ""),
                workflow_id=str(params.get("workflow_id") or ""),
                view=str(params.get("view") or "status"),
            )
        if method == "v2_start_workflow":
            result = self.v2_service.start_workflow(params)
            self._v2_wake_event.set()
            return result
        if method == "v2_rebind_task_delivery":
            result = self.v2_service.repository.rebind_task_delivery(
                task_id=str(params.get("task_id") or ""),
                binding=dict(params.get("binding") or {}),
            )
            if bool(result.get("changed")):
                self._replay_waiting_task_deliveries(
                    str(params.get("task_id") or ""),
                    binding_version=int(result.get("binding_version") or 0),
                )
            self._v2_wake_event.set()
            return result
        if method == "v2_ack_task_delivery":
            return {
                "acknowledged": self.v2_service.repository.acknowledge_task_delivery(
                    str(params.get("delivery_id") or "")
                )
            }
        if method == "v2_list_task_delivery_parts":
            return {
                "parts": list(
                    self.v2_service.repository.delivered_task_delivery_parts(
                        str(params.get("delivery_id") or "")
                    )
                )
            }
        if method == "v2_ack_task_delivery_part":
            return {
                "acknowledged": self.v2_service.repository.acknowledge_task_delivery_part(
                    str(params.get("delivery_id") or ""),
                    str(params.get("part_key") or ""),
                )
            }
        if method == "v2_defer_task_delivery":
            return {
                "deferred": self.v2_service.repository.defer_task_delivery(
                    str(params.get("delivery_id") or ""),
                    error=str(params.get("error") or ""),
                )
            }
        if method == "list_runs":
            return {"items": [item.summary() for item in sorted(self.runs.values(), key=lambda run: run.started_at)]}
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
        raise ValueError(f"unknown Bunshin V2 manager method: {method}")

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
        return payload

    async def _serve_llm_stream_request(
        self,
        request: dict[str, Any],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        worker: bool,
    ) -> None:
        request_id = str(request.get("id") or "")
        method = str(request.get("method") or "")
        disconnect: asyncio.Task[bytes] | None = None
        stream: AsyncIterator[dict[str, Any]] | None = None
        try:
            if method != "llm_transport_stream":
                raise ValueError(f"sidecar method is not streamable: {method}")
            params = dict(request.get("params") or {})
            if worker:
                params = await self._authorize_worker_broker_params(params)
            stream = self.llm_transport_stream_frames(params).__aiter__()
            disconnect = asyncio.create_task(reader.read(1))
            while True:
                advance = asyncio.create_task(anext(stream))
                done, _ = await asyncio.wait(
                    {advance, disconnect},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if disconnect in done:
                    advance.cancel()
                    with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                        await advance
                    return
                try:
                    item = advance.result()
                except StopAsyncIteration:
                    disconnect.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await disconnect
                    break
                writer.write(
                    pack_sidecar_message(
                        {
                            "type": "stream_item",
                            "id": request_id,
                            "ok": True,
                            "result": dict(item),
                        }
                    )
                )
                await writer.drain()
            terminal = {
                "type": "stream_end",
                "id": request_id,
                "ok": True,
                "result": {},
            }
        except Exception as exc:
            self.logger.exception("sidecar stream request failed: %s", method)
            terminal = {
                "type": "stream_end",
                "id": request_id,
                "ok": False,
                "error": {
                    "kind": str(
                        getattr(
                            exc,
                            "kind",
                            "role_gateway" if worker else "manager",
                        )
                    ),
                    "message": f"{exc.__class__.__name__}: {exc}",
                    "provider_started": bool(
                        getattr(exc, "provider_started", False)
                    ),
                },
            }
        finally:
            if disconnect is not None and not disconnect.done():
                disconnect.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await disconnect
            if stream is not None:
                close = getattr(stream, "aclose", None)
                if callable(close):
                    with contextlib.suppress(Exception):
                        await close()
        writer.write(pack_sidecar_message(terminal))
        await writer.drain()

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
                self.logger.exception("bunshin V2 outbox tick failed")
            self._v2_wake_event.clear()
            try:
                await asyncio.wait_for(self._v2_wake_event.wait(), timeout=0.25)
            except TimeoutError:
                pass

    async def _publish_v2_human_review(self, payload: Mapping[str, Any]) -> None:
        standalone = bool(payload.get("standalone_review_id"))
        event = {
            "event_kind": "standalone_review_completed" if standalone else "architecture_review_pending",
            "bunshin_id": "",
            "run_id": "",
            "workflow_id": str(payload.get("workflow_id") or ""),
            "bunshin_profile": "bunshin_v2.reviewer",
            "role_mode": "standalone" if standalone else "architecture",
            "payload": {**dict(payload), "bunshin_v2": True, **({"status": "completed"} if standalone else {})},
            "created_at": utc_now(),
        }
        self._queue_task_delivery_event(
            event,
            dedup_key=(
                f"human-review:{event['workflow_id']}:"
                f"{payload.get('architecture_revision_id') or payload.get('standalone_review_id') or ''}"
            ),
        )

    def _publish_v2_workflow_event(self, payload: Mapping[str, Any]) -> None:
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
                "bunshin_profile": "bunshin_v2.reviewer",
                "role_mode": "architecture",
                "payload": {**dict(payload), "bunshin_v2": True},
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
            "bunshin_profile": "bunshin_v2.workflow",
            "role_mode": "workflow",
            "payload": {**dict(payload), "bunshin_v2": True},
            "created_at": str(payload.get("terminal_at") or utc_now()),
        }
        self._queue_task_delivery_event(
            event,
            dedup_key=f"workflow-terminal:{workflow_id}:{status}",
        )

    async def _publish_v2_worker_event(self, event: Mapping[str, Any]) -> None:
        item = dict(event)
        delivery_attempt_id = str(item.pop("_attempt_id", "") or "")
        run_id = str(item.get("run_id") or "")
        state = self.runs.get(run_id)
        if state is not None:
            payload = dict(item.get("payload") or {})
            payload.pop("route", None)
            payload.pop("control_route", None)
            kind = str(item.get("event_kind") or "")
            binding = dict(dict(state.pack.metadata or {}).get("bunshin_v2") or {})
            workflow_id = str(binding.get("workflow_id") or "")
            if workflow_id and not item.get("workflow_id"):
                item["workflow_id"] = workflow_id
            item["payload"] = payload
            if kind == "approval_requested":
                state.pending_approval = payload
                state.status = "approval_pending"
            elif kind == "clarification_requested":
                state.pending_clarification = payload
                state.status = "clarification_pending"
            elif kind == "terminal":
                resolved_interactions: list[dict[str, str]] = []
                approval_id = str(state.pending_approval.get("approval_id") or "").strip()
                if approval_id:
                    resolved_interactions.append(
                        {
                            "interaction_id": approval_id,
                            "interaction_kind": "bunshin_approval",
                        }
                    )
                clarification_id = str(
                    state.pending_clarification.get("clarification_id") or ""
                ).strip()
                if clarification_id:
                    resolved_interactions.append(
                        {
                            "interaction_id": clarification_id,
                            "interaction_kind": "bunshin_clarification",
                        }
                    )
                if resolved_interactions:
                    payload["resolved_interactions"] = resolved_interactions
                state.pending_approval = {}
                state.pending_clarification = {}
                # A terminal IPC receipt means the worker has finished its
                # logical work, not that its process owner has finished
                # cleanup. Keep the run active until the owner confirms its
                # direct child has been reaped.
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
        kind = str(item.get("event_kind") or "")
        event_payload = dict(item.get("payload") or {})
        debug_enabled = (
            bool(dict(state.pack.metadata or {}).get("prompt_log_enabled"))
            if state is not None
            else bool(self.prompt_log_enabled)
        )
        if debug_enabled:
            self.v2_service.repository.record_worker_event(item)
        elif (
            kind == "progress"
            and str(event_payload.get("phase") or "")
            == "llm_round_completed"
        ):
            # Efficiency accounting is ordinary operational telemetry, not
            # prompt logging. Persist only the non-content round fields when
            # debug history is disabled so batching metrics remain available
            # without retaining prompts, previews, tool arguments, or routes.
            self.v2_service.repository.record_worker_event(
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

    def _queue_task_delivery_event(
        self,
        event: Mapping[str, Any],
        *,
        dedup_key: str,
    ) -> None:
        item = dict(event)
        workflow_id = str(item.get("workflow_id") or "")
        workflow = self.v2_service.repository.read_snapshot(
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
        row = self.v2_service.repository.enqueue_task_delivery(
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
            for row in self.v2_service.repository.list_pending_task_deliveries(limit=200)
        ]

    def _replay_waiting_task_deliveries(
        self,
        task_id: str,
        *,
        binding_version: int,
    ) -> None:
        candidates: set[tuple[str, str]] = {
            (workflow_id, "architecture_review_pending")
            for workflow_id in self.v2_service.repository.pending_human_review_workflows(
                task_id
            )
        }
        for state in self.runs.values():
            if not state.pending_clarification and not state.pending_approval:
                continue
            binding = dict(dict(state.pack.metadata or {}).get("bunshin_v2") or {})
            workflow_id = str(binding.get("workflow_id") or "")
            workflow = self.v2_service.repository.read_snapshot(
                AggregateType.WORKFLOW,
                workflow_id,
            )
            if str((workflow.payload if workflow is not None else {}).get("task_id") or "") != task_id:
                continue
            if state.pending_clarification:
                candidates.add((workflow_id, "clarification_requested"))
            if state.pending_approval:
                candidates.add((workflow_id, "approval_requested"))
        for workflow_id, event_kind in sorted(candidates):
            source = self.v2_service.repository.latest_task_delivery(
                task_id=task_id,
                workflow_id=workflow_id,
                event_kind=event_kind,
            )
            if source is None or str(source.get("status") or "") == "pending":
                continue
            replay = self.v2_service.repository.replay_task_delivery(
                delivery_id=str(source["delivery_id"]),
                dedup_key=(
                    f"rebind-replay:{task_id}:{binding_version}:"
                    f"{source['delivery_id']}"
                ),
            )
            self.events.queue_event(_delivery_event_from_row(replay))

    def _register_v2_broker_run(
        self,
        run_id: str,
        bunshin_id: str,
        pack: BunshinInvocationPack,
        process: ProcessStatusView,
    ) -> None:
        self.runs[run_id] = BunshinRunState(bunshin_id=bunshin_id, run_id=run_id, pack=pack, process=process)

    def _unregister_v2_broker_run(
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
        decision = BunshinApprovalDecision.from_dict(payload)
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
        state: BunshinRunState,
        clarification: Mapping[str, Any],
    ) -> dict[str, Any]:
        binding = dict(dict(state.pack.metadata or {}).get("bunshin_v2") or {})
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

    def _pending_clarification_runs(self, workflow_id: str) -> list[BunshinRunState]:
        matches: list[BunshinRunState] = []
        for state in self.runs.values():
            binding = dict(dict(state.pack.metadata or {}).get("bunshin_v2") or {})
            if (
                state.status not in _TERMINAL_RUN_STATUSES
                and state.pending_clarification
                and str(binding.get("workflow_id") or "") == workflow_id
            ):
                matches.append(state)
        return matches

    def _v2_task_status(
        self,
        task_id: str,
        *,
        workflow_id: str = "",
        view: str = "status",
    ) -> dict[str, Any]:
        status = self.v2_service.task_status(
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
                "pending_question": _pending_clarification_status(
                    matches[0].pending_clarification
                ),
            },
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
        result = await self.send_clarification(
            {
                "clarification_id": str(pending.get("clarification_id") or ""),
                "run_id": state.run_id,
                "bunshin_id": state.bunshin_id,
                "answers": [{"question_id": question_id, "answer": answer}],
            }
        )
        event = {
            "event_kind": "clarification_resolved",
            "bunshin_id": state.bunshin_id,
            "run_id": state.run_id,
            "workflow_id": workflow_id,
            "bunshin_profile": "",
            "payload": {
                "bunshin_v2": True,
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

    async def llm_transport_stream_frames(
        self,
        params: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        state = self._require_broker_run(params)
        endpoint = self._authorize_llm_transport_endpoint(state, params)
        request_id = str(params.get("request_id") or "").strip()
        if not request_id:
            raise _ManagerTransportError("LLM transport request_id is required")
        self._prune_llm_transport_requests()
        if request_id in self._llm_transport_requests:
            raise _ManagerTransportError(
                f"LLM transport request_id was already used: {request_id}",
                kind="request_replay",
            )
        timeout_seconds = _bounded_transport_timeout(params.get("timeout_seconds"))
        payload = params.get("payload")
        extra_body = params.get("extra_body")
        if not isinstance(payload, Mapping) or not isinstance(extra_body, Mapping):
            raise _ManagerTransportError("encoded LLM transport payload must be an object")
        _validate_transport_payload_authority(endpoint, payload, extra_body)
        record = _LLMTransportRequestState(
            request_id=request_id,
            run_id=state.run_id,
            endpoint_id=str(endpoint.endpoint_id),
            model_id=str(endpoint.model_id),
            provider=str(endpoint.provider),
        )
        self._llm_transport_requests[request_id] = record
        control = LLMStreamControl()
        iterator = self._manager_llm_json_transport().frames(
            endpoint,
            EncodedTransportRequest(
                request_id=request_id,
                wire_shape=WireShape(str(endpoint.wire_shape)),
                timeout_seconds=timeout_seconds,
                payload=dict(payload),
                extra_body=dict(extra_body),
                stream=bool(params.get("stream", True)),
                stream_control=control,
            ),
        )
        provider_announced = False
        completed = False
        loop = asyncio.get_running_loop()
        events: asyncio.Queue[tuple[str, Any, threading.Event | None]] = asyncio.Queue()

        def publish(kind: str, value: Any) -> bool:
            if kind == "frame" and control.cancelled:
                return False
            acknowledged = threading.Event() if kind == "frame" else None
            try:
                loop.call_soon_threadsafe(
                    events.put_nowait,
                    (kind, value, acknowledged),
                )
            except RuntimeError:
                return False
            if acknowledged is not None:
                while not acknowledged.wait(0.05):
                    if control.cancelled:
                        return False
            return True

        worker = asyncio.create_task(
            asyncio.to_thread(_consume_transport_iterator, iterator, publish),
            name=f"bunshin-llm-transport:{request_id}",
        )
        self._llm_transport_workers.add(worker)

        def worker_finished(task: asyncio.Task[_TransportWorkerResult]) -> None:
            self._llm_transport_workers.discard(task)
            if task.cancelled():
                return
            with contextlib.suppress(BaseException):
                task.result()
                record.provider_started = bool(
                    record.provider_started or control.provider_started
                )
                record.transport_terminal = True

        worker.add_done_callback(worker_finished)
        pending_acknowledgement: threading.Event | None = None
        try:
            while True:
                try:
                    kind, value, acknowledgement = await asyncio.wait_for(
                        events.get(),
                        timeout=0.05,
                    )
                except TimeoutError:
                    if control.provider_started and not provider_announced:
                        provider_announced = True
                        record.provider_started = True
                        yield {"event": "provider_started"}
                    continue
                if control.provider_started and not provider_announced:
                    provider_announced = True
                    record.provider_started = True
                    yield {"event": "provider_started"}
                if kind == "terminal":
                    result = value
                    if not isinstance(result, _TransportWorkerResult):
                        raise RuntimeError("LLM transport worker emitted an invalid terminal result")
                    completed = True
                    await asyncio.shield(worker)
                    if result.error is not None:
                        raise result.error
                    if result.close_error is not None:
                        raise result.close_error
                    return
                frame = value
                pending_acknowledgement = acknowledgement
                try:
                    yield {
                        "frame": {
                            "sequence": int(frame.sequence),
                            "payload": thaw_json(frame.payload),
                        }
                    }
                finally:
                    if acknowledgement is not None:
                        acknowledgement.set()
                    pending_acknowledgement = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            record.provider_started = bool(
                record.provider_started or control.provider_started
            )
            raise _ManagerTransportError(
                str(exc),
                provider_started=record.provider_started,
            ) from exc
        finally:
            if pending_acknowledgement is not None:
                pending_acknowledgement.set()
            if not completed:
                control.cancel("proxy_consumer_closed")
            if not worker.done():
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(asyncio.shield(worker), timeout=1.0)
            record.provider_started = bool(
                record.provider_started or control.provider_started
            )
            if worker.done() and not worker.cancelled():
                record.transport_terminal = True

    async def llm_usage_receipt(self, params: dict[str, Any]) -> dict[str, Any]:
        state = self._require_broker_run(params)
        self._prune_llm_transport_requests()
        request_id = str(params.get("request_id") or "").strip()
        record = self._llm_transport_requests.get(request_id)
        if record is None or record.run_id != state.run_id:
            raise KeyError(f"unknown LLM transport request receipt: {request_id}")
        if not record.transport_terminal:
            raise RuntimeError("LLM usage receipt arrived before transport terminal")
        endpoint_id = str(params.get("endpoint_id") or "").strip()
        if endpoint_id != record.endpoint_id:
            raise PermissionError("LLM usage receipt changed endpoint identity")
        usage_payload = params.get("usage")
        if not isinstance(usage_payload, Mapping):
            raise ValueError("LLM usage receipt has no usage object")
        normalized_usage = _normalize_usage_receipt(usage_payload)
        try:
            provider_response_count = int(
                params.get("provider_response_count") or 0
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "LLM usage receipt provider_response_count must be an integer"
            ) from exc
        if provider_response_count < 1:
            raise ValueError(
                "LLM usage receipt provider_response_count must be positive"
            )
        receipt_payload = {
            "request_id": request_id,
            "endpoint_id": endpoint_id,
            "model_id": str(params.get("model_id") or ""),
            "provider": str(params.get("provider") or ""),
            "provider_response_count": provider_response_count,
            "usage": normalized_usage,
        }
        receipt_fingerprint = hashlib.sha256(
            json.dumps(
                receipt_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if record.usage_received:
            if record.receipt_fingerprint != receipt_fingerprint:
                raise ValueError("LLM usage receipt replay changed its payload")
            return {"ok": True, "duplicate": True}
        if receipt_payload["model_id"] != record.model_id:
            raise ValueError("LLM usage receipt changed model identity")
        if receipt_payload["provider"] != record.provider:
            raise ValueError("LLM usage receipt changed provider identity")
        self._llm_usage_ledger.record_success(
            endpoint_id=record.endpoint_id,
            model_id=record.model_id,
            provider=record.provider,
            usage=LLMUsageIR(**normalized_usage),
            provider_response_count=receipt_payload["provider_response_count"],
        )
        record.usage_received = True
        record.receipt_fingerprint = receipt_fingerprint
        return {"ok": True, "duplicate": False}

    def _authorize_llm_transport_endpoint(
        self,
        state: BunshinRunState,
        params: Mapping[str, Any],
    ) -> Any:
        endpoint_id = str(params.get("endpoint_id") or "").strip()
        if not endpoint_id:
            raise _ManagerTransportError("LLM transport endpoint_id is required")
        pack_metadata = dict(state.pack.metadata or {})
        preferred = str(pack_metadata.get("preferred_endpoint_id") or "").strip()
        if preferred and endpoint_id != preferred:
            raise PermissionError(
                f"role assignment does not allow LLM endpoint {endpoint_id}"
            )
        if not preferred:
            active = str(
                RuntimeSettingRepository().get_active_llm_endpoint_id() or ""
            ).strip()
            if active and endpoint_id != active:
                raise _ManagerTransportError(
                    "active LLM endpoint changed before provider start",
                    kind="endpoint_spec_stale",
                )
        endpoint = LLMEndpointRepository().get(endpoint_id)
        if endpoint is None or not bool(endpoint.enabled):
            raise _ManagerTransportError(
                f"LLM endpoint is unavailable: {endpoint_id}",
                kind="endpoint_spec_stale",
            )
        requested_shape = str(params.get("wire_shape") or "").strip()
        if requested_shape != str(endpoint.wire_shape):
            raise _ManagerTransportError(
                "LLM endpoint wire shape changed before provider start",
                kind="endpoint_spec_stale",
            )
        requested_fingerprint = str(
            params.get("endpoint_spec_fingerprint") or ""
        ).strip()
        if requested_fingerprint != endpoint_spec_fingerprint(endpoint):
            raise _ManagerTransportError(
                "LLM endpoint specification changed before provider start",
                kind="endpoint_spec_stale",
            )
        return endpoint

    def _manager_llm_json_transport(self) -> DirectSDKTransport:
        if self._llm_json_transport is None:
            credentials = LLMCredentialResolver(
                secret_store=EncryptedFileSecretStore(
                    secrets_path=str(Path(self.runtime_root) / "secrets.json")
                )
            )
            self._llm_json_transport = DirectSDKTransport(
                credential_resolver=credentials.resolve_api_key
            )
        return self._llm_json_transport

    def _prune_llm_transport_requests(self) -> None:
        now = time.monotonic()
        for request_id, record in tuple(self._llm_transport_requests.items()):
            if (
                record.transport_terminal
                and now - record.created_at > _LLM_TRANSPORT_REQUEST_TTL_SECONDS
            ):
                self._llm_transport_requests.pop(request_id, None)
        while (
            len(self._llm_transport_requests)
            > _LLM_TRANSPORT_REQUEST_MAX_ENTRIES
        ):
            terminal_id = next(
                (
                    request_id
                    for request_id, record in self._llm_transport_requests.items()
                    if record.transport_terminal
                ),
                "",
            )
            if not terminal_id:
                break
            self._llm_transport_requests.pop(terminal_id, None)

    async def refresh_llm_endpoints(
        self,
        _params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Retire transport clients; Bunshins refresh semantic state at safe points."""

        refreshed = self._llm_json_transport is not None
        if self._llm_json_transport is not None:
            await asyncio.to_thread(self._llm_json_transport.close)
            self._llm_json_transport = None
        return {
            "ok": True,
            "runtime_loaded": False,
            "refreshed": refreshed,
        }

    def _require_broker_run(self, params: Mapping[str, Any]) -> BunshinRunState:
        run_id = str(params.get("run_id") or "")
        state = self.runs.get(run_id)
        if state is None:
            raise KeyError(f"unknown bunshin run: {run_id}")
        if state.status in _TERMINAL_RUN_STATUSES:
            raise RuntimeError(f"bunshin run is terminal: {run_id}")
        return state

    async def _host_tool_runtime_bundle(self) -> Any:
        if self._host_tool_bundle is None:
            from pal.bunshin.runner import build_slim_bunshin_runtime

            self._host_tool_bundle = await asyncio.to_thread(
                build_slim_bunshin_runtime,
                self.runtime_root,
                llm_authority="none",
            )
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
        bundle = await self._host_tool_runtime_bundle()
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
        active = [item for item in self.runs.values() if item.status not in _TERMINAL_RUN_STATUSES]
        self._prune_llm_transport_requests()
        usage_unreported = sum(
            1
            for item in self._llm_transport_requests.values()
            if item.transport_terminal and not item.usage_received
        )
        return {
            "ok": True,
            "health_source": "bunshin_v2_manager",
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
            "prompt_log_enabled": self.prompt_log_enabled,
            "pending_event_count": len(self.event_queue),
            "llm_transport_request_count": len(self._llm_transport_requests),
            "llm_transport_worker_count": len(self._llm_transport_workers),
            "fd_leases": fd_lease_snapshot(resource_kind=None),
            "llm_usage_unreported_count": usage_unreported,
            "llm_usage": self._llm_usage_ledger.snapshot(),
            "event_subscriber_count": len(self.event_subscribers),
            "bunshin_db_path": str(self.v2_service.repository.db_path),
            "log_sink": current_service_log_sink_description(),
            "catalog_generation": str(self.catalog.snapshot()["generation"]),
            "harness_generation": (
                self.harness_registry.snapshot().generation_hash
            ),
            "harnesses": [
                spec.to_dict()
                for spec in self.harness_registry.snapshot().specs
            ],
            **dict(self.endpoint_info),
        }

    def reload_runtime_config(self) -> dict[str, Any]:
        config = effective_bunshin_runtime_config(self.runtime_root)
        self.max_parallel_modules = max(
            1,
            int(config.get("max_parallel_llm_nodes", config.get("max_parallel_modules", self.max_parallel_modules)) or self.max_parallel_modules),
        )
        self.v2_semantic_orchestrator.max_parallel_workers = self.max_parallel_modules
        self._v2_wake_event.set()
        return {"ok": True, "status": "ok", "config": config, "max_parallel_llm_nodes": self.max_parallel_modules}

    async def set_prompt_log_enabled(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Set the debug policy snapshot used by future role processes."""

        self.prompt_log_enabled = bool(params.get("enabled"))
        self.v2_semantic_orchestrator.prompt_log_enabled = self.prompt_log_enabled
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
        self.v2_semantic_orchestrator.request_stop()
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
                    "bunshin manager graceful drain timed out active_runs=%s background_effects=%s",
                    len(active_runs),
                    background_count,
                )
                break
            await asyncio.sleep(0.05)
        self._shutdown_event.set()

    async def close_all(self) -> None:
        # The semantic orchestrator owns worker processes.  Cancelling its
        # logical tasks enters each process owner's close path, which withdraws
        # process authority, signals once when needed, and reaps the child.
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
        if self._host_tool_bundle is not None:
            close = getattr(self._host_tool_bundle, "close", None)
            if callable(close):
                await close()
            self._host_tool_bundle = None
        if self._llm_json_transport is not None:
            await asyncio.to_thread(self._llm_json_transport.close)
            self._llm_json_transport = None

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
