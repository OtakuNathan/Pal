"""Produced dependency gates through the real software Family and scheduler.

These small Python products are synthetic dependency-binding probes, not an
implementation or acceptance test of any external backup project. All graph
inputs, Git worktrees, candidate artifacts and runtime state are local fixtures.
"""
from __future__ import annotations

import copy
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from pal.bunshin.architecture_compilation import ArchitectureTemplateCompiler
from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.contract_protocol import validate_contract_payload
from pal.bunshin.contracts import ActionEnvelope, AggregateType
from pal.bunshin.cycle_protocol import AssignmentKind, CycleSlot
from pal.bunshin.dag_scheduling import DagScheduler
from pal.bunshin.dependency_baselines import prepare_node_verification_baseline
from pal.bunshin.graph_compiler import GraphCompileBindings, GraphCompiler
from pal.bunshin.graph_executor import FindingClass
from pal.bunshin.graph_protocol import EdgeKind, RoleBinding
from pal.bunshin.graph_satellites import FamilyGraphSatelliteProjector
from pal.bunshin.repository import BunshinV2Repository
from pal.bunshin.semantic_orchestration.verification_policy import _verification_repair_scope
from pal.bunshin.skeleton_compilation import SkeletonEpochCompiler
from pal.bunshin.swe_verification import verification_finding_route_errors
from pal.bunshin.workflow_runtime import WorkflowCoordinator


# A diamond gives the transitive provider two paths to inventory. The sink
# declares only inventory, but must check against all produced modules.
_DEPENDENCIES = {
    "manifest_model": (),
    "archive_verify": ("manifest_model",),
    "inventory": ("manifest_model", "archive_verify"),
    "backup_cli": ("inventory",),
}
_IMPLEMENTATIONS = {
    "manifest_model": "from types import SimpleNamespace\ndef run():\n    return SimpleNamespace(entries=('a', 'b'))\n",
    "archive_verify": "from manifest_model import run as manifest\ndef run():\n    return len(manifest().entries)\n",
    "inventory": "from manifest_model import run as manifest\nfrom archive_verify import run as verify\ndef run():\n    return len(manifest().entries) + verify()\n",
    "backup_cli": "from inventory import run as inventory\ndef run():\n    return inventory()\n",
}


def _compile_graph():
    definition = ArchitectureTemplateCompiler().compile("software_engineering.v1")
    payload = copy.deepcopy(definition.example)
    template = payload["modules"]["decoder"]
    modules = {}
    for name, providers in _DEPENDENCIES.items():
        module = copy.deepcopy(template)
        module["responsibility"] = f"Own the synthetic {name} product."
        module["dependencies"] = {
            provider: {
                "consumes": ["decoded_frames"],
                "purpose": f"Consume the accepted {provider} product.",
                "handoff": "Read the product through its declared interface.",
            }
            for provider in providers
        }
        module["definition"]["paths"] = {
            "contract_mode": "review_guarded",
            "contract_paths": [f"{name}.py"],
            "implementation_scopes": [{"kind": "file", "path": f"{name}.py"}],
            "reference_only": [],
        }
        modules[name] = module
    errors = copy.deepcopy(template)
    errors["execution"] = "contract_only"
    errors["responsibility"] = "Declare shared errors without a produced product."
    errors["dependencies"] = {}
    errors["definition"]["paths"] = {
        "contract_mode": "file_frozen",
        "contract_paths": ["errors.py"],
        "implementation_scopes": [],
        "reference_only": [],
    }
    modules["errors"] = errors
    modules["archive_verify"]["dependencies"]["errors"] = {
        "consumes": ["decoded_frames"],
        "purpose": "Use the shared error contract.",
        "handoff": "Shared declared exception type.",
    }
    payload["modules"] = modules
    payload["graph"]["sink"] = "backup_cli"
    payload["context"]["build_system"]["owner"] = "backup_cli"
    payload["requirements"]["decode_frames"]["owner"] = "manifest_model"
    payload["requirements"]["decode_frames"]["contract_path"] = ["manifest_model.decoded_frames"]
    scenario = payload["scenarios"]["decode_one_frame"]
    scenario["modules"] = list(_DEPENDENCIES)
    scenario["entrypoint"]["module"] = "backup_cli"
    return GraphCompiler().compile(
        validate_contract_payload(payload, definition=definition),
        graph_id="produced-gates", generation=1,
        bindings=GraphCompileBindings(
            producer=RoleBinding("profile", "coder"),
            checker=RoleBinding("profile", "verifier"),
            execution_adapter="software_git.v2",
        ),
        satellite_projector=FamilyGraphSatelliteProjector(
            specialization_id=definition.specialization_id,
            template=definition.graph_satellite_template,
        ),
        source_ref="synthetic-produced-gates.yaml",
        workspace_authority_rules=definition.workspace_authority_rules,
    )


class ProducedDependencyGateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="pal-produced-gates-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repository = BunshinV2Repository(self.root / "runtime")
        self.artifacts = ContentAddressedArtifactStore(
            self.repository.runtime_root, self.repository.artifacts,
        )
        self.coordinator = WorkflowCoordinator(self.repository)
        self.scheduler = DagScheduler(self.repository)
        self.graph = _compile_graph()
        self.workflow_id = self.graph.graph_id
        self.epoch_id = "epoch-gates"
        self.node_ids = {name: f"{self.epoch_id}:{name}" for name in self.graph.nodes}
        self.coordinator.install_graph(workflow_id=self.workflow_id, graph=self.graph)
        source = self.root / "source"
        source.mkdir()
        self._git(source, "init", "-q")
        self._git(source, "config", "user.name", "Dependency Gate Test")
        self._git(source, "config", "user.email", "test@example.invalid")
        self._git(source, "config", "commit.gpgsign", "false")
        for name in self.graph.nodes:
            (source / f"{name}.py").write_text("def run():\n    raise NotImplementedError\n")
        (source / "errors.py").write_text("class ProductError(Exception):\n    pass\n")
        self._git(source, "add", ".")
        self._git(source, "commit", "-qm", "declaration skeleton")
        self.base = self._git(source, "rev-parse", "HEAD")
        self.workspaces = {}
        for name in self.graph.nodes:
            workspace = self.root / name
            self._git(source, "worktree", "add", "--detach", str(workspace), self.base)
            self.workspaces[name] = workspace
        ref = self.artifacts.put_json({"fixture": "produced-gates"}, artifact_type="TestArtifact")
        compiler = SkeletonEpochCompiler(self.repository, None)
        compiler.publish_epoch(
            "test", {"skeleton_commit_sha": self.base}, self.epoch_id,
            self.graph, ref, ref, self.workflow_id,
        )
        compiler.create_node_runs(
            workflow_id=self.workflow_id, epoch_id=self.epoch_id, actor="test",
            graph=self.graph, unit_node_ids=self.node_ids,
            module_responsibilities={name: node.responsibility for name, node in self.graph.nodes.items()},
            module_refs={name: ref for name in self.graph.nodes}, manifest_ref=ref,
            source_role_generations={}, environment_fingerprint="fixture-only",
            initial_repair_bill_ref=None,
            workspaces={
                name: {"workspace_path": str(path), "base_sha": self.base,
                       "execution_adapter": "software_git.v2"}
                for name, path in self.workspaces.items()
            },
        )
        self._dispatch(
            AggregateType.EXECUTION_EPOCH, self.epoch_id, "NODES_COMPILED",
            {"node_ids": list(self.node_ids.values())},
        )

    @staticmethod
    def _git(workspace, *args):
        return subprocess.run(
            ["git", "-C", str(workspace), *args], check=True,
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()

    def _node(self, name):
        return self.repository.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, self.node_ids[name])

    def _dispatch(self, kind, identity, action, payload):
        current = self.repository.snapshots.read_snapshot(kind, identity)
        return self.repository.transitions.dispatch(ActionEnvelope(
            action_type=action, workflow_id=self.workflow_id,
            aggregate_type=kind, aggregate_id=identity, actor="test",
            expected_version=current.version,
            idempotency_key=f"{identity}:{action}:{current.version}", payload=payload,
        )).snapshot

    def _node_action(self, name, action, payload):
        return self._dispatch(AggregateType.DAG_NODE_RUN, self.node_ids[name], action, payload)

    def _schedule(self):
        return self.scheduler.schedule_ready_nodes(workflow_id=self.workflow_id, epoch_id=self.epoch_id)

    def _submit_all_producers(self):
        self.assertEqual(set(self._schedule()), set(self.node_ids.values()))
        for name, implementation in _IMPLEMENTATIONS.items():
            self.coordinator.start_assignment(
                workflow_id=self.workflow_id, node_name=name, slot=CycleSlot.PRODUCER,
                kind=AssignmentKind.INITIAL, input_fingerprint=f"{name}:producer",
            )
            workspace = self.workspaces[name]
            (workspace / f"{name}.py").write_text(implementation)
            self._git(workspace, "add", ".")
            self._git(workspace, "commit", "-qm", f"implement {name}")
            digest = self._git(workspace, "rev-parse", "HEAD")
            ref = self.artifacts.put_json({
                "candidate_digest": digest, "base_sha": self.base,
                "previous_head_sha": self.base,
                "candidate_tree_sha": self._git(workspace, "rev-parse", "HEAD^{tree}"),
                "changed_paths": [f"{name}.py"],
            }, artifact_type="GitCheckpointArtifact")
            for action, payload in (
                ("START_PRODUCING", {"fencing_token": 1}),
                ("SUBMIT_CANDIDATE", {"fencing_token": 1}),
                ("QUIESCE_COMPLETED", {"fencing_token": 1, "process_group_reaped": True,
                                       "exclusive_workspace_lock": True, "workspace_fingerprint": digest}),
                ("CANDIDATE_SNAPSHOTTED", {"candidate_ref": ref.to_dict(), "candidate_digest": digest,
                                          "workspace_fingerprint": digest}),
            ):
                self._node_action(name, action, payload)
            self.coordinator.producer_submitted(
                workflow_id=self.workflow_id, node_name=name, product_ref=ref.sha256,
            )

    def _accept(self, name):
        self.coordinator.start_assignment(
            workflow_id=self.workflow_id, node_name=name, slot=CycleSlot.CHECKER,
            kind=AssignmentKind.INITIAL, input_fingerprint=f"{name}:checker",
        )
        report = self.artifacts.put_json({"status": "PASS"}, artifact_type="VerificationArtifact")
        for action, payload in (
            ("START_REVIEW", {"fencing_token": 2}),
            ("SUBMIT_SEMANTIC_VERIFICATION", {"pending_verification_ref": report.to_dict()}),
            ("VERIFIER_QUIESCED", {"fencing_token": 2, "process_group_reaped": True,
                                   "exclusive_workspace_lock": True, "workspace_fingerprint": "checked"}),
            ("REVIEW_PASSED", {"verification_artifact_ref": report.to_dict()}),
        ):
            self._node_action(name, action, payload)
        self.coordinator.checker_verdict(workflow_id=self.workflow_id, node_name=name, accepted=True)
        return self._node(name)

    def _probe(self, name, expected):
        result = subprocess.run(
            [sys.executable, "-B", "-c", f"from {name} import run; assert run() == {expected!r}"],
            cwd=self.workspaces[name], capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_compiled_edges_gate_checkers_while_all_coders_are_initially_runnable(self):
        self.assertTrue(all(edge.kind == EdgeKind.EXECUTION for edge in self.graph.edges))
        self.assertEqual(set(self.graph.nodes), set(_DEPENDENCIES))
        for name, providers in _DEPENDENCIES.items():
            expected = set(providers) if name != self.graph.sink else set(self.graph.nodes) - {name}
            with self.subTest(node=name):
                self.assertEqual(self.graph.producer_predecessors(name), ())
                self.assertEqual(set(self.graph.checker_predecessors(name)), expected)
                node = self._node(name)
                self.assertEqual(set(node.payload["dependency_node_ids"]), {self.node_ids[x] for x in expected})
                self.assertEqual(node.payload["producer_dependency_node_ids"], [])
                self.assertEqual(node.payload["contract_dependency_node_ids"], [])
        self.assertEqual(set(self._schedule()), set(self.node_ids.values()))

    def test_non_sink_waits_for_accepted_direct_provider_and_binds_its_product(self):
        self._submit_all_producers()
        original = self._node("archive_verify")
        self.assertEqual(original.state, "REVIEW_BLOCKED_BY_DEPS")
        self.assertEqual(self._schedule(), (self.node_ids["manifest_model"],))
        self.assertEqual(self._node("archive_verify"), original)
        self.assertIn("raise NotImplementedError", (self.workspaces["archive_verify"] / "manifest_model.py").read_text())
        self.assertEqual(self._schedule(), ())  # Submitted/checker-ready is not acceptance.
        with self.assertRaisesRegex(ValueError, "dependency is not accepted"):
            prepare_node_verification_baseline(
                original, {self.node_ids[name]: self._node(name) for name in self.graph.nodes},
                artifacts=self.artifacts,
            )
        self.assertEqual(
            self._git(self.workspaces["archive_verify"], "rev-parse", "HEAD"),
            original.payload["candidate_digest"],
        )
        provider = self._accept("manifest_model")
        self.assertEqual(self._schedule(), (self.node_ids["archive_verify"],))
        self.assertEqual(self._node("inventory").state, "REVIEW_BLOCKED_BY_DEPS")
        bound = self._node("archive_verify")
        self.assertEqual(bound.state, "REVIEW_QUEUED")
        self.assertEqual(bound.payload["dependency_outputs"][provider.aggregate_id]["candidate_ref"], provider.payload["candidate_ref"])
        self.assertEqual(bound.payload["dependency_outputs"][provider.aggregate_id]["candidate_digest"], provider.payload["candidate_digest"])
        self.assertEqual(bound.payload["accepted_dependency_candidate_digests"], [provider.payload["candidate_digest"]])
        self.assertEqual(bound.payload["implementation_candidate_ref"], original.payload["candidate_ref"])
        self.assertNotEqual(bound.payload["candidate_digest"], original.payload["candidate_digest"])
        artifact = self.artifacts.read_json(bound.payload["candidate_ref"])
        self.assertEqual(artifact["assembly_boundary"], "verification")
        self.assertEqual(set(artifact["changed_paths"]), {"manifest_model.py", "archive_verify.py"})
        self._probe("archive_verify", 2)
        scope = _verification_repair_scope(self.repository, bound)
        self.assertEqual(scope["dependency_modules"], ["manifest_model"])
        self.assertEqual(verification_finding_route_errors([{
            "finding_kind": "dependency_defect", "locations": [
                {"scope": "workspace", "file": "manifest_model.py", "line": 1},
            ],
        }], scope), [])

    def test_transitive_diamond_applies_each_delta_once_and_replay_is_idempotent(self):
        self._submit_all_producers()
        self._schedule()
        self._accept("manifest_model")
        self._schedule()
        self._accept("archive_verify")
        self.assertEqual(self._schedule(), (self.node_ids["inventory"],))
        node = self._node("inventory")
        providers = [self._node(name) for name in ("manifest_model", "archive_verify")]
        self.assertEqual(node.payload["accepted_dependency_candidate_digests"], [x.payload["candidate_digest"] for x in providers])
        self.assertEqual(set(node.payload["dependency_outputs"]), {x.aggregate_id for x in providers})
        for provider in providers:
            self.assertEqual(node.payload["dependency_outputs"][provider.aggregate_id]["candidate_ref"], provider.payload["candidate_ref"])
        workspace = self.workspaces["inventory"]
        commits = self._git(workspace, "log", "--format=%s", f"{self.base}..HEAD").splitlines()
        self.assertEqual(sorted(commits), ["implement archive_verify", "implement inventory", "implement manifest_model"])
        self._probe("inventory", 4)
        head = self._git(workspace, "rev-parse", "HEAD")
        nodes = {self.node_ids[name]: self._node(name) for name in self.graph.nodes}
        # Replay once without persisted assembly outputs (crash before publish)
        # and once with the durable outputs. Neither may duplicate Git deltas.
        original = replace(node, payload={
            **node.payload, "candidate_ref": node.payload["implementation_candidate_ref"],
            "candidate_digest": node.payload["implementation_candidate_digest"],
            "dependency_outputs": {}, "verification_base_sha": "",
            "accepted_dependency_candidate_digests": [],
        })
        replay = prepare_node_verification_baseline(original, nodes, artifacts=self.artifacts)
        self.assertEqual(replay["candidate_ref"], node.payload["candidate_ref"])
        durable_replay = prepare_node_verification_baseline(node, nodes, artifacts=self.artifacts)
        self.assertEqual(durable_replay["verification_base_sha"], head)
        self.assertEqual(self._git(workspace, "rev-parse", "HEAD"), head)
        self.assertEqual(self._git(workspace, "status", "--porcelain"), "")
        self.assertEqual(self._schedule(), ())
        self.assertEqual(self._node("inventory"), node)
        self.assertEqual(self._node("backup_cli").state, "REVIEW_BLOCKED_BY_DEPS")
        self._accept("inventory")
        self.assertEqual(self._schedule(), (self.node_ids["backup_cli"],))
        sink = self._node("backup_cli")
        self.assertEqual(set(sink.payload["dependency_outputs"]), {self.node_ids[name] for name in self.graph.nodes if name != self.graph.sink})
        self._probe("backup_cli", 4)
        commits = self._git(self.workspaces["backup_cli"], "log", "--format=%s", f"{self.base}..HEAD").splitlines()
        self.assertEqual(len(commits), len(self.graph.nodes))
        self.assertEqual(len(set(commits)), len(commits))

    def test_true_contract_only_errors_have_no_vertex_gate_or_provider_repair(self):
        self.assertNotIn("errors", self.graph.nodes)
        self.assertFalse(any("errors" in (edge.producer, edge.consumer) for edge in self.graph.edges))
        self.assertIn("errors", self.graph.nodes["archive_verify"].satellite_data["architecture"]["modules"])
        execution = self.coordinator.execution(workflow_id=self.workflow_id)
        with self.assertRaises(ValueError):
            execution.route_finding(
                finding_class=FindingClass.DEPENDENCY_DEFECT,
                current_node="archive_verify", dependency_node="errors",
            )
        scope = _verification_repair_scope(self.repository, self._node("archive_verify"))
        self.assertNotIn("errors", scope["dependency_modules"])
        self.assertNotIn("errors", scope["repair_path_owners"])
        self.assertTrue(verification_finding_route_errors([{
            "finding_kind": "dependency_defect", "locations": [
                {"scope": "workspace", "file": "errors.py", "line": 1},
            ],
        }], scope))
