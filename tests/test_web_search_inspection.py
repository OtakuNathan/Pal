"""Pending batch: one parameterless snapshot covers every search backend."""
from unittest.mock import Mock

import pytest

from pal.core import PalCore
from pal.execution import register_with_core as register_execution
from pal.llm.repository import RuntimeSettingRepository
from pal.shared import IntrospectionCall
from pal.shared.tool_protocol import new_tool_call
from pal.web_search.capabilities import WebSearchIntrospectionProvider, register_with_core
from pal.web_search.models import WebSearchProviderModel
from pal.web_search.repository import WebSearchProviderRepository
from pal.web_search.service import WebSearchService
from tests.test_configuration_cli import runtime_root


@pytest.fixture
def service(runtime_root):
    return WebSearchService(WebSearchProviderRepository(), RuntimeSettingRepository())


def snapshot(service):
    return WebSearchIntrospectionProvider(service).show(IntrospectionCall(name="inspect_web_search"))


def test_snapshot_lists_all_including_disabled_without_network_calls(service, monkeypatch):
    for index in range(6):
        service.repository.upsert(provider_id=f"extra-{index}", provider_kind="duckduckgo_search", enabled=False)
    service.set_active_provider("brave")
    monkeypatch.setattr(service, "query", Mock(side_effect=AssertionError("inspection queried the web")))
    for backend in service.providers.values():
        monkeypatch.setattr(backend, "search", Mock(side_effect=AssertionError("inspection probed a backend")))
    result = snapshot(service)
    payload = result.structured
    assert payload["provider_count"] == len(payload["providers"]) == 8
    assert payload["enabled_provider_count"] == 2
    assert payload["configured_active_provider_id"] == "brave"
    assert payload["effective_active_provider_id"] == "brave"
    providers = {item["name"]: item for item in payload["providers"]}
    assert providers["extra-0"]["health"]["reason"] == "provider_disabled"
    assert providers["brave"]["auth"]["authorized"] is False
    assert "secret-keep" not in result.llm_text
    assert payload["health_basis"] == "configuration_and_last_request"
    service.query.assert_not_called()
    for backend in service.providers.values():
        backend.search.assert_not_called()


def test_empty_configuration_has_the_same_snapshot_shape(service):
    WebSearchProviderModel.delete().execute()
    payload = snapshot(service).structured
    assert payload["providers"] == []
    assert payload["provider_count"] == payload["enabled_provider_count"] == 0
    assert payload["configured_active_provider_id"] is None
    assert payload["effective_active_provider_id"] is None
    assert not payload["diagnostics"]["partial"]


def test_error_summaries_are_bounded_and_do_not_replace_resident_report(service):
    report = 'Traceback (most recent call last):\n  File "/owner/private.py", line 1\n    request()\nRuntimeError: quota exceeded token=hidden-secret\n' + 'response body ' * 500
    service.last_errors["brave"] = report
    result = snapshot(service)
    brave = next(item for item in result.structured["providers"] if item["name"] == "brave")
    assert len(brave["health"]["last_error"]) <= 600
    assert "quota exceeded" in brave["health"]["last_error"]
    assert "Traceback" not in result.llm_text
    assert "/owner/private.py" not in result.llm_text
    assert "hidden-secret" not in result.llm_text
    assert service.last_errors["brave"] == report


def test_one_unavailable_health_section_preserves_other_providers(service, monkeypatch):
    original = service.provider_health
    def health(provider):
        if provider.provider_id == "brave":
            raise RuntimeError("health source unavailable token=hidden-secret " + "x" * 2000)
        return original(provider)
    monkeypatch.setattr(service, "provider_health", health)
    result = snapshot(service)
    providers = {item["name"]: item for item in result.structured["providers"]}
    assert result.status == "ok"
    assert providers["brave"]["auth"]["authorized"] is False
    assert providers["brave"]["health"]["status"] == "unavailable"
    assert len(providers["brave"]["health"]["error"]) <= 600
    assert providers["ddg"]["health"]["healthy"] is True
    assert result.structured["diagnostics"]["partial"] is True
    assert "providers.brave.health" in result.structured["diagnostics"]["unavailable_sections"]
    assert "hidden-secret" not in result.llm_text
    assert "Traceback" not in result.llm_text


def test_tool_surface_is_two_entries_and_inspection_rejects_any_parameters(service, monkeypatch):
    core = PalCore()
    runtime = core.context.execution_runtime
    try:
        register_execution(core.context)
        handle = register_with_core(core.context, service)
        core.publish_module_capabilities("execution")
        core.publish_module_capabilities("web_search")
        generation = runtime.registry_generation
        names = {name for name, record in generation.search_records.items() if record["module_id"] == "web_search"}
        assert names == {"search_web", "inspect_web_search"}
        assert names.intersection(generation.direct_aliases) == {"search_web"}
        contract = generation.search_records["inspect_web_search"]["input_contract"]
        assert not contract.get("properties")
        assert not contract.get("required")
        assert contract["additionalProperties"] is False
        for alias in ("inspect_web_search_state", "list_web_search_providers", "inspect_web_search_provider"):
            assert generation.record_for_alias(alias) is None
            assert not runtime.execute_tool(new_tool_call(name="read_tool", args={"name": alias})).ok
            assert not runtime.execute_tool(new_tool_call(name="call_tool", args={"name": alias, "args": {}})).ok
        invoked = runtime.invoke_indirect_tool(new_tool_call(name="inspect_web_search", args={}))
        assert invoked.kind == "complete" and len(invoked.output["providers"]) == 2
        handler = Mock(side_effect=AssertionError("invalid arguments reached owner"))
        monkeypatch.setattr(handle.introspection_provider, "_provider_snapshot", handler)
        for args in ({"name": "brave"}, {"limit": 1}, {"view": "hub"}):
            rejected = runtime.invoke_indirect_tool(new_tool_call(name="inspect_web_search", args=args))
            assert rejected.kind == "rejected"
        handler.assert_not_called()
    finally:
        runtime.shutdown()
