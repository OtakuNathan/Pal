from __future__ import annotations

import base64
import json
from typing import Any

from pal.llm.contracts import CanonicalLLMRequest
from pal.llm.models import LLMEndpointModel

from pal.llm.llm_adaptor.base import (
    LLMProviderAdapter,
    OpenAIChatCompletionDraft,
    render_instruction_fallback_text,
)


class AnthropicMessagesProvider(LLMProviderAdapter):
    provider_names = frozenset({"anthropic"})
    adapter_names = frozenset({"anthropic", "anthropic_messages"})
    model_provider_prefix = "anthropic"
    model_provider_aliases = frozenset({"anthropic"})

    @classmethod
    def matches_endpoint(cls, endpoint: LLMEndpointModel) -> bool:
        return str(endpoint.api_mode or "") == "anthropic_messages"

    def apply_request(self, request: CanonicalLLMRequest, draft: OpenAIChatCompletionDraft) -> None:
        draft.thinking = _think_level_to_anthropic_thinking(
            request.metadata.get("think_level"),
            request.max_output_tokens,
        )


def chat_messages_to_anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    rendered_messages: list[dict[str, Any]] = []
    seen_conversation = False
    for message in list(messages or []):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user").strip() or "user"
        content = message.get("content")
        if role == "system" and not seen_conversation:
            text = _content_text(content)
            if text:
                system_parts.append(text)
            continue
        if role in {"system", "developer"}:
            text = render_instruction_fallback_text(role, content)
            if text:
                _append_anthropic_message(rendered_messages, "user", [{"type": "text", "text": text}])
                seen_conversation = True
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "").strip()
            if call_id:
                _append_anthropic_message(
                    rendered_messages,
                    "user",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": call_id,
                            "content": _content_text(content),
                        }
                    ],
                )
                seen_conversation = True
            continue
        if role == "assistant":
            blocks = _anthropic_provider_thinking_blocks(message.get("provider_specific_fields"))
            blocks.extend(_anthropic_text_blocks(content))
            for tool_call in list(message.get("tool_calls") or []):
                block = _anthropic_tool_use_block(tool_call)
                if block is not None:
                    blocks.append(block)
            if blocks:
                rendered_messages.append({"role": "assistant", "content": blocks})
                seen_conversation = True
            continue
        blocks = _anthropic_user_blocks(content)
        if blocks:
            _append_anthropic_message(rendered_messages, "user", blocks)
            seen_conversation = True
    if not rendered_messages:
        rendered_messages.append({"role": "user", "content": [{"type": "text", "text": "Continue."}]})
    system = "\n\n".join(system_parts).strip()
    return system or None, rendered_messages


def _append_anthropic_message(rendered_messages: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]) -> None:
    if not blocks:
        return
    if role == "user" and rendered_messages and rendered_messages[-1].get("role") == "user":
        rendered_messages[-1].setdefault("content", []).extend(blocks)
        return
    rendered_messages.append({"role": role, "content": blocks})


def chat_tools_to_anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for tool in list(tools or []):
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if isinstance(function, dict):
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            rendered.append(
                {
                    "name": name,
                    "description": str(function.get("description") or name),
                    "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
                }
            )
            continue
        name = str(tool.get("name") or "").strip()
        if name:
            rendered.append(
                {
                    "name": name,
                    "description": str(tool.get("description") or name),
                    "input_schema": tool.get("parameters") or {"type": "object", "properties": {}},
                }
            )
    return rendered


def think_level_to_anthropic_thinking(value: Any, max_output_tokens: int | None) -> dict[str, Any] | None:
    return _think_level_to_anthropic_thinking(value, max_output_tokens)


def _think_level_to_anthropic_effort(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    mapping = {
        "off": None,
        "minimal": "low",
        "low": "low",
        "balanced": "medium",
        "medium": "medium",
        "deep": "high",
        "high": "high",
        "xhigh": "high",
    }
    return mapping.get(text, "medium" if text else None)


def _think_level_to_anthropic_thinking(value: Any, max_output_tokens: int | None) -> dict[str, Any] | None:
    effort = _think_level_to_anthropic_effort(value)
    if effort is None:
        return None
    budget_map = {"low": 1024, "medium": 8192, "high": 32768}
    budget = budget_map.get(effort, 8192)
    if max_output_tokens is not None and max_output_tokens <= 1024:
        return None
    if max_output_tokens is not None and budget >= max_output_tokens:
        budget = max(1024, max_output_tokens // 2)
    if max_output_tokens is not None and budget >= max_output_tokens:
        return None
    return {"type": "enabled", "budget_tokens": budget}


def _anthropic_text_blocks(content: Any) -> list[dict[str, Any]]:
    text = _content_text(content)
    return [{"type": "text", "text": text}] if text else []


def _anthropic_provider_thinking_blocks(provider_specific_fields: Any) -> list[dict[str, Any]]:
    if not isinstance(provider_specific_fields, dict):
        return []
    raw_blocks = provider_specific_fields.get("anthropic_thinking_blocks")
    if not isinstance(raw_blocks, list):
        return []
    blocks: list[dict[str, Any]] = []
    for block in raw_blocks:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "").strip()
        if block_type in {"thinking", "redacted_thinking"}:
            blocks.append(dict(block))
    return blocks


def _anthropic_user_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        text = _content_text(content)
        return [{"type": "text", "text": text}] if text else []
    blocks: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            if item:
                blocks.append({"type": "text", "text": item})
            continue
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").strip()
        if kind in {"text", "input_text"}:
            text = str(item.get("text") or "")
            if text:
                blocks.append({"type": "text", "text": text})
            continue
        if kind in {"image_url", "input_image"}:
            image = item.get("image_url")
            if isinstance(image, dict):
                image = image.get("url")
            image_url = str(image or "").strip()
            image_block = _anthropic_image_block(image_url)
            if image_block is not None:
                blocks.append(image_block)
            elif image_url:
                blocks.append({"type": "text", "text": f"[image: {image_url}]"})
    return blocks


def _anthropic_tool_use_block(tool_call: Any) -> dict[str, Any] | None:
    if not isinstance(tool_call, dict):
        return None
    function = tool_call.get("function")
    if not isinstance(function, dict):
        return None
    name = str(function.get("name") or "").strip()
    if not name:
        return None
    call_id = str(tool_call.get("id") or tool_call.get("call_id") or "").strip()
    if not call_id:
        return None
    return {
        "type": "tool_use",
        "id": call_id,
        "name": name,
        "input": _coerce_json_object(function.get("arguments")),
    }


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                kind = str(item.get("type") or "").strip()
                if kind in {"text", "input_text", "output_text"}:
                    parts.append(str(item.get("text") or ""))
        return "\n".join(part for part in parts if part)
    return str(content)


def _coerce_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _anthropic_image_block(image_url: str) -> dict[str, Any] | None:
    if not image_url.startswith("data:") or "," not in image_url:
        return None
    header, data = image_url.split(",", 1)
    if ";base64" not in header:
        return None
    media_type = header.removeprefix("data:").split(";", 1)[0] or "image/png"
    try:
        base64.b64decode(data, validate=True)
    except Exception:
        return None
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": data,
        },
    }
