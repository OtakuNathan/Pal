from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from pal.artifact import ArtifactManager, ArtifactRepository, register_with_core as register_artifact_with_core
from pal.core import PalCore
from pal.core.runtime_config import RuntimeConfig
from pal.core.tool_stagnation import ToolStagnationGuardProcess
from pal.core.turn_events import TurnEventBus
from pal.core.turn_executor import TurnExecutor
from pal.core.turns import (
    AgentLoopFrame,
    EffectResult,
    L1CommitPayload,
    LLMRequestEffect,
    MemoryCompactEffect,
    ToolCallEffect,
    TurnContinuation,
    TurnOutcome,
    agent_turn_program,
)
from pal.execution import CapabilityCall, register_with_core as register_execution_with_core
from pal.foundation import EventEnvelope, PalV2Database, utc_now
from pal.llm import EndpointResolver, LLMEndpointRepository, LLMRuntime, LLMCredentialResolver, RuntimeSettingRepository, build_default_endpoint_invoker
from pal.llm.contracts import (
    CanonicalLLMOutcome,
    CanonicalLLMRequest,
    CanonicalToolCall,
    CanonicalToolResult,
    LLMPreflightAdvice,
    LLMPreflightRequest,
)
from pal.llm.request_hooks import DEFAULT_LLM_REQUEST_HOOKS
from pal.llm.secret_store import EncryptedFileSecretStore
from pal.lsp import build_lsp_plugin
from pal.minion.contracts import SERIAL_MILESTONE_MODES
from pal.memory import (
    CompactionProfile,
    L1MessageKind,
    L1TranscriptMessage,
    L3ProviderSelector,
    MemoryCommitRequest,
    MemoryCompactRequest,
    MemoryPackRequest,
    MemoryService,
    register_with_core as register_memory_with_core,
)
from pal.memory.rendering import COMPACTION_SCHEMA_MINION_V1, is_compaction_payload, render_compact_context_for_llm
from pal.minion.execution_strategy import execution_strategy_from_pack
from pal.minion.git_env import inspect_milestone_checkpoint
from pal.minion.gates import checkpoint_gate_spec_for_pack, pack_requires_plan_artifact_validation
from pal.minion.llm_broker import MinionBrokerLLMRuntime
from pal.minion.scoped_execution import (
    MINION_DIRECT_WORK_TOOL_SURFACE,
    MINION_DISCOVERY_TOOL_SURFACE,
    MinionScopedExecutionRuntime,
    _effective_capability_name,
    _effective_tool_args,
    _path_is_relative_to,
    _review_tool_evidence_ref,
)
from pal.minion.workspace_tools import _append_unique_artifact, _write_minion_artifact
from pal.minion.prompt_adapter import (
    build_minion_prompt_messages,
    build_minion_task_envelope as _minion_task_envelope,
    minion_primary_input as _minion_primary_input,
    prompt_scaffold_summary as _prompt_scaffold_summary,
    prompt_view_from_pack as _prompt_view_from_pack,
)
from pal.minion.profiles import filter_minion_allowed_capabilities, is_minion_capability_denied
from pal.minion.sandbox import minion_sandbox_is_enabled
from pal.minion.turns import apply_minion_turn_to_pack
from pal.minion.user_interaction import (
    MinionUserInteractionPort,
    ask_user_question_summary as _ask_user_question_summary,
)
from pal.minion.work_order import (
    dispatchable_plan_validation,
    validate_dispatchable_plan_artifact,
    build_planner_work_order,
)
from pal.plugins.l3 import MockL3Plugin, SQLiteVecL3Plugin, register_with_core as register_l3_with_core
from pal.shared import (
    ChannelEnvelope,
    EndpointConfig,
    EventKind,
    LLMFinishReason,
    LLMPreflightStatus,
    PromptAssemblyContext,
    ResponseHandle,
    RuntimeStatus,
    SourceKind,
    TaskContextPack,
    default_tool_result_text,
    llm_tool_name,
    replace_internal_tool_names,
    replace_internal_tool_names_in_value,
)
from pal.web_fetch import BrowserServiceManager, WebFetchProviderRepository, WebFetchService, register_with_core as register_web_fetch_with_core
from pal.web_search import WebSearchProviderRepository, WebSearchService, register_with_core as register_web_search_with_core
from pal.wizard.runtime import ALL_MODELS, DEFAULT_LLM_ENDPOINTS, DEFAULT_WEB_FETCH_PROVIDERS, DEFAULT_WEB_SEARCH_PROVIDERS


EventWriter = Callable[[dict[str, Any]], Awaitable[None]]
DecisionReader = Callable[[float | None], Awaitable[dict[str, Any] | None]]
_DEFAULT_MANAGER_TURN_TIMEOUT_SECONDS = 3600.0
_MAX_MANAGER_TURN_TIMEOUT_SECONDS = 3600.0


@dataclass
class MinionRuntimeBundle:
    llm_runtime: Any
    execution_runtime: Any
    close_async: Callable[[], Awaitable[None]] | None = None

    async def close(self) -> None:
        if self.close_async is not None:
            await self.close_async()


@dataclass
class _MinionTurnContext:
    execution_runtime: Any
    port_registry: dict[str, Any]
    turn_event_bus: TurnEventBus = field(default_factory=TurnEventBus)

    def require_port(self, key: str) -> Any:
        return self.port_registry[key]


@dataclass
class _MinionTurnState:
    diagnostics: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _MinionTurnManager:
    guard: ToolStagnationGuardProcess = field(default_factory=ToolStagnationGuardProcess)


@dataclass
class _MinionFailureResult:
    user_feedback: str
    verification: Any = None
    report: Any = None


class _MinionCooperativeCancel(Exception):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("summary") or payload.get("reason") or "minion cancellation requested"))
        self.payload = dict(payload)


class _MinionLLMRuntimeAdapter:
    def __init__(self, runner: "MinionRunner", base_runtime: Any, state: "MinionAgentLoopState") -> None:
        self._runner = runner
        self._base = base_runtime
        self._state = state

    def resolve_max_output_tokens(
        self,
        *,
        preferred_endpoint_id: str | None = None,
        preferred_endpoint_source: str | None = None,
    ) -> int | None:
        fn = getattr(self._base, "resolve_max_output_tokens", None)
        if not callable(fn):
            return None
        try:
            return fn(preferred_endpoint_id=preferred_endpoint_id, preferred_endpoint_source=preferred_endpoint_source)
        except TypeError:
            try:
                return fn(preferred_endpoint_id=preferred_endpoint_id)
            except TypeError:
                return fn()

    def resolve_endpoint_facts(self, *args, **kwargs) -> dict[str, Any]:
        fn = getattr(self._base, "resolve_endpoint_facts", None)
        if not callable(fn):
            return {}
        try:
            value = fn(*args, **kwargs)
        except TypeError:
            value = fn()
        return dict(value or {}) if isinstance(value, dict) else {}

    async def apreflight(self, request: LLMPreflightRequest) -> LLMPreflightAdvice:
        method = getattr(self._base, "apreflight", None)
        if callable(method):
            result = method(request)
            return await result if inspect.isawaitable(result) else result
        method = getattr(self._base, "preflight", None)
        if callable(method):
            result = method(request)
            if inspect.isawaitable(result):
                return await result
            return await asyncio.to_thread(lambda: result)
        return LLMPreflightAdvice(status=LLMPreflightStatus.READY)

    async def agenerate(self, request: CanonicalLLMRequest) -> CanonicalLLMOutcome:
        await self._runner._emit_progress(
            "llm_round_started",
            round=self._state.llm_round_count,
            tool_call_count=self._state.tool_call_count,
            tool_count=len(list(request.tools or [])),
        )
        restore_event_sink = self._install_llm_progress_sink()
        method = getattr(self._base, "agenerate", None)
        try:
            if callable(method):
                awaitable = method(request)
            else:
                sync_method = getattr(self._base, "generate")
                awaitable = asyncio.to_thread(sync_method, request)
            return await self._runner._await_with_progress_heartbeat(
                awaitable,
                phase="llm_round_waiting",
                round=self._state.llm_round_count,
                tool_call_count=self._state.tool_call_count,
            )
        finally:
            restore_event_sink()

    def _install_llm_progress_sink(self) -> Callable[[], None]:
        sentinel = object()
        try:
            previous = getattr(self._base, "event_sink")
        except Exception:
            previous = sentinel
        loop = asyncio.get_running_loop()

        def sink(event: dict[str, Any]) -> None:
            payload = dict(event or {})
            phase = str(payload.pop("phase", "") or "llm_endpoint_event")
            payload.setdefault("round", self._state.llm_round_count)
            payload.setdefault("tool_call_count", self._state.tool_call_count)

            def schedule() -> None:
                asyncio.create_task(self._runner._emit_progress(phase, **payload))

            loop.call_soon_threadsafe(schedule)

        try:
            setattr(self._base, "event_sink", sink)
        except Exception:
            return lambda: None

        def restore() -> None:
            with contextlib.suppress(Exception):
                if previous is sentinel:
                    delattr(self._base, "event_sink")
                else:
                    setattr(self._base, "event_sink", previous)

        return restore

    async def acompact_memory_structured(self, *args, **kwargs) -> dict[str, Any]:
        method = getattr(self._base, "acompact_memory_structured", None)
        if callable(method):
            result = method(*args, **kwargs)
            result = await result if inspect.isawaitable(result) else result
            return dict(result or {}) if isinstance(result, dict) else {}
        method = getattr(self._base, "compact_memory_structured", None)
        if callable(method):
            result = await asyncio.to_thread(method, *args, **kwargs)
            return dict(result or {}) if isinstance(result, dict) else {}
        source_text = str(args[0] if args else kwargs.get("source_text") or "").strip()
        return _fallback_minion_compaction_payload(source_text)

    async def asummarize_compaction(self, *args, **kwargs) -> str:
        method = getattr(self._base, "asummarize_compaction", None)
        if callable(method):
            result = method(*args, **kwargs)
            result = await result if inspect.isawaitable(result) else result
            return str(result or "").strip()
        method = getattr(self._base, "summarize_compaction", None)
        if callable(method):
            result = await asyncio.to_thread(method, *args, **kwargs)
            return str(result or "").strip()
        return ""


class _MinionMemoryServiceAdapter:
    def __init__(self, runner: "MinionRunner", state: "MinionAgentLoopState") -> None:
        self._runner = runner
        self._state = state

    def build_pack(self, request: MemoryPackRequest):
        return self._state.memory_service.build_pack(request)

    def build_compaction_source_text(self, *, target_input_budget: int) -> str:
        return self._runner._minion_compaction_source_text(self._state, target_input_budget=target_input_budget)

    def compact(self, request: MemoryCompactRequest):
        return self._state.memory_service.compact(request)

    def commit_l1(self, request: MemoryCommitRequest):
        return self._state.memory_service.commit_l1(request)

    def project_l2_entries(self, *args, **kwargs):
        method = getattr(self._state.memory_service, "project_l2_entries", None)
        if not callable(method):
            return None
        return method(*args, **kwargs)


class _MinionOutputPort:
    def __init__(self, runner: "MinionRunner") -> None:
        self._runner = runner

    async def queue_reply(self, envelope: ChannelEnvelope, text: str) -> str:
        _ = envelope
        await self._runner._emit("progress", {"phase": "reply", "summary": _preview_text(text, limit=500)})
        return f"minion_reply_{uuid4().hex[:12]}"

    async def queue_stream_event(self, envelope: ChannelEnvelope, event: Any) -> str:
        _ = envelope
        _ = event
        return f"minion_stream_{uuid4().hex[:12]}"

    async def abort_stream(self, response_handle: ResponseHandle, *, reason: str = "interrupted") -> None:
        _ = response_handle
        _ = reason

    async def queue_status(self, envelope: ChannelEnvelope, kind: str, *, payload: dict[str, Any] | None = None) -> str:
        _ = envelope
        await self._runner._emit("progress", {"phase": kind, "summary": _preview_text(json.dumps(payload or {}, ensure_ascii=False), limit=500)})
        return f"minion_status_{uuid4().hex[:12]}"

    async def queue_attachment(self, envelope: ChannelEnvelope, attachment: Any) -> str:
        _ = envelope
        _ = attachment
        return f"minion_attachment_{uuid4().hex[:12]}"


class _MinionExecutionRuntimeAdapter:
    def __init__(self, runner: "MinionRunner", state: "MinionAgentLoopState", continuation: TurnContinuation) -> None:
        self._runner = runner
        self._state = state
        self._continuation = continuation

    async def execute_tool_async(
        self,
        call: CanonicalToolCall,
        *,
        allow_tools: bool = True,
        budget: Any = None,
        turn_id: str | None = None,
    ) -> CanonicalToolResult:
        _ = allow_tools
        _ = budget
        _ = turn_id
        target_name = _effective_capability_name(call)
        index = len(self._continuation.pending_tool_results)
        await self._runner._emit_progress(
            "tool_call_started",
            round=self._state.llm_round_count,
            tool_call_index=index,
            tool_name=call.name,
            target_name=target_name,
            args_preview=_json_preview(call.args),
        )
        self._runner._append_debug_log(
            "tool_call_started",
            {
                "round": self._state.llm_round_count,
                "tool_call_index": index,
                "tool_name": call.name,
                "target_name": target_name,
                "args": dict(call.args),
            },
        )
        try:
            result = await self._runner._await_with_progress_heartbeat(
                self._runner._execute_allowed_tool(self._state.execution_runtime, call),
                phase="tool_call_waiting",
                round=self._state.llm_round_count,
                tool_call_index=index,
                tool_name=call.name,
                target_name=target_name,
            )
        except Exception as exc:
            self._runner._append_debug_log(
                "tool_call_failed",
                {
                    "round": self._state.llm_round_count,
                    "tool_call_index": index,
                    "tool_name": call.name,
                    "target_name": target_name,
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                },
            )
            await self._runner._emit_progress(
                "tool_call_failed",
                round=self._state.llm_round_count,
                tool_call_index=index,
                tool_name=call.name,
                target_name=target_name,
                error_type=exc.__class__.__name__,
                error=_preview_text(str(exc), limit=500),
            )
            raise
        self._state.tool_call_count += 1
        self._runner._append_debug_log(
            "tool_call_completed",
            {
                "round": self._state.llm_round_count,
                "tool_call_index": index,
                "tool_name": call.name,
                "target_name": target_name,
                "ok": bool(result.ok),
                "status": str(result.status or ""),
                "text": _tool_result_text(result),
                "structured": dict(result.structured or {}),
            },
        )
        await self._runner._emit_progress(
            "tool_call_completed",
            round=self._state.llm_round_count,
            tool_call_index=index,
            tool_name=call.name,
            target_name=target_name,
            ok=bool(result.ok),
            status=str(result.status or ""),
            text_preview=_preview_text(_tool_result_text(result)),
        )
        return result


@dataclass
class MinionAgentLoopState:
    execution_runtime: "MinionScopedExecutionRuntime"
    memory_service: MemoryService
    memory_l3: MockL3Plugin
    channel_envelope: ChannelEnvelope = field(
        default_factory=lambda: ChannelEnvelope(
            event=EventEnvelope(
                event_kind=EventKind.USER_MESSAGE,
                source_kind=SourceKind.MINION,
                payload={"text": ""},
            ),
            endpoint=EndpointConfig(endpoint_id="minion:unknown", channel_kind="stdio", binding_key="unknown"),
            response_handle=ResponseHandle(endpoint_id="minion:unknown"),
        )
    )
    tool_protocol_messages: list[dict[str, Any]] = field(default_factory=list)
    pending_assistant_tool_text: str = ""
    pending_assistant_provider_specific_fields: dict[str, Any] = field(default_factory=dict)
    pending_tool_call_batch: list[CanonicalToolCall] = field(default_factory=list)
    pending_tool_results: list[CanonicalToolResult] = field(default_factory=list)
    llm_round_count: int = 0
    tool_call_count: int = 0


@dataclass
class MinionRunner:
    runtime_root: Path
    pack: TaskContextPack
    minion_id: str
    run_id: str
    write_event: EventWriter
    read_decision: DecisionReader
    runtime_bundle: MinionRuntimeBundle | None = None
    blocked_summary: str = ""
    produced_artifacts: list[dict[str, Any]] = field(default_factory=list)
    memory_candidates: list[dict[str, Any]] = field(default_factory=list)
    shell_mutation_violations: list[dict[str, Any]] = field(default_factory=list)
    review_tool_evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    git_mutation_approved: bool = False
    web_research_usage: dict[str, int] = field(default_factory=dict)
    auto_accept_approvals: bool = False
    user_interaction: MinionUserInteractionPort | None = field(default=None, init=False, repr=False)
    _memory_l3: MockL3Plugin | None = field(default=None, init=False, repr=False)
    _memory_service: MemoryService | None = field(default=None, init=False, repr=False)
    _pending_control_messages: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _cancel_requested: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    async def run(self) -> int:
        bundle: MinionRuntimeBundle | None = None
        self._append_debug_log(
            "runner_started",
            {
                "goal": self.pack.goal,
                "instruction": self.pack.instruction,
                "allowed_capabilities": list(self.pack.allowed_capabilities),
            },
        )
        try:
            bundle = self.runtime_bundle or build_slim_minion_runtime(self.runtime_root, run_id=self.run_id)
            await self._emit(
                "phase_started",
                {
                    "phase": "accepted",
                    "summary": "minion accepted task context",
                    "prompt_scaffold_summary": _prompt_scaffold_summary(self._prompt_scaffold()),
                },
            )
            while True:
                await self._raise_if_cancel_requested()
                await self._emit(
                    "phase_started",
                    {
                        "phase": "milestone_started",
                        "summary": self._current_milestone_title(),
                        "milestone_index": self._current_milestone_index(),
                        "milestone_id": str(self._current_milestone().get("milestone_id") or ""),
                        "current_milestone": self._current_milestone(),
                    },
                )
                final_text = await self._run_agent_loop(bundle)
                await self._raise_if_cancel_requested()
                clarification_round = 0
                while not self.blocked_summary:
                    ask_user_question = _extract_ask_user_question_payload(final_text)
                    if not ask_user_question:
                        break
                    if clarification_round >= self._max_clarification_rounds():
                        self.blocked_summary = "planner asked too many clarification rounds"
                        break
                    clarification = await self._request_clarification(ask_user_question)
                    if not clarification:
                        self.blocked_summary = _ask_user_question_summary(ask_user_question)
                        break
                    self._apply_clarification_response(clarification)
                    clarification_round += 1
                    await self._emit_progress("clarification_applied", round=clarification_round)
                    final_text = await self._run_agent_loop(bundle)
                    await self._raise_if_cancel_requested()
                if self.blocked_summary:
                    blocked_checkpoint = {
                        "status": "blocked",
                        "milestone_index": self._current_milestone_index(),
                        "summary": self.blocked_summary,
                        **self._artifact_payload(),
                    }
                    terminal_payload = self._terminal_payload("blocked", self.blocked_summary)
                    await self._emit("checkpoint", blocked_checkpoint)
                    await self._emit("terminal", terminal_payload)
                    return 0
                checkpoint_payload = await self._complete_current_milestone(final_text)
                planner_repair_attempt = 0
                while self._should_retry_planner_artifact(checkpoint_payload, planner_repair_attempt):
                    planner_repair_attempt += 1
                    retry_note = self._planner_artifact_repair_note(checkpoint_payload, planner_repair_attempt)
                    await self._emit_progress(
                        "planner_artifact_repair_retry",
                        repair_attempt=planner_repair_attempt,
                        retry_limit=self._planner_artifact_repair_retry_limit(),
                        plan_validation=dict(checkpoint_payload.get("plan_validation") or {}),
                    )
                    final_text = await self._run_agent_loop(bundle, forced_retry_note=retry_note)
                    await self._raise_if_cancel_requested()
                    checkpoint_payload = await self._complete_current_milestone(final_text)
                if checkpoint_payload.get("status") not in {"completed", "claimed"}:
                    await self._emit("checkpoint", checkpoint_payload)
                    await self._emit("terminal", self._terminal_payload("blocked", checkpoint_payload.get("summary") or "milestone blocked"))
                    return 0
                current_index = self._current_milestone_index()
                await self._emit("checkpoint", checkpoint_payload)
                if checkpoint_payload.get("status") == "completed":
                    await self._emit("milestone_completed", self._milestone_completed_payload(final_text, checkpoint_payload))
                next_status, next_turn = await self._await_next_serial_module_turn(current_index)
                await self._raise_if_cancel_requested()
                if next_status == "next" and next_turn is not None:
                    self._apply_next_milestone_turn(next_turn, checkpoint_payload=checkpoint_payload)
                    continue
                if next_status == "repair" and next_turn is not None:
                    self._apply_next_milestone_turn(next_turn, checkpoint_payload={})
                    continue
                if next_status == "timeout":
                    summary = "manager did not acknowledge milestone checkpoint before continuation timeout"
                    await self._emit("terminal", self._terminal_payload("blocked", summary))
                    return 0
                if next_status == "blocked":
                    summary = str((next_turn or {}).get("summary") or "manager blocked serial milestone continuation")
                    await self._emit("terminal", self._terminal_payload("blocked", summary))
                    return 0
                await self._emit("terminal", self._terminal_payload("completed", final_text or "minion completed current milestone"))
                return 0
        except _MinionCooperativeCancel as cancel:
            with contextlib.suppress(Exception):
                await self._emit("terminal", self._cancel_terminal_payload(cancel.payload))
            return 0
        except Exception as exc:
            with contextlib.suppress(Exception):
                await self._emit(
                    "terminal",
                    {
                        "status": "failed",
                        "summary": f"minion runner failed: {exc.__class__.__name__}",
                        "error": str(exc),
                        "error_type": exc.__class__.__name__,
                        "task_lessons": [],
                        "system_lessons": [],
                    },
                )
            return 1
        finally:
            if bundle is not None:
                await bundle.close()
            self._append_debug_log("runner_stopped", {"blocked_summary": self.blocked_summary})

    async def _run_agent_loop(self, bundle: MinionRuntimeBundle, *, forced_retry_note: str = "") -> str:
        memory_l3, memory_service = self._runner_memory()
        workspace = dict(self.pack.workspace)
        workspace.setdefault("runtime_root", str(self.runtime_root))
        workspace.setdefault("run_id", self.run_id)
        workspace.setdefault("minion_id", self.minion_id)
        workspace.setdefault("minion_profile", self.pack.minion_profile)
        workspace.setdefault("work_order_id", self.pack.work_order_id)
        workspace.setdefault("goal", self.pack.instruction or self.pack.goal)
        if isinstance(self.pack.metadata, dict):
            workspace.setdefault("task_id", str(self.pack.metadata.get("task_id") or ""))
            for key in (
                "parent_work_order_id",
                "parent_module_id",
                "parent_module_name",
                "module_id",
                "module_name",
            ):
                value = str(self.pack.metadata.get(key) or "").strip()
                if value:
                    workspace.setdefault(key, value)
            if isinstance(self.pack.metadata.get("repair_context"), dict):
                workspace.setdefault("repair_context", dict(self.pack.metadata.get("repair_context") or {}))
            if isinstance(self.pack.metadata.get("requirements_brief"), dict):
                workspace.setdefault("requirements_brief", dict(self.pack.metadata.get("requirements_brief") or {}))
            if isinstance(self.pack.metadata.get("prompt_view"), dict):
                workspace.setdefault("prompt_view", dict(self.pack.metadata.get("prompt_view") or {}))
            if isinstance(self.pack.metadata.get("coder_work_order"), dict):
                coder_work_order = dict(self.pack.metadata.get("coder_work_order") or {})
                workspace.setdefault("coder_work_order", coder_work_order)
                order_metadata = coder_work_order.get("metadata")
                if isinstance(order_metadata, dict) and isinstance(order_metadata.get("module_dependency_context"), list):
                    workspace.setdefault(
                        "module_dependency_context",
                        [dict(item) for item in list(order_metadata.get("module_dependency_context") or []) if isinstance(item, dict)],
                    )
            if isinstance(self.pack.metadata.get("source_plan_ref"), dict):
                workspace.setdefault("source_plan_ref", dict(self.pack.metadata.get("source_plan_ref") or {}))
            if isinstance(self.pack.metadata.get("review_gate_ref"), dict):
                workspace.setdefault("review_gate_ref", dict(self.pack.metadata.get("review_gate_ref") or {}))
            if isinstance(self.pack.metadata.get("plan_revision_checklist"), list):
                workspace.setdefault(
                    "plan_revision_checklist",
                    [dict(item) for item in list(self.pack.metadata.get("plan_revision_checklist") or []) if isinstance(item, dict)],
                )
            if isinstance(self.pack.metadata.get("planner_work_order"), dict):
                workspace.setdefault("planner_work_order", dict(self.pack.metadata.get("planner_work_order") or {}))
            expected_plan_revision = _expected_planner_plan_revision(self.pack)
            if expected_plan_revision >= 0:
                workspace.setdefault("planner_plan_revision", expected_plan_revision)
        workspace.setdefault("review_tool_evidence_refs", self.review_tool_evidence_refs)
        workspace.setdefault("shell_mutation_violations", self.shell_mutation_violations)
        current_repair_attempt = self._current_repair_attempt_payload()
        if current_repair_attempt:
            workspace.setdefault("current_repair_attempt", current_repair_attempt)
        checkpoint_repair = (self.pack.metadata or {}).get("checkpoint_repair")
        if isinstance(checkpoint_repair, dict):
            workspace.setdefault("checkpoint_repair", dict(checkpoint_repair))
        active_gate_todo = (self.pack.metadata or {}).get("active_gate_todo")
        if isinstance(active_gate_todo, dict):
            workspace.setdefault("active_gate_todo", dict(active_gate_todo))
        if isinstance((self.pack.metadata or {}).get("review_target"), dict):
            review_target = dict((self.pack.metadata["review_target"] or {}))
            workspace.setdefault("review_target_run_id", str(review_target.get("run_id") or ""))
            workspace.setdefault("review_target_checkpoint_id", str(review_target.get("checkpoint_id") or ""))
            workspace.setdefault("review_target_commit_sha", str(review_target.get("commit_sha") or ""))
            workspace.setdefault("review_target_gate_kind", str(review_target.get("gate_kind") or ""))
            if isinstance(review_target.get("acceptance_criteria"), list):
                workspace.setdefault(
                    "review_target_acceptance_criteria",
                    [str(item) for item in list(review_target.get("acceptance_criteria") or []) if str(item or "").strip()],
                )
            if isinstance(review_target.get("acceptance_checklist"), list):
                workspace.setdefault(
                    "review_target_acceptance_checklist",
                    [dict(item) for item in list(review_target.get("acceptance_checklist") or []) if isinstance(item, dict)],
                )
            if isinstance(review_target.get("checkpoint_git"), dict):
                workspace.setdefault("review_target_checkpoint_git", dict(review_target.get("checkpoint_git") or {}))
            if isinstance(review_target.get("source_contract"), dict):
                workspace.setdefault("review_target_source_contract", dict(review_target.get("source_contract") or {}))
            if isinstance(review_target.get("gate_spec"), dict):
                workspace.setdefault("review_target_gate_spec", dict(review_target.get("gate_spec") or {}))
            if isinstance(review_target.get("module_contract"), dict):
                workspace.setdefault("review_target_module_contract", dict(review_target.get("module_contract") or {}))
        workspace.setdefault("current_milestone_index", self._current_milestone_index())
        workspace.setdefault("current_milestone_title", self._current_milestone_title())
        execution_runtime = MinionScopedExecutionRuntime(
            bundle.execution_runtime,
            self.pack.allowed_capabilities,
            workspace,
            produced_artifacts=self.produced_artifacts,
            memory_l3=memory_l3,
        )
        channel_envelope = _minion_task_envelope(self.pack, minion_id=self.minion_id, run_id=self.run_id)
        state = MinionAgentLoopState(
            execution_runtime=execution_runtime,
            memory_service=memory_service,
            memory_l3=memory_l3,
            channel_envelope=channel_envelope,
        )
        max_output_tokens = _resolve_minion_max_output_tokens(bundle.llm_runtime, self.pack)

        forced_retry_note = str(forced_retry_note or "").strip()

        def build_context(frame: AgentLoopFrame):
            retry_note = str(frame.retry_note or forced_retry_note or "")
            metadata = {
                "retry_note": retry_note,
            }
            return _minion_prompt_context(self.pack, metadata=metadata)

        def build_commit_payload(final_reply: str, observations: list[Any], reply_texts: list[str]) -> L1CommitPayload:
            _ = reply_texts
            transcript = [
                L1TranscriptMessage(
                    role="user",
                    content=_minion_primary_input(state.channel_envelope),
                    kind=L1MessageKind.USER_REQUEST,
                ),
                L1TranscriptMessage(
                    role="assistant",
                    content=final_reply or "minion completed",
                    kind=L1MessageKind.ASSISTANT_REPLY,
                ),
            ]
            return L1CommitPayload(turn_id=self.run_id, transcript=transcript, tool_observations=list(observations))

        turn_id = f"{self.run_id}:m{self._current_milestone_index()}"
        program = agent_turn_program(
            turn_id=turn_id,
            build_assembly_context=build_context,
            render_final_text=lambda outcome: str(getattr(outcome, "text", "") or "") if outcome is not None else "",
            build_commit_payload=build_commit_payload,
            max_output_tokens=max_output_tokens,
            build_retry_note=self._build_minion_retry_note,
        )
        continuation = TurnContinuation(
            turn_id=turn_id,
            channel_envelope=state.channel_envelope,
            program=program,
            correlation_id=self.run_id,
            control_scope_key=f"minion:{self.run_id}",
            turn_settings_snapshot={"prompt_log_enabled": bool((self.pack.metadata or {}).get("prompt_log_enabled"))},
            tool_protocol_messages=state.tool_protocol_messages,
        )
        executor = self._build_minion_turn_executor(bundle, state, continuation)
        current: EffectResult | None = None
        while True:
            await self._raise_if_cancel_requested()
            try:
                yielded = program.send(current) if current is not None else next(program)
            except StopIteration as completed:
                outcome = completed.value
                if not isinstance(outcome, TurnOutcome):
                    raise RuntimeError("minion agent loop ended without a turn outcome")
                if not self.blocked_summary:
                    await self._emit_progress(
                        "milestone_finalizing",
                        round=state.llm_round_count,
                        tool_call_count=state.tool_call_count,
                    )
                await self._commit_minion_l1(state, outcome.commit_payload.transcript)
                self.memory_candidates = _memory_candidates_from_l3(state.memory_l3)
                return outcome.final_reply
            current = await self._execute_minion_agent_effect(
                executor,
                continuation,
                state,
                yielded,
                max_output_tokens=max_output_tokens,
            )
            await self._raise_if_cancel_requested()

    def _runner_memory(self) -> tuple[MockL3Plugin, MemoryService]:
        if self._memory_l3 is not None and self._memory_service is not None:
            return self._memory_l3, self._memory_service
        memory_l3 = MockL3Plugin(provider_id=f"minion_run_{self.run_id}_l3")
        memory_service = MemoryService(
            l3_selector=L3ProviderSelector(
                resolver=lambda provider_id: memory_l3,
                active_provider_id=memory_l3.provider_id,
            )
        )
        memory_l3.service = memory_service
        self._memory_l3 = memory_l3
        self._memory_service = memory_service
        return memory_l3, memory_service

    async def _await_next_serial_module_turn(self, completed_milestone_index: int) -> tuple[str, dict[str, Any] | None]:
        metadata = dict(self.pack.metadata or {})
        module_execution = dict(metadata.get("module_execution") or {})
        is_serial = str(module_execution.get("mode") or "") in SERIAL_MILESTONE_MODES
        review_required = self._requires_checkpoint_review_gate()
        if not is_serial and not review_required:
            return "not_serial", None
        if is_serial and not bool(module_execution.get("auto_advance")):
            return "complete", None
        timeout = self._manager_turn_timeout_seconds()
        message = await self._read_manager_control(timeout)
        if not message:
            return "timeout", None
        message_type = str(message.get("type") or "").strip()
        if message_type in {"cancel_requested", "cancel"}:
            self._remember_cancel_request(dict(message.get("payload") or message))
            return "cancel", dict(message.get("payload") or message)
        if message_type == "next_turn" and isinstance(message.get("turn"), dict):
            turn = dict(message.get("turn") or {})
            current = dict(turn.get("current_milestone") or {})
            next_index = _coerce_int(current.get("milestone_index"), default=completed_milestone_index)
            if next_index <= completed_milestone_index:
                return "blocked", {"summary": "manager sent a stale milestone turn"}
            return "next", turn
        if message_type == "repair_turn" and isinstance(message.get("turn"), dict):
            turn = dict(message.get("turn") or {})
            current = dict(turn.get("current_milestone") or {})
            repair_index = _coerce_int(current.get("milestone_index"), default=completed_milestone_index)
            if repair_index != completed_milestone_index:
                return "blocked", {"summary": "manager sent repair for a different milestone"}
            return "repair", turn
        if message_type == "complete":
            return "complete", dict(message.get("completion") or {})
        if message_type == "blocked":
            return "blocked", dict(message.get("payload") or {})
        return "timeout", None

    async def _read_manager_control(self, timeout: float | None = None) -> dict[str, Any] | None:
        if self._pending_control_messages:
            message = self._pending_control_messages.pop(0)
        else:
            message = await self.read_decision(timeout)
        if not message:
            return None
        message_type = str(message.get("type") or "").strip()
        if message_type in {"cancel_requested", "cancel"}:
            raise _MinionCooperativeCancel(self._remember_cancel_request(dict(message.get("payload") or message)))
        return message

    async def _poll_cancel_requested(self) -> dict[str, Any]:
        if self._cancel_requested:
            return dict(self._cancel_requested)
        while True:
            message = await self.read_decision(0.001)
            if not message:
                return {}
            message_type = str(message.get("type") or "").strip()
            if message_type in {"cancel_requested", "cancel"}:
                return self._remember_cancel_request(dict(message.get("payload") or message))
            self._pending_control_messages.append(dict(message))
            return {}

    async def _raise_if_cancel_requested(self) -> None:
        cancel = await self._poll_cancel_requested()
        if cancel:
            raise _MinionCooperativeCancel(cancel)

    def _remember_cancel_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._cancel_requested:
            return dict(self._cancel_requested)
        reason = str(payload.get("reason") or payload.get("summary") or "cooperative_cancel_requested").strip()
        summary = str(payload.get("summary") or "minion cancellation requested").strip()
        self._cancel_requested = {
            **dict(payload),
            "reason": reason,
            "summary": summary,
            "status": str(payload.get("status") or "killed"),
        }
        return dict(self._cancel_requested)

    def _manager_turn_timeout_seconds(self) -> float:
        raw = (self.pack.metadata or {}).get("manager_turn_timeout_seconds")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = _DEFAULT_MANAGER_TURN_TIMEOUT_SECONDS
        return max(0.1, min(_MAX_MANAGER_TURN_TIMEOUT_SECONDS, value))

    def _apply_next_milestone_turn(self, turn: dict[str, Any], *, checkpoint_payload: dict[str, Any]) -> None:
        self.produced_artifacts.clear()
        self.blocked_summary = ""
        self.pack = apply_minion_turn_to_pack(self.pack, turn, checkpoint_payload=checkpoint_payload)

    def _milestone_completed_payload(self, final_text: str, checkpoint_payload: dict[str, Any]) -> dict[str, Any]:
        payload = self._terminal_payload("completed", final_text or "minion completed current milestone")
        for key in (
            "task_id",
            "module_id",
            "milestone_index",
            "milestone_id",
            "commit_sha",
            "git_commit",
            "evidence",
            "plan_ref",
            "plan_validation",
        ):
            if key in checkpoint_payload:
                payload[key] = checkpoint_payload[key]
        payload.setdefault("milestone_index", self._current_milestone_index())
        payload.setdefault("milestone_id", str(self._current_milestone().get("milestone_id") or ""))
        return payload

    def _build_minion_retry_note(self, outcome: Any, observations: list[Any], retry_count: int) -> str:
        if self.blocked_summary:
            return ""
        if str(getattr(outcome, "finish_reason", "") or "") == LLMFinishReason.ERROR:
            return ""
        tools_available = bool(self.pack.allowed_capabilities)
        if not tools_available:
            return ""
        if observations and self._needs_git_completion_retry():
            if retry_count >= self._git_completion_retry_limit():
                return ""
            return self._git_completion_retry_note()
        if retry_count > 0:
            return ""
        if observations:
            return ""
        if not self._requires_first_tool_call():
            return ""
        _ = outcome
        return (
            "You have not used any capability yet. This milestone requires executable evidence. "
            "Use one listed capability now to inspect, research, read, write, or verify the task before completing."
        )

    def _needs_git_completion_retry(self) -> bool:
        completion_policy = self._completion_policy()
        if str(completion_policy.get("evidence") or "").strip().lower() != "git_commit":
            return False
        metadata = self.pack.metadata if isinstance(self.pack.metadata, dict) else {}
        if bool(metadata.get("allow_text_only_completion") or completion_policy.get("allow_artifact_evidence")):
            return False
        return not self._completion_evidence_present()

    def _git_completion_retry_limit(self) -> int:
        metadata = self.pack.metadata if isinstance(self.pack.metadata, dict) else {}
        raw = metadata.get("git_completion_retry_limit")
        return max(1, min(5, _coerce_int(raw, default=3)))

    def _git_completion_retry_note(self) -> str:
        checkpoint = inspect_milestone_checkpoint(
            Path(str((self.pack.workspace or {}).get("repo_path") or ".")),
            base_sha=str((self.pack.workspace or {}).get("base_sha") or ""),
        )
        status = str(checkpoint.get("status") or "").strip()
        if status == "uncommitted_changes":
            return (
                "The milestone has uncommitted workspace changes and is not complete until a structured checkpoint "
                "commit exists. Do not answer with final text. If implementation and verification are complete, call "
                "`op_minion_checkpoint_commit` now. Do not use op_exec_shell git add/commit; Pal needs the structured result "
                "from `op_minion_checkpoint_commit` for manager reporting."
            )
        return (
            "The milestone is not complete yet: no minion checkpoint commit exists. Use the listed file/workspace "
            "capabilities to make and verify the required change, then call `op_minion_checkpoint_commit` to create "
            "the milestone checkpoint commit. Do not stop with a report-only response. If completion is truly "
            "impossible, report a concrete blocker that explains which required input, permission, file, or contract "
            "is missing."
        )

    def _should_retry_planner_artifact(self, checkpoint_payload: dict[str, Any], repair_attempt: int) -> bool:
        if not self._requires_planner_plan_artifact_validation():
            return False
        if repair_attempt >= self._planner_artifact_repair_retry_limit():
            return False
        if str(checkpoint_payload.get("status") or "").strip().lower() != "blocked":
            return False
        if checkpoint_payload.get("ask_user_question"):
            return False
        allowed = {str(item) for item in self.pack.allowed_capabilities}
        if not {
            "op_minion_artifact_write",
            "op_minion_plan_finalize",
            "op_minion_plan_validate_and_submit_for_review",
        }.intersection(allowed):
            return False
        validation = dict(checkpoint_payload.get("plan_validation") or {})
        validation_status = str(validation.get("status") or "").strip().lower()
        return validation_status in {"invalid", "draft"}

    def _planner_artifact_repair_retry_limit(self) -> int:
        metadata = self.pack.metadata if isinstance(self.pack.metadata, dict) else {}
        raw = metadata.get("planner_artifact_repair_retry_limit")
        return max(0, min(5, _coerce_int(raw, default=2)))

    def _planner_artifact_repair_note(self, checkpoint_payload: dict[str, Any], repair_attempt: int) -> str:
        validation = dict(checkpoint_payload.get("plan_validation") or {})
        errors = [str(item) for item in list(validation.get("errors") or []) if str(item).strip()]
        error_text = "\n".join(f"- {item}" for item in errors) or f"- {checkpoint_payload.get('summary') or 'plan validation failed'}"
        return (
            f"Planner repair attempt {repair_attempt}/{self._planner_artifact_repair_retry_limit()}.\n"
            "The previous planner output is not dispatchable. Do not finish with chat text and do not ask the user "
            "unless the missing information is truly not discoverable from the task context.\n\n"
            "Validation errors:\n"
            f"{error_text}\n\n"
            "Repair by using the plan builder tools on the current draft: use `plan_read`, `plan_find`, and node-specific "
            "`plan_update_*`/`plan_delete_*`/`plan_replace_*` tools when a local repair is possible. Rebuild with "
            "`plan_begin` only if no editable draft exists. Close every milestone and module, then call "
            "`plan_validate_and_submit_for_review`. Use `plan_validate` separately only while debugging a local draft. "
            "Do not hand-write JSON."
        )

    def _build_minion_turn_executor(
        self,
        bundle: MinionRuntimeBundle,
        state: MinionAgentLoopState,
        continuation: TurnContinuation,
    ) -> TurnExecutor:
        llm_runtime = _MinionLLMRuntimeAdapter(self, bundle.llm_runtime, state)
        memory_service = _MinionMemoryServiceAdapter(self, state)
        output_port = _MinionOutputPort(self)
        execution_runtime = _MinionExecutionRuntimeAdapter(self, state, continuation)
        context = _MinionTurnContext(
            execution_runtime=execution_runtime,
            port_registry={
                "llm:llm": llm_runtime,
                "memory:memory": memory_service,
                "agent_io:output": output_port,
            },
            turn_event_bus=TurnEventBus(),
        )
        turn_state = _MinionTurnState()
        turn_manager = _MinionTurnManager()

        def build_canonical_prompt(
            assembly_context: PromptAssemblyContext,
            *,
            max_output_tokens: int = 1024,
            model_hint: str | None = None,
        ) -> CanonicalLLMRequest:
            return CanonicalLLMRequest(
                messages=build_minion_prompt_messages(
                    scaffold=self._prompt_scaffold(),
                    channel_envelope=state.channel_envelope,
                    memory_text=self._render_minion_memory_context(state),
                    retry_note=str((assembly_context.metadata or {}).get("retry_note") or ""),
                    tool_protocol_messages=state.tool_protocol_messages,
                    include_tool_protocol=False,
                ),
                max_output_tokens=max_output_tokens,
                model_hint=model_hint,
                tools=[],
                metadata=_minion_llm_request_metadata(self.pack, self.run_id),
            )

        return TurnExecutor(
            context,
            turn_state,
            turn_manager,
            call_port_async=self._call_port_async,
            build_canonical_prompt=build_canonical_prompt,
            debug_log_prompt=lambda _continuation, request: self._debug_log_minion_llm_request(state, request),
            debug_log_outcome=lambda _continuation, outcome: self._debug_log_minion_llm_outcome(state, outcome),
            debug_log_reply=lambda _continuation, text: self._append_debug_log("reply", {"text": str(text or "")}),
            build_llm_tool_contracts=lambda: _llm_tools_for_allowed(state.execution_runtime, self.pack.allowed_capabilities),
            handle_failure_async=_minion_noop_failure_handler,
            render_failure_feedback_text=lambda feedback: str(feedback or ""),
            should_enter_failure_flow_for_tool_result=lambda _tool_result: False,
            handle_llm_provider_errors=False,
        )

    async def _execute_minion_agent_effect(
        self,
        executor: TurnExecutor,
        continuation: TurnContinuation,
        state: MinionAgentLoopState,
        effect: Any,
        *,
        max_output_tokens: int,
    ) -> EffectResult:
        if isinstance(effect, LLMRequestEffect):
            preflight = self._preflight_minion_llm_round(state)
            if preflight is not None:
                return preflight
        if isinstance(effect, MemoryCompactEffect) and effect.profile_override is None:
            effect = replace(effect, profile_override=CompactionProfile.MINION)
        before_protocol_len = len(state.tool_protocol_messages)
        result = await executor.execute_turn_effect_async(continuation, effect)
        self._sync_minion_state_from_continuation(state, continuation)
        if isinstance(effect, MemoryCompactEffect):
            await self._emit_progress(
                "memory_compacted",
                round=state.llm_round_count,
                summary=_preview_text(getattr(result.payload, "summary", ""), limit=500),
            )
        if isinstance(effect, LLMRequestEffect):
            result = await self._postprocess_minion_llm_round(state, result)
        if isinstance(effect, ToolCallEffect) and len(state.tool_protocol_messages) > before_protocol_len:
            await self._commit_minion_l1(
                state,
                _tool_protocol_transcript(state.tool_protocol_messages[before_protocol_len:]),
            )
        return result

    def _preflight_minion_llm_round(self, state: MinionAgentLoopState) -> EffectResult | None:
        if self.blocked_summary:
            return EffectResult(status=RuntimeStatus.OK, payload=CanonicalLLMOutcome(text=self.blocked_summary))
        max_rounds = _optional_positive_int(self.pack.metadata.get("max_tool_rounds") if isinstance(self.pack.metadata, dict) else None)
        if max_rounds is None or state.llm_round_count < max_rounds:
            state.llm_round_count += 1
            return None
        if self._completion_evidence_present() or self._artifact_completion_evidence_present():
            outcome = CanonicalLLMOutcome(text="milestone produced completion evidence")
        else:
            self.blocked_summary = f"minion reached explicit max_tool_rounds={max_rounds} before completing the current milestone"
            outcome = CanonicalLLMOutcome(text=self.blocked_summary)
        return EffectResult(status=RuntimeStatus.OK, payload=outcome)

    async def _postprocess_minion_llm_round(self, state: MinionAgentLoopState, result: EffectResult) -> EffectResult:
        outcome = result.payload
        finish_reason = str(getattr(outcome, "finish_reason", "") or "")
        if finish_reason == LLMFinishReason.ERROR:
            if self._completion_evidence_present() or self._artifact_completion_evidence_present():
                outcome = CanonicalLLMOutcome(
                    text=self._completion_evidence_fallback_text(str(getattr(outcome, "text", "") or "")),
                    finish_reason=LLMFinishReason.STOP,
                )
                finish_reason = str(LLMFinishReason.STOP)
                result = EffectResult(status=RuntimeStatus.OK, payload=outcome)
            else:
                self.blocked_summary = str(getattr(outcome, "text", "") or "LLM generation failed")
        elif _is_truncation_finish_reason(finish_reason):
            partial_text = str(getattr(outcome, "text", "") or "").strip()
            if partial_text:
                await self._persist_text_deliverable_if_needed(
                    partial_text,
                    partial=True,
                    truncation_reason=finish_reason,
                )
            self.blocked_summary = self._truncated_output_blocked_summary(finish_reason)
        await self._emit_progress(
            "llm_round_completed",
            round=state.llm_round_count,
            finish_reason=finish_reason,
            tool_call_count=state.tool_call_count,
            tool_calls=[_tool_call_summary(item) for item in list(getattr(outcome, "tool_calls", []) or [])],
            text_preview=_preview_text(str(getattr(outcome, "text", "") or "")),
        )
        return result

    def _sync_minion_state_from_continuation(self, state: MinionAgentLoopState, continuation: TurnContinuation) -> None:
        state.tool_protocol_messages = continuation.tool_protocol_messages
        state.pending_assistant_tool_text = continuation.pending_assistant_tool_text
        state.pending_assistant_provider_specific_fields = dict(
            getattr(continuation, "pending_assistant_provider_specific_fields", {}) or {}
        )
        state.pending_tool_call_batch = list(continuation.pending_tool_call_batch)
        state.pending_tool_results = list(continuation.pending_tool_results)

    def _debug_log_minion_llm_request(self, state: MinionAgentLoopState, request: CanonicalLLMRequest) -> None:
        self._append_debug_log(
            "llm_request",
            {
                "round": state.llm_round_count,
                "messages": request.messages,
                "tools": request.tools,
                "metadata": request.metadata,
            },
        )

    def _debug_log_minion_llm_outcome(self, state: MinionAgentLoopState, outcome: Any) -> None:
        self._append_debug_log(
            "llm_outcome",
            {
                "round": state.llm_round_count,
                "finish_reason": str(getattr(outcome, "finish_reason", "") or ""),
                "response_mode": str(getattr(outcome, "response_mode", "") or ""),
                "tool_calls": [_tool_call_summary(item) for item in list(getattr(outcome, "tool_calls", []) or [])],
                "reasoning_text": str(getattr(outcome, "reasoning_text", "") or ""),
                "text": str(getattr(outcome, "text", "") or "").strip(),
            },
        )

    async def _call_port_async(self, port: Any, async_name: str, sync_name: str, *args: Any, **kwargs: Any) -> Any:
        async_method = getattr(port, async_name, None)
        if callable(async_method):
            result = async_method(*args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
        sync_method = getattr(port, sync_name)
        return await asyncio.to_thread(sync_method, *args, **kwargs)

    async def _commit_minion_l1(self, state: MinionAgentLoopState, transcript: list[L1TranscriptMessage]) -> None:
        if not transcript:
            return
        with contextlib.suppress(Exception):
            state.memory_service.commit_l1(MemoryCommitRequest(turn_id=self.run_id, transcript=transcript))

    def _render_minion_memory_context(self, state: MinionAgentLoopState) -> str:
        pack = state.memory_service.build_pack(MemoryPackRequest(turn_kind="minion", work_order_id=self.pack.work_order_id))
        parts: list[str] = []
        summary_text = self._render_minion_current_summary(pack.current_summary)
        if summary_text:
            parts.append(f"Current summary:\n{summary_text}")
        entries = [entry for entry in list(pack.l2_working_memory or []) if str(entry.entry_id) != "memory.summary"]
        if entries:
            lines = [f"- {entry.kind}:{entry.title or entry.entry_id}: {entry.summary}" for entry in entries[:8]]
            parts.append("Working memory:\n" + "\n".join(lines))
        records = list(getattr(state.memory_l3, "records", []) or [])
        if records:
            lines = [f"- {record.get('document_kind')}:{record.get('title')}: {record.get('summary')}" for record in records[:8]]
            parts.append("Candidate experience records:\n" + "\n".join(lines))
        return "\n\n".join(parts).strip()

    @staticmethod
    def _render_minion_current_summary(entry: Any) -> str:
        if entry is None:
            return ""
        summary = str(getattr(entry, "summary", "") or "").strip()
        rendered = str(getattr(entry, "rendered", "") or "").strip()
        payload = getattr(entry, "payload", None)
        if is_compaction_payload(payload):
            return render_compact_context_for_llm(summary=summary, payload=dict(payload or {}))
        return rendered or summary

    def _minion_compaction_source_text(self, state: MinionAgentLoopState, *, target_input_budget: int) -> str:
        parts: list[str] = []
        with contextlib.suppress(Exception):
            parts.append(
                "[Task Assignment Snapshot]\n"
                + json.dumps(
                    _prompt_scaffold_summary(self._prompt_scaffold()),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        primary_input = _minion_primary_input(state.channel_envelope)
        if primary_input:
            parts.append("[Task Primary Input]\n" + primary_input)
        existing = state.memory_service.build_compaction_source_text(target_input_budget=target_input_budget)
        if existing:
            parts.append("[Prior Task Memory]\n" + existing)
        if state.tool_protocol_messages:
            rendered = []
            for message in state.tool_protocol_messages:
                role = str(message.get("role") or "")
                content = str(message.get("content") or "")
                if message.get("tool_calls"):
                    content += "\n" + json.dumps(message.get("tool_calls"), ensure_ascii=False, sort_keys=True)
                rendered.append(f"{role}: {content}")
            parts.append("[Current Task Trajectory]\n" + "\n".join(rendered))
        if not parts:
            parts.append(_minion_primary_input(state.channel_envelope))
        raw = "\n\n".join(parts).strip()
        return raw[: max(256, target_input_budget or 4096)]

    def _requires_first_tool_call(self) -> bool:
        if bool((self.pack.metadata or {}).get("allow_text_only_completion")):
            return False
        completion_policy = self._completion_policy()
        if "requires_capability_evidence" in completion_policy:
            return bool(completion_policy.get("requires_capability_evidence")) and bool(self.pack.allowed_capabilities)
        return str(completion_policy.get("evidence") or "").strip().lower() == "git_commit" and bool(self.pack.allowed_capabilities)

    async def _execute_allowed_tool(self, execution_runtime: "MinionScopedExecutionRuntime", tool_call: CanonicalToolCall) -> CanonicalToolResult:
        tool_call = self._canonicalize_allowed_tool_call(tool_call)
        tool_call = self._tool_call_with_minion_defaults(tool_call)
        target_name = _effective_capability_name(tool_call)
        allowed = set(str(item) for item in self.pack.allowed_capabilities)
        if is_minion_capability_denied(tool_call.name) or is_minion_capability_denied(target_name):
            self.blocked_summary = f"capability is denied by minion policy: {target_name}"
            return CanonicalToolResult(
                name=tool_call.name,
                ok=False,
                text="capability is denied by minion policy",
                structured={"reason": "capability_denied_by_minion_policy", "capability": target_name},
                call_id=tool_call.call_id,
                llm_text="capability is denied by minion policy",
                status=RuntimeStatus.ERROR,
            )
        if tool_call.name not in allowed or target_name not in allowed:
            self.blocked_summary = f"capability is not allowed for this minion run: {target_name}"
            return CanonicalToolResult(
                name=tool_call.name,
                ok=False,
                text="capability is not allowed for this minion run",
                structured={"reason": "capability_not_allowed", "capability": target_name},
                call_id=tool_call.call_id,
                llm_text="capability is not allowed for this minion run",
                status=RuntimeStatus.ERROR,
            )
        policy_error = self._runner_owned_git_command_error(target_name, tool_call)
        if policy_error:
            return CanonicalToolResult(
                name=tool_call.name,
                ok=False,
                text=policy_error,
                structured={"reason": "use_checkpoint_commit_capability", "capability": target_name},
                call_id=tool_call.call_id,
                llm_text=policy_error,
                status=RuntimeStatus.ERROR,
            )
        policy_error = self._read_only_git_command_error(target_name, tool_call)
        if policy_error:
            return CanonicalToolResult(
                name=tool_call.name,
                ok=False,
                text=policy_error,
                structured={"reason": "read_only_repo_git_policy", "capability": target_name},
                call_id=tool_call.call_id,
                llm_text=policy_error,
                status=RuntimeStatus.ERROR,
            )
        policy_error = self._read_only_shell_command_error(target_name, tool_call)
        if policy_error:
            return CanonicalToolResult(
                name=tool_call.name,
                ok=False,
                text=policy_error,
                structured={"reason": "read_only_repo_shell_cwd_policy", "capability": target_name},
                call_id=tool_call.call_id,
                llm_text=policy_error,
                status=RuntimeStatus.ERROR,
            )
        git_mutation_approval_handled = False
        if await self._requires_git_mutation_approval(target_name, tool_call):
            decision = await self._request_approval(target_name, tool_call)
            if decision != "accept":
                self.blocked_summary = f"approval {decision or 'timeout'} for {target_name}"
                return CanonicalToolResult(
                    name=tool_call.name,
                    ok=False,
                    text=f"approval {decision or 'timeout'}",
                    structured={"reason": "approval_not_accepted", "decision": decision or "timeout", "capability": target_name},
                    call_id=tool_call.call_id,
                    llm_text=f"approval {decision or 'timeout'}",
                    status=RuntimeStatus.ERROR,
                )
            self.git_mutation_approved = True
            git_mutation_approval_handled = True
        approval_handled = git_mutation_approval_handled
        if not approval_handled and await self._requires_approval(target_name, tool_call):
            decision = await self._request_approval(target_name, tool_call)
            if decision != "accept":
                self.blocked_summary = f"approval {decision or 'timeout'} for {target_name}"
                return CanonicalToolResult(
                    name=tool_call.name,
                    ok=False,
                    text=f"approval {decision or 'timeout'}",
                    structured={"reason": "approval_not_accepted", "decision": decision or "timeout", "capability": target_name},
                    call_id=tool_call.call_id,
                    llm_text=f"approval {decision or 'timeout'}",
                    status=RuntimeStatus.ERROR,
                )
            approval_handled = True
        if not approval_handled and await self._requires_web_research_approval(target_name, tool_call):
            status = self._web_research_budget_status(target_name)
            used = status["used"] if status else 0
            budget = status["budget"] if status else 0
            decision = await self._request_approval(
                target_name,
                tool_call,
                approval_kind="web_research_budget",
                title="Minion web research budget",
                risk="medium",
                impact="Minion used the included web research budget for this run and requested permission before another web call.",
                metadata={
                    "web_research_budget": budget,
                    "web_research_used": used,
                    "web_research_budget_key": str(status.get("key") or "") if status else "",
                },
            )
            if decision != "accept":
                self.blocked_summary = f"approval {decision or 'timeout'} for {target_name}"
                return CanonicalToolResult(
                    name=tool_call.name,
                    ok=False,
                    text=f"approval {decision or 'timeout'}",
                    structured={"reason": "approval_not_accepted", "decision": decision or "timeout", "capability": target_name},
                    call_id=tool_call.call_id,
                    llm_text=f"approval {decision or 'timeout'}",
                    status=RuntimeStatus.ERROR,
                )
        before_snapshot = {} if self._sandboxed() else self._shell_audit_snapshot(target_name)
        result = await execution_runtime.execute_tool_async(tool_call, allow_tools=True, turn_id=self.run_id)
        self._record_web_research_usage(target_name)
        violation = self._record_shell_audit_violation(target_name, tool_call, before_snapshot)
        if violation:
            result = _tool_result_with_shell_mutation_violation(result, violation)
        self._record_review_tool_evidence(target_name, tool_call, result)
        return result

    def _sandboxed(self) -> bool:
        return minion_sandbox_is_enabled(self.pack) or os.environ.get("PAL_MINION_SANDBOXED") == "1"

    def _canonicalize_allowed_tool_call(self, tool_call: CanonicalToolCall) -> CanonicalToolCall:
        name = self._resolve_allowed_capability_name(tool_call.name)
        args = dict(tool_call.args or {})
        if name == "op_tool_call":
            for key in ("name", "capability", "tool"):
                if str(args.get(key) or "").strip():
                    args[key] = self._resolve_allowed_capability_name(args.get(key))
                    break
        if name == tool_call.name and args == dict(tool_call.args or {}):
            return tool_call
        return CanonicalToolCall(name=name, args=args, call_id=tool_call.call_id)

    def _resolve_allowed_capability_name(self, name: object) -> str:
        raw = str(name or "").strip()
        if not raw:
            return ""
        allowed = [str(item).strip() for item in self.pack.allowed_capabilities if str(item).strip()]
        if raw in allowed:
            return raw
        matches = [canonical for canonical in allowed if llm_tool_name(canonical) == raw]
        return matches[0] if len(matches) == 1 else raw

    def _tool_call_with_minion_defaults(self, tool_call: CanonicalToolCall) -> CanonicalToolCall:
        if not _is_shell_capability_name(_effective_capability_name(tool_call)):
            return tool_call
        effective_args = _effective_tool_args(tool_call)
        cwd = str(effective_args.get("cwd") or "").strip()
        workdir = str(effective_args.get("workdir") or "").strip()
        default_cwd = self._default_shell_cwd()
        if cwd:
            return tool_call
        if workdir:
            effective_args["cwd"] = workdir
        elif default_cwd:
            effective_args["cwd"] = default_cwd
        else:
            return tool_call
        if tool_call.name == "op_tool_call":
            args = dict(tool_call.args or {})
            args["args"] = effective_args
            return CanonicalToolCall(name=tool_call.name, args=args, call_id=tool_call.call_id)
        return CanonicalToolCall(name=tool_call.name, args=effective_args, call_id=tool_call.call_id)

    def _default_shell_cwd(self) -> str:
        workspace = dict(self.pack.workspace or {})
        workspace_policy = dict(workspace.get("workspace_policy") or {})
        if str(workspace_policy.get("mode") or "").strip().lower() == "read_only_repo":
            for key in ("repo_path", "review_scratch_dir", "review_scratch_repo_path"):
                value = str(workspace.get(key) or "").strip()
                if value:
                    return value
            return ""
        for key in ("repo_path", "task_repo_path", "target_repo_path"):
            value = str(workspace.get(key) or "").strip()
            if value:
                return value
        return ""

    def _runner_owned_git_command_error(self, target_name: str, tool_call: CanonicalToolCall) -> str:
        if not (_is_git_capability_name(target_name) or _is_shell_capability_name(target_name)):
            return ""
        completion_policy = self._completion_policy()
        if str(completion_policy.get("evidence") or "").strip().lower() != "git_commit":
            return ""
        cmd = str(_effective_tool_args(tool_call).get("cmd") or "").strip()
        if not _git_command_is_mutating(cmd):
            return ""
        return (
            "Do not run git add, git commit, git reset, checkout/switch, clean, merge, rebase, tag, or push through "
            "the git capability in this minion workspace. Use `op_minion_checkpoint_commit` for milestone checkpoint "
            "commits so Pal can record structured commit evidence."
        )

    def _read_only_git_command_error(self, target_name: str, tool_call: CanonicalToolCall) -> str:
        if not _is_git_capability_name(target_name):
            return ""
        workspace_policy = dict((self.pack.workspace or {}).get("workspace_policy") or {})
        if str(workspace_policy.get("mode") or "").strip().lower() != "read_only_repo":
            return ""
        cmd = str(_effective_tool_args(tool_call).get("cmd") or "").strip()
        if not _git_command_is_mutating(cmd):
            return ""
        return "read_only_repo git capability is for inspection only; submit a blocking finding or use a dedicated repair workspace for mutations."

    def _read_only_shell_command_error(self, target_name: str, tool_call: CanonicalToolCall) -> str:
        if not _is_shell_capability_name(target_name):
            return ""
        workspace = dict(self.pack.workspace or {})
        workspace_policy = dict(workspace.get("workspace_policy") or {})
        if str(workspace_policy.get("mode") or "").strip().lower() != "read_only_repo":
            return ""
        allowed_roots = [
            str(workspace.get(key) or "").strip()
            for key in ("repo_path", "review_scratch_dir", "review_scratch_repo_path")
            if str(workspace.get(key) or "").strip()
        ]
        if not allowed_roots:
            return ""
        cwd = str(_effective_tool_args(tool_call).get("cwd") or "").strip()
        if not cwd:
            return ""
        if any(_path_is_relative_to(Path(cwd), Path(root)) for root in allowed_roots):
            return ""
        return "read_only_repo shell commands must run inside workspace.repo_path or review_scratch_dir; use those cwd values for reviewer commands."

    def _shell_audit_snapshot(self, target_name: str) -> dict[str, Any]:
        if not _is_shell_capability_name(target_name):
            return {}
        completion_policy = self._completion_policy()
        workspace_policy = dict((self.pack.workspace or {}).get("workspace_policy") or {})
        audit_read_only_repo = str(workspace_policy.get("mode") or "").strip().lower() == "read_only_repo"
        audit_git_commit = str(completion_policy.get("evidence") or "").strip().lower() == "git_commit"
        if not (audit_read_only_repo or audit_git_commit):
            return {}
        repo_path = str((self.pack.workspace or {}).get("repo_path") or "").strip()
        if not repo_path:
            return {}
        if audit_read_only_repo:
            scratch_repo = str((self.pack.workspace or {}).get("review_scratch_repo_path") or "").strip()
            snapshot = {
                "snapshot_kind": "read_only_repo",
                "repo_path": repo_path,
                "source_git": _git_workspace_snapshot(Path(repo_path)),
            }
            if scratch_repo:
                snapshot["review_scratch_repo_path"] = scratch_repo
                snapshot["scratch_tree"] = _file_tree_snapshot(Path(scratch_repo))
            if not snapshot.get("source_git") and not snapshot.get("scratch_tree"):
                return {}
            return snapshot
        return _git_workspace_snapshot(Path(repo_path))

    def _record_shell_audit_violation(self, target_name: str, tool_call: CanonicalToolCall, before_snapshot: dict[str, Any]) -> dict[str, Any] | None:
        if not before_snapshot or not _is_shell_capability_name(target_name):
            return None
        after_snapshot = _shell_audit_after_snapshot(before_snapshot)
        if not after_snapshot or not _shell_audit_snapshot_changed_meaningfully(before_snapshot, after_snapshot):
            return None
        repo_path = _shell_audit_changed_root_path(before_snapshot, after_snapshot)
        violation = {
            "violation_id": f"shell_mut_{uuid4().hex[:12]}",
            "kind": "shell_workspace_mutation",
            "command": str(_effective_tool_args(tool_call).get("cmd") or ""),
            "repo_path": repo_path,
            "before": before_snapshot,
            "after": after_snapshot,
            "summary": "op_exec_shell changed an audited workspace; reviewer must rerun from a clean workspace before closing the milestone.",
        }
        self.shell_mutation_violations.append(violation)
        self._append_debug_log("shell_mutation_violation", violation)
        return violation

    def _record_review_tool_evidence(self, target_name: str, tool_call: CanonicalToolCall, result: CanonicalToolResult) -> None:
        evidence = _review_tool_evidence_ref(target_name, tool_call, result)
        if not evidence:
            return
        self.review_tool_evidence_refs.append(evidence)
        self._append_debug_log("review_tool_evidence_ref", evidence)

    async def _await_with_progress_heartbeat(self, awaitable, *, phase: str, **payload: Any):
        interval = self._heartbeat_interval_seconds()
        if interval <= 0:
            return await awaitable
        task = asyncio.create_task(awaitable)
        heartbeat_count = 0
        try:
            while True:
                done, _pending = await asyncio.wait({task}, timeout=interval)
                if task in done:
                    return await task
                heartbeat_count += 1
                await self._emit_progress(phase, heartbeat_count=heartbeat_count, **payload)
        except BaseException:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            raise

    def _heartbeat_interval_seconds(self) -> float:
        metadata = self.pack.metadata if isinstance(self.pack.metadata, dict) else {}
        raw = metadata.get("heartbeat_interval_seconds")
        if raw is None:
            return 30.0
        try:
            interval = float(raw)
        except (TypeError, ValueError):
            return 30.0
        if interval <= 0:
            return 0.0
        return max(0.01, interval)

    async def _requires_approval(self, capability_name: str, tool_call: CanonicalToolCall) -> bool:
        if self._sandboxed() and (_is_shell_capability_name(capability_name) or _is_git_capability_name(capability_name)):
            return False
        return self._user_interaction_port().should_request_approval(capability_name, self.pack.approval_policy or {})

    async def _requires_web_research_approval(self, capability_name: str, tool_call: CanonicalToolCall) -> bool:
        _ = tool_call
        if self.auto_accept_approvals:
            return False
        status = self._web_research_budget_status(capability_name)
        if not status:
            return False
        return int(status["used"]) >= int(status["budget"])

    def _web_research_budget_status(self, capability_name: str) -> dict[str, Any] | None:
        canonical_name = _web_research_capability_name(capability_name)
        if canonical_name is None:
            return None
        budget_policy = (self.pack.approval_policy or {}).get("web_research_budget")
        if budget_policy is None:
            return None
        statuses: list[dict[str, Any]] = []
        if isinstance(budget_policy, dict):
            total_budget = _optional_nonnegative_int(
                budget_policy.get("total", budget_policy.get("web", budget_policy.get("all")))
            )
            if total_budget is not None:
                statuses.append({"key": "total", "used": self.web_research_usage.get("total", 0), "budget": total_budget})
            capability_budget = None
            for key in _web_research_budget_keys(canonical_name):
                if key in budget_policy:
                    capability_budget = _optional_nonnegative_int(budget_policy.get(key))
                    break
            if capability_budget is not None:
                statuses.append(
                    {"key": canonical_name, "used": self.web_research_usage.get(canonical_name, 0), "budget": capability_budget}
                )
        else:
            total_budget = _optional_nonnegative_int(budget_policy)
            if total_budget is not None:
                statuses.append({"key": "total", "used": self.web_research_usage.get("total", 0), "budget": total_budget})
        if not statuses:
            return None
        exceeded = [status for status in statuses if int(status["used"]) >= int(status["budget"])]
        return exceeded[0] if exceeded else statuses[0]

    def _record_web_research_usage(self, capability_name: str) -> None:
        canonical_name = _web_research_capability_name(capability_name)
        if canonical_name is None:
            return
        self.web_research_usage[canonical_name] = self.web_research_usage.get(canonical_name, 0) + 1
        self.web_research_usage["total"] = self.web_research_usage.get("total", 0) + 1

    async def _requires_git_mutation_approval(self, capability_name: str, tool_call: CanonicalToolCall) -> bool:
        if not _is_git_capability_name(capability_name):
            return False
        if self._sandboxed() or self.git_mutation_approved:
            return False
        cmd = str(_effective_tool_args(tool_call).get("cmd") or "").strip()
        return _git_command_is_mutating(cmd)

    async def _request_approval(
        self,
        capability_name: str,
        tool_call: CanonicalToolCall,
        *,
        approval_kind: str = "high_risk",
        title: str | None = None,
        risk: str = "high",
        impact: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        decision = await self._user_interaction_port().request_approval(
            capability_name=capability_name,
            args_summary=dict(tool_call.args),
            approval_policy=self.pack.approval_policy or {},
            approval_kind=approval_kind,
            title=title,
            risk=risk,
            impact=impact,
            metadata=metadata,
        )
        self.auto_accept_approvals = self._user_interaction_port().auto_accept_approvals
        return decision

    async def _request_clarification(self, ask_user_question: dict[str, Any]) -> dict[str, Any]:
        return await self._user_interaction_port().request_clarification(
            ask_user_question,
            approval_policy=self.pack.approval_policy or {},
        )

    def _user_interaction_port(self) -> MinionUserInteractionPort:
        if self.user_interaction is None:
            self.user_interaction = MinionUserInteractionPort(
                emit_event=self._emit,
                read_response=self._read_manager_control,
                run_id=self.run_id,
                minion_id=self.minion_id,
                work_order_id=self.pack.work_order_id,
                auto_accept_approvals=self.auto_accept_approvals,
            )
        return self.user_interaction

    def _apply_clarification_response(self, clarification: dict[str, Any]) -> None:
        answers = [dict(item) for item in list(clarification.get("answers") or []) if isinstance(item, dict)]
        if not answers:
            return
        metadata = dict(self.pack.metadata or {})
        existing_answers = [dict(item) for item in list(metadata.get("clarification_answers") or []) if isinstance(item, dict)]
        existing_answers.extend(answers)
        metadata["clarification_answers"] = existing_answers[-50:]
        planner_work_order = dict(metadata.get("planner_work_order") or {})
        if not planner_work_order:
            planner_work_order = build_planner_work_order(
                goal=self.pack.goal or self.pack.instruction,
                task_id=str(metadata.get("task_id") or ""),
                work_order_id=self.pack.work_order_id,
            )
        planner_work_order["turn_index"] = int(_coerce_int(planner_work_order.get("turn_index"), default=0)) + 1
        planner_work_order["plan_revision"] = int(_coerce_int(planner_work_order.get("plan_revision"), default=0)) + 1
        planner_work_order["clarifications"] = existing_answers[-50:]
        metadata["planner_work_order"] = planner_work_order
        metadata.pop("prompt_view", None)
        self.pack = TaskContextPack.from_dict({**self.pack.to_dict(), "metadata": metadata})

    def _max_clarification_rounds(self) -> int:
        raw = (self.pack.approval_policy or {}).get("max_clarification_rounds")
        return max(1, min(5, _coerce_int(raw, default=3)))

    async def _inspect_current_milestone_checkpoint(self) -> dict[str, Any]:
        repo_path = str((self.pack.workspace or {}).get("repo_path") or "").strip()
        if not repo_path:
            return {"status": "error", "error": "current project repo is missing"}
        return inspect_milestone_checkpoint(
            Path(repo_path),
            base_sha=str((self.pack.workspace or {}).get("base_sha") or ""),
        )

    async def _complete_current_milestone(self, final_text: str) -> dict[str, Any]:
        completion_policy = self._completion_policy()
        evidence = str(completion_policy.get("evidence") or "text_deliverable").strip().lower()
        prompt_view = _prompt_view_from_pack(self.pack)
        module = dict(prompt_view.get("module") or {}) if prompt_view else {}
        ask_user_question = _extract_ask_user_question_payload(final_text)
        base_payload = {
            "checkpoint_id": f"chk_{uuid4().hex[:16]}",
            "task_id": str((self.pack.metadata or {}).get("task_id") or prompt_view.get("task_id") or ""),
            "module_id": str(module.get("module_id") or ""),
            "milestone_index": self._current_milestone_index(),
            "milestone_id": str(self._current_milestone().get("milestone_id") or ""),
            "acceptance_criteria": _current_acceptance_criteria(self.pack, self._current_milestone()),
            "summary": self._short_summary(final_text or "minion completed current milestone"),
            "shell_mutation_violations": list(self.shell_mutation_violations),
        }
        if self._requires_checkpoint_review_gate():
            repair_attempt = self._current_repair_attempt_payload()
            if repair_attempt:
                base_payload["repair_attempt"] = repair_attempt
                base_payload["expected_review_gate_kind"] = "repair_verification"
            else:
                base_payload["expected_review_gate_kind"] = "checkpoint_verification"
        if ask_user_question:
            return {
                **base_payload,
                "status": "blocked",
                "summary": _ask_user_question_summary(ask_user_question),
                "ask_user_question": ask_user_question,
                **self._artifact_payload(),
            }
        if evidence == "git_commit":
            await self._persist_text_deliverable_if_needed(final_text)
            checkpoint = await self._inspect_current_milestone_checkpoint()
            checkpoint_status = "claimed" if self._requires_checkpoint_review_gate() else "completed"
            if checkpoint.get("status") == "committed":
                reuse_block = self._repair_reused_failed_checkpoint_payload(base_payload, checkpoint)
                if reuse_block:
                    return reuse_block
                return {
                    **base_payload,
                    "status": checkpoint_status,
                    "commit_sha": str(checkpoint.get("commit_sha") or ""),
                    "git_commit": checkpoint,
                    "evidence": "git_commit",
                    "shell_mutation_violations": list(self.shell_mutation_violations),
                    **self._artifact_payload(),
                }
            if checkpoint.get("status") == "no_changes" and self._artifact_completion_evidence_present():
                return {
                    **base_payload,
                    "status": checkpoint_status,
                    "commit_sha": str(checkpoint.get("commit_sha") or ""),
                    "git_commit": checkpoint,
                    "evidence": "git_commit",
                    "shell_mutation_violations": list(self.shell_mutation_violations),
                    **self._artifact_payload(),
                }
            blocked_summary = str(
                checkpoint.get("error")
                or checkpoint.get("summary")
                or f"milestone checkpoint is not committed: {checkpoint.get('status')}"
            )
            return {
                **base_payload,
                "status": "blocked",
                "summary": blocked_summary,
                "git_commit": checkpoint,
                "shell_mutation_violations": list(self.shell_mutation_violations),
                **self._artifact_payload(),
            }
        await self._persist_text_deliverable_if_needed(final_text)
        if not str(final_text or "").strip() and not self.produced_artifacts:
            return {
                **base_payload,
                "status": "blocked",
                "summary": "milestone produced no text deliverable",
                "evidence": evidence or "text_deliverable",
                **self._artifact_payload(),
            }
        planner_payload = self._planner_plan_artifact_completion_payload()
        if planner_payload.get("status") == "blocked":
            return {**base_payload, **planner_payload, **self._artifact_payload()}
        return {
            **base_payload,
            "status": "completed",
            "evidence": evidence or "text_deliverable",
            **self._artifact_payload(),
            **planner_payload,
        }

    def _repair_reused_failed_checkpoint_payload(self, base_payload: dict[str, Any], checkpoint: dict[str, Any]) -> dict[str, Any]:
        repair_attempt = self._current_repair_attempt_payload()
        failed_commit_sha = str(repair_attempt.get("failed_commit_sha") or "").strip()
        commit_sha = str(checkpoint.get("commit_sha") or "").strip()
        if not failed_commit_sha or not commit_sha or commit_sha != failed_commit_sha:
            return {}
        return {
            **base_payload,
            "status": "blocked",
            "reason": "repair_reused_failed_checkpoint",
            "summary": (
                "repair did not create a new checkpoint commit; the previous failed checkpoint commit "
                f"{failed_commit_sha} cannot be resubmitted"
            ),
            "commit_sha": commit_sha,
            "git_commit": checkpoint,
            "repair_attempt": repair_attempt,
            "shell_mutation_violations": list(self.shell_mutation_violations),
            **self._artifact_payload(),
        }

    def _completion_evidence_present(self) -> bool:
        completion_policy = self._completion_policy()
        if str(completion_policy.get("evidence") or "").strip().lower() != "git_commit":
            return False
        repo_path = str((self.pack.workspace or {}).get("repo_path") or "").strip()
        if not repo_path:
            return False
        repo = Path(repo_path)
        if not (repo / ".git").exists():
            return False
        checkpoint = inspect_milestone_checkpoint(
            repo,
            base_sha=str((self.pack.workspace or {}).get("base_sha") or ""),
        )
        return checkpoint.get("status") == "committed"

    def _requires_checkpoint_review_gate(self) -> bool:
        return checkpoint_gate_spec_for_pack(self.pack) is not None

    def _current_repair_attempt_payload(self) -> dict[str, Any]:
        metadata = dict(self.pack.metadata or {})
        module_execution = dict(metadata.get("module_execution") or {})
        last = dict(module_execution.get("last_repair_attempt") or {})
        if not last:
            return {}
        if _coerce_int(last.get("milestone_index"), default=-1) != self._current_milestone_index():
            return {}
        return dict(last)

    def _artifact_completion_evidence_present(self) -> bool:
        if not self.produced_artifacts:
            return False
        completion_policy = self._completion_policy()
        if str(completion_policy.get("evidence") or "").strip().lower() != "git_commit":
            return False
        metadata = self.pack.metadata if isinstance(self.pack.metadata, dict) else {}
        return bool(metadata.get("allow_text_only_completion") or completion_policy.get("allow_artifact_evidence"))

    def _completion_evidence_fallback_text(self, error_text: str) -> str:
        summary = str(error_text or "LLM final report generation failed").strip()
        if len(summary) > 500:
            summary = summary[:497].rstrip() + "..."
        return (
            "Milestone produced completion evidence through capability calls.\n\n"
            "The final report generation failed after evidence was produced, so the runner is "
            "using the verified workspace evidence as the completion signal.\n\n"
            f"LLM error: {summary}"
        )

    async def _persist_text_deliverable_if_needed(
        self,
        final_text: str,
        *,
        partial: bool = False,
        truncation_reason: str = "",
    ) -> None:
        text = str(final_text or "").strip()
        if not text or self.produced_artifacts:
            return
        if not str((self.pack.workspace or {}).get("artifact_dir") or "").strip():
            return
        suffix = ".partial" if partial else ""
        title = self._current_milestone_title()
        reviewer_json = "" if partial else self._reviewer_json_deliverable(text)
        if reviewer_json:
            artifact = _write_minion_artifact(
                self.pack.workspace,
                {
                    "relative_path": "review_report.json",
                    "title": "Review report",
                    "role": "primary",
                    "mime_type": "application/json",
                    "content": reviewer_json,
                },
            )
            self._record_produced_artifact(artifact)
            return
        header_lines = [
            f"# {title}",
            "",
            f"- work_order_id: {self.pack.work_order_id}",
            f"- minion_id: {self.minion_id}",
            f"- run_id: {self.run_id}",
        ]
        if partial:
            header_lines.extend(
                [
                    f"- status: blocked",
                    f"- truncation_reason: {str(truncation_reason or 'unknown')}",
                    "",
                    "This is partial LLM output saved for diagnosis. It must not be treated as a completed deliverable.",
                ]
            )
        header_lines.append("")
        artifact = _write_minion_artifact(
            self.pack.workspace,
            {
                "relative_path": self._default_text_artifact_path(suffix=suffix, partial=partial),
                "title": self._default_text_artifact_title(title, partial=partial),
                "role": "primary" if not partial else "partial",
                "mime_type": "text/markdown",
                "content": "\n".join([*header_lines, text, ""]),
            },
        )
        self._record_produced_artifact(artifact)

    def _default_text_artifact_path(self, *, suffix: str = "", partial: bool = False) -> str:
        if self._is_reviewer_profile() and not partial:
            return "review_report.md"
        return f"milestone_{self._current_milestone_index()}_{self._safe_path_part(self.pack.minion_profile)}{suffix}.md"

    def _default_text_artifact_title(self, title: str, *, partial: bool = False) -> str:
        if partial:
            return f"{title} (partial truncated output)"
        if self._is_reviewer_profile():
            return "Review report"
        return title

    def _execution_contract(self) -> dict[str, Any]:
        return self._policy_from_workspace_or_profile("execution_contract")

    def _is_reviewer_profile(self) -> bool:
        metadata = dict(self.pack.metadata or {})
        execution_contract = self._execution_contract()
        artifact_role = str(
            execution_contract.get("artifact_role")
            or execution_contract.get("module_role")
            or execution_contract.get("role")
            or ""
        ).strip().lower()
        output_policy = self._policy_from_workspace_or_profile("output_policy")
        primary_artifact = str(output_policy.get("primary_artifact") or "").strip().lower()
        output_types = {str(item or "").strip() for item in list(output_policy.get("allowed_output_types") or [])}
        return (
            artifact_role in {"reviewer", "review", "review_report"}
            or primary_artifact == "review_report"
            or "ReviewReport" in output_types
            or isinstance(metadata.get("reviewer_work_order"), dict)
            or isinstance(metadata.get("review_gate_ref"), dict)
            or isinstance(metadata.get("review_target"), dict)
        )

    def _reviewer_json_deliverable(self, text: str) -> str:
        if not self._is_reviewer_profile():
            return ""
        candidate = _strip_single_json_code_fence(text)
        if not candidate.startswith("{"):
            return ""
        try:
            parsed, end = json.JSONDecoder().raw_decode(candidate)
        except json.JSONDecodeError:
            return ""
        if not isinstance(parsed, dict):
            return ""
        if candidate[end:].strip():
            return ""
        return candidate

    def _truncated_output_blocked_summary(self, finish_reason: str) -> str:
        reason = str(finish_reason or "unknown").strip() or "unknown"
        primary = dict(self._artifact_payload().get("primary_artifact") or {})
        artifact_path = str(primary.get("path") or primary.get("relative_path") or "").strip()
        saved = f" Partial output was saved to {artifact_path}." if artifact_path else ""
        return (
            f"LLM output was truncated before the minion completed the milestone "
            f"(finish_reason={reason}). Treat this milestone as blocked.{saved} "
            "For long deliverables, write the full result as an artifact/file and keep the final reply short."
        )

    @staticmethod
    def _safe_path_part(value: str) -> str:
        normalized = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(value or "").strip())
        return normalized.strip("_")[:80] or "minion"

    async def _emit(self, event_kind: str, payload: dict[str, Any]) -> None:
        event = {
            "type": "event",
            "event_kind": event_kind,
            "minion_id": self.minion_id,
            "run_id": self.run_id,
            "work_order_id": self.pack.work_order_id,
            "minion_profile": self.pack.minion_profile,
            "payload": dict(payload),
            "created_at": utc_now(),
        }
        self._append_debug_log("runner_event", event)
        await self.write_event(event)

    async def _emit_progress(self, phase: str, **payload: Any) -> None:
        await self._emit(
            "progress",
            {
                "phase": phase,
                "summary": _progress_summary(phase, payload),
                "milestone_index": self._current_milestone_index(),
                "milestone_title": self._current_milestone_title(),
                **payload,
            },
        )

    def _prompt_scaffold(self) -> dict[str, Any]:
        profile = dict(self.pack.resolved_profile or {})
        prompt_view = _prompt_view_from_pack(self.pack)
        milestone = dict(prompt_view.get("milestone") or {}) if prompt_view else self._current_milestone()
        requirements_brief = dict((self.pack.metadata or {}).get("requirements_brief") or {})
        brief_acceptance = [
            str(item).strip()
            for item in list(requirements_brief.get("acceptance_criteria") or [])
            if str(item or "").strip()
        ]
        acceptance = brief_acceptance or list(milestone.get("acceptance_criteria") or self.pack.acceptance_criteria)
        instruction = str(milestone.get("task") or self.pack.instruction or self.pack.goal)
        return {
            "identity": str(profile.get("identity_fragment") or ""),
            "behavior": str(profile.get("behavior_fragment") or ""),
            "instruction": instruction,
            "acceptance_criteria": acceptance,
            "continuity": dict(self.pack.continuity),
            "current_milestone": milestone,
            "allowed_capabilities": list(self.pack.allowed_capabilities),
            "skill_manual_context": list((self.pack.metadata or {}).get("skill_manual_context") or []),
            "output_contract": str(profile.get("output_contract_fragment") or ""),
            "workspace_policy": self._workspace_policy(),
            "completion_policy": self._completion_policy(),
            "execution_strategy": self._execution_strategy(),
            "prompt_view": prompt_view,
            "active_gate_todo": dict((self.pack.metadata or {}).get("active_gate_todo") or {}),
            "repair_context": self._repair_context_for_compaction(),
            "requirements_brief": requirements_brief,
        }

    def _repair_context_for_compaction(self) -> dict[str, Any]:
        metadata = dict(self.pack.metadata or {})
        result: dict[str, Any] = {}
        current = self._current_repair_attempt_payload()
        if current:
            result["current_repair_attempt"] = current
        checkpoint_repair = metadata.get("checkpoint_repair")
        if isinstance(checkpoint_repair, dict):
            result["checkpoint_repair"] = dict(checkpoint_repair)
        active_gate_todo = metadata.get("active_gate_todo")
        if isinstance(active_gate_todo, dict):
            result["active_gate_todo"] = dict(active_gate_todo)
        repair_context = metadata.get("repair_context")
        if isinstance(repair_context, dict):
            result["repair_bill"] = dict(repair_context)
        return result

    def _workspace_policy(self) -> dict[str, Any]:
        workspace_policy = self.pack.workspace.get("workspace_policy")
        if isinstance(workspace_policy, dict):
            return dict(workspace_policy)
        profile = dict(self.pack.resolved_profile or {})
        if isinstance(profile.get("effective_workspace_policy"), dict):
            return dict(profile.get("effective_workspace_policy") or {})
        return {}

    def _completion_policy(self) -> dict[str, Any]:
        completion_policy = self.pack.workspace.get("completion_policy")
        if isinstance(completion_policy, dict):
            return dict(completion_policy)
        profile = dict(self.pack.resolved_profile or {})
        if isinstance(profile.get("effective_completion_policy"), dict):
            return dict(profile.get("effective_completion_policy") or {})
        return {}

    def _execution_strategy(self) -> dict[str, Any]:
        execution_strategy = self.pack.workspace.get("execution_strategy")
        if isinstance(execution_strategy, dict):
            return dict(execution_strategy)
        profile = dict(self.pack.resolved_profile or {})
        if isinstance(profile.get("effective_execution_strategy"), dict):
            return dict(profile.get("effective_execution_strategy") or {})
        return execution_strategy_from_pack(
            self.pack,
            workspace_policy=self._workspace_policy(),
            completion_policy=self._completion_policy(),
            gate_policy=self._policy_from_workspace_or_profile("gate_policy"),
            output_policy=self._policy_from_workspace_or_profile("output_policy"),
        )

    def _policy_from_workspace_or_profile(self, key: str) -> dict[str, Any]:
        value = self.pack.workspace.get(key)
        if isinstance(value, dict):
            return dict(value)
        profile = dict(self.pack.resolved_profile or {})
        effective_key = f"effective_{key}"
        if isinstance(profile.get(effective_key), dict):
            return dict(profile.get(effective_key) or {})
        if isinstance(profile.get(key), dict):
            return dict(profile.get(key) or {})
        return {}

    def _current_milestone(self) -> dict[str, Any]:
        prompt_view = _prompt_view_from_pack(self.pack)
        milestone = dict(prompt_view.get("milestone") or {}) if prompt_view else {}
        if milestone:
            return milestone
        return dict((self.pack.continuity or {}).get("current_milestone") or {})

    def _current_milestone_index(self) -> int:
        try:
            return int(self._current_milestone().get("milestone_index") or 0)
        except (TypeError, ValueError):
            return 0

    def _current_milestone_title(self) -> str:
        return str(self._current_milestone().get("title") or self.pack.instruction or self.pack.goal or "Complete milestone")

    def _terminal_payload(self, status: str, summary: Any) -> dict[str, Any]:
        resolved_status = str(status or "").strip() or "completed"
        summary_text = str(summary or "").strip()
        ask_user_question = _extract_ask_user_question_payload(summary_text)
        lesson_payload = _extract_lessons_and_clean_summary(summary_text)
        summary_text = self._short_summary(str(lesson_payload.get("summary") or summary_text).strip())
        experience_payload = {
            "task_lessons": list(lesson_payload.get("task_lessons") or []),
            "system_lessons": list(lesson_payload.get("system_lessons") or []),
            "memory_candidates": [dict(item) for item in self.memory_candidates],
        }
        payload = {
            "status": resolved_status,
            "summary": summary_text,
            **experience_payload,
            **self._artifact_payload(),
        }
        if resolved_status == "completed" and self._defer_experience_until_module_complete():
            payload["task_lessons"] = []
            payload["system_lessons"] = []
            payload["memory_candidates"] = []
            payload["deferred_experience"] = experience_payload
        if ask_user_question:
            payload["status"] = "blocked"
            payload["summary"] = _ask_user_question_summary(ask_user_question)
            payload["ask_user_question"] = ask_user_question
        return payload

    def _cancel_terminal_payload(self, cancel: dict[str, Any]) -> dict[str, Any]:
        payload = self._terminal_payload("killed", cancel.get("summary") or cancel.get("reason") or "minion cancellation requested")
        payload["reason"] = str(cancel.get("reason") or "cooperative_cancel_requested")
        payload["cooperative_cancel"] = True
        for key in ("parent_work_order_id", "child_work_order_id", "module_id", "bill_id"):
            value = str(cancel.get(key) or "").strip()
            if value:
                payload[key] = value
        return payload

    def _defer_experience_until_module_complete(self) -> bool:
        metadata = dict(self.pack.metadata or {})
        module_execution = dict(metadata.get("module_execution") or {})
        return bool(
            metadata.get("defer_experience_until_module_complete")
            or module_execution.get("defer_experience_until_module_complete")
        )

    def _artifact_payload(self) -> dict[str, Any]:
        artifacts = [dict(item) for item in self.produced_artifacts]
        payload: dict[str, Any] = {"artifacts": artifacts}
        primary = next((item for item in artifacts if str(item.get("role") or "") == "primary"), None)
        if primary is None and artifacts:
            primary = artifacts[0]
        if primary is not None:
            payload["primary_artifact"] = dict(primary)
        return payload

    def _planner_plan_artifact_completion_payload(self) -> dict[str, Any]:
        if not self._requires_planner_plan_artifact_validation():
            return {}
        artifact = self._select_planner_json_artifact()
        if not artifact:
            return {
                "status": "blocked",
                "summary": "planner milestone did not submit a primary plan draft artifact",
                "plan_validation": {"status": "invalid", "errors": ["primary submitted plan draft artifact is required"]},
            }
        path = self._artifact_file_path(artifact)
        if path is None:
            return {
                "status": "blocked",
                "summary": "planner plan artifact has no readable path",
                "plan_validation": {"status": "invalid", "errors": ["plan artifact path is required"]},
            }
        try:
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            payload = json.loads(content.decode("utf-8"))
        except Exception as exc:
            return {
                "status": "blocked",
                "summary": f"planner plan artifact is not readable JSON: {exc}",
                "plan_validation": {"status": "invalid", "errors": [str(exc)]},
            }
        if not isinstance(payload, dict):
            return {
                "status": "blocked",
                "summary": "planner plan artifact must be a JSON object",
                "plan_validation": {"status": "invalid", "errors": ["plan artifact must be an object"]},
            }
        metadata = dict(self.pack.metadata or {})
        planner_work_order = dict(metadata.get("planner_work_order") or {})
        expected_task_id = str(
            metadata.get("expected_plan_task_id")
            or (
                planner_work_order.get("task_id")
                if isinstance(planner_work_order.get("revision_source"), dict)
                else ""
            )
            or metadata.get("task_id")
            or ""
        ).strip()
        declared_task_id = str(payload.get("task_id") or "").strip()
        if expected_task_id:
            if declared_task_id and declared_task_id != expected_task_id:
                return {
                    "status": "blocked",
                    "summary": "planner plan artifact task_id does not match manager task identity",
                    "plan_validation": {"status": "invalid", "errors": ["task_id does not match manager task identity"]},
                }
            payload["task_id"] = expected_task_id
        output_type = str(payload.get("type") or payload.get("output_type") or "").strip()
        payload_metadata = dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {}
        plan_builder_metadata = dict(payload_metadata.get("plan_builder") or {}) if isinstance(payload_metadata.get("plan_builder"), dict) else {}
        plan_revision = _coerce_int(payload.get("plan_revision"), default=-1)
        if plan_revision < 0:
            plan_revision = _coerce_int(payload_metadata.get("plan_revision"), default=0)
        expected_plan_revision = _expected_planner_plan_revision(self.pack)
        if expected_plan_revision >= 0 and plan_revision != expected_plan_revision:
            return {
                "status": "blocked",
                "summary": f"planner plan artifact plan_revision must be {expected_plan_revision}",
                "plan_validation": {
                    "status": "invalid",
                    "errors": [f"plan_revision must be {expected_plan_revision}, got {plan_revision}"],
                },
            }
        plan_ref = {
            "path": str(path),
            "sha256": digest,
            "artifact_role": str(artifact.get("role") or ""),
            "relative_path": str(artifact.get("relative_path") or ""),
            "plan_revision": plan_revision,
        }
        plan_handle = str(plan_builder_metadata.get("plan_handle") or "").strip()
        if plan_handle:
            plan_ref["plan_handle"] = plan_handle
        lifecycle = str(plan_builder_metadata.get("lifecycle") or "").strip()
        if lifecycle == "submitted" or str(artifact.get("relative_path") or "").endswith(".draft.json"):
            plan_ref["ref_kind"] = "plan_draft"
            artifact_dir = str((self.pack.workspace or {}).get("artifact_dir") or "").strip()
            if artifact_dir:
                plan_ref["artifact_dir"] = artifact_dir
        if output_type == "AskUserQuestion":
            return {
                "status": "blocked",
                "summary": _ask_user_question_summary(payload),
                "ask_user_question": payload,
                "plan_ref": plan_ref,
                "plan_validation": {"status": "ask_user_question"},
            }
        if output_type == "PlanDraft":
            return {
                "status": "blocked",
                "summary": "planner produced PlanDraft; a dispatchable FinalPlanArtifact is required before implementation",
                "plan_ref": plan_ref,
                "plan_validation": {"status": "draft"},
            }
        try:
            artifact_payload = validate_dispatchable_plan_artifact(payload)
            validation = dispatchable_plan_validation(artifact_payload)
        except Exception as exc:
            return {
                "status": "blocked",
                "summary": f"planner FinalPlanArtifact failed dispatch validation: {exc}",
                "plan_ref": plan_ref,
                "plan_validation": {"status": "invalid", "errors": [str(exc)]},
            }
        payload, digest = self._persist_normalized_planner_plan_artifact(
            path,
            payload=payload,
            artifact=artifact_payload,
            plan_revision=plan_revision,
        )
        plan_ref.update(
            {
                "plan_id": artifact_payload.plan_id,
                "task_id": artifact_payload.task_id,
                "sha256": digest,
                "plan_revision": plan_revision,
            }
        )
        return {
            "plan_ref": plan_ref,
            **({"plan_draft_ref": dict(plan_ref)} if plan_ref.get("ref_kind") == "plan_draft" else {}),
            "plan_validation": validation,
        }

    def _persist_normalized_planner_plan_artifact(
        self,
        path: Path,
        *,
        payload: dict[str, Any],
        artifact: Any,
        plan_revision: int,
    ) -> tuple[dict[str, Any], str]:
        normalized = dict(payload)
        normalized["type"] = "FinalPlanArtifact"
        normalized["plan_id"] = artifact.plan_id
        normalized["task_id"] = artifact.task_id
        normalized["plan_revision"] = max(0, int(plan_revision or 0))
        metadata = dict(normalized.get("metadata") or {}) if isinstance(normalized.get("metadata"), dict) else {}
        metadata.setdefault("plan_revision", normalized["plan_revision"])
        normalized["metadata"] = metadata

        current_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        next_digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        if next_digest != current_digest:
            path.write_text(encoded, encoding="utf-8")
            current_digest = next_digest

        size_bytes = path.stat().st_size
        for existing in self.produced_artifacts:
            existing_path = str(existing.get("path") or "").strip()
            if existing_path and Path(existing_path) == path:
                existing["sha256"] = current_digest
                existing["size_bytes"] = size_bytes
        return normalized, current_digest

    def _requires_planner_plan_artifact_validation(self) -> bool:
        return pack_requires_plan_artifact_validation(self.pack)

    def _select_planner_json_artifact(self) -> dict[str, Any]:
        candidates = [(index, dict(item)) for index, item in enumerate(self.produced_artifacts)]
        candidates.sort(key=lambda indexed: (0 if str(indexed[1].get("role") or "") == "primary" else 1, -indexed[0]))
        for _index, artifact in candidates:
            relative_path = str(artifact.get("relative_path") or artifact.get("path") or "").strip().lower()
            mime_type = str(artifact.get("mime_type") or "").strip().lower()
            if relative_path.endswith("plan.json") or mime_type == "application/json":
                return artifact
        return {}

    def _artifact_file_path(self, artifact: dict[str, Any]) -> Path | None:
        raw_path = str(artifact.get("path") or "").strip()
        if raw_path:
            return Path(raw_path)
        relative_path = str(artifact.get("relative_path") or "").strip()
        artifact_dir = str((self.pack.workspace or {}).get("artifact_dir") or "").strip()
        if relative_path and artifact_dir:
            return Path(artifact_dir) / relative_path
        return None

    def _record_produced_artifact(self, artifact: dict[str, Any]) -> None:
        path = str(artifact.get("path") or "").strip()
        relative_path = str(artifact.get("relative_path") or "").strip()
        if not path and not relative_path:
            return
        for existing in self.produced_artifacts:
            if path and str(existing.get("path") or "") == path:
                return
            if relative_path and str(existing.get("relative_path") or "") == relative_path:
                return
        _append_unique_artifact(self.produced_artifacts, artifact)

    @staticmethod
    def _short_summary(value: Any, *, limit: int = 500) -> str:
        text = _compact_preview_text(str(value or ""))
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."

    def _append_debug_log(self, section: str, payload: dict[str, Any]) -> None:
        config = dict((self.pack.metadata or {}).get("debug_log") or {})
        if not bool(config.get("enabled")):
            return
        path_text = str(config.get("path") or "").strip()
        if not path_text:
            return
        record = {
            "created_at": utc_now(),
            "section": str(section or "debug"),
            "work_order_id": self.pack.work_order_id,
            "minion_profile": self.pack.minion_profile,
            "minion_id": self.minion_id,
            "run_id": self.run_id,
            "payload": dict(payload or {}),
        }
        try:
            path = Path(path_text)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        except Exception:
            return

def build_slim_minion_runtime(runtime_root: Path, *, run_id: str = "") -> MinionRuntimeBundle:
    from pal.core import register_with_core as register_core_with_core

    database = PalV2Database(db_path=Path(runtime_root) / "pal.sqlite3")
    database.initialize(ALL_MODELS)
    llm_repository = LLMEndpointRepository()
    web_search_repository = WebSearchProviderRepository()
    web_fetch_repository = WebFetchProviderRepository()
    if not llm_repository.list_enabled():
        llm_repository.ensure_defaults(DEFAULT_LLM_ENDPOINTS)
    if not web_search_repository.list_all():
        web_search_repository.ensure_defaults(DEFAULT_WEB_SEARCH_PROVIDERS)
    if not web_fetch_repository.list_all():
        web_fetch_repository.ensure_defaults(DEFAULT_WEB_FETCH_PROVIDERS)
    settings = RuntimeSettingRepository()
    settings.ensure_defaults()
    if settings.get("active_web_search_provider_id") is None:
        enabled = web_search_repository.list_enabled()
        if enabled:
            settings.set("active_web_search_provider_id", enabled[0].provider_id)
    if settings.get("active_web_fetch_provider_id") is None:
        enabled = web_fetch_repository.list_enabled()
        if enabled:
            settings.set("active_web_fetch_provider_id", enabled[0].provider_id)

    config = RuntimeConfig.load(Path(runtime_root))
    core = PalCore(config=config)
    core.context.execution_runtime.runtime_root = Path(runtime_root)
    artifact_service = ArtifactManager(runtime_root=Path(runtime_root), repository=ArtifactRepository())
    if os.environ.get("PAL_MINION_LLM_BROKER") == "1":
        llm_runtime = MinionBrokerLLMRuntime(runtime_root=Path(runtime_root), run_id=run_id)
    else:
        llm_runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(repository=llm_repository),
            settings_repository=settings,
            endpoint_invoker=build_default_endpoint_invoker(
                credentials=LLMCredentialResolver(secret_store=EncryptedFileSecretStore(secrets_path=str(Path(runtime_root) / "secrets.json"))),
                artifact_manager=artifact_service,
                runtime_root=runtime_root,
                message_hooks=DEFAULT_LLM_REQUEST_HOOKS,
            ),
            config=config,
        )
    register_core_with_core(core)
    register_execution_with_core(core.context)
    register_artifact_with_core(core.context, artifact_service)
    memory_service = MemoryService(
        l3_selector=L3ProviderSelector(
            resolver=core.context.execution_runtime.l3_plugin_registry.require,
            active_provider_id="sqlite_vec_l3",
        )
    )
    register_memory_with_core(core.context, memory_service, config=config)
    l3_plugin = SQLiteVecL3Plugin(service=memory_service)
    memory_service.l3_selector.active_provider_id = l3_plugin.provider_id
    register_l3_with_core(core.context, l3_plugin)
    register_web_search_with_core(core.context, WebSearchService(repository=web_search_repository, settings_repository=settings))
    register_web_fetch_with_core(
        core.context,
        WebFetchService(
            repository=web_fetch_repository,
            settings_repository=settings,
            browser_manager=BrowserServiceManager(runtime_root=Path(runtime_root)),
        ),
    )
    build_lsp_plugin(runtime_root=Path(runtime_root)).register_with_core(core.context)
    for module_id in ("core", "execution", "artifact", "memory", l3_plugin.module_id, "web_search", "web_fetch", "lsp"):
        core.publish_module_capabilities(module_id)

    async def close() -> None:
        for handle in tuple(core.context.module_registry.modules.values()):
            shutdown_async = getattr(handle, "shutdown_async", None)
            shutdown_sync = getattr(handle, "shutdown_sync", None)
            if callable(shutdown_async):
                await shutdown_async()
            elif callable(shutdown_sync):
                shutdown_sync()
        database.close()

    return MinionRuntimeBundle(llm_runtime=llm_runtime, execution_runtime=core.context.execution_runtime, close_async=close)


def _minion_prompt_context(pack: TaskContextPack, *, metadata: dict[str, Any]) -> PromptAssemblyContext:
    return PromptAssemblyContext(
        core_mode="minion",
        turn_kind="minion",
        work_order_id=pack.work_order_id,
        metadata=dict(metadata),
    )


def _memory_candidates_from_l3(memory_l3: MockL3Plugin) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for record in list(getattr(memory_l3, "records", []) or []):
        if not isinstance(record, dict):
            continue
        item = {
            "document_id": str(record.get("document_id") or ""),
            "kind": str(record.get("document_kind") or record.get("kind") or ""),
            "scope": str(record.get("scope") or "task"),
            "title": str(record.get("title") or ""),
            "summary": str(record.get("summary") or ""),
            "search_text": str(record.get("search_text") or ""),
            "canonical_key": str(record.get("canonical_key")) if record.get("canonical_key") is not None else None,
            "dedupe_fingerprint": str(record.get("dedupe_fingerprint")) if record.get("dedupe_fingerprint") is not None else None,
            "topics": list(record.get("topics") or []),
            "payload": dict(record.get("payload") or {}),
            "source_kind": "minion_ephemeral_l3",
            "candidate_state": "candidate",
        }
        if item["summary"].strip() or item["title"].strip():
            result.append(item)
    return result


async def _minion_noop_failure_handler(*args: Any, **kwargs: Any) -> _MinionFailureResult:
    _ = args
    _ = kwargs
    return _MinionFailureResult(user_feedback="minion turn failed before a normal reply could be produced")


def _tool_protocol_transcript(messages: list[dict[str, Any]]) -> list[L1TranscriptMessage]:
    transcript: list[L1TranscriptMessage] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip()
        if role == "assistant" and message.get("tool_calls"):
            transcript.append(
                L1TranscriptMessage(
                    role="assistant",
                    content=str(message.get("content") or ""),
                    kind=L1MessageKind.ASSISTANT_TOOL_CALL,
                    tool_calls=[dict(item) for item in list(message.get("tool_calls") or []) if isinstance(item, dict)],
                )
            )
        elif role == "tool":
            transcript.append(
                L1TranscriptMessage(
                    role="tool",
                    content=str(message.get("content") or ""),
                    kind=L1MessageKind.TOOL_RESULT,
                    tool_call_id=str(message.get("tool_call_id") or ""),
                )
            )
    return transcript


def _llm_tools_for_allowed(execution_runtime: Any, allowed_capabilities: list[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    filtered = filter_minion_allowed_capabilities(allowed_capabilities)
    tool_surface = _minion_llm_tool_surface(filtered)
    for name in tool_surface:
        canonical = str(name or "").strip()
        if not canonical or canonical in seen:
            continue
        spec = execution_runtime.get_capability_spec(canonical)
        if spec is None:
            continue
        seen.add(canonical)
        result.append(
            {
                "type": "function",
                "function": {
                    "name": llm_tool_name(spec.get("name") or canonical),
                    "description": replace_internal_tool_names(
                        spec.get("description") or spec.get("display_name") or canonical
                    ),
                    "parameters": replace_internal_tool_names_in_value(
                        dict(spec.get("parameters_schema") or {"type": "object", "properties": {}})
                    ),
                },
            }
        )
    return result



def _minion_llm_tool_surface(allowed_capabilities: list[str]) -> list[str]:
    ordered = [
        name
        for name in (*MINION_DISCOVERY_TOOL_SURFACE, *MINION_DIRECT_WORK_TOOL_SURFACE)
        if name in allowed_capabilities
    ]
    seen = set(ordered)
    for name in allowed_capabilities:
        if name not in seen:
            ordered.append(name)
            seen.add(name)
    return ordered


def _expected_planner_plan_revision(pack: TaskContextPack) -> int:
    metadata = dict(pack.metadata or {})
    planner_work_order = dict(metadata.get("planner_work_order") or {})
    if not isinstance(planner_work_order.get("revision_source"), dict):
        return -1
    if "plan_revision" not in planner_work_order:
        return -1
    return _coerce_int(planner_work_order.get("plan_revision"), default=-1)


def _current_acceptance_criteria(pack: TaskContextPack, milestone: dict[str, Any]) -> list[str]:
    for key in ("acceptance_criteria", "acceptance"):
        value = milestone.get(key)
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item or "").strip()]
    return [str(item).strip() for item in list(pack.acceptance_criteria or []) if str(item or "").strip()]


def _tool_result_with_shell_mutation_violation(result: CanonicalToolResult, violation: dict[str, Any]) -> CanonicalToolResult:
    structured = dict(result.structured or {})
    existing = [dict(item) for item in list(structured.get("shell_mutation_violations") or []) if isinstance(item, dict)]
    existing.append(dict(violation))
    structured["shell_mutation_violations"] = existing
    structured["workspace_mutation_violation"] = dict(violation)
    structured["read_only_workspace_dirty"] = True
    warning = (
        "WARNING: this shell command changed an audited workspace "
        f"({violation.get('violation_id')}) at {violation.get('repo_path')}. "
        "Do not claim the reviewed workspace is clean unless you rerun from a clean checkout; "
        "include this mutation evidence in the final report."
    )
    text = _append_tool_result_warning(result.text or result.llm_text, warning)
    llm_text = _append_tool_result_warning(result.llm_text or result.text, warning)
    return replace(result, text=text, llm_text=llm_text, structured=structured)


def _append_tool_result_warning(text: str, warning: str) -> str:
    base = str(text or "").rstrip()
    if not base:
        return warning
    return f"{base}\n\n{warning}"


def _tool_result_text(result: CanonicalToolResult) -> str:
    return default_tool_result_text(result, fallback_ok="tool completed", fallback_error="tool failed")


def _is_truncation_finish_reason(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in {"length", "max_tokens", "max_output_tokens", "token_limit", "output_truncated"}


def _tool_call_summary(tool_call: CanonicalToolCall) -> dict[str, str]:
    return {
        "tool_name": str(tool_call.name or ""),
        "target_name": _effective_capability_name(tool_call),
        "call_id": str(tool_call.call_id or ""),
    }


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
    if phase_name == "milestone_finalizing":
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


def _fallback_minion_compaction_payload(source_text: str) -> dict[str, Any]:
    source_preview = _preview_text(source_text, limit=1200)
    summary = (
        "Minion run memory was compacted with a deterministic fallback because no model compaction "
        "method was available. Recheck the work order, plan artifact, current milestone, checkpoint "
        "ledger, and workspace before acting."
    )
    return {
        "schema": COMPACTION_SCHEMA_MINION_V1,
        "kind": "minion",
        "continuity": {
            "current_goal": "Continue the assigned minion task from current source-of-truth artifacts.",
            "completed_work": [],
            "current_position": source_preview,
            "next_actions": ["Reconstruct the current milestone state from the work order, plan, and workspace."],
            "open_questions": [],
            "risks": ["This fallback summary is conservative and must be verified against source-of-truth state."],
        },
        "summary": {
            "summary": summary,
            "search_text": f"{summary}\n{source_preview}".strip(),
        },
    }


def _optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _optional_positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _resolve_minion_max_output_tokens(llm_runtime: Any, pack: TaskContextPack) -> int:
    metadata = pack.metadata if isinstance(pack.metadata, dict) else {}
    explicit = _optional_positive_int(metadata.get("max_output_tokens"))
    if explicit is not None:
        return explicit
    preferred_endpoint_id = _preferred_endpoint_id_from_pack(pack)
    preferred_endpoint_source = _preferred_endpoint_source_from_pack(pack)
    resolved = _runtime_max_output_tokens(
        llm_runtime,
        preferred_endpoint_id=preferred_endpoint_id,
        preferred_endpoint_source=preferred_endpoint_source,
    )
    if resolved is not None:
        return resolved
    facts = _runtime_endpoint_facts(
        llm_runtime,
        preferred_endpoint_id=preferred_endpoint_id,
        preferred_endpoint_source=preferred_endpoint_source,
    )
    fact_max = _optional_positive_int(facts.get("max_output_tokens")) if facts else None
    if fact_max is not None:
        return fact_max
    context_window = _optional_positive_int(facts.get("context_window")) if facts else None
    if context_window is not None:
        return _max_output_tokens_from_context_window(context_window, llm_runtime)
    config = getattr(llm_runtime, "config", None)
    return _optional_positive_int(getattr(config, "fallback_max_output_tokens", None)) or 4096


def _minion_llm_request_metadata(pack: TaskContextPack, run_id: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "endpoint_fallback_policy": "none",
        "response_mode_hint": "operational",
        "minion_run_id": str(run_id or ""),
        "max_output_tokens_source": "minion",
    }
    preferred_endpoint_id = _preferred_endpoint_id_from_pack(pack)
    if preferred_endpoint_id:
        metadata["preferred_endpoint_id"] = preferred_endpoint_id
        preferred_endpoint_source = _preferred_endpoint_source_from_pack(pack)
        if preferred_endpoint_source:
            metadata["preferred_endpoint_source"] = preferred_endpoint_source
    timeout_seconds = _minion_llm_request_timeout_seconds(pack)
    if timeout_seconds is not None:
        metadata["timeout_seconds"] = timeout_seconds
    return metadata


def _minion_llm_request_timeout_seconds(pack: TaskContextPack) -> float | None:
    pack_metadata = pack.metadata if isinstance(pack.metadata, dict) else {}
    explicit = _optional_positive_float(pack_metadata.get("timeout_seconds"))
    if explicit is not None:
        return explicit
    return _optional_positive_float(pack_metadata.get("llm_round_timeout_seconds"))


def _preferred_endpoint_id_from_pack(pack: TaskContextPack) -> str | None:
    metadata = pack.metadata if isinstance(pack.metadata, dict) else {}
    value = str(metadata.get("preferred_endpoint_id") or "").strip()
    return value or None


def _preferred_endpoint_source_from_pack(pack: TaskContextPack) -> str | None:
    metadata = pack.metadata if isinstance(pack.metadata, dict) else {}
    value = str(metadata.get("preferred_endpoint_source") or "").strip()
    return value or None


def _runtime_max_output_tokens(
    llm_runtime: Any,
    *,
    preferred_endpoint_id: str | None = None,
    preferred_endpoint_source: str | None = None,
) -> int | None:
    resolver = getattr(llm_runtime, "resolve_max_output_tokens", None)
    if not callable(resolver):
        return None
    with contextlib.suppress(Exception):
        try:
            return _optional_positive_int(
                resolver(preferred_endpoint_id=preferred_endpoint_id, preferred_endpoint_source=preferred_endpoint_source)
            )
        except TypeError:
            try:
                return _optional_positive_int(resolver(preferred_endpoint_id=preferred_endpoint_id))
            except TypeError:
                return _optional_positive_int(resolver())
    return None


def _runtime_endpoint_facts(
    llm_runtime: Any,
    *,
    preferred_endpoint_id: str | None = None,
    preferred_endpoint_source: str | None = None,
) -> dict[str, Any]:
    resolver = getattr(llm_runtime, "resolve_endpoint_facts", None)
    if not callable(resolver):
        return {}
    with contextlib.suppress(Exception):
        try:
            facts = resolver(preferred_endpoint_id=preferred_endpoint_id, preferred_endpoint_source=preferred_endpoint_source)
        except TypeError:
            try:
                facts = resolver(preferred_endpoint_id=preferred_endpoint_id)
            except TypeError:
                facts = resolver()
        return dict(facts) if isinstance(facts, dict) else {}
    return {}


def _max_output_tokens_from_context_window(context_window: int, llm_runtime: Any) -> int:
    config = getattr(llm_runtime, "config", None)
    cap = _optional_positive_int(getattr(config, "default_max_output_tokens", None)) or 25_000
    floor = _optional_positive_int(getattr(config, "fallback_max_output_tokens", None)) or 4096
    margin_factor = float(getattr(config, "context_margin_factor", 0.05) or 0.05)
    margin_cap = _optional_positive_int(getattr(config, "context_margin_cap", None)) or 16_384
    margin_min = _optional_positive_int(getattr(config, "context_margin_min", None)) or 1024
    margin = min(margin_cap, max(margin_min, int(context_window * margin_factor)))
    usable = max(512, context_window - margin)
    context_fraction = max(512, context_window // 4)
    return max(512, min(cap, max(floor, context_fraction), usable))


def _is_shell_capability_name(name: object) -> bool:
    return str(name or "").strip() in {"op_exec_shell", "run_shell", "shell"}


def _is_git_capability_name(name: object) -> bool:
    return str(name or "").strip() in {"op_git", "git"}


def _web_research_capability_name(name: object) -> str | None:
    normalized = str(name or "").strip()
    if normalized in {"op_web_search", "search_web", "web_search"}:
        return "op_web_search"
    if normalized in {"op_web_read", "read_web", "web_read"}:
        return "op_web_read"
    return None


def _web_research_budget_keys(canonical_name: str) -> tuple[str, ...]:
    if canonical_name == "op_web_search":
        return ("op_web_search", "search_web", "web_search", "search")
    if canonical_name == "op_web_read":
        return ("op_web_read", "read_web", "web_read", "read")
    return (canonical_name,)


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number


_GIT_MUTATION_COMMANDS = {
    "add",
    "checkout",
    "cherry-pick",
    "clean",
    "commit",
    "merge",
    "mv",
    "push",
    "rebase",
    "reset",
    "restore",
    "revert",
    "rm",
    "stash",
    "switch",
    "tag",
}
_GIT_GLOBAL_OPTIONS_WITH_VALUE = {
    "-C",
    "-c",
    "--exec-path",
    "--git-dir",
    "--namespace",
    "--super-prefix",
    "--work-tree",
}
_GIT_BRANCH_MUTATING_OPTIONS = {
    "-c",
    "-C",
    "-d",
    "-D",
    "-f",
    "-m",
    "-M",
    "-t",
    "-u",
    "--copy",
    "--delete",
    "--edit-description",
    "--force",
    "--move",
    "--no-track",
    "--set-upstream-to",
    "--track",
    "--unset-upstream",
}
_GIT_BRANCH_READ_ONLY_OPTIONS = {
    "-a",
    "-l",
    "-r",
    "-v",
    "-vv",
    "--all",
    "--color",
    "--column",
    "--contains",
    "--format",
    "--ignore-case",
    "--list",
    "--merged",
    "--no-color",
    "--no-column",
    "--no-contains",
    "--no-merged",
    "--points-at",
    "--remotes",
    "--show-current",
    "--sort",
    "--verbose",
}


def _git_command_is_mutating(cmd: str) -> bool:
    try:
        tokens = shlex.split(str(cmd or ""))
    except ValueError:
        tokens = str(cmd or "").split()
    if tokens and tokens[0] == "git":
        tokens = tokens[1:]
    subcommand, args = _git_subcommand_from_tokens(tokens)
    if not subcommand:
        return False
    if subcommand == "branch":
        return _git_branch_args_mutate(args)
    return subcommand in _GIT_MUTATION_COMMANDS


def _git_subcommand_from_tokens(tokens: list[str]) -> tuple[str, list[str]]:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if token in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if any(token.startswith(option + "=") for option in _GIT_GLOBAL_OPTIONS_WITH_VALUE if option.startswith("--")):
            index += 1
            continue
        if token.startswith("-C") and token != "-C":
            index += 1
            continue
        if token.startswith("-c") and token != "-c":
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token, tokens[index + 1 :]
    if index < len(tokens):
        return tokens[index], tokens[index + 1 :]
    return "", []


def _git_branch_args_mutate(args: list[str]) -> bool:
    if not args:
        return False
    if any(arg in _GIT_BRANCH_MUTATING_OPTIONS for arg in args):
        return True
    for arg in args:
        if not arg.startswith("-"):
            return True
        option = arg.split("=", 1)[0]
        if option not in _GIT_BRANCH_READ_ONLY_OPTIONS:
            return True
    return False


def _git_workspace_snapshot(repo_path: Path) -> dict[str, Any]:
    repo = Path(repo_path)
    if not (repo / ".git").exists():
        return {}
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, timeout=10)
        status = subprocess.run(["git", "status", "--porcelain", "--ignored"], cwd=str(repo), capture_output=True, text=True, timeout=10)
    except Exception:
        return {}
    if head.returncode != 0 or status.returncode != 0:
        return {}
    return {
        "repo_path": str(repo),
        "head": head.stdout.strip(),
        "status": status.stdout.strip(),
    }


_FILE_TREE_IGNORED_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "target",
    "htmlcov",
    "coverage",
}
_FILE_TREE_IGNORED_SUFFIXES = {".pyc", ".pyo", ".coverage"}


def _file_tree_snapshot(root_path: Path) -> dict[str, Any]:
    root = Path(root_path)
    if not root.exists() or not root.is_dir():
        return {}
    entries: dict[str, str] = {}
    try:
        paths = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
    except Exception:
        return {}
    for path in paths:
        try:
            rel_path = path.relative_to(root)
        except ValueError:
            continue
        rel = rel_path.as_posix()
        parts = rel_path.parts
        if any(part in _FILE_TREE_IGNORED_DIRS or part.endswith(".egg-info") for part in parts):
            continue
        if path.is_dir():
            continue
        if path.suffix in _FILE_TREE_IGNORED_SUFFIXES:
            continue
        try:
            if path.is_symlink():
                entries[rel] = f"symlink:{path.readlink()}"
                continue
            if not path.is_file():
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries[rel] = f"file:{path.stat().st_size}:{digest}"
        except Exception:
            entries[rel] = "unreadable"
    tree_digest = hashlib.sha256(json.dumps(entries, sort_keys=True).encode("utf-8")).hexdigest()
    return {"root_path": str(root), "digest": tree_digest, "entries": entries}


def _shell_audit_after_snapshot(before: dict[str, Any]) -> dict[str, Any]:
    if str(before.get("snapshot_kind") or "") == "read_only_repo":
        snapshot = {
            "snapshot_kind": "read_only_repo",
            "repo_path": str(before.get("repo_path") or ""),
            "source_git": _git_workspace_snapshot(Path(str(before.get("repo_path") or ""))),
        }
        scratch_repo = str(before.get("review_scratch_repo_path") or "").strip()
        if scratch_repo:
            snapshot["review_scratch_repo_path"] = scratch_repo
            snapshot["scratch_tree"] = _file_tree_snapshot(Path(scratch_repo))
        return snapshot
    repo_path = str(before.get("repo_path") or "").strip()
    return _git_workspace_snapshot(Path(repo_path)) if repo_path else {}


def _shell_audit_snapshot_changed_meaningfully(before: dict[str, Any], after: dict[str, Any]) -> bool:
    if not before or not after:
        return False
    if str(before.get("snapshot_kind") or "") == "read_only_repo":
        source_changed = _git_workspace_snapshot_changed_meaningfully(
            dict(before.get("source_git") or {}),
            dict(after.get("source_git") or {}),
        )
        scratch_changed = _file_tree_snapshot_changed_meaningfully(
            dict(before.get("scratch_tree") or {}),
            dict(after.get("scratch_tree") or {}),
        )
        return source_changed or scratch_changed
    return _git_workspace_snapshot_changed_meaningfully(before, after)


def _shell_audit_changed_root_path(before: dict[str, Any], after: dict[str, Any]) -> str:
    if str(before.get("snapshot_kind") or "") != "read_only_repo":
        return str(before.get("repo_path") or "")
    source_changed = _git_workspace_snapshot_changed_meaningfully(
        dict(before.get("source_git") or {}),
        dict(after.get("source_git") or {}),
    )
    scratch_changed = _file_tree_snapshot_changed_meaningfully(
        dict(before.get("scratch_tree") or {}),
        dict(after.get("scratch_tree") or {}),
    )
    if source_changed:
        return str(before.get("repo_path") or "")
    if scratch_changed:
        return str(before.get("review_scratch_repo_path") or "")
    return str(before.get("repo_path") or before.get("review_scratch_repo_path") or "")


def _file_tree_snapshot_changed_meaningfully(before: dict[str, Any], after: dict[str, Any]) -> bool:
    if not before or not after:
        return False
    return str(before.get("digest") or "") != str(after.get("digest") or "")


def _git_workspace_snapshot_changed_meaningfully(before: dict[str, Any], after: dict[str, Any]) -> bool:
    if not before or not after:
        return False
    if str(before.get("head") or "") != str(after.get("head") or ""):
        return True
    return _git_status_without_ignored(before.get("status")) != _git_status_without_ignored(after.get("status"))


def _git_status_without_ignored(status: Any) -> str:
    lines = []
    for line in str(status or "").splitlines():
        if line.startswith("!! "):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _extract_ask_user_question_payload(text: str) -> dict[str, Any]:
    loaded = _try_extract_json(str(text or "").strip())
    if not isinstance(loaded, dict):
        return {}
    output_type = str(loaded.get("type") or loaded.get("output_type") or "").strip().lower()
    if output_type not in {"ask_user_question", "clarification_request"} and "questions" not in loaded:
        return {}
    questions = [dict(item) for item in list(loaded.get("questions") or []) if isinstance(item, dict)]
    if not questions:
        return {}
    payload = {
        "type": "ask_user_question",
        "task_id": str(loaded.get("task_id") or ""),
        "work_order_id": str(loaded.get("work_order_id") or ""),
        "turn_index": loaded.get("turn_index", 0),
        "plan_revision": loaded.get("plan_revision", 0),
        "plan_draft_id": str(loaded.get("plan_draft_id") or ""),
        "questions": questions[:3],
    }
    return payload


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _extract_lessons_and_clean_summary(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    lessons = {"task_lessons": [], "system_lessons": []}
    if not raw:
        return {"summary": "", **lessons}
    loaded = _try_extract_json(raw)
    if isinstance(loaded, dict):
        lessons["task_lessons"].extend(_string_items(loaded.get("task_lessons") or loaded.get("taskLessons") or loaded.get("task_lessons_to_remember")))
        lessons["system_lessons"].extend(_string_items(loaded.get("system_lessons") or loaded.get("systemLessons") or loaded.get("system_lesson_candidates")))
        summary = str(loaded.get("summary") or loaded.get("final_summary") or loaded.get("result") or raw).strip()
        return {"summary": summary, **{key: _dedupe_nonempty(value) for key, value in lessons.items()}}

    current: str | None = None
    summary_lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip().strip("-* ")
        heading = _lesson_heading_kind(stripped)
        if heading == "task_lessons":
            current = "task_lessons"
            value = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
        elif heading == "system_lessons":
            current = "system_lessons"
            value = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
        elif current and stripped:
            value = stripped
        else:
            current = None
            value = ""
            summary_lines.append(line.rstrip())
        if current and value and value.lower() not in {"none", "n/a"}:
            lessons[current].append(value)
    summary_text = "\n".join(summary_lines).strip()
    return {"summary": summary_text, **{key: _dedupe_nonempty(value) for key, value in lessons.items()}}


def _compact_preview_text(value: str) -> str:
    lines: list[str] = []
    blank_pending = False
    for raw_line in str(value or "").strip().splitlines():
        line = " ".join(raw_line.strip().split())
        if not line:
            blank_pending = bool(lines)
            continue
        if blank_pending:
            lines.append("")
            blank_pending = False
        lines.append(line)
    return "\n".join(lines).strip()


def _strip_single_json_code_fence(value: str) -> str:
    raw = str(value or "").strip()
    if not raw.startswith("```"):
        return raw
    lines = raw.splitlines()
    if not lines or not lines[0].strip().startswith("```"):
        return raw
    opener = lines[0].strip().lower()
    if opener not in {"```", "```json", "```jsonc"}:
        return raw
    if len(lines) < 2 or not lines[-1].strip().startswith("```"):
        return raw
    return "\n".join(lines[1:-1]).strip()


def _lesson_heading_kind(text: str) -> str:
    normalized = str(text or "").strip().strip("#*_` ")
    while normalized and not (normalized[0].isalnum() or normalized[0] == "_"):
        normalized = normalized[1:].strip()
    lowered = normalized.lower().replace("_", " ")
    lowered = lowered.rstrip(":").strip()
    if lowered in {"task lesson", "task lessons", "task wise lessons", "task-wise lessons"}:
        return "task_lessons"
    if lowered in {"system lesson", "system lessons", "system wise lessons", "system-wise lessons"}:
        return "system_lessons"
    if lowered.startswith(("task lesson:", "task lessons:", "task wise lessons:", "task-wise lessons:")):
        return "task_lessons"
    if lowered.startswith(("system lesson:", "system lessons:", "system wise lessons:", "system-wise lessons:")):
        return "system_lessons"
    return ""


def _extract_lessons(text: str) -> dict[str, list[str]]:
    payload = _extract_lessons_and_clean_summary(text)
    return {
        "task_lessons": list(payload.get("task_lessons") or []),
        "system_lessons": list(payload.get("system_lessons") or []),
    }
    raw = str(text or "").strip()
    lessons = {"task_lessons": [], "system_lessons": []}
    if not raw:
        return lessons
    loaded = _try_extract_json(raw)
    if isinstance(loaded, dict):
        lessons["task_lessons"].extend(_string_items(loaded.get("task_lessons") or loaded.get("taskLessons") or loaded.get("task_lessons_to_remember")))
        lessons["system_lessons"].extend(_string_items(loaded.get("system_lessons") or loaded.get("systemLessons") or loaded.get("system_lesson_candidates")))
    current: str | None = None
    for line in raw.splitlines():
        stripped = line.strip().strip("-* ")
        lowered = stripped.lower()
        if lowered.startswith(("task lesson:", "task lessons:", "task_lessons:", "task-wise lessons:", "task wise lessons:")):
            current = "task_lessons"
            value = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
        elif lowered.startswith(("system lesson:", "system lessons:", "system_lessons:", "system-wise lessons:", "system wise lessons:")):
            current = "system_lessons"
            value = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
        elif current and stripped:
            value = stripped
        else:
            value = ""
        if current and value and value.lower() not in {"none", "n/a", "无", "没有"}:
            lessons[current].append(value)
    return {key: _dedupe_nonempty(value) for key, value in lessons.items()}


def _try_extract_json(text: str) -> Any:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except Exception:
        return None


def _string_items(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []


def _dedupe_nonempty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = " ".join(str(value or "").split())
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
