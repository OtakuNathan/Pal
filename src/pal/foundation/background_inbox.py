"""Durable model-neutral results waiting for the next resident conversation."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping


class BackgroundInbox:
    def __init__(self, runtime_root: Path):
        self.path = runtime_root / "data" / "background-inbox.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("""CREATE TABLE IF NOT EXISTS results (
            workflow_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
            delivery_status TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0
        )""")
        return conn

    def publish(self, workflow_id: str, payload: Mapping[str, Any], *, delivery_status: str) -> None:
        encoded = json.dumps(dict(payload), sort_keys=True, ensure_ascii=False)
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT OR IGNORE INTO results(workflow_id,payload,delivery_status) VALUES(?,?,?)",
                         (workflow_id, encoded, delivery_status))
            conn.execute("UPDATE results SET payload=?, delivery_status=? WHERE workflow_id=? AND consumed=1 AND payload='{}'",
                         (encoded, delivery_status, workflow_id))
            existing = conn.execute("SELECT payload FROM results WHERE workflow_id=?", (workflow_id,)).fetchone()
            if existing is None or existing[0] != encoded:
                raise ValueError("background result identity conflict")

    def update_delivery(self, workflow_id: str, status: str) -> None:
        if not self.path.exists():
            return
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE results SET delivery_status=? WHERE workflow_id=? AND delivery_status!='delivered'",
                         (status, workflow_id))

    def begin_delivery(self, workflow_id: str) -> bool:
        """An interrupted channel send has an unknown external outcome."""
        if not self.path.exists():
            return False
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute("UPDATE results SET delivery_status='sending' WHERE workflow_id=? AND delivery_status IN ('pending','failed')", (workflow_id,))
            if cursor.rowcount == 1:
                return True
            conn.execute("UPDATE results SET delivery_status='unknown' WHERE workflow_id=? AND delivery_status='sending'", (workflow_id,))
            return False

    def pending(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with closing(self._connect()) as conn:
            return [{"workflow_id": row[0], **json.loads(row[1]), "delivery_status": row[2]}
                    for row in conn.execute("SELECT workflow_id,payload,delivery_status FROM results WHERE consumed=0 ORDER BY rowid")]

    def acknowledge(self, workflow_ids: Iterable[str]) -> None:
        with closing(self._connect()) as conn, conn:
            keys = [(key,) for key in workflow_ids]
            conn.executemany("INSERT OR IGNORE INTO results(workflow_id,payload,delivery_status,consumed) VALUES(?,'{}','not_requested',1)", keys)
            conn.executemany("UPDATE results SET consumed=1 WHERE workflow_id=?", keys)
