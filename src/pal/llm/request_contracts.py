"""Generation plans and transport admission receipts, independent of runtime owners."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from pal.llm.endpoint_spec import LLMEndpointSpec
from pal.llm.ir import LLMRequestIR
from pal.llm.projection_contracts import EndpointBinding


@dataclass(frozen=True)
class RequestSubmission:
    """Observed response admission, independent of output or cache success."""

    request_id: str
    endpoint_id: str
    model_id: str
    message_ids: tuple[str, ...]


@dataclass(frozen=True)
class LLMProgressEvent:
    payload: dict[str, Any]


@dataclass(frozen=True)
class PreparedLLMRequest:
    endpoint: LLMEndpointSpec
    request: LLMRequestIR
    estimated_input_tokens: int
    target_input_budget: int

    @property
    def compact_required(self) -> bool:
        return (
            self.target_input_budget > 0
            and self.estimated_input_tokens > self.target_input_budget
        )


@dataclass(frozen=True)
class LLMPreparedPlan:
    """One immutable prepared-generation plan (F1, review af51d74).

    Derived BEFORE any projection encodes anything: the resolved endpoint
    (the request's explicit preference or active endpoint), the compiled
    EFFECTIVE request (model hooks, effective thinking, output caps, cache
    policy selection), the validated capability profile, and the strong
    projection binding.  A live projection encodes ``effective_request``
    and only THIS plan's endpoint may apply it; anything else must report
    the projection unapplied (F2 receipt).
    """

    endpoint: LLMEndpointSpec
    prepared: PreparedLLMRequest
    binding: EndpointBinding
    capabilities: Mapping[str, Any]

    @property
    def effective_request(self) -> LLMRequestIR:
        return self.prepared.request

    @property
    def endpoint_id(self) -> str:
        return str(self.endpoint.endpoint_id)

    @property
    def model_id(self) -> str:
        return str(self.endpoint.model_id)

    @property
    def wire_shape(self) -> str:
        return str(self.endpoint.wire_shape)

    @property
    def compact_required(self) -> bool:
        return self.prepared.compact_required
