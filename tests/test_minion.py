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
from pal.control.interactions import build_minion_question_interaction
from pal.core import MemoryCompactEffect, PalCore
from pal.channel.contracts import ChannelEnvelope, EndpointConfig, ResponseHandle
from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from pal.execution import CapabilityCall, CapabilityDescriptor, CapabilityResult, register_with_core as register_execution_with_core
from pal.foundation import BoundedTTLBuffer, PalV2Database
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
    AskUserQuestion,
    CoderWorkOrder,
    MilestoneReport,
    MinionControlEventHandler,
    MinionEventSource,
    MinionManager,
    MinionManagerClient,
    MinionManagerProvider,
    MinionProfile,
    MinionProfileRegistry,
    MinionTaskingRepository,
    PlanArtifact,
    PromptView,
    ReviewReport,
    ReviewerWorkOrder,
    build_planner_work_order,
    compile_coder_work_order,
    validate_final_plan_artifact,
    prompt_view_for_coder,
    prompt_view_for_reviewer,
    prompt_view_from_metadata,
    register_with_core as register_minion_with_core,
)
from pal.llm.contracts import CanonicalLLMOutcome, CanonicalToolCall, CanonicalToolResult, LLMPreflightAdvice
from pal.minion.git_env import _git, commit_milestone, finalize_work_order_branch, prepare_git_task_environment, prepare_task_workspace
from pal.minion.ipc import minion_runner_log_path, open_manager_connection, python_subprocess_env
from pal.minion.manager import MinionRunState
from pal.minion.runner import (
    MinionAgentLoopState,
    MinionRunner,
    MinionRuntimeBundle,
    MinionScopedExecutionRuntime,
    _llm_tools_for_allowed,
    _minion_llm_request_metadata,
    _prompt_view_from_pack,
    _render_task_prompt,
    _render_system_prompt,
    _resolve_minion_max_output_tokens,
    build_slim_minion_runtime,
)
from pal.minion.introspection import _control_route_payload_for_turn, _prompt_log_enabled_for_turn
from pal.minion.prompt import TaskingPromptFragmentProvider
from pal.memory import MemoryCompactRequest, MemoryService
from pal.foundation import EventEnvelope
from pal.shared import EventKind, LLMFinishReason, LLMPreflightStatus, MinionApprovalDecision, PromptAssemblyContext, RuntimeStatus, SourceKind, TaskContextPack


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return str(content or "")


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
    def test_minion_approval_decision_accepts_accept_all(self) -> None:
        decision = MinionApprovalDecision.from_dict(
            {"approval_id": "ap1", "decision": "accept_all", "run_id": "r1", "minion_id": "m1"}
        )

        self.assertEqual(decision.decision, "accept_all")
        self.assertEqual(decision.to_dict()["decision"], "accept_all")

    def test_task_context_pack_json_roundtrip_uses_allowed_capabilities(self) -> None:
        pack = TaskContextPack.from_dict(
            {
                "work_order_id": "wo_1",
                "goal": "inspect repo",
                "acceptance_criteria": ["report findings"],
                "workspace": {"root": "/tmp/repo"},
                "allowed_capabilities": ["tool_read"],
                "approval_policy": {"high_risk_capabilities": ["shell_exec"]},
                "minion_profile": "software_engineering.coder",
                "resolved_profile": {"profile_id": "coder", "display_name": "Coder Minion"},
            }
        )

        restored = TaskContextPack.from_json(pack.to_json())

        self.assertEqual(restored.work_order_id, "wo_1")
        self.assertEqual(restored.instruction, "inspect repo")
        self.assertEqual(restored.allowed_capabilities, ["tool_read"])
        self.assertNotIn("allowed_tools", restored.to_dict())
        self.assertEqual(restored.approval_policy["high_risk_capabilities"], ["shell_exec"])
        self.assertEqual(restored.minion_profile, "software_engineering.coder")
        self.assertEqual(restored.resolved_profile["profile_id"], "coder")

    def test_legacy_task_context_pack_defaults_to_generic_profile(self) -> None:
        restored = TaskContextPack.from_dict({"work_order_id": "wo_legacy", "goal": "old payload"})

        self.assertEqual(restored.minion_profile, "generic")
        self.assertEqual(restored.resolved_profile, {})

    def test_task_context_pack_ignores_removed_allowed_tools_field(self) -> None:
        restored = TaskContextPack.from_dict({"work_order_id": "wo_caps", "allowed_tools": ["op_exec_shell"]})

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

    def test_structured_plan_compiles_to_module_scoped_prompt_view(self) -> None:
        plan = PlanArtifact.from_dict(
            {
                "plan_id": "plan_1",
                "task_id": "task_1",
                "summary": "Split by module.",
                "modules": [
                    {
                        "module_id": "module_a",
                        "owned_area": ["src/a.py"],
                        "responsibility": "Implement A.",
                        "provided_interfaces": [{"name": "AResult", "contract": "A provides result"}],
                        "consumed_interfaces": [{"name": "BInput", "contract": "A consumes B contract only"}],
                        "metadata": {"skill_refs": ["contract_testing"]},
                        "internal_milestones": [
                            {
                                "milestone_id": "a1",
                                "title": "Implement A model",
                                "task": "Add A model only.",
                                "acceptance_criteria": ["A model roundtrips"],
                                "suggested_skills": ["python"],
                                "test_plan": {"unit": ["test A"]},
                            }
                        ],
                    },
                    {
                        "module_id": "module_b",
                        "owned_area": ["src/b.py"],
                        "responsibility": "Implement B.",
                        "internal_milestones": [
                            {
                                "milestone_id": "b1",
                                "title": "Secret B implementation plan",
                                "task": "Do not leak this to A.",
                            }
                        ],
                    },
                ],
                "cross_module_contracts": [
                    {"producer": "module_a", "consumer": "module_b", "interface": "AResult"},
                ],
            }
        )

        order = compile_coder_work_order(plan, module_id="module_a", milestone_id="a1", work_order_id="wo_a")
        view = prompt_view_for_coder(order).to_dict()
        rendered = json.dumps(view, sort_keys=True)

        self.assertEqual(order.module_id, "module_a")
        self.assertEqual(order.milestone_id, "a1")
        self.assertEqual(view["module"]["owned_area"], ["src/a.py"])
        self.assertEqual(order.skill_refs, ["python", "contract_testing"])
        self.assertEqual(view["skill_refs"], ["python", "contract_testing"])
        self.assertIn("A model roundtrips", rendered)
        self.assertIn("AResult", rendered)
        self.assertNotIn("Secret B implementation plan", rendered)
        self.assertNotIn("suggested_skills", rendered)

    def test_final_plan_artifact_validation_and_milestone_index(self) -> None:
        artifact = validate_final_plan_artifact(
            {
                "type": "FinalPlanArtifact",
                "plan_id": "plan_valid",
                "task_id": "task_valid",
                "modules": [
                    {
                        "module_id": "module_valid",
                        "internal_milestones": [
                            {"milestone_id": "m1", "title": "First", "task": "Do first."},
                            {"milestone_id": "m2", "title": "Second", "task": "Do second."},
                        ],
                    }
                ],
            }
        )

        order = compile_coder_work_order(artifact, module_id="module_valid", milestone_id="m2", work_order_id="wo_valid")
        view = prompt_view_for_coder(order).to_dict()

        self.assertEqual(view["milestone"]["milestone_id"], "m2")
        self.assertEqual(view["milestone"]["milestone_index"], 1)
        with self.assertRaises(ValueError):
            validate_final_plan_artifact({"type": "PlanDraft", "task_id": "task_valid", "modules": []})

    def test_prompt_view_roundtrip_and_task_prompt_avoid_raw_pack_dump(self) -> None:
        view = PromptView(
            role="coder",
            task_id="task_prompt",
            work_order_id="wo_prompt",
            module={"module_id": "module_prompt", "owned_area": ["src/pal/minion/work_order.py"]},
            milestone={"milestone_id": "m1", "title": "Scoped milestone", "task": "Do only this."},
            workspace={"repo_path": "/tmp/repo"},
        ).to_dict()
        pack = TaskContextPack(
            work_order_id="wo_prompt",
            goal="full goal should not be the primary prompt",
            workspace={"repo_path": "/tmp/repo", "run_dir": "/tmp/run"},
            continuity={"recent_ledger": [{"payload": "large raw continuity"}]},
            metadata={"prompt_view": view},
        )

        rendered = _render_task_prompt(pack)
        parsed = json.loads(rendered)

        self.assertEqual(_prompt_view_from_pack(pack)["module"]["module_id"], "module_prompt")
        self.assertEqual(parsed["prompt_view"]["milestone"]["title"], "Scoped milestone")
        self.assertEqual(parsed["prompt_view"]["workspace"], {"repo_path": "/tmp/repo"})
        self.assertNotIn("large raw continuity", rendered)
        self.assertNotIn("run_dir", rendered)

    def test_prompt_view_workspace_prefers_prepared_workspace(self) -> None:
        pack = TaskContextPack(
            work_order_id="wo_workspace_prompt",
            goal="use prepared workspace",
            workspace={
                "repo_path": "/tmp/prepared-repo",
                "source_repo": "/tmp/source-repo",
                "artifact_dir": "/tmp/prepared-repo/minion_outputs/wo_workspace_prompt",
            },
            allowed_capabilities=["op_file_read", "op_file_write"],
            metadata={
                "prompt_view": {
                    "role": "coder",
                    "work_order_id": "wo_workspace_prompt",
                    "allowed_capabilities": [],
                    "workspace": {"source_repo": "/tmp/source-repo"},
                }
            },
        )

        parsed = json.loads(_render_task_prompt(pack))

        self.assertEqual(parsed["prompt_view"]["workspace"]["repo_path"], "/tmp/prepared-repo")
        self.assertEqual(parsed["prompt_view"]["workspace"]["source_repo"], "/tmp/source-repo")
        self.assertEqual(
            parsed["prompt_view"]["workspace"]["artifact_dir"],
            "/tmp/prepared-repo/minion_outputs/wo_workspace_prompt",
        )
        self.assertEqual(parsed["prompt_view"]["allowed_capabilities"], ["op_file_read", "op_file_write"])

    def test_planner_task_prompt_ignores_raw_pack_dump_when_work_order_is_structured(self) -> None:
        planner = build_planner_work_order(
            goal="Plan a compact minion work-order flow",
            task_id="task_planner_compact",
            work_order_id="wo_planner_compact",
        )
        pack = TaskContextPack(
            work_order_id="wo_planner_compact",
            goal="Plan a compact minion work-order flow",
            metadata={
                "planner_work_order": planner,
                "payload_json": "raw payload should not leak" * 1000,
                "full_context": "full context should not leak" * 1000,
            },
            continuity={"recent_ledger": [{"payload": "old run details should not leak" * 1000}]},
            memory_pack={"raw_memory": "raw memory should not leak" * 1000},
        )

        rendered = _render_task_prompt(pack)
        parsed = json.loads(rendered)

        self.assertEqual(parsed["prompt_view"]["role"], "planner")
        self.assertIn("planning_requirements", parsed["prompt_view"])
        self.assertNotIn("payload_json", rendered)
        self.assertNotIn("full_context", rendered)
        self.assertNotIn("recent_ledger", rendered)
        self.assertNotIn("memory_pack", rendered)
        self.assertLess(len(rendered), 5000)

    def test_planner_work_order_and_question_models_roundtrip(self) -> None:
        planner = build_planner_work_order(goal="Design minion work orders", task_id="task_plan", work_order_id="wo_plan")
        question = AskUserQuestion.from_dict(
            {
                "task_id": "task_plan",
                "work_order_id": "wo_plan",
                "turn_index": 1,
                "plan_revision": 1,
                "questions": [
                    {
                        "question_id": "q1",
                        "blocking": True,
                        "question": "Which tradeoff should be preferred?",
                        "why_needed": "The choice changes module boundaries.",
                        "evidence": [{"file": "src/pal/minion/runner.py", "finding": "Runner accepts TaskContextPack."}],
                    }
                ],
            }
        )

        self.assertEqual(planner["planning_requirements"]["ask_user_policy"]["max_questions_per_turn"], 3)
        self.assertTrue(planner["planning_requirements"]["ask_user_policy"]["do_not_ask_if_repo_discoverable"])
        self.assertEqual(question.to_dict()["type"], "ask_user_question")
        self.assertEqual(question.to_dict()["questions"][0]["why_needed"], "The choice changes module boundaries.")

    def test_milestone_report_roundtrip_preserves_commit_sha(self) -> None:
        report = MilestoneReport.from_dict(
            {
                "report_id": "mr_1",
                "task_id": "task_1",
                "work_order_id": "wo_1",
                "module_id": "module_a",
                "milestone_id": "a1",
                "status": "done",
                "summary": "Implemented A.",
                "changed_files": ["src/a.py"],
                "test_evidence": [{"command": "pytest tests/test_a.py", "status": "passed"}],
                "commit_sha": "abc123",
            }
        )

        restored = MilestoneReport.from_dict(report.to_dict())

        self.assertEqual(restored.commit_sha, "abc123")
        self.assertEqual(restored.changed_files, ["src/a.py"])
        self.assertEqual(restored.test_evidence[0]["status"], "passed")

    def test_reviewer_work_order_and_report_roundtrip(self) -> None:
        order = ReviewerWorkOrder.from_dict(
            {
                "work_order_id": "wo_review",
                "task_id": "task_review",
                "review_target": {"commit_sha": "abc123", "module_id": "module_a"},
                "relevant_contracts": [{"name": "AResult", "contract": "A provides result"}],
                "acceptance_criteria": ["No regression"],
                "test_evidence": [{"command": "pytest tests/test_a.py", "status": "passed"}],
                "skill_refs": ["code_review"],
            }
        )
        view = prompt_view_for_reviewer(order)
        report = ReviewReport.from_dict(
            {
                "report_id": "rr_1",
                "task_id": "task_review",
                "work_order_id": "wo_review",
                "status": "approved",
                "summary": "No issues found.",
                "findings": [],
            }
        )

        self.assertEqual(view["role"], "reviewer")
        self.assertEqual(view["review_target"]["commit_sha"], "abc123")
        self.assertEqual(view["skill_refs"], ["code_review"])
        self.assertEqual(order.to_dict()["skill_refs"], ["code_review"])
        self.assertEqual(view["acceptance_criteria"], ["No regression"])
        self.assertEqual(prompt_view_from_metadata({"prompt_view": view})["review_target"]["commit_sha"], "abc123")
        self.assertEqual(report.to_dict()["status"], "approved")

    def test_runner_marks_ask_user_question_as_blocked_report(self) -> None:
        async def write_event(_event):
            return None

        async def read_decision(_timeout):
            return None

        runner = MinionRunner(
            runtime_root=Path(tempfile.mkdtemp(prefix="pal_minion_question_test_")),
            pack=TaskContextPack(work_order_id="wo_question", goal="plan"),
            minion_id="m1",
            run_id="r1",
            write_event=write_event,
            read_decision=read_decision,
        )
        try:
            payload = asyncio.run(
                runner._complete_current_milestone(
                    json.dumps(
                        {
                            "type": "ask_user_question",
                            "work_order_id": "wo_question",
                            "questions": [
                                {
                                    "question_id": "q1",
                                    "question": "Prefer module A or module B first?",
                                    "why_needed": "The answer changes sequencing.",
                                }
                            ],
                        }
                    )
                )
            )
        finally:
            shutil.rmtree(runner.runtime_root, ignore_errors=True)

        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["ask_user_question"]["questions"][0]["question_id"], "q1")
        self.assertIn("Prefer module A", payload["summary"])

    def test_runner_waits_for_planner_clarification_and_continues_same_run(self) -> None:
        async def scenario() -> None:
            events = []
            planner_work_order = build_planner_work_order(
                goal="Plan module work",
                task_id="task_planner_wait",
                work_order_id="wo_planner_wait",
            )

            class FakeLLM:
                def __init__(self) -> None:
                    self.calls = 0

                async def agenerate(self, request):
                    self.calls += 1
                    if self.calls == 1:
                        return CanonicalLLMOutcome(
                            text=json.dumps(
                                {
                                    "type": "ask_user_question",
                                    "work_order_id": "wo_planner_wait",
                                    "questions": [
                                        {
                                            "question_id": "q1",
                                            "question": "Prefer module A or module B first?",
                                            "why_needed": "The answer changes sequencing.",
                                            "options": [
                                                {"id": "module_a", "label": "Module A"},
                                                {"id": "module_b", "label": "Module B"},
                                            ],
                                        }
                                    ],
                                }
                            )
                        )
                    _ = request
                    return CanonicalLLMOutcome(text="final plan ready")

            class FakeExecution:
                def get_capability_spec(self, name):
                    _ = name
                    return None

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                clarification = next(event for event in events if event["event_kind"] == "clarification_requested")
                return {
                    "type": "clarification",
                    "clarification": {
                        "clarification_id": clarification["payload"]["clarification_id"],
                        "run_id": "r1",
                        "work_order_id": "wo_planner_wait",
                        "answers": [
                            {
                                "question_id": "q1",
                                "selected_option_id": "module_a",
                                "answer": "Module A",
                            }
                        ],
                    },
                }

            code = await MinionRunner(
                runtime_root=Path(tempfile.mkdtemp(prefix="pal_minion_planner_question_")),
                pack=TaskContextPack(
                    work_order_id="wo_planner_wait",
                    goal="Plan module work",
                    minion_profile="software_engineering.planner",
                    metadata={"task_id": "task_planner_wait", "planner_work_order": planner_work_order},
                ),
                minion_id="m1",
                run_id="r1",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=FakeLLM(), execution_runtime=FakeExecution()),
            ).run()

            self.assertEqual(code, 0)
            kinds = [event["event_kind"] for event in events]
            self.assertIn("clarification_requested", kinds)
            self.assertIn("clarification_received", kinds)
            terminal = next(event for event in events if event["event_kind"] == "terminal")
            self.assertEqual(terminal["payload"]["status"], "completed")
            self.assertNotIn("ask_user_question", terminal["payload"])

        asyncio.run(scenario())

    def test_runner_blocks_non_interactive_planner_question_without_waiting(self) -> None:
        async def scenario() -> None:
            events = []

            class FakeLLM:
                async def agenerate(self, request):
                    _ = request
                    return CanonicalLLMOutcome(
                        text=json.dumps(
                            {
                                "type": "ask_user_question",
                                "work_order_id": "wo_no_options",
                                "questions": [
                                    {
                                        "question_id": "q1",
                                        "question": "What naming style should be used?",
                                    }
                                ],
                            }
                        )
                    )

            class FakeExecution:
                def get_capability_spec(self, name):
                    _ = name
                    return None

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                raise AssertionError("runner should not wait on a non-interactive clarification")

            code = await MinionRunner(
                runtime_root=Path(tempfile.mkdtemp(prefix="pal_minion_planner_no_options_")),
                pack=TaskContextPack(
                    work_order_id="wo_no_options",
                    goal="Plan module work",
                    minion_profile="software_engineering.planner",
                ),
                minion_id="m1",
                run_id="r1",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=FakeLLM(), execution_runtime=FakeExecution()),
            ).run()

            self.assertEqual(code, 0)
            kinds = [event["event_kind"] for event in events]
            self.assertIn("clarification_unavailable", kinds)
            self.assertNotIn("clarification_requested", kinds)
            terminal = next(event for event in events if event["event_kind"] == "terminal")
            self.assertEqual(terminal["payload"]["status"], "blocked")

        asyncio.run(scenario())

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
                CanonicalToolCall(name="op_tool_call", args={"name": "op_meta_probe"}),
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
            pack = registry.resolve_pack(TaskContextPack(work_order_id="wo_profile", goal="research"), requested_profile="software_engineering.planner")

            self.assertIn("op_memory_recall", pack.allowed_capabilities)
            self.assertIn("op_workspace_tree", pack.allowed_capabilities)
            self.assertIn("op_workspace_search", pack.allowed_capabilities)
            self.assertIn("op_workspace_read", pack.allowed_capabilities)
            self.assertIn("op_web_search", pack.allowed_capabilities)
            self.assertIn("op_web_read", pack.allowed_capabilities)
            self.assertFalse(any(name.startswith("intro_") for name in pack.allowed_capabilities))
            self.assertEqual(
                [name for name in pack.allowed_capabilities if name.startswith("op_minion_")],
                ["op_minion_artifact_write", "op_minion_artifact_edit", "op_minion_memory_candidate_write"],
            )
            self.assertEqual(pack.workspace["workspace_policy"]["mode"], "read_only_repo")
            self.assertEqual(pack.workspace["completion_policy"]["evidence"], "text_deliverable")
            self.assertEqual(pack.minion_profile, "software_engineering.planner")
            self.assertEqual(pack.resolved_profile["canonical_profile_id"], "software_engineering.planner")
            self.assertEqual(pack.resolved_profile["profile_group"], "software_engineering")
            self.assertEqual(pack.allowed_skills, ["software_planning"])
            self.assertEqual(pack.resolved_profile["skill_refs"], ["software_planning"])
            self.assertEqual(pack.resolved_profile["effective_skill_refs"], ["software_planning"])
            self.assertNotIn("default_allowed_skills", pack.resolved_profile)

    def test_profile_skill_refs_accept_legacy_skill_fields(self) -> None:
        profile = MinionProfile.from_dict(
            {
                "profile_id": "legacy",
                "display_name": "Legacy",
                "identity_fragment": "Legacy profile.",
                "default_allowed_skills": ["legacy_skill"],
            }
        )
        registry = MinionProfileRegistry(builtin_profiles=(profile,))

        pack = registry.resolve_pack(TaskContextPack(work_order_id="wo_legacy_skill", goal="use legacy"), requested_profile="legacy")

        self.assertEqual(profile.skill_refs, ("legacy_skill",))
        self.assertEqual(profile.to_dict()["skill_refs"], ["legacy_skill"])
        self.assertNotIn("default_allowed_skills", profile.to_dict())
        self.assertEqual(pack.allowed_skills, ["legacy_skill"])

    def test_profile_resolution_filters_minion_denied_capabilities(self) -> None:
        registry = MinionProfileRegistry()
        pack = registry.resolve_pack(
            TaskContextPack(
                work_order_id="wo_policy",
                goal="do work",
                allowed_capabilities=[
                    "intro_minion_task_read",
                    "intro_minion_work_order_read",
                    "op_memory_write",
                    "op_memory_update",
                    "op_memory_delete",
                    "op_behavior_save",
                    "op_skill_commit",
                    "op_channel_send_attachment",
                    "op_minion_spawn",
                    "op_minion_kill",
                    "op_exec_shell",
                    "op_memory_recall",
                    "op_web_search",
                    "op_fake_extra",
                ],
            ),
            requested_profile="software_engineering.coder",
        )

        self.assertEqual(pack.allowed_capabilities, ["op_exec_shell", "op_memory_recall", "op_web_search", "op_fake_extra"])
        self.assertIn("effective_capability_policy", pack.resolved_profile)

    def test_profile_resolution_can_inherit_current_capability_surface(self) -> None:
        registry = MinionProfileRegistry(
            ambient_capabilities=(
                "op_tool_search",
                "op_tool_read",
                "op_tool_call",
                "op_exec_shell",
                "op_memory_recall",
                "op_web_search",
                "op_web_read",
                "intro_minion_task_read",
                "op_minion_spawn",
                "op_memory_write",
                "op_memory_update",
                "op_memory_delete",
            )
        )
        pack = registry.resolve_pack(TaskContextPack(work_order_id="wo_inherit", goal="inherit"), requested_profile="software_engineering.planner")

        self.assertIn("op_tool_search", pack.allowed_capabilities)
        self.assertIn("op_tool_read", pack.allowed_capabilities)
        self.assertIn("op_tool_call", pack.allowed_capabilities)
        self.assertNotIn("op_exec_shell", pack.allowed_capabilities)
        self.assertIn("op_workspace_read", pack.allowed_capabilities)
        self.assertIn("op_memory_recall", pack.allowed_capabilities)
        self.assertIn("op_web_search", pack.allowed_capabilities)
        self.assertIn("op_web_read", pack.allowed_capabilities)
        self.assertNotIn("intro_minion_task_read", pack.allowed_capabilities)
        self.assertNotIn("op_minion_spawn", pack.allowed_capabilities)
        self.assertNotIn("op_memory_write", pack.allowed_capabilities)
        self.assertNotIn("op_memory_update", pack.allowed_capabilities)
        self.assertNotIn("op_memory_delete", pack.allowed_capabilities)

    def test_runtime_profiles_load_recursively_with_scoped_profile_name(self) -> None:
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
            profile = registry.get("runtime_software.planner")

            self.assertIsNotNone(profile)
            assert profile is not None
            self.assertEqual(profile.display_name, "Runtime Planner")
            self.assertEqual(profile.profile_group, "runtime_software")
            self.assertEqual(profile.canonical_profile_id, "runtime_software.planner")
            self.assertEqual(profile.workspace_policy["mode"], "read_only_repo")
            self.assertIsNone(registry.get("planner"))

    def test_runtime_profile_file_overrides_builtin_profile(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_profile_registry_test_") as tmp:
            profile_dir = Path(tmp) / "plugins" / "minion" / "profiles" / "software_engineering"
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

            profile = MinionProfileRegistry(runtime_root=Path(tmp)).get("software_engineering.planner")

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
        self.assertEqual(snapshot["work_order"]["status"], "active")
        self.assertEqual(snapshot["current_milestone"]["milestone_index"], 1)
        self.assertEqual(snapshot["work_order"]["metadata"]["artifacts"][0]["relative_path"], "design.md")
        self.assertEqual(snapshot["work_order"]["metadata"]["primary_artifact"]["relative_path"], "design.md")
        self.assertEqual(snapshot["task_lessons"], [])
        self.assertEqual(snapshot["pending_system_lesson_candidates"], [])

        self.repository.record_minion_event(
            {
                "event_kind": "checkpoint",
                "work_order_id": "wo_cursor",
                "minion_id": "m1",
                "run_id": "r2",
                "payload": {"status": "completed", "milestone_index": 1, "summary": "implement done"},
            }
        )
        self.repository.record_minion_event(
            {
                "event_kind": "terminal",
                "work_order_id": "wo_cursor",
                "minion_id": "m1",
                "run_id": "r2",
                "payload": {"status": "completed", "summary": "all done"},
            }
        )
        snapshot = self.repository.read_work_order("wo_cursor")
        self.assertEqual(snapshot["work_order"]["status"], "completed")

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

    def test_empty_tasking_search_lists_recent_records(self) -> None:
        self.repository.prepare_pack_for_spawn(
            TaskContextPack(
                work_order_id="wo_recent",
                goal="summarize recent minion state",
                metadata={"task_id": "task_recent", "task_title": "Recent minion state"},
            )
        )
        draft = self.repository.create_work_order_draft(
            {
                "title": "Recent draft",
                "goal": "List recent work order drafts",
                "task_id": "task_recent_draft",
            }
        )

        task_ids = {item["task"]["task_id"] for item in self.repository.search_tasks("", limit=10)["items"]}
        work_order_ids = {item["work_order_id"] for item in self.repository.search_work_orders("", limit=10)["items"]}
        draft_ids = {item["draft_id"] for item in self.repository.search_work_order_drafts("", limit=10)["items"]}

        self.assertIn("task_recent", task_ids)
        self.assertIn("wo_recent", work_order_ids)
        self.assertIn(draft["draft"]["draft_id"], draft_ids)

    def test_prepare_pack_for_spawn_compiles_metadata_work_order_to_prompt_view(self) -> None:
        plan = PlanArtifact.from_dict(
            {
                "plan_id": "plan_repo",
                "task_id": "task_repo",
                "modules": [
                    {
                        "module_id": "module_repo",
                        "owned_area": ["src/pal/minion/work_order.py"],
                        "responsibility": "Define structured work order models.",
                        "internal_milestones": [
                            {
                                "milestone_id": "m1",
                                "title": "Add models",
                                "task": "Add only the work order models.",
                                "acceptance_criteria": ["PromptView is scoped"],
                            }
                        ],
                    },
                    {
                        "module_id": "module_other",
                        "internal_milestones": [{"milestone_id": "m2", "title": "Other module internals"}],
                    },
                ],
            }
        )
        order = compile_coder_work_order(plan, module_id="module_repo", milestone_id="m1", work_order_id="wo_structured")
        prepared = self.repository.prepare_pack_for_spawn(
            TaskContextPack(
                work_order_id="wo_structured",
                goal="implement structured work orders",
                workspace={"repo_path": str(self.root), "run_dir": str(self.root / "run")},
                metadata={"task_id": "task_repo", "coder_work_order": order.to_dict()},
            )
        )
        rendered = _render_task_prompt(prepared)

        self.assertEqual(prepared.metadata["prompt_view"]["module"]["module_id"], "module_repo")
        self.assertEqual(prepared.continuity["current_milestone"]["milestone_index"], 0)
        self.assertIn("Add only the work order models.", rendered)
        self.assertNotIn("Other module internals", rendered)
        self.assertNotIn("run_dir", rendered)

    def test_plan_driven_module_pack_advances_to_next_milestone(self) -> None:
        plan = {
            "type": "FinalPlanArtifact",
            "plan_id": "plan_serial",
            "task_id": "task_serial",
            "summary": "Serial module implementation.",
            "modules": [
                {
                    "module_id": "module_serial",
                    "owned_area": ["src/pal/minion/repository.py"],
                    "internal_milestones": [
                        {"milestone_id": "m1", "title": "First milestone", "task": "Implement first part."},
                        {"milestone_id": "m2", "title": "Second milestone", "task": "Implement second part."},
                    ],
                }
            ],
        }
        pack = self.repository.build_coder_module_pack_from_plan(
            plan,
            module_id="module_serial",
            work_order_id="wo_serial",
            workspace={"repo_path": str(self.root)},
        )
        prepared = self.repository.prepare_pack_for_spawn(pack)

        self.assertEqual(prepared.metadata["prompt_view"]["milestone"]["milestone_id"], "m1")
        self.assertEqual(prepared.metadata["prompt_view"]["milestone"]["milestone_index"], 0)
        self.repository.record_minion_event(
            {
                "event_kind": "checkpoint",
                "work_order_id": "wo_serial",
                "minion_id": "minion_1",
                "run_id": "run_1",
                "payload": {"status": "completed", "milestone_index": 0, "summary": "m1 done"},
            }
        )
        self.repository.record_minion_event(
            {
                "event_kind": "terminal",
                "work_order_id": "wo_serial",
                "minion_id": "minion_1",
                "run_id": "run_1",
                "payload": {
                    "status": "completed",
                    "summary": "terminal",
                    "deferred_experience": {
                        "memory_candidates": [{"kind": "case", "title": "m1 lesson", "summary": "remember m1"}],
                    },
                },
            }
        )

        next_pack = self.repository.next_serial_module_pack("wo_serial")
        self.assertIsNotNone(next_pack)
        assert next_pack is not None
        prepared_next = self.repository.prepare_pack_for_spawn(next_pack)

        self.assertEqual(prepared_next.metadata["prompt_view"]["milestone"]["milestone_id"], "m2")
        self.assertEqual(prepared_next.metadata["prompt_view"]["milestone"]["milestone_index"], 1)
        snapshot = self.repository.read_work_order("wo_serial")
        pending = snapshot["work_order"]["metadata"]["module_execution"]["pending_experience"]
        self.assertEqual(pending["memory_candidates"][0]["title"], "m1 lesson")

    def test_plan_driven_module_completion_reports_deferred_experience(self) -> None:
        plan = {
            "type": "FinalPlanArtifact",
            "plan_id": "plan_done",
            "task_id": "task_done",
            "modules": [
                {
                    "module_id": "module_done",
                    "internal_milestones": [
                        {"milestone_id": "m1", "title": "Only milestone", "task": "Finish it."},
                    ],
                }
            ],
        }
        pack = self.repository.build_coder_module_pack_from_plan(plan, module_id="module_done", work_order_id="wo_done")
        self.repository.prepare_pack_for_spawn(pack)
        self.repository.record_minion_event(
            {
                "event_kind": "checkpoint",
                "work_order_id": "wo_done",
                "payload": {"status": "completed", "milestone_index": 0, "summary": "done"},
            }
        )
        self.repository.record_minion_event(
            {
                "event_kind": "terminal",
                "work_order_id": "wo_done",
                "payload": {
                    "status": "completed",
                    "summary": "done",
                    "deferred_experience": {
                        "task_lessons": ["task lesson"],
                        "system_lessons": ["system lesson"],
                        "memory_candidates": [{"kind": "case", "title": "done", "summary": "done memory"}],
                    },
                },
            }
        )

        completion = self.repository.mark_serial_module_completed("wo_done")

        self.assertEqual(completion["status"], "completed")
        self.assertEqual(completion["module_id"], "module_done")
        self.assertEqual(completion["task_lessons"], ["task lesson"])
        self.assertEqual(completion["system_lessons"], ["system lesson"])
        self.assertEqual(completion["memory_candidates"][0]["title"], "done")
        self.assertEqual(self.repository.mark_serial_module_completed("wo_done")["status"], "already_completed")

    def test_plan_parent_uses_modules_as_parent_milestones(self) -> None:
        plan = {
            "type": "FinalPlanArtifact",
            "plan_id": "plan_parent",
            "task_id": "task_parent",
            "summary": "Parent plan.",
            "modules": [
                {
                    "module_id": "module_a",
                    "responsibility": "Build A.",
                    "internal_milestones": [{"milestone_id": "a1", "title": "A1", "task": "Do A1."}],
                },
                {
                    "module_id": "module_b",
                    "responsibility": "Build B.",
                    "internal_milestones": [{"milestone_id": "b1", "title": "B1", "task": "Do B1."}],
                },
            ],
        }
        parent = self.repository.build_plan_parent_pack_from_plan(plan, work_order_id="wo_parent", workspace={"repo_path": str(self.root)})
        prepared_parent = self.repository.prepare_pack_for_spawn(parent)

        self.assertNotIn("prompt_view", prepared_parent.metadata)
        self.assertEqual(prepared_parent.continuity["current_milestone"]["milestone_index"], 0)
        self.assertEqual(prepared_parent.continuity["current_milestone"]["title"], "module_a")

        child = self.repository.next_plan_module_pack("wo_parent", allow_paused=True)
        self.assertIsNotNone(child)
        assert child is not None
        prepared_child = self.repository.prepare_pack_for_spawn(child)

        self.assertEqual(prepared_child.metadata["parent_work_order_id"], "wo_parent")
        self.assertEqual(prepared_child.metadata["parent_milestone_index"], 0)
        self.assertEqual(prepared_child.metadata["prompt_view"]["module"]["module_id"], "module_a")
        self.assertEqual(prepared_child.metadata["prompt_view"]["milestone"]["milestone_id"], "a1")

    def test_plan_parent_completion_advances_parent_cursor_and_waits(self) -> None:
        plan = {
            "type": "FinalPlanArtifact",
            "plan_id": "plan_parent_cursor",
            "task_id": "task_parent_cursor",
            "modules": [
                {"module_id": "module_a", "internal_milestones": [{"milestone_id": "a1", "title": "A1", "task": "Do A."}]},
                {"module_id": "module_b", "internal_milestones": [{"milestone_id": "b1", "title": "B1", "task": "Do B."}]},
            ],
        }
        parent = self.repository.build_plan_parent_pack_from_plan(plan, work_order_id="wo_parent_cursor")
        self.repository.prepare_pack_for_spawn(parent)
        child = self.repository.next_plan_module_pack("wo_parent_cursor", allow_paused=True)
        assert child is not None
        self.repository.prepare_pack_for_spawn(child)
        self.repository.record_minion_event(
            {
                "event_kind": "checkpoint",
                "work_order_id": child.work_order_id,
                "payload": {"status": "completed", "milestone_index": 0, "summary": "child done"},
            }
        )
        self.repository.record_minion_event(
            {
                "event_kind": "terminal",
                "work_order_id": child.work_order_id,
                "payload": {"status": "completed", "summary": "child terminal"},
            }
        )
        child_completion = self.repository.mark_serial_module_completed(child.work_order_id)

        parent_completion = self.repository.record_plan_module_completion(child.work_order_id, child_completion)
        snapshot = self.repository.read_work_order("wo_parent_cursor")

        self.assertEqual(parent_completion["status"], "awaiting_continue")
        self.assertTrue(parent_completion["has_next_module"])
        self.assertEqual(parent_completion["next_module_id"], "module_b")
        self.assertEqual(snapshot["current_milestone"]["title"], "module_b")
        self.assertEqual(snapshot["work_order"]["metadata"]["plan_execution"]["status"], "awaiting_continue")

    def test_record_clarification_answer_appends_to_work_order_metadata(self) -> None:
        self.repository.prepare_pack_for_spawn(
            TaskContextPack(work_order_id="wo_clarify", goal="clarify", metadata={"task_id": "task_clarify"})
        )

        result = self.repository.record_clarification_answer(
            "wo_clarify",
            {"question_id": "q1", "selected_option_id": "module_first", "answer": "Module first"},
        )
        snapshot = self.repository.read_work_order("wo_clarify")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(snapshot["work_order"]["metadata"]["clarification_answers"][0]["question_id"], "q1")
        self.assertEqual(snapshot["work_order"]["metadata"]["clarification_answers"][0]["selected_option_id"], "module_first")

    def test_minion_question_answer_control_action_records_answer(self) -> None:
        self.repository.prepare_pack_for_spawn(
            TaskContextPack(work_order_id="wo_question_answer", goal="clarify", metadata={"task_id": "task_question_answer"})
        )
        provider = MinionManagerProvider(runtime_root=self.root)

        message = asyncio.run(
            provider.handle_control_action_async(
                ControlAction(
                    action_kind="minion_question_answer",
                    target_scope="minion",
                    target_id="wo_question_answer",
                    args={
                        "work_order_id": "wo_question_answer",
                        "question_id": "q1",
                        "selected_option_id": "module_first",
                        "answer": "Module first",
                    },
                )
            )
        )
        snapshot = self.repository.read_work_order("wo_question_answer")

        self.assertIn("recorded", message)
        self.assertEqual(snapshot["work_order"]["metadata"]["clarification_answers"][0]["answer"], "Module first")

    def test_planner_question_answer_resumes_same_work_order_with_clarification(self) -> None:
        planner_work_order = build_planner_work_order(
            goal="Plan module work",
            task_id="task_planner_resume",
            work_order_id="wo_planner_resume",
        )
        self.repository.prepare_pack_for_spawn(
            TaskContextPack(
                work_order_id="wo_planner_resume",
                goal="Plan module work",
                minion_profile="software_engineering.planner",
                metadata={"task_id": "task_planner_resume", "planner_work_order": planner_work_order},
            )
        )
        self.repository.record_minion_event(
            {
                "event_kind": "terminal",
                "work_order_id": "wo_planner_resume",
                "minion_id": "m1",
                "run_id": "r1",
                "payload": {"status": "blocked", "summary": "question asked"},
            }
        )

        class FakeClient:
            def __init__(self) -> None:
                self.spawned = []

            def spawn_sync(self, payload):
                self.spawned.append(dict(payload))
                return {"status": "running", "run_id": "run_resume"}

        provider = MinionManagerProvider(runtime_root=self.root)
        fake_client = FakeClient()
        provider.client = fake_client
        provider._ensure_manager_started = lambda: None  # type: ignore[method-assign]
        route = ControlRoute(endpoint_id="telegram_main", channel_kind="telegram", reply_target={"chat_id": "42"})

        message = asyncio.run(
            provider.handle_control_action_async(
                ControlAction(
                    action_kind="minion_question_answer",
                    target_scope="minion",
                    target_id="wo_planner_resume",
                    route=route,
                    args={
                        "work_order_id": "wo_planner_resume",
                        "question_id": "q1",
                        "selected_option_id": "module_first",
                        "answer": "Module first",
                        "turn_index": 0,
                        "plan_revision": 0,
                    },
                )
            )
        )

        self.assertIn("planner resumed", message)
        self.assertEqual(len(fake_client.spawned), 1)
        spawned = fake_client.spawned[0]
        planner = spawned["metadata"]["planner_work_order"]
        self.assertEqual(spawned["work_order_id"], "wo_planner_resume")
        self.assertEqual(planner["turn_index"], 1)
        self.assertEqual(planner["plan_revision"], 1)
        self.assertEqual(planner["clarifications"][0]["answer"], "Module first")
        self.assertEqual(spawned["metadata"]["prompt_view"]["clarifications"][0]["selected_option_id"], "module_first")
        self.assertEqual(spawned["metadata"]["control_route"]["reply_target"], {"chat_id": "42"})

    def test_minion_recent_observation_buffer_expires_by_ttl(self) -> None:
        now = [0.0]
        provider = MinionManagerProvider(runtime_root=self.root)
        provider.recent_observations = BoundedTTLBuffer(
            capacity=10,
            ttl_seconds=10,
            clock=lambda: now[0],
        )

        provider.record_minion_observation(
            {
                "status": "completed",
                "summary": "done",
                "run_id": "run_recent",
                "work_order_id": "wo_recent",
                "minion_profile": "software_engineering.reviewer",
            }
        )
        self.assertEqual(provider.recent_minion_observations()[0]["run_id"], "run_recent")

        now[0] = 11.0
        self.assertEqual(provider.recent_minion_observations(), [])

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
        self.assertNotIn("payload_json", read["draft"])
        self.assertNotIn("payload", read["draft"])
        self.assertEqual(read["planner_review"]["minion_profile"], "software_engineering.planner")
        self.assertEqual(missing_work_order["status"], "not_found")

    def test_work_order_draft_snapshot_compacts_raw_payload_metadata(self) -> None:
        huge = "x" * 10000
        draft = self.repository.create_work_order_draft(
            {
                "title": "Compact draft payloads",
                "goal": "Keep draft work-order reads compact",
                "source_summary": huge,
                "metadata": {
                    "raw_payload": huge,
                    "payload_json": huge,
                    "transcript": huge,
                    "safe_note": huge,
                },
            }
        )
        read = self.repository.read_work_order_draft(draft["draft"]["draft_id"])
        rendered = json.dumps(read, ensure_ascii=False, sort_keys=True)

        self.assertNotIn("payload_json", rendered)
        self.assertNotIn("raw_payload", rendered)
        self.assertNotIn("transcript", rendered)
        self.assertLessEqual(len(read["draft"]["source_summary"]), 4000)
        self.assertLessEqual(len(read["work_order_candidate"]["metadata"]["source_summary"]), 4000)
        self.assertLessEqual(len(read["work_order_candidate"]["metadata"]["safe_note"]), 1000)
        self.assertLess(len(rendered), 12000)

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
        self.assertEqual(read_draft["draft"]["proposed_work_order_id"], "wo_minion_ledger")

    def test_pack_for_existing_work_order_preserves_stored_instruction_and_workspace(self) -> None:
        self.repository.prepare_pack_for_spawn(
            TaskContextPack(
                work_order_id="wo_existing_pack",
                goal="existing goal",
                instruction="stored instruction survives spawn by id",
                workspace={"source_repo": "local-source"},
                allowed_capabilities=["op_exec_shell"],
                metadata={"task_id": "task_existing_pack"},
            )
        )

        pack = self.repository.pack_for_work_order("wo_existing_pack")

        self.assertEqual(pack.instruction, "stored instruction survives spawn by id")
        self.assertEqual(pack.workspace["source_repo"], "local-source")
        self.assertEqual(pack.allowed_capabilities, [])

    def test_build_continuity_compacts_ledger_payloads(self) -> None:
        self.repository.prepare_pack_for_spawn(
            TaskContextPack(
                work_order_id="wo_compact_continuity",
                goal="compact continuity",
                metadata={"task_id": "task_compact_continuity", "milestones": ["first"]},
            )
        )
        self.repository.record_minion_event(
            {
                "event_kind": "phase_started",
                "work_order_id": "wo_compact_continuity",
                "minion_id": "m1",
                "run_id": "r1",
                "payload": {
                    "phase": "accepted",
                    "summary": "minion accepted task context",
                    "prompt_scaffold": {
                        "instruction": "x" * 20000,
                        "acceptance_criteria": ["one"],
                        "allowed_capabilities": ["op_workspace_read"],
                        "continuity": {
                            "recent_ledger": [{"payload": "y" * 20000}],
                            "completed_milestones": [{"title": "done"}],
                            "task_lessons": [],
                        },
                    },
                },
            }
        )

        continuity = self.repository.build_continuity("wo_compact_continuity")
        ledger = continuity["recent_ledger"][0]
        snapshot = self.repository.read_work_order("wo_compact_continuity")
        snapshot_ledger = snapshot["recent_ledger"][0]

        self.assertNotIn("payload_json", ledger)
        self.assertNotIn("prompt_scaffold", ledger["payload"])
        self.assertIn("prompt_scaffold_summary", ledger["payload"])
        self.assertNotIn("payload_json", snapshot_ledger)
        self.assertNotIn("prompt_scaffold", snapshot_ledger["payload"])
        self.assertIn("prompt_scaffold_summary", snapshot_ledger["payload"])
        self.assertNotIn("metadata_json", snapshot["work_order"])
        self.assertLess(len(json.dumps(continuity, ensure_ascii=False)), 4000)

    def test_work_order_workspace_drops_run_specific_paths(self) -> None:
        repo_path = self.root / "repo"
        repo_path.mkdir()
        self.repository.prepare_pack_for_spawn(
            TaskContextPack(
                work_order_id="wo_workspace_paths",
                goal="workspace paths",
                minion_profile="software_engineering.planner",
                workspace={
                    "repo_path": str(repo_path),
                    "workspace_policy": {"mode": "read_only_repo"},
                    "completion_policy": {"evidence": "text_deliverable"},
                },
                metadata={"task_id": "task_workspace_paths"},
            )
        )
        self.repository.update_work_order_workspace(
            "wo_workspace_paths",
            {
                "repo_path": str(repo_path),
                "workspace_policy": {"mode": "read_only_repo"},
                "completion_policy": {"evidence": "text_deliverable"},
                "run_dir": "/tmp/stale-run",
                "artifact_dir": "/tmp/stale-run/deliverables",
                "log_dir": "/tmp/stale-run/logs",
            },
        )

        stored = self.repository.pack_for_work_order("wo_workspace_paths")
        prepared = prepare_task_workspace(self.root, stored, run_id="run_next")

        self.assertNotIn("run_dir", stored.workspace)
        self.assertNotIn("artifact_dir", stored.workspace)
        self.assertNotIn("log_dir", stored.workspace)
        self.assertIn("run_next_software_engineering_planner", prepared.workspace["run_dir"])

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

    def test_commit_milestone_excludes_generated_artifacts(self) -> None:
        pack = self.repository.prepare_pack_for_spawn(
            TaskContextPack(work_order_id="wo_generated", goal="ignore generated", metadata={"task_id": "task_generated"})
        )
        prepared = prepare_git_task_environment(self.root, pack)
        repo = Path(prepared.workspace["repo_path"])

        (repo / "src").mkdir()
        (repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        (repo / "__pycache__").mkdir()
        (repo / "__pycache__" / "app.cpython-313.pyc").write_bytes(b"pyc")
        (repo / "build").mkdir()
        (repo / "build" / "thing.o").write_bytes(b"obj")
        (repo / "libthing.a").write_bytes(b"archive")
        (repo / "minion_outputs" / "wo_generated").mkdir(parents=True, exist_ok=True)
        (repo / "minion_outputs" / "wo_generated" / "report.md").write_text("report\n", encoding="utf-8")

        result = commit_milestone(repo, work_order_id="wo_generated", milestone_index=0, title="source only")

        self.assertEqual(result["status"], "committed")
        committed_files = _git(repo, "show", "--name-only", "--format=", result["commit_sha"], check=True).stdout.splitlines()
        self.assertEqual(committed_files, ["src/app.py"])
        status = _git(repo, "status", "--porcelain", check=True).stdout
        self.assertEqual(status, "")

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
        prepared = prepare_git_task_environment(
            self.root,
            TaskContextPack(
                work_order_id="wo_clone",
                goal="clone source",
                workspace={"source_repo": str(source)},
                metadata={"task_id": "task_clone"},
            ),
        )

        repo = Path(prepared.workspace["repo_path"])
        self.assertEqual(repo, self.root / "data" / "minion" / "repos" / "task_clone")
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
            requested_profile="software_engineering.planner",
        )
        planner_prepared = prepare_task_workspace(self.root, planner, run_id="run_plan")

        self.assertEqual(Path(planner_prepared.workspace["repo_path"]), source)
        self.assertEqual(planner_prepared.workspace["workspace_policy"]["mode"], "read_only_repo")
        self.assertEqual(planner_prepared.workspace["workspace_kind"], "folder")
        self.assertTrue(Path(planner_prepared.workspace["run_dir"]).is_dir())
        self.assertTrue(Path(planner_prepared.workspace["artifact_dir"]).is_dir())
        self.assertTrue(str(planner_prepared.workspace["run_dir"]).endswith("run_plan_software_engineering_planner"))
        self.assertTrue((Path(planner_prepared.workspace["run_dir"]) / "work_order.json").is_file())
        self.assertNotIn("work_order_branch", planner_prepared.workspace)
        self.assertFalse((source / "deliverables").exists())

        planner_with_cwd = registry.resolve_pack(
            TaskContextPack(
                work_order_id="wo_plan_readonly_cwd",
                goal="plan",
                workspace={"cwd": str(source), "type": "local_repo"},
            ),
            requested_profile="software_engineering.planner",
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
            requested_profile="software_engineering.coder",
        )
        coder_prepared = prepare_task_workspace(self.root, coder)

        self.assertEqual(coder_prepared.workspace["workspace_policy"]["mode"], "writable_git_branch")
        self.assertEqual(coder_prepared.workspace["completion_policy"]["evidence"], "git_commit")
        self.assertEqual(coder_prepared.workspace["workspace_kind"], "git_repo")
        self.assertTrue(Path(coder_prepared.workspace["artifact_dir"]).is_dir())
        self.assertTrue(str(coder_prepared.workspace["artifact_dir"]).replace("\\", "/").endswith("minion_outputs/wo_code_writable"))
        self.assertIn("work_order_branch", coder_prepared.workspace)
        self.assertNotEqual(Path(coder_prepared.workspace["repo_path"]), source)

        coder_from_repo_path = registry.resolve_pack(
            TaskContextPack(work_order_id="wo_code_repo_path", goal="code", workspace={"repo_path": str(source)}),
            requested_profile="software_engineering.coder",
        )
        coder_from_repo_path_prepared = prepare_task_workspace(self.root, coder_from_repo_path)
        coder_repo = Path(coder_from_repo_path_prepared.workspace["repo_path"])

        self.assertEqual(Path(coder_from_repo_path_prepared.workspace["source_repo"]), source)
        self.assertNotEqual(coder_repo, source)
        self.assertTrue(coder_repo.is_relative_to(self.root / "data" / "minion" / "repos"))
        self.assertEqual((coder_repo / "README.md").read_text(encoding="utf-8"), "SPEC\n")
        self.assertTrue(Path(coder_from_repo_path_prepared.workspace["artifact_dir"]).is_relative_to(coder_repo))
        self.assertFalse((source / "minion_outputs").exists())


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

    def test_manager_serially_spawns_next_module_milestone_and_reports_completion(self) -> None:
        class FakeSerialManager(MinionManager):
            def __post_init__(self) -> None:
                super().__post_init__()
                self.started_milestones = []

            async def _start_runner(self, state: MinionRunState) -> None:
                state.status = "running"
                prompt_view = dict(state.pack.metadata.get("prompt_view") or {})
                self.started_milestones.append(str((prompt_view.get("milestone") or {}).get("milestone_id") or ""))

        async def scenario() -> None:
            manager = FakeSerialManager(runtime_root=self.root)
            plan = {
                "type": "FinalPlanArtifact",
                "plan_id": "plan_manager_serial",
                "task_id": "task_manager_serial",
                "modules": [
                    {
                        "module_id": "module_manager_serial",
                        "internal_milestones": [
                            {"milestone_id": "m1", "title": "First", "task": "Do first."},
                            {"milestone_id": "m2", "title": "Second", "task": "Do second."},
                        ],
                    }
                ],
            }
            pack = manager.tasking_repository.build_coder_module_pack_from_plan(
                plan,
                module_id="module_manager_serial",
                work_order_id="wo_manager_serial",
            )
            first = await manager.spawn(pack.to_dict())
            first_state = manager.runs[first["run_id"]]

            manager._record_event(
                first_state,
                {
                    "event_kind": "checkpoint",
                    "payload": {"status": "completed", "milestone_index": 0, "summary": "m1 done"},
                    "created_at": "2026-01-01T00:00:00Z",
                },
            )
            manager._record_event(
                first_state,
                {
                    "event_kind": "terminal",
                    "payload": {"status": "completed", "summary": "m1 terminal"},
                    "created_at": "2026-01-01T00:00:01Z",
                },
            )
            for _ in range(20):
                if len(manager.started_milestones) >= 2:
                    break
                await asyncio.sleep(0.01)

            self.assertEqual(manager.started_milestones[:2], ["m1", "m2"])
            second_state = next(state for run_id, state in manager.runs.items() if run_id != first["run_id"])
            manager._record_event(
                second_state,
                {
                    "event_kind": "checkpoint",
                    "payload": {"status": "completed", "milestone_index": 1, "summary": "m2 done"},
                    "created_at": "2026-01-01T00:00:02Z",
                },
            )
            manager._record_event(
                second_state,
                {
                    "event_kind": "terminal",
                    "payload": {
                        "status": "completed",
                        "summary": "m2 terminal",
                        "deferred_experience": {
                            "memory_candidates": [{"kind": "case", "title": "serial done", "summary": "serial done"}],
                        },
                    },
                    "created_at": "2026-01-01T00:00:03Z",
                },
            )
            for _ in range(20):
                if any(event.get("event_kind") == "module_completed" for event in manager.event_queue):
                    break
                await asyncio.sleep(0.01)

            module_event = next(event for event in manager.event_queue if event.get("event_kind") == "module_completed")
            self.assertEqual(module_event["payload"]["module_id"], "module_manager_serial")
            self.assertEqual(module_event["payload"]["memory_candidates"][0]["title"], "serial done")

        asyncio.run(scenario())

    def test_manager_parent_plan_waits_for_continue_between_modules(self) -> None:
        class FakePlanManager(MinionManager):
            def __post_init__(self) -> None:
                super().__post_init__()
                self.started_modules = []

            async def _start_runner(self, state: MinionRunState) -> None:
                state.status = "running"
                prompt_view = dict(state.pack.metadata.get("prompt_view") or {})
                self.started_modules.append(str((prompt_view.get("module") or {}).get("module_id") or ""))

        async def scenario() -> None:
            manager = FakePlanManager(runtime_root=self.root)
            plan = {
                "type": "FinalPlanArtifact",
                "plan_id": "plan_parent_manager",
                "task_id": "task_parent_manager",
                "modules": [
                    {"module_id": "module_a", "internal_milestones": [{"milestone_id": "a1", "title": "A1", "task": "Do A."}]},
                    {"module_id": "module_b", "internal_milestones": [{"milestone_id": "b1", "title": "B1", "task": "Do B."}]},
                ],
            }
            parent = manager.tasking_repository.build_plan_parent_pack_from_plan(plan, work_order_id="wo_parent_manager")
            spawned_parent = await manager.spawn(parent.to_dict())

            self.assertTrue(spawned_parent["plan_parent"])
            self.assertEqual(manager.started_modules, ["module_a"])
            child_a = next(state for state in manager.runs.values() if state.pack.work_order_id != "wo_parent_manager")
            manager._record_event(
                child_a,
                {
                    "event_kind": "checkpoint",
                    "payload": {"status": "completed", "milestone_index": 0, "summary": "module A done"},
                    "created_at": "2026-01-01T00:00:00Z",
                },
            )
            manager._record_event(
                child_a,
                {
                    "event_kind": "terminal",
                    "payload": {"status": "completed", "summary": "module A terminal"},
                    "created_at": "2026-01-01T00:00:01Z",
                },
            )
            for _ in range(20):
                if any(event.get("event_kind") == "module_completed" for event in manager.event_queue):
                    break
                await asyncio.sleep(0.01)

            self.assertEqual(manager.started_modules, ["module_a"])
            parent_snapshot = manager.tasking_repository.read_work_order("wo_parent_manager")
            self.assertEqual(parent_snapshot["current_milestone"]["title"], "module_b")
            self.assertEqual(parent_snapshot["work_order"]["metadata"]["plan_execution"]["status"], "awaiting_continue")
            module_event = next(event for event in manager.event_queue if event.get("event_kind") == "module_completed")
            self.assertEqual(module_event["work_order_id"], "wo_parent_manager")
            self.assertTrue(module_event["payload"]["has_next_module"])

            continued = await manager.continue_work_order("wo_parent_manager")

            self.assertEqual(continued["status"], "running_module")
            self.assertEqual(continued["module_id"], "module_b")
            self.assertEqual(manager.started_modules, ["module_a", "module_b"])

        asyncio.run(scenario())

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

    def test_runner_accept_all_approval_skips_later_approvals_in_run(self) -> None:
        async def scenario() -> None:
            events = []
            decisions = [{"decision": {"decision": "accept_all"}}]
            executed = []

            class FakeLLM:
                def __init__(self):
                    self.calls = 0

                async def agenerate(self, request):
                    self.calls += 1
                    if self.calls == 1:
                        return CanonicalLLMOutcome(
                            text="",
                            tool_calls=[
                                CanonicalToolCall(name="op_fake_tool", args={"value": 1}, call_id="call_1"),
                                CanonicalToolCall(name="op_fake_tool", args={"value": 2}, call_id="call_2"),
                            ],
                        )
                    return CanonicalLLMOutcome(text="done")

            class FakeExecution:
                def get_capability_spec(self, name):
                    if name != "op_fake_tool":
                        return None
                    return {"name": "op_fake_tool", "description": "fake write tool", "parameters_schema": {}}

                async def execute_tool_async(self, call, **kwargs):
                    _ = kwargs
                    executed.append(dict(call.args))
                    return CanonicalToolResult(
                        name=call.name,
                        ok=True,
                        text="ok",
                        llm_text="ok",
                        call_id=call.call_id,
                        status=RuntimeStatus.OK,
                    )

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return decisions.pop(0)

            code = await MinionRunner(
                runtime_root=self.root,
                pack=TaskContextPack(
                    work_order_id="wo_accept_all",
                    goal="accept all approvals",
                    allowed_capabilities=["op_fake_tool"],
                    approval_policy={"high_risk_capabilities": ["op_fake_tool"], "decision_timeout_seconds": 5},
                    metadata={"allow_text_only_completion": True},
                ),
                minion_id="m_accept_all",
                run_id="r_accept_all",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=FakeLLM(), execution_runtime=FakeExecution()),
            ).run()

            approval_events = [event for event in events if event["event_kind"] == "approval_requested"]
            decision_events = [event for event in events if event["event_kind"] == "decision_received"]
            self.assertEqual(code, 0)
            self.assertEqual(len(approval_events), 1)
            self.assertEqual(decision_events[0]["payload"]["decision"], "accept_all")
            self.assertEqual(executed, [{"value": 1}, {"value": 2}])

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
                    minion_profile="software_engineering.planner",
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
            self.assertTrue(all(record["minion_profile"] == "software_engineering.planner" for record in records))
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
                ["op_minion_artifact_write", "op_minion_artifact_edit"],
                {"artifact_dir": str(artifact_dir)},
                produced_artifacts=produced,
            )

            specs = {spec["name"] for spec in runtime.list_capability_specs()}
            self.assertIn("op_minion_artifact_write", specs)
            self.assertIn("op_minion_artifact_edit", specs)

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

            second = await runtime.execute_tool_async(
                CanonicalToolCall(
                    name="op_minion_artifact_write",
                    args={"relative_path": "plan.md", "content": "second plan"},
                ),
                turn_id="r",
            )
            self.assertTrue(second.ok)
            self.assertEqual((artifact_dir / "plan.md").read_text(encoding="utf-8"), "hello plan")
            self.assertEqual((artifact_dir / "plan_2.md").read_text(encoding="utf-8"), "second plan")
            self.assertEqual([item["relative_path"] for item in produced], ["plan.md", "plan_2.md"])

            append = await runtime.execute_tool_async(
                CanonicalToolCall(
                    name="op_minion_artifact_edit",
                    args={"relative_path": "plan.md", "operation": "append", "content": "\nnext section"},
                ),
                turn_id="r",
            )
            self.assertTrue(append.ok)
            self.assertEqual((artifact_dir / "plan.md").read_text(encoding="utf-8"), "hello plan\nnext section")
            self.assertEqual([item["relative_path"] for item in produced], ["plan.md", "plan_2.md"])
            self.assertEqual(produced[0]["size_bytes"], len("hello plan\nnext section".encode("utf-8")))

            replace = await runtime.execute_tool_async(
                CanonicalToolCall(
                    name="op_minion_artifact_edit",
                    args={"relative_path": "plan.md", "operation": "replace", "content": "replacement"},
                ),
                turn_id="r",
            )
            self.assertTrue(replace.ok)
            self.assertEqual((artifact_dir / "plan.md").read_text(encoding="utf-8"), "replacement")
            self.assertEqual([item["relative_path"] for item in produced], ["plan.md", "plan_2.md"])

            create_missing = await runtime.execute_tool_async(
                CanonicalToolCall(
                    name="op_minion_artifact_edit",
                    args={"relative_path": "notes.md", "operation": "append", "content": "notes"},
                ),
                turn_id="r",
            )
            self.assertTrue(create_missing.ok)
            self.assertEqual((artifact_dir / "notes.md").read_text(encoding="utf-8"), "notes")
            self.assertEqual([item["relative_path"] for item in produced], ["plan.md", "plan_2.md", "notes.md"])

            missing = await runtime.execute_tool_async(
                CanonicalToolCall(
                    name="op_minion_artifact_edit",
                    args={"relative_path": "missing.md", "operation": "append", "content": "no", "create_if_missing": False},
                ),
                turn_id="r",
            )
            self.assertFalse(missing.ok)
            self.assertIn("does not exist", missing.text)

            empty = await runtime.execute_tool_async(
                CanonicalToolCall(name="op_minion_artifact_write", args={"relative_path": "empty.md", "content": ""}),
                turn_id="r",
            )
            self.assertFalse(empty.ok)
            self.assertIn("content is required", empty.text)

            empty_edit = await runtime.execute_tool_async(
                CanonicalToolCall(name="op_minion_artifact_edit", args={"relative_path": "empty.md", "content": ""}),
                turn_id="r",
            )
            self.assertFalse(empty_edit.ok)
            self.assertIn("content is required", empty_edit.text)

            escaped = await runtime.execute_tool_async(
                CanonicalToolCall(name="op_minion_artifact_write", args={"relative_path": "../escape.md", "content": "no"}),
                turn_id="r",
            )
            self.assertFalse(escaped.ok)
            self.assertIn("escapes artifact_dir", escaped.text)
            self.assertFalse((self.root / "escape.md").exists())

            escaped_edit = await runtime.execute_tool_async(
                CanonicalToolCall(name="op_minion_artifact_edit", args={"relative_path": "../escape.md", "content": "no"}),
                turn_id="r",
            )
            self.assertFalse(escaped_edit.ok)
            self.assertIn("escapes artifact_dir", escaped_edit.text)

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
                    metadata={"allow_text_only_completion": True},
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

    def test_runner_blocks_artifact_only_git_completion_unless_allowed(self) -> None:
        async def scenario() -> None:
            events = []
            repo_pack = prepare_git_task_environment(
                self.root,
                TaskContextPack(
                    work_order_id="wo_artifact_only_blocked",
                    goal="code change required",
                    continuity={"current_milestone": {"milestone_index": 0, "title": "code"}},
                ),
            )

            class FakeLLM:
                async def agenerate(self, request):
                    _ = request
                    return CanonicalLLMOutcome(text="I only wrote a report, no code changes.")

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return None

            code = await MinionRunner(
                runtime_root=self.root,
                pack=repo_pack,
                minion_id="m_artifact_only",
                run_id="r_artifact_only",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=FakeLLM(), execution_runtime=SimpleNamespace()),
            ).run()

            self.assertEqual(code, 0)
            checkpoint = next(event for event in events if event["event_kind"] == "checkpoint")
            self.assertEqual(checkpoint["payload"]["status"], "blocked")
            self.assertIn("milestone produced no git changes", checkpoint["payload"]["summary"])

        asyncio.run(scenario())

    def test_runner_retries_once_when_git_milestone_has_no_changes_after_tools(self) -> None:
        async def scenario() -> None:
            events = []
            repo_pack = prepare_git_task_environment(
                self.root,
                TaskContextPack(
                    work_order_id="wo_retry_no_changes",
                    goal="retry before report-only completion",
                    continuity={"current_milestone": {"milestone_index": 0, "title": "code"}},
                    allowed_capabilities=["op_fake_read", "op_fake_change"],
                ),
            )
            repo = Path(repo_pack.workspace["repo_path"])

            class FakeLLM:
                def __init__(self):
                    self.calls = 0
                    self.retry_seen = False

                async def agenerate(self, request):
                    self.calls += 1
                    messages = list(getattr(request, "messages", []) or [])
                    rendered = json.dumps(messages, ensure_ascii=False)
                    if "no runner-owned git completion evidence exists" in rendered:
                        self.retry_seen = True
                    if self.calls == 1:
                        return CanonicalLLMOutcome(text="", tool_calls=[CanonicalToolCall(name="op_fake_read", args={})])
                    if self.calls == 2:
                        return CanonicalLLMOutcome(text="report only, no changes")
                    if self.calls == 3:
                        return CanonicalLLMOutcome(text="", tool_calls=[CanonicalToolCall(name="op_fake_change", args={})])
                    return CanonicalLLMOutcome(text="changed after retry")

            llm = FakeLLM()

            class FakeExecution:
                def get_capability_spec(self, name):
                    if name not in {"op_fake_read", "op_fake_change"}:
                        return None
                    return {"name": name, "description": name, "parameters_schema": {"type": "object", "properties": {}}}

                async def execute_tool_async(self, call, **kwargs):
                    _ = kwargs
                    if call.name == "op_fake_change":
                        (repo / "changed.txt").write_text("done\n", encoding="utf-8")
                        return CanonicalToolResult(name=call.name, ok=True, text="changed", llm_text="changed", status=RuntimeStatus.OK)
                    return CanonicalToolResult(name=call.name, ok=True, text="read", llm_text="read", status=RuntimeStatus.OK)

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return None

            code = await MinionRunner(
                runtime_root=self.root,
                pack=repo_pack,
                minion_id="m_retry_no_changes",
                run_id="r_retry_no_changes",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=llm, execution_runtime=FakeExecution()),
            ).run()

            self.assertEqual(code, 0)
            self.assertTrue(llm.retry_seen)
            self.assertGreaterEqual(llm.calls, 4)
            checkpoint = next(event for event in events if event["event_kind"] == "checkpoint")
            self.assertEqual(checkpoint["payload"]["status"], "completed")
            self.assertEqual(checkpoint["payload"]["git_commit"]["status"], "committed")

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

    def test_runner_llm_error_after_git_evidence_completes(self) -> None:
        async def scenario() -> None:
            events = []
            repo_pack = prepare_git_task_environment(
                self.root,
                TaskContextPack(work_order_id="wo_llm_error_evidence", goal="finish", continuity={"current_milestone": {"milestone_index": 0}}),
            )
            repo_path = Path(repo_pack.workspace["repo_path"])
            (repo_path / "result.txt").write_text("done\n", encoding="utf-8")

            class FakeLLM:
                async def agenerate(self, request):
                    _ = request
                    return CanonicalLLMOutcome(text="LLM final reply failed", finish_reason=LLMFinishReason.ERROR)

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return None

            code = await MinionRunner(
                runtime_root=self.root,
                pack=repo_pack,
                minion_id="m_error_evidence",
                run_id="r_error_evidence",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=FakeLLM(), execution_runtime=SimpleNamespace()),
            ).run()

            self.assertEqual(code, 0)
            checkpoint = next(event for event in events if event["event_kind"] == "checkpoint")
            self.assertEqual(checkpoint["payload"]["status"], "completed")
            self.assertEqual(checkpoint["payload"]["evidence"], "git_commit")
            self.assertEqual(checkpoint["payload"]["git_commit"]["status"], "committed")
            self.assertIn("Milestone produced completion evidence", checkpoint["payload"]["summary"])
            self.assertIn("result.txt", _git(repo_path, "show", "--name-only", "--format=", "HEAD", check=True).stdout)

        asyncio.run(scenario())

    def test_runner_blocks_shell_git_checkpoint_mutation_without_blocking_run(self) -> None:
        async def scenario() -> None:
            called = False
            pack = TaskContextPack(
                work_order_id="wo_git_guard",
                goal="guard git checkpoint",
                allowed_capabilities=["op_exec_shell"],
                workspace={"completion_policy": {"evidence": "git_commit"}},
            )

            class FakeExecution:
                async def execute_tool_async(self, *args, **kwargs):
                    nonlocal called
                    called = True
                    raise AssertionError("shell execution should have been blocked before runtime execution")

            async def write_event(event):
                _ = event

            async def read_decision(timeout):
                _ = timeout
                return None

            runner = MinionRunner(
                runtime_root=self.root,
                pack=pack,
                minion_id="m_git_guard",
                run_id="r_git_guard",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=SimpleNamespace(), execution_runtime=SimpleNamespace()),
            )
            result = await runner._execute_allowed_tool(
                FakeExecution(),
                CanonicalToolCall(name="op_exec_shell", args={"cmd": "git add . && git commit -m done"}, call_id="call_git"),
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.structured["reason"], "runner_owns_git_checkpoint")
            self.assertIn("runner owns", result.llm_text)
            self.assertFalse(runner.blocked_summary)
            self.assertFalse(called)

        asyncio.run(scenario())

    def test_runner_truncated_llm_output_blocks_and_saves_partial_artifact(self) -> None:
        async def scenario() -> None:
            events = []
            artifact_dir = self.root / "truncated_artifacts"
            pack = TaskContextPack(
                work_order_id="wo_llm_length",
                goal="produce a long structured plan",
                workspace={"artifact_dir": str(artifact_dir)},
                continuity={"current_milestone": {"milestone_index": 0, "title": "Plan long output"}},
                metadata={"allow_text_only_completion": True},
            )
            partial_json = '{"task_id":"task_length","modules":[{"module_id":"a"}'

            class FakeLLM:
                async def agenerate(self, request):
                    _ = request
                    return CanonicalLLMOutcome(text=partial_json, finish_reason="length")

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return None

            code = await MinionRunner(
                runtime_root=self.root,
                pack=pack,
                minion_id="m_length",
                run_id="r_length",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=FakeLLM(), execution_runtime=SimpleNamespace()),
            ).run()

            self.assertEqual(code, 0)
            checkpoint = next(event for event in events if event["event_kind"] == "checkpoint")
            terminal = next(event for event in events if event["event_kind"] == "terminal")
            self.assertEqual(checkpoint["payload"]["status"], "blocked")
            self.assertIn("finish_reason=length", checkpoint["payload"]["summary"])
            self.assertIn("artifacts", checkpoint["payload"])
            self.assertEqual(terminal["payload"]["status"], "blocked")
            self.assertEqual(terminal["payload"]["artifacts"][0]["role"], "partial")
            artifact_path = Path(terminal["payload"]["artifacts"][0]["path"])
            self.assertEqual(artifact_path.name, "milestone_0_generic.partial.md")
            saved = artifact_path.read_text(encoding="utf-8")
            self.assertIn("partial LLM output", saved)
            self.assertIn("truncation_reason: length", saved)
            self.assertIn(partial_json, saved)

        asyncio.run(scenario())

    def test_runner_max_rounds_blocks_but_preserves_artifact_deliverable(self) -> None:
        async def scenario() -> None:
            events = []
            artifact_dir = self.root / "artifact_completion"
            pack = TaskContextPack(
                work_order_id="wo_artifact_done",
                goal="write the plan artifact",
                allowed_capabilities=["op_minion_artifact_write"],
                workspace={"artifact_dir": str(artifact_dir)},
                continuity={"current_milestone": {"milestone_index": 0, "title": "Write plan artifact"}},
                metadata={"max_tool_rounds": 1},
            )

            class FakeLLM:
                async def agenerate(self, request):
                    _ = request
                    return CanonicalLLMOutcome(
                        text="",
                        tool_calls=[
                            CanonicalToolCall(
                                name="op_minion_artifact_write",
                                args={
                                    "relative_path": "plan.md",
                                    "content": "full plan body",
                                    "title": "Plan",
                                    "role": "primary",
                                },
                            )
                        ],
                        finish_reason=LLMFinishReason.TOOL_CALLS,
                    )

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return None

            code = await MinionRunner(
                runtime_root=self.root,
                pack=pack,
                minion_id="m_artifact_done",
                run_id="r_artifact_done",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=FakeLLM(), execution_runtime=SimpleNamespace()),
            ).run()

            self.assertEqual(code, 0)
            checkpoint = next(event for event in events if event["event_kind"] == "checkpoint")
            terminal = next(event for event in events if event["event_kind"] == "terminal")
            self.assertEqual(checkpoint["payload"]["status"], "blocked")
            self.assertEqual(terminal["payload"]["status"], "blocked")
            self.assertIn("max_tool_rounds=1", terminal["payload"]["summary"])
            self.assertEqual((artifact_dir / "plan.md").read_text(encoding="utf-8"), "full plan body")
            self.assertEqual(len(terminal["payload"]["artifacts"]), 1)

        asyncio.run(scenario())

    def test_runner_compacts_minion_memory_when_preflight_requires_budget(self) -> None:
        async def scenario() -> None:
            events = []
            generated_messages = []

            class FakeLLM:
                def __init__(self):
                    self.preflight_calls = 0

                async def apreflight(self, request):
                    _ = request
                    self.preflight_calls += 1
                    if self.preflight_calls == 1:
                        return LLMPreflightAdvice(
                            status=LLMPreflightStatus.COMPACT_REQUIRED,
                            target_input_budget=256,
                            reserved_output_tokens=128,
                        )
                    return LLMPreflightAdvice(status=LLMPreflightStatus.READY)

                async def agenerate(self, request):
                    generated_messages.extend(request.messages)
                    return CanonicalLLMOutcome(text="compacted completion")

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return None

            llm = FakeLLM()
            code = await MinionRunner(
                runtime_root=self.root,
                pack=TaskContextPack(
                    work_order_id="wo_compact",
                    goal="finish after compaction",
                    metadata={"allow_text_only_completion": True},
                ),
                minion_id="m_compact",
                run_id="r_compact",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=llm, execution_runtime=SimpleNamespace()),
            ).run()

            self.assertEqual(code, 0)
            self.assertEqual(llm.preflight_calls, 2)
            phases = [event["payload"]["phase"] for event in events if event["event_kind"] == "progress"]
            self.assertIn("memory_compacted", phases)
            self.assertNotIn("## Minion Run Memory", generated_messages[0]["content"])
            rendered_messages = "\n".join(_message_text(message) for message in generated_messages)
            self.assertIn("<system-reminder>", rendered_messages)
            self.assertIn("Minion run memory", rendered_messages)
            self.assertIn("Current summary", rendered_messages)
            self.assertNotIn("Compaction Note", generated_messages[0]["content"])
            terminal = next(event for event in events if event["event_kind"] == "terminal")
            self.assertEqual(terminal["payload"]["status"], "completed")

        asyncio.run(scenario())

    def test_runner_compaction_preserves_current_tool_protocol(self) -> None:
        async def scenario() -> None:
            events = []

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return None

            runner = MinionRunner(
                runtime_root=self.root,
                pack=TaskContextPack(
                    work_order_id="wo_compact_protocol",
                    goal="compact without dropping active tool context",
                    metadata={"allow_text_only_completion": True},
                ),
                minion_id="m_compact_protocol",
                run_id="r_compact_protocol",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=SimpleNamespace(), execution_runtime=SimpleNamespace()),
            )
            state = MinionAgentLoopState(
                execution_runtime=SimpleNamespace(),
                memory_service=MemoryService(),
                memory_l3=SimpleNamespace(records=[]),
            )
            state.tool_protocol_messages = [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "op_probe", "arguments": "{}"}}],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "fresh tool result"},
            ]
            state.pending_assistant_tool_text = "pending"
            state.pending_tool_call_batch = [CanonicalToolCall(name="op_probe", args={}, call_id="call_2")]
            state.pending_tool_results = [
                CanonicalToolResult(
                    name="op_probe",
                    ok=True,
                    text="pending result",
                    llm_text="pending result",
                    call_id="call_2",
                )
            ]

            result = await runner._handle_minion_memory_compact(
                MinionRuntimeBundle(llm_runtime=SimpleNamespace(), execution_runtime=SimpleNamespace()),
                state,
                MemoryCompactEffect(target_input_budget=256, reserved_output_tokens=128),
            )

            self.assertEqual(result.status, RuntimeStatus.OK)
            self.assertEqual(state.tool_protocol_messages[1]["content"], "fresh tool result")
            self.assertEqual(state.pending_assistant_tool_text, "pending")
            self.assertEqual(state.pending_tool_call_batch[0].call_id, "call_2")
            self.assertEqual(state.pending_tool_results[0].text, "pending result")
            self.assertIn("memory_compacted", [event["payload"]["phase"] for event in events if event["event_kind"] == "progress"])

        asyncio.run(scenario())

    def test_minion_prompt_places_memory_after_tool_protocol_when_present(self) -> None:
        runner = MinionRunner(
            runtime_root=self.root,
            pack=TaskContextPack(
                work_order_id="wo_memory_after_tool",
                goal="finish with active tool context",
                metadata={"allow_text_only_completion": True},
            ),
            minion_id="m_memory_after_tool",
            run_id="r_memory_after_tool",
            write_event=lambda event: None,
            read_decision=lambda timeout: None,
            runtime_bundle=MinionRuntimeBundle(llm_runtime=SimpleNamespace(), execution_runtime=SimpleNamespace()),
        )
        memory_service = MemoryService()
        memory_service.compact(
            MemoryCompactRequest(
                target_input_budget=512,
                reserved_output_tokens=128,
                metadata={"semantic_summary": "Prior minion context was compacted."},
            )
        )
        state = MinionAgentLoopState(
            execution_runtime=SimpleNamespace(),
            memory_service=memory_service,
            memory_l3=SimpleNamespace(records=[]),
        )
        state.tool_protocol_messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "op_probe", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "fresh tool result"},
        ]

        messages = runner._minion_prompt_messages(state, PromptAssemblyContext(metadata={}))

        self.assertEqual([message["role"] for message in messages], ["system", "user", "assistant", "tool", "user"])
        self.assertNotIn("Minion run memory", _message_text(messages[1]))
        self.assertIn("fresh tool result", _message_text(messages[3]))
        self.assertIn("<system-reminder>", _message_text(messages[4]))
        self.assertIn("Minion run memory", _message_text(messages[4]))
        self.assertIn("Prior minion context was compacted.", _message_text(messages[4]))

    def test_runner_defers_experience_for_serial_module_completion(self) -> None:
        runner = MinionRunner(
            runtime_root=self.root,
            pack=TaskContextPack(
                work_order_id="wo_defer_exp",
                goal="finish serial module",
                metadata={
                    "module_execution": {
                        "mode": "serial_module_milestones",
                        "defer_experience_until_module_complete": True,
                    }
                },
            ),
            minion_id="m_defer_exp",
            run_id="r_defer_exp",
            write_event=lambda event: None,
            read_decision=lambda timeout: None,
            runtime_bundle=MinionRuntimeBundle(llm_runtime=SimpleNamespace(), execution_runtime=SimpleNamespace()),
        )
        runner.memory_candidates = [{"kind": "case", "title": "candidate", "summary": "candidate summary"}]

        payload = runner._terminal_payload(
            "completed",
            '{"task_lessons":["task lesson"],"system_lessons":["system lesson"],"summary":"done"}',
        )

        self.assertEqual(payload["task_lessons"], [])
        self.assertEqual(payload["system_lessons"], [])
        self.assertEqual(payload["memory_candidates"], [])
        self.assertEqual(payload["deferred_experience"]["task_lessons"], ["task lesson"])
        self.assertEqual(payload["deferred_experience"]["system_lessons"], ["system lesson"])
        self.assertEqual(payload["deferred_experience"]["memory_candidates"][0]["title"], "candidate")

    def test_runner_accepted_event_emits_compact_scaffold_summary(self) -> None:
        async def scenario() -> None:
            events = []

            class FakeLLM:
                async def agenerate(self, request):
                    _ = request
                    return CanonicalLLMOutcome(text="done")

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return None

            code = await MinionRunner(
                runtime_root=self.root,
                pack=TaskContextPack(
                    work_order_id="wo_compact_accepted_event",
                    goal="finish",
                    continuity={"recent_ledger": [{"payload": "x" * 20000}], "completed_milestones": []},
                    metadata={"allow_text_only_completion": True},
                ),
                minion_id="m_compact_accepted_event",
                run_id="r_compact_accepted_event",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=FakeLLM(), execution_runtime=SimpleNamespace()),
            ).run()

            self.assertEqual(code, 0)
            accepted = next(
                event for event in events if event["event_kind"] == "phase_started" and event["payload"]["phase"] == "accepted"
            )
            self.assertNotIn("prompt_scaffold", accepted["payload"])
            summary = accepted["payload"]["prompt_scaffold_summary"]
            self.assertEqual(summary["continuity"]["recent_ledger_count"], 1)
            self.assertLess(len(json.dumps(accepted["payload"], ensure_ascii=False)), 2000)

        asyncio.run(scenario())

    def test_runner_returns_ephemeral_memory_candidates_in_terminal_payload(self) -> None:
        async def scenario() -> None:
            events = []
            test_case = self

            class FakeLLM:
                def __init__(self):
                    self.calls = 0

                async def agenerate(self, request):
                    self.calls += 1
                    tool_names = {item["function"]["name"] for item in request.tools}
                    test_case.assertIn("op_minion_memory_candidate_write", tool_names)
                    if self.calls == 1:
                        return CanonicalLLMOutcome(
                            text="",
                            tool_calls=[
                                CanonicalToolCall(
                                    name="op_minion_memory_candidate_write",
                                    args={
                                        "kind": "case",
                                        "scope": "task",
                                        "title": "Minion compact contract",
                                        "summary": "Minion memory should be returned as a candidate for Pal to review.",
                                        "topics": ["minion", "memory"],
                                        "payload": {"source": "test"},
                                    },
                                )
                            ],
                        )
                    return CanonicalLLMOutcome(text="candidate recorded")

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return None

            code = await MinionRunner(
                runtime_root=self.root,
                pack=TaskContextPack(
                    work_order_id="wo_memory_candidate",
                    goal="record reusable memory",
                    allowed_capabilities=["op_minion_memory_candidate_write"],
                    metadata={"allow_text_only_completion": True},
                ),
                minion_id="m_memory_candidate",
                run_id="r_memory_candidate",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=FakeLLM(), execution_runtime=SimpleNamespace()),
            ).run()

            self.assertEqual(code, 0)
            terminal = next(event for event in events if event["event_kind"] == "terminal")
            self.assertEqual(terminal["payload"]["status"], "completed")
            candidates = terminal["payload"]["memory_candidates"]
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["source_kind"], "minion_ephemeral_l3")
            self.assertEqual(candidates[0]["candidate_state"], "candidate")
            self.assertEqual(candidates[0]["title"], "Minion compact contract")
            self.assertIn("Pal to review", candidates[0]["summary"])
            self.assertEqual(candidates[0]["topics"], ["minion", "memory"])
            self.assertEqual(candidates[0]["payload"], {"source": "test"})

        asyncio.run(scenario())

    def test_runner_does_not_expose_or_execute_denied_capabilities(self) -> None:
        async def scenario() -> None:
            events = []
            executed = []
            test_case = self
            pack = TaskContextPack(
                work_order_id="wo_denied",
                goal="do not mutate Pal state",
                allowed_capabilities=["op_memory_write", "op_minion_spawn", "op_exec_shell"],
            )

            class FakeLLM:
                async def agenerate(self, request):
                    tool_names = {item["function"]["name"] for item in request.tools}
                    test_case.assertNotIn("op_memory_write", tool_names)
                    test_case.assertNotIn("op_minion_spawn", tool_names)
                    test_case.assertIn("op_exec_shell", tool_names)
                    return CanonicalLLMOutcome(
                        text="",
                        tool_calls=[CanonicalToolCall(name="op_memory_write", args={"title": "bad"})],
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
                            "op_tool_search",
                            "op_tool_read",
                            "op_tool_call",
                            "op_file_read",
                            "op_file_edit",
                            "op_file_write",
                            "op_exec_shell",
                            "op_web_search",
                            "op_web_read",
                            "op_memory_recall",
                            "intro_minion_task_read",
                            "op_minion_spawn",
                            "op_memory_write",
                            "op_memory_update",
                            "op_memory_delete",
                        )
                    }

                def list_capability_specs(self):
                    return list(self.specs.values())

                def get_capability_spec(self, name):
                    return self.specs.get(name)

            allowed = [
                "op_tool_search",
                "op_tool_read",
                "op_tool_call",
                "op_file_read",
                "op_file_edit",
                "op_file_write",
                "op_exec_shell",
                "op_web_search",
                "op_web_read",
                "op_memory_recall",
                "intro_minion_task_read",
                "op_minion_spawn",
                "op_memory_write",
                "op_memory_update",
                "op_memory_delete",
            ]
            scoped = MinionScopedExecutionRuntime(FakeExecution(), allowed)
            tool_names = [item["function"]["name"] for item in _llm_tools_for_allowed(scoped, allowed)]

            self.assertEqual(
                tool_names,
                [
                    "op_tool_search",
                    "op_tool_read",
                    "op_tool_call",
                    "op_file_read",
                    "op_file_edit",
                    "op_file_write",
                    "op_exec_shell",
                    "op_web_search",
                    "op_web_read",
                    "op_memory_recall",
                ],
            )

            result = await scoped.execute_tool_async(CanonicalToolCall(name="op_tool_search", args={"top_k": 20}))
            hit_names = {item["name"] for item in result.structured["hits"]}
            self.assertIn("op_file_read", hit_names)
            self.assertIn("op_file_edit", hit_names)
            self.assertIn("op_file_write", hit_names)
            self.assertIn("op_exec_shell", hit_names)
            self.assertIn("op_web_search", hit_names)
            self.assertIn("op_web_read", hit_names)
            self.assertIn("op_memory_recall", hit_names)
            self.assertNotIn("intro_minion_task_read", hit_names)
            self.assertNotIn("op_minion_spawn", hit_names)
            self.assertNotIn("op_memory_write", hit_names)
            self.assertNotIn("op_memory_update", hit_names)
            self.assertNotIn("op_memory_delete", hit_names)

            denied_read = await scoped.execute_tool_async(CanonicalToolCall(name="op_tool_read", args={"name": "op_minion_spawn"}))
            self.assertEqual(denied_read.status, RuntimeStatus.NOT_FOUND)

        asyncio.run(scenario())

    def test_slim_runner_runtime_publishes_resident_work_capabilities(self) -> None:
        async def scenario() -> None:
            bundle = build_slim_minion_runtime(self.root)
            try:
                names = {spec["name"] for spec in bundle.execution_runtime.list_capability_specs()}
                self.assertIn("op_exec_shell", names)
                self.assertIn("op_web_search", names)
                self.assertIn("op_web_read", names)
                self.assertIn("op_memory_recall", names)
            finally:
                await bundle.close()

        asyncio.run(scenario())

    def test_runner_system_prompt_uses_targeted_recall_after_tool_failure(self) -> None:
        prompt = _render_system_prompt(
            {
                "identity": "You are a coder.",
                "behavior": "Do the milestone.",
                "output_contract": "Summarize results.",
                "allowed_capabilities": ["op_exec_shell", "op_memory_recall"],
            }
        )

        self.assertIn("obvious schema, argument, path, or local input mistake", prompt)
        self.assertIn("correct the call directly", prompt)
        self.assertIn("repeated retries would be guesswork", prompt)
        self.assertIn("use `op_memory_recall`", prompt)
        self.assertIn("before retrying, debugging further, or reporting blocked", prompt)
        self.assertIn("If completion evidence cannot be produced", prompt)
        self.assertIn("<operating_rules>", prompt)
        self.assertIn("<allowed_capabilities>", prompt)
        self.assertNotIn("Allowed capabilities:", prompt)
        self.assertNotIn("##", prompt)
        self.assertNotIn("disposable task runner", prompt)

    def test_planner_prompt_is_contract_and_milestone_handoff_oriented(self) -> None:
        registry = MinionProfileRegistry(runtime_root=self.root)
        pack = registry.resolve_pack(TaskContextPack(work_order_id="wo_planner_prompt", goal="plan module"), requested_profile="software_engineering.planner")
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

        self.assertIn("design top-down", prompt)
        self.assertIn("Preserve the task_id", prompt)
        self.assertIn("Prefer official documentation", prompt)
        self.assertIn("module-first", prompt)
        self.assertIn("AskUserQuestion", prompt)
        self.assertIn("Unit tests", prompt)
        self.assertIn("Module tests", prompt)
        self.assertIn("Integration tests", prompt)
        self.assertIn("dogfood", prompt)
        self.assertIn("Output exactly one JSON object", prompt)
        self.assertIn('"type": "FinalPlanArtifact"', prompt)
        self.assertIn("Do not output markdown tables", prompt)
        self.assertIn("op_workspace_read", prompt)
        self.assertIn("<identity>", prompt)
        self.assertIn("<behavior_guidance>", prompt)
        self.assertIn("<workspace_policy>", prompt)
        self.assertNotIn("op_exec_shell", pack.allowed_capabilities)
        self.assertNotIn("Stay read-only", prompt)
        self.assertNotIn("disposable task runner", prompt)

    def test_coder_prompt_requires_developer_test_evidence(self) -> None:
        registry = MinionProfileRegistry(runtime_root=self.root)
        pack = registry.resolve_pack(
            TaskContextPack(
                work_order_id="wo_coder_prompt",
                goal="implement the bounded code change",
                acceptance_criteria=["Add regression coverage for the changed behavior."],
            ),
            requested_profile="software_engineering.coder",
        )
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

        self.assertTrue(pack.workspace["completion_policy"]["requires_developer_tests"])
        self.assertIn("scoped software work order", prompt)
        self.assertIn("bottom-up", prompt)
        self.assertIn("developer test plan", prompt)
        self.assertIn("Completion requires developer test evidence", prompt)
        self.assertIn("Fix failures", prompt)
        self.assertIn("Dogfood", prompt)
        self.assertIn("tests/commands run with pass/fail evidence", prompt)
        self.assertIn("op_file_read", pack.allowed_capabilities)
        self.assertIn("op_file_edit", pack.allowed_capabilities)
        self.assertIn("op_file_write", pack.allowed_capabilities)
        self.assertIn("op_minion_artifact_write", pack.allowed_capabilities)
        self.assertIn("op_minion_artifact_edit", pack.allowed_capabilities)
        self.assertIn("op_file_edit", prompt)
        self.assertIn("op_file_write", prompt)
        self.assertIn("op_minion_artifact_write", prompt)
        self.assertIn("op_minion_artifact_edit", prompt)
        self.assertIn("Do not create or commit generated", prompt)
        self.assertIn("CMakeLists.txt", prompt)

    def test_reviewer_prompt_uses_contract_lifecycle_and_severity_rubric(self) -> None:
        registry = MinionProfileRegistry(runtime_root=self.root)
        pack = registry.resolve_pack(
            TaskContextPack(work_order_id="wo_reviewer_prompt", goal="review the bounded change"),
            requested_profile="software_engineering.reviewer",
        )
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

        self.assertIn("Trace happens-before chains", prompt)
        self.assertIn("contract alignment", prompt)
        self.assertIn("preconditions and postconditions", prompt)
        self.assertIn("exception escape", prompt)
        self.assertIn("lifecycle management", prompt)
        self.assertIn("blocker: correctness/security/data loss/API breakage", prompt)
        self.assertIn("note: residual risk or observation", prompt)
        self.assertNotIn("op_exec_shell", pack.allowed_capabilities)

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
                self.assertIn("intro_minion_task_search", names)
                self.assertIn("intro_minion_task_read", names)
                self.assertIn("intro_minion_work_order_search", names)
                self.assertIn("intro_minion_work_order_read", names)
                self.assertIn("intro_minion_work_order_draft_search", names)
                self.assertIn("intro_minion_work_order_draft_read", names)
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
                self.assertIn("intro_minion_work_order_draft_read", candidate.capability_refs)
                self.assertIn("intro_minion_profile_list", candidate.capability_refs)
                self.assertIn("intro_minion_profile_read", candidate.capability_refs)
                self.assertIn("op_minion_spawn", candidate.capability_refs)
                self.assertIn("intro_minion_work_order_read", candidate.capability_refs)
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
                    CapabilityCall(name="intro_minion_work_order_draft_search", args={"query": "replace active minions"})
                )
                read = core.context.execution_runtime.execute(
                    CapabilityCall(name="intro_minion_work_order_draft_read", args={"draft_id": draft_id})
                )

                self.assertEqual(searched.status, "ok")
                self.assertEqual(searched.structured["items"][0]["draft_id"], draft_id)
                self.assertEqual(read.structured["work_order_candidate"]["metadata"]["work_order_draft_id"], draft_id)
                self.assertNotIn("payload_json", created.llm_text)
                self.assertNotIn('"payload"', created.llm_text)
                self.assertNotIn("payload_json", read.llm_text)
                self.assertNotIn('"payload"', read.llm_text)
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
                    CapabilityCall(name="intro_minion_work_order_read", args={"work_order_id": "wo_draft_promotion"})
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
                        args={"draft_id": created_2.structured["draft"]["draft_id"], "minion_profile": "software_engineering.planner"},
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
                profile_ids = {item["canonical_profile_id"] for item in listed.structured["items"]}
                self.assertIn("generic", profile_ids)
                self.assertIn("software_engineering.planner", profile_ids)
                self.assertIn("software_engineering.coder", profile_ids)
                self.assertNotIn("architect", profile_ids)
                self.assertEqual(listed.structured["runtime_profile_dir"], str(Path(tmp) / "plugins" / "minion" / "profiles"))
                self.assertIn("runtime TOML", listed.structured["profile_source_order"])
                self.assertIn("canonical_profile_id", listed.structured["usage"])

                planner = core.context.execution_runtime.execute(CapabilityCall(name="intro_minion_profile_read", args={"profile_id": "software_engineering.planner"}))
                self.assertEqual(planner.status, "ok")
                self.assertEqual(planner.structured["profile_group"], "software_engineering")
                self.assertEqual(planner.structured["workspace_policy"]["mode"], "read_only_repo")
                self.assertIn("work order", planner.structured["identity_fragment"].lower())

                read = core.context.execution_runtime.execute(CapabilityCall(name="intro_minion_profile_read", args={"profile_id": "software_engineering.reviewer"}))
                self.assertEqual(read.status, "ok")
                self.assertEqual(read.structured["profile_id"], "reviewer")

                spawn_spec = core.context.execution_runtime.get_capability_spec("op_minion_spawn")
                self.assertIsNotNone(spawn_spec)
                assert spawn_spec is not None
                self.assertIn("intro_minion_profile_list", spawn_spec["description"])
                self.assertIn("intro_minion_profile_read", spawn_spec["description"])
                self.assertIn("runtime_root/plugins/minion/profiles/*.toml", spawn_spec["description"])
                self.assertIn("Do not spawn from a bare goal/profile handoff", spawn_spec["description"])
                self.assertIn("prompt_view", spawn_spec["description"])
                self.assertIn("preferred_endpoint_id", spawn_spec["description"])
                pack_arg = spawn_spec["parameters_schema"]["properties"]["task_context_pack"]
                self.assertIn("goal-only packet", pack_arg["description"])
                self.assertIn("instruction", pack_arg["required"])
                self.assertIn("acceptance_criteria", pack_arg["required"])
                self.assertIn("metadata", pack_arg["required"])
                self.assertIn("prompt_view", pack_arg["properties"]["metadata"]["properties"])
                profile_arg = spawn_spec["parameters_schema"]["properties"]["minion_profile"]
                self.assertIn("profile_id", profile_arg["description"])
                endpoint_arg = spawn_spec["parameters_schema"]["properties"]["preferred_endpoint_id"]
                self.assertIn("active endpoint", endpoint_arg["description"])

                draft_spec = core.context.execution_runtime.get_capability_spec("op_minion_draft_work_order")
                self.assertIsNotNone(draft_spec)
                assert draft_spec is not None
                self.assertIn("Draft a minion work order candidate", draft_spec["description"])
                self.assertIn("prompt_view", draft_spec["description"])
                draft_required = draft_spec["parameters_schema"]["required"]
                self.assertEqual(draft_required, ["goal"])
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
                            "minion_profile": "software_engineering.coder",
                            "preferred_endpoint_id": "coder_fast",
                            "task_context_pack": {"work_order_id": "wo_coder", "goal": "make a change"},
                        },
                    )
                )
                self.assertEqual(spawned.status, "ok")
                self.assertEqual(spawned.structured["minion_profile"], "software_engineering.coder")
                read = core.context.execution_runtime.execute(
                    CapabilityCall(name="intro_minion_read", args={"run_id": spawned.structured["run_id"]})
                )
                pack = read.structured["task_context_pack"]
                self.assertEqual(pack["minion_profile"], "software_engineering.coder")
                self.assertEqual(pack["resolved_profile"]["profile_id"], "coder")
                self.assertIn("op_file_read", pack["allowed_capabilities"])
                self.assertIn("op_file_edit", pack["allowed_capabilities"])
                self.assertIn("op_file_write", pack["allowed_capabilities"])
                self.assertIn("op_exec_shell", pack["allowed_capabilities"])
                self.assertIn("op_minion_artifact_write", pack["allowed_capabilities"])
                self.assertIn("op_minion_artifact_edit", pack["allowed_capabilities"])
                self.assertIn("op_fake_extra", pack["allowed_capabilities"])
                self.assertEqual(pack["approval_policy"]["high_risk_capabilities"], ["op_exec_shell"])
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
                            "minion_profile": "software_engineering.planner",
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
        self.assertIsNotNone(action.delivery)
        assert action.delivery is not None and action.delivery.interaction is not None
        self.assertEqual(action.delivery.delivery_kind, "interactive_open")
        self.assertEqual(action.delivery.interaction.buttons[0][0].action_key, "control.action.dispatch")
        self.assertEqual(action.delivery.interaction.buttons[0][0].action_args["action_kind"], "minion_approval_decision")
        self.assertEqual(action.delivery.interaction.buttons[0][1].label, "Accept All")
        self.assertEqual(action.delivery.interaction.buttons[0][1].action_args["args"]["decision"], "accept_all")

    def test_minion_approval_text_truncates_large_args_and_preserves_decision_target(self) -> None:
        route = ControlRoute(endpoint_id="telegram_main", channel_kind="telegram", reply_target={"chat_id": "42"})
        event = EventEnvelope(
            event_kind=EventKind.APPROVAL_REQUEST,
            source_kind=SourceKind.MINION,
            payload={
                "run_id": "r1",
                "minion_id": "m1",
                "work_order_id": "wo1",
                "requested_action": "op_exec_shell",
                "args_summary": {"cmd": "cat <<'EOF'\n" + ("x" * 10000) + "\nEOF"},
                "route": {
                    "endpoint_id": route.endpoint_id,
                    "channel_kind": route.channel_kind,
                    "reply_target": route.reply_target,
                },
            },
        )

        derived = MinionControlEventHandler().handle(event, context=None)

        self.assertEqual(len(derived), 1)
        action = derived[0].payload
        self.assertIsNotNone(action.delivery)
        assert action.delivery is not None and action.delivery.interaction is not None
        interaction = action.delivery.interaction
        self.assertLess(len(interaction.text), 3400)
        self.assertIn("[truncated]", interaction.text)
        accept = interaction.buttons[0][0]
        self.assertTrue(accept.action_args["target_id"].startswith("approval_"))
        self.assertEqual(accept.action_args["args"]["approval_id"], accept.action_args["target_id"])

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
                            "minion_profile": "software_engineering.planner",
                            "payload": {
                                "phase": "tool_call_started",
                                "summary": "Tool started: op_exec_shell",
                                "target_name": "op_exec_shell",
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
        self.assertEqual(events[0].payload["minion_profile"], "software_engineering.planner")

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
                "minion_profile": "software_engineering.planner",
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
                            "minion_profile": "software_engineering.coder",
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
        self.assertIsNotNone(action.delivery)
        assert action.delivery is not None
        self.assertIn("Minion finished: completed", action.delivery.text)
        self.assertIn("[1] Review:", action.delivery.text)
        self.assertIn("/tmp/minion/review.md", action.delivery.text)

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
        delivery = derived[0].payload.delivery
        assert delivery is not None
        text = delivery.text
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
        delivery = derived[0].payload.delivery
        assert delivery is not None
        rendered, _mode = _telegram_markdown(delivery.text)

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
                "minion_profile": "software_engineering.planner",
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
        self.assertIsNotNone(finished.delivery)
        assert finished.delivery is not None
        self.assertNotIn("Task Lesson", finished.delivery.text)
        self.assertEqual(approval.action_kind, "interactive_open")
        self.assertIsNotNone(approval.delivery)
        assert approval.delivery is not None and approval.delivery.interaction is not None
        self.assertEqual(approval.delivery.interaction.interaction_kind, "minion_lesson_approval")
        buttons = approval.delivery.interaction.buttons[0]
        self.assertEqual([button.label for button in buttons], ["Accept", "Reject", "Edit"])
        self.assertEqual(buttons[0].action_args["action_kind"], "minion_lesson_decision")

    def test_minion_terminal_memory_candidates_open_approval(self) -> None:
        long_summary = "Use the compact contract when minions need to preserve reusable work. " + ("detail " * 80)
        candidate = {
            "kind": "case",
            "scope": "task",
            "title": "Reusable minion pattern",
            "summary": long_summary,
            "search_text": long_summary,
            "topics": ["minion", "memory"],
            "payload": {"source": "test"},
        }
        event = EventEnvelope(
            event_kind=EventKind.MINION_TERMINAL,
            source_kind=SourceKind.MINION,
            payload={
                "status": "completed",
                "summary": "Done.",
                "memory_candidates": [candidate],
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
        approval = derived[1].payload
        self.assertEqual(approval.action_kind, "interactive_open")
        self.assertIsNotNone(approval.delivery)
        assert approval.delivery is not None and approval.delivery.interaction is not None
        self.assertIn("Memory candidates:", approval.delivery.interaction.text)
        self.assertIn("Reusable minion pattern", approval.delivery.interaction.text)
        self.assertIn("detail detail detail detail", approval.delivery.interaction.text)
        accept = approval.delivery.interaction.buttons[0][0]
        self.assertEqual(accept.action_args["args"]["memory_candidates"], [candidate])

    def test_minion_module_completed_opens_continue_interaction(self) -> None:
        event = EventEnvelope(
            event_kind=EventKind.MINION_MODULE_COMPLETED,
            source_kind=SourceKind.MINION,
            payload={
                "status": "awaiting_continue",
                "summary": "Module A is done.",
                "parent_work_order_id": "wo_parent_continue",
                "work_order_id": "wo_parent_continue",
                "module_id": "module_a",
                "next_module_id": "module_b",
                "parent_milestone_index": 0,
                "has_next_module": True,
                "minion_profile": "software_engineering.coder",
                "route": {
                    "endpoint_id": "telegram_main",
                    "channel_kind": "telegram",
                    "reply_target": {"chat_id": "42"},
                },
            },
        )

        derived = MinionControlEventHandler().handle(event, context=None)

        self.assertEqual(len(derived), 1)
        action = derived[0].payload
        self.assertEqual(action.action_kind, "interactive_open")
        self.assertIsNotNone(action.delivery)
        assert action.delivery is not None and action.delivery.interaction is not None
        interaction = action.delivery.interaction
        self.assertEqual(interaction.interaction_kind, "minion_module_continue")
        self.assertIn("Next module: module_b", interaction.text)
        self.assertEqual([button.label for button in interaction.buttons[0]], ["Continue", "Pause"])
        self.assertEqual(interaction.buttons[0][0].action_args["action_kind"], "minion_plan_continue")
        self.assertEqual(interaction.buttons[0][0].action_args["args"]["work_order_id"], "wo_parent_continue")

    def test_minion_clarification_request_opens_question_interaction(self) -> None:
        event = EventEnvelope(
            event_kind=EventKind.MINION_CLARIFICATION_REQUEST,
            source_kind=SourceKind.MINION,
            payload={
                "clarification_id": "clarify_1",
                "questions": [
                    {
                        "question_id": "q1",
                        "question": "Which module should go first?",
                        "why_needed": "The choice changes sequencing.",
                        "evidence": [{"file": "src/pal/minion/runner.py", "finding": "Runner has one active milestone."}],
                        "options": [
                            {"id": "module_a", "label": "Module A"},
                            {"id": "module_b", "label": "Module B"},
                        ],
                    }
                ],
                "turn_index": 1,
                "plan_revision": 2,
                "minion_id": "m1",
                "run_id": "r1",
                "work_order_id": "wo_question",
                "minion_profile": "software_engineering.planner",
                "route": {
                    "endpoint_id": "telegram_main",
                    "channel_kind": "telegram",
                    "reply_target": {"chat_id": "42"},
                },
            },
        )

        derived = MinionControlEventHandler().handle(event, context=None)

        self.assertEqual(len(derived), 1)
        question = derived[0].payload
        self.assertEqual(question.action_kind, "interactive_open")
        self.assertIsNotNone(question.delivery)
        assert question.delivery is not None and question.delivery.interaction is not None
        interaction = question.delivery.interaction
        self.assertEqual(interaction.interaction_kind, "minion_question")
        self.assertIn("Which module should go first?", interaction.text)
        self.assertIn("Options:", interaction.text)
        self.assertEqual(interaction.buttons[0][0].label, "Module A")
        self.assertEqual(interaction.buttons[0][0].action_args["action_kind"], "minion_question_select")
        self.assertEqual(interaction.buttons[0][0].action_args["args"]["selected_option_id"], "module_a")

    def test_minion_question_interaction_preserves_long_question_context_in_message_text(self) -> None:
        long_question = "Which contract should the planner preserve for downstream coder work? " + ("detail " * 120)
        long_why = "This affects module boundaries and whether the coder can work with a narrow prompt. " + ("reason " * 80)
        long_evidence = "Runner currently parses ask_user_question from final output. " + ("evidence " * 90)
        long_option = "Use a clarification round that resumes the same planner work order. " + ("option " * 40)
        event = EventEnvelope(
            event_kind=EventKind.MINION_CLARIFICATION_REQUEST,
            source_kind=SourceKind.MINION,
            payload={
                "clarification_id": "clarify_long",
                "questions": [
                    {
                        "question_id": "q1",
                        "question": long_question,
                        "why_needed": long_why,
                        "evidence": [
                            {"file": "src/pal/minion/runner.py", "finding": long_evidence},
                            {"file": "src/pal/minion/repository.py", "finding": "Terminal events update work order state."},
                        ],
                        "options": [
                            {"id": "resume", "label": long_option},
                            {"id": "restart", "label": "Restart planner with a new work order."},
                        ],
                    }
                ],
                "minion_id": "m1",
                "run_id": "r1",
                "work_order_id": "wo_question_long",
                "minion_profile": "software_engineering.planner",
                "route": {
                    "endpoint_id": "telegram_main",
                    "channel_kind": "telegram",
                    "reply_target": {"chat_id": "42"},
                },
            },
        )

        derived = MinionControlEventHandler().handle(event, context=None)

        interaction = derived[0].payload.delivery.interaction
        assert interaction is not None
        self.assertLessEqual(len(interaction.text), 3900)
        self.assertIn("detail detail detail", interaction.text)
        self.assertIn("reason reason reason", interaction.text)
        self.assertIn("evidence evidence evidence", interaction.text)
        self.assertIn("src/pal/minion/repository.py", interaction.text)
        self.assertIn("Options:", interaction.text)
        self.assertIn("Use a clarification round", interaction.text)
        self.assertTrue(interaction.buttons[0][0].label.startswith("Use a clarification"))
        self.assertEqual(interaction.buttons[0][0].action_args["args"]["answer"], long_option.strip())

    def test_minion_question_select_paginates_and_back_shows_selected_answer(self) -> None:
        route = ControlRoute(endpoint_id="telegram_main", channel_kind="telegram", reply_target={"chat_id": "42"})
        payload = {
            "clarification_id": "clarify_pages",
            "run_id": "r1",
            "minion_id": "m1",
            "work_order_id": "wo_pages",
            "questions": [
                {
                    "question_id": "q1",
                    "question": "Which module goes first?",
                    "options": [{"id": "module_a", "label": "Module A"}, {"id": "module_b", "label": "Module B"}],
                },
                {
                    "question_id": "q2",
                    "question": "Which test depth?",
                    "options": [{"id": "unit", "label": "Unit only"}, {"id": "integration", "label": "Integration"}],
                },
            ],
        }
        interaction = build_minion_question_interaction(payload, route)
        assert interaction is not None
        provider = MinionManagerProvider(runtime_root=Path(tempfile.mkdtemp(prefix="pal_minion_question_pages_")))

        update = asyncio.run(
            provider.handle_control_action_async(
                ControlAction(
                    action_kind="minion_question_select",
                    target_scope="minion",
                    target_id="clarify_pages",
                    route=route,
                    args=interaction.buttons[0][0].action_args["args"],
                )
            )
        )

        page_two = update["delivery"].interaction
        assert page_two is not None
        self.assertIn("Question 2/2", page_two.text)
        self.assertIn("Answered: 1/2", page_two.text)
        back = next(button for row in page_two.buttons for button in row if button.label == "Back")
        back_update = asyncio.run(
            provider.handle_control_action_async(
                ControlAction(
                    action_kind="minion_question_nav",
                    target_scope="minion",
                    target_id="clarify_pages",
                    route=route,
                    args=back.action_args["args"],
                )
            )
        )

        page_one = back_update["delivery"].interaction
        assert page_one is not None
        self.assertIn("Question 1/2", page_one.text)
        self.assertIn("> 1. Module A", page_one.text)
        self.assertEqual(page_one.buttons[0][0].label, "> Module A")

    def test_minion_question_final_required_answer_submits_batch_to_runner(self) -> None:
        route = ControlRoute(endpoint_id="telegram_main", channel_kind="telegram", reply_target={"chat_id": "42"})
        payload = {
            "clarification_id": "clarify_submit",
            "run_id": "r1",
            "minion_id": "m1",
            "work_order_id": "wo_submit",
            "questions": [
                {
                    "question_id": "q1",
                    "question": "Which module goes first?",
                    "options": [{"id": "module_a", "label": "Module A"}],
                },
                {
                    "question_id": "q2",
                    "question": "Which test depth?",
                    "options": [{"id": "integration", "label": "Integration"}],
                },
            ],
        }
        interaction = build_minion_question_interaction(payload, route)
        assert interaction is not None

        class FakeClient:
            def __init__(self) -> None:
                self.submitted = []

            def send_clarification_sync(self, clarification):
                self.submitted.append(dict(clarification))
                return {"ok": True, "run": {"run_id": "r1"}}

        provider = MinionManagerProvider(runtime_root=Path(tempfile.mkdtemp(prefix="pal_minion_question_submit_")))
        fake_client = FakeClient()
        provider.client = fake_client
        provider._ensure_manager_started = lambda: None  # type: ignore[method-assign]

        first_update = asyncio.run(
            provider.handle_control_action_async(
                ControlAction(
                    action_kind="minion_question_select",
                    target_scope="minion",
                    target_id="clarify_submit",
                    route=route,
                    args=interaction.buttons[0][0].action_args["args"],
                )
            )
        )
        page_two = first_update["delivery"].interaction
        assert page_two is not None
        final = asyncio.run(
            provider.handle_control_action_async(
                ControlAction(
                    action_kind="minion_question_select",
                    target_scope="minion",
                    target_id="clarify_submit",
                    route=route,
                    args=page_two.buttons[0][0].action_args["args"],
                )
            )
        )

        self.assertEqual(len(fake_client.submitted), 1)
        submitted = fake_client.submitted[0]
        self.assertEqual(submitted["clarification_id"], "clarify_submit")
        self.assertEqual([answer["question_id"] for answer in submitted["answers"]], ["q1", "q2"])
        self.assertEqual(final["delivery"].delivery_kind, "interactive_resolve")

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

    def test_minion_lesson_accept_button_commits_memory_candidates(self) -> None:
        class RecordingRuntime:
            def __init__(self) -> None:
                self.capabilities = {"op_memory_write": object()}
                self.calls = []

            def execute(self, call):
                self.calls.append(call)
                return CapabilityResult(status=RuntimeStatus.OK, text="ok", llm_text="ok")

        with tempfile.TemporaryDirectory(prefix="pal_minion_memory_accept_") as tmp:
            repository = MinionTaskingRepository(runtime_root=Path(tmp))
            repository.prepare_pack_for_spawn(
                TaskContextPack(
                    work_order_id="wo_memory_accept",
                    goal="learn",
                    metadata={"task_id": "task_memory_accept", "milestones": ["Done"]},
                )
            )
            runtime = RecordingRuntime()
            provider = MinionManagerProvider(
                runtime_root=Path(tmp),
                context=SimpleNamespace(execution_runtime=runtime),
            )

            message = asyncio.run(
                provider.handle_control_action_async(
                    ControlAction(
                        action_kind="minion_lesson_decision",
                        target_scope="minion",
                        target_id="wo_memory_accept",
                        args={
                            "decision": "accept",
                            "work_order_id": "wo_memory_accept",
                            "memory_candidates": [
                                {
                                    "kind": "case",
                                    "scope": "task",
                                    "title": "Reusable minion pattern",
                                    "summary": "Use the compact contract when minions need reusable memory.",
                                    "topics": ["minion", "memory"],
                                    "payload": {"source": "test"},
                                }
                            ],
                        },
                    )
                )
            )

            self.assertIn("accepted", message)
            self.assertIn("memory records committed", message)
            self.assertEqual(len(runtime.calls), 1)
            call = runtime.calls[0]
            self.assertEqual(call.name, "op_memory_write")
            self.assertEqual(call.args["title"], "Reusable minion pattern")
            self.assertEqual(call.args["search_text"], "Use the compact contract when minions need reusable memory.")
            self.assertEqual(call.args["task_id"], "wo_memory_accept")
            self.assertEqual(call.args["payload"], {"source": "test"})

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
