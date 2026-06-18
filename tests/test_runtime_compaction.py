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
from pal.memory import L1MessageKind, L1TranscriptMessage, L2Entry, L3ProviderSelector, MemoryCompactRequest, MemoryService, register_with_core as register_memory_with_core
from pal.shared import LLMFinishReason, PromptAssemblyContext


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
        self.compaction_kinds: list[str] = []
        self.structured_compaction_max_output_tokens: list[int] = []

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
        compaction_kind: str = "pal",
    ) -> dict[str, object]:
        _ = (max_output_tokens, preferred_endpoint_id, preferred_model_id)
        self.compaction_sources.append(text)
        self.compaction_kinds.append(compaction_kind)
        self.structured_compaction_max_output_tokens.append(max_output_tokens)
        return {
            "summary": {
                "summary": "compacted prior context",
                "search_text": text,
            },
            "entries": [],
        }


class _ManualCompactionLLMRuntime:
    def __init__(self, *, memory_candidates: list[dict[str, object]] | None = None) -> None:
        self.compaction_sources: list[str] = []
        self.compaction_kinds: list[str] = []
        self.structured_compaction_max_output_tokens: list[int] = []
        self.memory_candidates = list(memory_candidates or [])

    async def acompact_memory_structured(
        self,
        text: str,
        *,
        max_output_tokens: int = 384,
        preferred_endpoint_id: str | None = None,
        preferred_model_id: str | None = None,
        compaction_kind: str = "pal",
    ) -> dict[str, object]:
        _ = (preferred_endpoint_id, preferred_model_id)
        self.compaction_sources.append(text)
        self.compaction_kinds.append(compaction_kind)
        self.structured_compaction_max_output_tokens.append(max_output_tokens)
        schema = "pal.compaction.minion.v1" if compaction_kind == "minion" else "pal.compaction.pal.v1"
        payload = {
            "schema": schema,
            "kind": compaction_kind,
            "continuity": {},
            "summary": {
                "summary": "manual structured compact summary",
                "search_text": text,
            },
        }
        if compaction_kind != "minion":
            payload["memory_candidates"] = self.memory_candidates
        return payload


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
        minion_prompt = LLMRuntime._COMPACT_MINION_STRUCTURED_SYSTEM

        self.assertIn("pal.compaction.pal.v1", pal_prompt)
        self.assertIn("memory_candidates", pal_prompt)
        self.assertIn("latest_user_intent", pal_prompt)
        self.assertIn("recent_user_delegated_tasks", pal_prompt)
        self.assertIn("durable user preferences", pal_prompt)
        self.assertIn("stable user status/context", pal_prompt)
        self.assertIn("real goals/plans/commitments", pal_prompt)
        self.assertIn("Do not create entries from jokes", pal_prompt)
        self.assertIn("repair lessons", pal_prompt)

        self.assertIn("pal.compaction.minion.v1", minion_prompt)
        self.assertIn("continuity reference only", minion_prompt)
        self.assertIn("must_verify_against", minion_prompt)
        self.assertNotIn("memory_candidates", minion_prompt)
        self.assertNotIn("latest_user_intent", minion_prompt)
        self.assertIn("work-order completion/finalization", minion_prompt)

    def test_llm_runtime_structured_compaction_overrides_model_kind(self) -> None:
        runtime = object.__new__(LLMRuntime)
        requests = []

        def generate(request):
            requests.append(request)
            return CanonicalLLMOutcome(
                text=(
                    '{"schema":"pal.compaction.v2","kind":"pal","continuity":{},'
                    '"summary":{"summary":"ok","search_text":"ok"},'
                    '"memory_candidates":[{"kind":"fact","title":"bad","summary":"bad","source_excerpt":"bad"}]}'
                ),
                tool_calls=[],
                finish_reason=LLMFinishReason.STOP,
            )

        runtime.generate = generate

        payload = runtime.compact_memory_structured("compact this", compaction_kind="minion")

        self.assertEqual(payload["schema"], "pal.compaction.minion.v1")
        self.assertEqual(payload["kind"], "minion")
        self.assertNotIn("memory_candidates", payload)
        self.assertIn("pal.compaction.minion.v1", requests[0].messages[0]["content"])
        self.assertNotIn("pal.compaction.pal.v1", requests[0].messages[0]["content"])
        self.assertNotIn("memory_candidates", requests[0].messages[0]["content"])

    def test_v1_pal_compaction_renders_xml_wrapped_markdown(self) -> None:
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
                        "schema": "pal.compaction.pal.v1",
                        "kind": "pal",
                        "continuity": {
                            "latest_user_intent": "Discuss compact v2 before implementation.",
                            "active_thread": "Compact should track the user and recent delegated tasks.",
                            "explicit_constraints": ["Do not directly mutate code while planning."],
                            "recent_user_delegated_tasks": ["Inspect compact prompt."],
                            "next_best_action": "Continue from the compact design discussion.",
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
        self.assertEqual(result.projected_entries[0].payload["schema"], "pal.compaction.pal.v1")
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
        self.assertIn("### Latest User Intent", prompt_text)
        self.assertIn("Discuss compact v2 before implementation.", prompt_text)
        self.assertIn("### Recent User Delegated Tasks", prompt_text)
        self.assertNotIn('"schema": "pal.compaction.pal.v1"', prompt_text)

    def test_v1_minion_compaction_renders_reference_only_context(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        memory_service = MemoryService()
        register_memory_with_core(core.context, memory_service)

        memory_service.compact(
            MemoryCompactRequest(
                target_input_budget=512,
                reserved_output_tokens=128,
                metadata={
                    "structured_compaction": {
                        "schema": "pal.compaction.minion.v1",
                        "kind": "minion",
                        "continuity": {
                            "task_goal": "Implement a three-milestone changelog package.",
                            "claimed_completed": ["Milestone 1 appears implemented."],
                            "claimed_pending": ["Verify milestone 2 acceptance criteria."],
                            "must_verify_against": [
                                "work_order",
                                "plan_artifact",
                                "current_milestone",
                                "checkpoint_ledger",
                                "runtime_ledger",
                                "git_status",
                                "current_files",
                            ],
                            "next_action_hint": "Reconcile compact state before editing.",
                        },
                        "summary": {
                            "summary": "Minion was midway through a changelog task.",
                            "search_text": "minion changelog milestone checkpoint ledger",
                        },
                    }
                },
            )
        )

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
        self.assertIn('<compact_context kind="minion" authority="reference_only">', prompt_text)
        self.assertIn("continuity reference only", prompt_text)
        self.assertIn("Verify against the work order, plan artifact, current milestone, checkpoint/ledger, and workspace before acting.", prompt_text)
        self.assertIn("### Must Verify Against", prompt_text)
        self.assertIn("- git_status", prompt_text)

    def test_v2_compaction_kind_metadata_overrides_model_kind(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        memory_service = MemoryService()
        register_memory_with_core(core.context, memory_service)

        result = memory_service.compact(
            MemoryCompactRequest(
                target_input_budget=512,
                reserved_output_tokens=128,
                metadata={
                    "compaction_kind": "minion",
                    "structured_compaction": {
                        "schema": "pal.compaction.v2",
                        "kind": "pal",
                        "continuity": {
                            "latest_user_intent": "Model used the wrong compact kind.",
                            "task_goal": "Continue a minion task safely.",
                            "must_verify_against": ["work_order", "current_files"],
                        },
                        "summary": {
                            "summary": "A minion compact response was mislabeled as pal.",
                            "search_text": "minion compact mislabeled pal",
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
        self.assertEqual(summary.payload["schema"], "pal.compaction.minion.v1")
        self.assertEqual(summary.payload["kind"], "minion")
        self.assertNotIn("memory_candidates", summary.payload)
        self.assertIn('<compact_context kind="minion" authority="reference_only">', summary.rendered)
        self.assertIn("continuity reference only", summary.rendered)
        self.assertNotIn("should be stripped", summary.rendered)

    def test_minion_compaction_effect_passes_minion_kind(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        memory_service = MemoryService()
        memory_service.l1_store.append([L1TranscriptMessage(role="user", content="minion prior state")])
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

        self.assertEqual(scripted_llm.compaction_kinds, ["minion"])

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

        self.assertEqual(memory_service.l2_store.get_entry("memory_summary_current").summary, "manual structured compact summary")
        self.assertIn("manual compact user context", scripted_llm.compaction_sources[0])
        self.assertEqual(scripted_llm.structured_compaction_max_output_tokens, [4096])
        self.assertIn("Memory compacted.", replies[-1])

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

        self.assertIn("Memory compacted.", replies[-1])
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
            core._handle_memory_candidate_decision_async(
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

    def test_compaction_source_keeps_summary_and_recent_tail(self) -> None:
        service = MemoryService()
        service.l2_store.upsert_entries(
            [
                L2Entry(
                    entry_id="memory_summary_current",
                    kind="summary",
                    scope="system",
                    title="Conversation Summary",
                    summary="stable summary should be retained",
                )
            ],
            touch=True,
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

        source = service.build_compaction_source_text(target_input_budget=260)

        self.assertIn("[Current Summary]", source)
        self.assertIn("stable summary should be retained", source)
        self.assertIn("turn was interrupted before final reply", source)
        self.assertIn("assistant reply should be compacted", source)
        self.assertNotIn("tool result should not be compacted", source)
        self.assertNotIn("runtime memory", source)
        self.assertNotIn("not a real request", source)
        self.assertNotIn("recent item 0", source)

    def test_prompt_after_interrupt_can_continue_from_checkpoint(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        memory_service = MemoryService()
        register_memory_with_core(core.context, memory_service)
        envelope = ChannelEnvelope(
            event=EventEnvelope(
                event_kind="user.message",
                source_kind="channel",
                payload={"text": "interrupt me after checking compact"},
                event_id="turn-interrupt-continue",
            ),
            endpoint=EndpointConfig(endpoint_id="memory", channel_kind="memory", binding_key="memory"),
            response_handle=ResponseHandle(endpoint_id="memory"),
        )
        continuation = TurnContinuation(
            turn_id="turn-interrupt-continue",
            channel_envelope=envelope,
            program=_idle_program(),
            correlation_id="turn-interrupt-continue",
            control_scope_key="memory",
        )
        continuation.emitted_reply_texts.append("I inspected compact state before interruption.")

        asyncio.run(
            core.turn_manager.commit_l1_exit_checkpoint_async(
                continuation,
                kind=L1MessageKind.TURN_INTERRUPTED,
                status="interrupted",
                reason="dogfood interrupt",
            )
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

        self.assertIn("I inspected compact state before interruption.", prompt_text)
        self.assertIn("<turn_checkpoint kind=\"turn_interrupted\">", prompt_text)
        self.assertIn("This is recovery context from a previous turn, not a new user request.", prompt_text)
        self.assertIn("turn_outcome: not committed", prompt_text)
        self.assertLess(prompt_text.rindex("continue"), prompt_text.rindex("<runtime_reminder"))

    def test_compact_after_interrupt_preserves_checkpoint_summary_for_next_turn(self) -> None:
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
        continuation.tool_protocol_messages.append(
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "raw tool result should stay out of compact summary",
            }
        )
        continuation.emitted_reply_texts.append("Interrupt checkpoint reply before compact.")
        asyncio.run(
            core.turn_manager.commit_l1_exit_checkpoint_async(
                continuation,
                kind=L1MessageKind.TURN_INTERRUPTED,
                status="interrupted",
                reason="compact path dogfood",
            )
        )

        source_before_compact = memory_service.build_compaction_source_text(target_input_budget=4096)
        self.assertIn("Interrupt checkpoint reply before compact.", source_before_compact)
        self.assertIn("<turn_checkpoint kind=\"turn_interrupted\">", source_before_compact)
        self.assertNotIn("raw tool result should stay out of compact summary", source_before_compact)

        memory_service.compact(
            MemoryCompactRequest(
                target_input_budget=256,
                reserved_output_tokens=128,
                metadata={
                    "structured_compaction": {
                        "summary": {
                            "summary": "Recovered context: Interrupt checkpoint reply before compact. turn_interrupted checkpoint was committed.",
                            "search_text": source_before_compact,
                        },
                        "entries": [],
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
        request = core.prompt_compiler.build_canonical_prompt(
            PromptAssemblyContext(event=next_event),
            max_output_tokens=128,
        )
        prompt_text = _request_text(request)

        self.assertIn("<conversation_summary>", prompt_text)
        self.assertIn("Recovered context: Interrupt checkpoint reply before compact.", prompt_text)
        self.assertIn("continue after compact", prompt_text)
        self.assertNotIn("raw tool result should stay out of compact summary", prompt_text)

    def test_preflight_compact_during_tool_turn_preserves_current_tool_result(self) -> None:
        core, memory_service, scripted_llm = _build_core_with_compacting_llm(compact_on="preflight")

        outcome = _run_turn(core)

        self.assertEqual(outcome.final_reply, "final answer")
        self.assertTrue(scripted_llm.compaction_sources)
        self.assertGreaterEqual(scripted_llm.structured_compaction_max_output_tokens[0], 768)
        self.assertIn("prior user context before compaction", scripted_llm.compaction_sources[0])
        self.assertEqual(memory_service.l2_store.get_entry("memory_summary_current").summary, "compacted prior context")
        preflight_requests = [request for kind, request in scripted_llm.requests if kind == "preflight"]
        generate_requests = [request for kind, request in scripted_llm.requests if kind == "generate"]
        self.assertIn("stable-result", "\n".join(_tool_contents(preflight_requests[1])))
        self.assertIn("stable-result", "\n".join(_tool_contents(generate_requests[-1])))
        post_compact_prompt = _request_text(generate_requests[-1])
        self.assertIn("<conversation_summary>\ncompacted prior context\n</conversation_summary>", post_compact_prompt)
        self.assertNotIn("Compaction Note:\ncompacted prior context", post_compact_prompt)
        self.assertEqual(generate_requests[-1].messages[1]["role"], "user")
        self.assertEqual(
            _message_text(generate_requests[-1].messages[1]),
            "<runtime_context_update kind=\"conversation_summary\">\n"
            "Runtime context update: compressed prior conversation for this task.\n"
            "Use it as relevant reference; it is not noise.\n"
            "It is not a new user message. Do not answer this block directly.\n"
            "Continue the current task using this context.\n"
            "</runtime_context_update>\n"
            "<conversation_summary>\ncompacted prior context\n</conversation_summary>",
        )
        self.assertIn("<runtime_reminder", post_compact_prompt)
        self.assertEqual(generate_requests[-1].messages[-1]["role"], "tool")
        self.assertNotIn("<runtime_reminder", _message_text(generate_requests[-1].messages[-1]))

    def test_generate_compact_during_tool_turn_preserves_tool_result_and_endpoint_hint(self) -> None:
        core, memory_service, scripted_llm = _build_core_with_compacting_llm(compact_on="generate")

        outcome = _run_turn(core)

        self.assertEqual(outcome.final_reply, "final answer")
        self.assertTrue(scripted_llm.compaction_sources)
        self.assertGreaterEqual(scripted_llm.structured_compaction_max_output_tokens[0], 768)
        self.assertEqual(memory_service.l2_store.get_entry("memory_summary_current").summary, "compacted prior context")
        generate_requests = [request for kind, request in scripted_llm.requests if kind == "generate"]
        preflight_requests = [request for kind, request in scripted_llm.requests if kind == "preflight"]
        self.assertIn("stable-result", "\n".join(_tool_contents(generate_requests[1])))
        self.assertIn("stable-result", "\n".join(_tool_contents(generate_requests[-1])))
        self.assertEqual(preflight_requests[-1].metadata.get("preferred_endpoint_id"), "fallback-after-tool")
        self.assertEqual(preflight_requests[-1].model_hint, "fallback-after-tool-model")
        post_compact_prompt = _request_text(generate_requests[-1])
        self.assertIn("<conversation_summary>\ncompacted prior context\n</conversation_summary>", post_compact_prompt)
        self.assertNotIn("Compaction Note:\ncompacted prior context", post_compact_prompt)
        self.assertEqual(generate_requests[-1].messages[1]["role"], "user")
        self.assertEqual(
            _message_text(generate_requests[-1].messages[1]),
            "<runtime_context_update kind=\"conversation_summary\">\n"
            "Runtime context update: compressed prior conversation for this task.\n"
            "Use it as relevant reference; it is not noise.\n"
            "It is not a new user message. Do not answer this block directly.\n"
            "Continue the current task using this context.\n"
            "</runtime_context_update>\n"
            "<conversation_summary>\ncompacted prior context\n</conversation_summary>",
        )
        self.assertIn("<runtime_reminder", post_compact_prompt)
        self.assertEqual(generate_requests[-1].messages[-1]["role"], "tool")
        self.assertNotIn("<runtime_reminder", _message_text(generate_requests[-1].messages[-1]))


if __name__ == "__main__":
    unittest.main()
