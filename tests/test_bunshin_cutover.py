from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

from pal.bunshin.config import BUNSHIN_DB_FILENAME
from pal.bunshin.catalog import BunshinCatalogService
from pal.bunshin.cutover import cutover_bunshin_runtime
from pal.bunshin.ipc import bunshin_runtime_dir
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.schema import (
    BUNSHIN_SCHEMA_VERSION,
    ensure_bunshin_schema,
)
from pal.bunshin.service import BunshinWorkflowService


class BunshinCurrentCutoverTests(unittest.TestCase):
    def test_fresh_schema_is_current_and_legacy_schema_is_rejected(self) -> None:
        with sqlite3.connect(":memory:") as connection:
            ensure_bunshin_schema(connection)
            version = connection.execute(
                "SELECT schema_value FROM bunshin_schema_meta "
                "WHERE schema_key = 'schema_version'"
            ).fetchone()[0]
            self.assertEqual(version, str(BUNSHIN_SCHEMA_VERSION))
            connection.execute(
                "UPDATE bunshin_schema_meta SET schema_value = '24' "
                "WHERE schema_key = 'schema_version'"
            )
            with self.assertRaisesRegex(RuntimeError, "not migrated in place"):
                ensure_bunshin_schema(connection)

    def test_cutover_archives_old_runtime_and_copies_only_user_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir)
            source = bunshin_runtime_dir(runtime_root)
            source.mkdir(parents=True)
            with sqlite3.connect(str(source / BUNSHIN_DB_FILENAME)) as connection:
                connection.execute(
                    "CREATE TABLE bunshin_schema_meta("
                    "schema_key TEXT PRIMARY KEY, schema_value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO bunshin_schema_meta VALUES "
                    "('schema_version', '24')"
                )
            catalog = BunshinCatalogService(runtime_root)
            catalog.bootstrap()
            catalog.set_profile_override(
                profile="software_engineering.v2_coder",
                changes={"display_name": "Custom Coder"},
            )
            catalog.set_family_override(
                family="software_engineering",
                changes={"display_name": "Custom Software Engineering"},
            )
            (source / "legacy-worker-state.json").write_text(
                "{}",
                encoding="utf-8",
            )

            result = cutover_bunshin_runtime(runtime_root)

            self.assertEqual(result.status, "archived_and_initialized")
            self.assertEqual(result.previous_version, 24)
            self.assertEqual(result.copied_profile_overrides, 1)
            self.assertEqual(result.copied_family_overrides, 1)
            archive = Path(result.archive_root)
            self.assertTrue((archive / "legacy-worker-state.json").is_file())
            self.assertFalse((source / "legacy-worker-state.json").exists())
            self.assertTrue(
                (
                    source
                    / "catalog"
                    / "profile_overrides"
                    / "software_engineering"
                    / "v2_coder.json"
                ).is_file()
            )
            self.assertTrue(
                (
                    source
                    / "catalog"
                    / "family_overrides"
                    / "software_engineering.json"
                ).is_file()
            )
            with sqlite3.connect(str(source / BUNSHIN_DB_FILENAME)) as connection:
                version = connection.execute(
                    "SELECT schema_value FROM bunshin_schema_meta "
                    "WHERE schema_key = 'schema_version'"
                ).fetchone()[0]
            self.assertEqual(version, str(BUNSHIN_SCHEMA_VERSION))

    def test_invalid_override_aborts_before_archive_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir)
            source = bunshin_runtime_dir(runtime_root)
            source.mkdir(parents=True)
            with sqlite3.connect(str(source / BUNSHIN_DB_FILENAME)) as connection:
                connection.execute(
                    "CREATE TABLE bunshin_schema_meta("
                    "schema_key TEXT PRIMARY KEY, schema_value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO bunshin_schema_meta VALUES "
                    "('schema_version', '24')"
                )
            invalid = source / "catalog" / "profile_overrides" / "invalid.json"
            invalid.parent.mkdir(parents=True)
            invalid.write_text("[]", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "JSON object"):
                cutover_bunshin_runtime(runtime_root)

            self.assertTrue(source.is_dir())
            self.assertFalse((runtime_root / "data" / "bunshin-archive").exists())

    def test_unknown_existing_database_is_archived_instead_of_treated_as_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir)
            source = bunshin_runtime_dir(runtime_root)
            source.mkdir(parents=True)
            with sqlite3.connect(str(source / BUNSHIN_DB_FILENAME)) as connection:
                connection.execute("CREATE TABLE legacy_worker_state(value TEXT)")
                connection.execute("INSERT INTO legacy_worker_state VALUES ('kept')")

            result = cutover_bunshin_runtime(runtime_root)

            self.assertEqual(result.status, "archived_and_initialized")
            self.assertEqual(result.previous_version, -1)
            self.assertIn("-vunknown", Path(result.archive_root).name)
            with sqlite3.connect(str(Path(result.archive_root) / BUNSHIN_DB_FILENAME)) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM legacy_worker_state").fetchone()[0],
                    "kept",
                )
            with sqlite3.connect(str(source / BUNSHIN_DB_FILENAME)) as connection:
                version = connection.execute(
                    "SELECT schema_value FROM bunshin_schema_meta "
                    "WHERE schema_key = 'schema_version'"
                ).fetchone()[0]
            self.assertEqual(version, str(BUNSHIN_SCHEMA_VERSION))


@pytest.mark.parametrize("phase", ["archive", "install"])
def test_archive_cutover_resumes_after_process_exit(tmp_path, phase):
    source = bunshin_runtime_dir(tmp_path)
    source.mkdir(parents=True)
    with sqlite3.connect(source / BUNSHIN_DB_FILENAME) as db:
        db.execute("CREATE TABLE bunshin_schema_meta(schema_key TEXT PRIMARY KEY, schema_value TEXT)")
        db.execute("INSERT INTO bunshin_schema_meta VALUES ('schema_version', '24')")
    catalog = BunshinCatalogService(tmp_path)
    catalog.bootstrap()
    catalog.set_profile_override(profile="software_engineering.v2_coder", changes={"display_name": "Preserved"})
    script = """
import os
import sys
from pathlib import Path
from pal.bunshin.cutover import cutover_bunshin_runtime
from pal.bunshin.ipc import bunshin_runtime_dir

root = Path(sys.argv[1])
phase = sys.argv[2]
source = bunshin_runtime_dir(root)
replace = os.replace

def interrupt(old, new):
    replace(old, new)
    if (phase == 'archive' and Path(old) == source) or (phase == 'install' and Path(new) == source):
        os._exit(86)

os.replace = interrupt
cutover_bunshin_runtime(root)
"""
    process = subprocess.run([sys.executable, "-c", script, str(tmp_path), phase], capture_output=True, text=True, timeout=35)
    assert process.returncode == 86, process.stderr
    with pytest.raises(RuntimeError, match="archive cutover is pending"):
        BunshinRepository(tmp_path).database.ensure_schema()
    with pytest.raises(RuntimeError, match="archive cutover is pending"):
        BunshinWorkflowService(tmp_path).submit_artifact({"artifact_type": "Test", "content": {}})
    with pytest.raises(RuntimeError, match="archive cutover is pending"):
        BunshinCatalogService(tmp_path).bootstrap()
    if phase == "archive":
        assert not source.exists()

    result = cutover_bunshin_runtime(tmp_path)
    assert result.status == "archived_and_initialized"
    assert result.copied_profile_overrides == 1
    assert (Path(result.archive_root) / BUNSHIN_DB_FILENAME).is_file()
    override = source / "catalog/profile_overrides/software_engineering/v2_coder.json"
    assert "Preserved" in override.read_text(encoding="utf-8")
    assert cutover_bunshin_runtime(tmp_path).status == "already_current"


if __name__ == "__main__":
    unittest.main()
