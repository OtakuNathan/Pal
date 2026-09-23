"""Observe only provider identity and usage, including undecodable responses."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from pal.llm.ir import LLMUsageIR, WireShape
from pal.llm.usage_normalization import merge_usage, usage_from_mapping
from pal.shared.json_values import thaw_json


@dataclass
class WireResponseEvidence:
    wire_shape: WireShape
    usage: LLMUsageIR = field(default_factory=LLMUsageIR)
    provider_generation_id: str = ""
    returned_model: str = ""
    actual_provider: str = ""
    service_tier: str = ""
    reasoning_context: str = ""
    prompt_cache_diagnostics: dict = field(default_factory=dict)
    last_sequence: int = -1
    last_provider_sequence: int = -1

    def observe(self, frame: Any) -> None:
        if frame.sequence <= self.last_sequence:
            return
        self.last_sequence = frame.sequence
        payload = frame.payload
        event = str(payload.get("type") or "")
        provider_sequence = payload.get("sequence_number")
        if isinstance(provider_sequence, int):
            if provider_sequence <= self.last_provider_sequence:
                return
            self.last_provider_sequence = provider_sequence
        nested = payload.get("response", payload.get("message"))
        objects = [nested, payload] if isinstance(nested, Mapping) else [payload]
        final = event in {"response.completed", "response.failed", "response.incomplete", "message_delta", "message_stop"}
        choices = payload.get("choices")
        complete_chat = isinstance(choices, (list, tuple)) and (
            not choices or any(isinstance(c, Mapping) and ("message" in c or c.get("finish_reason")) for c in choices)
        )
        final = final or bool(not event and ("output" in payload or "content" in payload or complete_chat))
        for source in objects:
            if self.wire_shape == WireShape.OPENAI_RESPONSE:
                reasoning = source.get("reasoning")
                mode = reasoning.get("context") if isinstance(reasoning, Mapping) else None
                if isinstance(mode, str) and mode.strip():
                    self.reasoning_context = mode.strip()
            diagnostics = source.get("prompt_cache_diagnostics")
            if isinstance(diagnostics, Mapping):
                # Best-effort request diagnostics, never a per-marker receipt.
                self.prompt_cache_diagnostics = {k: thaw_json(diagnostics[k]) for k in
                    ("type", "reason", "comparison_response_id") if k in diagnostics}
            identity_object = source is nested or any(k in source for k in ("output", "content", "choices"))
            if identity_object:
                for wire_key, attr in (("id", "provider_generation_id"), ("model", "returned_model"),
                                       ("provider", "actual_provider"), ("service_tier", "service_tier")):
                    value = source.get(wire_key)
                    if isinstance(value, str) and value.strip():
                        setattr(self, attr, value.strip())
            usage = source.get("usage")
            if isinstance(usage, Mapping):
                accounting = "exclusive_cache" if self.wire_shape == WireShape.ANTHROPIC_MESSAGES else "inclusive_cache"
                self.usage = merge_usage(self.usage, usage_from_mapping(
                    usage, input_accounting=accounting, final=final,
                ))
