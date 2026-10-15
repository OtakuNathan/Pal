import argparse
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pal.core import PalCore
from pal.execution import register_with_core
from pal.main import _build_parser
from pal.memory.maintenance import MemoryBackendOwner
from pal.memory.storage import MemoryStorage
from pal.memory import embedding_migration as migration
from pal.shared import IntrospectionResult


class Embeddings:
    provider_id = "fixture"
    text_processing_version = "search_text_v1"

    def __init__(self, config):
        self.model_name = config.embedding_ollama_model_name
        self.model_revision = config.embedding_model_revision or None

    def embed_documents(self, texts):
        return [[1.0, 0.0] if self.model_name == "old" else [0.0, 1.0] for text in texts]

    def embed_query(self, text):
        return self.embed_documents([text])[0]


@pytest.fixture
def stored_memory(tmp_path, monkeypatch):
    (tmp_path / "pal.sqlite3").touch()
    (tmp_path / "config.toml").write_text('[core]\nuntouched = "keep"\n[memory]\nembedding_ollama_model_name = "old"\nkeep_recent_tool_messages = 9\n')
    storage = MemoryStorage(tmp_path)
    storage.create_initial()
    repo = storage.open()
    repo.Fact.create(fact_id="one", summary="alpha", search_text="alpha")
    repo.Fact.create(fact_id="two", summary="beta", search_text="beta")
    repo.close()
    monkeypatch.setattr(migration, "build_ollama_embedding_provider_from_config", Embeddings)
    patch = tmp_path / "patch.json"
    patch.write_text(json.dumps({"model_name": "new", "model_revision": "v2"}))
    return tmp_path, storage, patch


def command(name, patch=None, batch=1):
    return argparse.Namespace(embedding_command=name, patch_file=patch, batch_size=batch)


def test_migration_reembeds_same_dimension_preserves_references_and_old_pins(stored_memory):
    root, storage, patch = stored_memory
    old = storage.current()
    assert storage.pin("workflow") == old
    plan = migration.run_embedding_command(root, command("plan", patch))
    assert plan["state"]["reembedding_required"] is True
    result = migration.run_embedding_command(root, command("migrate", patch))
    assert result["status"] == "ok", result
    assert result["activation"] == "on_start"
    assert storage.current() != old
    assert storage.pinned("workflow") == old
    assert storage.path(old).is_file()
    repo = storage.open()
    try:
        assert repo.get_document("fact:one")["summary"] == "alpha"
        assert repo.inventory()["ready_embeddings"] == 2
        assert {m.model_name for m in repo.Embedding.select()} == {"new"}
        assert {m.model_revision for m in repo.Embedding.select()} == {"v2"}
    finally:
        repo.close()
    text = (root / "config.toml").read_text()
    assert 'untouched = "keep"' in text and "keep_recent_tool_messages = 9" in text
    assert migration.run_embedding_command(root, command("resume"))["status"] == "ok"
    migration.require_completed_migration(root)


def test_interrupted_rebuild_keeps_source_current_and_resumes(stored_memory, monkeypatch):
    root, storage, patch = stored_memory
    old = storage.current()
    config = (root / "config.toml").read_bytes()
    original = Embeddings.embed_documents
    calls = []
    def interrupt(self, texts):
        calls.append(list(texts))
        if len(calls) == 2:
            raise RuntimeError("fixture transport interrupted")
        return original(self, texts)
    monkeypatch.setattr(Embeddings, "embed_documents", interrupt)
    failed = migration.run_embedding_command(root, command("migrate", patch))
    assert failed["status"] == "error"
    assert storage.current() == old
    assert (root / "config.toml").read_bytes() == config
    with pytest.raises(RuntimeError, match="incomplete"):
        migration.require_completed_migration(root)
    completed = migration.run_embedding_command(root, command("resume"))
    assert completed["status"] == "ok", completed
    assert completed["state"]["candidate_generation"] == failed["state"]["candidate_generation"]
    assert len(calls) == 4  # completed first batch is not recomputed; final call verifies retrieval


def test_cli_parser_routes_backend_and_embedding_commands():
    parser = _build_parser()
    parsed = parser.parse_args(["memory", "refresh-indexes", "sqlite_vec_l3", "--limit", "7", "--json"])
    assert parsed.memory_command == "refresh-indexes" and parsed.limit == 7
    assert parser.parse_args(["memory", "embedding", "resume"]).embedding_command == "resume"


def test_resident_controls_select_real_provider_and_reject_busy_mutation(tmp_path):
    from pal.memory.service import MemoryService
    from pal.plugins.l3.stubs import MockL3Plugin
    core = PalCore()
    register_with_core(core.context)
    memory = MemoryService()
    provider = MockL3Plugin(provider_id="fixture")
    core.context.execution_runtime.l3_plugin_registry.register(provider)
    owner = MemoryBackendOwner(core, memory)
    try:
        selected = asyncio.run(owner.dispatch("select", {"name": "fixture"}))
        assert selected["selection_scope"] == "until_restart"
        assert memory.l3_selector.active_provider_id == "fixture"
        core.memory_maintenance_busy = lambda: True
        with pytest.raises(RuntimeError, match="busy"):
            asyncio.run(owner.dispatch("detach", {"name": "fixture"}))
        assert provider.mounted
    finally:
        core.close()


def test_invalid_batch_does_not_create_pending_migration(stored_memory):
    root, storage, patch = stored_memory
    with pytest.raises(ValueError, match="batch-size"):
        migration.run_embedding_command(root, command("migrate", patch, batch=0))
    assert not migration.journal_path(root).exists()
    migration.require_completed_migration(root)


@pytest.mark.parametrize("after_write", [False, True])
def test_publication_config_interruption_blocks_startup_and_recovers(stored_memory, monkeypatch, after_write):
    root, storage, patch = stored_memory
    old = storage.current()
    original = migration._write_config
    def interrupt(*args):
        if after_write:
            original(*args)
        raise OSError("interrupted configuration publication")
    monkeypatch.setattr(migration, "_write_config", interrupt)
    result = migration.run_embedding_command(root, command("migrate", patch))
    assert result["status"] == "error"
    assert storage.current() != old
    with pytest.raises(RuntimeError, match="incomplete"):
        migration.require_completed_migration(root)
    monkeypatch.setattr(migration, "_write_config", original)
    completed = migration.run_embedding_command(root, command("resume"))
    assert completed["status"] == "ok", completed
    assert storage.current() == result["state"]["candidate_generation"]
    migration.require_completed_migration(root)


def test_live_memory_cli_uses_resident_owner_and_refuses_offline_migration(stored_memory, capsys):
    from pal.control.configuration import CoreConfigurationService
    from pal.memory.cli import run_memory_cli_async
    from pal.memory.service import MemoryService
    from pal.plugins.l3.stubs import MockL3Plugin
    from pal.packages.process import runtime_lease
    root, storage, patch = stored_memory
    async def run():
        core = PalCore()
        register_with_core(core.context)
        core.context.execution_runtime.l3_plugin_registry.register(MockL3Plugin(provider_id="fixture"))
        memory = MemoryService()
        service = CoreConfigurationService(root, None, memory_owner=MemoryBackendOwner(core, memory))
        parser = _build_parser()
        def parse(*args):
            return parser.parse_args(["memory", *args, "--runtime-root", str(root), "--json"])
        with runtime_lease(root):
            await service.start_async()
            try:
                assert await run_memory_cli_async(parse("select", "fixture")) == 0
                result = json.loads(capsys.readouterr().out)
                assert result["activation"] == "live"
                assert memory.l3_selector.active_provider_id == "fixture"
                assert await run_memory_cli_async(parse("embedding", "migrate", "--patch-file", str(patch))) == 2
                assert "Stop Pal" in json.loads(capsys.readouterr().out)["error"]
                assert not migration.journal_path(root).exists()
                await service.stop_async()
                assert await run_memory_cli_async(parse("select", "fixture")) == 2
                assert json.loads(capsys.readouterr().out)["outcome"] == "unknown"
            finally:
                await service.stop_async()
                core.close()
    asyncio.run(run())


def test_resume_refuses_unrelated_config_change_after_publication(stored_memory, monkeypatch):
    root, storage, patch = stored_memory
    original = migration._write_config
    monkeypatch.setattr(migration, "_write_config", lambda *args: (_ for _ in ()).throw(OSError("interrupt")))
    assert migration.run_embedding_command(root, command("migrate", patch))["status"] == "error"
    config = root / "config.toml"
    config.write_text(config.read_text().replace('untouched = "keep"', 'untouched = "changed"'))
    monkeypatch.setattr(migration, "_write_config", original)
    result = migration.run_embedding_command(root, command("resume"))
    assert result["status"] == "error"
    assert "configuration changed" in result["state"]["error"]
    assert 'untouched = "changed"' in config.read_text()
