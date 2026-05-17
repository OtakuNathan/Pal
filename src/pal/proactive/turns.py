from __future__ import annotations

from pal.channel import ChannelEnvelope
from pal.channel.contracts import EndpointConfig, ResponseHandle
from pal.core.turns import (
    AgentLoopFrame,
    L1CommitPayload,
    MailboxReplyEffect,
    ToolObservation,
    TurnContinuation,
    TurnOutcome,
    TurnProgram,
    agent_turn_program,
)
from pal.foundation import EventEnvelope
from pal.memory import L1TranscriptMessage
from pal.proactive.contracts import ProactiveDefinition
from pal.proactive.input_builder import build_proactive_trigger_input
from pal.shared import EventKind, PromptAssemblyContext, ProactiveTriggerEvent, SourceKind


def build_proactive_turn_continuation(
    context,
    trigger: ProactiveTriggerEvent,
    definition: ProactiveDefinition,
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
            "proactive_id": definition.proactive_id,
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
        endpoint=_build_proactive_endpoint_config(definition),
        response_handle=_build_proactive_response_handle(definition),
    )
    return TurnContinuation(
        turn_id=proactive_event.event_id,
        channel_envelope=synthetic_envelope,
        program=proactive_turn_program(
            resolved_trigger,
            definition,
            core_mode=core_mode,
            max_output_tokens=max_output_tokens,
            reply_envelope=_resolve_proactive_reply_envelope(context, proactive_event, definition, resolved_trigger),
        ),
        correlation_id=proactive_event.correlation_id or proactive_event.event_id,
    )


def proactive_turn_program(
    trigger: ProactiveTriggerEvent,
    definition: ProactiveDefinition,
    *,
    core_mode: str = "default",
    max_output_tokens: int = 1024,
    reply_envelope: ChannelEnvelope | None = None,
) -> TurnProgram:
    proactive_input = build_proactive_trigger_input(definition)

    def build_context(frame: AgentLoopFrame) -> PromptAssemblyContext:
        metadata = {
            "proactive_definition": definition,
            "proactive_input": proactive_input,
            "proactive_trigger": trigger,
        }
        if frame.retry_note:
            metadata["retry_note"] = frame.retry_note
        return PromptAssemblyContext(
            core_mode=core_mode,
            turn_kind="proactive_trigger",
            metadata=metadata,
        )

    def build_commit_payload(final_reply: str, observations: list[ToolObservation], reply_texts: list[str]) -> L1CommitPayload:
        return L1CommitPayload(
            turn_id=str(trigger.metadata.get("turn_id") or trigger.proactive_id),
            transcript=_build_proactive_turn_transcript(proactive_input, final_reply, observations=observations, reply_texts=reply_texts),
            tool_observations=list(observations),
        )

    return (
        yield from agent_turn_program(
            turn_id=str(trigger.metadata.get("turn_id") or trigger.proactive_id),
            build_assembly_context=build_context,
            render_final_text=lambda outcome: str(outcome.text or "") if outcome is not None else "",
            build_commit_payload=build_commit_payload,
            max_output_tokens=max_output_tokens,
            emit_mid_text=(lambda text: MailboxReplyEffect(channel_envelope=reply_envelope, text=text)) if reply_envelope is not None else None,
            emit_final_text=(lambda text: MailboxReplyEffect(channel_envelope=reply_envelope, text=text)) if reply_envelope is not None else None,
        )
    )


def settled_output_text(outcome: TurnOutcome) -> str:
    replies = tuple(str(item).strip() for item in getattr(outcome, "reply_texts", ()) if str(item).strip())
    if replies:
        return "\n\n".join(replies)
    return str(getattr(outcome, "final_reply", "") or "")


def _build_proactive_turn_transcript(
    proactive_input: str,
    final_reply: str,
    observations: list[ToolObservation] | None = None,
    reply_texts: list[str] | tuple[str, ...] | None = None,
) -> list[L1TranscriptMessage]:
    transcript: list[L1TranscriptMessage] = []
    if proactive_input.strip():
        transcript.append(L1TranscriptMessage(role="user", content=proactive_input.strip()))
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


def _build_proactive_endpoint_config(definition: ProactiveDefinition) -> EndpointConfig:
    return EndpointConfig(
        endpoint_id=f"proactive:{definition.proactive_id}",
        channel_kind=SourceKind.PROACTIVE,
        binding_key=definition.proactive_id,
    )


def _build_proactive_response_handle(definition: ProactiveDefinition) -> ResponseHandle:
    return ResponseHandle(endpoint_id=f"proactive:{definition.proactive_id}")


def _resolve_proactive_reply_envelope(
    context,
    proactive_event: EventEnvelope,
    definition: ProactiveDefinition,
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
        event=proactive_event,
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
