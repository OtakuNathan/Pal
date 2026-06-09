from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class OpenAIResponsesDraft:
    model: str
    input: list[dict[str, Any]]
    instructions: str | None = None
    timeout: float | None = None
    api_base: str | None = None
    api_key: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_choice: Any = None
    reasoning: dict[str, Any] | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

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
        }
        kwargs.update({key: value for key, value in optional_values.items() if value is not None})
        if self.tools:
            kwargs["tools"] = self.tools
        if self.extra_body:
            kwargs["extra_body"] = dict(self.extra_body)
        kwargs.update(self.extra)
        return kwargs
