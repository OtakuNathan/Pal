from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from pal.bunshin.runner import BunshinRunner
from pal.bunshin.worker_events import EventWriter, run_worker_with_events
from pal.shared import BunshinInvocationPack


async def _read_control_message(
    messages: asyncio.Queue[dict[str, Any]],
    timeout: float | None,
) -> dict[str, Any] | None:
    if timeout is None:
        return await messages.get()
    timeout_seconds = float(timeout)
    if timeout_seconds <= 0:
        try:
            return messages.get_nowait()
        except asyncio.QueueEmpty:
            return None
    try:
        return await asyncio.wait_for(messages.get(), timeout=timeout_seconds)
    except TimeoutError:
        return None


async def _run(
    runtime_root: Path, pack_path: Path, bunshin_id: str, run_id: str,
    write_event: EventWriter,
) -> int:
    payload = json.loads(pack_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("worker pack must be a JSON object")
    pack = BunshinInvocationPack.from_dict(payload)

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

    stdin_task = asyncio.create_task(read_stdin(), name=f"bunshin-v2-stdin-{run_id}")

    async def read_decision(timeout: float | None = None) -> dict[str, Any] | None:
        return await _read_control_message(decisions, timeout)

    runner = BunshinRunner(
        runtime_root=runtime_root,
        pack=pack,
        bunshin_id=bunshin_id,
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
    parser = argparse.ArgumentParser(description="Run one isolated Bunshin worker invocation.")
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--pack-json", required=True)
    parser.add_argument("--bunshin-id", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    return asyncio.run(
        run_worker_with_events(
            lambda write_event: _run(
                Path(args.runtime_root),
                Path(args.pack_json),
                str(args.bunshin_id),
                str(args.run_id),
                write_event,
            )
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
