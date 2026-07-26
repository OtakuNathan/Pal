from __future__ import annotations

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

from pal.core.runtime import PalCore
from pal.execution.contracts import CapabilityCall
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
from pal.minion.v2.contract_builder import ARCHITECT_BUILDER_CAPABILITIES
from pal.minion.v2.execution import WorkspaceProcessHolder
from pal.minion.v2.orchestration import (
    MinionV2OutboxProcessor,
    _execution_epoch_id,
    reconcile_control_requests,
)
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.minion.v2.sessions import (
    architect_session_id,
    coder_session_id,
    module_verifier_session_id,
)
from pal.minion.v2.semantic_orchestration.orchestrator import (
    SemanticOrchestrator,
    _assignment_role_input_refs,
    _architecture_submit_idempotency_key,
    _bind_architecture_edit_instruction_for_review,
    _bind_role_attempt_sandbox,
    _candidate_tree_fingerprint,
    _durable_workspace_preparation,
    _named_json_output,
    _prepare_role_workspace_before_environment,
    _implementation_action_idempotency_key,
    _raise_if_workspace_held,
    _role_uses_bound_durable_workspace,
    _semantic_role_input_refs,
    _skeleton_architecture_review_view,
    _skeleton_architect_instruction,
    _recorded_role_metrics,
    _worker_event_timing,
    apply_v2_revision_scope_capability_policy,
    apply_v2_role_capability_policy,
)
from pal.minion.v2.role_protocol import RoleAssignmentRequest, RoleAssignmentState
from pal.minion.v2.role_protocol import stable_hash
from pal.minion.v2.skeleton import ArchitectureWorkspace
from pal.minion.v2 import ActionEnvelope, AggregateType
from pal.minion.v2.contracts import (
    AggregateSnapshot,
    DeferredEffectError,
    StaleFencingToken,
    SubmissionInvariantError,
)
from pal.minion.manager import MinionManager, MinionRunState
from pal.minion.prompt_adapter import render_minion_task_prompt
from pal.minion.runner import MinionAgentLoopState, MinionRunner
from pal.shared import IntrospectionCall, LLMFinishReason, MinionInvocationPack, RuntimeStatus


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

    def test_writers_use_bound_durable_workspace_while_reviewers_remain_isolated(self) -> None:
        writable = {"repo_path": "/tmp/node-worktree"}
        read_only = {
            "repo_path": "/tmp/review-worktree",
            "workspace_policy": {"mode": "read_only_repo"},
        }

        for role in ("architect", "implementation"):
            self.assertTrue(_role_uses_bound_durable_workspace(role, writable), role)
            self.assertFalse(_role_uses_bound_durable_workspace(role, read_only), role)
        for role in ("reviewer", "verifier"):
            self.assertFalse(_role_uses_bound_durable_workspace(role, writable), role)

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
                "workspace_preparation": {"sha256": "verification-environment-sha"},
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

    def test_architecture_submit_dedup_key_distinguishes_state_machine_cycles(self) -> None:
        first = _architecture_submit_idempotency_key("arch-1", 7, "same-submission")
        replay = _architecture_submit_idempotency_key("arch-1", 7, "same-submission")
        next_cycle = _architecture_submit_idempotency_key("arch-1", 12, "same-submission")

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
        repo.mkdir()
        task.mkdir()
        (task / "task.yaml").write_text("immutable task\n", encoding="utf-8")
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
        retry_attempt = _bind_role_attempt_sandbox(
            self.runtime_root,
            first_attempt,
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
        role_socket = self.runtime_root / "data" / "minion-role" / "role.sock"
        role_socket.parent.mkdir(parents=True, exist_ok=True)
        role_socket.write_text("test endpoint", encoding="utf-8")
        argv, _ = build_sandboxed_runner_invocation(
            runtime_root=self.runtime_root,
            pack=retry_attempt,
            argv=["/bin/true"],
        )
        self.assertIn(str(task), argv)

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

    def test_candidate_receipt_reuse_ignores_manager_progress_journal_drift(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        worker = SemanticOrchestrator(service)
        submitted_view = service.artifacts.put_json(
            {
                "schema_version": "2",
                "module_name": "router",
                "module": {"responsibility": "Route requests deterministically."},
                "requirements": {"routing": {"owner": "router"}},
                "node_run_journal": {
                    "current_micro_plan": ["implement contract"],
                    "last_safe_point": "worker_started",
                },
            },
            artifact_type="ModuleWorkViewArtifact",
        )
        resumed_view = service.artifacts.put_json(
            {
                "schema_version": "2",
                "module_name": "router",
                "module": {"responsibility": "Route requests deterministically."},
                "requirements": {"routing": {"owner": "router"}},
                "node_run_journal": {
                    "current_micro_plan": [],
                    "completed_checklist": ["implement contract"],
                    "files_changed": ["src/router.py"],
                    "last_safe_point": "candidate_submitted",
                },
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
            input_refs={"unit_work_view": resumed_view.to_dict()},
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
                "node_run_journal": {"last_safe_point": "candidate_submitted"},
            },
            artifact_type="ModuleWorkViewArtifact",
        )
        changed_view = service.artifacts.put_json(
            {
                "schema_version": "2",
                "module_name": "router",
                "module": {"responsibility": "Route requests with retry semantics."},
                "node_run_journal": {"last_safe_point": "worker_started"},
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

    def test_background_worker_supervisor_enforces_global_slot_limit(self) -> None:
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
            with self.assertRaisesRegex(DeferredEffectError, "no available execution slot"):
                await worker._launch_background_worker(
                    {
                        "effect_id": "effect-slot-two",
                        "effect_key": "effect-key-slot-two",
                    },
                    first_runner,
                )
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
                executor_profile_id="software_engineering.v2_coder",
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
                    executor_profile_id="software_engineering.v2_coder",
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
                executor_profile_id="software_engineering.v2_coder",
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
                        executor_profile_id="software_engineering.v2_coder",
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
            executor_profile_id="software_engineering.v2_coder",
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
                executor_profile_id="software_engineering.v2_coder",
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
            executor_profile_id="software_engineering.v2_coder",
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
                    executor_profile_id="software_engineering.v2_coder",
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
                executor_profile_id="software_engineering.v2_reviewer",
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
                    executor_profile_id="software_engineering.v2_reviewer",
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
                executor_profile_id="software_engineering.v2_coder",
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
                    executor_profile_id="software_engineering.v2_coder",
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
                executor_profile_id="software_engineering.v2_reviewer",
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
                    executor_profile_id="software_engineering.v2_reviewer",
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
        self.assertIn("op_minion_contract_unit_upsert", initial.allowed_capabilities)
        self.assertIn("op_minion_contract_submit", initial.allowed_capabilities)

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
        processor.repository.dispatch = lambda action: actions.append(action)
        processor._link_workflow = lambda *_args: None

        processor._create_revision({"effect_key": "event-edit:0"})

        self.assertEqual(completed, [])
        self.assertEqual([action.action_type for action in actions], ["CREATE_ARCHITECTURE_REVISION"])
        self.assertEqual(actions[0].payload["parent_revision_id"], previous.aggregate_id)
        self.assertEqual(
            actions[0].payload["architecture_cycle_id"],
            previous.aggregate_id,
        )

    def test_runner_checkpoint_uses_manager_selected_paths_and_ignores_stale_files(self) -> None:
        run_dir = self.runtime_root / "agent-session"
        run_dir.mkdir(parents=True)
        first_output = run_dir / "attempt-1" / "continuation-output.json"
        first_pack = MinionInvocationPack(
            invocation_id="inv-session-checkpoint",
            instruction="initial assignment",
            workspace={"run_dir": str(run_dir)},
            metadata={
                "agent_session": {
                    "session_id": "inv-session-checkpoint",
                    "response_key": "effect-1",
                    "fencing_token": 3,
                    "scope_kind": "module_run",
                    "subject_key": "node-router",
                    "continuation_output_path": str(first_output),
                }
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
        state = SimpleNamespace(llm_round_count=8, tool_call_count=21)
        continuation = SimpleNamespace(
            pending_tool_call_batch=[],
            pending_tool_results=[],
            tool_protocol_messages=[{"role": "assistant", "content": "inspected the boundary"}],
            tool_batch_count=6,
            preferred_llm_endpoint_id="glm",
            preferred_llm_model_id="glm-5.2",
        )
        first._persist_agent_session_checkpoint(
            first_pack.workspace,
            state,
            continuation,
            initial_instruction="initial assignment",
            response_keys=["effect-1"],
        )
        (run_dir / "session-continuation-999.json").write_text(
            json.dumps(
                {
                    "schema_version": "3",
                    "session_id": "inv-session-checkpoint",
                    "scope_kind": "wrong",
                    "subject_key": "wrong",
                    "llm_round_count": 999,
                }
            ),
            encoding="utf-8",
        )

        second_output = run_dir / "attempt-2" / "continuation-output.json"
        second_pack = MinionInvocationPack(
            invocation_id="inv-session-checkpoint",
            instruction="repair reviewer finding",
            workspace={"run_dir": str(run_dir)},
            metadata={
                "agent_session": {
                    "session_id": "inv-session-checkpoint",
                    "response_key": "effect-2",
                    "fencing_token": 4,
                    "scope_kind": "module_run",
                    "subject_key": "node-router",
                    "continuation_input_path": str(first_output),
                    "continuation_output_path": str(second_output),
                }
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
        )
        self.assertEqual(restored["llm_round_count"], 8)
        self.assertEqual(restored["tool_call_count"], 21)
        self.assertEqual(restored["response_keys"], ["effect-1"])
        self.assertEqual(restored["tool_protocol_messages"], continuation.tool_protocol_messages)
        self.assertEqual(restored["scope_kind"], "module_run")
        self.assertEqual(restored["subject_key"], "node-router")
        self.assertTrue(first_output.is_file())

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
            "pal.minion.v2.candidate_builder.candidate_checklist_context",
            side_effect=["pending: implement", "completed: implement"],
        ) as render:
            self.assertEqual(runner._render_durable_role_context(), "pending: implement")
            self.assertEqual(runner._render_durable_role_context(), "completed: implement")
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
                SimpleNamespace(pending_tool_call_batch=[object()], pending_tool_results=[])
            )
        )
        self.assertTrue(
            runner._continuation_is_restart_safe(
                SimpleNamespace(pending_tool_call_batch=[], pending_tool_results=[])
            )
        )
        with self.assertRaisesRegex(Exception, "reload at safe point"):
            asyncio.run(runner._raise_if_restart_requested())

    def test_runner_keys_new_session_inputs_by_assignment_not_repeated_text(self) -> None:
        restored = {
            "initial_instruction": "implement the bound module",
            "response_keys": ["assignment-produce"],
            "tool_protocol_messages": [
                {"role": "assistant", "content": "implementation is ready"},
            ],
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

    @staticmethod
    def _start_workflow(service: MinionV2WorkflowService, request: dict):
        data = dict(request)
        if (
            str(data.get("operation") or "new_requirement") == "new_requirement"
            and not data.get("requirements_ref")
            and not data.get("task_spec")
        ):
            data["task_spec"] = {"objective": str(data.get("goal") or "test task")}
        return service.start_workflow(data)

    def test_supporting_artifact_does_not_complete_required_primary_output(self) -> None:
        pack = MinionInvocationPack(
            invocation_id="inv-primary",
            workspace={"output_policy": {"primary_artifact": "architecture_bundle.json"}},
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
            {"role": "primary", "relative_path": "architecture_bundle.json", "path": "/tmp/architecture_bundle.json"}
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
            memory_l3=SimpleNamespace(),
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
            memory_l3=SimpleNamespace(),
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
            workspace={"output_policy": {"primary_artifact": "architecture_submission.json"}},
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
        instruction = _skeleton_architect_instruction(
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
        self.assertIn("Read revision_finding before any other work", instruction)
        self.assertIn("package/__init__.py", instruction)
        self.assertIn("Do not report the earlier submit as completion", instruction)
        self.assertIn("Call architecture_submit again", instruction)

        scoped = _skeleton_architect_instruction(
            finding={"summary": "One physical reference is invalid."},
            has_base_manifest=True,
            has_revision_scope=True,
        )
        self.assertIn("Read the bound revision_scope before editing", scoped)
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
                {"effect_id": "eff-duplicate", "payload": {"stage": "architect"}}
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

        def capture_dispatch(action):
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
        worker.repository.dispatch = lambda action: actions.append(action)
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
        worker.repository.dispatch = lambda action: actions.append(action)
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
        worker.repository.dispatch = lambda action: actions.append(action)

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
                        "path": "tests/router/verification",
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
        submission_path = self.runtime_root / "architecture_submission.json"
        submission_path.write_text(
            json.dumps({"modules": {"router": {}}}),
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
                        "artifacts": [{"path": str(submission_path), "role": "primary"}],
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
        worker.repository.dispatch = lambda _action: SimpleNamespace(snapshot=running)
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
        constraints_ref = service.artifacts.put_json([], artifact_type="GlobalConstraintsArtifact")
        gates_ref = service.artifacts.put_json([], artifact_type="ArchitectureGateChecksArtifact")
        unit_ref = service.artifacts.put_json({"unit_id": "foundation"}, artifact_type="UnitContractArtifact")
        topology_ref = service.artifacts.put_json({"depends_on": {"foundation": []}}, artifact_type="TopologyArtifact")
        integration_ref = service.artifacts.put_json({"depends_on": ["foundation"]}, artifact_type="IntegrationContractArtifact")
        assumptions_ref = service.artifacts.put_json({"assumptions": []}, artifact_type="AssumptionLedgerArtifact")
        risks_ref = service.artifacts.put_json({"risks": []}, artifact_type="RiskLedgerArtifact")
        manifest_ref = service.artifacts.put_json(
            {
                "requirements_ref": requirements_ref.to_dict(),
                "global_constraints_ref": constraints_ref.to_dict(),
                "gate_checks_ref": gates_ref.to_dict(),
                "unit_contract_refs": [unit_ref.to_dict()],
                "cross_unit_contract_refs": [],
                "topology_ref": topology_ref.to_dict(),
                "integration_contract_ref": integration_ref.to_dict(),
                "assumption_ledger_ref": assumptions_ref.to_dict(),
                "risk_ledger_ref": risks_ref.to_dict(),
            },
            artifact_type="ArchitectureContractArtifact",
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
        self.assertEqual(scope["context"][0]["target"]["name"], "foundation")
        self.assertNotIn("id", scope["context"][0]["target"])
        self.assertIn("scoped revision", instruction)
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

    def test_architect_revision_seeds_the_contract_builder_draft(self) -> None:
        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        captured: dict[str, object] = {}
        ref = worker.service.artifacts.put_json({"base": True}, artifact_type="ArchitectureContractArtifact")
        snapshot = SimpleNamespace(
            workflow_id="wf-seeded-revision",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch-seeded-revision",
            payload={"research_mode": "local_only", "base_architecture_manifest_ref": ref.to_dict()},
        )
        worker.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_WORKFLOW",
                workflow_id=snapshot.workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=snapshot.workflow_id,
                actor="test",
                expected_version=0,
            )
        )
        worker.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_ARCHITECTURE_REVISION",
                workflow_id=snapshot.workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=snapshot.aggregate_id,
                actor="test",
                expected_version=0,
                payload={"architecture_cycle_id": snapshot.aggregate_id},
            )
        )
        worker.repository.read_snapshot = lambda *_args: SimpleNamespace(
            payload={"family_binding_ref": {"sha256": "binding"}}
        )
        worker._base_contract_builder_payload_from_manifest = lambda _ref: {"seed": "base"}

        def record(**_kwargs) -> None:
            raise RuntimeError("stop-after-seed")

        identity = lambda pack, **_kwargs: pack
        with (
            patch("pal.minion.v2.semantic_orchestration.orchestrator.seed_contract_builder_draft") as seed,
            patch("pal.minion.v2.semantic_orchestration.orchestrator.workflow_request_from_snapshot", return_value={"workspace": {"kind": "new_project"}}),
            patch.object(
                worker.service.artifacts,
                "read_json",
                return_value={
                    "schema_version": "3",
                    "policies": {},
                    "role_bindings": {
                        "architect": {
                            "selector": "software_engineering.v2_architect",
                            "executor_profile": {
                                "profile_id": "v2_architect",
                                "profile_group": "software_engineering",
                                "canonical_profile_id": "software_engineering.v2_architect",
                            },
                        }
                    },
                },
            ),
            patch("pal.minion.v2.semantic_orchestration.orchestrator.resolve_pinned_minion_pack", lambda pack, **_kwargs: pack),
            patch("pal.minion.v2.semantic_orchestration.orchestrator.apply_v2_role_capability_policy", identity),
            patch("pal.minion.v2.semantic_orchestration.orchestrator.apply_v2_research_capability_policy", identity),
            patch("pal.minion.v2.semantic_orchestration.orchestrator.sanitize_runner_session_pack", identity),
            patch("pal.minion.v2.semantic_orchestration.orchestrator.with_minion_sandbox_metadata", lambda _root, pack, **_kwargs: pack),
            patch.object(worker.repository, "record_role_invocation", side_effect=record),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop-after-seed"):
                asyncio.run(
                    worker._run_profile(
                        effect={"effect_id": "eff-seed", "effect_key": "event:seed"},
                        snapshot=snapshot,
                        invocation_id="inv_seeded_revision",
                        lease_resource="architecture:arch-seeded-revision:architect",
                        fencing_token=1,
                        profile="software_engineering.v2_architect",
                        activation=RoleActivation(
                            OrchestrationRole.ARCHITECT,
                            RoleMode.REVISION,
                        ),
                        instruction="repair only the finding",
                        reference_refs={},
                        prepare_workspace=False,
                    )
                )
        self.assertEqual(seed.call_count, 1)
        self.assertEqual(seed.call_args.args[1], {"seed": "base"})
        self.assertIsNone(seed.call_args.kwargs["revision_scope"])

    def test_scoped_revision_reuses_unmodified_fragment_refs(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        worker = SemanticOrchestrator(service)
        requirements = service.task_ledger.publish(
            title="Implement the module",
            task_spec={"objective": "Implement the module."},
            actor="test",
            source_channel="test",
        )

        def unit(unit_id: str) -> dict[str, object]:
            return {
                "unit_id": unit_id,
                "unit_behavior_kind": "stateless",
                "responsibility": f"Own {unit_id}.",
                "owned_area": [unit_id],
                "reference_only_paths": [],
                "provided_interfaces": [{"name": f"{unit_id}_service"}],
                "consumed_interfaces": [],
                "ownership": {"rule": f"{unit_id} exclusively owns its output."},
                "lifecycle": "N/A: stateless",
                "state_model": "stateless",
                "invariants": ["deterministic"],
                "error_behavior": ["Invalid input fails deterministically."],
                "compatibility": ["The public output shape remains stable."],
                "dependency_constraints": [],
                "verification_obligations": [],
                "complexity_budget": {
                    "target_file_count": 1,
                    "estimated_context_tokens": 100,
                    "public_interface_count": 1,
                    "cross_unit_contract_count": 0,
                    "stateful_resource_count": 0,
                    "expected_candidate_cycles": 1,
                    "platform_dependency_level": 0,
                },
                "split_conditions": [],
            }

        base = {
            "global_constraints": [{"id": "C-1", "constraint": "Keep stable."}],
            "gate_checks": [],
            "units": [unit("foundation"), unit("window")],
            "cross_unit_contracts": [],
            "topology": {"depends_on": {"foundation": [], "window": ["foundation"]}},
            "integration_contract": {
                "depends_on": ["foundation", "window"],
                "entrypoint": "window_service",
                "completion_condition": "The window consumes the foundation output.",
                "failure_behavior": "Any rejected module output fails integration.",
            },
            "assumption_ledger": {"assumptions": []},
            "risk_ledger": {"risks": []},
        }
        revision = SimpleNamespace(
            aggregate_id="arch-structural-sharing",
            payload={"requirements_ref": requirements.to_dict()},
        )
        base_ref = worker._publish_planning_bundle(revision, base, requirements_ref=requirements)
        changed = deepcopy(base)
        changed["units"][0]["ownership"] = {
            "rule": "foundation-revised exclusively owns its output."
        }
        revised_ref = worker._publish_planning_bundle(
            revision,
            changed,
            requirements_ref=requirements,
            base_manifest_ref=base_ref,
        )
        old_manifest = service.artifacts.read_json(base_ref)
        new_manifest = service.artifacts.read_json(revised_ref)
        self.assertEqual(new_manifest["global_constraints_ref"], old_manifest["global_constraints_ref"])
        self.assertNotEqual(new_manifest["unit_contract_refs"][0], old_manifest["unit_contract_refs"][0])
        self.assertEqual(new_manifest["unit_contract_refs"][1], old_manifest["unit_contract_refs"][1])

    def test_profile_worker_preserves_scheduler_lease_owner_id(self) -> None:
        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        leased_invocation_id = "inv_scheduler_owned"
        captured: dict[str, object] = {}
        self._create_role_scope(
            worker.service,
            workflow_id="wf-lease-owner",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch-lease-owner",
        )

        def capture_invocation(**kwargs) -> None:
            captured["invocation_id"] = str(kwargs["invocation_id"])
            raise RuntimeError("stop-after-invocation-record")

        worker.repository.read_snapshot = lambda *_args: SimpleNamespace(
            payload={
                "family_binding_ref": {"sha256": "binding"},
                "control_route": {
                    "endpoint_id": "socket",
                    "channel_kind": "socket",
                    "reply_target": {"connection_id": "client-1"},
                    "control_scope_key": "socket:client-1",
                },
            }
        )
        worker.repository.record_role_invocation = capture_invocation
        snapshot = SimpleNamespace(
            workflow_id="wf-lease-owner",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch-lease-owner",
            payload={"research_mode": "local_only"},
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

        with (
            patch("pal.minion.v2.semantic_orchestration.orchestrator.workflow_request_from_snapshot", return_value={"workspace": {"kind": "new_project"}}),
            patch.object(
                worker.service.artifacts,
                "read_json",
                return_value={
                    "schema_version": "3",
                    "policies": {},
                    "role_bindings": {
                        "architect": {
                            "selector": "software_engineering.v2_architect",
                            "executor_profile": {
                                "profile_id": "v2_architect",
                                "profile_group": "software_engineering",
                                "canonical_profile_id": "software_engineering.v2_architect",
                            },
                        }
                    },
                },
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
                        fencing_token=7,
                        profile="software_engineering.v2_architect",
                        activation=RoleActivation(
                            OrchestrationRole.ARCHITECT,
                            RoleMode.AUTHOR,
                        ),
                        instruction="produce architecture",
                        reference_refs={},
                        prepare_workspace=False,
                    )
                )

        self.assertEqual(captured["invocation_id"], leased_invocation_id)
        self.assertEqual(
            captured["control_route"],
            {
                "endpoint_id": "socket",
                "channel_kind": "socket",
                "reply_target": {"connection_id": "client-1"},
                "control_scope_key": "socket:client-1",
            },
        )
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
                    "intro_minion_workflow_status",
                    "op_minion_resume_workflow",
                    "op_minion_restart_execution",
                    "op_minion_resolve_triage",
                    "op_minion_submit_human_decision",
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
            start_schema = start_descriptor.InputModel.model_json_schema(mode="validation")
            self.assertIn("task_spec", start_schema["properties"])
            self.assertNotIn("source_files", start_schema["properties"])
            self.assertIn("search active Pal skills", start_descriptor.description)
            self.assertIn("ask the user whether to inject them", start_descriptor.description)
            self.assertIn("call skill_inject for every approved skill", start_descriptor.description)
            self.assertIn("Never infer consent", start_descriptor.description)
            self.assertIn("Never inspect or implement the target in the foreground first", start_descriptor.description)
            decision_schema = next(
                descriptor.InputModel.model_json_schema(mode="validation")
                for descriptor in core.context.capability_registry.descriptors.values()
                if descriptor.canonical_path == "op_minion_submit_human_decision"
            )
            self.assertEqual(
                decision_schema["properties"]["decision"]["enum"],
                ["accept", "edit", "reject"],
            )
            self.assertNotIn("clarification_response", decision_schema["properties"])
            self.assertNotIn("op_minion_dispatch_workflow", canonical)
            self.assertNotIn("op_minion_tick_parent_dag", canonical)
            self.assertNotIn("op_minion_recover_work_order", canonical)
        finally:
            with contextlib.suppress(Exception):
                core.detach_module("minion")

    def test_public_provider_binds_current_workflow_without_exposing_manager_identity(self) -> None:
        wakes: list[str] = []
        provider = MinionV2PublicProvider(
            runtime_root=self.runtime_root,
            wake_manager=lambda: wakes.append("wake"),
        )
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
        encoded = json.dumps(started.structured, sort_keys=True)
        for forbidden in ("workflow_id", "task_id", "artifact_ref", "sha256"):
            self.assertNotIn(forbidden, encoded)

        restarted = MinionV2PublicProvider(runtime_root=self.runtime_root, wake_manager=lambda: None)
        status = restarted.workflow_status(
            CapabilityCall(name="intro_minion_workflow_status", meta=meta, args={})
        )
        self.assertEqual(status.structured["task"], "Tiny semantic router")
        self.assertEqual(status.structured["phase"], "created")
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
            name="router review notes", actor="nathan", source_channel="socket:test"
        )
        self.assertTrue(resolved["sha256"])

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

    def test_task_ledger_uses_fts_and_status_can_rebind_across_channels(self) -> None:
        provider = MinionV2PublicProvider(runtime_root=self.runtime_root, wake_manager=lambda: None)
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
        self.assertFalse(search.structured["tasks"][0]["workflows"][0]["bound_to_current_channel"])
        encoded = json.dumps(search.structured, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("task_id", encoded)
        self.assertNotIn("workflow_id", encoded)

        status = provider.workflow_status(
            CapabilityCall(
                name="intro_minion_workflow_status",
                meta=new_meta,
                args={"task": "鸿蒙字体渲染验证"},
            )
        )
        self.assertEqual(status.status, RuntimeStatus.OK)
        self.assertEqual(status.structured["task"], "鸿蒙字体渲染验证")
        rebound = provider.service.repository.read_channel_workflow(
            actor_id="nathan", channel_id="telegram:main"
        )
        self.assertTrue(rebound)

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
        provider.service.resolve_workflow_selector = lambda **_kwargs: "wf_internal"
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
        self.assertEqual(submitted[0]["source_channel"], "socket:test")
        self.assertNotIn("decision_token", submitted[0])
        self.assertNotIn("workflow_id", result.structured)

    def test_public_triage_resolution_uses_semantic_subject_and_wakes_manager(self) -> None:
        wakes: list[str] = []
        submitted: list[dict[str, object]] = []
        provider = MinionV2PublicProvider(
            runtime_root=self.runtime_root,
            wake_manager=lambda: wakes.append("wake"),
        )
        provider.service.resolve_workflow_selector = lambda **_kwargs: "wf_internal"
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

    def test_public_execution_restart_uses_task_selector_and_wakes_manager(self) -> None:
        wakes: list[str] = []
        submitted: list[dict[str, object]] = []
        selectors: list[dict[str, object]] = []
        provider = MinionV2PublicProvider(
            runtime_root=self.runtime_root,
            wake_manager=lambda: wakes.append("wake"),
        )
        provider.service.resolve_workflow_selector = lambda **kwargs: (
            selectors.append(dict(kwargs)) or "wf_internal"
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
        self.assertEqual(submitted[0]["source_channel"], "socket:test")
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
            artifact_type="ArchitectureSkeletonArtifact",
        ).to_dict()
        service.start_workflow(
            {
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
            active_channel_id="socket:test",
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
            control_route={"channel": "socket:new"},
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
        self.assertEqual(
            repository.read_channel_workflow(
                actor_id="nathan",
                channel_id="socket:new",
            ),
            replacement_id,
        )
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
            artifact_type="ArchitectureSkeletonArtifact",
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
        provider.service.resolve_workflow_selector = lambda **_kwargs: "wf_internal"

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

        provider.service.workflow_status = status
        result = provider.workflow_status(
            CapabilityCall(
                name="intro_minion_workflow_status",
                meta={"actor_id": "nathan", "channel_id": "socket:new"},
                args={"task": "Router", "view": "human_review"},
            )
        )

        self.assertEqual(requested, [("wf_internal", "human_review")])
        self.assertEqual(result.structured["human_review"]["markdown"], "# Review me")
        encoded = json.dumps(result.structured, sort_keys=True)
        for forbidden in ("workflow_id", "decision_token", "actor_id", "active_channel_id", "manifest_sha", "route"):
            self.assertNotIn(forbidden, encoded)

    def test_public_status_uses_manager_live_projection(self) -> None:
        requested: list[tuple[str, dict[str, object]]] = []

        def manager_request(method: str, params: dict[str, object] | None) -> dict[str, object]:
            requested.append((method, dict(params or {})))
            return {
                "status": "ok",
                "workflow_id": "wf-live-question",
                "waiting_for_user": True,
                "liveness": "human_wait",
            }

        provider = MinionV2PublicProvider(
            runtime_root=self.runtime_root,
            wake_manager=lambda: None,
            manager_request=manager_request,
        )
        provider.service.resolve_workflow_selector = lambda **_kwargs: "wf-live-question"

        result = provider.workflow_status(
            CapabilityCall(
                name="intro_minion_workflow_status",
                meta={"actor_id": "nathan", "channel_id": "socket:new"},
                args={"task": "Framepipe", "view": "status"},
            )
        )

        self.assertEqual(
            requested,
            [
                (
                    "v2_workflow_status",
                    {"workflow_id": "wf-live-question", "view": "status"},
                )
            ],
        )
        self.assertTrue(result.structured["waiting_for_user"])
        self.assertEqual(result.structured["liveness"], "human_wait")

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
            subject="architecture",
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
            artifact_type="ArchitectureSkeletonArtifact",
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
            subject="architecture",
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

        result = service.resume_workflow(
            workflow_id="wf_orphaned_node",
            actor="nathan",
            source_channel="socket:test",
        )

        node = service.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
        self.assertEqual(result["status"], "triage_requires_resolution")
        self.assertEqual(result["triage"][0]["subject"], "drawing")
        self.assertEqual(node.state, "TRIAGE_REQUIRED")

        resolved = service.resolve_triage(
            workflow_id="wf_orphaned_node",
            actor="nathan",
            source_channel="socket:test",
            subject="drawing",
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
            artifact_type="ArchitectureSkeletonArtifact",
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
            artifact_type="ArchitectureSkeletonArtifact",
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
            active_channel_id="socket:test",
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

    def test_manager_workflow_status_projects_pending_architect_question(self) -> None:
        manager = MinionManager(self.runtime_root)
        manager.v2_service.workflow_status = lambda workflow_id, *, view="status": {
            "status": "ok",
            "workflow_id": workflow_id,
            "active_worker": "inv-architect-question",
            "active_worker_role": "architect",
            "next_legal_action": ["control_workflow:pause", "control_workflow:cancel"],
            "waiting_for_user": False,
            "liveness": "live_lease",
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
                "v2_workflow_status",
                {"workflow_id": "wf-architect-question"},
            )
        )

        self.assertTrue(status["waiting_for_user"])
        self.assertEqual(status["liveness"], "human_wait")
        self.assertEqual(status["active_worker"], "")
        self.assertEqual(status["active_worker_role"], "")
        self.assertEqual(
            status["next_legal_action"],
            ["answer_question", "control_workflow:cancel"],
        )
        self.assertEqual(status["pending_question_count"], 1)
        self.assertEqual(
            status["pending_question"],
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
        self.assertNotIn("clarification_id", json.dumps(status["pending_question"]))

    def test_manager_binds_worker_question_to_workflow_control_route(self) -> None:
        manager = MinionManager(self.runtime_root)
        route = {
            "endpoint_id": "socket",
            "channel_kind": "socket",
            "reply_target": {"connection_id": "client-1"},
            "control_scope_key": "socket:client-1",
        }
        state = MinionRunState(
            minion_id="inv-routed-question",
            run_id="run-routed-question",
            pack=MinionInvocationPack(
                invocation_id="inv-routed-question",
                metadata={
                    "minion_v2": {
                        "workflow_id": "wf-routed-question",
                        "control_route": route,
                    }
                },
            ),
        )
        manager.runs[state.run_id] = state
        recorded: list[dict[str, object]] = []
        queued: list[dict[str, object]] = []
        manager.v2_service.repository.read_snapshot = lambda *_args: None
        manager.v2_service.repository.record_worker_event = (
            lambda event: recorded.append(dict(event))
        )
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
        self.assertEqual(recorded, queued)
        self.assertEqual(queued[0]["workflow_id"], "wf-routed-question")
        self.assertEqual(dict(queued[0]["payload"])["control_route"], route)
        self.assertEqual(state.status, "clarification_pending")
        self.assertEqual(
            state.pending_clarification["clarification_id"],
            "clarification-routed",
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
