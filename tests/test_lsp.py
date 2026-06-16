from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from pal.lsp.config import LspServerConfig, LspServerFileConfig, load_builtin_lsp_templates, load_lsp_server_file, lsp_config_root
from pal.lsp.manager import LspManager, LspServerState


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
        self.assertEqual(pyright.startup_timeout_ms, 60_000)
        self.assertEqual(pyright.diagnostics_timeout_ms, 10_000)
        clangd = next(item.config for item in templates if item.config.server_id == "clangd")
        self.assertEqual(clangd.command, ("clangd",))
        self.assertEqual(clangd.startup_timeout_ms, 30_000)
        self.assertEqual(clangd.diagnostics_timeout_ms, 10_000)

    def test_runtime_lsp_config_root_mirrors_minion_profile_layout(self) -> None:
        self.assertEqual(lsp_config_root(self.root), self.root / "plugins" / "lsp" / "servers")


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
