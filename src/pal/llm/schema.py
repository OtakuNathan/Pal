from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pal.llm.endpoint_spec import LLMEndpointSpec, LLMEndpointSpecError
from pal.llm.ir import ThinkingLevel, WireShape


class LLMEndpointSchemaError(RuntimeError):
    pass


@dataclass(frozen=True)
class LLMEndpointMigrationResult:
    status: str
    migrated_rows: int = 0


_CURRENT_COLUMNS = frozenset(
    {
        "endpoint_id",
        "provider",
        "model_id",
        "display_name",
        "wire_shape",
        "base_url",
        "auth_kind",
        "credential_ref",
        "context_window",
        "max_output_tokens",
        "thinking_levels_blob",
        "default_thinking_level",
        "supports_tools",
        "supports_streaming",
        "supports_vision",
        "input_modalities_blob",
        "output_modalities_blob",
        "priority",
        "enabled",
        "capabilities_blob",
        "notes",
        "created_at",
        "updated_at",
    }
)
_REMOVED_COLUMNS = frozenset({"api_mode", "supports_reasoning"})
_THINKING_LEVELS = frozenset(item.value for item in ThinkingLevel)


def assert_llm_endpoint_schema_current(database: sqlite3.Connection) -> None:
    columns = _table_columns(database, "llm_endpoints")
    if not columns:
        return
    missing = _CURRENT_COLUMNS - columns
    removed = _REMOVED_COLUMNS & columns
    if missing or removed:
        details: list[str] = []
        if missing:
            details.append(f"missing={sorted(missing)}")
        if removed:
            details.append(f"removed_columns_present={sorted(removed)}")
        raise LLMEndpointSchemaError(
            "legacy LLM endpoint schema is not migrated; run `pal setup --upgrade "
            "--runtime-root <runtime-root>` (" + ", ".join(details) + ")"
        )
    _validate_existing_rows(database)


def migrate_llm_endpoint_schema(db_path: Path) -> LLMEndpointMigrationResult:
    path = Path(db_path)
    if not path.exists():
        return LLMEndpointMigrationResult(status="new_database")
    database = sqlite3.connect(path)
    database.row_factory = sqlite3.Row
    try:
        columns = _table_columns(database, "llm_endpoints")
        if not columns:
            return LLMEndpointMigrationResult(status="new_database")
        if _CURRENT_COLUMNS <= columns and not (_REMOVED_COLUMNS & columns):
            assert_llm_endpoint_schema_current(database)
            return LLMEndpointMigrationResult(status="current")
        if "api_mode" not in columns:
            raise LLMEndpointSchemaError(
                "unrecognized LLM endpoint schema; expected legacy api_mode or current wire_shape"
            )
        rows = [dict(row) for row in database.execute("SELECT * FROM llm_endpoints ORDER BY endpoint_id")]
        database.execute("BEGIN IMMEDIATE")
        try:
            database.execute("DROP TABLE IF EXISTS llm_endpoints__next")
            database.execute(_CREATE_NEXT_TABLE_SQL)
            for row in rows:
                payload = _migrate_row(row)
                database.execute(
                    _INSERT_NEXT_SQL,
                    tuple(payload[name] for name in _INSERT_COLUMNS),
                )
            database.execute("DROP TABLE llm_endpoints")
            database.execute("ALTER TABLE llm_endpoints__next RENAME TO llm_endpoints")
            database.execute(
                "CREATE INDEX llm_endpoints_enabled_priority ON llm_endpoints(enabled, priority)"
            )
            database.execute(
                "CREATE INDEX llm_endpoints_enabled_supports_tools_supports_streaming "
                "ON llm_endpoints(enabled, supports_tools, supports_streaming)"
            )
            database.commit()
        except Exception:
            database.rollback()
            raise
        assert_llm_endpoint_schema_current(database)
        return LLMEndpointMigrationResult(status="migrated", migrated_rows=len(rows))
    finally:
        database.close()


def _migrate_row(row: Mapping[str, Any]) -> dict[str, Any]:
    capabilities = _json_object(row.get("capabilities_blob"))
    api_mode = str(row.get("api_mode") or "").strip()
    if api_mode == "anthropic_messages":
        wire_shape = WireShape.ANTHROPIC_MESSAGES.value
    elif bool(capabilities.get("responses_api") or capabilities.get("openai_responses")):
        wire_shape = WireShape.OPENAI_RESPONSE.value
    else:
        wire_shape = WireShape.OPENAI_COMPLETION.value

    raw_levels = capabilities.get("thinking_levels")
    levels = _normalize_thinking_levels(raw_levels)
    if not levels:
        levels = (
            [ThinkingLevel.OFF.value, ThinkingLevel.LOW.value, ThinkingLevel.MEDIUM.value, ThinkingLevel.HIGH.value]
            if bool(row.get("supports_reasoning"))
            else [ThinkingLevel.OFF.value]
        )
    requested_default = str(capabilities.get("default_thinking_level") or "").strip().lower()
    default_level = requested_default if requested_default in levels else (
        ThinkingLevel.MEDIUM.value if ThinkingLevel.MEDIUM.value in levels else levels[0]
    )
    capabilities.pop("thinking_levels", None)
    capabilities.pop("default_thinking_level", None)

    payload = {
        name: row.get(name)
        for name in _INSERT_COLUMNS
        if name not in {"wire_shape", "thinking_levels_blob", "default_thinking_level", "capabilities_blob"}
    }
    payload.update(
        {
            "wire_shape": wire_shape,
            "thinking_levels_blob": json.dumps(levels, ensure_ascii=False),
            "default_thinking_level": default_level,
            "capabilities_blob": json.dumps(capabilities, ensure_ascii=False),
        }
    )
    return payload


def _validate_existing_rows(database: sqlite3.Connection) -> None:
    columns = tuple(name for name in _INSERT_COLUMNS if name not in {"created_at", "updated_at"})
    cursor = database.execute(f"SELECT {', '.join(columns)} FROM llm_endpoints")
    for row in cursor:
        payload = dict(zip(columns, row))
        endpoint_id = str(payload.get("endpoint_id") or "")
        try:
            LLMEndpointSpec.from_value(payload)
        except LLMEndpointSpecError as exc:
            raise LLMEndpointSchemaError(str(exc)) from exc


def _normalize_thinking_levels(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    levels: list[str] = []
    for item in value:
        normalized = str(item or "").strip().lower()
        if normalized in _THINKING_LEVELS and normalized not in levels:
            levels.append(normalized)
    return levels


def _json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _json_object(value: Any) -> dict[str, Any]:
    parsed = _json_value(value)
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _table_columns(database: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(str(row[1]) for row in database.execute(f'PRAGMA table_info("{table}")'))


_INSERT_COLUMNS = (
    "endpoint_id",
    "provider",
    "model_id",
    "display_name",
    "wire_shape",
    "base_url",
    "auth_kind",
    "credential_ref",
    "context_window",
    "max_output_tokens",
    "thinking_levels_blob",
    "default_thinking_level",
    "supports_tools",
    "supports_streaming",
    "supports_vision",
    "input_modalities_blob",
    "output_modalities_blob",
    "priority",
    "enabled",
    "capabilities_blob",
    "notes",
    "created_at",
    "updated_at",
)
_INSERT_NEXT_SQL = (
    f"INSERT INTO llm_endpoints__next ({', '.join(_INSERT_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _INSERT_COLUMNS)})"
)
_CREATE_NEXT_TABLE_SQL = """
CREATE TABLE llm_endpoints__next (
    endpoint_id TEXT PRIMARY KEY NOT NULL,
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    display_name TEXT,
    wire_shape TEXT NOT NULL CHECK (
        wire_shape IN ('openai_completion', 'openai_response', 'anthropic_messages')
    ),
    base_url TEXT NOT NULL,
    auth_kind TEXT NOT NULL DEFAULT 'api_key_ref' CHECK (
        auth_kind IN ('api_key_ref', 'oauth', 'local_provider_auth')
    ),
    credential_ref TEXT NOT NULL,
    context_window INTEGER,
    max_output_tokens INTEGER,
    thinking_levels_blob JSON NOT NULL,
    default_thinking_level TEXT NOT NULL,
    supports_tools INTEGER NOT NULL DEFAULT 1,
    supports_streaming INTEGER NOT NULL DEFAULT 1,
    supports_vision INTEGER NOT NULL DEFAULT 0,
    input_modalities_blob JSON NOT NULL,
    output_modalities_blob JSON NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    capabilities_blob JSON NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""
