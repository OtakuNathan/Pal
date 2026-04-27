from __future__ import annotations

import ast
import asyncio
import importlib
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pal.channel import (
    ChannelAdapter,
    ChannelEnvelope,
    ChannelEndpointQueueBase,
    ChannelEventSource,
    ChannelRuntime,
    EndpointConfig,
    ResponseHandle,
    register_with_core as register_channel_with_core,
)
from pal.control import ControlPlane, register_with_core as register_control_with_core
from pal.core import (
    MainLoop,
    PalCore,
    ToolExecutionRecord,
    ToolStagnationGuardProcess,
    canonical_result_fingerprint,
    canonical_tool_signature_hash,
    register_with_core as register_core_with_core,
)
from pal.core.runtime_config import RuntimeConfig
from pal.execution import CapabilityCall, CapabilityResult, ToolCallBudget, register_with_core as register_execution_with_core
from pal.failure import (
    FAILURE_VERIFICATION_FAILED,
    FAILURE_VERIFICATION_OK,
    FailureRuntime,
    FailureSignal,
    register_with_core as register_failure_with_core,
)
from pal.foundation import EventEnvelope, RawSQLHookRegistry
from pal.identity import IdentityRepository, IdentityService, register_with_core as register_identity_with_core
from pal.llm import CanonicalLLMOutcome, CanonicalToolCall, LLMPreflightAdvice
from pal.memory import (
    L1TranscriptMessage,
    L2Entry,
    L3RecallView,
    L3ProviderSelector,
    MemoryCompactRequest,
    MemoryQuery,
    MemoryService,
    register_with_core as register_memory_with_core,
)
from pal.plugins import PluginHost, register_with_core as register_plugins_with_core
from pal.plugins.l3 import MockL3Plugin, register_with_core as register_l3_with_core
from pal.service import ServiceDefinition, ServiceManager, ServiceTriggerEvent, register_with_core as register_service_with_core
from pal.service.scheduling import compute_next_service_run_at_utc, utc_now_dt
from pal.shared import LLMStreamEventKind, MinionProgressEvent, PromptAssemblyContext, SINGLETON_TARGET
from pal.stream_events import NormalizedLLMStreamEvent
from pal.wizard import WizardService
from pal.minion import TaskingService, register_with_core as register_tasking_with_core


ROOT = Path(__file__).resolve().parents[1]


class RecordingAdapter(ChannelAdapter):
    channel_kind = "stdio"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, response_handle: ResponseHandle, text: str) -> None:
        self.sent.append((response_handle.endpoint_id, text))


class EchoNormalizer:
    def normalize(self, payload: object) -> dict[str, object]:
        return {"normalized": payload}


class StubEndpoint(ChannelEndpointQueueBase):
    def __init__(self, endpoint: EndpointConfig) -> None:
        super().__init__(endpoint=endpoint)
        self.sent: list[tuple[str, str]] = []
        self.streamed: list[tuple[str, str, str]] = []
        self.statuses: list[tuple[str, str, dict[str, object]]] = []

    def normalize_raw(self, payload: object) -> dict[str, object]:
        return {"normalized": payload}

    def send_reply(self, response_handle: ResponseHandle, text: str) -> None:
        self.sent.append((response_handle.endpoint_id, text))

    def send_stream_event(self, response_handle: ResponseHandle, event: NormalizedLLMStreamEvent) -> None:
        if event.event_kind == LLMStreamEventKind.TEXT_DELTA:
            self.streamed.append((response_handle.endpoint_id, "text", event.text))
        elif event.event_kind == LLMStreamEventKind.REASONING_DELTA:
            self.streamed.append((response_handle.endpoint_id, "reasoning", event.reasoning_text))
        elif event.event_kind == LLMStreamEventKind.TOOL_CALL and event.tool_call is not None:
            self.streamed.append((response_handle.endpoint_id, "tool_call", event.tool_call.name))
        else:
            self.streamed.append((response_handle.endpoint_id, str(event.event_kind), event.finish_reason or event.error_text))

    def send_status(self, response_handle: ResponseHandle, kind: str, payload: dict[str, object]) -> None:
        self.statuses.append((response_handle.endpoint_id, kind, dict(payload)))

    def inspect_health(self) -> dict[str, object]:
        return {
            "endpoint_id": self.endpoint.endpoint_id,
            "healthy": True,
            "attached": self.attached,
            "enabled": self.enabled,
            "last_delivery_error": self.last_delivery_error,
        }

    def inspect_auth_state(self) -> dict[str, object]:
        return {
            "endpoint_id": self.endpoint.endpoint_id,
            "paired": self.paired,
            "attached": self.attached,
            "authorized": bool(self.pairing_metadata.get("authorized", self.paired)),
        }


class EchoTool:
    name = "echo"
    description = "Echo test arguments back as a stable result."
    args_schema = {
        "type": "object",
        "properties": {
            "value": {"type": "string"},
        },
    }
    result_schema = {
        "type": "object",
        "properties": {
            "echo": {"type": "object"},
        },
    }

    def invoke(self, args: dict[str, object]) -> CapabilityResult:
        return CapabilityResult(status="ok", llm_text="stable-result", text="stable-result", structured={"echo": args})


class VerboseTool:
    name = "verbose"
    description = "Return a placeholder summary plus rich llm_text."
    args_schema = {
        "type": "object",
        "properties": {
            "value": {"type": "string"},
        },
    }
    result_schema = {
        "type": "object",
        "properties": {
            "payload": {"type": "object"},
        },
    }

    def invoke(self, args: dict[str, object]) -> CapabilityResult:
        return CapabilityResult(
            status="ok",
            text="placeholder",
            structured={"payload": args},
            llm_text='{"payload": {"value": "rich"}}',
        )


class HugeTool:
    name = "huge"
    description = "Return a very large llm_text payload."
    args_schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    result_schema = {"type": "object", "properties": {"payload": {"type": "object"}}}

    def __init__(self, *, size: int = 4000) -> None:
        self.size = size

    def invoke(self, args: dict[str, object]) -> CapabilityResult:
        return CapabilityResult(
            status="ok",
            text="huge-result",
            structured={"payload": args, "size": self.size},
            llm_text="X" * self.size,
        )


class MalformedTool:
    name = "malformed"
    description = "Return an object whose llm_text is empty to simulate a bad plugin result."
    args_schema = {"type": "object", "properties": {}}
    result_schema = {"type": "object", "properties": {}}

    def invoke(self, args: dict[str, object]):
        class BadResult:
            status = "ok"
            text = "bad"
            structured = {"bad": True}
            llm_text = ""

        _ = args
        return BadResult()


class RaisingTool:
    name = "boom"
    description = "Raise a runtime error for tool isolation tests."
    args_schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    result_schema = {"type": "object", "properties": {}}

    def invoke(self, args: dict[str, object]) -> CapabilityResult:
        raise RuntimeError(f"boom: {args!r}")


class ScriptedLLMRuntime:
    def __init__(self, outcomes: list[CanonicalLLMOutcome]) -> None:
        self.outcomes = outcomes
        self.requests = []

    def preflight(self, request) -> LLMPreflightAdvice:
        self.requests.append(("preflight", request))
        return LLMPreflightAdvice(
            status="ready",
            active_model="stub-model",
            fallback_chain=["stub-fallback"],
            target_input_budget=2048,
            reserved_output_tokens=request.max_output_tokens,
        )

    def generate(self, request):
        self.requests.append(("generate", request))
        if self.outcomes:
            return self.outcomes.pop(0)
        return CanonicalLLMOutcome(text="done", tool_calls=[], finish_reason="stop")

    def generate_stream(self, request):
        self.requests.append(("generate_stream", request))
        if self.outcomes:
            outcome = self.outcomes.pop(0)
        else:
            outcome = CanonicalLLMOutcome(text="done", tool_calls=[], finish_reason="stop")
        events: list[NormalizedLLMStreamEvent] = []
        if outcome.reasoning_text:
            events.append(
                NormalizedLLMStreamEvent(
                    event_kind=LLMStreamEventKind.REASONING_DELTA,
                    reasoning_text=outcome.reasoning_text,
                )
            )
        if outcome.text:
            events.append(
                NormalizedLLMStreamEvent(
                    event_kind=LLMStreamEventKind.TEXT_DELTA,
                    text=outcome.text,
                )
            )
        for tool_call in outcome.tool_calls:
            events.append(
                NormalizedLLMStreamEvent(
                    event_kind=LLMStreamEventKind.TOOL_CALL,
                    tool_call=tool_call,
                )
            )
        events.append(
            NormalizedLLMStreamEvent(
                event_kind=LLMStreamEventKind.DONE,
                finish_reason=outcome.finish_reason,
                response_mode=outcome.response_mode,
            )
        )
        return events


class PalV2ArchitectureSkeletonTests(unittest.TestCase):
    def _create_database(self):
        runtime_root = Path(tempfile.mkdtemp(prefix="pal_architecture_test_"))
        wizard = WizardService()
        registration = wizard.provision_runtime(
            display_name="PalV2 Architecture Test",
            runtime_root=runtime_root,
            db_filename="pal_test.sqlite3",
            pal_entrypoint="pal.runtime.test",
        )
        database = wizard.create_database(registration)
        return runtime_root, database

    def test_top_level_modules_import(self) -> None:
        modules = (
            "pal.foundation",
            "pal.shared",
            "pal.core",
            "pal.control",
            "pal.channel",
            "pal.identity",
            "pal.llm",
            "pal.memory",
            "pal.execution",
            "pal.failure",
            "pal.minion",
            "pal.service",
            "pal.web_search",
            "pal.web_fetch",
            "pal.minion",
            "pal.bootstrap",
            "pal.wizard",
            "pal.minion",
            "pal.plugins",
        )
        for module_name in modules:
            imported = importlib.import_module(module_name)
            self.assertIsNotNone(imported, module_name)

    def test_shared_exports_introspection_contract_shape(self) -> None:
        shared = importlib.import_module("pal.shared")
        self.assertTrue(hasattr(shared, "IntrospectionCall"))
        self.assertTrue(hasattr(shared, "IntrospectionResult"))
        self.assertTrue(hasattr(shared, "IntrospectionPort"))
        self.assertTrue(hasattr(shared, "LifecycleIntrospectionPort"))
        self.assertTrue(hasattr(shared, "standard_descriptors"))
        self.assertTrue(hasattr(shared, "PromptFragment"))
        self.assertTrue(hasattr(shared, "PromptAssemblyContext"))
        self.assertTrue(hasattr(shared, "PromptFragmentProvider"))

    def test_public_modules_export_introspection_surface(self) -> None:
        exports = {
            "pal.core": ("CoreIntrospectionProvider", "register_with_core", "inspect_core"),
            "pal.channel": ("ChannelIntrospectionProvider", "register_with_core", "inspect_channel"),
            "pal.identity": ("IdentityIntrospectionProvider", "register_with_core", "inspect_identity"),
            "pal.llm": ("LLMIntrospectionProvider", "register_with_core", "inspect_llm"),
            "pal.memory": ("MemoryIntrospectionProvider", "register_with_core", "inspect_memory"),
            "pal.execution": ("ExecutionIntrospectionProvider", "register_with_core", "inspect_execution"),
            "pal.minion": ("TaskingIntrospectionProvider", "register_with_core", "inspect_tasking"),
            "pal.service": ("ServiceIntrospectionProvider", "register_with_core", "inspect_service"),
            "pal.web_search": ("WebSearchIntrospectionProvider", "register_with_core", "inspect_web_search"),
            "pal.web_fetch": ("WebFetchIntrospectionProvider", "register_with_core", "inspect_web_fetch"),
            "pal.control": ("ControlIntrospectionProvider", "register_with_core", "inspect_control"),
            "pal.failure": ("FailureIntrospectionProvider", "register_with_core"),
            "pal.plugins": ("PluginsIntrospectionProvider", "register_with_core"),
            "pal.bootstrap": ("inspect_bootstrap",),
            "pal.wizard": ("inspect_wizard",),
            "pal.plugins.l3": ("register_with_core",),
            "pal.minion": ("inspect_minion", "inspect_tasking"),
        }
        for module_name, symbols in exports.items():
            module = importlib.import_module(module_name)
            for symbol in symbols:
                self.assertTrue(hasattr(module, symbol), f"{module_name} missing {symbol}")

    def test_wizard_stays_outside_pal_core_registration_surface(self) -> None:
        wizard = importlib.import_module("pal.wizard")
        bootstrap = importlib.import_module("pal.bootstrap")

        self.assertFalse(hasattr(wizard, "register_with_core"))
        self.assertFalse(hasattr(wizard, "WizardIntrospectionProvider"))
        self.assertFalse(hasattr(bootstrap, "register_with_core"))
        self.assertFalse(hasattr(bootstrap, "BootstrapIntrospectionProvider"))

    def test_core_does_not_import_models_or_repositories(self) -> None:
        for relative_path in (
            "src/pal/core/contracts.py",
            "src/pal/core/runtime.py",
        ):
            self._assert_no_forbidden_imports(
                ROOT / relative_path,
                forbidden_fragments=(".models", ".repository"),
            )

    def test_control_does_not_depend_on_execution_runtime_impl(self) -> None:
        for relative_path in (
            "src/pal/control/contracts.py",
            "src/pal/control/service.py",
        ):
            self._assert_no_forbidden_imports(
                ROOT / relative_path,
                forbidden_fragments=("pal.execution.runtime",),
            )

    def test_minion_does_not_depend_on_user_facing_channel_modules(self) -> None:
        for relative_path in (
            "src/pal/minion/contracts.py",
            "src/pal/minion/runtime.py",
        ):
            self._assert_no_forbidden_imports(
                ROOT / relative_path,
                forbidden_fragments=("pal.channel",),
            )

    def test_memory_does_not_import_plugin_implementations_directly(self) -> None:
        for relative_path in (
            "src/pal/memory/contracts.py",
            "src/pal/memory/repository.py",
            "src/pal/memory/service.py",
        ):
            self._assert_no_forbidden_imports(
                ROOT / relative_path,
                forbidden_fragments=("pal.plugins.l3",),
            )

    def test_core_no_longer_exports_legacy_dependency_bundle(self) -> None:
        core = importlib.import_module("pal.core")
        self.assertFalse(hasattr(core, "CoreDependencies"))
        self.assertTrue(hasattr(core, "PromptFragmentRegistry"))

    def test_identity_contracts_do_not_import_models(self) -> None:
        self._assert_no_forbidden_imports(
            ROOT / "src/pal/identity/contracts.py",
            forbidden_fragments=(".models",),
        )

    def test_raw_sql_hook_registry_is_idle_by_default(self) -> None:
        runtime_root, database = self._create_database()
        db_path = database.db_path
        try:
            self.assertEqual(database.raw_sql_hooks.iter_statements(), ())
            conn = sqlite3.connect(db_path)
            try:
                fts_tables = conn.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type IN ('table', 'view') AND LOWER(name) LIKE '%fts%'
                    """
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(fts_tables, [])
        finally:
            database.close()
            shutil.rmtree(runtime_root, ignore_errors=True)

    def test_raw_sql_hook_registry_collects_statements_without_auto_registration(self) -> None:
        registry = RawSQLHookRegistry()
        registry.register("CREATE VIRTUAL TABLE memory_fts USING fts5(content)")
        self.assertEqual(
            registry.iter_statements(),
            ("CREATE VIRTUAL TABLE memory_fts USING fts5(content)",),
        )

    def test_memory_service_reads_l3_via_context_owned_registry(self) -> None:
        plugin = MockL3Plugin(records=[{"document_id": "fact:1", "title": "Redis"}])
        selector = L3ProviderSelector(resolver={plugin.provider_id: plugin}.get)  # type: ignore[arg-type]
        service = MemoryService(l3_selector=selector)
        service.l3_selector.active_provider_id = "mock_l3"

        recall_result = plugin.recall(MemoryQuery(level="deep", queries=["redis"]))

        self.assertEqual(recall_result.hits, [{"document_id": "fact:1", "title": "Redis"}])

    def test_execution_calls_registered_capabilities_from_pal_core(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        register_execution_with_core(core.context)
        register_tasking_with_core(core.context, TaskingService())

        core.publish_module_capabilities("core")
        core.publish_module_capabilities("execution")
        core.publish_module_capabilities("tasking")

        result = core.context.execution_runtime.execute(CapabilityCall(name="intro_module_tasking_show"))

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.structured["mounted"])

    def test_execution_registers_shell_exec_builtin_tool(self) -> None:
        core = PalCore()

        register_execution_with_core(core.context)

        self.assertIn("shell.exec", core.context.execution_runtime.tools)
        self.assertIn("tool.search", core.context.execution_runtime.tools)
        self.assertIn("tool.read", core.context.execution_runtime.tools)

    def test_shell_exec_builtin_tool_runs_commands(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)

        result = core.context.execution_runtime.execute_tool(
            CanonicalToolCall(name="shell.exec", args={"cmd": "echo pong"})
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.structured["returncode"], 0)
        self.assertEqual(str(result.structured["stdout"]).strip(), "pong")
        self.assertEqual(result.text, "pong")

    def test_execution_exec_run_capability_routes_to_shell_exec_tool(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")

        result = core.context.execution_runtime.execute_tool(
            CanonicalToolCall(name="op_exec_run", args={"cmd": "echo pong"})
        )

        self.assertTrue(result.ok)
        self.assertEqual(str(result.structured["stdout"]).strip(), "pong")

    def test_capability_compiler_abbreviates_canonical_paths_for_core_and_plugins(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        memory_service = MemoryService()
        register_memory_with_core(core.context, memory_service)
        plugin = MockL3Plugin()
        register_l3_with_core(core.context, plugin)

        core.publish_module_capabilities("execution")
        core.publish_module_capabilities("memory")
        core.publish_module_capabilities(plugin.module_id)

        published = core.context.capability_registry.descriptors

        self.assertIn("op_exec_disc_search", published)
        self.assertIn("op_exec_capability_call", published)
        self.assertIn("intro_module_memory_show", published)
        self.assertIn("op_memory_mgmt_set_active_provider", published)
        self.assertIn("intro_provider_l3_show::mock_l3", published)
        self.assertIn("op_l3_recall_query::mock_l3", published)
        self.assertNotIn("operation_execution_discovery_search", published)
        self.assertNotIn("introspection_module_memory_show", published)

    def test_execution_tool_failures_are_isolated(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.context.execution_runtime.register_tool(RaisingTool())

        result = core.context.execution_runtime.execute_tool(
            CanonicalToolCall(name="boom", args={"value": "x"})
        )

        self.assertFalse(result.ok)
        self.assertIn("tool execution failed", result.text)
        self.assertEqual(result.structured["tool"], "boom")

    def test_execution_tools_introspection_includes_description_and_schema(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")

        result = core.context.execution_runtime.execute(CapabilityCall(name="intro_module_exec_tools"))

        self.assertEqual(result.status, "ok")
        tools = result.structured["tools"]
        shell_exec = next(tool for tool in tools if tool["name"] == "shell.exec")
        self.assertIn("description", shell_exec)
        self.assertIn("cmd", shell_exec["args_schema"]["properties"])

    def test_tool_search_discovers_shell_exec_by_natural_language(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")

        result = core.context.execution_runtime.execute_tool(
            CanonicalToolCall(name="op_exec_disc_search", args={"query": "run shell command", "top_k": 5})
        )

        self.assertTrue(result.ok)
        hit_names = [item["name"] for item in result.structured["hits"]]
        self.assertIn("op_exec_run", hit_names)

    def test_tool_read_returns_full_tool_contract(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")

        result = core.context.execution_runtime.execute_tool(
            CanonicalToolCall(name="op_exec_disc_read", args={"name": "op_exec_run"})
        )

        self.assertTrue(result.ok)
        capability = result.structured["capability"]
        self.assertEqual(capability["canonical_path"], "op_exec_run")
        self.assertEqual(result.text, "capability definition")
        self.assertIn("op_exec_run", result.llm_text)
        self.assertIn("cmd", capability["parameters_schema"]["properties"])
        self.assertIn("returncode", capability["result_schema"]["properties"])

    def test_tool_read_invalid_result_carries_llm_text(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")

        result = core.context.execution_runtime.execute_tool(
            CanonicalToolCall(name="op_exec_disc_read", args={})
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.text, "name missing")
        self.assertEqual(result.llm_text, "name missing")

    def test_successful_introspection_llm_text_contains_key_structured_content(self) -> None:
        core = PalCore()
        runtime_root, database = self._create_database()
        try:
            service = IdentityService(repository=IdentityRepository())
            service.ensure_defaults()
            register_identity_with_core(core.context, service)
            core.publish_module_capabilities("identity")

            result = core.context.execution_runtime.execute(
                CapabilityCall(name="intro_module_identity_show")
            )

            self.assertEqual(result.status, "ok")
            self.assertEqual(result.text, "identity snapshot")
            self.assertIn("has_persona", result.llm_text)
            self.assertIn("mounted", result.llm_text)
        finally:
            database.close()
            shutil.rmtree(runtime_root, ignore_errors=True)

    def test_invalid_capability_result_preserves_llm_text(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")

        result = core.context.execution_runtime.execute(
            CapabilityCall(name="op_exec_disc_read", args={})
        )

        self.assertEqual(result.status, "error")
        self.assertEqual(result.text, "name missing")
        self.assertEqual(result.llm_text, "name missing")

    def test_turn_runtime_passes_tool_descriptions_and_input_schemas_to_llm(self) -> None:
        runtime_root, database = self._create_database()
        try:
            core = PalCore()
            register_core_with_core(core)
            register_execution_with_core(core.context)
            core.publish_module_capabilities("execution")
            register_channel_with_core(core.context, ChannelRuntime())
            identity_service = IdentityService(repository=IdentityRepository())
            identity_service.ensure_defaults()
            register_identity_with_core(core.context, identity_service)
            llm_module = importlib.import_module("pal.llm")
            llm_runtime = llm_module.LLMRuntime(
                endpoint_resolver=llm_module.EndpointResolver(repository=llm_module.LLMEndpointRepository()),
                settings_repository=llm_module.RuntimeSettingRepository(),
            )
            llm_module.register_with_core(core.context, llm_runtime)
            memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
            register_memory_with_core(core.context, memory_service)
            l3_plugin = MockL3Plugin()
            register_l3_with_core(core.context, l3_plugin)
            memory_service.l3_selector.active_provider_id = l3_plugin.provider_id
            core.publish_module_capabilities("identity")
            core.publish_module_capabilities("llm")
            core.publish_module_capabilities("memory")
            core.publish_module_capabilities(l3_plugin.module_id)
            core.context.execution_runtime.register_tool(EchoTool())
            scripted_llm = ScriptedLLMRuntime([CanonicalLLMOutcome(text="final answer", tool_calls=[], finish_reason="stop")])
            core.context.port_registry["llm:llm"] = scripted_llm

            core.process_channel_turn(
                ChannelEnvelope(
                    event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "hello"}),
                    endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
                    response_handle=ResponseHandle(endpoint_id="stdio"),
                )
            )

            request = next(request for kind, request in scripted_llm.requests if kind in {"generate", "generate_stream"})
            exposed_names = [item["function"]["name"] for item in request.tools]
            self.assertIn("op_exec_disc_search", exposed_names)
            self.assertIn("op_exec_disc_read", exposed_names)
            self.assertIn("op_exec_run", exposed_names)
            self.assertIn("op_exec_capability_call", exposed_names)
            self.assertNotIn("intro_module_identity_show", exposed_names)
            self.assertNotIn("intro_module_llm_active", exposed_names)
            self.assertNotIn("op_llm_mgmt_set_active_endpoint", exposed_names)
            self.assertNotIn("echo", exposed_names)
            exec_tool = next(item for item in request.tools if item["function"]["name"] == "op_exec_run")
            self.assertIn("cmd", exec_tool["function"]["parameters"]["properties"])
        finally:
            database.close()
            shutil.rmtree(runtime_root, ignore_errors=True)

    def test_failure_flow_success_writes_system_case_memory(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        register_execution_with_core(core.context)
        register_failure_with_core(core, FailureRuntime())
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        register_memory_with_core(core.context, memory_service)
        l3_plugin = MockL3Plugin()
        register_l3_with_core(core.context, l3_plugin)
        memory_service.l3_selector.active_provider_id = l3_plugin.provider_id
        for module_id in ("execution", "failure", "memory", l3_plugin.module_id):
            core.publish_module_capabilities(module_id)
        scripted_llm = ScriptedLLMRuntime(
            [
                CanonicalLLMOutcome(
                    text="",
                    tool_calls=[
                        CanonicalToolCall(
                            name="op_l3_maintenance_refresh_indexes",
                            args={"target_id": l3_plugin.provider_id},
                        )
                    ],
                    finish_reason="tool_calls",
                ),
                CanonicalLLMOutcome(
                    text='{"verification_status":"ok","reason":"Indexes refreshed and the provider is healthy."}',
                    tool_calls=[],
                    finish_reason="stop",
                ),
            ]
        )
        core.context.port_registry["llm:llm"] = scripted_llm

        outcome = asyncio.run(
            core.handle_failure_async(
                FailureSignal(
                    subsystem="memory",
                    component=l3_plugin.provider_id,
                    failure_kind="provider_failure",
                    severity="medium",
                    primary_blocker="L3 index refresh is required before recall can continue.",
                    evidence={"provider_id": l3_plugin.provider_id},
                    related_ids={"turn_id": "repair-turn-1"},
                    safe_to_retry=True,
                    repair_domain="memory:provider",
                ),
                origin="test.memory",
            )
        )

        self.assertEqual(outcome.verification.status, FAILURE_VERIFICATION_OK)
        self.assertIsNotNone(outcome.repair_resolution)
        self.assertTrue(any(record.get("document_kind") == "case" for record in l3_plugin.records))
        self.assertTrue(any(entry.kind == "case" for entry in memory_service.l2_store.items.values()))
        request = next(request for kind, request in scripted_llm.requests if kind == "generate")
        tool_names = [tool["function"]["name"] for tool in request.tools]
        self.assertIn("op_l3_maintenance_refresh_indexes", tool_names)
        self.assertNotIn("op_exec_run", tool_names)

    def test_failure_flow_llm_blocker_fails_without_work_order(self) -> None:
        core = PalCore()
        register_failure_with_core(core, FailureRuntime())

        outcome = asyncio.run(
            core.handle_failure_async(
                FailureSignal(
                    subsystem="llm",
                    component="primary",
                    failure_kind="provider_failure",
                    severity="high",
                    primary_blocker="All configured llm endpoints failed.",
                    evidence={"endpoint_id": "primary"},
                    related_ids={"turn_id": "turn-llm-fail"},
                    safe_to_retry=False,
                    repair_domain="llm:core",
                ),
                origin="test.llm",
            )
        )

        self.assertEqual(outcome.verification.status, FAILURE_VERIFICATION_FAILED)
        self.assertIsNotNone(outcome.report)
        self.assertIsNone(outcome.repair_work_order)
        self.assertIn("LLM", outcome.user_feedback.summary.upper())

    def test_plugin_failure_generates_work_order_only_for_known_first_party_domain(self) -> None:
        runtime_root = Path(tempfile.mkdtemp(prefix="pal_failure_plugins_"))
        try:
            core = PalCore()
            register_core_with_core(core)
            register_execution_with_core(core.context)
            register_failure_with_core(core, FailureRuntime())
            host = PluginHost(context=core.context, runtime_root=runtime_root, services={})
            register_plugins_with_core(core.context, host)
            for module_id in ("execution", "failure", "plugins"):
                core.publish_module_capabilities(module_id)
            scripted_llm = ScriptedLLMRuntime(
                [
                    CanonicalLLMOutcome(
                        text='{"verification_status":"degraded","reason":"Inline repair is insufficient; isolate repair is required."}',
                        tool_calls=[],
                        finish_reason="stop",
                    )
                ]
            )
            core.context.port_registry["llm:llm"] = scripted_llm

            outcome = asyncio.run(
                core.handle_failure_async(
                    FailureSignal(
                        subsystem="plugin",
                        component="demo",
                        failure_kind="load_failure",
                        severity="medium",
                        primary_blocker="The first-party plugin needs isolate repair.",
                        evidence={"plugin_id": "demo"},
                        related_ids={"plugin_id": "demo"},
                        safe_to_retry=False,
                        repair_domain="plugin:first_party",
                        known_first_party_repair_domain=True,
                    ),
                    origin="test.plugin",
                )
            )

            self.assertEqual(outcome.verification.status, "degraded")
            self.assertIsNotNone(outcome.repair_work_order)
            request = next(request for kind, request in scripted_llm.requests if kind == "generate")
            tool_names = [tool["function"]["name"] for tool in request.tools]
            self.assertIn("op_plugin_mgmt_rescan", tool_names)
            self.assertIn("op_plugin_mgmt_enable", tool_names)
            self.assertIn("op_plugin_mgmt_disable", tool_names)
            self.assertNotIn("op_plugin_mgmt_detach", tool_names)
            self.assertNotIn("op_exec_run", tool_names)
        finally:
            shutil.rmtree(runtime_root, ignore_errors=True)

    def test_pal_core_collects_prompt_fragments_only_from_registry(self) -> None:
        runtime_root, database = self._create_database()
        try:
            identity_service = IdentityService(repository=IdentityRepository())
            identity_service.ensure_defaults()

            core = PalCore()
            register_core_with_core(core)
            service_manager = ServiceManager()
            service_manager.register(ServiceDefinition(service_id="digest", goal="Summarize updates"))
            tasking_service = TaskingService()
            tasking_service.build_context_pack(work_order_id="wo-1", goal="Ship prompt registry")
            memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
            memory_service.l1_store.append(
                [
                    L1TranscriptMessage(role="user", content="What timezone should you use?"),
                    L1TranscriptMessage(role="assistant", content="I should use Asia/Shanghai context."),
                ]
            )
            memory_service.l2_store.upsert_entries(
                [
                    L2Entry(
                        entry_id="summary-1",
                        kind="fact",
                        scope="system",
                        title="Timezone Preference",
                        summary="The user prefers replies in Asia/Shanghai context.",
                        rendered="The user prefers replies in Asia/Shanghai context.",
                    )
                ],
                touch=True,
                top_of_mind=True,
            )

            register_identity_with_core(core.context, identity_service)
            register_control_with_core(core.context, ControlPlane())
            register_tasking_with_core(core.context, tasking_service)
            register_service_with_core(core.context, service_manager)
            register_memory_with_core(core.context, memory_service)

            fragments = core.collect_prompt_fragments(PromptAssemblyContext(core_mode="default"))
            sections = [fragment.section for fragment in fragments]

            self.assertEqual(sections, ["identity", "rules"])
            prompt_ir = core.build_prompt_ir(
                PromptAssemblyContext(
                    core_mode="default",
                    event=EventEnvelope(
                        event_kind="user.message",
                        source_kind="channel",
                        payload={"text": "Hello from user"},
                    ),
                )
            )
            prompt = core.build_canonical_prompt(
                PromptAssemblyContext(
                    core_mode="default",
                    event=EventEnvelope(
                        event_kind="user.message",
                        source_kind="channel",
                        payload={"text": "Hello from user"},
                    ),
                )
            )
            self.assertEqual(prompt.metadata["fragment_sections"], ["identity", "operating_rules", "memory_context"])
            self.assertEqual(
                prompt.metadata["user_context_blocks"],
                ["l1_recent_context_0", "l1_recent_context_1"],
            )
            self.assertEqual(prompt_ir.turn_kind, "chat")
            self.assertEqual(prompt.messages[0]["role"], "system")
            self.assertEqual(prompt.messages[-1], {"role": "user", "content": "Hello from user"})
            self.assertIn("## Identity", prompt.messages[0]["content"])
            self.assertIn("## Operating Rules", prompt.messages[0]["content"])
            self.assertNotIn("## Capability Guide", prompt.messages[0]["content"])
            self.assertIn("Source-of-truth preference", prompt.messages[0]["content"])
            self.assertNotIn("op_exec_disc_search", prompt.messages[0]["content"])
            self.assertNotIn("op_exec_capability_call", prompt.messages[0]["content"])
            self.assertLess(prompt.messages[0]["content"].index("## Identity"), prompt.messages[0]["content"].index("## Operating Rules"))
            self.assertNotIn("## Runtime Overlay", prompt.messages[0]["content"])
            self.assertNotIn("## Memory Projection", prompt.messages[0]["content"])
            self.assertEqual(prompt.messages[1], {"role": "user", "content": "What timezone should you use?"})
            self.assertEqual(prompt.messages[2], {"role": "assistant", "content": "I should use Asia/Shanghai context."})
            self.assertIn("Working Memory", prompt.messages[0]["content"])
            self.assertIn("Timezone Preference", prompt.messages[0]["content"])
            self.assertNotIn("Issued work orders", prompt.messages[0]["content"])
            self.assertNotIn("Registered services", prompt.messages[0]["content"])
            self.assertNotIn("Active L3", prompt.messages[0]["content"])
            self.assertNotIn("Available L3", prompt.messages[0]["content"])
            self.assertNotIn("candidate_state", prompt.messages[0]["content"])
        finally:
            database.close()
            shutil.rmtree(runtime_root, ignore_errors=True)

    def test_pal_core_builds_service_trigger_prompt_as_single_user_input(self) -> None:
        runtime_root, database = self._create_database()
        try:
            core = PalCore()
            register_core_with_core(core)
            identity_service = IdentityService(repository=IdentityRepository())
            identity_service.ensure_defaults()
            register_identity_with_core(core.context, identity_service)
            memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
            register_memory_with_core(core.context, memory_service)
            memory_service.l1_store.append(
                [
                    L1TranscriptMessage(role="user", content="Remember the last digest."),
                    L1TranscriptMessage(role="assistant", content="I will keep it in mind."),
                ]
            )
            memory_service.l2_store.upsert_entries(
                [
                    L2Entry(
                        entry_id="svc-summary",
                        kind="fact",
                        scope="system",
                        title="Recent Digest Preference",
                        summary="Prefer concise digests.",
                        rendered="Prefer concise digests.",
                        touched_at="2026-01-01T00:00:00+00:00",
                    )
                ],
                touch=True,
            )

            prompt = core.build_canonical_prompt(
                PromptAssemblyContext(
                    core_mode="default",
                    turn_kind="service_trigger",
                    metadata={
                        "service_definition": ServiceDefinition(
                            service_id="daily_digest",
                            goal="Summarize repository updates",
                            method="Review recent changes and produce a concise digest.",
                            skill_refs=["git", "summary"],
                        )
                    },
                )
            )

            self.assertEqual(prompt.messages[0]["role"], "system")
            self.assertEqual(prompt.messages[-1]["role"], "user")
            self.assertIn("[Service Trigger]", prompt.messages[-1]["content"])
            self.assertIn("Goal: Summarize repository updates", prompt.messages[-1]["content"])
            self.assertIn("Method: Review recent changes and produce a concise digest.", prompt.messages[-1]["content"])
            self.assertEqual(len(prompt.messages), 2)
            self.assertNotIn("Remember the last digest.", prompt.messages[-1]["content"])
            self.assertNotIn("Recent summaries", "\n".join(message["content"] for message in prompt.messages))
        finally:
            database.close()
            shutil.rmtree(runtime_root, ignore_errors=True)

    def test_service_trigger_executes_turn_and_commits_transcript_to_l1(self) -> None:
        runtime_root, database = self._create_database()
        try:
            core = PalCore()
            register_core_with_core(core)
            register_execution_with_core(core.context)
            identity_service = IdentityService(repository=IdentityRepository())
            identity_service.ensure_defaults()
            register_identity_with_core(core.context, identity_service)
            memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
            register_memory_with_core(core.context, memory_service)
            service_manager = ServiceManager()
            register_service_with_core(core.context, service_manager, None)
            definition = ServiceDefinition(
                service_id="daily_digest",
                goal="Summarize repository updates",
                method="Review recent changes and produce a concise digest.",
                skill_refs=["git", "summary"],
            )
            service_manager.register(definition)
            core.context.port_registry["llm:llm"] = ScriptedLLMRuntime(
                [CanonicalLLMOutcome(text="Daily digest complete.", tool_calls=[], finish_reason="stop")]
            )

            service_manager.enqueue_trigger(ServiceTriggerEvent(service_id="daily_digest", trigger_kind="manual"))
            processed = core.run_until_idle()

            self.assertIn("service.trigger", [item.event_kind for item in processed])
            self.assertEqual(len(memory_service.l1_store.items), 1)
            transcript = memory_service.l1_store.items[0]
            self.assertEqual(transcript[0].role, "user")
            self.assertIn("[Service Trigger]", transcript[0].content)
            self.assertIn("Goal: Summarize repository updates", transcript[0].content)
            self.assertEqual(transcript[1].role, "assistant")
            self.assertEqual(transcript[1].content, "Daily digest complete.")
        finally:
            database.close()
            shutil.rmtree(runtime_root, ignore_errors=True)

    def test_service_trigger_delivers_reply_to_persisted_output_target(self) -> None:
        runtime_root, database = self._create_database()
        try:
            core = PalCore()
            register_core_with_core(core)
            register_execution_with_core(core.context)
            identity_service = IdentityService(repository=IdentityRepository())
            identity_service.ensure_defaults()
            register_identity_with_core(core.context, identity_service)
            channel_runtime = ChannelRuntime()
            register_channel_with_core(core.context, channel_runtime)
            endpoint = StubEndpoint(EndpointConfig(endpoint_id="telegram_main", channel_kind="telegram", binding_key="chat:12345"))
            channel_runtime.register_endpoint(endpoint)
            memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
            register_memory_with_core(core.context, memory_service)
            service_manager = ServiceManager()
            register_service_with_core(core.context, service_manager, None)
            definition = ServiceDefinition(
                service_id="daily_digest",
                goal="Summarize repository updates",
                method="Review recent changes and produce a concise digest.",
                out_channel_id="telegram_main",
                out_reply_target={"chat_id": "12345", "thread_id": "7"},
            )
            service_manager.register(definition)
            core.context.port_registry["llm:llm"] = ScriptedLLMRuntime(
                [CanonicalLLMOutcome(text="Daily digest complete.", tool_calls=[], finish_reason="stop")]
            )

            service_manager.enqueue_trigger(ServiceTriggerEvent(service_id="daily_digest", trigger_kind="scheduled"))
            core.run_until_idle()

            self.assertEqual(endpoint.sent, [("telegram_main", "Daily digest complete.")])
        finally:
            database.close()
            shutil.rmtree(runtime_root, ignore_errors=True)

    def test_service_manager_enqueues_due_scheduled_trigger(self) -> None:
        manager = ServiceManager()
        reference = utc_now_dt()
        definition = ServiceDefinition(
            service_id="heartbeat",
            goal="Check the repository status",
            schedule={
                "cadence": "cron",
                "cron": "* * * * *",
                "timezone": "UTC",
            },
        )
        manager.register(definition)

        due = manager.enqueue_due_triggers(now_utc=reference + timedelta(minutes=2))

        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].service_id, "heartbeat")
        self.assertEqual(due[0].trigger_kind, "scheduled")
        self.assertIn("scheduled_for", due[0].metadata)
        self.assertEqual(len(manager.pending_triggers), 1)
        self.assertIsNotNone(manager.schedule_engine.next_due_at("heartbeat"))

    def test_compute_next_service_run_supports_cron_schedule(self) -> None:
        next_due = compute_next_service_run_at_utc(
            {
                "cadence": "cron",
                "cron": "30 9 * * 5",
                "timezone": "Asia/Shanghai",
            },
            now_utc=datetime(2026, 4, 10, 0, 0, tzinfo=timezone.utc),
        )

        self.assertIsNotNone(next_due)

    def test_main_loop_type_is_available(self) -> None:
        self.assertIs(MainLoop, importlib.import_module("pal.core").MainLoop)

    def test_main_loop_drains_channel_service_and_tasking_sources(self) -> None:
        core = PalCore()
        channel_runtime = ChannelRuntime()
        service_manager = ServiceManager()
        tasking_service = TaskingService()

        register_channel_with_core(core.context, channel_runtime)
        register_service_with_core(core.context, service_manager)
        register_tasking_with_core(core.context, tasking_service)
        register_control_with_core(core.context, ControlPlane())

        channel_runtime.emit(
            ChannelEnvelope(
                event=EventEnvelope(
                    event_kind="slash_command",
                    source_kind="channel",
                    payload={"command": "/pause"},
                ),
                endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
                response_handle=ResponseHandle(endpoint_id="stdio"),
            )
        )
        service_manager.enqueue_trigger(ServiceTriggerEvent(service_id="svc-1", trigger_kind="manual"))
        tasking_service.enqueue_minion_progress(
            MinionProgressEvent(work_order_id="wo-1", summary="started")
        )

        processed = core.run_until_idle()

        processed_kinds = [item.event_kind for item in processed]
        self.assertIn("slash_command", processed_kinds)
        self.assertIn("service.trigger", processed_kinds)
        self.assertIn("minion.progress", processed_kinds)
        self.assertIn("control.action", processed_kinds)

    def test_foundation_modules_do_not_publish_lifecycle_capabilities(self) -> None:
        core = PalCore()
        register_channel_with_core(core.context, ChannelRuntime())

        published = core.publish_module_capabilities("channel")

        self.assertIn("intro_module_channel_list", published)
        self.assertIn("op_channel_mgmt_enable", published)
        self.assertIn("op_channel_mgmt_disable", published)
        self.assertIn("op_channel_mgmt_attach", published)
        self.assertIn("op_channel_mgmt_detach", published)
        self.assertNotIn("operation_channel_lifecycle_attach", published)
        self.assertNotIn("operation_channel_lifecycle_detach", published)
        self.assertNotIn("operation_channel_endpoint_attach", published)
        self.assertNotIn("operation_channel_endpoint_detach", published)
        descriptor = core.context.capability_registry.descriptors["intro_module_channel_list"]
        self.assertEqual(descriptor.display_name, "intro_module_channel_list")
        self.assertEqual(descriptor.target_kind, "module")
        self.assertEqual(descriptor.target_id, SINGLETON_TARGET)
        self.assertEqual(descriptor.target_label, "channel")
        self.assertIn("channel_introspection_list", descriptor.aliases)
        self.assertIn("introspection_module_channel_observe", descriptor.aliases)

    def test_identity_is_always_on_and_query_only(self) -> None:
        runtime_root, database = self._create_database()
        try:
            core = PalCore()
            service = IdentityService(repository=IdentityRepository())
            service.ensure_defaults()
            register_identity_with_core(core.context, service)

            published = core.publish_module_capabilities("identity")

            self.assertIn("intro_module_identity_show", published)
            self.assertNotIn("intro_module_identity_configure", published)
            self.assertNotIn("identity.lifecycle.attach", published)
            self.assertNotIn("identity.lifecycle.detach", published)
            handle = core.context.module_registry.require("identity")
            self.assertEqual(handle.tier, "core-foundation")
            self.assertFalse(handle.supports_lifecycle_capabilities)
        finally:
            database.close()
            shutil.rmtree(runtime_root, ignore_errors=True)

    def test_managed_essential_module_detach_degrades_without_withdrawing_capabilities(self) -> None:
        core = PalCore()
        register_control_with_core(core.context, ControlPlane())
        core.publish_module_capabilities("control")

        result = core.detach_module("control")

        self.assertEqual(result, "ok")
        self.assertIn("intro_module_control_show", core.context.capability_registry.descriptors)
        observed = core.context.execution_runtime.execute(CapabilityCall(name="intro_module_control_show"))
        self.assertTrue(observed.structured["degraded"])

    def test_detachable_module_detach_withdraws_capabilities_and_reattach_restores_them(self) -> None:
        core = PalCore()
        tasking_service = TaskingService()
        register_tasking_with_core(core.context, tasking_service)
        core.publish_module_capabilities("tasking")

        self.assertIn("intro_module_tasking_show", core.context.capability_registry.descriptors)
        self.assertIn("tasking.minion", core.context.event_source_registry.sources)
        self.assertNotIn("tasking.prompt.default", core.context.prompt_fragment_registry.providers)

        detached = core.detach_module("tasking")
        self.assertEqual(detached, "ok")
        self.assertNotIn("intro_module_tasking_show", core.context.capability_registry.descriptors)
        self.assertNotIn("tasking.minion", core.context.event_source_registry.sources)
        self.assertNotIn("tasking.prompt.default", core.context.prompt_fragment_registry.providers)

        reattached = core.reattach_module("tasking")
        self.assertEqual(reattached, "ok")
        self.assertIn("intro_module_tasking_show", core.context.capability_registry.descriptors)
        self.assertIn("tasking.minion", core.context.event_source_registry.sources)
        self.assertNotIn("tasking.prompt.default", core.context.prompt_fragment_registry.providers)
        observed = core.context.execution_runtime.execute(CapabilityCall(name="intro_module_tasking_show"))
        self.assertEqual(observed.status, "ok")

    def test_memory_can_switch_active_l3_provider_via_registered_capability(self) -> None:
        core = PalCore()
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        mock_l3 = MockL3Plugin(records=[{"document_id": "fact:1", "title": "Redis"}])

        register_memory_with_core(core.context, memory_service)
        register_l3_with_core(core.context, mock_l3)
        core.publish_module_capabilities("memory")
        core.publish_module_capabilities(mock_l3.module_id)

        configured = core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_memory_mgmt_set_active_provider",
                args={"active_provider_id": "mock_l3"},
            )
        )
        recall_result = mock_l3.recall(MemoryQuery(level="deep", queries=["redis"]))

        self.assertEqual(configured.status, "ok")
        self.assertEqual(recall_result.hits, [{"document_id": "fact:1", "title": "Redis"}])

    def test_execution_runtime_bootstraps_default_l3_stub(self) -> None:
        core = PalCore()

        self.assertIsNotNone(core.context.execution_runtime.l3_plugin_registry.get("null_l3"))
        self.assertIn("null_l3", core.context.execution_runtime.provider_registry)

    def test_memory_can_fallback_to_stub_provider(self) -> None:
        core = PalCore()
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        mock_l3 = MockL3Plugin(records=[{"document_id": "fact:1", "title": "Redis"}])

        register_memory_with_core(core.context, memory_service)
        register_l3_with_core(core.context, mock_l3)
        core.publish_module_capabilities("memory")
        core.publish_module_capabilities(mock_l3.module_id)

        core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_memory_mgmt_set_active_provider",
                args={"active_provider_id": "mock_l3"},
            )
        )
        fallback = core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_memory_mgmt_set_active_provider",
                args={"active_provider_id": "null_l3"},
            )
        )

        self.assertEqual(fallback.status, "ok")
        self.assertEqual(memory_service.l3_selector.active_provider_id, "null_l3")

    def test_detachable_l3_provider_can_detach_and_reattach(self) -> None:
        core = PalCore()
        mock_l3 = MockL3Plugin(records=[{"document_id": "fact:1", "title": "Redis"}])
        register_l3_with_core(core.context, mock_l3)
        core.publish_module_capabilities(mock_l3.module_id)

        detached = core.detach_module(mock_l3.module_id)

        self.assertEqual(detached, "ok")
        self.assertIsNone(core.context.execution_runtime.l3_plugin_registry.get("mock_l3"))
        self.assertNotIn("intro_provider_l3_show::mock_l3", core.context.capability_registry.descriptors)

        reattached = core.reattach_module(mock_l3.module_id)

        self.assertEqual(reattached, "ok")
        self.assertIsNotNone(core.context.execution_runtime.l3_plugin_registry.get("mock_l3"))

    def test_l3_recall_query_supports_summary_and_origin_views(self) -> None:
        core = PalCore()
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        register_memory_with_core(core.context, memory_service)
        mock_l3 = MockL3Plugin(
            records=[
                {
                    "document_id": "fact:1",
                    "document_kind": "fact",
                    "scope": "system",
                    "title": "Nathan profile",
                    "summary": "Nathan built Pal and wants it to act directly.",
                    "rendered": "Nathan built Pal and wants it to act directly.",
                    "search_text": "Nathan built Pal after being unhappy with OpenClaw and wants Pal to act directly for him.",
                    "canonical_key": "nathan_profile",
                    "dedupe_fingerprint": "fp_1",
                }
            ]
        )
        register_l3_with_core(core.context, mock_l3)
        core.publish_module_capabilities(mock_l3.module_id)

        summary_result = core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_l3_recall_query",
                args={"target_id": "mock_l3", "queries": ["nathan"], "view": "summary"},
            )
        )
        origin_result = core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_l3_recall_query",
                args={"target_id": "mock_l3", "queries": ["nathan"], "view": "origin"},
            )
        )

        self.assertEqual(summary_result.status, "ok")
        self.assertEqual(summary_result.structured["view"], "summary")
        self.assertEqual(summary_result.structured["hit_count"], 1)
        self.assertEqual(summary_result.structured["hits_preview"][0]["document_id"], "fact:1")
        self.assertNotIn("hits", summary_result.structured)
        self.assertNotIn("projected_entries", summary_result.structured)
        self.assertIn("Nathan built Pal and wants it to act directly.", summary_result.llm_text)
        self.assertNotIn("projected_entries", summary_result.llm_text)
        self.assertNotIn("OpenClaw", summary_result.llm_text)

        self.assertEqual(origin_result.status, "ok")
        self.assertEqual(origin_result.structured["view"], "origin")
        self.assertEqual(origin_result.structured["hit_count"], 1)
        self.assertNotIn("hits", origin_result.structured)
        self.assertNotIn("projected_entries", origin_result.structured)
        self.assertIn("OpenClaw", origin_result.llm_text)
        self.assertNotIn("projected_entries", origin_result.llm_text)
        self.assertIn("intro_provider_l3_show::mock_l3", core.context.capability_registry.descriptors)

    def test_detachable_service_module_round_trips_through_core_registry(self) -> None:
        core = PalCore()
        manager = ServiceManager()
        register_service_with_core(core.context, manager)
        core.publish_module_capabilities("service")

        self.assertIn("intro_module_service_show", core.context.capability_registry.descriptors)
        self.assertIn("intro_module_service_list", core.context.capability_registry.descriptors)
        self.assertIn("op_service_mgmt_create", core.context.capability_registry.descriptors)
        self.assertIn("op_service_mgmt_destroy", core.context.capability_registry.descriptors)
        self.assertIn("op_service_mgmt_enable", core.context.capability_registry.descriptors)
        self.assertIn("op_service_mgmt_disable", core.context.capability_registry.descriptors)
        self.assertIn("op_service_mgmt_set_output_channel", core.context.capability_registry.descriptors)
        self.assertIn("op_service_mgmt_set_output_target", core.context.capability_registry.descriptors)
        self.assertIn("op_service_mgmt_update_schedule", core.context.capability_registry.descriptors)
        self.assertIn("op_service_lifecycle_attach", core.context.capability_registry.descriptors)
        self.assertIn("op_service_lifecycle_detach", core.context.capability_registry.descriptors)
        self.assertIn("service.triggers", core.context.event_source_registry.sources)

    def test_service_management_capabilities_create_update_and_destroy(self) -> None:
        runtime_root, database = self._create_database()
        try:
            core = PalCore()
            register_service_with_core(core.context, ServiceManager())
            core.publish_module_capabilities("service")

            created = core.context.execution_runtime.execute(
                CapabilityCall(
                    name="op_service_mgmt_create",
                    args={
                        "service_id": "daily_digest",
                        "goal": "Summarize repository updates",
                        "method": "Review recent changes and produce a concise digest.",
                        "skill_refs": ["git", "summary"],
                        "out_channel_id": "socket_default",
                        "out_reply_target": {"session_id": "session-1", "request_id": "req-1"},
                        "schedule": {"cadence": "daily", "hour": 9, "minute": 0, "timezone": "Asia/Shanghai"},
                    },
                )
            )
            self.assertEqual(created.status, "ok")

            listed = core.context.execution_runtime.execute(CapabilityCall(name="intro_module_service_list"))
            self.assertEqual(listed.status, "ok")
            self.assertEqual(len(listed.structured["items"]), 1)
            self.assertEqual(listed.structured["items"][0]["service_id"], "daily_digest")
            self.assertEqual(listed.structured["items"][0]["out_channel_id"], "socket_default")
            self.assertEqual(listed.structured["items"][0]["out_reply_target"], {"session_id": "session-1", "request_id": "req-1"})

            changed_channel = core.context.execution_runtime.execute(
                CapabilityCall(
                    name="op_service_mgmt_set_output_channel",
                    args={"target_id": "daily_digest", "out_channel_id": "telegram_main"},
                )
            )
            self.assertEqual(changed_channel.status, "ok")
            self.assertEqual(changed_channel.structured["out_channel_id"], "telegram_main")
            self.assertEqual(changed_channel.structured["out_reply_target"], {"session_id": "session-1", "request_id": "req-1"})

            changed_target = core.context.execution_runtime.execute(
                CapabilityCall(
                    name="op_service_mgmt_set_output_target",
                    args={"target_id": "daily_digest", "out_reply_target": {"chat_id": "12345", "thread_id": "7"}},
                )
            )
            self.assertEqual(changed_target.status, "ok")
            self.assertEqual(changed_target.structured["out_reply_target"], {"chat_id": "12345", "thread_id": "7"})

            rescheduled = core.context.execution_runtime.execute(
                CapabilityCall(
                    name="op_service_mgmt_update_schedule",
                    args={
                        "target_id": "daily_digest",
                        "schedule": {"cadence": "daily", "hour": 10, "minute": 15, "timezone": "Asia/Shanghai"},
                    },
                )
            )
            self.assertEqual(rescheduled.status, "ok")
            self.assertIn("next_due_at", rescheduled.structured)

            disabled = core.context.execution_runtime.execute(
                CapabilityCall(name="op_service_mgmt_disable", args={"target_id": "daily_digest"})
            )
            self.assertEqual(disabled.status, "ok")
            self.assertFalse(disabled.structured["enabled"])

            enabled = core.context.execution_runtime.execute(
                CapabilityCall(name="op_service_mgmt_enable", args={"target_id": "daily_digest"})
            )
            self.assertEqual(enabled.status, "ok")
            self.assertTrue(enabled.structured["enabled"])

            destroyed = core.context.execution_runtime.execute(
                CapabilityCall(name="op_service_mgmt_destroy", args={"target_id": "daily_digest"})
            )
            self.assertEqual(destroyed.status, "ok")
            after_destroy = core.context.execution_runtime.execute(CapabilityCall(name="intro_module_service_list"))
            self.assertEqual(after_destroy.structured["items"], [])
        finally:
            database.close()
            shutil.rmtree(runtime_root, ignore_errors=True)

    def test_service_instance_introspection_exposes_show_and_run_history(self) -> None:
        runtime_root, database = self._create_database()
        try:
            core = PalCore()
            manager = ServiceManager(repository=importlib.import_module("pal.service").ServiceRepository())
            runner = importlib.import_module("pal.service").ServiceRunner(repository=manager.repository)
            register_service_with_core(core.context, manager, runner)
            core.publish_module_capabilities("service")

            manager.create_service(
                service_id="daily_digest",
                goal="Summarize repository updates",
                method="Review recent changes.",
                out_channel_id="socket_default",
                schedule={"cadence": "daily", "hour": 9, "minute": 0, "timezone": "Asia/Shanghai"},
            )
            run_id = runner.begin_run(ServiceTriggerEvent(service_id="daily_digest", trigger_kind="manual"))
            runner.complete_run(run_id, turn_id="turn-123", final_reply="Digest sent.")

            self.assertIn("intro_service_show::daily_digest", core.context.capability_registry.descriptors)
            self.assertIn("intro_service_last_run::daily_digest", core.context.capability_registry.descriptors)
            self.assertIn("intro_service_list_runs::daily_digest", core.context.capability_registry.descriptors)

            shown = core.context.execution_runtime.execute(
                CapabilityCall(name="intro_service_show", args={"target_id": "daily_digest"})
            )
            self.assertEqual(shown.status, "ok")
            self.assertEqual(shown.structured["service_id"], "daily_digest")
            self.assertEqual(shown.structured["out_channel_id"], "socket_default")

            latest = core.context.execution_runtime.execute(
                CapabilityCall(name="intro_service_last_run", args={"target_id": "daily_digest"})
            )
            self.assertEqual(latest.status, "ok")
            self.assertEqual(latest.structured["run"]["turn_id"], "turn-123")
            self.assertEqual(latest.structured["run"]["output_summary"], "Digest sent.")

            history = core.context.execution_runtime.execute(
                CapabilityCall(name="intro_service_list_runs", args={"target_id": "daily_digest", "limit": 5})
            )
            self.assertEqual(history.status, "ok")
            self.assertEqual(len(history.structured["items"]), 1)
            self.assertEqual(history.structured["items"][0]["service_run_id"], run_id)
        finally:
            database.close()
            shutil.rmtree(runtime_root, ignore_errors=True)

    def test_instance_level_capability_requires_target_id_and_schema_injects_it(self) -> None:
        runtime_root, database = self._create_database()
        try:
            core = PalCore()
            channel_runtime = ChannelRuntime()
            repository = importlib.import_module("pal.channel.repository").ChannelEndpointRepository()
            repository.upsert(
                endpoint_id="telegram_main",
                channel_kind="telegram",
                binding_key="chat:1",
                enabled=True,
            )
            register_channel_with_core(core.context, channel_runtime)
            core.publish_module_capabilities("channel")

            descriptor = core.context.capability_registry.descriptors["intro_endpoint_channel_inspect::telegram_main"]
            target_schema = descriptor.parameters_schema["properties"]["target_id"]

            self.assertEqual(target_schema["enum"], ["telegram_main"])
            self.assertIn("target_id", descriptor.parameters_schema["required"])

            missing_target = core.context.execution_runtime.execute(
                CapabilityCall(name="intro_endpoint_channel_inspect")
            )
            self.assertEqual(missing_target.status, "invalid")
            self.assertEqual(missing_target.structured["available_target_ids"], ["telegram_main"])

            resolved = core.context.execution_runtime.execute(
                CapabilityCall(
                    name="intro_endpoint_channel_inspect",
                    args={"target_id": "telegram_main"},
                )
            )
            self.assertEqual(resolved.status, "ok")
            self.assertEqual(resolved.structured["endpoint_id"], "telegram_main")
        finally:
            database.close()
            shutil.rmtree(runtime_root, ignore_errors=True)

    def test_wizard_is_not_registered_or_governed_by_pal_core(self) -> None:
        core = PalCore()

        self.assertIsNone(core.context.module_registry.get("wizard"))
        with self.assertRaises(KeyError):
            core.detach_module("wizard")

    def test_channel_outbox_emits_delivery_events_without_blocking_turn(self) -> None:
        core = PalCore()
        adapter = RecordingAdapter()
        channel_runtime = ChannelRuntime()
        channel_runtime.adapter_registry.register(adapter)
        register_channel_with_core(core.context, channel_runtime)

        envelope = ChannelEnvelope(
            event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "hello"}),
            endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
            response_handle=ResponseHandle(endpoint_id="stdio"),
        )
        reply_id = channel_runtime.queue_reply(envelope, "world")
        delivered = ChannelEventSource(runtime=channel_runtime).drain(core.context)

        self.assertEqual(reply_id != "", True)
        self.assertEqual(adapter.sent, [("stdio", "world")])
        self.assertEqual([item.event_kind for item in delivered], ["reply.delivered"])

    def test_channel_endpoint_queue_base_handles_pairing_mailbox_and_outbox(self) -> None:
        endpoint = StubEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="telegram_main",
                channel_kind="telegram",
                binding_key="chat:1",
            )
        )

        endpoint.pair(binding_key="chat:2", pairing_metadata={"authorized": True})
        self.assertTrue(endpoint.paired)
        self.assertEqual(endpoint.endpoint.binding_key, "chat:2")
        self.assertTrue(endpoint.pairing_metadata["authorized"])

        envelope = endpoint.accept_raw({"text": "hello"}, event_kind="user.message", reply_target={"chat_id": 1})
        self.assertIsNotNone(envelope)
        self.assertTrue(endpoint.has_pending())
        drained = endpoint.poll()
        self.assertEqual(len(drained), 1)
        self.assertEqual(drained[0].event.payload, {"normalized": {"text": "hello"}})
        self.assertEqual(drained[0].response_handle.reply_target, {"chat_id": 1})

        endpoint.disable()
        skipped = endpoint.accept_raw({"text": "blocked"}, event_kind="user.message")
        self.assertIsNone(skipped)

        endpoint.enable()
        reply_id = endpoint.queue_reply("world")
        self.assertTrue(reply_id)
        self.assertTrue(endpoint.has_queued_replies())
        events = endpoint.flush_outbox()
        self.assertEqual(endpoint.sent, [("telegram_main", "world")])
        self.assertEqual([item.event_kind for item in events], ["reply.delivered"])

    def test_channel_endpoint_capabilities_cover_auth_health_and_backlog_without_attach(self) -> None:
        runtime_root, database = self._create_database()
        try:
            core = PalCore()
            channel_runtime = ChannelRuntime()
            repository = importlib.import_module("pal.channel.repository").ChannelEndpointRepository()
            repository.upsert(
                endpoint_id="telegram_main",
                channel_kind="telegram",
                binding_key="chat:1",
                enabled=True,
            )
            endpoint = StubEndpoint(
                endpoint=EndpointConfig(endpoint_id="telegram_main", channel_kind="telegram", binding_key="chat:1")
            )
            channel_runtime.register_endpoint(endpoint)
            register_channel_with_core(core.context, channel_runtime)

            published = core.publish_module_capabilities("channel")

            self.assertIn("intro_endpoint_channel_inspect::telegram_main", published)
            self.assertIn("intro_endpoint_channel_auth_state::telegram_main", published)
            self.assertIn("op_channel_endpoint_set_auth_material::telegram_main", published)
            self.assertIn("intro_endpoint_channel_backlog::telegram_main", published)
            self.assertIn("intro_endpoint_channel_health::telegram_main", published)
            self.assertNotIn("op_channel_endpoint_attach::telegram_main", published)
            self.assertNotIn("op_channel_endpoint_detach::telegram_main", published)

            configured = core.context.execution_runtime.execute(
                CapabilityCall(
                    name="op_channel_endpoint_set_auth_material",
                    args={"target_id": "telegram_main", "material": {"bot_token": "secret-token", "authorized": True}},
                )
            )
            self.assertEqual(configured.status, "ok")
            self.assertNotIn("token", configured.structured)
            self.assertEqual(configured.structured["accepted_keys"], ["authorized", "bot_token"])

            auth_state = core.context.execution_runtime.execute(
                CapabilityCall(
                    name="intro_endpoint_channel_auth_state",
                    args={"target_id": "telegram_main"},
                )
            )
            self.assertEqual(auth_state.status, "ok")
            self.assertNotIn("token", auth_state.structured)
            self.assertTrue(auth_state.structured["authorized"])

            health = core.context.execution_runtime.execute(
                CapabilityCall(
                    name="intro_endpoint_channel_health",
                    args={"target_id": "telegram_main"},
                )
            )
            self.assertEqual(health.status, "ok")
            self.assertNotIn("token", health.structured)
            self.assertTrue(health.structured["healthy"])

            backlog = core.context.execution_runtime.execute(
                CapabilityCall(
                    name="intro_endpoint_channel_backlog",
                    args={"target_id": "telegram_main"},
                )
            )
            self.assertEqual(backlog.status, "ok")
            self.assertIn("outbox_size", backlog.structured)
        finally:
            database.close()
            shutil.rmtree(runtime_root, ignore_errors=True)

    def test_only_channel_management_can_change_endpoint_attach_state(self) -> None:
        runtime_root, database = self._create_database()
        try:
            core = PalCore()
            channel_runtime = ChannelRuntime()
            repository = importlib.import_module("pal.channel.repository").ChannelEndpointRepository()
            repository.upsert(
                endpoint_id="telegram_main",
                channel_kind="telegram",
                binding_key="chat:1",
                enabled=True,
            )
            endpoint = StubEndpoint(
                endpoint=EndpointConfig(endpoint_id="telegram_main", channel_kind="telegram", binding_key="chat:1")
            )
            channel_runtime.register_endpoint(endpoint)
            register_channel_with_core(core.context, channel_runtime)
            core.publish_module_capabilities("channel")

            detached = core.context.execution_runtime.execute(
                CapabilityCall(name="op_channel_mgmt_detach", args={"target_id": "telegram_main"})
            )
            self.assertEqual(detached.status, "ok")
            self.assertFalse(endpoint.attached)

            blocked = endpoint.accept_raw({"text": "hello"}, event_kind="user.message")
            self.assertIsNone(blocked)

            attached = core.context.execution_runtime.execute(
                CapabilityCall(name="op_channel_mgmt_attach", args={"target_id": "telegram_main"})
            )
            self.assertEqual(attached.status, "ok")
            self.assertTrue(endpoint.attached)

            accepted = endpoint.accept_raw({"text": "hello"}, event_kind="user.message")
            self.assertIsNotNone(accepted)
        finally:
            database.close()
            shutil.rmtree(runtime_root, ignore_errors=True)

    def test_turn_runtime_queues_reply_and_commits_l1(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        channel_runtime = ChannelRuntime()
        register_channel_with_core(core.context, channel_runtime)
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        register_memory_with_core(core.context, memory_service)
        core.context.port_registry["llm:llm"] = ScriptedLLMRuntime(
            [CanonicalLLMOutcome(text="final answer", tool_calls=[], finish_reason="stop")]
        )

        outcome = core.process_channel_turn(
            ChannelEnvelope(
                event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "hello"}),
                endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
                response_handle=ResponseHandle(endpoint_id="stdio"),
            )
        )

        self.assertEqual(outcome.final_reply, "final answer")
        self.assertEqual(len(channel_runtime.outbox), 1)
        self.assertEqual(
            memory_service.l1_store.items,
            [[
                L1TranscriptMessage(role="user", content="hello"),
                L1TranscriptMessage(role="assistant", content="final answer"),
            ]],
        )

    def test_turn_runtime_shows_reasoning_context_on_stdio(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        channel_runtime = ChannelRuntime()
        register_channel_with_core(core.context, channel_runtime)
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        register_memory_with_core(core.context, memory_service)
        core.context.port_registry["llm:llm"] = ScriptedLLMRuntime(
            [CanonicalLLMOutcome(text="final answer", reasoning_text="thinking here", tool_calls=[], finish_reason="stop")]
        )

        outcome = core.process_channel_turn(
            ChannelEnvelope(
                event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "hello"}),
                endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
                response_handle=ResponseHandle(endpoint_id="stdio"),
            )
        )

        self.assertIn("[thinking]", outcome.final_reply)
        self.assertIn("thinking here", outcome.final_reply)
        self.assertTrue(outcome.final_reply.endswith("final answer"))

    def test_turn_runtime_streams_events_through_stdio_endpoint_while_core_executes_tools(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        channel_runtime = ChannelRuntime()
        endpoint = StubEndpoint(
            endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin")
        )
        channel_runtime.register_endpoint(endpoint)
        register_channel_with_core(core.context, channel_runtime)
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        register_memory_with_core(core.context, memory_service)
        core.context.execution_runtime.register_tool(EchoTool())
        core.context.port_registry["llm:llm"] = ScriptedLLMRuntime(
            [
                CanonicalLLMOutcome(
                    text="",
                    reasoning_text="thinking",
                    tool_calls=[CanonicalToolCall(name="echo", args={"value": "same"})],
                    finish_reason="tool_calls",
                ),
                CanonicalLLMOutcome(text="final answer", tool_calls=[], finish_reason="stop"),
            ]
        )

        outcome = core.process_channel_turn(
            ChannelEnvelope(
                event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "hello"}),
                endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
                response_handle=ResponseHandle(endpoint_id="stdio"),
            )
        )

        channel_runtime.sync_endpoints()
        self.assertIn(("stdio", "reasoning", "thinking"), endpoint.streamed)
        self.assertIn(("stdio", "tool_call", "echo"), endpoint.streamed)
        self.assertIn("final answer", outcome.final_reply)

    def test_turn_runtime_uses_response_mode_to_coarsely_tune_temperature(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        channel_runtime = ChannelRuntime()
        register_channel_with_core(core.context, channel_runtime)
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        register_memory_with_core(core.context, memory_service)
        scripted_llm = ScriptedLLMRuntime(
            [
                CanonicalLLMOutcome(
                    text="",
                    tool_calls=[CanonicalToolCall(name="echo", args={"value": "mode"})],
                    finish_reason="tool_calls",
                    response_mode="review",
                ),
                CanonicalLLMOutcome(text="final answer", tool_calls=[], finish_reason="stop"),
            ]
        )
        core.context.port_registry["llm:llm"] = scripted_llm
        core.context.execution_runtime.register_tool(EchoTool())

        core.process_channel_turn(
            ChannelEnvelope(
                event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "hello"}),
                endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
                response_handle=ResponseHandle(endpoint_id="stdio"),
            )
        )

        generate_requests = [request for kind, request in scripted_llm.requests if kind in {"generate", "generate_stream"}]
        self.assertEqual(generate_requests[0].temperature, 0.7)
        self.assertEqual(generate_requests[1].temperature, 0.1)

    def test_tool_results_are_returned_via_standard_assistant_and_tool_messages(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        channel_runtime = ChannelRuntime()
        register_channel_with_core(core.context, channel_runtime)
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        register_memory_with_core(core.context, memory_service)
        scripted_llm = ScriptedLLMRuntime(
            [
                CanonicalLLMOutcome(
                    text="",
                    tool_calls=[CanonicalToolCall(name="echo", args={"value": "proto"})],
                    finish_reason="tool_calls",
                ),
                CanonicalLLMOutcome(text="final answer", tool_calls=[], finish_reason="stop"),
            ]
        )
        core.context.port_registry["llm:llm"] = scripted_llm
        core.context.execution_runtime.register_tool(EchoTool())

        core.process_channel_turn(
            ChannelEnvelope(
                event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "hello"}),
                endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
                response_handle=ResponseHandle(endpoint_id="stdio"),
            )
        )

        generate_requests = [request for kind, request in scripted_llm.requests if kind in {"generate", "generate_stream"}]
        self.assertGreaterEqual(len(generate_requests), 2)
        followup_messages = generate_requests[1].messages
        assistant_tool_message = next(
            message for message in followup_messages if message.get("role") == "assistant" and message.get("tool_calls")
        )
        self.assertEqual(
            assistant_tool_message["tool_calls"][0]["function"]["name"],
            "echo",
        )
        self.assertEqual(
            ast.literal_eval(assistant_tool_message["tool_calls"][0]["function"]["arguments"]),
            {"value": "proto"},
        )
        tool_message = next(message for message in followup_messages if message.get("role") == "tool")
        self.assertIn("stable-result", tool_message["content"])
        system_message = next(message for message in followup_messages if message.get("role") == "system")
        self.assertNotIn("Tool Observation", system_message["content"])

    def test_tool_protocol_prefers_llm_text_over_summary_text(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        channel_runtime = ChannelRuntime()
        register_channel_with_core(core.context, channel_runtime)
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        register_memory_with_core(core.context, memory_service)
        scripted_llm = ScriptedLLMRuntime(
            [
                CanonicalLLMOutcome(
                    text="",
                    tool_calls=[CanonicalToolCall(name="verbose", args={"value": "proto"})],
                    finish_reason="tool_calls",
                ),
                CanonicalLLMOutcome(text="final answer", tool_calls=[], finish_reason="stop"),
            ]
        )
        core.context.port_registry["llm:llm"] = scripted_llm
        core.context.execution_runtime.register_tool(VerboseTool())

        core.process_channel_turn(
            ChannelEnvelope(
                event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "hello"}),
                endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
                response_handle=ResponseHandle(endpoint_id="stdio"),
            )
        )

        generate_requests = [request for kind, request in scripted_llm.requests if kind in {"generate", "generate_stream"}]
        self.assertGreaterEqual(len(generate_requests), 2)
        followup_messages = generate_requests[1].messages
        tool_message = next(message for message in followup_messages if message.get("role") == "tool")
        self.assertIn('"value": "rich"', tool_message["content"])
        self.assertNotIn("placeholder", tool_message["content"])

    def test_l3_recall_tool_protocol_uses_minimal_observation_text(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        channel_runtime = ChannelRuntime()
        register_channel_with_core(core.context, channel_runtime)
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        register_memory_with_core(core.context, memory_service)
        mock_l3 = MockL3Plugin(
            records=[
                {
                    "document_id": "fact:1",
                    "document_kind": "fact",
                    "scope": "system",
                    "title": "Nathan profile",
                    "summary": "Nathan built Pal and wants it to act directly.",
                    "rendered": "Nathan built Pal and wants it to act directly.",
                    "search_text": "Nathan built Pal after being unhappy with OpenClaw and wants Pal to act directly for him.",
                }
            ]
        )
        register_l3_with_core(core.context, mock_l3)
        core.publish_module_capabilities(mock_l3.module_id)
        scripted_llm = ScriptedLLMRuntime(
            [
                CanonicalLLMOutcome(
                    text="",
                    tool_calls=[
                        CanonicalToolCall(
                            name="op_l3_recall_query",
                            args={"target_id": "mock_l3", "queries": ["nathan"], "view": "summary"},
                        )
                    ],
                    finish_reason="tool_calls",
                ),
                CanonicalLLMOutcome(text="final answer", tool_calls=[], finish_reason="stop"),
            ]
        )
        core.context.port_registry["llm:llm"] = scripted_llm

        core.process_channel_turn(
            ChannelEnvelope(
                event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "who am i"}),
                endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
                response_handle=ResponseHandle(endpoint_id="stdio"),
            )
        )

        generate_requests = [request for kind, request in scripted_llm.requests if kind in {"generate", "generate_stream"}]
        self.assertGreaterEqual(len(generate_requests), 2)
        tool_message = next(message for message in generate_requests[1].messages if message.get("role") == "tool")
        self.assertIn("L3 recall completed.", tool_message["content"])
        self.assertIn("provider: mock_l3", tool_message["content"])
        self.assertIn("queries: nathan", tool_message["content"])
        self.assertIn("retrieved: 1 memories", tool_message["content"])
        self.assertNotIn("OpenClaw", tool_message["content"])
        self.assertNotIn("Nathan built Pal and wants it to act directly.", tool_message["content"])

    def test_malformed_tool_result_does_not_crash_turn_runtime(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        channel_runtime = ChannelRuntime()
        register_channel_with_core(core.context, channel_runtime)
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        register_memory_with_core(core.context, memory_service)
        scripted_llm = ScriptedLLMRuntime(
            [
                CanonicalLLMOutcome(
                    text="",
                    tool_calls=[CanonicalToolCall(name="malformed", args={})],
                    finish_reason="tool_calls",
                ),
                CanonicalLLMOutcome(text="final answer", tool_calls=[], finish_reason="stop"),
            ]
        )
        core.context.port_registry["llm:llm"] = scripted_llm
        core.context.execution_runtime.register_tool(MalformedTool())

        outcome = core.process_channel_turn(
            ChannelEnvelope(
                event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "hello"}),
                endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
                response_handle=ResponseHandle(endpoint_id="stdio"),
            )
        )

        self.assertTrue(str(outcome.final_reply or "").strip())
        generate_requests = [request for kind, request in scripted_llm.requests if kind in {"generate", "generate_stream"}]
        self.assertGreaterEqual(len(generate_requests), 2)
        tool_message = next(
            message
            for request in reversed(generate_requests)
            for message in request.messages
            if message.get("role") == "tool"
        )
        self.assertIn("tool execution failed: ValueError", tool_message["content"])

    def test_stagnation_guard_forces_finalization_only_and_strips_tools(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        channel_runtime = ChannelRuntime()
        register_channel_with_core(core.context, channel_runtime)
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        register_memory_with_core(core.context, memory_service)
        scripted_llm = ScriptedLLMRuntime(
            [
                CanonicalLLMOutcome(text="", tool_calls=[CanonicalToolCall(name="echo", args={"value": "same"})], finish_reason="tool_calls"),
                CanonicalLLMOutcome(text="", tool_calls=[CanonicalToolCall(name="echo", args={"value": "same"})], finish_reason="tool_calls"),
                CanonicalLLMOutcome(text="", tool_calls=[CanonicalToolCall(name="echo", args={"value": "same"})], finish_reason="tool_calls"),
                CanonicalLLMOutcome(text="", tool_calls=[CanonicalToolCall(name="echo", args={"value": "same"})], finish_reason="tool_calls"),
            ]
        )
        core.context.port_registry["llm:llm"] = scripted_llm
        core.context.execution_runtime.register_tool(EchoTool())

        outcome = core.process_channel_turn(
            ChannelEnvelope(
                event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "loop"}),
                endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
                response_handle=ResponseHandle(endpoint_id="stdio"),
            )
        )

        generate_requests = [request for kind, request in scripted_llm.requests if kind in {"generate", "generate_stream"}]
        self.assertEqual(generate_requests[-1].tools, [])
        self.assertIn("Finalization Directive", generate_requests[-1].messages[0]["content"])
        self.assertLess(
            generate_requests[-1].messages[0]["content"].index("## Operating Rules"),
            generate_requests[-1].messages[0]["content"].index("## Runtime Overlay"),
        )
        self.assertIn("stopped the tool loop", outcome.final_reply.lower())

    def test_turn_runtime_truncates_large_tool_results_without_forcing_finalization(self) -> None:
        class TinyBudgetLLMRuntime:
            def __init__(self) -> None:
                self.requests = []
                self.generate_count = 0

            def preflight(self, request) -> LLMPreflightAdvice:
                self.requests.append(("preflight", request))
                return LLMPreflightAdvice(
                    status="ready",
                    active_model="tiny-model",
                    fallback_chain=[],
                    target_input_budget=512,
                    reserved_output_tokens=request.max_output_tokens,
                )

            def resolve_endpoint_facts(self, *, preferred_endpoint_id: str | None = None) -> dict[str, object]:
                _ = preferred_endpoint_id
                return {
                    "endpoint_id": "tiny-endpoint",
                    "model_id": "tiny-model",
                    "context_window": 1400,
                    "max_output_tokens": 128,
                }

            def resolve_max_output_tokens(self, *, preferred_endpoint_id: str | None = None) -> int:
                _ = preferred_endpoint_id
                return 128

            def generate(self, request):
                self.requests.append(("generate", request))
                self.generate_count += 1
                if self.generate_count == 1:
                    return CanonicalLLMOutcome(
                        text="",
                        tool_calls=[CanonicalToolCall(name="huge", args={"value": "budget"})],
                        finish_reason="tool_calls",
                    )
                return CanonicalLLMOutcome(text="final answer", tool_calls=[], finish_reason="stop")

        core = PalCore()
        register_core_with_core(core)
        channel_runtime = ChannelRuntime()
        register_channel_with_core(core.context, channel_runtime)
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        register_memory_with_core(core.context, memory_service)
        scripted_llm = TinyBudgetLLMRuntime()
        core.context.port_registry["llm:llm"] = scripted_llm
        with tempfile.TemporaryDirectory() as tmpdir:
            core.context.execution_runtime.runtime_root = Path(tmpdir)
            core.context.execution_runtime.register_tool(HugeTool(size=80_000))

            outcome = core.process_channel_turn(
                ChannelEnvelope(
                    event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "hello"}),
                    endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
                    response_handle=ResponseHandle(endpoint_id="stdio"),
                )
        )

        self.assertEqual(outcome.final_reply, "final answer")
        generate_requests = [request for kind, request in scripted_llm.requests if kind == "generate"]
        self.assertNotIn("Finalization Directive", generate_requests[-1].messages[0]["content"])
        tool_message = next(message for message in generate_requests[-1].messages if message.get("role") == "tool")
        self.assertIn("[truncated:", tool_message["content"])
        self.assertIn("[artifact:", tool_message["content"])

    def test_turn_runtime_degrades_older_tool_results_when_current_turn_group_exceeds_limit(self) -> None:
        class MultiToolLLMRuntime:
            def __init__(self) -> None:
                self.requests = []
                self.generate_count = 0

            def preflight(self, request) -> LLMPreflightAdvice:
                self.requests.append(("preflight", request))
                return LLMPreflightAdvice(
                    status="ready",
                    active_model=request.model_hint or "primary-model",
                    fallback_chain=[],
                    target_input_budget=2048,
                    reserved_output_tokens=request.max_output_tokens,
                )

            def generate(self, request):
                self.requests.append(("generate", request))
                self.generate_count += 1
                if self.generate_count == 1:
                    return CanonicalLLMOutcome(
                        text="",
                        tool_calls=[CanonicalToolCall(name="huge_a", args={"value": "proto-a"})],
                        finish_reason="tool_calls",
                    )
                if self.generate_count == 2:
                    return CanonicalLLMOutcome(
                        text="",
                        tool_calls=[CanonicalToolCall(name="huge_b", args={"value": "proto-b"})],
                        finish_reason="tool_calls",
                    )
                return CanonicalLLMOutcome(text="final answer", tool_calls=[], finish_reason="stop")

        core = PalCore(config=RuntimeConfig(
            max_tool_results_per_message_chars=20_000,
            default_max_result_size_chars=120_000,
            active_tool_result_preview=400,
        ))
        register_core_with_core(core)
        channel_runtime = ChannelRuntime()
        register_channel_with_core(core.context, channel_runtime)
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        register_memory_with_core(core.context, memory_service)
        scripted_llm = MultiToolLLMRuntime()
        core.context.port_registry["llm:llm"] = scripted_llm
        core.context.execution_runtime.register_tool(type("HugeATool", (HugeTool,), {"name": "huge_a"})(size=12_000))
        core.context.execution_runtime.register_tool(type("HugeBTool", (HugeTool,), {"name": "huge_b"})(size=12_000))

        outcome = core.process_channel_turn(
            ChannelEnvelope(
                event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "hello"}),
                endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
                response_handle=ResponseHandle(endpoint_id="stdio"),
            )
        )

        self.assertEqual(outcome.final_reply, "final answer")
        generate_requests = [request for kind, request in scripted_llm.requests if kind == "generate"]
        self.assertGreaterEqual(len(generate_requests), 3)
        retry_request = generate_requests[-1]
        tool_messages = [message for message in retry_request.messages if message.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 2)
        self.assertIn("[preview only:", tool_messages[0]["content"])
        self.assertNotIn("[preview only:", tool_messages[1]["content"])

    def test_execution_runtime_spills_large_tool_results_to_artifacts(self) -> None:
        core = PalCore()
        with tempfile.TemporaryDirectory() as tmpdir:
            core.context.execution_runtime.runtime_root = Path(tmpdir)
            core.context.execution_runtime.register_tool(HugeTool(size=60_000))
            result = core.context.execution_runtime.execute_tool(
                CanonicalToolCall(name="huge", args={"value": "spill"}),
                budget=ToolCallBudget(
                    max_output_chars=10_000,
                    max_output_tokens_estimate=25_000,
                    max_result_spill_chars=50_000,
                    preview_chars=500,
                    artifact_bucket_id="turn_spill",
                ),
            )

            self.assertTrue(result.ok)
            self.assertTrue(result.structured["truncated"])
            artifact_ref = result.structured["artifact_ref"]
            self.assertIsNotNone(artifact_ref)
            self.assertTrue(Path(str(artifact_ref["local_cached_path"])).exists())
            self.assertIn("[truncated:", result.llm_text)

    def test_turn_runtime_recompacts_when_generate_requests_budget_for_fallback_endpoint(self) -> None:
        class FallbackBudgetLLMRuntime:
            def __init__(self) -> None:
                self.requests = []
                self.generate_count = 0

            def preflight(self, request) -> LLMPreflightAdvice:
                self.requests.append(("preflight", request))
                return LLMPreflightAdvice(
                    status="ready",
                    active_model=request.model_hint or "stub-model",
                    fallback_chain=[],
                    target_input_budget=2048,
                    reserved_output_tokens=request.max_output_tokens,
                )

            def generate(self, request):
                self.requests.append(("generate", request))
                self.generate_count += 1
                if self.generate_count == 1:
                    return CanonicalLLMOutcome(
                        text="",
                        tool_calls=[],
                        finish_reason="compact_required",
                        target_input_budget=256,
                        reserved_output_tokens=64,
                        preferred_endpoint_id="fallback-small",
                        preferred_model_id="fallback-small-model",
                    )
                return CanonicalLLMOutcome(text="final answer", tool_calls=[], finish_reason="stop")

        core = PalCore()
        register_core_with_core(core)
        channel_runtime = ChannelRuntime()
        register_channel_with_core(core.context, channel_runtime)
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        register_memory_with_core(core.context, memory_service)
        scripted_llm = FallbackBudgetLLMRuntime()
        core.context.port_registry["llm:llm"] = scripted_llm

        outcome = core.process_channel_turn(
            ChannelEnvelope(
                event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "hello"}),
                endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
                response_handle=ResponseHandle(endpoint_id="stdio"),
            )
        )

        self.assertEqual(outcome.final_reply, "final answer")
        preflight_requests = [request for kind, request in scripted_llm.requests if kind == "preflight"]
        self.assertGreaterEqual(len(preflight_requests), 2)
        self.assertEqual(preflight_requests[-1].metadata.get("preferred_endpoint_id"), "fallback-small")
        self.assertEqual(preflight_requests[-1].model_hint, "fallback-small-model")
        self.assertTrue(memory_service.l1_store.items)

    def test_memory_prompt_clears_old_tool_protocol_in_complete_batches(self) -> None:
        from pal.memory.prompt import _build_cleared_tool_indices

        messages = [
            L1TranscriptMessage(role="assistant", content="", tool_calls=[{"id": "call_1"}]),
            L1TranscriptMessage(role="tool", content="tool-1", tool_call_id="call_1"),
            L1TranscriptMessage(role="assistant", content="", tool_calls=[{"id": "call_2"}]),
            L1TranscriptMessage(role="tool", content="tool-2", tool_call_id="call_2"),
            L1TranscriptMessage(role="assistant", content="", tool_calls=[{"id": "call_3"}]),
            L1TranscriptMessage(role="tool", content="tool-3", tool_call_id="call_3"),
        ]

        cleared = _build_cleared_tool_indices(messages, keep_recent=1)

        self.assertEqual(cleared, {0, 1, 2, 3})

    def test_memory_prompt_dedupes_working_memory_entries_by_identity(self) -> None:
        from pal.memory import MemoryPack
        from pal.memory.prompt import MemoryPromptFragmentProvider

        provider = MemoryPromptFragmentProvider()
        pack = MemoryPack(
            l2_working_memory=[
                L2Entry(
                    entry_id="fact:1",
                    kind="fact",
                    scope="system",
                    title="Nathan profile",
                    summary="Nathan built Pal.",
                    rendered="Nathan built Pal.",
                    canonical_key="nathan_profile",
                    source_ref="fact:1",
                    source_kind="l3_recall",
                ),
                L2Entry(
                    entry_id="fact:2",
                    kind="fact",
                    scope="system",
                    title="Nathan profile duplicate",
                    summary="Nathan built Pal again.",
                    rendered="Nathan built Pal again.",
                    canonical_key="nathan_profile",
                    source_ref="fact:2",
                ),
            ]
        )

        fragments = provider.build_prompt_fragments(
            PromptAssemblyContext(metadata={"memory_pack": pack})
        )

        working_memory = next(fragment for fragment in fragments if fragment.metadata.get("block_id") == "memory_working_memory")
        self.assertIn("Nathan built Pal. [L3 summary; origin available]", working_memory.content)
        self.assertNotIn("Nathan built Pal again.", working_memory.content)

    def test_memory_query_defaults_to_summary_view_enum(self) -> None:
        self.assertEqual(MemoryQuery().view, L3RecallView.SUMMARY)

    def test_l3_recall_tool_result_stays_under_budget_with_preview_shape(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        mock_l3 = MockL3Plugin(
            records=[
                {
                    "document_id": f"fact:{index}",
                    "document_kind": "fact",
                    "scope": "system",
                    "title": f"Nathan fact {index}",
                    "summary": "Nathan built Pal and prefers direct execution." * 8,
                    "rendered": "Nathan built Pal and prefers direct execution." * 8,
                    "search_text": "Nathan built Pal after OpenClaw frustration and wants direct action." * 10,
                }
                for index in range(10)
            ]
        )
        register_l3_with_core(core.context, mock_l3)
        core.publish_module_capabilities(mock_l3.module_id)

        result = core.context.execution_runtime.execute_tool(
            CanonicalToolCall(
                name="op_l3_recall_query",
                args={
                    "target_id": "mock_l3",
                    "queries": ["用户是谁", "用户身份", "用户个人信息", "用户偏好"],
                    "limit": 10,
                },
            ),
            budget=ToolCallBudget(max_output_chars=12_000),
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.structured["hit_count"], 10)
        self.assertLess(len(str(result.llm_text)), 12_000)

    def test_minimal_operating_rules_prompt_omits_route_specific_tools(self) -> None:
        from pal.core.prompt import MinimalOperatingRulesPromptFragmentProvider

        fragment = MinimalOperatingRulesPromptFragmentProvider().build_prompt_fragments(PromptAssemblyContext())[0]

        self.assertIn("Source-of-truth preference", fragment.content)
        self.assertIn("No success claim without confirmation", fragment.content)
        self.assertIn("Advisor gate", fragment.content)
        self.assertNotIn("op_l3_recall_query", fragment.content)
        self.assertNotIn("op_l3_commit_write", fragment.content)

    def test_memory_service_compact_uses_semantic_summary_and_projects_to_l2(self) -> None:
        service = MemoryService()
        service.l1_store.append(
            [
                L1TranscriptMessage(role="user", content="Please remember that the user prefers concise replies."),
                L1TranscriptMessage(role="assistant", content="I will keep replies concise."),
            ]
        )

        result = service.compact(
            MemoryCompactRequest(
                target_input_budget=512,
                reserved_output_tokens=128,
                metadata={"semantic_summary": "The user prefers concise replies."},
            )
        )
        self.assertEqual(result.summary, "The user prefers concise replies.")
        self.assertEqual(
            service.l1_store.items,
            [[L1TranscriptMessage(role="assistant", content="The user prefers concise replies.")]],
        )
        projected = service.l2_store.items["memory_summary_current"]
        self.assertEqual(projected.summary, "The user prefers concise replies.")
        self.assertEqual(projected.kind, "summary")

    def test_tool_stagnation_guard_detects_repeat_and_oscillation(self) -> None:
        guard = ToolStagnationGuardProcess(repeat_threshold=3, oscillation_window=4)
        repeat_records = [
            ToolExecutionRecord(
                turn_id="turn-1",
                sequence=index,
                tool_signature_hash=canonical_tool_signature_hash("echo", {"value": "same"}),
                result_fingerprint=canonical_result_fingerprint({"ok": True, "structured": {"value": "same"}}),
            )
            for index in range(3)
        ]
        repeat_verdict = guard.observe_batch("turn-1", repeat_records)

        self.assertEqual(repeat_verdict.status, "repeat_stagnation")
        self.assertEqual(repeat_verdict.recommended_action, "terminate_tool_loop")

        oscillation_guard = ToolStagnationGuardProcess(repeat_threshold=3, oscillation_window=4)
        oscillation_records = [
            ToolExecutionRecord(
                turn_id="turn-2",
                sequence=0,
                tool_signature_hash=canonical_tool_signature_hash("open", {"state": "on"}),
                result_fingerprint=canonical_result_fingerprint({"ok": True, "state": "on"}),
            ),
            ToolExecutionRecord(
                turn_id="turn-2",
                sequence=1,
                tool_signature_hash=canonical_tool_signature_hash("open", {"state": "off"}),
                result_fingerprint=canonical_result_fingerprint({"ok": True, "state": "off"}),
            ),
            ToolExecutionRecord(
                turn_id="turn-2",
                sequence=2,
                tool_signature_hash=canonical_tool_signature_hash("open", {"state": "on"}),
                result_fingerprint=canonical_result_fingerprint({"ok": True, "state": "on"}),
            ),
            ToolExecutionRecord(
                turn_id="turn-2",
                sequence=3,
                tool_signature_hash=canonical_tool_signature_hash("open", {"state": "off"}),
                result_fingerprint=canonical_result_fingerprint({"ok": True, "state": "off"}),
            ),
        ]

        oscillation_verdict = oscillation_guard.observe_batch("turn-2", oscillation_records)
        self.assertEqual(oscillation_verdict.status, "oscillation_stagnation")
        self.assertEqual(oscillation_verdict.recommended_action, "terminate_tool_loop")

    def _assert_no_forbidden_imports(
        self,
        path: Path,
        *,
        forbidden_fragments: tuple[str, ...],
    ) -> None:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                imported_modules.append(module)
        for imported in imported_modules:
            for fragment in forbidden_fragments:
                self.assertNotIn(fragment, imported, f"{path} imports forbidden module fragment {fragment}: {imported}")
