from __future__ import annotations

import asyncio
import contextlib
from copy import deepcopy
import json
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
from pal.minion.v2.capabilities import MinionV2PublicProvider
from pal.minion.v2.contract_builder import ARCHITECT_BUILDER_CAPABILITIES
from pal.minion.v2.orchestration import MinionV2OutboxProcessor
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.sessions import architect_session_id, coder_session_id
from pal.minion.v2.workers import (
    MinionV2SemanticWorker,
    _architecture_submit_idempotency_key,
    _skeleton_architecture_review_view,
    _skeleton_architect_instruction,
    apply_v2_revision_scope_capability_policy,
    apply_v2_role_capability_policy,
)
from pal.minion.v2.worker_protocol import WorkerAssignmentRequest
from pal.minion.v2.skeleton import ArchitectureWorkspace
from pal.minion.v2 import ActionEnvelope, AggregateType
from pal.minion.v2.contracts import DeferredEffectError, StaleFencingToken, SubmissionInvariantError
from pal.minion.manager import MinionManager, MinionRunState
from pal.minion.prompt_adapter import render_minion_task_prompt
from pal.minion.runner import MinionAgentLoopState, MinionRunner
from pal.shared import IntrospectionCall, LLMFinishReason, MinionInvocationPack, RuntimeStatus


class _NoopSemanticEffects:
    async def execute_semantic_effect(self, effect):
        _ = effect
        return {}


class MinionV2WorkerIdentityTests(unittest.TestCase):
    def test_architecture_submit_dedup_key_distinguishes_state_machine_cycles(self) -> None:
        first = _architecture_submit_idempotency_key("arch-1", 7, "same-submission")
        replay = _architecture_submit_idempotency_key("arch-1", 7, "same-submission")
        next_cycle = _architecture_submit_idempotency_key("arch-1", 12, "same-submission")

        self.assertEqual(first, replay)
        self.assertNotEqual(first, next_cycle)


class _ControlSemanticEffects:
    def __init__(self, service: MinionV2WorkflowService) -> None:
        self.service = service

    async def execute_semantic_effect(self, effect):
        if effect.get("effect_type") == "pause_aggregate_work":
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
        if effect.get("effect_type") == "enqueue_architecture_stage":
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

    def test_background_effect_returns_after_durable_assignment_is_ready(self) -> None:
        async def scenario() -> None:
            worker = MinionV2SemanticWorker(MinionV2WorkflowService(self.runtime_root))
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

    def test_background_worker_supervisor_enforces_global_slot_limit(self) -> None:
        async def scenario() -> None:
            worker = MinionV2SemanticWorker(
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
            worker = MinionV2SemanticWorker(MinionV2WorkflowService(self.runtime_root))
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
            worker = MinionV2SemanticWorker(MinionV2WorkflowService(self.runtime_root))
            worker._assignment_ids_by_effect["effect-key-settled"] = "assignment-settled"
            worker.repository.read_worker_assignment = lambda _assignment_id: {
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

    def test_recorded_submission_replays_business_action_before_triage(self) -> None:
        async def scenario() -> None:
            worker = MinionV2SemanticWorker(MinionV2WorkflowService(self.runtime_root))
            effect = {
                "effect_id": "effect-reconcile",
                "effect_key": "effect-key-reconcile",
            }
            worker._assignment_ids_by_effect["effect-key-reconcile"] = (
                "assignment-reconcile"
            )
            assignment_state = {"value": "result_recorded"}
            calls = 0

            worker.repository.read_worker_assignment = lambda _assignment_id: {
                "assignment_id": "assignment-reconcile",
                "state": assignment_state["value"],
                "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                "aggregate_id": "node-reconcile",
            }
            worker.repository.list_worker_attempts = lambda _assignment_id: [
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

            with patch("pal.minion.v2.workers.asyncio.sleep", new=no_wait):
                result = await worker._background_worker_loop(effect, runner)

            self.assertEqual(calls, 2)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(assignment_state["value"], "settled")

        asyncio.run(scenario())

    def test_recovery_restarts_a_durable_queued_assignment(self) -> None:
        async def scenario() -> None:
            service = MinionV2WorkflowService(self.runtime_root)
            service.repository.ensure_worker_session(
                session_id="session-recovery",
                workflow_id="workflow-recovery",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-recovery",
                role="producer",
            )
            assignment = service.repository.create_worker_assignment(
                WorkerAssignmentRequest(
                    assignment_key="recovery-assignment",
                    session_id="session-recovery",
                    workflow_id="workflow-recovery",
                    aggregate_type=AggregateType.DAG_NODE_RUN.value,
                    aggregate_id="node-recovery",
                    role="producer",
                    input_fingerprint="input-recovery",
                    required_inputs=(),
                    input_refs={},
                    execution_spec={
                        "effect_type": "spawn_producer_worker",
                        "effect_key": "effect-key-recovery",
                        "workflow_id": "workflow-recovery",
                        "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                        "aggregate_id": "node-recovery",
                        "payload": {},
                    },
                    submission_kind="candidate",
                )
            )
            worker = MinionV2SemanticWorker(service)
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

    def test_recovery_cancels_assignment_for_a_paused_aggregate(self) -> None:
        async def scenario() -> None:
            service = MinionV2WorkflowService(self.runtime_root)
            service.repository.ensure_worker_session(
                session_id="session-paused",
                workflow_id="workflow-paused",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-paused",
                role="producer",
            )
            assignment = service.repository.create_worker_assignment(
                WorkerAssignmentRequest(
                    assignment_key="paused-assignment",
                    session_id="session-paused",
                    workflow_id="workflow-paused",
                    aggregate_type=AggregateType.DAG_NODE_RUN.value,
                    aggregate_id="node-paused",
                    role="producer",
                    input_fingerprint="input-paused",
                    required_inputs=(),
                    input_refs={},
                    execution_spec={
                        "effect_type": "spawn_producer_worker",
                        "effect_key": "effect-key-paused",
                        "workflow_id": "workflow-paused",
                        "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                        "aggregate_id": "node-paused",
                        "payload": {},
                    },
                    submission_kind="candidate",
                )
            )
            worker = MinionV2SemanticWorker(service)
            worker.repository.read_snapshot = lambda _aggregate_type, _aggregate_id: (
                SimpleNamespace(state="PAUSED")
            )

            count = await worker.recover_background_assignments()

            self.assertEqual(count, 0)
            self.assertEqual(
                service.repository.read_worker_assignment(assignment["assignment_id"])[
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

        initial = apply_v2_role_capability_policy(pack, role="architect")
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
        processor.repository.complete_worker_session = lambda invocation_id, **_kwargs: completed.append(
            invocation_id
        ) or True
        actions: list[ActionEnvelope] = []
        processor.repository.dispatch = lambda action: actions.append(action)
        processor._link_workflow = lambda *_args: None

        processor._create_revision({"effect_key": "event-edit:0"})

        self.assertEqual(
            completed,
            [architect_session_id(previous.workflow_id, previous.aggregate_id)],
        )
        self.assertEqual([action.action_type for action in actions], ["CREATE_ARCHITECTURE_REVISION"])
        self.assertEqual(actions[0].payload["parent_revision_id"], previous.aggregate_id)

    def test_runner_checkpoint_uses_fenced_files_and_restores_latest_safe_turn(self) -> None:
        run_dir = self.runtime_root / "agent-session"
        run_dir.mkdir(parents=True)
        first_pack = MinionInvocationPack(
            invocation_id="inv-session-checkpoint",
            instruction="initial assignment",
            workspace={"run_dir": str(run_dir)},
            metadata={
                "agent_session": {
                    "session_id": "inv-session-checkpoint",
                    "response_key": "effect-1",
                    "fencing_token": 3,
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

        second_pack = MinionInvocationPack(
            invocation_id="inv-session-checkpoint",
            instruction="repair reviewer finding",
            workspace={"run_dir": str(run_dir)},
            metadata={
                "agent_session": {
                    "session_id": "inv-session-checkpoint",
                    "response_key": "effect-2",
                    "fencing_token": 4,
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
        self.assertTrue((run_dir / "session-continuation-3.json").is_file())

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

    @staticmethod
    def _start_workflow(service: MinionV2WorkflowService, request: dict):
        payload = dict(request)
        if str(payload.get("operation") or "new_requirement") == "new_requirement" and not payload.get("requirements_ref"):
            prepared = service.prepare_requirements(
                {"requirements": [{"requirement_id": "R-1", "statement": str(payload.get("goal") or "bounded task"), "strength": "hard", "source_refs": ["test"]}]}
            )
            payload["requirements_ref"] = prepared["requirements_ref"]
        return service.start_workflow(payload)

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

    def test_architecture_stage_resolves_snapshot_before_profile(self) -> None:
        worker = MinionV2SemanticWorker(MinionV2WorkflowService(self.runtime_root))
        worker._effect_snapshot = lambda _effect: SimpleNamespace(workflow_id="wf-order")

        def stop_after_profile(workflow_id: str, role: str) -> str:
            self.assertEqual((workflow_id, role), ("wf-order", "architect"))
            raise RuntimeError("profile-resolved-after-snapshot")

        worker._profile_for_role = stop_after_profile
        with self.assertRaisesRegex(RuntimeError, "profile-resolved-after-snapshot"):
            asyncio.run(worker._run_architecture_stage({"payload": {"stage": "architect"}}))

    def test_resume_does_not_reclaim_a_live_architecture_stage(self) -> None:
        worker = MinionV2SemanticWorker(MinionV2WorkflowService(self.runtime_root))
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
        worker = MinionV2SemanticWorker(MinionV2WorkflowService(self.runtime_root))
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
        worker = MinionV2SemanticWorker(MinionV2WorkflowService(self.runtime_root))
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
        worker = MinionV2SemanticWorker(MinionV2WorkflowService(self.runtime_root))
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
                    payload={"candidate_cycle": 0},
                ),
                SimpleNamespace(
                    workflow_id="wf-coder-session",
                    aggregate_id="node-coder-session",
                    state="REPAIR_QUEUED",
                    version=7,
                    payload={"candidate_cycle": 1},
                ),
            )
        )
        worker._effect_snapshot = lambda _effect: next(snapshots)

        worker._admit_node_worker(
            {"effect_key": "producer"},
            action_type="START_PRODUCING",
            role="producer",
        )
        worker._admit_node_worker(
            {"effect_key": "repair"},
            action_type="START_REPAIR",
            role="repair",
        )

        expected = coder_session_id("node-coder-session")
        self.assertEqual(claims, [expected, expected])
        self.assertEqual([item.payload["active_worker_id"] for item in actions], [expected, expected])

    def test_snapshot_effect_rebinds_expired_lease_and_reacquires_workspace_lock(self) -> None:
        worker = MinionV2SemanticWorker(MinionV2WorkflowService(self.runtime_root))
        workspace = self.runtime_root / "snapshot-worktree"
        workspace.mkdir()
        node = SimpleNamespace(
            workflow_id="wf-rebind",
            aggregate_id="node-rebind",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            state="SNAPSHOTTING",
            version=8,
            payload={
                "active_worker_id": coder_session_id("node-rebind"),
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
        with patch("pal.minion.v2.workers.workspace_process_holders", return_value=()):
            rebound = asyncio.run(
                worker._ensure_node_effect_lease(
                    node,
                    action_type="REBIND_SNAPSHOTTER",
                    role="producer",
                )
            )

        self.assertEqual(captured[0].action_type, "REBIND_SNAPSHOTTER")
        self.assertEqual(rebound.payload["fencing_token"], 2)
        self.assertTrue(worker._worktree_locks.is_held("node-rebind"))
        worker._worktree_locks.release("node-rebind")

    def test_fresh_effect_lease_is_renewed_before_worker_spawn(self) -> None:
        worker = MinionV2SemanticWorker(MinionV2WorkflowService(self.runtime_root))
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
        worker = MinionV2SemanticWorker(MinionV2WorkflowService(self.runtime_root))
        workspace = self.runtime_root / "review-restart"
        workspace.mkdir()
        node = SimpleNamespace(
            workflow_id="wf-review-restart",
            aggregate_id="node-review-restart",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            state="REVIEWING",
            version=5,
            payload={
                "active_worker_id": "inv-review-restart",
                "lease_resource_key": "node:node-review-restart:review",
                "fencing_token": 6,
                "workspace_path": str(workspace),
            },
        )
        lease = {
            "owner_id": "inv-review-restart",
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

        with patch("pal.minion.v2.workers.terminate_process_group", side_effect=reaped):
            rebound = asyncio.run(
                worker._ensure_node_effect_lease(
                    node,
                    action_type="REBIND_REVIEWER",
                    role="reviewer",
                )
            )

        self.assertEqual(released, [6])
        self.assertEqual(captured[0].action_type, "REBIND_REVIEWER")
        self.assertEqual(rebound.payload["fencing_token"], 7)

    def test_architect_quiesce_releases_managed_lsp_before_holder_check(self) -> None:
        worker = MinionV2SemanticWorker(MinionV2WorkflowService(self.runtime_root))
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
            patch("pal.minion.v2.workers.workspace_process_holders", side_effect=holders),
            patch("pal.minion.v2.workers.workspace_content_fingerprint", return_value="tree"),
        ):
            asyncio.run(worker._quiesce_architect({"effect_key": "quiesce-lsp"}))

        self.assertEqual(released, [workspace])
        self.assertTrue(holder_checks)
        self.assertTrue(all(holder_checks))
        self.assertEqual(actions[0].action_type, "ARCHITECT_QUIESCED")
        worker._worktree_locks.release(revision.aggregate_id)

    def test_paused_architecture_stage_does_not_restart_worker(self) -> None:
        worker = MinionV2SemanticWorker(MinionV2WorkflowService(self.runtime_root))
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
                    "depends_on": [],
                    "consumes": [],
                    "paths": {"implementation_scopes": [{"kind": "file", "path": "src/router.cpp"}]},
                }
            },
            "verification_nodes": {
                "router_probe": {
                    "depends_on": ["router"],
                    "consumes": [{"module": "router", "path": "include/router.h"}],
                    "covers": [{"section": "Routing", "requirement": "Route deterministically."}],
                    "entrypoints": [{"kind": "build_target", "target": "router_probe"}],
                    "environment": {"runtime": "local"},
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
        self.assertEqual(view["verification_nodes"], submission["verification_nodes"])
        self.assertEqual(view["future_semantic_section"], submission["future_semantic_section"])
        self.assertEqual(view["changed_paths"], ["include/router.h", "src/router.cpp"])
        self.assertNotIn("skeleton_commit_sha", view)
        self.assertNotIn("requirements_ref", view)

    def test_skeleton_architect_submission_hands_live_lease_to_quiescer(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        worker = MinionV2SemanticWorker(service)
        requirements_ref = service.artifacts.put_json(
            {
                "requirements": [
                    {
                        "requirement_id": "R-1",
                        "section": "Routing",
                        "statement": "Route requests deterministically.",
                        "strength": "hard",
                    }
                ]
            },
            artifact_type="RequirementsArtifact",
        )
        workspace_snapshot_ref = service.artifacts.put_json(
            {"snapshot_commit_sha": "base"}, artifact_type="WorkspaceSnapshotArtifact"
        )
        prompt_ref = service.artifacts.put_json({}, artifact_type="WorkerPromptPackArtifact")
        terminal_ref = service.artifacts.put_json({}, artifact_type="WorkerResponseArtifact")
        architecture_worktree = self.runtime_root / "architecture-worktree"
        architecture_worktree.mkdir()
        submission_path = self.runtime_root / "architecture_submission.json"
        submission_path.write_text(
            json.dumps({"modules": {"router": {}}, "verification_nodes": {"probe": {}}}),
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

        def dispatch(action: ActionEnvelope):
            actions.append(action)
            return SimpleNamespace(snapshot=running)

        worker.repository.dispatch = dispatch
        worker.repository.record_worker_turn = lambda **_kwargs: None
        released: list[tuple[object, ...]] = []
        worker.repository.release_lease = lambda *args: released.append(args)

        async def run_profile(**_kwargs):
            return (
                {
                    "payload": {
                        "artifacts": [{"path": str(submission_path), "role": "primary"}],
                        "session_turn_index": 1,
                    }
                },
                prompt_ref,
                terminal_ref,
            )

        worker._run_profile = run_profile
        with patch(
            "pal.minion.v2.workers.workflow_request_from_snapshot",
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
        worker = MinionV2SemanticWorker(service)
        requirements_ref = service.artifacts.put_json(
            {
                "requirements": [
                    {
                        "requirement_id": "R-1",
                        "statement": "Route requests deterministically.",
                        "strength": "hard",
                    }
                ]
            },
            artifact_type="RequirementsArtifact",
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
            "pal.minion.v2.workers.workflow_request_from_snapshot",
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
        worker = MinionV2SemanticWorker(service)
        request_ref = service.artifacts.put_json({"goal": "research"}, artifact_type="WorkflowRequestArtifact")
        requirements_ref = service.artifacts.put_json(
            {
                "requirements": [
                    {
                        "requirement_id": "R-1",
                        "section": "Architecture",
                        "statement": "Repair the module boundary.",
                        "strength": "hard",
                    }
                ]
            },
            artifact_type="RequirementsArtifact",
        )
        constraints_ref = service.artifacts.put_json([], artifact_type="GlobalConstraintsArtifact")
        decisions_ref = service.artifacts.put_json([], artifact_type="DesignDecisionsArtifact")
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
                "design_decisions_ref": decisions_ref.to_dict(),
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

        with patch("pal.minion.v2.workers.workflow_request_from_snapshot", return_value={"references": []}):
            instruction, refs = worker._architecture_stage_prompt("architect", revision)

        self.assertIn("revision_scope", refs)
        self.assertNotIn("base_global_constraints", refs)
        self.assertNotIn("revision_finding", refs)
        scope = service.artifacts.read_json(refs["revision_scope"])
        self.assertEqual(scope["context"][0]["target"]["name"], "foundation")
        self.assertNotIn("id", scope["context"][0]["target"])
        self.assertIn("scoped revision", instruction)
        self.assertIn("read revision_scope first", instruction)

    def test_bound_artifact_prompt_hides_storage_path_and_names_reader(self) -> None:
        pack = MinionInvocationPack(
            invocation_id="inv_bound_prompt",
            goal="inspect",
            workspace={
                "reference_paths": [
                    {
                        "name": "revision_finding",
                        "path": "/host-only/artifacts/secret.json",
                        "bound_input": True,
                        "truth_source": True,
                    }
                ]
            },
        )
        prompt = render_minion_task_prompt(pack)
        self.assertIn('op_minion_input_read(name="revision_finding")', prompt)
        self.assertNotIn("/host-only/artifacts/secret.json", prompt)

    def test_architect_revision_seeds_the_contract_builder_draft(self) -> None:
        worker = MinionV2SemanticWorker(MinionV2WorkflowService(self.runtime_root))
        captured: dict[str, object] = {}
        ref = worker.service.artifacts.put_json({"base": True}, artifact_type="ArchitectureContractArtifact")
        snapshot = SimpleNamespace(
            workflow_id="wf-seeded-revision",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch-seeded-revision",
            payload={"research_mode": "local_only", "base_architecture_manifest_ref": ref.to_dict()},
        )
        worker.repository.read_snapshot = lambda *_args: SimpleNamespace(
            payload={"family_binding_ref": {"sha256": "binding"}}
        )
        worker._base_contract_builder_payload_from_manifest = lambda _ref: {"seed": "base"}

        def record(**_kwargs) -> None:
            raise RuntimeError("stop-after-seed")

        identity = lambda pack, **_kwargs: pack
        with (
            patch("pal.minion.v2.workers.seed_contract_builder_draft") as seed,
            patch("pal.minion.v2.workers.workflow_request_from_snapshot", return_value={"workspace": {"kind": "new_project"}}),
            patch.object(
                worker.service.artifacts,
                "read_json",
                return_value={
                    "policies": {},
                    "profile_definitions": {"architect": {"profile_id": "v2_architect"}},
                    "manifest": {"family_id": "software_engineering"},
                },
            ),
            patch("pal.minion.v2.workers.resolve_pinned_minion_pack", lambda pack, **_kwargs: pack),
            patch("pal.minion.v2.workers.apply_v2_role_capability_policy", identity),
            patch("pal.minion.v2.workers.apply_v2_research_capability_policy", identity),
            patch("pal.minion.v2.workers.sanitize_runner_session_pack", identity),
            patch("pal.minion.v2.workers.with_minion_sandbox_metadata", lambda _root, pack, **_kwargs: pack),
            patch.object(worker.repository, "record_worker_invocation", side_effect=record),
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
                        role_override="architect",
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
        worker = MinionV2SemanticWorker(service)
        requirements = service.architecture.publish_requirements(
            {"requirements": [{"requirement_id": "R-1", "statement": "Implement the module.", "strength": "hard"}]}
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
                "requirement_ids": ["R-1"],
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
            "design_decisions": [],
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
        worker = MinionV2SemanticWorker(MinionV2WorkflowService(self.runtime_root))
        leased_invocation_id = "inv_scheduler_owned"
        captured: dict[str, str] = {}

        def capture_invocation(**kwargs) -> None:
            captured["invocation_id"] = str(kwargs["invocation_id"])
            raise RuntimeError("stop-after-invocation-record")

        worker.repository.read_snapshot = lambda *_args: SimpleNamespace(
            payload={"family_binding_ref": {"sha256": "binding"}}
        )
        worker.repository.record_worker_invocation = capture_invocation
        snapshot = SimpleNamespace(
            workflow_id="wf-lease-owner",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch-lease-owner",
            payload={"research_mode": "local_only"},
        )
        identity = lambda pack, **_kwargs: pack
        with (
            patch("pal.minion.v2.workers.workflow_request_from_snapshot", return_value={"workspace": {"kind": "new_project"}}),
            patch.object(
                worker.service.artifacts,
                "read_json",
                return_value={
                    "policies": {},
                    "profile_definitions": {"architect": {"profile_id": "v2_architect"}},
                    "manifest": {"family_id": "software_engineering"},
                },
            ),
            patch("pal.minion.v2.workers.resolve_pinned_minion_pack", lambda pack, **_kwargs: pack),
            patch("pal.minion.v2.workers.apply_v2_role_capability_policy", identity),
            patch("pal.minion.v2.workers.apply_v2_research_capability_policy", identity),
            patch("pal.minion.v2.workers.sanitize_runner_session_pack", identity),
            patch("pal.minion.v2.workers.with_minion_sandbox_metadata", lambda _root, pack, **_kwargs: pack),
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
                        role_override="architect",
                        instruction="produce architecture",
                        reference_refs={},
                        prepare_workspace=False,
                    )
                )

        self.assertEqual(captured["invocation_id"], leased_invocation_id)

    def _create_task(self, service: MinionV2WorkflowService, suffix: str) -> str:
        task_id = f"task_{suffix}"
        service.create_task(
            {
                "task_id": task_id,
                "title": suffix,
                "objective": f"Exercise {suffix}",
                "family_id": "software_engineering",
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
                    "op_minion_submit_human_decision",
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
                    descriptor.canonical_path: descriptor.parameters_schema
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
                    "family_id": "software_engineering",
                    "goal": "Implement deterministic rule routing.",
                    "workspace": {"kind": "new_project", "project_name": "tiny-router"},
                    "sections": {
                        "Routing": ["Route matching must be deterministic."],
                    },
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
        with self.assertRaisesRegex(ValueError, "sections must be an object"):
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
                    "family_id": "software_engineering",
                    "goal": "验证 OpenHarmony 原生字体生命周期和渲染流程。",
                    "workspace": {"kind": "new_project", "project_name": "ohos-font-probe"},
                    "sections": {"字体渲染": ["原生字体必须由包装对象独占。"]},
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

    def test_new_requirement_routes_to_architecture_revision_without_cursor_state(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        task_id = self._create_task(service, "route")
        started = self._start_workflow(service,
            {
                "task_id": task_id,
                "workflow_id": "wf_route",
                "operation": "new_requirement",
                "goal": "Implement a bounded feature",
                "requirements": ["Preserve the public contract"],
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

    def test_resume_workflow_resolves_recoverable_child_triage(self) -> None:
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

        result = service.resume_workflow(
            workflow_id="wf_triage_resume",
            actor="nathan",
            source_channel="socket:test",
        )

        resumed = service.repository.read_snapshot(AggregateType.ARCHITECTURE_REVISION, revision.aggregate_id)
        self.assertEqual(result["status"], "triage_resolved")
        self.assertEqual(resumed.state, "ARCHITECT_QUEUED")

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
                "family_id": "software_engineering",
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

    def test_manager_has_no_v1_spawn_rpc(self) -> None:
        manager = MinionManager(self.runtime_root)
        with self.assertRaisesRegex(ValueError, "unknown Minion V2 manager method: spawn"):
            asyncio.run(manager._call_method("spawn", {"task_context_pack": {}}))
        self.assertEqual(asyncio.run(manager._call_method("v2_wake", {}))["status"], "woken")

    def test_worker_token_cannot_borrow_another_broker_run(self) -> None:
        manager = MinionManager(self.runtime_root)
        manager.worker_gateway.authorize = lambda _token: {
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

            manager.v2_semantic_worker.send_worker_control = send_control
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
