import asyncio
from unittest.mock import AsyncMock

from pal.lsp.config import LspServerConfig, LspServerFileConfig
from pal.lsp.connector import AsyncLspConnector
from pal.lsp.manager import LspManager, LspServerState
from pal.lsp.plugin import _capability_from_rpc


def test_old_diagnostics_do_not_replace_current_version_or_wake_waiters(tmp_path):
    async def run():
        sample = tmp_path / "sample.py"
        sample.write_text("value = 1")
        config = LspServerConfig(server_id="fake", command=("unused",), diagnostics_timeout_ms=1)
        connector = AsyncLspConnector(config, tmp_path)
        connector.notify = AsyncMock()
        await connector.ensure_document_open(sample, language_id="python")
        sample.write_text("value = 2")
        await connector.ensure_document_open(sample, language_id="python")
        uri = sample.as_uri()
        def notify(version, message):
            params = {"uri": uri, "diagnostics": [{"message": message}]}
            if version is not None:
                params["version"] = version
            connector._handle_notification({"method": "textDocument/publishDiagnostics", "params": params})
        notify(1, "old error")
        assert uri not in connector._diagnostics
        assert not connector._diagnostic_events[uri].is_set()
        pending = await connector.diagnostics(sample, language_id="python")
        manager = LspManager(tmp_path)
        state = LspServerState(LspServerFileConfig(config), tmp_path / "config")
        def project(result):
            return manager._evidence("diagnostics", state, tmp_path, sample, {}, result, file_sha256="current")
        payload = project(pending)
        assert payload["status"] == "unavailable"
        assert payload["evidence"]["freshness"] == "pending"
        tool_result = _capability_from_rpc("LSP diagnostics", payload)
        assert tool_result.status != "ok"
        assert "empty list is not a clean check" in tool_result.recovery_hint
        notify(2, "current error")
        notify(1, "late old error")
        current = await connector.diagnostics(sample, language_id="python")
        assert current["diagnostics"] == [{"message": "current error"}]
        assert project(current)["evidence"]["freshness"] == "fresh"
        notify(None, "unversioned observation")
        unknown = project(await connector.diagnostics(sample, language_id="python"))
        assert unknown["evidence"]["freshness"] == "unknown"
        assert "not proof" in unknown["next_step"]
    asyncio.run(run())


def test_document_version_is_set_before_change_notification(tmp_path):
    async def run():
        sample = tmp_path / "sample.py"
        sample.write_text("value = 1")
        connector = AsyncLspConnector(LspServerConfig(server_id="fake", command=("unused",)), tmp_path)
        async def notify(method, params):
            doc = params["textDocument"]
            connector._handle_notification({"method": "textDocument/publishDiagnostics",
                "params": {"uri": doc["uri"], "version": doc["version"], "diagnostics": []}})
        connector.notify = notify
        await connector.ensure_document_open(sample, language_id="python")
        sample.write_text("value = 2")
        await connector.ensure_document_open(sample, language_id="python")
        result = await connector.diagnostics(sample, language_id="python")
        assert result["document_version"] == result["diagnostic_version"] == 2
        assert result["diagnostics_state"] == "fresh"
    asyncio.run(run())
