from __future__ import annotations
import contextlib
from typing import Any
from pal.llm.contracts import LLMGenerationResult
from pal.llm.ir import LLMMessageIR, LLMResponseIR, MessageRole, TextPartIR
from pal.shared import LLMFinishReason, BunshinInvocationPack
from pal.bunshin.runner_components.numeric_values import _optional_positive_int


def _optional_positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _resolve_bunshin_max_output_tokens(llm_runtime: Any, pack: BunshinInvocationPack) -> int:
    metadata = pack.metadata if isinstance(pack.metadata, dict) else {}
    explicit = _optional_positive_int(metadata.get("max_output_tokens"))
    preferred_endpoint_id = _preferred_endpoint_id_from_pack(pack)
    preferred_endpoint_source = _preferred_endpoint_source_from_pack(pack)
    endpoint_limit = _runtime_max_output_tokens(
        llm_runtime,
        preferred_endpoint_id=preferred_endpoint_id,
        preferred_endpoint_source=preferred_endpoint_source,
    )
    if endpoint_limit is None:
        facts = _runtime_endpoint_facts(
            llm_runtime,
            preferred_endpoint_id=preferred_endpoint_id,
            preferred_endpoint_source=preferred_endpoint_source,
        )
        endpoint_limit = _optional_positive_int(facts.get("max_output_tokens")) if facts else None
        context_window = _optional_positive_int(facts.get("context_window")) if facts else None
        if endpoint_limit is None and context_window is not None:
            endpoint_limit = _max_output_tokens_from_context_window(context_window, llm_runtime)
    if explicit is not None:
        return min(explicit, endpoint_limit) if endpoint_limit is not None else explicit
    if endpoint_limit is not None:
        return endpoint_limit
    config = getattr(llm_runtime, "config", None)
    return _optional_positive_int(getattr(config, "fallback_max_output_tokens", None)) or 4096


def _bunshin_llm_request_metadata(pack: BunshinInvocationPack, run_id: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "endpoint_fallback_policy": "none",
        "response_mode_hint": "operational",
        "bunshin_run_id": str(run_id or ""),
        "max_output_tokens_source": "bunshin",
        # Bunshin owns bounded, action-forcing recovery.  Generic endpoint
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


def _bunshin_turn_settings_snapshot(pack: BunshinInvocationPack, llm_runtime: Any) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "prompt_log_enabled": bool((pack.metadata or {}).get("prompt_log_enabled")),
    }
    refresh = getattr(llm_runtime, "refresh_runtime_settings", None)
    if callable(refresh):
        with contextlib.suppress(Exception):
            refresh()
    thinking_levels_snapshot = getattr(llm_runtime, "thinking_levels_snapshot", None)
    if callable(thinking_levels_snapshot):
        with contextlib.suppress(Exception):
            snapshot["think_levels"] = dict(thinking_levels_snapshot())
    cache_policy_snapshot = getattr(llm_runtime, "cache_policy_snapshot", None)
    if callable(cache_policy_snapshot):
        snapshot["cache_policy_snapshot"] = cache_policy_snapshot()
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


def _runtime_max_output_tokens(
    llm_runtime: Any,
    *,
    preferred_endpoint_id: str | None = None,
    preferred_endpoint_source: str | None = None,
) -> int | None:
    resolver = getattr(llm_runtime, "resolve_max_output_tokens", None)
    if not callable(resolver):
        return None
    with contextlib.suppress(Exception):
        try:
            return _optional_positive_int(
                resolver(preferred_endpoint_id=preferred_endpoint_id, preferred_endpoint_source=preferred_endpoint_source)
            )
        except TypeError:
            try:
                return _optional_positive_int(resolver(preferred_endpoint_id=preferred_endpoint_id))
            except TypeError:
                return _optional_positive_int(resolver())
    return None


def _runtime_endpoint_facts(
    llm_runtime: Any,
    *,
    preferred_endpoint_id: str | None = None,
    preferred_endpoint_source: str | None = None,
) -> dict[str, Any]:
    resolver = getattr(llm_runtime, "resolve_endpoint_facts", None)
    if not callable(resolver):
        return {}
    with contextlib.suppress(Exception):
        try:
            facts = resolver(preferred_endpoint_id=preferred_endpoint_id, preferred_endpoint_source=preferred_endpoint_source)
        except TypeError:
            try:
                facts = resolver(preferred_endpoint_id=preferred_endpoint_id)
            except TypeError:
                facts = resolver()
        return dict(facts) if isinstance(facts, dict) else {}
    return {}


def _max_output_tokens_from_context_window(context_window: int, llm_runtime: Any) -> int:
    config = getattr(llm_runtime, "config", None)
    cap = _optional_positive_int(getattr(config, "default_max_output_tokens", None)) or 25_000
    floor = _optional_positive_int(getattr(config, "fallback_max_output_tokens", None)) or 4096
    margin_factor = float(getattr(config, "context_margin_factor", 0.05) or 0.05)
    margin_cap = _optional_positive_int(getattr(config, "context_margin_cap", None)) or 16_384
    margin_min = _optional_positive_int(getattr(config, "context_margin_min", None)) or 1024
    margin = min(margin_cap, max(margin_min, int(context_window * margin_factor)))
    usable = max(512, context_window - margin)
    context_fraction = max(512, context_window // 4)
    return max(512, min(cap, max(floor, context_fraction), usable))


_DEFAULT_MANAGER_TURN_TIMEOUT_SECONDS = 3600.0


_MAX_MANAGER_TURN_TIMEOUT_SECONDS = 3600.0
