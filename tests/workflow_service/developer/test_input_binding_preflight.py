"""Developer tests for the workflow intake input-binding preflight.

These tests exercise ``BunshinV2WorkflowService.start_workflow``'s
pre-dispatch binding stage: declared repo-relative inputs are captured and
recorded before ``CREATE_WORKFLOW`` dispatch, and every binding failure
fails closed before any workflow state exists.

The ``input_binding`` module is a parallel dependency; its public contract
(``declared_inputs_from_references`` / ``capture_input_binding`` /
``InputBindingManifest``) is mirrored here by contract-conforming doubles
patched at the ``pal.bunshin.v2.service`` import boundary.  The doubles
store real ``BoundInputArtifact`` blobs through the real artifact store so
the manifest publication path (child refs, durable records) is exercised
against production code.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

import pytest

import pal.bunshin.v2.service as service_module
from pal.bunshin.v2.artifacts import ArtifactRef
from pal.bunshin.v2.contracts import AggregateType
from pal.bunshin.v2.input_binding import (
    BOUND_INPUT_ARTIFACT,
    INPUT_BINDING_MANIFEST_ARTIFACT,
    INPUT_BINDING_SCHEMA_VERSION,
    BoundInputError,
    DeclaredInput,
)
from pal.bunshin.v2.service import BunshinV2WorkflowService

SOURCE_COMMIT = "0" * 40


@dataclass(frozen=True)
class FakeBoundInputRecord:
    name: str
    repo_path: str
    source_commit: str
    content_sha256: str
    byte_size: int
    content_ref: ArtifactRef


class FakeInputBindingManifest:
    """Contract-conforming stand-in for ``InputBindingManifest``."""

    def __init__(self, workflow_id: str, source_commit: str, records: Sequence[FakeBoundInputRecord]) -> None:
        self.inputs = tuple(records)
        self._workflow_id = workflow_id
        self._source_commit = source_commit

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": INPUT_BINDING_SCHEMA_VERSION,
            "workflow_id": self._workflow_id,
            "source_commit": self._source_commit,
            "inputs": [
                {
                    "name": record.name,
                    "repo_path": record.repo_path,
                    "source_commit": record.source_commit,
                    "content_sha256": record.content_sha256,
                    "byte_size": record.byte_size,
                    "content_ref": record.content_ref.to_dict(),
                }
                for record in self.inputs
            ],
        }


class InputBindingDouble:
    """Records calls and mirrors the public input_binding capture contract."""

    def __init__(self) -> None:
        self.declared_calls: list[list[Any]] = []
        self.capture_calls: list[dict[str, Any]] = []
        self.declarations: tuple[DeclaredInput, ...] = ()
        self.declared_error: BoundInputError | None = None
        self.capture_error: BoundInputError | None = None

    def declared_inputs_from_references(self, references: Sequence[Any]) -> tuple[DeclaredInput, ...]:
        self.declared_calls.append(list(references))
        if self.declared_error is not None:
            raise self.declared_error
        return self.declarations

    def capture_input_binding(
        self,
        *,
        repo_root: Path,
        workflow_id: str,
        declarations: Sequence[DeclaredInput],
        artifacts: Any,
    ) -> FakeInputBindingManifest:
        self.capture_calls.append(
            {
                "repo_root": repo_root,
                "workflow_id": workflow_id,
                "declarations": tuple(declarations),
                "artifacts": artifacts,
            }
        )
        if self.capture_error is not None:
            raise self.capture_error
        records: list[FakeBoundInputRecord] = []
        for declaration in declarations:
            data = (Path(repo_root) / declaration.repo_path).read_bytes()
            content_ref = artifacts.put_bytes(data, artifact_type=BOUND_INPUT_ARTIFACT)
            records.append(
                FakeBoundInputRecord(
                    name=declaration.name,
                    repo_path=declaration.repo_path,
                    source_commit=SOURCE_COMMIT,
                    content_sha256=hashlib.sha256(data).hexdigest(),
                    byte_size=len(data),
                    content_ref=content_ref,
                )
            )
        return FakeInputBindingManifest(workflow_id, SOURCE_COMMIT, records)


@pytest.fixture()
def environment(tmp_path: Path) -> SimpleNamespace:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    repo = tmp_path / "source-repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "spec.md").write_text("# specification\n", encoding="utf-8")
    (repo / "docs" / "notes.md").write_text("meeting notes\n", encoding="utf-8")
    service = BunshinV2WorkflowService(runtime_root)
    service.create_task(
        {
            "task_id": "task-bound",
            "title": "Summarize declared documents",
            "objective": "Summarize the declared project documents",
            "profile": "lifestyle.nutritionist",
            "workspace": {"repo_path": str(repo)},
            "references": [{"name": "spec", "path": "docs/spec.md"}],
        }
    )
    return SimpleNamespace(runtime_root=runtime_root, repo=repo, service=service)


def install_double(
    monkeypatch: pytest.MonkeyPatch,
    *,
    declarations: tuple[DeclaredInput, ...] = (),
    declared_error: BoundInputError | None = None,
    capture_error: BoundInputError | None = None,
) -> InputBindingDouble:
    double = InputBindingDouble()
    double.declarations = declarations
    double.declared_error = declared_error
    double.capture_error = capture_error
    monkeypatch.setattr(
        service_module, "declared_inputs_from_references", double.declared_inputs_from_references
    )
    monkeypatch.setattr(service_module, "capture_input_binding", double.capture_input_binding)
    return double


def start_request(**overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "workflow_id": "wf-bound",
        "task_id": "task-bound",
        "operation": "new_requirement",
        "goal": "Summarize the declared project documents",
        "task_spec": {"objective": "Summarize the declared project documents."},
        "delivery_binding": {
            "channel_id": "socket_test",
            "channel_kind": "socket",
            "reply_target": {"session_id": "test-session", "request_id": "test-request"},
            "control_scope_key": "socket:socket_test:test-session",
        },
    }
    request.update(overrides)
    return request


def read_workflow(environment: SimpleNamespace, workflow_id: str) -> Any:
    return environment.service.repository.read_snapshot(AggregateType.WORKFLOW, workflow_id)


def test_declared_inputs_are_captured_and_recorded_before_dispatch(
    environment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    double = install_double(
        monkeypatch,
        declarations=(
            DeclaredInput(name="notes", repo_path="docs/notes.md"),
            DeclaredInput(name="spec", repo_path="docs/spec.md"),
        ),
    )
    result = environment.service.start_workflow(
        start_request(references=[{"name": "notes", "path": "docs/notes.md"}])
    )
    assert result["status"] == "created"

    # Declarations were extracted from the merged references: task-revision
    # references first, then the request references.
    assert len(double.declared_calls) == 1
    merged = [(entry["name"], entry["path"]) for entry in double.declared_calls[0]]
    assert merged == [("spec", "docs/spec.md"), ("notes", "docs/notes.md")]

    # Capture ran against the task workspace repository root.
    assert len(double.capture_calls) == 1
    call = double.capture_calls[0]
    assert call["repo_root"] == Path(environment.repo)
    assert call["workflow_id"] == "wf-bound"
    assert call["declarations"] == double.declarations
    assert call["artifacts"] is environment.service.artifacts

    # The manifest was published durably and recorded in the workflow
    # aggregate payload and the WorkflowRequestArtifact payload.
    workflow = read_workflow(environment, "wf-bound")
    assert workflow is not None
    recorded = dict(workflow.payload["input_binding_ref"])
    assert recorded["artifact_type"] == INPUT_BINDING_MANIFEST_ARTIFACT
    manifest = environment.service.artifacts.read_json(recorded)
    assert manifest["schema_version"] == INPUT_BINDING_SCHEMA_VERSION
    assert manifest["workflow_id"] == "wf-bound"
    assert manifest["source_commit"] == SOURCE_COMMIT
    assert [entry["name"] for entry in manifest["inputs"]] == ["notes", "spec"]
    for entry in manifest["inputs"]:
        expected = hashlib.sha256(
            (Path(environment.repo) / entry["repo_path"]).read_bytes()
        ).hexdigest()
        assert entry["content_sha256"] == expected
        assert environment.service.repository.artifact_is_durable(entry["content_ref"]["sha256"])

    request = environment.service.artifacts.read_json(dict(workflow.payload["request_ref"]))
    assert request["input_binding_ref"] == recorded
    artifact_record = environment.service.repository.read_artifact_record(recorded["sha256"])
    assert artifact_record is not None
    assert artifact_record["artifact_type"] == INPUT_BINDING_MANIFEST_ARTIFACT


def test_workflow_without_declared_inputs_keeps_intake_unchanged(
    environment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    double = install_double(monkeypatch, declarations=())
    result = environment.service.start_workflow(start_request())
    assert result["status"] == "created"
    # The task-revision reference was still examined for participation; the
    # dependency reported no participating declarations, so no capture runs.
    assert len(double.declared_calls) == 1
    assert [(entry["name"], entry["path"]) for entry in double.declared_calls[0]] == [
        ("spec", "docs/spec.md")
    ]
    assert double.capture_calls == []

    workflow = read_workflow(environment, "wf-bound")
    assert workflow is not None
    assert "input_binding_ref" not in workflow.payload
    request = environment.service.artifacts.read_json(dict(workflow.payload["request_ref"]))
    assert "input_binding_ref" not in request


def test_invalid_declaration_fails_closed_before_workflow_creation(
    environment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_double(
        monkeypatch,
        declared_error=BoundInputError(
            "declared input 'spec' traverses parents: ../outside.md"
        ),
    )
    with pytest.raises(BoundInputError) as excinfo:
        environment.service.start_workflow(
            start_request(references=[{"name": "spec", "path": "../outside.md"}])
        )
    assert isinstance(excinfo.value, ValueError)
    assert "spec" in str(excinfo.value)
    assert read_workflow(environment, "wf-bound") is None
    assert not environment.service.repository.search_workflows(
        actor_id="pal", task_id="task-bound", include_terminal=True, limit=10
    )


def test_capture_failure_fails_closed_before_workflow_creation(
    environment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_double(
        monkeypatch,
        declarations=(DeclaredInput(name="spec", repo_path="docs/spec.md"),),
        capture_error=BoundInputError("required input 'spec' is missing at the source"),
    )
    with pytest.raises(BoundInputError) as excinfo:
        environment.service.start_workflow(
            start_request(references=[{"name": "spec", "path": "docs/spec.md"}])
        )
    assert "spec" in str(excinfo.value)
    assert read_workflow(environment, "wf-bound") is None
    assert not environment.service.repository.search_workflows(
        actor_id="pal", task_id="task-bound", include_terminal=True, limit=10
    )


def test_declared_inputs_without_workspace_repository_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path / "runtime-no-repo"
    runtime_root.mkdir()
    service = BunshinV2WorkflowService(runtime_root)
    service.create_task(
        {
            "task_id": "task-no-repo",
            "title": "Artifact project task",
            "objective": "Produce an artifact",
            "profile": "lifestyle.nutritionist",
            "workspace": {"kind": "artifact_project", "project_name": "nutrition"},
        }
    )
    double = install_double(
        monkeypatch,
        declarations=(DeclaredInput(name="spec", repo_path="docs/spec.md"),),
    )
    with pytest.raises(BoundInputError) as excinfo:
        service.start_workflow(
            start_request(task_id="task-no-repo", references=[{"name": "spec", "path": "docs/spec.md"}])
        )
    assert "task workspace repository" in str(excinfo.value)
    assert double.capture_calls == []
    assert service.repository.read_snapshot(AggregateType.WORKFLOW, "wf-bound") is None
