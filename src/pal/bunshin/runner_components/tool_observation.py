from __future__ import annotations
from pal.bunshin.verifier_tool_diagnostics import (
    VerifierFailureProvenance, capture_verifier_failure, is_verifier_pack,
    record_verifier_failure, verifier_tool_alias, verifier_tool_diagnostic,
)
from pal.bunshin.runner_components.models import BunshinAgentLoopState
from pal.bunshin.runner_components.prompt_values import _tool_result_text
from pal.bunshin.runner_components.prompt_values import _BUNSHIN_TOOL_RESULT_RETENTION_CALLS
from pal.bunshin.runner_components.progress_text import _json_preview
from pal.bunshin.runner_components.progress_text import _preview_text
from pal.shared.tool_protocol import ToolCallIR
from dataclasses import dataclass
from typing import Any, Literal
from pal.core.turns import TurnContinuation
from pal.bunshin.scoped_execution import _effective_capability_name
from pal.shared import ToolExecutionResult
from pal.bunshin.runner_components.heartbeat import Heartbeat
from pal.bunshin.runner_components.reporter import Reporter
from pal.bunshin.runner_components.tool_execution import ToolExecution
from pal.bunshin.runner_components.tool_session import ToolSession


@dataclass
class ToolObservation:
    heartbeat: Heartbeat
    reporter: Reporter
    tool_execution: ToolExecution
    tool_session: ToolSession

    async def execute_bunshin_tool_with_observation(
        self,
        state: BunshinAgentLoopState,
        continuation: TurnContinuation,
        call: ToolCallIR,
        *,
        allow_tools: bool = True,
        budget: Any = None,
        turn_id: str | None = None,
    ) -> ToolExecutionResult:
        index = len(continuation.pending_tool_results)
        await self.emit_verifier_diagnostic(state, call, index, "started")
        target_name = _effective_capability_name(call)
        await self.reporter.emit_progress(
            "tool_call_started",
            round=state.llm_round_count,
            tool_call_index=index,
            tool_name=call.name,
            target_name=target_name,
            args_preview=_json_preview(call.args),
        )
        self.reporter.append_debug_log(
            "tool_call_started",
            {
                "round": state.llm_round_count,
                "tool_call_index": index,
                "tool_name": call.name,
                "target_name": target_name,
                "args": dict(call.args),
            },
        )
        advance_result_clock = getattr(
            state.execution_runtime,
            "advance_tool_result_clock",
            None,
        )
        with capture_verifier_failure(
            enabled=is_verifier_pack(self.reporter.pack) and bool(verifier_tool_alias(call)),
        ) as capture:
            try:
                if callable(advance_result_clock):
                    advance_result_clock(
                        turn_id=turn_id or continuation.turn_id,
                        clock_id=f"tool:{call.call_id}",
                        retention_steps=_BUNSHIN_TOOL_RESULT_RETENTION_CALLS,
                    )
                operation = self.tool_execution.execute_allowed_tool(
                    state.execution_runtime, call, allow_tools=allow_tools,
                    budget=budget, turn_id=turn_id or continuation.turn_id,
                )
                result = await self.heartbeat.await_with_progress_heartbeat(
                    operation,
                    phase="tool_call_waiting",
                    round=state.llm_round_count,
                    tool_call_index=index,
                    tool_name=call.name,
                    target_name=target_name,
                )
            except Exception as exc:
                record_verifier_failure(exc)
                await self.emit_verifier_diagnostic(state, call, index, "failed", provenance=capture.provenance)
                self.reporter.append_debug_log(
                    "tool_call_failed",
                    {
                        "round": state.llm_round_count,
                        "tool_call_index": index,
                        "tool_name": call.name,
                        "target_name": target_name,
                        "error_type": exc.__class__.__name__,
                        "error": str(exc),
                    },
                )
                await self.reporter.emit_progress(
                    "tool_call_failed",
                    round=state.llm_round_count,
                    tool_call_index=index,
                    tool_name=call.name,
                    target_name=target_name,
                    error_type=exc.__class__.__name__,
                    error=_preview_text(str(exc), limit=500),
                )
                raise
            await self.emit_verifier_diagnostic(state, call, index, "completed", result=result, provenance=capture.provenance)
        state.tool_call_count += 1
        self.tool_session.observe_count(max(
            self.tool_session.observed_tool_call_count,
            state.tool_call_count,
        ))
        self.reporter.append_debug_log(
            "tool_call_completed",
            {
                "round": state.llm_round_count,
                "tool_call_index": index,
                "tool_name": call.name,
                "target_name": target_name,
                "ok": bool(result.ok),
                "status": str(result.status or ""),
                "text": _tool_result_text(result),
                "structured": dict(result.structured or {}),
            },
        )
        await self.reporter.emit_progress(
            "tool_call_completed",
            round=state.llm_round_count,
            tool_call_index=index,
            tool_name=call.name,
            target_name=target_name,
            ok=bool(result.ok),
            status=str(result.status or ""),
            text_preview=_preview_text(_tool_result_text(result)),
        )
        return result

    async def emit_verifier_diagnostic(
        self, state: BunshinAgentLoopState, call: ToolCallIR, index: int,
        stage: Literal["started", "completed", "failed"], *,
        result: ToolExecutionResult | None = None,
        provenance: VerifierFailureProvenance | None = None,
    ) -> None:
        if not is_verifier_pack(self.reporter.pack):
            return
        payload = verifier_tool_diagnostic(
            call, round_index=state.llm_round_count, tool_call_index=index,
            stage=stage, result=result, provenance=provenance,
        )
        if payload is not None:
            try:
                await self.reporter.emit("verifier_tool_diagnostic", payload)
            except Exception:
                # Optional telemetry must not prevent or repeat a tool effect.
                return
