from __future__ import annotations

import asyncio
import inspect
import json
from uuid import uuid4
from typing import Any, Awaitable, Callable

from pal.llm.contracts import CanonicalLLMOutcome, CanonicalLLMRequest, CanonicalToolResult, LLMPreflightRequest
from pal.memory.contracts import MemoryCommitRequest, MemoryCompactRequest
from pal.shared import GuardAction, LLMFinishReason, LLMResponseMode, LLMStreamEventKind, RuntimeStatus


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
    ) -> None:
        self.context = context
        self.state = state
        self.turn_manager = turn_manager
        self._call_port_async = call_port_async
        self._build_canonical_prompt = build_canonical_prompt
        self._debug_log_prompt = debug_log_prompt
        self._debug_log_outcome = debug_log_outcome
        self._debug_log_reply = debug_log_reply
        self._build_llm_tool_contracts = build_llm_tool_contracts
        self._handle_failure_async = handle_failure_async
        self._render_failure_feedback_text = render_failure_feedback_text
        self._should_enter_failure_flow_for_tool_result = should_enter_failure_flow_for_tool_result

    def execute_turn_effect(self, continuation, effect):
        return asyncio.run(self.execute_turn_effect_async(continuation, effect))

    async def execute_turn_effect_async(self, continuation, effect):
        from pal.core.turns import (
            LLMPreflightEffect,
            LLMRequestEffect,
            MailboxReplyEffect,
            MailboxReplyStreamEffect,
            MemoryCompactEffect,
            ToolCallEffect,
        )
        from pal.core.tool_stagnation import (
            ToolExecutionRecord,
            canonical_result_fingerprint,
            canonical_tool_signature_hash,
        )
        from pal.failure import FailureSignal

        continuation.waiting_effect_id = effect.effect_id
        if isinstance(effect, LLMPreflightEffect):
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
            continuation.waiting_effect_id = None
            from pal.core.turns import EffectResult

            return EffectResult(status=RuntimeStatus.OK, payload=advice)
        if isinstance(effect, MemoryCompactEffect):
            memory_service = self.context.require_port("memory:memory")
            metadata = dict(effect.assembly_context.metadata)
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
            continuation.waiting_effect_id = None
            from pal.core.turns import EffectResult

            return EffectResult(status=RuntimeStatus.OK, payload=compact_result)
        if isinstance(effect, LLMRequestEffect):
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
            continuation.waiting_effect_id = None
            from pal.core.turns import EffectResult

            return EffectResult(status=RuntimeStatus.OK, payload=outcome)
        if isinstance(effect, ToolCallEffect):
            execution_call = effect.tool_call
            if not str(getattr(execution_call, "call_id", "") or "").strip() and continuation.pending_tool_call_batch:
                pending_index = len(continuation.pending_tool_results)
                if 0 <= pending_index < len(continuation.pending_tool_call_batch):
                    execution_call = continuation.pending_tool_call_batch[pending_index]
            tool_result = await self._call_port_async(
                self.context.execution_runtime,
                "execute_tool_async",
                "execute_tool",
                execution_call,
                allow_tools=not continuation.finalization_only,
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
                )
            from pal.core.turns import EffectResult
            from pal.core.turns import ToolObservation

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
            continuation.waiting_effect_id = None
            return EffectResult(
                status=RuntimeStatus.OK if tool_result.ok else RuntimeStatus.ERROR,
                payload=tool_result,
                text=tool_result.text,
            )
        if isinstance(effect, MailboxReplyEffect):
            self._debug_log_reply(effect.text)
            channel_runtime = self.context.require_port("channel:channel")
            reply_id = channel_runtime.queue_reply(effect.channel_envelope, effect.text)
            continuation.waiting_effect_id = None
            from pal.core.turns import EffectResult

            return EffectResult(status=RuntimeStatus.QUEUED, payload={"reply_id": reply_id}, text=effect.text)
        if isinstance(effect, MailboxReplyStreamEffect):
            channel_runtime = self.context.require_port("channel:channel")
            event_id = channel_runtime.queue_stream_event(effect.channel_envelope, effect.event)
            continuation.waiting_effect_id = None
            from pal.core.turns import EffectResult

            return EffectResult(status=RuntimeStatus.QUEUED, payload={"event_id": event_id})
        continuation.waiting_effect_id = None
        from pal.core.turns import EffectResult

        return EffectResult(status=RuntimeStatus.UNSUPPORTED, text=f"unknown effect: {effect.kind}")

    async def stream_llm_request_async(self, continuation, llm_runtime, request: CanonicalLLMRequest) -> CanonicalLLMOutcome:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list = []
        finish_reason = LLMFinishReason.STOP
        response_mode = None
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
            if event.event_kind in {
                LLMStreamEventKind.TEXT_DELTA,
                LLMStreamEventKind.REASONING_DELTA,
                LLMStreamEventKind.TOOL_CALL,
                LLMStreamEventKind.DONE,
                LLMStreamEventKind.ERROR,
            }:
                from pal.core.turns import MailboxReplyStreamEffect

                await self.execute_turn_effect_async(
                    continuation,
                    MailboxReplyStreamEffect(channel_envelope=continuation.channel_envelope, event=event),
                )
            if event.event_kind == LLMStreamEventKind.TEXT_DELTA and event.text:
                text_parts.append(event.text)
            elif event.event_kind == LLMStreamEventKind.REASONING_DELTA and event.reasoning_text:
                reasoning_parts.append(event.reasoning_text)
            elif event.event_kind == LLMStreamEventKind.TOOL_CALL and event.tool_call is not None:
                tool_calls.append(event.tool_call)
            elif event.event_kind == LLMStreamEventKind.ERROR:
                finish_reason = event.finish_reason or LLMFinishReason.ERROR
                if event.error_text:
                    text_parts.append(event.error_text)
            elif event.event_kind == LLMStreamEventKind.DONE:
                finish_reason = event.finish_reason or finish_reason
                response_mode = event.response_mode or response_mode
        return CanonicalLLMOutcome(
            text="".join(text_parts),
            reasoning_text="".join(reasoning_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            response_mode=response_mode,
        )

    def build_turn_prompt(self, continuation, assembly_context, *, max_output_tokens: int) -> CanonicalLLMRequest:
        from pal.shared import PromptAssemblyContext

        metadata = dict(assembly_context.metadata)
        if assembly_context.turn_kind == "failure":
            metadata["observation_blocks"] = [item.to_prompt_block() for item in continuation.tool_observations]
        if continuation.preferred_llm_endpoint_id:
            metadata["preferred_endpoint_id"] = continuation.preferred_llm_endpoint_id
        if continuation.preferred_llm_model_id:
            metadata["preferred_model_id"] = continuation.preferred_llm_model_id
        if continuation.finalization_only:
            metadata["finalization_directive"] = (
                "Tool execution has been terminated. Use existing observations only and produce a pure text final reply."
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
            prompt.messages.extend(dict(message) for message in continuation.tool_protocol_messages)
        return prompt

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
                    "content": self._render_tool_result_content(result),
                }
            )
        continuation.pending_assistant_tool_text = ""
        continuation.pending_tool_call_batch = []
        continuation.pending_tool_results = []

    def _render_tool_result_content(self, result: CanonicalToolResult) -> str:
        if str(result.text or "").strip():
            return str(result.text).strip()
        if result.structured:
            return json.dumps(result.structured, ensure_ascii=False, sort_keys=True)
        return "ok" if result.ok else "error"

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
        transcripts = list(getattr(memory_service.l1_store, "items", [])[-12:])
        rendered_turns: list[str] = []
        for transcript in transcripts:
            lines = []
            for message in transcript:
                role = str(getattr(message, "role", "") or "").strip()
                content = str(getattr(message, "content", "") or "").strip()
                if role and content:
                    lines.append(f"{role}: {content}")
            if lines:
                rendered_turns.append("\n".join(lines))
        raw = "\n\n".join(rendered_turns).strip()
        if not raw:
            return ""
        limit = max(256, target_input_budget or 0)
        return raw[:limit]
