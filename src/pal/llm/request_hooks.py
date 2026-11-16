"""Endpoint-specific request controls, applied after either encode path.

Wire codecs own the shape grammar. Opt-in provider capabilities own server-side
history policy; sharing an API shape does not imply support for these controls.
"""
from dataclasses import replace
from collections.abc import Mapping

from pal.llm.ir import ThinkingLevel, WireShape
from pal.llm.shapes.base import EncodedRequest, ShapeContext, ShapeDecodeError, request_parameter_supported
from pal.shared.json_values import thaw_json


def apply_provider_request_hooks(encoded: EncodedRequest, context: ShapeContext) -> EncodedRequest:
    # Projected requests take the same final guard as ordinary codec encodes.
    payload = thaw_json(encoded.payload)
    extra_body = thaw_json(encoded.extra_body)
    # SDK and WebSocket extra_body merging replaces top-level fields. A new
    # reasoning object (or null) must not inherit an old effort="none".
    effective = {**payload, **extra_body}
    effort = None
    if context.wire_shape == WireShape.OPENAI_RESPONSE:
        reasoning = effective.get("reasoning")
        effort = reasoning.get("effort") if isinstance(reasoning, Mapping) else None
    elif context.wire_shape == WireShape.OPENAI_COMPLETION:
        effort = effective.get("reasoning_effort")
    level = ThinkingLevel.OFF if effort == "none" else None
    sampling_parameters = ["temperature", "top_p", "top_logprobs"]
    if context.wire_shape == WireShape.OPENAI_COMPLETION:
        sampling_parameters.append("logprobs")
    for parameter in sampling_parameters:
        if not request_parameter_supported(context, parameter, thinking_level=level):
            payload.pop(parameter, None)
            extra_body.pop(parameter, None)
    if context.wire_shape == WireShape.OPENAI_RESPONSE and not request_parameter_supported(
        context, "top_logprobs", thinking_level=level,
    ):
        for body in (payload, extra_body):
            include = body.get("include")
            if isinstance(include, (list, tuple)):
                body["include"] = [item for item in include if item != "message.output_text.logprobs"]
    encoded = replace(encoded, payload=payload, extra_body=extra_body)
    from pal.llm.chatgpt import UNSUPPORTED_PARAMETERS, is_chatgpt
    if is_chatgpt(context.capabilities):
        payload = thaw_json(encoded.payload)
        extra_body = thaw_json(encoded.extra_body)
        for parameter in UNSUPPORTED_PARAMETERS:
            payload.pop(parameter, None)
            extra_body.pop(parameter, None)
        payload["store"] = False
        extra_body.pop("store", None)
        extra_body.pop("stream", None)
        encoded = replace(encoded, payload=payload, extra_body=extra_body)
    protocol = context.capabilities.get("preserved_thinking")
    if protocol is None:
        return encoded
    bindings = {
        "glm": (WireShape.OPENAI_COMPLETION, "thinking.clear_thinking"),
        "anthropic": (WireShape.ANTHROPIC_MESSAGES, "context_management"),
    }
    binding = bindings.get(str(protocol))
    if binding is None or context.wire_shape != binding[0]:
        raise ShapeDecodeError("preserved_thinking protocol does not match the endpoint shape")
    if not request_parameter_supported(context, binding[1]):
        raise ShapeDecodeError(f"{binding[1]} is unsupported by this endpoint")
    payload = thaw_json(encoded.payload)
    extra_body = thaw_json(encoded.extra_body)
    if protocol == "glm":
        thinking = dict(payload.pop("thinking", None) or {})
        thinking.update(extra_body.get("thinking") or {})
        thinking["clear_thinking"] = False
        extra_body["thinking"] = thinking
    else:
        headers = dict(payload.get("extra_headers") or {})
        beta_key = next((key for key in headers if key.lower() == "anthropic-beta"), "anthropic-beta")
        betas = [value.strip() for value in str(headers.get(beta_key) or "").split(",") if value.strip()]
        if "context-management-2025-06-27" not in betas:
            betas.append("context-management-2025-06-27")
        headers[beta_key] = ",".join(betas)
        payload["extra_headers"] = headers
        management = dict(payload.pop("context_management", None) or {})
        management.update(extra_body.get("context_management") or {})
        edits = [edit for edit in management.get("edits", ())
                 if edit.get("type") != "clear_thinking_20251015"]
        # Anthropic requires thinking edits before tool clearing edits.
        management["edits"] = [{"type": "clear_thinking_20251015", "keep": "all"}, *edits]
        extra_body["context_management"] = management
    return replace(encoded, payload=payload, extra_body=extra_body)
