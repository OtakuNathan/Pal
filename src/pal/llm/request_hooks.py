"""Endpoint-specific request controls, applied after either encode path.

Wire codecs own the shape grammar. Opt-in provider capabilities own server-side
history policy; sharing an API shape does not imply support for these controls.
"""
from dataclasses import replace

from pal.llm.ir import WireShape
from pal.llm.shapes.base import EncodedRequest, ShapeContext, ShapeDecodeError, request_parameter_supported
from pal.shared.json_values import thaw_json


def apply_provider_request_hooks(encoded: EncodedRequest, context: ShapeContext) -> EncodedRequest:
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
