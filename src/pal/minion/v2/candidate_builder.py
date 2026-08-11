from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR

from pal.execution.generated_tool_models import (
    MinionV2CandidateBuilderOpMinionCandidateReportArchitectureDefectInput,
    MinionV2CandidateBuilderOpMinionCandidateRequestModuleSplitInput,
    MinionV2CandidateBuilderOpMinionCandidateSubmitInput,
)

import json
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from pal.execution.tool_facade import (
    ToolAffordance,
    ToolRejectedError,
    rejection,
)
from pal.minion.v2.adapters import ARTIFACT_BUNDLE_ADAPTER
from pal.minion.v2.skeleton import compiled_module_write_scopes
from pal.minion.v2.submission_drafts import (
    SubmissionDraftContext,
    SubmissionDraftStore,
    assert_authoring_schema_budget,
)
from pal.minion.v2.submission_preflight import bound_reference_payload
from pal.minion.v2.work_items import (
    assert_work_items_complete,
    read_work_items,
    submission_work_items,
)
from pal.minion.workspace_tools import _append_unique_artifact, _write_minion_artifact
from pal.shared import RuntimeStatus, ToolExecutionResult


CANDIDATE_BUILDER_CAPABILITIES = (
    "op_minion_candidate_submit",
    "op_minion_candidate_report_architecture_defect",
    "op_minion_candidate_request_module_split",
)


CANDIDATE_BUILDER_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_minion_candidate_submit": {
        "alias": "candidate_submit",
        "description": "Submit the current module Candidate for independent verification.",
        "guidance": {
            "use_when": (
                "Use after every checklist item is completed, focused checks pass, and the "
                "implementation is ready for independent verification. Takes no arguments; "
                "the Manager derives Git delta and journal fields."
            ),
            "do_not_use_when": (
                "Do not use with unfinished checklist work, without a contracted product delta, "
                "or when the correct terminal outcome is an architecture defect or module split. "
                "Never add candidate_submit itself to the checklist."
            ),
            "failure_next_steps": (
                "Follow the returned recovery affordance, correct the checklist or workspace "
                "state, and do not repeat the same rejected submission unchanged."
            ),
        },
        "InputModel": MinionV2CandidateBuilderOpMinionCandidateSubmitInput,
    },
    "op_minion_candidate_report_architecture_defect": {
        "alias": "candidate_report_architecture_defect",
        "description": "Terminally report that the frozen architecture contract cannot satisfy the task.",
        "guidance": {
            "use_when": (
                "Use only when correct implementation requires changing a public boundary, "
                "contract, ownership, lifecycle/state semantics, ABI, or topology. Explain the "
                "semantic conflict and optionally cite a task source filename or code location."
            ),
            "do_not_use_when": (
                "Do not use for ordinary implementation difficulty, a local code defect, a test "
                "failure, or an environment problem that can be handled within the bound module. "
                "Do not use because a parallel dependency's private definition is absent, unfinished, "
                "or fails to link in this isolated worktree: trust its public contract and continue the "
                "owned implementation. Dependency behavior and real composition belong to verification."
            ),
            "failure_next_steps": (
                "Correct the semantic conflict report or location from the returned error; do not "
                "retry the same rejected report unchanged."
            ),
        },
        "InputModel": MinionV2CandidateBuilderOpMinionCandidateReportArchitectureDefectInput,
    },
    "op_minion_candidate_request_module_split": {
        "alias": "candidate_request_module_split",
        "description": "Terminally request an architecture-owned module split.",
        "guidance": {
            "use_when": (
                "Use only when the accepted module's responsibility and scale genuinely cannot "
                "fit one Candidate cycle without an architecture-owned split."
            ),
            "do_not_use_when": (
                "Do not use for ordinary implementation difficulty, a preferred refactor, or an "
                "architecture contradiction that should be reported as a contract defect."
            ),
            "failure_next_steps": (
                "Correct the split rationale from the returned error; do not retry the same "
                "rejected request unchanged."
            ),
        },
        "InputModel": MinionV2CandidateBuilderOpMinionCandidateRequestModuleSplitInput,
    },
}

for _tool_name, _tool_spec in CANDIDATE_BUILDER_TOOL_SPECS.items():
    assert_authoring_schema_budget(
        _tool_spec["InputModel"].model_json_schema(mode="validation", union_format="primitive_type_array"),
        owner=_tool_name,
    )
    for _example in tuple(_tool_spec.get("examples") or ()):
        _tool_spec["InputModel"].model_validate(_example, strict=True)


def is_candidate_builder_capability(name: str) -> bool:
    return str(name or "") in CANDIDATE_BUILDER_TOOL_SPECS


async def candidate_builder_tool_result(
    call: ToolCallIR,
    workspace: dict[str, Any],
    produced_artifacts: list[dict[str, Any]],
) -> ToolExecutionResult:
    name = str(call.name or "")
    try:
        if name == "op_minion_candidate_submit":
            return _submit_candidate(call, workspace, produced_artifacts, status="candidate_ready")
        if name == "op_minion_candidate_report_architecture_defect":
            return _submit_candidate(call, workspace, produced_artifacts, status="architecture_defect")
        if name == "op_minion_candidate_request_module_split":
            return _submit_candidate(call, workspace, produced_artifacts, status="module_split_request")
        raise ValueError(f"unknown candidate authoring capability: {name}")
    except ToolRejectedError as exc:
        return _rejected(call, exc)
    except Exception as exc:
        return _error(call, exc)


def _submit_candidate(
    call: ToolCallIR,
    workspace: Mapping[str, Any],
    produced_artifacts: list[dict[str, Any]],
    *,
    status: str,
) -> ToolExecutionResult:
    args = dict(call.args or {})
    context = SubmissionDraftContext.from_workspace(workspace, draft_kind="candidate")
    store = SubmissionDraftStore(Path(str(workspace["runtime_root"])))
    snapshot = store.read(context, seed={})
    work_view = _bound_candidate_work_view(workspace)
    if status == "candidate_ready":
        try:
            work_items = assert_work_items_complete(workspace)
        except ValueError as exc:
            ledger = read_work_items(workspace)
            raise ToolRejectedError(
                f"candidate work items are not complete: {exc}",
                error_code="checklist_invalid",
                affordances=[
                    ToolAffordance(
                        tool="update_checklist",
                        arguments={
                            "plan": [
                                {
                                    "step": str(item.get("summary") or ""),
                                    "status": "completed",
                                }
                                for item in ledger["items"]
                                if str(item.get("kind") or "")
                                in {"phase", "task"}
                            ]
                        },
                        reason=(
                            "Mark each item complete only after doing the "
                            "corresponding work, then submit again."
                        ),
                    )
                ],
            ) from exc
        if args:
            raise ToolRejectedError(
                "candidate_submit takes no arguments",
                error_code="invalid_arguments",
            )
    else:
        work_items = {"items": []}
        _validate_defect_args(args, work_view=work_view)
    files_changed = _live_worktree_delta(workspace, work_view=work_view)
    reserved_paths = {
        str(item).replace("\\", "/").strip().lstrip("./")
        for item in list(workspace.get("manager_owned_submission_paths") or [])
        if str(item).strip()
    }
    polluted = sorted(reserved_paths.intersection(files_changed))
    if polluted:
        raise ToolRejectedError(
            "candidate workspace contains Manager-owned submission files: "
            + ", ".join(polluted),
            error_code="candidate_workspace_polluted",
        )
    if status == "candidate_ready" and _is_artifact_unit_view(work_view) and not files_changed:
        raise ToolRejectedError(
            "candidate_submit requires at least one contracted product file in the artifact workspace",
            error_code="candidate_product_required",
        )
    report = {
        "work_items": submission_work_items(
            work_items.get("items")
        ),
        "files_changed": files_changed,
        "status": status,
    }
    if status != "candidate_ready":
        module_name = _bound_unit_name(work_view)
        report.update(
            {
                "summary": str(args.get("summary") or "").strip(),
                "affected_module": module_name,
                "locations": _defect_locations(args),
                "source_file": str(args.get("source_file") or "").strip(),
            }
        )
    reference_warnings = validate_candidate_submission(report, work_view=work_view)
    if reference_warnings:
        report["reference_warnings"] = list(reference_warnings)
    artifact_filename = (
        "coder_report.json" if _is_software_module_view(work_view) else "producer_report.json"
    )
    manager_workspace = {**dict(workspace), "manager_submission_write": True}
    # A report file is a presentation artifact, not the completion receipt.
    # Let the Manager accept and durably record the submission before exposing
    # a primary artifact to the runner completion gate.
    if store.uses_role_gateway:
        store.mark_submitted(
            context,
            expected_version=snapshot.version,
            submission_payload=report,
        )
    artifact = _write_minion_artifact(
        manager_workspace,
        {
            "relative_path": artifact_filename,
            "title": "Module candidate submission",
            "role": "primary",
            "mime_type": "application/json",
            "overwrite": True,
            "content": json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        },
    )
    for existing in produced_artifacts:
        if str(existing.get("role") or "") == "primary":
            existing["role"] = "deliverable"
    _append_unique_artifact(produced_artifacts, artifact)
    if not store.uses_role_gateway:
        store.mark_submitted(
            context,
            expected_version=snapshot.version,
            submission_payload=report,
        )
    return _ok(
        call,
        "Candidate intent recorded. Stop now; Manager will quiesce and snapshot the worktree.",
        {"submitted": True},
    )


def validate_candidate_submission(
    value: Mapping[str, Any], *, work_view: Mapping[str, Any]
) -> tuple[str, ...]:
    required = {
        "work_items",
        "files_changed",
        "status",
    }
    missing = required - set(value)
    if missing:
        raise ValueError("candidate report is missing Manager fields: " + ", ".join(sorted(missing)))
    for field in required - {"status", "work_items"}:
        if not isinstance(value.get(field), list) or any(not isinstance(item, str) for item in value[field]):
            raise ValueError(f"candidate report {field} must be a string array")
    if not isinstance(value.get("work_items"), list) or any(
        not isinstance(item, Mapping)
        for item in list(value.get("work_items") or [])
    ):
        raise ValueError("candidate report work_items must be an object array")
    status = str(value.get("status") or "")
    if status not in {"candidate_ready", "architecture_defect", "module_split_request"}:
        raise ValueError("candidate report has invalid status")
    _validate_reported_changed_paths(value.get("files_changed"), work_view=work_view)
    if status != "candidate_ready":
        if not str(value.get("summary") or "").strip():
            raise ValueError(f"status={status} requires summary")
        if str(value.get("affected_module") or "") != _bound_unit_name(work_view):
            raise ValueError("candidate defect must target the bound module")
        return ()
    return ()


def _live_worktree_delta(workspace: Mapping[str, Any], *, work_view: Mapping[str, Any]) -> list[str]:
    raw_repo_path = str(workspace.get("repo_path") or "").strip()
    if not raw_repo_path:
        return []
    repo_path = Path(raw_repo_path).expanduser()
    if _is_artifact_unit_view(work_view):
        if not repo_path.is_dir():
            return []
        return sorted(
            path.relative_to(repo_path).as_posix()
            for path in repo_path.rglob("*")
            if path.is_file() and ".git" not in path.parts
        )
    if not repo_path.is_dir() or not (repo_path / ".git").exists():
        return []
    tracked = _git_paths(repo_path, "diff", "--name-only", "--no-renames", "-z", "HEAD", "--")
    untracked = _git_paths(repo_path, "ls-files", "--others", "--exclude-standard", "-z")
    actual = sorted(set(tracked + untracked))
    _validate_reported_changed_paths(actual, work_view=work_view)
    return actual


def _validate_reported_changed_paths(value: Any, *, work_view: Mapping[str, Any]) -> None:
    if _is_artifact_unit_view(work_view):
        return
    scopes = list(compiled_module_write_scopes(work_view))
    outside: list[str] = []
    frozen_contracts = (
        {
            str(item).replace("\\", "/")
            for item in list(work_view.get("contract_paths") or [])
        }
        if str(work_view.get("contract_mode") or "review_guarded") == "file_frozen"
        else set()
    )
    for raw in list(value or []):
        path = str(raw).replace("\\", "/").strip()
        parsed = PurePosixPath(path)
        normalized = str(parsed)
        if (
            not path
            or path.startswith("/")
            or ".." in parsed.parts
            or normalized in frozen_contracts
            or not any(_scope_matches(normalized, scope) for scope in scopes)
        ):
            outside.append(path or "<empty>")
    if outside:
        raise ValueError("Candidate Git delta is outside bound module write scopes: " + ", ".join(sorted(set(outside))))


def _git_paths(repo_path: Path, *args: str) -> list[str]:
    completed = subprocess.run(["git", "-C", str(repo_path), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if completed.returncode != 0:
        raise ValueError("candidate submit could not inspect Git delta: " + completed.stderr.decode("utf-8", errors="replace").strip())
    return [item.decode("utf-8", errors="surrogateescape").replace("\\", "/") for item in completed.stdout.split(b"\0") if item]


def _scope_matches(path: str, scope: Mapping[str, Any]) -> bool:
    target = str(scope.get("path") or "").replace("\\", "/").strip("/")
    return path == target if str(scope.get("kind") or "") == "file" else path == target or path.startswith(target + "/")


def _validate_defect_args(args: Mapping[str, Any], *, work_view: Mapping[str, Any]) -> None:
    del work_view
    if not str(args.get("summary") or "").strip():
        raise ValueError("summary is required")


def _is_software_module_view(work_view: Mapping[str, Any]) -> bool:
    return (
        bool(str(work_view.get("module_name") or "").strip())
        and str(work_view.get("execution_adapter") or "")
        != ARTIFACT_BUNDLE_ADAPTER
    )


def _is_artifact_unit_view(work_view: Mapping[str, Any]) -> bool:
    return (
        str(work_view.get("execution_adapter") or "")
        == ARTIFACT_BUNDLE_ADAPTER
        or (
            isinstance(work_view.get("unit_contract"), Mapping)
            and not _is_software_module_view(work_view)
        )
    )


def _bound_unit_name(work_view: Mapping[str, Any]) -> str:
    module_name = str(work_view.get("module_name") or "").strip()
    if module_name:
        return module_name
    contract = dict(work_view.get("unit_contract") or {})
    return str(
        work_view.get("module_name")
        or contract.get("name")
        or contract.get("unit_id")
        or ""
    ).strip()


def _defect_locations(args: Mapping[str, Any]) -> list[dict[str, str]]:
    path = str(args.get("path") or "").strip()
    return ([{"path": path, **({"symbol": str(args["symbol"])} if args.get("symbol") else {}), **({"section": str(args["contract_section"])} if args.get("contract_section") else {})}] if path else [])


def _bound_candidate_work_view(workspace: Mapping[str, Any]) -> dict[str, Any]:
    module_view = bound_reference_payload(
        workspace,
        "module_work_view",
        required=False,
    )
    if module_view:
        return module_view
    return bound_reference_payload(workspace, "unit_work_view")


def _ok(call: ToolCallIR, text: str, structured: Mapping[str, Any]) -> ToolExecutionResult:
    return ToolExecutionResult(name=call.name, ok=True, text=text, llm_text=text, structured=dict(structured), call_id=call.call_id, status=RuntimeStatus.OK)


def _error(call: ToolCallIR, exc: Exception) -> ToolExecutionResult:
    text = f"{exc.__class__.__name__}: {exc}"
    return ToolExecutionResult(name=call.name, ok=False, text=text, llm_text=text + " Correct only this issue and retry.", structured={"error": str(exc), "error_type": exc.__class__.__name__}, call_id=call.call_id, status=RuntimeStatus.INVALID)


def _rejected(
    call: ToolCallIR,
    exc: ToolRejectedError,
) -> ToolExecutionResult:
    result = rejection(
        exc.error_code,
        str(exc),
        retry=exc.retry,
        affordances=list(exc.affordances),
        details=dict(exc.details),
    )
    return ToolExecutionResult(
        name=call.name,
        ok=False,
        text=result.llm_text,
        llm_text=result.llm_text,
        structured=result.model_dump(mode="json"),
        call_id=call.call_id,
        status=result.error_code,
        invocation_result=result,
    )
