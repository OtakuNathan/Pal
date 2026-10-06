"""Synthetic worker bootstrap: no resident data, model, sidecars, or sockets."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import socket
import sqlite3
import subprocess
from unittest.mock import AsyncMock, Mock

import pytest

from pal.bunshin.scoped_execution import BunshinScopedExecutionRuntime
from pal.shared.tool_protocol import new_tool_call
from pal.bunshin.ipc import BUNSHIN_RUNTIME_DB_PATH_ENV
from pal.bunshin.runner_components import runtime_build
from pal.core.module_lifecycle import ModuleLifecycle
from pal.foundation import PalV2Database
from pal.lsp.plugin import LspManagerPluginProvider
from pal.memory.storage import MemoryStorage
from pal.skill import SkillDescriptor, SkillReadTool, SkillSearchTool, SkillInjectTool
from pal.skill.repository import SkillRepository, ReadOnlySkillRepository
from pal.web_fetch.browser_service import BrowserServiceManager
from pal.wizard.runtime import ALL_MODELS


def _skill(skill_id, *, module_id="skill", source_kind="declared"):
    return SkillDescriptor(skill_id=skill_id, module_id=module_id, source_kind=source_kind,
                           title=skill_id, summary=skill_id, manual_text="Synthetic manual")


@pytest.mark.parametrize("readonly", [True, False])
def test_full_slim_runtime_skill_publication_and_withdrawal(tmp_path, monkeypatch, readonly):
    # Seed only this test's fresh database, then reopen it through real bootstrap.
    database = PalV2Database(tmp_path / "pal.sqlite3")
    database.initialize(ALL_MODELS)
    durable = SkillRepository()
    durable.upsert_skill(_skill("test.user", source_kind="instructed"))
    durable.upsert_skill(_skill("test.stale", module_id="lsp"))
    durable.upsert_skill(_skill("test.host_only", module_id="host_only"))
    before = tuple(skill.to_dict() for skill in durable.list_skills())
    database.close()
    storage = MemoryStorage(tmp_path)
    generation = storage.create_initial()
    storage.pin("synthetic-workflow")

    monkeypatch.setenv(BUNSHIN_RUNTIME_DB_PATH_ENV, str(tmp_path / "pal.sqlite3"))
    monkeypatch.setenv("PAL_BUNSHIN_WEB_BROKER", "0")
    monkeypatch.delenv("PAL_BUNSHIN_SANDBOXED", raising=False)
    # Keep actual module registration and declaration collection. Only external
    # boundaries are disabled, including shutdown of any sidecar manager.
    no_model = Mock(return_value=None)
    monkeypatch.setattr(runtime_build, "build_role_llm", no_model)
    extension = Mock()
    monkeypatch.setattr("pal.execution.worker_extensions.activate_worker_extension", extension)
    monkeypatch.setattr(LspManagerPluginProvider, "stop_manager", Mock())
    monkeypatch.setattr(BrowserServiceManager, "shutdown_async", AsyncMock())
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("socket forbidden")))
    monkeypatch.setattr(subprocess, "Popen", Mock(side_effect=AssertionError("process forbidden")))

    lifecycles = []
    def lifecycle(context, state):
        instance = ModuleLifecycle(context, state)
        lifecycles.append(instance)
        return instance
    monkeypatch.setattr(runtime_build, "ModuleLifecycle", lifecycle)
    writes = []
    real_initialize = PalV2Database.initialize
    def initialize(db, models):
        real_initialize(db, models)
        if readonly:
            assert db.read_only
            assert db.peewee_db.execute_sql("PRAGMA query_only").fetchone() == (1,)
            def authorize(action, first, second, name, source):
                if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
                    writes.append((action, first))
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.peewee_db.connection().set_authorizer(authorize)
    monkeypatch.setattr(PalV2Database, "initialize", initialize)

    bundle = runtime_build.build_slim_bunshin_runtime(
        tmp_path, run_id="synthetic-role", llm_authority="manager_proxy" if readonly else "none",
        memory_workflow_id="synthetic-workflow", snapshot_root=tmp_path / "output",
    )
    try:
        assert bundle.llm_runtime is None
        assert bundle.memory_generation_id == generation
        no_model.assert_called_once()
        extension.assert_called_once()
        lifecycle = lifecycles[0]
        service = lifecycle.context.require_port("skill:skill")
        assert isinstance(service.repository, ReadOnlySkillRepository) is readonly
        published = set(bundle.execution_runtime.compiled_capability_index.by_canonical)
        assert {"op_skill_search", "op_skill_read", "op_skill_inject"} <= published
        assert service.repository.get_skill("test.user") is not None
        assert service.repository.get_skill("test.stale") is None
        if readonly:
            assert service.repository.get_skill("test.host_only") is None
        lsp_skills = [s for s in service.repository.list_skills() if s.module_id == "lsp"]
        assert lsp_skills
        skill = lsp_skills[0]
        assert SkillSearchTool(service).invoke({"query": skill.skill_id}).structured["count"] > 0
        assert SkillReadTool(service).invoke({"skill_id": skill.skill_id}).status == "ok"
        assert SkillInjectTool(service).invoke({"skill_id": skill.skill_id}).status == "ok"
        scoped = BunshinScopedExecutionRuntime(
            bundle.execution_runtime, ["op_skill_search", "op_skill_read", "op_skill_inject"],
            workspace={"run_id": "synthetic-role"},
        )
        assert scoped.get_capability_spec("inject_skill") is not None
        assert scoped.get_capability_spec("disable_skill") is None
        assert scoped.get_capability_spec("find_lsp_definitions") is None
        injected = asyncio.run(scoped.execute_tool_async(
            new_tool_call(name="inject_skill", args={"name": skill.skill_id}),
        ))
        assert injected.status == "ok"
        assert scoped.get_capability_spec("disable_skill") is None
        assert scoped.get_capability_spec("find_lsp_definitions") is None
        if readonly:
            assert tuple(s.to_dict() for s in durable.list_skills()) == before
            for mutate in (service.repository.disable_skill, service.repository.mark_deprecated):
                with pytest.raises(PermissionError, match="read-only"):
                    mutate("test.user")
            with pytest.raises(PermissionError, match="read-only"):
                service.repository.upsert_skill(_skill("test.new", source_kind="instructed"))
        else:
            assert durable.get_skill(skill.skill_id) is not None
            assert durable.get_skill("test.stale") is None
        lifecycle.withdraw_module_capabilities("lsp")
        assert service.repository.get_skill(skill.skill_id) is None
        assert service.repository.get_skill("test.stale") is None
        assert service.repository.get_skill("test.user") is not None
        lifecycle.publish_module_capabilities("lsp")
        assert service.repository.get_skill(skill.skill_id) is not None
        # A partial declaration failure must withdraw both capabilities and
        # local declarations without hiding the original exception.
        provider = lifecycle.context.module_registry.require("lsp").introspection_provider
        original_declarations = provider.declared_skills
        def failed_declarations():
            yield _skill("test.partial", module_id="lsp")
            raise ValueError("synthetic declaration failure")
        monkeypatch.setattr(provider, "declared_skills", failed_declarations)
        with pytest.raises(ValueError, match="synthetic declaration failure"):
            lifecycle.publish_module_capabilities("lsp")
        assert service.repository.get_skill("test.partial") is None
        assert service.repository.get_skill(skill.skill_id) is None
        assert not lifecycle.context.capability_registry.by_module.get("lsp")
        monkeypatch.setattr(provider, "declared_skills", original_declarations)
        lifecycle.publish_module_capabilities("lsp")
        assert service.repository.get_skill(skill.skill_id) is not None
        if readonly:
            assert tuple(s.to_dict() for s in durable.list_skills()) == before
        assert not writes
    finally:
        asyncio.run(bundle.close())
    assert not writes


def test_readonly_overlay_filters_and_does_not_resurrect_declared_rows(tmp_path):
    database = PalV2Database(tmp_path / "skills.sqlite3")
    database.initialize(ALL_MODELS)
    durable = SkillRepository()
    durable.upsert_skill(_skill("same", module_id="lsp"))
    durable.upsert_skill(_skill("learned", source_kind="learned"))
    database.close()
    database = PalV2Database(tmp_path / "skills.sqlite3", read_only=True)
    database.initialize(ALL_MODELS)
    try:
        repo = ReadOnlySkillRepository()
        assert repo.get_skill("same") is None
        local = replace(_skill("same", module_id="lsp"), title="Live worker", enabled=False)
        repo.upsert_skill(local)
        assert repo.get_skill("same") == local
        assert [s.skill_id for s in repo.list_skills()] == ["learned", "same"]
        assert [s.skill_id for s in repo.list_skills(enabled_only=True)] == ["learned"]
        assert [s.skill_id for s in repo.list_skills(active_only=True)] == ["learned"]
        repo.upsert_skill(replace(local, enabled=True, status="disabled"))
        assert not repo.get_skill("same").enabled
        assert [s.skill_id for s in repo.list_skills(enabled_only=True)] == ["learned"]
        assert repo.delete_declared_skills_for_module("lsp") == 1
        assert repo.get_skill("same") is None
        assert durable.get_skill("same").title == "same"
        assert ReadOnlySkillRepository().get_skill("same") is None
    finally:
        database.close()


def test_bwrap_full_slim_bootstrap_uses_only_readonly_fixture(tmp_path, monkeypatch):
    import os
    import shutil
    import textwrap

    from pal.bunshin.sandbox import build_sandboxed_runner_invocation
    from pal.bunshin.harnesses import pal_harness_spec
    from pal.shared import BunshinInvocationPack

    if not shutil.which("bwrap"):
        pytest.skip("bubblewrap is not available")
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    database = PalV2Database(runtime_root / "pal.sqlite3")
    database.initialize(ALL_MODELS)
    SkillRepository().upsert_skill(_skill("test.user", source_kind="instructed"))
    database.close()
    storage = MemoryStorage(runtime_root)
    storage.create_initial()
    storage.pin("synthetic-workflow")
    endpoint = runtime_root / "data" / "bunshin-role" / "role.sock"
    endpoint.parent.mkdir(parents=True)
    endpoint.write_text("synthetic endpoint, not a socket")
    monkeypatch.setenv("PAL_BUNSHIN_SANDBOX_SCRATCH_ROOT", str(tmp_path / "scratch"))
    # The child can only access this synthetic runtime. No role gateway, model,
    # extension, LSP manager or browser process is contacted, even at shutdown.
    script = textwrap.dedent(f'''\
        import asyncio, os, socket, subprocess
        from pathlib import Path
        from unittest.mock import AsyncMock, Mock, patch
        from pal.bunshin.runner_components import runtime_build
        from pal.skill.models import SkillModel
        from pal.skill.tools import SkillSearchTool, SkillInjectTool
        from pal.skill.repository import ReadOnlySkillRepository
        from pal.core.module_lifecycle import ModuleLifecycle
        from pal.foundation import PalV2Database
        lifecycle_instances = []
        def lifecycle(context, state):
            result = ModuleLifecycle(context, state)
            lifecycle_instances.append(result)
            return result
        with (
            patch.object(runtime_build, "build_role_llm", return_value=None),
            patch.object(runtime_build, "ModuleLifecycle", lifecycle),
            patch("pal.execution.worker_extensions.activate_worker_extension"),
            patch("pal.lsp.plugin.LspManagerPluginProvider.stop_manager"),
            patch("pal.web_fetch.browser_service.BrowserServiceManager.shutdown_async", new_callable=AsyncMock),
            patch.object(socket.socket, "connect", side_effect=AssertionError("socket forbidden")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("process forbidden")),
        ):
            bundle = runtime_build.build_slim_bunshin_runtime(
                Path({str(runtime_root)!r}), run_id="synthetic-role", llm_authority="manager_proxy",
                memory_workflow_id="synthetic-workflow",
            )
            try:
                db = SkillModel._meta.database
                assert os.statvfs(Path({str(runtime_root / "pal.sqlite3")!r})).f_flag & os.ST_RDONLY
                assert db.execute_sql("PRAGMA query_only").fetchone() == (1,)
                assert SkillModel.select().count() == 1
                lifecycle = lifecycle_instances[0]
                service = lifecycle.context.require_port("skill:skill")
                assert isinstance(service.repository, ReadOnlySkillRepository)
                assert service.repository.get_skill("test.user") is not None
                declared = next(s for s in service.repository.list_skills() if s.module_id == "lsp")
                assert SkillSearchTool(service).invoke({{"query": declared.skill_id}}).structured["count"] > 0
                assert SkillInjectTool(service).invoke({{"skill_id": declared.skill_id}}).status == "ok"
                lifecycle.withdraw_module_capabilities("lsp")
                assert service.repository.get_skill(declared.skill_id) is None
                assert SkillModel.select().count() == 1
                assert db.connection().total_changes == 0
            finally:
                asyncio.run(bundle.close())
        print("readonly-full-bootstrap-ok")
    ''')
    pack = BunshinInvocationPack(
        invocation_id="synthetic-attempt", goal="synthetic bootstrap",
        workspace={"repo_path": str(repo)},
        metadata={"sandbox": {"enabled": True, "backend": "bwrap", "run_id": "synthetic-role"}},
    )
    # Match the manager's ownership: retain read-only memory WAL sidecars
    # before constructing mounts and until the worker has finished shutdown.
    with storage.worker_read_lease("synthetic-workflow"):
        argv, env = build_sandboxed_runner_invocation(
            runtime_root=runtime_root, pack=pack, argv=[pal_harness_spec().worker_argv[0], "-c", script],
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
        result = subprocess.run(argv, env=env, cwd=repo, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "readonly-full-bootstrap-ok" in result.stdout
