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
from pal.minion.v2.module_protocol import ModuleDefinition
from pal.minion.v2.task_ledger import validate_task_ledger


ARCHITECTURE_SKELETON_ARTIFACT = "ArchitectureSkeletonArtifact"
ARCHITECTURE_SKELETON_BUNDLE_ARTIFACT = "ArchitectureSkeletonGitBundleArtifact"
ARCHITECTURE_SUBMISSION_ARTIFACT = "ArchitectureSkeletonSubmissionArtifact"
ARCHITECTURE_VALIDATION_REPORT_ARTIFACT = "ArchitectureValidationReportArtifact"
ARCHITECTURE_REPAIR_BASELINE_ARTIFACT = "ArchitectureSkeletonRepairBaselineArtifact"
SKELETON_MODULE_CONTRACT_ARTIFACT = "SkeletonModuleContractArtifact"

MODULE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,79}$")
PATH_SCOPE_KINDS = frozenset({"file", "directory"})
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
class ValidationIssue:
    severity: str
    code: str
    message: str
    subject_kind: str = "architecture"
    subject_name: str = ""
    location: Mapping[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "subject_kind": self.subject_kind,
            **({"subject_name": self.subject_name} if self.subject_name else {}),
            **({"location": dict(self.location)} if self.location else {}),
        }


class ArchitectureValidationError(ValueError):
    """Machine-readable rejection raised for an invalid architecture draft."""

    def __init__(self, issues: Sequence[ValidationIssue]) -> None:
        self.issues = tuple(issues)
        messages = list(
            dict.fromkeys(
                str(item.message).strip()
                for item in self.issues
                if str(item.message).strip()
            )
        )
        if len(messages) == 1:
            message = messages[0]
        else:
            message = (
                f"Architecture submission has {len(messages)} consistent errors:\n"
                + "\n".join(f"- {item}" for item in messages)
            )
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": str(self),
            "error_type": self.__class__.__name__,
            "errors": [item.to_dict() for item in self.issues],
        }


@dataclass(frozen=True)
class ArchitectureValidationResult:
    normalized_submission: Mapping[str, Any]
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(item for item in self.issues if item.severity == "error")

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(item for item in self.issues if item.severity == "warning")

    def raise_for_errors(self) -> None:
        if self.errors:
            raise ArchitectureValidationError(self.errors)

    def report_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "status": "invalid" if self.errors else ("warnings" if self.warnings else "valid"),
            "errors": [item.to_dict() for item in self.errors],
            "warnings": [item.to_dict() for item in self.warnings],
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


def compiled_module_write_scopes(path_policy: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    """Compile the single path policy into the module Coder's OS write scopes."""

    policy = dict(path_policy or {})
    scopes: list[dict[str, str]] = []
    if str(policy.get("contract_mode") or "review_guarded") == "review_guarded":
        scopes.extend(
            {"kind": "file", "path": _normalized_repo_path(str(path))}
            for path in list(policy.get("contract_paths") or [])
        )
    scopes.extend(
        {
            "kind": str(dict(raw or {}).get("kind") or ""),
            "path": _normalized_repo_path(str(dict(raw or {}).get("path") or "")),
        }
        for raw in list(policy.get("implementation_scopes") or [])
    )
    developer_tests = dict(policy.get("developer_tests") or {})
    if developer_tests:
        scopes.append(
            {
                "kind": str(developer_tests.get("kind") or ""),
                "path": _normalized_repo_path(str(developer_tests.get("path") or "")),
            }
        )
    return tuple(
        {"kind": kind, "path": path}
        for kind, path in dict.fromkeys((item["kind"], item["path"]) for item in scopes)
        if kind and path
    )


def _module_test_root(module_name: str) -> str:
    normalized_name = str(module_name or "").strip()
    if MODULE_NAME_PATTERN.fullmatch(normalized_name) is None:
        raise ValueError(
            f"invalid semantic module name: {normalized_name or '<empty>'}"
        )
    return f"tests/{normalized_name}"


def module_developer_test_path(module_name: str) -> str:
    """Return the module Coder-owned durable test corpus path."""

    return f"{_module_test_root(module_name)}/developer"


def module_verification_corpus_path(module_name: str) -> str:
    """Return the module Verifier-owned durable test corpus path."""

    return f"{_module_test_root(module_name)}/verification"


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
    finding_key: str
    finding_kind: str
    priority: str
    summary: str
    locations: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_key": self.finding_key,
            "finding_kind": self.finding_kind,
            "priority": self.priority,
            "summary": self.summary,
            "locations": [dict(item) for item in self.locations],
        }


@dataclass(frozen=True)
class SkeletonReviewResult:
    verdict: str
    findings: tuple[SkeletonReviewFinding, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "findings": [item.to_dict() for item in self.findings],
        }


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


def _semantic_reference_warning(
    error: SemanticReferenceError,
    *,
    code: str,
    subject_kind: str,
    subject_name: str,
) -> ValidationIssue:
    reference = dict(error.reference)
    location = {
        key: str(reference[key])
        for key in ("path", "symbol", "section")
        if str(reference.get(key) or "").strip()
    }
    candidates = list(error.possible_matches)
    suffix = (
        " Suggested matches: "
        + "; ".join(
            ": ".join(
                str(item.get(key) or "")
                for key in ("section", "requirement", "path", "symbol")
                if str(item.get(key) or "").strip()
            )
            for item in candidates[:5]
        )
        if candidates
        else ""
    )
    return ValidationIssue(
        "warning",
        code,
        str(error) + suffix,
        subject_kind=subject_kind,
        subject_name=subject_name,
        location=location or None,
    )


def analyze_architecture_submission(
    submission: Mapping[str, Any],
    *,
    requirements_payload: Mapping[str, Any],
    workspace_root: Path,
    reference_roots: Mapping[str, Path] | None = None,
    evidence_catalog: Mapping[str, Any] | None = None,
) -> ArchitectureValidationResult:
    del requirements_payload, reference_roots, evidence_catalog
    errors: list[Any] = []
    raw_requirements = submission.get("requirements")
    if not isinstance(raw_requirements, Mapping) or not raw_requirements:
        errors.append("Architecture submission requires a non-empty requirements map")
        raw_requirements = {}
    requirements: dict[str, dict[str, Any]] = {}
    for raw_name, raw_requirement in raw_requirements.items():
        name = str(raw_name or "").strip()
        if MODULE_NAME_PATTERN.fullmatch(name) is None:
            errors.append(f"invalid semantic requirement name: {name or '<empty>'}")
            continue
        if not isinstance(raw_requirement, Mapping):
            errors.append(f"requirement {name} must be an object")
            continue
        try:
            requirements[name] = _normalize_architecture_requirement(name, raw_requirement)
        except ValueError as exc:
            errors.append(exc)
    raw_modules = submission.get("modules")
    if not isinstance(raw_modules, Mapping) or not raw_modules:
        errors.append("Architecture submission requires a non-empty modules map")
        raw_modules = {}
    modules: dict[str, dict[str, Any]] = {}
    invalid_module_names: set[str] = set()
    for raw_name, raw_module in raw_modules.items():
        name = str(raw_name or "").strip()
        if MODULE_NAME_PATTERN.fullmatch(name) is None:
            errors.append(f"invalid semantic module name: {name or '<empty>'}")
            continue
        if not isinstance(raw_module, Mapping):
            errors.append(f"module {name} must be an object")
            invalid_module_names.add(name)
            continue
        try:
            modules[name] = validate_architecture_module_definition(
                name,
                dict(raw_module),
            )
        except ValueError as exc:
            errors.append(exc)
            invalid_module_names.add(name)
    declared_module_names = {
        str(name or "").strip()
        for name in raw_modules
        if MODULE_NAME_PATTERN.fullmatch(str(name or "").strip()) is not None
    }
    unknown = sorted(
        {
            dependency
            for module in modules.values()
            for dependency in dict(module["dependencies"])
            if dependency not in declared_module_names
        }
    )
    if unknown:
        errors.append("Architecture DAG references unknown modules: " + ", ".join(unknown))
    invalid_dependencies = sorted(
        {
            dependency
            for module in modules.values()
            for dependency in dict(module["dependencies"])
            if dependency in invalid_module_names
        }
    )
    if invalid_dependencies:
        errors.append(
            "Architecture DAG references modules whose definitions are invalid: "
            + ", ".join(invalid_dependencies)
        )
    if not unknown and not invalid_dependencies:
        try:
            _validate_construction_graph(modules)
        except ValueError as exc:
            errors.append(exc)
        for consumer_name, module in modules.items():
            for provider_name, raw_dependency in dict(module["dependencies"]).items():
                if provider_name not in modules:
                    continue
                provider_outputs = set(
                    dict(dict(modules[provider_name].get("contract") or {}).get("outputs") or {})
                )
                consumed_outputs = set(dict(raw_dependency or {}).get("consumes") or [])
                unavailable = sorted(consumed_outputs - provider_outputs)
                if unavailable:
                    errors.append(
                        f"module {consumer_name} consumes unknown outputs from {provider_name}: "
                        + ", ".join(unavailable)
                    )
    raw_scenarios = submission.get("scenarios")
    if not isinstance(raw_scenarios, Mapping) or not raw_scenarios:
        errors.append("Architecture submission requires at least one end-to-end scenario")
        raw_scenarios = {}
    scenarios: dict[str, dict[str, Any]] = {}
    implementation_names = {
        name
        for name, module in modules.items()
        if str(module.get("module_kind") or "") == "implementation"
    }
    for raw_name, raw_scenario in raw_scenarios.items():
        name = str(raw_name or "").strip()
        if MODULE_NAME_PATTERN.fullmatch(name) is None:
            errors.append(f"invalid semantic scenario name: {name or '<empty>'}")
            continue
        if not isinstance(raw_scenario, Mapping):
            errors.append(f"scenario {name} must be an object")
            continue
        if name in modules:
            errors.append(
                f"scenario {name} conflicts with a module name; semantic node names must be unique"
            )
            continue
        try:
            scenario = _normalize_architecture_scenario(name, raw_scenario)
        except ValueError as exc:
            errors.append(exc)
            continue
        unknown_modules = sorted(set(scenario["modules"]) - implementation_names)
        if unknown_modules:
            errors.append(
                f"scenario {name} references unknown or non-implementation modules: "
                + ", ".join(unknown_modules)
            )
        else:
            selected = set(scenario["modules"])
            required = set(selected)
            pending = list(selected)
            while pending:
                module_name = pending.pop()
                for dependency in dict(modules[module_name]["dependencies"]):
                    if dependency not in implementation_names or dependency in required:
                        continue
                    required.add(dependency)
                    pending.append(dependency)
            missing = sorted(required - selected)
            if missing:
                errors.append(
                    f"scenario {name} omits implementation contract dependencies: "
                    + ", ".join(missing)
                )
        scenarios[name] = scenario
    declared_requirements = set(requirements)
    referenced_requirements: set[str] = set()
    for scenario_name, scenario in scenarios.items():
        scenario_requirements = set(scenario["requirement_refs"])
        referenced_requirements.update(scenario_requirements)
        unknown_requirements = sorted(scenario_requirements - declared_requirements)
        if unknown_requirements:
            errors.append(
                f"scenario {scenario_name} references unknown requirements: "
                + ", ".join(unknown_requirements)
            )
    unreferenced_requirements = sorted(declared_requirements - referenced_requirements)
    if unreferenced_requirements:
        errors.append(
            "Architecture requirements must be consumed by at least one scenario: "
            + ", ".join(unreferenced_requirements)
        )
    declared_owners = set(modules) | set(scenarios)
    requirement_name_conflicts = sorted(set(requirements) & declared_owners)
    if requirement_name_conflicts:
        errors.append(
            "requirement names must not conflict with module or scenario names: "
            + ", ".join(requirement_name_conflicts)
        )
    for requirement_name, requirement in requirements.items():
        owner = str(requirement.get("owner") or "")
        if owner not in declared_owners:
            errors.append(
                f"requirement {requirement_name} owner references an unknown module or scenario: {owner}"
            )
    validators = (
        lambda: _validate_path_policy(modules),
        lambda: _validate_declared_paths(modules, Path(workspace_root)),
    )
    for validator in validators:
        try:
            validator()
        except ValueError as exc:
            errors.append(exc)
    issues = tuple(
        ValidationIssue("error", "architecture_structure", str(item))
        for item in _unique_architecture_errors(errors)
    )
    return ArchitectureValidationResult(
        {"requirements": requirements, "modules": modules, "scenarios": scenarios},
        issues,
    )


def validate_architecture_submission(
    submission: Mapping[str, Any],
    *,
    requirements_payload: Mapping[str, Any],
    workspace_root: Path,
    reference_roots: Mapping[str, Path] | None = None,
    evidence_catalog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = analyze_architecture_submission(
        submission,
        requirements_payload=requirements_payload,
        workspace_root=workspace_root,
        reference_roots=reference_roots,
        evidence_catalog=evidence_catalog,
    )
    result.raise_for_errors()
    return dict(result.normalized_submission)


def validate_architecture_module_definition(
    name: str,
    module: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and normalize one complete semantic module definition."""

    normalized_name = str(name or "").strip()
    if MODULE_NAME_PATTERN.fullmatch(normalized_name) is None:
        raise ValueError(
            f"invalid semantic module name: {normalized_name or '<empty>'}"
        )
    semantic_payload = dict(module)
    raw_paths = semantic_payload.pop("paths", None)
    try:
        semantic = ModuleDefinition.model_validate(
            semantic_payload,
            strict=True,
        ).model_dump(mode="python")
    except ValueError as exc:
        raise ValueError(f"module {normalized_name} definition is invalid: {exc}") from exc
    module_kind = str(semantic["module_kind"])
    paths = _normalize_module_paths(
        normalized_name,
        raw_paths,
        module_kind=module_kind,
    )
    return {**semantic, "paths": paths}


def _normalize_architecture_scenario(
    name: str,
    scenario: Mapping[str, Any],
) -> dict[str, Any]:
    modules = _unique_text(scenario.get("modules"))
    if not modules:
        raise ValueError(f"scenario {name} requires at least one implementation module")
    result = {
        "modules": modules,
        "requirement_refs": _unique_text(scenario.get("requirement_refs")),
        "entrypoint": str(scenario.get("entrypoint") or "").strip(),
        "contract_flow": _unique_text(scenario.get("contract_flow")),
        "observable_behavior": str(scenario.get("observable_behavior") or "").strip(),
        "failure_behavior": str(scenario.get("failure_behavior") or "").strip(),
        "environment": str(scenario.get("environment") or "").strip(),
    }
    for field in ("entrypoint", "observable_behavior", "failure_behavior", "environment"):
        if not result[field]:
            raise ValueError(f"scenario {name} requires {field}")
    if not result["requirement_refs"]:
        raise ValueError(f"scenario {name} requires at least one requirement reference")
    if not result["contract_flow"]:
        raise ValueError(f"scenario {name} requires at least one contract flow step")
    return result


def _normalize_architecture_requirement(
    name: str,
    requirement: Mapping[str, Any],
) -> dict[str, Any]:
    claim = str(requirement.get("claim") or "").strip()
    owner = str(requirement.get("owner") or "").strip()
    contract_path = _unique_text(requirement.get("contract_path"))
    for field, value in {"claim": claim, "owner": owner}.items():
        if not value:
            raise ValueError(f"requirement {name} requires {field}")
    if MODULE_NAME_PATTERN.fullmatch(owner) is None:
        raise ValueError(f"requirement {name} owner must be a stable snake_case semantic name")
    if not contract_path:
        raise ValueError(f"requirement {name} requires a non-empty contract_path")
    bare_files = [
        value
        for value in contract_path
        if ("/" in value or value.endswith((".h", ".hpp", ".py")))
        and not any(marker in value for marker in ("#", "::", "->"))
    ]
    if bare_files:
        raise ValueError(
            f"requirement {name} contract_path must name public interfaces/signals, not bare files: "
            + ", ".join(bare_files)
        )
    return {
        "claim": claim,
        "owner": owner,
        "contract_path": contract_path,
    }


def _unique_architecture_errors(errors: Iterable[Any]) -> tuple[Any, ...]:
    unique_by_message: dict[str, Any] = {}
    for item in errors:
        message = str(item).strip()
        if message:
            unique_by_message.setdefault(message, item)
    return tuple(unique_by_message.values())


def _raise_architecture_errors(errors: Iterable[Any]) -> None:
    unique_values = _unique_architecture_errors(errors)
    unique = [(str(item).strip(), item) for item in unique_values]
    if not unique:
        return
    if len(unique) == 1:
        message, original = unique[0]
        if isinstance(original, ValueError):
            raise original
        raise ValueError(message)
    raise ValueError(
        f"Architecture submission has {len(unique)} consistent errors:\n"
        + "\n".join(f"- {message}" for message, _original in unique)
    )


def review_architecture_skeleton(
    artifact: Mapping[str, Any],
    *,
    worktree: Path,
    requirements_payload: Mapping[str, Any],
) -> SkeletonReviewResult:
    del requirements_payload
    submission = dict(artifact.get("submission") or {})
    findings: list[SkeletonReviewFinding] = []
    for name, raw_module in dict(submission.get("modules") or {}).items():
        module = dict(raw_module)
        for contract_path in list(dict(module.get("paths") or {}).get("contract_paths") or []):
            path = Path(worktree) / str(contract_path)
            try:
                path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                findings.append(
                    SkeletonReviewFinding(
                        finding_key=f"unreadable_contract_{str(name)}",
                        finding_kind="contract_defect",
                        priority="p0",
                        summary="A frozen contract path is not readable UTF-8 source.",
                        locations=({"scope": "workspace", "file": str(contract_path), "line": 1},),
                    )
                )
    return SkeletonReviewResult("PASS" if not findings else "FAIL", tuple(findings))


def compile_skeleton_markdown(
    artifact: Mapping[str, Any],
    *,
    requirements_payload: Mapping[str, Any],
) -> str:
    submission = dict(artifact.get("submission") or {})
    task_ledger = validate_task_ledger(requirements_payload)
    lines = ["# Architecture Skeleton", "", "## Task Ledger", ""]
    lines.append("- `task.yaml`: original plus ordered append-only revisions")
    lines.append(f"- Revisions: {len(list(task_ledger.get('revisions') or []))}")
    for raw in list(task_ledger.get("revisions") or []):
        item = dict(raw or {})
        authority = dict(item.get("authority") or {})
        lines.append(
            f"  - {item.get('sequence')}: {item.get('summary', '')} "
            f"({authority.get('origin', 'user')}, {authority.get('observed_at', '')})"
        )
    lines.extend(["", "## Requirement Mapping", ""])
    for name, raw_requirement in dict(submission.get("requirements") or {}).items():
        requirement = dict(raw_requirement or {})
        lines.extend(
            [
                f"### {name}",
                "",
                f"- Claim: {requirement.get('claim', '')}",
                f"- Owner: {requirement.get('owner', '')}",
                f"- Contract path: {' -> '.join(str(item) for item in list(requirement.get('contract_path') or []))}",
                "",
            ]
        )
    lines.extend(["", "## Module Protocol", ""])
    for name, raw_module in dict(submission.get("modules") or {}).items():
        module = dict(raw_module)
        paths = dict(module.get("paths") or {})
        dependencies = dict(module.get("dependencies") or {})
        contract = dict(module.get("contract") or {})
        lifecycle = dict(module.get("lifecycle") or {})
        state_machine = dict(module.get("state_machine") or {})
        implementation_scope = ", ".join(
            f"`{dict(item).get('path', '')}`"
            for item in list(paths.get("implementation_scopes") or [])
        ) or "none"
        lines.extend(
            [
                f"### {name}",
                "",
                f"- Module kind: {module.get('module_kind', '')}",
                f"- Behavior kind: {module.get('behavior_kind', '')}",
                f"- Responsibility: {module.get('responsibility', '')}",
                f"- Dependencies: {', '.join(dependencies) or 'none'}",
                f"- Contract inputs: {', '.join(dict(contract.get('inputs') or {})) or 'none'}",
                f"- Contract outputs: {', '.join(dict(contract.get('outputs') or {})) or 'none'}",
                f"- Errors: {'; '.join(str(item) for item in list(contract.get('errors') or [])) or 'none'}",
                f"- Invariants: {'; '.join(str(item) for item in list(contract.get('invariants') or [])) or 'none'}",
                f"- Ownership: {'; '.join(str(item) for item in list(module.get('ownership') or []))}",
                f"- Lifecycle: creation={lifecycle.get('creation', '')}; operation={lifecycle.get('operation', '')}; shutdown={lifecycle.get('shutdown', '')}; failure={lifecycle.get('failure', '')}; cleanup={lifecycle.get('cleanup', '')}",
                f"- State machine: initial={state_machine.get('initial', 'none')}; states={', '.join(dict(state_machine.get('states') or {})) or 'none'}",
                f"- Contract enforcement: {paths.get('contract_mode', 'review_guarded')}",
                f"- Contracts: {', '.join(f'`{item}`' for item in list(paths.get('contract_paths') or []))}",
                f"- Implementation scope: {implementation_scope}",
                f"- Developer tests: `{module_developer_test_path(str(name))}/`",
                f"- Verification corpus: `{module_verification_corpus_path(str(name))}/`",
                f"- Reference only: {', '.join(f'`{item}`' for item in list(paths.get('reference_only') or [])) or 'none'}",
            ]
        )
        for provider, raw_dependency in dependencies.items():
            dependency = dict(raw_dependency or {})
            lines.append(
                f"  - `{provider}`: consumes {', '.join(str(item) for item in list(dependency.get('consumes') or []))}; purpose={dependency.get('purpose', '')}; handoff={dependency.get('handoff', '')}"
            )
        lines.append("")
    lines.extend(["## End-to-End Scenarios", ""])
    for name, raw_scenario in dict(submission.get("scenarios") or {}).items():
        scenario = dict(raw_scenario or {})
        lines.extend(
            [
                f"### {name}",
                "",
                f"- Modules: {', '.join(str(item) for item in list(scenario.get('modules') or []))}",
                f"- Requirements: {', '.join(str(item) for item in list(scenario.get('requirement_refs') or []))}",
                f"- Entrypoint: {scenario.get('entrypoint', '')}",
                f"- Contract flow: {' -> '.join(str(item) for item in list(scenario.get('contract_flow') or []))}",
                f"- Observable behavior: {scenario.get('observable_behavior', '')}",
                f"- Failure behavior: {scenario.get('failure_behavior', '')}",
                f"- Environment: {scenario.get('environment', '')}",
                "",
            ]
        )
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
    immutable_requirement_paths: set[str] = set()
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
        locations = [dict(item or {}) for item in list(finding.get("locations") or [])]
        locations.extend(
            {"path": str(path)}
            for path in list(finding.get("suggested_repair_boundary") or [])
            if str(path).strip()
        )
        for raw_location in locations:
            location = dict(raw_location or {})
            path = _normalized_repo_path(
                str(
                    location.get("file")
                    or location.get("path")
                    or ""
                )
            )
            if not path:
                continue
            if str(location.get("scope") or "workspace") == "task_ledger":
                immutable_requirement_paths.add(path)
                continue
            allowed_paths.add(path)
            for module_name, module in modules.items():
                if _module_declares_path(module, path):
                    affected_modules.add(module_name)
    allow_topology_changes = bool(
        finding_kinds.intersection({"architecture_defect", "requirements_defect"})
    )
    if allow_topology_changes and not allowed_paths:
        affected_modules.update(modules)
        allowed_paths.update(
            path for module in modules.values() for path in _module_declared_paths(module)
        )
    unknown_modules = sorted(affected_modules - set(modules))
    if unknown_modules and not allow_topology_changes:
        raise ValueError(
            "architecture finding names unknown modules: " + ", ".join(unknown_modules)
        )
    affected_modules.intersection_update(modules)
    if not affected_modules and not allowed_paths and not allow_topology_changes:
        raise ValueError("architecture finding has no semantic module or source-location scope")
    return {
        "affected_modules": sorted(affected_modules),
        "allowed_paths": sorted(allowed_paths),
        "immutable_requirement_paths": sorted(immutable_requirement_paths),
        "allow_topology_changes": allow_topology_changes,
    }


def _module_declared_paths(module: Mapping[str, Any]) -> set[str]:
    paths = dict(module.get("paths") or {})
    result = {
        str(item)
        for item in [
            *list(paths.get("contract_paths") or []),
            *list(paths.get("reference_only") or []),
        ]
    }
    result.update(
        str(dict(item or {}).get("path") or "")
        for item in [
            *list(paths.get("implementation_scopes") or []),
        ]
        if str(dict(item or {}).get("path") or "")
    )
    return result


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
    affected_modules = set(scope.get("affected_modules") or [])
    allow_topology_changes = bool(scope.get("allow_topology_changes"))
    unexpected_modules = sorted(
        (updated_modules | removed_modules) - affected_modules
    )
    if not allow_topology_changes:
        unexpected_modules.extend(sorted(added_modules))
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
    if unexpected_paths:
        raise ValueError(
            "revision changes source paths outside the finding scope: " + ", ".join(unexpected_paths)
        )


def _revision_comparable_contract(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(dict(value)))


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

    def snapshot_architect_result(
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
        submitted = dict(submission)
        requirements = validate_task_ledger(self.artifacts.read_json(requirements_ref))
        evidence_catalog = self.artifacts.read_json(evidence_catalog_ref) if evidence_catalog_ref else None
        validation = analyze_architecture_submission(
            submitted,
            requirements_payload=requirements,
            workspace_root=architecture_workspace.worktree,
            reference_roots=reference_roots,
            evidence_catalog=evidence_catalog,
        )
        validation.raise_for_errors()
        normalized = dict(validation.normalized_submission)
        changed_paths = _git_changed_paths(architecture_workspace.worktree, architecture_workspace.base_sha)
        architect_authored_paths = changed_paths
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
            architect_authored_paths = scope_changed_paths
        validate_architecture_changed_paths(normalized, architect_authored_paths)
        _git(architecture_workspace.worktree, "add", "-A")
        submission_hash = _stable_hash(normalized)
        commit_key = hashlib.sha256(
            f"{architecture_workspace.base_sha}\0{submission_hash}\0{requirements_ref.sha256}\0{workflow_name}\0{revision_name}".encode("utf-8")
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
        validation_report_ref = self.artifacts.put_json(
            validation.report_dict(),
            artifact_type=ARCHITECTURE_VALIDATION_REPORT_ARTIFACT,
            provenance={"workflow_name": workflow_name, "revision_name": revision_name},
            child_refs=((submission_ref.sha256, "architecture_submission"),),
        )
        payload = {
            "schema_version": "3",
            "requirements_ref": requirements_ref.to_dict(),
            "evidence_catalog_ref": evidence_catalog_ref.to_dict() if evidence_catalog_ref else {},
            "submission": normalized,
            "submission_ref": submission_ref.to_dict(),
            "validation_report_ref": validation_report_ref.to_dict(),
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
            (validation_report_ref.sha256, "validation_report"),
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
        raw_project_key = str(stored_layout.get("project_key") or "").strip()
        project_key = _safe_component(raw_project_key) if raw_project_key else ""
        bare = root / "project.git"
        temporary_common_git_dir = False
        worktree = root / "worktree"
        bundle_ref = ArtifactRef.from_mapping(dict(artifact.get("git_bundle_ref") or {}))
        skeleton_sha = str(artifact.get("skeleton_commit_sha") or "")
        try:
            if project_key:
                layout = resolve_project_git_layout(
                    self.runtime_root,
                    workspace={},
                    workflow_id="",
                    workflow_name=str(stored_layout.get("workflow_name") or review_name),
                    stored_layout=stored_layout,
                )
                bare = layout.common_git_dir
                with project_git_layout_lock(layout):
                    if not bare.is_dir():
                        self._restore_bundle_repository(bare, bundle_ref, staging_root=root)
                    elif not _git_object_exists(bare, skeleton_sha):
                        self._import_bundle(bare, bundle_ref)
                    self._add_review_worktree(bare, worktree=worktree, skeleton_sha=skeleton_sha)
            else:
                temporary_common_git_dir = True
                self._restore_bundle_repository(bare, bundle_ref, staging_root=root)
                self._add_review_worktree(bare, worktree=worktree, skeleton_sha=skeleton_sha)
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise
        if _git(worktree, "rev-parse", "HEAD").strip() != skeleton_sha:
            shutil.rmtree(root, ignore_errors=True)
            raise RuntimeError("architecture review worktree is not bound to the skeleton commit")
        return ArchitectureReviewWorkspace(
            root=root,
            worktree=worktree,
            common_git_dir=bare,
            temporary_common_git_dir=temporary_common_git_dir,
        )

    def _restore_bundle_repository(
        self,
        common_git_dir: Path,
        bundle_ref: ArtifactRef,
        *,
        staging_root: Path,
    ) -> None:
        common_git_dir.parent.mkdir(parents=True, exist_ok=True)
        bundle = staging_root / "architecture.bundle"
        self.materialize_bundle(bundle_ref, bundle)
        completed = subprocess.run(
            ["git", "clone", "--bare", str(bundle), str(common_git_dir)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        bundle.unlink(missing_ok=True)
        if completed.returncode != 0:
            shutil.rmtree(common_git_dir, ignore_errors=True)
            raise RuntimeError(
                completed.stderr
                or completed.stdout
                or "failed to restore architecture review repository"
            )

    @staticmethod
    def _add_review_worktree(
        common_git_dir: Path,
        *,
        worktree: Path,
        skeleton_sha: str,
    ) -> None:
        completed = subprocess.run(
            [
                "git",
                f"--git-dir={common_git_dir}",
                "worktree",
                "add",
                "--detach",
                str(worktree),
                skeleton_sha,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                completed.stderr
                or completed.stdout
                or "failed to restore architecture review worktree"
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
    default_contract_mode = "file_frozen" if module_kind == "contract_only" else "review_guarded"
    contract_mode = str(paths.get("contract_mode") or default_contract_mode).strip()
    if contract_mode not in {"file_frozen", "review_guarded"}:
        raise ValueError(
            f"module {module_name} paths.contract_mode must be file_frozen or review_guarded"
        )
    if module_kind == "contract_only" and contract_mode != "file_frozen":
        raise ValueError(
            f"contract_only module {module_name} must use file_frozen contract mode"
        )
    contracts = [_normalized_repo_path(str(item)) for item in list(paths.get("contract_paths") or [])]
    if not contracts:
        raise ValueError(f"module {module_name} requires paths.contract_paths")
    implementation = _normalize_path_scopes(
        paths.get("implementation_scopes"),
        field=f"{module_name}.implementation_scopes",
        allow_empty=(
            module_kind == "contract_only"
            or (contract_mode == "review_guarded" and bool(contracts))
        ),
    )
    if "test_scopes" in paths:
        raise ValueError(
            f"module {module_name} cannot declare paths.test_scopes; "
            "the Manager owns tests/<module_name>/developer and tests/<module_name>/verification"
        )
    if module_kind == "contract_only" and implementation:
        raise ValueError(
            f"contract_only module {module_name} cannot declare implementation_scopes"
        )
    references = [_normalized_repo_path(str(item)) for item in list(paths.get("reference_only") or [])]
    return {
        "contract_mode": contract_mode,
        "contract_paths": list(dict.fromkeys(contracts)),
        "implementation_scopes": [item.to_dict() for item in implementation],
        "reference_only": list(dict.fromkeys(references)),
    }


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
        if not path:
            raise ValueError(
                f"{field} must use a file or directory below the repository root"
            )
        result.append(PathScope(kind, path.rstrip("/")))
    if not result and not allow_empty:
        raise ValueError(f"{field} requires at least one narrow writable path scope")
    return tuple(dict.fromkeys(result))


def _validate_path_policy(modules: Mapping[str, Mapping[str, Any]]) -> None:
    errors: list[str] = []
    owners: list[tuple[str, str, PathScope]] = []
    contracts: dict[str, tuple[str, str]] = {}
    references: list[tuple[str, str]] = []
    for name, module in modules.items():
        paths = dict(module["paths"])
        for path in paths["contract_paths"]:
            previous = contracts.setdefault(
                path,
                (name, str(paths.get("contract_mode") or "review_guarded")),
            )
            if previous[0] != name:
                errors.append(f"contract path {path} is owned by both {previous[0]} and {name}")
        for value in paths["implementation_scopes"]:
            owners.append((name, "implementation", PathScope(str(value["kind"]), str(value["path"]))))
        if str(module.get("module_kind") or "") == "implementation":
            owners.append(
                (
                    name,
                    "developer_tests",
                    PathScope("directory", module_developer_test_path(name)),
                )
            )
            owners.append(
                (
                    name,
                    "verification_corpus",
                    PathScope("directory", module_verification_corpus_path(name)),
                )
            )
        references.extend((name, path) for path in paths["reference_only"])
    for index, (owner, scope_kind, scope) in enumerate(owners):
        for other_owner, other_scope_kind, other_scope in owners[index + 1 :]:
            if _path_scopes_overlap(scope, other_scope) and (
                owner != other_owner
                or scope_kind != other_scope_kind
            ):
                errors.append(
                    "writable path scopes overlap between "
                    f"{owner} ({scope_kind}) and {other_owner} ({other_scope_kind}): "
                    f"{scope.path}, {other_scope.path}"
                )
        for contract_path, (contract_owner, contract_mode) in contracts.items():
            if scope.matches(contract_path):
                if owner != contract_owner or scope_kind != "implementation":
                    errors.append(
                        f"writable {scope_kind} scope {scope.path} for {owner} overlaps "
                        f"{contract_mode} contract {contract_path} owned by {contract_owner}"
                    )
        for reference_owner, reference_path in references:
            if scope.matches(reference_path) or _repo_paths_overlap(
                scope.path,
                reference_path,
            ):
                errors.append(
                    f"writable scope {scope.path} for {owner} overlaps reference-only path {reference_path} from {reference_owner}"
                )
    _raise_architecture_errors(errors)


def _validate_construction_graph(
    modules: Mapping[str, Mapping[str, Any]],
) -> None:
    dependencies_by_module = {
        name: set(str(item) for item in dict(module.get("dependencies") or {}))
        for name, module in modules.items()
    }
    unknown = sorted(
        {
            dependency
            for dependencies in dependencies_by_module.values()
            for dependency in dependencies
            if dependency not in dependencies_by_module
        }
    )
    if unknown:
        raise ValueError(
            "Architecture contract dependency graph references unavailable modules: "
            + ", ".join(unknown)
        )
    cycle = _cycle_nodes(
        {name: sorted(dependencies) for name, dependencies in dependencies_by_module.items()}
    )
    if cycle:
        raise ValueError("Architecture contract dependency graph contains a cycle: " + ", ".join(cycle))


def _validate_declared_paths(
    modules: Mapping[str, Mapping[str, Any]],
    workspace_root: Path,
) -> None:
    errors: list[str] = []
    for name, module in modules.items():
        paths = dict(module["paths"])
        entrypoint = str(list(paths["contract_paths"])[0])
        target = workspace_root / entrypoint
        if not target.is_file():
            errors.append(f"module {name} contract entrypoint does not exist: {entrypoint}")
        for contract_path in list(paths["contract_paths"]):
            if not (workspace_root / contract_path).is_file():
                errors.append(f"module {name} contract path does not exist: {contract_path}")
        for reference in list(paths["reference_only"]):
            if not (workspace_root / reference).exists():
                errors.append(f"module {name} reference-only path does not exist: {reference}")
    _raise_architecture_errors(errors)


def _compiled_path_policy(submission: Mapping[str, Any]) -> dict[str, Any]:
    modules: dict[str, Any] = {}
    for name, raw_module in dict(submission.get("modules") or {}).items():
        paths = dict(dict(raw_module).get("paths") or {})
        modules[name] = {
            "module_kind": str(dict(raw_module).get("module_kind") or ""),
            "contract_mode": str(paths.get("contract_mode") or "review_guarded"),
            "contract_paths": list(paths.get("contract_paths") or []),
            "implementation_scopes": list(paths.get("implementation_scopes") or []),
            "developer_tests": (
                {
                    "kind": "directory",
                    "path": module_developer_test_path(str(name)),
                }
                if str(dict(raw_module).get("module_kind") or "") == "implementation"
                else None
            ),
            "verification_corpus": (
                {
                    "kind": "directory",
                    "path": module_verification_corpus_path(str(name)),
                }
                if str(dict(raw_module).get("module_kind") or "") == "implementation"
                else None
            ),
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
            "Architect changed paths outside declared contract skeleton paths: "
            + ", ".join(undeclared)
        )
    return normalized_paths


def _architect_path_is_declared(path: str, submission: Mapping[str, Any]) -> bool:
    for module in dict(submission.get("modules") or {}).values():
        paths = dict(dict(module).get("paths") or {})
        if path in set(str(item) for item in list(paths.get("contract_paths") or [])):
            return True
    return False


def _path_scopes_overlap(left: PathScope, right: PathScope) -> bool:
    probes = {left.path, right.path}
    if left.kind == "directory":
        probes.add(left.path + "/__pal_probe__")
    if right.kind == "directory":
        probes.add(right.path + "/__pal_probe__")
    return any(left.matches(path) and right.matches(path) for path in probes)


def _repo_paths_overlap(left: str, right: str) -> bool:
    left_path = _normalized_repo_path(left).rstrip("/")
    right_path = _normalized_repo_path(right).rstrip("/")
    return (
        left_path == right_path
        or left_path.startswith(right_path + "/")
        or right_path.startswith(left_path + "/")
    )


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
