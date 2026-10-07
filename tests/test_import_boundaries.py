"""Runtime imports, including deferred imports, must form a directed acyclic graph."""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "src"


class RuntimeImports(ast.NodeVisitor):
    def __init__(self, package: str) -> None:
        self.package = package
        self.targets: set[str] = set()

    def visit_If(self, node: ast.If) -> None:
        if (
            isinstance(node.test, ast.Constant) and not node.test.value
        ) or ast.unparse(node.test) in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}:
            for child in node.orelse:
                self.visit(child)
        else:
            self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        self.targets.update(alias.name for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = (
            importlib.util.resolve_name("." * node.level + (node.module or ""), self.package)
            if node.level else node.module or ""
        )
        self.targets.add(base)
        self.targets.update(base + "." + alias.name for alias in node.names)


def test_runtime_imports_have_no_cycles():
    # Explicit source imports only; dynamic import strings and implicit package
    # initialization are outside this check. TYPE_CHECKING/dead branches are not
    # execution dependencies. Function-local imports are execution dependencies.
    modules = {
        ".".join(path.relative_to(ROOT).with_suffix("").parts).removesuffix(".__init__"): path
        for path in (ROOT / "pal").rglob("*.py")
    }
    graph = {}
    for name, path in modules.items():
        visitor = RuntimeImports(name if path.name == "__init__.py" else name.rpartition(".")[0])
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        graph[name] = (visitor.targets & modules.keys()) - {name}
    visited: set[str] = set()
    active: list[str] = []

    def visit(name):
        assert name not in active, "Import cycle: " + " -> ".join([*active, name])
        if name in visited:
            return
        active.append(name)
        for target in sorted(graph[name]):
            visit(target)
        active.pop()
        visited.add(name)

    for name in sorted(graph):
        visit(name)


def test_moved_contracts_keep_compatibility_exports():
    from pal.llm import response_hooks, response_hook_contracts
    from pal.web_fetch import browser_service, runtime_paths
    from pal.plugins import host, paths
    from pal.bunshin import role_gateway, role_gateway_client, submission_drafts

    for name in ("ProviderResponseHookError", "ProviderResponseHookContext"):
        assert getattr(response_hooks, name) is getattr(response_hook_contracts, name)
    for name in ("BrowserRuntimePaths", "_installed_cli_version", "_chromium_installed"):
        assert getattr(browser_service, name) is getattr(runtime_paths, name)
    assert host._source_plugins_root is paths._source_plugins_root
    assert role_gateway.role_gateway_client_from_env is role_gateway_client.role_gateway_client_from_env
    assert role_gateway.RoleGatewayArtifactStore is role_gateway_client.RoleGatewayArtifactStore
    assert role_gateway.decode_remote_draft_snapshot is submission_drafts.decode_remote_draft_snapshot
