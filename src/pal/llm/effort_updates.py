"""Durable effort controls attached to the user input that they precede.

The root effort is a generation setting; an update is a chronological event.
Neither is inferred from a mutable operator setting at encode time.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TYPE_CHECKING

from pal.llm.cache_capabilities import supports_effort_updates
from pal.llm.ir import LLMMessageIR, MessageRole, ThinkingLevel
if TYPE_CHECKING:
    from pal.llm.shapes.base import ShapeContext

EFFORT_PIN = "pal_effort_pin"
EFFORT_UPDATE = "pal_effort_update"


def binding(facts: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: facts.get(key) for key in ("endpoint_id", "model_id", "wire_shape")}
    if facts.get("route_origin"):
        result["route_origin"] = facts["route_origin"]
    return result


def binding_matches(marker: Any, facts: Mapping[str, Any]) -> bool:
    if not isinstance(marker, Mapping):
        return False
    if any(marker.get(key) != facts.get(key) for key in ("model_id", "wire_shape")):
        return False
    origin = facts.get("route_origin")
    if marker.get("route_origin") and marker["route_origin"] != origin:
        return False
    if marker.get("endpoint_id") == facts.get("endpoint_id"):
        return True
    aliases = (facts.get("capabilities") or {}).get("replay_endpoint_aliases", {})
    if not origin or aliases.get(marker.get("endpoint_id")) != origin:
        return False
    from pal.llm.replay_compatibility import REPLAY_BINDINGS_KEY
    capabilities = facts.get("capabilities") or {}
    bindings = facts.get("replay_compatible_bindings")
    if bindings is None:
        bindings = capabilities.get(REPLAY_BINDINGS_KEY)
    return bindings is None or any(tuple(item) == (marker.get("wire_shape"), marker.get("endpoint_id"), marker.get("model_id")) for item in bindings)


def effort_route_changed(source: Any, target: Any) -> bool:
    from pal.llm.compaction import native_origin
    def supports(spec):
        return supports_effort_updates(spec.wire_shape, spec.model_id, spec.base_url, spec.capabilities_blob)
    return (native_origin(source) != native_origin(target)
            or supports(source) != supports(target)
            or not set(source.thinking_levels_blob or ()) <= set(target.thinking_levels_blob or ()))


def incompatible_effort_update(message: LLMMessageIR, facts: Mapping[str, Any]) -> bool:
    marker = message.metadata.get(EFFORT_UPDATE)
    if not isinstance(marker, Mapping):
        return False
    return (not binding_matches(marker, facts) or not facts.get("supports_effort_updates")
            or marker.get("effort") not in facts.get("thinking_levels", ()))


def can_append(facts: Mapping[str, Any], base: str, desired: str) -> bool:
    # Off changes the thinking/sampling mode, not just the effort field.
    # Until those transitions are verified, they use the compact boundary.
    return (bool(facts.get("supports_effort_updates"))
            and base != ThinkingLevel.OFF and desired != ThinkingLevel.OFF
            and base in facts.get("thinking_levels", ())
            and desired in facts.get("thinking_levels", ()))


def message_effort_update(message: LLMMessageIR, context: ShapeContext) -> str | None:
    marker = message.metadata.get(EFFORT_UPDATE)
    if (message.role != MessageRole.USER or not isinstance(marker, Mapping)
            or not binding_matches(marker, {"endpoint_id": context.endpoint_id,
                                            "model_id": context.model_id,
                                            "wire_shape": context.wire_shape,
                                            "route_origin": context.route_origin,
                                            "capabilities": context.capabilities})
            or not supports_effort_updates(context.wire_shape, context.model_id,
                                           context.base_url, context.capabilities)):
        return None
    effort = marker.get("effort")
    if effort not in {level.value for level in ThinkingLevel if level != ThinkingLevel.OFF}:
        return None
    return str(effort)
