from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pal.core import PalCore
from pal.execution import register_with_core as register_execution_with_core
from pal.execution.tool_semantics import EffectKind, InvocationMode, RetryPolicy
from pal.web_fetch import BrowserServiceError, WebFetchService, browser_session_key, register_with_core
from pal.web_fetch.browser_service import (
    PLAYWRIGHT_CLI_VERSION,
    PROFILE_MAX_BYTES,
    PROFILE_RETENTION_SECONDS,
    _PlaywrightCliWorker,
    _cli_args,
)
from pal.web_fetch.schema import migrate_web_fetch_schema


class _FakeManager:
    runtime_root = Path("/tmp/pal-browser-test")

    def execute(self, **kwargs):
        return {"action": kwargs["action"]}

    def health(self):
        return {"healthy": True, "service_running": False, "reason": "idle"}

    def stop_sync(self) -> None:
        return None

    async def shutdown_async(self) -> None:
        return None


def test_public_browser_surface_is_single_backend_and_discovery_first() -> None:
    core = PalCore()
    register_execution_with_core(core.context)
    core.publish_module_capabilities("execution")
    register_with_core(core.context, WebFetchService(browser_manager=_FakeManager()))  # type: ignore[arg-type]
    core.publish_module_capabilities("web_fetch")

    direct = {item["function"]["name"] for item in core._build_llm_tool_contracts()}
    assert {"browser_navigate", "browser_read", "browser_snapshot", "browser_find"} <= direct
    assert "browser_click" not in direct
    assert "browser_screenshot" not in direct

    descriptors = core.context.capability_registry.descriptors
    assert all(name in descriptors for name in ("browser_click", "browser_screenshot", "browser_reset"))
    assert descriptors["browser_read"].InputModel.__module__ == "pal.web_fetch.tool_models"
    assert not any(name.startswith("web_fetch_provider_") for name in descriptors)
    assert not {"read_web", "inspect_web_layout", "screenshot_web"} & set(descriptors)
    assert descriptors["browser_click"].execution.invocation_mode == InvocationMode.INDIRECT
    assert descriptors["browser_click"].execution.effect_kind == EffectKind.EXTERNAL_WRITE
    assert descriptors["browser_click"].execution.retry_policy == RetryPolicy.RECONCILE_FIRST
    assert descriptors["browser_screenshot"].execution.effect_kind == EffectKind.LOCAL_WRITE


def test_session_keys_are_stable_non_reversible_and_require_a_lifetime() -> None:
    assert browser_session_key("conversation-1") == browser_session_key("conversation-1")
    assert browser_session_key("conversation-1") != browser_session_key("conversation-2")
    assert len(browser_session_key("conversation-1")) == 64
    with pytest.raises(ValueError, match="execution lifetime"):
        browser_session_key("")


def test_user_positionals_are_separated_from_cli_options() -> None:
    assert _cli_args("fill", "e7", "--submit", options=["--submit"]) == [
        "fill",
        "--submit",
        "--",
        "e7",
        "--submit",
    ]


def test_missing_cli_reports_installing_without_http_fallback(tmp_path: Path) -> None:
    worker = _PlaywrightCliWorker(runtime_root=tmp_path, max_concurrency=1)
    scheduled = []
    worker._node_major_cached = None
    worker._cli_version_cached = ""
    worker._schedule_install = lambda **kwargs: scheduled.append(kwargs)  # type: ignore[method-assign]

    with pytest.raises(BrowserServiceError) as captured:
        worker.execute(
            session_key="a" * 64,
            action="read",
            args={"url": "https://example.com"},
            persistent=True,
            timeout_ms=1000,
        )

    assert captured.value.code == "dependency_installing"
    assert captured.value.curl_applicable is True
    assert scheduled == [{"reason": "CLI missing or wrong version"}]
    assert not hasattr(worker, "providers")


def test_navigation_failures_offer_main_pal_curl_but_invalid_urls_do_not(
    tmp_path: Path,
) -> None:
    worker = _PlaywrightCliWorker(runtime_root=tmp_path, max_concurrency=1)
    worker.paths.cli.parent.mkdir(parents=True, exist_ok=True)
    worker.paths.cli.touch()
    worker._node_major_cached = 24
    worker._cli_version_cached = PLAYWRIGHT_CLI_VERSION

    worker._execute_ready = lambda **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        BrowserServiceError("navigation failed", code="cli_command_failed")
    )
    with pytest.raises(BrowserServiceError) as captured:
        worker.execute(
            session_key="b" * 64,
            action="navigate",
            args={"url": "https://example.com"},
            persistent=True,
            timeout_ms=1000,
        )
    assert captured.value.curl_applicable is True

    worker._execute_ready = (  # type: ignore[method-assign]
        lambda **kwargs: (_ for _ in ()).throw(ValueError("bad URL"))
    )
    with pytest.raises(BrowserServiceError) as captured:
        worker.execute(
            session_key="b" * 64,
            action="navigate",
            args={"url": "not-a-url"},
            persistent=True,
            timeout_ms=1000,
        )
    assert captured.value.code == "invalid_arguments"
    assert captured.value.curl_applicable is False


def test_worker_config_is_pinned_bounded_and_has_no_recording_surface(tmp_path: Path) -> None:
    with patch.dict(
        "os.environ",
        {
            "https_proxy": "",
            "http_proxy": "",
            "HTTPS_PROXY": "http://proxy.test:8080",
            "NO_PROXY": "localhost,.cn",
        },
        clear=False,
    ):
        worker = _PlaywrightCliWorker(runtime_root=tmp_path, max_concurrency=1)

    config = json.loads(worker.paths.config.read_text(encoding="utf-8"))
    assert PLAYWRIGHT_CLI_VERSION == "0.1.19"
    assert config["browser"]["launchOptions"]["proxy"] == {
        "server": "http://proxy.test:8080",
        "bypass": "localhost,.cn",
    }
    assert config["allowUnrestrictedFileAccess"] is False
    assert config["codegen"] == "none"
    assert "saveTrace" not in config
    assert "saveVideo" not in config
    assert "outputMaxSize" not in config


def test_profile_pruning_removes_expired_and_lru_inactive_only(tmp_path: Path) -> None:
    worker = _PlaywrightCliWorker(runtime_root=tmp_path, max_concurrency=1)
    old_key, recent_key, active_key = "1" * 64, "2" * 64, "3" * 64
    now = time.time()
    for key, last_used in (
        (old_key, now - PROFILE_RETENTION_SECONDS - 10),
        (recent_key, now - 5),
        (active_key, now - PROFILE_RETENTION_SECONDS - 10),
    ):
        profile = worker.paths.profiles / key
        profile.mkdir(parents=True)
        (profile / "payload").write_bytes(b"x" * 16)
        (profile / "session.json").write_text(
            json.dumps({"last_used_at": last_used}), encoding="utf-8"
        )

    worker._prune_profiles(exclude={active_key})

    assert not (worker.paths.profiles / old_key).exists()
    assert (worker.paths.profiles / recent_key).exists()
    assert (worker.paths.profiles / active_key).exists()
    assert PROFILE_MAX_BYTES == 2 * 1024 * 1024 * 1024


def test_profile_pruning_enforces_lru_byte_cap(tmp_path: Path) -> None:
    worker = _PlaywrightCliWorker(runtime_root=tmp_path, max_concurrency=1)
    oldest_key, newest_key = "4" * 64, "5" * 64
    now = time.time()
    for key, last_used in ((oldest_key, now - 20), (newest_key, now - 10)):
        profile = worker.paths.profiles / key
        profile.mkdir(parents=True)
        (profile / "payload").write_bytes(b"x" * 64)
        (profile / "session.json").write_text(
            json.dumps({"last_used_at": last_used}), encoding="utf-8"
        )

    oldest = worker.paths.profiles / oldest_key
    total = sum(
        item.stat().st_size
        for item in worker.paths.profiles.rglob("*")
        if item.is_file()
    )
    oldest_size = sum(
        item.stat().st_size for item in oldest.rglob("*") if item.is_file()
    )
    with patch(
        "pal.web_fetch.browser_service.PROFILE_MAX_BYTES",
        total - oldest_size,
    ):
        worker._prune_profiles()

    assert not oldest.exists()
    assert (worker.paths.profiles / newest_key).exists()


def test_legacy_provider_schema_is_archived_then_removed(tmp_path: Path) -> None:
    db_path = tmp_path / "pal.sqlite3"
    database = sqlite3.connect(db_path)
    database.executescript(
        """
        CREATE TABLE web_fetch_providers (
            provider_id TEXT PRIMARY KEY,
            provider_kind TEXT,
            settings_blob TEXT
        );
        CREATE TABLE pal_runtime_settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT
        );
        INSERT INTO web_fetch_providers VALUES
            ('playwright_fetch_default', 'playwright_fetch', '{}'),
            ('custom_fetch', 'custom', '{"mode":"custom"}');
        INSERT INTO pal_runtime_settings VALUES
            ('active_web_fetch_provider_id', 'custom_fetch'),
            ('active_web_search_provider_id', 'brave_search_default');
        """
    )
    database.commit()
    database.close()

    result = migrate_web_fetch_schema(db_path)

    assert result.status == "migrated"
    assert result.removed_rows == 2
    assert result.archived_rows == 1
    archive = Path(result.archive_path)
    assert archive.stat().st_mode & 0o777 == 0o600
    assert json.loads(archive.read_text(encoding="utf-8"))["providers"][0]["provider_id"] == "custom_fetch"
    database = sqlite3.connect(db_path)
    assert database.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_fetch_providers'"
    ).fetchone() is None
    assert database.execute(
        "SELECT setting_value FROM pal_runtime_settings WHERE setting_key='active_web_fetch_provider_id'"
    ).fetchone() is None
    assert database.execute(
        "SELECT setting_value FROM pal_runtime_settings WHERE setting_key='active_web_search_provider_id'"
    ).fetchone()[0] == "brave_search_default"
    database.close()
