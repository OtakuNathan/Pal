from __future__ import annotations
from pal.bunshin.runner_components.progress_text import _preview_text
from pal.llm.projection_contracts import ProjectionSendReceipt
import asyncio
import json
from typing import Any
from uuid import uuid4
from pal.llm.contracts import LLMRuntimePort, LLMPreflightAdvice, LLMPreflightRequest
from pal.llm.ir import LLMRequestIR
from pal.shared import ResponseHandle, TurnDeliveryBinding
from pal.bunshin.runner_components.reporter import Reporter
from pal.bunshin.runner_components.heartbeat import Heartbeat


class _BunshinLLMRuntimeAdapter:
    """Role progress around the same explicit LLM contract used by resident Core."""

    def __init__(self, reporter: Reporter, heartbeat: Heartbeat, base_runtime: LLMRuntimePort, state: "BunshinAgentLoopState") -> None:
        self._reporter = reporter
        self._heartbeat = heartbeat
        self._base = base_runtime
        self._state = state

    @property
    def last_endpoint_id(self) -> str | None:
        return self._base.last_endpoint_id

    @property
    def last_model_id(self) -> str | None:
        return self._base.last_model_id

    @property
    def last_projection_receipt(self) -> ProjectionSendReceipt | None:
        return self._base.last_projection_receipt

    @property
    def projection_port(self):
        return self._base.projection_port

    def supports_streaming(self, request: LLMRequestIR | None = None) -> bool:
        return self._base.supports_streaming(request)

    def prompt_cache_eligible_anchor_request(self, *, logical_scope_id="pal:resident", endpoint_id=""):
        return self._base.prompt_cache_eligible_anchor_request(
            logical_scope_id=logical_scope_id, endpoint_id=endpoint_id,
        )

    def prompt_cache_confirmed_anchor_request(self, *, logical_scope_id="pal:resident", endpoint_id=""):
        return self._base.prompt_cache_confirmed_anchor_request(
            logical_scope_id=logical_scope_id, endpoint_id=endpoint_id,
        )

    def resolve_max_output_tokens(self, *, preferred_endpoint_id=None, preferred_endpoint_source=None):
        return self._base.resolve_max_output_tokens(
            preferred_endpoint_id=preferred_endpoint_id,
            preferred_endpoint_source=preferred_endpoint_source,
        )

    def resolve_endpoint_facts(self, *, preferred_endpoint_id=None, preferred_endpoint_source=None):
        return self._base.resolve_endpoint_facts(
            preferred_endpoint_id=preferred_endpoint_id,
            preferred_endpoint_source=preferred_endpoint_source,
        )

    def preflight(self, request: LLMPreflightRequest) -> LLMPreflightAdvice:
        return self._base.preflight(request)

    async def apreflight(self, request: LLMPreflightRequest) -> LLMPreflightAdvice:
        return await self._base.apreflight(request)

    def generate(self, request, *, projection=None, projection_binding=None,
                 projection_attempt_id="", generation_plan=None, on_submitted=None):
        return self._base.generate(
            request, projection=projection, projection_binding=projection_binding,
            projection_attempt_id=projection_attempt_id, generation_plan=generation_plan,
            on_submitted=on_submitted,
        )

    async def agenerate(self, request, *, projection=None, projection_binding=None,
                        projection_attempt_id="", generation_plan=None, on_submitted=None,
                        on_event=None):
        is_compaction = "compaction" in str(request.metadata.get("purpose") or "").lower()
        if not is_compaction:
            await self._reporter.emit_progress(
                "llm_round_started", round=self._state.llm_round_count,
                tool_call_count=self._state.tool_call_count, tool_count=len(request.tools),
            )
        awaitable = self._base.agenerate(
            request, projection=projection, projection_binding=projection_binding,
            projection_attempt_id=projection_attempt_id, generation_plan=generation_plan,
            on_submitted=on_submitted, on_event=self._progress_sink(on_event),
        )
        if is_compaction:
            return await awaitable
        return await self._heartbeat.await_with_progress_heartbeat(
            awaitable, phase="llm_round_waiting", round=self._state.llm_round_count,
            tool_call_count=self._state.tool_call_count,
        )

    async def astream(self, request, *, projection=None, projection_binding=None,
                      projection_attempt_id="", generation_plan=None, on_submitted=None,
                      on_event=None):
        await self._reporter.emit_progress(
            "llm_round_started", round=self._state.llm_round_count,
            tool_call_count=self._state.tool_call_count, tool_count=len(request.tools),
        )
        iterator = self._base.astream(
            request, projection=projection, projection_binding=projection_binding,
            projection_attempt_id=projection_attempt_id, generation_plan=generation_plan,
            on_submitted=on_submitted, on_event=self._progress_sink(on_event),
        )
        try:
            while True:
                try:
                    update = await self._heartbeat.await_with_progress_heartbeat(
                        anext(iterator), phase="llm_round_waiting",
                        round=self._state.llm_round_count,
                        tool_call_count=self._state.tool_call_count,
                    )
                except StopAsyncIteration:
                    break
                yield update
        finally:
            await iterator.aclose()

    def _progress_sink(self, observer):
        def sink(event: dict[str, Any]) -> None:
            if observer is not None:
                observer(event)
            payload = dict(event)
            phase = str(payload.pop("phase", "") or "llm_endpoint_event")
            payload.setdefault("round", self._state.llm_round_count)
            payload.setdefault("tool_call_count", self._state.tool_call_count)
            asyncio.create_task(self._reporter.emit_progress(phase, **payload))
        return sink


class _BunshinOutputPort:
    def __init__(self, reporter: Reporter) -> None:
        self._reporter = reporter

    async def queue_reply(self, envelope: TurnDeliveryBinding, text: Any) -> str:
        _ = envelope
        rendered = str(getattr(text, "text", text) or "")
        await self._reporter.emit("progress", {"phase": "reply", "summary": _preview_text(rendered, limit=500)})
        return f"bunshin_reply_{uuid4().hex[:12]}"

    async def queue_stream_update(self, envelope: TurnDeliveryBinding, event: Any) -> str:
        _ = envelope
        _ = event
        return f"bunshin_stream_{uuid4().hex[:12]}"

    async def abort_stream(self, response_handle: ResponseHandle, *, reason: str = "interrupted") -> None:
        _ = response_handle
        _ = reason

    async def queue_status(self, envelope: TurnDeliveryBinding, kind: str, *, payload: dict[str, Any] | None = None) -> str:
        _ = envelope
        await self._reporter.emit("progress", {"phase": kind, "summary": _preview_text(json.dumps(payload or {}, ensure_ascii=False), limit=500)})
        return f"bunshin_status_{uuid4().hex[:12]}"

    async def queue_attachment(self, envelope: TurnDeliveryBinding, attachment: Any) -> str:
        _ = envelope
        _ = attachment
        return f"bunshin_attachment_{uuid4().hex[:12]}"
