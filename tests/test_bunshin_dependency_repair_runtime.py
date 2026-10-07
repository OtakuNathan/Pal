"""Public/outbox release gates using real repositories, software GraphIR and git.

Role execution is represented by deterministic local tasks. No provider or live
Bunshin runtime is invoked; persisted public semantic effects perform repair.
"""
from __future__ import annotations

import asyncio
import copy
import os
import sys
from datetime import timedelta
import subprocess
from unittest.mock import patch

import pytest

from tests import test_bunshin_v2_verifier_scope_recovery as scope_fixture
from pal.bunshin.architecture_compilation import ArchitectureTemplateCompiler
from pal.bunshin.workflow_catalog import BunshinV2Catalog
from pal.bunshin.contract_protocol import validate_contract_payload
from pal.bunshin.contracts import ActionEnvelope, AggregateType, DeferredEffectError, SubmissionInvariantError
from pal.bunshin.execution_values import workspace_content_fingerprint
from pal.bunshin.graph_compiler import GraphCompileBindings, GraphCompiler
from pal.bunshin.graph_protocol import RoleBinding
from pal.bunshin.graph_satellites import FamilyGraphSatelliteProjector
from pal.bunshin.orchestration import BunshinV2OutboxProcessor
from pal.bunshin.role_protocol import RoleAssignmentRequest, stable_hash
from pal.bunshin.process_lifecycle import WorkerProcessOwner
from pal.bunshin.verification_readiness import verification_corpus_snapshot


def _concurrent_software_graph(workflow_id):
    definition = ArchitectureTemplateCompiler().compile("software_engineering.v1")
    payload = copy.deepcopy(definition.example)
    provider = payload["modules"]["decoder"]
    consumer = payload["modules"]["delivery"]
    dependency = consumer["dependencies"]["decoder"]
    modules = {name: copy.deepcopy(provider) for name in ("manifest_model", "index_model")}
    for name, target in (("archive_verify", "manifest_model"), ("checksum_verify", "manifest_model"), ("index_verify", "index_model")):
        modules[name] = copy.deepcopy(consumer)
        modules[name]["dependencies"] = {target: copy.deepcopy(dependency)}
    modules["backup_cli"] = copy.deepcopy(consumer)
    modules["backup_cli"]["dependencies"] = {
        name: {"consumes": ["application"], "purpose": "Combine verified results.", "handoff": "Read the application result."}
        for name in ("archive_verify", "checksum_verify", "index_verify")
    }
    payload["modules"] = modules
    for name, module in modules.items():
        module["definition"]["paths"] = {
            "contract_mode": "review_guarded", "contract_paths": [f"{name}.py"],
            "implementation_scopes": [{"kind": "file", "path": f"{name}.py"}], "reference_only": [],
        }
    payload["graph"]["sink"] = "backup_cli"
    payload["context"]["build_system"]["owner"] = "backup_cli"
    payload["requirements"]["decode_frames"]["owner"] = "manifest_model"
    payload["requirements"]["decode_frames"]["contract_path"] = ["manifest_model.decoded_frames"]
    payload["scenarios"]["decode_one_frame"]["modules"] = list(modules)
    payload["scenarios"]["decode_one_frame"]["entrypoint"]["module"] = "backup_cli"
    return GraphCompiler().compile(
        validate_contract_payload(payload, definition=definition), graph_id=workflow_id, generation=1,
        bindings=GraphCompileBindings(producer=RoleBinding("profile", "coder"), checker=RoleBinding("profile", "verifier"), execution_adapter="software_git.v2"),
        satellite_projector=FamilyGraphSatelliteProjector(specialization_id=definition.specialization_id, template=definition.graph_satellite_template),
        source_ref="concurrent-software-architecture.yaml", workspace_authority_rules=definition.workspace_authority_rules,
    )


class RuntimeCase:
    def __init__(self):
        self.fx = scope_fixture.VerifierScopeRecoveryTests()
        graph = _concurrent_software_graph("workflow-scope")
        dispatch = self.fx._dispatch
        self.workspaces = {}

        def create_isolated_workspace(name, action, payload=None):
            if action == "CREATE_NODE_RUN":
                workspace = self.fx.root / ("workspace-" + name)
                subprocess.run(["git", "clone", "--quiet", str(self.fx.repo), str(workspace)], check=True, capture_output=True)
                for key, value in (("user.email", "regression@example.com"), ("user.name", "Regression")):
                    subprocess.run(["git", "-C", str(workspace), "config", key, value], check=True)
                self.workspaces[name] = workspace
                view = self.fx.artifacts.put_json({"module_name": name}, artifact_type="ModuleWorkViewArtifact")
                payload = {**payload, "workspace_path": str(workspace), "graph_generation": 1,
                           "unit_work_view_ref": view.to_dict()}
            return dispatch(name, action, payload)

        self.fx._dispatch = create_isolated_workspace
        with patch.object(scope_fixture, "_compiled_graph", return_value=graph):
            self.fx.setUp()
        self.service, self.repository, self.artifacts = self.fx.service, self.fx.repository, self.fx.artifacts
        self.worker, self.coordinator = self.fx.worker, self.fx.coordinator
        self.workflow_id = self.fx.workflow_id
        self.worker.components.verification_settlement.dependency_repair_registration = self.worker.components.node_control.dependency_repairs.register
        self.binding = BunshinV2Catalog(self.fx.root, self.artifacts).publish_family_binding("software_engineering.v2_coder")
        for version, action in enumerate(("CREATE_WORKFLOW", "START_WORKFLOW")):
            self.repository.transitions.dispatch(ActionEnvelope(
                action_type=action, workflow_id=self.workflow_id, aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=self.workflow_id, actor="regression", expected_version=version,
                idempotency_key="fixture-workflow:" + action, payload={"family_binding_ref": self.binding.to_dict()},
            ))
        self.processor = BunshinV2OutboxProcessor(self.service, semantic_effects=self.worker, worker_id="cohort-regression")
        self.claimed = {}
        self.roles = {}
        self.fx._accept_provider("manifest_model")
        self.fx._accept_provider("index_model")

    def close(self):
        for name in self.workspaces:
            self.worker.workspace_locks.release("verification:" + self.fx._node_id(name))
            self.worker.workspace_locks.release(self.fx._node_id(name))
        self.fx.doCleanups()

    def node(self, name="archive_verify"):
        return self.fx._node(name)

    def graph(self):
        return self.coordinator.execution(workflow_id=self.workflow_id)

    def dispatch(self, name, action, payload=None, **settlement):
        node = self.node(name)
        self.fx.sequence += 1
        return self.repository.transitions.dispatch(ActionEnvelope(
            action_type=action, workflow_id=self.workflow_id, aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id=node.aggregate_id, actor="public-cohort-regression", expected_version=node.version,
            idempotency_key=f"cohort:{self.fx.sequence}:{name}:{action}", payload=payload or {},
        ), **settlement).snapshot

    def stored(self, name, effect_type):
        return self.fx._stored_effect(self.node(name), effect_type=effect_type)

    def claim(self, name, effect_type):
        stored = self.stored(name, effect_type)
        for effect in self.repository.outbox_claims.claim_outbox(self.processor.worker_id, limit=1000, lease_seconds=120):
            self.claimed[effect["effect_id"]] = effect
        effect = self.claimed[stored["effect_id"]]
        assert effect["status"] == "inflight"
        assert effect["payload"]["_causal_context"]
        return effect

    async def process(self, name, effect_type):
        effect = self.claim(name, effect_type)
        outcome = await self.processor._process_effect(effect)
        if outcome != "completed":
            with self.repository.database.read_connection() as connection:
                failure = dict(connection.execute("SELECT * FROM bunshin_v2_outbox WHERE effect_id=?", (effect["effect_id"],)).fetchone())
            pytest.fail(f"{effect_type}: {outcome}: {failure}")
        return effect

    async def start_checker(self, name, *, admit_only=False):
        self.dispatch(name, "DEPENDENCIES_ACCEPTED", {"accepted_producer_dependency_node_ids": [], "epoch_frozen": False})
        await self.process(name, "admit_implementation_role")
        producer = self.node(name)
        token = producer.payload["fencing_token"]
        self.dispatch(name, "SUBMIT_CANDIDATE", {"fencing_token": token})
        self.dispatch(name, "QUIESCE_COMPLETED", {"fencing_token": token, "process_group_reaped": True,
                      "exclusive_workspace_lock": True, "workspace_fingerprint": "candidate-tree"})
        self.dispatch(name, "CANDIDATE_SNAPSHOTTED", {"candidate_ref": self.fx.candidate_ref.to_dict(),
                      "candidate_digest": self.fx.digest, "workspace_fingerprint": "candidate-tree"})
        self.coordinator.producer_submitted(workflow_id=self.workflow_id, node_name=name, product_ref=self.fx.candidate_ref.sha256)
        self.repository.leases.release_lease(producer.payload["lease_resource_key"], producer.payload["active_worker_id"], token)
        outputs = {self.fx._node_id(target): {"candidate_ref": self.node(target).payload["candidate_ref"],
                   "candidate_digest": self.node(target).payload["candidate_digest"]}
                   for target in self.graph().graph.checker_predecessors(name)}
        self.dispatch(name, "VERIFICATION_DEPENDENCIES_ACCEPTED", {"accepted_dependency_node_ids": list(outputs),
                      "dependency_outputs": outputs, "epoch_frozen": False})
        await self.process(name, "admit_verifier_role")
        node = self.node(name)
        effect = self.stored(name, "run_verifier_role")
        assert effect["payload"]["_causal_context"]["active_worker_id"] == node.payload["active_worker_id"]
        if admit_only:
            return {"node": node, "effect": effect}
        session = node.payload["active_worker_id"]
        self.repository.role_sessions.ensure_role_session(
            session_id=session, workflow_id=self.workflow_id, aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id=node.aggregate_id, role="verifier", mode="module", role_profile_id="software_engineering.v2_verifier",
            family_binding_sha=self.binding.sha256, scope_kind="module", subject_key=name,
        )
        assignment = self.repository.role_assignments.create_role_assignment(RoleAssignmentRequest(
            assignment_key=effect["effect_key"], session_id=session, workflow_id=self.workflow_id,
            aggregate_type=AggregateType.DAG_NODE_RUN.value, aggregate_id=node.aggregate_id,
            role="verifier", mode="module", role_profile_id="software_engineering.v2_verifier",
            family_binding_sha=self.binding.sha256, input_fingerprint="bound-checker-input:" + name,
            required_inputs=(), input_refs={"candidate_diff": self.fx.candidate_ref.to_dict(), "module_work_view": node.payload["unit_work_view_ref"]},
            execution_spec={"effect_type": "run_verifier_role", "effect_key": effect["effect_key"], "effect_id": effect["effect_id"],
                            "evaluation_generation": 0, "business_lease": {"resource_key": node.payload["lease_resource_key"],
                            "owner_id": session, "fencing_token": node.payload["fencing_token"], "expected_state": "REVIEWING", "epoch_id": node.payload["epoch_id"], "graph_generation": 1}}, submission_kind="verification",
        ))
        attempt = self.repository.role_assignments.claim_role_assignment(assignment["assignment_id"])
        attempt_lease = self.repository.leases.claim_lease("attempt:" + attempt["attempt_id"], attempt["attempt_id"], ttl_seconds=120)
        workspace = {"repo_path": str(self.workspaces[name]), "review_scratch_dir": str(self.fx.root / ("scratch-" + name)),
                     "write_path_scopes": [node.payload["path_policy"]["verification_corpus"]], "workspace_binding": "canonical",
                     "bunshin_v2": {"workflow_id": self.workflow_id, "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                       "aggregate_id": node.aggregate_id, "role": "verifier", "mode": "module", "invocation_id": attempt["attempt_id"],
                       "authoring_input_fingerprint": assignment["input_fingerprint"], "lease_resource_key": attempt_lease.resource_key,
                       "fencing_token": attempt_lease.fencing_token}}
        prompt = self.artifacts.put_json({"workspace": workspace, "metadata": {"agent_session": {"session_id": session}}}, artifact_type="RolePromptPackArtifact")
        self.repository.role_attempts.start_role_attempt(assignment_id=assignment["assignment_id"], attempt_id_value=attempt["attempt_id"],
            lease_resource_key=attempt_lease.resource_key, fencing_token=attempt_lease.fencing_token, prompt_pack_ref=prompt.to_dict())
        role = {"assignment": assignment, "attempt": attempt, "attempt_lease": attempt_lease,
                "workspace": workspace, "effect": effect, "node": node}
        self.roles[name] = role
        return role

    def submit(self, name, *, dependency=True, settle=False, outcome="module_repair", reuse_recorded=False):
        role = self.roles[name]
        if reuse_recorded:
            submission, ref = role["submission"], role["submission_ref"]
        else:
            provider = "index_model" if name == "index_verify" else "manifest_model"
            path = self.workspaces[name] / "tests" / name / "verifier" / "test_contract.py"
            path.write_text(f"def test_{name}_preserved():\n    assert True\n", encoding="utf-8")
            findings = ([] if outcome == "pass" else [scope_fixture._finding("dependency_defect" if dependency else "module_defect",
                         f"{provider if dependency else name}.py", identity="finding_" + name, summary="Preserve " + name + " evidence.")])
            submission = self.fx._submission(findings)
            submission.update(outcome=outcome, target_modules=[provider if dependency else name] if findings else [])
            submission["recorded_results"][0].update(status="PASS" if outcome == "pass" else "FAIL", input_fingerprint=role["assignment"]["input_fingerprint"])
            submission["tool_receipts"][0].update(ok=outcome == "pass", verification_binding=verification_corpus_snapshot(role["workspace"]))
            ref = self.artifacts.put_json(submission, artifact_type="VerifierRoleSubmissionArtifact")
            self.repository.role_submissions.record_role_submission(assignment_id=role["assignment"]["assignment_id"],
                attempt_id_value=role["attempt"]["attempt_id"], fencing_token=role["attempt_lease"].fencing_token,
                artifact_ref=ref.to_dict(), payload_hash=stable_hash(submission), settlement_action={"action_type": "SUBMIT_SEMANTIC_VERIFICATION"})
            role.update(submission=submission, submission_ref=ref)
        if settle:
            node = self.node(name)
            pending = self.artifacts.put_json({
                "schema_version": "1", "submission": submission, "candidate_ref": self.fx.candidate_ref.to_dict(),
                "candidate_digest": self.fx.digest, "candidate_git_base": self.fx.digest,
                "implementation_candidate_ref": self.fx.candidate_ref.to_dict(), "review_workspace": str(self.workspaces[name]),
                "review_scratch": role["workspace"]["review_scratch_dir"], "execution_adapter": "software_git.v2",
                "submitted_workspace_fingerprint": workspace_content_fingerprint(self.workspaces[name]),
                "invocation_id": node.payload["active_worker_id"], "lease_resource_key": node.payload["lease_resource_key"],
                "fencing_token": node.payload["fencing_token"], "role_assignment_id": role["assignment"]["assignment_id"],
                "role_submission_payload_hash": stable_hash(submission), "submission_ref": ref.to_dict(),
            }, artifact_type="PendingSemanticVerificationArtifact")
            self.dispatch(name, "SUBMIT_SEMANTIC_VERIFICATION", {"pending_verification_ref": pending.to_dict(),
                "role_assignment_id": role["assignment"]["assignment_id"], "role_submission_payload_hash": stable_hash(submission)},
                role_assignment_id=role["assignment"]["assignment_id"], role_submission_payload_hash=stable_hash(submission))
            role["pending_ref"] = pending
        return role

    async def prepare_source(self, name="archive_verify", *, reuse_recorded=False):
        self.submit(name, settle=True, reuse_recorded=reuse_recorded)
        await self.process(name, "quiesce_verifier_role")
        return await self.process(name, "snapshot_verifier_result")

    def assert_receipt(self, effect):
        with self.repository.database.read_connection() as connection:
            row = dict(connection.execute("SELECT * FROM bunshin_v2_outbox WHERE effect_id=?", (effect["effect_id"],)).fetchone())
            count = connection.execute("SELECT count(*) FROM bunshin_v2_effect_receipts WHERE effect_key=?", (effect["effect_key"],)).fetchone()[0]
        assert row["status"] == "completed"
        assert count == 1


@pytest.fixture
def case():
    value = RuntimeCase()
    try:
        yield value
    finally:
        value.close()


@pytest.mark.parametrize("peer_state", ["running", "result_recorded", "pass"])
def test_claimed_public_cohort_preserves_peer_and_replays_exact_source(case, peer_state):
    async def run():
        await case.start_checker("archive_verify")
        peer = await case.start_checker("checksum_verify")
        if peer_state != "running":
            case.submit("checksum_verify", dependency=False, outcome="pass" if peer_state == "pass" else "module_repair")
        else:
            (case.workspaces["checksum_verify"] / "tests/checksum_verify/verifier/test_contract.py").write_text("def test_unsubmitted():\n    assert True\n")
        source_effect = await case.prepare_source()
        pending = case.graph().dependency_repairs.pending
        assert pending is not None
        assert {member.node_name for member in pending.frontier.values()} == {"archive_verify", "checksum_verify"}
        assert not pending.closures
        assert case.node("manifest_model").state == "ACCEPTED"
        assert case.node("checksum_verify").state == "CANCEL_REQUESTED"
        applied_effect = await case.process("archive_verify", "reconcile_dependency_repairs")
        graph = case.graph()
        assert graph.dependency_repairs.pending is None
        assert len(graph.dependency_repairs.history) == 1
        cohort = graph.dependency_repairs.history[0]
        assert cohort.status == "applied"
        assert not cohort.ready
        assert set(cohort.closures) == set(cohort.frontier)
        assert all(closure.task_closed and closure.process_reaped and closure.workspace_released
                   and closure.lease_released and closure.submission_cut for closure in cohort.closures.values())
        assert case.node("manifest_model").state == "REPAIR_QUEUED"
        assert case.node("archive_verify").state == case.node("checksum_verify").state == "STALE"
        capture = case.artifacts.read_json(case.node("checksum_verify").payload["dependency_repair_capture_ref"])
        assert capture["status"] == {"running": "no_submission", "result_recorded": "FAIL", "pass": "PASS"}[peer_state]
        assert capture["candidate_digest"] != case.fx.digest
        candidate = case.artifacts.read_json(capture["candidate_ref"])
        assert candidate["verifier_test_paths"] == ["tests/checksum_verify/verifier/test_contract.py"]
        if peer_state != "running":
            assert case.artifacts.read_json(peer["submission_ref"]) == peer["submission"]
            receipt = case.artifacts.read_json(case.node("checksum_verify").payload["verification_artifact_ref"])
            assert receipt["settlement_status"] == "invalidated"
            assert receipt["verification_artifact_ref"] == capture["report_ref"]
            history = case.node("checksum_verify").payload["historical_repair_bill_refs"]
            preservation = [case.artifacts.read_json(ref) for ref in history]
            assert any(packet.get("capture_ref") == case.node("checksum_verify").payload["dependency_repair_capture_ref"]
                       and packet.get("candidate_ref") == capture["candidate_ref"] for packet in preservation)
            if peer_state == "result_recorded":
                packet = case.artifacts.read_json(capture["repair_packet_ref"])
                assert packet["findings"] == peer["submission"]["findings"]
                assert capture["repair_packet_ref"] in history
        source_capture = case.artifacts.read_json(case.node().payload["dependency_repair_capture_ref"])
        assert source_capture["source_pending_ref"] == case.roles["archive_verify"]["pending_ref"].to_dict()
        assert case.node().payload["failure_history"] == [{
            "finding_fingerprint": source_capture["finding_fingerprint"],
            "candidate_tree_hash": case.artifacts.read_json(source_capture["candidate_ref"])["candidate_tree_sha"],
        }]
        assert case.artifacts.read_json(case.roles["archive_verify"]["submission_ref"]) == case.roles["archive_verify"]["submission"]
        assert not case.worker.background.active_count
        for role in case.roles.values():
            assert not case.repository.leases.read_lease(role["node"].payload["lease_resource_key"])["owner_id"]
            assert case.repository.leases.read_lease(role["attempt_lease"].resource_key) is None
        before = {name: case.node(name) for name in case.graph().graph.nodes}
        replay = await case.worker.execute_semantic_effect(source_effect)
        assert replay["status"] == "superseded"
        receipt = case.worker.components.verification_snapshot.snapshot_semantic_verification(source_effect)
        assert case.artifacts.read_json(receipt["result_artifact_ref"])["settlement_status"] == "invalidated"
        assert await case.processor._process_effect(applied_effect) == "completed"
        assert {name: case.node(name) for name in case.graph().graph.nodes} == before
        case.assert_receipt(source_effect)
        case.assert_receipt(applied_effect)
    asyncio.run(run())


@pytest.mark.parametrize("late_receipt", [False, True])
def test_provider_release_waits_for_real_peer_process_task_lock_and_submit_cut(case, late_receipt):
    async def run():
        await case.start_checker("archive_verify")
        peer = await case.start_checker("checksum_verify")
        entered, cancelling, finish = (asyncio.Event() for _ in range(3))
        node, effect = peer["node"], peer["effect"]
        owner = WorkerProcessOwner(
            argv=(sys.executable, "-c", "import time; time.sleep(60)"), env=dict(os.environ),
            invocation_id=node.payload["active_worker_id"], run_id="deterministic-checksum-process",
            workspace=case.workspaces["checksum_verify"], workspace_locks=case.worker.workspace_locks,
            on_started=lambda _: entered.set(), on_reserved=case.worker.processes.register,
            on_registered=case.worker.processes.register, on_unregistered=case.worker.processes.unregister,
            effect_key=effect["effect_key"], assignment_id=peer["assignment"]["assignment_id"],
            attempt_id=peer["attempt"]["attempt_id"], business_lease_resource_key=node.payload["lease_resource_key"],
            business_fencing_token=node.payload["fencing_token"],
        )

        async def role_task():
            async with owner:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelling.set()
                    await finish.wait()
            return {}

        task = asyncio.create_task(role_task())
        case.worker.background.track(effect["effect_key"], task)
        case.worker.background.bind(effect["effect_key"], peer["assignment"]["assignment_id"])
        case.worker.background.bind_lease(effect["effect_key"], node.payload["active_worker_id"],
                                         node.payload["lease_resource_key"], node.payload["fencing_token"])
        from pal.bunshin.semantic_orchestration.dependency_repair_apply import apply_dependency_repair_cohort
        apply_observed = []

        def checked_apply(**kwargs):
            # Assert cleanup at the exact production apply boundary, before any
            # provider REPAIR_QUEUED projection or admission effect can exist.
            assert task.done()
            assert owner.resources_released and owner.process_group_reaped
            assert not case.worker.processes.contains(owner.invocation_id)
            assert not case.worker.workspace_locks.is_held(owner.lock_key)
            assert not case.worker.background.active_count
            assert not case.repository.leases.read_lease(node.payload["lease_resource_key"])["owner_id"]
            assert case.repository.leases.read_lease(peer["attempt_lease"].resource_key) is None
            assert case.node("manifest_model").state == "ACCEPTED"
            apply_observed.append(kwargs["cohort_key"])
            return apply_dependency_repair_cohort(**kwargs)

        apply_spy = patch("pal.bunshin.semantic_orchestration.dependency_repair_apply.apply_dependency_repair_cohort", side_effect=checked_apply)
        apply_spy.start()
        repairing = None
        try:
            await asyncio.wait_for(entered.wait(), 10)
            assert owner.pid and case.worker.processes.contains(owner.invocation_id)
            await case.prepare_source()
            original_provider = case.node("manifest_model")
            repairing = asyncio.create_task(case.process("archive_verify", "reconcile_dependency_repairs"))
            await asyncio.wait_for(cancelling.wait(), 10)
            assert not repairing.done()
            assert not task.done()
            assert case.worker.workspace_locks.is_held(owner.lock_key)
            assert case.worker.processes.contains(owner.invocation_id)
            assert case.repository.leases.read_lease(node.payload["lease_resource_key"])["owner_id"]
            assert case.node("manifest_model") == original_provider
            assert not case.graph().dependency_repairs.pending.ready
            if late_receipt:
                # A receipt committed before the cancellation cut must join the
                # frozen cohort, even though the aggregate is already frozen.
                case.submit("checksum_verify")
            finish.set()
            applied_effect = await asyncio.wait_for(repairing, 10)
            assert task.done()
            assert owner.resources_released and owner.process_group_reaped
            assert not case.worker.processes.contains(owner.invocation_id)
            assert not case.worker.workspace_locks.is_held(owner.lock_key)
            assert not case.worker.background.active_count
            assert not case.repository.leases.read_lease(node.payload["lease_resource_key"])["owner_id"]
            assert case.repository.leases.read_lease(peer["attempt_lease"].resource_key) is None
            assert case.node("manifest_model").state == "REPAIR_QUEUED"
            cohort = case.graph().dependency_repairs.history[0]
            assert len(cohort.intents) == (2 if late_receipt else 1)
            if late_receipt:
                assert len(case.node("checksum_verify").payload["failure_history"]) == 1
            capture = case.artifacts.read_json(case.node("checksum_verify").payload["dependency_repair_capture_ref"])
            assert capture["status"] == ("FAIL" if late_receipt else "no_submission")
            if not late_receipt:
                late = case.artifacts.put_json({"outcome": "pass"}, artifact_type="VerifierRoleSubmissionArtifact")
                before = case.node("manifest_model")
                with pytest.raises(ValueError, match="not accepting a submission"):
                    case.repository.role_submissions.record_role_submission(
                        assignment_id=peer["assignment"]["assignment_id"], attempt_id_value=peer["attempt"]["attempt_id"],
                        fencing_token=peer["attempt_lease"].fencing_token, artifact_ref=late.to_dict(),
                        payload_hash="too-late", settlement_action={"action_type": "SUBMIT_SEMANTIC_VERIFICATION"},
                    )
                assert case.node("manifest_model") == before
            case.assert_receipt(applied_effect)
            assert len(apply_observed) == 1
        finally:
            apply_spy.stop()
            finish.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if repairing is not None:
                await asyncio.gather(repairing, return_exceptions=True)
    asyncio.run(run())


def test_independent_submitted_report_waits_for_later_cohort_without_global_barrier(case):
    async def run():
        await case.start_checker("archive_verify")
        await case.start_checker("checksum_verify")
        await case.start_checker("index_verify")
        first_source_effect = await case.prepare_source()
        first = case.graph().dependency_repairs.pending
        second_source_effect = await case.prepare_source("index_verify")
        pending = case.graph().dependency_repairs.pending
        assert pending.key == first.key
        assert pending.providers == ("manifest_model",)
        assert "index_verify" not in {member.node_name for member in pending.frontier.values()}
        assert case.node("index_verify").state == "REVIEW_SNAPSHOTTING"
        assert case.node("index_verify").payload["dependency_repair_capture_ref"]
        assert case.node("index_model").state == "ACCEPTED"
        await case.process("archive_verify", "reconcile_dependency_repairs")
        first_history = case.graph().dependency_repairs.history
        assert len(first_history) == 1
        assert first_history[0].key == first.key
        assert case.node("index_model").state == "ACCEPTED"
        assert case.node("index_verify").state == "REVIEW_SNAPSHOTTING"
        await case.process("index_verify", "reconcile_dependency_repairs")
        history = case.graph().dependency_repairs.history
        assert case.graph().dependency_repairs.pending is None
        assert len(history) == 2
        assert history[0].key == first.key
        assert history[0].providers == ("manifest_model",)
        assert history[1].providers == ("index_model",)
        assert {member.node_name for member in history[1].frontier.values()} == {"index_verify"}
        assert all(cohort.status == "applied" for cohort in history)
        assert case.node("manifest_model").state == case.node("index_model").state == "REPAIR_QUEUED"
        assert case.node("index_verify").state == "STALE"
        before = {name: case.node(name) for name in case.graph().graph.nodes}
        await case.process("index_verify", "reconcile_dependency_repairs")
        for effect in (first_source_effect, second_source_effect):
            assert (await case.worker.execute_semantic_effect(effect))["status"] == "superseded"
            case.assert_receipt(effect)
        assert {name: case.node(name) for name in case.graph().graph.nodes} == before
    asyncio.run(run())


def test_claimed_apply_crash_retries_exact_effect_after_source_stale_and_manager_restart(case):
    from pal.bunshin.semantic_orchestration.orchestrator import SemanticOrchestrator
    from pal.bunshin.storage.serialization import _utc_datetime

    async def run():
        await case.start_checker("archive_verify")
        await case.start_checker("checksum_verify")
        await case.start_checker("index_verify")
        case.submit("checksum_verify", dependency=False)
        await case.prepare_source()
        await case.prepare_source("index_verify")
        original = case.claim("archive_verify", "reconcile_dependency_repairs")
        with patch("pal.bunshin.semantic_orchestration.dependency_repair_apply.apply_dependency_repair_cohort",
                   side_effect=RuntimeError("deterministic crash after all closures")):
            assert await case.processor._process_effect(original) == "failed"
        cohort = case.graph().dependency_repairs.pending
        assert cohort.ready
        assert case.node("archive_verify").state == case.node("checksum_verify").state == "STALE"
        assert case.node("manifest_model").state == "ACCEPTED"
        captures = {name: case.node(name).payload["dependency_repair_capture_ref"] for name in ("archive_verify", "checksum_verify")}
        # A fresh manager has no knowledge of the first process's local task
        # registry; immutable effect authority must be sufficient for retry.
        case.worker = SemanticOrchestrator(case.service)
        case.processor.semantic_effects = case.worker
        with patch("pal.bunshin.storage.outbox_claims._utc_datetime", return_value=_utc_datetime() + timedelta(seconds=6)):
            retry = case.claim("archive_verify", "reconcile_dependency_repairs")
        assert retry["effect_id"] == original["effect_id"]
        assert retry["effect_key"] == original["effect_key"]
        assert retry["payload"] == original["payload"]
        assert retry["effect_attempt_index"] == 2
        assert await case.processor._process_effect(retry) == "completed"
        assert len(case.graph().dependency_repairs.history) == 1
        assert case.graph().dependency_repairs.history[0].key == cohort.key
        assert case.node("manifest_model").state == "REPAIR_QUEUED"
        assert {name: case.node(name).payload["dependency_repair_capture_ref"] for name in captures} == captures
        independent = case.node("index_verify")
        assert independent.state == "REVIEW_SNAPSHOTTING"
        assert (await case.worker.execute_semantic_effect(original))["status"] == "superseded"
        assert case.node("index_verify") == independent
        assert case.node("index_model").state == "ACCEPTED"
        await case.process("index_verify", "reconcile_dependency_repairs")
        after = case.graph()
        assert len(after.dependency_repairs.history) == 2
        assert (await case.worker.execute_semantic_effect(original))["status"] == "superseded"
        assert case.graph() == after
        case.assert_receipt(original)
        attempts = case.repository.outbox_claims.list_effect_attempts(original["effect_id"])
        assert [attempt["status"] for attempt in attempts] == ["retryable", "completed"]
    asyncio.run(run())


@pytest.mark.parametrize("corruption", ["generation", "foreign_capture", "foreign_aggregate"])
def test_public_reconcile_rejects_corrupt_or_foreign_preparation_without_changes(case, corruption):
    async def run():
        await case.start_checker("archive_verify")
        await case.start_checker("checksum_verify")
        await case.start_checker("index_verify")
        await case.prepare_source()
        await case.prepare_source("index_verify")
        effect = copy.deepcopy(case.stored("archive_verify", "reconcile_dependency_repairs"))
        foreign = case.stored("index_verify", "reconcile_dependency_repairs")
        if corruption == "generation":
            effect["payload"]["graph_generation"] = 2
        elif corruption == "foreign_capture":
            effect["payload"]["dependency_repair_capture_ref"] = foreign["payload"]["dependency_repair_capture_ref"]
        else:
            effect["aggregate_id"] = foreign["aggregate_id"]
        graph = case.graph()
        nodes = {name: case.node(name) for name in graph.graph.nodes}
        with pytest.raises(SubmissionInvariantError):
            await case.worker.execute_semantic_effect(effect)
        assert case.graph() == graph
        assert {name: case.node(name) for name in graph.graph.nodes} == nodes
    asyncio.run(run())


def test_public_cohort_waits_for_admitted_setup_before_role_row_exists(case):
    async def run():
        await case.start_checker("archive_verify")
        peer = await case.start_checker("checksum_verify", admit_only=True)
        began, cancelling, finish = (asyncio.Event() for _ in range(3))
        reached_assignment = False
        effect, node = peer["effect"], peer["node"]

        async def setup():
            nonlocal reached_assignment
            began.set()
            try:
                await asyncio.Event().wait()
                reached_assignment = True
            finally:
                cancelling.set()
                await finish.wait()
            return {}

        task = asyncio.create_task(setup())
        case.worker.background.track(effect["effect_key"], task)
        case.worker.background.bind_lease(effect["effect_key"], node.payload["active_worker_id"],
                                         node.payload["lease_resource_key"], node.payload["fencing_token"])
        repairing = None
        try:
            await began.wait()
            await case.prepare_source()
            cohort = case.graph().dependency_repairs.pending
            frozen = next(member for member in cohort.frontier.values() if member.node_name == "checksum_verify")
            assert not frozen.role_assignment_id and not frozen.attempt_id
            repairing = asyncio.create_task(case.process("archive_verify", "reconcile_dependency_repairs"))
            await asyncio.wait_for(cancelling.wait(), 10)
            assert not repairing.done()
            assert case.node("manifest_model").state == "ACCEPTED"
            assert not case.graph().dependency_repairs.pending.ready
            finish.set()
            await asyncio.wait_for(repairing, 10)
            assert task.done() and not reached_assignment
            assert case.node("manifest_model").state == "REPAIR_QUEUED"
            capture = case.artifacts.read_json(case.node("checksum_verify").payload["dependency_repair_capture_ref"])
            assert capture["status"] == "no_submission"
            assert capture["source_assignment_id"] == ""
            assert not case.repository.leases.read_lease(node.payload["lease_resource_key"])["owner_id"]
            assert len(case.repository.role_assignments.list_role_assignments(workflow_id=case.workflow_id)) == 1
        finally:
            finish.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if repairing is not None:
                await asyncio.gather(repairing, return_exceptions=True)
    asyncio.run(run())


def test_claimed_snapshot_rebind_preserves_original_fence_and_closes_both_incarnations(case):
    async def run():
        source = await case.start_checker("archive_verify")
        await case.start_checker("checksum_verify")
        case.submit("archive_verify", settle=True)
        original_node = source["node"]
        resource = original_node.payload["lease_resource_key"]
        owner = original_node.payload["active_worker_id"]
        original_token = original_node.payload["fencing_token"]
        original_pending = case.artifacts.read_json(source["pending_ref"])
        original_assignment = case.repository.role_assignments.read_role_assignment(source["assignment"]["assignment_id"])
        assert original_pending["fencing_token"] == original_token
        case.repository.leases.release_lease(resource, owner, original_token)
        await case.process("archive_verify", "quiesce_verifier_role")
        snapshot_token = case.node().payload["fencing_token"]
        assert snapshot_token == original_token + 1
        await case.process("archive_verify", "snapshot_verifier_result")
        cohort = case.graph().dependency_repairs.pending
        incarnation = next(member for member in cohort.frontier.values() if member.node_name == "archive_verify")
        capture = case.artifacts.read_json(case.node().payload["dependency_repair_capture_ref"])
        assert incarnation.fencing_token == capture["fencing_token"] == original_token
        assert incarnation.lease_resource == capture["lease_resource_key"] == resource
        assert incarnation.lease_owner == capture["invocation_id"] == owner
        assert incarnation.snapshot_fencing_token == capture["snapshot_fencing_token"] == snapshot_token
        assert incarnation.snapshot_lease_resource == capture["snapshot_lease_resource"] == resource
        assert incarnation.snapshot_lease_owner == capture["snapshot_lease_owner"] == owner
        effect = await case.process("archive_verify", "reconcile_dependency_repairs")
        applied = case.graph().dependency_repairs.history[0]
        closure = applied.closures[incarnation.key]
        assert closure.incarnation == incarnation and closure.lease_released
        assert case.repository.leases.read_lease(resource)["owner_id"] == ""
        assert case.repository.leases.read_lease(resource)["fencing_token"] == snapshot_token
        assert case.repository.leases.read_lease(source["attempt_lease"].resource_key) is None
        assert case.artifacts.read_json(source["pending_ref"]) == original_pending
        assert case.repository.role_assignments.read_role_assignment(source["assignment"]["assignment_id"]) == original_assignment
        assert case.node("manifest_model").state == "REPAIR_QUEUED"
        # An old immutable effect never releases a subsequent lease incarnation.
        replacement = case.repository.leases.claim_lease(resource, "later-worker", ttl_seconds=120)
        assert replacement.fencing_token == snapshot_token + 1
        assert (await case.worker.execute_semantic_effect(effect))["status"] == "superseded"
        assert case.repository.leases.read_lease(resource)["owner_id"] == "later-worker"
        case.assert_receipt(effect)
    asyncio.run(run())


def _seed_two_identical_dependency_failures(case, name):
    """Derive the real report hash, then seed prior attempts via public rebind."""
    from pal.bunshin.semantic_orchestration.dependency_repair_facts import freeze_incarnation
    from pal.bunshin.semantic_orchestration.role_inputs import _candidate_tree_fingerprint

    case.submit(name)
    node = case.node(name)
    incarnation, _ = freeze_incarnation(case.repository, case.artifacts, case.graph(), node)
    # The fixture has admitted rows but no live task or subprocess. Honor the
    # collector's real exclusive-lock contract while computing this evidence.
    lock_owner = "prediction:" + name
    case.worker.workspace_locks.acquire(lock_owner, case.workspaces[name])
    try:
        predicted_ref = case.worker.components.node_control.dependency_repairs.collector.capture(
            node=node, incarnation=incarnation,
        )
    finally:
        case.worker.workspace_locks.release(lock_owner)
    # Prediction may checkpoint the corpus. Restore only the temporary git
    # branch pointer, retaining all files/index changes, so the original
    # submitted execution receipt remains bound to its actual candidate HEAD.
    subprocess.run(["git", "-C", str(case.workspaces[name]), "reset", "--soft", case.fx.digest], check=True, capture_output=True)
    predicted = case.artifacts.read_json(predicted_ref)
    assert predicted["status"] == "FAIL" and predicted["defect_kind"] == "dependency_defect"
    assert not predicted["routing_errors"]
    candidate = case.artifacts.read_json(predicted["candidate_ref"])
    entry = {"finding_fingerprint": predicted["finding_fingerprint"],
             "candidate_tree_hash": _candidate_tree_fingerprint(candidate, fallback=predicted["candidate_digest"])}
    case.dispatch(name, "REBIND_REVIEWER", {
        "active_worker_id": node.payload["active_worker_id"], "lease_resource_key": node.payload["lease_resource_key"],
        "fencing_token": node.payload["fencing_token"], "failure_history": [dict(entry), dict(entry)],
    })
    return predicted, entry


@pytest.mark.parametrize("reporter", ["initiating_source", "captured_peer"])
def test_public_dependency_no_progress_is_terminal_for_source_and_captured_peer(case, reporter):
    async def run():
        await case.start_checker("archive_verify")
        await case.start_checker("checksum_verify")
        name = "archive_verify" if reporter == "initiating_source" else "checksum_verify"
        predicted, entry = _seed_two_identical_dependency_failures(case, name)
        source_effect = await case.prepare_source(reuse_recorded=reporter == "initiating_source")
        if reporter == "captured_peer":
            before = case.graph().dependency_repairs.pending
            assert len(before.intents) == 1
            effect = case.claim("archive_verify", "reconcile_dependency_repairs")
            outcome = await case.processor._process_effect(effect)
            assert outcome == "deferred", case.stored("archive_verify", "reconcile_dependency_repairs")["last_error"]
            cohort = case.graph().dependency_repairs.pending
            assert cohort is not None and not cohort.ready
            assert set(cohort.intents) == set(before.intents)
            assert all(intent.source_node != "checksum_verify" for intent in cohort.intents.values())
        else:
            assert case.graph().dependency_repairs.pending is None
            case.assert_receipt(source_effect)
        terminal = case.node(name)
        assert terminal.state == "TRIAGE_REQUIRED"
        assert terminal.payload["blocker"]["kind"] == "no_progress"
        assert terminal.payload["blocker"]["rounds"] == 3
        assert terminal.payload["failure_history"] == [entry, entry, entry]
        assert case.node("manifest_model").state == "ACCEPTED"
        assert not case.graph().dependency_repairs.history
        if reporter == "captured_peer":
            capture = case.artifacts.read_json(terminal.payload["dependency_repair_capture_ref"])
            assert capture["finding_fingerprint"] == predicted["finding_fingerprint"]
            assert capture["candidate_ref"] == terminal.payload["candidate_ref"]
            packet_ref = capture["repair_packet_ref"]
            assert packet_ref in terminal.payload["historical_repair_bill_refs"]
            report_ref = capture["report_ref"]
        else:
            packet_ref = terminal.payload["repair_bill_ref"]
            report_ref = terminal.payload["verification_artifact_ref"]
        assert case.artifacts.read_json(packet_ref)["findings"] == case.roles[name]["submission"]["findings"]
        assert case.artifacts.read_json(report_ref)["findings"] == case.roles[name]["submission"]["findings"]
        checkpoint = case.artifacts.read_json(terminal.payload["candidate_ref"])
        assert checkpoint["verifier_test_paths"] == [f"tests/{name}/verifier/test_contract.py"]
        assert case.artifacts.read_json(case.roles[name]["submission_ref"]) == case.roles[name]["submission"]
        graph_before = case.graph()
        nodes_before = {node_name: case.node(node_name) for node_name in graph_before.graph.nodes}
        if reporter == "captured_peer":
            with pytest.raises(DeferredEffectError):
                await case.worker.execute_semantic_effect(effect)
        else:
            assert (await case.worker.execute_semantic_effect(source_effect))["status"] == "superseded"
        assert case.graph() == graph_before
        assert {node_name: case.node(node_name) for node_name in graph_before.graph.nodes} == nodes_before
        with pytest.raises(SubmissionInvariantError, match="committed|no.progress|terminal"):
            case.service.resolve_triage(workflow_id=case.workflow_id, actor="operator", source_channel="test",
                subject="module:" + name, resolution="Retry the preserved dependency finding.")
        assert case.graph() == graph_before
        assert {node_name: case.node(node_name) for node_name in graph_before.graph.nodes} == nodes_before
    asyncio.run(run())


def test_nonfrontier_pending_scope_admission_defers_without_charging_retry(case):
    async def run():
        case.dispatch("backup_cli", "DEPENDENCIES_ACCEPTED", {
            "accepted_producer_dependency_node_ids": [], "epoch_frozen": False,
        })
        await case.start_checker("archive_verify")
        await case.prepare_source()
        cohort = case.graph().dependency_repairs.pending
        assert "backup_cli" in cohort.scope
        assert "backup_cli" not in {member.node_name for member in cohort.frontier.values()}
        effect = case.claim("backup_cli", "admit_implementation_role")
        assert await case.processor._process_effect(effect) == "deferred"
        assert case.node("backup_cli").state == "QUEUED"
        assert not case.repository.leases.read_lease("node:node-backup_cli:writer")
        with case.repository.database.read_connection() as connection:
            row = dict(connection.execute("SELECT * FROM bunshin_v2_outbox WHERE effect_id=?", (effect["effect_id"],)).fetchone())
        assert row["attempt_count"] == 0
        assert row["status"] == "pending"
        await case.process("archive_verify", "reconcile_dependency_repairs")
        assert case.node("manifest_model").state == "REPAIR_QUEUED"
        assert case.node("backup_cli").state == "STALE"
    asyncio.run(run())
