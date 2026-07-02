from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from pal.minion.ipc import minion_runtime_dir


DEFAULT_MAX_PARALLEL_LLM_NODES = 5
DEFAULT_MAX_PARALLEL_MODULES = DEFAULT_MAX_PARALLEL_LLM_NODES
MINION_DB_FILENAME = "minion.sqlite3"
MINION_RUNTIME_SETTING_KEYS = {"max_parallel_llm_nodes", "max_parallel_modules", "auto_resume_ready_modules"}
MINION_RUNTIME_SETTINGS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS minion_runtime_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT ''
);
"""


def minion_db_path(runtime_root: Path) -> Path:
    return minion_runtime_dir(Path(runtime_root)) / MINION_DB_FILENAME


def ensure_minion_runtime_settings_schema(connection: sqlite3.Connection) -> None:
    connection.execute(MINION_RUNTIME_SETTINGS_TABLE_SQL)


def read_minion_runtime_config(runtime_root: Path) -> dict[str, Any]:
    path = minion_db_path(runtime_root)
    if not path.exists():
        return {}
    try:
        with sqlite3.connect(str(path)) as db:
            db.row_factory = sqlite3.Row
            ensure_minion_runtime_settings_schema(db)
            rows = db.execute("SELECT setting_key, setting_value FROM minion_runtime_settings").fetchall()
            return {str(row["setting_key"]): _decode_setting_value(str(row["setting_value"])) for row in rows}
    except sqlite3.Error:
        return {}


def effective_minion_runtime_config(runtime_root: Path) -> dict[str, Any]:
    raw = read_minion_runtime_config(runtime_root)

    max_parallel_llm_nodes = _positive_int(
        raw.get("max_parallel_llm_nodes", raw.get("max_parallel_modules")),
        default=DEFAULT_MAX_PARALLEL_LLM_NODES,
    )
    env_max_parallel = _positive_int(
        os.environ.get("PAL_MINION_MAX_PARALLEL_LLM_NODES", os.environ.get("PAL_MINION_MAX_PARALLEL_MODULES")),
        default=None,
    )
    if env_max_parallel is not None:
        max_parallel_llm_nodes = env_max_parallel

    auto_resume = _optional_bool(raw.get("auto_resume_ready_modules"))
    env_auto_resume = _optional_bool(os.environ.get("PAL_MINION_AUTO_RESUME_READY_MODULES"))
    if env_auto_resume is not None:
        auto_resume = env_auto_resume

    config: dict[str, Any] = {
        "max_parallel_llm_nodes": int(max_parallel_llm_nodes or DEFAULT_MAX_PARALLEL_LLM_NODES),
        "max_parallel_modules": int(max_parallel_llm_nodes or DEFAULT_MAX_PARALLEL_LLM_NODES),
        "db_path": str(minion_db_path(runtime_root)),
    }
    if auto_resume is not None:
        config["auto_resume_ready_modules"] = bool(auto_resume)
    return config


def merge_minion_runtime_config(runtime_root: Path, patch: dict[str, Any]) -> dict[str, Any]:
    current = read_minion_runtime_config(runtime_root)
    updated = {str(key): value for key, value in dict(current).items() if str(key) in MINION_RUNTIME_SETTING_KEYS}
    if "max_parallel_llm_nodes" in patch or "max_parallel_modules" in patch:
        raw_limit = patch.get("max_parallel_llm_nodes", patch.get("max_parallel_modules"))
        max_parallel_llm_nodes = _positive_int(raw_limit, default=None)
        if max_parallel_llm_nodes is None:
            raise ValueError("max_parallel_llm_nodes must be a positive integer")
        updated["max_parallel_llm_nodes"] = int(max_parallel_llm_nodes)
        updated.pop("max_parallel_modules", None)
    if "auto_resume_ready_modules" in patch:
        auto_resume = _optional_bool(patch.get("auto_resume_ready_modules"))
        if auto_resume is None:
            raise ValueError("auto_resume_ready_modules must be boolean-like")
        updated["auto_resume_ready_modules"] = bool(auto_resume)
    path = minion_db_path(runtime_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as db:
        ensure_minion_runtime_settings_schema(db)
        for key in set(current) - MINION_RUNTIME_SETTING_KEYS:
            db.execute("DELETE FROM minion_runtime_settings WHERE setting_key = ?", (str(key),))
        for key, value in updated.items():
            db.execute(
                """
                INSERT INTO minion_runtime_settings(setting_key, setting_value, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value = excluded.setting_value,
                    updated_at = excluded.updated_at
                """,
                (str(key), _encode_setting_value(value)),
            )
    return effective_minion_runtime_config(runtime_root)


def _positive_int(value: Any, *, default: int | None) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return None


def _encode_setting_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _decode_setting_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value
