from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR

from collections.abc import Generator
from dataclasses import dataclass, field
import json
import re
from typing import Any, Callable
from uuid import uuid4

from pal.failure.contracts import (
    FAILURE_VERIFICATION_FAILED,
    FailureDraft,
    VerificationResult,
)
from pal.llm.contracts import LLMGenerationResult
from pal.shared import EffectKind, LLMFinishReason, LLMPreflightStatus, PromptAssemblyContext, RuntimeStatus, ToolExecutionResult, TurnDeliveryBinding
from pal.foundation import EventEnvelope
from pal.shared.payloads import extract_text_from_payload
from pal.memory import L1MessageKind, L1TranscriptMessage
from pal.shared.agent_io import ChannelMessage, ChannelStreamUpdate


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
    reply_texts: tuple[str, ...] = ()


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
    tools_override: list[dict[str, Any]] | None = None
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
    tool_call: ToolCallIR | None = None
    kind: str = EffectKind.TOOL_CALL


@dataclass(frozen=True)
class MemoryCompactEffect(EffectRequest):
    assembly_context: PromptAssemblyContext = field(default_factory=PromptAssemblyContext)
    target_input_budget: int = 0
    reserved_output_tokens: int = 0
    kind: str = EffectKind.MEMORY_COMPACT


@dataclass(frozen=True)
class MailboxReplyEffect(EffectRequest):
    text: str = ""
    message: ChannelMessage | None = None
    terminal: bool = True
    # Text already represented by the current LLM stream. Buffered channels
    # may suppress this copy while still delivering explicit tool echoes,
    # which are also non-terminal but are not stream companions.
    stream_companion: bool = False
    kind: str = EffectKind.MAILBOX_REPLY


@dataclass(frozen=True)
class MailboxReplyStreamUpdateEffect(EffectRequest):
    update: ChannelStreamUpdate = field(default_factory=ChannelStreamUpdate)
    kind: str = EffectKind.MAILBOX_REPLY_STREAM


TurnProgram = Generator[EffectRequest, EffectResult, TurnOutcome]


@dataclass(frozen=True)
class AgentLoopFrame:
    retry_note: str = ""
    observations: tuple[ToolObservation, ...] = ()
    reply_texts: tuple[str, ...] = ()


BuildAgentContext = Callable[[AgentLoopFrame], PromptAssemblyContext]
RenderAgentFinalText = Callable[[LLMGenerationResult | None], str]
BuildAgentCommitPayload = Callable[[str, list[ToolObservation], list[str]], L1CommitPayload]
BuildMailboxEffect = Callable[[str], MailboxReplyEffect | None]
BuildRetryNote = Callable[[LLMGenerationResult | None, list[ToolObservation], int], str]


@dataclass
class TurnContinuation:
    turn_id: str
    program: TurnProgram
    correlation_id: str
    opening_event: EventEnvelope | None = None
    delivery_binding: TurnDeliveryBinding | None = None
    control_scope_key: str = ""
    started: bool = False
    waiting_effect_id: str | None = None
    finalization_only: bool = False
    finalization_attempted: bool = False
    finalization_reason: str = ""
    interrupted: bool = False
    interrupt_reason: str = ""
    tool_batch_count: int = 0
    last_response_mode: str = "chat"
    preferred_llm_endpoint_id: str | None = None
    preferred_llm_model_id: str | None = None
    turn_settings_snapshot: dict[str, Any] = field(default_factory=dict)
    tool_observations: list[ToolObservation] = field(default_factory=list)
    pending_tool_call_batch: list[ToolCallIR] = field(default_factory=list)
    pending_tool_results: list[ToolExecutionResult] = field(default_factory=list)
    pending_assistant_tool_text: str = ""
    emitted_reply_texts: list[str] = field(default_factory=list)
    pending_compact_memory_candidate_batches: list[dict[str, Any]] = field(default_factory=list)
    budget_failure_feedback_text: str = ""
    prompt_budget_snapshot: dict[str, Any] = field(default_factory=dict)
    echoed_keys: set[str] = field(default_factory=set)
    channel_stream_active: bool = False
    channel_stream_terminal_text: str = ""
    channel_stream_terminal_finish_reason: str = ""

@dataclass(frozen=True)
class FailureFlowOutcome:
    verification: VerificationResult
    enriched_fields: dict[str, Any] = field(default_factory=dict)


def channel_turn_program(
    opening_event: EventEnvelope,
    *,
    core_mode: str = "default",
    max_output_tokens: int = 1024,
) -> TurnProgram:
    def build_context(frame: AgentLoopFrame) -> PromptAssemblyContext:
        metadata = {}
        if frame.retry_note:
            metadata["retry_note"] = frame.retry_note
        return PromptAssemblyContext(
            event=opening_event,
            core_mode=core_mode,
            metadata=metadata,
        )

    def build_commit_payload(final_reply: str, observations: list[ToolObservation], reply_texts: list[str]) -> L1CommitPayload:
        return L1CommitPayload(
            turn_id=opening_event.event_id,
            transcript=_build_turn_transcript(opening_event, final_reply, observations=observations, reply_texts=reply_texts),
            tool_observations=list(observations),
        )

    return (
        yield from agent_turn_program(
            turn_id=opening_event.event_id,
            build_assembly_context=build_context,
            render_final_text=lambda outcome: outcome.text if outcome is not None else "",
            build_commit_payload=build_commit_payload,
            max_output_tokens=max_output_tokens,
            emit_mid_text=lambda text: MailboxReplyEffect(
                text=text,
                terminal=False,
                stream_companion=True,
            ),
            emit_final_text=lambda text: MailboxReplyEffect(text=text),
        )
    )


def agent_turn_program(
    *,
    turn_id: str,
    build_assembly_context: BuildAgentContext,
    render_final_text: RenderAgentFinalText,
    build_commit_payload: BuildAgentCommitPayload,
    max_output_tokens: int = 1024,
    emit_mid_text: BuildMailboxEffect | None = None,
    emit_final_text: BuildMailboxEffect | None = None,
    tools_override: list[dict[str, Any]] | None = None,
    build_retry_note: BuildRetryNote | None = None,
) -> TurnProgram:
    observations: list[ToolObservation] = []
    reply_texts: list[str] = []
    retry_note = ""
    retry_count = 0
    compact_generation_count = 0
    while True:
        frame = AgentLoopFrame(
            retry_note=retry_note,
            observations=tuple(observations),
            reply_texts=tuple(reply_texts),
        )
        assembly_context = build_assembly_context(frame)
        retry_note = ""
        advice = yield LLMPreflightEffect(
            assembly_context=assembly_context,
            max_output_tokens=max_output_tokens,
            tools_override=tools_override,
        )
        if getattr(advice.payload, "status", "") == LLMPreflightStatus.COMPACT_REQUIRED:
            if compact_generation_count >= 3:
                return (yield from _finish_compaction_failure_turn(
                    turn_id=turn_id,
                    text=(
                        "Context remains over budget after three atomic L1 "
                        "compactions; stopping instead of compacting the same "
                        "logical turn again."
                    ),
                    emit_final_text=emit_final_text,
                    build_commit_payload=build_commit_payload,
                    observations=observations,
                    reply_texts=reply_texts,
                ))
            compact_result = yield MemoryCompactEffect(
                assembly_context=assembly_context,
                target_input_budget=getattr(advice.payload, "target_input_budget", 0),
                reserved_output_tokens=getattr(advice.payload, "reserved_output_tokens", 0),
            )
            if compact_result.status != RuntimeStatus.OK:
                return (yield from _finish_compaction_failure_turn(
                    turn_id=turn_id,
                    text=compact_result.text,
                    emit_final_text=emit_final_text,
                    build_commit_payload=build_commit_payload,
                    observations=observations,
                    reply_texts=reply_texts,
                ))
            compact_generation_count += 1
            continue
        outcome_result = yield LLMRequestEffect(
            assembly_context=assembly_context,
            max_output_tokens=max_output_tokens,
            tools_override=tools_override,
        )
        outcome = _as_llm_outcome(outcome_result.payload)
        if outcome is not None and outcome.finish_reason == LLMFinishReason.COMPACT_REQUIRED:
            if compact_generation_count >= 3:
                return (yield from _finish_compaction_failure_turn(
                    turn_id=turn_id,
                    text=(
                        "Context remains over budget after three atomic L1 "
                        "compactions; stopping instead of compacting the same "
                        "logical turn again."
                    ),
                    emit_final_text=emit_final_text,
                    build_commit_payload=build_commit_payload,
                    observations=observations,
                    reply_texts=reply_texts,
                ))
            compact_result = yield MemoryCompactEffect(
                assembly_context=assembly_context,
                target_input_budget=outcome.target_input_budget,
                reserved_output_tokens=outcome.reserved_output_tokens,
            )
            if compact_result.status != RuntimeStatus.OK:
                return (yield from _finish_compaction_failure_turn(
                    turn_id=turn_id,
                    text=compact_result.text,
                    emit_final_text=emit_final_text,
                    build_commit_payload=build_commit_payload,
                    observations=observations,
                    reply_texts=reply_texts,
                ))
            compact_generation_count += 1
            continue
        if outcome is not None and outcome.tool_calls:
            retry_count = 0
            mid_text = str(outcome.text or "").strip()
            if mid_text:
                rendered = mid_text if emit_mid_text is None else ""
                effect = emit_mid_text(mid_text) if emit_mid_text is not None else None
                if effect is not None:
                    reply_result = yield effect
                    if reply_result.status == RuntimeStatus.QUEUED:
                        rendered = reply_result.text or mid_text
                if rendered.strip():
                    reply_texts.append(rendered)
            for tool_call in outcome.tool_calls:
                tool_result = yield ToolCallEffect(tool_call=tool_call)
                _append_tool_observation(observations, tool_result.payload)
            continue
        if build_retry_note is not None:
            note = build_retry_note(outcome, observations, retry_count)
            if str(note or "").strip():
                retry_note = str(note).strip()
                retry_count += 1
                continue
        final_reply = render_final_text(outcome)
        if not str(final_reply or "").strip():
            final_reply = "The LLM completed this turn without producing a final answer."
        effect = emit_final_text(final_reply) if emit_final_text is not None else None
        if effect is not None:
            reply_result = yield effect
            if reply_result.status != RuntimeStatus.QUEUED:
                final_reply = reply_result.text or final_reply
            elif reply_result.text:
                final_reply = reply_result.text
        if final_reply.strip():
            reply_texts.append(final_reply)
        return TurnOutcome(
            turn_id=turn_id,
            final_reply=final_reply,
            commit_payload=build_commit_payload(final_reply, observations, reply_texts),
            reply_texts=tuple(reply_texts),
        )


def _finish_compaction_failure_turn(
    *,
    turn_id: str,
    text: str,
    emit_final_text: BuildMailboxEffect | None,
    build_commit_payload: BuildAgentCommitPayload,
    observations: list[ToolObservation],
    reply_texts: list[str],
) -> TurnProgram:
    detail = str(
        text or "Memory compaction failed; the current turn cannot safely continue."
    ).strip()
    recovery = "Please run `/compact` manually, then resend your request."
    final_reply = detail if recovery in detail else f"{detail}\n\n{recovery}"
    effect = emit_final_text(final_reply) if emit_final_text is not None else None
    if effect is not None:
        reply_result = yield effect
        if reply_result.status != RuntimeStatus.QUEUED:
            final_reply = reply_result.text or final_reply
        elif reply_result.text:
            final_reply = reply_result.text
    if final_reply:
        reply_texts.append(final_reply)
    return TurnOutcome(
        turn_id=turn_id,
        final_reply=final_reply,
        commit_payload=build_commit_payload(final_reply, observations, reply_texts),
        reply_texts=tuple(reply_texts),
    )


def _build_turn_transcript(
    opening_event: EventEnvelope,
    final_reply: str,
    observations: list[ToolObservation] | None = None,
    reply_texts: list[str] | tuple[str, ...] | None = None,
) -> list[L1TranscriptMessage]:
    _ = observations
    user_text = extract_text_from_payload(opening_event.payload)
    transcript: list[L1TranscriptMessage] = []
    if user_text:
        transcript.append(L1TranscriptMessage(role="user", content=user_text, kind=L1MessageKind.USER_REQUEST))
    replies = tuple(str(item).strip() for item in (reply_texts or (final_reply,)) if str(item).strip())
    for assistant_content in replies:
        transcript.append(
            L1TranscriptMessage(
                role="assistant",
                content=assistant_content,
                kind=L1MessageKind.ASSISTANT_REPLY,
            )
        )
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
        "maintenance_outcomes": [_project_maintenance_outcome(item) for item in list(draft.maintenance_outcomes)],
        "repair_domain": draft.repair_domain,
        "evidence": _project_failure_evidence(draft.evidence),
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
                "summary": _compact_prompt_text(item.summary, limit=500),
                "structured_summary": _project_tool_structured_summary(item.structured),
            }
            for item in observations
        ],
        "instructions": [
            "Work only on the primary blocker in this sanitized failure packet.",
            "Use introspection capabilities first to inspect live Pal runtime state.",
            "Use dedicated built-in operation or management capabilities only when they directly repair the blocker.",
            "Do not directly edit code, config, databases, runtime files, or user data.",
            "Do not restart Pal itself.",
            "Do not answer the user's original request.",
            "When verifying, return a JSON object with verification_status and explanatory fields.",
        ],
    }
    return json.dumps(payload, ensure_ascii=True)


def _project_maintenance_outcome(item: dict[str, Any]) -> dict[str, Any]:
    structured = item.get("structured") if isinstance(item, dict) else None
    return {
        "action_name": str(item.get("action_name") or "").strip(),
        "status": str(item.get("status") or "").strip(),
        "ok": bool(item.get("ok")),
        "text": _compact_prompt_text(str(item.get("text") or ""), limit=500),
        "structured_summary": _project_tool_structured_summary(structured),
    }


def _project_failure_evidence(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    projected: dict[str, Any] = {}
    for key, raw in value.items():
        normalized_key = str(key or "").strip()
        if not normalized_key:
            continue
        if isinstance(raw, dict):
            projected[normalized_key] = _project_mapping_shape(raw)
        elif isinstance(raw, list):
            projected[normalized_key] = _project_list_shape(raw)
        else:
            projected[normalized_key] = _compact_prompt_text(str(raw or ""), limit=500)
    return projected


def _project_tool_structured_summary(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        return {}
    if set(value) == {"payload"} and isinstance(value.get("payload"), dict):
        return _project_tool_structured_summary(value["payload"])
    if isinstance(value.get("capability"), dict):
        capability = dict(value.get("capability") or {})
        return {
            "kind": "capability_contract",
            "name": str(capability.get("name") or "").strip(),
            "required_params": _project_list_shape(capability.get("required_params")),
        }
    if isinstance(value.get("tools"), list):
        tools = [item for item in value.get("tools") or [] if isinstance(item, dict)]
        return {
            "kind": "tool_inventory",
            "tool_count": len(tools),
            "tool_names_preview": [str(item.get("name") or "").strip() for item in tools[:12] if str(item.get("name") or "").strip()],
        }
    return _project_mapping_shape(value)


def _project_mapping_shape(value: dict[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for key, raw in list(value.items())[:16]:
        normalized_key = str(key or "").strip()
        if not normalized_key:
            continue
        if isinstance(raw, dict):
            projected[normalized_key] = {"keys": [str(item) for item in list(raw.keys())[:12]], "key_count": len(raw)}
        elif isinstance(raw, list):
            projected[normalized_key] = _project_list_shape(raw)
        else:
            projected[normalized_key] = _compact_prompt_text(str(raw or ""), limit=240)
    if len(value) > 16:
        projected["truncated_key_count"] = len(value) - 16
    return projected


def _project_list_shape(value: object) -> list[Any] | dict[str, Any]:
    if not isinstance(value, list):
        return []
    preview: list[Any] = []
    for item in value[:12]:
        if isinstance(item, dict):
            preview.append({"keys": [str(key) for key in list(item.keys())[:8]]})
        else:
            preview.append(_compact_prompt_text(str(item or ""), limit=160))
    if len(value) <= 12:
        return preview
    return {"count": len(value), "preview": preview}


def _compact_prompt_text(text: str, *, limit: int) -> str:
    normalized = str(text or "").strip()
    if len(normalized) <= limit:
        return normalized
    if limit <= 20:
        return normalized[:limit].rstrip()
    return f"{normalized[: limit - 18].rstrip()} ... [truncated]"


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


def _as_llm_outcome(payload: Any) -> LLMGenerationResult | None:
    return payload if isinstance(payload, LLMGenerationResult) else None


def _as_tool_result(payload: Any) -> ToolExecutionResult | None:
    return payload if isinstance(payload, ToolExecutionResult) else None


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
