from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR

from dataclasses import dataclass, replace
from typing import Any, Mapping

from pal.llm.ir import (
    LLMMessageIR,
    LLMRequestIR,
    LLMResponseDeltaKind,
    LLMResponseIR,
    LLMResponseUpdate,
    LLMUsageIR,
    MessageRole,
    MessageState,
    TextPartIR,
)


OUTPUT_CONTINUATION_INSTRUCTION = (
    "Output limit reached. Continue directly from the exact point where the "
    "prior response stopped. Do not recap or apologize. Emit complete tool "
    "calls only."
)


@dataclass(frozen=True)
class OutputRecoverySettings:
    enabled: bool
    upper_limit: int
    max_continuations: int


def recovery_settings(
    endpoint: Any,
    request: LLMRequestIR,
    *,
    default_attempts: int,
) -> OutputRecoverySettings:
    capabilities = dict(getattr(endpoint, "capabilities_blob", None) or {})
    raw_recovery = capabilities.get("max_output_recovery")
    recovery = dict(raw_recovery) if isinstance(raw_recovery, Mapping) else {}
    enabled = bool(
        recovery.get(
            "enabled",
            raw_recovery if isinstance(raw_recovery, bool) else False,
        )
    )
    explicit = request.metadata.get("max_output_recovery_enabled")
    if explicit is not None:
        enabled = bool(explicit)

    endpoint_default = _positive_int(getattr(endpoint, "max_output_tokens", None))
    if (
        endpoint_default is not None
        and request.policy.max_output_tokens < endpoint_default
        and explicit is not True
    ):
        enabled = False

    upper_limit = endpoint_output_upper_limit(endpoint) or request.policy.max_output_tokens
    upper_limit = max(request.policy.max_output_tokens, upper_limit)
    raw_continuations = recovery.get(
        "max_continuations",
        capabilities.get("max_output_recovery_attempts", default_attempts),
    )
    try:
        max_continuations = max(0, int(raw_continuations))
    except (TypeError, ValueError):
        max_continuations = max(0, int(default_attempts))
    return OutputRecoverySettings(
        enabled=enabled
        and (
            upper_limit > request.policy.max_output_tokens
            or max_continuations > 0
        ),
        upper_limit=upper_limit,
        max_continuations=max_continuations,
    )


def endpoint_output_upper_limit(endpoint: Any) -> int | None:
    capabilities = dict(getattr(endpoint, "capabilities_blob", None) or {})
    raw_recovery = capabilities.get("max_output_recovery")
    recovery = dict(raw_recovery) if isinstance(raw_recovery, Mapping) else {}
    return _positive_int(
        recovery.get(
            "upper_limit",
            capabilities.get("max_output_tokens_upper_limit"),
        )
    )


def with_recovery_stage(
    request: LLMRequestIR,
    *,
    stage: str,
    attempt: int,
    max_output_tokens: int,
) -> LLMRequestIR:
    return replace(
        request,
        policy=replace(
            request.policy,
            max_output_tokens=max(1, int(max_output_tokens)),
        ),
        metadata={
            **dict(request.metadata),
            "max_output_recovery_stage": str(stage),
            "max_output_recovery_attempt": max(0, int(attempt)),
        },
    )


def continuation_request(
    request: LLMRequestIR,
    truncated: LLMResponseIR,
    *,
    max_output_tokens: int,
    attempt: int,
) -> LLMRequestIR:
    metadata = dict(request.metadata)
    metadata.pop("prompt_budget_snapshot", None)
    candidate = replace(request, metadata=metadata)
    candidate = replace(
        candidate,
        messages=(
            *candidate.messages,
            safe_truncated_message(truncated.message),
            LLMMessageIR(
                role=MessageRole.USER,
                parts=(TextPartIR(OUTPUT_CONTINUATION_INSTRUCTION),),
                semantic_kind="output_continuation",
            ),
        ),
    )
    return with_recovery_stage(
        candidate,
        stage="continue",
        attempt=attempt,
        max_output_tokens=max_output_tokens,
    )


def safe_truncated_response(response: LLMResponseIR) -> LLMResponseIR:
    return replace(response, message=safe_truncated_message(response.message))


def safe_truncated_message(message: LLMMessageIR) -> LLMMessageIR:
    return replace(
        message,
        parts=tuple(
            part
            for part in message.parts
            if not isinstance(part, ToolCallIR)
        ),
        replay=None,
        state=MessageState.COMPLETE,
    )


def merge_responses(
    responses: list[LLMResponseIR],
    *,
    discarded: tuple[LLMResponseIR, ...] = (),
) -> LLMResponseIR:
    if not responses:
        raise ValueError("output recovery requires at least one response")
    parts = tuple(
        part
        for index, response in enumerate(responses)
        for part in response.message.parts
        if index == len(responses) - 1
        or not isinstance(part, ToolCallIR)
    )
    usage_sources = (*discarded, *responses)
    usage = LLMUsageIR(
        input_tokens=sum(item.usage.input_tokens for item in usage_sources),
        uncached_input_tokens=sum(
            item.usage.uncached_input_tokens for item in usage_sources
        ),
        cached_input_tokens=sum(
            item.usage.cached_input_tokens for item in usage_sources
        ),
        cache_write_input_tokens=sum(
            item.usage.cache_write_input_tokens for item in usage_sources
        ),
        output_tokens=sum(item.usage.output_tokens for item in usage_sources),
        reasoning_tokens=sum(
            item.usage.reasoning_tokens for item in usage_sources
        ),
        cost=sum(item.usage.cost for item in usage_sources),
        reported=any(item.usage.reported for item in usage_sources),
    )
    final = responses[-1]
    return replace(
        final,
        message=replace(
            final.message,
            message_id=responses[0].message.message_id,
            parts=parts,
        ),
        usage=usage,
        provider_response_count=sum(
            item.provider_response_count for item in usage_sources
        ),
    )


def stream_recovery_updates(
    initial: LLMResponseIR,
    recovered: LLMResponseIR,
) -> tuple[LLMResponseUpdate, ...]:
    updates: list[LLMResponseUpdate] = []
    reasoning_delta = recovered.reasoning_text[len(initial.reasoning_text) :]
    text_delta = recovered.text[len(initial.text) :]
    if reasoning_delta:
        updates.append(
            LLMResponseUpdate(
                recovered,
                delta_kind=LLMResponseDeltaKind.REASONING,
                text_delta=reasoning_delta,
            )
        )
    if text_delta:
        updates.append(
            LLMResponseUpdate(
                recovered,
                delta_kind=LLMResponseDeltaKind.TEXT,
                text_delta=text_delta,
            )
        )
    for call in recovered.tool_calls:
        updates.append(
            LLMResponseUpdate(
                recovered,
                delta_kind=LLMResponseDeltaKind.TOOL_CALL,
                tool_call=call,
            )
        )
    updates.append(
        LLMResponseUpdate(recovered, delta_kind=LLMResponseDeltaKind.STATE)
    )
    return tuple(updates)


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
