from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, replace
from functools import singledispatchmethod
from typing import Any, Awaitable, Callable
from uuid import uuid4

from pal.execution.contracts import ToolCallBudget
from pal.core.runtime_config import RuntimeConfig
from pal.core.tool_stagnation import (
    ToolExecutionRecord,
    canonical_result_fingerprint,
    canonical_tool_signature_hash,
)
from pal.core.turns import (
    EffectResult,
    LLMPreflightEffect,
    LLMRequestEffect,
    MailboxReplyEffect,
    MailboxReplyStreamEffect,
    MemoryCompactEffect,
    ToolCallEffect,
    ToolObservation,
)
from pal.failure import FailureSignal
from pal.llm.contracts import CanonicalLLMOutcome, CanonicalLLMRequest, CanonicalToolResult, LLMPreflightRequest
from pal.memory.contracts import L2Entry, MemoryCommitRequest, MemoryCompactRequest, MemoryPackRequest
from pal.shared import GuardAction, LLMFinishReason, LLMPreflightStatus, LLMResponseMode, LLMStreamEventKind, RuntimeStatus
from pal.shared.payloads import extract_text_from_payload
from pal.stream_events import NormalizedLLMStreamEvent

_FORWARD_STREAM_KINDS = frozenset({
    LLMStreamEventKind.TEXT_DELTA,
    LLMStreamEventKind.REASONING_DELTA,
    LLMStreamEventKind.TOOL_CALL,
    LLMStreamEventKind.DONE,
    LLMStreamEventKind.ERROR,
})

class TurnExecutor:
    def __init__(
        self,
        context,
        state,
        turn_manager,
        *,
        call_port_async: Callable[..., Awaitable[Any]],
        build_canonical_prompt: Callable[..., Any],
        debug_log_prompt: Callable[..., None],
        debug_log_outcome: Callable[..., None],
        debug_log_reply: Callable[..., None],
        build_llm_tool_contracts: Callable[[], list[dict[str, object]]],
        handle_failure_async: Callable[..., Awaitable[Any]],
        render_failure_feedback_text: Callable[[Any], str],
        should_enter_failure_flow_for_tool_result: Callable[[Any], bool],
        config: RuntimeConfig | None = None,
    ) -> None:
        self.context = context
        self.state = state
        self.turn_manager = turn_manager
        self._config = config or RuntimeConfig.defaults()
        self._call_port_async = call_port_async
        self._build_canonical_prompt = build_canonical_prompt
        self._debug_log_prompt = debug_log_prompt
        self._debug_log_outcome = debug_log_outcome
        self._debug_log_reply = debug_log_reply
        self._build_llm_tool_contracts = build_llm_tool_contracts
        self._handle_failure_async = handle_failure_async
        self._render_failure_feedback_text = render_failure_feedback_text
        self._should_enter_failure_flow_for_tool_result = should_enter_failure_flow_for_tool_result

        self._stream_accumulators = {
            LLMStreamEventKind.TEXT_DELTA: self._accumulate_text_delta,
            LLMStreamEventKind.REASONING_DELTA: self._accumulate_reasoning_delta,
            LLMStreamEventKind.TOOL_CALL: self._accumulate_tool_call,
            LLMStreamEventKind.ERROR: self._accumulate_error,
            LLMStreamEventKind.DONE: self._accumulate_done,
        }

    # ── public entry point ──────────────────────────────────────────────

    def execute_turn_effect(self, continuation, effect):
        return asyncio.run(self.execute_turn_effect_async(continuation, effect))

    async def execute_turn_effect_async(self, continuation, effect):
        self._ensure_not_interrupted(continuation)
        continuation.waiting_effect_id = effect.effect_id
        result = await self._dispatch_effect(effect, continuation)
        self._ensure_not_interrupted(continuation)
        continuation.waiting_effect_id = None
        return result

    # ── effect dispatch (singledispatch — C++ overload style) ────────────

    @singledispatchmethod
    async def _dispatch_effect(self, effect, continuation):
        return EffectResult(status=RuntimeStatus.UNSUPPORTED, text=f"unknown effect: {effect.kind}")

    @_dispatch_effect.register(LLMPreflightEffect)
    async def _handle_llm_preflight(self, effect, continuation):
        llm_runtime = self.context.require_port("llm:llm")
        tools = self._resolve_llm_tools(continuation, effect.tools_override)
        prompt = self.build_turn_prompt(
            continuation,
            effect.assembly_context,
            max_output_tokens=effect.max_output_tokens,
            tools=tools,
        )
        advice = await self._call_port_async(
            llm_runtime,
            "apreflight",
            "preflight",
            LLMPreflightRequest(
                messages=prompt.messages,
                max_output_tokens=prompt.max_output_tokens,
                model_hint=prompt.model_hint,
                tools=tools,
                metadata=dict(prompt.metadata),
            )
        )
        continuation.prompt_budget_snapshot = dict(getattr(advice, "breakdown", {}) or {})
        if self._is_hard_budget_overflow(advice):
            failure_result = await self._handle_failure_async(
                FailureSignal(
                    subsystem="core",
                    component="turn_budget",
                    failure_kind="context_budget_exhausted",
                    severity="high",
                    primary_blocker="The current turn exceeds the available context window before any older history can be compacted.",
                    evidence={
                        "prompt_budget": dict(getattr(advice, "breakdown", {}) or {}),
                        "preferred_endpoint_id": prompt.metadata.get("preferred_endpoint_id"),
                        "preferred_model_id": prompt.metadata.get("preferred_model_id"),
                    },
                    related_ids={"turn_id": continuation.turn_id},
                    safe_to_retry=False,
                    repair_domain="core:budgeting",
                ),
                origin="prompt_budget",
                conversation_context={"turn_id": continuation.turn_id},
            )
            continuation.budget_failure_feedback_text = self._render_failure_feedback_text(failure_result.user_feedback)
            advice = replace(advice, status=LLMPreflightStatus.READY)
        return EffectResult(status=RuntimeStatus.OK, payload=advice)

    @_dispatch_effect.register(MemoryCompactEffect)
    async def _handle_memory_compact(self, effect, continuation):
        memory_service = self.context.require_port("memory:memory")
        metadata = dict(effect.assembly_context.metadata)
        metadata.update(await self.build_compaction_metadata_async(
            memory_service,
            target_input_budget=effect.target_input_budget,
            reserved_output_tokens=effect.reserved_output_tokens,
            preferred_endpoint_id=metadata.get("preferred_endpoint_id"),
            preferred_model_id=metadata.get("preferred_model_id"),
        ))
        compact_result = await self._call_port_async(
            memory_service,
            "acompact",
            "compact",
            MemoryCompactRequest(
                target_input_budget=effect.target_input_budget,
                reserved_output_tokens=effect.reserved_output_tokens,
                metadata=metadata,
            )
        )
        return EffectResult(status=RuntimeStatus.OK, payload=compact_result)

    @_dispatch_effect.register(LLMRequestEffect)
    async def _handle_llm_request(self, effect, continuation):
        if continuation.budget_failure_feedback_text:
            text = continuation.budget_failure_feedback_text
            continuation.budget_failure_feedback_text = ""
            return EffectResult(
                status=RuntimeStatus.OK,
                payload=CanonicalLLMOutcome(
                    text=text,
                    reasoning_text="",
                    tool_calls=[],
                    finish_reason=LLMFinishReason.FALLBACK,
                    response_mode=LLMResponseMode.CHAT,
                ),
            )
        llm_runtime = self.context.require_port("llm:llm")
        tools = self._resolve_llm_tools(continuation, effect.tools_override)
        prompt = self.build_turn_prompt(
            continuation,
            effect.assembly_context,
            max_output_tokens=effect.max_output_tokens,
            tools=tools,
        )
        request = CanonicalLLMRequest(
            messages=list(prompt.messages),
            max_output_tokens=prompt.max_output_tokens,
            model_hint=prompt.model_hint,
            temperature=self.select_turn_temperature(continuation.last_response_mode),
            tools=tools,
            metadata=dict(prompt.metadata),
        )
        self._debug_log_prompt(continuation, request)
        if self.should_stream_reply(continuation.channel_envelope) and hasattr(llm_runtime, "generate_stream"):
            outcome = await self.stream_llm_request_async(continuation, llm_runtime, request)
        else:
            outcome = await self._call_port_async(llm_runtime, "agenerate", "generate", request)
        self._debug_log_outcome(continuation, outcome)
        preferred_endpoint_id = str(getattr(outcome, "preferred_endpoint_id", "") or "").strip() or None
        preferred_model_id = str(getattr(outcome, "preferred_model_id", "") or "").strip() or None
        if preferred_endpoint_id is None:
            preferred_endpoint_id = str(getattr(llm_runtime, "last_endpoint_id", "") or "").strip() or None
        if preferred_model_id is None:
            preferred_model_id = str(getattr(llm_runtime, "last_model_id", "") or "").strip() or None
        continuation.preferred_llm_endpoint_id = preferred_endpoint_id
        continuation.preferred_llm_model_id = preferred_model_id
        continuation.last_response_mode = self.infer_response_mode(
            outcome,
            used_tools=bool(continuation.tool_observations),
        )
        if outcome.tool_calls:
            continuation.pending_assistant_tool_text = str(outcome.text or "")
            continuation.pending_tool_call_batch = [
                self._ensure_tool_call_identity(tool_call) for tool_call in list(outcome.tool_calls)
            ]
            continuation.pending_tool_results = []
        if continuation.finalization_only:
            if outcome.finish_reason != LLMFinishReason.COMPACT_REQUIRED:
                continuation.finalization_attempted = True
            if outcome.finish_reason != LLMFinishReason.COMPACT_REQUIRED and (outcome.tool_calls or not outcome.text.strip()):
                outcome = CanonicalLLMOutcome(
                    text=self.fallback_final_reply(continuation),
                    tool_calls=[],
                    finish_reason=LLMFinishReason.FALLBACK,
                    response_mode=LLMResponseMode.CHAT,
                )
        if effect.assembly_context.turn_kind != "failure" and outcome.finish_reason == LLMFinishReason.ERROR:
            failure_result = await self._handle_failure_async(
                FailureSignal(
                    subsystem="llm",
                    component=continuation.preferred_llm_endpoint_id
                    or str(getattr(llm_runtime, "last_endpoint_id", "") or "")
                    or "llm_runtime",
                    failure_kind="provider_failure",
                    severity="high",
                    primary_blocker=str(outcome.text or "LLM generation failed."),
                    evidence={"llm_text": outcome.text, "preferred_model_id": continuation.preferred_llm_model_id},
                    related_ids={"turn_id": continuation.turn_id},
                    safe_to_retry=False,
                    repair_domain="llm:core",
                ),
                origin="llm_request",
                conversation_context={"turn_id": continuation.turn_id},
            )
            outcome = CanonicalLLMOutcome(
                text=self._render_failure_feedback_text(failure_result.user_feedback),
                reasoning_text="",
                tool_calls=[],
                finish_reason=LLMFinishReason.FALLBACK,
                response_mode=LLMResponseMode.CHAT,
        )
        return EffectResult(status=RuntimeStatus.OK, payload=outcome)

    def _resolve_llm_tools(self, continuation, tools_override: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        if tools_override is not None:
            return list(tools_override)
        return [] if continuation.finalization_only else self._build_llm_tool_contracts()

    @_dispatch_effect.register(ToolCallEffect)
    async def _handle_tool_call(self, effect, continuation):
        execution_call = effect.tool_call
        if not str(getattr(execution_call, "call_id", "") or "").strip() and continuation.pending_tool_call_batch:
            pending_index = len(continuation.pending_tool_results)
            if 0 <= pending_index < len(continuation.pending_tool_call_batch):
                execution_call = continuation.pending_tool_call_batch[pending_index]
        tool_budget = self._build_tool_call_budget(continuation, execution_call=execution_call)
        self._log_tool_call_start(continuation, execution_call)
        self.context.turn_event_bus.emit("turn.tool_call_before", {
            "turn_id": continuation.turn_id,
            "tool_name": execution_call.name,
        })
        try:
            tool_result = await self._call_port_async(
                self.context.execution_runtime,
                "execute_tool_async",
                "execute_tool",
                execution_call,
                allow_tools=not continuation.finalization_only,
                budget=tool_budget,
                turn_id=continuation.turn_id,
            )
        except Exception as exc:
            self._log_tool_call_exception(continuation, execution_call, exc)
            raise
        if self._should_enter_failure_flow_for_tool_result(tool_result):
            failure_result = await self._handle_failure_async(
                FailureSignal(
                    subsystem="execution",
                    component=execution_call.name,
                    failure_kind="capability_failure",
                    severity="medium",
                    primary_blocker=str(tool_result.text or f"{execution_call.name} failed"),
                    evidence={"tool_result": tool_result.structured or {}, "tool_name": execution_call.name},
                    related_ids={"turn_id": continuation.turn_id},
                    safe_to_retry=False,
                    repair_domain="execution:runtime",
                ),
                origin="tool_call",
                conversation_context={"turn_id": continuation.turn_id, "tool_name": execution_call.name},
            )
            tool_result = CanonicalToolResult(
                name=execution_call.name,
                ok=False,
                text=self._render_failure_feedback_text(failure_result.user_feedback),
                structured={
                    "failure_status": failure_result.verification.status,
                    "report_id": failure_result.report.report_id if failure_result.report is not None else None,
                },
                call_id=getattr(execution_call, "call_id", None),
                llm_text=self._render_failure_feedback_text(failure_result.user_feedback),
            )
        self._log_tool_call_result(continuation, execution_call, tool_result)
        continuation.tool_observations.append(
            ToolObservation(
                tool_name=tool_result.name,
                ok=tool_result.ok,
                summary=tool_result.text or ("tool succeeded" if tool_result.ok else "tool failed"),
                structured=tool_result.structured,
            )
        )
        await self._project_behavior_advice_result_async(execution_call.name, tool_result)
        record = ToolExecutionRecord(
            turn_id=continuation.turn_id,
            sequence=continuation.tool_batch_count,
            tool_signature_hash=canonical_tool_signature_hash(execution_call.name, execution_call.args),
            result_fingerprint=canonical_result_fingerprint(
                {"ok": tool_result.ok, "text": tool_result.text, "structured": tool_result.structured}
            ),
        )
        continuation.tool_batch_count += 1
        verdict = self.turn_manager.guard.observe_batch(continuation.turn_id, [record])
        if verdict.recommended_action == GuardAction.TERMINATE_TOOL_LOOP:
            continuation.finalization_only = True
        self.context.turn_event_bus.emit("turn.tool_call_after", {
            "turn_id": continuation.turn_id,
            "tool_name": execution_call.name,
            "ok": tool_result.ok,
        })
        if continuation.pending_tool_call_batch:
            continuation.pending_tool_results.append(tool_result)
            if len(continuation.pending_tool_results) >= len(continuation.pending_tool_call_batch):
                self._flush_tool_protocol_messages(continuation)
        return EffectResult(
            status=RuntimeStatus.OK if tool_result.ok else RuntimeStatus.ERROR,
            payload=tool_result,
            text=tool_result.text,
        )

    async def _project_behavior_advice_result_async(self, tool_name: str, tool_result: CanonicalToolResult) -> None:
        if not self._is_behavior_advise_tool_call(tool_name):
            return
        if not tool_result.ok:
            return
        entries = self._behavior_advice_l2_entries(tool_result)
        if not entries:
            return
        try:
            memory_service = self.context.require_port("memory:memory")
        except KeyError:
            return
        try:
            await self._call_port_async(
                memory_service,
                "aproject_l2_entries",
                "project_l2_entries",
                entries,
                touch=True,
                top_of_mind=True,
            )
        except Exception:
            diagnostics = getattr(self.state, "diagnostics", None)
            if isinstance(diagnostics, list):
                diagnostics.append(
                    {
                        "kind": "memory.behavior_advice_projection_failed",
                        "tool": tool_name,
                    }
                )

    def _log_tool_call_start(self, continuation, tool_call: Any) -> None:
        print(
            " ".join(
                [
                    "[tool call]",
                    f"turn_id={getattr(continuation, 'turn_id', '')}",
                    f"name={getattr(tool_call, 'name', '')}",
                    f"call_id={str(getattr(tool_call, 'call_id', '') or '')}",
                    f"args={self._log_preview(getattr(tool_call, 'args', {}), max_chars=1200)}",
                ]
            ),
            flush=True,
        )

    def _log_tool_call_result(self, continuation, tool_call: Any, tool_result: CanonicalToolResult) -> None:
        print(
            " ".join(
                [
                    "[tool result]",
                    f"turn_id={getattr(continuation, 'turn_id', '')}",
                    f"name={getattr(tool_call, 'name', '')}",
                    f"call_id={str(getattr(tool_call, 'call_id', '') or '')}",
                    f"ok={bool(getattr(tool_result, 'ok', False))}",
                    f"status={str(getattr(tool_result, 'status', '') or '')}",
                    f"text={self._log_preview(getattr(tool_result, 'text', '') or getattr(tool_result, 'llm_text', ''), max_chars=1200)}",
                ]
            ),
            flush=True,
        )

    def _log_tool_call_exception(self, continuation, tool_call: Any, exc: Exception) -> None:
        print(
            " ".join(
                [
                    "[tool result]",
                    f"turn_id={getattr(continuation, 'turn_id', '')}",
                    f"name={getattr(tool_call, 'name', '')}",
                    f"call_id={str(getattr(tool_call, 'call_id', '') or '')}",
                    "ok=False",
                    f"exception={type(exc).__name__}",
                    f"text={self._log_preview(str(exc), max_chars=1200)}",
                ]
            ),
            flush=True,
        )

    @staticmethod
    def _log_preview(value: Any, *, max_chars: int) -> str:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            text = str(value)
        text = " ".join(str(text).split())
        if len(text) <= max_chars:
            return text
        return f"{text[:max_chars].rstrip()}...[truncated {len(text)} chars]"

    @staticmethod
    def _behavior_advice_l2_entries(tool_result: CanonicalToolResult) -> list[L2Entry]:
        structured = tool_result.structured if isinstance(tool_result.structured, dict) else {}
        raw_candidates = structured.get("candidates") if isinstance(structured, dict) else None
        if not isinstance(raw_candidates, list):
            return []
        entries: list[L2Entry] = []
        for raw_candidate in raw_candidates:
            if not isinstance(raw_candidate, dict):
                continue
            candidate = dict(raw_candidate)
            affordance_id = str(candidate.get("affordance_id") or "").strip()
            if not affordance_id:
                continue
            entry_id = f"behavior_advice:{affordance_id}"
            title = str(candidate.get("title") or affordance_id).strip()
            summary = str(candidate.get("prompt_hint") or candidate.get("reason") or title).strip()
            rendered = TurnExecutor._render_behavior_guidance_entry(candidate)
            entries.append(
                L2Entry(
                    entry_id=entry_id,
                    kind="behavior_rule",
                    scope="behavior",
                    title=title,
                    summary=summary,
                    source_kind="behavior_advice",
                    source_ref=affordance_id,
                    candidate_state="active",
                    rendered=rendered,
                    search_text=TurnExecutor._behavior_candidate_search_text(candidate),
                    canonical_key=entry_id,
                    dedupe_fingerprint=entry_id,
                    payload=candidate,
                )
            )
        return entries

    @staticmethod
    def _is_behavior_advise_tool_call(name: str) -> bool:
        normalized = str(name or "").strip()
        return normalized == "op_behavior_advise" or normalized.endswith("_behavior_advise")

    @staticmethod
    def _render_behavior_guidance_entry(candidate: dict[str, Any]) -> str:
        hint = str(candidate.get("prompt_hint") or "").strip()
        skill_refs = TurnExecutor._string_list(candidate.get("skill_refs"))
        capability_refs = TurnExecutor._string_list(candidate.get("capability_refs"))
        memory_query_hints = TurnExecutor._string_list(candidate.get("memory_query_hints"))

        parts: list[str] = []
        if hint:
            parts.append(f"Hint: {hint}")
        if skill_refs:
            parts.append(f"Skill refs: {', '.join(skill_refs)}. MUST NOT call `op_skill_inject` solely because listed; call it only when workflow/domain rules are needed.")
        if capability_refs:
            parts.append(f"Capability refs: {', '.join(capability_refs)}. Resolve current inventory before use; if one directly completes the request, use it without injecting a skill.")
        if memory_query_hints:
            parts.append(f"Memory query hints: {', '.join(memory_query_hints)}. They do not trigger recall by themselves; when recall is required, use them as query seeds.")
        return " ".join(parts).strip()

    @staticmethod
    def _behavior_candidate_search_text(candidate: dict[str, Any]) -> str:
        parts = [
            str(candidate.get("title") or "").strip(),
            str(candidate.get("prompt_hint") or "").strip(),
            str(candidate.get("reason") or "").strip(),
            " ".join(TurnExecutor._string_list(candidate.get("skill_refs"))),
            " ".join(TurnExecutor._string_list(candidate.get("capability_refs"))),
            " ".join(TurnExecutor._string_list(candidate.get("memory_query_hints"))),
        ]
        return "\n".join(part for part in parts if part).strip()

    @staticmethod
    def _string_list(value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    @_dispatch_effect.register(MailboxReplyEffect)
    async def _handle_mailbox_reply(self, effect, continuation):
        if continuation.interrupted:
            return EffectResult(status=RuntimeStatus.SKIPPED, text="interrupted")
        channel_runtime = self.context.require_port("channel:channel")
        reply_id = channel_runtime.queue_reply(effect.channel_envelope, effect.text)
        text = str(effect.text or "").strip()
        if text:
            continuation.emitted_reply_texts.append(text)
        self._debug_log_reply(continuation, effect.text)
        return EffectResult(status=RuntimeStatus.QUEUED, payload={"reply_id": reply_id}, text=effect.text)

    @_dispatch_effect.register(MailboxReplyStreamEffect)
    async def _handle_mailbox_reply_stream(self, effect, continuation):
        if continuation.interrupted:
            return EffectResult(status=RuntimeStatus.SKIPPED, text="interrupted")
        channel_runtime = self.context.require_port("channel:channel")
        event_id = channel_runtime.queue_stream_event(effect.channel_envelope, effect.event)
        return EffectResult(status=RuntimeStatus.QUEUED, payload={"event_id": event_id})

    # ── stream request (table-driven accumulator) ───────────────────────

    async def stream_llm_request_async(self, continuation, llm_runtime, request: CanonicalLLMRequest) -> CanonicalLLMOutcome:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list = []
        state: dict[str, Any] = {"finish_reason": LLMFinishReason.STOP, "response_mode": None}

        events = await self._call_port_async(llm_runtime, "agenerate_stream", "generate_stream", request)
        for event in events:
            self._ensure_not_interrupted(continuation)
            if event.event_kind == LLMStreamEventKind.COMPACT_REQUIRED:
                return CanonicalLLMOutcome(
                    text="",
                    reasoning_text="",
                    tool_calls=[],
                    finish_reason=LLMFinishReason.COMPACT_REQUIRED,
                    target_input_budget=event.target_input_budget,
                    reserved_output_tokens=event.reserved_output_tokens,
                    preferred_endpoint_id=event.preferred_endpoint_id,
                    preferred_model_id=event.preferred_model_id,
                )
            if event.event_kind in _FORWARD_STREAM_KINDS:
                await self.execute_turn_effect_async(
                    continuation,
                    MailboxReplyStreamEffect(channel_envelope=continuation.channel_envelope, event=event),
                )
                self._ensure_not_interrupted(continuation)
            acc = self._stream_accumulators.get(event.event_kind)
            if acc is not None:
                acc(event, text_parts, reasoning_parts, tool_calls, state)

        return CanonicalLLMOutcome(
            text="".join(text_parts),
            reasoning_text="".join(reasoning_parts),
            tool_calls=tool_calls,
            finish_reason=state["finish_reason"],
            response_mode=state["response_mode"],
        )

    # ── stream accumulators ─────────────────────────────────────────────

    @staticmethod
    def _accumulate_text_delta(event, text_parts, _reasoning, _tools, _state):
        if event.text:
            text_parts.append(event.text)

    @staticmethod
    def _accumulate_reasoning_delta(event, _text, reasoning_parts, _tools, _state):
        if event.reasoning_text:
            reasoning_parts.append(event.reasoning_text)

    @staticmethod
    def _accumulate_tool_call(event, _text, _reasoning, tool_calls, _state):
        if event.tool_call is not None:
            tool_calls.append(event.tool_call)

    @staticmethod
    def _accumulate_error(event, text_parts, _reasoning, _tools, state):
        state["finish_reason"] = event.finish_reason or LLMFinishReason.ERROR
        if event.error_text:
            text_parts.append(event.error_text)

    @staticmethod
    def _accumulate_done(event, _text, _reasoning, _tools, state):
        state["finish_reason"] = event.finish_reason or state["finish_reason"]
        state["response_mode"] = event.response_mode or state["response_mode"]

    # ── prompt building ─────────────────────────────────────────────────

    def build_turn_prompt(
        self,
        continuation,
        assembly_context,
        *,
        max_output_tokens: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> CanonicalLLMRequest:
        from pal.shared import PromptAssemblyContext

        metadata = dict(assembly_context.metadata)
        if assembly_context.turn_kind == "failure":
            metadata["observation_blocks"] = [item.to_prompt_block() for item in continuation.tool_observations]
        if continuation.preferred_llm_endpoint_id:
            metadata["preferred_endpoint_id"] = continuation.preferred_llm_endpoint_id
        if continuation.preferred_llm_model_id:
            metadata["preferred_model_id"] = continuation.preferred_llm_model_id
        snapshot_think_level = str(continuation.turn_settings_snapshot.get("think_level") or "").strip()
        if snapshot_think_level:
            metadata["think_level"] = snapshot_think_level
        metadata["prompt_log_enabled"] = bool(continuation.turn_settings_snapshot.get("prompt_log_enabled"))
        metadata["artifact_scope_key"] = continuation.control_scope_key
        metadata["artifact_turn_id"] = continuation.turn_id
        metadata["llm_capabilities"] = self._resolve_llm_capabilities(continuation)
        if assembly_context.turn_kind != "failure":
            try:
                memory_service = self.context.require_port("memory:memory")
            except KeyError:
                memory_service = None
            if memory_service is not None and "memory_pack" not in metadata:
                try:
                    metadata["memory_pack"] = memory_service.build_pack(
                        MemoryPackRequest(
                            turn_kind=assembly_context.turn_kind,
                            task_id=assembly_context.task_id,
                            work_order_id=assembly_context.work_order_id,
                        )
                    )
                except Exception:
                    pass
        if continuation.finalization_only:
            metadata["finalization_directive"] = (
                continuation.finalization_reason
                or "Tool execution has been terminated. Use existing observations only and produce a pure text final reply."
            )
        prompt_context = PromptAssemblyContext(
            event=assembly_context.event,
            core_mode=assembly_context.core_mode,
            turn_kind=assembly_context.turn_kind,
            task_id=assembly_context.task_id,
            work_order_id=assembly_context.work_order_id,
            metadata=metadata,
        )
        prompt = self._build_canonical_prompt(
            prompt_context,
            max_output_tokens=max_output_tokens,
            model_hint=continuation.preferred_llm_model_id,
        )
        base_messages = list(prompt.messages)
        prepared_tool_protocol = self._prepare_tool_protocol_for_prompt(continuation.tool_protocol_messages)
        prompt_messages = self._merge_tool_protocol_into_prompt(
            base_messages,
            prepared_tool_protocol,
            primary_input=extract_text_from_payload(getattr(getattr(assembly_context, "event", None), "payload", None)).strip(),
        )
        metadata = dict(prompt.metadata)
        if snapshot_think_level:
            metadata["think_level"] = snapshot_think_level
        metadata["prompt_log_enabled"] = bool(continuation.turn_settings_snapshot.get("prompt_log_enabled"))
        metadata["artifact_scope_key"] = continuation.control_scope_key
        metadata["artifact_turn_id"] = continuation.turn_id
        metadata["llm_capabilities"] = self._resolve_llm_capabilities(continuation)
        metadata["prompt_budget_snapshot"] = self._build_prompt_budget_snapshot(
            assembly_context,
            base_messages=base_messages,
            prepared_tool_protocol=prepared_tool_protocol,
            tools=list(tools or []),
        )
        prompt = CanonicalLLMRequest(
            messages=prompt_messages,
            max_output_tokens=prompt.max_output_tokens,
            model_hint=prompt.model_hint,
            temperature=prompt.temperature,
            tools=list(prompt.tools),
            metadata=metadata,
        )
        return prompt

    def _merge_tool_protocol_into_prompt(
        self,
        base_messages: list[dict[str, Any]],
        prepared_tool_protocol: list[dict[str, Any]],
        *,
        primary_input: str,
    ) -> list[dict[str, Any]]:
        if not prepared_tool_protocol:
            return list(base_messages)
        split = self._split_final_user_context(base_messages, primary_input=primary_input)
        if split is None:
            return [*base_messages, *prepared_tool_protocol]
        prefix, current_user_message, trailing_context_message = split
        messages = [*prefix, current_user_message, *prepared_tool_protocol]
        if trailing_context_message is not None:
            messages.append(trailing_context_message)
        return messages

    def _split_final_user_context(
        self,
        messages: list[dict[str, Any]],
        *,
        primary_input: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any] | None] | None:
        if not messages or not primary_input:
            return None
        last = dict(messages[-1])
        if str(last.get("role") or "").strip() != "user":
            return None
        content = last.get("content")
        if not isinstance(content, list):
            return None
        parts = [dict(part) for part in content if isinstance(part, dict)]
        if not parts:
            return None
        final_part = parts[-1]
        if final_part.get("type") != "text" or str(final_part.get("text") or "") != primary_input:
            return None
        context_parts = parts[:-1]
        if any(str(part.get("type") or "") != "text" for part in context_parts):
            return None
        current_user_message = {**last, "content": primary_input}
        trailing_context_message = None
        if context_parts:
            trailing_context_message = {
                "role": "user",
                "content": self._coerce_text_parts_content(context_parts),
            }
        return list(messages[:-1]), current_user_message, trailing_context_message

    @staticmethod
    def _coerce_text_parts_content(parts: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
        if len(parts) == 1 and parts[0].get("type") == "text":
            return str(parts[0].get("text") or "")
        return parts

    def _resolve_llm_capabilities(self, continuation) -> dict[str, Any]:
        llm_runtime = self.context.port_registry.get("llm:llm")
        if llm_runtime is None:
            return {}
        resolver = getattr(llm_runtime, "resolve_endpoint_facts", None)
        if not callable(resolver):
            return {}
        try:
            facts = resolver(preferred_endpoint_id=continuation.preferred_llm_endpoint_id)
        except TypeError:
            facts = resolver()
        except Exception:
            return {}
        if not isinstance(facts, dict):
            return {}
        return {
            "endpoint_id": facts.get("endpoint_id"),
            "model_id": facts.get("model_id"),
            "supports_vision": bool(facts.get("supports_vision")),
            "input_modalities": list(facts.get("input_modalities") or []),
            "capabilities": dict(facts.get("capabilities") or {}),
        }

    @staticmethod
    def _ensure_not_interrupted(continuation) -> None:
        if getattr(continuation, "interrupted", False):
            raise asyncio.CancelledError(getattr(continuation, "interrupt_reason", "") or "interrupted")

    def _build_prompt_budget_snapshot(
        self,
        assembly_context,
        *,
        base_messages: list[dict[str, Any]],
        prepared_tool_protocol: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, int]:
        system_chars = sum(
            self._estimate_prompt_message_chars(message)
            for message in base_messages
            if str(message.get("role") or "").strip() == "system"
        )
        primary_input = ""
        if assembly_context.event is not None:
            primary_input = extract_text_from_payload(assembly_context.event.payload).strip()
        if not primary_input:
            for message in reversed(base_messages):
                if str(message.get("role") or "").strip() == "user":
                    primary_input = self._message_content_text(message.get("content")).strip()
                    if primary_input:
                        break
        current_user_chars = len(primary_input)
        base_non_system_chars = sum(
            self._estimate_prompt_message_chars(message)
            for message in base_messages
            if str(message.get("role") or "").strip() != "system"
        )
        tool_protocol_chars = sum(self._estimate_prompt_message_chars(message) for message in prepared_tool_protocol)
        tools_schema_chars = self._estimate_tools_schema_chars(tools)
        conversation_chars = max(base_non_system_chars - current_user_chars, 0)
        estimated_input_chars = system_chars + current_user_chars + conversation_chars + tool_protocol_chars + tools_schema_chars
        return {
            "system_chars": system_chars,
            "tool_protocol_chars": tool_protocol_chars,
            "tools_schema_chars": tools_schema_chars,
            "conversation_chars": conversation_chars,
            "current_user_chars": current_user_chars,
            "estimated_input_chars": estimated_input_chars,
            "hard_keep_chars": system_chars + current_user_chars + tool_protocol_chars + tools_schema_chars,
        }

    @staticmethod
    def _estimate_tools_schema_chars(tools: list[dict[str, Any]]) -> int:
        if not tools:
            return 0
        try:
            return len(json.dumps(tools, ensure_ascii=False, sort_keys=True))
        except TypeError:
            return len(str(tools))

    def _build_tool_call_budget(self, continuation, *, execution_call=None) -> ToolCallBudget:
        cfg = self._config
        token_limit = cfg.max_tool_result_tokens
        if execution_call is not None and str(getattr(execution_call, "name", "") or "").strip() == "shell_exec":
            token_limit = min(cfg.max_tool_result_tokens, cfg.default_max_output_tokens)
        max_output_chars = min(
            cfg.default_max_result_size_chars,
            int(token_limit * cfg.chars_per_token),
        )
        return ToolCallBudget(
            max_output_chars=max_output_chars,
            max_output_tokens_estimate=token_limit,
            max_output_bytes=cfg.max_output_size_bytes,
            max_result_spill_chars=cfg.default_max_result_size_chars,
            max_result_group_chars=cfg.max_tool_results_per_message_chars,
            preview_chars=cfg.active_tool_result_preview,
            artifact_bucket_id=continuation.turn_id,
            max_read_bytes=cfg.max_output_size_bytes,
            max_lines_to_read=cfg.max_lines_to_read,
            max_stdout_chars=max_output_chars,
            timeout_ms=None,
        )

    def _resolve_effective_max_output_tokens(self, continuation) -> int:
        llm_runtime = self.context.port_registry.get("llm:llm")
        if llm_runtime is not None:
            fn = getattr(llm_runtime, "resolve_max_output_tokens", None)
            if callable(fn):
                try:
                    result = fn(preferred_endpoint_id=continuation.preferred_llm_endpoint_id)
                except TypeError:
                    result = fn()
                if isinstance(result, int) and result > 0:
                    return result
        return self._config.fallback_max_output_tokens

    def _prepare_tool_protocol_for_prompt(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = [{**message} for message in messages]
        total = self._tool_result_chars(result)
        if total <= self._config.max_tool_results_per_message_chars:
            return result
        for batch_indices in self._tool_protocol_batches(result):
            if total <= self._config.max_tool_results_per_message_chars:
                break
            for idx in batch_indices:
                if str(result[idx].get("role") or "").strip() != "tool":
                    continue
                old = str(result[idx].get("content", ""))
                preview = self._render_tool_preview(old)
                if preview == old:
                    continue
                total -= len(old) - len(preview)
                result[idx] = {**result[idx], "content": preview}
        if total <= self._config.max_tool_results_per_message_chars:
            return result
        for batch_indices in self._tool_protocol_batches(result):
            if total <= self._config.max_tool_results_per_message_chars:
                break
            for idx in batch_indices:
                if str(result[idx].get("role") or "").strip() != "tool":
                    continue
                old = str(result[idx].get("content", ""))
                minimal = self._render_minimal_tool_observation(old)
                if minimal == old:
                    continue
                total -= len(old) - len(minimal)
                result[idx] = {**result[idx], "content": minimal}
        return result

    @staticmethod
    def _tool_result_chars(messages: list[dict[str, Any]]) -> int:
        return sum(
            TurnExecutor._message_content_chars(message.get("content"))
            for message in messages
            if str(message.get("role") or "").strip() == "tool"
        )

    @staticmethod
    def _estimate_prompt_message_chars(message: dict[str, Any]) -> int:
        total = TurnExecutor._message_content_chars(message.get("content"))
        tool_calls = message.get("tool_calls")
        if tool_calls:
            try:
                total += len(json.dumps(tool_calls, ensure_ascii=False, sort_keys=True))
            except TypeError:
                total += len(str(tool_calls))
        tool_call_id = str(message.get("tool_call_id", "") or "").strip()
        if tool_call_id:
            total += len(tool_call_id)
        return total

    @staticmethod
    def _message_content_chars(content: Any) -> int:
        return len(TurnExecutor._message_content_text(content))

    @staticmethod
    def _message_content_text(content: Any) -> str:
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if str(item.get("type") or "") == "text":
                    parts.append(str(item.get("text") or ""))
            return "\n".join(part for part in parts if part)
        return str(content or "")

    def _render_tool_preview(self, content: str) -> str:
        if len(content) <= self._config.active_tool_result_preview:
            return content
        preview = content[: self._config.active_tool_result_preview].rstrip()
        return f"{preview}\n\n[preview only: original={len(content)} chars]"

    @staticmethod
    def _render_minimal_tool_observation(content: str) -> str:
        lines = [line.strip() for line in str(content or "").splitlines() if line.strip()]
        summary = lines[0] if lines else "tool result summarized due to prompt budget pressure"
        summary = summary[:180].rstrip()
        artifact_line = next((line for line in lines if line.startswith("[artifact:")), "")
        parts = [summary or "tool result summarized due to prompt budget pressure"]
        if artifact_line:
            parts.append(artifact_line)
        return "\n".join(parts)

    def _tool_protocol_batches(self, messages: list[dict[str, Any]]) -> list[list[int]]:
        batches: list[list[int]] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            role = str(message.get("role") or "").strip()
            if role == "assistant" and message.get("tool_calls"):
                batch: list[int] = [index]
                cursor = index + 1
                while cursor < len(messages) and str(messages[cursor].get("role") or "").strip() == "tool":
                    batch.append(cursor)
                    cursor += 1
                batches.append(batch)
                index = cursor
                continue
            if role == "tool":
                batches.append([index])
            index += 1
        return batches

    @staticmethod
    def _is_hard_budget_overflow(advice) -> bool:
        breakdown = getattr(advice, "breakdown", {}) or {}
        return bool(breakdown.get("hard_overflow"))

    def fallback_final_reply(self, continuation) -> str:
        if continuation.tool_observations:
            latest = continuation.tool_observations[-1]
            return (
                "I stopped the tool loop to avoid getting stuck. "
                f"Latest observation from {latest.tool_name}: {latest.summary}"
            )
        return "I stopped the tool loop to avoid getting stuck and can only provide a text-only final reply."

    def _ensure_tool_call_identity(self, tool_call):
        call_id = str(getattr(tool_call, "call_id", "") or "").strip() or f"call_{uuid4().hex[:12]}"
        return type(tool_call)(name=tool_call.name, args=dict(tool_call.args), call_id=call_id)

    def _flush_tool_protocol_messages(self, continuation) -> None:
        if not continuation.pending_tool_call_batch:
            return
        assistant_message = {
            "role": "assistant",
            "content": str(continuation.pending_assistant_tool_text or ""),
            "tool_calls": [
                {
                    "id": str(tool_call.call_id or ""),
                    "type": "function",
                    "function": {
                        "name": tool_call.name,
                        "arguments": json.dumps(tool_call.args, ensure_ascii=False, sort_keys=True),
                    },
                }
                for tool_call in continuation.pending_tool_call_batch
            ],
        }
        continuation.tool_protocol_messages.append(assistant_message)
        result_by_call_id = {
            str(result.call_id or ""): result for result in continuation.pending_tool_results if str(result.call_id or "").strip()
        }
        for tool_call in continuation.pending_tool_call_batch:
            call_id = str(tool_call.call_id or "").strip()
            result = result_by_call_id.get(call_id)
            if result is None:
                continue
            continuation.tool_protocol_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": self._render_tool_result_content(tool_call, result),
                }
            )
        continuation.pending_assistant_tool_text = ""
        continuation.pending_tool_call_batch = []
        continuation.pending_tool_results = []

    def _render_tool_result_content(self, tool_call: CanonicalToolCall, result: CanonicalToolResult) -> str:
        if self._is_memory_recall_tool_call(tool_call.name):
            return self._render_memory_recall_tool_observation(tool_call, result)
        if str(result.llm_text or "").strip():
            return str(result.llm_text).strip()
        if str(result.text or "").strip():
            return str(result.text).strip()
        if result.structured:
            return json.dumps(result.structured, ensure_ascii=False, sort_keys=True)
        return "ok" if result.ok else "error"

    @staticmethod
    def _is_memory_recall_tool_call(name: str) -> bool:
        normalized = str(name or "").strip()
        return normalized == "op_memory_recall" or normalized.endswith("_memory_recall")

    def _render_memory_recall_tool_observation(self, tool_call: CanonicalToolCall, result: CanonicalToolResult) -> str:
        provider_id = str(tool_call.args.get("target_id") or "").strip() or "default"
        queries = [str(value).strip() for value in list(tool_call.args.get("queries") or []) if str(value).strip()]
        topic_scope = [str(value).strip() for value in list(tool_call.args.get("topic_scope") or []) if str(value).strip()]
        hit_count = 0
        if isinstance(result.structured, dict):
            raw_count = result.structured.get("hit_count")
            if isinstance(raw_count, int):
                hit_count = raw_count
            else:
                hit_count = len(list(result.structured.get("hits") or []))
        lines = [f"L3 recall {'completed' if result.ok else 'failed'}.", f"provider: {provider_id}"]
        if queries:
            lines.append(f"queries: {', '.join(queries)}")
        if topic_scope:
            lines.append(f"topics: {', '.join(topic_scope)}")
        if result.ok:
            lines.append(f"retrieved: {hit_count} memories")
        elif str(result.text or "").strip():
            lines.append(f"status: {str(result.text).strip()}")
        return "\n".join(lines)

    # ── response / temperature helpers ───────────────────────────────────

    def should_stream_reply(self, channel_envelope) -> bool:
        return channel_envelope.endpoint.channel_kind in {"stdio", "socket"}

    def infer_response_mode(self, outcome: CanonicalLLMOutcome | None, *, used_tools: bool) -> str:
        if outcome is not None:
            response_mode = str(outcome.response_mode or "").strip().lower()
            if response_mode in {
                LLMResponseMode.CHAT,
                LLMResponseMode.OPERATIONAL,
                LLMResponseMode.REVIEW,
            }:
                return response_mode
            if outcome.tool_calls:
                return LLMResponseMode.OPERATIONAL
            if outcome.text.strip():
                return LLMResponseMode.CHAT
        return LLMResponseMode.OPERATIONAL if used_tools else LLMResponseMode.CHAT

    def select_turn_temperature(self, response_mode: str) -> float:
        base_by_mode = {
            LLMResponseMode.CHAT: 0.7,
            LLMResponseMode.OPERATIONAL: 0.2,
            LLMResponseMode.REVIEW: 0.1,
        }
        value = base_by_mode.get(response_mode, 0.3)
        return max(0.0, min(1.0, round(value, 2)))

    # ── post-turn commit ─────────────────────────────────────────────────

    async def schedule_post_turn_commit_async(self, outcome) -> None:
        try:
            memory_service = self.context.require_port("memory:memory")
        except KeyError:
            return
        result = await self._call_port_async(
            memory_service,
            "acommit_l1",
            "commit_l1",
            MemoryCommitRequest(
                turn_id=outcome.commit_payload.turn_id,
                transcript=list(outcome.commit_payload.transcript),
                metadata={
                    "tool_observation_count": len(outcome.commit_payload.tool_observations),
                },
            )
        )
        if result.status != RuntimeStatus.OK:
            self.state.diagnostics.append(
                {
                    "kind": "memory.commit.retry",
                    "turn_id": outcome.commit_payload.turn_id,
                    "status": result.status,
                }
            )
        try:
            memory_service.l2_store.tick_heat()
        except Exception:
            pass

    # ── compaction summary ───────────────────────────────────────────────

    async def summarize_compaction_async(
        self,
        memory_service,
        *,
        target_input_budget: int,
        reserved_output_tokens: int,
        preferred_endpoint_id: str | None = None,
        preferred_model_id: str | None = None,
    ) -> str:
        llm_runtime = self.context.port_registry.get("llm:llm")
        if llm_runtime is None:
            return ""
        source_text = self.build_compaction_source_text(memory_service, target_input_budget=target_input_budget)
        if not source_text:
            return ""
        async_method = getattr(llm_runtime, "asummarize_compaction", None)
        if callable(async_method):
            result = async_method(
                source_text,
                max_output_tokens=min(max(512, reserved_output_tokens or 0), 1024),
                preferred_endpoint_id=preferred_endpoint_id,
                preferred_model_id=preferred_model_id,
            )
            if inspect.isawaitable(result):
                try:
                    return str((await result) or "").strip()
                except Exception:
                    return ""
            return str(result or "").strip()
        sync_method = getattr(llm_runtime, "summarize_compaction", None)
        if callable(sync_method):
            try:
                result = await asyncio.to_thread(
                    sync_method,
                    source_text,
                    max_output_tokens=min(max(512, reserved_output_tokens or 0), 1024),
                    preferred_endpoint_id=preferred_endpoint_id,
                    preferred_model_id=preferred_model_id,
                )
            except Exception:
                return ""
            return str(result or "").strip()
        return ""

    async def build_compaction_metadata_async(
        self,
        memory_service,
        *,
        target_input_budget: int,
        reserved_output_tokens: int,
        preferred_endpoint_id: str | None = None,
        preferred_model_id: str | None = None,
    ) -> dict[str, Any]:
        structured_compaction = await self.build_structured_compaction_async(
            memory_service,
            target_input_budget=target_input_budget,
            reserved_output_tokens=reserved_output_tokens,
            preferred_endpoint_id=preferred_endpoint_id,
            preferred_model_id=preferred_model_id,
        )
        if structured_compaction:
            return {"structured_compaction": structured_compaction}
        semantic_summary = await self.summarize_compaction_async(
            memory_service,
            target_input_budget=target_input_budget,
            reserved_output_tokens=reserved_output_tokens,
            preferred_endpoint_id=preferred_endpoint_id,
            preferred_model_id=preferred_model_id,
        )
        if semantic_summary:
            return {"semantic_summary": semantic_summary}
        return {}

    def build_compaction_source_text(self, memory_service, *, target_input_budget: int) -> str:
        builder = getattr(memory_service, "build_compaction_source_text", None)
        if not callable(builder):
            return ""
        try:
            return str(builder(target_input_budget=target_input_budget) or "").strip()
        except Exception:
            return ""

    async def build_structured_compaction_async(
        self,
        memory_service,
        *,
        target_input_budget: int,
        reserved_output_tokens: int,
        preferred_endpoint_id: str | None = None,
        preferred_model_id: str | None = None,
    ) -> dict[str, Any]:
        llm_runtime = self.context.port_registry.get("llm:llm")
        if llm_runtime is None:
            return {}
        source_text = self.build_compaction_source_text(memory_service, target_input_budget=target_input_budget)
        if not source_text:
            return {}
        async_method = getattr(llm_runtime, "acompact_memory_structured", None)
        if callable(async_method):
            result = async_method(
                source_text,
                max_output_tokens=min(max(1536, reserved_output_tokens or 0), 4096),
                preferred_endpoint_id=preferred_endpoint_id,
                preferred_model_id=preferred_model_id,
            )
            if inspect.isawaitable(result):
                try:
                    payload = await result
                except Exception:
                    return {}
            else:
                payload = result
            return dict(payload or {}) if isinstance(payload, dict) else {}
        return {}
