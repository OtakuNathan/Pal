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
from unittest.mock import patch

from pal.bunshin.contracts import AggregateSnapshot, AggregateType
from pal.bunshin.input_binding import BoundInputError
from pal.bunshin.semantic_orchestration.orchestrator import SemanticOrchestrator
from pal.bunshin.sandbox import with_bunshin_sandbox_metadata
from pal.bunshin.semantic_orchestration.role_environment import (
    _reconcile_declared_input_reference_binds, _refresh_ephemeral_role_reference_binds,
)
from pal.bunshin.service import BunshinWorkflowService
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
        self.service = BunshinWorkflowService(self.runtime_root)
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
        workflow = self.service.repository.snapshots.read_snapshot(
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
        entries = self.orchestrator.components.attempt_inputs.bind_role_attempt_inputs(
            workflow=workflow,
            request={},
            workspace={"repo_path": str(workspace)},
            snapshot=node,
            workspace_source_root=str(WORKSPACE_REPO),
        )
        self.assertEqual(entries, [])
        self.assertFalse((workspace / "inputs").exists())
        with self.assertRaisesRegex(BoundInputError, "without a frozen input-binding manifest"):
            self.orchestrator.components.attempt_inputs.bind_role_attempt_inputs(
                workflow=workflow, request={"references": [{"name": "facts", "path": "facts.txt"}]},
                workspace={"repo_path": str(workspace)}, snapshot=node,
                workspace_source_root=str(WORKSPACE_REPO),
            )


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


class LegacyInputReferenceRecoveryTests(unittest.TestCase):
    def test_old_prompt_alias_is_removed_or_rebound_to_the_authorized_absolute_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            frozen = repo / "facts.txt"
            frozen.write_text("frozen facts", encoding="utf-8")
            stale = root / "manager-facts.txt"
            stale.write_text("unrelated facts", encoding="utf-8")
            external = root / "external-facts.txt"
            external.write_text("approved reference", encoding="utf-8")
            extra = root / "extra.txt"
            extra.write_text("other reference", encoding="utf-8")
            bound = {"name": "facts", "path": str(frozen), "include": ["facts.txt"], "bound_input": True}
            other = {"name": "user_extra", "path": str(extra)}
            legacy_alias = {"name": "user_facts", "path": str(stale)}
            legacy = BunshinInvocationPack(invocation_id="old", workspace={"repo_path": str(repo), "reference_paths": [
                legacy_alias, bound, other,
            ]}, metadata={"requirements_brief": {"acceptance_criteria": ["Original requirement"],
                                                   "references": [legacy_alias, other]}})
            with patch("pal.bunshin.sandbox.sandbox_supported_backend", return_value="bwrap"):
                projected = with_bunshin_sandbox_metadata(root, legacy, run_id="old")
                for keep_alias in (False, True):
                    with self.subTest(keep_alias=keep_alias):
                        request = {"references": [{"name": "facts", "path": "facts.txt"}]}
                        current_refs = [bound, other]
                        if keep_alias:
                            request["references"].append({"name": "facts", "path": str(external)})
                            current_refs.append({"name": "user_facts", "path": str(external)})
                        current = BunshinInvocationPack(invocation_id="current", workspace={
                            "repo_path": str(repo), "reference_paths": current_refs,
                        }, metadata={"requirements_brief": {"references": [item for item in current_refs
                                                                              if not item.get("bound_input")]}})
                        refreshed = _reconcile_declared_input_reference_binds(projected, current, request=request)
                        retry = with_bunshin_sandbox_metadata(root, refreshed, run_id="retry")
                        references = {item["name"]: item for item in retry.workspace["reference_paths"]}
                        binds = {item["name"]: item for item in retry.metadata["sandbox"]["reference_binds"]}
                        brief_refs = {item["name"]: item for item in retry.metadata["requirements_brief"]["references"]}
                        # Frozen in-workspace inputs are files, not external
                        # mount roots. The retry must retain their exact names.
                        self.assertEqual(references["facts"]["path"], str(frozen))
                        self.assertNotIn("facts", binds)
                        from pal.bunshin.prompt_adapter import render_bunshin_task_prompt
                        self.assertIn(f"path={frozen}", render_bunshin_task_prompt(retry))
                        self.assertEqual(references["user_extra"]["path"], "/pal/references/user_extra/extra.txt")
                        if keep_alias:
                            self.assertEqual(binds["user_facts"]["include"], ["external-facts.txt"])
                            self.assertEqual(references["user_facts"]["path"], "/pal/references/user_facts/external-facts.txt")
                            self.assertEqual(brief_refs["user_facts"]["path"], str(external))
                        else:
                            self.assertNotIn("user_facts", references)
                            self.assertNotIn("user_facts", binds)
                            self.assertNotIn("user_facts", brief_refs)
                        correction = retry.metadata["requirements_brief"]["acceptance_criteria"][-1]
                        self.assertIn("recheck any work", correction)
                        self.assertEqual(_reconcile_declared_input_reference_binds(retry, current, request=request), retry)


if __name__ == "__main__":
    unittest.main()
