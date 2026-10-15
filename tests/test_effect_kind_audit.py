from __future__ import annotations

from pal.artifact.capabilities import ArtifactIntrospectionProvider
from pal.execution.tool_facade import EffectKind
from pal.memory.capabilities import MemoryIntrospectionProvider
from pal.plugins.l3.sqlite_vec import SQLiteVecL3Plugin
from pal.plugins.l3.stubs import _L3ProviderCapabilityMixin


def _declared_effect(provider_type: type, method_name: str) -> EffectKind:
    method = getattr(provider_type, method_name)
    blueprints = tuple(getattr(method, "__capability_action_blueprints__", ()))
    assert len(blueprints) == 1
    execution = blueprints[0].execution
    assert execution is not None
    return execution.effect_kind


def test_artifact_effects_track_hot_state_and_content_mutations() -> None:
    expected = {
        "import_file": EffectKind.LOCAL_WRITE,
        "info": EffectKind.LOCAL_WRITE,
        "search": EffectKind.LOCAL_READ,
    }

    assert {
        method_name: _declared_effect(ArtifactIntrospectionProvider, method_name)
        for method_name in expected
    } == expected


def test_stateful_query_tools_are_not_declared_as_reads() -> None:
    assert _declared_effect(MemoryIntrospectionProvider, "recall") is EffectKind.LOCAL_WRITE
    assert not getattr(SQLiteVecL3Plugin.recall_query, "__capability_action_blueprints__", ())
    assert not getattr(_L3ProviderCapabilityMixin.recall_query, "__capability_action_blueprints__", ())
