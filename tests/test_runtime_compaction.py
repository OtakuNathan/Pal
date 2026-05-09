from __future__ import annotations

import asyncio
import unittest

from pal.channel import ChannelEnvelope, ChannelRuntime, EndpointConfig, ResponseHandle, register_with_core as register_channel_with_core
from pal.core import PalCore, register_with_core as register_core_with_core
from pal.execution import CapabilityResult
from pal.foundation import EventEnvelope
from pal.llm import CanonicalLLMOutcome, CanonicalToolCall, LLMPreflightAdvice
from pal.memory import L1TranscriptMessage, L3ProviderSelector, MemoryService, register_with_core as register_memory_with_core
from pal.shared import LLMFinishReason


class EchoTool:
    name = "echo"
    description = "Echo test arguments back as a stable result."
    args_schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    result_schema = {"type": "object", "properties": {"echo": {"type": "object"}}}

    def invoke(self, args: dict[str, object]) -> CapabilityResult:
        return CapabilityResult(
            status="ok",
            text="stable-result",
            llm_text="stable-result",
            structured={"echo": args},
        )


class _CompactingLLMRuntime:
    def __init__(self, *, compact_on: str) -> None:
        self.compact_on = compact_on
        self.preflight_count = 0
        self.generate_count = 0
        self.requests: list[tuple[str, object]] = []
        self.compaction_sources: list[str] = []

    def preflight(self, request) -> LLMPreflightAdvice:
        self.preflight_count += 1
        self.requests.append(("preflight", request))
        if self.compact_on == "preflight" and self.preflight_count == 2:
            return LLMPreflightAdvice(
                status="compact_required",
                active_model="stub-model",
                fallback_chain=[],
                target_input_budget=512,
                reserved_output_tokens=request.max_output_tokens,
            )
        return LLMPreflightAdvice(
            status="ready",
            active_model=request.model_hint or "stub-model",
            fallback_chain=[],
            target_input_budget=2048,
            reserved_output_tokens=request.max_output_tokens,
        )

    def generate(self, request) -> CanonicalLLMOutcome:
        self.generate_count += 1
        self.requests.append(("generate", request))
        if self.generate_count == 1:
            return CanonicalLLMOutcome(
                text="",
                tool_calls=[CanonicalToolCall(name="echo", args={"value": "during-compact"})],
                finish_reason="tool_calls",
            )
        if self.compact_on == "generate" and self.generate_count == 2:
            return CanonicalLLMOutcome(
                text="",
                tool_calls=[],
                finish_reason=LLMFinishReason.COMPACT_REQUIRED,
                target_input_budget=384,
                reserved_output_tokens=96,
                preferred_endpoint_id="fallback-after-tool",
                preferred_model_id="fallback-after-tool-model",
            )
        return CanonicalLLMOutcome(text="final answer", tool_calls=[], finish_reason="stop")

    async def acompact_memory_structured(
        self,
        text: str,
        *,
        max_output_tokens: int = 384,
        preferred_endpoint_id: str | None = None,
        preferred_model_id: str | None = None,
    ) -> dict[str, object]:
        _ = (max_output_tokens, preferred_endpoint_id, preferred_model_id)
        self.compaction_sources.append(text)
        return {
            "summary": {
                "summary": "compacted prior context",
                "search_text": text,
            },
            "entries": [],
        }


class _ManualCompactionLLMRuntime:
    def __init__(self) -> None:
        self.requests = []

    async def agenerate(self, request) -> CanonicalLLMOutcome:
        self.requests.append(request)
        return CanonicalLLMOutcome(text="manual compact summary", tool_calls=[], finish_reason="stop")


def _build_core_with_compacting_llm(*, compact_on: str):
    core = PalCore()
    register_core_with_core(core)
    channel_runtime = ChannelRuntime()
    register_channel_with_core(core.context, channel_runtime)
    memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
    memory_service.l1_store.append(
        [
            L1TranscriptMessage(role="user", content="prior user context before compaction"),
            L1TranscriptMessage(role="assistant", content="prior assistant context before compaction"),
        ]
    )
    register_memory_with_core(core.context, memory_service)
    scripted_llm = _CompactingLLMRuntime(compact_on=compact_on)
    core.context.port_registry["llm:llm"] = scripted_llm
    core.context.execution_runtime.register_tool(EchoTool())
    return core, memory_service, scripted_llm


def _run_turn(core: PalCore):
    return core.process_channel_turn(
        ChannelEnvelope(
            event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "use a tool then continue"}),
            endpoint=EndpointConfig(endpoint_id="memory", channel_kind="memory", binding_key="memory"),
            response_handle=ResponseHandle(endpoint_id="memory"),
        )
    )


def _tool_contents(request) -> list[str]:
    return [
        str(message.get("content") or "")
        for message in list(request.messages)
        if str(message.get("role") or "") == "tool"
    ]


class RuntimeCompactionTests(unittest.TestCase):
    def test_manual_compaction_summary_uses_current_llm_contract_imports(self) -> None:
        core = PalCore()
        scripted_llm = _ManualCompactionLLMRuntime()
        core.context.port_registry["llm:llm"] = scripted_llm

        summary = asyncio.run(core._generate_compaction_summary_async("user context to compact"))

        self.assertEqual(summary, "manual compact summary")
        self.assertEqual(len(scripted_llm.requests), 1)
        self.assertEqual(scripted_llm.requests[0].metadata["purpose"], "manual_compaction")

    def test_preflight_compact_during_tool_turn_preserves_current_tool_result(self) -> None:
        core, memory_service, scripted_llm = _build_core_with_compacting_llm(compact_on="preflight")

        outcome = _run_turn(core)

        self.assertEqual(outcome.final_reply, "final answer")
        self.assertTrue(scripted_llm.compaction_sources)
        self.assertIn("prior user context before compaction", scripted_llm.compaction_sources[0])
        self.assertEqual(memory_service.l2_store.get_entry("memory_summary_current").summary, "compacted prior context")
        preflight_requests = [request for kind, request in scripted_llm.requests if kind == "preflight"]
        generate_requests = [request for kind, request in scripted_llm.requests if kind == "generate"]
        self.assertIn("stable-result", "\n".join(_tool_contents(preflight_requests[1])))
        self.assertIn("stable-result", "\n".join(_tool_contents(generate_requests[-1])))

    def test_generate_compact_during_tool_turn_preserves_tool_result_and_endpoint_hint(self) -> None:
        core, memory_service, scripted_llm = _build_core_with_compacting_llm(compact_on="generate")

        outcome = _run_turn(core)

        self.assertEqual(outcome.final_reply, "final answer")
        self.assertTrue(scripted_llm.compaction_sources)
        self.assertEqual(memory_service.l2_store.get_entry("memory_summary_current").summary, "compacted prior context")
        generate_requests = [request for kind, request in scripted_llm.requests if kind == "generate"]
        preflight_requests = [request for kind, request in scripted_llm.requests if kind == "preflight"]
        self.assertIn("stable-result", "\n".join(_tool_contents(generate_requests[1])))
        self.assertIn("stable-result", "\n".join(_tool_contents(generate_requests[-1])))
        self.assertEqual(preflight_requests[-1].metadata.get("preferred_endpoint_id"), "fallback-after-tool")
        self.assertEqual(preflight_requests[-1].model_hint, "fallback-after-tool-model")


if __name__ == "__main__":
    unittest.main()
