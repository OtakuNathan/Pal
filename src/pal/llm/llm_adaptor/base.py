from __future__ import annotations

from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
import re
import sys
import types
from typing import Any, ClassVar

from pal.llm.contracts import CanonicalLLMRequest
from pal.llm.models import LLMEndpointModel

LLM_PROVIDER_ADAPTER_ENTRY_POINT_GROUP = "pal.llm_provider_adapters"
RUNTIME_PROVIDER_ADAPTER_DIR = "llm/adapters"
LEGACY_RUNTIME_PROVIDER_ADAPTER_DIR = "llm_provider_adapters"


@dataclass
class LiteLLMCompletionDraft:
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
    litellm_provider: ClassVar[str] = "openai"
    model_provider_aliases: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def matches_endpoint(cls, endpoint: LLMEndpointModel) -> bool:
        return False

    def new_draft(self, messages: list[dict[str, Any]]) -> LiteLLMCompletionDraft:
        return LiteLLMCompletionDraft(model=self.litellm_model(), messages=messages)

    def litellm_model(self) -> str:
        model_id = str(self.endpoint.model_id or "").strip()
        if "/" not in model_id:
            return f"{self.litellm_provider}/{model_id}"
        provider, name = model_id.split("/", 1)
        if provider.strip().lower() in self.model_provider_aliases:
            return f"{self.litellm_provider}/{name}"
        return model_id

    def apply_request(self, request: CanonicalLLMRequest, draft: LiteLLMCompletionDraft) -> None:
        return None


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
    from pal.llm.llm_adaptor.zai_glm import ZaiGLMProvider

    registry = LLMProviderRegistry()
    registry.register(CodexBridgeProvider)
    registry.register(DeepSeekProvider)
    registry.register(ZaiGLMProvider)
    registry.register(AnthropicMessagesProvider)
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
    }
    return mapping.get(text, "medium" if text else None)


default_provider_registry = build_default_provider_registry()
