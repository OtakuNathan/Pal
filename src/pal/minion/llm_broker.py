from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pal.llm.contracts import (
    CanonicalLLMOutcome,
    CanonicalLLMRequest,
    CanonicalToolCall,
    LLMPreflightAdvice,
    LLMPreflightRequest,
)
from pal.minion.ipc import MinionManagerClient


def llm_request_to_payload(request: CanonicalLLMRequest) -> dict[str, Any]:
    return {
        "messages": list(request.messages or []),
        "max_output_tokens": int(request.max_output_tokens or 0),
        "model_hint": request.model_hint,
        "temperature": request.temperature,
        "tools": list(request.tools or []),
        "metadata": dict(request.metadata or {}),
    }


def llm_request_from_payload(payload: dict[str, Any]) -> CanonicalLLMRequest:
    return CanonicalLLMRequest(
        messages=[dict(item) for item in list(payload.get("messages") or []) if isinstance(item, dict)],
        max_output_tokens=int(payload.get("max_output_tokens") or 0),
        model_hint=str(payload.get("model_hint") or "") or None,
        temperature=payload.get("temperature"),
        tools=[dict(item) for item in list(payload.get("tools") or []) if isinstance(item, dict)],
        metadata=dict(payload.get("metadata") or {}),
    )


def preflight_request_to_payload(request: LLMPreflightRequest) -> dict[str, Any]:
    return {
        "messages": list(request.messages or []),
        "max_output_tokens": int(request.max_output_tokens or 0),
        "model_hint": request.model_hint,
        "tools": list(request.tools or []),
        "metadata": dict(request.metadata or {}),
    }


def preflight_request_from_payload(payload: dict[str, Any]) -> LLMPreflightRequest:
    return LLMPreflightRequest(
        messages=[dict(item) for item in list(payload.get("messages") or []) if isinstance(item, dict)],
        max_output_tokens=int(payload.get("max_output_tokens") or 0),
        model_hint=str(payload.get("model_hint") or "") or None,
        tools=[dict(item) for item in list(payload.get("tools") or []) if isinstance(item, dict)],
        metadata=dict(payload.get("metadata") or {}),
    )


def preflight_advice_to_payload(advice: LLMPreflightAdvice) -> dict[str, Any]:
    return {
        "status": advice.status,
        "active_model": advice.active_model,
        "fallback_chain": list(advice.fallback_chain or []),
        "target_input_budget": int(advice.target_input_budget or 0),
        "reserved_output_tokens": int(advice.reserved_output_tokens or 0),
        "breakdown": dict(advice.breakdown or {}),
    }


def preflight_advice_from_payload(payload: dict[str, Any]) -> LLMPreflightAdvice:
    return LLMPreflightAdvice(
        status=str(payload.get("status") or "ready"),
        active_model=str(payload.get("active_model") or "") or None,
        fallback_chain=[str(item) for item in list(payload.get("fallback_chain") or []) if str(item or "").strip()],
        target_input_budget=int(payload.get("target_input_budget") or 0),
        reserved_output_tokens=int(payload.get("reserved_output_tokens") or 0),
        breakdown=dict(payload.get("breakdown") or {}),
    )


def llm_outcome_to_payload(outcome: CanonicalLLMOutcome) -> dict[str, Any]:
    return {
        "text": outcome.text,
        "reasoning_text": outcome.reasoning_text,
        "tool_calls": [
            {"name": call.name, "args": dict(call.args or {}), "call_id": call.call_id}
            for call in list(outcome.tool_calls or [])
        ],
        "finish_reason": outcome.finish_reason,
        "response_mode": outcome.response_mode,
        "target_input_budget": int(outcome.target_input_budget or 0),
        "reserved_output_tokens": int(outcome.reserved_output_tokens or 0),
        "preferred_endpoint_id": outcome.preferred_endpoint_id,
        "preferred_model_id": outcome.preferred_model_id,
        "provider_specific_fields": dict(outcome.provider_specific_fields or {}),
    }


def llm_outcome_from_payload(payload: dict[str, Any]) -> CanonicalLLMOutcome:
    return CanonicalLLMOutcome(
        text=str(payload.get("text") or ""),
        reasoning_text=str(payload.get("reasoning_text") or ""),
        tool_calls=[
            CanonicalToolCall(
                name=str(item.get("name") or ""),
                args=dict(item.get("args") or {}),
                call_id=str(item.get("call_id") or "") or None,
            )
            for item in list(payload.get("tool_calls") or [])
            if isinstance(item, dict)
        ],
        finish_reason=str(payload.get("finish_reason") or "stop"),
        response_mode=str(payload.get("response_mode") or "") or None,
        target_input_budget=int(payload.get("target_input_budget") or 0),
        reserved_output_tokens=int(payload.get("reserved_output_tokens") or 0),
        preferred_endpoint_id=str(payload.get("preferred_endpoint_id") or "") or None,
        preferred_model_id=str(payload.get("preferred_model_id") or "") or None,
        provider_specific_fields=dict(payload.get("provider_specific_fields") or {}),
    )


@dataclass
class MinionBrokerLLMRuntime:
    runtime_root: Path
    run_id: str
    request_timeout_seconds: float = 3900.0

    @property
    def _client(self) -> MinionManagerClient:
        return MinionManagerClient(runtime_root=Path(self.runtime_root), request_timeout_seconds=self.request_timeout_seconds)

    def resolve_max_output_tokens(
        self,
        *,
        preferred_endpoint_id: str | None = None,
        preferred_endpoint_source: str | None = None,
    ) -> int | None:
        result = self._client.request_sync(
            "llm_resolve_max_output_tokens",
            {
                "run_id": self.run_id,
                "preferred_endpoint_id": preferred_endpoint_id or "",
                "preferred_endpoint_source": preferred_endpoint_source or "",
            },
        )
        value = result.get("max_output_tokens")
        return int(value) if value is not None else None

    def resolve_endpoint_facts(self, *args, **kwargs) -> dict[str, Any]:
        preferred_endpoint_id = str(kwargs.get("preferred_endpoint_id") or "").strip()
        preferred_endpoint_source = str(kwargs.get("preferred_endpoint_source") or "").strip()
        if args and not preferred_endpoint_id:
            preferred_endpoint_id = str(args[0] or "").strip()
        return self._client.request_sync(
            "llm_resolve_endpoint_facts",
            {
                "run_id": self.run_id,
                "preferred_endpoint_id": preferred_endpoint_id,
                "preferred_endpoint_source": preferred_endpoint_source,
            },
        )

    def preflight(self, request: LLMPreflightRequest) -> LLMPreflightAdvice:
        result = self._client.request_sync(
            "llm_preflight",
            {"run_id": self.run_id, "request": preflight_request_to_payload(request)},
        )
        return preflight_advice_from_payload(dict(result.get("advice") or {}))

    async def apreflight(self, request: LLMPreflightRequest) -> LLMPreflightAdvice:
        result = await self._client.request(
            "llm_preflight",
            {"run_id": self.run_id, "request": preflight_request_to_payload(request)},
        )
        return preflight_advice_from_payload(dict(result.get("advice") or {}))

    def generate(self, request: CanonicalLLMRequest) -> CanonicalLLMOutcome:
        result = self._client.request_sync(
            "llm_generate",
            {"run_id": self.run_id, "request": llm_request_to_payload(request)},
        )
        return llm_outcome_from_payload(dict(result.get("outcome") or {}))

    async def agenerate(self, request: CanonicalLLMRequest) -> CanonicalLLMOutcome:
        result = await self._client.request(
            "llm_generate",
            {"run_id": self.run_id, "request": llm_request_to_payload(request)},
        )
        return llm_outcome_from_payload(dict(result.get("outcome") or {}))

    def generate_stream(self, request: CanonicalLLMRequest) -> list[Any]:
        _ = request
        raise NotImplementedError("minion LLM broker does not support streaming yet")

    async def agenerate_stream(self, request: CanonicalLLMRequest) -> list[Any]:
        return await asyncio.to_thread(self.generate_stream, request)
