"""Canonical LLM-facing contracts for structured text file tools."""

from __future__ import annotations

from typing import Any


DEFAULT_FILE_READ_LIMIT = 2000

FILE_READ_DESCRIPTION = (
    "Read a UTF-8 text file and return line-numbered content. "
    "Use offset and limit for large files. Read the complete file before editing, "
    "overwriting, or deleting it so stale changes can be detected."
)

FILE_EDIT_DESCRIPTION = (
    "Replace an exact old_string in a UTF-8 text file after reading the complete file. "
    "The match must be unique unless replace_all=true. Returns a unified diff on success."
)

FILE_WRITE_DESCRIPTION = (
    "Write complete UTF-8 text content to a file. Missing files and parent directories are created; "
    "existing files are overwritten only after the complete file has been read. "
    "Use file_edit for focused changes to existing files."
)


def file_read_args_schema(*, scoped: bool = False) -> dict[str, Any]:
    path_field = "path" if scoped else "file_path"
    properties: dict[str, Any] = {
        path_field: {
            "type": "string",
            "description": (
                "Root-relative file path, for example src/app.py."
                if scoped
                else "Path to the file to read."
            ),
        },
        "offset": {
            "type": "integer",
            "minimum": 1,
            "description": "1-based line number to start reading from. Defaults to 1.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "description": f"Maximum number of lines to return. Defaults to {DEFAULT_FILE_READ_LIMIT}.",
        },
    }
    if scoped:
        properties.update(
            {
                "root": {
                    "type": "string",
                    "description": (
                        "Optional root selector. Omit or use project for the current project repo; "
                        "use reference:<name> for a declared read-only truth-source reference."
                    ),
                },
                "reference_name": {
                    "type": "string",
                    "description": (
                        "Optional declared reference root name; equivalent to root=reference:<name>."
                    ),
                },
            }
        )
    return {
        "type": "object",
        "properties": properties,
        "required": [path_field],
        "additionalProperties": False,
    }


def file_edit_args_schema(*, scoped: bool = False) -> dict[str, Any]:
    path_field = "path" if scoped else "file_path"
    return {
        "type": "object",
        "properties": {
            path_field: {
                "type": "string",
                "description": (
                    "Repo-relative file path, for example src/app.py."
                    if scoped
                    else "Path to the file to edit."
                ),
            },
            "old_string": {
                "type": "string",
                "description": "Exact text to find and replace.",
            },
            "new_string": {"type": "string", "description": "Replacement text."},
            "replace_all": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Replace every exact occurrence. Leave false to require one unique match."
                ),
            },
        },
        "required": [path_field, "old_string", "new_string"],
        "additionalProperties": False,
    }


def file_write_args_schema(*, scoped: bool = False) -> dict[str, Any]:
    path_field = "path" if scoped else "file_path"
    return {
        "type": "object",
        "properties": {
            path_field: {
                "type": "string",
                "description": (
                    "Repo-relative file path, for example tests/test_app.py."
                    if scoped
                    else "Path to create or overwrite."
                ),
            },
            "content": {
                "type": "string",
                "description": "Complete UTF-8 text content for the file.",
            },
        },
        "required": [path_field, "content"],
        "additionalProperties": False,
    }


FILE_READ_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string"},
        "content": {"type": "string"},
        "start_line": {"type": "integer"},
        "end_line": {"type": "integer"},
        "total_lines": {"type": "integer"},
        "truncated": {"type": "boolean"},
        "full_view": {"type": "boolean"},
        "unchanged": {"type": "boolean"},
        "encoding": {"type": "string"},
        "error_code": {"type": "string"},
    },
}

FILE_EDIT_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string"},
        "error_code": {"type": "string"},
        "patch": {"type": "string"},
        "match_count": {"type": "integer"},
    },
}

FILE_WRITE_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string"},
        "bytes_written": {"type": "integer"},
        "created": {"type": "boolean"},
        "operation": {"type": "string", "enum": ["create", "update"]},
        "patch": {"type": "string"},
        "encoding": {"type": "string"},
        "error_code": {"type": "string"},
    },
}
