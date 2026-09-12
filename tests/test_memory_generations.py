from __future__ import annotations

import tempfile
import unittest
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from peewee import SqliteDatabase

from pal.memory.contracts import L3CommitRequest, L3CorrectRequest, L3DeleteRequest, MemoryQuery
from pal.memory.embedding import HashingEmbedder
from pal.memory.repository import MemoryDurableRepository
from pal.memory.service import MemoryService
from pal.memory.storage import MemoryStorage
from pal.plugins.l3.sqlite_vec import SQLiteVecL3Plugin


class MemoryGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.storage = MemoryStorage(self.root)
        self.storage.create_initial()
        self.repo = self.storage.open()
        self.provider = SQLiteVecL3Plugin(service=MemoryService(), repository=self.repo, embedder=HashingEmbedder())

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def fact(self, **values):
        return L3CommitRequest(kind="fact", title="API preference", summary="Explicit APIs", search_text="explicit interfaces", **values)

    def test_independent_repositories_do_not_rebind_source_models(self):
        first = self.provider.commit(self.fact())
        other = self.storage.open("other")
        try:
            second = SQLiteVecL3Plugin(service=MemoryService(), repository=other, embedder=HashingEmbedder())
            second.commit(replace(self.fact(), summary="Other database"))
            self.assertEqual(self.repo.get_document(first.document_id)["summary"], "Explicit APIs")
            self.assertEqual(len(self.repo.list_projection_rows()), 1)
            self.assertEqual(len(other.list_projection_rows()), 1)
            self.assertIsNot(self.repo.Fact, other.Fact)
        finally:
            other.close()

    def test_semantic_similarity_never_overwrites_and_canonical_conflicts_are_scoped(self):
        first = self.provider.commit(self.fact(canonical_key="api"))
        conflict = self.provider.commit(replace(self.fact(canonical_key="api"), summary="No hidden magic"))
        self.assertEqual(conflict.status, "conflict")
        self.assertEqual(conflict.document_id, first.document_id)
        self.assertEqual(self.repo.get_document(first.document_id)["summary"], "Explicit APIs")
        task = self.provider.commit(replace(self.fact(canonical_key="api"), scope="task", task_id="t1"))
        self.assertEqual(task.status, "ok")
        different = self.provider.commit(replace(self.fact(), summary="No hidden magic"))
        self.assertNotEqual(different.document_id, first.document_id)

    def test_idempotency_is_not_content_identity(self):
        request = self.fact(mutation_id="request-1")
        first = self.provider.commit(request)
        self.assertEqual(self.provider.commit(request).document_id, first.document_id)
        self.assertEqual(self.provider.commit(replace(request, summary="Changed")).status, "conflict")
        self.assertNotEqual(self.provider.commit(replace(self.fact(), topics=["new retrieval term"])).document_id, first.document_id)

    def test_cases_without_event_identity_are_not_mechanically_merged(self):
        case = L3CommitRequest(kind="case", summary="Socket closed", situation_text="Disconnected", action_text="Reattach")
        first, second = self.provider.commit(case), self.provider.commit(case)
        self.assertNotEqual(first.document_id, second.document_id)
        identified = replace(case, payload={"event_id": "failure-1"})
        self.assertEqual(self.provider.commit(identified).document_id, self.provider.commit(identified).document_id)

    def test_eviction_of_an_explicit_commit_does_not_create_another_case(self):
        case = L3CommitRequest(kind="case", summary="Session closed", search_text="Session closed", situation_text="Disconnected")
        committed = self.provider.commit(case)
        self.assertEqual(self.provider.service._retire_entries([committed.projected_entry]), 0)
        self.assertEqual(len(self.repo.list_projection_rows()), 1)

    def test_explicit_update_keeps_history_and_rejects_stale_references(self):
        first = self.provider.commit(self.fact())
        request = L3CorrectRequest(document_id=first.document_id, summary="Explicit APIs in public interfaces", mutation_id="update-1")
        updated = self.provider.correct(request)
        self.assertNotEqual(first.document_id, updated.document_id)
        self.assertIsNone(self.repo.get_document(first.document_id))
        self.assertEqual(self.provider.correct(request).document_id, updated.document_id)
        stale = self.provider.correct(replace(request, mutation_id="update-2"))
        self.assertEqual(stale.status, "conflict")
        self.storage.flush_revisions(self.repo)
        history = self.storage.history(updated.document_id)
        self.assertEqual(history[0]["document"]["summary"], "Explicit APIs")
        self.assertEqual(len(self.repo.list_projection_rows()), 1)

    def test_pending_history_is_readable_by_successor_and_search_before_archiving(self):
        first = self.provider.commit(self.fact())
        updated = self.provider.correct(L3CorrectRequest(document_id=first.document_id, summary="Scoped explicit interfaces"))
        self.assertEqual(self.storage.history(updated.document_id), [])
        history = self.storage.history(updated.document_id, repo=self.repo)
        self.assertEqual(history[0]["document"]["document_id"], first.document_id)
        self.assertNotIn("original_index", history[0]["document"])
        self.assertEqual(self.storage.search_archive("Explicit", repo=self.repo)[0]["document"]["document_id"], first.document_id)
        self.storage.forget(self.repo, first.document_id)
        self.assertEqual(self.storage.search_archive("Explicit", repo=self.repo), [])

    def test_failed_workflow_creation_does_not_leak_a_pin_but_committed_creation_does(self):
        from pal.bunshin.memory_binding import workflow_memory_binding
        repository = SimpleNamespace(read_snapshot=lambda *args: None)
        with self.assertRaisesRegex(RuntimeError, "failed"):
            with workflow_memory_binding(self.root, repository, "new-workflow"):
                raise RuntimeError("failed")
        with self.assertRaisesRegex(RuntimeError, "no live"):
            self.storage.pinned("new-workflow")
        with self.assertRaisesRegex(RuntimeError, "lost acknowledgement"):
            with workflow_memory_binding(self.root, repository, "new-workflow"):
                repository.read_snapshot = lambda *args: object()
                raise RuntimeError("lost acknowledgement")
        self.assertEqual(self.storage.pinned("new-workflow"), self.storage.current())

    def test_sealing_refuses_an_incomplete_wal_checkpoint(self):
        original = self.repo.database.execute_sql
        def execute(sql, *args, **kwargs):
            if sql == "PRAGMA wal_checkpoint(TRUNCATE)":
                return SimpleNamespace(fetchone=lambda: (1, 7, 3))
            return original(sql, *args, **kwargs)
        with patch.object(self.repo.database, "execute_sql", side_effect=execute):
            with self.assertRaisesRegex(RuntimeError, "checkpoint did not complete"):
                self.storage.seal(self.repo)

    def test_setup_upgrade_splits_memory_once_and_preserves_legacy_and_configuration(self):
        from pal.wizard.cli import run_setup_upgrade
        first = self.provider.commit(self.fact(topics=["API"]))
        target = self.root / "upgrade-target"
        target.mkdir()
        legacy = target / "pal.sqlite3"
        self.storage.snapshot(self.repo, legacy)
        (target / "config.toml").write_text('[identity]\nname = "Preserve me"\n')
        with sqlite3.connect(legacy) as db:
            db.execute("CREATE TABLE untouched_setting(value TEXT)")
            db.execute("INSERT INTO untouched_setting VALUES ('keep')")
        with patch("pal.llm.schema.migrate_llm_endpoint_schema", return_value=SimpleNamespace(status="current")), \
             patch("pal.web_fetch.schema.migrate_web_fetch_schema", return_value=SimpleNamespace(status="current", archive_path="")), \
             patch("pal.bunshin.cutover.cutover_bunshin_runtime", return_value=SimpleNamespace(status="current", archive_root=None)):
            self.assertEqual(run_setup_upgrade(runtime_root=target), 0)
            self.assertEqual(run_setup_upgrade(runtime_root=target), 0)
        storage = MemoryStorage(target)
        migrated = storage.open()
        try:
            self.assertEqual(migrated.get_document(first.document_id)["topics"], ["API"])
            self.assertEqual(len(migrated.list_projection_rows()), 1)
        finally:
            migrated.close()
        with sqlite3.connect(legacy) as db:
            self.assertEqual(db.execute("SELECT value FROM untouched_setting").fetchone()[0], "keep")
            self.assertEqual(db.execute("SELECT count(*) FROM memory_facts").fetchone()[0], 1)
        self.assertEqual((target / "config.toml").read_text(), '[identity]\nname = "Preserve me"\n')

    def test_setup_upgrade_refuses_a_live_runtime_before_mutation(self):
        from pal.packages.process import runtime_lease
        from pal.wizard.cli import run_setup_upgrade
        (self.root / "pal.sqlite3").touch()
        with runtime_lease(self.root), patch("pal.llm.schema.migrate_llm_endpoint_schema") as migrate:
            self.assertEqual(run_setup_upgrade(runtime_root=self.root), 2)
            migrate.assert_not_called()

    def test_setup_upgrade_also_refuses_a_surviving_bunshin_manager(self):
        from pal.wizard.cli import run_setup_upgrade
        (self.root / "pal.sqlite3").touch()
        with patch("pal.bunshin.ipc.BunshinManagerClient.health_sync", return_value={"ok": True}), \
             patch("pal.llm.schema.migrate_llm_endpoint_schema") as migrate:
            with self.assertRaisesRegex(RuntimeError, "sidecar"):
                run_setup_upgrade(runtime_root=self.root)
            migrate.assert_not_called()

    def test_forgetting_removes_potential_original_text_from_run_reports(self):
        import json
        from pal.memory.dreaming.service import DreamingService
        service = DreamingService(storage=self.storage, provider=self.provider, llm=None)
        ref = self.provider.commit(self.fact()).document_id
        run_id = service.create_run()
        service._phase(run_id, "completed", {"outcome": "no_changes", "input_records": 1,
            "notes": ["Explicit APIs"], "error": "May contain the original text"})
        self.storage.forget(self.repo, ref)
        with self.storage.connection() as connection:
            report = json.loads(connection.execute("SELECT report_json FROM dreaming_runs WHERE run_id=?", (run_id,)).fetchone()[0])
        self.assertEqual(report["input_records"], 1)
        self.assertNotIn("notes", report)
        self.assertNotIn("error", report)
        self.assertTrue(report["details_removed_after_forgetting"])

    def test_usage_does_not_change_content_revision_or_updated_at(self):
        first = self.provider.commit(self.fact())
        before = self.repo.get_document(first.document_id)
        self.repo.bump_usage([first.document_id])
        after = self.repo.get_document(first.document_id)
        self.assertEqual(before["content_revision"], after["content_revision"])
        self.assertEqual(before["updated_at"], after["updated_at"])
        self.assertEqual(after["use_count"], before["use_count"] + 1)

    def test_freeze_blocks_mutations_without_disabling_reads(self):
        first = self.provider.commit(self.fact())
        self.repo.freeze()
        self.assertEqual(self.provider.commit(self.fact()).status, "unavailable")
        self.assertIsNotNone(self.repo.get_document(first.document_id))

    def test_pins_survive_new_catalog_instances_and_cannot_be_reacquired_after_release(self):
        generation = self.storage.pin("workflow-1")
        self.assertEqual(MemoryStorage(self.root).pin("workflow-1"), generation)
        self.storage.release("workflow-1")
        with self.assertRaises(RuntimeError):
            self.storage.pin("workflow-1")

    def test_failed_fence_persistence_releases_writer_lock(self):
        with patch.object(self.storage, "set_frozen", side_effect=RuntimeError("catalog unavailable")):
            with self.assertRaisesRegex(RuntimeError, "catalog unavailable"):
                self.repo.freeze()
        self.assertIsNone(self.repo._frozen_writer)
        self.assertFalse(self.repo.frozen)
        self.assertEqual(self.provider.commit(self.fact()).status, "ok")

    def test_tombstones_apply_to_an_old_database_view(self):
        first = self.provider.commit(self.fact())
        old = self.storage.candidate(self.repo)
        try:
            self.storage.forget(self.repo, first.document_id)
            self.assertIsNone(old.get_document(first.document_id))
        finally:
            old.close()

    def test_legacy_migration_supplies_new_column_defaults_and_is_restartable(self):
        first = self.provider.commit(self.fact(topics=["API"]))
        legacy = self.root / "legacy.sqlite3"
        self.storage.snapshot(self.repo, legacy)
        with sqlite3.connect(legacy) as connection:
            connection.execute("ALTER TABLE memory_facts DROP COLUMN content_revision")
            connection.execute("ALTER TABLE memory_cases DROP COLUMN content_revision")
            connection.execute("ALTER TABLE memory_embeddings DROP COLUMN text_processing_version")
        destination = MemoryStorage(self.root / "migrated-runtime")
        generation = destination.migrate(legacy)
        self.assertEqual(destination.migrate(legacy), generation)
        migrated = destination.open()
        try:
            document = migrated.get_document(first.document_id)
            self.assertEqual(document["content_revision"], 1)
            self.assertEqual(document["summary"], "Explicit APIs")
            self.assertEqual(document["topics"], ["API"])
        finally:
            migrated.close()

    def test_forgetting_cannot_be_undone_by_replaying_a_write(self):
        request = self.fact(mutation_id="retry-me")
        first = self.provider.commit(request)
        self.provider.delete(L3DeleteRequest(document_id=first.document_id))
        replay = self.provider.commit(request)
        self.assertEqual(replay.status, "not_found")
        self.assertEqual(len(self.repo.list_projection_rows()), 0)

    def test_freeze_is_visible_to_an_independent_writer_connection(self):
        other = self.storage.open()
        try:
            self.repo.freeze()
            self.assertTrue(other.frozen)
            with self.assertRaises(PermissionError):
                with other.write_transaction():
                    self.fail("independent writer entered a frozen generation")
        finally:
            self.repo.thaw()
            other.close()
