"""Durable tool interactions keyed by logical assignment and original tool call."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Mapping, TypedDict, cast

from pal.bunshin.storage.connection_contracts import DatabasePort


class InteractionWait(TypedDict):
    wait_id: str
    assignment_id: str
    attempt_id: str
    kind: Literal["approval", "clarification"]
    state: Literal["open", "answered", "resuming", "consumed", "cancelled"]
    request: dict[str, Any]
    answer: dict[str, Any]
    resume_attempt_id: str


@dataclass
class InteractionWaits:
    database: DatabasePort

    def get(self, wait_id: str) -> InteractionWait | None:
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            row = connection.execute("SELECT * FROM bunshin_interaction_waits WHERE wait_id=?", (wait_id,)).fetchone()
        return self._decode(row) if row else None

    @staticmethod
    def _decode(row) -> InteractionWait:
        result = dict(row)
        result["request"] = json.loads(result.pop("request_json"))
        result["answer"] = json.loads(result.pop("answer_json"))
        return cast(InteractionWait, result)

    def is_parked(self, assignment_id: str) -> bool:
        with self.database.read_connection() as connection:
            return connection.execute(
                "SELECT 1 FROM bunshin_interaction_waits WHERE assignment_id=? AND state IN ('open','answered')",
                (assignment_id,),
            ).fetchone() is not None

    def pending(self, *, workflow_id: str = "", assignment_id: str = "") -> list[InteractionWait]:
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            rows = connection.execute(
                "SELECT w.* FROM bunshin_interaction_waits w JOIN bunshin_role_assignments a "
                "ON a.assignment_id=w.assignment_id WHERE w.state='open' "
                "AND a.state IN ('claimed','running','retry_queued','queued') "
                "AND (?='' OR a.workflow_id=?) AND (?='' OR a.assignment_id=?) ORDER BY w.rowid",
                (workflow_id, workflow_id, assignment_id, assignment_id),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def unresolved_cards(self, workflow_id: str) -> list[dict[str, str]]:
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            rows = connection.execute(
                """SELECT w.wait_id, w.kind FROM bunshin_interaction_waits w
                   JOIN bunshin_role_assignments a ON a.assignment_id = w.assignment_id
                   WHERE a.workflow_id = ? AND w.state IN ('open','cancelled') ORDER BY w.wait_id""",
                (workflow_id,),
            ).fetchall()
        return [{"interaction_id": row[0], "interaction_kind": "bunshin_" + row[1]} for row in rows]

    def open(self, *, wait_id: str, assignment_id: str, attempt_id: str,
             kind: str, request: Mapping[str, Any]) -> None:
        raw = json.dumps(request, ensure_ascii=False, sort_keys=True)
        with self.database.write_connection() as connection:
            connection.execute(
                "INSERT INTO bunshin_interaction_waits(wait_id,assignment_id,attempt_id,kind,request_json) "
                "VALUES(?,?,?,?,?) ON CONFLICT(wait_id) DO NOTHING", (wait_id, assignment_id, attempt_id, kind, raw),
            )
            row = connection.execute(
                "SELECT assignment_id,attempt_id,kind,request_json FROM bunshin_interaction_waits WHERE wait_id=?",
                (wait_id,),
            ).fetchone()
            if tuple(row) != (assignment_id, attempt_id, kind, raw):
                raise ValueError("conflicting role interaction")

    def answer(self, wait_id: str, answer: Mapping[str, Any]) -> None:
        raw = json.dumps(answer, ensure_ascii=False, sort_keys=True)
        with self.database.write_connection() as connection:
            row = connection.execute(
                "SELECT w.state,w.answer_json,a.state,s.state,f.state FROM bunshin_interaction_waits w "
                "JOIN bunshin_role_assignments a ON a.assignment_id=w.assignment_id "
                "JOIN bunshin_aggregate_snapshots s ON s.aggregate_id=a.aggregate_id AND s.aggregate_type=a.aggregate_type "
                "JOIN bunshin_aggregate_snapshots f ON f.aggregate_id=a.workflow_id AND f.aggregate_type='workflow' "
                "WHERE wait_id=?",
                (wait_id,),
            ).fetchone()
            if row is None:
                raise KeyError("unknown role interaction")
            if row[0] == "cancelled":
                raise ValueError("role interaction has been superseded")
            if row[0] != "open":
                if row[1] != raw:
                    raise ValueError("role interaction already has a different answer")
                return
            if (row[2] not in {"running", "retry_queued", "claimed", "queued"}
                    or row[3] in {"STALE", "SUPERSEDED", "CANCELLED", "CANCEL_REQUESTED"}
                    or row[4] not in {"ACTIVE", "PAUSE_REQUESTED", "PAUSED"}):
                raise ValueError("role interaction has been superseded")
            connection.execute("UPDATE bunshin_interaction_waits SET state='answered',answer_json=? WHERE wait_id=?",
                               (raw, wait_id))

    def begin_resume(self, wait_id: str, assignment_id: str, attempt_id: str) -> dict[str, Any]:
        with self.database.write_connection() as connection:
            row = connection.execute(
                "SELECT state,answer_json,resume_attempt_id FROM bunshin_interaction_waits "
                "WHERE wait_id=? AND assignment_id=?", (wait_id, assignment_id),
            ).fetchone()
            if row is None or row[0] != "answered":
                raise ValueError("role interaction lacks an unconsumed answer; reconcile prior execution")
            connection.execute("UPDATE bunshin_interaction_waits SET state='resuming',resume_attempt_id=? WHERE wait_id=?",
                               (attempt_id, wait_id))
            return json.loads(row[1])

    def consume(self, wait_id: str, assignment_id: str, attempt_id: str) -> None:
        with self.database.write_connection() as connection:
            matched = connection.execute(
                "UPDATE bunshin_interaction_waits SET state='consumed' WHERE wait_id=? AND assignment_id=? "
                "AND resume_attempt_id=? AND state IN ('resuming','consumed')", (wait_id, assignment_id, attempt_id),
            ).rowcount
            if matched != 1:
                raise ValueError("stale resumed role interaction")
