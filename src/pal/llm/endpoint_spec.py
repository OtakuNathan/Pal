from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from typing import Any, Mapping
from urllib.parse import urlsplit

from pal.llm.context_budget import physical_window
from pal.llm.ir import ThinkingLevel, WireShape
from pal.shared.json_values import thaw_json


class LLMEndpointSpecError(ValueError):
    pass


@dataclass(frozen=True)
class LLMEndpointSpec:
    """The one validated endpoint contract persisted by the repository."""

    endpoint_id: str
    provider: str
    model_id: str
    display_name: str | None
    wire_shape: str
    base_url: str
    auth_kind: str
    credential_ref: str
    context_window: int | None
    max_output_tokens: int | None
    thinking_levels_blob: tuple[str, ...]
    default_thinking_level: str
    supports_tools: bool
    supports_streaming: bool
    supports_vision: bool
    input_modalities_blob: tuple[str, ...]
    output_modalities_blob: tuple[str, ...]
    priority: int
    enabled: bool
    capabilities_blob: Mapping[str, Any]
    notes: str | None
    transport: str = "http"

    @property
    def supports_response_continuation(self) -> bool:
        return self.capabilities_blob.get("continuation_mode") != "full" and (
            self.wire_shape == WireShape.GEMINI_INTERACTIONS.value or self.transport == "websocket"
        )

    @classmethod
    def from_value(cls, value: Mapping[str, Any] | object) -> "LLMEndpointSpec":
        source = _value_mapping(value)
        endpoint_id = _required_text(source, "endpoint_id")
        provider = _required_text(source, "provider")
        model_id = _required_text(source, "model_id")
        base_url = _required_text(source, "base_url")
        try:
            wire_shape = WireShape(_required_text(source, "wire_shape")).value
        except ValueError as exc:
            raise LLMEndpointSpecError(
                f"endpoint {endpoint_id} has invalid wire_shape"
            ) from exc
        transport = str(source.get("transport") or "http").strip()
        if transport not in {"http", "websocket"}:
            raise LLMEndpointSpecError(f"endpoint {endpoint_id} has invalid transport: {transport}")
        if transport == "websocket" and wire_shape != WireShape.OPENAI_RESPONSE.value:
            raise LLMEndpointSpecError("websocket transport requires openai_response wire_shape")
        if transport == "websocket":
            address = urlsplit(base_url)
            if (address.scheme not in {"http", "https", "ws", "wss"} or not address.netloc
                    or address.username or address.password or address.query or address.fragment):
                raise LLMEndpointSpecError("websocket transport requires a base URL without credentials, query or fragment")
        auth_kind = str(source.get("auth_kind") or "api_key_ref").strip()
        if auth_kind not in {"api_key_ref", "oauth", "local_provider_auth"}:
            raise LLMEndpointSpecError(
                f"endpoint {endpoint_id} has invalid auth_kind: {auth_kind}"
            )
        credential_ref = str(source.get("credential_ref") or "").strip()
        if auth_kind != "local_provider_auth" and not credential_ref:
            raise LLMEndpointSpecError(
                f"endpoint {endpoint_id} requires credential_ref for {auth_kind}"
            )

        levels = validate_thinking_levels(
            source.get("thinking_levels_blob"), wire_shape=wire_shape, endpoint_id=endpoint_id,
        )
        default_level = str(source.get("default_thinking_level") or "").strip().lower()
        if default_level not in levels:
            raise LLMEndpointSpecError(
                f"endpoint {endpoint_id} default_thinking_level={default_level!r} "
                f"is not declared by thinking_levels_blob; available: {', '.join(levels)}"
            )
        context_window = _optional_positive_int(
            source.get("context_window"),
            field_name="context_window",
            endpoint_id=endpoint_id,
        )
        max_output_tokens = _optional_positive_int(
            source.get("max_output_tokens"),
            field_name="max_output_tokens",
            endpoint_id=endpoint_id,
        )
        capabilities = _json_mapping(source.get("capabilities_blob"))
        aliases = capabilities.get("replay_endpoint_aliases", {})
        if not isinstance(aliases, Mapping) or any(
            not isinstance(alias, str) or not alias.strip() or not isinstance(origin, str)
            or len(origin) != 64 or any(ch not in "0123456789abcdef" for ch in origin)
            for alias, origin in aliases.items()
        ):
            raise LLMEndpointSpecError("replay_endpoint_aliases must map former endpoint IDs to origin digests")
        declared_window = capabilities.get("physical_context_window")
        if declared_window is not None and (type(declared_window) is not int or declared_window < 1):
            raise LLMEndpointSpecError("physical_context_window must be a positive integer")
        if "allow_long_context_pricing" in capabilities and not isinstance(capabilities["allow_long_context_pricing"], bool):
            raise LLMEndpointSpecError("allow_long_context_pricing must be a boolean")
        if capabilities.get("compaction_strategy", "auto") not in {"auto", "semantic_checkpoint", "openai_responses"}:
            raise LLMEndpointSpecError("invalid capabilities_blob.compaction_strategy")
        if capabilities.get("continuation_mode", "auto") not in ("auto", "full"):
            raise LLMEndpointSpecError("invalid continuation_mode")
        sampling_policy = capabilities.get("sampling_policy")
        if sampling_policy is not None and (
            wire_shape not in {"openai_response", "openai_completion"}
            or sampling_policy not in ("supported", "unsupported", "requires_no_reasoning")
        ):
            raise LLMEndpointSpecError(
                "capabilities_blob.sampling_policy requires an OpenAI wire shape and "
                "supported, unsupported or requires_no_reasoning"
            )
        access_profile = capabilities.get("access_profile")
        if access_profile is not None:
            from pal.llm.chatgpt import API_URL, PROFILE
            if (access_profile != PROFILE or auth_kind != "oauth"
                    or wire_shape != "openai_response" or base_url.rstrip("/") != API_URL):
                raise LLMEndpointSpecError(
                    "openai_chatgpt requires oauth, openai_response and https://api.openai.com/v1"
                )
        preservation = capabilities.get("preserved_thinking")
        if preservation is not None:
            expected_shape = {
                "glm": "openai_completion",
                "anthropic": "anthropic_messages",
            }.get(str(preservation))
            if expected_shape is None or wire_shape != expected_shape:
                raise LLMEndpointSpecError(
                    "capabilities_blob.preserved_thinking requires glm with "
                    "openai_completion or anthropic with anthropic_messages"
                )
        reasoning_context = capabilities.get("reasoning_context")
        if reasoning_context is not None:
            if (wire_shape != "openai_response"
                    or reasoning_context not in ("auto", "current_turn", "all_turns")):
                raise LLMEndpointSpecError(
                    "capabilities_blob.reasoning_context requires openai_response "
                    "and auto, current_turn or all_turns"
                )
        unsupported_parameters = capabilities.get(
            "unsupported_request_parameters"
        )
        if unsupported_parameters is not None:
            capabilities["unsupported_request_parameters"] = list(
                _string_tuple(
                    unsupported_parameters,
                    lowercase=True,
                    field_name="capabilities_blob.unsupported_request_parameters",
                )
            )
        spec = cls(
            endpoint_id=endpoint_id,
            provider=provider,
            model_id=model_id,
            display_name=_optional_text(source.get("display_name")),
            wire_shape=wire_shape,
            base_url=base_url,
            auth_kind=auth_kind,
            credential_ref=credential_ref,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            thinking_levels_blob=levels,
            default_thinking_level=default_level,
            supports_tools=bool(source.get("supports_tools", True)),
            supports_streaming=bool(source.get("supports_streaming", True)),
            supports_vision=bool(source.get("supports_vision", False)),
            input_modalities_blob=_string_tuple(
                source.get("input_modalities_blob"),
                field_name="input_modalities_blob",
            ),
            output_modalities_blob=_string_tuple(
                source.get("output_modalities_blob"),
                field_name="output_modalities_blob",
            ),
            priority=int(source.get("priority") or 0),
            enabled=bool(source.get("enabled", True)),
            capabilities_blob=capabilities,
            notes=_optional_text(source.get("notes")),
            transport=transport,
        )
        capacity = physical_window(spec)
        if capacity and max_output_tokens and max_output_tokens > capacity:
            raise LLMEndpointSpecError(f"endpoint {endpoint_id} max_output_tokens exceeds context_window capacity")
        return spec

    def to_payload(self) -> dict[str, Any]:
        return {
            "endpoint_id": self.endpoint_id,
            "provider": self.provider,
            "model_id": self.model_id,
            "display_name": self.display_name,
            "wire_shape": self.wire_shape,
            "transport": self.transport,
            "base_url": self.base_url,
            "auth_kind": self.auth_kind,
            "credential_ref": self.credential_ref,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "thinking_levels_blob": list(self.thinking_levels_blob),
            "default_thinking_level": self.default_thinking_level,
            "supports_tools": self.supports_tools,
            "supports_streaming": self.supports_streaming,
            "supports_vision": self.supports_vision,
            "input_modalities_blob": list(self.input_modalities_blob),
            "output_modalities_blob": list(self.output_modalities_blob),
            "priority": self.priority,
            "enabled": self.enabled,
            "capabilities_blob": thaw_json(self.capabilities_blob),
            "notes": self.notes,
        }


ENDPOINT_SPEC_FIELDS = frozenset(field.name for field in fields(LLMEndpointSpec))


def endpoint_spec_fingerprint(value: Mapping[str, Any] | object) -> str:
    """Fingerprint the public endpoint contract without resolving its secret."""

    payload = LLMEndpointSpec.from_value(value).to_payload()
    material = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def merge_endpoint_spec_payload(
    payload: Mapping[str, Any],
    *,
    existing: Mapping[str, Any] | object | None = None,
) -> LLMEndpointSpec:
    unknown = set(payload) - ENDPOINT_SPEC_FIELDS
    if unknown:
        raise LLMEndpointSpecError(
            f"unknown LLM endpoint fields: {sorted(unknown)}"
        )
    merged = _value_mapping(existing) if existing is not None else {}
    merged.update(dict(payload))
    return LLMEndpointSpec.from_value(merged)


def _value_mapping(value: Mapping[str, Any] | object | None) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    return {
        name: getattr(value, name)
        for name in ENDPOINT_SPEC_FIELDS
        if hasattr(value, name)
    }


def _required_text(source: Mapping[str, Any], name: str) -> str:
    value = str(source.get(name) or "").strip()
    if not value:
        endpoint_id = str(source.get("endpoint_id") or "<unknown>")
        raise LLMEndpointSpecError(f"endpoint {endpoint_id} requires {name}")
    return value


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _optional_positive_int(
    value: Any,
    *,
    field_name: str,
    endpoint_id: str,
) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise LLMEndpointSpecError(
            f"endpoint {endpoint_id} {field_name} must be an integer"
        ) from exc
    if parsed <= 0:
        raise LLMEndpointSpecError(
            f"endpoint {endpoint_id} {field_name} must be positive"
        )
    return parsed


def validate_thinking_levels(
    levels: Any, *, wire_shape: str, endpoint_id: str = "configuration",
) -> tuple[str, ...]:
    """Validate declarations and report this shape's accepted vocabulary."""
    allowed = tuple(
        level.value for level in ThinkingLevel
        if wire_shape != WireShape.ANTHROPIC_MESSAGES.value or level != ThinkingLevel.MINIMAL
    )
    hint = f"available for {wire_shape}: {', '.join(allowed)}"
    values = _string_tuple(levels, lowercase=True, field_name="thinking_levels_blob")
    if not values:
        raise LLMEndpointSpecError(f"endpoint {endpoint_id} has no thinking level enum; {hint}")
    for level in values:
        if level not in allowed:
            raise LLMEndpointSpecError(
                f"endpoint {endpoint_id} has invalid thinking level: {level!r}; {hint}"
            )
    return values


def _string_tuple(
    value: Any,
    *,
    lowercase: bool = False,
    field_name: str,
) -> tuple[str, ...]:
    if value is None:
        return ()
    parsed = _json_value(value, field_name=field_name)
    if not isinstance(parsed, (list, tuple)):
        raise LLMEndpointSpecError(f"{field_name} must be an array")
    items: list[str] = []
    for item in parsed:
        text = str(item or "").strip()
        if lowercase:
            text = text.lower()
        if text and text not in items:
            items.append(text)
    return tuple(items)


def _json_mapping(value: Any) -> dict[str, Any]:
    parsed = _json_value(value, field_name="capabilities_blob")
    if parsed is None:
        return {}
    if not isinstance(parsed, Mapping):
        raise LLMEndpointSpecError("capabilities_blob must be an object")
    return dict(parsed)


def _json_value(value: Any, *, field_name: str) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise LLMEndpointSpecError(
            f"{field_name} contains invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"
        ) from exc
