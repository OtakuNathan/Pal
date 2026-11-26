from __future__ import annotations

from pal.llm.history_projection import HistoryProjection
from pal.core.compaction_operation import CompactionOperation
from pal.llm.contracts import LLM_RUNTIME
from pal.memory.contracts import MEMORY

from pal.shared.tool_protocol import CompleteResult, ToolCallIR, ToolResultIR
from pal.shared.json_values import thaw_json
from pal.shared.diagnostics import exception_summary, exception_reason

import asyncio
import inspect
import json
import logging
from datetime import datetime, timezone
from dataclasses import replace
from functools import singledispatchmethod
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from pal.execution.contracts import ToolCallBudget
from pal.llm.compaction_contracts import CompactionService
from pal.core.compaction_coordinator import compaction_gate_active
from pal.memory.persistence.contracts import RESIDENT_HISTORY
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
    TurnContinuation,
)
from pal.core.core_events import TURN_TOOL_CALL_FAILED, TURN_TOOL_RESULT_COMMITTED
from pal.failure import FailureSignal
from pal.llm.contracts import LLMGenerationResult, LLMPreflightRequest
from pal.llm.conversions import tool_definition_ir_from_dict
from pal.llm.serde import message_to_payload
from pal.llm.ir import (
    ArtifactRefPartIR,
    ImagePartIR,
    InstructionScopeIR,
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
from pal.memory.compact import memory_candidates_from_compact_result, compact_normalization_diagnostics
from pal.memory.contracts import L1MessageKind, MemoryPackRequest
from pal.memory.prompt_context import prompt_date_pin, select_prompt_date_pin
from pal.llm.cache_reminder import prefix_refresh_likely_cold
from pal.shared.prompt_dates import today_for_timezone
from pal.shared import (
    GuardAction,
    LLMPreflightStatus,
    LLMResponseMode,
    ChannelStreamUpdateKind,
    RuntimeStatus,
    ToolExecutionResult,
)
from pal.shared.payloads import extract_text_from_payload
from pal.shared.agent_io import ChannelMessage, ChannelStreamUpdate

LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pal.core.main_context import MainContext
    from pal.shared import PromptAssemblyContext


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
    borrowed_prompt_date: str | None = None
    include_live_execution_context = True

    def __init__(
        self,
        context: MainContext,
        state,
        turn_manager,
        *,
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
        compaction_engine: CompactionService | None = None,
        compaction_clock_provider: Callable[[], int] | None = None,
        after_tool_batch: Callable[[Any], Awaitable[None]] | None = None,
        inject_pending: Callable[[Any], Awaitable[bool]] | None = None,
        after_compaction: Callable[[Any], Awaitable[None]] | None = None,
    ) -> None:
        self.context = context
        self.llm = context.port_registry.observe(LLM_RUNTIME)
        self.memory = context.port_registry.observe(MEMORY)
        self.projection = HistoryProjection(self.memory, state.diagnostics,
            self._project_messages_for_prompt, self._resolve_llm_capabilities_for_endpoint)
        self.state = state
        self.turn_manager = turn_manager
        self._config = config or RuntimeConfig.defaults()
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
        self.compaction = CompactionOperation(context=context, state=state, llm=self.llm,
            projection=self.projection, engine=compaction_engine,
            clock=compaction_clock_provider or (lambda: 0),
            build_prompt=lambda *args, **kwargs: self._build_canonical_prompt(*args, **kwargs),
            build_tools=lambda: self._build_llm_tool_contracts(), clear_cursors=self.clear_execution_cursors)
        self._after_tool_batch = after_tool_batch
        self._inject_pending = inject_pending
        self._after_compaction = after_compaction

    # ── public entry point ──────────────────────────────────────────────

    async def execute_turn_effect_async(self, continuation, effect):
        self._ensure_not_interrupted(continuation)
        continuation.waiting_effect_id = effect.effect_id
        result = await self._dispatch_effect(effect, continuation)
        self._ensure_not_interrupted(continuation)
        continuation.waiting_effect_id = None
        return result

    async def _checkpoint_resident_async(self, turn_id: str, reason: str, continuation=None) -> None:
        history = self.context.port_registry.get(RESIDENT_HISTORY.name)
        if history is not None:
            history.finish_partial_stream(str(turn_id))
            await history.checkpoint_async(reason)
            if continuation is not None:
                output = self._agent_output_port()
                binding = continuation.delivery_binding
                if output is not None and binding is not None and history.take_degraded_notice():
                    try:
                        await self._call_output_port_async(output.queue_status, binding, "history_persistence_degraded",
                            payload={"text": "Conversation storage is temporarily unavailable. Recovery or searchable "
                                     "history may be incomplete; Pal will retry saving at the next round boundary."})
                    except Exception:
                        self.state.diagnostics.append({"kind": "resident.history.notice_failed", "turn_id": turn_id})

    # ── effect dispatch (singledispatch — C++ overload style) ────────────

    @singledispatchmethod
    async def _dispatch_effect(self, effect, continuation):
        return EffectResult(status=RuntimeStatus.UNSUPPORTED, text=f"unknown effect: {effect.kind}")

    @_dispatch_effect.register(LLMPreflightEffect)
    async def _handle_llm_preflight(self, effect, continuation):
        self._begin_l1_turn(
            continuation,
            effect.assembly_context,
        )
        llm_runtime = self.llm.current
        tools = self._resolve_llm_tools(continuation, effect.tools_override)
        prompt = self.build_turn_prompt(
            continuation,
            effect.assembly_context,
            max_output_tokens=effect.max_output_tokens,
            tools=tools,
        )
        advice = await llm_runtime.preflight(LLMPreflightRequest(request=prompt))
        continuation.prompt_budget_snapshot = dict(advice.breakdown or {})
        if self._is_hard_budget_overflow(advice):
            failure_result = await self._handle_failure_async(
                FailureSignal(
                    subsystem="core",
                    component="turn_budget",
                    failure_kind="context_budget_exhausted",
                    severity="high",
                    primary_blocker="The current turn exceeds the available context window before any older history can be compacted.",
                    evidence={
                        "prompt_budget": dict(advice.breakdown or {}),
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
        elif (
            effect.assembly_context.turn_kind != "proactive_trigger"
            and self._inject_pending is not None
            and str(advice.status) == LLMPreflightStatus.READY
            and continuation.llm_round_index >= 1
            and self.state.pending_channel_turns
            and not compaction_gate_active(self.state)
        ):
            # Queued interjection admission point (P1 ordering): queued
            # input is admitted only between rounds of a running turn
            # (llm_round_index >= 1, i.e. after a tool batch), never at
            # the turn's first preflight — a burst that arrived before
            # the turn ever ran stays FIFO-queued and drains as its own
            # turns instead of being absorbed into this one. Admission
            # additionally requires the next ordinary request to be real
            # and no compaction ticket holding the scope, so fresh user
            # input is never fed into an imminent compaction source. The
            # prompt and budget advice are recomputed after the append
            # (one bounded second pass; no further injection inside this
            # effect).
            injected = await self._inject_pending(continuation)
            if injected:
                prompt = self.build_turn_prompt(
                    continuation,
                    effect.assembly_context,
                    max_output_tokens=effect.max_output_tokens,
                    tools=tools,
                )
                advice = await llm_runtime.preflight(LLMPreflightRequest(request=prompt))
                continuation.prompt_budget_snapshot = dict(advice.breakdown or {})
        return EffectResult(status=RuntimeStatus.OK, payload=advice)

    def _round_safe_for_compaction(
        self, continuation, *, reasons: list[str] | None = None,
    ) -> bool:
        """Round-safety evidence for auto compaction admission (A01-A05).

        HTTP completion alone is not proof: require no in-progress/
        incomplete message and a fully paired tool protocol on the active
        L1 turn. Effects are strictly sequential in the turn program, so
        while this effect runs no other effect can be mid-flight
        (checking ``waiting_effect_id`` here would always see this effect
        itself and reject every legitimate claim). Likewise the L1 store's
        ``has_open_round`` streaming bookkeeping stays open after the
        terminal provider response on the real auto path, so it must not
        gate admission here either; message-state closure below is the
        boundary that matters.

        ``reasons`` (A04), when provided, collects a structured cause per
        rejection so an unknown/unreconciled effect is explicitly visible
        as reconcile-required instead of a silent bool.
        """

        def _note(reason: str) -> None:
            if reasons is not None:
                reasons.append(reason)

        memory_service = self.memory.optional
        if memory_service is None:
            _note("memory_service_unavailable")
            return False
        turn_id = str(continuation.turn_id)
        turn = memory_service.active_l1_turn(turn_id)
        if turn is None:
            _note("no_active_l1_turn")
            return False
        calls: set[str] = set()
        results: set[str] = set()
        for message in turn.messages:
            message_state = str(message.state)
            if message_state in {"in_progress", "incomplete"}:
                _note(f"open_message:{message_state}")
                return False
            for part in message.parts:
                if isinstance(part, ToolCallIR):
                    calls.add(str(part.call_id))
                elif isinstance(part, ToolResultIR):
                    results.add(str(part.call_id))
        if calls != results:
            # A04: a mutation whose effect ledger entry is unknown (result
            # not closed) is never deleted to fake closure and never
            # re-run; the claim must wait for reconciliation.
            unpaired = sorted(calls.symmetric_difference(results))
            _note("reconcile_required:unpaired_tool_protocol:" + ",".join(unpaired))
            return False
        return True

    @_dispatch_effect.register(MemoryCompactEffect)
    async def _handle_memory_compact(self, effect, continuation):
        if effect.assembly_context.turn_kind == "proactive_trigger":
            return EffectResult(status=RuntimeStatus.ERROR, text=(
                "This proactive run exceeds its independent context budget. "
                "Stopping with its work recorded; resident conversation history was not compacted."
            ))
        self._begin_l1_turn(
            continuation,
            effect.assembly_context,
        )
        round_reasons: list[str] = []
        if not self._round_safe_for_compaction(
            continuation, reasons=round_reasons,
        ):
            # Round-safety proof (A01-A05): HTTP completion alone is not a
            # safe boundary. The claim waits until the round is closed.
            # A04: the rejection is structured evidence — an unknown or
            # unreconciled mutation is explicitly reconcile-required, never
            # silently deleted to fake closure and never re-run.
            diagnostics = self.state.diagnostics
            if diagnostics is not None:
                diagnostics.append({
                    "kind": "compaction_round_unsafe",
                    "turn_id": str(continuation.turn_id),
                    "reasons": list(round_reasons),
                    "reconcile_required": any(
                        reason.startswith("reconcile_required")
                        for reason in round_reasons
                    ),
                })
            return EffectResult(
                status=RuntimeStatus.ERROR,
                text=(
                    "Memory compaction requires a closed round: a provider "
                    "stream, tool protocol, or result commit is still open."
                ),
            )
        memory_service = self.memory.current
        # two_segment admission: the history owner IS the arbiter — one live
        # run per session (CompactLaneBusy), owner-side terminal arbitration,
        # and the minimal-seed guard already refuses re-compacting an
        # unchanged left segment (L08).  No tickets, no ledger, no staging.

        run_result = await self.compaction.run(
            memory_service,
            target_input_budget=effect.target_input_budget,
            reserved_output_tokens=effect.reserved_output_tokens,
            assembly_context=effect.assembly_context,
            continuation=continuation,
        )
        if not run_result.success:
            return EffectResult(
                status=RuntimeStatus.ERROR,
                text=("Memory compaction did not report success.\n"
                      + run_result.user_notice),
                payload=run_result,
            )
        compact_result = run_result.memory_result
        accepts_candidates = self.compaction.accepts_memory_candidates
        candidates = (
            memory_candidates_from_compact_result(compact_result)
            if accepts_candidates
            else []
        )
        if candidates:
            from pal.control.contracts import ControlRoute
            from pal.memory.mutations import content_hash
            batch = {
                "source_kind": "pal_compact", "source_label": "Pal compact",
                "memory_candidates": candidates,
                "normalization_diagnostics": compact_normalization_diagnostics(compact_result),
                "candidate_batch_id": "compact_" + content_hash({
                    "turn": continuation.turn_id, "candidates": candidates,
                    "index": len(continuation.pending_compact_memory_candidate_batches)})[:24],
            }
            binding = continuation.delivery_binding
            if binding is not None:
                route = ControlRoute(
                    endpoint_id=binding.endpoint.endpoint_id, channel_kind=binding.endpoint.channel_kind,
                    reply_target=dict(binding.response_handle.reply_target),
                    control_scope_key=binding.control_scope_key, correlation_id=binding.correlation_id)
                batch["source_ref"] = batch["candidate_batch_id"]
                # Persist before resuming the turn. Delivery can wait; a crash
                # leaves the draft reachable through /memory_review. A stage
                # failure here must NOT roll back the committed compact
                # (I08): it is recorded as a diagnostic and the batch still
                # rides the turn's pending candidate delivery at the owner —
                # never an automatic L3 write, and Bunshin lanes never gain a
                # candidate route (their policy rejects candidates upstream).
                try:
                    memory_service.reviews.stage_payload(batch, route)
                except Exception as exc:
                    diagnostics = self.state.diagnostics
                    if diagnostics is not None:
                        diagnostics.append({
                            "kind": "compaction_candidate_stage_failed",
                            "candidate_batch_id": batch.get("candidate_batch_id"),
                            "error": f"{type(exc).__name__}: {exc}",
                        })
            continuation.pending_compact_memory_candidate_batches.append(batch)
        if self._after_compaction is not None:
            # Drain queued input into the fresh context before the loop's
            # next preflight (PLAN section 4 default order).
            await self._after_compaction(continuation)
        # R08 lease: artifacts referenced by the committed L1 (seed plus
        # active input) keep their TTL refreshed after the install, so a
        # restart still finds the owner-scoped content and never treats a
        # live handle as a new resource to request or delete.
        try:
            from pal.core.artifact_lease import (
                artifact_ids_from_l1_turns,
                touch_artifacts,
            )

            turns = memory_service.history.turns
            if turns:
                live_ids = artifact_ids_from_l1_turns(list(turns or ()))
                if live_ids:
                    touch_artifacts(
                        self.context,
                        live_ids,
                        str(self.state.resident_execution_lifetime_id or ""),
                    )
        except Exception:
            pass
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
        continuation.llm_round_index = continuation.llm_round_index + 1
        llm_runtime = self.llm.current
        tools = self._resolve_llm_tools(continuation, effect.tools_override)
        prompt = self.build_turn_prompt(
            continuation,
            effect.assembly_context,
            max_output_tokens=effect.max_output_tokens,
            tools=tools,
            commit_scoped_instruction=True,
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
            metadata={
                **dict(prompt.metadata),
                "failure_context": self._llm_failure_context(continuation),
            },
        )
        snapshot_store = self.context.execution_runtime.result_snapshots
        memory = self.memory.optional
        history = (memory.history if memory is not None else None)
        if snapshot_store is not None and history is not None:
            snapshot_store.pin_history_request(history, str(continuation.turn_id))
        self._debug_log_prompt(continuation, request)
        # N3 (review §4) + F1/F2/F4 (review af51d74): the projection is
        # prepared owner-side for THIS round from the runtime's immutable
        # prepared-generation plan — typed immutable input, never a session
        # in metadata — and the accept side closes the loop only after the
        # canonical acceptance boundary, authorized by the send receipt.
        # None everywhere means the honest cold codec path.
        projection_pack = self.projection.prepare(llm_runtime, request)
        on_submitted = self._history_submission_observer(request)
        # R6 (review 95373ef): the prepared projection draft belongs to THIS
        # attempt.  Any exit between prepare and canonical observation —
        # cancellation of the provider await, a failed L1 acceptance, an
        # owner-side exception — must close only this round; the owner-match
        # check in the guard keeps it from ever touching a successor round
        # or rolling back an accept that already committed.
        try:
            outcome = await self.stream_llm_request_async(
                continuation, llm_runtime, request, projection_pack=projection_pack,
                on_submitted=on_submitted)
            # F4 (review af51d74): the AcceptedContribution boundary.  Owner-side
            # postprocessing (finalization_only) may still discard the provider's
            # tool-call/empty answer and replace it with fallback text; the
            # projection may only freeze what canonical L1 actually keeps.
            outcome = await self._finalize_accepted_contribution(continuation, outcome)
            # Publish the final assistant contribution and its batch together.
            # Until here streaming tool calls are drafts, including a terminal
            # frame still awaiting iterator/channel teardown.
            if outcome.tool_calls:
                self._upsert_l1_assistant(continuation, outcome.response.message)
                continuation.accepted_tool_batch_open = True
                continuation.pending_assistant_tool_text = str(outcome.text or "")
                continuation.pending_tool_call_batch = list(outcome.tool_calls)
                continuation.pending_tool_results = []
            # F2: observe only what the transport receipt authorizes.
            self.projection.accept(projection_pack, continuation, outcome)
        finally:
            # Normal exits closed the round inside the observation; this only
            # bites for cancelled/failed rounds, and only while the lineage
            # still holds THIS attempt's draft.
            self.projection.abandon(projection_pack)
        self._debug_log_outcome(continuation, outcome)
        if self.include_live_execution_context:
            self.context.execution_runtime.model_response_received(continuation)
        preferred_endpoint_id = str(outcome.preferred_endpoint_id or "").strip() or None
        preferred_model_id = str(outcome.preferred_model_id or "").strip() or None
        continuation.preferred_llm_endpoint_id = preferred_endpoint_id or continuation.preferred_llm_endpoint_id
        continuation.preferred_llm_model_id = preferred_model_id or continuation.preferred_llm_model_id
        continuation.last_response_mode = self.infer_response_mode(
            outcome,
            used_tools=bool(continuation.tool_observations),
        )
        if not outcome.tool_calls:
            await self._checkpoint_resident_async(continuation.turn_id, "round", continuation)
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
                or "llm_runtime"
            )
            failure_kind = str(
                failure_metadata.get("failure_kind")
                or ("local_state" if local_state_failure else "provider_failure")
            )
            # The LLM owner has already rendered the failure. Private metadata
            # is diagnostic evidence, never reply text or a second model prompt.
            blocker_text = str(outcome.text or "LLM invocation failed.")
            try:
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
                            **dict(failure_metadata.get("failure_evidence") or {}),
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
                feedback_text = self._render_failure_feedback_text(failure_result.user_feedback)
            except Exception as exc:
                feedback_text = "Blocker: " + blocker_text + "\nFailure recovery also failed: " + exception_reason(exc)
            outcome = self._generation_result_from_text(
                feedback_text,
                finish_reason=LLMFinishReason.FALLBACK,
                response_mode=LLMResponseMode.CHAT,
            )
        return EffectResult(status=RuntimeStatus.OK, payload=outcome)

    async def _finalize_accepted_contribution(self, continuation: Any, outcome: Any) -> Any:
        """F4 (review af51d74): the explicit AcceptedContribution boundary.

        Owner-side postprocessing that can still REPLACE the provider's
        contribution — finalization_only discarding a tool-call/empty
        answer for fallback text — runs here, BEFORE any projection commit.
        What this boundary returns is what canonical L1 keeps, and the only
        contribution the projection may freeze.
        """

        if not continuation.finalization_only:
            return outcome
        if outcome.finish_reason != LLMFinishReason.COMPACT_REQUIRED:
            continuation.finalization_attempted = True
        if outcome.finish_reason != LLMFinishReason.COMPACT_REQUIRED and (
            outcome.tool_calls or not outcome.text.strip()
        ):
            self._discard_l1_assistant(
                continuation,
                outcome.response.message.message_id,
            )
            # The send proof survives replacement. Replay belongs solely
            # to the accepted message, so there is no parallel response
            # payload on the receipt to revoke.
            send_receipt = outcome.projection_receipt
            outcome = self._generation_result_from_text(
                self.fallback_final_reply(continuation),
                finish_reason=LLMFinishReason.FALLBACK,
                response_mode=LLMResponseMode.CHAT,
            )
            if send_receipt is not None:
                outcome = replace(outcome, projection_receipt=send_receipt)
            self._upsert_l1_assistant(
                continuation,
                outcome.response.message,
            )
        return outcome

    def _resolve_llm_tools(self, continuation, tools_override: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        if tools_override is not None:
            return list(tools_override)
        # Finalization revokes execution authority, not the input contract.
        # Keep schemas and tool_choice stable; the chronological directive and
        # _finalize_generation_outcome close the turn even if tools are returned.
        return self._build_llm_tool_contracts()

    @_dispatch_effect.register(ToolCallEffect)
    async def _handle_tool_call(self, effect, continuation):
        self._ensure_not_interrupted(continuation)
        execution_call = effect.tool_call
        if execution_call is None and continuation.pending_tool_call_batch:
            pending_index = len(continuation.pending_tool_results)
            if 0 <= pending_index < len(continuation.pending_tool_call_batch):
                execution_call = continuation.pending_tool_call_batch[pending_index]
        if execution_call is None:
            raise ValueError("ToolCallEffect requires a tool call")
        tool_budget = self._build_tool_call_budget(continuation, execution_call=execution_call)
        self._log_tool_call_start(continuation, execution_call)
        observed_name = execution_call.name
        if observed_name == "call_tool" and isinstance(execution_call.args, dict):
            observed_name = str(execution_call.args.get("name") or observed_name)
        tool_event = {
            "turn_id": continuation.turn_id,
            "call_id": execution_call.call_id,
            "tool_name": execution_call.name,
            "subsystem": "execution", "component": observed_name,
        }
        self.context.core_event_bus.emit("turn.tool_call_before", tool_event)
        try:
            if self._execute_tool_async is not None:
                tool_result = await self._execute_tool_async(
                    execution_call,
                    allow_tools=continuation.accepted_tool_batch_open or not continuation.finalization_only,
                    budget=tool_budget,
                    turn_id=continuation.turn_id,
                )
            else:
                tool_result = await self.context.execution_runtime.execute_tool_async(execution_call, allow_tools=continuation.accepted_tool_batch_open or not continuation.finalization_only, budget=tool_budget, turn_id=continuation.turn_id)
        except Exception as exc:
            self._log_tool_call_exception(continuation, execution_call, exc)
            tool_result = self.context.execution_runtime.tool_call_failed_result(execution_call, exc)
        if not tool_result.ok:
            self.context.core_event_bus.emit(TURN_TOOL_CALL_FAILED, {**tool_event, "ok": False})
        if not continuation.stop_after_tool_batch and self._should_enter_failure_flow_for_tool_result(tool_result):
            try:
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
                feedback_text = self._render_failure_feedback_text(failure_result.user_feedback)
                failure_metadata = {
                    "failure_status": failure_result.verification.status,
                    "report_id": failure_result.report.report_id if failure_result.report is not None else None,
                }
            except Exception as exc:
                # Recovery is additional evidence; its own failure must not
                # discard the tool result that was already obtained.
                diagnostic = exception_reason(exc)
                feedback_text = "Failure recovery also failed:\n" + diagnostic
                failure_metadata = {"failure_recovery_error": diagnostic}
            if not continuation.stop_after_tool_batch:
                tool_result = ToolExecutionResult(
                    name=execution_call.name,
                    ok=False,
                    text=f"{tool_result.text}\n\n{feedback_text}",
                    structured={
                        **dict(tool_result.structured or {}),
                        **failure_metadata,
                    },
                    call_id=execution_call.call_id,
                    llm_text=f"{tool_result.llm_text or tool_result.text}\n\n{feedback_text}",
                    snapshot_refs=tool_result.snapshot_refs,
                    replay_result_ref=tool_result.replay_result_ref,
                    status=tool_result.status,
                    invocation_result=tool_result.invocation_result,
                    context_delivery=tool_result.context_delivery,
                    context_messages=tool_result.context_messages,
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
                self.context.execution_runtime.stagnation_payload(execution_call, tool_result)
            ),
        )
        continuation.tool_batch_count += 1
        verdict = self.turn_manager.guard.observe_batch(continuation.turn_id, [record])
        if verdict.recommended_action == GuardAction.TERMINATE_TOOL_LOOP:
            LOGGER.info("tool loop finalization turn=%s tool=%s status=%s sequence=%d",
                        continuation.turn_id, execution_call.name, verdict.status, record.sequence)
            continuation.finalization_only = True
        if (execution_call.name == "search_tools"
                and self.turn_manager.guard.last_result_repeated(continuation.turn_id)):
            # The guard fingerprints the owner's observation before this hint.
            # Advice must not turn a third unchanged search into fresh evidence.
            tool_result = replace(tool_result, llm_text=(
                tool_result.llm_text + "\n\nDiscovery repetition: the same search arguments "
                "returned the same result consecutively in this turn; this adds no new information. "
                "Do not repeat this unchanged search. Reuse the returned directory or contract; "
                "if unresolved, change the query/domain/filters meaningfully or explain that "
                "no suitable tool was found and any uncertainty."
            ))
        self.context.turn_event_bus.emit("turn.tool_call_after", {
            **tool_event, "ok": tool_result.ok,
        })
        if continuation.pending_tool_call_batch:
            continuation.pending_tool_results.append(tool_result)
            await self._append_l1_tool_result_async(
                continuation,
                execution_call,
                tool_result,
            )
            if len(continuation.pending_tool_results) >= len(continuation.pending_tool_call_batch):
                await self._append_l1_tool_context_messages_async(
                    continuation,
                    tuple(continuation.pending_tool_call_batch),
                    tuple(continuation.pending_tool_results),
                )
                continuation.pending_assistant_tool_text = ""
                continuation.pending_tool_call_batch = []
                continuation.pending_tool_results = []
                if self._after_tool_batch is not None:
                    await self._after_tool_batch(continuation)
                await self._checkpoint_resident_async(continuation.turn_id, "round", continuation)
                if self.include_live_execution_context:
                    self.context.execution_runtime.model_response_received(continuation)
                continuation.accepted_tool_batch_open = False
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
        Independent tagged control delivery uses ``channel_event`` with tag/payload
        and optional fallback text, through the same ordered delivery path.
        Core is the only actor that touches the output port; the tool itself
        never knows the envelope or the channel. Only channel turns carry an
        envelope, so service/bunshin turns silently ignore echo declarations —
        there is physically no path to send "to the LLM itself".
        """
        structured = dict(tool_result.structured or {})
        event = structured.get("channel_event")
        if isinstance(event, dict):
            # A control event does not depend on the size/existence of visual echo.
            declaration = event
            tag = str(event.get("tag") or "").strip()
            if not tag:
                return
            markdown = str(event.get("text") or "").strip()[:self._ECHO_MARKDOWN_MAX_CHARS]
        else:
            declaration = structured.get("echo")
            if not isinstance(declaration, dict):
                return
            markdown = str(declaration.get("markdown") or "").strip()
            if not markdown or len(markdown) > self._ECHO_MARKDOWN_MAX_CHARS:
                return
            tag = str(declaration.get("tag") or "").strip() or None
        raw_payload = declaration.get("payload")
        payload = dict(raw_payload) if isinstance(raw_payload, dict) else {}
        message = ChannelMessage(text=markdown, tag=tag, payload=payload)
        dedupe_key = (
            str(declaration.get("dedupe_key") or "").strip()
            or f"{tool_call.name}:{tool_call.call_id}"
        )
        if not dedupe_key:
            return
        if dedupe_key in continuation.echoed_keys:
            return
        if continuation.delivery_binding is None:
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

    def _log_tool_call_start(self, continuation, tool_call: ToolCallIR) -> None:
        if not LOGGER.isEnabledFor(logging.DEBUG):
            return
        LOGGER.debug(
            "[tool call] turn_id=%s name=%s call_id=%s args=%s",
            continuation.turn_id,
            tool_call.name,
            tool_call.call_id,
            self._log_preview(tool_call.args, max_chars=1200),
        )

    def _log_tool_call_result(self, continuation, tool_call: ToolCallIR, tool_result: ToolExecutionResult) -> None:
        if not LOGGER.isEnabledFor(logging.DEBUG):
            return
        LOGGER.debug(
            "[tool result] turn_id=%s name=%s call_id=%s ok=%s status=%s text=%s",
            continuation.turn_id,
            tool_call.name,
            tool_call.call_id,
            tool_result.ok,
            tool_result.status,
            self._log_preview(tool_result.text or tool_result.llm_text, max_chars=1200),
        )

    def _log_tool_call_exception(self, continuation, tool_call: ToolCallIR, exc: Exception) -> None:
        if not LOGGER.isEnabledFor(logging.DEBUG):
            return
        LOGGER.debug(
            "[tool result] turn_id=%s name=%s call_id=%s ok=False exception=%s text=%s",
            continuation.turn_id,
            tool_call.name,
            tool_call.call_id,
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
                history = self.context.port_registry.get(RESIDENT_HISTORY.name)
                if history is not None:
                    history.record_reply(str(continuation.turn_id), text)
            self._debug_log_reply(continuation, effect.text)
            return EffectResult(status=RuntimeStatus.QUEUED, text=effect.text)
        if not effect.terminal:
            reply_target = dict(binding.response_handle.reply_target)
            reply_target["_pal_turn_continues"] = True
            binding = replace(
                binding,
                response_handle=replace(
                    binding.response_handle,
                    reply_target=reply_target,
                ),
            )
        reply_id = await self._call_output_port_async(output_port.queue_reply, binding, message)
        text = str(message.text or "").strip()
        if text:
            continuation.emitted_reply_texts.append(text)
            history = self.context.port_registry.get(RESIDENT_HISTORY.name)
            if history is not None:
                history.record_reply(str(continuation.turn_id), text)
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
            output_port.queue_stream_update,
            binding,
            effect.update,
        )
        history = self.context.port_registry.get(RESIDENT_HISTORY.name)
        if history is not None:
            history.record_stream(str(continuation.turn_id), str(effect.update.kind), str(effect.update.text or ""))
        return EffectResult(status=RuntimeStatus.QUEUED, payload={"update_id": update_id})

    def _agent_output_port(self):
        return self.context.port_registry.get("agent_io:output") or self.context.port_registry.get("channel:channel")

    def _channel_supports_stream_delivery(self, continuation: Any) -> bool:
        output_port = self._agent_output_port()
        binding = continuation.delivery_binding
        if output_port is None or binding is None:
            return False
        return output_port.supports_stream_delivery(binding)

    async def _call_output_port_async(self, method: Callable[..., Any], *args, **kwargs):
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
        *,
        projection_pack: tuple | None = None,
        on_submitted: Any = None,
    ) -> LLMGenerationResult:
        final_response: LLMResponseIR | None = None
        completion: LLMGenerationResult | None = None
        stream = llm_runtime.generate
        continuation.channel_stream_active = self._channel_supports_stream_delivery(
            continuation
        )
        if projection_pack is not None:
            iterator = stream(
                request,
                projection=projection_pack[0],
                projection_binding=projection_pack[1],
                projection_attempt_id=projection_pack[2]["attempt"].attempt_id,
                generation_plan=projection_pack[2]["plan"],
                on_submitted=on_submitted,
            ).__aiter__()
        else:
            iterator = stream(request, on_submitted=on_submitted).__aiter__()
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
                completion = update.completion
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
            try:
                await iterator.aclose()
            except (asyncio.CancelledError, RuntimeError):
                pass
        if final_response is None:
            return self._generation_result_from_text(
                "LLM stream completed without a response.",
                finish_reason=LLMFinishReason.ERROR,
            )
        return completion if completion is not None else LLMGenerationResult(response=final_response)

    def _llm_wait_status_schedule(self) -> tuple[float, ...]:
        raw = self._config.llm_wait_status_seconds
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
            output_port.queue_status,
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
            self._discard_l1_assistant(
                continuation,
                update.response.message.message_id,
            )
        else:
            message = update.response.message
            if message.tool_calls:
                message = replace(message, state=MessageState.IN_PROGRESS)
            self._upsert_l1_assistant(continuation, message)
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
        if channel_update is not None and continuation.channel_stream_active:
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
        commit_scoped_instruction: bool = False,
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
        if assembly_context.turn_kind == "proactive_trigger":
            logical_scope_id = f"pal:proactive:{continuation.turn_id}"
        if not logical_scope_id:
            logical_scope_id = (
                f"bunshin:{assembly_context.work_order_id or continuation.turn_id}"
                if assembly_context.core_mode == "bunshin"
                else "pal:resident"
            )
        artifact_scope_key = str(
            metadata.get("artifact_scope_key")
            or ("pal:resident" if assembly_context.turn_kind == "proactive_trigger" else logical_scope_id)
        ).strip()
        metadata["artifact_scope_key"] = artifact_scope_key
        metadata["artifact_turn_id"] = continuation.turn_id
        metadata["turn_id"] = continuation.turn_id
        metadata["task_id"] = str(assembly_context.task_id or "")
        metadata["llm_round_index"] = continuation.llm_round_index
        if "cache_policy_snapshot" in continuation.turn_settings_snapshot:
            metadata["cache_policy_snapshot"] = continuation.turn_settings_snapshot["cache_policy_snapshot"]
        metadata["prompt_cache_scope_id"] = logical_scope_id
        metadata["llm_capabilities"] = self._resolve_llm_capabilities(continuation)
        memory_service = self.memory.optional
        active_turn = None
        if memory_service is not None:
            try:
                active_turn = memory_service.active_l1_turn(continuation.turn_id)
            except Exception as exc:
                active_turn = None
                metadata["memory_context_errors"] = {
                    **metadata.get("memory_context_errors", {}),
                    "active_turn": exception_summary(exc),
                }
        if active_turn is not None:
            metadata["active_l1_owns_primary_input"] = True
        if assembly_context.turn_kind != "failure":
            try:
                memory_service = self.memory.current
            except KeyError:
                memory_service = None
            if memory_service is not None and "memory_pack" not in metadata:
                try:
                    metadata["memory_pack"] = memory_service.build_pack(
                        MemoryPackRequest(
                            turn_kind=assembly_context.turn_kind,
                            include_l1_recent_context=False,
                            task_id=assembly_context.task_id,
                            work_order_id=assembly_context.work_order_id,
                            active_input_id=assembly_context.event.event_id
                                if assembly_context.event is not None else None,
                        )
                    )
                except Exception as exc:
                    # Preserve the failure for this request instead of silently
                    # retrying in PromptCompiler and losing the diagnosis.
                    metadata["memory_pack"] = None
                    metadata["memory_context_errors"] = {
                        **metadata.get("memory_context_errors", {}),
                        "memory_pack": exception_summary(exc),
                    }
        metadata["typed_l1_projection"] = True
        today = today_for_timezone("UTC")
        date_pin = None
        if (memory_service is not None and active_turn is not None
                and logical_scope_id == "pal:resident" and assembly_context.core_mode != "bunshin"):
            previous = prompt_date_pin(memory_service)
            cold = False
            if (previous and previous["date"] != today and self.borrowed_prompt_date is None
                    and not active_turn.metadata.get("prompt_context_state", {}).get("initialized")):
                llm = self.llm.optional
                if llm is not None:
                    try:
                        snapshot = llm.prompt_cache_reminder_snapshot()
                        route = metadata["llm_capabilities"]
                        cold = prefix_refresh_likely_cold(snapshot,
                            endpoint_id=str(route.get("endpoint_id") or ""),
                            model_id=str(route.get("model_id") or ""))
                    except Exception:
                        LOGGER.debug("Date refresh cache estimate unavailable", exc_info=True)
            date_pin = select_prompt_date_pin(memory_service, active_turn, today,
                cold=cold, borrowed_date=self.borrowed_prompt_date)
            metadata["prompt_utc_date"] = date_pin["date"]
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
        context_committed = False
        facts = metadata.get("llm_capabilities", {})
        turn_scoped = bool(facts.get("supports_turn_scoped_instructions"))
        request_instructions = not turn_scoped and bool(facts.get("supports_request_instructions")
            or facts.get("supports_response_instructions") or facts.get("supports_suffix_instructions")
            or facts.get("supports_interaction_instructions"))
        reminder_text = str(prompt.metadata.get("runtime_reminder_text") or "").strip()
        # A restored active legacy turn keeps its original delivery policy.
        if active_turn is not None:
            sources = active_turn.metadata.get("prompt_context_state", {}).get("sources", {})
            if any(str(key).startswith("runtime:") for key in sources):
                request_instructions = turn_scoped = False
        candidates = list(prompt.metadata["context_candidates"])
        if request_instructions or turn_scoped:
            candidates = [c for c in candidates if not str(c.get("key", "")).startswith("runtime:")]
        if active_turn is not None:
            selected_turns = tuple(metadata["memory_pack"].l1_turns) if metadata.get("memory_pack") is not None else ()
            frozen, active_turn = memory_service.prepare_l1_prompt_context(
                continuation.turn_id, stable_messages, candidates,
                boundary=json.dumps([(metadata["memory_pack"].metadata if metadata.get("memory_pack") is not None else {}).get("continuity_id", ""),
                                     continuation.preferred_llm_endpoint_id, continuation.preferred_llm_model_id]),
                date_pin=date_pin if "current_date" in prompt.metadata.get("developer_sections", ()) else None,
                today=today,
                checklist=thaw_json(prompt.metadata.get("history_states", {}).get("checklist_state")),
                settled_turns=selected_turns)
            stable_messages = list(frozen)
            contextual_messages = []
            context_committed = True
        settled_messages: list[LLMMessageIR] = []
        memory_pack = metadata.get("memory_pack")
        context_view = None
        if memory_service is not None and "active_turn" not in metadata.get("memory_context_errors", {}):
            view_builder = memory_service.l1_context_view
            selected_turns = tuple(memory_pack.l1_turns) if memory_pack is not None else ()
            context_view = view_builder(continuation.turn_id, selected_turns)
            self.context.execution_runtime.prepare_prompt_context(memory_service, continuation,
                context_view=context_view, include_live=self.include_live_execution_context)
            active_turn = memory_service.active_l1_turn(continuation.turn_id)
            context_view = view_builder(continuation.turn_id, selected_turns)
        scoped_message = None
        scoped_message_id = ""
        if turn_scoped and reminder_text:
            # Append AFTER live tool/user context has been materialized. A
            # following user/tool_result would otherwise clear the reminder
            # before the provider ever sees it. Preflight only estimates it.
            previous = active_turn.messages if active_turn is not None else tuple(contextual_messages)
            legal = bool(previous and previous[-1].role in {MessageRole.USER, MessageRole.TOOL, MessageRole.DEVELOPER})
            round_index = continuation.llm_round_index if commit_scoped_instruction else continuation.llm_round_index + 1
            scoped_message_id = f"turn-instruction:{continuation.turn_id}:{round_index}"
            scoped_message = LLMMessageIR(MessageRole.DEVELOPER, (TextPartIR(reminder_text),),
                message_id=scoped_message_id, semantic_kind="pal_prompt_context",
                prompt_region=PromptRegionIR.ACTIVE_DYNAMIC,
                instruction_scope=InstructionScopeIR.NEXT_USER_MESSAGE if legal else InstructionScopeIR.PERSISTENT,
                metadata={"pal_authored": True, "scope_turn_id": continuation.turn_id,
                          "context_key": "runtime:round-instruction"})
            if active_turn is not None and commit_scoped_instruction:
                existing = next((m for m in active_turn.messages if m.message_id == scoped_message_id), None)
                if existing is None:
                    active_turn = memory_service.append_l1_prompt_contexts(continuation.turn_id, (scoped_message,),
                        thaw_json(active_turn.metadata.get("prompt_context_state", {})), expected_revision=active_turn.revision)
                scoped_message = None
                if context_view is not None:
                    context_view = memory_service.l1_context_view(continuation.turn_id, selected_turns)
        for settled_turn in (memory_pack.l1_turns if memory_pack is not None else ()):
            from pal.memory.context_view import projected_messages
            if context_view is not None:
                original = list(context_view.turns[settled_turn.turn_id].messages.values())
            else:
                original = list(projected_messages(settled_turn, settled=True))
            projected = self._project_messages_for_prompt(
                original,
                turn_id=settled_turn.turn_id,
                artifact_scope_key=artifact_scope_key,
                capabilities=dict(metadata.get("llm_capabilities") or {}),
            )
            settled_messages.extend(
                replace(message, prompt_region=PromptRegionIR.SETTLED_HISTORY)
                for message in projected
            )
        active_messages: list[LLMMessageIR] = []
        if active_turn is not None:
            from pal.memory.prompt_context import projected_context
            active_messages = (list(context_view.turns[active_turn.turn_id].messages.values())
                               if context_view is not None else projected_context(active_turn))
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
                        PromptRegionIR.ACTIVE_DYNAMIC if message.message_id == scoped_message_id else
                        PromptRegionIR.ACTIVE_INPUT
                        if message.role == MessageRole.USER and
                        (index == 0 or message.semantic_kind in {"user_request", "user_interjection"})
                        else PromptRegionIR.ACTIVE_HISTORY
                    ),
                )
                for index, message in enumerate(active_messages)
            ]
        runtime_reminder = "" if context_committed or request_instructions or turn_scoped else reminder_text
        tail_messages = (
            [
                LLMMessageIR(
                    role=MessageRole.DEVELOPER,
                    parts=(TextPartIR(runtime_reminder),),
                    semantic_kind="runtime_reminder",
                    prompt_region=PromptRegionIR.ACTIVE_DYNAMIC,
                )
            ]
            if runtime_reminder
            else []
        )
        # ACTIVE_HISTORY is immutable within this active turn and may advance
        # the same-turn cache frontier. Contextual compiler output and runtime
        # reminders remain ACTIVE_DYNAMIC after every reusable checkpoint.
        prompt_messages = [
            *stable_messages,
            *settled_messages,
            *active_messages,
            *contextual_messages,
            *tail_messages,
            *((scoped_message,) if scoped_message is not None else ()),
        ]
        continuity_id = memory_pack.metadata.get("continuity_id", "") if memory_pack is not None else ""
        if memory_service is not None and continuity_id:
            prompt_messages = memory_service.project_continuity(prompt_messages)
        metadata = dict(prompt.metadata)
        metadata["continuity_id"] = continuity_id
        metadata["history_visibility"] = "active_turn" if assembly_context.turn_kind == "proactive_trigger" else "resident"
        if snapshot_think_levels:
            metadata["think_levels"] = snapshot_think_levels
        metadata["prompt_log_enabled"] = bool(continuation.turn_settings_snapshot.get("prompt_log_enabled"))
        # PromptCompiler intentionally emits only provider-facing prompt
        # metadata.  Logical cache/artifact ownership is executor-owned, so
        # restore it after compilation instead of allowing Bunshin requests to
        # silently fall back to the resident Pal scope.
        metadata["artifact_scope_key"] = artifact_scope_key
        metadata["artifact_turn_id"] = continuation.turn_id
        metadata["turn_id"] = continuation.turn_id
        metadata["task_id"] = str(assembly_context.task_id or "")
        metadata["llm_round_index"] = continuation.llm_round_index
        if "cache_policy_snapshot" in continuation.turn_settings_snapshot:
            metadata["cache_policy_snapshot"] = continuation.turn_settings_snapshot["cache_policy_snapshot"]
        metadata["prompt_cache_scope_id"] = logical_scope_id
        metadata["llm_capabilities"] = self._resolve_llm_capabilities(continuation)
        prompt = replace(
            prompt,
            messages=tuple(prompt_messages),
            tools=tuple(tool_definition_ir_from_dict(tool) for tool in tools) if tools is not None else prompt.tools,
            logical_scope_id=logical_scope_id,
            metadata=metadata,
            request_instructions=reminder_text if request_instructions else "",
        )
        return prompt

    def _resolve_llm_capabilities(self, continuation) -> dict[str, Any]:
        return self._resolve_llm_capabilities_for_endpoint(
            continuation.preferred_llm_endpoint_id
        )

    def _resolve_llm_capabilities_for_endpoint(
        self,
        preferred_endpoint_id: str | None,
    ) -> dict[str, Any]:
        llm_runtime = self.llm.optional
        if llm_runtime is None:
            return {}
        facts = llm_runtime.metadata(preferred_endpoint_id=preferred_endpoint_id)
        return facts.to_payload()

    @staticmethod
    def _ensure_not_interrupted(continuation) -> None:
        if continuation.interrupted:
            raise asyncio.CancelledError(continuation.interrupt_reason or "interrupted")

    def _build_tool_call_budget(self, continuation, *, execution_call=None) -> ToolCallBudget:
        cfg = self._config
        token_limit = cfg.max_tool_result_tokens
        if execution_call is not None and execution_call.name in {"op_exec_shell", "run_shell"}:
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
            if message.instruction_scope != InstructionScopeIR.NEXT_USER_MESSAGE
            or (capabilities or {}).get("supports_turn_scoped_instructions")
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
        exposure = None
        if manager is not None:
            try:
                exposure = manager.select_prompt_exposure(
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
                source = inline.source_url
                if not source:
                    source = manager.to_data_url(inline.representation_id) or ""
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
        breakdown = advice.breakdown or {}
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
        return result.llm_text

    # ── response / temperature helpers ───────────────────────────────────

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

    def _upsert_l1_assistant(
        self,
        continuation: Any,
        message: LLMMessageIR,
    ) -> None:
        memory_service = self.memory.current
        if not message.semantic_kind:
            message = replace(
                message,
                semantic_kind=(
                    L1MessageKind.ASSISTANT_TOOL_CALL
                    if message.tool_calls
                    else L1MessageKind.ASSISTANT_REPLY
                ),
            )
        memory_service.stream_l1_assistant(str(continuation.turn_id), message)

    def _discard_l1_assistant(
        self,
        continuation: Any,
        message_id: str,
    ) -> None:
        memory_service = self.memory.current
        active = memory_service.active_l1_turn(str(continuation.turn_id))
        if active is None or not any(
            item.role == MessageRole.ASSISTANT
            and str(item.message_id or "") == str(message_id)
            for item in active.messages
        ):
            return
        memory_service.discard_l1_assistant(str(continuation.turn_id), str(message_id))

    def select_turn_temperature(self, response_mode: str) -> float:
        base_by_mode = {
            LLMResponseMode.CHAT: 0.7,
            LLMResponseMode.OPERATIONAL: 0.2,
            LLMResponseMode.REVIEW: 0.1,
        }
        value = base_by_mode.get(response_mode, 0.3)
        return max(0.0, min(1.0, round(value, 2)))

    # ── L1 working-set settlement ───────────────────────────────────────

    def _begin_l1_turn(
        self,
        continuation: TurnContinuation,
        assembly_context: PromptAssemblyContext,
    ) -> None:
        memory_service = self.memory.current
        self.context.execution_runtime.bind_result_history(memory_service)
        event = assembly_context.event
        event_payload = event.payload if event is not None else None
        user_message = event_payload if isinstance(event_payload, LLMMessageIR) else None
        text = "" if user_message is not None else extract_text_from_payload(event_payload).strip()
        if not text:
            text = str(assembly_context.metadata.get("proactive_input") or "").strip()
        turn_metadata = {"_pal_input_id": self._active_input_id(continuation, assembly_context)}
        if self.context.port_registry.get(RESIDENT_HISTORY.name) is not None:
            origin = "proactive" if (assembly_context.turn_kind == "proactive_trigger"
                or assembly_context.metadata.get("proactive_input")) else "user"
            if user_message is None and text:
                user_message = LLMMessageIR(role=MessageRole.USER, parts=(TextPartIR(text),),
                                           semantic_kind="user_request")
                text = ""
            if user_message is not None:
                user_message = replace(user_message, semantic_kind=user_message.semantic_kind or "user_request",
                    metadata={**thaw_json(user_message.metadata),
                    "conversation_origin": origin,
                    "conversation_timestamp": event.created_at if event is not None else datetime.now(timezone.utc).isoformat()})
            turn_metadata.update(conversation_tracking=True, conversation_input_kind=origin)
        memory_service.begin_l1_turn(
            str(continuation.turn_id),
            user_text=text,
            user_message=user_message,
            metadata=turn_metadata,
        )

    async def _append_l1_tool_result_async(
        self,
        continuation: Any,
        call: ToolCallIR,
        result: ToolExecutionResult,
    ) -> None:
        memory_service = self.memory.optional
        if memory_service is None:
            self._discard_uncommitted_tool_delivery(str(continuation.turn_id), call.call_id)
            return
        execution = self.context.execution_runtime
        execution.bind_result_history(memory_service)
        content = self._render_tool_result_content(call, result)
        turn_id = str(continuation.turn_id)
        previous = memory_service.active_l1_turn(
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
                snapshot_refs=result.snapshot_refs,
        )
        try:
            memory_service.append_l1_tool_result(turn_id, tool_result)
        except Exception:
            self._discard_uncommitted_tool_delivery(
                turn_id,
                call.call_id,
            )
            raise
        delivery = result.context_delivery
        if delivery is not None:
            try:
                execution.commit_tool_delivery(
                    turn_id=str(continuation.turn_id),
                    context_delivery=dict(delivery),
                    result_id=call.call_id,
                )
            except Exception:
                if previous is None:
                    raise RuntimeError(
                        "tool delivery commit failed and L1 cannot be rolled back"
                    )
                memory_service.rollback_l1_tool_result(
                    turn_id,
                    previous=previous,
                    call_id=call.call_id,
                )
                self._discard_uncommitted_tool_delivery(
                    turn_id,
                    call.call_id,
                )
                raise

        snapshots = execution.result_snapshots
        scope = execution.logical_context_for_turn(turn_id)
        snapshots.finish_delivery(lifetime=scope.execution_lifetime_id, call_id=call.call_id)
        snapshots.finish_references(result.snapshot_refs)
        execution.observe_tool_delivery(call, result)
        self.context.core_event_bus.emit(TURN_TOOL_RESULT_COMMITTED, {
            "turn_id": turn_id, "call_id": call.call_id,
            "complete": bool(result.ok and not result.snapshot_refs
                             and not (isinstance(result.invocation_result, CompleteResult)
                                      and result.invocation_result.output_error)),
        })
        await execution.acknowledge_tool_result_async(call.call_id, turn_id)

    async def _append_l1_tool_context_messages_async(
        self,
        continuation: Any,
        calls: tuple[ToolCallIR, ...],
        results: tuple[ToolExecutionResult, ...],
    ) -> None:
        """Append tool-declared user context after the complete tool-result batch."""

        memory_service = self.memory.optional
        if memory_service is None:
            return
        messages: list[LLMMessageIR] = []
        for call, result in zip(calls, results, strict=True):
            for index, context_message in enumerate(result.context_messages):
                messages.append(
                    LLMMessageIR(
                        role=MessageRole.USER,
                        parts=(
                            TextPartIR(context_message.content),
                            *(ArtifactRefPartIR(artifact_id=artifact_id) for artifact_id in context_message.artifact_ids),
                        ),
                        message_id=f"tool-context:{call.call_id}:{index}",
                        semantic_kind=context_message.semantic_kind,
                        metadata={
                            **dict(context_message.metadata),
                            "source_tool_call_id": call.call_id,
                            "source_tool_name": call.name,
                        },
                    )
                )
        if messages:
            memory_service.append_l1_user_contexts(str(continuation.turn_id), tuple(messages))

    def _discard_uncommitted_tool_delivery(
        self,
        turn_id: str,
        result_ref: str,
    ) -> None:
        self.context.execution_runtime.discard_uncommitted_tool_delivery(
            turn_id=str(turn_id), result_ref=str(result_ref or ""))

    @staticmethod
    def _active_input_id(
        continuation: Any,
        assembly_context: Any | None,
    ) -> str:
        event = assembly_context.event if assembly_context is not None else None
        if event is None:
            event = continuation.opening_event
        return (
            str(event.event_id if event is not None else "").strip()
            or str(continuation.turn_id or "").strip()
        )

    # ── post-turn commit ─────────────────────────────────────────────────

    async def schedule_post_turn_commit_async(self, outcome) -> Any:
        self.context.execution_runtime.result_snapshots.finish_turn(str(outcome.commit_payload.turn_id))
        llm = self.llm.optional
        if llm is not None:
            llm.end_prompt_cache_turn(str(outcome.commit_payload.turn_id))
        memory_service = self.memory.optional
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
                memory_service.tick_heat()
            except Exception:
                pass
        self._reap_expired_artifacts()
        await self._checkpoint_resident_async(
            outcome.commit_payload.turn_id, "turn")
        return result

    def _reap_expired_artifacts(self) -> None:
        service = self.context.port_registry.get("artifact:artifact")
        if service is None:
            return
        try:
            service.reap_expired()
        except Exception as exc:
            self.state.diagnostics.append(
                {
                    "kind": "artifact.lifecycle.reap_failed",
                    "status": RuntimeStatus.ERROR,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )


    # ── shared compaction engine ─────────────────────────────────────────



    def _llm_failure_context(self, continuation) -> dict[str, Any]:
        """Freeze diagnostic R before submission advances the live L/R cut.

        This snapshot is never sent to the provider or installed back into L1.
        The endpoint writes it only if this request fails.
        """
        context: dict[str, Any] = {
            "turn_id": str(continuation.turn_id),
            "llm_round_index": continuation.llm_round_index,
        }
        memory = self.memory.optional
        root = memory.history_root if memory is not None else None
        if root is not None:
            cut = root.cut
            context.update(
                history_incarnation=root.incarnation,
                history_cut={
                    "turn_count": cut.turn_count,
                    "intra_messages": cut.intra_messages,
                    "revision": cut.revision,
                    "cut_id": cut.cut_id,
                },
                right_messages=[message_to_payload(m) for m in root.right_messages()],
            )
        return context

    def _history_submission_observer(self, request: LLMRequestIR):
        memory = self.memory.optional
        root = (memory.history_root if memory is not None else None)
        if root is None:
            return None
        prepared = root.prepare_submission(m.message_id for m in request.messages)

        def submitted(receipt):
            # Runs on the owner loop, never the SDK worker. The root fences
            # reset, retries and mutations after this request's snapshot.
            if root.commit_submission(prepared, receipt.message_ids):
                history = self.context.port_registry.get(RESIDENT_HISTORY.name)
                if history is not None:
                    history.submitted()

        return submitted
