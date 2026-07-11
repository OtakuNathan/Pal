from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pal.minion.v2 import ActionEnvelope, AggregateType, ContentAddressedArtifactStore, MinionV2Repository
from pal.minion.v2.contracts import AggregateSnapshot
from pal.minion.v2.integration import IntegrationOwnershipDefect, IntegrationService
from pal.minion.v2.orchestration import MinionV2OutboxProcessor
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.verification import (
    DefectKind,
    DefectPropagationService,
    UnknownPolicy,
    VerificationCaseKind,
    VerificationCaseRunner,
    VerificationCaseSpec,
    VerificationService,
    VerificationStatus,
    candidate_reuse_fingerprint,
    finding_fingerprint,
    no_progress_detected,
)


class MinionV2VerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal_minion_v2_verify_"))
        self.repository = MinionV2Repository(self.runtime_root)
        self.store = ContentAddressedArtifactStore(self.runtime_root, self.repository)
        self.verification = VerificationService(self.repository, self.store)

    def tearDown(self) -> None:
        shutil.rmtree(self.runtime_root, ignore_errors=True)

    def test_case_runner_persists_command_output(self) -> None:
        result = VerificationCaseRunner(self.store).run(
            VerificationCaseSpec(
                case_id="case_1",
                case_kind=VerificationCaseKind.CONTRACT_ADVERSARIAL,
                command=("sh", "-c", "printf pass-output"),
                contract_refs=("INV-1",),
            ),
            cwd=self.runtime_root,
        )
        self.assertEqual(result.status, VerificationStatus.PASS)
        self.assertEqual(self.store.read_bytes(result.stdout_ref), b"pass-output")
        self.assertTrue(self.repository.artifact_is_durable(str(result.stderr_ref["sha256"])))

    def test_repair_bill_has_stable_fingerprint_and_regression_obligation(self) -> None:
        node = self._reviewing_node("node_repair")
        candidate = self.store.put_json({"candidate_digest": "c1"}, artifact_type="CandidateSnapshotArtifact")
        output = self.store.put_bytes(b"failure", artifact_type="VerificationStdoutArtifact")
        case_result = VerificationCaseRunner(self.store).run(
            VerificationCaseSpec(
                case_id="case_fail",
                case_kind=VerificationCaseKind.CONTRACT_ADVERSARIAL,
                command=("sh", "-c", "exit 7"),
                contract_refs=("INV-1",),
            ),
            cwd=self.runtime_root,
        )
        report_ref, status = self.verification.publish_report(
            node=node,
            candidate_ref=candidate.to_dict(),
            case_results=[case_result],
            reviewer_summary="Invariant can be broken.",
        )
        self.assertEqual(status, VerificationStatus.FAIL)
        repair_ref, fingerprint = self.verification.publish_repair_bill(
            node=node,
            candidate_digest="c1",
            verification_ref=report_ref,
            defect_kind=DefectKind.MODULE,
            severity="high",
            contract_refs=["INV-1"],
            minimal_reproducer_ref=output.to_dict(),
            test_artifact_ref=output.to_dict(),
            expected={"returncode": 0},
            actual={"returncode": 7},
            suggested_repair_boundary=["src/module/**"],
            finding_section="invariant",
            finding_summary="Invariant can be broken",
            failure_reason="case_fail exits 7",
            affected_refs=["src/module/core.py:42"],
            finding_id="F-1",
        )
        repair = self.store.read_json(repair_ref)
        self.assertEqual(repair["finding_fingerprint"], fingerprint)
        self.assertIn("regression_test_obligation", repair)
        self.assertEqual(repair["finding_section"], "invariant")
        self.assertEqual(repair["finding_id"], "F-1")
        self.assertEqual(repair["affected_refs"], ["src/module/core.py:42"])
        self.assertEqual(
            fingerprint,
            finding_fingerprint(
                defect_kind=DefectKind.MODULE,
                contract_refs=["INV-1"],
                reproducer_hash=output.sha256,
                expected={"returncode": 0},
                actual={"returncode": 7},
            ),
        )

    def test_unknown_hard_semantics_requires_human_waiver(self) -> None:
        assumption = self.store.put_json({"owner": "platform", "verification_plan": "device CI"}, artifact_type="AssumptionLedgerArtifact")
        waiver = self.store.put_json({"actor": "nathan"}, artifact_type="HumanWaiverArtifact")
        self.assertFalse(
            UnknownPolicy(
                architecture_allows_platform_unknown=True,
                assumption_ref=assumption.to_dict(),
                hard_or_core_semantics=True,
            ).allows()
        )
        self.assertTrue(
            UnknownPolicy(
                architecture_allows_platform_unknown=True,
                assumption_ref=assumption.to_dict(),
                hard_or_core_semantics=True,
                human_waiver_ref=waiver.to_dict(),
            ).allows()
        )

    def test_three_identical_failures_without_tree_change_require_triage(self) -> None:
        history = [
            {"finding_fingerprint": "same", "candidate_tree_hash": "same-tree"},
            {"finding_fingerprint": "same", "candidate_tree_hash": "same-tree"},
            {"finding_fingerprint": "same", "candidate_tree_hash": "same-tree"},
        ]
        self.assertTrue(no_progress_detected(history))
        self.assertFalse(no_progress_detected([{**item, "candidate_tree_hash": str(index)} for index, item in enumerate(history)]))

    def test_candidate_reuse_fingerprint_requires_all_inputs(self) -> None:
        values = {
            "unit_contract_hash": "1",
            "relevant_requirements_hash": "2",
            "relevant_evidence_hash": "3",
            "global_constraint_hash": "4",
            "owned_area_hash": "5",
            "dependency_set_hash": "6",
            "dependency_interface_hash": "7",
            "dependency_output_hash": "8",
            "integration_contract_subset_hash": "9",
            "environment_policy_hash": "10",
        }
        first = candidate_reuse_fingerprint(**values)
        second = candidate_reuse_fingerprint(**values)
        self.assertEqual(first, second)
        with self.assertRaises(ValueError):
            candidate_reuse_fingerprint(**{**values, "relevant_evidence_hash": ""})

    def test_verdicts_drive_pass_fail_unknown_and_not_applicable_states(self) -> None:
        verification_ref = self.store.put_json({"status": "test"}, artifact_type="VerificationArtifact")
        repair_ref = self.store.put_json({"finding": "test"}, artifact_type="RepairBillArtifact")
        assumption_ref = self.store.put_json({"owner": "platform"}, artifact_type="AssumptionLedgerArtifact")
        cases = (
            ("pass", VerificationStatus.PASS, None, None, "ACCEPTED"),
            ("not_applicable", VerificationStatus.NOT_APPLICABLE, None, None, "ACCEPTED"),
            (
                "unknown_allowed",
                VerificationStatus.UNKNOWN,
                UnknownPolicy(True, assumption_ref.to_dict(), False),
                None,
                "ACCEPTED",
            ),
            ("fail", VerificationStatus.FAIL, None, repair_ref, "REPAIR_QUEUED"),
            ("unknown_blocked", VerificationStatus.UNKNOWN, None, repair_ref, "REPAIR_QUEUED"),
        )
        for name, status, policy, repair, expected_state in cases:
            with self.subTest(status=name):
                node = self._reviewing_node(f"node_verdict_{name}")
                result = self.verification.submit_verdict(
                    node=node,
                    verification_ref=verification_ref,
                    status=status,
                    actor="reviewer",
                    unknown_policy=policy,
                    repair_bill_ref=repair,
                    finding_fingerprint_value="fingerprint" if repair else "",
                    candidate_tree_hash="tree" if repair else "",
                )
                self.assertEqual(result.snapshot.state, expected_state)

    def test_accepted_unit_publishes_reviewable_memory_candidate(self) -> None:
        node = self._reviewing_node("node_memory")
        verification_ref = self.store.put_json({"status": "PASS"}, artifact_type="VerificationArtifact")
        accepted = self.verification.submit_verdict(
            node=node,
            verification_ref=verification_ref,
            status=VerificationStatus.PASS,
            actor="verifier",
        ).snapshot
        service = MinionV2WorkflowService(self.runtime_root)
        processor = MinionV2OutboxProcessor(service)

        result = processor._publish_accepted_memory_candidate(
            {
                "effect_key": "accepted-memory",
                "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                "aggregate_id": accepted.aggregate_id,
            }
        )

        updated = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, accepted.aggregate_id)
        memory_ref = updated.payload["memory_candidate_ref"]
        memory = self.store.read_json(memory_ref)
        self.assertEqual(memory["review_status"], "pending_human_review")
        self.assertEqual(memory["verification_artifact_ref"]["sha256"], verification_ref.sha256)
        self.assertEqual(result["result_artifact_ref"]["sha256"], memory_ref["sha256"])

    def test_dependency_defect_reopens_upstream_and_stales_accepted_downstream(self) -> None:
        verification_ref = self.store.put_json({"status": "pass"}, artifact_type="VerificationArtifact")
        repair_ref = self.store.put_json({"finding": "dependency"}, artifact_type="RepairBillArtifact")
        upstream = self._reviewing_node("node_upstream")
        upstream = self.verification.submit_verdict(
            node=upstream,
            verification_ref=verification_ref,
            status=VerificationStatus.PASS,
            actor="reviewer",
        ).snapshot
        downstream = self._reviewing_node("node_downstream", dependency_node_ids=(upstream.aggregate_id,))
        downstream = self.verification.submit_verdict(
            node=downstream,
            verification_ref=verification_ref,
            status=VerificationStatus.PASS,
            actor="reviewer",
        ).snapshot

        affected = DefectPropagationService(self.repository).propagate_dependency_defect(
            workflow_id=upstream.workflow_id,
            epoch_id="epoch",
            dependency_node_id=upstream.aggregate_id,
            repair_bill_ref=repair_ref,
        )

        self.assertEqual(affected, (downstream.aggregate_id,))
        self.assertEqual(
            self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, upstream.aggregate_id).state,
            "REPAIR_QUEUED",
        )
        self.assertEqual(
            self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, downstream.aggregate_id).state,
            "STALE",
        )

    def _reviewing_node(
        self,
        node_id: str,
        *,
        dependency_node_ids: tuple[str, ...] = (),
    ) -> AggregateSnapshot:
        artifact = self.store.put_json({"contract": node_id}, artifact_type="UnitContractArtifact")
        actions = [
            (
                "CREATE_NODE_RUN",
                {
                    "unit_contract_ref": artifact.to_dict(),
                    "epoch_id": "epoch",
                    "dependency_node_ids": list(dependency_node_ids),
                },
            ),
            (
                "DEPENDENCIES_ACCEPTED",
                {"accepted_dependency_node_ids": list(dependency_node_ids), "epoch_frozen": False},
            ),
            ("START_PRODUCING", {"fencing_token": 1}),
            ("SUBMIT_CANDIDATE", {"fencing_token": 1}),
            (
                "QUIESCE_COMPLETED",
                {"fencing_token": 1, "process_group_reaped": True, "exclusive_workspace_lock": True, "workspace_fingerprint": "tree"},
            ),
            ("CANDIDATE_SNAPSHOTTED", {"candidate_ref": artifact.to_dict(), "candidate_digest": "c1", "workspace_fingerprint": "tree"}),
            ("START_REVIEW", {"fencing_token": 2}),
        ]
        for action_type, payload in actions:
            snapshot = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
            self.repository.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id="wf_verify",
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node_id,
                    actor="test",
                    expected_version=snapshot.version if snapshot else 0,
                    idempotency_key=f"{node_id}:{action_type}",
                    payload=payload,
                )
            )
        return self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)


class MinionV2IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal_minion_v2_integration_"))
        self.repository = MinionV2Repository(self.runtime_root)
        self.store = ContentAddressedArtifactStore(self.runtime_root, self.repository)
        self.repo = self.runtime_root / "repo"
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("config", "user.name", "Test")
        self._git("config", "user.email", "test@example.com")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-qm", "base")
        self.base = self._git("rev-parse", "HEAD").strip()

    def tearDown(self) -> None:
        shutil.rmtree(self.runtime_root, ignore_errors=True)

    def test_integration_cherry_picks_candidates_in_declared_order(self) -> None:
        sha_a = self._candidate("candidate-a", "a.txt", "a\n")
        sha_b = self._candidate("candidate-b", "b.txt", "b\n")
        self._git("checkout", "-q", "--detach", self.base)
        ref, integration_sha = IntegrationService(self.store).integrate_candidates(
            integration_worktree=self.repo,
            ordered_candidates=[
                {"node_run_id": "a", "candidate_digest": sha_a},
                {"node_run_id": "b", "candidate_digest": sha_b},
            ],
            architecture_manifest_sha="manifest",
        )
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), integration_sha)
        self.assertEqual([item["node_run_id"] for item in self.store.read_json(ref)["merged_candidates"]], ["a", "b"])
        self.assertTrue((self.repo / "a.txt").is_file())
        self.assertTrue((self.repo / "b.txt").is_file())

    def test_integration_conflict_is_an_ownership_defect(self) -> None:
        sha_a = self._candidate("conflict-a", "base.txt", "from a\n")
        sha_b = self._candidate("conflict-b", "base.txt", "from b\n")
        self._git("checkout", "-q", "--detach", self.base)

        with self.assertRaises(IntegrationOwnershipDefect):
            IntegrationService(self.store).integrate_candidates(
                integration_worktree=self.repo,
                ordered_candidates=[
                    {"node_run_id": "a", "candidate_digest": sha_a},
                    {"node_run_id": "b", "candidate_digest": sha_b},
                ],
                architecture_manifest_sha="manifest",
            )

        self.assertEqual(self._git("rev-parse", "HEAD").strip(), sha_a)

    def _candidate(self, branch: str, filename: str, content: str) -> str:
        self._git("checkout", "-q", "-B", branch, self.base)
        (self.repo / filename).write_text(content, encoding="utf-8")
        self._git("add", filename)
        self._git("commit", "-qm", branch)
        return self._git("rev-parse", "HEAD").strip()

    def _git(self, *args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=self.repo, text=True)


if __name__ == "__main__":
    unittest.main()
