"""Offline endpoint cutover. Immutable recovery journals are never rewritten."""
from __future__ import annotations

from contextlib import closing, contextmanager, nullcontext
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import tomllib
from typing import Any, Mapping

from pal.llm.compaction import native_origin
from pal.llm.endpoint_spec import LLMEndpointSpec
from pal.llm.schema import (LLMEndpointSchemaError, assert_llm_endpoint_schema_current,
                            migrate_llm_endpoint_schema, plan_identity_rows, migrate_identity_settings,
                            _migrate_row, _legacy_transport, _json_object, _table_columns)
from pal.memory.persistence.checkpoint import atomic_json, _fsync_directory
from pal.memory.persistence.journal import ResidentJournalStore
from pal.memory.storage import MemoryStorage
from pal.packages.process import runtime_lease
from pal.bunshin.cutover import require_bunshin_runtime_stopped
from pal.bunshin.checkpoint import open_agent_session_checkpoint


_MARKER = "endpoint-id-migration.json"


def assert_no_pending_endpoint_migration(root: Path) -> None:
    if (root / _MARKER).exists():
        raise LLMEndpointSchemaError("Incomplete endpoint migration; rerun offline pal llm migrate-ids")


def assert_runtime_endpoint_schema(root: Path, path: Path) -> None:
    assert_no_pending_endpoint_migration(root)
    if path.exists():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            assert_llm_endpoint_schema_current(db)


def _original_spec(row: dict) -> LLMEndpointSpec:
    value = dict(row) if "wire_shape" in row else _migrate_row(row)
    value.setdefault("transport", _legacy_transport(_json_object(value["capabilities_blob"])))
    return LLMEndpointSpec.from_value(value)


def _changed_bindings(rows: list[dict], migrated: list[dict], mapping: dict[str, str]) -> set[str]:
    targets = {row["endpoint_id"]: LLMEndpointSpec.from_value(row) for row in migrated}
    incompatible = set()
    for row in rows:
        source = _original_spec(row)
        target = targets[mapping[source.endpoint_id]]
        from pal.llm.effort_updates import effort_route_changed
        if (effort_route_changed(source, target) or
                source.endpoint_id != target.endpoint_id and
                target.capabilities_blob.get("replay_endpoint_aliases", {}).get(source.endpoint_id) != native_origin(source)):
            incompatible.add(source.endpoint_id)
            incompatible.update(source.capabilities_blob.get("replay_endpoint_aliases", {}))
    return incompatible


def _history_bindings(snapshot: Mapping[str, Any]) -> set[str]:
    """Inspect route metadata only; never serialize/decrypt history to a new file."""
    from pal.llm.serde import message_from_payload
    from pal.llm.replay_acceptance import has_opaque_continuation
    from pal.llm.effort_updates import EFFORT_PIN, EFFORT_UPDATE
    result = set()
    memory = snapshot.get("modules", {}).get("memory", {}).get("payload", {})
    for turn in memory.get("l1_turns", ()):
        pin = turn.get("metadata", {}).get(EFFORT_PIN)
        if isinstance(pin, Mapping) and pin.get("endpoint_id"):
            result.add(str(pin["endpoint_id"]))
        for raw in turn.get("messages", ()):
            message = message_from_payload(raw)
            if message.replay is not None and has_opaque_continuation(message):
                result.add(message.replay.endpoint_id)
            marker = message.metadata.get(EFFORT_UPDATE)
            if isinstance(marker, Mapping) and marker.get("endpoint_id"):
                result.add(str(marker["endpoint_id"]))
    return result


def _check_history(root: Path, incompatible: set[str]) -> None:
    if not incompatible:
        return
    snapshot = ResidentJournalStore(root, read_only=True).read()
    if snapshot and (conflict := _history_bindings(snapshot) & incompatible):
        raise LLMEndpointSchemaError(f"Retained resident history requires semantic handoff on {sorted(conflict)} before changing the route")
    path = root / "data" / "bunshin" / "bunshin.sqlite3"
    if not path.exists():
        return
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if _table_columns(db, "bunshin_role_checkpoint_heads"):
            for (envelope,) in db.execute("SELECT envelope_json FROM bunshin_role_checkpoint_heads"):
                checkpoint = open_agent_session_checkpoint(root, json.loads(envelope), read_only=True)
                refs = _history_bindings(checkpoint["runtime_snapshot"])
                refs.add(str(checkpoint["coroutine_state"].get("preferred_llm_endpoint_id") or ""))
                if refs & incompatible:
                    raise LLMEndpointSchemaError(f"Bunshin checkpoint needs its original route: {sorted(refs & incompatible)}")
        if _table_columns(db, "bunshin_role_assignments"):
            for (raw,) in db.execute("SELECT execution_spec_json FROM bunshin_role_assignments WHERE state NOT IN ('settled','cancelled')"):
                value = json.loads(raw)
                if _endpoint_refs(value) & incompatible:
                    raise LLMEndpointSchemaError("Nonterminal Bunshin assignment still uses an incompatible endpoint")
            if _table_columns(db, "bunshin_role_attempts"):
                from pal.bunshin.artifacts import read_typed_artifact_json
                for (raw,) in db.execute("SELECT t.prompt_pack_ref_json FROM bunshin_role_attempts t "
                                         "JOIN bunshin_role_assignments a ON a.assignment_id=t.assignment_id "
                                         "WHERE a.state NOT IN ('settled','cancelled')"):
                    ref = json.loads(raw)
                    if ref.get("sha256") and _endpoint_refs(read_typed_artifact_json(root, ref)) & incompatible:
                        raise LLMEndpointSchemaError("Nonterminal Bunshin invocation pack still uses an incompatible endpoint")


def _endpoint_refs(value: Any) -> set[str]:
    if isinstance(value, dict):
        return ({v for k, v in value.items() if k in {"preferred_endpoint_id", "preferred_llm_endpoint_id", "endpoint_id"} and isinstance(v, str)}
                | set().union(*(_endpoint_refs(v) for v in value.values())))
    if isinstance(value, list):
        return set().union(*(_endpoint_refs(v) for v in value))
    return set()


@contextmanager
def endpoint_replacement_guard(root: Path, existing: Any, payload: dict | None, *, already_locked: bool = False):
    if existing is None:
        yield
        return
    source = LLMEndpointSpec.from_value(existing)
    target = LLMEndpointSpec.from_value(payload) if payload is not None else None
    from pal.llm.effort_updates import effort_route_changed
    if target is not None and not effort_route_changed(source, target):
        yield
        return
    with nullcontext() if already_locked else runtime_lease(root):
        require_bunshin_runtime_stopped(root)
        _check_history(root, {source.endpoint_id, *source.capabilities_blob.get("replay_endpoint_aliases", {})})
        yield


def _rewrite_refs(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {k: mapping.get(v, v) if k in {"preferred_endpoint_id", "endpoint_id", "review_endpoint_id"} and isinstance(v, str)
                else _rewrite_refs(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite_refs(v, mapping) for v in value]
    return value


def _configuration_changes(root: Path, mapping: dict[str, str]) -> tuple[dict[Path, bytes], tuple[Path, str] | None]:
    files = {}
    for path in sorted((root / "data/bunshin/catalog/profile_overrides").rglob("*.json")):
        value = json.loads(path.read_text())
        updated = _rewrite_refs(value, mapping)
        if updated != value:
            files[path] = (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode()
    path = root / "config.toml"
    if path.exists():
        text = path.read_text()
        original = tomllib.loads(text)
        config = original.get("memory", {}).get("dreaming", {})
        changes = {k: mapping[v] for k, v in config.items() if k in {"endpoint_id", "review_endpoint_id"} and v in mapping and mapping[v] != v}
        if changes:
            from pal.wizard.config_file import _section_name, _line_key
            section = None
            lines = []
            for line in text.splitlines(keepends=True):
                section = _section_name(line) or section
                key = _line_key(line)
                if section == "memory.dreaming" and key in changes:
                    line = f"{key} = {json.dumps(changes[key], ensure_ascii=False)}\n"
                lines.append(line)
            updated = "".join(lines)
            config.update(changes)
            if tomllib.loads(updated) != original:
                raise LLMEndpointSchemaError("Cannot safely rewrite Dreaming config.toml; update its endpoint references explicitly")
            files[path] = updated.encode()
    storage = MemoryStorage(root, read_only=True)
    memory = None
    if storage.catalog_path.exists():
        with storage.connection() as db:
            if _table_columns(db, "memory_settings"):
                row = db.execute("SELECT value FROM memory_settings WHERE key='dreaming_config'").fetchone()
                if row:
                    value = json.loads(row[0]); updated = _rewrite_refs(value, mapping)
                    if updated != value:
                        memory = (storage.catalog_path, json.dumps(updated, ensure_ascii=False))
    return files, memory


def _check_hooks(root: Path, rows: list[dict], mapping: dict[str, str], migrated: list[dict]) -> None:
    providers = {row["endpoint_id"]: row["provider"] for row in migrated}
    changed = {r["provider"] for r in rows if r["provider"] != providers[mapping[r["endpoint_id"]]]}
    old_ids = {old for old, new in mapping.items() if old != new}
    for kind, identities in (("providers", changed), ("endpoints", old_ids)):
        for path in (root / "llm" / kind).rglob("*.py"):
            if any(identity in path.read_text() or identity == path.stem for identity in identities):
                raise LLMEndpointSchemaError(f"Update custom response hook registration before migration: {path}")


def _atomic_copy(path: Path, content: bytes) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content); handle.flush(); os.fsync(handle.fileno())
        if path.exists():
            os.chmod(name, path.stat().st_mode & 0o777)
        os.replace(name, path)
        _fsync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def _rollback(root: Path) -> None:
    marker = root / _MARKER
    if not marker.exists():
        return
    manifest = json.loads(marker.read_text())
    backup = Path(manifest["backup"])
    for relative in manifest["databases"]:
        with closing(sqlite3.connect(backup / relative)) as source, closing(sqlite3.connect(root / relative)) as target:
            source.backup(target)
    for relative in manifest["files"]:
        _atomic_copy(root / relative, (backup / relative).read_bytes())
    marker.unlink()
    _fsync_directory(root)


def migrate_runtime_endpoint_ids(root: Path, *, dry_run: bool = False, keep: tuple[str, ...] = (), already_locked: bool = False) -> dict:
    root = root.resolve()
    if not (root / "pal.sqlite3").is_file():
        raise FileNotFoundError("Existing Pal runtime database required")
    with nullcontext() if dry_run or already_locked else runtime_lease(root):
        if dry_run:
            assert_no_pending_endpoint_migration(root)
        else:
            require_bunshin_runtime_stopped(root)
            _rollback(root)
        path = root / "pal.sqlite3"
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            try:
                assert_llm_endpoint_schema_current(db)
                return {"status": "current", "dry_run": dry_run}
            except LLMEndpointSchemaError:
                pass
            rows = [dict(row) for row in db.execute("SELECT * FROM llm_endpoints ORDER BY endpoint_id")]
            migrated, mapping = plan_identity_rows(rows, keep=keep)
            # Exercise the same settings conflict checks without modifying the source DB.
            if _table_columns(db, "pal_runtime_settings"):
                with closing(sqlite3.connect(":memory:")) as scratch:
                    scratch.execute("CREATE TABLE pal_runtime_settings(setting_key TEXT PRIMARY KEY, setting_value TEXT, updated_at TEXT)")
                    scratch.executemany("INSERT INTO pal_runtime_settings VALUES (?,?,?)", list(db.execute("SELECT setting_key, setting_value, updated_at FROM pal_runtime_settings")))
                    migrate_identity_settings(scratch, mapping, migrated, keep=keep)
        incompatible = _changed_bindings(rows, migrated, mapping)
        _check_history(root, incompatible)
        _check_hooks(root, rows, mapping, migrated)
        files, memory = _configuration_changes(root, mapping)
        result = {"status": "planned" if dry_run else "migrated", "dry_run": dry_run,
                  "before": len(rows), "after": len(migrated), "endpoint_ids": mapping,
                  "incompatible_old_bindings": sorted(incompatible),
                  "configuration_files": [str(p.relative_to(root)) for p in files], "dreaming_settings_changed": memory is not None}
        if dry_run:
            return result
        backup = Path(tempfile.mkdtemp(prefix="endpoint-ids-", dir=root))
        databases = [path] + ([memory[0]] if memory else [])
        for db_path in databases:
            dest = backup / db_path.relative_to(root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(dest)) as target:
                source.backup(target)
        for file in files:
            dest = backup / file.relative_to(root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, dest)
            with dest.open("rb") as handle:
                os.fsync(handle.fileno())
        for directory, _, _ in os.walk(backup, topdown=False):
            _fsync_directory(Path(directory))
        _fsync_directory(root)
        marker = root / _MARKER
        atomic_json(marker, {"backup": str(backup), "databases": [str(p.relative_to(root)) for p in databases], "files": [str(p.relative_to(root)) for p in files]})
        try:
            migrate_llm_endpoint_schema(path, keep=keep)
            for file, content in files.items():
                _atomic_copy(file, content)
            if memory:
                with closing(sqlite3.connect(memory[0])) as db, db:
                    db.execute("UPDATE memory_settings SET value=? WHERE key='dreaming_config'", (memory[1],))
            marker.unlink()
            _fsync_directory(root)
        except BaseException:
            _rollback(root)
            raise
        return {**result, "backup": str(backup)}
