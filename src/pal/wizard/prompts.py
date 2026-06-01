"""Interactive setup wizard for Pal.

Standalone prompts and data collection — no database or repository imports.
The I/O layer produces pure data objects that WizardService.seed_from_wizard()
persists through repositories.
"""

from __future__ import annotations

import getpass
import json
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Model metadata query (ported from old wizard)
# ---------------------------------------------------------------------------

_KNOWN_ANTHROPIC_MODELS: dict[str, dict[str, Any]] = {
    "claude-sonnet-4-20250514": {
        "context_length": 200000,
        "max_output_tokens": 16384,
        "supports_thinking": True,
        "supports_vision": True,
        "supports_tools": True,
    },
    "claude-opus-4-20250514": {
        "context_length": 200000,
        "max_output_tokens": 16384,
        "supports_thinking": True,
        "supports_vision": True,
        "supports_tools": True,
    },
    "claude-haiku-4-5-20251001": {
        "context_length": 200000,
        "max_output_tokens": 8192,
        "supports_thinking": True,
        "supports_vision": True,
        "supports_tools": True,
    },
}

DEFAULT_CODEX_WIZARD_MODELS = (
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
)
DEFAULT_CODEX_CONTEXT_WINDOW = 200_000
DEFAULT_CODEX_MAX_OUTPUT_TOKENS = 32_768


def _models_url_from_base(base_url: str, model_id: str) -> str:
    url = base_url.rstrip("/")
    for suffix in ("/chat/completions", "/chat", "/v1/messages", "/messages"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    url = url.rstrip("/")
    return f"{url}/models/{model_id}"


def query_model_metadata(
    base_url: str,
    model_id: str,
    api_key: str,
    api_mode: str,
) -> dict[str, Any] | None:
    if api_mode == "anthropic_messages":
        return _KNOWN_ANTHROPIC_MODELS.get(model_id)

    url = _models_url_from_base(base_url, model_id)
    headers = {"Authorization": f"Bearer {api_key}"}
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return None

    if not isinstance(data, dict):
        return None

    meta = data.get("data") or data
    if not isinstance(meta, dict):
        return None

    result: dict[str, Any] = {}
    if meta.get("context_length") or meta.get("context_window"):
        result["context_length"] = meta.get("context_length") or meta.get("context_window")
    if meta.get("max_output_tokens") or meta.get("max_tokens"):
        result["max_output_tokens"] = meta.get("max_output_tokens") or meta.get("max_tokens")
    if "supports_tools" in meta:
        result["supports_tools"] = meta["supports_tools"]
    else:
        result["supports_tools"] = True
    if "supports_vision" in meta:
        result["supports_vision"] = meta["supports_vision"]
    if "supports_thinking" in meta:
        result["supports_thinking"] = meta["supports_thinking"]

    return result or None


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------

def _read_line(prompt_text: str) -> str:
    if sys.stdin.isatty():
        try:
            from prompt_toolkit import prompt as tty_prompt

            return tty_prompt(prompt_text)
        except Exception:
            pass
    return input(prompt_text)


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]: " if default else ": "
    raw = _read_line(prompt + suffix).strip()
    return raw if raw else default


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    raw = _read_line(prompt + suffix).strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def ask_password(prompt: str) -> str | None:
    raw = getpass.getpass(prompt + ": ").strip()
    return raw if raw else None


def normalize_telegram_binding_key(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if re.match(r"^(user|chat|chat_user):", raw):
        return raw
    if re.fullmatch(r"-?\d+", raw):
        scope = "chat" if raw.startswith("-") else "user"
        return f"{scope}:{raw}"
    return f"user:{raw}"


def multiline_input(prompt: str, sentinel: str = ".") -> str:
    print(f"{prompt} (enter '{sentinel}' on its own line to finish)")
    lines: list[str] = []
    while True:
        try:
            line = _read_line("> ")
        except EOFError:
            break
        if line.strip() == sentinel:
            break
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class WizardIdentity:
    display_name: str
    language: str
    vibe: str | None
    tone: str | None
    core_policy: list[str]
    timezone: str | None


@dataclass
class WizardLLMEndpoint:
    endpoint_id: str
    model_id: str
    api_mode: str
    base_url: str
    api_key: str | None
    context_window: int | None
    max_output_tokens: int | None
    supports_reasoning: bool
    supports_tools: bool
    supports_streaming: bool
    supports_vision: bool
    priority: int
    provider: str | None = None
    auth_kind: str = "api_key_ref"
    credential_ref: str | None = None
    capabilities_blob: dict[str, Any] = field(default_factory=dict)
    notes: str | None = None


@dataclass
class WizardChannel:
    endpoint_id: str
    channel_kind: str
    binding_key: str
    binding_metadata: dict[str, object] = field(default_factory=dict)
    supports_typing: bool = False
    supports_receipt_marker: bool = False


@dataclass
class WizardCollectedData:
    identity: WizardIdentity
    endpoints: list[WizardLLMEndpoint]
    channel: WizardChannel
    active_endpoint_id: str


@dataclass(frozen=True)
class WizardLLMPreflightResult:
    status: str
    detail: str
    text_ok: bool = False
    tool_ok: bool = False


# ---------------------------------------------------------------------------
# Prompt steps
# ---------------------------------------------------------------------------

def _print_step(step: int, total: int, title: str) -> None:
    print(f"\n{'=' * 50}")
    print(f"  Step {step}/{total}: {title}")
    print(f"{'=' * 50}\n")


def _multiline_with_default(prompt: str, default: str | None = None) -> str | None:
    if default:
        print("Current value:")
        for line in str(default).splitlines():
            print(f"  {line}")
        print("Leave blank to keep current. Enter '<clear>' to clear.")
    value = multiline_input(prompt)
    if default is not None and not value.strip():
        return default
    if value.strip() == "<clear>":
        return None
    return value or None


def _policy_with_default(prompt: str, default: list[str] | None = None) -> list[str]:
    default_text = "\n".join(default or [])
    text = _multiline_with_default(prompt, default_text if default else None)
    if text is None:
        return []
    return [line for line in text.splitlines() if line.strip()]


def _prompt_int(prompt: str, default: int | None, fallback: int | None = None) -> int | None:
    raw = ask(prompt, "" if default is None else str(default))
    if not raw:
        return fallback
    try:
        return int(raw)
    except ValueError:
        return fallback


def prompt_runtime_home() -> Path:
    default = str(Path.home() / ".pal")
    raw = ask("Where should Pal live?", default)
    return Path(raw).expanduser().resolve()


def prompt_identity(current: WizardIdentity | None = None) -> WizardIdentity:
    _print_step(1, 4, "Identity")

    display_name = ask("Pal's display name", current.display_name if current else "Pal")
    language = ask("Language (en, zh, ja, ...)", current.language if current else "en")
    timezone = ask("Timezone (blank for auto-detect)", current.timezone if current and current.timezone else "")

    print()
    vibe = _multiline_with_default("Pal's personality / vibe (blank to skip)", current.vibe if current else None)

    print()
    tone = _multiline_with_default("Communication tone (blank to skip)", current.tone if current else None)

    print()
    core_policy = _policy_with_default("Core policy rules (one per line, blank to skip)", current.core_policy if current else None)

    return WizardIdentity(
        display_name=display_name,
        language=language,
        vibe=vibe,
        tone=tone,
        core_policy=core_policy,
        timezone=timezone if timezone else time.tzname[0],
    )


def _prompt_one_endpoint(index: int, current: WizardLLMEndpoint | None = None) -> WizardLLMEndpoint | None:
    print(f"\n  Endpoint #{index}:")
    label = ask("  Label (e.g. my-claude, deepseek-chat)", current.endpoint_id if current else "")
    if not label:
        return None

    current_mode_choice = "2" if current and current.api_mode == "anthropic_messages" else "1"
    mode_choice = ask("  API mode: 1) openai_chat  2) anthropic_messages", current_mode_choice)
    api_mode = "openai_chat" if mode_choice.strip() != "2" else "anthropic_messages"

    model_id = ask("  Model ID", current.model_id if current else label)

    if api_mode == "openai_chat":
        default_url = "https://api.openai.com/v1"
    else:
        default_url = "https://api.anthropic.com/v1"
    base_url = ask("  Base URL", current.base_url if current else default_url)

    api_key = ask_password("  API key (hidden, blank to keep current)" if current else "  API key (hidden, blank to skip)")

    capabilities: dict[str, Any] | None = None
    if api_key:
        print("  Querying model metadata...")
        capabilities = query_model_metadata(base_url, model_id, api_key, api_mode)

    context_window: int | None = current.context_window if current else None
    max_output_tokens: int | None = current.max_output_tokens if current else None
    supports_reasoning = current.supports_reasoning if current else False
    supports_tools = current.supports_tools if current else True
    supports_streaming = current.supports_streaming if current else True
    supports_vision = current.supports_vision if current else False

    if capabilities:
        context_window = capabilities.get("context_length")
        max_output_tokens = capabilities.get("max_output_tokens")
        supports_reasoning = bool(capabilities.get("supports_thinking"))
        supports_vision = bool(capabilities.get("supports_vision"))
        supports_tools = capabilities.get("supports_tools", True)

        print(f"    Context window: {context_window or '?'} tokens")
        if max_output_tokens:
            print(f"    Max output: {max_output_tokens} tokens")
        print(f"    Reasoning: {'yes' if supports_reasoning else 'no'} | Vision: {'yes' if supports_vision else 'no'} | Tools: {'yes' if supports_tools else 'no'}")

        if not capabilities.get("context_length"):
            ctx = ask("  Context window size", "32768")
            context_window = int(ctx)

        if not ask_yes_no("  Confirm", True):
            capabilities = None

    if current is not None and not capabilities:
        if ask_yes_no("  Update model capability metadata", False):
            context_window = _prompt_int("  Context window size", context_window, context_window)
            max_output_tokens = _prompt_int("  Max output tokens (blank for current/default)", max_output_tokens, max_output_tokens)
            supports_reasoning = ask_yes_no("  Supports reasoning / thinking", supports_reasoning)
            supports_vision = ask_yes_no("  Supports vision (image input)", supports_vision)
            supports_tools = ask_yes_no("  Supports tool calling", supports_tools)
            supports_streaming = ask_yes_no("  Supports streaming", supports_streaming)
    elif not capabilities:
        print("  Could not query metadata. Enter manually.")
        context_window = _prompt_int("  Context window size", 32768, 32768)
        max_output_tokens = _prompt_int("  Max output tokens (blank for default)", None, None)
        supports_reasoning = ask_yes_no("  Supports reasoning / thinking", False)
        supports_vision = ask_yes_no("  Supports vision (image input)", False)
        supports_tools = ask_yes_no("  Supports tool calling", True)
        supports_streaming = ask_yes_no("  Supports streaming", True)

    provider = None
    credential_ref = None
    capabilities_blob: dict[str, Any] = {}
    notes = None
    if current is not None and label == current.endpoint_id and base_url == current.base_url and api_mode == current.api_mode:
        provider = current.provider
        credential_ref = current.credential_ref
        capabilities_blob = dict(current.capabilities_blob or {})
        notes = current.notes

    endpoint = WizardLLMEndpoint(
        endpoint_id=label,
        model_id=model_id,
        api_mode=api_mode,
        base_url=base_url,
        api_key=api_key,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        supports_reasoning=supports_reasoning,
        supports_tools=supports_tools,
        supports_streaming=supports_streaming,
        supports_vision=supports_vision,
        priority=0,
        provider=provider,
        credential_ref=credential_ref,
        capabilities_blob=capabilities_blob,
        notes=notes,
    )

    if ask_yes_no("  Run live LLM preflight now", current is None and bool(api_key)):
        result = run_llm_endpoint_preflight(endpoint)
        _print_llm_preflight_result(result)
        if result.status == "error":
            if not ask_yes_no("  Keep this endpoint anyway", False):
                return None
        elif result.status == "warn":
            if not ask_yes_no("  Keep this endpoint with warnings", True):
                return None

    return endpoint


def _parse_codex_model_list(raw: str) -> tuple[str, ...]:
    items: list[str] = []
    seen: set[str] = set()
    for chunk in str(raw or "").replace("\n", ",").split(","):
        model = chunk.strip()
        if not model or model in seen:
            continue
        seen.add(model)
        items.append(model)
    return tuple(items)


def _codex_endpoint_id(model_id: str) -> str:
    suffix = re.sub(r"[^a-zA-Z0-9]+", "_", str(model_id or "").strip()).strip("_").lower()
    return f"codex_{suffix or 'model'}"


def build_codex_wizard_endpoints(
    model_ids: tuple[str, ...] = DEFAULT_CODEX_WIZARD_MODELS,
) -> list[WizardLLMEndpoint]:
    endpoints: list[WizardLLMEndpoint] = []
    for priority, model_id in enumerate(model_ids):
        endpoints.append(
            WizardLLMEndpoint(
                endpoint_id=_codex_endpoint_id(model_id),
                model_id=model_id,
                api_mode="openai_chat",
                base_url="codex://cli",
                api_key=None,
                context_window=DEFAULT_CODEX_CONTEXT_WINDOW,
                max_output_tokens=DEFAULT_CODEX_MAX_OUTPUT_TOKENS,
                supports_reasoning=True,
                supports_tools=True,
                supports_streaming=True,
                supports_vision=True,
                priority=priority,
                provider="codex_cli",
                auth_kind="local_provider_auth",
                credential_ref="",
                capabilities_blob={
                    "official_codex_cli": True,
                    "codex_cli": True,
                    "native_tool_bridge": True,
                },
                notes="Configured by setup wizard. Uses local Codex CLI authentication.",
            )
        )
    return endpoints


def _prompt_codex_endpoints(index: int) -> list[WizardLLMEndpoint]:
    print(f"\n  Codex endpoint group #{index}:")
    codex_bin = shutil.which("codex")
    if codex_bin:
        print(f"  Codex CLI: {codex_bin}")
    else:
        print("  Codex CLI: not found on PATH; Pal will still try the usual nvm codex location at runtime.")

    default_models = ",".join(DEFAULT_CODEX_WIZARD_MODELS)
    raw_models = ask("  Models", default_models)
    model_ids = _parse_codex_model_list(raw_models) or DEFAULT_CODEX_WIZARD_MODELS
    endpoints = build_codex_wizard_endpoints(model_ids)

    print("  Will configure:")
    for endpoint in endpoints:
        print(f"    {endpoint.endpoint_id}: {endpoint.model_id}")

    if ask_yes_no("  Run live Codex preflight now (starts Codex app-server)", False):
        result = run_llm_endpoint_preflight(endpoints[0], timeout_seconds=60)
        _print_llm_preflight_result(result)
        if result.status == "error":
            if not ask_yes_no("  Keep these endpoints anyway", False):
                return []
        elif result.status == "warn":
            if not ask_yes_no("  Keep these endpoints with warnings", True):
                return []

    return endpoints


def _print_llm_preflight_result(result: WizardLLMPreflightResult) -> None:
    marker = {"ok": "OK", "warn": "WARN", "error": "ERR"}.get(result.status, result.status.upper())
    print(f"  [{marker}] LLM preflight: {result.detail}")


def run_llm_endpoint_preflight(
    endpoint: WizardLLMEndpoint,
    *,
    timeout_seconds: int = 20,
    invoker: object | None = None,
) -> WizardLLMPreflightResult:
    try:
        from pal.llm import CanonicalLLMRequest, LiteLLMCredentialResolver, build_default_endpoint_invoker
        from pal.llm.models import LLMEndpointModel
        from pal.llm.secret_store import InMemorySecretStore, SecretRef
    except Exception as exc:
        return WizardLLMPreflightResult(status="error", detail=f"could not load LLM runtime: {exc}")

    secret_store = InMemorySecretStore()
    credential_ref = endpoint.credential_ref if endpoint.credential_ref is not None else f"{endpoint.endpoint_id}:api-key"
    if endpoint.api_key:
        secret_store.set_secret(SecretRef(service=endpoint.endpoint_id, account="api-key"), endpoint.api_key)
    model = LLMEndpointModel(
        endpoint_id=endpoint.endpoint_id,
        provider=_infer_endpoint_provider(endpoint),
        model_id=endpoint.model_id,
        display_name=endpoint.endpoint_id,
        api_mode=endpoint.api_mode,
        base_url=endpoint.base_url,
        auth_kind=endpoint.auth_kind,
        credential_ref=credential_ref,
        context_window=endpoint.context_window,
        max_output_tokens=endpoint.max_output_tokens,
        supports_reasoning=endpoint.supports_reasoning,
        supports_tools=endpoint.supports_tools,
        supports_streaming=endpoint.supports_streaming,
        supports_vision=endpoint.supports_vision,
        input_modalities_blob=["text", "image"] if endpoint.supports_vision else ["text"],
        output_modalities_blob=["text"],
        priority=endpoint.priority,
        enabled=True,
        capabilities_blob=dict(endpoint.capabilities_blob or {}),
        notes="Setup preflight endpoint.",
    )
    active_invoker = invoker or build_default_endpoint_invoker(credentials=LiteLLMCredentialResolver(secret_store=secret_store))
    metadata = {"timeout_seconds": timeout_seconds}

    try:
        text_outcome = active_invoker.invoke(
            model,
            CanonicalLLMRequest(
                messages=[
                    {"role": "system", "content": "You validate Pal LLM endpoint setup."},
                    {"role": "user", "content": "Reply with exactly PAL_PREFLIGHT_OK."},
                ],
                max_output_tokens=16,
                temperature=0,
                metadata=metadata,
            ),
        )
    except Exception as exc:
        return WizardLLMPreflightResult(status="error", detail=f"text call failed: {exc}")

    text = str(getattr(text_outcome, "text", "") or "").strip()
    if not text and not getattr(text_outcome, "tool_calls", None):
        return WizardLLMPreflightResult(status="error", detail="text call returned no content")

    if not endpoint.supports_tools:
        return WizardLLMPreflightResult(
            status="warn",
            detail="text call succeeded, but this endpoint is configured without tool support",
            text_ok=True,
        )

    tools = [
        {
            "type": "function",
            "function": {
                "name": "pal_preflight_probe",
                "description": "Validate that this endpoint can emit a tool call.",
                "parameters": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                },
            },
        }
    ]
    try:
        tool_outcome = active_invoker.invoke(
            model,
            CanonicalLLMRequest(
                messages=[
                    {"role": "system", "content": "You validate Pal LLM tool calling setup."},
                    {"role": "user", "content": "Call pal_preflight_probe with ok=true. Do not answer in prose."},
                ],
                max_output_tokens=64,
                temperature=0,
                tools=tools,
                metadata=metadata,
            ),
        )
    except Exception as exc:
        return WizardLLMPreflightResult(status="warn", detail=f"text call succeeded, tool probe failed: {exc}", text_ok=True)

    tool_ok = any(str(call.name) == "pal_preflight_probe" for call in list(getattr(tool_outcome, "tool_calls", None) or []))
    if not tool_ok:
        return WizardLLMPreflightResult(
            status="warn",
            detail="text call succeeded, but tool probe returned no tool call",
            text_ok=True,
        )
    return WizardLLMPreflightResult(status="ok", detail="text and tool calls succeeded", text_ok=True, tool_ok=True)


def _infer_endpoint_provider(endpoint: WizardLLMEndpoint) -> str:
    if endpoint.provider:
        return endpoint.provider
    if str(endpoint.base_url or "").strip().lower().startswith("codex://"):
        return "codex_cli"
    if "anthropic" in endpoint.api_mode:
        return "anthropic"
    if "openai" in endpoint.api_mode or endpoint.api_mode == "openai_chat":
        base_url = endpoint.base_url.lower()
        if "deepseek" in base_url:
            return "deepseek"
        if "zhipu" in base_url or "z.ai" in base_url or "bigmodel.cn" in base_url:
            return "zhipu"
        if "moonshot" in base_url or "kimi" in base_url:
            return "moonshot"
        return "openai"
    return endpoint.endpoint_id


def _append_prompted_endpoints(endpoints: list[WizardLLMEndpoint], *, start_index: int) -> None:
    idx = 1
    if start_index > 1:
        idx = start_index
    while True:
        default_choice = "1" if not endpoints else "3"
        source_choice = ask(
            "  Endpoint source:\n"
            "    1) Codex CLI subscription\n"
            "    2) API-compatible endpoint\n"
            "    3) Done",
            default_choice,
        ).strip()
        if source_choice == "3":
            if not endpoints:
                print("  At least one endpoint is required.")
                continue
            break
        if source_choice == "1":
            codex_endpoints = _prompt_codex_endpoints(idx)
            if not codex_endpoints:
                if not endpoints:
                    print("  At least one endpoint is required.")
                    continue
            endpoints.extend(codex_endpoints)
            idx += 1
        else:
            ep = _prompt_one_endpoint(idx)
            if ep is None:
                if not endpoints:
                    print("  At least one endpoint is required.")
                    continue
                break
            endpoints.append(ep)
            idx += 1
        if not ask_yes_no("  Add another endpoint?", True):
            break


def prompt_llm_endpoints() -> tuple[list[WizardLLMEndpoint], str]:
    return prompt_llm_endpoints_with_current()


def prompt_llm_endpoints_with_current(
    current_endpoints: list[WizardLLMEndpoint] | None = None,
    current_active_endpoint_id: str | None = None,
) -> tuple[list[WizardLLMEndpoint], str]:
    _print_step(2, 4, "LLM Endpoints")
    print("(Codex uses your local Codex CLI subscription login.)")
    print("(OpenAI and Anthropic are API formats; compatible providers also work.)\n")

    endpoints: list[WizardLLMEndpoint] = []
    current_endpoints = list(current_endpoints or [])
    if current_endpoints:
        print("  Existing endpoints:")
        for current in current_endpoints:
            active_marker = " [active]" if current.endpoint_id == current_active_endpoint_id else ""
            print(f"    {current.endpoint_id}: {current.model_id} ({current.provider or current.api_mode}){active_marker}")
        for current in current_endpoints:
            if not ask_yes_no(f"  Keep endpoint {current.endpoint_id}", True):
                continue
            if ask_yes_no(f"  Edit endpoint {current.endpoint_id}", False):
                edited = _prompt_one_endpoint(len(endpoints) + 1, current)
                if edited is not None:
                    endpoints.append(edited)
            else:
                endpoints.append(current)
        if ask_yes_no("  Add another endpoint?", False):
            _append_prompted_endpoints(endpoints, start_index=len(endpoints) + 1)
        if not endpoints:
            print("  At least one endpoint is required.")
            _append_prompted_endpoints(endpoints, start_index=1)
    else:
        _append_prompted_endpoints(endpoints, start_index=1)

    if len(endpoints) > 1:
        print("\n  Priority order (lower = higher priority):")
        for i, ep in enumerate(endpoints, 1):
            print(f"    {i}. {ep.endpoint_id} ({ep.model_id})")
        if ask_yes_no("  Reorder?", False):
            order_str = ask(
                "  Enter new order (comma-separated indices)",
                ",".join(str(i) for i in range(1, len(endpoints) + 1)),
            )
            try:
                indices = [int(x.strip()) - 1 for x in order_str.split(",")]
                endpoints = [endpoints[i] for i in indices if 0 <= i < len(endpoints)]
            except (ValueError, IndexError):
                print("  Invalid order, keeping current.")

    for i, ep in enumerate(endpoints):
        ep.priority = i

    print("\n  Which endpoint should be active?")
    for i, ep in enumerate(endpoints, 1):
        print(f"    {i}. {ep.endpoint_id} ({ep.model_id})")
    default_active_index = 1
    if current_active_endpoint_id:
        for i, ep in enumerate(endpoints, 1):
            if ep.endpoint_id == current_active_endpoint_id:
                default_active_index = i
                break
    active_idx = ask("  Choice", str(default_active_index))
    try:
        active_endpoint_id = endpoints[int(active_idx) - 1].endpoint_id
    except (ValueError, IndexError):
        active_endpoint_id = endpoints[0].endpoint_id

    return endpoints, active_endpoint_id


def prompt_channel(runtime_root: Path, current: WizardChannel | None = None) -> WizardChannel:
    _print_step(3, 4, "Channel")

    if current is not None:
        print(f"  Existing channel: {current.channel_kind} ({current.binding_key})")
        if ask_yes_no("  Keep current channel", True):
            if not ask_yes_no("  Edit current channel", False):
                return current

    choice = ask(
        "How will you interact with Pal?\n"
        "  1) Socket (pal run + pal client)\n"
        "  2) Telegram bot",
        "2" if current and current.channel_kind == "telegram" else "1",
    )

    if choice.strip() == "2":
        endpoint_id = ask("  Endpoint ID", current.endpoint_id if current and current.channel_kind == "telegram" else "telegram_main")
        existing_token = ""
        if current and current.channel_kind == "telegram":
            existing_token = str(current.binding_metadata.get("bot_token") or "")
        bot_token = ask("  Bot token (blank to keep current)", "") if existing_token else ""
        while not bot_token:
            if existing_token:
                bot_token = existing_token
                break
            bot_token = ask("  Bot token", "").strip()
            if not bot_token:
                print("  Bot token is required for Telegram.")
        binding_default = current.binding_key if current and current.channel_kind == "telegram" else "user:me"
        binding_key = normalize_telegram_binding_key(ask("  Binding key (e.g. user:12345 or chat:-10012345)", binding_default))
        return WizardChannel(
            endpoint_id=endpoint_id,
            channel_kind="telegram",
            binding_key=binding_key,
            binding_metadata={"bot_token": bot_token},
            supports_typing=True,
            supports_receipt_marker=True,
        )

    endpoint_id = ask("  Endpoint ID", current.endpoint_id if current and current.channel_kind == "socket" else "socket_default")
    socket_default = current.binding_key if current and current.channel_kind == "socket" else str(runtime_root / "pal.sock")
    socket_path = ask("  Socket path", socket_default)
    return WizardChannel(
        endpoint_id=endpoint_id,
        channel_kind="socket",
        binding_key=socket_path,
    )


def prompt_review(data: WizardCollectedData, runtime_root: Path) -> bool:
    _print_step(4, 4, "Review")

    id = data.identity
    print(f"  Home:        {runtime_root}")
    print(f"  Name:        {id.display_name}")
    print(f"  Language:    {id.language}")
    print(f"  Timezone:    {id.timezone or 'auto'}")
    if id.vibe:
        print(f"  Vibe:        {id.vibe[:80]}{'...' if len(id.vibe) > 80 else ''}")
    if id.tone:
        print(f"  Tone:        {id.tone[:80]}{'...' if len(id.tone) > 80 else ''}")
    if id.core_policy:
        print(f"  Policy:      {len(id.core_policy)} rule(s)")

    print()
    for i, ep in enumerate(data.endpoints, 1):
        active_marker = " [active]" if ep.endpoint_id == data.active_endpoint_id else ""
        provider_label = ep.provider or ep.api_mode
        print(f"  Endpoint {i}: {ep.endpoint_id} ({ep.model_id}, {provider_label}){active_marker}")
        print(f"    URL: {ep.base_url}")
        print(f"    Context: {ep.context_window or '?'} | Reasoning: {'yes' if ep.supports_reasoning else 'no'} | Vision: {'yes' if ep.supports_vision else 'no'}")

    print()
    ch = data.channel
    if ch.channel_kind == "telegram":
        print(f"  Channel:     telegram ({ch.binding_key})")
    else:
        print(f"  Channel:     socket ({ch.binding_key})")

    print()
    return ask_yes_no("  Proceed?", True)


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------

def run_interactive_wizard(
    *,
    existing_loader: Callable[[Path], WizardCollectedData | None] | None = None,
) -> tuple[Path, WizardCollectedData] | None:
    print("\n=== Pal Setup ===\n")

    runtime_root = prompt_runtime_home()
    current = existing_loader(runtime_root) if existing_loader is not None else None
    if current is not None:
        print(f"\n  Existing Pal runtime detected at {runtime_root}; current values will be used as defaults.")
    identity = prompt_identity(current.identity if current else None)
    endpoints, active_endpoint_id = prompt_llm_endpoints_with_current(
        current.endpoints if current else None,
        current.active_endpoint_id if current else None,
    )
    channel = prompt_channel(runtime_root, current.channel if current else None)

    data = WizardCollectedData(
        identity=identity,
        endpoints=endpoints,
        channel=channel,
        active_endpoint_id=active_endpoint_id,
    )

    if not prompt_review(data, runtime_root):
        print("\n  Setup cancelled.")
        return None

    return runtime_root, data
