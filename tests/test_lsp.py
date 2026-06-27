from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
import asyncio
import time
from pathlib import Path
from unittest.mock import patch

from pal.execution.contracts import CapabilityResult
from pal.lsp.config import LspServerConfig, LspServerFileConfig, load_builtin_lsp_templates, load_lsp_server_file, lsp_config_root
from pal.lsp.connector import AsyncLspConnector
from pal.lsp.ipc import LspManagerClient
from pal.lsp.manager import LspManager, LspServerState
from pal.minion.ipc import MinionManagerClient
from pal.minion.lsp_prewarm import lsp_prewarm_plan, prewarm_workspace_lsp
from pal.shared import RuntimeStatus


class LspConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal_lsp_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_load_runtime_lsp_servers_table(self) -> None:
        path = self.root / "servers.toml"
        path.write_text(
            """
[lspServers.fake_python]
command = ["pal-lsp-missing-python-test"]
extensions = [".py", "pyi"]
language_ids = ["python"]
workspace_markers = ["pyproject.toml"]
install_hint = "install pyright"
""".strip()
            + "\n",
            encoding="utf-8",
        )

        configs = load_lsp_server_file(path)

        self.assertEqual(len(configs), 1)
        config = configs[0]
        self.assertEqual(config.config.server_id, "fake_python")
        self.assertEqual(config.config.command, ("pal-lsp-missing-python-test",))
        self.assertEqual(config.config.extensions, (".py", ".pyi"))
        self.assertEqual(config.config.language_ids, ("python",))
        self.assertEqual(config.config.workspace_markers, ("pyproject.toml",))
        self.assertEqual(config.config.install_hint, "install pyright")

    def test_builtin_templates_cover_expected_language_servers(self) -> None:
        templates = load_builtin_lsp_templates()
        server_ids = {item.config.server_id for item in templates}

        self.assertTrue(
            {
                "clangd",
                "pyright",
                "typescript",
                "rust_analyzer",
                "gopls",
                "bash",
                "json",
                "yaml",
                "html",
                "css",
            }.issubset(server_ids)
        )
        pyright = next(item.config for item in templates if item.config.server_id == "pyright")
        self.assertEqual(pyright.command, ("npx",))
        self.assertEqual(pyright.args, ("--yes", "--package", "pyright", "pyright-langserver", "--stdio"))
        self.assertEqual(pyright.startup_timeout_ms, 120_000)
        self.assertEqual(pyright.diagnostics_timeout_ms, 10_000)
        clangd = next(item.config for item in templates if item.config.server_id == "clangd")
        self.assertEqual(clangd.command, ("clangd",))
        self.assertEqual(clangd.startup_timeout_ms, 30_000)
        self.assertEqual(clangd.diagnostics_timeout_ms, 10_000)

    def test_lsp_rpc_timeout_exceeds_slowest_builtin_operation_window(self) -> None:
        templates = load_builtin_lsp_templates()
        slowest_window_ms = max(
            item.config.startup_timeout_ms + item.config.diagnostics_timeout_ms
            for item in templates
        )

        self.assertGreaterEqual(
            int(LspManagerClient(self.root).request_timeout_seconds * 1000),
            slowest_window_ms + 10_000,
        )

    def test_minion_manager_rpc_timeout_covers_lsp_prewarm_window(self) -> None:
        templates = load_builtin_lsp_templates()
        slowest_window_ms = max(
            item.config.startup_timeout_ms + item.config.diagnostics_timeout_ms
            for item in templates
        )

        self.assertGreaterEqual(
            int(MinionManagerClient(self.root).request_timeout_seconds * 1000),
            slowest_window_ms + 10_000,
        )

    def test_runtime_lsp_config_root_mirrors_minion_profile_layout(self) -> None:
        self.assertEqual(lsp_config_root(self.root), self.root / "plugins" / "lsp" / "servers")


class LspConnectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal_lsp_connector_test_"))

    async def asyncTearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    async def test_initialize_drains_large_stderr_output(self) -> None:
        server = self.root / "fake_lsp.py"
        server.write_text(
            """
import json
import sys


def read_message():
    content_length = 0
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\\r\\n", b"\\n"):
            break
        key, _, value = line.partition(b":")
        if key.lower() == b"content-length":
            content_length = int(value.strip())
    if content_length <= 0:
        return None
    return json.loads(sys.stdin.buffer.read(content_length).decode("utf-8"))


def write_message(payload):
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: " + str(len(raw)).encode("ascii") + b"\\r\\n\\r\\n" + raw)
    sys.stdout.buffer.flush()


sys.stderr.buffer.write(b"x" * 200000 + b"\\nready\\n")
sys.stderr.buffer.flush()
request = read_message()
write_message({"jsonrpc": "2.0", "id": request["id"], "result": {"serverInfo": {"name": "fake"}, "capabilities": {}}})
while read_message() is not None:
    pass
""".lstrip(),
            encoding="utf-8",
        )
        connector = AsyncLspConnector(
            LspServerConfig(
                server_id="fake",
                command=(sys.executable,),
                args=("-u", str(server)),
                extensions=(".fake",),
                language_ids=("fake",),
                startup_timeout_ms=2000,
                request_timeout_ms=2000,
            ),
            workspace_root=self.root,
        )

        try:
            await connector.initialize()

            self.assertTrue(connector.initialized)
            self.assertIn("ready", connector.stderr_tail_text())
        finally:
            await connector.close()

    async def test_connector_routes_concurrent_response_and_diagnostics_notification(self) -> None:
        sample = self.root / "sample.fake"
        sample.write_text("symbol value\n", encoding="utf-8")
        server = self.root / "fake_lsp_concurrent.py"
        server.write_text(
            """
import json
import sys


def read_message():
    content_length = 0
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\\r\\n", b"\\n"):
            break
        key, _, value = line.partition(b":")
        if key.lower() == b"content-length":
            content_length = int(value.strip())
    if content_length <= 0:
        return None
    return json.loads(sys.stdin.buffer.read(content_length).decode("utf-8"))


def write_message(payload):
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: " + str(len(raw)).encode("ascii") + b"\\r\\n\\r\\n" + raw)
    sys.stdout.buffer.flush()


request = read_message()
write_message({"jsonrpc": "2.0", "id": request["id"], "result": {"serverInfo": {"name": "fake"}, "capabilities": {}}})
opened_uri = ""
while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    if method == "textDocument/didOpen":
        opened_uri = message["params"]["textDocument"]["uri"]
        write_message({
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {"uri": opened_uri, "diagnostics": [{"message": "fixture diagnostic"}]},
        })
    elif method == "textDocument/hover":
        write_message({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"contents": {"kind": "markdown", "value": "fixture hover"}},
        })
    elif "id" in message:
        write_message({"jsonrpc": "2.0", "id": message["id"], "result": None})
""".lstrip(),
            encoding="utf-8",
        )
        connector = AsyncLspConnector(
            LspServerConfig(
                server_id="fake",
                command=(sys.executable,),
                args=("-u", str(server)),
                extensions=(".fake",),
                language_ids=("fake",),
                startup_timeout_ms=2000,
                request_timeout_ms=2000,
                diagnostics_timeout_ms=2000,
            ),
            workspace_root=self.root,
        )

        try:
            await connector.initialize()

            diagnostics_result, hover_result = await asyncio.gather(
                connector.diagnostics(sample, language_id="fake"),
                connector.request(
                    "textDocument/hover",
                    {"textDocument": {"uri": sample.resolve().as_uri()}, "position": {"line": 0, "character": 1}},
                ),
            )

            self.assertEqual(diagnostics_result["status"], "ok")
            self.assertEqual(diagnostics_result["diagnostics"][0]["message"], "fixture diagnostic")
            self.assertEqual(hover_result["contents"]["value"], "fixture hover")
        finally:
            await connector.close()


class LspManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal_lsp_manager_test_"))
        self.manager = LspManager(runtime_root=self.root)

    async def asyncTearDown(self) -> None:
        await self.manager.close_all()
        shutil.rmtree(self.root, ignore_errors=True)

    async def test_rescan_runtime_config_overrides_builtin_and_reports_missing_binary(self) -> None:
        config_root = lsp_config_root(self.root)
        config_root.mkdir(parents=True)
        (config_root / "pyright.toml").write_text(
            """
server_id = "pyright"
command = ["pal-lsp-missing-pyright-test"]
extensions = [".py"]
language_ids = ["python"]
workspace_markers = ["pyproject.toml"]
""".strip()
            + "\n",
            encoding="utf-8",
        )
        (self.root / "pyproject.toml").write_text("[project]\nname = \"demo\"\n", encoding="utf-8")

        result = await self.manager.rescan()
        doctor = await self.manager.doctor({"server_id": "pyright", "workspace_root": str(self.root)})

        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.manager.states["pyright"].file_config.source, "runtime")
        self.assertEqual(doctor["status"], "unavailable")
        binary_check = next(item for item in doctor["checks"] if item["name"] == "binary")
        self.assertEqual(binary_check["status"], "missing_binary")

    async def test_operation_returns_unavailable_without_spawning_missing_server(self) -> None:
        config_root = lsp_config_root(self.root)
        config_root.mkdir(parents=True)
        (config_root / "fake.toml").write_text(
            """
server_id = "fake_foo"
command = ["pal-lsp-missing-foo-test"]
extensions = [".foo"]
language_ids = ["foo"]
""".strip()
            + "\n",
            encoding="utf-8",
        )
        sample = self.root / "sample.foo"
        sample.write_text("symbol value\n", encoding="utf-8")

        await self.manager.rescan()
        result = await self.manager.run_lsp_operation("diagnostics", {"file": str(sample)})

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "missing_binary")
        self.assertEqual(result["server"]["server_id"], "fake_foo")
        self.assertFalse(self.manager.states["fake_foo"].attached)

    async def test_doctor_without_file_or_server_does_not_pick_arbitrary_server(self) -> None:
        await self.manager.rescan()

        result = await self.manager.doctor({})

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "server_id_or_file_required")
        self.assertIn("servers", result)
        self.assertFalse(any(state.attached for state in self.manager.states.values()))

    async def test_operation_resolves_relative_file_against_workspace_root(self) -> None:
        config_root = lsp_config_root(self.root)
        config_root.mkdir(parents=True)
        (config_root / "fake.toml").write_text(
            """
server_id = "fake_foo"
command = ["pal-lsp-missing-foo-test"]
extensions = [".foo"]
language_ids = ["foo"]
""".strip()
            + "\n",
            encoding="utf-8",
        )
        workspace = self.root / "workspace"
        source_dir = workspace / "src"
        source_dir.mkdir(parents=True)
        (source_dir / "sample.foo").write_text("symbol value\n", encoding="utf-8")

        await self.manager.rescan()
        result = await self.manager.run_lsp_operation(
            "diagnostics",
            {"file": "src/sample.foo", "workspace_root": str(workspace)},
        )

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "missing_binary")
        self.assertEqual(result["server"]["server_id"], "fake_foo")

    async def test_extension_language_takes_priority_over_workspace_language(self) -> None:
        python_state = LspServerState(
            file_config=LspServerFileConfig(
                config=LspServerConfig(
                    server_id="pyright",
                    command=(sys.executable,),
                    extensions=(".py",),
                    language_ids=("python",),
                ),
                source="test",
                config_path=str(self.root / "pyright.toml"),
            ),
            config_path=self.root / "pyright.toml",
        )
        typescript_state = LspServerState(
            file_config=LspServerFileConfig(
                config=LspServerConfig(
                    server_id="typescript",
                    command=(sys.executable,),
                    extensions=(".ts", ".js"),
                    language_ids=("typescript", "javascript"),
                ),
                source="test",
                config_path=str(self.root / "typescript.toml"),
            ),
            config_path=self.root / "typescript.toml",
        )
        self.manager.states = {python_state.server_id: python_state, typescript_state.server_id: typescript_state}

        selected = self.manager._select_state({"file": "src/app.js", "workspace_languages": ["python"]})
        language_id = self.manager._language_id(selected, Path("src/app.js"), {"workspace_languages": ["python"]})

        self.assertEqual(selected.server_id, "typescript")
        self.assertEqual(language_id, "javascript")

    async def test_workspace_language_falls_back_when_extension_does_not_select_language(self) -> None:
        python_state = LspServerState(
            file_config=LspServerFileConfig(
                config=LspServerConfig(
                    server_id="pyright",
                    command=(sys.executable,),
                    extensions=(".py",),
                    language_ids=("python",),
                ),
                source="test",
                config_path=str(self.root / "pyright.toml"),
            ),
            config_path=self.root / "pyright.toml",
        )
        clangd_state = LspServerState(
            file_config=LspServerFileConfig(
                config=LspServerConfig(
                    server_id="clangd",
                    command=(sys.executable,),
                    extensions=(".c", ".cpp", ".h"),
                    language_ids=("c", "cpp"),
                ),
                source="test",
                config_path=str(self.root / "clangd.toml"),
            ),
            config_path=self.root / "clangd.toml",
        )
        self.manager.states = {python_state.server_id: python_state, clangd_state.server_id: clangd_state}

        selected = self.manager._select_state({"file": "Makefile", "workspace_languages": ["cpp"]})
        fallback_language = self.manager._language_id(selected, Path("Makefile"), {"workspace_languages": ["cpp"]})
        header_language = self.manager._language_id(clangd_state, Path("include/lib.h"), {"workspace_languages": ["c"]})

        self.assertEqual(selected.server_id, "clangd")
        self.assertEqual(fallback_language, "cpp")
        self.assertEqual(header_language, "c")

    async def test_server_keeps_separate_workspace_sessions(self) -> None:
        workspace_a = self.root / "workspace_a"
        workspace_b = self.root / "workspace_b"
        workspace_a.mkdir()
        workspace_b.mkdir()
        initialized: list[Path] = []
        closed: list[Path] = []

        class FakeConnector:
            def __init__(self, config: LspServerConfig, *, workspace_root: Path) -> None:
                _ = config
                self.workspace_root = workspace_root

            async def initialize(self) -> None:
                initialized.append(self.workspace_root)

            async def request(self, method: str, params: dict) -> dict:
                self_method = method
                return {
                    "value": [
                        {
                            "method": self_method,
                            "workspace_root": str(self.workspace_root),
                            "query": str(params.get("query") or ""),
                        }
                    ]
                }

            async def close(self) -> None:
                closed.append(self.workspace_root)

        state = LspServerState(
            file_config=LspServerFileConfig(
                config=LspServerConfig(
                    server_id="fake_python",
                    command=(sys.executable,),
                    extensions=(".py",),
                    language_ids=("python",),
                ),
                source="test",
                config_path=str(self.root / "fake.toml"),
            ),
            config_path=self.root / "fake.toml",
        )
        self.manager.states[state.server_id] = state

        with patch("pal.lsp.manager.AsyncLspConnector", FakeConnector):
            first = await self.manager.run_lsp_operation(
                "workspace_symbols",
                {"server_id": "fake_python", "workspace_root": str(workspace_a), "query": "A"},
            )
            second = await self.manager.run_lsp_operation(
                "workspace_symbols",
                {"server_id": "fake_python", "workspace_root": str(workspace_b), "query": "B"},
            )
            third = await self.manager.run_lsp_operation(
                "workspace_symbols",
                {"server_id": "fake_python", "workspace_root": str(workspace_a), "query": "C"},
            )

        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "ok")
        self.assertEqual(third["status"], "ok")
        self.assertEqual(initialized, [workspace_a.resolve(), workspace_b.resolve()])
        self.assertEqual(len(state.sessions), 2)
        self.assertEqual(third["result"][0]["workspace_root"], str(workspace_a.resolve()))
        summary = self.manager._server_summary(state)
        self.assertEqual(summary["attached_count"], 2)
        await self.manager.close_all()
        self.assertEqual(sorted(str(path) for path in closed), sorted(str(path.resolve()) for path in (workspace_a, workspace_b)))

    async def test_idle_eviction_closes_only_stale_workspace_sessions(self) -> None:
        workspace_a = self.root / "workspace_a"
        workspace_b = self.root / "workspace_b"
        workspace_a.mkdir()
        workspace_b.mkdir()
        closed: list[Path] = []

        class FakeConnector:
            def __init__(self, config: LspServerConfig, *, workspace_root: Path) -> None:
                _ = config
                self.workspace_root = workspace_root

            async def initialize(self) -> None:
                return None

            async def request(self, method: str, params: dict) -> dict:
                _ = method
                return {"value": [{"workspace_root": str(self.workspace_root), "query": str(params.get("query") or "")}]}

            async def close(self) -> None:
                closed.append(self.workspace_root)

        state = LspServerState(
            file_config=LspServerFileConfig(
                config=LspServerConfig(
                    server_id="fake_python",
                    command=(sys.executable,),
                    extensions=(".py",),
                    language_ids=("python",),
                ),
                source="test",
                config_path=str(self.root / "fake.toml"),
            ),
            config_path=self.root / "fake.toml",
        )
        self.manager.states[state.server_id] = state

        with patch("pal.lsp.manager.AsyncLspConnector", FakeConnector):
            await self.manager.run_lsp_operation(
                "workspace_symbols",
                {"server_id": "fake_python", "workspace_root": str(workspace_a), "query": "A"},
            )
            await self.manager.run_lsp_operation(
                "workspace_symbols",
                {"server_id": "fake_python", "workspace_root": str(workspace_b), "query": "B"},
            )

        stale = state.sessions[str(workspace_a.resolve())]
        active = state.sessions[str(workspace_b.resolve())]
        now = time.monotonic()
        stale.last_used_at = now - 10.0
        active.last_used_at = now

        result = await self.manager.evict_idle_sessions(now=now, idle_seconds=1.0)

        self.assertEqual(result["evicted_count"], 1)
        self.assertEqual(closed, [workspace_a.resolve()])
        self.assertNotIn(str(workspace_a.resolve()), state.sessions)
        self.assertIn(str(workspace_b.resolve()), state.sessions)
        self.assertTrue(state.attached)
        self.assertIs(state.connector, active.connector)

    async def test_recent_attach_failure_short_circuits_repeated_operations(self) -> None:
        sample = self.root / "sample.foo"
        sample.write_text("symbol value\n", encoding="utf-8")
        attempts: list[Path] = []

        class FailingConnector:
            def __init__(self, config: LspServerConfig, *, workspace_root: Path) -> None:
                _ = config
                self.workspace_root = workspace_root

            async def initialize(self) -> None:
                attempts.append(self.workspace_root)
                raise TimeoutError("startup timed out")

            async def close(self) -> None:
                return None

        state = LspServerState(
            file_config=LspServerFileConfig(
                config=LspServerConfig(
                    server_id="fake_foo",
                    command=(sys.executable,),
                    extensions=(".foo",),
                    language_ids=("foo",),
                ),
                source="test",
                config_path=str(self.root / "fake.toml"),
            ),
            config_path=self.root / "fake.toml",
        )
        self.manager.states[state.server_id] = state

        with patch("pal.lsp.manager.AsyncLspConnector", FailingConnector):
            first = await self.manager.run_lsp_operation("diagnostics", {"file": str(sample)})
            second = await self.manager.run_lsp_operation("diagnostics", {"file": str(sample)})

        self.assertEqual(first["status"], "unavailable")
        self.assertIn("attach_failed:TimeoutError", first["reason"])
        self.assertEqual(second["status"], "unavailable")
        self.assertIn("recent_attach_failure:TimeoutError", second["reason"])
        self.assertEqual(len(attempts), 1)

    async def test_extended_operations_use_fixed_lsp_methods(self) -> None:
        sample = self.root / "sample.py"
        sample.write_text("def caller():\n    return target()\n\ndef target():\n    return 1\n", encoding="utf-8")
        (self.root / "pyproject.toml").write_text("[project]\nname = \"demo\"\n", encoding="utf-8")

        class FakeConnector:
            def __init__(self, workspace_root: Path) -> None:
                self.workspace_root = workspace_root
                self.requests: list[tuple[str, dict]] = []

            async def ensure_document_open(self, file_path: Path, *, language_id: str) -> dict:
                _ = language_id
                return {"uri": file_path.resolve().as_uri(), "file_sha256": "fake-sha", "text": file_path.read_text(encoding="utf-8")}

            async def request(self, method: str, params: dict) -> dict:
                self.requests.append((method, params))
                if method == "textDocument/implementation":
                    return {"value": [{"uri": params["textDocument"]["uri"], "range": {"start": {"line": 3, "character": 4}}}]}
                if method == "textDocument/prepareCallHierarchy":
                    return {
                        "value": [
                            {
                                "name": "target",
                                "kind": 12,
                                "uri": params["textDocument"]["uri"],
                                "range": {"start": {"line": 3, "character": 0}, "end": {"line": 4, "character": 12}},
                                "selectionRange": {"start": {"line": 3, "character": 4}, "end": {"line": 3, "character": 10}},
                            }
                        ]
                    }
                if method == "callHierarchy/incomingCalls":
                    return {"value": [{"from": {"name": "caller"}, "fromRanges": [{"start": {"line": 1, "character": 11}}]}]}
                if method == "callHierarchy/outgoingCalls":
                    return {"value": [{"to": {"name": "dependency"}, "fromRanges": [{"start": {"line": 1, "character": 11}}]}]}
                raise AssertionError(f"unexpected LSP method: {method}")

            async def close(self) -> None:
                return None

        connector = FakeConnector(self.root)
        state = LspServerState(
            file_config=LspServerFileConfig(
                config=LspServerConfig(
                    server_id="fake_python",
                    command=(sys.executable,),
                    extensions=(".py",),
                    language_ids=("python",),
                ),
                source="test",
                config_path=str(self.root / "fake.toml"),
            ),
            config_path=self.root / "fake.toml",
            connector=connector,  # type: ignore[arg-type]
            attached=True,
        )
        self.manager.states[state.server_id] = state

        async def fake_ensure_attached(target_state: LspServerState, workspace_root: Path) -> None:
            target_state.connector = connector  # type: ignore[assignment]
            target_state.attached = True
            connector.workspace_root = workspace_root

        self.manager._ensure_attached = fake_ensure_attached  # type: ignore[method-assign]

        implementation = await self.manager.run_lsp_operation("implementation", {"file": str(sample), "line": 3, "character": 4})
        prepared = await self.manager.run_lsp_operation("prepare_call_hierarchy", {"file": str(sample), "line": 3, "character": 4})
        incoming = await self.manager.run_lsp_operation("incoming_calls", {"file": str(sample), "line": 3, "character": 4})
        outgoing = await self.manager.run_lsp_operation("outgoing_calls", {"file": str(sample), "line": 3, "character": 4})

        methods = [item[0] for item in connector.requests]
        self.assertIn("textDocument/implementation", methods)
        self.assertGreaterEqual(methods.count("textDocument/prepareCallHierarchy"), 3)
        self.assertIn("callHierarchy/incomingCalls", methods)
        self.assertIn("callHierarchy/outgoingCalls", methods)
        self.assertEqual(implementation["evidence"]["method"], "textDocument/implementation")
        self.assertEqual(prepared["evidence"]["method"], "textDocument/prepareCallHierarchy")
        self.assertEqual(incoming["evidence"]["method"], "callHierarchy/incomingCalls")
        self.assertEqual(outgoing["evidence"]["method"], "callHierarchy/outgoingCalls")
        self.assertEqual(incoming["result"]["items"][0]["name"], "target")
        self.assertEqual(incoming["result"]["calls"][0]["calls"][0]["from"]["name"], "caller")
        self.assertEqual(outgoing["result"]["calls"][0]["calls"][0]["to"]["name"], "dependency")


class LspPrewarmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal_lsp_prewarm_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_prewarm_plan_uses_workspace_lsp_setup_servers(self) -> None:
        workspace = {
            "repo_path": str(self.root),
            "languages": ["python"],
            "lsp_setup": {"servers": ["pyright", "pyright"], "languages": ["python"]},
        }

        plan = lsp_prewarm_plan(workspace)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.workspace_root, self.root.resolve())
        self.assertEqual(plan.server_ids, ("pyright",))
        self.assertEqual(plan.languages, ("python",))

    def test_prewarm_calls_lsp_doctor_for_each_server(self) -> None:
        calls = []

        class FakeProvider:
            def __init__(self, *, runtime_root: Path) -> None:
                self.runtime_root = runtime_root

            def doctor(self, call):
                calls.append(call)
                return CapabilityResult(
                    status=RuntimeStatus.OK,
                    text="ok",
                    llm_text="ok",
                    structured={"status": "ok"},
                )

        result = prewarm_workspace_lsp(
            runtime_root=self.root,
            workspace={
                "repo_path": str(self.root),
                "languages": ["python"],
                "lsp_setup": {"servers": ["pyright"], "languages": ["python"]},
            },
            provider_factory=FakeProvider,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["ok_count"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args["server_id"], "pyright")
        self.assertEqual(calls[0].args["workspace_root"], str(self.root.resolve()))
        self.assertEqual(calls[0].args["workspace_languages"], ["python"])
