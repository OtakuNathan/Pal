"""Attach a parked tool batch to the current worker and finish its original turn."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from pal.bunshin.checkpoint import AgentSessionCheckpointError
from pal.bunshin.interaction_suspend import SuspendedToolBatch, WorkerInteraction
from pal.bunshin.role_gateway_client import role_gateway_client_from_env
from pal.bunshin.runner_components.control import Control
from pal.bunshin.runner_components.llm_rounds import LlmRounds
from pal.bunshin.runner_components.models import BunshinAgentLoopState, BunshinRuntimeBundle
from pal.bunshin.runner_components.session_checkpoints import SessionCheckpoints
from pal.bunshin.runner_components.tool_session import ToolSession
from pal.core.turn_executor import TurnExecutor
from pal.core.turns import ToolCallEffect, TurnContinuation


async def bind_interaction_boundary(
    *, runtime_root: Path, control: Control, llm_rounds: LlmRounds, checkpoints: SessionCheckpoints,
    bundle: BunshinRuntimeBundle, state: BunshinAgentLoopState, continuation: TurnContinuation,
    tool_session: ToolSession, executor: TurnExecutor, max_output_tokens: int,
    response_key: str, response_keys: list[str], initial_instruction: str,
    enabled: bool, parked: Mapping[str, Any] | None, semantic_input_is_new: bool,
) -> None:
    client = role_gateway_client_from_env(runtime_root) if enabled else None
    if enabled and client is None:
        raise AgentSessionCheckpointError("durable role interactions require an authenticated gateway")

    async def save_batch(batch: SuspendedToolBatch) -> int:
        if tool_session.execution_sessions is not None and tool_session.execution_sessions.resources_live:
            raise ValueError("close live execution sessions before requesting user input")
        await checkpoints.persist_agent_session_checkpoint(
            bundle, state, continuation, initial_instruction=initial_instruction, response_keys=response_keys,
            parked_batch=batch.payload(),
        )
        return int(checkpoints.agent_session_checkpoint["sequence"])

    if client is not None:
        interaction = WorkerInteraction(client, response_key, continuation, state, save_batch)
        control.user_interaction_port().exchange = interaction.exchange
    if parked:
        if client is None or semantic_input_is_new:
            raise AgentSessionCheckpointError("parked tool batch has lost its original assignment")
        batch = SuspendedToolBatch.restore(parked)
        if continuation.turn_id != batch.turn_id:
            raise AgentSessionCheckpointError("parked tool batch has lost its original turn")
        try:
            reply = await client.request("interaction_resume", {"wait_id": batch.wait_id})
        except Exception as exc:
            raise AgentSessionCheckpointError("cannot safely replay a previously resumed tool operation") from exc
        interaction.expected = [*batch.prior_answers, (batch.wait_id, reply["answer"])]
        interaction.resumed_wait_id = batch.wait_id
        continuation.pending_tool_call_batch = batch.calls
        continuation.pending_tool_results = batch.completed
        for call in batch.remaining:
            await llm_rounds.execute_bunshin_agent_effect(
                executor, continuation, state, ToolCallEffect(tool_call=call), max_output_tokens=max_output_tokens,
            )
            if interaction.expected:
                raise AgentSessionCheckpointError("resumed tool no longer requested its original authorization")
            interaction.accepted.clear()
        async def consume(sequence: int) -> None:
            await client.request("interaction_consumed", {"wait_id": batch.wait_id, "sequence": sequence})
            interaction.resumed_wait_id = ""

        # An approved tool may open a live process. Acknowledge at the next
        # closed checkpoint, after the normal loop can collect its result.
        checkpoints.on_closed_checkpoint = consume
