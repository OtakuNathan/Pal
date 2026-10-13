#!/usr/bin/env python3
"""Count distinct authored aliases and export the module-by-module surface review.

Definition imports only: no runtime publication, provider instantiation, model
requests or business invocation. This is a source union, not a live inventory.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
import csv
import importlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.audit_tool_discovery import declared_tools
from scripts.benchmark_full_discovery_flow import _ast_value, build_flow_catalog


def build_inventory() -> dict:
    declarations = declared_tools()
    records, _, _ = build_flow_catalog()
    review = json.loads((ROOT / "docs/tool_surface_review.json").read_text(encoding="utf-8"))
    grouped = defaultdict(list)
    trees = {}
    for declaration in declarations:
        path = ROOT / declaration.source
        if declaration.role_scoped:
            native_opt_in = False
        else:
            if path not in trees:
                trees[path] = {node.lineno: node for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                               if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                               and node.func.id == "capability_action"}
            node = trees[path][declaration.line]
            fields = {key.arg: key.value for key in node.keywords}
            module = importlib.import_module(".".join(path.relative_to(ROOT / "src").with_suffix("").parts))
            metadata = _ast_value(fields["metadata"], module) if "metadata" in fields else {}
            native_opt_in = metadata.get("native_function") is True
        grouped[declaration.alias].append({
            "source": declaration.source, "line": declaration.line,
            "role_scoped": declaration.role_scoped,
            "generated_spec_key": declaration.generated_spec_key,
            "native_opt_in": native_opt_in,
        })

    if review["tools"].keys() - grouped.keys():
        raise ValueError("Review contains aliases absent from the source catalog")
    rows = []
    for alias, sources in sorted(grouped.items()):
        roles = {source["role_scoped"] for source in sources}
        if len(roles) != 1:
            raise ValueError(f"Alias shared between resident and role surfaces: {alias}")
        role = roles.pop()
        record = records[alias]
        module = record["module_id"]
        owner = ("memory" if module.startswith("l3.") else
                 {"web_fetch": "browser", "packages": "plugins"}.get(module, module))
        group = "bunshin_roles" if role else owner
        native = record["invocation_mode"] == "direct" or any(source["native_opt_in"] for source in sources)
        decision = review["tools"].get(alias, {
            "disposition": "keep", "reason": review["modules"][group]["reason"],
            "replacement": "", "prerequisite": "",
        })
        rows.append({
            "alias": alias, "module": group, "domain": record["domain"],
            "surface": "role_only" if role else "resident_union",
            "invocation_mode": record["invocation_mode"],
            "exposure": "native" if native else "search_only",
            "purpose": record["purpose"], "sources": sources, **decision,
        })
    modules = {}
    for group in sorted({row["module"] for row in rows}):
        selected = [row for row in rows if row["module"] == group]
        modules[group] = {
            "aliases": len(selected), "native": sum(row["exposure"] == "native" for row in selected),
            "search_only": sum(row["exposure"] == "search_only" for row in selected),
            "dispositions": dict(Counter(row["disposition"] for row in selected)),
            "review": review["modules"][group]["reason"],
        }
    resident = [row for row in rows if row["surface"] == "resident_union"]
    return {
        "scope": "authored_source_union", "review_date": review["date"],
        "limitations": ["Not a snapshot of a running Pal or a single role's permissions.",
                        "External MCP tools and installed third-party contributions are excluded.",
                        "Native counts assume all authored owners/targets are available; failure windows differ.",
                        f"{sum(len(sources) > 1 for sources in grouped.values())} aliases have multiple provider declarations; each alias counts once.",
                        "Source consolidation is implemented; a running Pal has not been activated by this audit."],
        "counts": {"declarations": len(declarations), "distinct_aliases": len(rows),
                   "resident_union": len(resident), "role_only_union": len(rows) - len(resident),
                   "resident_direct": sum(row["invocation_mode"] == "direct" for row in resident),
                   "resident_native_opt_ins": sum(row["invocation_mode"] != "direct" and row["exposure"] == "native" for row in resident),
                   "resident_native": sum(row["exposure"] == "native" for row in resident),
                   "resident_search_only": sum(row["exposure"] == "search_only" for row in resident)},
        "duplicate_declarations": {alias: sources for alias, sources in sorted(grouped.items()) if len(sources) > 1},
        "retired_tools": review.get("retired_tools", {}), "modules": modules, "tools": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--csv", type=Path)
    args = parser.parse_args()
    report = build_inventory()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        fields = ["module", "alias", "surface", "exposure", "invocation_mode", "domain",
                  "purpose", "disposition", "reason", "replacement", "prerequisite", "sources"]
        with args.csv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in report["tools"]:
                writer.writerow({**row, "sources": json.dumps(row["sources"], ensure_ascii=False)})
    print(json.dumps(report["counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
