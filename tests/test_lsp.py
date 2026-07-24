from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
import asyncio
import time
from pathlib import Path
from unittest.mock import patch

from pal.lsp.config import LspServerConfig, LspServerFileConfig, load_builtin_lsp_templates, load_lsp_server_file, lsp_config_root
from pal.lsp.connector import AsyncLspConnector, LspProtocolError
from pal.lsp.environment import prepare_workspace_lsp_environment
from pal.lsp.ipc import LspManagerClient
from pal.lsp.manager import LspManager, LspServerState
from pal.minion.ipc import MinionManagerClient
from pal.minion.lsp_prewarm import DEFAULT_LSP_PREWARM_TIMEOUT_SECONDS, lsp_prewarm_plan, prewarm_workspace_lsp


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
        self.assertEqual(pyright.command, ("pyright-langserver",))
        self.assertEqual(pyright.args, ("--stdio",))
        self.assertEqual(pyright.startup_timeout_ms, 30_000)
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

    async def test_connector_increments_document_versions_and_clears_them_on_close(self) -> None:
        sample = self.root / "versioned.fake"
        sample.write_text("one\n", encoding="utf-8")
        connector = AsyncLspConnector(
            LspServerConfig(
                server_id="fake",
                command=(sys.executable,),
                extensions=(".fake",),
                language_ids=("fake",),
            ),
            workspace_root=self.root,
        )
        notifications: list[tuple[str, dict]] = []

        async def record(method: str, params: dict) -> None:
            notifications.append((method, params))

        connector.notify = record  # type: ignore[method-assign]
        await connector.ensure_document_open(sample, language_id="fake")
        sample.write_text("two\n", encoding="utf-8")
        await connector.ensure_document_open(sample, language_id="fake")
        sample.write_text("three\n", encoding="utf-8")
        await connector.ensure_document_open(sample, language_id="fake")

        versions = [
            int(params["textDocument"]["version"])
            for method, params in notifications
            if method in {"textDocument/didOpen", "textDocument/didChange"}
        ]
        self.assertEqual(versions, [1, 2, 3])
        await connector.close()
        self.assertEqual(connector._document_versions, {})
        self.assertEqual(connector._open_hashes, {})


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

    async def test_prepared_project_context_controls_workspace_session_args(self) -> None:
        workspace = self.root / "prepared_cpp"
        workspace.mkdir()
        (workspace / "module.cpp").write_text(
            "int value() { return 1; }\n",
            encoding="utf-8",
        )
        context = self.root / "lsp-context"
        context.mkdir()
        compile_commands = context / "compile_commands.json"
        compile_commands.write_text("[]\n", encoding="utf-8")
        observed_args: list[tuple[str, ...]] = []

        class FakeConnector:
            def __init__(
                self,
                config: LspServerConfig,
                *,
                workspace_root: Path,
                extra_args: tuple[str, ...] = (),
            ) -> None:
                _ = config
                self.workspace_root = workspace_root
                self.extra_args = extra_args
                self.server_info = {}

            async def initialize(self) -> None:
                observed_args.append(self.extra_args)

            async def request(self, method: str, params: dict) -> dict:
                _ = method, params
                return {"value": []}

            async def ensure_document_open(self, file_path: Path, *, language_id: str) -> dict:
                _ = language_id
                return {
                    "uri": file_path.resolve().as_uri(),
                    "file_sha256": "probe-file",
                }

            async def diagnostics(self, file_path: Path, *, language_id: str) -> dict:
                _ = file_path, language_id
                return {
                    "status": "ok",
                    "diagnostics": [],
                    "diagnostics_state": "fresh",
                }

            async def close(self) -> None:
                return None

        state = LspServerState(
            file_config=LspServerFileConfig(
                config=LspServerConfig(
                    server_id="clangd",
                    command=(sys.executable,),
                    extensions=(".cpp",),
                    language_ids=("cpp",),
                ),
                source="test",
                config_path=str(self.root / "clangd.toml"),
            ),
            config_path=self.root / "clangd.toml",
        )
        self.manager.states = {state.server_id: state}
        with patch("pal.lsp.manager.AsyncLspConnector", FakeConnector):
            prepared = await self.manager.prepare_workspace(
                {
                    "workspace_root": str(workspace),
                    "primary_language": "cpp",
                    "compile_commands_path": str(compile_commands),
                }
            )
            result = await self.manager.run_lsp_operation(
                "workspace_symbols",
                {
                    "server_id": "clangd",
                    "workspace_root": str(workspace),
                    "query": "value",
                },
            )

        self.assertEqual(prepared["status"], "ok")
        self.assertEqual(
            prepared["servers"][0]["probe"],
            {
                "status": "ok",
                "operation": "diagnostics+document_symbols",
                "file": str((workspace / "module.cpp").resolve()),
                "recognized": True,
                "diagnostic_count": 0,
                "semantic_probe": {
                    "operation": "document_symbols",
                    "symbol_count": 0,
                },
            },
        )
        self.assertEqual(prepared["primary_server"], "clangd")
        self.assertTrue(prepared["primary_probe_ready"])
        self.assertEqual(prepared["optional_server_failures"], [])
        status = self.manager.status({"workspace_root": str(workspace)})
        self.assertEqual(status["status"], "ok")
        self.assertTrue(status["workspace"]["prepared"])
        self.assertEqual(status["workspace"]["primary_server"], "clangd")
        self.assertTrue(status["workspace"]["primary_probe_ready"])
        restarted = LspManager(runtime_root=self.root)
        restarted_status = restarted.status({"workspace_root": str(workspace)})
        self.assertEqual(restarted_status["status"], "ok")
        self.assertTrue(restarted_status["workspace"]["primary_probe_ready"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(observed_args, [(f"--compile-commands-dir={context}",)])

    async def test_workspace_status_does_not_fall_back_to_another_worktree(self) -> None:
        prepared = self.root / "prepared"
        other = self.root / "other"
        prepared.mkdir()
        other.mkdir()
        self.manager.workspace_environments[str(prepared.resolve())] = {
            "workspace_root": str(prepared.resolve()),
            "primary_language": "cpp",
            "fingerprint": "prepared-fingerprint",
            "prepared_at": "2026-07-24T10:00:00+00:00",
            "readiness": {
                "status": "ok",
                "primary_server": "clangd",
                "primary_probe_ready": True,
                "servers": [{"server_id": "clangd", "status": "ok"}],
                "optional_server_failures": [],
            },
        }

        status = await self.manager._call_method(
            "status",
            {"workspace_root": str(other)},
        )

        self.assertEqual(status["status"], "unavailable")
        self.assertFalse(status["workspace"]["prepared"])
        self.assertEqual(status["workspace"]["reason"], "workspace_not_prepared")

    async def test_primary_probe_stays_ready_when_optional_server_is_unavailable(self) -> None:
        workspace = self.root / "prepared_cpp_with_task_yaml"
        workspace.mkdir()
        (workspace / "module.cpp").write_text(
            "int value() { return 1; }\n",
            encoding="utf-8",
        )
        (workspace / "task.yaml").write_text("goal: test\n", encoding="utf-8")

        class FakeConnector:
            def __init__(
                self,
                config: LspServerConfig,
                *,
                workspace_root: Path,
                extra_args: tuple[str, ...] = (),
            ) -> None:
                _ = config, extra_args
                self.workspace_root = workspace_root
                self.server_info = {}

            async def initialize(self) -> None:
                return None

            async def request(self, method: str, params: dict) -> dict:
                _ = method, params
                return {"value": []}

            async def ensure_document_open(
                self,
                file_path: Path,
                *,
                language_id: str,
            ) -> dict:
                _ = language_id
                return {
                    "uri": file_path.resolve().as_uri(),
                    "file_sha256": "probe-file",
                }

            async def diagnostics(self, file_path: Path, *, language_id: str) -> dict:
                _ = file_path, language_id
                return {
                    "status": "ok",
                    "diagnostics": [],
                    "diagnostics_state": "fresh",
                }

            async def close(self) -> None:
                return None

        state = LspServerState(
            file_config=LspServerFileConfig(
                config=LspServerConfig(
                    server_id="clangd",
                    command=(sys.executable,),
                    extensions=(".cpp",),
                    language_ids=("cpp",),
                ),
                source="test",
                config_path=str(self.root / "clangd.toml"),
            ),
            config_path=self.root / "clangd.toml",
        )
        self.manager.states = {state.server_id: state}

        with patch("pal.lsp.manager.AsyncLspConnector", FakeConnector):
            prepared = await self.manager.prepare_workspace(
                {
                    "workspace_root": str(workspace),
                    "primary_language": "cpp",
                }
            )

        self.assertEqual(prepared["status"], "ok")
        self.assertTrue(prepared["primary_probe_ready"])
        self.assertEqual(
            prepared["optional_server_failures"],
            [
                {
                    "server_id": "yaml",
                    "status": "unavailable",
                    "reason": "server_not_configured",
                }
            ],
        )

    async def test_clangd_preparation_rejects_failed_semantic_probe(self) -> None:
        workspace = self.root / "prepared_cpp_without_symbol_support"
        workspace.mkdir()
        (workspace / "module.cpp").write_text(
            "int value() { return 1; }\n",
            encoding="utf-8",
        )

        class FakeConnector:
            def __init__(
                self,
                config: LspServerConfig,
                *,
                workspace_root: Path,
                extra_args: tuple[str, ...] = (),
            ) -> None:
                _ = config, extra_args
                self.workspace_root = workspace_root
                self.server_info = {}

            async def initialize(self) -> None:
                return None

            async def request(self, method: str, params: dict) -> dict:
                _ = method, params
                raise LspProtocolError("semantic probe unavailable")

            async def ensure_document_open(
                self,
                file_path: Path,
                *,
                language_id: str,
            ) -> dict:
                _ = language_id
                return {
                    "uri": file_path.resolve().as_uri(),
                    "file_sha256": "probe-file",
                }

            async def diagnostics(self, file_path: Path, *, language_id: str) -> dict:
                _ = file_path, language_id
                return {
                    "status": "ok",
                    "diagnostics": [],
                    "diagnostics_state": "fresh",
                }

            async def close(self) -> None:
                return None

        state = LspServerState(
            file_config=LspServerFileConfig(
                config=LspServerConfig(
                    server_id="clangd",
                    command=(sys.executable,),
                    extensions=(".cpp",),
                    language_ids=("cpp",),
                ),
                source="test",
                config_path=str(self.root / "clangd.toml"),
            ),
            config_path=self.root / "clangd.toml",
        )
        self.manager.states = {state.server_id: state}

        with patch("pal.lsp.manager.AsyncLspConnector", FakeConnector):
            prepared = await self.manager.prepare_workspace(
                {
                    "workspace_root": str(workspace),
                    "primary_language": "cpp",
                }
            )

        self.assertEqual(prepared["status"], "unavailable")
        self.assertFalse(prepared["primary_probe_ready"])
        self.assertIn(
            "semantic_probe_failed:request_failed_after_restart:"
            "LspProtocolError: semantic probe unavailable",
            prepared["servers"][0]["probe"]["reason"],
        )

    async def test_workspace_preparation_is_idempotent_and_restarts_changed_session(self) -> None:
        workspace = self.root / "prepared_repeatedly"
        workspace.mkdir()
        (workspace / "module.cpp").write_text("int value() { return 1; }\n", encoding="utf-8")
        initialized: list[tuple[str, ...]] = []
        closed: list[tuple[str, ...]] = []

        class FakeConnector:
            def __init__(
                self,
                config: LspServerConfig,
                *,
                workspace_root: Path,
                extra_args: tuple[str, ...] = (),
            ) -> None:
                _ = config
                self.workspace_root = workspace_root
                self.extra_args = extra_args
                self.server_info = {}

            async def initialize(self) -> None:
                initialized.append(self.extra_args)

            async def request(self, method: str, params: dict) -> dict:
                _ = method, params
                return {"value": []}

            async def ensure_document_open(self, file_path: Path, *, language_id: str) -> dict:
                _ = language_id
                return {
                    "uri": file_path.resolve().as_uri(),
                    "file_sha256": "probe-file",
                }

            async def diagnostics(self, file_path: Path, *, language_id: str) -> dict:
                _ = file_path, language_id
                return {
                    "status": "ok",
                    "diagnostics": [],
                    "diagnostics_state": "fresh",
                }

            async def close(self) -> None:
                closed.append(self.extra_args)

        state = LspServerState(
            file_config=LspServerFileConfig(
                config=LspServerConfig(
                    server_id="clangd",
                    command=(sys.executable,),
                    extensions=(".cpp",),
                    language_ids=("cpp",),
                ),
                source="test",
                config_path=str(self.root / "clangd.toml"),
            ),
            config_path=self.root / "clangd.toml",
        )
        self.manager.states = {state.server_id: state}

        with patch("pal.lsp.manager.AsyncLspConnector", FakeConnector):
            first = await self.manager.prepare_workspace(
                {
                    "workspace_root": str(workspace),
                    "primary_language": "cpp",
                    "cpp_standard": "c++17",
                }
            )
            repeated = await self.manager.prepare_workspace(
                {
                    "workspace_root": str(workspace),
                    "primary_language": "cpp",
                    "cpp_standard": "c++17",
                }
            )
            changed = await self.manager.prepare_workspace(
                {
                    "workspace_root": str(workspace),
                    "primary_language": "cpp",
                    "cpp_standard": "c++20",
                }
            )

        self.assertTrue(first["environment_changed"])
        self.assertFalse(repeated["environment_changed"])
        self.assertTrue(changed["environment_changed"])
        self.assertEqual(len(initialized), 2)
        self.assertEqual(len(closed), 1)
        self.assertNotEqual(first["environment_fingerprint"], changed["environment_fingerprint"])

        restarted_manager = LspManager(runtime_root=self.root)
        restored = restarted_manager._workspace_environment(workspace)
        self.assertEqual(restored["fingerprint"], changed["environment_fingerprint"])
        self.assertEqual(restored["primary_language"], "cpp")

    async def test_workspace_preparation_requires_a_recognized_source_file(self) -> None:
        workspace = self.root / "prepared_without_source"
        workspace.mkdir()

        class FakeConnector:
            def __init__(
                self,
                config: LspServerConfig,
                *,
                workspace_root: Path,
                extra_args: tuple[str, ...] = (),
            ) -> None:
                _ = config, extra_args
                self.workspace_root = workspace_root
                self.server_info = {}

            async def initialize(self) -> None:
                return None

            async def close(self) -> None:
                return None

        state = LspServerState(
            file_config=LspServerFileConfig(
                config=LspServerConfig(
                    server_id="clangd",
                    command=(sys.executable,),
                    extensions=(".cpp",),
                    language_ids=("cpp",),
                ),
                source="test",
                config_path=str(self.root / "clangd.toml"),
            ),
            config_path=self.root / "clangd.toml",
        )
        self.manager.states = {state.server_id: state}

        with patch("pal.lsp.manager.AsyncLspConnector", FakeConnector):
            prepared = await self.manager.prepare_workspace(
                {
                    "workspace_root": str(workspace),
                    "primary_language": "cpp",
                }
            )

        self.assertEqual(prepared["status"], "unavailable")
        self.assertEqual(
            prepared["servers"],
            [
                {
                    "server_id": "clangd",
                    "status": "unavailable",
                    "reason": "recognition_probe_failed:no_matching_source_file",
                    "probe": {
                        "status": "unavailable",
                        "operation": "diagnostics",
                        "reason": "no_matching_source_file",
                    },
                }
            ],
        )

    async def test_workspace_preparation_rejects_unresolved_project_include(self) -> None:
        workspace = self.root / "prepared_with_unresolved_include"
        source = workspace / "src" / "module.cpp"
        source.parent.mkdir(parents=True)
        source.write_text(
            '#include "package/missing.hpp"\nint value() { return 1; }\n',
            encoding="utf-8",
        )
        header = workspace / "include" / "package" / "public.hpp"
        header.parent.mkdir(parents=True)
        header.write_text("int value();\n", encoding="utf-8")

        class FakeConnector:
            def __init__(
                self,
                config: LspServerConfig,
                *,
                workspace_root: Path,
                extra_args: tuple[str, ...] = (),
            ) -> None:
                _ = config, extra_args
                self.workspace_root = workspace_root
                self.server_info = {}

            async def initialize(self) -> None:
                return None

            async def ensure_document_open(
                self,
                file_path: Path,
                *,
                language_id: str,
            ) -> dict:
                _ = language_id
                return {
                    "uri": file_path.resolve().as_uri(),
                    "file_sha256": "probe-file",
                }

            async def diagnostics(self, file_path: Path, *, language_id: str) -> dict:
                _ = language_id
                if file_path.suffix == ".hpp":
                    return {
                        "status": "ok",
                        "diagnostics_state": "fresh",
                        "diagnostics": [],
                    }
                return {
                    "status": "ok",
                    "diagnostics_state": "fresh",
                    "diagnostics": [
                        {
                            "code": "pp_file_not_found",
                            "message": "'package/missing.hpp' file not found",
                            "severity": 1,
                        }
                    ],
                }

            async def close(self) -> None:
                return None

        state = LspServerState(
            file_config=LspServerFileConfig(
                config=LspServerConfig(
                    server_id="clangd",
                    command=(sys.executable,),
                    extensions=(".cpp",),
                    language_ids=("cpp",),
                ),
                source="test",
                config_path=str(self.root / "clangd.toml"),
            ),
            config_path=self.root / "clangd.toml",
        )
        self.manager.states = {state.server_id: state}

        with patch("pal.lsp.manager.AsyncLspConnector", FakeConnector):
            prepared = await self.manager.prepare_workspace(
                {
                    "workspace_root": str(workspace),
                    "primary_language": "cpp",
                }
            )

        self.assertEqual(prepared["status"], "unavailable")
        server = prepared["servers"][0]
        self.assertEqual(server["status"], "unavailable")
        self.assertIn(
            "recognition_probe_failed:unresolved_project_include:",
            server["reason"],
        )
        self.assertEqual(
            server["probe"]["diagnostic"]["code"],
            "pp_file_not_found",
        )

    async def test_cpp_fallback_context_includes_existing_public_include_root(self) -> None:
        workspace = self.root / "prepared_cpp_include"
        include = workspace / "include"
        include.mkdir(parents=True)
        (workspace / "module.cpp").write_text(
            '#include "package/value.hpp"\nint value() { return package_value(); }\n',
            encoding="utf-8",
        )
        package = include / "package"
        package.mkdir()
        (package / "value.hpp").write_text(
            "inline int package_value() { return 1; }\n",
            encoding="utf-8",
        )

        setup, unavailable = prepare_workspace_lsp_environment(
            workspace_root=workspace,
            primary_language="cpp",
            context_root=self.root / "contexts",
            workspace={"cpp_standard": "c++17"},
        )

        self.assertEqual(unavailable, [])
        context = setup["project_contexts"]["clangd"]
        self.assertEqual(context["kind"], "generated_compile_flags")
        flags = Path(context["source_path"]).read_text(encoding="utf-8").splitlines()
        self.assertIn(f"-I{workspace.resolve()}", flags)
        self.assertIn(f"-I{include.resolve()}", flags)

    async def test_cpp_fallback_context_is_materialized_per_worktree(self) -> None:
        worktree_a = self.root / "worktree_a"
        worktree_b = self.root / "worktree_b"
        for worktree in (worktree_a, worktree_b):
            (worktree / "include").mkdir(parents=True)
            (worktree / "module.cpp").write_text(
                "int value() { return 1; }\n",
                encoding="utf-8",
            )

        setup_a, unavailable_a = prepare_workspace_lsp_environment(
            workspace_root=worktree_a,
            primary_language="cpp",
            context_root=self.root / "contexts",
            workspace={"cpp_standard": "c++17"},
        )
        setup_b, unavailable_b = prepare_workspace_lsp_environment(
            workspace_root=worktree_b,
            primary_language="cpp",
            context_root=self.root / "contexts",
            workspace={"cpp_standard": "c++17"},
        )

        self.assertEqual(unavailable_a, [])
        self.assertEqual(unavailable_b, [])
        context_a = setup_a["project_contexts"]["clangd"]
        context_b = setup_b["project_contexts"]["clangd"]
        self.assertNotEqual(context_a["source_path"], context_b["source_path"])
        flags_a = Path(context_a["source_path"]).read_text(encoding="utf-8")
        flags_b = Path(context_b["source_path"]).read_text(encoding="utf-8")
        self.assertIn(str((worktree_a / "include").resolve()), flags_a)
        self.assertNotIn(str(worktree_b.resolve()), flags_a)
        self.assertIn(str((worktree_b / "include").resolve()), flags_b)
        self.assertNotIn(str(worktree_a.resolve()), flags_b)

    async def test_required_project_context_rejects_unprepared_lsp_session(self) -> None:
        workspace = self.root / "unprepared_cpp"
        workspace.mkdir()
        state = LspServerState(
            file_config=LspServerFileConfig(
                config=LspServerConfig(
                    server_id="clangd",
                    command=(sys.executable,),
                    extensions=(".cpp",),
                    language_ids=("cpp",),
                ),
                source="test",
                config_path=str(self.root / "clangd.toml"),
            ),
            config_path=self.root / "clangd.toml",
        )
        self.manager.states = {state.server_id: state}

        self.manager.workspace_environments[str(workspace.resolve())] = {
            "workspace_root": str(workspace.resolve()),
            "primary_language": "cpp",
            "languages": ["cpp"],
            "setup": {
                "require_project_context": True,
                "languages": ["cpp"],
                "project_contexts": {
                    "clangd": {
                        "status": "unavailable",
                        "reason": "missing_compile_context",
                    }
                },
            },
        }
        result = await self.manager.run_lsp_operation(
            "workspace_symbols",
            {
                "server_id": "clangd",
                "workspace_root": str(workspace),
                "query": "value",
            },
        )

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(
            result["reason"],
            "project_context_unavailable:missing_compile_context",
        )
        self.assertFalse(state.attached)

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

        released = await self.manager.release_workspace(
            {"workspace_root": str(workspace_a)}
        )

        self.assertEqual(released["released_count"], 1)
        self.assertEqual(released["released"][0]["server_id"], "fake_python")
        self.assertEqual(closed, [workspace_a.resolve()])
        self.assertNotIn(str(workspace_a.resolve()), state.sessions)
        self.assertIn(str(workspace_b.resolve()), state.sessions)
        self.assertTrue(state.attached)
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
        self.assertIn("attach_failed_after_retry:TimeoutError", first["reason"])
        self.assertEqual(second["status"], "unavailable")
        self.assertIn("recent_attach_failure:TimeoutError", second["reason"])
        self.assertEqual(len(attempts), 2)

    async def test_protocol_failure_restarts_workspace_session_once(self) -> None:
        workspace = self.root / "restartable"
        workspace.mkdir()
        initialized: list[int] = []
        closed: list[int] = []
        instances: list[object] = []

        class RestartingConnector:
            def __init__(self, config: LspServerConfig, *, workspace_root: Path) -> None:
                _ = config
                self.workspace_root = workspace_root
                self.index = len(instances)
                self.healthy = True
                instances.append(self)

            async def initialize(self) -> None:
                initialized.append(self.index)

            async def request(self, method: str, params: dict) -> dict:
                _ = method, params
                if self.index == 0:
                    self.healthy = False
                    raise LspProtocolError("server stdout closed")
                return {"value": [{"name": "route"}]}

            async def close(self) -> None:
                closed.append(self.index)

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

        with patch("pal.lsp.manager.AsyncLspConnector", RestartingConnector):
            result = await self.manager.run_lsp_operation(
                "workspace_symbols",
                {
                    "server_id": "fake_python",
                    "workspace_root": str(workspace),
                    "query": "route",
                },
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(initialized, [0, 1])
        self.assertEqual(closed, [0])

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

    def test_prewarm_plan_uses_workspace_languages(self) -> None:
        workspace = {
            "repo_path": str(self.root),
            "primary_language": "python",
            "languages": ["python"],
        }

        plan = lsp_prewarm_plan(workspace)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.workspace_root, self.root.resolve())
        self.assertEqual(plan.primary_language, "python")
        self.assertEqual(plan.languages, ("python",))

    def test_prewarm_calls_manager_workspace_preparation_once(self) -> None:
        calls: list[dict] = []

        class FakeClient:
            def __init__(self, *, runtime_root: Path, request_timeout_seconds: float) -> None:
                self.runtime_root = runtime_root
                self.request_timeout_seconds = request_timeout_seconds

            def prepare_workspace_sync(self, params: dict) -> dict:
                calls.append(dict(params))
                return {"status": "ok", "servers": [{"server_id": "pyright", "status": "ok"}]}

        result = prewarm_workspace_lsp(
            runtime_root=self.root,
            workspace={
                "repo_path": str(self.root),
                "primary_language": "python",
                "languages": ["python"],
            },
            client_factory=FakeClient,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["workspace_root"], str(self.root.resolve()))
        self.assertEqual(calls[0]["primary_language"], "python")
        self.assertEqual(calls[0]["languages"], ["python"])
        self.assertTrue(calls[0]["prewarm"])

    def test_default_prewarm_uses_background_lsp_rpc_timeout(self) -> None:
        observed_timeouts = []

        class FakeClient:
            def __init__(self, *, runtime_root: Path, request_timeout_seconds: float = 180.0) -> None:
                self.runtime_root = runtime_root
                self.request_timeout_seconds = request_timeout_seconds

            def prepare_workspace_sync(self, params: dict) -> dict:
                _ = params
                observed_timeouts.append(self.request_timeout_seconds)
                return {"status": "ok", "servers": []}

        result = prewarm_workspace_lsp(
            runtime_root=self.root,
            workspace={
                "repo_path": str(self.root),
                "primary_language": "python",
                "languages": ["python"],
            },
            client_factory=FakeClient,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(observed_timeouts, [DEFAULT_LSP_PREWARM_TIMEOUT_SECONDS])

    def test_prewarm_timeout_can_be_overridden_by_workspace(self) -> None:
        observed_timeouts = []

        class FakeClient:
            def __init__(self, *, runtime_root: Path, request_timeout_seconds: float = 180.0) -> None:
                self.runtime_root = runtime_root
                self.request_timeout_seconds = request_timeout_seconds

            def prepare_workspace_sync(self, params: dict) -> dict:
                _ = params
                observed_timeouts.append(self.request_timeout_seconds)
                return {"status": "ok", "servers": []}

        result = prewarm_workspace_lsp(
            runtime_root=self.root,
            workspace={
                "repo_path": str(self.root),
                "primary_language": "python",
                "languages": ["python"],
                "lsp_prewarm_timeout_seconds": 3,
            },
            client_factory=FakeClient,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(observed_timeouts, [3.0])
