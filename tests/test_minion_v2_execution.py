from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pal.minion.v2 import ActionEnvelope, AggregateType, ContentAddressedArtifactStore, MinionV2Repository
from pal.minion.v2.architecture import ArchitectureArtifactService, ResearchMode
from pal.minion.v2.contracts import AggregateVersionConflict, StaleFencingToken
from pal.minion.v2.execution import (
    CandidateSnapshotService,
    DagScheduler,
    ExecutionCompiler,
    ModuleRunJournal,
    ModuleWorkViewBuilder,
    NodeQuiescer,
    WorktreeLockRegistry,
    provision_verification_worktree,
    terminate_process_group,
)


class _StoppedProcessController:
    def __init__(self) -> None:
        self.revoked: list[tuple[str, int]] = []

    def revoke_tool_token(self, worker_id: str, fencing_token: int) -> None:
        self.revoked.append((worker_id, fencing_token))

    def request_cooperative_stop(self, worker_id: str) -> None:
        _ = worker_id

    def kill_and_reap_process_group(self, worker_id: str, timeout_seconds: float) -> bool:
        _ = worker_id, timeout_seconds
        return True

    def has_live_processes_for_worktree(self, worktree: Path) -> bool:
        _ = worktree
        return False


def _budget() -> dict[str, int]:
    return {
        "target_file_count": 2,
        "estimated_context_tokens": 8000,
        "public_interface_count": 2,
        "cross_module_contract_count": 1,
        "stateful_resource_count": 0,
        "expected_candidate_cycles": 2,
        "platform_dependency_level": 1,
    }


def _contract(module_id: str, requirement_id: str, evidence_id: str, owned_area: str) -> dict:
    return {
        "module_id": module_id,
        "module_behavior_kind": "stateless",
        "responsibility": f"Implement {module_id}.",
        "owned_area": [owned_area],
        "reference_only_paths": ["references/**"],
        "provided_interfaces": [],
        "consumed_interfaces": [],
        "ownership": {},
        "lifecycle": "N/A",
        "state_model": "stateless",
        "invariants": [f"{module_id} remains deterministic"],
        "error_behavior": [],
        "compatibility": [],
        "dependency_constraints": [],
        "requirement_ids": [requirement_id],
        "evidence_ids": [evidence_id],
        "verification_obligations": [{"kind": "consumer_probe"}],
        "complexity_budget": _budget(),
        "split_conditions": [],
    }


class MinionV2ExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal_minion_v2_exec_"))
        self.repository = MinionV2Repository(self.runtime_root)
        self.store = ContentAddressedArtifactStore(self.runtime_root, self.repository)
        self.architecture = ArchitectureArtifactService(self.store, self.repository)

    def tearDown(self) -> None:
        shutil.rmtree(self.runtime_root, ignore_errors=True)

    def test_persisted_process_group_is_killed_after_leader_exits(self) -> None:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import subprocess; subprocess.Popen(['sleep', '60'])",
            ],
            start_new_session=True,
        )
        process.wait(timeout=5)
        process_group = process.pid
        try:
            os.killpg(process_group, 0)
            self.assertTrue(asyncio.run(terminate_process_group(process_group, timeout_seconds=1.0)))
            with self.assertRaises(ProcessLookupError):
                os.killpg(process_group, 0)
        finally:
            try:
                os.killpg(process_group, 9)
            except ProcessLookupError:
                pass

    def _manifest(self):
        requirements = self.architecture.publish_requirements(
            {
                "requirements": [
                    {"requirement_id": "R-A", "statement": "Implement A", "strength": "hard"},
                    {"requirement_id": "R-B", "statement": "Implement B", "strength": "hard"},
                ]
            }
        )
        evidence = self.architecture.publish_evidence_catalog(
            {
                "evidence": [
                    {"evidence_id": "E-A", "location": "ref.patch:1", "summary": "A reference", "supports_requirement_ids": ["R-A"], "content_sha256": "a" * 64},
                    {"evidence_id": "E-B", "location": "ref.patch:2", "summary": "B reference", "supports_requirement_ids": ["R-B"], "content_sha256": "b" * 64},
                ]
            },
            requirements_ref=requirements,
            research_mode=ResearchMode.LOCAL_ONLY,
        )
        module_a = self.architecture.publish_module_contract(_contract("a", "R-A", "E-A", "src/a/**"))
        module_b = self.architecture.publish_module_contract(_contract("b", "R-B", "E-B", "src/b/**"))
        constraints = self.architecture.publish_fragment([], artifact_type="GlobalConstraintsArtifact")
        decisions = self.architecture.publish_fragment([], artifact_type="DesignDecisionsArtifact")
        cross = self.architecture.publish_fragment(
            {"contract_id": "X", "provider": "a", "consumer": "b"},
            artifact_type="CrossModuleContractArtifact",
        )
        topology = self.architecture.publish_fragment(
            {"depends_on": {"a": [], "b": ["a"]}},
            artifact_type="TopologyArtifact",
        )
        integration = self.architecture.publish_fragment(
            {"node_kind": "integration", "depends_on": ["b"]},
            artifact_type="IntegrationContractArtifact",
        )
        assumptions = self.architecture.publish_fragment({"assumptions": []}, artifact_type="AssumptionLedgerArtifact")
        risks = self.architecture.publish_fragment({"risks": []}, artifact_type="RiskLedgerArtifact")
        return self.architecture.publish_manifest(
            {
                "requirements_ref": requirements.to_dict(),
                "evidence_catalog_ref": evidence.to_dict(),
                "global_constraints_ref": constraints.to_dict(),
                "design_decisions_ref": decisions.to_dict(),
                "module_contract_refs": [module_a.to_dict(), module_b.to_dict()],
                "cross_module_contract_refs": [cross.to_dict()],
                "topology_ref": topology.to_dict(),
                "integration_contract_ref": integration.to_dict(),
                "assumption_ledger_ref": assumptions.to_dict(),
                "risk_ledger_ref": risks.to_dict(),
            }
        )

    def test_epoch_compilation_and_dependency_readiness(self) -> None:
        manifest = self._manifest()
        compilation = ExecutionCompiler(self.repository, self.architecture).compile_epoch(
            workflow_id="wf_exec",
            epoch_id="epoch_1",
            manifest_ref=manifest,
        )
        scheduler = DagScheduler(self.repository)
        self.assertEqual(scheduler.schedule_ready_nodes(workflow_id="wf_exec", epoch_id="epoch_1", max_new_nodes=3), (compilation.module_node_ids["a"],))
        self._accept_node(compilation.module_node_ids["a"])
        self.assertEqual(scheduler.schedule_ready_nodes(workflow_id="wf_exec", epoch_id="epoch_1", max_new_nodes=3), (compilation.module_node_ids["b"],))
        self._accept_node(compilation.module_node_ids["b"])
        self.assertEqual(scheduler.schedule_ready_nodes(workflow_id="wf_exec", epoch_id="epoch_1", max_new_nodes=3), (compilation.integration_node_id,))

    def test_scheduler_queues_independent_nodes_in_the_same_tick(self) -> None:
        manifest = self._manifest()
        payload = self.store.read_json(manifest)
        topology = self.architecture.publish_fragment(
            {"depends_on": {"a": [], "b": []}},
            artifact_type="TopologyArtifact",
        )
        independent_manifest = self.architecture.publish_manifest(
            {**payload, "topology_ref": topology.to_dict()}
        )
        compilation = ExecutionCompiler(self.repository, self.architecture).compile_epoch(
            workflow_id="wf_parallel",
            epoch_id="epoch_parallel",
            manifest_ref=independent_manifest,
        )

        queued = DagScheduler(self.repository).schedule_ready_nodes(
            workflow_id="wf_parallel",
            epoch_id=compilation.epoch_id,
            max_new_nodes=2,
        )

        self.assertEqual(set(queued), set(compilation.module_node_ids.values()))

    def test_replan_reuses_only_exactly_matching_accepted_candidates(self) -> None:
        manifest = self._manifest()
        first = ExecutionCompiler(self.repository, self.architecture).compile_epoch(
            workflow_id="wf_reuse",
            epoch_id="epoch_reuse_1",
            manifest_ref=manifest,
        )
        scheduler = DagScheduler(self.repository)
        scheduler.schedule_ready_nodes(workflow_id="wf_reuse", epoch_id=first.epoch_id, max_new_nodes=2)
        self._accept_node(first.module_node_ids["a"])
        scheduler.schedule_ready_nodes(workflow_id="wf_reuse", epoch_id=first.epoch_id, max_new_nodes=2)
        self._accept_node(first.module_node_ids["b"])

        second = ExecutionCompiler(self.repository, self.architecture).compile_epoch(
            workflow_id="wf_reuse",
            epoch_id="epoch_reuse_2",
            manifest_ref=manifest,
            reuse_from_epoch_id=first.epoch_id,
        )
        first_a = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, first.module_node_ids["a"])
        second_a = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, second.module_node_ids["a"])
        self.assertEqual(first_a.payload["epoch_base_tree_sha"], second_a.payload["epoch_base_tree_sha"])
        self.assertEqual(first_a.payload["environment_fingerprint"], second_a.payload["environment_fingerprint"])
        for module_id in ("a", "b"):
            source = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, first.module_node_ids[module_id])
            reused = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, second.module_node_ids[module_id])
            self.assertEqual(reused.state, "ACCEPTED")
            self.assertEqual(reused.payload["candidate_sha"], source.payload["candidate_sha"])
            self.assertEqual(reused.payload["reused_from_epoch_id"], first.epoch_id)

        changed_payload = self.store.read_json(manifest)
        changed_constraints = self.architecture.publish_fragment(
            ["new global constraint"], artifact_type="GlobalConstraintsArtifact"
        )
        changed_manifest = self.architecture.publish_manifest(
            {**changed_payload, "global_constraints_ref": changed_constraints.to_dict()}
        )
        third = ExecutionCompiler(self.repository, self.architecture).compile_epoch(
            workflow_id="wf_reuse",
            epoch_id="epoch_reuse_3",
            manifest_ref=changed_manifest,
            reuse_from_epoch_id=first.epoch_id,
        )
        self.assertEqual(
            self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, third.module_node_ids["a"]).state,
            "BLOCKED_BY_DEPS",
        )

    def test_module_work_view_preserves_architect_evidence(self) -> None:
        manifest = self._manifest()
        compilation = ExecutionCompiler(self.repository, self.architecture).compile_epoch(
            workflow_id="wf_view",
            epoch_id="epoch_view",
            manifest_ref=manifest,
        )
        node = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, compilation.module_node_ids["a"])
        view_ref = ModuleWorkViewBuilder(self.architecture).build(node, dependency_outputs={})
        view = self.store.read_json(view_ref)
        self.assertEqual([item["evidence_id"] for item in view["evidence"]], ["E-A"])
        self.assertEqual([item["requirement_id"] for item in view["requirements"]], ["R-A"])

    def test_verification_uses_disposable_detached_worktree(self) -> None:
        manifest = self._manifest()
        compilation = ExecutionCompiler(self.repository, self.architecture).compile_epoch(
            workflow_id="wf_review_tree",
            epoch_id="epoch_review_tree",
            manifest_ref=manifest,
        )
        node = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, compilation.module_node_ids["a"])
        candidate_worktree = Path(str(node.payload["worktree_path"]))
        (candidate_worktree / "src" / "a").mkdir(parents=True, exist_ok=True)
        (candidate_worktree / "src" / "a" / "candidate.txt").write_text("candidate\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=candidate_worktree, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "candidate"],
            cwd=candidate_worktree,
            check=True,
        )
        candidate_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=candidate_worktree, text=True
        ).strip()
        review_worktree, scratch = provision_verification_worktree(
            self.runtime_root,
            node=node,
            candidate_sha=candidate_sha,
        )
        (review_worktree / "review-only.txt").write_text("probe\n", encoding="utf-8")
        (scratch / "adversarial_test.py").write_text("assert True\n", encoding="utf-8")
        self.assertFalse((candidate_worktree / "review-only.txt").exists())
        same_worktree, _ = provision_verification_worktree(
            self.runtime_root,
            node=node,
            candidate_sha=candidate_sha,
        )
        self.assertEqual(same_worktree, review_worktree)
        self.assertFalse((review_worktree / "review-only.txt").exists())

    def test_journal_is_mutable_but_lease_fenced(self) -> None:
        lease = self.repository.claim_lease("node:journal", "worker_journal", ttl_seconds=60)
        journal = ModuleRunJournal(
            current_micro_plan=("write failing test", "implement"),
            files_inspected=("src/a/api.h",),
            last_safe_point="test written",
        )
        generation = self.repository.update_module_journal(
            node_run_id="node_journal",
            workflow_id="wf_journal",
            lease_resource_key=lease.resource_key,
            owner_id=lease.owner_id,
            fencing_token=lease.fencing_token,
            expected_generation=0,
            journal=journal.to_dict(),
        )
        self.assertEqual(generation, 1)
        with self.assertRaises(AggregateVersionConflict):
            self.repository.update_module_journal(
                node_run_id="node_journal",
                workflow_id="wf_journal",
                lease_resource_key=lease.resource_key,
                owner_id=lease.owner_id,
                fencing_token=lease.fencing_token,
                expected_generation=0,
                journal=journal.to_dict(),
            )
        self.repository.release_lease(lease.resource_key, lease.owner_id, lease.fencing_token)
        with self.assertRaises(StaleFencingToken):
            self.repository.update_module_journal(
                node_run_id="node_journal",
                workflow_id="wf_journal",
                lease_resource_key=lease.resource_key,
                owner_id=lease.owner_id,
                fencing_token=lease.fencing_token,
                expected_generation=1,
                journal=journal.to_dict(),
            )

    def test_quiesced_manager_creates_candidate_commit(self) -> None:
        worktree = self.runtime_root / "candidate_repo"
        worktree.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=worktree, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=worktree, check=True)
        (worktree / "src").mkdir()
        (worktree / "src" / "a.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=worktree, check=True)
        base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=worktree, text=True).strip()
        (worktree / "src" / "a.txt").write_text("candidate\n", encoding="utf-8")

        contract = self.store.put_json({"module_id": "candidate"}, artifact_type="ModuleContractArtifact")
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_NODE_RUN",
                workflow_id="wf_candidate",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node_candidate",
                actor="test",
                expected_version=0,
                idempotency_key="node-candidate:create",
                payload={
                    "module_contract_ref": contract.to_dict(),
                    "epoch_id": "epoch_candidate",
                    "environment_fingerprint": "env-hash",
                },
            )
        )

        lease = self.repository.claim_lease("worktree:node_candidate", "worker_candidate", ttl_seconds=60)
        locks = WorktreeLockRegistry()
        controller = _StoppedProcessController()
        quiesced = NodeQuiescer(self.repository, controller, locks).quiesce(
            node_run_id="node_candidate",
            worker_id=lease.owner_id,
            lease_resource_key=lease.resource_key,
            fencing_token=lease.fencing_token,
            worktree=worktree,
        )
        self.assertTrue(locks.is_held("node_candidate"))
        candidate_ref, candidate_sha = CandidateSnapshotService(self.repository, self.store, locks).create_candidate(
            node_run_id="node_candidate",
            worker_id=lease.owner_id,
            lease_resource_key=lease.resource_key,
            fencing_token=lease.fencing_token,
            worktree=worktree,
            expected_worktree_fingerprint=quiesced.worktree_fingerprint,
            owned_area=["src/**"],
            reference_only_paths=["references/**"],
            base_sha=base_sha,
            module_contract_hash=contract.sha256,
            dependency_output_hashes={},
            environment_fingerprint="env-hash",
        )
        self.assertFalse(locks.is_held("node_candidate"))
        self.assertEqual(subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=worktree, text=True).strip(), candidate_sha)
        self.assertEqual(self.store.read_json(candidate_ref)["changed_paths"], ["src/a.txt"])
        message = subprocess.check_output(["git", "log", "-1", "--format=%B"], cwd=worktree, text=True)
        self.assertIn("Pal-Candidate-Key:", message)

    def test_candidate_rejects_contract_hash_and_owned_area_violations(self) -> None:
        worktree = self.runtime_root / "candidate_rejection_repo"
        worktree.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=worktree, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=worktree, check=True)
        (worktree / "base.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=worktree, check=True)
        base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=worktree, text=True).strip()
        (worktree / "outside.txt").write_text("not owned\n", encoding="utf-8")
        contract = self.store.put_json({"module_id": "reject"}, artifact_type="ModuleContractArtifact")
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_NODE_RUN",
                workflow_id="wf_reject_candidate",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node_reject_candidate",
                actor="test",
                expected_version=0,
                idempotency_key="node-reject-candidate:create",
                payload={
                    "module_contract_ref": contract.to_dict(),
                    "epoch_id": "epoch_reject",
                    "environment_fingerprint": "env-hash",
                },
            )
        )
        lease = self.repository.claim_lease("worktree:node_reject_candidate", "worker_reject", ttl_seconds=60)
        locks = WorktreeLockRegistry()
        service = CandidateSnapshotService(self.repository, self.store, locks)

        def snapshot(*, contract_hash: str) -> None:
            quiesced = NodeQuiescer(
                self.repository,
                _StoppedProcessController(),
                locks,
            ).quiesce(
                node_run_id="node_reject_candidate",
                worker_id=lease.owner_id,
                lease_resource_key=lease.resource_key,
                fencing_token=lease.fencing_token,
                worktree=worktree,
            )
            service.create_candidate(
                node_run_id="node_reject_candidate",
                worker_id=lease.owner_id,
                lease_resource_key=lease.resource_key,
                fencing_token=lease.fencing_token,
                worktree=worktree,
                expected_worktree_fingerprint=quiesced.worktree_fingerprint,
                owned_area=["src/**"],
                reference_only_paths=["references/**"],
                base_sha=base_sha,
                module_contract_hash=contract_hash,
                dependency_output_hashes={},
                environment_fingerprint="env-hash",
            )

        with self.assertRaisesRegex(ValueError, "contract hash"):
            snapshot(contract_hash="wrong")
        self.assertFalse(locks.is_held("node_reject_candidate"))
        with self.assertRaisesRegex(ValueError, "outside owned_area"):
            snapshot(contract_hash=contract.sha256)
        self.assertFalse(locks.is_held("node_reject_candidate"))

    def _accept_node(self, node_id: str) -> None:
        dummy = self.store.put_json({"node_id": node_id}, artifact_type="TestArtifact")
        initial = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
        worktree = Path(str(initial.payload["worktree_path"]))
        module_id = str(initial.payload.get("module_id") or "module")
        candidate_path = worktree / "src" / module_id / "candidate.txt"
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_path.write_text(f"candidate for {module_id}\n", encoding="utf-8")
        subprocess.run(["git", "add", str(candidate_path.relative_to(worktree))], cwd=worktree, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-qm",
                f"candidate {node_id}",
            ],
            cwd=worktree,
            check=True,
        )
        candidate_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=worktree, text=True).strip()
        sequence = [
            ("START_CODING", {"fencing_token": 1}),
            ("SUBMIT_CANDIDATE", {"fencing_token": 1}),
            (
                "QUIESCE_COMPLETED",
                {
                    "fencing_token": 1,
                    "process_group_reaped": True,
                    "exclusive_worktree_lock": True,
                    "worktree_fingerprint": "tree",
                },
            ),
            (
                "CANDIDATE_SNAPSHOTTED",
                {
                    "candidate_ref": dummy.to_dict(),
                    "candidate_sha": candidate_sha,
                    "worktree_fingerprint": "tree",
                },
            ),
            ("START_REVIEW", {"fencing_token": 2}),
            ("REVIEW_PASSED", {"verification_artifact_ref": dummy.to_dict()}),
        ]
        for action_type, payload in sequence:
            snapshot = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
            self.repository.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=snapshot.workflow_id,
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node_id,
                    actor="test",
                    payload=payload,
                    expected_version=snapshot.version,
                    idempotency_key=f"{node_id}:{action_type}:{snapshot.version}",
                )
            )


if __name__ == "__main__":
    unittest.main()
