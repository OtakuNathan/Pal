"""Memory backend controls use the resident owner; migrations require its lease."""
from __future__ import annotations

import argparse

from pal.cli_config import add_config_options, configuration_target, emit_error, emit_result, require_runtime, request_owner
from pal.control.configuration import configuration_endpoint
from pal.foundation.sidecar import SidecarRpcClient


def configure_memory_parser(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="memory_command", required=True)
    providers = commands.add_parser("providers", help="List resident memory providers")
    inspect = commands.add_parser("inspect", help="Inspect a provider or the active provider")
    inspect.add_argument("name", nargs="?")
    refresh = commands.add_parser("refresh-indexes", help="Refresh a bounded batch of pending embeddings")
    refresh.add_argument("name", nargs="?")
    refresh.add_argument("--limit", type=int, default=32)
    refresh.add_argument("--retry-failed", action="store_true")
    for command in (providers, inspect, refresh):
        add_config_options(command, online=True)
    for name in ("attach", "detach", "select"):
        command = commands.add_parser(name)
        command.add_argument("name")
        add_config_options(command, online=True)
    embedding = commands.add_parser("embedding", help="Plan or resume an offline embedding-space migration")
    operations = embedding.add_subparsers(dest="embedding_command", required=True)
    from pathlib import Path
    for name in ("plan", "migrate", "resume", "status"):
        command = operations.add_parser(name)
        if name in {"plan", "migrate"}:
            command.add_argument("--patch-file", type=Path, required=True)
        if name in {"migrate", "resume"}:
            command.add_argument("--batch-size", type=int, default=32)
        add_config_options(command)


async def run_memory_cli_async(args: argparse.Namespace) -> int:
    try:
        root = require_runtime(args.runtime_root)
        with configuration_target(root) as target:
            if args.memory_command == "embedding":
                from pal.memory.embedding_migration import run_embedding_command
                if target == "live":
                    raise RuntimeError("Stop Pal before planning or running an embedding migration")
                result = run_embedding_command(root, args)
            elif target == "live":
                params = {"name": args.name} if getattr(args, "name", None) else {}
                if args.memory_command == "refresh-indexes":
                    params.update(limit=args.limit, retry_failed=args.retry_failed)
                client = SidecarRpcClient(configuration_endpoint(root), request_timeout_seconds=args.timeout_seconds)
                result = await request_owner(client, "memory." + args.memory_command, params)
            elif args.memory_command in {"providers", "inspect"}:
                from pal.memory.embedding_migration import inspect_offline
                result = inspect_offline(root, getattr(args, "name", None))
            else:
                raise RuntimeError("This operation requires the resident memory owner; start Pal or use offline embedding migration")
        emit_result(args, result)
        return 0 if result.get("status") == "ok" else 2
    except Exception as exc:
        return emit_error(args, exc)
