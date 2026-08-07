from __future__ import annotations

import asyncio
import unittest

from pal.channel.contracts import ChannelEnvelope, EndpointConfig, ResponseHandle
from pal.core.agent_turn_runtime import AgentTurnRuntime, AgentTurnGuardHost
from pal.core.contracts import CoreRuntimeState
from pal.core.main_context import MainContext
from pal.core.runtime import TurnManager
from pal.core.runtime_config import RuntimeConfig
from pal.core.turn_executor import TurnExecutor
from pal.core.turns import TurnOutcome
from pal.foundation.io import EventEnvelope
from pal.llm.contracts import LLMGenerationResult, LLMPreflightAdvice, LLMPreflightRequest
from pal.llm.ir import (
    LLMFinishReason,
    LLMMessageIR,
    LLMResponseIR,
    MessageRole,
    TextPartIR,
)
from pal.memory import MemoryService
from pal.memory.contracts import L1MessageKind
from pal.shared import (
    EventKind,
    LLMPreflightStatus,
    LLMResponseMode,
    SourceKind,
    ToolExecutionResult,
)
from pal.shared.tool_protocol import ToolResultIR, new_tool_call


def _make_envelope(
    *,
    turn_id: str,
    text: str,
    session_id: str = "interjection-session",
) -> ChannelEnvelope:
    return ChannelEnvelope(
        event=EventEnvelope(
            event_kind=EventKind.USER_MESSAGE,
            source_kind=SourceKind.CHANNEL,
            payload={"text": text, "session_id": session_id},
            correlation_id=turn_id,
            event_id=turn_id,
        ),
        endpoint=EndpointConfig(
            endpoint_id="interjection_endpoint",
            channel_kind="memory",
            binding_key="memory://interjection",
        ),
        response_handle=ResponseHandle(
            endpoint_id="interjection_endpoint",
            reply_target={"session_id": session_id},
        ),
    )


class _FakeLLMPort:
    """Non-streaming fake LLM: first generation issues one tool call,
    subsequent generations return a plain text reply."""

    def __init__(self) -> None:
        self.generation_index = 0

    async def apreflight(self, request: LLMPreflightRequest) -> LLMPreflightAdvice:
        return LLMPreflightAdvice(
            status=LLMPreflightStatus.READY.value,
            breakdown={},
        )

    async def agenerate(self, request) -> LLMGenerationResult:
        self.generation_index += 1
        if self.generation_index == 1:
            return LLMGenerationResult(
                response=LLMResponseIR(
                    message=LLMMessageIR(
                        role=MessageRole.ASSISTANT,
                        parts=(
                            TextPartIR("calling the stub tool"),
                            new_tool_call(
                                call_id="call_1",
                                name="stub_tool",
                                args={"x": 1},
                            ),
                        ),
                        semantic_kind=L1MessageKind.ASSISTANT_TOOL_CALL,
                    ),
                    finish_reason=LLMFinishReason.TOOL_CALLS,
                ),
                response_mode=LLMResponseMode.OPERATIONAL,
            )
        return LLMGenerationResult(
            response=LLMResponseIR(
                message=LLMMessageIR(
                    role=MessageRole.ASSISTANT,
                    parts=(TextPartIR("done."),),
                ),
                finish_reason=LLMFinishReason.STOP,
            ),
            response_mode=LLMResponseMode.CHAT,
        )


class InterjectionInjectionTests(unittest.TestCase):
    """Interjection injection: messages arriving while a turn is busy
    executing a tool batch are consumed from the head of the pending queue
    and injected into the next LLM generation, right after the tool results.
    """

    def setUp(self) -> None:
        self.context = MainContext()
        self.context.port_registry["memory:memory"] = MemoryService()
        self.fake_llm = _FakeLLMPort()
        self.context.port_registry["llm:llm"] = self.fake_llm
        self.state = CoreRuntimeState()
        self.turn_manager = TurnManager(
            context=self.context,
            state=self.state,
            config=RuntimeConfig.defaults(),
        )
        self.captured_prompts = []
        self.interjection_enqueued = False

        async def call_port_async(port, async_name: str, sync_name: str, *args, **kwargs):
            if async_name == "apreflight":
                self.captured_prompts.append(args[0].request)
                return LLMPreflightAdvice(
                    status=LLMPreflightStatus.READY.value,
                    breakdown={},
                )
            if async_name == "agenerate":
                self.captured_prompts.append(args[0])
                return await port.agenerate(args[0])
            raise AssertionError(f"unexpected port call: {async_name}")

        async def execute_tool_async(call, *, allow_tools, budget, turn_id):
            # Simulate the user sending a message while the tool runs.
            if not self.interjection_enqueued:
                self.interjection_enqueued = True
                self.state.pending_channel_turns.append(
                    _make_envelope(
                        turn_id="interjection-1",
                        text="wait, hold on",
                    )
                )
            return ToolExecutionResult(
                name=call.name,
                ok=True,
                text="tool returned",
                llm_text="tool returned",
                call_id=call.call_id,
            )

        self.runtime = AgentTurnRuntime.build(
            context=self.context,
            config=RuntimeConfig.defaults(),
            call_port_async=call_port_async,
            debug_log_prompt=lambda *_: None,
            debug_log_outcome=lambda *_: None,
            debug_log_reply=lambda *_: None,
            build_llm_tool_contracts=lambda: [],
            handle_failure_async=lambda *_, **__: None,
            render_failure_feedback_text=lambda _: "",
            should_enter_failure_flow_for_tool_result=lambda _: False,
            state=self.state,
            guard_host=AgentTurnGuardHost(guard=self.turn_manager.guard),
            execute_tool_async=execute_tool_async,
        )
        self.executor: TurnExecutor = self.runtime.executor

    def _drive(self, continuation) -> TurnOutcome:
        async def scenario() -> TurnOutcome:
            current = None
            while True:
                yielded = self.turn_manager.resume(continuation, current)
                if isinstance(yielded, TurnOutcome):
                    return yielded
                current = await self.executor.execute_turn_effect_async(
                    continuation,
                    yielded,
                )

        return asyncio.run(scenario())

    def test_interjection_during_tool_batch_is_injected_into_next_generation(self) -> None:
        envelope = _make_envelope(turn_id="turn-main", text="run the batch")
        continuation = self.turn_manager.start(envelope)

        outcome = self._drive(continuation)

        # The turn ran to a normal completion.
        self.assertIsNotNone(outcome)
        # The pending queue was consumed by the injection; the interjection
        # never starts as its own turn afterwards.
        self.assertEqual(len(self.state.pending_channel_turns), 0)
        # Two LLM generations happened: tool-call generation and final reply.
        self.assertGreaterEqual(len(self.captured_prompts), 2)

        second_prompt = self.captured_prompts[-1]
        messages = list(second_prompt.messages)
        # The injected interjection appears as a USER message, positioned
        # right after the tool result (the tail of the active transcript).
        self.assertEqual(messages[-1].role, MessageRole.USER)
        self.assertIn("wait, hold on", messages[-1].text)
        self.assertEqual(messages[-1].semantic_kind, "user_interjection")
        # The message before the interjection is the tool result.
        self.assertEqual(messages[-2].role, MessageRole.TOOL)
        self.assertTrue(
            any(isinstance(part, ToolResultIR) for part in messages[-2].parts)
        )

    def test_only_head_of_queue_is_injected(self) -> None:
        # Pre-seeded queue; disable the execute_tool_async auto-enqueue so
        # the queue is exactly the two pre-seeded interjections.
        self.interjection_enqueued = True
        envelope = _make_envelope(turn_id="turn-main", text="run the batch")
        self.state.pending_channel_turns.append(
            _make_envelope(turn_id="interjection-1", text="first interjection")
        )
        self.state.pending_channel_turns.append(
            _make_envelope(turn_id="interjection-2", text="second interjection")
        )
        continuation = self.turn_manager.start(envelope)

        outcome = self._drive(continuation)

        self.assertIsNotNone(outcome)
        # Only the head was consumed; the second interjection stays queued
        # and will be picked up as the next turn.
        self.assertEqual(len(self.state.pending_channel_turns), 1)
        remaining = self.state.pending_channel_turns[0]
        self.assertIn(
            "second interjection",
            str(remaining.event.payload.get("text") or ""),
        )

        second_prompt = self.captured_prompts[-1]
        messages = list(second_prompt.messages)
        self.assertEqual(messages[-1].role, MessageRole.USER)
        self.assertIn("first interjection", messages[-1].text)
        self.assertNotIn("second interjection", messages[-1].text)

    def test_injection_restores_envelope_when_l1_unavailable(self) -> None:
        # No L1 turn has been begun (preflight never ran), so appending the
        # interjection fails; the envelope must be restored at the head.
        envelope = _make_envelope(turn_id="turn-main", text="run the batch")
        continuation = self.turn_manager.start(envelope)
        queued = _make_envelope(turn_id="interjection-1", text="wait, hold on")
        self.state.pending_channel_turns.append(queued)

        async def scenario() -> None:
            await self.executor._inject_pending_interjection_async(continuation)

        asyncio.run(scenario())

        # The envelope is back at the head of the queue — nothing lost.
        self.assertEqual(len(self.state.pending_channel_turns), 1)
        self.assertIs(self.state.pending_channel_turns[0], queued)
        # Nothing was injected into L1 (the turn has no L1 transcript).
        self.assertIsNone(self.context.port_registry["memory:memory"].active_l1_turn("turn-main"))


if __name__ == "__main__":
    unittest.main()
