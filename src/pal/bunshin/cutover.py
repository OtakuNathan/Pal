from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pal.bunshin.catalog import BunshinCatalogService
from pal.bunshin.catalog_store import load_json_objects
from pal.bunshin.config import (
    BUNSHIN_DB_FILENAME,
    ensure_bunshin_runtime_settings_schema,
)
from pal.bunshin.ipc import BunshinManagerClient, bunshin_runtime_dir
from pal.bunshin.v2.schema import (
    BUNSHIN_V2_SCHEMA_VERSION,
    ensure_bunshin_v2_schema,
)


@dataclass(frozen=True)
class BunshinRuntimeCutoverResult:
    status: str
    previous_version: int
    archive_root: str = ""
    copied_profile_overrides: int = 0
    copied_family_overrides: int = 0


def cutover_bunshin_runtime(runtime_root: Path) -> BunshinRuntimeCutoverResult:
    """Archive a legacy Bunshin runtime and create one current-schema root."""

    root = Path(runtime_root)
    source = bunshin_runtime_dir(root)
    previous_version = _bunshin_schema_version(source / BUNSHIN_DB_FILENAME)
    if previous_version in {0, BUNSHIN_V2_SCHEMA_VERSION}:
        return BunshinRuntimeCutoverResult(
            status=(
                "already_current"
                if previous_version == BUNSHIN_V2_SCHEMA_VERSION
                else "not_required"
            ),
            previous_version=previous_version,
        )
    _require_manager_stopped(root)

    profile_source = source / "catalog" / "profile_overrides"
    family_source = source / "catalog" / "family_overrides"
    profiles = load_json_objects(profile_source)
    families = load_json_objects(family_source)

    data_root = root / "data"
    archive_parent = data_root / "bunshin-archive"
    archive_parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    version_label = str(previous_version) if previous_version >= 0 else "unknown"
    archive = archive_parent / f"{stamp}-v{version_label}"
    staging_runtime_root = data_root / (
        f".bunshin-v{BUNSHIN_V2_SCHEMA_VERSION}-{uuid4().hex}"
    )
    staging = bunshin_runtime_dir(staging_runtime_root)
    try:
        _build_staging_runtime(
            staging,
            profiles=profiles,
            families=families,
            profile_source=profile_source,
            family_source=family_source,
        )
        BunshinCatalogService(staging_runtime_root).bootstrap()
        os.replace(source, archive)
        try:
            os.replace(staging, source)
        except Exception:
            os.replace(archive, source)
            raise
        _fsync_directory(data_root)
    finally:
        shutil.rmtree(staging_runtime_root, ignore_errors=True)
    return BunshinRuntimeCutoverResult(
        status="archived_and_initialized",
        previous_version=previous_version,
        archive_root=str(archive),
        copied_profile_overrides=len(profiles),
        copied_family_overrides=len(families),
    )


def _build_staging_runtime(
    staging: Path,
    *,
    profiles: list[tuple[Path, dict[str, object]]],
    families: list[tuple[Path, dict[str, object]]],
    profile_source: Path,
    family_source: Path,
) -> None:
    profile_target = staging / "catalog" / "profile_overrides"
    family_target = staging / "catalog" / "family_overrides"
    profile_target.mkdir(parents=True, exist_ok=True)
    family_target.mkdir(parents=True, exist_ok=True)
    for path, _payload in profiles:
        target = profile_target / path.relative_to(profile_source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    for path, _payload in families:
        target = family_target / path.relative_to(family_source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    with sqlite3.connect(str(staging / BUNSHIN_DB_FILENAME)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        ensure_bunshin_v2_schema(connection)
        ensure_bunshin_runtime_settings_schema(connection)
        connection.commit()
    _fsync_directory(staging)


def _bunshin_schema_version(db_path: Path) -> int:
    if not db_path.is_file():
        return 0
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
            row = connection.execute(
                """
                SELECT schema_value
                FROM bunshin_v2_schema_meta
                WHERE schema_key = 'schema_version'
                """
            ).fetchone()
    except sqlite3.Error:
        return -1
    try:
        return int(row[0]) if row is not None else -1
    except (TypeError, ValueError):
        return -1


def _require_manager_stopped(runtime_root: Path) -> None:
    try:
        health = BunshinManagerClient(
            runtime_root,
            request_timeout_seconds=0.5,
        ).health_sync()
    except Exception:
        return
    if bool(health.get("ok")):
        raise RuntimeError(
            "Bunshin runtime cutover requires the Bunshin sidecar to be detached "
            "and all worker processes to be stopped"
        )


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "BunshinRuntimeCutoverResult",
    "cutover_bunshin_runtime",
]
