from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pal.llm.endpoint_spec import LLMEndpointSpec
from pal.llm.ir import ThinkingLevel, WireShape
from pal.llm.endpoint_identity import IDENTITY_CHECK, bind_endpoint_identity, legacy_provider
from pal.llm.compaction import native_origin


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
        "transport",
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
    if not _is_current_schema(database, columns):
        raise LLMEndpointSchemaError("legacy endpoint schema; run offline `pal llm migrate-ids --runtime-root <runtime-root>`")
    _validate_existing_rows(database)


def migrate_llm_endpoint_schema(db_path: Path, *, keep: tuple[str, ...] = ()) -> LLMEndpointMigrationResult:
    path = Path(db_path)
    if not path.exists():
        return LLMEndpointMigrationResult(status="new_database")
    database = sqlite3.connect(path)
    database.row_factory = sqlite3.Row
    try:
        columns = _table_columns(database, "llm_endpoints")
        if not columns:
            return LLMEndpointMigrationResult(status="new_database")
        if _is_current_schema(database, columns):
            try:
                _validate_existing_rows(database)
            except LLMEndpointSchemaError:
                # Identity migration may repair an otherwise valid row
                # (for example a missing ChatGPT access profile).
                pass
            else:
                return LLMEndpointMigrationResult(status="current")
        database.execute("BEGIN IMMEDIATE")
        # Another opener may have completed the migration while this one waited.
        columns = _table_columns(database, "llm_endpoints")
        if _is_current_schema(database, columns):
            try:
                _validate_existing_rows(database)
            except LLMEndpointSchemaError:
                # Identity migration may repair an otherwise valid row
                # (for example a missing ChatGPT access profile).
                pass
            else:
                return LLMEndpointMigrationResult(status="current")
        current_shape = "wire_shape" in columns and not (_REMOVED_COLUMNS & columns)
        if not current_shape and "api_mode" not in columns:
            raise LLMEndpointSchemaError(
                "unrecognized LLM endpoint schema; expected legacy api_mode or current wire_shape"
            )
        schema_objects = database.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name='llm_endpoints' "
            "AND type IN ('index', 'trigger') AND sql IS NOT NULL"
        ).fetchall() if current_shape else []
        rows = [dict(row) for row in database.execute("SELECT * FROM llm_endpoints ORDER BY endpoint_id")]
        try:
            database.execute("DROP TABLE IF EXISTS llm_endpoints__next")
            database.execute(_CREATE_NEXT_TABLE_SQL)
            migrated, aliases = plan_identity_rows(rows, keep=keep)
            for payload in migrated:
                database.execute(_INSERT_NEXT_SQL, tuple(payload[name] for name in _INSERT_COLUMNS))
            migrate_identity_settings(database, aliases, migrated, keep=keep)
            database.execute("DROP TABLE llm_endpoints")
            database.execute("ALTER TABLE llm_endpoints__next RENAME TO llm_endpoints")
            for statement in schema_objects:
                database.execute(statement[0])
            database.execute(
                "CREATE INDEX IF NOT EXISTS llm_endpoints_enabled_priority ON llm_endpoints(enabled, priority)"
            )
            database.execute(
                "CREATE INDEX IF NOT EXISTS llm_endpoints_enabled_supports_tools_supports_streaming "
                "ON llm_endpoints(enabled, supports_tools, supports_streaming)"
            )
            assert_llm_endpoint_schema_current(database)
            database.commit()
        except Exception:
            database.rollback()
            raise
        assert_llm_endpoint_schema_current(database)
        return LLMEndpointMigrationResult(status="migrated", migrated_rows=len(rows))
    finally:
        database.close()


def _is_current_schema(database: sqlite3.Connection, columns: frozenset[str]) -> bool:
    if not _CURRENT_COLUMNS <= columns or _REMOVED_COLUMNS & columns:
        return False
    ddl = database.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='llm_endpoints'").fetchone()
    if not (ddl and "gemini_interactions" in ddl[0] and IDENTITY_CHECK in ddl[0]):
        return False
    for index in database.execute("PRAGMA index_list(llm_endpoints)"):
        if index[2] and not index[4]:
            name = str(index[1]).replace('"', '""')
            if tuple(row[2] for row in database.execute(f'PRAGMA index_info("{name}")')) == ("provider", "model_id"):
                return True
    return False


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
            "transport": _legacy_transport(capabilities),
            "wire_shape": wire_shape,
            "thinking_levels_blob": json.dumps(levels, ensure_ascii=False),
            "default_thinking_level": default_level,
            "capabilities_blob": json.dumps(capabilities, ensure_ascii=False),
        }
    )
    return payload


def _legacy_transport(capabilities: Mapping[str, Any]) -> str:
    from pal.llm.chatgpt import PROFILE

    return "websocket" if capabilities.get("access_profile") == PROFILE else "http"


def _validate_existing_rows(database: sqlite3.Connection) -> None:
    columns = tuple(name for name in _INSERT_COLUMNS if name not in {"created_at", "updated_at"})
    cursor = database.execute(f"SELECT {', '.join(columns)} FROM llm_endpoints")
    for row in cursor:
        payload = dict(zip(columns, row))
        try:
            spec = LLMEndpointSpec.from_value(payload)
            if bind_endpoint_identity(spec.to_payload()) != spec.to_payload():
                raise ValueError("missing required endpoint identity profile")
        except ValueError as exc:
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
    "transport",
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
_CREATE_NEXT_TABLE_SQL = f"""
CREATE TABLE llm_endpoints__next (
    endpoint_id TEXT PRIMARY KEY NOT NULL CHECK ({IDENTITY_CHECK}),
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    display_name TEXT,
    wire_shape TEXT NOT NULL CHECK (
        wire_shape IN ('openai_completion', 'openai_response', 'anthropic_messages', 'gemini_interactions')
    ),
    transport TEXT NOT NULL DEFAULT 'http' CHECK (transport IN ('http', 'websocket')),
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
    updated_at TEXT NOT NULL,
    UNIQUE (provider, model_id)
)
"""


def plan_identity_rows(rows: list[dict[str, Any]], *, keep: tuple[str, ...] = ()) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Compute the whole rename before altering any persisted row."""
    groups: dict[str, list[tuple[dict, LLMEndpointSpec]]] = {}
    for row in rows:
        payload = dict(row) if "wire_shape" in row else _migrate_row(row)
        if "transport" not in payload:
            payload["transport"] = _legacy_transport(_json_object(payload["capabilities_blob"]))
        try:
            original = LLMEndpointSpec.from_value(payload)
            new = original.to_payload()
            new["provider"] = legacy_provider(new)
            new.pop("endpoint_id")
            spec = LLMEndpointSpec.from_value(bind_endpoint_identity(new))
        except ValueError as exc:
            raise LLMEndpointSchemaError(f"invalid endpoint {row['endpoint_id']!r}: {exc}") from exc
        groups.setdefault(spec.endpoint_id, []).append((row, spec))
    migrated, mapping = [], {}
    used_keep = set()
    for new_id, candidates in groups.items():
        selected = [item for item in candidates if item[0]["endpoint_id"] in keep]
        if len(candidates) > 1 and len(selected) != 1:
            raise LLMEndpointSchemaError(f"duplicate {new_id}: choose one --keep from {[r['endpoint_id'] for r, _ in candidates]}")
        row, spec = selected[0] if selected else candidates[0]
        if selected:
            used_keep.add(row["endpoint_id"])
        payload = spec.to_payload()
        capabilities = payload["capabilities_blob"]
        aliases = dict(capabilities.get("replay_endpoint_aliases", {}))
        previous = dict(row) if "wire_shape" in row else _migrate_row(row)
        previous.setdefault("transport", _legacy_transport(_json_object(previous["capabilities_blob"])))
        from pal.llm.effort_updates import effort_route_changed
        for candidate, _ in candidates:
            prior = dict(candidate) if "wire_shape" in candidate else _migrate_row(candidate)
            prior.setdefault("transport", _legacy_transport(_json_object(prior["capabilities_blob"])))
            source = LLMEndpointSpec.from_value(prior)
            if not effort_route_changed(source, spec):
                origin = native_origin(source)
                if candidate["endpoint_id"] != new_id:
                    if candidate["endpoint_id"] in groups:
                        raise LLMEndpointSchemaError(f"historical ID collides with a canonical endpoint: {candidate['endpoint_id']}")
                    aliases[candidate["endpoint_id"]] = origin
                aliases.update(source.capabilities_blob.get("replay_endpoint_aliases", {}))
        aliases = {alias: digest for alias, digest in aliases.items() if alias not in groups and digest == native_origin(spec)}
        if aliases:
            capabilities["replay_endpoint_aliases"] = aliases
        else:
            capabilities.pop("replay_endpoint_aliases", None)
        payload.update(created_at=row["created_at"], updated_at=row["updated_at"])
        for field in ("thinking_levels_blob", "input_modalities_blob", "output_modalities_blob", "capabilities_blob"):
            original_value = previous.get(field)
            payload[field] = (original_value if _json_value(original_value) == payload[field]
                              else json.dumps(payload[field], ensure_ascii=False, separators=(",", ":")))
        migrated.append(payload)
        for candidate, _ in candidates:
            mapping[candidate["endpoint_id"]] = new_id
    owners: dict[str, str] = {}
    for row in migrated:
        for alias in _json_object(row["capabilities_blob"]).get("replay_endpoint_aliases", {}):
            if alias in owners and owners[alias] != row["endpoint_id"]:
                raise LLMEndpointSchemaError(f"ambiguous historical endpoint ID: {alias}")
            owners[alias] = row["endpoint_id"]
    if set(keep) - used_keep:
        raise LLMEndpointSchemaError(f"unknown --keep endpoint(s): {sorted(set(keep) - used_keep)}")
    return migrated, mapping


def migrate_identity_settings(database: sqlite3.Connection, mapping: dict[str, str], rows: list[dict[str, Any]], *, keep: tuple[str, ...] = ()) -> None:
    if not _table_columns(database, "pal_runtime_settings"):
        return
    settings = {key: (value, stamp) for key, value, stamp in database.execute(
        "SELECT setting_key, setting_value, updated_at FROM pal_runtime_settings")}
    active = settings.get("active_llm_endpoint_id")
    if active and active[0] in mapping:
        database.execute("UPDATE pal_runtime_settings SET setting_value=? WHERE setting_key='active_llm_endpoint_id'", (mapping[active[0]],))
    levels = {r["endpoint_id"]: json.loads(r["thinking_levels_blob"]) for r in rows}
    selected = {mapping[old]: old for old in keep if old in mapping}
    updates: dict[str, tuple[str, str]] = {}
    for old, new in mapping.items():
        key = f"think_level:{old}"
        if key not in settings:
            continue
        value, stamp = settings[key]
        if new in selected and selected[new] != old:
            database.execute("DELETE FROM pal_runtime_settings WHERE setting_key=?", (key,))
            continue
        if value not in levels[new]:
            raise LLMEndpointSchemaError(f"thinking setting for {old} is not supported by {new}")
        target = f"think_level:{new}"
        if target in updates and updates[target][0] != value:
            raise LLMEndpointSchemaError(f"conflicting thinking settings for {new}; resolve before migration")
        updates[target] = (value, stamp)
        database.execute("DELETE FROM pal_runtime_settings WHERE setting_key=?", (key,))
    for key, (value, stamp) in updates.items():
        database.execute("INSERT OR REPLACE INTO pal_runtime_settings (setting_key, setting_value, updated_at) VALUES (?,?,?)", (key, value, stamp))
