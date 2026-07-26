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

from pal.channel.endpoints.socket_protocol import DEFAULT_SOCKET_FILENAME
from pal.channel.providers.websocket_bridge.sidecar import SidecarConfig, serve
from pal.foundation.service_logging import configure_process_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pal-websocket-bridge-sidecar")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument(
        "--socket-channel-path",
        type=Path,
        default=None,
        help="path to the existing local socket channel (defaults to <runtime-root>/pal.sock)",
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
        "--message-timeout-seconds",
        type=float,
        default=3000.0,
        help="maximum time to wait for a peer Pal response",
    )
    parser.add_argument(
        "--binding-metadata",
        default=None,
        help="optional JSON object of provider binding metadata",
    )
    return parser


def _default_socket_channel_path(runtime_root: Path) -> Path:
    return Path(runtime_root) / DEFAULT_SOCKET_FILENAME


def _parse_binding_metadata(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def build_config(args: argparse.Namespace) -> SidecarConfig:
    socket_channel_path = args.socket_channel_path or _default_socket_channel_path(args.runtime_root)
    return SidecarConfig(
        runtime_root=args.runtime_root,
        socket_channel_path=socket_channel_path,
        bind_host=args.bind_host,
        bind_port=args.bind_port,
        peer_url=args.peer_url,
        reconnect_initial_delay_seconds=args.reconnect_initial_delay_seconds,
        reconnect_max_delay_seconds=args.reconnect_max_delay_seconds,
        message_timeout_seconds=args.message_timeout_seconds,
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
