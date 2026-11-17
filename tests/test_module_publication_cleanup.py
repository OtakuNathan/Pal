"""Failed projections remain owned until their compensation finishes."""
from __future__ import annotations

import asyncio

import pytest

from pal.core.module_publication import ModulePublications
from pal.core.module_registry import ModuleHandle


def handle(name):
    return ModuleHandle(name, "test")


class Projection:
    def __init__(self):
        self.visible = {}
        self.calls = []
        self.reject_publish = lambda owner: False
        self.reject_withdraw = lambda owner: False

    def published(self, owner):
        self.calls.append(("publish", owner))
        self.visible[owner.module_id] = owner
        if self.reject_publish(owner):
            raise OSError("projection publication failed")

    def withdrawn(self, owner):
        self.calls.append(("withdraw", owner))
        if self.reject_withdraw(owner):
            raise OSError("projection withdrawal failed")
        self.visible.pop(owner.module_id, None)

    def count(self, operation, owner):
        return sum(kind == operation and value is owner for kind, value in self.calls)


def test_failed_publication_retries_cleanup_without_repeating_successful_compensation():
    publications = ModulePublications()
    first, failing = Projection(), Projection()
    publications.subscribe(handle("first"), first)
    publications.subscribe(handle("failing"), failing)
    candidate = handle("candidate")
    failing.reject_publish = lambda owner: owner is candidate
    failing.reject_withdraw = lambda owner: owner is candidate

    with pytest.raises(OSError, match="publication failed"):
        publications.publish(candidate)
    assert first.visible == {}  # Other compensations ran despite the failure.
    assert failing.visible["candidate"] is candidate
    assert first.count("withdraw", candidate) == 1
    with pytest.raises(OSError, match="withdrawal failed"):
        publications.withdraw(candidate)
    assert first.count("withdraw", candidate) == 1

    successor = handle("candidate")
    with pytest.raises(RuntimeError, match="another generation"):
        publications.publish(successor)
    publications.withdraw(successor)  # A foreign owner cannot finish cleanup.
    assert failing.visible["candidate"] is candidate

    failing.reject_withdraw = lambda owner: False
    publications.withdraw(candidate)
    assert failing.visible == {}
    publications.publish(successor)
    publications.withdraw(candidate)
    assert first.visible["candidate"] is successor
    assert failing.visible["candidate"] is successor
    assert publications.handles["candidate"] is successor


def test_failed_previous_projection_restoration_retains_its_completed_withdrawal():
    publications = ModulePublications()
    observer = Projection()
    publications.subscribe(handle("observer"), observer)
    previous, candidate, successor = (handle("module") for _ in range(3))
    publications.publish(previous)
    observer.reject_publish = lambda owner: owner is candidate or owner is previous

    with pytest.raises(OSError, match="publication failed"):
        publications.publish(candidate)
    assert publications.handles["module"] is previous
    assert observer.count("withdraw", candidate) == 1
    with pytest.raises(OSError, match="publication failed"):
        publications.withdraw(candidate)
    assert observer.count("withdraw", candidate) == 1
    with pytest.raises(RuntimeError, match="cleanup remains pending"):
        publications.withdraw(previous)
    with pytest.raises(RuntimeError, match="another generation"):
        publications.publish(successor)

    observer.reject_publish = lambda owner: owner is candidate
    publications.withdraw(candidate)
    assert observer.count("withdraw", candidate) == 1
    assert observer.visible["module"] is previous
    publications.publish(successor)
    publications.withdraw(candidate)
    assert observer.visible["module"] is successor


def test_withdraw_failed_refresh_discards_restoration_and_retries_real_cleanup():
    from pal.shared.capability_forest import MountedSubtreeHandle

    publications = ModulePublications()
    observer = Projection()
    publications.subscribe(handle("observer"), observer)
    current = handle("module")
    current.mounted_subtree = MountedSubtreeHandle("module")
    publications.publish(current)
    current.mounted_subtree = MountedSubtreeHandle("module")
    observer.reject_publish = lambda owner: owner is current

    with pytest.raises(OSError, match="publication failed"):
        publications.publish(current)
    assert observer.visible["module"] is current  # Partially restored projection.
    published = observer.count("publish", current)
    observer.reject_withdraw = lambda owner: owner is current
    with pytest.raises(OSError, match="withdrawal failed"):
        publications.withdraw(current)
    assert publications.handles["module"] is current
    successor = handle("module")
    with pytest.raises(RuntimeError, match="another generation"):
        publications.publish(successor)

    observer.reject_withdraw = lambda owner: False
    publications.withdraw(current)
    assert observer.visible == {}
    assert observer.count("publish", current) == published
    publications.publish(successor)
    publications.withdraw(current)
    assert observer.visible["module"] is successor


def test_retiring_observer_cleans_partial_restoration_independently():
    publications = ModulePublications()
    remaining, retiring = Projection(), Projection()
    owner = handle("retiring")
    publications.subscribe(handle("remaining"), remaining)
    publications.subscribe(owner, retiring)
    previous, candidate = handle("module"), handle("module")
    publications.publish(previous)
    remaining.reject_withdraw = lambda value: value is candidate
    retiring.reject_publish = lambda value: True
    with pytest.raises(OSError, match="publication failed"):
        publications.publish(candidate)
    assert retiring.visible["module"] is previous
    published = retiring.count("publish", previous)
    remaining_calls = list(remaining.calls)

    retiring.reject_withdraw = lambda value: value is previous
    with pytest.raises(OSError, match="withdrawal failed"):
        publications.unsubscribe(owner)
    assert publications.observers["retiring"] == (owner, retiring)
    retiring.reject_withdraw = lambda value: False
    publications.unsubscribe(owner)
    assert retiring.visible == {}
    assert retiring.count("publish", previous) == published
    assert remaining.calls == remaining_calls
    assert "retiring" not in publications.observers
    with pytest.raises(OSError, match="withdrawal failed"):
        publications.withdraw(candidate)
    remaining.reject_withdraw = lambda value: False
    publications.withdraw(candidate)
    assert remaining.visible["module"] is previous


def test_observer_resources_remain_owned_while_publication_cleanup_needs_them():
    publications = ModulePublications()
    owner, candidate = handle("observer"), handle("module")
    observer = Projection()
    publications.subscribe(owner, observer)
    observer.reject_publish = lambda value: value is candidate
    observer.reject_withdraw = lambda value: value is candidate
    with pytest.raises(OSError, match="publication failed"):
        publications.publish(candidate)
    with pytest.raises(OSError, match="withdrawal failed"):
        publications.unsubscribe(owner)
    assert publications.observers["observer"] == (owner, observer)

    observer.reject_withdraw = lambda value: False
    publications.unsubscribe(owner)
    assert observer.visible == {}
    assert "observer" not in publications.observers
    calls = list(observer.calls)
    publications.withdraw(candidate)
    assert observer.calls == calls


def test_failed_subscription_replay_keeps_cleanup_and_rejects_a_successor():
    publications = ModulePublications()
    first, second = handle("first"), handle("second")
    publications.publish(first)
    publications.publish(second)
    owner, successor = handle("observer"), handle("observer")
    observer, replacement = Projection(), Projection()
    observer.reject_publish = lambda value: value is second
    observer.reject_withdraw = lambda value: value is second

    with pytest.raises(OSError, match="publication failed"):
        publications.subscribe(owner, observer)
    assert "first" not in observer.visible
    assert observer.visible["second"] is second
    assert observer.count("withdraw", first) == 1
    with pytest.raises(OSError, match="withdrawal failed"):
        publications.unsubscribe(owner)
    assert observer.count("withdraw", first) == 1
    with pytest.raises(RuntimeError, match="another generation"):
        publications.subscribe(successor, replacement)
    publications.unsubscribe(successor)
    assert replacement.calls == []

    observer.reject_withdraw = lambda value: False
    publications.unsubscribe(owner)
    assert observer.visible == {}
    publications.subscribe(successor, replacement)
    publications.unsubscribe(owner)
    assert publications.observers["observer"] == (successor, replacement)
    assert replacement.visible == {"first": first, "second": second}


def test_cancelled_compensation_remains_retryable_without_skipping_other_observers():
    publications = ModulePublications()
    first, failing = Projection(), Projection()
    publications.subscribe(handle("first"), first)
    publications.subscribe(handle("failing"), failing)
    candidate = handle("candidate")
    failing.reject_publish = lambda owner: owner is candidate
    withdraw = failing.withdrawn

    def cancelled(owner):
        raise asyncio.CancelledError("compensation cancelled")

    failing.withdrawn = cancelled
    with pytest.raises(OSError, match="publication failed"):
        publications.publish(candidate)
    assert first.visible == {}
    with pytest.raises(asyncio.CancelledError, match="compensation cancelled"):
        publications.withdraw(candidate)
    failing.withdrawn = withdraw
    publications.withdraw(candidate)
    assert failing.visible == {}


def test_plugin_retains_failed_declared_skill_cleanup_until_detach_retry(tmp_path, monkeypatch):
    from pal.foundation.persistence import PalV2Database
    from pal.plugins.host import PluginHost
    from pal.skill.capabilities import register_with_core as register_skill
    from pal.skill.models import SkillModel
    from pal.skill.repository import SkillRepository
    from pal.skill.service import SkillService
    from tests.core_factory import build_core
    from tests.test_plugin_publication_rollback import MemoryPluginRepository
    from tests.test_plugin_raii import _write_plugin

    root = tmp_path / "builtin"
    _write_plugin(root, "failed_skill_projection")
    source = root / "failed_skill_projection_runtime.py"
    source.write_text(source.read_text().replace("class Provider:", """from pal.skill.contracts import SkillDescriptor, SKILL_SOURCE_DECLARED
class Provider:
    def declared_skills(self):
        yield SkillDescriptor(skill_id='failed_skill_projection.one', module_id='failed_skill_projection', source_kind=SKILL_SOURCE_DECLARED, title='Demo', summary='Demo', manual_text='Demo')
        raise OSError('source failure after first declaration')"""))
    monkeypatch.syspath_prepend(str(root))
    database = PalV2Database(tmp_path / "skills.sqlite3")
    database.initialize([SkillModel])
    core = build_core()
    repository = SkillRepository()
    register_skill(core.context, SkillService(repository=repository))
    delete = repository.delete_declared_skills_for_module
    attempts = 0
    blocked = True

    def fail_cleanup(module_id):
        nonlocal attempts
        if module_id == "failed_skill_projection":
            attempts += 1
            if attempts > 1 and blocked:
                raise OSError("skill cleanup remains blocked")
        return delete(module_id)

    monkeypatch.setattr(repository, "delete_declared_skills_for_module", fail_cleanup)
    host = PluginHost(core.context, tmp_path, builtin_root=root, services={"ledger": []},
                      third_party_repository=MemoryPluginRepository())
    try:
        host.rescan()
        result = host.attach("failed_skill_projection")
        assert result["status"] == "error" and result["cleanup_pending"]
        assert repository.get_skill("failed_skill_projection.one") is not None
        assert not host._record("failed_skill_projection").attached
        assert host.detach("failed_skill_projection")["cleanup_pending"]
        assert host.attach("failed_skill_projection")["status"] == "error"

        blocked = False
        assert host.detach("failed_skill_projection")["status"] == "ok"
        assert repository.get_skill("failed_skill_projection.one") is None
        assert core.context.module_registry.get("failed_skill_projection") is None
        assert "failed_skill_projection" not in host.generations
    finally:
        blocked = False
        host.shutdown()
        core.close()
        core.context.execution_runtime.shutdown()
        database.close()


@pytest.mark.parametrize("operation", ("detach", "reload"))
def test_plugin_can_retire_after_declared_skill_refresh_and_restoration_fail(tmp_path, monkeypatch, operation):
    from pal.foundation.persistence import PalV2Database
    from pal.plugins.host import PluginHost
    from pal.skill.capabilities import register_with_core as register_skill
    from pal.skill.models import SkillModel
    from pal.skill.repository import SkillRepository
    from pal.skill.service import SkillService
    from tests.core_factory import build_core
    from tests.test_plugin_publication_rollback import MemoryPluginRepository
    from tests.test_plugin_raii import _write_plugin

    root = tmp_path / "builtin"
    name = "failed_skill_refresh"
    _write_plugin(root, name)
    source = root / f"{name}_runtime.py"
    source.write_text(source.read_text().replace("class Provider:", """from pal.skill.contracts import SkillDescriptor, SKILL_SOURCE_DECLARED
class Provider:
    broken = False
    def declared_skills(self):
        if self.broken:
            raise OSError('declarations unavailable')
        return (SkillDescriptor(skill_id='failed_skill_refresh.one', module_id='failed_skill_refresh', source_kind=SKILL_SOURCE_DECLARED, title='Demo', summary='Demo', manual_text='Demo'),)"""))
    monkeypatch.syspath_prepend(str(root))
    database = PalV2Database(tmp_path / "skills.sqlite3")
    database.initialize([SkillModel])
    core = build_core()
    repository = SkillRepository()
    register_skill(core.context, SkillService(repository=repository))
    host = PluginHost(core.context, tmp_path, builtin_root=root, services={"ledger": []},
                      third_party_repository=MemoryPluginRepository())
    try:
        host.rescan()
        assert host.attach(name)["status"] == "ok"
        previous = host.generations[name].handle
        provider = previous.introspection_provider
        assert repository.get_skill("failed_skill_refresh.one") is not None
        provider.broken = True
        with pytest.raises(OSError, match="declarations unavailable"):
            core.context.refresh_module_capabilities(previous)

        if operation == "reload":
            assert host.reload_module(name).status == "ok"
            assert host.generations[name].handle is not previous
            assert repository.get_skill("failed_skill_refresh.one") is not None
        else:
            assert host.detach(name)["status"] == "ok"
            assert core.context.module_registry.get(name) is None
            assert repository.get_skill("failed_skill_refresh.one") is None
        assert provider.broken  # Recovery never repairs the retiring instance.
        assert name not in core.context.module_publications._pending_publications
    finally:
        host.shutdown()
        core.close()
        core.context.execution_runtime.shutdown()
        database.close()


def test_plugin_retains_failed_observer_replay_until_detach_retry(tmp_path, monkeypatch):
    from pal.control.contracts import ControlAction
    from pal.plugins.host import PluginHost
    from tests.core_factory import build_core
    from tests.test_plugin_publication_rollback import MemoryPluginRepository
    from tests.test_plugin_raii import _write_plugin

    root = tmp_path / "builtin"
    _write_plugin(root, "failed_replay")
    source = root / "failed_replay_runtime.py"
    source.write_text("from pal.core.control_action_registry import ControlActionSpec\n" +
        source.read_text().replace("        return handle", """        handle.module_observer = self.ledger[0]
        async def control(value):
            self.ledger.append(('control', value))
        handle.control_action_handlers = {'failed_replay_action': ControlActionSpec(control)}
        handle.configuration_handlers = {'failed_replay.configure': control}
        return handle"""))
    monkeypatch.syspath_prepend(str(root))
    core = build_core()
    existing = handle("existing")
    core.context.module_publications.publish(existing)
    observer = Projection()
    observer.reject_publish = lambda owner: owner is existing
    observer.reject_withdraw = lambda owner: owner is existing
    host = PluginHost(core.context, tmp_path, builtin_root=root, services={"ledger": [observer]},
                      third_party_repository=MemoryPluginRepository())
    try:
        host.rescan()
        result = host.attach("failed_replay")
        assert result["status"] == "error" and result["cleanup_pending"]
        assert observer.visible["existing"] is existing
        assert not asyncio.run(core.context.control_action_registry.handle(
            ControlAction("failed_replay_action", "failed_replay")))
        assert "failed_replay.configure" not in core.context.configuration_operations
        assert host.detach("failed_replay")["cleanup_pending"]

        observer.reject_withdraw = lambda owner: False
        assert host.detach("failed_replay")["status"] == "ok"
        assert observer.visible == {}
        assert core.context.module_registry.get("failed_replay") is None
        assert core.context.module_publications.handles["existing"] is existing
    finally:
        observer.reject_withdraw = lambda owner: False
        host.shutdown()
        core.close()
        core.context.execution_runtime.shutdown()


@pytest.mark.parametrize("cancelled", [False, True])
def test_main_context_retains_rejected_observer_until_explicit_unregister(cancelled):
    from pal.control.contracts import ControlAction
    from pal.core.control_action_registry import ControlActionSpec
    from pal.core.event_handler_registry import EventHandlerRegistration
    from tests.core_factory import build_core

    core = build_core()
    context = core.context
    existing = handle("existing")
    context.module_publications.publish(existing)
    observer = Projection()
    failure = asyncio.CancelledError("replay cancelled") if cancelled else OSError("replay failed")

    def fail_replay(owner):
        observer.visible[owner.module_id] = owner
        raise failure

    observer.published = fail_replay
    observer.reject_withdraw = lambda owner: owner is existing
    calls = []

    async def callback(value=None):
        calls.append(value)

    timers = core.control_runtime.open_timer_scope()
    timer = timers.schedule(0, callback)
    provider = object()
    candidate = ModuleHandle("rejected", "test", module_observer=observer,
        timer_scope=timers, control_action_handlers={"rejected_action": ControlActionSpec(callback)},
        configuration_handlers={"rejected.configure": callback},
        provider_refs=["rejected:provider"], ports={"provider:rejected:provider": provider})
    queued_handler = EventHandlerRegistration(callback, candidate)
    successor = handle("rejected")
    try:
        with pytest.raises(type(failure)) as caught:
            context.register_module(candidate)
        assert caught.value is failure
        assert any("registration cleanup remains pending" in note for note in failure.__notes__)
        assert context.module_registry.require("rejected") is candidate
        assert not candidate.mounted and not queued_handler.is_current(context)
        assert timers.closed and not timer.pending
        asyncio.run(timer.fire())
        assert not asyncio.run(context.control_action_registry.handle(ControlAction("rejected_action", "rejected")))
        assert calls == []
        assert "rejected.configure" not in context.configuration_operations
        assert "rejected:provider" not in context.execution_runtime.provider_registry
        with pytest.raises(ValueError, match="module already registered"):
            context.register_module(successor)
        with pytest.raises(OSError, match="withdrawal failed"):
            context.unregister_module(candidate)

        # Registering the same object is not a cleanup retry. Retirement goes
        # through the exact retained owner and must finish before replacement.
        observer.reject_withdraw = lambda owner: False
        assert context.unregister_module(candidate)
        assert observer.visible == {}
        assert "rejected:provider:rejected:provider" not in context.port_registry
        context.register_module(successor)
        assert not context.unregister_module(candidate)
        assert context.module_registry.require("rejected") is successor
    finally:
        observer.reject_withdraw = lambda owner: False
        context.unregister_module(candidate)
        context.unregister_module(successor)
        core.close()
        context.execution_runtime.shutdown()


@pytest.mark.parametrize("phase", ["ports", "l3", "provider"])
def test_failed_registration_unwinds_only_acquired_provider_and_port_bindings(monkeypatch, phase):
    from pal.memory.service import DetachedL3Provider, MemoryService
    from tests.core_factory import build_context

    context = build_context()
    memory = MemoryService()
    context.port_registry["memory:memory"] = memory
    foreign = object()
    context.port_registry["foreign:kept"] = foreign
    context.execution_runtime.register_provider_ref("foreign:kept", foreign, owner_id="foreign")
    providers_before = dict(context.execution_runtime.provider_registry)
    provider = DetachedL3Provider(provider_id="candidate")
    first, second = object(), object()
    candidate = ModuleHandle("l3.candidate", "test", l3_provider=provider,
        provider_refs=["l3.candidate:first", "l3.candidate:second"],
        ports={"provider:l3.candidate:first": first, "provider:l3.candidate:second": second})
    if phase == "ports":
        publish = context.port_registry.publish
        def fail_ports(values):
            publish(values)
            raise OSError("registration interrupted")
        monkeypatch.setattr(context.port_registry, "publish", fail_ports)
    elif phase == "l3":
        register = memory.register_l3_provider
        def fail_l3(provider):
            register(provider)
            raise OSError("registration interrupted")
        monkeypatch.setattr(memory, "register_l3_provider", fail_l3)
    else:
        runtime = context.execution_runtime.implementation
        register = runtime.register_provider_ref
        def fail_second(name, value, *, owner_id):
            if name.endswith(":second"):
                raise OSError("registration interrupted")
            register(name, value, owner_id=owner_id)
        monkeypatch.setattr(runtime, "register_provider_ref", fail_second)
    try:
        with pytest.raises(OSError, match="registration interrupted"):
            context.register_module(candidate)
        assert context.module_registry.get("l3.candidate") is None
        assert memory.providers.get("candidate") is None
        assert context.execution_runtime.provider_registry == providers_before
        assert context.port_registry["foreign:kept"] is foreign
        assert not any(name.startswith("l3.candidate:") for name in context.port_registry)
    finally:
        context.execution_runtime.shutdown()
