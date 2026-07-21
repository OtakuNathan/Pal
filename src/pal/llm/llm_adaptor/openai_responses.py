from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from pal.llm.contracts import CanonicalLLMRequest
from pal.llm.models import LLMEndpointModel

from pal.llm.llm_adaptor.base import (
    OPENAI_RESPONSES_SHAPE,
    LLMProviderAdapter,
    _capabilities,
    _think_level_to_completion_reasoning_effort,
)


@dataclass
class OpenAIResponsesDraft:
    model: str
    input: list[dict[str, Any]]
    instructions: str | None = None
    timeout: float | None = 120
    api_base: str | None = None
    api_key: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_choice: Any = None
    reasoning: dict[str, Any] | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    max_retries: int | None = 0

    def to_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": self.input,
        }
        optional_values = {
            "instructions": self.instructions,
            "timeout": self.timeout,
            "api_base": self.api_base,
            "api_key": self.api_key,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "tool_choice": self.tool_choice,
            "reasoning": self.reasoning,
            "max_retries": self.max_retries,
        }
        kwargs.update({key: value for key, value in optional_values.items() if value is not None})
        if self.tools:
            kwargs["tools"] = self.tools
        if self.extra_body:
            kwargs["extra_body"] = dict(self.extra_body)
        kwargs.update(self.extra)
        return kwargs


class OpenAIResponsesProvider(LLMProviderAdapter):
    provider_names = frozenset({"openai"})
    adapter_names = frozenset({"openai_responses", "responses"})
    model_provider_prefix = "openai"
    model_provider_aliases = frozenset({"openai"})
    request_shape = OPENAI_RESPONSES_SHAPE

    @classmethod
    def matches_endpoint(cls, endpoint: LLMEndpointModel) -> bool:
        capabilities = _capabilities(endpoint)
        return bool(capabilities.get("responses_api") or capabilities.get("openai_responses"))

    def new_draft(self, messages: list[dict[str, Any]]) -> OpenAIResponsesDraft:  # type: ignore[override]
        instructions, input_items = chat_messages_to_responses_input(messages)
        return OpenAIResponsesDraft(
            model=self.api_model(),
            instructions=instructions,
            input=input_items,
        )

    def apply_request(self, request: CanonicalLLMRequest, draft: OpenAIResponsesDraft) -> None:  # type: ignore[override]
        effort = _think_level_to_completion_reasoning_effort(request.metadata.get("think_level"))
        if effort is not None:
            draft.reasoning = {"effort": effort}


def chat_messages_to_responses_input(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    input_items: list[dict[str, Any]] = []
    for message in list(messages or []):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user").strip() or "user"
        content = message.get("content")
        if role in {"system", "developer"}:
            text = _content_text(content)
            if text:
                input_items.append({"role": "developer", "content": text})
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "").strip()
            if call_id:
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": _content_text(content),
                    }
                )
            continue
        if role == "assistant":
            output_content = _responses_output_content(content)
            if output_content:
                input_items.append({"type": "message", "role": "assistant", "content": output_content})
            for tool_call in list(message.get("tool_calls") or []):
                item = _responses_function_call(tool_call)
                if item is not None:
                    input_items.append(item)
            continue
        input_content = _responses_input_content(content)
        if input_content:
            input_items.append({"role": "user", "content": input_content})
    if not input_items:
        input_items.append({"role": "user", "content": "Continue."})
    return None, input_items


def chat_tools_to_responses_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for tool in list(tools or []):
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            function = dict(tool.get("function") or {})
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            rendered.append(
                {
                    "type": "function",
                    "name": name,
                    "description": str(function.get("description") or name),
                    "parameters": function.get("input_schema")
                    or {"type": "object", "properties": {}},
                }
            )
            continue
        name = str(tool.get("name") or "").strip()
        if name:
            rendered.append(
                {
                    "type": "function",
                    "name": name,
                    "description": str(tool.get("description") or name),
                    "parameters": tool.get("input_schema")
                    or {"type": "object", "properties": {}},
                }
            )
    return rendered


def _responses_function_call(tool_call: Any) -> dict[str, Any] | None:
    if not isinstance(tool_call, dict):
        return None
    function = tool_call.get("function")
    if not isinstance(function, dict):
        return None
    name = str(function.get("name") or "").strip()
    if not name:
        return None
    call_id = str(tool_call.get("id") or tool_call.get("call_id") or "").strip()
    arguments = function.get("arguments")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
    payload: dict[str, Any] = {
        "type": "function_call",
        "name": name,
        "arguments": arguments,
    }
    if call_id:
        payload["call_id"] = call_id
    return payload


def _responses_input_content(content: Any) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return _content_text(content)
    parts: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        part_type = str(item.get("type") or "").strip()
        if part_type in {"text", "input_text"}:
            text = str(item.get("text") or "")
            if text:
                parts.append({"type": "input_text", "text": text})
            continue
        if part_type in {"image_url", "input_image"}:
            image = item.get("image_url")
            if isinstance(image, dict):
                image = image.get("url")
            image_url = str(image or item.get("image_url") or "").strip()
            if image_url:
                parts.append({"type": "input_image", "image_url": image_url})
    if not parts:
        return ""
    if len(parts) == 1 and parts[0].get("type") == "input_text":
        return str(parts[0].get("text") or "")
    return parts


def _responses_output_content(content: Any) -> list[dict[str, Any]]:
    text = _content_text(content)
    if not text:
        return []
    return [{"type": "output_text", "text": text}]


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
                part_type = str(item.get("type") or "").strip()
                if part_type in {"text", "input_text", "output_text"}:
                    parts.append(str(item.get("text") or ""))
        return "\n".join(part for part in parts if part)
    return str(content)
