"""Interactive setup wizard for Pal.

Standalone prompts and data collection — no database or repository imports.
The I/O layer produces pure data objects that WizardService.seed_from_wizard()
persists through repositories.
"""

from __future__ import annotations

import getpass
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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

def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]: " if default else ": "
    raw = input(prompt + suffix).strip()
    return raw if raw else default


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    raw = input(prompt + suffix).strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def ask_password(prompt: str) -> str | None:
    raw = getpass.getpass(prompt + ": ").strip()
    return raw if raw else None


def multiline_input(prompt: str, sentinel: str = ".") -> str:
    print(f"{prompt} (enter '{sentinel}' on its own line to finish)")
    lines: list[str] = []
    while True:
        try:
            line = input("> ")
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


# ---------------------------------------------------------------------------
# Prompt steps
# ---------------------------------------------------------------------------

def _print_step(step: int, total: int, title: str) -> None:
    print(f"\n{'=' * 50}")
    print(f"  Step {step}/{total}: {title}")
    print(f"{'=' * 50}\n")


def prompt_runtime_home() -> Path:
    default = str(Path.home() / ".pal")
    raw = ask("Where should Pal live?", default)
    return Path(raw).expanduser().resolve()


def prompt_identity() -> WizardIdentity:
    _print_step(1, 4, "Identity")

    display_name = ask("Pal's display name", "Pal")
    language = ask("Language (en, zh, ja, ...)", "en")
    timezone = ask("Timezone (blank for auto-detect)", "")

    print()
    vibe = multiline_input("Pal's personality / vibe (blank to skip)")
    if not vibe.strip():
        vibe = None

    print()
    tone = multiline_input("Communication tone (blank to skip)")
    if not tone.strip():
        tone = None

    print()
    policy_text = multiline_input("Core policy rules (one per line, blank to skip)")
    core_policy = [line for line in policy_text.splitlines() if line.strip()]

    return WizardIdentity(
        display_name=display_name,
        language=language,
        vibe=vibe,
        tone=tone,
        core_policy=core_policy,
        timezone=timezone if timezone else time.tzname[0],
    )


def _prompt_one_endpoint(index: int) -> WizardLLMEndpoint | None:
    print(f"\n  Endpoint #{index}:")
    label = ask("  Label (e.g. my-claude, deepseek-chat)", "")
    if not label:
        return None

    mode_choice = ask("  API mode: 1) openai_chat  2) anthropic_messages", "1")
    api_mode = "openai_chat" if mode_choice.strip() != "2" else "anthropic_messages"

    model_id = ask("  Model ID", label)

    if api_mode == "openai_chat":
        default_url = "https://api.openai.com/v1/chat/completions"
    else:
        default_url = "https://api.anthropic.com/v1/messages"
    base_url = ask("  Base URL", default_url)

    api_key = ask_password("  API key (hidden, blank to skip)")

    capabilities: dict[str, Any] | None = None
    if api_key:
        print("  Querying model metadata...")
        capabilities = query_model_metadata(base_url, model_id, api_key, api_mode)

    context_window: int | None = None
    max_output_tokens: int | None = None
    supports_reasoning = False
    supports_tools = True
    supports_streaming = True
    supports_vision = False

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

    if not capabilities:
        print("  Could not query metadata. Enter manually.")
        ctx = ask("  Context window size", "32768")
        context_window = int(ctx)
        max_output_tokens_raw = ask("  Max output tokens (blank for default)", "")
        max_output_tokens = int(max_output_tokens_raw) if max_output_tokens_raw else None
        supports_reasoning = ask_yes_no("  Supports reasoning / thinking", False)
        supports_vision = ask_yes_no("  Supports vision (image input)", False)
        supports_tools = ask_yes_no("  Supports tool calling", True)
        supports_streaming = ask_yes_no("  Supports streaming", True)

    return WizardLLMEndpoint(
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
    )


def prompt_llm_endpoints() -> tuple[list[WizardLLMEndpoint], str]:
    _print_step(2, 4, "LLM Endpoints")
    print("(OpenAI and Anthropic are API formats, not model names.)")
    print("(Any model with a compatible API works.)\n")

    endpoints: list[WizardLLMEndpoint] = []
    idx = 1
    while True:
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
    active_idx = ask("  Choice", "1")
    try:
        active_endpoint_id = endpoints[int(active_idx) - 1].endpoint_id
    except (ValueError, IndexError):
        active_endpoint_id = endpoints[0].endpoint_id

    return endpoints, active_endpoint_id


def prompt_channel(runtime_root: Path) -> WizardChannel:
    _print_step(3, 4, "Channel")

    choice = ask(
        "How will you interact with Pal?\n"
        "  1) Socket (pal run + pal client)\n"
        "  2) Telegram bot",
        "1",
    )

    if choice.strip() == "2":
        bot_token = ask("  Bot token", "")
        binding_key = ask("  Binding key (e.g. chat:12345)", "user:me")
        return WizardChannel(
            endpoint_id="telegram_main",
            channel_kind="telegram",
            binding_key=binding_key,
            binding_metadata={"bot_token": bot_token},
            supports_typing=True,
            supports_receipt_marker=True,
        )

    socket_path = ask("  Socket path", str(runtime_root / "pal.sock"))
    return WizardChannel(
        endpoint_id="socket_default",
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
        print(f"  Endpoint {i}: {ep.endpoint_id} ({ep.model_id}){active_marker}")
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

def run_interactive_wizard() -> tuple[Path, WizardCollectedData] | None:
    print("\n=== Pal Setup ===\n")

    runtime_root = prompt_runtime_home()
    identity = prompt_identity()
    endpoints, active_endpoint_id = prompt_llm_endpoints()
    channel = prompt_channel(runtime_root)

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
