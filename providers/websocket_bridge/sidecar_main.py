"""Process entrypoint for the WebSocket bridge sidecar.

Spawned and supervised by the ``websocket_bridge`` provider. Parses the
declarative :class:`SidecarConfig` from command-line arguments, configures
process logging, and runs :func:`serve` until the parent issues a shutdown RPC
over the manager socket. A fatal startup error (for example a missing
``websockets`` dependency or an unbindable manager socket) exits the process
non-zero so the provider's subprocess supervision can report it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

try:
    from .sidecar import SidecarConfig, serve
except ImportError:
    # Runtime-root execution imports sibling modules directly from the provider
    # directory rather than through Pal's site-packages namespace.
    from sidecar import SidecarConfig, serve  # type: ignore[no-redef]
from pal.foundation.service_logging import configure_process_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pal-websocket-bridge-sidecar")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--endpoint-id", default="websocket_bridge")
    parser.add_argument(
        "--bridge-socket-path",
        type=Path,
        default=None,
        help=(
            "path to the provider-private local channel "
            "(defaults to <runtime-root>/data/channel/<endpoint-id>/channel.sock)"
        ),
    )
    parser.add_argument("--bind-host", default="0.0.0.0", help="inbound WebSocket listener host")
    parser.add_argument(
        "--bind-port",
        type=int,
        default=0,
        help="inbound WebSocket listener port (0 picks an ephemeral port)",
    )
    parser.add_argument(
        "--peer-url",
        default=None,
        help="optional outbound peer Pal bridge WebSocket URL (ws:// or wss://)",
    )
    parser.add_argument(
        "--reconnect-initial-delay-seconds",
        type=float,
        default=1.0,
        help="initial reconnect backoff delay in seconds",
    )
    parser.add_argument(
        "--reconnect-max-delay-seconds",
        type=float,
        default=30.0,
        help="maximum reconnect backoff delay in seconds",
    )
    parser.add_argument(
        "--binding-metadata",
        default=None,
        help="optional JSON object of provider binding metadata",
    )
    return parser


def _default_data_root(runtime_root: Path, endpoint_id: str) -> Path:
    return Path(runtime_root) / "data" / "channel" / endpoint_id


def _parse_binding_metadata(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def build_config(args: argparse.Namespace) -> SidecarConfig:
    data_root = _default_data_root(args.runtime_root, args.endpoint_id)
    bridge_socket_path = args.bridge_socket_path or data_root / "channel.sock"
    return SidecarConfig(
        runtime_root=args.runtime_root,
        data_root=data_root,
        bridge_socket_path=bridge_socket_path,
        bind_host=args.bind_host,
        bind_port=args.bind_port,
        peer_url=args.peer_url,
        reconnect_initial_delay_seconds=args.reconnect_initial_delay_seconds,
        reconnect_max_delay_seconds=args.reconnect_max_delay_seconds,
        binding_metadata=_parse_binding_metadata(args.binding_metadata),
    )


async def amain(config: SidecarConfig) -> int:
    await serve(config)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        configure_process_logging(component="pal.channel.providers.websocket_bridge.sidecar")
        config = build_config(args)
        return asyncio.run(amain(config))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # fatal startup/runtime error: exit non-zero, surface to provider
        print(
            f"websocket bridge sidecar fatal error: {exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
