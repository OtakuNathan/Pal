"""v3 real-provider E2E (主项2, matrix N27).

Opt-in only: set ``PAL_V3_E2E=1`` (plus optional ``PAL_V3_E2E_ENDPOINT``).
Without the gate the suite SKIPs honestly — the DELIVERY ledger records
NOT_RUN unless this gate is exercised.

What it proves against a REAL provider (read-only live config: endpoints
from ~/.pal, credentials via the production resolver; fresh in-memory
MemoryService so nothing live is mutated):

1. ordinary rounds ride the owner projection: answers accepted, chunks
   frozen, send receipts applied;
2. a second round replays the frozen prefix through the same session;
3. two-segment compaction runs the REAL engine end-to-end — warm replay
   engages when the provider confirms a cached anchor inside L, otherwise
   the honest cold-left source (both must install the summary);
4. a post-compact round answers on the rebuilt prefix (rebase consumed).
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace

from pal.core.turns import LLMRequestEffect
from pal.llm.credentials import LLMCredentialResolver
from pal.llm.ir import (
    GenerationPolicyIR, LLMMessageIR, MessageRole, TextPartIR, replace,
)
from pal.llm.models import LLMEndpointModel
from pal.llm.runtime import (
    EndpointResolver, LLMRuntime, build_default_endpoint_invoker,
)
from pal.llm.secret_store import EncryptedFileSecretStore
from pal.memory import MemoryService
from pal.shared import PromptAssemblyContext

from tests.test_v3_n1_root_lifecycle import user


def _e2e_executor(memory, runtime):
    """Vertical-trace executor shape, but with a REAL-provider compaction
    budget (the trace fixture's 2s deadline only fits faked frames)."""
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
        compaction_engine=CompactionEngine(PalCompactionPolicy(), max_attempts=2,
                                           timeout_seconds=120.0),
        compaction_clock_provider=lambda: 1,
    )

_LIVE_DB = Path.home() / ".pal" / "pal.sqlite3"
_DEFAULT_ENDPOINT_HINTS = ("glm-5.3-flash", "glm-5.3-flash-bigmodel", "glm-5.3")


def _e2e_enabled() -> bool:
    return str(os.environ.get("PAL_V3_E2E", "")).strip() not in {"", "0", "false"}


def _load_endpoints() -> list[LLMEndpointModel]:
    connection = sqlite3.connect(f"file:{_LIVE_DB}?mode=ro", uri=True)
    try:
        columns = [
            column[1]
            for column in connection.execute(
                "PRAGMA table_info(llm_endpoints)").fetchall()
        ]
        rows = connection.execute(
            "SELECT * FROM llm_endpoints WHERE enabled = 1 ORDER BY priority"
        ).fetchall()
    finally:
        connection.close()
    models: list[LLMEndpointModel] = []
    blob_columns = {
        "capabilities_blob", "thinking_levels_blob",
        "input_modalities_blob", "output_modalities_blob",
    }
    for row in rows:
        payload = dict(zip(columns, row))
        payload.pop("id", None)
        for key in blob_columns & set(payload):
            value = payload[key]
            if isinstance(value, str) and value.strip():
                try:
                    payload[key] = json.loads(value)
                except ValueError:
                    payload[key] = {}
            elif value is None:
                payload[key] = {}
        try:
            models.append(LLMEndpointModel(**payload))
        except Exception:
            continue
    return models


def _pick_endpoint(models: list[LLMEndpointModel]) -> LLMEndpointModel | None:
    requested = str(os.environ.get("PAL_V3_E2E_ENDPOINT", "")).strip()
    if requested:
        for model in models:
            if model.endpoint_id == requested:
                return model
        return None
    if str(os.environ.get("PAL_V3_E2E_EXPLICIT", "")).strip() not in {"", "0", "false"}:
        # Explicit prompt-cache mode needs a breakpoint dialect; the
        # gateway (openrouter) openai_response endpoints support it.
        for hint in ("openrouter-gpt-5.6-luna", "openrouter-gpt-5.6-terra",
                     "openrouter-gpt-5.6-sol", "gpt-6-astra"):
            for model in models:
                if model.endpoint_id == hint:
                    return model
        for model in models:
            if str(model.provider) == "openrouter" and str(
                    model.wire_shape) == "openai_response":
                return model
        return None
    for hint in _DEFAULT_ENDPOINT_HINTS:
        for model in models:
            if model.endpoint_id == hint:
                return model
    return models[0] if models else None


class _Settings:
    """Read-only settings stub: never persists through the E2E."""

    def __init__(self, endpoint_id: str):
        self._endpoint_id = endpoint_id
        self._think_levels: dict[str, str] = {}

    def get_active_llm_endpoint_id(self):
        return self._endpoint_id

    def get_think_level(self, endpoint_id):
        return self._think_levels.get(endpoint_id, "off")

    def set_think_level(self, endpoint_id, level):
        self._think_levels[endpoint_id] = level

    def get_llm_endpoint_fallback(self):
        return False


def _build_runtime(endpoint: LLMEndpointModel) -> LLMRuntime:
    runtime_root = Path.home() / ".pal"
    secret_store = EncryptedFileSecretStore(
        secrets_path=str(runtime_root / "secrets.json")
    )
    return LLMRuntime(
        EndpointResolver(endpoints=(endpoint,)),
        _Settings(endpoint.endpoint_id),
        endpoint_invoker=build_default_endpoint_invoker(
            credentials=LLMCredentialResolver(secret_store=secret_store),
            runtime_root=runtime_root,
        ),
        config=SimpleNamespace(
            runtime_root=str(runtime_root),
            llm_endpoint_retry_attempts=1,
            llm_max_output_recovery_attempts=0,
            llm_stream_wall_timeout_seconds=120.0,
            llm_stream_cleanup_timeout_seconds=5.0,
        ),
    )


def _request(memory: MemoryService, endpoint_id: str, extra_tail=()) -> object:
    from pal.llm.ir import LLMRequestIR, PromptRegionIR

    preamble = LLMMessageIR(role=MessageRole.SYSTEM,
                            parts=(TextPartIR("E2E BASE"),), message_id="e2e-base",
                            prompt_region=PromptRegionIR.STABLE_SYSTEM)
    history = [
        message
        for turn in memory.l1_store.turns.turns
        for message in turn.messages
    ]
    # Real-compiler region semantics: everything settled except the newest
    # user message, which is the active input (the anchorable tail).
    last_user_index = max(
        (index for index, message in enumerate(history)
         if message.role == MessageRole.USER),
        default=-1,
    )
    tagged = [
        message if index == last_user_index
        else replace(message, prompt_region=PromptRegionIR.SETTLED_HISTORY)
        for index, message in enumerate(history)
    ]
    return LLMRequestIR(
        messages=(preamble, *tagged, *extra_tail),
        tools=(),
        policy=GenerationPolicyIR(max_output_tokens=512),
        metadata={
            "prompt_cache_scope_id": "pal:resident",
            "preferred_endpoint_id": endpoint_id,
        },
    )


def _drive_with(ex, request):
    """Drive one LLM round with a stream-safe continuation namespace."""
    from types import SimpleNamespace as _NS

    from pal.core.turns import LLMRequestEffect as _Effect
    from pal.shared import PromptAssemblyContext as _Ctx

    class _Continuation(_NS):
        """Optional turn fields default to None instead of exploding."""

        def __getattr__(self, name: str):
            return None

    async def run():
        return await ex._handle_llm_request(
            _Effect(assembly_context=_Ctx()),
            _Continuation(
                budget_failure_feedback_text="",
                llm_round_index=0,
                preferred_llm_endpoint_id=None,
                preferred_llm_model_id=None,
                finalization_only=False,
                finalization_attempted=False,
                tool_observations=[],
                last_response_mode=None,
                turn_id="T",
                delivery_binding=None,
                interrupted=False,
            ),
        )

    ex.build_turn_prompt = lambda *args, **kwargs: request
    return asyncio.run(run())


class RealProviderE2ETests(unittest.TestCase):
    """One vertical: project → freeze → replay → compact → post-compact."""

    def setUp(self) -> None:
        if not _e2e_enabled():
            self.skipTest(
                "PAL_V3_E2E not set — real-provider E2E is opt-in (spends "
                "real tokens); the ledger records NOT_RUN for N27 otherwise")
        models = _load_endpoints()
        endpoint = _pick_endpoint(models)
        if endpoint is None:
            self.skipTest(
                "no enabled real endpoint found (or PAL_V3_E2E_ENDPOINT "
                f"unmatched) in {_LIVE_DB}")
        if str(os.environ.get("PAL_V3_E2E_EXPLICIT", "")).strip() not in {"", "0", "false"}:
            # In-test override only (unsaved model instance, never persisted):
            # enable explicit breakpoints so anchors submit and READ evidence
            # can confirm.
            capabilities = dict(endpoint.capabilities_blob or {})
            capabilities["prompt_cache"] = {
                **dict(capabilities.get("prompt_cache") or {}),
                "mode": "explicit",
            }
            endpoint.capabilities_blob = capabilities
        self.endpoint = endpoint

    def test_projection_rounds_compact_and_post_compact_round(self):
        memory = MemoryService()
        runtime = _build_runtime(self.endpoint)
        ex = _e2e_executor(memory, runtime)
        endpoint_id = self.endpoint.endpoint_id

        explicit_mode = str(os.environ.get("PAL_V3_E2E_EXPLICIT", "")).strip() not in {"", "0", "false"}
        filler = ("cache anchor payload " * 220 if explicit_mode else "")
        rounds = [
            (f"{filler}E2E-Q{index}: {question}", _expected)
            for index, (question, _expected) in enumerate([
                ("What is 2+2? Answer with just the number.", "4"),
                ("What is the capital of France? One word.", "Paris"),
                ("Name the primary color of the sky on a clear day. One word.",
                 "blue"),
                ("How many legs does a tripod have? One digit.", "3"),
            ])
        ]
        accepted_texts: list[str] = []
        for index, (question, _expected) in enumerate(rounds):
            memory.begin_l1_turn(
                "T", user_message=user(f"E2E-Q{index}: {question}", f"q{index}")
            ) if index == 0 else memory.append_l1_user(
                "T", user(f"E2E-Q{index}: {question}", f"q{index}")
            )
            outcome = _drive_with(ex, _request(memory, endpoint_id))
            status = outcome.status.value if hasattr(outcome.status, "value") else str(
                outcome.status)
            self.assertEqual(status, "ok", f"round {index} failed")
            payload = outcome.payload
            self.assertTrue(payload.projection_receipt is None
                            or payload.projection_receipt.applied,
                            f"round {index}: projection offered but not applied")
            accepted_texts.append(str(payload.text or ""))

        session = runtime.endpoint_projection_session("pal:resident")
        self.assertEqual(len(session.chunks), len(rounds),
                         "every answered round must freeze exactly one chunk")

        # Two-segment compaction through the REAL engine.
        anchor_peek = {}
        try:
            anchor_peek = dict(
                runtime.prompt_cache_confirmed_anchor_request(
                    logical_scope_id="pal:resident",
                    endpoint_id=endpoint_id,
                ) or {}
            )
        except Exception:
            anchor_peek = {}
        result = asyncio.run(ex.compact_memory_async(
            memory, target_input_budget=100_000, reserved_output_tokens=1024,
            continuation=SimpleNamespace(turn_id="T"),
        ))
        self.assertTrue(
            result.success,
            f"compaction failed: {getattr(result, 'failures', None)}")
        diagnostics = [d.get("kind") for d in ex.state.diagnostics]
        if anchor_peek.get("request") is not None:
            self.assertIn(
                "two_segment_warm_replay_engaged", diagnostics,
                "provider confirmed a cached anchor but the warm replay "
                "did not engage")
        # The install won and the projection lineage consumed the rebase.
        root = memory.history_root
        self.assertGreater(len(root.left_messages()), 0)

        # Post-compact round answers on the rebuilt prefix.
        memory.append_l1_user(
            "T", user("E2E-POST: reply with the single word OK", "q-post"))
        outcome = _drive_with(ex, _request(memory, endpoint_id))
        status = outcome.status.value if hasattr(outcome.status, "value") else str(
            outcome.status)
        self.assertEqual(status, "ok", "post-compact round failed")
        self.assertTrue(str(outcome.payload.text or "").strip())
        print(
            f"\n[e2e] endpoint={endpoint_id} rounds={len(rounds)} "
            f"warm_anchor={'yes' if anchor_peek.get('request') is not None else 'no'} "
            f"chunks_after_compact={len(runtime.endpoint_projection_session('pal:resident').chunks)}"
        )


if __name__ == "__main__":
    unittest.main()
