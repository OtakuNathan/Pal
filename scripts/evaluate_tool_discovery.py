#!/usr/bin/env python3
"""Offline discovery evaluation; never starts Pal or invokes business tools.

Queries are a small authored challenge set, not a production traffic estimate.
Labels follow the current consolidated surface; CLI-only configuration cases
are evaluated separately. Suggestions never count as strict recall.
Compare a supplied git revision's matcher on the SAME current source catalog to
isolate routing changes from the preceding unconditional vocabulary expansion.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import itertools
import json
import random
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.audit_tool_discovery import declared_tools


# One intended operation per positive query; alternatives are counted as noise.
POSITIVE = [
    ("llm endpoint current information", "inspect_llm"),
    ("current llm configuration", "inspect_llm"),
    ("llm active endpoint details", "inspect_llm"),
    ("llm token spending", "inspect_llm"),
    ("channel send file", "send_channel_attachment"),
    ("channel upload attachment", "send_channel_attachment"),
    ("send photo", "send_channel_attachment"),
    ("send document", "send_channel_attachment"),
    ("browser checkbox check", "set_browser_checked_state"),
    ("check radio browser", "set_browser_checked_state"),
    ("browser checkbox select", "set_browser_checked_state"),
    ("browser check health", "inspect_browser_status"),
    ("memory delete", "forget_memory"),
    ("memory erase", "forget_memory"),
    ("memory modify", "update_memory"),
    ("plugin refresh code", "manage_plugin"),
    ("plugin disconnect", "detach_plugin"),
    ("channel endpoint reconnect", "manage_channel_endpoint"),
    ("channel provider reload code", "manage_plugin"),
    ("read file contents", "read_file"),
    ("file apply patch", "edit_file"),
    ("directory remove", "run_shell"),
    ("shell execute", "run_shell"),
    ("browser page screenshot", "capture_browser_screenshot"),
    ("browser page text find", "find_browser_text"),
    ("browser previous page", "navigate_browser_history"),
    ("browser tabs enumerate", "manage_browser_tabs"),
    ("lsp symbol references", "find_lsp_references"),
    ("lsp diagnostic errors", "read_lsp_diagnostics"),
    ("artifact inspect file paths", "inspect_artifact_info"),
    ("artifact metadata inspect", "inspect_artifact_info"),
    ("scheduled task suspend", "manage_proactive_task"),
    ("scheduled task enable", "manage_proactive_task"),
    ("checklist finish step", "complete_checklist_step"),
    ("skill manual lookup", "search_skills"),
]

# Strict-hit rejection probes. Prefix typos may legitimately produce suggestions;
# negative_with_suggestions measures exposure, NOT a false-positive rate.
NEGATIVE = [
    "git check status", "database check status", "kubernetes check status",
    "git delete memory", "database delete memory", "do not delete memory",
    "browser check deployment", "browser check certificate",
    "browser delete checkbox", "browser check radio memory",
    "bunshin delete profile", "bunshin delete family",
    "bunshin clear profile", "bunshin remove family",
    "llm delete endpoint", "channel delete attachment",
    "memory send attachment", "artifact restart endpoint",
    "browser chec checkbox", "bunshin remo profile override",
]


def baseline_matcher(revision):
    from pal.execution import runtime

    source = subprocess.check_output(
        ["git", "show", f"{revision}:src/pal/execution/runtime.py"], text=True)
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "ExecutionRuntime")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                  and n.name == "_search_generation")
    method.decorator_list = []
    namespace = dict(vars(runtime))
    exec(compile(ast.Module(body=[method], type_ignores=[]), "<baseline matcher>", "exec"), namespace)
    return namespace[method.name]


def evaluate(search, generation):
    rows = []
    for query, expected in POSITIVE:
        result = search(generation, {"query": query})
        hits = [h["alias"] for h in result["hits"]]
        suggestions = [h["alias"] for h in result.get("suggestions", [])]
        broad = [h["alias"] for h in search(generation, {"query": query, "top_k": 5})["hits"]]
        rows.append(dict(query=query, expected=expected, hits=hits,
                         suggestions=suggestions, broad_recalled=expected in broad))
    negatives = []
    for query in NEGATIVE:
        result = search(generation, {"query": query})
        negatives.append(dict(query=query, hits=[h["alias"] for h in result["hits"]],
                              suggestions=result.get("suggestions", [])))
    return dict(metrics={
        "positive_queries": len(rows),
        "top1_correct": sum(r["hits"][:1] == [r["expected"]] for r in rows),
        "strict_recalled": sum(r["expected"] in r["hits"] for r in rows),
        "broad_recalled": sum(r["broad_recalled"] for r in rows),
        "suggestion_only_recalled": sum(not r["hits"] and r["expected"] in r["suggestions"] for r in rows),
        "returned_hits": sum(len(r["hits"]) for r in rows),
        "correct_returned_hits": sum(r["expected"] in r["hits"] for r in rows),
        "negative_queries": len(negatives),
        "negative_with_hits": sum(bool(r["hits"]) for r in negatives),
        "negative_with_suggestions": sum(bool(r["suggestions"]) for r in negatives),
    }, positives=rows, negatives=negatives)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="HEAD")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    catalog = declared_tools()
    from pal.execution import runtime

    records = {t.alias: dict(alias=t.alias, purpose=t.guidance.purpose,
                            search_objects=t.guidance.search_objects, search_terms=t.vocabulary,
                            search_term_rules=[r.model_dump() for r in t.guidance.search_term_rules],
                            search_scope_terms=t.guidance.search_scope_terms)
               for t in catalog if not t.role_scoped}
    generation = SimpleNamespace(search_records=records)
    assert all(expected in records for _, expected in POSITIVE)
    search = runtime.ExecutionRuntime._search_generation
    report = dict(baseline_ref=subprocess.check_output(
        ["git", "rev-parse", args.baseline_ref], text=True).strip(),
        resident_aliases=len(records), declarations=len(catalog),
        catalog_sha256=hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest(),
        limitations=["Authored challenge set, not sampled production traffic",
                     "Same current vocabulary for both matchers; not a historical end-to-end comparison",
                     "Source resident catalog only; excludes role-only and dynamic MCP tools",
                     "No model query generation, contract checking or business invocation evaluated",
                     "Negative suggestions include useful typo recovery; inspect individual cases"],
        baseline=evaluate(baseline_matcher(args.baseline_ref), generation),
        candidate=evaluate(search, generation))
    # Full output stability under repeated calls and shuffled registry insertion.
    queries = [q for q, _ in POSITIVE] + NEGATIVE + sorted(records)
    expected = {q: search(generation, {"query": q}) for q in queries}
    rng = random.Random(20261012)
    unstable = []
    for trial in range(10):
        items = list(records.items())
        rng.shuffle(items)
        shuffled = SimpleNamespace(search_records=dict(items))
        for q in queries:
            if search(shuffled, {"query": q}) != expected[q]:
                unstable.append(dict(trial=trial, query=q))
    variant_failures = []
    variant_count = 0
    for q, _ in POSITIVE:
        target = [h["alias"] for h in expected[q]["hits"]]
        variants = {q.upper(), q.replace(" ", "_"), q.replace(" ", "-"),
                    *[" ".join(p) for p in itertools.islice(itertools.permutations(q.split()), 6)]}
        for variant in sorted(variants):
            variant_count += 1
            hits = [h["alias"] for h in search(generation, {"query": variant})["hits"]]
            if hits != target:
                variant_failures.append(dict(query=q, variant=variant, hits=hits, expected=target))
    report["determinism"] = dict(registry_permutation_checks=10 * len(queries),
        failures=unstable, variant_checks=variant_count, variant_failures=variant_failures,
        exact_alias_top1=sum(expected[a]["hits"][0]["alias"] == a for a in records))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v["metrics"] for k, v in report.items()
                      if k in {"baseline", "candidate"}}, indent=2))
    print(json.dumps(report["determinism"], indent=2))


if __name__ == "__main__":
    main()
