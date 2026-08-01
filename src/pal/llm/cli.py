from __future__ import annotations

import argparse
import getpass
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from pal.foundation import PalV2Database
from pal.llm.credentials import LLMCredentialResolver
from pal.llm.ir import ThinkingLevel, WireShape
from pal.llm.models import LLMEndpointModel, PalRuntimeSettingModel
from pal.llm.repository import LLMEndpointRepository, RuntimeSettingRepository
from pal.llm.schema import migrate_llm_endpoint_schema
from pal.llm.secret_store import EncryptedFileSecretStore


DEFAULT_RUNTIME_ROOT = Path.home() / ".pal"
_DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"


def configure_llm_parser(parser: argparse.ArgumentParser) -> None:
    parser.epilog = (
        "examples:\n"
        "  pal llm list\n"
        "  pal llm add deepseek-v4-pro --store-api-key --set-active\n"
        "  pal llm delete deepseek-v4-pro"
    )
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    commands = parser.add_subparsers(dest="llm_command", required=True)

    list_parser = commands.add_parser("list", help="List configured LLM endpoints")
    _add_runtime_root(list_parser)
    list_parser.add_argument("--all", action="store_true", help="Include disabled endpoints")
    list_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    add_parser = commands.add_parser("add", help="Add an LLM endpoint")
    _add_runtime_root(add_parser)
    add_parser.add_argument("endpoint_id", help="Stable endpoint identifier")
    add_parser.add_argument("--model-id", default=None)
    add_parser.add_argument("--provider", default=None)
    add_parser.add_argument("--display-name", default=None)
    add_parser.add_argument("--wire-shape", choices=tuple(item.value for item in WireShape), default=None)
    add_parser.add_argument("--base-url", default=None)
    add_parser.add_argument(
        "--auth-kind",
        choices=("api_key_ref", "oauth", "local_provider_auth"),
        default=None,
    )
    add_parser.add_argument("--credential-ref", default=None)
    add_parser.add_argument(
        "--api-key-env",
        default=None,
        metavar="ENV_VAR",
        help="Resolve the API key from this environment variable",
    )
    key_input = add_parser.add_mutually_exclusive_group()
    key_input.add_argument(
        "--store-api-key",
        action="store_true",
        help="Prompt securely and store the API key in runtime-root/secrets.json",
    )
    key_input.add_argument(
        "--api-key-stdin",
        action="store_true",
        help="Read the API key from stdin and store it; never place secrets in argv",
    )
    add_parser.add_argument("--context-window", type=_positive_int, default=None)
    add_parser.add_argument("--max-output-tokens", type=_positive_int, default=None)
    add_parser.add_argument(
        "--thinking-levels",
        default=None,
        metavar="LEVELS",
        help="Comma-separated provider-supported levels, for example off,high,max",
    )
    add_parser.add_argument("--default-thinking-level", default=None)
    add_parser.add_argument("--priority", type=int, default=None)
    add_parser.add_argument("--tools", action=argparse.BooleanOptionalAction, default=None)
    add_parser.add_argument("--streaming", action=argparse.BooleanOptionalAction, default=None)
    add_parser.add_argument("--vision", action=argparse.BooleanOptionalAction, default=None)
    add_parser.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=None)
    add_parser.add_argument("--notes", default=None)
    add_parser.add_argument(
        "--replace",
        action="store_true",
        help="Update an existing endpoint; omitted fields retain their current values",
    )
    add_parser.add_argument("--set-active", action="store_true", help="Use this endpoint for future turns")

    delete_parser = commands.add_parser(
        "delete",
        help="Delete an LLM endpoint",
        description=(
            "Delete exactly one configured LLM endpoint. Its per-endpoint thinking "
            "selection is removed, but shared credential material is preserved."
        ),
    )
    _add_runtime_root(delete_parser)
    delete_parser.add_argument("endpoint_id", help="Exact endpoint identifier to delete")


def run_llm_cli(args: argparse.Namespace) -> int:
    runtime_root = Path(args.runtime_root).expanduser().resolve()
    try:
        with _open_runtime_database(runtime_root) as database:
            if args.llm_command == "list":
                return _run_list(args)
            if args.llm_command == "add":
                with database.transaction():
                    return _run_add(args, runtime_root)
            if args.llm_command == "delete":
                with database.transaction():
                    return _run_delete(args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"pal llm: {exc}", file=sys.stderr)
        return 2
    return 2


def _run_list(args: argparse.Namespace) -> int:
    repository = LLMEndpointRepository()
    endpoints = repository.list_all() if args.all else repository.list_enabled()
    active_endpoint_id = RuntimeSettingRepository().get_active_llm_endpoint_id()
    rows = [_endpoint_payload(endpoint, active_endpoint_id=active_endpoint_id) for endpoint in endpoints]
    if args.json:
        print(json.dumps({"active_endpoint_id": active_endpoint_id, "items": rows}, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("No configured LLM endpoints.")
        return 0
    headers = ("ACTIVE", "ENDPOINT", "MODEL", "PROVIDER", "SHAPE", "PRIORITY", "ENABLED")
    values = [
        (
            "*" if row["active"] else "",
            row["endpoint_id"],
            row["model_id"],
            row["provider"],
            row["wire_shape"],
            str(row["priority"]),
            "yes" if row["enabled"] else "no",
        )
        for row in rows
    ]
    widths = [max(len(headers[index]), *(len(row[index]) for row in values)) for index in range(len(headers))]
    print("  ".join(headers[index].ljust(widths[index]) for index in range(len(headers))).rstrip())
    print("  ".join("-" * width for width in widths).rstrip())
    for row in values:
        print("  ".join(row[index].ljust(widths[index]) for index in range(len(row))).rstrip())
    return 0


def _run_add(args: argparse.Namespace, runtime_root: Path) -> int:
    repository = LLMEndpointRepository()
    endpoint_id = str(args.endpoint_id or "").strip()
    if not endpoint_id:
        raise ValueError("endpoint_id must be non-empty")
    existing = repository.get(endpoint_id)
    if existing is not None and not args.replace:
        raise ValueError(f"endpoint {endpoint_id!r} already exists; pass --replace to update it")

    model_id = _option(args.model_id, existing, "model_id", endpoint_id)
    requested_provider = _option(args.provider, existing, "provider", "")
    requested_shape = _option(args.wire_shape, existing, "wire_shape", "")
    requested_base_url = _option(args.base_url, existing, "base_url", "")
    provider, wire_shape, base_url = _endpoint_identity_defaults(
        provider=requested_provider,
        model_id=model_id,
        wire_shape=requested_shape,
        base_url=requested_base_url,
    )
    auth_kind = _option(args.auth_kind, existing, "auth_kind", "api_key_ref")
    credential_ref = str(
        args.api_key_env
        or _option(args.credential_ref, existing, "credential_ref", f"{endpoint_id}:api-key")
    ).strip()
    if auth_kind != "local_provider_auth" and not credential_ref:
        raise ValueError("credential_ref is required unless auth-kind is local_provider_auth")

    levels = _thinking_levels(
        args.thinking_levels,
        existing=existing,
        model_id=model_id,
    )
    default_level = str(
        args.default_thinking_level
        or _existing_value(existing, "default_thinking_level", "")
        or ("high" if "high" in levels else levels[0])
    ).strip().lower()
    if default_level not in levels:
        raise ValueError(
            f"default thinking level {default_level!r} is not in configured levels: {', '.join(levels)}"
        )

    supports_tools = _bool_option(args.tools, existing, "supports_tools", True)
    supports_streaming = _bool_option(args.streaming, existing, "supports_streaming", True)
    supports_vision = _bool_option(args.vision, existing, "supports_vision", False)
    enabled = _bool_option(args.enabled, existing, "enabled", True)
    payload = {
        "endpoint_id": endpoint_id,
        "provider": provider,
        "model_id": model_id,
        "display_name": _option(args.display_name, existing, "display_name", endpoint_id),
        "wire_shape": wire_shape,
        "base_url": base_url,
        "auth_kind": auth_kind,
        "credential_ref": credential_ref,
        "context_window": _option(args.context_window, existing, "context_window", None),
        "max_output_tokens": _option(args.max_output_tokens, existing, "max_output_tokens", None),
        "thinking_levels_blob": levels,
        "default_thinking_level": default_level,
        "supports_tools": supports_tools,
        "supports_streaming": supports_streaming,
        "supports_vision": supports_vision,
        "input_modalities_blob": ["text", "image"] if supports_vision else ["text"],
        "output_modalities_blob": list(_existing_value(existing, "output_modalities_blob", ["text"]) or ["text"]),
        "priority": int(_option(args.priority, existing, "priority", 0)),
        "enabled": enabled,
        "capabilities_blob": dict(_existing_value(existing, "capabilities_blob", {}) or {}),
        "notes": _option(args.notes, existing, "notes", "Configured via pal llm add."),
    }
    endpoint = repository.upsert(**payload)
    _store_api_key_if_requested(args, runtime_root, endpoint)
    if args.set_active:
        if not endpoint.enabled:
            raise ValueError("a disabled endpoint cannot be set active")
        RuntimeSettingRepository().set_active_llm_endpoint_id(endpoint.endpoint_id)
    action = "updated" if existing is not None else "added"
    print(
        f"LLM endpoint {endpoint.endpoint_id!r} {action}: "
        f"{endpoint.model_id} via {endpoint.provider}/{endpoint.wire_shape}"
    )
    if args.set_active:
        print("Active endpoint updated.")
    print("A running Pal can load the change with /refresh_llm_endpoint; a new process loads it automatically.")
    return 0


def _run_delete(args: argparse.Namespace) -> int:
    repository = LLMEndpointRepository()
    settings = RuntimeSettingRepository()
    endpoint_id = str(args.endpoint_id or "").strip()
    if not endpoint_id:
        raise ValueError("endpoint_id must be non-empty")
    endpoint = repository.get(endpoint_id)
    if endpoint is None:
        raise ValueError(f"endpoint {endpoint_id!r} does not exist")

    was_active = settings.get_active_llm_endpoint_id() == endpoint_id
    if not repository.delete(endpoint_id):
        raise RuntimeError(f"endpoint {endpoint_id!r} could not be deleted")
    settings.delete_think_level(endpoint_id)

    next_active: str | None = None
    if was_active:
        replacement = repository.get_primary_enabled()
        if replacement is None:
            settings.delete_active_llm_endpoint_id()
        else:
            next_active = str(replacement.endpoint_id)
            settings.set_active_llm_endpoint_id(next_active)

    print(f"LLM endpoint {endpoint_id!r} deleted. Stored credential material was preserved.")
    if was_active:
        if next_active is None:
            print("No enabled endpoints remain; the active endpoint setting was cleared.")
        else:
            print(f"Active endpoint moved to {next_active!r}.")
    print("A running Pal can load the change with /refresh_llm_endpoint; a new process loads it automatically.")
    return 0


@contextmanager
def _open_runtime_database(runtime_root: Path) -> Iterator[PalV2Database]:
    db_path = runtime_root / "pal.sqlite3"
    if not db_path.is_file():
        raise FileNotFoundError(f"Pal runtime database does not exist: {db_path}")
    migrate_llm_endpoint_schema(db_path)
    database = PalV2Database(db_path)
    database.initialize((LLMEndpointModel, PalRuntimeSettingModel))
    try:
        yield database
    finally:
        database.close()


def _store_api_key_if_requested(
    args: argparse.Namespace,
    runtime_root: Path,
    endpoint: LLMEndpointModel,
) -> None:
    secret = ""
    if args.store_api_key:
        secret = getpass.getpass("API key: ").strip()
    elif args.api_key_stdin:
        secret = sys.stdin.read().strip()
    if not secret:
        if args.store_api_key or args.api_key_stdin:
            raise ValueError("API key input was empty")
        return
    store = EncryptedFileSecretStore(runtime_root / "secrets.json")
    ref = LLMCredentialResolver(secret_store=store).secret_ref_for_endpoint(endpoint)
    if ref is None:
        raise ValueError("the endpoint does not define a credential reference")
    store.set_secret(ref, secret)


def _endpoint_identity_defaults(
    *,
    provider: str,
    model_id: str,
    wire_shape: str,
    base_url: str,
) -> tuple[str, str, str]:
    normalized_model = str(model_id or "").strip()
    normalized_provider = str(provider or "").strip().lower()
    normalized_shape = str(wire_shape or "").strip()
    normalized_url = str(base_url or "").strip()
    deepseek = normalized_provider == "deepseek" or normalized_model.lower().startswith("deepseek-") or "deepseek.com" in normalized_url.lower()
    if deepseek:
        normalized_provider = "deepseek"
        normalized_shape = normalized_shape or WireShape.ANTHROPIC_MESSAGES.value
        normalized_url = normalized_url or _DEEPSEEK_ANTHROPIC_BASE_URL
    else:
        normalized_shape = normalized_shape or WireShape.OPENAI_COMPLETION.value
        if not normalized_provider:
            normalized_provider = "anthropic" if normalized_shape == WireShape.ANTHROPIC_MESSAGES.value else "openai"
        if not normalized_url:
            normalized_url = (
                "https://api.anthropic.com"
                if normalized_shape == WireShape.ANTHROPIC_MESSAGES.value
                else "https://api.openai.com/v1"
            )
    WireShape(normalized_shape)
    if not normalized_model:
        raise ValueError("model_id must be non-empty")
    if not normalized_url:
        raise ValueError("base_url must be non-empty")
    return normalized_provider, normalized_shape, normalized_url


def _thinking_levels(raw: str | None, *, existing: LLMEndpointModel | None, model_id: str) -> list[str]:
    if raw is not None:
        candidates = [item.strip().lower() for item in str(raw).split(",")]
    elif existing is not None:
        candidates = [str(item).strip().lower() for item in (existing.thinking_levels_blob or ())]
    elif str(model_id).lower().startswith("deepseek-v4-"):
        candidates = ["off", "high", "max"]
    else:
        candidates = ["off"]
    allowed = {item.value for item in ThinkingLevel}
    levels: list[str] = []
    for item in candidates:
        if not item:
            continue
        if item not in allowed:
            raise ValueError(f"unknown thinking level: {item}")
        if item not in levels:
            levels.append(item)
    if not levels:
        raise ValueError("at least one thinking level is required")
    return levels


def _endpoint_payload(endpoint: LLMEndpointModel, *, active_endpoint_id: str | None) -> dict[str, Any]:
    return {
        "endpoint_id": endpoint.endpoint_id,
        "model_id": endpoint.model_id,
        "provider": endpoint.provider,
        "display_name": endpoint.display_name,
        "wire_shape": endpoint.wire_shape,
        "base_url": endpoint.base_url,
        "context_window": endpoint.context_window,
        "max_output_tokens": endpoint.max_output_tokens,
        "thinking_levels": list(endpoint.thinking_levels_blob or ()),
        "default_thinking_level": endpoint.default_thinking_level,
        "priority": int(endpoint.priority),
        "enabled": bool(endpoint.enabled),
        "active": endpoint.endpoint_id == active_endpoint_id,
    }


def _add_runtime_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=DEFAULT_RUNTIME_ROOT,
        help=f"Pal runtime root (default: {DEFAULT_RUNTIME_ROOT})",
    )


def _existing_value(existing: LLMEndpointModel | None, field_name: str, fallback: Any) -> Any:
    return getattr(existing, field_name, fallback) if existing is not None else fallback


def _option(value: Any, existing: LLMEndpointModel | None, field_name: str, fallback: Any) -> Any:
    return value if value is not None else _existing_value(existing, field_name, fallback)


def _bool_option(value: bool | None, existing: LLMEndpointModel | None, field_name: str, fallback: bool) -> bool:
    return bool(value if value is not None else _existing_value(existing, field_name, fallback))


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed
