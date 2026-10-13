"""Plugin owners declare search buckets independently of implementation modules."""
from types import SimpleNamespace

import pytest

from pal.core.prompt import MinimalOperatingRulesPromptFragmentProvider
from pal.core.prompt_compiler import PromptCompiler
from pal.core.prompt_fragment_registry import PromptFragmentRegistry
from pal.execution.tool_facade import EmptyToolInput, EmptyToolOutput, ToolGuidance
from pal.shared import PromptAssemblyContext
from pal.shared.tool_protocol import new_tool_call
from tests.capability_fixture import mount_test_capability
from tests.test_tool_failure_affordances import runtime


def mount(runtime, alias, *, domain, aliases=(), description="", module="execution", purpose="Read retained session output"):
    return mount_test_capability(runtime, alias=alias, canonical_path="op_test_" + alias,
        module_id=module, InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=lambda _: {},
        guidance=ToolGuidance(purpose=purpose, use_when="The requested operation matches", do_not_use_when="Other tasks",
            search_domain=domain, search_domain_aliases=aliases, search_domain_description=description))


def search(runtime, domain, query):
    return runtime.execute_tool(new_tool_call(name="search_tools", args={"domain": domain, "query": query}))


def test_shell_session_owner_overrides_execution_module(runtime):
    mount(runtime, "read_shell_session", domain="shell", aliases=("terminal",))
    for domain in ("shell", "terminal"):
        result = search(runtime, domain, "read retained session output")
        assert result.ok
        assert result.structured["hits"][0]["alias"] == "read_shell_session"
        assert result.structured["hits"][0]["domain"] == "shell"
        assert "search_domain_aliases" not in result.llm_text
    result = search(runtime, "execution", "read_shell_session")
    assert not result.structured["hits"]


def test_camera_alias_keeps_eye_in_its_own_bucket(runtime):
    mount(runtime, "eye_observe", domain="eye", aliases=("camera",), module="eye", purpose="Capture physical surroundings")
    mount(runtime, "read_image", domain="vision", module="image_provider", purpose="Inspect an existing image")
    result = search(runtime, "camera", "capture physical surroundings")
    assert [hit["alias"] for hit in result.structured["hits"]] == ["eye_observe"]
    assert not search(runtime, "vision", "eye_observe").structured["hits"]


def test_full_name_alias_accepts_separators_and_case(runtime):
    mount(runtime, "eye_observe", domain="eye", aliases=("physical camera",),
          module="eye", purpose="Capture physical surroundings")
    for domain in ("physical camera", "physical_camera", "Physical-Camera"):
        result = search(runtime, domain, "capture physical surroundings")
        assert result.structured["applied_filters"]["domain"] == "eye"
        assert [hit["alias"] for hit in result.structured["hits"]] == ["eye_observe"]


@pytest.mark.parametrize("aliases", [("shell",), ("camera",)])
def test_conflicting_domain_alias_rejects_generation_and_preserves_previous(runtime, aliases):
    mount(runtime, "eye_observe", domain="eye", aliases=("camera",), module="eye")
    previous = runtime.registry_generation
    with pytest.raises(ValueError, match="conflicts with owner"):
        mount(runtime, "show_emotion", domain="emotion", aliases=aliases, module="avatar")
    assert runtime.registry_generation is previous
    assert search(runtime, "camera", "eye_observe").structured["hits"][0]["alias"] == "eye_observe"


def test_registry_domain_metadata_changes_hash_and_roundtrips(runtime):
    data = ToolGuidance(purpose="Capture a photo", use_when="Physical input", do_not_use_when="Existing files",
        search_domain="eye", search_domain_aliases=("camera",), search_domain_description="Physical camera input.")
    assert ToolGuidance.model_validate_json(data.model_dump_json()) == data
    mount(runtime, "eye_observe", domain="eye", aliases=("camera",), description="Physical camera input.")
    first = runtime.registry_generation.generation_hash
    mount(runtime, "eye_observe", domain="eye", aliases=("physical camera",), description="Physical camera input.")
    assert runtime.registry_generation.generation_hash != first


def test_tool_policy_includes_current_declared_domains_and_updates_after_unmount(runtime):
    from tests.capability_fixture import unmount_test_capability
    handle = mount(runtime, "eye_observe", domain="eye", aliases=("camera",), description="Physical camera input.")
    registry = PromptFragmentRegistry()
    registry.register(MinimalOperatingRulesPromptFragmentProvider())
    compiler = PromptCompiler(SimpleNamespace(prompt_fragment_registry=registry, execution_runtime=runtime))
    def policy():
        return next(f.content for f in compiler.collect_prompt_fragments(PromptAssemblyContext()) if f.section == "tool_policy")
    assert "eye (also: camera): Physical camera input." in policy()
    unmount_test_capability(runtime, handle)
    assert "eye (also: camera)" not in policy()


@pytest.mark.parametrize("patch", [{"search_domain": "Eye"}, {"search_domain_aliases": ("camera",)},
                                  {"search_domain": "eye", "search_domain_aliases": ("physical camera", "physical_camera")},
                                  {"search_domain": "eye", "search_domain_aliases": ("bad alias!",)}])
def test_invalid_domain_metadata_is_rejected(patch):
    with pytest.raises(ValueError):
        ToolGuidance(purpose="Inspect", use_when="Requested", do_not_use_when="Other tasks", **patch)
