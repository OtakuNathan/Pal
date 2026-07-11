from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pal.core.runtime import PalCore
from pal.minion import register_with_core as register_minion_with_core
from pal.minion.capabilities import MinionManagerProvider, inspect_minion
from pal.minion.ipc import minion_port_path, minion_socket_path
from pal.minion.v2.capabilities import MinionV2PublicProvider
from pal.minion.v2.orchestration import MinionV2OutboxProcessor
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.workers import MinionV2SemanticWorker
from pal.minion.v2 import ActionEnvelope, AggregateType
from pal.minion.manager import MinionManager
from pal.minion.runner import MinionRunner
from pal.shared import MinionInvocationPack


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

    def test_architecture_stage_resolves_snapshot_before_profile(self) -> None:
        worker = MinionV2SemanticWorker(MinionV2WorkflowService(self.runtime_root))
        worker._effect_snapshot = lambda _effect: SimpleNamespace(workflow_id="wf-order")

        def stop_after_profile(workflow_id: str, role: str) -> str:
            self.assertEqual((workflow_id, role), ("wf-order", "requirements"))
            raise RuntimeError("profile-resolved-after-snapshot")

        worker._profile_for_role = stop_after_profile
        with self.assertRaisesRegex(RuntimeError, "profile-resolved-after-snapshot"):
            asyncio.run(worker._run_architecture_stage({"payload": {"stage": "requirements"}}))

    def test_resume_does_not_reclaim_a_live_architecture_stage(self) -> None:
        worker = MinionV2SemanticWorker(MinionV2WorkflowService(self.runtime_root))
        worker._effect_snapshot = lambda _effect: SimpleNamespace(
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch-live",
            state="PLANNING_RUNNING",
        )
        worker.repository.read_lease = lambda _key: {
            "owner_id": "inv-live",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
        }

        result = asyncio.run(worker._resume_aggregate({"payload": {}}))

        self.assertEqual(result, {"status": "already_running", "active_worker_id": "inv-live"})

    def test_requirements_stage_uses_artifact_only_workspace(self) -> None:
        worker = MinionV2SemanticWorker(MinionV2WorkflowService(self.runtime_root))
        revision = SimpleNamespace(
            workflow_id="wf-requirements-scope",
            aggregate_id="arch-requirements-scope",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            state="REQUIREMENTS_RUNNING",
            version=1,
            payload={},
        )
        worker._effect_snapshot = lambda _effect: revision
        worker._profile_for_role = lambda *_args: "software_engineering.v2_requirements_analyst"
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
            self.assertEqual(kwargs["workspace_override"], {"kind": "artifact_only"})
            self.assertFalse(kwargs["prepare_workspace"])
            raise RuntimeError("requirements-scope-captured")

        worker._run_profile = capture_scope
        with self.assertRaisesRegex(RuntimeError, "requirements-scope-captured"):
            asyncio.run(
                worker._run_architecture_stage(
                    {
                        "effect_id": "eff-requirements-scope",
                        "effect_key": "event:0",
                        "payload": {"stage": "requirements"},
                    }
                )
            )
        self.assertEqual(dispatched, ["REBIND_REQUIREMENTS"])

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
        worker._profile_for_role = lambda *_args: "software_engineering.v2_researcher"
        worker.repository.read_snapshot = lambda *_args: workflow
        worker.repository.claim_lease = lambda *_args, **_kwargs: self.fail("paused effect claimed a lease")

        result = asyncio.run(
            worker._run_architecture_stage(
                {
                    "effect_id": "eff-paused-stage",
                    "effect_key": "event:0",
                    "payload": {"stage": "research"},
                }
            )
        )

        self.assertEqual(result, {"status": "superseded"})

    def test_nonblocking_requirements_assumptions_do_not_request_human_clarification(self) -> None:
        worker = MinionV2SemanticWorker(MinionV2WorkflowService(self.runtime_root))
        revision = SimpleNamespace(
            workflow_id="wf-nonblocking",
            aggregate_id="arch-nonblocking",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            version=2,
        )
        dispatched: list[str] = []
        worker.repository.read_snapshot = lambda *_args: SimpleNamespace(version=2)
        worker.repository.dispatch = lambda action: dispatched.append(action.action_type) or SimpleNamespace(snapshot=revision)

        result = worker._accept_architecture_stage_output(
            "requirements",
            revision,
            {
                "requirements": [
                    {
                        "requirement_id": "R-1",
                        "statement": "Implement the declared contract.",
                        "strength": "hard",
                        "source_refs": ["request"],
                        "acceptance_semantics": "Every declaration has a real implementation.",
                        "ambiguities": [],
                    }
                ],
                "open_clarifications": [
                    {
                        "topic": "environment",
                        "clarification": "SDK presence is not known yet.",
                        "blocking": False,
                        "bounded_assumption": "Use the declared local fallback when absent.",
                    }
                ],
                "source_coverage": [],
            },
        )

        self.assertEqual(result.artifact_type, "RequirementsArtifact")
        self.assertEqual(dispatched, ["REQUIREMENTS_COMPLETED"])

    def test_research_revision_handoff_includes_base_evidence_catalog(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        worker = MinionV2SemanticWorker(service)
        request_ref = service.artifacts.put_json({"goal": "research"}, artifact_type="WorkflowRequestArtifact")
        requirements_ref = service.artifacts.put_json({"requirements": []}, artifact_type="RequirementsArtifact")
        evidence_ref = service.artifacts.put_json({"evidence": []}, artifact_type="EvidenceCatalogArtifact")
        finding_ref = service.artifacts.put_json(
            {"finding_kind": "evidence_gap", "summary": "link requirements"},
            artifact_type="ArchitectureFindingArtifact",
        )
        workflow = SimpleNamespace(payload={"request_ref": request_ref.to_dict()})
        revision = SimpleNamespace(
            workflow_id="wf-research-revision",
            payload={
                "request_ref": request_ref.to_dict(),
                "requirements_ref": requirements_ref.to_dict(),
                "evidence_catalog_ref": evidence_ref.to_dict(),
                "finding_artifact_ref": finding_ref.to_dict(),
                "research_mode": "local_only",
            },
        )
        worker.repository.read_snapshot = lambda *_args: workflow

        with patch("pal.minion.v2.workers.workflow_request_from_snapshot", return_value={"references": []}):
            instruction, refs = worker._architecture_stage_prompt("research", revision)

        self.assertEqual(refs["base_evidence_catalog"].sha256, evidence_ref.sha256)
        self.assertEqual(refs["revision_finding"].sha256, finding_ref.sha256)
        self.assertIn("retain every unaffected base evidence entry", instruction)

    def test_profile_worker_preserves_scheduler_lease_owner_id(self) -> None:
        worker = MinionV2SemanticWorker(MinionV2WorkflowService(self.runtime_root))
        leased_invocation_id = "inv_scheduler_owned"
        captured: dict[str, str] = {}

        def capture_invocation(**kwargs) -> None:
            captured["invocation_id"] = str(kwargs["invocation_id"])
            raise RuntimeError("stop-after-invocation-record")

        worker.repository.read_snapshot = lambda *_args: SimpleNamespace(payload={})
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
            patch("pal.minion.v2.workers.MinionProfileRegistry.resolve_pack", lambda _registry, pack: pack),
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
                        lease_resource="architecture:arch-lease-owner:requirements",
                        fencing_token=7,
                        profile="software_engineering.v2_requirements_analyst",
                        role_override="requirements",
                        instruction="normalize requirements",
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
            self.assertTrue(inspect_minion(provider).manager_running)
            self.assertTrue(minion_socket_path(self.runtime_root).exists() or minion_port_path(self.runtime_root).exists())
        finally:
            provider.detach_manager()

        self.assertFalse(minion_socket_path(self.runtime_root).exists())
        self.assertFalse(minion_port_path(self.runtime_root).exists())
        self.assertTrue(inspect_minion(provider).degraded)

    def test_manager_rejects_pre_raii_sidecar_health(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "protocol is incompatible"):
            MinionManagerProvider._validate_health(
                {"ok": True, "health_source": "minion_v2_manager", "manager_pid": 42}
            )

    def test_minion_plugin_exposes_task_first_v2_business_capabilities(self) -> None:
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
                    "intro_minion_workflow_status",
                    "op_minion_resume_workflow",
                    "op_minion_submit_human_decision",
                    "op_minion_control_workflow",
                    "op_minion_archive_workflow",
                    "op_minion_task_create",
                    "intro_minion_task_search",
                    "op_minion_task_update",
                    "op_minion_task_archive",
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
        task_id = self._create_task(service, "route")
        started = service.start_workflow(
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
        self.assertEqual(status["current_phase"], "requirements")
        self.assertEqual(status["active_node_state"], "REQUIREMENTS_QUEUED")
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
        service.start_workflow(
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
        service.start_workflow(
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
        self.assertEqual(resumed.state, "REQUIREMENTS_QUEUED")

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
        started = service.start_workflow(
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
        service.start_workflow(
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

    def test_pause_settles_only_after_child_pause_confirmation(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        task_id = self._create_task(service, "pause")
        service.start_workflow(
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

    def test_control_effect_runs_while_semantic_worker_effect_is_inflight(self) -> None:
        async def scenario() -> None:
            service = MinionV2WorkflowService(self.runtime_root)
            task_id = self._create_task(service, "concurrent_control")
            service.start_workflow(
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
