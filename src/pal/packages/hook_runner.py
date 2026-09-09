"""Standalone stdlib-only runner, also executable inside a clean plugin venv."""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file")
    parser.add_argument("--module")
    parser.add_argument("--source-root")
    parser.add_argument("--stage", choices=("check", "prepare", "verify"), required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    if args.source_root:
        sys.path.insert(0, args.source_root)
    if args.file:
        spec = importlib.util.spec_from_file_location("_pal_install_hook", args.file)
        if spec is None or spec.loader is None:
            raise RuntimeError("Cannot load installation hook")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(args.module)
    hook = getattr(module, args.stage, None)
    context = json.loads(Path(args.context).read_text())
    result = hook(context) if hook else {"ok": True, "skipped": True}
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise TypeError("Installation hooks must return an object containing boolean ok")
    Path(args.result).write_text(json.dumps(result), encoding="utf-8")
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
