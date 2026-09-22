from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from copy import deepcopy
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Callable, Protocol, Sequence
from uuid import uuid4

from pal.llm.contracts import (
    LLMPreflightRequest,
)
from pal.llm.conversions import request_ir_from_prompt
from pal.llm.ir import (
    LLMMessageIR,
    LLMRequestIR,
    MessageRole,
    PromptRegionIR,
    TextPartIR,
)
from pal.memory.contracts import (
    L1MessageKind,
    L1TranscriptMessage,
    L2Entry,
    MemoryCompactResult,
)
from pal.shared import LLMFinishReason, LLMPreflightStatus

_LOGGER = logging.getLogger(__name__)

MAX_COMPACTION_VISIBLE_TOKENS = 20_000


def compaction_visible_token_limit(snapshot: "CompactionSnapshot") -> int:
    """Bound a checkpoint to half the selected endpoint's input budget."""

    target = max(0, int(snapshot.target_input_budget or 0))
    if target <= 0:
        return MAX_COMPACTION_VISIBLE_TOKENS
    return max(1, min(MAX_COMPACTION_VISIBLE_TOKENS, target // 2))


class CompactionClockKind(StrEnum):
    USER_TURN = "user_turn"
    LLM_ROUND = "llm_round"


@dataclass(frozen=True)
class CompactionUnit:
    """One protocol-closed history item kept or omitted as a whole from compact input."""

    unit_id: str
    source: str
    text: str
    order: int


@dataclass(frozen=True)
class CompactionSnapshot:
    """Frozen L1 captured before a compaction run starts.

    L1 is the sole compaction truth source. Provider protocol projections,
    external role anchors, recall caches, and the current event must already be
    represented in L1 or remain mechanically projected outside compaction.
    """

    target_input_budget: int
    reserved_output_tokens: int
    clock_kind: CompactionClockKind
    clock_value: int
    memory_items: tuple[tuple[L1TranscriptMessage, ...], ...] = ()
    replay_request: LLMRequestIR | None = None
    replay_dialect: str = ""
    replay_wire_shape: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    # v3 two-segment capture: the LEFT stamp issued by the history owner at
    # begin_left_compaction.  A non-empty stamp selects the stamped-snapshot
    # engine rules (S03/S04 oversize separation, no silent shrink).
    source_stamp: str = ""
    source_epoch: int = 0
    active_turn_ids: tuple[str, ...] = ()

    @classmethod
    def capture_left(
        cls,
        memory_service: Any,
        left_snapshot: Any,
        *,
        target_input_budget: int,
        reserved_output_tokens: int,
        clock_kind: CompactionClockKind,
        clock_value: int,
        metadata: dict[str, Any] | None = None,
        replay_request: LLMRequestIR | None = None,
        replay_dialect: str = "",
        replay_wire_shape: str = "",
    ) -> "CompactionSnapshot":
        """v3 two-segment capture: the LEFT segment only (I10).

        The run has already been opened by the history owner
        (``begin_left_compaction``); this snapshot carries its stamp so the
        install path can verify the left side never moved, while the right
        side stays out of the summary source entirely.  A warm
        ``replay_request`` (provider-confirmed anchor inside L + accepted
        suffix + instruction tail, see the executor's warm split) replaces
        the cold source builder for the model attempt; oversized replays
        fall back to the bounded frozen-L1 source inside the engine.
        """

        transcripts = getattr(memory_service, "left_transcripts", None)
        if not callable(transcripts):
            raise RuntimeError(
                "memory service does not expose left_transcripts for v3 compaction"
            )
        memory_items = tuple(
            tuple(_copy_l1_message(item) for item in transcript)
            for transcript in transcripts()
        )
        merged = dict(metadata or {})
        merged["two_segment_run_id"] = str(left_snapshot.run_id)
        return cls(
            target_input_budget=max(0, int(target_input_budget or 0)),
            reserved_output_tokens=max(0, int(reserved_output_tokens or 0)),
            clock_kind=clock_kind,
            clock_value=max(0, int(clock_value or 0)),
            memory_items=memory_items,
            replay_request=replay_request,
            replay_dialect=str(replay_dialect or "").strip(),
            replay_wire_shape=str(replay_wire_shape or "").strip(),
            metadata=deepcopy(merged),
            source_stamp=str(left_snapshot.stamp or ""),
            source_epoch=0,
            active_turn_ids=(),
        )

    @property
    def previous_summary(self) -> L2Entry | None:
        """Return the compact seed already stored inside frozen L1."""

        return _current_summary(self.memory_items)

    @property
    def has_compactable_history(self) -> bool:
        return bool(
            any(
                not _transcript_is_summary(transcript)
                for transcript in self.memory_items
                if transcript
            )
            or self.previous_summary is not None
        )


class CompactionPolicy(Protocol):
    policy_id: str
    clock_kind: CompactionClockKind
    accepts_memory_candidates: bool

    def system_prompt(self, snapshot: CompactionSnapshot) -> str:
        ...

    def build_source(
        self,
        snapshot: CompactionSnapshot,
        units: Sequence[CompactionUnit],
        *,
        validation_error: str = "",
    ) -> str:
        ...

    def validate_checkpoint(
        self,
        raw_text: str,
        snapshot: CompactionSnapshot,
    ) -> L2Entry:
        ...

@dataclass(frozen=True)
class CompactionRunResult:
    status: str
    attempts: int = 0
    summary_entry: L2Entry | None = None
    memory_result: MemoryCompactResult | None = None
    source_sizes: tuple[int, ...] = ()
    failures: tuple[str, ...] = ()
    clock_kind: CompactionClockKind = CompactionClockKind.USER_TURN
    clock_value: int = 0
    usage: dict[str, Any] | None = None

    @property
    def success(self) -> bool:
        return self.status == "compacted"


def _scope_safe_snapshot(snapshot: CompactionSnapshot) -> CompactionSnapshot:
    """Compaction is a new instruction scope, including when using a warm anchor."""
    from pal.core.prompt_context import CONTEXT_KIND, applicable_context
    items = tuple(tuple(message for message in transcript if not (
        message.role == "developer" and message.kind == CONTEXT_KIND
        and message.payload.get("pal_authored")
    )) for transcript in snapshot.memory_items)
    replay = snapshot.replay_request
    if replay is not None and len(applicable_context(replay.messages, active_turn_id="")) != len(replay.messages):
        replay = None  # Do not preserve a checkpoint at the cost of an expired instruction.
    return replace(snapshot, memory_items=items, replay_request=replay,
                   replay_dialect=snapshot.replay_dialect if replay else "",
                   replay_wire_shape=snapshot.replay_wire_shape if replay else "")


@dataclass
class CompactionEngine:
    """Shared compaction orchestration with its own bounded retry budget."""

    policy: CompactionPolicy
    max_attempts: int = 3
    timeout_seconds: float = 180.0
    # An explicit engine ceiling; each request reserves only its visible
    # checkpoint allowance plus modest reasoning/serialization headroom.
    max_output_tokens: int = 64_000

    async def run(
        self,
        snapshot: CompactionSnapshot,
        *,
        llm_runtime: Any,
        memory_service: Any,
        after_commit: Callable[[], None] | None = None,
        replay_guard: Callable[[], bool] | None = None,
        commit_guard: Callable[[], bool] | None = None,
    ) -> CompactionRunResult:
        snapshot = _scope_safe_snapshot(snapshot)
        units = list(build_compaction_units(snapshot))
        retained = list(units)
        source_sizes: list[int] = []
        failures: list[str] = []
        attempts = 0
        validation_error = ""
        repair_output = ""
        output_target = None
        run_id = uuid4().hex[:12]
        started_at = time.monotonic()
        attempt_started_at = started_at
        outcome = None

        def log_failure(reason: str) -> None:
            failures.append(reason)
            response = getattr(outcome, "response", None)
            details = getattr(getattr(response, "message", None), "metadata", {}) or {}
            endpoint = (
                getattr(outcome, "preferred_endpoint_id", None)
                or getattr(llm_runtime, "last_endpoint_id", None)
                or snapshot.metadata.get("preferred_endpoint_id")
                or "unresolved"
            )
            _LOGGER.warning(
                "compact attempt_failed run=%s policy=%s endpoint=%s attempt=%s "
                "elapsed_seconds=%.3f replay=%s reason=%s failure_kind=%s error_type=%s "
                "input_tokens=%s output_tokens=%s output_limit=%s timeout_seconds=%s",
                run_id, self.policy.policy_id, endpoint, attempts,
                time.monotonic() - attempt_started_at,
                snapshot.replay_request is not None, _log_failure_reason(reason),
                details.get("failure_kind", ""), details.get("error_type", ""),
                getattr(outcome, "input_tokens", 0), getattr(outcome, "output_tokens", 0),
                request.policy.max_output_tokens, self.timeout_seconds,
            )

        def finish(snapshot: CompactionSnapshot, **kwargs: Any) -> CompactionRunResult:
            result = self._result(snapshot, **kwargs)
            _LOGGER.log(
                logging.INFO if result.success else logging.WARNING,
                "compact finished run=%s policy=%s status=%s attempts=%s "
                "elapsed_seconds=%.3f failure_count=%s",
                run_id, self.policy.policy_id, result.status, result.attempts,
                time.monotonic() - started_at, len(result.failures),
            )
            return result

        def hot_replay_unavailable() -> bool:
            return replay_guard is not None and (
                snapshot.replay_request is None or not replay_guard()
            )

        while attempts < max(1, int(self.max_attempts or 1)):
            # Preflight failures must not inherit the preceding model response.
            outcome = None
            attempt_started_at = time.monotonic()
            if hot_replay_unavailable():
                return finish(
                    snapshot, status="hot_cache_unavailable", attempts=attempts,
                    source_sizes=source_sizes, failures=failures,
                )
            source = (
                ""
                if snapshot.replay_request is not None
                else self.policy.build_source(
                    snapshot,
                    retained,
                    validation_error=validation_error,
                ).strip()
            )
            request = self._request(
                snapshot,
                source,
                attempt=attempts + 1,
                validation_error=validation_error,
            )
            base_request = request
            request = _repair_request(request, output=repair_output, error=validation_error, target=output_target)
            advice = await _preflight(llm_runtime, request)
            if repair_output and _preflight_requires_compaction(advice):
                # Source has priority over the model's failed output.
                request = _repair_request(base_request, output="", error=validation_error, target=output_target)
                advice = await _preflight(llm_runtime, request)
                _LOGGER.info("compact repair output omitted for context budget run=%s", run_id)
            if snapshot.target_input_budget <= 0:
                resolved = _snapshot_for_budget_advice(snapshot, advice)
                if resolved.target_input_budget > 0:
                    snapshot = resolved
                    # Rebuild output allowance as well as input checks for
                    # small endpoints; do not send the provisional request.
                    continue
            if _preflight_requires_compaction(advice):
                if snapshot.replay_request is not None:
                    # The provider-identical replay is useful only while it
                    # fits.  Fall back to the bounded frozen-L1 source so the
                    # ordinary shrink policy can still recover an oversized
                    # conversation instead of looping on an immutable replay.
                    snapshot = replace(
                        snapshot,
                        replay_request=None,
                        replay_dialect="",
                        replay_wire_shape="",
                    )
                    continue
                if snapshot.source_stamp:
                    # Full-source mode never trades coverage for fit (S03/
                    # S04), but it must still separate an oversized source
                    # from an incompressible base that overflows on its own
                    # (B02): probe the request with an empty source once.
                    empty_source = self.policy.build_source(
                        snapshot,
                        [],
                        validation_error=validation_error,
                    ).strip()
                    empty_request = self._request(
                        snapshot,
                        empty_source,
                        attempt=attempts + 1,
                        validation_error=validation_error,
                    )
                    empty_advice = await _preflight(llm_runtime, empty_request)
                    if not _preflight_requires_compaction(empty_advice):
                        log_failure("input:source_too_large")
                        return finish(
                            snapshot,
                            status="source_too_large",
                            attempts=attempts,
                            source_sizes=source_sizes,
                            failures=failures,
                        )
                    log_failure("input:base_context_over_budget")
                    break
                previous_size = len(source)
                snapshot = _snapshot_for_budget_advice(snapshot, advice)
                shrunk = self._shrink(
                    snapshot,
                    retained,
                    previous_size=previous_size,
                    validation_error=validation_error,
                    aggressive=False,
                )
                if shrunk is None:
                    log_failure("input:base_context_over_budget")
                    break
                retained = shrunk
                continue

            # Preflight may await provider work. Recheck immediately before
            # every model attempt, including retries; never silently go cold.
            if hot_replay_unavailable():
                return finish(
                    snapshot, status="hot_cache_unavailable", attempts=attempts,
                    source_sizes=source_sizes, failures=failures,
                )
            source_sizes.append(len(source))
            attempts += 1
            attempt_started_at = time.monotonic()
            outcome = None
            try:
                outcome = await self._generate(llm_runtime, request)
            except Exception as exc:
                log_failure(f"endpoint:{type(exc).__name__}")
                continue

            finish_reason = str(getattr(outcome, "finish_reason", "") or "")
            if finish_reason == LLMFinishReason.COMPACT_REQUIRED:
                log_failure("endpoint:compact_required")
                if snapshot.replay_request is not None:
                    snapshot = replace(
                        snapshot,
                        replay_request=None,
                        replay_dialect="",
                        replay_wire_shape="",
                    )
                    validation_error = ""
                    repair_output = ""
                    continue
                if snapshot.source_stamp:
                    # Same full-source rule on the endpoint's own verdict.
                    return finish(
                        snapshot,
                        status="source_too_large",
                        attempts=attempts,
                        source_sizes=source_sizes,
                        failures=failures,
                    )
                snapshot = _snapshot_for_outcome(snapshot, outcome)
                shrunk = self._shrink(
                    snapshot,
                    retained,
                    previous_size=len(source),
                    validation_error="",
                    aggressive=False,
                )
                if shrunk is None:
                    log_failure("input:base_context_over_budget")
                    break
                retained = shrunk
                validation_error = ""
                repair_output = ""
                continue
            if _is_output_truncation(finish_reason):
                log_failure(f"output:{finish_reason or 'truncated'}")
                repair_output = str(getattr(outcome, "text", "") or "")
                validation_error = "Previous output was truncated; regenerate a shorter complete JSON object."
                output_target = max(1, (output_target or compaction_visible_token_limit(snapshot)) // 2)
                continue
            if finish_reason == LLMFinishReason.ERROR:
                log_failure("endpoint:error")
                continue

            raw_text = str(getattr(outcome, "text", "") or "").strip()
            try:
                visible_limit = compaction_visible_token_limit(snapshot)
                summary_entry = self.policy.validate_checkpoint(
                    raw_text,
                    snapshot,
                )
                normalized_tokens = _estimate_visible_tokens(json.dumps(summary_entry.payload, ensure_ascii=False))
                if normalized_tokens > visible_limit:
                    raise ValueError(f"checkpoint exceeds the {visible_limit:,}-token visible output limit")
                diagnostics = summary_entry.payload.get("compaction_diagnostics") or []
                if diagnostics:
                    _LOGGER.info("compact normalization run=%s diagnostics=%s", run_id, diagnostics)
                rendered_visible_tokens = _estimate_visible_tokens(
                    summary_entry.rendered or summary_entry.summary
                )
                if rendered_visible_tokens > visible_limit:
                    raise ValueError(
                        f"rendered checkpoint exceeds the {visible_limit:,}-token "
                        f"visible output limit (estimated {rendered_visible_tokens})"
                    )
            except Exception as exc:
                validation_error = _validation_error(exc)
                log_failure(f"schema:{validation_error}")
                repair_output = raw_text
                if "visible" in validation_error and "limit" in validation_error:
                    output_target = max(1, (output_target or visible_limit) // 2)
                continue

            # I14/B05 next-request fit precheck: a schema-valid seed that
            # cannot fit the post-install context — with the next round's
            # tool-result headroom reserved — must never be committed
            # as-is. The engine retries shorter inside the same ticket;
            # without a window budget nothing is claimed to fit.
            headroom = max(0, int(
                snapshot.metadata.get("next_round_headroom_tokens") or 0
            ))
            if snapshot.target_input_budget > 0:
                seed_tokens = _estimate_visible_tokens(
                    summary_entry.rendered or summary_entry.summary
                )
                if seed_tokens + headroom > snapshot.target_input_budget:
                    log_failure("fit:seed_over_budget")
                    output_target = max(
                        1,
                        min(
                            output_target or compaction_visible_token_limit(snapshot),
                            max(1, snapshot.target_input_budget - headroom),
                        ) // 2,
                    )
                    continue

            if commit_guard is not None and not commit_guard():
                # Cancel/sweep safety (X03/X06/X07): a ticket that lost
                # commit eligibility must never install; memory and the
                # receipt ledger stay exactly as they were.
                failures.append("ticket_cancelled")
                return finish(
                    snapshot,
                    status="ticket_cancelled",
                    attempts=attempts,
                    source_sizes=source_sizes,
                    failures=failures,
                )
            committed = await self._commit(
                snapshot,
                memory_service=memory_service,
                summary_entry=summary_entry,
                after_commit=after_commit,
            )
            if isinstance(committed, Exception):
                log_failure(f"commit:{type(committed).__name__}")
                return finish(
                    snapshot,
                    status="commit_failed",
                    attempts=attempts,
                    source_sizes=source_sizes,
                    failures=failures,
                )
            return finish(
                snapshot,
                status="compacted",
                attempts=attempts,
                summary_entry=summary_entry,
                memory_result=committed,
                source_sizes=source_sizes,
                failures=failures,
                usage=_usage_snapshot(outcome),
            )

        return finish(
            snapshot,
            status="failed",
            attempts=attempts,
            source_sizes=source_sizes,
            failures=failures,
        )

    def _request(
        self,
        snapshot: CompactionSnapshot,
        source: str,
        *,
        attempt: int,
        validation_error: str = "",
    ) -> LLMRequestIR:
        snapshot = _scope_safe_snapshot(snapshot)
        visible_limit = compaction_visible_token_limit(snapshot)
        user_prompt = (
            f"{source.rstrip()}\n\n"
            "## Request Constraint\n"
            "The final visible JSON checkpoint for this request must not exceed "
            f"{visible_limit:,} tokens."
        )
        max_output = min(
            max(1, int(self.max_output_tokens)),
            visible_limit + max(2048, (visible_limit + 3) // 4),
        )
        metadata = {
            "preferred_endpoint_id": snapshot.metadata.get(
                "preferred_endpoint_id"
            ),
            "preferred_model_id": snapshot.metadata.get("preferred_model_id"),
            "preferred_endpoint_source": (
                "prompt_cache_replay"
                if snapshot.replay_request is not None
                else None
            ),
            "endpoint_fallback_policy": (
                "strict_preferred"
                if snapshot.replay_request is not None
                else None
            ),
            "response_mode_hint": "operational",
            "purpose": "memory_compaction_engine",
            "compaction_policy": self.policy.policy_id,
            "compaction_attempt": max(0, int(attempt)),
            "compaction_clock_kind": snapshot.clock_kind.value,
            "compaction_clock_value": snapshot.clock_value,
            "max_output_recovery_enabled": False,
            "timeout_seconds": self.timeout_seconds,
            # Replay requests are captured after endpoint model hooks.  The
            # LLM runtime must not inject those instructions/tools a second
            # time before sending the provider-identical A prefix.
            "model_hooks_already_applied": (
                True if snapshot.replay_request is not None else None
            ),
        }
        metadata = {
            key: value
            for key, value in metadata.items()
            if value is not None
        }
        if snapshot.replay_request is not None:
            replay = snapshot.replay_request
            replay_source = [
                self.policy.system_prompt(snapshot).strip(),
                "",
                "## Source Semantics",
                "The preceding resident conversation prefix and its closed tool protocol are the frozen L1 truth source for this compaction.",
                "Do not call tools. Return the complete checkpoint directly as valid JSON.",
            ]
            if validation_error:
                replay_source.extend(
                    [
                        "",
                        "## Previous Output Validation Error",
                        validation_error,
                        "Return a complete corrected JSON object.",
                    ]
                )
            replay_source.extend(
                [
                    "",
                    "## Request Constraint",
                    "The final visible JSON checkpoint for this request must not exceed "
                    f"{visible_limit:,} tokens.",
                ]
            )
            tail_role = (
                MessageRole.DEVELOPER
                if snapshot.replay_wire_shape == "openai_response"
                else MessageRole.USER
            )
            tail = LLMMessageIR(
                role=tail_role,
                parts=(TextPartIR("\n".join(replay_source).strip()),),
                semantic_kind="memory_compaction_request",
                prompt_region=PromptRegionIR.ACTIVE_DYNAMIC,
            )
            return replace(
                replay,
                messages=(*replay.messages, tail),
                policy=replace(
                    replay.policy,
                    max_output_tokens=max_output,
                    temperature=0.0,
                    thinking_level=None,
                    thinking_budget_tokens=None,
                    thinking_selection="lowest_supported",
                ),
                model_hint=str(
                    snapshot.metadata.get("preferred_model_id")
                    or replay.model_hint
                    or ""
                )
                or None,
                logical_scope_id=str(
                    snapshot.metadata.get("prompt_cache_scope_id")
                    or replay.logical_scope_id
                    or "pal:resident"
                ).strip(),
                metadata=metadata,
            )
        request = request_ir_from_prompt(
            messages=[
                {
                    "role": "system",
                    "content": self.policy.system_prompt(snapshot),
                },
                {"role": "user", "content": user_prompt},
            ],
            max_output_tokens=max_output,
            model_hint=str(
                snapshot.metadata.get("preferred_model_id") or ""
            )
            or None,
            temperature=0.0,
            tools=[],
            metadata=metadata,
        )
        scope = str(
            snapshot.metadata.get("prompt_cache_scope_id") or "pal:resident"
        ).strip()
        messages = tuple(
            replace(
                message,
                prompt_region=(
                    PromptRegionIR.STABLE_SYSTEM
                    if message.role in {MessageRole.SYSTEM, MessageRole.DEVELOPER}
                    else PromptRegionIR.ACTIVE_DYNAMIC
                ),
            )
            for message in request.messages
        )
        return replace(
            request,
            messages=messages,
            logical_scope_id=f"{scope}:compaction",
            policy=replace(request.policy, thinking_selection="lowest_supported"),
        )

    async def _generate(
        self,
        llm_runtime: Any,
        request: LLMRequestIR,
    ) -> Any:
        method = getattr(llm_runtime, "agenerate", None)
        if callable(method):
            value = method(request)
            if inspect.isawaitable(value):
                return await asyncio.wait_for(
                    value,
                    timeout=max(0.1, float(self.timeout_seconds or 0.1)),
                )
            return value
        method = getattr(llm_runtime, "generate", None)
        if not callable(method):
            raise RuntimeError("LLM runtime does not expose agenerate/generate")
        return await asyncio.wait_for(
            asyncio.to_thread(method, request),
            timeout=max(0.1, float(self.timeout_seconds or 0.1)),
        )

    def _shrink(
        self,
        snapshot: CompactionSnapshot,
        retained: Sequence[CompactionUnit],
        *,
        previous_size: int,
        validation_error: str,
        aggressive: bool,
    ) -> list[CompactionUnit] | None:
        candidate = list(retained)
        removable_count = len(candidate)
        drop_goal = (
            max(1, (removable_count + 3) // 4)
            if aggressive
            else 1
        )
        dropped = 0
        while True:
            if not candidate:
                return None
            candidate.pop(0)
            dropped += 1
            rendered_size = len(
                self.policy.build_source(
                    snapshot,
                    candidate,
                    validation_error=validation_error,
                )
            )
            if dropped >= drop_goal and rendered_size < previous_size:
                return candidate

    async def _commit(
        self,
        snapshot: CompactionSnapshot,
        *,
        memory_service: Any,
        summary_entry: L2Entry,
        after_commit: Callable[[], None] | None = None,
    ) -> MemoryCompactResult | Exception:
        two_segment_run_id = str(
            snapshot.metadata.get("two_segment_run_id") or ""
        ).strip()
        if two_segment_run_id:
            # v3 install: replace the LEFT segment only.  Root arbitration
            # (StaleRun / TerminalClosed / CandidateConflict / stamp drift)
            # surfaces as a normal engine failure — never a silent retry.
            try:
                outcome = memory_service.compact_left(
                    two_segment_run_id,
                    summary_entry,
                    candidate_id=str(
                        snapshot.metadata.get("compaction_op_id")
                        or two_segment_run_id
                    ),
                    after_commit=after_commit,
                )
            except Exception as exc:
                return exc
            return MemoryCompactResult(
                summary=summary_entry.summary,
                projected_entries=[summary_entry],
                metadata={
                    "two_segment": True,
                    "run_id": two_segment_run_id,
                    "status": getattr(outcome, "status", ""),
                    "left_revision": getattr(outcome, "left_revision", 0),
                    "replayed": bool(getattr(outcome, "replayed", False)),
                },
            )
        # capture_left is the only producer of snapshots; a missing
        # two-segment run id means a hand-built snapshot, which has no
        # supported install path (v2 whole-source was physically removed).
        raise RuntimeError(
            "compaction snapshot carries no two_segment_run_id; "
            "whole-source install was removed with v2"
        )


    @staticmethod
    def _result(
        snapshot: CompactionSnapshot,
        *,
        status: str,
        attempts: int,
        summary_entry: L2Entry | None = None,
        memory_result: MemoryCompactResult | None = None,
        source_sizes: Sequence[int] = (),
        failures: Sequence[str] = (),
        usage: dict[str, Any] | None = None,
    ) -> CompactionRunResult:
        return CompactionRunResult(
            status=status,
            attempts=max(0, int(attempts)),
            summary_entry=summary_entry,
            memory_result=memory_result,
            source_sizes=tuple(max(0, int(size)) for size in source_sizes),
            failures=tuple(str(item) for item in failures if str(item)),
            clock_kind=snapshot.clock_kind,
            clock_value=snapshot.clock_value,
            usage=dict(usage) if usage else None,
        )


def _usage_snapshot(outcome: Any) -> dict[str, Any] | None:
    """Structured usage from the successful attempt's provider outcome.

    B10/W11: cached-token misses are recorded exactly as the provider
    reported them instead of being assumed cheap, and a usage-less
    outcome yields None rather than misleading zeros. This is billing
    evidence, never a savings promise.
    """
    if outcome is None:
        return None
    token_fields = (
        "input_tokens",
        "uncached_input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
    )
    values: dict[str, Any] = {
        **{field: int(getattr(outcome, field, 0) or 0) for field in token_fields},
        "cost": float(getattr(outcome, "cost", 0.0) or 0.0),
        "usage_reported": bool(getattr(outcome, "usage_reported", False)),
    }
    if not values["usage_reported"] and not any(
        values[field] for field in token_fields
    ):
        return None
    return values


def build_compaction_units(
    snapshot: CompactionSnapshot,
) -> tuple[CompactionUnit, ...]:
    units: list[CompactionUnit] = []
    order = 0
    unit_text_limit = max(
        1_024,
        min(
            16_000,
            max(1_024, int(snapshot.target_input_budget or 0) // 2),
        ),
    )
    for index, transcript in enumerate(snapshot.memory_items):
        if not transcript or _transcript_is_summary(transcript):
            continue
        units.append(
            CompactionUnit(
                unit_id=f"memory:{index}",
                source="memory",
                text=_render_l1_transcript(
                    transcript,
                    max_chars=unit_text_limit,
                ),
                order=order,
            )
        )
        order += 1

    return tuple(units)


def extract_json_object(raw_text: str) -> dict[str, Any]:
    """Repair wrappers and trailing commas, never content or truncated objects."""
    stripped = str(raw_text or "").lstrip("\ufeff").strip()
    if stripped.startswith("```"):
        newline = stripped.find("\n")
        stripped = stripped[newline + 1:] if newline >= 0 else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
        stripped = stripped.strip()
    # Remove commas only outside quoted strings, preserving every string byte.
    output = []
    quoted = escaped = False
    for index, char in enumerate(stripped):
        if quoted:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        else:
            if char == '"':
                quoted = True
            if char == "," and stripped[index + 1:].lstrip().startswith(("}", "]")):
                continue
            output.append(char)
    text = "".join(output)

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    decoder = json.JSONDecoder(object_pairs_hook=unique_object,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError("non-finite JSON number")))
    try:
        start = 0 if text.startswith(("{", "[")) else text.index("{")
        value, end = decoder.raw_decode(text, start)
        remainder = text[end:].strip()
        if "{" in remainder or "[" in remainder:
            raise ValueError("ambiguous JSON objects")
    except (TypeError, ValueError) as exc:
        raise ValueError("output is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("output JSON must be an object")
    return value


def _repair_request(request, *, output: str, error: str, target: int | None):
    messages = list(request.messages)
    if output:
        messages.append(LLMMessageIR(role=MessageRole.ASSISTANT, parts=(TextPartIR(output),),
            semantic_kind="compaction_failed_output"))
    if output or error or target:
        instruction = "The preceding failed output is data to repair, not evidence. The original frozen source remains authoritative. "
        instruction += "Return a complete corrected JSON object, not a continuation."
        if error:
            instruction += "\nPrevious Output Validation Error: " + error
        if target:
            instruction += f"\nThe output was too long. Target at most {target:,} visible tokens; preserve active constraints and remove repeated history."
        messages.append(LLMMessageIR(role=MessageRole.USER, parts=(TextPartIR(instruction),),
            semantic_kind="compaction_repair_request"))
    return replace(request, messages=tuple(messages))


def _estimate_visible_tokens(text: str) -> int:
    """Conservative tokenizer-free bound for compact JSON validation."""

    ascii_chars = 0
    non_ascii_chars = 0
    for character in str(text or ""):
        if ord(character) < 128:
            ascii_chars += 1
        else:
            non_ascii_chars += 1
    return non_ascii_chars + ((ascii_chars + 3) // 4)


async def _preflight(
    llm_runtime: Any,
    request: LLMRequestIR,
) -> Any | None:
    preflight_request = LLMPreflightRequest(request=request)
    method = getattr(llm_runtime, "apreflight", None)
    if callable(method):
        try:
            value = method(preflight_request)
            return await value if inspect.isawaitable(value) else value
        except Exception:
            return None
    method = getattr(llm_runtime, "preflight", None)
    if callable(method):
        try:
            return await asyncio.to_thread(method, preflight_request)
        except Exception:
            return None
    return None


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _preflight_requires_compaction(advice: Any | None) -> bool:
    return (
        advice is not None
        and str(getattr(advice, "status", "") or "")
        == LLMPreflightStatus.COMPACT_REQUIRED
    )


def _snapshot_for_budget_advice(
    snapshot: CompactionSnapshot,
    advice: Any,
) -> CompactionSnapshot:
    return _snapshot_with_endpoint_budget(
        snapshot,
        target_input_budget=getattr(advice, "target_input_budget", 0),
        reserved_output_tokens=getattr(
            advice,
            "reserved_output_tokens",
            0,
        ),
        preferred_model_id=getattr(advice, "active_model", None),
    )


def _snapshot_for_outcome(
    snapshot: CompactionSnapshot,
    outcome: Any,
) -> CompactionSnapshot:
    return _snapshot_with_endpoint_budget(
        snapshot,
        target_input_budget=getattr(outcome, "target_input_budget", 0),
        reserved_output_tokens=getattr(
            outcome,
            "reserved_output_tokens",
            0,
        ),
        preferred_endpoint_id=getattr(
            outcome,
            "preferred_endpoint_id",
            None,
        ),
        preferred_model_id=getattr(
            outcome,
            "preferred_model_id",
            None,
        ),
    )


def _snapshot_with_endpoint_budget(
    snapshot: CompactionSnapshot,
    *,
    target_input_budget: Any = 0,
    reserved_output_tokens: Any = 0,
    preferred_endpoint_id: Any = None,
    preferred_model_id: Any = None,
) -> CompactionSnapshot:
    target = max(0, int(target_input_budget or 0))
    reserved = max(0, int(reserved_output_tokens or 0))
    endpoint_id = str(preferred_endpoint_id or "").strip()
    model_id = str(preferred_model_id or "").strip()
    metadata = deepcopy(snapshot.metadata)
    if endpoint_id:
        metadata["preferred_endpoint_id"] = endpoint_id
    if model_id:
        metadata["preferred_model_id"] = model_id
    return replace(
        snapshot,
        target_input_budget=target or snapshot.target_input_budget,
        reserved_output_tokens=reserved or snapshot.reserved_output_tokens,
        metadata=metadata,
    )


def _is_output_truncation(finish_reason: str) -> bool:
    return str(finish_reason or "").strip().lower() in {
        "length",
        "max_tokens",
        "max_output_tokens",
        "model_context_window_exceeded",
    }


def _copy_l1_message(value: Any) -> L1TranscriptMessage:
    if isinstance(value, L1TranscriptMessage):
        return replace(
            value,
            tool_calls=(
                deepcopy(value.tool_calls)
                if value.tool_calls is not None
                else None
            ),
            payload=deepcopy(value.payload or {}),
        )
    if isinstance(value, dict):
        return L1TranscriptMessage(
            role=str(value.get("role") or ""),
            content=str(value.get("content") or ""),
            kind=value.get("kind") or "",
            tool_calls=(
                deepcopy(
                    [
                        item
                        for item in list(value.get("tool_calls") or ())
                        if isinstance(item, dict)
                    ]
                )
                or None
            ),
            tool_call_id=str(value.get("tool_call_id") or "") or None,
            payload=deepcopy(value.get("payload") or {}),
        )
    return L1TranscriptMessage(role="assistant", content=str(value or ""))


def _current_summary(
    memory_items: Sequence[Sequence[L1TranscriptMessage]],
) -> L2Entry | None:
    for transcript in memory_items:
        for message in transcript:
            if _message_kind(message) != L1MessageKind.RUNTIME_CONTEXT_SUMMARY:
                continue
            content = str(message.content or "").strip()
            payload = deepcopy(message.payload or {})
            summary_payload = (
                payload.get("summary")
                if isinstance(payload.get("summary"), dict)
                else {}
            )
            summary = str(summary_payload.get("summary") or "").strip()
            search_text = str(
                summary_payload.get("search_text") or ""
            ).strip()
            if content or summary:
                return L2Entry(
                    entry_id="memory_summary_current",
                    kind="summary",
                    scope="system",
                    title="Conversation Summary",
                    summary=summary or content,
                    source_kind="l1_compaction",
                    candidate_state="stable",
                    rendered=content or summary,
                    search_text=search_text or summary or content,
                    payload=payload,
                )
    return None


def _transcript_is_summary(
    transcript: Sequence[L1TranscriptMessage],
) -> bool:
    return any(
        _message_kind(message) == L1MessageKind.RUNTIME_CONTEXT_SUMMARY
        for message in transcript
    )


def _message_kind(message: L1TranscriptMessage) -> L1MessageKind:
    try:
        return L1MessageKind(str(message.kind or ""))
    except ValueError:
        role = str(message.role or "").strip()
        if role == "user":
            return L1MessageKind.USER_REQUEST
        if role == "tool":
            return L1MessageKind.TOOL_RESULT
        if role == "assistant" and message.tool_calls:
            return L1MessageKind.ASSISTANT_TOOL_CALL
        return L1MessageKind.ASSISTANT_REPLY


def _render_l1_transcript(
    transcript: Sequence[L1TranscriptMessage],
    *,
    max_chars: int,
) -> str:
    lines: list[str] = []
    for message in transcript:
        role = str(message.role or "assistant").strip()
        kind = _message_kind(message).value
        lines.append(f"[{role} kind={kind}]")
        if message.tool_calls:
            lines.append(
                json.dumps(
                    message.tool_calls,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
            )
        content = str(message.content or "").strip()
        if content:
            lines.append(
                _bounded_source_text(
                    content,
                    max_chars=max_chars,
                    label=f"{role or 'message'} body",
                )
            )
        if message.tool_call_id:
            lines.append(f"[tool_call_id={message.tool_call_id}]")
    return "\n".join(lines).strip()


def _bounded_source_text(
    value: str,
    *,
    max_chars: int,
    label: str,
) -> str:
    text = str(value or "")
    limit = max(256, int(max_chars or 0))
    if len(text) <= limit:
        return text
    marker_budget = 96
    content_budget = max(2, limit - marker_budget)
    head_chars = max(1, content_budget // 2)
    tail_chars = max(1, content_budget - head_chars)
    omitted = max(0, len(text) - head_chars - tail_chars)
    return (
        text[:head_chars].rstrip()
        + f"\n[... {label} omitted {omitted} chars; head/tail projection only ...]\n"
        + text[-tail_chars:].lstrip()
    )


def _log_failure_reason(reason: str) -> str:
    if not reason.startswith("schema:"):
        return reason
    detail = reason.removeprefix("schema:")
    if " has extra fields:" in detail:
        detail = detail.split(" has extra fields:", 1)[0] + " has extra fields"
    # Only known validator diagnostics are safe; custom policy exceptions may
    # contain source text, credentials, or a provider response body.
    if re.fullmatch(
        r"(?:output (?:is not valid JSON|JSON must be an object)|"
        r"(?:rendered )?checkpoint exceeds the [0-9,]+-token (?:visible )?output limit \(estimated [0-9]+\)|"
        r"[a-zA-Z0-9_.\[\]]+ (?:must be [a-zA-Z0-9_. /-]+|is required for case|has extra fields|must be omitted for fact)|"
        r"continuity missing fields: [a-z_, ]+)", detail,
    ):
        return "schema:" + detail
    return "schema:checkpoint_validation_failed"


def _validation_error(exc: Exception) -> str:
    text = " ".join(str(exc or "").split())
    if not text:
        text = type(exc).__name__
    return text[:240]


__all__ = [
    "CompactionClockKind",
    "CompactionEngine",
    "CompactionPolicy",
    "CompactionRunResult",
    "CompactionSnapshot",
    "CompactionUnit",
    "build_compaction_units",
    "compaction_visible_token_limit",
    "extract_json_object",
]
