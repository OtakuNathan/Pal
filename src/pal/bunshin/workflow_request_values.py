from __future__ import annotations
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.paths import inferred_project_name


def _normalize_workspace(value: Any) -> dict[str, Any]:
    workspace = dict(value or {}) if isinstance(value, Mapping) else {}
    if not str(workspace.get("repo_path") or "").strip():
        alias = str(workspace.get("repo_root") or workspace.get("root") or workspace.get("path") or "").strip()
        if alias:
            workspace["repo_path"] = str(Path(_file_uri_path(alias)).expanduser())
    elif workspace.get("repo_path"):
        workspace["repo_path"] = str(Path(_file_uri_path(str(workspace["repo_path"]))).expanduser())
    if workspace.get("repo_path") and not workspace.get("kind"):
        workspace["kind"] = "existing_repo"
    if not str(workspace.get("project_name") or "").strip():
        workspace["project_name"] = inferred_project_name(workspace)
    return workspace


def _normalize_references(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("workflow references must be an array")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value, start=1):
        item = dict(raw) if isinstance(raw, Mapping) else {"path": str(raw)}
        path = str(item.get("path") or item.get("root") or item.get("uri") or "").strip()
        if not path:
            continue
        normalized = str(Path(_file_uri_path(path)).expanduser())
        if normalized in seen:
            continue
        seen.add(normalized)
        item["path"] = normalized
        item.setdefault("name", Path(normalized.rstrip("/")).name or f"reference_{index}")
        if not item.get("description") and item.get("note"):
            item["description"] = str(item["note"])
        result.append(item)
    return result


def _normalize_skill_refs(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("workflow skill_refs must be an array")
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        skill_id = str(raw or "").strip()
        if not skill_id:
            continue
        if skill_id in seen:
            continue
        seen.add(skill_id)
        result.append(skill_id)
    return result


def _file_uri_path(value: str) -> str:
    text = str(value or "").strip()
    return text.removeprefix("file://") if text.startswith("file://") else text
