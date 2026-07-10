from __future__ import annotations

from pathlib import Path
from typing import Any

from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.shared import RuntimeStatus

from pal.minion.workspace_tools import _workspace_path, _workspace_root_payload, _workspace_root_with_info


WORKSPACE_FILE_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_file_read": {
        "name": "op_file_read",
        "description": (
            "Use this first when you need to inspect a UTF-8 text file under the current project repo or a declared read-only reference root; do not use op_exec_shell with cat/head/tail for repo/reference file reads when this tool is visible. "
            "Read using a root-relative path. Use root='reference:<name>' or reference_name='<name>' for declared truth-source references; omit it for the current project repo. "
            "A successful read also caches the file for op_file_edit, op_file_write overwrite/append, "
            "and op_path_delete safety checks."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Root-relative file path, for example src/app.py."},
                "root": {
                    "type": "string",
                    "description": "Optional root selector. Omit or use project for the current project repo; use reference:<name> for a declared read-only truth-source reference.",
                },
                "reference_name": {"type": "string", "description": "Optional declared reference root name; equivalent to root=reference:<name>."},
                "start_line": {"type": "integer", "default": 1},
                "limit_lines": {"type": "integer", "default": 2000},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "op_file_edit": {
        "name": "op_file_edit",
        "description": (
            "Use this first for precise repo text edits; do not use op_exec_shell with sed/awk/python one-liners for file edits when this tool is visible. "
            "Edit a UTF-8 text file under the current project repo by replacing one exact old_string with new_string. "
            "The file must first be read with op_file_read so Pal can detect stale edits."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo-relative file path, for example src/app.py."},
                "old_string": {"type": "string", "description": "Exact text to find and replace."},
                "new_string": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": False,
        },
    },
    "op_file_write": {
        "name": "op_file_write",
        "description": (
            "Use this first for creating, overwriting, or appending UTF-8 text files under the current project repo; do not use op_exec_shell with tee/echo/printf redirection when this tool is visible. "
            "Create, overwrite, or append to a UTF-8 text file under the current project repo. "
            "Create mode fails if the file exists. Overwrite and append require a current prior op_file_read snapshot."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo-relative file path, for example tests/test_app.py."},
                "content": {"type": "string", "description": "UTF-8 text content to write."},
                "mode": {"type": "string", "enum": ["create", "overwrite", "append"], "default": "create"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    "op_path_delete": {
        "name": "op_path_delete",
        "description": (
            "Use this first for deleting a file or directory under the current project repo; do not use op_exec_shell with rm/unlink/rmdir/git rm/find -delete when this tool is visible. "
            "Regular files must first be read with op_file_read, "
            "or expected_sha256 must match the current file bytes. Directories require recursive=true."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo-relative path to delete."},
                "expected_sha256": {
                    "type": "string",
                    "description": "Optional current SHA-256 digest for regular files. If supplied, a prior op_file_read snapshot is not required.",
                },
                "recursive": {
                    "type": "boolean",
                    "default": False,
                    "description": "Required for directory deletion. Regular file deletion does not require this.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "op_file_state": {
        "name": "op_file_state",
        "description": (
            "Use this first when you need to check read-before-edit state for a repo-relative file. "
            "Inspect whether a repo-relative file has a current cached read snapshot for safe op_file_edit, "
            "op_file_write overwrite/append, or op_path_delete use. This does not return cached file contents."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo-relative file path, for example src/app.py."},
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
    budget: Any = None,
    turn_id: str | None = None,
) -> CanonicalToolResult:
    try:
        root, root_info = _workspace_root_with_info(workspace, call.args)
        if str(root_info.get("root_kind") or "") == "reference" and call.name != "op_file_read":
            raise ValueError("reference roots are read-only; use op_file_read, op_tree, or op_search for reference inspection")
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
                "offset": args.get("start_line") or args.get("offset") or 1,
                "limit": args.get("limit_lines") or args.get("limit") or 2000,
            },
        )
    if call.name == "op_file_edit":
        return (
            "op_file_edit",
            {
                "file_path": str(absolute),
                "old_string": args.get("old_string", ""),
                "new_string": args.get("new_string", ""),
            },
        )
    if call.name == "op_file_write":
        return (
            "op_file_write",
            {
                "file_path": str(absolute),
                "content": args.get("content"),
                "mode": args.get("mode") or "create",
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
