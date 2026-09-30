from __future__ import annotations
from pal.bunshin.runner_components.numeric_values import _optional_positive_int
from pal.bunshin.runner_components.models import BunshinLLMRetryableError
from pal.bunshin.runner_components.models import BunshinAgentLoopState
from pal.bunshin.runner_components.llm_settings import _bunshin_generation_result
from pal.bunshin.runner_components.prompt_values import _is_truncation_finish_reason
from pal.bunshin.runner_components.prompt_values import _tool_call_summary
from pal.bunshin.runner_components.prompt_values import DEFAULT_BUNSHIN_OUTPUT_LENGTH_RECOVERY_ROUNDS
from pal.bunshin.runner_components.prompt_values import BUNSHIN_OUTPUT_LENGTH_RECOVERY_NOTE
from pal.bunshin.runner_components.progress_text import _preview_text
from dataclasses import dataclass
from typing import Any
from pal.core.turn_executor import TurnExecutor
from pal.core.turns import EffectResult, LLMRequestEffect, MemoryCompactEffect, TurnContinuation
from pal.llm.output_recovery import has_committed_tool_calls
from pal.shared import LLMFinishReason, RuntimeStatus, BunshinInvocationPack
from pal.bunshin.runner_components.completion import Completion
from pal.bunshin.runner_components.control import Control
from pal.bunshin.runner_components.heartbeat import Heartbeat
from pal.bunshin.runner_components.prompt_context import PromptContext
from pal.bunshin.runner_components.reporter import Reporter
from pal.bunshin.runner_components.status import Status
from pal.bunshin.runner_components.text_deliverables import TextDeliverables
from pal.bunshin.runner_components.tool_session import ToolSession


@dataclass
class LlmRounds:
    completion: Completion
    control: Control
    heartbeat: Heartbeat
    prompt_context: PromptContext
    reporter: Reporter
    status: Status
    text_deliverables: TextDeliverables
    tool_session: ToolSession
    pack: BunshinInvocationPack

    def build_bunshin_retry_note(
        self,
        outcome: Any,
        observations: list[Any],
        retry_count: int,
        *,
        state: BunshinAgentLoopState | None = None,
    ) -> str:
        if state is not None and state.pending_output_length_recovery_note:
            return state.pending_output_length_recovery_note
        if self.status.blocked_summary:
            return ""
        if self.tool_session.execution_sessions is not None and self.tool_session.execution_sessions.has_work:
            return self.tool_session.execution_sessions.retry_note()
        if str(getattr(outcome, "finish_reason", "") or "") == LLMFinishReason.ERROR:
            return ""
        tools_available = bool(self.pack.allowed_capabilities)
        if not tools_available:
            return ""
        if retry_count > 0:
            return ""
        if observations:
            return ""
        if not self.requires_first_tool_call():
            return ""
        _ = outcome
        return (
            "You have not used any capability yet. This contract invocation requires executable evidence. "
            "Use one listed capability now to inspect, research, read, write, or verify before completing."
        )

    async def execute_bunshin_agent_effect(
        self,
        executor: TurnExecutor,
        continuation: TurnContinuation,
        state: BunshinAgentLoopState,
        effect: Any,
        *,
        max_output_tokens: int,
    ) -> EffectResult:
        if isinstance(effect, LLMRequestEffect):
            preflight = self.preflight_bunshin_llm_round(state)
            if preflight is not None:
                return preflight
        result = await executor.execute_turn_effect_async(continuation, effect)
        self.sync_bunshin_state_from_continuation(state, continuation)
        if (
            isinstance(effect, MemoryCompactEffect)
            and result.status == RuntimeStatus.OK
        ):
            await self.reporter.emit_progress(
                "memory_compacted",
                round=state.llm_round_count,
                summary=_preview_text(getattr(result.payload, "summary", ""), limit=500),
            )
        if isinstance(effect, LLMRequestEffect):
            result = await self.postprocess_bunshin_llm_round(
                state,
                result,
                continuation=continuation,
            )
            outcome = result.payload
            if (self.tool_session.execution_sessions is not None and not self.status.blocked_summary
                and not continuation.finalization_only and not getattr(outcome, "tool_calls", ())
                and str(getattr(outcome, "finish_reason", "")) == LLMFinishReason.STOP):
                blocked = await self.heartbeat.await_with_progress_heartbeat(
                    self.tool_session.execution_sessions.wait_after_response(self.control.raise_if_cancel_requested),
                    phase="execution_session_waiting", round=state.llm_round_count,
                )
                if blocked:
                    self.status.block(blocked)
                    result = EffectResult(status=RuntimeStatus.OK, payload=_bunshin_generation_result(blocked))
        return result

    def preflight_bunshin_llm_round(self, state: BunshinAgentLoopState) -> EffectResult | None:
        if self.status.blocked_summary:
            return EffectResult(status=RuntimeStatus.OK, payload=_bunshin_generation_result(self.status.blocked_summary))
        execution_pending = self.tool_session.execution_sessions is not None and self.tool_session.execution_sessions.has_work
        if not execution_pending and self.completion.required_primary_artifact_name() and self.completion.completion_evidence_present():
            return EffectResult(
                status=RuntimeStatus.OK,
                payload=_bunshin_generation_result(
                    text="assignment produced completion evidence",
                    finish_reason=LLMFinishReason.STOP,
                ),
            )
        max_rounds = _optional_positive_int(self.pack.metadata.get("max_tool_rounds") if isinstance(self.pack.metadata, dict) else None)
        if max_rounds is None or state.llm_round_count < max_rounds:
            state.llm_round_count += 1
            return None
        if not execution_pending and (self.completion.completion_evidence_present() or self.completion.artifact_completion_evidence_present()):
            outcome = _bunshin_generation_result("assignment produced completion evidence")
        else:
            self.status.block(f"bunshin reached explicit max_tool_rounds={max_rounds} before completing the current invocation")
            outcome = _bunshin_generation_result(self.status.blocked_summary)
        return EffectResult(status=RuntimeStatus.OK, payload=outcome)

    async def postprocess_bunshin_llm_round(
        self,
        state: BunshinAgentLoopState,
        result: EffectResult,
        *,
        continuation: TurnContinuation | None = None,
    ) -> EffectResult:
        outcome = result.payload
        finish_reason = str(getattr(outcome, "finish_reason", "") or "")
        provider_failed = finish_reason in {
            LLMFinishReason.ERROR,
            LLMFinishReason.COMPACT_REQUIRED,
        }
        truncated = _is_truncation_finish_reason(finish_reason)
        committed_tool_calls = bool(
            truncated
            and has_committed_tool_calls(getattr(outcome, "response", None))
        )
        has_consumable_output = bool(
            str(getattr(outcome, "text", "") or "").strip()
            or list(getattr(outcome, "tool_calls", []) or [])
        )
        consumed = (
            not provider_failed
            and (not truncated or committed_tool_calls)
            and has_consumable_output
        )
        if not consumed:
            state.llm_round_count = max(0, state.llm_round_count - 1)
        if provider_failed:
            # Provider exhaustion produced no assistant/tool turn.  Keep the
            # durable checkpoint at the same logical round so a later process
            # attempt retries the exact request instead of projecting phantom
            # progress into the role session.
            if (
                finish_reason == LLMFinishReason.ERROR
                and (
                    self.completion.completion_evidence_present()
                    or self.completion.artifact_completion_evidence_present()
                )
            ):
                outcome = _bunshin_generation_result(
                    text=self.completion.completion_evidence_fallback_text(str(getattr(outcome, "text", "") or "")),
                    finish_reason=LLMFinishReason.STOP,
                )
                finish_reason = str(LLMFinishReason.STOP)
                result = EffectResult(status=RuntimeStatus.OK, payload=outcome)
            elif finish_reason == LLMFinishReason.ERROR:
                # Do not turn an endpoint error into a completed/blocked role
                # turn.  The manager must retry this same logical session
                # from its last safe checkpoint; settling L1 here would make
                # the next worker's first update a late write to a closed turn.
                raise BunshinLLMRetryableError(
                    str(getattr(outcome, "text", "") or "LLM generation failed")
                )
        elif truncated and not committed_tool_calls:
            if continuation is not None:
                response = getattr(outcome, "response", None)
                message = getattr(response, "message", None)
                message_id = str(getattr(message, "message_id", "") or "").strip()
                if not message_id:
                    raise RuntimeError(
                        "truncated LLM response has no message id for atomic L1 discard"
                    )
                state.memory_service.discard_l1_assistant(
                    continuation.turn_id,
                    message_id,
                )
            recovery_limit = max(
                0,
                int(
                    self.pack.metadata.get(
                        "max_output_length_recovery_rounds",
                        DEFAULT_BUNSHIN_OUTPUT_LENGTH_RECOVERY_ROUNDS,
                    )
                    if isinstance(self.pack.metadata, dict)
                    else DEFAULT_BUNSHIN_OUTPUT_LENGTH_RECOVERY_ROUNDS
                ),
            )
            if state.output_length_recovery_count < recovery_limit:
                state.output_length_recovery_count += 1
                state.pending_output_length_recovery_note = (
                    BUNSHIN_OUTPUT_LENGTH_RECOVERY_NOTE
                )
                await self.reporter.emit_progress(
                    "llm_output_length_recovery_scheduled",
                    round=state.llm_round_count,
                    attempt=state.output_length_recovery_count,
                    max_attempts=recovery_limit,
                    summary="truncated response discarded; bounded tool-call recovery scheduled",
                )
            else:
                partial_text = str(getattr(outcome, "text", "") or "").strip()
                if partial_text:
                    await self.text_deliverables.persist_text_deliverable_if_needed(
                        partial_text,
                        partial=True,
                        truncation_reason=finish_reason,
                    )
                state.pending_output_length_recovery_note = ""
                self.status.block(self.text_deliverables.truncated_output_blocked_summary(
                    finish_reason
                ))
        elif consumed:
            state.output_length_recovery_count = 0
            state.pending_output_length_recovery_note = ""
        await self.reporter.emit_progress(
            "llm_round_completed" if consumed else "llm_round_discarded",
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
            tool_call_count=len(list(getattr(outcome, "tool_calls", []) or [])),
            tool_calls=[_tool_call_summary(item) for item in list(getattr(outcome, "tool_calls", []) or [])],
            text_preview=_preview_text(str(getattr(outcome, "text", "") or "")),
        )
        return result

    def sync_bunshin_state_from_continuation(self, state: BunshinAgentLoopState, continuation: TurnContinuation) -> None:
        state.pending_assistant_tool_text = continuation.pending_assistant_tool_text
        state.pending_tool_call_batch = list(continuation.pending_tool_call_batch)
        state.pending_tool_results = list(continuation.pending_tool_results)

    def requires_first_tool_call(self) -> bool:
        if bool((self.pack.metadata or {}).get("allow_text_only_completion")):
            return False
        completion_policy = self.prompt_context.completion_policy()
        if "requires_capability_evidence" in completion_policy:
            return bool(completion_policy.get("requires_capability_evidence")) and bool(self.pack.allowed_capabilities)
        return str(completion_policy.get("evidence") or "").strip().lower() == "git_commit" and bool(self.pack.allowed_capabilities)
