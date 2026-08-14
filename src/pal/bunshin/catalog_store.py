from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from uuid import uuid4


_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def bunshin_catalog_root(runtime_root: Path) -> Path:
    return Path(runtime_root) / "data" / "bunshin" / "catalog"


def profile_override_root(runtime_root: Path) -> Path:
    return bunshin_catalog_root(runtime_root) / "profile_overrides"


def family_override_root(runtime_root: Path) -> Path:
    return bunshin_catalog_root(runtime_root) / "family_overrides"


def profile_override_path(runtime_root: Path, profile_group: str, profile_id: str) -> Path:
    group_parts = _semantic_parts(profile_group or "general")
    profile_name = _semantic_segment(profile_id, field="profile_id")
    return profile_override_root(runtime_root).joinpath(*group_parts, f"{profile_name}.json")


def family_override_path(runtime_root: Path, family_id: str) -> Path:
    parts = _semantic_parts(family_id or "general")
    return family_override_root(runtime_root).joinpath(*parts[:-1], f"{parts[-1]}.json")


def load_json_objects(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    directory = Path(root)
    if not directory.exists():
        return []
    result: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(directory.rglob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"catalog override must contain a JSON object: {path}")
        result.append((path, payload))
    return result


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def remove_override(path: Path) -> bool:
    target = Path(path)
    if not target.exists():
        return False
    target.unlink()
    _fsync_directory(target.parent)
    return True


def _semantic_parts(value: str) -> tuple[str, ...]:
    normalized = str(value or "").strip().replace("/", ".")
    parts = tuple(item for item in normalized.split(".") if item)
    if not parts:
        raise ValueError("semantic catalog name is required")
    for part in parts:
        _semantic_segment(part, field="catalog name")
    return parts


def _semantic_segment(value: str, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or not _SEGMENT_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field} must contain only letters, digits, underscores, or hyphens")
    return normalized


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
