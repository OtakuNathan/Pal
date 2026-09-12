from __future__ import annotations

from dataclasses import dataclass
import json
from collections import Counter, defaultdict

from pal.memory.mutations import canonical_content, content_hash, document_request
from pal.memory.repository import deserialize_vector
from pal.shared.text_search import jieba_search_terms


def semantic_document(document: dict) -> dict:
    return {"document_id": document["document_id"], "content_revision": document["content_revision"],
            "created_at": document.get("created_at"), "updated_at": document.get("updated_at"),
            **canonical_content(document_request(document))}


def identity(document):
    return document["document_kind"], document["scope"], document.get("task_id")


def event_identity(document):
    payload = document.get("payload") or {}
    event = payload.get("event_id") or payload.get("source_event_id")
    return event if isinstance(event, str) and event.strip() else None


@dataclass(frozen=True)
class Cluster:
    cluster_id: str
    kind: str
    members: tuple[dict, ...]
    references: tuple[dict, ...] = ()

    def payload(self):
        return {"cluster_id": self.cluster_id, "kind": self.kind,
                "members": [semantic_document(doc) for doc in self.members],
                "references": [semantic_document(doc) for doc in self.references]}


def pair_fingerprint(left, right):
    return content_hash([semantic_document(doc) for doc in sorted((left, right), key=lambda doc: doc["document_id"])])


def discover_clusters(repo, config, *, storage=None, review_scope="") -> list[Cluster]:
    documents = {row["document_id"]: repo.get_document(row["document_id"]) for row in repo.list_projection_rows()}
    documents = {ref: doc for ref, doc in documents.items() if doc is not None}
    terms = {ref: jieba_search_terms(doc["search_text"]) for ref, doc in documents.items()}
    frequency = Counter(term for values in terms.values() for term in set(values))
    canonical = defaultdict(list)
    for ref, doc in documents.items():
        if doc.get("canonical_key"):
            canonical[(*identity(doc), doc["canonical_key"])].append(ref)
    vectors = {}
    for metadata in repo.Embedding.select().where(repo.Embedding.index_status == "ready"):
        blob = repo.get_vector_blob(metadata.embedding_id)
        if blob is not None:
            vectors[metadata.document_id] = metadata, deserialize_vector(blob)
    cached = {}
    checked_pairs = {}
    if storage is not None:
        with storage.connection() as connection:
            cached = {ref: (fingerprint, json.loads(neighbors)) for ref, fingerprint, neighbors in connection.execute("SELECT * FROM dreaming_neighbors")}
            checked_pairs = {(left, right): fingerprint for left, right, fingerprint in connection.execute(
                "SELECT left_ref,right_ref,fingerprint FROM dreaming_pairs WHERE scope=?", (review_scope,))}
    fingerprints = {ref: content_hash({"document": semantic_document(doc), "neighbors": config.neighbors,
        "algorithm": "incremental_seed_v2", "index": dict(vectors[ref][0].__data__) if ref in vectors else None})
        for ref, doc in documents.items()}

    def discover(ref):
        doc = documents[ref]
        sources = [
            repo.list_fts_term_candidates(sorted(terms[ref], key=lambda term: (frequency[term], -len(term), term))[:24], limit=config.neighbors),
            repo.list_topic_candidates(doc["topics"], limit=config.neighbors),
        ]
        if ref in vectors:
            metadata, vector = vectors[ref]
            sources.append(repo.query_vector_candidates_sqlite_vec(provider_id=metadata.provider_id,
                model_name=metadata.model_name, query_vector=vector, limit=config.neighbors + 1,
                model_revision=metadata.model_revision, text_processing_version=metadata.text_processing_version) or {})
        scores = {}
        for source in sources:
            for rank, other in enumerate(source):
                if other != ref and other in documents and identity(documents[other]) == identity(doc):
                    scores[other] = scores.get(other, 0.0) + 1.0 / (rank + 1)
        if doc.get("canonical_key"):
            for other in canonical[(*identity(doc), doc["canonical_key"])]:
                if other != ref:
                    scores[other] = scores.get(other, 0.0) + 1
        return sorted(scores, key=lambda other: (-scores[other], other))

    changed = {ref for ref in documents if ref not in cached or cached[ref][0] != fingerprints[ref]}
    removed = (set(cached) | {other for _, values in cached.values() for other in values}) - set(documents)
    neighbors = {ref: discover(ref) for ref in sorted(changed)}
    affected = {ref for ref in documents if ref in cached and set(cached[ref][1]) & (changed | removed)}
    affected.update(other for values in neighbors.values() for other in values)
    # Refresh both old and newly discovered neighborhoods of changed records.
    # Untouched records reuse bounded candidate lists, never previous LLM approval.
    for ref in sorted(set(documents) - changed):
        neighbors[ref] = discover(ref) if ref in affected else [other for other in cached[ref][1] if other in documents]
    if storage is not None:
        with storage.connection(write=True) as connection:
            for ref in removed:
                connection.execute("DELETE FROM dreaming_neighbors WHERE document_id=?", (ref,))
            for ref in documents:
                connection.execute("INSERT OR REPLACE INTO dreaming_neighbors VALUES (?,?,?)",
                    (ref, fingerprints[ref], json.dumps(neighbors[ref])))

    def checked(left, right):
        pair = tuple(sorted((left, right)))
        return checked_pairs.get(pair) == pair_fingerprint(documents[left], documents[right])

    for ref in neighbors:
        neighbors[ref].sort(key=lambda other: checked(ref, other))
    # Seed neighborhoods, not transitive connected components. Each record has
    # exactly one owner regardless of how many neighborhoods mention it.
    order = sorted(documents, key=lambda ref: (not any(not checked(ref, other) for other in neighbors[ref]), ref))
    owned = set()
    clusters = []
    for ref in order:
        if ref in owned:
            continue
        members = [ref, *[other for other in neighbors[ref] if other not in owned][:config.max_group_members - 1]]
        references = [other for other in neighbors[ref] if other not in members][:4]
        owned.update(members)
        groups, group, size = [], [], 0
        for member in members:
            cost = len(json.dumps(semantic_document(documents[member]), ensure_ascii=False)) / 2
            if group and size + cost > config.input_tokens * 0.75:
                groups.append(group)
                group, size = [], 0
            group.append(member)
            size += cost
        if group:
            groups.append(group)
        for group in groups:
            cluster_id = "cluster_" + content_hash(sorted(group))[:16]
            clusters.append(Cluster(cluster_id, documents[ref]["document_kind"],
                tuple(documents[item] for item in group), tuple(documents[item] for item in references)))
    return sorted(clusters, key=lambda cluster: (-sum(int(doc.get("use_count") or 0) for doc in cluster.members), cluster.cluster_id))
