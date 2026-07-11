from __future__ import annotations

import asyncio
import unittest

from pal.channel import ChannelEnvelope, ChannelRuntime, EndpointConfig, ResponseHandle, register_with_core as register_channel_with_core
from pal.control import ControlAction, ControlRoute
from pal.core import MemoryCompactEffect, PalCore, TurnContinuation, register_with_core as register_core_with_core
from pal.execution import CapabilityDescriptor, CapabilityResult
from pal.foundation import EventEnvelope
from pal.llm import CanonicalLLMOutcome, CanonicalToolCall, LLMPreflightAdvice
from pal.llm.runtime import LLMRuntime
from pal.memory import CompactionProfile, L1MessageKind, L1TranscriptMessage, L2Entry, L3ProviderSelector, MemoryCompactRequest, MemoryCompactResult, MemoryPackRequest, MemoryService, register_with_core as register_memory_with_core
from pal.minion.compact import compact_minion_memory_service
from pal.shared import LLMFinishReason, PromptAssemblyContext, RuntimeStatus


class EchoTool:
    name = "op_test_echo"
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
    def __init__(self, *, compact_on: str, memory_candidates: list[dict[str, object]] | None = None) -> None:
        self.compact_on = compact_on
        self.preflight_count = 0
        self.generate_count = 0
        self.requests: list[tuple[str, object]] = []
        self.compaction_sources: list[str] = []
        self.compaction_profiles: list[CompactionProfile] = []
        self.structured_compaction_max_output_tokens: list[int] = []
        self.memory_candidates = list(memory_candidates or [])

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
        profile: CompactionProfile = CompactionProfile.PAL,
    ) -> dict[str, object]:
        _ = (max_output_tokens, preferred_endpoint_id, preferred_model_id)
        self.compaction_sources.append(text)
        self.compaction_profiles.append(profile)
        self.structured_compaction_max_output_tokens.append(max_output_tokens)
        schema = "pal.compaction.minion.v1" if profile == CompactionProfile.MINION else "pal.compaction.pal.v2"
        payload = {
            "schema": schema,
            "kind": profile.value,
            "continuity": {},
            "summary": {
                "summary": "compacted prior context",
                "search_text": text,
            },
        }
        if profile != CompactionProfile.MINION:
            payload["memory_candidates"] = self.memory_candidates
        return payload


class _ManualCompactionLLMRuntime:
    def __init__(self, *, memory_candidates: list[dict[str, object]] | None = None) -> None:
        self.compaction_sources: list[str] = []
        self.compaction_profiles: list[CompactionProfile] = []
        self.structured_compaction_max_output_tokens: list[int] = []
        self.memory_candidates = list(memory_candidates or [])

    async def acompact_memory_structured(
        self,
        text: str,
        *,
        max_output_tokens: int = 384,
        preferred_endpoint_id: str | None = None,
        preferred_model_id: str | None = None,
        profile: CompactionProfile = CompactionProfile.PAL,
    ) -> dict[str, object]:
        _ = (preferred_endpoint_id, preferred_model_id)
        self.compaction_sources.append(text)
        self.compaction_profiles.append(profile)
        self.structured_compaction_max_output_tokens.append(max_output_tokens)
        schema = "pal.compaction.minion.v1" if profile == CompactionProfile.MINION else "pal.compaction.pal.v2"
        payload = {
            "schema": schema,
            "kind": profile.value,
            "continuity": {},
            "summary": {
                "summary": "manual structured compact summary",
                "search_text": text,
            },
        }
        if profile != CompactionProfile.MINION:
            payload["memory_candidates"] = self.memory_candidates
        return payload


class _FailingCompactionLLMRuntime:
    def __init__(self) -> None:
        self.structured_calls = 0
        self.summary_calls = 0

    async def acompact_memory_structured(self, *args, **kwargs) -> dict[str, object]:
        _ = (args, kwargs)
        self.structured_calls += 1
        return {}

    async def asummarize_compaction(self, *args, **kwargs) -> str:
        _ = (args, kwargs)
        self.summary_calls += 1
        return ""


def _build_core_with_compacting_llm(*, compact_on: str, memory_candidates: list[dict[str, object]] | None = None):
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
    scripted_llm = _CompactingLLMRuntime(compact_on=compact_on, memory_candidates=memory_candidates)
    core.context.port_registry["llm:llm"] = scripted_llm
    echo_tool = EchoTool()
    core.context.execution_runtime.register_tool(echo_tool)
    core.context.execution_runtime.register_capability(
        CapabilityDescriptor(
            name="echo",
            canonical_path=echo_tool.name,
            family="test",
            description=echo_tool.description,
            source="test",
        ),
        lambda _call: CapabilityResult(
            status="error",
            text="resident tool binding missing",
            llm_text="resident tool binding missing",
        ),
    )
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


def _message_text(message) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return str(content or "")


def _request_text(request) -> str:
    return "\n".join(_message_text(message) for message in list(request.messages))


def _idle_program():
    if False:
        yield None


class RuntimeCompactionTests(unittest.TestCase):
    def test_structured_compaction_prompt_guides_summary_and_l2_entries(self) -> None:
        pal_prompt = LLMRuntime._COMPACT_PAL_STRUCTURED_SYSTEM

        self.assertIn("pal.compaction.pal.v2", pal_prompt)
        self.assertIn("memory_candidates", pal_prompt)
        self.assertIn("active_operating_instructions", pal_prompt)
        self.assertIn("active_requests", pal_prompt)
        self.assertIn("temporary_task_state", pal_prompt)
        self.assertIn("Field boundaries", pal_prompt)
        self.assertIn("HOW Pal should work", pal_prompt)
        self.assertIn("WHAT Pal should do", pal_prompt)
        self.assertIn("ephemeral progress needed to resume", pal_prompt)
        self.assertIn("Previous compact data is already lossy", pal_prompt)
        self.assertIn("Automatic and manual compact candidates both require approval", pal_prompt)
        self.assertIn("durable user preferences", pal_prompt)
        self.assertIn("stable user status/context", pal_prompt)
        self.assertIn("real goals/plans/commitments", pal_prompt)
        self.assertIn("Do not create entries from jokes", pal_prompt)
        self.assertIn("repair lessons", pal_prompt)
        self.assertFalse(hasattr(LLMRuntime, "_COMPACT_MINION_STRUCTURED_SYSTEM"))

    def test_llm_runtime_rejects_minion_structured_compaction(self) -> None:
        runtime = object.__new__(LLMRuntime)
        requests = []

        def generate(request):
            requests.append(request)
            return CanonicalLLMOutcome(
                text=(
                    '{"schema":"pal.compaction.pal.v2","kind":"pal","continuity":{},'
                    '"summary":{"summary":"ok","search_text":"ok"},'
                    '"memory_candidates":[{"kind":"fact","title":"bad","summary":"bad","source_excerpt":"bad"}]}'
                ),
                tool_calls=[],
                finish_reason=LLMFinishReason.STOP,
            )

        runtime.generate = generate

        payload = runtime.compact_memory_structured("compact this", profile=CompactionProfile.MINION)

        self.assertEqual(payload, {})
        self.assertEqual(requests, [])

    def test_llm_runtime_uses_zero_temperature_for_compaction(self) -> None:
        runtime = object.__new__(LLMRuntime)
        requests = []

        def generate(request):
            requests.append(request)
            if request.metadata.get("purpose") == "memory_compaction":
                return CanonicalLLMOutcome(text="compact summary", tool_calls=[], finish_reason=LLMFinishReason.STOP)
            return CanonicalLLMOutcome(
                text='{"schema":"pal.compaction.pal.v2","kind":"pal","continuity":{},'
                '"summary":{"summary":"ok","search_text":"ok"},"memory_candidates":[]}',
                tool_calls=[],
                finish_reason=LLMFinishReason.STOP,
            )

        runtime.generate = generate

        self.assertEqual(runtime.summarize_compaction("compact this"), "compact summary")
        payload = runtime.compact_memory_structured("compact this")

        self.assertEqual(payload["schema"], "pal.compaction.pal.v2")
        self.assertEqual([request.temperature for request in requests], [0.0, 0.0])

    def test_v2_pal_compaction_renders_xml_wrapped_markdown(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        memory_service = MemoryService()
        register_memory_with_core(core.context, memory_service)

        result = memory_service.compact(
            MemoryCompactRequest(
                target_input_budget=512,
                reserved_output_tokens=128,
                metadata={
                    "structured_compaction": {
                        "schema": "pal.compaction.pal.v2",
                        "kind": "pal",
                        "continuity": {
                            "current_focus": "Compact should track the user and recent delegated tasks.",
                            "primary_request_and_intent": "Discuss compact v2 before implementation.",
                            "active_operating_instructions": ["Do not directly mutate code while planning."],
                            "active_requests": ["Inspect compact prompt."],
                            "temporary_task_state": ["The team is still planning the Pal compact v2 shape."],
                            "optional_next_step": "Continue from the compact design discussion.",
                        },
                        "summary": {
                            "summary": "The user is refining compact v2 semantics.",
                            "search_text": "compact v2 track user recent delegated tasks",
                        },
                        "memory_candidates": [
                            {
                                "kind": "fact",
                                "title": "Compact memory candidates are not auto-committed",
                                "summary": "Compact can propose candidates, but Pal decides whether to commit.",
                                "source_excerpt": "compact only responsible for candidates",
                            }
                        ],
                    }
                },
            )
        )

        self.assertEqual(len(result.projected_entries), 1)
        self.assertEqual(result.projected_entries[0].payload["schema"], "pal.compaction.pal.v2")
        request = core.prompt_compiler.build_canonical_prompt(
            PromptAssemblyContext(
                event=EventEnvelope(
                    event_kind="user.message",
                    source_kind="channel",
                    payload={"text": "continue"},
                )
            ),
            max_output_tokens=128,
        )
        prompt_text = _request_text(request)
        self.assertIn('<compact_context kind="pal" authority="conversation_continuity">', prompt_text)
        self.assertEqual(prompt_text.count('<compact_context kind="pal" authority="conversation_continuity">'), 1)
        self.assertIn("### Primary Request And Intent", prompt_text)
        self.assertIn("Discuss compact v2 before implementation.", prompt_text)
        self.assertIn("### Active Requests", prompt_text)
        self.assertIn("### Temporary Task State", prompt_text)
        self.assertNotIn('"schema": "pal.compaction.pal.v2"', prompt_text)

    def test_minion_compaction_helper_renders_reference_only_context(self) -> None:
        memory_service = MemoryService()

        compact_minion_memory_service(
            memory_service,
            MemoryCompactRequest(
                target_input_budget=512,
                reserved_output_tokens=128,
                profile=CompactionProfile.MINION,
                metadata={
                    "structured_compaction": {
                        "schema": "pal.compaction.minion.v2",
                        "kind": "minion",
                        "continuity": {
                            "prior_completed_user_inputs": ["Create a changelog package."],
                            "history_rule": "Prior input is background only.",
                            "current_turn_rule": "Current invocation and canonical artifacts are authoritative.",
                        },
                        "summary": {
                            "summary": "Minion was midway through a changelog task.",
                            "search_text": "minion changelog package",
                        },
                    }
                },
            )
        )

        summary = memory_service.build_pack(MemoryPackRequest()).current_summary
        self.assertIsNotNone(summary)
        assert summary is not None
        prompt_text = summary.rendered
        self.assertIn('<compact_context kind="minion" authority="reference_only">', prompt_text)
        self.assertIn("continuity reference only", prompt_text)
        self.assertIn("Verify against the canonical workflow artifacts, current aggregate, worker journal, and workspace before acting.", prompt_text)
        self.assertIn("### Prior Completed User Inputs", prompt_text)
        self.assertIn("- Create a changelog package.", prompt_text)

    def test_compaction_rejects_invalid_payload_without_mutating_memory(self) -> None:
        service = MemoryService()
        original_l1 = [L1TranscriptMessage(role="user", content="keep this L1 context")]
        service.l1_store.append(original_l1)
        service.l2_store.upsert_entries(
            [
                L2Entry(
                    entry_id="existing_fact",
                    kind="fact",
                    scope="system",
                    title="Existing",
                    summary="existing l2 fact",
                )
            ],
            touch=True,
        )
        before_l1 = list(service.l1_store.items)
        before_l2 = dict(service.l2_store.items)

        with self.assertRaises(ValueError):
            service.compact(
                MemoryCompactRequest(
                    target_input_budget=512,
                    reserved_output_tokens=128,
                    metadata={"structured_compaction": {"summary": {"summary": "missing schema"}}},
                )
            )

        self.assertEqual(service.l1_store.items, before_l1)
        self.assertEqual(service.l2_store.items, before_l2)

    def test_compaction_stores_summary_in_l1_and_cleans_legacy_l2_summary(self) -> None:
        service = MemoryService()
        service.l1_store.append([L1TranscriptMessage(role="user", content="original l1 context")])
        service.l2_store.items["existing_fact"] = L2Entry(
            entry_id="existing_fact",
            kind="fact",
            scope="system",
            title="Existing",
            summary="existing l2 fact",
        )
        service.l2_store.items["memory_summary_current"] = L2Entry(
            entry_id="memory_summary_current",
            kind="summary",
            scope="system",
            title="Legacy",
            summary="legacy l2 summary should be removed",
        )

        result = service.compact(
            MemoryCompactRequest(
                target_input_budget=512,
                reserved_output_tokens=128,
                metadata={
                    "structured_compaction": {
                        "schema": "pal.compaction.pal.v2",
                        "kind": "pal",
                        "continuity": {},
                        "summary": {
                            "summary": "new compact summary lives in l1",
                            "search_text": "new compact summary lives in l1",
                        },
                    }
                },
            )
        )

        self.assertEqual(result.metadata["projected_entry_count"], 0)
        self.assertEqual(result.metadata["compact_summary_count"], 1)
        self.assertEqual(service.l2_store.get_entry("existing_fact").summary, "existing l2 fact")
        self.assertIsNone(service.l2_store.get_entry("memory_summary_current"))
        self.assertEqual(len(service.l1_store.items), 1)
        self.assertIn('<compact_context kind="pal" authority="conversation_continuity">', service.l1_store.items[0][0].content)
        self.assertIn("new compact summary lives in l1", service.l1_store.items[0][0].content)

    def test_compaction_effect_retries_and_fails_without_mutating_memory(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        memory_service = MemoryService()
        memory_service.l1_store.append([L1TranscriptMessage(role="user", content="prior L1 survives failed compact")])
        register_memory_with_core(core.context, memory_service)
        failing_llm = _FailingCompactionLLMRuntime()
        core.context.port_registry["llm:llm"] = failing_llm
        before_l1 = list(memory_service.l1_store.items)

        continuation = TurnContinuation(
            turn_id="compact-failure-test",
            channel_envelope=ChannelEnvelope(
                event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "continue"}),
                endpoint=EndpointConfig(endpoint_id="memory", channel_kind="memory", binding_key="memory"),
                response_handle=ResponseHandle(endpoint_id="memory"),
            ),
            program=_idle_program(),
            correlation_id="compact-failure-test",
        )

        result = asyncio.run(
            core.turn_executor.execute_turn_effect_async(
                continuation,
                MemoryCompactEffect(
                    assembly_context=PromptAssemblyContext(),
                    target_input_budget=512,
                    reserved_output_tokens=128,
                ),
            )
        )

        self.assertEqual(result.status, RuntimeStatus.ERROR)
        self.assertEqual(failing_llm.structured_calls, 3)
        self.assertEqual(failing_llm.summary_calls, 3)
        self.assertEqual(memory_service.l1_store.items, before_l1)
        self.assertIsNone(memory_service.l2_store.get_entry("memory_summary_current"))

    def test_minion_compaction_helper_strips_memory_candidates(self) -> None:
        memory_service = MemoryService()

        result = compact_minion_memory_service(
            memory_service,
            MemoryCompactRequest(
                target_input_budget=512,
                reserved_output_tokens=128,
                profile=CompactionProfile.MINION,
                metadata={
                    "structured_compaction": {
                        "schema": "pal.compaction.minion.v2",
                        "kind": "minion",
                        "continuity": {
                            "prior_completed_user_inputs": ["Continue the task safely."],
                        },
                        "summary": {
                            "summary": "A minion compact payload was produced mechanically.",
                            "search_text": "minion compact mechanical payload",
                        },
                        "memory_candidates": [
                            {
                                "kind": "fact",
                                "title": "should be stripped",
                                "summary": "minion compact must not create candidates",
                                "source_excerpt": "bad candidate",
                            }
                        ],
                    },
                },
            )
        )

        summary = result.projected_entries[0]
        self.assertEqual(summary.payload["schema"], "pal.compaction.minion.v2")
        self.assertEqual(summary.payload["kind"], "minion")
        self.assertNotIn("memory_candidates", summary.payload)
        self.assertIn('<compact_context kind="minion" authority="reference_only">', summary.rendered)
        self.assertIn("continuity reference only", summary.rendered)
        self.assertNotIn("should be stripped", summary.rendered)

    def test_minion_compaction_effect_does_not_call_llm(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        memory_service = MemoryService()
        memory_service.l1_store.append([L1TranscriptMessage(role="user", content="minion prior state")])
        memory_service.build_compaction_payload = lambda **kwargs: {
            "schema": "pal.compaction.minion.v2",
            "kind": "minion",
            "continuity": {"prior_completed_user_inputs": ["minion prior state"]},
            "summary": {
                "summary": "Mechanical minion compact summary.",
                "search_text": "minion prior state",
            },
        }
        memory_service.compact = lambda request: compact_minion_memory_service(memory_service, request)
        register_memory_with_core(core.context, memory_service)
        scripted_llm = _ManualCompactionLLMRuntime()
        core.context.port_registry["llm:llm"] = scripted_llm
        continuation = TurnContinuation(
            turn_id="minion-compact-test",
            channel_envelope=ChannelEnvelope(
                event=EventEnvelope(
                    event_kind="user.message",
                    source_kind="minion",
                    payload={"text": "continue task"},
                ),
                endpoint=EndpointConfig(endpoint_id="minion:test", channel_kind="stdio", binding_key="test"),
                response_handle=ResponseHandle(endpoint_id="minion:test"),
            ),
            program=_idle_program(),
            correlation_id="minion-compact-test",
        )

        asyncio.run(
            core.turn_executor.execute_turn_effect_async(
                continuation,
                MemoryCompactEffect(
                    assembly_context=PromptAssemblyContext(core_mode="minion", turn_kind="minion"),
                    target_input_budget=512,
                    reserved_output_tokens=128,
                ),
            )
        )

        self.assertEqual(scripted_llm.compaction_profiles, [])
        compacted = memory_service.l1_store.items[0][0].content
        self.assertIn('<compact_context kind="minion" authority="reference_only">', compacted)
        self.assertIn("canonical workflow artifacts", compacted)

    def test_manual_compaction_uses_structured_compaction_path(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        memory_service.l1_store.append(
            [
                L1TranscriptMessage(role="user", content="manual compact user context"),
                L1TranscriptMessage(role="assistant", content="manual compact assistant context"),
            ]
        )
        register_memory_with_core(core.context, memory_service)
        scripted_llm = _ManualCompactionLLMRuntime()
        core.context.port_registry["llm:llm"] = scripted_llm
        replies: list[str] = []

        async def capture_reply(route, text: str) -> None:
            _ = route
            replies.append(text)

        core._reply_to_route_async = capture_reply

        asyncio.run(
            core._handle_compact_memory_async(
                ControlAction(
                    action_kind="compact_memory",
                    target_scope="memory",
                    route=ControlRoute(endpoint_id="memory", channel_kind="memory"),
                )
            )
        )

        summary = memory_service.build_pack(MemoryPackRequest()).current_summary
        self.assertIsNotNone(summary)
        self.assertIn("manual structured compact summary", summary.rendered)
        self.assertIsNone(memory_service.l2_store.get_entry("memory_summary_current"))
        self.assertIn("manual compact user context", scripted_llm.compaction_sources[0])
        self.assertEqual(scripted_llm.structured_compaction_max_output_tokens, [4096])
        self.assertEqual(len(replies), 1)
        self.assertIn("Context compacted.", replies[-1])

    def test_manual_compaction_opens_memory_candidate_approval(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        memory_service.l1_store.append(
            [
                L1TranscriptMessage(role="user", content="manual compact user context"),
                L1TranscriptMessage(role="assistant", content="manual compact assistant context"),
            ]
        )
        register_memory_with_core(core.context, memory_service)
        candidate = {
            "kind": "fact",
            "title": "Compact candidates need approval",
            "summary": "Compact proposes durable memory candidates but does not auto-commit them.",
            "source_excerpt": "candidate came from compact",
        }
        core.context.port_registry["llm:llm"] = _ManualCompactionLLMRuntime(memory_candidates=[candidate])
        replies: list[str] = []
        statuses: list[tuple[str, dict[str, object]]] = []

        async def capture_reply(route, text: str) -> None:
            _ = route
            replies.append(text)

        async def capture_status(route, kind: str, payload: dict[str, object]) -> None:
            _ = route
            statuses.append((kind, payload))

        core._reply_to_route_async = capture_reply
        core._status_to_route_async = capture_status

        asyncio.run(
            core._handle_compact_memory_async(
                ControlAction(
                    action_kind="compact_memory",
                    target_scope="memory",
                    route=ControlRoute(endpoint_id="memory", channel_kind="memory"),
                )
            )
        )

        self.assertEqual(len(replies), 1)
        self.assertIn("Context compacted.", replies[-1])
        self.assertEqual(statuses[-1][0], "interactive_open")
        spec = statuses[-1][1]["spec"]
        self.assertEqual(spec.interaction_kind, "memory_candidate_approval")
        self.assertIn("Compact candidates need approval", spec.text)
        accept = spec.buttons[0][0]
        self.assertEqual(accept.action_args["action_kind"], "memory_candidate_decision")
        self.assertEqual(accept.action_args["args"]["memory_candidates"], [candidate])

    def test_memory_candidate_accept_commits_to_l3(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        register_memory_with_core(core.context, MemoryService())
        calls = []

        def record_memory_write(call):
            calls.append(call)
            return CapabilityResult(status="ok", text="ok", llm_text="ok")

        core.context.execution_runtime.register_capability(
            CapabilityDescriptor(
                name="op_memory_write",
                family="memory",
                description="record memory write",
                source="test",
            ),
            record_memory_write,
        )
        replies: list[str] = []

        async def capture_reply(route, text: str) -> None:
            _ = route
            replies.append(text)

        core._reply_to_route_async = capture_reply

        asyncio.run(
            core.handle_control_action_async(
                ControlAction(
                    action_kind="memory_candidate_decision",
                    target_scope="memory",
                    target_id="compact_test",
                    route=ControlRoute(endpoint_id="memory", channel_kind="memory"),
                    args={
                        "decision": "accept",
                        "source_kind": "pal_compact",
                        "source_ref": "compact_test",
                        "memory_candidates": [
                            {
                                "kind": "fact",
                                "title": "Source excerpt fallback",
                                "summary": "",
                                "source_excerpt": "Use source excerpts when compact candidates lack search_text.",
                                "topics": ["compact"],
                                "payload": {"existing": "value"},
                            }
                        ],
                    },
                )
            )
        )

        self.assertIn("1 committed", replies[-1])
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call.name, "op_memory_write")
        self.assertEqual(call.args["scope"], "system")
        self.assertEqual(call.args["search_text"], "Use source excerpts when compact candidates lack search_text.")
        self.assertEqual(call.args["payload"]["existing"], "value")
        self.assertEqual(call.args["payload"]["source_kind"], "pal_compact")
        self.assertEqual(call.args["payload"]["source_ref"], "compact_test")

    def test_compaction_source_uses_v2_seed_and_turn_windows(self) -> None:
        service = MemoryService()
        service.l1_store.items = [[
            L1TranscriptMessage(
                role="assistant",
                content="<compact_context kind=\"pal\" authority=\"conversation_continuity\">\nstable summary should be retained\n</compact_context>",
                kind=L1MessageKind.RUNTIME_CONTEXT_SUMMARY,
                payload={
                    "schema": "pal.compaction.pal.v2",
                    "kind": "pal",
                    "continuity": {
                        "active_operating_instructions": ["Do not mutate L3 while compact is being designed."],
                        "active_requests": ["Implement Pal compact v2."],
                        "temporary_task_state": ["hot/raw=5 and warm=20 are the first-pass windows."],
                        "retired_or_superseded_context": ["Old v1 summary text should not be recursively summarized."],
                    },
                    "summary": {
                        "summary": "stable summary should not be re-fed as transcript",
                        "search_text": "compact v2 seed",
                    },
                    "memory_candidates": [],
                },
            )
        ]]
        service.l2_store.items["memory_summary_current"] = L2Entry(
            entry_id="memory_summary_current",
            kind="summary",
            scope="system",
            title="Legacy",
            summary="legacy l2 summary should not be used",
        )
        for index in range(20):
            service.l1_store.append([L1TranscriptMessage(role="user", content=f"recent item {index}")])
        service.l1_store.append(
            [
                L1TranscriptMessage(role="assistant", content="", tool_calls=[{"id": "call_1"}]),
                L1TranscriptMessage(role="tool", content="tool result should not be compacted", tool_call_id="call_1"),
                L1TranscriptMessage(
                    role="user",
                    content="<runtime_context_update kind=\"memory\">\nnot a real request\n</runtime_context_update>\n<recalled_memories>\n[mem_1]: runtime memory\n</recalled_memories>",
                    kind="runtime_context_memory",
                ),
                L1TranscriptMessage(
                    role="assistant",
                    content="turn was interrupted before final reply",
                    kind=L1MessageKind.TURN_INTERRUPTED,
                ),
                L1TranscriptMessage(role="assistant", content="assistant reply should be compacted"),
            ]
        )

        source = service.build_compaction_source_text(target_input_budget=4096)

        self.assertIn('<compact_source kind="pal" schema_target="pal.compaction.pal.v2">', source)
        self.assertIn("## Previous Compact Seed", source)
        self.assertIn("Do not mutate L3 while compact is being designed.", source)
        self.assertIn("hot/raw=5 and warm=20", source)
        self.assertNotIn("<compact_context", source)
        self.assertNotIn("stable summary should be retained", source)
        self.assertIn("turn was interrupted before final reply", source)
        self.assertIn("assistant reply should be compacted", source)
        self.assertNotIn("tool result should not be compacted", source)
        self.assertNotIn("runtime memory", source)
        self.assertNotIn("not a real request", source)
        self.assertIn("recent item 0", source)

    def test_legacy_tool_trace_dict_is_dropped_before_prompt_or_compaction_source(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        memory_service = MemoryService()
        memory_service.l1_store.append(
            [
                {"role": "user", "content": "keep this ordinary user context"},
                {
                    "role": "assistant",
                    "content": (
                        "ordinary assistant reply\n\n"
                        "<system-reminder>Tools used: inline private artifact path "
                        "/home/nathan/.pal/data/minion/inline-secret</system-reminder>"
                    ),
                    "kind": L1MessageKind.ASSISTANT_REPLY,
                    "tool_trace": "call_tool(ok): private artifact path /home/nathan/.pal/data/minion/secret",
                },
            ]
        )
        register_memory_with_core(core.context, memory_service)

        request = core.prompt_compiler.build_canonical_prompt(
            PromptAssemblyContext(
                event=EventEnvelope(
                    event_kind="user.message",
                    source_kind="channel",
                    payload={"text": "continue"},
                )
            ),
            max_output_tokens=128,
        )
        prompt_text = _request_text(request)
        source = memory_service.build_compaction_source_text(target_input_budget=4096)

        self.assertFalse(hasattr(memory_service.l1_store.items[0][1], "tool_trace"))
        self.assertIn("ordinary assistant reply", prompt_text)
        self.assertIn("ordinary assistant reply", source)
        self.assertNotIn("Tools used:", prompt_text)
        self.assertNotIn("private artifact path", prompt_text)
        self.assertNotIn("inline private artifact path", prompt_text)
        self.assertNotIn("tool_trace:", source)
        self.assertNotIn("private artifact path", source)
        self.assertNotIn("inline private artifact path", source)

    def test_compaction_source_drops_old_warm_turns_by_turn(self) -> None:
        service = MemoryService()
        for index in range(30):
            service.l1_store.append([L1TranscriptMessage(role="user", content=f"turn item {index}")])

        source = service.build_compaction_source_text(target_input_budget=4096)

        self.assertNotIn("turn item 0", source)
        self.assertNotIn("turn item 4", source)
        self.assertIn("turn item 5", source)
        self.assertIn("turn item 29", source)

    def test_second_compaction_source_does_not_recompact_rendered_context(self) -> None:
        service = MemoryService()
        service.l1_store.append([L1TranscriptMessage(role="user", content="first real turn")])
        service.compact(
            MemoryCompactRequest(
                target_input_budget=512,
                reserved_output_tokens=128,
                metadata={
                    "structured_compaction": {
                        "schema": "pal.compaction.pal.v2",
                        "kind": "pal",
                        "continuity": {
                            "active_operating_instructions": ["Keep L3 out of this compact implementation."],
                            "active_requests": ["Implement Pal compact v2."],
                            "retired_or_superseded_context": ["A completed old task must stay retired."],
                        },
                        "summary": {
                            "summary": "First compact rendered context should not be re-fed as transcript.",
                            "search_text": "first compact seed",
                        },
                        "memory_candidates": [],
                    }
                },
            )
        )
        service.l1_store.append([L1TranscriptMessage(role="user", content="new turn after compact")])

        source = service.build_compaction_source_text(target_input_budget=4096)

        self.assertNotIn("<compact_context", source)
        self.assertNotIn("First compact rendered context should not be re-fed as transcript.", source)
        self.assertIn("Keep L3 out of this compact implementation.", source)
        self.assertIn("new turn after compact", source)

    def test_prompt_merges_previous_user_context_with_next_user_message(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        memory_service = MemoryService()
        register_memory_with_core(core.context, memory_service)
        memory_service.l1_store.append(
            [L1TranscriptMessage(role="user", content="interrupt me after checking compact")]
        )
        next_event = EventEnvelope(
            event_kind="user.message",
            source_kind="channel",
            payload={"text": "continue"},
            event_id="turn-after-interrupt",
        )
        request = core.prompt_compiler.build_canonical_prompt(
            PromptAssemblyContext(event=next_event),
            max_output_tokens=128,
        )
        prompt_text = _request_text(request)

        self.assertIn("interrupt me after checking compact", prompt_text)
        self.assertIn("continue", prompt_text)
        self.assertNotIn("<turn_checkpoint", prompt_text)
        self.assertLess(prompt_text.rindex("continue"), prompt_text.rindex("<runtime_reminder"))

    def test_compact_after_interrupt_ignores_unsettled_turn_until_next_user(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        memory_service = MemoryService()
        register_memory_with_core(core.context, memory_service)
        envelope = ChannelEnvelope(
            event=EventEnvelope(
                event_kind="user.message",
                source_kind="channel",
                payload={"text": "interrupt before compact"},
                event_id="turn-interrupt-compact",
            ),
            endpoint=EndpointConfig(endpoint_id="memory", channel_kind="memory", binding_key="memory"),
            response_handle=ResponseHandle(endpoint_id="memory"),
        )
        continuation = TurnContinuation(
            turn_id="turn-interrupt-compact",
            channel_envelope=envelope,
            program=_idle_program(),
            correlation_id="turn-interrupt-compact",
            control_scope_key="memory",
        )
        scope_state = core._ensure_scope_state("memory")
        continuation.interrupted = True
        core.turn_manager._queue_interrupted_turn_settlement(scope_state, continuation)
        self.assertEqual(len(scope_state.interrupted_turns_to_settle), 1)

        source_before_compact = memory_service.build_compaction_source_text(target_input_budget=4096)
        self.assertNotIn("interrupt before compact", source_before_compact)
        self.assertNotIn("<turn_checkpoint", source_before_compact)

        memory_service.compact(
            MemoryCompactRequest(
                target_input_budget=256,
                reserved_output_tokens=128,
                metadata={
                    "structured_compaction": {
                        "schema": "pal.compaction.pal.v2",
                        "kind": "pal",
                        "continuity": {},
                        "summary": {
                            "summary": "No interrupted turn checkpoint was committed.",
                            "search_text": source_before_compact,
                        },
                    }
                },
            )
        )
        next_event = EventEnvelope(
            event_kind="user.message",
            source_kind="channel",
            payload={"text": "continue after compact"},
            event_id="turn-after-compact",
        )
        next_envelope = ChannelEnvelope(
            event=next_event,
            endpoint=EndpointConfig(endpoint_id="memory", channel_kind="memory", binding_key="memory"),
            response_handle=ResponseHandle(endpoint_id="memory"),
        )
        asyncio.run(core._settle_interrupted_turns_for_next_user_async(next_envelope, scope_state))
        self.assertEqual(len(scope_state.interrupted_turns_to_settle), 0)
        self.assertEqual(len(memory_service.l1_store.items), 2)
        request = core.prompt_compiler.build_canonical_prompt(
            PromptAssemblyContext(event=next_event),
            max_output_tokens=128,
        )
        prompt_text = _request_text(request)

        self.assertIn('<compact_context kind="pal" authority="conversation_continuity">', prompt_text)
        self.assertIn("interrupt before compact", prompt_text)
        self.assertIn("continue after compact", prompt_text)
        self.assertNotIn("<turn_checkpoint", prompt_text)
        self.assertLess(prompt_text.rindex("continue after compact"), prompt_text.rindex("<runtime_reminder"))

    def test_preflight_compact_during_tool_turn_preserves_current_tool_result(self) -> None:
        core, memory_service, scripted_llm = _build_core_with_compacting_llm(compact_on="preflight")

        outcome = _run_turn(core)

        self.assertEqual(outcome.final_reply, "final answer")
        self.assertTrue(scripted_llm.compaction_sources)
        self.assertGreaterEqual(scripted_llm.structured_compaction_max_output_tokens[0], 768)
        self.assertIn("prior user context before compaction", scripted_llm.compaction_sources[0])
        self.assertIsNone(memory_service.l2_store.get_entry("memory_summary_current"))
        self.assertIn("compacted prior context", memory_service.l1_store.items[0][0].content)
        preflight_requests = [request for kind, request in scripted_llm.requests if kind == "preflight"]
        generate_requests = [request for kind, request in scripted_llm.requests if kind == "generate"]
        self.assertIn("stable-result", "\n".join(_tool_contents(preflight_requests[1])))
        self.assertIn("stable-result", "\n".join(_tool_contents(generate_requests[-1])))
        post_compact_prompt = _request_text(generate_requests[-1])
        self.assertIn('<compact_context kind="pal" authority="conversation_continuity">', post_compact_prompt)
        self.assertIn("### Summary\ncompacted prior context", post_compact_prompt)
        self.assertNotIn("Compaction Note:\ncompacted prior context", post_compact_prompt)
        self.assertEqual(generate_requests[-1].messages[1]["role"], "user")
        user_context = _message_text(generate_requests[-1].messages[1])
        self.assertIn("<runtime_context_update kind=\"conversation_summary\">", user_context)
        self.assertIn('<compact_context kind="pal" authority="conversation_continuity">', user_context)
        self.assertIn("### Summary\ncompacted prior context", user_context)
        self.assertIn("use a tool then continue", user_context)
        self.assertIn("<runtime_reminder", user_context)
        self.assertLess(user_context.index("</compact_context>"), user_context.index("use a tool then continue"))
        self.assertFalse(
            any(
                generate_requests[-1].messages[index]["role"] == generate_requests[-1].messages[index + 1]["role"] == "user"
                for index in range(len(generate_requests[-1].messages) - 1)
            )
        )
        self.assertIn("<runtime_reminder", post_compact_prompt)
        self.assertEqual(generate_requests[-1].messages[-1]["role"], "tool")
        self.assertNotIn("<runtime_reminder", _message_text(generate_requests[-1].messages[-1]))

    def test_auto_budget_compaction_opens_memory_candidate_approval_after_turn(self) -> None:
        candidate = {
            "kind": "fact",
            "title": "Budget compact candidates still require approval",
            "summary": "Automatic compact may propose durable memory candidates, but must not write L3 directly.",
            "source_excerpt": "automatic compact candidates require approval",
        }
        core, _memory_service, _scripted_llm = _build_core_with_compacting_llm(
            compact_on="preflight",
            memory_candidates=[candidate],
        )
        statuses: list[tuple[str, dict[str, object]]] = []

        async def capture_status(route, kind: str, payload: dict[str, object]) -> None:
            _ = route
            statuses.append((kind, payload))

        core._status_to_route_async = capture_status

        outcome = _run_turn(core)

        self.assertEqual(outcome.final_reply, "final answer")
        self.assertTrue(statuses)
        self.assertEqual(statuses[-1][0], "interactive_open")
        spec = statuses[-1][1]["spec"]
        self.assertEqual(spec.interaction_kind, "memory_candidate_approval")
        self.assertIn("Budget compact candidates still require approval", spec.text)
        accept = spec.buttons[0][0]
        self.assertEqual(accept.action_args["args"]["memory_candidates"], [candidate])

    def test_generate_compact_during_tool_turn_preserves_tool_result_and_endpoint_hint(self) -> None:
        core, memory_service, scripted_llm = _build_core_with_compacting_llm(compact_on="generate")

        outcome = _run_turn(core)

        self.assertEqual(outcome.final_reply, "final answer")
        self.assertTrue(scripted_llm.compaction_sources)
        self.assertGreaterEqual(scripted_llm.structured_compaction_max_output_tokens[0], 768)
        self.assertIsNone(memory_service.l2_store.get_entry("memory_summary_current"))
        self.assertIn("compacted prior context", memory_service.l1_store.items[0][0].content)
        generate_requests = [request for kind, request in scripted_llm.requests if kind == "generate"]
        preflight_requests = [request for kind, request in scripted_llm.requests if kind == "preflight"]
        self.assertIn("stable-result", "\n".join(_tool_contents(generate_requests[1])))
        self.assertIn("stable-result", "\n".join(_tool_contents(generate_requests[-1])))
        self.assertEqual(preflight_requests[-1].metadata.get("preferred_endpoint_id"), "fallback-after-tool")
        self.assertEqual(preflight_requests[-1].model_hint, "fallback-after-tool-model")
        post_compact_prompt = _request_text(generate_requests[-1])
        self.assertIn('<compact_context kind="pal" authority="conversation_continuity">', post_compact_prompt)
        self.assertIn("### Summary\ncompacted prior context", post_compact_prompt)
        self.assertNotIn("Compaction Note:\ncompacted prior context", post_compact_prompt)
        self.assertEqual(generate_requests[-1].messages[1]["role"], "user")
        user_context = _message_text(generate_requests[-1].messages[1])
        self.assertIn("<runtime_context_update kind=\"conversation_summary\">", user_context)
        self.assertIn('<compact_context kind="pal" authority="conversation_continuity">', user_context)
        self.assertIn("### Summary\ncompacted prior context", user_context)
        self.assertIn("use a tool then continue", user_context)
        self.assertIn("<runtime_reminder", user_context)
        self.assertLess(user_context.index("</compact_context>"), user_context.index("use a tool then continue"))
        self.assertFalse(
            any(
                generate_requests[-1].messages[index]["role"] == generate_requests[-1].messages[index + 1]["role"] == "user"
                for index in range(len(generate_requests[-1].messages) - 1)
            )
        )
        self.assertIn("<runtime_reminder", post_compact_prompt)
        self.assertEqual(generate_requests[-1].messages[-1]["role"], "tool")
        self.assertNotIn("<runtime_reminder", _message_text(generate_requests[-1].messages[-1]))


if __name__ == "__main__":
    unittest.main()
