from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from pal.foundation.service_logging import configure_process_logging
from pal.lsp.manager import LspManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pal-lsp-manager")
    parser.add_argument("--runtime-root", type=Path, required=True)
    return parser


def configure_logging(runtime_root: Path) -> None:
    _ = runtime_root
    configure_process_logging(component="pal.lsp.manager")


async def amain(runtime_root: Path) -> int:
    configure_logging(runtime_root)
    manager = LspManager(runtime_root=runtime_root)
    await manager.run()
    return 0


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(amain(args.runtime_root))


if __name__ == "__main__":
    raise SystemExit(main())
