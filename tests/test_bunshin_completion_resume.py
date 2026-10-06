"""Real session-loop coverage for in-process completion feedback and restarts."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pal.bunshin.checkpoint import (
    AgentSessionCheckpointError,
    LogicalCoroutineCheckpointStore,
    seal_agent_session_checkpoint,
)
from pal.bunshin.runner import BunshinRunner
from pal.bunshin.runner_components.models import BunshinRuntimeBundle
from pal.core.main_context import MainContext
from pal.core.runtime_state import RuntimeSnapshotCoordinator
from pal.execution.capabilities import register_with_core as register_execution
from pal.llm import generation_result_from_values
from pal.memory import MemoryService
from pal.memory.capabilities import register_with_core as register_memory
from pal.shared import BunshinInvocationPack
from tests.llm_fakes import NonStreamingLLM


class SyntheticModel(NonStreamingLLM):
    def __init__(self):
        self.calls = 0

    def generate(self, request, **options):
        self.calls += 1
        return generation_result_from_values(
            text=f"local synthetic answer {self.calls}", finish_reason="stop"
        )


async def noop(*args, **kwargs):
    return None


def make_bundle():
    context = MainContext()
    memory = MemoryService()
    register_execution(context)
    register_memory(context, memory)
    return BunshinRuntimeBundle(
        SyntheticModel(), context.execution_runtime, memory,
        context.module_registry, RuntimeSnapshotCoordinator(context.module_registry),
    )


def make_runner(root, *, output, restore=None, token=1, write_event=noop):
    session = {
        "session_id": "resume-session", "workflow_id": "resume-workflow",
        "stage_key": "module:test:verifier", "response_key": "assignment-3",
        "fencing_token": token, "continuation_output_path": str(output),
    }
    if restore is not None:
        session["continuation_input_path"] = str(restore)
    pack = BunshinInvocationPack(
        invocation_id="resume-session", instruction="Return a synthetic answer.",
        workspace={
            "run_dir": str(root),
            "output_policy": {"primary_artifact": "expected.json"},
        },
        metadata={"agent_session": session},
    )
    return BunshinRunner(
        runtime_root=root, pack=pack, bunshin_id=pack.invocation_id,
        run_id="resume-attempt", write_event=write_event, read_decision=noop,
    )


async def seed_checkpoint(root, bundle):
    path = root / "input.json"
    runner = make_runner(root, output=path)
    state = SimpleNamespace(
        llm_round_count=39, tool_call_count=66, memory_service=bundle.memory_service,
        memory_candidate_sink=SimpleNamespace(records=[]),
    )
    continuation = SimpleNamespace(
        pending_tool_call_batch=[], pending_tool_results=[], tool_batch_count=0,
        preferred_llm_endpoint_id="", preferred_llm_model_id="",
    )
    await runner.components.session_checkpoints.persist_agent_session_checkpoint(
        bundle, state, continuation, initial_instruction=runner.pack.instruction,
        response_keys=["assignment-3"],
    )
    return path, runner.components.session_checkpoints.agent_session_checkpoint


@pytest.mark.parametrize("resumed", [False, True])
def test_real_completion_retry_preserves_memory_rounds_and_sequence(tmp_path, resumed):
    async def scenario():
        bundle = make_bundle()
        events, checkpoints = [], []
        output = tmp_path / "output.json"

        async def record(event):
            events.append(event)
            if event.get("payload", {}).get("phase") in {
                "completion_gate_rejected", "completion_gate_stalled",
            }:
                checkpoints.append(json.loads(output.read_text()))

        try:
            restore = (await seed_checkpoint(tmp_path, bundle))[0] if resumed else None
            runner = make_runner(
                tmp_path, output=output, restore=restore, token=2, write_event=record,
            )
            # Only the progress lookup and model are synthetic. Both loop passes,
            # memory settlement, snapshot restoration, and encryption are real.
            with patch.object(
                runner.components.completion, "completion_gate_progress_marker",
                return_value="unchanged",
            ):
                await runner.components.invocation.run_v2_invocation(bundle)
            rounds = [
                event["payload"]["round"] for event in events
                if event.get("payload", {}).get("phase") == "llm_round_completed"
            ]
            assert bundle.llm_runtime.calls == 2
            assert rounds == ([40, 41] if resumed else [1, 2])
            assert len(checkpoints) == 2
            assert checkpoints[1]["sequence"] > checkpoints[0]["sequence"]
            assert all(value["producer_fencing_token"] == 2 for value in checkpoints)
            assert "local synthetic answer 1" in str(bundle.memory_service.history.turns)
            assert "local synthetic answer 2" in str(bundle.memory_service.history.turns)
            assert runner.components.status.blocked_kind == "completion_gate_stalled"

            # A fresh process must use the manager's selected input, not the
            # newer output lying beside it or another runner's in-memory state.
            if resumed:
                fresh = make_runner(tmp_path, output=output, restore=restore, token=3)
                restored = await fresh.components.agent_session.restore_runtime(
                    bundle, "resume-session", fresh.pack.workspace,
                )
                assert restored["coroutine_state"]["llm_round_count"] == 39
                assert restored["sequence"] == 1
                assert "local synthetic answer 1" not in str(bundle.memory_service.history.turns)
                store = LogicalCoroutineCheckpointStore(tmp_path)
                with pytest.raises(AgentSessionCheckpointError, match="stale fencing"):
                    store.publish(
                        checkpoints[-1], expected_logical_coroutine_id="resume-session",
                        current_fencing_token=3,
                    )
        finally:
            bundle.execution_runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("defect", [
    "missing", "ciphertext", "logical_coroutine_id", "workflow_id", "stage_key",
    "runtime_spec_hash",
])
def test_fresh_process_rejects_invalid_manager_selected_checkpoint(tmp_path, defect):
    async def scenario():
        bundle = make_bundle()
        try:
            path, private = await seed_checkpoint(tmp_path, bundle)
            if defect == "missing":
                path.unlink()
            elif defect == "ciphertext":
                envelope = json.loads(path.read_text())
                envelope["ciphertext"] = envelope["ciphertext"][:-2] + "aa"
                path.write_text(json.dumps(envelope))
            else:
                private[defect] = "wrong-identity"
                private["runtime_snapshot"][defect] = "wrong-identity"
                path.write_text(json.dumps(seal_agent_session_checkpoint(tmp_path, private)))
            fresh = make_runner(tmp_path, output=tmp_path / "output.json", restore=path, token=2)
            with pytest.raises(AgentSessionCheckpointError):
                await fresh.components.agent_session.restore_runtime(
                    bundle, "resume-session", fresh.pack.workspace,
                )
            assert not fresh.components.session_checkpoints.agent_session_checkpoint
        finally:
            bundle.execution_runtime.shutdown()

    asyncio.run(scenario())


def test_in_process_retry_retains_live_runtime_without_unsafe_checkpoint(tmp_path):
    async def scenario():
        bundle = make_bundle()
        events = []

        async def record(event):
            events.append(event)

        try:
            restore, _ = await seed_checkpoint(tmp_path, bundle)
            output = tmp_path / "unsafe-output.json"
            runner = make_runner(
                tmp_path, output=output, restore=restore, token=2, write_event=record,
            )
            runner.components.tool_session.bind_execution(SimpleNamespace(
                resources_live=True, has_work=False, wait_after_response=noop,
            ))
            with patch.object(
                runner.components.completion, "completion_gate_progress_marker",
                return_value="unchanged",
            ), patch.object(
                bundle.runtime_state_coordinator, "restore",
                wraps=bundle.runtime_state_coordinator.restore,
            ) as restore_runtime:
                await runner.components.invocation.run_v2_invocation(bundle)
            assert restore_runtime.await_count == 1
            rounds = [
                event["payload"]["round"] for event in events
                if event.get("payload", {}).get("phase") == "llm_round_completed"
            ]
            assert rounds == [40, 41]
            assert not output.exists()
            assert runner.components.session_checkpoints.agent_session_checkpoint["sequence"] == 1
            assert "local synthetic answer 1" in str(bundle.memory_service.history.turns)
            assert "local synthetic answer 2" in str(bundle.memory_service.history.turns)

            other_bundle = make_bundle()
            try:
                with pytest.raises(AgentSessionCheckpointError, match="incompatible"):
                    await runner.components.agent_session.run_agent_loop(other_bundle)
            finally:
                other_bundle.execution_runtime.shutdown()
            runner.pack.metadata["agent_session"]["fencing_token"] = 3
            with pytest.raises(AgentSessionCheckpointError, match="incompatible"):
                await runner.components.agent_session.run_agent_loop(bundle)
            runner.pack.metadata["agent_session"]["fencing_token"] = 2
            handoff = await runner.components.agent_session.restore_runtime(
                bundle, "resume-session", runner.pack.workspace,
            )
            assert handoff["coroutine_state"]["llm_round_count"] == 41
            with pytest.raises(AgentSessionCheckpointError, match="no completed loop handoff"):
                await runner.components.agent_session.restore_runtime(
                    bundle, "resume-session", runner.pack.workspace,
                )
        finally:
            bundle.execution_runtime.shutdown()

    asyncio.run(scenario())
