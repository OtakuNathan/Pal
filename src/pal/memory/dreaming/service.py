from __future__ import annotations

import asyncio
import json
import hashlib
import math
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

from pal.foundation import utc_now
from pal.memory.service import MemoryService
from pal.memory.storage import MemoryStorage, MemoryDatabase
from pal.memory.repository import MemoryDurableRepository
from pal.memory.dreaming.clustering import discover_clusters, semantic_document
from pal.memory.dreaming.contracts import DreamingConfig
from pal.memory.dreaming.pipeline import DreamingPipeline, apply_merges



class DreamingService:
    def __init__(self, *, storage, provider, llm, config=None, core=None):
        self.storage = storage
        self.provider = provider
        self.llm = llm
        self.config = config or DreamingConfig()
        self.core = core
        self.task = None
        self.run_id = None
        self.on_ready = None
        self.activation_error = ""

    def status(self, run_id=None):
        with self.storage.connection() as connection:
            row = (connection.execute("SELECT * FROM dreaming_runs WHERE run_id=?", (run_id,)).fetchone() if run_id
                   else connection.execute("SELECT * FROM dreaming_runs ORDER BY created_at DESC LIMIT 1").fetchone())
        if row is None:
            return {"status": "not_found" if run_id else "idle", "enabled": self.config.enabled, "generation_id": self.storage.current()}
        return {**{key: row[key] for key in ("run_id", "status", "source_generation", "created_at", "updated_at")},
                "configuration": json.loads(row["config_json"]), "report": json.loads(row["report_json"]),
                "generation_id": self.storage.current(),
                **({"service_ready": False, "activation_error": self.activation_error} if self.activation_error else {})}

    def _phase(self, run_id, phase, report=None):
        with self.storage.connection(write=True) as connection:
            if report is None:
                connection.execute("UPDATE dreaming_runs SET status=?,updated_at=? WHERE run_id=? AND status!='completed'", (phase, utc_now(), run_id))
            else:
                connection.execute("UPDATE dreaming_runs SET status=?,report_json=?,updated_at=? WHERE run_id=? AND status!='completed'", (phase, json.dumps(report), utc_now(), run_id))

    async def _work(self, function, *args, **kwargs):
        # Cancelling an asyncio waiter does not stop its worker thread. Join it
        # before closing connections or deciding whether publication committed.
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            except Exception:
                pass
            raise

    def create_run(self, *, slot=None):
        with self.storage.connection(write=True) as connection:
            active = connection.execute("SELECT run_id FROM dreaming_runs WHERE status NOT IN ('completed','failed')").fetchone()
            if active:
                return str(active[0])
            if slot:
                existing = connection.execute("SELECT run_id FROM dreaming_runs WHERE slot=?", (slot,)).fetchone()
                if existing:
                    return str(existing[0])
            run_id = "dream_" + uuid4().hex
            connection.execute("INSERT INTO dreaming_runs VALUES (?,?,?,?,?,?,?,?)", (
                run_id, slot, "scheduled", self.storage.current(), json.dumps(asdict(self.config)), "{}", utc_now(), utc_now(),
            ))
        return run_id

    def start(self, *, slot=None, resume=None, dry_run=False):
        if self.task is not None and not self.task.done():
            return {"status": "running", "run_id": self.run_id}
        if dry_run:
            target = self.storage.root / "dry_runs" / uuid4().hex
            self.task = asyncio.create_task(self.dry_run(target), name="memory-dreaming-dry-run")
            self.task.add_done_callback(self._task_done)
            return {"status": "started", "dry_run_root": str(target)}
        if resume:
            with self.storage.connection() as connection:
                row = connection.execute("SELECT status FROM dreaming_runs WHERE run_id=?", (resume,)).fetchone()
                active = connection.execute("SELECT run_id FROM dreaming_runs WHERE status NOT IN ('completed','failed')").fetchone()
            if row is None:
                return {"status": "not_found", "run_id": resume}
            if active and active[0] != resume:
                return {"status": "running", "run_id": active[0]}
        run_id = resume or self.create_run(slot=slot)
        if self.status(run_id)["status"] == "completed":
            return self.status(run_id)
        self.run_id = run_id
        self.task = asyncio.create_task(self.run(run_id), name="memory-dreaming")
        self.task.add_done_callback(self._task_done)
        return {"status": "started", "run_id": run_id}

    def _task_done(self, task):
        # run() records failures; consume the task result without leaking a
        # second unhandled exception through the resident loop.
        if not task.cancelled():
            task.exception()
        if self.on_ready:
            self.on_ready()

    def _prepare(self, run_id, review_scope):
        source_path = self.storage.root / "runs" / run_id / "source.sqlite3"
        self.storage.snapshot(self.provider.repository, source_path)
        db = MemoryDatabase(str(source_path), check_same_thread=False)
        repo = MemoryDurableRepository(db)
        # This is a private backup, not another writer to the source
        # generation. Snapshot deletion state; final inputs are rebuilt after
        # admission and candidate validation checks catalog tombstones again.
        repo.excluded_refs = self.storage.deleted_refs()
        repo.generation_id = self.provider.repository.generation_id
        repo.ensure_schema()
        prepared_provider = replace(self.provider, repository=repo, service=MemoryService(), read_only=False)
        try:
            # Index preparation is program work. Provider outages can fall back
            # to existing lexical/topic discovery, without sleeping the host.
            while repo.list_pending_embeddings(limit=8):
                refreshed = prepared_provider.refresh_indexes(limit=8)
                if not refreshed.get("refreshed"):
                    break
            return discover_clusters(repo, self.config, storage=self.storage, review_scope=review_scope)
        finally:
            repo.close()

    def _source_matches(self, clusters):
        expected = {doc["document_id"]: semantic_document(doc) for cluster in clusters for doc in cluster.members}
        actual = {}
        repo = self.provider.repository
        for row in repo.list_projection_rows():
            document = repo.get_document(row["document_id"])
            if document is not None:
                actual[document["document_id"]] = semantic_document(document)
        return actual == expected

    def _resolve_main_provider(self):
        if self.core is None:
            return
        memory = self.core.context.require_port("memory:memory")
        provider = memory._resolve_l3_provider()
        catalog = getattr(getattr(provider, "repository", None), "catalog", None)
        if catalog is None or catalog.catalog_path != self.storage.catalog_path or not provider.mounted:
            raise RuntimeError("the active L3 provider does not own this dreaming storage")
        self.provider = provider

    async def run(self, run_id=None):
        run_id = run_id or self.create_run()
        if self.status(run_id).get("run_id") != run_id:
            raise ValueError("unknown dreaming run")
        self.run_id = run_id
        acquired = False
        sleeping = False
        route = None
        source = self.provider.repository
        candidate = None
        pipeline = None
        report = {"outcome": "failed", "processed_groups": 0, "merged_groups": 0}
        try:
            self._resolve_main_provider()
            source = self.provider.repository
            pipeline = DreamingPipeline(llm=self.llm, config=self.config, storage=self.storage)
            with self.storage.connection(write=True) as connection:
                connection.execute("UPDATE dreaming_runs SET config_json=?,source_generation=? WHERE run_id=?",
                    (json.dumps(asdict(self.config)), source.generation_id, run_id))
            report.update(endpoints=pipeline.endpoints, config_fingerprint=pipeline.config_fingerprint)
            self._phase(run_id, "preprocessing", report)
            clusters = await self._work(self._prepare, run_id, pipeline.config_fingerprint)
            self._phase(run_id, "waiting_idle")
            if self.core is not None:
                while True:
                    self._resolve_main_provider()
                    if await self.core.try_enter_memory_maintenance_async(self.provider):
                        break
                    await self.core.wait_for_memory_maintenance_async()
                acquired = True
                route = self.core.memory_notification_route()
                if route is None:
                    raise RuntimeError("no user notification route is available")
            else:
                source.freeze()
                acquired = True
            # Rebuild affected input after the write fence, including additions
            # and deletions since preprocessing. Cached semantic work is keyed
            # by the full input/configuration fingerprint.
            source = self.provider.repository
            if not await self._work(self._source_matches, clusters):
                clusters = await self._work(self._prepare, run_id, pipeline.config_fingerprint)
            with self.storage.connection(write=True) as connection:
                connection.execute("UPDATE dreaming_runs SET source_generation=? WHERE run_id=?", (source.generation_id, run_id))
            if self.core is not None:
                if not await self.core.deliver_memory_notice_async(route, "Pal 开始 dreaming，正在整理重复记忆。期间暂不接收消息，醒来后会通知你。"):
                    raise RuntimeError("dreaming notification delivery failed")
            sleeping = True
            if self.core is not None:
                self.core.begin_memory_sleep()
            self._phase(run_id, "sleeping")
            report.update({"input_records": sum(len(item.members) for item in clusters), "groups": len(clusters),
                           "endpoints": pipeline.endpoints, "config_fingerprint": pipeline.config_fingerprint})
            self._phase(run_id, "refining", report)
            pipeline.phase_callback = lambda phase: self._phase(run_id, phase, report)
            def progress(done, total, usage):
                report.update(processed_groups=done, groups=total, usage=usage)
                self._phase(run_id, "refining", report)
            results = await pipeline.process(clusters, progress=progress)
            self._phase(run_id, "reviewing", report)
            report.update(usage=pipeline.usage, processed_groups=len(clusters),
                          merged_groups=sum(len(result.merges) for result in results),
                          notes=[{"cluster_id": result.cluster_id, "notes": result.notes} for result in results if result.notes])
            if not report["merged_groups"]:
                await self._work(self.storage.flush_revisions, source)
                report["outcome"] = "no_changes"
                self._phase(run_id, "completed", report)
                return self.status(run_id)
            self._phase(run_id, "verifying", report)
            candidate = await self._work(self.storage.candidate, source)
            private_provider = replace(self.provider, repository=candidate, service=MemoryService(), read_only=False)
            replacements = await self._work(apply_merges, private_provider, clusters, results)
            await self._work(self._verify, private_provider, clusters, replacements)
            pipeline.assert_configuration()
            report.update(outcome="merged", replacements=replacements, generation_id=candidate.generation_id)
            self._phase(run_id, "publishing", report)
            # Publication is one catalog transaction. No ordinary reader can
            # bind the candidate until this commits with completed.
            await self._work(self.storage.publish, candidate, source_generation=source.generation_id, run_id=run_id, report=report)
            source.close()
            self.provider.repository = self.storage.open()
            from pal.bunshin.memory_binding import release_retired_workflow_pins
            await self._work(release_retired_workflow_pins, self.storage.root.parent, self.storage)
            await self._work(self.storage.collect, retention_days=self.config.retention_days)
            return self.status(run_id)
        except BaseException as exc:
            # A committed publication is authoritative even if acknowledgement
            # or local reconnection failed. Never repeat it or silently undo it.
            if pipeline is not None:
                report["usage"] = pipeline.usage
            if self.status(run_id)["status"] != "completed":
                report["error"] = f"{type(exc).__name__}: {exc}"
                self._phase(run_id, "failed", report)
            elif self.provider.repository.generation_id != self.storage.current():
                self.provider.repository.close()
                self.provider.repository = self.storage.open()
            if isinstance(exc, asyncio.CancelledError):
                raise
            return self.status(run_id)
        finally:
            if candidate is not None:
                candidate.close()
            if acquired:
                source.thaw()
                self.provider.repository.thaw()
                if self.provider.repository.generation_id != self.storage.current():
                    self.activation_error = "Published memory generation could not be mounted; external restart required."
                else:
                    self.activation_error = ""
                    self.provider.service.clear_generation_projection()
                    if self.core is not None:
                        await self.core.leave_memory_maintenance_async()
            if sleeping and self.core is not None:
                status = self.status(run_id)
                outcome = status.get("report", {}).get("outcome")
                text = ("记忆新版本已发布，但连接尚未恢复；Pal 仍处于维护状态，需要外部完整重启。" if self.activation_error
                        else "Pal 睡醒了。本次未确认可安全合并的重复记忆。" if outcome == "no_changes"
                        else "Pal 睡醒了，重复记忆整理已完成。" if status["status"] == "completed"
                        else "Pal 睡醒了。本次整理未完成，继续使用原记忆库。")
                await self.core.deliver_memory_notice_async(route, text, require_provider=False)

    def _verify(self, provider, clusters, replacements):
        repo = provider.repository
        expected = {doc["document_id"] for cluster in clusters for doc in cluster.members}
        for replacement in replacements:
            expected.difference_update(replacement["source_refs"])
            expected.add(replacement["new_ref"])
        actual = {row["document_id"] for row in repo.list_projection_rows()}
        if actual != expected or actual.intersection(self.storage.deleted_refs()):
            raise RuntimeError("candidate coverage or deletion validation failed")
        for ref in actual:
            metadata = repo.get_embedding(f"{ref}:primary")
            document = repo.get_document(ref)
            source_hash = hashlib.sha256(document["search_text"].encode("utf-8")).hexdigest() if document else None
            if metadata is None or metadata.source_text_hash != source_hash:
                provider._mark_document_pending(ref)
        provider.refresh_indexes(limit=8)
        while repo.list_pending_embeddings(limit=8):
            result = provider.refresh_indexes(limit=8)
            if not result.get("refreshed"):
                raise RuntimeError("candidate embedding preparation failed")
        if repo.list_failed_embeddings(limit=1):
            raise RuntimeError("candidate has failed embeddings")
        for ref in actual:
            document = repo.get_document(ref)
            if not document or not document["summary"].strip() or not document["search_text"].strip():
                raise RuntimeError("candidate contains an invalid document")
            for metadata in repo.Embedding.select().where(repo.Embedding.document_id == ref):
                blob = repo.get_vector_blob(metadata.embedding_id)
                if metadata.index_status != "ready" or blob is None:
                    raise RuntimeError("candidate index is incomplete")
                from pal.memory.repository import deserialize_vector
                vector = deserialize_vector(blob)
                profile = provider._embedding_profile()
                if (metadata.source_text_hash != hashlib.sha256(document["search_text"].encode("utf-8")).hexdigest()
                        or metadata.provider_id != provider._embedding_provider_id()
                        or metadata.model_name != provider._embedding_model_name()
                        or any(getattr(metadata, key) != value for key, value in profile.items())
                        or not vector or not all(math.isfinite(value) for value in vector)
                        or repo.Vector.get_by_id(metadata.embedding_id).dimension != len(vector)):
                    raise RuntimeError("candidate vector does not match its document or embedding profile")
                repo.ensure_vec_index_synced(provider_id=metadata.provider_id, model_name=metadata.model_name,
                                             dimension=len(vector))
            from pal.shared.text_search import jieba_search_terms
            if ref not in repo.list_fts_term_candidates(jieba_search_terms(document["search_text"])[:24], limit=max(12, len(actual))):
                raise RuntimeError("candidate lexical retrieval failed")
        if repo.database.execute_sql("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("candidate integrity check failed")
        private = self.storage.open(repo.generation_id, read_only=True)
        try:
            reader = replace(provider, repository=private, service=MemoryService(), read_only=True)
            for ref in actual:
                if reader.repository.get_document(ref) is None:
                    raise RuntimeError("candidate private read failed")
        finally:
            private.close()

    async def dry_run(self, target_root: Path):
        storage = MemoryStorage(target_root)
        storage.deletion_source = self.storage
        storage.initialize()
        source_path = storage.root / "source.sqlite3"
        await self._work(self.storage.snapshot, self.provider.repository, source_path)
        await self._work(storage.migrate, source_path)
        await self._work(storage.inherit_deletions, self.storage)
        provider = replace(self.provider, repository=storage.open(), service=MemoryService(), read_only=False)
        try:
            await self._work(storage.purge_forgotten, provider.repository, storage.deleted_refs())
            service = DreamingService(storage=storage, provider=provider, llm=self.llm, config=self.config)
            result = await service.run()
            report_path = storage.root / "report.json"
            report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            comparison_path = storage.root / "comparison.json"
            comparison_path.write_text(json.dumps(service.comparison(result.get("run_id")), ensure_ascii=False, indent=2), encoding="utf-8")
            return {"dry_run": True, "report_path": str(report_path), "comparison_path": str(comparison_path), **result}
        finally:
            provider.repository.close()

    def comparison(self, run_id=None):
        status = self.status(run_id)
        changes = []
        for replacement in status.get("report", {}).get("replacements", []):
            originals = []
            for ref in replacement["source_refs"]:
                originals.extend({key: value for key, value in item["document"].items() if key != "original_index"}
                    for item in self.storage.history(ref) if item["document"]["document_id"] == ref)
            changes.append({"before": originals, "after": self.provider.repository.get_document(replacement["new_ref"])})
        return {"run_id": status.get("run_id"), "generation_id": status.get("generation_id"),
                "outcome": status.get("report", {}).get("outcome"), "changes": changes}

    async def shutdown(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
