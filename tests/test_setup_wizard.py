"""Tests for the interactive setup wizard.

Covers:
- multiline_input helper
- seed_from_wizard integration (identity, endpoints, channel, secrets, settings)
- re-run / upsert on existing database
- systemd service generation and conflict avoidance
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from pal.wizard import WizardService
from pal.wizard.prompts import (
    WizardChannel,
    WizardCollectedData,
    WizardIdentity,
    WizardLLMEndpoint,
    multiline_input,
)


def _make_collected() -> WizardCollectedData:
    return WizardCollectedData(
        identity=WizardIdentity(
            display_name="TestPal",
            language="zh",
            vibe="Warm and concise.",
            tone="Direct",
            core_policy=["Never fabricate memory."],
            timezone="Asia/Shanghai",
        ),
        endpoints=[
            WizardLLMEndpoint(
                endpoint_id="test-claude",
                model_id="claude-sonnet-4-20250514",
                api_mode="anthropic_messages",
                base_url="https://api.anthropic.com/v1/messages",
                api_key="sk-test-key-123",
                context_window=200000,
                max_output_tokens=16384,
                supports_reasoning=True,
                supports_tools=True,
                supports_streaming=True,
                supports_vision=True,
                priority=0,
            ),
        ],
        channel=WizardChannel(
            endpoint_id="socket_default",
            channel_kind="socket",
            binding_key="/tmp/test-pal-wizard/pal.sock",
        ),
        active_endpoint_id="test-claude",
    )


class TestMultilineInput(unittest.TestCase):
    @patch("builtins.input")
    def test_single_line_then_sentinel(self, mock_input: object) -> None:
        mock_input.side_effect = ["hello world", "."]
        result = multiline_input("Test")
        self.assertEqual(result, "hello world")

    @patch("builtins.input")
    def test_multiple_lines(self, mock_input: object) -> None:
        mock_input.side_effect = ["line 1", "line 2", "line 3", "."]
        result = multiline_input("Test")
        self.assertEqual(result, "line 1\nline 2\nline 3")

    @patch("builtins.input")
    def test_empty_input(self, mock_input: object) -> None:
        mock_input.side_effect = ["."]
        result = multiline_input("Test")
        self.assertEqual(result, "")

    @patch("builtins.input")
    def test_eof_terminates(self, mock_input: object) -> None:
        mock_input.side_effect = EOFError
        result = multiline_input("Test")
        self.assertEqual(result, "")


class TestSeedFromWizard(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal_wizard_test_"))
        self.wizard = WizardService()
        self.registration = self.wizard.provision_runtime(
            display_name="Wizard Test",
            runtime_root=self.runtime_root,
            db_filename="pal_test.sqlite3",
            pal_entrypoint="pal.runtime.test",
        )
        self.database = self.wizard.create_database(self.registration)

    def tearDown(self) -> None:
        self.database.close()
        shutil.rmtree(self.runtime_root, ignore_errors=True)

    def test_seed_from_wizard_writes_identity(self) -> None:
        from pal.identity import IdentityRepository

        collected = _make_collected()
        self.wizard.seed_from_wizard(self.registration, collected)

        repo = IdentityRepository()
        persona = repo.get_persona()
        self.assertIsNotNone(persona)
        self.assertEqual(persona.display_name, "TestPal")
        self.assertEqual(persona.language, "zh")
        self.assertEqual(persona.vibe, "Warm and concise.")

    def test_seed_from_wizard_writes_llm_endpoint(self) -> None:
        from pal.llm import LLMEndpointRepository

        collected = _make_collected()
        self.wizard.seed_from_wizard(self.registration, collected)

        repo = LLMEndpointRepository()
        endpoints = repo.list_enabled()
        self.assertEqual(len(endpoints), 1)
        self.assertEqual(endpoints[0].endpoint_id, "test-claude")
        self.assertEqual(endpoints[0].model_id, "claude-sonnet-4-20250514")
        self.assertEqual(endpoints[0].api_mode, "anthropic_messages")
        self.assertEqual(endpoints[0].context_window, 200000)

    def test_seed_from_wizard_stores_api_key(self) -> None:
        from pal.llm import LLMEndpointRepository, LiteLLMCredentialResolver
        from pal.llm.secret_store import EncryptedFileSecretStore, SecretRef

        collected = _make_collected()
        self.wizard.seed_from_wizard(self.registration, collected)

        secrets_path = self.runtime_root / "secrets.json"
        store = EncryptedFileSecretStore(str(secrets_path))
        key = store.get_secret(SecretRef(service="test-claude", account="api-key"))
        self.assertEqual(key, "sk-test-key-123")
        endpoint = LLMEndpointRepository().get_primary_enabled()
        self.assertIsNotNone(endpoint)
        self.assertEqual(LiteLLMCredentialResolver(secret_store=store).resolve_api_key(endpoint), "sk-test-key-123")

    def test_seed_from_wizard_sets_active_endpoint(self) -> None:
        from pal.llm import RuntimeSettingRepository

        collected = _make_collected()
        self.wizard.seed_from_wizard(self.registration, collected)

        settings = RuntimeSettingRepository()
        active = settings.get_active_llm_endpoint_id()
        self.assertEqual(active, "test-claude")

    def test_seed_from_wizard_writes_channel(self) -> None:
        from pal.channel import ChannelEndpointRepository

        collected = _make_collected()
        self.wizard.seed_from_wizard(self.registration, collected)

        repo = ChannelEndpointRepository()
        ep = repo.get("socket_default")
        self.assertIsNotNone(ep)
        self.assertEqual(ep.channel_kind, "socket")

    def test_seed_from_wizard_writes_telegram_channel(self) -> None:
        from pal.channel import ChannelEndpointRepository

        collected = _make_collected()
        collected.channel = WizardChannel(
            endpoint_id="telegram_main",
            channel_kind="telegram",
            binding_key="chat:12345",
            binding_metadata={"bot_token": "123456:ABC"},
            supports_typing=True,
            supports_receipt_marker=True,
        )
        self.wizard.seed_from_wizard(self.registration, collected)

        repo = ChannelEndpointRepository()
        ep = repo.get("telegram_main")
        self.assertIsNotNone(ep)
        self.assertEqual(ep.channel_kind, "telegram")
        self.assertEqual(ep.binding_metadata.get("bot_token"), "123456:ABC")

    def test_seed_from_wizard_seeds_web_providers(self) -> None:
        from pal.llm import RuntimeSettingRepository

        collected = _make_collected()
        self.wizard.seed_from_wizard(self.registration, collected)

        settings = RuntimeSettingRepository()
        self.assertEqual(settings.get("active_web_search_provider_id"), "brave_search_default")
        self.assertEqual(settings.get("active_web_fetch_provider_id"), "playwright_fetch_default")

    def test_rerun_seed_from_wizard_updates_in_place(self) -> None:
        from pal.identity import IdentityRepository
        from pal.llm import LLMEndpointRepository

        collected = _make_collected()
        self.wizard.seed_from_wizard(self.registration, collected)

        updated = _make_collected()
        updated.identity.display_name = "UpdatedPal"
        updated.endpoints[0] = WizardLLMEndpoint(
            endpoint_id="test-claude",
            model_id="claude-opus-4-20250514",
            api_mode="anthropic_messages",
            base_url="https://api.anthropic.com/v1/messages",
            api_key=None,
            context_window=200000,
            max_output_tokens=16384,
            supports_reasoning=True,
            supports_tools=True,
            supports_streaming=True,
            supports_vision=True,
            priority=0,
        )
        self.wizard.seed_from_wizard(self.registration, updated)

        persona = IdentityRepository().get_persona()
        self.assertEqual(persona.display_name, "UpdatedPal")

        endpoints = LLMEndpointRepository().list_enabled()
        self.assertEqual(len(endpoints), 1)
        self.assertEqual(endpoints[0].model_id, "claude-opus-4-20250514")

    def test_seed_from_wizard_multiple_endpoints(self) -> None:
        from pal.llm import LLMEndpointRepository

        collected = _make_collected()
        collected.endpoints.append(
            WizardLLMEndpoint(
                endpoint_id="deepseek-fallback",
                model_id="deepseek-chat",
                api_mode="openai_chat",
                base_url="https://api.deepseek.com/v1/chat/completions",
                api_key="ds-test-key",
                context_window=64000,
                max_output_tokens=8192,
                supports_reasoning=False,
                supports_tools=True,
                supports_streaming=True,
                supports_vision=False,
                priority=1,
            ),
        )
        self.wizard.seed_from_wizard(self.registration, collected)

        endpoints = LLMEndpointRepository().list_enabled()
        self.assertEqual(len(endpoints), 2)
        self.assertEqual(endpoints[0].endpoint_id, "test-claude")
        self.assertEqual(endpoints[1].endpoint_id, "deepseek-fallback")


class TestServiceGeneration(unittest.TestCase):
    def test_generate_service_content_no_debug(self) -> None:
        from pal.wizard.cli import _generate_service_content

        content = _generate_service_content(
            pal_bin="/usr/local/bin/pal",
            runtime_root=Path("/home/test/.pal"),
        )
        self.assertIn("ExecStart=/usr/local/bin/pal run --runtime-root /home/test/.pal\n", content)
        self.assertNotIn("--debug-prompt", content)
        self.assertIn("Restart=on-failure", content)
        self.assertIn("StandardOutput=append:/home/test/.pal/pal.log", content)
        self.assertIn("WantedBy=default.target", content)

    def test_pick_service_name_no_conflict(self) -> None:
        from pal.wizard import cli as cli_mod

        tmp = Path(tempfile.mkdtemp(prefix="pal_svc_test_"))
        original = cli_mod._SYSTEMD_USER_DIR
        cli_mod._SYSTEMD_USER_DIR = tmp
        try:
            result = cli_mod._pick_service_name(Path("/home/test/.pal"))
            self.assertEqual(result, "pal.service")
        finally:
            cli_mod._SYSTEMD_USER_DIR = original
            shutil.rmtree(tmp, ignore_errors=True)

    def test_pick_service_name_same_runtime_reuses(self) -> None:
        from pal.wizard import cli as cli_mod

        tmp = Path(tempfile.mkdtemp(prefix="pal_svc_test_"))
        original = cli_mod._SYSTEMD_USER_DIR
        cli_mod._SYSTEMD_USER_DIR = tmp
        try:
            (tmp / "pal.service").write_text(
                "ExecStart=pal run --runtime-root /home/test/.pal\n", encoding="utf-8"
            )
            result = cli_mod._pick_service_name(Path("/home/test/.pal"))
            self.assertEqual(result, "pal.service")
        finally:
            cli_mod._SYSTEMD_USER_DIR = original
            shutil.rmtree(tmp, ignore_errors=True)

    def test_pick_service_name_conflict_uses_dedicated_name(self) -> None:
        from pal.wizard import cli as cli_mod

        tmp = Path(tempfile.mkdtemp(prefix="pal_svc_test_"))
        original = cli_mod._SYSTEMD_USER_DIR
        cli_mod._SYSTEMD_USER_DIR = tmp
        try:
            # pal.service points to a different runtime_root
            (tmp / "pal.service").write_text(
                "ExecStart=pal run --runtime-root /home/other/.pal\n", encoding="utf-8"
            )
            result = cli_mod._pick_service_name(Path("/home/test/.testpal"))
            self.assertEqual(result, "pal@testpal.service")
        finally:
            cli_mod._SYSTEMD_USER_DIR = original
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
