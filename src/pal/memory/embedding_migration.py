"""Offline, resumable embedding rebuild into an unpublished memory generation."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import tomllib
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import Field, field_validator

from pal.core.runtime_config import RuntimeConfig
from pal.execution.tool_facade import StrictToolModel
from pal.foundation import utc_now
from pal.memory.embedding import build_ollama_embedding_provider_from_config
from pal.memory.repository import deserialize_vector
from pal.memory.storage import MemoryStorage
from pal.packages.process import atomic_json


class EmbeddingPatch(StrictToolModel):
    model_name: str | None = Field(None, min_length=1)
    model_revision: str | None = Field(None, min_length=1)
    text_processing_version: str | None = Field(None, min_length=1)
    local_base_url: str | None = None
    remote_base_urls: list[str] | None = None

    @field_validator("local_base_url", "remote_base_urls")
    @classmethod
    def validate_urls(cls, value):
        for url in value if isinstance(value, list) else [value]:
            if url is None:
                continue
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Embedding URLs must be HTTP(S) without embedded credentials")
        return value


CONFIG_KEYS = {
    "model_name": "embedding_ollama_model_name", "model_revision": "embedding_model_revision",
    "text_processing_version": "embedding_text_processing_version",
    "local_base_url": "embedding_ollama_local_base_url", "remote_base_urls": "embedding_ollama_remote_base_urls",
}


def journal_path(root):
    return Path(root) / "memory" / "embedding-migration.json"


def require_completed_migration(root):
    path = journal_path(root)
    if path.exists() and json.loads(path.read_text())["stage"] != "completed":
        raise RuntimeError("Embedding migration is incomplete; keep Pal stopped and run pal memory embedding resume")


def _config_text(root):
    path = Path(root) / "config.toml"
    text = path.read_text() if path.exists() else ""
    tomllib.loads(text)  # Do not silently migrate using defaults on malformed configuration.
    return text


def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _target_config(root, patch):
    values = {CONFIG_KEYS[key]: tuple(value) if key == "remote_base_urls" else value
              for key, value in patch.items() if value is not None}
    return replace(RuntimeConfig.load(root), **values)


def _identity(config):
    provider = build_ollama_embedding_provider_from_config(config)
    return {"provider_id": provider.provider_id, "model_name": provider.model_name,
            "model_revision": getattr(provider, "model_revision", None),
            "text_processing_version": getattr(provider, "text_processing_version", "search_text_v1")}


def _storage(root, *, read_only=False):
    storage = MemoryStorage(root, read_only=read_only)
    if not storage.catalog_path.exists():
        raise RuntimeError("Memory generation storage is absent; run pal setup --upgrade with Pal stopped")
    return storage


def inspect_offline(root, name=None):
    if name not in (None, "sqlite_vec_l3"):
        raise ValueError("Offline inspection supports sqlite_vec_l3; other providers require the resident owner")
    _config_text(root)
    storage = _storage(root, read_only=True)
    repo = storage.open(read_only=True)
    try:
        return {"status": "ok", "activation": "on_start", "state": {
            "provider_id": "sqlite_vec_l3", "mounted": False, "source": "offline_storage",
            "generation_id": repo.generation_id, "embedding_identity": _identity(RuntimeConfig.load(root)),
            "inventory": repo.inventory(), "migration": _public_state(_read_journal(root))}}
    finally:
        repo.close()


def _read_journal(root):
    path = journal_path(root)
    return json.loads(path.read_text()) if path.exists() else None


def _public_state(state):
    return {key: value for key, value in (state or {}).items()
            if key in {"run_id", "stage", "source_generation", "candidate_generation", "target_identity",
                       "refreshed", "inventory", "error", "verification"}}


def _save(root, state):
    atomic_json(journal_path(root), state)
    atomic_json(Path(root) / "memory" / "embedding-migrations" / (state["run_id"] + ".json"), state)


def _render_config(before, patch):
    # Reuse the existing TOML section editor, restricting its managed key set so
    # identity, endpoints and unrelated memory settings remain byte-preserved.
    from pal.wizard.config_file import _replace_memory_embedding_keys
    lines = []
    for key, value in patch.items():
        if value is not None:
            lines.append(f"{CONFIG_KEYS[key]} = {json.dumps(value, ensure_ascii=False)}")
    text = _replace_memory_embedding_keys(before, lines, managed_keys={CONFIG_KEYS[key] for key in patch})
    tomllib.loads(text)
    return text


def _write_config(root, patch):
    text = _render_config(_config_text(root), patch)
    path = Path(root) / "config.toml"
    fd, temporary = tempfile.mkstemp(prefix=".embedding-config-", dir=root)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def run_embedding_command(root, args):
    command = args.embedding_command
    if command in {"migrate", "resume"}:
        batch_size = args.batch_size
        if type(batch_size) is not int or not 1 <= batch_size <= 10000:
            raise ValueError("batch-size must be between 1 and 10000")
    if command == "status":
        return {"status": "ok", "activation": "on_start", "state": _public_state(_read_journal(root))}
    if command in {"plan", "migrate"}:
        raw = json.loads(args.patch_file.read_text())
        patch = EmbeddingPatch.model_validate(raw, strict=True).model_dump(exclude_unset=True)
        if not patch or any(value is None for value in patch.values()):
            raise ValueError("Supply at least one non-null embedding setting")
        before = _config_text(root)
        source_identity = _identity(RuntimeConfig.load(root))
        target_identity = _identity(_target_config(root, patch))
        if command == "plan":
            return {"status": "ok", "activation": "not_applied", "state": {
                "source_identity": source_identity, "target_identity": target_identity,
                "reembedding_required": source_identity != target_identity,
                "dimension_alone_establishes_compatibility": False,
                "strategy": "rebuild unpublished generation, verify, then publish and update configuration"}}
        require_completed_migration(root)
        state = {"run_id": uuid4().hex, "stage": "creating", "patch": patch,
                 "source_generation": _storage(root).current(), "candidate_generation": None,
                 "target_identity": target_identity, "config_before_digest": _digest(before),
                 "config_target_digest": _digest(_render_config(before, patch)), "refreshed": 0}
        backup = Path(root) / "memory" / "embedding-migrations" / (state["run_id"] + ".config-before.toml")
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup_fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(backup_fd, "w") as stream:
            stream.write(before)
            stream.flush()
            os.fsync(stream.fileno())
        _save(root, state)
    else:
        state = _read_journal(root)
        if not state:
            raise ValueError("No embedding migration to resume")
    if state["stage"] == "completed":
        return {"status": "ok", "activation": "on_start", "state": _public_state(state)}
    try:
        _resume(root, state, batch_size)
    except Exception as exc:
        state["error"] = str(exc)
        _save(root, state)
        return {"status": "error", "activation": "not_completed", "state": _public_state(state),
                "recovery": "Keep Pal stopped and run pal memory embedding resume; source data and configuration backup are retained"}
    return {"status": "ok", "activation": "on_start", "state": _public_state(state)}


def _resume(root, state, batch_size):
    storage = _storage(root)
    storage.recover_abandoned_fence()
    source = state["source_generation"]
    candidate = state["candidate_generation"]
    if storage.current() not in {source, candidate}:
        raise RuntimeError("Current memory generation changed; migration cannot overwrite it")
    config_digest = _digest(_config_text(root))
    if config_digest not in {state["config_before_digest"], state["config_target_digest"]}:
        raise RuntimeError("Runtime configuration changed since planning; reconcile before resuming")
    if not candidate:
        old = storage.open(read_only=True)
        try:
            repo = storage.candidate(old)
        finally:
            old.close()
        candidate = state["candidate_generation"] = repo.generation_id
        state["stage"] = "reindexing"
        _save(root, state)
    else:
        repo = storage.open(candidate)
    try:
        if storage.current() != candidate:
            from pal.plugins.l3.sqlite_vec import SQLiteVecL3Plugin
            from pal.memory.service import MemoryService
            provider = SQLiteVecL3Plugin(service=MemoryService(), repository=repo,
                embedding_provider=build_ollama_embedding_provider_from_config(_target_config(root, state["patch"])))
            identity = state["target_identity"]
            with repo.write_transaction():
                repo.retarget_embeddings(**identity)
                for (ref,) in repo.database.execute_sql("SELECT document_id FROM memory_document_projection").fetchall():
                    if repo.get_embedding(ref + ":primary") is None:
                        provider._mark_document_pending(ref)
            while repo.inventory()["retryable_embeddings"]:
                result = provider.refresh_indexes(limit=batch_size, retry_failed=True)
                state["refreshed"] += result.get("refreshed", 0)
                state["inventory"] = repo.inventory()
                _save(root, state)
                if result.get("last_embedding_error") or not result.get("refreshed"):
                    raise RuntimeError(result.get("last_embedding_error") or "Embedding rebuild made no progress")
            _verify(repo, storage, source, provider)
            state["stage"] = "publishing"
            state["verification"] = "documents_unchanged_vectors_complete_self_retrieval_checked"
            _save(root, state)
            storage.publish_reindexed(repo, source_generation=source)
        state["stage"] = "configuring"
        _save(root, state)
        _write_config(root, state["patch"])
        state.update(stage="completed", config_after_digest=_digest(_config_text(root)), error="")
        _save(root, state)
    finally:
        repo.close()


def _verify(repo, storage, source_generation, provider):
    source = storage.open(source_generation, read_only=True)
    try:
        for table, key in (("memory_facts", "fact_id"), ("memory_cases", "case_id"), ("memory_topics", "topic_id")):
            sql = f"SELECT * FROM {table} ORDER BY {key}"
            if source.database.execute_sql(sql).fetchall() != repo.database.execute_sql(sql).fetchall():
                raise RuntimeError("Embedding migration changed memory content or references")
    finally:
        source.close()
    dimensions = set()
    for metadata in repo.Embedding.select().where(repo._active_embedding_condition()):
        vector = deserialize_vector(repo.get_vector_blob(metadata.embedding_id) or b"[]")
        if metadata.index_status != "ready" or not vector or not all(math.isfinite(v) for v in vector) or not any(vector):
            raise RuntimeError("Embedding coverage or vector validation failed")
        dimensions.add(len(vector))
    if len(dimensions) > 1:
        raise RuntimeError("Incompatible vector dimensions in target embedding space")
    sample = repo.database.execute_sql("SELECT document_id, search_text FROM memory_document_projection LIMIT 1").fetchone()
    if sample and sample[1]:
        if sample[0] not in provider._vector_candidates(sample[1], limit=max(1, repo.inventory()["embedding_count"])):
            raise RuntimeError("Representative vector retrieval failed")
