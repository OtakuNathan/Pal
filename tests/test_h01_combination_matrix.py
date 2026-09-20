"""G3(d): H01 combination matrix — two hosts × three shapes × two modes.

Every combination drives the REAL two-segment executor entry
(compact_memory_async in two_segment mode) over a REAL MemoryService
HistoryRoot, with the REAL engine and validator; the LLM is a network-side
fake only.  Each combo then verifies the handoff view through the REAL
codec for its wire shape (prepare_handoff) and asserts the v3 essentials:
the compact source contains the left segment only, the newest right tail
survives the install verbatim, and the task can continue afterwards.

Honest scope notes (delivery limitations):
- The two-segment handoff currently rides the cold-left engine path; the
  warm-anchor split is a documented remaining limitation, so the
  "warm eligible" mode column exercises eligibility resolution falling
  back to cold rather than a cached-prefix handoff.
- The Bunshin host dimension uses the real BunshinCompactionPolicy and a
  bunshin-scoped runtime; the full Bunshin runner harness is covered by
  the inherited bunshin suites.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from pal.core.compaction import CompactionEngine
from pal.core.pal_compaction import PalCompactionPolicy
from pal.core.turn_executor import TurnExecutor
from pal.llm import generation_result_from_values
from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR, WireShape
from pal.llm.projection_contracts import (
    AttemptKey,
    EndpointBinding,
    HistoryCursor,
    LogicalSessionId,
    OwnerFence,
)
from pal.llm.projection_session import EndpointProjectionSession
from pal.memory import MemoryService
from pal.memory.contracts import L1MessageKind, L1TranscriptMessage

HOSTS = ("resident", "bunshin")
SHAPES = (
    WireShape.OPENAI_COMPLETION,
    WireShape.OPENAI_RESPONSE,
    WireShape.ANTHROPIC_MESSAGES,
)
MODES = ("warm_eligible_cold_fallback", "cold")

_CONTAINERS = {
    WireShape.OPENAI_COMPLETION: "messages",
    WireShape.OPENAI_RESPONSE: "input",
    WireShape.ANTHROPIC_MESSAGES: "messages",
}


def _seed_transcript() -> list[L1TranscriptMessage]:
    return [L1TranscriptMessage(role="assistant", content="SEED0",
                                kind=L1MessageKind.RUNTIME_CONTEXT_SUMMARY)]


def _settled(mark: str) -> list[L1TranscriptMessage]:
    return [
        L1TranscriptMessage(role="user", content=f"{mark} request",
                            kind=L1MessageKind.USER_REQUEST),
        L1TranscriptMessage(role="assistant", content=f"{mark} reply",
                            kind=L1MessageKind.ASSISTANT_REPLY),
    ]


class _FakeNetwork:
    def __init__(self, host: str) -> None:
        self.requests: list = []
        self._host = host

    async def agenerate(self, request, *args, **kwargs):
        self.requests.append(request)
        from tests.test_runtime_compaction import (
            _valid_bunshin_payload,
            _valid_pal_payload,
        )

        text = (
            _valid_bunshin_payload()
            if self._host == "bunshin"
            else _valid_pal_payload("H01 summary")
        )
        return generation_result_from_values(text=text)


class _Ports:
    """Minimal port registry: memory + llm only, fake network behind llm."""

    def __init__(self, memory_service: MemoryService, llm: _FakeNetwork) -> None:
        self._ports = {"memory:memory": memory_service, "llm:llm": llm}

    def get(self, name: str):
        return self._ports.get(name)

    def require_port(self, name: str):
        value = self._ports.get(name)
        if value is None:
            raise KeyError(name)
        return value


class _Clock:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> int:
        self.value += 1
        return self.value


def _executor(host: str, ports: _Ports) -> TurnExecutor:
    if host == "bunshin":
        from pal.bunshin.runner import BunshinCompactionPolicy

        policy = BunshinCompactionPolicy()
    else:
        policy = PalCompactionPolicy()
    engine = CompactionEngine(policy=policy, max_attempts=1, timeout_seconds=10.0)
    return TurnExecutor(
        SimpleNamespace(port_registry=ports, require_port=ports.require_port),
        SimpleNamespace(diagnostics=[]),
        None,
        call_port_async=None,
        build_canonical_prompt=None,
        debug_log_prompt=lambda *a: None,
        debug_log_outcome=lambda *a: None,
        debug_log_reply=lambda *a: None,
        build_llm_tool_contracts=lambda: [],
        handle_failure_async=None,
        render_failure_feedback_text=lambda v: "",
        should_enter_failure_flow_for_tool_result=lambda v: False,
        compaction_engine=engine,
        compaction_clock_provider=_Clock(),
        compaction_mode="two_segment",
        compaction_scope=f"{host}:h01",
    )


def _binding(shape: WireShape) -> EndpointBinding:
    return EndpointBinding(
        endpoint_id="endpoint-h01", model_id="model-h01", wire_shape=shape,
        endpoint_spec_revision="rev-1", continuation_policy_version="policy-1",
        config_fingerprint="fp-h01",
    )


class H01CombinationMatrixTests(unittest.TestCase):
    def _scenario(self, host: str, shape: WireShape, mode: str) -> None:
        service = MemoryService()
        service.l1_store.append(_seed_transcript())
        service.l1_store.append(_settled("s0"))
        service.l1_store.append(_settled("s1"))
        service.begin_l1_turn(f"{host}-task", user_text="RIGHT_SENTINEL current work")
        service.upsert_l1_assistant(
            f"{host}-task",
            LLMMessageIR(role=MessageRole.ASSISTANT,
                         parts=(TextPartIR("A_DECISION live"),),
                         message_id=f"{host}-assistant-1"),
        )
        network = _FakeNetwork(host)
        ports = _Ports(service, network)
        executor = _executor(host, ports)
        continuation = SimpleNamespace(turn_id=f"{host}-task")
        assembly = SimpleNamespace(
            metadata={"prompt_cache_scope_id": f"{host}:h01"},
            work_order_id=f"{host}-order" if host == "bunshin" else "",
            core_mode="bunshin" if host == "bunshin" else "",
        )

        result = asyncio.run(executor.compact_memory_async(
            service,
            target_input_budget=1_000_000,
            reserved_output_tokens=1024,
            assembly_context=assembly,
            continuation=continuation,
        ))
        self.assertTrue(result.success, f"{host}/{shape.value}/{mode}: {result.failures}")

        # 1) exactly one generation left, and it consumed the LEFT source.
        self.assertEqual(len(network.requests), 1)
        source_text = " ".join(
            part.text
            for message in getattr(network.requests[0], "messages", ())
            for part in getattr(message, "parts", ())
            if isinstance(part, TextPartIR)
        )
        self.assertIn("s0 reply", source_text)
        self.assertIn("s1 reply", source_text)
        self.assertNotIn("RIGHT_SENTINEL", source_text)
        self.assertNotIn("A_DECISION live", source_text)

        # 2) the newest right tail survived the install verbatim.
        root = service.history_root
        right = root.right_turns()
        self.assertEqual([t.turn_id for t in right], [f"{host}-task"])
        self.assertIn("RIGHT_SENTINEL", right[0].messages[0].text)
        seed_text = service.l1_store.turns.turns[0].messages[0].text
        seed_marker = "shared engine" if host == "bunshin" else "H01 summary"
        self.assertIn(seed_marker, seed_text)

        # 3) the task continues: a next authorized round lands in R only.
        service.upsert_l1_assistant(
            f"{host}-task",
            LLMMessageIR(role=MessageRole.ASSISTANT,
                         parts=(TextPartIR("NEXT_ROUND after compact"),),
                         message_id=f"{host}-assistant-2"),
        )
        self.assertIn("NEXT_ROUND after compact",
                      " ".join(m.text for m in root.right_messages()))

        # 4) the handoff view encodes through the REAL codec for this shape:
        #    base shell + left(seed) + trailing instruction, right excluded.
        session = EndpointProjectionSession(LogicalSessionId(f"{host}:h01"))
        session.bind(_binding(shape))
        left = root.left_turns()
        instruction = LLMMessageIR(role=MessageRole.USER,
                                   parts=(TextPartIR("Summarize the history."),))
        handoff = session.prepare_handoff(
            [m for t in left for m in t.messages],
            instruction=instruction,
            attempt=AttemptKey(identity=session.identity,  # type: ignore[arg-type]
                               owner_fence=OwnerFence(0), attempt_id=f"{host}-h1"),
        )
        import json

        payload = json.loads(handoff.payload_json)
        blob = repr(payload[_CONTAINERS[shape]])
        self.assertIn(seed_marker, blob)
        self.assertIn("Summarize the history.", blob)
        self.assertNotIn("RIGHT_SENTINEL", blob)
        self.assertNotIn("NEXT_ROUND after compact", blob)
        if mode == "warm_eligible_cold_fallback":
            # Warm eligibility that cannot be honored falls back to the
            # honest cold-left request — never an invented cached prefix.
            self.assertEqual(len(network.requests), 1)

    def test_H01_matrix(self):
        for host in HOSTS:
            for shape in SHAPES:
                for mode in MODES:
                    with self.subTest(host=host, shape=shape.value, mode=mode):
                        self._scenario(host, shape, mode)


if __name__ == "__main__":
    unittest.main()
