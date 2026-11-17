from __future__ import annotations

import json
import re
from collections.abc import Generator
from typing import Any

from pal.core.turns import (EffectRequest, EffectResult, ToolObservation, LLMRequestEffect,
    ToolCallEffect, _as_llm_outcome, _append_tool_observation)
from pal.failure.contracts import FAILURE_VERIFICATION_FAILED, FailureDraft, VerificationResult, FailureFlowOutcome
from pal.failure.presentation import _failure_draft_debug_payload, _render_failure_primary_input
from pal.shared import PromptAssemblyContext


FailureProgram = Generator[EffectRequest, EffectResult, FailureFlowOutcome]


def failure_turn_program(
    draft: FailureDraft,
    *,
    allowed_tools: list[dict[str, Any]],
    max_output_tokens: int = 768,
    max_maintenance_batches: int = 2,
) -> FailureProgram:
    observations: list[ToolObservation] = []
    request_stage = "diagnose"
    for batch_index in range(max_maintenance_batches):
        diagnose_context = PromptAssemblyContext(
            turn_kind="failure",
            metadata={
                "failure_draft": _failure_draft_debug_payload(draft),
                "failure_stage": request_stage,
                "failure_primary_input": _render_failure_primary_input(
                    draft,
                    allowed_tools=allowed_tools,
                    observations=observations,
                    stage=request_stage,
                ),
            },
        )
        diagnose_result = yield LLMRequestEffect(
            assembly_context=diagnose_context,
            max_output_tokens=max_output_tokens,
            tools_override=list(allowed_tools),
        )
        outcome = _as_llm_outcome(diagnose_result.payload)
        if outcome is None:
            return FailureFlowOutcome(
                verification=VerificationResult(status=FAILURE_VERIFICATION_FAILED, reason="Failure diagnosis did not return an LLM outcome."),
                enriched_fields={},
            )
        if outcome.tool_calls:
            for tool_call in outcome.tool_calls:
                tool_result = yield ToolCallEffect(tool_call=tool_call)
                _append_tool_observation(observations, tool_result.payload)
            verify_context = PromptAssemblyContext(
                turn_kind="failure",
                metadata={
                    "failure_draft": _failure_draft_debug_payload(draft),
                    "failure_stage": "verify",
                    "observation_blocks": [item.to_prompt_block() for item in observations],
                    "failure_primary_input": _render_failure_primary_input(
                        draft,
                        allowed_tools=allowed_tools,
                        observations=observations,
                        stage="verify",
                    ),
                },
            )
            verify_result = yield LLMRequestEffect(
                assembly_context=verify_context,
                max_output_tokens=max_output_tokens,
                tools_override=[],
            )
            verify_outcome = _as_llm_outcome(verify_result.payload)
            if verify_outcome is not None:
                verification, enriched_fields = _parse_failure_verification(verify_outcome.text)
                if verification.status != FAILURE_VERIFICATION_FAILED or batch_index >= max_maintenance_batches - 1:
                    return FailureFlowOutcome(verification=verification, enriched_fields=enriched_fields)
                request_stage = "maintain"
                continue
            return FailureFlowOutcome(
                verification=VerificationResult(status=FAILURE_VERIFICATION_FAILED, reason="Failure verification did not return an LLM outcome."),
                enriched_fields={},
            )
        verification, enriched_fields = _parse_failure_verification(outcome.text)
        return FailureFlowOutcome(verification=verification, enriched_fields=enriched_fields)
    return FailureFlowOutcome(
        verification=VerificationResult(status=FAILURE_VERIFICATION_FAILED, reason="Inline repair budget exhausted."),
        enriched_fields={},
    )


def _parse_failure_verification(text: str) -> tuple[VerificationResult, dict[str, Any]]:
    raw = str(text or "").strip()
    if not raw:
        return VerificationResult(status=FAILURE_VERIFICATION_FAILED, reason="Failure verification produced no output."), {}
    payload = _extract_json_object(raw)
    if isinstance(payload, dict):
        status = str(payload.get("verification_status") or payload.get("status") or "").strip().lower()
        if status not in {"ok", "degraded", "failed"}:
            status = FAILURE_VERIFICATION_FAILED
        reason = str(payload.get("reason") or payload.get("why_blocked") or raw).strip()
        fields = {
            "why_blocked": payload.get("why_blocked"),
            "current_blocker": payload.get("current_blocker"),
            "impact": payload.get("impact"),
            "possible_solutions": payload.get("possible_solutions"),
            "recommended_next_step": payload.get("recommended_next_step"),
        }
        return VerificationResult(status=status, reason=reason, evidence={"raw": raw}), {key: value for key, value in fields.items() if value}
    match = re.search(r"\b(ok|degraded|failed)\b", raw.lower())
    status = match.group(1) if match is not None else FAILURE_VERIFICATION_FAILED
    return VerificationResult(status=status, reason=raw, evidence={"raw": raw}), {}


def _extract_json_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    candidate = text[start : end + 1]
    try:
        payload = json.loads(candidate)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None
