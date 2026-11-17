from __future__ import annotations
from typing import Any
from pal.llm.contracts import LLMGenerationResult, LLMService
from pal.llm.ir import LLMMessageIR, LLMResponseIR, MessageRole, TextPartIR
from pal.shared import LLMFinishReason, BunshinInvocationPack
from pal.bunshin.runner_components.numeric_values import _optional_positive_int


def _optional_positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _resolve_bunshin_max_output_tokens(llm_runtime: LLMService, pack: BunshinInvocationPack) -> int:
    metadata = pack.metadata if isinstance(pack.metadata, dict) else {}
    return llm_runtime.resolve_output_budget(
        preferred_endpoint_id=_preferred_endpoint_id_from_pack(pack),
        preferred_endpoint_source=_preferred_endpoint_source_from_pack(pack),
        requested=_optional_positive_int(metadata.get("max_output_tokens")))


def _bunshin_llm_request_metadata(pack: BunshinInvocationPack, run_id: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "endpoint_fallback_policy": "none",
        "response_mode_hint": "operational",
        "bunshin_run_id": str(run_id or ""),
        "max_output_tokens_source": "bunshin",
        # Bunshin owns bounded tool-call recovery.  Generic endpoint
        # continuation would replay the same oversized reasoning before the
        # role harness can narrow the next step.
        "max_output_recovery_enabled": False,
    }
    preferred_endpoint_id = _preferred_endpoint_id_from_pack(pack)
    if preferred_endpoint_id:
        metadata["preferred_endpoint_id"] = preferred_endpoint_id
        preferred_endpoint_source = _preferred_endpoint_source_from_pack(pack)
        if preferred_endpoint_source:
            metadata["preferred_endpoint_source"] = preferred_endpoint_source
    timeout_seconds = _bunshin_llm_request_timeout_seconds(pack)
    if timeout_seconds is not None:
        metadata["timeout_seconds"] = timeout_seconds
    prompt_observation_tag = _prompt_observation_tag_from_pack(pack)
    if prompt_observation_tag:
        metadata["prompt_observation_tag"] = prompt_observation_tag
    return metadata


def _bunshin_turn_settings_snapshot(pack: BunshinInvocationPack, llm_runtime: LLMService) -> dict[str, Any]:
    snapshot = {**llm_runtime.snapshot_turn_settings().to_payload(),
        "prompt_log_enabled": bool((pack.metadata or {}).get("prompt_log_enabled"))}
    temperature = _bunshin_temperature(pack)
    if temperature is not None:
        snapshot["temperature"] = temperature
    prompt_observation_tag = _prompt_observation_tag_from_pack(pack)
    if prompt_observation_tag:
        snapshot["prompt_observation_tag"] = prompt_observation_tag
    return snapshot


def _prompt_observation_tag_from_pack(pack: BunshinInvocationPack) -> str:
    metadata = pack.metadata if isinstance(pack.metadata, dict) else {}
    tag = str(metadata.get("prompt_observation_tag") or "").strip()
    return tag


def _bunshin_llm_request_timeout_seconds(pack: BunshinInvocationPack) -> float | None:
    pack_metadata = pack.metadata if isinstance(pack.metadata, dict) else {}
    explicit = _optional_positive_float(pack_metadata.get("timeout_seconds"))
    if explicit is not None:
        return explicit
    return _optional_positive_float(pack_metadata.get("llm_round_timeout_seconds"))


def _bunshin_generation_result(
    text: str,
    *,
    finish_reason: LLMFinishReason = LLMFinishReason.STOP,
) -> LLMGenerationResult:
    return LLMGenerationResult(
        response=LLMResponseIR(
            message=LLMMessageIR(
                role=MessageRole.ASSISTANT,
                parts=(TextPartIR(str(text)),) if str(text) else (),
            ),
            finish_reason=finish_reason,
            provider_response_count=0,
        )
    )


def _bunshin_temperature(pack: BunshinInvocationPack, *, fallback: float | None = None) -> float | None:
    metadata = pack.metadata if isinstance(pack.metadata, dict) else {}
    raw = metadata.get("temperature")
    if raw is None:
        return fallback
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback
    if 0.0 <= value <= 2.0:
        return value
    return fallback


def _preferred_endpoint_id_from_pack(pack: BunshinInvocationPack) -> str | None:
    metadata = pack.metadata if isinstance(pack.metadata, dict) else {}
    value = str(metadata.get("preferred_endpoint_id") or "").strip()
    return value or None


def _preferred_endpoint_source_from_pack(pack: BunshinInvocationPack) -> str | None:
    metadata = pack.metadata if isinstance(pack.metadata, dict) else {}
    value = str(metadata.get("preferred_endpoint_source") or "").strip()
    return value or None
