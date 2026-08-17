#!/usr/bin/env python3
"""Run one stable CI-sized slice of Pal's test suite."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "tests"
BATCHES = ("core-a", "core-b", "bunshin-a", "bunshin-b")


def _test_files() -> tuple[list[Path], list[Path]]:
    top_level = sorted(TEST_ROOT.glob("test_*.py"))
    bunshin = [path for path in top_level if path.name.startswith("test_bunshin")]
    core = [path for path in top_level if path not in bunshin]
    nested = sorted(TEST_ROOT.glob("**/test_*.py"))
    nested = [path for path in nested if path.parent != TEST_ROOT]
    return core, [*bunshin, *nested]


def select_batch(name: str) -> list[Path]:
    core, bunshin = _test_files()
    family, parity = name.split("-", maxsplit=1)
    candidates = core if family == "core" else bunshin
    offset = 0 if parity == "a" else 1
    return candidates[offset::2]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", choices=BATCHES)
    args, pytest_args = parser.parse_known_args()
    selected = select_batch(args.batch)
    if not selected:
        parser.error(f"batch {args.batch!r} selected no tests")
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        *(str(path.relative_to(ROOT)) for path in selected),
        *pytest_args,
    ]
    print(f"Running {args.batch}: {len(selected)} test files", flush=True)
    return subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
