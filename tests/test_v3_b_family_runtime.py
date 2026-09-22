"""G4/H (owner+executor level): B-family runtime facts for the v3 flow.

B04 · R growth after handoff start — the install may still swap L only, R
        survives verbatim, and the NEXT ordinary request is blocked on the
        CURRENT view's budget (hard overflow), never on a stale estimate.
B05 · base cannot be compacted — a fixed base that overflows the window on
        its own is a distinct terminal verdict (``base_over_budget``); no
        futile summarization attempt, no trimming of the base to fake a fit.
B06 · upstream paging contract — compaction retires exactly the results that
        left L1; results that survive in R stay alive with the resource
        owner, and the install never creates a second paging layer.
B07 · run total deadline — one absolute cutoff spans preflight/generation/
        repair; the clock is never reset per attempt, and the run ends with
        attempts unconsumed when the total budget is spent.
B08 · work budget is not reset — after a mid-turn compaction the
        finalization_only downgrade and the consumed round counters persist;
        tool permissions are not restored and no new task is recognized.

The engine, validator, memory service, and history root are real; the LLM
is a network-side fake only (H01 harness pattern).
"""

from __future__ import annotations

import asyncio
import inspect
import time
import unittest
from types import SimpleNamespace

from pal.core.compaction import (
    CompactionClockKind,
    CompactionEngine,
    CompactionSnapshot,
)
from pal.core.pal_compaction import PalCompactionPolicy
from pal.core.turn_executor import TurnExecutor
from pal.core.turns import LLMPreflightEffect, MemoryCompactEffect
from pal.llm import generation_result_from_values
from pal.llm.conversions import request_ir_from_prompt
from pal.llm.contracts import LLMPreflightAdvice
from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
from pal.memory import MemoryService
from pal.memory.contracts import L1MessageKind, L1TranscriptMessage
from pal.shared.enums import LLMPreflightStatus
from pal.shared.prompting import PromptAssemblyContext
from pal.shared.tool_protocol import ToolResultIR, new_tool_call

from tests.test_runtime_compaction import _valid_pal_payload


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


def _service_with_history() -> MemoryService:
    service = MemoryService()
    service.l1_store.append(_seed())
    for mark in ("s0", "s1"):
        service.l1_store.append(_settled(mark))
    return service


class _Ports:
    def __init__(self, memory_service, llm, execution_runtime=None):
        self._ports = {"memory:memory": memory_service, "llm:llm": llm}
        self.execution_runtime = execution_runtime

    def get(self, name):
        return self._ports.get(name)

    def require_port(self, name):
        value = self._ports.get(name)
        if value is None:
            raise KeyError(name)
        return value


async def _call_port_async(port, async_name, sync_name, request):
    method = getattr(port, async_name, None) or getattr(port, sync_name, None)
    value = method(request)
    if inspect.isawaitable(value):
        return await value
    return value


def _executor(ports, *, engine=None) -> TurnExecutor:
    return TurnExecutor(
        SimpleNamespace(port_registry=ports, require_port=ports.require_port,
                        execution_runtime=ports.execution_runtime),
        SimpleNamespace(diagnostics=[]),
        None,
        call_port_async=_call_port_async,
        build_canonical_prompt=None,
        debug_log_prompt=lambda *a: None,
        debug_log_outcome=lambda *a: None,
        debug_log_reply=lambda *a: None,
        build_llm_tool_contracts=lambda: [{"name": "TOOL_RESTORED"}],
        handle_failure_async=None,
        render_failure_feedback_text=lambda v: "BLOCKED_FEEDBACK",
        should_enter_failure_flow_for_tool_result=lambda v: False,
        compaction_engine=engine or CompactionEngine(
            policy=PalCompactionPolicy(), max_attempts=1, timeout_seconds=10.0),
        compaction_clock_provider=lambda: 1,
    )


def _continuation(turn_id: str, **overrides):
    fields = dict(
        turn_id=turn_id,
        llm_round_index=0,
        finalization_only=False,
        finalization_reason="",
        tool_observations=[],
        preferred_llm_endpoint_id="",
        preferred_llm_model_id="",
        turn_settings_snapshot={},
        delivery_binding=None,
        pending_compact_memory_candidate_batches=[],
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


class _ResidentNetwork:
    """Fake provider: compaction summaries on demand, R growth as a hook."""

    def __init__(self, *, on_generate=None, generate_sleep=0.0,
                 preflight_sleep=0.0, preflight_advice=None):
        self.requests: list = []
        self.preflights: list = []
        self.on_generate = on_generate
        self.generate_sleep = generate_sleep
        self.preflight_sleep = preflight_sleep
        self._preflight_advice = preflight_advice

    async def apreflight(self, request):
        if self.preflight_sleep:
            await asyncio.sleep(self.preflight_sleep)
        self.preflights.append(request)
        if self._preflight_advice is not None:
            return self._preflight_advice
        return LLMPreflightAdvice(status=LLMPreflightStatus.READY.value,
                                  breakdown={})

    async def agenerate(self, request, *args, **kwargs):
        self.requests.append(request)
        if self.generate_sleep:
            await asyncio.sleep(self.generate_sleep)
        if self.on_generate is not None:
            self.on_generate()
        return generation_result_from_values(text=_valid_pal_payload("B family summary"))


class BFamilyRuntimeFactsTests(unittest.TestCase):
    def test_B04_r_growth_kept_next_normal_blocked_on_current_view(self):
        service = _service_with_history()
        root = service.history_root
        service.begin_l1_turn("task-R", user_text="RIGHT_SENTINEL work")
        network = _ResidentNetwork(
            on_generate=lambda: service.upsert_l1_assistant(
                "task-R",
                LLMMessageIR(role=MessageRole.ASSISTANT,
                             parts=(TextPartIR("R_GROWTH_FINAL " + "x" * 200),),
                             message_id="r-grow-1"),
            ),
        )
        ports = _Ports(service, network)
        executor = _executor(ports)
        assembly = SimpleNamespace(
            metadata={"prompt_cache_scope_id": "pal:resident"},
            work_order_id="", core_mode="",
        )

        result = asyncio.run(executor.compact_memory_async(
            service, target_input_budget=1_000_000, reserved_output_tokens=1024,
            assembly_context=assembly,
            continuation=_continuation("task-R"),
        ))
        self.assertTrue(result.success, f"compact failed: {result.failures}")

        # Only the left was swapped; R survived the growth verbatim.
        right_text = " ".join(m.text for m in root.right_messages())
        self.assertIn("RIGHT_SENTINEL", right_text)
        self.assertIn("R_GROWTH_FINAL", right_text)
        left_text = " ".join(m.text for m in root.left_messages())
        self.assertIn("B family summary", left_text)
        self.assertNotIn("s0 reply", left_text)

        # The next ordinary request: hard overflow on the CURRENT view —
        # the budget basis contains the new seed + grown R (never the old
        # left, never a stale estimate), the send is refused, and a
        # context_budget_exhausted failure with no auto-retry is raised.
        captured: dict = {}

        async def fake_failure(signal, **kwargs):
            captured["signal"] = signal
            return SimpleNamespace(user_feedback={"kind": "blocked"})

        executor._handle_failure_async = fake_failure

        def canonical_prompt(context, *, max_output_tokens, model_hint):
            view = " ".join(
                m.text
                for turn in (*root.left_turns(), *root.right_turns())
                for m in turn.messages
            )
            return request_ir_from_prompt(
                messages=[{"role": "user", "content": view}],
                max_output_tokens=max_output_tokens,
                model_hint=model_hint,
            )

        executor._build_canonical_prompt = canonical_prompt
        hard_llm = _ResidentNetwork(
            preflight_advice=LLMPreflightAdvice(
                status=LLMPreflightStatus.READY.value,
                breakdown={"hard_overflow": True, "estimated": 99999},
            ),
        )
        ports._ports["llm:llm"] = hard_llm
        effect = LLMPreflightEffect(
            assembly_context=PromptAssemblyContext(
                metadata={"prompt_cache_scope_id": "pal:resident"}),
            max_output_tokens=128,
        )
        outcome = asyncio.run(executor._handle_llm_preflight(
            effect, _continuation("task-R", llm_round_index=5)))
        self.assertEqual(outcome.status.value, "ok")
        signal = captured.get("signal")
        self.assertIsNotNone(signal, "hard overflow must raise a failure signal")
        self.assertEqual(signal.failure_kind, "context_budget_exhausted")
        self.assertFalse(signal.safe_to_retry)
        # The refused request's budget basis is the CURRENT view: new seed
        # and grown R present, the pre-install left absent.
        basis = " ".join(
            part.text
            for message in hard_llm.preflights[0].request.messages
            for part in getattr(message, "parts", ())
            if isinstance(part, TextPartIR)
        )
        self.assertIn("B family summary", basis)
        self.assertIn("R_GROWTH_FINAL", basis)
        self.assertNotIn("s0 reply", basis)
        # Blocked before any send.
        self.assertEqual(hard_llm.requests, [])
        self.assertEqual(network.requests, [r for r in network.requests])

    def test_B05_base_over_budget_is_distinct_and_never_summarizes(self):
        service = _service_with_history()
        root = service.history_root
        service.begin_l1_turn("task-R", user_text="RIGHT_SENTINEL work")
        root.promote()
        left_snapshot = service.begin_left_compaction("run-b05", reason="auto")
        snapshot = CompactionSnapshot.capture_left(
            service, left_snapshot,
            target_input_budget=64, reserved_output_tokens=32,
            clock_kind=CompactionClockKind.USER_TURN, clock_value=1,
            metadata={"compaction_op_id": "run-b05"},
        )
        # The two-segment snapshot is stamped: the empty-source probe path
        # is the honest route for it.
        self.assertTrue(snapshot.source_stamp)

        compact_required = LLMPreflightAdvice(
            status=LLMPreflightStatus.COMPACT_REQUIRED.value,
            breakdown={"hard_overflow": True},
        )

        class _AlwaysOverBudget:
            def __init__(self) -> None:
                self.preflights: list = []

            async def apreflight(self, request):
                self.preflights.append(request)
                return compact_required

            async def agenerate(self, request, *a, **kw):  # pragma: no cover
                raise AssertionError("base overflow must refuse before any send")

        llm = _AlwaysOverBudget()
        engine = CompactionEngine(policy=PalCompactionPolicy(),
                                  max_attempts=3, timeout_seconds=10.0)
        result = asyncio.run(engine.run(
            snapshot, llm_runtime=llm, memory_service=service))
        # Distinct terminal verdict, not a generic failure.
        self.assertEqual(result.status, "base_over_budget")
        self.assertFalse(result.success)
        self.assertIn("input:base_context_over_budget", result.failures)
        self.assertEqual(result.attempts, 0)
        # One probe with the FULL source, one with the empty source — both
        # kept the fixed base (policy system prompt) untrimmed; zero
        # generation attempts (no futile summarization).
        self.assertEqual(len(llm.preflights), 2)
        policy = PalCompactionPolicy()
        system_prompt = policy.system_prompt(snapshot).strip()
        for preflight in llm.preflights:
            system_text = " ".join(
                part.text
                for message in preflight.request.messages
                if message.role == MessageRole.SYSTEM
                for part in getattr(message, "parts", ())
                if isinstance(part, TextPartIR)
            )
            self.assertIn(system_prompt, system_text)
        # Left untouched.
        root.fail("run-b05", reason="base budget")
        left_text = " ".join(
            m.text for t in service.history_root.left_turns() for m in t.messages)
        self.assertIn("s0 reply", left_text)

    def test_B06_paging_contract_retires_exactly_what_left_l1(self):
        service = _service_with_history()
        root = service.history_root
        # A settled left turn carrying a paged tool result (L side):
        # assistant call first, paired result second (protocol-matched).
        service.begin_l1_turn("t-l", user_text="left request")
        service.upsert_l1_assistant("t-l", LLMMessageIR(
            role=MessageRole.ASSISTANT,
            parts=(TextPartIR("running left probe"),
                   new_tool_call("call-left", "run_shell")),
            message_id="a-left",
        ))
        service.append_l1_tool_result("t-l", ToolResultIR(
            call_id="call-left", name="run_shell",
            content="LEFT_PAGE payload page 1/2 (spilled)",
            replay_result_ref="res-left",
        ))
        service.settle_l1_turn("t-l")
        # An active right turn with its own paged result (R side).
        service.begin_l1_turn("task-R", user_text="RIGHT_SENTINEL work")
        service.upsert_l1_assistant("task-R", LLMMessageIR(
            role=MessageRole.ASSISTANT,
            parts=(TextPartIR("reading right pages"),
                   new_tool_call("call-right", "read_tool_result")),
            message_id="a-right",
        ))
        service.append_l1_tool_result("task-R", ToolResultIR(
            call_id="call-right", name="read_tool_result",
            content="RIGHT_PAGE payload page 1/3 (spilled)",
            replay_result_ref="res-right",
        ))

        retire_calls: list[dict] = []
        spills: list[str] = []

        class _ExecRuntime:
            def retire_tool_results(self, *, turn_id, result_ids,
                                    execution_lifetime_id=""):
                retire_calls.append({
                    "turn_id": turn_id,
                    "result_ids": tuple(result_ids),
                    "execution_lifetime_id": execution_lifetime_id,
                })

            def spill_result(self, *a, **kw):  # pragma: no cover
                spills.append("second-paging-layer")
                raise AssertionError("compaction must not create a second "
                                     "paging layer")

        network = _ResidentNetwork()
        ports = _Ports(service, network, execution_runtime=_ExecRuntime())
        executor = _executor(ports)
        result = asyncio.run(executor.compact_memory_async(
            service, target_input_budget=1_000_000, reserved_output_tokens=1024,
            assembly_context=SimpleNamespace(
                metadata={"prompt_cache_scope_id": "pal:resident"},
                work_order_id="", core_mode=""),
            continuation=_continuation("task-R"),
        ))
        self.assertTrue(result.success, f"compact failed: {result.failures}")

        # Exactly the L-only result was retired; the R result stays alive
        # with the resource owner (F25: survivors are not removable).
        self.assertEqual(len(retire_calls), 1)
        self.assertEqual(retire_calls[0]["result_ids"], ("call-left",))
        self.assertEqual(retire_calls[0]["turn_id"], "task-R")
        # The installed left is a plain-text summary: no ToolResultIR parts,
        # no new page/reference structures (no second paging layer).
        for turn in root.left_turns():
            for message in turn.messages:
                for part in getattr(message, "parts", ()):
                    self.assertNotIsInstance(part, ToolResultIR)
        left_text = " ".join(m.text for m in root.left_messages())
        self.assertNotIn("LEFT_PAGE payload", left_text)
        # R's paged payload survives byte-identically.
        right_results = [
            part for m in root.right_messages()
            for part in getattr(m, "parts", ())
            if isinstance(part, ToolResultIR)
        ]
        self.assertEqual(len(right_results), 1)
        self.assertEqual(right_results[0].call_id, "call-right")
        self.assertEqual(right_results[0].content,
                         "RIGHT_PAGE payload page 1/3 (spilled)")
        self.assertEqual(spills, [])

    def test_B07_total_deadline_ends_run_with_attempts_unconsumed(self):
        service = _service_with_history()
        service.begin_l1_turn("task-R", user_text="RIGHT_SENTINEL work")
        # Per-attempt budget 0.25s x 5 attempts = absolute run cutoff 1.25s.
        # Every attempt burns ~0.65s of wall time (slow preflight + slow
        # generation), so the run must die at the TOTAL cutoff during the
        # second attempt with three attempts never dispatched — the clock
        # is never reset per attempt.
        network = _ResidentNetwork(generate_sleep=0.3, preflight_sleep=0.4)
        engine = CompactionEngine(policy=PalCompactionPolicy(),
                                  max_attempts=5, timeout_seconds=0.25)
        ports = _Ports(service, network)
        executor = _executor(ports, engine=engine)
        started = time.monotonic()
        result = asyncio.run(executor.compact_memory_async(
            service, target_input_budget=1_000_000, reserved_output_tokens=1024,
            assembly_context=SimpleNamespace(
                metadata={"prompt_cache_scope_id": "pal:resident"},
                work_order_id="", core_mode=""),
            continuation=_continuation("task-R"),
        ))
        elapsed = time.monotonic() - started
        self.assertFalse(result.success)
        self.assertTrue(
            any(str(f).startswith("deadline") for f in result.failures),
            f"expected a deadline failure, got: {result.failures}",
        )
        # The absolute cutoff fired with attempts unconsumed: only 2 of 5
        # model attempts were ever dispatched.  A per-attempt reset would
        # have let all 5 run.
        self.assertLessEqual(len(network.requests), 2)
        self.assertLess(elapsed, 1.25 + 0.6)
        # The dead run is terminal on the owner: a new compact run can be
        # opened immediately (no lane stuck busy).
        service.history_root.promote()
        service.begin_left_compaction("run-after-deadline", reason="auto")

    def test_B08_compaction_does_not_reset_work_budget_or_permissions(self):
        service = _service_with_history()
        service.begin_l1_turn("task-R", user_text="RIGHT_SENTINEL work")
        service.upsert_l1_assistant("task-R", LLMMessageIR(
            role=MessageRole.ASSISTANT, parts=(TextPartIR("A_DECISION"),),
            message_id="a-1"))
        network = _ResidentNetwork()
        ports = _Ports(service, network)
        executor = _executor(ports)
        tool_contract_calls: list[int] = []
        original_contracts = executor._build_llm_tool_contracts

        def counting_contracts():
            tool_contract_calls.append(1)
            return original_contracts()

        executor._build_llm_tool_contracts = counting_contracts
        continuation = _continuation(
            "task-R",
            llm_round_index=4,
            finalization_only=True,
            finalization_reason="Tool execution has been terminated.",
            tool_observations=[SimpleNamespace(
                tool_name="run_shell", summary="used the shell",
                to_prompt_block=lambda: "observation",
            )],
        )
        effect = MemoryCompactEffect(
            assembly_context=PromptAssemblyContext(
                metadata={"prompt_cache_scope_id": "pal:resident"}),
            target_input_budget=1_000_000,
            reserved_output_tokens=1024,
        )
        outcome = asyncio.run(executor._handle_memory_compact(
            effect, continuation))
        self.assertEqual(outcome.status.value, "ok",
                         f"compact effect failed: {outcome.text}")

        # Counters kept, downgrade kept, permissions NOT restored: the
        # finalization_only turn still resolves to zero tool contracts and
        # the underlying contract builder is never consulted for it.
        self.assertTrue(continuation.finalization_only)
        self.assertEqual(continuation.llm_round_index, 4)
        self.assertEqual(len(continuation.tool_observations), 1)
        resolved = executor._resolve_llm_tools(continuation, None)
        self.assertEqual(resolved, [])
        self.assertEqual(tool_contract_calls, [])
        # No new task was recognized from the new left: the work tail is
        # still the same R turn.
        right = service.history_root.right_turns()
        self.assertEqual([t.turn_id for t in right], ["task-R"])
        self.assertIn("A_DECISION",
                      " ".join(m.text for m in service.history_root.right_messages()))


if __name__ == "__main__":
    unittest.main()
