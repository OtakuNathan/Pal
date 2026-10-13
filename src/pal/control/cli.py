"""Read the human control registry through its resident owner."""
from __future__ import annotations

import argparse

from pal.cli_config import add_config_options, configuration_target, emit_error, emit_result, require_runtime, request_owner
from pal.control.configuration import configuration_endpoint
from pal.foundation.sidecar import SidecarRpcClient


def configure_control_parser(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="control_command", required=True)
    add_config_options(commands.add_parser("status"), online=True)


async def run_control_cli_async(args: argparse.Namespace) -> int:
    try:
        root = require_runtime(args.runtime_root)
        with configuration_target(root) as target:
            if target != "live":
                raise RuntimeError("Control status requires a running Pal.")
            client = SidecarRpcClient(configuration_endpoint(root), request_timeout_seconds=args.timeout_seconds)
            result = await request_owner(client, "control.inspect", {})
        return emit_result(args, result)
    except Exception as exc:
        return emit_error(args, exc)
