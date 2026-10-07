"""Offline regressions for verifier ownership and durable route correction.

The architecture is compiled through the software Family's real contract and
GraphCompiler, then explicitly restored from the serialized legacy policy
with CONTRACT non-sink edges. These recovery cases cover persisted old graphs;
newly compiled graphs gate every produced dependency at verification.
No live provider, runtime database, or external architecture artifact is used.
"""
from __future__ import annotations

import asyncio
import copy
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pal.bunshin.architecture_compilation import ArchitectureTemplateCompiler
from pal.bunshin.contract_protocol import validate_contract_payload
from pal.bunshin.contracts import ActionEnvelope, AggregateType, SubmissionInvariantError
from pal.bunshin.cycle_protocol import AssignmentKind, CycleSlot, NodeCycleState
from pal.bunshin.graph_compiler import GraphCompileBindings, GraphCompiler
from pal.bunshin.graph_protocol import EdgeKind, RoleBinding, graph_ir_from_mapping
from pal.bunshin.graph_satellites import FamilyGraphSatelliteProjector
from pal.bunshin.review_findings import ADD_FINDING_CAPABILITY, add_finding_tool_result, empty_review_draft, structured_findings
from pal.bunshin.role_protocol import stable_hash
from pal.bunshin.service import BunshinV2WorkflowService
from pal.bunshin.semantic_orchestration.orchestrator import SemanticOrchestrator
from pal.bunshin.semantic_orchestration.role_inputs import _verifier_reference_refs
from pal.bunshin.semantic_orchestration.verification_policy import _verification_repair_scope
from pal.bunshin.submission_drafts import AUTHORING_CONTRACT_VERSION, SubmissionDraftContext, SubmissionDraftStore
from pal.bunshin.swe_verification import (
    compile_swe_verification_tool_contract,
    infer_repair_target_modules,
    semantic_verification_submission_errors,
    swe_verification_tool_result,
    verification_finding_route_errors,
    verification_outcome_readiness,
)
from pal.bunshin.verification import VerificationService, VerificationStatus
from pal.bunshin.verification_readiness import record_verification_execution, verification_corpus_snapshot
from pal.bunshin.work_items import findings_from_work_items, update_checklist_tool_result
from pal.bunshin.workflow_runtime import WorkflowCoordinator
from pal.shared import RuntimeStatus, ToolExecutionResult
from pal.shared.tool_protocol import EffectOutcome, RejectedResult, RetryDirective, new_tool_call


def _compiled_graph(workflow_id: str, *, legacy_contract_edges: bool = False):
    definition = ArchitectureTemplateCompiler().compile("software_engineering.v1")
    payload = copy.deepcopy(definition.example)
    provider = payload["modules"]["decoder"]
    consumer = payload["modules"]["delivery"]
    dependency = copy.deepcopy(consumer["dependencies"]["decoder"])
    archive = copy.deepcopy(consumer)
    archive["dependencies"] = {"manifest_model": dependency}
    cli = copy.deepcopy(consumer)
    cli["dependencies"] = {
        "manifest_model": copy.deepcopy(dependency),
        "archive_verify": {
            "consumes": ["application"],
            "purpose": "Validate archives before publishing a CLI result.",
            "handoff": "Read the validated archive result through its contract.",
        },
    }
    payload["modules"] = {"manifest_model": provider, "archive_verify": archive, "backup_cli": cli}
    for name, module in payload["modules"].items():
        module["definition"]["paths"] = {
            "contract_mode": "review_guarded",
            "contract_paths": [f"{name}.py"],
            "implementation_scopes": [{"kind": "file", "path": f"{name}.py"}],
            "reference_only": [],
        }
    payload["graph"]["sink"] = "backup_cli"
    payload["context"]["build_system"]["owner"] = "backup_cli"
    payload["requirements"]["decode_frames"]["owner"] = "manifest_model"
    payload["requirements"]["decode_frames"]["contract_path"] = ["manifest_model.decoded_frames"]
    scenario = payload["scenarios"]["decode_one_frame"]
    scenario["modules"] = list(payload["modules"])
    scenario["entrypoint"]["module"] = "backup_cli"
    graph = GraphCompiler().compile(
        validate_contract_payload(payload, definition=definition),
        graph_id=workflow_id,
        generation=1,
        bindings=GraphCompileBindings(
            producer=RoleBinding("profile", "coder"),
            checker=RoleBinding("profile", "verifier"),
            execution_adapter="software_git.v2",
        ),
        satellite_projector=FamilyGraphSatelliteProjector(
            specialization_id=definition.specialization_id,
            template=definition.graph_satellite_template,
        ),
        source_ref="representative-architecture.yaml",
        workspace_authority_rules=definition.workspace_authority_rules,
    )
    if not legacy_contract_edges:
        return graph
    # Model a persisted pre-verification-gates graph, not current compiler
    # policy. Keeping the serialized edge kinds preserves negative recovery
    # coverage: a visible contract stub is not an accepted checker product.
    legacy = replace(
        graph, edges=tuple(
            replace(edge, kind=EdgeKind.CONTRACT)
            if edge.consumer != graph.sink else edge
            for edge in graph.edges
        ),
    )
    return graph_ir_from_mapping(legacy.to_dict())


def _finding(kind: str, path: str, *, identity: str = "finding_stub", summary: str | None = None):
    return {
        "finding_id": identity,
        "finding_kind": kind,
        "priority": "p1",
        "disposition": "blocking",
        "summary": summary or "The manifest implementation is still a contract stub.",
        "locations": [{"scope": "workspace", "file": path, "line": 1}],
    }


class VerifierScopeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pal-verifier-scope-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = BunshinV2WorkflowService(self.root)
        self.repository = self.service.repository
        self.artifacts = self.service.artifacts
        self.coordinator = WorkflowCoordinator(self.repository)
        self.worker = SemanticOrchestrator(self.service)
        self.workflow_id = "workflow-scope"
        self.graph = _compiled_graph(self.workflow_id, legacy_contract_edges=True)
        self.coordinator.install_graph(workflow_id=self.workflow_id, graph=self.graph)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "regression@example.com")
        self._git("config", "user.name", "Regression")
        for name in self.graph.nodes:
            (self.repo / f"{name}.py").write_text("def run():\n    raise NotImplementedError\n", encoding="utf-8")
            corpus = self.repo / "tests" / name / "verifier"
            corpus.mkdir(parents=True)
            (corpus / "test_contract.py").write_text("def test_contract():\n    assert True\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-qm", "candidate")
        self.digest = self._git("rev-parse", "HEAD").strip()
        self.candidate = {
            "candidate_digest": self.digest,
            "candidate_tree_sha": self._git("rev-parse", "HEAD^{tree}").strip(),
            "changed_paths": ["archive_verify.py"],
        }
        self.candidate_ref = self.artifacts.put_json(self.candidate, artifact_type="CandidateSnapshotArtifact")
        self.contract_ref = self.artifacts.put_json({"contract": "archive"}, artifact_type="UnitContractArtifact")
        self.sequence = 0
        for name, spec in self.graph.nodes.items():
            self._dispatch(name, "CREATE_NODE_RUN", {
                "module_name": name,
                "node_kind": "unit",
                "graph_sink": spec.is_sink,
                "epoch_id": "epoch-scope",
                "unit_contract_ref": self.contract_ref.to_dict(),
                "execution_adapter": "software_git.v2",
                "workspace_path": str(self.repo),
                "path_policy": {**dict(spec.workspace_policy), "verification_corpus": {"kind": "directory", "path": f"tests/{name}/verifier"}},
                "dependency_node_ids": [self._node_id(x) for x in self.graph.checker_predecessors(name)],
                "producer_dependency_node_ids": [],
                "contract_dependency_node_ids": [self._node_id(edge.producer) for edge in self.graph.edges if edge.consumer == name and edge.kind == EdgeKind.CONTRACT],
                "candidate_cycle": 1,
            })
        self.fifo = _finding("module_defect", "archive_verify.py", identity="finding_fifo", summary="Archive FIFO open can block indefinitely before validation.")
        self.stub = _finding("dependency_defect", "manifest_model.py")

    def _git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True).stdout

    @staticmethod
    def _node_id(name):
        return "node-" + name

    def _node(self, name="archive_verify"):
        return self.repository.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, self._node_id(name))

    def _dispatch(self, name, action, payload=None):
        node = self._node(name)
        self.sequence += 1
        return self.repository.transitions.dispatch(ActionEnvelope(
            action_type=action, workflow_id=self.workflow_id,
            aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=self._node_id(name),
            actor="regression", expected_version=node.version if node else 0,
            idempotency_key=f"test:{self.sequence}:{name}:{action}", payload=payload or {},
        )).snapshot

    def _review_node(self, name="archive_verify", *, outputs=None):
        dependencies = list(self._node(name).payload.get("dependency_node_ids") or [])
        for action, payload in (
            ("DEPENDENCIES_ACCEPTED", {"accepted_producer_dependency_node_ids": [], "epoch_frozen": False}),
            ("START_PRODUCING", {"fencing_token": 1}),
            ("SUBMIT_CANDIDATE", {"fencing_token": 1}),
            ("QUIESCE_COMPLETED", {"fencing_token": 1, "process_group_reaped": True, "exclusive_workspace_lock": True, "workspace_fingerprint": "candidate-tree"}),
            ("CANDIDATE_SNAPSHOTTED", {"candidate_ref": self.candidate_ref.to_dict(), "candidate_digest": self.digest, "workspace_fingerprint": "candidate-tree"}),
            ("VERIFICATION_DEPENDENCIES_ACCEPTED", {"accepted_dependency_node_ids": dependencies, "dependency_outputs": outputs or {}, "epoch_frozen": False}),
            ("START_REVIEW", {"fencing_token": 2}),
            ("SUBMIT_SEMANTIC_VERIFICATION", {"pending_verification_ref": self.artifacts.put_json({"submission": self._submission(), "candidate_ref": self.candidate_ref.to_dict(), "candidate_digest": self.digest, "review_workspace": str(self.repo), "review_scratch": str(self.root / "scratch"), "execution_adapter": "software_git.v2"}, artifact_type="PendingSemanticVerificationArtifact").to_dict()}),
            ("VERIFIER_QUIESCED", {"fencing_token": 2, "process_group_reaped": True, "exclusive_workspace_lock": True, "workspace_fingerprint": "verification-tree"}),
        ):
            self._dispatch(name, action, payload)
        self.coordinator.start_assignment(workflow_id=self.workflow_id, node_name=name, slot=CycleSlot.PRODUCER, kind=AssignmentKind.INITIAL, input_fingerprint=name + "-input")
        self.coordinator.producer_submitted(workflow_id=self.workflow_id, node_name=name, product_ref=self.candidate_ref.sha256)
        self.coordinator.start_assignment(workflow_id=self.workflow_id, node_name=name, slot=CycleSlot.CHECKER, kind=AssignmentKind.INITIAL, input_fingerprint=name + "-check")
        return self._node(name)

    def _accept_provider(self, name="manifest_model"):
        node = self._review_node(name)
        report = self.artifacts.put_json({"status": "PASS"}, artifact_type="VerificationArtifact")
        VerificationService(self.repository, self.artifacts).submit_verdict(node=node, verification_ref=report, status=VerificationStatus.PASS, actor="regression")
        self.coordinator.checker_verdict(workflow_id=self.workflow_id, node_name=name, accepted=True)
        return self._node(name)

    def _submission(self, findings=None):
        return {
            "outcome": "module_repair", "target_modules": ["archive_verify", "manifest_model"],
            "findings": copy.deepcopy(findings if findings is not None else [self.fifo, self.stub]),
            "advisories": [],
            "tool_receipts": [{"kind": "command", "ok": False, "output_sha256": "reproduced-fifo-and-stub", "args": {"cmd": "python -B tests/archive_verify/verifier/test_contract.py"}}],
            "recorded_results": [{"name": "current candidate delta", "case_kind": "diff_risk", "status": "FAIL", "obligation_tags": ["candidate_delta_review"]}],
        }

    def _finalize(self, node, submission=None):
        return self.worker.components.verification_settlement.finalize_semantic_verification(
            node=node, pending={"invocation_id": "legacy-verifier", "lease_resource_key": "legacy-verifier", "fencing_token": 2},
            submission=submission or self._submission(), candidate_ref=self.candidate_ref,
            candidate_digest=self.digest, candidate=self.candidate, review_workspace=self.repo,
            review_scratch=self.root / "scratch", execution_adapter="software_git.v2",
        )

    def _workspace(self, name="archive_verify"):
        invocation = "preflight-" + name
        lease = self.repository.leases.claim_lease(invocation, invocation, ttl_seconds=120)
        corpus = {"kind": "directory", "path": f"tests/{name}/verifier"}
        workspace = {
            "runtime_root": str(self.root), "repo_path": str(self.repo),
            "artifact_dir": str(self.root / "artifacts-out"), "artifact_stage_dir": str(self.root / "stage"),
            "review_scratch_dir": str(self.root / "scratch"), "write_path_scopes": [corpus],
            "review_tool_evidence_refs": [],
            "bunshin_v2": {
                "workflow_id": self.workflow_id, "invocation_id": invocation,
                "lease_resource_key": invocation, "fencing_token": lease.fencing_token,
                "role": "verifier", "mode": "module", "authoring_input_fingerprint": invocation,
                "authoring_contract_version": AUTHORING_CONTRACT_VERSION,
                "swe_verification_tool_contract": compile_swe_verification_tool_contract(
                    {"module_name": name, "verification_corpus": corpus}, repair_scope=_verification_repair_scope(self.repository, self._node(name))),
            },
        }
        result = update_checklist_tool_result(new_tool_call(name="op_bunshin_update_checklist", args={"plan": [{"step": "check scope", "status": "completed"}]}), workspace)
        self.assertTrue(result.ok, result.llm_text)
        call = new_tool_call(name="op_exec_shell", args={"cmd": "true", "cwd": str(self.repo)})
        result = ToolExecutionResult(name=call.name, ok=True, text="ok", llm_text="ok", structured={"returncode": 0, "stdout": "", "stderr": ""}, status=RuntimeStatus.OK, call_id=call.call_id)
        record_verification_execution(workspace, call, result, verification_corpus_snapshot(workspace))
        return workspace

    def test_legacy_graph_keeps_visible_stub_out_of_checker_products(self):
        self._accept_provider()
        node = self._node()
        self.assertEqual(self.graph.checker_predecessors("archive_verify"), ())
        self.assertIn(self._node_id("manifest_model"), node.payload["contract_dependency_node_ids"])
        scope = _verification_repair_scope(self.repository, node)
        self.assertEqual(scope["dependency_modules"], [])
        self.assertEqual(scope["contract_only_modules"], ["manifest_model"])
        self.assertEqual(set(scope["repair_path_owners"]), {"archive_verify", "manifest_model"})
        self.assertFalse(scope["graph_sink"])

    def test_scope_requires_both_bound_product_fields_and_checker_edge(self):
        node = self._node("backup_cli")
        output = {"candidate_ref": self.candidate_ref.to_dict(), "candidate_digest": self.digest}
        for incomplete in ({}, {"candidate_ref": self.candidate_ref.to_dict()}, {"candidate_digest": self.digest}):
            with self.subTest(output=incomplete):
                bound = replace(node, payload={**node.payload, "dependency_outputs": {self._node_id("manifest_model"): incomplete}})
                self.assertEqual(_verification_repair_scope(self.repository, bound)["dependency_modules"], [])
        bound = replace(node, payload={**node.payload, "dependency_outputs": {self._node_id(x): output for x in ("archive_verify", "manifest_model")}})
        self.assertEqual(_verification_repair_scope(self.repository, bound)["dependency_modules"], ["archive_verify", "manifest_model"])
        self.assertEqual(_verification_repair_scope(self.repository, bound)["contract_only_modules"], [])
        # A stale/misbound payload cannot turn a contract edge into a checker edge.
        contract_node = self._node()
        forged = replace(contract_node, payload={**contract_node.payload, "dependency_node_ids": [self._node_id("manifest_model")], "dependency_outputs": {self._node_id("manifest_model"): output}})
        self.assertEqual(_verification_repair_scope(self.repository, forged)["dependency_modules"], [])

    def test_finding_kinds_require_their_specific_owners(self):
        scope = _verification_repair_scope(self.repository, self._node())
        self.assertEqual(verification_finding_route_errors([self.fifo], scope), [])
        for finding in (self.stub, _finding("dependency_defect", "archive_verify.py"), _finding("module_defect", "manifest_model.py"), _finding("sink_defect", "archive_verify.py"), _finding("module_defect", "unowned.py")):
            with self.subTest(finding=finding):
                self.assertTrue(verification_finding_route_errors([finding], scope))
        for kind in ("contract_defect", "architecture_defect", "requirements_defect"):
            with self.subTest(kind=kind):
                self.assertEqual(verification_finding_route_errors([_finding(kind, "manifest_model.py")], scope), [])

    def test_bound_dependency_with_multiple_owners_remains_valid(self):
        scope = _verification_repair_scope(self.repository, self._node("backup_cli"))
        scope["dependency_modules"] = ["archive_verify", "manifest_model"]
        scope["contract_only_modules"] = []
        finding = _finding("dependency_defect", "manifest_model.py")
        finding["locations"] += [{"scope": "workspace", "file": "archive_verify.py", "line": 1}, {"scope": "workspace", "file": "backup_cli.py", "line": 1}]
        self.assertEqual(verification_finding_route_errors([finding], scope), [])
        self.assertEqual(infer_repair_target_modules([finding], scope["repair_path_owners"]), ["archive_verify", "backup_cli", "manifest_model"])
        self.assertEqual(verification_finding_route_errors([_finding("sink_defect", "backup_cli.py")], scope), [])

    def test_shared_preflight_rejects_stub_without_freezing_draft_or_findings(self):
        workspace = self._workspace()
        for finding in (self.fifo, self.stub):
            authored = {key: value for key, value in finding.items() if key != "finding_id"}
            result = add_finding_tool_result(new_tool_call(name=ADD_FINDING_CAPABILITY, args=authored), workspace)
            self.assertTrue(result.ok, result.llm_text)
        context = SubmissionDraftContext.from_workspace(workspace, draft_kind="verification")
        store = SubmissionDraftStore(self.root)
        before = store.read(context, seed=empty_review_draft())
        saved_findings = findings_from_work_items(workspace)
        ready = verification_outcome_readiness(workspace, before.payload, outcome="module_repair")
        self.assertFalse(ready["ready"])
        self.assertTrue(any("bound" in item and "provider" in item for item in ready["blockers"]))
        result = swe_verification_tool_result(new_tool_call(name="op_bunshin_verification_request_module_repair", args={}), workspace, [])
        self.assertFalse(result.ok)
        self.assertIsInstance(result.invocation_result, RejectedResult)
        self.assertEqual(result.invocation_result.retry, RetryDirective.CORRECT_INPUT)
        self.assertEqual(result.invocation_result.effect, EffectOutcome.NOT_STARTED)
        after = store.read(context, seed=empty_review_draft())
        self.assertEqual(after.status, "active")
        self.assertEqual(after.version, before.version)
        self.assertEqual(after.payload, before.payload)
        self.assertEqual(findings_from_work_items(workspace), saved_findings)
        self.assertFalse((self.root / "stage" / "verification_submission.json").exists())
        errors = semantic_verification_submission_errors(
            self._submission(), work_view={"module_name": "archive_verify"}, changed_paths=[],
            current_case_paths=["tests/archive_verify/verifier/test_contract.py"], corpus_scope=workspace["write_path_scopes"][0],
            scratch_only=False, workspace=workspace,
        )
        self.assertTrue(any("bound" in item and "provider" in item for item in errors), errors)

    def test_legacy_invalid_submission_rechecks_without_reopening_provider(self):
        provider = self._accept_provider()
        node = self._review_node()
        test_path = self.repo / "tests/archive_verify/verifier/test_contract.py"
        test_path.write_text("def test_fifo_is_rejected_before_open():\n    assert True\n", encoding="utf-8")
        original = self._submission()
        original_copy = copy.deepcopy(original)
        result = self._finalize(node, original)
        updated = self._node()
        self.assertEqual(updated.state, "REVIEW_QUEUED")
        self.assertEqual(self._node("manifest_model"), provider)
        execution = self.coordinator.execution(workflow_id=self.workflow_id)
        self.assertEqual(execution.cycles["manifest_model"].state, NodeCycleState.ACCEPTED)
        self.assertEqual(execution.cycles["archive_verify"].state, NodeCycleState.CHECKER_READY)
        packet = self.artifacts.read_json(updated.payload["repair_bill_ref"])
        self.assertEqual(packet["route"], "verification_correction")
        self.assertEqual(packet["classification"], "invalid_verifier_submission")
        self.assertTrue(packet["routing_errors"])
        self.assertEqual(packet["original_outcome"], "module_repair")
        self.assertEqual(packet["original_target_modules"], original["target_modules"])
        self.assertEqual(packet["findings"], structured_findings(original))
        self.assertEqual(packet["source_pending_verification_ref"], node.payload["pending_verification_ref"])
        self.assertEqual(self.artifacts.read_json(packet["source_pending_verification_ref"])["submission"], self._submission())
        self.assertEqual(packet["changed_test_paths"], ["tests/archive_verify/verifier/test_contract.py"])
        self.assertEqual(self.artifacts.read_json(packet["tool_receipts_ref"])["receipts"], original["tool_receipts"])
        report = self.artifacts.read_json(result["result_artifact_ref"])
        self.assertEqual(report["outcome"], "module_repair")
        self.assertEqual(report["findings"], structured_findings(original))
        self.assertEqual(original, original_copy)
        checkpoint = self.artifacts.read_json(updated.payload["candidate_ref"])
        self.assertIn("tests/archive_verify/verifier/test_contract.py", checkpoint["verifier_test_paths"])
        self.assertEqual(self._git("show", updated.payload["candidate_digest"] + ":tests/archive_verify/verifier/test_contract.py"), test_path.read_text())
        self.assertEqual(updated.payload["verification_correction_attempts"], 1)
        self.assertEqual(updated.payload["verification_correction_cycle"], updated.payload["candidate_cycle"])

    def test_failed_correction_commit_rolls_back_graph_and_node_together(self):
        self._accept_provider()
        node = self._review_node()
        execution = self.coordinator.execution(workflow_id=self.workflow_id)
        with patch.object(VerificationService, "submit_verdict", side_effect=RuntimeError("injected verdict failure")):
            with self.assertRaisesRegex(RuntimeError, "injected verdict failure"):
                self._finalize(node)
        self.assertEqual(self._node(), node)
        self.assertEqual(self.coordinator.execution(workflow_id=self.workflow_id), execution)
        self._finalize(node)
        self.assertEqual(self._node().state, "REVIEW_QUEUED")

    def test_correction_reference_changes_identity_and_preserves_findings(self):
        self._accept_provider()
        self._finalize(self._review_node())
        node = self._node()
        work_ref = self.artifacts.put_json({"module_name": "archive_verify"}, artifact_type="ModuleWorkViewArtifact")
        diff_ref = self.artifacts.put_json({"changed_paths": ["archive_verify.py"]}, artifact_type="CandidateDiffArtifact")
        arguments = {"artifacts": self.artifacts, "module_work_view_ref": work_ref, "candidate_diff_ref": diff_ref}
        before = _verifier_reference_refs(node_payload={}, **arguments)
        after = _verifier_reference_refs(node_payload=node.payload, **arguments)
        self.assertIn("repair_bill", after)
        self.assertNotEqual(stable_hash({key: ref.to_dict() for key, ref in before.items()}), stable_hash({key: ref.to_dict() for key, ref in after.items()}))
        repair = self.artifacts.read_json(after["repair_bill"])
        self.assertEqual(repair["route"], "verification_correction")
        self.assertEqual(repair["findings"], structured_findings(self._submission()))
        self.assertTrue(repair["routing_errors"])
        self.assertNotIn("verification_ref", repair)
        self.assertNotIn("tool_receipts_ref", repair)

    def _recheck(self, *, attempt, submission):
        name = "archive_verify"
        pending_ref = self.artifacts.put_json({"submission": submission, "attempt": attempt}, artifact_type="PendingSemanticVerificationArtifact")
        self._dispatch(name, "START_REVIEW", {"fencing_token": attempt + 2})
        self._dispatch(name, "SUBMIT_SEMANTIC_VERIFICATION", {"pending_verification_ref": pending_ref.to_dict()})
        self._dispatch(name, "VERIFIER_QUIESCED", {"fencing_token": attempt + 2, "process_group_reaped": True, "exclusive_workspace_lock": True, "workspace_fingerprint": "verification-tree"})
        self.coordinator.start_assignment(workflow_id=self.workflow_id, node_name=name, slot=CycleSlot.CHECKER, kind=AssignmentKind.RECHECK, input_fingerprint=f"corrected-{attempt}")
        return self._node()

    def test_correction_retry_bound_counts_changed_findings_per_candidate_cycle(self):
        from pal.bunshin.verification import verification_correction_count

        provider = self._accept_provider()
        node = self._review_node()
        for attempt in range(1, 4):
            submission = self._submission()
            submission["findings"][1]["summary"] += f" Claim version {attempt}."
            if attempt > 1:
                node = self._recheck(attempt=attempt, submission=submission)
            self._finalize(node, submission)
            updated = self._node()
            self.assertEqual(updated.payload["verification_correction_attempts"], attempt)
            self.assertEqual(updated.state, "REVIEW_QUEUED" if attempt < 3 else "TRIAGE_REQUIRED")
            self.assertEqual(self._node("manifest_model"), provider)
        self.assertEqual(updated.payload["blocker"]["kind"], "invalid_verifier_submission")
        self.assertEqual(self.coordinator.execution(workflow_id=self.workflow_id).cycles["archive_verify"].state, NodeCycleState.TRIAGE_REQUIRED)
        next_candidate = replace(updated, payload={**updated.payload, "candidate_cycle": int(updated.payload["candidate_cycle"]) + 1})
        self.assertEqual(verification_correction_count(next_candidate), 1)

    def test_replaying_a_durable_correction_does_not_advance_state_or_counter(self):
        self._accept_provider()
        node = self._review_node()
        first = self._finalize(node)
        updated = self._node()
        graph = self.coordinator.execution(workflow_id=self.workflow_id)
        replay = self._finalize(node)
        self.assertEqual(replay, first)
        self.assertEqual(self._node(), updated)
        self.assertEqual(self.coordinator.execution(workflow_id=self.workflow_id), graph)
        self.assertEqual(self._node().payload["verification_correction_attempts"], 1)

    def test_valid_mixed_batch_routes_only_bound_providers_and_projects_owner_views(self):
        # This legacy handler fixture deliberately has no admitted worker or
        # durable role receipt. Full cohort/public-outbox ownership is covered
        # in test_bunshin_dependency_repair_runtime, rather than fabricated here.
        self.worker.components.verification_settlement.dependency_repair_registration = None
        from pal.bunshin.verification import repair_bill_semantic_view

        self._accept_provider("manifest_model")
        self._accept_provider("archive_verify")
        outputs = {self._node_id(name): {"candidate_ref": self.candidate_ref.to_dict(), "candidate_digest": self.digest} for name in ("manifest_model", "archive_verify")}
        node = self._review_node("backup_cli", outputs=outputs)
        current = _finding("module_defect", "backup_cli.py", identity="finding_cli", summary="CLI drops a verified error code.")
        manifest = _finding("dependency_defect", "manifest_model.py", identity="finding_manifest", summary="Bound provider accepts an invalid manifest path.")
        archive = _finding("dependency_defect", "archive_verify.py", identity="finding_archive", summary="Bound provider opens a FIFO before rejecting it.")
        # A provider finding may cite its consumer too, without targeting that consumer.
        manifest["locations"].append({"scope": "workspace", "file": "backup_cli.py", "line": 2})
        submission = self._submission([current, manifest, archive])
        submission["target_modules"] = ["archive_verify", "backup_cli", "manifest_model"]
        self._finalize(node, submission)
        updated = self._node("backup_cli")
        self.assertEqual(updated.state, "STALE")
        self.assertEqual(updated.payload["repair_target_node_ids"], [self._node_id("archive_verify"), self._node_id("manifest_model")])
        execution = self.coordinator.execution(workflow_id=self.workflow_id)
        self.assertEqual(execution.cycles["archive_verify"].state, NodeCycleState.REPAIR_READY)
        self.assertEqual(execution.cycles["manifest_model"].state, NodeCycleState.REPAIR_READY)
        packet_ref = updated.payload["repair_bill_ref"]
        packet = self.artifacts.read_json(packet_ref)
        self.assertEqual(packet["target_modules"], ["archive_verify", "manifest_model"])
        self.assertEqual(packet["findings"], structured_findings(submission))
        for owner, expected_id in (("backup_cli", "finding_cli"), ("manifest_model", "finding_manifest"), ("archive_verify", "finding_archive")):
            with self.subTest(owner=owner):
                view = repair_bill_semantic_view(self.artifacts, packet_ref, module_name=owner)
                self.assertEqual([item["finding_id"] for item in view["findings"]], [expected_id])
                self.assertEqual(len(view["related_findings"]), 2)
        self.assertEqual(self.artifacts.read_json(packet_ref), packet)

    def test_current_only_implementation_repair_does_not_inherit_submitted_stub_target(self):
        provider = self._accept_provider()
        node = self._review_node()
        # Durable target lists are not authority; derive the route from findings.
        self._finalize(node, self._submission([self.fifo]))
        self.assertEqual(self._node().state, "REPAIR_QUEUED")
        self.assertEqual(self._node("manifest_model"), provider)
        packet = self.artifacts.read_json(self._node().payload["repair_bill_ref"])
        self.assertEqual(packet["target_modules"], ["archive_verify"])
        self.assertEqual(packet["route"], "module_repair")

    def test_contract_only_finding_uses_revision_route_without_stub_repair(self):
        provider = self._accept_provider()
        node = self._review_node()
        submission = self._submission([_finding("contract_defect", "manifest_model.py", summary="The declared manifest shape is contradictory.")])
        submission["outcome"] = "contract_revision"
        self._finalize(node, submission)
        self.assertEqual(self._node().state, "STALE")
        self.assertEqual(self._node("manifest_model"), provider)
        packet = self.artifacts.read_json(self._node().payload["repair_bill_ref"])
        self.assertEqual(packet["route"], "contract_revision")
        self.assertEqual(packet["target_modules"], [])
        self.assertNotIn("routing_errors", packet)

    def test_correction_preserves_out_of_corpus_invariant_guard(self):
        self._accept_provider()
        node = self._review_node()
        execution = self.coordinator.execution(workflow_id=self.workflow_id)
        (self.repo / "archive_verify.py").write_text("# unauthorized verifier product edit\n", encoding="utf-8")
        with self.assertRaisesRegex(SubmissionInvariantError, "outside the bound module corpus"):
            self._finalize(node)
        self.assertEqual(self._node(), node)
        self.assertEqual(self.coordinator.execution(workflow_id=self.workflow_id), execution)

    def test_correction_findings_seed_required_next_verifier_tasks(self):
        from pal.bunshin.workflow_catalog import BunshinV2Catalog
        from pal.bunshin.role_contracts import OrchestrationRole, RoleActivation, RoleMode
        from pal.bunshin.semantic_orchestration.attempt_models import RoleAttemptRequest
        from pal.bunshin.semantic_orchestration.attempt_playbook_binding import PlaybookBinding
        from pal.bunshin.semantic_orchestration.attempt_prompt_construction import PromptConstruction
        from pal.bunshin.semantic_orchestration.attempt_reference_binding import ReferenceBinding
        from pal.bunshin.semantic_orchestration.attempt_verifier_context import VerifierContext
        from pal.bunshin.semantic_orchestration.workflow_facts import WorkflowFacts
        from pal.bunshin.task_ledger import TaskLedgerService

        self._accept_provider()
        self._finalize(self._review_node())
        node = self._node()
        work_ref = self.artifacts.put_json({"module_name": "archive_verify", "graph_sink": False}, artifact_type="ModuleWorkViewArtifact")
        diff_ref = self.artifacts.put_json({"changed_paths": ["archive_verify.py"]}, artifact_type="CandidateDiffArtifact")
        refs = _verifier_reference_refs(artifacts=self.artifacts, node_payload=node.payload, module_work_view_ref=work_ref, candidate_diff_ref=diff_ref)
        binding_ref = BunshinV2Catalog(self.root, self.artifacts).publish_family_binding("software_engineering.v2_coder")
        binding = dict(self.artifacts.read_json(binding_ref))
        lease = self.repository.leases.claim_lease("correction-prompt", "next-verifier", ttl_seconds=120)
        command = RoleAttemptRequest(
            effect={"effect_id": "correction-prompt"}, snapshot=node,
            invocation_id="next-verifier", lease_resource=lease.resource_key, fencing_token=lease.fencing_token,
            profile="software_engineering.v2_verifier", activation=RoleActivation(OrchestrationRole.VERIFIER, RoleMode.MODULE),
            instruction="Reevaluate the preserved findings against the bound repair scope.", reference_refs=refs,
            workspace_override=None, prepare_workspace=False,
        )
        prepared = SimpleNamespace(
            binding=binding, binding_ref=binding_ref.to_dict(), bound_input_entries=[], bound_reference_refs=refs,
            contract_authoring=False, family_policies=dict(binding.get("policies") or {}), llm_policy={},
            mode="module", role="verifier", workspace={"runtime_root": str(self.root), "repo_path": str(self.repo)},
        )
        facts = WorkflowFacts(self.artifacts, self.repository)

        async def bind():
            verifier = await VerifierContext(self.artifacts).execute(command, prepared)
            references = await ReferenceBinding(self.repository, TaskLedgerService(self.root, self.artifacts), facts).execute(command, prepared)
            prompt = await PromptConstruction(facts).execute(command, references, verifier, prepared)
            return await PlaybookBinding(self.artifacts).execute(command, prompt, prepared)

        pack = asyncio.run(bind()).pack
        seeds = pack.metadata["bunshin_v2"]["work_item_seed"]
        tasks = [item for item in seeds if item["origin"] == "manager_routed_finding"]
        self.assertEqual({item["summary"] for item in tasks}, {"resolve finding: finding_fifo", "resolve finding: finding_stub"})
        self.assertTrue(all(item["required"] and item["status"] == "pending" for item in tasks))

    def test_stale_node_version_and_candidate_cannot_enter_correction(self):
        self._accept_provider()
        node = self._review_node()
        graph = self.coordinator.execution(workflow_id=self.workflow_id)
        with self.assertRaisesRegex(SubmissionInvariantError, "current node version"):
            self._finalize(replace(node, version=node.version - 1))
        original_digest = self.digest
        self.digest = "stale-candidate"
        try:
            with self.assertRaisesRegex(SubmissionInvariantError, "candidate no longer matches"):
                self._finalize(node)
        finally:
            self.digest = original_digest
        self.assertEqual(self._node(), node)
        self.assertEqual(self.coordinator.execution(workflow_id=self.workflow_id), graph)

    def test_quiesce_preserves_live_worker_fence_before_correction(self):
        from pal.bunshin.contracts import StaleFencingToken

        self._accept_provider()
        node = self._review_node()
        invocation = "fenced-verifier"
        first = self.repository.leases.claim_lease(invocation, invocation, ttl_seconds=120)
        self.repository.leases.release_lease(invocation, invocation, first.fencing_token)
        replacement = self.repository.leases.claim_lease(invocation, invocation, ttl_seconds=120)
        pending_ref = self.artifacts.put_json({
            "invocation_id": invocation, "lease_resource_key": invocation,
            "fencing_token": first.fencing_token, "review_workspace": str(self.repo),
            "submission": self._submission(),
        }, artifact_type="PendingSemanticVerificationArtifact")
        stale = replace(node, state="REVIEW_QUIESCING", payload={**node.payload, "pending_verification_ref": pending_ref.to_dict()})
        snapshot = self.worker.components.verification_snapshot
        with patch.object(snapshot.effect_reads, "effect_snapshot", return_value=stale), patch.object(snapshot.role_cleanup, "close_owned_process", new_callable=AsyncMock) as close:
            with self.assertRaises(StaleFencingToken):
                asyncio.run(snapshot.quiesce_verifier_role(self._effect(stale, effect_type="quiesce_verifier_role")))
            close.assert_not_awaited()
        self.assertEqual(self.repository.leases.read_lease(invocation)["fencing_token"], replacement.fencing_token)
        self.assertEqual(self._node(), node)

    def test_workspace_staleness_guard_precedes_correction_recovery(self):
        from pal.bunshin.execution_values import workspace_content_fingerprint

        self._accept_provider()
        node = self._review_node()
        frozen = replace(node, payload={**node.payload, "workspace_fingerprint": workspace_content_fingerprint(self.repo)})
        (self.repo / "tests/archive_verify/verifier/test_contract.py").write_text("# late edit\n", encoding="utf-8")
        snapshot = self.worker.components.verification_snapshot
        with patch.object(snapshot.effect_reads, "effect_snapshot", return_value=frozen), patch.object(snapshot.verification_settlement, "finalize_semantic_verification") as finalize:
            with self.assertRaisesRegex(RuntimeError, "changed after quiescing"):
                snapshot.snapshot_semantic_verification(self._effect(frozen))
            finalize.assert_not_called()
        self.assertEqual(self._node(), node)

    def test_snapshot_replay_uses_receipt_even_after_worktree_is_removed(self):
        self._accept_provider()
        first = self._finalize(self._review_node())
        updated = self._node()
        self.repo.rename(self.root / "retired-worktree")
        replay = self.worker.components.verification_snapshot.snapshot_semantic_verification(self._effect(updated))
        self.assertEqual(replay, first)
        self.assertEqual(self._node(), updated)

    def _effect(self, node, *, effect_type="snapshot_verifier_result"):
        return {
            "effect_key": "synthetic:" + str(node.version),
            "effect_type": effect_type,
            "aggregate_type": AggregateType.DAG_NODE_RUN.value,
            "aggregate_id": node.aggregate_id,
            "payload": {"_causal_context": {"pending_verification_ref": dict(node.payload["pending_verification_ref"])}},
        }

    def _stored_effect(self, node, *, effect_type="snapshot_verifier_result"):
        with self.repository.database.read_connection() as connection:
            row = connection.execute(
                "SELECT o.* FROM bunshin_v2_outbox AS o JOIN bunshin_v2_domain_events AS e ON e.event_id = o.event_id "
                "WHERE o.aggregate_id = ? AND o.effect_type = ? ORDER BY e.aggregate_version DESC LIMIT 1",
                (node.aggregate_id, effect_type),
            ).fetchone()
        self.assertIsNotNone(row)
        effect = dict(row)
        effect["payload"] = json.loads(effect.pop("payload_json"))
        return effect

    def _two_corrections(self):
        self._accept_provider()
        first_node = self._review_node()
        first_effect = self._stored_effect(first_node)
        first = self._finalize(first_node)
        submission = self._submission()
        submission["findings"][1]["summary"] += " Rechecked claim."
        second_node = self._recheck(attempt=2, submission=submission)
        second = self._finalize(second_node, submission)
        self.assertNotEqual(first["result_artifact_ref"], second["result_artifact_ref"])
        return first_node, first_effect, first, second

    def test_delayed_replay_returns_first_committed_report_after_second_correction(self):
        first_node, first_effect, first, second = self._two_corrections()
        updated = self._node()
        graph = self.coordinator.execution(workflow_id=self.workflow_id)
        self.assertEqual(updated.payload["verification_correction_attempts"], 2)
        self.assertEqual(updated.payload["verification_artifact_ref"], second["result_artifact_ref"])
        self.assertEqual(self.worker.components.verification_snapshot.snapshot_semantic_verification(first_effect), first)
        self.assertEqual(self._finalize(first_node), first)
        self.assertEqual(asyncio.run(self.worker.components.verification_snapshot.quiesce_verifier_role(first_effect)), first)
        self.assertEqual(self._node(), updated)
        self.assertEqual(self.coordinator.execution(workflow_id=self.workflow_id), graph)

    def test_legacy_event_fallback_binds_old_submit_before_newer_pending(self):
        first_node, first_effect, first, _ = self._two_corrections()
        expected = first_node.payload["pending_verification_ref"]
        self.assertEqual(self.repository.queries.read_effect_pending_verification_ref(first_effect["event_id"]), expected)
        legacy = {**first_effect, "payload": {}}
        updated = self._node()
        replay = self.worker.components.verification_snapshot.snapshot_semantic_verification(legacy)
        self.assertEqual(replay, first)
        self.assertEqual(self._node(), updated)
        self.assertEqual(self.repository.queries.read_verification_settlement_ref(first_node.aggregate_id, expected["sha256"]), first["result_artifact_ref"])

    def test_stored_effect_context_suppresses_delayed_work_before_newer_snapshot_settles(self):
        self._accept_provider()
        first_node = self._review_node()
        old_effect = self._stored_effect(first_node)
        self.assertEqual(old_effect["payload"]["_causal_context"]["pending_verification_ref"], first_node.payload["pending_verification_ref"])
        quiesce = self._stored_effect(first_node, effect_type="quiesce_verifier_role")
        self.assertEqual(quiesce["payload"]["_causal_context"]["pending_verification_ref"], first_node.payload["pending_verification_ref"])
        first = self._finalize(first_node)
        submission = self._submission()
        submission["findings"][1]["summary"] += " Current second attempt."
        second_node = self._recheck(attempt=2, submission=submission)
        graph = self.coordinator.execution(workflow_id=self.workflow_id)
        stale = asyncio.run(self.worker.execute_semantic_effect(old_effect))
        self.assertEqual(stale["status"], "superseded")
        self.assertEqual(self._node(), second_node)
        self.assertEqual(self.coordinator.execution(workflow_id=self.workflow_id), graph)
        second = self._finalize(second_node, submission)
        updated = self._node()
        stale = asyncio.run(self.worker.execute_semantic_effect(old_effect))
        self.assertEqual(stale["status"], "superseded")
        self.assertEqual(self._node(), updated)
        self.assertEqual(updated.payload["verification_artifact_ref"], second["result_artifact_ref"])
        self.assertEqual(self.worker.components.verification_snapshot.snapshot_semantic_verification(old_effect), first)

    def test_uncommitted_pending_cannot_settle_or_quiesce_newer_submission(self):
        self._accept_provider()
        uncommitted = self.artifacts.put_json({"submission": self._submission(), "attempt": "abandoned"}, artifact_type="PendingSemanticVerificationArtifact")
        node = self._review_node()
        old = replace(node, version=node.version - 1, payload={**node.payload, "pending_verification_ref": uncommitted.to_dict()})
        snapshot = self.worker.components.verification_snapshot
        graph = self.coordinator.execution(workflow_id=self.workflow_id)
        self.assertEqual(self.repository.queries.read_verification_settlement_ref(node.aggregate_id, uncommitted.sha256), {})
        for method in ("snapshot", "quiesce"):
            with self.subTest(method=method), self.assertRaisesRegex(SubmissionInvariantError, "superseded pending submission"):
                if method == "snapshot":
                    snapshot.snapshot_semantic_verification(self._effect(old))
                else:
                    asyncio.run(snapshot.quiesce_verifier_role(self._effect(old, effect_type="quiesce_verifier_role")))
        with self.assertRaisesRegex(SubmissionInvariantError, "current node version"):
            self._finalize(old)
        self.assertEqual(self._node(), node)
        self.assertEqual(self.coordinator.execution(workflow_id=self.workflow_id), graph)

    def test_pending_without_causal_identity_fails_closed(self):
        self._accept_provider()
        node = self._review_node()
        effect = self._effect(node)
        effect["payload"] = {}
        with self.assertRaisesRegex(SubmissionInvariantError, "no causal pending submission"):
            self.worker.components.verification_snapshot.snapshot_semantic_verification(effect)
        self.assertEqual(self._node(), node)

    def _reaccept_repaired_provider(self, name):
        for action, payload in (
            ("START_REPAIR", {"fencing_token": 3}),
            ("SUBMIT_CANDIDATE", {"fencing_token": 3}),
            ("QUIESCE_COMPLETED", {"fencing_token": 3, "process_group_reaped": True, "exclusive_workspace_lock": True, "workspace_fingerprint": "repaired-tree"}),
            ("CANDIDATE_SNAPSHOTTED", {"candidate_ref": self.candidate_ref.to_dict(), "candidate_digest": self.digest, "workspace_fingerprint": "repaired-tree"}),
            ("VERIFICATION_DEPENDENCIES_ACCEPTED", {"accepted_dependency_node_ids": [], "epoch_frozen": False}),
            ("START_REVIEW", {"fencing_token": 4}),
            ("SUBMIT_SEMANTIC_VERIFICATION", {"pending_verification_ref": self.artifacts.put_json({"repaired": name}, artifact_type="PendingSemanticVerificationArtifact").to_dict()}),
            ("VERIFIER_QUIESCED", {"fencing_token": 4, "process_group_reaped": True, "exclusive_workspace_lock": True, "workspace_fingerprint": "repaired-review-tree"}),
        ):
            self._dispatch(name, action, payload)
        report = self.artifacts.put_json({"status": "PASS", "repaired": name}, artifact_type="VerificationArtifact")
        VerificationService(self.repository, self.artifacts).submit_verdict(node=self._node(name), verification_ref=report, status=VerificationStatus.PASS, actor="regression")

    def test_aggregate_provider_batch_preserves_targets_and_replay_after_reacceptance(self):
        from pal.bunshin.verification import DefectPropagationService

        self._accept_provider("manifest_model")
        self._accept_provider("archive_verify")
        self.assertIn(self._node_id("manifest_model"), self._node().payload["contract_dependency_node_ids"])
        repair_ref = self.artifacts.put_json({"findings": [self.fifo, self.stub]}, artifact_type="RepairPacketArtifact")
        service = DefectPropagationService(self.repository)
        arguments = dict(workflow_id=self.workflow_id, epoch_id="epoch-scope", dependency_node_ids=[self._node_id("archive_verify"), self._node_id("manifest_model")], repair_bill_ref=repair_ref)
        affected = service.propagate_dependency_defects(**arguments)
        self.assertEqual(affected, (self._node_id("backup_cli"),))
        # archive_verify is also a semantic consumer of manifest_model, but it
        # is an explicit repair target and must not be overwritten as STALE.
        for name in ("archive_verify", "manifest_model"):
            self.assertEqual(self._node(name).state, "REPAIR_QUEUED")
            self.assertEqual(self._node(name).payload["repair_bill_ref"], repair_ref.to_dict())
        self.assertEqual(self._node("backup_cli").state, "STALE")
        before = self.repository.queries.list_workflow_snapshots(self.workflow_id)
        self.assertEqual(service.propagate_dependency_defects(**arguments), affected)
        self.assertEqual(self.repository.queries.list_workflow_snapshots(self.workflow_id), before)
        for name in ("archive_verify", "manifest_model"):
            self._reaccept_repaired_provider(name)
        reaccepted = self.repository.queries.list_workflow_snapshots(self.workflow_id)
        self.assertEqual(service.propagate_dependency_defects(**arguments), affected)
        self.assertEqual(self.repository.queries.list_workflow_snapshots(self.workflow_id), reaccepted)
        self.assertTrue(all(self._node(name).state == "ACCEPTED" for name in ("archive_verify", "manifest_model")))

    def test_aggregate_provider_batch_rolls_back_first_reopen_if_later_target_rejects(self):
        from pal.bunshin.verification import DefectPropagationService

        self._accept_provider("archive_verify")
        self.assertEqual(self._node("manifest_model").state, "BLOCKED_BY_DEPS")
        repair_ref = self.artifacts.put_json({"findings": [self.fifo, self.stub]}, artifact_type="RepairPacketArtifact")
        before = self.repository.queries.list_workflow_snapshots(self.workflow_id)
        with self.repository.database.read_connection() as connection:
            effects_before = connection.execute("SELECT count(*) FROM bunshin_v2_outbox").fetchone()[0]
        with self.assertRaisesRegex(ValueError, "repair target is not accepted: node-manifest_model"):
            DefectPropagationService(self.repository).propagate_dependency_defects(
                workflow_id=self.workflow_id, epoch_id="epoch-scope",
                dependency_node_ids=[self._node_id("archive_verify"), self._node_id("manifest_model")], repair_bill_ref=repair_ref,
            )
        self.assertEqual(self.repository.queries.list_workflow_snapshots(self.workflow_id), before)
        with self.repository.database.read_connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM bunshin_v2_outbox").fetchone()[0], effects_before)

    def test_delayed_provider_packet_replay_does_not_overwrite_a_newer_repair(self):
        from pal.bunshin.verification import DefectPropagationService

        for name in ("manifest_model", "archive_verify"):
            self._accept_provider(name)
        service = DefectPropagationService(self.repository)
        targets = [self._node_id("archive_verify"), self._node_id("manifest_model")]
        first_ref = self.artifacts.put_json({"batch": "first", "findings": [self.fifo, self.stub]}, artifact_type="RepairPacketArtifact")
        second_ref = self.artifacts.put_json({"batch": "second", "findings": [self.fifo, self.stub]}, artifact_type="RepairPacketArtifact")
        arguments = dict(workflow_id=self.workflow_id, epoch_id="epoch-scope", dependency_node_ids=targets)
        affected = service.propagate_dependency_defects(**arguments, repair_bill_ref=first_ref)
        for name in ("archive_verify", "manifest_model"):
            self._reaccept_repaired_provider(name)
        self.assertEqual(service.propagate_dependency_defects(**arguments, repair_bill_ref=second_ref), affected)
        for name in ("archive_verify", "manifest_model"):
            self.assertEqual(self._node(name).payload["repair_bill_ref"], second_ref.to_dict())
            self.assertTrue(self.repository.queries.has_dependency_repair_receipt(self._node_id(name), first_ref.sha256, "REOPEN_DEPENDENCY"))
        newer_repair = self.repository.queries.list_workflow_snapshots(self.workflow_id)
        self.assertEqual(service.propagate_dependency_defects(**arguments, repair_bill_ref=first_ref), affected)
        self.assertEqual(self.repository.queries.list_workflow_snapshots(self.workflow_id), newer_repair)
        for name in ("archive_verify", "manifest_model"):
            self._reaccept_repaired_provider(name)
        reaccepted = self.repository.queries.list_workflow_snapshots(self.workflow_id)
        self.assertEqual(service.propagate_dependency_defects(**arguments, repair_bill_ref=first_ref), affected)
        self.assertEqual(self.repository.queries.list_workflow_snapshots(self.workflow_id), reaccepted)

    def test_current_finding_survives_stale_requeue_and_only_its_coder_task_is_required(self):
        # This legacy handler fixture deliberately has no admitted worker or
        # durable role receipt. Full cohort/public-outbox ownership is covered
        # in test_bunshin_dependency_repair_runtime, rather than fabricated here.
        self.worker.components.verification_settlement.dependency_repair_registration = None
        from pal.bunshin.workflow_catalog import BunshinV2Catalog
        from pal.bunshin.role_contracts import OrchestrationRole, RoleActivation, RoleMode
        from pal.bunshin.semantic_orchestration.attempt_models import RoleAttemptRequest
        from pal.bunshin.semantic_orchestration.attempt_playbook_binding import PlaybookBinding
        from pal.bunshin.semantic_orchestration.attempt_prompt_construction import PromptConstruction
        from pal.bunshin.semantic_orchestration.attempt_reference_binding import ReferenceBinding
        from pal.bunshin.semantic_orchestration.attempt_verifier_context import VerifierContext
        from pal.bunshin.semantic_orchestration.workflow_facts import WorkflowFacts
        from pal.bunshin.task_ledger import TaskLedgerService
        from pal.bunshin.verification import DefectPropagationService, repair_bill_semantic_view

        for name in ("manifest_model", "archive_verify"):
            self._accept_provider(name)
        outputs = {self._node_id(name): {"candidate_ref": self.candidate_ref.to_dict(), "candidate_digest": self.digest} for name in ("manifest_model", "archive_verify")}
        current = _finding("module_defect", "backup_cli.py", identity="finding_cli", summary="CLI loses the verified error code.")
        submission = self._submission([current, self.stub, _finding("dependency_defect", "archive_verify.py", identity="finding_archive")])
        self._finalize(self._review_node("backup_cli", outputs=outputs), submission)
        node = self._node("backup_cli")
        self.assertEqual(node.state, "STALE")
        packet_ref = node.payload["repair_bill_ref"]
        from pal.bunshin.artifacts import ArtifactRef
        DefectPropagationService(self.repository).propagate_dependency_defects(
            workflow_id=self.workflow_id, epoch_id="epoch-scope",
            dependency_node_ids=[self._node_id("archive_verify"), self._node_id("manifest_model")],
            repair_bill_ref=ArtifactRef(**packet_ref),
        )
        for name in ("archive_verify", "manifest_model"):
            self._reaccept_repaired_provider(name)
        node = self._dispatch("backup_cli", "REQUEUE_STALE", {
            "unit_contract_ref": self.contract_ref.to_dict(), "dependency_fingerprint": "repaired-provider-baseline",
            "accepted_producer_dependency_node_ids": [], "epoch_frozen": False,
        })
        self.assertEqual(node.state, "QUEUED")
        self.assertEqual(node.payload["repair_bill_ref"], packet_ref)
        view = repair_bill_semantic_view(self.artifacts, packet_ref, module_name="backup_cli")
        self.assertEqual([item["finding_id"] for item in view["findings"]], ["finding_cli"])
        self.assertEqual({item["finding_id"] for item in view["related_findings"]}, {"finding_stub", "finding_archive"})
        refs = {
            "module_work_view": self.artifacts.put_json({"module_name": "backup_cli"}, artifact_type="ModuleWorkViewArtifact"),
            "repair_bill": self.artifacts.put_json(view, artifact_type="RepairBillSemanticViewArtifact"),
        }
        binding_ref = BunshinV2Catalog(self.root, self.artifacts).publish_family_binding("software_engineering.v2_coder")
        binding = dict(self.artifacts.read_json(binding_ref))
        lease = self.repository.leases.claim_lease("requeued-coder", "next-coder", ttl_seconds=120)
        command = RoleAttemptRequest(
            effect={"effect_id": "requeued-coder"}, snapshot=node,
            invocation_id="next-coder", lease_resource=lease.resource_key, fencing_token=lease.fencing_token,
            profile="software_engineering.v2_coder", activation=RoleActivation(OrchestrationRole.IMPLEMENTATION, RoleMode.PRODUCE),
            instruction="Repair the current module against the refreshed providers.", reference_refs=refs,
            workspace_override=None, prepare_workspace=False,
        )
        prepared = SimpleNamespace(
            binding=binding, binding_ref=binding_ref.to_dict(), bound_input_entries=[], bound_reference_refs=refs,
            contract_authoring=False, family_policies=dict(binding.get("policies") or {}), llm_policy={},
            mode="produce", role="implementation", workspace={"runtime_root": str(self.root), "repo_path": str(self.repo)},
        )
        facts = WorkflowFacts(self.artifacts, self.repository)

        async def bind():
            verifier = await VerifierContext(self.artifacts).execute(command, prepared)
            references = await ReferenceBinding(self.repository, TaskLedgerService(self.root, self.artifacts), facts).execute(command, prepared)
            prompt = await PromptConstruction(facts).execute(command, references, verifier, prepared)
            return await PlaybookBinding(self.artifacts).execute(command, prompt, prepared)

        pack = asyncio.run(bind()).pack
        tasks = [item for item in pack.metadata["bunshin_v2"]["work_item_seed"] if item["origin"] == "manager_routed_finding"]
        self.assertEqual([item["summary"] for item in tasks], ["resolve finding: finding_cli"])
        self.assertTrue(tasks[0]["required"])
        self.assertEqual(tasks[0]["status"], "pending")
