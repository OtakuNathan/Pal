"""Generation-owned SQLite storage and the atomic publication catalog.

The catalog is the only authority for current and workflow pins. Candidate
databases are durable before their paths can be published here.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import fcntl
from functools import wraps
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from peewee import SqliteDatabase

from pal.foundation import utc_now
from pal.memory.repository import MemoryDurableRepository


CATALOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS generations (
 generation_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
 published_at TEXT, retired_at TEXT, collected INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS memory_head (singleton INTEGER PRIMARY KEY CHECK(singleton=1), generation_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workflow_pins (
 workflow_id TEXT PRIMARY KEY, generation_id TEXT NOT NULL,
 released INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deleted_memories (document_id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archive_records (
 document_id TEXT PRIMARY KEY, content_revision INTEGER NOT NULL,
 document_json TEXT NOT NULL, successors_json TEXT NOT NULL,
 reason TEXT NOT NULL, archived_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS archive_fts USING fts5(document_id UNINDEXED, body);
CREATE TABLE IF NOT EXISTS dreaming_runs (
 run_id TEXT PRIMARY KEY, slot TEXT UNIQUE, status TEXT NOT NULL,
 source_generation TEXT, config_json TEXT NOT NULL DEFAULT '{}',
 report_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS dreaming_single_active ON dreaming_runs((1))
 WHERE status NOT IN ('completed','failed');
CREATE TABLE IF NOT EXISTS dreaming_batches (
 fingerprint TEXT PRIMARY KEY, refs_json TEXT NOT NULL,
 result_json TEXT NOT NULL, review_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dreaming_neighbors (
 document_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, neighbors_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dreaming_pairs (
 left_ref TEXT NOT NULL, right_ref TEXT NOT NULL, scope TEXT NOT NULL, fingerprint TEXT NOT NULL,
 PRIMARY KEY(left_ref,right_ref,scope)
);
"""


class MemoryDatabase(SqliteDatabase):
    """Track worker-thread connections so provider detach really closes them."""
    def __init__(self, *args, **kwargs):
        self._owned_connections = set()
        self._connections_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def _connect(self):
        connection = super()._connect()
        with self._connections_lock:
            self._owned_connections.add(connection)
        return connection

    def _close(self, connection):
        with self._connections_lock:
            self._owned_connections.discard(connection)
        return super()._close(connection)

    def close_all(self):
        with self._connections_lock:
            connections = tuple(self._owned_connections)
            self._owned_connections.clear()
        for connection in connections:
            connection.close()
        self._state.reset()


def serialized_initialization(method):
    @wraps(method)
    def invoke(self, *args, **kwargs):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "initialize.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return method(self, *args, **kwargs)
    return invoke


class MemoryStorage:
    def __init__(self, runtime_root: Path, *, read_only=False):
        self.root = Path(runtime_root) / "memory"
        self.catalog_path = self.root / "archive.sqlite3"
        self.read_only = read_only
        self.deletion_source = None

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(CATALOG_SCHEMA)

    @contextmanager
    def connection(self, *, write=False):
        if write and self.read_only:
            raise PermissionError("memory catalog is read-only")
        connection = sqlite3.connect(f"file:{self.catalog_path}?mode=ro" if self.read_only else self.catalog_path,
                                     uri=self.read_only, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def current(self) -> str:
        with self.connection() as connection:
            row = connection.execute("SELECT generation_id FROM memory_head WHERE singleton=1").fetchone()
            if row is None:
                raise RuntimeError("memory storage has not been initialized or migrated")
            return str(row[0])

    def is_frozen(self, generation_id):
        with self.connection() as connection:
            row = connection.execute("SELECT value FROM memory_settings WHERE key='frozen_generation'").fetchone()
        return bool(row and row[0] == generation_id)

    def set_frozen(self, generation_id, frozen):
        with self.connection(write=True) as connection:
            if frozen:
                connection.execute("INSERT OR REPLACE INTO memory_settings VALUES ('frozen_generation',?)", (generation_id,))
            else:
                connection.execute("DELETE FROM memory_settings WHERE key='frozen_generation' AND value=?", (generation_id,))

    def writer_lock(self, generation_id):
        handle = (self.path(generation_id).parent / "writer.lock").open("a+b")
        fcntl.flock(handle, fcntl.LOCK_EX)
        return handle

    def recover_abandoned_fence(self):
        with self.connection() as connection:
            row = connection.execute("SELECT value FROM memory_settings WHERE key='frozen_generation'").fetchone()
        if not row:
            return
        with (self.path(row[0]).parent / "writer.lock").open("a+b") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("memory maintenance is still owned by a running process") from exc
            self.set_frozen(row[0], False)

    def path(self, generation_id: str) -> Path:
        if not generation_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in generation_id):
            raise ValueError("invalid memory generation id")
        return self.root / "generations" / generation_id / "memory.sqlite3"

    def open(self, generation_id: str | None = None, *, read_only=False) -> MemoryDurableRepository:
        generation_id = generation_id or self.current()
        path = self.path(generation_id)
        if read_only and not path.is_file():
            raise FileNotFoundError(path)
        if not read_only:
            path.parent.mkdir(parents=True, exist_ok=True)
        db = MemoryDatabase(
            f"file:{path}?mode=ro" if read_only else str(path),
            uri=read_only, check_same_thread=False,
            pragmas={"foreign_keys": 1, "query_only": 1} if read_only else {"foreign_keys": 1, "journal_mode": "wal"},
        )
        repo = MemoryDurableRepository(db, read_only=read_only)
        repo.generation_id = generation_id
        repo.catalog = self
        lease = (path.parent / "readers.lock").open("rb" if read_only else "a+b")
        fcntl.flock(lease, fcntl.LOCK_SH)
        repo.connection_lease = lease
        if not read_only:
            if self.is_frozen(generation_id):
                repo.close()
                raise PermissionError("cannot reopen a frozen generation for writing")
            repo.ensure_schema()
        return repo

    @serialized_initialization
    def create_initial(self) -> str:
        self.initialize()
        with self.connection() as connection:
            if connection.execute("SELECT 1 FROM memory_head").fetchone():
                return self.current()
        generation = "initial"
        repo = self.open(generation)
        self.seal(repo)
        with self.connection(write=True) as connection:
            connection.execute("INSERT OR IGNORE INTO generations VALUES (?,?,?,NULL,0)", (generation, utc_now(), utc_now()))
            connection.execute("INSERT OR IGNORE INTO memory_head VALUES (1,?)", (generation,))
        return self.current()

    @serialized_initialization
    def migrate(self, legacy_path: Path) -> str:
        """Offline, restartable migration; the legacy database is never rebound."""
        self.initialize()
        with self.connection() as connection:
            row = connection.execute("SELECT generation_id FROM memory_head").fetchone()
        if row:
            return str(row[0])
        generation = "migrated"
        destination = self.open(generation)
        source = sqlite3.connect(f"file:{Path(legacy_path)}?mode=ro", uri=True)
        try:
            with destination.database.atomic():
                for model in (destination.Fact, destination.Case, destination.Topic, destination.Embedding, destination.Vector):
                    table = model._meta.table_name
                    exists = source.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
                    if not exists:
                        continue
                    old_columns = [row[1] for row in source.execute(f'PRAGMA table_info("{table}")')]
                    columns = [name for name in old_columns if name in model._meta.columns]
                    quoted = ",".join(f'"{name}"' for name in columns)
                    rows = source.execute(f'SELECT {quoted} FROM "{table}"').fetchall()
                    # Peewee defaults are applied by model.create, not by raw
                    # INSERT. Supply newly introduced columns during migration.
                    missing = [(name, field.default) for name, field in model._meta.columns.items()
                               if name not in columns and field.default is not None]
                    if missing:
                        columns.extend(name for name, _ in missing)
                        rows = [(*row, *(default() if callable(default) else default for _, default in missing)) for row in rows]
                        quoted = ",".join(f'"{name}"' for name in columns)
                    destination.database.execute_sql(f'DELETE FROM "{table}"')
                    if rows:
                        destination.database.connection().executemany(
                            f'INSERT INTO "{table}" ({quoted}) VALUES ({",".join("?" for _ in columns)})', rows,
                        )
                    count = destination.database.execute_sql(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                    if count != len(rows):
                        raise RuntimeError(f"memory migration count mismatch: {table}")
                    copied = destination.database.execute_sql(f'SELECT {quoted} FROM "{table}"').fetchall()
                    if sorted(map(repr, copied)) != sorted(map(repr, rows)):
                        raise RuntimeError(f"memory migration content mismatch: {table}")
                for table in ("memory_mutations", "memory_revisions"):
                    if source.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                        rows = source.execute(f'SELECT * FROM "{table}"').fetchall()
                        destination.database.execute_sql(f'DELETE FROM "{table}"')
                        if rows:
                            destination.database.connection().executemany(
                                f'INSERT INTO "{table}" VALUES ({",".join("?" for _ in rows[0])})', rows)
            destination.rebuild_fts_indexes()
            self.seal(destination)
        finally:
            source.close()
            destination.close()
        with self.connection(write=True) as connection:
            connection.execute("INSERT OR IGNORE INTO generations VALUES (?,?,?,NULL,0)", (generation, utc_now(), utc_now()))
            connection.execute("INSERT INTO memory_head VALUES (1,?)", (generation,))
            connection.execute("INSERT OR REPLACE INTO memory_settings VALUES ('migration_source',?)", (str(legacy_path),))
        return generation

    def snapshot(self, source: MemoryDurableRepository, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # A backup yields one consistent committed view, including WAL data.
        with source.write_lock:
            target = sqlite3.connect(destination)
            try:
                source.database.connection().backup(target)
            finally:
                target.close()

    def candidate(self, source: MemoryDurableRepository) -> MemoryDurableRepository:
        generation = f"gen_{uuid4().hex}"
        self.snapshot(source, self.path(generation))
        repo = self.open(generation)
        with self.connection(write=True) as connection:
            connection.execute("INSERT INTO generations VALUES (?,?,NULL,NULL,0)", (generation, utc_now()))
        return repo

    @staticmethod
    def seal(repo: MemoryDurableRepository) -> None:
        if repo.database.execute_sql("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("memory database integrity check failed")
        checkpoint = repo.database.execute_sql("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or checkpoint[0] != 0 or checkpoint[1] != checkpoint[2]:
            raise RuntimeError("memory database checkpoint did not complete")
        path = Path(repo.database.database)
        repo.close()
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
        for directory in (path.parent, path.parent.parent):
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def pin(self, workflow_id: str) -> str:
        if not workflow_id:
            raise ValueError("workflow identity is required for a generation pin")
        with self.connection(write=True) as connection:
            existing = connection.execute("SELECT generation_id,released FROM workflow_pins WHERE workflow_id=?", (workflow_id,)).fetchone()
            if existing:
                if existing[1]:
                    raise RuntimeError("released workflow cannot reacquire its memory generation")
                return str(existing[0])
            generation = connection.execute("SELECT generation_id FROM memory_head WHERE singleton=1").fetchone()[0]
            connection.execute("INSERT INTO workflow_pins VALUES (?,?,0,?)", (workflow_id, generation, utc_now()))
            return str(generation)

    def release(self, workflow_id: str) -> None:
        """Caller must have permanently retired the workflow and its readers."""
        with self.connection(write=True) as connection:
            connection.execute("UPDATE workflow_pins SET released=1 WHERE workflow_id=?", (workflow_id,))

    def pinned(self, workflow_id: str) -> str:
        with self.connection() as connection:
            row = connection.execute("SELECT generation_id FROM workflow_pins WHERE workflow_id=? AND released=0", (workflow_id,)).fetchone()
        if row is None:
            raise RuntimeError("logical workflow has no live memory generation pin")
        return str(row[0])

    def deleted_refs(self) -> set[str]:
        with self.connection() as connection:
            refs = {str(row[0]) for row in connection.execute("SELECT document_id FROM deleted_memories")}
        if self.deletion_source is not None:
            refs.update(self.deletion_source.deleted_refs())
        return refs

    def inherit_deletions(self, source) -> None:
        with self.connection(write=True) as connection:
            for ref in source.deleted_refs():
                connection.execute("INSERT OR IGNORE INTO deleted_memories VALUES (?,?)", (ref, utc_now()))

    def is_deleted(self, ref: str) -> bool:
        with self.connection() as connection:
            deleted = connection.execute("SELECT 1 FROM deleted_memories WHERE document_id=?", (ref,)).fetchone() is not None
        return deleted or bool(self.deletion_source is not None and self.deletion_source.is_deleted(ref))

    @staticmethod
    def _archive_rows(connection, rows) -> None:
        from pal.shared.text_search import jieba_fts_text
        for row in rows:
            if connection.execute("SELECT 1 FROM deleted_memories WHERE document_id=?", (row[0],)).fetchone():
                continue
            inserted = connection.execute("INSERT OR IGNORE INTO archive_records VALUES (?,?,?,?,?,?)", (*tuple(row[:5]), utc_now())).rowcount
            if inserted:
                doc = json.loads(row[2])
                text = "\n".join(str(doc.get(key) or "") for key in ("title", "summary", "search_text", "rendered"))
                connection.execute("INSERT INTO archive_fts VALUES (?,?)", (row[0], jieba_fts_text(text)))

    def flush_revisions(self, repo: MemoryDurableRepository) -> None:
        rows = repo.database.execute_sql("SELECT document_id,revision,document_json,successors_json,reason FROM memory_revisions WHERE archived=0").fetchall()
        with self.connection(write=True) as connection:
            self._archive_rows(connection, rows)
        with repo.database.atomic():
            for row in rows:
                repo.database.execute_sql("UPDATE memory_revisions SET archived=1 WHERE document_id=?", (row[0],))

    def publish(self, repo: MemoryDurableRepository, *, source_generation: str, run_id: str, report: dict) -> None:
        rows = repo.database.execute_sql("SELECT document_id,revision,document_json,successors_json,reason FROM memory_revisions WHERE archived=0").fetchall()
        generation = repo.generation_id
        self.seal(repo)
        with self.connection(write=True) as connection:
            head = connection.execute("SELECT generation_id FROM memory_head WHERE singleton=1").fetchone()[0]
            if head != source_generation:
                raise RuntimeError("memory publication source is no longer current")
            if connection.execute("SELECT 1 FROM dreaming_runs WHERE run_id=? AND status='publishing'", (run_id,)).fetchone() is None:
                raise RuntimeError("dreaming run is not authorized to publish")
            self._archive_rows(connection, rows)
            now = utc_now()
            connection.execute("UPDATE generations SET published_at=? WHERE generation_id=?", (now, generation))
            connection.execute("UPDATE generations SET retired_at=? WHERE generation_id=?", (now, source_generation))
            connection.execute("UPDATE memory_head SET generation_id=? WHERE singleton=1", (generation,))
            connection.execute("UPDATE dreaming_runs SET status='completed',report_json=?,updated_at=? WHERE run_id=?", (json.dumps(report), now, run_id))

    def history(self, ref: str, *, repo=None) -> list[dict]:
        if self.is_deleted(ref):
            return []
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM archive_records").fetchall()
        rows = self._with_pending(rows, repo)
        return [self._historical(row) for row in rows if not self.is_deleted(row["document_id"])
                and (row["document_id"] == ref or ref in json.loads(row["successors_json"]))]

    @staticmethod
    def _with_pending(rows, repo):
        rows = {row["document_id"]: dict(row) for row in rows}
        if repo is not None:
            for ref, body, successors, reason in repo.database.execute_sql(
                "SELECT document_id,document_json,successors_json,reason FROM memory_revisions"
            ).fetchall():
                rows.setdefault(ref, {"document_id": ref, "document_json": body,
                                      "successors_json": successors, "reason": reason})
        return list(rows.values())

    @staticmethod
    def _historical(row) -> dict:
        document = json.loads(row["document_json"])
        document.pop("original_index", None)
        return {"document": document, "historical": True,
                "successors": json.loads(row["successors_json"]), "reason": row["reason"]}

    def search_archive(self, query: str, *, limit=8, repo=None) -> list[dict]:
        from pal.shared.text_search import compile_jieba_fts_queries, jieba_search_terms
        hits = {}
        with self.connection() as connection:
            for text, _ in compile_jieba_fts_queries(query):
                for row in connection.execute("SELECT a.* FROM archive_fts f JOIN archive_records a ON a.document_id=f.document_id WHERE archive_fts MATCH ? ORDER BY bm25(archive_fts) LIMIT ?", (text, limit)):
                    if not self.is_deleted(row["document_id"]):
                        hits[row["document_id"]] = self._historical(row)
                if len(hits) >= limit:
                    break
        terms = set(jieba_search_terms(query))
        for row in self._with_pending([], repo):
            if self.is_deleted(row["document_id"]):
                continue
            item = self._historical(row)
            doc = item["document"]
            text = "\n".join(str(doc.get(key) or "") for key in ("title", "summary", "search_text", "rendered"))
            if terms.intersection(jieba_search_terms(text)):
                hits.setdefault(row["document_id"], item)
        return list(hits.values())[:limit]

    def forget(self, repo: MemoryDurableRepository, ref: str) -> set[str]:
        # Only replacement ancestry, never topics or generic related links.
        pending = repo.database.execute_sql("SELECT document_id,successors_json FROM memory_revisions").fetchall()
        with self.connection(write=True) as connection:
            links = [*pending, *connection.execute("SELECT document_id,successors_json FROM archive_records").fetchall()]
            refs = {ref}
            while True:
                expanded = set(refs)
                for old, successors in links:
                    family = {old, *json.loads(successors)}
                    if refs & family:
                        expanded.update(family)
                if expanded == refs:
                    break
                refs = expanded
            for value in refs:
                connection.execute("INSERT OR IGNORE INTO deleted_memories VALUES (?,?)", (value, utc_now()))
                connection.execute("DELETE FROM archive_records WHERE document_id=?", (value,))
                connection.execute("DELETE FROM archive_fts WHERE document_id=?", (value,))
                connection.execute("DELETE FROM dreaming_neighbors WHERE document_id=?", (value,))
                connection.execute("DELETE FROM dreaming_pairs WHERE left_ref=? OR right_ref=?", (value, value))
            for row in connection.execute("SELECT fingerprint,refs_json FROM dreaming_batches").fetchall():
                if refs.intersection(json.loads(row[1])):
                    connection.execute("DELETE FROM dreaming_batches WHERE fingerprint=?", (row[0],))
            # LLM notes may quote originals without machine-readable references.
            # Preserve aggregate diagnostics, not potentially forgotten prose.
            safe_keys = {"outcome", "processed_groups", "merged_groups", "input_records", "groups",
                         "endpoints", "config_fingerprint", "usage", "generation_id"}
            for run_id, body in connection.execute("SELECT run_id,report_json FROM dreaming_runs").fetchall():
                report = {key: value for key, value in json.loads(body).items() if key in safe_keys}
                report["details_removed_after_forgetting"] = True
                connection.execute("UPDATE dreaming_runs SET report_json=? WHERE run_id=?", (json.dumps(report), run_id))
        return refs

    def purge_forgotten(self, repo, refs) -> None:
        # Tombstones commit first. A crash during cleanup cannot resurrect a
        # record, including in a candidate whose processing was interrupted.
        with repo.write_transaction():
            for ref in refs:
                repo.delete_document(ref, physical=True)
        for directory in (self.root / "runs", self.root / "dry_runs"):
            if directory.exists():
                shutil.rmtree(directory)

    def collect(self, *, now=None, retention_days=7) -> list[str]:
        now = now or datetime.now(timezone.utc)
        collected = []
        with self.connection(write=True) as connection:
            head = connection.execute("SELECT generation_id FROM memory_head").fetchone()[0]
            rows = connection.execute("SELECT * FROM generations WHERE published_at IS NOT NULL ORDER BY published_at DESC").fetchall()
            # Always retain current and the immediately preceding published version.
            for row in rows[2:]:
                generation = row["generation_id"]
                if generation == head or row["collected"] or not row["retired_at"]:
                    continue
                if datetime.fromisoformat(row["retired_at"]) + timedelta(days=retention_days) > now:
                    continue
                if connection.execute("SELECT 1 FROM workflow_pins WHERE generation_id=? AND released=0", (generation,)).fetchone():
                    continue
                if not self.path(generation).parent.exists():
                    connection.execute("UPDATE generations SET collected=1 WHERE generation_id=?", (generation,))
                    collected.append(generation)
                    continue
                lease = (self.path(generation).parent / "readers.lock").open("a+b")
                try:
                    try:
                        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    shutil.rmtree(self.path(generation).parent)
                finally:
                    lease.close()
                connection.execute("UPDATE generations SET collected=1 WHERE generation_id=?", (generation,))
                collected.append(generation)
        return collected
