from __future__ import annotations

import contextlib
import hashlib
import mimetypes
import os
from pathlib import Path
from typing import Any

from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.shared import RuntimeStatus

def _workspace_tool_result(call: CanonicalToolCall, workspace: dict[str, Any]) -> CanonicalToolResult:
    try:
        if call.name == "op_minion_artifact_write":
            artifact = _write_minion_artifact(workspace, call.args)
            payload = {"artifact": artifact}
            text = f"Artifact written: {artifact['relative_path']}"
        elif call.name == "op_minion_artifact_edit":
            artifact = _edit_minion_artifact(workspace, call.args)
            payload = {"artifact": artifact}
            text = f"Artifact edited: {artifact['relative_path']}"
        else:
            root = _workspace_root(workspace)
            if call.name == "op_workspace_tree":
                payload = _workspace_tree(root, call.args)
                text = "\n".join(item["path"] for item in payload["items"])
            elif call.name == "op_workspace_search":
                payload = _workspace_search(root, call.args)
                text = "\n".join(f"{item['path']}:{item['line_number']}: {item['line']}" for item in payload["matches"])
            elif call.name == "op_workspace_read":
                payload = _workspace_read(root, call.args)
                text = payload["text"]
            else:
                raise ValueError(f"unknown workspace tool: {call.name}")
        if not text.strip():
            text = _empty_workspace_tool_text(call.name, payload)
        return CanonicalToolResult(
            name=call.name,
            ok=True,
            text=text,
            structured=payload,
            call_id=call.call_id,
            llm_text=text,
            status=RuntimeStatus.OK,
        )
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


def _workspace_root(workspace: dict[str, Any]) -> Path:
    repo_path = str((workspace or {}).get("repo_path") or "").strip()
    if not repo_path:
        raise ValueError("workspace.repo_path is not available")
    root = Path(repo_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"workspace.repo_path is not a directory: {root}")
    return root


def _artifact_root(workspace: dict[str, Any]) -> Path:
    artifact_dir = str((workspace or {}).get("artifact_dir") or "").strip()
    if not artifact_dir:
        raise ValueError("workspace.artifact_dir is not available")
    root = Path(artifact_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _artifact_path(root: Path, raw_path: Any) -> Path:
    relative = str(raw_path or "").strip()
    if not relative:
        raise ValueError("relative_path is required")
    if Path(relative).is_absolute():
        raise ValueError("artifact path must be relative to artifact_dir")
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("artifact path escapes artifact_dir")
    if candidate == root:
        raise ValueError("artifact path must name a file")
    return candidate


def _write_minion_artifact(workspace: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown_args(args, {"relative_path", "content", "title", "role", "mime_type", "overwrite"})
    root = _artifact_root(workspace)
    path = _artifact_path(root, args.get("relative_path"))
    content = str(args.get("content") or "")
    if not content.strip():
        raise ValueError("artifact content is required")
    if path.exists() and not bool(args.get("overwrite")):
        path = _next_available_artifact_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return _artifact_metadata(root, path, args)


def _edit_minion_artifact(workspace: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown_args(args, {"relative_path", "content", "operation", "create_if_missing", "title", "role", "mime_type"})
    root = _artifact_root(workspace)
    path = _artifact_path(root, args.get("relative_path"))
    operation = str(args.get("operation") or "append").strip().lower() or "append"
    if operation not in {"append", "replace"}:
        raise ValueError("operation must be append or replace")
    content = str(args.get("content") or "")
    if not content.strip():
        raise ValueError("artifact content is required")
    create_if_missing = bool(args.get("create_if_missing", True))
    if not path.exists() and not create_if_missing:
        raise ValueError("artifact does not exist")
    path.parent.mkdir(parents=True, exist_ok=True)
    if operation == "replace":
        path.write_text(content, encoding="utf-8")
    else:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(content)
    return _artifact_metadata(root, path, args)


def _artifact_metadata(root: Path, path: Path, args: dict[str, Any]) -> dict[str, Any]:
    content = path.read_text(encoding="utf-8", errors="ignore")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    relative_path = str(path.relative_to(root)).replace("\\", "/")
    mime_type = str(args.get("mime_type") or mimetypes.guess_type(path.name)[0] or "text/plain").strip()
    role = str(args.get("role") or "primary").strip() or "primary"
    return {
        "kind": "file",
        "path": str(path),
        "relative_path": relative_path,
        "title": str(args.get("title") or path.stem).strip() or path.name,
        "role": role,
        "mime_type": mime_type,
        "size_bytes": path.stat().st_size,
        "sha256": digest,
    }


def _reject_unknown_args(args: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(str(key) for key in args if str(key) not in allowed)
    if unknown:
        raise ValueError(f"unknown argument(s): {', '.join(unknown)}")


def _next_available_artifact_path(path: Path) -> Path:
    if not path.exists():
        return path
    parent = path.parent
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = parent / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise ValueError(f"could not allocate unique artifact path for {path.name}")


def _append_unique_artifact(items: list[dict[str, Any]], artifact: dict[str, Any]) -> None:
    path = str(artifact.get("path") or "").strip()
    relative_path = str(artifact.get("relative_path") or "").strip()
    for index, existing in enumerate(items):
        if path and str(existing.get("path") or "") == path:
            items[index] = dict(artifact)
            return
        if relative_path and str(existing.get("relative_path") or "") == relative_path:
            items[index] = dict(artifact)
            return
    items.append(dict(artifact))


def _workspace_path(root: Path, raw_path: Any = "") -> Path:
    relative = str(raw_path or ".").strip() or "."
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("workspace path must be relative to workspace.repo_path; absolute paths and clone-source paths are not accepted")
    return candidate


_WORKSPACE_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".venv", "venv"}
_WORKSPACE_SKIP_SUFFIXES = {
    ".a",
    ".bin",
    ".class",
    ".dll",
    ".dylib",
    ".exe",
    ".o",
    ".obj",
    ".pdf",
    ".png",
    ".pyc",
    ".pyo",
    ".so",
    ".zip",
}


def _workspace_should_skip_generated(path: Path, root: Path) -> bool:
    with contextlib.suppress(ValueError):
        parts = path.relative_to(root).parts
        if any(part in _WORKSPACE_SKIP_DIRS for part in parts):
            return True
    return path.suffix.lower() in _WORKSPACE_SKIP_SUFFIXES


def _empty_workspace_tool_text(name: str, payload: dict[str, Any]) -> str:
    if name == "op_workspace_tree":
        return "No workspace entries found."
    if name == "op_workspace_search":
        query = str(payload.get("query") or "").strip()
        return f"No workspace matches found for query: {query}" if query else "No workspace matches found."
    if name == "op_workspace_read":
        path = str(payload.get("path") or "").strip()
        return f"{path}: empty file" if path else "Empty workspace file."
    return "Workspace tool completed with no textual output."


def _workspace_tree(root: Path, args: dict[str, Any]) -> dict[str, Any]:
    base = _workspace_path(root, args.get("path") or ".")
    if not base.exists():
        raise ValueError(f"workspace path does not exist: {base.relative_to(root)}")
    max_depth = max(0, min(_optional_positive_int(args.get("max_depth")) or 2, 8))
    limit = max(1, min(_optional_positive_int(args.get("limit")) or 200, 1000))
    items: list[dict[str, Any]] = []
    if base.is_file():
        stat = base.stat()
        items.append({"path": str(base.relative_to(root)).replace("\\", "/"), "kind": "file", "size_bytes": stat.st_size})
        return {"root": str(root), "items": items, "count": len(items)}
    base_depth = len(base.relative_to(root).parts) if base != root else 0
    for current, dirs, files in os.walk(base):
        current_path = Path(current)
        rel_parts = current_path.relative_to(root).parts if current_path != root else ()
        depth = len(rel_parts) - base_depth
        dirs[:] = [name for name in sorted(dirs) if name not in _WORKSPACE_SKIP_DIRS]
        for name in dirs:
            if len(items) >= limit:
                return {"root": str(root), "items": items, "count": len(items), "truncated": True}
            path = current_path / name
            items.append({"path": str(path.relative_to(root)).replace("\\", "/"), "kind": "dir"})
        for name in sorted(files):
            if len(items) >= limit:
                return {"root": str(root), "items": items, "count": len(items), "truncated": True}
            path = current_path / name
            with contextlib.suppress(OSError):
                items.append({"path": str(path.relative_to(root)).replace("\\", "/"), "kind": "file", "size_bytes": path.stat().st_size})
        if depth >= max_depth:
            dirs[:] = []
    return {"root": str(root), "items": items, "count": len(items)}


def _workspace_search(root: Path, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    base = _workspace_path(root, args.get("path") or ".")
    limit = max(1, min(_optional_positive_int(args.get("limit")) or 50, 500))
    matches: list[dict[str, Any]] = []
    query_lower = query.lower()
    paths = [base] if base.is_file() else [path for path in base.rglob("*") if path.is_file()]
    for path in paths:
        if _workspace_should_skip_generated(path, root):
            continue
        try:
            for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
                if query_lower not in line.lower():
                    continue
                matches.append(
                    {
                        "path": str(path.relative_to(root)).replace("\\", "/"),
                        "line_number": line_number,
                        "line": _preview_text(line, limit=300),
                    }
                )
                if len(matches) >= limit:
                    return {"root": str(root), "query": query, "matches": matches, "count": len(matches), "truncated": True}
        except OSError:
            continue
    return {"root": str(root), "query": query, "matches": matches, "count": len(matches)}


def _workspace_read(root: Path, args: dict[str, Any]) -> dict[str, Any]:
    path = _workspace_path(root, args.get("path") or "")
    if not path.is_file():
        raise ValueError(f"workspace path is not a file: {path.relative_to(root)}")
    start_line = max(1, _optional_positive_int(args.get("start_line")) or 1)
    limit_lines = max(1, min(_optional_positive_int(args.get("limit_lines")) or 200, 1000))
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    selected = lines[start_line - 1 : start_line - 1 + limit_lines]
    numbered = [f"{index}: {line}" for index, line in enumerate(selected, start=start_line)]
    return {
        "root": str(root),
        "path": str(path.relative_to(root)).replace("\\", "/"),
        "start_line": start_line,
        "line_count": len(selected),
        "truncated": start_line - 1 + limit_lines < len(lines),
        "text": "\n".join(numbered),
    }

def _preview_text(value: Any, *, limit: int = 400) -> str:
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
