from __future__ import annotations
from pal.bunshin.workspace_paths import path_scope_matches

from pal.shared.tool_protocol import ToolCallIR

from pal.execution.generated_tool_models import (
    BunshinV2CandidateBuilderOpBunshinCandidateReportArchitectureDefectInput,
    BunshinV2CandidateBuilderOpBunshinCandidateRequestModuleSplitInput,
    BunshinV2CandidateBuilderOpBunshinCandidateSubmitInput,
)

import json
from dataclasses import dataclass
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from pal.execution.tool_facade import (
    ToolAffordance,
    ToolRejectedError,
    rejection,
)
from pal.bunshin.adapters import ARTIFACT_BUNDLE_ADAPTER
from pal.bunshin.skeleton import compiled_module_write_scopes
from pal.bunshin.submission_drafts import (
    SubmissionDraftContext,
    SubmissionDraftStore,
    assert_authoring_schema_budget,
)
from pal.bunshin.submission_errors import submission_error_result, submission_validation
from pal.bunshin.submission_preflight import bound_reference_payload
from pal.bunshin.work_items import (
    assert_work_items_complete,
    read_work_items,
    submission_work_items,
)
from pal.bunshin.workspace_tools import _append_unique_artifact, _write_bunshin_artifact
from pal.shared import RuntimeStatus, ToolExecutionResult


CANDIDATE_BUILDER_CAPABILITIES = (
    "op_bunshin_candidate_submit",
    "op_bunshin_candidate_report_architecture_defect",
    "op_bunshin_candidate_request_module_split",
    "op_bunshin_candidate_report_task_blocker",
)


CANDIDATE_BUILDER_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_bunshin_candidate_submit": {
        "alias": "submit_candidate",
        "guidance": {
            "search_objects": ('candidate', 'candidates'),
            "purpose": "Submit the current module Candidate for independent verification.",
            "use_when": (
                "Use after every checklist item is completed, focused checks pass, and the "
                "implementation is ready for independent verification. Takes no arguments; "
                "the Manager derives Git delta and journal fields. A software module may "
                "submit an unchanged baseline when it already satisfies the bound contract "
                "and focused checks pass; do not manufacture changes for submission."
            ),
            "do_not_use_when": (
                "Do not use with unfinished checklist work or when the correct terminal "
                "outcome is an architecture defect or module split. An artifact-bundle unit "
                "must have at least one contracted product file before submission. "
                "Never add submit_candidate itself to the checklist."
            ),
            "failure_next_steps": (
                "Follow the returned recovery affordance, correct the checklist or workspace "
                "state, and do not repeat the same rejected submission unchanged."
            ),
        },
        "InputModel": BunshinV2CandidateBuilderOpBunshinCandidateSubmitInput,
    },
    "op_bunshin_candidate_report_architecture_defect": {
        "alias": "report_candidate_architecture_defect",
        "guidance": {
            "search_objects": ('defect', 'defects'),
            "purpose": "Terminally report that the frozen architecture contract cannot satisfy the task.",
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
        "InputModel": BunshinV2CandidateBuilderOpBunshinCandidateReportArchitectureDefectInput,
    },
    "op_bunshin_candidate_request_module_split": {
        "alias": "request_candidate_module_split",
        "guidance": {
            "search_objects": ('split', 'splits'),
            "purpose": "Terminally request an architecture-owned module split.",
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
        "InputModel": BunshinV2CandidateBuilderOpBunshinCandidateRequestModuleSplitInput,
    },
}

CANDIDATE_BUILDER_TOOL_SPECS["op_bunshin_candidate_report_task_blocker"] = {
    "alias": "report_task_blocker",
    "guidance": {
        "purpose": "Return a conflicting or underspecified direct task to Pal for clarification.",
        "use_when": "The original task and confirmed decisions cannot be implemented without a decision from Pal. Explain the conflict and the exact question.",
        "do_not_use_when": "Ordinary debugging, test failures, or implementation choices within the task remain the coder's responsibility.",
        "failure_next_steps": "Correct the blocker explanation and resubmit.",
        "search_objects": ("task", "blocker"),
    },
    "InputModel": BunshinV2CandidateBuilderOpBunshinCandidateReportArchitectureDefectInput,
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


@dataclass
class _SubmissionProgress:
    started: bool = False
    accepted: bool = False


async def candidate_builder_tool_result(
    call: ToolCallIR,
    workspace: dict[str, Any],
    produced_artifacts: list[dict[str, Any]],
) -> ToolExecutionResult:
    name = str(call.name or "")
    progress = _SubmissionProgress()
    try:
        if name == "op_bunshin_candidate_report_task_blocker":
            if workspace.get("execution_mode") != "direct":
                raise ValueError("task blocker is only available in direct mode")
            return _submit_candidate(call, workspace, produced_artifacts, progress=progress, status="task_blocked")
        if name == "op_bunshin_candidate_submit":
            return _submit_candidate(call, workspace, produced_artifacts, progress=progress, status="candidate_ready")
        if name == "op_bunshin_candidate_report_architecture_defect":
            return _submit_candidate(call, workspace, produced_artifacts, progress=progress, status="architecture_defect")
        if name == "op_bunshin_candidate_request_module_split":
            return _submit_candidate(call, workspace, produced_artifacts, progress=progress, status="module_split_request")
        raise ValueError(f"unknown candidate authoring capability: {name}")
    except ToolRejectedError as exc:
        return _rejected(call, exc)
    except Exception as exc:
        return submission_error_result(
            call, exc, submission_started=progress.started,
            submission_accepted=progress.accepted,
            invalid_code="invalid_candidate_submission",
            correction="Correct the reported candidate/checklist defect before retrying.",
        )


def _submit_candidate(
    call: ToolCallIR,
    workspace: Mapping[str, Any],
    produced_artifacts: list[dict[str, Any]],
    *,
    status: str,
    progress: _SubmissionProgress,
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
        if work_view.get("execution_mode") == "direct":
            repo = Path(str(workspace["repo_path"])).resolve()
            for relative in work_view.get("deliverable_paths", []):
                target = repo / relative
                if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(repo):
                    raise ValueError(f"required deliverable is missing or unsafe: {relative}")
        if args:
            raise ToolRejectedError(
                "submit_candidate takes no arguments",
                error_code="invalid_arguments",
            )
    else:
        work_items = {"items": []}
        with submission_validation():
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
            "submit_candidate requires at least one contracted product file in the artifact workspace",
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
    with submission_validation():
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
        progress.started = True
        store.mark_submitted(
            context,
            expected_version=snapshot.version,
            submission_payload=report,
        )
        progress.accepted = True
    # Local report writes may have effects even if their result is lost.
    progress.started = True
    artifact = _write_bunshin_artifact(
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
    if status not in {"candidate_ready", "architecture_defect", "module_split_request", "task_blocked"}:
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
    return path_scope_matches(path, scope)


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
