from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any

from pal.foundation import utc_now
from pal.minion.contracts import SERIAL_MILESTONE_MODES
from pal.minion.turns import apply_minion_turn_to_pack


@dataclass
class SerialMilestoneScheduler:
    manager: Any

    def schedule(self, state: Any, event: dict[str, Any]) -> None:
        payload = dict(event.get("payload") or {})
        if str(payload.get("status") or "").strip().lower() != "completed":
            return
        metadata = dict(state.pack.metadata or {})
        module_execution = dict(metadata.get("module_execution") or {})
        if str(module_execution.get("mode") or "") not in SERIAL_MILESTONE_MODES:
            return
        if not bool(module_execution.get("auto_advance")):
            return
        work_order_id = str(event.get("work_order_id") or state.pack.work_order_id)
        milestone_index = str(payload.get("milestone_index") if payload.get("milestone_index") is not None else "")
        inflight_key = f"{work_order_id}:{milestone_index or state.run_id}"
        if not work_order_id or not self.manager._serial_turns_inflight.claim(inflight_key):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.manager._serial_turns_inflight.release(inflight_key)
            return
        loop.create_task(
            self._send_serial_module_turn(state, event, inflight_key),
            name=f"minion-serial-turn-{work_order_id}-{milestone_index or 'next'}",
        )

    async def _send_serial_module_turn(self, state: Any, event: dict[str, Any], inflight_key: str) -> None:
        manager = self.manager
        work_order_id = str(event.get("work_order_id") or state.pack.work_order_id)
        payload = dict(event.get("payload") or {})
        try:
            turn = manager.tasking_repository.next_serial_module_turn(work_order_id)
            if turn is not None:
                state.pack = apply_minion_turn_to_pack(state.pack, turn, checkpoint_payload=payload)
                with contextlib.suppress(Exception):
                    manager.tasking_repository.update_work_order_workspace(work_order_id, dict(state.pack.workspace))
                await manager._send_runner_control_or_record(state, {"type": "next_turn", "turn": turn})
                manager.logger.info(
                    "minion serial module sent next turn run=%s work_order=%s milestone=%s",
                    state.run_id,
                    work_order_id,
                    str((turn.get("current_milestone") or {}).get("milestone_id") or ""),
                )
                return
            completion = manager.tasking_repository.mark_serial_module_completed(work_order_id)
            event_payload: dict[str, Any] = {**payload, **dict(completion)}
            event_work_order_id = work_order_id
            if str(completion.get("status") or "") == "completed":
                parent_completion = manager.tasking_repository.record_plan_module_completion(work_order_id, completion)
                if str(parent_completion.get("status") or "") in {"awaiting_continue", "completed"}:
                    event_work_order_id = str(parent_completion.get("parent_work_order_id") or work_order_id)
                    event_payload = {**completion, **parent_completion}
            elif str(completion.get("status") or "") == "already_completed":
                event_payload = {**completion, "summary": completion.get("summary") or "serial module was already completed"}
            if str(completion.get("status") or "") in {"completed", "already_completed"}:
                module_event = {
                    "event_kind": "module_completed",
                    "minion_id": "",
                    "run_id": state.run_id,
                    "work_order_id": event_work_order_id,
                    "minion_profile": state.pack.minion_profile,
                    "payload": event_payload,
                    "created_at": utc_now(),
                }
                manager._queue_event_delivery(module_event)
                manager.tasking_repository.record_minion_event(module_event)
                if str(event_payload.get("status") or "") in {"completed", "already_completed"}:
                    completed_event = {
                        "event_kind": "work_order_completed",
                        "minion_id": "",
                        "run_id": state.run_id,
                        "work_order_id": event_work_order_id,
                        "minion_profile": state.pack.minion_profile,
                        "payload": {
                            **event_payload,
                            "status": "completed" if str(event_payload.get("status") or "") == "completed" else str(event_payload.get("status") or "completed"),
                            "plan_ref": dict((state.pack.metadata or {}).get("plan_ref") or {}),
                            "profile_group": state.pack.profile_group,
                            "profile_name": state.pack.profile_name,
                        },
                        "created_at": utc_now(),
                    }
                    manager._queue_event_delivery(completed_event)
                    manager.tasking_repository.record_minion_event(completed_event)
            await manager._send_runner_control_or_record(state, {"type": "complete", "completion": event_payload})
            manager.logger.info("minion serial module sent completion run=%s work_order=%s", state.run_id, work_order_id)
        except Exception:
            manager.logger.exception("failed to send serial minion turn: %s", work_order_id)
        finally:
            manager._serial_turns_inflight.release(inflight_key)
