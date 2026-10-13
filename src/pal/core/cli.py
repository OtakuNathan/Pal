from __future__ import annotations

import argparse
from dataclasses import asdict

from pal.cli_config import add_config_options, configuration_target, emit_error, emit_result, open_config_database, require_runtime, request_owner
from pal.control.configuration import configuration_endpoint
from pal.core.cache_warm_deadline import configure_cache_reminder_settings, read_cache_reminder_settings
from pal.execution.generated_tool_models import CoreCapabilitiesCoreIntrospectionProviderConfigureCacheWarmDeadlineInput
from pal.foundation.sidecar import SidecarRpcClient
from pal.llm.models import PalRuntimeSettingModel
from pal.llm.repository import RuntimeSettingRepository


def configure_core_parser(parser: argparse.ArgumentParser) -> None:
    groups = parser.add_subparsers(dest="core_group", required=True)
    commands = groups.add_parser("cache-reminder").add_subparsers(dest="core_command", required=True)
    show = commands.add_parser("show")
    configure = commands.add_parser("configure")
    configure.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=None)
    configure.add_argument("--lead-seconds", type=int)
    configure.add_argument("--min-prefix-tokens", type=int)
    for command in (show, configure):
        add_config_options(command, online=True)


async def run_core_cli_async(args: argparse.Namespace) -> int:
    try:
        root = require_runtime(args.runtime_root)
        patch = {}
        if args.core_command == "configure":
            patch = {key: value for key, value in {
                "enabled": args.enabled, "lead_seconds": args.lead_seconds,
                "min_prefix_tokens": args.min_prefix_tokens,
            }.items() if value is not None}
            if not patch:
                raise ValueError("configure requires at least one setting")
            CoreCapabilitiesCoreIntrospectionProviderConfigureCacheWarmDeadlineInput.model_validate(patch)
        with configuration_target(root) as target:
            if target == "live":
                client = SidecarRpcClient(configuration_endpoint(root),
                                           request_timeout_seconds=args.timeout_seconds)
                result = await request_owner(client, "cache_reminder." + ("inspect" if args.core_command == "show" else "configure"), patch)
            else:
                with open_config_database(root, (PalRuntimeSettingModel,),
                                          read_only=args.core_command == "show"):
                    repository = RuntimeSettingRepository()
                    settings = (configure_cache_reminder_settings(repository, **patch)
                                if args.core_command == "configure" else read_cache_reminder_settings(repository))
                    result = {"status": "ok", "activation": "on_start", "state": asdict(settings)}
        return emit_result(args, result)
    except Exception as exc:
        return emit_error(args, exc)
