from __future__ import annotations

from pal.execution.generated_tool_models import (
    MinionWorkspaceFileToolsOpFileEditInput,
    MinionWorkspaceFileToolsOpFileReadInput,
    MinionWorkspaceFileToolsOpFileStateInput,
    MinionWorkspaceFileToolsOpFileWriteInput,
    MinionWorkspaceFileToolsOpPathDeleteInput,
)

from pathlib import Path
from typing import Any

from pal.execution.file_tool_contracts import (
    FILE_EDIT_DESCRIPTION,
    FILE_READ_DESCRIPTION,
    FILE_WRITE_DESCRIPTION,
)
from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.shared import RuntimeStatus

from pal.minion.workspace_tools import (
    _normalized_reference_paths,
    _workspace_path,
    _workspace_path_allowed_by_reference,
    _workspace_root_payload,
    _workspace_root_with_info,
)


WORKSPACE_FILE_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_file_read": {
        "alias": "read_file",
        "description": (
            "Use this instead of shell cat/head/tail for repo or reference files. "
            + FILE_READ_DESCRIPTION
            + " Paths are root-relative. Use root='reference:<name>' or reference_name='<name>' "
            "for declared read-only truth-source references; omit it for the project repo."
        ),
        "InputModel": MinionWorkspaceFileToolsOpFileReadInput,
    },
    "op_file_edit": {
        "alias": "edit_file",
        "description": (
            "Use this instead of shell sed/awk or one-off rewrite scripts for repo files. "
            + FILE_EDIT_DESCRIPTION
        ),
        "InputModel": MinionWorkspaceFileToolsOpFileEditInput,
    },
    "op_file_write": {
        "alias": "write_file",
        "description": (
            "Use this instead of shell redirection for project repo files. "
            + FILE_WRITE_DESCRIPTION
        ),
        "InputModel": MinionWorkspaceFileToolsOpFileWriteInput,
    },
    "op_path_delete": {
        "alias": "delete_path",
        "description": (
            "Use this first for deleting a file or directory under the current project repo; do not use run_shell with rm/unlink/rmdir/git rm/find -delete when this tool is visible. "
            "Regular files must first be read with read_file, "
            "or expected_sha256 must match the current file bytes. Directories require recursive=true."
        ),
        "InputModel": MinionWorkspaceFileToolsOpPathDeleteInput,
    },
    "op_file_state": {
        "alias": "file_state",
        "description": (
            "Use this first when you need to check read-before-edit state for a repo-relative file. "
            "Inspect whether a repo-relative file has a current cached read snapshot for safe edit_file, "
            "write_file, or delete_path use. This does not return cached file contents."
        ),
        "InputModel": MinionWorkspaceFileToolsOpFileStateInput,
    },
}


async def workspace_file_tool_result(
    call: CanonicalToolCall,
    workspace: dict[str, Any],
    base_runtime: Any,
    *,
    allow_tools: bool = True,
    budget: Any = None,
    turn_id: str | None = None,
) -> CanonicalToolResult:
    try:
        root, root_info = _workspace_root_with_info(workspace, call.args)
        if str(root_info.get("root_kind") or "") == "reference" and call.name != "op_file_read":
            raise ValueError("reference roots are read-only; use read_file, search, or run_shell for reference inspection")
        relative, absolute = _workspace_file_path(root, call.args)
        if not _workspace_path_allowed_by_reference(absolute, root, root_info):
            raise ValueError("path is outside the declared immutable input include set")
        if (
            str(root_info.get("root_kind") or "") != "reference"
            and call.name in {"op_file_edit", "op_file_write", "op_path_delete"}
            and _is_manager_owned_submission_path(workspace, relative)
        ):
            raise ValueError(
                f"path {relative!r} is Manager-owned; use the bound no-argument submit tool"
            )
        if (
            str(root_info.get("root_kind") or "") != "reference"
            and call.name in {"op_file_edit", "op_file_write", "op_path_delete"}
            and workspace.get("write_path_scopes")
            and not any(_write_scope_matches(relative, dict(item or {})) for item in list(workspace["write_path_scopes"]))
        ):
            raise ValueError("path is outside this module's writable implementation/test scopes")
        if (
            str(root_info.get("root_kind") or "") != "reference"
            and call.name in {"op_file_edit", "op_file_write", "op_path_delete"}
            and relative.replace("\\", "/")
            in {
                str(item).replace("\\", "/")
                for item in list(workspace.get("read_only_overlay_paths") or [])
            }
        ):
            raise ValueError(
                "path is a verifier-owned regression test; repair product code and rerun the test without editing it"
            )
        tool_name, tool_args = _underlying_file_tool(call, absolute)
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=message,
            structured={"error": message, "error_type": exc.__class__.__name__},
            call_id=call.call_id,
            llm_text=message,
            status=RuntimeStatus.ERROR,
        )

    result = await base_runtime.execute_tool_async(
        CanonicalToolCall(name=tool_name, args=tool_args, call_id=call.call_id),
        allow_tools=allow_tools,
        budget=budget,
        turn_id=turn_id,
    )
    return _workspace_result(call, result, relative=relative, absolute=absolute, root=root, root_info=root_info)


def _workspace_file_path(root: Path, args: dict[str, Any]) -> tuple[str, Path]:
    raw = str(args.get("path") or args.get("file_path") or "").strip()
    if not raw:
        raise ValueError("path is required")
    path = _workspace_path(root, raw)
    if path == root:
        raise ValueError("repo file path must name a file")
    return str(path.relative_to(root)).replace("\\", "/"), path


def _underlying_file_tool(call: CanonicalToolCall, absolute: Path) -> tuple[str, dict[str, Any]]:
    args = dict(call.args or {})
    if call.name == "op_file_read":
        return (
            "op_file_read",
            {
                "file_path": str(absolute),
                "offset": args.get("offset") or args.get("start_line") or 1,
                "limit": args.get("limit") or args.get("limit_lines") or 2000,
            },
        )
    if call.name == "op_file_edit":
        return (
            "op_file_edit",
            {
                "file_path": str(absolute),
                "old_string": args.get("old_string", ""),
                "new_string": args.get("new_string", ""),
                "replace_all": args.get("replace_all", False),
            },
        )
    if call.name == "op_file_write":
        return (
            "op_file_write",
            {
                "file_path": str(absolute),
                "content": args.get("content"),
            },
        )
    if call.name == "op_path_delete":
        tool_args: dict[str, Any] = {"file_path": str(absolute)}
        if str(args.get("expected_sha256") or "").strip():
            tool_args["expected_sha256"] = str(args.get("expected_sha256") or "").strip()
        if "recursive" in args:
            tool_args["recursive"] = bool(args.get("recursive"))
        return "op_path_delete", tool_args
    if call.name == "op_file_state":
        return "op_file_state", {"file_path": str(absolute)}
    raise ValueError(f"unknown scoped file tool: {call.name}")


def _write_scope_matches(path: str, scope: dict[str, Any]) -> bool:
    normalized = str(path).replace("\\", "/").strip("/")
    target = str(scope.get("path") or "").replace("\\", "/").strip("/")
    kind = str(scope.get("kind") or "")
    if not target:
        return False
    if kind == "file":
        return normalized == target
    if kind == "directory":
        return normalized == target or normalized.startswith(target + "/")
    if kind == "prefix":
        target_parent, _, target_name = target.rpartition("/")
        parent, _, name = normalized.rpartition("/")
        return parent == target_parent and name.startswith(target_name)
    return False


def _is_manager_owned_submission_path(workspace: dict[str, Any], path: str) -> bool:
    normalized = str(path or "").replace("\\", "/").strip().lstrip("./")
    return normalized in {
        str(item).replace("\\", "/").strip().lstrip("./")
        for item in list(workspace.get("manager_owned_submission_paths") or [])
        if str(item).strip()
    }


def _workspace_result(
    call: CanonicalToolCall,
    result: CanonicalToolResult,
    *,
    relative: str,
    absolute: Path,
    root: Path,
    root_info: dict[str, Any],
) -> CanonicalToolResult:
    structured = dict(result.structured or {})
    structured["path"] = relative
    structured["workspace_path"] = relative
    structured.setdefault("file_path", str(absolute))
    structured.update(_workspace_root_payload(root, root_info))
    text = str(result.llm_text or result.text or "").strip()
    if result.ok and call.name == "op_path_delete":
        path_kind = str(structured.get("path_kind") or "path")
        text = f"Deleted {path_kind}: {relative}"
    return CanonicalToolResult(
        name=call.name,
        ok=result.ok,
        text=text,
        structured=structured,
        call_id=call.call_id,
        llm_text=text,
        status=result.status,
    )
