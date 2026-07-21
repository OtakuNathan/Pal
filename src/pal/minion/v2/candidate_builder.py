from __future__ import annotations

from pal.execution.generated_tool_models import (
    MinionV2CandidateBuilderOpMinionCandidateReportArchitectureDefectInput,
    MinionV2CandidateBuilderOpMinionCandidateRequestModuleSplitInput,
    MinionV2CandidateBuilderOpMinionCandidateSubmitInput,
    MinionV2CandidateBuilderOpMinionDeveloperCheckUnavailableInput,
    MinionV2CandidateBuilderOpMinionDeveloperCompileCheckInput,
    MinionV2CandidateBuilderOpMinionDeveloperLspCheckInput,
    MinionV2CandidateBuilderOpMinionDeveloperNoteInput,
    MinionV2CandidateBuilderOpMinionDeveloperTestInput,
)

import json
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.minion.v2.semantic_evidence import (
    record_unavailable_evidence,
    recorded_cases,
    run_lsp_evidence,
    run_shell_evidence,
)
from pal.minion.v2.submission_drafts import (
    SubmissionDraftContext,
    SubmissionDraftStore,
    assert_authoring_schema_budget,
)
from pal.minion.v2.submission_preflight import bound_reference_payload
from pal.minion.workspace_tools import _append_unique_artifact, _write_minion_artifact
from pal.shared import RuntimeStatus


CANDIDATE_BUILDER_CAPABILITIES = (
    "op_minion_developer_note",
    "op_minion_developer_test",
    "op_minion_developer_compile_check",
    "op_minion_developer_lsp_check",
    "op_minion_developer_check_unavailable",
    "op_minion_candidate_submit",
    "op_minion_candidate_report_architecture_defect",
    "op_minion_candidate_request_module_split",
)

_NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["micro_plan", "completed", "file_inspected", "open_question", "known_failure"],
        },
        "text": {"type": "string", "minLength": 1},
    },
    "required": ["kind", "text"],
    "additionalProperties": False,
}
_RUN_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "command": {"type": "string", "minLength": 1},
        "description": {"type": "string"},
        "expected_exit_codes": {"type": "array", "items": {"type": "integer"}},
        "timeout_seconds": {"type": "integer", "minimum": 1},
    },
    "required": ["name", "command"],
    "additionalProperties": False,
}
_LSP_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "file": {"type": "string", "minLength": 1},
        "description": {"type": "string"},
    },
    "required": ["name", "file"],
    "additionalProperties": False,
}
_UNAVAILABLE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "obligation": {"type": "string", "enum": ["focused_tests", "compile", "warning_clean", "lsp"]},
        "reason": {"type": "string", "minLength": 1},
    },
    "required": ["name", "obligation", "reason"],
    "additionalProperties": False,
}
_DEFECT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "minLength": 1},
        "source_file": {"type": "string"},
        "path": {"type": "string"},
        "symbol": {"type": "string"},
        "contract_section": {"type": "string"},
    },
    "required": ["summary"],
    "additionalProperties": False,
}
_NO_ARGS_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}

CANDIDATE_BUILDER_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_minion_developer_note": {
        "alias": "developer_note",
        "description": "Record one durable local progress item. kind is micro_plan, completed, file_inspected, open_question, or known_failure.",
        "InputModel": MinionV2CandidateBuilderOpMinionDeveloperNoteInput,
    },
    "op_minion_developer_test": {
        "alias": "developer_test",
        "description": "Run and durably record one focused developer test. Provide a shell command, not a nested test report.",
        "InputModel": MinionV2CandidateBuilderOpMinionDeveloperTestInput,
    },
    "op_minion_developer_compile_check": {
        "alias": "developer_compile_check",
        "description": "Run and durably record one compile or warning-clean check. The command is executed inside the bound module sandbox.",
        "InputModel": MinionV2CandidateBuilderOpMinionDeveloperCompileCheckInput,
    },
    "op_minion_developer_lsp_check": {
        "alias": "developer_lsp_check",
        "description": "Run and durably record LSP diagnostics for one changed source file when a matching server is available.",
        "InputModel": MinionV2CandidateBuilderOpMinionDeveloperLspCheckInput,
    },
    "op_minion_developer_check_unavailable": {
        "alias": "developer_check_unavailable",
        "description": "Record why focused_tests, compile, warning_clean, or lsp cannot run in this environment. This records UNKNOWN, not PASS.",
        "InputModel": MinionV2CandidateBuilderOpMinionDeveloperCheckUnavailableInput,
    },
    "op_minion_candidate_submit": {
        "alias": "candidate_submit",
        "description": "Submit the current module Candidate after all developer checks pass. Takes no arguments; Manager derives Git delta and journal fields.",
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


def is_candidate_builder_capability(name: str) -> bool:
    return str(name or "") in CANDIDATE_BUILDER_TOOL_SPECS


async def candidate_builder_tool_result(
    call: CanonicalToolCall,
    workspace: dict[str, Any],
    produced_artifacts: list[dict[str, Any]],
    *,
    original_adapter: Any | None = None,
    turn_id: str | None = None,
) -> CanonicalToolResult:
    name = str(call.name or "")
    if name == "op_minion_developer_test":
        return await run_shell_evidence(
            call,
            workspace=workspace,
            original_adapter=_require_adapter(original_adapter),
            draft_kind="candidate",
            case_kind="unit",
            obligation_tag="focused_tests",
            turn_id=turn_id,
        )
    if name == "op_minion_developer_compile_check":
        return await run_shell_evidence(
            call,
            workspace=workspace,
            original_adapter=_require_adapter(original_adapter),
            draft_kind="candidate",
            case_kind="compile",
            obligation_tag="compile",
            turn_id=turn_id,
        )
    if name == "op_minion_developer_lsp_check":
        return await run_lsp_evidence(
            call,
            workspace=workspace,
            original_adapter=_require_adapter(original_adapter),
            draft_kind="candidate",
            turn_id=turn_id,
        )
    if name == "op_minion_developer_check_unavailable":
        return record_unavailable_evidence(call, workspace=workspace, draft_kind="candidate")
    try:
        if name == "op_minion_developer_note":
            return _record_note(call, workspace)
        if name == "op_minion_candidate_submit":
            return _submit_candidate(call, workspace, produced_artifacts, status="candidate_ready")
        if name == "op_minion_candidate_report_architecture_defect":
            return _submit_candidate(call, workspace, produced_artifacts, status="architecture_defect")
        if name == "op_minion_candidate_request_module_split":
            return _submit_candidate(call, workspace, produced_artifacts, status="module_split_request")
        raise ValueError(f"unknown candidate authoring capability: {name}")
    except Exception as exc:
        return _error(call, exc)


def _record_note(call: CanonicalToolCall, workspace: Mapping[str, Any]) -> CanonicalToolResult:
    args = dict(call.args or {})
    kind = str(args.get("kind") or "").strip()
    text = str(args.get("text") or "").strip()
    if kind not in {"micro_plan", "completed", "file_inspected", "open_question", "known_failure"} or not text:
        raise ValueError("developer note requires a valid kind and non-empty text")
    context = SubmissionDraftContext.from_workspace(workspace, draft_kind="candidate")
    store = SubmissionDraftStore(Path(str(workspace["runtime_root"])))

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        summary = dict(payload.get("summary") or {})
        values = list(summary.get(kind) or [])
        if text not in values:
            values.append(text)
        summary[kind] = values
        payload["summary"] = summary
        return payload, {"recorded": True, "kind": kind, "text": text}

    result = store.mutate(
        context,
        operation_key=str(call.call_id or f"note:{kind}:{text}"),
        request=args,
        reducer=reducer,
        seed=_empty_candidate_payload(),
    )
    return _ok(call, "developer note recorded", result)


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
    summary = dict(snapshot.payload.get("summary") or {})
    cases = recorded_cases(snapshot.payload)
    if status == "candidate_ready":
        if args:
            raise ValueError("candidate_submit takes no arguments")
        failures = [item for item in cases if str(item.get("status")) == "FAIL"]
        if failures:
            raise ValueError("developer checks still fail: " + ", ".join(str(item.get("name")) for item in failures))
        if not cases:
            raise ValueError("candidate_submit requires at least one recorded developer check")
    else:
        _validate_defect_args(args, work_view=work_view)
    files_changed = _live_worktree_delta(workspace, work_view=work_view)
    reserved_paths = {
        str(item).replace("\\", "/").strip().lstrip("./")
        for item in list(workspace.get("manager_owned_submission_paths") or [])
        if str(item).strip()
    }
    polluted = sorted(reserved_paths.intersection(files_changed))
    if polluted:
        raise ValueError(
            "candidate workspace contains Manager-owned submission files: "
            + ", ".join(polluted)
        )
    if status == "candidate_ready" and _is_artifact_unit_view(work_view) and not files_changed:
        raise ValueError(
            "candidate_submit requires at least one contracted product file in the artifact workspace"
        )
    report = {
        "current_micro_plan": list(summary.get("micro_plan") or []),
        "completed_checklist": list(summary.get("completed") or []),
        "files_inspected": list(summary.get("file_inspected") or []),
        "files_changed": files_changed,
        "tests_run": [f"{item.get('name')}: {item.get('status')}" for item in cases],
        "open_questions": list(summary.get("open_question") or []),
        "known_failures": list(summary.get("known_failure") or []),
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
        "current_micro_plan",
        "completed_checklist",
        "files_inspected",
        "files_changed",
        "tests_run",
        "open_questions",
        "known_failures",
        "status",
    }
    missing = required - set(value)
    if missing:
        raise ValueError("candidate report is missing Manager fields: " + ", ".join(sorted(missing)))
    for field in required - {"status"}:
        if not isinstance(value.get(field), list) or any(not isinstance(item, str) for item in value[field]):
            raise ValueError(f"candidate report {field} must be a string array")
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
    scopes = [
        dict(item or {})
        for item in [
            *list(work_view.get("implementation_scopes") or []),
            *list(work_view.get("test_scopes") or []),
        ]
    ]
    outside: list[str] = []
    for raw in list(value or []):
        path = str(raw).replace("\\", "/").strip()
        parsed = PurePosixPath(path)
        normalized = str(parsed)
        if not path or path.startswith("/") or ".." in parsed.parts or not any(_scope_matches(normalized, scope) for scope in scopes):
            outside.append(path or "<empty>")
    if outside:
        raise ValueError("Candidate Git delta is outside bound implementation/test scopes: " + ", ".join(sorted(set(outside))))


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
    return {"definitions": {}, "evidence": {"cases": {}}, "findings": [], "summary": {}}


def _has_bound_reference(workspace: Mapping[str, Any], name: str) -> bool:
    return any(
        str(dict(item or {}).get("name") or "") == name
        for item in list(workspace.get("reference_paths") or [])
    )


def _require_adapter(adapter: Any | None) -> Any:
    if adapter is None:
        raise ValueError("developer check requires scoped execution")
    return adapter


def _ok(call: CanonicalToolCall, text: str, structured: Mapping[str, Any]) -> CanonicalToolResult:
    return CanonicalToolResult(name=call.name, ok=True, text=text, llm_text=text, structured=dict(structured), call_id=call.call_id, status=RuntimeStatus.OK)


def _error(call: CanonicalToolCall, exc: Exception) -> CanonicalToolResult:
    text = f"{exc.__class__.__name__}: {exc}"
    return CanonicalToolResult(name=call.name, ok=False, text=text, llm_text=text + " Correct only this issue and retry.", structured={"error": str(exc), "error_type": exc.__class__.__name__}, call_id=call.call_id, status=RuntimeStatus.INVALID)
