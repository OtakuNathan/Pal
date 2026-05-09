from __future__ import annotations

import argparse
import asyncio
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
    return parser


async def _write(payload: dict[str, Any]) -> None:
    from pal.foundation.sidecar import pack_sidecar_message

    writer = _PROTOCOL_STDOUT
    writer.write(pack_sidecar_message(payload))
    writer.flush()


async def _read_decision(timeout: float | None = None) -> dict[str, Any] | None:
    from pal.foundation.sidecar import read_sidecar_message_sync

    try:
        pending = asyncio.to_thread(read_sidecar_message_sync, sys.stdin.buffer)
        if timeout is None:
            return await pending
        return await asyncio.wait_for(pending, timeout=timeout)
    except (asyncio.TimeoutError, EOFError, ValueError):
        return None


async def amain(args: argparse.Namespace) -> int:
    _redirect_stdout_to_stderr()
    from pal.minion.runner import MinionRunner
    from pal.shared import TaskContextPack

    pack = TaskContextPack.from_json(args.task_json)
    return await MinionRunner(
        runtime_root=args.runtime_root,
        pack=pack,
        minion_id=args.minion_id,
        run_id=args.run_id,
        write_event=_write,
        read_decision=_read_decision,
    ).run()


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
