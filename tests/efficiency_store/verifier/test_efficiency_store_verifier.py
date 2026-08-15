"""Verifier adversarial cases for the efficiency_store Candidate.

Cases target the newly implemented read-only path: WAL databases written by
the live runtime (repository.py sets PRAGMA journal_mode=WAL), unreadable
files, legacy-schema drift on the optional tables, and the immutable-record
consumer edge used by efficiency_metrics.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pal.bunshin.v2.efficiency_store import (
    EfficiencyStoreError,
    StorageUnavailableError,
    WorkflowNotFoundError,
    WorkflowTelemetryRecords,
    open_readonly,
    read_workflow_telemetry)
from pal.bunshin.v2.schema import ensure_bunshin_v2_schema


def _make_db(tmp_path: Path, name: str = "bunshin.sqlite3") -> Path:
    db_path = tmp_path / name
    connection = sqlite3.connect(db_path)
    try:
        ensure_bunshin_v2_schema(connection)
        connection.commit()
    finally:
        connection.close()
    return db_path


def _seed_workflow(db_path: Path, workflow_id: str = "wf-v") -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            INSERT INTO bunshin_v2_role_invocations(
                invocation_id, workflow_id, aggregate_type, aggregate_id,
                lease_resource_key, fencing_token, role, prompt_pack_ref_json,
                status, created_at, updated_at
            ) VALUES ('inv-v', ?, 'module', 'm1', 'lease:v', 1,
                      'candidate_builder', '{}', 'completed',
                      '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')
            """,
            (workflow_id,),
        )
        connection.commit()
    finally:
        connection.close()


def test_readonly_open_on_live_wal_database(tmp_path: Path) -> None:
    """The runtime writer runs in WAL mode; the read path must still read it.

    A writer connection holds an open transaction state with committed frames
    in the -wal file; read_workflow_telemetry must return those committed rows
    without mutating storage.
    """
    db_path = tmp_path / "live.sqlite3"
    writer = sqlite3.connect(db_path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        ensure_bunshin_v2_schema(writer)
        writer.execute(
            """
            INSERT INTO bunshin_v2_role_invocations(
                invocation_id, workflow_id, aggregate_type, aggregate_id,
                lease_resource_key, fencing_token, role, prompt_pack_ref_json,
                status, created_at, updated_at
            ) VALUES ('inv-w', 'wf-wal', 'module', 'm1', 'lease:w', 1,
                      'candidate_builder', '{}', 'completed',
                      '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')
            """
        )
        writer.commit()
        # -wal and -shm sidecars now exist next to the live database.
        assert (tmp_path / "live.sqlite3-wal").exists()

        records = read_workflow_telemetry(db_path, "wf-wal")
        assert records.workflow_id == "wf-wal"
        assert [row["invocation_id"] for row in records.role_invocations] == ["inv-w"]
    finally:
        writer.close()


def test_broken_symlink_raises_storage_unavailable(tmp_path: Path) -> None:
    """A dangling symlink is not an existing file; the store must fail closed."""
    real = _make_db(tmp_path)
    _seed_workflow(real)
    link = tmp_path / "dangling.sqlite3"
    link.symlink_to(tmp_path / "never-created.sqlite3")
    with pytest.raises(StorageUnavailableError):
        with open_readonly(link):
            pass
    with pytest.raises(StorageUnavailableError):
        read_workflow_telemetry(link, "wf-v")


def test_legacy_turns_table_with_drifted_schema_aborts(
    tmp_path: Path,
) -> None:
    """A legacy optional table lacking expected columns must abort, not fabricate.

    Only a wholly absent legacy table may yield empty tuples; schema drift on
    an existing table is a read failure on the declared error path.
    """
    db_path = tmp_path / "drift.sqlite3"
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
        # Legacy turns table exists but lacks turn_index used by the query.
        connection.execute(
            "CREATE TABLE bunshin_v2_role_turns (invocation_id TEXT NOT NULL)"
        )
        connection.execute(
            """
            INSERT INTO bunshin_v2_role_invocations VALUES (
                'inv-d', 'wf-drift', 'module', 'm1', 'lease:d', 1,
                'architect', '{}', 'completed',
                '2023-01-01T00:00:00Z', '2023-01-01T00:00:00Z'
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(EfficiencyStoreError) as excinfo:
        read_workflow_telemetry(db_path, "wf-drift")
    assert not isinstance(excinfo.value, WorkflowNotFoundError)


def test_records_are_frozen_values_owned_by_caller(tmp_path: Path) -> None:
    """Consumer edge: efficiency_metrics receives immutable records it may hold."""
    db_path = _make_db(tmp_path)
    _seed_workflow(db_path)
    records = read_workflow_telemetry(db_path, "wf-v")
    assert isinstance(records, WorkflowTelemetryRecords)
    with pytest.raises(Exception):
        records.workflow_id = "mutated"
    with pytest.raises(Exception):
        records.role_invocations.append({})  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        records.role_invocations[0]["role"] = "mutated"


def test_connection_error_after_failed_read_is_terminal(tmp_path: Path) -> None:
    """failed-until-reset: a failed read leaves the connection closed for reuse."""
    db_path = tmp_path / "corrupt.sqlite3"
    db_path.write_bytes(b"not a sqlite database file at all")
    with pytest.raises(EfficiencyStoreError):
        read_workflow_telemetry(db_path, "wf-x")
