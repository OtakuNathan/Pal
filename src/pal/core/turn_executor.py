from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, ToolResultIR
from pal.shared.json_values import thaw_json

import asyncio
import inspect
import json
import logging
from dataclasses import dataclass, replace
from functools import singledispatchmethod
from typing import Any, Awaitable, Callable

from pal.execution.contracts import ToolCallBudget
from pal.core.compaction import (
    CompactionClockKind,
    CompactionEngine,
    CompactionRunResult,
    CompactionSnapshot,
)
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
    MailboxReplyStreamUpdateEffect,
    MemoryCompactEffect,
    ToolCallEffect,
    ToolObservation,
)
from pal.failure import FailureSignal
from pal.llm.contracts import LLMGenerationResult, LLMPreflightRequest
from pal.llm.conversions import tool_definition_ir_from_dict
from pal.llm.ir import (
    ArtifactRefPartIR,
    ImagePartIR,
    LLMFinishReason,
    LLMMessageIR,
    LLMRequestIR,
    LLMResponseDeltaKind,
    LLMResponseIR,
    MessageRole,
    MessageState,
    PromptRegionIR,
    TextPartIR,
)
from pal.memory.compact import memory_candidates_from_compact_result
from pal.memory.contracts import (
    L1MessageKind,
    L1TranscriptMessage,
    MemoryCommitRequest,
    MemoryPackRequest,
)
from pal.shared import (
    GuardAction,
    LLMPreflightStatus,
    LLMResponseMode,
    ChannelStreamUpdateKind,
    RuntimeStatus,
    ToolExecutionResult,
    default_tool_result_text,
)
from pal.shared.payloads import extract_text_from_payload
from pal.shared.agent_io import ChannelMessage, ChannelStreamUpdate

LOGGER = logging.getLogger(__name__)


def _format_elapsed_seconds(value: float) -> str:
    seconds = max(0, int(round(float(value))))
    minutes, seconds = divmod(seconds, 60)
    if minutes and seconds:
        return f"{minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def _artifact_unavailable_summary(refs: list[ArtifactRefPartIR]) -> str:
    lines = ["Attached artifact content is currently unavailable; stable references follow:"]
    for ref in refs:
        lines.append(
            f"- artifact_id: {ref.artifact_id}; file_name: {ref.file_name or '<unknown>'}; "
            f"kind: {ref.kind or '<unknown>'}; status: {ref.status or 'unavailable'}; "
            f"summary: {ref.summary or '<none>'}"
        )
    return "\n".join(lines)

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
        handle_llm_provider_errors: bool = True,
        execute_tool_async: Callable[..., Awaitable[Any]] | None = None,
        config: RuntimeConfig | None = None,
        compaction_engine: CompactionEngine | None = None,
        compaction_clock_provider: Callable[[], int] | None = None,
        after_tool_batch: Callable[[Any], Awaitable[None]] | None = None,
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
        self._handle_llm_provider_errors = handle_llm_provider_errors
        self._execute_tool_async = execute_tool_async
        self._compaction_engine = compaction_engine
        self._compaction_clock_provider = (
            compaction_clock_provider or (lambda: 0)
        )
        self._after_tool_batch = after_tool_batch

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
        await self._ensure_l1_turn_async(
            continuation,
            effect.assembly_context,
        )
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
            LLMPreflightRequest(request=prompt)
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
        settled = await self._ensure_l1_turn_async(
            continuation,
            effect.assembly_context,
        )
        if not settled:
            return EffectResult(
                status=RuntimeStatus.ERROR,
                text=(
                    "Memory compaction requires a fully committed L1 working "
                    "set; the current input or closed tool protocol could not "
                    "be committed."
                ),
            )
        memory_service = self.context.require_port("memory:memory")
        run_result = await self.compact_memory_async(
            memory_service,
            target_input_budget=effect.target_input_budget,
            reserved_output_tokens=effect.reserved_output_tokens,
            assembly_context=effect.assembly_context,
            continuation=continuation,
        )
        if not run_result.success:
            return EffectResult(
                status=RuntimeStatus.ERROR,
                text="Memory compaction failed; memory and the active tool RPC were left unchanged.",
                payload=run_result,
            )
        compact_result = run_result.memory_result
        accepts_candidates = bool(
            getattr(
                getattr(self._compaction_engine, "policy", None),
                "accepts_memory_candidates",
                False,
            )
        )
        candidates = (
            memory_candidates_from_compact_result(compact_result)
            if accepts_candidates
            else []
        )
        if candidates:
            continuation.pending_compact_memory_candidate_batches.append(
                {
                    "source_kind": "pal_compact",
                    "source_label": "Pal compact",
                    "memory_candidates": candidates,
                }
            )
        return EffectResult(status=RuntimeStatus.OK, payload=compact_result)

    @_dispatch_effect.register(LLMRequestEffect)
    async def _handle_llm_request(self, effect, continuation):
        if continuation.budget_failure_feedback_text:
            text = continuation.budget_failure_feedback_text
            continuation.budget_failure_feedback_text = ""
            return EffectResult(
                status=RuntimeStatus.OK,
                payload=self._generation_result_from_text(
                    text,
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
        request = replace(
            prompt,
            policy=replace(
                prompt.policy,
                temperature=(
                prompt.policy.temperature
                if prompt.policy.temperature is not None
                else self.select_turn_temperature(continuation.last_response_mode)
                ),
            ),
            tools=tuple(tool_definition_ir_from_dict(tool) for tool in tools),
            metadata=dict(prompt.metadata),
        )
        self._debug_log_prompt(continuation, request)
        if self._llm_runtime_supports_streaming(llm_runtime, request):
            outcome = await self.stream_llm_request_async(continuation, llm_runtime, request)
        else:
            outcome = await self._call_port_async(llm_runtime, "agenerate", "generate", request)
            # An endpoint error is transport/recovery state, not an assistant
            # message.  Persisting it into L1 makes a later retry replay a
            # synthetic assistant turn and, for strict providers such as
            # Anthropic thinking mode, can produce an ill-formed protocol.
            if outcome.response.finish_reason != LLMFinishReason.ERROR:
                await self._upsert_l1_assistant_async(continuation, outcome.response.message)
        self._debug_log_outcome(continuation, outcome)
        preferred_endpoint_id = str(getattr(outcome, "preferred_endpoint_id", "") or "").strip() or None
        preferred_model_id = str(getattr(outcome, "preferred_model_id", "") or "").strip() or None
        if preferred_endpoint_id is None:
            preferred_endpoint_id = str(getattr(llm_runtime, "last_endpoint_id", "") or "").strip() or None
        if preferred_model_id is None:
            preferred_model_id = str(getattr(llm_runtime, "last_model_id", "") or "").strip() or None
        continuation.preferred_llm_endpoint_id = preferred_endpoint_id
        continuation.preferred_llm_model_id = preferred_model_id
        if continuation.finalization_only:
            if outcome.finish_reason != LLMFinishReason.COMPACT_REQUIRED:
                continuation.finalization_attempted = True
            if outcome.finish_reason != LLMFinishReason.COMPACT_REQUIRED and (outcome.tool_calls or not outcome.text.strip()):
                rejected_message_id = outcome.response.message.message_id
                await self._discard_l1_assistant_async(
                    continuation,
                    rejected_message_id,
                )
                outcome = self._generation_result_from_text(
                    self.fallback_final_reply(continuation),
                    finish_reason=LLMFinishReason.FALLBACK,
                    response_mode=LLMResponseMode.CHAT,
                )
                await self._upsert_l1_assistant_async(
                    continuation,
                    outcome.response.message,
                )
        continuation.last_response_mode = self.infer_response_mode(
            outcome,
            used_tools=bool(continuation.tool_observations),
        )
        if outcome.tool_calls:
            continuation.pending_assistant_tool_text = str(outcome.text or "")
            continuation.pending_tool_call_batch = [
                tool_call for tool_call in outcome.tool_calls
            ]
            continuation.pending_tool_results = []
        if (
            self._handle_llm_provider_errors
            and effect.assembly_context.turn_kind != "failure"
            and outcome.finish_reason == LLMFinishReason.ERROR
        ):
            failure_metadata = dict(outcome.response.message.metadata)
            failure_subsystem = str(
                failure_metadata.get("failure_subsystem") or "llm"
            )
            local_state_failure = failure_subsystem == "persistence"
            failure_component = (
                "runtime_database"
                if local_state_failure
                else continuation.preferred_llm_endpoint_id
                or str(getattr(llm_runtime, "last_endpoint_id", "") or "")
                or "llm_runtime"
            )
            failure_kind = str(
                failure_metadata.get("failure_kind")
                or ("local_state" if local_state_failure else "provider_failure")
            )
            error_type = str(failure_metadata.get("error_type") or "UnknownError")
            try:
                partial_chars = max(
                    0,
                    int(failure_metadata.get("partial_output_chars") or 0),
                )
            except (TypeError, ValueError):
                partial_chars = 0
            blocker_text = (
                f"LLM generation failed on {failure_component}: "
                f"{error_type} ({failure_kind})"
                + (
                    f"; stream interrupted after {partial_chars} chars of partial output."
                    if partial_chars
                    else "."
                )
            )
            failure_result = await self._handle_failure_async(
                FailureSignal(
                    subsystem=failure_subsystem,
                    component=failure_component,
                    failure_kind=failure_kind,
                    severity="high",
                    primary_blocker=blocker_text,
                    evidence={
                        "llm_text": outcome.text,
                        "preferred_model_id": continuation.preferred_llm_model_id,
                        **failure_metadata,
                    },
                    related_ids={"turn_id": continuation.turn_id},
                    safe_to_retry=False,
                    repair_domain=(
                        "core:persistence" if local_state_failure else "llm:core"
                    ),
                ),
                origin="llm_request",
                conversation_context={"turn_id": continuation.turn_id},
            )
            outcome = self._generation_result_from_text(
                self._render_failure_feedback_text(failure_result.user_feedback),
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
            if self._execute_tool_async is not None:
                tool_result = await self._execute_tool_async(
                    execution_call,
                    allow_tools=not continuation.finalization_only,
                    budget=tool_budget,
                    turn_id=continuation.turn_id,
                )
            else:
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
            failure = (
                f"Tool {execution_call.name} timed out before returning a result."
                if isinstance(exc, TimeoutError)
                else (
                    f"Tool {execution_call.name} did not complete: "
                    f"{exc.__class__.__name__}: {exc}"
                )
            )
            guidance = (
                "Its side effects may be incomplete or unknown. Inspect the current state, "
                "then retry the operation if appropriate."
            )
            tool_result = ToolExecutionResult(
                name=execution_call.name,
                ok=False,
                text=f"{failure}\n{guidance}",
                llm_text=f"{failure}\n{guidance}",
                structured={
                    "error_code": (
                        "tool_timeout"
                        if isinstance(exc, TimeoutError)
                        else "tool_rpc_failed"
                    ),
                    "error_type": exc.__class__.__name__,
                    "effect": "unknown",
                    "retry": "reconcile_first",
                },
                call_id=getattr(execution_call, "call_id", None),
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
                origin="op_tool_call",
                conversation_context={"turn_id": continuation.turn_id, "tool_name": execution_call.name},
            )
            tool_result = ToolExecutionResult(
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
        await self._maybe_echo_tool_result_async(continuation, execution_call, tool_result)
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
        self.context.turn_event_bus.emit("turn.tool_call_after", {
            "turn_id": continuation.turn_id,
            "tool_name": execution_call.name,
            "ok": tool_result.ok,
        })
        if continuation.pending_tool_call_batch:
            continuation.pending_tool_results.append(tool_result)
            await self._append_l1_tool_result_async(
                continuation,
                execution_call,
                tool_result,
            )
            if len(continuation.pending_tool_results) >= len(continuation.pending_tool_call_batch):
                continuation.pending_assistant_tool_text = ""
                continuation.pending_tool_call_batch = []
                continuation.pending_tool_results = []
                if self._after_tool_batch is not None:
                    await self._after_tool_batch(continuation)
        return EffectResult(
            status=RuntimeStatus.OK if tool_result.ok else RuntimeStatus.ERROR,
            payload=tool_result,
            text=tool_result.text,
        )

    _ECHO_MARKDOWN_MAX_CHARS = 4000

    async def _maybe_echo_tool_result_async(self, continuation: Any, tool_call: Any, tool_result: Any) -> None:
        """Fan out a tool-declared echo to the user's channel through core.

        A tool declares that its side effect should be visible to the user by
        returning structured ``{"echo": {"markdown": ..., "dedupe_key": ...}}``.
        Core is the only actor that touches the output port; the tool itself
        never knows the envelope or the channel. Only channel turns carry an
        envelope, so service/bunshin turns silently ignore echo declarations —
        there is physically no path to send "to the LLM itself".
        """
        structured = dict(tool_result.structured or {})
        echo = structured.get("echo")
        if not isinstance(echo, dict):
            return
        markdown = str(echo.get("markdown") or "").strip()
        if not markdown or len(markdown) > self._ECHO_MARKDOWN_MAX_CHARS:
            return
        tag = str(echo.get("tag") or "").strip() or None
        raw_payload = echo.get("payload")
        payload = dict(raw_payload) if isinstance(raw_payload, dict) else {}
        message = ChannelMessage(text=markdown, tag=tag, payload=payload)
        dedupe_key = (
            str(echo.get("dedupe_key") or "").strip()
            or f"{getattr(tool_call, 'name', '')}:{getattr(tool_call, 'call_id', None) or ''}"
        )
        if not dedupe_key:
            return
        if dedupe_key in continuation.echoed_keys:
            return
        if getattr(continuation, "delivery_binding", None) is None:
            return
        continuation.echoed_keys.add(dedupe_key)
        if continuation.channel_stream_active:
            await self.execute_turn_effect_async(
                continuation,
                MailboxReplyStreamUpdateEffect(
                    update=ChannelStreamUpdate(
                        kind=(
                            ChannelStreamUpdateKind.MESSAGE
                            if message.tag
                            else ChannelStreamUpdateKind.PROGRESS
                        ),
                        text=message.text,
                        message=message if message.tag else None,
                    ),
                ),
            )
            continuation.emitted_reply_texts.append(markdown)
        else:
            await self.execute_turn_effect_async(
                continuation,
                MailboxReplyEffect(text=message.text, message=message, terminal=False),
            )

    def _log_tool_call_start(self, continuation, tool_call: Any) -> None:
        LOGGER.debug(
            "[tool call] turn_id=%s name=%s call_id=%s args=%s",
            getattr(continuation, "turn_id", ""),
            getattr(tool_call, "name", ""),
            str(getattr(tool_call, "call_id", "") or ""),
            self._log_preview(getattr(tool_call, "args", {}), max_chars=1200),
        )

    def _log_tool_call_result(self, continuation, tool_call: Any, tool_result: ToolExecutionResult) -> None:
        LOGGER.debug(
            "[tool result] turn_id=%s name=%s call_id=%s ok=%s status=%s text=%s",
            getattr(continuation, "turn_id", ""),
            getattr(tool_call, "name", ""),
            str(getattr(tool_call, "call_id", "") or ""),
            bool(getattr(tool_result, "ok", False)),
            str(getattr(tool_result, "status", "") or ""),
            self._log_preview(getattr(tool_result, "text", "") or getattr(tool_result, "llm_text", ""), max_chars=1200),
        )

    def _log_tool_call_exception(self, continuation, tool_call: Any, exc: Exception) -> None:
        LOGGER.debug(
            "[tool result] turn_id=%s name=%s call_id=%s ok=False exception=%s text=%s",
            getattr(continuation, "turn_id", ""),
            getattr(tool_call, "name", ""),
            str(getattr(tool_call, "call_id", "") or ""),
            type(exc).__name__,
            self._log_preview(str(exc), max_chars=1200),
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

    @_dispatch_effect.register(MailboxReplyEffect)
    async def _handle_mailbox_reply(self, effect, continuation):
        if continuation.interrupted:
            return EffectResult(status=RuntimeStatus.SKIPPED, text="interrupted")
        output_port = self._agent_output_port()
        if output_port is None:
            return EffectResult(status=RuntimeStatus.SKIPPED, text=effect.text)
        binding = continuation.delivery_binding
        if binding is None:
            return EffectResult(status=RuntimeStatus.SKIPPED, text=effect.text)
        message = effect.message or ChannelMessage(text=effect.text)
        if continuation.channel_stream_active and effect.stream_companion:
            return EffectResult(status=RuntimeStatus.QUEUED, text=effect.text)
        if continuation.channel_stream_active and effect.terminal:
            text = str(effect.text or "").strip()
            if not (
                continuation.channel_stream_terminal_finish_reason
                and continuation.channel_stream_terminal_text == text
            ):
                await self._emit_synthetic_stream_terminal(
                    continuation,
                    text=text,
                    finish_reason=LLMFinishReason.FALLBACK.value,
                )
            if text:
                continuation.emitted_reply_texts.append(text)
            self._debug_log_reply(continuation, effect.text)
            return EffectResult(status=RuntimeStatus.QUEUED, text=effect.text)
        if not effect.terminal:
            reply_target = dict(binding.response_handle.reply_target)
            reply_target["_pal_turn_continues"] = True
            if effect.stream_companion:
                reply_target["_pal_stream_companion"] = True
            binding = replace(
                binding,
                response_handle=replace(
                    binding.response_handle,
                    reply_target=reply_target,
                ),
            )
        reply_id = await self._call_output_port_async(output_port, "queue_reply", binding, message)
        text = str(message.text or "").strip()
        if text:
            continuation.emitted_reply_texts.append(text)
        self._debug_log_reply(continuation, effect.text)
        return EffectResult(status=RuntimeStatus.QUEUED, payload={"reply_id": reply_id}, text=effect.text)

    @_dispatch_effect.register(MailboxReplyStreamUpdateEffect)
    async def _handle_mailbox_reply_stream(self, effect, continuation):
        if continuation.interrupted:
            return EffectResult(status=RuntimeStatus.SKIPPED, text="interrupted")
        output_port = self._agent_output_port()
        if output_port is None:
            return EffectResult(status=RuntimeStatus.SKIPPED)
        binding = continuation.delivery_binding
        if binding is None:
            return EffectResult(status=RuntimeStatus.SKIPPED)
        update_id = await self._call_output_port_async(
            output_port,
            "queue_stream_update",
            binding,
            effect.update,
        )
        return EffectResult(status=RuntimeStatus.QUEUED, payload={"update_id": update_id})

    def _agent_output_port(self):
        return self.context.port_registry.get("agent_io:output") or self.context.port_registry.get("channel:channel")

    def _channel_supports_stream_delivery(self, continuation: Any) -> bool:
        output_port = self._agent_output_port()
        binding = continuation.delivery_binding
        supports = getattr(output_port, "supports_stream_delivery", None)
        if binding is None or not callable(supports):
            return False
        try:
            return bool(supports(binding))
        except Exception:
            return False

    async def _call_output_port_async(self, output_port, method_name: str, *args, **kwargs):
        method = getattr(output_port, method_name)
        result = method(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    # ── stream request ──────────────────────────────────────────────────

    async def stream_llm_request_async(
        self,
        continuation: Any,
        llm_runtime: Any,
        request: LLMRequestIR,
    ) -> LLMGenerationResult:
        final_response: LLMResponseIR | None = None
        stream = getattr(llm_runtime, "astream", None)
        if not callable(stream):
            raise TypeError("LLM runtime does not implement the astream contract")
        continuation.channel_stream_active = self._channel_supports_stream_delivery(
            continuation
        )
        iterator = stream(request).__aiter__()
        schedule = self._llm_wait_status_schedule()
        schedule_index = 0
        started_at = asyncio.get_running_loop().time()
        semantic_seen = False
        pending: asyncio.Task[Any] | None = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.create_task(anext(iterator))
                if not semantic_seen and schedule_index < len(schedule):
                    elapsed = asyncio.get_running_loop().time() - started_at
                    wait_seconds = max(0.0, schedule[schedule_index] - elapsed)
                    done, _ = await asyncio.wait({pending}, timeout=wait_seconds)
                    if pending not in done:
                        await self._queue_llm_waiting_status(
                            continuation,
                            elapsed_seconds=schedule[schedule_index],
                        )
                        schedule_index += 1
                        continue
                try:
                    update = await pending
                except StopAsyncIteration:
                    break
                finally:
                    if pending.done():
                        pending = None
                final_response = update.response
                semantic_seen = semantic_seen or (
                    update.delta_kind != LLMResponseDeltaKind.STATE
                    and bool(update.response.message.parts)
                )
                await self._handle_ir_stream_update(continuation, update)
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                try:
                    await pending
                except asyncio.CancelledError:
                    pass
            close = getattr(iterator, "aclose", None)
            if callable(close):
                try:
                    await close()
                except (asyncio.CancelledError, RuntimeError):
                    pass
        if final_response is None:
            return self._generation_result_from_text(
                "LLM stream completed without a response.",
                finish_reason=LLMFinishReason.ERROR,
            )
        return LLMGenerationResult(
            response=final_response,
            preferred_endpoint_id=str(getattr(llm_runtime, "last_endpoint_id", "") or "") or None,
            preferred_model_id=str(getattr(llm_runtime, "last_model_id", "") or "") or None,
        )

    def _llm_wait_status_schedule(self) -> tuple[float, ...]:
        raw = getattr(
            self._config,
            "llm_wait_status_seconds",
            (120.0, 300.0, 600.0, 1_200.0),
        )
        schedule: list[float] = []
        for item in tuple(raw or ()):
            try:
                seconds = float(item)
            except (TypeError, ValueError):
                continue
            if seconds > 0:
                schedule.append(seconds)
        return tuple(sorted(set(schedule)))

    async def _queue_llm_waiting_status(
        self,
        continuation: Any,
        *,
        elapsed_seconds: float,
    ) -> None:
        output_port = self._agent_output_port()
        binding = continuation.delivery_binding
        if output_port is None or binding is None or continuation.interrupted:
            return
        elapsed = _format_elapsed_seconds(elapsed_seconds)
        await self._call_output_port_async(
            output_port,
            "queue_status",
            binding,
            "llm_waiting",
            payload={
                "elapsed_seconds": float(elapsed_seconds),
                "text": (
                    f"LLM is still processing · {elapsed} elapsed. "
                    "Use /interrupt to stop waiting."
                ),
            },
        )

    async def _handle_ir_stream_update(self, continuation: Any, update: Any) -> None:
        self._ensure_not_interrupted(continuation)
        # A terminal ERROR is transport state, not an assistant message.  Any
        # partial text or item-level tool snapshot from this failed response
        # must leave L1 together so the retry resends the same logical input.
        terminal_error = (
            update.delta_kind == LLMResponseDeltaKind.STATE
            and update.response.finish_reason == LLMFinishReason.ERROR
        )
        if terminal_error:
            await self._discard_l1_assistant_async(
                continuation,
                update.response.message.message_id,
            )
        else:
            await self._upsert_l1_assistant_async(continuation, update.response.message)
        channel_update: ChannelStreamUpdate | None = None
        if update.delta_kind == LLMResponseDeltaKind.TEXT and update.text_delta:
            channel_update = ChannelStreamUpdate(
                kind=ChannelStreamUpdateKind.TEXT_DELTA,
                text=update.text_delta,
            )
        elif update.delta_kind == LLMResponseDeltaKind.REASONING and update.text_delta:
            channel_update = ChannelStreamUpdate(
                kind=ChannelStreamUpdateKind.REASONING_DELTA,
                reasoning_text=update.text_delta,
            )
        elif update.delta_kind == LLMResponseDeltaKind.TOOL_CALL and update.tool_call is not None:
            channel_update = ChannelStreamUpdate(
                kind=ChannelStreamUpdateKind.TOOL_CALL,
                tool_call=update.tool_call,
            )
        elif update.delta_kind == LLMResponseDeltaKind.STATE:
            finish_reason = update.response.finish_reason
            canonical_text = str(update.response.message.text or "").strip()
            if finish_reason == LLMFinishReason.ERROR:
                # Provider failure is not yet the user-facing terminal event.
                # Failure orchestration will produce one canonical fallback
                # through _handle_mailbox_reply.
                channel_update = None
            elif finish_reason in {
                LLMFinishReason.TOOL_CALLS,
                LLMFinishReason.COMPACT_REQUIRED,
            }:
                channel_update = ChannelStreamUpdate(
                    kind=ChannelStreamUpdateKind.DONE,
                    text=canonical_text,
                    finish_reason=finish_reason.value,
                )
            elif canonical_text:
                continuation.channel_stream_terminal_text = canonical_text
                continuation.channel_stream_terminal_finish_reason = finish_reason.value
                channel_update = ChannelStreamUpdate(
                    kind=ChannelStreamUpdateKind.DONE,
                    text=canonical_text,
                    finish_reason=finish_reason.value,
                )
        if channel_update is not None:
            await self.execute_turn_effect_async(
                continuation,
                MailboxReplyStreamUpdateEffect(
                    update=channel_update,
                ),
            )

    async def _emit_synthetic_stream_terminal(
        self,
        continuation: Any,
        *,
        text: str,
        finish_reason: str,
    ) -> None:
        if text:
            await self.execute_turn_effect_async(
                continuation,
                MailboxReplyStreamUpdateEffect(
                    update=ChannelStreamUpdate(
                        kind=ChannelStreamUpdateKind.TEXT_DELTA,
                        text=text,
                    ),
                ),
            )
        await self.execute_turn_effect_async(
            continuation,
            MailboxReplyStreamUpdateEffect(
                update=ChannelStreamUpdate(
                    kind=ChannelStreamUpdateKind.DONE,
                    text=text,
                    finish_reason=finish_reason,
                ),
            ),
        )
        continuation.channel_stream_terminal_text = text
        continuation.channel_stream_terminal_finish_reason = finish_reason

    # ── prompt building ─────────────────────────────────────────────────

    def build_turn_prompt(
        self,
        continuation,
        assembly_context,
        *,
        max_output_tokens: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMRequestIR:
        from pal.shared import PromptAssemblyContext

        metadata = dict(assembly_context.metadata)
        if assembly_context.turn_kind == "failure":
            metadata["observation_blocks"] = [item.to_prompt_block() for item in continuation.tool_observations]
        if continuation.preferred_llm_endpoint_id:
            metadata["preferred_endpoint_id"] = continuation.preferred_llm_endpoint_id
        if continuation.preferred_llm_model_id:
            metadata["preferred_model_id"] = continuation.preferred_llm_model_id
        snapshot_think_levels = dict(continuation.turn_settings_snapshot.get("think_levels") or {})
        if snapshot_think_levels:
            metadata["think_levels"] = snapshot_think_levels
        metadata["prompt_log_enabled"] = bool(continuation.turn_settings_snapshot.get("prompt_log_enabled"))
        logical_scope_id = str(metadata.get("prompt_cache_scope_id") or "").strip()
        if not logical_scope_id:
            logical_scope_id = (
                f"bunshin:{assembly_context.work_order_id or continuation.turn_id}"
                if assembly_context.core_mode == "bunshin"
                else "pal:resident"
            )
        artifact_scope_key = str(
            metadata.get("artifact_scope_key") or logical_scope_id
        ).strip()
        metadata["artifact_scope_key"] = artifact_scope_key
        metadata["artifact_turn_id"] = continuation.turn_id
        metadata["prompt_cache_scope_id"] = logical_scope_id
        metadata["llm_capabilities"] = self._resolve_llm_capabilities(continuation)
        memory_service = self.context.port_registry.get("memory:memory")
        active_turn = None
        active_reader = getattr(memory_service, "active_l1_turn", None)
        if callable(active_reader):
            try:
                active_turn = active_reader(continuation.turn_id)
            except Exception:
                active_turn = None
        if active_turn is not None:
            metadata["active_l1_owns_primary_input"] = True
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
                            active_input_id=str(
                                getattr(
                                    getattr(assembly_context, "event", None),
                                    "event_id",
                                    "",
                                )
                                or ""
                            )
                            or None,
                        )
                    )
                except Exception:
                    pass
        metadata["typed_l1_projection"] = True
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
        stable_messages = [
            replace(message, prompt_region=PromptRegionIR.STABLE_SYSTEM)
            for message in prompt.messages
            if message.role in {MessageRole.SYSTEM, MessageRole.DEVELOPER}
        ]
        contextual_messages = [
            replace(message, prompt_region=PromptRegionIR.ACTIVE_DYNAMIC)
            for message in prompt.messages
            if message.role not in {MessageRole.SYSTEM, MessageRole.DEVELOPER}
        ]
        if active_turn is None and contextual_messages:
            contextual_messages[-1] = replace(
                contextual_messages[-1],
                prompt_region=PromptRegionIR.ACTIVE_INPUT,
            )
        settled_messages: list[LLMMessageIR] = []
        memory_pack = metadata.get("memory_pack")
        for settled_turn in list(getattr(memory_pack, "l1_turns", ()) or ()):
            original = list(getattr(settled_turn, "messages", ()) or ())
            projected = self._project_messages_for_prompt(
                original,
                turn_id=str(getattr(settled_turn, "turn_id", "") or ""),
                artifact_scope_key=artifact_scope_key,
                capabilities=dict(metadata.get("llm_capabilities") or {}),
            )
            settled_messages.extend(
                replace(message, prompt_region=PromptRegionIR.SETTLED_HISTORY)
                for message in projected
            )
        active_messages: list[LLMMessageIR] = []
        if active_turn is not None:
            active_messages = list(active_turn.messages)
            active_messages = self._project_messages_for_prompt(
                active_messages,
                turn_id=continuation.turn_id,
                artifact_scope_key=artifact_scope_key,
                capabilities=dict(metadata.get("llm_capabilities") or {}),
            )
            active_messages = [
                replace(
                    message,
                    prompt_region=(
                        PromptRegionIR.ACTIVE_INPUT
                        if index == 0 and message.role == MessageRole.USER
                        else PromptRegionIR.ACTIVE_DYNAMIC
                    ),
                )
                for index, message in enumerate(active_messages)
            ]
        runtime_reminder = str(prompt.metadata.get("runtime_reminder_text") or "").strip()
        tail_messages = (
            [
                LLMMessageIR(
                    role=MessageRole.USER,
                    parts=(TextPartIR(runtime_reminder),),
                    semantic_kind="runtime_reminder",
                    prompt_region=PromptRegionIR.ACTIVE_DYNAMIC,
                )
            ]
            if runtime_reminder
            else []
        )
        prompt_messages = [
            *stable_messages,
            *settled_messages,
            *contextual_messages,
            *active_messages,
            *tail_messages,
        ]
        metadata = dict(prompt.metadata)
        if snapshot_think_levels:
            metadata["think_levels"] = snapshot_think_levels
        metadata["prompt_log_enabled"] = bool(continuation.turn_settings_snapshot.get("prompt_log_enabled"))
        # PromptCompiler intentionally emits only provider-facing prompt
        # metadata.  Logical cache/artifact ownership is executor-owned, so
        # restore it after compilation instead of allowing Bunshin requests to
        # silently fall back to the resident Pal scope.
        metadata["artifact_scope_key"] = artifact_scope_key
        metadata["artifact_turn_id"] = continuation.turn_id
        metadata["prompt_cache_scope_id"] = logical_scope_id
        metadata["llm_capabilities"] = self._resolve_llm_capabilities(continuation)
        metadata["prompt_budget_snapshot"] = self._build_prompt_budget_snapshot(
            assembly_context,
            base_messages=[
                *stable_messages,
                *settled_messages,
                *contextual_messages,
            ],
            active_messages=[*active_messages, *tail_messages],
            tools=list(tools or []),
        )
        prompt = replace(
            prompt,
            messages=tuple(prompt_messages),
            logical_scope_id=logical_scope_id,
            metadata=metadata,
        )
        return prompt

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
        base_messages: list[LLMMessageIR],
        active_messages: list[LLMMessageIR],
        tools: list[dict[str, Any]],
    ) -> dict[str, int]:
        system_chars = sum(
            self._estimate_ir_message_chars(message)
            for message in base_messages
            if message.role == MessageRole.SYSTEM
        )
        primary_input = ""
        if assembly_context.event is not None:
            primary_input = extract_text_from_payload(assembly_context.event.payload).strip()
        if not primary_input:
            for message in reversed(base_messages):
                if message.role == MessageRole.USER:
                    primary_input = message.text.strip()
                    if primary_input:
                        break
        current_user_chars = len(primary_input)
        base_non_system_chars = sum(
            self._estimate_ir_message_chars(message)
            for message in base_messages
            if message.role != MessageRole.SYSTEM
        )
        protocol_messages = list(active_messages)
        if (
            primary_input
            and protocol_messages
            and protocol_messages[0].role == MessageRole.USER
        ):
            protocol_messages = protocol_messages[1:]
        tool_protocol_chars = sum(
            self._estimate_ir_message_chars(message)
            for message in protocol_messages
        )
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
        if execution_call is not None and str(getattr(execution_call, "name", "") or "").strip() in {"op_exec_shell", "run_shell"}:
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

    @staticmethod
    def _estimate_ir_message_chars(message: LLMMessageIR) -> int:
        total = len(message.text) + len(message.reasoning_text)
        for call in message.tool_calls:
            total += len(call.call_id) + len(call.name)
            total += len(json.dumps(thaw_json(call.arguments), ensure_ascii=False, sort_keys=True))
        for part in message.parts:
            if isinstance(part, ToolResultIR):
                total += len(part.call_id) + len(part.name) + len(part.content)
        return total

    def _project_messages_for_prompt(
        self,
        messages: list[LLMMessageIR],
        *,
        turn_id: str = "",
        artifact_scope_key: str = "pal:resident",
        capabilities: dict[str, Any] | None = None,
    ) -> list[LLMMessageIR]:
        """Resolve prompt-only artifact representations without changing L1 results."""

        return [
            self._project_artifact_refs(
                message,
                turn_id=turn_id,
                scope_key=artifact_scope_key,
                capabilities=capabilities or {},
            )
            for message in messages
        ]

    def _project_artifact_refs(
        self,
        message: LLMMessageIR,
        *,
        turn_id: str,
        scope_key: str,
        capabilities: dict[str, Any],
    ) -> LLMMessageIR:
        refs = [part for part in message.parts if isinstance(part, ArtifactRefPartIR)]
        if not refs:
            return message
        manager = self.context.port_registry.get("artifact:artifact")
        select = getattr(manager, "select_prompt_exposure", None)
        exposure = None
        if callable(select):
            try:
                exposure = select(
                    str(scope_key or "pal:resident"),
                    str(turn_id),
                    message.text,
                    capabilities,
                    artifact_ids=tuple(ref.artifact_id for ref in refs),
                )
            except Exception:
                exposure = None
        replacement: list[Any] = []
        if exposure is not None:
            for inline in exposure.inline_parts:
                source = str(getattr(inline, "source_url", "") or "")
                if not source:
                    resolver = getattr(manager, "to_data_url", None)
                    if callable(resolver):
                        source = str(resolver(inline.representation_id) or "")
                if source:
                    replacement.append(ImagePartIR(source=source, media_type=inline.mime_type or None))
            if str(exposure.text or "").strip():
                replacement.append(TextPartIR(str(exposure.text).strip()))
        if not replacement:
            replacement.append(TextPartIR(_artifact_unavailable_summary(refs)))

        parts: list[Any] = []
        inserted = False
        for part in message.parts:
            if isinstance(part, ArtifactRefPartIR):
                if not inserted:
                    parts.extend(replacement)
                    inserted = True
                continue
            parts.append(part)
        return replace(message, parts=tuple(parts))

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

    @staticmethod
    def clear_execution_cursors(continuation: Any) -> None:
        """Release transient execution cursors after L1 has closed the turn."""

        continuation.pending_assistant_tool_text = ""
        continuation.pending_tool_call_batch = []
        continuation.pending_tool_results = []

    def _render_tool_result_content(self, tool_call: ToolCallIR, result: ToolExecutionResult) -> str:
        if isinstance(getattr(result, "context_delivery", None), dict):
            return str(result.llm_text or "")
        if str(result.llm_text or "").strip():
            return str(result.llm_text).strip()
        return default_tool_result_text(result)

    @staticmethod
    def _is_memory_recall_tool_call(name: str) -> bool:
        normalized = str(name or "").strip()
        return normalized in {"op_memory_recall", "recall_memory"} or normalized.endswith("_memory_recall")

    def _render_memory_recall_tool_observation(self, tool_call: ToolCallIR, result: ToolExecutionResult) -> str:
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

    @staticmethod
    def _llm_runtime_supports_streaming(llm_runtime, request: LLMRequestIR | None = None) -> bool:
        endpoint_facts = TurnExecutor._llm_runtime_endpoint_facts(llm_runtime, request)
        if "supports_streaming" in endpoint_facts and not bool(endpoint_facts.get("supports_streaming")):
            return False
        supports_streaming = getattr(llm_runtime, "supports_streaming", None)
        if callable(supports_streaming):
            try:
                supports_streaming = supports_streaming(request)
            except Exception:
                supports_streaming = False
        if supports_streaming is not None and not bool(supports_streaming):
            return False
        return (
            callable(getattr(llm_runtime, "astream", None))
        )

    @staticmethod
    def _llm_runtime_endpoint_facts(llm_runtime, request: LLMRequestIR | None) -> dict[str, Any]:
        method = getattr(llm_runtime, "resolve_endpoint_facts", None)
        if not callable(method):
            return {}
        metadata = dict(getattr(request, "metadata", {}) or {}) if request is not None else {}
        try:
            facts = method(
                preferred_endpoint_id=str(metadata.get("preferred_endpoint_id") or "").strip() or None,
                preferred_endpoint_source=str(metadata.get("preferred_endpoint_source") or "").strip() or None,
            )
        except TypeError:
            try:
                facts = method()
            except Exception:
                return {}
        except Exception:
            return {}
        return dict(facts or {}) if isinstance(facts, dict) else {}

    def infer_response_mode(self, outcome: LLMGenerationResult | None, *, used_tools: bool) -> str:
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

    @staticmethod
    def _generation_result_from_text(
        text: str,
        *,
        finish_reason: LLMFinishReason,
        response_mode: str | None = None,
    ) -> LLMGenerationResult:
        return LLMGenerationResult(
            response=LLMResponseIR(
                message=LLMMessageIR(
                    role=MessageRole.ASSISTANT,
                    parts=(TextPartIR(str(text)),) if str(text) else (),
                    state=MessageState.COMPLETE,
                ),
                finish_reason=finish_reason,
                provider_response_count=0,
            ),
            response_mode=response_mode,
        )

    async def _upsert_l1_assistant_async(
        self,
        continuation: Any,
        message: LLMMessageIR,
    ) -> None:
        memory_service = self.context.port_registry.get("memory:memory")
        method = getattr(memory_service, "upsert_l1_assistant", None)
        if not callable(method):
            return
        if not message.semantic_kind:
            message = replace(
                message,
                semantic_kind=(
                    L1MessageKind.ASSISTANT_TOOL_CALL
                    if message.tool_calls
                    else L1MessageKind.ASSISTANT_REPLY
                ),
            )
        result = method(str(continuation.turn_id), message)
        if inspect.isawaitable(result):
            await result

    async def _discard_l1_assistant_async(
        self,
        continuation: Any,
        message_id: str,
    ) -> None:
        memory_service = self.context.port_registry.get("memory:memory")
        method = getattr(memory_service, "discard_l1_assistant", None)
        if not callable(method):
            return
        active_reader = getattr(memory_service, "active_l1_turn", None)
        if callable(active_reader):
            active = active_reader(str(continuation.turn_id))
            if active is None or not any(
                item.role == MessageRole.ASSISTANT
                and str(item.message_id or "") == str(message_id)
                for item in active.messages
            ):
                return
        result = method(str(continuation.turn_id), str(message_id))
        if inspect.isawaitable(result):
            await result

    def select_turn_temperature(self, response_mode: str) -> float:
        base_by_mode = {
            LLMResponseMode.CHAT: 0.7,
            LLMResponseMode.OPERATIONAL: 0.2,
            LLMResponseMode.REVIEW: 0.1,
        }
        value = base_by_mode.get(response_mode, 0.3)
        return max(0.0, min(1.0, round(value, 2)))

    # ── L1 working-set settlement ───────────────────────────────────────

    async def _ensure_l1_turn_async(
        self,
        continuation: Any,
        assembly_context: Any,
    ) -> bool:
        memory_service = self.context.port_registry.get("memory:memory")
        if memory_service is None:
            return False
        event = getattr(assembly_context, "event", None)
        event_payload = getattr(event, "payload", None)
        user_message = event_payload if isinstance(event_payload, LLMMessageIR) else None
        text = "" if user_message is not None else extract_text_from_payload(event_payload).strip()
        if not text:
            text = str(dict(getattr(assembly_context, "metadata", {}) or {}).get("proactive_input") or "").strip()
        try:
            method = getattr(memory_service, "begin_l1_turn")
            method(
                str(continuation.turn_id),
                user_text=text,
                user_message=user_message,
                metadata={"_pal_input_id": self._active_input_id(continuation, assembly_context)},
            )
            return True
        except Exception:
            return False

    async def _append_l1_tool_result_async(
        self,
        continuation: Any,
        call: ToolCallIR,
        result: ToolExecutionResult,
    ) -> None:
        memory_service = self.context.port_registry.get("memory:memory")
        method = getattr(memory_service, "append_l1_tool_result", None)
        if not callable(method):
            return
        content = self._render_tool_result_content(call, result)
        turn_id = str(continuation.turn_id)
        previous = getattr(memory_service, "active_l1_turn", lambda _turn_id: None)(
            turn_id
        )
        tool_result = ToolResultIR(
                call_id=call.call_id,
                name=call.name,
                content=content,
                ok=result.ok,
                status=str(result.status or ("ok" if result.ok else "error")),
                structured=dict(result.structured) if result.structured is not None else None,
                context_delivery=(
                    dict(result.context_delivery)
                    if isinstance(result.context_delivery, dict)
                    else None
                ),
                replay_result_ref=str(result.replay_result_ref or ""),
        )
        try:
            method(turn_id, tool_result)
        except Exception:
            self._discard_uncommitted_tool_delivery(
                turn_id,
                result.replay_result_ref,
            )
            raise
        delivery = getattr(result, "context_delivery", None)
        commit = getattr(
            getattr(self.context, "execution_runtime", None),
            "commit_tool_delivery",
            None,
        )
        if isinstance(delivery, dict) and callable(commit):
            try:
                commit(
                    turn_id=str(continuation.turn_id),
                    context_delivery=dict(delivery),
                    result_id=call.call_id,
                )
            except Exception:
                rollback = getattr(memory_service, "rollback_l1_tool_result", None)
                if previous is None or not callable(rollback):
                    raise RuntimeError(
                        "tool delivery commit failed and L1 cannot be rolled back"
                    )
                rollback(
                    turn_id,
                    previous=previous,
                    call_id=call.call_id,
                )
                self._discard_uncommitted_tool_delivery(
                    turn_id,
                    result.replay_result_ref,
                )
                raise

    def _discard_uncommitted_tool_delivery(
        self,
        turn_id: str,
        result_ref: str,
    ) -> None:
        discard = getattr(
            getattr(self.context, "execution_runtime", None),
            "discard_uncommitted_tool_delivery",
            None,
        )
        if callable(discard):
            discard(turn_id=str(turn_id), result_ref=str(result_ref or ""))

    @staticmethod
    def _active_input_id(
        continuation: Any,
        assembly_context: Any | None,
    ) -> str:
        event = getattr(assembly_context, "event", None)
        if event is None:
            event = getattr(continuation, "opening_event", None)
        return (
            str(getattr(event, "event_id", "") or "").strip()
            or str(getattr(continuation, "turn_id", "") or "").strip()
        )

    # ── post-turn commit ─────────────────────────────────────────────────

    async def schedule_post_turn_commit_async(self, outcome) -> Any:
        memory_service = self.context.port_registry.get("memory:memory")
        result = None
        if memory_service is not None:
            try:
                turn_id = str(outcome.commit_payload.turn_id)
                result = memory_service.settle_l1_turn(turn_id)
            except Exception as exc:
                self.state.diagnostics.append(
                    {
                        "kind": "memory.turn.settle_failed",
                        "turn_id": outcome.commit_payload.turn_id,
                        "status": RuntimeStatus.ERROR,
                        "error": str(exc),
                    }
                )
                # Do not advance lifecycle clocks or hide a failed L1 commit
                # from the caller.
                raise
            try:
                memory_service.l2_store.tick_heat()
            except Exception:
                pass
        self._tick_behavior_lifecycle()
        self._reap_expired_artifacts()
        return result

    def _reap_expired_artifacts(self) -> None:
        service = self.context.port_registry.get("artifact:artifact")
        reap = getattr(service, "reap_expired", None)
        if not callable(reap):
            return
        try:
            reap()
        except Exception as exc:
            self.state.diagnostics.append(
                {
                    "kind": "artifact.lifecycle.reap_failed",
                    "status": RuntimeStatus.ERROR,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )

    def _tick_behavior_lifecycle(self) -> None:
        behavior_service = self.context.port_registry.get("behavior:behavior")
        tick = getattr(behavior_service, "tick_advisor_hints", None)
        if not callable(tick):
            return
        try:
            tick()
        except Exception:
            pass

    # ── shared compaction engine ─────────────────────────────────────────

    async def compact_memory_async(
        self,
        memory_service: Any,
        *,
        target_input_budget: int,
        reserved_output_tokens: int,
        assembly_context: Any | None = None,
        continuation: Any | None = None,
        preferred_endpoint_id: str | None = None,
        preferred_model_id: str | None = None,
    ) -> CompactionRunResult:
        engine = self._compaction_engine
        if engine is None:
            return CompactionRunResult(
                status="engine_unavailable",
                clock_kind=CompactionClockKind.USER_TURN,
            )
        llm_runtime = self.context.port_registry.get("llm:llm")
        if llm_runtime is None:
            return CompactionRunResult(
                status="engine_unavailable",
                clock_kind=engine.policy.clock_kind,
            )
        metadata = dict(
            getattr(assembly_context, "metadata", {}) or {}
        )
        logical_scope_id = str(
            metadata.get("prompt_cache_scope_id") or ""
        ).strip()
        if not logical_scope_id:
            bunshin_scope_key = (
                getattr(assembly_context, "work_order_id", "")
                or getattr(continuation, "turn_id", "")
            )
            logical_scope_id = (
                f"bunshin:{bunshin_scope_key}"
                if getattr(assembly_context, "core_mode", "") == "bunshin"
                else "pal:resident"
            )
        metadata["prompt_cache_scope_id"] = logical_scope_id
        preferred_endpoint_id = (
            preferred_endpoint_id
            or metadata.get("preferred_endpoint_id")
            or getattr(
                continuation,
                "preferred_llm_endpoint_id",
                None,
            )
        )
        preferred_model_id = (
            preferred_model_id
            or metadata.get("preferred_model_id")
            or getattr(
                continuation,
                "preferred_llm_model_id",
                None,
            )
        )
        try:
            clock_value = max(
                0,
                int(self._compaction_clock_provider() or 0),
            )
        except Exception:
            clock_value = 0
        snapshot = CompactionSnapshot.capture(
            memory_service,
            target_input_budget=target_input_budget,
            reserved_output_tokens=reserved_output_tokens,
            clock_kind=engine.policy.clock_kind,
            clock_value=clock_value,
            metadata={
                **metadata,
                "preferred_endpoint_id": preferred_endpoint_id,
                "preferred_model_id": preferred_model_id,
                "prompt_cache_scope_id": logical_scope_id,
            },
        )
        execution_runtime = getattr(self.context, "execution_runtime", None)
        def current_l1_result_ids() -> tuple[str, ...]:
            turns = getattr(
                getattr(getattr(memory_service, "l1_store", None), "turns", None),
                "turns",
                (),
            )
            return tuple(
                dict.fromkeys(
                    part.call_id
                    for turn in list(turns or ())
                    for message in turn.messages
                    for part in message.parts
                    if isinstance(part, ToolResultIR)
                )
            )

        result_ids_before_compact = current_l1_result_ids()

        after_compact = None
        retire_tool_results = getattr(
            execution_runtime,
            "retire_tool_results",
            None,
        )
        if callable(retire_tool_results) and result_ids_before_compact:
            def retire_compacted_l1_results() -> None:
                remaining_ids = set(current_l1_result_ids())
                removed = tuple(
                    result_id
                    for result_id in result_ids_before_compact
                    if result_id not in remaining_ids
                )
                if removed:
                    retirement_turn_id = (
                        str(continuation.turn_id)
                        if continuation is not None
                        else None
                    )
                    retire_tool_results(
                        turn_id=retirement_turn_id,
                        result_ids=removed,
                        execution_lifetime_id=(
                            "" if retirement_turn_id else logical_scope_id
                        ),
                    )

            after_compact = retire_compacted_l1_results
        run_result = await engine.run(
            snapshot,
            llm_runtime=llm_runtime,
            memory_service=memory_service,
            after_commit=after_compact,
        )
        if not run_result.success or continuation is None:
            return run_result

        self.clear_execution_cursors(continuation)
        return run_result
