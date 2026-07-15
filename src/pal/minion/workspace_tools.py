from __future__ import annotations

import contextlib
import fnmatch
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
            action = "staged" if artifact.get("staged") else "written"
            text = f"Artifact {action}: {artifact['relative_path']}"
        elif call.name == "op_minion_artifact_edit":
            artifact = _edit_minion_artifact(workspace, call.args)
            payload = {"artifact": artifact}
            action = "staged" if artifact.get("staged") else "edited"
            text = f"Artifact {action}: {artifact['relative_path']}"
        else:
            root, root_info = _workspace_root_with_info(workspace, call.args)
            if call.name == "op_tree":
                payload = _workspace_tree(root, call.args, root_info=root_info)
                text = "\n".join(item["path"] for item in payload["items"])
            elif call.name == "op_search":
                payload = _workspace_search(root, call.args, root_info=root_info)
                text = "\n".join(f"{item['path']}:{item['line_number']}: {item['line']}" for item in payload["matches"])
            else:
                raise ValueError(f"unknown repo tool: {call.name}")
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


def _workspace_root(workspace: dict[str, Any], args: dict[str, Any] | None = None) -> Path:
    root, _ = _workspace_root_with_info(workspace, args)
    return root


def _workspace_root_with_info(workspace: dict[str, Any], args: dict[str, Any] | None = None) -> tuple[Path, dict[str, Any]]:
    args = dict(args or {})
    reference_name = _requested_reference_name(args)
    if reference_name:
        return _reference_root(workspace, reference_name)
    repo_path = str((workspace or {}).get("repo_path") or "").strip()
    if not repo_path:
        raise ValueError("current project repo is not available")
    root = Path(repo_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"current project repo is not a directory: {root}")
    return root, {
        "root_kind": "project",
        "root_name": "project",
        "read_only": False,
        "truth_source": False,
    }


def _requested_reference_name(args: dict[str, Any]) -> str:
    reference_name = str(args.get("reference_name") or args.get("reference") or "").strip()
    if reference_name:
        return reference_name.removeprefix("reference:").strip()
    root = str(args.get("root") or "").strip()
    if not root or root in {"project", "repo", "workspace"}:
        return ""
    if root.startswith("reference:"):
        return root.split(":", 1)[1].strip()
    return root


def _reference_root(workspace: dict[str, Any], reference_name: str) -> tuple[Path, dict[str, Any]]:
    normalized = _normalized_reference_paths(workspace)
    if not normalized:
        raise ValueError("workspace.reference_paths is not available")
    requested = str(reference_name or "").strip()
    for item in normalized:
        if requested in {str(item.get("name") or ""), str(item.get("id") or "")}:
            path = Path(str(item.get("path") or "")).expanduser().resolve()
            if not path.exists() or not path.is_dir():
                raise ValueError(f"reference path is not a directory: {path}")
            info = {
                **item,
                "root_kind": "reference",
                "root_name": str(item.get("name") or requested),
                "reference_name": str(item.get("name") or requested),
                "read_only": True,
                "truth_source": bool(item.get("truth_source", True)),
            }
            return path, info
    available = ", ".join(str(item.get("name") or "") for item in normalized if str(item.get("name") or ""))
    raise ValueError(f"unknown reference root: {requested}; available references: {available or '(none)'}")


def _normalized_reference_paths(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    refs = (workspace or {}).get("reference_paths")
    if not isinstance(refs, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(refs, start=1):
        item = _normalize_reference_path_item(raw, index=index)
        name = str(item.get("name") or "").strip()
        path = str(item.get("path") or "").strip()
        if not name or not path or name in seen:
            continue
        seen.add(name)
        result.append(item)
    return result


def _normalize_reference_path_item(raw: Any, *, index: int) -> dict[str, Any]:
    if isinstance(raw, str):
        payload: dict[str, Any] = {"path": raw}
    elif isinstance(raw, dict):
        payload = dict(raw)
    else:
        return {}
    raw_path = str(
        payload.get("path")
        or payload.get("root")
        or payload.get("base_path")
        or payload.get("glob")
        or payload.get("pattern")
        or ""
    ).strip()
    if not raw_path:
        return {}
    root_path, include = _reference_root_and_include(raw_path)
    extra_include = _string_list(payload.get("include") or payload.get("includes"))
    if extra_include:
        include = extra_include
    root = Path(root_path).expanduser()
    name = _safe_reference_name(str(payload.get("name") or payload.get("id") or "")) or _safe_reference_name(root.name) or f"reference_{index}"
    item = {
        "name": name,
        "path": str(root),
        "mode": "read_only",
        "truth_source": _coerce_bool(payload.get("truth_source"), default=True),
    }
    if include:
        item["include"] = include
    for key in ("description", "role", "required"):
        if key in payload:
            item[key] = payload.get(key)
    return item


def _reference_root_and_include(raw_path: str) -> tuple[str, list[str]]:
    path = str(raw_path or "").strip()
    if not _has_glob(path):
        expanded = Path(path).expanduser()
        if expanded.exists() and expanded.is_file():
            return str(expanded.parent), [expanded.name]
        return path, []
    normalized = path.replace("\\", "/")
    parts = normalized.split("/")
    prefix: list[str] = []
    suffix: list[str] = []
    glob_seen = False
    for part in parts:
        if not glob_seen and not _has_glob(part):
            prefix.append(part)
            continue
        glob_seen = True
        suffix.append(part)
    root = "/".join(prefix) or "."
    include = "/".join(suffix) if suffix else "*"
    return root, [include]


def _has_glob(value: str) -> bool:
    return any(char in value for char in "*?[")


def _safe_reference_name(value: str) -> str:
    text = str(value or "").strip().lower()
    safe = [char if char.isalnum() else "_" for char in text]
    return "_".join("".join(safe).split("_")).strip("_")[:80]


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = []
    result: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "required", "source_of_truth", "truth_source"}


def _artifact_roots(workspace: dict[str, Any]) -> tuple[Path, Path]:
    artifact_dir = str((workspace or {}).get("artifact_dir") or "").strip()
    if not artifact_dir:
        raise ValueError("workspace.artifact_dir is not available")
    final_root = Path(artifact_dir).expanduser().resolve()
    stage_dir = str((workspace or {}).get("artifact_stage_dir") or "").strip()
    write_root = Path(stage_dir).expanduser().resolve() if stage_dir else final_root
    final_root.mkdir(parents=True, exist_ok=True)
    write_root.mkdir(parents=True, exist_ok=True)
    return final_root, write_root


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
    _reject_manager_owned_artifact_path(workspace, args.get("relative_path"))
    final_root, write_root = _artifact_roots(workspace)
    path = _artifact_path(write_root, args.get("relative_path"))
    content = str(args.get("content") or "")
    if not content.strip():
        raise ValueError("artifact content is required")
    overwrite = bool(args.get("overwrite")) if "overwrite" in args else bool((workspace or {}).get("artifact_overwrite_default"))
    if path.exists() and not overwrite:
        path = _next_available_artifact_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return _artifact_metadata(write_root, path, args, final_root=final_root)


def _edit_minion_artifact(workspace: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown_args(args, {"relative_path", "content", "operation", "create_if_missing", "title", "role", "mime_type"})
    _reject_manager_owned_artifact_path(workspace, args.get("relative_path"))
    final_root, write_root = _artifact_roots(workspace)
    path = _artifact_path(write_root, args.get("relative_path"))
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
    return _artifact_metadata(write_root, path, args, final_root=final_root)


def _artifact_metadata(root: Path, path: Path, args: dict[str, Any], *, final_root: Path | None = None) -> dict[str, Any]:
    content = path.read_text(encoding="utf-8", errors="ignore")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    relative_path = str(path.relative_to(root)).replace("\\", "/")
    mime_type = str(args.get("mime_type") or mimetypes.guess_type(path.name)[0] or "text/plain").strip()
    role = str(args.get("role") or "primary").strip() or "primary"
    artifact = {
        "kind": "file",
        "path": str(path),
        "relative_path": relative_path,
        "title": str(args.get("title") or path.stem).strip() or path.name,
        "role": role,
        "mime_type": mime_type,
        "size_bytes": path.stat().st_size,
        "sha256": digest,
    }
    if final_root is not None and final_root != root:
        artifact["staged"] = True
        artifact["stage_path"] = str(path)
        artifact["final_artifact_dir"] = str(final_root)
        requested_relative = str(args.get("relative_path") or "").strip()
        if requested_relative:
            artifact["requested_relative_path"] = requested_relative
    return artifact


def _reject_unknown_args(args: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(str(key) for key in args if str(key) not in allowed)
    if unknown:
        raise ValueError(f"unknown argument(s): {', '.join(unknown)}")


def _reject_manager_owned_artifact_path(workspace: dict[str, Any], raw_path: Any) -> None:
    if bool(workspace.get("manager_submission_write")):
        return
    relative = str(raw_path or "").replace("\\", "/").strip().lstrip("./")
    reserved = {
        str(item).replace("\\", "/").strip().lstrip("./")
        for item in list(workspace.get("manager_owned_submission_paths") or [])
        if str(item).strip()
    }
    if relative in reserved:
        raise ValueError(
            f"artifact path {relative!r} is Manager-owned; use the bound no-argument submit tool"
        )


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
        raise ValueError("path must be relative to the current project repo; absolute paths and clone-source paths are not accepted")
    return candidate


def _workspace_path_allowed_by_reference(path: Path, root: Path, root_info: dict[str, Any]) -> bool:
    if str(root_info.get("root_kind") or "") != "reference":
        return True
    includes = _string_list(root_info.get("include"))
    if not includes:
        return True
    try:
        relative = str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return False
    if path.is_dir():
        return True
    return any(fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(path.name, pattern) for pattern in includes)


def _workspace_root_payload(root: Path, root_info: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "root": str(root),
        "root_kind": str(root_info.get("root_kind") or "project"),
        "root_name": str(root_info.get("root_name") or root_info.get("reference_name") or "project"),
    }
    if str(root_info.get("root_kind") or "") == "reference":
        payload.update(
            {
                "reference_name": str(root_info.get("reference_name") or root_info.get("root_name") or ""),
                "read_only": True,
                "truth_source": bool(root_info.get("truth_source", True)),
            }
        )
        includes = _string_list(root_info.get("include"))
        if includes:
            payload["include"] = includes
    return payload


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
    if name == "op_tree":
        return "No repo entries found."
    if name == "op_search":
        query = str(payload.get("query") or "").strip()
        return f"No repo matches found for query: {query}" if query else "No repo matches found."
    return "Repo tool completed with no textual output."


def _workspace_tree(root: Path, args: dict[str, Any], *, root_info: dict[str, Any] | None = None) -> dict[str, Any]:
    root_info = dict(root_info or {})
    base = _workspace_path(root, args.get("path") or ".")
    if not base.exists():
        raise ValueError(f"repo path does not exist: {base.relative_to(root)}")
    max_depth = max(0, min(_optional_positive_int(args.get("max_depth")) or 2, 8))
    limit = max(1, min(_optional_positive_int(args.get("limit")) or 200, 1000))
    items: list[dict[str, Any]] = []
    if base.is_file():
        stat = base.stat()
        items.append({"path": str(base.relative_to(root)).replace("\\", "/"), "kind": "file", "size_bytes": stat.st_size})
        return {**_workspace_root_payload(root, root_info), "items": items, "count": len(items)}
    base_depth = len(base.relative_to(root).parts) if base != root else 0
    for current, dirs, files in os.walk(base):
        current_path = Path(current)
        rel_parts = current_path.relative_to(root).parts if current_path != root else ()
        depth = len(rel_parts) - base_depth
        dirs[:] = [name for name in sorted(dirs) if name not in _WORKSPACE_SKIP_DIRS]
        for name in dirs:
            if len(items) >= limit:
                return {**_workspace_root_payload(root, root_info), "items": items, "count": len(items), "truncated": True}
            path = current_path / name
            items.append({"path": str(path.relative_to(root)).replace("\\", "/"), "kind": "dir"})
        for name in sorted(files):
            if len(items) >= limit:
                return {**_workspace_root_payload(root, root_info), "items": items, "count": len(items), "truncated": True}
            path = current_path / name
            if not _workspace_path_allowed_by_reference(path, root, root_info):
                continue
            with contextlib.suppress(OSError):
                items.append({"path": str(path.relative_to(root)).replace("\\", "/"), "kind": "file", "size_bytes": path.stat().st_size})
        if depth >= max_depth:
            dirs[:] = []
    return {**_workspace_root_payload(root, root_info), "items": items, "count": len(items)}


def _workspace_search(root: Path, args: dict[str, Any], *, root_info: dict[str, Any] | None = None) -> dict[str, Any]:
    root_info = dict(root_info or {})
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
        if not _workspace_path_allowed_by_reference(path, root, root_info):
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
                    return {**_workspace_root_payload(root, root_info), "query": query, "matches": matches, "count": len(matches), "truncated": True}
        except OSError:
            continue
    return {**_workspace_root_payload(root, root_info), "query": query, "matches": matches, "count": len(matches)}


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
