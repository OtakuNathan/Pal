from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any
from uuid import uuid4


@dataclass
class MinionLedgerStore:
    owner: Any

    def insert_ledger(
        self,
        db: sqlite3.Connection,
        work_order_id: str,
        event_kind: str,
        summary: str,
        payload: dict[str, Any],
        minion_id: str,
        run_id: str,
        created_at: str,
    ) -> str:
        ledger_id = f"led_{uuid4().hex[:16]}"
        db.execute(
            """
            INSERT INTO minion_worker_ledger(
                ledger_id, work_order_id, event_kind, summary, payload_json, minion_id, run_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ledger_id, work_order_id, event_kind, summary, _json(payload), minion_id, run_id, created_at),
        )
        return ledger_id

    def insert_checkpoint(
        self,
        db: sqlite3.Connection,
        work_order_id: str,
        payload: dict[str, Any],
        minion_id: str,
        run_id: str,
        created_at: str,
    ) -> None:
        milestone_index = _coerce_optional_int(payload.get("milestone_index"))
        if milestone_index is None:
            milestone_index = self.owner._derive_current_milestone_index(db, work_order_id)
        status = str(payload.get("status") or "partial").strip().lower()
        if status not in {"completed", "claimed", "partial", "blocked", "failed"}:
            status = "partial"
        checkpoint_id = str(payload.get("checkpoint_id") or "").strip() or f"chk_{uuid4().hex[:16]}"
        db.execute(
            """
            INSERT INTO minion_worker_checkpoints(
                checkpoint_id, work_order_id, milestone_index, status, summary, payload_json, minion_id, run_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                checkpoint_id,
                work_order_id,
                int(milestone_index or 0),
                status,
                str(payload.get("summary") or ""),
                _json(payload),
                minion_id,
                run_id,
                created_at,
            ),
        )

    def record_terminal(
        self,
        db: sqlite3.Connection,
        work_order_id: str,
        payload: dict[str, Any],
        minion_id: str,
        run_id: str,
        created_at: str,
    ) -> None:
        _ = minion_id, run_id, created_at
        status = str(payload.get("status") or "completed").strip().lower()
        if status not in {"completed", "failed", "blocked", "killed"}:
            status = "completed"
        if status == "completed" and self.owner._has_incomplete_milestones(db, work_order_id):
            status = "active"
        self.owner._update_work_order_status(db, work_order_id, status)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _coerce_optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
