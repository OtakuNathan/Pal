from __future__ import annotations
from pal.bunshin.storage.serialization import _json
from pal.bunshin.storage.serialization import _cycle_payload
import json
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass
from pal.foundation import utc_now
from pal.bunshin.cycle_protocol import NodeCycle, PlanCycle, node_cycle_from_mapping, plan_cycle_from_mapping
from pal.bunshin.graph_executor import GraphExecution
from pal.bunshin.dependency_repair_protocol import DependencyRepairLedger
from pal.bunshin.graph_protocol import GraphIR, graph_ir_from_mapping
from pal.bunshin.storage.connection_contracts import DatabasePort


@dataclass
class CyclesStore:
    database: DatabasePort

    def store_graph_generation(
        self,
        *,
        workflow_id: str,
        graph: GraphIR,
        status: str = "compiled",
        _connection: sqlite3.Connection | None = None,
    ) -> GraphIR:
        """Persist one immutable compiled GraphIR generation idempotently."""

        if _connection is None:
            self.database.ensure_schema()
        now = utc_now()
        payload = graph.to_dict()
        transaction = self.database.write_connection() if _connection is None else nullcontext(_connection)
        with transaction as connection:
            existing = connection.execute(
                "SELECT graph_ir_json, workflow_id FROM bunshin_v2_graph_generations "
                "WHERE graph_id = ? AND generation = ?",
                (graph.graph_id, graph.generation),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["workflow_id"]) != workflow_id
                    or json.loads(str(existing["graph_ir_json"])) != payload
                ):
                    raise ValueError(
                        "GraphIR generation identity is already bound to other content"
                    )
                return graph_ir_from_mapping(
                    json.loads(str(existing["graph_ir_json"]))
                )
            latest = connection.execute(
                "SELECT MAX(generation) AS generation "
                "FROM bunshin_v2_graph_generations WHERE graph_id = ?",
                (graph.graph_id,),
            ).fetchone()
            latest_generation = int((latest or {})["generation"] or 0)
            if graph.generation != latest_generation + 1:
                raise ValueError(
                    "GraphIR generations must be appended without gaps"
                )
            connection.execute(
                """
                INSERT INTO bunshin_v2_graph_generations(
                    graph_id, generation, workflow_id, generation_hash,
                    graph_ir_json, source_ref, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    graph.graph_id,
                    graph.generation,
                    workflow_id,
                    graph.generation_hash,
                    _json(payload),
                    graph.source_ref,
                    str(status),
                    now,
                ),
            )
        return graph

    def read_graph_generation(
        self,
        *,
        graph_id: str,
        generation: int | None = None,
        _connection: sqlite3.Connection | None = None,
    ) -> GraphIR | None:
        if _connection is None:
            self.database.ensure_schema()
        connection_scope = self.database.read_connection() if _connection is None else nullcontext(_connection)
        with connection_scope as connection:
            if generation is None:
                row = connection.execute(
                    "SELECT graph_ir_json FROM bunshin_v2_graph_generations "
                    "WHERE graph_id = ? ORDER BY generation DESC LIMIT 1",
                    (graph_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT graph_ir_json FROM bunshin_v2_graph_generations "
                    "WHERE graph_id = ? AND generation = ?",
                    (graph_id, int(generation)),
                ).fetchone()
        if row is None:
            return None
        return graph_ir_from_mapping(json.loads(str(row["graph_ir_json"])))

    def store_plan_cycle(
        self,
        *,
        workflow_id: str,
        cycle: PlanCycle,
        _connection: sqlite3.Connection | None = None,
    ) -> None:
        if _connection is None:
            self.database.ensure_schema()
        now = utc_now()
        payload = _cycle_payload(cycle)
        transaction = self.database.write_connection() if _connection is None else nullcontext(_connection)
        with transaction as connection:
            connection.execute(
                """
                INSERT INTO bunshin_v2_plan_cycles(
                    cycle_id, workflow_id, generation, state, payload_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cycle_id) DO UPDATE SET
                    generation = excluded.generation,
                    state = excluded.state,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    cycle.cycle_id,
                    workflow_id,
                    cycle.generation,
                    cycle.state.value,
                    _json(payload),
                    now,
                    now,
                ),
            )

    def read_plan_cycle(
        self,
        *,
        workflow_id: str,
        _connection: sqlite3.Connection | None = None,
    ) -> PlanCycle | None:
        if _connection is None:
            self.database.ensure_schema()
        connection_scope = self.database.read_connection() if _connection is None else nullcontext(_connection)
        with connection_scope as connection:
            row = connection.execute(
                "SELECT payload_json FROM bunshin_v2_plan_cycles "
                "WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
        if row is None:
            return None
        return plan_cycle_from_mapping(
            json.loads(str(row["payload_json"]))
        )

    def read_node_cycles(
        self,
        *,
        workflow_id: str,
        graph_generation: int | None = None,
        _connection: sqlite3.Connection | None = None,
    ) -> dict[str, NodeCycle]:
        if _connection is None:
            self.database.ensure_schema()
        connection_scope = self.database.read_connection() if _connection is None else nullcontext(_connection)
        with connection_scope as connection:
            if graph_generation is None:
                graph = connection.execute(
                    "SELECT MAX(graph_generation) AS generation "
                    "FROM bunshin_v2_node_cycles WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchone()
                graph_generation = int((graph or {})["generation"] or 0)
            rows = connection.execute(
                "SELECT payload_json FROM bunshin_v2_node_cycles "
                "WHERE workflow_id = ? AND graph_generation = ? "
                "ORDER BY node_name",
                (workflow_id, int(graph_generation)),
            ).fetchall()
        cycles = [
            node_cycle_from_mapping(json.loads(str(row["payload_json"])))
            for row in rows
        ]
        return {cycle.node_name: cycle for cycle in cycles}

    def store_graph_execution(
        self,
        *,
        workflow_id: str,
        execution: GraphExecution,
        _connection: sqlite3.Connection | None = None,
    ) -> None:
        """Atomically replace every cycle projection for one graph generation."""

        if _connection is None:
            self.database.ensure_schema()
        now = utc_now()
        transaction = self.database.write_connection() if _connection is None else nullcontext(_connection)
        with transaction as connection:
            graph_row = connection.execute(
                "SELECT workflow_id, generation_hash, execution_json "
                "FROM bunshin_v2_graph_generations "
                "WHERE graph_id = ? AND generation = ?",
                (execution.graph.graph_id, execution.graph.generation),
            ).fetchone()
            if graph_row is None:
                raise ValueError("GraphExecution requires a stored GraphIR generation")
            if (
                str(graph_row["workflow_id"]) != workflow_id
                or str(graph_row["generation_hash"])
                != execution.graph.generation_hash
            ):
                raise ValueError("GraphExecution is bound to another workflow generation")
            previous_runtime = json.loads(str(graph_row["execution_json"] or "{}"))
            if not isinstance(previous_runtime, dict):
                raise ValueError("graph execution metadata must be an object")
            if "dependency_repairs" in previous_runtime and previous_runtime["dependency_repairs"] is None:
                raise ValueError("dependency repair section cannot be null")
            DependencyRepairLedger.from_mapping(
                previous_runtime.get("dependency_repairs"),
            ).assert_successor(execution.dependency_repairs)
            for cycle in execution.cycles.values():
                payload = _cycle_payload(cycle)
                connection.execute(
                    """
                    INSERT INTO bunshin_v2_node_cycles(
                        cycle_id, workflow_id, graph_id, graph_generation,
                        node_name, state, payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cycle_id) DO UPDATE SET
                        graph_generation = excluded.graph_generation,
                        state = excluded.state,
                        payload_json = excluded.payload_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        cycle.cycle_id,
                        workflow_id,
                        execution.graph.graph_id,
                        execution.graph.generation,
                        cycle.node_name,
                        cycle.state.value,
                        _json(payload),
                        now,
                        now,
                    ),
                )
            connection.execute(
                "UPDATE bunshin_v2_graph_generations "
                "SET status = ?, execution_json = ? "
                "WHERE graph_id = ? AND generation = ?",
                (
                    execution.state.value.lower(),
                    _json(
                        {
                            "state": execution.state.value,
                            "published_sink_ref": execution.published_sink_ref,
                            "repair_barriers": {
                                name: list(providers)
                                for name, providers in execution.repair_barriers.items()
                            },
                            "dependency_repairs": execution.dependency_repairs.to_dict(),
                        }
                    ),
                    execution.graph.graph_id,
                    execution.graph.generation,
                ),
            )

    def read_graph_execution(
        self,
        *,
        workflow_id: str,
        generation: int | None = None,
        _connection: sqlite3.Connection | None = None,
    ) -> GraphExecution | None:
        if _connection is None:
            self.database.ensure_schema()
        connection_scope = self.database.read_connection() if _connection is None else nullcontext(_connection)
        with connection_scope as connection:
            # Execution metadata and cycle projections are one read snapshot.
            # A caller-owned write transaction is borrowed without committing
            # it; plain readers own only this short read transaction.
            owns_read = not connection.in_transaction
            if owns_read:
                connection.execute("BEGIN")
            try:
                if generation is None:
                    row = connection.execute(
                        "SELECT graph_ir_json, status, execution_json "
                        "FROM bunshin_v2_graph_generations "
                        "WHERE workflow_id = ? ORDER BY generation DESC LIMIT 1",
                        (workflow_id,),
                    ).fetchone()
                else:
                    row = connection.execute(
                        "SELECT graph_ir_json, status, execution_json "
                        "FROM bunshin_v2_graph_generations "
                        "WHERE workflow_id = ? AND generation = ?",
                        (workflow_id, int(generation)),
                    ).fetchone()
                if row is None:
                    return None
                graph = graph_ir_from_mapping(json.loads(str(row["graph_ir_json"])))
                cycles = self.read_node_cycles(
                    workflow_id=workflow_id, graph_generation=graph.generation,
                    _connection=connection,
                )
            finally:
                if owns_read:
                    connection.rollback()
        if not cycles:
            return None
        from pal.bunshin.graph_executor import GraphExecutionState

        runtime = json.loads(str(row["execution_json"] or "{}"))
        if not isinstance(runtime, dict):
            raise ValueError("graph execution metadata must be an object")
        if "dependency_repairs" in runtime and runtime["dependency_repairs"] is None:
            raise ValueError("dependency repair section cannot be null")
        repairs = DependencyRepairLedger.from_mapping(runtime.get("dependency_repairs"))
        raw_status = str(
            runtime.get("state") or row["status"] or ""
        ).upper()
        state = (
            GraphExecutionState(raw_status)
            if raw_status in {GraphExecutionState.CANCELLED.value, GraphExecutionState.REPLAN_REQUIRED.value}
            else GraphExecutionState.RUNNING
            if repairs.pending is not None
            else GraphExecutionState.COMPLETED
            if cycles[graph.sink].state.value == "ACCEPTED"
            and all(cycle.state.value == "ACCEPTED" for cycle in cycles.values())
            else GraphExecutionState(raw_status)
            if raw_status in GraphExecutionState._value2member_map_
            else GraphExecutionState.RUNNING
        )
        return GraphExecution(
            graph=graph,
            state=state,
            cycles=cycles,
            published_sink_ref=(
                "" if repairs.pending is not None else str(runtime.get("published_sink_ref") or "")
                or (
                    cycles[graph.sink].accepted_product_ref
                    if state == GraphExecutionState.COMPLETED
                    else ""
                )
            ),
            repair_barriers={
                str(name): tuple(str(item) for item in list(providers or []))
                for name, providers in dict(
                    runtime.get("repair_barriers") or {}
                ).items()
            },
            dependency_repairs=repairs,
        )
