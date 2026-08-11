from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, new_tool_call

import asyncio
import contextlib
from copy import deepcopy
import json
import os
import sqlite3
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pal.core.main_context import MainContext
from pal.core.runtime_state import RuntimeSnapshotCoordinator
from pal.core.runtime import PalCore
from pal.execution.capabilities import register_with_core as register_execution_with_core
from pal.execution.contracts import CapabilityCall
from pal.memory.capabilities import register_with_core as register_memory_with_core
from pal.minion import register_with_core as register_minion_with_core
from pal.minion.capabilities import MinionManagerProvider, inspect_minion
from pal.minion.ipc import minion_port_path, minion_socket_path
from pal.minion.sandbox import build_sandboxed_runner_invocation
from pal.minion.v2.adapters import prepare_v2_workspace_environment
from pal.minion.v2.capabilities import (
    MinionV2CapabilitiesMinionV2PublicProviderStartWorkflowInput,
    MinionV2CapabilitiesMinionV2PublicProviderSubmitHumanDecisionInput,
    MinionV2PublicProvider,
)
from pal.minion.v2.ask_question import ASK_QUESTION_CAPABILITY
from pal.minion.v2.architecture_templates import ArchitectureTemplateCompiler
from pal.minion.v2.contract_submission import CONTRACT_SUBMIT_CAPABILITY
from pal.minion.v2.execution import WorkspaceProcessHolder
from pal.minion.v2.cycle_protocol import (
    AssignmentKind,
    CycleSlot,
    PlanCycle,
    PlanCycleState,
)
from pal.minion.v2.graph_protocol import GraphIR, NodeSpec, RoleBinding
from pal.minion.v2.workflow_runtime import WorkflowCoordinator
from pal.minion.v2.orchestration import (
    MinionV2OutboxProcessor,
    _execution_epoch_id,
    reconcile_control_requests,
)
from pal.minion.v2.service import MinionV2WorkflowService, _active_workflow_lineage_ids
from pal.minion.v2.human_review import HUMAN_REVIEW_RENDER_VERSION
from pal.minion.v2.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.minion.v2.sessions import (
    architect_session_id,
    coder_session_id,
    module_verifier_session_id,
)
from pal.minion.v2.semantic_orchestration.orchestrator import (
    SemanticOrchestrator,
    _charged_role_failure_attempt_count,
    _assignment_input_fingerprint,
    _assignment_role_input_refs,
    _architect_authoring_locations,
    _contract_submit_idempotency_key,
    _bind_architecture_edit_instruction_for_review,
    _bind_role_attempt_sandbox,
    _candidate_tree_fingerprint,
    _durable_workspace_preparation,
    _refresh_ephemeral_role_reference_binds,
    _workspace_tooling_from_work_view,
    _named_json_output,
    _prepare_role_workspace_before_environment,
    _implementation_action_idempotency_key,
    _raise_if_workspace_held,
    _role_uses_bound_durable_workspace,
    _semantic_role_input_refs,
    _skeleton_architecture_review_view,
    _stable_architecture_preflight_finding,
    _contract_architect_instruction,
    _recorded_role_metrics,
    _worker_event_timing,
    _workflow_skill_injections,
    apply_v2_revision_scope_capability_policy,
    apply_v2_role_capability_policy,
)
from pal.minion.v2.role_protocol import RoleAssignmentRequest, RoleAssignmentState
from pal.minion.v2.role_protocol import stable_hash
from pal.minion.v2.skeleton import ArchitectureWorkspace, architecture_revision_scope
from pal.minion.v2.work_items import UPDATE_CHECKLIST_CAPABILITY
from pal.minion.v2.submission_drafts import (
    SubmissionDraftContext,
    SubmissionDraftStore,
)
from pal.minion.v2 import ActionEnvelope, AggregateType
from pal.minion.v2.contracts import (
    AggregateSnapshot,
    DeferredEffectError,
    StaleFencingToken,
    SubmissionInvariantError,
)
from pal.minion.manager import MinionManager, MinionRunState
from pal.minion.prompt_adapter import render_minion_task_prompt
from pal.minion.runner import MinionAgentLoopState, MinionRunner, MinionRuntimeBundle
from pal.memory import (
    L1MessageKind,
    L1TranscriptMessage,
    MemoryCommitRequest,
    MemoryService,
)
from pal.llm.ir import LLMMessageIR, MessageRole
from pal.shared import (
    EventKind,
    IntrospectionCall,
    LLMFinishReason,
    MinionInvocationPack,
    RuntimeStatus,
)


ARCHITECT_BUILDER_CAPABILITIES = (
    UPDATE_CHECKLIST_CAPABILITY,
    CONTRACT_SUBMIT_CAPABILITY,
    ASK_QUESTION_CAPABILITY,
)
ARCHITECTURE_CHECKLIST_STEPS = (
    "requirements design",
    "declarations",
    "contract projection",
)


class _NoopSemanticEffects:
    async def execute_semantic_effect(self, effect):
        _ = effect
        return {}


class MinionV2WorkerIdentityTests(unittest.TestCase):
    def test_workspace_holder_check_ignores_only_the_manager_snapshot_lock(self) -> None:
        workspace = Path(tempfile.mkdtemp(prefix="pal-v2-holder-filter-"))
        self.addCleanup(shutil.rmtree, workspace, True)
        lock_path = workspace / ".git" / "pal-minion-v2.snapshot.lock"
        lock_path.parent.mkdir()
        lock_path.touch()
        manager_lock = WorkspaceProcessHolder(
            pid=os.getpid(),
            process_group=os.getpgrp(),
            command="python",
            write_paths=(".git/pal-minion-v2.snapshot.lock",),
        )

        with patch(
            "pal.minion.v2.semantic_orchestration.orchestrator.workspace_process_holders",
            return_value=(manager_lock,),
        ):
            _raise_if_workspace_held(
                workspace,
                "workspace is held",
                manager_snapshot_lock=lock_path,
            )

        unexpected_holders = {
            "same process also holds a source file": WorkspaceProcessHolder(
                pid=os.getpid(),
                process_group=os.getpgrp(),
                command="python",
                write_paths=(
                    ".git/pal-minion-v2.snapshot.lock",
                    "src/module.cpp",
                ),
            ),
            "another process holds the lock": WorkspaceProcessHolder(
                pid=os.getpid() + 1,
                process_group=os.getpgrp(),
                command="python",
                write_paths=(".git/pal-minion-v2.snapshot.lock",),
            ),
            "manager cwd remains in the workspace": WorkspaceProcessHolder(
                pid=os.getpid(),
                process_group=os.getpgrp(),
                command="python",
                holds_cwd=True,
                write_paths=(".git/pal-minion-v2.snapshot.lock",),
            ),
        }
        for label, holder in unexpected_holders.items():
            with self.subTest(label=label), patch(
                "pal.minion.v2.semantic_orchestration.orchestrator.workspace_process_holders",
                return_value=(holder,),
            ):
                with self.assertRaisesRegex(RuntimeError, "workspace is held"):
                    _raise_if_workspace_held(
                        workspace,
                        "workspace is held",
                        manager_snapshot_lock=lock_path,
                    )

    def test_durable_receipt_replay_does_not_record_a_fake_role_turn(self) -> None:
        worker = SemanticOrchestrator(MinionV2WorkflowService(Path(tempfile.mkdtemp())))
        recorded: list[dict[str, object]] = []
        worker.repository.record_role_turn = lambda **kwargs: recorded.append(kwargs)

        worker._record_role_turn(
            terminal={"payload": {"durable_receipt_replay": True}},
            invocation_id="inv-replay",
            fencing_token=2,
            turn_index=0,
            llm_request_ref={},
            llm_response_ref={},
        )

        self.assertEqual(recorded, [])

    def test_fresh_durable_receipt_preserves_billable_role_turn(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="pal-v2-fresh-receipt-"))
        self.addCleanup(shutil.rmtree, root, True)
        worker = SemanticOrchestrator(MinionV2WorkflowService(root))
        submitted = {"status": "candidate_ready", "summary": "done"}
        ref = worker.service.artifacts.put_json(
            submitted,
            artifact_type="RoleSubmissionArtifact",
        )

        terminal = worker._terminal_from_assignment_receipt(
            {
                "assignment_id": "assignment-fresh-receipt",
                "submission_artifact_ref": ref.to_dict(),
                "submission_payload_hash": stable_hash(submitted),
            },
            primary_artifact_name="coder_report.json",
            summary="done",
            original_terminal={
                "payload": {
                    "session_turn_index": 3,
                    "v2_timing": {"input_tokens": 7},
                }
            },
        )

        self.assertFalse(terminal["payload"]["durable_receipt_replay"])
        self.assertEqual(terminal["payload"]["session_turn_index"], 3)
        replay = worker._terminal_from_assignment_receipt(
            {
                "assignment_id": "assignment-fresh-receipt",
                "submission_artifact_ref": ref.to_dict(),
                "submission_payload_hash": stable_hash(submitted),
            },
            primary_artifact_name="coder_report.json",
            summary="recovered",
        )
        self.assertTrue(replay["payload"]["durable_receipt_replay"])


    def test_worker_metrics_include_provider_usage(self) -> None:
        timing = _worker_event_timing(
            [
                {
                    "event_kind": "progress",
                    "created_at": "2026-07-24T10:00:00+00:00",
                    "payload": {"phase": "llm_round_started", "round": 1},
                },
                {
                    "event_kind": "progress",
                    "created_at": "2026-07-24T10:00:02+00:00",
                    "payload": {
                        "phase": "llm_round_completed",
                        "round": 1,
                        "input_tokens": 101,
                        "output_tokens": 29,
                        "cost": 0.04,
                    },
                },
            ]
        )

        metrics = _recorded_role_metrics({"payload": {"v2_timing": timing}})

        self.assertEqual(metrics["input_tokens"], 101)
        self.assertEqual(metrics["output_tokens"], 29)
        self.assertEqual(metrics["cost"], 0.04)
        self.assertEqual(metrics["latency_ms"], 2000)

    def test_canonical_workspace_binding_is_explicit_for_every_durable_role(self) -> None:
        canonical = {
            "repo_path": "/tmp/node-worktree",
            "workspace_binding": "canonical",
        }
        ephemeral = {
            "repo_path": "/tmp/review-worktree",
            "workspace_binding": "ephemeral_artifact",
        }

        for role in ("architect", "implementation", "reviewer", "verifier"):
            self.assertTrue(
                _role_uses_bound_durable_workspace(role, canonical),
                role,
            )
            self.assertFalse(
                _role_uses_bound_durable_workspace(role, ephemeral),
                role,
            )

    def test_read_only_role_workspace_is_prepared_before_lsp_environment(self) -> None:
        runtime_root = Path(tempfile.mkdtemp(prefix="pal-v2-lsp-root-"))
        self.addCleanup(shutil.rmtree, runtime_root, True)
        source = runtime_root / "candidate-review"
        source.mkdir()
        (source / "module.cpp").write_text("int value() { return 1; }\n", encoding="utf-8")

        workspace, uses_bound = _prepare_role_workspace_before_environment(
            runtime_root,
            {"repo_path": str(source), "primary_language": "cpp"},
            role="verifier",
            invocation_id="inv-lsp-root",
            run_id="run-lsp-root",
            fencing_token=4,
            prepare_workspace=True,
        )

        self.assertFalse(uses_bound)
        self.assertTrue(workspace["v2_role_workspace"])
        self.assertNotEqual(Path(workspace["repo_path"]), source)
        self.assertEqual(
            Path(workspace["repo_path"]),
            runtime_root
            / "data"
            / "minion"
            / "runtime"
            / "role-workspaces"
            / "run-lsp-root"
            / "attempts"
            / "fence-4",
        )
        self.assertTrue((Path(workspace["repo_path"]) / "module.cpp").is_file())
        prepared, report = prepare_v2_workspace_environment(
            workspace,
            runtime_root=runtime_root,
        )
        role_root = Path(workspace["repo_path"]).resolve()
        self.assertEqual(Path(report["workspace_root"]), role_root)
        self.assertEqual(
            Path(report["build_scratch_root"]),
            Path(workspace["build_scratch_dir"]),
        )
        self.assertTrue(Path(report["build_scratch_root"]).is_dir())
        self.assertEqual(prepared["primary_language"], "cpp")
        self.assertNotIn("lsp_setup", prepared)
        self.assertNotIn("lsp_setup", report)

    def test_semantic_worker_inputs_exclude_ephemeral_workspace_preparation(self) -> None:
        self.assertEqual(
            _semantic_role_input_refs(
                {
                    "requirements": {"sha256": "requirements-sha"},
                    "workspace_preparation": {"sha256": "temporary-worktree-sha"},
                }
            ),
            {"requirements": {"sha256": "requirements-sha"}},
        )
        self.assertEqual(
            _semantic_role_input_refs(
                {
                    "candidate_diff": {"sha256": "candidate-sha"},
                    "workspace_preparation": {"sha256": "verification-environment-sha"},
                },
                role="verifier",
            ),
            {
                "candidate_diff": {"sha256": "candidate-sha"},
            },
        )
        self.assertEqual(
            _semantic_role_input_refs(
                {
                    "architecture_index": {"sha256": "architecture-sha"},
                    "workspace_preparation": {"sha256": "review-environment-sha"},
                },
                role="reviewer",
                mode="architecture",
            ),
            {"architecture_index": {"sha256": "architecture-sha"}},
        )
        self.assertEqual(
            _assignment_role_input_refs(
                {
                    "requirements": {"sha256": "requirements-sha"},
                    "workspace_preparation": {"sha256": "attempt-worktree-sha"},
                }
            ),
            {
                "requirements": {"sha256": "requirements-sha"},
                "workspace_preparation": {"sha256": "attempt-worktree-sha"},
            },
        )

    def test_workspace_preparation_artifact_omits_lsp_observation_noise(self) -> None:
        stable = {
            "workspace_root": "/workspace/module",
            "environment_fingerprint": "workspace-environment",
            "lsp_workspace_preparation": {
                "status": "ok",
                "environment_fingerprint": "lsp-environment",
                "workspace_root": "/workspace/module",
                "prepared_at": "2026-07-17T10:02:58+00:00",
                "environment_changed": True,
            },
        }
        replay = deepcopy(stable)
        replay["lsp_workspace_preparation"]["prepared_at"] = "2026-07-17T10:30:04+00:00"
        replay["lsp_workspace_preparation"]["environment_changed"] = False

        first = _durable_workspace_preparation(stable)
        second = _durable_workspace_preparation(replay)

        self.assertEqual(first, second)
        self.assertNotIn("prepared_at", first["lsp_workspace_preparation"])
        self.assertNotIn("environment_changed", first["lsp_workspace_preparation"])
        self.assertIn("prepared_at", stable["lsp_workspace_preparation"])

    def test_candidate_tree_fingerprint_uses_content_tree_not_commit(self) -> None:
        self.assertEqual(
            _candidate_tree_fingerprint(
                {
                    "candidate_digest": "commit-sha",
                    "candidate_tree_sha": "tree-sha",
                },
                fallback="commit-sha",
            ),
            "tree-sha",
        )
        self.assertEqual(
            _candidate_tree_fingerprint(
                {"tree_fingerprint": "artifact-tree"},
                fallback="artifact-sha",
            ),
            "artifact-tree",
        )

    def test_named_json_output_uses_semantic_name_for_content_addressed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact_path = Path(temporary_directory) / "content-hash-without-extension"
            artifact_path.write_text('{"verdict":"PASS"}', encoding="utf-8")
            terminal = {
                "payload": {
                    "artifacts": [
                        {
                            "path": str(artifact_path),
                            "relative_path": "architecture_review.json",
                            "role": "primary",
                        }
                    ]
                }
            }

            self.assertEqual(
                _named_json_output(terminal, "architecture_review.json"),
                {"verdict": "PASS"},
            )

    def test_contract_submit_dedup_key_distinguishes_state_machine_cycles(self) -> None:
        first = _contract_submit_idempotency_key("arch-1", 7, "same-submission")
        replay = _contract_submit_idempotency_key("arch-1", 7, "same-submission")
        next_cycle = _contract_submit_idempotency_key("arch-1", 12, "same-submission")

        self.assertEqual(first, replay)
        self.assertNotEqual(first, next_cycle)

    def test_producer_dedup_key_distinguishes_candidate_cycles(self) -> None:
        first = _implementation_action_idempotency_key("submit", "node-1", 2, "report")
        replay = _implementation_action_idempotency_key("submit", "node-1", 2, "report")
        next_cycle = _implementation_action_idempotency_key("submit", "node-1", 3, "report")

        self.assertEqual(first, replay)
        self.assertNotEqual(first, next_cycle)

    def test_node_recovery_resumes_quiesce_and_snapshot_mechanical_steps(self) -> None:
        async def scenario() -> None:
            worker = SemanticOrchestrator(
                MinionV2WorkflowService(Path(tempfile.mkdtemp()))
            )
            state = {"value": "QUIESCING"}
            calls: list[str] = []
            worker._effect_snapshot = lambda _effect: SimpleNamespace(
                state=state["value"]
            )

            async def quiesce(_effect):
                calls.append("quiesce")
                return {"status": "quiesced"}

            async def snapshot(_effect):
                calls.append("snapshot")
                return {"status": "snapshotted"}

            worker._quiesce_node = quiesce
            worker._snapshot_implementation_result = snapshot

            self.assertEqual(
                (await worker._resume_node({}))["status"],
                "quiesced",
            )
            state["value"] = "SNAPSHOTTING"
            self.assertEqual(
                (await worker._resume_node({}))["status"],
                "snapshotted",
            )
            self.assertEqual(calls, ["quiesce", "snapshot"])

        asyncio.run(scenario())


class _ControlSemanticEffects:
    def __init__(self, service: MinionV2WorkflowService) -> None:
        self.service = service

    async def execute_semantic_effect(self, effect):
        if effect.get("effect_type") == "pause_role":
            aggregate_type = AggregateType(str(effect["aggregate_type"]))
            snapshot = self.service.repository.read_snapshot(aggregate_type, str(effect["aggregate_id"]))
            self.service.repository.dispatch(
                ActionEnvelope(
                    action_type="PAUSE_CONFIRMED",
                    workflow_id=snapshot.workflow_id,
                    aggregate_type=aggregate_type,
                    aggregate_id=snapshot.aggregate_id,
                    actor="test",
                    expected_version=snapshot.version,
                    idempotency_key=f"test:{effect['effect_key']}:pause",
                )
            )
        return {}


class _SlowSemanticEffects:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute_semantic_effect(self, effect):
        if effect.get("effect_type") == "admit_architect_role":
            self.started.set()
            await self.release.wait()
        return {}


class _PermanentFailureSemanticEffects:
    async def execute_semantic_effect(self, effect):
        raise SubmissionInvariantError(
            f"accepted submit disagreed with manager validation for {effect.get('effect_type')}"
        )


class _DeferredSemanticEffects:
    def __init__(self) -> None:
        self.calls = 0

    async def execute_semantic_effect(self, effect):
        self.calls += 1
        raise DeferredEffectError(f"deferred {effect.get('effect_type')} for manager restart")


class _FakeRuntimeBundle:
    async def close(self) -> None:
        return None


async def _noop_write_event(_event) -> None:
    return None


async def _noop_read_decision(_timeout=None):
    return None


class _SingleInvocationRunner(MinionRunner):
    async def _run_agent_loop(self, bundle, *, forced_retry_note: str = "") -> str:
        _ = bundle, forced_retry_note
        return "done"


class _CompletionGateRunner(MinionRunner):
    retry_notes: list[str]

    async def _run_agent_loop(self, bundle, *, forced_retry_note: str = "") -> str:
        _ = bundle
        if not hasattr(self, "retry_notes"):
            self.retry_notes = []
        self.retry_notes.append(forced_retry_note)
        if len(self.retry_notes) == 2:
            self.produced_artifacts.append(
                {
                    "role": "primary",
                    "relative_path": "coder_report.json",
                    "path": str(self.runtime_root / "coder_report.json"),
                }
            )
        return "done"


class _StalledCompletionGateRunner(MinionRunner):
    agent_calls: int = 0

    async def _run_agent_loop(self, bundle, *, forced_retry_note: str = "") -> str:
        _ = bundle, forced_retry_note
        self.agent_calls += 1
        return "already completed"


class MinionV2PublicSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal_minion_v2_public_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.runtime_root, ignore_errors=True)

    def test_profile_override_translates_public_endpoint_name_at_manager_boundary(self) -> None:
        captured: dict[str, object] = {}

        def manager_request(method: str, params: dict[str, object] | None = None) -> dict[str, object]:
            captured["method"] = method
            captured["params"] = dict(params or {})
            return {"updated": True}

        provider = MinionV2PublicProvider(
            runtime_root=self.runtime_root,
            manager_request=manager_request,
        )
        result = provider.set_profile_override(
            CapabilityCall(
                name="op_minion_catalog_set_profile_override",
                meta={"actor_id": "nathan"},
                args={
                    "profile": "software_engineering.v2_coder",
                    "changes": {"preferred_endpoint_name": "fast-coder"},
                },
            )
        )

        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertEqual(captured["method"], "catalog_set_profile_override")
        params = captured["params"]
        self.assertIsInstance(params, dict)
        self.assertEqual(
            params["changes"],
            {"preferred_endpoint_id": "fast-coder"},
        )

    def test_preflight_finding_preserves_exact_authoring_locations(self) -> None:
        path = self.runtime_root / "architect.yaml"
        path.write_text(
            "schema_version: '1'\n"
            "context: {}\n"
            "requirements:\n"
            "  e2e_stdio_verification:\n"
            "    claim: observable\n"
            "modules:\n"
            "  frame_protocol: {}\n"
            "  framepipe_cli: {}\n"
            "scenarios:\n"
            "  e2e_stdio_verification:\n"
            "    modules: [framepipe_cli, frame_protocol]\n",
            encoding="utf-8",
        )
        submission = {
            "requirements": {
                "e2e_stdio_verification": {
                    "owner": "framepipe_cli",
                }
            },
            "modules": {
                "frame_protocol": {"paths": {}},
                "framepipe_cli": {"paths": {}},
            },
            "scenarios": {
                "e2e_stdio_verification": {
                    "modules": ["framepipe_cli", "frame_protocol"],
                }
            },
        }
        locations = _architect_authoring_locations(path, submission)

        finding = _stable_architecture_preflight_finding(
            ValueError(
                "requirement names must not conflict with module or scenario "
                "names: e2e_stdio_verification"
            ),
            contract_intent={"authoring_locations": locations},
            submission=submission,
        )

        self.assertEqual(
            [item["symbol"] for item in finding["locations"]],
            [
                "requirements.e2e_stdio_verification",
                "scenarios.e2e_stdio_verification",
            ],
        )
        self.assertEqual(
            [item["line"] for item in finding["locations"]],
            [4, 10],
        )
        self.assertEqual(
            finding["affected_modules"],
            ["frame_protocol", "framepipe_cli"],
        )
        scope = architecture_revision_scope(submission, finding)
        self.assertEqual(
            scope["affected_modules"],
            ["frame_protocol", "framepipe_cli"],
        )
        self.assertEqual(
            scope["allowed_paths"],
            [".pal-minion-architect/architect.yaml"],
        )

    def test_execution_epoch_identity_is_semantic_not_effect_scoped(self) -> None:
        first = _execution_epoch_id(
            workflow_id="workflow",
            manifest_sha="manifest",
        )
        replay = _execution_epoch_id(
            workflow_id="workflow",
            manifest_sha="manifest",
        )
        replacement = _execution_epoch_id(
            workflow_id="workflow",
            manifest_sha="manifest",
            source_epoch_id="prior-epoch",
        )
        repair = _execution_epoch_id(
            workflow_id="workflow",
            manifest_sha="manifest",
            repair_bill_sha="repair-bill",
        )

        self.assertEqual(first, replay)
        self.assertNotEqual(first, replacement)
        self.assertNotEqual(first, repair)

    def _create_role_scope(
        self,
        service: MinionV2WorkflowService,
        *,
        workflow_id: str,
        aggregate_type: AggregateType,
        aggregate_id: str,
        module_name: str = "router",
    ) -> None:
        service.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_WORKFLOW",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="test",
                expected_version=0,
            )
        )
        service.repository.dispatch(
            ActionEnvelope(
                action_type=(
                    "CREATE_NODE_RUN"
                    if aggregate_type == AggregateType.DAG_NODE_RUN
                    else "CREATE_ARCHITECTURE_REVISION"
                ),
                workflow_id=workflow_id,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                actor="test",
                expected_version=0,
                payload=(
                    {
                        "epoch_id": f"epoch-{aggregate_id}",
                        "module_name": module_name,
                        "unit_contract_ref": {"sha256": f"contract-{aggregate_id}"},
                    }
                    if aggregate_type == AggregateType.DAG_NODE_RUN
                    else {"architecture_cycle_id": aggregate_id}
                ),
            )
        )

    def test_retry_reuses_durable_prompt_reference_binds(self) -> None:
        repo = self.runtime_root / "repo"
        task = self.runtime_root / "task"
        first_preparation = self.runtime_root / "first-preparation"
        retry_preparation = self.runtime_root / "retry-preparation"
        repo.mkdir()
        task.mkdir()
        first_preparation.mkdir()
        retry_preparation.mkdir()
        (task / "task.yaml").write_text("immutable task\n", encoding="utf-8")
        (first_preparation / "workspace_preparation.json").write_text(
            "{\"fence\": 1}\n", encoding="utf-8"
        )
        (retry_preparation / "workspace_preparation.json").write_text(
            "{\"fence\": 2}\n", encoding="utf-8"
        )
        fresh = MinionInvocationPack(
            invocation_id="inv_retry_projection",
            workspace={
                "repo_path": str(repo),
                "reference_paths": [
                    {
                        "name": "task",
                        "path": str(task),
                        "truth_source": True,
                        "required": True,
                    },
                    {
                        "name": "workspace_preparation",
                        "path": str(first_preparation),
                        "truth_source": True,
                        "required": True,
                    }
                ],
            },
            metadata={"manager_only": "discard me"},
        )

        first_attempt = _bind_role_attempt_sandbox(
            self.runtime_root,
            fresh,
            run_id="run_retry_projection",
            durable_prompt_reused=False,
        )
        current_attempt = MinionInvocationPack.from_dict(
            {
                **fresh.to_dict(),
                "workspace": {
                    **fresh.workspace,
                    "reference_paths": [
                        fresh.workspace["reference_paths"][0],
                        {
                            **fresh.workspace["reference_paths"][1],
                            "path": str(retry_preparation),
                        },
                    ],
                },
            }
        )
        refreshed = _refresh_ephemeral_role_reference_binds(
            first_attempt,
            current_attempt,
        )
        retry_attempt = _bind_role_attempt_sandbox(
            self.runtime_root,
            refreshed,
            run_id="run_retry_projection",
            durable_prompt_reused=True,
        )

        self.assertEqual(
            retry_attempt.workspace["reference_paths"][0]["path"],
            "/pal/references/task",
        )
        self.assertEqual(
            retry_attempt.metadata["sandbox"]["reference_binds"][0]["source_path"],
            str(task),
        )
        self.assertEqual(
            retry_attempt.metadata["sandbox"]["reference_binds"][1]["source_path"],
            str(retry_preparation),
        )
        self.assertEqual(
            retry_attempt.workspace["reference_paths"][1]["path"],
            "/pal/references/workspace_preparation",
        )
        role_socket = self.runtime_root / "data" / "minion-role" / "role.sock"
        role_socket.parent.mkdir(parents=True, exist_ok=True)
        role_socket.write_text("test endpoint", encoding="utf-8")
        argv, _ = build_sandboxed_runner_invocation(
            runtime_root=self.runtime_root,
            pack=retry_attempt,
            argv=["/bin/true"],
        )
        self.assertIn(str(task), argv)
        self.assertIn(str(retry_preparation), argv)
        self.assertNotIn(str(first_preparation), argv)

    def test_workspace_tooling_uses_explicit_accepted_language_context(self) -> None:
        self.assertEqual(
            _workspace_tooling_from_work_view(
                {"context": {"language": "C++20"}}
            ),
            {
                "primary_language": "cpp",
                "languages": ["cpp"],
                "cpp_standard": "c++20",
            },
        )
        self.assertEqual(
            _workspace_tooling_from_work_view(
                {"context": {"cpp_standard": "c++23"}}
            ),
            {"cpp_standard": "c++23"},
        )
        self.assertEqual(
            _workspace_tooling_from_work_view(
                {"context": {"language": "C++"}}
            ),
            {"primary_language": "cpp", "languages": ["cpp"]},
        )
        self.assertEqual(
            _workspace_tooling_from_work_view(
                {"context": {"language": "Python 3.12"}}
            ),
            {"primary_language": "python", "languages": ["python"]},
        )
        self.assertEqual(
            _workspace_tooling_from_work_view(
                {
                    "context": {
                        "language": "TypeScript 5.6",
                        "languages": ["JavaScript", "CSS"],
                    }
                }
            ),
            {
                "primary_language": "typescript",
                "languages": ["typescript", "javascript", "css"],
            },
        )
        self.assertEqual(
            _workspace_tooling_from_work_view(
                {"context": {"language": "Rust 2024 edition"}}
            ),
            {"primary_language": "rust", "languages": ["rust"]},
        )
        self.assertEqual(
            _workspace_tooling_from_work_view(
                {"context": {"language": "C17"}}
            ),
            {
                "primary_language": "c",
                "languages": ["c"],
                "cpp_standard": "c17",
            },
        )
        self.assertEqual(
            _workspace_tooling_from_work_view(
                {"context": {"language": "Kotlin 2.0"}}
            ),
            {},
        )
        for declared, canonical in (
            ("Go 1.24", "go"),
            ("Java 21 LTS", "java"),
            ("C# 12", "csharp"),
            ("Lua 5.4", "lua"),
            ("Bash 5", "shell"),
            ("JSON", "json"),
            ("YAML", "yaml"),
        ):
            with self.subTest(declared=declared):
                self.assertEqual(
                    _workspace_tooling_from_work_view(
                        {"context": {"language": declared}}
                    ),
                    {
                        "primary_language": canonical,
                        "languages": [canonical],
                    },
                )
        self.assertEqual(
            _workspace_tooling_from_work_view(
                {"context": {"primary_language": "cpp"}}
            ),
            {"primary_language": "cpp", "languages": ["cpp"]},
        )

    def test_background_effect_returns_after_durable_assignment_is_ready(self) -> None:
        async def scenario() -> None:
            worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
            release = asyncio.Event()
            effect = {
                "effect_id": "effect-background",
                "effect_key": "effect-key-background",
            }

            async def runner(value):
                worker._signal_assignment_ready(value, "assignment-background")
                await release.wait()
                return {"status": "completed"}

            result = await worker._launch_background_worker(effect, runner)

            self.assertEqual(result["status"], "assignment_started")
            self.assertEqual(result["provider_request_id"], "assignment-background")
            self.assertEqual(worker.active_background_count, 1)
            release.set()
            await asyncio.gather(*tuple(worker._background_workers.values()))
            await asyncio.sleep(0)
            self.assertEqual(worker.active_background_count, 0)

        asyncio.run(scenario())

    def test_reusable_assignment_matches_semantic_inputs_across_worktrees(self) -> None:
        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        worker.repository.list_role_assignments = lambda **_kwargs: (
            {
                "assignment_id": "accepted-review-assignment",
                "workflow_id": "workflow-review",
                "aggregate_type": AggregateType.ARCHITECTURE_REVISION.value,
                "aggregate_id": "architecture-review",
                "role": "reviewer",
                "mode": "architecture",
                "submission_kind": "architecture_review",
                "state": "settled",
                "submission_artifact_ref": {
                    "artifact_type": "ArchitectureReviewRoleSubmissionArtifact",
                    "sha256": "review-sha",
                },
                "input_refs": {
                    "requirements": {"sha256": "requirements-sha"},
                    "workspace_preparation": {"sha256": "old-worktree-sha"},
                },
            },
        )

        reusable = worker._reusable_role_assignment(
            workflow_id="workflow-review",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION.value,
            aggregate_id="architecture-review",
            role="reviewer",
            mode="architecture",
            submission_kind="architecture_review",
            input_refs={
                "requirements": {"sha256": "requirements-sha"},
                "workspace_preparation": {"sha256": "new-worktree-sha"},
            },
        )

        self.assertIsNotNone(reusable)
        self.assertEqual(reusable["assignment_id"], "accepted-review-assignment")

    def test_candidate_receipt_reuse_requires_the_exact_work_view(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        worker = SemanticOrchestrator(service)
        submitted_view = service.artifacts.put_json(
            {
                "schema_version": "2",
                "module_name": "router",
                "module": {"responsibility": "Route requests deterministically."},
                "requirements": {"routing": {"owner": "router"}},
            },
            artifact_type="ModuleWorkViewArtifact",
        )
        worker.repository.list_role_assignments = lambda **_kwargs: (
            {
                "assignment_id": "submitted-candidate-assignment",
                "workflow_id": "workflow-router",
                "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                "aggregate_id": "node-router",
                "role": "implementation",
                "mode": "produce",
                "submission_kind": "candidate",
                "state": RoleAssignmentState.SETTLED.value,
                "submission_artifact_ref": {
                    "artifact_type": "CandidateRoleSubmissionArtifact",
                    "sha256": "candidate-submission",
                },
                "input_refs": {"unit_work_view": submitted_view.to_dict()},
                "execution_spec": {"evaluation_generation": 0},
            },
        )

        reusable = worker._reusable_role_assignment(
            workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN.value,
            aggregate_id="node-router",
            role="implementation",
            mode="produce",
            submission_kind="candidate",
            input_refs={"unit_work_view": submitted_view.to_dict()},
        )

        self.assertIsNotNone(reusable)
        self.assertEqual(
            reusable["assignment_id"],
            "submitted-candidate-assignment",
        )

    def test_candidate_receipt_reuse_rejects_semantic_work_view_drift(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        worker = SemanticOrchestrator(service)
        submitted_view = service.artifacts.put_json(
            {
                "schema_version": "2",
                "module_name": "router",
                "module": {"responsibility": "Route requests deterministically."},
            },
            artifact_type="ModuleWorkViewArtifact",
        )
        changed_view = service.artifacts.put_json(
            {
                "schema_version": "2",
                "module_name": "router",
                "module": {"responsibility": "Route requests with retry semantics."},
            },
            artifact_type="ModuleWorkViewArtifact",
        )
        worker.repository.list_role_assignments = lambda **_kwargs: (
            {
                "assignment_id": "stale-candidate-assignment",
                "workflow_id": "workflow-router",
                "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                "aggregate_id": "node-router",
                "role": "implementation",
                "mode": "produce",
                "submission_kind": "candidate",
                "state": RoleAssignmentState.SETTLED.value,
                "submission_artifact_ref": {
                    "artifact_type": "CandidateRoleSubmissionArtifact",
                    "sha256": "candidate-submission",
                },
                "input_refs": {"unit_work_view": submitted_view.to_dict()},
                "execution_spec": {"evaluation_generation": 0},
            },
        )

        reusable = worker._reusable_role_assignment(
            workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN.value,
            aggregate_id="node-router",
            role="implementation",
            mode="produce",
            submission_kind="candidate",
            input_refs={"unit_work_view": changed_view.to_dict()},
        )

        self.assertIsNone(reusable)

    def test_durable_submission_reuses_the_attempt_prompt_workspace_binding(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        worker = SemanticOrchestrator(service)
        prompt_ref = service.artifacts.put_json(
            {
                "workspace": {
                    "repo_path": "/tmp/original-verifier-workspace",
                    "v2_role_workspace": True,
                }
            },
            artifact_type="RolePromptPackArtifact",
        )
        worker.repository.read_role_attempt = lambda _attempt_id: {
            "prompt_pack_ref": prompt_ref.to_dict()
        }

        resolved = worker._durable_assignment_prompt_ref(
            {"active_attempt_id": "attempt-with-receipt"}
        )

        self.assertEqual(resolved, prompt_ref)

    def test_verifier_assignment_is_not_reused_across_verification_environments(self) -> None:
        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        worker.repository.list_role_assignments = lambda **_kwargs: (
            {
                "assignment_id": "old-verification-assignment",
                "workflow_id": "workflow-verification",
                "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                "aggregate_id": "drawing-node",
                "role": "verifier",
                "submission_kind": "verification",
                "state": "settled",
                "submission_artifact_ref": {
                    "artifact_type": "VerifierRoleSubmissionArtifact",
                    "sha256": "verification-sha",
                },
                "input_refs": {
                    "candidate_diff": {"sha256": "candidate-sha"},
                    "workspace_preparation": {"sha256": "old-environment-sha"},
                },
            },
        )

        reusable = worker._reusable_role_assignment(
            workflow_id="workflow-verification",
            aggregate_type=AggregateType.DAG_NODE_RUN.value,
            aggregate_id="drawing-node",
            role="verifier",
            mode="module",
            submission_kind="verification",
            input_refs={
                "candidate_diff": {"sha256": "candidate-sha"},
                "workspace_preparation": {"sha256": "new-environment-sha"},
            },
        )

        self.assertIsNone(reusable)

    def test_architecture_reviewer_assignment_is_not_reused_across_review_generations(self) -> None:
        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        worker.repository.list_role_assignments = lambda **_kwargs: (
            {
                "assignment_id": "old-review-generation",
                "workflow_id": "workflow-review-generation",
                "aggregate_type": AggregateType.ARCHITECTURE_REVISION.value,
                "aggregate_id": "architecture-review-generation",
                "role": "reviewer",
                "mode": "architecture",
                "submission_kind": "architecture_review",
                "state": "settled",
                "submission_artifact_ref": {
                    "artifact_type": "ArchitectureReviewRoleSubmissionArtifact",
                    "sha256": "review-sha",
                },
                "input_refs": {"requirements": {"sha256": "requirements-sha"}},
                "execution_spec": {"evaluation_generation": 0},
            },
        )

        reusable = worker._reusable_role_assignment(
            workflow_id="workflow-review-generation",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION.value,
            aggregate_id="architecture-review-generation",
            role="reviewer",
            mode="architecture",
            submission_kind="architecture_review",
            input_refs={"requirements": {"sha256": "requirements-sha"}},
            evaluation_generation=1,
        )

        self.assertIsNone(reusable)

    def test_failure_artifact_is_never_reused_as_role_submission(self) -> None:
        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        worker.repository.list_role_assignments = lambda **_kwargs: (
            {
                "assignment_id": "failed-producer-assignment",
                "workflow_id": "workflow-producer",
                "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                "aggregate_id": "node-producer",
                "role": "implementation",
                "mode": "produce",
                "submission_kind": "candidate",
                "state": "settled",
                "submission_artifact_ref": {
                    "artifact_type": "RoleAssignmentFailureArtifact",
                    "sha256": "failure-sha",
                },
                "input_refs": {
                    "module_work_view": {"sha256": "module-sha"},
                },
            },
        )

        reusable = worker._reusable_role_assignment(
            workflow_id="workflow-producer",
            aggregate_type=AggregateType.DAG_NODE_RUN.value,
            aggregate_id="node-producer",
            role="implementation",
            mode="produce",
            submission_kind="candidate",
            input_refs={"module_work_view": {"sha256": "module-sha"}},
        )

        self.assertIsNone(reusable)

    def test_background_worker_supervisor_does_not_limit_logical_coroutines(self) -> None:
        async def scenario() -> None:
            worker = SemanticOrchestrator(
                MinionV2WorkflowService(self.runtime_root),
                max_parallel_workers=1,
            )
            release = asyncio.Event()
            first_effect = {
                "effect_id": "effect-slot-one",
                "effect_key": "effect-key-slot-one",
            }

            async def first_runner(value):
                worker._signal_assignment_ready(value, "assignment-slot-one")
                await release.wait()
                return {"status": "completed"}

            await worker._launch_background_worker(first_effect, first_runner)
            await worker._launch_background_worker(
                {
                    "effect_id": "effect-slot-two",
                    "effect_key": "effect-key-slot-two",
                },
                first_runner,
            )
            self.assertEqual(worker.active_background_count, 2)
            self.assertEqual(worker.active_process_count, 0)
            release.set()
            await asyncio.gather(*tuple(worker._background_workers.values()))

        asyncio.run(scenario())

    def test_stopping_before_assignment_keeps_effect_deferred(self) -> None:
        async def scenario() -> None:
            worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
            worker.request_stop()

            async def runner(_effect):
                self.fail("runner must not start while the supervisor is stopping")

            with self.assertRaisesRegex(DeferredEffectError, "durable assignment"):
                await worker._background_worker_loop(
                    {
                        "effect_id": "effect-before-assignment",
                        "effect_key": "effect-key-before-assignment",
                    },
                    runner,
                )

        asyncio.run(scenario())

    def test_post_settlement_telemetry_failure_does_not_reopen_business_work(self) -> None:
        async def scenario() -> None:
            worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
            worker._assignment_ids_by_effect["effect-key-settled"] = "assignment-settled"
            worker.repository.read_role_assignment = lambda _assignment_id: {
                "assignment_id": "assignment-settled",
                "state": "settled",
            }

            async def failed_after_settlement(_effect):
                raise RuntimeError("metrics sink unavailable")

            result = await worker._background_worker_loop(
                {
                    "effect_id": "effect-settled",
                    "effect_key": "effect-key-settled",
                },
                failed_after_settlement,
            )

            self.assertEqual(result["status"], "settled")

        asyncio.run(scenario())

    def test_settled_receipt_is_replayed_while_aggregate_awaits_business_action(self) -> None:
        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        worker.repository.read_snapshot = lambda _aggregate_type, _aggregate_id: (
            SimpleNamespace(state="PRODUCING")
        )
        worker.repository.list_role_assignments = lambda **_kwargs: ()

        disposition = worker._role_assignment_disposition(
            {"effect_type": "run_implementation_role"},
            {
                "assignment_id": "assignment-settled",
                "state": "settled",
                "workflow_id": "workflow-reconcile",
                "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                "aggregate_id": "node-reconcile",
                "role": "implementation",
                "mode": "produce",
                "submission_kind": "candidate",
                "input_refs": {},
            },
        )

        self.assertEqual(disposition, "")

    def test_settled_receipt_reconciliation_retries_without_reinvoking_worker(self) -> None:
        async def scenario() -> None:
            worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
            effect = {
                "effect_id": "effect-settled-reconcile",
                "effect_key": "effect-key-settled-reconcile",
                "effect_type": "run_implementation_role",
            }
            worker._assignment_ids_by_effect[effect["effect_key"]] = "assignment-settled"
            worker.repository.read_role_assignment = lambda _assignment_id: {
                "assignment_id": "assignment-settled",
                "state": "settled",
                "workflow_id": "workflow-reconcile",
                "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                "aggregate_id": "node-reconcile",
                "role": "implementation",
                "mode": "produce",
                "submission_kind": "candidate",
                "input_refs": {},
            }
            worker.repository.read_snapshot = lambda _aggregate_type, _aggregate_id: (
                SimpleNamespace(state="PRODUCING")
            )
            worker.repository.list_role_assignments = lambda **_kwargs: ()
            worker.repository.list_role_attempts = lambda _assignment_id: []
            calls = 0

            async def runner(_effect):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("business action lost a CAS race")
                return {"status": "completed"}

            async def no_wait(_seconds):
                return None

            with patch("pal.minion.v2.semantic_orchestration.orchestrator.asyncio.sleep", new=no_wait):
                result = await worker._background_worker_loop(effect, runner)

            self.assertEqual(calls, 2)
            self.assertEqual(result["status"], "completed")

        asyncio.run(scenario())

    def test_exhausted_settled_receipt_reconciliation_routes_failure(self) -> None:
        async def scenario() -> None:
            worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
            effect = {
                "effect_id": "effect-settled-failure",
                "effect_key": "effect-key-settled-failure",
                "effect_type": "run_implementation_role",
            }
            worker._assignment_ids_by_effect[effect["effect_key"]] = "assignment-settled"
            assignment = {
                "assignment_id": "assignment-settled",
                "state": "settled",
                "workflow_id": "workflow-reconcile",
                "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                "aggregate_id": "node-reconcile",
                "role": "implementation",
                "mode": "produce",
                "submission_kind": "candidate",
                "input_refs": {},
            }
            worker.repository.read_role_assignment = lambda _assignment_id: dict(assignment)
            worker.repository.read_snapshot = lambda _aggregate_type, _aggregate_id: (
                SimpleNamespace(state="PRODUCING")
            )
            worker.repository.list_role_assignments = lambda **_kwargs: ()
            worker.repository.list_role_attempts = lambda _assignment_id: []
            routed: list[dict[str, object]] = []
            worker._settle_background_role_failure = (
                lambda received_effect, received_assignment, error, *, exhausted: (
                    routed.append(
                        {
                            "effect": received_effect,
                            "assignment": received_assignment,
                            "error": error,
                            "exhausted": exhausted,
                        }
                    )
                    or {"status": "triage_required"}
                )
            )
            calls = 0

            async def runner(_effect):
                nonlocal calls
                calls += 1
                raise RuntimeError("business action cannot be reconciled")

            async def no_wait(_seconds):
                return None

            with patch("pal.minion.v2.semantic_orchestration.orchestrator.asyncio.sleep", new=no_wait):
                result = await worker._background_worker_loop(effect, runner)

            self.assertEqual(calls, 3)
            self.assertEqual(result["status"], "triage_required")
            self.assertEqual(len(routed), 1)
            self.assertTrue(routed[0]["exhausted"])

        asyncio.run(scenario())

    def test_sqlite_lock_after_durable_receipt_defers_reconciliation_without_triage(self) -> None:
        async def scenario() -> None:
            worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
            effect = {
                "effect_id": "effect-durable-lock",
                "effect_key": "effect-key-durable-lock",
                "effect_type": "run_reviewer_role",
                "role_mode": "architecture",
            }
            worker._assignment_ids_by_effect[effect["effect_key"]] = (
                "assignment-durable-lock"
            )
            assignment = {
                "assignment_id": "assignment-durable-lock",
                "state": RoleAssignmentState.RESULT_RECORDED.value,
                "workflow_id": "workflow-durable-lock",
                "aggregate_type": AggregateType.ARCHITECTURE_REVISION.value,
                "aggregate_id": "architecture-durable-lock",
                "role": "reviewer",
                "mode": "architecture",
                "submission_kind": "contract_review",
                "input_refs": {},
                "submission_artifact_ref": {
                    "artifact_type": "ArchitectureReviewRoleSubmissionArtifact",
                    "sha256": "a" * 64,
                },
                "submission_payload_hash": "b" * 64,
            }
            worker.repository.read_role_assignment = lambda _assignment_id: dict(
                assignment
            )
            worker.repository.read_snapshot = lambda _aggregate_type, _aggregate_id: (
                SimpleNamespace(state="REVIEWING")
            )
            worker.repository.list_role_assignments = lambda **_kwargs: ()
            worker.repository.list_role_attempts = lambda _assignment_id: [
                {"attempt_id": "attempt-durable-lock"}
            ]
            worker._settle_background_role_failure = lambda *_args, **_kwargs: self.fail(
                "a durable submission must not be converted into role failure"
            )
            calls = 0

            async def runner(_effect):
                nonlocal calls
                calls += 1
                raise sqlite3.OperationalError("database is locked")

            async def no_wait(_seconds):
                return None

            with patch(
                "pal.minion.v2.semantic_orchestration.orchestrator.asyncio.sleep",
                new=no_wait,
            ):
                result = await worker._background_worker_loop(effect, runner)

            self.assertEqual(calls, 3)
            self.assertEqual(result["status"], "reconciliation_deferred")
            self.assertEqual(
                result["provider_request_id"], "assignment-durable-lock"
            )

        asyncio.run(scenario())

    def test_recorded_submission_replays_business_action_before_triage(self) -> None:
        async def scenario() -> None:
            worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
            effect = {
                "effect_id": "effect-reconcile",
                "effect_key": "effect-key-reconcile",
            }
            worker._assignment_ids_by_effect["effect-key-reconcile"] = (
                "assignment-reconcile"
            )
            assignment_state = {"value": "result_recorded"}
            calls = 0

            worker.repository.read_role_assignment = lambda _assignment_id: {
                "assignment_id": "assignment-reconcile",
                "state": assignment_state["value"],
                "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                "aggregate_id": "node-reconcile",
            }
            worker.repository.list_role_attempts = lambda _assignment_id: [
                {"attempt_id": "attempt-reconcile"}
            ]
            worker.repository.read_snapshot = lambda _aggregate_type, _aggregate_id: (
                SimpleNamespace(state="PRODUCING")
            )

            async def runner(_effect):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("CAS conflict after recording submission")
                assignment_state["value"] = "settled"
                return {"status": "completed"}

            async def no_wait(_seconds):
                return None

            with patch("pal.minion.v2.semantic_orchestration.orchestrator.asyncio.sleep", new=no_wait):
                result = await worker._background_worker_loop(effect, runner)

            self.assertEqual(calls, 2)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(assignment_state["value"], "settled")

        asyncio.run(scenario())

    def test_graceful_rebinds_do_not_consume_role_failure_budget(self) -> None:
        async def scenario() -> None:
            worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
            effect = {
                "effect_id": "effect-rebind-budget",
                "effect_key": "effect-key-rebind-budget",
                "effect_type": "test-role-effect",
            }
            assignment = {
                "assignment_id": "assignment-rebind-budget",
                "state": RoleAssignmentState.RUNNING.value,
                "active_attempt_id": "",
                "workflow_id": "workflow-rebind-budget",
                "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                "aggregate_id": "node-rebind-budget",
                "role": "verification",
                "mode": "check",
                "submission_kind": "verification",
                "input_refs": {},
            }
            attempts: list[dict[str, object]] = [
                {
                    "attempt_id": "attempt-rebind-1",
                    "status": "lost",
                    "error_kind": "manager_restart",
                },
                {
                    "attempt_id": "attempt-rebind-2",
                    "status": "lost",
                    "error_kind": "manager_restart",
                },
            ]
            worker._assignment_ids_by_effect[effect["effect_key"]] = assignment[
                "assignment_id"
            ]
            worker.repository.read_role_assignment = lambda _assignment_id: dict(
                assignment
            )
            worker.repository.list_role_assignments = lambda **_kwargs: ()
            worker.repository.list_role_attempts = lambda _assignment_id: tuple(
                dict(item) for item in attempts
            )
            worker._release_background_business_lease = lambda _effect: None

            def queue_retry(current, *, error_kind, error_text):
                del current, error_text
                attempts[-1].update(status="lost", error_kind=error_kind)
                assignment["state"] = RoleAssignmentState.RETRY_QUEUED.value
                return dict(assignment)

            worker._queue_active_assignment_retry = queue_retry
            settled: dict[str, object] = {}

            def settle(_effect, _assignment, _error, *, exhausted):
                settled["exhausted"] = exhausted
                settled["charged"] = _charged_role_failure_attempt_count(attempts)
                return {"status": "triage_required"}

            worker._settle_background_role_failure = settle
            calls = 0

            async def runner(_effect):
                nonlocal calls
                calls += 1
                attempt_id = f"attempt-failure-{calls}"
                attempts.append(
                    {
                        "attempt_id": attempt_id,
                        "status": "running",
                        "error_kind": "",
                    }
                )
                assignment.update(
                    state=RoleAssignmentState.RUNNING.value,
                    active_attempt_id=attempt_id,
                )
                raise RuntimeError("LLM generation failed")

            async def no_wait(_seconds):
                return None

            with patch(
                "pal.minion.v2.semantic_orchestration.orchestrator.asyncio.sleep",
                new=no_wait,
            ):
                result = await worker._background_worker_loop(effect, runner)

            self.assertEqual(result["status"], "triage_required")
            self.assertEqual(calls, 3)
            self.assertEqual(settled, {"exhausted": True, "charged": 3})
            self.assertEqual(len(attempts), 5)

        asyncio.run(scenario())

    def test_recovery_restarts_a_durable_queued_assignment(self) -> None:
        async def scenario() -> None:
            service = MinionV2WorkflowService(self.runtime_root)
            self._create_role_scope(
                service,
                workflow_id="workflow-recovery",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-recovery",
            )
            service.repository.ensure_role_session(
                session_id="session-recovery",
                workflow_id="workflow-recovery",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-recovery",
                role="implementation",
                mode="produce",
                role_profile_id="software_engineering.v2_coder",
                family_binding_sha="binding",
                scope_kind="module",
                subject_key="router",
            )
            assignment = service.repository.create_role_assignment(
                RoleAssignmentRequest(
                    assignment_key="recovery-assignment",
                    session_id="session-recovery",
                    workflow_id="workflow-recovery",
                    aggregate_type=AggregateType.DAG_NODE_RUN.value,
                    aggregate_id="node-recovery",
                    role="implementation",
                    mode="produce",
                    role_profile_id="software_engineering.v2_coder",
                    family_binding_sha="binding",
                    input_fingerprint="input-recovery",
                    required_inputs=(),
                    input_refs={},
                    execution_spec={
                        "effect_type": "run_implementation_role",
                        "effect_key": "effect-key-recovery",
                        "workflow_id": "workflow-recovery",
                        "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                        "aggregate_id": "node-recovery",
                        "payload": {"role_mode": "produce"},
                    },
                    submission_kind="candidate",
                )
            )
            worker = SemanticOrchestrator(service)
            started = asyncio.Event()
            release = asyncio.Event()
            worker.repository.read_snapshot = lambda _aggregate_type, _aggregate_id: (
                SimpleNamespace(state="PRODUCING")
            )

            async def recovered_runner(_effect):
                started.set()
                await release.wait()
                return {"status": "completed"}

            worker._runner_for_recovered_effect = lambda _effect: recovered_runner
            count = await worker.recover_background_assignments()
            await asyncio.wait_for(started.wait(), timeout=1.0)

            self.assertEqual(count, 1)
            self.assertEqual(
                worker._assignment_ids_by_effect["effect-key-recovery"],
                assignment["assignment_id"],
            )
            release.set()
            await asyncio.gather(*tuple(worker._background_workers.values()))

        asyncio.run(scenario())

    def test_recovery_tick_preserves_assignment_owned_by_active_worker(self) -> None:
        async def scenario() -> None:
            service = MinionV2WorkflowService(self.runtime_root)
            self._create_role_scope(
                service,
                workflow_id="workflow-recovery-owner",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-recovery-owner",
            )
            service.repository.ensure_role_session(
                session_id="session-recovery-owner",
                workflow_id="workflow-recovery-owner",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-recovery-owner",
                role="implementation",
                mode="produce",
                role_profile_id="software_engineering.v2_coder",
                family_binding_sha="binding",
                scope_kind="module",
                subject_key="router",
            )

            def create_assignment(key: str, fingerprint: str) -> dict:
                return service.repository.create_role_assignment(
                    RoleAssignmentRequest(
                        assignment_key=key,
                        session_id="session-recovery-owner",
                        workflow_id="workflow-recovery-owner",
                        aggregate_type=AggregateType.DAG_NODE_RUN.value,
                        aggregate_id="node-recovery-owner",
                        role="implementation",
                        mode="produce",
                        role_profile_id="software_engineering.v2_coder",
                        family_binding_sha="binding",
                        input_fingerprint=fingerprint,
                        required_inputs=(),
                        input_refs={},
                        execution_spec={
                            "effect_type": "run_implementation_role",
                            "effect_key": "effect-key-recovery-owner",
                            "workflow_id": "workflow-recovery-owner",
                            "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                            "aggregate_id": "node-recovery-owner",
                            "payload": {"role_mode": "produce"},
                        },
                        submission_kind="candidate",
                    )
                )

            stale = create_assignment("stale-recovery-owner", "old-input")
            service.repository.cancel_role_assignments(
                workflow_id="workflow-recovery-owner",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-recovery-owner",
                reason="superseded before the next logical assignment",
            )
            current = create_assignment("current-recovery-owner", "new-input")
            worker = SemanticOrchestrator(service)
            worker._assignment_ids_by_effect["effect-key-recovery-owner"] = str(
                current["assignment_id"]
            )
            release = asyncio.Event()

            async def active_worker() -> dict[str, str]:
                await release.wait()
                return {"status": "completed"}

            task = asyncio.create_task(active_worker())
            worker._background_workers["effect-key-recovery-owner"] = task
            worker._runner_for_recovered_effect = lambda _effect: active_worker

            count = await worker.recover_background_assignments()

            self.assertEqual(count, 0)
            self.assertFalse(task.done())
            self.assertEqual(
                worker._assignment_ids_by_effect["effect-key-recovery-owner"],
                current["assignment_id"],
            )
            self.assertEqual(
                service.repository.read_role_assignment(stale["assignment_id"])["state"],
                RoleAssignmentState.CANCELLED.value,
            )
            release.set()
            await task

        asyncio.run(scenario())

    def test_expired_active_assignment_is_reused_for_logical_effect(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        self._create_role_scope(
            service,
            workflow_id="workflow-expired-reuse",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-expired-reuse",
        )
        service.repository.ensure_role_session(
            session_id="session-expired-reuse",
            workflow_id="workflow-expired-reuse",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-expired-reuse",
            role="implementation",
            mode="produce",
            role_profile_id="software_engineering.v2_coder",
            family_binding_sha="binding",
            scope_kind="module",
            subject_key="router",
        )
        assignment = service.repository.create_role_assignment(
            RoleAssignmentRequest(
                assignment_key="expired-reuse-assignment",
                session_id="session-expired-reuse",
                workflow_id="workflow-expired-reuse",
                aggregate_type=AggregateType.DAG_NODE_RUN.value,
                aggregate_id="node-expired-reuse",
                role="implementation",
                mode="produce",
                role_profile_id="software_engineering.v2_coder",
                family_binding_sha="binding",
                input_fingerprint="original-input",
                required_inputs=(),
                input_refs={},
                execution_spec={
                    "effect_type": "run_implementation_role",
                    "effect_key": "effect-key-expired-reuse",
                    "workflow_id": "workflow-expired-reuse",
                    "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                    "aggregate_id": "node-expired-reuse",
                    "payload": {"role_mode": "produce"},
                },
                submission_kind="candidate",
            )
        )
        attempt = service.repository.claim_role_assignment(str(assignment["assignment_id"]))
        lease_resource = f"assignment:{assignment['assignment_id']}"
        lease = service.repository.claim_lease(
            lease_resource,
            str(attempt["attempt_id"]),
            ttl_seconds=120,
        )
        prompt_ref = service.artifacts.put_json(
            {"stable": "original durable prompt"},
            artifact_type="RolePromptPackArtifact",
        )
        service.repository.start_role_attempt(
            assignment_id=str(assignment["assignment_id"]),
            attempt_id_value=str(attempt["attempt_id"]),
            lease_resource_key=lease_resource,
            fencing_token=lease.fencing_token,
            prompt_pack_ref=prompt_ref.to_dict(),
        )
        service.repository.release_lease(
            lease_resource,
            str(attempt["attempt_id"]),
            lease.fencing_token,
        )
        worker = SemanticOrchestrator(service)
        worker._assignment_ids_by_effect["effect-key-expired-reuse"] = str(
            assignment["assignment_id"]
        )
        snapshot = SimpleNamespace(
            workflow_id="workflow-expired-reuse",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-expired-reuse",
        )

        recovered = worker._retry_assignment_for_effect(
            {"effect_key": "effect-key-expired-reuse"},
            snapshot=snapshot,
            role="implementation",
            mode="produce",
            submission_kind="candidate",
        )

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["assignment_id"], assignment["assignment_id"])
        self.assertEqual(recovered["state"], RoleAssignmentState.RUNNING.value)
        retryable = worker._queue_active_assignment_retry(
            recovered,
            error_kind="attempt_lease_expired",
            error_text="test recovery",
        )
        self.assertEqual(retryable["state"], RoleAssignmentState.RETRY_QUEUED.value)
        self.assertEqual(
            worker._durable_assignment_prompt_ref(retryable),
            prompt_ref,
        )

    def test_role_retry_recovers_effect_assignment_after_manager_restart(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        self._create_role_scope(
            service,
            workflow_id="workflow-effect-recovery",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-effect-recovery",
        )
        service.repository.ensure_role_session(
            session_id="session-effect-recovery",
            workflow_id="workflow-effect-recovery",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-effect-recovery",
            role="implementation",
            mode="produce",
            role_profile_id="software_engineering.v2_coder",
            family_binding_sha="binding",
            scope_kind="module",
            subject_key="router",
        )
        assignment = service.repository.create_role_assignment(
            RoleAssignmentRequest(
                assignment_key="effect-recovery-assignment",
                session_id="session-effect-recovery",
                workflow_id="workflow-effect-recovery",
                aggregate_type=AggregateType.DAG_NODE_RUN.value,
                aggregate_id="node-effect-recovery",
                role="implementation",
                mode="produce",
                role_profile_id="software_engineering.v2_coder",
                family_binding_sha="binding",
                input_fingerprint="immutable-input",
                required_inputs=(),
                input_refs={},
                execution_spec={
                    "effect_type": "reconcile_semantic_state",
                    "effect_key": "effect-after-restart",
                    "workflow_id": "workflow-effect-recovery",
                    "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                    "aggregate_id": "node-effect-recovery",
                    "payload": {},
                },
                submission_kind="candidate",
            )
        )
        worker = SemanticOrchestrator(service)
        snapshot = SimpleNamespace(
            workflow_id="workflow-effect-recovery",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-effect-recovery",
        )

        recovered = worker._retry_assignment_for_effect(
            {"effect_key": "effect-after-restart"},
            snapshot=snapshot,
            role="implementation",
            mode="produce",
            submission_kind="candidate",
        )

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["assignment_id"], assignment["assignment_id"])
        self.assertEqual(
            worker._assignment_ids_by_effect["effect-after-restart"],
            assignment["assignment_id"],
        )

    def test_submission_settlement_uses_terminal_assignment_identity(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        self._create_role_scope(
            service,
            workflow_id="workflow-explicit-settlement",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-explicit-settlement",
        )
        service.repository.ensure_role_session(
            session_id="session-explicit-settlement",
            workflow_id="workflow-explicit-settlement",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-explicit-settlement",
            role="implementation",
            mode="produce",
            role_profile_id="software_engineering.v2_coder",
            family_binding_sha="binding",
            scope_kind="module",
            subject_key="router",
        )

        def create_assignment(key: str, fingerprint: str) -> dict:
            return service.repository.create_role_assignment(
                RoleAssignmentRequest(
                    assignment_key=key,
                    session_id="session-explicit-settlement",
                    workflow_id="workflow-explicit-settlement",
                    aggregate_type=AggregateType.DAG_NODE_RUN.value,
                    aggregate_id="node-explicit-settlement",
                    role="implementation",
                    mode="produce",
                    role_profile_id="software_engineering.v2_coder",
                    family_binding_sha="binding",
                    input_fingerprint=fingerprint,
                    required_inputs=(),
                    input_refs={},
                    execution_spec={
                        "effect_type": "run_implementation_role",
                        "effect_key": "effect-key-explicit-settlement",
                        "workflow_id": "workflow-explicit-settlement",
                        "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                        "aggregate_id": "node-explicit-settlement",
                        "payload": {"role_mode": "produce"},
                    },
                    submission_kind="candidate",
                )
            )

        stale = create_assignment("stale-explicit-settlement", "old-input")
        service.repository.cancel_role_assignments(
            workflow_id="workflow-explicit-settlement",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-explicit-settlement",
            reason="superseded before the next logical assignment",
        )
        completed = create_assignment("completed-explicit-settlement", "new-input")
        attempt = service.repository.claim_role_assignment(str(completed["assignment_id"]))
        lease_resource = f"assignment:{completed['assignment_id']}"
        lease = service.repository.claim_lease(
            lease_resource,
            str(attempt["attempt_id"]),
            ttl_seconds=120,
        )
        prompt_ref = service.artifacts.put_json(
            {"role": "implementation"},
            artifact_type="RolePromptPackArtifact",
        )
        service.repository.start_role_attempt(
            assignment_id=str(completed["assignment_id"]),
            attempt_id_value=str(attempt["attempt_id"]),
            lease_resource_key=lease_resource,
            fencing_token=lease.fencing_token,
            prompt_pack_ref=prompt_ref.to_dict(),
        )
        submission = {"status": "candidate_ready"}
        submission_ref = service.artifacts.put_json(
            submission,
            artifact_type="CandidateRoleSubmissionArtifact",
        )
        service.repository.record_role_submission(
            assignment_id=str(completed["assignment_id"]),
            attempt_id_value=str(attempt["attempt_id"]),
            fencing_token=lease.fencing_token,
            artifact_ref=submission_ref.to_dict(),
            payload_hash=stable_hash(submission),
            settlement_action={"action_type": "SUBMIT_CANDIDATE"},
        )
        worker = SemanticOrchestrator(service)
        worker._assignment_ids_by_effect["effect-key-explicit-settlement"] = str(
            stale["assignment_id"]
        )
        terminal = worker._terminal_from_assignment_receipt(
            service.repository.read_role_assignment(completed["assignment_id"]),
            primary_artifact_name="candidate.json",
            summary="candidate ready",
        )

        settlement = worker._role_submission_settlement(
            {"effect_key": "effect-key-explicit-settlement"},
            assignment_id=worker._terminal_role_assignment_id(terminal),
        )

        self.assertEqual(settlement["role_assignment_id"], completed["assignment_id"])
        self.assertEqual(
            settlement["role_submission_payload_hash"],
            stable_hash(submission),
        )

    def test_recovery_does_not_duplicate_a_live_role_attempt(self) -> None:
        async def scenario() -> None:
            service = MinionV2WorkflowService(self.runtime_root)
            self._create_role_scope(
                service,
                workflow_id="workflow-live-recovery",
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id="architecture-live-recovery",
            )
            service.repository.ensure_role_session(
                session_id="session-live-recovery",
                workflow_id="workflow-live-recovery",
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id="architecture-live-recovery",
                role="reviewer",
                mode="architecture",
                role_profile_id="software_engineering.v2_reviewer",
                family_binding_sha="binding",
                scope_kind="architecture_cycle",
                subject_key="architecture-live-recovery",
            )
            assignment = service.repository.create_role_assignment(
                RoleAssignmentRequest(
                    assignment_key="live-recovery-assignment",
                    session_id="session-live-recovery",
                    workflow_id="workflow-live-recovery",
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION.value,
                    aggregate_id="architecture-live-recovery",
                    role="reviewer",
                    mode="architecture",
                    role_profile_id="software_engineering.v2_reviewer",
                    family_binding_sha="binding",
                    input_fingerprint="live-recovery-input",
                    required_inputs=(),
                    input_refs={},
                    execution_spec={
                        "effect_type": "run_reviewer_role",
                        "effect_key": "effect-key-live-recovery",
                        "workflow_id": "workflow-live-recovery",
                        "aggregate_type": AggregateType.ARCHITECTURE_REVISION.value,
                        "aggregate_id": "architecture-live-recovery",
                        "payload": {"role_mode": "architecture"},
                    },
                    submission_kind="architecture_review",
                )
            )
            attempt = service.repository.claim_role_assignment(
                str(assignment["assignment_id"])
            )
            lease_resource = f"assignment:{assignment['assignment_id']}"
            lease = service.repository.claim_lease(
                lease_resource,
                str(attempt["attempt_id"]),
                ttl_seconds=120,
            )
            prompt_ref = service.artifacts.put_json(
                {"role": "reviewer", "mode": "architecture"},
                artifact_type="RolePromptPackArtifact",
            )
            service.repository.start_role_attempt(
                assignment_id=str(assignment["assignment_id"]),
                attempt_id_value=str(attempt["attempt_id"]),
                lease_resource_key=lease_resource,
                fencing_token=lease.fencing_token,
                prompt_pack_ref=prompt_ref.to_dict(),
            )
            submission_ref = service.artifacts.put_json(
                {"verdict": "PASS"},
                artifact_type="ArchitectureReviewRoleSubmissionArtifact",
            )
            service.repository.record_role_submission(
                assignment_id=str(assignment["assignment_id"]),
                attempt_id_value=str(attempt["attempt_id"]),
                fencing_token=lease.fencing_token,
                artifact_ref=submission_ref.to_dict(),
                payload_hash="live-review-submission",
                settlement_action={
                    "action_type": "SETTLE_WORKER_SUBMISSION",
                    "aggregate_type": AggregateType.ARCHITECTURE_REVISION.value,
                    "aggregate_id": "architecture-live-recovery",
                },
            )
            worker = SemanticOrchestrator(service)
            worker.repository.read_snapshot = lambda _aggregate_type, _aggregate_id: (
                SimpleNamespace(state="REVIEWING")
            )

            count = await worker.recover_background_assignments()

            self.assertEqual(count, 0)
            self.assertEqual(worker.active_background_count, 0)
            self.assertEqual(
                service.repository.read_role_assignment(assignment["assignment_id"])[
                    "state"
                ],
                "result_recorded",
            )

        asyncio.run(scenario())

    def test_recovery_cancels_assignment_for_a_paused_aggregate(self) -> None:
        async def scenario() -> None:
            service = MinionV2WorkflowService(self.runtime_root)
            self._create_role_scope(
                service,
                workflow_id="workflow-paused",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-paused",
            )
            service.repository.ensure_role_session(
                session_id="session-paused",
                workflow_id="workflow-paused",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-paused",
                role="implementation",
                mode="produce",
                role_profile_id="software_engineering.v2_coder",
                family_binding_sha="binding",
                scope_kind="module",
                subject_key="router",
            )
            assignment = service.repository.create_role_assignment(
                RoleAssignmentRequest(
                    assignment_key="paused-assignment",
                    session_id="session-paused",
                    workflow_id="workflow-paused",
                    aggregate_type=AggregateType.DAG_NODE_RUN.value,
                    aggregate_id="node-paused",
                    role="implementation",
                    mode="produce",
                    role_profile_id="software_engineering.v2_coder",
                    family_binding_sha="binding",
                    input_fingerprint="input-paused",
                    required_inputs=(),
                    input_refs={},
                    execution_spec={
                        "effect_type": "run_implementation_role",
                        "effect_key": "effect-key-paused",
                        "workflow_id": "workflow-paused",
                        "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                        "aggregate_id": "node-paused",
                        "payload": {"role_mode": "produce"},
                    },
                    submission_kind="candidate",
                )
            )
            worker = SemanticOrchestrator(service)
            worker.repository.read_snapshot = lambda _aggregate_type, _aggregate_id: (
                SimpleNamespace(state="PAUSED")
            )

            count = await worker.recover_background_assignments()

            self.assertEqual(count, 0)
            self.assertEqual(
                service.repository.read_role_assignment(assignment["assignment_id"])[
                    "state"
                ],
                "cancelled",
            )

        asyncio.run(scenario())

    def test_recovery_cancels_assignment_superseded_by_equivalent_submission(self) -> None:
        async def scenario() -> None:
            service = MinionV2WorkflowService(self.runtime_root)
            requirements_ref = service.task_ledger.publish(
                title="Requirements",
                task_spec={"objective": "Review the submitted architecture."},
                actor="test",
                source_channel="test",
            )
            preparation_ref = service.artifacts.put_json(
                {"workspace_root": "/tmp/ephemeral"},
                artifact_type="WorkspacePreparationArtifact",
            )
            self._create_role_scope(
                service,
                workflow_id="workflow-duplicate-review",
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id="architecture-duplicate-review",
            )
            service.repository.ensure_role_session(
                session_id="session-duplicate-review",
                workflow_id="workflow-duplicate-review",
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id="architecture-duplicate-review",
                role="reviewer",
                mode="architecture",
                role_profile_id="software_engineering.v2_reviewer",
                family_binding_sha="binding",
                scope_kind="architecture_cycle",
                subject_key="architecture-duplicate-review",
            )
            assignment = service.repository.create_role_assignment(
                RoleAssignmentRequest(
                    assignment_key="duplicate-review-assignment",
                    session_id="session-duplicate-review",
                    workflow_id="workflow-duplicate-review",
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION.value,
                    aggregate_id="architecture-duplicate-review",
                    role="reviewer",
                    mode="architecture",
                    role_profile_id="software_engineering.v2_reviewer",
                    family_binding_sha="binding",
                    input_fingerprint="duplicate-review-input",
                    required_inputs=("requirements", "workspace_preparation"),
                    input_refs={
                        "requirements": requirements_ref.to_dict(),
                        "workspace_preparation": preparation_ref.to_dict(),
                    },
                    execution_spec={
                        "effect_type": "run_reviewer_role",
                        "effect_key": "duplicate-review-effect",
                        "workflow_id": "workflow-duplicate-review",
                        "aggregate_type": AggregateType.ARCHITECTURE_REVISION.value,
                        "aggregate_id": "architecture-duplicate-review",
                        "payload": {"role_mode": "architecture"},
                    },
                    submission_kind="architecture_review",
                )
            )
            worker = SemanticOrchestrator(service)
            worker._reusable_role_assignment = lambda **_kwargs: {
                "assignment_id": "accepted-review-assignment"
            }

            count = await worker.recover_background_assignments()

            self.assertEqual(count, 0)
            self.assertEqual(
                service.repository.read_role_assignment(assignment["assignment_id"])[
                    "state"
                ],
                "cancelled",
            )

        asyncio.run(scenario())

    def test_revision_scope_read_is_not_exposed_to_initial_architect(self) -> None:
        pack = MinionInvocationPack(
            invocation_id="inv-capability-scope",
            allowed_capabilities=[
                *ARCHITECT_BUILDER_CAPABILITIES,
                "op_minion_contract_revision_read",
            ],
        )

        initial = apply_v2_role_capability_policy(
            pack,
            activation=RoleActivation(OrchestrationRole.ARCHITECT, RoleMode.AUTHOR),
        )
        self.assertNotIn("op_minion_contract_revision_read", initial.allowed_capabilities)
        self.assertNotIn("op_minion_contract_read", initial.allowed_capabilities)
        self.assertIn("op_minion_contract_submit", initial.allowed_capabilities)
        self.assertIn("op_minion_update_checklist", initial.allowed_capabilities)

        scoped = apply_v2_revision_scope_capability_policy(initial)
        self.assertNotIn("op_minion_contract_revision_read", scoped.allowed_capabilities)
        self.assertNotIn("op_minion_contract_read", scoped.allowed_capabilities)
        self.assertEqual(scoped.allowed_capabilities, initial.allowed_capabilities)

    def test_human_edit_closes_previous_architect_session_before_new_revision(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        processor = MinionV2OutboxProcessor(service, semantic_effects=_NoopSemanticEffects())
        previous = SimpleNamespace(
            workflow_id="wf-edit",
            aggregate_id="arch-edit-1",
            payload={
                "request_ref": {"sha256": "request"},
                "requirements_ref": {"sha256": "requirements"},
                "edit_instruction_ref": {"sha256": "edit"},
                "architecture_manifest_ref": {"sha256": "skeleton"},
                "revision_number": 1,
                "research_mode": "local_only",
            },
        )
        processor._effect_snapshot = lambda _effect: previous
        completed: list[str] = []
        processor.repository.complete_role_session = lambda invocation_id, **_kwargs: completed.append(
            invocation_id
        ) or True
        actions: list[ActionEnvelope] = []
        processor.repository.dispatch = lambda action, **_kwargs: actions.append(action)
        processor._link_workflow = lambda *_args, **_kwargs: None

        with patch(
            "pal.minion.v2.orchestration.WorkflowCoordinator.begin_plan_revision"
        ) as begin_revision:
            processor._create_revision({"effect_key": "event-edit:0"})

        self.assertEqual(completed, [])
        begin_revision.assert_called_once()
        self.assertEqual(begin_revision.call_args.kwargs["workflow_id"], "wf-edit")
        self.assertIsNotNone(begin_revision.call_args.kwargs["_connection"])
        self.assertEqual([action.action_type for action in actions], ["CREATE_ARCHITECTURE_REVISION"])
        self.assertEqual(actions[0].payload["parent_revision_id"], previous.aggregate_id)
        self.assertEqual(
            actions[0].payload["architecture_cycle_id"],
            previous.aggregate_id,
        )

    def test_materialized_human_decision_publishes_review_resolution(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        manifest_ref = service.artifacts.put_json(
            {"repository_layout": {"workspace_root": "/tmp/example"}},
            artifact_type="ContractArtifact",
        )
        published: list[dict[str, object]] = []
        worker = SemanticOrchestrator(
            service,
            publish_workflow_event=lambda payload: published.append(dict(payload)),
        )
        revision = SimpleNamespace(
            workflow_id="wf-human-resolution",
            aggregate_id="revision-human-resolution",
            state="ACCEPTED",
            updated_at="2026-08-10T05:00:00+00:00",
            payload={"architecture_manifest_ref": manifest_ref.to_dict()},
        )
        worker._effect_snapshot = lambda _effect: revision

        with patch(
            "pal.minion.v2.semantic_orchestration.orchestrator."
            "PlanRevisionProjectionStore.update_status",
            return_value=self.runtime_root / "plans" / "revision-human-resolution",
        ):
            result = worker._materialize_plan_revision_status(
                {"status": "accepted"}
            )

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(
            published,
            [
                {
                    "event_kind": "architecture_review_resolved",
                    "workflow_id": "wf-human-resolution",
                    "architecture_revision_id": "revision-human-resolution",
                    "status": "accepted",
                    "summary": "Minion architecture decision recorded (accepted).",
                    "resolved_at": "2026-08-10T05:00:00+00:00",
                }
            ],
        )

    def test_human_edit_rebinds_replan_epoch_to_child_revision_atomically(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        processor = MinionV2OutboxProcessor(service, semantic_effects=_NoopSemanticEffects())
        previous = SimpleNamespace(
            workflow_id="wf-replan-edit",
            aggregate_id="arch-replan-1",
            payload={
                "request_ref": {"sha256": "request"},
                "requirements_ref": {"sha256": "requirements"},
                "edit_instruction_ref": {"sha256": "edit"},
                "architecture_manifest_ref": {"sha256": "skeleton"},
                "architecture_cycle_id": "arch-replan-root",
                "source_execution_epoch_id": "epoch-replan",
                "replan_generation": 4,
                "replan_finding_batch_ref": {"sha256": "findings"},
                "revision_number": 2,
                "research_mode": "local_only",
            },
        )
        epoch = SimpleNamespace(
            workflow_id=previous.workflow_id,
            aggregate_id="epoch-replan",
            aggregate_type=AggregateType.EXECUTION_EPOCH,
            state="REPLAN_REQUIRED",
            version=7,
            payload={"active_replan_revision_id": previous.aggregate_id},
        )
        processor._effect_snapshot = lambda _effect: previous
        processor.repository.read_snapshot = lambda *_args, **_kwargs: epoch
        actions: list[ActionEnvelope] = []
        processor.repository.dispatch = lambda action, **_kwargs: actions.append(action)
        linked: list[dict[str, str]] = []
        processor._link_workflow = (
            lambda _workflow_id, _action_type, payload, *_args, **_kwargs: linked.append(
                dict(payload)
            )
        )

        with patch(
            "pal.minion.v2.orchestration.WorkflowCoordinator.begin_plan_revision"
        ):
            processor._create_revision({"effect_key": "event-replan-edit:0"})

        self.assertEqual(
            [action.action_type for action in actions],
            ["CREATE_ARCHITECTURE_REVISION", "REPLAN_REVISION_LINKED"],
        )
        child_id = actions[0].aggregate_id
        self.assertEqual(
            actions[0].payload["source_execution_epoch_id"],
            epoch.aggregate_id,
        )
        self.assertEqual(actions[0].payload["replan_generation"], 4)
        self.assertEqual(
            actions[0].payload["replan_finding_batch_ref"],
            {"sha256": "findings"},
        )
        self.assertEqual(
            actions[1].payload["active_replan_revision_id"],
            child_id,
        )
        self.assertEqual(linked, [{"architecture_revision_id": child_id}])

    def test_workflow_completion_publishes_one_terminal_task_event(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        published: list[dict[str, object]] = []
        processor = MinionV2OutboxProcessor(
            service,
            semantic_effects=_NoopSemanticEffects(),
            publish_workflow_event=lambda event: published.append(dict(event)),
        )
        workflow = AggregateSnapshot(
            aggregate_type=AggregateType.WORKFLOW,
            aggregate_id="wf-terminal-delivery",
            workflow_id="wf-terminal-delivery",
            state="ACTIVE",
            version=3,
            payload={},
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        actions: list[ActionEnvelope] = []
        processor.repository.dispatch = lambda action, **_kwargs: actions.append(action)
        processor.repository.complete_workflow_role_sessions = lambda *_args, **_kwargs: None

        completed = AggregateSnapshot(
            aggregate_type=workflow.aggregate_type,
            aggregate_id=workflow.aggregate_id,
            workflow_id=workflow.workflow_id,
            state="COMPLETED",
            version=4,
            payload={
                "architecture_revision_id": "arch-terminal-delivery",
                "result_artifact_ref": {"sha256": "deliverable-sha"},
            },
            created_at=workflow.created_at,
            updated_at=workflow.updated_at,
        )
        processor.repository.read_snapshot = lambda *_args, **_kwargs: completed

        processor._publish_terminal_workflow_if_any(workflow.workflow_id)

        self.assertEqual(actions, [])
        self.assertEqual(
            published,
            [
                {
                    "workflow_id": workflow.workflow_id,
                    "status": "completed",
                    "summary": "Minion workflow completed.",
                    "terminal_at": workflow.updated_at,
                    "resolved_interactions": [
                        {
                            "interaction_id": "minion_v2_architecture_arch-terminal-delivery",
                            "interaction_kind": "minion_v2_architecture_review",
                        }
                    ],
                    "result_artifact_ref": {"sha256": "deliverable-sha"},
                }
            ],
        )

    def test_runner_checkpoint_uses_manager_selected_paths_and_ignores_stale_files(self) -> None:
        async def scenario() -> None:
            run_dir = self.runtime_root / "agent-session"
            run_dir.mkdir(parents=True)
            context = MainContext()
            memory_service = MemoryService()
            register_execution_with_core(context)
            register_memory_with_core(context, memory_service)
            bundle = MinionRuntimeBundle(
                llm_runtime=SimpleNamespace(),
                execution_runtime=context.execution_runtime,
                memory_service=memory_service,
                module_registry=context.module_registry,
                runtime_state_coordinator=RuntimeSnapshotCoordinator(context.module_registry),
            )
            first_output = run_dir / "attempt-1" / "continuation-output.json"
            base_session = {
                "session_id": "inv-session-checkpoint",
                "workflow_id": "wf-session-checkpoint",
                "stage_key": "module:node-router:implementation",
                "scope_kind": "module_run",
                "subject_key": "node-router",
                "harness_id": "pal",
            }
            first_pack = MinionInvocationPack(
                invocation_id="inv-session-checkpoint",
                instruction="initial assignment",
                workspace={"run_dir": str(run_dir)},
                metadata={
                    "minion_v2": {"role": "implementation", "mode": "produce"},
                    "agent_session": {
                        **base_session,
                        "response_key": "effect-1",
                        "fencing_token": 3,
                        "continuation_output_path": str(first_output),
                    },
                },
            )
            first = MinionRunner(
                runtime_root=self.runtime_root,
                pack=first_pack,
                minion_id=first_pack.invocation_id,
                run_id="run-session-checkpoint",
                write_event=lambda _event: None,
                read_decision=lambda: None,
            )
            memory_service.commit_l1(
                MemoryCommitRequest(
                    turn_id="run-session-checkpoint",
                    transcript=[
                        L1TranscriptMessage(
                            role="assistant",
                            content="inspected the boundary",
                            kind=L1MessageKind.ASSISTANT_REPLY,
                        )
                    ],
                )
            )
            state = SimpleNamespace(
                llm_round_count=8,
                tool_call_count=21,
                memory_service=memory_service,
                memory_candidate_sink=SimpleNamespace(
                    records=[
                        {
                            "document_id": "case:1",
                            "document_kind": "case",
                            "scope": "task",
                            "summary": "preserve this candidate",
                        }
                    ]
                ),
            )
            first.produced_artifacts.append(
                {"path": "/tmp/primary.json", "role": "primary"}
            )
            first.review_tool_evidence_refs.append({"kind": "test", "ok": True})
            first.web_research_usage.update({"total": 2, "search_web": 2})
            first._manager_submission_receipt_observed = True
            continuation = SimpleNamespace(
                pending_tool_call_batch=[],
                pending_tool_results=[],
                tool_batch_count=6,
                preferred_llm_endpoint_id="glm",
                preferred_llm_model_id="glm-5.2",
            )
            await first._persist_agent_session_checkpoint(
                bundle,
                state,
                continuation,
                initial_instruction="initial assignment",
                response_keys=["effect-1"],
            )
            stale = run_dir / "session-continuation-999.json"
            stale.write_text('{"schema_version":"4","llm_round_count":999}', encoding="utf-8")

            second_output = run_dir / "attempt-2" / "continuation-output.json"
            second_pack = MinionInvocationPack(
                invocation_id="inv-session-checkpoint",
                instruction="repair reviewer finding",
                workspace={"run_dir": str(run_dir)},
                metadata={
                    # Repair is another playbook turn of the same durable
                    # implementation coroutine, not a new checkpoint stage.
                    "minion_v2": {"role": "implementation", "mode": "repair"},
                    "agent_session": {
                        **base_session,
                        "response_key": "effect-2",
                        "fencing_token": 4,
                        "continuation_input_path": str(first_output),
                        "continuation_output_path": str(second_output),
                    },
                },
            )
            second = MinionRunner(
                runtime_root=self.runtime_root,
                pack=second_pack,
                minion_id=second_pack.invocation_id,
                run_id="run-session-checkpoint",
                write_event=lambda _event: None,
                read_decision=lambda: None,
            )
            restored = second._load_agent_session_checkpoint(
                second_pack.workspace,
                session_id="inv-session-checkpoint",
                bundle=bundle,
            )
            coroutine_state = dict(restored["coroutine_state"])
            self.assertEqual(coroutine_state["llm_round_count"], 8)
            self.assertEqual(coroutine_state["tool_call_count"], 21)
            self.assertEqual(coroutine_state["response_keys"], ["effect-1"])
            self.assertEqual(coroutine_state["active_response_key"], "effect-1")
            invocation_state = dict(coroutine_state["invocation_state"])
            self.assertEqual(invocation_state["web_research_usage"]["total"], 2)
            self.assertEqual(
                invocation_state["produced_artifacts"][0]["role"],
                "primary",
            )
            restored_sink = SimpleNamespace(records=[])
            second._restore_invocation_checkpoint_state(
                coroutine_state,
                active_response_key="effect-1",
                memory_candidate_sink=restored_sink,
            )
            self.assertEqual(second.web_research_usage["search_web"], 2)
            self.assertEqual(restored_sink.records[0]["document_id"], "case:1")
            self.assertTrue(second._manager_submission_receipt_observed)

            second._restore_invocation_checkpoint_state(
                coroutine_state,
                active_response_key="effect-2",
                memory_candidate_sink=restored_sink,
            )
            self.assertEqual(second.produced_artifacts, [])
            self.assertEqual(second.web_research_usage, {})
            self.assertEqual(restored_sink.records, [])
            self.assertIn(
                "inspected the boundary",
                json.dumps(
                    restored["runtime_snapshot"]["modules"]["memory"]["payload"]["l1_turns"]
                ),
            )
            self.assertEqual(restored["workflow_id"], "wf-session-checkpoint")
            self.assertEqual(restored["stage_key"], "module:node-router:implementation")
            self.assertTrue(first_output.is_file())
            self.assertNotIn("inspected the boundary", first_output.read_text(encoding="utf-8"))
            context.execution_runtime.shutdown()

        asyncio.run(scenario())

    def test_runner_reloads_latest_coder_checklist_for_each_prompt_assembly(self) -> None:
        pack = MinionInvocationPack(
            invocation_id="inv-checklist-context",
            workspace={"repo_path": str(self.runtime_root)},
            metadata={
                "minion_v2": {
                    "role": "implementation",
                    "invocation_id": "inv-checklist-context",
                }
            },
        )
        runner = MinionRunner(
            runtime_root=self.runtime_root,
            pack=pack,
            minion_id=pack.invocation_id,
            run_id="run-checklist-context",
            write_event=_noop_write_event,
            read_decision=lambda: None,
        )
        with patch(
            "pal.minion.v2.work_items.render_work_item_context",
            side_effect=["pending: implement", "completed: implement"],
        ) as render:
            self.assertEqual(runner._render_durable_role_context(), "pending: implement")
            self.assertEqual(runner._render_durable_role_context(), "completed: implement")
        self.assertEqual(render.call_count, 2)

    def test_runner_reloads_latest_architect_checklist_for_each_prompt_assembly(
        self,
    ) -> None:
        pack = MinionInvocationPack(
            invocation_id="inv-architect-checklist-context",
            workspace={"repo_path": str(self.runtime_root)},
            metadata={
                "minion_v2": {
                    "role": "architect",
                    "invocation_id": "inv-architect-checklist-context",
                }
            },
        )
        runner = MinionRunner(
            runtime_root=self.runtime_root,
            pack=pack,
            minion_id=pack.invocation_id,
            run_id="run-architect-checklist-context",
            write_event=_noop_write_event,
            read_decision=lambda: None,
        )
        with patch(
            "pal.minion.v2.work_items.render_work_item_context",
            side_effect=["phase 1", "phase 2"],
        ) as render:
            self.assertEqual(
                runner._render_durable_role_context(),
                "phase 1",
            )
            self.assertEqual(
                runner._render_durable_role_context(),
                "phase 2",
            )
        self.assertEqual(render.call_count, 2)

    def test_runner_restart_control_is_distinct_from_workflow_cancel(self) -> None:
        messages = [
            {
                "type": "restart_requested",
                "payload": {"reason": "plugin_reload", "summary": "reload at safe point"},
            }
        ]

        async def read_control(_timeout=None):
            return messages.pop(0) if messages else None

        runner = MinionRunner(
            runtime_root=self.runtime_root,
            pack=MinionInvocationPack(invocation_id="inv-restart-control"),
            minion_id="inv-restart-control",
            run_id="run-restart-control",
            write_event=_noop_write_event,
            read_decision=read_control,
        )

        asyncio.run(runner._raise_if_cancel_requested())
        self.assertFalse(runner._cancel_requested)
        self.assertFalse(
            runner._continuation_is_restart_safe(
                SimpleNamespace(
                    pending_tool_call_batch=[object()],
                    pending_tool_results=[],
                )
            )
        )
        self.assertTrue(
            runner._continuation_is_restart_safe(
                SimpleNamespace(
                    pending_tool_call_batch=[],
                    pending_tool_results=[],
                )
            )
        )
        memory = MemoryService()
        memory.begin_l1_turn("restart-pending", user_text="continue")
        memory.upsert_l1_assistant(
            "restart-pending",
            LLMMessageIR(
                role=MessageRole.ASSISTANT,
                parts=(
                    new_tool_call(
                        call_id="pending-call",
                        name="read_file",
                        arguments={},
                    ),
                ),
            ),
        )
        self.assertFalse(
            runner._continuation_is_restart_safe(
                SimpleNamespace(
                    pending_tool_call_batch=[],
                    pending_tool_results=[],
                ),
                memory,
            )
        )
        with self.assertRaisesRegex(Exception, "reload at safe point"):
            asyncio.run(runner._raise_if_restart_requested())

    def test_runner_keys_new_session_inputs_by_assignment_not_repeated_text(self) -> None:
        restored = {
            "initial_instruction": "implement the bound module",
            "response_keys": ["assignment-produce"],
        }

        self.assertEqual(
            MinionRunner._new_session_response_message(
                restored,
                response_key="assignment-produce",
                response_text="implement the bound module",
            ),
            "",
        )
        repair = MinionRunner._new_session_response_message(
            restored,
            response_key="assignment-repair-1",
            response_text=(
                "# Contract Invocation\n\n"
                "## Assignment\n"
                "Repair only the bound RepairBill.\n\n"
                "## Immutable Inputs\n"
                "- reference:repair_bill: path=/pal/references/repair_bill-1.json"
            ),
        )
        self.assertIn("# New Manager-Bound Role Input", repair)
        self.assertIn("reference:repair_bill", repair)
        self.assertIn("/pal/references/repair_bill-1.json", repair)

        repeated_instruction_new_bill = MinionRunner._new_session_response_message(
            {
                **restored,
                "response_keys": ["assignment-produce", "assignment-repair-1"],
            },
            response_key="assignment-repair-2",
            response_text=(
                "# Contract Invocation\n\n"
                "## Assignment\n"
                "Repair only the bound RepairBill.\n\n"
                "## Immutable Inputs\n"
                "- reference:repair_bill: path=/pal/references/repair_bill-2.json"
            ),
        )
        self.assertIn(
            "/pal/references/repair_bill-2.json",
            repeated_instruction_new_bill,
        )
        self.assertEqual(
            MinionRunner._new_session_response_message(
                restored,
                response_key="",
                response_text="The user selected the bounded compatibility option.",
            ),
            "",
        )

    def test_effect_retry_reuses_same_durable_role_assignment(self) -> None:
        processor = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        snapshot = AggregateSnapshot(
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router",
            workflow_id="wf-router",
            state="PRODUCING",
            version=3,
            payload={},
            created_at="2026-07-22T00:00:00+00:00",
            updated_at="2026-07-22T00:00:00+00:00",
        )
        assignment = {
            "assignment_id": "asg-router",
            "workflow_id": "wf-router",
            "aggregate_type": AggregateType.DAG_NODE_RUN.value,
            "aggregate_id": "node-router",
            "role": "implementation",
            "mode": "produce",
            "submission_kind": "candidate",
            "state": RoleAssignmentState.RETRY_QUEUED.value,
        }
        processor._assignment_ids_by_effect["effect-router"] = "asg-router"
        processor.repository.read_role_assignment = lambda assignment_id: (
            dict(assignment) if assignment_id == "asg-router" else None
        )

        selected = processor._retry_assignment_for_effect(
            {"effect_key": "effect-router"},
            snapshot=snapshot,
            role="implementation",
            mode="produce",
            submission_kind="candidate",
        )

        self.assertEqual(selected["assignment_id"], "asg-router")

    def test_role_retry_uses_assignment_fingerprint_as_immutable_truth(self) -> None:
        self.assertEqual(
            _assignment_input_fingerprint(
                {"input_fingerprint": "assignment-input"}
            ),
            "assignment-input",
        )
        with self.assertRaisesRegex(
            SubmissionInvariantError,
            "no immutable input fingerprint",
        ):
            _assignment_input_fingerprint({"input_fingerprint": ""})

    @staticmethod
    def _start_workflow(service: MinionV2WorkflowService, request: dict):
        data = dict(request)
        data.setdefault(
            "delivery_binding",
            {
                "channel_id": "socket_test",
                "channel_kind": "socket",
                "reply_target": {
                    "session_id": "test-session",
                    "request_id": "test-request",
                },
                "control_scope_key": "socket:socket_test:test-session",
            },
        )
        if (
            str(data.get("operation") or "new_requirement") == "new_requirement"
            and not data.get("requirements_ref")
            and not data.get("task_spec")
        ):
            data["task_spec"] = {"objective": str(data.get("goal") or "test task")}
        return service.start_workflow(data)

    def _public_provider(self, *, wake_manager=lambda: None) -> MinionV2PublicProvider:
        provider = MinionV2PublicProvider(
            runtime_root=self.runtime_root,
            wake_manager=wake_manager,
        )
        def manager_request(method, params=None):
            request = dict(params or {})
            if method == "v2_start_workflow":
                return provider.service.start_workflow(request)
            if method == "v2_rebind_task_delivery":
                return provider.service.repository.rebind_task_delivery(
                    task_id=str(request.get("task_id") or ""),
                    binding=dict(request.get("binding") or {}),
                )
            if method == "v2_task_status":
                return provider.service.task_status(
                    str(request.get("task_id") or ""),
                    workflow_id=str(request.get("workflow_id") or ""),
                    view=str(request.get("view") or "status"),
                )
            self.fail(f"unexpected Manager request: {method}")

        provider.manager_request = manager_request
        provider._capture_delivery_binding = lambda _call: {
            "channel_id": "socket_test",
            "channel_kind": "socket",
            "reply_target": {
                "session_id": "test-session",
                "request_id": "test-request",
            },
            "control_scope_key": "socket:socket_test:test-session",
        }
        return provider

    def test_supporting_artifact_does_not_complete_required_primary_output(self) -> None:
        pack = MinionInvocationPack(
            invocation_id="inv-primary",
            workspace={"output_policy": {"primary_artifact": "expected.json"}},
        )
        runner = MinionRunner(
            runtime_root=self.runtime_root,
            pack=pack,
            minion_id="inv-primary",
            run_id="run-primary",
            write_event=lambda _event: None,
            read_decision=lambda: None,
        )
        runner.produced_artifacts = [
            {"role": "supporting", "relative_path": "requirements.json", "path": "/tmp/requirements.json"}
        ]
        self.assertFalse(runner._completion_evidence_present())
        runner.produced_artifacts.append(
            {"role": "primary", "relative_path": "expected.json", "path": "/tmp/expected.json"}
        )
        self.assertTrue(runner._completion_evidence_present())

    def test_runner_keeps_same_invocation_alive_until_primary_submit_succeeds(self) -> None:
        pack = MinionInvocationPack(
            invocation_id="inv-completion-gate",
            workspace={"output_policy": {"primary_artifact": "coder_report.json"}},
        )
        runner = _CompletionGateRunner(
            runtime_root=self.runtime_root,
            pack=pack,
            minion_id="inv-completion-gate",
            run_id="run-completion-gate",
            write_event=_noop_write_event,
            read_decision=_noop_read_decision,
        )

        result = asyncio.run(runner._run_v2_invocation(_FakeRuntimeBundle()))

        self.assertEqual(result, 0)
        self.assertEqual(len(runner.retry_notes), 2)
        self.assertEqual(runner.retry_notes[0], "")
        self.assertIn("candidate_submit", runner.retry_notes[1])
        self.assertIn("coder_report.json", runner.retry_notes[1])

    def test_runner_stops_before_another_llm_round_after_primary_submit(self) -> None:
        pack = MinionInvocationPack(
            invocation_id="inv-submit-stop",
            workspace={"output_policy": {"primary_artifact": "verification_plan.json"}},
        )
        runner = MinionRunner(
            runtime_root=self.runtime_root,
            pack=pack,
            minion_id="inv-submit-stop",
            run_id="run-submit-stop",
            write_event=_noop_write_event,
            read_decision=_noop_read_decision,
        )
        runner.produced_artifacts.append(
            {
                "role": "primary",
                "relative_path": "verification_plan.json",
                "path": str(self.runtime_root / "verification_plan.json"),
            }
        )
        state = MinionAgentLoopState(
            execution_runtime=SimpleNamespace(),
            memory_service=SimpleNamespace(),
            memory_candidate_sink=SimpleNamespace(),
        )

        result = runner._preflight_minion_llm_round(state)

        self.assertIsNotNone(result)
        self.assertEqual(result.payload.finish_reason, LLMFinishReason.STOP)
        self.assertEqual(state.llm_round_count, 0)

    def test_manager_bound_runner_requires_receipt_not_primary_file(self) -> None:
        pack = MinionInvocationPack(
            invocation_id="inv-receipt-gate",
            workspace={"output_policy": {"primary_artifact": "coder_report.json"}},
            metadata={"minion_v2": {"submission_receipt_required": True}},
        )
        runner = MinionRunner(
            runtime_root=self.runtime_root,
            pack=pack,
            minion_id=pack.invocation_id,
            run_id="run-receipt-gate",
            write_event=_noop_write_event,
            read_decision=_noop_read_decision,
        )
        runner.produced_artifacts.append(
            {
                "role": "primary",
                "relative_path": "coder_report.json",
                "path": str(self.runtime_root / "coder_report.json"),
            }
        )
        state = MinionAgentLoopState(
            execution_runtime=SimpleNamespace(),
            memory_service=SimpleNamespace(),
            memory_candidate_sink=SimpleNamespace(),
        )

        with patch.object(runner, "_manager_submission_receipt_present", return_value=False):
            self.assertIsNone(runner._preflight_minion_llm_round(state))
        self.assertEqual(state.llm_round_count, 1)

        with patch.object(runner, "_manager_submission_receipt_present", return_value=True):
            result = runner._preflight_minion_llm_round(state)
        self.assertIsNotNone(result)
        self.assertEqual(result.payload.finish_reason, LLMFinishReason.STOP)

    def test_completion_gate_stops_after_explicit_retry_makes_no_progress(self) -> None:
        pack = MinionInvocationPack(
            invocation_id="inv-completion-stalled",
            workspace={"output_policy": {"primary_artifact": "expected.json"}},
        )
        runner = _StalledCompletionGateRunner(
            runtime_root=self.runtime_root,
            pack=pack,
            minion_id=pack.invocation_id,
            run_id="run-completion-stalled",
            write_event=_noop_write_event,
            read_decision=_noop_read_decision,
        )

        result = asyncio.run(runner._run_v2_invocation(_FakeRuntimeBundle()))

        self.assertEqual(result, 0)
        self.assertEqual(runner.agent_calls, 2)
        self.assertEqual(runner.blocked_kind, "completion_gate_stalled")
        self.assertIn("made no capability or artifact progress", runner.blocked_summary)

    def test_snapshot_rejection_becomes_the_latest_architect_instruction(self) -> None:
        instruction = _contract_architect_instruction(
            finding={
                "summary": "Architect changed paths outside declared scopes: package/__init__.py",
                "repair_instruction": "Declare the path without changing unrelated modules.",
            },
            has_base_manifest=False,
            has_revision_scope=False,
        )

        self.assertIn("was rejected and is not accepted", instruction)
        self.assertIn("Author the current Architecture Skeleton", instruction)
        self.assertIn("strictly module-level declaration", instruction)
        self.assertIn("never implement product behavior", instruction)
        self.assertLess(len(instruction), 2_000)
        rendered_input = render_minion_task_prompt(
            MinionInvocationPack(
                invocation_id="inv-architect-boundary",
                goal="Design the requested software architecture.",
                instruction=instruction,
            )
        )
        self.assertIn("## Assignment", rendered_input)
        self.assertIn("strictly module-level declaration", rendered_input)
        self.assertIn("First read the ordered read-only task ledger", instruction)
        self.assertIn("Use update_checklist as the fixed work cursor", instruction)
        self.assertIn("only then fill the Manager-preseeded architect.yaml", instruction)
        self.assertIn("Immediately begin file-edit tool calls", instruction)
        self.assertIn("do not spend another response restating", instruction)
        self.assertIn("Read revision_finding before any other work", instruction)
        self.assertIn("package/__init__.py", instruction)
        self.assertIn("Do not report the earlier submit as completion", instruction)
        self.assertIn("Call contract_submit again", instruction)

        scoped = _contract_architect_instruction(
            finding={"summary": "One physical reference is invalid."},
            has_base_manifest=True,
            has_revision_scope=True,
        )
        self.assertIn("revision_scope as repair guidance, not as a write fence", scoped)
        self.assertIn("repair every affected module in the same candidate", scoped)
        self.assertIn("immutable_requirement_paths", scoped)
        self.assertIn("call ask_question and wait", scoped)

    def test_architecture_stage_resolves_snapshot_before_profile(self) -> None:
        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        worker._effect_snapshot = lambda _effect: SimpleNamespace(
            workflow_id="wf-order", payload={}
        )

        def stop_after_profile(workflow_id: str, role: str) -> str:
            self.assertEqual((workflow_id, role), ("wf-order", "architect"))
            raise RuntimeError("profile-resolved-after-snapshot")

        worker._profile_for_role = stop_after_profile
        with self.assertRaisesRegex(RuntimeError, "profile-resolved-after-snapshot"):
            asyncio.run(worker._run_architecture_stage({"payload": {"stage": "architect"}}))

    def test_resume_does_not_reclaim_a_live_architecture_stage(self) -> None:
        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        worker._effect_snapshot = lambda _effect: SimpleNamespace(
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch-live",
            state="ARCHITECT_RUNNING",
        )
        worker.repository.read_lease = lambda _key: {
            "owner_id": "inv-live",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
        }

        result = asyncio.run(worker._resume_aggregate({"payload": {}}))

        self.assertEqual(result, {"status": "already_running", "active_worker_id": "inv-live"})

    def test_architecture_enqueue_does_not_reclaim_a_live_stage(self) -> None:
        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        worker._effect_snapshot = lambda _effect: SimpleNamespace(
            workflow_id="wf-live",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch-live",
            state="ARCHITECT_RUNNING",
            payload={},
        )
        worker._profile_for_role = lambda *_args: "software_engineering.v2_architect"
        worker.repository.read_snapshot = lambda *_args: SimpleNamespace(state="ACTIVE")
        worker.repository.read_lease = lambda _key: {
            "owner_id": "inv-live",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
        }

        result = asyncio.run(
            worker._run_architecture_stage(
                {
                    "effect_id": "eff-duplicate",
                    "effect_key": "architecture:duplicate",
                    "payload": {"stage": "architect"},
                }
            )
        )

        self.assertEqual(result, {"status": "already_running", "active_worker_id": "inv-live"})

    def test_architect_stage_uses_prepared_read_only_workspace(self) -> None:
        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        revision = SimpleNamespace(
            workflow_id="wf-requirements-scope",
            aggregate_id="arch-requirements-scope",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            state="ARCHITECT_RUNNING",
            version=1,
            payload={},
        )
        worker._effect_snapshot = lambda _effect: revision
        worker._profile_for_role = lambda *_args: "software_engineering.v2_architect"
        worker._architecture_stage_prompt = lambda *_args: ("normalize", {})
        worker.repository.claim_lease = lambda *_args, **_kwargs: SimpleNamespace(fencing_token=3)
        worker.repository.release_lease = lambda *_args, **_kwargs: None
        worker.repository.engine.legal_actions = lambda *_args: ()
        worker.repository.read_snapshot = lambda *_args: SimpleNamespace(state="ACTIVE")
        dispatched: list[str] = []

        def capture_dispatch(action, **_kwargs):
            dispatched.append(action.action_type)
            return SimpleNamespace(snapshot=revision)

        worker.repository.dispatch = capture_dispatch

        async def capture_scope(**kwargs):
            self.assertEqual(
                kwargs["invocation_id"],
                architect_session_id(revision.workflow_id, revision.aggregate_id),
            )
            self.assertIsNone(kwargs["workspace_override"])
            self.assertTrue(kwargs["prepare_workspace"])
            raise RuntimeError("architect-scope-captured")

        worker._run_profile = capture_scope
        with self.assertRaisesRegex(RuntimeError, "architect-scope-captured"):
            asyncio.run(
                worker._run_architecture_stage(
                    {
                        "effect_id": "eff-requirements-scope",
                        "effect_key": "event:0",
                        "payload": {"stage": "architect"},
                    }
                )
            )
        self.assertEqual(dispatched, ["REBIND_ARCHITECT"])

    def test_producer_and_repair_admission_reuse_one_coder_session(self) -> None:
        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        claims: list[str] = []
        actions: list[ActionEnvelope] = []

        def claim(_resource: str, owner_id: str, **_kwargs):
            claims.append(owner_id)
            return SimpleNamespace(fencing_token=len(claims))

        worker.repository.claim_lease = claim
        worker.repository.dispatch = lambda action, **_kwargs: actions.append(action)
        snapshots = iter(
            (
                SimpleNamespace(
                    workflow_id="wf-coder-session",
                    aggregate_id="node-coder-session",
                    state="QUEUED",
                    version=1,
                    payload={"candidate_cycle": 0, "module_name": "router"},
                ),
                SimpleNamespace(
                    workflow_id="wf-coder-session",
                    aggregate_id="node-coder-session",
                    state="REPAIR_QUEUED",
                    version=7,
                    payload={"candidate_cycle": 1, "module_name": "router"},
                ),
            )
        )
        worker._effect_snapshot = lambda _effect: next(snapshots)

        with patch.object(worker, "_start_graph_cycle_assignment"):
            worker._admit_node_worker(
                {"effect_key": "producer"},
                action_type="START_PRODUCING",
                activation=RoleActivation(OrchestrationRole.IMPLEMENTATION, RoleMode.PRODUCE),
            )
            worker._admit_node_worker(
                {"effect_key": "repair"},
                action_type="START_REPAIR",
                activation=RoleActivation(OrchestrationRole.IMPLEMENTATION, RoleMode.REPAIR),
            )

        expected = coder_session_id("wf-coder-session", "router")
        self.assertEqual(claims, [expected, expected])
        self.assertEqual([item.payload["active_worker_id"] for item in actions], [expected, expected])

    def test_repeated_review_cycles_reuse_module_verifier_session(self) -> None:
        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        claims: list[str] = []
        actions: list[ActionEnvelope] = []

        def claim(_resource: str, owner_id: str, **_kwargs):
            claims.append(owner_id)
            return SimpleNamespace(fencing_token=len(claims))

        worker.repository.claim_lease = claim
        worker.repository.dispatch = lambda action, **_kwargs: actions.append(action)
        snapshots = iter(
            (
                SimpleNamespace(
                    workflow_id="wf-verifier-session",
                    aggregate_id="node-verifier-session",
                    state="REVIEW_QUEUED",
                    version=1,
                    payload={
                        "candidate_cycle": 1,
                        "candidate_digest": "candidate-a",
                        "module_name": "router",
                    },
                ),
                SimpleNamespace(
                    workflow_id="wf-verifier-session",
                    aggregate_id="node-verifier-session",
                    state="REVIEW_QUEUED",
                    version=9,
                    payload={
                        "candidate_cycle": 2,
                        "candidate_digest": "candidate-b",
                        "module_name": "router",
                    },
                ),
            )
        )
        worker._effect_snapshot = lambda _effect: next(snapshots)

        with patch.object(worker, "_start_graph_cycle_assignment"):
            worker._admit_node_worker(
                {"effect_key": "review-first"},
                action_type="START_REVIEW",
                activation=RoleActivation(OrchestrationRole.VERIFIER, RoleMode.MODULE),
            )
            worker._admit_node_worker(
                {"effect_key": "review-after-repair"},
                action_type="START_REVIEW",
                activation=RoleActivation(OrchestrationRole.VERIFIER, RoleMode.MODULE),
            )

        verifier_id = module_verifier_session_id(
            "wf-verifier-session",
            "router",
        )
        expected = [verifier_id, verifier_id]
        self.assertEqual(claims, expected)
        self.assertEqual(
            [item.payload["active_worker_id"] for item in actions],
            expected,
        )

    def test_node_acceptance_preserves_module_sessions(self) -> None:
        processor = MinionV2OutboxProcessor(MinionV2WorkflowService(self.runtime_root))
        node = SimpleNamespace(
            workflow_id="wf-module-pass",
            aggregate_id="node-module-pass",
            payload={
                "node_kind": "unit",
                "epoch_id": "epoch-module-pass",
                "role_session_generation": 0,
                "module_name": "router",
            },
        )
        processor._effect_snapshot = lambda _effect: node
        completed: list[str] = []
        processor.repository.complete_role_session = lambda invocation_id, **_kwargs: completed.append(
            invocation_id
        ) or True
        processor.repository.list_workflow_snapshots = lambda _workflow_id: []

        with patch("pal.minion.v2.orchestration.DagScheduler.schedule_ready_nodes"):
            processor._node_accepted({"effect_key": "node-pass"})

        self.assertEqual(completed, [])

    def test_snapshot_effect_rebinds_expired_lease_and_reacquires_workspace_lock(self) -> None:
        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        workspace = self.runtime_root / "snapshot-worktree"
        workspace.mkdir()
        node = SimpleNamespace(
            workflow_id="wf-rebind",
            aggregate_id="node-rebind",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            state="SNAPSHOTTING",
            version=8,
            payload={
                "module_name": "router",
                "active_worker_id": coder_session_id("wf-rebind", "router"),
                "lease_resource_key": "node:node-rebind:writer",
                "fencing_token": 1,
                "workspace_path": str(workspace),
                "workspace_fingerprint": "stable-tree",
                "execution_adapter": "artifact_bundle",
            },
        )
        worker.repository.assert_fencing_token = lambda *_args: (_ for _ in ()).throw(StaleFencingToken("expired"))
        worker.repository.read_lease = lambda _key: {
            "owner_id": "",
            "fencing_token": 1,
            "expires_at": "2026-01-01T00:00:00+00:00",
            "metadata": {},
        }
        worker.repository.claim_lease = lambda *_args, **_kwargs: SimpleNamespace(fencing_token=2)
        worker._workspace_fingerprint = lambda *_args: "stable-tree"
        captured: list[ActionEnvelope] = []

        def dispatch(action):
            captured.append(action)
            rebound = SimpleNamespace(
                **{key: getattr(node, key) for key in ("workflow_id", "aggregate_id", "aggregate_type", "state")},
                version=9,
                payload={**node.payload, **action.payload},
            )
            return SimpleNamespace(snapshot=rebound)

        worker.repository.dispatch = dispatch
        with patch("pal.minion.v2.semantic_orchestration.orchestrator.workspace_process_holders", return_value=()):
            rebound = asyncio.run(
                worker._ensure_node_effect_lease(
                    node,
                    action_type="REBIND_SNAPSHOTTER",
                    activation=RoleActivation(
                        OrchestrationRole.IMPLEMENTATION, RoleMode.PRODUCE
                    ),
                )
            )

        self.assertEqual(captured[0].action_type, "REBIND_SNAPSHOTTER")
        self.assertEqual(rebound.payload["fencing_token"], 2)
        self.assertTrue(worker._worktree_locks.is_held("node-rebind"))
        worker._worktree_locks.release("node-rebind")

    def test_failed_effect_rebind_releases_only_the_newly_claimed_lease(self) -> None:
        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        workspace = self.runtime_root / "failed-rebind-worktree"
        workspace.mkdir()
        owner_id = coder_session_id("wf-failed-rebind", "router")
        node = SimpleNamespace(
            workflow_id="wf-failed-rebind",
            aggregate_id="node-failed-rebind",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            state="SNAPSHOTTING",
            version=8,
            payload={
                "module_name": "router",
                "active_worker_id": owner_id,
                "lease_resource_key": "node:node-failed-rebind:writer",
                "fencing_token": 1,
                "workspace_path": str(workspace),
                "workspace_fingerprint": "stable-tree",
                "execution_adapter": "artifact_bundle",
            },
        )
        worker.repository.assert_fencing_token = lambda *_args: (_ for _ in ()).throw(
            StaleFencingToken("expired")
        )
        worker.repository.read_lease = lambda _key: {
            "owner_id": "",
            "fencing_token": 1,
            "expires_at": "2026-01-01T00:00:00+00:00",
            "metadata": {},
        }
        worker.repository.claim_lease = lambda *_args, **_kwargs: SimpleNamespace(
            fencing_token=2
        )
        released: list[tuple[str, str, int]] = []
        worker.repository.release_lease = lambda resource, owner, token: released.append(
            (resource, owner, token)
        )
        worker.repository.dispatch = lambda _action: (_ for _ in ()).throw(
            RuntimeError("rebind dispatch failed")
        )

        with patch(
            "pal.minion.v2.semantic_orchestration.orchestrator.workspace_process_holders",
            return_value=(),
        ):
            with self.assertRaisesRegex(RuntimeError, "rebind dispatch failed"):
                asyncio.run(
                    worker._ensure_node_effect_lease(
                        node,
                        action_type="REBIND_SNAPSHOTTER",
                        activation=RoleActivation(
                            OrchestrationRole.IMPLEMENTATION,
                            RoleMode.PRODUCE,
                        ),
                    )
                )

        self.assertEqual(
            released,
            [("node:node-failed-rebind:writer", owner_id, 2)],
        )

    def test_stale_snapshot_effect_cannot_release_current_role_resources(self) -> None:
        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        node = SimpleNamespace(
            workflow_id="wf-stale-snapshot",
            aggregate_id="node-stale-snapshot",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            state="ACCEPTED",
            version=9,
            payload={
                "module_name": "router",
                "execution_adapter": "software_git.v2",
            },
        )
        worker._effect_snapshot = lambda _effect: node
        released: list[tuple[str, str, int]] = []
        worker.repository.read_lease = lambda _resource: {
            "owner_id": "inv-old-coder",
            "fencing_token": 7,
            "metadata": {"aggregate_id": node.aggregate_id},
        }
        worker.repository.release_lease = (
            lambda resource, owner, token: released.append((resource, owner, token))
        )

        result = asyncio.run(
            worker.execute_semantic_effect(
                {
                    "effect_key": "stale-snapshot-effect",
                    "effect_type": "snapshot_implementation_result",
                    "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                    "aggregate_id": node.aggregate_id,
                    "payload": {
                        "_causal_context": {
                            "aggregate_version": 4,
                            "target_state": "SNAPSHOTTING",
                            "active_worker_id": "inv-old-coder",
                            "lease_resource_key": (
                                "node:node-stale-snapshot:writer"
                            ),
                            "fencing_token": 7,
                        }
                    },
                }
            )
        )

        self.assertEqual(result["status"], "superseded")
        self.assertEqual(released, [])

    def test_replayed_pause_effect_cannot_stop_resumed_worker(self) -> None:
        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        node = SimpleNamespace(
            workflow_id="wf-pause-replay",
            aggregate_id="node-pause-replay",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            state="PAUSE_REQUESTED",
            version=12,
            payload={
                "active_worker_id": "inv-new-worker",
                "lease_resource_key": "node:node-pause-replay:writer",
                "fencing_token": 9,
            },
        )
        worker._effect_snapshot = lambda _effect: node
        stopped: list[str] = []

        async def stop_current(_effect, *, cancel, confirm=True):
            stopped.append(node.payload["active_worker_id"])
            return {}

        worker._stop_node_worker = stop_current
        result = asyncio.run(
            worker.execute_semantic_effect(
                {
                    "effect_key": "old-pause-effect",
                    "effect_type": "pause_role",
                    "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                    "aggregate_id": node.aggregate_id,
                    "payload": {
                        "_causal_context": {
                            "aggregate_version": 5,
                            "target_state": "PAUSE_REQUESTED",
                            "active_worker_id": "inv-old-worker",
                            "lease_resource_key": (
                                "node:node-pause-replay:writer"
                            ),
                            "fencing_token": 3,
                        }
                    },
                }
            )
        )

        self.assertEqual(result["status"], "superseded")
        self.assertEqual(stopped, [])

    def test_snapshot_effect_reuses_live_lease_and_reacquires_workspace_lock(self) -> None:
        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        workspace = self.runtime_root / "live-snapshot-worktree"
        workspace.mkdir()
        node = SimpleNamespace(
            workflow_id="wf-live-snapshot",
            aggregate_id="node-live-snapshot",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            state="SNAPSHOTTING",
            version=4,
            payload={
                "module_name": "router",
                "active_worker_id": coder_session_id("wf-live-snapshot", "router"),
                "lease_resource_key": "node:node-live-snapshot:writer",
                "fencing_token": 3,
                "workspace_path": str(workspace),
                "workspace_fingerprint": "stable-tree",
                "execution_adapter": "artifact_bundle",
            },
        )
        worker.repository.assert_fencing_token = lambda *_args: None
        worker.repository.read_lease = lambda _key: {
            "owner_id": coder_session_id("wf-live-snapshot", "router"),
            "fencing_token": 3,
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
            "metadata": {},
        }
        worker.repository.renew_lease = lambda *_args, **_kwargs: None
        worker._workspace_fingerprint = lambda *_args: "stable-tree"

        with patch("pal.minion.v2.semantic_orchestration.orchestrator.workspace_process_holders", return_value=()):
            rebound = asyncio.run(
                worker._ensure_node_effect_lease(
                    node,
                    action_type="REBIND_SNAPSHOTTER",
                    activation=RoleActivation(
                        OrchestrationRole.IMPLEMENTATION, RoleMode.PRODUCE
                    ),
                )
            )

        self.assertIs(rebound, node)
        self.assertTrue(worker._worktree_locks.is_held("node-live-snapshot"))
        worker._worktree_locks.release("node-live-snapshot")

    def test_fresh_effect_lease_is_renewed_before_worker_spawn(self) -> None:
        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        renewals: list[tuple[str, str, int, int]] = []
        worker.repository.assert_fencing_token = lambda *_args: None
        worker.repository.read_lease = lambda _key: {
            "owner_id": "inv-fresh",
            "fencing_token": 4,
            "metadata": {},
        }
        worker.repository.renew_lease = lambda resource, owner, token, *, ttl_seconds: renewals.append(
            (resource, owner, token, ttl_seconds)
        )

        reused = asyncio.run(
            worker._reuse_or_retire_effect_lease(
                resource_key="node:review:fresh",
                owner_id="inv-fresh",
                fencing_token=4,
                worker_label="test reviewer",
            )
        )

        self.assertTrue(reused)
        self.assertEqual(renewals, [("node:review:fresh", "inv-fresh", 4, 120)])

    def test_live_unmanaged_worker_is_reaped_and_rebound_with_new_fence(self) -> None:
        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        workspace = self.runtime_root / "review-restart"
        workspace.mkdir()
        verifier_id = module_verifier_session_id(
            "wf-review-restart",
            "router",
        )
        node = SimpleNamespace(
            workflow_id="wf-review-restart",
            aggregate_id="node-review-restart",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            state="REVIEWING",
            version=5,
            payload={
                "module_name": "router",
                "candidate_cycle": 1,
                "active_worker_id": verifier_id,
                "lease_resource_key": "node:node-review-restart:review",
                "fencing_token": 6,
                "workspace_path": str(workspace),
                "candidate_digest": "candidate-restart",
            },
        )
        lease = {
            "owner_id": verifier_id,
            "fencing_token": 6,
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
            "metadata": {"process_group_id": 99999999},
        }
        worker.repository.assert_fencing_token = lambda *_args: None
        worker.repository.read_lease = lambda _key: dict(lease)
        released: list[int] = []

        def release(_resource, _owner, token):
            released.append(token)
            lease["owner_id"] = ""

        worker.repository.release_lease = release
        worker.repository.claim_lease = lambda *_args, **_kwargs: SimpleNamespace(fencing_token=7)
        captured: list[ActionEnvelope] = []

        def dispatch(action):
            captured.append(action)
            return SimpleNamespace(
                snapshot=SimpleNamespace(
                    workflow_id=node.workflow_id,
                    aggregate_id=node.aggregate_id,
                    aggregate_type=node.aggregate_type,
                    state=node.state,
                    version=node.version + 1,
                    payload={**node.payload, **action.payload},
                )
            )

        worker.repository.dispatch = dispatch

        async def reaped(_process_group, *, timeout_seconds):
            self.assertEqual(timeout_seconds, 5.0)
            return True

        with patch("pal.minion.v2.semantic_orchestration.orchestrator.terminate_process_group", side_effect=reaped):
            rebound = asyncio.run(
                worker._ensure_node_effect_lease(
                    node,
                    action_type="REBIND_REVIEWER",
                    activation=RoleActivation(OrchestrationRole.VERIFIER, RoleMode.MODULE),
                )
            )

        self.assertEqual(released, [6])
        self.assertEqual(captured[0].action_type, "REBIND_REVIEWER")
        self.assertEqual(rebound.payload["fencing_token"], 7)

    def test_architect_quiesce_releases_managed_lsp_before_holder_check(self) -> None:
        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        workspace = self.runtime_root / "architecture-worktree"
        workspace.mkdir()
        revision = SimpleNamespace(
            workflow_id="wf-quiesce-lsp",
            aggregate_id="arch-quiesce-lsp",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            state="ARCHITECT_QUIESCING",
            version=4,
            payload={
                "active_worker_id": "inv-architect",
                "lease_resource_key": "architecture:arch-quiesce-lsp:writer",
                "fencing_token": 3,
                "architecture_workspace_path": str(workspace),
            },
        )

        async def ensure_effect_lease(_snapshot, *, action_type):
            self.assertEqual(action_type, "REBIND_ARCHITECT_QUIESCER")
            return revision

        released: list[Path] = []

        async def release_lsp(target: Path):
            released.append(target)
            return {"status": "ok", "released_count": 1}

        holder_checks: list[bool] = []

        def holders(_target: Path):
            holder_checks.append(bool(released))
            return ()

        worker._effect_snapshot = lambda _effect: revision
        worker._ensure_architecture_effect_lease = ensure_effect_lease  # type: ignore[method-assign]
        worker._release_managed_lsp_workspace = release_lsp  # type: ignore[method-assign]
        worker.repository.assert_fencing_token = lambda *_args: None
        worker.repository.read_lease = lambda _key: {"metadata": {}}
        worker.repository.read_snapshot = lambda *_args: SimpleNamespace(version=4)
        actions: list[ActionEnvelope] = []
        worker.repository.dispatch = lambda action, **_kwargs: actions.append(action)

        with (
            patch("pal.minion.v2.semantic_orchestration.orchestrator.workspace_process_holders", side_effect=holders),
            patch("pal.minion.v2.semantic_orchestration.orchestrator.workspace_content_fingerprint", return_value="tree"),
        ):
            asyncio.run(worker._quiesce_architect_role({"effect_key": "quiesce-lsp"}))

        self.assertEqual(released, [workspace])
        self.assertTrue(holder_checks)
        self.assertTrue(all(holder_checks))
        self.assertEqual(actions[0].action_type, "ARCHITECT_QUIESCED")
        worker._worktree_locks.release(revision.aggregate_id)

    def test_paused_architecture_stage_does_not_restart_worker(self) -> None:
        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        revision = SimpleNamespace(
            workflow_id="wf-paused-stage",
            aggregate_id="arch-paused-stage",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            state="PAUSED",
            version=4,
            payload={},
        )
        workflow = SimpleNamespace(state="PAUSED")
        worker._effect_snapshot = lambda _effect: revision
        worker._profile_for_role = lambda *_args: "software_engineering.v2_architect"
        worker.repository.read_snapshot = lambda *_args: workflow
        worker.repository.claim_lease = lambda *_args, **_kwargs: self.fail("paused effect claimed a lease")

        result = asyncio.run(
            worker._run_architecture_stage(
                {
                    "effect_id": "eff-paused-stage",
                    "effect_key": "event:0",
                    "payload": {"stage": "architect"},
                }
            )
        )

        self.assertEqual(result, {"status": "superseded"})

    def test_skeleton_architecture_review_view_preserves_complete_semantic_submission(self) -> None:
        submission = {
            "modules": {
                "router": {
                    "module_kind": "implementation",
                    "depends_on": [],
                    "consumes": [],
                    "paths": {"implementation_scopes": [{"kind": "file", "path": "src/router.cpp"}]},
                }
            },
            "future_semantic_section": {"boundary": "preserve this without a worker whitelist"},
        }
        view = _skeleton_architecture_review_view(
            {
                "submission": submission,
                "changed_paths": ["include/router.h", "src/router.cpp"],
                "skeleton_commit_sha": "manager-owned-sha",
                "requirements_ref": {"sha256": "manager-owned-requirements-ref"},
            }
        )

        self.assertEqual(view["modules"], submission["modules"])
        self.assertEqual(view["future_semantic_section"], submission["future_semantic_section"])
        self.assertEqual(view["changed_paths"], ["include/router.h", "src/router.cpp"])
        self.assertEqual(
            view["manager_derived_verification_policy"],
            {
                "architect_declares_test_scopes": False,
                "tests_are_product_scenarios": False,
                "developer_corpora": {
                    "router": {
                        "kind": "directory",
                        "path": "tests/router/developer",
                        "owner": "coder",
                        "verifier_access": "read_only",
                    }
                },
                "verification_corpora": {
                    "router": {
                        "kind": "directory",
                        "path": "tests/router/verifier",
                        "owner": "verifier",
                        "coder_access": "read_only",
                    }
                },
            },
        )
        self.assertNotIn("skeleton_commit_sha", view)
        self.assertNotIn("requirements_ref", view)

    def test_architecture_reviewer_binds_human_edit_instruction_on_every_attempt(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        instruction_ref = service.artifacts.put_json(
            {
                "instruction": "Close the Ctrl-C lifecycle and dependency graph.",
            },
            artifact_type="ArchitectureEditInstructionArtifact",
        )
        references: dict[str, object] = {}
        revision = SimpleNamespace(
            payload={"edit_instruction_ref": instruction_ref.to_dict()}
        )

        bound = _bind_architecture_edit_instruction_for_review(
            references,
            revision,
        )

        self.assertTrue(bound)
        self.assertEqual(references["edit_instruction"], instruction_ref)
        retry_references: dict[str, object] = {}
        self.assertTrue(
            _bind_architecture_edit_instruction_for_review(
                retry_references,
                revision,
            )
        )
        self.assertEqual(retry_references["edit_instruction"], instruction_ref)

    def test_architecture_reviewer_omits_repair_reference_without_human_edit(self) -> None:
        references: dict[str, object] = {}

        bound = _bind_architecture_edit_instruction_for_review(
            references,
            SimpleNamespace(payload={}),
        )

        self.assertFalse(bound)
        self.assertEqual(references, {})

    def test_skeleton_architect_submission_hands_live_lease_to_quiescer(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        worker = SemanticOrchestrator(service)
        requirements_ref = service.task_ledger.publish(
            title="Routing",
            task_spec={"objective": "Route requests deterministically."},
            actor="test",
            source_channel="test",
        )
        workspace_snapshot_ref = service.artifacts.put_json(
            {"snapshot_commit_sha": "base"}, artifact_type="WorkspaceSnapshotArtifact"
        )
        prompt_ref = service.artifacts.put_json({}, artifact_type="RolePromptPackArtifact")
        terminal_ref = service.artifacts.put_json({}, artifact_type="WorkerResponseArtifact")
        architecture_worktree = self.runtime_root / "architecture-worktree"
        architecture_worktree.mkdir()
        submission_path = self.runtime_root / "architect.yaml"
        contract_definition = ArchitectureTemplateCompiler().compile(
            "software_engineering.v1"
        )
        submission_path.write_text(
            json.dumps(
                {
                    "contract_schema": "software_engineering.v1",
                    "contract": contract_definition.example,
                    "work_items": [
                        {
                            "kind": "phase",
                            "summary": "contract projection",
                            "status": "completed",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        revision = SimpleNamespace(
            workflow_id="wf-lease-handoff",
            aggregate_id="arch-lease-handoff",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            state="ARCHITECT_QUEUED",
            version=1,
            payload={"requirements_ref": requirements_ref.to_dict()},
        )
        running = SimpleNamespace(**{**revision.__dict__, "state": "ARCHITECT_RUNNING", "version": 2})
        workflow = SimpleNamespace(payload={"request_ref": {}}, state="ACTIVE")
        worker._architecture_worker_suppressed = lambda *_args, **_kwargs: False
        worker._profile_for_role = lambda *_args: "software_engineering.v2_architect"
        worker.service.skeleton.provision_architecture_workspace = lambda **_kwargs: ArchitectureWorkspace(
            worktree=architecture_worktree,
            common_git_dir=self.runtime_root / "project.git",
            base_sha="base",
            base_tree_sha="tree",
            original_head="",
            source_fingerprint="source",
            workspace_snapshot_ref=workspace_snapshot_ref,
        )
        worker.repository.read_snapshot = lambda aggregate_type, _aggregate_id: (
            workflow if aggregate_type == AggregateType.WORKFLOW else running
        )
        worker.repository.claim_lease = lambda *_args, **_kwargs: SimpleNamespace(fencing_token=7)
        worker.repository.engine.legal_actions = lambda *_args: {"START_ARCHITECT"}
        actions: list[ActionEnvelope] = []

        def dispatch(action: ActionEnvelope, **_settlement):
            actions.append(action)
            return SimpleNamespace(snapshot=running)

        worker.repository.dispatch = dispatch
        worker.repository.read_role_assignment = lambda _assignment_id: {
            "assignment_id": "assignment-lease-handoff",
            "state": RoleAssignmentState.RESULT_RECORDED.value,
            "submission_payload_hash": "lease-handoff-payload",
        }
        worker.repository.record_role_turn = lambda **_kwargs: None
        released: list[tuple[object, ...]] = []
        worker.repository.release_lease = lambda *args: released.append(args)
        async def run_profile(**_kwargs):
            return (
                {
                    "payload": {
                        "artifacts": [
                            {
                                "path": str(submission_path),
                                "role": "primary",
                            },
                        ],
                        "role_assignment_id": "assignment-lease-handoff",
                        "session_turn_index": 1,
                    }
                },
                prompt_ref,
                terminal_ref,
            )

        worker._run_profile = run_profile
        with patch(
            "pal.minion.v2.semantic_orchestration.orchestrator.workflow_request_from_snapshot",
            return_value={"workspace": {"kind": "existing_repo"}, "references": []},
        ):
            result = asyncio.run(
                worker._run_skeleton_architecture_stage(
                    {"effect_id": "eff-handoff", "effect_key": "event:0"}, revision
                )
            )

        self.assertEqual(
            result["provider_request_id"],
            architect_session_id(revision.workflow_id, revision.aggregate_id),
        )
        self.assertEqual([action.action_type for action in actions], ["START_ARCHITECT", "ARCHITECT_SUBMITTED"])
        self.assertIn(
            "architect_checklist_ref",
            actions[-1].payload,
        )
        self.assertEqual(released, [])

    def test_skeleton_architect_failure_releases_writer_lease(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        worker = SemanticOrchestrator(service)
        requirements_ref = service.task_ledger.publish(
            title="Routing",
            task_spec={"objective": "Route requests deterministically."},
            actor="test",
            source_channel="test",
        )
        snapshot_ref = service.artifacts.put_json({}, artifact_type="WorkspaceSnapshotArtifact")
        worktree = self.runtime_root / "failed-architecture-worktree"
        worktree.mkdir()
        revision = SimpleNamespace(
            workflow_id="wf-lease-failure",
            aggregate_id="arch-lease-failure",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            state="ARCHITECT_QUEUED",
            version=1,
            payload={"requirements_ref": requirements_ref.to_dict()},
        )
        running = SimpleNamespace(**{**revision.__dict__, "state": "ARCHITECT_RUNNING", "version": 2})
        worker._architecture_worker_suppressed = lambda *_args, **_kwargs: False
        worker._profile_for_role = lambda *_args: "software_engineering.v2_architect"
        worker.service.skeleton.provision_architecture_workspace = lambda **_kwargs: ArchitectureWorkspace(
            worktree=worktree,
            common_git_dir=self.runtime_root / "failed.git",
            base_sha="base",
            base_tree_sha="tree",
            original_head="",
            source_fingerprint="source",
            workspace_snapshot_ref=snapshot_ref,
        )
        worker.repository.read_snapshot = lambda aggregate_type, _aggregate_id: (
            SimpleNamespace(payload={}, state="ACTIVE")
            if aggregate_type == AggregateType.WORKFLOW
            else running
        )
        worker.repository.claim_lease = lambda *_args, **_kwargs: SimpleNamespace(fencing_token=9)
        worker.repository.engine.legal_actions = lambda *_args: {"START_ARCHITECT"}
        worker.repository.dispatch = lambda _action, **_kwargs: SimpleNamespace(
            snapshot=running
        )
        released: list[tuple[object, ...]] = []
        worker.repository.release_lease = lambda *args: released.append(args)

        async def fail_profile(**_kwargs):
            raise RuntimeError("architect failed")

        worker._run_profile = fail_profile
        with patch(
            "pal.minion.v2.semantic_orchestration.orchestrator.workflow_request_from_snapshot",
            return_value={"workspace": {"kind": "existing_repo"}, "references": []},
        ):
            with self.assertRaisesRegex(RuntimeError, "architect failed"):
                asyncio.run(
                    worker._run_skeleton_architecture_stage(
                        {"effect_id": "eff-failure", "effect_key": "event:0"}, revision
                    )
                )

        self.assertEqual(
            released,
            [
                (
                    "architecture:arch-lease-failure:writer",
                    architect_session_id(revision.workflow_id, revision.aggregate_id),
                    9,
                )
            ],
        )

    def test_architect_revision_handoff_binds_only_semantic_scope(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        worker = SemanticOrchestrator(service)
        request_ref = service.artifacts.put_json({"goal": "research"}, artifact_type="WorkflowRequestArtifact")
        requirements_ref = service.task_ledger.publish(
            title="Architecture repair",
            task_spec={"objective": "Repair the module boundary."},
            actor="test",
            source_channel="test",
        )
        manifest_ref = service.artifacts.put_json(
            {
                "requirements_ref": requirements_ref.to_dict(),
                "contract_schema": "general.v1",
                "contract": {
                    "modules": {
                        "foundation": {
                            "responsibility": "Own the repaired boundary."
                        }
                    },
                },
            },
            artifact_type="ContractArtifact",
        )
        finding_ref = service.artifacts.put_json(
            {
                "findings": [
                    {
                        "finding_kind": "contract_defect",
                        "summary": "repair the module boundary",
                        "revision_targets": [
                            {"section": "unit", "id": "foundation", "fields": ["ownership"], "operation": "update"}
                        ],
                    }
                ]
            },
            artifact_type="ArchitectureFindingArtifact",
        )
        workflow = SimpleNamespace(payload={"request_ref": request_ref.to_dict()})
        revision = SimpleNamespace(
            workflow_id="wf-research-revision",
            aggregate_id="arch-research-revision",
            payload={
                "request_ref": request_ref.to_dict(),
                "requirements_ref": requirements_ref.to_dict(),
                "base_architecture_manifest_ref": manifest_ref.to_dict(),
                "finding_artifact_ref": finding_ref.to_dict(),
                "research_mode": "local_only",
            },
        )
        worker.repository.read_snapshot = lambda *_args: workflow

        with patch("pal.minion.v2.semantic_orchestration.orchestrator.workflow_request_from_snapshot", return_value={"references": []}):
            instruction, refs = worker._architecture_stage_prompt("architect", revision)

        self.assertIn("revision_scope", refs)
        self.assertNotIn("base_global_constraints", refs)
        self.assertNotIn("revision_finding", refs)
        scope = service.artifacts.read_json(refs["revision_scope"])
        self.assertEqual(
            scope["findings"][0]["summary"],
            "repair the module boundary",
        )
        self.assertNotIn("contract_schema", scope)
        self.assertIn("guided revision", instruction)
        self.assertIn("repair guidance, not a write fence", instruction)
        self.assertIn("read revision_scope first", instruction)

    def test_semantic_reference_prompt_hides_storage_path_and_uses_normal_file_tools(self) -> None:
        pack = MinionInvocationPack(
            invocation_id="inv_bound_prompt",
            goal="inspect",
            workspace={
                "reference_paths": [
                    {
                        "name": "revision_finding",
                        "path": "/host-only/artifacts/secret.json",
                        "bound_input": True,
                        "required": True,
                        "truth_source": True,
                    }
                ]
            },
        )
        prompt = render_minion_task_prompt(pack)
        self.assertIn("reference:revision_finding", prompt)
        self.assertIn("ordinary file/search tools", prompt)
        self.assertNotIn("tree -a", prompt)
        self.assertNotIn("find ", prompt)
        self.assertNotIn("input_read", prompt)
        self.assertNotIn("/host-only/artifacts/secret.json", prompt)

    def test_repair_bill_prompt_uses_semantic_reference_roots(self) -> None:
        pack = MinionInvocationPack(
            invocation_id="inv_repair_prompt",
            goal="repair",
            workspace={
                "reference_paths": [
                    {
                        "name": "repair_bill",
                        "path": "/host-only/artifacts/repair.json",
                        "bound_input": True,
                        "required": True,
                    },
                    {
                        "name": "unit_work_view",
                        "path": "/host-only/artifacts/view.json",
                        "bound_input": True,
                        "required": True,
                    },
                ]
            },
        )

        prompt = render_minion_task_prompt(pack)

        self.assertIn("reference:repair_bill", prompt)
        self.assertIn("reference:unit_work_view", prompt)
        self.assertNotIn("repair_checklist", prompt)
        self.assertNotIn("input_read", prompt)


    def test_profile_worker_preserves_scheduler_lease_owner_id(self) -> None:
        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        leased_invocation_id = "inv_scheduler_owned"
        scheduler_lease = worker.repository.claim_lease(
            "architecture:arch-lease-owner:architect",
            leased_invocation_id,
            ttl_seconds=120,
        )
        captured: dict[str, object] = {}
        admission_events: list[str] = []
        self._create_role_scope(
            worker.service,
            workflow_id="wf-lease-owner",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="arch-lease-owner",
            module_name="lease_owner",
        )

        def capture_invocation(**kwargs) -> None:
            captured["invocation_id"] = str(kwargs["invocation_id"])
            raise RuntimeError("stop-after-invocation-record")

        acquire_process_slot = worker._role_supervisor.acquire_process_slot
        claim_role_assignment = worker.repository.claim_role_assignment

        async def capture_process_slot(run_id: str):
            admission_events.append("process_permit")
            return await acquire_process_slot(run_id)

        def capture_attempt(*args, **kwargs):
            admission_events.append("attempt")
            return claim_role_assignment(*args, **kwargs)

        worker.repository.read_snapshot = lambda *_args: SimpleNamespace(
            payload={
                "family_binding_ref": {"sha256": "binding"},
            }
        )
        worker.repository.record_role_invocation = capture_invocation
        snapshot = SimpleNamespace(
            workflow_id="wf-lease-owner",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="arch-lease-owner",
            payload={
                "research_mode": "local_only",
                "module_name": "lease_owner",
                "execution_adapter": "software_git.v2",
            },
        )
        identity = lambda pack, **_kwargs: pack

        def capture_sandbox_pack(_runtime_root, pack, **_kwargs):
            captured["control_route"] = dict(
                dict(pack.metadata.get("minion_v2") or {}).get("control_route") or {}
            )
            captured["response_key"] = str(
                dict(pack.metadata.get("agent_session") or {}).get("response_key")
                or ""
            )
            return pack

        binding = {
            "schema_version": "7",
            "family_id": "software_engineering",
            "execution_adapter": "software_git.v2",
            "architecture_definition": {
                "specialization_id": "software_engineering.v1",
                "family_id": "software_engineering",
                "generation_hash": "a" * 64,
                "schema_ref": {"sha256": "schema"},
                "template_ref": {"sha256": "template"},
                "satellite_template_ref": {"sha256": "satellite"},
            },
            "policies": {},
            "role_bindings": {
                role: {
                    "participant": "profile",
                    "selector": profile,
                    "role_profile": {
                        "profile_id": profile.rsplit(".", 1)[-1],
                        "profile_group": "software_engineering",
                        "canonical_profile_id": profile,
                        "role": {
                            "kind": role,
                            "modes": {
                                "architect": ["author", "revision"],
                                "reviewer": ["architecture", "standalone"],
                                "implementation": ["produce", "repair"],
                                "verifier": ["module", "system"],
                            }[role],
                            "playbook": {"steps": []},
                        },
                    },
                }
                for role, profile in {
                    "architect": "software_engineering.v2_architect",
                    "reviewer": "software_engineering.v2_reviewer",
                    "implementation": "software_engineering.v2_coder",
                    "verifier": "software_engineering.v2_verifier",
                }.items()
            },
        }
        with (
            patch("pal.minion.v2.semantic_orchestration.orchestrator.workflow_request_from_snapshot", return_value={"workspace": {"kind": "new_project"}}),
            patch.object(
                worker.service.artifacts,
                "read_json",
                return_value=binding,
            ),
            patch.object(
                worker._role_supervisor,
                "acquire_process_slot",
                capture_process_slot,
            ),
            patch.object(
                worker.repository,
                "claim_role_assignment",
                capture_attempt,
            ),
            patch("pal.minion.v2.semantic_orchestration.orchestrator.resolve_pinned_minion_pack", lambda pack, **_kwargs: pack),
            patch("pal.minion.v2.semantic_orchestration.orchestrator.apply_v2_role_capability_policy", identity),
            patch("pal.minion.v2.semantic_orchestration.orchestrator.apply_v2_research_capability_policy", identity),
            patch("pal.minion.v2.semantic_orchestration.orchestrator.sanitize_runner_session_pack", identity),
            patch(
                "pal.minion.v2.semantic_orchestration.orchestrator.with_minion_sandbox_metadata",
                capture_sandbox_pack,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop-after-invocation-record"):
                asyncio.run(
                    worker._run_profile(
                        effect={"effect_id": "eff-lease-owner", "effect_key": "event:0"},
                        snapshot=snapshot,
                        invocation_id=leased_invocation_id,
                        lease_resource="architecture:arch-lease-owner:architect",
                        fencing_token=scheduler_lease.fencing_token,
                        profile="software_engineering.v2_coder",
                        activation=RoleActivation(
                            OrchestrationRole.IMPLEMENTATION,
                            RoleMode.PRODUCE,
                        ),
                        instruction="produce architecture",
                        reference_refs={},
                        prepare_workspace=False,
                    )
                )

        self.assertEqual(captured["invocation_id"], leased_invocation_id)
        self.assertEqual(admission_events[:2], ["process_permit", "attempt"])
        self.assertEqual(worker.active_process_count, 0)
        self.assertEqual(captured["control_route"], {})
        assignments = worker.repository.list_role_assignments(
            workflow_id="wf-lease-owner"
        )
        self.assertEqual(len(assignments), 1)
        self.assertEqual(
            captured["response_key"],
            assignments[0]["assignment_id"],
        )

    def _create_task(self, service: MinionV2WorkflowService, suffix: str) -> str:
        task_id = f"task_{suffix}"
        service.create_task(
            {
                "task_id": task_id,
                "title": suffix,
                "objective": f"Exercise {suffix}",
                "profile": "software_engineering.v2_coder",
                "workspace": {"kind": "new_project", "project_name": suffix},
                "actor": "nathan",
                "source_channel": "socket:test",
            }
        )
        return task_id

    def test_public_provider_binds_sidecar_to_attach_and_detach(self) -> None:
        calls: list[str] = []
        provider = MinionV2PublicProvider(
            runtime_root=self.runtime_root,
            attach_manager=lambda: calls.append("attach") or {"ok": True, "manager_pid": 42},
            detach_manager=lambda: calls.append("detach"),
        )

        attached = provider.attach()
        detached = provider.detach()

        self.assertEqual(calls, ["attach", "detach"])
        self.assertTrue(attached.structured["manager_running"])
        self.assertFalse(detached.structured["manager_running"])

    def test_manager_lifecycle_is_eager_on_attach_and_never_lazy_on_status(self) -> None:
        provider = MinionManagerProvider(self.runtime_root)

        before = inspect_minion(provider)
        self.assertTrue(before.degraded)
        self.assertFalse(minion_socket_path(self.runtime_root).exists())
        self.assertFalse(minion_port_path(self.runtime_root).exists())
        with self.assertRaisesRegex(RuntimeError, "detached"):
            provider.wake_v2()

        try:
            health = provider.attach_manager()
            self.assertTrue(health["ok"])
            self.assertEqual(health["lifecycle_protocol"], "plugin_raii.v1")
            self.assertGreater(int(health["manager_pid"]), 1)
            manager_pid = int(health["manager_pid"])
            self.assertIsNotNone(provider.process)
            self.assertEqual(provider.process.pid, manager_pid)
            self.assertTrue(inspect_minion(provider).manager_running)
            self.assertTrue(minion_socket_path(self.runtime_root).exists() or minion_port_path(self.runtime_root).exists())
        finally:
            provider.detach_manager()

        self.assertFalse(provider._pid_is_running(manager_pid))
        self.assertIsNone(provider.process)
        self.assertFalse(minion_socket_path(self.runtime_root).exists())
        self.assertFalse(minion_port_path(self.runtime_root).exists())
        self.assertTrue(inspect_minion(provider).degraded)

    def test_manager_attach_retires_compatible_unowned_sidecar(self) -> None:
        provider = MinionManagerProvider(self.runtime_root)
        existing_health = {
            "ok": True,
            "health_source": "minion_v2_manager",
            "lifecycle_protocol": "plugin_raii.v1",
            "manager_pid": 111,
        }
        owned_health = {**existing_health, "manager_pid": 222}
        fake_process = SimpleNamespace(pid=222, poll=lambda: None)

        with (
            patch.object(provider._lifecycle_client, "health_sync", side_effect=[existing_health, owned_health]),
            patch.object(provider, "_retire_existing_manager") as retire,
            patch("pal.minion.capabilities.subprocess.Popen", return_value=fake_process),
        ):
            health = provider._start_manager()

        retire.assert_called_once_with(existing_health)
        self.assertIs(provider.process, fake_process)
        self.assertEqual(health["manager_pid"], 222)

    def test_manager_detach_fences_an_unresponsive_but_live_process(self) -> None:
        provider = MinionManagerProvider(self.runtime_root)
        provider.last_health = {"manager_pid": 333}

        with (
            patch.object(provider._lifecycle_client, "shutdown_sync"),
            patch.object(provider, "_wait_for_pid_exit", return_value=False),
            patch.object(provider, "_pid_is_running", side_effect=[True, False]),
            patch.object(provider, "_terminate_manager") as terminate,
            patch.object(provider, "_manager_is_responding", return_value=False),
        ):
            provider._stop_manager_locked()

        terminate.assert_called_once_with(333)

    def test_manager_rejects_pre_raii_sidecar_health(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "protocol is incompatible"):
            MinionManagerProvider._validate_health(
                {"ok": True, "health_source": "minion_v2_manager", "manager_pid": 42}
            )

    def test_minion_plugin_exposes_only_semantic_v2_business_capabilities(self) -> None:
        self.assertEqual(
            MinionV2CapabilitiesMinionV2PublicProviderStartWorkflowInput.__module__,
            "pal.minion.v2.capabilities",
        )
        self.assertEqual(
            MinionV2CapabilitiesMinionV2PublicProviderSubmitHumanDecisionInput.__module__,
            "pal.minion.v2.capabilities",
        )
        core = PalCore()
        register_minion_with_core(core.context, runtime_root=self.runtime_root)
        core.publish_module_capabilities("minion")
        try:
            minion_handle = core.context.module_registry.require("minion")
            self.assertIn(
                EventKind.MINION_CLARIFICATION_RESOLVED,
                minion_handle.event_handlers,
            )
            self.assertIn(
                EventKind.MINION_ARCHITECTURE_REVIEW_RESOLVED,
                minion_handle.event_handlers,
            )
            canonical = {
                descriptor.canonical_path
                for descriptor in core.context.capability_registry.descriptors.values()
                if descriptor.module_id == "minion"
            }
            self.assertEqual(
                canonical,
                {
                    "op_minion_start_workflow",
                    "op_minion_submit_artifact",
                    "intro_minion_task_search",
                    "intro_minion_task_status",
                    "op_minion_resume_workflow",
                    "op_minion_restart_execution",
                    "op_minion_resolve_triage",
                    "op_minion_submit_human_decision",
                    "op_minion_rebind_task_delivery",
                    "op_minion_answer_question",
                    "op_minion_control_workflow",
                    "op_minion_archive_workflow",
                    "intro_minion_catalog_read",
                    "op_minion_catalog_set_profile_override",
                    "op_minion_catalog_reset_profile_override",
                    "op_minion_catalog_set_family_override",
                    "op_minion_catalog_reset_family_override",
                    "op_minion_catalog_refresh",
                },
            )
            schemas = json.dumps(
                {
                    descriptor.canonical_path: descriptor.InputModel.model_json_schema(mode="validation")
                    for descriptor in core.context.capability_registry.descriptors.values()
                    if descriptor.module_id == "minion"
                },
                sort_keys=True,
            )
            for forbidden in (
                "workflow_id",
                "task_id",
                "revision_id",
                "requirement_id",
                "evidence_id",
                "artifact_ref",
                "sha256",
                "decision_token",
            ):
                self.assertNotIn(f'"{forbidden}"', schemas)
            start_descriptor = next(
                descriptor
                for descriptor in core.context.capability_registry.descriptors.values()
                if descriptor.canonical_path == "op_minion_start_workflow"
            )
            generation = core.context.execution_runtime.registry_generation
            self.assertIn("minion_start_workflow", generation.direct_aliases)
            self.assertNotIn("minion_start_workflow", generation.indirect_aliases)
            start_schema = start_descriptor.InputModel.model_json_schema(mode="validation")
            self.assertIn("task_spec", start_schema["properties"])
            self.assertIn("skill_refs", start_schema["properties"])
            self.assertNotIn("source_files", start_schema["properties"])
            task_spec_schema = start_schema["properties"]["task_spec"]
            task_spec_description = task_spec_schema["description"]
            self.assertIn("exact text", task_spec_description)
            self.assertIn("authoritative_text", task_spec_description)
            self.assertIn("Do not summarize, paraphrase, normalize, reinterpret", task_spec_description)
            self.assertIn("omit examples", task_spec_description)
            self.assertIn("path/reference", task_spec_description)
            self.assertIn("exact leading and trailing whitespace", task_spec_description)
            self.assertIn("every final newline", task_spec_description)
            self.assertEqual(
                task_spec_schema["examples"][0]["authoritative_text"],
                "# Complete requirement\n\n"
                "Preserve every example exactly, including `0000 / 0248 / 69`.\n\n",
            )
            goal_description = start_schema["properties"]["goal"]["description"]
            self.assertIn("routing objective only", goal_description)
            self.assertIn("cannot substitute", goal_description)
            workspace_schema = start_schema["$defs"]["MinionV2StartWorkflowWorkspace"]
            self.assertIn("kind", workspace_schema["required"])
            self.assertEqual(
                workspace_schema["properties"]["kind"]["enum"],
                ["new_project", "existing_repo"],
            )
            self.assertIn(
                "create a missing repo_path",
                workspace_schema["properties"]["kind"]["description"],
            )
            self.assertIn(
                "never pass a parent container",
                workspace_schema["properties"]["repo_path"]["description"],
            )
            self.assertIn(
                "same exact project repository",
                workspace_schema["properties"]["repo_root"]["description"],
            )
            self.assertEqual(
                start_descriptor.guidance.purpose,
                "Start one durable Minion workflow from the complete, lossless authoritative task specification and "
                "bind its future delivery to the channel that owns the current turn.",
            )
            self.assertIn("inspect skill_search with read_tool", start_descriptor.guidance.use_when)
            self.assertIn("invoke it through call_tool", start_descriptor.guidance.use_when)
            self.assertIn("ask whether to provide them", start_descriptor.guidance.use_when)
            self.assertIn("explicitly approved names in skill_refs", start_descriptor.guidance.use_when)
            self.assertIn("read the complete content", start_descriptor.guidance.use_when)
            self.assertIn("task_spec.authoritative_text", start_descriptor.guidance.use_when)
            self.assertIn("including every final newline", start_descriptor.guidance.use_when)
            self.assertIn("cannot substitute for the full task specification", start_descriptor.guidance.use_when)
            self.assertIn("Do not inspect or implement", start_descriptor.guidance.do_not_use_when)
            self.assertIn("do not poll status", start_descriptor.guidance.do_not_use_when)
            self.assertIn("callbacks deliver", start_descriptor.guidance.do_not_use_when)
            self.assertIn("identity-light executor work", start_descriptor.guidance.use_when)
            self.assertIn("not by prose length or step count", start_descriptor.guidance.use_when)
            self.assertIn("strongly bound to Pal's relationship with the user", start_descriptor.guidance.do_not_use_when)
            self.assertIn("In the gray area Pal handles the task directly", start_descriptor.guidance.do_not_use_when)
            decision_schema = next(
                descriptor.InputModel.model_json_schema(mode="validation")
                for descriptor in core.context.capability_registry.descriptors.values()
                if descriptor.canonical_path == "op_minion_submit_human_decision"
            )
            status_schema = next(
                descriptor.InputModel.model_json_schema(mode="validation")
                for descriptor in core.context.capability_registry.descriptors.values()
                if descriptor.canonical_path == "intro_minion_task_status"
            )
            self.assertIn("task", status_schema["properties"])
            self.assertNotIn("workflow", status_schema["properties"])
            self.assertEqual(
                decision_schema["properties"]["decision"]["enum"],
                ["accept", "edit", "reject"],
            )
            self.assertIn("task", decision_schema["properties"])
            self.assertNotIn("workflow", decision_schema["properties"])
            self.assertNotIn("clarification_response", decision_schema["properties"])
            self.assertNotIn("op_minion_dispatch_workflow", canonical)
            self.assertNotIn("op_minion_tick_parent_dag", canonical)
            self.assertNotIn("op_minion_recover_work_order", canonical)
        finally:
            with contextlib.suppress(Exception):
                core.detach_module("minion")

    def test_start_pins_approved_skill_refs_for_manager_role_sessions(self) -> None:
        reminder = (
            "<system-reminder>\n"
            "Injected skill:\nSkill id: pal.channel.provider.development\n\n"
            "Manual:\nReuse the channel provider contract.\n"
            "</system-reminder>"
        )
        provider = self._public_provider()
        started = provider.start_workflow(
            CapabilityCall(
                name="op_minion_start_workflow",
                meta={"actor_id": "nathan", "channel_id": "socket:test"},
                args={
                    "title": "Channel adapter",
                    "profile": "software_engineering.v2_coder",
                    "goal": "Implement one channel adapter.",
                    "task_spec": {
                        "objective": "Implement one channel adapter.",
                        "requirements": ["Reuse the channel provider contract."],
                    },
                    "workspace": {"kind": "new_project", "project_name": "channel-adapter"},
                    "skill_refs": [
                        "pal.channel.provider.development",
                        "pal.channel.provider.development",
                    ],
                },
            )
        )

        self.assertEqual(started.status, RuntimeStatus.OK)
        workflow_id = provider.service.repository.workflow_ids()[0]
        workflow = provider.service.repository.read_snapshot(
            AggregateType.WORKFLOW,
            workflow_id,
        )
        self.assertIsNotNone(workflow)
        request = provider.service.artifacts.read_json(workflow.payload["request_ref"])
        self.assertEqual(
            request["skill_refs"],
            ["pal.channel.provider.development"],
        )
        injected: list[str] = []

        def inject(skill_id: str) -> dict[str, str]:
            injected.append(skill_id)
            return {"skill_id": skill_id, "system_reminder": reminder}

        self.assertEqual(
            _workflow_skill_injections(request, inject),
            [
                {
                    "skill_id": "pal.channel.provider.development",
                    "system_reminder": reminder,
                }
            ],
        )
        self.assertEqual(injected, ["pal.channel.provider.development"])

    def test_approved_skill_reminder_is_one_user_side_prompt_block(self) -> None:
        reminder = (
            "<system-reminder>\n"
            "Injected skill:\nSkill id: channel-manual\n\n"
            "Manual:\nFollow the channel contract.\n"
            "</system-reminder>"
        )
        pack = MinionInvocationPack(
            invocation_id="inv-skill-reminder",
            instruction="Implement the bound module.",
            metadata={
                "initial_skill_injections": [
                    {"skill_id": "channel-manual", "system_reminder": reminder},
                    {"skill_id": "channel-manual", "system_reminder": reminder},
                ]
            },
        )

        first_prompt = render_minion_task_prompt(pack)
        runner_pack = _bind_role_attempt_sandbox(
            self.runtime_root,
            pack,
            run_id="run-skill-reminder",
            durable_prompt_reused=False,
        )
        reconstructed_prompt = render_minion_task_prompt(
            MinionInvocationPack.from_dict(runner_pack.to_dict())
        )

        self.assertIn("initial_skill_injections", runner_pack.metadata)
        self.assertEqual(first_prompt.count("<system-reminder>"), 1)
        self.assertEqual(first_prompt.count("</system-reminder>"), 1)
        self.assertEqual(reconstructed_prompt, first_prompt)
        self.assertLess(
            first_prompt.index("<system-reminder>"),
            first_prompt.index("## Execution Discipline"),
        )

    def test_manager_reuses_skill_inject_system_reminder_projection(self) -> None:
        from pal.behavior.models import BehaviorSkillModel
        from pal.foundation import PalV2Database
        from pal.skill.contracts import SkillDescriptor
        from pal.skill.repository import SkillRepository

        database = PalV2Database(self.runtime_root / "pal.sqlite3")
        database.initialize((BehaviorSkillModel,))
        try:
            SkillRepository().upsert_skill(
                SkillDescriptor(
                    skill_id="manager.manual",
                    module_id="test",
                    title="Manager manual",
                    summary="Exercise Manager-owned injection.",
                    manual_text="Read the contract before editing.",
                )
            )
        finally:
            database.close()

        manager = MinionManager(self.runtime_root)
        try:
            injected = manager._inject_skill_for_role("manager.manual")
        finally:
            if manager._skill_database is not None:
                manager._skill_database.close()

        self.assertEqual(injected["skill_id"], "manager.manual")
        self.assertTrue(injected["system_reminder"].startswith("<system-reminder>"))
        self.assertIn("Read the contract before editing.", injected["system_reminder"])

    def test_durable_role_session_reuses_injected_manual_without_reinjecting(self) -> None:
        reminder = {
            "skill_id": "manager.manual",
            "system_reminder": (
                "<system-reminder>\n"
                "Injected skill:\nSkill id: manager.manual\n"
                "</system-reminder>"
            ),
        }
        worker = SemanticOrchestrator(
            MinionV2WorkflowService(self.runtime_root),
            inject_skill=lambda _skill_id: self.fail(
                "a durable logical role session must not reinject its skill"
            ),
        )

        with patch.object(
            worker,
            "_durable_session_skill_injections",
            return_value=[reminder],
        ):
            resolved = worker._role_session_skill_injections(
                request={"skill_refs": ["manager.manual"]},
                workflow_id="workflow",
                session_id="coder-session",
            )

        self.assertEqual(resolved, [reminder])

    def test_public_provider_binds_current_workflow_without_exposing_manager_identity(self) -> None:
        wakes: list[str] = []
        provider = self._public_provider(wake_manager=lambda: wakes.append("wake"))
        meta = {"actor_id": "nathan", "channel_id": "socket:test"}
        started = provider.start_workflow(
            CapabilityCall(
                name="op_minion_start_workflow",
                meta=meta,
                args={
                    "title": "Tiny semantic router",
                    "profile": "software_engineering.v2_coder",
                    "goal": "Implement deterministic rule routing. Route matching must be deterministic.",
                    "task_spec": {
                        "objective": "Implement deterministic rule routing.",
                        "requirements": ["Route matching must be deterministic."],
                    },
                    "workspace": {"kind": "new_project", "project_name": "tiny-router"},
                },
            )
        )
        self.assertEqual(wakes, ["wake"])
        self.assertEqual(started.structured["task"], "Tiny semantic router")
        self.assertNotIn("bound_to_current_channel", started.structured)
        encoded = json.dumps(started.structured, sort_keys=True)
        for forbidden in ("workflow_id", "task_id", "artifact_ref", "sha256"):
            self.assertNotIn(forbidden, encoded)

        restarted = MinionV2PublicProvider(runtime_root=self.runtime_root, wake_manager=lambda: None)
        status = restarted.workflow_status(
            CapabilityCall(name="intro_minion_task_status", meta=meta, args={})
        )
        self.assertEqual(status.structured["task"]["name"], "Tiny semantic router")
        self.assertEqual(status.structured["workflow"]["phase"], "created")
        self.assertNotIn("active_aggregate_id", json.dumps(status.structured, sort_keys=True))

        artifact = restarted.submit_artifact(
            CapabilityCall(
                name="op_minion_submit_artifact",
                meta=meta,
                args={
                    "name": "router review notes",
                    "artifact_type": "ReviewNotesArtifact",
                    "content": {"summary": "Check deterministic ordering."},
                },
            )
        )
        self.assertEqual(artifact.structured["name"], "router review notes")
        self.assertNotIn("artifact_ref", artifact.structured)
        resolved = MinionV2WorkflowService(self.runtime_root).resolve_artifact_name(
            name="router review notes", actor="nathan"
        )
        self.assertTrue(resolved["sha256"])

    def test_task_status_projects_every_current_module_and_role_semantically(self) -> None:
        provider = self._public_provider()
        meta = {"actor_id": "nathan", "channel_id": "socket:test"}
        started = provider.start_workflow(
            CapabilityCall(
                name="op_minion_start_workflow",
                meta=meta,
                args={
                    "title": "Parallel semantic modules",
                    "profile": "software_engineering.v2_coder",
                    "goal": "Implement two contract-bound modules.",
                    "task_spec": {
                        "objective": "Implement two contract-bound modules.",
                        "requirements": ["Both modules must be independently verified."],
                    },
                    "workspace": {
                        "kind": "new_project",
                        "project_name": "parallel-semantic-modules",
                    },
                },
            )
        )
        self.assertEqual(started.status, RuntimeStatus.OK)
        service = provider.service
        workflow_id = service.repository.workflow_ids()[0]
        now = "2026-07-30T12:00:00+00:00"
        with sqlite3.connect(service.repository.db_path) as connection:
            for node_id, module, state, dependencies, worker in (
                ("node-a", "parser", "CANDIDATE_READY", [], "inv-coder"),
                ("node-b", "runtime", "VERIFYING", ["node-a"], "inv-verifier"),
            ):
                connection.execute(
                    """
                    INSERT INTO minion_v2_node_projection(
                        node_run_id, workflow_id, epoch_id, unit_id, node_kind,
                        state, dependency_node_ids_json, active_worker_id,
                        candidate_digest, blocker_json, updated_at
                    ) VALUES (?, ?, 'epoch-current', ?, 'implementation', ?, ?, ?, ?, '{}', ?)
                    """,
                    (
                        node_id,
                        workflow_id,
                        module,
                        state,
                        json.dumps(dependencies),
                        worker,
                        "candidate" if module == "parser" else "",
                        now,
                    ),
                )
            for invocation_id, node_id, role, status, turns in (
                ("inv-coder", "node-a", "coder", "completed", 4),
                ("inv-verifier", "node-b", "verifier", "running", 2),
            ):
                connection.execute(
                    """
                    INSERT INTO minion_v2_role_invocations(
                        invocation_id, workflow_id, aggregate_type, aggregate_id,
                        lease_resource_key, fencing_token, role, mode,
                        role_profile_id, family_binding_sha,
                        authoring_contract_version, prompt_pack_ref_json,
                        status, last_completed_turn,
                        created_at, updated_at
                    ) VALUES (?, ?, 'dag_node_run', ?, ?, 1, ?, 'module', '',
                              '', '', '{}', ?, ?, ?, ?)
                    """,
                    (
                        invocation_id,
                        workflow_id,
                        node_id,
                        f"lease:{invocation_id}",
                        role,
                        status,
                        turns,
                        now,
                        now,
                    ),
                )

        result = provider.workflow_status(
            CapabilityCall(
                name="intro_minion_task_status",
                meta=meta,
                args={"task": "Parallel semantic modules"},
            )
        )
        self.assertEqual(result.status, RuntimeStatus.OK)
        modules = result.structured["workflow"]["modules"]
        self.assertEqual([item["module"] for item in modules], ["parser", "runtime"])
        self.assertEqual(modules[1]["dependencies"], ["parser"])
        self.assertEqual(modules[0]["roles"]["coder"]["status"], "completed")
        self.assertEqual(modules[1]["roles"]["verifier"]["status"], "running")
        encoded = json.dumps(result.structured, sort_keys=True)
        for forbidden in ("node-a", "node-b", "inv-coder", "inv-verifier"):
            self.assertNotIn(forbidden, encoded)

    def test_public_start_rejects_second_active_workflow_for_same_task(self) -> None:
        provider = self._public_provider()
        call = CapabilityCall(
            name="op_minion_start_workflow",
            meta={"actor_id": "nathan", "channel_id": "socket:test"},
            args={
                "title": "Stable workflow name",
                "profile": "software_engineering.v2_coder",
                "goal": "Implement one deterministic component.",
                "task_spec": {
                    "objective": "Implement one deterministic component.",
                    "requirements": ["The result is deterministic."],
                },
                "workspace": {
                    "kind": "new_project",
                    "project_name": "stable-workflow-name",
                },
            },
        )

        first = provider.start_workflow(call)
        second = provider.start_workflow(call)

        self.assertEqual(first.status, RuntimeStatus.OK)
        self.assertEqual(second.status, RuntimeStatus.INVALID)
        self.assertIn("Task already has an active workflow", second.llm_text)
        self.assertEqual(len(provider.service.repository.workflow_ids()), 1)

    def test_one_click_start_validates_before_creating_its_task(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        base = {
            "title": "Invalid one-click request",
            "family_id": "software_engineering",
            "goal": "This request must not leave an orphan Task.",
            "workspace": {"kind": "new_project", "project_name": "invalid-start"},
            "actor": "nathan",
        }

        with self.assertRaisesRegex(ValueError, "requires artifact_ref"):
            service.start_workflow(
                {**base, "operation": "review_then_execute"}
            )
        with self.assertRaisesRegex(ValueError, "no longer accepts normalized"):
            service.start_workflow(
                {
                    **base,
                    "operation": "new_requirement",
                    "sections": ["not-a-section-map"],
                }
            )

        self.assertEqual(
            service.repository.search_tasks(include_archived=True, limit=10),
            (),
        )

    def test_task_ledger_uses_fts_and_only_explicit_operation_rebinds_delivery(self) -> None:
        provider = self._public_provider()
        old_meta = {"actor_id": "nathan", "channel_id": "socket:old"}
        started = provider.start_workflow(
            CapabilityCall(
                name="op_minion_start_workflow",
                meta=old_meta,
                args={
                    "title": "鸿蒙字体渲染验证",
                    "profile": "software_engineering.v2_coder",
                    "goal": "验证 OpenHarmony 原生字体生命周期和渲染流程。原生字体必须由包装对象独占。",
                    "task_spec": {
                        "objective": "验证 OpenHarmony 原生字体生命周期和渲染流程。",
                        "requirements": ["原生字体必须由包装对象独占。"],
                    },
                    "workspace": {"kind": "new_project", "project_name": "ohos-font-probe"},
                },
            )
        )
        self.assertEqual(started.status, RuntimeStatus.OK)

        new_meta = {"actor_id": "nathan", "channel_id": "telegram:main"}
        search = provider.search_tasks(
            IntrospectionCall(
                name="intro_minion_task_search",
                meta=new_meta,
                args={"query": "鸿蒙 字体"},
            )
        )
        self.assertEqual(search.status, RuntimeStatus.OK)
        self.assertEqual(search.structured["count"], 1)
        self.assertEqual(search.structured["tasks"][0]["title"], "鸿蒙字体渲染验证")
        self.assertNotIn(
            "bound_to_current_channel",
            search.structured["tasks"][0]["workflows"][0],
        )
        encoded = json.dumps(search.structured, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("task_id", encoded)
        self.assertNotIn("workflow_id", encoded)

        status = provider.workflow_status(
            CapabilityCall(
                name="intro_minion_task_status",
                meta=new_meta,
                args={"task": "鸿蒙字体渲染验证"},
            )
        )
        self.assertEqual(status.status, RuntimeStatus.OK)
        self.assertEqual(status.structured["task"]["name"], "鸿蒙字体渲染验证")
        task_id = provider.service.resolve_task_selector(
            selector="鸿蒙字体渲染验证",
            actor="nathan",
        )
        original_delivery = provider.service.repository.read_task_delivery(task_id)
        self.assertEqual(original_delivery["current"]["channel_id"], "socket_test")
        self.assertEqual(original_delivery["binding_version"], 1)

        provider._resolve_rebind_binding = lambda _call, _channel_id: {
            "channel_id": "telegram_main",
            "channel_kind": "telegram",
            "reply_target": {"chat_id": "42"},
            "control_scope_key": "telegram:telegram_main:42",
        }
        rebound_result = provider.rebind_task_delivery(
            CapabilityCall(
                name="op_minion_rebind_task_delivery",
                meta=new_meta,
                args={"task": "鸿蒙字体渲染验证", "channel_name": "telegram_main"},
            )
        )
        self.assertEqual(rebound_result.status, RuntimeStatus.OK)
        rebound_delivery = provider.service.repository.read_task_delivery(task_id)
        self.assertEqual(rebound_delivery["current"]["channel_id"], "telegram_main")
        self.assertEqual(rebound_delivery["binding_version"], 2)

        workflow_id = provider.service.repository.search_workflows(
            actor_id="nathan", task_id=task_id, include_terminal=True
        )[0]["workflow_id"]
        workflow = provider.service.repository.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        self.assertIsNotNone(workflow)
        provider.service.update_task(
            {
                "task_id": str(workflow.payload["task_id"]),
                "title": "后来修改的 Task 标题",
                "actor": "nathan",
                "source_channel": "telegram:main",
            }
        )
        stable_name_status = provider.workflow_status(
            CapabilityCall(
                name="intro_minion_task_status",
                meta={"actor_id": "nathan", "channel_id": "socket:another"},
                args={"task": "后来修改的 Task 标题"},
            )
        )
        self.assertEqual(stable_name_status.status, RuntimeStatus.OK)
        self.assertEqual(
            stable_name_status.structured["task"]["name"],
            "后来修改的 Task 标题",
        )

    def test_task_delivery_rebind_never_transitions_workflow(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        task_id = self._create_task(service, "delivery-rebind-state")
        self._start_workflow(
            service,
            {
                "task_id": task_id,
                "workflow_id": "wf_delivery_rebind_state",
                "operation": "new_requirement",
                "goal": "Keep workflow state independent from delivery.",
                "actor": "nathan",
            },
        )
        before = service.repository.read_snapshot(
            AggregateType.WORKFLOW,
            "wf_delivery_rebind_state",
        )

        rebound = service.repository.rebind_task_delivery(
            task_id=task_id,
            binding={
                "channel_id": "telegram_main",
                "channel_kind": "telegram",
                "reply_target": {"chat_id": "42"},
                "control_scope_key": "telegram:telegram_main:42",
            },
        )
        after = service.repository.read_snapshot(
            AggregateType.WORKFLOW,
            "wf_delivery_rebind_state",
        )

        self.assertTrue(rebound["changed"])
        self.assertEqual(rebound["binding_version"], 2)
        self.assertEqual((after.state, after.version, after.payload), (before.state, before.version, before.payload))

    def test_task_creation_and_delivery_binding_share_one_commit(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        with self.assertRaisesRegex(ValueError, "delivery binding requires"):
            service.create_task(
                {
                    "task_id": "task_atomic_delivery",
                    "title": "atomic delivery",
                    "objective": "Never leave a Task without its requested binding.",
                    "profile": "software_engineering.v2_coder",
                    "workspace": {
                        "kind": "new_project",
                        "project_name": "atomic-delivery",
                    },
                    "actor": "nathan",
                    "source_channel": "socket:test",
                    "delivery_binding": {"channel_id": "socket_test"},
                }
            )

        self.assertIsNone(
            service.repository.read_snapshot(
                AggregateType.TASK,
                "task_atomic_delivery",
            )
        )
        self.assertIsNone(
            service.repository.read_task_delivery("task_atomic_delivery")
        )

        creation = {
            "task_id": "task_atomic_delivery",
            "title": "atomic delivery",
            "objective": "Never leave a Task without its requested binding.",
            "profile": "software_engineering.v2_coder",
            "workspace": {
                "kind": "new_project",
                "project_name": "atomic-delivery",
            },
            "actor": "nathan",
            "source_channel": "socket:test",
            "delivery_binding": {
                "channel_id": "socket_test",
                "channel_kind": "socket",
                "reply_target": {"session_id": "s1", "request_id": "r1"},
            },
        }
        service.create_task(creation)
        service.repository.rebind_task_delivery(
            task_id="task_atomic_delivery",
            binding={
                "channel_id": "telegram_main",
                "channel_kind": "telegram",
                "reply_target": {"chat_id": "42"},
            },
        )
        service.create_task(creation)
        rebound = service.repository.read_task_delivery("task_atomic_delivery")
        self.assertEqual(rebound["origin"]["channel_id"], "socket_test")
        self.assertEqual(rebound["current"]["channel_id"], "telegram_main")

    def test_dead_task_channel_waits_durably_then_falls_back_to_live_recovery_socket(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        task_id = self._create_task(service, "durable-socket-fallback")
        service.repository.bind_task_delivery(
            task_id=task_id,
            binding={
                "channel_id": "telegram_dead",
                "channel_kind": "telegram",
                "reply_target": {"chat_id": "42"},
                "control_scope_key": "telegram:telegram_dead:42",
            },
        )
        delivery = service.repository.enqueue_task_delivery(
            task_id=task_id,
            workflow_id="",
            event_kind="terminal",
            payload={"event_kind": "terminal", "payload": {"summary": "done"}},
            dedup_key="terminal:durable-socket-fallback",
        )

        self.assertEqual(
            service.repository.delivered_task_delivery_parts(
                delivery["delivery_id"]
            ),
            (),
        )
        self.assertTrue(
            service.repository.acknowledge_task_delivery_part(
                delivery["delivery_id"],
                "attachment:0",
            )
        )
        self.assertTrue(
            service.repository.acknowledge_task_delivery_part(
                delivery["delivery_id"],
                "attachment:0",
            )
        )
        self.assertEqual(
            service.repository.delivered_task_delivery_parts(
                delivery["delivery_id"]
            ),
            ("attachment:0",),
        )

        class Runtime:
            def __init__(self, endpoints):
                self.endpoints = endpoints

            def get_endpoint(self, endpoint_id):
                return next(
                    (
                        endpoint
                        for endpoint in self.endpoints
                        if endpoint.endpoint.endpoint_id == endpoint_id
                    ),
                    None,
                )

            def list_endpoints(self):
                return tuple(self.endpoints)

        dead = SimpleNamespace(
            endpoint=SimpleNamespace(
                endpoint_id="telegram_dead",
                channel_kind="telegram",
                binding_key="telegram",
            ),
            attached=False,
            enabled=True,
        )
        recovery = SimpleNamespace(
            endpoint=SimpleNamespace(
                endpoint_id="socket_default",
                channel_kind="socket",
                binding_key=str(self.runtime_root / "pal.sock"),
            ),
            socket_path=self.runtime_root / "pal.sock",
            attached=True,
            enabled=True,
            sessions={},
        )
        context = SimpleNamespace(
            port_registry={"channel:channel": Runtime([dead, recovery])}
        )
        provider = MinionManagerProvider(self.runtime_root, context=context)
        event = {
            **dict(delivery["payload"]),
            "delivery_id": delivery["delivery_id"],
            "task_id": task_id,
        }

        self.assertIsNone(provider._prepare_delivery_event(event))
        self.assertEqual(
            service.repository.list_pending_task_deliveries()[0]["status"],
            "pending",
        )

        recovery.sessions["tty-session"] = SimpleNamespace(
            session_id="tty-session",
            closed=False,
        )
        prepared = provider._prepare_delivery_event(event)
        route = dict(dict(prepared["payload"])["route"])
        self.assertEqual(route["endpoint_id"], "socket_default")
        self.assertTrue(route["reply_target"]["request_id"].startswith("task-notification:"))

    def test_task_fts_reindexes_updated_task_projection(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        task_id = self._create_task(service, "legacy-router")
        before = service.search_tasks({"query": "legacy router", "owner": "nathan"})
        self.assertEqual(before["count"], 1)

        service.update_task(
            {
                "task_id": task_id,
                "title": "deterministic prefix engine",
                "actor": "nathan",
                "source_channel": "socket:test",
            }
        )

        after = service.search_tasks({"query": "deterministic prefix", "owner": "nathan"})
        self.assertEqual(after["count"], 1)
        self.assertEqual(after["tasks"][0]["title"], "deterministic prefix engine")

    def test_task_fts_migration_indexes_existing_projection_rows(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        self._create_task(service, "existing-ledger-entry")
        with sqlite3.connect(service.repository.db_path) as connection:
            connection.execute("DELETE FROM minion_v2_tasks_fts")
            connection.execute(
                "DELETE FROM minion_v2_schema_meta WHERE schema_key = 'task_fts_index_version'"
            )

        service.repository.ensure_schema()

        result = service.search_tasks({"query": "existing-ledger-entry", "owner": "nathan"})
        self.assertEqual(result["count"], 1)

    def test_public_human_decision_resolves_pending_card_without_token_argument(self) -> None:
        wakes: list[str] = []
        submitted: list[dict[str, object]] = []
        validated = MinionV2CapabilitiesMinionV2PublicProviderSubmitHumanDecisionInput.model_validate(
            {"task": "OHOS platform layer", "decision": "accept"}
        )
        self.assertIsNone(validated.edit_instruction)
        provider = MinionV2PublicProvider(
            runtime_root=self.runtime_root,
            wake_manager=lambda: wakes.append("wake"),
        )
        provider.service.resolve_task_workflow_selector = (
            lambda **_kwargs: ("task_internal", "wf_internal")
        )
        provider.service.submit_human_decision = lambda request: (
            submitted.append(dict(request))
            or {"status": "accepted", "workflow_id": "wf_internal", "state": "ACCEPTED"}
        )
        result = provider.submit_human_decision(
            CapabilityCall(
                name="op_minion_submit_human_decision",
                meta={"actor_id": "nathan", "channel_id": "socket:test"},
                args={"task": "OHOS platform layer", "decision": "accept"},
            )
        )
        self.assertEqual(wakes, ["wake"])
        self.assertEqual(submitted[0]["workflow_id"], "wf_internal")
        self.assertEqual(submitted[0]["actor"], "nathan")
        self.assertEqual(submitted[0]["source_channel"], "local")
        self.assertNotIn("decision_token", submitted[0])
        self.assertNotIn("workflow_id", result.structured)

    def test_public_triage_resolution_uses_semantic_subject_and_wakes_manager(self) -> None:
        wakes: list[str] = []
        submitted: list[dict[str, object]] = []
        provider = MinionV2PublicProvider(
            runtime_root=self.runtime_root,
            wake_manager=lambda: wakes.append("wake"),
        )
        provider.service.resolve_task_workflow_selector = (
            lambda **_kwargs: ("task_internal", "wf_internal")
        )
        provider.service.resolve_triage = lambda **kwargs: (
            submitted.append(dict(kwargs))
            or {
                "status": "triage_resolved",
                "workflow_id": "wf_internal",
                "subject": "ohos_font",
                "state": "REVIEW_QUEUED",
                "resolution": kwargs["resolution"],
            }
        )

        result = provider.resolve_triage(
            CapabilityCall(
                name="op_minion_resolve_triage",
                meta={"actor_id": "nathan", "channel_id": "socket:test"},
                args={
                    "task": "OHOS platform layer",
                    "subject": "ohos_font",
                    "resolution": "Prepared the missing project LSP context and verified clangd can parse the module.",
                },
            )
        )

        self.assertEqual(wakes, ["wake"])
        self.assertEqual(submitted[0]["subject"], "ohos_font")
        self.assertEqual(submitted[0]["actor"], "nathan")
        self.assertEqual(result.structured["subject"], "ohos_font")
        self.assertNotIn("workflow_id", result.structured)

    def test_public_execution_restart_uses_task_name_and_wakes_manager(self) -> None:
        wakes: list[str] = []
        submitted: list[dict[str, object]] = []
        selectors: list[dict[str, object]] = []
        provider = MinionV2PublicProvider(
            runtime_root=self.runtime_root,
            wake_manager=lambda: wakes.append("wake"),
        )
        provider.service.resolve_task_workflow_selector = lambda **kwargs: (
            selectors.append(dict(kwargs)) or ("task_internal", "wf_internal")
        )
        provider.service.restart_execution_from_architecture = lambda **kwargs: (
            submitted.append(dict(kwargs))
            or {
                "status": "restart_requested",
                "workflow_id": "wf_internal",
                "state": "CANCEL_REQUESTED",
                "architecture_review": "required",
                "module_identity_reuse": False,
            }
        )

        result = provider.restart_execution(
            CapabilityCall(
                name="op_minion_restart_execution",
                meta={"actor_id": "nathan", "channel_id": "socket:test"},
                args={
                    "task": "OHOS platform layer",
                    "reason": "Discard the host fallback execution and use the updated Coder policy.",
                },
            )
        )

        self.assertEqual(wakes, ["wake"])
        self.assertEqual(selectors[0]["selector"], "OHOS platform layer")
        self.assertEqual(submitted[0]["workflow_id"], "wf_internal")
        self.assertEqual(submitted[0]["actor"], "nathan")
        self.assertEqual(submitted[0]["source_channel"], "local")
        self.assertFalse(result.structured["module_identity_reuse"])
        self.assertNotIn("workflow_id", result.structured)

    def test_execution_restart_creates_fresh_review_workflow_after_old_workflow_settles(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        repository = service.repository
        task_id = self._create_task(service, "restart-execution")
        requirements_ref = service.prepare_requirements(
            {
                "title": "Platform backend",
                "task_spec": {
                    "objective": "Use the selected platform backend in the production path."
                },
            }
        )["requirements_ref"]
        manifest_ref = service.artifacts.put_json(
            {
                "requirements_ref": requirements_ref,
                "submission": {"modules": {}},
            },
            artifact_type="TestManifestArtifact",
        ).to_dict()
        service.start_workflow(
            {
                "delivery_binding": {
                    "channel_id": "socket_test",
                    "channel_kind": "socket",
                    "reply_target": {"session_id": "test-session", "request_id": "test-request"},
                    "control_scope_key": "socket:socket_test:test-session",
                },
                "task_id": task_id,
                "workflow_id": "wf_restart_source",
                "operation": "new_requirement",
                "goal": "Implement the platform backend.",
                "requirements_ref": requirements_ref,
                "actor": "nathan",
                "source_channel": "socket:test",
            }
        )
        workflow = repository.read_snapshot(AggregateType.WORKFLOW, "wf_restart_source")
        repository.dispatch(
            ActionEnvelope(
                action_type="START_WORKFLOW",
                workflow_id=workflow.workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow.aggregate_id,
                actor="test",
                expected_version=workflow.version,
                idempotency_key="restart-source:start",
            )
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="IMPORT_ARCHITECTURE_REVISION",
                workflow_id="wf_restart_source",
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id="arch_restart_source",
                actor="test",
                expected_version=0,
                idempotency_key="restart-source:import-architecture",
                payload={"architecture_manifest_ref": manifest_ref},
            )
        )
        workflow = repository.read_snapshot(AggregateType.WORKFLOW, "wf_restart_source")
        repository.dispatch(
            ActionEnvelope(
                action_type="LINK_ARCHITECTURE_REVISION",
                workflow_id=workflow.workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow.aggregate_id,
                actor="test",
                expected_version=workflow.version,
                idempotency_key="restart-source:link-architecture",
                payload={"architecture_revision_id": "arch_restart_source"},
            )
        )
        revision = repository.read_snapshot(
            AggregateType.ARCHITECTURE_REVISION,
            "arch_restart_source",
        )
        review_ref = service.artifacts.put_json(
            {"verdict": "PASS", "findings": []},
            artifact_type="ArchitectureReviewArtifact",
        ).to_dict()
        repository.dispatch(
            ActionEnvelope(
                action_type="START_ARCHITECTURE_REVIEW",
                workflow_id=revision.workflow_id,
                aggregate_type=revision.aggregate_type,
                aggregate_id=revision.aggregate_id,
                actor="test",
                expected_version=revision.version,
                idempotency_key="restart-source:start-review",
                payload={"fencing_token": 1},
            )
        )
        revision = repository.read_snapshot(revision.aggregate_type, revision.aggregate_id)
        decision_token = repository.issue_human_decision_token(
            workflow_id=revision.workflow_id,
            architecture_revision_id=revision.aggregate_id,
            manifest_sha=str(manifest_ref["sha256"]),
            actor_id="nathan",
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="ARCHITECTURE_REVIEW_PASSED",
                workflow_id=revision.workflow_id,
                aggregate_type=revision.aggregate_type,
                aggregate_id=revision.aggregate_id,
                actor="test",
                expected_version=revision.version,
                idempotency_key="restart-source:review-pass",
                payload={
                    "review_artifact_ref": review_ref,
                    "architecture_manifest_ref": manifest_ref,
                },
            )
        )
        revision = repository.read_snapshot(revision.aggregate_type, revision.aggregate_id)
        repository.dispatch(
            ActionEnvelope(
                action_type="HUMAN_ACCEPT",
                workflow_id=revision.workflow_id,
                aggregate_type=revision.aggregate_type,
                aggregate_id=revision.aggregate_id,
                actor="nathan",
                source_channel="socket:test",
                expected_version=revision.version,
                idempotency_key="restart-source:human-accept",
                payload={
                    "decision_token": decision_token,
                    "architecture_manifest_ref": manifest_ref,
                },
            )
        )
        with sqlite3.connect(str(repository.db_path)) as connection:
            connection.execute(
                "UPDATE minion_v2_outbox SET status = 'completed' WHERE workflow_id = ?",
                ("wf_restart_source",),
            )

        requested = service.restart_execution_from_architecture(
            workflow_id="wf_restart_source",
            actor="nathan",
            source_channel="socket:new",
            reason="Use the updated Coder and Verifier policy.",
        )
        self.assertEqual(requested["state"], "CANCEL_REQUESTED")
        processor = MinionV2OutboxProcessor(
            service,
            semantic_effects=_NoopSemanticEffects(),
        )
        asyncio.run(processor.process_once(limit=10))
        source = repository.read_snapshot(AggregateType.WORKFLOW, "wf_restart_source")
        self.assertEqual(source.state, "RESTARTING")

        with (
            patch.object(service, "_validate_external_architecture_ref") as validate,
            patch.object(
                service.catalog,
                "publish_family_binding",
                wraps=service.catalog.publish_family_binding,
            ) as publish_family_binding,
        ):
            asyncio.run(processor.process_once(limit=10))
        validate.assert_called_once()
        publish_family_binding.assert_not_called()

        source = repository.read_snapshot(AggregateType.WORKFLOW, "wf_restart_source")
        replacement_id = str(source.payload["replacement_workflow_id"])
        replacement = repository.read_snapshot(AggregateType.WORKFLOW, replacement_id)
        replacement_request = service.artifacts.read_json(replacement.payload["request_ref"])
        self.assertEqual(source.state, "CANCELLED")
        self.assertEqual(replacement.state, "CREATED")
        self.assertEqual(replacement.payload["task_id"], task_id)
        task = repository.read_snapshot(AggregateType.TASK, task_id)
        self.assertEqual(replacement.payload["family_binding_ref"], task.payload["family_binding_ref"])
        self.assertEqual(replacement_request["operation"], "review_then_execute")
        self.assertEqual(replacement_request["input_artifact_ref"], manifest_ref)
        self.assertNotIn("reuse_candidates", replacement_request)
        delivery = repository.read_task_delivery(task_id)
        self.assertEqual(delivery["current"]["channel_id"], "socket_test")
        self.assertEqual(delivery["binding_version"], 1)
        asyncio.run(processor.process_once(limit=10))
        asyncio.run(processor.process_once(limit=10))
        replacement_revision = next(
            item
            for item in repository.list_workflow_snapshots(replacement_id)
            if item.aggregate_type == AggregateType.ARCHITECTURE_REVISION
        )
        self.assertEqual(
            replacement_revision.payload["requirements_ref"],
            requirements_ref,
        )

    def test_execution_restart_cancelled_while_restarting_creates_no_replacement(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        repository = service.repository
        requirements_ref = service.task_ledger.publish(
            title="Restart cancellation",
            task_spec={"objective": "Restart the accepted execution."},
            actor="test",
            source_channel="test",
        ).to_dict()
        manifest_ref = service.artifacts.put_json(
            {"requirements_ref": requirements_ref},
            artifact_type="TestManifestArtifact",
        ).to_dict()
        created = repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_WORKFLOW",
                workflow_id="wf_restart_cancel",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="wf_restart_cancel",
                actor="test",
                expected_version=0,
                idempotency_key="restart-cancel:create",
            )
        ).snapshot
        active = repository.dispatch(
            ActionEnvelope(
                action_type="START_WORKFLOW",
                workflow_id=created.workflow_id,
                aggregate_type=created.aggregate_type,
                aggregate_id=created.aggregate_id,
                actor="test",
                expected_version=created.version,
                idempotency_key="restart-cancel:start",
            )
        ).snapshot
        with sqlite3.connect(str(repository.db_path)) as connection:
            connection.execute(
                "UPDATE minion_v2_outbox SET status = 'completed' WHERE workflow_id = ?",
                (active.workflow_id,),
            )
        repository.dispatch(
            ActionEnvelope(
                action_type="REQUEST_EXECUTION_RESTART",
                workflow_id=active.workflow_id,
                aggregate_type=active.aggregate_type,
                aggregate_id=active.aggregate_id,
                actor="test",
                expected_version=active.version,
                idempotency_key="restart-cancel:request",
                payload={
                    "restart_execution_request": {
                        "task_id": "task_restart_cancel",
                        "architecture_manifest_ref": manifest_ref,
                        "requirements_ref": requirements_ref,
                    }
                },
            )
        )
        processor = MinionV2OutboxProcessor(
            service,
            semantic_effects=_NoopSemanticEffects(),
        )
        asyncio.run(processor.process_once(limit=10))
        restarting = repository.read_snapshot(
            AggregateType.WORKFLOW,
            "wf_restart_cancel",
        )
        self.assertEqual(restarting.state, "RESTARTING")
        repository.dispatch(
            ActionEnvelope(
                action_type="REQUEST_CANCEL",
                workflow_id=restarting.workflow_id,
                aggregate_type=restarting.aggregate_type,
                aggregate_id=restarting.aggregate_id,
                actor="test",
                expected_version=restarting.version,
                idempotency_key="restart-cancel:abort",
            )
        )

        asyncio.run(processor.process_once(limit=10))

        cancelled = repository.read_snapshot(
            AggregateType.WORKFLOW,
            "wf_restart_cancel",
        )
        with sqlite3.connect(str(repository.db_path)) as connection:
            workflow_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM minion_v2_aggregate_snapshots WHERE aggregate_type = ?",
                    (AggregateType.WORKFLOW.value,),
                ).fetchone()[0]
            )
        self.assertEqual(cancelled.state, "CANCELLED")
        self.assertNotIn("replacement_workflow_id", cancelled.payload)
        self.assertEqual(workflow_count, 1)

    def test_public_status_human_review_view_hides_card_bindings(self) -> None:
        requested: list[tuple[str, str]] = []
        provider = MinionV2PublicProvider(runtime_root=self.runtime_root, wake_manager=lambda: None)
        provider.service.resolve_task_workflow_selector = (
            lambda **_kwargs: ("task_internal", "wf_internal")
        )

        def status(workflow_id: str, *, view: str = "status") -> dict[str, object]:
            requested.append((workflow_id, view))
            return {
                "status": "ok",
                "workflow_id": workflow_id,
                "current_phase": "human_review",
                "waiting_for_user": True,
                "human_review_available": True,
                "human_review": {
                    "markdown": "# Review me",
                    "actions": ["accept", "edit", "reject"],
                    "decision_token": "secret",
                    "actor_id": "nathan",
                    "active_channel_id": "socket:old",
                    "manifest_sha": "hidden",
                    "route": {"session_id": "hidden"},
                },
            }

        provider.service.task_status = lambda task_id, *, workflow_id="", view="status": {
            "status": "ok",
            "task": {"name": "Router", "state": "ACTIVE"},
            "workflow": status(workflow_id, view=view),
        }
        result = provider.workflow_status(
            CapabilityCall(
                name="intro_minion_task_status",
                meta={"actor_id": "nathan", "channel_id": "socket:new"},
                args={"task": "Router", "view": "human_review"},
            )
        )

        self.assertEqual(requested, [("wf_internal", "human_review")])
        self.assertEqual(
            result.structured["workflow"]["human_review"]["markdown"],
            "# Review me",
        )
        encoded = json.dumps(result.structured, sort_keys=True)
        for forbidden in ("workflow_id", "decision_token", "actor_id", "active_channel_id", "manifest_sha", "route"):
            self.assertNotIn(forbidden, encoded)

    def test_recovery_refreshes_a_card_from_an_old_renderer_generation(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        stale_card_ref = service.artifacts.put_json(
            {
                "render_version": HUMAN_REVIEW_RENDER_VERSION - 1,
                "manifest_sha": "manifest-current",
                "markdown": "# Stale renderer output",
            },
            artifact_type="HumanReviewCardArtifact",
        ).to_dict()
        workflow = AggregateSnapshot(
            aggregate_type=AggregateType.WORKFLOW,
            aggregate_id="wf_refresh_card",
            workflow_id="wf_refresh_card",
            state="ACTIVE",
            version=3,
            payload={},
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        revision = AggregateSnapshot(
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch_refresh_card",
            workflow_id=workflow.workflow_id,
            state="HUMAN_REVIEW",
            version=4,
            payload={
                "architecture_manifest_ref": {"sha256": "manifest-current"},
                "human_review_card_ref": stale_card_ref,
            },
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        processor = MinionV2OutboxProcessor(
            service,
            semantic_effects=_NoopSemanticEffects(),
        )

        with patch.object(service.repository, "dispatch") as dispatch:
            processor._reconcile_linked_revision(
                workflow,
                revision,
                [workflow, revision],
                {"effect_key": "refresh-card"},
            )

        action = dispatch.call_args.args[0]
        self.assertEqual(action.action_type, "REFRESH_HUMAN_REVIEW_CARD")
        self.assertEqual(action.expected_version, revision.version)
        self.assertEqual(
            action.payload["architecture_manifest_ref"],
            {"sha256": "manifest-current"},
        )

    def test_human_review_fallback_uses_the_complete_shared_renderer(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        definition = ArchitectureTemplateCompiler().compile(
            "software_engineering.v1"
        )
        requirements_ref = service.artifacts.put_json(
            {
                "schema_version": "1",
                "title": "Framepipe",
                "original": {"objective": "Decode frames."},
                "revisions": [
                    {
                        "sequence": 1,
                        "authority": {
                            "title": "Clarify malformed input",
                            "question": "Should malformed input fail permanently?",
                            "answer": "Yes, until reset.",
                            "observed_at": "2026-08-09T00:00:00Z",
                            "origin": "architect_user_clarification",
                        },
                    }
                ],
            },
            artifact_type="TaskLedgerArtifact",
        )
        manifest_ref = service.artifacts.put_json(
            {
                "requirements_ref": requirements_ref.to_dict(),
                "contract": deepcopy(definition.example),
            },
            artifact_type="ContractArtifact",
            child_refs=((requirements_ref.sha256, "requirements"),),
        )
        revision = AggregateSnapshot(
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch_shared_renderer",
            workflow_id="wf_shared_renderer",
            state="HUMAN_REVIEW",
            version=4,
            payload={"architecture_manifest_ref": manifest_ref.to_dict()},
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )

        with patch.object(service, "_workflow_uses_git_strategy", return_value=True):
            view = service._human_review_view(revision)

        self.assertIn("## Family Context", view["markdown"])
        self.assertIn("## Task Revision History", view["markdown"])
        self.assertIn("Exact answer: Yes, until reset.", view["markdown"])

    def test_human_review_is_recoverable_without_a_delivery_notification(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        repository = service.repository
        task_id = self._create_task(service, "recover-review-without-delivery")
        workflow_id = "wf_recover_review_without_delivery"
        revision_id = "arch_recover_review_without_delivery"
        repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_WORKFLOW",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="nathan",
                expected_version=0,
                idempotency_key="recover-review:create-workflow",
                payload={"task_id": task_id, "owner": "nathan"},
            )
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="START_WORKFLOW",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="nathan",
                expected_version=1,
                idempotency_key="recover-review:start-workflow",
            )
        )
        manifest_ref = service.artifacts.put_json(
            {"contract": "recoverable"},
            artifact_type="TestManifestArtifact",
        ).to_dict()
        review_ref = service.artifacts.put_json(
            {"verdict": "PASS", "findings": []},
            artifact_type="ArchitectureReviewArtifact",
        ).to_dict()
        repository.dispatch(
            ActionEnvelope(
                action_type="IMPORT_ARCHITECTURE_REVISION",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision_id,
                actor="nathan",
                expected_version=0,
                idempotency_key="recover-review:import-revision",
                payload={"architecture_manifest_ref": manifest_ref},
            )
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="LINK_ARCHITECTURE_REVISION",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="nathan",
                expected_version=2,
                idempotency_key="recover-review:link-revision",
                payload={"architecture_revision_id": revision_id},
            )
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="START_ARCHITECTURE_REVIEW",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision_id,
                actor="nathan",
                expected_version=1,
                idempotency_key="recover-review:start-review",
                payload={"fencing_token": 1},
            )
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="ARCHITECTURE_REVIEW_PASSED",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision_id,
                actor="nathan",
                expected_version=2,
                idempotency_key="recover-review:review-passed",
                payload={
                    "review_artifact_ref": review_ref,
                    "architecture_manifest_ref": manifest_ref,
                },
            )
        )
        token = repository.issue_human_decision_token(
            workflow_id=workflow_id,
            architecture_revision_id=revision_id,
            manifest_sha=str(manifest_ref["sha256"]),
            actor_id="nathan",
        )
        card_ref = service.artifacts.put_json(
            {
                "render_version": HUMAN_REVIEW_RENDER_VERSION,
                "workflow_id": workflow_id,
                "architecture_revision_id": revision_id,
                "manifest_sha": str(manifest_ref["sha256"]),
                "actor_id": "nathan",
                "decision_token": token,
                "markdown": "# Recoverable review",
                "actions": ["accept", "edit", "reject"],
            },
            artifact_type="HumanReviewCardArtifact",
            child_refs=((str(manifest_ref["sha256"]), "architecture_manifest"),),
        ).to_dict()
        repository.dispatch(
            ActionEnvelope(
                action_type="HUMAN_REVIEW_PUBLISHED",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision_id,
                actor="minion-v2-manager",
                expected_version=3,
                idempotency_key="recover-review:card-persisted",
                payload={"human_review_card_ref": card_ref},
            )
        )
        repository.store_plan_cycle(
            workflow_id=workflow_id,
            cycle=PlanCycle(
                cycle_id=f"{workflow_id}:plan",
                state=PlanCycleState.HUMAN_REVIEW,
                product_ref=str(manifest_ref["sha256"]),
                accepted_product_ref=str(manifest_ref["sha256"]),
            ),
        )

        self.assertIsNone(
            repository.latest_task_delivery(
                task_id=task_id,
                workflow_id=workflow_id,
                event_kind="architecture_review_pending",
            )
        )
        status = service.task_status(
            task_id,
            workflow_id=workflow_id,
            view="human_review",
        )
        self.assertEqual(
            status["workflow"]["human_review"]["markdown"],
            "# Recoverable review",
        )

        result = service.submit_human_decision(
            {
                "workflow_id": workflow_id,
                "decision": "accept",
                "actor": "nathan",
                "source_channel": "socket:recovered",
            }
        )

        self.assertEqual(result["state"], "ACCEPTED")
        self.assertEqual(
            repository.read_plan_cycle(workflow_id=workflow_id).state,
            PlanCycleState.ACCEPTED,
        )

    def test_public_status_uses_manager_live_projection(self) -> None:
        requested: list[tuple[str, dict[str, object]]] = []

        def manager_request(method: str, params: dict[str, object] | None) -> dict[str, object]:
            requested.append((method, dict(params or {})))
            return {
                "status": "ok",
                "task": {"name": "Framepipe", "state": "ACTIVE"},
                "workflow": {
                    "workflow_id": "wf-live-question",
                    "waiting_for_user": True,
                    "liveness": "human_wait",
                },
            }

        provider = MinionV2PublicProvider(
            runtime_root=self.runtime_root,
            wake_manager=lambda: None,
            manager_request=manager_request,
        )
        provider.service.resolve_task_workflow_selector = (
            lambda **_kwargs: ("task-live-question", "wf-live-question")
        )

        result = provider.workflow_status(
            CapabilityCall(
                name="intro_minion_task_status",
                meta={"actor_id": "nathan", "channel_id": "socket:new"},
                args={"task": "Framepipe", "view": "status"},
            )
        )

        self.assertEqual(
            requested,
            [
                (
                    "v2_task_status",
                    {
                        "task_id": "task-live-question",
                        "workflow_id": "wf-live-question",
                        "view": "status",
                    },
                )
            ],
        )
        self.assertTrue(result.structured["workflow"]["waiting_for_user"])
        self.assertEqual(result.structured["workflow"]["liveness"], "human_wait")

    def test_new_requirement_routes_to_architecture_revision_without_cursor_state(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        task_id = self._create_task(service, "route")
        started = self._start_workflow(service,
            {
                "task_id": task_id,
                "workflow_id": "wf_route",
                "operation": "new_requirement",
                "goal": "Implement a bounded feature. Preserve the public contract.",
                "actor": "nathan",
                "source_channel": "socket:test",
            }
        )
        processor = MinionV2OutboxProcessor(service, semantic_effects=_NoopSemanticEffects())
        asyncio.run(processor.process_once(limit=10))
        asyncio.run(processor.process_once(limit=10))
        status = service.workflow_status(started["workflow_id"])
        self.assertEqual(status["current_phase"], "architecture")
        self.assertEqual(status["active_node_state"], "ARCHITECT_QUEUED")
        snapshots = service.repository.list_workflow_snapshots("wf_route")
        self.assertFalse(any("milestone" in key or "cursor" in key for item in snapshots for key in item.payload))

    def test_standalone_review_artifact_routes_without_architecture_or_coder(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        task_id = self._create_task(service, "review_only")
        artifact = service.submit_artifact(
            {
                "artifact_type": "CodeSnapshotArtifact",
                "content": {"repository": "demo", "sha": "abc"},
            }
        )["artifact_ref"]
        self._start_workflow(service,
            {
                "task_id": task_id,
                "workflow_id": "wf_review_only",
                "operation": "standalone_review",
                "artifact_ref": artifact,
                "actor": "nathan",
                "source_channel": "socket:test",
            }
        )
        processor = MinionV2OutboxProcessor(service, semantic_effects=_NoopSemanticEffects())
        for _ in range(3):
            asyncio.run(processor.process_once(limit=10))

        status = service.workflow_status("wf_review_only")
        snapshots = service.repository.list_workflow_snapshots("wf_review_only")
        self.assertEqual(status["current_phase"], "standalone_review")
        self.assertTrue(any(item.aggregate_type == AggregateType.STANDALONE_REVIEW for item in snapshots))
        self.assertFalse(any(item.aggregate_type == AggregateType.ARCHITECTURE_REVISION for item in snapshots))
        self.assertFalse(any(item.aggregate_type == AggregateType.EXECUTION_EPOCH for item in snapshots))

    def test_manual_triage_resolution_resumes_selected_child_and_records_summary(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        task_id = self._create_task(service, "triage-resume")
        self._start_workflow(service,
            {
                "task_id": task_id,
                "workflow_id": "wf_triage_resume",
                "operation": "new_requirement",
                "goal": "Exercise child triage recovery",
            }
        )
        processor = MinionV2OutboxProcessor(service, semantic_effects=_NoopSemanticEffects())
        asyncio.run(processor.process_once(limit=10))
        asyncio.run(processor.process_once(limit=10))
        revision = next(
            item
            for item in service.repository.list_workflow_snapshots("wf_triage_resume")
            if item.aggregate_type == AggregateType.ARCHITECTURE_REVISION
        )
        service.repository.dispatch(
            ActionEnvelope(
                action_type="ENTER_TRIAGE",
                workflow_id=revision.workflow_id,
                aggregate_type=revision.aggregate_type,
                aggregate_id=revision.aggregate_id,
                actor="test",
                expected_version=revision.version,
                idempotency_key="triage-resume:enter",
                payload={"blocker": {"kind": "test"}},
            )
        )

        result = service.resolve_triage(
            workflow_id="wf_triage_resume",
            actor="nathan",
            source_channel="socket:test",
            subject="phase:architecture",
            resolution="Removed the stale worker lease and verified the architecture workspace is stable.",
        )

        resumed = service.repository.read_snapshot(AggregateType.ARCHITECTURE_REVISION, revision.aggregate_id)
        self.assertEqual(result["status"], "triage_resolved")
        self.assertEqual(resumed.state, "ARCHITECT_QUEUED")
        self.assertEqual(
            resumed.payload["triage_resolution"],
            "Removed the stale worker lease and verified the architecture workspace is stable.",
        )
        self.assertEqual(resumed.payload["triage_resolution_kind"], "manual")

    def test_successor_epoch_retires_historic_node_triage_ownership(self) -> None:
        def snapshot(
            aggregate_type: AggregateType,
            aggregate_id: str,
            *,
            payload: dict[str, object] | None = None,
        ) -> AggregateSnapshot:
            return AggregateSnapshot(
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                workflow_id="wf_lineage",
                state="TRIAGE_REQUIRED",
                version=1,
                payload=payload or {},
                created_at="",
                updated_at="",
            )

        workflow = snapshot(
            AggregateType.WORKFLOW,
            "wf_lineage",
            payload={
                "execution_epoch_id": "epoch_new",
                "architecture_revision_id": "arch_new",
            },
        )
        current_epoch = snapshot(
            AggregateType.EXECUTION_EPOCH,
            "epoch_new",
            payload={"active_replan_revision_id": "arch_replan"},
        )
        current_node = snapshot(
            AggregateType.DAG_NODE_RUN,
            "node_new",
            payload={"epoch_id": "epoch_new"},
        )
        old_node = snapshot(
            AggregateType.DAG_NODE_RUN,
            "node_old",
            payload={"epoch_id": "epoch_old"},
        )

        active = _active_workflow_lineage_ids(
            workflow,
            (workflow, current_epoch, current_node, old_node),
            active_aggregate_id="arch_replan",
        )

        self.assertEqual(
            active,
            {"wf_lineage", "epoch_new", "arch_new", "arch_replan", "node_new"},
        )

    def test_triage_subjects_disambiguate_module_names_from_phase_names(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        repository = service.repository
        repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_WORKFLOW",
                workflow_id="wf_subjects",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="wf_subjects",
                actor="test",
                expected_version=0,
                idempotency_key="subjects:create-workflow",
            )
        )
        revision = repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_ARCHITECTURE_REVISION",
                workflow_id="wf_subjects",
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id="arch_subjects",
                actor="test",
                expected_version=0,
                idempotency_key="subjects:create-architecture",
            )
        ).snapshot
        contract_ref = service.artifacts.put_json(
            {"module_name": "architecture"},
            artifact_type="ModuleContractArtifact",
        )
        node = repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_NODE_RUN",
                workflow_id="wf_subjects",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node_subjects",
                actor="test",
                expected_version=0,
                idempotency_key="subjects:create-node",
                payload={
                    "unit_contract_ref": contract_ref.to_dict(),
                    "epoch_id": "epoch_subjects",
                    "unit_id": "architecture",
                    "module_name": "architecture",
                    "node_kind": "unit",
                    "dependency_node_ids": [],
                },
            )
        ).snapshot
        for snapshot, key in ((revision, "architecture"), (node, "module")):
            repository.dispatch(
                ActionEnvelope(
                    action_type="ENTER_TRIAGE",
                    workflow_id="wf_subjects",
                    aggregate_type=snapshot.aggregate_type,
                    aggregate_id=snapshot.aggregate_id,
                    actor="test",
                    expected_version=snapshot.version,
                    idempotency_key=f"subjects:triage:{key}",
                    payload={"blocker": {"kind": "test"}},
                )
            )

        status = service.workflow_status("wf_subjects")
        subjects = {item["subject"] for item in status["triage"]}
        self.assertEqual(subjects, {"module:architecture", "phase:architecture"})

        service.resolve_triage(
            workflow_id="wf_subjects",
            actor="nathan",
            source_channel="socket:test",
            subject="module:architecture",
            resolution="Recovered only the module worker.",
        )

        self.assertNotEqual(
            repository.read_snapshot(AggregateType.DAG_NODE_RUN, "node_subjects").state,
            "TRIAGE_REQUIRED",
        )
        self.assertEqual(
            repository.read_snapshot(AggregateType.ARCHITECTURE_REVISION, "arch_subjects").state,
            "TRIAGE_REQUIRED",
        )

    def test_triage_resolution_restores_verified_imported_requirements_binding(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        repository = service.repository
        requirements_ref = service.task_ledger.publish(
            title="Imported architecture",
            task_spec={"objective": "Execute the imported architecture."},
            actor="test",
            source_channel="test",
        ).to_dict()
        manifest_ref = service.artifacts.put_json(
            {"requirements_ref": requirements_ref},
            artifact_type="TestManifestArtifact",
        ).to_dict()
        request_ref = service.artifacts.put_json(
            {"requirements_ref": requirements_ref},
            artifact_type="WorkflowRequestArtifact",
        ).to_dict()
        repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_WORKFLOW",
                workflow_id="wf_imported_triage",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="wf_imported_triage",
                actor="test",
                expected_version=0,
                idempotency_key="imported-triage:create-workflow",
                payload={"request_ref": request_ref, "owner": "nathan"},
            )
        )
        imported = repository.dispatch(
            ActionEnvelope(
                action_type="IMPORT_ARCHITECTURE_REVISION",
                workflow_id="wf_imported_triage",
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id="arch_imported_triage",
                actor="test",
                expected_version=0,
                idempotency_key="imported-triage:import",
                payload={"architecture_manifest_ref": manifest_ref},
            )
        ).snapshot
        repository.dispatch(
            ActionEnvelope(
                action_type="ENTER_TRIAGE",
                workflow_id=imported.workflow_id,
                aggregate_type=imported.aggregate_type,
                aggregate_id=imported.aggregate_id,
                actor="test",
                expected_version=imported.version,
                idempotency_key="imported-triage:enter",
                payload={"blocker": {"kind": "missing_requirements_binding"}},
            )
        )

        result = service.resolve_triage(
            workflow_id="wf_imported_triage",
            actor="nathan",
            source_channel="socket:test",
            subject="phase:architecture",
            resolution="Verified the imported manifest and Workflow Request bind identical Requirements.",
        )

        restored = repository.read_snapshot(
            AggregateType.ARCHITECTURE_REVISION,
            "arch_imported_triage",
        )
        self.assertEqual(result["state"], "REVIEW_QUEUED")
        self.assertEqual(restored.payload["requirements_ref"], requirements_ref)
        self.assertEqual(
            restored.payload["triage_repair"],
            "restored_imported_architecture_requirements_binding",
        )

    def test_resume_workflow_normalizes_orphaned_node_before_retry(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        service.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_WORKFLOW",
                workflow_id="wf_orphaned_node",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="wf_orphaned_node",
                actor="test",
                expected_version=0,
                idempotency_key="orphaned-node:create-workflow",
            )
        )
        service.repository.dispatch(
            ActionEnvelope(
                action_type="START_WORKFLOW",
                workflow_id="wf_orphaned_node",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="wf_orphaned_node",
                actor="test",
                expected_version=1,
                idempotency_key="orphaned-node:start-workflow",
            )
        )
        contract_ref = service.artifacts.put_json(
            {"module_name": "drawing"},
            artifact_type="ModuleContractArtifact",
        )
        graph = GraphIR(
            graph_id="wf_orphaned_node",
            generation=1,
            nodes={
                "drawing": NodeSpec(
                    name="drawing",
                    responsibility="render one drawing",
                    satellite_data={"test": True},
                    producer_binding=RoleBinding("profile", "coder"),
                    checker_binding=RoleBinding("profile", "verifier"),
                    execution_adapter="software_git.v2",
                    workspace_policy={},
                    output_contract=("drawing",),
                    is_sink=True,
                )
            },
            edges=(),
            sink="drawing",
            source_ref="architect.yaml",
            source_map_ref="source-map",
        )
        coordinator = WorkflowCoordinator(service.repository)
        coordinator.install_graph(
            workflow_id="wf_orphaned_node",
            graph=graph,
        )
        coordinator.start_assignment(
            workflow_id="wf_orphaned_node",
            node_name="drawing",
            slot=CycleSlot.PRODUCER,
            kind=AssignmentKind.INITIAL,
            input_fingerprint="orphaned-producer",
        )
        node_id = "epoch_orphaned:node:drawing"
        service.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_NODE_RUN",
                workflow_id="wf_orphaned_node",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=node_id,
                actor="test",
                expected_version=0,
                idempotency_key="orphaned-node:create-node",
                payload={
                    "unit_contract_ref": contract_ref.to_dict(),
                    "epoch_id": "epoch_orphaned",
                    "unit_id": "drawing",
                    "node_kind": "unit",
                    "dependency_node_ids": [],
                },
            )
        )
        service.repository.dispatch(
            ActionEnvelope(
                action_type="DEPENDENCIES_ACCEPTED",
                workflow_id="wf_orphaned_node",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=node_id,
                actor="test",
                expected_version=1,
                idempotency_key="orphaned-node:dependencies",
                payload={"accepted_dependency_node_ids": []},
            )
        )
        service.repository.dispatch(
            ActionEnvelope(
                action_type="START_PRODUCING",
                workflow_id="wf_orphaned_node",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=node_id,
                actor="test",
                expected_version=2,
                idempotency_key="orphaned-node:start-producing",
                payload={
                    "fencing_token": 1,
                    "active_worker_id": "dead-worker",
                    "lease_resource_key": "node:dead-worker",
                },
            )
        )
        with sqlite3.connect(str(service.repository.db_path)) as connection:
            connection.execute(
                "UPDATE minion_v2_outbox SET status = 'completed' WHERE workflow_id = ?",
                ("wf_orphaned_node",),
            )

        orphaned = service.workflow_status("wf_orphaned_node")
        self.assertEqual(orphaned["liveness"], "orphaned")
        self.assertEqual(
            orphaned["next_legal_action"],
            ["resume_workflow", "control_workflow:cancel"],
        )

        result = service.resume_workflow(
            workflow_id="wf_orphaned_node",
            actor="nathan",
            source_channel="socket:test",
        )

        node = service.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
        self.assertEqual(result["status"], "triage_requires_resolution")
        self.assertEqual(result["triage"][0]["subject"], "unit:drawing")
        self.assertEqual(node.state, "TRIAGE_REQUIRED")

        status = service.workflow_status("wf_orphaned_node")
        self.assertEqual(
            status["next_legal_action"],
            ["resolve_triage", "control_workflow:cancel"],
        )
        self.assertEqual(
            status["triage"],
            [
                {
                    "subject": "unit:drawing",
                    "kind": "dag node run",
                    "blocker": {
                        "kind": "orphaned_worker",
                        "reason": (
                            "worker-owned state has no live lease, pending outbox "
                            "effect, or durable role assignment"
                        ),
                    },
                    "resume_state": "PRODUCING",
                    "recovery": {
                        "action": "resolve_triage",
                        "arguments": {"subject": "unit:drawing"},
                        "requires": ["resolution"],
                    },
                }
            ],
        )

        resolved = service.resolve_triage(
            workflow_id="wf_orphaned_node",
            actor="nathan",
            source_channel="socket:test",
            subject="unit:drawing",
            resolution="Confirmed the previous worker is gone and the worktree contains no active writers.",
        )

        node = service.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
        self.assertEqual(resolved["status"], "triage_resolved")
        self.assertEqual(node.state, "QUEUED")
        self.assertNotIn("active_worker_id", node.payload)
        self.assertNotIn("lease_resource_key", node.payload)

    def test_task_workspace_and_file_uri_references_are_normalized(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        repo = self.runtime_root / "repo"
        repo.mkdir()
        patch = repo / "source.patch"
        patch.write_text("diff --git a/a b/a\n", encoding="utf-8")
        created = service.create_task(
            {
                "task_id": "normalized-task",
                "title": "Normalize task inputs",
                "objective": "Keep worker handoff canonical",
                "profile": "software_engineering.v2_coder",
                "workspace": {"repo_root": str(repo)},
                "references": [{"uri": f"file://{patch}", "note": "truth"}],
            }
        )
        started = self._start_workflow(service,
            {
                "task_id": created["task_id"],
                "workflow_id": "wf_normalized_task",
                "operation": "new_requirement",
                "goal": "Use normalized paths",
            }
        )
        workflow = service.repository.read_snapshot(AggregateType.WORKFLOW, started["workflow_id"])
        request = service.artifacts.read_json(dict(workflow.payload["request_ref"]))

        self.assertEqual(request["workspace"]["repo_path"], str(repo))
        self.assertEqual(request["workspace"]["kind"], "existing_repo")
        self.assertEqual(request["references"][0]["path"], str(patch))
        self.assertEqual(request["references"][0]["description"], "truth")

    def test_prepare_requirements_rejects_normalized_requirement_fields(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        with self.assertRaisesRegex(ValueError, "no longer accepts normalized"):
            service.prepare_requirements(
                {
                    "title": "Merged requirements",
                    "request_text": "Implement the task.",
                    "sections": {"Protocol": ["Encode one frame."]},
                }
            )

    def test_start_workflow_preserves_structured_task_spec_as_single_ledger(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        repo = self.runtime_root / "framepipe"
        repo.mkdir()
        task_spec = {
            "objective": "Implement Framepipe.",
            "language": "C++17",
            "protocol": {
                "framing": "four-byte length prefix",
                "maximum_payload_bytes": 1024,
                "oversize_behavior": "reject deterministically",
            },
            "example": "framepipe encode 4869",
            "acceptance": "Run a real subprocess through stdin and stdout.",
        }

        started = service.start_workflow(
            {
                "delivery_binding": {
                    "channel_id": "socket_test",
                    "channel_kind": "socket",
                    "reply_target": {"session_id": "test-session", "request_id": "test-request"},
                    "control_scope_key": "socket:socket_test:test-session",
                },
                "workflow_id": "wf_requirement_file",
                "title": "Framepipe",
                "profile": "software_engineering.v2_coder",
                "operation": "new_requirement",
                "goal": "Implement Framepipe.\nUse C++17 only.\nThe verification node must launch the built executable.",
                "workspace": {"repo_path": str(repo), "primary_language": "cpp"},
                "task_spec": task_spec,
            }
        )
        workflow = service.repository.read_snapshot(AggregateType.WORKFLOW, started["workflow_id"])
        request = service.artifacts.read_json(dict(workflow.payload["request_ref"]))
        artifact = service.artifacts.read_json(dict(request["requirements_ref"]))
        self.assertEqual(artifact["original"], task_spec)
        self.assertEqual(artifact["revisions"], [])
        materialized = service.task_ledger.materialize(request["requirements_ref"])
        self.assertEqual(materialized.files, ("task.yaml",))

    def test_legacy_requirement_source_files_are_rejected(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        repo = self.runtime_root / "repo"
        repo.mkdir()
        base = {
            "title": "Unsafe requirement source",
            "profile": "software_engineering.v2_coder",
            "operation": "new_requirement",
            "goal": "Reject unsafe paths.",
            "task_spec": {"objective": "Reject unsafe paths."},
            "workspace": {"repo_path": str(repo)},
        }

        with self.assertRaisesRegex(ValueError, "source_files was removed"):
            service.start_workflow(
                {**base, "workflow_id": "wf_requirement_legacy", "source_files": ["legacy.txt"]}
            )

    def test_effect_replay_after_side_effect_before_ack_is_idempotent(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        task_id = self._create_task(service, "effect_replay")
        self._start_workflow(service,
            {
                "task_id": task_id,
                "workflow_id": "wf_effect_replay",
                "operation": "new_requirement",
                "goal": "Exercise effect replay",
            }
        )
        effect = service.repository.claim_outbox("crash-window-worker", limit=1, lease_seconds=60)[0]
        processor = MinionV2OutboxProcessor(service, semantic_effects=_NoopSemanticEffects())

        asyncio.run(processor._execute_mechanical(effect))
        first = service.repository.read_snapshot(AggregateType.WORKFLOW, "wf_effect_replay")
        asyncio.run(processor._execute_mechanical(effect))
        replayed = service.repository.read_snapshot(AggregateType.WORKFLOW, "wf_effect_replay")

        self.assertEqual(first.state, "ACTIVE")
        self.assertEqual(replayed.version, first.version)

    def test_submission_invariant_failure_is_not_retried(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        task_id = self._create_task(service, "permanent-submit-failure")
        self._start_workflow(
            service,
            {
                "task_id": task_id,
                "workflow_id": "wf_permanent_submit_failure",
                "operation": "new_requirement",
                "goal": "Exercise permanent submit failure handling",
            },
        )
        processor = MinionV2OutboxProcessor(
            service,
            semantic_effects=_PermanentFailureSemanticEffects(),
        )
        for _ in range(6):
            asyncio.run(processor.process_once(limit=10))

        with sqlite3.connect(str(service.repository.db_path)) as connection:
            row = connection.execute(
                """
                SELECT status, attempt_count, last_error
                FROM minion_v2_outbox
                WHERE workflow_id = ? AND status = 'failed'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                ("wf_permanent_submit_failure",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "failed")
        self.assertEqual(row[1], 1)
        self.assertIn("SubmissionInvariantError", row[2])
        snapshots = service.repository.list_workflow_snapshots("wf_permanent_submit_failure")
        self.assertTrue(any(item.state == "TRIAGE_REQUIRED" for item in snapshots))

    def test_manager_restart_defers_semantic_effect_without_triage_or_retry_cost(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        task_id = self._create_task(service, "restart-defer")
        self._start_workflow(
            service,
            {
                "task_id": task_id,
                "workflow_id": "wf_restart_defer",
                "operation": "new_requirement",
                "goal": "Exercise graceful manager restart",
            },
        )
        semantic = _DeferredSemanticEffects()
        processor = MinionV2OutboxProcessor(service, semantic_effects=semantic)
        for _ in range(8):
            asyncio.run(processor.process_once(limit=10))
            if semantic.calls:
                break

        self.assertEqual(semantic.calls, 1)
        with sqlite3.connect(str(service.repository.db_path)) as connection:
            row = connection.execute(
                """
                SELECT status, attempt_count, last_error
                FROM minion_v2_outbox
                WHERE workflow_id = ? AND last_error LIKE 'deferred%manager restart%'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                ("wf_restart_defer",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "pending")
        self.assertEqual(row[1], 0)
        self.assertFalse(
            any(
                item.state == "TRIAGE_REQUIRED"
                for item in service.repository.list_workflow_snapshots("wf_restart_defer")
            )
        )

    def test_pause_settles_only_after_child_pause_confirmation(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        task_id = self._create_task(service, "pause")
        self._start_workflow(service,
            {
                "task_id": task_id,
                "workflow_id": "wf_pause",
                "operation": "new_requirement",
                "goal": "Pauseable workflow",
                "actor": "nathan",
                "source_channel": "socket:test",
            }
        )
        processor = MinionV2OutboxProcessor(service, semantic_effects=_ControlSemanticEffects(service))
        asyncio.run(processor.process_once(limit=10))
        asyncio.run(processor.process_once(limit=10))
        requested = service.control_workflow(
            workflow_id="wf_pause",
            command="pause",
            actor="nathan",
            source_channel="socket:test",
        )
        self.assertEqual(requested["state"], "PAUSE_REQUESTED")
        for _ in range(5):
            asyncio.run(processor.process_once(limit=10))
            if service.workflow_status("wf_pause")["workflow_state"] == "PAUSED":
                break
        status = service.workflow_status("wf_pause")
        self.assertEqual(status["workflow_state"], "PAUSED")
        self.assertEqual(status["liveness"], "paused")

    def test_reconciler_pauses_and_resumes_dependency_blocked_node(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        repository = service.repository
        workflow_id = "wf_blocked_pause"
        epoch_id = "epoch_blocked_pause"
        node_id = f"{epoch_id}:node:downstream"
        repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_WORKFLOW",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="test",
                expected_version=0,
                idempotency_key="blocked-pause:create-workflow",
            )
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="START_WORKFLOW",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="test",
                expected_version=1,
                idempotency_key="blocked-pause:start-workflow",
            )
        )
        manifest_ref = service.artifacts.put_json(
            {"architecture": "blocked pause"},
            artifact_type="TestManifestArtifact",
        ).to_dict()
        topology_ref = service.artifacts.put_json(
            {"modules": {"downstream": {"depends_on": ["upstream"]}}},
            artifact_type="ConstructionTopologyArtifact",
        ).to_dict()
        contract_ref = service.artifacts.put_json(
            {"module_name": "downstream"},
            artifact_type="ModuleContractArtifact",
        ).to_dict()
        repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_EXECUTION_EPOCH",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.EXECUTION_EPOCH,
                aggregate_id=epoch_id,
                actor="test",
                expected_version=0,
                idempotency_key="blocked-pause:create-epoch",
                payload={
                    "architecture_manifest_ref": manifest_ref,
                    "topology_ref": topology_ref,
                },
            )
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="START_EXECUTION",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.EXECUTION_EPOCH,
                aggregate_id=epoch_id,
                actor="test",
                expected_version=1,
                idempotency_key="blocked-pause:start-epoch",
            )
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_NODE_RUN",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=node_id,
                actor="test",
                expected_version=0,
                idempotency_key="blocked-pause:create-node",
                payload={
                    "unit_contract_ref": contract_ref,
                    "epoch_id": epoch_id,
                    "unit_id": "downstream",
                    "node_kind": "unit",
                    "dependency_node_ids": [f"{epoch_id}:node:upstream"],
                },
            )
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="NODES_COMPILED",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.EXECUTION_EPOCH,
                aggregate_id=epoch_id,
                actor="test",
                expected_version=2,
                idempotency_key="blocked-pause:nodes-compiled",
                payload={"node_ids": [node_id]},
            )
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="LINK_EXECUTION_EPOCH",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="test",
                expected_version=2,
                idempotency_key="blocked-pause:link-epoch",
                payload={"execution_epoch_id": epoch_id},
            )
        )
        with sqlite3.connect(str(repository.db_path)) as connection:
            connection.execute(
                "UPDATE minion_v2_outbox SET status = 'completed' WHERE workflow_id = ?",
                (workflow_id,),
            )
        service.control_workflow(
            workflow_id=workflow_id,
            command="pause",
            actor="nathan",
            source_channel="socket:test",
        )

        reconcile_control_requests(repository, workflow_id)

        epoch = repository.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch_id)
        node = repository.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
        self.assertEqual(epoch.state, "PAUSE_REQUESTED")
        self.assertEqual(node.state, "PAUSE_REQUESTED")
        repository.dispatch(
            ActionEnvelope(
                action_type="PAUSE_CONFIRMED",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=node_id,
                actor="test",
                expected_version=node.version,
                idempotency_key="blocked-pause:node-paused",
            )
        )
        reconcile_control_requests(repository, workflow_id)
        self.assertEqual(
            repository.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch_id).state,
            "PAUSED",
        )
        self.assertEqual(
            repository.read_snapshot(AggregateType.WORKFLOW, workflow_id).state,
            "PAUSED",
        )

        service.resume_workflow(
            workflow_id=workflow_id,
            actor="nathan",
            source_channel="socket:test",
        )
        reconcile_control_requests(repository, workflow_id)
        self.assertEqual(
            repository.read_snapshot(AggregateType.DAG_NODE_RUN, node_id).state,
            "BLOCKED_BY_DEPS",
        )
        self.assertEqual(
            repository.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch_id).state,
            "RUNNING",
        )

    def test_failed_cancel_effect_enters_triage_and_replays_cancel_after_resolution(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        service.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_WORKFLOW",
                workflow_id="wf_cancel_recovery",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="wf_cancel_recovery",
                actor="test",
                expected_version=0,
                idempotency_key="cancel-recovery:create-workflow",
            )
        )
        service.repository.dispatch(
            ActionEnvelope(
                action_type="START_WORKFLOW",
                workflow_id="wf_cancel_recovery",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="wf_cancel_recovery",
                actor="test",
                expected_version=1,
                idempotency_key="cancel-recovery:start-workflow",
            )
        )
        service.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_ARCHITECTURE_REVISION",
                workflow_id="wf_cancel_recovery",
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id="arch_cancel_recovery",
                actor="test",
                expected_version=0,
                idempotency_key="cancel-recovery:create-revision",
            )
        )
        service.repository.dispatch(
            ActionEnvelope(
                action_type="LINK_ARCHITECTURE_REVISION",
                workflow_id="wf_cancel_recovery",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="wf_cancel_recovery",
                actor="test",
                expected_version=2,
                idempotency_key="cancel-recovery:link-revision",
                payload={"architecture_revision_id": "arch_cancel_recovery"},
            )
        )
        with sqlite3.connect(str(service.repository.db_path)) as connection:
            connection.execute(
                "UPDATE minion_v2_outbox SET status = 'completed' WHERE workflow_id = ?",
                ("wf_cancel_recovery",),
            )
        revision = service.repository.read_snapshot(
            AggregateType.ARCHITECTURE_REVISION,
            "arch_cancel_recovery",
        )
        service.repository.dispatch(
            ActionEnvelope(
                action_type="REQUEST_CANCEL",
                workflow_id="wf_cancel_recovery",
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id="arch_cancel_recovery",
                actor="test",
                expected_version=revision.version,
                idempotency_key="cancel-recovery:request-cancel",
            )
        )

        failing = MinionV2OutboxProcessor(
            service,
            semantic_effects=_PermanentFailureSemanticEffects(),
        )
        asyncio.run(failing.process_once(limit=1))
        triaged = service.repository.read_snapshot(
            AggregateType.ARCHITECTURE_REVISION,
            "arch_cancel_recovery",
        )
        self.assertEqual(triaged.state, "TRIAGE_REQUIRED")
        self.assertEqual(triaged.payload["triage_resume_state"], "CANCEL_REQUESTED")

        service.repository.dispatch(
            ActionEnvelope(
                action_type="RESOLVE_TRIAGE",
                workflow_id="wf_cancel_recovery",
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id="arch_cancel_recovery",
                actor="test",
                expected_version=triaged.version,
                idempotency_key="cancel-recovery:resolve-triage",
            )
        )
        asyncio.run(failing.process_once(limit=1))
        retriaged = service.repository.read_snapshot(
            AggregateType.ARCHITECTURE_REVISION,
            "arch_cancel_recovery",
        )
        self.assertEqual(retriaged.state, "TRIAGE_REQUIRED")
        self.assertEqual(retriaged.payload["triage_resume_state"], "CANCEL_REQUESTED")
        service.repository.dispatch(
            ActionEnvelope(
                action_type="RESOLVE_TRIAGE",
                workflow_id="wf_cancel_recovery",
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id="arch_cancel_recovery",
                actor="test",
                expected_version=retriaged.version,
                idempotency_key="cancel-recovery:resolve-triage-again",
            )
        )
        recovery = MinionV2OutboxProcessor(
            service,
            semantic_effects=SemanticOrchestrator(service),
        )
        asyncio.run(recovery.process_once(limit=1))
        cancelled = service.repository.read_snapshot(
            AggregateType.ARCHITECTURE_REVISION,
            "arch_cancel_recovery",
        )
        self.assertEqual(cancelled.state, "CANCELLED")

    def test_workflow_triage_freezes_epoch_without_bypassing_node_owner(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        repository = service.repository
        repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_WORKFLOW",
                workflow_id="wf_triage_hierarchy",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="wf_triage_hierarchy",
                actor="test",
                expected_version=0,
                idempotency_key="triage-hierarchy:create-workflow",
            )
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="START_WORKFLOW",
                workflow_id="wf_triage_hierarchy",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="wf_triage_hierarchy",
                actor="test",
                expected_version=1,
                idempotency_key="triage-hierarchy:start-workflow",
            )
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_EXECUTION_EPOCH",
                workflow_id="wf_triage_hierarchy",
                aggregate_type=AggregateType.EXECUTION_EPOCH,
                aggregate_id="epoch_triage_hierarchy",
                actor="test",
                expected_version=0,
                idempotency_key="triage-hierarchy:create-epoch",
                payload={
                    "architecture_manifest_ref": {"sha256": "manifest"},
                    "topology_ref": {"sha256": "topology"},
                },
            )
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="START_EXECUTION",
                workflow_id="wf_triage_hierarchy",
                aggregate_type=AggregateType.EXECUTION_EPOCH,
                aggregate_id="epoch_triage_hierarchy",
                actor="test",
                expected_version=1,
                idempotency_key="triage-hierarchy:start-epoch",
            )
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_NODE_RUN",
                workflow_id="wf_triage_hierarchy",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node_triage_hierarchy",
                actor="test",
                expected_version=0,
                idempotency_key="triage-hierarchy:create-node",
                payload={
                    "unit_contract_ref": {"sha256": "contract"},
                    "epoch_id": "epoch_triage_hierarchy",
                    "node_kind": "unit",
                    "dependency_node_ids": [],
                },
            )
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="DEPENDENCIES_ACCEPTED",
                workflow_id="wf_triage_hierarchy",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node_triage_hierarchy",
                actor="test",
                expected_version=1,
                idempotency_key="triage-hierarchy:node-ready",
                payload={"accepted_dependency_node_ids": []},
            )
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="NODES_COMPILED",
                workflow_id="wf_triage_hierarchy",
                aggregate_type=AggregateType.EXECUTION_EPOCH,
                aggregate_id="epoch_triage_hierarchy",
                actor="test",
                expected_version=2,
                idempotency_key="triage-hierarchy:nodes-compiled",
                payload={"node_ids": ["node_triage_hierarchy"]},
            )
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="LINK_EXECUTION_EPOCH",
                workflow_id="wf_triage_hierarchy",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="wf_triage_hierarchy",
                actor="test",
                expected_version=2,
                idempotency_key="triage-hierarchy:link-epoch",
                payload={"execution_epoch_id": "epoch_triage_hierarchy"},
            )
        )
        with sqlite3.connect(str(repository.db_path)) as connection:
            connection.execute(
                "UPDATE minion_v2_outbox SET status = 'completed' WHERE workflow_id = ?",
                ("wf_triage_hierarchy",),
            )
        workflow = repository.read_snapshot(AggregateType.WORKFLOW, "wf_triage_hierarchy")
        repository.dispatch(
            ActionEnvelope(
                action_type="ENTER_TRIAGE",
                workflow_id="wf_triage_hierarchy",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="wf_triage_hierarchy",
                actor="test",
                expected_version=workflow.version,
                idempotency_key="triage-hierarchy:enter-triage",
                payload={"blocker": {"kind": "test"}},
            )
        )

        processor = MinionV2OutboxProcessor(service, semantic_effects=_NoopSemanticEffects())
        asyncio.run(processor.process_once(limit=1))

        epoch = repository.read_snapshot(
            AggregateType.EXECUTION_EPOCH,
            "epoch_triage_hierarchy",
        )
        node = repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            "node_triage_hierarchy",
        )
        self.assertEqual(epoch.state, "PAUSE_REQUESTED")
        self.assertEqual(node.state, "PAUSE_REQUESTED")
        with sqlite3.connect(str(repository.db_path)) as connection:
            pending_effect_types = {
                str(row[0])
                for row in connection.execute(
                    "SELECT effect_type FROM minion_v2_outbox WHERE workflow_id = ? AND status = 'pending'",
                    ("wf_triage_hierarchy",),
                )
            }
        self.assertIn("pause_epoch_nodes", pending_effect_types)
        self.assertIn("pause_role", pending_effect_types)

    def test_terminal_child_effect_failure_escalates_to_active_workflow(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        repository = service.repository
        workflow_id = "wf_terminal_effect_failure"
        revision_id = "arch_terminal_effect_failure"
        repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_WORKFLOW",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="test",
                expected_version=0,
                idempotency_key="terminal-effect:create-workflow",
            )
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="START_WORKFLOW",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="test",
                expected_version=1,
                idempotency_key="terminal-effect:start-workflow",
            )
        )
        manifest_ref = service.artifacts.put_json(
            {"architecture": "accepted"},
            artifact_type="TestManifestArtifact",
        ).to_dict()
        repository.dispatch(
            ActionEnvelope(
                action_type="IMPORT_ARCHITECTURE_REVISION",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision_id,
                actor="test",
                expected_version=0,
                idempotency_key="terminal-effect:import-revision",
                payload={"architecture_manifest_ref": manifest_ref},
            )
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="LINK_ARCHITECTURE_REVISION",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="test",
                expected_version=2,
                idempotency_key="terminal-effect:link-revision",
                payload={"architecture_revision_id": revision_id},
            )
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="START_ARCHITECTURE_REVIEW",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision_id,
                actor="test",
                expected_version=1,
                idempotency_key="terminal-effect:start-review",
                payload={"fencing_token": 1},
            )
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="ARCHITECTURE_REVIEW_PASSED",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision_id,
                actor="test",
                expected_version=2,
                idempotency_key="terminal-effect:review-pass",
                payload={
                    "review_artifact_ref": {"sha256": "review"},
                    "architecture_manifest_ref": manifest_ref,
                },
            )
        )
        with sqlite3.connect(str(repository.db_path)) as connection:
            connection.execute(
                "UPDATE minion_v2_outbox SET status = 'completed' WHERE workflow_id = ?",
                (workflow_id,),
            )
        decision_token = repository.issue_human_decision_token(
            workflow_id=workflow_id,
            architecture_revision_id=revision_id,
            manifest_sha=str(manifest_ref["sha256"]),
            actor_id="test",
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="HUMAN_ACCEPT",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision_id,
                actor="test",
                source_channel="socket:test",
                expected_version=3,
                idempotency_key="terminal-effect:human-accept",
                payload={
                    "decision_token": decision_token,
                    "architecture_manifest_ref": manifest_ref,
                },
            )
        )
        with sqlite3.connect(str(repository.db_path)) as connection:
            connection.execute(
                """
                UPDATE minion_v2_outbox
                SET status = 'completed'
                WHERE workflow_id = ? AND effect_type != 'materialize_plan_revision'
                """,
                (workflow_id,),
            )

        processor = MinionV2OutboxProcessor(
            service,
            semantic_effects=_PermanentFailureSemanticEffects(),
        )
        asyncio.run(processor.process_once(limit=1))

        revision = repository.read_snapshot(
            AggregateType.ARCHITECTURE_REVISION,
            revision_id,
        )
        workflow = repository.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        self.assertEqual(revision.state, "ACCEPTED")
        self.assertEqual(workflow.state, "TRIAGE_REQUIRED")
        self.assertEqual(workflow.payload["triage_resume_state"], "ACTIVE")
        self.assertEqual(
            workflow.payload["blocker"]["source_aggregate_id"],
            revision_id,
        )

    def test_workflow_reconcile_relinks_an_existing_unlinked_child(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        repository = service.repository
        repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_WORKFLOW",
                workflow_id="wf_relink_child",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="wf_relink_child",
                actor="test",
                expected_version=0,
                idempotency_key="relink-child:create-workflow",
            )
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="START_WORKFLOW",
                workflow_id="wf_relink_child",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="wf_relink_child",
                actor="test",
                expected_version=1,
                idempotency_key="relink-child:start-workflow",
            )
        )
        repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_ARCHITECTURE_REVISION",
                workflow_id="wf_relink_child",
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id="arch_unlinked",
                actor="test",
                expected_version=0,
                idempotency_key="relink-child:create-revision",
            )
        )
        with sqlite3.connect(str(repository.db_path)) as connection:
            connection.execute(
                "UPDATE minion_v2_outbox SET status = 'completed' WHERE workflow_id = ?",
                ("wf_relink_child",),
            )

        processor = MinionV2OutboxProcessor(service, semantic_effects=_NoopSemanticEffects())
        processor._reconcile_workflow(
            {
                "workflow_id": "wf_relink_child",
                "aggregate_type": AggregateType.WORKFLOW.value,
                "aggregate_id": "wf_relink_child",
                "effect_key": "relink-child:reconcile",
            }
        )

        workflow = repository.read_snapshot(AggregateType.WORKFLOW, "wf_relink_child")
        revisions = [
            item
            for item in repository.list_workflow_snapshots("wf_relink_child")
            if item.aggregate_type == AggregateType.ARCHITECTURE_REVISION
        ]
        self.assertEqual(workflow.payload["architecture_revision_id"], "arch_unlinked")
        self.assertEqual([item.aggregate_id for item in revisions], ["arch_unlinked"])

    def test_manager_has_no_v1_spawn_rpc(self) -> None:
        manager = MinionManager(self.runtime_root)
        with self.assertRaisesRegex(ValueError, "unknown Minion V2 manager method: spawn"):
            asyncio.run(manager._call_method("spawn", {"task_context_pack": {}}))
        self.assertEqual(asyncio.run(manager._call_method("v2_wake", {}))["status"], "woken")

    def test_worker_token_cannot_borrow_another_broker_run(self) -> None:
        manager = MinionManager(self.runtime_root)
        manager.role_gateway.authorize = lambda _token: {
            "assignment": {"session_id": "inv-owner"}
        }
        manager.runs["run-other"] = MinionRunState(
            minion_id="inv-other",
            run_id="run-other",
            pack=MinionInvocationPack(invocation_id="inv-other"),
        )

        with self.assertRaisesRegex(PermissionError, "does not own"):
            asyncio.run(
                manager._call_worker_method(
                    "llm_resolve_max_output_tokens",
                    {
                        "access_token": "assignment-token",
                        "run_id": "run-other",
                    },
                )
            )

    def test_manager_answers_one_pending_architect_question_in_place(self) -> None:
        manager = MinionManager(self.runtime_root)
        state = MinionRunState(
            minion_id="inv-architect-question",
            run_id="run-architect-question",
            pack=MinionInvocationPack(
                invocation_id="inv-architect-question",
                metadata={
                    "minion_v2": {
                        "workflow_id": "wf-architect-question",
                        "aggregate_type": "architecture_revision",
                        "aggregate_id": "arch-architect-question",
                        "role": "architect",
                    }
                },
            ),
            pending_clarification={
                "clarification_id": "clarification-1",
                "title": "Compatibility",
                "questions": [
                    {
                        "question_id": "compatibility-boundary",
                        "question": "Which public API is binding?",
                    }
                ],
            },
            status="clarification_pending",
        )
        manager.runs[state.run_id] = state
        controls: list[dict[str, object]] = []
        event_order: list[str] = []

        def append_revision(request: dict[str, object]) -> dict[str, object]:
            event_order.append("ledger")
            self.assertEqual(request["question"], "Which public API is binding?")
            self.assertEqual(request["answer"], "Preserve the checked-in public API.")
            return {
                "appended": True,
                "sequence": 1,
                "requirements_ref": {"sha256": "task-ledger-generation-2"},
            }

        async def send_control(run_id: str, message: dict[str, object]) -> bool:
            event_order.append("worker")
            controls.append({"run_id": run_id, "message": dict(message)})
            return True

        manager.v2_service.append_architect_clarification = append_revision
        manager.v2_semantic_orchestrator.send_worker_control = send_control
        deliveries: list[tuple[dict[str, object], str]] = []
        manager._queue_task_delivery_event = lambda event, *, dedup_key: deliveries.append(  # type: ignore[method-assign]
            (dict(event), dedup_key)
        )
        result = asyncio.run(
            manager.answer_workflow_question(
                {
                    "workflow_id": "wf-architect-question",
                    "answer": "Preserve the checked-in public API.",
                }
            )
        )

        self.assertTrue(result["ok"])
        self.assertEqual(event_order, ["ledger", "worker"])
        self.assertEqual(state.status, "running")
        self.assertEqual(state.pending_clarification, {})
        self.assertEqual(deliveries[0][0]["event_kind"], "clarification_resolved")
        self.assertEqual(
            dict(deliveries[0][0]["payload"])["clarification_id"],
            "clarification-1",
        )
        self.assertEqual(
            deliveries[0][1],
            "clarification-resolved:wf-architect-question:clarification-1",
        )
        self.assertEqual(
            controls,
            [
                {
                    "run_id": state.run_id,
                    "message": {
                        "type": "clarification",
                        "clarification": {
                            "clarification_id": "clarification-1",
                            "run_id": state.run_id,
                            "minion_id": state.minion_id,
                            "answers": [
                                {
                                    "question_id": "compatibility-boundary",
                                    "answer": "Preserve the checked-in public API.",
                                }
                            ],
                            "task_revision": {
                                "appended": True,
                                "sequence": 1,
                                "requirements_ref": {
                                    "sha256": "task-ledger-generation-2"
                                },
                            },
                        },
                    },
                }
            ],
        )

    def test_manager_task_status_projects_pending_architect_question(self) -> None:
        manager = MinionManager(self.runtime_root)
        manager.v2_service.task_status = lambda task_id, *, workflow_id="", view="status": {
            "status": "ok",
            "task": {"name": "Architecture question", "state": "ACTIVE"},
            "workflow": {
                "status": "ok",
                "workflow_id": workflow_id,
                "active_worker": "inv-architect-question",
                "active_worker_role": "architect",
                "active_role_progress": {
                    "activity_observed": True,
                    "checklist": {"completed": 1, "total": 3},
                },
                "next_legal_action": ["control_workflow:pause", "control_workflow:cancel"],
                "waiting_for_user": False,
                "liveness": "live_lease",
            },
        }
        manager.runs["run-architect-question"] = MinionRunState(
            minion_id="inv-architect-question",
            run_id="run-architect-question",
            pack=MinionInvocationPack(
                invocation_id="inv-architect-question",
                metadata={"minion_v2": {"workflow_id": "wf-architect-question"}},
            ),
            pending_clarification={
                "clarification_id": "clarification-1",
                "title": "Compatibility boundary",
                "questions": [
                    {
                        "id": "compatibility",
                        "question": "Which public API is binding?",
                        "options": [
                            {
                                "label": "Preserve",
                                "description": "Keep the checked-in API.",
                            },
                            {
                                "label": "Replace",
                                "description": "Use the new API only.",
                            },
                        ],
                    }
                ],
            },
            status="clarification_pending",
        )

        status = asyncio.run(
            manager._call_method(
                "v2_task_status",
                {
                    "task_id": "task-architect-question",
                    "workflow_id": "wf-architect-question",
                },
            )
        )

        workflow = status["workflow"]
        self.assertTrue(workflow["waiting_for_user"])
        self.assertEqual(workflow["liveness"], "human_wait")
        self.assertEqual(workflow["active_worker"], "")
        self.assertEqual(workflow["active_worker_role"], "")
        self.assertEqual(workflow["active_role_progress"], {})
        self.assertEqual(
            workflow["next_legal_action"],
            ["answer_question", "control_workflow:cancel"],
        )
        self.assertEqual(workflow["pending_question_count"], 1)
        self.assertEqual(
            workflow["pending_question"],
            {
                "title": "Compatibility boundary",
                "question": "Which public API is binding?",
                "options": [
                    {
                        "label": "Preserve",
                        "description": "Keep the checked-in API.",
                    },
                    {
                        "label": "Replace",
                        "description": "Use the new API only.",
                    },
                ],
            },
        )
        self.assertNotIn("clarification_id", json.dumps(workflow["pending_question"]))

    def test_manager_routes_worker_question_through_task_delivery_outbox(self) -> None:
        manager = MinionManager(self.runtime_root)
        state = MinionRunState(
            minion_id="inv-routed-question",
            run_id="run-routed-question",
            pack=MinionInvocationPack(
                invocation_id="inv-routed-question",
                metadata={
                    "prompt_log_enabled": True,
                    "minion_v2": {
                        "workflow_id": "wf-routed-question",
                    }
                },
            ),
        )
        manager.runs[state.run_id] = state
        recorded: list[dict[str, object]] = []
        queued: list[dict[str, object]] = []
        manager.v2_service.repository.read_snapshot = lambda *_args: AggregateSnapshot(
            aggregate_type=AggregateType.WORKFLOW,
            aggregate_id="wf-routed-question",
            workflow_id="wf-routed-question",
            state="ACTIVE",
            version=2,
            payload={"task_id": "task-routed-question"},
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        manager.v2_service.repository.record_worker_event = (
            lambda event: recorded.append(dict(event))
        )
        manager.v2_service.repository.enqueue_task_delivery = lambda **kwargs: {
            "delivery_id": "delivery-routed-question",
            "task_id": kwargs["task_id"],
            "payload": kwargs["payload"],
        }
        manager.events.queue_event = lambda event: queued.append(dict(event))

        asyncio.run(
            manager._publish_v2_worker_event(
                {
                    "event_kind": "clarification_requested",
                    "run_id": state.run_id,
                    "invocation_id": state.pack.invocation_id,
                    "payload": {
                        "clarification_id": "clarification-routed",
                        "control_route": {
                            "endpoint_id": "stale",
                            "channel_kind": "socket",
                            "reply_target": {"connection_id": "old-client"},
                            "control_scope_key": "socket:old-client",
                        },
                        "questions": [
                            {
                                "id": "compatibility",
                                "question": "Which boundary is binding?",
                            }
                        ],
                    },
                }
            )
        )

        self.assertEqual(len(recorded), 1)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["delivery_id"], "delivery-routed-question")
        self.assertEqual(queued[0]["workflow_id"], "wf-routed-question")
        self.assertNotIn("control_route", dict(queued[0]["payload"]))
        self.assertEqual(state.status, "clarification_pending")
        self.assertEqual(
            state.pending_clarification["clarification_id"],
            "clarification-routed",
        )

        asyncio.run(
            manager._publish_v2_worker_event(
                {
                    "event_kind": "terminal",
                    "run_id": state.run_id,
                    "invocation_id": state.pack.invocation_id,
                    "payload": {
                        "status": "cancelled",
                        "summary": "Worker cancelled.",
                    },
                }
            )
        )

        terminal_payload = dict(queued[-1]["payload"])
        self.assertEqual(
            terminal_payload["resolved_interactions"],
            [
                {
                    "interaction_id": "clarification-routed",
                    "interaction_kind": "minion_clarification",
                }
            ],
        )
        self.assertEqual(state.pending_clarification, {})
        self.assertEqual(state.pending_approval, {})
        self.assertEqual(state.status, "exiting")


    def test_manager_workflow_terminal_event_is_deterministic_for_dedup(self) -> None:
        manager = MinionManager(self.runtime_root)
        queued: list[tuple[dict[str, object], str]] = []
        manager._queue_task_delivery_event = lambda event, *, dedup_key: queued.append(  # type: ignore[method-assign]
            (dict(event), dedup_key)
        )
        payload = {
            "workflow_id": "wf-terminal-dedup",
            "status": "completed",
            "summary": "Minion workflow completed.",
            "terminal_at": "2026-08-10T02:59:11+00:00",
            "result_artifact_ref": {"sha256": "deliverable"},
        }

        manager._publish_v2_workflow_event(payload)
        manager._publish_v2_workflow_event(payload)

        self.assertEqual(queued[0], queued[1])
        self.assertEqual(
            queued[0][1],
            "workflow-terminal:wf-terminal-dedup:completed",
        )

    def test_manager_architecture_resolution_event_is_nonterminal_and_deduplicated(self) -> None:
        manager = MinionManager(self.runtime_root)
        queued: list[tuple[dict[str, object], str]] = []
        manager._queue_task_delivery_event = lambda event, *, dedup_key: queued.append(  # type: ignore[method-assign]
            (dict(event), dedup_key)
        )
        payload = {
            "event_kind": "architecture_review_resolved",
            "workflow_id": "wf-review-resolved",
            "architecture_revision_id": "revision-7",
            "status": "accepted",
            "summary": "Minion architecture decision recorded (accepted).",
            "resolved_at": "2026-08-10T05:00:00+00:00",
        }

        manager._publish_v2_workflow_event(payload)
        manager._publish_v2_workflow_event(payload)

        self.assertEqual(queued[0], queued[1])
        self.assertEqual(
            queued[0][0]["event_kind"],
            "architecture_review_resolved",
        )
        self.assertEqual(
            queued[0][1],
            "architecture-review-resolved:wf-review-resolved:revision-7",
        )

    def test_graceful_manager_shutdown_drains_before_stopping(self) -> None:
        async def scenario() -> None:
            manager = MinionManager(self.runtime_root)
            result = manager.request_shutdown(
                reason="plugin_reload",
                timeout_seconds=1.0,
                graceful=True,
            )
            self.assertEqual(result["status"], "draining")
            self.assertTrue(manager._drain_requested.is_set())
            self.assertFalse(manager._shutdown_event.is_set())
            await manager._drain_task
            self.assertTrue(manager._shutdown_event.is_set())

        asyncio.run(scenario())

    def test_graceful_manager_shutdown_requests_worker_restart_safe_point(self) -> None:
        async def scenario() -> None:
            manager = MinionManager(self.runtime_root)
            process = SimpleNamespace(returncode=None, pid=4242)
            pack = MinionInvocationPack(invocation_id="inv-drain-worker")
            state = MinionRunState(
                minion_id="inv-drain-worker",
                run_id="run-drain-worker",
                pack=pack,
                process=process,
            )
            manager.runs[state.run_id] = state
            controls: list[dict] = []

            async def send_control(run_id, message):
                controls.append({"run_id": run_id, "message": dict(message)})
                state.status = "suspended"
                process.returncode = 0
                return True

            manager.v2_semantic_orchestrator.send_worker_control = send_control
            manager.request_shutdown(
                reason="plugin_reload",
                timeout_seconds=1.0,
                graceful=True,
            )
            await manager._drain_task

            self.assertEqual(controls[0]["run_id"], state.run_id)
            self.assertEqual(controls[0]["message"]["type"], "restart_requested")
            self.assertTrue(manager._shutdown_event.is_set())

        asyncio.run(scenario())

    def test_control_effect_runs_while_semantic_worker_effect_is_inflight(self) -> None:
        async def scenario() -> None:
            service = MinionV2WorkflowService(self.runtime_root)
            task_id = self._create_task(service, "concurrent_control")
            self._start_workflow(service,
                {
                    "task_id": task_id,
                    "workflow_id": "wf_concurrent_control",
                    "operation": "new_requirement",
                    "goal": "Long running architecture",
                    "actor": "nathan",
                    "source_channel": "socket:test",
                }
            )
            semantic = _SlowSemanticEffects()
            processor = MinionV2OutboxProcessor(service, semantic_effects=semantic)
            await processor.process_once(limit=1)
            await processor.process_once(limit=1)
            self.assertEqual(processor.start_available(max_concurrency=2), 1)
            await asyncio.wait_for(semantic.started.wait(), timeout=1)
            service.control_workflow(
                workflow_id="wf_concurrent_control",
                command="pause",
                actor="nathan",
                source_channel="socket:test",
            )
            self.assertEqual(processor.start_available(max_concurrency=2), 1)
            for _ in range(20):
                revision = next(
                    item
                    for item in service.repository.list_workflow_snapshots("wf_concurrent_control")
                    if item.aggregate_type == AggregateType.ARCHITECTURE_REVISION
                )
                if revision.state == "PAUSE_REQUESTED":
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(revision.state, "PAUSE_REQUESTED")
            semantic.release.set()
            await asyncio.sleep(0)
            await processor.stop_background()

        asyncio.run(scenario())

    def test_v2_worker_runner_skips_milestone_and_checkpoint_protocol(self) -> None:
        async def scenario() -> list[dict]:
            events: list[dict] = []

            async def write_event(event):
                events.append(dict(event))

            async def read_decision(timeout=None):
                _ = timeout
                return None

            runner = _SingleInvocationRunner(
                runtime_root=self.runtime_root,
                pack=MinionInvocationPack(
                    invocation_id="v2_invocation",
                    goal="one contract invocation",
                    metadata={"minion_v2": {"workflow_id": "wf"}},
                ),
                minion_id="inv_test",
                run_id="run_test",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=_FakeRuntimeBundle(),
            )
            self.assertEqual(await runner.run(), 0)
            return events

        events = asyncio.run(scenario())
        self.assertIn("invocation_started", [item.get("payload", {}).get("phase") for item in events])
        self.assertNotIn("checkpoint", [item.get("event_kind") for item in events])
        self.assertNotIn("milestone_completed", [item.get("event_kind") for item in events])


if __name__ == "__main__":
    unittest.main()
