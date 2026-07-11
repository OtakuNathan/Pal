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

from pal.minion.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.shared import MinionInvocationPack


SOFTWARE_GIT_ADAPTER = "software_git.v2"
ARTIFACT_BUNDLE_ADAPTER = "artifact_bundle.v2"

_LANGUAGE_BY_SUFFIX = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".h": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".go": "go",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".py": "python",
    ".pyi": "python",
    ".rs": "rust",
    ".sh": "shell",
    ".ts": "typescript",
    ".tsx": "typescript",
}

_LSP_BY_LANGUAGE = {
    "c": ("clangd", "clangd"),
    "cpp": ("clangd", "clangd"),
    "go": ("gopls", "gopls"),
    "java": ("jdtls", "jdtls"),
    "javascript": ("typescript", "typescript-language-server"),
    "python": ("pyright", "pyright-langserver"),
    "rust": ("rust-analyzer", "rust-analyzer"),
    "shell": ("bash", "bash-language-server"),
    "typescript": ("typescript", "typescript-language-server"),
}

_IGNORED_DISCOVERY_DIRS = frozenset({".git", ".hg", ".svn", ".venv", "node_modules", "build", "dist", "__pycache__"})


def prepare_v2_workspace_environment(workspace: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    prepared = dict(workspace or {})
    root_value = str(prepared.get("repo_path") or prepared.get("workspace_path") or "").strip()
    root = Path(root_value).expanduser().resolve() if root_value else None
    declared = [str(item).strip() for item in list(prepared.get("languages") or []) if str(item).strip()]
    if prepared.get("primary_language"):
        declared.insert(0, str(prepared["primary_language"]).strip())
    detected, scanned_files = _detect_workspace_languages(root)
    languages = _dedupe([*declared, *detected])
    available_servers: list[str] = []
    unavailable_servers: list[dict[str, str]] = []
    for language in languages:
        server = _LSP_BY_LANGUAGE.get(language)
        if server is None:
            continue
        server_id, command = server
        if shutil.which(command):
            if server_id not in available_servers:
                available_servers.append(server_id)
        else:
            unavailable_servers.append({"language": language, "server_id": server_id, "missing_command": command})
    build_markers = [
        marker
        for marker in ("pyproject.toml", "setup.py", "CMakeLists.txt", "Makefile", "package.json", "Cargo.toml", "go.mod", "pom.xml")
        if root is not None and (root / marker).exists()
    ]
    if languages:
        prepared["languages"] = languages
        prepared.setdefault("primary_language", languages[0])
    prepared["lsp_setup"] = {"servers": available_servers, "languages": languages}
    report = {
        "schema_version": "1",
        "workspace_root": str(root or ""),
        "languages": languages,
        "primary_language": str(prepared.get("primary_language") or ""),
        "scanned_files": scanned_files,
        "build_markers": build_markers,
        "lsp_setup": dict(prepared["lsp_setup"]),
        "unavailable_lsp": unavailable_servers,
        "source_modified": False,
    }
    report["environment_fingerprint"] = hashlib.sha256(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    prepared["prepared_environment_fingerprint"] = report["environment_fingerprint"]
    return prepared, report


def _detect_workspace_languages(root: Path | None, *, limit: int = 5000) -> tuple[list[str], int]:
    if root is None or not root.is_dir():
        return [], 0
    counts: dict[str, int] = {}
    scanned = 0
    for path in root.rglob("*"):
        if any(part in _IGNORED_DISCOVERY_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        scanned += 1
        language = _LANGUAGE_BY_SUFFIX.get(path.suffix.lower())
        if language:
            counts[language] = counts.get(language, 0) + 1
        if scanned >= limit:
            break
    ordered = sorted(counts, key=lambda item: (-counts[item], item))
    return ordered, scanned


def _dedupe(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def prepare_v2_role_workspace(runtime_root: Path, pack: MinionInvocationPack, *, run_id: str) -> MinionInvocationPack:
    workspace = dict(pack.workspace or {})
    source = str(workspace.get("repo_path") or workspace.get("cwd") or "").strip()
    target = Path(runtime_root) / "data" / "minion" / "v2" / "role-workspaces" / _safe_component(run_id)
    invocation_dir = Path(runtime_root) / "data" / "minion" / "v2" / "invocations" / _safe_component(pack.invocation_id)
    if source:
        source_path = Path(source).expanduser().resolve()
        if not source_path.is_dir():
            raise ValueError(f"role workspace source is not a directory: {source_path}")
        if (source_path / ".git").exists():
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                completed = subprocess.run(
                    ["git", "clone", "--no-hardlinks", "--shared", str(source_path), str(target)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                if completed.returncode != 0:
                    raise RuntimeError(completed.stderr or completed.stdout or "failed to clone role workspace")
        else:
            if not target.exists():
                shutil.copytree(source_path, target)
    else:
        target.mkdir(parents=True, exist_ok=True)
    writable_dirs = {
        "run_dir": invocation_dir,
        "artifact_dir": invocation_dir / "artifacts",
        "artifact_stage_dir": invocation_dir / "artifact-stage",
        "log_dir": invocation_dir / "logs",
        "review_scratch_dir": invocation_dir / "review-scratch",
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
    return MinionInvocationPack.from_dict({**pack.to_dict(), "workspace": workspace})


def provision_artifact_workspaces(
    runtime_root: Path,
    *,
    epoch_id: str,
    unit_ids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    root = Path(runtime_root) / "data" / "minion" / "v2" / "epochs" / epoch_id / "artifact-workspaces"
    result: dict[str, dict[str, Any]] = {}
    for unit_id in [*unit_ids, "integration"]:
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
        owned_area: Sequence[str],
        reference_only_paths: Sequence[str],
        unit_contract_hash: str,
        dependency_output_hashes: Mapping[str, str],
        environment_fingerprint: str,
        parent_candidate_digest: str = "",
        repair_bill_ref: Mapping[str, Any] | None = None,
    ) -> tuple[ArtifactRef, str]:
        files = _bundle_files(workspace)
        changed_paths = [str(item["path"]) for item in files]
        violations = [path for path in changed_paths if not _is_owned(path, owned_area)]
        reference_violations = [path for path in changed_paths if _matches_any(path, reference_only_paths)]
        if violations or reference_violations:
            raise ValueError(
                "artifact candidate violates ownership: "
                + json.dumps({"outside_owned_area": violations, "reference_only": reference_violations}, sort_keys=True)
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
        root = Path(self.runtime_root) / "data" / "minion" / "v2" / "verification" / _safe_component(review_id)
        candidate = root / "candidate"
        scratch = root / "scratch"
        if candidate.exists():
            shutil.rmtree(candidate)
        scratch.mkdir(parents=True, exist_ok=True)
        self.materialize_candidate(candidate_ref, candidate)
        return candidate, scratch

    def integrate_candidates(
        self,
        *,
        integration_workspace: Path,
        ordered_candidates: Sequence[Mapping[str, Any]],
        architecture_manifest_sha: str,
    ) -> tuple[ArtifactRef, str]:
        if integration_workspace.exists():
            shutil.rmtree(integration_workspace)
        integration_workspace.mkdir(parents=True, exist_ok=True)
        owners: dict[str, str] = {}
        for candidate in ordered_candidates:
            node_run_id = str(candidate.get("node_run_id") or "")
            ref = dict(candidate.get("candidate_ref") or {})
            payload = dict(self.artifacts.read_json(ref))
            for raw in list(payload.get("files") or []):
                item = dict(raw or {})
                relative = _safe_relative(str(item.get("path") or ""))
                digest = str(item.get("sha256") or "")
                if relative in owners:
                    existing = hashlib.sha256((integration_workspace / relative).read_bytes()).hexdigest()
                    if existing != digest:
                        raise ValueError(f"artifact ownership conflict for {relative}: {owners[relative]} vs {node_run_id}")
                    continue
                content = base64.b64decode(str(item.get("content_base64") or ""))
                target = integration_workspace / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                owners[relative] = node_run_id
        ref, digest = self.snapshot_candidate(
            workspace=integration_workspace,
            owned_area=("artifact:integration",),
            reference_only_paths=(),
            unit_contract_hash=architecture_manifest_sha,
            dependency_output_hashes={str(item.get("node_run_id") or ""): str(dict(item.get("candidate_ref") or {}).get("sha256") or "") for item in ordered_candidates},
            environment_fingerprint="artifact_bundle.v2",
        )
        return ref, digest

    def publish_deliverable(
        self,
        *,
        workflow_id: str,
        candidate_ref: Mapping[str, Any],
        verification_ref: ArtifactRef,
    ) -> ArtifactRef:
        destination = Path(self.runtime_root) / "data" / "minion" / "v2" / "deliverables" / _safe_component(workflow_id)
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


def _bundle_files(workspace: Path) -> list[dict[str, Any]]:
    if not workspace.is_dir():
        raise ValueError(f"artifact workspace does not exist: {workspace}")
    result = []
    for path in sorted(item for item in workspace.rglob("*") if item.is_file() and ".pal-candidate" not in item.parts):
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
    for path in sorted(item for item in workspace.rglob("*") if item.is_file() and ".pal-candidate" not in item.parts):
        result.append({"path": path.relative_to(workspace).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return result


def _is_owned(path: str, owned_area: Sequence[str]) -> bool:
    if any(str(item).startswith(("artifact:", "component:", "domain:")) for item in owned_area):
        return True
    return _matches_any(path, owned_area)


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
