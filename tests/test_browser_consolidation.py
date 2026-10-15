import json
from unittest.mock import Mock

import pytest

from pal.shared import IntrospectionCall
from pal.web_fetch.browser_service import BrowserServiceManager, _PlaywrightCliWorker, _SessionRecord
from pal.web_fetch.capabilities import WebFetchIntrospectionProvider


def configured_extensions(paths, key):
    items = [{"id": ch * 32, "name": ch, "version": "1", "path": "/extension/" + ch,
              "permissions": ["storage"], "manifest_url": f"chrome-extension://{ch * 32}/manifest.json"}
             for ch in ("a", "b")]
    directory = paths.profiles / key
    directory.mkdir(parents=True)
    (directory / "extensions.json").write_text(json.dumps(items))
    return items


def test_passive_inspection_reads_config_without_starting_sidecar(tmp_path):
    from pal.web_fetch.runtime_paths import BrowserRuntimePaths
    manager = BrowserServiceManager(runtime_root=tmp_path)
    manager._ensure_started = Mock(side_effect=AssertionError("inspection started sidecar"))
    key = "c" * 64
    items = configured_extensions(BrowserRuntimePaths(tmp_path), key)
    summary = manager.execute(session_key=key, action="inspect", persistent=True)
    assert len(summary["extensions"]) == 2
    assert all(set(item) == {"id", "name", "version"} for item in summary["extensions"])
    detail = manager.execute(session_key=key, action="inspect", args={"extension_id": items[0]["id"]})
    assert detail["extension"] == items[0]
    assert "extensions" not in detail
    assert detail["workers_observed"] is False
    assert detail["session"]["running"] is None
    manager._ensure_started.assert_not_called()


def test_selected_extension_observes_only_its_workers_without_installing(tmp_path):
    worker = _PlaywrightCliWorker(runtime_root=tmp_path, max_concurrency=1)
    key = "c" * 64
    items = configured_extensions(worker.paths, key)
    worker.sessions[key] = _SessionRecord(key=key, name="existing", persistent=True)
    worker._cli_ready = Mock(side_effect=AssertionError("inspection provisioned browser"))
    worker._run = Mock(return_value=json.dumps({"workers": [
        f"chrome-extension://{item['id']}/worker.js" for item in items]}))
    result = worker.execute(session_key=key, action="inspect", args={"extension_id": items[0]["id"]},
                            persistent=True, timeout_ms=1000)
    assert result["extension"] == items[0]
    assert result["observed_service_workers"] == [f"chrome-extension://{items[0]['id']}/worker.js"]
    assert result["workers_observed"] is True
    worker._cli_ready.assert_not_called()


@pytest.mark.parametrize("operation", ["close", "clear_cache", "reset"])
def test_maintenance_dispatch_preserves_reset_confirmation(operation):
    provider = WebFetchIntrospectionProvider(service=Mock())
    provider._action = Mock(return_value="completed")
    args = {"operation": operation}
    if operation == "reset":
        from pal.execution.tool_facade import ToolRejectedError
        with pytest.raises(ToolRejectedError):
            provider.manage_session(IntrospectionCall(name="manage_browser_session", args=args))
        provider._action.assert_not_called()
        args["confirm"] = True
    provider.manage_session(IntrospectionCall(name="manage_browser_session", args=args))
    assert provider._action.call_args.args[1] == operation
