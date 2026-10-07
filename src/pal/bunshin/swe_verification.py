from __future__ import annotations

from pal.bunshin.verifier_tool_diagnostics import record_verifier_failure

from pal.shared.tool_protocol import ToolCallIR

from pal.execution.generated_tool_models import (
    BunshinV2SweVerificationOpBunshinVerificationRequestArchitectureRevisionInput,
    BunshinV2SweVerificationOpBunshinVerificationRequestContractRevisionInput,
    BunshinV2SweVerificationOpBunshinVerificationRequestModuleRepairInput,
    BunshinV2SweVerificationOpBunshinVerificationRequestRequirementsRevisionInput,
    BunshinV2SweVerificationOpBunshinVerificationUnknownInput,
)

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Mapping

from pal.execution.tool_facade import EmptyToolInput
from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.workspace_git import git_changed_paths
from pal.bunshin.review_findings import (
    ADD_FINDING_CAPABILITY, UPDATE_FINDING_CAPABILITY, REMOVE_FINDING_CAPABILITY,
    empty_review_draft,
    partition_findings,
    structured_advisories,
    structured_findings,
)
from pal.bunshin.repository import BunshinV2Repository
from pal.bunshin.semantic_evidence import recorded_cases
from pal.bunshin.verification_readiness import current_verification_receipts, final_verification_errors, verification_case_errors, verification_corpus_snapshot
from pal.bunshin.submission_errors import submission_error_result, submission_validation
from pal.bunshin.submission_drafts import SubmissionDraftContext, SubmissionDraftStore
from pal.bunshin.verification_policy_validation import semantic_verification_draft_errors
from pal.bunshin.verification_lsp_policy import bound_verification_policy, lsp_policy_errors
from pal.bunshin.verification import (
    historical_repair_checklist_items,
    validate_verification_case_order,
)
from pal.bunshin.work_items import (
    assert_work_items_complete,
    findings_from_work_items,
    submission_work_items,
)
from pal.bunshin.workspace_paths import module_developer_test_path
from pal.bunshin.workspace_tools import _append_unique_artifact, _write_bunshin_artifact
from pal.shared import RuntimeStatus, ToolExecutionResult


SWE_VERIFICATION_CAPABILITIES = (
    UPDATE_FINDING_CAPABILITY, REMOVE_FINDING_CAPABILITY,
    "op_bunshin_verification_pass",
    "op_bunshin_verification_request_module_repair",
    "op_bunshin_verification_request_contract_revision",
    "op_bunshin_verification_request_architecture_revision",
    "op_bunshin_verification_request_requirements_revision",
    "op_bunshin_verification_unknown",
)


SEMANTIC_VERIFICATION_OUTCOMES = frozenset(
    {
        "pass",
        "module_repair",
        "contract_revision",
        "architecture_revision",
        "requirements_revision",
        "unknown",
    }
)


def semantic_verification_submission_errors(
    submission: Mapping[str, Any],
    *,
    work_view: Mapping[str, Any],
    changed_paths: list[str],
    current_case_paths: list[str],
    corpus_scope: Mapping[str, Any],
    scratch_only: bool,
    workspace: Mapping[str, Any] | None = None,
    accepted_legacy_receipt: bool = False,
) -> tuple[str, ...]:
    """Validate one semantic verifier submission against Manager-owned facts.

    This function is shared by the pre-receipt Role Gateway check and the
    post-receipt semantic effect check.  A gateway rejection therefore leaves
    the Draft and role attempt live, while a later disagreement is a genuine
    durable invariant failure.
    """

    outcome = str(submission.get("outcome") or "").strip()
    errors: list[str] = []
    accepted_binding = submission.get("verification_binding")
    # Callers may enable this only after matching a durable assignment receipt.
    # A submitted field can never authorize the compatibility path.
    allow_legacy = accepted_legacy_receipt and "verification_binding" not in submission
    if allow_legacy and workspace is not None:
        workspace = {**dict(workspace), "verification_case_revision": 0}
    if workspace is not None and isinstance(accepted_binding, Mapping):
        workspace = {**dict(workspace), "verification_case_revision": int(
            workspace.get("verification_case_revision", accepted_binding.get("case_revision", 0)))}
        if dict(accepted_binding) != verification_corpus_snapshot(workspace):
            errors.append("accepted verification identity does not match the current corpus and case revision")
    if outcome not in SEMANTIC_VERIFICATION_OUTCOMES:
        errors.append(
            f"unknown semantic verification outcome: {outcome or '<missing>'}"
        )
    try:
        findings = structured_findings(submission)
        structured_advisories(submission)
    except ValueError as exc:
        findings = []
        errors.append(str(exc))
    if workspace is not None:
        contract = dict(dict(workspace.get("bunshin_v2") or {}).get("swe_verification_tool_contract") or {})
        errors.extend(verification_finding_route_errors(findings, contract))
    reason = str(submission.get("reason") or "").strip()
    if outcome not in {"pass", "unknown"} and not findings:
        errors.append("repair and revision outcomes require structured findings")
    if outcome in {"pass", "unknown"} and findings:
        errors.append(f"{outcome.upper()} requires an empty finding list")
    if outcome == "unknown" and not reason:
        errors.append("UNKNOWN requires an environmental reason")

    recorded_results = [
        dict(item)
        for item in list(submission.get("recorded_results") or [])
        if isinstance(item, Mapping)
    ]
    errors.extend(verification_case_errors(recorded_results, outcome=outcome, workspace=workspace, allow_legacy=allow_legacy))
    if workspace is not None:
        errors.extend(lsp_policy_errors(bound_verification_policy(workspace), recorded_results))
    required_historical = historical_repair_checklist_items(work_view)
    try:
        validate_verification_case_order(
            [str(item.get("case_kind") or "") for item in recorded_results],
            historical_required=bool(required_historical),
        )
    except ValueError as exc:
        errors.append(str(exc))
    historical_names = {
        str(item.get("name") or "")
        for item in recorded_results
        if str(item.get("case_kind") or "") == "historical_regression"
    }
    missing_historical = [
        str(item["case"])
        for item in required_historical
        if str(item["case"]) not in historical_names
    ]
    if missing_historical:
        errors.append(
            "verification must replay every historical RepairBill case before submit: "
            + ", ".join(missing_historical)
        )
    evidence_tags = {
        str(tag)
        for item in recorded_results
        for tag in list(item.get("obligation_tags") or [])
    }
    if "candidate_delta_review" not in evidence_tags:
        errors.append(
            "verification requires a current-Candidate diff-risk check after regressions"
        )

    outside = [] if scratch_only else [
        path
        for path in changed_paths
        if not verification_path_scope_matches(path, corpus_scope)
    ]
    if outside:
        errors.append(
            "verifier changed paths outside the bound module corpus: "
            + ", ".join(outside)
        )
    if outcome != "unknown" and not current_case_paths:
        errors.append(
            "verification requires at least one durable verifier-authored case; "
            "the bound verification corpus is empty"
        )
    receipts = [
        dict(item)
        for item in list(submission.get("tool_receipts") or [])
        if isinstance(item, Mapping)
    ]
    if not receipts:
        errors.append(
            "verification requires Manager-recorded shell, Git, or LSP evidence"
        )
    if workspace is not None:
        receipts = current_verification_receipts(receipts, workspace, allow_legacy=allow_legacy)
    errors.extend(final_verification_errors(receipts, outcome=outcome, changed=bool(changed_paths)))
    return tuple(dict.fromkeys(errors))


def verification_workspace_changed_paths(
    review_workspace: Path,
    candidate_digest: str,
) -> list[str]:
    return git_changed_paths(review_workspace, candidate_digest)


def verification_scratch_paths(review_scratch: Path) -> list[str]:
    if not review_scratch.is_dir():
        return []
    return [
        f"review_scratch/{path.relative_to(review_scratch).as_posix()}"
        for path in sorted(
            item
            for item in review_scratch.rglob("*")
            if item.is_file() and not item.is_symlink()
        )
    ]


def verification_corpus_files(
    review_workspace: Path,
    corpus_scope: Mapping[str, Any],
) -> list[str]:
    root = review_workspace.resolve()
    target = str(corpus_scope.get("path") or "").replace("\\", "/").strip("/")
    if not target or not root.is_dir():
        return []
    path = (root / target).resolve()
    if not path.is_relative_to(root):
        return []
    if str(corpus_scope.get("kind") or "") == "file":
        return [target] if path.is_file() and not path.is_symlink() else []
    if not path.is_dir():
        return []
    return [
        item.relative_to(root).as_posix()
        for item in sorted(path.rglob("*"))
        if item.is_file() and not item.is_symlink()
    ]


def verification_path_scope_matches(
    path: str,
    scope: Mapping[str, Any],
) -> bool:
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


SWE_VERIFICATION_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_bunshin_verification_pass": {
        "alias": "submit_verification_pass",
        "guidance": {
            "purpose": "Submit a successful semantic verification outcome.",
            "use_when": (
                "Use with no arguments only after every required regression, diff-risk, "
                "behavioral, consumer, and delivery obligation for the bound scope passes."
            ),
            "do_not_use_when": (
                "Do not use with a blocking finding, missing required evidence, an untested "
                "final corpus edit, or a merely green compilation or LSP summary."
            ),
            "failure_next_steps": (
                "Run or record the missing obligation, correct any incomplete checklist or "
                "finding state, and retry only after the current assignment genuinely passes."
            ),
        },
        "InputModel": EmptyToolInput,
    },
    "op_bunshin_verification_request_module_repair": {
        "alias": "request_verification_module_repair",
        "guidance": {
            "purpose": "Submit reproduced implementation defects for module repair.",
            "use_when": (
                "Use with no arguments after every current implementation defect is reproduced "
                "and recorded with update_finding."
            ),
            "do_not_use_when": (
                "Do not use for a verifier-corpus, frozen contract, architecture, requirements, "
                "or unavailable-environment outcome."
            ),
            "failure_next_steps": (
                "Correct missing or misclassified findings and complete the checklist before retrying."
            ),
        },
        "InputModel": BunshinV2SweVerificationOpBunshinVerificationRequestModuleRepairInput,
    },
    "op_bunshin_verification_request_contract_revision": {
        "alias": "request_verification_contract_revision",
        "guidance": {
            "purpose": "Submit a frozen public-contract defect for contract revision.",
            "use_when": (
                "Use after recording a contradictory, incomplete, or impossible public contract "
                "or lifecycle/state-model defect with update_finding."
            ),
            "do_not_use_when": "Do not use when the current module can be repaired without changing its accepted contract.",
            "failure_next_steps": "Correct the finding classification or complete the checklist before retrying.",
        },
        "InputModel": BunshinV2SweVerificationOpBunshinVerificationRequestContractRevisionInput,
    },
    "op_bunshin_verification_request_architecture_revision": {
        "alias": "request_verification_architecture_revision",
        "guidance": {
            "purpose": "Submit a topology or ownership defect for architecture revision.",
            "use_when": (
                "Use after recording a module-boundary, ownership, hidden-coupling, dependency, "
                "bootstrap, or scenario-topology defect that cannot be repaired locally."
            ),
            "do_not_use_when": "Do not use for an implementation defect or a frozen-contract defect with unchanged topology.",
            "failure_next_steps": "Correct the finding classification or complete the checklist before retrying.",
        },
        "InputModel": BunshinV2SweVerificationOpBunshinVerificationRequestArchitectureRevisionInput,
    },
    "op_bunshin_verification_request_requirements_revision": {
        "alias": "request_verification_requirements_revision",
        "guidance": {
            "purpose": "Submit a contradictory or materially incomplete requirement for user revision.",
            "use_when": "Use after recording the exact requirements conflict or omission with update_finding.",
            "do_not_use_when": (
                "Do not use for an implementation, contract, or architecture defect, and do not "
                "author replacement requirement records."
            ),
            "failure_next_steps": "Correct the requirements finding or complete the checklist before retrying.",
        },
        "InputModel": BunshinV2SweVerificationOpBunshinVerificationRequestRequirementsRevisionInput,
    },
    "op_bunshin_verification_unknown": {
        "alias": "submit_verification_unknown",
        "guidance": {
            "purpose": "Submit an UNKNOWN outcome for required evidence unavailable in the bound environment.",
            "use_when": (
                "Use only when a required platform or environment genuinely cannot be exercised "
                "and provide the concrete missing evidence and follow-up plan."
            ),
            "do_not_use_when": (
                "Do not use for a failing check, an absent but non-required obligation, ordinary "
                "implementation difficulty, or evidence that can still be collected locally."
            ),
            "failure_next_steps": "Correct the reason or collect the available evidence before retrying.",
        },
        "InputModel": BunshinV2SweVerificationOpBunshinVerificationUnknownInput,
    },
}


_OUTCOME_BY_CAPABILITY = {
    "op_bunshin_verification_pass": "pass",
    "op_bunshin_verification_request_module_repair": "module_repair",
    "op_bunshin_verification_request_contract_revision": "contract_revision",
    "op_bunshin_verification_request_architecture_revision": "architecture_revision",
    "op_bunshin_verification_request_requirements_revision": "requirements_revision",
    "op_bunshin_verification_unknown": "unknown",
}


def is_swe_verification_capability(name: str) -> bool:
    return str(name) in SWE_VERIFICATION_TOOL_SPECS


def compile_swe_verification_tool_contract(
    work_view: Mapping[str, Any],
    *,
    repair_path_owners: Mapping[str, Any] | None = None,
    repair_scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    module_name = str(
        work_view.get("module_name") or work_view.get("verification_name") or ""
    ).strip()
    verification_corpus = str(
        dict(work_view.get("verification_corpus") or {}).get("path") or ""
    ).strip()
    requirements = {
        str(name): dict(value or {})
        for name, value in dict(work_view.get("requirements") or {}).items()
    }
    compiled_repair_path_owners = _normalize_repair_path_owners(
        dict(repair_scope).get("repair_path_owners")
        if repair_scope is not None
        else repair_path_owners
        if repair_path_owners is not None
        else _work_view_repair_path_owners(work_view)
    )
    dependency_modules = sorted(str(item) for item in dict(repair_scope or {}).get("dependency_modules") or [])
    guidance_overrides: dict[str, dict[str, str]] = {}
    guidance_overrides[UPDATE_FINDING_CAPABILITY] = {"use_when": (
        "Upsert one evidence-backed current verifier finding: unused finding_id with expected_revision=0 creates; current ID/revision from read_verification_draft_status replaces. Correct an incorrect Verifier-owned probe "
        "in this session before submission; use module_defect for the current implementation, "
        "dependency_defect only for an accepted upstream product bound to this check, contract_defect for a frozen public contract, "
        "architecture_defect for ownership/topology, requirements_defect for a contradictory task ledger, and "
        "sink_defect for the authored composition/delivery module. A performance finding requires a representative "
        "workload, concrete impact, and exact hot path; do not report speculative micro-optimization. "
        "Contract-only stubs are intentional and are not evidence that an upstream implementation is defective. "
        "Test the current module against the declared contract using focused probes or doubles. "
        f"Bound upstream implementation repair targets: {json.dumps(dependency_modules)}."
    )}
    if verification_corpus:
        guidance_overrides["op_bunshin_verification_pass"] = {"use_when": (
            f"Submit PASS only after reading and running the existing {verification_corpus}/ corpus, "
            "adding or strengthening coverage only for a demonstrated gap, and running a successful "
            "shell or LSP check (including a semantic run tool) against the final corpus state. PASS takes no arguments."
        )}
    elif requirements:
        guidance_overrides["op_bunshin_verification_pass"] = {"use_when": (
            "Submit PASS with no arguments only after testing the exact assembled scenario against its contract flow, requirements, and success/failure observations."
        )}
    return {
        "module_name": module_name,
        "repair_path_owners": compiled_repair_path_owners,
        "graph_sink": bool(dict(repair_scope or {}).get("graph_sink", work_view.get("graph_sink"))),
        "dependency_modules": dependency_modules,
        "contract_only_modules": sorted(set(compiled_repair_path_owners) - set(dependency_modules) - {module_name}),
        "verification_corpus": verification_corpus,
        "requirements": requirements,
        "guidance_overrides": guidance_overrides,
    }


def swe_verification_tool_result(
    call: ToolCallIR,
    workspace: Mapping[str, Any],
    produced_artifacts: list[dict[str, Any]],
) -> ToolExecutionResult:
    submission_started = False
    try:
        outcome = _OUTCOME_BY_CAPABILITY[call.name]
        args = dict(call.args or {})
        reason = str(args.get("reason") or "").strip()
        if outcome == "pass" and args:
            raise ValueError("submit_verification_pass takes no arguments")
        context = SubmissionDraftContext.from_workspace(
            workspace,
            draft_kind="verification",
        )
        store = SubmissionDraftStore(Path(str(workspace["runtime_root"])))
        snapshot = store.read(context, seed=empty_review_draft())
        findings, advisories = partition_findings(
            findings_from_work_items(workspace)
        )
        with submission_validation():
            readiness = verification_outcome_readiness(
                workspace, snapshot.payload, outcome=outcome, reason=reason,
            )
            if readiness["blockers"]:
                raise ValueError("Submission has the following errors:\n- " + "\n- ".join(readiness["blockers"]))
        work_items = readiness["work_items"]
        target_modules = readiness["target_modules"]

        receipts = [
            dict(item)
            for item in list(workspace.get("review_tool_evidence_refs") or [])
            if isinstance(item, Mapping)
        ]
        changed_paths = _changed_paths(workspace)
        cases = recorded_cases(snapshot.payload)
        submission = {
            "schema_version": "4",
            "verification_binding": verification_corpus_snapshot(workspace),
            "outcome": outcome,
            "findings": findings,
            "advisories": advisories,
            "work_items": submission_work_items(work_items.get("items")),
            **({"reason": reason} if reason else {}),
            **({"target_modules": target_modules} if target_modules else {}),
            "changed_test_paths": changed_paths,
            "tool_receipts": receipts,
            "recorded_results": cases,
        }
        submission_ref: dict[str, Any]
        if store.uses_role_gateway:
            submission_started = True
            receipt = store.mark_submitted(
                context,
                expected_version=snapshot.version,
                expected_work_item_version=int(work_items["version"]),
                verification_workspace=workspace,
                submission_payload=submission,
            )
            submission_ref = dict(receipt.get("submission_artifact_ref") or {})
        else:
            artifact_store = ContentAddressedArtifactStore(
                Path(str(workspace["runtime_root"])),
                BunshinV2Repository(Path(str(workspace['runtime_root']))).artifacts,
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
            submission_started = True
            store.mark_submitted(
                context,
                expected_version=snapshot.version,
                expected_work_item_version=int(work_items["version"]),
                verification_workspace=workspace,
                submission_artifact_ref=ref.to_dict(),
                submission_payload_hash=payload_hash,
                submission_payload=submission,
            )
            submission_ref = ref.to_dict()
        if not submission_ref:
            raise RuntimeError("Manager accepted verification without a durable receipt")
        artifact = _write_bunshin_artifact(
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
        return ToolExecutionResult(
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
        record_verifier_failure(exc)
        return submission_error_result(
            call, exc, submission_started=submission_started,
            invalid_code="verification_validation",
            correction="Correct all listed issues and retry in this invocation.",
        )


def verification_outcome_readiness(
    workspace: Mapping[str, Any], payload: Mapping[str, Any], *,
    outcome: str, reason: str = "", require_outcome_arguments: bool = True,
) -> dict[str, Any]:
    """The single read-only preflight used by status and semantic submit."""
    workspace = {**dict(workspace), "verification_case_revision": int(payload.get("case_revision") or 0)}
    findings, _ = partition_findings(findings_from_work_items(workspace))
    errors = _submission_errors(outcome=outcome, findings=findings, reason=reason, workspace=workspace,
                                require_outcome_arguments=require_outcome_arguments)
    errors.extend(semantic_verification_draft_errors(payload, workspace))
    work_items: dict[str, Any] = {}
    try:
        work_items = assert_work_items_complete(workspace)
    except ValueError as exc:
        errors.append(str(exc))
    targets: list[str] = []
    if outcome == "module_repair":
        contract = dict(dict(workspace.get("bunshin_v2") or {}).get("swe_verification_tool_contract") or {})
        try:
            targets = infer_repair_target_modules(findings, contract.get("repair_path_owners") or {})
        except ValueError as exc:
            errors.append(str(exc))
    contract = dict(dict(workspace.get("bunshin_v2") or {}).get("swe_verification_tool_contract") or {})
    errors.extend(verification_finding_route_errors(findings, contract))
    errors.extend(verification_case_errors(recorded_cases(payload), outcome=outcome, workspace=workspace))
    return {"ready": not errors, "blockers": list(dict.fromkeys(errors)),
            "work_items": work_items, "target_modules": targets}


def _submission_errors(
    *,
    outcome: str,
    findings: list[Mapping[str, Any]],
    reason: str,
    workspace: Mapping[str, Any],
    require_outcome_arguments: bool = True,
) -> list[str]:
    errors: list[str] = []
    receipts = [
        dict(item)
        for item in list(workspace.get("review_tool_evidence_refs") or [])
        if isinstance(item, Mapping)
    ]
    if not receipts:
        errors.append("run at least one shell, Git, or LSP verification before submitting")
    if outcome == "pass" and not any(bool(item.get("ok")) for item in receipts):
        errors.append("PASS requires at least one successful recorded verification tool result")
    if outcome not in {"pass", "unknown"} and not findings:
        errors.append("repair or revision outcomes require at least one update_finding call")
    if outcome in {"pass", "unknown"} and findings:
        errors.append(f"{outcome.upper()} requires an empty finding Draft")
    if outcome == "unknown" and require_outcome_arguments and not reason:
        errors.append("UNKNOWN requires an environmental reason and follow-up verification plan")
    finding_kinds = {
        str(item.get("finding_kind") or "")
        for item in findings
        if str(item.get("finding_kind") or "")
    }
    if "verification_defect" in finding_kinds:
        errors.append(
            "correct Verifier-owned probes in this session before submitting"
        )
    changed_paths = _changed_paths(workspace)
    if outcome != "unknown" and not _verification_case_files(workspace):
        errors.append(
            "verification requires at least one durable verifier-authored case; "
            "add coverage because the bound verification corpus is empty"
        )
    write_scopes = [
        dict(item or {}) for item in list(workspace.get("write_path_scopes") or [])
    ]
    scratch_only = bool(workspace.get("verification_scratch_only"))
    outside = [] if scratch_only else [
        path
        for path in changed_paths
        if not any(_path_scope_matches(path, scope) for scope in write_scopes)
    ]
    if outside:
        errors.append(
            "verification changed paths outside the bound module corpus: "
            + ", ".join(outside)
        )
    receipts = current_verification_receipts(receipts, workspace)
    errors.extend(final_verification_errors(receipts, outcome=outcome, changed=bool(changed_paths)))
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


def infer_repair_target_modules(
    findings: list[Mapping[str, Any]],
    repair_path_owners: Mapping[str, Any],
) -> list[str]:
    """Resolve semantic repair owners from verifier-authored source locations."""

    owners = _normalize_repair_path_owners(repair_path_owners)
    resolved: set[str] = set()
    unresolved: list[str] = []
    for finding in findings:
        finding_key = str(finding.get("finding_key") or "<unnamed>")
        finding_owners: set[str] = set()
        for raw_location in list(finding.get("locations") or []):
            location = dict(raw_location or {})
            if str(location.get("scope") or "") != "workspace":
                continue
            path = str(location.get("file") or location.get("path") or "").strip()
            if not path:
                continue
            for module_name, scopes in owners.items():
                if any(_path_scope_matches(path, scope) for scope in scopes):
                    finding_owners.add(module_name)
        if not finding_owners:
            unresolved.append(finding_key)
            continue
        resolved.update(finding_owners)
    if unresolved:
        raise ValueError(
            "Manager cannot derive a repair owner for finding(s): "
            + ", ".join(sorted(unresolved))
            + ". Cite at least one workspace file owned by the affected module, "
            "or use the contract/architecture/requirements revision outcome."
        )
    if not resolved:
        raise ValueError(
            "Manager cannot derive repair targets without workspace-owned finding locations"
        )
    return sorted(resolved)


def verification_finding_route_errors(
    findings: list[Mapping[str, Any]],
    contract: Mapping[str, Any],
) -> list[str]:
    """Validate semantic finding kinds against the same bound repair authority.

    Workspace visibility/path ownership alone cannot authorize implementation
    repairs. Contract/architecture/requirements findings retain their plan route.
    """

    if "dependency_modules" not in contract:
        # Legacy prompt packs are revalidated from their baseline at settlement.
        return []
    current = str(contract.get("module_name") or "")
    dependencies = set(str(item) for item in contract.get("dependency_modules") or [])
    errors: list[str] = []
    for finding in findings:
        kind = str(finding.get("finding_kind") or finding.get("defect_kind") or "")
        identity = str(finding.get("finding_id") or finding.get("finding_key") or "<unnamed>")
        if kind in {"contract_defect", "architecture_defect", "requirements_defect"}:
            continue
        if kind == "verification_defect":
            errors.append(f"{identity}: correct Verifier-owned probes in this session before submitting")
            continue
        try:
            owners = set(infer_repair_target_modules([finding], contract.get("repair_path_owners") or {}))
        except ValueError as exc:
            errors.append(f"{identity}: {exc}")
            continue
        if kind == "dependency_defect":
            upstream = owners - {current}
            if not upstream or upstream - dependencies:
                errors.append(
                    f"{identity}: dependency_defect must cite a bound accepted provider implementation; "
                    f"eligible modules: {', '.join(sorted(dependencies)) or '(none)'}. "
                    "Contract-only stubs are intentional, not provider defects. Correct the probe/claim; "
                    "use contract_defect only for evidence of a defect in the declared contract. "
                    "Preserve unrelated current-module findings."
                )
        elif kind in {"module_defect", "sink_defect"}:
            if current not in owners:
                errors.append(f"{identity}: {kind} must cite the current module {current}; use dependency_defect for a bound provider product")
            if kind == "sink_defect" and not contract.get("graph_sink"):
                errors.append(f"{identity}: sink_defect is only valid for the declared sink checker")
    return errors


def _work_view_repair_path_owners(
    work_view: Mapping[str, Any],
) -> dict[str, list[dict[str, str]]]:
    owners: dict[str, list[dict[str, str]]] = {}
    modules = {
        str(name): dict(value or {})
        for name, value in dict(work_view.get("modules") or {}).items()
    }
    if modules:
        for module_name, module in modules.items():
            paths = dict(module.get("paths") or {})
            scopes = [
                {"kind": "file", "path": str(path)}
                for path in list(paths.get("contract_paths") or [])
            ]
            scopes.extend(
                dict(scope or {})
                for scope in list(paths.get("implementation_scopes") or [])
            )
            scopes.extend(
                (
                    {
                        "kind": "directory",
                        "path": module_developer_test_path(module_name),
                    },
                )
            )
            owners[module_name] = scopes
        return owners

    module_name = str(work_view.get("module_name") or "").strip()
    if not module_name:
        return owners
    scopes = [
        {"kind": "file", "path": str(path)}
        for path in list(work_view.get("contract_paths") or [])
    ]
    scopes.extend(
        dict(scope or {})
        for scope in list(work_view.get("implementation_scopes") or [])
    )
    for field in ("developer_tests",):
        scope = dict(work_view.get(field) or {})
        if scope:
            scopes.append(scope)
    owners[module_name] = scopes
    return owners


def _normalize_repair_path_owners(
    value: Mapping[str, Any],
) -> dict[str, list[dict[str, str]]]:
    normalized: dict[str, list[dict[str, str]]] = {}
    for raw_module_name, raw_scopes in dict(value or {}).items():
        module_name = str(raw_module_name or "").strip()
        if not module_name:
            continue
        scopes: list[dict[str, str]] = []
        for raw_scope in list(raw_scopes or []):
            scope = dict(raw_scope or {})
            kind = str(scope.get("kind") or "").strip().lower()
            path = str(scope.get("path") or "").replace("\\", "/").strip("/")
            if kind not in {"file", "directory"} or not path:
                continue
            item = {"kind": kind, "path": path}
            if item not in scopes:
                scopes.append(item)
        if scopes:
            normalized[module_name] = scopes
    return dict(sorted(normalized.items()))


def _changed_paths(workspace: Mapping[str, Any]) -> list[str]:
    if bool(workspace.get("verification_scratch_only")):
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


def _verification_case_files(workspace: Mapping[str, Any]) -> list[str]:
    if bool(workspace.get("verification_scratch_only")):
        root = Path(str(workspace.get("review_scratch_dir") or ""))
        return (
            [
                f"review_scratch/{path.relative_to(root).as_posix()}"
                for path in sorted(
                    item
                    for item in root.rglob("*")
                    if item.is_file() and not item.is_symlink()
                )
            ]
            if root.is_dir()
            else []
        )
    root = Path(str(workspace.get("repo_path") or "")).resolve()
    if not root.is_dir():
        return []
    files: list[str] = []
    for raw_scope in list(workspace.get("write_path_scopes") or []):
        scope = dict(raw_scope or {})
        target = str(scope.get("path") or "").replace("\\", "/").strip("/")
        if not target:
            continue
        path = (root / target).resolve()
        if not path.is_relative_to(root.resolve()):
            continue
        if str(scope.get("kind") or "") == "file":
            if path.is_file() and not path.is_symlink():
                files.append(target)
            continue
        if not path.is_dir():
            continue
        files.extend(
            item.relative_to(root).as_posix()
            for item in sorted(path.rglob("*"))
            if item.is_file() and not item.is_symlink()
        )
    return sorted(set(files))
