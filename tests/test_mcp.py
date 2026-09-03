from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pal.behavior import BehaviorAffordanceModel, BehaviorRepository, BehaviorService, BehaviorSkillModel, register_with_core as register_behavior_with_core
from pal.core import PalCore, register_with_core as register_core_with_core
from pal.execution import CapabilityCall, register_with_core as register_execution_with_core
from pal.execution.tool_facade import EffectKind, Idempotency, RetryPolicy
from pal.foundation import PalV2Database, SidecarEndpoint, SidecarRpcClient, SidecarRpcError
from pal.mcp import AsyncStdioMcpConnector, McpCompiler, McpDiscoverySnapshot, McpProtocolError, McpServerConfig, load_mcp_server_file
from pal.mcp.connector import _expand_env_vars
from pal.mcp.ipc import McpManagerClient
from pal.mcp.manager import McpManager
from pal.mcp.model import McpPromptArgumentSpec, McpPromptSpec, McpToolSpec
from pal.mcp.normalize import normalize_tool_payload
from pal.mcp.plugin import McpManagerPluginProvider, build_mcp_plugin
from pal.plugins.host import PluginHost
from pal.plugins.models import PluginBundleModel
from pal.shared import RuntimeStatus
from pal.skill import SkillRepository, SkillSearchTool, SkillService, register_with_core as register_skill_with_core


class FakeInvoker:
    def __init__(self, *, tool_result=None, prompt_result=None, call_error: Exception | None = None) -> None:
        self.tool_result = tool_result or {"content": [{"type": "text", "text": "tool ok"}]}
        self.prompt_result = prompt_result or {"messages": [{"role": "user", "content": {"type": "text", "text": "rendered"}}]}
        self.call_error = call_error
        self.tool_calls = []
        self.prompt_calls = []

    def call_tool(self, server_id, tool_name, arguments):
        if self.call_error is not None:
            raise self.call_error
        self.tool_calls.append((server_id, tool_name, dict(arguments)))
        return dict(self.tool_result)

    def render_prompt(self, server_id, prompt_name, arguments):
        self.prompt_calls.append((server_id, prompt_name, dict(arguments)))
        return dict(self.prompt_result)


class McpCompilerTests(unittest.TestCase):
    def test_tool_compiles_to_capability_and_invokes_manager_invoker(self) -> None:
        invoker = FakeInvoker()
        snapshot = McpDiscoverySnapshot(
            server_id="demo-server",
            transport="stdio",
            tools=(
                McpToolSpec(
                    name="read.file",
                    description="Read a file",
                    input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                ),
            ),
        ).with_hash()

        projection = McpCompiler().compile(module_id="mcp", snapshots=(snapshot,), invoker=invoker)

        descriptor = projection.mounted_subtree.descriptors[0]
        self.assertEqual(descriptor.canonical_path, "op_mcp_demo_server_tool_read_file")
        self.assertEqual(descriptor.module_id, "mcp")
        result = projection.mounted_subtree.bound_actions[0].callable(CapabilityCall(name=descriptor.canonical_path, args={"path": "a.txt"}))
        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertEqual(invoker.tool_calls, [("demo-server", "read.file", {"path": "a.txt"})])

    def test_invalid_tool_schema_is_rejected_without_executable_capability(self) -> None:
        snapshot = McpDiscoverySnapshot(
            server_id="demo",
            transport="stdio",
            tools=(McpToolSpec(name="bad", input_schema={"type": "string"}),),
        ).with_hash()

        projection = McpCompiler().compile(module_id="mcp", snapshots=(snapshot,), invoker=FakeInvoker())

        self.assertEqual(projection.mounted_subtree.descriptors, [])
        self.assertEqual(projection.snapshots[0].rejected_items[0].reason, "non_object_input_schema")

    def test_invalid_raw_tool_schema_is_not_silently_treated_as_missing(self) -> None:
        tool = normalize_tool_payload({"name": "bad.raw", "inputSchema": "not-a-schema"})
        snapshot = McpDiscoverySnapshot(server_id="demo", transport="stdio", tools=(tool,)).with_hash()

        projection = McpCompiler().compile(module_id="mcp", snapshots=(snapshot,), invoker=FakeInvoker())

        self.assertEqual(projection.mounted_subtree.descriptors, [])
        self.assertEqual(projection.snapshots[0].rejected_items[0].reason, "invalid_input_schema")

    def test_tool_error_kinds_are_preserved(self) -> None:
        tool = McpToolSpec(name="run", input_schema={"type": "object", "properties": {}})
        snapshot = McpDiscoverySnapshot(server_id="demo", transport="stdio", tools=(tool,)).with_hash()
        projection = McpCompiler().compile(
            module_id="mcp",
            snapshots=(snapshot,),
            invoker=FakeInvoker(tool_result={"isError": True, "content": [{"type": "text", "text": "failed"}]}),
        )
        result = projection.mounted_subtree.bound_actions[0].callable(CapabilityCall(name="op_mcp_demo_tool_run", args={}))
        self.assertEqual(result.status, RuntimeStatus.ERROR)
        self.assertEqual(result.structured["error_kind"], "tool_execution")
        self.assertEqual(result.structured["tool_text"], "failed")
        self.assertEqual(result.text, "failed")
        self.assertIn("MCP tool execution failed:\nfailed", result.llm_text)

        projection = McpCompiler().compile(
            module_id="mcp",
            snapshots=(snapshot,),
            invoker=FakeInvoker(call_error=McpProtocolError("closed")),
        )
        result = projection.mounted_subtree.bound_actions[0].callable(CapabilityCall(name="op_mcp_demo_tool_run", args={}))
        self.assertEqual(result.status, RuntimeStatus.ERROR)
        self.assertEqual(result.structured["error_kind"], "protocol")
        self.assertIn("closed", result.text)
        self.assertIn("closed", result.llm_text)

    def test_prompt_compiles_to_declared_skill_and_render_capability(self) -> None:
        prompt = McpPromptSpec(
            name="code.review",
            description="Review code",
            arguments=(McpPromptArgumentSpec(name="diff", description="Patch diff", required=True),),
        )
        snapshot = McpDiscoverySnapshot(server_id="demo", transport="stdio", prompts=(prompt,)).with_hash()
        invoker = FakeInvoker(prompt_result={"messages": [{"role": "user", "content": {"type": "image", "data": "..."}}]})

        projection = McpCompiler().compile(module_id="mcp", snapshots=(snapshot,), invoker=invoker)

        descriptor = projection.mounted_subtree.descriptors[0]
        self.assertEqual(descriptor.canonical_path, "op_mcp_demo_prompt_code_review_render")
        self.assertEqual(projection.skills[0].skill_id, "mcp_demo_prompt_code_review")
        result = projection.mounted_subtree.bound_actions[0].callable(CapabilityCall(name=descriptor.canonical_path, args={"diff": "patch"}))
        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertEqual(result.structured["unsupported_content_types"], ["image"])
        self.assertEqual(invoker.prompt_calls, [("demo", "code.review", {"diff": "patch"})])


class McpConnectorAndManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal_mcp_manager_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_default_mcp_request_timeouts_are_long_running_friendly(self) -> None:
        config_path = self.root / "server.toml"
        config_path.write_text(
            "\n".join(
                [
                    'server_id = "demo"',
                    f"command = '{sys.executable}'",
                    "args = []",
                ]
            ),
            encoding="utf-8",
        )

        (file_config,) = load_mcp_server_file(config_path)

        self.assertEqual(McpServerConfig(server_id="demo", command=("cmd",)).request_timeout_ms, 300_000)
        self.assertEqual(file_config.config.request_timeout_ms, 300_000)
        self.assertEqual(McpManagerClient(runtime_root=self.root).request_timeout_seconds, 300.0)

    def test_mcp_command_env_var_expansion_preserves_unknown_vars(self) -> None:
        expanded = _expand_env_vars(
            "$PAL_MCP_BIN --root=${PAL_MCP_HOME} --literal=$PAL_MCP_MISSING",
            {"PAL_MCP_BIN": "/opt/mcp/server", "PAL_MCP_HOME": "/srv/mcp"},
        )

        self.assertEqual(expanded, "/opt/mcp/server --root=/srv/mcp --literal=$PAL_MCP_MISSING")

    def test_mcp_only_expands_executable_and_preserves_argument_placeholders(self) -> None:
        captured: dict[str, object] = {}

        async def fake_create_subprocess_exec(*args, **kwargs):
            captured["args"] = args
            captured["env"] = kwargs.get("env")
            raise RuntimeError("stop before spawning")

        async def scenario() -> None:
            connector = AsyncStdioMcpConnector(
                McpServerConfig(
                    server_id="env-command",
                    command=("$PAL_MCP_BIN", "--root=${PAL_MCP_HOME}", "--literal=$PAL_MCP_MISSING"),
                )
            )
            with patch.dict(os.environ, {"PAL_MCP_BIN": "/opt/mcp/server", "PAL_MCP_HOME": "/srv/mcp"}):
                with patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec):
                    with self.assertRaisesRegex(RuntimeError, "stop before spawning"):
                        await connector._start()

        asyncio.run(scenario())

        self.assertEqual(
            captured["args"],
            ("/opt/mcp/server", "--root=${PAL_MCP_HOME}", "--literal=$PAL_MCP_MISSING"),
        )
        self.assertIsNone(captured["env"])

    def test_mcp_secret_header_stays_out_of_argv(self) -> None:
        captured: dict[str, object] = {}

        async def fake_create_subprocess_exec(*args, **kwargs):
            captured["args"] = args
            captured["env"] = kwargs.get("env")
            raise RuntimeError("stop before spawning")

        async def scenario() -> None:
            connector = AsyncStdioMcpConnector(
                McpServerConfig(
                    server_id="secret-header",
                    command=("/opt/mcp-remote", "--header", "Authorization:${MCP_AUTH}"),
                    env={"MCP_AUTH": "Bearer secret-value"},
                )
            )
            with patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec):
                with self.assertRaisesRegex(RuntimeError, "stop before spawning"):
                    await connector._start()

        asyncio.run(scenario())

        self.assertEqual(
            captured["args"],
            ("/opt/mcp-remote", "--header", "Authorization:${MCP_AUTH}"),
        )
        self.assertEqual(captured["env"]["MCP_AUTH"], "Bearer secret-value")

    def test_mcp_rescan_restricts_config_files_containing_environment_secrets(self) -> None:
        config_root = self.root / "plugins" / "mcp"
        config_root.mkdir(parents=True)
        config_path = config_root / "private.toml"
        config_path.write_text(
            "\n".join(
                [
                    'server_id = "private"',
                    'command = "/opt/private-mcp"',
                    "enabled = false",
                    "[env]",
                    'PRIVATE_TOKEN = "secret-value"',
                ]
            ),
            encoding="utf-8",
        )
        config_path.chmod(0o664)

        asyncio.run(McpManager(runtime_root=self.root).rescan())

        self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)

    def test_sidecar_rpc_timeout_covers_connection_phase(self) -> None:
        async def slow_open_connection(endpoint):
            _ = endpoint
            await asyncio.sleep(1.0)

        async def run() -> None:
            client = SidecarRpcClient(
                endpoint=SidecarEndpoint(runtime_root=self.root, name="mcp-timeout-test"),
                request_timeout_seconds=0.01,
            )
            with patch("pal.foundation.sidecar.open_sidecar_connection", side_effect=slow_open_connection):
                with self.assertRaises(SidecarRpcError) as raised:
                    await client.request("health")
            self.assertEqual(raised.exception.kind, "timeout")
            self.assertEqual(raised.exception.payload["method"], "health")

        asyncio.run(run())

    def _write_fake_server(self) -> Path:
        server = self.root / "fake_mcp_server.py"
        server.write_text(
            textwrap.dedent(
                """
                import json
                import sys

                initialized = False
                for line in sys.stdin:
                    payload = json.loads(line)
                    method = payload.get("method")
                    if method == "notifications/initialized":
                        initialized = True
                        continue
                    ident = payload.get("id")
                    params = payload.get("params") or {}
                    if method == "initialize":
                        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}, "prompts": {}}, "serverInfo": {"name": "fake"}}
                    elif not initialized:
                        result = {"error": "not initialized"}
                    elif method == "tools/list":
                        if params.get("cursor") == "next-tools":
                            result = {"tools": [{"name": "beta", "inputSchema": {"type": "object", "properties": {}}}]}
                        else:
                            result = {"tools": [{"name": "alpha", "inputSchema": {"type": "object", "properties": {}}}], "nextCursor": "next-tools"}
                    elif method == "tools/call":
                        result = {"content": [{"type": "text", "text": "called " + params.get("name", "")}]}
                    elif method == "prompts/list":
                        if params.get("cursor") == "next-prompts":
                            result = {"prompts": [{"name": "brief.two"}]}
                        else:
                            result = {"prompts": [{"name": "brief.one"}], "nextCursor": "next-prompts"}
                    elif method == "prompts/get":
                        result = {"messages": [{"role": "user", "content": {"type": "text", "text": "rendered " + params.get("name", "")}}]}
                    else:
                        result = {}
                    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}) + "\\n")
                    sys.stdout.flush()
                """
            ),
            encoding="utf-8",
        )
        return server

    def test_async_stdio_connector_initializes_and_paginates(self) -> None:
        async def scenario() -> None:
            server = self._write_fake_server()
            connector = AsyncStdioMcpConnector(
                McpServerConfig(server_id="stdio-test", command=(sys.executable, str(server)), startup_timeout_ms=2_000, request_timeout_ms=2_000)
            )
            try:
                await connector.initialize()
                self.assertEqual([item.name for item in await connector.list_tools_all()], ["alpha", "beta"])
                self.assertEqual([item.name for item in await connector.list_prompts_all()], ["brief.one", "brief.two"])
            finally:
                await connector.close()
                await connector.close()

        asyncio.run(scenario())

    def test_manager_rpc_scans_lists_reads_and_calls(self) -> None:
        async def scenario() -> None:
            server = self._write_fake_server()
            config_root = self.root / "plugins" / "mcp"
            config_root.mkdir(parents=True)
            (config_root / "demo.toml").write_text(
                "\n".join(
                    [
                        'server_id = "demo"',
                        f"command = '{sys.executable}'",
                        f"args = ['{server.as_posix()}']",
                        "request_timeout_ms = 2000",
                    ]
                ),
                encoding="utf-8",
            )
            manager = McpManager(runtime_root=self.root)
            task = asyncio.create_task(manager.run())
            client = McpManagerClient(runtime_root=self.root)
            try:
                for _ in range(60):
                    try:
                        health = await client.health()
                        if health.get("ok"):
                            break
                    except Exception:
                        await asyncio.sleep(0.05)
                servers = {}
                for _ in range(60):
                    servers = await client.list_servers()
                    if servers["items"] and servers["items"][0]["tool_count"] == 2:
                        break
                    await asyncio.sleep(0.05)
                self.assertEqual(servers["items"][0]["server_id"], "demo")
                self.assertEqual(servers["items"][0]["tool_count"], 2)
                read = await client.read_server("demo")
                self.assertEqual(read["remote_type"], "stdio")
                snapshot = await client.snapshot()
                self.assertEqual(snapshot["server_count"], 1)
                result = await client.call_tool("demo", "alpha", {})
                self.assertIn("called alpha", str(result))
            finally:
                try:
                    await client.shutdown()
                except Exception:
                    pass
                await asyncio.wait_for(task, timeout=5)

        asyncio.run(scenario())


class McpPluginSidecarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal_mcp_plugin_test_"))
        self.database = PalV2Database(self.root / "pal_mcp.sqlite3")
        self.database.initialize([BehaviorAffordanceModel, BehaviorSkillModel, PluginBundleModel])

    def tearDown(self) -> None:
        self.database.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _core_with_services(self):
        core = PalCore()
        core.context.execution_runtime.runtime_root = self.root
        register_core_with_core(core)
        register_execution_with_core(core.context)
        skill_repository = SkillRepository()
        skill_service = SkillService(repository=skill_repository, runtime_root=self.root)
        register_skill_with_core(core.context, skill_service)
        behavior_service = BehaviorService(repository=BehaviorRepository(skill_repository=skill_repository))
        register_behavior_with_core(core.context, behavior_service)
        core.publish_module_capabilities("execution")
        core.publish_module_capabilities("skill")
        core.publish_module_capabilities("behavior")
        return core, skill_service

    def _write_config_and_server(self) -> None:
        server = self.root / "fake_mcp_server.py"
        server.write_text(
            textwrap.dedent(
                """
                import json
                import sys
                initialized = False
                for line in sys.stdin:
                    payload = json.loads(line)
                    method = payload.get("method")
                    if method == "notifications/initialized":
                        initialized = True
                        continue
                    ident = payload.get("id")
                    if method == "initialize":
                        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}, "prompts": {}}, "serverInfo": {"name": "fake"}}
                    elif not initialized:
                        result = {"error": "not initialized"}
                    elif method == "tools/list":
                        result = {"tools": [{"name": "alpha", "inputSchema": {"type": "object", "properties": {}}}]}
                    elif method == "prompts/list":
                        result = {"prompts": [{"name": "brief"}]}
                    elif method == "tools/call":
                        result = {"content": [{"type": "text", "text": "tool ok"}]}
                    else:
                        result = {}
                    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}) + "\\n")
                    sys.stdout.flush()
                """
            ),
            encoding="utf-8",
        )
        config_root = self.root / "plugins" / "mcp"
        config_root.mkdir(parents=True)
        (config_root / "demo.toml").write_text(
            "\n".join(
                [
                    'server_id = "demo"',
                    f"command = '{sys.executable}'",
                    f"args = ['{server.as_posix()}']",
                    "request_timeout_ms = 2000",
                ]
            ),
            encoding="utf-8",
        )

    def test_attach_failure_is_reported_as_failure_not_applied_success(self) -> None:
        refreshed: list[bool] = []
        provider = McpManagerPluginProvider(
            runtime_root=self.root,
            core_context=object(),
            refresh_capabilities=lambda: refreshed.append(provider.projection is None),
        )

        def fail_startup() -> None:
            raise RuntimeError("sidecar unavailable")

        provider._ensure_manager_started = fail_startup  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "sidecar unavailable"):
            provider.start_manager()

        self.assertIn("sidecar unavailable", provider.last_error)
        self.assertEqual(refreshed, [True])

    def test_attach_success_republishes_the_rebuilt_projection(self) -> None:
        refreshed: list[bool] = []
        provider = McpManagerPluginProvider(
            runtime_root=self.root,
            core_context=object(),
            refresh_capabilities=lambda: refreshed.append(True),
        )
        provider._ensure_manager_started = lambda: None  # type: ignore[method-assign]
        provider._refresh_projection = lambda: None  # type: ignore[method-assign]
        manager_client = Mock()
        manager_client.rescan_sync.return_value = {"status": RuntimeStatus.OK}

        with patch("pal.mcp.plugin.McpManagerClient", return_value=manager_client):
            provider.start_manager()

        self.assertEqual(provider.last_error, "")
        self.assertEqual(refreshed, [True])
        manager_client.rescan_sync.assert_called_once_with()

    def test_plugin_attach_projects_capabilities_and_skills_through_sidecar(self) -> None:
        self._write_config_and_server()
        core, skill_service = self._core_with_services()
        handle = build_mcp_plugin(runtime_root=self.root).register_with_core(core.context)
        host = PluginHost(context=core.context, runtime_root=self.root)

        handle.ports["mcp"].start_manager()
        host._do_attach(handle)
        try:
            search = core.context.execution_runtime.execute(CapabilityCall(name="op_tool_search", args={"query": "alpha"}))
            alpha_hit = next(item for item in search.structured["hits"] if item["alias"] == "mcp_demo_alpha")
            alpha_read = core.context.execution_runtime.execute(
                CapabilityCall(name="op_tool_read", args={"name": alpha_hit["alias"]})
            )
            self.assertEqual(alpha_read.structured["alias"], "mcp_demo_alpha")
            self.assertNotIn("canonical_path", alpha_read.structured)
            self.assertIn("mcp_demo_prompt_brief", str(SkillSearchTool(service=skill_service).invoke({"query": "brief", "top_k": 5}).structured))
            show = core.context.execution_runtime.execute(CapabilityCall(name="mcp_show", args={}))
            self.assertEqual(show.status, RuntimeStatus.OK)
            image = core.context.execution_runtime.execute(CapabilityCall(name="mcp_image_prepare", args={"url": "https://example.test/a.png"}))
            self.assertEqual(image.structured["kind"], "url")
            image_path = self.root / "image.png"
            image_path.write_bytes(b"not really an image")
            path_image = core.context.execution_runtime.execute(
                CapabilityCall(name="mcp_image_prepare", args={"path": str(image_path), "mode": "path"})
            )
            self.assertEqual(path_image.structured["kind"], "path")
            self.assertEqual(Path(path_image.structured["path"]), image_path.resolve())
            image_record = core.context.execution_runtime.registry_generation.indirect_aliases[
                "mcp_image_prepare"
            ]
            self.assertEqual(image_record.execution.effect_kind, EffectKind.LOCAL_WRITE)
            self.assertEqual(image_record.execution.idempotency, Idempotency.IDEMPOTENT)
            self.assertEqual(image_record.execution.retry_policy, RetryPolicy.AUTOMATIC)
        finally:
            host._do_detach(handle)
        missing = core.context.execution_runtime.execute(CapabilityCall(name="mcp_demo_alpha", args={}))
        self.assertIn("unknown capability", missing.text)

    def test_default_disabled_first_party_mcp_can_attach_temporarily_or_enable(self) -> None:
        core, _skill_service = self._core_with_services()
        builtin = self.root / "plugins" / "_builtin" / "mcp"
        builtin.mkdir(parents=True)
        (builtin / "plugin.toml").write_text(
            "\n".join(
                [
                    'plugin_id = "mcp"',
                    'entrypoint = "pal.plugins_builtin.mcp.runtime"',
                    'version = "0.1.0"',
                    "enabled_by_default = false",
                    'lifecycle_protocol = "raii.v1"',
                    'module_id = "mcp"',
                ]
            ),
            encoding="utf-8",
        )
        host = PluginHost(context=core.context, runtime_root=self.root)

        host.rescan()
        attached = host.attach("mcp")

        self.assertEqual(attached["status"], RuntimeStatus.FORBIDDEN)
        self.assertFalse(attached["enabled"])
        self.assertNotIn("mcp_show", core.context.capability_registry.descriptors)

        enabled = host.enable("mcp")
        try:
            self.assertEqual(enabled["status"], RuntimeStatus.OK)
            mcp_record = next(item for item in host.list_plugins() if item["plugin_id"] == "mcp")
            self.assertTrue(mcp_record["enabled"])
            self.assertTrue(mcp_record["attached"])
            self.assertIn("mcp_show", core.context.capability_registry.descriptors)
            self.assertIn("mcp_image_prepare", core.context.capability_registry.descriptors)
        finally:
            host.detach("mcp")

    def test_explicitly_disabled_first_party_plugin_attach_points_to_enable(self) -> None:
        core, _skill_service = self._core_with_services()
        builtin = self.root / "plugins" / "_builtin" / "mcp"
        builtin.mkdir(parents=True)
        (builtin / "plugin.toml").write_text(
            "\n".join(
                [
                    'plugin_id = "mcp"',
                    'entrypoint = "pal.plugins_builtin.mcp.runtime"',
                    'version = "0.1.0"',
                    "enabled_by_default = true",
                    'lifecycle_protocol = "raii.v1"',
                    'module_id = "mcp"',
                ]
            ),
            encoding="utf-8",
        )
        host = PluginHost(context=core.context, runtime_root=self.root)

        host.rescan()
        self.assertEqual(host.enable("mcp")["status"], RuntimeStatus.OK)
        self.assertEqual(host.disable("mcp")["status"], RuntimeStatus.OK)
        attached = host.attach("mcp")

        self.assertEqual(attached["status"], RuntimeStatus.FORBIDDEN)
        self.assertEqual(attached["reason"], "plugin_disabled")
        self.assertEqual(attached["next_action"], "plugin_enable")


if __name__ == "__main__":
    unittest.main()
