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
from pal.minion.v2.paths import (
    minion_data_root,
    project_git_layout_lock,
    resolve_project_git_layout,
)


ARCHITECTURE_SKELETON_ARTIFACT = "ArchitectureSkeletonArtifact"
ARCHITECTURE_SKELETON_BUNDLE_ARTIFACT = "ArchitectureSkeletonGitBundleArtifact"
ARCHITECTURE_SUBMISSION_ARTIFACT = "ArchitectureSkeletonSubmissionArtifact"
ARCHITECTURE_REPAIR_BASELINE_ARTIFACT = "ArchitectureSkeletonRepairBaselineArtifact"
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
PATH_SCOPE_KINDS = frozenset({"file", "directory"})
MODULE_KINDS = frozenset({"implementation", "contract_only"})
VERIFICATION_KINDS = frozenset({"consumer_probe", "end_to_end", "dogfood", "platform"})
VERIFICATION_ENTRYPOINT_KINDS = frozenset(
    {"source_symbol", "build_target", "product_entrypoint", "platform_probe"}
)
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
        return normalized == self.path or normalized.startswith(self.path + "/")


@dataclass(frozen=True)
class ArchitectureWorkspace:
    worktree: Path
    common_git_dir: Path
    base_sha: str
    base_tree_sha: str
    original_head: str
    source_fingerprint: str
    workspace_snapshot_ref: ArtifactRef
    project_name: str = ""
    project_key: str = ""
    workflow_name: str = ""
    workflow_key: str = ""
    workflow_branch: str = ""
    architecture_branch: str = ""


@dataclass(frozen=True)
class ArchitectureReviewWorkspace:
    root: Path
    worktree: Path
    common_git_dir: Path
    temporary_common_git_dir: bool = False

    def cleanup(self) -> None:
        if self.worktree.exists() and self.common_git_dir.is_dir():
            subprocess.run(
                [
                    "git",
                    f"--git-dir={self.common_git_dir}",
                    "worktree",
                    "remove",
                    "--force",
                    str(self.worktree),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            subprocess.run(
                ["git", f"--git-dir={self.common_git_dir}", "worktree", "prune"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        shutil.rmtree(self.root, ignore_errors=True)


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
    patches = [
        {
            key: item[key]
            for key in (
                "patch_kind",
                "section",
                "requirement",
                "strength",
                "reason",
                "affected_modules",
                "affected_contracts",
                "source",
                "observed_at",
            )
            if key in item
        }
        for raw in list(payload.get("patch_ledger") or [])
        if isinstance(raw, Mapping)
        for item in (dict(raw),)
    ]
    if patches:
        result["requirement_patches"] = patches
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
        consumes = [_normalize_contract_reference(item) for item in list(module.get("consumes") or [])]
        module_kind = str(module.get("module_kind") or "").strip()
        if module_kind not in MODULE_KINDS:
            raise ValueError(
                f"module {name} module_kind must be implementation or contract_only"
            )
        paths = _normalize_module_paths(name, module.get("paths"), module_kind=module_kind)
        covers = [
            _architecture_requirement_reference(dict(item or {}), requirements_payload)
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
            "module_kind": module_kind,
            "depends_on": depends_on,
            "consumes": consumes,
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
    verification_nodes = _normalize_verification_nodes(
        submission.get("verification_nodes"),
        requirements_payload=requirements_payload,
        workspace_root=Path(workspace_root),
    )
    collisions = sorted(set(modules) & set(verification_nodes))
    if collisions:
        raise ValueError(
            "Implementation modules and Verification Nodes must have distinct semantic names: "
            + ", ".join(collisions)
        )
    _validate_contract_graph(modules, verification_nodes)
    _validate_verification_coverage(verification_nodes, requirements_payload)
    _validate_path_policy(modules)
    _validate_declared_paths(modules, verification_nodes, Path(workspace_root))
    return {"modules": modules, "verification_nodes": verification_nodes}


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
    verification_nodes = dict(submission.get("verification_nodes") or {})
    verified = {
        (_normalized_semantic_text(item["section"]), _normalized_semantic_text(item["requirement"]))
        for node in verification_nodes.values()
        for item in list(dict(node).get("covers") or [])
    }
    missing = [
        item
        for item in requirements
        if item.strength == "hard"
        and (_normalized_semantic_text(item.section), _normalized_semantic_text(item.requirement)) not in verified
    ]
    for item in missing:
        findings.append(
            SkeletonReviewFinding(
                "contract_defect",
                "A hard Requirement has no real Verification Node landing.",
                requirements=(item.to_dict(),),
            )
        )
    for name, raw_module in dict(submission.get("modules") or {}).items():
        module = dict(raw_module)
        entrypoint = str((list(dict(module.get("paths") or {}).get("contract_paths") or []) or [""])[0])
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
    patches = [dict(item) for item in list(requirements_payload.get("patch_ledger") or [])]
    if patches:
        lines.extend(["", "## Requirement Amendments", ""])
        for patch in patches:
            source = dict(patch.get("source") or {})
            origin = " / ".join(
                item
                for item in (
                    str(source.get("role") or ""),
                    str(source.get("stage") or ""),
                    str(source.get("case") or ""),
                )
                if item
            )
            lines.extend(
                [
                    f"- **{patch.get('section', '')}**: {patch.get('requirement', '')}",
                    f"  - Reason: {patch.get('reason', '')}",
                    f"  - Source: {origin or 'human edit'} at {patch.get('observed_at', '')}",
                ]
            )
    lines.extend(["", "## Construction DAG", ""])
    for name, raw_module in dict(submission.get("modules") or {}).items():
        module = dict(raw_module)
        paths = dict(module.get("paths") or {})
        dependencies = ", ".join(module.get("depends_on") or []) or "none"
        lines.extend(
            [
                f"### {name}",
                "",
                f"- Module kind: {module.get('module_kind', '')}",
                f"- Starts after: {dependencies}",
                f"- Contracts: {', '.join(f'`{item}`' for item in list(paths.get('contract_paths') or []))}",
                f"- Consumes: {', '.join(_format_contract_reference(item) for item in list(module.get('consumes') or [])) or 'none'}",
                "- Covers:",
            ]
        )
        lines.extend(
            f"  - **{item.get('section', '')}**: {item.get('requirement', '')}"
            for item in list(module.get("covers") or [])
        )
        lines.append("")
    lines.extend(["## Verification Topology", ""])
    for name, raw_node in dict(submission.get("verification_nodes") or {}).items():
        node = dict(raw_node)
        dependencies = ", ".join(node.get("depends_on") or [])
        lines.extend([f"### {name}", "", f"- Kind: {node.get('kind', '')}", f"- Combines: {dependencies}", "- Proves:"])
        lines.extend(
            f"  - **{item.get('section', '')}**: {item.get('requirement', '')}"
            for item in list(node.get("covers") or [])
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def architecture_revision_scope(
    base_submission: Mapping[str, Any],
    finding_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Compile a reviewer finding into semantic names and exact writable paths."""

    findings = list(finding_payload.get("findings") or [])
    if not findings and finding_payload:
        findings = [dict(finding_payload)]
    modules = {str(name): dict(value or {}) for name, value in dict(base_submission.get("modules") or {}).items()}
    affected_modules: set[str] = set()
    allowed_paths: set[str] = set()
    finding_kinds: set[str] = set()
    for raw_finding in findings:
        finding = dict(raw_finding or {})
        finding_kinds.add(str(finding.get("finding_kind") or ""))
        affected_modules.update(
            str(item).strip()
            for item in list(finding.get("affected_modules") or [])
            if str(item).strip()
        )
        module_name = str(finding.get("module_name") or "").strip()
        if module_name in modules:
            affected_modules.add(module_name)
        elif module_name in dict(base_submission.get("verification_nodes") or {}):
            verification_node = dict(
                dict(base_submission.get("verification_nodes") or {}).get(module_name) or {}
            )
            affected_modules.update(
                str(item)
                for item in list(verification_node.get("depends_on") or [])
                if str(item) in modules
            )
        locations = [dict(item or {}) for item in list(finding.get("locations") or [])]
        locations.extend(
            {"path": str(path)}
            for path in list(finding.get("suggested_repair_boundary") or [])
            if str(path).strip()
        )
        semantic_reference_error = dict(finding.get("semantic_reference_error") or {})
        semantic_reference = dict(semantic_reference_error.get("reference") or {})
        if semantic_reference:
            locations.append(
                {
                    key: semantic_reference[key]
                    for key in ("path", "symbol", "section")
                    if str(semantic_reference.get(key) or "").strip()
                }
            )
            for module_name, module in modules.items():
                if any(
                    _semantic_reference_matches(
                        dict(item or {}),
                        semantic_reference,
                        error=str(semantic_reference_error.get("error") or ""),
                    )
                    for item in list(module.get("evidence") or [])
                ):
                    affected_modules.add(module_name)
        for raw_location in locations:
            path = _normalized_repo_path(str(dict(raw_location or {}).get("path") or ""))
            if not path:
                continue
            allowed_paths.add(path)
            for module_name, module in modules.items():
                if _module_declares_path(module, path):
                    affected_modules.add(module_name)
    allow_topology_changes = bool(
        finding_kinds.intersection({"architecture_defect", "requirements_defect"})
    )
    unknown_modules = sorted(affected_modules - set(modules))
    if unknown_modules and not allow_topology_changes:
        raise ValueError(
            "architecture finding names unknown modules: " + ", ".join(unknown_modules)
        )
    affected_modules.intersection_update(modules)
    affected_verification_nodes = {
        str(name)
        for name, raw_node in dict(base_submission.get("verification_nodes") or {}).items()
        if affected_modules.intersection(
            {
                *(str(item) for item in list(dict(raw_node or {}).get("depends_on") or [])),
                *(
                    str(dict(item or {}).get("module") or "")
                    for item in list(dict(raw_node or {}).get("consumes") or [])
                ),
            }
        )
    }
    if not affected_modules and not allowed_paths and not allow_topology_changes:
        raise ValueError("architecture finding has no semantic module or source-location scope")
    return {
        "affected_modules": sorted(affected_modules),
        "affected_verification_nodes": sorted(affected_verification_nodes),
        "allowed_paths": sorted(allowed_paths),
        "allow_topology_changes": allow_topology_changes,
    }


def _semantic_reference_matches(
    candidate: Mapping[str, Any],
    reference: Mapping[str, Any],
    *,
    error: str = "",
) -> bool:
    normalized_error = str(error or "").casefold()
    if "path does not exist" in normalized_error or "reference root" in normalized_error:
        # A missing physical source invalidates every claim that uses the same
        # root/path, regardless of the symbol or section each module cites.
        identity_fields = ("kind", "path", "reference_name")
    else:
        identity_fields = (
            "kind",
            "path",
            "reference_name",
            "symbol",
            "source",
            "section",
            "conclusion",
        )
    compared = False
    for field in identity_fields:
        if field not in reference:
            continue
        compared = True
        if str(candidate.get(field) or "").strip() != str(reference.get(field) or "").strip():
            return False
    return compared


def validate_architecture_revision_scope(
    *,
    base_submission: Mapping[str, Any],
    revised_submission: Mapping[str, Any],
    changed_paths: Sequence[str],
    scope: Mapping[str, Any],
) -> None:
    base_modules = {
        str(name): _revision_comparable_contract(dict(value or {}))
        for name, value in dict(base_submission.get("modules") or {}).items()
    }
    revised_modules = {
        str(name): _revision_comparable_contract(dict(value or {}))
        for name, value in dict(revised_submission.get("modules") or {}).items()
    }
    base_module_names = set(base_modules)
    revised_module_names = set(revised_modules)
    added_modules = revised_module_names - base_module_names
    removed_modules = base_module_names - revised_module_names
    updated_modules = {
        name
        for name in base_module_names & revised_module_names
        if base_modules.get(name) != revised_modules.get(name)
    }
    base_verification = {
        str(name): _revision_comparable_contract(dict(value or {}))
        for name, value in dict(base_submission.get("verification_nodes") or {}).items()
    }
    revised_verification = {
        str(name): _revision_comparable_contract(dict(value or {}))
        for name, value in dict(revised_submission.get("verification_nodes") or {}).items()
    }
    base_verification_names = set(base_verification)
    revised_verification_names = set(revised_verification)
    added_verification = revised_verification_names - base_verification_names
    removed_verification = base_verification_names - revised_verification_names
    updated_verification = {
        name
        for name in base_verification_names & revised_verification_names
        if base_verification.get(name) != revised_verification.get(name)
    }
    affected_modules = set(scope.get("affected_modules") or [])
    affected_verification = set(scope.get("affected_verification_nodes") or [])
    allow_topology_changes = bool(scope.get("allow_topology_changes"))
    unexpected_modules = sorted(
        (updated_modules | removed_modules) - affected_modules
    )
    if not allow_topology_changes:
        unexpected_modules.extend(sorted(added_modules))
    unexpected_verification = sorted(
        (updated_verification | removed_verification) - affected_verification
    )
    if not allow_topology_changes:
        unexpected_verification.extend(sorted(added_verification))
    unexpected_paths = sorted(
        path
        for path in set(str(item) for item in changed_paths)
        if not _revision_path_is_allowed(
            path,
            base_modules=base_modules,
            revised_modules=revised_modules,
            added_modules=added_modules,
            removed_modules=removed_modules,
            affected_modules=affected_modules,
            explicit_paths=set(scope.get("allowed_paths") or []),
            allow_topology_changes=allow_topology_changes,
        )
    )
    if unexpected_modules:
        raise ValueError(
            "revision changes modules outside the finding scope: " + ", ".join(unexpected_modules)
        )
    if unexpected_verification:
        raise ValueError(
            "revision changes Verification Nodes outside the finding scope: "
            + ", ".join(unexpected_verification)
        )
    if unexpected_paths:
        raise ValueError(
            "revision changes source paths outside the finding scope: " + ", ".join(unexpected_paths)
        )


def _revision_comparable_contract(value: Mapping[str, Any]) -> dict[str, Any]:
    comparable = json.loads(json.dumps(dict(value)))
    comparable["covers"] = [
        {
            "section": str(dict(item or {}).get("section") or ""),
            "requirement": str(dict(item or {}).get("requirement") or ""),
        }
        for item in list(comparable.get("covers") or [])
    ]
    return comparable


def _revision_path_is_allowed(
    path: str,
    *,
    base_modules: Mapping[str, Any],
    revised_modules: Mapping[str, Any],
    added_modules: set[str],
    removed_modules: set[str],
    affected_modules: set[str],
    explicit_paths: set[str],
    allow_topology_changes: bool,
) -> bool:
    if path in explicit_paths:
        return True
    if not allow_topology_changes:
        return False
    if any(
        _module_declares_writable_skeleton_path(dict(revised_modules[name] or {}), path)
        for name in added_modules
    ):
        return True
    if any(
        _module_declares_writable_skeleton_path(dict(base_modules[name] or {}), path)
        for name in removed_modules & affected_modules
    ):
        return True
    for name in affected_modules & (set(base_modules) & set(revised_modules)):
        before = _module_declares_writable_skeleton_path(dict(base_modules[name] or {}), path)
        after = _module_declares_writable_skeleton_path(dict(revised_modules[name] or {}), path)
        if after and not before:
            return True
    return False


def _module_declares_writable_skeleton_path(module: Mapping[str, Any], path: str) -> bool:
    paths = dict(module.get("paths") or {})
    if path in set(str(item) for item in list(paths.get("contract_paths") or [])):
        return True
    return any(
        PathScope(str(item.get("kind") or ""), str(item.get("path") or "")).matches(path)
        for item in [
            *list(paths.get("implementation_scopes") or []),
            *list(paths.get("test_scopes") or []),
        ]
    )


def _module_declares_path(module: Mapping[str, Any], path: str) -> bool:
    paths = dict(module.get("paths") or {})
    if path in set(str(item) for item in list(paths.get("contract_paths") or [])):
        return True
    if path in set(str(item) for item in list(paths.get("reference_only") or [])):
        return True
    return any(
        PathScope(str(item.get("kind") or ""), str(item.get("path") or "")).matches(path)
        for item in [
            *list(paths.get("implementation_scopes") or []),
            *list(paths.get("test_scopes") or []),
        ]
    )


@dataclass
class GitBackedSkeletonService:
    runtime_root: Path
    artifacts: ContentAddressedArtifactStore

    def provision_architecture_workspace(
        self,
        *,
        workflow_id: str,
        workflow_name: str = "",
        revision_name: str,
        workspace: Mapping[str, Any],
        requirements_ref: ArtifactRef,
        base_artifact: Mapping[str, Any] | None = None,
    ) -> ArchitectureWorkspace:
        stored_layout = dict(dict(base_artifact or {}).get("repository_layout") or {})
        layout = resolve_project_git_layout(
            self.runtime_root,
            workspace=workspace,
            workflow_id=workflow_id,
            workflow_name=workflow_name or workflow_id,
            stored_layout=stored_layout,
        )
        common_git_dir = layout.common_git_dir
        snapshot_marker = layout.workspace_snapshot_marker
        source = str(workspace.get("repo_path") or workspace.get("cwd") or "").strip()
        with project_git_layout_lock(layout):
            if not snapshot_marker.is_file():
                snapshot = self._create_synthetic_snapshot(
                    common_git_dir,
                    Path(source).expanduser() if source else None,
                    snapshot_ref=f"refs/minion/snapshots/{layout.workflow_key}",
                )
                snapshot_ref = self.artifacts.put_json(
                    snapshot,
                    artifact_type="WorkspaceSnapshotArtifact",
                    provenance={"workflow_name": layout.workflow_name},
                    child_refs=((requirements_ref.sha256, "requirements"),),
                )
                snapshot_marker.parent.mkdir(parents=True, exist_ok=True)
                _write_json_atomic(snapshot_marker, snapshot_ref.to_dict())
            else:
                snapshot_ref = ArtifactRef.from_mapping(
                    json.loads(snapshot_marker.read_text(encoding="utf-8"))
                )
                snapshot = self.artifacts.read_json(snapshot_ref)
            base_sha = str(
                dict(base_artifact or {}).get("skeleton_commit_sha")
                or snapshot["snapshot_commit_sha"]
            )
            if base_artifact:
                bundle_value = dict(base_artifact.get("git_bundle_ref") or {})
                if bundle_value:
                    self._import_bundle(common_git_dir, ArtifactRef.from_mapping(bundle_value))
            _git_dir(common_git_dir, "cat-file", "-e", f"{base_sha}^{{commit}}")
            _ensure_git_branch(
                common_git_dir,
                layout.workflow_branch,
                str(snapshot["snapshot_commit_sha"]),
            )
            worktree = layout.architecture_worktree(revision_name)
            branch = layout.architecture_branch(revision_name)
            if not worktree.exists():
                worktree.parent.mkdir(parents=True, exist_ok=True)
                _add_git_worktree(
                    common_git_dir,
                    worktree=worktree,
                    branch=branch,
                    start_sha=base_sha,
                )
        return ArchitectureWorkspace(
            worktree=worktree,
            common_git_dir=common_git_dir,
            base_sha=base_sha,
            base_tree_sha=_git_dir(common_git_dir, "rev-parse", f"{base_sha}^{{tree}}").strip(),
            original_head=str(snapshot.get("original_head") or ""),
            source_fingerprint=str(snapshot.get("source_fingerprint") or ""),
            workspace_snapshot_ref=snapshot_ref,
            project_name=layout.project_name,
            project_key=layout.project_key,
            workflow_name=layout.workflow_name,
            workflow_key=layout.workflow_key,
            workflow_branch=layout.workflow_branch,
            architecture_branch=branch,
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
        revision_base_artifact: Mapping[str, Any] | None = None,
        revision_scope: Mapping[str, Any] | None = None,
        revision_base_path_states: Mapping[str, str] | None = None,
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
        if revision_base_artifact is not None:
            if revision_scope is None:
                raise ValueError("architecture revision snapshot requires an explicit semantic scope")
            scope_changed_paths = (
                architecture_revision_changed_paths_since(
                    architecture_workspace.worktree,
                    architecture_workspace.base_sha,
                    revision_base_path_states,
                )
                if revision_base_path_states is not None
                else changed_paths
            )
            validate_architecture_revision_scope(
                base_submission=dict(revision_base_artifact.get("submission") or {}),
                revised_submission=normalized,
                changed_paths=scope_changed_paths,
                scope=revision_scope,
            )
        validate_architecture_changed_paths(normalized, changed_paths)
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
        declared_contract_paths = {
            str(path)
            for module in dict(normalized.get("modules") or {}).values()
            for path in list(dict(dict(module).get("paths") or {}).get("contract_paths") or [])
        }
        contract_file_hashes = {
            path: _git(architecture_workspace.worktree, "rev-parse", f"{skeleton_sha}:{path}").strip()
            for path in sorted(declared_contract_paths)
        }
        with tempfile.TemporaryDirectory(prefix="pal-skeleton-bundle-") as temporary:
            bundle_path = Path(temporary) / "architecture.bundle"
            bundle_source = architecture_workspace.architecture_branch or "--all"
            _git(
                architecture_workspace.worktree,
                "bundle",
                "create",
                str(bundle_path),
                bundle_source,
            )
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
            "schema_version": "3",
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
            "contract_file_hashes": contract_file_hashes,
            "changed_paths": changed_paths,
            "path_policy": _compiled_path_policy(normalized),
            "original_workspace_head": architecture_workspace.original_head,
            "source_fingerprint": architecture_workspace.source_fingerprint,
            "repository_layout": {
                "project_name": architecture_workspace.project_name,
                "project_key": architecture_workspace.project_key,
                "workflow_name": architecture_workspace.workflow_name,
                "workflow_key": architecture_workspace.workflow_key,
                "workflow_branch": architecture_workspace.workflow_branch,
            },
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
    ) -> ArchitectureReviewWorkspace:
        root = Path(tempfile.mkdtemp(prefix=f"pal-architecture-review-{_safe_component(review_name)}-"))
        stored_layout = dict(artifact.get("repository_layout") or {})
        project_key = _safe_component(str(stored_layout.get("project_key") or ""))
        shared_bare = minion_data_root(self.runtime_root) / "repos" / project_key / "project.git"
        bare = shared_bare
        temporary_common_git_dir = False
        worktree = root / "worktree"
        bundle_ref = ArtifactRef.from_mapping(dict(artifact.get("git_bundle_ref") or {}))
        skeleton_sha = str(artifact.get("skeleton_commit_sha") or "")
        if project_key and bare.is_dir() and not _git_object_exists(bare, skeleton_sha):
            self._import_bundle(bare, bundle_ref)
        if not project_key or not bare.is_dir():
            temporary_common_git_dir = True
            bare = root / "project.git"
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
        completed = subprocess.run(
            ["git", f"--git-dir={bare}", "worktree", "add", "--detach", str(worktree), skeleton_sha],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            shutil.rmtree(root, ignore_errors=True)
            raise RuntimeError(completed.stderr or completed.stdout or "failed to restore architecture review worktree")
        if _git(worktree, "rev-parse", "HEAD").strip() != skeleton_sha:
            shutil.rmtree(root, ignore_errors=True)
            raise RuntimeError("architecture review worktree is not bound to the skeleton commit")
        return ArchitectureReviewWorkspace(
            root=root,
            worktree=worktree,
            common_git_dir=bare,
            temporary_common_git_dir=temporary_common_git_dir,
        )

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

    def _create_synthetic_snapshot(
        self,
        common_git_dir: Path,
        source: Path | None,
        *,
        snapshot_ref: str,
    ) -> dict[str, Any]:
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
            if common_git_dir.exists():
                completed = subprocess.run(
                    [
                        "git",
                        f"--git-dir={common_git_dir}",
                        "fetch",
                        str(seed),
                        f"+{snapshot_sha}:{snapshot_ref}",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
            else:
                completed = subprocess.run(
                    ["git", "clone", "--bare", str(seed), str(common_git_dir)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                if completed.returncode == 0:
                    completed = subprocess.run(
                        ["git", f"--git-dir={common_git_dir}", "update-ref", snapshot_ref, snapshot_sha],
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

def _normalize_module_paths(
    module_name: str,
    value: Any,
    *,
    module_kind: str,
) -> dict[str, Any]:
    paths = dict(value or {}) if isinstance(value, Mapping) else {}
    contracts = [_normalized_repo_path(str(item)) for item in list(paths.get("contract_paths") or [])]
    if not contracts:
        raise ValueError(f"module {module_name} requires paths.contract_paths")
    implementation = _normalize_path_scopes(
        paths.get("implementation_scopes"),
        field=f"{module_name}.implementation_scopes",
        allow_empty=module_kind == "contract_only",
    )
    tests = _normalize_path_scopes(
        paths.get("test_scopes"),
        field=f"{module_name}.test_scopes",
        allow_empty=module_kind == "contract_only",
    )
    if module_kind == "contract_only" and (implementation or tests):
        raise ValueError(
            f"contract_only module {module_name} cannot declare implementation_scopes or test_scopes"
        )
    references = [_normalized_repo_path(str(item)) for item in list(paths.get("reference_only") or [])]
    return {
        "contract_paths": list(dict.fromkeys(contracts)),
        "implementation_scopes": [item.to_dict() for item in implementation],
        "test_scopes": [item.to_dict() for item in tests],
        "reference_only": list(dict.fromkeys(references)),
    }


def _normalize_contract_reference(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("contract consumption references must be objects")
    module = str(value.get("module") or "").strip()
    path = _normalized_repo_path(str(value.get("path") or ""))
    symbol = str(value.get("symbol") or "").strip()
    if not module or not path:
        raise ValueError("contract consumption references require module and path")
    result = {"module": module, "path": path}
    if symbol:
        result["symbol"] = symbol
    return result


def _architecture_requirement_reference(
    value: Mapping[str, Any],
    requirements_payload: Mapping[str, Any],
) -> dict[str, str]:
    resolved = resolve_requirement_reference(value, requirements_payload)
    # Strength remains owned by the immutable RequirementsArtifact. The DAG
    # references only the natural-language Requirement identity.
    return {
        "section": resolved.section,
        "requirement": resolved.requirement,
    }


def _normalize_verification_nodes(
    value: Any,
    *,
    requirements_payload: Mapping[str, Any],
    workspace_root: Path,
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError("Architecture submission requires at least one Verification Node")
    result: dict[str, dict[str, Any]] = {}
    for raw_name, raw_node in value.items():
        name = str(raw_name or "").strip()
        if MODULE_NAME_PATTERN.fullmatch(name) is None:
            raise ValueError(f"invalid semantic Verification Node name: {name or '<empty>'}")
        if not isinstance(raw_node, Mapping):
            raise ValueError(f"Verification Node {name} must be an object")
        node = dict(raw_node)
        kind = str(node.get("kind") or "").strip()
        if kind not in VERIFICATION_KINDS:
            raise ValueError(f"Verification Node {name} has invalid kind: {kind or '<empty>'}")
        depends_on = _unique_text(node.get("depends_on"))
        consumes = [_normalize_contract_reference(item) for item in list(node.get("consumes") or [])]
        if not consumes:
            raise ValueError(f"Verification Node {name} must consume at least one declared contract")
        covers = [
            _architecture_requirement_reference(dict(item or {}), requirements_payload)
            for item in list(node.get("covers") or [])
        ]
        if not covers:
            raise ValueError(f"Verification Node {name} must cover at least one Requirement")
        entrypoints = [
            _normalize_verification_entrypoint(item, node_name=name, workspace_root=workspace_root)
            for item in list(node.get("entrypoints") or [])
        ]
        if not entrypoints:
            raise ValueError(f"Verification Node {name} requires a real entrypoint or build target")
        environment = node.get("environment")
        if not isinstance(environment, Mapping):
            raise ValueError(f"Verification Node {name} environment must be an object")
        result[name] = {
            "kind": kind,
            "depends_on": depends_on,
            "consumes": consumes,
            "covers": covers,
            "entrypoints": entrypoints,
            "environment": dict(environment),
        }
    return result


def _normalize_verification_entrypoint(
    value: Any,
    *,
    node_name: str,
    workspace_root: Path,
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Verification Node {node_name} entrypoints must be objects")
    kind = str(value.get("kind") or "").strip()
    if kind not in VERIFICATION_ENTRYPOINT_KINDS:
        raise ValueError(f"Verification Node {node_name} has invalid entrypoint kind: {kind or '<empty>'}")
    path = _normalized_repo_path(str(value.get("path") or ""))
    symbol = str(value.get("symbol") or "").strip()
    target = str(value.get("target") or "").strip()
    if kind in {"source_symbol", "product_entrypoint"} and not path:
        raise ValueError(f"Verification Node {node_name} {kind} requires path")
    if kind == "source_symbol" and not symbol:
        raise ValueError(f"Verification Node {node_name} source_symbol requires symbol")
    if kind in {"build_target", "platform_probe"} and not target:
        raise ValueError(f"Verification Node {node_name} {kind} requires target")
    if path:
        source = workspace_root / path
        if not source.is_file():
            raise ValueError(f"Verification Node {node_name} entrypoint path does not exist: {path}")
        if symbol:
            content = source.read_text(encoding="utf-8", errors="replace")
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])", content) is None:
                raise ValueError(f"Verification Node {node_name} entrypoint symbol does not exist: {path}::{symbol}")
    result = {"kind": kind}
    if path:
        result["path"] = path
    if symbol:
        result["symbol"] = symbol
    if target:
        result["target"] = target
    return result


def _normalize_path_scopes(
    value: Any,
    *,
    field: str,
    allow_empty: bool = False,
) -> tuple[PathScope, ...]:
    result: list[PathScope] = []
    for raw in list(value or []):
        if isinstance(raw, str):
            raise ValueError(f"{field} path scopes require explicit kind=file|directory")
        if not isinstance(raw, Mapping):
            raise ValueError(f"{field} path scope must be an object")
        kind = str(raw.get("kind") or "").strip()
        path = _normalized_repo_path(str(raw.get("path") or ""))
        if kind not in PATH_SCOPE_KINDS:
            raise ValueError(f"{field} path scope kind must be file or directory")
        if not path or (kind != "file" and "/" not in path):
            raise ValueError(f"{field} must use a file or narrow subdirectory/prefix, never the repository root")
        result.append(PathScope(kind, path.rstrip("/")))
    if not result and not allow_empty:
        raise ValueError(f"{field} requires at least one narrow writable path scope")
    return tuple(dict.fromkeys(result))


def _validate_path_policy(modules: Mapping[str, Mapping[str, Any]]) -> None:
    owners: list[tuple[str, PathScope]] = []
    frozen: dict[str, str] = {}
    references: list[tuple[str, str]] = []
    for name, module in modules.items():
        paths = dict(module["paths"])
        for path in paths["contract_paths"]:
            previous = frozen.setdefault(path, name)
            if previous != name:
                raise ValueError(f"frozen contract path {path} is owned by both {previous} and {name}")
        for value in [*paths["implementation_scopes"], *paths["test_scopes"]]:
            owners.append((name, PathScope(str(value["kind"]), str(value["path"]))))
        references.extend((name, path) for path in paths["reference_only"])
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


def _validate_contract_graph(
    modules: Mapping[str, Mapping[str, Any]],
    verification_nodes: Mapping[str, Mapping[str, Any]],
) -> None:
    contract_paths = {
        name: set(str(item) for item in list(dict(module["paths"])["contract_paths"]))
        for name, module in modules.items()
    }
    consumed: set[tuple[str, str]] = set()
    for consumer_name, module in modules.items():
        for reference in list(module.get("consumes") or []):
            provider = str(reference["module"])
            path = str(reference["path"])
            if provider == consumer_name:
                raise ValueError(f"module {consumer_name} cannot consume its own cross-module contract")
            _validate_contract_reference(provider, path, contract_paths, consumer=f"module {consumer_name}")
            consumed.add((provider, path))
    names = set(modules)
    implementation_names = {
        name
        for name, module in modules.items()
        if str(module.get("module_kind") or "") == "implementation"
    }
    construction_dependencies = {
        name: set(str(item) for item in list(module.get("depends_on") or []))
        for name, module in modules.items()
    }
    for module_name, dependencies in construction_dependencies.items():
        module_kind = str(modules[module_name].get("module_kind") or "")
        if module_kind == "contract_only" and dependencies:
            raise ValueError(
                f"contract_only module {module_name} cannot declare construction dependencies"
            )
        non_candidate_dependencies = sorted(dependencies - implementation_names)
        if non_candidate_dependencies:
            raise ValueError(
                f"module {module_name} construction depends_on may name implementation modules only: "
                + ", ".join(non_candidate_dependencies)
            )
    for node_name, node in verification_nodes.items():
        dependencies = set(str(item) for item in list(node.get("depends_on") or []))
        unknown = sorted(dependencies - names)
        if unknown:
            raise ValueError(
                f"Verification Node {node_name} references unknown implementation modules: {', '.join(unknown)}"
            )
        non_candidate_dependencies = sorted(dependencies - implementation_names)
        if non_candidate_dependencies:
            raise ValueError(
                f"Verification Node {node_name} depends_on may name implementation Candidates only; "
                f"reference contract_only modules through consumes: {', '.join(non_candidate_dependencies)}"
            )
        required_closure = _dependency_closure(dependencies, construction_dependencies)
        missing_dependencies = sorted(required_closure - dependencies)
        if missing_dependencies:
            raise ValueError(
                f"Verification Node {node_name} must list the complete construction dependency closure; "
                f"missing: {', '.join(missing_dependencies)}"
            )
        for reference in list(node.get("consumes") or []):
            provider = str(reference["module"])
            path = str(reference["path"])
            _validate_contract_reference(provider, path, contract_paths, consumer=f"Verification Node {node_name}")
            provider_kind = str(modules[provider].get("module_kind") or "")
            if provider_kind == "implementation" and provider not in dependencies:
                raise ValueError(
                    f"Verification Node {node_name} consumes {provider}:{path} but does not include {provider} in depends_on"
                )
            consumed.add((provider, path))
    unconsumed = sorted(
        f"{module}:{path}"
        for module, paths in contract_paths.items()
        for path in paths
        if (module, path) not in consumed
    )
    if unconsumed:
        raise ValueError(
            "Architecture declares externally observable contracts with no real consumer: " + ", ".join(unconsumed)
        )


def _dependency_closure(
    roots: set[str],
    dependencies: Mapping[str, set[str]],
) -> set[str]:
    closure = set(roots)
    frontier = list(roots)
    while frontier:
        current = frontier.pop()
        for dependency in dependencies.get(current, set()):
            if dependency not in closure:
                closure.add(dependency)
                frontier.append(dependency)
    return closure


def _validate_contract_reference(
    provider: str,
    path: str,
    contract_paths: Mapping[str, set[str]],
    *,
    consumer: str,
) -> None:
    if provider not in contract_paths:
        raise ValueError(f"{consumer} consumes an unknown provider module: {provider}")
    if path not in contract_paths[provider]:
        raise ValueError(f"{consumer} consumes an undeclared contract path: {provider}:{path}")


def _validate_verification_coverage(
    verification_nodes: Mapping[str, Mapping[str, Any]],
    requirements_payload: Mapping[str, Any],
) -> None:
    verified = {
        (_normalized_semantic_text(str(item["section"])), _normalized_semantic_text(str(item["requirement"])))
        for node in verification_nodes.values()
        for item in list(node.get("covers") or [])
    }
    missing = [
        item
        for item in semantic_requirements(requirements_payload)
        if item.strength == "hard"
        and (_normalized_semantic_text(item.section), _normalized_semantic_text(item.requirement)) not in verified
    ]
    if missing:
        raise ValueError(
            "Hard Requirements without a real Verification Node landing: "
            + "; ".join(f"{item.section}: {item.requirement}" for item in missing)
        )


def _validate_declared_paths(
    modules: Mapping[str, Mapping[str, Any]],
    verification_nodes: Mapping[str, Mapping[str, Any]],
    workspace_root: Path,
) -> None:
    for name, module in modules.items():
        paths = dict(module["paths"])
        entrypoint = str(list(paths["contract_paths"])[0])
        target = workspace_root / entrypoint
        if not target.is_file():
            raise ValueError(f"module {name} contract entrypoint does not exist: {entrypoint}")
        missing = contract_comment_missing_sections(target.read_text(encoding="utf-8"), module_name=name)
        if missing:
            raise ValueError(f"module {name} contract entrypoint is incomplete: {', '.join(missing)}")
        for contract_path in list(paths["contract_paths"]):
            if not (workspace_root / contract_path).is_file():
                raise ValueError(f"module {name} contract path does not exist: {contract_path}")
        for raw_scope in [*list(paths["implementation_scopes"]), *list(paths["test_scopes"])]:
            scope = PathScope(str(raw_scope["kind"]), str(raw_scope["path"]))
            declared = workspace_root / scope.path
            if scope.kind == "file" and not declared.is_file():
                raise ValueError(f"module {name} writable file does not exist in the skeleton: {scope.path}")
            if scope.kind == "directory" and not declared.is_dir():
                raise ValueError(f"module {name} writable directory does not exist in the skeleton: {scope.path}")
        for reference in list(paths["reference_only"]):
            if not (workspace_root / reference).exists():
                raise ValueError(f"module {name} reference-only path does not exist: {reference}")
    for consumer_name, module in modules.items():
        for reference in list(module.get("consumes") or []):
            symbol = str(reference.get("symbol") or "")
            if not symbol:
                continue
            target = workspace_root / str(reference["path"])
            content = target.read_text(encoding="utf-8", errors="replace")
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])", content) is None:
                raise ValueError(
                    f"module {consumer_name} consumes an unknown contract symbol: "
                    f"{reference['module']}:{reference['path']}::{symbol}"
                )
    for node_name, node in verification_nodes.items():
        for reference in list(node.get("consumes") or []):
            symbol = str(reference.get("symbol") or "")
            if not symbol:
                continue
            target = workspace_root / str(reference["path"])
            content = target.read_text(encoding="utf-8", errors="replace")
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])", content) is None:
                raise ValueError(
                    f"Verification Node {node_name} consumes an unknown contract symbol: "
                    f"{reference['module']}:{reference['path']}::{symbol}"
                )


def _compiled_path_policy(submission: Mapping[str, Any]) -> dict[str, Any]:
    modules: dict[str, Any] = {}
    for name, raw_module in dict(submission.get("modules") or {}).items():
        paths = dict(dict(raw_module).get("paths") or {})
        modules[name] = {
            "module_kind": str(dict(raw_module).get("module_kind") or ""),
            "contract_paths": list(paths.get("contract_paths") or []),
            "implementation_scopes": list(paths.get("implementation_scopes") or []),
            "test_scopes": list(paths.get("test_scopes") or []),
            "reference_only": list(paths.get("reference_only") or []),
        }
    return {"modules": modules}


def validate_architecture_changed_paths(
    submission: Mapping[str, Any],
    changed_paths: Sequence[str],
) -> tuple[str, ...]:
    normalized_paths = tuple(
        sorted({_normalized_repo_path(str(path)) for path in changed_paths if str(path).strip()})
    )
    undeclared = [
        path for path in normalized_paths if not _architect_path_is_declared(path, submission)
    ]
    if undeclared:
        raise ValueError(
            "Architect changed paths outside declared contract/implementation/test skeleton scopes: "
            + ", ".join(undeclared)
        )
    return normalized_paths


def _architect_path_is_declared(path: str, submission: Mapping[str, Any]) -> bool:
    for module in dict(submission.get("modules") or {}).values():
        paths = dict(dict(module).get("paths") or {})
        if path in set(str(item) for item in list(paths.get("contract_paths") or [])):
            return True
        for raw_scope in [
            *list(paths.get("implementation_scopes") or []),
            *list(paths.get("test_scopes") or []),
        ]:
            if PathScope(str(raw_scope["kind"]), str(raw_scope["path"])).matches(path):
                return True
    return False


def _path_scopes_overlap(left: PathScope, right: PathScope) -> bool:
    probes = {left.path, right.path}
    if left.kind == "directory":
        probes.add(left.path + "/__pal_probe__")
    if right.kind == "directory":
        probes.add(right.path + "/__pal_probe__")
    return any(left.matches(path) and right.matches(path) for path in probes)


def _format_contract_reference(value: Mapping[str, Any]) -> str:
    symbol = str(value.get("symbol") or "")
    suffix = f"::{symbol}" if symbol else ""
    return f"`{value.get('module', '')}:{value.get('path', '')}{suffix}`"


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


def architecture_revision_path_states(worktree: Path, base_sha: str) -> dict[str, str]:
    """Fingerprint only paths whose working-tree state differs from the Git base."""

    states: dict[str, str] = {}
    for relative in _git_changed_paths(worktree, base_sha):
        path = worktree / relative
        if not path.exists() and not path.is_symlink():
            states[relative] = "absent"
            continue
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            payload = os.readlink(path).encode("utf-8", errors="surrogateescape")
            kind = "symlink"
        elif stat.S_ISREG(mode):
            payload = path.read_bytes()
            kind = "file"
        else:
            payload = b""
            kind = "other"
        states[relative] = (
            f"{kind}:{mode & 0o777:o}:{hashlib.sha256(payload).hexdigest()}"
        )
    return states


def architecture_revision_changed_paths_since(
    worktree: Path,
    base_sha: str,
    baseline_states: Mapping[str, str],
) -> list[str]:
    """Return the local delta since a rejected, stable architecture candidate."""

    current = architecture_revision_path_states(worktree, base_sha)
    baseline = {str(path): str(value) for path, value in baseline_states.items()}
    return sorted(
        path
        for path in set(current) | set(baseline)
        if current.get(path) != baseline.get(path)
    )


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


def _git_object_exists(git_dir: Path, object_name: str) -> bool:
    if not object_name or not git_dir.is_dir():
        return False
    completed = subprocess.run(
        ["git", f"--git-dir={git_dir}", "cat-file", "-e", f"{object_name}^{{commit}}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def _git_branch_exists(git_dir: Path, branch: str) -> bool:
    completed = subprocess.run(
        ["git", f"--git-dir={git_dir}", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def _ensure_git_branch(git_dir: Path, branch: str, start_sha: str) -> None:
    if _git_branch_exists(git_dir, branch):
        return
    _git_dir(git_dir, "branch", branch, start_sha)


def _add_git_worktree(
    git_dir: Path,
    *,
    worktree: Path,
    branch: str,
    start_sha: str,
) -> None:
    if _git_branch_exists(git_dir, branch):
        command = ["git", f"--git-dir={git_dir}", "worktree", "add", str(worktree), branch]
    else:
        command = [
            "git",
            f"--git-dir={git_dir}",
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree),
            start_sha,
        ]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or f"failed to create Git worktree {worktree}")


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
