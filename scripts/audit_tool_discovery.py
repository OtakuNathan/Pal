#!/usr/bin/env python3
"""Export authored tool discovery contracts without starting Pal or invoking tools.

Includes built-in/provider declarations and Bunshin role tool dictionaries.
External MCP tools are dynamic; their reviewed guidance overrides live in the
runtime's MCP configuration and are not represented by this source catalog.
"""
from __future__ import annotations

import argparse
import ast
import csv
import importlib
import json
from dataclasses import dataclass
from pathlib import Path

from pal.execution.discovery_terms import discovery_vocabulary, validate_search_rules
from pal.execution.tool_facade import EmptyToolInput, ToolGuidance


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class DeclaredTool:
    alias: str
    source: str
    line: int
    guidance: ToolGuidance
    vocabulary: tuple[str, ...]
    role_scoped: bool
    module_id: str = ""
    namespace: str = ""
    family: str = ""
    generated_spec_key: str = ""


def declared_tools() -> list[DeclaredTool]:
    """Read actual guidance/model definitions, including selected enum values.

    Import definition modules only; never instantiate providers or publish a
    runtime. Keep duplicate provider aliases as separate catalog entries.
    """
    rows = []
    for path in sorted((ROOT / "src" / "pal").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        declarations = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "capability_action"):
                fields = {key.arg: key.value for key in node.keywords}
                if "aliases" not in fields or "guidance" not in fields:
                    continue
                try:
                    aliases = ast.literal_eval(fields["aliases"])
                except (ValueError, TypeError):
                    continue  # Runtime-generated declarations, such as MCP tools.
                declarations.append((node, aliases, fields))
            elif isinstance(node, ast.Dict):
                fields = {key.value: value for key, value in zip(node.keys, node.values)
                          if isinstance(key, ast.Constant)}
                if (isinstance(fields.get("alias"), ast.Constant)
                        and isinstance(fields["alias"].value, str)
                        and isinstance(fields.get("guidance"), ast.Dict)):
                    declarations.append((node, (ast.literal_eval(fields["alias"]),), fields))
        if not declarations:
            continue
        module_name = ".".join(path.relative_to(ROOT / "src").with_suffix("").parts)
        module = importlib.import_module(module_name)

        def value(node):
            if isinstance(node, ast.Name):
                return getattr(module, node.id)
            return ast.literal_eval(node)

        for node, aliases, fields in declarations:
            authored = fields["guidance"]
            if isinstance(authored, ast.Name):
                guidance = value(authored)
            elif isinstance(authored, ast.Call):
                guidance = ToolGuidance(**{
                    key.arg: value(key.value) for key in authored.keywords
                    if key.arg != "next_tool_hints"
                })
            else:
                guidance = ToolGuidance.model_validate(ast.literal_eval(authored))
            model = value(fields["InputModel"]) if "InputModel" in fields else EmptyToolInput
            vocabulary = discovery_vocabulary(guidance, model.model_json_schema())
            for alias in aliases:
                validate_search_rules(alias, guidance, vocabulary)
                rows.append(DeclaredTool(alias, str(path.relative_to(ROOT)), node.lineno,
                                         guidance, vocabulary, isinstance(node, ast.Dict),
                                         ("l3." + path.stem if path.parent.name == "l3"
                                          else path.relative_to(ROOT / "src" / "pal").parts[0]),
                                         value(fields["namespace"]) if "namespace" in fields else "operation",
                                         value(fields["family"]) if "family" in fields else ""))
    # Emotion's schema is generated from renderer declarations. Its base
    # contract is safe to read without loading devices; live enums are covered
    # by the registry lifecycle tests rather than an installed-plugin snapshot.
    from pal.emotion.runtime import EMOTION_ALIASES, emotion_contract
    model, guidance = emotion_contract()
    dynamic_path = ROOT / "src/pal/emotion/runtime.py"
    dynamic_line = next(node.lineno for node in ast.walk(ast.parse(dynamic_path.read_text()))
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "capability_action")
    for alias in EMOTION_ALIASES:
        vocabulary = discovery_vocabulary(guidance, model.model_json_schema())
        validate_search_rules(alias, guidance, vocabulary)
        rows.append(DeclaredTool(alias, "src/pal/emotion/runtime.py", dynamic_line,
                                 guidance, vocabulary, False, "emotion", "operation"))
    # Literal AST dictionaries miss aliases expanded by comprehensions. The
    # assembled worker catalog is the authority for those role-only entries.
    from pal.bunshin.scoped_execution import _WORKSPACE_TOOL_SPECS

    known_roles = {row.alias for row in rows if row.role_scoped}
    role_aliases = {spec["alias"] for spec in _WORKSPACE_TOOL_SPECS.values()}
    if len(role_aliases) != len(_WORKSPACE_TOOL_SPECS):
        raise ValueError("Duplicate alias in assembled workspace tool catalog")
    if known_roles - role_aliases:
        raise ValueError(f"Unregistered role declarations: {sorted(known_roles - role_aliases)}")
    source = ROOT / "src/pal/bunshin/scoped_execution.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    catalog_line = next(node.lineno for node in tree.body
                        if isinstance(node, ast.AnnAssign)
                        and isinstance(node.target, ast.Name)
                        and node.target.id == "_WORKSPACE_TOOL_SPECS")
    for key, spec in _WORKSPACE_TOOL_SPECS.items():
        alias = spec["alias"]
        if alias in known_roles:
            continue
        guidance = ToolGuidance.model_validate(spec["guidance"])
        vocabulary = discovery_vocabulary(guidance, spec["InputModel"].model_json_schema())
        validate_search_rules(alias, guidance, vocabulary)
        rows.append(DeclaredTool(alias, str(source.relative_to(ROOT)), catalog_line,
                                 guidance, vocabulary, True, "bunshin", "operation",
                                 "workflow", key))
    return sorted(rows, key=lambda row: (row.alias, row.source, row.line))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Destination CSV file.")
    args = parser.parse_args()
    rows = declared_tools()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("alias", "source", "line", "role_scoped", "generated_spec_key", "search_domain", "search_domain_aliases", "search_domain_description", "purpose", "use_when", "do_not_use_when", "failure_next_steps",
                         "search_objects", "search_terms", "search_intents",
                         "search_enum_fields", "effective_vocabulary", "search_term_rules", "search_scope_terms", "next_tool_hints"))
        for row in rows:
            writer.writerow((row.alias, row.source, row.line, row.role_scoped, row.generated_spec_key,
                             row.guidance.search_domain, " | ".join(row.guidance.search_domain_aliases),
                             row.guidance.search_domain_description, row.guidance.purpose,
                             row.guidance.use_when, row.guidance.do_not_use_when, row.guidance.failure_next_steps,
                             " ".join(row.guidance.search_objects), " ".join(row.guidance.search_terms),
                             json.dumps(row.guidance.search_intents, ensure_ascii=False),
                             " ".join(row.guidance.search_enum_fields), " ".join(row.vocabulary),
                             json.dumps([rule.model_dump(mode="json") for rule in row.guidance.search_term_rules]),
                             " ".join(row.guidance.search_scope_terms),
                             json.dumps([hint.model_dump(mode="json") for hint in row.guidance.next_tool_hints], ensure_ascii=False)))
    print(f"Exported {len(rows)} declarations ({len({row.alias for row in rows})} distinct aliases) to {args.output}")


if __name__ == "__main__":
    main()
