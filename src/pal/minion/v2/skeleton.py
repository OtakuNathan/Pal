from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from pal.minion.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore


ARCHITECTURE_SKELETON_ARTIFACT = "ArchitectureSkeletonArtifact"
ARCHITECTURE_SKELETON_BUNDLE_ARTIFACT = "ArchitectureSkeletonGitBundleArtifact"
ARCHITECTURE_SUBMISSION_ARTIFACT = "ArchitectureSkeletonSubmissionArtifact"
SKELETON_MODULE_CONTRACT_ARTIFACT = "SkeletonModuleContractArtifact"

MODULE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,79}$")
CONTRACT_COMMENT_SECTIONS = (
    "Module",
    "Responsibility",
    "Requirements",
    "Provides",
    "Consumes",
    "Ownership",
    "Lifecycle",
    "State",
    "Invariants",
    "Errors",
    "Compatibility",
)
PATH_SCOPE_KINDS = frozenset({"file", "directory", "prefix"})
_IGNORED_SNAPSHOT_PARTS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "__pycache__",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
    }
)
_SENSITIVE_BASENAMES = frozenset(
    {
        ".env",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
    }
)
_PRIVATE_KEY_MARKER = b"-----BEGIN PRIVATE KEY-----"
_MAX_SNAPSHOT_FILE_BYTES = 25 * 1024 * 1024


class SemanticReferenceError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        reference: Mapping[str, Any] | None = None,
        possible_matches: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        super().__init__(message)
        self.reference = dict(reference or {})
        self.possible_matches = tuple(dict(item) for item in possible_matches)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": str(self),
            "reference": self.reference,
            "possible_matches": list(self.possible_matches),
        }


@dataclass(frozen=True)
class SemanticRequirement:
    section: str
    requirement: str
    strength: str = "hard"

    def to_dict(self) -> dict[str, str]:
        return {
            "section": self.section,
            "requirement": self.requirement,
            "strength": self.strength,
        }


@dataclass(frozen=True)
class PathScope:
    kind: str
    path: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "path": self.path}

    def matches(self, candidate: str) -> bool:
        normalized = _normalized_repo_path(candidate)
        if self.kind == "file":
            return normalized == self.path
        if self.kind == "directory":
            return normalized == self.path or normalized.startswith(self.path + "/")
        candidate_path = PurePosixPath(normalized)
        prefix_path = PurePosixPath(self.path)
        return candidate_path.parent == prefix_path.parent and candidate_path.name.startswith(prefix_path.name)


@dataclass(frozen=True)
class ArchitectureWorkspace:
    worktree: Path
    common_git_dir: Path
    base_sha: str
    base_tree_sha: str
    original_head: str
    source_fingerprint: str
    workspace_snapshot_ref: ArtifactRef


@dataclass(frozen=True)
class SkeletonReviewFinding:
    finding_kind: str
    summary: str
    severity: str = "error"
    affected_modules: tuple[str, ...] = ()
    requirements: tuple[Mapping[str, str], ...] = ()
    locations: tuple[Mapping[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_kind": self.finding_kind,
            "summary": self.summary,
            "severity": self.severity,
            "affected_modules": list(self.affected_modules),
            "requirements": [dict(item) for item in self.requirements],
            "locations": [dict(item) for item in self.locations],
        }


@dataclass(frozen=True)
class SkeletonReviewResult:
    verdict: str
    findings: tuple[SkeletonReviewFinding, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "findings": [item.to_dict() for item in self.findings]}


def semantic_requirements(payload: Mapping[str, Any]) -> tuple[SemanticRequirement, ...]:
    result: list[SemanticRequirement] = []
    sections = payload.get("sections")
    if isinstance(sections, Mapping):
        strengths = dict(payload.get("strengths") or {})
        for raw_section, values in sections.items():
            section = str(raw_section or "").strip()
            if not section:
                raise ValueError("RequirementsArtifact section names must not be empty")
            for value in list(values or []):
                requirement = str(value or "").strip()
                if not requirement:
                    raise ValueError(f"RequirementsArtifact section {section!r} contains an empty requirement")
                strength = str(strengths.get(requirement) or "hard").strip().lower()
                result.append(SemanticRequirement(section, requirement, _requirement_strength(strength)))
    else:
        for value in list(payload.get("requirements") or []):
            if not isinstance(value, Mapping):
                raise ValueError("RequirementsArtifact requirements must be objects")
            requirement = str(value.get("statement") or value.get("requirement") or value.get("text") or "").strip()
            if not requirement:
                raise ValueError("RequirementsArtifact contains an empty requirement")
            section = str(value.get("section") or "Requirements").strip()
            strength = _requirement_strength(str(value.get("strength") or "hard").strip().lower())
            result.append(SemanticRequirement(section, requirement, strength))
    if not result:
        raise ValueError("RequirementsArtifact must contain at least one requirement")
    return tuple(result)


def requirements_semantic_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    requirements = semantic_requirements(payload)
    sections: dict[str, list[str]] = {}
    strengths: dict[str, str] = {}
    for item in requirements:
        sections.setdefault(item.section, []).append(item.requirement)
        if item.strength != "hard":
            strengths[item.requirement] = item.strength
    result: dict[str, Any] = {
        "title": str(payload.get("title") or "Requirements").strip(),
        "sections": sections,
    }
    if strengths:
        result["strengths"] = strengths
    clarifications = list(payload.get("open_clarifications") or [])
    if clarifications:
        result["open_clarifications"] = clarifications
    return result


def resolve_requirement_reference(
    reference: Mapping[str, Any],
    requirements_payload: Mapping[str, Any],
) -> SemanticRequirement:
    text = str(reference.get("requirement") or reference.get("statement") or "").strip()
    section = str(reference.get("section") or "").strip()
    if not text:
        raise SemanticReferenceError("Requirement reference must include the Requirement text.", reference=reference)
    normalized = _normalized_semantic_text(text)
    candidates = [
        item
        for item in semantic_requirements(requirements_payload)
        if _normalized_semantic_text(item.requirement) == normalized
        and (not section or _normalized_semantic_text(item.section) == _normalized_semantic_text(section))
    ]
    if len(candidates) == 1:
        return candidates[0]
    all_requirements = semantic_requirements(requirements_payload)
    suggestions = _semantic_requirement_suggestions(text, section, all_requirements)
    if not candidates:
        raise SemanticReferenceError(
            "Requirement reference cannot be resolved by normalized exact match.",
            reference=reference,
            possible_matches=[item.to_dict() for item in suggestions],
        )
    raise SemanticReferenceError(
        "Requirement reference is ambiguous; include its natural-language section.",
        reference=reference,
        possible_matches=[item.to_dict() for item in candidates],
    )


def resolve_evidence_reference(
    reference: Mapping[str, Any],
    *,
    workspace_root: Path,
    reference_roots: Mapping[str, Path] | None = None,
    evidence_catalog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    value = {str(key): item for key, item in dict(reference or {}).items() if item not in (None, "")}
    kind = str(value.get("kind") or "").strip()
    path_value = str(value.get("path") or "").strip()
    if path_value:
        root = Path(workspace_root)
        reference_name = str(value.get("reference") or value.get("reference_name") or "").strip()
        if kind.startswith("reference_") or reference_name:
            roots = {str(key): Path(item) for key, item in dict(reference_roots or {}).items()}
            if reference_name:
                root = roots.get(reference_name, Path())
            elif len(roots) == 1:
                root = next(iter(roots.values()))
            else:
                raise SemanticReferenceError(
                    "Reference evidence must name its declared reference root.", reference=value
                )
        if not str(root):
            raise SemanticReferenceError("Evidence reference root cannot be resolved.", reference=value)
        relative = _normalized_repo_path(path_value)
        target = (root.expanduser().resolve() / relative).resolve()
        if not target.is_relative_to(root.expanduser().resolve()) or not target.is_file():
            raise SemanticReferenceError("Evidence path does not exist in its declared root.", reference=value)
        symbol = str(value.get("symbol") or "").strip()
        if symbol:
            content = target.read_text(encoding="utf-8", errors="replace")
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])", content) is None:
                candidates = _symbol_suggestions(symbol, content)
                raise SemanticReferenceError(
                    "Evidence symbol cannot be located in the referenced file.",
                    reference=value,
                    possible_matches=[{"path": relative, "symbol": item} for item in candidates],
                )
        return value
    catalog_entries = list(dict(evidence_catalog or {}).get("evidence") or [])
    matches = [entry for entry in catalog_entries if _evidence_semantically_matches(value, dict(entry or {}))]
    if len(matches) == 1:
        return value
    if kind in {"documentation", "research_conclusion"} and not catalog_entries:
        raise SemanticReferenceError("Evidence catalog is unavailable for this semantic source.", reference=value)
    if len(matches) > 1:
        raise SemanticReferenceError(
            "Evidence reference is ambiguous.", reference=value, possible_matches=[dict(item) for item in matches]
        )
    raise SemanticReferenceError("Evidence reference cannot be resolved.", reference=value)


def validate_architecture_submission(
    submission: Mapping[str, Any],
    *,
    requirements_payload: Mapping[str, Any],
    workspace_root: Path,
    reference_roots: Mapping[str, Path] | None = None,
    evidence_catalog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    raw_modules = submission.get("modules")
    if not isinstance(raw_modules, Mapping) or not raw_modules:
        raise ValueError("Architecture submission requires a non-empty modules map")
    modules: dict[str, dict[str, Any]] = {}
    for raw_name, raw_module in raw_modules.items():
        name = str(raw_name or "").strip()
        if MODULE_NAME_PATTERN.fullmatch(name) is None:
            raise ValueError(f"invalid semantic module name: {name or '<empty>'}")
        if not isinstance(raw_module, Mapping):
            raise ValueError(f"module {name} must be an object")
        module = dict(raw_module)
        depends_on = _unique_text(module.get("depends_on"))
        paths = _normalize_module_paths(name, module.get("paths"))
        covers = [
            resolve_requirement_reference(dict(item or {}), requirements_payload).to_dict()
            for item in list(module.get("covers") or [])
        ]
        if not covers:
            raise ValueError(f"module {name} must cover at least one Requirement")
        evidence = [
            resolve_evidence_reference(
                dict(item or {}),
                workspace_root=workspace_root,
                reference_roots=reference_roots,
                evidence_catalog=evidence_catalog,
            )
            for item in list(module.get("evidence") or [])
        ]
        modules[name] = {
            "depends_on": depends_on,
            "paths": paths,
            "covers": covers,
            "evidence": evidence,
        }
    names = set(modules)
    unknown = sorted({dependency for module in modules.values() for dependency in module["depends_on"] if dependency not in names})
    if unknown:
        raise ValueError("Architecture DAG references unknown modules: " + ", ".join(unknown))
    cycle = _cycle_nodes({name: module["depends_on"] for name, module in modules.items()})
    if cycle:
        raise ValueError("Architecture DAG contains a cycle: " + ", ".join(cycle))
    integration = _normalize_integration(submission.get("integration"), requirements_payload)
    _validate_path_policy(modules, integration)
    _validate_contract_entrypoints(modules, integration, Path(workspace_root))
    return {"modules": modules, "integration": integration}


def review_architecture_skeleton(
    artifact: Mapping[str, Any],
    *,
    worktree: Path,
    requirements_payload: Mapping[str, Any],
) -> SkeletonReviewResult:
    submission = dict(artifact.get("submission") or {})
    findings: list[SkeletonReviewFinding] = []
    requirements = semantic_requirements(requirements_payload)
    covered = {
        (_normalized_semantic_text(item["section"]), _normalized_semantic_text(item["requirement"]))
        for module in dict(submission.get("modules") or {}).values()
        for item in list(dict(module).get("covers") or [])
    }
    integration = dict(submission.get("integration") or {})
    covered.update(
        (_normalized_semantic_text(item["section"]), _normalized_semantic_text(item["requirement"]))
        for item in list(integration.get("covers") or [])
    )
    missing = [
        item
        for item in requirements
        if item.strength == "hard"
        and (_normalized_semantic_text(item.section), _normalized_semantic_text(item.requirement)) not in covered
    ]
    for item in missing:
        findings.append(
            SkeletonReviewFinding(
                "contract_defect",
                "A hard Requirement is not covered by any module or the integration contract.",
                requirements=(item.to_dict(),),
            )
        )
    for name, raw_module in dict(submission.get("modules") or {}).items():
        module = dict(raw_module)
        entrypoint = str(dict(module.get("paths") or {}).get("contract_entrypoint") or "")
        path = Path(worktree) / entrypoint
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            findings.append(
                SkeletonReviewFinding(
                    "contract_defect",
                    "The module contract entrypoint is not readable UTF-8 source.",
                    affected_modules=(str(name),),
                    locations=({"path": entrypoint, "section": "Contract"},),
                )
            )
            continue
        missing_sections = contract_comment_missing_sections(content, module_name=str(name))
        if missing_sections:
            findings.append(
                SkeletonReviewFinding(
                    "contract_defect",
                    "The code skeleton is missing required contract sections: " + ", ".join(missing_sections),
                    affected_modules=(str(name),),
                    locations=({"path": entrypoint, "section": "Contract"},),
                )
            )
    return SkeletonReviewResult("PASS" if not findings else "FAIL", tuple(findings))


def contract_comment_missing_sections(content: str, *, module_name: str) -> tuple[str, ...]:
    labels = {
        match.group(1).strip(): match.group(2).strip()
        for match in re.finditer(
            rf"(?im)^\s*(?:/[*]+|[*#/\-]+)?\s*({'|'.join(map(re.escape, CONTRACT_COMMENT_SECTIONS))})\s*:\s*(.*?)\s*$",
            content,
        )
    }
    missing = [section for section in CONTRACT_COMMENT_SECTIONS if section not in labels]
    if "Module" in labels and labels["Module"] != module_name:
        missing.append(f"Module: {module_name}")
    for section in ("Responsibility", "Ownership", "Lifecycle", "State", "Invariants", "Errors", "Compatibility"):
        if section in labels and not labels[section]:
            missing.append(f"{section} value")
    return tuple(dict.fromkeys(missing))


def compile_skeleton_markdown(
    artifact: Mapping[str, Any],
    *,
    requirements_payload: Mapping[str, Any],
) -> str:
    submission = dict(artifact.get("submission") or {})
    lines = ["# Architecture Skeleton", "", "## Requirements", ""]
    for item in semantic_requirements(requirements_payload):
        strength = "" if item.strength == "hard" else f" [{item.strength}]"
        lines.append(f"- **{item.section}**{strength}: {item.requirement}")
    lines.extend(["", "## Module DAG", ""])
    for name, raw_module in dict(submission.get("modules") or {}).items():
        module = dict(raw_module)
        paths = dict(module.get("paths") or {})
        dependencies = ", ".join(module.get("depends_on") or []) or "none"
        lines.extend(
            [
                f"### {name}",
                "",
                f"- Starts after: {dependencies}",
                f"- Contract: `{paths.get('contract_entrypoint', '')}`",
                "- Covers:",
            ]
        )
        lines.extend(
            f"  - **{item.get('section', '')}**: {item.get('requirement', '')}"
            for item in list(module.get("covers") or [])
        )
        lines.append("")
    lines.extend(["## Integration", ""])
    integration = dict(submission.get("integration") or {})
    lines.append(f"- Contract: `{integration.get('contract_entrypoint', '')}`")
    for item in list(integration.get("covers") or []):
        lines.append(f"- **{item.get('section', '')}**: {item.get('requirement', '')}")
    return "\n".join(lines).rstrip() + "\n"


@dataclass
class GitBackedSkeletonService:
    runtime_root: Path
    artifacts: ContentAddressedArtifactStore

    def provision_architecture_workspace(
        self,
        *,
        workflow_id: str,
        revision_name: str,
        workspace: Mapping[str, Any],
        requirements_ref: ArtifactRef,
        base_artifact: Mapping[str, Any] | None = None,
    ) -> ArchitectureWorkspace:
        root = Path(self.runtime_root) / "data" / "minion" / "v2" / "architecture" / _safe_component(workflow_id)
        common_git_dir = root / "project.git"
        snapshot_marker = root / "workspace_snapshot_ref.json"
        source = str(workspace.get("repo_path") or workspace.get("cwd") or "").strip()
        if not common_git_dir.exists():
            snapshot = self._create_synthetic_snapshot(common_git_dir, Path(source).expanduser() if source else None)
            snapshot_ref = self.artifacts.put_json(
                snapshot,
                artifact_type="WorkspaceSnapshotArtifact",
                provenance={"workflow_name": workflow_id},
                child_refs=((requirements_ref.sha256, "requirements"),),
            )
            _write_json_atomic(snapshot_marker, snapshot_ref.to_dict())
        elif snapshot_marker.is_file():
            snapshot_ref = ArtifactRef.from_mapping(json.loads(snapshot_marker.read_text(encoding="utf-8")))
            snapshot = self.artifacts.read_json(snapshot_ref)
        else:
            snapshot_ref = self._existing_workspace_snapshot_ref(common_git_dir, requirements_ref, workflow_id)
            snapshot = self.artifacts.read_json(snapshot_ref)
            _write_json_atomic(snapshot_marker, snapshot_ref.to_dict())
        base_sha = str(dict(base_artifact or {}).get("skeleton_commit_sha") or snapshot["snapshot_commit_sha"])
        if base_artifact:
            bundle_value = dict(base_artifact.get("git_bundle_ref") or {})
            if bundle_value:
                self._import_bundle(common_git_dir, ArtifactRef.from_mapping(bundle_value))
        _git_dir(common_git_dir, "cat-file", "-e", f"{base_sha}^{{commit}}")
        worktree = root / "worktrees" / _safe_component(revision_name)
        branch = f"pal/architecture/{_safe_component(revision_name)}"
        if not worktree.exists():
            worktree.parent.mkdir(parents=True, exist_ok=True)
            completed = subprocess.run(
                ["git", f"--git-dir={common_git_dir}", "worktree", "add", "-b", branch, str(worktree), base_sha],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr or completed.stdout or "failed to create architecture worktree")
        return ArchitectureWorkspace(
            worktree=worktree,
            common_git_dir=common_git_dir,
            base_sha=base_sha,
            base_tree_sha=_git_dir(common_git_dir, "rev-parse", f"{base_sha}^{{tree}}").strip(),
            original_head=str(snapshot.get("original_head") or ""),
            source_fingerprint=str(snapshot.get("source_fingerprint") or ""),
            workspace_snapshot_ref=snapshot_ref,
        )

    def snapshot_architecture(
        self,
        *,
        workflow_name: str,
        revision_name: str,
        architecture_workspace: ArchitectureWorkspace,
        submission: Mapping[str, Any],
        requirements_ref: ArtifactRef,
        reference_roots: Mapping[str, Path] | None = None,
        evidence_catalog_ref: ArtifactRef | None = None,
    ) -> ArtifactRef:
        requirements = self.artifacts.read_json(requirements_ref)
        evidence_catalog = self.artifacts.read_json(evidence_catalog_ref) if evidence_catalog_ref else None
        normalized = validate_architecture_submission(
            submission,
            requirements_payload=requirements,
            workspace_root=architecture_workspace.worktree,
            reference_roots=reference_roots,
            evidence_catalog=evidence_catalog,
        )
        changed_paths = _git_changed_paths(architecture_workspace.worktree, architecture_workspace.base_sha)
        declared_contract_paths = _declared_architect_paths(normalized)
        undeclared = [path for path in changed_paths if path not in declared_contract_paths]
        if undeclared:
            raise ValueError("Architect changed paths not declared as frozen contracts: " + ", ".join(undeclared))
        _git(architecture_workspace.worktree, "add", "-A")
        submission_hash = _stable_hash(normalized)
        commit_key = hashlib.sha256(
            f"{architecture_workspace.base_sha}\0{submission_hash}\0{workflow_name}\0{revision_name}".encode("utf-8")
        ).hexdigest()
        message = f"minion architecture skeleton {revision_name}\n\nPal-Architecture-Key: {commit_key}"
        existing_sha = _find_architecture_commit(architecture_workspace.worktree, commit_key)
        if existing_sha:
            skeleton_sha = existing_sha
        else:
            current_head = _git(architecture_workspace.worktree, "rev-parse", "HEAD").strip()
            if current_head != architecture_workspace.base_sha:
                raise ValueError(
                    "Architect changed Git HEAD; commits, merges, rebases, checkouts, and resets are manager-owned operations"
                )
            _git(
                architecture_workspace.worktree,
                "-c",
                "user.name=Pal Minion",
                "-c",
                "user.email=minion@localhost",
                "commit",
                "--allow-empty",
                "-m",
                message,
            )
            skeleton_sha = _git(architecture_workspace.worktree, "rev-parse", "HEAD").strip()
        skeleton_tree = _git(architecture_workspace.worktree, "rev-parse", f"{skeleton_sha}^{{tree}}").strip()
        with tempfile.TemporaryDirectory(prefix="pal-skeleton-bundle-") as temporary:
            bundle_path = Path(temporary) / "architecture.bundle"
            _git(architecture_workspace.worktree, "bundle", "create", str(bundle_path), "--all")
            bundle_ref = self.artifacts.put_bytes(
                bundle_path.read_bytes(),
                artifact_type=ARCHITECTURE_SKELETON_BUNDLE_ARTIFACT,
                media_type="application/x-git-bundle",
                provenance={"workflow_name": workflow_name, "revision_name": revision_name},
            )
        submission_ref = self.artifacts.put_json(
            normalized,
            artifact_type=ARCHITECTURE_SUBMISSION_ARTIFACT,
            provenance={"workflow_name": workflow_name, "revision_name": revision_name},
            child_refs=((requirements_ref.sha256, "requirements"),),
        )
        payload = {
            "schema_version": "1",
            "requirements_ref": requirements_ref.to_dict(),
            "evidence_catalog_ref": evidence_catalog_ref.to_dict() if evidence_catalog_ref else {},
            "submission": normalized,
            "submission_ref": submission_ref.to_dict(),
            "git_bundle_ref": bundle_ref.to_dict(),
            "workspace_snapshot_ref": architecture_workspace.workspace_snapshot_ref.to_dict(),
            "base_commit_sha": architecture_workspace.base_sha,
            "base_tree_sha": architecture_workspace.base_tree_sha,
            "skeleton_commit_sha": skeleton_sha,
            "skeleton_tree_sha": skeleton_tree,
            "changed_paths": changed_paths,
            "path_policy": _compiled_path_policy(normalized),
            "original_workspace_head": architecture_workspace.original_head,
            "source_fingerprint": architecture_workspace.source_fingerprint,
        }
        children = [
            (requirements_ref.sha256, "requirements"),
            (submission_ref.sha256, "semantic_dag"),
            (bundle_ref.sha256, "git_bundle"),
            (architecture_workspace.workspace_snapshot_ref.sha256, "workspace_snapshot"),
        ]
        if evidence_catalog_ref:
            children.append((evidence_catalog_ref.sha256, "evidence_catalog"))
        return self.artifacts.put_json(
            payload,
            artifact_type=ARCHITECTURE_SKELETON_ARTIFACT,
            provenance={"workflow_name": workflow_name, "revision_name": revision_name},
            child_refs=tuple(children),
        )

    def materialize_bundle(self, bundle_ref: ArtifactRef, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.artifacts.read_bytes(bundle_ref))

    def provision_review_worktree(
        self,
        *,
        artifact: Mapping[str, Any],
        review_name: str,
    ) -> Path:
        root = Path(self.runtime_root) / "data" / "minion" / "v2" / "architecture-reviews" / _safe_component(review_name)
        bare = root / "project.git"
        worktree = root / "worktree"
        bundle_ref = ArtifactRef.from_mapping(dict(artifact.get("git_bundle_ref") or {}))
        skeleton_sha = str(artifact.get("skeleton_commit_sha") or "")
        if not bare.exists():
            root.mkdir(parents=True, exist_ok=True)
            bundle = root / "architecture.bundle"
            self.materialize_bundle(bundle_ref, bundle)
            completed = subprocess.run(
                ["git", "clone", "--bare", str(bundle), str(bare)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            bundle.unlink(missing_ok=True)
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr or completed.stdout or "failed to restore architecture review repository")
        if not worktree.exists():
            completed = subprocess.run(
                ["git", f"--git-dir={bare}", "worktree", "add", "--detach", str(worktree), skeleton_sha],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr or completed.stdout or "failed to restore architecture review worktree")
        if _git(worktree, "rev-parse", "HEAD").strip() != skeleton_sha:
            raise RuntimeError("architecture review worktree is not bound to the skeleton commit")
        return worktree

    def _import_bundle(self, common_git_dir: Path, bundle_ref: ArtifactRef) -> None:
        with tempfile.TemporaryDirectory(prefix="pal-skeleton-import-") as temporary:
            bundle = Path(temporary) / "architecture.bundle"
            self.materialize_bundle(bundle_ref, bundle)
            namespace = f"refs/pal/import/{bundle_ref.sha256[:16]}"
            completed = subprocess.run(
                [
                    "git",
                    f"--git-dir={common_git_dir}",
                    "fetch",
                    str(bundle),
                    f"+refs/heads/*:{namespace}/*",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr or completed.stdout or "failed to import architecture bundle")

    def _create_synthetic_snapshot(self, common_git_dir: Path, source: Path | None) -> dict[str, Any]:
        common_git_dir.parent.mkdir(parents=True, exist_ok=True)
        original_head = ""
        files: list[str] = []
        with tempfile.TemporaryDirectory(prefix="pal-workspace-snapshot-") as temporary:
            seed = Path(temporary) / "seed"
            seed.mkdir()
            if source is not None:
                source = source.resolve()
                if not source.is_dir():
                    raise ValueError(f"workspace source is not a directory: {source}")
                original_head = _optional_git(source, "rev-parse", "HEAD")
                for relative in _workspace_snapshot_paths(source):
                    _copy_snapshot_path(source, seed, relative)
                    files.append(relative)
            _git(seed, "init", "-q", "-b", "main")
            _git(seed, "add", "-A")
            _git(
                seed,
                "-c",
                "user.name=Pal Minion",
                "-c",
                "user.email=minion@localhost",
                "commit",
                "--allow-empty",
                "-qm",
                "Minion synthetic workspace snapshot",
            )
            snapshot_sha = _git(seed, "rev-parse", "HEAD").strip()
            snapshot_tree = _git(seed, "rev-parse", "HEAD^{tree}").strip()
            completed = subprocess.run(
                ["git", "clone", "--bare", str(seed), str(common_git_dir)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr or completed.stdout or "failed to persist synthetic workspace snapshot")
        return {
            "schema_version": "1",
            "snapshot_commit_sha": snapshot_sha,
            "snapshot_tree_sha": snapshot_tree,
            "original_head": original_head,
            "source_fingerprint": snapshot_tree,
            "included_paths": files,
        }

    def _existing_workspace_snapshot_ref(
        self,
        common_git_dir: Path,
        requirements_ref: ArtifactRef,
        workflow_id: str,
    ) -> ArtifactRef:
        snapshot_sha = _git_dir(common_git_dir, "rev-list", "--max-parents=0", "HEAD").splitlines()[-1]
        payload = {
            "schema_version": "1",
            "snapshot_commit_sha": snapshot_sha,
            "snapshot_tree_sha": _git_dir(common_git_dir, "rev-parse", f"{snapshot_sha}^{{tree}}").strip(),
            "original_head": "",
            "source_fingerprint": "",
            "included_paths": [],
        }
        return self.artifacts.put_json(
            payload,
            artifact_type="WorkspaceSnapshotArtifact",
            provenance={"workflow_name": workflow_id, "recovered": True},
            child_refs=((requirements_ref.sha256, "requirements"),),
        )


def _normalize_module_paths(module_name: str, value: Any) -> dict[str, Any]:
    paths = dict(value or {}) if isinstance(value, Mapping) else {}
    contract_entrypoint = _normalized_repo_path(str(paths.get("contract_entrypoint") or ""))
    if not contract_entrypoint:
        raise ValueError(f"module {module_name} requires paths.contract_entrypoint")
    frozen = [_normalized_repo_path(str(item)) for item in list(paths.get("frozen_contract") or [])]
    if contract_entrypoint not in frozen:
        frozen.insert(0, contract_entrypoint)
    owned_impl = _normalize_path_scopes(paths.get("owned_impl"), field=f"{module_name}.owned_impl")
    owned_test = _normalize_path_scopes(paths.get("owned_test"), field=f"{module_name}.owned_test")
    references = [_normalized_repo_path(str(item)) for item in list(paths.get("reference_only") or [])]
    return {
        "contract_entrypoint": contract_entrypoint,
        "frozen_contract": list(dict.fromkeys(frozen)),
        "owned_impl": [item.to_dict() for item in owned_impl],
        "owned_test": [item.to_dict() for item in owned_test],
        "reference_only": list(dict.fromkeys(references)),
    }


def _normalize_integration(value: Any, requirements_payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("Architecture submission requires an integration contract")
    integration = dict(value)
    entrypoint = _normalized_repo_path(str(integration.get("contract_entrypoint") or ""))
    if not entrypoint:
        contract_paths = list(integration.get("contract_paths") or [])
        entrypoint = _normalized_repo_path(str(contract_paths[0] if contract_paths else ""))
    if not entrypoint:
        raise ValueError("integration.contract_entrypoint is required")
    frozen = [_normalized_repo_path(str(item)) for item in list(integration.get("frozen_contract") or integration.get("contract_paths") or [])]
    if entrypoint not in frozen:
        frozen.insert(0, entrypoint)
    owned = _normalize_path_scopes(
        integration.get("owned_impl") or integration.get("owned_paths"), field="integration.owned_impl"
    )
    covers = [
        resolve_requirement_reference(dict(item or {}), requirements_payload).to_dict()
        for item in list(integration.get("covers") or [])
    ]
    if not covers:
        raise ValueError("integration must cover at least one end-to-end Requirement")
    return {
        "contract_entrypoint": entrypoint,
        "frozen_contract": list(dict.fromkeys(frozen)),
        "owned_impl": [item.to_dict() for item in owned],
        "covers": covers,
        "evidence": [dict(item or {}) for item in list(integration.get("evidence") or [])],
    }


def _normalize_path_scopes(value: Any, *, field: str) -> tuple[PathScope, ...]:
    result: list[PathScope] = []
    for raw in list(value or []):
        if isinstance(raw, str):
            raise ValueError(f"{field} path scopes require explicit kind=file|directory|prefix")
        if not isinstance(raw, Mapping):
            raise ValueError(f"{field} path scope must be an object")
        kind = str(raw.get("kind") or "").strip()
        path = _normalized_repo_path(str(raw.get("path") or ""))
        if kind not in PATH_SCOPE_KINDS:
            raise ValueError(f"{field} path scope kind must be file, directory, or prefix")
        if not path or (kind != "file" and "/" not in path):
            raise ValueError(f"{field} must use a file or narrow subdirectory/prefix, never the repository root")
        result.append(PathScope(kind, path.rstrip("/")))
    if not result:
        raise ValueError(f"{field} requires at least one narrow writable path scope")
    return tuple(dict.fromkeys(result))


def _validate_path_policy(modules: Mapping[str, Mapping[str, Any]], integration: Mapping[str, Any]) -> None:
    owners: list[tuple[str, PathScope]] = []
    frozen: dict[str, str] = {}
    references: list[tuple[str, str]] = []
    for name, module in modules.items():
        paths = dict(module["paths"])
        for path in paths["frozen_contract"]:
            previous = frozen.setdefault(path, name)
            if previous != name:
                raise ValueError(f"frozen contract path {path} is owned by both {previous} and {name}")
        for value in [*paths["owned_impl"], *paths["owned_test"]]:
            owners.append((name, PathScope(str(value["kind"]), str(value["path"]))))
        references.extend((name, path) for path in paths["reference_only"])
    for path in integration["frozen_contract"]:
        previous = frozen.setdefault(path, "integration")
        if previous != "integration":
            raise ValueError(f"frozen contract path {path} is owned by both {previous} and integration")
    for value in integration["owned_impl"]:
        owners.append(("integration", PathScope(str(value["kind"]), str(value["path"]))))
    for index, (owner, scope) in enumerate(owners):
        for other_owner, other_scope in owners[index + 1 :]:
            if owner != other_owner and _path_scopes_overlap(scope, other_scope):
                raise ValueError(
                    f"writable path scopes overlap between {owner} and {other_owner}: {scope.path}, {other_scope.path}"
                )
        for contract_path, contract_owner in frozen.items():
            if scope.matches(contract_path):
                raise ValueError(
                    f"writable scope {scope.path} for {owner} overlaps frozen contract {contract_path} owned by {contract_owner}"
                )
        for reference_owner, reference_path in references:
            if scope.matches(reference_path):
                raise ValueError(
                    f"writable scope {scope.path} for {owner} overlaps reference-only path {reference_path} from {reference_owner}"
                )


def _validate_contract_entrypoints(
    modules: Mapping[str, Mapping[str, Any]], integration: Mapping[str, Any], workspace_root: Path
) -> None:
    for name, module in modules.items():
        entrypoint = str(dict(module["paths"])["contract_entrypoint"])
        target = workspace_root / entrypoint
        if not target.is_file():
            raise ValueError(f"module {name} contract entrypoint does not exist: {entrypoint}")
        missing = contract_comment_missing_sections(target.read_text(encoding="utf-8"), module_name=name)
        if missing:
            raise ValueError(f"module {name} contract entrypoint is incomplete: {', '.join(missing)}")
    integration_entrypoint = workspace_root / str(integration["contract_entrypoint"])
    if not integration_entrypoint.is_file():
        raise ValueError(f"integration contract entrypoint does not exist: {integration['contract_entrypoint']}")


def _compiled_path_policy(submission: Mapping[str, Any]) -> dict[str, Any]:
    modules: dict[str, Any] = {}
    for name, raw_module in dict(submission.get("modules") or {}).items():
        paths = dict(dict(raw_module).get("paths") or {})
        modules[name] = {
            "frozen_contract": list(paths.get("frozen_contract") or []),
            "owned_impl": list(paths.get("owned_impl") or []),
            "owned_test": list(paths.get("owned_test") or []),
            "reference_only": list(paths.get("reference_only") or []),
        }
    integration = dict(submission.get("integration") or {})
    return {
        "modules": modules,
        "integration": {
            "frozen_contract": list(integration.get("frozen_contract") or []),
            "owned_impl": list(integration.get("owned_impl") or []),
        },
    }


def _declared_architect_paths(submission: Mapping[str, Any]) -> set[str]:
    result = {
        path
        for module in dict(submission.get("modules") or {}).values()
        for path in list(dict(dict(module).get("paths") or {}).get("frozen_contract") or [])
    }
    result.update(list(dict(submission.get("integration") or {}).get("frozen_contract") or []))
    return result


def _path_scopes_overlap(left: PathScope, right: PathScope) -> bool:
    probes = {left.path, right.path}
    if left.kind == "directory":
        probes.add(left.path + "/__pal_probe__")
    if right.kind == "directory":
        probes.add(right.path + "/__pal_probe__")
    return any(left.matches(path) and right.matches(path) for path in probes)


def _cycle_nodes(depends_on: Mapping[str, Sequence[str]]) -> tuple[str, ...]:
    indegree = {name: 0 for name in depends_on}
    dependents = {name: [] for name in depends_on}
    for name, dependencies in depends_on.items():
        for dependency in dependencies:
            indegree[name] += 1
            dependents[dependency].append(name)
    ready = sorted(name for name, count in indegree.items() if count == 0)
    while ready:
        current = ready.pop(0)
        for dependent in sorted(dependents[current]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort()
    return tuple(sorted(name for name, count in indegree.items() if count))


def _workspace_snapshot_paths(source: Path) -> tuple[str, ...]:
    if (source / ".git").exists() or _optional_git(source, "rev-parse", "--git-dir"):
        completed = subprocess.run(
            ["git", "-C", str(source), "ls-files", "-c", "-o", "--exclude-standard", "-z"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.decode("utf-8", errors="replace") or "failed to enumerate workspace")
        values = completed.stdout.split(b"\0")
        paths = [item.decode("utf-8", errors="surrogateescape") for item in values if item]
    else:
        paths = [
            str(path.relative_to(source)).replace(os.sep, "/")
            for path in source.rglob("*")
            if not any(part in _IGNORED_SNAPSHOT_PARTS for part in path.relative_to(source).parts)
            and (path.is_file() or path.is_symlink())
        ]
    return tuple(sorted(dict.fromkeys(paths)))


def _copy_snapshot_path(source: Path, target: Path, relative: str) -> None:
    normalized = _normalized_repo_path(relative)
    source_path = source / normalized
    target_path = target / normalized
    if not source_path.exists() and not source_path.is_symlink():
        return
    if any(part in _IGNORED_SNAPSHOT_PARTS for part in PurePosixPath(normalized).parts):
        return
    if source_path.name.lower() in _SENSITIVE_BASENAMES or source_path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
        raise ValueError(f"workspace snapshot rejected sensitive path: {normalized}")
    mode = source_path.lstat().st_mode
    if stat.S_ISLNK(mode):
        link = os.readlink(source_path)
        resolved = (source_path.parent / link).resolve()
        if not resolved.is_relative_to(source.resolve()):
            raise ValueError(f"workspace snapshot rejected external symlink: {normalized}")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.symlink_to(link)
        return
    if not stat.S_ISREG(mode):
        raise ValueError(f"workspace snapshot rejected non-regular path: {normalized}")
    size = source_path.stat().st_size
    if size > _MAX_SNAPSHOT_FILE_BYTES:
        raise ValueError(f"workspace snapshot rejected oversized file: {normalized}")
    data = source_path.read_bytes()
    if _PRIVATE_KEY_MARKER in data:
        raise ValueError(f"workspace snapshot rejected private key content: {normalized}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target_path)


def _git_changed_paths(worktree: Path, base_sha: str) -> list[str]:
    result = _git_null_paths(worktree, "diff", "--name-only", "--no-renames", "-z", base_sha)
    result.extend(_git_null_paths(worktree, "ls-files", "--others", "--exclude-standard", "-z"))
    return sorted(dict.fromkeys(result))


def _find_architecture_commit(worktree: Path, commit_key: str) -> str:
    output = _git(worktree, "log", "--all", "--format=%H%x00%B%x00")
    values = output.split("\0")
    marker = f"Pal-Architecture-Key: {commit_key}"
    for index in range(0, len(values) - 1, 2):
        if marker in values[index + 1]:
            return values[index].strip()
    return ""


def _git_null_paths(worktree: Path, *args: str) -> list[str]:
    completed = subprocess.run(
        ["git", "-C", str(worktree), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", errors="replace") or f"git {' '.join(args)} failed")
    return [
        _normalized_repo_path(value.decode("utf-8", errors="surrogateescape"))
        for value in completed.stdout.split(b"\0")
        if value
    ]


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(dict(payload), sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _evidence_semantically_matches(reference: Mapping[str, Any], entry: Mapping[str, Any]) -> bool:
    keys = ("source", "section", "conclusion", "path", "symbol")
    compared = False
    for key in keys:
        expected = str(reference.get(key) or "").strip()
        if not expected:
            continue
        compared = True
        actual = str(entry.get(key) or entry.get("location") or entry.get("summary") or "").strip()
        if _normalized_semantic_text(expected) != _normalized_semantic_text(actual):
            return False
    return compared


def _semantic_requirement_suggestions(
    text: str, section: str, requirements: Sequence[SemanticRequirement]
) -> tuple[SemanticRequirement, ...]:
    wanted = set(_normalized_semantic_text(text).split())
    ranked: list[tuple[float, SemanticRequirement]] = []
    for item in requirements:
        if section and _normalized_semantic_text(section) != _normalized_semantic_text(item.section):
            continue
        words = set(_normalized_semantic_text(item.requirement).split())
        score = len(wanted & words) / max(1, len(wanted | words))
        if score:
            ranked.append((score, item))
    return tuple(item for _score, item in sorted(ranked, key=lambda pair: (-pair[0], pair[1].section))[:5])


def _symbol_suggestions(symbol: str, content: str) -> tuple[str, ...]:
    wanted = symbol.casefold()
    identifiers = sorted(set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", content)))
    ranked = sorted(identifiers, key=lambda item: (_edit_distance(wanted, item.casefold()), item))
    return tuple(ranked[:5])


def _edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for index, left_char in enumerate(left, start=1):
        current = [index]
        for other_index, right_char in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[other_index] + 1,
                    previous[other_index - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def _normalized_semantic_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "—": "-", "–": "-"}))
    text = re.sub(r"\s+", " ", text).strip().rstrip(".。;；")
    return text.casefold()


def _normalized_repo_path(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"repository path must be normalized and relative: {value}")
    return str(path)


def _requirement_strength(value: str) -> str:
    if value not in {"hard", "soft"}:
        raise ValueError(f"invalid Requirement strength: {value}")
    return value


def _unique_text(value: Any) -> list[str]:
    return list(dict.fromkeys(str(item).strip() for item in list(value or []) if str(item).strip()))


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _safe_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._-") or "item"


def _git(worktree: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(worktree), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or f"git {' '.join(args)} failed")
    return completed.stdout


def _git_dir(git_dir: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", f"--git-dir={git_dir}", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or f"git {' '.join(args)} failed")
    return completed.stdout


def _optional_git(worktree: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(worktree), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""
