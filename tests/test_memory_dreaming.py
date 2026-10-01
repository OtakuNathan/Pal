from __future__ import annotations

from pal.bunshin.runner_components.results import Results

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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

    async def test_bucket_merges_small_batches_into_reviewed_parents_before_one_publication(self):
        for i in range(3):
            self.refs.append(self.provider.commit(L3CommitRequest(kind="fact", title=f"API preference {i}",
                summary="Nathan prefers explicit public APIs.", search_text="Nathan explicit APIs interfaces",
                topics=["API"])).document_id)
        original_refs = set(self.refs)
        owner = self
        class ObservingLLM(ReviewingLLM):
            async def agenerate(self, request):
                owner.assertEqual(owner.storage.current(), owner.head)
                owner.assertEqual({r["document_id"] for r in owner.provider.repository.list_projection_rows()}, original_refs)
                return await super().agenerate(request)
        llm = ObservingLLM()
        service = DreamingService(storage=self.storage, provider=self.provider, llm=llm, config=DreamingConfig())
        with patch.object(self.storage, "publish", wraps=self.storage.publish) as publish:
            result = await service.run()
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["report"]["merge_level"], 3)
        self.assertEqual(result["report"]["merged_groups"], 4)
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(len(self.provider.repository.list_projection_rows()), 1)
        payloads = [json.loads(r.messages[-1].text) for r in llm.calls]
        self.assertEqual(sum("candidate" in p for p in payloads), 4)
        parent_members = [d for p in payloads for c in p.get("clusters", []) for d in c["members"]
                          if d["document_id"] not in original_refs]
        self.assertTrue(parent_members)
        self.assertTrue(all(d["payload"].get("source_refs") for d in parent_members))
        with self.storage.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM archive_records").fetchone()[0], 8)

    async def test_parent_receives_both_kept_originals_and_reviewed_child_successors(self):
        for i in range(2):
            self.refs.append(self.provider.commit(L3CommitRequest(kind="fact", title=f"Preference {i}",
                summary="Nathan prefers explicit public APIs.", search_text="Nathan explicit APIs interfaces",
                topics=["API"])).document_id)
        original_refs = set(self.refs)
        class MixedLLM(ReviewingLLM):
            async def agenerate(self, request):
                response = await super().agenerate(request)
                payload = json.loads(request.messages[-1].text)
                if "clusters" in payload and len(payload["clusters"]) == 2:
                    output = json.loads(response.response.message.parts[0].text)
                    kept = output["clusters"][0]
                    kept["keep_refs"] = kept["merges"][0]["source_refs"]
                    kept["merges"] = []
                    response.response = replace(response.response, message=LLMMessageIR(role="assistant",
                        parts=(TextPartIR(text=json.dumps(output)),)))
                return response
        llm = MixedLLM()
        service = DreamingService(storage=self.storage, provider=self.provider, llm=llm, config=DreamingConfig())
        result = await service.run()
        self.assertEqual(result["status"], "completed", result)
        payloads = [json.loads(r.messages[-1].text) for r in llm.calls]
        parent = [p for p in payloads if "clusters" in p][-1]["clusters"][0]["members"]
        self.assertEqual(len(parent), 3)
        self.assertEqual(sum(d["document_id"] in original_refs for d in parent), 2)
        self.assertEqual(sum("candidate" in p for p in payloads), 2)
        self.assertEqual(len(self.provider.repository.list_projection_rows()), 1)

    async def test_oversized_parent_preserves_reviewed_child_merges(self):
        for i in range(2):
            self.refs.append(self.provider.commit(L3CommitRequest(kind="fact", title=f"Preference {i}",
                summary="Nathan prefers explicit public APIs.", search_text="Nathan explicit APIs interfaces",
                topics=["API"])).document_id)
        original_refs = set(self.refs)
        class LimitedParentLLM(ReviewingLLM):
            async def apreflight(self, request):
                payload = json.loads(request.request.messages[-1].text)
                parent = any(d["document_id"] not in original_refs
                             for c in payload.get("clusters", []) for d in c["members"])
                return SimpleNamespace(status="ready", breakdown={"estimated_input_tokens": 20000 if parent else 1000})
        llm = LimitedParentLLM()
        service = DreamingService(storage=self.storage, provider=self.provider, llm=llm, config=DreamingConfig())
        result = await service.run()
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["report"]["merged_groups"], 2)
        self.assertIn("exceeds request budget", str(result["report"]["notes"]))
        self.assertEqual(len(self.provider.repository.list_projection_rows()), 2)
        self.assertEqual(len(llm.calls), 3)  # One leaf batch and two independent reviews.

    async def test_merge_tree_never_combines_separate_buckets(self):
        for i in range(2):
            self.refs.append(self.provider.commit(L3CommitRequest(kind="fact", title=f"Preference {i}",
                summary="Nathan prefers explicit public APIs.", search_text="Nathan explicit APIs interfaces",
                topics=["API"])).document_id)
        docs = tuple(self.provider.repository.get_document(ref) for ref in self.refs)
        buckets = [Cluster("left", "fact", docs[:2]), Cluster("right", "fact", docs[2:])]
        llm = ReviewingLLM()
        service = DreamingService(storage=self.storage, provider=self.provider, llm=llm, config=DreamingConfig())
        with patch.object(service, "_prepare", return_value=buckets):
            result = await service.run()
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["report"]["merge_level"], 1)
        self.assertEqual(len(self.provider.repository.list_projection_rows()), 2)
        self.assertEqual(len(llm.calls), 3)

    async def test_parent_review_failure_leaves_original_generation_unpublished(self):
        for i in range(2):
            self.refs.append(self.provider.commit(L3CommitRequest(kind="fact", title=f"Preference {i}",
                summary="Nathan prefers explicit public APIs.", search_text="Nathan explicit APIs interfaces",
                topics=["API"])).document_id)
        original_refs = set(self.refs)
        class FailingParentLLM(ReviewingLLM):
            async def agenerate(self, request):
                payload = json.loads(request.messages[-1].text)
                if "candidate" in payload and any(d["document_id"] not in original_refs
                        for d in payload["input"]["members"]):
                    raise RuntimeError("parent review unavailable")
                return await super().agenerate(request)
        service = DreamingService(storage=self.storage, provider=self.provider, llm=FailingParentLLM(), config=DreamingConfig())
        result = await service.run()
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("parent review unavailable", result["report"]["error"])
        self.assertEqual(self.storage.current(), self.head)
        self.assertEqual({r["document_id"] for r in self.provider.repository.list_projection_rows()}, original_refs)
        self.assertFalse(self.storage.is_frozen(self.head))
        with self.storage.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM archive_records").fetchone()[0], 0)

    async def test_dreaming_uses_low_reasoning_independent_of_conversation(self):
        for levels, expected in ((["off", "low", "high", "max"], "low"),
                                 (["off", "high"], "off"), (["medium", "high"], "medium")):
            with self.subTest(levels=levels):
                llm = ReviewingLLM()
                llm.thinking_levels_snapshot = lambda: {"test": "max"}
                llm.resolve_endpoint_facts = lambda **kw: {"endpoint_id": "test", "thinking_levels": levels}
                pipeline = DreamingPipeline(llm=llm, config=DreamingConfig(), storage=self.storage)
                for review in (False, True):
                    request = pipeline._request({}, kind="fact", review=review)
                    self.assertEqual(request.policy.thinking_level, expected)

    async def test_request_budget_is_half_effective_output_including_prompt_and_review(self):
        llm = ReviewingLLM()
        llm.resolve_endpoint_facts = lambda **kw: {"endpoint_id": "test", "max_output_tokens": 8000}
        pipeline = DreamingPipeline(llm=llm, config=DreamingConfig(output_tokens=16000), storage=self.storage)
        for review in (False, True):
            with self.subTest(review=review):
                async def preflight(request):
                    payload = json.loads(request.request.messages[-1].text)
                    return SimpleNamespace(status="ready", breakdown={"estimated_input_tokens":
                        1000 if not payload else payload["tokens"]})
                llm.apreflight = preflight
                self.assertTrue(await pipeline._fits({"tokens": 4000}, kind="fact", review=review))
                self.assertFalse(await pipeline._fits({"tokens": 4001}, kind="fact", review=review))
                self.assertEqual(pipeline._request({}, kind="fact", review=review).policy.max_output_tokens, 8000)

    async def test_length_splits_batches_and_reviews_only_complete_outputs(self):
        class LimitedLLM(ReviewingLLM):
            async def agenerate(self, request):
                response = await super().agenerate(request)
                payload = json.loads(request.messages[-1].text)
                if len(payload.get("clusters", [])) > 1:
                    response.response = replace(response.response, finish_reason="length")
                return response

        docs = tuple(self.provider.repository.get_document(ref) for ref in self.refs)
        clusters = [Cluster(f"group-{i}", "fact", tuple(
            dict(doc, document_id=f"{doc['document_id']}-{i}") for doc in docs)) for i in range(4)]
        llm = LimitedLLM()
        pipeline = DreamingPipeline(llm=llm, config=DreamingConfig(), storage=self.storage)
        progress = Mock()
        results = await pipeline.process(clusters, progress=progress)
        payloads = [json.loads(request.messages[-1].text) for request in llm.calls]
        self.assertEqual([len(p["clusters"]) for p in payloads if "clusters" in p], [3, 1, 2, 1, 1, 1])
        self.assertEqual(sum("candidate" in p for p in payloads), 4)
        self.assertEqual([r.cluster_id for r in results], [c.cluster_id for c in clusters])
        self.assertTrue(all(len(r.merges) == 1 for r in results))
        self.assertEqual(pipeline.usage["calls"], 10)
        self.assertEqual(progress.call_args.args[:2], (4, 4))
        await pipeline.process(clusters)
        self.assertEqual(len(llm.calls), 10)

    async def test_single_truncated_group_does_not_block_other_reviewed_merges(self):
        class LimitedLLM(ReviewingLLM):
            async def agenerate(self, request):
                response = await super().agenerate(request)
                payload = json.loads(request.messages[-1].text)
                if any(c["cluster_id"] == "limited" for c in payload.get("clusters", [])):
                    response.response = replace(response.response, finish_reason="length")
                return response

        docs = tuple(self.provider.repository.get_document(ref) for ref in self.refs)
        clusters = [Cluster(name, "fact", tuple(dict(doc, document_id=f"{name}-{i}")
                    for i, doc in enumerate(docs))) for name in ("limited", "healthy")]
        llm = LimitedLLM()
        pipeline = DreamingPipeline(llm=llm, config=DreamingConfig(), storage=self.storage)
        results = await pipeline.process(clusters)
        self.assertEqual(set(results[0].keep_refs), {d["document_id"] for d in clusters[0].members})
        self.assertFalse(results[0].merges)
        self.assertEqual(len(results[1].merges), 1)
        self.assertEqual(len(llm.calls), 4)
        self.assertIsNone(pipeline._cached(clusters[0]))
        self.assertIsNotNone(pipeline._cached(clusters[1]))
        with self.storage.connection() as db:
            pairs = db.execute("SELECT left_ref,right_ref FROM dreaming_pairs").fetchall()
        self.assertEqual([tuple(row) for row in pairs], [("healthy-0", "healthy-1")])

    async def test_truncated_merge_or_review_keeps_originals_without_caching(self):
        for stage in ("merge", "review"):
            with self.subTest(stage=stage):
                class LimitedLLM(ReviewingLLM):
                    async def astream(self, request):
                        response = await super().agenerate(request)
                        payload = json.loads(request.messages[-1].text)
                        if ("candidate" in payload) == (stage == "review"):
                            # Even syntactically complete JSON must be discarded
                            # if the provider marks the response truncated.
                            response.response = replace(response.response, finish_reason="length")
                        yield response

                llm = LimitedLLM()
                service = DreamingService(storage=self.storage, provider=self.provider,
                    llm=llm, config=DreamingConfig())
                result = await service.run()
                self.assertEqual(result["status"], "completed", result)
                self.assertEqual(result["report"]["outcome"], "no_changes")
                self.assertIn("truncated", str(result["report"]["notes"]))
                self.assertEqual(result["report"]["usage"]["calls"], 1 if stage == "merge" else 2)
                self.assertEqual(self.storage.current(), self.head)
                self.assertEqual({r["document_id"] for r in self.provider.repository.list_projection_rows()}, set(self.refs))
                with self.storage.connection() as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM dreaming_batches").fetchone()[0], 0)
                    self.assertEqual(db.execute("SELECT count(*) FROM dreaming_pairs").fetchone()[0], 0)
                # A later run actually calls the model again.
                await service.run()
                self.assertEqual(len(llm.calls), 2 if stage == "merge" else 4)

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
            leave_memory_maintenance_async=AsyncMock(), begin_memory_sleep=Mock())
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
        core.begin_memory_sleep.assert_called_once()
        core.leave_memory_maintenance_async.assert_not_awaited()
        self.assertFalse(self.provider.repository.frozen)

    async def test_sleep_notification_and_failure_recovery_boundaries(self):
        for notice_delivered in (False, True):
            with self.subTest(notice_delivered=notice_delivered):
                def admit(provider):
                    provider.repository.freeze()
                    return True
                states = []
                core = SimpleNamespace(
                    try_enter_memory_maintenance_async=AsyncMock(side_effect=admit),
                    memory_notification_route=lambda: "fixed-route",
                    deliver_memory_notice_async=AsyncMock(return_value=notice_delivered),
                    begin_memory_sleep=lambda: states.append("sleeping"),
                    leave_memory_maintenance_async=AsyncMock(side_effect=lambda: states.append("awake")),
                )
                service = DreamingService(storage=self.storage, provider=self.provider,
                    llm=ReviewingLLM(fail_review=True), core=core)
                with patch.object(service, "_resolve_main_provider"):
                    result = await service.run()
                self.assertEqual(result["status"], "failed")
                self.assertEqual(states, ["sleeping", "awake"] if notice_delivered else ["awake"])
                self.assertEqual(self.storage.current(), self.head)

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
        from pal.bunshin.runner_components.artifacts import Artifacts
        from pal.bunshin.runner_components.memory_results import MemoryResults
        from pal.bunshin.runner_components.status import Status
        from pal.shared import BunshinInvocationPack
        from pal.bunshin.runner_components.runtime_build import build_slim_bunshin_runtime
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
                memory = MemoryResults(run_id="workflow")
                memory.bind_generation(bundle.memory_generation_id, bundle.memory_service)
                results = Results(
                    artifacts=Artifacts(BunshinInvocationPack(invocation_id="workflow")),
                    memory_results=memory, status=Status(),
                )
                payload = results.terminal_payload("completed", "Done")
                self.assertEqual(payload["memory_generation_id"], self.head)
                self.assertIn(self.refs[0], payload["memory_refs"])
            finally:
                await bundle.close()
