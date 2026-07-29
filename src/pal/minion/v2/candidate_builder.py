from __future__ import annotations

from pal.execution.generated_tool_models import (
    MinionV2CandidateBuilderOpMinionCandidateReportArchitectureDefectInput,
    MinionV2CandidateBuilderOpMinionCandidateRequestModuleSplitInput,
    MinionV2CandidateBuilderOpMinionCandidateSubmitInput,
)

import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, Mapping

from pydantic import Field, model_validator
from pal.execution.tool_facade import (
    RetryDirective,
    StrictToolModel,
    ToolAffordance,
    ToolRejectedError,
    rejection,
)
from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.minion.v2.skeleton import compiled_module_write_scopes
from pal.minion.v2.submission_drafts import (
    SubmissionDraftContext,
    SubmissionDraftStore,
    assert_authoring_schema_budget,
)
from pal.minion.v2.submission_preflight import bound_reference_payload
from pal.minion.workspace_tools import _append_unique_artifact, _write_minion_artifact
from pal.shared import RuntimeStatus


CANDIDATE_BUILDER_CAPABILITIES = (
    "op_minion_candidate_update_checklist",
    "op_minion_candidate_submit",
    "op_minion_candidate_report_architecture_defect",
    "op_minion_candidate_request_module_split",
)


_ChecklistStep = Annotated[str, Field(min_length=1, max_length=240)]


class MinionV2CandidateChecklistItem(StrictToolModel):
    """One Coder micro-plan item with exactly one current state."""

    step: _ChecklistStep
    status: Literal["pending", "in_progress", "completed"]


class MinionV2CandidateUpdateChecklistInput(StrictToolModel):
    """Complete replacement for the Coder's durable, evidence-free micro-plan."""

    plan: list[MinionV2CandidateChecklistItem] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_plan(self) -> "MinionV2CandidateUpdateChecklistInput":
        steps = [item.step for item in self.plan]
        duplicates = sorted({step for step in steps if steps.count(step) > 1})
        if duplicates:
            raise ValueError(
                "Coder checklist steps must be unique: " + ", ".join(duplicates)
            )
        in_progress = [item.step for item in self.plan if item.status == "in_progress"]
        if len(in_progress) > 1:
            raise ValueError(
                "Coder checklist allows at most one in_progress item: "
                + ", ".join(in_progress)
            )
        return self


_CHECKLIST_EXAMPLE = {
    "plan": [
        {
            "step": "implement split-header buffering",
            "status": "completed",
        },
        {
            "step": "cover incomplete EOF at the CLI boundary",
            "status": "completed",
        },
    ],
}

CANDIDATE_BUILDER_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_minion_candidate_update_checklist": {
        "alias": "update_checklist",
        "description": (
            "Replace your complete durable Coder micro-plan. Each unique plan item has exactly "
            "one status: pending, in_progress, or completed; at most one item may be in_progress. "
            "Keep steps short and update only when work state materially changes. candidate_submit "
            "is a terminal tool call, never a checklist item. Before calling candidate_submit, "
            "mark every plan item completed as shown in the valid example. The checklist is a "
            "self-reported reminder, never test evidence or proof of correctness."
        ),
        "InputModel": MinionV2CandidateUpdateChecklistInput,
        "examples": (_CHECKLIST_EXAMPLE,),
        "idempotency": "idempotent",
        "retry_policy": "automatic",
    },
    "op_minion_candidate_submit": {
        "alias": "candidate_submit",
        "description": (
            "Submit the current module Candidate after every update_checklist plan item is completed "
            "and the implementation is ready for independent verification. The checklist is a "
            "self-reported work ledger, not proof. candidate_submit itself must never be a "
            "checklist item. Takes no arguments; Manager derives Git delta and journal fields."
        ),
        "InputModel": MinionV2CandidateBuilderOpMinionCandidateSubmitInput,
    },
    "op_minion_candidate_report_architecture_defect": {
        "alias": "candidate_report_architecture_defect",
        "description": "Terminally report that the frozen architecture contract cannot satisfy the task. Explain the semantic conflict and optionally cite a task source filename or code location.",
        "InputModel": MinionV2CandidateBuilderOpMinionCandidateReportArchitectureDefectInput,
    },
    "op_minion_candidate_request_module_split": {
        "alias": "candidate_request_module_split",
        "description": "Terminally request an architecture-owned module split when the bound module cannot fit one Candidate cycle.",
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
    call: CanonicalToolCall,
    workspace: dict[str, Any],
    produced_artifacts: list[dict[str, Any]],
) -> CanonicalToolResult:
    name = str(call.name or "")
    if name == "op_minion_candidate_update_checklist":
        try:
            return _replace_checklist(call, workspace)
        except ToolRejectedError as exc:
            return _rejected(call, exc)
        except Exception as exc:
            return _error(call, exc)
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


def _replace_checklist(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
) -> CanonicalToolResult:
    args = dict(call.args or {})
    checklist = _normalize_checklist(args, require_nonempty=True)
    context = SubmissionDraftContext.from_workspace(workspace, draft_kind="candidate")
    store = SubmissionDraftStore(Path(str(workspace["runtime_root"])))

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        payload["checklist"] = checklist
        return payload, {
            "updated": True,
            "checklist": checklist,
            "unfinished_count": len(
                [
                    item
                    for item in checklist["plan"]
                    if item["status"] != "completed"
                ]
            ),
        }

    result = store.mutate(
        context,
        operation_key=str(
            call.call_id
            or "replace-candidate-checklist:"
            + hashlib.sha256(
                json.dumps(
                    args,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        ),
        request=args,
        reducer=reducer,
        seed=_empty_candidate_payload(),
    )
    return _ok(call, _render_checklist(checklist), result)


def _submit_candidate(
    call: CanonicalToolCall,
    workspace: Mapping[str, Any],
    produced_artifacts: list[dict[str, Any]],
    *,
    status: str,
) -> CanonicalToolResult:
    args = dict(call.args or {})
    context = SubmissionDraftContext.from_workspace(workspace, draft_kind="candidate")
    store = SubmissionDraftStore(Path(str(workspace["runtime_root"])))
    snapshot = store.read(context, seed=_empty_candidate_payload())
    work_view = bound_reference_payload(workspace, "unit_work_view")
    raw_checklist = snapshot.payload.get("checklist")
    if status == "candidate_ready":
        if not isinstance(raw_checklist, Mapping):
            raise ToolRejectedError(
                "candidate checklist is not initialized; create the micro-plan before submission",
                error_code="checklist_required",
                affordances=[_initialize_checklist_affordance()],
            )
        try:
            checklist = _normalize_checklist(
                raw_checklist,
                require_nonempty=True,
            )
        except ValueError as exc:
            raise ToolRejectedError(
                f"candidate checklist is invalid: {exc}",
                error_code="checklist_invalid",
                affordances=[_initialize_checklist_affordance()],
            ) from exc
        if args:
            raise ToolRejectedError(
                "candidate_submit takes no arguments",
                error_code="invalid_arguments",
            )
        unfinished = [
            str(item["step"])
            for item in checklist["plan"]
            if item["status"] != "completed"
        ]
        if unfinished:
            raise ToolRejectedError(
                "candidate checklist still has unfinished work: " + ", ".join(unfinished),
                error_code="checklist_unfinished",
                affordances=[
                    ToolAffordance(
                        tool="update_checklist",
                        arguments={
                            "plan": [
                                {
                                    "step": str(item["step"]),
                                    "status": "completed",
                                }
                                for item in checklist["plan"]
                            ]
                        },
                        reason=(
                            "Update the existing micro-plan states; do not add candidate_submit "
                            "as a plan item."
                        ),
                    )
                ],
            )
    else:
        checklist = _normalize_checklist(
            raw_checklist if isinstance(raw_checklist, Mapping) else {"plan": []},
            require_nonempty=False,
        )
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
        "checklist": checklist,
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
        "checklist",
        "files_changed",
        "status",
    }
    missing = required - set(value)
    if missing:
        raise ValueError("candidate report is missing Manager fields: " + ", ".join(sorted(missing)))
    for field in required - {"status", "checklist"}:
        if not isinstance(value.get(field), list) or any(not isinstance(item, str) for item in value[field]):
            raise ValueError(f"candidate report {field} must be a string array")
    checklist = _normalize_checklist(value.get("checklist"), require_nonempty=False)
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
    if any(item["status"] != "completed" for item in checklist["plan"]):
        raise ValueError(
            "candidate_ready requires every checklist plan item to be completed"
        )
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
    return bool(str(work_view.get("module_name") or "").strip())


def _is_artifact_unit_view(work_view: Mapping[str, Any]) -> bool:
    return isinstance(work_view.get("unit_contract"), Mapping) and not _is_software_module_view(
        work_view
    )


def _bound_unit_name(work_view: Mapping[str, Any]) -> str:
    module_name = str(work_view.get("module_name") or "").strip()
    if module_name:
        return module_name
    contract = dict(work_view.get("unit_contract") or {})
    return str(contract.get("name") or contract.get("unit_id") or "").strip()


def _defect_locations(args: Mapping[str, Any]) -> list[dict[str, str]]:
    path = str(args.get("path") or "").strip()
    return ([{"path": path, **({"symbol": str(args["symbol"])} if args.get("symbol") else {}), **({"section": str(args["contract_section"])} if args.get("contract_section") else {})}] if path else [])


def _empty_candidate_payload() -> dict[str, Any]:
    return {}


def candidate_checklist_context(workspace: Mapping[str, Any]) -> str:
    """Render the latest fenced Coder checklist for prompt assembly."""

    metadata = dict(workspace.get("minion_v2") or {})
    if str(metadata.get("role") or "") != "implementation":
        return ""
    try:
        context = SubmissionDraftContext.from_workspace(workspace, draft_kind="candidate")
        snapshot = SubmissionDraftStore(Path(str(workspace["runtime_root"]))).read(
            context,
            seed=_empty_candidate_payload(),
        )
    except (OSError, RuntimeError, ValueError):
        return ""
    raw = snapshot.payload.get("checklist")
    if not isinstance(raw, Mapping):
        return (
            "Coder checklist: not initialized. Use update_checklist to record a short "
            "plan whose items each have pending / in_progress / completed status."
        )
    return _render_checklist(_normalize_checklist(raw, require_nonempty=False))


def _normalize_checklist(
    value: Any,
    *,
    require_nonempty: bool,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("Coder checklist must be an object")
    raw_plan = value.get("plan")
    if not isinstance(raw_plan, list):
        raise ValueError("Coder checklist plan must be an array")
    if require_nonempty and not raw_plan:
        raise ValueError("Coder checklist must contain at least one step")
    plan: list[dict[str, str]] = []
    for index, raw_item in enumerate(raw_plan):
        if not isinstance(raw_item, Mapping):
            raise ValueError(f"Coder checklist plan[{index}] must be an object")
        unexpected = sorted(set(raw_item) - {"step", "status"})
        if unexpected:
            raise ValueError(
                f"Coder checklist plan[{index}] has unknown fields: "
                + ", ".join(unexpected)
            )
        step = str(raw_item.get("step") or "").strip()
        status = str(raw_item.get("status") or "").strip()
        if not step:
            raise ValueError(f"Coder checklist plan[{index}].step must be non-empty")
        if status not in {"pending", "in_progress", "completed"}:
            raise ValueError(
                f"Coder checklist plan[{index}].status must be pending, in_progress, or completed"
            )
        plan.append({"step": step, "status": status})
    steps = [item["step"] for item in plan]
    duplicates = sorted({step for step in steps if steps.count(step) > 1})
    if duplicates:
        raise ValueError("Coder checklist steps must be unique: " + ", ".join(duplicates))
    in_progress = [item["step"] for item in plan if item["status"] == "in_progress"]
    if len(in_progress) > 1:
        raise ValueError(
            "Coder checklist allows at most one in_progress item: "
            + ", ".join(in_progress)
        )
    return {"plan": plan}


def _render_checklist(checklist: Mapping[str, Any]) -> str:
    lines = ["Coder checklist (self-reported micro-plan; not verification evidence):"]
    for item in list(checklist.get("plan") or []):
        entry = dict(item or {})
        lines.append(f"- {entry.get('status')}: {entry.get('step')}")
    if len(lines) == 1:
        lines.append("- (empty)")
    return "\n".join(lines)


def _initialize_checklist_affordance() -> ToolAffordance:
    return ToolAffordance(
        tool="update_checklist",
        arguments={
            "plan": [
                {
                    "step": "implement the bound module contract",
                    "status": "pending",
                }
            ]
        },
        reason="Create a short implementation micro-plan before submitting the Candidate.",
    )


def _ok(call: CanonicalToolCall, text: str, structured: Mapping[str, Any]) -> CanonicalToolResult:
    return CanonicalToolResult(name=call.name, ok=True, text=text, llm_text=text, structured=dict(structured), call_id=call.call_id, status=RuntimeStatus.OK)


def _error(call: CanonicalToolCall, exc: Exception) -> CanonicalToolResult:
    text = f"{exc.__class__.__name__}: {exc}"
    return CanonicalToolResult(name=call.name, ok=False, text=text, llm_text=text + " Correct only this issue and retry.", structured={"error": str(exc), "error_type": exc.__class__.__name__}, call_id=call.call_id, status=RuntimeStatus.INVALID)


def _rejected(
    call: CanonicalToolCall,
    exc: ToolRejectedError,
) -> CanonicalToolResult:
    result = rejection(
        exc.error_code,
        str(exc),
        retry=exc.retry,
        affordances=list(exc.affordances),
        details=dict(exc.details),
    )
    return CanonicalToolResult(
        name=call.name,
        ok=False,
        text=result.llm_text,
        llm_text=result.llm_text,
        structured=result.model_dump(mode="json"),
        call_id=call.call_id,
        status=result.error_code,
        invocation_result=result,
    )
