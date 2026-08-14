"""Focused developer tests for the orchestrator's role-workspace input binding.

These tests exercise the pre-dispatch binding stage that
``SemanticOrchestrator._run_profile_inner`` runs before sandbox environment
preparation: loading the workflow's immutable input-binding manifest from the
durable ``input_binding_ref`` record, deciding repository-including versus
artifact-style binding from the resolved workspace contract, materializing or
verifying accordingly, and projecting ``bound_input`` reference entries.

The ``pal.bunshin.v2.input_binding`` procedures are a parallel module whose
declaration bodies are deferred, so these tests substitute a test adapter at
the orchestrator module's import surface while the production path keeps
calling the declared public functions.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pal.bunshin.v2.contracts import (
    AggregateSnapshot,
    AggregateType,
)
from pal.bunshin.v2.input_binding import BoundInputError
from pal.bunshin.v2.semantic_orchestration import orchestrator as orchestrator_module
from pal.bunshin.v2.semantic_orchestration.orchestrator import (
    SemanticOrchestrator,
    _attach_bound_input_read_only_overlays,
    _role_workspace_input_binding_roots,
)
from pal.bunshin.v2.service import BunshinV2WorkflowService

WORKFLOW_ID = "wf-bind"


def _snapshot(
    aggregate_type: AggregateType,
    aggregate_id: str,
    payload: dict,
) -> AggregateSnapshot:
    return AggregateSnapshot(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        workflow_id=WORKFLOW_ID,
        state="RUNNING",
        version=1,
        payload=payload,
        created_at="2026-08-14T00:00:00Z",
        updated_at="2026-08-14T00:00:00Z",
    )


class RoleWorkspaceInputBindingRootsTests(unittest.TestCase):
    """The workspace contract decides repository-including versus artifact-style."""

    def test_artifact_adapter_attempt_never_includes_the_repository(self) -> None:
        node = _snapshot(
            AggregateType.DAG_NODE_RUN,
            "node-a",
            {"execution_adapter": "artifact_bundle.v2"},
        )
        workspace = {"repo_path": "/runtime/artifact-workspace", "workspace_binding": "canonical"}
        repo_root, workspace_root = _role_workspace_input_binding_roots(
            workspace,
            snapshot=node,
            workspace_source_root="/host/repo",
        )
        self.assertIsNone(repo_root)
        self.assertEqual(workspace_root, Path("/runtime/artifact-workspace"))

    def test_git_module_worktree_verifies_in_place(self) -> None:
        node = _snapshot(
            AggregateType.DAG_NODE_RUN,
            "node-g",
            {"execution_adapter": "software_git.v2"},
        )
        workspace = {"repo_path": "/runtime/module-worktree", "workspace_binding": "canonical"}
        repo_root, workspace_root = _role_workspace_input_binding_roots(
            workspace,
            snapshot=node,
            workspace_source_root="/host/repo",
        )
        self.assertEqual(repo_root, Path("/runtime/module-worktree"))
        self.assertIsNone(workspace_root)

    def test_prepared_workspace_from_source_includes_the_repository(self) -> None:
        architect = _snapshot(AggregateType.ARCHITECTURE_REVISION, "arch-1", {})
        workspace = {"repo_path": "/runtime/role-workspace", "v2_role_workspace": True}
        repo_root, workspace_root = _role_workspace_input_binding_roots(
            workspace,
            snapshot=architect,
            workspace_source_root="/host/repo",
        )
        self.assertEqual(repo_root, Path("/runtime/role-workspace"))
        self.assertIsNone(workspace_root)

    def test_prepared_workspace_without_source_materializes(self) -> None:
        architect = _snapshot(AggregateType.ARCHITECTURE_REVISION, "arch-2", {})
        workspace = {"repo_path": "/runtime/empty-role-workspace", "v2_role_workspace": True}
        repo_root, workspace_root = _role_workspace_input_binding_roots(
            workspace,
            snapshot=architect,
            workspace_source_root="",
        )
        self.assertIsNone(repo_root)
        self.assertEqual(workspace_root, Path("/runtime/empty-role-workspace"))

    def test_ephemeral_review_clone_verifies_in_place(self) -> None:
        review = _snapshot(AggregateType.STANDALONE_REVIEW, "rev-1", {})
        workspace = {
            "repo_path": "/runtime/standalone-review/worktree",
            "workspace_binding": "ephemeral_artifact",
        }
        repo_root, workspace_root = _role_workspace_input_binding_roots(
            workspace,
            snapshot=review,
            workspace_source_root="/host/repo",
        )
        self.assertEqual(repo_root, Path("/runtime/standalone-review/worktree"))
        self.assertIsNone(workspace_root)

    def test_workspace_without_any_root_fails_closed(self) -> None:
        review = _snapshot(AggregateType.STANDALONE_REVIEW, "rev-2", {})
        with self.assertRaises(BoundInputError):
            _role_workspace_input_binding_roots(
                {"run_dir": ""},
                snapshot=review,
                workspace_source_root="",
            )


class BindRoleAttemptInputsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal_orchestrator_binding_"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.service = BunshinV2WorkflowService(self.root)
        self.orchestrator = SemanticOrchestrator(self.service)
        self.manifest_ref = self.service.artifacts.put_json(
            {
                "schema_version": "1",
                "workflow_id": WORKFLOW_ID,
                "source_commit": "a" * 40,
                "inputs": [],
            },
            artifact_type="InputBindingManifestArtifact",
        ).to_dict()
        self.workflow = _snapshot(
            AggregateType.WORKFLOW,
            WORKFLOW_ID,
            {"input_binding_ref": self.manifest_ref},
        )
        self.request: dict = {}
        self.fake_manifest = SimpleNamespace(workflow_id=WORKFLOW_ID)

    def _bind(self, snapshot, workspace, *, source_root="/host/repo"):
        with (
            patch.object(orchestrator_module, "InputBindingManifest") as manifest_cls,
            patch.object(orchestrator_module, "materialize_bound_inputs") as materialize,
            patch.object(orchestrator_module, "verify_bound_inputs") as verify,
            patch.object(orchestrator_module, "verify_repo_bound_inputs") as verify_repo,
            patch.object(orchestrator_module, "bound_input_reference_entries") as entries,
        ):
            manifest_cls.from_payload.return_value = self.fake_manifest
            entries.return_value = [{"name": "docs", "bound_input": True}]
            result = self.orchestrator._bind_role_attempt_inputs(
                workflow=self.workflow,
                request=self.request,
                workspace=workspace,
                snapshot=snapshot,
                workspace_source_root=source_root,
            )
            return {
                "result": result,
                "manifest_cls": manifest_cls,
                "materialize": materialize,
                "verify": verify,
                "verify_repo": verify_repo,
                "entries": entries,
            }

    def test_missing_record_skips_binding_entirely(self) -> None:
        self.workflow = _snapshot(AggregateType.WORKFLOW, WORKFLOW_ID, {})
        calls = self._bind(
            _snapshot(AggregateType.DAG_NODE_RUN, "node-1", {}),
            {"repo_path": "/runtime/ws"},
        )
        self.assertEqual(calls["result"], [])
        calls["materialize"].assert_not_called()
        calls["verify"].assert_not_called()
        calls["verify_repo"].assert_not_called()
        calls["entries"].assert_not_called()

    def test_artifact_attempt_materializes_and_verifies_before_entry_projection(self) -> None:
        artifact_workspace = self.root / "artifact-workspace"
        artifact_workspace.mkdir()
        node = _snapshot(
            AggregateType.DAG_NODE_RUN,
            "node-2",
            {"execution_adapter": "artifact_bundle.v2"},
        )
        calls = self._bind(node, {"repo_path": str(artifact_workspace)})
        calls["manifest_cls"].from_payload.assert_called_once()
        calls["materialize"].assert_called_once_with(
            manifest=self.fake_manifest,
            artifacts=self.service.artifacts,
            destination_root=artifact_workspace,
        )
        calls["verify"].assert_called_once_with(
            manifest=self.fake_manifest,
            destination_root=artifact_workspace,
        )
        calls["verify_repo"].assert_not_called()
        calls["entries"].assert_called_once_with(
            manifest=self.fake_manifest,
            workspace_root=artifact_workspace,
        )
        self.assertEqual(calls["result"], [{"name": "docs", "bound_input": True}])

    def test_repository_attempt_verifies_in_place_without_materialization(self) -> None:
        node = _snapshot(
            AggregateType.DAG_NODE_RUN,
            "node-3",
            {"execution_adapter": "software_git.v2"},
        )
        module_worktree = self.root / "module-worktree"
        module_worktree.mkdir()
        calls = self._bind(
            node,
            {"repo_path": str(module_worktree), "workspace_binding": "canonical"},
        )
        calls["verify_repo"].assert_called_once_with(
            manifest=self.fake_manifest,
            repo_root=module_worktree,
        )
        calls["materialize"].assert_not_called()
        calls["verify"].assert_not_called()
        calls["entries"].assert_called_once_with(
            manifest=self.fake_manifest,
            repo_root=module_worktree,
        )

    def test_request_payload_record_is_honored_when_workflow_payload_lacks_it(self) -> None:
        self.workflow = _snapshot(AggregateType.WORKFLOW, WORKFLOW_ID, {})
        self.request = {"input_binding_ref": self.manifest_ref}
        node = _snapshot(
            AggregateType.DAG_NODE_RUN,
            "node-4",
            {"execution_adapter": "artifact_bundle.v2"},
        )
        workspace = self.root / "artifact-ws-2"
        workspace.mkdir()
        calls = self._bind(node, {"repo_path": str(workspace)})
        calls["materialize"].assert_called_once()

    def test_unreadable_manifest_record_fails_closed(self) -> None:
        self.workflow = _snapshot(
            AggregateType.WORKFLOW,
            WORKFLOW_ID,
            {
                "input_binding_ref": {
                    "sha256": "f" * 64,
                    "artifact_type": "InputBindingManifestArtifact",
                }
            },
        )
        node = _snapshot(
            AggregateType.DAG_NODE_RUN,
            "node-5",
            {"execution_adapter": "artifact_bundle.v2"},
        )
        workspace = self.root / "artifact-ws-3"
        workspace.mkdir()
        with self.assertRaises(BoundInputError):
            self._bind(node, {"repo_path": str(workspace)})

    def test_malformed_record_reference_fails_closed(self) -> None:
        for bad in ("sha256:abc", {"artifact_type": "InputBindingManifestArtifact"}, 42):
            with self.subTest(bad=bad):
                self.workflow = _snapshot(
                    AggregateType.WORKFLOW,
                    WORKFLOW_ID,
                    {"input_binding_ref": bad},
                )
                node = _snapshot(
                    AggregateType.DAG_NODE_RUN,
                    "node-6",
                    {"execution_adapter": "artifact_bundle.v2"},
                )
                with self.assertRaises(BoundInputError):
                    self._bind(node, {"repo_path": str(self.root)})

    def test_wrong_artifact_type_fails_closed(self) -> None:
        self.workflow = _snapshot(
            AggregateType.WORKFLOW,
            WORKFLOW_ID,
            {
                "input_binding_ref": {
                    **self.manifest_ref,
                    "artifact_type": "SomeOtherArtifact",
                }
            },
        )
        node = _snapshot(AggregateType.DAG_NODE_RUN, "node-wrong-type", {})
        with self.assertRaisesRegex(BoundInputError, "InputBindingManifestArtifact"):
            self._bind(node, {"repo_path": str(self.root)})

    def test_manifest_for_another_workflow_fails_closed(self) -> None:
        node = _snapshot(AggregateType.DAG_NODE_RUN, "node-wrong-owner", {})
        with patch.object(orchestrator_module, "InputBindingManifest") as manifest_cls:
            manifest_cls.from_payload.return_value = SimpleNamespace(workflow_id="wf-other")
            with self.assertRaisesRegex(BoundInputError, "different workflow"):
                self.orchestrator._bind_role_attempt_inputs(
                    workflow=self.workflow,
                    request=self.request,
                    workspace={"repo_path": str(self.root)},
                    snapshot=node,
                    workspace_source_root="/host/repo",
                )

    def test_hash_mismatch_fails_the_attempt_closed(self) -> None:
        node = _snapshot(
            AggregateType.DAG_NODE_RUN,
            "node-7",
            {"execution_adapter": "artifact_bundle.v2"},
        )
        workspace = self.root / "artifact-ws-4"
        workspace.mkdir()
        with (
            patch.object(orchestrator_module, "InputBindingManifest") as manifest_cls,
            patch.object(orchestrator_module, "materialize_bound_inputs"),
            patch.object(
                orchestrator_module,
                "verify_bound_inputs",
                side_effect=BoundInputError("input docs drifted"),
            ) as verify,
            patch.object(orchestrator_module, "verify_repo_bound_inputs"),
            patch.object(orchestrator_module, "bound_input_reference_entries"),
        ):
            manifest_cls.from_payload.return_value = self.fake_manifest
            with self.assertRaises(BoundInputError):
                self.orchestrator._bind_role_attempt_inputs(
                    workflow=self.workflow,
                    request=self.request,
                    workspace={"repo_path": str(workspace)},
                    snapshot=node,
                    workspace_source_root="/host/repo",
                )
        verify.assert_called_once()

    def test_binding_is_idempotent_across_repeated_attempts(self) -> None:
        node = _snapshot(
            AggregateType.DAG_NODE_RUN,
            "node-8",
            {"execution_adapter": "artifact_bundle.v2"},
        )
        workspace = self.root / "artifact-ws-5"
        workspace.mkdir()
        first = self._bind(node, {"repo_path": str(workspace)})
        second = self._bind(node, {"repo_path": str(workspace)})
        self.assertEqual(first["result"], second["result"])
        self.assertEqual(first["materialize"].call_count, 1)
        self.assertEqual(second["materialize"].call_count, 1)
        self.assertEqual(
            first["materialize"].call_args,
            second["materialize"].call_args,
        )


class BoundInputReadOnlyOverlayTests(unittest.TestCase):
    def test_bound_inputs_become_deduplicated_workspace_relative_overlays(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_bound_overlay_") as tmp:
            workspace_root = Path(tmp)
            first = workspace_root / "inputs" / "spec" / "docs" / "spec.md"
            second = workspace_root / "inputs" / "notes" / "notes.txt"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_text("spec\n", encoding="utf-8")
            second.write_text("notes\n", encoding="utf-8")
            workspace = {
                "repo_path": str(workspace_root),
                "read_only_overlay_paths": ["inputs/spec/docs/spec.md"],
            }

            _attach_bound_input_read_only_overlays(
                workspace,
                [
                    {"path": str(first), "bound_input": True},
                    {"path": str(second), "bound_input": True},
                ],
            )

            self.assertEqual(
                workspace["read_only_overlay_paths"],
                ["inputs/spec/docs/spec.md", "inputs/notes/notes.txt"],
            )


if __name__ == "__main__":
    unittest.main()
