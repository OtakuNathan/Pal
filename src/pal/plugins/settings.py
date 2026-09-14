"""Runtime-local plugin preferences, independent of installed generations."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from pal.foundation import utc_now


class PluginSettings:
    def __init__(self, runtime_root):
        self.path = Path(runtime_root) / "pal.sqlite3"

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS pal_runtime_settings (setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL, updated_at TEXT NOT NULL)")
                yield db
        finally:
            db.close()

    def get(self, key):
        with self.connection() as db:
            row = db.execute("SELECT setting_value FROM pal_runtime_settings WHERE setting_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, key, value):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO pal_runtime_settings (setting_key,setting_value,updated_at) VALUES (?,?,?)", (key, json.dumps(value), utc_now()))

    def delete(self, key):
        with self.connection() as db:
            db.execute("DELETE FROM pal_runtime_settings WHERE setting_key=?", (key,))

    def forget_installation(self, name):
        with self.connection() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_bundles'").fetchone():
                db.execute("DELETE FROM plugin_bundles WHERE plugin_id=?", (name,))

    def installation(self, name):
        with self.connection() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_bundles'").fetchone():
                return None
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM plugin_bundles WHERE plugin_id=?", (name,)).fetchone()
        return dict(row) if row else None
