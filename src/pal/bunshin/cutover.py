from __future__ import annotations

import json
import os
import shutil
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pal.bunshin.catalog import BunshinCatalogService
from pal.bunshin.catalog_store import atomic_write_json, load_json_objects
from pal.bunshin.config import (
    BUNSHIN_DB_FILENAME,
    ensure_bunshin_runtime_settings_schema,
)
from pal.bunshin.ipc import BunshinManagerClient, bunshin_port_path, bunshin_runtime_dir, bunshin_socket_path
from pal.bunshin.paths import bunshin_archive_cutover_marker, bunshin_runtime_ownership
from pal.bunshin.schema import (
    BUNSHIN_IN_PLACE_VERSIONS,
    BUNSHIN_SCHEMA_VERSION,
    ensure_bunshin_schema,
    schema_meta_table,
)
from pal.bunshin.storage.leases import recorded_role_process_groups
from pal.bunshin.workspace_resources import inherited_process_group_live


@dataclass(frozen=True)
class BunshinRuntimeCutoverResult:
    status: str
    previous_version: int
    archive_root: str = ""
    copied_profile_overrides: int = 0
    copied_family_overrides: int = 0


@dataclass(frozen=True)
class _ArchiveCutover:
    archive_name: str
    staging_name: str
    previous_version: int
    copied_profile_overrides: int
    copied_family_overrides: int

    def __post_init__(self) -> None:
        if (Path(self.archive_name).name != self.archive_name or not self.archive_name[0:1].isdigit()
                or Path(self.staging_name).name != self.staging_name or not self.staging_name.startswith(".bunshin-v")):
            raise ValueError("invalid Bunshin archive cutover path")


def cutover_bunshin_runtime(runtime_root: Path) -> BunshinRuntimeCutoverResult:
    """Upgrade a stopped Bunshin runtime to the current schema."""

    root = Path(runtime_root)
    with bunshin_runtime_ownership(root):
        return _cutover_bunshin_runtime(root)


def _cutover_bunshin_runtime(root: Path) -> BunshinRuntimeCutoverResult:
    marker = bunshin_archive_cutover_marker(root)
    if marker.exists():
        pending = _ArchiveCutover(**json.loads(marker.read_text(encoding="utf-8")))
        return _finish_archive_cutover(root, marker, pending)
    source = bunshin_runtime_dir(root)
    db_path = source / BUNSHIN_DB_FILENAME
    previous_version = _bunshin_schema_version(db_path)
    if previous_version in BUNSHIN_IN_PLACE_VERSIONS:
        with closing(sqlite3.connect(db_path)) as connection:
            require_bunshin_runtime_stopped(root)
            with connection:
                ensure_bunshin_schema(connection, migrate_in_place=True)
        return BunshinRuntimeCutoverResult(status="migrated_in_place", previous_version=previous_version)
    if previous_version > BUNSHIN_SCHEMA_VERSION:
        raise RuntimeError("Bunshin runtime was written by a newer version")
    if previous_version in {0, BUNSHIN_SCHEMA_VERSION}:
        return BunshinRuntimeCutoverResult(
            status=(
                "already_current"
                if previous_version == BUNSHIN_SCHEMA_VERSION
                else "not_required"
            ),
            previous_version=previous_version,
        )
    require_bunshin_runtime_stopped(root)

    profile_source = source / "catalog" / "profile_overrides"
    family_source = source / "catalog" / "family_overrides"
    profiles = load_json_objects(profile_source)
    families = load_json_objects(family_source)

    data_root = root / "data"
    archive_parent = data_root / "bunshin-archive"
    archive_parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    version_label = str(previous_version) if previous_version >= 0 else "unknown"
    pending = _ArchiveCutover(
        archive_name=f"{stamp}-v{version_label}",
        staging_name=f".bunshin-v{BUNSHIN_SCHEMA_VERSION}-{uuid4().hex}",
        previous_version=previous_version,
        copied_profile_overrides=len(profiles),
        copied_family_overrides=len(families),
    )
    staging_runtime_root = data_root / pending.staging_name
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
        _fsync_directory(staging.parent)
        _fsync_directory(staging_runtime_root)
        atomic_write_json(marker, asdict(pending))
    except BaseException:
        if not marker.exists():
            shutil.rmtree(staging_runtime_root, ignore_errors=True)
        raise
    return _finish_archive_cutover(root, marker, pending)


def _finish_archive_cutover(root: Path, marker: Path, pending: _ArchiveCutover) -> BunshinRuntimeCutoverResult:
    source = bunshin_runtime_dir(root)
    archive = marker.parent / "bunshin-archive" / pending.archive_name
    staging_runtime_root = marker.parent / pending.staging_name
    staging = bunshin_runtime_dir(staging_runtime_root)
    require_bunshin_runtime_stopped(root)
    if staging.is_dir():
        if source.exists() == archive.exists():
            raise RuntimeError("Bunshin archive cutover found ambiguous source and archive directories")
        if _bunshin_schema_version(staging / BUNSHIN_DB_FILENAME) != BUNSHIN_SCHEMA_VERSION:
            raise RuntimeError("Bunshin archive cutover staging database is unavailable")
        if source.exists():
            if _bunshin_schema_version(source / BUNSHIN_DB_FILENAME) != pending.previous_version:
                raise RuntimeError("Bunshin source schema changed during archive cutover")
            os.replace(source, archive)
        _fsync_directory(archive.parent)
        _fsync_directory(marker.parent)
        os.replace(staging, source)
    if not archive.is_dir() or _bunshin_schema_version(source / BUNSHIN_DB_FILENAME) != BUNSHIN_SCHEMA_VERSION:
        raise RuntimeError("Bunshin archive cutover cannot verify the installed runtime and archive")
    _fsync_directory(marker.parent)
    if staging.parent.is_dir():
        _fsync_directory(staging.parent)
    marker.unlink()
    _fsync_directory(marker.parent)
    shutil.rmtree(staging_runtime_root, ignore_errors=True)
    return BunshinRuntimeCutoverResult(
        status="archived_and_initialized",
        previous_version=pending.previous_version,
        archive_root=str(archive),
        copied_profile_overrides=pending.copied_profile_overrides,
        copied_family_overrides=pending.copied_family_overrides,
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
        ensure_bunshin_schema(connection)
        ensure_bunshin_runtime_settings_schema(connection)
        connection.commit()
    _fsync_directory(staging)


def _bunshin_schema_version(db_path: Path) -> int:
    if not db_path.is_file():
        return 0
    try:
        with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            table = schema_meta_table(connection)
            if connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,),
            ).fetchone() is None:
                return -1
            row = connection.execute(
                f"""
                SELECT schema_value
                FROM {table}
                WHERE schema_key = 'schema_version'
                """
            ).fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError(f"Could not read Bunshin schema version at {db_path}") from exc
    try:
        return int(row[0]) if row is not None else -1
    except (TypeError, ValueError):
        return -1


def require_bunshin_runtime_stopped(runtime_root: Path) -> None:
    try:
        endpoint_present = _manager_endpoint_present(runtime_root)
        if endpoint_present:
            BunshinManagerClient(
                runtime_root,
                request_timeout_seconds=0.5,
            ).health_sync()
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    except Exception as exc:
        raise RuntimeError("Could not verify that the Bunshin Manager has stopped") from exc
    else:
        if endpoint_present:
            raise RuntimeError(
                "Bunshin runtime cutover requires the Bunshin sidecar to be detached "
                "and all worker processes to be stopped"
            )
    _require_recorded_worker_groups_stopped(runtime_root)


def _manager_endpoint_present(runtime_root: Path) -> bool:
    # An absent Unix endpoint can exceed the platform's socket address limit.
    # Check both published transports before attempting a connection. lstat
    # preserves permission and other observation errors for the caller.
    for path in (bunshin_socket_path(runtime_root), bunshin_port_path(runtime_root)):
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        return True
    return False


def _require_recorded_worker_groups_stopped(runtime_root: Path) -> None:
    path = bunshin_runtime_dir(runtime_root) / BUNSHIN_DB_FILENAME
    if _bunshin_schema_version(path) < min(BUNSHIN_IN_PLACE_VERSIONS):
        return
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        groups = recorded_role_process_groups(
            connection, legacy=schema_meta_table(connection) == "bunshin_v2_schema_meta",
            include_released=True,
        )
    if any(inherited_process_group_live(group, birth) for _, _, group, birth in groups):
        raise RuntimeError("Bunshin runtime cutover requires all recorded worker process groups to exit")


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
    "require_bunshin_runtime_stopped",
]
