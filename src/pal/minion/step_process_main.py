from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from pal.minion.step_process import run_step_process_payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a minion workflow step process.")
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--step-json-file", required=True)
    args = parser.parse_args(argv)

    try:
        payload = _load_step_json(Path(args.step_json_file))
        result = asyncio.run(run_step_process_payload(Path(args.runtime_root), payload))
    except Exception as exc:
        result = {
            "status": "failed",
            "reason": "step_process_exception",
            "error": f"{exc.__class__.__name__}: {exc}",
        }
        print(json.dumps(result, sort_keys=True), file=sys.stdout, flush=True)
        return 1
    print(json.dumps(result, sort_keys=True), file=sys.stdout, flush=True)
    return 0 if str(result.get("status") or "") in {"completed", "waiting_for_slot"} else 1


def _load_step_json(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("step process payload must be a JSON object")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
