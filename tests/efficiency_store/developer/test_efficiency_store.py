"""Developer tests for the read-only efficiency telemetry store."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from pal.bunshin.v2.efficiency_store import (
    EfficiencyStoreError,
    StorageUnavailableError,
    WorkflowNotFoundError,
    WorkflowTelemetryRecords,
    open_readonly,
    read_workflow_telemetry,
)
from pal.bunshin.v2.schema import ensure_bunshin_v2_schema


def _insert_role_invocation(
    connection: sqlite3.Connection,
    invocation_id: str,
    workflow_id: str,
    role: str = "candidate_builder",
    created_at: str = "2024-01-01T00:00:00Z",
) -> None:
    connection.execute(
        """
        INSERT INTO bunshin_v2_role_invocations(
            invocation_id, workflow_id, aggregate_type, aggregate_id,
            lease_resource_key, fencing_token, role, prompt_pack_ref_json,
            status, created_at, updated_at
        ) VALUES (?, ?, 'module', 'm1', ?, 1, ?, '{}', 'completed', ?, ?)
        """,
        (invocation_id, workflow_id, f"lease:{invocation_id}", role, created_at, created_at),
    )


def _insert_role_turn(
    connection: sqlite3.Connection,
    invocation_id: str,
    turn_index: int,
    input_tokens: int = 100,
    output_tokens: int = 10,
    latency_ms: int = 500,
) -> None:
    connection.execute(
        """
        INSERT INTO bunshin_v2_role_turns(
            invocation_id, turn_index, llm_request_ref_json, llm_response_ref_json,
            input_tokens, output_tokens, latency_ms, completed_at
        ) VALUES (?, ?, '{}', '{}', ?, ?, ?, '2024-01-01T00:00:01Z')
        """,
        (invocation_id, turn_index, input_tokens, output_tokens, latency_ms),
    )


def _insert_worker_event(
    connection: sqlite3.Connection,
    invocation_id: str,
    round_index: int,
    tool_call_count: int,
) -> None:
    connection.execute(
        """
        INSERT INTO bunshin_v2_worker_events(
            invocation_id, event_kind, round_index, tool_call_count, created_at
        ) VALUES (?, 'round_completed', ?, ?, '2024-01-01T00:00:02Z')
        """,
        (invocation_id, round_index, tool_call_count),
    )


def _make_db(tmp_path: Path, name: str = "bunshin.sqlite3") -> Path:
    db_path = tmp_path / name
    connection = sqlite3.connect(db_path)
    try:
        ensure_bunshin_v2_schema(connection)
        connection.commit()
    finally:
        connection.close()
    return db_path


def _file_digest(db_path: Path) -> str:
    return hashlib.sha256(db_path.read_bytes()).hexdigest()


def test_open_readonly_yields_closing_connection(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    with open_readonly(db_path) as connection:
        row = connection.execute("SELECT 42 AS value").fetchone()
        assert row["value"] == 42
        assert connection.total_changes == 0
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1").fetchall()


def test_open_readonly_missing_file_raises_storage_unavailable(tmp_path: Path) -> None:
    with pytest.raises(StorageUnavailableError):
        with open_readonly(tmp_path / "absent.sqlite3"):
            pass


def test_open_readonly_never_creates_file(tmp_path: Path) -> None:
    absent = tmp_path / "absent.sqlite3"
    with pytest.raises(StorageUnavailableError):
        with open_readonly(absent):
            pass
    assert not absent.exists()


def test_open_readonly_rejects_directory(tmp_path: Path) -> None:
    with pytest.raises(StorageUnavailableError):
        with open_readonly(tmp_path):
            pass


def test_open_readonly_blocks_writes(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    with open_readonly(db_path) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(
                "CREATE TABLE IF NOT EXISTS probe (x INTEGER)"
            ).fetchall()


def test_read_workflow_telemetry_returns_workflow_rows_only(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    connection = sqlite3.connect(db_path)
    try:
        _insert_role_invocation(connection, "inv-a", "wf-1", created_at="2024-01-01T00:00:00Z")
        _insert_role_invocation(connection, "inv-b", "wf-1", created_at="2024-01-01T00:00:05Z")
        _insert_role_invocation(connection, "inv-other", "wf-2")
        _insert_role_turn(connection, "inv-a", 0)
        _insert_role_turn(connection, "inv-a", 1)
        _insert_role_turn(connection, "inv-other", 0)
        _insert_worker_event(connection, "inv-a", 0, tool_call_count=3)
        _insert_worker_event(connection, "inv-other", 0, tool_call_count=1)
        connection.commit()
    finally:
        connection.close()

    records = read_workflow_telemetry(db_path, "wf-1")
    assert isinstance(records, WorkflowTelemetryRecords)
    assert records.workflow_id == "wf-1"
    assert [row["invocation_id"] for row in records.role_invocations] == ["inv-a", "inv-b"]
    assert {row["turn_index"] for row in records.role_turns} == {0, 1}
    assert all(row["invocation_id"] == "inv-a" for row in records.role_turns)
    assert [row["tool_call_count"] for row in records.worker_events] == [3]


def test_read_workflow_telemetry_rows_are_immutable_mappings(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    connection = sqlite3.connect(db_path)
    try:
        _insert_role_invocation(connection, "inv-a", "wf-1")
        connection.commit()
    finally:
        connection.close()

    records = read_workflow_telemetry(db_path, "wf-1")
    row = records.role_invocations[0]
    with pytest.raises(TypeError):
        row["role"] = "mutated"


def test_read_workflow_telemetry_unknown_workflow_raises(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    connection = sqlite3.connect(db_path)
    try:
        _insert_role_invocation(connection, "inv-a", "wf-1")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(WorkflowNotFoundError) as excinfo:
        read_workflow_telemetry(db_path, "wf-unknown")
    assert "wf-unknown" in str(excinfo.value)


def test_read_workflow_telemetry_missing_file_raises_storage_unavailable(
    tmp_path: Path,
) -> None:
    with pytest.raises(StorageUnavailableError):
        read_workflow_telemetry(tmp_path / "absent.sqlite3", "wf-1")


def test_read_workflow_telemetry_legacy_tables_absent_yield_empty_tuples(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE bunshin_v2_role_invocations (
                invocation_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                aggregate_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                lease_resource_key TEXT NOT NULL,
                fencing_token INTEGER NOT NULL,
                role TEXT NOT NULL,
                prompt_pack_ref_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO bunshin_v2_role_invocations VALUES (
                'inv-legacy', 'wf-legacy', 'module', 'm1', 'lease:k', 1,
                'architect', '{}', 'completed',
                '2023-01-01T00:00:00Z', '2023-01-01T00:00:00Z'
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    records = read_workflow_telemetry(db_path, "wf-legacy")
    assert len(records.role_invocations) == 1
    assert records.role_turns == ()
    assert records.worker_events == ()


def test_read_workflow_telemetry_corrupt_file_raises_store_error(tmp_path: Path) -> None:
    db_path = tmp_path / "corrupt.sqlite3"
    db_path.write_bytes(b"this is definitely not a sqlite database file")
    with pytest.raises(EfficiencyStoreError) as excinfo:
        read_workflow_telemetry(db_path, "wf-1")
    assert not isinstance(excinfo.value, WorkflowNotFoundError)


def test_read_workflow_telemetry_leaves_storage_byte_identical(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    connection = sqlite3.connect(db_path)
    try:
        _insert_role_invocation(connection, "inv-a", "wf-1")
        _insert_role_turn(connection, "inv-a", 0)
        _insert_worker_event(connection, "inv-a", 0, tool_call_count=2)
        connection.commit()
    finally:
        connection.close()

    before = _file_digest(db_path)
    read_workflow_telemetry(db_path, "wf-1")
    with open_readonly(db_path) as readonly_connection:
        readonly_connection.execute("SELECT count(*) FROM bunshin_v2_role_turns").fetchall()
    assert _file_digest(db_path) == before


def test_read_workflow_telemetry_no_sidecar_files_created(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    connection = sqlite3.connect(db_path)
    try:
        _insert_role_invocation(connection, "inv-a", "wf-1")
        connection.commit()
    finally:
        connection.close()

    read_workflow_telemetry(db_path, "wf-1")
    suffixes = {p.suffix for p in tmp_path.iterdir()}
    assert suffixes == {".sqlite3"}
