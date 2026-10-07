"""Borrow one transaction connection without gaining commit or rollback authority."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from pal.bunshin.storage.connection_contracts import DatabasePort


@dataclass
class TransactionSession:
    parent: DatabasePort
    connection: sqlite3.Connection
    _active: bool = field(default=True, init=False)

    @property
    def runtime_root(self) -> Path:
        return self.parent.runtime_root

    @property
    def db_path(self) -> Path:
        return self.parent.db_path

    def ensure_schema(self) -> None:
        if not self._active:
            raise RuntimeError('Bunshin unit of work is already closed')

    def finish(self) -> None:
        self._active = False

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        self.ensure_schema()
        yield self.connection

    @contextmanager
    def write_connection(self) -> Iterator[sqlite3.Connection]:
        self.ensure_schema()
        yield self.connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.ensure_schema()
        yield self.connection

    def sync_task_fts_locked(
        self, connection: sqlite3.Connection, task_id: str, *, row: sqlite3.Row | None = None,
    ) -> None:
        self.ensure_schema()
        if connection is not self.connection:
            raise ValueError('a unit of work cannot write through another connection')
        self.parent.sync_task_fts_locked(connection, task_id, row=row)
