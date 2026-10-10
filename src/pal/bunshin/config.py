from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from pal.bunshin.ipc import bunshin_runtime_dir


DEFAULT_MAX_PARALLEL_LLM_NODES = 5
BUNSHIN_DB_FILENAME = "bunshin.sqlite3"
BUNSHIN_RUNTIME_SETTINGS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS bunshin_runtime_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT ''
);
"""


def bunshin_db_path(runtime_root: Path) -> Path:
    return bunshin_runtime_dir(Path(runtime_root)) / BUNSHIN_DB_FILENAME


def ensure_bunshin_runtime_settings_schema(connection: sqlite3.Connection) -> None:
    connection.execute(BUNSHIN_RUNTIME_SETTINGS_TABLE_SQL)


def read_bunshin_runtime_config(runtime_root: Path) -> dict[str, Any]:
    path = bunshin_db_path(runtime_root)
    if not path.exists():
        return {}
    try:
        with sqlite3.connect(str(path)) as db:
            db.row_factory = sqlite3.Row
            ensure_bunshin_runtime_settings_schema(db)
            rows = db.execute("SELECT setting_key, setting_value FROM bunshin_runtime_settings").fetchall()
            return {str(row["setting_key"]): _decode_setting_value(str(row["setting_value"])) for row in rows}
    except sqlite3.Error:
        return {}


def effective_bunshin_runtime_config(runtime_root: Path) -> dict[str, Any]:
    raw = read_bunshin_runtime_config(runtime_root)

    max_parallel_llm_nodes = _positive_int(
        raw.get("max_parallel_llm_nodes", raw.get("max_parallel_modules")),
        default=DEFAULT_MAX_PARALLEL_LLM_NODES,
    )
    env_max_parallel = _positive_int(
        os.environ.get("PAL_BUNSHIN_MAX_PARALLEL_LLM_NODES", os.environ.get("PAL_BUNSHIN_MAX_PARALLEL_MODULES")),
        default=None,
    )
    if env_max_parallel is not None:
        max_parallel_llm_nodes = env_max_parallel

    auto_resume = _optional_bool(raw.get("auto_resume_ready_modules"))
    env_auto_resume = _optional_bool(os.environ.get("PAL_BUNSHIN_AUTO_RESUME_READY_MODULES"))
    if env_auto_resume is not None:
        auto_resume = env_auto_resume

    config: dict[str, Any] = {
        "max_parallel_llm_nodes": int(max_parallel_llm_nodes or DEFAULT_MAX_PARALLEL_LLM_NODES),
        "max_parallel_modules": int(max_parallel_llm_nodes or DEFAULT_MAX_PARALLEL_LLM_NODES),
        "db_path": str(bunshin_db_path(runtime_root)),
    }
    if auto_resume is not None:
        config["auto_resume_ready_modules"] = bool(auto_resume)
    return config


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


def _decode_setting_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value
