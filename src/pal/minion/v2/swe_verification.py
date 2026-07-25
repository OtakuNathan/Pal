from __future__ import annotations

from pal.execution.generated_tool_models import (
    MinionV2SweVerificationOpMinionVerificationRequestArchitectureRevisionInput,
    MinionV2SweVerificationOpMinionVerificationRequestContractRevisionInput,
    MinionV2SweVerificationOpMinionVerificationRequestModuleRepairInput,
    MinionV2SweVerificationOpMinionVerificationRequestRequirementsRevisionInput,
    MinionV2SweVerificationOpMinionVerificationUnknownInput,
)

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Mapping

from pal.execution.tool_facade import EmptyToolInput
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


SWE_VERIFICATION_CAPABILITIES = (
    ADD_FINDING_CAPABILITY,
    "op_minion_verification_pass",
    "op_minion_verification_request_module_repair",
    "op_minion_verification_request_corpus_repair",
    "op_minion_verification_request_contract_revision",
    "op_minion_verification_request_architecture_revision",
    "op_minion_verification_request_requirements_revision",
    "op_minion_verification_unknown",
)


SWE_VERIFICATION_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_minion_verification_pass": {
        "alias": "verification_pass",
        "description": (
            "Submit PASS after the durable module corpus covers the candidate and passes. Add or materially strengthen "
            "adversarial coverage only when the existing corpus leaves a real gap. Takes no arguments. The Manager requires "
            "a non-empty tests/<module_name>/verification corpus and a successful ordinary shell or LSP check after any final edit."
        ),
        "InputModel": EmptyToolInput,
    },
    "op_minion_verification_request_module_repair": {
        "alias": "verification_request_module_repair",
        "description": "Submit module repair after every reproduced defect has been recorded with add_finding. Takes no arguments.",
        "InputModel": MinionV2SweVerificationOpMinionVerificationRequestModuleRepairInput,
    },
    "op_minion_verification_request_corpus_repair": {
        "alias": "verification_request_corpus_repair",
        "description": (
            "Submit a Verifier-owned corpus defect after recording every incorrect probe with "
            "finding_kind=verification_defect and an exact tests/<module>/verification location. "
            "Manager routes the original module Verifier to correct and rerun its corpus; Coder is never invoked."
        ),
        "InputModel": EmptyToolInput,
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
    "op_minion_verification_request_corpus_repair": "verification_repairs",
    "op_minion_verification_request_contract_revision": "contract_revision",
    "op_minion_verification_request_architecture_revision": "architecture_revision",
    "op_minion_verification_request_requirements_revision": "requirements_revision",
    "op_minion_verification_unknown": "unknown",
}


def is_swe_verification_capability(name: str) -> bool:
    return str(name) in SWE_VERIFICATION_TOOL_SPECS


def compile_swe_verification_tool_contract(
    work_view: Mapping[str, Any],
    *,
    repair_path_owners: Mapping[str, Any] | None = None,
    verification_path_owners: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    module_name = str(
        work_view.get("module_name") or work_view.get("verification_name") or ""
    ).strip()
    system_mode = str(work_view.get("kind") or "") == "system_and_delivery"
    verification_corpus = str(
        dict(work_view.get("verification_corpus") or {}).get("path") or ""
    ).strip()
    requirements = {
        str(name): dict(value or {})
        for name, value in dict(work_view.get("requirements") or {}).items()
    }
    compiled_repair_path_owners = _normalize_repair_path_owners(
        repair_path_owners
        if repair_path_owners is not None
        else _work_view_repair_path_owners(work_view)
    )
    compiled_verification_path_owners = _normalize_repair_path_owners(
        verification_path_owners
        if verification_path_owners is not None
        else _work_view_verification_path_owners(work_view)
    )
    guidance_overrides: dict[str, dict[str, str]] = {}
    guidance_overrides[ADD_FINDING_CAPABILITY] = {"use_when": (
        "Record or replace one evidence-backed verifier finding. Use verification_defect only for an incorrect "
        "Verifier-owned probe or corpus, module_defect for the current implementation, "
        "dependency_defect for an upstream module, contract_defect for a frozen public contract, "
        "architecture_defect for ownership/topology, requirements_defect for a contradictory task ledger, and "
        "integration_defect for cross-module product behavior. Material performance defects are valid findings when "
        "supported by a representative scaling probe or source-level complexity trace; include the triggering workload, "
        "impact, exact hot path, and a bounded contract-preserving optimization direction, and do not report speculative "
        "micro-optimizations. Finish the breadth-first audit first and batch "
        "independent add_finding calls in one tool round when possible."
    )}
    if system_mode:
        guidance_overrides[ADD_FINDING_CAPABILITY] = {"use_when": (
            "Record or replace one evidence-backed scenario finding. Use verification_defect when the failure is "
            "caused by an incorrect Verifier-owned module corpus or test double; cite its exact "
            "tests/<module>/verification path and submit corpus repair so Manager returns it to the original Verifier. "
            "For every implementation repair, cite at least "
            "one exact workspace file owned by the affected module so Manager can derive the graph route mechanically. "
            "Use contract_defect, architecture_defect, or requirements_defect when no implementation-owned path can "
            "legally resolve the issue. Material end-to-end performance defects are valid findings when supported by a "
            "representative scaling probe or source-level complexity trace; include the triggering workload, impact, exact "
            "hot path, and bounded contract-preserving optimization direction, not speculative micro-optimization. "
            "Finish the breadth-first audit first and batch independent findings in one tool round."
        )}
    if system_mode:
        guidance_overrides["op_minion_verification_request_module_repair"] = {"use_when": (
            "Submit reproduced implementation defects after recording each with add_finding. "
            "Manager derives every affected module mechanically from the finding locations and "
            "the bound path ownership map; do not choose repair targets yourself."
        )}
        guidance_overrides["op_minion_verification_request_corpus_repair"] = {
            "use_when": (
                "Submit only incorrect Verifier-owned corpus cases after recording each as verification_defect. "
                "Manager derives the owning module from exact verification-corpus locations and reopens that "
                "module's Verifier; this route never sends the corpus to a Coder."
            ),
            "do_not_use_when": (
                "Do not use for a product, contract, architecture, integration, or requirements defect. "
                "An isolation double is not defective merely because a full-project build incorrectly compiles it."
            ),
        }
    if verification_corpus:
        guidance_overrides["op_minion_verification_pass"] = {"use_when": (
            f"Submit PASS only after reading and running the existing {verification_corpus}/ corpus, "
            "adding or strengthening coverage only for a demonstrated gap, and running a successful "
            "ordinary shell or LSP check against the final corpus state. PASS takes no arguments."
        )}
    elif requirements:
        guidance_overrides["op_minion_verification_pass"] = {"use_when": (
            "Submit PASS with no arguments only after testing the exact assembled scenario against its contract flow, requirements, and success/failure observations."
        )}
    return {
        "module_name": module_name,
        "system_mode": system_mode,
        "repair_path_owners": compiled_repair_path_owners,
        "verification_path_owners": compiled_verification_path_owners,
        "verification_corpus": verification_corpus,
        "requirements": requirements,
        "guidance_overrides": guidance_overrides,
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
        if outcome == "pass" and args:
            raise ValueError("verification_pass takes no arguments")
        context = SubmissionDraftContext.from_workspace(
            workspace,
            draft_kind="verification",
        )
        store = SubmissionDraftStore(Path(str(workspace["runtime_root"])))
        snapshot = store.read(context, seed=empty_review_draft())
        findings = structured_findings(snapshot.payload)
        contract = dict(
            dict(workspace.get("minion_v2") or {}).get(
                "swe_verification_tool_contract"
            )
            or {}
        )
        system_mode = bool(
            workspace.get("system_verification")
            or contract.get("system_mode")
        )
        if system_mode and outcome == "module_repair":
            if any(
                str(item.get("finding_kind") or "") == "verification_defect"
                for item in findings
            ):
                raise ValueError(
                    "verification_defect findings must use verification_request_corpus_repair"
                )
            target_modules = infer_repair_target_modules(
                findings,
                contract.get("repair_path_owners") or {},
            )
            # Manager owns the graph route and resolves source locations to
            # the module whose public boundary was violated.
            outcome = "module_repair"
        elif system_mode and outcome == "verification_repairs":
            if {
                str(item.get("finding_kind") or "")
                for item in findings
            } != {"verification_defect"}:
                raise ValueError(
                    "corpus repair requires every finding to use verification_defect"
                )
            target_modules = infer_repair_target_modules(
                findings,
                contract.get("verification_path_owners") or {},
            )
        else:
            target_modules = []
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
            "schema_version": "3",
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
    if outcome == "pass" and not any(bool(item.get("ok")) for item in receipts):
        errors.append("PASS requires at least one successful recorded verification tool result")
    if outcome not in {"pass", "unknown"} and not findings:
        errors.append("repair or revision outcomes require at least one add_finding call")
    if outcome in {"pass", "unknown"} and findings:
        errors.append(f"{outcome.upper()} requires an empty finding Draft")
    if outcome == "unknown" and not reason:
        errors.append("UNKNOWN requires an environmental reason and follow-up verification plan")
    finding_kinds = {
        str(item.get("finding_kind") or "")
        for item in findings
        if str(item.get("finding_kind") or "")
    }
    if outcome == "verification_repairs":
        if finding_kinds != {"verification_defect"}:
            errors.append(
                "corpus repair requires every finding to use verification_defect"
            )
        if not bool(
            workspace.get("system_verification")
            or dict(
                dict(workspace.get("minion_v2") or {}).get(
                    "swe_verification_tool_contract"
                )
                or {}
            ).get("system_mode")
        ):
            errors.append(
                "the active module Verifier owns its corpus; correct it and rerun instead of submitting corpus repair"
            )
    elif "verification_defect" in finding_kinds:
        errors.append(
            "verification_defect findings must use verification_request_corpus_repair"
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
    scratch_only = bool(
        workspace.get("system_verification")
        or workspace.get("verification_scratch_only")
    )
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
                        "path": f"tests/{module_name}/developer",
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


def _work_view_verification_path_owners(
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
            scope = dict(paths.get("verification_corpus") or {})
            if not scope:
                scope = {
                    "kind": "directory",
                    "path": f"tests/{module_name}/verification",
                }
            owners[module_name] = [scope]
        return owners
    module_name = str(work_view.get("module_name") or "").strip()
    scope = dict(work_view.get("verification_corpus") or {})
    if module_name and scope:
        owners[module_name] = [scope]
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
    if bool(
        workspace.get("system_verification")
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


def _verification_case_files(workspace: Mapping[str, Any]) -> list[str]:
    if bool(
        workspace.get("system_verification")
        or workspace.get("verification_scratch_only")
    ):
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
    root = Path(str(workspace.get("repo_path") or ""))
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
