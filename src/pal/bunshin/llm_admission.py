"""Validate a role's encoded request against its Manager-bound endpoint and output budget."""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from pal.bunshin.llm_output_budget import with_role_output_budget
from pal.bunshin.llm_proxy import LLMProxyError
from pal.llm.endpoint_spec import LLMEndpointSpec, endpoint_spec_fingerprint
from pal.llm.ir import WireShape
from pal.llm.models import LLMEndpointModel
from pal.llm.repository import LLMEndpointRepository, RuntimeSettingRepository
from pal.llm.transport_contracts import EncodedTransportRequest


_LLM_TRANSPORT_MAX_TIMEOUT_SECONDS = 3900.0


def _bounded_transport_timeout(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise LLMProxyError("LLM transport timeout must be numeric") from exc
    if not math.isfinite(parsed) or parsed <= 0 or parsed > _LLM_TRANSPORT_MAX_TIMEOUT_SECONDS:
        raise LLMProxyError(
            "LLM transport timeout must be within "
            f"(0, {_LLM_TRANSPORT_MAX_TIMEOUT_SECONDS:g}]"
        )
    return parsed


def _validate_transport_payload_authority(
    endpoint: LLMEndpointSpec,
    payload: Mapping[str, Any],
    extra_body: Mapping[str, Any],
) -> None:
    if str(payload.get("model") or "").strip() != str(endpoint.model_id):
        raise LLMProxyError(
            "encoded LLM payload changed endpoint model identity"
        )
    continuation_ids = {"previous_response_id", "previous_interaction_id"}
    if continuation_ids.intersection(payload) or continuation_ids.intersection(extra_body):
        raise LLMProxyError("encoded LLM payload supplied an unbound continuation ID")
    if "extra_body" in payload:
        raise LLMProxyError(
            "encoded LLM payload must use the separate extra_body field"
        )
    protected = {
        "model",
        "max_output_tokens",
        "max_completion_tokens",
        "max_tokens",
        "generation_config",
        "stream",
    }
    overridden = sorted(protected.intersection(extra_body))
    if overridden:
        raise LLMProxyError(
            f"encoded LLM extra_body overrides transport authority: {overridden}"
        )
    maximum = endpoint.max_output_tokens
    if maximum is None:
        return
    if endpoint.wire_shape == "gemini_interactions":
        payload = payload.get("generation_config", {})
        if not isinstance(payload, Mapping):
            raise LLMProxyError("Gemini generation_config must be an object")
    for key in ("max_output_tokens", "max_completion_tokens", "max_tokens"):
        if key not in payload:
            continue
        try:
            requested = int(payload[key])
        except (TypeError, ValueError) as exc:
            raise LLMProxyError(
                f"encoded LLM {key} must be an integer"
            ) from exc
        if requested <= 0 or requested > maximum:
            raise LLMProxyError(
                f"encoded LLM {key} exceeds endpoint output authority"
            )


def authorize_role_request(
    pack_metadata: Mapping[str, Any],
    params: Mapping[str, Any],
) -> tuple[LLMEndpointModel, EncodedTransportRequest]:
    endpoint_id = str(params.get("endpoint_id") or "").strip()
    if not endpoint_id:
        raise LLMProxyError("LLM transport endpoint_id is required")
    preferred = str(pack_metadata.get("preferred_endpoint_id") or "").strip()
    if preferred and endpoint_id != preferred:
        raise PermissionError(
            f"role assignment does not allow LLM endpoint {endpoint_id}"
        )
    if not preferred:
        active = str(
            RuntimeSettingRepository().get_active_llm_endpoint_id() or ""
        ).strip()
        if active and endpoint_id != active:
            raise LLMProxyError(
                "active LLM endpoint changed before provider start",
                kind="endpoint_spec_stale",
            )
    configured_endpoint = LLMEndpointRepository().get(endpoint_id)
    if configured_endpoint is None or not configured_endpoint.enabled:
        raise LLMProxyError(
            f"LLM endpoint is unavailable: {endpoint_id}",
            kind="endpoint_spec_stale",
        )
    endpoint = LLMEndpointSpec.from_value(with_role_output_budget(configured_endpoint, pack_metadata.get("max_output_tokens")))
    requested_shape = str(params.get("wire_shape") or "").strip()
    if requested_shape != str(endpoint.wire_shape):
        raise LLMProxyError(
            "LLM endpoint wire shape changed before provider start",
            kind="endpoint_spec_stale",
        )
    requested_fingerprint = str(
        params.get("endpoint_spec_fingerprint") or ""
    ).strip()
    if requested_fingerprint != endpoint_spec_fingerprint(endpoint):
        raise LLMProxyError(
            "LLM endpoint specification changed before provider start",
            kind="endpoint_spec_stale",
        )
    request_id = str(params.get("request_id") or "").strip()
    if not request_id:
        raise LLMProxyError("LLM transport request_id is required")
    timeout_seconds = _bounded_transport_timeout(params.get("timeout_seconds"))
    payload = params.get("payload")
    extra_body = params.get("extra_body")
    if not isinstance(payload, Mapping) or not isinstance(extra_body, Mapping):
        raise LLMProxyError("encoded LLM transport payload must be an object")
    _validate_transport_payload_authority(endpoint, payload, extra_body)
    if endpoint.wire_shape == WireShape.GEMINI_INTERACTIONS.value:
        # The broker receives full assignment-local history and owns no accepted parent ID.
        payload = {**payload, "store": False}
        extra_body = {k: v for k, v in extra_body.items() if k != "store"}
    return configured_endpoint, EncodedTransportRequest(
        request_id=request_id, wire_shape=WireShape(str(endpoint.wire_shape)), timeout_seconds=timeout_seconds,
        payload=dict(payload), extra_body=dict(extra_body), stream=bool(params.get("stream", True)),
    )
