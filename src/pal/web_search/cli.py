from __future__ import annotations

import argparse
from pathlib import Path

from pal.cli_config import add_config_options, emit_error, emit_result, open_config_database, read_object
from pal.llm.models import PalRuntimeSettingModel
from pal.llm.repository import RuntimeSettingRepository
from pal.web_search.models import WebSearchProviderModel
from pal.web_search.repository import WebSearchProviderRepository
from pal.web_search.service import WebSearchService


def configure_web_search_parser(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="web_search_command", required=True)
    for name in ("list", "show", "active", "configure", "auth", "enable", "disable", "set-active"):
        command = commands.add_parser(name)
        if name not in {"list", "active"}:
            command.add_argument("name")
        if name == "configure":
            command.add_argument("--patch-file", type=Path, required=True,
                                 help="JSON settings merge patch; omitted keys are preserved")
        if name == "auth":
            command.add_argument("--stdin", action="store_true", required=True,
                                 help="Read a JSON credentials merge patch from stdin; never put secrets in argv")
        add_config_options(command)


def provider_payload(record) -> dict:
    return {"name": record.provider_id, "provider_id": record.provider_id,
            "provider_kind": record.provider_kind, "display_name": record.display_name,
            "enabled": bool(record.enabled), "priority": int(record.priority),
            "settings": dict(record.settings_blob or {}),
            "auth_keys": sorted((record.auth_material_blob or {}).keys()), "notes": record.notes}


def run_web_search_cli(args: argparse.Namespace) -> int:
    try:
        action = args.web_search_command
        patch = read_object(path=args.patch_file) if action == "configure" else (
            read_object(stdin=True) if action == "auth" else None)
        with open_config_database(args.runtime_root, (WebSearchProviderModel, PalRuntimeSettingModel),
                                  read_only=action in {"list", "show", "active"}) as database:
            service = WebSearchService(WebSearchProviderRepository(), RuntimeSettingRepository())
            with database.transaction():
                if action == "list":
                    payload = {"items": [provider_payload(r) for r in service.list_providers()]}
                elif action == "active":
                    payload = {"configured_provider_id": service.configured_active_provider_id(),
                               "effective_provider_id": service.effective_active_provider_id()}
                else:
                    record = service.get_provider(args.name)
                    if record is None:
                        raise ValueError(f"Unknown web search provider: {args.name}")
                    if action == "configure":
                        record = service.set_config(args.name, patch)
                    elif action == "auth":
                        record = service.set_auth_material(args.name, patch)
                    elif action in {"enable", "disable"}:
                        record = service.set_enabled(args.name, action == "enable")
                    elif action == "set-active":
                        if not record.enabled:
                            raise ValueError("Web search provider is disabled; enable it before selecting it")
                        service.set_active_provider(args.name)
                    payload = (dict(service.provider_auth_state(record), accepted_keys=sorted(patch))
                               if action == "auth" else provider_payload(record))
                result = {"status": "ok", **payload}
                if action not in {"list", "show", "active"}:
                    result["activation"] = "subsequent_queries"
            return emit_result(args, result)
    except Exception as exc:
        return emit_error(args, exc)
