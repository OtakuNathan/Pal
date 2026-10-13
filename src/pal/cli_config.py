"""Shared I/O and existing-runtime selection for configuration CLIs."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sys
from typing import Iterator

from pal.cli_paths import default_runtime_root, RUNTIME_ROOT_HELP
from pal.foundation.persistence import PalV2Database, database_proxy
from pal.foundation.sidecar import SidecarRpcError
from pal.packages.process import PackageError, runtime_lease


def add_config_options(parser: argparse.ArgumentParser, *, online: bool = False) -> None:
    parser.add_argument("--runtime-root", type=Path, default=default_runtime_root(), help=RUNTIME_ROOT_HELP)
    parser.add_argument("--json", action="store_true", help="Emit a machine-readable result")
    if online:
        parser.add_argument("--timeout-seconds", type=positive_timeout, default=30.0)


def positive_timeout(value: str) -> float:
    parsed = float(value)
    if not 0 < parsed < float("inf"):
        raise argparse.ArgumentTypeError("timeout must be positive and finite")
    return parsed


def read_object(*, path: Path | None = None, stdin: bool = False) -> dict:
    raw = sys.stdin.read() if stdin else path.read_text(encoding="utf-8")
    try:
        value = json.loads(raw)
    except ValueError:
        raise ValueError("Input must be a valid JSON object") from None
    if not isinstance(value, dict):
        raise ValueError("Input must be a JSON object")
    return value


def emit_result(args: argparse.Namespace, result: dict) -> int:
    # As with package commands, readable JSON is also the terminal default.
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def emit_error(args: argparse.Namespace, error: Exception) -> int:
    payload = {"status": "error", "error": str(error)}
    if isinstance(error, SidecarRpcError) and error.kind in {"timeout", "transport"}:
        payload.update(outcome="unknown", recovery="Inspect owner state before retrying; do not fall back to direct storage writes.")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(str(error), file=sys.stderr)
    return 2


def require_runtime(runtime_root: Path) -> Path:
    root = Path(runtime_root).expanduser().resolve()
    if not (root / "pal.sqlite3").is_file():
        raise FileNotFoundError(f"No configured Pal runtime database at {root / 'pal.sqlite3'}")
    return root


async def request_owner(client, method: str, params: dict) -> dict:
    try:
        return await client.request(method, params)
    except (OSError, EOFError) as exc:
        raise SidecarRpcError(f"Configuration owner connection failed: {exc}", kind="transport") from exc


@contextmanager
def configuration_target(runtime_root: Path) -> Iterator[str]:
    """Hold the host lease throughout offline work; never infer idle from a socket."""
    lease = runtime_lease(require_runtime(runtime_root))
    try:
        lease.__enter__()
    except PackageError:
        yield "live"
        return
    try:
        yield "offline"
    finally:
        lease.__exit__(None, None, None)


@contextmanager
def open_config_database(runtime_root: Path, models: tuple, *, read_only: bool = False):
    root = require_runtime(runtime_root)
    database = PalV2Database(root / "pal.sqlite3", read_only=read_only)
    previous_proxy = database_proxy.obj
    previous_bindings = {model: model._meta.database for model in models}
    try:
        database.peewee_db.connect()
        tables = set(database.peewee_db.get_tables())
        missing = [model._meta.table_name for model in models if model._meta.table_name not in tables]
        if missing:
            raise RuntimeError("Runtime schema is missing: " + ", ".join(missing))
        database_proxy.initialize(database.peewee_db)
        database.peewee_db.bind(models, bind_refs=False, bind_backrefs=False)
        yield database
    finally:
        database.close()
        for model, binding in previous_bindings.items():
            model._meta.set_database(binding)
        database_proxy.initialize(previous_proxy)
