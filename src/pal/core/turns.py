from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
import json
import re
from typing import Any
from uuid import uuid4

from pal.channel import ChannelEnvelope
from pal.failure.contracts import (
    FAILURE_VERIFICATION_FAILED,
    FailureDraft,
    VerificationResult,
)
from pal.llm.contracts import CanonicalLLMOutcome, CanonicalToolCall, CanonicalToolResult
from pal.service import ServiceDefinition, ServiceTriggerEvent, build_service_trigger_input
from pal.shared import EffectKind, LLMFinishReason, LLMPreflightStatus, PromptAssemblyContext, RuntimeStatus
from pal.shared.payloads import extract_text_from_payload
from pal.memory import L1TranscriptMessage
from pal.stream_events import NormalizedLLMStreamEvent


@dataclass(frozen=True)
class ToolObservation:
    tool_name: str
    ok: bool
    summary: str
    structured: dict[str, Any] | None = None

    def to_prompt_block(self) -> str:
        status = RuntimeStatus.OK if self.ok else RuntimeStatus.ERROR
        lines = [f"tool={self.tool_name}", f"status={status}", f"summary={self.summary}"]
        if self.structured:
            lines.append(f"structured={self.structured}")
        return "\n".join(lines)


@dataclass(frozen=True)
class L1CommitPayload:
    turn_id: str
    transcript: list[L1TranscriptMessage] = field(default_factory=list)
    tool_observations: list[ToolObservation] = field(default_factory=list)


@dataclass(frozen=True)
class TurnOutcome:
    turn_id: str
    final_reply: str
    commit_payload: L1CommitPayload


@dataclass(frozen=True)
class EffectRequest:
    kind: str
    effect_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(frozen=True)
class EffectResult:
    status: str
    payload: Any = None
    text: str = ""


@dataclass(frozen=True)
class LLMPreflightEffect(EffectRequest):
    assembly_context: PromptAssemblyContext = field(default_factory=PromptAssemblyContext)
    model_hint: str | None = None
    max_output_tokens: int = 1024
    kind: str = EffectKind.LLM_PREFLIGHT


@dataclass(frozen=True)
class LLMRequestEffect(EffectRequest):
    assembly_context: PromptAssemblyContext = field(default_factory=PromptAssemblyContext)
    model_hint: str | None = None
    max_output_tokens: int = 1024
    tools_override: list[dict[str, Any]] | None = None
    kind: str = EffectKind.LLM_REQUEST


@dataclass(frozen=True)
class ToolCallEffect(EffectRequest):
    tool_call: CanonicalToolCall = field(default_factory=lambda: CanonicalToolCall(name="", args={}))
    kind: str = EffectKind.TOOL_CALL


@dataclass(frozen=True)
class MemoryCompactEffect(EffectRequest):
    assembly_context: PromptAssemblyContext = field(default_factory=PromptAssemblyContext)
    target_input_budget: int = 0
    reserved_output_tokens: int = 0
    kind: str = EffectKind.MEMORY_COMPACT


@dataclass(frozen=True)
class MailboxReplyEffect(EffectRequest):
    channel_envelope: ChannelEnvelope = field(default_factory=lambda: ChannelEnvelope(event=None, endpoint=None, response_handle=None))  # type: ignore[arg-type]
    text: str = ""
    kind: str = EffectKind.MAILBOX_REPLY


@dataclass(frozen=True)
class MailboxReplyStreamEffect(EffectRequest):
    channel_envelope: ChannelEnvelope = field(default_factory=lambda: ChannelEnvelope(event=None, endpoint=None, response_handle=None))  # type: ignore[arg-type]
    event: NormalizedLLMStreamEvent = field(default_factory=NormalizedLLMStreamEvent)
    kind: str = EffectKind.MAILBOX_REPLY_STREAM


TurnProgram = Generator[EffectRequest, EffectResult, TurnOutcome]


@dataclass
class TurnContinuation:
    turn_id: str
    channel_envelope: ChannelEnvelope
    program: TurnProgram
    correlation_id: str
    started: bool = False
    waiting_effect_id: str | None = None
    finalization_only: bool = False
    finalization_attempted: bool = False
    finalization_reason: str = ""
    tool_batch_count: int = 0
    last_response_mode: str = "chat"
    preferred_llm_endpoint_id: str | None = None
    preferred_llm_model_id: str | None = None
    tool_observations: list[ToolObservation] = field(default_factory=list)
    tool_protocol_messages: list[dict[str, Any]] = field(default_factory=list)
    pending_tool_call_batch: list[CanonicalToolCall] = field(default_factory=list)
    pending_tool_results: list[CanonicalToolResult] = field(default_factory=list)
    pending_assistant_tool_text: str = ""
    budget_failure_feedback_text: str = ""
    prompt_budget_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FailureFlowOutcome:
    verification: VerificationResult
    enriched_fields: dict[str, Any] = field(default_factory=dict)


def channel_turn_program(
    channel_envelope: ChannelEnvelope,
    *,
    core_mode: str = "default",
    max_output_tokens: int = 1024,
) -> TurnProgram:
    # This program expresses the control flow of a single user-facing turn.
    # It never performs I/O directly; it only yields effects for PalCore to
    # interpret and feed back into the generator.
    observations: list[ToolObservation] = []
    compact_note = ""
    while True:
        metadata = {"compact_note": compact_note}
        assembly_context = PromptAssemblyContext(
            event=channel_envelope.event,
            core_mode=core_mode,
            metadata=metadata,
        )
        advice = yield LLMPreflightEffect(
            assembly_context=assembly_context,
            max_output_tokens=max_output_tokens,
        )
        compact_note = ""
        if getattr(advice.payload, "status", "") == LLMPreflightStatus.COMPACT_REQUIRED:
            compact_result = yield MemoryCompactEffect(
                assembly_context=assembly_context,
                target_input_budget=getattr(advice.payload, "target_input_budget", 0),
                reserved_output_tokens=getattr(advice.payload, "reserved_output_tokens", 0),
            )
            compact_note = str(compact_result.payload.summary) if compact_result.payload is not None else ""
            continue
        outcome_result = yield LLMRequestEffect(
            assembly_context=assembly_context,
            max_output_tokens=max_output_tokens,
        )
        outcome = _as_llm_outcome(outcome_result.payload)
        if outcome is not None and outcome.finish_reason == LLMFinishReason.COMPACT_REQUIRED:
            compact_result = yield MemoryCompactEffect(
                assembly_context=assembly_context,
                target_input_budget=outcome.target_input_budget,
                reserved_output_tokens=outcome.reserved_output_tokens,
            )
            compact_note = str(compact_result.payload.summary) if compact_result.payload is not None else ""
            continue
        if outcome is not None and outcome.tool_calls:
            for tool_call in outcome.tool_calls:
                tool_result = yield ToolCallEffect(tool_call=tool_call)
                _append_tool_observation(observations, tool_result.payload)
            continue
        final_reply = render_final_reply(channel_envelope, outcome) if outcome is not None else ""
        reply_result = yield MailboxReplyEffect(channel_envelope=channel_envelope, text=final_reply)
        if reply_result.status != RuntimeStatus.QUEUED:
            final_reply = reply_result.text or final_reply
        return TurnOutcome(
            turn_id=channel_envelope.event.event_id,
            final_reply=final_reply,
            commit_payload=L1CommitPayload(
                turn_id=channel_envelope.event.event_id,
                transcript=_build_turn_transcript(channel_envelope, final_reply, observations=observations),
                tool_observations=list(observations),
            ),
        )


def render_final_reply(channel_envelope: ChannelEnvelope, outcome: CanonicalLLMOutcome) -> str:
    if channel_envelope.endpoint.channel_kind != "stdio":
        return outcome.text
    reasoning = str(outcome.reasoning_text or "").strip()
    answer = str(outcome.text or "").strip()
    if not reasoning:
        return outcome.text
    if not answer:
        return f"[thinking]\n{reasoning}"
    return f"[thinking]\n{reasoning}\n\n{answer}"


def _render_tool_summary(observations: list[ToolObservation], *, max_summary_chars: int = 500) -> str:
    if not observations:
        return ""
    per_item_limit = max_summary_chars // 3
    parts: list[str] = []
    total = 0
    for obs in observations:
        status = "ok" if obs.ok else "error"
        summary = obs.summary[:per_item_limit]
        line = f"{obs.tool_name}({status}): {summary}"
        if total + len(line) > max_summary_chars:
            remaining = len(observations) - len(parts)
            if remaining > 0:
                parts.append(f"... +{remaining} more")
            break
        parts.append(line)
        total += len(line)
    return "\n".join(parts)


def _build_turn_transcript(channel_envelope: ChannelEnvelope, final_reply: str, observations: list[ToolObservation] | None = None) -> list[L1TranscriptMessage]:
    user_text = extract_text_from_payload(channel_envelope.event.payload)
    transcript: list[L1TranscriptMessage] = []
    if user_text:
        transcript.append(L1TranscriptMessage(role="user", content=user_text))
    assistant_content = final_reply.strip()
    tool_summary = _render_tool_summary(observations or [])
    if assistant_content:
        transcript.append(L1TranscriptMessage(role="assistant", content=assistant_content, tool_trace=tool_summary or None))
    return transcript


def service_turn_program(
    trigger: ServiceTriggerEvent,
    definition: ServiceDefinition,
    *,
    core_mode: str = "default",
    max_output_tokens: int = 1024,
    reply_envelope: ChannelEnvelope | None = None,
) -> TurnProgram:
    observations: list[ToolObservation] = []
    compact_note = ""
    service_input = build_service_trigger_input(definition)
    while True:
        metadata = {
            "service_definition": definition,
            "service_input": service_input,
            "service_trigger": trigger,
            "compact_note": compact_note,
        }
        assembly_context = PromptAssemblyContext(
            core_mode=core_mode,
            turn_kind="service_trigger",
            metadata=metadata,
        )
        advice = yield LLMPreflightEffect(
            assembly_context=assembly_context,
            max_output_tokens=max_output_tokens,
        )
        compact_note = ""
        if getattr(advice.payload, "status", "") == LLMPreflightStatus.COMPACT_REQUIRED:
            compact_result = yield MemoryCompactEffect(
                assembly_context=assembly_context,
                target_input_budget=getattr(advice.payload, "target_input_budget", 0),
                reserved_output_tokens=getattr(advice.payload, "reserved_output_tokens", 0),
            )
            compact_note = str(compact_result.payload.summary) if compact_result.payload is not None else ""
            continue
        outcome_result = yield LLMRequestEffect(
            assembly_context=assembly_context,
            max_output_tokens=max_output_tokens,
        )
        outcome = _as_llm_outcome(outcome_result.payload)
        if outcome is not None and outcome.finish_reason == LLMFinishReason.COMPACT_REQUIRED:
            compact_result = yield MemoryCompactEffect(
                assembly_context=assembly_context,
                target_input_budget=outcome.target_input_budget,
                reserved_output_tokens=outcome.reserved_output_tokens,
            )
            compact_note = str(compact_result.payload.summary) if compact_result.payload is not None else ""
            continue
        if outcome is not None and outcome.tool_calls:
            for tool_call in outcome.tool_calls:
                tool_result = yield ToolCallEffect(tool_call=tool_call)
                _append_tool_observation(observations, tool_result.payload)
            continue
        final_reply = str(outcome.text or "") if outcome is not None else ""
        if reply_envelope is not None:
            reply_result = yield MailboxReplyEffect(channel_envelope=reply_envelope, text=final_reply)
            if reply_result.status != RuntimeStatus.QUEUED:
                final_reply = reply_result.text or final_reply
        return TurnOutcome(
            turn_id=str(trigger.metadata.get("turn_id") or trigger.service_id),
            final_reply=final_reply,
            commit_payload=L1CommitPayload(
                turn_id=str(trigger.metadata.get("turn_id") or trigger.service_id),
                transcript=_build_service_turn_transcript(service_input, final_reply, observations=observations),
                tool_observations=list(observations),
            ),
        )


def _build_service_turn_transcript(service_input: str, final_reply: str, observations: list[ToolObservation] | None = None) -> list[L1TranscriptMessage]:
    transcript: list[L1TranscriptMessage] = []
    if service_input.strip():
        transcript.append(L1TranscriptMessage(role="user", content=service_input.strip()))
    assistant_content = final_reply.strip()
    tool_summary = _render_tool_summary(observations or [])
    if assistant_content:
        transcript.append(L1TranscriptMessage(role="assistant", content=assistant_content, tool_trace=tool_summary or None))
    return transcript


FailureProgram = Generator[EffectRequest, EffectResult, FailureFlowOutcome]


def failure_turn_program(
    draft: FailureDraft,
    *,
    allowed_tools: list[dict[str, Any]],
    max_output_tokens: int = 768,
    max_maintenance_batches: int = 2,
) -> FailureProgram:
    observations: list[ToolObservation] = []
    request_stage = "diagnose"
    for batch_index in range(max_maintenance_batches):
        diagnose_context = PromptAssemblyContext(
            turn_kind="failure",
            metadata={
                "failure_draft": _failure_draft_debug_payload(draft),
                "failure_stage": request_stage,
                "failure_primary_input": _render_failure_primary_input(
                    draft,
                    allowed_tools=allowed_tools,
                    observations=observations,
                    stage=request_stage,
                ),
            },
        )
        diagnose_result = yield LLMRequestEffect(
            assembly_context=diagnose_context,
            max_output_tokens=max_output_tokens,
            tools_override=list(allowed_tools),
        )
        outcome = _as_llm_outcome(diagnose_result.payload)
        if outcome is None:
            return FailureFlowOutcome(
                verification=VerificationResult(status=FAILURE_VERIFICATION_FAILED, reason="Failure diagnosis did not return an LLM outcome."),
                enriched_fields={},
            )
        if outcome.tool_calls:
            for tool_call in outcome.tool_calls:
                tool_result = yield ToolCallEffect(tool_call=tool_call)
                _append_tool_observation(observations, tool_result.payload)
            verify_context = PromptAssemblyContext(
                turn_kind="failure",
                metadata={
                    "failure_draft": _failure_draft_debug_payload(draft),
                    "failure_stage": "verify",
                    "observation_blocks": [item.to_prompt_block() for item in observations],
                    "failure_primary_input": _render_failure_primary_input(
                        draft,
                        allowed_tools=allowed_tools,
                        observations=observations,
                        stage="verify",
                    ),
                },
            )
            verify_result = yield LLMRequestEffect(
                assembly_context=verify_context,
                max_output_tokens=max_output_tokens,
                tools_override=[],
            )
            verify_outcome = _as_llm_outcome(verify_result.payload)
            if verify_outcome is not None:
                verification, enriched_fields = _parse_failure_verification(verify_outcome.text)
                if verification.status != FAILURE_VERIFICATION_FAILED or batch_index >= max_maintenance_batches - 1:
                    return FailureFlowOutcome(verification=verification, enriched_fields=enriched_fields)
                request_stage = "maintain"
                continue
            return FailureFlowOutcome(
                verification=VerificationResult(status=FAILURE_VERIFICATION_FAILED, reason="Failure verification did not return an LLM outcome."),
                enriched_fields={},
            )
        verification, enriched_fields = _parse_failure_verification(outcome.text)
        return FailureFlowOutcome(verification=verification, enriched_fields=enriched_fields)
    return FailureFlowOutcome(
        verification=VerificationResult(status=FAILURE_VERIFICATION_FAILED, reason="Inline repair budget exhausted."),
        enriched_fields={},
    )


def _failure_draft_debug_payload(draft: FailureDraft) -> dict[str, Any]:
    return {
        "subsystem": draft.subsystem,
        "component": draft.component,
        "failure_kind": draft.failure_kind,
        "severity": draft.severity,
        "primary_blocker": draft.primary_blocker,
        "secondary_issues": list(draft.secondary_issues),
        "attempted_actions": list(draft.attempted_actions),
        "documents_checked": list(draft.documents_checked),
        "maintenance_outcomes": list(draft.maintenance_outcomes),
        "repair_domain": draft.repair_domain,
        "evidence": dict(draft.evidence),
        "related_ids": dict(draft.related_ids),
        "safe_to_retry": draft.safe_to_retry,
    }


def _render_failure_primary_input(
    draft: FailureDraft,
    *,
    allowed_tools: list[dict[str, Any]],
    observations: list[ToolObservation],
    stage: str,
) -> str:
    allowed_names = []
    for tool in allowed_tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = str((function or {}).get("name") or "").strip()
        if name:
            allowed_names.append(name)
    payload = {
        "stage": stage,
        "failure": _failure_draft_debug_payload(draft),
        "allowed_capabilities": allowed_names,
        "recent_observations": [
            {
                "tool_name": item.tool_name,
                "ok": item.ok,
                "summary": item.summary,
                "structured": item.structured,
            }
            for item in observations
        ],
        "instructions": [
            "Work only on the primary blocker.",
            "You may inspect and perform bounded maintenance with the allowed capabilities.",
            "Do not expand scope to secondary issues.",
            "When verifying, return a JSON object with verification_status and any explanatory fields.",
        ],
    }
    return json.dumps(payload, ensure_ascii=True, indent=2)


def _parse_failure_verification(text: str) -> tuple[VerificationResult, dict[str, Any]]:
    raw = str(text or "").strip()
    if not raw:
        return VerificationResult(status=FAILURE_VERIFICATION_FAILED, reason="Failure verification produced no output."), {}
    payload = _extract_json_object(raw)
    if isinstance(payload, dict):
        status = str(payload.get("verification_status") or payload.get("status") or "").strip().lower()
        if status not in {"ok", "degraded", "failed"}:
            status = FAILURE_VERIFICATION_FAILED
        reason = str(payload.get("reason") or payload.get("why_blocked") or raw).strip()
        fields = {
            "why_blocked": payload.get("why_blocked"),
            "current_blocker": payload.get("current_blocker"),
            "impact": payload.get("impact"),
            "possible_solutions": payload.get("possible_solutions"),
            "recommended_next_step": payload.get("recommended_next_step"),
        }
        return VerificationResult(status=status, reason=reason, evidence={"raw": raw}), {key: value for key, value in fields.items() if value}
    match = re.search(r"\b(ok|degraded|failed)\b", raw.lower())
    status = match.group(1) if match is not None else FAILURE_VERIFICATION_FAILED
    return VerificationResult(status=status, reason=raw, evidence={"raw": raw}), {}


def _extract_json_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    candidate = text[start : end + 1]
    try:
        payload = json.loads(candidate)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _as_llm_outcome(payload: Any) -> CanonicalLLMOutcome | None:
    return payload if isinstance(payload, CanonicalLLMOutcome) else None


def _as_tool_result(payload: Any) -> CanonicalToolResult | None:
    return payload if isinstance(payload, CanonicalToolResult) else None


def _append_tool_observation(observations: list[ToolObservation], payload: Any) -> None:
    tool_result = _as_tool_result(payload)
    if tool_result is None:
        return
    observations.append(
        ToolObservation(
            tool_name=tool_result.name,
            ok=tool_result.ok,
            summary=tool_result.text or ("tool succeeded" if tool_result.ok else "tool failed"),
            structured=tool_result.structured,
        )
    )
