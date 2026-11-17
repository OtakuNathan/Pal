from __future__ import annotations

from pal.memory.contracts import MEMORY

from dataclasses import replace
import asyncio
import json
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from pal.core.core_events import (
    FAILURE_STARTED, FAILURE_FINISHED, SAFE_MODE_STARTED, SAFE_MODE_FINISHED,
)
from pal.core.turns import EffectResult, LLMRequestEffect, ToolCallEffect
from pal.core.tool_surface import ToolSurface
from pal.control.runtime_contracts import CORE_CONTROL
from pal.failure.contracts import FailureFlowOutcome, FailureHandlingResult
from pal.failure.flow import failure_turn_program
from pal.failure.tools import select_failure_descriptors
from pal.failure.runtime import FailureRuntime
from pal.failure.contracts import (
    FAILURE_VERIFICATION_DEGRADED,
    FAILURE_VERIFICATION_FAILED,
    FAILURE_VERIFICATION_OK,
    FailureDraft,
    FailureSignal,
    RepairResolutionRecord,
    VerificationResult,
)
from pal.shared.diagnostics import exception_reason
from pal.llm.conversions import request_ir_from_prompt
from pal.llm.ir import LLMRequestIR
from pal.memory.contracts import MemoryCase, L3MutationResult
from pal.shared import LLMResponseMode, RuntimeStatus


if TYPE_CHECKING:
    from pal.core.main_context import MainContext


_SAFE_MODE_LLM_TIMEOUT_SECONDS = 45.0


class FailureRecovery:
    def __init__(self, context: MainContext, runtime: FailureRuntime) -> None:
        self.context = context
        self.runtime = runtime
        self.host = context.port_registry.observe(CORE_CONTROL)
        self.tool_surface = ToolSurface(context)

    async def handle_failure_async(
        self,
        signal: FailureSignal,
        *,
        origin: str,
        route: str | None = None,
        conversation_context: dict[str, Any] | None = None,
    ) -> FailureHandlingResult:
        failure_id = uuid4().hex
        event = {
            "failure_id": failure_id, "subsystem": signal.subsystem,
            "component": signal.component, "failure_kind": signal.failure_kind,
            "severity": signal.severity, "related_ids": dict(signal.related_ids),
        }
        bus = self.context.core_event_bus
        bus.emit(FAILURE_STARTED, event)
        status = "failed"
        try:
            result = await self._handle_failure_async(signal, origin=origin, route=route,
                conversation_context=conversation_context, failure_event=event)
            status = result.verification.status
            return result
        except asyncio.CancelledError:
            status = "interrupted"
            raise
        finally:
            bus.emit(FAILURE_FINISHED, {**event, "status": status})

    async def _handle_failure_async(
        self, signal: FailureSignal, *, origin: str, route: str | None,
        conversation_context: dict[str, Any] | None, failure_event: dict[str, Any],
    ) -> FailureHandlingResult:
        failure_runtime = self.runtime
        draft = failure_runtime.begin_draft(signal)
        failure_runtime.record_document_checked(draft, f"origin:{origin}")
        if route:
            failure_runtime.record_document_checked(draft, f"route:{route}")
        if signal.related_modules:
            for module_name in signal.related_modules:
                failure_runtime.record_document_checked(draft, f"module:{module_name}")
        context_payload = dict(conversation_context or {})
        if signal.subsystem == "persistence":
            verification = VerificationResult(
                status=FAILURE_VERIFICATION_FAILED,
                reason=(
                    "Local runtime state is unavailable; inline repair was stopped "
                    "to avoid compounding possible database damage."
                ),
                evidence={"origin": origin, **context_payload},
            )
            failure_runtime.record_verification(draft, verification)
            report = failure_runtime.build_report(
                draft,
                verification=verification,
                enriched_fields={
                    "why_blocked": verification.reason,
                    "current_blocker": draft.primary_blocker,
                    "impact": "Durable runtime state cannot be read safely.",
                    "recommended_next_step": (
                        "Stop Pal, back up the runtime database, and verify or "
                        "restore it before retrying."
                    ),
                },
            )
            feedback = failure_runtime.render_user_feedback(
                draft,
                verification=verification,
                report=report,
            )
            return FailureHandlingResult(
                verification=verification,
                user_feedback=feedback,
                report=report,
            )
        if signal.subsystem == "llm":
            subscription_guidance = str(signal.evidence.get("user_guidance") or "")
            decode_failure = signal.failure_kind == "decode_error"
            verification = VerificationResult(
                status=FAILURE_VERIFICATION_FAILED,
                reason=(
                    subscription_guidance if subscription_guidance else
                    "LLM response decoding failed; inline repair cannot continue with this response."
                    if decode_failure else
                    "The selected LLM endpoint failed; inline repair cannot continue without a healthy model endpoint."
                ),
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
                    "recommended_next_step": (
                        subscription_guidance if subscription_guidance else
                        "Inspect the LLM decode diagnostic in the service logs; retry the request, "
                        "or use another endpoint if the error persists."
                        if decode_failure else
                        "Restore a healthy LLM endpoint or credential before retrying."
                    ),
                },
            )
            feedback = failure_runtime.render_user_feedback(draft, verification=verification, report=report)
            return FailureHandlingResult(verification=verification, user_feedback=feedback, report=report)

        try:
            allowed_descriptors = select_failure_descriptors(self.context, signal)
            allowed_tools = self.tool_surface.build_tool_contracts_from_descriptors(allowed_descriptors)
            bus = self.context.core_event_bus
            # LLM/persistence reports above do not enter safe mode.
            if self.context.port_registry.get("llm:llm") is not None:
                bus.emit(SAFE_MODE_STARTED, failure_event)
                try:
                    flow_outcome = await self._run_failure_flow_async(draft, allowed_tools=allowed_tools)
                finally:
                    bus.emit(SAFE_MODE_FINISHED, failure_event)
            else:
                flow_outcome = await self._run_failure_flow_async(draft, allowed_tools=allowed_tools)
        except Exception as exc:
            flow_outcome = _failure_flow_exception_outcome(exc, phase="orchestration")
        verification = flow_outcome.verification
        failure_runtime.record_verification(draft, verification)
        if verification.status == FAILURE_VERIFICATION_OK:
            resolution = failure_runtime.build_repair_resolution_record(draft, verification=verification)
            persistence = self._persist_repair_resolution(resolution)
            feedback = failure_runtime.render_user_feedback(draft, verification=verification)
            feedback = replace(feedback, details={**feedback.details, "repair_memory": {
                "status": persistence.status, "document_id": persistence.document_id, **persistence.metadata}})
            if persistence.status != RuntimeStatus.OK:
                feedback = replace(feedback, next_step="Recovery succeeded, but the repair case could not be saved to memory.")
                self.host.current.record_diagnostic("failure.repair_memory.failed", dict(feedback.details["repair_memory"]))
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
        safe_mode_scope_id = f"pal:safe_mode:{uuid4().hex}"
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
                request = self._build_safe_mode_request(
                    effect,
                    logical_scope_id=safe_mode_scope_id,
                )
                try:
                    outcome = await self.host.current.generate(request)
                except Exception as exc:
                    diagnostic = exception_reason(exc)
                    return FailureFlowOutcome(
                        verification=VerificationResult(
                            status=FAILURE_VERIFICATION_FAILED,
                            reason=f"Failure safe-mode LLM request failed: {diagnostic}",
                            evidence={"error_type": type(exc).__name__},
                        ),
                        enriched_fields={
                            "why_blocked": "Failure safe-mode LLM request failed.",
                            "current_blocker": diagnostic,
                            "recommended_next_step": "Surface the original runtime failure and safe-mode LLM failure to the user.",
                        },
                    )
                current = EffectResult(status=RuntimeStatus.OK, payload=outcome)
                continue
            if isinstance(effect, ToolCallEffect):
                call = effect.tool_call
                tool_result = await self.host.current.execute_local_tool(call,
                    scope_id=safe_mode_scope_id, phase="safe_mode",
                    turn_id=draft.related_ids.get("turn_id", ""))
                self.runtime.absorb_maintenance_outcome(
                    draft,
                    action_name=effect.tool_call.name,
                    status=RuntimeStatus.OK if tool_result.ok else RuntimeStatus.ERROR,
                    ok=tool_result.ok,
                    text=tool_result.text,
                    structured=tool_result.structured,
                    call_id=effect.tool_call.call_id,
                )
                current = EffectResult(
                    status=RuntimeStatus.OK if tool_result.ok else RuntimeStatus.ERROR,
                    payload=tool_result,
                    text=tool_result.text,
                )
                continue
            effect_kind = type(effect).__name__
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

    def _build_safe_mode_request(
        self,
        effect: LLMRequestEffect,
        *,
        logical_scope_id: str,
    ) -> LLMRequestIR:
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
        request = request_ir_from_prompt(
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
                "prompt_cache_scope_id": logical_scope_id,
                "failure_stage": stage,
                "timeout_seconds": _SAFE_MODE_LLM_TIMEOUT_SECONDS,
            },
        )
        return replace(request, logical_scope_id=logical_scope_id)

    def _persist_repair_resolution(self, record: RepairResolutionRecord) -> L3MutationResult:
        memory = self.context.port_registry.optional(MEMORY)
        if memory is None:
            return L3MutationResult(status="unavailable", document_id="", metadata={"reason": "Memory is not mounted"})
        try:
            return memory.remember_case(MemoryCase(
                canonical_key=f"repair:{record.subsystem}:{record.component}:{record.failure_kind}",
                title=f"{record.subsystem}:{record.failure_kind} repair",
                situation=record.situation_text, task=record.task_text,
                action=record.action_text, result=record.result_text,
                topics=(record.subsystem, record.component, "self_healing"),
                metadata={"subsystem": record.subsystem, "component": record.component,
                          "failure_kind": record.failure_kind, "related_ids": dict(record.related_ids)}))
        except Exception as exc:
            return L3MutationResult(status="error", document_id="", metadata={"reason": exception_reason(exc)})


def _failure_flow_exception_outcome(exc: Exception, *, phase: str) -> FailureFlowOutcome:
    error_type = type(exc).__name__
    reason = f"Failure safe-mode flow crashed during {phase}: {exception_reason(exc)}"
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
