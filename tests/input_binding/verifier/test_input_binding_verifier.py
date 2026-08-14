"""Verifier corpus for ``pal.bunshin.v2.input_binding``.

The developer corpus builds Git fixture repositories with ``git init`` /
``git add`` / ``git commit``, which the sandboxed read-only Git gateway traps,
so its Git-dependent cases cannot execute in this environment.  These cases
cover the same contract paths in an environment-compatible way: plain
temporary directories for containment resolution and in-repo verification,
and the bound module workspace's own read-only Git repository (the only Git
cwd the gateway admits) for provenance capture.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import pytest

from pal.bunshin.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.v2.input_binding import (
    BOUND_INPUT_ARTIFACT,
    BOUND_INPUTS_ROOT,
    BoundInputError,
    BoundInputRecord,
    DeclaredInput,
    InputBindingManifest,
    bound_input_reference_entries,
    bound_input_target_path,
    capture_input_binding,
    declared_inputs_from_references,
    is_bound_input_path,
    materialize_bound_inputs,
    resolve_within_repo,
    verify_bound_inputs,
    verify_repo_bound_inputs,
)

# The bound module workspace itself: a real read-only Git repository whose
# HEAD the provenance probe may resolve under the read-only Git gateway.
WORKSPACE_REPO = Path(__file__).resolve().parents[3]

# Stable tracked files of the workspace repository used as capture sources.
MODULE_SOURCE = "src/pal/bunshin/v2/input_binding.py"
PROJECT_SOURCE = "pyproject.toml"


class RecordingMetadataRepository:
    """Minimal in-memory ``ArtifactMetadataPort`` for the artifact store."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    def record_artifact(
        self,
        ref: ArtifactRef,
        *,
        storage_path: Path,
        provenance: Mapping[str, Any],
        metadata: Mapping[str, Any],
        child_refs: tuple[tuple[str, str], ...],
    ) -> None:
        self.records[ref.sha256] = {
            **ref.to_dict(),
            "storage_path": str(storage_path),
            "provenance": dict(provenance),
            "metadata": dict(metadata),
            "child_refs": list(child_refs),
        }

    def read_artifact_record(self, sha256: str) -> Mapping[str, Any] | None:
        return self.records.get(sha256)


@pytest.fixture()
def store(tmp_path: Path) -> ContentAddressedArtifactStore:
    return ContentAddressedArtifactStore(
        tmp_path / "runtime", RecordingMetadataRepository()
    )


def _plain_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _workspace_head() -> str:
    completed = subprocess.run(
        ["git", "-C", str(WORKSPACE_REPO), "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    return completed.stdout.decode().strip()


# --- participation, derivation, and the exclusion predicate ----------------


def test_verifier_participation_rules_fail_closed() -> None:
    declared = declared_inputs_from_references(
        [
            {"name": "module", "path": MODULE_SOURCE},
            {"name": "host", "path": "/etc/hosts"},
            {"name": "win", "path": "Q:/Hosts"},
            {"name": "marked", "path": PROJECT_SOURCE, "repo_relative": True},
            {"name": "optional", "path": "docs/optional.md", "required": False},
            {"path": "/abs/ignored", "repo_relative": False},
            "not-a-mapping",
        ]
    )
    assert declared == (
        DeclaredInput(name="marked", repo_path=PROJECT_SOURCE, required=True),
        DeclaredInput(name="module", repo_path=MODULE_SOURCE, required=True),
        DeclaredInput(name="optional", repo_path="docs/optional.md", required=False),
    )
    # A repo_relative-marked absolute path is a participating declaration
    # that must raise, never a silent host-path fallback.
    with pytest.raises(BoundInputError):
        declared_inputs_from_references(
            [{"name": "a", "path": "/etc/hosts", "repo_relative": True}]
        )
    with pytest.raises(BoundInputError):
        declared_inputs_from_references(
            [{"name": "a", "path": "C:/x.md", "repo_relative": True}]
        )
    # Duplicate derived names and malformed participating shapes fail closed.
    with pytest.raises(BoundInputError):
        declared_inputs_from_references(
            [{"name": "a", "path": "d1/x.md"}, {"name": "a", "path": "d2/x.md"}]
        )
    with pytest.raises(BoundInputError):
        declared_inputs_from_references([{"path": "x.md"}])
    with pytest.raises(BoundInputError):
        declared_inputs_from_references([{"name": "a", "path": "x.md", "required": 1}])
    with pytest.raises(BoundInputError):
        declared_inputs_from_references([{"name": "a", "path": "x.md", "repo_relative": "yes"}])
    with pytest.raises(BoundInputError):
        declared_inputs_from_references([{"name": "a/b", "path": "x.md"}])
    with pytest.raises(BoundInputError):
        declared_inputs_from_references([{"name": "a", "path": "../escape.md"}])
    with pytest.raises(BoundInputError):
        declared_inputs_from_references([{"name": "a", "path": "d1//x.md"}])
    # Names are never derived from repo_path: one basename, two bindings.
    pair = declared_inputs_from_references(
        [{"name": "a", "path": "d1/x.md"}, {"name": "b", "path": "d2/x.md"}]
    )
    assert [item.name for item in pair] == ["a", "b"]
    assert bound_input_target_path("a", "d1/x.md") == PurePosixPath("inputs/a/d1/x.md")
    assert bound_input_target_path("b", "d2/x.md") == PurePosixPath("inputs/b/d2/x.md")
    with pytest.raises(BoundInputError):
        bound_input_target_path("a/b", "x.md")
    with pytest.raises(BoundInputError):
        bound_input_target_path("a", "../x.md")


def test_verifier_predicate_is_first_component_only_and_total() -> None:
    assert BOUND_INPUTS_ROOT == "inputs"
    assert is_bound_input_path("inputs")
    assert is_bound_input_path("inputs/")
    assert is_bound_input_path("inputs/a/d1/x.md")
    assert is_bound_input_path(PurePosixPath("inputs/a"))
    assert not is_bound_input_path("src/inputs/data.py")
    assert not is_bound_input_path("docs/inputs/notes.md")
    assert not is_bound_input_path("pkg/inputs")
    assert not is_bound_input_path("inputsx/a")
    assert not is_bound_input_path("Inputs/a")
    assert not is_bound_input_path("/inputs/x")
    # Pure and total: malformed input is simply not under the root.
    assert not is_bound_input_path("")
    assert not is_bound_input_path(".")
    assert not is_bound_input_path(None)  # type: ignore[arg-type]
    assert not is_bound_input_path(123)  # type: ignore[arg-type]


# --- containment resolution without Git --------------------------------------


def test_verifier_resolve_within_repo_containment(tmp_path: Path) -> None:
    root = tmp_path / "plain_repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "spec.md").write_text("# specification\n", encoding="utf-8")
    resolved = resolve_within_repo(root, "docs/spec.md")
    assert resolved == (root / "docs" / "spec.md").resolve()
    assert resolved.is_relative_to(root.resolve())
    # An in-repo symlink is followed and stays acceptable.
    (root / "alias.md").symlink_to(root / "docs" / "spec.md")
    assert resolve_within_repo(root, "alias.md") == (root / "docs" / "spec.md").resolve()
    # An escaping symlink, absolute input, traversal, missing file, and a
    # directory target are all rejected fail-closed.
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    (root / "leak.md").symlink_to(outside)
    with pytest.raises(BoundInputError):
        resolve_within_repo(root, "leak.md")
    for bad in ("/etc/hosts", "../outside.md", "missing.md", "docs", "~/x.md", "C:/x.md", "./x.md"):
        with pytest.raises(BoundInputError):
            resolve_within_repo(root, bad)


# --- provenance capture against the real workspace repository ----------------


def test_verifier_capture_records_provenance(store: ContentAddressedArtifactStore) -> None:
    head = _workspace_head()
    manifest = capture_input_binding(
        repo_root=WORKSPACE_REPO,
        workflow_id="wf_verifier",
        declarations=(
            DeclaredInput(name="module", repo_path=MODULE_SOURCE),
            DeclaredInput(name="project", repo_path=PROJECT_SOURCE),
        ),
        artifacts=store,
    )
    assert manifest.workflow_id == "wf_verifier"
    assert manifest.source_commit == head
    assert [record.name for record in manifest.inputs] == ["module", "project"]
    module = manifest.record("module")
    module_bytes = (WORKSPACE_REPO / MODULE_SOURCE).read_bytes()
    assert module.content_sha256 == _plain_sha256(module_bytes)
    assert module.source_commit == head
    assert module.byte_size == len(module_bytes) == module.content_ref.byte_size
    assert module.content_ref.artifact_type == BOUND_INPUT_ARTIFACT
    # The plain content digest and the store's typed digest stay distinct.
    assert module.content_ref.sha256 != module.content_sha256
    with pytest.raises(BoundInputError):
        manifest.record("unknown")


def test_verifier_capture_optional_and_required_missing(
    store: ContentAddressedArtifactStore,
) -> None:
    manifest = capture_input_binding(
        repo_root=WORKSPACE_REPO,
        workflow_id="wf_optional",
        declarations=(DeclaredInput(name="opt", repo_path="absent.md", required=False),),
        artifacts=store,
    )
    assert manifest.inputs == ()
    with pytest.raises(BoundInputError, match="required"):
        capture_input_binding(
            repo_root=WORKSPACE_REPO,
            workflow_id="wf_required",
            declarations=(DeclaredInput(name="req", repo_path="absent.md"),),
            artifacts=store,
        )
    with pytest.raises(BoundInputError):
        capture_input_binding(
            repo_root=WORKSPACE_REPO,
            workflow_id="   ",
            declarations=(),
            artifacts=store,
        )
    # Duplicate declaration names fail closed at manifest construction.
    with pytest.raises(BoundInputError):
        capture_input_binding(
            repo_root=WORKSPACE_REPO,
            workflow_id="wf_duplicate",
            declarations=(
                DeclaredInput(name="a", repo_path=MODULE_SOURCE),
                DeclaredInput(name="a", repo_path=PROJECT_SOURCE),
            ),
            artifacts=store,
        )


def test_verifier_capture_requires_resolvable_head(
    tmp_path: Path, store: ContentAddressedArtifactStore
) -> None:
    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    (plain / "x.md").write_text("x\n", encoding="utf-8")
    with pytest.raises(BoundInputError, match="HEAD"):
        capture_input_binding(
            repo_root=plain,
            workflow_id="wf_plain",
            declarations=(DeclaredInput(name="x", repo_path="x.md"),),
            artifacts=store,
        )


def test_verifier_manifest_payload_round_trip_and_rejections(
    store: ContentAddressedArtifactStore,
) -> None:
    manifest = capture_input_binding(
        repo_root=WORKSPACE_REPO,
        workflow_id="wf_round_trip",
        declarations=(DeclaredInput(name="project", repo_path=PROJECT_SOURCE),),
        artifacts=store,
    )
    payload = manifest.to_payload()
    rebuilt = InputBindingManifest.from_payload(payload)
    assert rebuilt == manifest
    assert rebuilt.to_payload() == payload
    for bad in (
        None,
        "not-a-mapping",
        {"schema_version": "2", "workflow_id": "wf", "source_commit": "0" * 64, "inputs": []},
        {"workflow_id": "wf", "source_commit": "0" * 64, "inputs": []},
        {"schema_version": "1", "workflow_id": "", "source_commit": "0" * 64, "inputs": []},
        {"schema_version": "1", "workflow_id": "wf", "source_commit": "zz", "inputs": []},
        {"schema_version": "1", "workflow_id": "wf", "source_commit": "0" * 64},
        {"schema_version": "1", "workflow_id": "wf", "source_commit": "0" * 64, "inputs": {}},
        {
            "schema_version": "1",
            "workflow_id": "wf",
            "source_commit": "0" * 64,
            "inputs": [{"name": "a", "repo_path": "x.md"}],
        },
    ):
        with pytest.raises(BoundInputError):
            InputBindingManifest.from_payload(bad)


# --- materialization, verification, and fail-closed content gaps -------------


def test_verifier_materialize_verify_and_fail_closed_gaps(
    store: ContentAddressedArtifactStore, tmp_path: Path
) -> None:
    manifest = capture_input_binding(
        repo_root=WORKSPACE_REPO,
        workflow_id="wf_materialize",
        declarations=(
            DeclaredInput(name="project", repo_path=PROJECT_SOURCE),
            DeclaredInput(name="module", repo_path=MODULE_SOURCE, required=False),
        ),
        artifacts=store,
    )
    workspace = tmp_path / "role_workspace"
    materialized = materialize_bound_inputs(
        manifest=manifest, artifacts=store, destination_root=workspace
    )
    assert set(materialized) == {"project", "module"}
    assert materialized["project"] == workspace / "inputs" / "project" / PROJECT_SOURCE
    assert materialized["project"].is_absolute()
    assert materialized["project"].read_bytes() == (WORKSPACE_REPO / PROJECT_SOURCE).read_bytes()
    verify_bound_inputs(manifest=manifest, destination_root=workspace)
    # Re-materialization of the same manifest is byte-identical and idempotent.
    again = materialize_bound_inputs(
        manifest=manifest, artifacts=store, destination_root=workspace
    )
    assert again["project"].read_bytes() == materialized["project"].read_bytes()
    verify_bound_inputs(manifest=manifest, destination_root=workspace)
    # Drift is detected and names the drifted input.
    materialized["project"].write_text("tampered\n", encoding="utf-8")
    with pytest.raises(BoundInputError, match="project"):
        verify_bound_inputs(manifest=manifest, destination_root=workspace)
    # Loss is detected as a missing input.
    materialized["project"].unlink()
    with pytest.raises(BoundInputError, match="missing"):
        verify_bound_inputs(manifest=manifest, destination_root=workspace)
    # Unavailable durable content fails closed before any partial tree counts.
    record = manifest.record("module")
    phantom = BoundInputRecord(
        name=record.name,
        repo_path=record.repo_path,
        source_commit=record.source_commit,
        content_sha256=record.content_sha256,
        byte_size=record.byte_size,
        content_ref=ArtifactRef(
            sha256="ab" * 32,
            artifact_type=BOUND_INPUT_ARTIFACT,
            schema_version=record.content_ref.schema_version,
            media_type=record.content_ref.media_type,
            byte_size=record.byte_size,
        ),
    )
    phantom_manifest = InputBindingManifest(
        schema_version=manifest.schema_version,
        workflow_id=manifest.workflow_id,
        source_commit=manifest.source_commit,
        inputs=(phantom,),
    )
    with pytest.raises(BoundInputError, match="unavailable"):
        materialize_bound_inputs(
            manifest=phantom_manifest,
            artifacts=store,
            destination_root=tmp_path / "phantom_workspace",
        )


def test_verifier_rejects_symlinked_materialized_file(
    store: ContentAddressedArtifactStore, tmp_path: Path
) -> None:
    manifest = capture_input_binding(
        repo_root=WORKSPACE_REPO,
        workflow_id="wf_target_symlink",
        declarations=(DeclaredInput(name="project", repo_path=PROJECT_SOURCE),),
        artifacts=store,
    )
    workspace = tmp_path / "role_workspace"
    materialized = materialize_bound_inputs(
        manifest=manifest,
        artifacts=store,
        destination_root=workspace,
    )
    target = materialized["project"]
    outside = tmp_path / "outside.txt"
    outside.write_bytes((WORKSPACE_REPO / PROJECT_SOURCE).read_bytes())
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(BoundInputError):
        verify_bound_inputs(manifest=manifest, destination_root=workspace)
    with pytest.raises(BoundInputError, match="symlink"):
        materialize_bound_inputs(
            manifest=manifest,
            artifacts=store,
            destination_root=workspace,
        )


def test_verifier_verify_repo_bound_inputs_in_place(
    store: ContentAddressedArtifactStore, tmp_path: Path
) -> None:
    manifest = capture_input_binding(
        repo_root=WORKSPACE_REPO,
        workflow_id="wf_inrepo",
        declarations=(DeclaredInput(name="project", repo_path=PROJECT_SOURCE),),
        artifacts=store,
    )
    source_bytes = (WORKSPACE_REPO / PROJECT_SOURCE).read_bytes()
    mirror = tmp_path / "repo_workspace"
    mirror.mkdir()
    (mirror / PROJECT_SOURCE).write_bytes(source_bytes)
    verify_repo_bound_inputs(manifest=manifest, repo_root=mirror)
    (mirror / PROJECT_SOURCE).write_text("drifted\n", encoding="utf-8")
    with pytest.raises(BoundInputError, match="drifted"):
        verify_repo_bound_inputs(manifest=manifest, repo_root=mirror)
    # Containment applies before hashing: an escaping symlink is rejected even
    # when its bytes would match the recorded digest.
    (mirror / PROJECT_SOURCE).unlink()
    outside = tmp_path / "outside.toml"
    outside.write_bytes(source_bytes)
    (mirror / PROJECT_SOURCE).symlink_to(outside)
    with pytest.raises(BoundInputError):
        verify_repo_bound_inputs(manifest=manifest, repo_root=mirror)


def test_verifier_reference_entries_projection_and_ambiguity(
    store: ContentAddressedArtifactStore, tmp_path: Path
) -> None:
    manifest = capture_input_binding(
        repo_root=WORKSPACE_REPO,
        workflow_id="wf_entries",
        declarations=(
            DeclaredInput(name="project", repo_path=PROJECT_SOURCE),
            DeclaredInput(name="module", repo_path=MODULE_SOURCE, required=False),
        ),
        artifacts=store,
    )
    workspace_root = tmp_path / "role_workspace"
    entries = bound_input_reference_entries(manifest=manifest, workspace_root=workspace_root)
    assert [entry["name"] for entry in entries] == ["module", "project"]
    assert entries[1] == {
        "name": "project",
        "path": str(workspace_root / "inputs" / "project" / PROJECT_SOURCE),
        "include": [PROJECT_SOURCE],
        "mode": "read_only",
        "truth_source": True,
        "required": True,
        "bound_input": True,
    }
    assert entries[0]["required"] is False
    repo_entries = bound_input_reference_entries(manifest=manifest, repo_root=WORKSPACE_REPO)
    assert repo_entries[1]["path"] == str((WORKSPACE_REPO / PROJECT_SOURCE).resolve())
    assert repo_entries[1]["bound_input"] is True
    with pytest.raises(BoundInputError):
        bound_input_reference_entries(manifest=manifest)
    with pytest.raises(BoundInputError):
        bound_input_reference_entries(
            manifest=manifest, workspace_root=workspace_root, repo_root=WORKSPACE_REPO
        )
