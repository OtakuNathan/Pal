from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pal.minion.v2 import ActionEnvelope, AggregateType, ContentAddressedArtifactStore, MinionV2Repository
from pal.minion.v2.architecture import ArchitectureArtifactService
from pal.minion.v2.adapters import SOFTWARE_GIT_ADAPTER
from pal.minion.v2.contracts import AggregateSnapshot, AggregateVersionConflict, StaleFencingToken
from pal.minion.v2.execution import (
    CandidateSnapshotService,
    DagScheduler,
    ExecutionCompiler,
    NodeRunJournal,
    UnitWorkViewBuilder,
    WorkspaceLockRegistry,
    prepare_node_dependency_baseline,
    provision_module_verification_workspace,
    format_workspace_process_holders,
    terminate_process_group,
    workspace_process_holders,
    workspace_content_fingerprint,
    _validate_skeleton_candidate_paths,
)
from pal.minion.v2.task_ledger import TaskLedgerService


def _lock_candidate_workspace(
    locks: WorkspaceLockRegistry,
    node_run_id: str,
    worktree: Path,
) -> str:
    locks.acquire(node_run_id, worktree)
    return workspace_content_fingerprint(worktree)


def _budget() -> dict[str, int]:
    return {
        "target_file_count": 2,
        "estimated_context_tokens": 8000,
        "public_interface_count": 2,
        "cross_unit_contract_count": 1,
        "stateful_resource_count": 0,
        "expected_candidate_cycles": 2,
        "platform_dependency_level": 1,
    }


def _contract(unit_id: str, owned_area: str) -> dict:
    return {
        "unit_id": unit_id,
        "unit_behavior_kind": "stateless",
        "responsibility": f"Implement {unit_id}.",
        "owned_area": [owned_area],
        "reference_only_paths": ["references/**"],
        "provided_interfaces": [{"name": f"{unit_id}_output"}],
        "consumed_interfaces": [],
        "ownership": {"rule": f"{unit_id} exclusively owns its output."},
        "lifecycle": "N/A",
        "state_model": "stateless",
        "invariants": [f"{unit_id} remains deterministic"],
        "error_behavior": ["Invalid input fails deterministically."],
        "compatibility": ["The public output shape remains stable."],
        "dependency_constraints": [],
        "verification_obligations": [{"kind": "consumer_probe"}],
        "complexity_budget": _budget(),
        "split_conditions": [],
    }


class MinionV2ExecutionTests(unittest.TestCase):
    def test_contract_enforcement_modes_distinguish_frozen_and_guarded_files(self) -> None:
        policy = {
            "contract_paths": ["src/router.py"],
            "reference_only": [],
            "implementation_scopes": [{"kind": "file", "path": "src/router.py"}],
            "verification_corpus": {"kind": "directory", "path": "tests/router"},
        }
        with self.assertRaisesRegex(ValueError, "frozen architecture contracts"):
            _validate_skeleton_candidate_paths(
                ["src/router.py"],
                {**policy, "contract_mode": "file_frozen"},
            )

        _validate_skeleton_candidate_paths(
            ["src/router.py"],
            {**policy, "contract_mode": "review_guarded"},
        )
        with self.assertRaisesRegex(ValueError, "outside"):
            _validate_skeleton_candidate_paths(
                ["tests/router/test_router.py"],
                {**policy, "contract_mode": "review_guarded"},
            )
        with self.assertRaisesRegex(ValueError, "outside"):
            _validate_skeleton_candidate_paths(
                ["src/other.py"],
                {
                    **policy,
                    "contract_mode": "review_guarded",
                    "implementation_scopes": [],
                },
            )

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

    def test_workspace_process_holders_report_read_and_write_access(self) -> None:
        if not Path("/proc").is_dir():
            self.skipTest("process holder diagnostics require procfs")
        workspace = self.runtime_root / "holder-worktree"
        workspace.mkdir()
        held_file = workspace / "held.txt"
        held_file.write_text("content\n", encoding="utf-8")

        with held_file.open("rb"), held_file.open("ab"):
            holders = workspace_process_holders(workspace)

        current = next(item for item in holders if item.pid == os.getpid())
        self.assertIn("held.txt", current.read_paths)
        self.assertIn("held.txt", current.write_paths)
        rendered = format_workspace_process_holders(holders)
        self.assertIn(f'"pid":{os.getpid()}', rendered)
        self.assertIn('"write_paths":["held.txt"]', rendered)

    def _manifest(self):
        requirements = TaskLedgerService(self.runtime_root, self.store).publish(
            title="A and B",
            task_spec={"objective": "Implement A, then implement B using A."},
            actor="test",
            source_channel="test",
        )
        module_a = self.architecture.publish_unit_contract(_contract("a", "src/a/**"))
        module_b = self.architecture.publish_unit_contract(_contract("b", "src/b/**"))
        constraints = self.architecture.publish_fragment([], artifact_type="GlobalConstraintsArtifact")
        gates = self.architecture.publish_fragment([], artifact_type="ArchitectureGateChecksArtifact")
        cross = self.architecture.publish_fragment(
            {"contract_id": "X", "provider": "a", "consumer": "b"},
            artifact_type="CrossUnitContractArtifact",
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
                "global_constraints_ref": constraints.to_dict(),
                "gate_checks_ref": gates.to_dict(),
                "unit_contract_refs": [module_a.to_dict(), module_b.to_dict()],
                "cross_unit_contract_refs": [cross.to_dict()],
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
        self.assertEqual(scheduler.schedule_ready_nodes(workflow_id="wf_exec", epoch_id="epoch_1", max_new_nodes=3), (compilation.unit_node_ids["a"],))
        self._accept_node(compilation.unit_node_ids["a"])
        self.assertEqual(scheduler.schedule_ready_nodes(workflow_id="wf_exec", epoch_id="epoch_1", max_new_nodes=3), (compilation.unit_node_ids["b"],))
        self._accept_node(compilation.unit_node_ids["b"])
        self.assertEqual(scheduler.schedule_ready_nodes(workflow_id="wf_exec", epoch_id="epoch_1", max_new_nodes=3), (compilation.integration_node_id,))

    def test_node_baseline_applies_the_complete_construction_dependency_closure(self) -> None:
        worktree = self.runtime_root / "dependency_closure_repo"
        worktree.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=worktree, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=worktree, check=True)
        (worktree / "base.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=worktree, check=True)
        base_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=worktree, text=True
        ).strip()
        (worktree / "foundation.txt").write_text("foundation\n", encoding="utf-8")
        subprocess.run(["git", "add", "foundation.txt"], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-qm", "foundation"], cwd=worktree, check=True)
        foundation_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=worktree, text=True
        ).strip()
        (worktree / "drawing.txt").write_text("drawing\n", encoding="utf-8")
        subprocess.run(["git", "add", "drawing.txt"], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-qm", "drawing"], cwd=worktree, check=True)
        drawing_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=worktree, text=True
        ).strip()
        subprocess.run(["git", "checkout", "-q", "--detach", base_sha], cwd=worktree, check=True)

        def node(
            node_id: str,
            *,
            state: str,
            candidate_digest: str = "",
            candidate_base: str = "",
            dependencies: tuple[str, ...] = (),
        ) -> AggregateSnapshot:
            return AggregateSnapshot(
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=node_id,
                workflow_id="wf_dependency_closure",
                state=state,
                version=1,
                payload={
                    "workspace_path": str(worktree),
                    "execution_adapter": SOFTWARE_GIT_ADAPTER,
                    "dependency_node_ids": list(dependencies),
                    "base_sha": candidate_base or base_sha,
                    "candidate_digest": candidate_digest,
                    "candidate_ref": {"sha256": candidate_digest},
                    "output_hashes": {"surface": node_id},
                },
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            )

        foundation = node("foundation", state="ACCEPTED", candidate_digest=foundation_sha)
        drawing = node(
            "drawing",
            state="ACCEPTED",
            candidate_digest=drawing_sha,
            candidate_base=foundation_sha,
            dependencies=(foundation.aggregate_id,),
        )
        window = node("window", state="BLOCKED_BY_DEPS", dependencies=(drawing.aggregate_id,))
        baseline = prepare_node_dependency_baseline(
            window,
            {
                foundation.aggregate_id: foundation,
                drawing.aggregate_id: drawing,
                window.aggregate_id: window,
            },
        )

        self.assertEqual(
            baseline["accepted_dependency_candidate_digests"],
            [foundation_sha, drawing_sha],
        )
        self.assertEqual((worktree / "foundation.txt").read_text(encoding="utf-8"), "foundation\n")
        self.assertEqual((worktree / "drawing.txt").read_text(encoding="utf-8"), "drawing\n")

    def test_node_baseline_applies_every_commit_in_an_accepted_candidate(self) -> None:
        worktree = self.runtime_root / "candidate_chain_repo"
        worktree.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=worktree, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=worktree, check=True)
        (worktree / "module.cpp").write_text("// skeleton\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=worktree, check=True)
        base_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=worktree, text=True
        ).strip()
        (worktree / "module.cpp").write_text("int value() { return 1; }\n", encoding="utf-8")
        subprocess.run(["git", "add", "module.cpp"], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-qm", "implementation"], cwd=worktree, check=True)
        (worktree / "module_test.cpp").write_text("// verifier test\n", encoding="utf-8")
        subprocess.run(["git", "add", "module_test.cpp"], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-qm", "checkpoint verifier test"], cwd=worktree, check=True)
        accepted_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=worktree, text=True
        ).strip()
        subprocess.run(["git", "reset", "--hard", base_sha], cwd=worktree, check=True)

        dependency = AggregateSnapshot(
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="dependency",
            workflow_id="wf-chain",
            state="ACCEPTED",
            version=1,
            payload={
                "base_sha": base_sha,
                "candidate_digest": accepted_sha,
                "candidate_ref": {"sha256": "candidate"},
                "dependency_node_ids": [],
                "output_hashes": {},
            },
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        consumer = AggregateSnapshot(
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="consumer",
            workflow_id="wf-chain",
            state="BLOCKED_BY_DEPS",
            version=1,
            payload={
                "workspace_path": str(worktree),
                "execution_adapter": SOFTWARE_GIT_ADAPTER,
                "base_sha": base_sha,
                "dependency_node_ids": [dependency.aggregate_id],
            },
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )

        prepare_node_dependency_baseline(
            consumer,
            {dependency.aggregate_id: dependency, consumer.aggregate_id: consumer},
        )

        self.assertEqual(
            (worktree / "module.cpp").read_text(encoding="utf-8"),
            "int value() { return 1; }\n",
        )
        self.assertTrue((worktree / "module_test.cpp").is_file())

    def test_dependency_baseline_rolls_back_the_whole_attempt_on_conflict(self) -> None:
        worktree = self.runtime_root / "dependency_rollback_repo"
        worktree.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=worktree, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=worktree, check=True)
        shared = worktree / "shared.txt"
        shared.write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=worktree, check=True)
        base_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=worktree, text=True
        ).strip()
        candidates: list[str] = []
        for value in ("first\n", "second\n"):
            subprocess.run(["git", "reset", "--hard", base_sha], cwd=worktree, check=True)
            shared.write_text(value, encoding="utf-8")
            subprocess.run(["git", "add", "shared.txt"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-qm", value.strip()], cwd=worktree, check=True)
            candidates.append(
                subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=worktree, text=True
                ).strip()
            )
        subprocess.run(["git", "reset", "--hard", base_sha], cwd=worktree, check=True)

        dependencies: dict[str, AggregateSnapshot] = {}
        for index, candidate_sha in enumerate(candidates):
            dependency = AggregateSnapshot(
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=f"dependency-{index}",
                workflow_id="wf-rollback",
                state="ACCEPTED",
                version=1,
                payload={
                    "base_sha": base_sha,
                    "candidate_digest": candidate_sha,
                    "candidate_ref": {"sha256": candidate_sha},
                    "dependency_node_ids": [],
                    "output_hashes": {},
                },
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            )
            dependencies[dependency.aggregate_id] = dependency
        consumer = AggregateSnapshot(
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="consumer",
            workflow_id="wf-rollback",
            state="BLOCKED_BY_DEPS",
            version=1,
            payload={
                "workspace_path": str(worktree),
                "execution_adapter": SOFTWARE_GIT_ADAPTER,
                "base_sha": base_sha,
                "dependency_node_ids": sorted(dependencies),
            },
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )

        with self.assertRaises(subprocess.CalledProcessError):
            prepare_node_dependency_baseline(
                consumer,
                {**dependencies, consumer.aggregate_id: consumer},
            )

        self.assertEqual(
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=worktree, text=True
            ).strip(),
            base_sha,
        )
        self.assertEqual(shared.read_text(encoding="utf-8"), "base\n")
        self.assertEqual(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=worktree, text=True
            ),
            "",
        )

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

        self.assertEqual(set(queued), set(compilation.unit_node_ids.values()))

    def test_integration_dependencies_preserve_topological_merge_order(self) -> None:
        manifest = self._manifest()
        payload = self.store.read_json(manifest)
        contracts = [self.store.read_json(ref) for ref in payload["unit_contract_refs"]]
        refs_by_name = {
            str(contract["unit_id"]): ref
            for contract, ref in zip(contracts, payload["unit_contract_refs"], strict=True)
        }
        topology = self.architecture.publish_fragment(
            {"depends_on": {"a": ["b"], "b": []}},
            artifact_type="TopologyArtifact",
        )
        reordered = self.architecture.publish_manifest(
            {
                **payload,
                "unit_contract_refs": [refs_by_name["a"], refs_by_name["b"]],
                "topology_ref": topology.to_dict(),
            }
        )
        compilation = ExecutionCompiler(self.repository, self.architecture).compile_epoch(
            workflow_id="wf_topological_merge",
            epoch_id="epoch_topological_merge",
            manifest_ref=reordered,
        )
        integration = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN, compilation.integration_node_id
        )
        assert integration is not None
        self.assertEqual(
            integration.payload["dependency_node_ids"],
            [compilation.unit_node_ids["b"], compilation.unit_node_ids["a"]],
        )

    def test_replan_reuses_only_exactly_matching_accepted_candidates(self) -> None:
        manifest = self._manifest()
        first = ExecutionCompiler(self.repository, self.architecture).compile_epoch(
            workflow_id="wf_reuse",
            epoch_id="epoch_reuse_1",
            manifest_ref=manifest,
        )
        scheduler = DagScheduler(self.repository)
        scheduler.schedule_ready_nodes(workflow_id="wf_reuse", epoch_id=first.epoch_id, max_new_nodes=2)
        self._accept_node(first.unit_node_ids["a"])
        scheduler.schedule_ready_nodes(workflow_id="wf_reuse", epoch_id=first.epoch_id, max_new_nodes=2)
        self._accept_node(first.unit_node_ids["b"])

        second = ExecutionCompiler(self.repository, self.architecture).compile_epoch(
            workflow_id="wf_reuse",
            epoch_id="epoch_reuse_2",
            manifest_ref=manifest,
            source_epoch_id=first.epoch_id,
        )
        first_a = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, first.unit_node_ids["a"])
        second_a = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, second.unit_node_ids["a"])
        self.assertEqual(first_a.payload["epoch_base_tree_sha"], second_a.payload["epoch_base_tree_sha"])
        self.assertEqual(first_a.payload["environment_fingerprint"], second_a.payload["environment_fingerprint"])
        for unit_id in ("a", "b"):
            source = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, first.unit_node_ids[unit_id])
            reused = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, second.unit_node_ids[unit_id])
            self.assertEqual(reused.state, "ACCEPTED")
            self.assertEqual(reused.payload["candidate_digest"], source.payload["candidate_digest"])
            self.assertEqual(reused.payload["carried_forward_from_epoch_id"], first.epoch_id)
            self.assertEqual(reused.payload["workspace_path"], source.payload["workspace_path"])
            self.assertEqual(reused.payload["worktree_branch"], source.payload["worktree_branch"])

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
            source_epoch_id=first.epoch_id,
        )
        self.assertEqual(
            self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, third.unit_node_ids["a"]).state,
            "BLOCKED_BY_DEPS",
        )

    def test_unit_work_view_contains_only_module_local_semantics(self) -> None:
        manifest = self._manifest()
        compilation = ExecutionCompiler(self.repository, self.architecture).compile_epoch(
            workflow_id="wf_view",
            epoch_id="epoch_view",
            manifest_ref=manifest,
        )
        node = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, compilation.unit_node_ids["a"])
        view_ref = UnitWorkViewBuilder(self.architecture).build(node)
        view = self.store.read_json(view_ref)
        self.assertNotIn("evidence", view)
        self.assertNotIn("requirements", view)
        self.assertEqual(view["unit_contract"]["name"], "a")
        self.assertNotIn("unit_id", view["unit_contract"])

    def test_verification_reuses_module_worktree_with_candidate_scoped_scratch(self) -> None:
        manifest = self._manifest()
        compilation = ExecutionCompiler(self.repository, self.architecture).compile_epoch(
            workflow_id="wf_review_tree",
            epoch_id="epoch_review_tree",
            manifest_ref=manifest,
        )
        node = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, compilation.unit_node_ids["a"])
        candidate_worktree = Path(str(node.payload["workspace_path"]))
        (candidate_worktree / "src" / "a").mkdir(parents=True, exist_ok=True)
        (candidate_worktree / "src" / "a" / "candidate.txt").write_text("candidate\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=candidate_worktree, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "candidate"],
            cwd=candidate_worktree,
            check=True,
        )
        candidate_digest = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=candidate_worktree, text=True
        ).strip()
        review_worktree, scratch = provision_module_verification_workspace(
            self.runtime_root,
            node=node,
            candidate_digest=candidate_digest,
        )
        (scratch / "adversarial_test.py").write_text("assert True\n", encoding="utf-8")
        self.assertEqual(review_worktree, candidate_worktree)
        same_worktree, same_scratch = provision_module_verification_workspace(
            self.runtime_root,
            node=node,
            candidate_digest=candidate_digest,
        )
        self.assertEqual(same_worktree, review_worktree)
        self.assertEqual(same_scratch, scratch)
        self.assertTrue((same_scratch / "adversarial_test.py").is_file())

    def test_journal_is_mutable_but_lease_fenced(self) -> None:
        lease = self.repository.claim_lease("node:journal", "worker_journal", ttl_seconds=60)
        journal = NodeRunJournal(
            current_micro_plan=("write failing test", "implement"),
            files_inspected=("src/a/api.h",),
            last_safe_point="test written",
        )
        self.assertNotIn("tests_run", journal.to_dict())
        generation = self.repository.update_node_journal(
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
            self.repository.update_node_journal(
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
            self.repository.update_node_journal(
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

        contract = self.store.put_json({"unit_id": "candidate"}, artifact_type="UnitContractArtifact")
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
                    "unit_contract_ref": contract.to_dict(),
                    "epoch_id": "epoch_candidate",
                    "environment_fingerprint": "env-hash",
                },
            )
        )

        lease = self.repository.claim_lease("worktree:node_candidate", "worker_candidate", ttl_seconds=60)
        locks = WorkspaceLockRegistry()
        workspace_fingerprint = _lock_candidate_workspace(
            locks,
            "node_candidate",
            worktree,
        )
        self.assertTrue(locks.is_held("node_candidate"))
        candidate_ref, candidate_digest = CandidateSnapshotService(self.repository, self.store, locks).create_candidate(
            node_run_id="node_candidate",
            worker_id=lease.owner_id,
            lease_resource_key=lease.resource_key,
            fencing_token=lease.fencing_token,
            worktree=worktree,
            expected_workspace_fingerprint=workspace_fingerprint,
            reference_only_paths=["references/**"],
            base_sha=base_sha,
            candidate_baseline_sha=base_sha,
            unit_contract_hash=contract.sha256,
            dependency_output_hashes={},
            environment_fingerprint="env-hash",
        )
        self.assertFalse(locks.is_held("node_candidate"))
        self.assertEqual(subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=worktree, text=True).strip(), candidate_digest)
        self.assertEqual(self.store.read_json(candidate_ref)["changed_paths"], ["src/a.txt"])
        message = subprocess.check_output(["git", "log", "-1", "--format=%B"], cwd=worktree, text=True)
        self.assertIn("Pal-Assignment-Key:", message)

    def test_workspace_lock_identity_is_the_canonical_worktree(self) -> None:
        worktree = self.runtime_root / "canonical_lock_workspace"
        worktree.mkdir()
        locks = WorkspaceLockRegistry()
        locks.acquire("old-node-run", worktree)
        with self.assertRaisesRegex(BlockingIOError, "workspace lock is already held"):
            locks.acquire("new-node-run", worktree / ".")
        locks.release("old-node-run")
        locks.acquire("new-node-run", worktree)
        self.assertTrue(locks.is_held("new-node-run"))
        locks.release("new-node-run")

    def test_repair_candidate_is_a_linear_delta_from_the_previous_checkpoint(self) -> None:
        worktree = self.runtime_root / "cumulative_candidate_repo"
        worktree.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=worktree, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=worktree, check=True)
        (worktree / "src").mkdir()
        (worktree / "src" / "first.cpp").write_text("base first\n", encoding="utf-8")
        (worktree / "src" / "second.cpp").write_text("base second\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=worktree, check=True)
        baseline_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=worktree, text=True
        ).strip()
        contract = self.store.put_json(
            {"module_name": "cumulative"},
            artifact_type="ArchitectureSkeletonModuleContractArtifact",
        )
        node_id = "node_cumulative_candidate"
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_NODE_RUN",
                workflow_id="wf_cumulative_candidate",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=node_id,
                actor="test",
                expected_version=0,
                idempotency_key=f"{node_id}:create",
                payload={
                    "unit_contract_ref": contract.to_dict(),
                    "epoch_id": "epoch_cumulative_candidate",
                    "environment_fingerprint": "env-hash",
                },
            )
        )
        lease = self.repository.claim_lease(
            f"worktree:{node_id}", "worker_cumulative", ttl_seconds=60
        )
        locks = WorkspaceLockRegistry()
        snapshotter = CandidateSnapshotService(self.repository, self.store, locks)

        def snapshot(*, current_head: str, parent_candidate: str = ""):
            workspace_fingerprint = _lock_candidate_workspace(
                locks,
                node_id,
                worktree,
            )
            return snapshotter.create_candidate(
                node_run_id=node_id,
                worker_id=lease.owner_id,
                lease_resource_key=lease.resource_key,
                fencing_token=lease.fencing_token,
                worktree=worktree,
                expected_workspace_fingerprint=workspace_fingerprint,
                reference_only_paths=[],
                path_policy={
                    "contract_paths": [],
                    "reference_only": [],
                    "implementation_scopes": [{"kind": "directory", "path": "src"}],
                    "verification_corpus": {"kind": "directory", "path": "tests/module"},
                },
                base_sha=current_head,
                candidate_baseline_sha=baseline_sha,
                unit_contract_hash=contract.sha256,
                dependency_output_hashes={},
                environment_fingerprint="env-hash",
            )

        (worktree / "src" / "first.cpp").write_text("implemented first\n", encoding="utf-8")
        first_ref, first_digest = snapshot(current_head=baseline_sha)
        (worktree / "src" / "second.cpp").write_text("repaired second\n", encoding="utf-8")
        second_ref, second_digest = snapshot(
            current_head=first_digest,
            parent_candidate=first_digest,
        )

        second = self.store.read_json(second_ref)
        self.assertEqual(second["base_sha"], first_digest)
        self.assertEqual(second["previous_head_sha"], first_digest)
        self.assertEqual(second["architecture_base_sha"], baseline_sha)
        self.assertEqual(second["changed_paths"], ["src/second.cpp"])
        self.assertEqual(
            subprocess.check_output(
                ["git", "rev-parse", f"{second_digest}^"], cwd=worktree, text=True
            ).strip(),
            first_digest,
        )
        self.assertEqual(
            (worktree / "src" / "first.cpp").read_text(encoding="utf-8"),
            "implemented first\n",
        )
        self.assertEqual(
            (worktree / "src" / "second.cpp").read_text(encoding="utf-8"),
            "repaired second\n",
        )
        self.assertEqual(self.store.read_json(first_ref)["changed_paths"], ["src/first.cpp"])

    def test_candidate_rejects_contract_hash_and_reference_only_violations(self) -> None:
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
        contract = self.store.put_json({"unit_id": "reject"}, artifact_type="UnitContractArtifact")
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
                    "unit_contract_ref": contract.to_dict(),
                    "epoch_id": "epoch_reject",
                    "environment_fingerprint": "env-hash",
                },
            )
        )
        lease = self.repository.claim_lease("worktree:node_reject_candidate", "worker_reject", ttl_seconds=60)
        locks = WorkspaceLockRegistry()
        service = CandidateSnapshotService(self.repository, self.store, locks)

        def snapshot(*, contract_hash: str) -> None:
            workspace_fingerprint = _lock_candidate_workspace(
                locks,
                "node_reject_candidate",
                worktree,
            )
            service.create_candidate(
                node_run_id="node_reject_candidate",
                worker_id=lease.owner_id,
                lease_resource_key=lease.resource_key,
                fencing_token=lease.fencing_token,
                worktree=worktree,
                expected_workspace_fingerprint=workspace_fingerprint,
                reference_only_paths=["references/**"],
                base_sha=base_sha,
                candidate_baseline_sha=base_sha,
                unit_contract_hash=contract_hash,
                dependency_output_hashes={},
                environment_fingerprint="env-hash",
            )

        with self.assertRaisesRegex(ValueError, "contract hash"):
            snapshot(contract_hash="wrong")
        self.assertFalse(locks.is_held("node_reject_candidate"))
        snapshot(contract_hash=contract.sha256)
        self.assertFalse(locks.is_held("node_reject_candidate"))

    def test_skeleton_candidate_rejects_frozen_reference_and_unowned_changes(self) -> None:
        cases = {
            "frozen rename": (
                lambda root: subprocess.run(
                    ["git", "mv", "include/contract.h", "src/contract.h"], cwd=root, check=True
                ),
                "frozen architecture contracts",
            ),
            "reference write": (
                lambda root: (root / "reference" / "api.h").write_text(
                    "changed reference\n", encoding="utf-8"
                ),
                "reference-only paths",
            ),
            "outside write": (
                lambda root: (root / "docs.txt").write_text("outside\n", encoding="utf-8"),
                "outside its compiled module write scopes",
            ),
        }
        for index, (name, (mutate, error)) in enumerate(cases.items()):
            with self.subTest(name=name):
                worktree = self.runtime_root / f"skeleton_candidate_{index}"
                worktree.mkdir()
                subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
                subprocess.run(["git", "config", "user.name", "Test"], cwd=worktree, check=True)
                subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=worktree, check=True)
                for directory in ("include", "reference", "src", "tests"):
                    (worktree / directory).mkdir()
                (worktree / "include" / "contract.h").write_text("contract\n", encoding="utf-8")
                (worktree / "reference" / "api.h").write_text("reference\n", encoding="utf-8")
                (worktree / "src" / "owned.cpp").write_text("base\n", encoding="utf-8")
                subprocess.run(["git", "add", "."], cwd=worktree, check=True)
                subprocess.run(["git", "commit", "-qm", "base"], cwd=worktree, check=True)
                base_sha = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=worktree, text=True
                ).strip()
                mutate(worktree)

                node_id = f"node_skeleton_candidate_{index}"
                contract = self.store.put_json(
                    {"module_name": name}, artifact_type="ArchitectureSkeletonModuleContractArtifact"
                )
                self.repository.dispatch(
                    ActionEnvelope(
                        action_type="CREATE_NODE_RUN",
                        workflow_id="wf_skeleton_candidate",
                        aggregate_type=AggregateType.DAG_NODE_RUN,
                        aggregate_id=node_id,
                        actor="test",
                        expected_version=0,
                        idempotency_key=f"{node_id}:create",
                        payload={
                            "unit_contract_ref": contract.to_dict(),
                            "epoch_id": "epoch_skeleton_candidate",
                            "environment_fingerprint": "env-hash",
                        },
                    )
                )
                lease = self.repository.claim_lease(f"worktree:{node_id}", f"worker_{index}", ttl_seconds=60)
                locks = WorkspaceLockRegistry()
                workspace_fingerprint = _lock_candidate_workspace(
                    locks,
                    node_id,
                    worktree,
                )
                with self.assertRaisesRegex(ValueError, error):
                    CandidateSnapshotService(self.repository, self.store, locks).create_candidate(
                        node_run_id=node_id,
                        worker_id=lease.owner_id,
                        lease_resource_key=lease.resource_key,
                        fencing_token=lease.fencing_token,
                        worktree=worktree,
                        expected_workspace_fingerprint=workspace_fingerprint,
                        reference_only_paths=[],
                        path_policy={
                            "contract_paths": ["include/contract.h"],
                            "reference_only": ["reference/api.h"],
                            "implementation_scopes": [{"kind": "directory", "path": "src"}],
                            "verification_corpus": {
                                "kind": "directory",
                                "path": "tests/module",
                            },
                        },
                        base_sha=base_sha,
                        candidate_baseline_sha=base_sha,
                        unit_contract_hash=contract.sha256,
                        dependency_output_hashes={},
                        environment_fingerprint="env-hash",
                    )
                self.assertFalse(locks.is_held(node_id))

    def _accept_node(self, node_id: str) -> None:
        dummy = self.store.put_json({"node_id": node_id}, artifact_type="TestArtifact")
        initial = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
        worktree = Path(str(initial.payload["workspace_path"]))
        unit_id = str(initial.payload.get("unit_id") or "module")
        candidate_path = worktree / "src" / unit_id / "candidate.txt"
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_path.write_text(f"candidate for {unit_id}\n", encoding="utf-8")
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
        candidate_digest = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=worktree, text=True).strip()
        sequence = [
            ("START_PRODUCING", {"fencing_token": 1}),
            ("SUBMIT_CANDIDATE", {"fencing_token": 1}),
            (
                "QUIESCE_COMPLETED",
                {
                    "fencing_token": 1,
                    "process_group_reaped": True,
                    "exclusive_workspace_lock": True,
                    "workspace_fingerprint": "tree",
                },
            ),
            (
                "CANDIDATE_SNAPSHOTTED",
                {
                    "candidate_ref": dummy.to_dict(),
                    "candidate_digest": candidate_digest,
                    "workspace_fingerprint": "tree",
                },
            ),
            ("START_REVIEW", {"fencing_token": 2}),
            (
                "SUBMIT_SEMANTIC_VERIFICATION",
                {"pending_verification_ref": dummy.to_dict()},
            ),
            (
                "VERIFIER_QUIESCED",
                {
                    "fencing_token": 2,
                    "process_group_reaped": True,
                    "exclusive_workspace_lock": True,
                    "workspace_fingerprint": "review-tree",
                },
            ),
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
