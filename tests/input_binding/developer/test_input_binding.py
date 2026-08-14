"""Focused developer tests for ``pal.bunshin.v2.input_binding``.

Covers the owned contract directly: participation and derivation of
declared inputs, containment resolution, provenance capture against a real
Git repository, manifest payload round-trip, deterministic materialization,
hash verification, and reference-entry projection.
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


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    return completed.stdout.decode().strip()


def _init_repo(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "binding@example.com")
    _git(root, "config", "user.name", "Binding Test")
    (root / "docs").mkdir()
    (root / "docs" / "spec.md").write_text("# specification\n", encoding="utf-8")
    (root / "notes.txt").write_text("keeper notes\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "fixture")
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "source_repo"
    _init_repo(root)
    return root


@pytest.fixture()
def store(tmp_path: Path) -> ContentAddressedArtifactStore:
    return ContentAddressedArtifactStore(
        tmp_path / "runtime", RecordingMetadataRepository()
    )


def _plain_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- containment predicate and deterministic layout -----------------------


def test_is_bound_input_path_matches_only_first_component() -> None:
    assert is_bound_input_path("inputs")
    assert is_bound_input_path("inputs/a/d1/x.md")
    assert is_bound_input_path(PurePosixPath("inputs/a"))
    assert is_bound_input_path("inputs/")
    assert not is_bound_input_path("src/inputs/data.py")
    assert not is_bound_input_path("docs/inputs/notes.md")
    assert not is_bound_input_path("pkg/inputs")
    assert not is_bound_input_path("inputsx/a")
    assert not is_bound_input_path("Inputs/a")
    assert not is_bound_input_path("")
    assert not is_bound_input_path(".")
    assert not is_bound_input_path("/inputs/x")
    assert not is_bound_input_path(123)  # type: ignore[arg-type]


def test_bound_input_target_path_is_inputs_name_then_repo_path() -> None:
    assert bound_input_target_path("a", "d1/x.md") == PurePosixPath("inputs/a/d1/x.md")
    assert str(bound_input_target_path("b", "notes.txt")) == "inputs/b/notes.txt"
    assert bound_input_target_path("a", "d1/x.md").parts[0] == BOUND_INPUTS_ROOT
    with pytest.raises(BoundInputError):
        bound_input_target_path("a/b", "x.md")
    with pytest.raises(BoundInputError):
        bound_input_target_path("a", "../x.md")


# --- declaration extraction -------------------------------------------------


def test_declared_inputs_participation_and_derivation() -> None:
    references = [
        {"name": "spec", "path": "docs/spec.md"},
        {"name": "host", "path": "/etc/hosts"},
        {"name": "win", "path": "C:/Hosts"},
        {"name": "marked", "path": "notes.txt", "repo_relative": True},
        {"name": "optional", "path": "docs/optional.md", "required": False},
        {"path": "/abs/ignored", "repo_relative": False},
        "not-a-mapping",
    ]
    declared = declared_inputs_from_references(references)
    assert declared == (
        DeclaredInput(name="marked", repo_path="notes.txt", required=True),
        DeclaredInput(name="optional", repo_path="docs/optional.md", required=False),
        DeclaredInput(name="spec", repo_path="docs/spec.md", required=True),
    )
    # Names are never derived from repo_path: same basename, distinct names.
    pair = declared_inputs_from_references(
        [
            {"name": "a", "path": "d1/x.md"},
            {"name": "b", "path": "d2/x.md"},
        ]
    )
    assert [item.name for item in pair] == ["a", "b"]
    assert bound_input_target_path("a", "d1/x.md") == PurePosixPath("inputs/a/d1/x.md")
    assert bound_input_target_path("b", "d2/x.md") == PurePosixPath("inputs/b/d2/x.md")


@pytest.mark.parametrize(
    "references",
    [
        [{"name": "a", "path": "d1/x.md"}, {"name": "a", "path": "d2/x.md"}],
        [{"name": "a/b", "path": "x.md"}],
        [{"name": "", "path": "x.md"}],
        [{"name": ".", "path": "x.md"}],
        [{"name": "..", "path": "x.md"}],
        [{"path": "x.md"}],
        [{"name": "a", "path": "/etc/hosts", "repo_relative": True}],
        [{"name": "a", "path": "../escape.md"}],
        [{"name": "a", "path": "./x.md"}],
        [{"name": "a", "path": "d1//x.md"}],
        [{"name": "a", "path": "d1/x.md/"}],
        [{"name": "a", "path": "~/x.md"}],
        [{"name": "a", "path": "C:x.md"}],
        [{"name": "a", "path": "C:/x.md", "repo_relative": True}],
        [{"name": "a", "path": "x.md", "required": "yes"}],
        [{"name": "a", "path": "x.md", "repo_relative": "yes"}],
    ],
)
def test_declared_inputs_rejects_invalid_participating_entries(
    references: list[Any],
) -> None:
    with pytest.raises(BoundInputError):
        declared_inputs_from_references(references)


def test_declared_input_constructor_validates() -> None:
    with pytest.raises(BoundInputError):
        DeclaredInput(name="a b", repo_path="x.md")
    with pytest.raises(BoundInputError):
        DeclaredInput(name="a", repo_path="/x.md")
    with pytest.raises(BoundInputError):
        DeclaredInput(name="a", repo_path="x.md", required=1)  # type: ignore[arg-type]


def test_bound_input_record_accepts_sha1_and_sha256_source_commits() -> None:
    ref = ArtifactRef(
        sha256="cd" * 32,
        artifact_type=BOUND_INPUT_ARTIFACT,
        schema_version="1",
        media_type="application/octet-stream",
        byte_size=3,
    )
    for commit in ("ab" * 20, "ab" * 32):
        record = BoundInputRecord(
            name="a",
            repo_path="x.md",
            source_commit=commit,
            content_sha256="ef" * 32,
            byte_size=3,
            content_ref=ref,
        )
        assert record.source_commit == commit
    with pytest.raises(BoundInputError):
        BoundInputRecord(
            name="a",
            repo_path="x.md",
            source_commit="ab" * 19,
            content_sha256="ef" * 32,
            byte_size=3,
            content_ref=ref,
        )


# --- containment resolution -------------------------------------------------


def test_resolve_within_repo_accepts_in_repo_file(repo: Path) -> None:
    resolved = resolve_within_repo(repo, "docs/spec.md")
    assert resolved == (repo / "docs" / "spec.md").resolve()
    assert resolved.is_relative_to(repo.resolve())


def test_resolve_within_repo_follows_in_repo_symlink(repo: Path) -> None:
    (repo / "alias.md").symlink_to(repo / "docs" / "spec.md")
    resolved = resolve_within_repo(repo, "alias.md")
    assert resolved == (repo / "docs" / "spec.md").resolve()


def test_resolve_within_repo_rejects_escaping_symlink(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    (repo / "leak.md").symlink_to(outside)
    with pytest.raises(BoundInputError):
        resolve_within_repo(repo, "leak.md")


@pytest.mark.parametrize(
    "repo_path",
    ["/etc/hosts", "../outside.md", "missing.md", "docs", "~/x.md", "C:/x.md"],
)
def test_resolve_within_repo_rejects_invalid_paths(repo: Path, repo_path: str) -> None:
    with pytest.raises(BoundInputError):
        resolve_within_repo(repo, repo_path)


# --- capture and manifest ---------------------------------------------------


def test_capture_records_provenance(repo: Path, store: ContentAddressedArtifactStore) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    manifest = capture_input_binding(
        repo_root=repo,
        workflow_id="wf_binding",
        declarations=(
            DeclaredInput(name="spec", repo_path="docs/spec.md"),
            DeclaredInput(name="notes", repo_path="notes.txt"),
        ),
        artifacts=store,
    )
    assert manifest.workflow_id == "wf_binding"
    assert manifest.source_commit == head
    assert [record.name for record in manifest.inputs] == ["notes", "spec"]
    spec = manifest.record("spec")
    spec_bytes = (repo / "docs" / "spec.md").read_bytes()
    assert spec.content_sha256 == _plain_sha256(spec_bytes)
    assert spec.source_commit == head
    assert spec.byte_size == len(spec_bytes) == spec.content_ref.byte_size
    assert spec.content_ref.artifact_type == BOUND_INPUT_ARTIFACT
    # The plain content digest and the store's typed digest are distinct.
    assert spec.content_ref.sha256 != spec.content_sha256
    with pytest.raises(BoundInputError):
        manifest.record("unknown")


def test_capture_omits_missing_optional_but_fails_missing_required(
    repo: Path, store: ContentAddressedArtifactStore
) -> None:
    manifest = capture_input_binding(
        repo_root=repo,
        workflow_id="wf_optional",
        declarations=(DeclaredInput(name="opt", repo_path="absent.md", required=False),),
        artifacts=store,
    )
    assert manifest.inputs == ()
    with pytest.raises(BoundInputError, match="required"):
        capture_input_binding(
            repo_root=repo,
            workflow_id="wf_required",
            declarations=(DeclaredInput(name="req", repo_path="absent.md"),),
            artifacts=store,
        )


def test_capture_requires_resolvable_head(tmp_path: Path, store: ContentAddressedArtifactStore) -> None:
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


def test_manifest_payload_round_trips_losslessly(
    repo: Path, store: ContentAddressedArtifactStore
) -> None:
    manifest = capture_input_binding(
        repo_root=repo,
        workflow_id="wf_round_trip",
        declarations=(DeclaredInput(name="spec", repo_path="docs/spec.md"),),
        artifacts=store,
    )
    payload = manifest.to_payload()
    rebuilt = InputBindingManifest.from_payload(payload)
    assert rebuilt == manifest
    assert rebuilt.to_payload() == payload


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"schema_version": "2", "workflow_id": "wf", "source_commit": "0" * 64, "inputs": []},
        {"workflow_id": "wf", "source_commit": "0" * 64, "inputs": []},
        {"schema_version": "1", "workflow_id": "", "source_commit": "0" * 64, "inputs": []},
        {"schema_version": "1", "workflow_id": "wf", "source_commit": "zz", "inputs": []},
        {"schema_version": "1", "workflow_id": "wf", "source_commit": "0" * 64},
    ],
)
def test_manifest_from_payload_rejects_non_conforming(payload: Any) -> None:
    with pytest.raises(BoundInputError):
        InputBindingManifest.from_payload(payload)


def test_manifest_rejects_duplicate_and_foreign_commit_records(
    repo: Path, store: ContentAddressedArtifactStore
) -> None:
    manifest = capture_input_binding(
        repo_root=repo,
        workflow_id="wf_dup",
        declarations=(DeclaredInput(name="spec", repo_path="docs/spec.md"),),
        artifacts=store,
    )
    record = manifest.record("spec")
    with pytest.raises(BoundInputError):
        InputBindingManifest(
            schema_version="1",
            workflow_id="wf_dup",
            source_commit=manifest.source_commit,
            inputs=(record, record),
        )
    drifted = BoundInputRecord(
        name="spec",
        repo_path=record.repo_path,
        source_commit="0" * 64,
        content_sha256=record.content_sha256,
        byte_size=record.byte_size,
        content_ref=record.content_ref,
    )
    with pytest.raises(BoundInputError):
        InputBindingManifest(
            schema_version="1",
            workflow_id="wf_dup",
            source_commit=manifest.source_commit,
            inputs=(drifted,),
        )


# --- materialization and verification ---------------------------------------


def test_materialize_then_verify_is_deterministic_and_idempotent(
    repo: Path, store: ContentAddressedArtifactStore, tmp_path: Path
) -> None:
    manifest = capture_input_binding(
        repo_root=repo,
        workflow_id="wf_materialize",
        declarations=(
            DeclaredInput(name="spec", repo_path="docs/spec.md"),
            DeclaredInput(name="notes", repo_path="notes.txt", required=False),
        ),
        artifacts=store,
    )
    workspace = tmp_path / "role_workspace"
    materialized = materialize_bound_inputs(
        manifest=manifest, artifacts=store, destination_root=workspace
    )
    assert set(materialized) == {"spec", "notes"}
    assert materialized["spec"] == workspace / "inputs" / "spec" / "docs" / "spec.md"
    assert materialized["spec"].is_absolute()
    assert materialized["spec"].read_bytes() == (repo / "docs" / "spec.md").read_bytes()
    verify_bound_inputs(manifest=manifest, destination_root=workspace)
    # A second materialization of the same manifest is byte-identical.
    again = materialize_bound_inputs(
        manifest=manifest, artifacts=store, destination_root=workspace
    )
    assert again["spec"].read_bytes() == materialized["spec"].read_bytes()
    verify_bound_inputs(manifest=manifest, destination_root=workspace)


def test_verify_bound_inputs_detects_drift_and_loss(
    repo: Path, store: ContentAddressedArtifactStore, tmp_path: Path
) -> None:
    manifest = capture_input_binding(
        repo_root=repo,
        workflow_id="wf_drift",
        declarations=(DeclaredInput(name="spec", repo_path="docs/spec.md"),),
        artifacts=store,
    )
    workspace = tmp_path / "role_workspace"
    materialized = materialize_bound_inputs(
        manifest=manifest, artifacts=store, destination_root=workspace
    )
    materialized["spec"].write_text("tampered\n", encoding="utf-8")
    with pytest.raises(BoundInputError, match="spec"):
        verify_bound_inputs(manifest=manifest, destination_root=workspace)
    materialized["spec"].unlink()
    with pytest.raises(BoundInputError, match="missing"):
        verify_bound_inputs(manifest=manifest, destination_root=workspace)


def test_materialize_rejects_symlinked_destination_parent(
    repo: Path, store: ContentAddressedArtifactStore, tmp_path: Path
) -> None:
    manifest = capture_input_binding(
        repo_root=repo,
        workflow_id="wf_destination_escape",
        declarations=(DeclaredInput(name="spec", repo_path="docs/spec.md"),),
        artifacts=store,
    )
    workspace = tmp_path / "role_workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "inputs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(BoundInputError, match="symlink"):
        materialize_bound_inputs(
            manifest=manifest,
            artifacts=store,
            destination_root=workspace,
        )
    assert not (outside / "spec" / "docs" / "spec.md").exists()


def test_materialize_fails_closed_on_unavailable_durable_content(
    repo: Path, store: ContentAddressedArtifactStore, tmp_path: Path
) -> None:
    manifest = capture_input_binding(
        repo_root=repo,
        workflow_id="wf_unavailable",
        declarations=(DeclaredInput(name="spec", repo_path="docs/spec.md"),),
        artifacts=store,
    )
    record = manifest.record("spec")
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
            destination_root=tmp_path / "workspace",
        )


def test_verify_repo_bound_inputs_hashes_in_place(
    repo: Path, store: ContentAddressedArtifactStore
) -> None:
    manifest = capture_input_binding(
        repo_root=repo,
        workflow_id="wf_inrepo",
        declarations=(DeclaredInput(name="spec", repo_path="docs/spec.md"),),
        artifacts=store,
    )
    verify_repo_bound_inputs(manifest=manifest, repo_root=repo)
    (repo / "docs" / "spec.md").write_text("drifted\n", encoding="utf-8")
    with pytest.raises(BoundInputError, match="drifted"):
        verify_repo_bound_inputs(manifest=manifest, repo_root=repo)


# --- reference-entry projection ----------------------------------------------


def test_bound_input_reference_entries_project_both_workspace_kinds(
    repo: Path, store: ContentAddressedArtifactStore, tmp_path: Path
) -> None:
    manifest = capture_input_binding(
        repo_root=repo,
        workflow_id="wf_entries",
        declarations=(
            DeclaredInput(name="spec", repo_path="docs/spec.md"),
            DeclaredInput(name="notes", repo_path="notes.txt", required=False),
        ),
        artifacts=store,
    )
    workspace_root = tmp_path / "role_workspace"
    entries = bound_input_reference_entries(manifest=manifest, workspace_root=workspace_root)
    assert [entry["name"] for entry in entries] == ["notes", "spec"]
    assert entries[1] == {
        "name": "spec",
        "path": str(workspace_root / "inputs" / "spec" / "docs" / "spec.md"),
        "include": ["docs/spec.md"],
        "mode": "read_only",
        "truth_source": True,
        "required": True,
        "bound_input": True,
    }
    assert entries[0]["required"] is False
    repo_entries = bound_input_reference_entries(manifest=manifest, repo_root=repo)
    assert repo_entries[1]["path"] == str((repo / "docs" / "spec.md").resolve())
    assert repo_entries[1]["bound_input"] is True
    with pytest.raises(BoundInputError):
        bound_input_reference_entries(manifest=manifest)
    with pytest.raises(BoundInputError):
        bound_input_reference_entries(
            manifest=manifest, workspace_root=workspace_root, repo_root=repo
        )
