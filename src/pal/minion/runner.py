from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import os
import re
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from uuid import uuid4

from pal.artifact import ArtifactManager, ArtifactRepository, register_with_core as register_artifact_with_core
from pal.core import PalCore
from pal.core.prompt_compiler import PromptCompiler
from pal.core.prompt_fragment_registry import PromptFragmentRegistry
from pal.core.runtime_config import RuntimeConfig
from pal.core.prompt_debug_log import (
    append_prompt_debug_log,
    render_llm_outcome_debug_log,
    render_prompt_debug_log,
    render_reply_debug_log,
)
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
from pal.execution import (
    ApprovalExecutionDecorator,
    CapabilityCall,
    ExecutionApprovalRequest,
    register_with_core as register_execution_with_core,
)
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
from pal.memory import (
    CompactionProfile,
    L1MessageKind,
    L1TranscriptMessage,
    L3ProviderSelector,
    MemoryCommitRequest,
    MemoryCompactRequest,
    MemoryPackRequest,
    MemoryService,
    build_ollama_embedding_provider_from_config,
    register_with_core as register_memory_with_core,
)
from pal.memory.prompt import MemoryPromptFragmentProvider
from pal.memory.tool_protocol import l1_tool_protocol_transcript
from pal.minion.compact import (
    build_minion_prior_user_inputs_compaction_payload,
    build_minion_prior_user_inputs_source_text,
    compact_minion_memory_service,
)
from pal.minion.debug_log import minion_debug_log_enabled
from pal.minion.llm_broker import MinionBrokerLLMRuntime
from pal.minion.web_broker import MinionBrokerWebClient
from pal.minion.profiles import filter_minion_allowed_capabilities
from pal.minion.scoped_execution import (
    MinionScopedExecutionRuntime,
    _effective_capability_name,
    _effective_tool_args,
    _path_is_relative_to,
    _review_tool_evidence_ref,
)
from pal.minion.tool_admission import admit_minion_tool_call
from pal.minion.workspace_tools import _append_unique_artifact, _write_minion_artifact
from pal.minion.prompt_adapter import (
    build_minion_task_envelope as _minion_task_envelope,
    MinionPromptFragmentProvider,
    minion_primary_input as _minion_primary_input,
    prompt_scaffold_summary as _prompt_scaffold_summary,
    prompt_view_from_pack as _prompt_view_from_pack,
)
from pal.minion.user_interaction import (
    MinionUserInteractionPort,
    ask_user_question_summary as _ask_user_question_summary,
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
    MinionInvocationPack,
    default_tool_result_text,
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
    memory_service: MemoryService
    config: RuntimeConfig | None = None
    close_async: Callable[[], Awaitable[None]] | None = None

    async def close(self) -> None:
        if self.close_async is not None:
            await self.close_async()


@dataclass
class _MinionTurnContext:
    execution_runtime: Any
    prompt_fragment_registry: PromptFragmentRegistry
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


class _MinionCooperativeRestart(Exception):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("summary") or "minion suspended for manager restart"))
        self.payload = dict(payload)


class _MinionLLMRuntimeAdapter:
    def __init__(self, runner: "MinionRunner", base_runtime: Any, state: "MinionAgentLoopState") -> None:
        self._runner = runner
        self._base = base_runtime
        self._state = state

    @property
    def supports_streaming(self) -> bool:
        supports_streaming = getattr(self._base, "supports_streaming", None)
        if callable(supports_streaming):
            try:
                supports_streaming = supports_streaming()
            except Exception:
                supports_streaming = False
        if supports_streaming is not None:
            return bool(supports_streaming) and (
                callable(getattr(self._base, "agenerate_stream", None))
                or callable(getattr(self._base, "generate_stream", None))
            )
        return callable(getattr(self._base, "agenerate_stream", None)) or callable(getattr(self._base, "generate_stream", None))

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

    def generate_stream(self, request: CanonicalLLMRequest) -> list[Any]:
        method = getattr(self._base, "generate_stream", None)
        if not callable(method):
            raise AttributeError("wrapped minion LLM runtime does not support generate_stream")
        result = method(request)
        return list(result or [])

    async def agenerate_stream(self, request: CanonicalLLMRequest) -> list[Any]:
        await self._runner._emit_progress(
            "llm_round_started",
            round=self._state.llm_round_count,
            tool_call_count=self._state.tool_call_count,
            tool_count=len(list(request.tools or [])),
        )
        restore_event_sink = self._install_llm_progress_sink()
        async_method = getattr(self._base, "agenerate_stream", None)
        try:
            if callable(async_method):
                awaitable = async_method(request)
            else:
                sync_method = getattr(self._base, "generate_stream", None)
                if not callable(sync_method):
                    raise AttributeError("wrapped minion LLM runtime does not support generate_stream")
                awaitable = asyncio.to_thread(sync_method, request)
            events = await self._runner._await_with_progress_heartbeat(
                awaitable,
                phase="llm_round_waiting",
                round=self._state.llm_round_count,
                tool_call_count=self._state.tool_call_count,
            )
            return list(events or [])
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
        return {}

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

    def build_compaction_payload(
        self,
        *,
        target_input_budget: int,
        reserved_output_tokens: int = 0,
        profile: CompactionProfile = CompactionProfile.MINION,
    ) -> dict[str, Any]:
        _ = reserved_output_tokens
        if profile != CompactionProfile.MINION and str(profile or "").strip().lower() != CompactionProfile.MINION.value:
            return {}
        items = getattr(getattr(self._state.memory_service, "l1_store", None), "items", [])
        return build_minion_prior_user_inputs_compaction_payload(items, target_input_budget=target_input_budget)

    def compact(self, request: MemoryCompactRequest):
        return compact_minion_memory_service(self._state.memory_service, request)

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

    def project_llm_text(self, value: object) -> str:
        return self._state.execution_runtime.project_llm_text(value)

    def project_llm_value(self, value: Any) -> Any:
        return self._state.execution_runtime.project_llm_value(value)

    def reconcile_tool_context(self, **kwargs: Any) -> Any:
        reconcile = getattr(
            self._state.execution_runtime,
            "reconcile_tool_context",
            None,
        )
        return reconcile(**kwargs) if callable(reconcile) else None

    async def execute_tool_async(
        self,
        call: CanonicalToolCall,
        *,
        allow_tools: bool = True,
        budget: Any = None,
        turn_id: str | None = None,
    ) -> CanonicalToolResult:
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
                self._runner._execute_allowed_tool(
                    self._state.execution_runtime,
                    call,
                    allow_tools=allow_tools,
                    budget=budget,
                    turn_id=turn_id or self._continuation.turn_id,
                ),
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
        self._runner._observed_tool_call_count = max(
            self._runner._observed_tool_call_count,
            self._state.tool_call_count,
        )
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
    memory_candidate_sink: MockL3Plugin
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
    pack: MinionInvocationPack
    minion_id: str
    run_id: str
    write_event: EventWriter
    read_decision: DecisionReader
    runtime_bundle: MinionRuntimeBundle | None = None
    blocked_summary: str = ""
    blocked_kind: str = ""
    produced_artifacts: list[dict[str, Any]] = field(default_factory=list)
    memory_candidates: list[dict[str, Any]] = field(default_factory=list)
    review_tool_evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    web_research_usage: dict[str, int] = field(default_factory=dict)
    auto_accept_approvals: bool = False
    user_interaction: MinionUserInteractionPort | None = field(default=None, init=False, repr=False)
    _memory_candidate_sink: MockL3Plugin | None = field(default=None, init=False, repr=False)
    _pending_control_messages: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _cancel_requested: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _restart_requested: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _agent_session_checkpoint: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _visible_capability_aliases: list[str] = field(default_factory=list, init=False, repr=False)
    _observed_tool_call_count: int = field(default=0, init=False, repr=False)
    _manager_submission_receipt_observed: bool = field(default=False, init=False, repr=False)

    async def run(self) -> int:
        bundle: MinionRuntimeBundle | None = None
        prompt_observation_tag = _prompt_observation_tag_from_pack(self.pack)
        runner_started_payload = {
            "goal": self.pack.goal,
            "instruction": self.pack.instruction,
            "allowed_capabilities": list(self.pack.allowed_capabilities),
        }
        if prompt_observation_tag:
            runner_started_payload["prompt_observation_tag"] = prompt_observation_tag
        self._append_debug_log("runner_started", runner_started_payload)
        try:
            bundle = self.runtime_bundle or build_slim_minion_runtime(self.runtime_root, run_id=self.run_id)
            accepted_payload = {
                "phase": "accepted",
                "summary": "minion accepted task context",
                "prompt_scaffold_summary": _prompt_scaffold_summary(self._prompt_scaffold()),
            }
            if prompt_observation_tag:
                accepted_payload["prompt_observation_tag"] = prompt_observation_tag
            await self._emit(
                "phase_started",
                accepted_payload,
            )
            return await self._run_v2_invocation(bundle, prompt_observation_tag=prompt_observation_tag)
        except _MinionCooperativeCancel as cancel:
            with contextlib.suppress(Exception):
                await self._emit("terminal", self._cancel_terminal_payload(cancel.payload))
            return 0
        except _MinionCooperativeRestart as restart:
            with contextlib.suppress(Exception):
                await self._emit("terminal", self._restart_terminal_payload(restart.payload))
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

    async def _run_v2_invocation(
        self,
        bundle: MinionRuntimeBundle,
        *,
        prompt_observation_tag: str = "",
    ) -> int:
        payload = {
            "phase": "invocation_started",
            "summary": "Minion V2 worker invocation started",
        }
        if prompt_observation_tag:
            payload["prompt_observation_tag"] = prompt_observation_tag
        await self._emit("phase_started", payload)
        retry_note = ""
        while True:
            await self._raise_if_cancel_requested()
            await self._raise_if_restart_requested()
            progress_before = self._completion_gate_progress_marker()
            final_text = await self._run_agent_loop(bundle, forced_retry_note=retry_note)
            await self._raise_if_cancel_requested()
            await self._raise_if_restart_requested()
            if self.blocked_summary:
                await self._emit("terminal", self._terminal_payload("blocked", self.blocked_summary))
                return 0
            if not self._required_primary_artifact_name() or self._completion_evidence_present():
                break
            progress_after = self._completion_gate_progress_marker()
            if retry_note and progress_after == progress_before:
                self.blocked_kind = "completion_gate_stalled"
                self.blocked_summary = (
                    "completion gate stalled: the required primary artifact is still absent after explicit "
                    "submit feedback, and the worker made no capability or artifact progress"
                )
                await self._emit_progress(
                    "completion_gate_stalled",
                    round=0,
                    summary=self.blocked_summary,
                )
                await self._emit(
                    "terminal",
                    self._terminal_payload("blocked", self.blocked_summary),
                )
                return 0
            retry_note = self._missing_completion_evidence_feedback()
            await self._emit_progress(
                "completion_gate_rejected",
                round=0,
                summary=retry_note,
            )
        await self._persist_text_deliverable_if_needed(final_text)
        self._finalize_produced_artifacts()
        await self._emit(
            "terminal",
            self._terminal_payload("completed", final_text or "Minion V2 invocation completed"),
        )
        return 0

    def _completion_gate_progress_marker(self) -> tuple[int, tuple[tuple[str, str], ...]]:
        tool_call_count = max(
            self._observed_tool_call_count,
            int(self._agent_session_checkpoint.get("tool_call_count") or 0),
        )
        artifacts = tuple(
            sorted(
                (
                    str(item.get("role") or ""),
                    str(
                        item.get("relative_path")
                        or item.get("requested_relative_path")
                        or item.get("path")
                        or ""
                    ),
                )
                for item in self.produced_artifacts
            )
        )
        return tool_call_count, artifacts

    def _missing_completion_evidence_feedback(self) -> str:
        primary = self._required_primary_artifact_name()
        target = f"the required primary artifact {primary!r}" if primary else "required submit evidence"
        submit_tool = {
            "architecture_submission.json": "architecture_submit",
            "architecture_review.json": "architecture_review_submit",
            "coder_report.json": "candidate_submit",
            "producer_report.json": "candidate_submit",
            "verification_plan.json": "verification_submit",
            "verification_submission.json": "a semantic verification outcome tool",
            "standalone_review.json": "review_submit",
        }.get(primary, "the bound role-specific submit tool")
        return (
            f"Completion gate rejected the final response because {target} is absent. "
            f"Continue in this same invocation, correct any prior submit rejection, and call {submit_tool}. "
            "Do not answer with another completion summary until that submit call succeeds."
        )

    def _required_primary_artifact_name(self) -> str:
        output_policy = dict((self.pack.workspace or {}).get("output_policy") or {})
        if not output_policy:
            output_policy = dict((self.pack.resolved_profile or {}).get("effective_output_policy") or {})
        return str(output_policy.get("primary_artifact") or "").strip()

    async def _run_agent_loop(self, bundle: MinionRuntimeBundle, *, forced_retry_note: str = "") -> str:
        memory_service = bundle.memory_service
        memory_candidate_sink = self._runner_memory_candidate_sink()
        workspace = dict(self.pack.workspace)
        workspace.setdefault("runtime_root", str(self.runtime_root))
        workspace.setdefault("run_id", self.run_id)
        workspace.setdefault("minion_id", self.minion_id)
        workspace.setdefault("minion_profile", self.pack.minion_profile)
        workspace.setdefault("invocation_id", self.pack.invocation_id)
        workspace.setdefault("goal", self.pack.instruction or self.pack.goal)
        if isinstance(self.pack.metadata, dict):
            if isinstance(self.pack.metadata.get("requirements_brief"), dict):
                workspace.setdefault("requirements_brief", dict(self.pack.metadata.get("requirements_brief") or {}))
            if isinstance(self.pack.metadata.get("minion_v2"), dict):
                workspace.setdefault("minion_v2", dict(self.pack.metadata.get("minion_v2") or {}))
        workspace.setdefault("review_tool_evidence_refs", self.review_tool_evidence_refs)
        execution_runtime = MinionScopedExecutionRuntime(
            bundle.execution_runtime,
            self.pack.allowed_capabilities,
            workspace,
            produced_artifacts=self.produced_artifacts,
            capability_guidance_overrides=dict(
                dict(self.pack.resolved_profile or {}).get("capability_guidance_overrides") or {}
            ),
            request_user_clarification=self._request_architecture_clarification,
        )
        self._visible_capability_aliases = [
            str(dict(spec.get("function") or {}).get("name") or "").strip()
            for spec in execution_runtime.build_llm_tool_contracts()
            if str(dict(spec.get("function") or {}).get("name") or "").strip()
        ]
        session_metadata = dict((self.pack.metadata or {}).get("agent_session") or {})
        session_id = str(session_metadata.get("session_id") or "").strip()
        restored = self._load_agent_session_checkpoint(workspace, session_id=session_id)
        initial_instruction = str(restored.get("initial_instruction") or self.pack.instruction or self.pack.goal).strip()
        current_channel_envelope = _minion_task_envelope(
            self.pack,
            minion_id=self.minion_id,
            run_id=self.run_id,
        )
        prompt_pack = self.pack
        if initial_instruction and initial_instruction != str(self.pack.instruction or ""):
            prompt_pack = MinionInvocationPack.from_dict(
                {**self.pack.to_dict(), "goal": initial_instruction, "instruction": initial_instruction}
            )
        channel_envelope = _minion_task_envelope(prompt_pack, minion_id=self.minion_id, run_id=self.run_id)
        tool_protocol_messages = [
            dict(item)
            for item in list(restored.get("tool_protocol_messages") or [])
            if isinstance(item, dict)
        ]
        response_keys = [str(item) for item in list(restored.get("response_keys") or []) if str(item)]
        response_key = str(session_metadata.get("response_key") or "").strip()
        if restored and response_key and response_key not in response_keys:
            response_text = self._new_session_response_message(
                restored,
                response_key=response_key,
                response_text=_minion_primary_input(current_channel_envelope),
            )
            if response_text:
                tool_protocol_messages.append({"role": "user", "content": response_text})
            response_keys.append(response_key)
        elif not restored and response_key:
            response_keys.append(response_key)
        state = MinionAgentLoopState(
            execution_runtime=execution_runtime,
            memory_service=memory_service,
            memory_candidate_sink=memory_candidate_sink,
            channel_envelope=channel_envelope,
            tool_protocol_messages=tool_protocol_messages,
            llm_round_count=max(0, int(restored.get("llm_round_count") or 0)),
            tool_call_count=max(0, int(restored.get("tool_call_count") or 0)),
        )
        self._observed_tool_call_count = max(
            self._observed_tool_call_count,
            state.tool_call_count,
        )
        max_output_tokens = _resolve_minion_max_output_tokens(bundle.llm_runtime, self.pack)

        forced_retry_note = str(forced_retry_note or "").strip()

        def build_context(frame: AgentLoopFrame):
            retry_note = str(frame.retry_note or forced_retry_note or "")
            metadata = {
                "retry_note": retry_note,
            }
            return _minion_prompt_context(self.pack, event=state.channel_envelope.event, metadata=metadata)

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

        turn_id = f"{self.run_id}:invocation"
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
            turn_settings_snapshot=_minion_turn_settings_snapshot(self.pack),
            tool_protocol_messages=state.tool_protocol_messages,
            tool_delivery_records={
                str(key): dict(value)
                for key, value in dict(
                    restored.get("tool_delivery_records") or {}
                ).items()
                if str(key) and isinstance(value, dict)
            },
            tool_batch_count=max(0, int(restored.get("tool_batch_count") or 0)),
            preferred_llm_endpoint_id=str(restored.get("preferred_llm_endpoint_id") or "") or None,
            preferred_llm_model_id=str(restored.get("preferred_llm_model_id") or "") or None,
        )
        self._persist_agent_session_checkpoint(
            workspace,
            state,
            continuation,
            initial_instruction=initial_instruction,
            response_keys=response_keys,
        )
        begin_tool_results = getattr(state.execution_runtime, "begin_tool_result_turn", None)
        if callable(begin_tool_results):
            begin_tool_results(
                turn_id=turn_id,
                scope_key=session_id or f"minion:{self.run_id}",
                input_id=response_key or turn_id,
            )
        executor = self._build_minion_turn_executor(bundle, state, continuation)
        current: EffectResult | None = None
        while True:
            await self._raise_if_cancel_requested()
            if self._continuation_is_restart_safe(continuation):
                await self._raise_if_restart_requested()
            try:
                yielded = program.send(current) if current is not None else next(program)
            except StopIteration as completed:
                outcome = completed.value
                if not isinstance(outcome, TurnOutcome):
                    raise RuntimeError("minion agent loop ended without a turn outcome")
                if not self.blocked_summary:
                    await self._emit_progress(
                        "invocation_finalizing",
                        round=state.llm_round_count,
                        tool_call_count=state.tool_call_count,
                    )
                await self._commit_minion_l1(state, outcome.commit_payload.transcript)
                self.memory_candidates = _memory_candidates_from_sink(state.memory_candidate_sink)
                final_reply = str(outcome.final_reply or "").strip()
                if final_reply:
                    continuation.tool_protocol_messages.append({"role": "assistant", "content": final_reply})
                    self._sync_minion_state_from_continuation(state, continuation)
                self._persist_agent_session_checkpoint(
                    workspace,
                    state,
                    continuation,
                    initial_instruction=initial_instruction,
                    response_keys=response_keys,
                )
                await self._raise_if_cancel_requested()
                await self._raise_if_restart_requested()
                return outcome.final_reply
            current = await self._execute_minion_agent_effect(
                executor,
                continuation,
                state,
                yielded,
                max_output_tokens=max_output_tokens,
            )
            if self._continuation_is_restart_safe(continuation):
                self._persist_agent_session_checkpoint(
                    workspace,
                    state,
                    continuation,
                    initial_instruction=initial_instruction,
                    response_keys=response_keys,
                )
            await self._raise_if_cancel_requested()
            if self._continuation_is_restart_safe(continuation):
                await self._raise_if_restart_requested()

    @staticmethod
    def _continuation_is_restart_safe(continuation: TurnContinuation) -> bool:
        return not continuation.pending_tool_call_batch and not continuation.pending_tool_results

    @staticmethod
    def _new_session_response_message(
        restored: Mapping[str, Any],
        *,
        response_key: str,
        response_text: str,
    ) -> str:
        """Return one explicit user turn for a new Manager-bound semantic input.

        Instruction text is intentionally reusable across repair cycles.  The
        durable assignment key, rather than text equality or an outbox effect
        key, determines whether this input has already entered the session.
        """

        key = str(response_key or "").strip()
        seen = {
            str(item)
            for item in list(restored.get("response_keys") or [])
            if str(item)
        }
        text = str(response_text or "").strip()
        if not key or key in seen or not text:
            return ""
        return (
            "# New Manager-Bound Role Input\n\n"
            "This is a new semantic assignment for the durable role session. "
            "Any earlier completion summary or submit action settled only an earlier input; "
            "it does not complete this one.\n\n"
            + text
        )

    def _load_agent_session_checkpoint(self, workspace: dict[str, Any], *, session_id: str) -> dict[str, Any]:
        if not session_id:
            return {}
        session_metadata = dict((self.pack.metadata or {}).get("agent_session") or {})
        restore_text = str(session_metadata.get("continuation_input_path") or "").strip()
        if not restore_text:
            return {}
        restore_path = Path(restore_text)
        if not restore_path.is_file():
            raise RuntimeError("manager-selected agent continuation is unavailable")
        try:
            value = json.loads(restore_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("manager-selected agent continuation is unreadable") from exc
        if not isinstance(value, dict) or str(value.get("session_id") or "") != session_id:
            raise RuntimeError("manager-selected agent continuation has the wrong session identity")
        expected_scope = str(session_metadata.get("scope_kind") or "")
        expected_subject = str(session_metadata.get("subject_key") or "")
        if str(value.get("scope_kind") or "") != expected_scope:
            raise RuntimeError("manager-selected agent continuation has the wrong scope")
        if str(value.get("subject_key") or "") != expected_subject:
            raise RuntimeError("manager-selected agent continuation has the wrong subject")
        self._agent_session_checkpoint = dict(value)
        return dict(value)

    def _persist_agent_session_checkpoint(
        self,
        workspace: dict[str, Any],
        state: MinionAgentLoopState,
        continuation: TurnContinuation,
        *,
        initial_instruction: str,
        response_keys: list[str],
    ) -> None:
        session_metadata = dict((self.pack.metadata or {}).get("agent_session") or {})
        session_id = str(session_metadata.get("session_id") or "").strip()
        fencing_token = int(session_metadata.get("fencing_token") or 0)
        checkpoint_text = str(session_metadata.get("continuation_output_path") or "").strip()
        if not session_id or fencing_token <= 0 or not checkpoint_text:
            return
        checkpoint_path = Path(checkpoint_text)
        if continuation.pending_tool_call_batch or continuation.pending_tool_results:
            return
        payload = {
            "schema_version": "3",
            "session_id": session_id,
            "scope_kind": str(session_metadata.get("scope_kind") or ""),
            "subject_key": str(session_metadata.get("subject_key") or ""),
            "invocation_id": self.pack.invocation_id,
            "fencing_token": fencing_token,
            "initial_instruction": str(initial_instruction),
            "response_keys": list(response_keys),
            "tool_protocol_messages": [dict(item) for item in continuation.tool_protocol_messages],
            "tool_delivery_records": {
                str(key): dict(value)
                for key, value in dict(
                    getattr(continuation, "tool_delivery_records", {}) or {}
                ).items()
                if str(key) and isinstance(value, dict)
            },
            "llm_round_count": int(state.llm_round_count),
            "tool_call_count": int(state.tool_call_count),
            "tool_batch_count": int(continuation.tool_batch_count),
            "preferred_llm_endpoint_id": str(continuation.preferred_llm_endpoint_id or ""),
            "preferred_llm_model_id": str(continuation.preferred_llm_model_id or ""),
        }
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        target = checkpoint_path
        temporary = target.parent / f".{target.name}.{os.getpid()}.tmp"
        temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temporary, target)
        self._agent_session_checkpoint = payload

    def _runner_memory_candidate_sink(self) -> MockL3Plugin:
        if self._memory_candidate_sink is not None:
            return self._memory_candidate_sink
        sink = MockL3Plugin(provider_id=f"minion_run_{self.run_id}_memory_candidates")
        self._memory_candidate_sink = sink
        return sink

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
        if message_type in {"restart_requested", "manager_restart"}:
            raise _MinionCooperativeRestart(
                self._remember_restart_request(dict(message.get("payload") or message))
            )
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
            if message_type in {"restart_requested", "manager_restart"}:
                self._remember_restart_request(dict(message.get("payload") or message))
                continue
            self._pending_control_messages.append(dict(message))
            return {}

    async def _raise_if_cancel_requested(self) -> None:
        cancel = await self._poll_cancel_requested()
        if cancel:
            raise _MinionCooperativeCancel(cancel)

    async def _raise_if_restart_requested(self) -> None:
        if self._restart_requested:
            raise _MinionCooperativeRestart(dict(self._restart_requested))

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

    def _remember_restart_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._restart_requested:
            return dict(self._restart_requested)
        self._restart_requested = {
            **dict(payload),
            "reason": str(payload.get("reason") or "manager_restart_requested"),
            "summary": str(
                payload.get("summary")
                or "minion suspended after the current durable LLM/tool safe point"
            ),
        }
        return dict(self._restart_requested)

    def _build_minion_retry_note(self, outcome: Any, observations: list[Any], retry_count: int) -> str:
        if self.blocked_summary:
            return ""
        if str(getattr(outcome, "finish_reason", "") or "") == LLMFinishReason.ERROR:
            return ""
        tools_available = bool(self.pack.allowed_capabilities)
        if not tools_available:
            return ""
        if retry_count > 0:
            return ""
        if observations:
            return ""
        if not self._requires_first_tool_call():
            return ""
        _ = outcome
        return (
            "You have not used any capability yet. This contract invocation requires executable evidence. "
            "Use one listed capability now to inspect, research, read, write, or verify before completing."
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
        prompt_fragment_registry = self._build_minion_prompt_fragment_registry(config=bundle.config)
        context = _MinionTurnContext(
            execution_runtime=execution_runtime,
            prompt_fragment_registry=prompt_fragment_registry,
            port_registry={
                "llm:llm": llm_runtime,
                "memory:memory": memory_service,
                "agent_io:output": output_port,
            },
            turn_event_bus=TurnEventBus(),
        )
        prompt_compiler = PromptCompiler(context)
        turn_state = _MinionTurnState()
        turn_manager = _MinionTurnManager()

        def build_canonical_prompt(
            assembly_context: PromptAssemblyContext,
            *,
            max_output_tokens: int = 1024,
            model_hint: str | None = None,
        ) -> CanonicalLLMRequest:
            prompt = prompt_compiler.build_canonical_prompt(
                assembly_context,
                max_output_tokens=max_output_tokens,
                model_hint=model_hint,
            )
            return CanonicalLLMRequest(
                messages=list(prompt.messages),
                max_output_tokens=prompt.max_output_tokens,
                model_hint=prompt.model_hint,
                temperature=_minion_temperature(self.pack, fallback=prompt.temperature),
                tools=list(prompt.tools),
                metadata={**dict(prompt.metadata), **_minion_llm_request_metadata(self.pack, self.run_id)},
            )

        return TurnExecutor(
            context,
            turn_state,
            turn_manager,
            call_port_async=self._call_port_async,
            build_canonical_prompt=build_canonical_prompt,
            debug_log_prompt=lambda _continuation, request: self._debug_log_minion_llm_request(state, request),
            debug_log_outcome=lambda _continuation, outcome: self._debug_log_minion_llm_outcome(state, outcome),
            debug_log_reply=lambda _continuation, text: self._debug_log_minion_reply(text),
            build_llm_tool_contracts=lambda: _llm_tools_for_allowed(state.execution_runtime, self.pack.allowed_capabilities),
            handle_failure_async=_minion_noop_failure_handler,
            render_failure_feedback_text=lambda feedback: str(feedback or ""),
            should_enter_failure_flow_for_tool_result=lambda _tool_result: False,
            handle_llm_provider_errors=False,
        )

    def _build_minion_prompt_fragment_registry(
        self,
        *,
        config: RuntimeConfig | None = None,
    ) -> PromptFragmentRegistry:
        prompt_fragment_registry = PromptFragmentRegistry()
        prompt_fragment_registry.register(
            MinionPromptFragmentProvider(
                scaffold_factory=self._prompt_scaffold,
                role_context_factory=self._render_durable_role_context,
            )
        )
        prompt_fragment_registry.register(
            MemoryPromptFragmentProvider(
                config=config,
                include_l1_recent_context=False,
            )
        )
        return prompt_fragment_registry

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
        if (
            isinstance(effect, ToolCallEffect)
            and len(state.tool_protocol_messages) > before_protocol_len
            and self._persist_tool_protocol_to_l1()
        ):
            protocol_transcript, _ = l1_tool_protocol_transcript(state.tool_protocol_messages[before_protocol_len:])
            await self._commit_minion_l1(
                state,
                protocol_transcript,
            )
        return result

    def _preflight_minion_llm_round(self, state: MinionAgentLoopState) -> EffectResult | None:
        if self.blocked_summary:
            return EffectResult(status=RuntimeStatus.OK, payload=CanonicalLLMOutcome(text=self.blocked_summary))
        if self._required_primary_artifact_name() and self._completion_evidence_present():
            return EffectResult(
                status=RuntimeStatus.OK,
                payload=CanonicalLLMOutcome(
                    text="assignment produced completion evidence",
                    finish_reason=LLMFinishReason.STOP,
                ),
            )
        max_rounds = _optional_positive_int(self.pack.metadata.get("max_tool_rounds") if isinstance(self.pack.metadata, dict) else None)
        if max_rounds is None or state.llm_round_count < max_rounds:
            state.llm_round_count += 1
            return None
        if self._completion_evidence_present() or self._artifact_completion_evidence_present():
            outcome = CanonicalLLMOutcome(text="assignment produced completion evidence")
        else:
            self.blocked_summary = f"minion reached explicit max_tool_rounds={max_rounds} before completing the current invocation"
            outcome = CanonicalLLMOutcome(text=self.blocked_summary)
        return EffectResult(status=RuntimeStatus.OK, payload=outcome)

    async def _postprocess_minion_llm_round(self, state: MinionAgentLoopState, result: EffectResult) -> EffectResult:
        outcome = result.payload
        finish_reason = str(getattr(outcome, "finish_reason", "") or "")
        if finish_reason == LLMFinishReason.ERROR:
            # Provider exhaustion produced no assistant/tool turn.  Keep the
            # durable checkpoint at the same logical round so a later process
            # attempt retries the exact request instead of projecting phantom
            # progress into the role session.
            state.llm_round_count = max(0, state.llm_round_count - 1)
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
            input_tokens=max(0, int(getattr(outcome, "input_tokens", 0) or 0)),
            uncached_input_tokens=max(
                0,
                int(getattr(outcome, "uncached_input_tokens", 0) or 0),
            ),
            cached_input_tokens=max(
                0,
                int(getattr(outcome, "cached_input_tokens", 0) or 0),
            ),
            cache_write_input_tokens=max(
                0,
                int(getattr(outcome, "cache_write_input_tokens", 0) or 0),
            ),
            output_tokens=max(0, int(getattr(outcome, "output_tokens", 0) or 0)),
            reasoning_tokens=max(
                0,
                int(getattr(outcome, "reasoning_tokens", 0) or 0),
            ),
            cost=max(0.0, float(getattr(outcome, "cost", 0.0) or 0.0)),
            usage_reported=bool(getattr(outcome, "usage_reported", False)),
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
        context = self._prompt_debug_context(round=state.llm_round_count)
        self._append_prompt_debug_log(render_prompt_debug_log(request, context=context))

    def _debug_log_minion_llm_outcome(self, state: MinionAgentLoopState, outcome: Any) -> None:
        context = self._prompt_debug_context(round=state.llm_round_count)
        self._append_prompt_debug_log(
            render_llm_outcome_debug_log(
                outcome,
                provider_payload="{}",
                context=context,
            )
        )

    def _debug_log_minion_reply(self, text: object) -> None:
        self._append_prompt_debug_log(render_reply_debug_log(text, context=self._prompt_debug_context()))

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

    def _persist_tool_protocol_to_l1(self) -> bool:
        return True

    def _render_durable_role_context(self) -> str:
        binding = dict((self.pack.metadata or {}).get("minion_v2") or {})
        if str(binding.get("role") or "") != "implementation":
            return ""
        from pal.minion.v2.candidate_builder import candidate_checklist_context

        workspace = {
            **dict(self.pack.workspace or {}),
            "runtime_root": str(self.runtime_root),
            "invocation_id": self.pack.invocation_id,
            "minion_v2": binding,
        }
        return candidate_checklist_context(workspace)

    def _minion_compaction_source_text(self, state: MinionAgentLoopState, *, target_input_budget: int) -> str:
        items = getattr(getattr(state.memory_service, "l1_store", None), "items", [])
        return build_minion_prior_user_inputs_source_text(items, target_input_budget=target_input_budget)

    def _requires_first_tool_call(self) -> bool:
        if bool((self.pack.metadata or {}).get("allow_text_only_completion")):
            return False
        completion_policy = self._completion_policy()
        if "requires_capability_evidence" in completion_policy:
            return bool(completion_policy.get("requires_capability_evidence")) and bool(self.pack.allowed_capabilities)
        return str(completion_policy.get("evidence") or "").strip().lower() == "git_commit" and bool(self.pack.allowed_capabilities)

    async def _execute_allowed_tool(
        self,
        execution_runtime: "MinionScopedExecutionRuntime",
        tool_call: CanonicalToolCall,
        *,
        allow_tools: bool = True,
        budget: Any = None,
        turn_id: str | None = None,
    ) -> CanonicalToolResult:
        effective_allowed = getattr(execution_runtime, "allowed_capabilities", None) or self.pack.allowed_capabilities
        allowed_items = [
            str(item).strip()
            for item in [*list(self.pack.allowed_capabilities or []), *list(effective_allowed or [])]
            if str(item).strip()
        ]
        allowed_items = filter_minion_allowed_capabilities(allowed_items)
        resolve_name = getattr(execution_runtime, "resolve_capability_address", None)
        if not callable(resolve_name):
            resolve_name = lambda name: str(name or "").strip()
        provider_call = tool_call
        admission = admit_minion_tool_call(
            provider_call,
            allowed_items,
            resolve_name=resolve_name,
            require_effective_target=True,
        )
        policy_call = self._tool_call_with_minion_defaults(admission.call)
        tool_call = _provider_call_with_effective_args(provider_call, policy_call)
        target_name = admission.target_name
        if not admission.ok:
            self.blocked_summary = f"{admission.message}: {target_name}"
            return admission.to_result()
        approval_runtime = ApprovalExecutionDecorator(
            delegate=execution_runtime,
            classify=lambda _call: self._execution_approval_request(target_name),
            request=lambda request, call: self._request_execution_approval(
                request,
                call,
                capability_name=target_name,
            ),
        )
        result = await approval_runtime.execute_tool_async(
            tool_call,
            allow_tools=allow_tools,
            budget=budget,
            turn_id=turn_id or self.run_id,
        )
        if (result.structured or {}).get("reason") == "approval_not_accepted":
            decision = str((result.structured or {}).get("decision") or "timeout")
            self.blocked_summary = f"approval {decision} for {target_name}"
            result.structured["capability"] = target_name
        self._record_web_research_usage(target_name)
        self._record_review_tool_evidence(target_name, policy_call, result)
        return result

    def _tool_call_with_minion_defaults(self, tool_call: CanonicalToolCall) -> CanonicalToolCall:
        target_name = _effective_capability_name(tool_call)
        if str(target_name).startswith("op_lsp_"):
            return self._tool_call_with_lsp_workspace(tool_call)
        if str(target_name) == "op_exec_shell":
            return self._tool_call_with_shell_workspace(tool_call)
        return tool_call

    def _tool_call_with_shell_workspace(self, tool_call: CanonicalToolCall) -> CanonicalToolCall:
        effective_args = _effective_tool_args(tool_call)
        workspace = dict(self.pack.workspace or {})
        repo_path = str(
            workspace.get("repo_path")
            or workspace.get("workspace_path")
            or workspace.get("task_repo_path")
            or workspace.get("target_repo_path")
            or ""
        ).strip()
        if repo_path and not str(effective_args.get("cwd") or "").strip():
            effective_args["cwd"] = repo_path
        if tool_call.name == "op_tool_call":
            args = dict(tool_call.args or {})
            args["args"] = effective_args
            return CanonicalToolCall(name=tool_call.name, args=args, call_id=tool_call.call_id)
        return CanonicalToolCall(name=tool_call.name, args=effective_args, call_id=tool_call.call_id)

    def _tool_call_with_lsp_workspace(self, tool_call: CanonicalToolCall) -> CanonicalToolCall:
        effective_args = _effective_tool_args(tool_call)
        workspace = dict(self.pack.workspace or {})
        repo_path = str(
            workspace.get("repo_path")
            or workspace.get("workspace_path")
            or workspace.get("task_repo_path")
            or workspace.get("target_repo_path")
            or ""
        ).strip()
        if repo_path:
            effective_args["workspace_root"] = repo_path
        if tool_call.name == "op_tool_call":
            args = dict(tool_call.args or {})
            args["args"] = effective_args
            return CanonicalToolCall(name=tool_call.name, args=args, call_id=tool_call.call_id)
        return CanonicalToolCall(name=tool_call.name, args=effective_args, call_id=tool_call.call_id)


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

    def _execution_approval_request(self, capability_name: str) -> ExecutionApprovalRequest | None:
        if self._user_interaction_port().should_request_approval(
            capability_name,
            self.pack.approval_policy or {},
        ):
            return ExecutionApprovalRequest(
                title="Minion high-risk operation",
                risk="high",
                impact="Minion requested permission before running a high-risk operation.",
            )
        if self.auto_accept_approvals:
            return None
        status = self._web_research_budget_status(capability_name)
        if not status or int(status["used"]) < int(status["budget"]):
            return None
        return ExecutionApprovalRequest(
            title="Minion web research budget",
            risk="medium",
            impact="Minion used the included web research budget for this run and requested permission before another web call.",
            approval_kind="web_research_budget",
            metadata={
                "web_research_budget": status["budget"],
                "web_research_used": status["used"],
                "web_research_budget_key": str(status.get("key") or ""),
            },
        )

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

    async def _request_execution_approval(
        self,
        request: ExecutionApprovalRequest,
        tool_call: CanonicalToolCall,
        *,
        capability_name: str,
    ) -> str:
        return await self._request_approval(
            capability_name,
            tool_call,
            approval_kind=request.approval_kind,
            title=request.title,
            risk=request.risk,
            impact=request.impact,
            metadata=request.metadata,
        )

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

    def _user_interaction_port(self) -> MinionUserInteractionPort:
        if self.user_interaction is None:
            binding = dict(dict(self.pack.metadata or {}).get("minion_v2") or {})
            self.user_interaction = MinionUserInteractionPort(
                emit_event=self._emit,
                read_response=self._read_manager_control,
                run_id=self.run_id,
                minion_id=self.minion_id,
                invocation_id=self.pack.invocation_id,
                workflow_id=str(binding.get("workflow_id") or ""),
                control_route=dict(binding.get("control_route") or {}),
                auto_accept_approvals=self.auto_accept_approvals,
            )
        return self.user_interaction

    async def _request_architecture_clarification(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._user_interaction_port().request_clarification(payload)

    def _completion_evidence_present(self) -> bool:
        if self._manager_submission_receipt_required():
            return self._manager_submission_receipt_present()
        return self._required_primary_artifact_present()

    def _artifact_completion_evidence_present(self) -> bool:
        if self._manager_submission_receipt_required():
            return self._manager_submission_receipt_present()
        return self._required_primary_artifact_present()

    def _manager_submission_receipt_required(self) -> bool:
        metadata = dict(self.pack.metadata or {})
        minion_v2 = dict(metadata.get("minion_v2") or {})
        return bool(minion_v2.get("submission_receipt_required"))

    def _manager_submission_receipt_present(self) -> bool:
        if self._manager_submission_receipt_observed:
            return True
        try:
            from pal.minion.v2.role_gateway import role_gateway_client_from_env

            client = role_gateway_client_from_env(self.runtime_root)
            if client is None:
                return False
            status = client.request_sync("submission_status", {})
        except Exception:
            return False
        self._manager_submission_receipt_observed = bool(status.get("recorded"))
        return self._manager_submission_receipt_observed

    def _required_primary_artifact_present(self) -> bool:
        required = self._required_primary_artifact_name()
        if not required:
            return bool(self.produced_artifacts)
        for artifact in self.produced_artifacts:
            if str(artifact.get("role") or "").strip().lower() != "primary":
                continue
            candidates = {
                str(artifact.get("relative_path") or "").strip(),
                str(artifact.get("requested_relative_path") or "").strip(),
                Path(str(artifact.get("path") or "")).name,
            }
            if required in candidates:
                return True
        return False

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
        title = str(self.pack.instruction or self.pack.goal or "Minion invocation result")
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
            f"- invocation_id: {self.pack.invocation_id}",
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
        return f"invocation_{self._safe_path_part(self.pack.minion_profile)}{suffix}.md"

    def _default_text_artifact_title(self, title: str, *, partial: bool = False) -> str:
        if partial:
            return f"{title} (partial truncated output)"
        if self._is_reviewer_profile():
            return "Review report"
        return title

    def _is_reviewer_profile(self) -> bool:
        output_policy = self._policy_from_workspace_or_profile("output_policy")
        primary_artifact = str(output_policy.get("primary_artifact") or "").strip().lower()
        output_types = {str(item or "").strip() for item in list(output_policy.get("allowed_output_types") or [])}
        return (
            "review" in primary_artifact
            or any("Review" in item or "Verification" in item for item in output_types)
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
        scope = "contract invocation"
        return (
            f"LLM output was truncated before the minion completed the {scope} "
            f"(finish_reason={reason}). Treat this {scope} as blocked.{saved} "
            "For long deliverables, write the full result as an artifact/file and keep the final reply short."
        )

    @staticmethod
    def _safe_path_part(value: str) -> str:
        normalized = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(value or "").strip())
        return normalized.strip("_")[:80] or "minion"

    async def _emit(self, event_kind: str, payload: dict[str, Any]) -> None:
        binding = dict(dict(self.pack.metadata or {}).get("minion_v2") or {})
        event_payload = dict(payload)
        if event_kind in {"approval_requested", "clarification_requested"}:
            control_route = dict(binding.get("control_route") or {})
            if control_route and not event_payload.get("control_route"):
                event_payload["control_route"] = control_route
        event = {
            "type": "event",
            "event_kind": event_kind,
            "minion_id": self.minion_id,
            "run_id": self.run_id,
            "invocation_id": self.pack.invocation_id,
            "workflow_id": str(binding.get("workflow_id") or ""),
            "minion_profile": self.pack.minion_profile,
            "payload": event_payload,
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
                **payload,
            },
        )

    def _prompt_scaffold(self) -> dict[str, Any]:
        profile = dict(self.pack.resolved_profile or {})
        prompt_view = _prompt_view_from_pack(self.pack)
        unit_scope = dict(prompt_view.get("unit") or {}) if prompt_view else {}
        requirements_brief = dict((self.pack.metadata or {}).get("requirements_brief") or {})
        brief_acceptance = [
            str(item).strip()
            for item in list(requirements_brief.get("acceptance_criteria") or [])
            if str(item or "").strip()
        ]
        acceptance = brief_acceptance or list(self.pack.acceptance_criteria)
        instruction = str(self.pack.instruction or self.pack.goal)
        return {
            "identity": str(profile.get("identity_fragment") or ""),
            "behavior": str(profile.get("behavior_fragment") or ""),
            "instruction": instruction,
            "acceptance_criteria": acceptance,
            "continuity": dict(self.pack.continuity),
            "unit_scope": unit_scope,
            "allowed_capabilities": list(self.pack.allowed_capabilities),
            "visible_capabilities": list(self._visible_capability_aliases),
            "skill_manual_context": list((self.pack.metadata or {}).get("skill_manual_context") or []),
            "output_contract": str(profile.get("output_contract_fragment") or ""),
            "workspace_policy": self._workspace_policy(),
            "completion_policy": self._completion_policy(),
            "execution_strategy": self._execution_strategy(),
            "prompt_view": prompt_view,
            "requirements_brief": requirements_brief,
            "workflow_model": "contract_v2",
        }

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
        return {}

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
        if self.blocked_kind:
            payload["blocker_kind"] = self.blocked_kind
        if ask_user_question:
            payload["status"] = "blocked"
            payload["summary"] = _ask_user_question_summary(ask_user_question)
            payload["ask_user_question"] = ask_user_question
        return payload

    def _cancel_terminal_payload(self, cancel: dict[str, Any]) -> dict[str, Any]:
        payload = self._terminal_payload("killed", cancel.get("summary") or cancel.get("reason") or "minion cancellation requested")
        payload["reason"] = str(cancel.get("reason") or "cooperative_cancel_requested")
        payload["cooperative_cancel"] = True
        for key in ("workflow_id", "invocation_id", "unit_id", "repair_bill_ref"):
            value = str(cancel.get(key) or "").strip()
            if value:
                payload[key] = value
        return payload

    def _restart_terminal_payload(self, restart: dict[str, Any]) -> dict[str, Any]:
        payload = self._terminal_payload(
            "suspended",
            restart.get("summary") or "minion suspended for manager restart",
        )
        payload["reason"] = str(restart.get("reason") or "manager_restart_requested")
        payload["manager_restart"] = True
        payload["durable_safe_point"] = True
        return payload

    def _finalize_produced_artifacts(self) -> None:
        if not self.produced_artifacts:
            return
        staged_present = any(bool(item.get("staged")) or str(item.get("stage_path") or "").strip() for item in self.produced_artifacts)
        staging_enabled = bool(str((self.pack.workspace or {}).get("artifact_stage_dir") or "").strip() or staged_present)
        if not staging_enabled:
            return
        accepted_indexes = self._accepted_artifact_indexes()
        next_artifacts: list[dict[str, Any]] = []
        for index, artifact in enumerate(list(self.produced_artifacts)):
            if index in accepted_indexes:
                next_artifacts.append(self._promote_produced_artifact(artifact))
            else:
                self._delete_registered_artifact_paths(artifact)
        self.produced_artifacts[:] = next_artifacts
        self._cleanup_artifact_stage_dir()

    def _accepted_artifact_indexes(self) -> set[int]:
        primary_indexes = [
            index
            for index, artifact in enumerate(self.produced_artifacts)
            if str(artifact.get("role") or "").strip().lower() == "primary"
        ]
        if not primary_indexes:
            return set(range(len(self.produced_artifacts)))
        latest_primary = primary_indexes[-1]
        return {
            index
            for index, artifact in enumerate(self.produced_artifacts)
            if index == latest_primary or str(artifact.get("role") or "").strip().lower() != "primary"
        }

    def _promote_produced_artifact(self, artifact: dict[str, Any]) -> dict[str, Any]:
        promoted = dict(artifact)
        source_text = str(promoted.get("stage_path") or promoted.get("path") or "").strip()
        relative_path = str(promoted.get("relative_path") or "").strip()
        if str(promoted.get("role") or "").strip().lower() == "primary":
            relative_path = str(promoted.get("requested_relative_path") or relative_path).strip()
        final_root_text = str(promoted.get("final_artifact_dir") or (self.pack.workspace or {}).get("artifact_dir") or "").strip()
        if source_text and relative_path and final_root_text:
            source = Path(source_text).expanduser()
            final_root = Path(final_root_text).expanduser().resolve()
            destination = (final_root / relative_path).resolve()
            if destination != final_root and _path_is_relative_to(destination, final_root):
                if source.exists():
                    final_root.mkdir(parents=True, exist_ok=True)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        same_file = source.resolve() == destination.resolve()
                    except OSError:
                        same_file = False
                    if not same_file:
                        shutil.copy2(source, destination)
                if destination.exists() and destination.is_file():
                    promoted = self._refresh_artifact_file_metadata(promoted, destination)
        for key in ("staged", "stage_path", "final_artifact_dir", "requested_relative_path"):
            promoted.pop(key, None)
        return promoted

    def _refresh_artifact_file_metadata(self, artifact: dict[str, Any], path: Path) -> dict[str, Any]:
        refreshed = dict(artifact)
        data = path.read_bytes()
        refreshed["path"] = str(path)
        refreshed["size_bytes"] = path.stat().st_size
        refreshed["sha256"] = hashlib.sha256(data).hexdigest()
        final_root_text = str(refreshed.get("final_artifact_dir") or (self.pack.workspace or {}).get("artifact_dir") or "").strip()
        if final_root_text:
            final_root = Path(final_root_text).expanduser().resolve()
            with contextlib.suppress(ValueError):
                refreshed["relative_path"] = str(path.resolve().relative_to(final_root)).replace("\\", "/")
        return refreshed

    def _delete_registered_artifact_paths(self, artifact: dict[str, Any]) -> None:
        cleanup_roots = self._artifact_cleanup_roots(artifact)
        if not cleanup_roots:
            return
        seen: set[Path] = set()
        for raw_path in (artifact.get("stage_path"), artifact.get("path")):
            text = str(raw_path or "").strip()
            if not text:
                continue
            path = Path(text).expanduser()
            with contextlib.suppress(OSError):
                path = path.resolve()
            if path in seen:
                continue
            seen.add(path)
            root = next((item for item in cleanup_roots if path != item and _path_is_relative_to(path, item)), None)
            if root is None or not path.exists():
                continue
            with contextlib.suppress(OSError):
                if path.is_file() or path.is_symlink():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path)
            self._prune_empty_artifact_dirs(path.parent, stop=root)

    def _artifact_cleanup_roots(self, artifact: dict[str, Any]) -> list[Path]:
        roots: list[Path] = []
        for raw_root in (
            (self.pack.workspace or {}).get("artifact_stage_dir"),
            artifact.get("final_artifact_dir"),
            (self.pack.workspace or {}).get("artifact_dir"),
        ):
            text = str(raw_root or "").strip()
            if not text:
                continue
            root = Path(text).expanduser()
            with contextlib.suppress(OSError):
                root = root.resolve()
            if root not in roots:
                roots.append(root)
        return roots

    def _cleanup_artifact_stage_dir(self) -> None:
        stage_dir = str((self.pack.workspace or {}).get("artifact_stage_dir") or "").strip()
        if not stage_dir:
            return
        root = Path(stage_dir).expanduser()
        with contextlib.suppress(OSError):
            root = root.resolve()
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)

    def _prune_empty_artifact_dirs(self, path: Path, *, stop: Path) -> None:
        current = path
        while current != stop and _path_is_relative_to(current, stop):
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent

    def _artifact_payload(self) -> dict[str, Any]:
        self._finalize_produced_artifacts()
        artifacts = [dict(item) for item in self.produced_artifacts]
        payload: dict[str, Any] = {"artifacts": artifacts}
        primary = next((item for item in artifacts if str(item.get("role") or "") == "primary"), None)
        if primary is None and artifacts:
            primary = artifacts[0]
        if primary is not None:
            payload["primary_artifact"] = dict(primary)
        return payload

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
            "invocation_id": self.pack.invocation_id,
            "minion_profile": self.pack.minion_profile,
            "minion_id": self.minion_id,
            "run_id": self.run_id,
            "payload": dict(payload or {}),
        }
        prompt_observation_tag = _prompt_observation_tag_from_pack(self.pack)
        if prompt_observation_tag:
            record["prompt_observation_tag"] = prompt_observation_tag
        try:
            path = Path(path_text)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        except Exception:
            return

    def _append_prompt_debug_log(self, text: str) -> None:
        config = dict((self.pack.metadata or {}).get("debug_log") or {})
        if not bool(config.get("enabled")):
            return
        path_text = str(config.get("path") or "").strip()
        if not path_text:
            return
        try:
            append_prompt_debug_log(Path(path_text), text)
        except Exception:
            return

    def _prompt_debug_context(self, *, round: int | None = None) -> dict[str, Any]:
        context: dict[str, Any] = {
            "invocation_id": self.pack.invocation_id,
            "minion_profile": self.pack.minion_profile,
            "minion_id": self.minion_id,
            "run_id": self.run_id,
        }
        if round is not None:
            context["round"] = round
        prompt_observation_tag = _prompt_observation_tag_from_pack(self.pack)
        if prompt_observation_tag:
            context["prompt_observation_tag"] = prompt_observation_tag
        return context

def build_slim_minion_runtime(runtime_root: Path, *, run_id: str = "") -> MinionRuntimeBundle:
    from pal.core import register_with_core as register_core_with_core

    read_only_database = os.environ.get("PAL_DATABASE_READ_ONLY") == "1"
    database = PalV2Database(
        db_path=Path(runtime_root) / "pal.sqlite3",
        read_only=read_only_database,
    )
    database.initialize(ALL_MODELS)
    llm_repository = LLMEndpointRepository()
    web_search_repository = WebSearchProviderRepository()
    web_fetch_repository = WebFetchProviderRepository()
    if not read_only_database:
        if not llm_repository.list_enabled():
            llm_repository.ensure_defaults(DEFAULT_LLM_ENDPOINTS)
        if not web_search_repository.list_all():
            web_search_repository.ensure_defaults(DEFAULT_WEB_SEARCH_PROVIDERS)
        if not web_fetch_repository.list_all():
            web_fetch_repository.ensure_defaults(DEFAULT_WEB_FETCH_PROVIDERS)
    settings = RuntimeSettingRepository()
    if not read_only_database:
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
    from pal.minion.v2.execution_state import RoleGatewayLogicalExecutionState
    from pal.minion.v2.role_gateway import role_gateway_client_from_env

    role_gateway_client = role_gateway_client_from_env(Path(runtime_root))
    if role_gateway_client is not None:
        logical_state = RoleGatewayLogicalExecutionState(role_gateway_client)
        core.context.execution_runtime.logical_state = logical_state
        core.context.execution_runtime.tool_result_pager.state_backend = logical_state
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
    l3_plugin = SQLiteVecL3Plugin(
        service=memory_service,
        embedding_provider=build_ollama_embedding_provider_from_config(config),
        read_only=read_only_database,
    )
    memory_service.l3_selector.active_provider_id = l3_plugin.provider_id
    register_l3_with_core(core.context, l3_plugin)
    broker_web = (
        MinionBrokerWebClient(runtime_root=Path(runtime_root), run_id=run_id)
        if os.environ.get("PAL_MINION_WEB_BROKER") == "1"
        else None
    )
    register_web_search_with_core(
        core.context,
        WebSearchService(
            repository=web_search_repository,
            settings_repository=settings,
        ),
        query_delegate=broker_web.search if broker_web is not None else None,
    )
    register_web_fetch_with_core(
        core.context,
        WebFetchService(
            repository=web_fetch_repository,
            settings_repository=settings,
            browser_manager=BrowserServiceManager(runtime_root=Path(runtime_root)),
        ),
        read_delegate=broker_web.read if broker_web is not None else None,
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

    return MinionRuntimeBundle(
        llm_runtime=llm_runtime,
        execution_runtime=core.context.execution_runtime,
        memory_service=memory_service,
        config=config,
        close_async=close,
    )


def _minion_prompt_context(pack: MinionInvocationPack, *, event: EventEnvelope | None = None, metadata: dict[str, Any]) -> PromptAssemblyContext:
    return PromptAssemblyContext(
        event=event,
        core_mode="minion",
        turn_kind="minion",
        work_order_id=pack.invocation_id,
        metadata=dict(metadata),
    )


def _memory_candidates_from_sink(memory_candidate_sink: MockL3Plugin) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for record in list(getattr(memory_candidate_sink, "records", []) or []):
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
            "source_kind": "minion_candidate_sink",
            "candidate_state": "candidate",
        }
        payload = item["payload"]
        if item["kind"] == "case" and all(str(payload.get(field) or "").strip() for field in ("situation", "task", "action", "result")):
            item["star"] = {field: str(payload.get(field) or "").strip() for field in ("situation", "task", "action", "result")}
        if item["summary"].strip() or item["title"].strip():
            result.append(item)
    return result


async def _minion_noop_failure_handler(*args: Any, **kwargs: Any) -> _MinionFailureResult:
    _ = args
    _ = kwargs
    return _MinionFailureResult(user_feedback="minion turn failed before a normal reply could be produced")


def _llm_tools_for_allowed(execution_runtime: Any, allowed_capabilities: list[str]) -> list[dict[str, Any]]:
    _ = allowed_capabilities
    build = getattr(execution_runtime, "build_llm_tool_contracts", None)
    if not callable(build):
        raise TypeError("Minion execution runtime must expose immutable generation tool contracts")
    return list(build())


def _provider_call_with_effective_args(
    provider_call: CanonicalToolCall,
    effective_call: CanonicalToolCall,
) -> CanonicalToolCall:
    """Apply Manager defaults without leaking canonical paths back to the facade."""

    args = dict(effective_call.args or {})
    if effective_call.name == "op_tool_call":
        provider_args = dict(provider_call.args or {})
        provider_target = str(provider_args.get("name") or "").strip()
        if provider_target:
            args["name"] = provider_target
    return CanonicalToolCall(
        name=provider_call.name,
        args=args,
        call_id=provider_call.call_id,
    )


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


def _resolve_minion_max_output_tokens(llm_runtime: Any, pack: MinionInvocationPack) -> int:
    metadata = pack.metadata if isinstance(pack.metadata, dict) else {}
    explicit = _optional_positive_int(metadata.get("max_output_tokens"))
    preferred_endpoint_id = _preferred_endpoint_id_from_pack(pack)
    preferred_endpoint_source = _preferred_endpoint_source_from_pack(pack)
    endpoint_limit = _runtime_max_output_tokens(
        llm_runtime,
        preferred_endpoint_id=preferred_endpoint_id,
        preferred_endpoint_source=preferred_endpoint_source,
    )
    if endpoint_limit is None:
        facts = _runtime_endpoint_facts(
            llm_runtime,
            preferred_endpoint_id=preferred_endpoint_id,
            preferred_endpoint_source=preferred_endpoint_source,
        )
        endpoint_limit = _optional_positive_int(facts.get("max_output_tokens")) if facts else None
        context_window = _optional_positive_int(facts.get("context_window")) if facts else None
        if endpoint_limit is None and context_window is not None:
            endpoint_limit = _max_output_tokens_from_context_window(context_window, llm_runtime)
    if explicit is not None:
        return min(explicit, endpoint_limit) if endpoint_limit is not None else explicit
    if endpoint_limit is not None:
        return endpoint_limit
    config = getattr(llm_runtime, "config", None)
    return _optional_positive_int(getattr(config, "fallback_max_output_tokens", None)) or 4096


def _minion_llm_request_metadata(pack: MinionInvocationPack, run_id: str) -> dict[str, Any]:
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
    prompt_observation_tag = _prompt_observation_tag_from_pack(pack)
    if prompt_observation_tag:
        metadata["prompt_observation_tag"] = prompt_observation_tag
    return metadata


def _minion_turn_settings_snapshot(pack: MinionInvocationPack) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "prompt_log_enabled": bool((pack.metadata or {}).get("prompt_log_enabled")),
    }
    prompt_observation_tag = _prompt_observation_tag_from_pack(pack)
    if prompt_observation_tag:
        snapshot["prompt_observation_tag"] = prompt_observation_tag
    return snapshot


def _prompt_observation_tag_from_pack(pack: MinionInvocationPack) -> str:
    metadata = pack.metadata if isinstance(pack.metadata, dict) else {}
    tag = str(metadata.get("prompt_observation_tag") or "").strip()
    return tag


def _minion_llm_request_timeout_seconds(pack: MinionInvocationPack) -> float | None:
    pack_metadata = pack.metadata if isinstance(pack.metadata, dict) else {}
    explicit = _optional_positive_float(pack_metadata.get("timeout_seconds"))
    if explicit is not None:
        return explicit
    return _optional_positive_float(pack_metadata.get("llm_round_timeout_seconds"))


def _minion_temperature(pack: MinionInvocationPack, *, fallback: float | None = None) -> float | None:
    metadata = pack.metadata if isinstance(pack.metadata, dict) else {}
    raw = metadata.get("temperature")
    if raw is None:
        return fallback
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback
    if 0.0 <= value <= 2.0:
        return value
    return fallback


def _preferred_endpoint_id_from_pack(pack: MinionInvocationPack) -> str | None:
    metadata = pack.metadata if isinstance(pack.metadata, dict) else {}
    value = str(metadata.get("preferred_endpoint_id") or "").strip()
    return value or None


def _preferred_endpoint_source_from_pack(pack: MinionInvocationPack) -> str | None:
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


def _web_research_capability_name(name: object) -> str | None:
    normalized = str(name or "").strip()
    if normalized in {"op_web_search", "search_web"}:
        return "op_web_search"
    if normalized in {"op_web_read", "read_web"}:
        return "op_web_read"
    return None


def _web_research_budget_keys(canonical_name: str) -> tuple[str, ...]:
    if canonical_name == "op_web_search":
        return ("op_web_search", "search_web")
    if canonical_name == "op_web_read":
        return ("op_web_read", "read_web")
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
        "workflow_id": str(loaded.get("workflow_id") or ""),
        "invocation_id": str(loaded.get("invocation_id") or ""),
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
