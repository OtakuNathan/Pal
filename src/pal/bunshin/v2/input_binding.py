"""Declared repo-relative input binding for Bunshin role workspaces.

This module owns the complete semantic contract for binding explicitly
declared repository-relative read-only inputs into role workspaces.  It
closes the input-binding gap recorded in
``docs/evidence/2026-08-14-public-proof``: workflow-declared repo-relative
source documents never reached artifact role workspaces, so workers could
only report blockers.

Module contract
---------------
* **Explicit declaration only.**  Only references explicitly declared as
  repo-relative inputs (a relative ``path`` on a task-revision or
  ``start_workflow`` reference entry, optionally marked
  ``repo_relative: true``) may be bound.  Nothing implicit, pattern-derived,
  or host-wide is ever bound, and the repository root itself is never bound
  into a workspace whose contract does not include the repository.
* **Fail closed.**  Every validation, capture, materialization, and
  verification failure raises :class:`BoundInputError` (a ``ValueError``)
  before worker dispatch.  Missing or invalid declared inputs are never
  silently skipped.
* **Verifiable provenance.**  A binding is captured once per workflow as an
  immutable :class:`InputBindingManifest` recording the source repository
  HEAD commit and the plain SHA-256 content digest of every declared input,
  with the content itself stored durably in the content-addressed artifact
  store.  The manifest's ``content_sha256`` is the plain digest of the file
  bytes; the accompanying ``content_ref.sha256`` is the artifact store's
  typed digest that addresses the stored blob.  The two are different
  values by design and never compared to each other.
* **Deterministic access.**  Inside a role workspace whose contract does not
  include the repository, every bound input is materialized at
  ``<workspace_root>/inputs/<name>/<repo_path>``.  Inside a workspace that
  already contains the repository, the input stays at its repository-relative
  path and is verified against the manifest hashes.
* **Attempt stability.**  Materialization reads the durable recorded
  content, never the mutable host repository, so retries, process restarts,
  and attempt fencing re-materialize byte-identical inputs and re-verify the
  same hashes.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any
from uuid import uuid4

from pal.bunshin.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore

# Durable record schema version for input-binding manifests.
INPUT_BINDING_SCHEMA_VERSION = "1"

# Artifact type of the durable per-workflow input-binding manifest.
INPUT_BINDING_MANIFEST_ARTIFACT = "InputBindingManifestArtifact"

# Artifact type of one durably stored bound-input content blob.
BOUND_INPUT_ARTIFACT = "BoundInputArtifact"

# Root directory, relative to a role workspace, that owns all
# Manager-materialized bound inputs.  Candidate snapshots, tree
# fingerprints, and deliverables must exclude this root; it is written only
# by the Manager-side materializer.
#
# Containment is defined strictly as a first-path-component match: a file is
# under this root iff the first component of its workspace-relative POSIX
# path is exactly ``inputs``.  A path that merely contains an ``inputs``
# component elsewhere (for example ``src/inputs/data.py`` or
# ``docs/inputs/notes.md``) is NOT under this root and is never excluded
# from candidates, fingerprints, or deliverables.  See
# :func:`is_bound_input_path`, the single authoritative predicate.
BOUND_INPUTS_ROOT = "inputs"

# Full lowercase hexadecimal SHA-256 digest.
_HEX_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")

# Full lowercase hexadecimal Git object id: SHA-1 (40) or SHA-256 (64).
_HEX_COMMIT = re.compile(r"\A[0-9a-f]{40}\Z|\A[0-9a-f]{64}\Z")

# Characters permitted in a declared input name: it must be safe as a
# single workspace path component.
_SAFE_NAME = re.compile(r"\A[A-Za-z0-9._-]+\Z")

# Windows-style drive prefix such as ``C:`` or ``d:``.
_DRIVE_PREFIX = re.compile(r"\A[A-Za-z]:")

# Upper bound for the read-only ``git rev-parse HEAD`` provenance probe.
_GIT_HEAD_TIMEOUT_SECONDS = 30


class BoundInputError(ValueError):
    """Raised when a declared repo-relative input fails any binding rule.

    Observable at workflow intake (before workflow creation), at role
    workspace preparation (before worker process spawn), and at bound-input
    verification.  Carries a human-readable, operator-actionable message
    naming the offending input and the violated rule.  Raising is always
    fail-closed: no partial binding is retained.
    """


def _require_str(value: Any, *, what: str) -> str:
    if not isinstance(value, str):
        raise BoundInputError(f"{what} must be a string, got {type(value).__name__}")
    return value


def _validated_name(name: Any) -> str:
    text = _require_str(name, what="declared input name")
    if not text:
        raise BoundInputError("declared input name must not be empty")
    if text in {".", ".."}:
        raise BoundInputError(f"declared input name {text!r} is not a safe path component")
    if not _SAFE_NAME.match(text):
        raise BoundInputError(
            f"declared input name {text!r} may contain only ASCII letters, digits, '_', '-', and '.'"
        )
    return text


def _is_absolute_path_text(text: str) -> bool:
    if text.startswith("/") or text.startswith("\\\\"):
        return True
    return PureWindowsPath(text).is_absolute() or PurePosixPath(text).is_absolute()


def _validated_repo_path(repo_path: Any, *, name: str | None = None) -> str:
    subject = f"declared input {name!r} " if name is not None else "declared input "
    text = _require_str(repo_path, what=f"{subject}repo_path")
    if not text:
        raise BoundInputError(f"{subject}repo_path must not be empty")
    if text.startswith("~"):
        raise BoundInputError(f"{subject}repo_path {text!r} must not be home-relative")
    if _is_absolute_path_text(text):
        raise BoundInputError(
            f"{subject}repo_path {text!r} must be relative to the repository root"
        )
    parts = PurePosixPath(text).parts
    if not parts:
        raise BoundInputError(f"{subject}repo_path {text!r} is not a normalized relative path")
    if any(part in {"..", "."} for part in parts):
        raise BoundInputError(
            f"{subject}repo_path {text!r} must not contain '.' or '..' segments"
        )
    if _DRIVE_PREFIX.match(parts[0]):
        raise BoundInputError(f"{subject}repo_path {text!r} must not carry a drive component")
    if PurePosixPath(text).as_posix() != text:
        raise BoundInputError(f"{subject}repo_path {text!r} is not in normalized POSIX form")
    return text


def _validated_hex_digest(value: Any, *, what: str) -> str:
    text = _require_str(value, what=what)
    if not _HEX_SHA256.match(text):
        raise BoundInputError(f"{what} must be a full lowercase hexadecimal SHA-256 digest")
    return text


def _validated_commit_id(value: Any, *, what: str) -> str:
    text = _require_str(value, what=what)
    if not _HEX_COMMIT.match(text):
        raise BoundInputError(
            f"{what} must be a full lowercase hexadecimal Git object id (SHA-1 or SHA-256)"
        )
    return text


def _absolute_without_symlinks(path: Path) -> Path:
    return path if path.is_absolute() else path.absolute()


@dataclass(frozen=True)
class DeclaredInput:
    """One explicitly declared repo-relative read-only input.

    Invariants (enforced at construction by the implementation):

    * ``name`` is non-empty, unique within one declaration set, and contains
      only ASCII letters, digits, ``_``, ``-``, and ``.`` so it is safe as a
      single workspace path component.
    * ``repo_path`` is a normalized POSIX-style relative path: non-empty,
      not absolute, with no ``..`` or ``.`` segments, no drive component,
      and no leading ``~``.  It addresses exactly one regular file inside
      the source repository.
    * ``required`` defaults to true; an optional input that is missing at
      the source is omitted from the binding instead of failing capture.
    """

    name: str
    repo_path: str
    required: bool = True

    def __post_init__(self) -> None:
        name = _validated_name(self.name)
        _validated_repo_path(self.repo_path, name=name)
        if not isinstance(self.required, bool):
            raise BoundInputError(
                f"declared input {name!r} required flag must be a boolean, "
                f"got {type(self.required).__name__}"
            )


@dataclass(frozen=True)
class BoundInputRecord:
    """The durable provenance record for one bound input.

    Invariants (enforced at construction by the implementation):

    * ``name`` and ``repo_path`` mirror the source :class:`DeclaredInput`.
    * ``source_commit`` is the full hexadecimal SHA of the source
      repository HEAD at capture time.
    * ``content_sha256`` is the plain SHA-256 hex digest of the captured
      file bytes.  This is the provenance hash that materialization and
      verification recompute; it is NOT ``content_ref.sha256``.
    * ``content_ref`` addresses the durable ``BoundInputArtifact`` that
      stores the captured bytes in the content-addressed artifact store.
      Its ``sha256`` is the store's typed digest
      ``sha256(artifact_type \\0 schema_version \\0 media_type \\0 data)``
      produced by ``ContentAddressedArtifactStore.put_bytes``; it addresses
      and integrity-checks the stored blob and is never compared with
      ``content_sha256``.
    * ``byte_size`` is the captured file size in bytes and equals
      ``content_ref.byte_size`` (``put_bytes`` sets it to the data length).
    """

    name: str
    repo_path: str
    source_commit: str
    content_sha256: str
    byte_size: int
    content_ref: ArtifactRef
    required: bool = True

    def __post_init__(self) -> None:
        name = _validated_name(self.name)
        _validated_repo_path(self.repo_path, name=name)
        _validated_commit_id(self.source_commit, what=f"input {name!r} source_commit")
        _validated_hex_digest(self.content_sha256, what=f"input {name!r} content_sha256")
        if (
            not isinstance(self.byte_size, int)
            or isinstance(self.byte_size, bool)
            or self.byte_size < 0
        ):
            raise BoundInputError(f"input {name!r} byte_size must be a non-negative integer")
        if not isinstance(self.content_ref, ArtifactRef):
            raise BoundInputError(f"input {name!r} content_ref must be an ArtifactRef")
        _validated_hex_digest(self.content_ref.sha256, what=f"input {name!r} content_ref.sha256")
        if self.content_ref.artifact_type != BOUND_INPUT_ARTIFACT:
            raise BoundInputError(
                f"input {name!r} content_ref must address a {BOUND_INPUT_ARTIFACT}, "
                f"got {self.content_ref.artifact_type!r}"
            )
        if self.byte_size != self.content_ref.byte_size:
            raise BoundInputError(
                f"input {name!r} byte_size {self.byte_size} does not equal "
                f"content_ref.byte_size {self.content_ref.byte_size}"
            )
        if not isinstance(self.required, bool):
            raise BoundInputError(
                f"input {name!r} required flag must be a boolean, "
                f"got {type(self.required).__name__}"
            )


@dataclass(frozen=True)
class InputBindingManifest:
    """The immutable per-workflow truth of every bound input.

    One manifest is captured at workflow intake and never mutated.  All
    later attempts, retries, and recovered processes re-materialize and
    re-verify from the same manifest, which is what makes binding identical
    across retries, process restarts, and attempt fencing.

    Invariants (enforced at construction by the implementation):

    * ``schema_version`` equals :data:`INPUT_BINDING_SCHEMA_VERSION`.
    * ``workflow_id`` is the owning workflow identifier.
    * ``source_commit`` is the source repository HEAD at capture time and is
      identical for every record in ``inputs``.
    * ``inputs`` is non-empty when at least one required input was declared,
      ordered by ``name``, with unique names and unique ``repo_path``
      values.
    """

    schema_version: str
    workflow_id: str
    source_commit: str
    inputs: tuple[BoundInputRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.schema_version, str) or (
            self.schema_version != INPUT_BINDING_SCHEMA_VERSION
        ):
            raise BoundInputError(
                f"manifest schema_version must be {INPUT_BINDING_SCHEMA_VERSION!r}"
            )
        if not isinstance(self.workflow_id, str) or not self.workflow_id.strip():
            raise BoundInputError("manifest workflow_id must be a non-empty string")
        _validated_commit_id(self.source_commit, what="manifest source_commit")
        if not isinstance(self.inputs, (list, tuple)):
            raise BoundInputError("manifest inputs must be a sequence of bound-input records")
        records = tuple(self.inputs)
        for record in records:
            if not isinstance(record, BoundInputRecord):
                raise BoundInputError("manifest inputs must contain only BoundInputRecord values")
            if record.source_commit != self.source_commit:
                raise BoundInputError(
                    f"input {record.name!r} source_commit does not match the manifest source_commit"
                )
        names = [record.name for record in records]
        if len(set(names)) != len(names):
            raise BoundInputError("manifest input names must be unique")
        repo_paths = [record.repo_path for record in records]
        if len(set(repo_paths)) != len(repo_paths):
            raise BoundInputError("manifest input repo_path values must be unique")
        object.__setattr__(self, "inputs", tuple(sorted(records, key=lambda item: item.name)))

    def to_payload(self) -> dict[str, Any]:
        """Return the durable JSON payload shape of this manifest.

        The payload is published by the caller as an
        ``InputBindingManifestArtifact`` whose child refs are the per-input
        ``content_ref`` values.  The mapping is total, key-ordered, and
        round-trips through :meth:`from_payload` without loss.
        """
        return {
            "schema_version": self.schema_version,
            "workflow_id": self.workflow_id,
            "source_commit": self.source_commit,
            "inputs": [
                {
                    "name": record.name,
                    "repo_path": record.repo_path,
                    "source_commit": record.source_commit,
                    "content_sha256": record.content_sha256,
                    "byte_size": record.byte_size,
                    "required": record.required,
                    "content_ref": record.content_ref.to_dict(),
                }
                for record in self.inputs
            ],
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "InputBindingManifest":
        """Rebuild a manifest from its durable payload.

        Raises:
            BoundInputError: if the payload is not a conforming manifest of
                the supported schema version.
        """
        if not isinstance(value, Mapping):
            raise BoundInputError("manifest payload must be a mapping")
        schema_version = value.get("schema_version")
        if schema_version != INPUT_BINDING_SCHEMA_VERSION:
            raise BoundInputError(f"unsupported manifest payload schema_version: {schema_version!r}")
        workflow_id = value.get("workflow_id")
        if not isinstance(workflow_id, str) or not workflow_id.strip():
            raise BoundInputError("manifest payload workflow_id must be a non-empty string")
        source_commit = value.get("source_commit")
        raw_inputs = value.get("inputs")
        if not isinstance(raw_inputs, (list, tuple)):
            raise BoundInputError("manifest payload inputs must be an array")
        records: list[BoundInputRecord] = []
        for raw in raw_inputs:
            if not isinstance(raw, Mapping):
                raise BoundInputError("each manifest payload input must be a mapping")
            raw_ref = raw.get("content_ref")
            if not isinstance(raw_ref, Mapping):
                raise BoundInputError("each manifest payload input must carry a content_ref mapping")
            content_ref = ArtifactRef.from_mapping(raw_ref)
            records.append(
                BoundInputRecord(
                    name=raw.get("name"),
                    repo_path=raw.get("repo_path"),
                    source_commit=raw.get("source_commit", source_commit),
                    content_sha256=raw.get("content_sha256"),
                    byte_size=raw.get("byte_size"),
                    content_ref=content_ref,
                    required=raw["required"] if "required" in raw else True,
                )
            )
        return cls(
            schema_version=INPUT_BINDING_SCHEMA_VERSION,
            workflow_id=workflow_id,
            source_commit=source_commit,
            inputs=tuple(records),
        )

    def record(self, name: str) -> BoundInputRecord:
        """Return the record for ``name``.

        Raises:
            BoundInputError: if no record with that name exists.
        """
        for record in self.inputs:
            if record.name == name:
                return record
        raise BoundInputError(f"no bound input named {name!r} in manifest of {self.workflow_id!r}")


def declared_inputs_from_references(
    references: Sequence[Any],
) -> tuple[DeclaredInput, ...]:
    """Extract the explicitly declared repo-relative inputs from references.

    Consumes the normalized workflow/task reference entries (the merged
    ``references`` array of a ``WorkflowRequestArtifact``).

    **Participation rule (which entries this module binds).**  An entry is a
    declared repo-relative input iff either:

    1. its ``path`` is relative, or
    2. it explicitly carries ``repo_relative: true``, regardless of whether
       its ``path`` is relative or absolute.

    Every participating entry is validated fail-closed below; in particular
    an entry that explicitly declares ``repo_relative: true`` with an
    absolute ``path`` is a participating declaration that *raises*
    :class:`BoundInputError` — it is never silently ignored and never falls
    back to a host-path bind.  Entries that do not participate (an absolute
    ``path`` with no explicit ``repo_relative: true``) are ignored by this
    module and keep their existing host-path reference handling; they never
    become bound inputs.

    **Field derivation (single deterministic rule).**

    * ``DeclaredInput.name`` is the entry's declared ``name`` field,
      verbatim after the safety validation above.  Normalized entries
      always carry a ``name`` (the service normalizer defaults an absent
      name to the path basename), and an explicitly declared ``name`` is
      always preserved.  ``name`` is NEVER derived from ``repo_path``.
    * ``DeclaredInput.required`` is the entry's declared ``required`` flag,
      defaulting to ``True`` when absent.
    * Name uniqueness is enforced over the derived names, so two entries
      declaring distinct names over distinct paths (for example
      ``{"name": "a", "path": "d1/x.md"}`` and
      ``{"name": "b", "path": "d2/x.md"}``) both bind, at
      ``inputs/a/d1/x.md`` and ``inputs/b/d2/x.md`` respectively.

    Returns:
        The declared inputs ordered by name.

    Raises:
        BoundInputError: on any invalid participating declaration: empty or
            duplicate derived names, a name unsafe as a single path
            component, an absolute path (including on entries explicitly
            marked ``repo_relative: true``), a path containing ``..`` or
            ``.`` segments, a home-relative path, or a path that is not a
            normalized POSIX relative path.
    """
    if references is None:
        return ()
    if isinstance(references, (str, bytes)) or not isinstance(references, Sequence):
        raise BoundInputError("references must be a sequence of reference entries")
    declared: list[DeclaredInput] = []
    seen_names: set[str] = set()
    for entry in references:
        if not isinstance(entry, Mapping):
            # Without a path or an explicit repo_relative marker an entry
            # cannot participate in binding; it keeps its existing handling.
            continue
        marker = entry.get("repo_relative", False)
        if marker is not True and marker is not False:
            raise BoundInputError(
                f"reference entry repo_relative flag must be a boolean, got {marker!r}"
            )
        raw_path = entry.get("path")
        if raw_path is None:
            path_text = ""
        elif isinstance(raw_path, str):
            path_text = raw_path.strip()
        else:
            path_text = str(raw_path).strip()
        participates = marker is True or (
            bool(path_text) and not _is_absolute_path_text(path_text)
        )
        if not participates:
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise BoundInputError(
                f"participating reference entry with path {path_text!r} must declare a name"
            )
        if name in seen_names:
            raise BoundInputError(f"duplicate declared input name {name!r}")
        required = entry.get("required", True)
        if not isinstance(required, bool):
            raise BoundInputError(
                f"declared input {name!r} required flag must be a boolean, got {required!r}"
            )
        declared.append(DeclaredInput(name=name, repo_path=path_text, required=required))
        seen_names.add(name)
    return tuple(sorted(declared, key=lambda item: item.name))


def resolve_within_repo(repo_root: Path, repo_path: str) -> Path:
    """Resolve one repo-relative input path inside the repository root.

    Performs full containment validation: the candidate path must be
    relative, must not traverse parents, and its fully resolved location
    (following symlinks) must remain strictly inside the fully resolved
    ``repo_root``.  Symlink escapes, bind-mount tricks, and any resolution
    outside the repository root are rejected.

    Returns:
        The fully resolved absolute path of the input file.

    Raises:
        BoundInputError: if the path is absolute, traverses parents,
            escapes the repository root through symlinks or otherwise, or
            does not address an existing regular file.
    """
    _validated_repo_path(repo_path)
    root = _absolute_without_symlinks(Path(repo_root))
    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        raise BoundInputError(f"repository root {str(root)!r} is not a directory")
    candidate = (resolved_root / repo_path).resolve()
    if candidate == resolved_root or not candidate.is_relative_to(resolved_root):
        raise BoundInputError(
            f"input path {repo_path!r} resolves outside the repository root {str(resolved_root)!r}"
        )
    if not candidate.exists():
        raise BoundInputError(f"input path {repo_path!r} is missing in {str(resolved_root)!r}")
    if not candidate.is_file():
        raise BoundInputError(
            f"input path {repo_path!r} in {str(resolved_root)!r} is not a regular file"
        )
    return candidate


def _repository_head_commit(repo_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=_GIT_HEAD_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BoundInputError(
            f"source repository HEAD could not be resolved in {str(repo_root)!r}: {error}"
        ) from error
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise BoundInputError(
            f"source repository HEAD could not be resolved in {str(repo_root)!r}: "
            f"{detail or 'git rev-parse HEAD failed'}"
        )
    return _validated_commit_id(
        completed.stdout.decode("utf-8", "replace").strip(),
        what="repository HEAD commit",
    )


def capture_input_binding(
    *,
    repo_root: Path,
    workflow_id: str,
    declarations: Sequence[DeclaredInput],
    artifacts: ContentAddressedArtifactStore,
) -> InputBindingManifest:
    """Capture the immutable binding for one workflow.

    Resolves the source repository HEAD commit, validates and reads every
    declared input through :func:`resolve_within_repo`, and stores each
    input's bytes durably as a ``BoundInputArtifact`` in ``artifacts``.
    Each record's ``content_sha256`` is the plain SHA-256 digest of the
    captured bytes (the provenance hash); each ``content_ref`` is the
    artifact store's typed-digest address of the stored blob.  The returned
    manifest is the single durable truth for this workflow's bound inputs;
    the caller publishes it as an ``InputBindingManifestArtifact`` and
    records that ref in the workflow records.

    Optional declarations (``required=False``) that are missing at the
    source are omitted from the manifest; every other failure is fatal.

    Returns:
        The captured manifest.

    Raises:
        BoundInputError: if the repository root is not a Git repository
            with a resolvable HEAD commit, if any required input is
            missing, is not a regular file, fails containment validation,
            or cannot be read.
    """
    if not isinstance(workflow_id, str) or not workflow_id.strip():
        raise BoundInputError("workflow_id must be a non-empty string")
    root = _absolute_without_symlinks(Path(repo_root))
    if not root.is_dir():
        raise BoundInputError(f"repository root {str(root)!r} is not a directory")
    source_commit = _repository_head_commit(root)
    records: list[BoundInputRecord] = []
    for declaration in declarations:
        if not isinstance(declaration, DeclaredInput):
            raise BoundInputError("declarations must contain only DeclaredInput values")
        if not (root / declaration.repo_path).exists():
            if declaration.required:
                raise BoundInputError(
                    f"required input {declaration.name!r} is missing at "
                    f"{declaration.repo_path!r} in {str(root)!r}"
                )
            continue
        resolved = resolve_within_repo(root, declaration.repo_path)
        try:
            data = resolved.read_bytes()
        except OSError as error:
            raise BoundInputError(
                f"input {declaration.name!r} at {declaration.repo_path!r} is unreadable: {error}"
            ) from error
        content_sha256 = hashlib.sha256(data).hexdigest()
        try:
            content_ref = artifacts.put_bytes(
                data,
                artifact_type=BOUND_INPUT_ARTIFACT,
                provenance={
                    "workflow_id": workflow_id,
                    "name": declaration.name,
                    "repo_path": declaration.repo_path,
                    "source_commit": source_commit,
                },
                metadata={"content_sha256": content_sha256, "required": declaration.required},
            )
        except (OSError, ValueError) as error:
            raise BoundInputError(
                f"input {declaration.name!r} could not be stored durably: {error}"
            ) from error
        records.append(
            BoundInputRecord(
                name=declaration.name,
                repo_path=declaration.repo_path,
                source_commit=source_commit,
                content_sha256=content_sha256,
                byte_size=len(data),
                content_ref=content_ref,
                required=declaration.required,
            )
        )
    return InputBindingManifest(
        schema_version=INPUT_BINDING_SCHEMA_VERSION,
        workflow_id=workflow_id,
        source_commit=source_commit,
        inputs=tuple(records),
    )


def bound_input_target_path(name: str, repo_path: str) -> PurePosixPath:
    """Return the deterministic in-workspace relative path of one input.

    The location is exactly ``inputs/<name>/<repo_path>`` relative to the
    role workspace root and is stable across attempts, retries, restarts,
    and fencing.  It is the documented, predictable access location for
    workers whose workspace contract does not include the repository.
    """
    validated_name = _validated_name(name)
    validated_repo_path = _validated_repo_path(repo_path, name=validated_name)
    return PurePosixPath(BOUND_INPUTS_ROOT, validated_name, validated_repo_path)


def is_bound_input_path(relative_path: str | PurePosixPath) -> bool:
    """Return whether one workspace-relative path is under the inputs root.

    This is the single authoritative containment predicate for the
    bound-inputs root.  It returns ``True`` iff the FIRST component of the
    workspace-relative POSIX path is exactly ``inputs``; equivalently, iff
    the path is ``inputs`` itself or lies strictly beneath it (for example
    ``inputs/a/d1/x.md``).

    It returns ``False`` for any path that merely contains an ``inputs``
    component elsewhere, such as ``src/inputs/data.py``,
    ``docs/inputs/notes.md``, or ``pkg/inputs``.  Such paths are ordinary
    worker-authored content and are never excluded from candidate
    snapshots, tree fingerprints, verification workspaces, or deliverables.

    The predicate is pure and total: it performs no filesystem access and
    never raises, including for empty or malformed input (which is simply
    not under the root).
    """
    if isinstance(relative_path, PurePath):
        parts = relative_path.parts
    elif isinstance(relative_path, str):
        parts = PurePosixPath(relative_path).parts
    else:
        return False
    return bool(parts) and parts[0] == BOUND_INPUTS_ROOT


def _open_materialization_parent(
    root: Path,
    relative_path: PurePosixPath,
    *,
    create: bool,
) -> tuple[int, str]:
    """Open a target parent beneath ``root`` without following symlinks."""
    if create:
        root.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(root, flags)
    except OSError as error:
        raise BoundInputError(
            f"bound-input destination root {str(root)!r} is unavailable or is a symlink: {error}"
        ) from error
    try:
        for component in relative_path.parts[:-1]:
            if create:
                try:
                    os.mkdir(component, mode=0o755, dir_fd=directory_fd)
                except FileExistsError:
                    pass
            try:
                child_fd = os.open(component, flags, dir_fd=directory_fd)
            except OSError as error:
                raise BoundInputError(
                    f"bound-input destination component {component!r} is unavailable or is a symlink: {error}"
                ) from error
            os.close(directory_fd)
            directory_fd = child_fd
        return directory_fd, relative_path.name
    except BaseException:
        os.close(directory_fd)
        raise


def _write_bytes_atomically_beneath(
    root: Path,
    relative_path: PurePosixPath,
    data: bytes,
) -> None:
    directory_fd, filename = _open_materialization_parent(root, relative_path, create=True)
    temporary = f".{filename}.{uuid4().hex}.tmp"
    created = False
    try:
        try:
            existing = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and stat.S_ISLNK(existing.st_mode):
            raise BoundInputError(
                f"bound-input destination {relative_path.as_posix()!r} is a symlink"
            )
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        file_fd = os.open(temporary, file_flags, 0o600, dir_fd=directory_fd)
        created = True
        with os.fdopen(file_fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(
            temporary,
            filename,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        created = False
        os.fsync(directory_fd)
    finally:
        if created:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)


def _read_bytes_beneath(root: Path, relative_path: PurePosixPath) -> bytes:
    directory_fd, filename = _open_materialization_parent(root, relative_path, create=False)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        file_fd = os.open(filename, flags, dir_fd=directory_fd)
        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise BoundInputError(
                    f"bound-input destination {relative_path.as_posix()!r} is not a regular file"
                )
            with os.fdopen(file_fd, "rb") as stream:
                file_fd = -1
                return stream.read()
        finally:
            if file_fd >= 0:
                os.close(file_fd)
    finally:
        os.close(directory_fd)


def materialize_bound_inputs(
    *,
    manifest: InputBindingManifest,
    artifacts: ContentAddressedArtifactStore,
    destination_root: Path,
) -> dict[str, Path]:
    """Materialize every bound input into a role workspace.

    Reads each record's content from the durable artifact store via its
    ``content_ref`` (never from the mutable host repository), verifies the
    plain SHA-256 content digest of the retrieved bytes against the
    manifest's ``content_sha256`` while writing, and places it at
    ``destination_root / bound_input_target_path(...)``.  The operation is
    idempotent and deterministic: materializing the same manifest twice
    produces byte-identical trees, which is what a recovered attempt
    re-executes.

    The ``inputs`` tree under ``destination_root`` is owned exclusively by
    the Manager-side materializer; workers read it but never own it.

    Returns:
        Mapping from input name to the absolute materialized file path.

    Raises:
        BoundInputError: if a content artifact is unavailable, fails hash
            verification, or cannot be written.
    """
    if not isinstance(manifest, InputBindingManifest):
        raise BoundInputError("manifest must be an InputBindingManifest")
    root = _absolute_without_symlinks(Path(destination_root))
    materialized: dict[str, Path] = {}
    for record in manifest.inputs:
        try:
            data = artifacts.read_bytes(record.content_ref)
        except (OSError, ValueError) as error:
            raise BoundInputError(
                f"bound input {record.name!r} durable content is unavailable: {error}"
            ) from error
        digest = hashlib.sha256(data).hexdigest()
        if digest != record.content_sha256:
            raise BoundInputError(
                f"bound input {record.name!r} durable content digest mismatch: "
                f"expected {record.content_sha256}, got {digest}"
            )
        relative_target = bound_input_target_path(record.name, record.repo_path)
        target = root / relative_target
        try:
            _write_bytes_atomically_beneath(root, relative_target, data)
        except (OSError, BoundInputError) as error:
            raise BoundInputError(
                f"bound input {record.name!r} could not be materialized at {str(target)!r}: {error}"
            ) from error
        materialized[record.name] = target
    return materialized


def verify_bound_inputs(
    *,
    manifest: InputBindingManifest,
    destination_root: Path,
) -> None:
    """Re-verify materialized inputs against the manifest hashes.

    Re-computes the plain SHA-256 content digest of every materialized file
    in place and compares it with the recorded ``content_sha256``.  Used
    after re-materialization on retry, restart, and attempt recovery so a
    recovered attempt provably consumes the same bound inputs.

    Raises:
        BoundInputError: if any materialized input is missing, is not a
            regular file, or its SHA-256 differs from the recorded
            ``content_sha256``.
    """
    if not isinstance(manifest, InputBindingManifest):
        raise BoundInputError("manifest must be an InputBindingManifest")
    root = _absolute_without_symlinks(Path(destination_root))
    for record in manifest.inputs:
        relative_path = bound_input_target_path(record.name, record.repo_path)
        path = root / relative_path
        try:
            data = _read_bytes_beneath(root, relative_path)
        except FileNotFoundError as error:
            raise BoundInputError(
                f"materialized input {record.name!r} is missing at {str(path)!r}"
            ) from error
        except OSError as error:
            raise BoundInputError(
                f"materialized input {record.name!r} at {str(path)!r} is unreadable: {error}"
            ) from error
        digest = hashlib.sha256(data).hexdigest()
        if digest != record.content_sha256:
            raise BoundInputError(
                f"materialized input {record.name!r} at {str(path)!r} drifted: "
                f"expected content SHA-256 {record.content_sha256}, got {digest}"
            )


def verify_repo_bound_inputs(
    *,
    manifest: InputBindingManifest,
    repo_root: Path,
) -> None:
    """Verify in-repo inputs of a repository-including workspace.

    For role workspaces whose contract already includes the repository
    (Git-backed worktrees at a pinned commit), bound inputs are accessed at
    their repository-relative paths instead of being copied.  This check
    re-hashes those in-repo files against the manifest so the same
    provenance guarantee holds without duplication.

    Raises:
        BoundInputError: if any input is missing, fails containment
            validation, or the plain SHA-256 content digest of the in-repo
            file differs from the recorded ``content_sha256``.
    """
    if not isinstance(manifest, InputBindingManifest):
        raise BoundInputError("manifest must be an InputBindingManifest")
    root = Path(repo_root)
    for record in manifest.inputs:
        resolved = resolve_within_repo(root, record.repo_path)
        try:
            data = resolved.read_bytes()
        except OSError as error:
            raise BoundInputError(
                f"bound input {record.name!r} at {record.repo_path!r} is unreadable: {error}"
            ) from error
        digest = hashlib.sha256(data).hexdigest()
        if digest != record.content_sha256:
            raise BoundInputError(
                f"bound input {record.name!r} at {record.repo_path!r} drifted: "
                f"expected content SHA-256 {record.content_sha256}, got {digest}"
            )


def bound_input_reference_entries(
    *,
    manifest: InputBindingManifest,
    workspace_root: Path | None = None,
    repo_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Project a manifest into ``workspace["reference_paths"]`` entries.

    Exactly one of ``workspace_root`` (materialized artifact-style
    workspace) or ``repo_root`` (repository-including workspace) must be
    provided.  Each entry carries ``name``, the deterministic ``path``
    (``<workspace_root>/inputs/<name>/<repo_path>`` or the in-repo absolute
    path), ``include=[repo_path]``, ``mode="read_only"``,
    ``truth_source=True``, the record's ``required``, and
    ``bound_input=True`` so the existing sandbox, prompt, and gateway
    machinery advertises the input without a host bind mount.

    Returns:
        The reference entries ordered by name.

    Raises:
        BoundInputError: if neither or both roots are provided.
    """
    if not isinstance(manifest, InputBindingManifest):
        raise BoundInputError("manifest must be an InputBindingManifest")
    if (workspace_root is None) == (repo_root is None):
        raise BoundInputError("exactly one of workspace_root or repo_root must be provided")
    entries: list[dict[str, Any]] = []
    for record in manifest.inputs:
        if workspace_root is not None:
            root = _absolute_without_symlinks(Path(workspace_root))
            path: Path = root / bound_input_target_path(record.name, record.repo_path)
        else:
            path = resolve_within_repo(Path(repo_root), record.repo_path)
        entries.append(
            {
                "name": record.name,
                "path": str(path),
                "include": [record.repo_path],
                "mode": "read_only",
                "truth_source": True,
                "required": record.required,
                "bound_input": True,
            }
        )
    return entries
