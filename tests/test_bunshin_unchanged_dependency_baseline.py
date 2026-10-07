"""Existing repository products can be accepted without manufacturing a commit.

These are offline Git/outbox regressions. The compiled software graph, epoch,
leases, checkpoint snapshotter, acceptance transitions, and checker admission
are real. Deterministic Python probes stand in for role execution; no provider
or live workflow is contacted.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from pal.bunshin.workflow_catalog import BunshinWorkflowCatalog
from pal.bunshin.contracts import ActionEnvelope, AggregateType
from pal.bunshin.cycle_protocol import CycleSlot
from pal.bunshin.dag_scheduling import DagScheduler
from pal.bunshin.dependency_baselines import prepare_node_verification_baseline
from pal.bunshin.execution_values import workspace_content_fingerprint
from pal.bunshin.orchestration import BunshinOutboxProcessor
from pal.bunshin.semantic_orchestration.orchestrator import SemanticOrchestrator
from pal.bunshin.service import BunshinWorkflowService
from pal.bunshin.skeleton_compilation import SkeletonEpochCompiler
from pal.bunshin.verification import VerificationService, VerificationStatus
from pal.bunshin.workflow_runtime import WorkflowCoordinator
from tests import test_bunshin_produced_dependency_gates as gates
from tests import test_bunshin_dependency_repair_runtime as runtime


EMPTY_DELTA_SHA = hashlib.sha256(b"").hexdigest()


class ExistingRepositoryCase:
    """Reuse the produced-gates graph and runtime receipt assertions."""

    git = staticmethod(gates.ProducedDependencyGateTests._git)
    assert_receipt = runtime.RuntimeCase.assert_receipt

    def __init__(self, root: Path, *, all_baseline_products=False):
        self.root = root
        self.service = BunshinWorkflowService(root / "runtime")
        self.repository, self.artifacts = self.service.repository, self.service.artifacts
        self.coordinator = WorkflowCoordinator(self.repository)
        self.scheduler = DagScheduler(self.repository)
        self.worker = SemanticOrchestrator(self.service)
        self.processor = BunshinOutboxProcessor(
            self.service, semantic_effects=self.worker, worker_id="unchanged-regression",
        )
        self.graph = gates._compile_graph()
        self.workflow_id = self.graph.graph_id
        self.epoch_id = "epoch-existing-repository"
        self.node_ids = {name: f"{self.epoch_id}:{name}" for name in self.graph.nodes}
        self.claimed = {}
        binding = BunshinWorkflowCatalog(self.service.runtime_root, self.artifacts).publish_family_binding(
            "software_engineering.v2_coder",
        )
        self.dispatch(AggregateType.WORKFLOW, self.workflow_id, "CREATE_WORKFLOW", {
            "family_binding_ref": binding.to_dict(),
        })
        self.dispatch(AggregateType.WORKFLOW, self.workflow_id, "START_WORKFLOW")
        self.coordinator.install_graph(workflow_id=self.workflow_id, graph=self.graph)

        self.source = root / "existing-repo"
        self.source.mkdir()
        self.git(self.source, "init", "-q")
        self.git(self.source, "config", "user.name", "Unchanged Dependency Test")
        self.git(self.source, "config", "user.email", "test@example.invalid")
        self.git(self.source, "config", "commit.gpgsign", "false")
        for name in self.graph.nodes:
            implementation = (
                gates._IMPLEMENTATIONS[name] if name == "manifest_model" or all_baseline_products
                else "def run():\n    raise NotImplementedError\n"
            )
            (self.source / f"{name}.py").write_text(implementation)
        (self.source / "errors.py").write_text("class ProductError(Exception):\n    pass\n")
        self.obsolete_test = Path("tests/manifest_model/developer/test_obsolete.py")
        (self.source / self.obsolete_test).parent.mkdir(parents=True)
        (self.source / self.obsolete_test).write_text("def test_obsolete():\n    assert True\n")
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "-qm", "existing functioning manifest product")
        self.base = self.git(self.source, "rev-parse", "HEAD")
        self.workspaces = {}
        for name in self.graph.nodes:
            workspace = root / name
            self.git(self.source, "worktree", "add", "--detach", str(workspace), self.base)
            self.workspaces[name] = workspace
        manifest = self.artifacts.put_json(
            {"skeleton_commit_sha": self.base}, artifact_type="ArchitectureManifestArtifact",
        )
        contracts = {
            name: self.artifacts.put_json(
                {"module_name": name, "contract_hash": spec.contract_hash},
                artifact_type="UnitContractArtifact",
            ) for name, spec in self.graph.nodes.items()
        }
        compiler = SkeletonEpochCompiler(self.repository, None)
        compiler.publish_epoch(
            "test", {"skeleton_commit_sha": self.base}, self.epoch_id,
            self.graph, manifest, manifest, self.workflow_id,
        )
        compiler.create_node_runs(
            workflow_id=self.workflow_id, epoch_id=self.epoch_id, actor="test",
            graph=self.graph, unit_node_ids=self.node_ids,
            module_responsibilities={name: spec.responsibility for name, spec in self.graph.nodes.items()},
            module_refs=contracts, manifest_ref=manifest, source_role_generations={},
            environment_fingerprint="existing-repository-fixture", initial_repair_bill_ref=None,
            workspaces={
                name: {"workspace_path": str(path), "base_sha": self.base,
                       "execution_adapter": "software_git.v2"}
                for name, path in self.workspaces.items()
            },
        )
        self.dispatch(AggregateType.EXECUTION_EPOCH, self.epoch_id, "NODES_COMPILED", {
            "node_ids": list(self.node_ids.values()),
        })
        self.dispatch(AggregateType.WORKFLOW, self.workflow_id, "LINK_EXECUTION_EPOCH", {
            "execution_epoch_id": self.epoch_id,
        })

    def node(self, name="manifest_model"):
        return self.repository.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, self.node_ids[name])

    def workflow(self):
        return self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, self.workflow_id)

    def nodes(self):
        return {self.node_ids[name]: self.node(name) for name in self.graph.nodes}

    def dispatch(self, kind, identity, action, payload=None):
        current = self.repository.snapshots.read_snapshot(kind, identity)
        version = current.version if current else 0
        return self.repository.transitions.dispatch(ActionEnvelope(
            action_type=action, workflow_id=self.workflow_id, aggregate_type=kind,
            aggregate_id=identity, actor="unchanged-regression", expected_version=version,
            idempotency_key=f"{identity}:{action}:{version}", payload=payload or {},
        )).snapshot

    def node_action(self, name, action, payload=None):
        return self.dispatch(AggregateType.DAG_NODE_RUN, self.node_ids[name], action, payload)

    def stored(self, identity, effect_type):
        with self.repository.database.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM bunshin_v2_outbox WHERE aggregate_id = ? AND effect_type = ? "
                "ORDER BY rowid DESC LIMIT 1", (identity, effect_type),
            ).fetchone()
        assert row is not None, (identity, effect_type)
        effect = dict(row)
        effect["payload"] = json.loads(effect.pop("payload_json"))
        return effect

    def claim(self, identity, effect_type):
        stored = self.stored(identity, effect_type)
        for effect in self.repository.outbox_claims.claim_outbox(
            self.processor.worker_id, limit=1000, lease_seconds=120,
        ):
            self.claimed[effect["effect_id"]] = effect
        effect = self.claimed[stored["effect_id"]]
        assert effect["status"] == "inflight"
        assert effect["payload"]["_causal_context"]
        return effect

    async def process(self, identity, effect_type):
        effect = self.claim(identity, effect_type)
        result = await self.processor._process_effect(effect)
        assert result == "completed", self.stored(identity, effect_type)["last_error"]
        self.assert_receipt(effect)
        return effect

    async def submit_producers(self, *, delete_provider_test=False):
        assert set(self.scheduler.schedule_ready_nodes(
            workflow_id=self.workflow_id, epoch_id=self.epoch_id,
        )) == set(self.node_ids.values())
        for name, implementation in gates._IMPLEMENTATIONS.items():
            await self.process(self.node_ids[name], "admit_implementation_role")
            node = self.node(name)
            workspace = self.workspaces[name]
            (workspace / f"{name}.py").write_text(implementation)
            if name == "manifest_model" and delete_provider_test:
                (workspace / self.obsolete_test).unlink()
            token = node.payload["fencing_token"]
            self.node_action(name, "SUBMIT_CANDIDATE", {"fencing_token": token})
            # No worker process was launched. Hold the actual snapshot lock and
            # capture the actual fingerprint at the deterministic quiescence cut.
            self.worker.workspace_locks.acquire(node.aggregate_id, workspace)
            self.node_action(name, "QUIESCE_COMPLETED", {
                "fencing_token": token, "process_group_reaped": True,
                "exclusive_workspace_lock": True,
                "workspace_fingerprint": workspace_content_fingerprint(workspace),
            })
            await self.process(self.node_ids[name], "snapshot_implementation_result")
        if not delete_provider_test:
            assert self.node().payload["candidate_digest"] == self.base
            assert self.git(self.workspaces["manifest_model"], "rev-list", "--count", "HEAD") == "1"
        assert self.scheduler.schedule_ready_nodes(
            workflow_id=self.workflow_id, epoch_id=self.epoch_id,
        ) == (self.node_ids["manifest_model"],)

    def probe(self, name, expression):
        completed = subprocess.run(
            [sys.executable, "-B", "-c", f"from {name} import run; assert {expression}"],
            cwd=self.workspaces[name], text=True, capture_output=True, timeout=30,
        )
        assert completed.returncode == 0, completed.stderr

    async def accept_provider(self, name="manifest_model"):
        await self.process(self.node_ids[name], "admit_verifier_role")
        node = self.node(name)
        expression = "run().entries == ('a', 'b')" if name == "manifest_model" else (
            "run() == 2" if name == "archive_verify" else "run() == 4"
        )
        self.probe(name, expression)
        report = self.artifacts.put_json({
            "status": "PASS", "candidate_ref": node.payload["candidate_ref"],
            "candidate_digest": node.payload["candidate_digest"],
            "evidence": f"Executed {name}: {expression}",
        }, artifact_type="VerificationArtifact")
        self.node_action(name, "SUBMIT_SEMANTIC_VERIFICATION", {
            "pending_verification_ref": report.to_dict(),
        })
        self.node_action(name, "VERIFIER_QUIESCED", {
            "fencing_token": node.payload["fencing_token"], "process_group_reaped": True,
            "exclusive_workspace_lock": True,
            "workspace_fingerprint": workspace_content_fingerprint(self.workspaces[name]),
        })
        VerificationService(self.repository, self.artifacts).submit_verdict(
            node=self.node(name), verification_ref=report, status=VerificationStatus.PASS,
            actor="deterministic-checker",
        )
        self.coordinator.checker_verdict(
            workflow_id=self.workflow_id, node_name=name, accepted=True,
        )
        self.repository.leases.release_lease(
            node.payload["lease_resource_key"], node.payload["active_worker_id"],
            node.payload["fencing_token"],
        )
        assert self.node(name).state == "ACCEPTED"
        return self.node(name)

    async def ready(self, *, delete_provider_test=False):
        await self.submit_producers(delete_provider_test=delete_provider_test)
        return await self.accept_provider()


@pytest.fixture
def case(tmp_path, request):
    value = ExistingRepositoryCase(tmp_path, all_baseline_products=getattr(request, "param", False))
    yield value
    for node_id in value.node_ids.values():
        value.worker.workspace_locks.release(node_id)
        value.worker.workspace_locks.release("verification:" + node_id)


def test_unchanged_checkpoint_unblocks_claimed_notification_and_checker_replay(case):
    async def run():
        provider = await case.ready()
        provider_ref = provider.payload["candidate_ref"]
        candidate = case.artifacts.read_json(provider_ref)
        assert provider_ref["artifact_type"] == "GitCheckpointArtifact"
        assert provider_ref["durable"] is True
        assert candidate["schema_version"] == "3"
        assert candidate["node_run_id"] == provider.aggregate_id
        assert candidate["unit_contract_hash"] == provider.payload["unit_contract_ref"]["sha256"]
        assert candidate["environment_fingerprint"] == provider.payload["environment_fingerprint"]
        assert candidate["candidate_digest"] == candidate["base_sha"] == case.base
        assert candidate["architecture_base_sha"] == case.base
        assert candidate["baseline_tree_sha"] == candidate["candidate_tree_sha"]
        assert candidate["changed_paths"] == []
        assert candidate["delta_patch_sha"] == EMPTY_DELTA_SHA
        original = case.node("archive_verify")
        assert original.state == "REVIEW_BLOCKED_BY_DEPS"
        head = case.git(case.workspaces["archive_verify"], "rev-parse", "HEAD")
        commits = case.git(case.workspaces["archive_verify"], "rev-list", "--count", "HEAD")
        effect = case.claim(provider.aggregate_id, "notify_node_accepted")
        # The first application simulates a crash after the durable checker gate
        # has committed but before the claimed notification is acknowledged.
        await case.processor._execute_mechanical(effect)
        bound = case.node("archive_verify")
        assert bound.state == "REVIEW_QUEUED"
        assert bound.payload["dependency_outputs"][provider.aggregate_id]["candidate_ref"] == provider_ref
        assert bound.payload["dependency_outputs"][provider.aggregate_id]["candidate_digest"] == case.base
        assert bound.payload["accepted_dependency_candidate_digests"] == [case.base]
        assert bound.payload["verification_base_sha"] == head
        assert bound.payload["candidate_ref"] == original.payload["candidate_ref"]
        assert bound.payload["candidate_digest"] == original.payload["candidate_digest"]
        assert await case.processor._process_effect(effect) == "completed"
        case.assert_receipt(effect)
        assert case.node("archive_verify") == bound
        replay = prepare_node_verification_baseline(bound, case.nodes(), artifacts=case.artifacts)
        assert replay["verification_base_sha"] == head
        assert replay["dependency_outputs"] == bound.payload["dependency_outputs"]
        assert case.git(case.workspaces["archive_verify"], "rev-parse", "HEAD") == head
        assert case.git(case.workspaces["archive_verify"], "rev-list", "--count", "HEAD") == commits
        assert case.git(case.workspaces["archive_verify"], "status", "--porcelain") == ""
        assert case.node() == provider
        case.probe("archive_verify", "run() == 2")
        await case.process(bound.aggregate_id, "admit_verifier_role")
        admitted = case.node("archive_verify")
        assert admitted.state == "REVIEWING"
        assert admitted.payload["candidate_ref"] == bound.payload["candidate_ref"]
        cycle = case.coordinator.execution(workflow_id=case.workflow_id).cycles["archive_verify"]
        assert cycle.active_assignment.slot == CycleSlot.CHECKER
        assert case.node("inventory").state == "REVIEW_BLOCKED_BY_DEPS"
    asyncio.run(run())


def test_public_workflow_triage_resolution_resumes_same_checkpoint_and_epoch(case):
    async def run():
        provider = await case.ready()
        consumer = case.node("archive_verify")
        stored = case.stored(provider.aggregate_id, "notify_node_accepted")
        # Retry exhaustion is deterministic and preserves the failed receipt.
        with case.repository.database.write_connection() as connection:
            connection.execute(
                "UPDATE bunshin_v2_outbox SET max_attempts = 1 WHERE effect_id = ?",
                (stored["effect_id"],),
            )
        effect = case.claim(provider.aggregate_id, "notify_node_accepted")
        effect["max_attempts"] = 1
        with patch(
            "pal.bunshin.dependency_baselines._validate_unchanged_dependency_candidate",
            side_effect=ValueError("accepted dependency Candidate contains no delta: " + provider.aggregate_id),
        ):
            assert await case.processor._process_effect(effect) == "failed"
        assert case.workflow().state == "TRIAGE_REQUIRED"
        assert case.node() == provider
        assert case.node("archive_verify") == consumer
        attempts = case.repository.outbox_claims.list_effect_attempts(effect["effect_id"])
        assert [item["status"] for item in attempts] == ["failed"]
        await case.process(case.workflow_id, "freeze_workflow_children")
        for name in case.graph.nodes:
            if case.node(name).state == "PAUSE_REQUESTED":
                await case.process(case.node_ids[name], "pause_role")
        assert case.repository.snapshots.read_snapshot(AggregateType.EXECUTION_EPOCH, case.epoch_id).state == "PAUSED"
        assert case.node("archive_verify").state == "PAUSED"
        response = case.service.resume_workflow(
            workflow_id=case.workflow_id, actor="operator", source_channel="test",
        )
        assert response["status"] == "triage_requires_resolution"
        response = case.service.resolve_triage(
            workflow_id=case.workflow_id, actor="operator", source_channel="test",
            subject="phase:workflow",
            resolution="The unchanged accepted Git checkpoint is now supported; resume this existing workflow.",
        )
        assert response["status"] == "triage_resolved"
        assert response["workflow_id"] == case.workflow_id
        await case.process(case.workflow_id, "reconcile_workflow")
        await case.process(case.epoch_id, "reconcile_execution_epoch")
        recovered = case.node("archive_verify")
        assert recovered.state == "REVIEW_QUEUED"
        assert recovered.payload["candidate_ref"] == consumer.payload["candidate_ref"]
        assert recovered.payload["dependency_outputs"][provider.aggregate_id]["candidate_ref"] == provider.payload["candidate_ref"]
        assert case.node() == provider
        assert case.workflow().payload["execution_epoch_id"] == case.epoch_id
        assert case.repository.outbox_claims.list_effect_attempts(effect["effect_id"]) == attempts
        assert case.stored(provider.aggregate_id, "notify_node_accepted")["status"] == "failed"
        case.probe("archive_verify", "run() == 2")
        await case.process(recovered.aggregate_id, "admit_verifier_role")
        assert case.node("archive_verify").state == "REVIEWING"
    asyncio.run(run())


@pytest.mark.parametrize("tamper", [
    "missing_ref", "missing_blob", "corrupt_blob", "non_durable", "wrong_artifact_type",
    "byte_size", "schema_version", "node_run_id", "unit_contract_hash",
    "environment_fingerprint", "architecture_base_sha", "base_sha", "previous_head_sha",
    "candidate_digest", "baseline_tree_sha", "candidate_tree_sha", "changed_paths",
    "delta_patch_sha", "workflow_id", "epoch_id", "graph_generation",
])
@pytest.mark.parametrize("cached", [False, True], ids=["first-assembly", "durable-replay"])
def test_unchanged_dependency_rejects_missing_corrupt_or_unowned_checkpoint(case, tamper, cached):
    provider = asyncio.run(case.ready())
    if cached:
        asyncio.run(case.process(provider.aggregate_id, "notify_node_accepted"))
    consumer = case.node("archive_verify")
    if cached:
        assert consumer.payload["dependency_outputs"][provider.aggregate_id]["candidate_digest"] == case.base
    payload = dict(provider.payload)
    ref = dict(payload["candidate_ref"])
    candidate = dict(case.artifacts.read_json(ref))
    if tamper == "missing_ref":
        payload["candidate_ref"] = {}
    elif tamper in {"missing_blob", "corrupt_blob"}:
        record = case.repository.artifacts.read_artifact_record(ref["sha256"])
        path = Path(record["storage_path"])
        if tamper == "missing_blob":
            path.unlink()
        else:
            path.write_text("{}")
    elif tamper == "non_durable":
        payload["candidate_ref"] = {**ref, "durable": False}
    elif tamper == "byte_size":
        payload["candidate_ref"] = {**ref, "byte_size": ref["byte_size"] + 1}
    elif tamper == "wrong_artifact_type":
        payload["candidate_ref"] = case.artifacts.put_json(
            candidate, artifact_type="TestArtifact",
        ).to_dict()
    elif tamper in {"epoch_id", "graph_generation"}:
        payload[tamper] = 2 if tamper == "graph_generation" else "unrelated-epoch"
    elif tamper == "workflow_id":
        provider = replace(provider, workflow_id="unrelated-workflow")
    else:
        candidate[tamper] = (
            ["manifest_model.py"] if tamper == "changed_paths"
            else "not-the-bound-value"
        )
        payload["candidate_ref"] = case.artifacts.put_json(
            candidate, artifact_type="GitCheckpointArtifact",
        ).to_dict()
    altered = replace(provider, payload=payload)
    nodes = {**case.nodes(), provider.aggregate_id: altered}
    workspace = case.workspaces["archive_verify"]
    head = case.git(workspace, "rev-parse", "HEAD")
    with pytest.raises((ValueError, OSError)):
        prepare_node_verification_baseline(consumer, nodes, artifacts=case.artifacts)
    assert case.git(workspace, "rev-parse", "HEAD") == head
    assert case.git(workspace, "status", "--porcelain") == ""
    assert case.node("archive_verify") == consumer


def test_unchanged_dependency_rejects_baseline_outside_consumer_history(case):
    provider = asyncio.run(case.ready())
    consumer = case.node("archive_verify")
    workspace = case.workspaces["archive_verify"]
    # Equal file bytes do not make an unrelated commit an owned baseline.
    tree = case.git(workspace, "rev-parse", "HEAD^{tree}")
    unrelated = case.git(workspace, "commit-tree", tree, "-m", "unrelated history with matching product")
    case.git(workspace, "reset", "--hard", unrelated)
    with pytest.raises(ValueError):
        prepare_node_verification_baseline(consumer, case.nodes(), artifacts=case.artifacts)
    assert case.git(workspace, "rev-parse", "HEAD") == unrelated
    assert case.node() == provider
    assert case.node("archive_verify") == consumer


def test_delete_only_provider_delta_still_assembles_and_replays(case):
    async def run():
        provider = await case.ready(delete_provider_test=True)
        candidate = case.artifacts.read_json(provider.payload["candidate_ref"])
        assert candidate["candidate_digest"] != case.base
        assert candidate["changed_paths"] == [str(case.obsolete_test)]
        assert candidate["delta_patch_sha"] != EMPTY_DELTA_SHA
        assert case.git(
            case.workspaces["manifest_model"], "diff", "--name-status", case.base,
            provider.payload["candidate_digest"],
        ) == "D\t" + str(case.obsolete_test)
        original = case.node("archive_verify")
        workspace = case.workspaces["archive_verify"]
        assert (workspace / case.obsolete_test).is_file()
        await case.process(provider.aggregate_id, "notify_node_accepted")
        bound = case.node("archive_verify")
        assert bound.state == "REVIEW_QUEUED"
        assert not (workspace / case.obsolete_test).exists()
        assert bound.payload["candidate_digest"] != original.payload["candidate_digest"]
        assert bound.payload["implementation_candidate_ref"] == original.payload["candidate_ref"]
        assembled = case.artifacts.read_json(bound.payload["candidate_ref"])
        assert assembled["assembly_boundary"] == "verification"
        assert set(assembled["changed_paths"]) == {"archive_verify.py", str(case.obsolete_test)}
        assert bound.payload["dependency_outputs"][provider.aggregate_id]["candidate_ref"] == provider.payload["candidate_ref"]
        head = case.git(workspace, "rev-parse", "HEAD")
        assert case.git(workspace, "rev-list", "--count", f"{case.base}..HEAD") == "2"
        # Both a crash before publishing bindings and durable replay preserve
        # the one real deletion commit and its immutable assembly checkpoint.
        replay = prepare_node_verification_baseline(original, case.nodes(), artifacts=case.artifacts)
        assert replay["candidate_ref"] == bound.payload["candidate_ref"]
        prepare_node_verification_baseline(bound, case.nodes(), artifacts=case.artifacts)
        assert case.git(workspace, "rev-parse", "HEAD") == head
        assert case.git(workspace, "rev-list", "--count", f"{case.base}..HEAD") == "2"
        assert case.git(workspace, "status", "--porcelain") == ""
        case.probe("archive_verify", "run() == 2")
        await case.process(bound.aggregate_id, "admit_verifier_role")
        assert case.node("archive_verify").state == "REVIEWING"
    asyncio.run(run())


@pytest.mark.parametrize("case", [True], indirect=True, ids=["all-products-preexist"])
def test_unchanged_intermediate_keeps_provider_bindings_through_next_consumer(case):
    async def run():
        await case.submit_producers()
        original_refs = {name: case.node(name).payload["candidate_ref"] for name in case.graph.nodes}
        for name in case.graph.nodes:
            candidate = case.artifacts.read_json(original_refs[name])
            assert candidate["node_run_id"] == case.node_ids[name]
            assert candidate["candidate_digest"] == case.base
            assert candidate["changed_paths"] == []
            assert candidate["delta_patch_sha"] == EMPTY_DELTA_SHA
        provider = await case.accept_provider()
        await case.process(provider.aggregate_id, "notify_node_accepted")
        intermediate = await case.accept_provider("archive_verify")
        assert intermediate.payload["candidate_ref"] == original_refs["archive_verify"]
        assert intermediate.payload["dependency_outputs"][provider.aggregate_id]["candidate_ref"] == original_refs["manifest_model"]
        assert intermediate.payload["dependency_output_hashes"]
        # The producer checkpoint precedes verifier dependency binding. Those
        # later hashes belong to the accepted node, not the immutable no-op file.
        assert case.artifacts.read_json(original_refs["archive_verify"])["dependency_output_hashes"] == {}
        await case.process(intermediate.aggregate_id, "notify_node_accepted")
        consumer = case.node("inventory")
        assert consumer.state == "REVIEW_QUEUED"
        assert consumer.payload["candidate_ref"] == original_refs["inventory"]
        assert consumer.payload["candidate_digest"] == case.base
        assert consumer.payload["accepted_dependency_candidate_digests"] == [case.base, case.base]
        assert set(consumer.payload["dependency_outputs"]) == {provider.aggregate_id, intermediate.aggregate_id}
        for accepted in (provider, intermediate):
            assert consumer.payload["dependency_outputs"][accepted.aggregate_id]["candidate_ref"] == accepted.payload["candidate_ref"]
        assert case.node("archive_verify") == intermediate
        replay = prepare_node_verification_baseline(consumer, case.nodes(), artifacts=case.artifacts)
        assert replay["dependency_outputs"] == consumer.payload["dependency_outputs"]
        assert replay["accepted_dependency_candidate_digests"] == [case.base, case.base]
        case.probe("inventory", "run() == 4")
        await case.process(consumer.aggregate_id, "admit_verifier_role")
        assert case.node("inventory").state == "REVIEWING"
        for workspace in case.workspaces.values():
            assert case.git(workspace, "rev-parse", "HEAD") == case.base
            assert case.git(workspace, "rev-list", "--count", "HEAD") == "1"
            assert case.git(workspace, "status", "--porcelain") == ""
    asyncio.run(run())
