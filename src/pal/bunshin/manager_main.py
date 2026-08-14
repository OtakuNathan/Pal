from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from pal.foundation import PalV2Database
from pal.foundation.service_logging import configure_process_logging
from pal.llm.models import LLMEndpointModel, PalRuntimeSettingModel
from pal.bunshin.ipc import BUNSHIN_RUNTIME_DB_PATH_ENV
from pal.bunshin.manager import BunshinManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pal-bunshin-manager")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--runtime-db-path", type=Path, default=None)
    parser.add_argument("--max-parallel-llm-nodes", type=int, default=None)
    parser.add_argument("--max-parallel-modules", type=int, default=None)
    return parser


def configure_logging(runtime_root: Path) -> None:
    _ = runtime_root
    configure_process_logging(component="pal.bunshin.manager")


async def amain(
    runtime_root: Path,
    *,
    runtime_db_path: Path | None = None,
    max_parallel_modules: int | None = None,
) -> int:
    configure_logging(runtime_root)
    database = _open_runtime_database(runtime_root, runtime_db_path)
    try:
        manager = BunshinManager(
            runtime_root=runtime_root,
            runtime_db_path=runtime_db_path,
            max_parallel_modules=max_parallel_modules,
        )
        await manager.run()
    finally:
        if database is not None:
            database.close()
    return 0


def _open_runtime_database(
    runtime_root: Path,
    runtime_db_path: Path | None = None,
) -> PalV2Database | None:
    """Bind Manager-owned endpoint reads to Pal's existing runtime database."""

    configured_env = str(os.environ.get(BUNSHIN_RUNTIME_DB_PATH_ENV) or "").strip()
    explicit_path = runtime_db_path is not None or bool(configured_env)
    resolved_path = Path(
        runtime_db_path
        or configured_env
        or Path(runtime_root) / "pal.sqlite3"
    )
    if not resolved_path.is_file() and not explicit_path:
        # Standalone lifecycle/status managers do not need LLM authority.
        # Provider calls remain unavailable until Pal supplies its canonical
        # runtime database path.
        return None
    database = PalV2Database(
        db_path=resolved_path,
        read_only=True,
    )
    database.initialize((LLMEndpointModel, PalRuntimeSettingModel))
    return database


def main() -> int:
    args = build_parser().parse_args()
    max_parallel = args.max_parallel_llm_nodes if args.max_parallel_llm_nodes is not None else args.max_parallel_modules
    runtime_db_path = args.runtime_db_path
    if runtime_db_path is None:
        configured_path = str(os.environ.get(BUNSHIN_RUNTIME_DB_PATH_ENV) or "").strip()
        runtime_db_path = Path(configured_path) if configured_path else None
    return asyncio.run(
        amain(
            args.runtime_root,
            runtime_db_path=runtime_db_path,
            max_parallel_modules=max_parallel,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
