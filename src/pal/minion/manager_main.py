from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from pal.minion.ipc import minion_log_path
from pal.minion.manager import MinionManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pal-minion-manager")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--max-parallel-modules", type=int, default=None)
    return parser


def configure_logging(runtime_root: Path) -> None:
    log_path = minion_log_path(runtime_root)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_path),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


async def amain(runtime_root: Path, *, max_parallel_modules: int | None = None) -> int:
    configure_logging(runtime_root)
    manager = MinionManager(runtime_root=runtime_root, max_parallel_modules=max_parallel_modules)
    await manager.run()
    return 0


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(amain(args.runtime_root, max_parallel_modules=args.max_parallel_modules))


if __name__ == "__main__":
    raise SystemExit(main())
