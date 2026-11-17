"""Embedding provenance survives reload and is enforced through real migration."""
import asyncio
import pytest

from pal.memory.contracts import MemoryQuery
from pal.memory.embedding import HashingEmbedder
from pal.memory.embedding_space import read_space
from pal.plugins.l3.capabilities import register_with_core
from pal.plugins.l3.sqlite_vec import SQLiteVecL3Plugin
from tests.test_memory_swap import fact, settled, swap_runtime


@pytest.mark.parametrize("change", ["dimension", "model", "revision", "unknown_provenance"])
def test_changed_embedding_reload_is_basic_read_only_until_managed_swap(swap_runtime, monkeypatch, change):
    core, memory, storage, source, _, command = swap_runtime
    saved = source.commit(fact("lexical content remains available during migration"))
    assert source.refresh_indexes()["refreshed"] == 1
    generation = storage.current()
    identity = storage.generation_profile(generation)
    if change == "unknown_provenance":
        source.repository.database.execute_sql("DELETE FROM memory_embedding_space")
    core.context.unregister_module(core.context.module_registry.require(source.module_id))
    source.repository.close()
    embedding = HashingEmbedder(dimension=16 if change == "dimension" else 32,
                               model_name="different-model" if change == "model" else "hashing-test-embedder")
    if change == "revision":
        embedding.model_revision = "different-revision"
    provider = SQLiteVecL3Plugin(memory, repository=storage.open(), provider_id=source.provider_id,
                                embedding_provider=embedding)
    try:
        register_with_core(core.context, provider)
        assert provider.read_only
        assert provider.inspect()["read_only"] and not provider.inspect_summary()["embedding_space_matches"]
        assert provider.commit(fact("must not enter the old generation")).status == "unavailable"
        assert provider.refresh_indexes()["read_only"]
        def forbidden(_text):
            raise AssertionError("a mismatched embedding transport was called")
        with monkeypatch.context() as guard:
            guard.setattr(embedding, "embed_query", forbidden)
            guard.setattr(embedding, "embed_documents", forbidden)
            assert provider.recall(MemoryQuery(mem_ref=saved.document_id)).hits
            assert provider.recall(MemoryQuery(queries=["lexical content"])).hits
        assert storage.generation_profile(generation) == identity
        assert {row.dimension for row in provider.repository.Vector.select()} == {32}
        async def migrate():
            job = (await command("swap", name=provider.provider_id))["state"]["job_id"]
            result = await settled(command, job)
            assert result["state"]["phase"] == "complete", result
        asyncio.run(migrate())
        assert storage.current() != generation
        assert not provider.read_only
        assert provider.commit(fact("allowed after migration")).status == "ok"
        assert {row.dimension for row in provider.repository.Vector.select()} == {embedding.dimension}
        assert {row.model_name for row in provider.repository.Embedding.select()} == {embedding.model_name}
        assert read_space(provider.repository.database.connection()).profile == provider.migration_identity
    finally:
        provider.repository.close()


def test_configuration_change_during_embedding_discards_the_response(swap_runtime, monkeypatch):
    _, _, _, source, _, _ = swap_runtime
    source.commit(fact("already indexed content"))
    source.refresh_indexes()
    pending = source.commit(fact("embedding request in flight"))
    embedding = source.embedding_provider
    original = embedding.embed_documents
    def change_configuration(texts):
        vectors = original(texts)
        embedding.model_name = "changed-while-awaiting-transport"
        return vectors
    monkeypatch.setattr(embedding, "embed_documents", change_configuration)
    result = source.refresh_indexes()
    assert result["refreshed"] == 0 and result["last_embedding_error"]
    assert source.repository.get_vector_blob(pending.document_id + ":primary") is None
    assert source.repository.Vector.select().count() == 1
    assert source.read_only


def test_query_response_from_a_changed_model_is_discarded(swap_runtime, monkeypatch):
    _, _, _, source, _, _ = swap_runtime
    source.commit(fact("a vector query cannot cross model identities"))
    source.refresh_indexes()
    embedding = source.embedding_provider
    query = embedding.embed_query
    def change_configuration(text):
        vector = query(text)
        embedding.model_name = "changed-during-query"
        return vector
    monkeypatch.setattr(embedding, "embed_query", change_configuration)
    assert source._vector_candidates("vector query", limit=4) == {}
    assert source.last_embedding_error
    assert source.repository.Vector.select().count() == 1


@pytest.mark.parametrize("dimensions", [(16,), (32, 16)])
def test_changed_response_dimensions_never_mix_with_stored_or_batch_vectors(swap_runtime, monkeypatch, dimensions):
    _, _, _, source, _, _ = swap_runtime
    if len(dimensions) == 1:
        source.commit(fact("existing thirty two dimensional vector"))
        source.refresh_indexes()
    for index in range(len(dimensions)):
        source.commit(fact(f"pending vector {index}"))
    monkeypatch.setattr(source.embedding_provider, "embed_documents", lambda _texts: [[1.0] * size for size in dimensions])
    result = source.refresh_indexes()
    assert result["refreshed"] == 0 and result["last_embedding_error"]
    assert {row.dimension for row in source.repository.Vector.select()} <= {32}
    assert source.repository.Vector.select().count() == (1 if len(dimensions) == 1 else 0)


def test_storage_rejects_a_vector_with_another_identity_or_dimension(swap_runtime):
    _, _, _, source, _, _ = swap_runtime
    saved = source.commit(fact("storage enforces vector provenance"))
    source.refresh_indexes()
    embedding_id = saved.document_id + ":primary"
    original = source.repository.get_vector_blob(embedding_id)
    for identity, dimension in [({**source.migration_identity, "model_name": "other"}, 32), (source.migration_identity, 16)]:
        with pytest.raises(RuntimeError, match="identity or dimension"), source.repository.write_transaction():
            source.repository.upsert_vector_blob(embedding_id=embedding_id, vector_blob=b"[1]", dimension=dimension,
                                                 profile=identity)
        assert source.repository.get_vector_blob(embedding_id) == original
    with pytest.raises(RuntimeError, match="unpublished"):
        source.prepare_generation(source.repository)
