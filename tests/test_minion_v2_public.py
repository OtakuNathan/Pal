from __future__ import annotations

import asyncio
import contextlib
from copy import deepcopy
import json
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
    apply_v2_revision_scope_capability_policy,
    apply_v2_role_capability_policy,
)
from pal.minion.v2 import ActionEnvelope, AggregateType
from pal.minion.v2.contracts import StaleFencingToken
from pal.minion.manager import MinionManager
from pal.minion.prompt_adapter import render_minion_task_prompt
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
        self.assertIn("op_minion_contract_read", initial.allowed_capabilities)

        scoped = apply_v2_revision_scope_capability_policy(initial)
        self.assertIn("op_minion_contract_revision_read", scoped.allowed_capabilities)
        self.assertNotIn("op_minion_contract_read", scoped.allowed_capabilities)

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
            self.assertEqual(kwargs["invocation_id"], architect_session_id(revision.workflow_id))
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
        with patch("pal.minion.v2.workers.workspace_has_live_processes", return_value=False):
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

    def test_architect_revision_handoff_binds_only_semantic_scope(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        worker = MinionV2SemanticWorker(service)
        request_ref = service.artifacts.put_json({"goal": "research"}, artifact_type="WorkflowRequestArtifact")
        requirements_ref = service.artifacts.put_json({"requirements": []}, artifact_type="RequirementsArtifact")
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
        self.assertEqual(refs["revision_finding"].sha256, finding_ref.sha256)
        scope = service.artifacts.read_json(refs["revision_scope"])
        self.assertEqual(scope["write_targets"][0]["id"], "foundation")
        self.assertIn("scoped revision", instruction)
        self.assertIn("op_minion_input_read", instruction)

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
        worker.repository.read_snapshot = lambda *_args: SimpleNamespace(payload={})
        worker._base_contract_builder_payload_from_manifest = lambda _ref: {"seed": "base"}

        def record(**_kwargs) -> None:
            raise RuntimeError("stop-after-seed")

        identity = lambda pack, **_kwargs: pack
        with (
            patch("pal.minion.v2.workers.seed_contract_builder_draft") as seed,
            patch("pal.minion.v2.workers.workflow_request_from_snapshot", return_value={"workspace": {"kind": "new_project"}}),
            patch("pal.minion.v2.workers.MinionProfileRegistry.resolve_pack", lambda _registry, pack: pack),
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
                "provided_interfaces": [],
                "consumed_interfaces": [],
                "ownership": {"owner": unit_id},
                "lifecycle": "N/A: stateless",
                "state_model": "stateless",
                "invariants": ["deterministic"],
                "error_behavior": [],
                "compatibility": [],
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
            "integration_contract": {"depends_on": ["foundation", "window"]},
            "assumption_ledger": {"assumptions": []},
            "risk_ledger": {"risks": []},
        }
        revision = SimpleNamespace(
            aggregate_id="arch-structural-sharing",
            payload={"requirements_ref": requirements.to_dict()},
        )
        base_ref = worker._publish_planning_bundle(revision, base, requirements_ref=requirements)
        changed = deepcopy(base)
        changed["units"][0]["ownership"] = {"owner": "foundation-revised"}
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
                    "intro_minion_workflow_status",
                    "op_minion_resume_workflow",
                    "op_minion_submit_human_decision",
                    "op_minion_control_workflow",
                    "op_minion_archive_workflow",
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
