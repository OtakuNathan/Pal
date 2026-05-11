import asyncio
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pal.behavior import (
    BehaviorAdviceRequest,
    BehaviorAffordanceModel,
    BehaviorRepository,
    BehaviorService,
    BehaviorSkillModel,
    register_with_core as register_behavior_with_core,
)
from pal.control import ControlAction, ControlEvent, ControlPlane, ControlRoute, InteractionResult
from pal.core import PalCore
from pal.channel.contracts import ChannelEnvelope, EndpointConfig, ResponseHandle
from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from pal.execution import CapabilityCall, CapabilityDescriptor, CapabilityResult, register_with_core as register_execution_with_core
from pal.foundation import PalV2Database
from pal.foundation.sidecar import (
    SidecarEndpoint,
    SidecarRpcClient,
    cleanup_sidecar_endpoint,
    dispatch_sidecar_request,
    handle_sidecar_client,
    pack_sidecar_message,
    read_sidecar_message,
    read_sidecar_message_sync,
    run_blocking,
    start_sidecar_server,
)
from pal.minion import (
    MinionControlEventHandler,
    MinionEventSource,
    MinionManager,
    MinionManagerClient,
    MinionManagerProvider,
    MinionProfile,
    MinionProfileRegistry,
    MinionTaskingRepository,
    register_with_core as register_minion_with_core,
)
from pal.llm.contracts import CanonicalLLMOutcome, CanonicalToolCall, CanonicalToolResult
from pal.minion.git_env import _git, commit_milestone, finalize_work_order_branch, prepare_git_task_environment, prepare_task_workspace
from pal.minion.ipc import minion_runner_log_path, open_manager_connection, python_subprocess_env
from pal.minion.manager import MinionRunState
from pal.minion.runner import (
    MinionRunner,
    MinionRuntimeBundle,
    MinionScopedExecutionRuntime,
    _llm_tools_for_allowed,
    _minion_llm_request_metadata,
    _render_system_prompt,
    _resolve_minion_max_output_tokens,
    build_slim_minion_runtime,
)
from pal.minion.introspection import _control_route_payload_for_turn, _prompt_log_enabled_for_turn
from pal.minion.prompt import TaskingPromptFragmentProvider
from pal.foundation import EventEnvelope
from pal.shared import EventKind, LLMFinishReason, PromptAssemblyContext, RuntimeStatus, SourceKind, TaskContextPack


class SidecarFoundationTests(unittest.TestCase):
    def test_frame_round_trips_async_and_sync(self) -> None:
        async def scenario() -> None:
            reader = asyncio.StreamReader()
            reader.feed_data(pack_sidecar_message({"hello": "world"}))
            reader.feed_eof()
            self.assertEqual(await read_sidecar_message(reader), {"hello": "world"})

        asyncio.run(scenario())
        self.assertEqual(read_sidecar_message_sync(io.BytesIO(pack_sidecar_message({"n": 3}))), {"n": 3})

    def test_runner_main_keeps_protocol_stdout_separate_from_prints(self) -> None:
        script = (
            "import asyncio\n"
            "import pal.minion.runner_main as runner_main\n"
            "runner_main._redirect_stdout_to_stderr()\n"
            "print('stdout noise')\n"
            "asyncio.run(runner_main._write({'type': 'event', 'event_kind': 'probe'}))\n"
        )

        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            env=python_subprocess_env(),
            timeout=30,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        self.assertIn("stdout noise", completed.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(read_sidecar_message_sync(io.BytesIO(completed.stdout)), {"type": "event", "event_kind": "probe"})

    def test_python_subprocess_env_includes_source_root_for_sidecars(self) -> None:
        env = python_subprocess_env()
        entries = [Path(item).resolve() for item in str(env.get("PYTHONPATH") or "").split(os.pathsep) if item]

        self.assertIn((Path(__file__).resolve().parents[1] / "src").resolve(), entries)

    def test_minion_runner_log_path_is_pal_log_sibling_and_sanitized(self) -> None:
        root = Path("C:/tmp/pal-runtime")
        path = minion_runner_log_path(root, "wo:bad/id", "planner/reviewer")

        self.assertEqual(path.parent, root)
        self.assertEqual(path.name, "wo_bad_id.planner_reviewer.log")

    def test_rpc_success_error_and_running_loop_sync_wrapper(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory(prefix="pal_sidecar_test_") as tmp:
                endpoint = SidecarEndpoint(runtime_root=Path(tmp), name="unit")

                async def call_method(method: str, params: dict) -> dict:
                    if method == "boom":
                        raise ValueError("nope")
                    return {"method": method, "params": dict(params)}

                async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                    await handle_sidecar_client(
                        reader,
                        writer,
                        lambda request: dispatch_sidecar_request(request, call_method, error_kind=lambda exc: "test"),
                    )

                server, _info = await start_sidecar_server(endpoint, handler)
                client = SidecarRpcClient(endpoint=endpoint, request_timeout_seconds=1.0)
                try:
                    self.assertEqual(await client.request("ping", {"x": 1}), {"method": "ping", "params": {"x": 1}})
                    with self.assertRaises(Exception) as caught:
                        await client.request("boom")
                    self.assertIn("nope", str(caught.exception))
                    self.assertEqual(run_blocking(asyncio.sleep(0, result="ok")), "ok")
                finally:
                    server.close()
                    await server.wait_closed()
                    await cleanup_sidecar_endpoint(endpoint)

        asyncio.run(scenario())


class MinionContractTests(unittest.TestCase):
    def test_task_context_pack_json_roundtrip_uses_allowed_capabilities(self) -> None:
        pack = TaskContextPack.from_dict(
            {
                "work_order_id": "wo_1",
                "goal": "inspect repo",
                "acceptance_criteria": ["report findings"],
                "workspace": {"root": "/tmp/repo"},
                "allowed_capabilities": ["tool.read"],
                "approval_policy": {"high_risk_capabilities": ["shell.exec"]},
                "minion_profile": "coder",
                "resolved_profile": {"profile_id": "coder", "display_name": "Coder Minion"},
            }
        )

        restored = TaskContextPack.from_json(pack.to_json())

        self.assertEqual(restored.work_order_id, "wo_1")
        self.assertEqual(restored.instruction, "inspect repo")
        self.assertEqual(restored.allowed_capabilities, ["tool.read"])
        self.assertNotIn("allowed_tools", restored.to_dict())
        self.assertEqual(restored.approval_policy["high_risk_capabilities"], ["shell.exec"])
        self.assertEqual(restored.minion_profile, "coder")
        self.assertEqual(restored.resolved_profile["profile_id"], "coder")

    def test_legacy_task_context_pack_defaults_to_generic_profile(self) -> None:
        restored = TaskContextPack.from_dict({"work_order_id": "wo_legacy", "goal": "old payload"})

        self.assertEqual(restored.minion_profile, "generic")
        self.assertEqual(restored.resolved_profile, {})

    def test_task_context_pack_ignores_removed_allowed_tools_field(self) -> None:
        restored = TaskContextPack.from_dict({"work_order_id": "wo_caps", "allowed_tools": ["op_exec_run"]})

        self.assertEqual(restored.allowed_capabilities, [])
        self.assertNotIn("allowed_tools", restored.to_dict())

    def test_minion_max_output_tokens_prefers_explicit_work_order_metadata(self) -> None:
        runtime = SimpleNamespace(resolve_max_output_tokens=lambda: 16384)
        pack = TaskContextPack(work_order_id="wo_tokens", goal="budget", metadata={"max_output_tokens": 900})

        self.assertEqual(_resolve_minion_max_output_tokens(runtime, pack), 900)

    def test_minion_max_output_tokens_uses_llm_runtime_limit(self) -> None:
        runtime = SimpleNamespace(resolve_max_output_tokens=lambda: 16384)
        pack = TaskContextPack(work_order_id="wo_tokens", goal="budget")

        self.assertEqual(_resolve_minion_max_output_tokens(runtime, pack), 16384)

    def test_minion_max_output_tokens_passes_preferred_endpoint_to_runtime(self) -> None:
        calls = []

        def resolve_max_output_tokens(*, preferred_endpoint_id=None):
            calls.append(preferred_endpoint_id)
            return 8192

        runtime = SimpleNamespace(resolve_max_output_tokens=resolve_max_output_tokens)
        pack = TaskContextPack(work_order_id="wo_tokens", goal="budget", metadata={"preferred_endpoint_id": "coder_fast"})

        self.assertEqual(_resolve_minion_max_output_tokens(runtime, pack), 8192)
        self.assertEqual(calls, ["coder_fast"])

    def test_minion_max_output_tokens_derives_from_context_window_when_needed(self) -> None:
        runtime = SimpleNamespace(
            resolve_endpoint_facts=lambda: {"context_window": 131072, "max_output_tokens": None},
            config=SimpleNamespace(
                default_max_output_tokens=25000,
                fallback_max_output_tokens=4096,
                context_margin_factor=0.05,
                context_margin_cap=16384,
                context_margin_min=1024,
            ),
        )
        pack = TaskContextPack(work_order_id="wo_tokens", goal="budget")

        self.assertEqual(_resolve_minion_max_output_tokens(runtime, pack), 25000)

    def test_minion_llm_request_metadata_omits_preferred_endpoint_when_unspecified(self) -> None:
        pack = TaskContextPack(work_order_id="wo_tokens", goal="budget")

        metadata = _minion_llm_request_metadata(pack, "run_tokens")

        self.assertNotIn("preferred_endpoint_id", metadata)
        self.assertEqual(metadata["minion_run_id"], "run_tokens")

    def test_minion_llm_request_metadata_includes_explicit_preferred_endpoint(self) -> None:
        pack = TaskContextPack(work_order_id="wo_tokens", goal="budget", metadata={"preferred_endpoint_id": "planner_long"})

        metadata = _minion_llm_request_metadata(pack, "run_tokens")

        self.assertEqual(metadata["preferred_endpoint_id"], "planner_long")

    def test_invalid_task_context_pack_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TaskContextPack.from_dict({"goal": "missing id"})

    def test_execution_runtime_passes_turn_id_to_capability_call_meta(self) -> None:
        core = PalCore()
        observed = {}

        def invoke(call: CapabilityCall):
            observed.update(dict(call.meta))
            return CapabilityResult(status=RuntimeStatus.OK, text="ok", llm_text="ok")

        core.context.execution_runtime.register_capability(
            CapabilityDescriptor(
                name="op_meta_probe",
                family="test",
                description="probe",
                source="test",
            ),
            invoke,
        )

        asyncio.run(
            core.context.execution_runtime.execute_tool_async(
                CanonicalToolCall(name="op_meta_probe", args={}),
                turn_id="turn_meta",
            )
        )

        self.assertEqual(observed["turn_id"], "turn_meta")

    def test_exec_capability_call_forwards_turn_meta_to_nested_capability(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")
        observed = {}

        def invoke(call: CapabilityCall):
            observed.update(dict(call.meta))
            return CapabilityResult(status=RuntimeStatus.OK, text="ok", llm_text="ok")

        core.context.execution_runtime.register_capability(
            CapabilityDescriptor(
                name="op_meta_probe",
                family="test",
                description="probe",
                source="test",
            ),
            invoke,
        )

        asyncio.run(
            core.context.execution_runtime.execute_tool_async(
                CanonicalToolCall(name="op_exec_capability_call", args={"name": "op_meta_probe"}),
                turn_id="turn_meta_nested",
            )
        )

        self.assertEqual(observed["turn_id"], "turn_meta_nested")

    def test_control_route_can_be_derived_from_active_turn_for_minion_spawn(self) -> None:
        core = PalCore()
        core.context.port_registry["core:core"] = core
        envelope = ChannelEnvelope(
            event=EventEnvelope(event_kind=EventKind.USER_MESSAGE, source_kind=SourceKind.CHANNEL, payload={}, event_id="turn_route"),
            endpoint=EndpointConfig(endpoint_id="telegram_main", channel_kind="telegram", binding_key="telegram://main"),
            response_handle=ResponseHandle(endpoint_id="telegram_main", reply_target={"chat_id": "42"}),
        )
        core.state.active_turns["turn_route"] = SimpleNamespace(channel_envelope=envelope)

        route = _control_route_payload_for_turn(core.context, "turn_route")

        self.assertEqual(route["endpoint_id"], "telegram_main")
        self.assertEqual(route["channel_kind"], "telegram")
        self.assertEqual(route["reply_target"], {"chat_id": "42"})

    def test_prompt_log_enabled_for_minion_spawn_uses_turn_snapshot(self) -> None:
        core = PalCore()
        core.context.port_registry["core:core"] = core
        core.state.prompt_log_enabled = True
        core.state.active_turns["turn_log_off"] = SimpleNamespace(turn_settings_snapshot={"prompt_log_enabled": False})
        core.state.active_turns["turn_log_on"] = SimpleNamespace(turn_settings_snapshot={"prompt_log_enabled": True})

        self.assertFalse(_prompt_log_enabled_for_turn(core.context, "turn_log_off"))
        self.assertTrue(_prompt_log_enabled_for_turn(core.context, "turn_log_on"))
        self.assertTrue(_prompt_log_enabled_for_turn(core.context, "missing_turn"))
        with tempfile.TemporaryDirectory(prefix="pal_minion_log_inject_test_") as tmp:
            provider = MinionManagerProvider(runtime_root=Path(tmp), context=core.context)
            pack = TaskContextPack(work_order_id="wo_log_inject", goal="debug")
            injected = provider._inject_debug_log_request(
                pack,
                CapabilityCall(name="op_minion_spawn", args={}, meta={"turn_id": "turn_log_on"}),
            )
            skipped = provider._inject_debug_log_request(
                pack,
                CapabilityCall(name="op_minion_spawn", args={}, meta={"turn_id": "turn_log_off"}),
            )

        self.assertTrue(injected.metadata["minion_debug_log_enabled"])
        self.assertNotIn("minion_debug_log_enabled", skipped.metadata)

    def test_declarative_profiles_expand_web_research_group(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_profile_registry_test_") as tmp:
            registry = MinionProfileRegistry(runtime_root=Path(tmp))
            pack = registry.resolve_pack(TaskContextPack(work_order_id="wo_profile", goal="research"), requested_profile="planner")

            self.assertIn("op_l3_recall_query", pack.allowed_capabilities)
            self.assertIn("op_workspace_tree", pack.allowed_capabilities)
            self.assertIn("op_workspace_search", pack.allowed_capabilities)
            self.assertIn("op_workspace_read", pack.allowed_capabilities)
            self.assertIn("op_web_search_query", pack.allowed_capabilities)
            self.assertIn("op_web_fetch_read", pack.allowed_capabilities)
            self.assertFalse(any(name.startswith("intro_") for name in pack.allowed_capabilities))
            self.assertEqual([name for name in pack.allowed_capabilities if name.startswith("op_minion_")], ["op_minion_artifact_write"])
            self.assertEqual(pack.workspace["workspace_policy"]["mode"], "read_only_repo")
            self.assertEqual(pack.workspace["completion_policy"]["evidence"], "text_deliverable")
            self.assertEqual(pack.resolved_profile["profile_group"], "software_engineering")

    def test_profile_resolution_filters_minion_denied_capabilities(self) -> None:
        registry = MinionProfileRegistry()
        pack = registry.resolve_pack(
            TaskContextPack(
                work_order_id="wo_policy",
                goal="do work",
                allowed_capabilities=[
                    "intro_task_read",
                    "intro_work_order_read",
                    "op_l3_commit_write",
                    "op_l3_correct_patch",
                    "op_behavior_affordance_submit",
                    "op_skill_commit",
                    "op_channel_send_attachment",
                    "op_minion_spawn",
                    "op_minion_kill",
                    "op_exec_run",
                    "op_l3_recall_query",
                    "op_web_search_query",
                    "op_fake_extra",
                ],
            ),
            requested_profile="coder",
        )

        self.assertEqual(pack.allowed_capabilities, ["op_exec_run", "op_l3_recall_query", "op_web_search_query", "op_fake_extra"])
        self.assertIn("effective_capability_policy", pack.resolved_profile)

    def test_profile_resolution_can_inherit_current_capability_surface(self) -> None:
        registry = MinionProfileRegistry(
            ambient_capabilities=(
                "op_exec_disc_search",
                "op_exec_disc_read",
                "op_exec_capability_call",
                "op_exec_run",
                "op_l3_recall_query",
                "op_web_search_query",
                "op_web_fetch_read",
                "intro_task_read",
                "op_minion_spawn",
                "op_l3_commit_write",
                "op_l3_correct_patch",
            )
        )
        pack = registry.resolve_pack(TaskContextPack(work_order_id="wo_inherit", goal="inherit"), requested_profile="planner")

        self.assertIn("op_exec_disc_search", pack.allowed_capabilities)
        self.assertIn("op_exec_disc_read", pack.allowed_capabilities)
        self.assertIn("op_exec_capability_call", pack.allowed_capabilities)
        self.assertNotIn("op_exec_run", pack.allowed_capabilities)
        self.assertIn("op_workspace_read", pack.allowed_capabilities)
        self.assertIn("op_l3_recall_query", pack.allowed_capabilities)
        self.assertIn("op_web_search_query", pack.allowed_capabilities)
        self.assertIn("op_web_fetch_read", pack.allowed_capabilities)
        self.assertNotIn("intro_task_read", pack.allowed_capabilities)
        self.assertNotIn("op_minion_spawn", pack.allowed_capabilities)
        self.assertNotIn("op_l3_commit_write", pack.allowed_capabilities)
        self.assertNotIn("op_l3_correct_patch", pack.allowed_capabilities)

    def test_runtime_profiles_load_recursively_and_override_builtin(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_profile_registry_test_") as tmp:
            profile_dir = Path(tmp) / "plugins" / "minion" / "profiles" / "software_engineering"
            profile_dir.mkdir(parents=True)
            (profile_dir / "planner.toml").write_text(
                "\n".join(
                    [
                        'profile_id = "planner"',
                        'display_name = "Runtime Planner"',
                        'identity_fragment = "Runtime override planner."',
                        'profile_group = "runtime_software"',
                        'capability_groups = ["workspace_read"]',
                        "[workspace_policy]",
                        'mode = "read_only_repo"',
                        "[completion_policy]",
                        'evidence = "text_deliverable"',
                    ]
                ),
                encoding="utf-8",
            )

            registry = MinionProfileRegistry(runtime_root=Path(tmp))
            profile = registry.get("planner")

            self.assertIsNotNone(profile)
            assert profile is not None
            self.assertEqual(profile.display_name, "Runtime Planner")
            self.assertEqual(profile.profile_group, "runtime_software")
            self.assertEqual(profile.workspace_policy["mode"], "read_only_repo")

    def test_runtime_profile_file_overrides_builtin_profile(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_profile_registry_test_") as tmp:
            profile_dir = Path(tmp) / "plugins" / "minion" / "profiles"
            profile_dir.mkdir(parents=True)
            (profile_dir / "planner.toml").write_text(
                "\n".join(
                    [
                        'profile_id = "planner"',
                        'display_name = "Runtime Planner"',
                        'identity_fragment = "Runtime planner identity."',
                        'capability_groups = ["web_research"]',
                    ]
                ),
                encoding="utf-8",
            )

            profile = MinionProfileRegistry(runtime_root=Path(tmp)).get("planner")

            self.assertIsNotNone(profile)
            assert profile is not None
            self.assertEqual(profile.display_name, "Runtime Planner")


class MinionTaskingRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal_minion_tasking_test_"))
        self.repository = MinionTaskingRepository(runtime_root=self.root)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_checkpoint_is_milestone_cursor_and_lessons_wait_for_approval(self) -> None:
        pack = TaskContextPack(
            work_order_id="wo_cursor",
            goal="ship feature",
            metadata={"task_id": "task_ship", "milestones": ["Design", "Implement"]},
        )
        prepared = self.repository.prepare_pack_for_spawn(pack)

        self.assertEqual(prepared.continuity["current_milestone"]["milestone_index"], 0)

        self.repository.record_minion_event(
            {
                "event_kind": "checkpoint",
                "work_order_id": "wo_cursor",
                "minion_id": "m1",
                "run_id": "r1",
                "payload": {"status": "partial", "milestone_index": 0, "summary": "halfway"},
            }
        )
        snapshot = self.repository.read_work_order("wo_cursor")
        self.assertEqual(snapshot["current_milestone"]["milestone_index"], 0)

        self.repository.record_minion_event(
            {
                "event_kind": "checkpoint",
                "work_order_id": "wo_cursor",
                "minion_id": "m1",
                "run_id": "r1",
                "payload": {"status": "completed", "milestone_index": 0, "summary": "design done"},
            }
        )
        snapshot = self.repository.read_work_order("wo_cursor")
        self.assertEqual(snapshot["current_milestone"]["milestone_index"], 1)
        self.assertEqual(snapshot["latest_completed_checkpoint"]["status"], "completed")

        self.repository.record_minion_event(
            {
                "event_kind": "terminal",
                "work_order_id": "wo_cursor",
                "minion_id": "m1",
                "run_id": "r1",
                "payload": {
                    "status": "completed",
                    "summary": "done",
                    "artifacts": [{"path": "/tmp/minion/design.md", "relative_path": "design.md", "role": "primary"}],
                    "primary_artifact": {"path": "/tmp/minion/design.md", "relative_path": "design.md", "role": "primary"},
                    "task_lessons": ["Use the existing milestone cursor."],
                    "system_lessons": ["Maybe promote this pattern later."],
                },
            }
        )
        snapshot = self.repository.read_work_order("wo_cursor")
        self.assertEqual(snapshot["work_order"]["status"], "completed")
        self.assertEqual(snapshot["work_order"]["metadata"]["artifacts"][0]["relative_path"], "design.md")
        self.assertEqual(snapshot["work_order"]["metadata"]["primary_artifact"]["relative_path"], "design.md")
        self.assertEqual(snapshot["task_lessons"], [])
        self.assertEqual(snapshot["pending_system_lesson_candidates"], [])

        absorbed = self.repository.absorb_lessons(
            "wo_cursor",
            task_lessons=["Use the existing milestone cursor."],
            system_lessons=["Maybe promote this pattern later."],
            minion_id="m1",
            run_id="r1",
        )

        self.assertEqual(absorbed["status"], "ok")
        snapshot = self.repository.read_work_order("wo_cursor")
        self.assertEqual(snapshot["task_lessons"][0]["lesson_text"], "Use the existing milestone cursor.")
        self.assertEqual(snapshot["pending_system_lesson_candidates"], [])

    def test_one_task_cannot_have_two_active_work_orders(self) -> None:
        self.repository.prepare_pack_for_spawn(
            TaskContextPack(work_order_id="wo_a", goal="same task", metadata={"task_id": "task_one"})
        )

        with self.assertRaises(ValueError):
            self.repository.prepare_pack_for_spawn(
                TaskContextPack(work_order_id="wo_b", goal="same task again", metadata={"task_id": "task_one"})
            )

    def test_task_and_work_order_search_use_minion_store(self) -> None:
        self.repository.prepare_pack_for_spawn(
            TaskContextPack(
                work_order_id="wo_search",
                goal="stabilize telegram replies",
                metadata={"task_id": "task_telegram", "task_title": "Telegram reliability"},
            )
        )

        tasks = self.repository.search_tasks("telegram reliability")
        work_orders = self.repository.search_work_orders("stabilize replies")

        self.assertEqual(tasks["items"][0]["task"]["task_id"], "task_telegram")
        self.assertEqual(work_orders["items"][0]["work_order_id"], "wo_search")

    def test_work_order_draft_is_stored_searchable_without_creating_work_order(self) -> None:
        draft = self.repository.create_work_order_draft(
            {
                "title": "Artifact routing cleanup",
                "goal": "Fix current-turn artifact routing",
                "source_summary": "User brainstormed that current-turn artifacts must be authoritative.",
                "module_boundaries": ["artifact prompt provider", "artifact manager exposure"],
                "milestones": [
                    {
                        "title": "Current artifact selection",
                        "summary": "Use explicit artifact refs and avoid hot fallback.",
                        "acceptance": ["empty caption image still belongs to current turn"],
                    }
                ],
                "task_id": "task_artifact_routing",
            }
        )

        draft_id = draft["draft"]["draft_id"]
        found = self.repository.search_work_order_drafts("artifact routing", limit=5)
        read = self.repository.read_work_order_draft(draft_id)
        missing_work_order = self.repository.read_work_order(read["work_order_candidate"]["work_order_id"])

        self.assertEqual(found["items"][0]["draft_id"], draft_id)
        self.assertEqual(read["work_order_candidate"]["metadata"]["work_order_draft_id"], draft_id)
        self.assertEqual(read["planner_review"]["minion_profile"], "planner")
        self.assertEqual(missing_work_order["status"], "not_found")

    def test_promote_work_order_draft_creates_formal_work_order(self) -> None:
        draft = self.repository.create_work_order_draft(
            {
                "title": "Minion ledger cleanup",
                "goal": "Make minion ledger reads factual",
                "instruction": "Use checkpoint facts instead of chat guesses.",
                "source_summary": "User asked that progress comes from checkpoint facts.",
                "task_id": "task_minion_ledger",
                "proposed_work_order_id": "wo_minion_ledger",
                "milestones": ["read facts", "report current worker"],
            }
        )

        promoted = self.repository.promote_work_order_draft(draft["draft"]["draft_id"])
        work_order = self.repository.read_work_order("wo_minion_ledger")
        read_draft = self.repository.read_work_order_draft(draft["draft"]["draft_id"])

        self.assertEqual(promoted["status"], "ok")
        self.assertEqual(work_order["status"], "ok")
        self.assertEqual(work_order["work_order"]["task_id"], "task_minion_ledger")
        self.assertEqual(work_order["work_order"]["instruction"], "Use checkpoint facts instead of chat guesses.")
        self.assertEqual(work_order["milestones"][0]["title"], "read facts")
        self.assertEqual(read_draft["draft"]["status"], "promoted")
        self.assertEqual(read_draft["draft"]["payload"]["promoted_work_order_id"], "wo_minion_ledger")

    def test_pack_for_existing_work_order_preserves_stored_instruction_and_workspace(self) -> None:
        self.repository.prepare_pack_for_spawn(
            TaskContextPack(
                work_order_id="wo_existing_pack",
                goal="existing goal",
                instruction="stored instruction survives spawn by id",
                workspace={"source_repo": "local-source"},
                allowed_capabilities=["op_exec_run"],
                metadata={"task_id": "task_existing_pack"},
            )
        )

        pack = self.repository.pack_for_work_order("wo_existing_pack")

        self.assertEqual(pack.instruction, "stored instruction survives spawn by id")
        self.assertEqual(pack.workspace["source_repo"], "local-source")
        self.assertEqual(pack.allowed_capabilities, [])

    def test_promote_work_order_draft_respects_single_active_work_order_invariant(self) -> None:
        self.repository.prepare_pack_for_spawn(
            TaskContextPack(work_order_id="wo_existing", goal="already active", metadata={"task_id": "task_collision"})
        )
        draft = self.repository.create_work_order_draft(
            {
                "title": "Colliding work",
                "goal": "should not promote",
                "task_id": "task_collision",
                "proposed_work_order_id": "wo_collision",
            }
        )

        with self.assertRaises(ValueError):
            self.repository.promote_work_order_draft(draft["draft"]["draft_id"])

        self.assertEqual(self.repository.read_work_order_draft(draft["draft"]["draft_id"])["draft"]["status"], "draft")
        self.assertEqual(self.repository.read_work_order("wo_collision")["status"], "not_found")

    def test_commit_failure_is_recorded_as_partial_and_does_not_advance_cursor(self) -> None:
        pack = self.repository.prepare_pack_for_spawn(
            TaskContextPack(work_order_id="wo_commit_failure", goal="commit failure", metadata={"milestones": ["first", "second"]})
        )
        repo_pack = prepare_git_task_environment(self.root, pack)
        repo = Path(repo_pack.workspace["repo_path"])
        not_a_repo = self.root / "not_a_repo"
        not_a_repo.mkdir()

        result = commit_milestone(not_a_repo, work_order_id="wo_commit_failure", milestone_index=0, title="first")
        self.repository.record_minion_event(
            {
                "event_kind": "checkpoint",
                "work_order_id": "wo_commit_failure",
                "payload": {
                    "status": "partial" if result["status"] == "error" else "completed",
                    "milestone_index": 0,
                    "summary": result.get("error") or result["status"],
                    "git_commit": result,
                },
            }
        )
        snapshot = self.repository.read_work_order("wo_commit_failure")

        self.assertEqual(result["status"], "error")
        self.assertEqual(snapshot["current_milestone"]["milestone_index"], 0)
        self.assertEqual(snapshot["latest_checkpoint"]["status"], "partial")
        self.assertTrue((repo / ".git").exists())

    def test_git_task_environment_branch_commit_and_finalize(self) -> None:
        pack = self.repository.prepare_pack_for_spawn(
            TaskContextPack(work_order_id="wo_git", goal="git backed work", metadata={"task_id": "task_git"})
        )
        prepared = prepare_git_task_environment(self.root, pack)
        repo = Path(prepared.workspace["repo_path"])

        (repo / "note.txt").write_text("milestone one", encoding="utf-8")
        committed = self.repository.prepare_pack_for_spawn(prepared)
        _ = committed
        result = commit_milestone(
            repo,
            work_order_id="wo_git",
            milestone_index=0,
            title="git backed work",
        )
        self.assertEqual(result["status"], "committed")
        self.assertTrue(result["commit_sha"])

        finalized = finalize_work_order_branch(
            repo,
            work_order_branch=prepared.workspace["work_order_branch"],
            merge_target=prepared.workspace["merge_target"],
            message="finalize git work",
        )
        self.assertIn(finalized["status"], {"committed", "no_changes"})

    def test_git_task_environment_clones_source_repo_into_task_repo(self) -> None:
        source = self.root / "source_repo"
        source.mkdir()
        _git(source, "init")
        _git(source, "checkout", "-B", "main")
        _git(source, "config", "user.email", "test@example.com")
        _git(source, "config", "user.name", "Test User")
        (source / "app.py").write_text("print('hello')\n", encoding="utf-8")
        _git(source, "add", "-A")
        _git(source, "commit", "-m", "initial source")
        target = self.root / "task_clone"

        prepared = prepare_git_task_environment(
            self.root,
            TaskContextPack(
                work_order_id="wo_clone",
                goal="clone source",
                workspace={"repo_path": str(target), "source_repo": str(source)},
                metadata={"task_id": "task_clone"},
            ),
        )

        repo = Path(prepared.workspace["repo_path"])
        self.assertTrue((repo / ".git").exists())
        self.assertEqual((repo / "app.py").read_text(encoding="utf-8"), "print('hello')\n")
        self.assertEqual(prepared.workspace["source_repo"], str(source))

    def test_policy_workspace_keeps_planner_read_only_and_coder_writable(self) -> None:
        source = self.root / "source_repo_readonly"
        source.mkdir()
        _git(source, "init")
        _git(source, "checkout", "-B", "main")
        _git(source, "config", "user.email", "test@example.com")
        _git(source, "config", "user.name", "Test User")
        (source / "README.md").write_text("SPEC\n", encoding="utf-8")
        _git(source, "add", "-A")
        _git(source, "commit", "-m", "initial source")

        registry = MinionProfileRegistry(runtime_root=self.root)
        planner = registry.resolve_pack(
            TaskContextPack(work_order_id="wo_plan_readonly", goal="plan", workspace={"source_repo": str(source)}),
            requested_profile="planner",
        )
        planner_prepared = prepare_task_workspace(self.root, planner, run_id="run_plan")

        self.assertEqual(Path(planner_prepared.workspace["repo_path"]), source)
        self.assertEqual(planner_prepared.workspace["workspace_policy"]["mode"], "read_only_repo")
        self.assertEqual(planner_prepared.workspace["workspace_kind"], "folder")
        self.assertTrue(Path(planner_prepared.workspace["run_dir"]).is_dir())
        self.assertTrue(Path(planner_prepared.workspace["artifact_dir"]).is_dir())
        self.assertTrue(str(planner_prepared.workspace["run_dir"]).endswith("run_plan_planner"))
        self.assertTrue((Path(planner_prepared.workspace["run_dir"]) / "work_order.json").is_file())
        self.assertNotIn("work_order_branch", planner_prepared.workspace)
        self.assertFalse((source / "deliverables").exists())

        planner_with_cwd = registry.resolve_pack(
            TaskContextPack(
                work_order_id="wo_plan_readonly_cwd",
                goal="plan",
                workspace={"cwd": str(source), "type": "local_repo"},
            ),
            requested_profile="planner",
        )
        planner_with_cwd_prepared = prepare_task_workspace(self.root, planner_with_cwd)

        self.assertEqual(Path(planner_with_cwd_prepared.workspace["repo_path"]), source)
        self.assertEqual(Path(planner_with_cwd_prepared.workspace["source_repo"]), source)
        self.assertEqual(planner_with_cwd_prepared.workspace["workspace_policy"]["mode"], "read_only_repo")
        self.assertEqual(planner_with_cwd_prepared.workspace["workspace_kind"], "folder")
        self.assertTrue(Path(planner_with_cwd_prepared.workspace["artifact_dir"]).is_dir())
        self.assertNotIn("work_order_branch", planner_with_cwd_prepared.workspace)

        coder = registry.resolve_pack(
            TaskContextPack(work_order_id="wo_code_writable", goal="code", workspace={"source_repo": str(source)}),
            requested_profile="coder",
        )
        coder_prepared = prepare_task_workspace(self.root, coder)

        self.assertEqual(coder_prepared.workspace["workspace_policy"]["mode"], "writable_git_branch")
        self.assertEqual(coder_prepared.workspace["completion_policy"]["evidence"], "git_commit")
        self.assertEqual(coder_prepared.workspace["workspace_kind"], "git_repo")
        self.assertTrue(Path(coder_prepared.workspace["artifact_dir"]).is_dir())
        self.assertTrue(str(coder_prepared.workspace["artifact_dir"]).replace("\\", "/").endswith("minion_outputs/wo_code_writable"))
        self.assertIn("work_order_branch", coder_prepared.workspace)
        self.assertNotEqual(Path(coder_prepared.workspace["repo_path"]), source)


class MinionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal_minion_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_manager_spawn_returns_immediately_and_records_terminal_event(self) -> None:
        async def scenario() -> None:
            manager, manager_task, client = await self._start_manager()
            _ = manager
            reader, writer = await self._subscribe_events()
            try:
                pack = TaskContextPack(work_order_id="wo_spawn", goal="finish scaffold task")
                spawned = await client.request("spawn", {"task_context_pack": pack.to_dict()})
                self.assertEqual(spawned["work_order_id"], "wo_spawn")
                self.assertIn(spawned["status"], {"running", "completed"})

                terminal = await self._read_subscribed_until(reader, lambda event: event.get("event_kind") == "terminal")
                self.assertEqual(terminal["run_id"], spawned["run_id"])
                self.assertIn(terminal["payload"]["status"], {"completed", "blocked"})

                detail = await client.request("read_run", {"run_id": spawned["run_id"]})
                self.assertIn(detail["status"], {"completed", "blocked"})
                self.assertTrue(detail["ledger"])
            finally:
                await self._close_subscription(writer)
                await self._shutdown_manager(client, manager_task)

        asyncio.run(scenario())

    def test_manager_adds_runner_debug_log_when_prompt_logging_requested(self) -> None:
        manager = MinionManager(runtime_root=self.root)
        pack = TaskContextPack(
            work_order_id="wo:debug/id",
            goal="debug minion",
            minion_profile="planner/reviewer",
            metadata={"minion_debug_log_enabled": True},
        )

        configured = manager._with_runner_debug_log(pack)
        debug_log = dict(configured.metadata["debug_log"])

        self.assertTrue(debug_log["enabled"])
        self.assertEqual(Path(debug_log["path"]).parent, self.root)
        self.assertEqual(Path(debug_log["path"]).name, "wo_debug_id.planner_reviewer.log")
        self.assertEqual(debug_log["managed_by"], "minion.manager")
        state = MinionRunState(minion_id="m_debug", run_id="r_debug", pack=configured)
        self.assertEqual(state.summary()["debug_log_path"], debug_log["path"])

    def test_manager_pushes_events_to_subscribers_without_poll_drain(self) -> None:
        async def scenario() -> None:
            manager, manager_task, client = await self._start_manager()
            reader, writer = await open_manager_connection(self.root)
            try:
                request_id = "sub-test"
                writer.write(
                    pack_sidecar_message(
                        {
                            "type": "request",
                            "id": request_id,
                            "method": "subscribe_events",
                            "params": {},
                        }
                    )
                )
                await writer.drain()
                response = await asyncio.wait_for(read_sidecar_message(reader), timeout=2)
                self.assertTrue(response["ok"])
                self.assertEqual(response["id"], request_id)

                state = MinionRunState(
                    minion_id="m_push",
                    run_id="r_push",
                    pack=TaskContextPack(work_order_id="wo_push", goal="push event"),
                    status="running",
                )
                manager.runs[state.run_id] = state
                manager._record_event(
                    state,
                    {
                        "event_kind": "terminal",
                        "payload": {"status": "completed", "summary": "pushed"},
                        "created_at": "2026-01-01T00:00:00Z",
                    },
                )

                frame = await asyncio.wait_for(read_sidecar_message(reader), timeout=2)
                self.assertEqual(frame["type"], "event")
                self.assertEqual(frame["event"]["event_kind"], "terminal")
                self.assertEqual(frame["event"]["run_id"], "r_push")
                self.assertEqual(manager.event_queue, [])
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                await self._shutdown_manager(client, manager_task)

        asyncio.run(scenario())

    def test_runner_tool_loop_approval_and_milestone_commit(self) -> None:
        async def scenario() -> None:
            events = []
            decisions = [{"decision": {"decision": "accept"}}]
            repo_pack = prepare_git_task_environment(
                self.root,
                MinionProfileRegistry(runtime_root=self.root).resolve_pack(
                    TaskContextPack(
                        work_order_id="wo_approval",
                        goal="needs approval",
                        approval_policy={"high_risk_capabilities": ["op_fake_tool"], "decision_timeout_seconds": 5},
                    )
                ),
            )

            class FakeLLM:
                def __init__(self):
                    self.calls = 0

                async def agenerate(self, request):
                    self.calls += 1
                    if self.calls == 1:
                        return CanonicalLLMOutcome(
                            text="",
                            tool_calls=[CanonicalToolCall(name="op_fake_tool", args={"value": 1})],
                        )
                    return CanonicalLLMOutcome(
                        text=(
                            "milestone done\n"
                            "Task lessons: Use the confirmed tool result as task continuity evidence.\n"
                            "System lessons: Keep high-risk approval decisions in the minion ledger."
                        )
                    )

            class FakeExecution:
                def get_capability_spec(self, name):
                    if name != "op_fake_tool":
                        return None
                    return {
                        "name": "op_fake_tool",
                        "description": "fake write tool",
                        "parameters_schema": {"type": "object", "properties": {"value": {"type": "integer"}}},
                    }

                async def execute_tool_async(self, call, **kwargs):
                    _ = kwargs
                    (Path(repo_pack.workspace["repo_path"]) / "done.txt").write_text(str(call.args["value"]), encoding="utf-8")
                    return CanonicalToolResult(
                        name=call.name,
                        ok=True,
                        text="fake tool completed",
                        llm_text="fake tool completed",
                        structured={"ok": True},
                        status=RuntimeStatus.OK,
                    )

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return decisions.pop(0)

            code = await MinionRunner(
                runtime_root=self.root,
                pack=TaskContextPack.from_dict({**repo_pack.to_dict(), "allowed_capabilities": ["op_fake_tool"]}),
                minion_id="m1",
                run_id="r1",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=FakeLLM(), execution_runtime=FakeExecution()),
            ).run()

            self.assertEqual(code, 0)
            kinds = [event["event_kind"] for event in events]
            self.assertIn("approval_requested", kinds)
            checkpoint = next(event for event in events if event["event_kind"] == "checkpoint")
            self.assertEqual(checkpoint["payload"]["status"], "completed")
            self.assertTrue(checkpoint["payload"]["commit_sha"])
            terminal = next(event for event in events if event["event_kind"] == "terminal")
            self.assertEqual(terminal["payload"]["status"], "completed")
            self.assertNotIn("Task lessons", terminal["payload"]["summary"])
            self.assertNotIn("System lessons", terminal["payload"]["summary"])
            self.assertIn("Use the confirmed tool result", terminal["payload"]["task_lessons"][0])
            self.assertIn("high-risk approval", terminal["payload"]["system_lessons"][0])

        asyncio.run(scenario())

    def test_runner_emits_terminal_when_runtime_build_fails(self) -> None:
        async def scenario() -> None:
            events = []

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return None

            with patch("pal.minion.runner.build_slim_minion_runtime", side_effect=RuntimeError("missing llm endpoint")):
                code = await MinionRunner(
                    runtime_root=self.root,
                    pack=TaskContextPack(work_order_id="wo_build_fail", goal="fail before llm"),
                    minion_id="m_build_fail",
                    run_id="r_build_fail",
                    write_event=write_event,
                    read_decision=read_decision,
                ).run()

            self.assertEqual(code, 1)
            terminal = next(event for event in events if event["event_kind"] == "terminal")
            self.assertEqual(terminal["payload"]["status"], "failed")
            self.assertEqual(terminal["payload"]["error_type"], "RuntimeError")
            self.assertIn("missing llm endpoint", terminal["payload"]["error"])

        asyncio.run(scenario())

    def test_manager_exit_terminal_includes_runner_stderr_tail(self) -> None:
        async def scenario() -> None:
            manager = MinionManager(runtime_root=self.root)
            state = MinionRunState(
                minion_id="m_stderr",
                run_id="r_stderr",
                pack=TaskContextPack(work_order_id="wo_stderr", goal="stderr failure"),
                status="running",
            )

            class FakeProcess:
                async def wait(self):
                    return 1

            state.process = FakeProcess()  # type: ignore[assignment]
            manager._record_runner_stderr_line(state, "Traceback: boom")
            await manager._wait_runner(state)

            terminal = manager.event_queue[-1]
            self.assertEqual(terminal["event_kind"], "terminal")
            self.assertEqual(terminal["payload"]["status"], "failed")
            self.assertIn("Traceback: boom", terminal["payload"]["stderr_tail"])
            self.assertEqual(terminal["payload"]["error"], "Traceback: boom")

        asyncio.run(scenario())

    def test_manager_reconciles_dead_runner_before_reading_status(self) -> None:
        async def scenario() -> None:
            manager = MinionManager(runtime_root=self.root)
            state = MinionRunState(
                minion_id="m_dead",
                run_id="r_dead",
                pack=TaskContextPack(work_order_id="wo_dead", goal="dead runner"),
                status="running",
            )

            class FakeProcess:
                pid = 12345
                returncode = 1

            state.process = FakeProcess()  # type: ignore[assignment]
            manager.runs[state.run_id] = state

            detail = await manager._call_method("read_run", {"run_id": state.run_id})

            self.assertEqual(detail["status"], "failed")
            self.assertEqual(detail["returncode"], 1)
            self.assertEqual(manager.event_queue[-1]["event_kind"], "terminal")
            self.assertEqual(manager.event_queue[-1]["payload"]["status"], "failed")

        asyncio.run(scenario())

    def test_manager_lets_runner_terminal_event_win_process_exit_race(self) -> None:
        async def scenario() -> None:
            manager = MinionManager(runtime_root=self.root)
            state = MinionRunState(
                minion_id="m_race",
                run_id="r_race",
                pack=TaskContextPack(work_order_id="wo_race", goal="race"),
                status="running",
            )

            class FakeProcess:
                returncode = 0

                async def wait(self):
                    self.returncode = 0
                    return 0

            async def emit_runner_terminal() -> None:
                await asyncio.sleep(0)
                manager._record_event(
                    state,
                    {
                        "event_kind": "terminal",
                        "payload": {"status": "blocked", "summary": "runner reported blocked"},
                        "created_at": "2026-01-01T00:00:00Z",
                    },
                )

            state.process = FakeProcess()  # type: ignore[assignment]
            state.stdout_task = asyncio.create_task(emit_runner_terminal())

            await manager._wait_runner(state)

            terminal_events = [event for event in manager.event_queue if event["event_kind"] == "terminal"]
            self.assertEqual(len(terminal_events), 1)
            self.assertEqual(terminal_events[0]["payload"]["status"], "blocked")
            self.assertEqual(state.status, "blocked")

        asyncio.run(scenario())

    def test_runner_nudges_once_when_llm_finishes_without_using_available_tool(self) -> None:
        async def scenario() -> None:
            events = []
            executed = []
            llm_messages = []
            repo_pack = prepare_git_task_environment(
                self.root,
                TaskContextPack(
                    work_order_id="wo_nudge",
                    goal="write evidence file",
                    continuity={"current_milestone": {"milestone_index": 0, "title": "write evidence"}},
                    allowed_capabilities=["op_fake_write"],
                ),
            )
            repo = Path(repo_pack.workspace["repo_path"])

            class FakeLLM:
                def __init__(self):
                    self.calls = 0

                async def agenerate(self, request):
                    self.calls += 1
                    llm_messages.append(list(request.messages))
                    if self.calls == 1:
                        return CanonicalLLMOutcome(text="done without tools")
                    if self.calls == 2:
                        return CanonicalLLMOutcome(
                            text="",
                            tool_calls=[CanonicalToolCall(name="op_fake_write", args={"content": "OK"})],
                        )
                    return CanonicalLLMOutcome(text="milestone done with evidence")

            class FakeExecution:
                def get_capability_spec(self, name):
                    if name != "op_fake_write":
                        return None
                    return {
                        "name": "op_fake_write",
                        "description": "write evidence",
                        "parameters_schema": {
                            "type": "object",
                            "properties": {"content": {"type": "string"}},
                            "required": ["content"],
                        },
                    }

                async def execute_tool_async(self, call, **kwargs):
                    _ = kwargs
                    executed.append(dict(call.args))
                    (repo / "evidence.txt").write_text(str(call.args["content"]), encoding="utf-8")
                    return CanonicalToolResult(name=call.name, ok=True, text="written", llm_text="written", status=RuntimeStatus.OK)

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return None

            code = await MinionRunner(
                runtime_root=self.root,
                pack=repo_pack,
                minion_id="m_nudge",
                run_id="r_nudge",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=FakeLLM(), execution_runtime=FakeExecution()),
            ).run()

            self.assertEqual(code, 0)
            self.assertIn("You have not used any capability yet", llm_messages[1][-1]["content"])
            self.assertEqual(executed, [{"content": "OK"}])
            self.assertEqual((repo / "evidence.txt").read_text(encoding="utf-8"), "OK")
            checkpoint = next(event for event in events if event["event_kind"] == "checkpoint")
            self.assertEqual(checkpoint["payload"]["status"], "completed")

        asyncio.run(scenario())

    def test_runner_emits_progress_during_llm_and_tool_rounds(self) -> None:
        async def scenario() -> None:
            events = []
            repo_pack = prepare_git_task_environment(
                self.root,
                TaskContextPack(
                    work_order_id="wo_progress",
                    goal="write progress evidence",
                    continuity={"current_milestone": {"milestone_index": 0, "title": "progress milestone"}},
                    allowed_capabilities=["op_fake_write"],
                ),
            )
            repo = Path(repo_pack.workspace["repo_path"])

            class FakeLLM:
                def __init__(self):
                    self.calls = 0

                async def agenerate(self, request):
                    _ = request
                    self.calls += 1
                    if self.calls == 1:
                        return CanonicalLLMOutcome(
                            text="",
                            tool_calls=[CanonicalToolCall(name="op_fake_write", args={"content": "PROGRESS_OK"})],
                        )
                    return CanonicalLLMOutcome(text="progress milestone done")

            class FakeExecution:
                def get_capability_spec(self, name):
                    if name != "op_fake_write":
                        return None
                    return {
                        "name": "op_fake_write",
                        "description": "write progress evidence",
                        "parameters_schema": {
                            "type": "object",
                            "properties": {"content": {"type": "string"}},
                            "required": ["content"],
                        },
                    }

                async def execute_tool_async(self, call, **kwargs):
                    _ = kwargs
                    (repo / "progress.txt").write_text(str(call.args["content"]), encoding="utf-8")
                    return CanonicalToolResult(name=call.name, ok=True, text="progress written", llm_text="progress written", status=RuntimeStatus.OK)

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return None

            code = await MinionRunner(
                runtime_root=self.root,
                pack=repo_pack,
                minion_id="m_progress",
                run_id="r_progress",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=FakeLLM(), execution_runtime=FakeExecution()),
            ).run()

            self.assertEqual(code, 0)
            progress_events = [event["payload"] for event in events if event["event_kind"] == "progress"]
            phases = [event["phase"] for event in progress_events]
            self.assertIn("llm_round_started", phases)
            self.assertIn("llm_round_completed", phases)
            self.assertIn("tool_call_started", phases)
            self.assertIn("tool_call_completed", phases)
            self.assertIn("milestone_finalizing", phases)
            started = next(event for event in progress_events if event["phase"] == "tool_call_started")
            self.assertEqual(started["target_name"], "op_fake_write")
            self.assertEqual(started["milestone_title"], "progress milestone")
            completed = next(event for event in progress_events if event["phase"] == "tool_call_completed")
            self.assertTrue(completed["ok"])
            self.assertEqual((repo / "progress.txt").read_text(encoding="utf-8"), "PROGRESS_OK")

        asyncio.run(scenario())

    def test_runner_emits_heartbeat_while_llm_waits(self) -> None:
        async def scenario() -> None:
            events = []

            class FakeLLM:
                async def agenerate(self, request):
                    _ = request
                    await asyncio.sleep(0.05)
                    return CanonicalLLMOutcome(text="heartbeat complete")

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return None

            code = await MinionRunner(
                runtime_root=self.root,
                pack=TaskContextPack(
                    work_order_id="wo_heartbeat",
                    goal="complete after a slow llm call",
                    metadata={"heartbeat_interval_seconds": 0.01, "allow_text_only_completion": True},
                ),
                minion_id="m_heartbeat",
                run_id="r_heartbeat",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=FakeLLM(), execution_runtime=SimpleNamespace()),
            ).run()

            self.assertEqual(code, 0)
            progress_events = [event["payload"] for event in events if event["event_kind"] == "progress"]
            heartbeats = [event for event in progress_events if event["phase"] == "llm_round_waiting"]
            self.assertGreaterEqual(len(heartbeats), 1)
            self.assertEqual(heartbeats[0]["round"], 1)
            self.assertGreaterEqual(heartbeats[0]["heartbeat_count"], 1)

        asyncio.run(scenario())

    def test_runner_writes_debug_log_when_manager_configures_path(self) -> None:
        async def scenario() -> None:
            events = []
            log_path = self.root / "wo_debug.planner.log"

            class FakeLLM:
                async def agenerate(self, request):
                    _ = request
                    return CanonicalLLMOutcome(text="debug log complete")

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return None

            code = await MinionRunner(
                runtime_root=self.root,
                pack=TaskContextPack(
                    work_order_id="wo_debug",
                    goal="write debug log",
                    minion_profile="planner",
                    metadata={
                        "allow_text_only_completion": True,
                        "debug_log": {"enabled": True, "path": str(log_path)},
                    },
                ),
                minion_id="m_debug_log",
                run_id="r_debug_log",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=FakeLLM(), execution_runtime=SimpleNamespace()),
            ).run()

            self.assertEqual(code, 0)
            self.assertTrue(log_path.exists())
            records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            sections = [record["section"] for record in records]
            self.assertIn("runner_started", sections)
            self.assertIn("llm_request", sections)
            self.assertIn("llm_outcome", sections)
            self.assertIn("runner_event", sections)
            self.assertIn("runner_stopped", sections)
            self.assertTrue(all(record["work_order_id"] == "wo_debug" for record in records))
            self.assertTrue(all(record["minion_profile"] == "planner" for record in records))
            self.assertTrue(all(record["run_id"] == "r_debug_log" for record in records))

        asyncio.run(scenario())

    def test_workspace_read_tools_are_scoped_to_repo_path(self) -> None:
        async def scenario() -> None:
            repo = self.root / "workspace"
            repo.mkdir()
            (repo / "src").mkdir()
            (repo / "src" / "app.py").write_text("def target():\n    return 'ok'\n", encoding="utf-8")
            (self.root / "outside.txt").write_text("secret", encoding="utf-8")

            class FakeBase:
                def list_capability_specs(self):
                    return []

                def get_capability_spec(self, name):
                    _ = name
                    return None

                async def execute_tool_async(self, call, **kwargs):
                    raise AssertionError("workspace tools must not delegate to base runtime")

            runtime = MinionScopedExecutionRuntime(
                FakeBase(),
                ["op_workspace_tree", "op_workspace_search", "op_workspace_read"],
                {"repo_path": str(repo)},
            )

            specs = {spec["name"] for spec in runtime.list_capability_specs()}
            self.assertIn("op_workspace_tree", specs)
            self.assertIn("op_workspace_search", specs)
            self.assertIn("op_workspace_read", specs)

            tree = await runtime.execute_tool_async(CanonicalToolCall(name="op_workspace_tree", args={"path": "."}), turn_id="r")
            self.assertTrue(tree.ok)
            self.assertIn("src/app.py", tree.text)

            search = await runtime.execute_tool_async(CanonicalToolCall(name="op_workspace_search", args={"query": "target"}), turn_id="r")
            self.assertTrue(search.ok)
            self.assertIn("src/app.py:1", search.text)

            read = await runtime.execute_tool_async(CanonicalToolCall(name="op_workspace_read", args={"path": "src/app.py"}), turn_id="r")
            self.assertTrue(read.ok)
            self.assertIn("1: def target", read.text)

            escaped = await runtime.execute_tool_async(CanonicalToolCall(name="op_workspace_read", args={"path": "../outside.txt"}), turn_id="r")
            self.assertFalse(escaped.ok)
            self.assertIn("escapes", escaped.text)

        asyncio.run(scenario())

    def test_minion_artifact_write_is_scoped_to_artifact_dir(self) -> None:
        async def scenario() -> None:
            artifact_dir = self.root / "artifact_dir"
            produced: list[dict] = []

            class FakeBase:
                def list_capability_specs(self):
                    return []

                def get_capability_spec(self, name):
                    _ = name
                    return None

                async def execute_tool_async(self, call, **kwargs):
                    _ = call
                    _ = kwargs
                    raise AssertionError("artifact tool must not delegate to base runtime")

            runtime = MinionScopedExecutionRuntime(
                FakeBase(),
                ["op_minion_artifact_write"],
                {"artifact_dir": str(artifact_dir)},
                produced_artifacts=produced,
            )

            specs = {spec["name"] for spec in runtime.list_capability_specs()}
            self.assertIn("op_minion_artifact_write", specs)

            result = await runtime.execute_tool_async(
                CanonicalToolCall(
                    name="op_minion_artifact_write",
                    args={
                        "relative_path": "plan.md",
                        "content": "hello plan",
                        "title": "Plan",
                        "role": "primary",
                        "mime_type": "text/markdown",
                    },
                ),
                turn_id="r",
            )

            self.assertTrue(result.ok)
            self.assertEqual((artifact_dir / "plan.md").read_text(encoding="utf-8"), "hello plan")
            self.assertEqual(produced[0]["relative_path"], "plan.md")
            self.assertEqual(produced[0]["role"], "primary")
            self.assertTrue(produced[0]["sha256"])

            escaped = await runtime.execute_tool_async(
                CanonicalToolCall(name="op_minion_artifact_write", args={"relative_path": "../escape.md", "content": "no"}),
                turn_id="r",
            )
            self.assertFalse(escaped.ok)
            self.assertIn("escapes artifact_dir", escaped.text)
            self.assertFalse((self.root / "escape.md").exists())

        asyncio.run(scenario())

    def test_manager_summarizes_progress_state_for_read_run(self) -> None:
        manager = MinionManager(self.root)
        pack = TaskContextPack(
            work_order_id="wo_state",
            goal="state",
            continuity={"current_milestone": {"milestone_index": 1, "title": "Inspect state"}},
        )
        state = MinionRunState(minion_id="m_state", run_id="r_state", pack=pack)
        manager.runs[state.run_id] = state

        manager._record_event(
            state,
            {
                "event_kind": "progress",
                "payload": {"phase": "llm_round_started", "round": 3, "summary": "round 3"},
                "created_at": "2026-01-01T00:00:00Z",
            },
        )
        manager._record_event(
            state,
            {
                "event_kind": "progress",
                "payload": {"phase": "tool_call_completed", "tool_name": "op_workspace_read", "target_name": "op_workspace_read", "ok": True},
                "created_at": "2026-01-01T00:00:01Z",
            },
        )

        detail = manager.read_run("r_state")

        self.assertEqual(detail["last_phase"], "tool_call_completed")
        self.assertEqual(detail["last_event_at"], "2026-01-01T00:00:01Z")
        self.assertEqual(detail["last_tool_call"]["target_name"], "op_workspace_read")
        self.assertEqual(detail["llm_round_count"], 3)
        self.assertEqual(detail["tool_call_count"], 1)
        self.assertEqual(detail["current_milestone"]["title"], "Inspect state")

    def test_runner_has_no_default_tool_round_limit(self) -> None:
        async def scenario() -> None:
            events = []
            repo_pack = prepare_git_task_environment(
                self.root,
                TaskContextPack(
                    work_order_id="wo_many_tools",
                    goal="use many tool calls",
                    continuity={"current_milestone": {"milestone_index": 0, "title": "many tools"}},
                    allowed_capabilities=["op_fake_append"],
                ),
            )
            repo = Path(repo_pack.workspace["repo_path"])

            class FakeLLM:
                def __init__(self):
                    self.calls = 0

                async def agenerate(self, request):
                    _ = request
                    self.calls += 1
                    if self.calls <= 12:
                        return CanonicalLLMOutcome(
                            text="",
                            tool_calls=[CanonicalToolCall(name="op_fake_append", args={"value": self.calls})],
                        )
                    return CanonicalLLMOutcome(text="completed after many tool calls")

            class FakeExecution:
                def get_capability_spec(self, name):
                    if name != "op_fake_append":
                        return None
                    return {
                        "name": "op_fake_append",
                        "description": "append evidence",
                        "parameters_schema": {"type": "object", "properties": {"value": {"type": "integer"}}},
                    }

                async def execute_tool_async(self, call, **kwargs):
                    _ = kwargs
                    with (repo / "many.txt").open("a", encoding="utf-8") as handle:
                        handle.write(f"{call.args['value']}\n")
                    return CanonicalToolResult(name=call.name, ok=True, text="appended", llm_text="appended", status=RuntimeStatus.OK)

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return None

            code = await MinionRunner(
                runtime_root=self.root,
                pack=repo_pack,
                minion_id="m_many",
                run_id="r_many",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=FakeLLM(), execution_runtime=FakeExecution()),
            ).run()

            self.assertEqual(code, 0)
            checkpoint = next(event for event in events if event["event_kind"] == "checkpoint")
            self.assertEqual(checkpoint["payload"]["status"], "completed")
            self.assertEqual(len((repo / "many.txt").read_text(encoding="utf-8").strip().splitlines()), 12)

        asyncio.run(scenario())

    def test_runner_explicit_tool_round_limit_blocks_without_advancing(self) -> None:
        async def scenario() -> None:
            events = []
            repo_pack = prepare_git_task_environment(
                self.root,
                TaskContextPack(
                    work_order_id="wo_explicit_limit",
                    goal="debug limit",
                    continuity={"current_milestone": {"milestone_index": 0, "title": "limited"}},
                    allowed_capabilities=["op_fake_read"],
                    metadata={"max_tool_rounds": 2},
                ),
            )

            class FakeLLM:
                async def agenerate(self, request):
                    _ = request
                    return CanonicalLLMOutcome(text="", tool_calls=[CanonicalToolCall(name="op_fake_read", args={})])

            class FakeExecution:
                def get_capability_spec(self, name):
                    if name != "op_fake_read":
                        return None
                    return {"name": "op_fake_read", "description": "read", "parameters_schema": {"type": "object", "properties": {}}}

                async def execute_tool_async(self, call, **kwargs):
                    _ = call
                    _ = kwargs
                    return CanonicalToolResult(name="op_fake_read", ok=True, text="read", llm_text="read", status=RuntimeStatus.OK)

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return None

            code = await MinionRunner(
                runtime_root=self.root,
                pack=repo_pack,
                minion_id="m_limit",
                run_id="r_limit",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=FakeLLM(), execution_runtime=FakeExecution()),
            ).run()

            self.assertEqual(code, 0)
            checkpoint = next(event for event in events if event["event_kind"] == "checkpoint")
            self.assertEqual(checkpoint["payload"]["status"], "blocked")
            self.assertIn("explicit max_tool_rounds=2", checkpoint["payload"]["summary"])

        asyncio.run(scenario())

    def test_runner_explicit_tool_round_limit_finalizes_when_git_evidence_exists(self) -> None:
        async def scenario() -> None:
            events = []
            repo_pack = prepare_git_task_environment(
                self.root,
                TaskContextPack(
                    work_order_id="wo_limit_with_evidence",
                    goal="write before limit",
                    continuity={"current_milestone": {"milestone_index": 0, "title": "write before limit"}},
                    allowed_capabilities=["op_fake_write"],
                    metadata={"max_tool_rounds": 1},
                ),
            )
            repo = Path(repo_pack.workspace["repo_path"])

            class FakeLLM:
                async def agenerate(self, request):
                    _ = request
                    return CanonicalLLMOutcome(text="", tool_calls=[CanonicalToolCall(name="op_fake_write", args={})])

            class FakeExecution:
                def get_capability_spec(self, name):
                    if name != "op_fake_write":
                        return None
                    return {"name": "op_fake_write", "description": "write", "parameters_schema": {"type": "object", "properties": {}}}

                async def execute_tool_async(self, call, **kwargs):
                    _ = call
                    _ = kwargs
                    (repo / "evidence.txt").write_text("ok", encoding="utf-8")
                    return CanonicalToolResult(name="op_fake_write", ok=True, text="written", llm_text="written", status=RuntimeStatus.OK)

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return None

            code = await MinionRunner(
                runtime_root=self.root,
                pack=repo_pack,
                minion_id="m_limit_evidence",
                run_id="r_limit_evidence",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=FakeLLM(), execution_runtime=FakeExecution()),
            ).run()

            self.assertEqual(code, 0)
            checkpoint = next(event for event in events if event["event_kind"] == "checkpoint")
            self.assertEqual(checkpoint["payload"]["status"], "completed")
            self.assertTrue(checkpoint["payload"]["commit_sha"])

        asyncio.run(scenario())

    def test_runner_persists_text_deliverable_before_commit(self) -> None:
        async def scenario() -> None:
            events = []
            repo_pack = prepare_git_task_environment(
                self.root,
                TaskContextPack(
                    work_order_id="wo_text_deliverable",
                    goal="write planning report",
                    continuity={"current_milestone": {"milestone_index": 0, "title": "plan"}},
                    allowed_capabilities=["op_fake_read"],
                ),
            )
            repo = Path(repo_pack.workspace["repo_path"])

            class FakeLLM:
                def __init__(self):
                    self.calls = 0

                async def agenerate(self, request):
                    _ = request
                    self.calls += 1
                    if self.calls == 1:
                        return CanonicalLLMOutcome(text="", tool_calls=[CanonicalToolCall(name="op_fake_read", args={})])
                    return CanonicalLLMOutcome(text="Planner report body")

            class FakeExecution:
                def get_capability_spec(self, name):
                    if name != "op_fake_read":
                        return None
                    return {"name": "op_fake_read", "description": "read", "parameters_schema": {"type": "object", "properties": {}}}

                async def execute_tool_async(self, call, **kwargs):
                    _ = call
                    _ = kwargs
                    return CanonicalToolResult(name="op_fake_read", ok=True, text="read complete", llm_text="read complete", status=RuntimeStatus.OK)

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return None

            code = await MinionRunner(
                runtime_root=self.root,
                pack=repo_pack,
                minion_id="m_text",
                run_id="r_text",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=FakeLLM(), execution_runtime=FakeExecution()),
            ).run()

            self.assertEqual(code, 0)
            output_file = repo / "minion_outputs" / "wo_text_deliverable" / "milestone_0_generic.md"
            self.assertIn("Planner report body", output_file.read_text(encoding="utf-8"))
            checkpoint = next(event for event in events if event["event_kind"] == "checkpoint")
            self.assertEqual(checkpoint["payload"]["status"], "completed")
            self.assertTrue(checkpoint["payload"]["commit_sha"])
            self.assertEqual(checkpoint["payload"]["primary_artifact"]["relative_path"], "milestone_0_generic.md")
            terminal = next(event for event in events if event["event_kind"] == "terminal")
            self.assertEqual(terminal["payload"]["primary_artifact"]["relative_path"], "milestone_0_generic.md")
            self.assertLess(len(terminal["payload"]["summary"]), len("Planner report body") + 100)

        asyncio.run(scenario())

    def test_runner_persists_text_deliverable_for_folder_workspace(self) -> None:
        async def scenario() -> None:
            events = []
            registry = MinionProfileRegistry(runtime_root=self.root)
            pack = prepare_task_workspace(
                self.root,
                registry.resolve_pack(
                    TaskContextPack(
                        work_order_id="wo_folder_deliverable",
                        goal="write report",
                        continuity={"current_milestone": {"milestone_index": 0, "title": "report"}},
                    ),
                    requested_profile="generic",
                ),
                run_id="run_folder",
            )

            class FakeLLM:
                async def agenerate(self, request):
                    _ = request
                    return CanonicalLLMOutcome(text="Long report body for the folder minion")

            class FakeExecution:
                def list_capability_specs(self):
                    return []

                def get_capability_spec(self, name):
                    _ = name
                    return None

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return None

            code = await MinionRunner(
                runtime_root=self.root,
                pack=pack,
                minion_id="m_folder",
                run_id="r_folder",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=FakeLLM(), execution_runtime=FakeExecution()),
            ).run()

            self.assertEqual(code, 0)
            output_file = Path(pack.workspace["artifact_dir"]) / "milestone_0_generic.md"
            self.assertIn("Long report body", output_file.read_text(encoding="utf-8"))
            checkpoint = next(event for event in events if event["event_kind"] == "checkpoint")
            self.assertEqual(checkpoint["payload"]["status"], "completed")
            self.assertEqual(checkpoint["payload"]["primary_artifact"]["path"], str(output_file))
            terminal = next(event for event in events if event["event_kind"] == "terminal")
            self.assertEqual(terminal["payload"]["artifacts"][0]["relative_path"], "milestone_0_generic.md")

        asyncio.run(scenario())

    def test_runner_llm_error_blocks_without_fake_completion(self) -> None:
        async def scenario() -> None:
            events = []
            repo_pack = prepare_git_task_environment(
                self.root,
                TaskContextPack(work_order_id="wo_llm_error", goal="fail", continuity={"current_milestone": {"milestone_index": 0}}),
            )

            class FakeLLM:
                async def agenerate(self, request):
                    _ = request
                    return CanonicalLLMOutcome(text="LLM failed", finish_reason=LLMFinishReason.ERROR)

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return None

            code = await MinionRunner(
                runtime_root=self.root,
                pack=repo_pack,
                minion_id="m_error",
                run_id="r_error",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=FakeLLM(), execution_runtime=SimpleNamespace()),
            ).run()

            self.assertEqual(code, 0)
            checkpoint = next(event for event in events if event["event_kind"] == "checkpoint")
            self.assertEqual(checkpoint["payload"]["status"], "blocked")
            self.assertIn("LLM failed", checkpoint["payload"]["summary"])

        asyncio.run(scenario())

    def test_runner_does_not_expose_or_execute_denied_capabilities(self) -> None:
        async def scenario() -> None:
            events = []
            executed = []
            test_case = self
            pack = TaskContextPack(
                work_order_id="wo_denied",
                goal="do not mutate Pal state",
                allowed_capabilities=["op_l3_commit_write", "op_minion_spawn", "op_exec_run"],
            )

            class FakeLLM:
                async def agenerate(self, request):
                    tool_names = {item["function"]["name"] for item in request.tools}
                    test_case.assertNotIn("op_l3_commit_write", tool_names)
                    test_case.assertNotIn("op_minion_spawn", tool_names)
                    test_case.assertIn("op_exec_run", tool_names)
                    return CanonicalLLMOutcome(
                        text="",
                        tool_calls=[CanonicalToolCall(name="op_l3_commit_write", args={"title": "bad"})],
                    )

            class FakeExecution:
                def get_capability_spec(self, name):
                    return {
                        "name": name,
                        "description": f"{name} spec",
                        "parameters_schema": {"type": "object", "properties": {}},
                    }

                async def execute_tool_async(self, call, **kwargs):
                    _ = kwargs
                    executed.append(call.name)
                    return CanonicalToolResult(
                        name=call.name,
                        ok=True,
                        text="should not execute",
                        llm_text="should not execute",
                        status=RuntimeStatus.OK,
                    )

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return None

            code = await MinionRunner(
                runtime_root=self.root,
                pack=pack,
                minion_id="m_policy",
                run_id="r_policy",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=FakeLLM(), execution_runtime=FakeExecution()),
            ).run()

            self.assertEqual(code, 0)
            self.assertEqual(executed, [])
            checkpoint = next(event for event in events if event["event_kind"] == "checkpoint")
            self.assertEqual(checkpoint["payload"]["status"], "blocked")
            terminal = next(event for event in events if event["event_kind"] == "terminal")
            self.assertEqual(terminal["payload"]["status"], "blocked")
            self.assertIn("denied", terminal["payload"]["summary"])

        asyncio.run(scenario())

    def test_runner_discovery_surface_filters_denied_capabilities(self) -> None:
        async def scenario() -> None:
            class FakeExecution:
                def __init__(self):
                    self.specs = {
                        name: {
                            "name": name,
                            "display_name": name,
                            "description": f"{name} description",
                            "family": "test",
                            "module_id": "fake",
                            "aliases": [],
                            "parameters_schema": {"type": "object", "properties": {}},
                        }
                        for name in (
                            "op_exec_disc_search",
                            "op_exec_disc_read",
                            "op_exec_capability_call",
                            "op_exec_run",
                            "op_web_search_query",
                            "op_web_fetch_read",
                            "op_l3_recall_query",
                            "intro_task_read",
                            "op_minion_spawn",
                            "op_l3_commit_write",
                            "op_l3_correct_patch",
                        )
                    }

                def list_capability_specs(self):
                    return list(self.specs.values())

                def get_capability_spec(self, name):
                    return self.specs.get(name)

            allowed = [
                "op_exec_disc_search",
                "op_exec_disc_read",
                "op_exec_capability_call",
                "op_exec_run",
                "op_web_search_query",
                "op_web_fetch_read",
                "op_l3_recall_query",
                "intro_task_read",
                "op_minion_spawn",
                "op_l3_commit_write",
                "op_l3_correct_patch",
            ]
            scoped = MinionScopedExecutionRuntime(FakeExecution(), allowed)
            tool_names = [item["function"]["name"] for item in _llm_tools_for_allowed(scoped, allowed)]

            self.assertEqual(
                tool_names,
                [
                    "op_exec_disc_search",
                    "op_exec_disc_read",
                    "op_exec_capability_call",
                    "op_exec_run",
                    "op_web_search_query",
                    "op_web_fetch_read",
                    "op_l3_recall_query",
                ],
            )

            result = await scoped.execute_tool_async(CanonicalToolCall(name="op_exec_disc_search", args={"top_k": 20}))
            hit_names = {item["name"] for item in result.structured["hits"]}
            self.assertIn("op_exec_run", hit_names)
            self.assertIn("op_web_search_query", hit_names)
            self.assertIn("op_web_fetch_read", hit_names)
            self.assertIn("op_l3_recall_query", hit_names)
            self.assertNotIn("intro_task_read", hit_names)
            self.assertNotIn("op_minion_spawn", hit_names)
            self.assertNotIn("op_l3_commit_write", hit_names)
            self.assertNotIn("op_l3_correct_patch", hit_names)

            denied_read = await scoped.execute_tool_async(CanonicalToolCall(name="op_exec_disc_read", args={"name": "op_minion_spawn"}))
            self.assertEqual(denied_read.status, RuntimeStatus.NOT_FOUND)

        asyncio.run(scenario())

    def test_slim_runner_runtime_publishes_resident_work_capabilities(self) -> None:
        async def scenario() -> None:
            bundle = build_slim_minion_runtime(self.root)
            try:
                names = {spec["name"] for spec in bundle.execution_runtime.list_capability_specs()}
                self.assertIn("op_exec_run", names)
                self.assertIn("op_web_search_query", names)
                self.assertIn("op_web_fetch_read", names)
                self.assertIn("op_l3_recall_query", names)
            finally:
                await bundle.close()

        asyncio.run(scenario())

    def test_runner_system_prompt_requires_recall_after_tool_failure(self) -> None:
        prompt = _render_system_prompt(
            {
                "identity": "You are a coder.",
                "behavior": "Do the milestone.",
                "output_contract": "Summarize results.",
                "allowed_capabilities": ["op_exec_run", "op_l3_recall_query"],
            }
        )

        self.assertIn("tool/capability call fails", prompt)
        self.assertIn("MUST call `op_l3_recall_query`", prompt)
        self.assertIn("before retrying, debugging further, or reporting blocked", prompt)
        self.assertIn("If completion evidence cannot be produced", prompt)
        self.assertNotIn("disposable task runner", prompt)

    def test_planner_prompt_is_read_only_and_milestone_handoff_oriented(self) -> None:
        registry = MinionProfileRegistry(runtime_root=self.root)
        pack = registry.resolve_pack(TaskContextPack(work_order_id="wo_planner_prompt", goal="plan module"), requested_profile="planner")
        prompt = _render_system_prompt(
            {
                "identity": pack.resolved_profile["identity_fragment"],
                "behavior": pack.resolved_profile["behavior_fragment"],
                "output_contract": pack.resolved_profile["output_contract_fragment"],
                "allowed_capabilities": pack.allowed_capabilities,
                "workspace_policy": pack.workspace["workspace_policy"],
                "completion_policy": pack.workspace["completion_policy"],
            }
        )

        self.assertIn("Stay read-only", prompt)
        self.assertIn("coder handoff instruction", prompt)
        self.assertIn("verification/test strategy", prompt)
        self.assertIn("op_workspace_read", prompt)
        self.assertNotIn("op_exec_run", pack.allowed_capabilities)
        self.assertNotIn("disposable task runner", prompt)

    async def _start_manager(self):
        manager = MinionManager(runtime_root=self.root)
        manager_task = asyncio.create_task(manager.run())
        client = MinionManagerClient(runtime_root=self.root, request_timeout_seconds=3.0)
        for _ in range(80):
            try:
                health = await client.request("health")
                if health.get("ok"):
                    return manager, manager_task, client
            except Exception:
                await asyncio.sleep(0.05)
        manager_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await manager_task
        self.fail("minion manager did not become healthy")

    async def _shutdown_manager(self, client: MinionManagerClient, task: asyncio.Task) -> None:
        with contextlib.suppress(Exception):
            await client.request("shutdown")
        await asyncio.wait_for(task, timeout=5.0)

    async def _subscribe_events(self):
        reader, writer = await open_manager_connection(self.root)
        request_id = "sub-test"
        writer.write(
            pack_sidecar_message(
                {
                    "type": "request",
                    "id": request_id,
                    "method": "subscribe_events",
                    "params": {},
                }
            )
        )
        await writer.drain()
        response = await asyncio.wait_for(read_sidecar_message(reader), timeout=2)
        self.assertTrue(response.get("ok"), response)
        self.assertEqual(response.get("id"), request_id)
        return reader, writer

    async def _read_subscribed_until(self, reader, predicate) -> dict:
        seen = []
        for _ in range(120):
            try:
                frame = await asyncio.wait_for(read_sidecar_message(reader), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if str(frame.get("type") or "") != "event":
                continue
            event = frame.get("event")
            if not isinstance(event, dict):
                continue
            seen.append(event)
            if predicate(event):
                return event
        self.fail(f"expected minion event not observed; seen={seen}")

    async def _close_subscription(self, writer) -> None:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


class MinionIntegrationTests(unittest.TestCase):
    def test_minion_capabilities_are_registered_under_expected_names(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_core_test_") as tmp:
            core = PalCore()
            register_execution_with_core(core.context)
            handle = register_minion_with_core(core.context, runtime_root=Path(tmp))
            core.publish_module_capabilities("minion")
            try:
                names = set(core.context.capability_registry.descriptors)
                self.assertIn("intro_module_minion_show", names)
                self.assertIn("intro_minion_list", names)
                self.assertIn("intro_minion_read", names)
                self.assertIn("intro_task_search", names)
                self.assertIn("intro_task_read", names)
                self.assertIn("intro_work_order_search", names)
                self.assertIn("intro_work_order_read", names)
                self.assertIn("intro_work_order_draft_search", names)
                self.assertIn("intro_work_order_draft_read", names)
                self.assertIn("intro_minion_profile_list", names)
                self.assertIn("intro_minion_profile_read", names)
                self.assertIn("op_minion_draft_work_order", names)
                self.assertIn("op_minion_promote_work_order_draft", names)
                self.assertIn("op_minion_spawn", names)
                self.assertIn("op_minion_kill", names)
                self.assertIn("op_minion_finalize", names)
                self.assertNotIn("op_minion_decision_send", names)
            finally:
                handle.shutdown_sync()

    def test_minion_declares_discoverable_advisor_affordance_without_persisting_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_affordance_test_") as tmp:
            root = Path(tmp)
            database = PalV2Database(root / "pal.sqlite3")
            database.initialize([BehaviorAffordanceModel, BehaviorSkillModel])
            handle = None
            try:
                behavior_repository = BehaviorRepository()
                behavior_service = BehaviorService(repository=behavior_repository)
                core = PalCore()
                register_execution_with_core(core.context)
                register_behavior_with_core(core.context, behavior_service)
                handle = register_minion_with_core(core.context, runtime_root=root)
                core.publish_module_capabilities("minion")

                advice = asyncio.run(
                    behavior_service.advise_async(
                        BehaviorAdviceRequest(
                            scenario=(
                                "Delegate this professional asynchronous module-scoped coding task into a SPEC work order "
                                "with milestones, repository research, implementation, and test repair."
                            ),
                            top_k=10,
                        )
                    )
                )
                candidates = {candidate.affordance_id: candidate for candidate in advice.candidates}
                candidate = candidates["declared.minion.delegate_professional_work"]

                self.assertIsNone(behavior_repository.get_affordance("declared.minion.delegate_professional_work"))
                self.assertIsNone(behavior_repository.get_affordance("declared.minion.natural_language_takeover"))
                self.assertIn("op_minion_draft_work_order", candidate.capability_refs)
                self.assertIn("op_minion_promote_work_order_draft", candidate.capability_refs)
                self.assertIn("intro_work_order_draft_read", candidate.capability_refs)
                self.assertIn("intro_minion_profile_list", candidate.capability_refs)
                self.assertIn("intro_minion_profile_read", candidate.capability_refs)
                self.assertIn("op_minion_spawn", candidate.capability_refs)
                self.assertIn("intro_work_order_read", candidate.capability_refs)
                self.assertIn("draft", candidate.prompt_hint.lower())
                self.assertIn("planner", candidate.prompt_hint.lower())
                self.assertIn("profile", candidate.prompt_hint.lower())
                self.assertIn("registered", candidate.prompt_hint.lower())
                system_prompt = core.build_canonical_prompt(PromptAssemblyContext()).messages[0]["content"]
                self.assertNotIn("Delegate professional work to Minion", system_prompt)
                self.assertNotIn("Control or inspect active Minion work", system_prompt)

                takeover = asyncio.run(
                    behavior_service.advise_async(
                        BehaviorAdviceRequest(
                            scenario="用户问 minion 在干嘛，进度怎么样，然后说换掉它并继续这个 work order。",
                            top_k=10,
                        )
                    )
                )
                takeover_candidates = {item.affordance_id: item for item in takeover.candidates}
                self.assertIn("declared.minion.natural_language_takeover", takeover_candidates)
                self.assertIn("op_minion_kill", takeover_candidates["declared.minion.natural_language_takeover"].capability_refs)
                self.assertIn("op_minion_spawn", takeover_candidates["declared.minion.natural_language_takeover"].capability_refs)

                core.detach_module("minion")
                after = asyncio.run(
                    behavior_service.advise_async(
                        BehaviorAdviceRequest(
                            scenario="Delegate this professional asynchronous module-scoped coding task into milestones.",
                            top_k=10,
                        )
                    )
                )
                self.assertNotIn("declared.minion.delegate_professional_work", {item.affordance_id for item in after.candidates})
                self.assertNotIn("declared.minion.natural_language_takeover", {item.affordance_id for item in after.candidates})
            finally:
                if handle is not None:
                    handle.shutdown_sync()
                database.close()

    def test_work_order_draft_capabilities_create_read_and_search(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_draft_cap_test_") as tmp:
            core = PalCore()
            register_execution_with_core(core.context)
            handle = register_minion_with_core(core.context, runtime_root=Path(tmp))
            core.publish_module_capabilities("minion")
            try:
                created = core.context.execution_runtime.execute(
                    CapabilityCall(
                        name="op_minion_draft_work_order",
                        args={
                            "title": "Minion control affordance",
                            "goal": "Let Pal inspect and replace active minions by facts.",
                            "source_summary": "Natural language should resolve current worker through introspection.",
                            "milestones": ["add affordance", "test detach behavior"],
                        },
                    )
                )
                self.assertEqual(created.status, "ok")
                draft_id = created.structured["draft"]["draft_id"]

                searched = core.context.execution_runtime.execute(
                    CapabilityCall(name="intro_work_order_draft_search", args={"query": "replace active minions"})
                )
                read = core.context.execution_runtime.execute(
                    CapabilityCall(name="intro_work_order_draft_read", args={"draft_id": draft_id})
                )

                self.assertEqual(searched.status, "ok")
                self.assertEqual(searched.structured["items"][0]["draft_id"], draft_id)
                self.assertEqual(read.structured["work_order_candidate"]["metadata"]["work_order_draft_id"], draft_id)
            finally:
                handle.shutdown_sync()

    def test_promote_work_order_draft_capability_and_spawn_from_draft(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_promote_cap_test_") as tmp:
            core = PalCore()
            register_execution_with_core(core.context)
            handle = register_minion_with_core(core.context, runtime_root=Path(tmp))
            core.publish_module_capabilities("minion")
            try:
                created = core.context.execution_runtime.execute(
                    CapabilityCall(
                        name="op_minion_draft_work_order",
                        args={
                            "title": "Draft promotion path",
                            "goal": "Promote draft into a formal work order",
                            "task_id": "task_draft_promotion",
                            "proposed_work_order_id": "wo_draft_promotion",
                            "milestones": ["promote", "spawn"],
                        },
                    )
                )
                draft_id = created.structured["draft"]["draft_id"]
                promoted = core.context.execution_runtime.execute(
                    CapabilityCall(name="op_minion_promote_work_order_draft", args={"draft_id": draft_id})
                )
                read = core.context.execution_runtime.execute(
                    CapabilityCall(name="intro_work_order_read", args={"work_order_id": "wo_draft_promotion"})
                )

                self.assertEqual(promoted.status, "ok")
                self.assertEqual(read.status, "ok")
                self.assertEqual(read.structured["work_order"]["task_id"], "task_draft_promotion")

                created_2 = core.context.execution_runtime.execute(
                    CapabilityCall(
                        name="op_minion_draft_work_order",
                        args={
                            "title": "Spawn from draft",
                            "goal": "Let spawn promote draft through the unified entry",
                            "task_id": "task_spawn_from_draft",
                            "proposed_work_order_id": "wo_spawn_from_draft",
                        },
                    )
                )
                spawned = core.context.execution_runtime.execute(
                    CapabilityCall(
                        name="op_minion_spawn",
                        args={"draft_id": created_2.structured["draft"]["draft_id"], "minion_profile": "planner"},
                    )
                )
                self.assertEqual(spawned.status, "ok")
                self.assertEqual(spawned.structured["work_order_id"], "wo_spawn_from_draft")
            finally:
                handle.shutdown_sync()

    def test_builtin_profiles_are_listed_and_readable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_profile_test_") as tmp:
            core = PalCore()
            register_execution_with_core(core.context)
            handle = register_minion_with_core(core.context, runtime_root=Path(tmp))
            core.publish_module_capabilities("minion")
            try:
                listed = core.context.execution_runtime.execute(CapabilityCall(name="intro_minion_profile_list"))
                self.assertEqual(listed.status, "ok")
                profile_ids = {item["profile_id"] for item in listed.structured["items"]}
                self.assertIn("generic", profile_ids)
                self.assertIn("planner", profile_ids)
                self.assertIn("coder", profile_ids)
                self.assertNotIn("architect", profile_ids)
                self.assertEqual(listed.structured["runtime_profile_dir"], str(Path(tmp) / "plugins" / "minion" / "profiles"))
                self.assertIn("runtime TOML", listed.structured["profile_source_order"])
                self.assertIn("profile_id", listed.structured["usage"])

                planner = core.context.execution_runtime.execute(CapabilityCall(name="intro_minion_profile_read", args={"profile_id": "planner"}))
                self.assertEqual(planner.status, "ok")
                self.assertEqual(planner.structured["profile_group"], "software_engineering")
                self.assertEqual(planner.structured["workspace_policy"]["mode"], "read_only_repo")
                self.assertIn("work order", planner.structured["identity_fragment"].lower())

                read = core.context.execution_runtime.execute(CapabilityCall(name="intro_minion_profile_read", args={"profile_id": "reviewer"}))
                self.assertEqual(read.status, "ok")
                self.assertEqual(read.structured["profile_id"], "reviewer")

                spawn_spec = core.context.execution_runtime.get_capability_spec("op_minion_spawn")
                self.assertIsNotNone(spawn_spec)
                assert spawn_spec is not None
                self.assertIn("intro_minion_profile_list", spawn_spec["description"])
                self.assertIn("intro_minion_profile_read", spawn_spec["description"])
                self.assertIn("runtime_root/plugins/minion/profiles/*.toml", spawn_spec["description"])
                self.assertIn("Do not spawn from a bare goal/profile handoff", spawn_spec["description"])
                self.assertIn("task_title", spawn_spec["description"])
                self.assertIn("milestones", spawn_spec["description"])
                self.assertIn("acceptance_criteria", spawn_spec["description"])
                self.assertIn("module_boundaries", spawn_spec["description"])
                self.assertIn("preferred_endpoint_id", spawn_spec["description"])
                pack_arg = spawn_spec["parameters_schema"]["properties"]["task_context_pack"]
                self.assertIn("goal-only packet", pack_arg["description"])
                self.assertIn("instruction", pack_arg["required"])
                self.assertIn("acceptance_criteria", pack_arg["required"])
                self.assertIn("metadata", pack_arg["required"])
                self.assertIn("milestones", pack_arg["properties"]["metadata"]["required"])
                profile_arg = spawn_spec["parameters_schema"]["properties"]["minion_profile"]
                self.assertIn("profile_id", profile_arg["description"])
                endpoint_arg = spawn_spec["parameters_schema"]["properties"]["preferred_endpoint_id"]
                self.assertIn("active endpoint", endpoint_arg["description"])

                draft_spec = core.context.execution_runtime.get_capability_spec("op_minion_draft_work_order")
                self.assertIsNotNone(draft_spec)
                assert draft_spec is not None
                self.assertIn("Draft a complete minion work order", draft_spec["description"])
                self.assertIn("Do not create a lazy", draft_spec["description"])
                draft_required = draft_spec["parameters_schema"]["required"]
                self.assertIn("instruction", draft_required)
                self.assertIn("source_summary", draft_required)
                self.assertIn("conversation_summary", draft_required)
                self.assertIn("module_boundaries", draft_required)
                self.assertIn("milestones", draft_required)
                self.assertIn("acceptance_criteria", draft_required)
                self.assertIn("workspace", draft_required)
                self.assertIn("artifacts", draft_required)
            finally:
                handle.shutdown_sync()

    def test_architect_profile_is_not_builtin_after_merge(self) -> None:
        registry = MinionProfileRegistry()

        self.assertIsNone(registry.get("architect"))
        with self.assertRaises(KeyError):
            registry.resolve_pack(TaskContextPack(work_order_id="wo_arch_removed", goal="design boundaries"), requested_profile="architect")

    def test_minion_detach_removes_capabilities_event_source_and_control_handler(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_detach_test_") as tmp:
            core = PalCore()
            register_execution_with_core(core.context)
            handle = register_minion_with_core(core.context, runtime_root=Path(tmp))
            core.publish_module_capabilities("minion")
            try:
                self.assertIn("minion.manager", core.context.event_source_registry.sources)
                self.assertIn(EventKind.APPROVAL_REQUEST, core.context.event_handler_registry.handlers)
                self.assertIn(EventKind.MINION_PROGRESS, core.context.event_handler_registry.handlers)
                self.assertIn("minion_approval_decision", core.context.control_action_registry.handlers)
                self.assertIn("minion_lesson_decision", core.context.control_action_registry.handlers)
                self.assertIn("op_minion_spawn", core.context.capability_registry.descriptors)

                core.detach_module("minion")

                self.assertNotIn("minion.manager", core.context.event_source_registry.sources)
                self.assertNotIn(EventKind.APPROVAL_REQUEST, core.context.event_handler_registry.handlers)
                self.assertNotIn(EventKind.MINION_PROGRESS, core.context.event_handler_registry.handlers)
                self.assertNotIn("minion_approval_decision", core.context.control_action_registry.handlers)
                self.assertNotIn("minion_lesson_decision", core.context.control_action_registry.handlers)
                self.assertNotIn("op_minion_spawn", core.context.capability_registry.descriptors)
            finally:
                handle.shutdown_sync()

    def test_public_minion_detach_capability_routes_through_core_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_public_detach_test_") as tmp:
            core = PalCore()
            register_execution_with_core(core.context)
            handle = register_minion_with_core(core.context, runtime_root=Path(tmp))
            core.publish_module_capabilities("minion")
            try:
                runtime = core.context.execution_runtime
                self.assertIn("op_minion_detach", {spec["name"] for spec in runtime.list_capability_specs()})
                self.assertIn("op_minion_spawn", runtime.compiled_capability_index.records)

                result = runtime.execute(CapabilityCall(name="op_minion_detach"))

                self.assertEqual(result.status, RuntimeStatus.OK)
                self.assertEqual(result.structured["lifecycle_controller"], "core")
                self.assertNotIn("op_minion_detach", {spec["name"] for spec in runtime.list_capability_specs()})
                self.assertNotIn("op_minion_spawn", runtime.compiled_capability_index.records)
                self.assertNotIn("op_minion_spawn", core.context.capability_registry.descriptors)
                self.assertNotIn("minion.manager", core.context.event_source_registry.sources)
            finally:
                handle.shutdown_sync()

    def test_minion_lifecycle_reattach_starts_manager(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_attach_start_test_") as tmp:
            core = PalCore()
            register_execution_with_core(core.context)
            handle = register_minion_with_core(core.context, runtime_root=Path(tmp))
            core.publish_module_capabilities("minion")
            provider = handle.ports["minion"]
            try:
                self.assertFalse(provider._status_payload()["manager_running"])

                self.assertEqual(core.detach_module("minion"), RuntimeStatus.OK)
                self.assertEqual(core.reattach_module("minion"), RuntimeStatus.OK)

                status = provider._status_payload()
                self.assertFalse(status["degraded"])
                self.assertTrue(status["manager_running"])
                self.assertTrue(status["ok"])
                self.assertIn(EventKind.APPROVAL_REQUEST, core.context.event_handler_registry.handlers)
                self.assertIn(EventKind.MINION_PROGRESS, core.context.event_handler_registry.handlers)
                self.assertIn("minion_lesson_decision", core.context.control_action_registry.handlers)
            finally:
                handle.shutdown_sync()

    def test_dynamic_profile_provider_is_runtime_only_and_respects_mount_state(self) -> None:
        class FakeProfileProvider:
            def declared_minion_profiles(self):
                return [
                    MinionProfile(
                        profile_id="designer",
                        display_name="Designer Minion",
                        identity_fragment="Design interfaces.",
                    )
                ]

        with tempfile.TemporaryDirectory(prefix="pal_minion_profile_test_") as tmp:
            core = PalCore()
            register_execution_with_core(core.context)
            handle = register_minion_with_core(core.context, runtime_root=Path(tmp))
            fake = ModuleHandle(
                module_id="fake_profile",
                tier=MODULE_TIER_DETACHABLE,
                ports={"minion_profile_provider:fake": FakeProfileProvider()},
            )
            core.context.register_module(fake)
            core.publish_module_capabilities("minion")
            try:
                listed = core.context.execution_runtime.execute(CapabilityCall(name="intro_minion_profile_list"))
                profile_ids = {item["profile_id"] for item in listed.structured["items"]}
                self.assertIn("designer", profile_ids)

                fake.mounted = False
                listed = core.context.execution_runtime.execute(CapabilityCall(name="intro_minion_profile_list"))
                profile_ids = {item["profile_id"] for item in listed.structured["items"]}
                self.assertNotIn("designer", profile_ids)
            finally:
                handle.shutdown_sync()

    def test_spawn_resolves_profile_defaults_and_capability_hook(self) -> None:
        class FakeCapabilityProvider:
            def capabilities_for_minion_profile(self, profile, pack):
                _ = pack
                if profile.profile_id == "coder":
                    return ["op_fake_extra"]
                return []

        with tempfile.TemporaryDirectory(prefix="pal_minion_profile_spawn_test_") as tmp:
            core = PalCore()
            register_execution_with_core(core.context)
            handle = register_minion_with_core(core.context, runtime_root=Path(tmp))
            fake = ModuleHandle(
                module_id="fake_capability_policy",
                tier=MODULE_TIER_DETACHABLE,
                ports={"minion_profile_capability_provider:fake": FakeCapabilityProvider()},
            )
            core.context.register_module(fake)
            core.publish_module_capabilities("minion")
            try:
                spawned = core.context.execution_runtime.execute(
                    CapabilityCall(
                        name="op_minion_spawn",
                        args={
                            "minion_profile": "coder",
                            "preferred_endpoint_id": "coder_fast",
                            "task_context_pack": {"work_order_id": "wo_coder", "goal": "make a change"},
                        },
                    )
                )
                self.assertEqual(spawned.status, "ok")
                self.assertEqual(spawned.structured["minion_profile"], "coder")
                read = core.context.execution_runtime.execute(
                    CapabilityCall(name="intro_minion_read", args={"run_id": spawned.structured["run_id"]})
                )
                pack = read.structured["task_context_pack"]
                self.assertEqual(pack["minion_profile"], "coder")
                self.assertEqual(pack["resolved_profile"]["profile_id"], "coder")
                self.assertIn("op_exec_run", pack["allowed_capabilities"])
                self.assertIn("op_fake_extra", pack["allowed_capabilities"])
                self.assertEqual(pack["approval_policy"]["high_risk_capabilities"], ["op_exec_run"])
                self.assertEqual(pack["metadata"]["preferred_endpoint_id"], "coder_fast")
            finally:
                handle.shutdown_sync()

    def test_spawn_keeps_explicit_allowed_fields_and_unknown_profile_does_not_start_manager(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_profile_spawn_test_") as tmp:
            core = PalCore()
            register_execution_with_core(core.context)
            handle = register_minion_with_core(core.context, runtime_root=Path(tmp))
            provider = handle.ports["minion"]
            core.publish_module_capabilities("minion")
            try:
                unknown = core.context.execution_runtime.execute(
                    CapabilityCall(
                        name="op_minion_spawn",
                        args={
                            "minion_profile": "missing",
                            "task_context_pack": {"work_order_id": "wo_missing", "goal": "nope"},
                        },
                    )
                )
                self.assertEqual(unknown.status, "error")
                self.assertIsNone(provider.process)

                spawned = core.context.execution_runtime.execute(
                    CapabilityCall(
                        name="op_minion_spawn",
                        args={
                            "minion_profile": "planner",
                            "task_context_pack": {
                                "work_order_id": "wo_explicit",
                                "goal": "plan",
                                "allowed_capabilities": ["op_only_this"],
                                "allowed_skills": ["skill.only"],
                                "approval_policy": {"custom": True},
                            },
                        },
                    )
                )
                self.assertEqual(spawned.status, "ok")
                read = core.context.execution_runtime.execute(
                    CapabilityCall(name="intro_minion_read", args={"run_id": spawned.structured["run_id"]})
                )
                pack = read.structured["task_context_pack"]
                self.assertEqual(pack["allowed_capabilities"], ["op_only_this"])
                self.assertEqual(pack["allowed_skills"], ["skill.only"])
                self.assertTrue(pack["approval_policy"]["custom"])
            finally:
                handle.shutdown_sync()

    def test_spawn_can_resolve_single_work_order_query_but_not_ambiguous_query(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_query_spawn_test_") as tmp:
            root = Path(tmp)
            repository = MinionTaskingRepository(runtime_root=root)
            repository.prepare_pack_for_spawn(
                TaskContextPack(
                    work_order_id="wo_query_one",
                    goal="telegram reliability repair",
                    instruction="repair telegram by reading the stored work order facts",
                    metadata={"task_id": "task_query_one"},
                )
            )
            repository.prepare_pack_for_spawn(
                TaskContextPack(
                    work_order_id="wo_query_two",
                    goal="shared ambiguous work",
                    metadata={"task_id": "task_query_two"},
                )
            )
            repository.prepare_pack_for_spawn(
                TaskContextPack(
                    work_order_id="wo_query_three",
                    goal="shared ambiguous work extra",
                    metadata={"task_id": "task_query_three"},
                )
            )

            core = PalCore()
            register_execution_with_core(core.context)
            handle = register_minion_with_core(core.context, runtime_root=root)
            core.publish_module_capabilities("minion")
            try:
                spawned = core.context.execution_runtime.execute(
                    CapabilityCall(name="op_minion_spawn", args={"task_query": "telegram reliability"})
                )
                self.assertEqual(spawned.status, "ok")
                self.assertEqual(spawned.structured["work_order_id"], "wo_query_one")
                self.assertEqual(spawned.structured["instruction"], "repair telegram by reading the stored work order facts")

                ambiguous = core.context.execution_runtime.execute(
                    CapabilityCall(name="op_minion_spawn", args={"task_query": "shared ambiguous"})
                )
                self.assertEqual(ambiguous.status, "error")
                self.assertGreaterEqual(ambiguous.structured["candidate_count"], 2)
            finally:
                handle.shutdown_sync()

    def test_minion_event_source_maps_approval_to_control_payload(self) -> None:
        class Provider:
            def has_pending_events(self) -> bool:
                return True

            def drain_events_sync(self, *, limit: int = 20) -> dict:
                _ = limit
                return {
                    "events": [
                        {
                            "event_kind": "approval_requested",
                            "minion_id": "m1",
                            "run_id": "r1",
                            "work_order_id": "wo1",
                            "payload": {
                                "approval_id": "ap1",
                                "metadata": {
                                    "control_route": {
                                        "endpoint_id": "telegram_main",
                                        "channel_kind": "telegram",
                                        "reply_target": {"chat_id": "42"},
                                    }
                                },
                            },
                        }
                    ]
                }

        source = MinionEventSource(provider=Provider())
        events = source.drain(context=None)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_kind, EventKind.APPROVAL_REQUEST)
        self.assertEqual(events[0].source_kind, SourceKind.MINION)
        self.assertEqual(events[0].payload["route"]["endpoint_id"], "telegram_main")
        derived = MinionControlEventHandler().handle(events[0], context=None)
        self.assertEqual(derived[0].event_kind, EventKind.CONTROL_ACTION)
        action = derived[0].payload
        self.assertEqual(action.action_kind, "interactive_open")
        self.assertEqual(action.args["interaction"]["buttons"][0][0]["action_key"], "control.action.dispatch")
        self.assertEqual(action.args["interaction"]["buttons"][0][0]["action_args"]["action_kind"], "minion_approval_decision")

    def test_minion_event_source_maps_runner_progress(self) -> None:
        class Provider:
            def has_pending_events(self) -> bool:
                return True

            def drain_events_sync(self, *, limit: int = 20) -> dict:
                _ = limit
                return {
                    "events": [
                        {
                            "event_kind": "progress",
                            "minion_id": "m1",
                            "run_id": "r1",
                            "work_order_id": "wo1",
                            "minion_profile": "planner",
                            "payload": {
                                "phase": "tool_call_started",
                                "summary": "Tool started: op_exec_run",
                                "target_name": "op_exec_run",
                            },
                            "created_at": "2026-01-01T00:00:00Z",
                        }
                    ]
                }

        source = MinionEventSource(provider=Provider())
        events = source.drain(context=None)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_kind, EventKind.MINION_PROGRESS)
        self.assertEqual(events[0].source_kind, SourceKind.MINION)
        self.assertEqual(events[0].payload["event_kind"], "progress")
        self.assertEqual(events[0].payload["phase"], "tool_call_started")
        self.assertEqual(events[0].payload["minion_profile"], "planner")

    def test_minion_progress_event_is_manager_telemetry_only(self) -> None:
        event = EventEnvelope(
            event_kind=EventKind.MINION_PROGRESS,
            source_kind=SourceKind.MINION,
            payload={
                "phase": "llm_round_started",
                "summary": "LLM round 1 started",
                "minion_id": "m1",
                "run_id": "r1",
                "work_order_id": "wo1",
                "minion_profile": "planner",
                "route": {
                    "endpoint_id": "telegram_main",
                    "channel_kind": "telegram",
                    "reply_target": {"chat_id": "42"},
                },
            },
        )

        derived = MinionControlEventHandler().handle(event, context=None)

        self.assertEqual(derived, [])

    def test_minion_completed_checkpoint_is_manager_telemetry_only(self) -> None:
        event = EventEnvelope(
            event_kind=EventKind.MINION_CHECKPOINT,
            source_kind=SourceKind.MINION,
            payload={
                "status": "completed",
                "summary": "final milestone done",
                "minion_id": "m1",
                "run_id": "r1",
                "work_order_id": "wo1",
                "minion_profile": "generic",
                "route": {
                    "endpoint_id": "telegram_main",
                    "channel_kind": "telegram",
                    "reply_target": {"chat_id": "42"},
                },
            },
        )

        derived = MinionControlEventHandler().handle(event, context=None)

        self.assertEqual(derived, [])

    def test_minion_partial_checkpoint_is_manager_telemetry_only(self) -> None:
        event = EventEnvelope(
            event_kind=EventKind.MINION_CHECKPOINT,
            source_kind=SourceKind.MINION,
            payload={
                "status": "partial",
                "summary": "draft ready, continuing",
                "minion_id": "m1",
                "run_id": "r1",
                "work_order_id": "wo1",
                "minion_profile": "generic",
                "route": {
                    "endpoint_id": "telegram_main",
                    "channel_kind": "telegram",
                    "reply_target": {"chat_id": "42"},
                },
            },
        )

        derived = MinionControlEventHandler().handle(event, context=None)

        self.assertEqual(derived, [])

    def test_minion_terminal_event_keeps_control_route_for_user_notification(self) -> None:
        class Provider:
            def has_pending_events(self) -> bool:
                return True

            def drain_events_sync(self, *, limit: int = 20) -> dict:
                _ = limit
                return {
                    "events": [
                        {
                            "event_kind": "terminal",
                            "minion_id": "m1",
                            "run_id": "r1",
                            "work_order_id": "wo1",
                            "minion_profile": "coder",
                            "payload": {
                                "status": "completed",
                                "summary": "done",
                                "artifacts": [
                                    {
                                        "title": "Review",
                                        "relative_path": "review.md",
                                        "path": "/tmp/minion/review.md",
                                        "role": "primary",
                                    }
                                ],
                                "primary_artifact": {
                                    "title": "Review",
                                    "relative_path": "review.md",
                                    "path": "/tmp/minion/review.md",
                                    "role": "primary",
                                },
                                "metadata": {
                                    "control_route": {
                                        "endpoint_id": "telegram_main",
                                        "channel_kind": "telegram",
                                        "reply_target": {"chat_id": "42"},
                                    }
                                },
                            },
                        }
                    ]
                }

        source = MinionEventSource(provider=Provider())
        events = source.drain(context=None)

        self.assertEqual(events[0].event_kind, EventKind.MINION_TERMINAL)
        self.assertEqual(events[0].payload["route"]["endpoint_id"], "telegram_main")
        derived = MinionControlEventHandler().handle(events[0], context=None)
        self.assertEqual(len(derived), 1)
        action = derived[0].payload
        self.assertEqual(derived[0].event_kind, EventKind.CONTROL_ACTION)
        self.assertEqual(action.action_kind, "route_reply")
        self.assertEqual(action.route.reply_target["chat_id"], "42")
        self.assertIn("Minion finished: completed", action.args["text"])
        self.assertIn("[1] Review:", action.args["text"])
        self.assertIn("/tmp/minion/review.md", action.args["text"])

    def test_minion_terminal_notification_preserves_summary_line_breaks(self) -> None:
        event = EventEnvelope(
            event_kind=EventKind.MINION_TERMINAL,
            source_kind=SourceKind.MINION,
            payload={
                "status": "completed",
                "run_id": "r1",
                "work_order_id": "wo1",
                "minion_profile": "generic",
                "summary": "## Report\nMilestone: read docs\nResult: done\n\n- Item A\n- Item B",
                "artifacts": [
                    {
                        "title": "Report",
                        "path": "/tmp/minion/report.md",
                        "relative_path": "report.md",
                        "role": "primary",
                    }
                ],
                "route": {
                    "endpoint_id": "telegram_main",
                    "channel_kind": "telegram",
                    "reply_target": {"chat_id": "42"},
                },
            },
        )

        derived = MinionControlEventHandler().handle(event, context=None)

        self.assertEqual(len(derived), 1)
        text = derived[0].payload.args["text"]
        self.assertIn("[1] Report:", text)
        self.assertIn("Summary:\n## Report\nMilestone: read docs\nResult: done\n\n- Item A\n- Item B", text)
        self.assertNotIn("Summary: ## Report Milestone", text)

    def test_minion_terminal_notification_survives_telegram_markdown_rendering(self) -> None:
        from pal.channel.endpoints.telegram_endpoint import _telegram_markdown

        event = EventEnvelope(
            event_kind=EventKind.MINION_TERMINAL,
            source_kind=SourceKind.MINION,
            payload={
                "status": "completed",
                "run_id": "r1",
                "work_order_id": "wo1",
                "minion_profile": "generic",
                "summary": "## Report\nMilestone: read docs\nResult: done",
                "artifacts": [
                    {
                        "title": "Report",
                        "path": "/tmp/minion/report.md",
                        "relative_path": "report.md",
                        "role": "primary",
                    }
                ],
                "route": {
                    "endpoint_id": "telegram_main",
                    "channel_kind": "telegram",
                    "reply_target": {"chat_id": "42"},
                },
            },
        )

        derived = MinionControlEventHandler().handle(event, context=None)
        rendered, _mode = _telegram_markdown(derived[0].payload.args["text"])

        self.assertIn("\n\nSummary:", rendered)
        self.assertNotIn("\n  Summary:", rendered)

    def test_minion_terminal_event_updates_observation_prompt_context(self) -> None:
        class Provider:
            def __init__(self) -> None:
                self.observations: list[dict] = []

            def record_minion_observation(self, payload):
                self.observations.append(dict(payload))

            def recent_minion_observations(self, *, limit: int = 5):
                _ = limit
                return [
                    {
                        "run_id": "r1",
                        "work_order_id": "wo1",
                        "profile": "planner",
                        "status": "completed",
                        "completed_at": "2026-01-01T00:00:00Z",
                        "summary": "done",
                        "artifacts": [{"path": "/tmp/minion/plan.md", "relative_path": "plan.md"}],
                    }
                ]

        provider = Provider()
        event = EventEnvelope(
            event_kind=EventKind.MINION_TERMINAL,
            source_kind=SourceKind.MINION,
            payload={
                "status": "completed",
                "summary": "done",
                "minion_id": "m1",
                "run_id": "r1",
                "work_order_id": "wo1",
                "minion_profile": "planner",
                "artifacts": [{"path": "/tmp/minion/plan.md", "relative_path": "plan.md"}],
                "route": {
                    "endpoint_id": "telegram_main",
                    "channel_kind": "telegram",
                    "reply_target": {"chat_id": "42"},
                },
            },
        )

        derived = MinionControlEventHandler(provider=provider).handle(event, context=None)
        self.assertEqual(len(derived), 1)
        self.assertEqual(provider.observations[0]["work_order_id"], "wo1")

        fragments = TaskingPromptFragmentProvider(manager=provider).build_prompt_fragments(
            PromptAssemblyContext(turn_kind="channel")
        )
        self.assertEqual(fragments[0].title, "Recent Minion Completions")
        self.assertIn("work_order=wo1", fragments[0].content)
        self.assertIn("/tmp/minion/plan.md", fragments[0].content)

    def test_minion_terminal_lessons_open_approval_and_hide_from_final_summary(self) -> None:
        event = EventEnvelope(
            event_kind=EventKind.MINION_TERMINAL,
            source_kind=SourceKind.MINION,
            payload={
                "status": "completed",
                "summary": "Done.\n\nTask Lesson\nKeep final replies clean.",
                "task_lessons": ["Keep final replies clean."],
                "system_lessons": ["Ask before absorbing lessons."],
                "minion_id": "m1",
                "run_id": "r1",
                "work_order_id": "wo1",
                "minion_profile": "generic",
                "route": {
                    "endpoint_id": "telegram_main",
                    "channel_kind": "telegram",
                    "reply_target": {"chat_id": "42"},
                },
            },
        )

        derived = MinionControlEventHandler().handle(event, context=None)

        self.assertEqual(len(derived), 2)
        finished = derived[0].payload
        approval = derived[1].payload
        self.assertEqual(finished.action_kind, "route_reply")
        self.assertNotIn("Task Lesson", finished.args["text"])
        self.assertEqual(approval.action_kind, "interactive_open")
        self.assertEqual(approval.args["interaction"]["interaction_kind"], "minion_lesson_approval")
        buttons = approval.args["interaction"]["buttons"][0]
        self.assertEqual([button["label"] for button in buttons], ["Accept", "Reject", "Edit"])
        self.assertEqual(buttons[0]["action_args"]["action_kind"], "minion_lesson_decision")

    def test_control_plane_turns_minion_approval_button_into_decision_action(self) -> None:
        route = ControlRoute(endpoint_id="telegram_main", channel_kind="telegram", reply_target={"chat_id": "42"})
        action = ControlPlane().handle_interaction(
            InteractionResult(
                interaction_id="ap1",
                interaction_kind="approval_request",
                action_key="control.action.dispatch",
                action_args={
                    "action_kind": "minion_approval_decision",
                    "target_scope": "minion",
                    "target_id": "ap1",
                    "args": {"approval_id": "ap1", "run_id": "r1", "minion_id": "m1", "decision": "accept"},
                },
                route=route,
            )
        )

        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.action_kind, "minion_approval_decision")
        self.assertEqual(action.args["decision"], "accept")
        self.assertEqual(action.args["approval_id"], "ap1")

    def test_minion_lesson_accept_button_absorbs_lessons_after_approval(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_lesson_accept_") as tmp:
            repository = MinionTaskingRepository(runtime_root=Path(tmp))
            repository.prepare_pack_for_spawn(
                TaskContextPack(
                    work_order_id="wo_lesson_accept",
                    goal="learn",
                    metadata={"task_id": "task_lesson_accept", "milestones": ["Done"]},
                )
            )
            provider = MinionManagerProvider(runtime_root=Path(tmp))

            message = asyncio.run(
                provider.handle_control_action_async(
                    ControlAction(
                        action_kind="minion_lesson_decision",
                        target_scope="minion",
                        target_id="wo_lesson_accept",
                        args={
                            "decision": "accept",
                            "work_order_id": "wo_lesson_accept",
                            "task_lessons": ["Keep final replies clean."],
                            "system_lessons": ["Ask before absorbing lessons."],
                            "minion_id": "m1",
                            "run_id": "r1",
                        },
                    )
                )
            )

            snapshot = repository.read_work_order("wo_lesson_accept")
            self.assertIn("accepted", message)
            self.assertEqual(snapshot["task_lessons"][0]["lesson_text"], "Keep final replies clean.")
            self.assertEqual(snapshot["pending_system_lesson_candidates"], [])

    def test_minion_lesson_reject_button_does_not_absorb_lessons(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_lesson_reject_") as tmp:
            repository = MinionTaskingRepository(runtime_root=Path(tmp))
            repository.prepare_pack_for_spawn(
                TaskContextPack(
                    work_order_id="wo_lesson_reject",
                    goal="learn",
                    metadata={"task_id": "task_lesson_reject", "milestones": ["Done"]},
                )
            )
            provider = MinionManagerProvider(runtime_root=Path(tmp))

            message = asyncio.run(
                provider.handle_control_action_async(
                    ControlAction(
                        action_kind="minion_lesson_decision",
                        target_scope="minion",
                        target_id="wo_lesson_reject",
                        args={
                            "decision": "reject",
                            "work_order_id": "wo_lesson_reject",
                            "task_lessons": ["Do not save me."],
                        },
                    )
                )
            )

            snapshot = repository.read_work_order("wo_lesson_reject")
            self.assertIn("discarded", message)
            self.assertEqual(snapshot["task_lessons"], [])
