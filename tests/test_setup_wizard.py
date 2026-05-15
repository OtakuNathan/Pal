"""Tests for the interactive setup wizard.

Covers:
- multiline_input helper
- seed_from_wizard integration (identity, endpoints, channel, secrets, settings)
- re-run / upsert on existing database
- systemd / launchd service generation and conflict avoidance
"""

from __future__ import annotations

import os
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
    build_codex_wizard_endpoints,
    multiline_input,
    normalize_telegram_binding_key,
    run_llm_endpoint_preflight,
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


class TestTelegramWizardInput(unittest.TestCase):
    def test_numeric_user_binding_is_normalized(self) -> None:
        self.assertEqual(normalize_telegram_binding_key("8620024896"), "user:8620024896")

    def test_negative_numeric_chat_binding_is_normalized(self) -> None:
        self.assertEqual(normalize_telegram_binding_key("-10012345"), "chat:-10012345")

    def test_scoped_binding_is_preserved(self) -> None:
        self.assertEqual(normalize_telegram_binding_key("chat:12345"), "chat:12345")


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

    def test_seed_from_wizard_writes_codex_app_server_endpoint(self) -> None:
        from pal.llm import LLMEndpointRepository

        collected = _make_collected()
        collected.endpoints = build_codex_wizard_endpoints(("gpt-5.5",))
        collected.active_endpoint_id = "codex_gpt_5_5"
        self.wizard.seed_from_wizard(self.registration, collected)

        endpoint = LLMEndpointRepository().get_primary_enabled()
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint.endpoint_id, "codex_gpt_5_5")
        self.assertEqual(endpoint.provider, "codex_app_server")
        self.assertEqual(endpoint.model_id, "gpt-5.5")
        self.assertEqual(endpoint.api_mode, "openai_chat")
        self.assertEqual(endpoint.base_url, "codex://app-server")
        self.assertEqual(endpoint.auth_kind, "local_provider_auth")
        self.assertEqual(endpoint.credential_ref, "")
        self.assertTrue(endpoint.supports_reasoning)
        self.assertTrue(endpoint.supports_tools)
        self.assertTrue(endpoint.supports_streaming)
        self.assertTrue(endpoint.supports_vision)
        self.assertTrue(endpoint.capabilities_blob["official_codex_app_server"])


class TestCodexWizardEndpoints(unittest.TestCase):
    def test_build_codex_wizard_endpoints_sanitizes_ids(self) -> None:
        endpoints = build_codex_wizard_endpoints(("gpt-5.5", "gpt-5.3-codex-spark"))

        self.assertEqual([ep.endpoint_id for ep in endpoints], ["codex_gpt_5_5", "codex_gpt_5_3_codex_spark"])
        self.assertEqual([ep.priority for ep in endpoints], [0, 1])
        self.assertEqual(endpoints[0].provider, "codex_app_server")
        self.assertEqual(endpoints[0].auth_kind, "local_provider_auth")
        self.assertEqual(endpoints[0].credential_ref, "")
        self.assertEqual(endpoints[0].base_url, "codex://app-server")
        self.assertTrue(endpoints[0].capabilities_blob["codex_app_server"])


class TestLLMPreflight(unittest.TestCase):
    def test_preflight_passes_text_and_tool_probe(self) -> None:
        from pal.llm import CanonicalLLMOutcome, CanonicalToolCall

        class FakeInvoker:
            def __init__(self) -> None:
                self.calls = []

            def invoke(self, endpoint, request):
                self.calls.append((endpoint, request))
                if request.tools:
                    return CanonicalLLMOutcome(tool_calls=[CanonicalToolCall(name="pal_preflight_probe", args={"ok": True})])
                return CanonicalLLMOutcome(text="PAL_PREFLIGHT_OK")

        invoker = FakeInvoker()
        result = run_llm_endpoint_preflight(_make_collected().endpoints[0], timeout_seconds=7, invoker=invoker)

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.text_ok)
        self.assertTrue(result.tool_ok)
        self.assertEqual(len(invoker.calls), 2)
        self.assertEqual(invoker.calls[0][0].provider, "anthropic")
        self.assertEqual(invoker.calls[0][1].metadata["timeout_seconds"], 7)
        self.assertEqual(invoker.calls[1][1].tools[0]["function"]["name"], "pal_preflight_probe")

    def test_preflight_errors_when_text_call_fails(self) -> None:
        class FailingInvoker:
            def invoke(self, endpoint, request):
                raise RuntimeError("bad key")

        result = run_llm_endpoint_preflight(_make_collected().endpoints[0], invoker=FailingInvoker())

        self.assertEqual(result.status, "error")
        self.assertIn("bad key", result.detail)

    def test_preflight_warns_when_tool_probe_returns_text(self) -> None:
        from pal.llm import CanonicalLLMOutcome

        class TextOnlyInvoker:
            def invoke(self, endpoint, request):
                return CanonicalLLMOutcome(text="PAL_PREFLIGHT_OK")

        result = run_llm_endpoint_preflight(_make_collected().endpoints[0], invoker=TextOnlyInvoker())

        self.assertEqual(result.status, "warn")
        self.assertTrue(result.text_ok)
        self.assertFalse(result.tool_ok)

    def test_preflight_preserves_codex_endpoint_metadata(self) -> None:
        from pal.llm import CanonicalLLMOutcome

        class FakeInvoker:
            def __init__(self) -> None:
                self.endpoints = []

            def invoke(self, endpoint, request):
                self.endpoints.append(endpoint)
                return CanonicalLLMOutcome(text="PAL_PREFLIGHT_OK")

        invoker = FakeInvoker()
        result = run_llm_endpoint_preflight(build_codex_wizard_endpoints(("gpt-5.5",))[0], invoker=invoker)

        self.assertEqual(result.status, "warn")
        self.assertEqual(invoker.endpoints[0].provider, "codex_app_server")
        self.assertEqual(invoker.endpoints[0].auth_kind, "local_provider_auth")
        self.assertEqual(invoker.endpoints[0].credential_ref, "")
        self.assertTrue(invoker.endpoints[0].capabilities_blob["official_codex_app_server"])


class TestServiceGeneration(unittest.TestCase):
    def test_resolve_pal_command_prefers_invoked_console_script(self) -> None:
        from pal.wizard import cli as cli_mod

        tmp = Path(tempfile.mkdtemp(prefix="pal_bin_test_"))
        pal_script = tmp / "pal"
        pal_script.write_text("#!/bin/sh\n", encoding="utf-8")
        try:
            with patch.object(cli_mod.sys, "argv", [str(pal_script), "setup"]):
                with patch.object(cli_mod.shutil, "which", return_value="/usr/local/bin/pal"):
                    self.assertEqual(cli_mod._resolve_pal_command(), [str(pal_script)])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

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

    def test_generate_service_content_includes_proxy_environment(self) -> None:
        from pal.wizard.cli import _generate_service_content

        content = _generate_service_content(
            pal_bin="/usr/local/bin/pal",
            runtime_root=Path("/home/test/.pal"),
            environment={"PYTHONUNBUFFERED": "1", "https_proxy": "http://127.0.0.1:8118"},
        )

        self.assertIn('Environment="PYTHONUNBUFFERED=1"', content)
        self.assertIn('Environment="https_proxy=http://127.0.0.1:8118"', content)

    def test_runtime_service_environment_carries_proxy_vars(self) -> None:
        from pal.wizard.cli import _runtime_service_environment

        with patch.dict(os.environ, {"https_proxy": "http://127.0.0.1:8118", "NO_PROXY": "localhost"}, clear=True):
            environment = _runtime_service_environment()

        self.assertEqual(environment["PYTHONUNBUFFERED"], "1")
        self.assertEqual(environment["https_proxy"], "http://127.0.0.1:8118")
        self.assertEqual(environment["NO_PROXY"], "localhost")

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

    def test_generate_launchd_plist(self) -> None:
        from pal.wizard.cli import _generate_launchd_plist

        payload = _generate_launchd_plist(
            label="com.pal.test",
            pal_command=["/usr/local/bin/pal"],
            runtime_root=Path("/Users/test/.pal"),
        )

        self.assertEqual(payload["Label"], "com.pal.test")
        self.assertEqual(
            payload["ProgramArguments"],
            ["/usr/local/bin/pal", "run", "--runtime-root", "/Users/test/.pal"],
        )
        self.assertEqual(payload["RunAtLoad"], True)
        self.assertEqual(payload["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(payload["WorkingDirectory"], "/Users/test/.pal")
        self.assertEqual(payload["EnvironmentVariables"], {"PYTHONUNBUFFERED": "1"})
        self.assertEqual(payload["StandardOutPath"], "/Users/test/.pal/pal.log")
        self.assertEqual(payload["StandardErrorPath"], "/Users/test/.pal/pal.log")

    def test_generate_launchd_plist_includes_proxy_environment(self) -> None:
        from pal.wizard.cli import _generate_launchd_plist

        payload = _generate_launchd_plist(
            label="com.pal.test",
            pal_command=["/usr/local/bin/pal"],
            runtime_root=Path("/Users/test/.pal"),
            environment={"PYTHONUNBUFFERED": "1", "HTTPS_PROXY": "http://127.0.0.1:8118"},
        )

        self.assertEqual(
            payload["EnvironmentVariables"],
            {"PYTHONUNBUFFERED": "1", "HTTPS_PROXY": "http://127.0.0.1:8118"},
        )

    def test_pick_launchd_label_no_conflict(self) -> None:
        from pal.wizard import cli as cli_mod

        tmp = Path(tempfile.mkdtemp(prefix="pal_launchd_test_"))
        original = cli_mod._LAUNCHD_USER_DIR
        cli_mod._LAUNCHD_USER_DIR = tmp
        try:
            result = cli_mod._pick_launchd_label(Path("/Users/test/.pal"))
            self.assertEqual(result, "com.pal.runtime")
        finally:
            cli_mod._LAUNCHD_USER_DIR = original
            shutil.rmtree(tmp, ignore_errors=True)

    def test_pick_launchd_label_same_runtime_reuses(self) -> None:
        from pal.wizard import cli as cli_mod

        tmp = Path(tempfile.mkdtemp(prefix="pal_launchd_test_"))
        original = cli_mod._LAUNCHD_USER_DIR
        cli_mod._LAUNCHD_USER_DIR = tmp
        try:
            (tmp / "com.pal.runtime.plist").write_text(
                "<string>/Users/test/.pal</string>", encoding="utf-8"
            )
            result = cli_mod._pick_launchd_label(Path("/Users/test/.pal"))
            self.assertEqual(result, "com.pal.runtime")
        finally:
            cli_mod._LAUNCHD_USER_DIR = original
            shutil.rmtree(tmp, ignore_errors=True)

    def test_pick_launchd_label_conflict_uses_runtime_name(self) -> None:
        from pal.wizard import cli as cli_mod

        tmp = Path(tempfile.mkdtemp(prefix="pal_launchd_test_"))
        original = cli_mod._LAUNCHD_USER_DIR
        cli_mod._LAUNCHD_USER_DIR = tmp
        try:
            (tmp / "com.pal.runtime.plist").write_text(
                "<string>/Users/other/.pal</string>", encoding="utf-8"
            )
            result = cli_mod._pick_launchd_label(Path("/Users/test/work-pal"))
            self.assertEqual(result, "com.pal.work-pal")
        finally:
            cli_mod._LAUNCHD_USER_DIR = original
            shutil.rmtree(tmp, ignore_errors=True)

    def test_register_and_start_launchd_runs_launchctl_sequence(self) -> None:
        from pal.wizard import cli as cli_mod

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            calls.append(cmd)
            return object()

        with patch.object(cli_mod, "_launchd_domain", return_value="gui/501"):
            with patch("subprocess.run", side_effect=fake_run):
                ok = cli_mod._register_and_start_launchd(
                    "com.pal.test",
                    Path("/Users/test/Library/LaunchAgents/com.pal.test.plist"),
                )

        self.assertTrue(ok)
        self.assertEqual(
            calls,
            [
                ["launchctl", "bootout", "gui/501/com.pal.test"],
                [
                    "launchctl",
                    "bootstrap",
                    "gui/501",
                    "/Users/test/Library/LaunchAgents/com.pal.test.plist",
                ],
                ["launchctl", "kickstart", "-k", "gui/501/com.pal.test"],
            ],
        )


class TestDependencyDoctor(unittest.TestCase):
    def test_dependency_check_blocking_only_for_required_missing_or_error(self) -> None:
        from pal.wizard.dependencies import WizardDependencyCheck

        self.assertTrue(WizardDependencyCheck("required", "Required", "missing", "missing", required=True).blocking)
        self.assertTrue(WizardDependencyCheck("required", "Required", "error", "error", required=True).blocking)
        self.assertFalse(WizardDependencyCheck("optional", "Optional", "warn", "warn", required=False).blocking)
        self.assertFalse(WizardDependencyCheck("ok", "OK", "ok", "ok", required=True).blocking)

    def test_dependency_report_counts_blocking_and_warnings(self) -> None:
        from pal.wizard import dependencies as dep_mod
        from pal.wizard.dependencies import WizardDependencyCheck

        checks = (
            WizardDependencyCheck("python", "Python", "ok", "ok"),
            WizardDependencyCheck("package", "Package", "missing", "missing", required=True),
            WizardDependencyCheck("ollama", "Ollama", "warn", "warn", required=False),
        )
        with patch.object(dep_mod, "collect_dependency_checks", return_value=checks):
            report = dep_mod.dependency_report()

        self.assertFalse(report["ok"])
        self.assertEqual(report["blocking_count"], 1)
        self.assertEqual(report["warning_count"], 1)

    def test_dependency_doctor_exit_code_reflects_blocking_checks(self) -> None:
        from pal.wizard import cli as cli_mod
        from pal.wizard.dependencies import WizardDependencyCheck

        checks = (
            WizardDependencyCheck("ok", "OK", "ok", "ok"),
            WizardDependencyCheck("missing", "Missing", "missing", "missing", required=True, fix="install"),
        )
        with patch.object(cli_mod, "collect_dependency_checks", return_value=checks):
            with patch("sys.stdout", new=StringIO()):
                exit_code = cli_mod.run_dependency_doctor()

        self.assertEqual(exit_code, 2)

    def test_service_manager_check_supports_launchd(self) -> None:
        from pal.wizard import dependencies as dep_mod

        with patch.object(dep_mod.platform, "system", return_value="Darwin"):
            with patch.object(dep_mod.shutil, "which", return_value="/bin/launchctl"):
                check = dep_mod._check_service_manager()

        self.assertEqual(check.status, "ok")
        self.assertIn("LaunchAgent", check.detail)

    def test_service_manager_check_warns_when_launchctl_missing(self) -> None:
        from pal.wizard import dependencies as dep_mod

        with patch.object(dep_mod.platform, "system", return_value="Darwin"):
            with patch.object(dep_mod.shutil, "which", return_value=None):
                check = dep_mod._check_service_manager()

        self.assertEqual(check.status, "warn")
        self.assertFalse(check.required)

    def test_codex_cli_check_is_optional(self) -> None:
        from pal.wizard import dependencies as dep_mod

        with patch.object(dep_mod.shutil, "which", return_value=None):
            check = dep_mod._check_codex_cli()

        self.assertEqual(check.status, "warn")
        self.assertFalse(check.required)

    def test_jieba_package_check_is_required(self) -> None:
        from pal.wizard import dependencies as dep_mod

        with patch.object(dep_mod.importlib.util, "find_spec", return_value=None):
            check = dep_mod._check_python_package("jieba", "jieba", "Chinese FTS tokenization")

        self.assertEqual(check.check_id, "python.package.jieba")
        self.assertEqual(check.status, "missing")
        self.assertTrue(check.required)


if __name__ == "__main__":
    unittest.main()
