from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pal.llm.schema import LLMEndpointSchemaError, assert_llm_endpoint_schema_current, migrate_llm_endpoint_schema
from pal.foundation import PalV2Database
from pal.llm.models import LLMEndpointModel
from pal.llm.endpoint_spec import LLMEndpointSpec
from tests.test_chatgpt_subscription import endpoint


class LLMEndpointSchemaMigrationTests(unittest.TestCase):
    def _create_previous_schema(self, path: Path) -> None:
        database = PalV2Database(path)
        database.initialize((LLMEndpointModel,))
        try:
            spec = LLMEndpointSpec.from_value(endpoint())
            LLMEndpointModel.create(**spec.to_payload())
            api = spec.to_payload()
            api.update(endpoint_id="api", auth_kind="api_key_ref", capabilities_blob={}, transport="http")
            LLMEndpointModel.create(**api)
        finally:
            database.close()
        with sqlite3.connect(path) as connection:
            connection.execute("ALTER TABLE llm_endpoints DROP COLUMN transport")

    def test_current_shape_schema_adds_transport_and_preserves_rows_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pal.sqlite3"
            self._create_previous_schema(path)
            with sqlite3.connect(path) as connection:
                before = connection.execute("SELECT endpoint_id, credential_ref, model_id, capabilities_blob FROM llm_endpoints ORDER BY endpoint_id").fetchall()
                with self.assertRaises(LLMEndpointSchemaError):
                    assert_llm_endpoint_schema_current(connection)
            result = migrate_llm_endpoint_schema(path)
            self.assertEqual((result.status, result.migrated_rows), ("migrated", 2))
            with sqlite3.connect(path) as connection:
                assert_llm_endpoint_schema_current(connection)
                self.assertEqual(before, connection.execute("SELECT endpoint_id, credential_ref, model_id, capabilities_blob FROM llm_endpoints ORDER BY endpoint_id").fetchall())
                self.assertEqual(connection.execute("SELECT endpoint_id, transport FROM llm_endpoints ORDER BY endpoint_id").fetchall(), [("api", "http"), ("chatgpt", "websocket")])
            self.assertEqual(migrate_llm_endpoint_schema(path).status, "current")

    def test_failed_transport_upgrade_rolls_back_schema_and_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pal.sqlite3"
            self._create_previous_schema(path)
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE llm_endpoints SET default_thinking_level='unsupported'")
                before = connection.execute("SELECT * FROM llm_endpoints ORDER BY endpoint_id").fetchall()
            with self.assertRaises(LLMEndpointSchemaError):
                migrate_llm_endpoint_schema(path)
            with sqlite3.connect(path) as connection:
                self.assertNotIn("transport", {row[1] for row in connection.execute("PRAGMA table_info(llm_endpoints)")})
                self.assertEqual(before, connection.execute("SELECT * FROM llm_endpoints ORDER BY endpoint_id").fetchall())

    def test_old_endpoint_schema_is_atomically_replaced(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="pal-llm-schema-"))
        path = root / "pal.sqlite3"
        database = sqlite3.connect(path)
        database.execute(
            """
            CREATE TABLE llm_endpoints (
                endpoint_id TEXT PRIMARY KEY, provider TEXT, model_id TEXT,
                display_name TEXT, api_mode TEXT, base_url TEXT, auth_kind TEXT,
                credential_ref TEXT, context_window INTEGER, max_output_tokens INTEGER,
                supports_reasoning INTEGER, supports_tools INTEGER,
                supports_streaming INTEGER, supports_vision INTEGER,
                input_modalities_blob JSON, output_modalities_blob JSON,
                priority INTEGER, enabled INTEGER, capabilities_blob JSON,
                notes TEXT, created_at TEXT, updated_at TEXT
            )
            """
        )
        database.execute(
            "INSERT INTO llm_endpoints VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ep", "openai", "model", None, "openai_chat", "https://example.test", "api_key_ref",
                "key", 1000, 100, 1, 1, 1, 0, json.dumps(["text"]), json.dumps(["text"]),
                0, 1, json.dumps({"openai_responses": True}), None, "now", "now",
            ),
        )
        database.commit()
        with self.assertRaises(LLMEndpointSchemaError):
            assert_llm_endpoint_schema_current(database)
        database.close()

        result = migrate_llm_endpoint_schema(path)
        self.assertEqual(result.status, "migrated")
        database = sqlite3.connect(path)
        assert_llm_endpoint_schema_current(database)
        row = database.execute(
            "SELECT wire_shape, thinking_levels_blob, default_thinking_level FROM llm_endpoints"
        ).fetchone()
        self.assertEqual(row[0], "openai_response")
        self.assertIn("medium", json.loads(row[1]))
        self.assertEqual(row[2], "medium")
        columns = {item[1] for item in database.execute("PRAGMA table_info(llm_endpoints)")}
        self.assertNotIn("api_mode", columns)
        self.assertNotIn("supports_reasoning", columns)
        database.close()


if __name__ == "__main__":
    unittest.main()
