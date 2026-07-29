from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from pal.minion.config import MINION_DB_FILENAME
from pal.minion.catalog import MinionCatalogService
from pal.minion.cutover import cutover_minion_runtime_v25
from pal.minion.ipc import minion_runtime_dir
from pal.minion.v2.schema import (
    MINION_V2_SCHEMA_VERSION,
    ensure_minion_v2_schema,
)


class MinionV25CutoverTests(unittest.TestCase):
    def test_fresh_schema_is_v25_and_legacy_schema_is_rejected(self) -> None:
        with sqlite3.connect(":memory:") as connection:
            ensure_minion_v2_schema(connection)
            version = connection.execute(
                "SELECT schema_value FROM minion_v2_schema_meta "
                "WHERE schema_key = 'schema_version'"
            ).fetchone()[0]
            self.assertEqual(version, str(MINION_V2_SCHEMA_VERSION))
            connection.execute(
                "UPDATE minion_v2_schema_meta SET schema_value = '24' "
                "WHERE schema_key = 'schema_version'"
            )
            with self.assertRaisesRegex(RuntimeError, "not migrated in place"):
                ensure_minion_v2_schema(connection)

    def test_cutover_archives_old_runtime_and_copies_only_user_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir)
            source = minion_runtime_dir(runtime_root)
            source.mkdir(parents=True)
            with sqlite3.connect(str(source / MINION_DB_FILENAME)) as connection:
                connection.execute(
                    "CREATE TABLE minion_v2_schema_meta("
                    "schema_key TEXT PRIMARY KEY, schema_value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO minion_v2_schema_meta VALUES "
                    "('schema_version', '24')"
                )
            catalog = MinionCatalogService(runtime_root)
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

            result = cutover_minion_runtime_v25(runtime_root)

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
            with sqlite3.connect(str(source / MINION_DB_FILENAME)) as connection:
                version = connection.execute(
                    "SELECT schema_value FROM minion_v2_schema_meta "
                    "WHERE schema_key = 'schema_version'"
                ).fetchone()[0]
            self.assertEqual(version, str(MINION_V2_SCHEMA_VERSION))

    def test_invalid_override_aborts_before_archive_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir)
            source = minion_runtime_dir(runtime_root)
            source.mkdir(parents=True)
            with sqlite3.connect(str(source / MINION_DB_FILENAME)) as connection:
                connection.execute(
                    "CREATE TABLE minion_v2_schema_meta("
                    "schema_key TEXT PRIMARY KEY, schema_value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO minion_v2_schema_meta VALUES "
                    "('schema_version', '24')"
                )
            invalid = source / "catalog" / "profile_overrides" / "invalid.json"
            invalid.parent.mkdir(parents=True)
            invalid.write_text("[]", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "JSON object"):
                cutover_minion_runtime_v25(runtime_root)

            self.assertTrue(source.is_dir())
            self.assertFalse((runtime_root / "data" / "minion-archive").exists())

    def test_unknown_existing_database_is_archived_instead_of_treated_as_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir)
            source = minion_runtime_dir(runtime_root)
            source.mkdir(parents=True)
            with sqlite3.connect(str(source / MINION_DB_FILENAME)) as connection:
                connection.execute("CREATE TABLE legacy_worker_state(value TEXT)")
                connection.execute("INSERT INTO legacy_worker_state VALUES ('kept')")

            result = cutover_minion_runtime_v25(runtime_root)

            self.assertEqual(result.status, "archived_and_initialized")
            self.assertEqual(result.previous_version, -1)
            self.assertIn("-vunknown", Path(result.archive_root).name)
            with sqlite3.connect(str(Path(result.archive_root) / MINION_DB_FILENAME)) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM legacy_worker_state").fetchone()[0],
                    "kept",
                )
            with sqlite3.connect(str(source / MINION_DB_FILENAME)) as connection:
                version = connection.execute(
                    "SELECT schema_value FROM minion_v2_schema_meta "
                    "WHERE schema_key = 'schema_version'"
                ).fetchone()[0]
            self.assertEqual(version, str(MINION_V2_SCHEMA_VERSION))


if __name__ == "__main__":
    unittest.main()
