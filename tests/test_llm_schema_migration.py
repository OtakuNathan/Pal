from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pal.llm.schema import LLMEndpointSchemaError, assert_llm_endpoint_schema_current, migrate_llm_endpoint_schema


class LLMEndpointSchemaMigrationTests(unittest.TestCase):
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
