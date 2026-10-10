#!/usr/bin/env python3
"""Cache Python references and rank unreachable chains for manual review.

This is a conservative inventory, not a proof that a candidate can be deleted.
It never imports project code or executes tests. Reports and caches are local.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict, deque
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import tokenize
import tomllib


ROOT = Path(__file__).resolve().parents[1]
CACHE_VERSION = 2
SKIP = {".git", ".venv", ".venv312", "__pycache__", "test-logs", "build", "dist", "node_modules"}


def repository_files(root: Path) -> list[Path]:
    try:
        output = subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root, stderr=subprocess.DEVNULL,
        )
        paths = [root / name for name in output.decode().split("\0") if name]
    except (OSError, subprocess.CalledProcessError):
        paths = list(root.rglob("*"))
    return sorted({path for path in paths if path.is_file()
                   and not set(path.relative_to(root).parts) & SKIP})


def module_name(path: str) -> str:
    parts = list(Path(path).with_suffix("").parts)
    if "src" in parts:
        parts = parts[parts.index("src") + 1:]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def package_aliases(paths: list[Path], root: Path) -> dict[str, str]:
    aliases = {}
    for path in paths:
        if path.name == "pyproject.toml":
            with path.open("rb") as stream:
                config = tomllib.load(stream)
            mapping = config.get("tool", {}).get("setuptools", {}).get("package-dir", {})
            for package, directory in mapping.items():
                if package:
                    aliases[(path.parent / directory).resolve().relative_to(root).as_posix()] = package
    return aliases


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted(node.value)
        return prefix + "." + node.attr if prefix else ""
    return ""


class LocalBindings(ast.NodeVisitor):
    """Collect bindings without descending into child lexical scopes."""
    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.add(node.name)

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        pass

    def visit_Import(self, node: ast.Import) -> None:
        pass  # Imports have their own resolution table.

    visit_ImportFrom = visit_Import


class FactVisitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.module = module_name(path)
        self.scope = ""
        self.class_scope = ""
        self.definitions: list[dict] = []
        self.references: list[list] = []
        self.seen_references: set[tuple] = set()
        self.imports: list[list] = []
        self.bindings: dict[str, list[str]] = {}

    def facts(self, tree: ast.Module) -> dict:
        self.visit(tree)
        return {"path": self.path, "module": self.module, "definitions": self.definitions,
                "references": self.references, "imports": self.imports, "bindings": self.bindings}

    def _definition(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        parent, owner = self.scope, self.class_scope
        qualname = parent + "." + node.name if parent else node.name
        decorators = [dotted(item.func if isinstance(item, ast.Call) else item)
                      for item in node.decorator_list]
        bases = [dotted(item) for item in node.bases] if isinstance(node, ast.ClassDef) else []
        self.definitions.append({"qualname": qualname, "name": node.name,
                                 "line": node.lineno, "end": node.end_lineno,
                                 "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                                 "parent": parent, "owner": owner,
                                 "decorators": decorators, "bases": bases})
        # Defaults, decorators and bases execute in the enclosing scope.
        for item in node.decorator_list:
            self.visit(item)
        if isinstance(node, ast.ClassDef):
            for item in [*node.bases, *node.keywords]:
                self.visit(item)
            self.class_scope = qualname
        else:
            for item in [*node.args.defaults, *node.args.kw_defaults]:
                if item is not None:
                    self.visit(item)
            for item in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                         node.args.vararg, node.args.kwarg]:
                if item is not None and item.annotation is not None:
                    self.visit(item.annotation)
            if node.returns is not None:
                self.visit(node.returns)
        self.scope = qualname
        bindings = LocalBindings()
        for item in node.body:
            bindings.visit(item)
        if not isinstance(node, ast.ClassDef):
            bindings.names.update(item.arg for item in [*node.args.posonlyargs, *node.args.args,
                *node.args.kwonlyargs, node.args.vararg, node.args.kwarg] if item is not None)
        self.bindings[qualname] = sorted(bindings.names)
        for item in node.body:
            self.visit(item)
        self.scope, self.class_scope = parent, owner

    visit_FunctionDef = _definition
    visit_AsyncFunctionDef = _definition
    visit_ClassDef = _definition

    def visit_Import(self, node: ast.Import) -> None:
        for item in node.names:
            target = item.name if item.asname else item.name.split(".")[0]
            self.imports.append([self.scope, item.asname or target, target, item.name, node.lineno,
                                 bool(item.asname and item.asname == item.name)])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = node.module or ""
        if node.level:
            package = self.module if self.path.endswith("/__init__.py") else self.module.rpartition(".")[0]
            parts = package.split(".") if package else []
            base = ".".join([*parts[:len(parts) - node.level + 1], *([base] if base else [])])
        for item in node.names:
            target = base + "." + item.name if base else item.name
            self.imports.append([self.scope, item.asname or item.name, target, base, node.lineno,
                                 item.asname == item.name or item.name == "*"])

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._reference("name", node.id, node.lineno)

    def _reference(self, kind: str, value: str, line: int) -> None:
        key = (self.scope, kind, value, self.class_scope)
        if key not in self.seen_references:
            self.seen_references.add(key)
            self.references.append([self.scope, kind, value, line, self.class_scope])

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            self._reference("attribute", dotted(node) or node.attr, node.lineno)
        self.visit(node.value)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and len(node.value) <= 500:
            self._reference("string", node.value, node.lineno)


def cached_facts(root: Path, paths: list[Path], cache: Path) -> tuple[list[dict], dict]:
    cache.parent.mkdir(parents=True, exist_ok=True)
    parsed = reused = 0
    errors = []
    facts = []
    # The schema need not change when the analysis changes; version the key.
    with sqlite3.connect(cache) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS facts (key TEXT PRIMARY KEY, digest TEXT, payload TEXT)")
        active = set()
        for path in paths:
            relative = path.relative_to(root).as_posix()
            key = f"{root}:{CACHE_VERSION}:{relative}"
            active.add(key)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            row = connection.execute("SELECT digest, payload FROM facts WHERE key = ?", (key,)).fetchone()
            if row and row[0] == digest:
                fact = json.loads(row[1])
                reused += 1
            else:
                try:
                    with tokenize.open(path) as stream:
                        fact = FactVisitor(relative).facts(ast.parse(stream.read(), filename=relative))
                except (SyntaxError, UnicodeError, LookupError) as exc:
                    # A parse failure is visible; it must never imply that imports disappeared.
                    errors.append({"path": relative, "error": str(exc)})
                    continue
                connection.execute("INSERT OR REPLACE INTO facts VALUES (?, ?, ?)",
                                   (key, digest, json.dumps(fact, separators=(",", ":"))))
                parsed += 1
            facts.append(fact)
        prefix = f"{root}:"
        stale = [key for (key,) in connection.execute("SELECT key FROM facts")
                 if key.startswith(prefix) and key not in active]
        connection.executemany("DELETE FROM facts WHERE key = ?", [(key,) for key in stale])
    return facts, {"parsed": parsed, "reused": reused, "removed": len(stale), "errors": errors}


def reachable(roots, edges) -> set[str]:
    seen = set(roots)
    queue = deque(seen)
    while queue:
        for target in edges.get(queue.popleft(), ()):
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return seen


class ReferenceGraph:
    def __init__(self, facts: list[dict], package_names: dict[str, str] | None = None) -> None:
        self.facts = {fact["path"]: fact for fact in facts}
        self.aliases = {}
        for fact in facts:
            scopes = defaultdict(dict)
            for scope, alias, target, _, _, _ in fact["imports"]:
                scopes[scope][alias] = target
            self.aliases[fact["path"]] = scopes
        self.nodes: dict[str, dict] = {}
        self.modules: dict[str, list[str]] = defaultdict(list)
        self.symbols: dict[str, list[str]] = defaultdict(list)
        self.names: dict[str, list[str]] = defaultdict(list)
        self.edges: dict[str, set[str]] = defaultdict(set)
        self.exact_edges: dict[str, set[str]] = defaultdict(set)
        self.reverse: dict[str, set[str]] = defaultdict(set)
        self.evidence: dict[str, list[dict]] = defaultdict(list)
        self.runtime_roots: dict[str, set[str]] = defaultdict(set)
        self.test_roots: set[str] = set()
        self.module_dependencies: dict[str, set[str]] = defaultdict(set)
        self.edge_kinds = Counter()
        self.unresolved = Counter()
        self.unresolved_entries: list[str] = []
        for fact in facts:
            module_id = self.node_id(fact["path"], "")
            self.nodes[module_id] = {"path": fact["path"], "module": fact["module"],
                                     "qualname": "", "name": "<module>", "kind": "module", "line": 1}
            self.modules[fact["module"]].append(module_id)
            for definition in fact["definitions"]:
                node_id = self.node_id(fact["path"], definition["qualname"])
                self.nodes[node_id] = {**definition, "path": fact["path"], "module": fact["module"]}
                self.symbols[fact["module"] + "." + definition["qualname"]].append(node_id)
                self.names[definition["name"]].append(node_id)
        for fact in facts:
            for directory, package in (package_names or {}).items():
                prefix = directory.rstrip("/") + "/" if directory != "." else ""
                if fact["path"].startswith(prefix):
                    parts = list(Path(fact["path"][len(prefix):]).with_suffix("").parts)
                    if parts[-1] == "__init__":
                        parts.pop()
                    alias = ".".join([package, *parts])
                    self.modules[alias].append(self.node_id(fact["path"], ""))
                    for definition in fact["definitions"]:
                        self.symbols[alias + "." + definition["qualname"]].append(
                            self.node_id(fact["path"], definition["qualname"]))
        self._build()

    @staticmethod
    def node_id(path: str, scope: str) -> str:
        return path + ":" + (scope or "<module>")

    @lru_cache(maxsize=16384)
    def targets(self, qualified: str, trail: frozenset[str] = frozenset()) -> tuple[str, ...]:
        if qualified in trail:
            return ()
        direct = [*self.symbols.get(qualified, ()), *self.modules.get(qualified, ())]
        if direct:
            return tuple(direct)
        # Resolve package re-exports, including explicit compatibility aliases.
        parts = qualified.split(".")
        for boundary in range(len(parts) - 1, 0, -1):
            module = ".".join(parts[:boundary])
            module_ids = self.modules.get(module, ())
            suffix = qualified[len(module) + 1:]
            first, _, rest = suffix.partition(".")
            for module_id in module_ids:
                aliases = self.aliases[self.nodes[module_id]["path"]].get("", {})
                if first in aliases:
                    direct.extend(self.targets(aliases[first] + ("." + rest if rest else ""), trail | {qualified}))
        return tuple(direct)

    def add_edge(self, source: str, target: str, kind: str, line: int = 0) -> None:
        if kind != "attribute_name":
            self.exact_edges[source].add(target)
            a, b = self.nodes[source]["path"], self.nodes[target]["path"]
            if a != b:
                self.module_dependencies[a].add(b)
        if target in self.edges[source]:
            return
        self.edges[source].add(target)
        self.reverse[target].add(source)
        self.edge_kinds[kind] += 1
        if len(self.evidence[target]) < 8:
            self.evidence[target].append({"source": source, "kind": kind, "line": line})

    def root(self, node_id: str, reason: str) -> None:
        self.runtime_roots[node_id].add(reason)
        self.runtime_roots[self.node_id(self.nodes[node_id]["path"], "")].add(reason)

    def resolve(self, fact: dict, scope: str, value: str) -> tuple[str, ...]:
        first, _, rest = value.partition(".")
        current = scope
        while True:
            # Methods and nested classes do not close over a class namespace.
            # A bare helper() in C.helper must resolve in enclosing functions
            # or the module, rather than incorrectly becoming a self-call.
            if current != scope and current and self.nodes[
                self.node_id(fact["path"], current)
            ]["kind"] == "class":
                current = current.rpartition(".")[0]
                continue
            aliases = self.aliases[fact["path"]].get(current, {})
            if first in aliases:
                return self.targets(aliases[first] + ("." + rest if rest else ""))
            local = self.symbols.get(fact["module"] + "." + (current + "." if current else "") + value)
            if local:
                return tuple(local)
            if first in fact["bindings"].get(current, ()):
                return ()
            if not current:
                break
            current = current.rpartition(".")[0]
        return self.targets(value)

    def _build(self) -> None:
        for fact in self.facts.values():
            path = fact["path"]
            for scope, alias, target, imported_module, line, exported in fact["imports"]:
                source = self.node_id(path, scope)
                # Import executes a module, but an imported function is not thereby called.
                for imported in {imported_module, target}:
                    parts = imported.split(".")
                    for boundary in range(1, len(parts) + 1):
                        for destination in self.modules.get(".".join(parts[:boundary]), ()):
                            self.add_edge(source, destination, "module_import", line)
                if exported:
                    for destination in self.targets(target):
                        self.add_edge(source, destination, "explicit_export", line)
                    if alias == "*":
                        for destination in self.modules.get(imported_module, ()):
                            imported = self.facts[self.nodes[destination]["path"]]
                            for definition in imported["definitions"]:
                                if not definition["parent"] and not definition["name"].startswith("_"):
                                    self.add_edge(source, self.node_id(imported["path"], definition["qualname"]),
                                                  "wildcard_export", line)
            for scope, kind, value, line, owner in fact["references"]:
                source = self.node_id(path, scope)
                # A().method has an unknown receiver; the bare method spelling
                # must not resolve to a same-name function in the lexical scope.
                targets = self.resolve(fact, scope, value) if kind != "string" and not (
                    kind == "attribute" and "." not in value
                ) else []
                if value.startswith(("self.", "cls.")) and owner:
                    targets = self.symbols.get(fact["module"] + "." + owner + "." + value.split(".", 1)[1], [])
                if kind == "string":
                    targets = self.resolve(fact, scope, value)
                    targets = targets or self.names.get(value, [])
                    targets = [*targets, *self.targets(value.replace(":", "."))]
                    for module in re.findall(r"(?:python\S*\s+-m\s+)([\w.]+)", value):
                        targets.extend(self.modules.get(module, ()))
                edge_kind = kind
                if not targets and kind == "attribute":
                    # Unknown receiver: preserve every same-name method conservatively.
                    name = value.rsplit(".", 1)[-1]
                    matches = self.names.get(name, [])
                    targets = []
                    if matches:
                        # Share uncertain dispatch rather than duplicating N callers x M methods.
                        dispatch = "<attribute>:" + name
                        if dispatch not in self.nodes:
                            self.nodes[dispatch] = {"path": "<dynamic>", "name": name,
                                                    "qualname": name, "kind": "dynamic", "line": 0}
                            for target in matches:
                                self.add_edge(dispatch, target, "attribute_name")
                        targets = [dispatch]
                    edge_kind = "attribute_name"
                if not targets:
                    self.unresolved[kind] += 1
                for target in set(targets):
                    self.add_edge(source, target, edge_kind, line)
            for definition in fact["definitions"]:
                node_id = self.node_id(path, definition["qualname"])
                if path.startswith("tests/"):
                    self.test_roots.add(node_id)
                    continue
                if definition["name"].startswith("__"):
                    self.root(node_id, "Python protocol hook")
                if any(item.rsplit(".", 1)[-1] not in {"property", "classmethod", "staticmethod"}
                       for item in definition["decorators"]):
                    self.root(node_id, "decorator may register or replace definition")
                if definition["kind"] == "class":
                    for method in ("__init__", "__new__"):
                        for target in self.symbols.get(fact["module"] + "." + definition["qualname"] + "." + method, ()):
                            self.add_edge(node_id, target, "constructor")
                    if definition["bases"]:
                        # Overrides and external interface calls need semantic receiver typing.
                        prefix = definition["qualname"] + "."
                        for child in fact["definitions"]:
                            if child["parent"] == definition["qualname"]:
                                self.root(self.node_id(path, prefix + child["name"]), "inheritance/interface review")
            module_id = self.node_id(path, "")
            if path.startswith("tests/"):
                self.test_roots.add(module_id)
            if path.startswith("scripts/") or path.endswith("/__main__.py"):
                self.root(module_id, "script/module execution entry")
            for scope, kind, value, line, _ in fact["references"]:
                if kind == "name" and value == "__name__" and not scope:
                    self.root(module_id, "module has execution guard")

    def external_roots(self, paths: list[Path], root: Path, entries: list[str]) -> None:
        for path in paths:
            if path.name == "pyproject.toml":
                with path.open("rb") as stream:
                    config = tomllib.load(stream)
                entries.extend(config.get("project", {}).get("scripts", {}).values())
                entries.extend(config.get("project", {}).get("gui-scripts", {}).values())
                for group in config.get("project", {}).get("entry-points", {}).values():
                    entries.extend(group.values())
            elif path.name in {"plugin.toml", "provider.toml"}:
                with path.open("rb") as stream:
                    config = tomllib.load(stream)
                entry = config.get("entrypoint", "")
                if entry.endswith(".py"):
                    entry = module_name((path.parent / entry).relative_to(root).as_posix())
                targets = self.targets(entry.replace(":", "."))
                if not targets:
                    self.unresolved_entries.append(path.relative_to(root).as_posix() + ": " + entry)
                for target in targets:
                    self.root(target, "manifest: " + path.relative_to(root).as_posix())
                    if self.nodes[target]["kind"] == "module":
                        factory = "build_plugin" if path.name == "plugin.toml" else "build_channel_provider"
                        for node_id, node in self.nodes.items():
                            if node["path"] == self.nodes[target]["path"] and node["name"] == factory:
                                self.root(node_id, "manifest module convention")
        for entry in entries:
            targets = self.targets(entry.replace(":", "."))
            if not targets:
                self.unresolved_entries.append(entry)
            for target in targets:
                self.root(target, "configured entry: " + entry)

    def affected_tests(self, changed: list[str]) -> list[str]:
        reverse = defaultdict(set)
        for source, targets in self.module_dependencies.items():
            for target in targets:
                reverse[target].add(source)
        affected = reachable(changed, reverse)
        return sorted(path for path in affected if path.startswith("tests/") and Path(path).name.startswith("test_"))

    def report(self, *, limit: int, focus: list[str], changed: list[str]) -> dict:
        runtime = reachable(self.runtime_roots, self.edges)
        tests = reachable(self.test_roots, self.edges)
        candidates = {node_id for node_id, node in self.nodes.items()
                      if node["kind"] in {"class", "function"} and not node["path"].startswith("tests/")
                      and node_id not in runtime}
        selected = {node_id for node_id in candidates if not focus
                    or any(self.nodes[node_id]["path"].startswith(path) for path in focus)}
        rows = []
        for node_id in selected:
            node = self.nodes[node_id]
            rows.append({**node, "id": node_id,
                         "category": "test_only" if node_id in tests else "unreachable",
                         "incoming_references": len(self.reverse.get(node_id, ())),
                         "evidence": self.evidence.get(node_id, []),
                         "review_required": True})
        rows.sort(key=lambda item: (item["category"] != "test_only",
                                   item["incoming_references"] != 0,
                                   not item["name"].startswith("_"), item["path"], item["line"]))
        # Connected components retain callers and callees even when every member has references.
        links = defaultdict(set)
        for source in selected:
            for target in self.exact_edges.get(source, set()) & selected:
                links[source].add(target)
                links[target].add(source)
        groups = []
        remaining = set(selected)
        while remaining:
            first = min(remaining)
            members = reachable([first], links)
            remaining.difference_update(members)
            if len(members) > 1:
                groups.append({"members": sorted(members), "size": len(members),
                               "definition_lines": sum(self.nodes[item]["end"] - self.nodes[item]["line"] + 1 for item in members),
                               "category": "test_only" if members <= tests else "unreachable",
                               "review_required": True})
        groups.sort(key=lambda item: (-item["definition_lines"], item["members"][0]))
        return {"notice": "Candidates require manual review; dynamic Python reachability is incomplete. No deletion or tests executed.",
                "summary": {"files": len(self.facts), "definitions": sum(node["kind"] in {"class", "function"} for node in self.nodes.values()),
                            "runtime_reachable_definitions": sum(self.nodes[item]["kind"] in {"class", "function"} for item in runtime),
                            "candidate_count": len(candidates), "focused_candidate_count": len(rows),
                            "categories": dict(Counter(item["category"] for item in rows)),
                            "edge_kinds": dict(self.edge_kinds), "unresolved_references": dict(self.unresolved)},
                "candidates": rows[:limit], "chains": groups[:limit],
                "runtime_roots": {key: sorted(value) for key, value in sorted(self.runtime_roots.items())},
                "unresolved_entries": self.unresolved_entries,
                "priority_test_files": sorted(path for path, dependencies in self.module_dependencies.items()
                                              if path.startswith("tests/") and Path(path).name.startswith("test_")
                                              and dependencies.intersection(changed)),
                "affected_test_files": self.affected_tests(changed),
                "limitations": ["Unknown receivers use same-name matches, which may hide real dead code.",
                                "Unmodelled dynamic imports, metaprogramming and external consumers can create false candidates.",
                                "Inheritance, decorators, protocol hooks and explicit exports are preserved conservatively.",
                                "Affected tests are an import/reference estimate, not a guarantee of complete regression coverage."]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--entry", action="append", default=[], help="Additional module:symbol runtime entry")
    parser.add_argument("--focus", action="append", default=[], help="Limit candidates to a relative path prefix")
    parser.add_argument("--changed", action="append", default=[], help="Estimate affected test files for a relative path")
    parser.add_argument("--limit", type=int, default=100, help="Maximum definitions and chains to include")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be positive")
    started = time.perf_counter()
    root = args.root.resolve()
    output = args.output or root / "test-logs/dead-code-audit/report.json"
    cache = args.cache or root / "test-logs/dead-code-audit/cache.sqlite3"
    paths = repository_files(root)
    discovery_done = time.perf_counter()
    facts, cache_stats = cached_facts(root, [path for path in paths if path.suffix == ".py"], cache)
    parsing_done = time.perf_counter()
    graph = ReferenceGraph(facts, package_aliases(paths, root))
    graph.external_roots(paths, root, list(args.entry))
    graph_done = time.perf_counter()
    report = graph.report(limit=args.limit, focus=args.focus, changed=args.changed)
    report["timings_seconds"] = {"discovery": round(discovery_done - started, 3),
                                 "parse_or_cache": round(parsing_done - discovery_done, 3),
                                 "graph": round(graph_done - parsing_done, 3),
                                 "report": round(time.perf_counter() - graph_done, 3)}
    report["cache"] = cache_stats
    report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    report["root"] = str(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({**report["summary"], "cache": cache_stats,
                      "elapsed_seconds": report["elapsed_seconds"], "report": str(output)}, ensure_ascii=False))
    return 1 if cache_stats["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
