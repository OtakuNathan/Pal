"""Background turns retain their owner without freezing other module lifecycles."""
import asyncio
from unittest.mock import patch

import pytest

from pal.core.scheduling import ResidentTurnKind
from pal.execution.contracts import CapabilityCall
from pal.memory.capabilities import register_with_core as register_memory
from pal.memory.service import MemoryService
from pal.plugins.host import PluginHost
from tests.core_factory import build_core
from tests.test_plugin_publication_rollback import MemoryPluginRepository
from tests.test_plugin_raii import _write_plugin
from tests.turn_fakes import continuation


@pytest.fixture
def background_host(tmp_path, monkeypatch):
    root = tmp_path / "builtin"
    for name in ("background_owner", "other_plugin"):
        _write_plugin(root, name)
    monkeypatch.syspath_prepend(str(root))
    core = build_core()
    register_memory(core.context, MemoryService())
    host = PluginHost(core.context, tmp_path, builtin_root=root,
        services={"ledger": []}, third_party_repository=MemoryPluginRepository())
    host.rescan()
    for name in ("background_owner", "other_plugin"):
        assert host.attach(name)["status"] == "ok"
    host.publish_management_capabilities()
    yield core, host
    host.shutdown()
    assert not host.shutdown_errors
    core.close()
    core.context.execution_runtime.shutdown()


@pytest.mark.parametrize("kind", [ResidentTurnKind.PROACTIVE, ResidentTurnKind.BACKGROUND])
def test_background_can_manage_another_plugin_and_retains_its_own_module(background_host, kind):
    core, host = background_host
    owner = host.generations["background_owner"].handle

    async def manage(name):
        return await core.context.execution_runtime.execute_async(CapabilityCall(
            name="manage_plugin", args={"name": name, "operation": "detach"}))

    async def run():
        async def work():
            assert (await manage("other_plugin")).status == "ok"
            result = await manage("background_owner")
            assert result.status == "error"
            assert "active background work" in result.llm_text
            assert host._record("background_owner").attached
            with pytest.raises(RuntimeError, match="active background work"):
                with host.package_lifecycle.switch("background_owner"):
                    pytest.fail("Package replacement entered while its owner was active")
            assert owner.mounted_subtree.mounted
            return "success"
        task = core.control_runtime.start_background_turn(continuation(turn_id="owned"), work,
                                                        owner=owner, kind=kind)
        assert task is not None
        await asyncio.wait_for(task, 5)
        await asyncio.sleep(0)
        assert (await manage("background_owner")).status == "ok"
        assert not core.state.active_turns and not core.state.turn_tasks
    asyncio.run(run())


def test_resident_completion_can_run_exclusive_tools(background_host):
    core, host = background_host

    async def run():
        async def work():
            result = await core.context.execution_runtime.execute_async(CapabilityCall(
                name="manage_plugin", args={"name": "other_plugin", "operation": "detach"}))
            assert result.status == "ok"
            return "success"
        task = core.control_runtime.start_background_turn(continuation(turn_id="completion"), work,
            owner=None, kind=ResidentTurnKind.BACKGROUND)
        assert task is not None
        await asyncio.wait_for(task, 5)
    asyncio.run(run())


def test_cancel_before_start_releases_module_without_running_its_body(background_host):
    core, host = background_host
    owner = host.generations["background_owner"].handle

    async def run():
        entered = False
        async def work():
            nonlocal entered
            entered = True
            return "success"
        task = core.control_runtime.start_background_turn(continuation(turn_id="cancelled"), work, owner=owner)
        assert task is not None
        assert host.detach("background_owner")["status"] == "error"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert not entered
        assert host.detach("background_owner")["status"] == "ok"
        assert core.control_runtime.start_background_turn(
            continuation(turn_id="retired"), work, owner=owner) is None
    asyncio.run(run())


@pytest.mark.parametrize("phase", ["busy", "admission", "registration"])
def test_failed_start_does_not_leak_module_ownership(background_host, phase):
    core, host = background_host
    owner = host.generations["background_owner"].handle

    async def run():
        async def work():
            return "success"
        if phase == "busy":
            core.state.memory_maintenance = True
            assert core.control_runtime.start_background_turn(continuation(), work, owner=owner) is None
            core.state.memory_maintenance = False
        else:
            method = "_admit_resident_turn" if phase == "admission" else "track_turn_task"
            with patch.object(core, method, side_effect=RuntimeError("start failed")):
                with pytest.raises(RuntimeError, match="start failed"):
                    core.control_runtime.start_background_turn(continuation(), work, owner=owner)
            for _ in range(3):
                await asyncio.sleep(0)
        assert host.detach("background_owner")["status"] == "ok"
    asyncio.run(run())


def test_failed_withdrawal_restores_module_background_admission(background_host):
    core, host = background_host
    owner = host.generations["background_owner"].handle

    async def run():
        async def work():
            return "success"
        with patch.object(core.context.execution_runtime.implementation, "unmount_subtree", side_effect=RuntimeError("registry unavailable")):
            assert host.detach("background_owner")["status"] == "error"
        task = core.control_runtime.start_background_turn(continuation(), work, owner=owner)
        assert task is not None
        await task
        assert host.detach("background_owner")["status"] == "ok"
    asyncio.run(run())
