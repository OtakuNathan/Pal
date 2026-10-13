from __future__ import annotations

import argparse
from pathlib import Path

from pal.bunshin.catalog import BunshinCatalogService
from pal.bunshin.ipc import BunshinManagerClient
from pal.cli_config import add_config_options, configuration_target, emit_error, emit_result, read_object, require_runtime, request_owner
from pal.execution.generated_tool_models import (
    BunshinV2CapabilitiesBunshinV2PublicProviderSetFamilyOverrideInput,
    BunshinV2CapabilitiesBunshinV2PublicProviderSetProfileOverrideInput,
)


def configure_bunshin_config_parsers(commands) -> None:
    for kind in ("catalog", "profile", "family"):
        operations = commands.add_parser(kind).add_subparsers(dest="config_action", required=True)
        for action in (("show", "refresh") if kind == "catalog" else ("patch", "reset")):
            command = operations.add_parser(action)
            if kind != "catalog":
                command.add_argument("name")
                command.add_argument("--if-generation", default="", help="Reject changes to a different catalog generation")
            if action == "patch":
                command.add_argument("--patch-file", type=Path, required=True,
                                     help="Typed JSON merge patch; explicit null removes an optional field")
            add_config_options(command, online=True)


async def run_bunshin_config_cli_async(args: argparse.Namespace) -> int:
    try:
        root = require_runtime(args.runtime_root)
        kind, action = args.bunshin_command, args.config_action
        params = {"actor": "pal_cli"}
        if kind == "catalog":
            method = "catalog_snapshot" if action == "show" else "catalog_refresh"
            if action == "show":
                params = {"include_definitions": True}
        else:
            params.update({kind: args.name, "if_generation": args.if_generation})
            method = f"catalog_{'set' if action == 'patch' else 'reset'}_{kind}_override"
            if action == "patch":
                changes = read_object(path=args.patch_file)
                model_type = (BunshinV2CapabilitiesBunshinV2PublicProviderSetProfileOverrideInput
                              if kind == "profile" else BunshinV2CapabilitiesBunshinV2PublicProviderSetFamilyOverrideInput)
                model = model_type.model_validate({kind: args.name, "changes": changes})
                params['changes'] = model.model_dump(exclude_unset=True)['changes']
                if kind == "profile" and "preferred_endpoint_name" in params['changes']:
                    params['changes']['preferred_endpoint_id'] = params['changes'].pop('preferred_endpoint_name')
        with configuration_target(root) as target:
            if target == "live":
                result = await request_owner(BunshinManagerClient(root, request_timeout_seconds=args.timeout_seconds), method, params)
            else:
                service = BunshinCatalogService(root)
                operations = {
                    "catalog_snapshot": service.snapshot,
                    "catalog_refresh": service.refresh,
                    "catalog_set_profile_override": service.set_profile_override,
                    "catalog_reset_profile_override": service.reset_profile_override,
                    "catalog_set_family_override": service.set_family_override,
                    "catalog_reset_family_override": service.reset_family_override,
                }
                result = operations[method](**params)
        result = {**result, "activation": "future_tasks", "owner": target}
        return emit_result(args, result)
    except Exception as exc:
        return emit_error(args, exc)
