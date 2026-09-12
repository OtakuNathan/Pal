from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace

from pal.foundation import utc_now
from pal.llm.contracts import LLMPreflightRequest
from pal.llm.conversions import request_ir_from_prompt
from pal.llm.ir import TextPartIR
from pal.memory.contracts import L3CommitRequest
from pal.memory.mutations import _insert, content_hash, retire_document
from pal.memory.dreaming.clustering import Cluster, event_identity, identity, pair_fingerprint
from pal.memory.dreaming.contracts import (
    BatchResult, ClusterResult, COMMON_PROMPT, FACT_PROMPT, CASE_PROMPT,
    REVIEW_PROMPT, ReviewResult,
)


def validate_result(cluster: Cluster, result: ClusterResult) -> None:
    if result.cluster_id != cluster.cluster_id:
        raise ValueError("unknown cluster id")
    members = {doc["document_id"]: doc for doc in cluster.members}
    assigned = list(result.keep_refs)
    for merge in result.merges:
        assigned.extend(merge.source_refs)
        if any(ref not in members for ref in merge.source_refs):
            raise ValueError("merge includes a reference or an unknown primary member")
        sources = [members[ref] for ref in merge.source_refs]
        if len({identity(doc) for doc in sources}) != 1:
            raise ValueError("merge crosses kind, scope or task identity")
        keys = {doc["canonical_key"] for doc in sources if doc.get("canonical_key")}
        if len(keys) > 1:
            raise ValueError("merge contains conflicting canonical identities")
        if cluster.kind == "case":
            events = [event_identity(doc) for doc in sources]
            if not all(events) or len(set(events)) != 1:
                raise ValueError("case merge requires one shared explicit event identity on every source")
            for field in ("situation_text", "task_text", "action_text", "result_text"):
                if any((doc.get("payload") or {}).get(field) for doc in sources) and not getattr(merge.entry, field).strip():
                    raise ValueError("case merge dropped a populated STAR field")
        elif any(getattr(merge.entry, name) for name in ("situation_text", "task_text", "action_text", "result_text")):
            raise ValueError("fact merge contains case fields")
    if len(assigned) != len(set(assigned)) or set(assigned) != set(members):
        raise ValueError("every primary member must be accounted for exactly once")


def keep_result(cluster, note=""):
    return ClusterResult(cluster_id=cluster.cluster_id, keep_refs=[doc["document_id"] for doc in cluster.members], merges=[], notes=[note] if note else [])


class DreamingPipeline:
    def __init__(self, *, llm, config, storage):
        self.llm, self.config, self.storage = llm, config, storage
        endpoint = config.endpoint_id or str(getattr(llm, "active_endpoint_id", "") or "")
        self.endpoints = {"merge": endpoint, "review": config.review_endpoint_id or endpoint}
        self.models = {}
        thinking_snapshot = getattr(llm, "thinking_levels_snapshot", None)
        thinking = thinking_snapshot() if callable(thinking_snapshot) else {}
        self.thinking = {}
        resolver = getattr(llm, "resolve_endpoint_facts", None)
        for purpose, endpoint_id in self.endpoints.items():
            facts = resolver(preferred_endpoint_id=endpoint_id) if callable(resolver) else {}
            self.models[purpose] = dict(facts)
            self.endpoints[purpose] = str(facts.get("endpoint_id") or endpoint_id)
            self.thinking[purpose] = thinking.get(self.endpoints[purpose])
        self.config_fingerprint = content_hash({"config": asdict(config), "algorithm_version": "incremental_seed_v2", "models": self.models,
            "endpoints": self.endpoints, "thinking": self.thinking, "prompts": [COMMON_PROMPT, FACT_PROMPT, CASE_PROMPT, REVIEW_PROMPT],
            "schemas": [BatchResult.model_json_schema(), ReviewResult.model_json_schema()]})
        self.phase_callback = None
        self._input_overhead = {}
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0}

    def fingerprint(self, cluster):
        return content_hash({"input": cluster.payload(), "configuration": self.config_fingerprint})

    def assert_configuration(self):
        resolver = getattr(self.llm, "resolve_endpoint_facts", None)
        if callable(resolver):
            for purpose, endpoint in self.endpoints.items():
                if dict(resolver(preferred_endpoint_id=endpoint)) != self.models[purpose]:
                    raise RuntimeError("dreaming endpoint configuration changed during the run")

    def _cached(self, cluster):
        with self.storage.connection() as connection:
            row = connection.execute("SELECT result_json,review_json FROM dreaming_batches WHERE fingerprint=?", (self.fingerprint(cluster),)).fetchone()
        if row is None:
            return None
        result = ClusterResult.model_validate_json(row[0])
        binding = json.loads(row[1])
        if binding.get("input_fingerprint") != self.fingerprint(cluster) or binding.get("output_hash") != content_hash(result.model_dump()):
            return None
        if result.merges and (not binding.get("review", {}).get("approved") or binding.get("review", {}).get("issues")):
            return None
        validate_result(cluster, result)
        return result

    def _save(self, cluster, result, review):
        refs = [doc["document_id"] for doc in (*cluster.members, *cluster.references)]
        with self.storage.connection(write=True) as connection:
            if set(refs).intersection(self.storage.deleted_refs()):
                return
            connection.execute("INSERT OR REPLACE INTO dreaming_batches VALUES (?,?,?,?,?)", (
                self.fingerprint(cluster), json.dumps(refs), result.model_dump_json(),
                json.dumps({"input_fingerprint": self.fingerprint(cluster), "output_hash": content_hash(result.model_dump()),
                            "review": review}), utc_now(),
            ))
            if not review.get("oversized"):
                for index, left in enumerate(cluster.members):
                    for right in cluster.members[index + 1:]:
                        refs = sorted((left["document_id"], right["document_id"]))
                        connection.execute("INSERT OR REPLACE INTO dreaming_pairs VALUES (?,?,?,?)",
                            (*refs, self.config_fingerprint, pair_fingerprint(left, right)))

    def _request(self, payload, *, kind, review=False, correction=""):
        purpose = "review" if review else "merge"
        schema = ReviewResult if review else BatchResult
        prompt = REVIEW_PROMPT if review else COMMON_PROMPT + (FACT_PROMPT if kind == "fact" else CASE_PROMPT)
        prompt += "\nOutput schema:\n" + json.dumps(schema.model_json_schema())
        if correction:
            prompt += "\nPrevious output was invalid. Correct this structural error: " + correction
        facts = self.models[purpose]
        output = min(self.config.output_tokens, int(facts.get("max_output_tokens") or self.config.output_tokens))
        request = request_ir_from_prompt(messages=[{"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            max_output_tokens=output, temperature=0.0, tools=[], metadata={
                "purpose": "memory_dreaming_" + purpose,
                "preferred_endpoint_id": self.endpoints[purpose],
                "endpoint_fallback_policy": "strict_preferred",
                "timeout_seconds": self.config.request_timeout_seconds,
                "stream_wall_timeout_seconds": self.config.request_timeout_seconds,
                "max_output_recovery_enabled": False,
            })
        return replace(request, policy=replace(request.policy, thinking_level=self.thinking[purpose]),
                       logical_scope_id="memory:dreaming:" + self.config_fingerprint)

    async def _fits(self, payload, *, kind, review=False):
        material_tokens = len(json.dumps(payload, ensure_ascii=False)) / 2
        output_budget = min(self.config.output_tokens, int(self.models["review" if review else "merge"].get("max_output_tokens") or self.config.output_tokens))
        preflight = getattr(self.llm, "apreflight", None)
        if callable(preflight):
            advice = await preflight(LLMPreflightRequest(self._request(payload, kind=kind, review=review)))
            if str(advice.status) != "ready":
                return False
            estimated = getattr(advice, "breakdown", {}).get("estimated_input_tokens")
            if estimated is not None:
                key = (kind, review)
                if key not in self._input_overhead:
                    empty = await preflight(LLMPreflightRequest(self._request({}, kind=kind, review=review)))
                    self._input_overhead[key] = int(empty.breakdown.get("estimated_input_tokens", 0))
                material_tokens = max(0, int(estimated) - self._input_overhead[key]) + 32
        if material_tokens > self.config.input_tokens:
            return False
        # Reproducing faithful case details competes with reasoning for output
        # tokens. Bound merge batches by output capacity as well as context.
        if not review and material_tokens > output_budget * 0.75:
            return False
        return True

    async def _generate(self, payload, *, kind, review=False, correction=""):
        self.assert_configuration()
        if self.phase_callback is not None:
            self.phase_callback("reviewing" if review else "refining")
        request = self._request(payload, kind=kind, review=review, correction=correction)
        async with asyncio.timeout(self.config.request_timeout_seconds):
            stream = getattr(self.llm, "astream", None)
            if callable(stream):
                response = None
                async for update in stream(request):
                    response = update.response
                if response is None:
                    raise RuntimeError("dreaming LLM returned no response")
            else:
                response = (await self.llm.agenerate(request)).response
        self.usage["calls"] += 1
        self.usage["input_tokens"] += response.usage.input_tokens
        self.usage["output_tokens"] += response.usage.output_tokens
        self.usage["cost"] += response.usage.cost
        if str(response.finish_reason) not in {"stop", "end_turn"}:
            raise RuntimeError(f"dreaming LLM failed: {response.finish_reason}")
        return "".join(part.text for part in response.message.parts if isinstance(part, TextPartIR))

    async def process(self, clusters, *, progress=None):
        self.assert_configuration()
        results = {}
        pending = []
        for cluster in clusters:
            cached = self._cached(cluster)
            if cached is not None:
                results[cluster.cluster_id] = cached
            elif cluster.kind == "case" and not any(
                event_identity(left) and event_identity(left) == event_identity(right)
                for index, left in enumerate(cluster.members) for right in cluster.members[index + 1:]
            ):
                result = keep_result(cluster, "No shared explicit event identity; similar incident descriptions remain separate")
                results[cluster.cluster_id] = result
                self._save(cluster, result, {"no_changes": True})
            elif len(cluster.members) < 2:
                result = keep_result(cluster, "No duplicate candidate in this group")
                results[cluster.cluster_id] = result
                self._save(cluster, result, {"no_changes": True})
            else:
                pending.append(cluster)
        while pending:
            first = pending.pop(0)
            batch = [first]
            while pending and len(batch) < self.config.max_clusters_per_request and pending[0].kind == first.kind:
                trial = {"clusters": [item.payload() for item in (*batch, pending[0])]}
                if not await self._fits(trial, kind=first.kind):
                    break
                batch.append(pending.pop(0))
            payload = {"clusters": [item.payload() for item in batch]}
            if not await self._fits(payload, kind=first.kind):
                # No truncation: preserve any oversized records as-is.
                for cluster in batch:
                    result = keep_result(cluster, "Complete group exceeds request budget")
                    results[cluster.cluster_id] = result
                    self._save(cluster, result, {"oversized": True})
                continue
            error = ""
            for attempt in range(3):
                text = await self._generate(payload, kind=first.kind, correction=error)
                try:
                    output = BatchResult.model_validate_json(text)
                    by_id = {item.cluster_id: item for item in output.clusters}
                    if len(by_id) != len(output.clusters) or set(by_id) != {item.cluster_id for item in batch}:
                        raise ValueError("batch must return each supplied cluster exactly once")
                    for cluster in batch:
                        validate_result(cluster, by_id[cluster.cluster_id])
                    break
                except ValueError as exc:
                    # Only structural errors cause correction calls. Provider
                    # failures use the shared LLM runtime's bounded retries.
                    error = str(exc)[:600]
                    if attempt == 2:
                        raise ValueError("dreaming output failed structural validation") from exc
            for cluster in batch:
                result = by_id[cluster.cluster_id]
                members = {doc["document_id"]: doc for doc in cluster.members}
                # Review the exact retrieval text that will be committed.
                for merge in result.merges:
                    sources = [members[ref] for ref in merge.source_refs]
                    merge.entry.search_text = "\n".join(dict.fromkeys([merge.entry.search_text, *[doc["search_text"] for doc in sources]]))
                    merge.entry.topics = sorted(set(merge.entry.topics).union(*(doc["topics"] for doc in sources)))
                review_record = {"no_changes": True}
                if result.merges:
                    review_payload = {"input": cluster.payload(), "candidate": result.model_dump()}
                    if not await self._fits(review_payload, kind=cluster.kind, review=True):
                        result = keep_result(cluster, "Complete transformation exceeds review budget")
                        review_record = {"oversized": True}
                    else:
                        correction = ""
                        for attempt in range(3):
                            text = await self._generate(review_payload, kind=cluster.kind, review=True, correction=correction)
                            try:
                                review = ReviewResult.model_validate_json(text)
                                break
                            except ValueError as exc:
                                if attempt == 2:
                                    raise ValueError("dreaming review failed structural validation") from exc
                                correction = str(exc)[:600]
                        review_record = review.model_dump()
                        if not review.approved or review.issues:
                            result = keep_result(cluster, "Independent review did not confirm a faithful duplicate merge")
                self._save(cluster, result, review_record)
                results[cluster.cluster_id] = result
            if progress is not None:
                progress(len(results), len(clusters), dict(self.usage))
        self.assert_configuration()
        return [results[cluster.cluster_id] for cluster in clusters]


def apply_merges(provider, clusters, results):
    """Apply reviewed replacements only to an unpublished candidate database."""
    repo = provider.repository
    documents = {doc["document_id"]: doc for cluster in clusters for doc in cluster.members}
    replacements = []
    with repo.write_transaction():
        for cluster, result in zip(clusters, results):
            validate_result(cluster, result)
            for merge in result.merges:
                sources = [documents[ref] for ref in merge.source_refs]
                for doc in sources:
                    current = repo.get_document(doc["document_id"])
                    if current is None or current["content_revision"] != doc["content_revision"]:
                        raise RuntimeError("candidate source changed or was deleted")
                first = sources[0]
                keys = {doc["canonical_key"] for doc in sources if doc.get("canonical_key")}
                fields = merge.entry.model_dump()
                payload = {"source_refs": merge.source_refs, "source_payloads": {doc["document_id"]: doc["payload"] for doc in sources}}
                if cluster.kind == "case":
                    payload["event_id"] = event_identity(first)
                request = L3CommitRequest(kind=first["document_kind"], scope=first["scope"], task_id=first["task_id"],
                    canonical_key=next(iter(keys)) if keys else None, payload=payload, **fields)
                created = _insert(provider, request, revision=max(doc["content_revision"] for doc in sources) + 1)
                for doc in sources:
                    retire_document(repo, doc, [created.document_id], reason="dreaming_duplicate_merge")
                replacements.append({"source_refs": merge.source_refs, "new_ref": created.document_id})
    return replacements
