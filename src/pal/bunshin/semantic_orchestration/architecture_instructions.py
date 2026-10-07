from __future__ import annotations
import re
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.contracts import SubmissionInvariantError
from pal.bunshin.skeleton import SemanticReferenceError
from pal.bunshin.workspace_paths import ARCHITECT_AUTHORING_RELATIVE_PATH, module_developer_test_path, module_verification_corpus_path


def _architect_authoring_locations(
    path: Path,
    contract: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Capture exact authoring lines before the Manager removes its YAML projection."""

    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    result: dict[str, dict[str, Any]] = {}
    for section in ("requirements", "modules", "scenarios"):
        names = [str(name) for name in dict(contract.get(section) or {})]
        section_line = _yaml_top_level_line(lines, section)
        for name in names:
            symbol = f"{section}.{name}"
            result[symbol] = {
                "scope": "workspace",
                "file": ARCHITECT_AUTHORING_RELATIVE_PATH,
                "line": _yaml_mapping_member_line(
                    lines,
                    section=section,
                    member=name,
                    fallback=section_line,
                ),
                "symbol": symbol,
            }
    return result


def _yaml_top_level_line(lines: list[str], key: str) -> int:
    for index, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if raw == raw.lstrip() and stripped.split("#", 1)[0].rstrip() == f"{key}:":
            return index
    return 1


def _yaml_mapping_member_line(
    lines: list[str],
    *,
    section: str,
    member: str,
    fallback: int,
) -> int:
    in_section = False
    for index, raw in enumerate(lines, start=1):
        content = raw.split("#", 1)[0].rstrip()
        if not content.strip():
            continue
        indentation = len(content) - len(content.lstrip(" "))
        stripped = content.strip()
        if indentation == 0:
            in_section = stripped == f"{section}:"
            continue
        if not in_section or indentation != 2 or ":" not in stripped:
            continue
        candidate = stripped.split(":", 1)[0].strip().strip("\"'")
        if candidate == member:
            return index
    return max(1, int(fallback or 1))


def _stable_architecture_preflight_finding(
    exc: ValueError,
    *,
    contract_intent: Mapping[str, Any],
    submission: Mapping[str, Any],
) -> dict[str, Any]:
    """Compile every Manager finding into the same routable location contract."""

    error = str(exc)
    requirements = {
        str(name): dict(value or {})
        for name, value in dict(submission.get("requirements") or {}).items()
    }
    modules = {
        str(name): dict(value or {})
        for name, value in dict(submission.get("modules") or {}).items()
    }
    scenarios = {
        str(name): dict(value or {})
        for name, value in dict(submission.get("scenarios") or {}).items()
    }
    mentioned: list[str] = []
    affected_modules: set[str] = set()
    for section, values in (
        ("requirements", requirements),
        ("modules", modules),
        ("scenarios", scenarios),
    ):
        for name in values:
            if not re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
                error,
            ):
                continue
            mentioned.append(f"{section}.{name}")
            if section == "modules":
                affected_modules.add(name)
            elif section == "requirements":
                owner = str(requirements[name].get("owner") or "")
                if owner in modules:
                    affected_modules.add(owner)
                elif owner in scenarios:
                    affected_modules.update(
                        str(item)
                        for item in list(scenarios[owner].get("modules") or [])
                        if str(item) in modules
                    )
            else:
                affected_modules.update(
                    str(item)
                    for item in list(scenarios[name].get("modules") or [])
                    if str(item) in modules
                )
    symbols = list(dict.fromkeys(mentioned)) or ["architecture"]
    known_locations = {
        str(symbol): dict(location or {})
        for symbol, location in dict(
            contract_intent.get("authoring_locations") or {}
        ).items()
    }
    locations = [
        known_locations.get(
            symbol,
            {
                "scope": "workspace",
                "file": ARCHITECT_AUTHORING_RELATIVE_PATH,
                "line": 1,
                "symbol": symbol,
            },
        )
        for symbol in symbols
    ]
    payload: dict[str, Any] = {
        "finding_kind": "contract_defect",
        "summary": error,
        "source": "stable_architecture_preflight",
        "locations": locations,
        "repair_instruction": (
            "Correct only the rejected semantic DAG, reference, contract skeleton, or path declaration; "
            "preserve unrelated accepted architecture content."
        ),
    }
    if affected_modules:
        payload["affected_modules"] = sorted(affected_modules)
    if isinstance(exc, SemanticReferenceError):
        payload["semantic_reference_error"] = exc.to_dict()
    return payload


def _contract_architect_instruction(
    *,
    finding: Mapping[str, Any],
    has_base_manifest: bool,
    has_revision_scope: bool,
) -> str:
    instruction = (
        "Author the current Architecture Skeleton in the bound worktree. First read the ordered read-only task ledger, perform the required "
        "consistency pass, and inspect only the repository context needed to understand existing public boundaries. Then settle the smallest "
        "complete module-level design: define responsibilities and boundaries, directional public contracts and dependency handoffs, ownership, "
        "lifecycle, invariants, observable errors, optional state machines, and meaningful end-to-end scenario composition. Use update_checklist "
        "as the fixed work cursor: complete the requirements/design phase, then write the matching public declarations without product behavior, "
        "and only then fill the Manager-preseeded architect.yaml. Immediately begin file-edit tool calls once the design is settled; do not spend "
        "another response restating, rehearsing, simulating, "
        "or drafting the settled design in prose. Encode that same design according to the commented structure, reconcile both projections, and complete the checklist. This work is "
        "strictly module-level declaration: never implement "
        "product behavior, private algorithms, test bodies, or build machinery. Use the task-selected language. Ask the user only for an unresolved "
        "requirement or scope-changing decision. Call submit_contract only after the complete YAML and declaration skeleton agree and all checklist phases are completed."
    )
    if has_base_manifest:
        instruction += (
            " This is a revision based on the existing skeleton. Start from revision_finding or the explicit edit instruction and preserve "
            "unrelated declarations, contracts, path scopes, and dependencies unless consistency requires a wider correction. The semantic DAG Draft is already seeded from "
            "the accepted baseline in architect.yaml: do not remove, recreate, or restate unchanged modules. A source-only contract "
            "repair may submit the unchanged semantic DAG after editing the relevant declaration files."
        )
    if finding:
        summary = str(finding.get("summary") or "the bound architecture finding").strip()
        repair = str(finding.get("repair_instruction") or "").strip()
        instruction += (
            " A previous architecture submission was rejected and is not accepted. Read revision_finding before any other work, "
            f"correct this exact defect: {summary}"
            + (f" Repair boundary: {repair}" if repair else "")
            + " Do not report the earlier submit as completion. Call submit_contract again after the correction."
        )
    elif has_base_manifest:
        instruction += (
            " Start from the affected architect.yaml entries and declaration files, widen only when consistency requires it, then call submit_contract with no arguments."
        )
    if has_revision_scope:
        instruction += (
            " Read the bound revision_scope as repair guidance, not as a write fence. Append every bound finding_key to update_checklist as "
            "`resolve finding: <finding_key>` and mark it completed only when you claim the repair is closed. If one physical reference or contract defect affects multiple named modules, "
            "repair every affected module in the same candidate; do not wait for stable preflight to report the same defect one module at a time. "
            "Paths listed as immutable_requirement_paths identify evidence in task.yaml, never writable workspace targets. "
            "If a requirements defect remains unresolved after applying ordered task.yaml revisions over original, call ask_question and wait. The Manager records the exact question and answer in task.yaml before resuming you; continue directly without editing or submitting task data. "
            "The Manager checks checklist and structural closure; the Reviewer decides whether the repair is semantically correct."
        )
    return instruction


def _skeleton_architecture_review_view(
    artifact: Mapping[str, Any],
) -> dict[str, Any]:
    submission = artifact.get("submission")
    if not isinstance(submission, Mapping):
        raise SubmissionInvariantError("architecture skeleton is missing its semantic submission")
    review_view = dict(submission)
    review_view["changed_paths"] = list(artifact.get("changed_paths") or [])
    modules = dict(submission.get("modules") or {})
    review_view["manager_derived_verification_policy"] = {
        "architect_declares_test_scopes": False,
        "tests_are_product_scenarios": False,
        "developer_corpora": {
            str(name): {
                "kind": "directory",
                "path": module_developer_test_path(str(name)),
                "owner": "coder",
                "verifier_access": "read_only",
            }
            for name, raw_module in modules.items()
            if str(dict(raw_module or {}).get("module_kind") or "")
            == "implementation"
        },
        "verification_corpora": {
            str(name): {
                "kind": "directory",
                "path": module_verification_corpus_path(str(name)),
                "owner": "verifier",
                "coder_access": "read_only",
            }
            for name, raw_module in modules.items()
            if str(dict(raw_module or {}).get("module_kind") or "")
            == "implementation"
        },
    }
    return review_view
