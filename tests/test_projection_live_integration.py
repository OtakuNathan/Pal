"""G3+: projection session goes LIVE in the two-segment chain.

1. LLMRuntime.endpoint_projection_session hosts per-scope sessions bound to
   the active endpoint (lazy create, stable identity, rebind on endpoint
   switch).
2. The real two_segment executor flow drives on_left_replaced after a
   successful install: the hosted session's frozen prefix becomes the new
   seed and its frontier advances — post-commit, never a rollback (F13).
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from pal.core.compaction import CompactionEngine
from pal.core.pal_compaction import PalCompactionPolicy
from pal.core.turn_executor import TurnExecutor
from pal.llm import generation_result_from_values
from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
from pal.llm.projection_contracts import HistoryCursor
from pal.llm.projection_session import EndpointProjectionSession
from pal.llm.runtime import LLMRuntime
from pal.memory import MemoryService
from pal.memory.contracts import L1MessageKind, L1TranscriptMessage


def _seed() -> list[L1TranscriptMessage]:
    return [L1TranscriptMessage(role="assistant", content="SEED0",
                                kind=L1MessageKind.RUNTIME_CONTEXT_SUMMARY)]


def _settled(mark: str) -> list[L1TranscriptMessage]:
    return [
        L1TranscriptMessage(role="user", content=f"{mark} request",
                            kind=L1MessageKind.USER_REQUEST),
        L1TranscriptMessage(role="assistant", content=f"{mark} reply",
                            kind=L1MessageKind.ASSISTANT_REPLY),
    ]


class _Endpoint:
    def __init__(self, endpoint_id: str, model_id: str = "m1",
                 shape: str = "openai_completion") -> None:
        self.endpoint_id = endpoint_id
        self.model_id = model_id
        self.wire_shape = shape
        self.provider = "test"
        self.base_url = "http://test.local"
        self.auth_kind = "api_key_ref"
        self.credential_ref = "test-cred-ref"
        self.display_name = endpoint_id
        self.default_thinking_level = "high"
        self.thinking_levels_blob = ["off", "minimal", "low", "medium", "high"]
        self.input_modalities_blob = []
        self.supports_tools = True
        self.supports_streaming = True
        self.supports_vision = False
        self.context_window = 200000
        self.max_output_tokens = 8192


class _Resolver:
    def __init__(self, *endpoints: _Endpoint) -> None:
        self.endpoints = list(endpoints)

    def primary(self, *, preferred_endpoint_id=None):
        if preferred_endpoint_id:
            for endpoint in self.endpoints:
                if endpoint.endpoint_id == preferred_endpoint_id:
                    return endpoint
        return self.endpoints[0] if self.endpoints else None


class _Settings:
    def get_active_llm_endpoint_id(self):
        return None

    def get_think_level(self, endpoint_id):
        return ""

    def set_think_level(self, endpoint_id, value):
        pass


def _runtime(*endpoints: _Endpoint) -> LLMRuntime:
    return LLMRuntime(
        endpoint_resolver=_Resolver(*endpoints),
        settings_repository=_Settings(),
        endpoint_invoker=SimpleNamespace(),
        config=SimpleNamespace(runtime_root=None, llm_endpoint_retry_attempts=1),
    )


class RuntimeHostingTests(unittest.TestCase):
    def test_same_scope_same_session_with_stable_binding(self):
        runtime = _runtime(_Endpoint("e1"))
        first = runtime.endpoint_projection_session("pal:resident")
        second = runtime.endpoint_projection_session("pal:resident")
        self.assertIs(first, second)
        self.assertEqual(first.binding.endpoint_id, "e1")
        self.assertEqual(first.identity.projection_generation, 0)

    def test_endpoint_switch_rebinds_once(self):
        runtime = _runtime(_Endpoint("e1"), _Endpoint("e2"))
        session = runtime.endpoint_projection_session("s")
        runtime.active_endpoint_id = "e2"
        rebound = runtime.endpoint_projection_session("s")
        self.assertIs(rebound, session)
        self.assertEqual(rebound.binding.endpoint_id, "e2")
        self.assertEqual(rebound.identity.projection_generation, 1)
        # Stable afterwards: no generation churn on repeat calls.
        runtime.endpoint_projection_session("s")
        self.assertEqual(rebound.identity.projection_generation, 1)

    def test_no_endpoint_returns_none(self):
        runtime = _runtime()
        self.assertIsNone(runtime.endpoint_projection_session("s"))


class _Network:
    def __init__(self) -> None:
        self.requests: list = []

    async def agenerate(self, request, *a, **kw):
        self.requests.append(request)
        from tests.test_runtime_compaction import _valid_pal_payload

        return generation_result_from_values(
            text=_valid_pal_payload("LIVE REBASE SEED"))


class _Ports:
    def __init__(self, memory, llm) -> None:
        self._ports = {"memory:memory": memory, "llm:llm": llm}

    def get(self, name):
        return self._ports.get(name)

    def require_port(self, name):
        return self._ports[name]


class ExecutorLiveRebaseTests(unittest.TestCase):
    def test_install_drives_on_left_replaced_on_hosted_session(self):
        service = MemoryService()
        service.l1_store.append(_seed())
        service.l1_store.append(_settled("s0"))
        service.begin_l1_turn("task", user_text="RIGHT live work")
        root = service.history_root

        runtime = _runtime(_Endpoint("e1"))
        session = runtime.endpoint_projection_session("pal:resident")
        frontier_before = session.frontier
        network = _Network()
        ports = _Ports(service, _LiveLLM(runtime, network))
        engine = CompactionEngine(policy=PalCompactionPolicy(),
                                  max_attempts=1, timeout_seconds=10.0)
        executor = TurnExecutor(
            SimpleNamespace(port_registry=ports, require_port=ports.require_port,
                            execution_runtime=None),
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
            compaction_clock_provider=lambda: 1,
        )

        result = asyncio.run(executor.compact_memory_async(
            service,
            target_input_budget=1_000_000,
            reserved_output_tokens=1024,
            assembly_context=SimpleNamespace(
                metadata={"prompt_cache_scope_id": "pal:resident"}),
            continuation=SimpleNamespace(turn_id="task"),
        ))
        self.assertTrue(result.success, result.failures)
        # The hosted session was rebased: frontier advanced past the install
        # and the frozen prefix now carries the new seed only.
        self.assertNotEqual(session.frontier, frontier_before)
        self.assertNotEqual(session.frontier, HistoryCursor.initial())
        prefix_blob = repr(session._prefix_items)
        self.assertIn("LIVE REBASE SEED", prefix_blob)
        self.assertNotIn("s0 reply", prefix_blob)
        self.assertEqual(session.chunks, ())

    def test_rebase_failure_never_fails_the_install(self):
        service = MemoryService()
        service.l1_store.append(_seed())
        service.l1_store.append(_settled("s0"))
        root = service.history_root

        class _Boom:
            def endpoint_projection_session(self, scope_id):
                raise RuntimeError("hosting exploded")

            async def agenerate(self, request, *a, **kw):
                from tests.test_runtime_compaction import _valid_pal_payload

                return generation_result_from_values(
                    text=_valid_pal_payload("S"))

        diagnostics: list = []
        ports = _Ports(service, _Boom())
        engine = CompactionEngine(policy=PalCompactionPolicy(),
                                  max_attempts=1, timeout_seconds=10.0)
        executor = TurnExecutor(
            SimpleNamespace(port_registry=ports, require_port=ports.require_port,
                            execution_runtime=None),
            SimpleNamespace(diagnostics=diagnostics),
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
            compaction_clock_provider=lambda: 1,
        )
        result = asyncio.run(executor.compact_memory_async(
            service,
            target_input_budget=1_000_000,
            reserved_output_tokens=1024,
            assembly_context=SimpleNamespace(
                metadata={"prompt_cache_scope_id": "pal:resident"}),
            continuation=SimpleNamespace(turn_id="t"),
        ))
        self.assertTrue(result.success, result.failures)
        self.assertTrue(any(
            d.get("kind") == "two_segment_projection_rebase_failed"
            for d in diagnostics))
        # The left segment is installed regardless.
        self.assertIn("S", service.l1_store.turns.turns[0].messages[0].text)


class _LiveLLM:
    """LLM port facade: agenerate over the fake network + projection host."""

    def __init__(self, runtime: LLMRuntime, network: _Network) -> None:
        self._runtime = runtime
        self._network = network

    def endpoint_projection_session(self, scope_id):
        return self._runtime.endpoint_projection_session(scope_id)

    async def agenerate(self, request, *a, **kw):
        return await self._network.agenerate(request, *a, **kw)


if __name__ == "__main__":
    unittest.main()
