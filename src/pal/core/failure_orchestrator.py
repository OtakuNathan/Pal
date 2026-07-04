from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Awaitable, Callable

from pal.core.turns import EffectResult, FailureFlowOutcome, LLMRequestEffect, ToolCallEffect, failure_turn_program
from pal.execution.contracts import CapabilityCall
from pal.failure import (
    FAILURE_VERIFICATION_DEGRADED,
    FAILURE_VERIFICATION_FAILED,
    FAILURE_VERIFICATION_OK,
    FailureDraft,
    FailureReport,
    FailureRuntime,
    FailureSignal,
    FailureUserFeedback,
    RepairResolutionRecord,
    RepairWorkOrderDraft,
    VerificationResult,
)
from pal.foundation import utc_now
from pal.llm.contracts import CanonicalLLMRequest, CanonicalToolResult
from pal.memory.contracts import L2Entry
from pal.shared import LLMResponseMode, RuntimeStatus


_SAFE_MODE_LLM_TIMEOUT_SECONDS = 45.0


@dataclass(frozen=True)
class FailureHandlingResult:
    verification: VerificationResult
    user_feedback: FailureUserFeedback
    report: FailureReport | None = None
    repair_resolution: RepairResolutionRecord | None = None
    repair_work_order: RepairWorkOrderDraft | None = None


class FailureOrchestrator:
    def __init__(
        self,
        context,
        *,
        call_port_async: Callable[..., Awaitable[Any]],
        build_canonical_prompt: Callable[..., Any],
        debug_log_prompt: Callable[[CanonicalLLMRequest], None],
        tool_surface,
    ) -> None:
        self.context = context
        self._call_port_async = call_port_async
        self._build_canonical_prompt = build_canonical_prompt
        self._debug_log_prompt = debug_log_prompt
        self.tool_surface = tool_surface

    async def handle_failure_async(
        self,
        signal: FailureSignal,
        *,
        origin: str,
        route: str | None = None,
        conversation_context: dict[str, Any] | None = None,
    ) -> FailureHandlingResult:
        failure_runtime = self.failure_runtime()
        draft = failure_runtime.begin_draft(signal)
        failure_runtime.record_document_checked(draft, f"origin:{origin}")
        if route:
            failure_runtime.record_document_checked(draft, f"route:{route}")
        if signal.related_modules:
            for module_name in signal.related_modules:
                failure_runtime.record_document_checked(draft, f"module:{module_name}")
        context_payload = dict(conversation_context or {})
        if signal.subsystem == "llm":
            verification = VerificationResult(
                status=FAILURE_VERIFICATION_FAILED,
                reason="LLM provider fallback was exhausted; inline repair cannot continue without a healthy model endpoint.",
                evidence={"origin": origin, **context_payload},
            )
            failure_runtime.record_verification(draft, verification)
            report = failure_runtime.build_report(
                draft,
                verification=verification,
                enriched_fields={
                    "why_blocked": verification.reason,
                    "current_blocker": draft.primary_blocker,
                    "impact": "LLM-backed reasoning is unavailable for the current turn.",
                    "recommended_next_step": "Restore a healthy LLM endpoint or credential before retrying.",
                },
            )
            feedback = failure_runtime.render_user_feedback(draft, verification=verification, report=report)
            return FailureHandlingResult(verification=verification, user_feedback=feedback, report=report)

        try:
            allowed_descriptors = self.tool_surface.select_failure_descriptors(signal)
            allowed_tools = self.tool_surface.build_tool_contracts_from_descriptors(allowed_descriptors)
            flow_outcome = await self._run_failure_flow_async(draft, allowed_tools=allowed_tools)
        except Exception as exc:
            flow_outcome = _failure_flow_exception_outcome(exc, phase="orchestration")
        verification = flow_outcome.verification
        failure_runtime.record_verification(draft, verification)
        if verification.status == FAILURE_VERIFICATION_OK:
            resolution = failure_runtime.build_repair_resolution_record(draft, verification=verification)
            await self._persist_repair_resolution_async(resolution)
            feedback = failure_runtime.render_user_feedback(draft, verification=verification)
            return FailureHandlingResult(
                verification=verification,
                user_feedback=feedback,
                repair_resolution=resolution,
            )
        if verification.status == FAILURE_VERIFICATION_DEGRADED:
            feedback = failure_runtime.render_user_feedback(draft, verification=verification)
            work_order = failure_runtime.maybe_build_work_order_draft(draft, verification=verification)
            return FailureHandlingResult(
                verification=verification,
                user_feedback=feedback,
                repair_work_order=work_order,
            )
        report = failure_runtime.build_report(draft, verification=verification, enriched_fields=flow_outcome.enriched_fields)
        feedback = failure_runtime.render_user_feedback(draft, verification=verification, report=report)
        work_order = failure_runtime.maybe_build_work_order_draft(draft, verification=verification)
        return FailureHandlingResult(
            verification=verification,
            user_feedback=feedback,
            report=report,
            repair_work_order=work_order,
        )

    async def _run_failure_flow_async(self, draft: FailureDraft, *, allowed_tools: list[dict[str, Any]]) -> FailureFlowOutcome:
        llm_runtime = self.context.port_registry.get("llm:llm")
        if llm_runtime is None:
            return FailureFlowOutcome(
                verification=VerificationResult(
                    status=FAILURE_VERIFICATION_FAILED,
                    reason="Failure flow could not start because no llm runtime is mounted.",
                ),
                enriched_fields={},
            )
        program = failure_turn_program(draft, allowed_tools=allowed_tools)
        current: EffectResult | None = None
        while True:
            try:
                effect = next(program) if current is None else program.send(current)
            except StopIteration as stop:
                return stop.value
            except Exception as exc:
                return _failure_flow_exception_outcome(exc, phase="program")
            if isinstance(effect, LLMRequestEffect):
                request = self._build_safe_mode_request(effect)
                self._debug_log_prompt(request)
                try:
                    outcome = await self._call_port_async(llm_runtime, "agenerate", "generate", request)
                except Exception as exc:
                    return FailureFlowOutcome(
                        verification=VerificationResult(
                            status=FAILURE_VERIFICATION_FAILED,
                            reason=f"Failure safe-mode LLM request failed: {type(exc).__name__}: {exc}",
                            evidence={"error_type": type(exc).__name__},
                        ),
                        enriched_fields={
                            "why_blocked": "Failure safe-mode LLM request failed.",
                            "current_blocker": str(exc),
                            "recommended_next_step": "Surface the original runtime failure and safe-mode LLM failure to the user.",
                        },
                    )
                current = EffectResult(status=RuntimeStatus.OK, payload=outcome)
                continue
            if isinstance(effect, ToolCallEffect):
                try:
                    tool_result = await self._call_port_async(
                        self.context.execution_runtime,
                        "execute_tool_async",
                        "execute_tool",
                        effect.tool_call,
                        allow_tools=True,
                    )
                except Exception as exc:
                    text = f"safe-mode tool failed: {type(exc).__name__}: {exc}"
                    tool_result = CanonicalToolResult(
                        name=effect.tool_call.name,
                        ok=False,
                        llm_text=text,
                        text=text,
                        structured={"error_type": type(exc).__name__},
                    )
                self.failure_runtime().absorb_maintenance_outcome(
                    draft,
                    action_name=effect.tool_call.name,
                    status=RuntimeStatus.OK if tool_result.ok else RuntimeStatus.ERROR,
                    ok=tool_result.ok,
                    text=tool_result.text,
                    structured=tool_result.structured,
                )
                current = EffectResult(
                    status=RuntimeStatus.OK if tool_result.ok else RuntimeStatus.ERROR,
                    payload=tool_result,
                    text=tool_result.text,
                )
                continue
            effect_kind = str(getattr(effect, "kind", "") or type(effect).__name__)
            return FailureFlowOutcome(
                verification=VerificationResult(
                    status=FAILURE_VERIFICATION_FAILED,
                    reason=f"Failure flow yielded unsupported effect: {effect_kind}",
                    evidence={"effect_kind": effect_kind},
                ),
                enriched_fields={
                    "why_blocked": "Failure flow yielded an unsupported internal effect.",
                    "current_blocker": effect_kind,
                    "recommended_next_step": "Surface the original runtime failure and unsupported safe-mode effect to the user.",
                },
            )

    def _build_safe_mode_request(self, effect: LLMRequestEffect) -> CanonicalLLMRequest:
        metadata = dict(effect.assembly_context.metadata or {})
        stage = str(metadata.get("failure_stage") or "diagnose").strip() or "diagnose"
        primary_input = str(metadata.get("failure_primary_input") or "").strip()
        if not primary_input:
            primary_input = json.dumps(
                {
                    "stage": stage,
                    "failure": dict(metadata.get("failure_draft") or {}),
                    "instructions": ["Diagnose the reported Pal runtime failure and return a recovery verdict."],
                },
                ensure_ascii=True,
            )
        return CanonicalLLMRequest(
            messages=[
                {"role": "system", "content": _SAFE_MODE_SYSTEM_PROMPT},
                {"role": "user", "content": primary_input},
            ],
            max_output_tokens=effect.max_output_tokens,
            model_hint=effect.model_hint,
            temperature=0.2,
            tools=list(effect.tools_override or []),
            metadata={
                "response_mode_hint": LLMResponseMode.OPERATIONAL,
                "purpose": "failure_flow",
                "prompt_profile": "safe_mode",
                "failure_stage": stage,
                "timeout_seconds": _SAFE_MODE_LLM_TIMEOUT_SECONDS,
            },
        )

    async def _persist_repair_resolution_async(self, record: RepairResolutionRecord) -> None:
        try:
            memory_service = self.context.require_port("memory:memory")
        except KeyError:
            return
        active_provider_id = str(memory_service.l3_selector.active_provider_id or "").strip()
        if not active_provider_id:
            return
        title = f"{record.subsystem}:{record.failure_kind} repair"
        search_text = (
            f"Situation: {record.situation_text}\n"
            f"Task: {record.task_text}\n"
            f"Action: {record.action_text}\n"
            f"Result: {record.result_text}"
        )
        call_args = {
            "target_id": active_provider_id,
            "kind": "case",
            "scope": "system",
            "title": title,
            "summary": record.result_text,
            "search_text": search_text,
            "topics": [record.subsystem, record.component, "self_healing"],
            "canonical_key": f"repair:{record.subsystem}:{record.component}:{record.failure_kind}",
            "situation_text": record.situation_text,
            "task_text": record.task_text,
            "action_text": record.action_text,
            "result_text": record.result_text,
        }
        await self._call_port_async(
            self.context.execution_runtime,
            "execute_async",
            "execute",
            CapabilityCall(name="op_memory_write", args=call_args),
        )
        memory_service.l2_store.upsert_entries(
            [
                L2Entry(
                    entry_id=f"repair_case:{record.subsystem}:{record.component}:{record.failure_kind}",
                    kind="case",
                    scope="system",
                    title=title,
                    summary=record.result_text,
                    source_kind="repair_resolution",
                    source_ref=call_args["canonical_key"],
                    candidate_state="stable",
                    touched_at=utc_now(),
                    rendered=search_text,
                    payload={
                        "subsystem": record.subsystem,
                        "component": record.component,
                        "failure_kind": record.failure_kind,
                        "situation_text": record.situation_text,
                        "task_text": record.task_text,
                        "action_text": record.action_text,
                        "result_text": record.result_text,
                        "related_ids": dict(record.related_ids),
                    },
                )
            ],
            touch=True,
        )

    def failure_runtime(self) -> FailureRuntime:
        try:
            return self.context.require_port("failure:failure")
        except KeyError:
            runtime = FailureRuntime()
            self.context.port_registry["failure:failure"] = runtime
            return runtime

    def render_failure_feedback_text(self, feedback: FailureUserFeedback) -> str:
        parts = [feedback.summary.strip()]
        if feedback.blocker.strip():
            parts.append(f"Blocker: {feedback.blocker.strip()}")
        if feedback.next_step.strip():
            parts.append(f"Next step: {feedback.next_step.strip()}")
        return "\n".join(part for part in parts if part)


def _failure_flow_exception_outcome(exc: Exception, *, phase: str) -> FailureFlowOutcome:
    error_type = type(exc).__name__
    reason = f"Failure safe-mode flow crashed during {phase}: {error_type}: {exc}"
    return FailureFlowOutcome(
        verification=VerificationResult(
            status=FAILURE_VERIFICATION_FAILED,
            reason=reason,
            evidence={"error_type": error_type, "phase": phase},
        ),
        enriched_fields={
            "why_blocked": f"Failure safe-mode flow crashed during {phase}.",
            "current_blocker": str(exc),
            "recommended_next_step": "Surface the original runtime failure and safe-mode crash to the user.",
        },
    )


_SAFE_MODE_SYSTEM_PROMPT = (
    "You are Pal Safe Mode, an internal runtime recovery worker.\n"
    "\n"
    "Your job is to diagnose and recover the reported Pal runtime failure. Do not answer the user's original request.\n"
    "You are given only a sanitized failure packet; do not ask for or infer user conversation context.\n"
    "\n"
    "Operating rules:\n"
    "- Use introspection capabilities first to inspect live module, provider, capability, endpoint, or sidecar state.\n"
    "- Use dedicated built-in operation, management, health, reload, or refresh capabilities when they directly repair the blocker.\n"
    "- Do not modify Pal source code, runtime files, config files, database rows, or user data directly.\n"
    "- Do not restart Pal itself.\n"
    "- Do not use shell unless a shell capability is explicitly provided in this safe-mode tool surface and the failure domain requires it.\n"
    "- Work only on the primary blocker in the failure packet.\n"
    "- If a safe-mode tool fails, treat that as evidence; do not start another failure flow.\n"
    "- Prefer minimal, reversible runtime recovery.\n"
    "\n"
    "Return valid JSON only. Use this shape:\n"
    "{\n"
    '  "verification_status": "ok|degraded|failed",\n'
    '  "reason": "short result summary",\n'
    '  "checked": ["live facts inspected"],\n'
    '  "actions_taken": ["bounded recovery actions"],\n'
    '  "why_blocked": "required when not ok",\n'
    '  "current_blocker": "current blocker if any",\n'
    '  "impact": "runtime impact",\n'
    '  "possible_solutions": ["developer or user next steps"],\n'
    '  "recommended_next_step": "what main Pal/user should do next"\n'
    "}"
)
