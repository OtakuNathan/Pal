"""Sink verifier cases: bound-input delivery end to end through the real surface.

The orchestrator module is the authored graph sink for input binding.  These
cases exercise the four Manager-seeded system scenarios against the assembled
worktree with the real ``input_binding``, ``workflow_service``,
``semantic_orchestration``, ``execution_adapters``, and ``prompt_adapter``
modules — no contract doubles and no import-surface patches.

Environment note: the role sandbox exposes Git only through the read-only
gateway for the assigned workspace repository, so intake capture runs against
the bound module worktree (a real repository with a resolvable HEAD), exactly
like the sibling ``input_binding`` verifier corpus.  Repository-including
role workspaces are exercised through plain copies of the tracked input
files, which is all ``verify_repo_bound_inputs`` consumes.

Scenarios covered:

* ``general_family_bound_input_happy_path`` — real intake capture records the
  manifest ref with source commit and per-input SHA-256, and a real producer
  attempt's pre-dispatch binding stage materializes ``inputs/<name>/<repo_path>``
  verified before spawn and advertises it through the real prompt renderer.
* ``missing_or_escaping_input_fails_closed`` — real intake rejects a missing,
  absolute, or parent-traversing declaration before any workflow state exists,
  and the orchestrator's repository-including binding rejects a symlink that
  escapes the workspace root.
* ``recovery_rematerializes_identical_inputs`` — a replacement attempt after a
  simulated service restart re-materializes byte-identical inputs from the same
  durable manifest, and unavailable durable content or in-repo drift fails the
  attempt closed before spawn.
* ``artifact_candidate_excludes_bound_inputs`` — the real artifact-bundle
  adapter's candidate snapshot, tree fingerprint, verification workspace, and
  published deliverable exclude the materialized ``inputs/`` tree while keeping
  component-containment near misses.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pal.bunshin.prompt_adapter import render_bunshin_task_prompt
from pal.bunshin.v2.adapters import (
    ARTIFACT_BUNDLE_ADAPTER,
    ArtifactBundleAdapter,
    artifact_tree_fingerprint,
)
from pal.bunshin.v2.contracts import AggregateSnapshot, AggregateType
from pal.bunshin.v2.input_binding import (
    INPUT_BINDING_MANIFEST_ARTIFACT,
    BoundInputError,
    InputBindingManifest,
    verify_bound_inputs,
)
from pal.bunshin.v2.semantic_orchestration.orchestrator import SemanticOrchestrator
from pal.bunshin.v2.service import BunshinV2WorkflowService
from pal.shared.messages import BunshinInvocationPack

# The bound module workspace itself: a real read-only Git repository whose
# HEAD the provenance capture may resolve under the read-only Git gateway.
WORKSPACE_REPO = Path(__file__).resolve().parents[3]

# Stable tracked files of the workspace repository used as declared inputs.
SPEC_REPO_PATH = "pyproject.toml"
NOTES_REPO_PATH = "src/pal/bunshin/v2/input_binding.py"


def _workspace_head() -> str:
    completed = subprocess.run(
        ["git", "-C", str(WORKSPACE_REPO), "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    return completed.stdout.decode().strip()


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


def _start_request(task_id: str, references: list[dict]) -> dict:
    return {
        "workflow_id": f"wf-{task_id}",
        "task_id": task_id,
        "operation": "new_requirement",
        "goal": "Summarize the declared project documents",
        "task_spec": {"objective": "Summarize the declared project documents."},
        "references": references,
        "delivery_binding": {
            "channel_id": "socket_test",
            "channel_kind": "socket",
            "reply_target": {"session_id": "test-session", "request_id": "test-request"},
            "control_scope_key": "socket:socket_test:test-session",
        },
    }


class BoundInputDeliveryHarness(unittest.TestCase):
    """Shared real-surface harness for the four system scenarios."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal_sink_bound_inputs_"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.runtime_root = self.root / "runtime"
        self.runtime_root.mkdir()
        self.head = _workspace_head()
        self.service = BunshinV2WorkflowService(self.runtime_root)
        self.service.create_task(
            {
                "task_id": "task-bound",
                "title": "Summarize declared documents",
                "objective": "Summarize the declared project documents",
                "profile": "lifestyle.nutritionist",
                "workspace": {"repo_path": str(WORKSPACE_REPO)},
                "references": [{"name": "spec", "path": SPEC_REPO_PATH}],
            }
        )
        self.orchestrator = SemanticOrchestrator(self.service)

    def start_workflow(self, references: list[dict]) -> dict:
        return self.service.start_workflow(_start_request("task-bound", references))

    def workflow_snapshot(self, workflow_id: str) -> AggregateSnapshot:
        snapshot = self.service.repository.read_snapshot(
            AggregateType.WORKFLOW, workflow_id
        )
        self.assertIsNotNone(snapshot)
        return snapshot

    def manifest_for(self, workflow: AggregateSnapshot) -> InputBindingManifest:
        ref = dict(workflow.payload["input_binding_ref"])
        self.assertEqual(ref["artifact_type"], INPUT_BINDING_MANIFEST_ARTIFACT)
        return InputBindingManifest.from_payload(self.service.artifacts.read_json(ref))

    def artifact_workspace(self, name: str) -> Path:
        workspace = self.runtime_root / "artifact-workspaces" / name
        workspace.mkdir(parents=True)
        return workspace

    def bind_artifact_attempt(
        self,
        orchestrator: SemanticOrchestrator,
        *,
        workflow: AggregateSnapshot,
        workspace: Path,
    ) -> list[dict]:
        """Run the real pre-dispatch binding stage for one artifact attempt."""

        node = _snapshot(
            AggregateType.DAG_NODE_RUN,
            f"node-{workspace.name}",
            workflow.aggregate_id,
            {"execution_adapter": ARTIFACT_BUNDLE_ADAPTER},
        )
        return orchestrator._bind_role_attempt_inputs(
            workflow=workflow,
            request={},
            workspace={"repo_path": str(workspace)},
            snapshot=node,
            workspace_source_root=str(WORKSPACE_REPO),
        )

    def bind_repository_attempt(
        self,
        orchestrator: SemanticOrchestrator,
        *,
        workflow: AggregateSnapshot,
        repo_root: Path,
    ) -> list[dict]:
        """Run the real pre-dispatch binding stage for a repo-including attempt."""

        node = _snapshot(
            AggregateType.DAG_NODE_RUN,
            "node-git",
            workflow.aggregate_id,
            {"execution_adapter": "software_git.v2"},
        )
        return orchestrator._bind_role_attempt_inputs(
            workflow=workflow,
            request={},
            workspace={"repo_path": str(repo_root), "workspace_binding": "canonical"},
            snapshot=node,
            workspace_source_root=str(WORKSPACE_REPO),
        )

    def repository_worktree_copy(self, name: str) -> Path:
        """A repository-including role workspace copy of the declared inputs."""

        worktree = self.root / name
        (worktree / Path(NOTES_REPO_PATH).parent).mkdir(parents=True)
        shutil.copy2(WORKSPACE_REPO / SPEC_REPO_PATH, worktree / SPEC_REPO_PATH)
        shutil.copy2(WORKSPACE_REPO / NOTES_REPO_PATH, worktree / NOTES_REPO_PATH)
        return worktree


class GeneralFamilyHappyPathTests(BoundInputDeliveryHarness):
    """Scenario: general_family_bound_input_happy_path."""

    def test_intake_records_manifest_ref_with_commit_and_per_input_hash(self) -> None:
        result = self.start_workflow([{"name": "notes", "path": NOTES_REPO_PATH}])
        self.assertEqual(result["status"], "created")

        workflow = self.workflow_snapshot("wf-task-bound")
        manifest = self.manifest_for(workflow)

        # Task-revision reference plus the request reference, ordered by name.
        self.assertEqual(
            [(record.name, record.repo_path) for record in manifest.inputs],
            [("notes", NOTES_REPO_PATH), ("spec", SPEC_REPO_PATH)],
        )
        self.assertEqual(manifest.source_commit, self.head)
        for record in manifest.inputs:
            expected = hashlib.sha256(
                (WORKSPACE_REPO / record.repo_path).read_bytes()
            ).hexdigest()
            self.assertEqual(record.content_sha256, expected)
            self.assertTrue(
                self.service.repository.artifact_is_durable(record.content_ref.sha256)
            )

        # The same immutable ref is carried by the durable request artifact.
        request = self.service.artifacts.read_json(
            dict(workflow.payload["request_ref"])
        )
        self.assertEqual(
            request["input_binding_ref"], dict(workflow.payload["input_binding_ref"])
        )

    def test_producer_attempt_materializes_verifies_and_advertises(self) -> None:
        self.start_workflow([{"name": "notes", "path": NOTES_REPO_PATH}])
        workflow = self.workflow_snapshot("wf-task-bound")
        manifest = self.manifest_for(workflow)
        workspace = self.artifact_workspace("producer-ws")

        entries = self.bind_artifact_attempt(
            self.orchestrator, workflow=workflow, workspace=workspace
        )

        # Deterministic locations carry the exact captured bytes, verified.
        by_name = {entry["name"]: entry for entry in entries}
        self.assertEqual(set(by_name), {"notes", "spec"})
        for record in manifest.inputs:
            target = workspace / "inputs" / record.name / record.repo_path
            self.assertEqual(
                target.read_bytes(), (WORKSPACE_REPO / record.repo_path).read_bytes()
            )
            entry = by_name[record.name]
            self.assertEqual(entry["path"], str(target))
            self.assertEqual(entry["include"], [record.repo_path])
            self.assertEqual(entry["mode"], "read_only")
            self.assertTrue(entry["truth_source"])
            self.assertTrue(entry["bound_input"])
            self.assertTrue(entry["required"])
        verify_bound_inputs(manifest=manifest, destination_root=workspace)

        # The spawned worker's prompt advertises the deterministic in-workspace
        # path with exact read args and no host repository path.
        prompt = render_bunshin_task_prompt(
            BunshinInvocationPack(
                invocation_id="inv-producer",
                workspace={"reference_paths": entries},
            )
        )
        spec_path = str(workspace / "inputs" / "spec" / SPEC_REPO_PATH)
        self.assertIn(f"path={spec_path}", prompt)
        self.assertIn('"file_path":"' + spec_path.replace('"', '\\"') + '"', prompt)
        self.assertIn(f'projected_paths=["{SPEC_REPO_PATH}"]', prompt)
        self.assertNotIn(str(WORKSPACE_REPO), prompt)

        # The producer completes its artifact in the same workspace and the
        # settled candidate carries the artifact but never the bound inputs.
        deliverable = workspace / "deliverable" / "summary.md"
        deliverable.parent.mkdir(parents=True)
        deliverable.write_text("summary of declared documents\n", encoding="utf-8")
        adapter = ArtifactBundleAdapter(self.runtime_root, self.service.artifacts)
        candidate_ref, _ = adapter.snapshot_candidate(
            workspace=workspace,
            reference_only_paths=[],
            unit_contract_hash="contract-hash",
            dependency_output_hashes={},
            environment_fingerprint="env-fingerprint",
        )
        payload = self.service.artifacts.read_json(candidate_ref)
        candidate_paths = [str(item["path"]) for item in payload["files"]]
        self.assertIn("deliverable/summary.md", candidate_paths)
        self.assertFalse(
            any(path == "inputs" or path.startswith("inputs/") for path in candidate_paths),
            candidate_paths,
        )


class MissingOrEscapingInputFailsClosedTests(BoundInputDeliveryHarness):
    """Scenario: missing_or_escaping_input_fails_closed."""

    def assert_intake_fails_closed(self, references: list[dict], needle: str) -> None:
        with self.assertRaises(BoundInputError) as caught:
            self.start_workflow(references)
        self.assertIsInstance(caught.exception, ValueError)
        self.assertIn(needle, str(caught.exception))
        self.assertIsNone(
            self.service.repository.read_snapshot(
                AggregateType.WORKFLOW, "wf-task-bound"
            )
        )
        self.assertFalse(
            self.service.repository.search_workflows(
                actor_id="pal", task_id="task-bound", include_terminal=True, limit=10
            )
        )

    def test_missing_required_input_fails_intake(self) -> None:
        self.assert_intake_fails_closed(
            [{"name": "spec", "path": "docs/absent.md"}], "spec"
        )

    def test_absolute_repo_relative_declaration_fails_intake(self) -> None:
        self.assert_intake_fails_closed(
            [
                {
                    "name": "spec",
                    "path": str(WORKSPACE_REPO / SPEC_REPO_PATH),
                    "repo_relative": True,
                }
            ],
            "spec",
        )

    def test_parent_traversal_fails_intake(self) -> None:
        self.assert_intake_fails_closed(
            [{"name": "spec", "path": "../outside.md"}], "spec"
        )

    def test_symlink_escape_fails_the_repository_attempt_closed(self) -> None:
        # The escape rule is enforced by the same containment resolution the
        # intake preflight applies; the role sandbox's read-only Git gateway
        # prevents building a escaping-symlink fixture inside the one
        # repository intake may capture from, so the escape is exercised at
        # the orchestrator's repository-including binding boundary.
        self.start_workflow([{"name": "notes", "path": NOTES_REPO_PATH}])
        workflow = self.workflow_snapshot("wf-task-bound")
        worktree = self.repository_worktree_copy("escape-worktree")
        (self.root / "outside.txt").write_text("outside the workspace\n", encoding="utf-8")
        (worktree / "docs").mkdir()
        (worktree / "docs" / "escape.md").symlink_to("../../outside.txt")

        escape_manifest = self.service.artifacts.put_json(
            {
                "schema_version": "1",
                "workflow_id": workflow.aggregate_id,
                "source_commit": self.head,
                "inputs": [
                    {
                        "name": "escape",
                        "repo_path": "docs/escape.md",
                        "source_commit": self.head,
                        "content_sha256": hashlib.sha256(b"outside").hexdigest(),
                        "byte_size": 7,
                        "content_ref": self.service.artifacts.put_bytes(
                            b"outside", artifact_type="BoundInputArtifact"
                        ).to_dict(),
                        "required": True,
                    }
                ],
            },
            artifact_type=INPUT_BINDING_MANIFEST_ARTIFACT,
        )
        workflow = _snapshot(
            AggregateType.WORKFLOW,
            workflow.aggregate_id,
            workflow.workflow_id,
            {"input_binding_ref": escape_manifest.to_dict()},
        )
        with self.assertRaises(BoundInputError) as caught:
            self.bind_repository_attempt(
                self.orchestrator, workflow=workflow, repo_root=worktree
            )
        self.assertIn("escape", str(caught.exception))


class RecoveryRematerializesIdenticalInputsTests(BoundInputDeliveryHarness):
    """Scenario: recovery_rematerializes_identical_inputs."""

    def test_replacement_attempt_after_restart_rematerializes_identical_inputs(self) -> None:
        self.start_workflow([{"name": "notes", "path": NOTES_REPO_PATH}])
        workflow_before = self.workflow_snapshot("wf-task-bound")
        attempt_one = self.artifact_workspace("attempt-1")
        self.bind_artifact_attempt(
            self.orchestrator, workflow=workflow_before, workspace=attempt_one
        )
        manifest = self.manifest_for(workflow_before)
        first_bytes = {
            record.name: (attempt_one / "inputs" / record.name / record.repo_path).read_bytes()
            for record in manifest.inputs
        }

        # Full service restart over the same durable runtime root: the
        # replacement attempt (fencing token 2) re-materializes from the same
        # immutable manifest recorded before the restart.
        restarted = BunshinV2WorkflowService(self.runtime_root)
        replacement = SemanticOrchestrator(restarted)
        workflow_after = restarted.repository.read_snapshot(
            AggregateType.WORKFLOW, "wf-task-bound"
        )
        self.assertEqual(
            workflow_after.payload["input_binding_ref"],
            workflow_before.payload["input_binding_ref"],
        )

        attempt_two = self.runtime_root / "artifact-workspaces" / "attempt-2"
        attempt_two.mkdir(parents=True)
        entries = self.bind_artifact_attempt(
            replacement, workflow=workflow_after, workspace=attempt_two
        )
        for record in manifest.inputs:
            target = attempt_two / "inputs" / record.name / record.repo_path
            self.assertEqual(target.read_bytes(), first_bytes[record.name])
            self.assertEqual(
                hashlib.sha256(target.read_bytes()).hexdigest(), record.content_sha256
            )
        self.assertTrue(all(entry["bound_input"] for entry in entries))

        # A stale fenced attempt re-executing the same immutable manifest can
        # only rewrite identical bytes; binding stays deterministic.
        again = self.bind_artifact_attempt(
            replacement, workflow=workflow_after, workspace=attempt_two
        )
        self.assertEqual(again, entries)
        for record in manifest.inputs:
            target = attempt_two / "inputs" / record.name / record.repo_path
            self.assertEqual(target.read_bytes(), first_bytes[record.name])

    def test_unavailable_durable_content_fails_the_replacement_attempt_closed(self) -> None:
        self.start_workflow([{"name": "notes", "path": NOTES_REPO_PATH}])
        workflow = self.workflow_snapshot("wf-task-bound")
        manifest = self.manifest_for(workflow)
        record = manifest.inputs[0]

        # The durable blob for the first input becomes unavailable (disk loss).
        blob = (
            self.service.artifacts.root
            / record.content_ref.sha256[:2]
            / record.content_ref.sha256
        )
        self.assertTrue(blob.is_file())
        blob.unlink()

        workspace = self.artifact_workspace("attempt-starved")
        with self.assertRaises(BoundInputError) as caught:
            self.bind_artifact_attempt(
                self.orchestrator, workflow=workflow, workspace=workspace
            )
        self.assertIn(record.name, str(caught.exception))
        # Fail-closed: no bound input was retained for the failed attempt.
        self.assertFalse((workspace / "inputs").exists())

    def test_drifted_in_repo_input_fails_repository_attempt_closed(self) -> None:
        self.start_workflow([{"name": "notes", "path": NOTES_REPO_PATH}])
        workflow = self.workflow_snapshot("wf-task-bound")
        worktree = self.repository_worktree_copy("drift-worktree")

        entries = self.bind_repository_attempt(
            self.orchestrator, workflow=workflow, repo_root=worktree
        )
        self.assertTrue(entries)
        self.assertTrue(all(entry["bound_input"] for entry in entries))
        self.assertTrue(all(str(worktree) in entry["path"] for entry in entries))

        # An in-repo input drifting from the recorded hash fails closed before
        # the replacement worker spawns.
        (worktree / SPEC_REPO_PATH).write_text("drifted worktree content\n", encoding="utf-8")
        with self.assertRaises(BoundInputError) as caught:
            self.bind_repository_attempt(
                self.orchestrator, workflow=workflow, repo_root=worktree
            )
        self.assertIn("spec", str(caught.exception))


class ArtifactCandidateExcludesBoundInputsTests(BoundInputDeliveryHarness):
    """Scenario: artifact_candidate_excludes_bound_inputs."""

    def test_candidate_fingerprint_and_deliverable_exclude_bound_inputs(self) -> None:
        self.start_workflow([{"name": "notes", "path": NOTES_REPO_PATH}])
        workflow = self.workflow_snapshot("wf-task-bound")
        workspace = self.artifact_workspace("sink-ws")

        # Worker-authored content first, including the component-containment
        # near misses that must always remain bundle content.
        (workspace / "deliverable").mkdir()
        (workspace / "deliverable" / "summary.md").write_text(
            "summary of declared documents\n", encoding="utf-8"
        )
        (workspace / "src" / "inputs").mkdir(parents=True)
        (workspace / "src" / "inputs" / "data.py").write_text(
            "NEAR_MISS = True\n", encoding="utf-8"
        )
        base_fingerprint = artifact_tree_fingerprint(workspace)

        # The Manager-side materializer writes the bound inputs into the same
        # workspace; base digests must stay stable across materialization.
        self.bind_artifact_attempt(
            self.orchestrator, workflow=workflow, workspace=workspace
        )
        self.assertTrue(
            (workspace / "inputs" / "spec" / SPEC_REPO_PATH).is_file()
        )
        self.assertEqual(artifact_tree_fingerprint(workspace), base_fingerprint)

        adapter = ArtifactBundleAdapter(self.runtime_root, self.service.artifacts)
        candidate_ref, _ = adapter.snapshot_candidate(
            workspace=workspace,
            reference_only_paths=[],
            unit_contract_hash="contract-hash",
            dependency_output_hashes={},
            environment_fingerprint="env-fingerprint",
        )
        payload = self.service.artifacts.read_json(candidate_ref)
        candidate_paths = [str(item["path"]) for item in payload["files"]]
        self.assertIn("src/inputs/data.py", candidate_paths)
        self.assertIn("deliverable/summary.md", candidate_paths)
        self.assertFalse(
            any(path == "inputs" or path.startswith("inputs/") for path in candidate_paths),
            candidate_paths,
        )
        self.assertEqual(payload["tree_fingerprint"], base_fingerprint)

        # The fingerprint equals the same workspace without materialized inputs.
        without_inputs = self.root / "sink-ws-without-inputs"
        shutil.copytree(workspace, without_inputs)
        shutil.rmtree(without_inputs / "inputs")
        self.assertEqual(
            payload["tree_fingerprint"], artifact_tree_fingerprint(without_inputs)
        )

        # The verification workspace materializes the candidate without inputs.
        candidate_dir, _scratch = adapter.prepare_verification_workspace(
            review_id="review-sink", candidate_ref=candidate_ref
        )
        verification_paths = {
            item.relative_to(candidate_dir).as_posix()
            for item in candidate_dir.rglob("*")
            if item.is_file()
        }
        self.assertFalse(
            any(path == "inputs" or path.startswith("inputs/") for path in verification_paths),
            verification_paths,
        )
        self.assertIn("src/inputs/data.py", verification_paths)

        # The published deliverable carries no bound-input file either.
        verification_ref = self.service.artifacts.put_json(
            {"verdict": "pass"}, artifact_type="VerificationArtifact"
        )
        deliverable_ref = adapter.publish_deliverable(
            workflow_id="wf-task-bound",
            candidate_ref=candidate_ref.to_dict(),
            verification_ref=verification_ref,
        )
        deliverable = self.service.artifacts.read_json(deliverable_ref)
        deliverable_paths = [str(item["path"]) for item in deliverable["files"]]
        self.assertFalse(
            any(path == "inputs" or path.startswith("inputs/") for path in deliverable_paths),
            deliverable_paths,
        )
        destination = Path(deliverable["destination"])
        published_paths = {
            item.relative_to(destination).as_posix()
            for item in destination.rglob("*")
            if item.is_file()
        }
        self.assertFalse(
            any(path == "inputs" or path.startswith("inputs/") for path in published_paths),
            published_paths,
        )
        self.assertIn("deliverable/summary.md", published_paths)


if __name__ == "__main__":
    unittest.main()
