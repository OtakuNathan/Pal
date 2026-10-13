"""Human-owned identity inspection and explicit resident projection refresh."""
from __future__ import annotations

import argparse
from dataclasses import asdict

from pal.cli_config import add_config_options, configuration_target, emit_error, emit_result, open_config_database, require_runtime, request_owner
from pal.control.configuration import configuration_endpoint
from pal.foundation.sidecar import SidecarRpcClient
from pal.identity.models import PalPersonaModel, UserPreferencesModel
from pal.identity.repository import IdentityRepository
from pal.identity.service import IdentityService


def configure_identity_parser(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="identity_command", required=True)
    for name in ("show", "refresh"):
        add_config_options(commands.add_parser(name), online=True)


async def run_identity_cli_async(args: argparse.Namespace) -> int:
    try:
        root = require_runtime(args.runtime_root)
        with configuration_target(root) as target:
            if target == "live":
                client = SidecarRpcClient(configuration_endpoint(root), request_timeout_seconds=args.timeout_seconds)
                result = await request_owner(client, "identity." + ("inspect" if args.identity_command == "show" else "refresh"), {})
            else:
                if args.identity_command == "refresh":
                    raise RuntimeError("Identity refresh requires a running Pal; a new host loads identity on start.")
                with open_config_database(root, (PalPersonaModel, UserPreferencesModel), read_only=True):
                    persona, preferences = IdentityService(IdentityRepository()).refresh_projection()
                    result = {"status": "ok", "activation": "on_start", "state": {
                        "has_persona": persona is not None, "has_preferences": preferences is not None,
                        "persona": asdict(persona) if persona is not None else None,
                        "preferences": asdict(preferences) if preferences is not None else None,
                    }}
        return emit_result(args, result)
    except Exception as exc:
        return emit_error(args, exc)
