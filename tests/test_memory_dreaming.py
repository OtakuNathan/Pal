from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pal.llm.ir import LLMMessageIR, LLMResponseIR, TextPartIR
from pal.memory.contracts import L3CommitRequest
from pal.memory.dreaming.contracts import DreamingConfig
from pal.memory.dreaming.contracts import ClusterResult
from pal.memory.dreaming.clustering import Cluster, discover_clusters
from pal.memory.dreaming.pipeline import DreamingPipeline, validate_result
from pal.memory.dreaming.service import DreamingService
from pal.memory.dreaming.runtime import DreamingEventSource
from pal.memory.embedding import HashingEmbedder
from pal.memory.service import MemoryService
from pal.memory.storage import MemoryStorage
from pal.plugins.l3.sqlite_vec import SQLiteVecL3Plugin


class ReviewingLLM:
    active_endpoint_id = "test"

    def __init__(self, *, merge=True, approve=True, fail_review=False, issues=()):
        self.merge, self.approve, self.fail_review = merge, approve, fail_review
        self.issues = list(issues)
        self.calls = []

    async def agenerate(self, request):
        self.calls.append(request)
        payload = json.loads(request.messages[-1].text)
        if "candidate" in payload:
            if self.fail_review:
                raise RuntimeError("review endpoint failed")
            output = {"approved": self.approve, "issues": self.issues}
        else:
            output = {"clusters": []}
            for cluster in payload["clusters"]:
                refs = [doc["document_id"] for doc in cluster["members"]]
                output["clusters"].append({"cluster_id": cluster["cluster_id"],
                    "keep_refs": [] if self.merge else refs,
                    "merges": [{"source_refs": refs, "entry": {
                        "title": "Explicit interfaces", "summary": "Nathan prefers explicit public APIs.",
                        "search_text": "Nathan explicit APIs interfaces", "topics": ["API"]},
                        "reason": "Same preference and scope"}] if self.merge else [], "notes": []})
        return SimpleNamespace(response=LLMResponseIR(message=LLMMessageIR(role="assistant",
            parts=(TextPartIR(text=json.dumps(output)),)), finish_reason="stop"))


class DreamingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = MemoryStorage(Path(self.temp.name))
        self.storage.create_initial()
        self.provider = SQLiteVecL3Plugin(service=MemoryService(), repository=self.storage.open(), embedder=HashingEmbedder())
        request = L3CommitRequest(kind="fact", title="Explicit APIs", summary="Nathan prefers explicit public APIs.",
                                  search_text="Nathan explicit APIs interfaces", topics=["API"])
        self.refs = [self.provider.commit(request).document_id,
                     self.provider.commit(replace(request, title="API design")).document_id]
        self.head = self.storage.current()

    def tearDown(self):
        self.provider.repository.close()
        self.temp.cleanup()

    async def run_dream(self, **options):
        llm = ReviewingLLM(**options)
        service = DreamingService(storage=self.storage, provider=self.provider, llm=llm, config=DreamingConfig())
        result = await service.run()
        return result, llm

    async def test_no_changes_keeps_ids_and_generation_without_review(self):
        result, llm = await self.run_dream(merge=False)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["report"]["outcome"], "no_changes")
        self.assertEqual(self.storage.current(), self.head)
        self.assertEqual(len(llm.calls), 1)
        with self.storage.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM generations").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM archive_records").fetchone()[0], 0)

    async def test_monthly_schedule_coalesces_missed_months_and_suppresses_active_deadline(self):
        service = DreamingService(storage=self.storage, provider=self.provider, llm=ReviewingLLM(),
            config=DreamingConfig(enabled=True))
        source = DreamingEventSource(service)
        with patch("pal.memory.dreaming.runtime.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 5, 2, tzinfo=timezone.utc)
            self.assertFalse(source.prepare(None))
            clock.now.return_value = datetime(2026, 9, 11, tzinfo=timezone.utc)
            self.assertTrue(source.prepare(None))
            with patch.object(service, "create_run", wraps=service.create_run) as create:
                events = source.drain(None)
                self.assertEqual(create.call_args.kwargs["slot"], "2026-08-31T19:00:00+00:00")
            self.assertEqual(len(events), 1)
            self.assertFalse(source.prepare(None))
            service.task = SimpleNamespace(done=lambda: False)
            self.assertIsNone(source.seconds_until_next_due())

    async def test_case_identity_is_required_even_when_text_is_identical(self):
        request = L3CommitRequest(kind="case", title="Delivery failure", summary="Session closed; reattached",
            search_text="Session closed reattached", situation_text="Session closed", action_text="Reattached")
        refs = [self.provider.commit(request).document_id for _ in range(2)]
        docs = tuple(self.provider.repository.get_document(ref) for ref in refs)
        cluster = Cluster("same-text", "case", docs)
        llm = ReviewingLLM()
        pipeline = DreamingPipeline(llm=llm, config=DreamingConfig(), storage=self.storage)
        result = (await pipeline.process([cluster]))[0]
        self.assertEqual(set(result.keep_refs), set(refs))
        self.assertFalse(llm.calls)
        proposal = ClusterResult.model_validate({"cluster_id": cluster.cluster_id, "keep_refs": [],
            "merges": [{"source_refs": refs, "reason": "Same wording", "entry": {
                "title": "Delivery failure", "summary": request.summary, "search_text": request.search_text,
                "topics": [], "situation_text": request.situation_text, "action_text": request.action_text}}]})
        with self.assertRaisesRegex(ValueError, "shared explicit event identity"):
            validate_result(cluster, proposal)

        for doc in docs:
            doc["payload"]["event_id"] = "one-delivery"
        validate_result(cluster, proposal)
        docs[1]["payload"]["event_id"] = "another-delivery"
        with self.assertRaisesRegex(ValueError, "shared explicit event identity"):
            validate_result(cluster, proposal)

    async def test_incremental_discovery_reuses_neighbors_and_prioritizes_unchecked_pairs(self):
        self.provider.commit(L3CommitRequest(kind="fact", title="API spelling", summary="Explicit APIs",
            search_text="Nathan explicit APIs interfaces", topics=["API"]))
        config = DreamingConfig(max_group_members=2)
        pipeline = DreamingPipeline(llm=ReviewingLLM(merge=False), config=config, storage=self.storage)
        repo = self.provider.repository
        first = discover_clusters(repo, config, storage=self.storage, review_scope=pipeline.config_fingerprint)
        first_pairs = {frozenset(doc["document_id"] for doc in cluster.members) for cluster in first if len(cluster.members) == 2}
        await pipeline.process(first)
        with patch.object(repo, "list_fts_term_candidates", wraps=repo.list_fts_term_candidates) as lexical:
            second = discover_clusters(repo, config, storage=self.storage, review_scope=pipeline.config_fingerprint)
            lexical.assert_not_called()
        second_pairs = {frozenset(doc["document_id"] for doc in cluster.members) for cluster in second if len(cluster.members) == 2}
        self.assertTrue(second_pairs - first_pairs)
        added = self.provider.commit(L3CommitRequest(kind="fact", title="New API note", summary="Explicit APIs",
            search_text="Nathan explicit APIs interfaces", topics=["API"]))
        with patch.object(repo, "list_fts_term_candidates", wraps=repo.list_fts_term_candidates) as lexical:
            third = discover_clusters(repo, config, storage=self.storage, review_scope=pipeline.config_fingerprint)
            self.assertGreater(lexical.call_count, 0)
        self.assertIn(added.document_id, {doc["document_id"] for cluster in third for doc in cluster.members})

    async def test_reviewed_merge_publishes_and_old_reader_keeps_original(self):
        self.storage.pin("old-workflow")
        old = self.storage.open(self.head, read_only=True)
        try:
            result, llm = await self.run_dream()
            self.assertEqual(result["status"], "completed", result)
            self.assertNotEqual(self.storage.current(), self.head)
            self.assertEqual(len(llm.calls), 2)
            self.assertEqual(len(self.provider.repository.list_projection_rows()), 1)
            self.assertEqual(len(old.list_projection_rows()), 2)
            self.assertEqual(self.storage.pin("old-workflow"), self.head)
            self.assertEqual(self.storage.pin("new-workflow"), self.storage.current())
            self.assertEqual(len(self.storage.history(self.refs[0])), 1)
        finally:
            old.close()

    async def test_doubt_preserves_whole_group(self):
        result, _ = await self.run_dream(approve=False)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["report"]["outcome"], "no_changes")
        self.assertEqual(self.storage.current(), self.head)

    async def test_review_with_unresolved_issues_keeps_the_originals(self):
        result, _ = await self.run_dream(approve=True, issues=["The scope may have changed"])
        self.assertEqual(result["report"]["outcome"], "no_changes")
        self.assertEqual(self.storage.current(), self.head)

    async def test_review_failure_never_publishes(self):
        result, _ = await self.run_dream(fail_review=True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.storage.current(), self.head)
        self.assertFalse(self.provider.repository.frozen)

    async def test_candidate_validation_repairs_stale_hash_and_rejects_wrong_dimensions(self):
        self.provider.refresh_indexes(limit=8)
        candidate = self.storage.candidate(self.provider.repository)
        private = replace(self.provider, repository=candidate, service=MemoryService())
        service = DreamingService(storage=self.storage, provider=self.provider, llm=ReviewingLLM())
        clusters = discover_clusters(candidate, DreamingConfig())
        embedding_id = self.refs[0] + ":primary"
        try:
            candidate.Embedding.update(source_text_hash="obsolete").where(candidate.Embedding.embedding_id == embedding_id).execute()
            service._verify(private, clusters, [])
            self.assertNotEqual(candidate.get_embedding(embedding_id).source_text_hash, "obsolete")
            candidate.Vector.update(dimension=0).where(candidate.Vector.embedding_id == embedding_id).execute()
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                service._verify(private, clusters, [])
        finally:
            candidate.close()

    async def test_dry_run_changes_only_the_copy(self):
        service = DreamingService(storage=self.storage, provider=self.provider, llm=ReviewingLLM())
        result = await service.dry_run(Path(self.temp.name) / "offline")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self.storage.current(), self.head)
        self.assertEqual(len(self.provider.repository.list_projection_rows()), 2)

    async def test_dry_run_inherits_tombstones_before_incomplete_physical_cleanup(self):
        self.storage.forget(self.provider.repository, self.refs[0])
        target = Path(self.temp.name) / "offline-deletions"
        service = DreamingService(storage=self.storage, provider=self.provider, llm=ReviewingLLM())
        result = await service.dry_run(target)
        self.assertEqual(result["status"], "completed", result)
        copy = MemoryStorage(target)
        reader = copy.open(read_only=True)
        try:
            self.assertTrue(copy.is_deleted(self.refs[0]))
            self.assertIsNone(reader.get_document(self.refs[0]))
            self.assertEqual(len(reader.list_projection_rows()), 1)
        finally:
            reader.close()

    async def test_failure_before_publication_leaves_current_and_archive_unchanged(self):
        with patch.object(self.storage, "publish", side_effect=OSError("before catalog commit")):
            result, _ = await self.run_dream()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.storage.current(), self.head)
        self.assertEqual(self.storage.history(self.refs[0]), [])

    async def test_committed_publication_with_mount_failure_keeps_ingress_closed(self):
        def admit(provider):
            provider.repository.freeze()
            return True
        core = SimpleNamespace(try_enter_memory_maintenance_async=AsyncMock(side_effect=admit),
            memory_notification_route=lambda: "fixed-route", deliver_memory_notice_async=AsyncMock(return_value=True),
            leave_memory_maintenance_async=AsyncMock())
        service = DreamingService(storage=self.storage, provider=self.provider, llm=ReviewingLLM(), core=core)
        original_open = self.storage.open
        def open_generation(generation_id=None, **kwargs):
            if generation_id is None and self.storage.current() != self.head:
                raise OSError("new connection unavailable")
            return original_open(generation_id, **kwargs)
        with patch.object(service, "_resolve_main_provider"), patch.object(self.storage, "open", side_effect=open_generation):
            with self.assertRaisesRegex(OSError, "new connection unavailable"):
                await service.run()
        self.assertNotEqual(self.storage.current(), self.head)
        self.assertEqual(service.status()["status"], "completed")
        self.assertFalse(service.status()["service_ready"])
        core.leave_memory_maintenance_async.assert_not_awaited()
        self.assertFalse(self.provider.repository.frozen)

    async def test_lost_commit_acknowledgement_recovers_the_published_generation(self):
        publish = self.storage.publish
        def publish_then_fail(*args, **kwargs):
            publish(*args, **kwargs)
            raise OSError("lost acknowledgement")
        with patch.object(self.storage, "publish", side_effect=publish_then_fail):
            result, _ = await self.run_dream()
        self.assertEqual(result["status"], "completed", result)
        self.assertNotEqual(self.storage.current(), self.head)
        self.assertEqual(self.provider.repository.generation_id, self.storage.current())
        self.assertFalse(self.storage.is_frozen(self.head))
        self.assertEqual(len(self.provider.repository.list_projection_rows()), 1)

    async def test_cached_review_is_not_reusable_after_candidate_tampering(self):
        result, _ = await self.run_dream(merge=False)
        self.assertEqual(result["status"], "completed")
        with self.storage.connection(write=True) as connection:
            connection.execute("UPDATE dreaming_batches SET review_json='{}'")
        result, llm = await self.run_dream(merge=False)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(llm.calls), 1)

    async def test_cached_results_cannot_bypass_an_endpoint_change(self):
        llm = ReviewingLLM(merge=False)
        facts = {"endpoint_id": "test", "model_id": "original-model"}
        llm.resolve_endpoint_facts = lambda **kwargs: dict(facts)
        pipeline = DreamingPipeline(llm=llm, config=DreamingConfig(), storage=self.storage)
        clusters = discover_clusters(self.provider.repository, DreamingConfig())
        await pipeline.process(clusters)
        facts["model_id"] = "changed-model"
        with self.assertRaisesRegex(RuntimeError, "endpoint configuration changed"):
            await pipeline.process(clusters)
        self.assertEqual(len(llm.calls), 1)

    async def test_worker_runtime_restarts_with_its_workflow_generation(self):
        from pal.bunshin.runner import BunshinRunner, build_slim_bunshin_runtime
        self.storage.pin("workflow")
        result, _ = await self.run_dream()
        self.assertEqual(result["status"], "completed", result)
        for _ in range(2):
            bundle = build_slim_bunshin_runtime(self.storage.root.parent, llm_authority="none", memory_workflow_id="workflow")
            try:
                self.assertEqual(bundle.memory_generation_id, self.head)
                provider = bundle.memory_service._resolve_l3_provider()
                self.assertIsNotNone(provider.repository.get_document(self.refs[0]))
                self.assertTrue(provider.repository.read_only)
                document = provider.repository.get_document(self.refs[0])
                bundle.memory_service.project_l2_entries([provider._project_entry(document, source_kind="l3_recall", candidate_state="stable")], touch=True)
                owner = SimpleNamespace(_memory_generation_id=bundle.memory_generation_id,
                    _result_memory_service=bundle.memory_service, memory_candidates=[], blocked_kind="",
                    _short_summary=lambda text: text, _artifact_payload=lambda: {})
                payload = BunshinRunner._terminal_payload(owner, "completed", "Done")
                self.assertEqual(payload["memory_generation_id"], self.head)
                self.assertIn(self.refs[0], payload["memory_refs"])
            finally:
                await bundle.close()
