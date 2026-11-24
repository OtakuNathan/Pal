"""Checkpoint the accepted tool batch at a user-interaction boundary."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

from pal.shared.json_values import thaw_json
from pal.shared.tool_protocol import ToolCallIR, ToolContextMessageIR, ToolExecutionResult
from pal.bunshin.checkpoint import AgentSessionCheckpointError


if TYPE_CHECKING:
    from pal.bunshin.ipc import BunshinRoleGatewayClient
    from pal.bunshin.runner_components.models import BunshinAgentLoopState
    from pal.core.turns import TurnContinuation


class InteractionParked(BaseException):
    """Unwind through tool adapters without converting suspension to a failed result."""

    def __init__(self, wait_id: str) -> None:
        self.wait_id = wait_id
        super().__init__(wait_id)


@dataclass
class SuspendedToolBatch:
    turn_id: str
    calls: list[ToolCallIR]
    completed: list[ToolExecutionResult]
    wait_id: str
    kind: str
    prior_answers: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    @property
    def remaining(self) -> list[ToolCallIR]:
        return self.calls[len(self.completed):]

    def payload(self) -> dict[str, Any]:
        # Tool bodies and authority live in the L1 and execution snapshots. These
        # prefix records feed TurnExecutor's end-of-batch context publication.
        return {
            "turn_id": self.turn_id, "wait_id": self.wait_id, "kind": self.kind,
            "prior_answers": self.prior_answers,
            "calls": [{"call_id": c.call_id, "name": c.name, "args": c.args} for c in self.calls],
            "completed": [{"call_id": c.call_id, "name": c.name, "ok": c.ok, "llm_text": c.llm_text,
                           "context_messages": [{"content": m.content, "semantic_kind": m.semantic_kind,
                                                 "metadata": thaw_json(m.metadata), "artifact_ids": list(m.artifact_ids)}
                                                for m in c.context_messages]} for c in self.completed],
        }

    @classmethod
    def restore(cls, value: Mapping[str, Any]) -> SuspendedToolBatch:
        result = cls(str(value["turn_id"]), [ToolCallIR(**c) for c in value["calls"]],
                     [ToolExecutionResult(**{**c, "context_messages": tuple(
                         ToolContextMessageIR(**m) for m in c["context_messages"])}) for c in value["completed"]],
                     str(value["wait_id"]), str(value["kind"]),
                     [(str(key), dict(answer)) for key, answer in value.get("prior_answers", [])])
        prefix = [(c.call_id, c.name) for c in result.calls[:len(result.completed)]]
        if (not result.remaining or not result.wait_id or not result.turn_id
                or prefix != [(item.call_id, item.name) for item in result.completed]
                or len({call.call_id for call in result.calls}) != len(result.calls)):
            raise AgentSessionCheckpointError("invalid pending tool cursor")
        return result


@dataclass
class WorkerInteraction:
    """One assignment's tool-loop boundary, active only inside the built-in worker."""
    client: BunshinRoleGatewayClient
    assignment_id: str
    continuation: TurnContinuation
    state: BunshinAgentLoopState
    save: Callable[[SuspendedToolBatch], Awaitable[int]]
    expected: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    accepted: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    resumed_wait_id: str = ""

    async def exchange(self, kind: str, request: dict[str, Any]) -> dict[str, Any]:
        from pal.bunshin.role_protocol import stable_hash
        calls = self.continuation.pending_tool_call_batch
        completed = self.continuation.pending_tool_results
        if len(completed) >= len(calls):
            raise ValueError("user interaction requires an accepted tool call")
        call = calls[len(completed)]
        identity = [self.assignment_id, self.continuation.turn_id, call.call_id, call.name, call.args, kind]
        semantic = {k: v for k, v in request.items() if k not in
                    {"approval_id", "clarification_id", "invocation_id", "run_id", "bunshin_id"}}
        wait_id = kind + "_" + stable_hash({"identity": identity, "request": semantic, "ordinal": len(self.accepted)})
        if self.expected:
            if self.expected[0][0] != wait_id:
                raise AgentSessionCheckpointError("resumed tool requested different authorization")
            _, answer = self.expected.pop(0)
            self.accepted.append((wait_id, answer))
            return answer
        live = self.state.memory_service.active_l1_turn(self.continuation.turn_id)
        if live is None or live.pending_call_ids != frozenset(c.call_id for c in calls[len(completed):]):
            raise ValueError("user interaction requires a recoverable tool cursor")
        request[kind + "_id"] = wait_id
        batch = SuspendedToolBatch(self.continuation.turn_id, calls, completed, wait_id, kind, list(self.accepted))
        sequence = await self.save(batch)
        try:
            await self.client.request("interaction_park", {"wait_id": wait_id, "kind": kind,
                                                          "request": request, "sequence": sequence,
                                                          "previous_wait_id": self.resumed_wait_id})
        except Exception as exc:
            # The Manager may have committed even when its reply was lost.
            raise InteractionParked(wait_id) from exc
        raise InteractionParked(wait_id)
