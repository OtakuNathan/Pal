"""Entry withdrawal composes with package jobs and main-turn serial dispatch."""
import asyncio
import sys
import threading
from unittest.mock import AsyncMock

import pytest

from pal.core import PalCore
from pal.core.turns import ToolCallEffect
from pal.execution import register_with_core
from pal.execution.tool_facade import EmptyToolInput, EmptyToolOutput, RejectedResult
from pal.execution.tool_semantics import DIRECT_NONE
from pal.foundation.persistence import PalV2Database
from pal.packages.integration import RuntimeActivation
from pal.packages.process import CommandControl, PackageError, check_cancelled, command_control
from pal.plugins.capabilities import register_with_core as register_plugins
from pal.plugins.host import PluginHost
from pal.plugins.models import PluginBundleModel
from pal.shared.tool_protocol import new_tool_call
from tests.capability_fixture import mount_test_capability
from tests.test_plugin_raii import _write_plugin
from tests.turn_fakes import continuation


PLUGIN = '''from pal.core.module_registry import ModuleHandle, MODULE_TIER_DETACHABLE
from pal.execution.tool_facade import EmptyToolInput, EmptyToolOutput
from pal.execution.tool_semantics import DIRECT_NONE
from tests.capability_fixture import build_test_capability_handle
class Provider:
    def __init__(self, services): self.services = services
    def build_mounted_subtree(self, **kwargs):
        return build_test_capability_handle(alias="demo_read", canonical_path="op_test_demo_read",
            InputModel=EmptyToolInput, OutputModel=EmptyToolOutput,
            handler=lambda _: self.services["handler"](), execution=DIRECT_NONE,
            module_id="demo").mounted_subtree
class Plugin:
    def __init__(self, services): self.services = services
    def start(self, scope):
        handle = ModuleHandle(module_id="demo", tier=MODULE_TIER_DETACHABLE, detachable=True,
            introspection_provider=Provider(self.services))
        scope.context.register_module(handle)
        scope.defer(lambda: self.services["cleanup"]())
        return handle
def build_plugin(context): return Plugin(context.services["callbacks"])
'''


@pytest.fixture
def live_plugin(tmp_path):
    database = PalV2Database(tmp_path / 'pal.sqlite3')
    database.initialize([PluginBundleModel])
    community = tmp_path / 'plugins/community'
    _write_plugin(community, 'demo')
    (community / 'demo_runtime.py').write_text(PLUGIN)
    sys.path.insert(0, str(community))
    core = PalCore()
    register_with_core(core.context)
    core.publish_module_capabilities('execution')
    events = []
    services = {'handler': lambda: events.append('handler') or {},
                'cleanup': lambda: events.append('cleanup')}
    host = PluginHost(core.context, tmp_path, services={'callbacks': services})
    management = register_plugins(core.context, host)
    core.publish_module_capabilities('plugins')
    host.bootstrap()
    ex = core.turn_executor
    ex._build_tool_call_budget = lambda *a, **kw: None
    ex._log_tool_call_start = lambda *a: None
    ex._log_tool_call_result = lambda *a: None
    ex._maybe_echo_tool_result_async = AsyncMock()
    ex._append_l1_tool_result_async = AsyncMock()
    try:
        yield core, host, management.introspection_provider, services, events
    finally:
        management.introspection_provider.shutdown_packages()
        host.shutdown()
        core.context.execution_runtime.shutdown()
        sys.path.remove(str(community))
        sys.modules.pop('demo_runtime', None)
        database.close()


@pytest.mark.parametrize("phase", ["publication", "replay"])
def test_failed_attach_revokes_calls_even_if_registry_withdrawal_fails(live_plugin, monkeypatch, phase):
    core, host, _, services, events = live_plugin
    runtime = core.context.execution_runtime
    assert host.detach("demo")["status"] == "ok"
    events.clear()
    captured = []
    name = "_publish_module_capabilities" if phase == "publication" else "_replay_optional_contributions"
    original = getattr(host, name)
    unmount = runtime.unmount_subtree

    def fail_publication(*args):
        original(*args)
        captured.append(runtime.registry_generation)
        raise RuntimeError("publication failed")

    def fail_withdrawal(handle):
        if handle.module_id == "demo":
            raise RuntimeError("registry rebuild failed")
        return unmount(handle)

    with monkeypatch.context() as patch:
        patch.setattr(host, name, fail_publication)
        patch.setattr(runtime, "unmount_subtree", fail_withdrawal)
        assert host.attach("demo")["status"] == "error"
        assert "cleanup" not in events
        result = runtime.invoke_direct_tool(new_tool_call(name="demo_read", args={}), generation=captured[0])
        assert isinstance(result, RejectedResult)
        assert result.error_code == "unknown_tool"
        assert "handler" not in events
        result = host.detach("demo")
        assert result["status"] == "error"
        assert not result["attached"]
        assert host._record("demo").last_load_status == "cleanup_failed"
    assert host.detach("demo")["status"] == "ok"
    assert events.count("cleanup") == 1


def test_detach_surface_failure_keeps_resources_until_callbacks_are_withdrawn(live_plugin, monkeypatch):
    core, host, _, _, events = live_plugin
    with monkeypatch.context() as patch:
        def fail_surface(_):
            raise RuntimeError("event callback withdrawal failed")
        patch.setattr(host, "_withdraw_generation_surface", fail_surface)
        result = host.detach("demo")
        assert result["status"] == "error"
        assert not result["attached"]
        assert "cleanup" not in events
        assert core.context.module_registry.get("demo") is not None
    assert host.detach("demo")["status"] == "ok"
    assert events.count("cleanup") == 1


def test_serial_package_uninstall_withdraws_entry_before_cleanup(live_plugin, monkeypatch):
    core, host, provider, services, events = live_plugin
    runtime = core.context.execution_runtime
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    def cleanup():
        events.append('cleanup_start')
        cleanup_started.set()
        assert release_cleanup.wait(5)
        events.append('cleanup_end')
    services['cleanup'] = cleanup

    async def scenario():
        cont = continuation(turn_id='single-conversation')
        first = await core.turn_executor.execute_turn_effect_async(cont, ToolCallEffect(tool_call=new_tool_call(
            name='call_tool', args={'name': 'uninstall_plugin', 'args': {'name': 'demo', 'wait_ms': 1000}})))
        assert first.payload.ok
        assert first.payload.structured['status'] == 'running'
        assert cleanup_started.is_set()
        assert runtime.get_tool_spec('demo_read') is None
        try:
            second = await core.turn_executor.execute_turn_effect_async(cont,
                ToolCallEffect(tool_call=new_tool_call(name='demo_read', args={})))
            assert not second.payload.ok
            assert 'handler' not in events
        finally:
            release_cleanup.set()
        job = provider.jobs().threads[first.payload.structured['job_id']][0]
        await asyncio.to_thread(job.join, 2)
        assert not job.is_alive()
    try:
        asyncio.run(asyncio.wait_for(scenario(), 6))
    finally:
        release_cleanup.set()


def test_entry_withdraws_before_writer_wait_and_admitted_call_finishes(live_plugin):
    core, host, provider, services, events = live_plugin
    runtime = core.context.execution_runtime
    started, release = threading.Event(), threading.Event()
    def handler():
        started.set()
        assert release.wait(5)
        events.append('admitted_finished')
        return {}
    services['handler'] = handler

    async def scenario():
        admitted = asyncio.create_task(runtime.execute_tool_async(new_tool_call(name='demo_read', args={})))
        assert await asyncio.to_thread(started.wait, 2)
        job = provider.jobs().start('uninstall', name='demo', wait_ms=1000)
        try:
            assert job['status'] == 'running'
            assert runtime.get_tool_spec('demo_read') is None
            assert host.generations['demo'].handle.mounted
            assert 'cleanup' not in events
            rejected = await runtime.execute_tool_async(new_tool_call(name='demo_read', args={}))
            assert not rejected.ok
        finally:
            release.set()
        assert (await admitted).ok
        thread = provider.jobs().threads[job['job_id']][0]
        await asyncio.to_thread(thread.join, 2)
        assert not thread.is_alive()
        assert events == ['admitted_finished', 'cleanup']
    try:
        asyncio.run(asyncio.wait_for(scenario(), 7))
    finally:
        release.set()


@pytest.mark.parametrize('change', ['withdraw', 'remount', 'unrelated'])
def test_queued_binding_fences_mount_incarnation(live_plugin, monkeypatch, change):
    core, host, provider, services, events = live_plugin
    runtime = core.context.execution_runtime
    entered = threading.Event()
    acquire = runtime.lifecycle_gate._acquire_read
    def observed_acquire():
        entered.set()
        return acquire()
    monkeypatch.setattr(runtime.lifecycle_gate, '_acquire_read', observed_acquire)
    handle = host.generations['demo'].handle
    async def scenario():
        with runtime.lifecycle_gate.write():
            waiting = asyncio.create_task(runtime.execute_tool_async(new_tool_call(name='demo_read', args={})))
            assert await asyncio.to_thread(entered.wait, 2)
            if change == 'unrelated':
                mount_test_capability(runtime, alias='other', canonical_path='op_test_other',
                    InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=lambda _: {}, execution=DIRECT_NONE)
            else:
                runtime.unmount_subtree(handle)
                if change == 'remount':
                    runtime.mount_subtree(handle)
        result = await waiting
        assert result.ok == (change == 'unrelated')
        assert events == (['handler'] if change == 'unrelated' else [])
        if change == 'remount':
            assert (await runtime.execute_tool_async(new_tool_call(name='demo_read', args={}))).ok
    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_cancelled_precleanup_switch_restores_fresh_entry(live_plugin):
    core, host, provider, services, events = live_plugin
    runtime = core.context.execution_runtime
    old = runtime.registry_generation.record_for_alias('demo_read').binding.admission
    control = CommandControl()
    activation = RuntimeActivation(host)
    with pytest.raises(PackageError):
        with command_control(control):
            with activation.switch_gate('plugin', 'demo'):
                assert runtime.get_tool_spec('demo_read') is None
                control.stop()
                check_cancelled()
    new = runtime.registry_generation.record_for_alias('demo_read').binding.admission
    assert new is not old and new.live and not old.live
    assert events == []
    assert runtime.execute_tool(new_tool_call(name='demo_read', args={})).ok


def test_cleanup_failure_keeps_owner_without_republishing(live_plugin):
    core, host, provider, services, events = live_plugin
    def fail_cleanup():
        raise RuntimeError('cleanup failed')
    services['cleanup'] = fail_cleanup
    with RuntimeActivation(host).switch_gate('plugin', 'demo'):
        assert host.detach('demo')['status'] == 'error'
    assert 'demo' in host.generations
    assert host.generations['demo'].cleanup_started
    assert core.context.execution_runtime.get_tool_spec('demo_read') is None


def test_invalid_purge_does_not_withdraw(live_plugin):
    core, host, provider, services, events = live_plugin
    runtime = core.context.execution_runtime
    before = runtime.registry_generation
    with pytest.raises(PackageError, match='declaration'):
        provider.jobs().service.uninstall('demo', purge_data=True)
    assert runtime.registry_generation is before
    assert runtime.get_tool_spec('demo_read') is not None
    assert events == []


def test_failed_writer_entry_restores_untouched_generation(live_plugin, monkeypatch):
    from contextlib import contextmanager
    core, host, provider, services, events = live_plugin
    activation = RuntimeActivation(host)
    runtime = core.context.execution_runtime
    old = runtime.registry_generation.record_for_alias('demo_read').binding.admission
    @contextmanager
    def fail_gate():
        assert runtime.get_tool_spec('demo_read') is None
        raise RuntimeError('writer admission failed')
        yield
    monkeypatch.setattr(activation, 'gate', fail_gate)
    with pytest.raises(RuntimeError, match='writer admission failed'):
        with activation.switch_gate('plugin', 'demo'):
            pytest.fail('gate body entered')
    assert not old.live
    assert runtime.get_tool_spec('demo_read') is not None
    assert runtime.execute_tool(new_tool_call(name='demo_read', args={})).ok


def test_detach_preflight_refusal_keeps_all_entries(live_plugin, monkeypatch):
    core, host, provider, services, events = live_plugin
    runtime = core.context.execution_runtime
    before = runtime.registry_generation
    def refuse(handle):
        raise RuntimeError('owner busy')
    monkeypatch.setattr(type(runtime), 'check_detach', lambda self, handle: refuse(handle))
    with pytest.raises(RuntimeError, match='owner busy'):
        with RuntimeActivation(host).switch_gate('plugin', 'demo'):
            pytest.fail('refused operation entered')
    assert runtime.registry_generation is before
    assert runtime.get_tool_spec('demo_read') is not None
    assert not host.generations['demo'].cleanup_started
    assert events == []


def test_stale_empty_subtree_cannot_withdraw_replacement(live_plugin):
    from types import SimpleNamespace
    from pal.shared import MountedSubtreeHandle
    core, host, provider, services, events = live_plugin
    runtime = core.context.execution_runtime
    stale = SimpleNamespace(mounted_subtree=MountedSubtreeHandle(module_id='empty-owner'))
    runtime.mount_subtree(stale)
    replacement = mount_test_capability(runtime, alias='replacement', canonical_path='op_test_replacement',
        InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=lambda _: {}, execution=DIRECT_NONE)
    runtime.unmount_subtree(replacement)
    replacement.mounted_subtree.module_id = 'empty-owner'
    runtime.mount_subtree(replacement)
    runtime.unmount_subtree(stale)
    assert runtime.get_tool_spec('replacement') is not None
    assert runtime.execute_tool(new_tool_call(name='replacement', args={})).ok


def test_dependent_cleanup_failure_restores_only_untouched_root(live_plugin):
    core, host, provider, services, events = live_plugin
    community = host.runtime_root / 'plugins/community'
    _write_plugin(community, 'dependent', requires=('demo',))
    (community / 'dependent_runtime.py').write_text(
        PLUGIN.replace('demo', 'dependent').replace('self.services["cleanup"]()', 'self.services["dependent_cleanup"]()'))
    def fail():
        raise RuntimeError('dependent cleanup failed')
    services['dependent_cleanup'] = fail
    try:
        host.rescan()
        assert host.enable('dependent')['status'] == 'ok'
        runtime = core.context.execution_runtime
        old_root = runtime.registry_generation.record_for_alias('demo_read').binding.admission
        with RuntimeActivation(host).switch_gate('plugin', 'demo'):
            assert runtime.get_tool_spec('demo_read') is None
            assert runtime.get_tool_spec('dependent_read') is None
            assert host.detach('demo')['status'] == 'error'
        assert runtime.get_tool_spec('dependent_read') is None
        assert runtime.get_tool_spec('demo_read') is not None
        assert not old_root.live
        assert host.generations['dependent'].cleanup_started
        assert not host.generations['demo'].cleanup_started
        assert runtime.execute_tool(new_tool_call(name='demo_read', args={})).ok
    finally:
        services['dependent_cleanup'] = lambda: None
        host.detach('dependent')
        sys.modules.pop('dependent_runtime', None)


def test_targeted_alias_uses_selected_live_target_not_retired_representative(live_plugin):
    from pal.execution.tool_facade import StrictToolModel
    class TargetInput(StrictToolModel):
        target: str
    core, host, provider, services, events = live_plugin
    runtime = core.context.execution_runtime
    handles = []
    for target in ('a', 'b'):
        handles.append(mount_test_capability(runtime, alias='target_read', canonical_path='op_test_target_read',
            InputModel=TargetInput, OutputModel=EmptyToolOutput, handler=lambda _: {}, execution=DIRECT_NONE,
            target_id=target, metadata={'target_argument': 'target'}, examples=({'target': 'a'},)))
    captured = runtime.registry_generation
    runtime.unmount_subtree(handles[0])
    result = runtime.invoke_direct_tool(new_tool_call(name='target_read', args={'target': 'b'}), generation=captured)
    assert result.kind == 'complete'
    rejected = runtime.invoke_direct_tool(new_tool_call(name='target_read', args={'target': 'a'}), generation=captured)
    assert rejected.kind == 'rejected'


def test_partial_cleanup_reports_committed_unload_and_retries_owned_resource(live_plugin):
    core, host, provider, services, events = live_plugin
    runtime = core.context.execution_runtime
    generation = host.generations['demo']
    attempts = []
    def close_once():
        events.append('irreversible_close')
    def fail_then_succeed():
        attempts.append('attempt')
        if len(attempts) == 1:
            raise RuntimeError('remaining resource busy')
    generation.scope.defer(fail_then_succeed)
    generation.scope.defer(close_once)
    result = runtime.execute_tool(new_tool_call(name='call_tool', args={'name': 'detach_plugin', 'args': {'name': 'demo'}}))
    assert not result.ok
    assert result.invocation_result.effect.value == 'applied'
    assert result.invocation_result.retry.value == 'reconcile_first'
    assert result.invocation_result.details['logical_unload_committed'] is True
    assert result.invocation_result.details['cleanup_pending'] is True
    assert 'Resource cleanup is incomplete' in result.invocation_result.recovery_hint
    assert 'Resource cleanup' in result.llm_text
    assert 'logically detached' in result.llm_text
    assert runtime.get_tool_spec('demo_read') is None
    assert host.generations['demo'] is generation
    assert not host._record('demo').attached
    assert host.detach('demo')['status'] == 'ok'
    assert events.count('irreversible_close') == 1
    assert attempts == ['attempt', 'attempt']
    assert 'demo' not in host.generations


def test_precleanup_cancel_restores_service_intent_as_well_as_entry(live_plugin, monkeypatch):
    from contextlib import contextmanager
    from pal.packages.uninstall import removal_record
    core, host, provider, services, events = live_plugin
    service = provider.jobs().service
    activation = service.activation
    original = activation.switch_gate
    from pal.packages.process import atomic_json
    import json
    previous = {'id': 'demo', 'kind': 'plugin', 'status': 'ready', 'stage': 'complete'}
    record_path = service._record_path('plugin', 'demo')
    atomic_json(record_path, previous)
    control = CommandControl()
    @contextmanager
    def cancel_after_withdrawal(kind, name):
        with original(kind, name):
            control.stop()
            yield
    monkeypatch.setattr(activation, 'switch_gate', cancel_after_withdrawal)
    with command_control(control), pytest.raises(PackageError):
        service.uninstall('demo')
    assert removal_record(host.runtime_root, 'demo') is None
    assert json.loads(record_path.read_text()) == previous
    assert core.context.execution_runtime.get_tool_spec('demo_read') is not None
    assert host.attach('demo')['status'] == 'ok'
    assert events == []


def test_successful_uninstall_retry_clears_pending_cleanup_diagnostics(live_plugin):
    core, host, provider, services, events = live_plugin
    attempts = []
    def fail_once():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError('resource busy')
    services['cleanup'] = fail_once
    service = provider.jobs().service
    with pytest.raises(PackageError, match='cleanup'):
        service.uninstall('demo')
    result = service.uninstall('demo')
    assert result['status'] == 'uninstalled'
    assert result['cleanup_pending'] is False
    assert 'recovery_hint' not in result
    assert 'cleanup_plugin_id' not in result


def test_precommit_detach_refusal_rolls_back_new_retained_settings(live_plugin, monkeypatch):
    from pal.packages.uninstall import removal_record
    core, host, provider, services, events = live_plugin
    monkeypatch.setattr(host, 'detach', lambda name: {'status': 'error', 'error': 'owner refused before cleanup'})
    with pytest.raises(PackageError, match='cleanup'):
        provider.jobs().service.uninstall('demo')
    assert host.settings.get('plugin.retained:demo') is None
    assert removal_record(host.runtime_root, 'demo') is None
    assert core.context.execution_runtime.get_tool_spec('demo_read') is not None
    assert not host.generations['demo'].cleanup_started
    assert events == []
