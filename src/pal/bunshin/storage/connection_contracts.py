"""Connection authority shared by standalone stores and a transaction-bound view."""
from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol


class DatabasePort(Protocol):
    @property
    def runtime_root(self) -> Path: ...

    @property
    def db_path(self) -> Path: ...

    def ensure_schema(self) -> None: ...

    def read_connection(self) -> AbstractContextManager[sqlite3.Connection]: ...

    def write_connection(self) -> AbstractContextManager[sqlite3.Connection]: ...

    def transaction(self) -> AbstractContextManager[sqlite3.Connection]: ...

    def sync_task_fts_locked(
        self, connection: sqlite3.Connection, task_id: str, *, row: sqlite3.Row | None = None,
    ) -> None: ...
