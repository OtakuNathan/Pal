"""Dynamic access is admitted at explicit boundaries, never added silently."""
from __future__ import annotations

import ast
from collections import Counter
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).with_name("fixtures") / "dynamic_access.json"


def inventory():
    found = Counter()

    class Visitor(ast.NodeVisitor):
        def __init__(self, path):
            self.path = path
            self.scope = []

        def visit_ClassDef(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_FunctionDef = visit_ClassDef
        visit_AsyncFunctionDef = visit_ClassDef

        def visit_Call(self, node):
            name = ast.unparse(node.func)
            if name in {"getattr", "hasattr", "setattr", "delattr", "inspect.signature"}:
                key = f"{self.path}:{'.'.join(self.scope)}:{ast.unparse(node)}"
                found[key] += 1
            self.generic_visit(node)

    for path in sorted((ROOT / "src" / "pal").rglob("*.py")):
        Visitor(path.relative_to(ROOT).as_posix()).visit(ast.parse(path.read_text()))
    return found


def test_no_unreviewed_dynamic_access():
    baseline = json.loads(BASELINE.read_text())
    allowed = Counter({key: entry["count"] for key, entry in baseline.items()})
    unexpected = inventory() - allowed
    assert not unexpected, "Declare the communication contract instead of probing it:\n" + "\n".join(unexpected)
    assert all(entry["reason"] for entry in baseline.values())
