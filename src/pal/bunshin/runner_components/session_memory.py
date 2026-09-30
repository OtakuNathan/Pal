from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping
from pal.memory import MemoryService
from pal.memory.turn_ir import L1TurnState


@dataclass
class SessionMemory:


    @staticmethod
    def resume_or_reopen_l1_turn(
        memory_service: MemoryService,
        *,
        run_id: str,
        active_input_id: str,
        user_text: str,
        fencing_token: int,
        reuse_active: bool = True,
    ) -> str:
        """Return the resumable turn, reopening a closed retry checkpoint.

        A failed native worker may have persisted a terminal checkpoint just
        before the manager observed its process failure.  Reusing that closed
        turn id would make the next worker's first LLM delta a late update.
        Keep the closed turn as history and create one deterministic active
        recovery turn for the new fenced attempt instead.
        """
        prefix = f"{run_id}:invocation:"
        active = [
            turn
            for turn in memory_service.history.turns
            if turn.state == L1TurnState.ACTIVE and turn.turn_id.startswith(prefix)
        ]
        if active and reuse_active:
            return max(active, key=lambda turn: int(turn.revision)).turn_id

        base = f"{run_id}:invocation:{active_input_id}"
        existing = memory_service.history.get(base)
        if existing is None or existing.state == L1TurnState.ACTIVE:
            return base

        token = max(1, int(fencing_token or 0))
        suffix = 0
        while True:
            recovery_id = f"{base}:recovery:{token}" if suffix == 0 else f"{base}:recovery:{token}:{suffix}"
            if memory_service.history.get(recovery_id) is None:
                memory_service.begin_l1_turn(
                    recovery_id,
                    user_text=str(user_text or ""),
                    metadata={
                        "_pal_input_id": active_input_id,
                        "recovery_of": base,
                    },
                )
                return recovery_id
            suffix += 1

    @staticmethod
    def abort_stale_l1_turns(
        memory_service: MemoryService,
        *,
        active_turn_id: str,
    ) -> None:
        """Close orphaned active turns left by a failed native worker attempt.

        A logical Bunshin session may restore one active L1 turn.  A second
        active turn is necessarily a stale process-owned turn (for example a
        worker that crashed after all tool results were recorded but before
        terminal settlement).  Abort it before starting/resuming the current
        turn so checkpoint recovery cannot accumulate parallel active L1
        protocols.
        """
        for turn in tuple(memory_service.history.turns):
            if str(turn.state) != L1TurnState.ACTIVE or turn.turn_id == active_turn_id:
                continue
            try:
                memory_service.abort_l1_turn(
                    turn.turn_id,
                    reason="stale active turn recovered before a new Bunshin attempt",
                )
            except Exception:
                # The current turn remains authoritative; a malformed stale
                # record must not prevent the worker from reaching its own
                # durable protocol boundary.
                continue

    @staticmethod
    def new_session_response_message(
        restored: Mapping[str, Any],
        *,
        response_key: str,
        response_text: str,
    ) -> str:
        """Return one explicit user turn for a new Manager-bound semantic input.

        Instruction text is intentionally reusable across repair cycles.  The
        durable assignment key, rather than text equality or an outbox effect
        key, determines whether this input has already entered the session.
        """

        key = str(response_key or "").strip()
        seen = {
            str(item)
            for item in list(restored.get("response_keys") or [])
            if str(item)
        }
        text = str(response_text or "").strip()
        if not key or key in seen or not text:
            return ""
        return (
            "# New Manager-Bound Role Input\n\n"
            "This is a new semantic assignment for the durable role session. "
            "Any earlier completion summary or submit action settled only an earlier input; "
            "it does not complete this one.\n\n"
            + text
        )
