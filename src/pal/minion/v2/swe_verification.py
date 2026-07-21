from __future__ import annotations

from pal.execution.generated_tool_models import (
    MinionV2SweVerificationOpMinionVerificationPassInput,
    MinionV2SweVerificationOpMinionVerificationRequestArchitectureRevisionInput,
    MinionV2SweVerificationOpMinionVerificationRequestContractRevisionInput,
    MinionV2SweVerificationOpMinionVerificationRequestDependencyRepairsInput,
    MinionV2SweVerificationOpMinionVerificationRequestModuleRepairInput,
    MinionV2SweVerificationOpMinionVerificationRequestRequirementsRevisionInput,
    MinionV2SweVerificationOpMinionVerificationUnknownInput,
)

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Mapping

from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.minion.v2.artifacts import ContentAddressedArtifactStore
from pal.minion.v2.review_findings import (
    ADD_FINDING_CAPABILITY,
    empty_review_draft,
    structured_findings,
)
from pal.minion.v2.repository import MinionV2Repository
from pal.minion.v2.submission_drafts import SubmissionDraftContext, SubmissionDraftStore
from pal.minion.workspace_tools import _append_unique_artifact, _write_minion_artifact
from pal.shared import RuntimeStatus


_NO_ARGS_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
_TARGETED_FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "modules": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "uniqueItems": True,
            "description": "Semantic module names from the bound contract dependency or scenario closure.",
        },
    },
    "required": ["modules"],
    "additionalProperties": False,
}
_UNKNOWN_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {
            "type": "string",
            "minLength": 1,
            "description": "Unavailable environment or platform evidence and the concrete follow-up verification plan.",
        }
    },
    "required": ["reason"],
    "additionalProperties": False,
}


SWE_VERIFICATION_CAPABILITIES = (
    ADD_FINDING_CAPABILITY,
    "op_minion_verification_pass",
    "op_minion_verification_request_module_repair",
    "op_minion_verification_request_dependency_repairs",
    "op_minion_verification_request_contract_revision",
    "op_minion_verification_request_architecture_revision",
    "op_minion_verification_request_requirements_revision",
    "op_minion_verification_unknown",
)


SWE_VERIFICATION_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_minion_verification_pass": {
        "alias": "verification_pass",
        "description": (
            "Submit PASS only after historical verifier regressions pass and you have added or materially strengthened "
            "adversarial tests for this exact candidate. The Manager validates test-scope changes and recorded tool evidence."
        ),
        "InputModel": MinionV2SweVerificationOpMinionVerificationPassInput,
    },
    "op_minion_verification_request_module_repair": {
        "alias": "verification_request_module_repair",
        "description": "Submit module repair after every reproduced defect has been recorded with add_finding. Takes no arguments.",
        "InputModel": MinionV2SweVerificationOpMinionVerificationRequestModuleRepairInput,
    },
    "op_minion_verification_request_dependency_repairs": {
        "alias": "verification_request_dependency_repairs",
        "description": (
            "Submit reproduced defects owned by one or more named upstream modules after recording each with add_finding. "
            "Names must come from the bound dependency closure."
        ),
        "InputModel": MinionV2SweVerificationOpMinionVerificationRequestDependencyRepairsInput,
    },
    "op_minion_verification_request_contract_revision": {
        "alias": "verification_request_contract_revision",
        "description": "Submit a frozen public contract or lifecycle/state-model defect already recorded with add_finding.",
        "InputModel": MinionV2SweVerificationOpMinionVerificationRequestContractRevisionInput,
    },
    "op_minion_verification_request_architecture_revision": {
        "alias": "verification_request_architecture_revision",
        "description": "Submit an add_finding-recorded module-boundary, ownership, hidden-coupling, dependency, or scenario-topology defect requiring architecture revision.",
        "InputModel": MinionV2SweVerificationOpMinionVerificationRequestArchitectureRevisionInput,
    },
    "op_minion_verification_request_requirements_revision": {
        "alias": "verification_request_requirements_revision",
        "description": (
            "Submit a conflict or material omission in the original Requirements after recording it with add_finding. "
            "Do not author replacement requirement records here."
        ),
        "InputModel": MinionV2SweVerificationOpMinionVerificationRequestRequirementsRevisionInput,
    },
    "op_minion_verification_unknown": {
        "alias": "verification_unknown",
        "description": (
            "Submit UNKNOWN only when a required platform or environment cannot be exercised. Manager policy decides whether it blocks."
        ),
        "InputModel": MinionV2SweVerificationOpMinionVerificationUnknownInput,
    },
}


_OUTCOME_BY_CAPABILITY = {
    "op_minion_verification_pass": "pass",
    "op_minion_verification_request_module_repair": "module_repair",
    "op_minion_verification_request_dependency_repairs": "dependency_repairs",
    "op_minion_verification_request_contract_revision": "contract_revision",
    "op_minion_verification_request_architecture_revision": "architecture_revision",
    "op_minion_verification_request_requirements_revision": "requirements_revision",
    "op_minion_verification_unknown": "unknown",
}


def is_swe_verification_capability(name: str) -> bool:
    return str(name) in SWE_VERIFICATION_TOOL_SPECS


def compile_swe_verification_tool_contract(work_view: Mapping[str, Any]) -> dict[str, Any]:
    module_name = str(
        work_view.get("module_name") or work_view.get("verification_name") or ""
    ).strip()
    dependencies = sorted(
        {
            str(item).strip()
            for item in list(
                work_view.get("contract_dependencies")
                or work_view.get("construction_dependencies")
                or work_view.get("depends_on")
                or []
            )
            if str(item).strip()
        }
    )
    accepted_modules = sorted(
        {
            str(dict(item or {}).get("module_name") or "").strip()
            for item in list(work_view.get("accepted_modules") or [])
            if str(dict(item or {}).get("module_name") or "").strip()
        }
    )
    dependency_targets = sorted(set(dependencies + accepted_modules) - {module_name})
    descriptions: dict[str, str] = {}
    descriptions[ADD_FINDING_CAPABILITY] = (
        "Record or replace one evidence-backed verifier finding. Use module_defect for the current implementation, "
        "dependency_defect for an upstream module, contract_defect for a frozen public contract, "
        "architecture_defect for ownership/topology, requirements_defect for contradictory task sources, and "
        "integration_defect for cross-module product behavior. Finish the breadth-first audit first and batch "
        "independent add_finding calls in one tool round when possible."
    )
    if dependency_targets:
        descriptions["op_minion_verification_request_dependency_repairs"] = (
            "Submit all reproduced upstream defects in one call. Allowed semantic module names: "
            + ", ".join(dependency_targets)
            + "."
        )
    return {
        "module_name": module_name,
        "dependency_targets": dependency_targets,
        "description_overrides": descriptions,
    }


def swe_verification_tool_result(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
    produced_artifacts: list[dict[str, Any]],
) -> CanonicalToolResult:
    try:
        outcome = _OUTCOME_BY_CAPABILITY[call.name]
        args = dict(call.args or {})
        reason = str(args.get("reason") or "").strip()
        target_modules = _validate_target_modules(workspace, args.get("modules") or [])
        context = SubmissionDraftContext.from_workspace(
            workspace,
            draft_kind="verification",
        )
        store = SubmissionDraftStore(Path(str(workspace["runtime_root"])))
        snapshot = store.read(context, seed=empty_review_draft())
        findings = structured_findings(snapshot.payload)
        errors = _submission_errors(
            outcome=outcome,
            findings=findings,
            reason=reason,
            workspace=workspace,
        )
        if errors:
            raise ValueError("Submission has the following errors:\n- " + "\n- ".join(errors))

        receipts = [
            dict(item)
            for item in list(workspace.get("review_tool_evidence_refs") or [])
            if isinstance(item, Mapping)
        ]
        changed_paths = _changed_paths(workspace)
        submission = {
            "schema_version": "2",
            "outcome": outcome,
            "findings": findings,
            **({"reason": reason} if reason else {}),
            **({"target_modules": target_modules} if target_modules else {}),
            "changed_test_paths": changed_paths,
            "tool_receipts": receipts,
        }
        submission_ref: dict[str, Any]
        if store.uses_role_gateway:
            receipt = store.mark_submitted(
                context,
                expected_version=snapshot.version,
                submission_payload=submission,
            )
            submission_ref = dict(receipt.get("submission_artifact_ref") or {})
        else:
            artifact_store = ContentAddressedArtifactStore(
                Path(str(workspace["runtime_root"])),
                MinionV2Repository(Path(str(workspace["runtime_root"]))),
            )
            ref = artifact_store.put_json(
                submission,
                artifact_type="SemanticVerificationSubmissionArtifact",
            )
            payload_hash = hashlib.sha256(
                json.dumps(
                    submission,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            store.mark_submitted(
                context,
                expected_version=snapshot.version,
                submission_artifact_ref=ref.to_dict(),
                submission_payload_hash=payload_hash,
                submission_payload=submission,
            )
            submission_ref = ref.to_dict()
        if not submission_ref:
            raise RuntimeError("Manager accepted verification without a durable receipt")
        artifact = _write_minion_artifact(
            dict(workspace),
            {
                "relative_path": "verification_submission.json",
                "title": "Semantic verification submission",
                "role": "primary",
                "mime_type": "application/json",
                "overwrite": True,
                "content": json.dumps(submission, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            },
        )
        _append_unique_artifact(produced_artifacts, artifact)
        return CanonicalToolResult(
            name=call.name,
            ok=True,
            text="Semantic verification submission recorded by Manager. Stop now.",
            llm_text="Semantic verification submission recorded by Manager. Stop now.",
            structured={
                "submitted": True,
            },
            call_id=call.call_id,
            status=RuntimeStatus.OK,
        )
    except Exception as exc:
        text = f"{exc.__class__.__name__}: {exc}"
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=text,
            llm_text=text + " Correct all listed issues and retry in this invocation.",
            structured={"error": str(exc), "error_type": exc.__class__.__name__},
            call_id=call.call_id,
            status=RuntimeStatus.INVALID,
        )


def _submission_errors(
    *,
    outcome: str,
    findings: list[Mapping[str, Any]],
    reason: str,
    workspace: Mapping[str, Any],
) -> list[str]:
    errors: list[str] = []
    receipts = [
        dict(item)
        for item in list(workspace.get("review_tool_evidence_refs") or [])
        if isinstance(item, Mapping)
    ]
    if not receipts:
        errors.append("run at least one shell, Git, or LSP verification before submitting")
    if any(
        bool(dict(item.get("structured") or {}).get("read_only_workspace_dirty"))
        for item in receipts
    ):
        errors.append(
            "a verification command modified the audited workspace; restore the intended test delta and rerun verification"
        )
    if outcome == "pass" and not any(bool(item.get("ok")) for item in receipts):
        errors.append("PASS requires at least one successful recorded verification tool result")
    if outcome not in {"pass", "unknown"} and not findings:
        errors.append("repair or revision outcomes require at least one add_finding call")
    if outcome in {"pass", "unknown"} and findings:
        errors.append(f"{outcome.upper()} requires an empty finding Draft")
    if outcome == "unknown" and not reason:
        errors.append("UNKNOWN requires an environmental reason and follow-up verification plan")
    changed_paths = _changed_paths(workspace)
    if outcome != "unknown" and not changed_paths:
        errors.append("verification requires a verifier-authored test delta in the bound test scopes")
    write_scopes = [
        dict(item or {}) for item in list(workspace.get("write_path_scopes") or [])
    ]
    scratch_only = bool(
        workspace.get("verification_scenario")
        or workspace.get("verification_scratch_only")
    )
    outside = [] if scratch_only else [
        path
        for path in changed_paths
        if not any(_path_scope_matches(path, scope) for scope in write_scopes)
    ]
    if outside:
        errors.append(
            "verification changed paths outside the bound test scopes: "
            + ", ".join(outside)
        )
    last_write = max(
        (
            index
            for index, item in enumerate(receipts)
            if str(item.get("kind") or "") == "test_write"
        ),
        default=-1,
    )
    final_checks = [
        item
        for index, item in enumerate(receipts)
        if index > last_write and str(item.get("kind") or "") in {"command", "lsp"}
    ]
    if changed_paths and not final_checks:
        errors.append("run verification again after the final test edit")
    if outcome == "pass" and not any(bool(item.get("ok")) for item in final_checks):
        errors.append("PASS requires a successful final command or LSP check")
    return errors


def _path_scope_matches(path: str, scope: Mapping[str, Any]) -> bool:
    normalized = str(path).replace("\\", "/").strip("/")
    target = str(scope.get("path") or "").replace("\\", "/").strip("/")
    if not target:
        return False
    kind = str(scope.get("kind") or "").strip().lower()
    if kind == "file":
        return normalized == target
    if kind == "directory":
        return normalized == target or normalized.startswith(target + "/")
    return False


def _validate_target_modules(workspace: Mapping[str, Any], values: Any) -> list[str]:
    requested = list(dict.fromkeys(str(item).strip() for item in list(values or []) if str(item).strip()))
    if not requested:
        return []
    contract = dict(dict(workspace.get("minion_v2") or {}).get("swe_verification_tool_contract") or {})
    allowed = {str(item) for item in list(contract.get("dependency_targets") or [])}
    unknown = sorted(set(requested) - allowed)
    if unknown:
        raise ValueError(
            "dependency repair names are outside the bound dependency closure: "
            + ", ".join(unknown)
            + "; allowed: "
            + (", ".join(sorted(allowed)) or "<none>")
        )
    return requested


def _changed_paths(workspace: Mapping[str, Any]) -> list[str]:
    if bool(
        workspace.get("verification_scenario")
        or workspace.get("verification_scratch_only")
    ):
        root = Path(str(workspace.get("review_scratch_dir") or ""))
        if not root.is_dir():
            return []
        return [
            f"review_scratch/{path.relative_to(root).as_posix()}"
            for path in sorted(item for item in root.rglob("*") if item.is_file() and not item.is_symlink())
        ]
    root = Path(str(workspace.get("repo_path") or ""))
    if not root.is_dir():
        return []
    completed = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        return []
    entries = [item for item in completed.stdout.split(b"\0") if item]
    paths: list[str] = []
    index = 0
    while index < len(entries):
        text = entries[index].decode("utf-8", errors="surrogateescape")
        status = text[:2]
        relative = text[3:] if len(text) > 3 else ""
        if relative:
            paths.append(relative.replace("\\", "/"))
        index += 1
        if ("R" in status or "C" in status) and index < len(entries):
            # Porcelain v1 -z emits the destination in the status record and
            # the source as the next unprefixed NUL-delimited field.
            source = entries[index].decode("utf-8", errors="surrogateescape")
            if source:
                paths.append(source.replace("\\", "/"))
            index += 1
    return sorted(dict.fromkeys(paths))
