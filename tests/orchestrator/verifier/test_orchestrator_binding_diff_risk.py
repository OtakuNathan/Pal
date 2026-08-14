"""Diff-risk probe for the orchestrator input-binding Candidate.

Two targeted checks for defects introduced by the current Candidate's
orchestrator change:

1. A workflow without declared inputs keeps the pre-Candidate behavior: the
   binding stage is a no-op and ``workspace["reference_paths"]`` is exactly
   the references list (the Candidate changed the assignment from
   ``references`` to ``[*references, *bound_input_entries]``).
2. ``_refresh_ephemeral_role_reference_binds`` never rewrites an attempt's
   sandbox bind from a ``bound_input`` reference entry, even when it shares a
   name with an ephemeral role input, so a stale durable pack cannot point a
   bind at a bound input's workspace path.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from pal.bunshin.v2.contracts import AggregateSnapshot, AggregateType
from pal.bunshin.v2.semantic_orchestration.orchestrator import (
    SemanticOrchestrator,
    _refresh_ephemeral_role_reference_binds,
)
from pal.bunshin.v2.service import BunshinV2WorkflowService
from pal.shared.messages import BunshinInvocationPack

WORKSPACE_REPO = Path(__file__).resolve().parents[3]


def _snapshot(
    aggregate_type: AggregateType,
    aggregate_id: str,
    workflow_id: str,
    payload: dict,
) -> AggregateSnapshot:
    return AggregateSnapshot(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        workflow_id=workflow_id,
        state="RUNNING",
        version=1,
        payload=payload,
        created_at="2026-08-14T00:00:00Z",
        updated_at="2026-08-14T00:00:00Z",
    )


class NoDeclaredInputsKeepsLegacyBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal_diff_risk_binding_"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.runtime_root = self.root / "runtime"
        self.runtime_root.mkdir()
        self.service = BunshinV2WorkflowService(self.runtime_root)
        self.service.create_task(
            {
                "task_id": "task-plain",
                "title": "Plain task",
                "objective": "No declared repo-relative inputs",
                "profile": "lifestyle.nutritionist",
                "workspace": {"repo_path": str(WORKSPACE_REPO)},
            }
        )
        self.orchestrator = SemanticOrchestrator(self.service)

    def test_workflow_without_binding_record_binds_nothing(self) -> None:
        result = self.service.start_workflow(
            {
                "workflow_id": "wf-plain",
                "task_id": "task-plain",
                "operation": "new_requirement",
                "goal": "No declared inputs",
                "task_spec": {"objective": "No declared inputs."},
                "delivery_binding": {
                    "channel_id": "socket_test",
                    "channel_kind": "socket",
                    "reply_target": {
                        "session_id": "test-session",
                        "request_id": "test-request",
                    },
                    "control_scope_key": "socket:socket_test:test-session",
                },
            }
        )
        self.assertEqual(result["status"], "created")
        workflow = self.service.repository.read_snapshot(
            AggregateType.WORKFLOW, "wf-plain"
        )
        self.assertIsNotNone(workflow)
        self.assertNotIn("input_binding_ref", workflow.payload)

        node = _snapshot(
            AggregateType.DAG_NODE_RUN,
            "node-plain",
            "wf-plain",
            {"execution_adapter": "artifact_bundle.v2"},
        )
        workspace = self.runtime_root / "plain-ws"
        workspace.mkdir()
        entries = self.orchestrator._bind_role_attempt_inputs(
            workflow=workflow,
            request={},
            workspace={"repo_path": str(workspace)},
            snapshot=node,
            workspace_source_root=str(WORKSPACE_REPO),
        )
        self.assertEqual(entries, [])
        self.assertFalse((workspace / "inputs").exists())


class EphemeralBindRefreshIgnoresBoundInputsTests(unittest.TestCase):
    def test_bound_entry_never_rewrites_an_ephemeral_bind(self) -> None:
        durable_pack = BunshinInvocationPack(
            invocation_id="inv-durable",
            workspace={
                "reference_paths": [
                    {"name": "workspace_preparation", "path": "/stale/attempt-1"}
                ]
            },
            metadata={
                "sandbox": {
                    "reference_binds": [
                        {
                            "name": "workspace_preparation",
                            "source_path": "/stale/attempt-1",
                        }
                    ]
                }
            },
        )
        current_pack = BunshinInvocationPack(
            invocation_id="inv-current",
            workspace={
                "reference_paths": [
                    {
                        "name": "workspace_preparation",
                        "path": "/fresh/attempt-2",
                        "bound_input": True,
                    }
                ]
            },
        )
        refreshed = _refresh_ephemeral_role_reference_binds(
            durable_pack, current_pack
        )
        binds = refreshed.metadata["sandbox"]["reference_binds"]
        # The bound_input entry is not an ephemeral role input; the stale
        # attempt's bind must be left untouched rather than pointed at the
        # bound input's workspace path.
        self.assertEqual(binds[0]["source_path"], "/stale/attempt-1")

        # Control: a plain (non-bound) current entry does refresh the bind.
        control_pack = BunshinInvocationPack(
            invocation_id="inv-current",
            workspace={
                "reference_paths": [
                    {
                        "name": "workspace_preparation",
                        "path": "/fresh/attempt-2",
                    }
                ]
            },
        )
        refreshed_control = _refresh_ephemeral_role_reference_binds(
            durable_pack, control_pack
        )
        binds_control = refreshed_control.metadata["sandbox"]["reference_binds"]
        self.assertEqual(binds_control[0]["source_path"], "/fresh/attempt-2")


if __name__ == "__main__":
    unittest.main()
