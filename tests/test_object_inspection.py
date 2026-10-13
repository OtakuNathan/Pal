"""Object inspections preserve diagnosis, boundaries and owner-only maintenance."""
import asyncio
from types import SimpleNamespace

import pytest

from pal.control.configuration import CoreConfigurationService, configuration_endpoint
from pal.control.service import ControlPlane
from pal.foundation.persistence import PalV2Database
from pal.foundation.sidecar import SidecarRpcClient
from pal.identity.cli import run_identity_cli_async
from pal.identity.models import PalPersonaModel, UserPreferencesModel
from pal.identity.prompt import IdentityPromptFragmentProvider
from pal.identity.repository import IdentityRepository
from pal.identity.service import IdentityService
from pal.main import _build_parser
from pal.packages.process import runtime_lease
from pal.shared import IntrospectionCall, IntrospectionResult, RuntimeStatus
from pal.shared.inspection import inspection_snapshot
from pal.web_search.capabilities import WebSearchIntrospectionProvider
from pal.web_search.repository import WebSearchProviderRepository
from pal.web_search.service import WebSearchService
from pal.llm.repository import RuntimeSettingRepository
from tests.test_configuration_cli import runtime_root
from tests.test_channel_proactive_error_fidelity import channel_env, invoke


def test_channel_inspection_combines_one_object_and_preserves_partial_failure(channel_env, monkeypatch):
    core, channel, _ = channel_env
    channel.publish_endpoint_when_ready("channel-main")
    core.publish_module_capabilities("channel")
    provider = core.context.module_registry.require("channel").introspection_provider
    manager = provider.provider_manager
    calls = []
    def reader(name, payload):
        def read(endpoint_id):
            calls.append((name, endpoint_id))
            return IntrospectionResult(status=RuntimeStatus.OK, text=name, llm_text=name, structured=payload)
        return read
    monkeypatch.setattr(manager, "inspect_auth_state", reader("auth", {"authorized": True}))
    monkeypatch.setattr(manager, "inspect_backlog", reader("backlog", {"inbox_size": 2, "outbox_size": 3}))
    def broken(endpoint_id):
        calls.append(("health", endpoint_id))
        raise RuntimeError("health source unavailable")
    monkeypatch.setattr(manager, "inspect_health", broken)
    result = invoke(core.context.execution_runtime, "inspect_channel_endpoint", {"name": "channel-main"})
    assert result.ok, result.llm_text
    assert result.invocation_result.output["auth"]["authorized"] is True
    assert result.invocation_result.output["backlog"]["outbox_size"] == 3
    assert result.invocation_result.output["health"]["status"] == "unavailable"
    assert result.invocation_result.output["diagnostics"]["partial"] is True
    assert calls == [("auth", "channel-main"), ("health", "channel-main"), ("backlog", "channel-main")]
    hub = invoke(core.context.execution_runtime, "inspect_channel_state", {})
    assert hub.ok and "auth" not in hub.invocation_result.output and "backlog" not in hub.invocation_result.output
    assert len(calls) == 3


def test_web_inspection_preserves_auth_request_failure_and_hub_boundary(runtime_root):
    service = WebSearchService(WebSearchProviderRepository(), RuntimeSettingRepository())
    service.last_errors["brave"] = "last request transport failure"
    provider = WebSearchIntrospectionProvider(service)
    result = provider.show_provider(IntrospectionCall(name="inspect_web_search_provider", args={"target_id": "brave"}))
    assert result.structured["auth"]["authorized"] is False
    assert result.structured["health"]["last_error"] == "last request transport failure"
    assert "secret-keep" not in result.llm_text
    hub = provider.show(IntrospectionCall(name="inspect_state"))
    assert hub.structured["provider_count"] == 2
    assert "auth" not in hub.structured and "items" not in hub.structured


def test_plugin_directory_and_hub_do_not_expand_plugin_state(tmp_path, monkeypatch):
    from pal.plugins.host import PluginHost
    from pal.plugins.contracts import PluginRecord, PLUGIN_SOURCE_FIRST_PARTY
    from pal.packages.service import PackageService
    from pal.core import PalCore
    core = PalCore()
    host = PluginHost(core.context, tmp_path, third_party_repository=SimpleNamespace(list_all=lambda: [], get=lambda name: None))
    host.first_party_records["example"] = PluginRecord(
        plugin_id="example", source=PLUGIN_SOURCE_FIRST_PARTY, entrypoint="example:build",
        version="1", filesystem_path=str(tmp_path), enabled=True, attached=False, last_load_status="detached",
        config={"private_token": "secret-value"})
    package_calls = []
    def status(_self, *, name, kind):
        package_calls.append((name, kind))
        return {"items": [{"name": name, "status": "prepared"}]}
    monkeypatch.setattr(PackageService, "status", status)
    assert host.show_summary()["first_party_count"] == 1
    item, = host.list_plugin_summaries()
    assert "config" not in item and "installation" not in item
    assert package_calls == []
    result = host.inspect_plugin("example")
    assert result["installation"]["items"][0]["status"] == "prepared"
    assert result["config_keys"] == ["private_token"]
    assert "secret-value" not in str(result)
    assert package_calls == [("example", "builtin")]
    assert host.inspect_plugin("absent") is None


def test_unavailable_and_unhealthy_are_distinct():
    result = inspection_snapshot({"mounted": True}, sections={
        "health": lambda: {"healthy": False, "reason": "last_request_failed"},
        "inventory": lambda: {"inspection_status": "error", "reason": "database_unavailable"},
    })
    assert result["health"]["healthy"] is False
    assert result["diagnostics"]["unavailable_sections"] == {"inventory": "database_unavailable"}
    assert result["diagnostics"]["partial"] is True


def test_llm_inspection_distinguishes_configured_selection_from_effective_primary():
    from pal.llm.capabilities import LLMIntrospectionProvider
    from pal.llm.runtime import EndpointResolver, LLMRuntime
    from tests.test_llm_usage import _Settings, _endpoint
    runtime = LLMRuntime(endpoint_resolver=EndpointResolver(endpoints=(_endpoint("available"),)),
                         settings_repository=_Settings(active_endpoint_id="missing"))
    provider = LLMIntrospectionProvider(runtime)
    hub = provider.active(IntrospectionCall(name="inspect_active_llm"))
    assert hub.structured["configured_active_endpoint_id"] == "missing"
    assert hub.structured["effective_primary_endpoint_id"] is None
    assert hub.structured["has_active_model"] is False
    endpoint = provider.show_model(IntrospectionCall(name="inspect_llm_endpoint", args={"name": "available"}))
    assert endpoint.structured["active"] is False
    assert endpoint.structured["configured_active"] is False
    assert endpoint.structured["usage"]["status"] == "not_recorded"


def test_lsp_inspection_does_not_start_manager_or_expand_other_workspace(tmp_path):
    from pal.lsp.plugin import LspManagerPluginProvider
    provider = LspManagerPluginProvider(runtime_root=tmp_path)
    provider.client = SimpleNamespace(
        operation_sync=lambda method, params: {"status": "ok", "workspace": {"workspace_root": params["workspace_root"]}})
    provider._ensure_manager_started = lambda: pytest.fail("inspection started manager")
    provider.last_health = {"server_count": 2, "servers": [{"secret_workspace": "/other"}]}
    hub = provider.show(IntrospectionCall(name="inspect_state"))
    assert "servers" not in hub.structured["cached_snapshot"]
    result = provider.status(IntrospectionCall(name="inspect_lsp_status", args={"workspace_root": str(tmp_path)}))
    assert result.status == RuntimeStatus.OK
    assert "secret_workspace" not in result.llm_text


def test_lsp_workspace_status_does_not_read_unrelated_server_details(tmp_path):
    from pal.lsp.manager import LspManager
    manager = LspManager(tmp_path)
    manager.health = lambda: {"server_count": 2}
    manager.states = {"unrelated": SimpleNamespace(server_id="unrelated")}
    manager._server_summary = lambda state: pytest.fail("unrelated server expanded")
    manager._workspace_environment = lambda root: {
        "readiness": {"status": "ok", "servers": [{"server_id": "selected"}]},
    }
    payload = manager.status({"workspace_root": str(tmp_path)})
    assert payload["servers"] == [{"server_id": "selected"}]
    assert payload["workspace"]["workspace_root"] == str(tmp_path)


def test_identity_cli_refreshes_projection_and_reports_startup_pins(tmp_path, capsys):
    database = PalV2Database(tmp_path / "pal.sqlite3")
    database.initialize((PalPersonaModel, UserPreferencesModel))
    service = IdentityService(IdentityRepository())
    service.ensure_defaults()
    prompt = IdentityPromptFragmentProvider(service)
    original_system = prompt._system_identity_content
    PalPersonaModel.update(language="new language", tone="new tone").execute()
    assert service.get_persona().language != "new language"
    async def run():
        owner = CoreConfigurationService(tmp_path, SimpleNamespace(), identity_service=service, control_plane=ControlPlane())
        with runtime_lease(tmp_path):
            await owner.start_async()
            try:
                # Same owner protocol as the subprocess CLI; no conversation turn.
                shown = await SidecarRpcClient(configuration_endpoint(tmp_path)).request("identity.inspect", {})
                assert shown["state"]["persona"]["language"] != "new language"
                args = _build_parser().parse_args(["identity", "refresh", "--runtime-root", str(tmp_path), "--json"])
                assert await run_identity_cli_async(args) == 0
                assert service.get_persona().language == "new language"
                assert service.get_persona().tone == "new tone"
                assert prompt._system_identity_content == original_system
                control = await SidecarRpcClient(configuration_endpoint(tmp_path)).request("control.inspect", {})
                assert control["state"]["command_count"] == len(owner.control_plane.registry.commands)
                with pytest.raises(Exception):
                    await SidecarRpcClient(configuration_endpoint(tmp_path)).request("identity.refresh", {"name": "other"})
            finally:
                await owner.stop_async()
    try:
        asyncio.run(run())
        output = capsys.readouterr().out
        assert '"activation": "live"' in output
        assert '"startup_pinned_fields"' in output
        args = _build_parser().parse_args(["identity", "show", "--runtime-root", str(tmp_path), "--json"])
        assert asyncio.run(run_identity_cli_async(args)) == 0
        args.identity_command = "refresh"
        assert asyncio.run(run_identity_cli_async(args)) == 2
    finally:
        database.close()


def test_identity_live_owner_failure_does_not_claim_refresh(tmp_path, capsys):
    database = PalV2Database(tmp_path / "pal.sqlite3")
    database.initialize((PalPersonaModel, UserPreferencesModel))
    try:
        with runtime_lease(tmp_path):
            args = _build_parser().parse_args(["identity", "refresh", "--runtime-root", str(tmp_path), "--timeout-seconds", ".1", "--json"])
            assert asyncio.run(run_identity_cli_async(args)) == 2
            assert '"outcome": "unknown"' in capsys.readouterr().out
    finally:
        database.close()
