"""Search and browser failures survive provider, process and tool boundaries."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.error import HTTPError

import pytest

from pal.core import PalCore
from pal.execution import register_with_core as register_execution
from pal.foundation.diagnostics import exception_report
from pal.shared.tool_protocol import new_tool_call
from pal.web_fetch import browser_service as browser
from pal.web_fetch import provisioning
from pal.web_fetch.browser_service import BrowserServiceError, BrowserServiceManager, _PlaywrightCliWorker, _SessionRecord
from pal.web_fetch.capabilities import register_with_core as register_browser
from pal.web_fetch.service import WebFetchService
from pal.web_search import service as search_module
from pal.web_search.capabilities import register_with_core as register_search
from pal.web_search.contracts import WebSearchQuery
from pal.web_search.service import WebSearchService, BraveSearchProvider, DuckDuckGoSearchProvider
from tests.test_tool_failure_affordances import metadata


def failure(message="upstream failed"):
    try:
        raise OSError("backend root cause; api_key=hidden-secret")
    except OSError as cause:
        try:
            raise RuntimeError(message) from cause
        except RuntimeError as exc:
            return exc


def assert_diagnostic(text):
    assert "backend root cause" in text
    assert "hidden-secret" not in text


def search_service(*providers):
    records = [SimpleNamespace(provider_id=f"provider-{index}", provider_kind=f"kind-{index}",
        enabled=True, auth_material_blob={}) for index in range(len(providers))]
    return WebSearchService(SimpleNamespace(list_enabled=lambda: records, list_all=lambda: records),
        SimpleNamespace(get=lambda key: "provider-0"),
        {record.provider_kind: provider for record, provider in zip(records, providers)})


@pytest.fixture
def core(tmp_path):
    core = PalCore()
    register_execution(core.context)
    core.publish_module_capabilities("execution")
    core.context.execution_runtime.configure_runtime_root(tmp_path)
    yield core
    core.context.execution_runtime.shutdown()


@pytest.mark.parametrize("fallback", [False, True])
def test_search_tool_preserves_all_failures_even_when_fallback_succeeds(core, fallback):
    first = SimpleNamespace(search=MagicMock(side_effect=failure("first provider failed")))
    second = SimpleNamespace(search=MagicMock(return_value=[]) if fallback else MagicMock(side_effect=failure("second provider failed")))
    service = search_service(first, second)
    register_search(core.context, service)
    core.publish_module_capabilities("web_search")
    result = core.context.execution_runtime.execute_tool(new_tool_call(name="search_web", args={"query": "anything"}))
    assert result.ok is fallback
    assert_diagnostic(result.llm_text)
    assert "first provider failed" in result.llm_text
    if fallback:
        assert result.structured["items"] == []
        assert result.structured["fallback_used"] is True
        assert result.structured["provider_errors"][0]["provider_id"] == "provider-0"
    else:
        assert "second provider failed" in result.llm_text
        assert metadata(result)["error_code"] == "web_search_failed"
        assert metadata(result)["effect"] == "none"


@pytest.mark.parametrize("payload", [b"not JSON but a useful upstream error", b"[1,2]", b'{"error":{"reason":"quota exhausted"}}'])
def test_invalid_search_response_is_not_empty_success(monkeypatch, payload):
    monkeypatch.setattr(search_module, "urlopen", lambda *args, **kw: io.BytesIO(payload))
    with pytest.raises(RuntimeError) as caught:
        search_module._http_json("https://unused.test")
    assert payload.decode() in str(caught.value)


def test_search_http_error_keeps_complete_response_body(monkeypatch):
    body = "first cause " + "x" * 5000 + " last cause; token=hidden-secret"
    def request(*args, **kwargs):
        raise HTTPError("https://unused.test", 429, "rate limited", {}, io.BytesIO(body.encode()))
    monkeypatch.setattr(search_module, "urlopen", request)
    with pytest.raises(RuntimeError) as caught:
        search_module._http_json("https://unused.test")
    text = exception_report(caught.value)
    assert "429" in text and "first cause" in text and "last cause" in text
    assert "hidden-secret" not in text


@pytest.mark.parametrize("provider,payload", [
    (BraveSearchProvider(), {"web": {"results": [{"description": "malformed real item"}]}}),
    (DuckDuckGoSearchProvider(), {"RelatedTopics": [{"Text": "malformed real item"}]}),
])
def test_malformed_search_items_are_reported(monkeypatch, provider, payload):
    monkeypatch.setattr(search_module, "_http_json", lambda *args, **kw: payload)
    record = SimpleNamespace(provider_id="p", provider_kind=provider.provider_kind, auth_material_blob={"api_key": "test-key"})
    with pytest.raises(RuntimeError, match="malformed real item"):
        provider.search(record, WebSearchQuery("q"))


@pytest.mark.parametrize("provider,payload", [
    (BraveSearchProvider(), {"web": {"results": []}}),
    (DuckDuckGoSearchProvider(), {"RelatedTopics": []}),
])
def test_genuine_empty_search_results_remain_successful(monkeypatch, provider, payload):
    monkeypatch.setattr(search_module, "_http_json", lambda *args, **kw: payload)
    record = SimpleNamespace(provider_id="p", provider_kind=provider.provider_kind, auth_material_blob={"api_key": "test-key"})
    assert provider.search(record, WebSearchQuery("q")) == []


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "_detect_node_major", lambda *args: 24)
    worker = _PlaywrightCliWorker(runtime_root=tmp_path, max_concurrency=1)
    worker.paths.cli.parent.mkdir(parents=True, exist_ok=True)
    worker.paths.cli.touch()
    worker._cli_version_cached = browser.PLAYWRIGHT_CLI_VERSION
    return worker


def record(worker):
    entry = _SessionRecord("a" * 64, "test-browser", True, "https://example.test")
    worker.sessions[entry.key] = entry
    return entry


@pytest.mark.parametrize("timeout", [False, True])
def test_cli_failure_retains_both_streams_and_timeout_output(worker, monkeypatch, timeout):
    stdout = "output beginning " + "x" * 5000 + " output ending"
    stderr = "error beginning " + "y" * 5000 + " error ending; api_key=hidden-secret"
    if timeout:
        command = MagicMock(side_effect=subprocess.TimeoutExpired("test", 1, output=stdout.encode(), stderr=stderr.encode()))
    else:
        command = MagicMock(return_value=SimpleNamespace(returncode=3, stdout=stdout, stderr=stderr))
    monkeypatch.setattr(browser.subprocess, "run", command)
    with pytest.raises(BrowserServiceError) as caught:
        worker._run_write(record(worker), ["click", "target"], timeout_ms=1000)
    payload = caught.value.to_dict()
    text = json.dumps(payload)
    for marker in ("output beginning", "output ending", "error beginning", "error ending"):
        assert marker in text
    assert "hidden-secret" not in text
    assert payload["state_unknown"] is True


def test_cli_spawn_failure_is_not_marked_as_started(worker, monkeypatch):
    monkeypatch.setattr(browser.subprocess, "run", MagicMock(side_effect=FileNotFoundError("CLI vanished")))
    with pytest.raises(BrowserServiceError) as caught:
        worker._run_write(record(worker), ["click", "target"], timeout_ms=1000)
    assert caught.value.code == "cli_unavailable"
    assert not caught.value.state_unknown


def test_missing_page_text_is_not_reported_as_an_empty_page(worker, monkeypatch):
    monkeypatch.setattr(worker, "_run", lambda *args, **kwargs: '{"error":"document capture failed"}')
    with pytest.raises(BrowserServiceError) as caught:
        worker._read_document(record(worker), args={}, timeout_ms=1000)
    assert "document capture failed" in json.dumps(caught.value.to_dict())


def test_closed_session_is_not_removed_when_cli_close_fails(worker, monkeypatch):
    entry = record(worker)
    monkeypatch.setattr(worker, "_run", MagicMock(side_effect=BrowserServiceError("close failed", code="cli_command_failed")))
    with pytest.raises(BrowserServiceError):
        worker._close(entry.key)
    assert worker.sessions[entry.key] is entry


def test_failed_profile_reset_reports_completed_close_and_uncertain_delete(worker, monkeypatch):
    entry = record(worker)
    path = worker.paths.profiles / entry.key
    path.mkdir()
    (path / "cookies").write_text("still present")
    monkeypatch.setattr(worker, "_run", lambda *a, **k: "")
    monkeypatch.setattr(browser.shutil, "rmtree", MagicMock(side_effect=failure("delete failed")))
    with pytest.raises(BrowserServiceError) as caught:
        worker._reset(entry.key)
    assert entry.key not in worker.sessions
    assert caught.value.details["close_result"]["closed"] is True
    assert caught.value.state_unknown
    assert (path / "cookies").exists()
    assert_diagnostic(json.dumps(caught.value.to_dict()))


def test_successful_action_reports_page_and_profile_followup_errors(worker, monkeypatch):
    entry = record(worker)
    monkeypatch.setattr(worker, "_dispatch_action", lambda *a, **k: {"clicked": True})
    def page(*a, **k):
        raise BrowserServiceError("page inspection failed") from failure()
    monkeypatch.setattr(worker, "_page_state", page)
    monkeypatch.setattr(worker, "_write_profile_meta", MagicMock(side_effect=failure("metadata commit failed")))
    result = worker.execute(session_key=entry.key, action="click", args={"target": "e1"}, persistent=True, timeout_ms=1000)
    assert result["clicked"] is True and result["action_completed"] is True
    assert_diagnostic(json.dumps(result))
    assert "page inspection failed" in json.dumps(result) and "metadata commit failed" in json.dumps(result)


def test_corrupt_profile_metadata_is_not_silently_discarded(worker):
    entry = record(worker)
    path = worker.paths.profiles / entry.key
    path.mkdir()
    (path / "session.json").write_text("broken JSON")
    with pytest.raises(ValueError):
        worker._read_profile_meta(entry.key)


def test_install_failure_is_complete_and_not_relabelled_installing(worker, monkeypatch):
    monkeypatch.setattr("pal.packages.service.PackageService.prepare", MagicMock(side_effect=failure("package install failed")))
    worker._install_dependencies(browser_only=False)
    assert_diagnostic(worker._install_state["error"])
    with pytest.raises(BrowserServiceError) as caught:
        worker._schedule_install(reason="missing")
    assert caught.value.code == "dependency_install_failed"
    assert not caught.value.retryable
    assert_diagnostic(json.dumps(caught.value.to_dict()))


def test_dependency_repair_diagnostics_survive_successful_install(worker, monkeypatch):
    result = {"prepare_result": {"ok": True, "preparation_diagnostics": ["initial verification failed; repaired"]}}
    monkeypatch.setattr("pal.packages.service.PackageService.prepare", lambda *a, **k: result)
    monkeypatch.setattr(worker, "_node_major", lambda: 24)
    monkeypatch.setattr(worker, "_detected_cli_version", lambda: browser.PLAYWRIGHT_CLI_VERSION)
    worker._install_dependencies(browser_only=False)
    assert worker.health()["self_heal"]["preparation_result"] == result


@pytest.mark.parametrize("code,retry", [("dependency_install_failed", "do_not_retry"), ("cli_unavailable", "safe")])
def test_pre_dispatch_browser_errors_have_truthful_model_metadata(core, code, retry):
    manager = SimpleNamespace(execute=MagicMock(side_effect=BrowserServiceError(
        "request not dispatched", code=code, retryable=code == "cli_unavailable", diagnostic=exception_report(failure()))))
    register_browser(core.context, WebFetchService(manager))
    core.publish_module_capabilities("web_fetch")
    result = core.context.execution_runtime.execute_tool(new_tool_call(name="navigate_browser", args={"url": "https://unused.test"}), turn_id="web-audit")
    assert not result.ok
    assert metadata(result)["effect"] == "not_started"
    assert metadata(result)["retry"] == retry
    assert_diagnostic(result.llm_text)


@pytest.mark.parametrize("capture_fails", [False, True])
def test_screenshot_cleanup_preserves_capture_outcome(worker, monkeypatch, capture_fails):
    entry = record(worker)
    def capture(record, command, **kwargs):
        if capture_fails:
            raise BrowserServiceError("capture failed") from failure()
        path = next(value.removeprefix("--filename=") for value in command if value.startswith("--filename="))
        Path(path).write_bytes(b"screenshot bytes")
        return ""
    monkeypatch.setattr(worker, "_run", capture)
    monkeypatch.setattr(Path, "unlink", MagicMock(side_effect=failure("screenshot unlink failed")))
    if capture_fails:
        with pytest.raises(BrowserServiceError) as caught:
            worker._dispatch_action(entry, action="screenshot", args={}, timeout_ms=1000)
        diagnostic = exception_report(caught.value)
        assert "capture failed" in diagnostic and "screenshot unlink failed" in diagnostic
    else:
        result = worker._dispatch_action(entry, action="screenshot", args={}, timeout_ms=1000)
        assert result["png_base64"]
        diagnostic = result["screenshot_cleanup_error"]
    assert_diagnostic(diagnostic)


def test_shutdown_without_cli_does_not_forget_live_sessions(worker):
    entry = record(worker)
    worker.paths.cli.unlink()
    with pytest.raises(BrowserServiceError) as caught:
        worker.shutdown()
    assert caught.value.code == "browser_shutdown_failed"
    assert caught.value.state_unknown
    assert worker.sessions[entry.key] is entry


def test_profile_size_does_not_suppress_scan_failure(tmp_path, monkeypatch):
    def walk(path, *, onerror):
        onerror(PermissionError("profile directory unreadable"))
        return []
    monkeypatch.setattr(browser.os, "walk", walk)
    with pytest.raises(PermissionError, match="profile directory unreadable"):
        browser._tree_size(tmp_path)


def test_cli_install_and_rollback_failures_are_both_preserved(tmp_path, monkeypatch):
    paths = browser.BrowserRuntimePaths(tmp_path)
    paths.prepare()
    paths.tooling_current.mkdir()
    monkeypatch.setattr(provisioning, "_installed_cli_version", lambda paths: "")
    monkeypatch.setattr(provisioning.shutil, "which", lambda *a, **k: "npm")
    def install(command, **kwargs):
        candidate = Path(command[command.index("--prefix") + 1])
        metadata = candidate / "node_modules/playwright-core/package.json"
        metadata.parent.mkdir(parents=True)
        metadata.write_text('{"engines":{"node":">=20"}}')
    monkeypatch.setattr(provisioning, "run_command", install)
    replace = provisioning.os.replace
    def replace_cli(source, target):
        if source.name == "candidate":
            raise failure("CLI replacement failed")
        if source.name == "previous":
            raise failure("CLI rollback failed")
        replace(source, target)
    monkeypatch.setattr(provisioning.os, "replace", replace_cli)
    with pytest.raises(RuntimeError) as caught:
        provisioning._ensure_cli(paths)
    diagnostic = exception_report(caught.value)
    assert "CLI replacement failed" in diagnostic and "CLI rollback failed" in diagnostic
    assert_diagnostic(diagnostic)


def test_provisioning_inspection_retains_failed_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(provisioning, "node_major", MagicMock(side_effect=failure("node probe failed")))
    result = provisioning.inspect(tmp_path)
    assert not result["ok"]
    assert result["diagnostics"][0]["probe"] == "node"
    assert_diagnostic(result["diagnostics"][0]["error"])


@pytest.mark.parametrize("body", [b"upstream crashed before JSON", b"[1,2]", b'{"ok":true,"result":{}}'])
def test_browser_http_errors_preserve_status_and_body(tmp_path, monkeypatch, body):
    manager = BrowserServiceManager(tmp_path)
    resource = SimpleNamespace(host="127.0.0.1", port=1, token="test")
    def request(*args, **kwargs):
        raise HTTPError("http://unused", 502, "bad gateway", {}, io.BytesIO(body))
    monkeypatch.setattr(browser, "urlopen", request)
    payload = manager._request_json("POST", "/action", {}, timeout_seconds=1, resource=resource)
    assert payload["ok"] is False
    assert payload["http_status"] == 502
    assert payload["http_body"] == body.decode()


def test_startup_error_captures_real_child_output(tmp_path, monkeypatch):
    manager = BrowserServiceManager(tmp_path)
    popen = subprocess.Popen
    outputs = []
    def start(command, **kwargs):
        outputs.append(kwargs["stdout"])
        child = popen([sys.executable, "-c", "import sys; print('original startup diagnostic; api_key=hidden-secret', file=sys.stderr); sys.exit(7)"], **kwargs)
        child.wait(timeout=5)
        return child
    monkeypatch.setattr(browser.subprocess, "Popen", start)
    with pytest.raises(BrowserServiceError) as caught:
        manager._ensure_started()
    text = json.dumps(caught.value.to_dict())
    assert "original startup diagnostic" in text and "hidden-secret" not in text
    assert caught.value.details["returncode"] == 7
    assert outputs[0].closed


def test_failed_sidecar_reap_keeps_process_ownership(tmp_path, monkeypatch):
    manager = BrowserServiceManager(tmp_path)
    process = SimpleNamespace(pid=9999999, poll=lambda: None, wait=MagicMock(side_effect=subprocess.TimeoutExpired("sidecar", 1)))
    resource = browser._BrowserServiceProcess(process, "localhost", 1, "test")
    manager._process = resource
    monkeypatch.setattr(manager, "_request_json", MagicMock(side_effect=failure("shutdown unavailable")))
    monkeypatch.setattr(browser.os, "killpg", MagicMock())
    with pytest.raises(BrowserServiceError) as caught:
        manager.stop_sync()
    assert manager._process is resource
    assert caught.value.details["process_stopped"] is False
    assert_diagnostic(json.dumps(caught.value.to_dict()))


def test_shutdown_keeps_original_error_when_reading_output_also_fails(tmp_path, monkeypatch):
    manager = BrowserServiceManager(tmp_path)
    process = SimpleNamespace(pid=9999999, poll=MagicMock(side_effect=[None, 0, 0]))
    output = SimpleNamespace(seek=MagicMock(side_effect=failure("output read failed")), close=MagicMock())
    manager._process = browser._BrowserServiceProcess(process, "localhost", 1, "test", output)
    monkeypatch.setattr(manager, "_request_json", MagicMock(side_effect=failure("shutdown failed")))
    with pytest.raises(BrowserServiceError) as caught:
        manager.stop_sync()
    diagnostic = exception_report(caught.value)
    assert "shutdown failed" in diagnostic and "output read failed" in diagnostic
    assert caught.value.details["process_stopped"] is True
    assert manager._process is None
    output.close.assert_called_once()
    assert_diagnostic(diagnostic)


@pytest.fixture
def sidecar(tmp_path, monkeypatch, request):
    class Worker:
        in_flight = 0
        last_activity_at = browser.time.monotonic()
        def health(self):
            return {"ok": True, "healthy": True}
        def install_in_progress(self):
            return False
        def shutdown(self):
            pass
        def execute(self, **kwargs):
            if getattr(request, "param", "") == "unexpected":
                raise failure("unexpected worker exception")
            if getattr(request, "param", "") == "serialization":
                return {"unserializable": object()}
            raise BrowserServiceError("browser operation failed", code="cli_command_failed", state_unknown=True) from failure()
    monkeypatch.setattr(browser, "_PlaywrightCliWorker", lambda **kwargs: Worker())
    # HTTPServer's loopback reverse-DNS lookup can exceed startup time on macOS.
    monkeypatch.setattr(browser.socket, "getfqdn", lambda name="": "localhost")
    server_class = browser.ThreadingHTTPServer
    servers = []
    ready = threading.Event()
    def create(*args, **kwargs):
        server = server_class(*args, **kwargs)
        servers.append(server)
        ready.set()
        return server
    monkeypatch.setattr(browser, "ThreadingHTTPServer", create)
    thread = threading.Thread(target=browser.run_browser_service_cli, kwargs={
        "runtime_root": tmp_path, "host": "127.0.0.1", "port": 0, "token": "test-token",
        "idle_timeout_seconds": 300, "max_concurrency": 1,
    }, daemon=True)
    thread.start()
    try:
        assert ready.wait(5)
        manager = BrowserServiceManager(tmp_path)
        resource = SimpleNamespace(host="127.0.0.1", port=servers[0].server_port, token="test-token")
        monkeypatch.setattr(manager, "_ensure_started", lambda: resource)
        yield manager
    finally:
        for server in servers:
            server.shutdown()
        thread.join(5)
        assert not thread.is_alive()


@pytest.mark.parametrize("sidecar", ["operation", "unexpected", "serialization"], indirect=True)
def test_real_sidecar_transport_keeps_root_cause_in_model_tool_result(core, sidecar, request):
    register_browser(core.context, WebFetchService(sidecar))
    core.publish_module_capabilities("web_fetch")
    result = core.context.execution_runtime.execute_tool(new_tool_call(name="navigate_browser", args={"url": "https://unused.test"}), turn_id="web-audit")
    assert not result.ok
    failure_kind = request.node.callspec.params["sidecar"]
    if failure_kind == "serialization":
        assert "not JSON serializable" in result.llm_text
    else:
        assert_diagnostic(result.llm_text)
    assert metadata(result)["error_code"] == {
        "operation": "cli_command_failed", "unexpected": "handler_exception", "serialization": "invalid_sidecar_output",
    }[failure_kind]
    assert metadata(result)["effect"] == "unknown"
    assert metadata(result)["retry"] == "reconcile_first"
