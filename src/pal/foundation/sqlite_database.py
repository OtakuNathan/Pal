"""Owner-wide SQLite cleanup over Peewee's per-thread connections."""
from __future__ import annotations

from _thread import LockType
from pathlib import Path
import sqlite3
from threading import local
from typing import Any

from peewee import OperationalError, SqliteDatabase


class OwnedSqliteDatabase(SqliteDatabase):
    """Keep connection ownership until physical close succeeds.

    Owners must drain their operations before calling ``close_all``. Ordinary
    Peewee ``close``/connection contexts still close just the calling thread.
    Reopening after cleanup creates fresh connections on those same threads.
    """

    # Peewee's public stubs omit these connection lifecycle internals.
    _state: local
    _lock: LockType

    def __init__(self, database: str | Path, **options: Any) -> None:
        self._owned_connections: dict[sqlite3.Connection, dict[str, Any]] = {}
        super().__init__(database, thread_safe=True, check_same_thread=False, **options)

    def _connect(self) -> sqlite3.Connection:
        connection: sqlite3.Connection = super()._connect()  # type: ignore[misc]
        # Peewee's connect/close already hold _lock. Capture this thread's
        # actual state dictionary: calling a bound thread-local reset from
        # another thread would reset the closer's state instead.
        self._owned_connections[connection] = self._state.__dict__
        return connection

    def _close(self, connection: sqlite3.Connection) -> None:
        super()._close(connection)  # type: ignore[misc]
        state = self._owned_connections.pop(connection)
        # A failed per-thread close resets Peewee's state in its finally block.
        # That thread may since have opened a different connection in this dict.
        if state["conn"] is connection:
            state.update(closed=True, conn=None, ctx=[], transactions=[])

    def close_all(self) -> None:
        with self._lock:
            if any(state["transactions"] for state in self._owned_connections.values()):
                raise OperationalError("Cannot close database while a transaction is open")
            failures: list[Exception] = []
            for connection in tuple(self._owned_connections):
                try:
                    self._close(connection)
                except Exception as exc:
                    failures.append(exc)
            if failures:
                raise ExceptionGroup("SQLite connection cleanup failed", failures)
