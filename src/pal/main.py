from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from pal.runtime_app import build_runtime_app
from pal.socket_client import default_socket_path, send_message


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pal")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run the Pal runtime")
    run_parser.add_argument("--runtime-root", type=Path, required=True)
    run_parser.add_argument("--debug-prompt", action="store_true")

    client_parser = subparsers.add_parser("client", help="Send one message to a running Pal instance")
    client_parser.add_argument("--runtime-root", type=Path, required=True)
    client_parser.add_argument("--message", required=True)
    return parser


async def _run_async(args: argparse.Namespace) -> int:
    if args.command == "run":
        app = build_runtime_app(args.runtime_root, debug_prompt=bool(args.debug_prompt))
        await app.run()
        return 0
    if args.command == "client":
        await send_message(default_socket_path(args.runtime_root), args.message)
        return 0
    return 1


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    return asyncio.run(_run_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
