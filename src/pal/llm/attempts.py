from __future__ import annotations

from dataclasses import dataclass

from pal.llm.ir import LLMUsageIR


@dataclass(frozen=True)
class LLMAttemptResult:
    attempt_id: str
    endpoint_id: str
    model_id: str
    provider: str
    status: str
    usage: LLMUsageIR
    provider_generation_id: str = ""
    returned_model: str = ""
    actual_provider: str = ""
    service_tier: str = ""
    elapsed_seconds: float = 0.0
    error_type: str = ""
