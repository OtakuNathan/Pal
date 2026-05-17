from __future__ import annotations

from dataclasses import dataclass
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
from pal.llm.contracts import CanonicalLLMRequest
from pal.memory.contracts import L2Entry
from pal.shared import LLMResponseMode, RuntimeStatus


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

        allowed_descriptors = self.tool_surface.select_failure_descriptors(signal)
        allowed_tools = self.tool_surface.build_tool_contracts_from_descriptors(allowed_descriptors)
        flow_outcome = await self._run_failure_flow_async(draft, allowed_tools=allowed_tools)
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
            if isinstance(effect, LLMRequestEffect):
                prompt = self._build_canonical_prompt(
                    effect.assembly_context,
                    max_output_tokens=effect.max_output_tokens,
                    model_hint=effect.model_hint,
                )
                request = CanonicalLLMRequest(
                    messages=list(prompt.messages),
                    max_output_tokens=prompt.max_output_tokens,
                    model_hint=prompt.model_hint,
                    temperature=0.2,
                    tools=list(effect.tools_override or []),
                    metadata={**dict(prompt.metadata), "response_mode_hint": LLMResponseMode.OPERATIONAL, "purpose": "failure_flow"},
                )
                self._debug_log_prompt(request)
                outcome = await self._call_port_async(llm_runtime, "agenerate", "generate", request)
                current = EffectResult(status=RuntimeStatus.OK, payload=outcome)
                continue
            if isinstance(effect, ToolCallEffect):
                tool_result = await self._call_port_async(
                    self.context.execution_runtime,
                    "execute_tool_async",
                    "execute_tool",
                    effect.tool_call,
                    allow_tools=True,
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
            current = EffectResult(status=RuntimeStatus.UNSUPPORTED, text=f"unsupported failure effect: {effect.kind}")

    async def _persist_repair_resolution_async(self, record: RepairResolutionRecord) -> None:
        try:
            memory_service = self.context.require_port("memory:memory")
        except KeyError:
            return
        active_provider_id = str(memory_service.l3_selector.active_provider_id or "").strip()
        if not active_provider_id:
            return
        title = f"{record.subsystem}:{record.failure_kind} repair"
        call_args = {
            "target_id": active_provider_id,
            "kind": "case",
            "scope": "system",
            "title": title,
            "summary": record.result_text,
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
        summary = (
            f"Situation: {record.situation_text}\n"
            f"Task: {record.task_text}\n"
            f"Action: {record.action_text}\n"
            f"Result: {record.result_text}"
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
                    rendered=summary,
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
