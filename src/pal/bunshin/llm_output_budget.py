from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pal.llm import EndpointResolver
from pal.llm.endpoint_spec import LLMEndpointSpec
from pal.llm.models import LLMEndpointModel
from pal.bunshin.runner_components.numeric_values import _optional_positive_int


def with_role_output_budget(endpoint: LLMEndpointModel, value: Any) -> LLMEndpointModel:
    """Project the bound role's budget without changing the resident endpoint."""
    budget = _optional_positive_int(value)
    if budget is None:
        return endpoint
    payload = LLMEndpointSpec.from_value(endpoint).to_payload()
    context_window = payload.get("context_window")
    payload["max_output_tokens"] = min(budget, context_window) if context_window else budget
    return LLMEndpointModel(**LLMEndpointSpec.from_value(payload).to_payload())


@dataclass
class BunshinEndpointResolver(EndpointResolver):
    max_output_tokens_override: int | None = None

    def refresh(self) -> tuple[LLMEndpointModel, ...]:
        endpoints = super().refresh()
        self.endpoints = tuple(
            with_role_output_budget(endpoint, self.max_output_tokens_override)
            for endpoint in endpoints
        )
        return self.endpoints
