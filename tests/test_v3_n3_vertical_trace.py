"""N3 vertical trace: ordinary request -> owner projection -> fake frames
-> real decode/accept -> observe_commit -> NEXT request replays the frozen
round exactly once (review TEST_MATRIX "唯一纵向trace", executor level).

Real pieces: TurnExecutor two_segment flow, MemoryService + HistoryRoot,
LLMRuntime (endpoint resolution, compile, ShapeEndpointInvoker, prompt
cache coordinator), the hosted EndpointProjectionSession, decode +
response normalization, L1 accept.  Faked: the network frames only.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace

from pal.core.turns import LLMRequestEffect
from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.ir import (
    GenerationPolicyIR, LLMMessageIR, LLMRequestIR, MessageRole, TextPartIR,
)
from pal.llm.models import LLMEndpointModel
from pal.llm.runtime import EndpointResolver, LLMRuntime
from pal.llm.shapes.base import _JSONFrame
from pal.memory import MemoryService
from pal.shared import PromptAssemblyContext
from tests.test_v3_n1_root_lifecycle import (
    assistant, user, executor as make_executor,
)


def _endpoint() -> LLMEndpointModel:
    return LLMEndpointModel(
        endpoint_id="trace-endpoint", provider="openai",
        model_id="trace-model", display_name="Trace",
        wire_shape="openai_completion", base_url="https://example.test/v1",
        auth_kind="api_key_ref", credential_ref="key",
        context_window=100_000, max_output_tokens=4_096,
        thinking_levels_blob=["off"], default_thinking_level="off",
        supports_tools=True, supports_streaming=False, supports_vision=False,
        input_modalities_blob=["text"], output_modalities_blob=["text"],
        priority=0, enabled=True, capabilities_blob={},
    )


class _Settings:
    def get_active_llm_endpoint_id(self):
        return "trace-endpoint"

    def get_think_level(self, _endpoint_id):
        return "off"


class CapturingTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.captured = []

    def frames(self, _endpoint, transport_request):
        self.captured.append(transport_request)
        payload = {
            "choices": [{
                "message": {"role": "assistant",
                            "content": self.replies[len(self.captured) - 1]},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }
        yield _JSONFrame(0, payload)

    def activate_endpoint(self, endpoint_id):
        pass

    def close(self):
        pass


def _runtime(transport) -> LLMRuntime:
    return LLMRuntime(
        EndpointResolver(endpoints=(_endpoint(),)),
        _Settings(),
        endpoint_invoker=ShapeEndpointInvoker(transport=transport),
        config=SimpleNamespace(
            runtime_root=tempfile.mkdtemp(),
            llm_endpoint_retry_attempts=1,
            llm_max_output_recovery_attempts=0,
        ),
    )


def _executor(memory, runtime):
    from pal.core.compaction import CompactionEngine
    from pal.core.pal_compaction import PalCompactionPolicy
    from pal.core.turn_executor import TurnExecutor

    async def call_port(port, async_name, _sync_name, *args):
        return await getattr(port, async_name)(*args)

    ports = {"memory:memory": memory, "llm:llm": runtime}
    return TurnExecutor(
        SimpleNamespace(port_registry=ports, require_port=ports.__getitem__,
                        execution_runtime=None),
        SimpleNamespace(diagnostics=[]), None,
        call_port_async=call_port, build_canonical_prompt=None,
        debug_log_prompt=lambda *a: None, debug_log_outcome=lambda *a: None,
        debug_log_reply=lambda *a: None, build_llm_tool_contracts=lambda: [],
        handle_failure_async=None, render_failure_feedback_text=lambda _: '',
        should_enter_failure_flow_for_tool_result=lambda _: False,
        compaction_engine=CompactionEngine(PalCompactionPolicy(), max_attempts=1,
                                           timeout_seconds=2),
        compaction_clock_provider=lambda: 1, compaction_mode='two_segment',
    )


def _request_for(memory: MemoryService, extra_tail) -> LLMRequestIR:
    """Compiler-equivalent assembly: preamble + durable L1 history + tail."""
    preamble = LLMMessageIR(role=MessageRole.SYSTEM,
                            parts=(TextPartIR("BASE"),), message_id="base")
    history = [
        message
        for turn in memory.l1_store.turns.turns
        for message in turn.messages
    ]
    return LLMRequestIR(
        messages=(preamble, *history, *extra_tail),
        tools=(),
        policy=GenerationPolicyIR(max_output_tokens=512),
        metadata={"prompt_cache_scope_id": "pal:resident"},
    )


def _drive(executor, request):
    async def run():
        return await executor._handle_llm_request(
            LLMRequestEffect(assembly_context=PromptAssemblyContext()),
            SimpleNamespace(
                budget_failure_feedback_text="",
                llm_round_index=0,
                preferred_llm_endpoint_id=None,
                preferred_llm_model_id=None,
                finalization_only=False,
                tool_observations=[],
                last_response_mode=None,
                turn_id="T",
            ),
        )

    executor.build_turn_prompt = lambda *args, **kwargs: request
    outcome = asyncio.run(run())
    return outcome


class VerticalTraceTests(unittest.TestCase):
    def test_two_rounds_freeze_once_and_replay_once(self):
        memory = MemoryService()
        memory.begin_l1_turn("T", user_message=user("Q1", "q1"))
        transport = CapturingTransport(["A1 answer", "A2 answer"])
        runtime = _runtime(transport)
        ex = _executor(memory, runtime)

        # Round 1: only the L1 user message rides the tail.
        request1 = _request_for(memory, ())
        outcome1 = _drive(ex, request1)
        self.assertEqual(outcome1.status.value if hasattr(outcome1.status, "value") else str(outcome1.status), "ok",
                         "round 1 must succeed")
        payload1 = json.dumps(dict(transport.captured[0].payload))
        self.assertIn("Q1", payload1)

        session = runtime.endpoint_projection_session("pal:resident")
        self.assertEqual(len(session.chunks), 1,
                         "accept must freeze exactly one chunk")
        self.assertIn("q1", session.chunks[0].semantic_span)
        accepted1 = [m for t in memory.l1_store.turns.turns for m in t.messages
                     if m.role == MessageRole.ASSISTANT]
        self.assertTrue(accepted1 and "A1" in accepted1[0].text,
                        "the decoded answer must be accepted into L1")
        self.assertIn(accepted1[0].message_id, session.chunks[0].semantic_span)

        # Round 2: history now includes Q1/A1; the new tail is Q2 only.
        memory.append_l1_user("T", user("Q2", "q2"))
        request2 = _request_for(memory, ())
        outcome2 = _drive(ex, request2)
        payload2 = json.dumps(dict(transport.captured[1].payload))
        self.assertEqual(payload2.count("Q1"), 1,
                         "frozen round replays exactly once, not re-encoded twice")
        self.assertEqual(payload2.count("A1 answer"), 1)
        self.assertEqual(payload2.count("Q2"), 1)
        self.assertIn("BASE", payload2)
        self.assertEqual(len(session.chunks), 2)
        self.assertIn("q2", session.chunks[1].semantic_span)
        accepted2 = [m for t in memory.l1_store.turns.turns for m in t.messages
                     if m.role == MessageRole.ASSISTANT]
        self.assertEqual(len(accepted2), 2)
        self.assertIn("A2", accepted2[1].text)

    def test_identityless_transient_content_falls_back_cold(self):
        memory = MemoryService()
        memory.begin_l1_turn("T", user_message=user("Q1", "q1"))
        transport = CapturingTransport(["A1 answer", "A2 answer"])
        runtime = _runtime(transport)
        ex = _executor(memory, runtime)
        _drive(ex, _request_for(memory, ()))

        # A transient compiler message with no durable id must NOT freeze:
        # the round falls back to the cold codec path and still succeeds.
        transient = LLMMessageIR(role=MessageRole.USER,
                                 parts=(TextPartIR("TRANSIENT"),))
        memory.append_l1_user("T", user("Q2", "q2"))
        outcome = _drive(ex, _request_for(memory, (transient,)))
        payload2 = json.dumps(dict(transport.captured[1].payload))
        self.assertIn("Q1", payload2)
        self.assertIn("TRANSIENT", payload2)
        session = runtime.endpoint_projection_session("pal:resident")
        self.assertTrue(all(
            "TRANSIENT" not in repr(item)
            for chunk in session.chunks for item in chunk.items
        ), "transient content must never freeze into the prefix")


if __name__ == "__main__":
    unittest.main()
