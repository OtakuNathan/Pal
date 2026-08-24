from __future__ import annotations

import asyncio
import inspect
import json
from copy import deepcopy
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Callable, Protocol, Sequence

from pal.llm.contracts import (
    LLMPreflightRequest,
)
from pal.llm.conversions import request_ir_from_prompt
from pal.llm.ir import LLMRequestIR, MessageRole, PromptRegionIR
from pal.memory.contracts import (
    L1MessageKind,
    L1TranscriptMessage,
    L2Entry,
    MemoryCompactRequest,
    MemoryCompactResult,
)
from pal.shared import LLMFinishReason, LLMPreflightStatus

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
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def capture(
        cls,
        memory_service: Any,
        *,
        target_input_budget: int,
        reserved_output_tokens: int,
        clock_kind: CompactionClockKind,
        clock_value: int,
        metadata: dict[str, Any] | None = None,
    ) -> "CompactionSnapshot":
        l1_store = getattr(memory_service, "l1_store", None)
        raw_items = list(getattr(l1_store, "items", ()) or ())
        turns = list(
            getattr(getattr(l1_store, "turns", None), "turns", ()) or ()
        )
        if len(turns) == len(raw_items):
            raw_items = [
                transcript
                for turn, transcript in zip(turns, raw_items)
                if str(
                    getattr(
                        getattr(turn, "state", ""),
                        "value",
                        getattr(turn, "state", ""),
                    )
                ) != "active"
            ]
        memory_items = tuple(
            tuple(_copy_l1_message(item) for item in list(transcript or ()))
            for transcript in raw_items
        )
        return cls(
            target_input_budget=max(0, int(target_input_budget or 0)),
            reserved_output_tokens=max(0, int(reserved_output_tokens or 0)),
            clock_kind=clock_kind,
            clock_value=max(0, int(clock_value or 0)),
            memory_items=memory_items,
            metadata=deepcopy(metadata or {}),
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

    @property
    def success(self) -> bool:
        return self.status == "compacted"


@dataclass
class CompactionEngine:
    """Shared compaction orchestration with its own bounded retry budget."""

    policy: CompactionPolicy
    max_attempts: int = 3
    timeout_seconds: float = 180.0
    # Keep enough provider output headroom for models that count hidden
    # reasoning against max_output_tokens. The policy prompt independently
    # caps the visible checkpoint at half the input budget, up to 20k tokens.
    max_output_tokens: int = 64_000

    async def run(
        self,
        snapshot: CompactionSnapshot,
        *,
        llm_runtime: Any,
        memory_service: Any,
        after_commit: Callable[[], None] | None = None,
    ) -> CompactionRunResult:
        snapshot = await _with_compaction_output_limit(
            snapshot,
            llm_runtime=llm_runtime,
            fallback=self.max_output_tokens,
        )
        units = list(build_compaction_units(snapshot))
        retained = list(units)
        source_sizes: list[int] = []
        failures: list[str] = []
        attempts = 0
        validation_error = ""
        consecutive_schema_failures = 0

        while attempts < max(1, int(self.max_attempts or 1)):
            source = self.policy.build_source(
                snapshot,
                retained,
                validation_error=validation_error,
            ).strip()
            request = self._request(
                snapshot,
                source,
                attempt=attempts + 1,
            )
            advice = await _preflight(llm_runtime, request)
            if _preflight_requires_compaction(advice):
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
                    failures.append("input:base_context_over_budget")
                    break
                retained = shrunk
                continue

            source_sizes.append(len(source))
            attempts += 1
            try:
                outcome = await self._generate(llm_runtime, request)
            except Exception as exc:
                failures.append(f"endpoint:{type(exc).__name__}")
                consecutive_schema_failures = 0
                continue

            finish_reason = str(getattr(outcome, "finish_reason", "") or "")
            if finish_reason == LLMFinishReason.COMPACT_REQUIRED:
                failures.append("endpoint:compact_required")
                consecutive_schema_failures = 0
                snapshot = _snapshot_for_outcome(snapshot, outcome)
                shrunk = self._shrink(
                    snapshot,
                    retained,
                    previous_size=len(source),
                    validation_error="",
                    aggressive=False,
                )
                if shrunk is None:
                    failures.append("input:base_context_over_budget")
                    break
                retained = shrunk
                validation_error = ""
                continue
            if _is_output_truncation(finish_reason):
                failures.append(f"output:{finish_reason or 'truncated'}")
                consecutive_schema_failures = 0
                snapshot = _snapshot_for_outcome(snapshot, outcome)
                shrunk = self._shrink(
                    snapshot,
                    retained,
                    previous_size=len(source),
                    validation_error="",
                    aggressive=True,
                )
                if shrunk is None:
                    break
                retained = shrunk
                validation_error = ""
                continue
            if finish_reason == LLMFinishReason.ERROR:
                failures.append("endpoint:error")
                consecutive_schema_failures = 0
                continue

            raw_text = str(getattr(outcome, "text", "") or "").strip()
            try:
                visible_limit = compaction_visible_token_limit(snapshot)
                raw_visible_tokens = _estimate_visible_tokens(raw_text)
                if raw_visible_tokens > visible_limit:
                    raise ValueError(
                        f"checkpoint exceeds the {visible_limit:,}-token visible "
                        f"output limit (estimated {raw_visible_tokens})"
                    )
                summary_entry = self.policy.validate_checkpoint(
                    raw_text,
                    snapshot,
                )
                rendered_visible_tokens = _estimate_visible_tokens(
                    summary_entry.rendered or summary_entry.summary
                )
                if rendered_visible_tokens > visible_limit:
                    raise ValueError(
                        f"rendered checkpoint exceeds the {visible_limit:,}-token "
                        f"visible output limit (estimated {rendered_visible_tokens})"
                    )
            except Exception as exc:
                consecutive_schema_failures += 1
                validation_error = _validation_error(exc)
                failures.append(f"schema:{validation_error}")
                if consecutive_schema_failures >= 2:
                    shrunk = self._shrink(
                        snapshot,
                        retained,
                        previous_size=len(source),
                        validation_error=validation_error,
                        aggressive=False,
                    )
                    if shrunk is not None:
                        retained = shrunk
                continue

            committed = await self._commit(
                snapshot,
                memory_service=memory_service,
                summary_entry=summary_entry,
                after_commit=after_commit,
            )
            if isinstance(committed, Exception):
                return self._result(
                    snapshot,
                    status="commit_failed",
                    attempts=attempts,
                    source_sizes=source_sizes,
                    failures=(*failures, f"commit:{type(committed).__name__}"),
                )
            return self._result(
                snapshot,
                status="compacted",
                attempts=attempts,
                summary_entry=summary_entry,
                memory_result=committed,
                source_sizes=source_sizes,
                failures=failures,
            )

        return self._result(
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
    ) -> LLMRequestIR:
        visible_limit = compaction_visible_token_limit(snapshot)
        user_prompt = (
            f"{source.rstrip()}\n\n"
            "## Request Constraint\n"
            "The final visible JSON checkpoint for this request must not exceed "
            f"{visible_limit:,} tokens."
        )
        max_output = max(
            1,
            int(
                snapshot.metadata.get("compaction_max_output_tokens")
                or self.max_output_tokens
                or 0
            ),
        )
        metadata = {
            "preferred_endpoint_id": snapshot.metadata.get(
                "preferred_endpoint_id"
            ),
            "preferred_model_id": snapshot.metadata.get("preferred_model_id"),
            "response_mode_hint": "operational",
            "purpose": "memory_compaction_engine",
            "compaction_policy": self.policy.policy_id,
            "compaction_attempt": max(0, int(attempt)),
            "compaction_clock_kind": snapshot.clock_kind.value,
            "compaction_clock_value": snapshot.clock_value,
            "max_output_recovery_enabled": False,
            "timeout_seconds": self.timeout_seconds,
        }
        metadata = {
            key: value
            for key, value in metadata.items()
            if value is not None
        }
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
        request = MemoryCompactRequest(
            target_input_budget=snapshot.target_input_budget,
            reserved_output_tokens=snapshot.reserved_output_tokens,
            summary_entry=summary_entry,
            metadata={
                "compaction_policy": self.policy.policy_id,
                "compaction_clock_kind": snapshot.clock_kind.value,
                "compaction_clock_value": snapshot.clock_value,
            },
        )
        try:
            if after_commit is not None:
                method = getattr(
                    memory_service,
                    "acompact_transactionally",
                    None,
                )
                if callable(method):
                    value = method(request, after_commit=after_commit)
                    return await value if inspect.isawaitable(value) else value
                method = getattr(
                    memory_service,
                    "compact_transactionally",
                    None,
                )
                if not callable(method):
                    raise RuntimeError(
                        "memory service does not support transactional compaction"
                    )
                return await asyncio.to_thread(
                    method,
                    request,
                    after_commit=after_commit,
                )
            method = getattr(memory_service, "acompact", None)
            if callable(method):
                value = method(request)
                return await value if inspect.isawaitable(value) else value
            method = getattr(memory_service, "compact", None)
            if not callable(method):
                raise RuntimeError("memory service does not expose compact")
            return await asyncio.to_thread(method, request)
        except Exception as exc:
            return exc

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
        )


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
    stripped = str(raw_text or "").strip()
    if stripped.startswith("```"):
        newline = stripped.find("\n")
        stripped = stripped[newline + 1 :] if newline >= 0 else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
        stripped = stripped.strip()
    try:
        value = json.loads(stripped)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("output is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("output JSON must be an object")
    return value


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


async def _with_compaction_output_limit(
    snapshot: CompactionSnapshot,
    *,
    llm_runtime: Any,
    fallback: int,
) -> CompactionSnapshot:
    """Use the selected provider's declared ceiling without reducing reasoning headroom."""

    method = getattr(llm_runtime, "resolve_endpoint_facts", None)
    if not callable(method):
        return _snapshot_with_output_limit(snapshot, fallback)
    try:
        value = method(
            preferred_endpoint_id=str(
                snapshot.metadata.get("preferred_endpoint_id") or ""
            )
            or None,
        )
        facts = await value if inspect.isawaitable(value) else value
    except Exception:
        return _snapshot_with_output_limit(snapshot, fallback)
    if not isinstance(facts, dict):
        return _snapshot_with_output_limit(snapshot, fallback)
    limit = (
        _positive_int(facts.get("max_output_tokens_upper_limit"))
        or _positive_int(facts.get("max_output_tokens"))
        or max(1, int(fallback or 1))
    )
    return _snapshot_with_output_limit(snapshot, limit)


def _snapshot_with_output_limit(
    snapshot: CompactionSnapshot,
    value: Any,
) -> CompactionSnapshot:
    metadata = deepcopy(snapshot.metadata)
    metadata["compaction_max_output_tokens"] = max(
        1,
        _positive_int(value) or 1,
    )
    return replace(snapshot, metadata=metadata)


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
