from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pal.minion.v2.artifacts import ContentAddressedArtifactStore
from pal.minion.v2.contracts import ActionEnvelope, AggregateSnapshot, AggregateType
from pal.minion.v2.orchestration import (
    MinionV2OutboxProcessor,
    _active_control_children,
    reconcile_control_requests,
)
from pal.minion.v2.recovery import MinionV2Recovery
from pal.minion.v2.replan import architecture_finding_semantic_view
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.task_ledger import TaskLedgerService


class MinionV2ReplanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal_minion_v2_replan_"))
        self.service = MinionV2WorkflowService(self.runtime_root)
        self.repository = self.service.repository
        self.artifacts = ContentAddressedArtifactStore(self.runtime_root, self.repository)
        self.processor = MinionV2OutboxProcessor(self.service)
        self.workflow_id = "wf_replan"
        self.epoch_id = "epoch_replan"
        self.requirements_ref = TaskLedgerService(
            self.runtime_root,
            self.artifacts,
        ).publish(
            title="ABI compatibility",
            task_spec={"public_api": "The public API remains ABI compatible."},
            actor="test",
            source_channel="test",
        )
        self.manifest_ref = self.artifacts.put_json(
            {"requirements_ref": self.requirements_ref.to_dict()},
            artifact_type="TestManifestArtifact",
        )
        self.topology_ref = self.artifacts.put_json({}, artifact_type="SkeletonTopologyArtifact")
        self.contract_ref = self.artifacts.put_json({}, artifact_type="UnitContractArtifact")
        request_ref = self.artifacts.put_json(
            {"requirements_ref": self.requirements_ref.to_dict()},
            artifact_type="WorkflowRequestArtifact",
        )
        self._dispatch(
            AggregateType.WORKFLOW,
            self.workflow_id,
            "CREATE_WORKFLOW",
            {"request_ref": request_ref.to_dict()},
        )
        self._dispatch(AggregateType.WORKFLOW, self.workflow_id, "START_WORKFLOW")
        self._dispatch(
            AggregateType.EXECUTION_EPOCH,
            self.epoch_id,
            "CREATE_EXECUTION_EPOCH",
            {
                "architecture_manifest_ref": self.manifest_ref.to_dict(),
                "topology_ref": self.topology_ref.to_dict(),
            },
        )
        self._dispatch(AggregateType.EXECUTION_EPOCH, self.epoch_id, "START_EXECUTION")
        self._dispatch(
            AggregateType.EXECUTION_EPOCH,
            self.epoch_id,
            "NODES_COMPILED",
            {"node_ids": ["node_a", "node_b", "node_c"]},
        )
        self._dispatch(
            AggregateType.WORKFLOW,
            self.workflow_id,
            "LINK_EXECUTION_EPOCH",
            {"execution_epoch_id": self.epoch_id},
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.runtime_root, ignore_errors=True)

    def test_concurrent_contract_defects_drain_reviews_into_one_revision(self) -> None:
        self._create_reviewing_node("node_a", "module_a", worker="reviewer-a")
        self._create_reviewing_node("node_b", "module_b", worker="reviewer-b")
        self._create_queued_node("node_c", "module_c")
        finding_a = self._finding("module_a", "fingerprint-a", "First ABI contradiction")
        finding_b = self._finding("module_b", "fingerprint-b", "Second ABI contradiction")

        self._dispatch(
            AggregateType.DAG_NODE_RUN,
            "node_a",
            "CONTRACT_DEFECT",
            {"repair_bill_ref": finding_a.to_dict()},
        )
        self.processor._request_epoch_replan(self._node_effect("node_a", "finding-a"))
        self.processor._freeze_epoch_for_replan(self._epoch_effect("freeze"))

        epoch = self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, self.epoch_id)
        self.assertEqual(epoch.state, "REPLAN_COLLECTING")
        self.assertEqual(
            self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, "node_b").state,
            "REVIEW_SNAPSHOTTING",
        )
        self.assertEqual(
            self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, "node_c").state,
            "STALE",
        )
        projection = self.repository.read_workflow_projection(self.workflow_id)
        self.assertEqual(projection["current_phase"], "replan_collecting")
        self.assertEqual(projection["active_worker_id"], "reviewer-b")
        self.assertFalse(self._architecture_revisions())

        self._dispatch(
            AggregateType.DAG_NODE_RUN,
            "node_b",
            "CONTRACT_DEFECT",
            {"repair_bill_ref": finding_b.to_dict()},
        )
        self.processor._request_epoch_replan(self._node_effect("node_b", "finding-b"))
        self.processor._reconcile_replan_collections(self.workflow_id)

        epoch = self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, self.epoch_id)
        self.assertEqual(epoch.state, "REPLAN_REQUIRED")
        batch = self.artifacts.read_json(epoch.payload["replan_finding_batch_ref"])
        self.assertEqual(
            [item["finding_fingerprint"] for item in batch["finding_groups"]],
            ["fingerprint-a", "fingerprint-b"],
        )
        semantic_view = architecture_finding_semantic_view(batch)
        serialized_view = json.dumps(semantic_view, sort_keys=True)
        self.assertNotIn("repair_bill_refs", serialized_view)
        self.assertNotIn(self.epoch_id, serialized_view)

        self.processor._create_replan_revision(self._epoch_effect("create-revision"))
        revisions = self._architecture_revisions()
        self.assertEqual(len(revisions), 1)
        self.assertEqual(
            revisions[0].payload["replan_finding_batch_ref"],
            epoch.payload["replan_finding_batch_ref"],
        )

        self.processor._request_epoch_replan(self._node_effect("node_a", "finding-a-replay"))
        self.processor._request_epoch_replan(self._node_effect("node_b", "finding-b-replay"))
        self.assertEqual(len(self._architecture_revisions()), 1)
        projection = self.repository.read_workflow_projection(self.workflow_id)
        self.assertEqual(projection["active_aggregate_type"], "architecture_revision")
        self.assertEqual(projection["active_aggregate_id"], revisions[0].aggregate_id)

        self._dispatch(
            AggregateType.EXECUTION_EPOCH,
            self.epoch_id,
            "SUCCESSOR_EPOCH_STARTED",
            {"successor_execution_epoch_id": "epoch_successor"},
        )
        self.assertEqual(
            self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, self.epoch_id).state,
            "SUPERSEDED",
        )

    def test_pause_reconciler_closes_replan_required_epoch_and_workflow(self) -> None:
        self._create_reviewing_node("node_a", "module_a", worker="reviewer-a")
        finding = self._finding("module_a", "fingerprint-a", "ABI contradiction")
        self._dispatch(
            AggregateType.DAG_NODE_RUN,
            "node_a",
            "CONTRACT_DEFECT",
            {"repair_bill_ref": finding.to_dict()},
        )
        self.processor._request_epoch_replan(self._node_effect("node_a", "finding-a"))
        self.processor._freeze_epoch_for_replan(self._epoch_effect("freeze"))
        self.processor._reconcile_replan_collections(self.workflow_id)
        self.assertEqual(
            self.repository.read_snapshot(
                AggregateType.EXECUTION_EPOCH,
                self.epoch_id,
            ).state,
            "REPLAN_REQUIRED",
        )
        self._dispatch(AggregateType.WORKFLOW, self.workflow_id, "REQUEST_PAUSE")

        reconcile_control_requests(self.repository, self.workflow_id)

        self.assertEqual(
            self.repository.read_snapshot(
                AggregateType.EXECUTION_EPOCH,
                self.epoch_id,
            ).state,
            "PAUSED",
        )
        self.assertEqual(
            self.repository.read_snapshot(
                AggregateType.WORKFLOW,
                self.workflow_id,
            ).state,
            "PAUSED",
        )

    def test_workflow_control_scope_uses_direct_children_only(self) -> None:
        def snapshot(
            aggregate_type: AggregateType,
            aggregate_id: str,
            state: str,
            payload: dict | None = None,
        ) -> AggregateSnapshot:
            return AggregateSnapshot(
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                workflow_id="wf_control_scope",
                state=state,
                version=1,
                payload=payload or {},
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            )

        workflow = snapshot(
            AggregateType.WORKFLOW,
            "wf_control_scope",
            "PAUSE_REQUESTED",
            {
                "architecture_revision_id": "arch_active",
                "execution_epoch_id": "epoch_active",
            },
        )
        snapshots = [
            workflow,
            snapshot(
                AggregateType.ARCHITECTURE_REVISION,
                "arch_historical",
                "SUPERSEDED",
            ),
            snapshot(
                AggregateType.ARCHITECTURE_REVISION,
                "arch_active",
                "PAUSED",
            ),
            snapshot(
                AggregateType.EXECUTION_EPOCH,
                "epoch_active",
                "PAUSED",
            ),
            snapshot(
                AggregateType.DAG_NODE_RUN,
                "node_active",
                "STALE",
                {"epoch_id": "epoch_active"},
            ),
        ]

        children = _active_control_children(snapshots, workflow)

        self.assertEqual(
            {item.aggregate_id for item in children},
            {"arch_active", "epoch_active"},
        )

    def test_legacy_mixed_repair_bill_does_not_promote_unknown_case(self) -> None:
        repair_bill = {
            "defect_kind": "contract_defect",
            "actual": {
                "cases": [
                    {"name": "abi_failure", "status": "FAIL"},
                    {"name": "device_unavailable", "status": "UNKNOWN"},
                ]
            },
            "findings": [
                {
                    "case_name": "abi_failure",
                    "defect_kind": "contract_defect",
                    "summary": "ABI declaration is invalid",
                    "locations": [{"path": "include/api.h"}],
                },
                {
                    "case_name": "device_unavailable",
                    "defect_kind": "contract_defect",
                    "summary": "Device is unavailable",
                    "locations": [{"path": "tests/device_probe.cpp"}],
                },
            ],
        }

        view = architecture_finding_semantic_view(repair_bill)

        self.assertEqual(view["finding_count"], 1)
        self.assertEqual(view["findings"][0]["summary"], "ABI declaration is invalid")
        self.assertRegex(view["findings"][0]["finding_key"], r"^finding_[0-9a-f]{16}$")
        self.assertEqual(view["findings"][0]["priority"], "p1")
        self.assertEqual(view["findings"][0]["disposition"], "blocking")

    def test_architecture_finding_view_preserves_reviewer_semantic_identity(self) -> None:
        view = architecture_finding_semantic_view(
            {
                "findings": [
                    {
                        "finding_key": "missing_static_contract",
                        "finding_kind": "contract_defect",
                        "priority": "p2",
                        "disposition": "blocking",
                        "summary": "The public overload remains well-formed for an invalid type.",
                    }
                ]
            }
        )

        self.assertEqual(
            view["findings"][0],
            {
                "finding_key": "missing_static_contract",
                "finding_kind": "contract_defect",
                "priority": "p2",
                "disposition": "blocking",
                "summary": "The public overload remains well-formed for an invalid type.",
                "severity": "error",
                "module_name": "",
                "affected_modules": [],
                "requirements": [],
                "locations": [],
                "suggested_repair_boundary": [],
                "revision_targets": [],
                "expected": None,
                "actual": None,
            },
        )

    def test_same_fingerprint_preserves_both_repair_bills(self) -> None:
        self._create_reviewing_node("node_a", "module_a")
        self._create_reviewing_node("node_b", "module_b")
        first = self._finding("module_a", "same-fingerprint", "First reproducer")
        second = self._finding("module_b", "same-fingerprint", "Second reproducer")
        for node_id, finding in (("node_a", first), ("node_b", second)):
            self._dispatch(
                AggregateType.DAG_NODE_RUN,
                node_id,
                "CONTRACT_DEFECT",
                {"repair_bill_ref": finding.to_dict()},
            )
        self.processor._request_epoch_replan(self._node_effect("node_a", "first"))
        self.processor._request_epoch_replan(self._node_effect("node_b", "second"))
        self.processor._freeze_epoch_for_replan(self._epoch_effect("freeze-same"))

        epoch = self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, self.epoch_id)
        batch = self.artifacts.read_json(epoch.payload["replan_finding_batch_ref"])
        self.assertEqual(len(batch["finding_groups"]), 1)
        self.assertEqual(len(batch["finding_groups"][0]["repair_bill_refs"]), 2)
        self.assertEqual(len(batch["finding_groups"][0]["findings"]), 2)

    def test_startup_recovery_reopens_duplicate_revisions_as_one_collection(self) -> None:
        self._create_reviewing_node("node_a", "module_a")
        first = self._finding("module_a", "first", "First")
        second = self._finding("module_a", "second", "Second")
        self._dispatch(
            AggregateType.DAG_NODE_RUN,
            "node_a",
            "CONTRACT_DEFECT",
            {"repair_bill_ref": first.to_dict()},
        )
        self.processor._request_epoch_replan(self._node_effect("node_a", "first"))
        self.processor._freeze_epoch_for_replan(self._epoch_effect("freeze-recovery"))
        epoch = self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, self.epoch_id)
        self.assertEqual(epoch.state, "REPLAN_REQUIRED")

        for index, finding in enumerate((first, second), start=1):
            self._dispatch(
                AggregateType.ARCHITECTURE_REVISION,
                f"arch_duplicate_{index}",
                "CREATE_ARCHITECTURE_REVISION",
                {
                    "requirements_ref": self.requirements_ref.to_dict(),
                    "base_architecture_manifest_ref": self.manifest_ref.to_dict(),
                    "replan_finding_ref": finding.to_dict(),
                    "source_execution_epoch_id": self.epoch_id,
                },
            )

        recovered = MinionV2Recovery(self.service)._recover_duplicate_replans()

        self.assertEqual(recovered, [self.epoch_id])
        epoch = self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, self.epoch_id)
        self.assertEqual(epoch.state, "REPLAN_COLLECTING")
        self.assertEqual(len(epoch.payload["pending_replan_findings"]), 2)
        self.assertEqual(
            {item.state for item in self._architecture_revisions()},
            {"CANCEL_REQUESTED"},
        )

    def test_multiple_requirement_findings_share_the_unchanged_task_ledger(self) -> None:
        self._create_reviewing_node("node_a", "module_a")
        self._create_reviewing_node("node_b", "module_b")
        for node_id, module, fingerprint in (
            ("node_a", "module_a", "a"),
            ("node_b", "module_b", "b"),
        ):
            finding = self._finding(module, fingerprint, fingerprint)
            self._dispatch(
                AggregateType.DAG_NODE_RUN,
                node_id,
                "CONTRACT_DEFECT",
                {"repair_bill_ref": finding.to_dict()},
            )
            self.processor._request_epoch_replan(
                self._node_effect(node_id, f"requirement-{fingerprint}")
            )

        self.processor._freeze_epoch_for_replan(self._epoch_effect("freeze-requirements"))
        epoch = self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, self.epoch_id)
        self.assertEqual(epoch.state, "REPLAN_REQUIRED")
        self.assertEqual(epoch.payload["requirements_ref"], self.requirements_ref.to_dict())
        batch = self.artifacts.read_json(epoch.payload["replan_finding_batch_ref"])
        self.assertEqual(len(batch["finding_groups"]), 2)

    def test_unseen_finding_after_frozen_batch_enters_triage(self) -> None:
        self._create_reviewing_node("node_a", "module_a")
        first = self._finding("module_a", "first", "First")
        self._dispatch(
            AggregateType.DAG_NODE_RUN,
            "node_a",
            "CONTRACT_DEFECT",
            {"repair_bill_ref": first.to_dict()},
        )
        self.processor._request_epoch_replan(self._node_effect("node_a", "first"))
        self.processor._freeze_epoch_for_replan(self._epoch_effect("freeze-first"))
        self.assertEqual(
            self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, self.epoch_id).state,
            "REPLAN_REQUIRED",
        )

        self._create_reviewing_node("node_late", "module_late")
        late = self._finding("module_late", "late", "Late")
        self._dispatch(
            AggregateType.DAG_NODE_RUN,
            "node_late",
            "CONTRACT_DEFECT",
            {"repair_bill_ref": late.to_dict()},
        )
        self.processor._request_epoch_replan(self._node_effect("node_late", "late"))

        epoch = self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, self.epoch_id)
        self.assertEqual(epoch.state, "TRIAGE_REQUIRED")
        self.assertEqual(epoch.payload["blocker"]["kind"], "late_replan_finding")

    def _create_queued_node(self, node_id: str, module_name: str) -> None:
        self._dispatch(
            AggregateType.DAG_NODE_RUN,
            node_id,
            "CREATE_NODE_RUN",
            {
                "unit_contract_ref": self.contract_ref.to_dict(),
                "epoch_id": self.epoch_id,
                "unit_id": module_name,
                "module_name": module_name,
                "architecture_manifest_ref": self.manifest_ref.to_dict(),
                "dependency_node_ids": [],
            },
        )
        self._dispatch(
            AggregateType.DAG_NODE_RUN,
            node_id,
            "DEPENDENCIES_ACCEPTED",
            {"accepted_dependency_node_ids": [], "epoch_frozen": False},
        )

    def _create_reviewing_node(
        self,
        node_id: str,
        module_name: str,
        *,
        worker: str = "reviewer",
    ) -> None:
        self._create_queued_node(node_id, module_name)
        for action_type, payload in (
            ("START_PRODUCING", {"fencing_token": 1, "active_worker_id": f"coder-{node_id}"}),
            ("SUBMIT_CANDIDATE", {"fencing_token": 1}),
            (
                "QUIESCE_COMPLETED",
                {
                    "fencing_token": 1,
                    "process_group_reaped": True,
                    "exclusive_workspace_lock": True,
                    "workspace_fingerprint": f"tree-{node_id}",
                },
            ),
            (
                "CANDIDATE_SNAPSHOTTED",
                {
                    "candidate_ref": self.contract_ref.to_dict(),
                    "candidate_digest": f"candidate-{node_id}",
                    "workspace_fingerprint": f"tree-{node_id}",
                },
            ),
            ("START_REVIEW", {"fencing_token": 2, "active_worker_id": worker}),
            (
                "SUBMIT_SEMANTIC_VERIFICATION",
                {"pending_verification_ref": self.contract_ref.to_dict()},
            ),
            (
                "VERIFIER_QUIESCED",
                {
                    "fencing_token": 2,
                    "process_group_reaped": True,
                    "exclusive_workspace_lock": True,
                    "workspace_fingerprint": f"review-tree-{node_id}",
                },
            ),
        ):
            self._dispatch(AggregateType.DAG_NODE_RUN, node_id, action_type, payload)

    def _finding(self, module_name: str, fingerprint: str, summary: str):
        return self.artifacts.put_json(
            {
                "defect_kind": "contract_defect",
                "finding_fingerprint": fingerprint,
                "module_name": module_name,
                "finding_summary": summary,
                "severity": "major",
                "requirements": [
                    {
                        "section": "Public API",
                        "requirement": "The public API remains ABI compatible.",
                    }
                ],
                "locations": [{"path": f"include/{module_name}.h"}],
            },
            artifact_type="RepairBillArtifact",
        )

    def _dispatch(
        self,
        aggregate_type: AggregateType,
        aggregate_id: str,
        action_type: str,
        payload: dict | None = None,
    ):
        snapshot = self.repository.read_snapshot(aggregate_type, aggregate_id)
        return self.repository.dispatch(
            ActionEnvelope(
                action_type=action_type,
                workflow_id=self.workflow_id,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                actor="test",
                expected_version=snapshot.version if snapshot else 0,
                idempotency_key=f"{aggregate_id}:{action_type}:{snapshot.version if snapshot else 0}",
                payload=dict(payload or {}),
            )
        ).snapshot

    def _node_effect(self, node_id: str, key: str) -> dict[str, str]:
        return {
            "effect_key": key,
            "aggregate_type": AggregateType.DAG_NODE_RUN.value,
            "aggregate_id": node_id,
        }

    def _epoch_effect(self, key: str) -> dict[str, str]:
        return {
            "effect_key": key,
            "aggregate_type": AggregateType.EXECUTION_EPOCH.value,
            "aggregate_id": self.epoch_id,
        }

    def _architecture_revisions(self):
        return [
            item
            for item in self.repository.list_workflow_snapshots(self.workflow_id)
            if item.aggregate_type == AggregateType.ARCHITECTURE_REVISION
        ]


if __name__ == "__main__":
    unittest.main()
