from __future__ import annotations

from pathlib import Path
from typing import Any

from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.shared import RuntimeStatus

from pal.minion.workspace_tools import _workspace_path, _workspace_root


WORKSPACE_FILE_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_workspace_file_read": {
        "name": "op_workspace_file_read",
        "description": (
            "Read a UTF-8 text file under workspace.repo_path using a workspace-relative path. "
            "A successful read also caches the file for op_workspace_file_edit, op_workspace_file_write overwrite/append, "
            "and op_workspace_file_delete safety checks."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path, for example src/app.py."},
                "start_line": {"type": "integer", "default": 1},
                "limit_lines": {"type": "integer", "default": 2000},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "op_workspace_file_edit": {
        "name": "op_workspace_file_edit",
        "description": (
            "Edit a UTF-8 text file under workspace.repo_path by replacing one exact old_string with new_string. "
            "The file must first be read with op_workspace_file_read so Pal can detect stale edits."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path, for example src/app.py."},
                "old_string": {"type": "string", "description": "Exact text to find and replace."},
                "new_string": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": False,
        },
    },
    "op_workspace_file_write": {
        "name": "op_workspace_file_write",
        "description": (
            "Create, overwrite, or append to a UTF-8 text file under workspace.repo_path. "
            "Create mode fails if the file exists. Overwrite and append require a current prior op_workspace_file_read snapshot."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path, for example tests/test_app.py."},
                "content": {"type": "string", "description": "UTF-8 text content to write."},
                "mode": {"type": "string", "enum": ["create", "overwrite", "append"], "default": "create"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    "op_workspace_file_delete": {
        "name": "op_workspace_file_delete",
        "description": (
            "Delete a regular file under workspace.repo_path. The file must first be read with op_workspace_file_read, "
            "or expected_sha256 must match the current file bytes."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path to delete."},
                "expected_sha256": {
                    "type": "string",
                    "description": "Optional current SHA-256 digest. If supplied, a prior op_workspace_file_read snapshot is not required.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}


async def workspace_file_tool_result(
    call: CanonicalToolCall,
    workspace: dict[str, Any],
    base_runtime: Any,
    *,
    allow_tools: bool = True,
    turn_id: str | None = None,
) -> CanonicalToolResult:
    try:
        root = _workspace_root(workspace)
        relative, absolute = _workspace_file_path(root, call.args)
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
        turn_id=turn_id,
    )
    return _workspace_result(call, result, relative=relative, absolute=absolute)


def _workspace_file_path(root: Path, args: dict[str, Any]) -> tuple[str, Path]:
    raw = str(args.get("path") or "").strip()
    if not raw:
        raise ValueError("path is required")
    path = _workspace_path(root, raw)
    if path == root:
        raise ValueError("workspace file path must name a file")
    return str(path.relative_to(root)).replace("\\", "/"), path


def _underlying_file_tool(call: CanonicalToolCall, absolute: Path) -> tuple[str, dict[str, Any]]:
    args = dict(call.args or {})
    if call.name == "op_workspace_file_read":
        return (
            "op_file_read",
            {
                "file_path": str(absolute),
                "offset": args.get("start_line") or args.get("offset") or 1,
                "limit": args.get("limit_lines") or args.get("limit") or 2000,
            },
        )
    if call.name == "op_workspace_file_edit":
        return (
            "op_file_edit",
            {
                "file_path": str(absolute),
                "old_string": args.get("old_string", ""),
                "new_string": args.get("new_string", ""),
            },
        )
    if call.name == "op_workspace_file_write":
        return (
            "op_file_write",
            {
                "file_path": str(absolute),
                "content": args.get("content"),
                "mode": args.get("mode") or "create",
            },
        )
    if call.name == "op_workspace_file_delete":
        tool_args: dict[str, Any] = {"file_path": str(absolute)}
        if str(args.get("expected_sha256") or "").strip():
            tool_args["expected_sha256"] = str(args.get("expected_sha256") or "").strip()
        return "op_file_delete", tool_args
    raise ValueError(f"unknown workspace file tool: {call.name}")


def _workspace_result(call: CanonicalToolCall, result: CanonicalToolResult, *, relative: str, absolute: Path) -> CanonicalToolResult:
    structured = dict(result.structured or {})
    structured["path"] = relative
    structured["workspace_path"] = relative
    structured.setdefault("file_path", str(absolute))
    text = str(result.llm_text or result.text or "").strip()
    if result.ok and call.name == "op_workspace_file_delete":
        text = f"Deleted workspace file: {relative}"
    return CanonicalToolResult(
        name=call.name,
        ok=result.ok,
        text=text,
        structured=structured,
        call_id=call.call_id,
        llm_text=text,
        status=result.status,
    )
