"""Interactive setup wizard for Pal.

Standalone prompts and data collection — no database or repository imports.
The I/O layer produces pure data objects that WizardService.seed_from_wizard()
persists through repositories.
"""

from __future__ import annotations

from pal.shared.tool_protocol import ToolDefinitionIR

import getpass
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pal.wizard.config_file import DEFAULT_MEMORY_EMBEDDING_MODEL


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

WIZARD_STEP_TOTAL = 5


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
    wire_shape: str,
) -> dict[str, Any] | None:
    if wire_shape == "anthropic_messages":
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
    wire_shape: str
    base_url: str
    api_key: str | None
    context_window: int | None
    max_output_tokens: int | None
    thinking_levels: list[str]
    default_thinking_level: str
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
class WizardMemoryEmbedding:
    remote_ollama_base_urls: list[str] = field(default_factory=list)
    model_name: str = DEFAULT_MEMORY_EMBEDDING_MODEL


@dataclass
class WizardCollectedData:
    identity: WizardIdentity
    endpoints: list[WizardLLMEndpoint]
    channel: WizardChannel
    active_endpoint_id: str
    memory_embedding: WizardMemoryEmbedding = field(default_factory=WizardMemoryEmbedding)


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


def _prompt_thinking_levels(
    current_levels: list[str],
    current_default: str,
    *,
    wire_shape: str = "openai_completion",
) -> tuple[list[str], str]:
    from pal.llm.endpoint_spec import LLMEndpointSpecError, validate_thinking_levels

    while True:
        raw = ask("  Thinking levels (comma-separated enum values)", ",".join(current_levels or ["off"]))
        try:
            levels = list(validate_thinking_levels(raw.split(","), wire_shape=wire_shape))
        except LLMEndpointSpecError as exc:
            print(f"  Invalid thinking levels: {exc}")
            continue
        default = ask(
            "  Default thinking level",
            current_default if current_default in levels else levels[0],
        ).strip().lower()
        if default not in levels:
            print(f"  Default thinking level must be one of the declared levels; available: {', '.join(levels)}")
            continue
        return levels, default


def prompt_runtime_home() -> Path:
    default = str(Path.home() / ".pal")
    raw = ask("Where should Pal live?", default)
    return Path(raw).expanduser().resolve()


def prompt_identity(current: WizardIdentity | None = None) -> WizardIdentity:
    _print_step(1, WIZARD_STEP_TOTAL, "Identity")

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

    current_shape_choice = {
        "openai_completion": "1",
        "openai_response": "2",
        "anthropic_messages": "3",
    }.get(current.wire_shape if current else "", "1")
    shape_choice = ask(
        "  Wire shape: 1) openai_completion  2) openai_response  3) anthropic_messages",
        current_shape_choice,
    )
    wire_shape = {
        "2": "openai_response",
        "3": "anthropic_messages",
    }.get(shape_choice.strip(), "openai_completion")

    model_id = ask("  Model ID", current.model_id if current else label)

    if wire_shape in {"openai_completion", "openai_response"}:
        default_url = "https://api.openai.com/v1"
    else:
        default_url = "https://api.anthropic.com/v1"
    base_url = ask("  Base URL", current.base_url if current else default_url)

    api_key = ask_password("  API key (hidden, blank to keep current)" if current else "  API key (hidden, blank to skip)")

    capabilities: dict[str, Any] | None = None
    if api_key:
        print("  Querying model metadata...")
        capabilities = query_model_metadata(base_url, model_id, api_key, wire_shape)

    context_window: int | None = current.context_window if current else None
    max_output_tokens: int | None = current.max_output_tokens if current else None
    thinking_levels = list(current.thinking_levels) if current else ["off"]
    default_thinking_level = current.default_thinking_level if current else "off"
    supports_tools = current.supports_tools if current else True
    supports_streaming = current.supports_streaming if current else True
    supports_vision = current.supports_vision if current else False

    if capabilities:
        context_window = capabilities.get("context_length")
        max_output_tokens = capabilities.get("max_output_tokens")
        if bool(capabilities.get("supports_thinking")):
            thinking_levels = ["off", "low", "medium", "high"]
            default_thinking_level = "medium"
        else:
            thinking_levels = ["off"]
            default_thinking_level = "off"
        supports_vision = bool(capabilities.get("supports_vision"))
        supports_tools = capabilities.get("supports_tools", True)

        print(f"    Context window: {context_window or '?'} tokens")
        if max_output_tokens:
            print(f"    Max output: {max_output_tokens} tokens")
        print(f"    Thinking: {', '.join(thinking_levels)} | Vision: {'yes' if supports_vision else 'no'} | Tools: {'yes' if supports_tools else 'no'}")

        if not capabilities.get("context_length"):
            ctx = ask("  Context window size", "32768")
            context_window = int(ctx)

        if not ask_yes_no("  Confirm", True):
            capabilities = None

    if current is not None and not capabilities:
        if ask_yes_no("  Update model capability metadata", False):
            context_window = _prompt_int("  Context window size", context_window, context_window)
            max_output_tokens = _prompt_int("  Max output tokens (blank for current/default)", max_output_tokens, max_output_tokens)
            thinking_levels, default_thinking_level = _prompt_thinking_levels(
                thinking_levels,
                default_thinking_level,
                wire_shape=wire_shape,
            )
            supports_vision = ask_yes_no("  Supports vision (image input)", supports_vision)
            supports_tools = ask_yes_no("  Supports tool calling", supports_tools)
            supports_streaming = ask_yes_no("  Supports streaming", supports_streaming)
    elif not capabilities:
        print("  Could not query metadata. Enter manually.")
        context_window = _prompt_int("  Context window size", 32768, 32768)
        max_output_tokens = _prompt_int("  Max output tokens (blank for default)", None, None)
        thinking_levels, default_thinking_level = _prompt_thinking_levels(["off"], "off", wire_shape=wire_shape)
        supports_vision = ask_yes_no("  Supports vision (image input)", False)
        supports_tools = ask_yes_no("  Supports tool calling", True)
        supports_streaming = ask_yes_no("  Supports streaming", True)

    provider = None
    credential_ref = None
    capabilities_blob: dict[str, Any] = {}
    notes = None
    if current is not None and label == current.endpoint_id and base_url == current.base_url and wire_shape == current.wire_shape:
        provider = current.provider
        credential_ref = current.credential_ref
        capabilities_blob = dict(current.capabilities_blob or {})
        notes = current.notes

    endpoint = WizardLLMEndpoint(
        endpoint_id=label,
        model_id=model_id,
        wire_shape=wire_shape,
        base_url=base_url,
        api_key=api_key,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        thinking_levels=thinking_levels,
        default_thinking_level=default_thinking_level,
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


def _print_llm_preflight_result(result: WizardLLMPreflightResult) -> None:
    marker = {"ok": "OK", "warn": "WARN", "error": "ERR"}.get(result.status, result.status.upper())
    print(f"  [{marker}] LLM preflight: {result.detail}")


def run_llm_endpoint_preflight(
    endpoint: WizardLLMEndpoint,
    *,
    timeout_seconds: int = 20,
    invoker: object | None = None,
    secret_store: object | None = None,
) -> WizardLLMPreflightResult:
    try:
        from pal.llm.credentials import LLMCredentialResolver
        from pal.llm.endpoint import ShapeEndpointInvoker
        from pal.llm.ir import (
            GenerationPolicyIR,
            LLMMessageIR,
            LLMRequestIR,
            MessageRole,
            TextPartIR,
        )
        from pal.llm.models import LLMEndpointModel
        from pal.llm.secret_store import InMemorySecretStore, SecretRef
    except Exception as exc:
        return WizardLLMPreflightResult(status="error", detail=f"could not load LLM runtime: {exc}")

    secret_store = secret_store or InMemorySecretStore()
    credential_ref = endpoint.credential_ref if endpoint.credential_ref is not None else f"{endpoint.endpoint_id}:api-key"
    if endpoint.api_key:
        secret_store.set_secret(SecretRef(service=endpoint.endpoint_id, account="api-key"), endpoint.api_key)
    model = LLMEndpointModel(
        endpoint_id=endpoint.endpoint_id,
        provider=_infer_endpoint_provider(endpoint),
        model_id=endpoint.model_id,
        display_name=endpoint.endpoint_id,
        wire_shape=endpoint.wire_shape,
        base_url=endpoint.base_url,
        auth_kind=endpoint.auth_kind,
        credential_ref=credential_ref,
        context_window=endpoint.context_window,
        max_output_tokens=endpoint.max_output_tokens,
        thinking_levels_blob=list(endpoint.thinking_levels),
        default_thinking_level=endpoint.default_thinking_level,
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
    credentials = LLMCredentialResolver(secret_store=secret_store)
    active_invoker = invoker or ShapeEndpointInvoker(
        credential_resolver=credentials.resolve_api_key
    )
    from pal.llm.chatgpt import is_chatgpt
    thinking = endpoint.default_thinking_level if is_chatgpt(endpoint) else None

    try:
        try:
            text_outcome = active_invoker.invoke(
                model,
                LLMRequestIR(
                    messages=(
                        LLMMessageIR(MessageRole.SYSTEM, (TextPartIR("You validate Pal LLM endpoint setup."),)),
                        LLMMessageIR(MessageRole.USER, (TextPartIR("Reply with exactly PAL_PREFLIGHT_OK."),)),
                    ),
                    tools=(),
                    policy=GenerationPolicyIR(max_output_tokens=16, temperature=0, thinking_level=thinking),
                ),
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            return WizardLLMPreflightResult(status="error", detail=f"text call failed: {exc}")

        text_response = text_outcome[0] if isinstance(text_outcome, tuple) else text_outcome
        from pal.shared import LLMFinishReason
        if is_chatgpt(endpoint) and text_response.finish_reason != LLMFinishReason.STOP:
            return WizardLLMPreflightResult(status="error", detail="Text probe did not complete")
        text = str(getattr(getattr(text_response, "message", None), "text", "") or "").strip()
        if not text and not getattr(getattr(text_response, "message", None), "tool_calls", None):
            return WizardLLMPreflightResult(status="error", detail="text call returned no content")

        if not endpoint.supports_tools:
            return WizardLLMPreflightResult(
                status="warn",
                detail="text call succeeded, but this endpoint is configured without tool support",
                text_ok=True,
            )

        tools = (
            ToolDefinitionIR(
                name="pal_preflight_probe",
                description="Validate that this endpoint can emit a tool call.",
                input_schema={
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                },
            ),
        )
        tool_request = LLMRequestIR(
            messages=(
                LLMMessageIR(MessageRole.SYSTEM, (TextPartIR("You validate Pal LLM tool calling setup."),)),
                LLMMessageIR(MessageRole.USER, (TextPartIR("Call pal_preflight_probe with ok=true. After its result, reply PAL_PREFLIGHT_OK."),)),
            ),
            tools=tools,
            policy=GenerationPolicyIR(max_output_tokens=64, temperature=0, thinking_level=thinking),
        )
        try:
            tool_outcome = active_invoker.invoke(
                model,
                tool_request,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            return WizardLLMPreflightResult(status="warn", detail=f"text call succeeded, tool probe failed: {exc}", text_ok=True)

        tool_response = tool_outcome[0] if isinstance(tool_outcome, tuple) else tool_outcome
        if is_chatgpt(endpoint) and tool_response.finish_reason != LLMFinishReason.TOOL_CALLS:
            return WizardLLMPreflightResult(status="error", detail="Tool probe did not complete", text_ok=True)
        tool_ok = any(
            str(call.name) == "pal_preflight_probe"
            for call in getattr(getattr(tool_response, "message", None), "tool_calls", ())
        )
        if not tool_ok:
            return WizardLLMPreflightResult(
                status="warn",
                detail="text call succeeded, but tool probe returned no tool call",
                text_ok=True,
            )
        if is_chatgpt(endpoint):
            if any(call.name != "pal_preflight_probe" or dict(call.arguments) != {"ok": True}
                   for call in tool_response.message.tool_calls):
                return WizardLLMPreflightResult(status="error", detail="Unexpected probe tool or arguments", text_ok=True)
            from pal.shared.tool_protocol import ToolResultIR
            try:
                followup = active_invoker.invoke(model, LLMRequestIR(
                    messages=(
                        *tool_request.messages,
                        tool_response.message,
                        LLMMessageIR(MessageRole.TOOL, tuple(
                            ToolResultIR(item.call_id, item.name, '{"ok":true}')
                            for item in tool_response.message.tool_calls
                        )),
                    ), tools=tools, policy=GenerationPolicyIR(max_output_tokens=64, tool_choice="none", thinking_level=thinking),
                ), timeout_seconds=timeout_seconds)
                final = followup[0] if isinstance(followup, tuple) else followup
                if final.finish_reason != LLMFinishReason.STOP or not final.message.text.strip():
                    raise ValueError("probe did not complete")
            except Exception as exc:
                return WizardLLMPreflightResult(status="error", detail=f"Tool round trip failed: {exc}", text_ok=True)
        return WizardLLMPreflightResult(status="ok", detail="text and tool calls succeeded", text_ok=True, tool_ok=True)
    finally:
        if invoker is None:
            active_invoker.close()


def _infer_endpoint_provider(endpoint: WizardLLMEndpoint) -> str:
    if endpoint.provider:
        return endpoint.provider
    if endpoint.wire_shape == "anthropic_messages":
        return "anthropic"
    if endpoint.wire_shape in {"openai_completion", "openai_response"}:
        base_url = endpoint.base_url.lower()
        if "deepseek" in base_url:
            return "deepseek"
        if "zhipu" in base_url or "z.ai" in base_url or "bigmodel.cn" in base_url:
            return "zhipu"
        if "moonshot" in base_url or "kimi" in base_url:
            return "moonshot"
        return "openai"
    return endpoint.endpoint_id


def _append_prompted_endpoints(endpoints: list[WizardLLMEndpoint], *, start_index: int, runtime_root: Path | None = None) -> None:
    idx = 1
    if start_index > 1:
        idx = start_index
    while True:
        default_choice = "1" if not endpoints else "2"
        source_choice = ask(
            "  Endpoint source:\n"
            "    1) API endpoint\n"
            "    2) Done\n"
            "    3) Use ChatGPT plan / manage accounts",
            default_choice,
        ).strip()
        if source_choice == "2":
            if not endpoints:
                print("  At least one endpoint is required.")
                continue
            break
        if source_choice == "3":
            ep = prompt_chatgpt_endpoint(runtime_root)
            if ep is None:
                continue
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
    *, runtime_root: Path | None = None,
) -> tuple[list[WizardLLMEndpoint], str]:
    _print_step(2, WIZARD_STEP_TOTAL, "LLM Endpoints")
    print("(OpenAI Completion, OpenAI Responses, and Anthropic Messages wire shapes are supported.)\n")

    endpoints: list[WizardLLMEndpoint] = []
    current_endpoints = list(current_endpoints or [])
    if current_endpoints:
        print("  Existing endpoints:")
        for current in current_endpoints:
            active_marker = " [active]" if current.endpoint_id == current_active_endpoint_id else ""
            print(f"    {current.endpoint_id}: {current.model_id} ({current.wire_shape}){active_marker}")
        for current in current_endpoints:
            if not ask_yes_no(f"  Keep endpoint {current.endpoint_id}", True):
                continue
            if ask_yes_no(f"  Edit endpoint {current.endpoint_id}", False):
                from pal.llm.chatgpt import is_chatgpt
                if is_chatgpt(current):
                    edited = prompt_chatgpt_endpoint(runtime_root, current)
                else:
                    edited = _prompt_one_endpoint(len(endpoints) + 1, current)
                endpoints.append(edited if edited is not None else current)
            else:
                endpoints.append(current)
        if ask_yes_no("  Add another endpoint?", False):
            _append_prompted_endpoints(endpoints, start_index=len(endpoints) + 1, runtime_root=runtime_root)
        if not endpoints:
            print("  At least one endpoint is required.")
            _append_prompted_endpoints(endpoints, start_index=1, runtime_root=runtime_root)
    else:
        _append_prompted_endpoints(endpoints, start_index=1, runtime_root=runtime_root)

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
    _print_step(3, WIZARD_STEP_TOTAL, "Channel")

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


def _parse_remote_ollama_urls(raw: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for chunk in str(raw or "").replace("\n", ",").split(","):
        url = chunk.strip().rstrip("/")
        if not url:
            continue
        if "://" not in url:
            url = f"http://{url}"
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def prompt_memory_embedding(current: WizardMemoryEmbedding | None = None) -> WizardMemoryEmbedding:
    _print_step(4, WIZARD_STEP_TOTAL, "Memory Embeddings")
    current_urls = list(current.remote_ollama_base_urls if current else [])
    current_model = current.model_name if current and current.model_name else DEFAULT_MEMORY_EMBEDDING_MODEL

    print("  Memory embeddings use Ollama bge-m3 by default.")
    print("  Remote endpoints, when configured, are tried before local Ollama.")
    if current_urls:
        print("  Current remote endpoints:")
        for url in current_urls:
            print(f"    {url}")
    print()

    remote_urls: list[str] = []
    if ask_yes_no("  Use remote Ollama before local fallback", bool(current_urls)):
        raw = ask(
            "  Remote Ollama base URL(s), comma-separated",
            ", ".join(current_urls),
        )
        remote_urls = _parse_remote_ollama_urls(raw)
        if not remote_urls:
            print("  No remote URL entered; memory embeddings will use local Ollama only.")

    model_name = ask("  Embedding model", current_model) or DEFAULT_MEMORY_EMBEDDING_MODEL
    return WizardMemoryEmbedding(remote_ollama_base_urls=remote_urls, model_name=model_name)


def prompt_review(data: WizardCollectedData, runtime_root: Path) -> bool:
    _print_step(5, WIZARD_STEP_TOTAL, "Review")

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
        provider_label = ep.provider or ep.wire_shape
        print(f"  Endpoint {i}: {ep.endpoint_id} ({ep.model_id}, {provider_label}){active_marker}")
        print(f"    URL: {ep.base_url}")
        print(f"    Context: {ep.context_window or '?'} | Thinking: {', '.join(ep.thinking_levels)} | Vision: {'yes' if ep.supports_vision else 'no'}")

    print()
    ch = data.channel
    if ch.channel_kind == "telegram":
        print(f"  Channel:     telegram ({ch.binding_key})")
    else:
        print(f"  Channel:     socket ({ch.binding_key})")
    recovery_socket_path = str(runtime_root / "pal.sock")
    if ch.channel_kind != "socket" or ch.binding_key != recovery_socket_path:
        print(f"  Recovery:    socket ({recovery_socket_path})")

    print()
    mem = data.memory_embedding
    if mem.remote_ollama_base_urls:
        print(f"  Embedding:   remote Ollama -> local fallback ({mem.model_name})")
        for url in mem.remote_ollama_base_urls:
            print(f"    Remote:    {url}")
    else:
        print(f"  Embedding:   local Ollama ({mem.model_name})")

    print()
    return ask_yes_no("  Proceed?", True)


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------

def run_interactive_wizard(
    *,
    existing_loader: Callable[[Path], WizardCollectedData | None] | None = None,
    runtime_root: Path | None = None,
) -> tuple[Path, WizardCollectedData] | None:
    print("\n=== Pal Setup ===\n")

    runtime_root = (
        prompt_runtime_home()
        if runtime_root is None
        else Path(runtime_root).expanduser().resolve()
    )
    current = existing_loader(runtime_root) if existing_loader is not None else None
    if current is not None:
        print(f"\n  Existing Pal runtime detected at {runtime_root}; current values will be used as defaults.")
    identity = prompt_identity(current.identity if current else None)
    endpoints, active_endpoint_id = prompt_llm_endpoints_with_current(
        current.endpoints if current else None,
        current.active_endpoint_id if current else None,
        runtime_root=runtime_root,
    )
    channel = prompt_channel(runtime_root, current.channel if current else None)
    memory_embedding = prompt_memory_embedding(current.memory_embedding if current else None)

    data = WizardCollectedData(
        identity=identity,
        endpoints=endpoints,
        channel=channel,
        active_endpoint_id=active_endpoint_id,
        memory_embedding=memory_embedding,
    )

    if not prompt_review(data, runtime_root):
        print("\n  Setup cancelled.")
        return None

    return runtime_root, data


def _prompt_chatgpt_login(service, client_id: str | None = None) -> dict[str, Any]:
    import os
    import shlex
    import socket

    remote = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY")) or (
        sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    )
    mode = ask("  Continue with ChatGPT: 1) Browser on this computer  2) Remote / headless", "2" if remote else "1")
    if mode == "1":
        print("  Continue with ChatGPT in this computer's browser.")
        return service.login(client_id)
    if mode != "2":
        raise ValueError("Invalid login mode")
    connection = os.environ.get("SSH_CONNECTION", "").split()
    host = connection[2] if len(connection) == 4 else socket.gethostname()
    ssh_port = connection[3] if len(connection) == 4 else "22"
    destination = ask("  SSH destination reachable from your browser computer (user@host or SSH alias)",
                      f"{getpass.getuser()}@{host}").strip()
    if not destination or destination.startswith("-") or any(ord(char) < 32 for char in destination):
        raise ValueError("Invalid SSH destination")
    ssh_port = int(ask("  SSH port", ssh_port))
    callback_port = int(ask("  Callback port (must be free on both computers)", "1455"))
    if not (1 <= ssh_port <= 65535 and 1 <= callback_port <= 65535):
        raise ValueError("Ports must be between 1 and 65535")

    def listener_ready(port: int) -> None:
        command = shlex.join([
            "ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-L",
            f"127.0.0.1:{port}:127.0.0.1:{port}", "-p", str(ssh_port), "--", destination,
        ])
        print("\n  On your browser computer, run this command in a separate terminal:")
        print(f"    {command}")
        print("  Keep that SSH connection open, then open the following link in that computer's browser.")
        print("  Waiting up to 10 minutes. Ctrl+C cancels. If forwarding fails, cancel and choose another port.")

    account = service.login(client_id, open_browser=None, callback_port=callback_port,
                            on_listener_ready=listener_ready, timeout=600)
    print("  Authorization received. You can stop the forwarding command with Ctrl+C.")
    return account


def _prompt_chatgpt_thinking(model: dict[str, Any], current: WizardLLMEndpoint | None) -> tuple[list[str], str]:
    from pal.llm.ir import ThinkingLevel

    # Catalog effort names are wire values. In particular, Pal's "off" omits
    # the parameter; it must never stand in for the provider's "none".
    allowed = {level.value for level in ThinkingLevel if level != ThinkingLevel.OFF}
    raw = model.get("supported_reasoning_levels") or []
    advertised = list(dict.fromkeys(
        str(item.get("effort") or "") if isinstance(item, dict) else str(item)
        for item in raw
    ))
    levels = [level for level in advertised if level in allowed]
    omitted = [level for level in advertised if level not in allowed]
    if omitted:
        print(f"  Catalog effort values not supported by Pal: {', '.join(omitted)} (not mapped to other levels).")
    same_model = current is not None and current.model_id == model["slug"]
    if not levels:
        print("  No usable reasoning levels in the catalog. Enter verified levels; off only omits the effort parameter.")
        return _prompt_thinking_levels(
            list(current.thinking_levels) if same_model else ["off"],
            current.default_thinking_level if same_model else "off", wire_shape="openai_response",
        )
    default = str(model.get("default_reasoning_level") or "")
    if same_model and current.default_thinking_level in levels:
        default = current.default_thinking_level
    if default not in levels:
        default = levels[0]
    print(f"  Supported thinking levels: {', '.join(levels)}")
    while True:
        selected = ask("  Default thinking level", default).strip().lower()
        if selected in levels:
            return levels, selected
        print(f"  Choose one of: {', '.join(levels)}")


def prompt_chatgpt_endpoint(runtime_root: Path | None, current: WizardLLMEndpoint | None = None) -> WizardLLMEndpoint | None:
    from pal.llm.chatgpt import API_URL, PROFILE, ChatGPTAuthService, ChatGPTError, USAGE_URL, credential_ref
    from pal.llm.secret_store import EncryptedFileSecretStore

    if runtime_root is None:
        print("Select a runtime with pal wizard --runtime-root <dir> first.")
        return None
    store = EncryptedFileSecretStore(runtime_root / "secrets.json")
    service = ChatGPTAuthService(store)
    try:
        accounts = service.accounts()
        for index, account in enumerate(accounts, 1):
            print(f"  {index}. {account.get('email') or 'ChatGPT'} [{account['client_id']}]")
        default_account = next((str(index) for index, account in enumerate(accounts, 1)
                                if current and current.credential_ref == credential_ref(str(account["client_id"]))),
                               "1" if accounts else "new")
        choice = ask("  Account number, new, or cancel", default_account).strip()
        if not choice or choice.lower() == "cancel":
            return None
        if choice == "new":
            account = _prompt_chatgpt_login(service)
            if "chatgpt.tokens.use.direct" in (account.get("scopes") or []):
                print(f"  You're using your ChatGPT plan. Manage usage: {USAGE_URL}")
        else:
            index = int(choice) - 1
            if not 0 <= index < len(accounts):
                raise ValueError("Invalid account number")
            account = accounts[index]
            action = ask("  1) Use account  2) Reauthorize  3) Sign out", "1")
            if action not in {"1", "2", "3"}:
                raise ValueError("Invalid account action")
            if action == "3":
                if ask_yes_no("  Sign out this ChatGPT account?", False):
                    confirmed = service.logout(str(account["client_id"]))
                    print("  Signed out." if confirmed else "  Signed out locally; remote revocation was not confirmed. Disconnect Pal in ChatGPT Settings.")
                return None
            if action == "2" or account.get("needs_reauthorization"):
                account = _prompt_chatgpt_login(service, str(account["client_id"]))
        client_id = str(account["client_id"])
        if "chatgpt.tokens.use.direct" not in (account.get("scopes") or []):
            print("  Signed in without ChatGPT plan permission. Reauthorize to enable it, or configure an API endpoint.")
            return None
        models = service.models(client_id)
        if not models:
            print("  No selectable models returned for this account.")
            return None
        for index, model in enumerate(models, 1):
            print(f"  {index}. {model.get('display_name') or model['slug']} ({model['slug']})")
        default = next((str(i) for i, model in enumerate(models, 1) if current and model["slug"] == current.model_id), "1")
        index = int(ask("  Model", default)) - 1
        if not 0 <= index < len(models):
            raise ValueError("Invalid model number")
        model = models[index]
        label = ask("  Endpoint ID", current.endpoint_id if current else "chatgpt-" + model["slug"])
        context = int(ask("  Local context budget (tokens)", str(current.context_window if current else model.get("context_window") or 32768)))
        budget = int(ask("  Local output budget (not sent as an API limit)", str(current.max_output_tokens if current else 4096)))
        if context <= 0 or budget <= 0 or budget > context:
            raise ValueError("Invalid local token budgets")
        thinking_levels, default_thinking_level = _prompt_chatgpt_thinking(model, current)
        endpoint = WizardLLMEndpoint(
            endpoint_id=label, model_id=model["slug"], wire_shape="openai_response", base_url=API_URL,
            api_key=None, context_window=context, max_output_tokens=budget,
            thinking_levels=thinking_levels, default_thinking_level=default_thinking_level,
            supports_tools=True, supports_streaming=True,
            supports_vision=current.supports_vision if current else "image" in model.get("input_modalities", []),
            priority=current.priority if current else 0, provider="openai", auth_kind="oauth",
            credential_ref=credential_ref(client_id), capabilities_blob={"access_profile": PROFILE},
            notes="Using ChatGPT plan. Switch endpoints manually with /model.",
        )
        print("  Checking text and a harmless tool round trip uses your ChatGPT plan.")
        if not ask_yes_no("  Run checks now?", True):
            return None
        result = run_llm_endpoint_preflight(endpoint, timeout_seconds=60, secret_store=store)
        print(f"  {result.status}: {result.detail}")
        if result.status != "ok":
            print("  Endpoint configuration was not changed. The saved account can be reused.")
            return None
        return endpoint
    except (ChatGPTError, ValueError, OSError) as exc:
        print(f"  ChatGPT setup failed: {exc}")
        return None
    except KeyboardInterrupt:
        print("\n  ChatGPT setup cancelled.")
        return None
