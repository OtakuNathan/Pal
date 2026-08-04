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
from typing import Any, Mapping

from pal.minion.v2 import ActionEnvelope, AggregateType, ContentAddressedArtifactStore, MinionV2Repository
from pal.minion.v2.contract_runtime import ContractArtifactAccess
from pal.minion.v2.catalog import MinionV2Catalog
from pal.minion.v2.adapters import (
    ARTIFACT_BUNDLE_ADAPTER,
    SOFTWARE_GIT_ADAPTER,
    ArtifactBundleAdapter,
)
from pal.minion.v2.contracts import AggregateSnapshot, AggregateVersionConflict, StaleFencingToken
from pal.minion.v2.execution import (
    CandidateSnapshotService,
    DagScheduler,
    ExecutionCompiler,
    NodeRunJournal,
    UnitWorkViewBuilder,
    WorkspaceLockRegistry,
    prepare_node_dependency_baseline,
    prepare_node_verification_baseline,
    format_workspace_process_holders,
    terminate_process_group,
    workspace_process_holders,
    workspace_content_fingerprint,
    _validate_skeleton_candidate_paths,
)
from pal.minion.v2.task_ledger import TaskLedgerService
from pal.minion.v2.graph_compiler import GraphCompileBindings, GraphCompiler
from pal.minion.v2.graph_protocol import RoleBinding
from pal.minion.v2.graph_satellites import FamilyNodeProjection
from pal.minion.v2.cycle_protocol import AssignmentKind, CycleSlot
from pal.minion.v2.workflow_runtime import WorkflowCoordinator


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


class _SelectiveTestFamilyProjector:
    """A Family projection whose node semantics include only its providers."""

    def project(
        self,
        *,
        document: Mapping[str, Any],
        node_name: str,
        node: Mapping[str, Any],
    ) -> FamilyNodeProjection:
        modules = dict(document.get("modules") or {})
        dependencies = dict(node.get("dependencies") or {})
        return FamilyNodeProjection(
            satellite_data={
                "node_name": node_name,
                "node": dict(node),
                "context": dict(document.get("context") or {}),
                "requirements": {
                    name: dict(value or {})
                    for name, value in dict(document.get("requirements") or {}).items()
                    if str(dict(value or {}).get("owner") or "") == node_name
                },
                "provider_semantics": {
                    provider: dict(modules.get(provider) or {})
                    for provider in sorted(dependencies)
                },
            },
            workspace_policy={},
        )


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
        self.contracts = ContractArtifactAccess(self.store, self.repository)

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
        return self.store.put_json(
            {
                "requirements_ref": requirements.to_dict(),
                "contract_schema": "general.v1",
                "contract": {
                    "schema_version": "2",
                    "graph": {"sink": "delivery"},
                    "context": {
                        "goal": "Implement A, then B using A.",
                        "constraints": [],
                        "assumptions": [],
                    },
                    "requirements": {
                        "a_output": {
                            "claim": "Produce A.",
                            "owner": "a",
                            "contract_path": ["a.a_output"],
                        },
                        "b_output": {
                            "claim": "Produce B using A.",
                            "owner": "b",
                            "contract_path": ["b.b_output"],
                        },
                    },
                    "modules": {
                        "a": {
                            "responsibility": "Implement a.",
                            "execution": "produce",
                            "provides": ["a_output"],
                            "dependencies": {},
                            "definition": {"deliverables": ["a_output"]},
                        },
                        "b": {
                            "responsibility": "Implement b.",
                            "execution": "produce",
                            "provides": ["b_output"],
                            "dependencies": {
                                "a": {
                                    "consumes": ["a_output"],
                                    "purpose": "Build B from A.",
                                    "handoff": "A output becomes B input.",
                                }
                            },
                            "definition": {"deliverables": ["b_output"]},
                        },
                        "delivery": {
                            "responsibility": "Deliver the composed result.",
                            "execution": "produce",
                            "provides": ["result"],
                            "dependencies": {
                                "a": {
                                    "consumes": ["a_output"],
                                    "purpose": "Include A in the delivery.",
                                    "handoff": "A output is assembled into the result.",
                                },
                                "b": {
                                    "consumes": ["b_output"],
                                    "purpose": "Include B in the delivery.",
                                    "handoff": "B output is assembled into the result.",
                                },
                            },
                            "definition": {"deliverables": ["result"]},
                        },
                    },
                    "scenarios": {
                        "compose": {
                            "modules": ["a", "b", "delivery"],
                            "requirement_refs": ["a_output", "b_output"],
                            "entrypoint": {"module": "delivery", "surface": "result"},
                            "contract_flow": ["a_output -> b -> b_output -> delivery"],
                            "observable_behavior": "B is produced from A.",
                            "failure_behavior": "Rejected A prevents B.",
                            "environment": "Artifact workspace.",
                        }
                    },
                },
            },
            artifact_type="ContractArtifact",
        )

    def _bind_workflow(self, workflow_id: str, *, profile: str = "generic") -> None:
        if self.repository.read_snapshot(AggregateType.WORKFLOW, workflow_id) is not None:
            return
        family_binding_ref = MinionV2Catalog(
            self.runtime_root,
            self.store,
        ).publish_family_binding(profile)
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_WORKFLOW",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="test",
                source_channel="test",
                expected_version=0,
                idempotency_key=f"create-workflow:{workflow_id}",
                payload={
                    "family_binding_ref": family_binding_ref.to_dict(),
                },
            )
        )

    def _compile_epoch(
        self,
        *,
        workflow_id: str,
        epoch_id: str,
        manifest_ref,
        source_epoch_id: str = "",
    ):
        self._bind_workflow(workflow_id)
        artifact = dict(self.store.read_json(manifest_ref))
        contract = dict(artifact["contract"])
        latest_graph = self.repository.read_graph_generation(
            graph_id=workflow_id
        )
        graph = GraphCompiler().compile(
            contract,
            graph_id=workflow_id,
            generation=(latest_graph.generation + 1 if latest_graph else 1),
            bindings=GraphCompileBindings(
                producer=RoleBinding("profile", "generic"),
                checker=RoleBinding("profile", "general.verifier"),
                execution_adapter=ARTIFACT_BUNDLE_ADAPTER,
            ),
            satellite_projector=_SelectiveTestFamilyProjector(),
            source_ref="architect.yaml",
        )
        manifest_ref = self.store.put_json(
            {**artifact, "graph_ir": graph.to_dict()},
            artifact_type="ContractArtifact",
        )
        return ExecutionCompiler(self.repository, self.contracts).compile_epoch(
            workflow_id=workflow_id,
            epoch_id=epoch_id,
            manifest_ref=manifest_ref,
            source_epoch_id=source_epoch_id,
        )

    def test_epoch_compilation_and_dependency_readiness(self) -> None:
        manifest = self._manifest()
        compilation = self._compile_epoch(
            workflow_id="wf_exec",
            epoch_id="epoch_1",
            manifest_ref=manifest,
        )
        scheduler = DagScheduler(self.repository)
        self.assertEqual(scheduler.schedule_ready_nodes(workflow_id="wf_exec", epoch_id="epoch_1"), (compilation.unit_node_ids["a"],))
        self._accept_node(compilation.unit_node_ids["a"])
        self.assertEqual(scheduler.schedule_ready_nodes(workflow_id="wf_exec", epoch_id="epoch_1"), (compilation.unit_node_ids["b"],))
        self._accept_node(compilation.unit_node_ids["b"])
        self.assertEqual(scheduler.schedule_ready_nodes(workflow_id="wf_exec", epoch_id="epoch_1"), (compilation.sink_node_id,))

    def test_family_binding_not_contract_schema_selects_execution_strategy(
        self,
    ) -> None:
        manifest = self._manifest()
        payload = dict(self.store.read_json(manifest))
        misleading_schema = self.store.put_json(
            {
                **payload,
                "contract_schema": "software_engineering.v1",
            },
            artifact_type="ContractArtifact",
        )

        compilation = self._compile_epoch(
            workflow_id="wf_family_strategy",
            epoch_id="epoch_family_strategy",
            manifest_ref=misleading_schema,
        )
        module = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            compilation.unit_node_ids["a"],
        )
        assert module is not None
        self.assertEqual(
            module.payload["execution_adapter"],
            ARTIFACT_BUNDLE_ADAPTER,
        )

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

    def test_software_verification_assembles_dependencies_after_coder_candidate(self) -> None:
        worktree = self.runtime_root / "verification_assembly_repo"
        worktree.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=worktree, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=worktree, check=True)
        (worktree / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=worktree, check=True)
        base_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=worktree, text=True
        ).strip()

        (worktree / "dependency.cpp").write_text("int dependency();\n", encoding="utf-8")
        subprocess.run(["git", "add", "dependency.cpp"], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-qm", "dependency"], cwd=worktree, check=True)
        dependency_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=worktree, text=True
        ).strip()
        dependency_ref = self.store.put_json(
            {"candidate_digest": dependency_sha, "base_sha": base_sha},
            artifact_type="GitCheckpointArtifact",
        )

        subprocess.run(["git", "reset", "--hard", base_sha], cwd=worktree, check=True)
        (worktree / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
        subprocess.run(["git", "add", "main.cpp"], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-qm", "consumer"], cwd=worktree, check=True)
        consumer_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=worktree, text=True
        ).strip()
        consumer_ref = self.store.put_json(
            {
                "candidate_digest": consumer_sha,
                "base_sha": base_sha,
                "previous_head_sha": base_sha,
                "candidate_tree_sha": subprocess.check_output(
                    ["git", "rev-parse", f"{consumer_sha}^{{tree}}"],
                    cwd=worktree,
                    text=True,
                ).strip(),
                "changed_paths": ["main.cpp"],
            },
            artifact_type="GitCheckpointArtifact",
        )

        dependency = AggregateSnapshot(
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="dependency",
            workflow_id="wf-assembly",
            state="ACCEPTED",
            version=1,
            payload={
                "base_sha": base_sha,
                "candidate_digest": dependency_sha,
                "candidate_ref": dependency_ref.to_dict(),
                "dependency_node_ids": [],
                "output_hashes": {},
            },
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        consumer = AggregateSnapshot(
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="consumer",
            workflow_id="wf-assembly",
            state="REVIEW_BLOCKED_BY_DEPS",
            version=1,
            payload={
                "workspace_path": str(worktree),
                "execution_adapter": SOFTWARE_GIT_ADAPTER,
                "base_sha": base_sha,
                "candidate_digest": consumer_sha,
                "candidate_ref": consumer_ref.to_dict(),
                "dependency_node_ids": [dependency.aggregate_id],
            },
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )

        assembled = prepare_node_verification_baseline(
            consumer,
            {dependency.aggregate_id: dependency, consumer.aggregate_id: consumer},
            artifacts=self.store,
        )

        self.assertTrue((worktree / "main.cpp").is_file())
        self.assertTrue((worktree / "dependency.cpp").is_file())
        self.assertEqual(
            assembled["implementation_candidate_ref"],
            consumer_ref.to_dict(),
        )
        self.assertNotEqual(assembled["candidate_digest"], consumer_sha)
        assembled_artifact = self.store.read_json(assembled["candidate_ref"])
        self.assertEqual(assembled_artifact["assembly_boundary"], "verification")
        self.assertEqual(assembled_artifact["base_sha"], base_sha)
        self.assertEqual(
            set(assembled_artifact["changed_paths"]),
            {"dependency.cpp", "main.cpp"},
        )

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
        contract = dict(payload["contract"])
        modules = {
            name: dict(module)
            for name, module in dict(contract["modules"]).items()
        }
        modules["b"]["dependencies"] = {}
        independent_manifest = self.store.put_json(
            {
                **payload,
                "contract": {**contract, "modules": modules},
            },
            artifact_type="ContractArtifact",
        )
        compilation = self._compile_epoch(
            workflow_id="wf_parallel",
            epoch_id="epoch_parallel",
            manifest_ref=independent_manifest,
        )

        queued = DagScheduler(self.repository).schedule_ready_nodes(
            workflow_id="wf_parallel",
            epoch_id=compilation.epoch_id,
        )

        self.assertEqual(
            set(queued),
            {
                compilation.unit_node_ids["a"],
                compilation.unit_node_ids["b"],
            },
        )

    def test_authored_sink_uses_declared_execution_dependencies(self) -> None:
        manifest = self._manifest()
        payload = self.store.read_json(manifest)
        contract = dict(payload["contract"])
        modules = {
            name: dict(module)
            for name, module in dict(contract["modules"]).items()
        }
        modules["a"]["dependencies"] = {
            "b": {
                "consumes": ["b_output"],
                "purpose": "Exercise reverse topological order.",
                "handoff": "B output becomes A input.",
            }
        }
        modules["b"]["dependencies"] = {}
        reordered = self.store.put_json(
            {
                **payload,
                "contract": {**contract, "modules": modules},
            },
            artifact_type="ContractArtifact",
        )
        compilation = self._compile_epoch(
            workflow_id="wf_topological_merge",
            epoch_id="epoch_topological_merge",
            manifest_ref=reordered,
        )
        sink = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN, compilation.sink_node_id
        )
        assert sink is not None
        self.assertEqual(
            set(sink.payload["dependency_node_ids"]),
            {
                compilation.unit_node_ids["a"],
                compilation.unit_node_ids["b"],
            },
        )

    def test_replan_reuses_only_exactly_matching_accepted_candidates(self) -> None:
        manifest = self._manifest()
        first = self._compile_epoch(
            workflow_id="wf_reuse",
            epoch_id="epoch_reuse_1",
            manifest_ref=manifest,
        )
        scheduler = DagScheduler(self.repository)
        scheduler.schedule_ready_nodes(workflow_id="wf_reuse", epoch_id=first.epoch_id)
        self._accept_node(first.unit_node_ids["a"])
        scheduler.schedule_ready_nodes(workflow_id="wf_reuse", epoch_id=first.epoch_id)
        self._accept_node(first.unit_node_ids["b"])

        second = self._compile_epoch(
            workflow_id="wf_reuse",
            epoch_id="epoch_reuse_2",
            manifest_ref=manifest,
            source_epoch_id=first.epoch_id,
        )
        first_a = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, first.unit_node_ids["a"])
        second_a = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, second.unit_node_ids["a"])
        self.assertEqual(first_a.payload["environment_fingerprint"], second_a.payload["environment_fingerprint"])
        for unit_id in ("a", "b"):
            source = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, first.unit_node_ids[unit_id])
            reused = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, second.unit_node_ids[unit_id])
            self.assertEqual(reused.state, "ACCEPTED")
            self.assertEqual(reused.payload["candidate_digest"], source.payload["candidate_digest"])
            self.assertEqual(reused.payload["carried_forward_from_epoch_id"], first.epoch_id)
            self.assertEqual(reused.payload["workspace_path"], source.payload["workspace_path"])

        changed_payload = self.store.read_json(manifest)
        changed_contract = dict(changed_payload["contract"])
        changed_manifest = self.store.put_json(
            {
                **changed_payload,
                "contract": {
                    **changed_contract,
                    "context": {
                        **dict(changed_contract["context"]),
                        "constraints": ["new global constraint"],
                    },
                },
            },
            artifact_type="ContractArtifact",
        )
        third = self._compile_epoch(
            workflow_id="wf_reuse",
            epoch_id="epoch_reuse_3",
            manifest_ref=changed_manifest,
            source_epoch_id=first.epoch_id,
        )
        self.assertEqual(
            self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, third.unit_node_ids["a"]).state,
            "BLOCKED_BY_DEPS",
        )

    def test_replan_contract_only_dependency_change_requeues_consumer(self) -> None:
        """A GraphDiff stale decision must beat an unchanged node projection.

        ``catalog`` has no executable node, so the aggregate projection for
        ``b`` cannot observe it through dependency node ids.  Its changed
        contract is nevertheless part of ``b``'s GraphIR product boundary.
        Replan must preserve B's workspace but re-run B rather than carrying
        an accepted candidate that was verified against the old catalog.
        """

        original = self._manifest()
        original_payload = dict(self.store.read_json(original))
        original_contract = dict(original_payload["contract"])
        original_modules = {
            name: dict(module)
            for name, module in dict(original_contract["modules"]).items()
        }
        original_modules["catalog"] = {
            "responsibility": "Define the protocol catalog.",
            "execution": "contract_only",
            "provides": ["catalog_rules"],
            "dependencies": {},
            "definition": {"protocol_version": "v1"},
        }
        original_modules["b"] = {
            **original_modules["b"],
            "dependencies": {
                **dict(original_modules["b"]["dependencies"]),
                "catalog": {
                    "consumes": ["catalog_rules"],
                    "purpose": "Use the declared protocol catalog.",
                    "handoff": "Catalog rules constrain B.",
                },
            },
        }
        first_manifest = self.store.put_json(
            {
                **original_payload,
                "contract": {
                    **original_contract,
                    "modules": original_modules,
                },
            },
            artifact_type="ContractArtifact",
        )
        first = self._compile_epoch(
            workflow_id="wf_contract_only_replan",
            epoch_id="epoch_contract_only_1",
            manifest_ref=first_manifest,
        )
        scheduler = DagScheduler(self.repository)
        scheduler.schedule_ready_nodes(
            workflow_id="wf_contract_only_replan",
            epoch_id=first.epoch_id,
        )
        self._accept_node(first.unit_node_ids["a"])
        scheduler.schedule_ready_nodes(
            workflow_id="wf_contract_only_replan",
            epoch_id=first.epoch_id,
        )
        self._accept_node(first.unit_node_ids["b"])

        revised_payload = dict(self.store.read_json(first_manifest))
        revised_contract = dict(revised_payload["contract"])
        revised_modules = {
            name: dict(module)
            for name, module in dict(revised_contract["modules"]).items()
        }
        revised_modules["catalog"] = {
            **revised_modules["catalog"],
            "definition": {"protocol_version": "v2"},
        }
        revised_manifest = self.store.put_json(
            {
                **revised_payload,
                "contract": {
                    **revised_contract,
                    "modules": revised_modules,
                },
            },
            artifact_type="ContractArtifact",
        )
        replanned = self._compile_epoch(
            workflow_id="wf_contract_only_replan",
            epoch_id="epoch_contract_only_2",
            manifest_ref=revised_manifest,
            source_epoch_id=first.epoch_id,
        )

        carried_a = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            replanned.unit_node_ids["a"],
        )
        stale_b = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            replanned.unit_node_ids["b"],
        )
        assert carried_a is not None
        assert stale_b is not None
        self.assertEqual(carried_a.state, "ACCEPTED")
        self.assertEqual(stale_b.state, "BLOCKED_BY_DEPS")
        self.assertEqual(
            scheduler.schedule_ready_nodes(
                workflow_id="wf_contract_only_replan",
                epoch_id=replanned.epoch_id,
            ),
            (replanned.unit_node_ids["b"],),
        )

    def test_unit_work_view_contains_only_module_local_semantics(self) -> None:
        manifest = self._manifest()
        compilation = self._compile_epoch(
            workflow_id="wf_view",
            epoch_id="epoch_view",
            manifest_ref=manifest,
        )
        node = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, compilation.unit_node_ids["a"])
        view_ref = UnitWorkViewBuilder(self.contracts).build(node)
        view = self.store.read_json(view_ref)
        self.assertNotIn("evidence", view)
        self.assertEqual(view["module_name"], "a")
        self.assertEqual(view["module"]["responsibility"], "Implement a.")
        self.assertEqual(set(view["requirements"]), {"a_output", "b_output"})
        self.assertEqual(set(view["scenarios"]), {"compose"})
        self.assertEqual(
            view["entrypoints"],
            [{"module": "delivery", "surface": "result"}],
        )
        self.assertEqual(view["context"]["constraints"], [])

        consumer = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            compilation.unit_node_ids["b"],
        )
        assert consumer is not None
        consumer_view = self.store.read_json(
            UnitWorkViewBuilder(self.contracts).build(consumer)
        )
        self.assertEqual(
            consumer_view["dependency_contracts"]["a"]["handoff"]["handoff"],
            "A output becomes B input.",
        )

    def test_work_view_entrypoint_accepts_string_projection(self) -> None:
        # SWE's compact skeleton projection stores scenario entrypoints as
        # strings; the richer contract form remains a structured mapping.
        self.assertEqual(
            UnitWorkViewBuilder._entrypoint_view("framepipe decode"),
            {"target": "framepipe decode"},
        )
        self.assertEqual(
            UnitWorkViewBuilder._entrypoint_view(
                {"module": "framepipe_app", "surface": "decode"}
            ),
            {"module": "framepipe_app", "surface": "decode"},
        )

    def test_sink_work_view_receives_the_complete_scenario_graph(self) -> None:
        manifest = self._manifest()
        artifact = self.store.read_json(manifest)
        contract = dict(artifact["contract"])
        contract["scenarios"] = {
            **dict(contract["scenarios"]),
            "library_flow": {
                "modules": ["a", "b"],
                "requirement_refs": ["a_output", "b_output"],
                "entrypoint": {"module": "b", "surface": "b_output"},
                "contract_flow": ["a_output -> b -> b_output"],
                "observable_behavior": "Library consumers obtain B from A.",
                "failure_behavior": "Rejected A prevents B.",
                "environment": "Library consumer process.",
            },
        }
        manifest = self.store.put_json(
            {**artifact, "contract": contract},
            artifact_type="ContractArtifact",
        )
        compilation = self._compile_epoch(
            workflow_id="wf_sink_view",
            epoch_id="epoch_sink_view",
            manifest_ref=manifest,
        )
        sink = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            compilation.sink_node_id,
        )
        assert sink is not None
        view = self.store.read_json(
            UnitWorkViewBuilder(self.contracts).build(sink)
        )
        self.assertEqual(set(view["requirements"]), {"a_output", "b_output"})
        self.assertEqual(set(view["scenarios"]), {"compose", "library_flow"})
        self.assertIn(
            {"module": "b", "surface": "b_output"},
            view["entrypoints"],
        )

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
        initial = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            node_id,
        )
        module_name = str(
            initial.payload.get("module_name")
            or initial.payload.get("unit_id")
            or ""
        )
        coordinator = WorkflowCoordinator(self.repository)
        coordinator.start_assignment(
            workflow_id=initial.workflow_id,
            node_name=module_name,
            slot=CycleSlot.PRODUCER,
            kind=AssignmentKind.INITIAL,
            input_fingerprint=f"{node_id}:producer",
        )
        dummy = self.store.put_json({"node_id": node_id}, artifact_type="TestArtifact")
        initial = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
        worktree = Path(str(initial.payload["workspace_path"]))
        unit_id = str(initial.payload.get("unit_id") or "module")
        candidate_path = worktree / "src" / unit_id / "candidate.txt"
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_path.write_text(f"candidate for {unit_id}\n", encoding="utf-8")
        if (
            str(initial.payload.get("execution_adapter") or "")
            == ARTIFACT_BUNDLE_ADAPTER
        ):
            candidate_ref, candidate_digest = ArtifactBundleAdapter(
                self.runtime_root,
                self.store,
            ).snapshot_candidate(
                workspace=worktree,
                reference_only_paths=(),
                unit_contract_hash=str(
                    dict(initial.payload.get("unit_contract_ref") or {}).get(
                        "sha256"
                    )
                    or ""
                ),
                dependency_output_hashes={},
                environment_fingerprint=str(
                    initial.payload.get("environment_fingerprint") or ""
                ),
            )
        else:
            subprocess.run(
                ["git", "add", str(candidate_path.relative_to(worktree))],
                cwd=worktree,
                check=True,
            )
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
            candidate_digest = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree,
                text=True,
            ).strip()
            candidate_ref = dummy
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
                    "candidate_ref": candidate_ref.to_dict(),
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
            if action_type == "CANDIDATE_SNAPSHOTTED":
                coordinator.producer_submitted(
                    workflow_id=snapshot.workflow_id,
                    node_name=module_name,
                    product_ref=candidate_ref.sha256,
                )
                DagScheduler(self.repository).schedule_ready_nodes(
                    workflow_id=snapshot.workflow_id,
                    epoch_id=str(snapshot.payload.get("epoch_id") or ""),
                )
            elif action_type == "START_REVIEW":
                coordinator.start_assignment(
                    workflow_id=snapshot.workflow_id,
                    node_name=module_name,
                    slot=CycleSlot.CHECKER,
                    kind=AssignmentKind.INITIAL,
                    input_fingerprint=f"{node_id}:checker",
                )
            elif action_type == "REVIEW_PASSED":
                coordinator.checker_verdict(
                    workflow_id=snapshot.workflow_id,
                    node_name=module_name,
                    accepted=True,
                )


if __name__ == "__main__":
    unittest.main()
