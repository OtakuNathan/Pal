#!/usr/bin/env python3
"""Run the explicitly classified TLC inventory; never count a skipped check as a pass."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "spec/tlc-suite.json"
GROUPS = ("core", "execution", "llm", "foundation", "channel", "bunshin", "projection")
SUCCESS = "Model checking completed. No error has been found."


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_inventory(root: Path = ROOT) -> dict:
    manifest = json.loads((root / "spec/tlc-suite.json").read_text())
    discovered = {str(p.relative_to(root)) for base in
                  (root / "spec", root / "docs/llm_projection_refactor/formal")
                  for p in base.rglob("*.cfg")}
    seen = set()
    for case in manifest["cases"]:
        config, module = case["config"], case["module"]
        if config in seen:
            raise ValueError(f"Duplicate TLC config: {config}")
        seen.add(config)
        if config not in discovered or not (root / module).is_file():
            raise ValueError(f"Missing config/module: {case}")
        if Path(config).parent != Path(module).parent:
            raise ValueError(f"Config and module must share a directory: {case}")
        if case["kind"] not in {"positive", "negative", "witness", "retired"}:
            raise ValueError(f"Unclassified case: {case}")
        if case["kind"] in {"negative", "witness"} and not re.fullmatch(r"\w+", case.get("invariant", "")):
            raise ValueError(f"Expected invariant is missing: {case}")
        if case["kind"] in {"negative", "witness"}:
            config_text = (root / config).read_text()
            declarations = {name for line in re.findall(
                r"^\s*INVARIANTS?\s+([^\n]+)", config_text, re.MULTILINE
            ) for name in line.split("\\*", 1)[0].split()}
            if case["invariant"] not in declarations:
                raise ValueError(f"Expected invariant must have an explicit INVARIANT declaration: {case}")
        if case["kind"] == "retired" and not case.get("reason"):
            raise ValueError(f"Retired case needs a reason: {case}")
        if group_for(case) not in GROUPS:
            raise ValueError(f"Case has no nightly group: {case}")
    if discovered != seen:
        raise ValueError(f"Unclassified TLC configs: {sorted(discovered - seen)}")
    modules = {str(p.relative_to(root)) for base in
               (root / "spec", root / "docs/llm_projection_refactor/formal")
               for p in base.rglob("*.tla")}
    support = manifest.get("support_modules", [])
    if any(not item.get("reason") for item in support):
        raise ValueError("Support modules need an explicit reason")
    covered = {case["module"] for case in manifest["cases"]} | {item["module"] for item in support}
    if modules != covered:
        raise ValueError(f"Unclassified/missing TLA modules: {sorted(modules ^ covered)}")
    return manifest


def group_for(case: dict) -> str:
    return "projection" if case["config"].startswith("docs/") else Path(case["config"]).parts[1]


def check_generated(group: str | None) -> list[str]:
    # Compare against production renderers; never regenerate stale files in CI.
    from pal.bunshin.formal import render_implementation_topology
    from pal.channel.formal import render_endpoint_hub_implementation_relation
    from pal.foundation.fd_lease_formal import render_fd_lease_implementation_topology

    renderers = {
        "bunshin": ("spec/bunshin/ImplementationTopology.tla", render_implementation_topology),
        "channel": ("spec/channel/EndpointHubImplementationReducer.tla", render_endpoint_hub_implementation_relation),
        "foundation": ("spec/foundation/FdLeaseImplementationTopology.tla", render_fd_lease_implementation_topology),
    }
    checked = []
    for name, (path, render) in renderers.items():
        if group is not None and group != name:
            continue
        if (ROOT / path).read_text() != render():
            raise ValueError(f"Generated implementation model is stale: {path}")
        checked.append(path)
    return checked


def ensure_jar(path: Path, tool: dict, *, download: bool = False) -> None:
    if not path.exists() and download:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=path.parent) as staging:
            candidate = Path(staging) / "tla2tools.jar"
            with urllib.request.urlopen(tool["url"], timeout=60) as response, candidate.open("wb") as out:
                shutil.copyfileobj(response, out)
            if sha256(candidate) != tool["sha256"]:
                raise ValueError("Downloaded TLC jar does not match the pinned SHA-256")
            candidate.replace(path)
    if not path.is_file():
        raise ValueError(f"TLC jar missing: {path}; use --fetch-only to download the pinned release")
    if sha256(path) != tool["sha256"]:
        raise ValueError(f"TLC jar SHA-256 mismatch: {path}")


def accepted(case: dict, returncode: int | None, output: str) -> bool:
    violations = re.findall(
        r"^Error: Invariant (\w+) is violated(?:\.| by the initial state:)$",
        output, re.MULTILINE,
    )
    if case["kind"] == "positive":
        completed = re.findall(
            r"^[\d,]+ states generated, ([\d,]+) distinct states found, 0 states left on queue\.$",
            output, re.MULTILINE,
        )
        # TLC also exits successfully when Init has no solutions. Such a
        # vacuous run must not be reported as checking the production model.
        return (returncode == 0 and SUCCESS in output and "Error:" not in output
                and "Finished in" in output and len(completed) == 1
                and int(completed[0].replace(",", "")) > 0)
    if case["kind"] in {"negative", "witness"}:
        # TLC exit 12 specifically means an invariant violation, not timeout,
        # parser error, OOM, deadlock or an unrelated Java exception.
        errors = re.findall(r"^Error:.*$", output, re.MULTILINE)
        allowed = {f"Error: Invariant {case['invariant']} is violated.",
                   f"Error: Invariant {case['invariant']} is violated by the initial state:",
                   "Error: The behavior up to this point is:"}
        return (returncode == 12 and violations == [case["invariant"]]
                and "Finished in" in output and all(error in allowed for error in errors))
    return False


def run_case(case: dict, *, jar: Path, out: Path, timeout: int, heap: str, workers: int) -> dict:
    name = f"{group_for(case)}-{Path(case['config']).stem}"
    case_dir = out / name
    case_dir.mkdir()
    config = ROOT / case["config"]
    module = ROOT / case["module"]
    inputs = [config, *sorted(module.parent.glob("*.tla"))]
    hashes = {}
    for path in inputs:
        shutil.copyfile(path, case_dir / path.name)
        hashes[str(path.relative_to(ROOT))] = sha256(case_dir / path.name)
    log = case_dir / "tlc.log"
    result = {**case, "status": "running", "input_sha256": hashes, "log": str(log)}
    result_path = case_dir / "result.json"
    result_path.write_text(json.dumps(result, indent=2) + "\n")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="pal-tlc-") as metadata:
        command = ["java", f"-Xmx{heap}", "-XX:+UseParallelGC", "-jar", str(jar),
                   "-workers", str(workers), "-seed", "1", "-metadir", metadata,
                   "-config", config.name, module.name]
        result["command"] = command
        try:
            with log.open("w") as stream:
                process = subprocess.run(command, cwd=case_dir, stdout=stream,
                                         stderr=subprocess.STDOUT, timeout=timeout, check=False)
            result["returncode"] = process.returncode
            result["status"] = "passed" if accepted(case, process.returncode, log.read_text()) else "failed"
        except subprocess.TimeoutExpired:
            result.update(status="timeout", returncode=None)
        except OSError as error:
            result.update(status="error", returncode=None, error=str(error))
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    result_path.write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jar", type=Path, default=Path(os.environ.get("TLA2TOOLS_JAR", "tla2tools.jar")))
    parser.add_argument("--fetch-only", action="store_true")
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--group", choices=GROUPS)
    parser.add_argument("--case", action="append", default=[], help="exact config path; repeat to rerun selected cases")
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--heap", default="1g")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--output", type=Path, default=ROOT / "test-logs/tlc")
    args = parser.parse_args()
    if args.shards < 1 or not 0 <= args.shard < args.shards or args.timeout < 1 or args.workers < 1:
        parser.error("shard, timeout and worker limits must be positive and valid")
    if not re.fullmatch(r"[1-9][0-9]*[mgMG]", args.heap):
        parser.error("heap must be a size such as 512m or 2g")
    manifest = load_inventory()
    if args.inventory_only:
        print(json.dumps({"cases": dict(Counter(c["kind"] for c in manifest["cases"])),
                          "generated_checked": check_generated(args.group)}, indent=2))
        return 0
    jar = args.jar.expanduser().resolve()
    ensure_jar(jar, manifest["tool"], download=args.fetch_only)
    if args.fetch_only:
        print(f"Verified {manifest['tool']['release']} {manifest['tool']['sha256']} at {jar}")
        return 0
    cases = [c for c in manifest["cases"] if args.group is None or group_for(c) == args.group]
    if args.case:
        available = {case["config"] for case in cases if case["kind"] != "retired"}
        if not set(args.case) <= available:
            parser.error("requested case is missing, retired or outside the selected group")
        cases = [case for case in cases if case["config"] in args.case]
    retired = [c for c in cases if c["kind"] == "retired"]
    selected = [c for c in cases if c["kind"] != "retired"][args.shard::args.shards]
    if not selected:
        parser.error("selection has no active cases")
    out = args.output.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out / "summary.json").exists():
        parser.error("output already contains a run; choose a fresh directory")
    summary = {"tool": manifest["tool"], "group": args.group, "shard": args.shard,
               "shards": args.shards, "selected": len(selected), "retired": retired, "results": []}
    summary["manifest_sha256"] = sha256(MANIFEST)
    summary["runner_sha256"] = sha256(Path(__file__))
    summary["revision"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    summary["dirty"] = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT))
    summary["java"] = subprocess.run(["java", "-version"], capture_output=True, text=True, check=True).stderr
    summary_path = out / "summary.json"
    def save():
        summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    save()
    try:
        summary["generated_checked"] = check_generated(args.group)
    except Exception as error:
        summary["error"] = str(error)
        save()
        raise
    for case in selected:
        result = run_case(case, jar=jar, out=out, timeout=args.timeout, heap=args.heap, workers=args.workers)
        summary["results"].append(result)
        save()
        print(f"{result['status']}: {case['kind']} {case['config']} ({result['elapsed_seconds']}s)", flush=True)
    summary["passed"] = all(r["status"] == "passed" for r in summary["results"])
    save()
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError) as error:
        print(f"TLC suite error: {error}", file=sys.stderr)
        sys.exit(2)
