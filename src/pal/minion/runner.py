from __future__ import annotations

import asyncio
import contextlib
import hashlib
import mimetypes
import os
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from pal.artifact import ArtifactManager, ArtifactRepository, register_with_core as register_artifact_with_core
from pal.core import PalCore
from pal.core.runtime_config import RuntimeConfig
from pal.core.turns import (
    AgentLoopFrame,
    EffectResult,
    L1CommitPayload,
    LLMPreflightEffect,
    LLMRequestEffect,
    MemoryCompactEffect,
    ToolCallEffect,
    TurnOutcome,
    agent_turn_program,
)
from pal.execution import CapabilityCall, CapabilityResult, register_with_core as register_execution_with_core
from pal.foundation import PalV2Database, utc_now
from pal.llm import EndpointResolver, LLMEndpointRepository, LLMRuntime, LiteLLMCredentialResolver, RuntimeSettingRepository, build_default_endpoint_invoker
from pal.llm.contracts import (
    CanonicalLLMOutcome,
    CanonicalLLMRequest,
    CanonicalToolCall,
    CanonicalToolResult,
    LLMPreflightAdvice,
    LLMPreflightRequest,
)
from pal.llm.secret_store import EncryptedFileSecretStore
from pal.memory import (
    L1TranscriptMessage,
    L3CommitRequest,
    L3ProviderSelector,
    MemoryCommitRequest,
    MemoryCompactRequest,
    MemoryPackRequest,
    MemoryService,
    register_with_core as register_memory_with_core,
)
from pal.minion.git_env import commit_milestone, inspect_milestone_checkpoint
from pal.minion.profiles import filter_minion_allowed_capabilities, is_minion_capability_denied
from pal.minion.work_order import build_planner_work_order, prompt_view_from_metadata
from pal.plugins.l3 import MockL3Plugin, SQLiteVecL3Plugin, register_with_core as register_l3_with_core
from pal.shared import LLMFinishReason, LLMPreflightStatus, PromptAssemblyContext, RuntimeStatus, TaskContextPack
from pal.shared.prompt_rendering import render_system_reminder, render_xml_block
from pal.execution.tool_search import ToolReadTool, ToolSearchTool
from pal.web_fetch import BrowserServiceManager, WebFetchProviderRepository, WebFetchService, register_with_core as register_web_fetch_with_core
from pal.web_search import WebSearchProviderRepository, WebSearchService, register_with_core as register_web_search_with_core
from pal.wizard.runtime import ALL_MODELS, DEFAULT_LLM_ENDPOINTS, DEFAULT_WEB_FETCH_PROVIDERS, DEFAULT_WEB_SEARCH_PROVIDERS


EventWriter = Callable[[dict[str, Any]], Awaitable[None]]
DecisionReader = Callable[[float | None], Awaitable[dict[str, Any] | None]]


@dataclass
class MinionRuntimeBundle:
    llm_runtime: Any
    execution_runtime: Any
    close_async: Callable[[], Awaitable[None]] | None = None

    async def close(self) -> None:
        if self.close_async is not None:
            await self.close_async()


@dataclass
class MinionAgentLoopState:
    execution_runtime: "MinionScopedExecutionRuntime"
    memory_service: MemoryService
    memory_l3: MockL3Plugin
    tool_protocol_messages: list[dict[str, Any]] = field(default_factory=list)
    pending_assistant_tool_text: str = ""
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
    auto_accept_approvals: bool = False

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
            bundle = self.runtime_bundle or build_slim_minion_runtime(self.runtime_root)
            await self._emit(
                "phase_started",
                {
                    "phase": "accepted",
                    "summary": "minion accepted task context",
                    "prompt_scaffold_summary": _prompt_scaffold_summary(self._prompt_scaffold()),
                },
            )
            await self._emit("phase_started", {"phase": "milestone_started", "summary": self._current_milestone_title()})
            final_text = await self._run_agent_loop(bundle)
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
            if checkpoint_payload.get("status") != "completed":
                await self._emit("checkpoint", checkpoint_payload)
                await self._emit("terminal", self._terminal_payload("blocked", checkpoint_payload.get("summary") or "milestone blocked"))
                return 0
            await self._emit("checkpoint", checkpoint_payload)
            await self._emit("terminal", self._terminal_payload("completed", final_text or "minion completed current milestone"))
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

    async def _run_agent_loop(self, bundle: MinionRuntimeBundle) -> str:
        memory_l3 = MockL3Plugin(provider_id=f"minion_run_{self.run_id}_l3")
        memory_service = MemoryService(
            l3_selector=L3ProviderSelector(
                resolver=lambda provider_id: memory_l3,
                active_provider_id=memory_l3.provider_id,
            )
        )
        memory_l3.service = memory_service
        workspace = dict(self.pack.workspace)
        workspace.setdefault("work_order_id", self.pack.work_order_id)
        workspace.setdefault("current_milestone_index", self._current_milestone_index())
        workspace.setdefault("current_milestone_title", self._current_milestone_title())
        execution_runtime = MinionScopedExecutionRuntime(
            bundle.execution_runtime,
            self.pack.allowed_capabilities,
            workspace,
            produced_artifacts=self.produced_artifacts,
            memory_l3=memory_l3,
        )
        state = MinionAgentLoopState(
            execution_runtime=execution_runtime,
            memory_service=memory_service,
            memory_l3=memory_l3,
        )
        max_output_tokens = _resolve_minion_max_output_tokens(bundle.llm_runtime, self.pack)

        def build_context(frame: AgentLoopFrame):
            metadata = {
                "retry_note": frame.retry_note,
                "task_context_pack": self.pack,
            }
            return _minion_prompt_context(self.pack, metadata=metadata)

        def build_commit_payload(final_reply: str, observations: list[Any], reply_texts: list[str]) -> L1CommitPayload:
            _ = reply_texts
            transcript = [
                L1TranscriptMessage(role="user", content=_render_task_prompt(self.pack)),
                L1TranscriptMessage(role="assistant", content=final_reply or "minion completed"),
            ]
            return L1CommitPayload(turn_id=self.run_id, transcript=transcript, tool_observations=list(observations))

        program = agent_turn_program(
            turn_id=self.run_id,
            build_assembly_context=build_context,
            render_final_text=lambda outcome: str(getattr(outcome, "text", "") or "") if outcome is not None else "",
            build_commit_payload=build_commit_payload,
            max_output_tokens=max_output_tokens,
            build_retry_note=self._build_minion_retry_note,
        )
        current: EffectResult | None = None
        while True:
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
            current = await self._execute_minion_agent_effect(bundle, state, yielded, max_output_tokens=max_output_tokens)

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
                "`op_minion_checkpoint_commit` now. Do not use shell git add/commit; Pal needs the structured result "
                "from `op_minion_checkpoint_commit` for manager reporting."
            )
        return (
            "The milestone is not complete yet: no minion checkpoint commit exists. Use the listed file/workspace "
            "capabilities to make and verify the required change, then call `op_minion_checkpoint_commit` to create "
            "the milestone checkpoint commit. Do not stop with a report-only response. If completion is truly "
            "impossible, report a concrete blocker that explains which required input, permission, file, or contract "
            "is missing."
        )

    async def _execute_minion_agent_effect(
        self,
        bundle: MinionRuntimeBundle,
        state: MinionAgentLoopState,
        effect: Any,
        *,
        max_output_tokens: int,
    ) -> EffectResult:
        if isinstance(effect, LLMPreflightEffect):
            return await self._handle_minion_preflight(bundle, state, effect, max_output_tokens=max_output_tokens)
        if isinstance(effect, MemoryCompactEffect):
            return await self._handle_minion_memory_compact(bundle, state, effect)
        if isinstance(effect, LLMRequestEffect):
            return await self._handle_minion_llm_request(bundle, state, effect, max_output_tokens=max_output_tokens)
        if isinstance(effect, ToolCallEffect):
            return await self._handle_minion_tool_call(state, effect)
        return EffectResult(status=RuntimeStatus.UNSUPPORTED, text=f"unsupported minion effect: {getattr(effect, 'kind', '')}")

    async def _handle_minion_preflight(
        self,
        bundle: MinionRuntimeBundle,
        state: MinionAgentLoopState,
        effect: LLMPreflightEffect,
        *,
        max_output_tokens: int,
    ) -> EffectResult:
        request = LLMPreflightRequest(
            messages=self._minion_prompt_messages(state, effect.assembly_context),
            max_output_tokens=max_output_tokens,
            metadata=_minion_llm_request_metadata(self.pack, self.run_id),
        )
        preflight = getattr(bundle.llm_runtime, "apreflight", None)
        if callable(preflight):
            advice = preflight(request)
            if hasattr(advice, "__await__"):
                advice = await advice
        else:
            sync_preflight = getattr(bundle.llm_runtime, "preflight", None)
            if callable(sync_preflight):
                advice = sync_preflight(request)
            else:
                advice = LLMPreflightAdvice(status=LLMPreflightStatus.READY)
        return EffectResult(status=RuntimeStatus.OK, payload=advice)

    async def _handle_minion_llm_request(
        self,
        bundle: MinionRuntimeBundle,
        state: MinionAgentLoopState,
        effect: LLMRequestEffect,
        *,
        max_output_tokens: int,
    ) -> EffectResult:
        if self.blocked_summary:
            return EffectResult(status=RuntimeStatus.OK, payload=CanonicalLLMOutcome(text=self.blocked_summary))
        max_rounds = _optional_positive_int(self.pack.metadata.get("max_tool_rounds") if isinstance(self.pack.metadata, dict) else None)
        if max_rounds is not None and state.llm_round_count >= max_rounds:
            if self._completion_evidence_present() or self._artifact_completion_evidence_present():
                outcome = CanonicalLLMOutcome(text="milestone produced completion evidence")
            else:
                self.blocked_summary = f"minion reached explicit max_tool_rounds={max_rounds} before completing the current milestone"
                outcome = CanonicalLLMOutcome(text=self.blocked_summary)
            return EffectResult(status=RuntimeStatus.OK, payload=outcome)

        state.llm_round_count += 1
        await self._emit_progress(
            "llm_round_started",
            round=state.llm_round_count,
            tool_call_count=state.tool_call_count,
            tool_count=len(_llm_tools_for_allowed(state.execution_runtime, self.pack.allowed_capabilities)),
        )
        request = CanonicalLLMRequest(
            messages=self._minion_prompt_messages(state, effect.assembly_context),
            max_output_tokens=max_output_tokens,
            tools=_llm_tools_for_allowed(state.execution_runtime, self.pack.allowed_capabilities),
            metadata=_minion_llm_request_metadata(self.pack, self.run_id),
        )
        self._append_debug_log(
            "llm_request",
            {
                "round": state.llm_round_count,
                "messages": request.messages,
                "tools": request.tools,
                "metadata": request.metadata,
            },
        )
        outcome = await self._await_with_progress_heartbeat(
            bundle.llm_runtime.agenerate(request),
            phase="llm_round_waiting",
            round=state.llm_round_count,
            tool_call_count=state.tool_call_count,
        )
        tool_calls = [self._ensure_tool_call_identity(item) for item in list(getattr(outcome, "tool_calls", []) or [])]
        if tool_calls:
            outcome = CanonicalLLMOutcome(
                text=str(getattr(outcome, "text", "") or ""),
                reasoning_text=str(getattr(outcome, "reasoning_text", "") or ""),
                tool_calls=tool_calls,
                finish_reason=str(getattr(outcome, "finish_reason", "") or "stop"),
                response_mode=getattr(outcome, "response_mode", None),
                target_input_budget=int(getattr(outcome, "target_input_budget", 0) or 0),
                reserved_output_tokens=int(getattr(outcome, "reserved_output_tokens", 0) or 0),
                preferred_endpoint_id=getattr(outcome, "preferred_endpoint_id", None),
                preferred_model_id=getattr(outcome, "preferred_model_id", None),
            )
            state.pending_assistant_tool_text = str(outcome.text or "")
            state.pending_tool_call_batch = list(tool_calls)
            state.pending_tool_results = []
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
        finish_reason = str(getattr(outcome, "finish_reason", "") or "")
        if finish_reason == LLMFinishReason.ERROR:
            if self._completion_evidence_present() or self._artifact_completion_evidence_present():
                outcome = CanonicalLLMOutcome(
                    text=self._completion_evidence_fallback_text(str(getattr(outcome, "text", "") or "")),
                    finish_reason=LLMFinishReason.STOP,
                )
                finish_reason = str(LLMFinishReason.STOP)
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
        return EffectResult(status=RuntimeStatus.OK, payload=outcome)

    async def _handle_minion_memory_compact(
        self,
        bundle: MinionRuntimeBundle,
        state: MinionAgentLoopState,
        effect: MemoryCompactEffect,
    ) -> EffectResult:
        source_text = self._minion_compaction_source_text(state, target_input_budget=effect.target_input_budget)
        metadata: dict[str, Any] = {}
        structured_method = getattr(bundle.llm_runtime, "acompact_memory_structured", None)
        if callable(structured_method) and source_text:
            with contextlib.suppress(Exception):
                structured = structured_method(
                    source_text,
                    max_output_tokens=max(384, min(effect.reserved_output_tokens or 1024, 2048)),
                    preferred_endpoint_id=_preferred_endpoint_id_from_pack(self.pack),
                )
                metadata["structured_compaction"] = await structured if hasattr(structured, "__await__") else structured
        if "structured_compaction" not in metadata and source_text:
            summary_method = getattr(bundle.llm_runtime, "asummarize_compaction", None)
            if callable(summary_method):
                with contextlib.suppress(Exception):
                    summary = summary_method(
                        source_text,
                        target_input_budget=effect.target_input_budget,
                        reserved_output_tokens=effect.reserved_output_tokens,
                        preferred_endpoint_id=_preferred_endpoint_id_from_pack(self.pack),
                    )
                    metadata["semantic_summary"] = await summary if hasattr(summary, "__await__") else summary
            if "semantic_summary" not in metadata:
                metadata["semantic_summary"] = source_text[: max(256, effect.target_input_budget or 1024)]
        compact_result = state.memory_service.compact(
            MemoryCompactRequest(
                target_input_budget=effect.target_input_budget,
                reserved_output_tokens=effect.reserved_output_tokens,
                metadata=metadata,
            )
        )
        await self._emit_progress(
            "memory_compacted",
            round=state.llm_round_count,
            summary=_preview_text(compact_result.summary, limit=500),
        )
        return EffectResult(status=RuntimeStatus.OK, payload=compact_result)

    async def _handle_minion_tool_call(self, state: MinionAgentLoopState, effect: ToolCallEffect) -> EffectResult:
        tool_call = effect.tool_call
        target_name = _effective_capability_name(tool_call)
        index = len(state.pending_tool_results)
        await self._emit_progress(
            "tool_call_started",
            round=state.llm_round_count,
            tool_call_index=index,
            tool_name=tool_call.name,
            target_name=target_name,
            args_preview=_json_preview(tool_call.args),
        )
        try:
            self._append_debug_log(
                "tool_call_started",
                {
                    "round": state.llm_round_count,
                    "tool_call_index": index,
                    "tool_name": tool_call.name,
                    "target_name": target_name,
                    "args": dict(tool_call.args),
                },
            )
            result = await self._await_with_progress_heartbeat(
                self._execute_allowed_tool(state.execution_runtime, tool_call),
                phase="tool_call_waiting",
                round=state.llm_round_count,
                tool_call_index=index,
                tool_name=tool_call.name,
                target_name=target_name,
            )
        except Exception as exc:
            self._append_debug_log(
                "tool_call_failed",
                {
                    "round": state.llm_round_count,
                    "tool_call_index": index,
                    "tool_name": tool_call.name,
                    "target_name": target_name,
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                },
            )
            await self._emit_progress(
                "tool_call_failed",
                round=state.llm_round_count,
                tool_call_index=index,
                tool_name=tool_call.name,
                target_name=target_name,
                error_type=exc.__class__.__name__,
                error=_preview_text(str(exc), limit=500),
            )
            raise
        state.tool_call_count += 1
        state.pending_tool_results.append(result)
        if state.pending_tool_call_batch and len(state.pending_tool_results) >= len(state.pending_tool_call_batch):
            state.tool_protocol_messages.append(_assistant_tool_message(state.pending_assistant_tool_text, state.pending_tool_call_batch))
            for item in state.pending_tool_results:
                state.tool_protocol_messages.append({"role": "tool", "tool_call_id": str(item.call_id or ""), "content": _tool_result_text(item)})
            await self._commit_minion_l1(
                state,
                [
                    L1TranscriptMessage(
                        role="assistant",
                        content=state.pending_assistant_tool_text,
                        tool_calls=list(state.tool_protocol_messages[-len(state.pending_tool_results) - 1].get("tool_calls") or []),
                    ),
                    *[
                        L1TranscriptMessage(role="tool", content=_tool_result_text(item), tool_call_id=str(item.call_id or ""))
                        for item in state.pending_tool_results
                    ],
                ],
            )
            state.pending_assistant_tool_text = ""
            state.pending_tool_call_batch = []
            state.pending_tool_results = []
        self._append_debug_log(
            "tool_call_completed",
            {
                "round": state.llm_round_count,
                "tool_call_index": index,
                "tool_name": tool_call.name,
                "target_name": target_name,
                "ok": bool(result.ok),
                "status": str(result.status or ""),
                "text": _tool_result_text(result),
                "structured": dict(result.structured or {}),
            },
        )
        await self._emit_progress(
            "tool_call_completed",
            round=state.llm_round_count,
            tool_call_index=index,
            tool_name=tool_call.name,
            target_name=target_name,
            ok=bool(result.ok),
            status=str(result.status or ""),
            text_preview=_preview_text(_tool_result_text(result)),
        )
        return EffectResult(status=RuntimeStatus.OK if result.ok else RuntimeStatus.ERROR, payload=result, text=_tool_result_text(result))

    async def _commit_minion_l1(self, state: MinionAgentLoopState, transcript: list[L1TranscriptMessage]) -> None:
        if not transcript:
            return
        with contextlib.suppress(Exception):
            state.memory_service.commit_l1(MemoryCommitRequest(turn_id=self.run_id, transcript=transcript))

    def _minion_prompt_messages(self, state: MinionAgentLoopState, assembly_context: Any) -> list[dict[str, Any]]:
        scaffold = self._prompt_scaffold()
        system = _render_system_prompt(scaffold)
        memory_text = self._render_minion_memory_context(state)
        retry_note = str((getattr(assembly_context, "metadata", {}) or {}).get("retry_note") or "").strip()
        tool_protocol_messages = list(state.tool_protocol_messages)
        task_parts: list[dict[str, Any]] = []
        if memory_text and not tool_protocol_messages:
            task_parts.append({"type": "text", "text": render_system_reminder(f"Minion run memory:\n{memory_text}")})
        task_parts.append({"type": "text", "text": _render_task_prompt(self.pack)})
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": _coerce_user_content_parts(task_parts)},
            *tool_protocol_messages,
        ]
        trailing_parts: list[dict[str, Any]] = []
        if memory_text and tool_protocol_messages:
            trailing_parts.append({"type": "text", "text": render_system_reminder(f"Minion run memory:\n{memory_text}")})
        if retry_note:
            trailing_parts.append({"type": "text", "text": render_system_reminder(f"Minion retry guidance:\n{retry_note}")})
        if trailing_parts:
            messages.append({"role": "user", "content": _coerce_user_content_parts(trailing_parts)})
        return messages

    def _render_minion_memory_context(self, state: MinionAgentLoopState) -> str:
        pack = state.memory_service.build_pack(MemoryPackRequest(turn_kind="minion", work_order_id=self.pack.work_order_id))
        parts: list[str] = []
        if pack.current_summary is not None and str(pack.current_summary.summary or "").strip():
            parts.append(f"Current summary:\n{pack.current_summary.summary.strip()}")
        entries = [entry for entry in list(pack.l2_working_memory or []) if str(entry.entry_id) != "memory.summary"]
        if entries:
            lines = [f"- {entry.kind}:{entry.title or entry.entry_id}: {entry.summary}" for entry in entries[:8]]
            parts.append("Working memory:\n" + "\n".join(lines))
        records = list(getattr(state.memory_l3, "records", []) or [])
        if records:
            lines = [f"- {record.get('document_kind')}:{record.get('title')}: {record.get('summary')}" for record in records[:8]]
            parts.append("Candidate experience records:\n" + "\n".join(lines))
        return "\n\n".join(parts).strip()

    def _minion_compaction_source_text(self, state: MinionAgentLoopState, *, target_input_budget: int) -> str:
        parts: list[str] = []
        existing = state.memory_service.build_compaction_source_text(target_input_budget=target_input_budget)
        if existing:
            parts.append(existing)
        if state.tool_protocol_messages:
            rendered = []
            for message in state.tool_protocol_messages:
                role = str(message.get("role") or "")
                content = str(message.get("content") or "")
                if message.get("tool_calls"):
                    content += "\n" + json.dumps(message.get("tool_calls"), ensure_ascii=False, sort_keys=True)
                rendered.append(f"{role}: {content}")
            parts.append("[Current Minion Trajectory]\n" + "\n".join(rendered))
        if not parts:
            parts.append(_render_task_prompt(self.pack))
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
        if await self._requires_approval(target_name, tool_call):
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
        return await execution_runtime.execute_tool_async(tool_call, allow_tools=True, turn_id=self.run_id)

    def _runner_owned_git_command_error(self, target_name: str, tool_call: CanonicalToolCall) -> str:
        if str(target_name or "") != "op_exec_shell":
            return ""
        completion_policy = self._completion_policy()
        if str(completion_policy.get("evidence") or "").strip().lower() != "git_commit":
            return ""
        cmd = str((tool_call.args or {}).get("cmd") or "").strip()
        if not _contains_runner_owned_git_mutation(cmd):
            return ""
        return (
            "Do not run git add, git commit, git reset, checkout/switch, clean, merge, rebase, tag, or push through "
            "shell in this minion workspace. Use `op_minion_checkpoint_commit` for milestone checkpoint commits so "
            "Pal can record structured commit evidence."
        )

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
        _ = tool_call
        if self.auto_accept_approvals:
            return False
        high_risk = {str(item) for item in list((self.pack.approval_policy or {}).get("high_risk_capabilities") or [])}
        return str(capability_name) in high_risk

    async def _request_approval(self, capability_name: str, tool_call: CanonicalToolCall) -> str:
        approval_id = f"appr_{uuid4().hex[:16]}"
        await self._emit(
            "approval_requested",
            {
                "approval_id": approval_id,
                "title": "Minion high-risk operation",
                "requested_action": capability_name,
                "risk": "high",
                "impact": "Minion requested permission before running a high-risk operation.",
                "target": capability_name,
                "args_summary": dict(tool_call.args),
            },
        )
        timeout = float((self.pack.approval_policy or {}).get("decision_timeout_seconds") or 300)
        decision_payload = await self.read_decision(timeout)
        decision = str(((decision_payload or {}).get("decision") or {}).get("decision") or "").strip().lower()
        if decision == "accept_all":
            self.auto_accept_approvals = True
        await self._emit("decision_received", {"approval_id": approval_id, "decision": decision or "timeout"})
        return "accept" if decision == "accept_all" else decision

    async def _request_clarification(self, ask_user_question: dict[str, Any]) -> dict[str, Any]:
        clarification_id = f"clarify_{uuid4().hex[:16]}"
        payload = {
            **dict(ask_user_question or {}),
            "clarification_id": clarification_id,
            "run_id": self.run_id,
            "minion_id": self.minion_id,
            "work_order_id": self.pack.work_order_id,
            "status": "pending",
        }
        if not _clarification_questions_are_interactive(payload.get("questions")):
            await self._emit(
                "clarification_unavailable",
                {
                    "clarification_id": clarification_id,
                    "reason": "required clarification questions must include inline options",
                    "summary": _ask_user_question_summary(payload),
                },
            )
            return {}
        await self._emit("clarification_requested", payload)
        timeout = float((self.pack.approval_policy or {}).get("clarification_timeout_seconds") or 3600)
        message = await self.read_decision(timeout)
        if not isinstance(message, dict):
            await self._emit("clarification_timeout", {"clarification_id": clarification_id})
            return {}
        clarification = message.get("clarification") if isinstance(message.get("clarification"), dict) else message
        if not isinstance(clarification, dict):
            return {}
        if str(clarification.get("clarification_id") or "") != clarification_id:
            return {}
        await self._emit(
            "clarification_received",
            {
                "clarification_id": clarification_id,
                "answer_count": len(list(clarification.get("answers") or [])),
            },
        )
        return dict(clarification)

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
            return {"status": "error", "error": "workspace.repo_path is missing"}
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
            "task_id": str((self.pack.metadata or {}).get("task_id") or prompt_view.get("task_id") or ""),
            "module_id": str(module.get("module_id") or ""),
            "milestone_index": self._current_milestone_index(),
            "milestone_id": str(self._current_milestone().get("milestone_id") or ""),
            "summary": self._short_summary(final_text or "minion completed current milestone"),
        }
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
            if checkpoint.get("status") == "committed":
                return {
                    **base_payload,
                    "status": "completed",
                    "commit_sha": str(checkpoint.get("commit_sha") or ""),
                    "git_commit": checkpoint,
                    "evidence": "git_commit",
                    **self._artifact_payload(),
                }
            if checkpoint.get("status") == "no_changes" and self._artifact_completion_evidence_present():
                return {
                    **base_payload,
                    "status": "completed",
                    "commit_sha": str(checkpoint.get("commit_sha") or ""),
                    "git_commit": checkpoint,
                    "evidence": "git_commit",
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
        return {**base_payload, "status": "completed", "evidence": evidence or "text_deliverable", **self._artifact_payload()}

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
                "relative_path": f"milestone_{self._current_milestone_index()}_{self._safe_path_part(self.pack.minion_profile)}{suffix}.md",
                "title": title if not partial else f"{title} (partial truncated output)",
                "role": "primary" if not partial else "partial",
                "mime_type": "text/markdown",
                "content": "\n".join([*header_lines, text, ""]),
            },
        )
        self._record_produced_artifact(artifact)

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
        acceptance = list(milestone.get("acceptance_criteria") or self.pack.acceptance_criteria)
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
            "prompt_view": prompt_view,
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

    @staticmethod
    def _ensure_tool_call_identity(tool_call: CanonicalToolCall) -> CanonicalToolCall:
        call_id = str(getattr(tool_call, "call_id", "") or "").strip() or f"call_{uuid4().hex[:12]}"
        return CanonicalToolCall(name=tool_call.name, args=dict(tool_call.args), call_id=call_id)


def build_slim_minion_runtime(runtime_root: Path) -> MinionRuntimeBundle:
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
    llm_runtime = LLMRuntime(
        endpoint_resolver=EndpointResolver(repository=llm_repository),
        settings_repository=settings,
        endpoint_invoker=build_default_endpoint_invoker(
            credentials=LiteLLMCredentialResolver(secret_store=EncryptedFileSecretStore(secrets_path=str(Path(runtime_root) / "secrets.json"))),
            artifact_manager=artifact_service,
            runtime_root=runtime_root,
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
    for module_id in ("core", "execution", "artifact", "memory", l3_plugin.module_id, "web_search", "web_fetch"):
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


def _prompt_scaffold_summary(scaffold: dict[str, Any]) -> dict[str, Any]:
    continuity = dict(scaffold.get("continuity") or {})
    return {
        "instruction_chars": len(str(scaffold.get("instruction") or "")),
        "acceptance_criteria_count": len(list(scaffold.get("acceptance_criteria") or [])),
        "allowed_capability_count": len(list(scaffold.get("allowed_capabilities") or [])),
        "continuity": {
            "keys": sorted(str(key) for key in continuity.keys()),
            "recent_ledger_count": len(list(continuity.get("recent_ledger") or [])),
            "completed_milestone_count": len(list(continuity.get("completed_milestones") or [])),
            "task_lesson_count": len(list(continuity.get("task_lessons") or [])),
        },
        "current_milestone": dict(scaffold.get("current_milestone") or {}),
        "workspace_policy": dict(scaffold.get("workspace_policy") or {}),
        "completion_policy": dict(scaffold.get("completion_policy") or {}),
    }


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
                    "name": str(spec.get("name") or canonical),
                    "description": str(spec.get("description") or spec.get("display_name") or canonical),
                    "parameters": dict(spec.get("parameters_schema") or {"type": "object", "properties": {}}),
                },
            }
        )
    return result


MINION_DISCOVERY_TOOL_SURFACE = (
    "op_tool_search",
    "op_tool_read",
    "op_tool_call",
)


MINION_DIRECT_WORK_TOOL_SURFACE = (
    "op_file_read",
    "op_file_edit",
    "op_file_write",
    "op_exec_shell",
    "op_minion_checkpoint_commit",
    "op_workspace_tree",
    "op_workspace_search",
    "op_workspace_read",
    "op_minion_artifact_write",
    "op_minion_artifact_edit",
    "op_minion_memory_candidate_write",
    "op_web_search",
    "op_web_read",
    "op_memory_recall",
)


WORKSPACE_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_workspace_tree": {
        "name": "op_workspace_tree",
        "description": "List files under the minion workspace repo_path without modifying anything.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative directory path."},
                "max_depth": {"type": "integer", "default": 2},
                "limit": {"type": "integer", "default": 200},
            },
        },
    },
    "op_workspace_search": {
        "name": "op_workspace_search",
        "description": "Search text files under the minion workspace repo_path without modifying anything.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string", "description": "Workspace-relative directory path."},
                "limit": {"type": "integer", "default": 50},
            },
            "required": ["query"],
        },
    },
    "op_workspace_read": {
        "name": "op_workspace_read",
        "description": "Read a workspace-relative text file without modifying anything.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "default": 1},
                "limit_lines": {"type": "integer", "default": 200},
            },
            "required": ["path"],
        },
    },
    "op_minion_artifact_write": {
        "name": "op_minion_artifact_write",
        "description": "Write one complete minion deliverable file under workspace.artifact_dir and register it as produced artifact evidence. Use this for planner/reviewer plans and long structured output; final chat should only point to the artifact. Duplicate paths get a numbered suffix unless overwrite=true.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "relative_path": {"type": "string", "description": "Artifact-dir-relative file path, for example plan.md."},
                "content": {"type": "string", "description": "UTF-8 text content to write."},
                "title": {"type": "string"},
                "role": {"type": "string", "description": "Artifact role such as primary, evidence, notes, or tests."},
                "mime_type": {"type": "string", "default": "text/markdown"},
                "overwrite": {"type": "boolean", "default": False, "description": "Overwrite an existing artifact path. Defaults to false; duplicate paths get a numbered suffix."},
            },
            "required": ["relative_path", "content"],
        },
    },
    "op_minion_artifact_edit": {
        "name": "op_minion_artifact_edit",
        "description": "Create, append to, or replace a text artifact under workspace.artifact_dir and register it as produced artifact evidence. Use append for long deliverables split into coherent sections; use replace only when rewriting the complete artifact.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "relative_path": {"type": "string", "description": "Artifact-dir-relative file path, for example plan.md."},
                "operation": {"type": "string", "enum": ["append", "replace"], "default": "append"},
                "content": {"type": "string", "description": "UTF-8 text content to append or replace with."},
                "create_if_missing": {"type": "boolean", "default": True},
                "title": {"type": "string"},
                "role": {"type": "string", "description": "Artifact role such as primary, evidence, notes, or tests."},
                "mime_type": {"type": "string", "default": "text/markdown"},
            },
            "required": ["relative_path", "content"],
        },
    },
    "op_minion_memory_candidate_write": {
        "name": "op_minion_memory_candidate_write",
        "description": "Write a reusable memory candidate to this minion run's ephemeral in-memory L3. Pal will ask the user before absorbing candidates into durable memory.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "Memory kind such as fact or case."},
                "scope": {"type": "string", "default": "task"},
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "canonical_key": {"type": "string"},
                "topics": {"type": "array", "items": {"type": "string"}},
                "payload": {"type": "object"},
                "situation_text": {"type": "string"},
                "task_text": {"type": "string"},
                "action_text": {"type": "string"},
                "result_text": {"type": "string"},
            },
            "required": ["kind", "summary"],
        },
    },
    "op_minion_checkpoint_commit": {
        "name": "op_minion_checkpoint_commit",
        "description": (
            "Create the current milestone checkpoint commit in the minion workspace git branch and return structured "
            "commit evidence. Use this after implementation and verification are complete. The tool stages source, "
            "tests, docs, and project config while excluding generated build/cache artifacts and minion_outputs."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short milestone title for the commit message."},
            },
        },
    },
}


def _minion_llm_tool_surface(allowed_capabilities: list[str]) -> list[str]:
    surface = [
        name
        for name in (*MINION_DISCOVERY_TOOL_SURFACE, *MINION_DIRECT_WORK_TOOL_SURFACE)
        if name in allowed_capabilities
    ]
    if surface:
        return surface
    return allowed_capabilities


@dataclass
class MinionScopedExecutionRuntime:
    base_runtime: Any
    allowed_capabilities: list[str]
    workspace: dict[str, Any] = field(default_factory=dict)
    produced_artifacts: list[dict[str, Any]] = field(default_factory=list)
    memory_l3: MockL3Plugin | None = None

    def __post_init__(self) -> None:
        self.allowed_capabilities = filter_minion_allowed_capabilities(self.allowed_capabilities)

    def list_capability_specs(self) -> list[dict[str, Any]]:
        allowed = set(self.allowed_capabilities)
        specs = []
        for name, spec in WORKSPACE_TOOL_SPECS.items():
            if name in allowed and not is_minion_capability_denied(name):
                specs.append(dict(spec))
        list_specs = getattr(self.base_runtime, "list_capability_specs", None)
        if callable(list_specs):
            for spec in list(list_specs()):
                name = str(spec.get("name") or "").strip()
                if name in allowed and not is_minion_capability_denied(name):
                    specs.append(spec)
        return specs

    def get_capability_spec(self, name: str) -> dict[str, Any] | None:
        if name in WORKSPACE_TOOL_SPECS:
            if name not in set(self.allowed_capabilities) or is_minion_capability_denied(name):
                return None
            return dict(WORKSPACE_TOOL_SPECS[name])
        get_spec = getattr(self.base_runtime, "get_capability_spec", None)
        if not callable(get_spec):
            return None
        spec = get_spec(name)
        if spec is None:
            return None
        canonical = str(spec.get("name") or spec.get("canonical_path") or name).strip()
        if canonical not in set(self.allowed_capabilities) or is_minion_capability_denied(canonical):
            return None
        return spec

    async def execute_tool_async(
        self,
        call: CanonicalToolCall,
        *,
        allow_tools: bool = True,
        turn_id: str | None = None,
    ) -> CanonicalToolResult:
        if call.name == "op_tool_search":
            return _capability_result_to_tool_result(
                call,
                ToolSearchTool(runtime=self).invoke(dict(call.args)),
            )
        if call.name == "op_tool_read":
            return _capability_result_to_tool_result(
                call,
                ToolReadTool(runtime=self).invoke(dict(call.args)),
            )
        if call.name == "op_tool_call":
            return await self._execute_scoped_tool_call(call, allow_tools=allow_tools, turn_id=turn_id)
        if call.name in WORKSPACE_TOOL_SPECS:
            if call.name == "op_minion_memory_candidate_write":
                return _minion_memory_candidate_result(call, self.memory_l3)
            if call.name == "op_minion_checkpoint_commit":
                return _minion_checkpoint_commit_result(call, self.workspace)
            result = _workspace_tool_result(call, self.workspace)
            if call.name in {"op_minion_artifact_write", "op_minion_artifact_edit"} and result.ok:
                artifact = dict((result.structured or {}).get("artifact") or result.structured or {})
                if artifact:
                    _append_unique_artifact(self.produced_artifacts, artifact)
            return result
        return await self.base_runtime.execute_tool_async(call, allow_tools=allow_tools, turn_id=turn_id)

    async def _execute_scoped_tool_call(
        self,
        call: CanonicalToolCall,
        *,
        allow_tools: bool = True,
        turn_id: str | None = None,
    ) -> CanonicalToolResult:
        target_name = str(call.args.get("name") or call.args.get("capability") or call.args.get("tool") or "").strip()
        if not target_name:
            return CanonicalToolResult(
                name=call.name,
                ok=False,
                text="name is required",
                structured={"reason": "name_required"},
                call_id=call.call_id,
                llm_text="name is required",
                status=RuntimeStatus.INVALID,
            )
        spec = self.get_capability_spec(target_name)
        canonical = str((spec or {}).get("name") or (spec or {}).get("canonical_path") or target_name).strip()
        if spec is None or canonical == call.name:
            return CanonicalToolResult(
                name=call.name,
                ok=False,
                text=f"capability is not allowed for this minion: {target_name}",
                structured={"reason": "capability_not_allowed", "target": target_name},
                call_id=call.call_id,
                llm_text=f"capability is not allowed for this minion: {target_name}",
                status=RuntimeStatus.ERROR,
            )
        return await self.execute_tool_async(
            CanonicalToolCall(name=canonical, args=dict(call.args.get("args") or {}), call_id=call.call_id),
            allow_tools=allow_tools,
            turn_id=turn_id,
        )


def _capability_result_to_tool_result(call: CanonicalToolCall, result: CapabilityResult) -> CanonicalToolResult:
    return CanonicalToolResult(
        name=call.name,
        ok=result.status == RuntimeStatus.OK,
        text=result.text,
        structured=result.structured,
        call_id=call.call_id,
        llm_text=getattr(result, "llm_text", ""),
        status=result.status,
    )


def _minion_memory_candidate_result(call: CanonicalToolCall, memory_l3: MockL3Plugin | None) -> CanonicalToolResult:
    if memory_l3 is None:
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text="minion memory candidate store is not available",
            structured={"reason": "minion_memory_unavailable"},
            call_id=call.call_id,
            llm_text="minion memory candidate store is not available",
            status=RuntimeStatus.ERROR,
        )
    try:
        result = memory_l3.commit(
            L3CommitRequest(
                kind=str(call.args.get("kind") or "case"),
                scope=str(call.args.get("scope") or "task"),
                title=str(call.args.get("title") or ""),
                summary=str(call.args.get("summary") or ""),
                canonical_key=str(call.args.get("canonical_key")) if call.args.get("canonical_key") is not None else None,
                payload=dict(call.args.get("payload") or {}),
                topics=[str(value) for value in list(call.args.get("topics") or [])],
                situation_text=str(call.args.get("situation_text") or ""),
                task_text=str(call.args.get("task_text") or ""),
                action_text=str(call.args.get("action_text") or ""),
                result_text=str(call.args.get("result_text") or ""),
            )
        )
        payload = {"memory_candidate": result.hit or {"document_id": result.document_id}}
        return CanonicalToolResult(
            name=call.name,
            ok=result.status == RuntimeStatus.OK,
            text="memory candidate recorded",
            structured=payload,
            call_id=call.call_id,
            llm_text="memory candidate recorded",
            status=result.status,
        )
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=message,
            structured={"error": message, "error_type": exc.__class__.__name__},
            call_id=call.call_id,
            llm_text=message,
            status=RuntimeStatus.ERROR,
        )


def _minion_checkpoint_commit_result(call: CanonicalToolCall, workspace: dict[str, Any]) -> CanonicalToolResult:
    try:
        repo_path = str((workspace or {}).get("repo_path") or "").strip()
        if not repo_path:
            raise ValueError("workspace.repo_path is not available")
        repo = Path(repo_path)
        title = str(call.args.get("title") or workspace.get("current_milestone_title") or "").strip()
        result = commit_milestone(
            repo,
            work_order_id=str(workspace.get("work_order_id") or "work_order"),
            milestone_index=_coerce_int(workspace.get("current_milestone_index"), default=0),
            title=title,
        )
        status = str(result.get("status") or "").strip()
        if status == "no_changes":
            inspected = inspect_milestone_checkpoint(repo, base_sha=str(workspace.get("base_sha") or ""))
            if inspected.get("status") == "committed":
                payload = {**inspected, "already_committed": True}
                return CanonicalToolResult(
                    name=call.name,
                    ok=True,
                    text=f"Milestone checkpoint already committed: {payload.get('commit_sha')}",
                    structured=payload,
                    call_id=call.call_id,
                    llm_text=f"Milestone checkpoint already committed: {payload.get('commit_sha')}",
                    status=RuntimeStatus.OK,
                )
            return CanonicalToolResult(
                name=call.name,
                ok=False,
                text="No milestone changes to commit.",
                structured={**result, **inspected},
                call_id=call.call_id,
                llm_text="No milestone changes to commit.",
                status=RuntimeStatus.ERROR,
            )
        ok = status == "committed"
        text = (
            f"Milestone checkpoint committed: {result.get('commit_sha')}"
            if ok
            else str(result.get("error") or f"Milestone checkpoint commit failed: {status}")
        )
        return CanonicalToolResult(
            name=call.name,
            ok=ok,
            text=text,
            structured=result,
            call_id=call.call_id,
            llm_text=text,
            status=RuntimeStatus.OK if ok else RuntimeStatus.ERROR,
        )
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=message,
            structured={"error": message, "error_type": exc.__class__.__name__},
            call_id=call.call_id,
            llm_text=message,
            status=RuntimeStatus.ERROR,
        )


def _workspace_tool_result(call: CanonicalToolCall, workspace: dict[str, Any]) -> CanonicalToolResult:
    try:
        if call.name == "op_minion_artifact_write":
            artifact = _write_minion_artifact(workspace, call.args)
            payload = {"artifact": artifact}
            text = f"Artifact written: {artifact['relative_path']}"
        elif call.name == "op_minion_artifact_edit":
            artifact = _edit_minion_artifact(workspace, call.args)
            payload = {"artifact": artifact}
            text = f"Artifact edited: {artifact['relative_path']}"
        else:
            root = _workspace_root(workspace)
            if call.name == "op_workspace_tree":
                payload = _workspace_tree(root, call.args)
                text = "\n".join(item["path"] for item in payload["items"])
            elif call.name == "op_workspace_search":
                payload = _workspace_search(root, call.args)
                text = "\n".join(f"{item['path']}:{item['line_number']}: {item['line']}" for item in payload["matches"])
            elif call.name == "op_workspace_read":
                payload = _workspace_read(root, call.args)
                text = payload["text"]
            else:
                raise ValueError(f"unknown workspace tool: {call.name}")
        return CanonicalToolResult(
            name=call.name,
            ok=True,
            text=text,
            structured=payload,
            call_id=call.call_id,
            llm_text=text,
            status=RuntimeStatus.OK,
        )
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=message,
            structured={"error": message, "error_type": exc.__class__.__name__},
            call_id=call.call_id,
            llm_text=message,
            status=RuntimeStatus.ERROR,
        )


def _workspace_root(workspace: dict[str, Any]) -> Path:
    repo_path = str((workspace or {}).get("repo_path") or "").strip()
    if not repo_path:
        raise ValueError("workspace.repo_path is not available")
    root = Path(repo_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"workspace.repo_path is not a directory: {root}")
    return root


def _artifact_root(workspace: dict[str, Any]) -> Path:
    artifact_dir = str((workspace or {}).get("artifact_dir") or "").strip()
    if not artifact_dir:
        raise ValueError("workspace.artifact_dir is not available")
    root = Path(artifact_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _artifact_path(root: Path, raw_path: Any) -> Path:
    relative = str(raw_path or "").strip()
    if not relative:
        raise ValueError("relative_path is required")
    if Path(relative).is_absolute():
        raise ValueError("artifact path must be relative to artifact_dir")
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("artifact path escapes artifact_dir")
    if candidate == root:
        raise ValueError("artifact path must name a file")
    return candidate


def _write_minion_artifact(workspace: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    root = _artifact_root(workspace)
    path = _artifact_path(root, args.get("relative_path"))
    content = str(args.get("content") or "")
    if not content.strip():
        raise ValueError("artifact content is required")
    if path.exists() and not bool(args.get("overwrite")):
        path = _next_available_artifact_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return _artifact_metadata(root, path, args)


def _edit_minion_artifact(workspace: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    root = _artifact_root(workspace)
    path = _artifact_path(root, args.get("relative_path"))
    operation = str(args.get("operation") or "append").strip().lower() or "append"
    if operation not in {"append", "replace"}:
        raise ValueError("operation must be append or replace")
    content = str(args.get("content") or "")
    if not content.strip():
        raise ValueError("artifact content is required")
    create_if_missing = bool(args.get("create_if_missing", True))
    if not path.exists() and not create_if_missing:
        raise ValueError("artifact does not exist")
    path.parent.mkdir(parents=True, exist_ok=True)
    if operation == "replace":
        path.write_text(content, encoding="utf-8")
    else:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(content)
    return _artifact_metadata(root, path, args)


def _artifact_metadata(root: Path, path: Path, args: dict[str, Any]) -> dict[str, Any]:
    content = path.read_text(encoding="utf-8", errors="ignore")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    relative_path = str(path.relative_to(root)).replace("\\", "/")
    mime_type = str(args.get("mime_type") or mimetypes.guess_type(path.name)[0] or "text/plain").strip()
    role = str(args.get("role") or "primary").strip() or "primary"
    return {
        "kind": "file",
        "path": str(path),
        "relative_path": relative_path,
        "title": str(args.get("title") or path.stem).strip() or path.name,
        "role": role,
        "mime_type": mime_type,
        "size_bytes": path.stat().st_size,
        "sha256": digest,
    }


def _next_available_artifact_path(path: Path) -> Path:
    if not path.exists():
        return path
    parent = path.parent
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = parent / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise ValueError(f"could not allocate unique artifact path for {path.name}")


def _append_unique_artifact(items: list[dict[str, Any]], artifact: dict[str, Any]) -> None:
    path = str(artifact.get("path") or "").strip()
    relative_path = str(artifact.get("relative_path") or "").strip()
    for index, existing in enumerate(items):
        if path and str(existing.get("path") or "") == path:
            items[index] = dict(artifact)
            return
        if relative_path and str(existing.get("relative_path") or "") == relative_path:
            items[index] = dict(artifact)
            return
    items.append(dict(artifact))


def _workspace_path(root: Path, raw_path: Any = "") -> Path:
    relative = str(raw_path or ".").strip() or "."
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("workspace path escapes repo_path")
    return candidate


def _workspace_tree(root: Path, args: dict[str, Any]) -> dict[str, Any]:
    base = _workspace_path(root, args.get("path") or ".")
    if not base.exists():
        raise ValueError(f"workspace path does not exist: {base.relative_to(root)}")
    max_depth = max(0, min(_optional_positive_int(args.get("max_depth")) or 2, 8))
    limit = max(1, min(_optional_positive_int(args.get("limit")) or 200, 1000))
    items: list[dict[str, Any]] = []
    if base.is_file():
        stat = base.stat()
        items.append({"path": str(base.relative_to(root)).replace("\\", "/"), "kind": "file", "size_bytes": stat.st_size})
        return {"root": str(root), "items": items, "count": len(items)}
    base_depth = len(base.relative_to(root).parts) if base != root else 0
    for current, dirs, files in os.walk(base):
        current_path = Path(current)
        rel_parts = current_path.relative_to(root).parts if current_path != root else ()
        depth = len(rel_parts) - base_depth
        dirs[:] = [name for name in sorted(dirs) if name not in {".git", "__pycache__", ".pytest_cache"}]
        for name in dirs:
            if len(items) >= limit:
                return {"root": str(root), "items": items, "count": len(items), "truncated": True}
            path = current_path / name
            items.append({"path": str(path.relative_to(root)).replace("\\", "/"), "kind": "dir"})
        for name in sorted(files):
            if len(items) >= limit:
                return {"root": str(root), "items": items, "count": len(items), "truncated": True}
            path = current_path / name
            with contextlib.suppress(OSError):
                items.append({"path": str(path.relative_to(root)).replace("\\", "/"), "kind": "file", "size_bytes": path.stat().st_size})
        if depth >= max_depth:
            dirs[:] = []
    return {"root": str(root), "items": items, "count": len(items)}


def _workspace_search(root: Path, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    base = _workspace_path(root, args.get("path") or ".")
    limit = max(1, min(_optional_positive_int(args.get("limit")) or 50, 500))
    matches: list[dict[str, Any]] = []
    query_lower = query.lower()
    paths = [base] if base.is_file() else [path for path in base.rglob("*") if path.is_file()]
    for path in paths:
        if ".git" in path.relative_to(root).parts:
            continue
        try:
            for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
                if query_lower not in line.lower():
                    continue
                matches.append(
                    {
                        "path": str(path.relative_to(root)).replace("\\", "/"),
                        "line_number": line_number,
                        "line": _preview_text(line, limit=300),
                    }
                )
                if len(matches) >= limit:
                    return {"root": str(root), "query": query, "matches": matches, "count": len(matches), "truncated": True}
        except OSError:
            continue
    return {"root": str(root), "query": query, "matches": matches, "count": len(matches)}


def _workspace_read(root: Path, args: dict[str, Any]) -> dict[str, Any]:
    path = _workspace_path(root, args.get("path") or "")
    if not path.is_file():
        raise ValueError(f"workspace path is not a file: {path.relative_to(root)}")
    start_line = max(1, _optional_positive_int(args.get("start_line")) or 1)
    limit_lines = max(1, min(_optional_positive_int(args.get("limit_lines")) or 200, 1000))
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    selected = lines[start_line - 1 : start_line - 1 + limit_lines]
    numbered = [f"{index}: {line}" for index, line in enumerate(selected, start=start_line)]
    return {
        "root": str(root),
        "path": str(path.relative_to(root)).replace("\\", "/"),
        "start_line": start_line,
        "line_count": len(selected),
        "truncated": start_line - 1 + limit_lines < len(lines),
        "text": "\n".join(numbered),
    }


def _effective_capability_name(tool_call: CanonicalToolCall) -> str:
    if tool_call.name == "op_tool_call":
        return str(tool_call.args.get("name") or tool_call.name).strip()
    return tool_call.name


def _assistant_tool_message(text: str, tool_calls: list[CanonicalToolCall]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": text or "",
        "tool_calls": [
            {
                "id": str(tool_call.call_id or ""),
                "type": "function",
                "function": {"name": tool_call.name, "arguments": json.dumps(tool_call.args, ensure_ascii=False, sort_keys=True)},
            }
            for tool_call in tool_calls
        ],
    }


def _tool_result_text(result: CanonicalToolResult) -> str:
    if str(result.llm_text or "").strip():
        return str(result.llm_text).strip()
    if str(result.text or "").strip():
        return str(result.text).strip()
    if result.structured:
        return json.dumps(result.structured, ensure_ascii=False, sort_keys=True)
    return "tool completed" if result.ok else "tool failed"


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


def _optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _resolve_minion_max_output_tokens(llm_runtime: Any, pack: TaskContextPack) -> int:
    metadata = pack.metadata if isinstance(pack.metadata, dict) else {}
    explicit = _optional_positive_int(metadata.get("max_output_tokens"))
    if explicit is not None:
        return explicit
    preferred_endpoint_id = _preferred_endpoint_id_from_pack(pack)
    resolved = _runtime_max_output_tokens(llm_runtime, preferred_endpoint_id=preferred_endpoint_id)
    if resolved is not None:
        return resolved
    facts = _runtime_endpoint_facts(llm_runtime, preferred_endpoint_id=preferred_endpoint_id)
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
        "response_mode_hint": "operational",
        "minion_run_id": str(run_id or ""),
        "max_output_tokens_source": "minion",
    }
    preferred_endpoint_id = _preferred_endpoint_id_from_pack(pack)
    if preferred_endpoint_id:
        metadata["preferred_endpoint_id"] = preferred_endpoint_id
    return metadata


def _preferred_endpoint_id_from_pack(pack: TaskContextPack) -> str | None:
    metadata = pack.metadata if isinstance(pack.metadata, dict) else {}
    value = str(metadata.get("preferred_endpoint_id") or "").strip()
    return value or None


def _runtime_max_output_tokens(llm_runtime: Any, *, preferred_endpoint_id: str | None = None) -> int | None:
    resolver = getattr(llm_runtime, "resolve_max_output_tokens", None)
    if not callable(resolver):
        return None
    with contextlib.suppress(Exception):
        try:
            return _optional_positive_int(resolver(preferred_endpoint_id=preferred_endpoint_id))
        except TypeError:
            return _optional_positive_int(resolver())
    return None


def _runtime_endpoint_facts(llm_runtime: Any, *, preferred_endpoint_id: str | None = None) -> dict[str, Any]:
    resolver = getattr(llm_runtime, "resolve_endpoint_facts", None)
    if not callable(resolver):
        return {}
    with contextlib.suppress(Exception):
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


_RUNNER_OWNED_GIT_MUTATION_RE = re.compile(
    r"(?:^|[;&|()]\s*)git\s+"
    r"(?:add|commit|reset|checkout|switch|clean|rm|mv|merge|rebase|tag|push|branch)\b"
)


def _contains_runner_owned_git_mutation(cmd: str) -> bool:
    return bool(_RUNNER_OWNED_GIT_MUTATION_RE.search(str(cmd or "")))


def _render_system_prompt(scaffold: dict[str, Any]) -> str:
    completion_policy = scaffold.get("completion_policy") or {}
    testing_guidance = ""
    if isinstance(completion_policy, dict) and bool(completion_policy.get("requires_developer_tests")):
        testing_guidance = (
            "Completion requires developer test evidence: before completing, state the focused test plan, "
            "run the relevant tests/checks available through listed capabilities, fix failures you caused, "
            "and report blocked instead of completed if tests cannot be run or cannot pass with concrete evidence.\n"
        )
    operating_rules = (
        "Your context is the prompt_view/task context pack, the current milestone, and the listed capabilities.\n"
        "When prompt_view is present, treat it as the complete scoped assignment; do not infer or implement other modules.\n"
        "Use only the listed capabilities. Report by milestone, never by percentage or ETA.\n"
        "Use `op_memory_recall` when prior Pal experience, project lessons, or user preferences may materially improve the result.\n"
        "If capability evidence is required, use a relevant listed capability before completing the milestone.\n"
        f"{testing_guidance}"
        "If completion evidence cannot be produced, report blocked instead of completed.\n"
        "When completion policy requires git_commit, do not run git add, git commit, or other git mutation commands through shell. "
        "After implementing and verifying the milestone, call `op_minion_checkpoint_commit` to create the structured checkpoint commit in the minion workspace branch.\n"
        "Do not create or rely on committing generated build/cache artifacts such as __pycache__, .pytest_cache, .o, .obj, .a, .so, .dylib, .dll, .exe, class files, coverage output, build directories, or minion_outputs reports.\n"
        "When `op_minion_artifact_write` or `op_minion_artifact_edit` is available, write planner/reviewer deliverables and any long structured output to workspace.artifact_dir with artifact tools; keep the final chat summary short and point to the artifact.\n"
        "Use `op_minion_artifact_write` for one complete coherent file. Use `op_minion_artifact_edit` append for long deliverables split into coherent sections, or replace only when rewriting the complete artifact. Do not rely on final chat text for long plans or reports.\n"
        "When `op_minion_memory_candidate_write` is available and the run teaches something genuinely reusable, write a concise memory candidate there instead of asking Pal to remember it directly.\n"
        "If a tool/capability call fails because of an obvious schema, argument, path, or local input mistake, correct the call directly.\n"
        "If a tool/capability call fails and the next step is unclear, repeated retries would be guesswork, or the failure may have prior Pal/project repair history, use `op_memory_recall` when it is listed below before retrying, debugging further, or reporting blocked.\n"
        "When the current milestone is complete, stop with a concise milestone summary. "
        "Pal will ask the user before absorbing minion memory candidates."
    )
    blocks = [
        ("identity", str(scaffold.get("identity") or "").strip()),
        ("behavior_guidance", str(scaffold.get("behavior") or "").strip()),
        ("system-reminder", _render_skill_manual_context(scaffold.get("skill_manual_context"))),
        ("operating_rules", operating_rules.strip()),
        ("workspace_policy", json.dumps(scaffold.get("workspace_policy") or {}, ensure_ascii=False, sort_keys=True)),
        ("completion_policy", json.dumps(scaffold.get("completion_policy") or {}, ensure_ascii=False, sort_keys=True)),
        ("output_contract", str(scaffold.get("output_contract") or "").strip()),
        ("allowed_capabilities", json.dumps(scaffold.get("allowed_capabilities") or [], ensure_ascii=False)),
    ]
    return "\n\n".join(render_xml_block(tag, content) for tag, content in blocks if str(content or "").strip()).strip()


def _render_skill_manual_context(items: Any) -> str:
    blocks: list[str] = []
    for item in list(items or []):
        if not isinstance(item, dict):
            continue
        skill_id = str(item.get("skill_id") or "").strip()
        manual_text = str(item.get("manual_text") or "").strip()
        if not skill_id or not manual_text:
            continue
        title = str(item.get("title") or skill_id).strip()
        summary = str(item.get("summary") or "").strip()
        use_when = str(item.get("use_when") or "").strip()
        avoid_when = str(item.get("avoid_when") or "").strip()
        parts = [
            f"Skill: {skill_id} - {title}",
            f"Summary: {summary}" if summary else "",
            f"Use when: {use_when}" if use_when else "",
            f"Avoid when: {avoid_when}" if avoid_when else "",
            "Manual:",
            manual_text,
        ]
        blocks.append("\n".join(part for part in parts if part).strip())
    return "\n\n".join(blocks).strip()


def _coerce_user_content_parts(parts: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    if len(parts) == 1 and parts[0].get("type") == "text":
        return str(parts[0].get("text") or "")
    return parts


def _prompt_view_from_pack(pack: TaskContextPack) -> dict[str, Any]:
    metadata = dict(pack.metadata or {})
    prompt_view = prompt_view_from_metadata(metadata, workspace=dict(pack.workspace))
    if prompt_view:
        if pack.allowed_capabilities:
            prompt_view["allowed_capabilities"] = list(pack.allowed_capabilities)
        return prompt_view
    continuity = dict(pack.continuity or {})
    if isinstance(continuity.get("prompt_view"), dict):
        prompt_view = prompt_view_from_metadata({"prompt_view": dict(continuity.get("prompt_view") or {})}, workspace=dict(pack.workspace))
        if prompt_view and pack.allowed_capabilities:
            prompt_view["allowed_capabilities"] = list(pack.allowed_capabilities)
        return prompt_view
    return {}


def _render_task_prompt(pack: TaskContextPack) -> str:
    prompt_view = _prompt_view_from_pack(pack)
    if prompt_view:
        instructions = [
            "Execute only the scoped work in this prompt_view.",
            "Use module contracts instead of inferring other module internals.",
        ]
        if str(prompt_view.get("role") or "").strip().lower() == "planner":
            instructions.append(
                "If a question is user-answerable and materially changes the plan, return ask_user_question with evidence."
            )
        payload = {
            "prompt_view": prompt_view,
            "instructions": instructions,
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
    payload = {
        "work_order_id": pack.work_order_id,
        "goal": pack.goal,
        "instruction": pack.instruction,
        "acceptance_criteria": list(pack.acceptance_criteria),
        "workspace": dict(pack.workspace),
        "continuity": dict(pack.continuity),
        "artifacts": list(pack.artifacts),
        "memory_pack": dict(pack.memory_pack),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


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


def _ask_user_question_summary(payload: dict[str, Any]) -> str:
    questions = [dict(item) for item in list(payload.get("questions") or []) if isinstance(item, dict)]
    if not questions:
        return "minion asked a user clarification question"
    first = str(questions[0].get("question") or "minion asked a user clarification question").strip()
    if len(questions) == 1:
        return first
    return f"{first} (+{len(questions) - 1} more)"


def _clarification_questions_are_interactive(value: Any) -> bool:
    questions = [dict(item) for item in list(value or []) if isinstance(item, dict)]
    if not questions:
        return False
    for question in questions[:3]:
        options = [dict(item) for item in list(question.get("options") or []) if isinstance(item, dict)]
        if not options:
            return False
    return True


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
