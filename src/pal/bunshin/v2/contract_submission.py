from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR

from pathlib import Path
from typing import Any, Mapping

from pal.execution.tool_facade import EmptyToolInput, rejection
from pal.bunshin.v2.contract_protocol import (
    ARCHITECT_FILENAME,
    read_architect_yaml,
)
from pal.bunshin.v2.submission_drafts import (
    SubmissionDraftContext,
    SubmissionDraftStore,
)
from pal.bunshin.v2.workspace_paths import MANAGER_ARCHITECT_DIRECTORY
from pal.bunshin.v2.work_items import assert_work_items_complete
from pal.shared import RuntimeStatus, ToolExecutionResult


CONTRACT_SUBMIT_CAPABILITY = "op_bunshin_contract_submit"

CONTRACT_SUBMIT_TOOL_SPEC: dict[str, Any] = {
    "alias": "contract_submit",
    "guidance": {
        "purpose": "Submit the Manager-preseeded architect.yaml for independent semantic review.",
        "use_when": (
            "Use with no arguments after the fixed role playbook and checklist are complete "
            "and declarations agree with architect.yaml."
        ),
        "do_not_use_when": (
            "Do not use with unfinished checklist work, unreconciled declarations, or a "
            "known schema or graph defect."
        ),
        "failure_next_steps": (
            "Correct all reported checklist, schema, graph, and bound-file defects in "
            "place, then retry once the submitted contract is mechanically valid."
        ),
    },
    "InputModel": EmptyToolInput,
    "examples": (),
    "idempotency": "idempotent",
    "retry_policy": "automatic",
}


def bind_architect_file(
    workspace: Mapping[str, Any],
    *,
    template: str,
    base_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = dict(workspace)
    root = _architect_root(result)
    path = root / ARCHITECT_FILENAME
    root.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        if base_contract is not None:
            import yaml

            text = yaml.safe_dump(
                dict(base_contract),
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            )
        else:
            text = str(template)
        path.write_text(text, encoding="utf-8")
    elif not path.is_file():
        raise ValueError("architect.yaml path is not a file")
    result["architect_path"] = str(path)
    return result


def contract_submit_tool_result(
    call: ToolCallIR,
    workspace: Mapping[str, Any],
) -> ToolExecutionResult:
    try:
        if dict(call.args or {}):
            raise ValueError("contract_submit takes no arguments")
        assert_work_items_complete(workspace)
        context = SubmissionDraftContext.from_workspace(
            workspace,
            draft_kind="contract",
        )
        store = SubmissionDraftStore(_runtime_root(workspace))
        if not store.uses_role_gateway:
            raise ValueError(
                "contract_submit requires the assignment-scoped Manager gateway"
            )
        snapshot = store.read(context, seed={})
        payload = {
            "source": ARCHITECT_FILENAME,
            "architecture": read_architect_yaml(
                architect_path(workspace)
            ),
        }
        result = store.mark_submitted(
            context,
            expected_version=snapshot.version,
            submission_payload=payload,
        )
        text = (
            "architect.yaml was accepted by the Manager validator and "
            "submitted for semantic review."
        )
        return ToolExecutionResult(
            name=call.name,
            ok=True,
            text=text,
            llm_text=text,
            structured=dict(result),
            call_id=call.call_id,
            status=RuntimeStatus.OK,
        )
    except Exception as exc:
        text = f"{exc.__class__.__name__}: {exc}"
        llm_text = (
            text
            + " Correct all reported contract/checklist defects before retrying."
        )
        return ToolExecutionResult(
            name=call.name,
            ok=False,
            text=text,
            llm_text=llm_text,
            structured={
                "error": str(exc),
                "error_type": exc.__class__.__name__,
            },
            call_id=call.call_id,
            status=RuntimeStatus.INVALID,
            invocation_result=rejection(
                "invalid_contract_submission",
                llm_text,
                details={
                    "error": str(exc),
                    "error_type": exc.__class__.__name__,
                },
            ),
        )


def architect_path(workspace: Mapping[str, Any]) -> Path:
    explicit = str(workspace.get("architect_path") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    return (_architect_root(workspace) / ARCHITECT_FILENAME).resolve()


def _architect_root(workspace: Mapping[str, Any]) -> Path:
    if bool(workspace.get("contract_authoring_mode")):
        for key in (
            "repo_path",
            "worktree_path",
            "workspace_path",
            "root",
            "path",
        ):
            value = str(workspace.get(key) or "").strip()
            if value:
                return (
                    Path(value).expanduser().resolve()
                    / MANAGER_ARCHITECT_DIRECTORY
                )
    # Artifact-family roles author inside the isolated role workspace exposed
    # to the worker.  Once that workspace exists, the Manager-preseeded file,
    # workspace tools, and contract_submit must all name the same projection.
    # Falling through to artifact_stage_dir here creates two architect.yaml
    # files: the worker edits repo_path/architect.yaml while submission keeps
    # validating the untouched stage template.
    if bool(workspace.get("v2_role_workspace")):
        for key in (
            "repo_path",
            "worktree_path",
            "workspace_path",
            "root",
            "path",
        ):
            value = str(workspace.get(key) or "").strip()
            if value:
                return Path(value).expanduser().resolve()
    keys = (
        "artifact_stage_dir",
        "artifact_dir",
        "run_dir",
        "repo_path",
        "worktree_path",
        "workspace_path",
        "root",
        "path",
    )
    for key in keys:
        value = str(workspace.get(key) or "").strip()
        if value:
            return Path(value).expanduser().resolve()
    raise ValueError("contract authoring workspace has no writable root")


def _runtime_root(workspace: Mapping[str, Any]) -> Path:
    value = str(workspace.get("runtime_root") or "").strip()
    if not value:
        raise ValueError("contract submission requires runtime_root")
    return Path(value)
