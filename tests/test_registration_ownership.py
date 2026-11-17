"""Production ownership failures and the exact reducer relation checked by TLC."""
from itertools import product
from pathlib import Path

import pytest

from pal.core.capabilities import register_with_core
from pal.core.event_source_registry import EventSourceRegistry
from pal.core.module_registry import ModuleHandle, ModuleRegistry
from pal.core.registration_formal import registration_edges, ownership_edges, render_registration_relation
from pal.core.registration_state import (
    REGISTRATION_TRANSITIONS, RegistrationEvent, RegistrationPhase, RegistrationState, reduce_registration,
)
from pal.shared.registration import OWNERSHIP_TRANSITIONS, OwnerRelation, OwnershipEvent, owner_relation, reduce_ownership
from pal.plugins.host import PluginHost
from tests.core_factory import build_core
from tests.test_plugin_publication_rollback import MemoryPluginRepository
from tests.test_plugin_raii import _write_plugin


@pytest.fixture
def publication(tmp_path, monkeypatch):
    root = tmp_path / "builtin"
    root.mkdir()
    monkeypatch.syspath_prepend(str(root))
    core = build_core()
    ledger = []
    host = PluginHost(core.context, tmp_path, builtin_root=root,
                      services={"ledger": ledger}, third_party_repository=MemoryPluginRepository())
    yield core, host, root, ledger
    host.shutdown()
    assert not host.shutdown_errors
    core.close()
    core.context.execution_runtime.shutdown()


def plugin(root, name, *, module_id=None, source_id=None):
    _write_plugin(root, name)
    path = root / f"{name}_runtime.py"
    text = path.read_text().replace("        return handle", "        self.ledger.append(('scope', scope))\n        return handle")
    if module_id is not None:
        manifest = root / name / "plugin.toml"
        manifest.write_text(manifest.read_text().replace(f'module_id = "{name}"', f'module_id = "{module_id}"'))
        text = text.replace("ModuleHandle(module_id=self.plugin_id", f"ModuleHandle(module_id={module_id!r}")
    if source_id is not None:
        text = text.replace("        scope.context.register_module(handle)", f'''        class Source:
            source_id = {source_id!r}
            def prepare(self, context): return False
            def drain(self, context): return []
        handle.event_sources = [Source()]
        scope.context.register_module(handle)''')
    path.write_text(text)


def test_failed_module_claim_cannot_withdraw_resident_core(publication):
    core, host, root, ledger = publication
    original = register_with_core(core)
    names = core.publish_module_capabilities("core")
    generation = core.context.execution_runtime.registry_generation
    prompt = core.context.prompt_fragment_registry.providers["core.prompt.minimal_rules"]
    plugin(root, "collision", module_id="core")
    host.rescan()
    assert host.attach("collision")["status"] == "error"
    assert "module already registered: core" in host._record("collision").last_error
    assert core.context.module_registry.require("core") is original
    assert core.context.execution_runtime.registry_generation is generation
    assert original.mounted and original.mounted_subtree.admission.live
    assert original.published_capabilities == names
    assert core.context.prompt_fragment_registry.providers["core.prompt.minimal_rules"] is prompt
    assert ledger == []  # Reject before importing or starting the candidate.


def test_duplicate_source_rejects_candidate_and_preserves_first_owner(publication):
    core, host, root, ledger = publication
    for name in ("first", "second"):
        plugin(root, name, source_id="shared.source")
    host.rescan()
    assert host.attach("first")["status"] == "ok"
    original = core.context.event_source_registry.sources["shared.source"]
    assert host.attach("second")["status"] == "error"
    assert "event source already registered: shared.source" in host._record("second").last_error
    assert core.context.event_source_registry.sources["shared.source"] is original
    assert host._record("first").attached
    assert core.context.module_registry.get("second") is None
    assert host.generations["first"].scope.registration.phase is RegistrationPhase.ACTIVE


def test_retry_of_old_private_cleanup_cannot_remove_new_module_generation(publication):
    core, host, root, ledger = publication
    blocked = True
    def cleanup():
        if blocked:
            raise OSError("private resource still busy")
    ledger.append(cleanup)
    plugin(root, "old", module_id="shared", source_id="shared.source")
    source = root / "old_runtime.py"
    source.write_text(source.read_text().replace("    def start(self, scope):",
        "    def start(self, scope):\n        scope.defer(self.ledger[0])"))
    host.rescan()
    assert host.attach("old")["status"] == "ok"
    scope = host.generations["old"].scope
    assert host.detach("old")["status"] == "error"
    assert scope.registration.phase is RegistrationPhase.CLEANING
    plugin(root, "new", module_id="shared", source_id="shared.source")
    host.rescan()
    assert host.attach("new")["status"] == "ok"
    owner = host.generations["new"].handle
    event_source = core.context.event_source_registry.sources["shared.source"]
    blocked = False
    assert host.detach("old")["status"] == "ok"
    assert scope.registration.phase is RegistrationPhase.CLOSED
    assert core.context.module_registry.require("shared") is owner
    assert core.context.event_source_registry.sources["shared.source"] is event_source
    assert owner.mounted and owner.mounted_subtree.admission.live


def test_source_rejection_and_stale_withdrawal_use_exact_instance():
    class Source:
        source_id = "one"
        def prepare(self, _context): return False
        def drain(self, _context): return []
    registry = EventSourceRegistry()
    first, replacement = Source(), Source()
    registry.attach("module", first)
    for candidate in (first, replacement):
        with pytest.raises(ValueError, match="already registered"):
            registry.attach("module", candidate)
    assert registry.detach_module("module", expected_sources=(replacement,)) == []
    assert registry.sources["one"] is first
    assert registry.detach_module("module", expected_sources=(first,)) == ["one"]
    registry.attach("module", replacement)
    assert registry.detach_module("module", expected_sources=(first,)) == []
    assert registry.sources["one"] is replacement


@pytest.mark.parametrize("registry_kind", ["module", "source", "execution", "l3"])
@pytest.mark.parametrize("relation", list(OwnerRelation))
@pytest.mark.parametrize("event", list(OwnershipEvent))
def test_registry_effect_matches_generated_ownership_relation(registry_kind, relation, event, request):
    class Source:
        source_id = "one"
        def prepare(self, _context): return False
        def drain(self, _context): return []

    if registry_kind == "module":
        registry = ModuleRegistry()
        expected, other = ModuleHandle("one", "test"), ModuleHandle("one", "test")
        attach = registry.register
        claim = lambda: registry.register(expected)
        release = lambda: registry.unregister("one", expected=expected)
        lookup = lambda: registry.get("one")
    elif registry_kind == "source":
        registry = EventSourceRegistry()
        expected, other = Source(), Source()
        attach = lambda source: registry.attach("module", source)
        claim = lambda: registry.attach("module", expected)
        release = lambda: registry.detach_module("module", expected_sources=(expected,))
        lookup = lambda: registry.sources.get("one")
    elif registry_kind == "execution":
        from pal.execution.backend import build_execution_runtime
        registry = build_execution_runtime()
        request.addfinalizer(registry.shutdown)
        expected, other = object(), object()
        attach = lambda provider: registry.register_provider_ref("module:one", provider, owner_id="module")
        claim = lambda: attach(expected)
        release = lambda: registry.unregister_provider_ref("module:one", expected=expected)
        lookup = lambda: registry.provider_registry.get("module:one")
    else:
        from pal.memory.providers import L3PluginRegistry
        from pal.memory.service import DetachedL3Provider
        registry = L3PluginRegistry()
        expected, other = DetachedL3Provider(provider_id="one"), DetachedL3Provider(provider_id="one")
        attach = registry.register
        claim = lambda: registry.register(expected)
        release = lambda: registry.unregister(expected)
        lookup = lambda: registry.get("one")
    current = (None if relation is OwnerRelation.VACANT else
               expected if relation is OwnerRelation.SELF else other)
    if current is not None:
        attach(current)
    operation = claim if event is OwnershipEvent.CLAIM else release
    try:
        target = reduce_ownership(relation, event)
    except ValueError:
        with pytest.raises(ValueError):
            operation()
        assert lookup() is current
    else:
        operation()
        assert owner_relation(lookup(), expected) is target


def test_generated_tla_relation_is_the_complete_production_reducer():
    assert Path("spec/core/ModuleRegistrationReducer.tla").read_text() == render_registration_relation()
    assert set(registration_edges()) == {(p.value, own, event.value, target.value)
        for (p, own, event), target in REGISTRATION_TRANSITIONS.items()}
    assert set(ownership_edges()) == {(owner.value, event.value, target.value)
        for (owner, event), target in OWNERSHIP_TRANSITIONS.items()}
    for phase, own, event in product(RegistrationPhase, (False, True), RegistrationEvent):
        state = RegistrationState(phase)
        target = REGISTRATION_TRANSITIONS.get((phase, own, event))
        if target is None:
            with pytest.raises(ValueError):
                state.advance(event, owns_registration=own)
            assert state.phase is phase
        else:
            state.advance(event, owns_registration=own)
            assert state.phase is target
            assert reduce_registration(phase, event, owns_registration=own) is target
    for owner, event in product(OwnerRelation, OwnershipEvent):
        if (owner, event) in OWNERSHIP_TRANSITIONS:
            assert reduce_ownership(owner, event) is OWNERSHIP_TRANSITIONS[owner, event]
        else:
            with pytest.raises(ValueError):
                reduce_ownership(owner, event)
    token = object()
    assert owner_relation(None, token) is OwnerRelation.VACANT
    assert owner_relation(token, token) is OwnerRelation.SELF
    assert owner_relation(object(), token) is OwnerRelation.OTHER


def test_provider_namespace_is_validated_before_module_publication():
    from pal.memory.capabilities import register_with_core as register_memory
    from pal.memory.service import DetachedL3Provider, MemoryService
    core = build_core()
    memory = MemoryService()
    handle = register_memory(core.context, memory)
    try:
        turn_io = core.context.execution_runtime.turn_io
        candidate = ModuleHandle("foreign", "detachable", provider_refs=["core:turn_io"],
            ports={"provider:core:turn_io": turn_io})
        with pytest.raises(ValueError, match="does not belong"):
            core.context.register_module(candidate)
        assert core.context.execution_runtime.turn_io is turn_io
        assert core.context.module_registry.get("foreign") is None
        candidate = ModuleHandle("l3.foreign", "detachable",
            l3_provider=DetachedL3Provider(provider_id="different"))
        with pytest.raises(ValueError, match="does not belong"):
            core.context.register_module(candidate)
        assert memory.providers.get("different") is None
        assert core.context.module_registry.get("l3.foreign") is None
    finally:
        import asyncio
        asyncio.run(handle.quiesce_async())
        core.close()
        core.context.execution_runtime.shutdown()
