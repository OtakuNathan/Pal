from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from pal.minion.runner import MinionRunner
from pal.shared import MinionInvocationPack


async def _run(runtime_root: Path, pack_path: Path, minion_id: str, run_id: str) -> int:
    payload = json.loads(pack_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("worker pack must be a JSON object")
    pack = MinionInvocationPack.from_dict(payload)

    async def write_event(event: dict[str, Any]) -> None:
        print(json.dumps({"kind": "event", "event": event}, ensure_ascii=False), flush=True)

    decisions: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def read_stdin() -> None:
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        loop = asyncio.get_running_loop()
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        while True:
            line = await reader.readline()
            if not line:
                return
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(message, dict):
                await decisions.put(message)

    stdin_task = asyncio.create_task(read_stdin(), name=f"minion-v2-stdin-{run_id}")

    async def read_decision(timeout: float | None = None) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(decisions.get(), timeout=max(0.001, float(timeout or 0.001)))
        except TimeoutError:
            return None

    runner = MinionRunner(
        runtime_root=runtime_root,
        pack=pack,
        minion_id=minion_id,
        run_id=run_id,
        write_event=write_event,
        read_decision=read_decision,
    )
    try:
        return await runner.run()
    finally:
        stdin_task.cancel()
        try:
            await stdin_task
        except asyncio.CancelledError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one isolated Minion V2 worker invocation.")
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--pack-json", required=True)
    parser.add_argument("--minion-id", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    try:
        return asyncio.run(
            _run(
                Path(args.runtime_root),
                Path(args.pack_json),
                str(args.minion_id),
                str(args.run_id),
            )
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "kind": "worker_error",
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
