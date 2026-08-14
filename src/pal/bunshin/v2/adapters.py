from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from pal.lsp.environment import (
    detect_workspace_languages,
    normalize_lsp_language,
)
from pal.bunshin.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.v2.input_binding import BOUND_INPUTS_ROOT, is_bound_input_path
from pal.bunshin.v2.paths import (
    artifact_epoch_root,
    deliverable_root,
    invocation_root,
    role_workspace_root,
    verification_scratch_root,
)
from pal.shared import BunshinInvocationPack


SOFTWARE_GIT_ADAPTER = "software_git.v2"
ARTIFACT_BUNDLE_ADAPTER = "artifact_bundle.v2"

def prepare_v2_workspace_environment(
    workspace: Mapping[str, Any],
    *,
    runtime_root: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _ = runtime_root
    prepared = dict(workspace or {})
    root_value = str(prepared.get("repo_path") or prepared.get("workspace_path") or "").strip()
    root = Path(root_value).expanduser().resolve() if root_value else None
    declared = [
        normalize_lsp_language(item)
        for item in list(prepared.get("languages") or [])
        if str(item).strip()
    ]
    if prepared.get("primary_language"):
        declared.insert(0, normalize_lsp_language(str(prepared["primary_language"])))
    detected, scanned_files = detect_workspace_languages(root)
    languages = _dedupe([*declared, *detected])
    build_markers = [
        marker
        for marker in ("pyproject.toml", "setup.py", "CMakeLists.txt", "Makefile", "package.json", "Cargo.toml", "go.mod", "pom.xml")
        if root is not None and (root / marker).exists()
    ]
    if languages:
        prepared["languages"] = languages
        prepared["primary_language"] = normalize_lsp_language(
            str(prepared.get("primary_language") or languages[0])
        )
    report = {
        "schema_version": "1",
        "workspace_root": str(root or ""),
        "languages": languages,
        "primary_language": str(prepared.get("primary_language") or ""),
        "scanned_files": scanned_files,
        "build_markers": build_markers,
        "source_modified": False,
    }
    report["environment_fingerprint"] = hashlib.sha256(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    # Execution coordinates help the role use its bound workspace without
    # guessing paths, but do not describe the source/toolchain environment.
    report.update(
        {
            "default_shell_cwd": str(root or ""),
            "build_scratch_root": str(prepared.get("build_scratch_dir") or ""),
            "review_scratch_root": str(prepared.get("review_scratch_dir") or ""),
        }
    )
    prepared["prepared_environment_fingerprint"] = report["environment_fingerprint"]
    return prepared, report


def _dedupe(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def prepare_v2_role_workspace(
    runtime_root: Path,
    pack: BunshinInvocationPack,
    *,
    run_id: str,
    attempt_key: str = "",
) -> BunshinInvocationPack:
    workspace = dict(pack.workspace or {})
    source = str(workspace.get("repo_path") or workspace.get("cwd") or "").strip()
    target = role_workspace_root(runtime_root) / _safe_component(run_id)
    if str(attempt_key or "").strip():
        target = target / "attempts" / _safe_component(attempt_key)
    invocation_dir = invocation_root(runtime_root) / _safe_component(pack.invocation_id)
    attempt_dir = (
        invocation_dir / "attempts" / _safe_component(attempt_key)
        if str(attempt_key or "").strip()
        else invocation_dir
    )
    if source:
        source_path = Path(source).expanduser().resolve()
        if not source_path.is_dir():
            raise ValueError(f"role workspace source is not a directory: {source_path}")
        if (source_path / ".git").exists():
            if not target.exists():
                source_head = subprocess.run(
                    ["git", "-C", str(source_path), "rev-parse", "--verify", "HEAD"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                if source_head.returncode != 0 or not source_head.stdout.strip():
                    raise RuntimeError(
                        source_head.stderr
                        or source_head.stdout
                        or "failed to resolve role workspace source HEAD"
                    )
                source_head_sha = source_head.stdout.strip()
                target.parent.mkdir(parents=True, exist_ok=True)
                completed = subprocess.run(
                    [
                        "git",
                        "clone",
                        "--no-hardlinks",
                        "--shared",
                        "--no-checkout",
                        str(source_path),
                        str(target),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                if completed.returncode != 0:
                    raise RuntimeError(completed.stderr or completed.stdout or "failed to clone role workspace")
                checked_out = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(target),
                        "checkout",
                        "--detach",
                        "--force",
                        source_head_sha,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                if checked_out.returncode != 0:
                    raise RuntimeError(
                        checked_out.stderr
                        or checked_out.stdout
                        or "failed to bind role workspace to source HEAD"
                    )
        else:
            if not target.exists():
                shutil.copytree(source_path, target)
    else:
        target.mkdir(parents=True, exist_ok=True)
    writable_dirs = {
        "run_dir": invocation_dir,
        "artifact_dir": attempt_dir / "artifacts",
        "artifact_stage_dir": attempt_dir / "artifact-stage",
        "log_dir": attempt_dir / "logs",
        "build_scratch_dir": attempt_dir / "build-scratch",
        "review_scratch_dir": attempt_dir / "review-scratch",
    }
    for path in writable_dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    workspace.update(
        {
            "kind": "existing_repo",
            "repo_path": str(target),
            "v2_role_workspace": True,
            "workspace_capability_injection": False,
            **{key: str(path) for key, path in writable_dirs.items()},
        }
    )
    return BunshinInvocationPack.from_dict({**pack.to_dict(), "workspace": workspace})


def provision_artifact_workspaces(
    runtime_root: Path,
    *,
    epoch_id: str,
    unit_ids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    root = artifact_epoch_root(runtime_root) / epoch_id / "artifact-workspaces"
    result: dict[str, dict[str, Any]] = {}
    for unit_id in unit_ids:
        workspace = root / _safe_component(unit_id)
        workspace.mkdir(parents=True, exist_ok=True)
        result[unit_id] = {
            "execution_adapter": ARTIFACT_BUNDLE_ADAPTER,
            "workspace_path": str(workspace),
            "base_digest": _tree_fingerprint(workspace),
            "epoch_base_tree_sha": _tree_fingerprint(workspace),
        }
    return result


@dataclass
class ArtifactBundleAdapter:
    runtime_root: Path
    artifacts: ContentAddressedArtifactStore

    def snapshot_candidate(
        self,
        *,
        workspace: Path,
        reference_only_paths: Sequence[str],
        unit_contract_hash: str,
        dependency_output_hashes: Mapping[str, str],
        environment_fingerprint: str,
        parent_candidate_digest: str = "",
        repair_bill_ref: Mapping[str, Any] | None = None,
    ) -> tuple[ArtifactRef, str]:
        files = _bundle_files(workspace)
        changed_paths = [str(item["path"]) for item in files]
        reference_violations = [path for path in changed_paths if _matches_any(path, reference_only_paths)]
        if reference_violations:
            raise ValueError(
                "artifact candidate modified reference-only paths: " + json.dumps(reference_violations, sort_keys=True)
            )
        payload = {
            "schema_version": "1",
            "adapter": ARTIFACT_BUNDLE_ADAPTER,
            "files": files,
            "tree_fingerprint": _tree_fingerprint(workspace),
            "unit_contract_hash": unit_contract_hash,
            "dependency_output_hashes": dict(dependency_output_hashes),
            "environment_fingerprint": environment_fingerprint,
            "parent_candidate_digest": parent_candidate_digest,
            "repair_bill_ref": dict(repair_bill_ref or {}),
        }
        child_refs = ()
        if repair_bill_ref and repair_bill_ref.get("sha256"):
            child_refs = ((str(repair_bill_ref["sha256"]), "repair_bill"),)
        ref = self.artifacts.put_json(
            payload,
            artifact_type="CandidateSnapshotArtifact",
            child_refs=child_refs,
        )
        return ref, ref.sha256

    def materialize_candidate(self, candidate_ref: ArtifactRef | Mapping[str, Any], destination: Path) -> Path:
        payload = dict(self.artifacts.read_json(candidate_ref))
        if str(payload.get("adapter") or "") != ARTIFACT_BUNDLE_ADAPTER:
            raise ValueError("candidate is not an artifact bundle")
        destination.mkdir(parents=True, exist_ok=True)
        for raw in list(payload.get("files") or []):
            item = dict(raw or {})
            relative = _safe_relative(str(item.get("path") or ""))
            content = base64.b64decode(str(item.get("content_base64") or ""))
            if hashlib.sha256(content).hexdigest() != str(item.get("sha256") or ""):
                raise IOError(f"artifact bundle file failed digest verification: {relative}")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return destination

    def prepare_verification_workspace(self, *, review_id: str, candidate_ref: Mapping[str, Any]) -> tuple[Path, Path]:
        root = verification_scratch_root(self.runtime_root) / _safe_component(review_id)
        candidate = root / "candidate"
        scratch = root / "scratch"
        if candidate.exists():
            shutil.rmtree(candidate)
        scratch.mkdir(parents=True, exist_ok=True)
        self.materialize_candidate(candidate_ref, candidate)
        return candidate, scratch

    def publish_deliverable(
        self,
        *,
        workflow_id: str,
        candidate_ref: Mapping[str, Any],
        verification_ref: ArtifactRef,
    ) -> ArtifactRef:
        destination = deliverable_root(self.runtime_root) / _safe_component(workflow_id)
        if destination.exists():
            shutil.rmtree(destination)
        self.materialize_candidate(candidate_ref, destination)
        payload = dict(self.artifacts.read_json(candidate_ref))
        return self.artifacts.put_json(
            {
                "schema_version": "1",
                "workflow_id": workflow_id,
                "adapter": ARTIFACT_BUNDLE_ADAPTER,
                "destination": str(destination),
                "candidate_ref": dict(candidate_ref),
                "verification_ref": verification_ref.to_dict(),
                "files": [{"path": item["path"], "sha256": item["sha256"]} for item in list(payload.get("files") or [])],
            },
            artifact_type="PublishedDeliverableArtifact",
            child_refs=(
                (str(candidate_ref["sha256"]), "candidate"),
                (verification_ref.sha256, "verification"),
            ),
        )


def artifact_tree_fingerprint(workspace: Path) -> str:
    return _tree_fingerprint(workspace)


def _enumerable_workspace_files(workspace: Path) -> list[Path]:
    """Return the sorted worker-authored files of one artifact workspace.

    Manager-owned surface is never bundle content: ``.pal-candidate``
    staging trees and the bound-inputs root (``BOUND_INPUTS_ROOT``),
    which is written only by the Manager-side materializer.  Whether a
    path lies under the bound-inputs root is decided solely by
    :func:`pal.bunshin.v2.input_binding.is_bound_input_path`, a
    first-path-component match, and is never re-implemented here as a
    component-membership test; paths that merely contain an ``inputs``
    component elsewhere (``src/inputs/data.py``) remain bundle content.
    """
    files: list[Path] = []
    for item in workspace.rglob("*"):
        if not item.is_file() or ".pal-candidate" in item.parts:
            continue
        if is_bound_input_path(item.relative_to(workspace).as_posix()):
            continue
        files.append(item)
    return sorted(files)


def _bundle_files(workspace: Path) -> list[dict[str, Any]]:
    if not workspace.is_dir():
        raise ValueError(f"artifact workspace does not exist: {workspace}")
    result = []
    for path in _enumerable_workspace_files(workspace):
        relative = path.relative_to(workspace).as_posix()
        content = path.read_bytes()
        result.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
        )
    if not result:
        raise ValueError("artifact candidate contains no files")
    return result


def _tree_fingerprint(workspace: Path) -> str:
    digest = hashlib.sha256()
    if not workspace.exists():
        return digest.hexdigest()
    for item in _bundle_files_allow_empty(workspace):
        digest.update(str(item["path"]).encode("utf-8"))
        digest.update(str(item["sha256"]).encode("ascii"))
    return digest.hexdigest()


def _bundle_files_allow_empty(workspace: Path) -> list[dict[str, str]]:
    result = []
    for path in _enumerable_workspace_files(workspace):
        result.append({"path": path.relative_to(workspace).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return result


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    for raw in patterns:
        pattern = str(raw or "").strip().replace("\\", "/")
        if not pattern or pattern.startswith(("artifact:", "component:", "domain:")):
            continue
        if pattern.endswith("/") and (path == pattern[:-1] or path.startswith(pattern)):
            return True
        if fnmatch.fnmatch(path, pattern) or path == pattern:
            return True
    return False


def _safe_relative(value: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"invalid artifact bundle path: {value}")
    return path


def _safe_component(value: str) -> str:
    text = "".join(character if character.isalnum() or character in "-_." else "_" for character in str(value))
    return text.strip("._") or "item"
