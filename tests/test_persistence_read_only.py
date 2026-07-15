from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from peewee import OperationalError, TextField

from pal.foundation.persistence import BaseModel, PalV2Database
from pal.memory import L3CommitRequest, MemoryQuery, MemoryService
from pal.plugins.l3.sqlite_vec import SQLiteVecL3Plugin
from pal.wizard.runtime import ALL_MODELS


class ReadOnlyProbe(BaseModel):
    value = TextField(primary_key=True)


class PalV2ReadOnlyDatabaseTests(unittest.TestCase):
    def test_existing_database_can_be_queried_without_schema_writes(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="pal-read-only-db-"))
        path = root / "pal.sqlite3"
        writable = PalV2Database(path)
        writable.initialize([ReadOnlyProbe])
        ReadOnlyProbe.create(value="remembered")
        writable.close()

        read_only = PalV2Database(path, read_only=True)
        read_only.initialize([ReadOnlyProbe])
        self.assertEqual(ReadOnlyProbe.get().value, "remembered")
        with self.assertRaises(OperationalError):
            ReadOnlyProbe.create(value="must-not-write")
        read_only.close()

    def test_read_only_database_must_already_exist(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="pal-read-only-db-missing-"))
        database = PalV2Database(root / "missing.sqlite3", read_only=True)
        with self.assertRaises(FileNotFoundError):
            database.initialize([ReadOnlyProbe])

    def test_read_only_minion_memory_can_recall_without_usage_writes(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="pal-read-only-recall-"))
        path = root / "pal.sqlite3"
        writable = PalV2Database(path)
        writable.initialize(ALL_MODELS)
        writable_provider = SQLiteVecL3Plugin(service=MemoryService())
        committed = writable_provider.commit(
            L3CommitRequest(
                kind="fact",
                scope="system",
                title="OHOS ownership",
                summary="OHOS drawing consumers borrow font handles.",
                search_text="OHOS drawing consumers borrow font handles from the font owner.",
                topics=["ohos", "ownership"],
            )
        )
        self.assertEqual(committed.status, "ok")
        writable.close()

        with patch.dict("os.environ", {"PAL_DATABASE_READ_ONLY": "1"}):
            read_only = PalV2Database(path, read_only=True)
            read_only.initialize(ALL_MODELS)
            provider = SQLiteVecL3Plugin(service=MemoryService(), read_only=True)
            recalled = provider.recall(
                MemoryQuery(queries=["OHOS borrowed font handle ownership"], limit=3)
            )

            self.assertTrue(recalled.hits)
            self.assertIn("borrow", str(recalled.hits[0]["summary"]).lower())
            read_only.close()


if __name__ == "__main__":
    unittest.main()
