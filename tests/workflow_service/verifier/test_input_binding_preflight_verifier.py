"""Verifier cases for the workflow intake input-binding preflight.

``input_binding`` is a parallel dependency module: in this worktree its
procedures are contract declarations, so these cases drive the real
``BunshinV2WorkflowService.start_workflow`` against contract-conforming
doubles installed at the ``pal.bunshin.v2.service`` import boundary, exactly
as the declared edge contract (participation rule, fail-closed errors,
manifest publication) prescribes.

They close the gaps the developer corpus leaves open:

* a binding failure at either dependency stage publishes no
  ``WorkflowRequestArtifact`` and no ``InputBindingManifestArtifact`` and
  creates no workflow aggregate (fail-closed ordering);
* the binding repository root is resolved from the task workspace, never
  from a caller-supplied request workspace path;
* the recorded ``input_binding_ref`` addresses exactly one durable
  ``InputBindingManifestArtifact`` whose bound-input content is durable and
  byte-identical to the task repository source.
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

SOURCE_COMMIT = "1" * 40


@dataclass(frozen=True)
class StubBoundInputRecord:
    name: str
    repo_path: str
    source_commit: str
    content_sha256: str
    byte_size: int
    content_ref: ArtifactRef


class StubInputBindingManifest:
    """Contract-conforming stand-in for ``InputBindingManifest``."""

    def __init__(
        self, workflow_id: str, source_commit: str, records: Sequence[StubBoundInputRecord]
    ) -> None:
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


class InputBindingEdgeDouble:
    """Records calls and mirrors the declared input_binding capture edge."""

    def __init__(self) -> None:
        self.declared_calls: list[list[Any]] = []
        self.capture_calls: list[dict[str, Any]] = []
        self.declarations: tuple[DeclaredInput, ...] = ()
        self.declared_error: BoundInputError | None = None
        self.capture_error: BoundInputError | None = None

    def declared_inputs_from_references(
        self, references: Sequence[Any]
    ) -> tuple[DeclaredInput, ...]:
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
    ) -> StubInputBindingManifest:
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
        records: list[StubBoundInputRecord] = []
        for declaration in declarations:
            data = (Path(repo_root) / declaration.repo_path).read_bytes()
            content_ref = artifacts.put_bytes(data, artifact_type=BOUND_INPUT_ARTIFACT)
            records.append(
                StubBoundInputRecord(
                    name=declaration.name,
                    repo_path=declaration.repo_path,
                    source_commit=SOURCE_COMMIT,
                    content_sha256=hashlib.sha256(data).hexdigest(),
                    byte_size=len(data),
                    content_ref=content_ref,
                )
            )
        return StubInputBindingManifest(workflow_id, SOURCE_COMMIT, records)


@pytest.fixture()
def environment(tmp_path: Path) -> SimpleNamespace:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    repo = tmp_path / "task-repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "spec.md").write_text("# task repository spec\n", encoding="utf-8")
    (repo / "docs" / "notes.md").write_text("task repository notes\n", encoding="utf-8")
    decoy_repo = tmp_path / "decoy-repo"
    (decoy_repo / "docs").mkdir(parents=True)
    (decoy_repo / "docs" / "spec.md").write_text("decoy content\n", encoding="utf-8")
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
    return SimpleNamespace(
        runtime_root=runtime_root, repo=repo, decoy_repo=decoy_repo, service=service
    )


def install_double(
    monkeypatch: pytest.MonkeyPatch,
    *,
    declarations: tuple[DeclaredInput, ...] = (),
    declared_error: BoundInputError | None = None,
    capture_error: BoundInputError | None = None,
) -> InputBindingEdgeDouble:
    double = InputBindingEdgeDouble()
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


@pytest.mark.parametrize("stage", ["declaration", "capture"])
def test_binding_failure_publishes_no_workflow_artifacts(
    environment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """A binding failure at either edge stage fails closed before the
    WorkflowRequestArtifact or the manifest artifact is published and before
    any workflow aggregate exists."""
    service = environment.service
    published: list[str] = []
    original_put_json = service.artifacts.put_json

    def recording_put_json(value: Any, *, artifact_type: str, **kwargs: Any) -> ArtifactRef:
        published.append(artifact_type)
        return original_put_json(value, artifact_type=artifact_type, **kwargs)

    monkeypatch.setattr(service.artifacts, "put_json", recording_put_json)
    if stage == "declaration":
        install_double(
            monkeypatch,
            declared_error=BoundInputError(
                "declared input 'spec' traverses parents: ../outside.md"
            ),
        )
    else:
        install_double(
            monkeypatch,
            declarations=(DeclaredInput(name="spec", repo_path="docs/spec.md"),),
            capture_error=BoundInputError("required input 'spec' is missing at the source"),
        )

    with pytest.raises(BoundInputError) as excinfo:
        service.start_workflow(
            start_request(references=[{"name": "spec", "path": "docs/spec.md"}])
        )
    assert isinstance(excinfo.value, ValueError)

    assert "WorkflowRequestArtifact" not in published
    assert INPUT_BINDING_MANIFEST_ARTIFACT not in published
    assert read_workflow(environment, "wf-bound") is None
    assert not service.repository.search_workflows(
        actor_id="pal", task_id="task-bound", include_terminal=True, limit=10
    )


def test_binding_repo_root_comes_from_task_workspace_not_request(
    environment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller-supplied request workspace is rejected outright: the binding
    repository root can only ever be the task workspace repository."""
    double = install_double(
        monkeypatch,
        declarations=(DeclaredInput(name="spec", repo_path="docs/spec.md"),),
    )
    with pytest.raises(ValueError, match="workspace is owned by Task"):
        environment.service.start_workflow(
            start_request(workspace={"repo_path": str(environment.decoy_repo)})
        )
    assert double.capture_calls == []
    assert read_workflow(environment, "wf-bound") is None

    # Without the override the capture root is the task workspace repository
    # and the bound bytes are the task repository's, not any other source.
    result = environment.service.start_workflow(start_request())
    assert result["status"] == "created"
    assert len(double.capture_calls) == 1
    assert double.capture_calls[0]["repo_root"] == Path(environment.repo)
    workflow = read_workflow(environment, "wf-bound")
    assert workflow is not None
    manifest = environment.service.artifacts.read_json(dict(workflow.payload["input_binding_ref"]))
    entry = manifest["inputs"][0]
    task_bytes = (Path(environment.repo) / "docs" / "spec.md").read_bytes()
    decoy_bytes = (Path(environment.decoy_repo) / "docs" / "spec.md").read_bytes()
    assert entry["content_sha256"] == hashlib.sha256(task_bytes).hexdigest()
    assert entry["content_sha256"] != hashlib.sha256(decoy_bytes).hexdigest()


def test_recorded_binding_ref_addresses_one_durable_manifest(
    environment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorded input_binding_ref addresses exactly one durable
    InputBindingManifestArtifact, mirrored verbatim in the workflow aggregate
    payload and the WorkflowRequestArtifact payload, whose bound-input
    content is durable and byte-identical to the task repository source."""
    install_double(
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

    workflow = read_workflow(environment, "wf-bound")
    assert workflow is not None
    recorded = dict(workflow.payload["input_binding_ref"])
    assert recorded["artifact_type"] == INPUT_BINDING_MANIFEST_ARTIFACT
    assert recorded["schema_version"] == INPUT_BINDING_SCHEMA_VERSION

    artifact_record = environment.service.repository.read_artifact_record(recorded["sha256"])
    assert artifact_record is not None
    assert artifact_record["artifact_type"] == INPUT_BINDING_MANIFEST_ARTIFACT
    assert artifact_record["durable"] is True

    manifest = environment.service.artifacts.read_json(recorded)
    assert manifest["schema_version"] == INPUT_BINDING_SCHEMA_VERSION
    assert manifest["workflow_id"] == "wf-bound"
    assert [entry["name"] for entry in manifest["inputs"]] == ["notes", "spec"]

    request = environment.service.artifacts.read_json(dict(workflow.payload["request_ref"]))
    assert request["input_binding_ref"] == recorded

    for entry in manifest["inputs"]:
        source_bytes = (Path(environment.repo) / entry["repo_path"]).read_bytes()
        assert entry["content_sha256"] == hashlib.sha256(source_bytes).hexdigest()
        assert entry["byte_size"] == len(source_bytes)
        assert environment.service.repository.artifact_is_durable(entry["content_ref"]["sha256"])
        assert environment.service.artifacts.read_bytes(dict(entry["content_ref"])) == source_bytes
