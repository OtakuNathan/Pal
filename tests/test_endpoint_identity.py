from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3

import pytest

from pal.foundation import PalV2Database
from pal.llm.compaction import native_origin
from pal.llm.endpoint_identity import IDENTITY_CHECK, canonical_endpoint_id
from pal.llm.endpoint_spec import LLMEndpointSpec
from pal.llm.models import LLMEndpointModel, PalRuntimeSettingModel
from pal.llm.repository import LLMEndpointRepository
from pal.llm.schema import _CREATE_NEXT_TABLE_SQL, _INSERT_COLUMNS, LLMEndpointSchemaError
from pal.llm.replay_compatibility import historical_endpoint_id
from pal.wizard.endpoint_migration import migrate_runtime_endpoint_ids
from tests.test_chatgpt_subscription import endpoint


def payload(**updates):
    result = LLMEndpointSpec.from_value(endpoint()).to_payload()
    result.update(endpoint_id="openai-model", provider="openai", model_id="model", auth_kind="api_key_ref",
                  credential_ref="old_credential:api-key", capabilities_blob={}, transport="http")
    result.update(updates)
    return result


def legacy_database(root: Path, *rows: dict) -> Path:
    path = root / "pal.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(_CREATE_NEXT_TABLE_SQL.replace("llm_endpoints__next", "llm_endpoints")
                   .replace(f" CHECK ({IDENTITY_CHECK})", "").replace(",\n    UNIQUE (provider, model_id)", ""))
        for row in rows:
            value = {**row, "created_at": "now", "updated_at": "now"}
            for key in ("thinking_levels_blob", "capabilities_blob", "input_modalities_blob", "output_modalities_blob"):
                value[key] = json.dumps(value[key])
            db.execute(f"INSERT INTO llm_endpoints ({', '.join(_INSERT_COLUMNS)}) VALUES ({', '.join('?' for _ in _INSERT_COLUMNS)})",
                       [value[key] for key in _INSERT_COLUMNS])
        db.execute("CREATE TABLE pal_runtime_settings(setting_key TEXT PRIMARY KEY, setting_value TEXT, updated_at TEXT)")
    return path


def test_identity_constraints_and_auto_profile(tmp_path):
    db = PalV2Database(tmp_path / "pal.sqlite3")
    db.initialize((LLMEndpointModel, PalRuntimeSettingModel))
    try:
        repo = LLMEndpointRepository()
        data = payload(provider="openrouter", model_id="openai/GPT-test")
        del data["endpoint_id"]
        created = repo.upsert(**data)
        assert created.endpoint_id == "openrouter-openai/GPT-test"
        with pytest.raises(ValueError, match="endpoint_id"):
            repo.upsert(**data, endpoint_id="alternate")
        assert repo.upsert(**{**data, "wire_shape": "openai_completion"}).endpoint_id == created.endpoint_id
        assert len(repo.list_all()) == 1
        chat = repo.upsert(**payload(endpoint_id="chatgpt-model", provider="chatgpt", auth_kind="oauth",
                                     credential_ref="pal.chatgpt:test"))
        assert chat.capabilities_blob["access_profile"] == "openai_chatgpt"
        with pytest.raises(ValueError, match="provider"):
            repo.upsert(**payload(endpoint_id="openai-model", auth_kind="oauth", credential_ref="pal.chatgpt:test"))
    finally:
        db.close()
    with sqlite3.connect(tmp_path / "pal.sqlite3") as raw:
        for new_id, provider, model in [("incorrect", "openrouter", "openai/GPT-test"),
                                         ("openrouter-M", "OpenRouter", "M"), ("openrouter-", "openrouter", ""),
                                         ("openrouter-M\n", "openrouter", "M\n")]:
            with pytest.raises(sqlite3.IntegrityError):
                raw.execute("UPDATE llm_endpoints SET endpoint_id=?, provider=?, model_id=? WHERE provider='openrouter'", (new_id, provider, model))
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("INSERT INTO llm_endpoints SELECT * FROM llm_endpoints WHERE provider='openrouter'")
    for provider, model in [("foo-bar", "x"), ("", "x"), ("foo", " x"), ("foo", "")]:
        with pytest.raises(ValueError):
            canonical_endpoint_id(provider, model)


def test_runtime_migration_preserves_history_updates_references_and_is_idempotent(tmp_path):
    chosen = payload(endpoint_id="old messages", provider="zhipu", model_id="glm-5.4", thinking_levels_blob=["off", "high"], wire_shape="anthropic_messages", base_url="https://open.bigmodel.cn/api/anthropic")
    discarded = {**chosen, "endpoint_id": "old completion", "wire_shape": "openai_completion"}
    repaired = payload(endpoint_id="old chatgpt", provider="openai", model_id="model", auth_kind="oauth", credential_ref="pal.chatgpt:test")
    path = legacy_database(tmp_path, chosen, discarded, repaired)
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO pal_runtime_settings VALUES (?,?,?)", [("active_llm_endpoint_id", "old messages", "now"),
                        ("think_level:old messages", "high", "now"), ("think_level:old completion", "off", "now")])
    profile = tmp_path / "data/bunshin/catalog/profile_overrides/software_engineering/coder.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(json.dumps({"preferred_endpoint_id": "old messages", "metadata": {"preferred_endpoint_id": "old messages"}}))
    config = tmp_path / "config.toml"
    config.write_text('[memory.dreaming]\nendpoint_id = "old messages"\nreview_endpoint_id = "old chatgpt"\n')
    from tests.test_resident_checkpoint import _build_app
    app = _build_app(tmp_path)
    memory = app.handle.memory_service
    memory.begin_l1_turn("t", user_text="preserved", metadata={"pal_effort_pin": {"endpoint_id": "old messages", "model_id": "glm-5.4", "wire_shape": "anthropic_messages", "base": "high", "effective": "high"}})
    memory.settle_l1_turn("t")
    asyncio.run(app._publish_checkpoint_async())
    checkpoint = app.history.store.base.path
    before = {p: p.read_bytes() for p in (path, profile, config, checkpoint)}
    with pytest.raises(LLMEndpointSchemaError, match="duplicate"):
        migrate_runtime_endpoint_ids(tmp_path, dry_run=True)
    keep = ("old messages",)
    preview = migrate_runtime_endpoint_ids(tmp_path, dry_run=True, keep=keep)
    assert preview["before"] == 3 and preview["after"] == 2
    assert {p: p.read_bytes() for p in before} == before
    result = migrate_runtime_endpoint_ids(tmp_path, keep=keep)
    assert result["status"] == "migrated"
    assert checkpoint.read_bytes() == before[checkpoint]
    assert json.loads(profile.read_text())["preferred_endpoint_id"] == "bigmodel-glm-5.4"
    assert 'endpoint_id = "bigmodel-glm-5.4"' in config.read_text()
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        specs = [LLMEndpointSpec.from_value(dict(row)) for row in db.execute("SELECT * FROM llm_endpoints ORDER BY endpoint_id")]
        assert specs[0].endpoint_id == "bigmodel-glm-5.4"
        assert specs[0].credential_ref == chosen["credential_ref"]
        assert specs[0].capabilities_blob["replay_endpoint_aliases"] == {"old messages": native_origin(specs[0])}
        assert historical_endpoint_id("old messages", specs) == specs[0].endpoint_id
        assert historical_endpoint_id("old completion", specs) == "old completion"
        assert specs[1].capabilities_blob == {"access_profile": "openai_chatgpt"}
        assert {row[0]: row[1] for row in db.execute("SELECT setting_key, setting_value FROM pal_runtime_settings")} == {
            "active_llm_endpoint_id": "bigmodel-glm-5.4", "think_level:bigmodel-glm-5.4": "high"}
    assert Path(result["backup"], "pal.sqlite3").is_file()
    assert migrate_runtime_endpoint_ids(tmp_path)["status"] == "current"
    app.handle.core.close()


def test_migration_failure_restores_database_and_configs(tmp_path, monkeypatch):
    path = legacy_database(tmp_path, payload(endpoint_id="old model"))
    config = tmp_path / "config.toml"
    config.write_text('[memory.dreaming]\nendpoint_id="old model"\n')
    from pal.wizard import endpoint_migration as migration
    original = migration._atomic_copy
    def fail_once(target, value):
        if target == config and b'openai-model' in value:
            raise OSError("simulated publish failure")
        return original(target, value)
    monkeypatch.setattr(migration, "_atomic_copy", fail_once)
    with pytest.raises(OSError, match="simulated"):
        migrate_runtime_endpoint_ids(tmp_path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT endpoint_id FROM llm_endpoints").fetchone()[0] == "old model"
    assert 'old model' in config.read_text()
    assert not (tmp_path / migration._MARKER).exists()
    monkeypatch.setattr(migration, "_atomic_copy", original)
    assert migrate_runtime_endpoint_ids(tmp_path)["status"] == "migrated"


def test_effort_and_replay_aliases_use_the_same_proven_origin():
    from pal.llm.effort_updates import binding_matches, message_effort_update
    from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR, WireShape
    from pal.llm.shapes.base import ShapeContext
    from pal.memory.effort_pin import plan_effort_pin, pinned_think_levels
    from pal.memory import MemoryService
    source = LLMEndpointSpec.from_value(payload(endpoint_id="old gpt", model_id="gpt-6.2-sol",
                                                thinking_levels_blob=["low", "high"], default_thinking_level="low"))
    origin = native_origin(source)
    alias = {"old gpt": origin}
    facts = {"endpoint_id": "openai-gpt-6.2-sol", "model_id": source.model_id, "wire_shape": source.wire_shape,
             "route_origin": origin, "capabilities": {"replay_endpoint_aliases": alias},
             "thinking_levels": ["low", "high"], "supports_effort_updates": True}
    old = {"endpoint_id": source.endpoint_id, "model_id": source.model_id, "wire_shape": source.wire_shape}
    assert binding_matches(old, facts)
    assert not binding_matches({**old, "route_origin": "different"}, facts)
    assert not binding_matches(old, {**facts, "route_origin": "different"})
    context = ShapeContext(WireShape.OPENAI_RESPONSE, facts["endpoint_id"], source.model_id,
                           base_url=source.base_url, capabilities=facts["capabilities"], route_origin=origin)
    message = LLMMessageIR(MessageRole.USER, (TextPartIR("next"),), metadata={"pal_effort_update": {**old, "effort": "high"}})
    assert message_effort_update(message, context) == "high"
    memory = MemoryService()
    root = memory.history_root
    memory.begin_l1_turn("t", user_message=message, metadata={"pal_effort_pin": {
        **old, "base": "low", "effective": "high", "epoch": [root.incarnation, root.left_generation]}})
    memory.settle_l1_turn("t")
    assert pinned_think_levels(memory, facts, {facts["endpoint_id"]: "high"})[facts["endpoint_id"]] == "low"
    pin, update = plan_effort_pin(memory, facts, "high")
    assert pin["base"] == "low" and pin["effective"] == "high" and update is None


def test_incompatible_route_migration_refuses_retained_history(tmp_path):
    path = legacy_database(tmp_path, payload(endpoint_id="old chatgpt", auth_kind="oauth", credential_ref="pal.chatgpt:test"))
    from tests.test_resident_checkpoint import _build_app
    app = _build_app(tmp_path)
    memory = app.handle.memory_service
    memory.begin_l1_turn("t", user_text="retained", metadata={"pal_effort_pin": {"endpoint_id": "old chatgpt"}})
    memory.settle_l1_turn("t")
    asyncio.run(app._publish_checkpoint_async())
    before = path.read_bytes()
    with pytest.raises(LLMEndpointSchemaError, match="semantic handoff"):
        migrate_runtime_endpoint_ids(tmp_path, dry_run=True)
    assert path.read_bytes() == before
    app.handle.core.close()


def test_current_identity_schema_can_repair_missing_subscription_profile(tmp_path):
    db = PalV2Database(tmp_path / "pal.sqlite3")
    db.initialize((LLMEndpointModel, PalRuntimeSettingModel))
    try:
        # Simulate an older or manually imported canonical OAuth row that
        # missed its required access profile, without using the write service.
        LLMEndpointModel.create(**payload(endpoint_id="chatgpt-model", provider="chatgpt", auth_kind="oauth",
                                          credential_ref="pal.chatgpt:test"))
    finally:
        db.close()
    result = migrate_runtime_endpoint_ids(tmp_path)
    assert result["status"] == "migrated"
    assert result["incompatible_old_bindings"] == ["chatgpt-model"]
    with sqlite3.connect(tmp_path / "pal.sqlite3") as raw:
        assert json.loads(raw.execute("SELECT capabilities_blob FROM llm_endpoints").fetchone()[0]) == {"access_profile": "openai_chatgpt"}


def test_cli_only_migrates_explicitly_and_respects_the_live_runtime_lease(tmp_path):
    import argparse
    from pal.llm.cli import configure_llm_parser, run_llm_cli
    from pal.packages.process import runtime_lease
    path = legacy_database(tmp_path, payload(endpoint_id="old model"))
    parser = argparse.ArgumentParser()
    configure_llm_parser(parser)
    def run(*args):
        return run_llm_cli(parser.parse_args([*args, "--runtime-root", str(tmp_path)]))
    before = path.read_bytes()
    assert run("list") == 2
    with runtime_lease(tmp_path):
        assert run("migrate-ids", "--dry-run") == 0
        assert run("migrate-ids") == 2
    assert path.read_bytes() == before
    assert run("migrate-ids") == 0
    with runtime_lease(tmp_path):
        assert run("add", "--provider", "openai", "--model-id", "model", "--replace", "--wire-shape", "openai_completion") == 2
    assert run("add", "--provider", "openai", "--model-id", "model", "--replace", "--wire-shape", "openai_completion") == 0
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT endpoint_id, wire_shape FROM llm_endpoints").fetchall() == [("openai-model", "openai_completion")]
