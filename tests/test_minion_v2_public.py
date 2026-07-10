from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
import unittest
from pathlib import Path

from pal.core.runtime import PalCore
from pal.minion import register_with_core as register_minion_with_core
from pal.minion.service import TaskingService
from pal.minion.v2.orchestration import MinionV2OutboxProcessor
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2 import ActionEnvelope, AggregateType
from pal.minion.manager import MinionManager
from pal.minion.prompt import TaskingPromptFragmentProvider
from pal.minion.runner import MinionRunner
from pal.shared import PromptAssemblyContext
from pal.shared import TaskContextPack


class _NoopSemanticEffects:
    async def execute_semantic_effect(self, effect):
        _ = effect
        return {}


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


class _FakeRuntimeBundle:
    async def close(self) -> None:
        return None


class _SingleInvocationRunner(MinionRunner):
    async def _run_agent_loop(self, bundle, *, forced_retry_note: str = "") -> str:
        _ = bundle, forced_retry_note
        return "done"


class MinionV2PublicSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal_minion_v2_public_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.runtime_root, ignore_errors=True)

    def test_minion_plugin_exposes_only_the_seven_v2_business_capabilities(self) -> None:
        core = PalCore()
        register_minion_with_core(core.context, TaskingService(), runtime_root=self.runtime_root)
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
                    "intro_minion_workflow_status",
                    "op_minion_resume_workflow",
                    "op_minion_submit_human_decision",
                    "op_minion_control_workflow",
                    "op_minion_archive_workflow",
                },
            )
            self.assertNotIn("op_minion_dispatch_workflow", canonical)
            self.assertNotIn("op_minion_tick_parent_dag", canonical)
            self.assertNotIn("op_minion_recover_work_order", canonical)
        finally:
            with contextlib.suppress(Exception):
                core.detach_module("minion")

    def test_new_requirement_routes_to_architecture_revision_without_cursor_state(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        started = service.start_workflow(
            {
                "workflow_id": "wf_route",
                "operation": "new_requirement",
                "goal": "Implement a bounded feature",
                "requirements": ["Preserve the public contract"],
                "workspace": {"kind": "new_project", "project_name": "demo"},
                "actor": "nathan",
                "source_channel": "socket:test",
            }
        )
        processor = MinionV2OutboxProcessor(service, semantic_effects=_NoopSemanticEffects())
        asyncio.run(processor.process_once(limit=10))
        asyncio.run(processor.process_once(limit=10))
        status = service.workflow_status(started["workflow_id"])
        self.assertEqual(status["current_phase"], "requirements")
        self.assertEqual(status["active_node_state"], "REQUIREMENTS_QUEUED")
        snapshots = service.repository.list_workflow_snapshots("wf_route")
        self.assertFalse(any("milestone" in key or "cursor" in key for item in snapshots for key in item.payload))

    def test_standalone_review_artifact_routes_without_architecture_or_coder(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        artifact = service.submit_artifact(
            {
                "artifact_type": "CodeSnapshotArtifact",
                "content": {"repository": "demo", "sha": "abc"},
            }
        )["artifact_ref"]
        service.start_workflow(
            {
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

    def test_effect_replay_after_side_effect_before_ack_is_idempotent(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        service.start_workflow(
            {
                "workflow_id": "wf_effect_replay",
                "operation": "new_requirement",
                "goal": "Exercise effect replay",
                "workspace": {"kind": "new_project", "project_name": "demo"},
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

    def test_pause_settles_only_after_child_pause_confirmation(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        service.start_workflow(
            {
                "workflow_id": "wf_pause",
                "operation": "new_requirement",
                "goal": "Pauseable workflow",
                "workspace": {"kind": "new_project", "project_name": "demo"},
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

    def test_manager_rejects_archived_v1_write_rpc(self) -> None:
        manager = MinionManager(self.runtime_root)
        for method, params in (
            ("spawn", {"task_context_pack": {}}),
            ("send_decision", {"decision": {}}),
            ("send_clarification", {"clarification": {}}),
        ):
            with self.subTest(method=method):
                with self.assertRaisesRegex(ValueError, f"V1 write RPC '{method}' is archived"):
                    asyncio.run(manager._call_method(method, params))
        self.assertEqual(asyncio.run(manager._call_method("v2_wake", {}))["status"], "woken")

    def test_control_effect_runs_while_semantic_worker_effect_is_inflight(self) -> None:
        async def scenario() -> None:
            service = MinionV2WorkflowService(self.runtime_root)
            service.start_workflow(
                {
                    "workflow_id": "wf_concurrent_control",
                    "operation": "new_requirement",
                    "goal": "Long running architecture",
                    "workspace": {"kind": "new_project", "project_name": "demo"},
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

    def test_prompt_exposes_v2_workflow_semantics_only(self) -> None:
        content = TaskingPromptFragmentProvider().build_prompt_fragments(
            PromptAssemblyContext(turn_kind="channel")
        )[0].content
        self.assertIn("op_minion_start_workflow", content)
        self.assertIn("intro_minion_workflow_status", content)
        self.assertIn("op_minion_submit_human_decision", content)
        self.assertNotIn("minion_dispatch_workflow", content)
        self.assertNotIn("minion_tick_parent_dag", content)
        self.assertNotIn("milestone cursor", content)

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
                pack=TaskContextPack(
                    work_order_id="v2_invocation",
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
