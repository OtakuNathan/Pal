from __future__ import annotations

from dataclasses import dataclass, field
from importlib import metadata
import json
from pathlib import Path
import re
import sys
import types
from typing import Any, ClassVar

from pal.llm.contracts import CanonicalLLMRequest, ThinkingChoice, ThinkingContract
from pal.llm.models import LLMEndpointModel

LLM_PROVIDER_ADAPTER_ENTRY_POINT_GROUP = "pal.llm_provider_adapters"
RUNTIME_PROVIDER_ADAPTER_DIR = "llm/adapters"
LEGACY_RUNTIME_PROVIDER_ADAPTER_DIR = "llm_provider_adapters"
OPENAI_CHAT_COMPLETIONS_SHAPE = "chat_completions"
OPENAI_RESPONSES_SHAPE = "responses"

OPENAI_THINKING_CONTRACT = ThinkingContract(
    choices=(
        ThinkingChoice("off", "off", aliases=("none",)),
        ThinkingChoice("minimal", "minimal"),
        ThinkingChoice("low", "low"),
        ThinkingChoice("medium", "medium", aliases=("balanced",)),
        ThinkingChoice("high", "high", aliases=("deep",)),
        ThinkingChoice("xhigh", "xhigh"),
    ),
    default_choice_id="medium",
)
OPENAI_MAX_THINKING_CONTRACT = ThinkingContract(
    choices=(
        *OPENAI_THINKING_CONTRACT.choices,
        ThinkingChoice("max", "max", aliases=("maximum",)),
    ),
    default_choice_id="medium",
)
ANTHROPIC_THINKING_CONTRACT = ThinkingContract(
    choices=(
        ThinkingChoice("off", "off", aliases=("none",)),
        ThinkingChoice("low", "low", aliases=("minimal",)),
        ThinkingChoice("medium", "medium", aliases=("balanced",)),
        ThinkingChoice("high", "high", aliases=("deep", "xhigh", "max", "maximum")),
    ),
    default_choice_id="medium",
)


@dataclass
class OpenAIChatCompletionDraft:
    model: str
    messages: list[dict[str, Any]]
    timeout: int = 120
    request_timeout: float | None = None
    force_timeout: float | None = None
    max_retries: int | None = 0
    api_base: str | None = None
    api_key: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_choice: Any = None
    reasoning_effort: str | None = None
    thinking: dict[str, Any] | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_kwargs(self) -> dict[str, Any]:
        request_timeout = self.request_timeout if self.request_timeout is not None else self.timeout
        force_timeout = self.force_timeout if self.force_timeout is not None else request_timeout
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self.messages,
            "timeout": self.timeout,
            "request_timeout": request_timeout,
            "force_timeout": force_timeout,
        }
        optional_values = {
            "api_base": self.api_base,
            "api_key": self.api_key,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "max_retries": self.max_retries,
            "tool_choice": self.tool_choice,
            "reasoning_effort": self.reasoning_effort,
            "thinking": self.thinking,
        }
        kwargs.update({key: value for key, value in optional_values.items() if value is not None})
        if self.tools:
            kwargs["tools"] = self.tools
        if self.extra_body:
            kwargs["extra_body"] = dict(self.extra_body)
        kwargs.update(self.extra)
        return kwargs


@dataclass(frozen=True)
class LLMProviderAdapter:
    endpoint: LLMEndpointModel

    provider_names: ClassVar[frozenset[str]] = frozenset()
    adapter_names: ClassVar[frozenset[str]] = frozenset()
    model_provider_prefix: ClassVar[str] = "openai"
    model_provider_aliases: ClassVar[frozenset[str]] = frozenset()
    request_shape: ClassVar[str] = OPENAI_CHAT_COMPLETIONS_SHAPE
    reasoning_content_messages: ClassVar[bool] = False

    @classmethod
    def matches_endpoint(cls, endpoint: LLMEndpointModel) -> bool:
        return False

    def new_draft(self, messages: list[dict[str, Any]]) -> OpenAIChatCompletionDraft:
        return OpenAIChatCompletionDraft(
            model=self.api_model(),
            messages=chat_messages_to_openai_compatible_messages(
                messages,
                reasoning_content_messages=self.should_replay_reasoning_content(),
            ),
        )

    def should_replay_reasoning_content(self) -> bool:
        if self.reasoning_content_messages:
            return True
        capabilities = _capabilities(self.endpoint)
        if capabilities.get("reasoning_content_messages") is False:
            return False
        if capabilities.get("reasoning_content") is False:
            return False
        return bool(
            capabilities.get("reasoning_content_messages")
            or capabilities.get("reasoning_content")
            or capabilities.get("supports_reasoning_content")
            or capabilities.get("supports_thinking")
            or capabilities.get("thinking")
            or getattr(self.endpoint, "supports_reasoning", False)
        )

    def api_model(self) -> str:
        model_id = str(self.endpoint.model_id or "").strip()
        if "/" not in model_id:
            return model_id
        provider, name = model_id.split("/", 1)
        provider_aliases = set(self.model_provider_aliases)
        provider_aliases.add(str(self.model_provider_prefix or "").strip().lower())
        if provider.strip().lower() in provider_aliases:
            return name
        return model_id

    def apply_request(self, request: CanonicalLLMRequest, draft: OpenAIChatCompletionDraft) -> None:
        return None

    def provider_thinking_contract(self) -> ThinkingContract | None:
        return None

    def thinking_contract(self) -> ThinkingContract | None:
        return _thinking_contract_from_capabilities(
            self.endpoint,
            default=self.provider_thinking_contract(),
        )

    def resolve_think_level(self, value: Any) -> str | None:
        contract = self.thinking_contract()
        if contract is None:
            return None
        return contract.resolve(value)


class LLMProviderRegistry:
    def __init__(self, *, default_adapter: type[LLMProviderAdapter] | None = None) -> None:
        self._by_provider: dict[str, type[LLMProviderAdapter]] = {}
        self._by_adapter_name: dict[str, type[LLMProviderAdapter]] = {}
        self._matchers: list[type[LLMProviderAdapter]] = []
        self._default_adapter: type[LLMProviderAdapter] = default_adapter or _default_openai_chat_provider()
        self._runtime_adapters: list[type[LLMProviderAdapter]] = []
        self._runtime_module_names: set[str] = set()
        self.load_errors: list[str] = []

    def register(self, adapter_type: type[LLMProviderAdapter], *, runtime: bool = False) -> None:
        if adapter_type not in self._matchers:
            self._matchers.append(adapter_type)
        if runtime and adapter_type not in self._runtime_adapters:
            self._runtime_adapters.append(adapter_type)
        self._rebuild_indexes()

    def unregister(self, adapter_type: type[LLMProviderAdapter]) -> None:
        self._matchers = [registered for registered in self._matchers if registered is not adapter_type]
        self._runtime_adapters = [registered for registered in self._runtime_adapters if registered is not adapter_type]
        self._rebuild_indexes()

    def load_entry_points(self, *, group: str = LLM_PROVIDER_ADAPTER_ENTRY_POINT_GROUP) -> None:
        try:
            entry_points = metadata.entry_points(group=group)
        except TypeError:
            entry_points = metadata.entry_points().get(group, ())
        for entry_point in entry_points:
            try:
                adapter_type = entry_point.load()
                if not isinstance(adapter_type, type) or not issubclass(adapter_type, LLMProviderAdapter):
                    raise TypeError("entry point must load an LLMProviderAdapter subclass")
                self.register(adapter_type)
            except Exception as exc:
                self.load_errors.append(f"{entry_point.name}: {exc.__class__.__name__}: {exc}")

    def load_runtime_adapters(self, runtime_root: str | Path | None) -> None:
        self._clear_runtime_adapters()
        if runtime_root is None:
            return
        root = Path(runtime_root).expanduser()
        for adapters_dir in _runtime_adapter_dirs(root):
            for adapter_path in _iter_runtime_adapter_paths(adapters_dir):
                self._load_runtime_adapter_path(adapter_path, root=root)

    def refresh_external_sources(self, *, runtime_root: str | Path | None = None) -> None:
        self.load_errors = []
        self.load_entry_points()
        self.load_runtime_adapters(runtime_root)

    def registered_adapters(self) -> tuple[type[LLMProviderAdapter], ...]:
        adapters: list[type[LLMProviderAdapter]] = []
        for adapter_type in self._matchers:
            if adapter_type not in adapters:
                adapters.append(adapter_type)
        return tuple(adapters)

    def resolve(self, endpoint: LLMEndpointModel) -> LLMProviderAdapter:
        adapter_type = self._by_adapter_name.get(_adapter_name(endpoint))
        if adapter_type is not None:
            return adapter_type(endpoint)

        adapter_type = self._by_provider.get(_provider_name(endpoint))
        if adapter_type is not None:
            return adapter_type(endpoint)

        for candidate in self._matchers:
            if candidate.matches_endpoint(endpoint):
                return candidate(endpoint)
        return self._default_adapter(endpoint)

    def _clear_runtime_adapters(self) -> None:
        runtime_adapters = set(self._runtime_adapters)
        self._matchers = [adapter_type for adapter_type in self._matchers if adapter_type not in runtime_adapters]
        self._runtime_adapters.clear()
        for module_name in list(self._runtime_module_names):
            sys.modules.pop(module_name, None)
        self._runtime_module_names.clear()
        self._rebuild_indexes()

    def _load_runtime_adapter_path(self, adapter_path: Path, *, root: Path) -> None:
        try:
            module_name = _runtime_module_name(adapter_path, root=root)
            module = _load_source_module(module_name, adapter_path)
            self._runtime_module_names.add(module_name)
            adapter_types = _adapter_types_from_module(module)
            if not adapter_types:
                raise TypeError("module must export an LLMProviderAdapter subclass")
            for adapter_type in adapter_types:
                self.register(adapter_type, runtime=True)
        except Exception as exc:
            self.load_errors.append(f"{adapter_path}: {exc.__class__.__name__}: {exc}")

    def _rebuild_indexes(self) -> None:
        self._by_provider.clear()
        self._by_adapter_name.clear()
        for adapter_type in self._matchers:
            for name in adapter_type.provider_names:
                self._by_provider[_normalize_key(name)] = adapter_type
            for name in adapter_type.adapter_names:
                self._by_adapter_name[_normalize_key(name)] = adapter_type


def register_llm_provider_adapter(adapter_type: type[LLMProviderAdapter]) -> None:
    default_provider_registry.register(adapter_type)


def unregister_llm_provider_adapter(adapter_type: type[LLMProviderAdapter]) -> None:
    default_provider_registry.unregister(adapter_type)


def resolve_endpoint_adapter(endpoint: LLMEndpointModel) -> LLMProviderAdapter:
    return default_provider_registry.resolve(endpoint)


def build_default_provider_registry(*, load_entry_points: bool = False) -> LLMProviderRegistry:
    from pal.llm.llm_adaptor.anthropic_api import AnthropicMessagesProvider
    from pal.llm.llm_adaptor.deepseek import DeepSeekProvider
    from pal.llm.llm_adaptor.openai_chat import CodexBridgeProvider, OpenAIChatProvider
    from pal.llm.llm_adaptor.openai_responses import OpenAIResponsesProvider
    from pal.llm.llm_adaptor.zai_glm import ZaiGLMProvider

    registry = LLMProviderRegistry()
    registry.register(CodexBridgeProvider)
    registry.register(DeepSeekProvider)
    registry.register(ZaiGLMProvider)
    registry.register(AnthropicMessagesProvider)
    registry.register(OpenAIResponsesProvider)
    registry.register(OpenAIChatProvider)
    if load_entry_points:
        registry.load_entry_points()
    return registry


def build_runtime_provider_registry() -> LLMProviderRegistry:
    return build_default_provider_registry(load_entry_points=True)


def _default_openai_chat_provider() -> type[LLMProviderAdapter]:
    from pal.llm.llm_adaptor.openai_chat import OpenAIChatProvider

    return OpenAIChatProvider


def _runtime_adapter_dirs(runtime_root: Path) -> tuple[Path, ...]:
    return (
        runtime_root / RUNTIME_PROVIDER_ADAPTER_DIR,
        runtime_root / LEGACY_RUNTIME_PROVIDER_ADAPTER_DIR,
    )


def _iter_runtime_adapter_paths(adapters_dir: Path) -> tuple[Path, ...]:
    if not adapters_dir.exists():
        return ()
    paths: list[Path] = []
    for item in sorted(adapters_dir.iterdir(), key=lambda path: path.name):
        if item.is_file() and item.suffix == ".py" and not item.name.startswith("_"):
            paths.append(item)
            continue
        adapter_py = item / "adapter.py"
        if item.is_dir() and adapter_py.is_file():
            paths.append(adapter_py)
    return tuple(paths)


def _runtime_module_name(adapter_path: Path, *, root: Path) -> str:
    try:
        relative = adapter_path.resolve().relative_to(root.resolve())
    except ValueError:
        relative = adapter_path.name
    stem = re.sub(r"[^0-9A-Za-z_]+", "_", str(relative))
    return f"_pal_runtime_llm_adapter_{stem}"


def _load_source_module(module_name: str, adapter_path: Path) -> types.ModuleType:
    source = adapter_path.read_text(encoding="utf-8")
    module = types.ModuleType(module_name)
    module.__file__ = str(adapter_path)
    module.__package__ = ""
    adapter_dir = str(adapter_path.parent)
    inserted = False
    if adapter_dir not in sys.path:
        sys.path.insert(0, adapter_dir)
        inserted = True
    try:
        sys.modules[module_name] = module
        exec(compile(source, str(adapter_path), "exec"), module.__dict__)  # noqa: S102
    finally:
        if inserted:
            try:
                sys.path.remove(adapter_dir)
            except ValueError:
                pass
    return module


def _adapter_types_from_module(module: types.ModuleType) -> tuple[type[LLMProviderAdapter], ...]:
    explicit = getattr(module, "ADAPTERS", None)
    if explicit is None:
        single = getattr(module, "ADAPTER", None)
        candidates = (single,) if single is not None else tuple(vars(module).values())
    elif isinstance(explicit, type):
        candidates = (explicit,)
    else:
        candidates = tuple(explicit)
    adapter_types: list[type[LLMProviderAdapter]] = []
    for candidate in candidates:
        if (
            isinstance(candidate, type)
            and issubclass(candidate, LLMProviderAdapter)
            and candidate is not LLMProviderAdapter
            and candidate not in adapter_types
        ):
            adapter_types.append(candidate)
    return tuple(adapter_types)


def _adapter_name(endpoint: LLMEndpointModel) -> str:
    capabilities = _capabilities(endpoint)
    return _normalize_key(capabilities.get("adapter") or capabilities.get("llm_adapter") or "")


def _provider_name(endpoint: LLMEndpointModel) -> str:
    return _normalize_key(getattr(endpoint, "provider", "") or "")


def _normalize_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _capabilities(endpoint: LLMEndpointModel) -> dict[str, Any]:
    return dict(getattr(endpoint, "capabilities_blob", None) or {})


def openai_thinking_contract_for_endpoint(endpoint: LLMEndpointModel) -> ThinkingContract:
    capabilities = _capabilities(endpoint)
    max_support = capabilities.get("supports_max_reasoning_effort")
    if max_support is True:
        return OPENAI_MAX_THINKING_CONTRACT
    if max_support is False:
        return OPENAI_THINKING_CONTRACT
    model_id = _normalize_key(getattr(endpoint, "model_id", ""))
    if "gpt-5.6" in model_id:
        return OPENAI_MAX_THINKING_CONTRACT
    return OPENAI_THINKING_CONTRACT


def _thinking_contract_from_capabilities(
    endpoint: LLMEndpointModel,
    *,
    default: ThinkingContract | None,
) -> ThinkingContract | None:
    capabilities = _capabilities(endpoint)
    declaration = capabilities.get("thinking_contract")
    if declaration is None:
        return default
    if declaration is False:
        return None
    if not isinstance(declaration, dict):
        raise ValueError("endpoint thinking_contract must be an object or false")
    if default is None:
        raise ValueError("endpoint cannot declare thinking choices when its provider has no thinking contract")
    raw_choices = declaration.get("choices")
    if not isinstance(raw_choices, list) or not raw_choices:
        raise ValueError("endpoint thinking_contract.choices must be a non-empty list")
    choices: list[ThinkingChoice] = []
    for item in raw_choices:
        if isinstance(item, str):
            canonical_id = default.resolve(item)
            if canonical_id is None:
                raise ValueError(f"endpoint thinking choice is not supported by its provider: {item}")
            provider_choice = default.choice(canonical_id)
            assert provider_choice is not None
            choices.append(
                ThinkingChoice(
                    choice_id=canonical_id,
                    label=provider_choice.label,
                    aliases=provider_choice.aliases,
                )
            )
            continue
        if not isinstance(item, dict):
            raise ValueError("endpoint thinking_contract choices must be strings or objects")
        choice_id = str(item.get("id") or item.get("choice_id") or "").strip()
        canonical_id = default.resolve(choice_id)
        if canonical_id is None:
            raise ValueError(f"endpoint thinking choice is not supported by its provider: {choice_id}")
        provider_choice = default.choice(canonical_id)
        assert provider_choice is not None
        aliases = item.get("aliases") or ()
        if not isinstance(aliases, (list, tuple)):
            raise ValueError("endpoint thinking choice aliases must be a list")
        choices.append(
            ThinkingChoice(
                choice_id=canonical_id,
                label=str(item.get("label") or provider_choice.label),
                aliases=tuple(dict.fromkeys((*provider_choice.aliases, *(str(alias) for alias in aliases)))),
            )
        )
    default_choice_id = str(declaration.get("default") or declaration.get("default_choice_id") or "").strip()
    if not default_choice_id:
        default_choice_id = choices[0].choice_id
    else:
        default_choice_id = default.resolve(default_choice_id) or default_choice_id
    return ThinkingContract(choices=tuple(choices), default_choice_id=default_choice_id)


def _think_level_to_completion_reasoning_effort(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    mapping = {
        "off": "none",
        "minimal": "minimal",
        "low": "low",
        "balanced": "medium",
        "medium": "medium",
        "deep": "high",
        "high": "high",
        "xhigh": "xhigh",
        "max": "max",
    }
    return mapping.get(text, "medium" if text else None)


def chat_messages_to_openai_compatible_messages(
    messages: list[dict[str, Any]],
    *,
    supports_developer: bool = False,
    reasoning_content_messages: bool = False,
) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    seen_conversation = False
    for message in list(messages or []):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user").strip() or "user"
        if role == "system" and not seen_conversation:
            rendered.append(
                _openai_compatible_message(
                    message,
                    reasoning_content_messages=reasoning_content_messages,
                )
            )
            continue
        if role == "developer" and supports_developer:
            rendered.append(
                _openai_compatible_message(
                    message,
                    reasoning_content_messages=reasoning_content_messages,
                )
            )
            continue
        if role in {"system", "developer"}:
            fallback = _instruction_fallback_user_message(role, message.get("content"))
            if fallback is not None:
                rendered.append(fallback)
                seen_conversation = True
            continue
        rendered.append(
            _openai_compatible_message(
                message,
                reasoning_content_messages=reasoning_content_messages,
            )
        )
        seen_conversation = True
    if not rendered:
        rendered.append({"role": "user", "content": "Continue."})
    return rendered


def _openai_compatible_message(
    message: dict[str, Any],
    *,
    reasoning_content_messages: bool,
) -> dict[str, Any]:
    payload = {
        key: value
        for key, value in dict(message).items()
        if key not in {"provider_specific_fields", "reasoning_content"}
    }
    if reasoning_content_messages:
        reasoning_content = _message_reasoning_content(message)
        if reasoning_content:
            payload["reasoning_content"] = reasoning_content
    return payload


def _message_reasoning_content(message: dict[str, Any]) -> str:
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        return reasoning
    provider_fields = message.get("provider_specific_fields")
    if isinstance(provider_fields, dict):
        nested = provider_fields.get("reasoning_content")
        if isinstance(nested, str) and nested:
            return nested
    return ""


def render_instruction_fallback_text(role: str, content: Any) -> str:
    normalized_role = str(role or "developer").strip().lower()
    tag = "system-instruction" if normalized_role == "system" else "developer-instruction"
    label = "system" if normalized_role == "system" else "developer"
    body = _message_content_text(content).strip()
    if not body:
        body = "(empty instruction)"
    return (
        f"<{tag}>\n"
        f"This is a Pal {label} instruction for the current turn, not the user's request. "
        "Apply it as runtime guidance while preserving the surrounding conversation order.\n\n"
        f"{body}\n"
        f"</{tag}>"
    )


def _instruction_fallback_user_message(role: str, content: Any) -> dict[str, Any] | None:
    text = render_instruction_fallback_text(role, content)
    if not text.strip():
        return None
    return {"role": "user", "content": text}


def _message_content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                if item:
                    parts.append(item)
                continue
            if not isinstance(item, dict):
                rendered = str(item).strip()
                if rendered:
                    parts.append(rendered)
                continue
            part_type = str(item.get("type") or "").strip()
            if part_type in {"text", "input_text", "output_text"}:
                text = str(item.get("text") or "")
                if text:
                    parts.append(text)
                continue
            if part_type in {"image_url", "input_image"}:
                parts.append("[image content]")
                continue
            parts.append(json.dumps(item, ensure_ascii=False))
        return "\n".join(parts)
    return str(content)


default_provider_registry = build_default_provider_registry()
