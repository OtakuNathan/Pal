from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass
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
from pal.memory.contracts import MemoryCommitRequest, MemoryCompactRequest, MemoryPackRequest
from pal.shared import GuardAction, LLMFinishReason, LLMResponseMode, LLMStreamEventKind, RuntimeStatus
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
        debug_log_prompt: Callable[[CanonicalLLMRequest], None],
        debug_log_outcome: Callable[[CanonicalLLMOutcome], None],
        debug_log_reply: Callable[[str], None],
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
        continuation.waiting_effect_id = effect.effect_id
        result = await self._dispatch_effect(effect, continuation)
        continuation.waiting_effect_id = None
        return result

    # ── effect dispatch (singledispatch — C++ overload style) ────────────

    @singledispatchmethod
    async def _dispatch_effect(self, effect, continuation):
        return EffectResult(status=RuntimeStatus.UNSUPPORTED, text=f"unknown effect: {effect.kind}")

    @_dispatch_effect.register(LLMPreflightEffect)
    async def _handle_llm_preflight(self, effect, continuation):
        llm_runtime = self.context.require_port("llm:llm")
        prompt = self.build_turn_prompt(continuation, effect.assembly_context, max_output_tokens=effect.max_output_tokens)
        advice = await self._call_port_async(
            llm_runtime,
            "apreflight",
            "preflight",
            LLMPreflightRequest(
                messages=prompt.messages,
                max_output_tokens=prompt.max_output_tokens,
                model_hint=prompt.model_hint,
                metadata=dict(prompt.metadata),
            )
        )
        return EffectResult(status=RuntimeStatus.OK, payload=advice)

    @_dispatch_effect.register(MemoryCompactEffect)
    async def _handle_memory_compact(self, effect, continuation):
        memory_service = self.context.require_port("memory:memory")
        metadata = dict(effect.assembly_context.metadata)
        structured_compaction = await self.build_structured_compaction_async(
            memory_service,
            target_input_budget=effect.target_input_budget,
            reserved_output_tokens=effect.reserved_output_tokens,
            preferred_endpoint_id=metadata.get("preferred_endpoint_id"),
            preferred_model_id=metadata.get("preferred_model_id"),
        )
        if structured_compaction:
            metadata["structured_compaction"] = structured_compaction
        else:
            semantic_summary = await self.summarize_compaction_async(
                memory_service,
                target_input_budget=effect.target_input_budget,
                reserved_output_tokens=effect.reserved_output_tokens,
                preferred_endpoint_id=metadata.get("preferred_endpoint_id"),
                preferred_model_id=metadata.get("preferred_model_id"),
            )
            if semantic_summary:
                metadata["semantic_summary"] = semantic_summary
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
        llm_runtime = self.context.require_port("llm:llm")
        prompt = self.build_turn_prompt(continuation, effect.assembly_context, max_output_tokens=effect.max_output_tokens)
        if effect.tools_override is not None:
            tools = list(effect.tools_override)
        else:
            tools = [] if continuation.finalization_only else self._build_llm_tool_contracts()
        request = CanonicalLLMRequest(
            messages=list(prompt.messages),
            max_output_tokens=prompt.max_output_tokens,
            model_hint=prompt.model_hint,
            temperature=self.select_turn_temperature(continuation.last_response_mode),
            tools=tools,
            metadata=dict(prompt.metadata),
        )
        self._debug_log_prompt(request)
        if self.should_stream_reply(continuation.channel_envelope) and hasattr(llm_runtime, "generate_stream"):
            outcome = await self.stream_llm_request_async(continuation, llm_runtime, request)
        else:
            outcome = await self._call_port_async(llm_runtime, "agenerate", "generate", request)
        self._debug_log_outcome(outcome)
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

    @_dispatch_effect.register(ToolCallEffect)
    async def _handle_tool_call(self, effect, continuation):
        execution_call = effect.tool_call
        if not str(getattr(execution_call, "call_id", "") or "").strip() and continuation.pending_tool_call_batch:
            pending_index = len(continuation.pending_tool_results)
            if 0 <= pending_index < len(continuation.pending_tool_call_batch):
                execution_call = continuation.pending_tool_call_batch[pending_index]
        tool_budget = self._build_tool_call_budget(continuation)
        tool_result = await self._call_port_async(
            self.context.execution_runtime,
            "execute_tool_async",
            "execute_tool",
            execution_call,
            allow_tools=not continuation.finalization_only,
            budget=tool_budget,
        )
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
        if tool_result.status == "budget_exceeded":
            continuation.finalization_only = True
            continuation.finalization_reason = (
                "The tool execution budget for this turn was exhausted. Use the existing observations and retained tool protocol only."
            )
        continuation.tool_observations.append(
            ToolObservation(
                tool_name=tool_result.name,
                ok=tool_result.ok,
                summary=tool_result.text or ("tool succeeded" if tool_result.ok else "tool failed"),
                structured=tool_result.structured,
            )
        )
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
        if continuation.pending_tool_call_batch:
            continuation.pending_tool_results.append(tool_result)
            if len(continuation.pending_tool_results) >= len(continuation.pending_tool_call_batch):
                self._flush_tool_protocol_messages(continuation)
        return EffectResult(
            status=RuntimeStatus.OK if tool_result.ok else RuntimeStatus.ERROR,
            payload=tool_result,
            text=tool_result.text,
        )

    @_dispatch_effect.register(MailboxReplyEffect)
    async def _handle_mailbox_reply(self, effect, continuation):
        channel_runtime = self.context.require_port("channel:channel")
        reply_id = channel_runtime.queue_reply(effect.channel_envelope, effect.text)
        self._debug_log_reply(effect.text)
        return EffectResult(status=RuntimeStatus.QUEUED, payload={"reply_id": reply_id}, text=effect.text)

    @_dispatch_effect.register(MailboxReplyStreamEffect)
    async def _handle_mailbox_reply_stream(self, effect, continuation):
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
            acc = self._stream_accumulators.get(event.event_kind)
            if acc is not None:

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

    def build_turn_prompt(self, continuation, assembly_context, *, max_output_tokens: int) -> CanonicalLLMRequest:
        from pal.shared import PromptAssemblyContext

        metadata = dict(assembly_context.metadata)
        if assembly_context.turn_kind == "failure":
            metadata["observation_blocks"] = [item.to_prompt_block() for item in continuation.tool_observations]
        if continuation.preferred_llm_endpoint_id:
            metadata["preferred_endpoint_id"] = continuation.preferred_llm_endpoint_id
        if continuation.preferred_llm_model_id:
            metadata["preferred_model_id"] = continuation.preferred_llm_model_id
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
        if continuation.tool_protocol_messages:
            budget = self._resolve_tool_result_budget(continuation, max_output_tokens=max_output_tokens)
            prompt.messages.extend(
                self._trim_tool_protocol_for_prompt(
                    continuation.tool_protocol_messages,
                    aggregate_budget=budget,
                )
            )
        return prompt

    def _resolve_tool_result_budget(self, continuation, *, max_output_tokens: int) -> int:
        facts = self._resolve_endpoint_facts(
            continuation.preferred_llm_endpoint_id,
            preferred_model_id=continuation.preferred_llm_model_id,
        )
        return self._compute_tool_protocol_budget(facts, max_output_tokens=max_output_tokens)

    def _resolve_endpoint_facts(
        self,
        preferred_endpoint_id: str | None,
        *,
        preferred_model_id: str | None = None,
    ) -> dict[str, Any]:
        llm_runtime = self.context.port_registry.get("llm:llm")
        if llm_runtime is not None:
            facts_fn = getattr(llm_runtime, "resolve_endpoint_facts", None)
            if callable(facts_fn):
                try:
                    payload = facts_fn(preferred_endpoint_id=preferred_endpoint_id)
                except TypeError:
                    payload = facts_fn()
                if isinstance(payload, dict):
                    normalized = dict(payload)
                    if preferred_model_id and not normalized.get("model_id"):
                        normalized["model_id"] = preferred_model_id
                    return normalized
            max_output_fn = getattr(llm_runtime, "resolve_max_output_tokens", None)
            if callable(max_output_fn):
                try:
                    resolved_output = max_output_fn(preferred_endpoint_id=preferred_endpoint_id)
                except TypeError:
                    resolved_output = max_output_fn()
                return {
                    "endpoint_id": preferred_endpoint_id,
                    "model_id": preferred_model_id,
                    "context_window": None,
                    "max_output_tokens": resolved_output,
                }
        return {
            "endpoint_id": preferred_endpoint_id,
            "model_id": preferred_model_id,
            "context_window": None,
            "max_output_tokens": None,
        }

    def _compute_tool_protocol_budget(self, facts: dict[str, Any], *, max_output_tokens: int) -> int:
        cfg = self._config
        context_window = facts.get("context_window")
        endpoint_max_output = facts.get("max_output_tokens")
        if not isinstance(context_window, int) or context_window <= 0:
            return cfg.fallback_protocol_budget
        reserved_output = max_output_tokens
        if isinstance(endpoint_max_output, int) and endpoint_max_output > 0:
            reserved_output = min(max_output_tokens, endpoint_max_output)
        margin = min(cfg.context_margin_cap, max(cfg.context_margin_min, int(context_window * cfg.context_margin_factor)))
        input_tokens = max(context_window - reserved_output - margin, 0)
        budget = int(input_tokens * cfg.tool_protocol_share * cfg.chars_per_token)
        return max(cfg.min_tool_protocol_budget, budget)

    def _build_tool_call_budget(self, continuation) -> ToolCallBudget:
        cfg = self._config
        facts = self._resolve_endpoint_facts(
            continuation.preferred_llm_endpoint_id,
            preferred_model_id=continuation.preferred_llm_model_id,
        )
        max_output_tokens = self._resolve_effective_max_output_tokens(continuation)
        aggregate_budget = self._compute_tool_protocol_budget(facts, max_output_tokens=max_output_tokens)
        consumed_budget = self._current_tool_protocol_budget(continuation.tool_protocol_messages)
        remaining_budget = max(aggregate_budget - consumed_budget, 0)
        max_output_chars = min(cfg.active_tool_result_budget, remaining_budget)
        if max_output_chars <= 0:
            max_output_chars = 0
        else:
            max_output_chars = max(cfg.min_tool_call_output_budget, max_output_chars)
        return ToolCallBudget(max_output_chars=max_output_chars)

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

    def _current_tool_protocol_budget(self, messages: list[dict[str, Any]]) -> int:
        total = 0
        for message in messages:
            if message.get("role") == "tool":
                total += len(str(message.get("content", "")))
        return total

    def _trim_tool_protocol_for_prompt(self, messages: list[dict], *, aggregate_budget: int | None = None) -> list[dict]:
        if aggregate_budget is None:
            aggregate_budget = self._config.fallback_protocol_budget
        result = []
        for msg in messages:
            if msg.get("role") == "tool":
                content = str(msg.get("content", ""))
                if len(content) > self._config.active_tool_result_budget:
                    preview = content[:self._config.active_tool_result_preview].rstrip()
                    content = f"{preview}\n\n[... truncated, original: {len(content)} chars]"
                    result.append({**msg, "content": content})
                    continue
            result.append(msg)

        total = sum(len(str(m.get("content", ""))) for m in result if m.get("role") == "tool")
        if total <= aggregate_budget:
            return result

        for batch_indices in self._tool_protocol_batches(result):
            if total <= aggregate_budget:
                break
            for idx in batch_indices:
                if result[idx].get("role") != "tool":
                    continue
                old = str(result[idx].get("content", ""))
                cleared = "[old tool result cleared]"
                total -= len(old) - len(cleared)
                result[idx] = {**result[idx], "content": cleared}

        return result

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
        if self._is_l3_recall_tool_call(tool_call.name):
            return self._render_l3_recall_tool_observation(tool_call, result)
        if str(result.llm_text or "").strip():
            return str(result.llm_text).strip()
        if str(result.text or "").strip():
            return str(result.text).strip()
        if result.structured:
            return json.dumps(result.structured, ensure_ascii=False, sort_keys=True)
        return "ok" if result.ok else "error"

    @staticmethod
    def _is_l3_recall_tool_call(name: str) -> bool:
        normalized = str(name or "").strip()
        return normalized == "op_l3_recall_query" or normalized.endswith("_l3_recall_query")

    def _render_l3_recall_tool_observation(self, tool_call: CanonicalToolCall, result: CanonicalToolResult) -> str:
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
                max_output_tokens=min(max(128, reserved_output_tokens or 0), 256),
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
                    max_output_tokens=min(max(128, reserved_output_tokens or 0), 256),
                    preferred_endpoint_id=preferred_endpoint_id,
                    preferred_model_id=preferred_model_id,
                )
            except Exception:
                return ""
            return str(result or "").strip()
        return ""

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
                max_output_tokens=min(max(192, reserved_output_tokens or 0), 384),
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
