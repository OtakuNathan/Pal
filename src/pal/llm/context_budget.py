"""Input pricing and usage boundaries for a single endpoint generation."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from pal.llm.endpoint_spec import LLMEndpointSpec

from pal.llm.ir import LLMMessageIR, LLMRequestIR, WireShape
from pal.shared.tool_protocol import ToolDefinitionIR

OUTPUT_LIMIT = 65_535
OPENAI_STANDARD_MODELS = frozenset({
    "gpt-6-astra", "gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol", "gpt-6.1-terra",
    "gpt-6.2-astra", "gpt-6.2-sol",
})
OPENROUTER_STANDARD_MODELS = frozenset({
    "openai/gpt-6-astra", "openai/gpt-6.1-sol", "openai/gpt-6-luna", "openai/gpt-6-sol", "openai/gpt-6.1-terra",
    "openai/gpt-6.2-astra", "openai/gpt-6.2-sol",
})


def counting_shape(endpoint: LLMEndpointSpec) -> WireShape | None:
    if (endpoint.auth_kind != "api_key_ref"
            or (endpoint.capabilities_blob or {}).get("access_profile")):
        return None
    url = (endpoint.base_url or "").rstrip("/")
    model = endpoint.model_id
    shape = endpoint.wire_shape
    if endpoint.transport == "websocket":
        # Only the official TLS Responses socket shares the public HTTPS counter.
        if url not in {"https://api.openai.com/v1", "https://api.openai.com/v1/responses",
                       "wss://api.openai.com/v1", "wss://api.openai.com/v1/responses"}:
            return None
        url = "https://api.openai.com/v1"
    if url == "https://api.openai.com/v1" and shape == WireShape.OPENAI_RESPONSE and re.fullmatch(r"(?:gpt-[a-zA-Z0-9.-]+|o[1-9](?:-[a-zA-Z0-9.-]+)?)", model):
        return WireShape.OPENAI_RESPONSE
    if url in {"https://api.anthropic.com", "https://api.anthropic.com/v1"} and shape == WireShape.ANTHROPIC_MESSAGES and model.startswith("claude-"):
        return WireShape.ANTHROPIC_MESSAGES
    if url == "https://generativelanguage.googleapis.com/v1beta" and shape == WireShape.GEMINI_INTERACTIONS and re.fullmatch(r"gemini-[a-zA-Z0-9.-]+", model):
        return WireShape.GEMINI_INTERACTIONS
    return None


def _openrouter_standard_model(endpoint) -> bool:
    return ((endpoint.base_url or "").rstrip("/") == "https://openrouter.ai/api/v1"
            and endpoint.model_id in OPENROUTER_STANDARD_MODELS)


def _openai_standard_model(endpoint) -> bool:
    return counting_shape(endpoint) == WireShape.OPENAI_RESPONSE and endpoint.model_id in OPENAI_STANDARD_MODELS


def provider_input_maximum(endpoint) -> int | None:
    if (_openrouter_standard_model(endpoint) or
            (counting_shape(endpoint) == WireShape.OPENAI_RESPONSE
             and endpoint.model_id in {"gpt-6.2-sol", "gpt-6.2-astra"})):
        return 922_000
    return None


def standard_input_limit(endpoint: LLMEndpointSpec) -> int | None:
    if (endpoint.capabilities_blob or {}).get("allow_long_context_pricing"):
        return None
    if _openrouter_standard_model(endpoint) or _openai_standard_model(endpoint):
        return 272_000
    shape = counting_shape(endpoint)
    if shape == WireShape.ANTHROPIC_MESSAGES and re.fullmatch(r"claude-haiku-5-5(?:-\d{8})?", endpoint.model_id):
        return 100_000
    if shape == WireShape.GEMINI_INTERACTIONS and endpoint.model_id == "gemini-3.1-pro-preview":
        return 200_000
    return None


def message_fingerprint(message: LLMMessageIR) -> bytes:
    replay = message.replay
    content = (replay.wire_shape, replay.endpoint_id, replay.model_id, replay.payload) if replay else message.parts
    from pal.llm.effort_updates import EFFORT_UPDATE
    return hashlib.sha256(repr((message.role, message.prompt_region == "active_dynamic", message.instruction_scope, content, message.metadata.get(EFFORT_UPDATE))).encode()).digest()


@dataclass(frozen=True)
class UsageBoundary:
    prefix: tuple[bytes, ...]
    tools: tuple[ToolDefinitionIR, ...]
    reasoning_context: str | None
    tokens: int
    request_instructions: str = ""

    def tail(self, request: LLMRequestIR) -> tuple[LLMMessageIR, ...] | None:
        size = len(self.prefix)
        if (len(request.messages) < size or self.tools != request.tools
                or self.reasoning_context != request.policy.reasoning_context):
            return None
        if not all(message_fingerprint(msg) == expected for msg, expected in zip(request.messages, self.prefix)):
            return None
        return request.messages[size:]


def count_payload(shape: WireShape | None, wire: Mapping[str, Any]) -> dict[str, Any] | None:
    """Translate the complete encoded input, declining unsupported Gemini steps."""
    from pal.shared.json_values import thaw_json
    from pal.llm.compaction import token_count_payload

    payload = thaw_json(wire)
    if shape == WireShape.OPENAI_RESPONSE:
        return token_count_payload(payload)
    if shape == WireShape.ANTHROPIC_MESSAGES:
        return {key: value for key, value in payload.items()
                if key in {"model", "messages", "system", "tools", "tool_choice", "thinking", "output_config",
                           "output_format", "cache_control", "extra_headers"}}
    if shape != WireShape.GEMINI_INTERACTIONS:
        return None
    contents = []
    for step in payload["input"]:
        kind = step["type"]
        fields = {
            "user_input": {"content"}, "model_output": {"content"},
            "function_call": {"id", "name", "arguments"},
            "function_result": {"call_id", "name", "result", "is_error"},
        }
        if kind not in fields or step.keys() - fields[kind] - {"type", "signature"}:
            return None
        if kind in {"model_output", "user_input"}:
            role = "model" if kind == "model_output" else "user"
            parts = []
            for part in step["content"]:
                if part.keys() == {"type", "text"} and part["type"] == "text":
                    parts.append({"text": part["text"]})
                elif part["type"] == "image" and part.keys() == {"type", "mime_type", "data"}:
                    parts.append({"inlineData": {"mimeType": part["mime_type"], "data": part["data"]}})
                elif part["type"] == "image" and part.keys() == {"type", "mime_type", "uri"}:
                    parts.append({"fileData": {"mimeType": part["mime_type"], "fileUri": part["uri"]}})
                else:
                    return None
        elif kind == "function_call":
            role = "model"
            parts = [{"functionCall": {"id": step["id"], "name": step["name"], "args": step["arguments"]}}]
        elif kind == "function_result":
            role = "user"
            values = step["result"]
            if len(values) != 1 or values[0].keys() != {"type", "text"} or values[0].get("type") != "text":
                return None
            key = "error" if step.get("is_error") else "output"
            parts = [{"functionResponse": {"id": step["call_id"], "name": step["name"],
                                           "response": {key: values[0]["text"]}}}]
        else:
            # Interactions-only thought steps cannot be omitted from the count.
            return None
        if "signature" in step:
            parts[0]["thoughtSignature"] = step["signature"]
        contents.append({"role": role, "parts": parts})
    generate = {"model": "models/" + payload["model"], "contents": contents}
    if payload.get("system_instruction"):
        generate["systemInstruction"] = {"parts": [{"text": payload["system_instruction"]}]}
    if payload.get("tools"):
        if any(tool.get("type") != "function" for tool in payload["tools"]):
            return None
        generate["tools"] = [{"functionDeclarations": [
            {"name": tool["name"], "description": tool["description"], "parametersJsonSchema": tool["parameters"]}
            for tool in payload["tools"]]}]
    tool_choice = payload.get("generation_config", {}).get("tool_choice")
    if tool_choice is not None:
        if tool_choice not in {"any", "auto", "none"}:
            return None
        generate["toolConfig"] = {"functionCallingConfig": {"mode": tool_choice.upper()}}
    return {"generateContentRequest": generate}


def physical_window(endpoint: LLMEndpointSpec) -> int:
    """Verified provider capacity, independent of a configured compact watermark."""
    configured = (endpoint.capabilities_blob or {}).get("physical_context_window")
    if _openrouter_standard_model(endpoint) or _openai_standard_model(endpoint):
        return min(configured or 1_050_000, 1_050_000)
    shape = counting_shape(endpoint)
    if shape == WireShape.ANTHROPIC_MESSAGES and re.fullmatch(
            r"claude-(?:haiku-5-5|sonnet-5-5|opus-5-5|fable-5-2)(?:-\d{8})?", endpoint.model_id):
        return min(configured or 1_000_000, 1_000_000)
    if shape == WireShape.GEMINI_INTERACTIONS and endpoint.model_id in {
            "gemini-3.1-pro-preview", "gemini-4-neon", "gemini-4-hydrogen", "gemini-4-carbon"}:
        return min(configured or 1_048_576, 1_048_576)
    return configured or endpoint.context_window or 0
