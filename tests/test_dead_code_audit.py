"""Exercise audit evidence, conservative entry points and incremental parsing."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import runpy

import pytest


AUDIT = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/audit_dead_code.py"))
FactVisitor = AUDIT["FactVisitor"]
ReferenceGraph = AUDIT["ReferenceGraph"]
cached_facts = AUDIT["cached_facts"]


def graph_for(sources, entries=("pkg.app:main",)):
    facts = [FactVisitor(path).facts(ast.parse(source)) for path, source in sources.items()]
    graph = ReferenceGraph(facts)
    graph.external_roots([], Path("."), list(entries))
    return graph


def report_for(graph, **kwargs):
    return graph.report(limit=100, focus=[], changed=[], **kwargs)


def test_finds_referenced_but_disconnected_cycle_and_test_only_chain():
    graph = graph_for({
        "src/pkg/app.py": "def main(): pass\ndef alpha(): beta()\ndef beta(): alpha()\ndef orphan(): pass\n",
        "tests/test_app.py": "from pkg.app import alpha\ndef test_old(): alpha()\n",
    })
    report = report_for(graph)
    candidates = {item["name"]: item for item in report["candidates"]}
    assert "main" not in candidates
    assert candidates["alpha"]["category"] == "test_only"
    assert candidates["beta"]["category"] == "test_only"
    assert candidates["orphan"]["category"] == "unreachable"
    assert candidates["alpha"]["incoming_references"] > 0
    assert any(set(group["members"]) == {"src/pkg/app.py:alpha", "src/pkg/app.py:beta"}
               for group in report["chains"])


def test_import_does_not_make_unused_imported_function_live():
    graph = graph_for({
        "src/pkg/app.py": "from pkg.lib import old\ndef main(): pass\n",
        "src/pkg/lib.py": "def old(): pass\n",
    })
    assert any(item["name"] == "old" for item in report_for(graph)["candidates"])


def test_relative_reexports_and_module_aliases_resolve_current_entry():
    graph = graph_for({
        "src/pkg/__init__.py": "from .lib import entry as entry\n",
        "src/pkg/app.py": "import pkg as alias\ndef main(): alias.entry()\n",
        "src/pkg/lib.py": "def entry(): helper()\ndef helper(): pass\n",
    })
    assert report_for(graph)["candidates"] == []


def test_wildcard_export_preserves_public_definitions():
    graph = graph_for({
        "src/pkg/app.py": "from pkg.lib import *\ndef main(): exported()\n",
        "src/pkg/lib.py": "def exported(): pass\ndef _private(): pass\n",
    })
    assert {item["name"] for item in report_for(graph)["candidates"]} == {"_private"}


def test_unresolved_entry_is_visible_in_report():
    graph = graph_for({"src/pkg/app.py": "def main(): pass\n"}, entries=("pkg.app:typo",))
    assert report_for(graph)["unresolved_entries"] == ["pkg.app:typo"]


def test_self_reference_does_not_confuse_same_named_methods():
    graph = graph_for({"src/pkg/app.py": """
class Used:
    def run(self): self._helper()
    def _helper(self): pass
class Unused:
    def _helper(self): pass
def main(): Used.run(None)
"""})
    candidates = {item["qualname"] for item in report_for(graph)["candidates"]}
    assert "Used.run" not in candidates
    assert "Used._helper" not in candidates
    assert "Unused._helper" in candidates


def test_registration_strings_decorators_exports_and_interfaces_are_preserved():
    graph = graph_for({"src/pkg/app.py": """
__all__ = ['exported']
def exported(): pass
@register
def registered(): pass
class ExternalInterface(Protocol):
    def callback(self): pass
class Handler:
    def hook(self): pass
def main(): getattr(Handler, 'hook')
"""})
    assert not {"exported", "registered", "callback", "hook"} & {
        item["name"] for item in report_for(graph)["candidates"]}
    assert graph.runtime_roots["src/pkg/app.py:registered"]
    assert graph.runtime_roots["src/pkg/app.py:ExternalInterface.callback"]


def test_manifest_convention_and_project_entry_points(tmp_path):
    manifest = tmp_path / "plugin.toml"
    manifest.write_text('entrypoint = "pkg.plugin"\n')
    project = tmp_path / "pyproject.toml"
    project.write_text('[project.scripts]\ncli = "pkg.app:main"\n')
    graph = graph_for({
        "src/pkg/app.py": "def main(): pass\n",
        "src/pkg/plugin.py": "def build_plugin(): helper()\ndef helper(): pass\n",
    }, entries=())
    graph.external_roots([manifest, project], tmp_path, [])
    assert report_for(graph)["candidates"] == []


def test_provider_manifest_loads_relative_python_entrypoint(tmp_path):
    provider = tmp_path / "providers" / "demo"
    provider.mkdir(parents=True)
    manifest = provider / "provider.toml"
    manifest.write_text('entrypoint = "runtime.py"\n')
    graph = graph_for({"providers/demo/runtime.py": "def build_channel_provider(): helper()\ndef helper(): pass\n"}, entries=())
    graph.external_roots([manifest], tmp_path, [])
    assert report_for(graph)["candidates"] == []


def test_installed_package_alias_resolves_back_to_repository_source():
    sources = {
        "src/pkg/app.py": "from installed_demo.api import entry\ndef main(): entry()\n",
        "providers/demo/api.py": "def entry(): helper()\ndef helper(): pass\n",
    }
    facts = [FactVisitor(path).facts(ast.parse(source)) for path, source in sources.items()]
    graph = ReferenceGraph(facts, {"providers/demo": "installed_demo"})
    graph.external_roots([], Path("."), ["pkg.app:main"])
    assert report_for(graph)["candidates"] == []


def test_estimates_tests_through_transitive_imports_without_running_them():
    graph = graph_for({
        "src/pkg/app.py": "from pkg.lib import helper\ndef main(): helper()\n",
        "src/pkg/lib.py": "def helper(): pass\n",
        "tests/test_app.py": "from pkg.app import main\ndef test_app(): main()\n",
        "tests/test_other.py": "def test_other(): pass\n",
    })
    assert graph.affected_tests(["src/pkg/lib.py"]) == ["tests/test_app.py"]
    report = graph.report(limit=100, focus=[], changed=["src/pkg/lib.py"])
    assert report["priority_test_files"] == []
    report = graph.report(limit=100, focus=[], changed=["src/pkg/app.py"])
    assert report["priority_test_files"] == ["tests/test_app.py"]


def test_unknown_receiver_is_conservative_and_labelled():
    graph = graph_for({"src/pkg/app.py": """
class Handler:
    def hook(self): pass
def main(receiver): receiver.hook()
"""})
    assert not any(item["name"] == "hook" for item in report_for(graph)["candidates"])
    assert any(item["kind"] == "attribute_name" for item in graph.evidence["src/pkg/app.py:Handler.hook"])


def test_chained_call_receiver_does_not_resolve_to_lexical_same_name_method():
    graph = graph_for({"src/pkg/app.py": """
class Manager:
    def inspect(self): helper()
class Facade:
    def inspect(self): self.manager().inspect()
def helper(): pass
def main(): Facade.inspect(None)
"""})
    candidates = {item["qualname"] for item in report_for(graph)["candidates"]}
    assert "Manager.inspect" not in candidates
    assert "helper" not in candidates


@pytest.mark.parametrize("helper_source", [
    "def helper(): leaf()\ndef leaf(): pass\n",
    "from pkg.lib import helper\n",
])
def test_bare_method_call_skips_class_namespace(helper_source):
    graph = graph_for({
        "src/pkg/app.py": helper_source + """
class Facade:
    def helper(self): helper()
def main(): Facade.helper(None)
""",
        "src/pkg/lib.py": "def helper(): leaf()\ndef leaf(): pass\n",
    })
    candidates = {item["id"] for item in report_for(graph)["candidates"]}
    helper_module = "app" if helper_source.startswith("def ") else "lib"
    assert f"src/pkg/{helper_module}.py:helper" not in candidates
    assert f"src/pkg/{helper_module}.py:leaf" not in candidates


def test_cache_reuses_unchanged_facts_invalidates_edits_and_prunes_deletions(tmp_path):
    first, second = tmp_path / "first.py", tmp_path / "second.py"
    first.write_text("def old(): pass\n")
    second.write_text("def retained(): pass\n")
    cache = tmp_path / "cache.sqlite3"
    _, cold = cached_facts(tmp_path, [first, second], cache)
    _, warm = cached_facts(tmp_path, [first, second], cache)
    assert (cold["parsed"], warm["parsed"], warm["reused"]) == (2, 0, 2)
    first.write_text("def replacement(): pass\n")
    facts, edited = cached_facts(tmp_path, [first, second], cache)
    assert (edited["parsed"], edited["reused"]) == (1, 1)
    assert facts[0]["definitions"][0]["name"] == "replacement"
    second.unlink()
    _, removed = cached_facts(tmp_path, [first], cache)
    assert removed["removed"] == 1


def test_parse_failure_is_reported_and_cli_exits_nonzero(tmp_path):
    (tmp_path / "broken.py").write_text("def broken(:\n")
    output = tmp_path / "report.json"
    assert AUDIT["main"](["--root", str(tmp_path), "--output", str(output)]) == 1
    report = json.loads(output.read_text())
    assert report["cache"]["errors"][0]["path"] == "broken.py"


def test_cache_namespaces_roots_with_identical_relative_paths(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "same.py").write_text("def a(): pass\n")
    (b / "same.py").write_text("def b(): pass\n")
    cache = tmp_path / "shared.sqlite3"
    cached_facts(a, [a / "same.py"], cache)
    facts, stats = cached_facts(b, [b / "same.py"], cache)
    assert stats["parsed"] == 1
    assert facts[0]["definitions"][0]["name"] == "b"


@pytest.mark.parametrize("limit", [0, -1])
def test_rejects_empty_report_limits(limit):
    with pytest.raises(SystemExit) as exc:
        AUDIT["main"](["--limit", str(limit)])
    assert exc.value.code == 2
