from __future__ import annotations

import argparse
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pal.foundation import PalV2Database
from pal.llm.cli import configure_llm_parser, run_llm_cli
from pal.llm.models import LLMEndpointModel, PalRuntimeSettingModel


class LLMCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal-llm-cli-"))
        database = PalV2Database(self.runtime_root / "pal.sqlite3")
        database.initialize((LLMEndpointModel, PalRuntimeSettingModel))
        database.close()

    def _parse(self, *argv: str) -> argparse.Namespace:
        parser = argparse.ArgumentParser()
        configure_llm_parser(parser)
        return parser.parse_args(list(argv))

    def test_add_deepseek_defaults_to_official_anthropic_shape(self) -> None:
        args = self._parse(
            "add",
            "deepseek-v4-flash",
            "--runtime-root",
            str(self.runtime_root),
            "--credential-ref",
            "DEEPSEEK_API_KEY",
            "--set-active",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_llm_cli(args), 0)

        list_args = self._parse("list", "--runtime-root", str(self.runtime_root), "--json")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(run_llm_cli(list_args), 0)
        payload = json.loads(output.getvalue())
        endpoint = payload["items"][0]
        self.assertEqual(endpoint["provider"], "deepseek")
        self.assertEqual(endpoint["wire_shape"], "anthropic_messages")
        self.assertEqual(endpoint["base_url"], "https://api.deepseek.com/anthropic")
        self.assertEqual(endpoint["thinking_levels"], ["off", "high", "max"])
        self.assertEqual(payload["active_endpoint_id"], "deepseek-v4-flash")

    def test_add_gpt_6_astra_uses_official_responses_profile(self) -> None:
        args = self._parse(
            "add",
            "gpt-6-astra",
            "--runtime-root",
            str(self.runtime_root),
            "--api-key-env",
            "OPENAI_API_KEY",
            "--no-enabled",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_llm_cli(args), 0)

        list_args = self._parse(
            "list",
            "--runtime-root",
            str(self.runtime_root),
            "--all",
            "--json",
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(run_llm_cli(list_args), 0)
        endpoint = json.loads(output.getvalue())["items"][0]

        self.assertEqual(endpoint["provider"], "openai")
        self.assertEqual(endpoint["wire_shape"], "openai_response")
        self.assertEqual(endpoint["base_url"], "https://api.openai.com/v1")
        self.assertEqual(endpoint["context_window"], 1_050_000)
        self.assertEqual(endpoint["max_output_tokens"], 128_000)
        self.assertEqual(
            endpoint["thinking_levels"],
            ["low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual(endpoint["default_thinking_level"], "medium")
        self.assertTrue(endpoint["supports_tools"])
        self.assertTrue(endpoint["supports_streaming"])
        self.assertTrue(endpoint["supports_vision"])
        self.assertFalse(endpoint["enabled"])
        self.assertEqual(
            endpoint["capabilities"]["unsupported_request_parameters"],
            ["temperature", "top_p", "top_logprobs"],
        )

    def test_list_reports_corrupt_endpoint_contract_instead_of_returning_empty(self) -> None:
        args = self._parse(
            "add",
            "demo",
            "--runtime-root",
            str(self.runtime_root),
            "--base-url",
            "https://example.test/v1",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_llm_cli(args), 0)
        with sqlite3.connect(self.runtime_root / "pal.sqlite3") as database:
            database.execute(
                "UPDATE llm_endpoints SET default_thinking_level = 'max' WHERE endpoint_id = 'demo'"
            )

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = run_llm_cli(
                self._parse("list", "--runtime-root", str(self.runtime_root), "--json")
            )

        self.assertEqual(result, 2)
        self.assertIn("default_thinking_level='max' is not declared", stderr.getvalue())

    def test_duplicate_requires_replace_and_replace_preserves_omitted_fields(self) -> None:
        initial = self._parse(
            "add",
            "demo",
            "--runtime-root",
            str(self.runtime_root),
            "--model-id",
            "demo-model",
            "--base-url",
            "https://example.test/v1",
            "--credential-ref",
            "DEMO_API_KEY",
            "--priority",
            "7",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_llm_cli(initial), 0)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run_llm_cli(initial), 2)

        replacement = self._parse(
            "add",
            "demo",
            "--runtime-root",
            str(self.runtime_root),
            "--replace",
            "--display-name",
            "Demo renamed",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_llm_cli(replacement), 0)
        list_args = self._parse("list", "--runtime-root", str(self.runtime_root), "--json")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(run_llm_cli(list_args), 0)
        endpoint = json.loads(output.getvalue())["items"][0]
        self.assertEqual(endpoint["model_id"], "demo-model")
        self.assertEqual(endpoint["priority"], 7)
        self.assertEqual(endpoint["display_name"], "Demo renamed")

    def test_delete_active_endpoint_selects_next_enabled_then_clears_last(self) -> None:
        first = self._parse(
            "add",
            "first",
            "--runtime-root",
            str(self.runtime_root),
            "--base-url",
            "https://example.test/v1",
            "--set-active",
        )
        second = self._parse(
            "add",
            "second",
            "--runtime-root",
            str(self.runtime_root),
            "--base-url",
            "https://example.test/v1",
            "--priority",
            "1",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_llm_cli(first), 0)
            self.assertEqual(run_llm_cli(second), 0)

        delete_first = self._parse("delete", "first", "--runtime-root", str(self.runtime_root))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(run_llm_cli(delete_first), 0)
        self.assertIn("Active endpoint moved to 'second'", output.getvalue())

        list_args = self._parse("list", "--runtime-root", str(self.runtime_root), "--json")
        listed = io.StringIO()
        with contextlib.redirect_stdout(listed):
            self.assertEqual(run_llm_cli(list_args), 0)
        payload = json.loads(listed.getvalue())
        self.assertEqual(payload["active_endpoint_id"], "second")
        self.assertEqual([item["endpoint_id"] for item in payload["items"]], ["second"])

        delete_second = self._parse("delete", "second", "--runtime-root", str(self.runtime_root))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_llm_cli(delete_second), 0)
        listed = io.StringIO()
        with contextlib.redirect_stdout(listed):
            self.assertEqual(run_llm_cli(list_args), 0)
        payload = json.loads(listed.getvalue())
        self.assertIsNone(payload["active_endpoint_id"])
        self.assertEqual(payload["items"], [])

    def test_delete_unknown_endpoint_fails_without_mutation(self) -> None:
        args = self._parse("delete", "missing", "--runtime-root", str(self.runtime_root))
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertEqual(run_llm_cli(args), 2)
        self.assertIn("does not exist", error.getvalue())
