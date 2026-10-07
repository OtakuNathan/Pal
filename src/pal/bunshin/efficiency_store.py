"""Read-only Bunshin v2 storage access for efficiency telemetry.

This module is the single owner of the read-only query path into Bunshin
storage. It must never create, migrate, or mutate the database: it opens the
SQLite file strictly in read-only URI mode and must fail rather than degrade
to a writable open (for example when the file does not exist).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping


class EfficiencyStoreError(RuntimeError):
    """Raised when the telemetry query path cannot read Bunshin storage."""


class WorkflowNotFoundError(EfficiencyStoreError):
    """Raised when no role invocation exists for the requested workflow id."""


class StorageUnavailableError(EfficiencyStoreError):
    """Raised when the Bunshin database file is missing or not openable read-only."""


@dataclass(frozen=True)
class WorkflowTelemetryRecords:
    """Immutable raw telemetry rows for exactly one workflow.

    Rows are plain read-only mappings of the stored columns; the store never
    derives, fills, or zeroes missing values. A table that does not exist in
    an older storage file yields an empty tuple, never fabricated rows.
    """

    workflow_id: str
    role_invocations: tuple[Mapping[str, Any], ...]
    role_turns: tuple[Mapping[str, Any], ...]
    worker_events: tuple[Mapping[str, Any], ...]


@contextmanager
def open_readonly(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open the Bunshin SQLite database strictly read-only.

    Args:
        db_path: Path to the Bunshin v2 SQLite database file.

    Returns:
        A context manager yielding one ``sqlite3.Connection`` opened with
        ``mode=ro`` (and ``immutable`` never assumed); the connection is
        closed when the context exits.

    Errors:
        ``StorageUnavailableError`` if the file is missing or cannot be
        opened in read-only mode. This function never creates the file,
        never runs schema DDL, and never writes journal or WAL frames on its
        own account.
    """
    if not db_path.is_file():
        raise StorageUnavailableError(f"Bunshin storage not found at {db_path}")
    uri = f"{db_path.absolute().as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=30.0)
    except sqlite3.Error as exc:
        raise StorageUnavailableError(
            f"Bunshin storage at {db_path} cannot be opened read-only: {exc}"
        ) from exc
    try:
        connection.row_factory = sqlite3.Row
        yield connection
    finally:
        connection.close()


def _select_rows(
    connection: sqlite3.Connection, query: str, parameters: tuple
) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        MappingProxyType(dict(row)) for row in connection.execute(query, parameters)
    )


def read_workflow_telemetry(db_path: Path, workflow_id: str) -> WorkflowTelemetryRecords:
    """Read all raw telemetry rows for one workflow in a single read-only pass.

    Args:
        db_path: Path to the Bunshin v2 SQLite database file.
        workflow_id: Workflow identifier supplied by the operator.

    Returns:
        ``WorkflowTelemetryRecords`` with rows from
        ``bunshin_v2_role_invocations``, ``bunshin_v2_role_turns``, and
        ``bunshin_v2_worker_events`` filtered to the workflow.

    Errors:
        ``StorageUnavailableError`` when storage cannot be opened read-only;
        ``WorkflowNotFoundError`` when the workflow id has no recorded role
        invocations. Missing legacy tables surface as empty tuples, not as
        errors or zero-filled rows.
    """
    with open_readonly(db_path) as connection:
        try:
            role_invocations = _select_rows(
                connection,
                "SELECT * FROM bunshin_v2_role_invocations"
                " WHERE workflow_id = ? ORDER BY created_at, invocation_id",
                (workflow_id,),
            )
            if not role_invocations:
                raise WorkflowNotFoundError(
                    f"no recorded role invocations for workflow {workflow_id!r}"
                )
            role_turns = _select_optional(
                connection,
                "SELECT t.* FROM bunshin_v2_role_turns AS t"
                " JOIN bunshin_v2_role_invocations AS i"
                " ON t.invocation_id = i.invocation_id"
                " WHERE i.workflow_id = ?"
                " ORDER BY t.invocation_id, t.turn_index",
                (workflow_id,),
            )
            worker_events = _select_optional(
                connection,
                "SELECT e.* FROM bunshin_v2_worker_events AS e"
                " JOIN bunshin_v2_role_invocations AS i"
                " ON e.invocation_id = i.invocation_id"
                " WHERE i.workflow_id = ?"
                " ORDER BY e.invocation_id, e.event_id",
                (workflow_id,),
            )
        except sqlite3.DatabaseError as exc:
            raise EfficiencyStoreError(
                f"failed to read telemetry for workflow {workflow_id!r}"
                f" from {db_path}: {exc}"
            ) from exc
    return WorkflowTelemetryRecords(
        workflow_id=workflow_id,
        role_invocations=role_invocations,
        role_turns=role_turns,
        worker_events=worker_events,
    )


def _select_optional(
    connection: sqlite3.Connection, query: str, parameters: tuple
) -> tuple[Mapping[str, Any], ...]:
    """Run a telemetry select, treating an absent legacy table as no rows.

    Only a missing table is tolerated; every other database failure (for
    example a corrupt file) propagates to the caller's declared error path.
    """
    try:
        return _select_rows(connection, query, parameters)
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return ()
        raise
