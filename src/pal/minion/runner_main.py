from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import select
import sys
from pathlib import Path
from typing import Any

_PROTOCOL_STDOUT = getattr(sys.stdout, "buffer", sys.stdout)
_STDOUT_REDIRECT: Any | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pal-minion-runner")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--task-json", required=True)
    parser.add_argument("--minion-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--manager-liveness-fd", type=int, default=-1)
    return parser


async def _write(payload: dict[str, Any]) -> None:
    from pal.foundation.sidecar import pack_sidecar_message

    writer = _PROTOCOL_STDOUT
    writer.write(pack_sidecar_message(payload))
    writer.flush()


async def _read_decision(timeout: float | None = None) -> dict[str, Any] | None:
    from pal.foundation.sidecar import read_sidecar_message_sync

    try:
        if timeout is None:
            return await asyncio.to_thread(read_sidecar_message_sync, sys.stdin.buffer)
        if not await asyncio.to_thread(_stdin_readable, timeout):
            return None
        return read_sidecar_message_sync(sys.stdin.buffer)
    except (asyncio.TimeoutError, EOFError, ValueError):
        return None


def _stdin_readable(timeout: float | None) -> bool:
    try:
        fd = sys.stdin.buffer.fileno()
    except Exception:
        return True
    wait_seconds = max(0.0, float(timeout or 0.0))
    readable, _, _ = select.select([fd], [], [], wait_seconds)
    return bool(readable)


async def amain(args: argparse.Namespace) -> int:
    _redirect_stdout_to_stderr()
    from pal.minion.runner import MinionRunner
    from pal.shared import TaskContextPack

    pack = TaskContextPack.from_json(args.task_json)
    runner = MinionRunner(
        runtime_root=args.runtime_root,
        pack=pack,
        minion_id=args.minion_id,
        run_id=args.run_id,
        write_event=_write,
        read_decision=_read_decision,
    )
    if int(args.manager_liveness_fd or -1) < 0:
        return await runner.run()
    runner_task = asyncio.create_task(runner.run(), name=f"minion-runner-{args.run_id}")
    liveness_task = asyncio.create_task(
        _watch_manager_liveness(int(args.manager_liveness_fd)),
        name=f"minion-manager-liveness-{args.run_id}",
    )
    done, pending = await asyncio.wait({runner_task, liveness_task}, return_when=asyncio.FIRST_COMPLETED)
    if liveness_task in done:
        runner_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner_task
        print("manager liveness pipe closed; minion runner exiting", file=sys.stderr, flush=True)
        return 2
    liveness_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await liveness_task
    _ = pending
    return await runner_task


async def _watch_manager_liveness(fd: int) -> None:
    loop = asyncio.get_running_loop()
    gone = loop.create_future()
    with contextlib.suppress(OSError):
        os.set_inheritable(fd, False)

    def on_ready() -> None:
        if gone.done():
            return
        try:
            data = os.read(fd, 1)
        except OSError as exc:
            gone.set_result(exc)
            return
        if data == b"":
            gone.set_result(None)

    loop.add_reader(fd, on_ready)
    try:
        await gone
    finally:
        with contextlib.suppress(Exception):
            loop.remove_reader(fd)
        with contextlib.suppress(OSError):
            os.close(fd)


def main() -> int:
    return asyncio.run(amain(build_parser().parse_args()))


def _redirect_stdout_to_stderr() -> None:
    global _STDOUT_REDIRECT
    if _STDOUT_REDIRECT is not None:
        return
    stderr = getattr(sys.stderr, "buffer", sys.stderr)
    if not hasattr(stderr, "fileno"):
        return
    encoding = getattr(sys.stderr, "encoding", None) or "utf-8"
    errors = getattr(sys.stderr, "errors", None) or "replace"
    try:
        _STDOUT_REDIRECT = open(stderr.fileno(), "w", buffering=1, encoding=encoding, errors=errors, closefd=False)
    except Exception:
        return
    sys.stdout = _STDOUT_REDIRECT


if __name__ == "__main__":
    raise SystemExit(main())
