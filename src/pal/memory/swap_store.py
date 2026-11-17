"""Durable migration progress and the single catalog commit point."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from contextlib import closing
from uuid import uuid4
from typing import Any

from pal.foundation import utc_now
import pal.memory.change_capture as change_capture
from pal.memory.repository import MemoryDurableRepository
from pal.memory.embedding_space import read_space
from pal.memory.storage import MemoryStorage
from pal.memory.swap_state import SwapEvent, SwapFacts, SwapPhase, can_publish, reduce_swap


@dataclass
class SwapJob:
    job_id: str
    source_provider: str
    target_provider: str
    source_generation: str
    candidate_generation: str = ""
    phase: SwapPhase = SwapPhase.PREPARING
    cursor: int = 0
    profile: dict[str, str | None] = field(default_factory=dict)
    verified_digest: str = ""
    source_digest: str = ""
    dreaming_run_id: str = ""
    report: dict[str, Any] = field(default_factory=dict)
    indexes_ready: bool = False
    error: str = ""
    paused: bool = False

    @property
    def component(self) -> str:
        return f"dreaming:{self.dreaming_run_id}" if self.dreaming_run_id else f"memory:{self.job_id}"


class SwapStore:
    def __init__(self, storage: MemoryStorage):
        self.storage = storage
        with storage.connection(write=True) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS memory_swap_jobs (
                job_id TEXT PRIMARY KEY, phase TEXT NOT NULL, source_generation TEXT NOT NULL,
                candidate_generation TEXT NOT NULL, state_json TEXT NOT NULL, updated_at TEXT NOT NULL)""")

    def create(self, source_provider: str, target_provider: str, profile: dict[str, str | None],
               *, source_generation: str) -> SwapJob:
        job = SwapJob(f"swap_{uuid4().hex}", source_provider, target_provider,
                      source_generation, profile=dict(profile))
        with self.storage.connection(write=True) as connection:
            head = connection.execute("SELECT generation_id FROM memory_head WHERE singleton=1").fetchone()[0]
            if head != source_generation:
                raise RuntimeError("Memory migration source binding changed")
            row = connection.execute("SELECT provider_id,profile_json FROM generation_bindings WHERE generation_id=?", (head,)).fetchone()
            if row is not None and row[0] != source_provider:
                raise RuntimeError("Source backend does not own the active data generation")
            self._save(connection, job)
        return job

    @staticmethod
    def _save(connection: sqlite3.Connection, job: SwapJob) -> None:
        connection.execute("INSERT OR REPLACE INTO memory_swap_jobs VALUES (?,?,?,?,?,?)", (
            job.job_id, job.phase.value, job.source_generation, job.candidate_generation,
            json.dumps(asdict(job), ensure_ascii=False), utc_now()))

    def save(self, job: SwapJob) -> None:
        with self.storage.connection(write=True) as connection:
            self._save(connection, job)

    def load(self, job_id: str) -> SwapJob:
        with self.storage.connection() as connection:
            row = connection.execute("SELECT state_json FROM memory_swap_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown memory swap job: {job_id}")
        value = json.loads(row[0])
        value["phase"] = SwapPhase(value["phase"])
        return SwapJob(**value)

    def pending(self) -> list[SwapJob]:
        with self.storage.connection() as connection:
            ids = [row[0] for row in connection.execute(
                "SELECT job_id FROM memory_swap_jobs WHERE phase NOT IN ('complete','superseded') ORDER BY updated_at")]
        return [self.load(job_id) for job_id in ids]

    def facts(self, job: SwapJob, source: MemoryDurableRepository | None = None, *, indexed: bool = False) -> SwapFacts:
        head = self.storage.current()
        caught_up = False
        if source is not None:
            caught_up = change_capture.cursor(source.database.connection()) == job.cursor
        return SwapFacts(head == job.source_generation, caught_up,
                         bool(source is not None and source.owns_write_fence),
                         bool(job.candidate_generation and head == job.candidate_generation), indexed)

    def advance(self, job: SwapJob, event: SwapEvent, source: MemoryDurableRepository | None = None, *, indexed: bool = False) -> None:
        job.phase = reduce_swap(job.phase, event, self.facts(job, source, indexed=indexed))
        job.error = ""
        self.save(job)

    def capture(self, job: SwapJob) -> MemoryDurableRepository:
        job.candidate_generation = f"gen_{uuid4().hex}"
        path = self.storage.path(job.candidate_generation)
        with self.storage.connection(write=True) as connection:
            connection.execute("INSERT INTO generations VALUES (?,?,NULL,NULL,0)", (job.candidate_generation, utc_now()))
            self._save(connection, job)
        job.cursor = change_capture.snapshot(self.storage.path(job.source_generation), path)
        # A snapshot can contain a source fence from a competing publication.
        # The candidate is private; source-current CAS still controls publication.
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("UPDATE memory_generation_guard SET writable=1 WHERE singleton=1")
        with self.storage.connection(write=True) as connection:
            self._save(connection, job)
        return self.storage.open(job.candidate_generation)

    def catch_up(self, job: SwapJob, candidate: MemoryDurableRepository) -> set[str]:
        with candidate.write_transaction():
            job.cursor, documents = change_capture.replay(
                self.storage.path(job.source_generation), candidate.database.connection(), job.cursor)
        self.save(job)
        return documents

    def verify(self, job: SwapJob, source: MemoryDurableRepository, candidate: MemoryDurableRepository,
               *, approved_digest: str | None = None) -> None:
        if not source.owns_write_fence:
            raise RuntimeError("Memory verification requires the source write fence")
        if approved_digest is not None and not job.dreaming_run_id:
            raise RuntimeError("Transformed content requires a reviewed Dreaming run")
        self._verify_space(job, candidate)
        job.source_digest = change_capture.content_digest(source.database.connection())
        expected = approved_digest if approved_digest is not None else job.source_digest
        if change_capture.content_digest(candidate.database.connection()) != expected:
            raise RuntimeError("Memory candidate changed content, references or replay receipts")
        if not self.facts(job, source).caught_up:
            raise RuntimeError("Memory candidate has not caught up to the source")
        job.verified_digest = expected
        self.advance(job, SwapEvent.VERIFIED, source)

    def publish(self, job: SwapJob, source: MemoryDurableRepository, candidate: MemoryDurableRepository) -> None:
        if not can_publish(job.phase, self.facts(job, source)) or not job.verified_digest:
            raise RuntimeError("Memory candidate is not authorized to publish")
        self._verify_space(job, candidate)
        if (change_capture.content_digest(source.database.connection()) != job.source_digest
                or change_capture.content_digest(candidate.database.connection()) != job.verified_digest):
            raise RuntimeError("Memory changed after publication verification")
        revisions = candidate.database.execute_sql("SELECT document_id,revision,document_json,successors_json,reason FROM memory_revisions WHERE archived=0").fetchall()
        job.indexes_ready = candidate.inventory()["retryable_embeddings"] == 0
        self.storage.seal(candidate)
        with self.storage.connection(write=True) as connection:
            head = connection.execute("SELECT generation_id FROM memory_head WHERE singleton=1").fetchone()[0]
            phase = connection.execute("SELECT phase FROM memory_swap_jobs WHERE job_id=?", (job.job_id,)).fetchone()[0]
            if head != job.source_generation or phase != SwapPhase.READY.value:
                raise RuntimeError("Memory swap source or job phase changed")
            row = connection.execute("SELECT published_at FROM generations WHERE generation_id=?", (job.candidate_generation,)).fetchone()
            if row is None or row[0] is not None:
                raise RuntimeError("Memory candidate is absent or already published")
            if job.dreaming_run_id:
                if connection.execute("SELECT 1 FROM dreaming_runs WHERE run_id=? AND status='publishing'",
                                      (job.dreaming_run_id,)).fetchone() is None:
                    raise RuntimeError("Dreaming run is not authorized to publish")
                connection.execute("UPDATE dreaming_runs SET status='completed',report_json=?,updated_at=? WHERE run_id=?",
                    (json.dumps(job.report), utc_now(), job.dreaming_run_id))
            self.storage._archive_rows(connection, revisions)
            now = utc_now()
            connection.execute("UPDATE generations SET retired_at=? WHERE generation_id=?", (now, job.source_generation))
            connection.execute("UPDATE generations SET published_at=? WHERE generation_id=?", (now, job.candidate_generation))
            connection.execute("UPDATE memory_head SET generation_id=? WHERE singleton=1", (job.candidate_generation,))
            connection.execute("INSERT INTO generation_bindings VALUES (?,?,?)", (
                job.candidate_generation, job.target_provider, json.dumps(job.profile, sort_keys=True)))
            connection.execute("INSERT OR REPLACE INTO memory_settings VALUES ('active_l3_provider',?)", (job.target_provider,))
            connection.execute("INSERT OR REPLACE INTO memory_settings VALUES ('active_l3_profile',?)", (json.dumps(job.profile, sort_keys=True),))
            post = SwapFacts(False, True, True, True, False)
            job.phase = reduce_swap(job.phase, SwapEvent.COMMIT, post)
            self._save(connection, job)

    def adopt_reviewed_candidate(self, source: MemoryDurableRepository, candidate: MemoryDurableRepository,
                                *, run_id: str, report: dict[str, Any]) -> SwapJob:
        """Enter the same production reducer after semantic review succeeds."""
        profile = self.storage.generation_profile(source.generation_id)
        provider = self.storage.provider_for_generation(source.generation_id)
        job = self.create(provider, provider, profile, source_generation=source.generation_id)
        job.dreaming_run_id = run_id
        job.report = report
        job.candidate_generation = candidate.generation_id
        job.cursor = change_capture.cursor(source.database.connection())
        self.advance(job, SwapEvent.SNAPSHOT, source)
        self.advance(job, SwapEvent.COPIED, source)
        approved = change_capture.content_digest(candidate.database.connection())
        self.verify(job, source, candidate, approved_digest=approved)
        return job

    @staticmethod
    def _verify_space(job: SwapJob, candidate: MemoryDurableRepository) -> None:
        space = read_space(candidate.database.connection())
        if space is None or space.profile != job.profile:
            raise RuntimeError("Candidate embedding identity differs from the declared migration target")
