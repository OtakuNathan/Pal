from __future__ import annotations
from pal.bunshin.v2.storage.serialization import _TASK_FTS_INDEX_VERSION
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator
from pal.bunshin.config import bunshin_db_path
from pal.bunshin.v2.schema import ensure_bunshin_v2_schema
from pal.shared.text_search import jieba_fts_text


@dataclass
class BunshinDatabase:
    runtime_root: Path
    _schema_ready: bool = field(default=False, init=False, repr=False)

    @property
    def db_path(self) -> Path:
        return bunshin_db_path(Path(self.runtime_root))

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path), timeout=30.0) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            ensure_bunshin_v2_schema(connection)
            self.ensure_task_fts_index_locked(connection)
        self._schema_ready = True

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Open one repository transaction for an aggregate plus its projections.

        Callers may pass the yielded connection back to ``dispatch`` and the
        cycle projection methods.  Nothing becomes visible to the outbox
        processor until the complete business transition commits.
        """

        self.ensure_schema()
        with self.write_connection() as connection:
            yield connection

    def ensure_task_fts_index_locked(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT schema_value FROM bunshin_v2_schema_meta WHERE schema_key = 'task_fts_index_version'"
        ).fetchone()
        if row is not None and str(row[0]) == _TASK_FTS_INDEX_VERSION:
            return
        connection.execute("DELETE FROM bunshin_v2_tasks_fts")
        for task in connection.execute("SELECT * FROM bunshin_v2_task_projection").fetchall():
            self.sync_task_fts_locked(connection, str(task["task_id"]), row=task)
        connection.execute(
            """
            INSERT INTO bunshin_v2_schema_meta(schema_key, schema_value)
            VALUES ('task_fts_index_version', ?)
            ON CONFLICT(schema_key) DO UPDATE SET schema_value = excluded.schema_value
            """,
            (_TASK_FTS_INDEX_VERSION,),
        )

    def sync_task_fts_locked(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        *,
        row: sqlite3.Row | None = None,
    ) -> None:
        connection.execute("DELETE FROM bunshin_v2_tasks_fts WHERE task_id = ?", (str(task_id),))
        task = row or connection.execute(
            "SELECT * FROM bunshin_v2_task_projection WHERE task_id = ?", (str(task_id),)
        ).fetchone()
        if task is None:
            return
        connection.execute(
            """
            INSERT INTO bunshin_v2_tasks_fts(task_id, title, objective, workspace)
            VALUES (?, ?, ?, ?)
            """,
            (
                str(task["task_id"]),
                jieba_fts_text(task["title"]),
                jieba_fts_text(task["objective"]),
                jieba_fts_text(task["workspace_key"]),
            ),
        )

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.db_path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def write_connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
