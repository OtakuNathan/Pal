"""Replay compatibility from loaded endpoint metadata, never provider names.

The allowlist is intentionally exact. OpenAI documents mutual reasoning reuse
for these three models, not for every model with a GPT prefix:
https://developers.openai.com/api/docs/guides/reasoning#preserve-reasoning-across-calls
No provenance is added to durable L1; existing replay bindings remain unchanged.
"""
from __future__ import annotations

from typing import Any, Mapping

from pal.llm.compaction import native_origin
from pal.llm.ir import ReplayEnvelope, WireShape


REPLAY_BINDINGS_KEY = "_pal_replay_compatible_bindings"
_OPENAI_56 = frozenset({"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"})


def endpoints_replay_compatible(source: Any, target: Any) -> bool:
    """Only opt proven, same-service and same-auth endpoints into reuse."""
    if source is None or target is None:
        return False
    same_identity = (
        source.model_id == target.model_id and source.wire_shape == target.wire_shape
        and str(source.base_url).rstrip("/") == str(target.base_url).rstrip("/")
        and source.auth_kind == target.auth_kind and source.credential_ref == target.credential_ref
        and (source.capabilities_blob or {}).get("access_profile") == (target.capabilities_blob or {}).get("access_profile")
    )
    return same_identity or (
        source.wire_shape == target.wire_shape == WireShape.OPENAI_RESPONSE.value
        and source.model_id in _OPENAI_56
        and target.model_id in _OPENAI_56
        and str(source.base_url).rstrip("/") == str(target.base_url).rstrip("/") == "https://api.openai.com/v1"
        and source.auth_kind == target.auth_kind == "api_key_ref"
        and bool(source.credential_ref)
        and source.credential_ref == target.credential_ref
        and not (source.capabilities_blob or {}).get("access_profile")
        and not (target.capabilities_blob or {}).get("access_profile")
    )


def compatible_replay_bindings(target: Any, endpoints: Any) -> list[list[str]]:
    endpoints = tuple(endpoints)
    bindings = [
        [str(source.wire_shape), str(source.endpoint_id), str(source.model_id)]
        for source in endpoints
        if endpoints_replay_compatible(source, target)
    ]
    # A renamed endpoint can read its own older envelopes. Bind the former
    # ID to the original model, service and credential reference, and give
    # any currently registered endpoint precedence over a historical name.
    aliases = (target.capabilities_blob or {}).get("replay_endpoint_aliases", {})
    if aliases:
        current_ids = {source.endpoint_id for source in endpoints}
        origin = native_origin(target)
        bindings.extend([str(target.wire_shape), alias, str(target.model_id)]
                        for alias, fingerprint in aliases.items()
                        if fingerprint == origin and alias not in current_ids)
    return bindings


def accepts_replay(
    replay: ReplayEnvelope, *, wire_shape: WireShape, endpoint_id: str,
    model_id: str, capabilities: Mapping[str, Any],
) -> bool:
    if replay.matches(wire_shape=wire_shape, endpoint_id=endpoint_id, model_id=model_id):
        return True
    identity = (replay.wire_shape.value, replay.endpoint_id, replay.model_id)
    return any(identity == tuple(binding) for binding in capabilities.get(REPLAY_BINDINGS_KEY, ()))


def historical_endpoint_id(endpoint_id: str, endpoints: Any) -> str:
    """Resolve a persisted historical reference, never a new model selection."""
    endpoints = tuple(endpoints)
    if not endpoint_id or any(ep.endpoint_id == endpoint_id for ep in endpoints):
        return endpoint_id
    matches = [ep.endpoint_id for ep in endpoints
               if (ep.capabilities_blob or {}).get("replay_endpoint_aliases", {}).get(endpoint_id) == native_origin(ep)]
    if len(matches) > 1:
        raise ValueError(f"ambiguous historical endpoint reference: {endpoint_id}")
    return matches[0] if matches else endpoint_id
