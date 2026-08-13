from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, new_tool_call

import ast
import asyncio
import contextlib
import importlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

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
    CompactionClockKind,
    CompactionSnapshot,
    MainLoop,
    PalCore,
    ToolExecutionRecord,
    ToolStagnationGuardProcess,
    canonical_result_fingerprint,
    canonical_tool_signature_hash,
    register_with_core as register_core_with_core,
)
from pal.core.runtime_config import RuntimeConfig
from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from pal.execution import CapabilityCall, CapabilityDescriptor, CapabilityResult, ToolCallBudget, register_with_core as register_execution_with_core
from pal.execution.tool_facade import (
    EffectKind,
    Idempotency,
    InvocationMode,
    PagingMode,
    RejectedResult,
    StructuredToolOutput,
    RetryPolicy,
    StrictToolModel,
    ToolExecutionSemantics,
    ToolGuidance,
    ToolHandlerResult,
)
from pal.execution.tool_semantics import INDIRECT_NONE
from pal.core.pal_compaction import PalCompactionPolicy
from tests.capability_fixture import mount_test_capability
from pal.failure import (
    FAILURE_VERIFICATION_FAILED,
    FAILURE_VERIFICATION_OK,
    FailureRuntime,
    FailureSignal,
    register_with_core as register_failure_with_core,
)
from pal.foundation import EventEnvelope, RawSQLHookRegistry
from pal.identity import IdentityRepository, IdentityService, register_with_core as register_identity_with_core
from pal.identity.prompt import IdentityPromptFragmentProvider
from pal.llm import generation_result_from_values, LLMPreflightAdvice
from pal.llm.ir import LLMResponseDeltaKind, LLMResponseUpdate
from pal.memory import (
    L1MessageKind,
    L1TranscriptMessage,
    L2Entry,
    L3RecallView,
    L3ProviderSelector,
    MemoryCommitRequest,
    MemoryCompactRequest,
    MemoryPack,
    MemoryPackRequest,
    MemoryQuery,
    MemoryService,
    register_with_core as register_memory_with_core,
)
from pal.plugins import PluginHost, register_with_core as register_plugins_with_core
from pal.plugins.l3 import MockL3Plugin, register_with_core as register_l3_with_core
from pal.proactive import ProactiveDefinition, ProactiveManager, ProactiveRepository, ProactiveRunner, ProactiveTriggerEvent, build_proactive_trigger_input, register_with_core as register_proactive_with_core
from pal.proactive.scheduling import compute_next_proactive_run_at_utc, utc_now_dt
from pal.shared import ChannelStreamUpdate, ChannelStreamUpdateKind, EventKind, OPERATION_NAMESPACE, PromptAssemblyContext, RuntimeStatus, SINGLETON_TARGET, TurnDeliveryBinding, capability_action, capability_node, default_tool_result_text
from pal.shared.prompt_dates import today_for_timezone
from pal.wizard import WizardService
from pal.minion import register_with_core as register_minion_with_core


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

    def supports_stream_delivery(self) -> bool:
        return True

    def send_reply(self, response_handle: ResponseHandle, text: str) -> None:
        self.sent.append((response_handle.endpoint_id, text))

    def send_stream_update(self, response_handle: ResponseHandle, update: ChannelStreamUpdate) -> None:
        if update.kind == ChannelStreamUpdateKind.TEXT_DELTA:
            self.streamed.append((response_handle.endpoint_id, "text", update.text))
        elif update.kind == ChannelStreamUpdateKind.REASONING_DELTA:
            self.streamed.append((response_handle.endpoint_id, "reasoning", update.reasoning_text))
        elif update.kind == ChannelStreamUpdateKind.TOOL_CALL and update.tool_call is not None:
            self.streamed.append((response_handle.endpoint_id, "op_tool_call", update.tool_call.name))
        else:
            self.streamed.append((response_handle.endpoint_id, str(update.kind), update.finish_reason or update.error_text))

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

    def invoke(self, args: dict[str, object]) -> CapabilityResult:
        return CapabilityResult(status="ok", llm_text="stable-result", text="stable-result", structured={"echo": args})


class VerboseTool:
    name = "verbose"
    description = "Return a placeholder summary plus rich llm_text."

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

    def __init__(self, *, size: int = 4000) -> None:
        self.size = size

    def invoke(self, args: dict[str, object]) -> CapabilityResult:
        payload = "X" * self.size
        return CapabilityResult(
            status="ok",
            text="huge-result",
            structured={"payload": {**args, "content": payload}, "size": self.size},
            llm_text=payload,
        )


class HeadTailHugeTool:
    name = "head_tail_huge"
    description = "Return a large payload with distinct head and tail markers."

    def invoke(self, args: dict[str, object]) -> CapabilityResult:
        _ = args
        payload = "HEAD-SIGNAL\n" + ("M" * 20_000) + "\nTAIL-SIGNAL"
        return CapabilityResult(
            status="ok",
            text=payload,
            structured={"kind": "head_tail", "content": payload},
            llm_text=payload,
        )


class MalformedTool:
    name = "malformed"
    description = "Return an object whose llm_text is empty to simulate a bad plugin result."

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

    def invoke(self, args: dict[str, object]) -> CapabilityResult:
        raise RuntimeError(f"boom: {args!r}")


class FixtureToolInput(StrictToolModel):
    value: str | None = None


def register_test_tool(runtime, tool) -> str:
    alias = str(tool.name)
    canonical_path = alias if alias.startswith(("op_", "intro_")) else f"op_test_{alias}"
    def invoke(value: FixtureToolInput):
        result = tool.invoke(value.model_dump(mode="python", exclude_none=True))
        if isinstance(result, CapabilityResult):
            return ToolHandlerResult(
                output=dict(result.structured or {}),
                llm_text=result.llm_text,
            )
        return result

    mount_test_capability(
        runtime,
            alias=alias,
            canonical_path=canonical_path,
            InputModel=FixtureToolInput,
            OutputModel=StructuredToolOutput,
            guidance=ToolGuidance(
                purpose=str(tool.description),
                use_when="running this focused runtime test",
                do_not_use_when="outside this test",
                failure_next_steps="inspect the returned tagged failure",
            ),
            execution=ToolExecutionSemantics(
                invocation_mode=InvocationMode.DIRECT,
                effect_kind=EffectKind.NONE,
                idempotency=Idempotency.IDEMPOTENT,
                retry_policy=RetryPolicy.AUTOMATIC,
                paging=PagingMode.SUPPORTED,
            ),
            handler=invoke,
            examples=({},),
    )
    return canonical_path


class ScriptedLLMRuntime:
    def __init__(self, outcomes: list[generation_result_from_values]) -> None:
        self.outcomes = outcomes
        self.requests = []

    def preflight(self, request) -> LLMPreflightAdvice:
        self.requests.append(("preflight", request))
        return LLMPreflightAdvice(
            status="ready",
            active_model="stub-model",
            fallback_chain=["stub-fallback"],
            target_input_budget=2048,
            reserved_output_tokens=request.request.policy.max_output_tokens,
        )

    async def apreflight(self, request) -> LLMPreflightAdvice:
        return self.preflight(request)

    def generate(self, request):
        self.requests.append(("generate", request))
        if self.outcomes:
            return self.outcomes.pop(0)
        return generation_result_from_values(text="done", tool_calls=[], finish_reason="stop")

    async def agenerate(self, request):
        return self.generate(request)

    async def astream(self, request):
        self.requests.append(("astream", request))
        if self.outcomes:
            outcome = self.outcomes.pop(0)
        else:
            outcome = generation_result_from_values(text="done", tool_calls=[], finish_reason="stop")
        events: list[LLMResponseUpdate] = []
        if outcome.reasoning_text:
            events.append(
                LLMResponseUpdate(
                    response=outcome.response,
                    delta_kind=LLMResponseDeltaKind.REASONING,
                    text_delta=outcome.reasoning_text,
                )
            )
        if outcome.text:
            events.append(
                LLMResponseUpdate(
                    response=outcome.response,
                    delta_kind=LLMResponseDeltaKind.TEXT,
                    text_delta=outcome.text,
                )
            )
        for tool_call in outcome.tool_calls:
            events.append(
                LLMResponseUpdate(
                    response=outcome.response,
                    delta_kind=LLMResponseDeltaKind.TOOL_CALL,
                    tool_call=tool_call,
                )
            )
        events.append(
            LLMResponseUpdate(
                response=outcome.response,
                delta_kind=LLMResponseDeltaKind.STATE,
            )
        )
        for event in events:
            yield event


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
            "pal.proactive",
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
        self.assertFalse(hasattr(shared, "standard_descriptors"))
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
            "pal.minion": ("MinionV2WorkflowService", "register_with_core", "inspect_minion"),
            "pal.proactive": ("ProactiveIntrospectionProvider", "register_with_core", "inspect_proactive"),
            "pal.web_search": ("WebSearchIntrospectionProvider", "register_with_core", "inspect_web_search"),
            "pal.web_fetch": ("WebFetchIntrospectionProvider", "register_with_core", "inspect_web_fetch"),
            "pal.control": ("ControlIntrospectionProvider", "register_with_core", "inspect_control"),
            "pal.failure": ("FailureIntrospectionProvider", "register_with_core"),
            "pal.plugins": ("PluginsIntrospectionProvider", "register_with_core"),
            "pal.bootstrap": ("inspect_bootstrap",),
            "pal.wizard": ("inspect_wizard",),
            "pal.plugins.l3": ("register_with_core",),
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

    def test_operation_capabilities_declare_execution_semantics(self) -> None:
        missing: list[str] = []
        for path in (ROOT / "src" / "pal").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for decorator in node.decorator_list:
                    if not isinstance(decorator, ast.Call):
                        continue
                    name = (
                        decorator.func.id
                        if isinstance(decorator.func, ast.Name)
                        else decorator.func.attr
                        if isinstance(decorator.func, ast.Attribute)
                        else ""
                    )
                    if name != "capability_action":
                        continue
                    keywords = {
                        keyword.arg: keyword.value
                        for keyword in decorator.keywords
                        if keyword.arg
                    }
                    namespace = keywords.get("namespace")
                    if not (
                        isinstance(namespace, ast.Name)
                        and namespace.id == "OPERATION_NAMESPACE"
                    ):
                        continue
                    if "execution" not in keywords:
                        missing.append(f"{path.relative_to(ROOT)}:{node.lineno}:{node.name}")
        self.assertEqual(missing, [])

    def test_minion_does_not_depend_on_user_facing_channel_modules(self) -> None:
        for relative_path in (
            "src/pal/minion/v2/contracts.py",
            "src/pal/minion/v2/service.py",
        ):
            self._assert_no_forbidden_imports(
                ROOT / relative_path,
                forbidden_fragments=("pal.channel",),
            )

    def test_agent_turn_layers_use_shared_agent_io_not_concrete_channel_contracts(self) -> None:
        for relative_path in (
            "src/pal/core/turns.py",
            "src/pal/core/turn_executor.py",
            "src/pal/core/turn_handler.py",
            "src/pal/control/routing.py",
            "src/pal/control/handler.py",
            "src/pal/proactive/turns.py",
            "src/pal/minion/runner.py",
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

    def test_domain_interaction_specs_stay_out_of_core_and_control_sources(self) -> None:
        core_runtime = (ROOT / "src/pal/core/runtime.py").read_text(encoding="utf-8")
        core_prompt = (ROOT / "src/pal/core/prompt.py").read_text(encoding="utf-8")
        llm_model_hooks = (ROOT / "src/pal/llm/model_hooks.py").read_text(encoding="utf-8")
        minion_source = (ROOT / "src/pal/minion/source.py").read_text(encoding="utf-8")
        control_interactions = (ROOT / "src/pal/control/interactions.py").read_text(encoding="utf-8")
        minion_interactions = (ROOT / "src/pal/minion/interactions.py").read_text(encoding="utf-8")
        memory_interactions = (ROOT / "src/pal/memory/interactions.py").read_text(encoding="utf-8")

        self.assertNotIn("InteractionMessageSpec(", core_runtime)
        self.assertNotIn("InteractionButtonSpec(", core_runtime)
        self.assertNotIn('"minion:minion"', core_runtime)
        self.assertNotIn("event_notify", core_runtime)
        self.assertNotIn("memory_candidate_decision", core_runtime)
        self.assertNotIn("l3_commit_args_from_memory_candidate", core_runtime)
        self.assertNotIn("op_minion", core_prompt)
        self.assertNotIn("MINION_LLM_REQUEST_HOOKS", llm_model_hooks)
        self.assertNotIn("MINION_BEHAVIOR_ROUTING_HOOK", llm_model_hooks)
        self.assertNotIn("minion_behavior_routing", llm_model_hooks)
        self.assertFalse((ROOT / "src/pal/llm/request_hooks.py").exists())
        self.assertNotIn("InteractionMessageSpec(", minion_source)
        self.assertNotIn("InteractionButtonSpec(", minion_source)
        self.assertIn("InteractionMessageSpec(", control_interactions)
        self.assertNotIn("memory_candidate_approval", control_interactions)
        self.assertNotIn("minion_", control_interactions)
        self.assertIn("InteractionMessageSpec(", minion_interactions)
        self.assertIn("InteractionButtonSpec(", minion_interactions)
        self.assertIn("InteractionMessageSpec(", memory_interactions)
        self.assertIn("InteractionButtonSpec(", memory_interactions)

    def test_retired_legacy_files_and_aliases_are_not_referenced(self) -> None:
        old_bridge_alias = "codex_" + "proxy"
        old_app_alias = "codex_" + "app_server"
        old_migration_hook = "migrate_" + "legacy_service_tables"
        retired_git_canonical = "op_" + "git"
        retired_workspace_aliases = (
            "op_" + "search",
            "op_" + "tree",
            "op_memory_" + "refresh_indexes",
        )
        for relative_path in (
            "tests/" + "telegram_endpoint.py",
            "src/pal/proactive/" + "schema.py",
            f"src/pal/llm/{old_bridge_alias}.py",
            f"src/pal/llm/{old_app_alias}.py",
            "src/pal/execution/" + "git_capabilities.py",
        ):
            self.assertFalse((ROOT / relative_path).exists(), relative_path)
        self.assertFalse((ROOT / "src/pal/llm/adapters.py").exists())
        for relative_path in (
            "src/pal/llm/runtime.py",
            "src/pal/minion/runner.py",
            "src/pal/wizard/runtime.py",
        ):
            content = (ROOT / relative_path).read_text(encoding="utf-8")
            self.assertNotIn(old_bridge_alias, content)
            self.assertNotIn(old_app_alias, content)
            self.assertNotIn(old_migration_hook, content)
        for relative_path in (
            "src/pal/core/tool_surface.toml",
            "src/pal/execution/generated_tool_models.py",
            "src/pal/minion/profiles.py",
        ):
            content = (ROOT / relative_path).read_text(encoding="utf-8")
            self.assertNotIn(retired_git_canonical, content)
            for retired_alias in retired_workspace_aliases:
                self.assertNotIn(retired_alias, content)

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
        with tempfile.TemporaryDirectory() as temp_dir:
            core = PalCore()
            register_core_with_core(core)
            register_execution_with_core(core.context)
            register_minion_with_core(
                core.context,
                runtime_root=Path(temp_dir),
            )

            core.publish_module_capabilities("core")
            core.publish_module_capabilities("execution")
            core.publish_module_capabilities("minion")

            result = core.context.execution_runtime.execute(
                CapabilityCall(name="minion_task_status", args={})
            )

            self.assertEqual(result.status, RuntimeStatus.INVALID)
        self.assertIn("No active Minion Task", result.llm_text)

    def test_execution_compiles_one_facade_generation_without_legacy_tool_registrations(self) -> None:
        core = PalCore()

        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")

        generation = core.context.execution_runtime.registry_generation
        for alias in ("run_shell", "search_tools", "read_tool", "read_tool_result", "read_file", "write_file"):
            self.assertIn(alias, generation.direct_aliases)
        for alias in ("delete_path", "edit_file", "file_state"):
            self.assertIn(alias, generation.indirect_aliases)

    def test_shell_exec_builtin_tool_runs_commands(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")

        result = core.context.execution_runtime.execute_tool(
            new_tool_call(name="run_shell", args={"cmd": "echo pong"})
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.structured["returncode"], 0)
        self.assertEqual(str(result.structured["stdout"]).strip(), "pong")
        self.assertTrue(result.text.startswith("pong"))
        self.assertEqual(result.invocation_result.effect.value, "applied")

    def test_shell_exec_builtin_tool_allows_file_operations(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")
        with tempfile.TemporaryDirectory(prefix="pal_shell_exec_test_") as tmp:
            root = Path(tmp)
            target = root / "sample.txt"
            target.write_text("line\n", encoding="utf-8")

            read_result = core.context.execution_runtime.execute_tool(
                new_tool_call(name="run_shell", args={"cmd": "cat sample.txt", "cwd": str(root)})
            )
            stdout_tail = core.context.execution_runtime.execute_tool(
                new_tool_call(name="run_shell", args={"cmd": "printf 'line\\n' | tail -1", "cwd": str(root)})
            )
            delete_result = core.context.execution_runtime.execute_tool(
                new_tool_call(name="run_shell", args={"cmd": "rm sample.txt", "cwd": str(root)})
            )

            self.assertTrue(read_result.ok, read_result.text)
            self.assertEqual(str(read_result.structured["stdout"]).strip(), "line")
            self.assertTrue(stdout_tail.ok, stdout_tail.text)
            self.assertTrue(delete_result.ok, delete_result.text)
            self.assertFalse(target.exists())

    def test_execution_exec_shell_capability_routes_to_shell_exec_tool(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")

        result = core.context.execution_runtime.execute_tool(
            new_tool_call(name="run_shell", args={"cmd": "echo pong"})
        )

        self.assertTrue(result.ok)
        self.assertEqual(str(result.structured["stdout"]).strip(), "pong")

        compat = core.context.execution_runtime.execute(
            CapabilityCall(name="op_exec_shell", args={"cmd": "echo pong"})
        )

        self.assertEqual(compat.status, "ok")
        self.assertEqual(str(compat.structured["stdout"]).strip(), "pong")

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

        self.assertIn("search_tools", published)
        self.assertIn("call_tool", published)
        self.assertIn("memory_show", published)
        self.assertIn("memory_set_active_provider", published)
        self.assertIn("memory_provider_show", published)
        self.assertIn("memory_provider_recall", published)
        self.assertNotIn("operation_execution_discovery_search", published)
        self.assertNotIn("introspection_module_memory_show", published)

    def test_operation_family_is_omitted_and_aliases_are_callable(self) -> None:
        @capability_node(
            namespace=OPERATION_NAMESPACE,
            scope="demo",
            kind="module",
            source="test",
            target_kind="module",
        )
        class DemoProvider:
            @capability_action(
                namespace=OPERATION_NAMESPACE,
                scope="demo",
                family="operation",
                action_name="ping",
                aliases=("demo_ping",),
                guidance=ToolGuidance(
                    purpose="Ping the demo provider.",
                    use_when="Testing operation-family alias compilation.",
                    do_not_use_when="Outside this focused test.",
                    failure_next_steps="Inspect the returned test failure.",
                ),
                execution=INDIRECT_NONE,
            )
            def ping(self, call: CapabilityCall) -> CapabilityResult:
                _ = call
                return CapabilityResult(status="ok", text="pong", llm_text="pong")

        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")
        core.context.register_module(
            ModuleHandle(
                module_id="demo",
                tier=MODULE_TIER_DETACHABLE,
                detachable=True,
                introspection_provider=DemoProvider(),
            )
        )
        core.publish_module_capabilities("demo")

        published = core.context.capability_registry.descriptors
        self.assertIn("demo_ping", published)
        self.assertNotIn("demo_operation_ping", published)

        direct = core.context.execution_runtime.execute(CapabilityCall(name="demo_ping"))
        self.assertEqual(direct.status, "ok")
        via_router = core.context.execution_runtime.execute(
            CapabilityCall(name="op_tool_call", args={"name": "demo_ping"})
        )
        self.assertEqual(via_router.status, "ok")

        search = core.context.execution_runtime.execute(CapabilityCall(name="op_tool_search", args={"query": "demo ping"}))
        hit = next(item for item in search.structured["hits"] if item["alias"] == "demo_ping")
        self.assertIn("input_shape", hit)
        found_read = core.context.execution_runtime.execute(CapabilityCall(name="op_tool_read", args={"name": hit["alias"]}))
        self.assertEqual(found_read.status, "ok")
        self.assertEqual(found_read.structured["alias"], "demo_ping")
        self.assertNotIn("canonical_path", found_read.structured)
        found_call = core.context.execution_runtime.execute(
            CapabilityCall(name="op_tool_call", args={"name": hit["alias"]})
        )
        self.assertEqual(found_call.status, "ok")
        self.assertTrue(found_call.text.startswith("pong"))

    def test_module_registration_conflict_does_not_leak_partial_projections(self) -> None:
        core = PalCore()
        first = ModuleHandle(
            module_id="first",
            tier=MODULE_TIER_DETACHABLE,
            ports={"service": object()},
            control_action_handlers={"shared.action": lambda _action: None},
        )
        second_port = object()
        second = ModuleHandle(
            module_id="second",
            tier=MODULE_TIER_DETACHABLE,
            ports={"service": second_port},
            control_action_handlers={"shared.action": lambda _action: None},
        )
        core.context.register_module(first)

        with self.assertRaisesRegex(ValueError, "control action handler already registered"):
            core.context.register_module(second)

        self.assertIsNone(core.context.module_registry.get("second"))
        self.assertNotIn("second:service", core.context.port_registry)
        self.assertNotIn("second", core.context.control_action_registry.by_module)
        self.assertIs(core.context.module_registry.get("first"), first)

    def test_duplicate_module_registration_preserves_original_handle(self) -> None:
        core = PalCore()
        original = ModuleHandle(module_id="demo", tier=MODULE_TIER_DETACHABLE)
        replacement = ModuleHandle(module_id="demo", tier=MODULE_TIER_DETACHABLE)
        core.context.register_module(original)

        with self.assertRaisesRegex(ValueError, "module already registered"):
            core.context.register_module(replacement)

        self.assertIs(core.context.module_registry.get("demo"), original)

    def test_execution_tool_failures_are_isolated(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        register_test_tool(core.context.execution_runtime, RaisingTool())

        result = core.context.execution_runtime.execute_tool(
            new_tool_call(name="boom", args={"value": "x"})
        )

        self.assertFalse(result.ok)
        self.assertIn("Tool boom failed", result.text)
        self.assertIn("Failure next steps:", result.text)
        self.assertIn("inspect the returned tagged failure", result.text)
        self.assertEqual(result.structured["kind"], "failed")
        self.assertEqual(result.structured["error_code"], "handler_exception")
        self.assertEqual(
            result.structured["details"]["failure_next_steps"],
            "inspect the returned tagged failure",
        )
        self.assertEqual(result.structured["affordances"][0]["tool"], "read_tool")

    def test_execution_tools_introspection_includes_description_and_schema(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")

        result = core.context.execution_runtime.execute(CapabilityCall(name="intro_module_exec_tools"))

        self.assertEqual(result.status, "ok")
        tools = result.structured["tools"]
        shell = next(tool for tool in tools if tool["name"] == "run_shell")
        self.assertIn("description", shell)
        self.assertIn("cmd", shell["input_schema"]["properties"])
        self.assertNotIn("op_", shell["description"])

    def test_tool_search_discovers_shell_exec_by_natural_language(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")

        result = core.context.execution_runtime.execute_tool(
            new_tool_call(name="search_tools", args={"query": "run shell command", "top_k": 5})
        )

        self.assertTrue(result.ok)
        hit_names = [item["alias"] for item in result.structured["hits"]]
        self.assertIn("run_shell", hit_names)
        shell_hit = next(item for item in result.structured["hits"] if item["alias"] == "run_shell")
        self.assertIn("cmd", shell_hit["input_shape"]["required"])
        self.assertNotIn("canonical_path", shell_hit)
        self.assertEqual(shell_hit["module_id"], "execution")
        self.assertEqual(shell_hit["family"], "exec")
        self.assertGreater(shell_hit["score"], 0)
        self.assertNotIn("call_names", shell_hit)
        self.assertGreaterEqual(result.structured["total_count"], result.structured["returned_count"])
        self.assertNotIn("facets", result.structured)

    def test_file_tools_are_published_as_llm_capabilities(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")

        published = core.context.execution_runtime.compiled_capability_index.by_canonical

        self.assertIn("op_file_read", published)
        self.assertIn("op_file_edit", published)
        self.assertIn("op_file_write", published)
        self.assertIn("op_path_delete", published)
        self.assertIn("op_file_state", published)

        result = core.context.execution_runtime.execute_tool(
            new_tool_call(name="search_tools", args={"query": "read edit write delete path file", "top_k": 10})
        )

        self.assertTrue(result.ok)
        hit_names = [item["alias"] for item in result.structured["hits"]]
        self.assertIn("read_file", hit_names)
        self.assertIn("edit_file", hit_names)
        self.assertIn("write_file", hit_names)
        self.assertIn("delete_path", hit_names)
        self.assertFalse(any(name.startswith("op_") or name.startswith("intro_") for name in hit_names))

    def test_main_runtime_tool_search_returns_only_public_aliases(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")

        result = core.context.execution_runtime.execute_tool(
            new_tool_call(name="search_tools", args={"query": "tree directory listing", "top_k": 10})
        )

        self.assertTrue(result.ok)
        hit_names = [item["alias"] for item in result.structured["hits"]]
        self.assertFalse(any(name.startswith("op_") or name.startswith("intro_") for name in hit_names))

    def test_file_capabilities_share_read_before_edit_cache(self) -> None:
        temp_dir = tempfile.mkdtemp()
        try:
            path = Path(temp_dir) / "sample.txt"
            path.write_text("hello world\n", encoding="utf-8")
            core = PalCore()
            register_execution_with_core(core.context)
            core.publish_module_capabilities("execution")

            direct_meta = {"direct_context_id": "test-file-lifetime"}
            read = core.context.execution_runtime.execute(
                CapabilityCall(name="op_file_read", args={"file_path": str(path)}, meta=direct_meta)
            )
            state = core.context.execution_runtime.execute(
                CapabilityCall(name="op_file_state", args={"file_path": str(path)}, meta=direct_meta)
            )
            edit = core.context.execution_runtime.execute(
                CapabilityCall(
                    name="op_file_edit",
                    args={"file_path": str(path), "old_string": "hello", "new_string": "goodbye"},
                    meta=direct_meta,
                )
            )

            self.assertEqual(read.status, "ok")
            self.assertEqual(state.status, "ok")
            self.assertTrue(state.structured["valid"])
            self.assertEqual(edit.status, "ok")
            self.assertEqual(path.read_text(encoding="utf-8"), "goodbye world\n")

            created_path = Path(temp_dir) / "created.txt"
            write = core.context.execution_runtime.execute(
                CapabilityCall(
                    name="op_file_write",
                    args={"file_path": str(created_path), "content": "draft\n"},
                    meta=direct_meta,
                )
            )
            update = core.context.execution_runtime.execute(
                CapabilityCall(
                    name="op_file_write",
                    args={"file_path": str(created_path), "content": "draft\ntail\n"},
                    meta=direct_meta,
                )
            )
            edit_created = core.context.execution_runtime.execute(
                CapabilityCall(
                    name="op_file_edit",
                    args={"file_path": str(created_path), "old_string": "draft", "new_string": "final"},
                    meta=direct_meta,
                )
            )

            self.assertEqual(write.status, "ok")
            self.assertEqual(update.status, "ok")
            self.assertEqual(edit_created.status, "ok")
            self.assertEqual(created_path.read_text(encoding="utf-8"), "final\ntail\n")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_file_capabilities_reject_missing_logical_lifetime(self) -> None:
        temp_dir = tempfile.mkdtemp()
        try:
            path = Path(temp_dir) / "sample.txt"
            path.write_text("hello\n", encoding="utf-8")
            core = PalCore()
            register_execution_with_core(core.context)
            core.publish_module_capabilities("execution")

            result = core.context.execution_runtime.execute(
                CapabilityCall(name="op_file_read", args={"file_path": str(path)})
            )

            self.assertEqual(result.status, "invalid")
            self.assertEqual(result.structured["error_code"], "missing_execution_lifetime")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_l1_has_no_durable_tool_result_truncation_hook(self) -> None:
        core = PalCore()

        self.assertFalse(hasattr(core.turn_manager, "_truncate_tool_result_for_l1"))

    def test_every_capability_action_declares_explicit_guidance(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src" / "pal"
        missing: list[str] = []
        for source_path in source_root.rglob("*.py"):
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for decorator in node.decorator_list:
                    if not isinstance(decorator, ast.Call):
                        continue
                    decorator_name = (
                        decorator.func.id
                        if isinstance(decorator.func, ast.Name)
                        else getattr(decorator.func, "attr", "")
                    )
                    if decorator_name != "capability_action":
                        continue
                    if not any(keyword.arg == "guidance" for keyword in decorator.keywords):
                        missing.append(f"{source_path.relative_to(source_root)}:{node.lineno}:{node.name}")
        self.assertEqual(missing, [])

    def test_tool_guidance_does_not_restore_known_stale_routes(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src" / "pal"
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in source_root.rglob("*.py")
        )
        stale_fragments = (
            "use search for repository text search",
            "Use file_edit for focused changes",
            "not supported by file_read",
            "Use artifact or vision tools",
            "artifact / vision tools",
            "Use this git tool",
            "use vision or the inline image",
            "If not_text_readable, use vision",
            "lsp_document_symbols/workspace_symbols",
            "lsp_definition/references/hover/call hierarchy",
            "use minion directly",
            "minion status tools",
            "checklist_show/upsert/check/clear",
            'failure_next_steps="Read-only."',
            'failure_next_steps="No external dependencies."',
            'failure_next_steps="Correct invalid input."',
            "Read-only-ish",
        )
        for fragment in stale_fragments:
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, source)

    def test_declared_tool_guidance_is_concise_and_actionable(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src" / "pal"
        failures: list[str] = []
        non_actions = {"Read-only.", "No external dependencies.", "Correct invalid input.", "Read-only-ish."}

        for source_path in source_root.rglob("*.py"):
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            constants: dict[str, str] = {}
            named_guidance: dict[str, ast.Call] = {}
            for statement in tree.body:
                if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
                    continue
                target = statement.targets[0]
                if not isinstance(target, ast.Name):
                    continue
                try:
                    value = ast.literal_eval(statement.value)
                except (TypeError, ValueError):
                    value = None
                if isinstance(value, str):
                    constants[target.id] = value
                if isinstance(statement.value, ast.Call):
                    call_name = (
                        statement.value.func.id
                        if isinstance(statement.value.func, ast.Name)
                        else getattr(statement.value.func, "attr", "")
                    )
                    if call_name == "ToolGuidance":
                        named_guidance[target.id] = statement.value

            def guidance_value(node: ast.AST) -> str | None:
                if isinstance(node, ast.Name):
                    return constants.get(node.id)
                try:
                    value = ast.literal_eval(node)
                except (TypeError, ValueError):
                    return None
                return value if isinstance(value, str) else None

            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for decorator in node.decorator_list:
                    if not isinstance(decorator, ast.Call):
                        continue
                    decorator_name = (
                        decorator.func.id
                        if isinstance(decorator.func, ast.Name)
                        else getattr(decorator.func, "attr", "")
                    )
                    if decorator_name != "capability_action":
                        continue
                    guidance_node = next(
                        (keyword.value for keyword in decorator.keywords if keyword.arg == "guidance"),
                        None,
                    )
                    if isinstance(guidance_node, ast.Name):
                        guidance_node = named_guidance.get(guidance_node.id)
                    if not isinstance(guidance_node, ast.Call):
                        failures.append(f"{source_path.name}:{node.lineno}: unresolved guidance")
                        continue
                    fields = {
                        keyword.arg: guidance_value(keyword.value)
                        for keyword in guidance_node.keywords
                    }
                    label = f"{source_path.name}:{node.lineno}:{node.name}"
                    for field in ("purpose", "use_when", "do_not_use_when", "failure_next_steps"):
                        if not str(fields.get(field) or "").strip():
                            failures.append(f"{label}: empty {field}")
                    purpose = str(fields.get("purpose") or "")
                    if len(purpose) > 240:
                        failures.append(f"{label}: purpose is {len(purpose)} characters")
                    failure = str(fields.get("failure_next_steps") or "").strip()
                    if failure in non_actions:
                        failures.append(f"{label}: non-actionable failure guidance {failure!r}")

        self.assertEqual(failures, [])

    def test_tool_input_ids_explain_where_the_exact_value_comes_from(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src" / "pal"
        input_models: set[str] = set()
        for source_path in source_root.rglob("*.py"):
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                decorator_name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else getattr(node.func, "attr", "")
                )
                if decorator_name != "capability_action":
                    continue
                input_model = next(
                    (keyword.value for keyword in node.keywords if keyword.arg == "InputModel"),
                    None,
                )
                if isinstance(input_model, ast.Name):
                    input_models.add(input_model.id)

        generated_path = source_root / "execution" / "generated_tool_models.py"
        generated = ast.parse(generated_path.read_text(encoding="utf-8"))
        failures: list[str] = []
        provenance_terms = (
            "returned by ",
            "from available artifacts",
            "from current context",
        )
        for statement in generated.body:
            if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
                continue
            target = statement.targets[0]
            if not isinstance(target, ast.Name) or "Input" not in target.id:
                continue
            if not isinstance(statement.value, ast.Call) or len(statement.value.args) < 2:
                continue
            fields = statement.value.args[1]
            if not isinstance(fields, ast.Dict):
                continue
            for key, value in zip(fields.keys, fields.values, strict=True):
                if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                    continue
                if not key.value.endswith("_id"):
                    continue
                description = ""
                for child in ast.walk(value):
                    if not isinstance(child, ast.Call):
                        continue
                    for keyword in child.keywords:
                        if keyword.arg == "description" and isinstance(keyword.value, ast.Constant):
                            description = str(keyword.value.value or "").strip()
                lowered = description.lower()
                if not description or not any(term in lowered for term in provenance_terms):
                    failures.append(f"{target.id}.{key.value}: {description or 'missing description'}")

        for source_path in source_root.rglob("*.py"):
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for model in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
                if model.name not in input_models:
                    continue
                for field in model.body:
                    if not isinstance(field, ast.AnnAssign) or not isinstance(field.target, ast.Name):
                        continue
                    if not field.target.id.endswith("_id"):
                        continue
                    description = ""
                    if isinstance(field.value, ast.Call):
                        for keyword in field.value.keywords:
                            if keyword.arg == "description" and isinstance(keyword.value, ast.Constant):
                                description = str(keyword.value.value or "").strip()
                    lowered = description.lower()
                    if not description or not any(term in lowered for term in provenance_terms):
                        failures.append(
                            f"{model.name}.{field.target.id}: {description or 'missing description'}"
                        )

        self.assertEqual(failures, [])

    def test_tool_read_returns_llm_facing_call_contract(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")

        result = core.context.execution_runtime.execute_tool(
            new_tool_call(name="read_tool", args={"name": "run_shell"})
        )

        self.assertTrue(result.ok)
        capability = result.structured
        self.assertEqual(capability["alias"], "run_shell")
        self.assertIn("shell", result.llm_text)
        self.assertIn("bounded directory listings", capability["description"])
        self.assertIn(
            "prefer rg for text search and rg --files for file enumeration",
            capability["description"],
        )
        self.assertIn(
            "Pal runtime, module, capability, or Minion state",
            capability["description"],
        )
        self.assertIn("Repository text search remains a run_shell task", capability["description"])
        self.assertIn("cmd", capability["input_schema"]["properties"])
        cmd_description = capability["input_schema"]["properties"]["cmd"]["description"]
        self.assertEqual(
            cmd_description,
            "Shell command to execute as one string. Pipelines and shell operators are accepted.",
        )
        self.assertNotIn("read_file", cmd_description)
        self.assertNotIn("checkpoint", cmd_description)
        self.assertNotIn("op_", result.llm_text)
        self.assertEqual(capability["input_schema"]["required"], ["cmd"])
        self.assertIn("output_schema", capability)
        self.assertIn(
            "returncode",
            core.context.execution_runtime.get_capability_spec("op_exec_shell")["output_schema"]["properties"],
        )

    def test_tool_read_invalid_result_carries_llm_text(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")

        result = core.context.execution_runtime.execute_tool(
            new_tool_call(name="read_tool", args={})
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "invalid_arguments")
        self.assertIn("name", result.llm_text)
        self.assertIn('"retry": "correct_input"', result.llm_text)

    def test_successful_introspection_llm_text_contains_key_structured_content(self) -> None:
        core = PalCore()
        runtime_root, database = self._create_database()
        try:
            service = IdentityService(repository=IdentityRepository())
            service.ensure_defaults()
            register_identity_with_core(core.context, service)
            core.publish_module_capabilities("identity")

            result = core.context.execution_runtime.execute(
                CapabilityCall(name="identity_show")
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
            CapabilityCall(name="op_tool_read", args={})
        )

        self.assertEqual(result.status, "invalid")
        self.assertEqual(result.text, "name is required")
        self.assertIn("use search_tools", result.llm_text)

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
            register_test_tool(core.context.execution_runtime, EchoTool())
            scripted_llm = ScriptedLLMRuntime([generation_result_from_values(text="final answer", tool_calls=[], finish_reason="stop")])
            core.context.port_registry["llm:llm"] = scripted_llm

            core.process_channel_turn(
                ChannelEnvelope(
                    event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "hello"}),
                    endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
                    response_handle=ResponseHandle(endpoint_id="stdio"),
                )
            )

            request = next(request for kind, request in scripted_llm.requests if kind in {"generate", "astream"})
            exposed_names = [item.name for item in request.tools]
            self.assertIn("search_tools", exposed_names)
            self.assertIn("read_tool", exposed_names)
            self.assertIn("read_tool_result", exposed_names)
            self.assertIn("run_shell", exposed_names)
            self.assertIn("read_file", exposed_names)
            self.assertNotIn("edit_file", exposed_names)
            self.assertIn("write_file", exposed_names)
            self.assertNotIn("delete_path", exposed_names)
            self.assertIn("call_tool", exposed_names)
            self.assertIn("recall_memory", exposed_names)
            self.assertNotIn("remember_memory", exposed_names)
            self.assertNotIn("update_memory", exposed_names)
            self.assertNotIn("forget_memory", exposed_names)
            self.assertNotIn("file_state", exposed_names)
            self.assertNotIn("artifact_info", exposed_names)
            self.assertNotIn("list_artifacts", exposed_names)
            self.assertNotIn("read_artifact", exposed_names)
            self.assertNotIn("search_artifacts", exposed_names)
            self.assertFalse(any(name.startswith("op_") or name.startswith("intro_") for name in exposed_names))
            self.assertNotIn("memory_active_provider", exposed_names)
            self.assertNotIn("memory_refresh_indexes", exposed_names)
            self.assertNotIn("memory_show", exposed_names)
            self.assertNotIn("llm_active", exposed_names)
            self.assertNotIn("llm_set_active_endpoint", exposed_names)
            self.assertIn("echo", exposed_names)
            exec_tool = next(item for item in request.tools if item.name == "run_shell")
            self.assertIn("cmd", exec_tool.input_schema["properties"])
            self.assertIn(
                "Pal runtime, module, capability, or Minion state",
                exec_tool.description,
            )
            self.assertIn(
                "Repository text search remains a run_shell task",
                exec_tool.description,
            )
            self.assertNotIn("op_", exec_tool.description)
            generation = core.context.execution_runtime.registry_generation
            memory_update = generation.indirect_aliases["update_memory"]
            self.assertIn("mem_ref", memory_update.input_schema["properties"])
            self.assertNotIn("target_id", memory_update.input_schema["properties"])
            memory_recall = next(item for item in request.tools if item.name == "recall_memory")
            memory_recall_properties = memory_recall.input_schema["properties"]
            self.assertIn("task_id", memory_recall_properties)
            self.assertIn("queries", memory_recall_properties)
            self.assertIn("topic_scope", memory_recall_properties)
            self.assertIn("limit", memory_recall_properties)
            self.assertIn("kind", memory_recall_properties)
            self.assertIn("view", memory_recall_properties)
            self.assertNotIn("level", memory_recall_properties)
            self.assertNotIn("scope", memory_recall_properties)
            memory_write = generation.indirect_aliases["remember_memory"]
            memory_write_description = memory_write.compiled_description
            self.assertIn("recall_memory", memory_write_description)
            self.assertIn("use update_memory instead", memory_write_description)
            self.assertIn("Do not write duplicates", memory_write_description)
            self.assertIn("fact:/case:", memory_write_description)
            memory_write_properties = memory_write.input_schema["properties"]
            self.assertIn("star", memory_write_properties)
            self.assertIn("task_id", memory_write_properties)
            self.assertNotIn("scope", memory_write_properties)
            self.assertNotIn("canonical_key", memory_write_properties)
            self.assertNotIn("payload", memory_write_properties)
            self.assertNotIn("situation_text", memory_write_properties)
            self.assertNotIn("task_text", memory_write_properties)
            self.assertNotIn("action_text", memory_write_properties)
            self.assertNotIn("result_text", memory_write_properties)
            memory_update_properties = memory_update.input_schema["properties"]
            self.assertIn("star", memory_update_properties)
            self.assertNotIn("payload_patch", memory_update_properties)
            self.assertNotIn("situation_text", memory_update_properties)
        finally:
            database.close()
            shutil.rmtree(runtime_root, ignore_errors=True)

    def test_memory_recall_task_id_implies_task_scope(self) -> None:
        class RecordingL3Plugin(MockL3Plugin):
            def __init__(self) -> None:
                super().__init__()
                self.last_query: MemoryQuery | None = None

            def recall(self, query: MemoryQuery):
                self.last_query = query
                return super().recall(query)

        core = PalCore()
        register_core_with_core(core)
        register_execution_with_core(core.context)
        register_execution_with_core(core.context)
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        register_memory_with_core(core.context, memory_service)
        l3_plugin = RecordingL3Plugin()
        register_l3_with_core(core.context, l3_plugin)
        memory_service.l3_selector.active_provider_id = l3_plugin.provider_id
        core.publish_module_capabilities("memory")
        core.publish_module_capabilities(l3_plugin.module_id)

        core.context.execution_runtime.execute(CapabilityCall(name="op_memory_recall", args={"queries": ["general"]}))
        self.assertIsNotNone(l3_plugin.last_query)
        self.assertIsNone(l3_plugin.last_query.scope)
        self.assertIsNone(l3_plugin.last_query.task_id)

        core.context.execution_runtime.execute(
            CapabilityCall(name="op_memory_recall", args={"queries": ["task repair"], "task_id": "task_123"})
        )
        self.assertIsNotNone(l3_plugin.last_query)
        self.assertEqual(l3_plugin.last_query.scope, "task")
        self.assertEqual(l3_plugin.last_query.task_id, "task_123")

    def test_memory_write_case_requires_star_object(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        register_execution_with_core(core.context)
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        register_memory_with_core(core.context, memory_service)
        l3_plugin = MockL3Plugin()
        register_l3_with_core(core.context, l3_plugin)
        memory_service.l3_selector.active_provider_id = l3_plugin.provider_id
        core.publish_module_capabilities("memory")
        core.publish_module_capabilities(l3_plugin.module_id)

        missing_star = core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_memory_write",
                args={
                    "kind": "case",
                    "summary": "Recovered a failed task.",
                    "search_text": "Recovered a failed task by retrying with a smaller scope.",
                },
            )
        )
        self.assertEqual(missing_star.status, RuntimeStatus.INVALID)
        self.assertIn("requires star", missing_star.llm_text)

        complete_star = core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_memory_write",
                args={
                    "kind": "case",
                    "summary": "Recovered a failed task by narrowing scope.",
                    "search_text": "The task failed from too much scope. Pal narrowed the scope, retried the repair, and the test passed.",
                    "star": {
                        "situation": "The task failed from too much scope.",
                        "task": "Recover the failed repair.",
                        "action": "Narrowed the scope and retried the repair.",
                        "result": "The focused test passed.",
                    },
                },
            )
        )

        self.assertEqual(complete_star.status, RuntimeStatus.OK)
        self.assertTrue(any(record.get("document_kind") == "case" for record in l3_plugin.records))

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
                generation_result_from_values(
                    text='{"verification_status":"ok","reason":"Provider inventory is sufficient; maintenance index refresh stays internal."}',
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
        request = next(request for kind, request in scripted_llm.requests if kind in {"generate", "astream"})
        tool_names = [tool.name for tool in request.tools]
        self.assertNotIn("memory_provider_refresh_indexes", tool_names)
        self.assertIn("read_tool", tool_names)
        self.assertNotIn("memory_provider_inventory", tool_names)
        self.assertNotIn("shell", tool_names)

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
                    generation_result_from_values(
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
            request = next(request for kind, request in scripted_llm.requests if kind in {"generate", "astream"})
            tool_names = [tool.name for tool in request.tools]
            self.assertIn("read_tool", tool_names)
            self.assertNotIn("plugin_rescan", tool_names)
            self.assertNotIn("plugin_enable", tool_names)
            self.assertNotIn("plugin_disable", tool_names)
            self.assertNotIn("plugin_detach", tool_names)
            self.assertNotIn("shell", tool_names)
        finally:
            shutil.rmtree(runtime_root, ignore_errors=True)

    def test_pal_core_collects_prompt_fragments_only_from_registry(self) -> None:
        runtime_root, database = self._create_database()
        try:
            identity_service = IdentityService(repository=IdentityRepository())
            identity_service.ensure_defaults()

            core = PalCore()
            register_core_with_core(core)
            proactive_manager = ProactiveManager()
            proactive_manager.register(ProactiveDefinition(proactive_id="digest", goal="Summarize updates"))
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
            register_minion_with_core(core.context)
            register_proactive_with_core(core.context, proactive_manager)
            register_memory_with_core(core.context, memory_service)

            fragments = core.collect_prompt_fragments(PromptAssemblyContext(core_mode="default"))
            sections = [fragment.section for fragment in fragments]

            self.assertEqual(
                sections,
                [
                    "identity",
                    "memory_guide",
                    "system_map",
                    "source_of_truth",
                    "prompt_context_policy",
                    "operating_rules",
                    "operating_guidance",
                    "priority",
                    "tool_routing",
                    "tool_efficiency",
                    "mutation_policy",
                    "knowledge_storage_boundary",
                ],
            )
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
            self.assertEqual(
                prompt.metadata["fragment_sections"],
                (
                    "identity",
                    "system_map",
                    "source_of_truth",
                    "prompt_context_policy",
                    "operating_rules",
                    "priority",
                    "tool_routing",
                    "tool_efficiency",
                    "mutation_policy",
                    "memory_guide",
                    "knowledge_storage_boundary",
                ),
            )
            self.assertEqual(prompt.metadata["reminder_sections"], ())
            self.assertEqual(
                prompt.metadata["user_context_blocks"],
                ("l1_recent_context_0", "l1_recent_context_1", "memory_recalled_context"),
            )
            self.assertEqual(prompt_ir.turn_kind, "chat")
            self.assertEqual(prompt.messages[0].role.value, "system")
            self.assertEqual(prompt.messages[-1].role.value, "user")
            self.assertIn("Hello from user", prompt.messages[-1].text)
            self.assertNotIn("<runtime_reminder", prompt.messages[-1].text)
            self.assertIn("<identity>", prompt.messages[0].text)
            self.assertIn("<system_map>", prompt.messages[0].text)
            self.assertIn("<source_of_truth>", prompt.messages[0].text)
            self.assertIn("<prompt_context_policy>", prompt.messages[0].text)
            self.assertIn("<operating_rules>", prompt.messages[0].text)
            self.assertIn("<priority>", prompt.messages[0].text)
            self.assertIn("<tool_routing>", prompt.messages[0].text)
            self.assertNotIn("<task_flow>", prompt.messages[0].text)
            self.assertNotIn("minion_task_search", prompt.messages[0].text)
            self.assertNotIn("minion_dispatch_workflow", prompt.messages[0].text)
            self.assertIn("<tool_efficiency>", prompt.messages[0].text)
            system_text = prompt.messages[0].text
            self.assertIn("<mutation_policy>", system_text)
            self.assertIn("<memory_guide>", system_text)
            self.assertIn("<knowledge_storage_boundary>", system_text)
            self.assertNotIn("##", system_text)
            self.assertNotIn("<capability_guide>", system_text)
            self.assertIn("<recalled_memories> contains durable memory context", system_text)
            self.assertIn("execution/capability", system_text)
            self.assertNotIn("minion", system_text.split("<source_of_truth>", 1)[0].lower())
            self.assertIn("Memory tool descriptions", system_text)
            self.assertIn("prefixes such as fact: and case:", system_text)
            self.assertNotIn("memory_recall", system_text)
            self.assertNotIn("memory_write", system_text)
            self.assertNotIn("memory_update", system_text)
            self.assertNotIn("If recalled memories are already present in the prompt", system_text)
            self.assertNotIn("Mandatory recall", system_text)
            self.assertNotIn("custom Pal/project term", system_text)
            self.assertNotIn("op_tool_search", system_text)
            self.assertNotIn("op_tool_call", system_text)
            self.assertLess(system_text.index("<identity>"), system_text.index("<system_map>"))
            self.assertLess(system_text.index("<system_map>"), system_text.index("<source_of_truth>"))
            self.assertLess(system_text.index("<source_of_truth>"), system_text.index("<prompt_context_policy>"))
            self.assertLess(system_text.index("<prompt_context_policy>"), system_text.index("<operating_rules>"))
            self.assertLess(system_text.index("<operating_rules>"), system_text.index("<priority>"))
            self.assertLess(system_text.index("<priority>"), system_text.index("<tool_routing>"))
            self.assertLess(system_text.index("<tool_routing>"), system_text.index("<tool_efficiency>"))
            self.assertLess(system_text.index("<mutation_policy>"), system_text.index("<memory_guide>"))
            self.assertNotIn("<runtime_overlay>", system_text)
            self.assertNotIn("<memory_projection>", system_text)
            self.assertEqual((prompt.messages[1].role.value, prompt.messages[1].text), ("user", "What timezone should you use?"))
            self.assertEqual((prompt.messages[2].role.value, prompt.messages[2].text), ("assistant", "I should use Asia/Shanghai context."))
            final_text = prompt.messages[-1].text
            self.assertNotIn("Recalled memory references are operational metadata.", final_text)
            self.assertNotIn("<tool_efficiency>", final_text)
            self.assertNotIn("<memory_guidance>", final_text)
            self.assertNotIn("If recalled memories are already present in the prompt", final_text)
            self.assertNotIn("MUST call memory_recall", final_text)
            self.assertNotIn("custom Pal/project term", final_text)
            self.assertIn('<runtime_context_update kind="memory">', final_text)
            self.assertIn("This is not a new user message. Do not answer this block directly.", final_text)
            self.assertIn('<recalled_memories view="summary">', final_text)
            self.assertIn("[summary-1]: The user prefers replies in Asia/Shanghai context.", final_text)
            self.assertNotIn("Working Memory", final_text)
            self.assertNotIn("Timezone Preference", final_text)
            self.assertNotIn("Timezone Preference", system_text)
            self.assertNotIn("Issued work orders", system_text)
            self.assertNotIn("Registered proactive tasks", system_text)
            self.assertNotIn("Active L3", system_text)
            self.assertNotIn("Available L3", system_text)
            self.assertNotIn("candidate_state", system_text)
        finally:
            database.close()
            shutil.rmtree(runtime_root, ignore_errors=True)

    def test_pal_core_builds_proactive_trigger_prompt_as_single_user_input(self) -> None:
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

            definition = ProactiveDefinition(
                proactive_id="daily_digest",
                goal="Summarize repository updates",
                method="Review recent changes and produce a concise digest.",
                skill_refs=["git", "summary"],
            )
            register_proactive_with_core(core.context, ProactiveManager())
            prompt = core.build_canonical_prompt(
                PromptAssemblyContext(
                    core_mode="default",
                    turn_kind="proactive_trigger",
                    metadata={
                        "proactive_input": build_proactive_trigger_input(definition),
                    },
                )
            )

            self.assertEqual(prompt.messages[0].role.value, "system")
            self.assertEqual(prompt.messages[-1].role.value, "user")
            proactive_text = prompt.messages[-1].text
            self.assertIn("<proactive_trigger>", proactive_text)
            self.assertIn("</proactive_trigger>", proactive_text)
            self.assertIn("Goal: Summarize repository updates", proactive_text)
            self.assertIn("Method: Review recent changes and produce a concise digest.", proactive_text)
            self.assertEqual(len(prompt.messages), 2)
            self.assertNotIn("Remember the last digest.", proactive_text)
            self.assertNotIn("Recent summaries", prompt.messages[0].text + "\n" + proactive_text)
        finally:
            database.close()
            shutil.rmtree(runtime_root, ignore_errors=True)

    def test_proactive_trigger_executes_turn_and_commits_transcript_to_l1(self) -> None:
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
            proactive_manager = ProactiveManager()
            register_proactive_with_core(core.context, proactive_manager, None)
            definition = ProactiveDefinition(
                proactive_id="daily_digest",
                goal="Summarize repository updates",
                method="Review recent changes and produce a concise digest.",
                skill_refs=["git", "summary"],
            )
            proactive_manager.register(definition)
            core.context.port_registry["llm:llm"] = ScriptedLLMRuntime(
                [generation_result_from_values(text="Daily digest complete.", tool_calls=[], finish_reason="stop")]
            )

            proactive_manager.enqueue_trigger(ProactiveTriggerEvent(proactive_id="daily_digest", trigger_kind="manual"))
            processed = core.run_until_idle()

            self.assertIn("proactive.trigger", [item.event_kind for item in processed])
            transcript = [
                message
                for item in memory_service.l1_store.items
                for message in item
            ]
            self.assertEqual(transcript[0].role, "user")
            self.assertIn("<proactive_trigger>", transcript[0].content)
            self.assertIn("</proactive_trigger>", transcript[0].content)
            self.assertIn("Goal: Summarize repository updates", transcript[0].content)
            self.assertEqual(transcript[-1].role, "assistant")
            self.assertEqual(transcript[-1].content, "Daily digest complete.")
        finally:
            database.close()
            shutil.rmtree(runtime_root, ignore_errors=True)

    def test_proactive_trigger_delivers_reply_to_persisted_output_target(self) -> None:
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
            proactive_manager = ProactiveManager()
            register_proactive_with_core(core.context, proactive_manager, None)
            definition = ProactiveDefinition(
                proactive_id="daily_digest",
                goal="Summarize repository updates",
                method="Review recent changes and produce a concise digest.",
                out_channel_id="telegram_main",
                out_reply_target={"chat_id": "12345", "thread_id": "7"},
            )
            proactive_manager.register(definition)
            core.context.port_registry["llm:llm"] = ScriptedLLMRuntime(
                [generation_result_from_values(text="Daily digest complete.", tool_calls=[], finish_reason="stop")]
            )

            proactive_manager.enqueue_trigger(ProactiveTriggerEvent(proactive_id="daily_digest", trigger_kind="scheduled"))
            core.run_until_idle()

            self.assertEqual(endpoint.sent, [])
            self.assertEqual(
                endpoint.streamed,
                [
                    ("telegram_main", "text", "Daily digest complete."),
                    ("telegram_main", "done", "stop"),
                ],
            )
        finally:
            database.close()
            shutil.rmtree(runtime_root, ignore_errors=True)

    def test_identity_prompt_includes_current_date(self) -> None:
        service = SimpleNamespace(
            get_persona=lambda: SimpleNamespace(
                display_name="Pal",
                language="zh",
                vibe="",
                tone="",
                core_policy=[],
            ),
            get_preferences=lambda: SimpleNamespace(
                timezone="Asia/Shanghai",
                style_preference="",
                preferences_blob={},
            ),
        )
        fragment = IdentityPromptFragmentProvider(service=service).build_prompt_fragments(PromptAssemblyContext())[0]

        self.assertIn("Timezone: Asia/Shanghai", fragment.content)
        self.assertRegex(fragment.content, r"Today's date is \d{4}-\d{2}-\d{2}\.")

    def test_today_for_timezone_uses_configured_timezone(self) -> None:
        fixed_utc = datetime(2026, 7, 2, 16, 30, tzinfo=timezone.utc)

        self.assertEqual(today_for_timezone("Asia/Shanghai", now_utc=fixed_utc), "2026-07-03")
        self.assertEqual(today_for_timezone("UTC", now_utc=fixed_utc), "2026-07-02")

    def test_proactive_trigger_delivers_and_settles_all_turn_replies(self) -> None:
        runtime_root, database = self._create_database()
        try:
            core = PalCore()
            register_core_with_core(core)
            register_execution_with_core(core.context)
            register_test_tool(core.context.execution_runtime, EchoTool())
            identity_service = IdentityService(repository=IdentityRepository())
            identity_service.ensure_defaults()
            register_identity_with_core(core.context, identity_service)
            channel_runtime = ChannelRuntime()
            register_channel_with_core(core.context, channel_runtime)
            endpoint = StubEndpoint(EndpointConfig(endpoint_id="telegram_main", channel_kind="telegram", binding_key="chat:12345"))
            channel_runtime.register_endpoint(endpoint)
            memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
            register_memory_with_core(core.context, memory_service)
            proactive_repository = ProactiveRepository()
            proactive_manager = ProactiveManager(repository=proactive_repository)
            proactive_runner = ProactiveRunner(repository=proactive_repository)
            register_proactive_with_core(core.context, proactive_manager, proactive_runner)
            definition = ProactiveDefinition(
                proactive_id="daily_digest",
                goal="Summarize repository updates",
                method="Review recent changes and produce a concise digest.",
                out_channel_id="telegram_main",
                out_reply_target={"chat_id": "12345", "thread_id": "7"},
            )
            proactive_manager.register(definition)
            core.context.port_registry["llm:llm"] = ScriptedLLMRuntime(
                [
                    generation_result_from_values(
                        text="Starting digest.",
                        tool_calls=[new_tool_call(name="echo", args={"value": "digest"})],
                        finish_reason="tool_calls",
                    ),
                    generation_result_from_values(text="Daily digest complete.", tool_calls=[], finish_reason="stop"),
                ]
            )

            proactive_manager.enqueue_trigger(ProactiveTriggerEvent(proactive_id="daily_digest", trigger_kind="scheduled"))
            core.run_until_idle()

            self.assertEqual(endpoint.sent, [])
            self.assertEqual(
                endpoint.streamed,
                [
                    ("telegram_main", "text", "Starting digest."),
                    ("telegram_main", "op_tool_call", "echo"),
                    ("telegram_main", "done", "tool_calls"),
                    ("telegram_main", "text", "Daily digest complete."),
                    ("telegram_main", "done", "stop"),
                ],
            )
            latest = proactive_repository.latest_run("daily_digest")
            self.assertIsNotNone(latest)
            self.assertEqual(latest.output_summary, "Starting digest.\n\nDaily digest complete.")
            assistant_messages = [
                message.content
                for transcript in memory_service.l1_store.items
                for message in transcript
                if message.role == "assistant"
            ]
            self.assertEqual(assistant_messages, ["Starting digest.", "Daily digest complete."])
        finally:
            database.close()
            shutil.rmtree(runtime_root, ignore_errors=True)

    def test_proactive_manager_enqueues_due_scheduled_trigger(self) -> None:
        manager = ProactiveManager()
        reference = utc_now_dt()
        definition = ProactiveDefinition(
            proactive_id="heartbeat",
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
        self.assertEqual(due[0].proactive_id, "heartbeat")
        self.assertEqual(due[0].trigger_kind, "scheduled")
        self.assertIn("scheduled_for", due[0].metadata)
        self.assertEqual(len(manager.pending_triggers), 1)
        self.assertIsNotNone(manager.schedule_engine.next_due_at("heartbeat"))

    def test_compute_next_proactive_run_supports_cron_schedule(self) -> None:
        next_due = compute_next_proactive_run_at_utc(
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

    def test_main_loop_drains_channel_proactive_and_minion_sources(self) -> None:
        core = PalCore()
        channel_runtime = ChannelRuntime()
        proactive_manager = ProactiveManager()

        register_channel_with_core(core.context, channel_runtime)
        register_proactive_with_core(core.context, proactive_manager)
        register_minion_with_core(core.context)
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
        proactive_manager.enqueue_trigger(ProactiveTriggerEvent(proactive_id="svc-1", trigger_kind="manual"))
        minion_provider = core.context.port_registry["minion:minion"]
        minion_provider._buffer_event(
            {"event_kind": "terminal", "work_order_id": "wf-1", "payload": {"summary": "completed"}}
        )

        processed = core.run_until_idle()

        processed_kinds = [item.event_kind for item in processed]
        self.assertIn("slash_command", processed_kinds)
        self.assertIn("proactive.trigger", processed_kinds)
        self.assertIn("minion.terminal", processed_kinds)
        self.assertIn("control.action", processed_kinds)

    def test_foundation_modules_do_not_publish_lifecycle_capabilities(self) -> None:
        core = PalCore()
        register_channel_with_core(core.context, ChannelRuntime())

        published = core.publish_module_capabilities("channel")

        self.assertIn("channel_list", published)
        self.assertIn("channel_send_message", published)
        self.assertIn("channel_enable", published)
        self.assertIn("channel_disable", published)
        self.assertIn("channel_attach", published)
        self.assertIn("channel_detach", published)
        self.assertNotIn("operation_channel_lifecycle_attach", published)
        self.assertNotIn("operation_channel_lifecycle_detach", published)
        self.assertNotIn("operation_channel_endpoint_attach", published)
        self.assertNotIn("operation_channel_endpoint_detach", published)
        descriptor = core.context.capability_registry.descriptors["channel_list"]
        self.assertEqual(descriptor.display_name, "channel_list")
        self.assertEqual(descriptor.target_kind, "module")
        self.assertEqual(descriptor.target_id, SINGLETON_TARGET)
        self.assertEqual(descriptor.target_label, "channel")
        self.assertEqual(descriptor.aliases, ("channel_list",))

    def test_identity_is_always_on_and_query_only(self) -> None:
        runtime_root, database = self._create_database()
        try:
            core = PalCore()
            service = IdentityService(repository=IdentityRepository())
            service.ensure_defaults()
            register_identity_with_core(core.context, service)

            published = core.publish_module_capabilities("identity")

            self.assertIn("identity_show", published)
            self.assertNotIn("identity_configure", published)
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
        self.assertIn("control_show", core.context.capability_registry.descriptors)
        observed = core.context.execution_runtime.execute(CapabilityCall(name="control_show"))
        self.assertTrue(observed.structured["degraded"])

    def test_detachable_module_detach_withdraws_capabilities_and_reattach_restores_them(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_lifecycle_test_") as tmp:
            core = PalCore()
            register_minion_with_core(core.context, runtime_root=Path(tmp))
            core.publish_module_capabilities("minion")
            try:
                self.assertIn("minion_task_status", core.context.capability_registry.descriptors)
                self.assertIn("minion.manager", core.context.event_source_registry.sources)

                detached = core.detach_module("minion")
                self.assertEqual(detached, "ok")
                self.assertNotIn("minion_task_status", core.context.capability_registry.descriptors)
                self.assertNotIn("minion.manager", core.context.event_source_registry.sources)

                reattached = core.reattach_module("minion")
                self.assertEqual(reattached, "ok")
                self.assertIn("minion_task_status", core.context.capability_registry.descriptors)
                self.assertIn("minion.manager", core.context.event_source_registry.sources)
                observed = core.context.execution_runtime.execute(
                    CapabilityCall(name="minion_task_status", args={})
                )
                self.assertEqual(observed.status, RuntimeStatus.INVALID)
                self.assertIn("No active Minion Task", observed.llm_text)
            finally:
                with contextlib.suppress(Exception):
                    core.detach_module("minion")

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
                name="memory_set_active_provider",
                args={"name": "mock_l3"},
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
                name="memory_set_active_provider",
                args={"name": "mock_l3"},
            )
        )
        fallback = core.context.execution_runtime.execute(
            CapabilityCall(
                name="memory_set_active_provider",
                args={"name": "null_l3"},
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
        self.assertNotIn("memory_provider_show", core.context.capability_registry.descriptors)

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
                    "title": "Test user profile",
                    "summary": "The test user built Pal and wants it to act directly.",
                    "rendered": "The test user built Pal and wants it to act directly.",
                    "search_text": "The test user built Pal after being unhappy with a legacy assistant and wants Pal to act directly.",
                    "canonical_key": "test_user_profile",
                    "dedupe_fingerprint": "fp_1",
                }
            ]
        )
        register_l3_with_core(core.context, mock_l3)
        memory_service.l3_selector.active_provider_id = mock_l3.provider_id
        core.publish_module_capabilities("memory")
        core.publish_module_capabilities(mock_l3.module_id)

        summary_result = core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_memory_recall",
                args={"queries": ["test_user"], "view": "summary"},
            )
        )
        origin_result = core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_memory_recall",
                args={"queries": ["test_user"], "view": "origin"},
            )
        )

        self.assertEqual(summary_result.status, "ok")
        self.assertEqual(summary_result.structured["view"], "summary")
        self.assertEqual(summary_result.structured["hit_count"], 1)
        self.assertEqual(summary_result.structured["hits_preview"][0]["mem_ref"], "fact:1")
        self.assertEqual(summary_result.structured["hits_preview"][0]["summary"], "The test user built Pal and wants it to act directly.")
        self.assertNotIn("hits", summary_result.structured)
        self.assertNotIn("projected_entries", summary_result.structured)
        self.assertIn("[fact:1]: The test user built Pal and wants it to act directly.", summary_result.llm_text)
        self.assertIn("The test user built Pal and wants it to act directly.", summary_result.llm_text)
        self.assertNotIn("projected_entries", summary_result.llm_text)
        self.assertNotIn("legacy assistant", summary_result.llm_text)

        self.assertEqual(origin_result.status, "ok")
        self.assertEqual(origin_result.structured["view"], "origin")
        self.assertEqual(origin_result.structured["hit_count"], 1)
        self.assertEqual(origin_result.structured["hits_preview"][0]["mem_ref"], "fact:1")
        self.assertIn("legacy assistant", origin_result.structured["hits_preview"][0]["search_text"])
        self.assertNotIn("hits", origin_result.structured)
        self.assertNotIn("projected_entries", origin_result.structured)
        self.assertIn("legacy assistant", origin_result.llm_text)
        self.assertNotIn("projected_entries", origin_result.llm_text)
        self.assertIn("memory_provider_show", core.context.capability_registry.descriptors)

    def test_active_memory_update_and_delete_use_mem_ref_without_target_id(self) -> None:
        core = PalCore()
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        register_memory_with_core(core.context, memory_service)
        mock_l3 = MockL3Plugin(
            records=[
                {
                    "document_id": "fact:1",
                    "document_kind": "fact",
                    "scope": "system",
                    "summary": "Old memory text.",
                    "search_text": "Old source text.",
                }
            ]
        )
        register_l3_with_core(core.context, mock_l3)
        memory_service.l3_selector.active_provider_id = mock_l3.provider_id
        core.publish_module_capabilities("memory")
        core.publish_module_capabilities(mock_l3.module_id)

        update = core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_memory_update",
                args={"mem_ref": "fact:1", "summary": "Updated memory text.", "search_text": "Updated source text."},
            )
        )
        recall = core.context.execution_runtime.execute(
            CapabilityCall(name="op_memory_recall", args={"queries": ["updated"], "view": "summary"})
        )

        self.assertEqual(update.status, "ok")
        self.assertEqual(update.structured["mem_ref"], "fact:1")
        self.assertEqual(mock_l3.records[0]["summary"], "Updated memory text.")
        self.assertEqual(mock_l3.records[0]["search_text"], "Updated source text.")
        self.assertEqual(recall.status, "ok")
        self.assertIn("[fact:1]: Updated memory text.", recall.llm_text)
        delete = core.context.execution_runtime.execute(
            CapabilityCall(name="op_memory_delete", args={"mem_ref": "fact:1", "reason": "test cleanup"})
        )
        self.assertEqual(delete.status, "ok")
        self.assertEqual(delete.structured["mem_ref"], "fact:1")
        self.assertEqual(mock_l3.records, [])

    def test_active_memory_provider_is_discoverable_but_not_resident(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        register_memory_with_core(core.context, memory_service)
        mock_l3 = MockL3Plugin()
        register_l3_with_core(core.context, mock_l3)
        memory_service.l3_selector.active_provider_id = mock_l3.provider_id
        core.publish_module_capabilities("execution")
        core.publish_module_capabilities("memory")
        core.publish_module_capabilities(mock_l3.module_id)

        result = core.context.execution_runtime.execute(
            CapabilityCall(name="op_tool_search", args={"query": "active memory provider", "top_k": 10})
        )
        tool_names = [item["function"]["name"] for item in core.tool_surface.build_llm_tool_contracts()]

        self.assertEqual(result.status, "ok")
        self.assertIn("memory_active_provider", [item["alias"] for item in result.structured["hits"]])
        self.assertNotIn("memory_active_provider", tool_names)

    def test_tool_result_page_is_resident_llm_tool(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")

        tools = core.tool_surface.build_llm_tool_contracts()
        page_tool = next(
            item
            for item in tools
            if item.get("function", {}).get("name") == "read_tool_result"
        )

        schema = page_tool["function"]["input_schema"]
        self.assertIn("result_ref", schema["properties"])
        self.assertIn("page", schema["properties"])
        self.assertIn("anchor", schema["properties"])
        self.assertIn("tail", schema["properties"])
        self.assertEqual(schema["required"], ["result_ref"])
        read_result = core.context.execution_runtime.execute_tool(
            new_tool_call(name="read_tool", args={"name": "read_tool_result"})
        )
        self.assertTrue(read_result.ok)
        self.assertIn("result_ref", read_result.llm_text)

    def test_llm_tool_aliases_route_to_internal_canonical_capabilities(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")

        shell = core.context.execution_runtime.execute_tool(
            new_tool_call(name="run_shell", args={"cmd": "echo alias-ok"})
        )
        search = core.context.execution_runtime.execute_tool(
            new_tool_call(name="search_tools", args={"query": "shell command", "top_k": 3})
        )
        read = core.context.execution_runtime.execute_tool(
            new_tool_call(name="read_tool", args={"name": "run_shell"})
        )
        compat = core.context.execution_runtime.execute_tool(
            new_tool_call(name="op_tool_read", args={"name": "op_exec_shell"})
        )

        self.assertTrue(shell.ok)
        self.assertEqual(shell.name, "run_shell")
        self.assertEqual(str(shell.structured["stdout"]).strip(), "alias-ok")
        self.assertTrue(search.ok)
        self.assertIn("run_shell", [item["alias"] for item in search.structured["hits"]])
        self.assertTrue(read.ok)
        self.assertEqual(read.structured["alias"], "run_shell")
        self.assertNotIn("[truncated]", read.structured["description"])
        self.assertIn("delete_path for deletion", read.structured["description"])
        self.assertNotIn("op_exec_shell", read.llm_text)
        self.assertFalse(compat.ok)
        self.assertEqual(compat.status, "unknown_tool")

    def test_detachable_proactive_module_round_trips_through_core_registry(self) -> None:
        core = PalCore()
        manager = ProactiveManager()
        register_proactive_with_core(core.context, manager)
        core.publish_module_capabilities("proactive")

        self.assertIn("proactive_status", core.context.capability_registry.descriptors)
        self.assertIn("proactive_list", core.context.capability_registry.descriptors)
        self.assertIn("proactive_create", core.context.capability_registry.descriptors)
        self.assertIn("proactive_delete", core.context.capability_registry.descriptors)
        self.assertNotIn("proactive_destroy", core.context.capability_registry.descriptors["proactive_delete"].aliases)
        self.assertIn("proactive_enable", core.context.capability_registry.descriptors)
        self.assertIn("proactive_disable", core.context.capability_registry.descriptors)
        self.assertIn("proactive_set_output_channel", core.context.capability_registry.descriptors)
        self.assertIn("proactive_set_output_target", core.context.capability_registry.descriptors)
        self.assertIn("proactive_update_schedule", core.context.capability_registry.descriptors)
        self.assertIn("proactive_attach", core.context.capability_registry.descriptors)
        self.assertIn("proactive_detach", core.context.capability_registry.descriptors)
        self.assertIn("proactive.triggers", core.context.event_source_registry.sources)
        self.assertIn(EventKind.PROACTIVE_TRIGGER, core.context.event_handler_registry.handlers)

        self.assertEqual(core.detach_module("proactive"), RuntimeStatus.OK)
        self.assertNotIn("proactive.triggers", core.context.event_source_registry.sources)
        self.assertNotIn(EventKind.PROACTIVE_TRIGGER, core.context.event_handler_registry.handlers)

    def test_proactive_management_capabilities_create_update_and_delete(self) -> None:
        runtime_root, database = self._create_database()
        try:
            core = PalCore()
            register_proactive_with_core(core.context, ProactiveManager())
            core.publish_module_capabilities("proactive")

            invalid = core.context.execution_runtime.execute(
                CapabilityCall(
                    name="proactive_create",
                    args={
                        "name": "bad_digest",
                        "goal": "Summarize repository updates",
                        "schedule": {"cadence": "daily", "hour": 9, "minute": 0, "timezone": "Asia/Shanghai"},
                    },
                )
            )
            self.assertEqual(invalid.status, "invalid")
            self.assertIn("schedule.cadence", invalid.text)

            created = core.context.execution_runtime.execute(
                CapabilityCall(
                    name="proactive_create",
                    args={
                        "name": "daily_digest",
                        "goal": "Summarize repository updates",
                        "method": "Review recent changes and produce a concise digest.",
                        "skill_refs": ["git", "summary"],
                        "out_channel_name": "socket_default",
                        "out_reply_target": {"session_id": "session-1", "request_id": "req-1"},
                        "schedule": {"cadence": "cron", "cron": "0 9 * * *", "timezone": "Asia/Shanghai"},
                    },
                )
            )
            self.assertEqual(created.status, "ok")

            listed = core.context.execution_runtime.execute(CapabilityCall(name="proactive_list"))
            self.assertEqual(listed.status, "ok")
            self.assertEqual(len(listed.structured["items"]), 1)
            self.assertEqual(listed.structured["items"][0]["proactive_id"], "daily_digest")
            self.assertEqual(listed.structured["items"][0]["out_channel_id"], "socket_default")
            self.assertEqual(listed.structured["items"][0]["out_reply_target"], {"session_id": "session-1", "request_id": "req-1"})

            changed_channel = core.context.execution_runtime.execute(
                CapabilityCall(
                    name="proactive_set_output_channel",
                    args={"name": "daily_digest", "out_channel_name": "telegram_main"},
                )
            )
            self.assertEqual(changed_channel.status, "ok")
            self.assertEqual(changed_channel.structured["out_channel_id"], "telegram_main")
            self.assertEqual(changed_channel.structured["out_reply_target"], {"session_id": "session-1", "request_id": "req-1"})

            changed_target = core.context.execution_runtime.execute(
                CapabilityCall(
                    name="proactive_set_output_target",
                    args={"name": "daily_digest", "out_reply_target": {"chat_id": "12345", "thread_id": "7"}},
                )
            )
            self.assertEqual(changed_target.status, "ok")
            self.assertEqual(changed_target.structured["out_reply_target"], {"chat_id": "12345", "thread_id": "7"})

            rescheduled = core.context.execution_runtime.execute(
                CapabilityCall(
                    name="proactive_update_schedule",
                    args={
                        "name": "daily_digest",
                        "schedule": {"cadence": "cron", "cron": "15 10 * * *", "timezone": "Asia/Shanghai"},
                    },
                )
            )
            self.assertEqual(rescheduled.status, "ok")
            self.assertIn("next_due_at", rescheduled.structured)

            disabled = core.context.execution_runtime.execute(
                CapabilityCall(name="proactive_disable", args={"name": "daily_digest"})
            )
            self.assertEqual(disabled.status, "ok")
            self.assertFalse(disabled.structured["enabled"])

            enabled = core.context.execution_runtime.execute(
                CapabilityCall(name="proactive_enable", args={"name": "daily_digest"})
            )
            self.assertEqual(enabled.status, "ok")
            self.assertTrue(enabled.structured["enabled"])

            deleted = core.context.execution_runtime.execute(
                CapabilityCall(name="proactive_delete", args={"name": "daily_digest"})
            )
            self.assertEqual(deleted.status, "ok")
            self.assertEqual(deleted.text, "proactive task deleted")
            after_delete = core.context.execution_runtime.execute(CapabilityCall(name="proactive_list"))
            self.assertEqual(after_delete.structured["items"], [])
        finally:
            database.close()
            shutil.rmtree(runtime_root, ignore_errors=True)

    def test_proactive_delete_is_found_and_legacy_destroy_alias_is_rejected(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        register_proactive_with_core(core.context, ProactiveManager())
        core.publish_module_capabilities("execution")
        core.publish_module_capabilities("proactive")

        search = core.context.execution_runtime.execute(
            CapabilityCall(name="op_tool_search", args={"query": "delete proactive", "top_k": 5})
        )
        self.assertEqual(search.status, "ok")
        hit_names = [item["alias"] for item in search.structured["hits"]]
        self.assertIn("proactive_delete", hit_names)

        created = core.context.execution_runtime.execute(
            CapabilityCall(
                name="proactive_create",
                args={"name": "old_alias", "goal": "Test alias deletion"},
            )
        )
        self.assertEqual(created.status, "ok")
        legacy = core.context.execution_runtime.execute(
            CapabilityCall(name="proactive_destroy", args={"target_id": "old_alias"})
        )
        self.assertEqual(legacy.status, "error")
        deleted = core.context.execution_runtime.execute(
            CapabilityCall(name="proactive_delete", args={"name": "old_alias"})
        )
        self.assertEqual(deleted.status, "ok")
        self.assertEqual(deleted.text, "proactive task deleted")

    def test_proactive_task_introspection_exposes_show_and_run_history(self) -> None:
        runtime_root, database = self._create_database()
        try:
            core = PalCore()
            manager = ProactiveManager(repository=importlib.import_module("pal.proactive").ProactiveRepository())
            runner = importlib.import_module("pal.proactive").ProactiveRunner(repository=manager.repository)
            register_proactive_with_core(core.context, manager, runner)
            core.publish_module_capabilities("proactive")

            manager.create_task(
                proactive_id="daily_digest",
                goal="Summarize repository updates",
                method="Review recent changes.",
                out_channel_id="socket_default",
                schedule={"cadence": "cron", "cron": "0 9 * * *", "timezone": "Asia/Shanghai"},
            )
            run_id = runner.begin_run(ProactiveTriggerEvent(proactive_id="daily_digest", trigger_kind="manual"))
            runner.complete_run(run_id, turn_id="turn-123", final_reply="Digest sent.")

            self.assertIn("proactive_show", core.context.capability_registry.descriptors)
            self.assertIn("proactive_last_run", core.context.capability_registry.descriptors)
            self.assertIn("proactive_list_runs", core.context.capability_registry.descriptors)

            shown = core.context.execution_runtime.execute(
                CapabilityCall(name="proactive_show", args={"name": "daily_digest"})
            )
            self.assertEqual(shown.status, "ok")
            self.assertEqual(shown.structured["proactive_id"], "daily_digest")
            self.assertEqual(shown.structured["out_channel_id"], "socket_default")

            latest = core.context.execution_runtime.execute(
                CapabilityCall(name="proactive_last_run", args={"name": "daily_digest"})
            )
            self.assertEqual(latest.status, "ok")
            self.assertEqual(latest.structured["run"]["turn_id"], "turn-123")
            self.assertEqual(latest.structured["run"]["output_summary"], "Digest sent.")

            history = core.context.execution_runtime.execute(
                CapabilityCall(name="proactive_list_runs", args={"name": "daily_digest", "limit": 5})
            )
            self.assertEqual(history.status, "ok")
            self.assertEqual(len(history.structured["items"]), 1)
            self.assertEqual(history.structured["items"][0]["proactive_run_id"], run_id)

            missing = core.context.execution_runtime.invoke_indirect_tool(
                new_tool_call(name="proactive_show", args={"name": "missing_task"})
            )
            self.assertIsInstance(missing, RejectedResult)
            self.assertEqual(missing.error_code, "unknown_target")
            self.assertEqual(missing.details["available_names"], ["daily_digest"])
            self.assertEqual(missing.affordances[0].tool, "proactive_list")
        finally:
            database.close()
            shutil.rmtree(runtime_root, ignore_errors=True)

    def test_instance_level_capability_uses_one_name_parameterized_tool(self) -> None:
        runtime_root, database = self._create_database()
        try:
            core = PalCore()
            channel_runtime = ChannelRuntime()
            repository = importlib.import_module("pal.channel.repository").ChannelEndpointRepository()
            repository.upsert(
                endpoint_id="socket_main",
                channel_kind="socket",
                binding_key=str(runtime_root / "pal.sock"),
                enabled=True,
            )
            register_channel_with_core(core.context, channel_runtime)
            core.publish_module_capabilities("channel")

            descriptor = core.context.capability_registry.descriptors["channel_endpoint_inspect"]
            input_schema = descriptor.InputModel.model_json_schema(mode="validation")
            target_schema = input_schema["properties"]["name"]

            self.assertEqual(target_schema["type"], "string")
            self.assertIn("channel_list", target_schema["description"])
            self.assertIn("name", input_schema["required"])

            missing_target = core.context.execution_runtime.execute(
                CapabilityCall(name="intro_endpoint_channel_inspect")
            )
            self.assertEqual(missing_target.status, "invalid")
            self.assertEqual(missing_target.structured["available_names"], ["socket_main"])
            self.assertEqual(missing_target.structured["error_code"], "target_name_required")
            self.assertIn("socket_main", missing_target.llm_text)
            self.assertIn("Retry with args.name", missing_target.llm_text)

            resolved = core.context.execution_runtime.execute(
                CapabilityCall(
                    name="channel_endpoint_inspect",
                    args={"name": "socket_main"},
                )
            )
            self.assertEqual(resolved.status, "ok")
            self.assertEqual(resolved.structured["endpoint_id"], "socket_main")
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
        reply_id = channel_runtime.queue_reply(
            TurnDeliveryBinding.from_envelope(envelope, control_scope_key="stdio"),
            "world",
        )
        delivered = ChannelEventSource(runtime=channel_runtime).drain(core.context)

        self.assertEqual(reply_id != "", True)
        self.assertEqual(adapter.sent, [("stdio", "world")])
        self.assertEqual([item.event_kind for item in delivered], ["reply.delivered"])

    def test_channel_outbox_reports_missing_endpoint_once_and_delivers_after_restore(self) -> None:
        channel_runtime = ChannelRuntime()
        envelope = ChannelEnvelope(
            event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "hello"}),
            endpoint=EndpointConfig(endpoint_id="telegram_main", channel_kind="telegram", binding_key="chat:1"),
            response_handle=ResponseHandle(endpoint_id="telegram_main", reply_target={"chat_id": "1"}),
        )
        reply_id = channel_runtime.queue_reply(
            TurnDeliveryBinding.from_envelope(envelope, control_scope_key="telegram:1"),
            "world",
        )

        channel_runtime.flush_outbox()
        first = channel_runtime.mailbox.drain()
        channel_runtime.flush_outbox()
        second = channel_runtime.mailbox.drain()

        self.assertEqual([event.event_kind for event in first], ["reply.failed"])
        self.assertEqual(first[0].payload["reply_id"], reply_id)
        self.assertEqual(second, [])
        self.assertEqual(len(channel_runtime.outbox), 1)

        endpoint = StubEndpoint(endpoint=envelope.endpoint)
        channel_runtime.register_endpoint(endpoint)
        channel_runtime.flush_outbox()
        self.assertFalse(channel_runtime.outbox)
        self.assertEqual(len(endpoint.outbox), 1)

        channel_runtime.flush_outbox()
        delivered = channel_runtime.mailbox.drain()
        self.assertEqual(endpoint.sent, [("telegram_main", "world")])
        self.assertEqual([event.event_kind for event in delivered], ["reply.delivered"])
        self.assertEqual(delivered[0].payload["reply_id"], reply_id)

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

    def test_channel_endpoint_queue_reports_same_transient_failure_once(self) -> None:
        endpoint = StubEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="telegram_main",
                channel_kind="telegram",
                binding_key="chat:1",
            )
        )
        endpoint.disable()
        endpoint.queue_reply("world")

        first = endpoint.flush_outbox()
        second = endpoint.flush_outbox()

        self.assertEqual([event.event_kind for event in first], ["reply.failed"])
        self.assertEqual(second, [])
        self.assertEqual(len(endpoint.outbox), 1)

        endpoint.enable()
        delivered = endpoint.flush_outbox()
        self.assertEqual(endpoint.sent, [("telegram_main", "world")])
        self.assertEqual([event.event_kind for event in delivered], ["reply.delivered"])

    def test_channel_endpoint_capabilities_cover_auth_health_and_backlog_without_attach(self) -> None:
        runtime_root, database = self._create_database()
        try:
            core = PalCore()
            channel_runtime = ChannelRuntime()
            repository = importlib.import_module("pal.channel.repository").ChannelEndpointRepository()
            repository.upsert(
                endpoint_id="socket_main",
                channel_kind="socket",
                binding_key=str(runtime_root / "runtime.sock"),
                enabled=True,
            )
            endpoint = StubEndpoint(
                endpoint=EndpointConfig(endpoint_id="socket_main", channel_kind="socket", binding_key=str(runtime_root / "runtime.sock"))
            )
            channel_runtime.register_endpoint(endpoint)
            register_channel_with_core(core.context, channel_runtime)

            published = core.publish_module_capabilities("channel")

            self.assertIn("channel_endpoint_inspect", published)
            self.assertIn("channel_endpoint_auth_state", published)
            self.assertIn("channel_endpoint_set_auth_material", published)
            self.assertIn("channel_endpoint_backlog", published)
            self.assertIn("channel_endpoint_health", published)
            self.assertNotIn("channel_endpoint_attach", published)
            self.assertNotIn("channel_endpoint_detach", published)

            configured = core.context.execution_runtime.execute(
                CapabilityCall(
                    name="channel_endpoint_set_auth_material",
                    args={"name": "socket_main", "material": {"bot_token": "secret-token", "authorized": True}},
                )
            )
            self.assertEqual(configured.status, "ok")
            self.assertNotIn("token", configured.structured)
            self.assertEqual(configured.structured["accepted_keys"], ["authorized", "bot_token"])

            auth_state = core.context.execution_runtime.execute(
                CapabilityCall(
                    name="channel_endpoint_auth_state",
                    args={"name": "socket_main"},
                )
            )
            self.assertEqual(auth_state.status, "ok")
            self.assertNotIn("token", auth_state.structured)
            self.assertTrue(auth_state.structured["authorized"])

            health = core.context.execution_runtime.execute(
                CapabilityCall(
                    name="channel_endpoint_health",
                    args={"name": "socket_main"},
                )
            )
            self.assertEqual(health.status, "ok")
            self.assertNotIn("token", health.structured)
            self.assertTrue(health.structured["healthy"])

            backlog = core.context.execution_runtime.execute(
                CapabilityCall(
                    name="channel_endpoint_backlog",
                    args={"name": "socket_main"},
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
                endpoint_id="socket_main",
                channel_kind="socket",
                binding_key=str(runtime_root / "runtime.sock"),
                enabled=True,
            )
            endpoint = StubEndpoint(
                endpoint=EndpointConfig(endpoint_id="socket_main", channel_kind="socket", binding_key=str(runtime_root / "runtime.sock"))
            )
            channel_runtime.register_endpoint(endpoint)
            register_channel_with_core(core.context, channel_runtime)
            core.publish_module_capabilities("channel")

            detached = core.context.execution_runtime.execute(
                CapabilityCall(name="channel_detach", args={"name": "socket_main"})
            )
            self.assertEqual(detached.status, "ok")
            self.assertFalse(endpoint.attached)

            blocked = endpoint.accept_raw({"text": "hello"}, event_kind="user.message")
            self.assertIsNone(blocked)

            attached = core.context.execution_runtime.execute(
                CapabilityCall(name="channel_attach", args={"name": "socket_main"})
            )
            self.assertEqual(attached.status, "ok")
            active_endpoint = channel_runtime.get_endpoint("socket_main")
            self.assertIsNotNone(active_endpoint)
            self.assertIsNot(active_endpoint, endpoint)
            self.assertTrue(active_endpoint.attached)
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
            [generation_result_from_values(text="final answer", tool_calls=[], finish_reason="stop")]
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
        transcript = [
            message
            for item in memory_service.l1_store.items
            for message in item
        ]
        self.assertEqual(
            [(message.role, message.content, message.kind) for message in transcript],
            [
                ("user", "hello", L1MessageKind.USER_REQUEST),
                ("assistant", "final answer", L1MessageKind.ASSISTANT_REPLY),
            ],
        )

    def test_turn_runtime_keeps_reasoning_out_of_final_reply_and_l1(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        channel_runtime = ChannelRuntime()
        register_channel_with_core(core.context, channel_runtime)
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        register_memory_with_core(core.context, memory_service)
        core.context.port_registry["llm:llm"] = ScriptedLLMRuntime(
            [generation_result_from_values(text="final answer", reasoning_text="thinking here", tool_calls=[], finish_reason="stop")]
        )

        outcome = core.process_channel_turn(
            ChannelEnvelope(
                event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "hello"}),
                endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
                response_handle=ResponseHandle(endpoint_id="stdio"),
            )
        )

        self.assertEqual(outcome.final_reply, "final answer")
        self.assertNotIn("thinking here", outcome.final_reply)
        transcript = [
            message
            for item in memory_service.l1_store.items
            for message in item
        ]
        self.assertEqual(
            [(message.role, message.content, message.kind) for message in transcript],
            [
                ("user", "hello", L1MessageKind.USER_REQUEST),
                ("assistant", "final answer", L1MessageKind.ASSISTANT_REPLY),
            ],
        )

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
        register_test_tool(core.context.execution_runtime, EchoTool())
        core.context.port_registry["llm:llm"] = ScriptedLLMRuntime(
            [
                generation_result_from_values(
                    text="",
                    reasoning_text="thinking",
                    tool_calls=[new_tool_call(name="echo", args={"value": "same"})],
                    finish_reason="tool_calls",
                ),
                generation_result_from_values(text="final answer", tool_calls=[], finish_reason="stop"),
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
        self.assertIn(("stdio", "op_tool_call", "echo"), endpoint.streamed)
        self.assertIn(("stdio", "done", "stop"), endpoint.streamed)
        self.assertFalse(endpoint.sent)
        self.assertIn("final answer", outcome.final_reply)

    def test_streaming_channel_synthesizes_empty_model_terminal_on_same_stream(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        channel_runtime = ChannelRuntime()
        endpoint = StubEndpoint(
            endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin")
        )
        channel_runtime.register_endpoint(endpoint)
        register_channel_with_core(core.context, channel_runtime)
        memory_service = MemoryService(
            l3_selector=L3ProviderSelector(
                resolver=core.context.execution_runtime.l3_plugin_registry.require
            )
        )
        register_memory_with_core(core.context, memory_service)
        core.context.port_registry["llm:llm"] = ScriptedLLMRuntime(
            [generation_result_from_values(text="", tool_calls=[], finish_reason="stop")]
        )

        outcome = core.process_channel_turn(
            ChannelEnvelope(
                event=EventEnvelope(
                    event_kind="user.message",
                    source_kind="channel",
                    payload={"text": "hello"},
                ),
                endpoint=endpoint.endpoint,
                response_handle=ResponseHandle(endpoint_id="stdio"),
            )
        )

        channel_runtime.sync_endpoints()
        self.assertIn("without producing a final answer", outcome.final_reply)
        self.assertIn(
            ("stdio", "text", outcome.final_reply),
            endpoint.streamed,
        )
        self.assertIn(("stdio", "done", "fallback"), endpoint.streamed)
        self.assertFalse(endpoint.sent)

    def test_turn_runtime_uses_response_mode_to_coarsely_tune_temperature(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        channel_runtime = ChannelRuntime()
        register_channel_with_core(core.context, channel_runtime)
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        register_memory_with_core(core.context, memory_service)
        scripted_llm = ScriptedLLMRuntime(
            [
                generation_result_from_values(
                    text="",
                    tool_calls=[new_tool_call(name="echo", args={"value": "mode"})],
                    finish_reason="tool_calls",
                    response_mode="review",
                ),
                generation_result_from_values(text="final answer", tool_calls=[], finish_reason="stop"),
            ]
        )
        core.context.port_registry["llm:llm"] = scripted_llm
        register_test_tool(core.context.execution_runtime, EchoTool())

        core.process_channel_turn(
            ChannelEnvelope(
                event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "hello"}),
                endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
                response_handle=ResponseHandle(endpoint_id="stdio"),
            )
        )

        generate_requests = [request for kind, request in scripted_llm.requests if kind in {"generate", "astream"}]
        self.assertEqual(generate_requests[0].policy.temperature, 0.7)
        self.assertEqual(generate_requests[1].policy.temperature, 0.2)

    def test_tool_results_are_returned_via_standard_assistant_and_tool_messages(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        channel_runtime = ChannelRuntime()
        register_channel_with_core(core.context, channel_runtime)
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        register_memory_with_core(core.context, memory_service)
        scripted_llm = ScriptedLLMRuntime(
            [
                generation_result_from_values(
                    text="",
                    reasoning_text="hidden protocol reasoning",
                    tool_calls=[new_tool_call(name="echo", args={"value": "proto"})],
                    finish_reason="tool_calls",
                ),
                generation_result_from_values(text="final answer", tool_calls=[], finish_reason="stop"),
            ]
        )
        core.context.port_registry["llm:llm"] = scripted_llm
        register_test_tool(core.context.execution_runtime, EchoTool())

        core.process_channel_turn(
            ChannelEnvelope(
                event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "hello"}),
                endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
                response_handle=ResponseHandle(endpoint_id="stdio"),
            )
        )

        generate_requests = [request for kind, request in scripted_llm.requests if kind in {"generate", "astream"}]
        self.assertGreaterEqual(len(generate_requests), 2)
        followup_messages = generate_requests[1].messages
        assistant_tool_message = next(
            message
            for message in followup_messages
            if message.role.value == "assistant" and message.tool_calls
        )
        self.assertEqual(assistant_tool_message.tool_calls[0].name, "echo")
        self.assertEqual(dict(assistant_tool_message.tool_calls[0].arguments), {"value": "proto"})
        self.assertEqual(assistant_tool_message.reasoning_text, "hidden protocol reasoning")
        tool_message = next(message for message in followup_messages if message.role.value == "tool")
        self.assertIn("stable-result", str(tool_message.parts[0].content))
        system_message = next(message for message in followup_messages if message.role.value == "system")
        self.assertNotIn("Tool Observation", system_message.text)

    def test_closed_turn_tool_history_is_provider_neutral_on_next_turn(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        channel_runtime = ChannelRuntime()
        register_channel_with_core(core.context, channel_runtime)
        memory_service = MemoryService(
            l3_selector=L3ProviderSelector(
                resolver=core.context.execution_runtime.l3_plugin_registry.require
            )
        )
        register_memory_with_core(core.context, memory_service)
        scripted_llm = ScriptedLLMRuntime(
            [
                generation_result_from_values(
                    text="",
                    reasoning_text="ephemeral provider reasoning",
                    tool_calls=[
                        new_tool_call(
                            name="echo",
                            args={"value": "first turn"},
                        )
                    ],
                    finish_reason="tool_calls",
                ),
                generation_result_from_values(
                    text="first final",
                    tool_calls=[],
                    finish_reason="stop",
                ),
                generation_result_from_values(
                    text="second final",
                    tool_calls=[],
                    finish_reason="stop",
                ),
            ]
        )
        core.context.port_registry["llm:llm"] = scripted_llm
        register_test_tool(core.context.execution_runtime, EchoTool())

        def run_turn(text: str) -> None:
            core.process_channel_turn(
                ChannelEnvelope(
                    event=EventEnvelope(
                        event_kind="user.message",
                        source_kind="channel",
                        payload={"text": text},
                    ),
                    endpoint=EndpointConfig(
                        endpoint_id="stdio",
                        channel_kind="stdio",
                        binding_key="stdin",
                    ),
                    response_handle=ResponseHandle(endpoint_id="stdio"),
                )
            )

        run_turn("first request")
        first_followup = [
            request
            for kind, request in scripted_llm.requests
            if kind in {"generate", "astream"}
        ][1]
        active_tool_call = next(
            message
            for message in first_followup.messages
            if message.role.value == "assistant" and message.tool_calls
        )
        self.assertEqual(active_tool_call.reasoning_text, "ephemeral provider reasoning")

        run_turn("second request")
        second_request = [
            request
            for kind, request in scripted_llm.requests
            if kind in {"generate", "astream"}
        ][2]
        closed_call = next(
            message
            for message in second_request.messages
            if message.role.value == "assistant" and message.tool_calls
        )
        closed_result = next(
            message
            for message in second_request.messages
            if message.role.value == "tool"
        )
        self.assertEqual(closed_call.tool_calls[0].args["value"], "first turn")
        self.assertIn("full result retired", closed_result.text)
        self.assertNotIn("stable-result", closed_result.text)
        self.assertNotIn("ephemeral provider reasoning", closed_call.reasoning_text)
        self.assertNotIn(
            "ephemeral provider reasoning",
            json.dumps(
                [
                    message.payload
                    for transcript in memory_service.l1_store.items
                    for message in transcript
                ],
                ensure_ascii=False,
            ),
        )

    def test_tool_protocol_prefers_llm_text_over_summary_text(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        channel_runtime = ChannelRuntime()
        register_channel_with_core(core.context, channel_runtime)
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        register_memory_with_core(core.context, memory_service)
        scripted_llm = ScriptedLLMRuntime(
            [
                generation_result_from_values(
                    text="",
                    tool_calls=[new_tool_call(name="verbose", args={"value": "proto"})],
                    finish_reason="tool_calls",
                ),
                generation_result_from_values(text="final answer", tool_calls=[], finish_reason="stop"),
            ]
        )
        core.context.port_registry["llm:llm"] = scripted_llm
        register_test_tool(core.context.execution_runtime, VerboseTool())

        core.process_channel_turn(
            ChannelEnvelope(
                event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "hello"}),
                endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
                response_handle=ResponseHandle(endpoint_id="stdio"),
            )
        )

        generate_requests = [request for kind, request in scripted_llm.requests if kind in {"generate", "astream"}]
        self.assertGreaterEqual(len(generate_requests), 2)
        followup_messages = generate_requests[1].messages
        tool_message = next(message for message in followup_messages if message.role.value == "tool")
        self.assertIn('"value": "rich"', tool_message.text)
        self.assertNotIn("placeholder", tool_message.text)

    def test_default_tool_result_text_pretty_prints_structured_fallback(self) -> None:
        result = type(
            "Result",
            (),
            {
                "ok": True,
                "llm_text": "",
                "text": "",
                "structured": {"z": 2, "a": {"nested": True}},
            },
        )()

        rendered = default_tool_result_text(result)

        self.assertIn('{\n  "a": {', rendered)
        self.assertIn('"nested": true', rendered)

    def test_l3_recall_tool_protocol_uses_minimal_observation_text(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        register_execution_with_core(core.context)
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
                    "title": "Test user profile",
                    "summary": "The test user built Pal and wants it to act directly.",
                    "rendered": "The test user built Pal and wants it to act directly.",
                    "search_text": "The test user built Pal after being unhappy with a legacy assistant and wants Pal to act directly.",
                }
            ]
        )
        register_l3_with_core(core.context, mock_l3)
        core.publish_module_capabilities("execution")
        core.publish_module_capabilities(mock_l3.module_id)
        scripted_llm = ScriptedLLMRuntime(
            [
                generation_result_from_values(
                    text="",
                    reasoning_text="hidden recall reasoning",
                    tool_calls=[
                        new_tool_call(
                            name="call_tool",
                            args={
                                "name": "memory_provider_recall",
                                "args": {"name": "mock_l3", "queries": ["test_user"], "view": "summary"},
                            },
                        )
                    ],
                    finish_reason="tool_calls",
                ),
                generation_result_from_values(text="final answer", tool_calls=[], finish_reason="stop"),
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

        generate_requests = [request for kind, request in scripted_llm.requests if kind in {"generate", "astream"}]
        self.assertGreaterEqual(len(generate_requests), 2)
        tool_message = next(message for message in generate_requests[1].messages if message.role.value == "tool")
        self.assertIn('<recalled_memories view="summary">', tool_message.text)
        self.assertIn("[fact:1]:", tool_message.text)
        self.assertIn('"kind": "complete"', tool_message.text)
        self.assertNotIn("legacy assistant", tool_message.text)
        self.assertIn("The test user built Pal and wants it to act directly.", tool_message.text)

    def test_malformed_tool_result_does_not_crash_turn_runtime(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        channel_runtime = ChannelRuntime()
        register_channel_with_core(core.context, channel_runtime)
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        register_memory_with_core(core.context, memory_service)
        scripted_llm = ScriptedLLMRuntime(
            [
                generation_result_from_values(
                    text="",
                    tool_calls=[new_tool_call(name="malformed", args={})],
                    finish_reason="tool_calls",
                ),
                generation_result_from_values(text="final answer", tool_calls=[], finish_reason="stop"),
            ]
        )
        core.context.port_registry["llm:llm"] = scripted_llm
        register_test_tool(core.context.execution_runtime, MalformedTool())

        outcome = core.process_channel_turn(
            ChannelEnvelope(
                event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "hello"}),
                endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
                response_handle=ResponseHandle(endpoint_id="stdio"),
            )
        )

        self.assertTrue(str(outcome.final_reply or "").strip())
        generate_requests = [request for kind, request in scripted_llm.requests if kind in {"generate", "astream"}]
        self.assertGreaterEqual(len(generate_requests), 2)
        tool_message = next(
            message
            for request in reversed(generate_requests)
            for message in request.messages
            if message.role.value == "tool"
        )
        self.assertIn('{"bad": true}', tool_message.parts[0].content)
        self.assertNotIn("memory recall", tool_message.parts[0].content)

    def test_stagnation_guard_forces_finalization_only_and_strips_tools(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        channel_runtime = ChannelRuntime()
        register_channel_with_core(core.context, channel_runtime)
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        register_memory_with_core(core.context, memory_service)
        scripted_llm = ScriptedLLMRuntime(
            [
                generation_result_from_values(text="", tool_calls=[new_tool_call(name="echo", args={"value": "same"})], finish_reason="tool_calls"),
                generation_result_from_values(text="", tool_calls=[new_tool_call(name="echo", args={"value": "same"})], finish_reason="tool_calls"),
                generation_result_from_values(text="", tool_calls=[new_tool_call(name="echo", args={"value": "same"})], finish_reason="tool_calls"),
                generation_result_from_values(text="", tool_calls=[new_tool_call(name="echo", args={"value": "same"})], finish_reason="tool_calls"),
            ]
        )
        core.context.port_registry["llm:llm"] = scripted_llm
        register_test_tool(core.context.execution_runtime, EchoTool())

        outcome = core.process_channel_turn(
            ChannelEnvelope(
                event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "loop"}),
                endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
                response_handle=ResponseHandle(endpoint_id="stdio"),
            )
        )

        generate_requests = [request for kind, request in scripted_llm.requests if kind in {"generate", "astream"}]
        self.assertEqual(generate_requests[-1].tools, ())
        self.assertIn("<operating_rules>", generate_requests[-1].messages[0].text)
        self.assertIn("Finalization Directive", generate_requests[-1].messages[-1].text)
        self.assertEqual(
            generate_requests[-1].messages[-1].prompt_region.value,
            "active_dynamic",
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
                    reserved_output_tokens=request.request.policy.max_output_tokens,
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
                    return generation_result_from_values(
                        text="",
                        tool_calls=[new_tool_call(name="huge", args={"value": "budget"})],
                        finish_reason="tool_calls",
                    )
                return generation_result_from_values(text="final answer", tool_calls=[], finish_reason="stop")

            async def agenerate(self, request):
                self.requests.append(("agenerate", request))
                if "compaction" not in str(
                    request.metadata.get("purpose") or ""
                ):
                    return self.generate(request)
                return generation_result_from_values(
                    text=json.dumps(
                        {
                            "schema": "pal.compaction.pal.v2",
                            "kind": "pal",
                            "summary": {
                                "summary": "Compacted fallback context.",
                                "search_text": "Compacted fallback context.",
                            },
                            "continuity": {
                                "current_focus": "resume after compaction",
                                "primary_request_and_intent": "finish the active turn",
                                "active_operating_instructions": [],
                                "active_requests": [],
                                "temporary_task_state": [],
                                "key_decisions": [],
                                "pending_questions": [],
                                "recent_raw_turns": [],
                                "warm_compressed_turns": [],
                                "retired_or_superseded_context": [],
                                "optional_next_step": "retry the active request",
                            },
                            "memory_candidates": [],
                        }
                    ),
                    tool_calls=[],
                    finish_reason="stop",
                )

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
            register_test_tool(core.context.execution_runtime, HugeTool(size=80_000))

            outcome = core.process_channel_turn(
                ChannelEnvelope(
                    event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "hello"}),
                    endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
                    response_handle=ResponseHandle(endpoint_id="stdio"),
                )
        )

        self.assertEqual(outcome.final_reply, "final answer")
        generate_requests = [request for kind, request in scripted_llm.requests if kind == "generate"]
        self.assertNotIn("Finalization Directive", generate_requests[-1].messages[0].text)
        tool_message = next(message for message in generate_requests[-1].messages if message.role.value == "tool")
        self.assertIn('"kind": "paged"', tool_message.text)
        self.assertIn('"tool": "read_tool_result"', tool_message.text)

    def test_turn_runtime_degrades_older_tool_results_when_current_turn_group_exceeds_limit(self) -> None:
        class MultiToolLLMRuntime:
            def __init__(self) -> None:
                self.requests = []
                self.generate_count = 0

            def preflight(self, request) -> LLMPreflightAdvice:
                self.requests.append(("preflight", request))
                return LLMPreflightAdvice(
                    status="ready",
                    active_model=request.request.model_hint or "primary-model",
                    fallback_chain=[],
                    target_input_budget=2048,
                    reserved_output_tokens=request.request.policy.max_output_tokens,
                )

            def generate(self, request):
                self.requests.append(("generate", request))
                self.generate_count += 1
                if self.generate_count == 1:
                    return generation_result_from_values(
                        text="",
                        tool_calls=[new_tool_call(name="huge_a", args={"value": "proto-a"})],
                        finish_reason="tool_calls",
                    )
                if self.generate_count == 2:
                    return generation_result_from_values(
                        text="",
                        tool_calls=[new_tool_call(name="huge_b", args={"value": "proto-b"})],
                        finish_reason="tool_calls",
                    )
                return generation_result_from_values(text="final answer", tool_calls=[], finish_reason="stop")

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
        register_test_tool(core.context.execution_runtime, type("HugeATool", (HugeTool,), {"name": "huge_a"})(size=12_000))
        register_test_tool(core.context.execution_runtime, type("HugeBTool", (HugeTool,), {"name": "huge_b"})(size=12_000))

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
        tool_messages = [message for message in retry_request.messages if message.role.value == "tool"]
        self.assertEqual(len(tool_messages), 2)
        self.assertIn("[preview only:", tool_messages[0].parts[0].content)
        self.assertNotIn("[preview only:", tool_messages[1].parts[0].content)

    def test_execution_runtime_pages_large_tool_results_in_memory(self) -> None:
        core = PalCore()
        with tempfile.TemporaryDirectory() as tmpdir:
            core.context.execution_runtime.runtime_root = Path(tmpdir)
            register_test_tool(core.context.execution_runtime, HugeTool(size=60_000))
            result = core.context.execution_runtime.execute_tool(
                new_tool_call(name="huge", args={"value": "spill"}, call_id="call_spill"),
                budget=ToolCallBudget(
                    max_output_chars=10_000,
                    max_output_tokens_estimate=25_000,
                    max_result_spill_chars=50_000,
                    preview_chars=500,
                    artifact_bucket_id="turn_spill",
                ),
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.structured["kind"], "paged")
            result_handle = result.structured["result_handle"]
            self.assertEqual(result_handle["result_ref"], "call_spill")
            self.assertNotIn("backing_path", result_handle)
            self.assertIn('"result_ref": "call_spill"', result.llm_text)
            self.assertIn('"tool": "read_tool_result"', result.llm_text)
            self.assertNotIn("backing_path", result.llm_text)
            self.assertNotIn(str(tmpdir), result.llm_text)

    def test_execution_runtime_budget_fallback_without_runtime_root(self) -> None:
        core = PalCore()
        core.context.execution_runtime.runtime_root = None
        register_test_tool(core.context.execution_runtime, HugeTool(size=60_000))
        result = core.context.execution_runtime.execute_tool(
            new_tool_call(name="huge", args={"value": "head-tail"}),
            budget=ToolCallBudget(max_output_chars=2_000, preview_chars=1_000),
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.structured["kind"], "paged")
        self.assertTrue(str(result.structured["result_handle"]["result_ref"]))
        self.assertIn('"kind": "paged"', result.llm_text)

    def test_shell_output_uses_runtime_pager_without_tool_level_truncation(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")
        with tempfile.TemporaryDirectory() as tmpdir:
            core.context.execution_runtime.runtime_root = Path(tmpdir)
            result = core.context.execution_runtime.execute_tool(
                new_tool_call(
                    name="run_shell",
                    args={
                        "cmd": (
                            "python - <<'PY'\n"
                            "print('SHELL-PAGER-HEAD')\n"
                            "print('x' * 20000)\n"
                            "print('SHELL-PAGER-TAIL')\n"
                            "PY"
                        )
                    },
                    call_id="call_shell_pager",
                ),
                budget=ToolCallBudget(max_output_chars=2_000, preview_chars=1_000, artifact_bucket_id="turn_shell_pager"),
            )
            page_count = int(result.structured["result_handle"]["page_count"])
            later = core.context.execution_runtime.execute_tool(
                new_tool_call(
                    name="read_tool_result",
                    args={"result_ref": "call_shell_pager", "page": page_count},
                )
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.structured["kind"], "paged")
        self.assertIn("SHELL-PAGER-HEAD", result.llm_text)
        self.assertTrue(later.ok)
        self.assertIn("SHELL-PAGER-TAIL", later.llm_text)

    def test_execution_runtime_tool_result_page_reads_later_page(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")
        with tempfile.TemporaryDirectory() as tmpdir:
            core.context.execution_runtime.runtime_root = Path(tmpdir)
            register_test_tool(core.context.execution_runtime, HeadTailHugeTool())
            result = core.context.execution_runtime.execute_tool(
                new_tool_call(name="head_tail_huge", args={}, call_id="call_head_tail"),
                budget=ToolCallBudget(max_output_chars=2_000, preview_chars=1_000, artifact_bucket_id="turn_page"),
            )
            page_count = int(result.structured["result_handle"]["page_count"])
            later = core.context.execution_runtime.execute_tool(
                new_tool_call(
                    name="read_tool_result",
                    args={"result_ref": "call_head_tail", "page": page_count},
                )
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.structured["kind"], "paged")
        self.assertIn("HEAD-SIGNAL", result.llm_text)
        self.assertTrue(later.ok)
        self.assertEqual(later.structured["page"], page_count)
        self.assertIn("page_text", later.structured)
        self.assertIn("TAIL-SIGNAL", later.llm_text)
        self.assertNotIn("backing_path", later.llm_text)

    def test_execution_runtime_tool_result_page_reads_tail_anchor(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")
        with tempfile.TemporaryDirectory() as tmpdir:
            core.context.execution_runtime.runtime_root = Path(tmpdir)
            register_test_tool(core.context.execution_runtime, HeadTailHugeTool())
            result = core.context.execution_runtime.execute_tool(
                new_tool_call(name="head_tail_huge", args={}, call_id="call_tail_anchor"),
                budget=ToolCallBudget(max_output_chars=2_000, preview_chars=1_000, artifact_bucket_id="turn_tail_anchor"),
            )
            tail = core.context.execution_runtime.execute_tool(
                new_tool_call(
                    name="read_tool_result",
                    args={"result_ref": "call_tail_anchor", "anchor": "tail"},
                )
            )
            second_from_tail = core.context.execution_runtime.execute_tool(
                new_tool_call(
                    name="read_tool_result",
                    args={"result_ref": "call_tail_anchor", "tail": True, "page": 2},
                )
            )

        self.assertTrue(result.ok)
        self.assertTrue(tail.ok)
        self.assertEqual(tail.structured["anchor"], "tail")
        self.assertEqual(tail.structured["anchor_page"], 1)
        self.assertFalse(tail.structured["has_more_after"])
        self.assertTrue(tail.structured["has_more_before"])
        self.assertIn("TAIL-SIGNAL", tail.llm_text)
        self.assertEqual(
            tail.invocation_result.affordances[0].arguments,
            {"result_ref": "call_tail_anchor", "page": 2, "anchor": "tail"},
        )
        self.assertTrue(second_from_tail.ok)
        self.assertEqual(second_from_tail.structured["anchor"], "tail")
        self.assertEqual(second_from_tail.structured["anchor_page"], 2)
        self.assertEqual(
            second_from_tail.invocation_result.affordances[0].arguments,
            {"result_ref": "call_tail_anchor", "page": 1, "anchor": "tail"},
        )

    def test_execution_runtime_tool_result_page_expires_after_retention_turns(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")
        with tempfile.TemporaryDirectory() as tmpdir:
            core.context.execution_runtime.runtime_root = Path(tmpdir)
            register_test_tool(core.context.execution_runtime, HugeTool(size=60_000))
            core.context.execution_runtime.begin_tool_result_turn(turn_id="turn_1", retention_user_turns=5)
            core.context.execution_runtime.execute_tool(
                new_tool_call(name="huge", args={"value": "expire"}, call_id="call_expire"),
                budget=ToolCallBudget(max_output_chars=2_000, preview_chars=1_000, artifact_bucket_id="turn_1"),
            )

            for index in range(2, 7):
                core.context.execution_runtime.begin_tool_result_turn(turn_id=f"turn_{index}", retention_user_turns=5)
            expired = core.context.execution_runtime.execute_tool(
                new_tool_call(name="read_tool_result", args={"result_ref": "call_expire", "page": 1})
            )

        self.assertFalse(expired.ok)
        self.assertEqual(expired.structured["details"]["reason"], "expired_handle")

    def test_expired_pager_recovery_replays_reads_but_never_mutations(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")
        runtime = core.context.execution_runtime

        def details(alias: str, arguments: dict[str, object]) -> dict[str, object]:
            record = runtime.registry_generation.record_for_alias(alias)
            self.assertIsNotNone(record)
            return {
                "origin": {
                    "alias": alias,
                    "arguments": arguments,
                    "invocation_mode": record.execution.invocation_mode.value,
                    "execution": record.execution.model_dump(mode="json"),
                }
            }

        read_recovery = runtime._pager_recovery_affordances(
            details("read_file", {"path": "/tmp/example.txt"})
        )
        write_recovery = runtime._pager_recovery_affordances(
            details("write_file", {"path": "/tmp/example.txt", "content": "new"})
        )

        self.assertEqual(read_recovery[0].tool, "read_file")
        self.assertIn("idempotent read", read_recovery[0].reason)
        self.assertEqual(write_recovery[0].tool, "read_tool")
        self.assertEqual(write_recovery[0].arguments, {"name": "write_file"})
        self.assertNotEqual(write_recovery[0].tool, "write_file")
        self.assertIn("do not automatically repeat", write_recovery[0].reason)

    def test_turn_runtime_recompacts_when_generate_requests_budget_for_fallback_endpoint(self) -> None:
        class FallbackBudgetLLMRuntime:
            def __init__(self) -> None:
                self.requests = []
                self.generate_count = 0

            def preflight(self, request) -> LLMPreflightAdvice:
                self.requests.append(("preflight", request))
                return LLMPreflightAdvice(
                    status="ready",
                    active_model=request.request.model_hint or "stub-model",
                    fallback_chain=[],
                    target_input_budget=2048,
                    reserved_output_tokens=request.request.policy.max_output_tokens,
                )

            def generate(self, request):
                self.requests.append(("generate", request))
                self.generate_count += 1
                if self.generate_count == 1:
                    return generation_result_from_values(
                        text="",
                        tool_calls=[],
                        finish_reason="compact_required",
                        target_input_budget=256,
                        reserved_output_tokens=64,
                        preferred_endpoint_id="fallback-small",
                        preferred_model_id="fallback-small-model",
                    )
                return generation_result_from_values(text="final answer", tool_calls=[], finish_reason="stop")

            async def agenerate(self, request):
                self.requests.append(("agenerate", request))
                if "compaction" not in str(
                    request.metadata.get("purpose") or ""
                ):
                    return self.generate(request)
                return generation_result_from_values(
                    text=json.dumps(
                        {
                            "schema": "pal.compaction.pal.v2",
                            "kind": "pal",
                            "summary": {
                                "summary": "Compacted fallback context.",
                                "search_text": "Compacted fallback context.",
                            },
                            "continuity": {
                                "current_focus": "resume after compaction",
                                "primary_request_and_intent": "finish the active turn",
                                "active_operating_instructions": [],
                                "active_requests": [],
                                "temporary_task_state": [],
                                "key_decisions": [],
                                "pending_questions": [],
                                "recent_raw_turns": [],
                                "warm_compressed_turns": [],
                                "retired_or_superseded_context": [],
                                "optional_next_step": "retry the active request",
                            },
                            "memory_candidates": [],
                        }
                    ),
                    tool_calls=[],
                    finish_reason="stop",
                )

        core = PalCore()
        register_core_with_core(core)
        channel_runtime = ChannelRuntime()
        register_channel_with_core(core.context, channel_runtime)
        memory_service = MemoryService(l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require))
        memory_service.l1_store.append(
            [L1TranscriptMessage(role="user", content="Older context that must be compacted before retry.")]
        )
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
        self.assertEqual(preflight_requests[-1].request.metadata.get("preferred_endpoint_id"), "fallback-small")
        self.assertEqual(preflight_requests[-1].request.model_hint, "fallback-small-model")
        self.assertTrue(memory_service.l1_store.items)

    def test_memory_prompt_clears_old_tool_protocol_by_complete_turns(self) -> None:
        from pal.memory.prompt import _build_cleared_tool_indices

        messages = [
            L1TranscriptMessage(role="user", content="turn 1"),
            L1TranscriptMessage(role="assistant", content="", tool_calls=[{"id": "call_1"}]),
            L1TranscriptMessage(role="tool", content="tool-1", tool_call_id="call_1"),
            L1TranscriptMessage(role="assistant", content="", tool_calls=[{"id": "call_2"}]),
            L1TranscriptMessage(role="tool", content="tool-2", tool_call_id="call_2"),
            L1TranscriptMessage(role="user", content="turn 2"),
            L1TranscriptMessage(role="assistant", content="", tool_calls=[{"id": "call_3"}]),
            L1TranscriptMessage(role="tool", content="tool-3", tool_call_id="call_3"),
        ]

        cleared = _build_cleared_tool_indices(messages, keep_recent=1)

        self.assertEqual(cleared, {1, 2, 3, 4})

    def test_memory_prompt_retention_counts_tool_turns_not_tool_batches(self) -> None:
        from pal.memory.prompt import _build_cleared_tool_indices

        messages = [
            L1TranscriptMessage(role="user", content="turn 1"),
            L1TranscriptMessage(role="assistant", content="", tool_calls=[{"id": "call_1"}]),
            L1TranscriptMessage(role="tool", content="tool-1", tool_call_id="call_1"),
            L1TranscriptMessage(role="assistant", content="", tool_calls=[{"id": "call_2"}]),
            L1TranscriptMessage(role="tool", content="tool-2", tool_call_id="call_2"),
            L1TranscriptMessage(role="user", content="turn 2"),
            L1TranscriptMessage(role="assistant", content="", tool_calls=[{"id": "call_3"}]),
            L1TranscriptMessage(role="tool", content="tool-3", tool_call_id="call_3"),
        ]

        cleared = _build_cleared_tool_indices(messages, keep_recent=2)

        self.assertEqual(cleared, set())

    def test_memory_prompt_does_not_partially_clear_single_tool_heavy_turn(self) -> None:
        from pal.memory.prompt import _build_cleared_tool_indices

        messages = [
            L1TranscriptMessage(role="user", content="single turn"),
            L1TranscriptMessage(role="assistant", content="", tool_calls=[{"id": "call_1"}]),
            L1TranscriptMessage(role="tool", content="tool-1", tool_call_id="call_1"),
            L1TranscriptMessage(role="assistant", content="", tool_calls=[{"id": "call_2"}]),
            L1TranscriptMessage(role="tool", content="tool-2", tool_call_id="call_2"),
            L1TranscriptMessage(role="assistant", content="", tool_calls=[{"id": "call_3"}]),
            L1TranscriptMessage(role="tool", content="tool-3", tool_call_id="call_3"),
        ]

        cleared = _build_cleared_tool_indices(messages, keep_recent=1)

        self.assertEqual(cleared, set())

    def test_l1_transcript_preserves_empty_assistant_tool_call_headers(self) -> None:
        service = MemoryService()

        service.l1_store.append(
            [
                L1TranscriptMessage(
                    role="assistant",
                    content="",
                    tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "probe_tool", "arguments": "{}"}}],
                    payload={"provider_specific_fields": {"reasoning_content": "inspect the probe"}},
                ),
                L1TranscriptMessage(role="tool", content="probe result", tool_call_id="call_1"),
            ]
        )

        transcript = service.l1_store.items[0]

        self.assertEqual([item.role for item in transcript], ["assistant", "tool", "assistant"])
        self.assertEqual(transcript[0].content, "")
        self.assertEqual(transcript[0].tool_calls[0]["id"], "call_1")
        self.assertEqual(transcript[0].payload, {})
        self.assertEqual(transcript[1].tool_call_id, "call_1")
        self.assertIn("closed without a further assistant reply", transcript[2].content)

    def test_l1_commit_rejects_orphan_tool_result(self) -> None:
        service = MemoryService()

        result = service.commit_l1(
            MemoryCommitRequest(
                turn_id="turn_1",
                transcript=[
                    L1TranscriptMessage(role="user", content="question"),
                    L1TranscriptMessage(role="tool", content="orphan result", tool_call_id="call_missing"),
                ],
            )
        )

        self.assertEqual(result.status, RuntimeStatus.INVALID)
        self.assertEqual(result.committed_transcript, [])
        self.assertEqual(service.l1_store.items, [])

    def test_l1_commit_rejects_extra_tool_result(self) -> None:
        service = MemoryService()

        result = service.commit_l1(
            MemoryCommitRequest(
                turn_id="turn_1",
                transcript=[
                    L1TranscriptMessage(
                        role="assistant",
                        content="",
                        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "probe_tool", "arguments": "{}"}}],
                    ),
                    L1TranscriptMessage(role="tool", content="probe result", tool_call_id="call_1"),
                    L1TranscriptMessage(role="tool", content="extra result", tool_call_id="call_extra"),
                ],
            )
        )

        self.assertEqual(result.status, RuntimeStatus.INVALID)
        self.assertEqual(result.committed_transcript, [])
        self.assertEqual(service.l1_store.items, [])

    def test_l1_commit_rejects_incomplete_tool_call_header(self) -> None:
        service = MemoryService()

        result = service.commit_l1(
            MemoryCommitRequest(
                turn_id="turn_1",
                transcript=[
                    L1TranscriptMessage(
                        role="assistant",
                        content="",
                        tool_calls=[{"id": "call_missing", "type": "function", "function": {"name": "probe_tool", "arguments": "{}"}}],
                    ),
                ],
            )
        )

        self.assertEqual(result.status, RuntimeStatus.INVALID)
        self.assertEqual(result.committed_transcript, [])
        self.assertEqual(service.l1_store.items, [])

    def test_memory_pack_preserves_existing_l1_protocol_shape_without_rewriting(self) -> None:
        service = MemoryService()
        service.l1_store.items.append(
            [
                L1TranscriptMessage(role="user", content="valid context"),
                L1TranscriptMessage(role="tool", content="orphan result", tool_call_id="call_missing"),
            ]
        )
        service.l1_store.append([L1TranscriptMessage(role="user", content="fresh valid context")])

        pack = service.build_pack(MemoryPackRequest(turn_kind="chat"))

        self.assertEqual([item.role for item in pack.l1_recent_context], ["user"])
        self.assertEqual(
            [item.content for item in pack.l1_recent_context],
            ["fresh valid context"],
        )

    def test_memory_prompt_projects_closed_tool_protocol_as_neutral_history(self) -> None:
        from pal.memory.prompt import MemoryPromptFragmentProvider

        pack = MemoryPack(
            l1_recent_context=[
                L1TranscriptMessage(
                    role="assistant",
                    content="",
                    tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "probe_tool", "arguments": "{}"}}],
                    payload={"provider_specific_fields": {"reasoning_content": "inspect the probe"}},
                ),
                L1TranscriptMessage(role="tool", content="probe result", tool_call_id="call_1"),
            ]
        )

        fragments = MemoryPromptFragmentProvider().build_prompt_fragments(PromptAssemblyContext(metadata={"memory_pack": pack}))
        l1_fragments = [fragment for fragment in fragments if str(fragment.metadata.get("block_id") or "").startswith("l1_recent_context")]

        self.assertEqual([fragment.metadata.get("role") for fragment in l1_fragments], ["assistant"])
        self.assertNotIn("tool_calls", l1_fragments[0].metadata)
        self.assertNotIn("provider_specific_fields", l1_fragments[0].metadata)
        self.assertIn("<closed_tool_interaction>", l1_fragments[0].content)
        self.assertIn("probe_tool", l1_fragments[0].content)
        self.assertIn("probe result", l1_fragments[0].content)
        self.assertNotIn("inspect the probe", l1_fragments[0].content)

    def test_prompt_compiler_neutralizes_closed_l1_tool_protocol(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        service = MemoryService()
        service.l1_store.append(
            [
                L1TranscriptMessage(
                    role="assistant",
                    content="",
                    tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "probe_tool", "arguments": "{}"}}],
                    payload={"provider_specific_fields": {"reasoning_content": "inspect the probe"}},
                ),
                L1TranscriptMessage(role="tool", content="probe result", tool_call_id="call_1"),
            ]
        )
        register_memory_with_core(core.context, service)

        prompt = core.build_canonical_prompt(
            PromptAssemblyContext(
                core_mode="default",
                event=EventEnvelope(
                    event_kind="user.message",
                    source_kind="channel",
                    payload={"text": "current question"},
                ),
            )
        )
        protocol_messages = [
            message
            for message in prompt.messages
            if "<closed_tool_interaction>" in message.text
        ]

        self.assertEqual(len(protocol_messages), 1)
        self.assertEqual(protocol_messages[0].role.value, "assistant")
        self.assertFalse(protocol_messages[0].tool_calls)
        self.assertFalse(protocol_messages[0].replay)
        self.assertIn("probe_tool", protocol_messages[0].text)
        self.assertNotIn("probe result", protocol_messages[0].text)
        self.assertIn("full result retired", protocol_messages[0].text)
        self.assertNotIn("inspect the probe", protocol_messages[0].text)

    def test_memory_prompt_trusts_orphan_tool_result_history_without_revalidating(self) -> None:
        from pal.memory.prompt import MemoryPromptFragmentProvider

        pack = MemoryPack(
            l1_recent_context=[
                L1TranscriptMessage(role="tool", content="orphan result", tool_call_id="call_missing"),
            ]
        )

        fragments = MemoryPromptFragmentProvider().build_prompt_fragments(PromptAssemblyContext(metadata={"memory_pack": pack}))
        l1_fragments = [fragment for fragment in fragments if str(fragment.metadata.get("block_id") or "").startswith("l1_recent_context")]

        self.assertEqual([fragment.metadata.get("role") for fragment in l1_fragments], ["assistant"])
        self.assertNotIn("tool_call_id", l1_fragments[0].metadata)
        self.assertIn("call_missing", l1_fragments[0].content)
        self.assertIn("orphan result", l1_fragments[0].content)

    def test_memory_prompt_trusts_incomplete_tool_call_header_history_without_revalidating(self) -> None:
        from pal.memory.prompt import MemoryPromptFragmentProvider

        pack = MemoryPack(
            l1_recent_context=[
                L1TranscriptMessage(
                    role="assistant",
                    content="",
                    tool_calls=[{"id": "call_missing", "type": "function", "function": {"name": "probe_tool", "arguments": "{}"}}],
                ),
            ]
        )

        fragments = MemoryPromptFragmentProvider().build_prompt_fragments(PromptAssemblyContext(metadata={"memory_pack": pack}))
        l1_fragments = [fragment for fragment in fragments if str(fragment.metadata.get("block_id") or "").startswith("l1_recent_context")]

        self.assertEqual([fragment.metadata.get("role") for fragment in l1_fragments], ["assistant"])
        self.assertNotIn("tool_calls", l1_fragments[0].metadata)
        self.assertIn("call_missing", l1_fragments[0].content)

    def test_memory_prompt_trusts_duplicate_tool_call_id_history_without_revalidating(self) -> None:
        from pal.memory.prompt import MemoryPromptFragmentProvider

        pack = MemoryPack(
            l1_recent_context=[
                L1TranscriptMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        {"id": "call_1", "type": "function", "function": {"name": "probe_tool", "arguments": "{}"}},
                        {"id": "call_1", "type": "function", "function": {"name": "probe_tool", "arguments": "{}"}},
                    ],
                ),
                L1TranscriptMessage(role="tool", content="probe result", tool_call_id="call_1"),
            ]
        )

        fragments = MemoryPromptFragmentProvider().build_prompt_fragments(PromptAssemblyContext(metadata={"memory_pack": pack}))
        l1_fragments = [fragment for fragment in fragments if str(fragment.metadata.get("block_id") or "").startswith("l1_recent_context")]

        self.assertEqual([fragment.metadata.get("role") for fragment in l1_fragments], ["assistant"])
        self.assertNotIn("tool_calls", l1_fragments[0].metadata)
        self.assertGreaterEqual(l1_fragments[0].content.count("call_1"), 2)
        self.assertIn("probe result", l1_fragments[0].content)

    def test_memory_prompt_trusts_tool_call_header_with_missing_id_history_without_revalidating(self) -> None:
        from pal.memory.prompt import MemoryPromptFragmentProvider

        pack = MemoryPack(
            l1_recent_context=[
                L1TranscriptMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        {"type": "function", "function": {"name": "probe_tool", "arguments": "{}"}},
                    ],
                ),
                L1TranscriptMessage(role="tool", content="probe result", tool_call_id="call_1"),
            ]
        )

        fragments = MemoryPromptFragmentProvider().build_prompt_fragments(PromptAssemblyContext(metadata={"memory_pack": pack}))
        l1_fragments = [fragment for fragment in fragments if str(fragment.metadata.get("block_id") or "").startswith("l1_recent_context")]

        self.assertEqual([fragment.metadata.get("role") for fragment in l1_fragments], ["assistant"])
        self.assertNotIn("tool_calls", l1_fragments[0].metadata)
        self.assertIn("probe_tool", l1_fragments[0].content)
        self.assertIn("probe result", l1_fragments[0].content)

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
                    title="Test user profile",
                    summary="The test user built Pal.",
                    rendered="The test user built Pal.",
                    canonical_key="test_user_profile",
                    source_ref="fact:1",
                    source_kind="l3_recall",
                ),
                L2Entry(
                    entry_id="fact:2",
                    kind="fact",
                    scope="system",
                    title="Test user profile duplicate",
                    summary="The test user built Pal again.",
                    rendered="The test user built Pal again.",
                    canonical_key="test_user_profile",
                    source_ref="fact:2",
                ),
            ]
        )

        fragments = provider.build_prompt_fragments(
            PromptAssemblyContext(metadata={"memory_pack": pack})
        )

        remembered_facts = next(fragment for fragment in fragments if fragment.metadata.get("block_id") == "memory_recalled_context")
        self.assertIn("The test user built Pal.", remembered_facts.content)
        self.assertIn("[fact:1]: The test user built Pal.", remembered_facts.content)
        self.assertNotIn("Recalled memory references are operational metadata.", remembered_facts.content)
        self.assertIn('<recalled_memories view="summary">', remembered_facts.content)
        self.assertNotIn("Test user profile:", remembered_facts.content)
        self.assertNotIn("origin available", remembered_facts.content)
        self.assertNotIn("The test user built Pal again.", remembered_facts.content)

    def test_typed_l1_projection_keeps_summary_and_recalled_memory(self) -> None:
        from pal.memory.prompt import MemoryPromptFragmentProvider

        pack = MemoryPack(
            l1_recent_context=[
                L1TranscriptMessage(role="user", content="legacy duplicate L1")
            ],
            current_summary=L2Entry(
                entry_id="memory_summary_current",
                kind="summary",
                scope="conversation",
                title="Conversation summary",
                summary="Prior conversation summary.",
                rendered="Prior conversation summary.",
            ),
            l2_working_memory=[
                L2Entry(
                    entry_id="fact:typed",
                    kind="fact",
                    scope="system",
                    title="Typed memory",
                    summary="Typed projection still needs this memory.",
                    rendered="Typed projection still needs this memory.",
                    source_ref="fact:typed",
                    source_kind="l3_recall",
                )
            ],
        )

        fragments = MemoryPromptFragmentProvider().build_prompt_fragments(
            PromptAssemblyContext(
                metadata={
                    "memory_pack": pack,
                    "typed_l1_projection": True,
                }
            )
        )

        block_ids = {
            str(fragment.metadata.get("block_id") or "")
            for fragment in fragments
        }
        self.assertFalse(
            any(block_id.startswith("l1_recent_context") for block_id in block_ids)
        )
        self.assertIn("memory_current_summary", block_ids)
        self.assertIn("memory_recalled_context", block_ids)

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
                    "title": f"Test user fact {index}",
                    "summary": "The test user built Pal and prefers direct execution." * 8,
                    "rendered": "The test user built Pal and prefers direct execution." * 8,
                    "search_text": "The test user built Pal after legacy assistant frustration and wants direct action." * 10,
                }
                for index in range(10)
            ]
        )
        register_l3_with_core(core.context, mock_l3)
        core.publish_module_capabilities("execution")
        core.publish_module_capabilities(mock_l3.module_id)

        result = core.context.execution_runtime.execute_tool(
            new_tool_call(
                name="call_tool",
                args={
                    "name": "memory_provider_recall",
                    "args": {
                        "name": "mock_l3",
                        "queries": ["用户是谁", "用户身份", "用户个人信息", "用户偏好"],
                        "limit": 10,
                    },
                },
            ),
            budget=ToolCallBudget(max_output_chars=12_000),
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.structured["hit_count"], 10)
        self.assertLess(len(str(result.llm_text)), 12_000)

    def test_minimal_operating_rules_prompt_omits_route_specific_tools(self) -> None:
        from pal.core.prompt import MinimalOperatingRulesPromptFragmentProvider

        fragments = MinimalOperatingRulesPromptFragmentProvider().build_prompt_fragments(PromptAssemblyContext())
        by_section = {fragment.section: fragment for fragment in fragments}
        system_map = by_section["system_map"]
        source_of_truth = by_section["source_of_truth"]
        prompt_context_policy = by_section["prompt_context_policy"]
        rules = by_section["operating_rules"]
        priority = by_section["priority"]
        tool_routing = by_section["tool_routing"]
        tool_efficiency = by_section["tool_efficiency"]
        mutation_policy = by_section["mutation_policy"]
        knowledge_storage_boundary = by_section["knowledge_storage_boundary"]

        self.assertIn("execution/capability", system_map.content)
        self.assertIn("memory: durable facts", system_map.content)
        self.assertNotIn("minion", system_map.content.lower())
        self.assertIn("Use the right source for the truth needed", source_of_truth.content)
        self.assertIn("live introspection/capability calls", source_of_truth.content)
        self.assertIn("<recalled_memories> contains durable memory context", prompt_context_policy.content)
        self.assertIn("No success claim without confirmation", rules.content)
        self.assertIn("Pal capabilities are the execution path", rules.content)
        self.assertIn("shell", rules.content)
        self.assertIn("Source-of-truth, verification, and mutation rules", priority.content)
        self.assertIn("result-specific recovery affordances", tool_routing.content)
        self.assertIn("suggested next tool only when", tool_routing.content)
        self.assertIn("never blindly retry a mutation", tool_routing.content)
        self.assertIn("targeted search", tool_efficiency.content)
        self.assertIn("Runtime capability calls are governed actions", mutation_policy.content)
        self.assertIn("Future route hint or recurring decision rule -> behavior guidance", knowledge_storage_boundary.content)
        self.assertIn('what should be remembered as true or reusable knowledge?', knowledge_storage_boundary.content)
        self.assertIn('when this situation appears, what route/action should Pal consider?', knowledge_storage_boundary.content)
        self.assertIn("multi-step reusable procedure", knowledge_storage_boundary.content)
        self.assertNotIn("op_memory_recall", rules.content)
        self.assertNotIn("op_memory_write", rules.content)

    def test_memory_service_compact_commits_validated_summary_entry_atomically(self) -> None:
        service = MemoryService()
        service.l1_store.append(
            [
                L1TranscriptMessage(role="user", content="Please remember that the user prefers concise replies."),
                L1TranscriptMessage(role="assistant", content="I will keep replies concise."),
            ]
        )

        snapshot = CompactionSnapshot.capture(
            service,
            target_input_budget=512,
            reserved_output_tokens=128,
            clock_kind=CompactionClockKind.USER_TURN,
            clock_value=1,
        )
        entry = PalCompactionPolicy().validate_checkpoint(
            json.dumps(
                {
                    "schema": "pal.compaction.pal.v2",
                    "kind": "pal",
                    "continuity": {
                        "current_focus": "concise replies",
                        "primary_request_and_intent": "preserve the user's preference",
                        "active_operating_instructions": [],
                        "active_requests": [],
                        "temporary_task_state": [],
                        "key_decisions": [],
                        "pending_questions": [],
                        "recent_raw_turns": [],
                        "warm_compressed_turns": [],
                        "retired_or_superseded_context": [],
                        "optional_next_step": "",
                    },
                    "summary": {
                        "summary": "The user prefers concise replies.",
                        "search_text": "concise replies",
                    },
                    "memory_candidates": [],
                }
            ),
            snapshot,
        )
        result = service.compact(
            MemoryCompactRequest(
                target_input_budget=512,
                reserved_output_tokens=128,
                summary_entry=entry,
            )
        )
        self.assertEqual(result.summary, "The user prefers concise replies.")
        self.assertEqual(len(service.l1_store.items), 1)
        self.assertEqual(len(service.l1_store.items[0]), 1)
        summary_message = service.l1_store.items[0][0]
        self.assertEqual(summary_message.role, "assistant")
        self.assertEqual(summary_message.kind, L1MessageKind.RUNTIME_CONTEXT_SUMMARY)
        self.assertIn('<compact_context kind="pal" authority="conversation_continuity">', summary_message.content)
        self.assertIn("The user prefers concise replies.", summary_message.content)
        self.assertNotIn("memory_summary_current", service.l2_store.items)
        summary = service.build_pack(MemoryPackRequest()).current_summary
        self.assertIsNotNone(summary)
        self.assertEqual(summary.kind, "summary")
        self.assertIn("The user prefers concise replies.", summary.rendered)

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
