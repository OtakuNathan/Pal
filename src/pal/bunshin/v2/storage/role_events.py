from __future__ import annotations
from pal.bunshin.v2.storage.serialization import _json
import json
from dataclasses import dataclass
from typing import Any, Mapping
from pal.foundation import utc_now
from pal.bunshin.v2.contracts import AggregateVersionConflict
from pal.bunshin.v2.storage.artifacts import ArtifactsStore
from pal.bunshin.v2.storage.connection_contracts import DatabasePort
from pal.bunshin.v2.storage.leases import LeasesStore
from pal.bunshin.v2.storage.projections import ProjectionsStore


@dataclass
class RoleEventsStore:
    artifacts: ArtifactsStore
    database: DatabasePort
    leases: LeasesStore
    projections: ProjectionsStore

    def record_worker_event(self, event: Mapping[str, Any]) -> None:
        invocation_id = str(event.get("invocation_id") or event.get("bunshin_id") or "").strip()
        if not invocation_id:
            return
        event_kind = str(event.get("event_kind") or "progress")
        payload = dict(event.get("payload") or {})
        phase = str(payload.get("phase") or "")
        round_index = int(payload.get("round") or 0)
        tool_call_count = int(payload.get("tool_call_count") or 0)
        created_at = str(event.get("created_at") or utc_now())
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            invocation = connection.execute(
                "SELECT status FROM bunshin_v2_role_invocations WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()
            if invocation is None:
                return
            connection.execute(
                """
                INSERT INTO bunshin_v2_worker_events(
                    invocation_id, event_kind, phase, round_index, tool_call_count, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (invocation_id, event_kind, phase, round_index, tool_call_count, _json(payload), created_at),
            )
            if event_kind == "progress" and phase == "llm_round_completed":
                connection.execute(
                    """
                    UPDATE bunshin_v2_role_invocations
                    SET last_completed_turn = MAX(last_completed_turn, ?), updated_at = ?
                    WHERE invocation_id = ?
                    """,
                    (round_index, created_at, invocation_id),
                )

    def record_role_turn(
        self,
        *,
        invocation_id: str,
        fencing_token: int,
        turn_index: int,
        llm_request_ref: Mapping[str, Any],
        llm_response_ref: Mapping[str, Any],
        tool_summary_ref: Mapping[str, Any] | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost: float = 0,
        latency_ms: int = 0,
        tool_latency_ms: int = 0,
        wall_latency_ms: int = 0,
    ) -> None:
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            invocation = connection.execute(
                "SELECT * FROM bunshin_v2_role_invocations WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()
            if invocation is None:
                raise KeyError(f"unknown role invocation: {invocation_id}")
            self.leases.assert_lease_locked(
                connection,
                str(invocation["lease_resource_key"]),
                invocation_id,
                fencing_token,
            )
            refs = {
                "request": dict(llm_request_ref),
                "response": dict(llm_response_ref),
                "tools": dict(tool_summary_ref or {}),
            }
            self.artifacts.assert_artifact_refs_durable(connection, refs)
            now = utc_now()
            connection.execute(
                """
                INSERT INTO bunshin_v2_role_turns(
                    invocation_id, turn_index, llm_request_ref_json, llm_response_ref_json,
                    tool_summary_ref_json, input_tokens, output_tokens, cost, latency_ms,
                    tool_latency_ms, wall_latency_ms, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(invocation_id, turn_index) DO NOTHING
                """,
                (
                    invocation_id,
                    int(turn_index),
                    _json(dict(llm_request_ref)),
                    _json(dict(llm_response_ref)),
                    _json(dict(tool_summary_ref or {})),
                    max(0, int(input_tokens)),
                    max(0, int(output_tokens)),
                    max(0.0, float(cost)),
                    max(0, int(latency_ms)),
                    max(0, int(tool_latency_ms)),
                    max(0, int(wall_latency_ms)),
                    now,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] == 1:
                connection.execute(
                    """
                    UPDATE bunshin_v2_role_invocations
                    SET last_completed_turn = MAX(last_completed_turn, ?),
                        total_input_tokens = total_input_tokens + ?,
                        total_output_tokens = total_output_tokens + ?,
                        total_cost = total_cost + ?, total_latency_ms = total_latency_ms + ?,
                        total_tool_latency_ms = total_tool_latency_ms + ?,
                        total_wall_latency_ms = total_wall_latency_ms + ?,
                        updated_at = ?
                    WHERE invocation_id = ?
                    """,
                    (
                        int(turn_index),
                        max(0, int(input_tokens)),
                        max(0, int(output_tokens)),
                        max(0.0, float(cost)),
                        max(0, int(latency_ms)),
                        max(0, int(tool_latency_ms)),
                        max(0, int(wall_latency_ms)),
                        now,
                        invocation_id,
                    ),
                )
            self.projections.rebuild_workflow_projection_locked(
                connection,
                str(invocation["workflow_id"]),
                "",
            )

    def update_node_journal(
        self,
        *,
        node_run_id: str,
        workflow_id: str,
        lease_resource_key: str,
        owner_id: str,
        fencing_token: int,
        expected_generation: int,
        journal: Mapping[str, Any],
    ) -> int:
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            self.leases.assert_lease_locked(connection, lease_resource_key, owner_id, fencing_token)
            row = connection.execute(
                "SELECT generation FROM bunshin_v2_node_journals WHERE node_run_id = ?",
                (node_run_id,),
            ).fetchone()
            current_generation = int(row["generation"]) if row is not None else 0
            if current_generation != int(expected_generation):
                raise AggregateVersionConflict(
                    f"expected journal generation {expected_generation}, found {current_generation}"
                )
            next_generation = current_generation + 1
            connection.execute(
                """
                INSERT INTO bunshin_v2_node_journals(
                    node_run_id, workflow_id, lease_resource_key, fencing_token,
                    generation, journal_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_run_id) DO UPDATE SET
                    lease_resource_key = excluded.lease_resource_key,
                    fencing_token = excluded.fencing_token,
                    generation = excluded.generation,
                    journal_json = excluded.journal_json,
                    updated_at = excluded.updated_at
                """,
                (
                    node_run_id,
                    workflow_id,
                    lease_resource_key,
                    fencing_token,
                    next_generation,
                    _json(dict(journal)),
                    utc_now(),
                ),
            )
            return next_generation

    def read_node_journal(self, node_run_id: str) -> dict[str, Any] | None:
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM bunshin_v2_node_journals WHERE node_run_id = ?",
                (str(node_run_id),),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["journal"] = json.loads(str(result.pop("journal_json")))
            return result
