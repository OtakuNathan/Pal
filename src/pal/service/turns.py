from __future__ import annotations

from typing import Any

from pal.channel import ChannelEnvelope
from pal.channel.contracts import EndpointConfig, ResponseHandle
from pal.core.turns import (
    EffectResult,
    L1CommitPayload,
    LLMPreflightEffect,
    LLMRequestEffect,
    MailboxReplyEffect,
    MemoryCompactEffect,
    ToolCallEffect,
    ToolObservation,
    TurnContinuation,
    TurnOutcome,
    TurnProgram,
)
from pal.foundation import EventEnvelope
from pal.llm.contracts import CanonicalLLMOutcome, CanonicalToolResult
from pal.memory import L1TranscriptMessage
from pal.service.contracts import ServiceDefinition
from pal.service.input_builder import build_proactive_trigger_input
from pal.shared import EventKind, LLMFinishReason, LLMPreflightStatus, PromptAssemblyContext, RuntimeStatus, ProactiveTriggerEvent, SourceKind


def build_service_turn_continuation(
    context,
    trigger: ProactiveTriggerEvent,
    definition: ServiceDefinition,
    *,
    core_mode: str = "default",
    max_output_tokens: int = 1024,
) -> TurnContinuation:
    proactive_input = build_proactive_trigger_input(definition)
    proactive_event = EventEnvelope(
        event_kind=EventKind.PROACTIVE_TRIGGER,
        source_kind=SourceKind.PROACTIVE,
        payload={
            "text": proactive_input,
            "proactive_id": definition.service_id,
            "trigger_kind": trigger.trigger_kind,
            "metadata": dict(trigger.metadata or {}),
        },
        correlation_id=str(trigger.metadata.get("request_id") or trigger.proactive_id),
    )
    trigger_metadata = dict(trigger.metadata or {})
    trigger_metadata.setdefault("turn_id", proactive_event.event_id)
    resolved_trigger = ProactiveTriggerEvent(
        proactive_id=trigger.proactive_id,
        trigger_kind=trigger.trigger_kind,
        metadata=trigger_metadata,
    )
    synthetic_envelope = ChannelEnvelope(
        event=proactive_event,
        endpoint=_build_service_endpoint_config(definition),
        response_handle=_build_service_response_handle(definition),
    )
    return TurnContinuation(
        turn_id=proactive_event.event_id,
        channel_envelope=synthetic_envelope,
        program=service_turn_program(
            resolved_trigger,
            definition,
            core_mode=core_mode,
            max_output_tokens=max_output_tokens,
            reply_envelope=_resolve_service_reply_envelope(context, proactive_event, definition, resolved_trigger),
        ),
        correlation_id=proactive_event.correlation_id or proactive_event.event_id,
    )


def service_turn_program(
    trigger: ProactiveTriggerEvent,
    definition: ServiceDefinition,
    *,
    core_mode: str = "default",
    max_output_tokens: int = 1024,
    reply_envelope: ChannelEnvelope | None = None,
) -> TurnProgram:
    observations: list[ToolObservation] = []
    reply_texts: list[str] = []
    compact_note = ""
    proactive_input = build_proactive_trigger_input(definition)
    while True:
        metadata = {
            "service_definition": definition,
            "proactive_input": proactive_input,
            "proactive_trigger": trigger,
            "compact_note": compact_note,
        }
        assembly_context = PromptAssemblyContext(
            core_mode=core_mode,
            turn_kind="proactive_trigger",
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
            mid_text = str(outcome.text or "").strip()
            if mid_text:
                if reply_envelope is not None:
                    reply_result = yield MailboxReplyEffect(channel_envelope=reply_envelope, text=mid_text)
                    if reply_result.status == RuntimeStatus.QUEUED:
                        reply_texts.append(reply_result.text or mid_text)
                else:
                    reply_texts.append(mid_text)
            for tool_call in outcome.tool_calls:
                tool_result = yield ToolCallEffect(tool_call=tool_call)
                _append_tool_observation(observations, tool_result)
            continue
        final_reply = str(outcome.text or "") if outcome is not None else ""
        if reply_envelope is not None:
            reply_result = yield MailboxReplyEffect(channel_envelope=reply_envelope, text=final_reply)
            if reply_result.status != RuntimeStatus.QUEUED:
                final_reply = reply_result.text or final_reply
        if final_reply.strip():
            reply_texts.append(final_reply)
        return TurnOutcome(
            turn_id=str(trigger.metadata.get("turn_id") or trigger.proactive_id),
            final_reply=final_reply,
            commit_payload=L1CommitPayload(
                turn_id=str(trigger.metadata.get("turn_id") or trigger.proactive_id),
                transcript=_build_service_turn_transcript(proactive_input, final_reply, observations=observations, reply_texts=reply_texts),
                tool_observations=list(observations),
            ),
            reply_texts=tuple(reply_texts),
        )


def settled_output_text(outcome: TurnOutcome) -> str:
    replies = tuple(str(item).strip() for item in getattr(outcome, "reply_texts", ()) if str(item).strip())
    if replies:
        return "\n\n".join(replies)
    return str(getattr(outcome, "final_reply", "") or "")


def _build_service_turn_transcript(
    service_input: str,
    final_reply: str,
    observations: list[ToolObservation] | None = None,
    reply_texts: list[str] | tuple[str, ...] | None = None,
) -> list[L1TranscriptMessage]:
    transcript: list[L1TranscriptMessage] = []
    if service_input.strip():
        transcript.append(L1TranscriptMessage(role="user", content=service_input.strip()))
    tool_summary = _render_tool_summary(observations or [])
    replies = tuple(str(item).strip() for item in (reply_texts or (final_reply,)) if str(item).strip())
    for index, assistant_content in enumerate(replies):
        transcript.append(
            L1TranscriptMessage(
                role="assistant",
                content=assistant_content,
                tool_trace=(tool_summary or None) if index == len(replies) - 1 else None,
            )
        )
    return transcript


def _build_service_endpoint_config(definition: ServiceDefinition) -> EndpointConfig:
    return EndpointConfig(
        endpoint_id=f"proactive:{definition.service_id}",
        channel_kind=SourceKind.PROACTIVE,
        binding_key=definition.service_id,
    )


def _build_service_response_handle(definition: ServiceDefinition) -> ResponseHandle:
    return ResponseHandle(endpoint_id=f"proactive:{definition.service_id}")


def _resolve_service_reply_envelope(
    context,
    service_event: EventEnvelope,
    definition: ServiceDefinition,
    trigger: ProactiveTriggerEvent,
) -> ChannelEnvelope | None:
    out_channel_id = str(definition.out_channel_id or "").strip()
    if not out_channel_id:
        return None
    channel_runtime = context.port_registry.get("channel:channel")
    if channel_runtime is None:
        return None
    endpoint_runtime = channel_runtime.get_endpoint(out_channel_id)
    if endpoint_runtime is None:
        return None
    reply_target = endpoint_runtime.derive_default_reply_target()
    reply_target.update(dict(definition.out_reply_target or {}))
    reply_target.update(dict(trigger.metadata.get("reply_target") or {}))
    if endpoint_runtime.endpoint.channel_kind == "socket" and not reply_target:
        return None
    return ChannelEnvelope(
        event=service_event,
        endpoint=endpoint_runtime.endpoint,
        response_handle=endpoint_runtime.build_response_handle(reply_target=reply_target),
    )


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


def _as_llm_outcome(payload: Any) -> CanonicalLLMOutcome | None:
    return payload if isinstance(payload, CanonicalLLMOutcome) else None


def _as_tool_result(payload: Any) -> CanonicalToolResult | None:
    return payload if isinstance(payload, CanonicalToolResult) else None


def _append_tool_observation(observations: list[ToolObservation], result: EffectResult) -> None:
    tool_result = _as_tool_result(result.payload)
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
