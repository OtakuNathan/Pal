from __future__ import annotations

from pal.execution.generated_tool_models import (
    LlmCapabilitiesLLMIntrospectionProviderSetActiveEndpointInput,
    LlmCapabilitiesLLMIntrospectionProviderShowInput,
)

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
from pal.llm.models import LLMEndpointModel
from pal.llm.runtime import LLMRuntime
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    OPERATION_NAMESPACE,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    capability_action,
    capability_node,
)
from pal.shared.result_rendering import render_titled_structured_for_llm

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


@dataclass(frozen=True)
class LLMModelListItem:
    model_id: str
    endpoint_id: str
    display_name: str | None
    provider: str
    api_mode: str
    priority: int


@dataclass(frozen=True)
class LLMModelSnapshot:
    model_id: str
    endpoint_id: str
    display_name: str | None
    provider: str
    api_mode: str
    context_window: int | None
    max_output_tokens: int | None
    supports_reasoning: bool
    supports_tools: bool
    supports_streaming: bool
    supports_vision: bool
    priority: int
    enabled: bool


@dataclass(frozen=True)
class LLMActiveModelSnapshot:
    has_active_model: bool
    active_endpoint_id: str | None
    model_id: str | None
    endpoint_id: str | None
    display_name: str | None
    provider: str | None
    api_mode: str | None
    context_window: int | None
    max_output_tokens: int | None
    supports_reasoning: bool | None
    supports_tools: bool | None
    supports_streaming: bool | None
    supports_vision: bool | None
    priority: int | None
    enabled: bool | None


@dataclass(frozen=True)
class LLMThinkLevelSnapshot:
    persisted_think_level: str
    effective_think_level: str


@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:llm",
    target_kind="module",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:llm",
    target_kind="module",
)
@dataclass
class LLMIntrospectionProvider:
    runtime: LLMRuntime
    module_id: str = "llm"

    def iter_endpoints(self) -> list[LLMEndpointModel]:
        try:
            return list(self.runtime.endpoint_resolver.enabled())
        except Exception:
            return []

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="list",
        description="List enabled llm models ordered by priority. Use model_id with llm show.",
    )
    def list_endpoints(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = [
            LLMModelListItem(
                model_id=endpoint.model_id,
                endpoint_id=endpoint.endpoint_id,
                display_name=getattr(endpoint, "display_name", None),
                provider=endpoint.provider,
                api_mode=str(getattr(endpoint, "api_mode", "") or ""),
                priority=int(getattr(endpoint, "priority", 0) or 0),
            ).__dict__
            for endpoint in self.iter_endpoints()
        ]
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="llm models",
            structured={"items": payload},
            llm_text=render_titled_structured_for_llm("LLM models", {"items": payload}),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="active",
        description="Show the current active llm model metadata",
    )
    def active(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        snapshot = inspect_llm(self)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="llm active model",
            structured=snapshot.__dict__,
            llm_text=render_titled_structured_for_llm("LLM active model", snapshot.__dict__),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="show",
        description="Show public metadata for one enabled llm model by model_id",
        InputModel=LlmCapabilitiesLLMIntrospectionProviderShowInput,
    )
    def show_model(self, call: IntrospectionCall) -> IntrospectionResult:
        model_id = str(call.args.get("model_id") or "").strip()
        if not model_id:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="model_id is required",
                llm_text="model_id is required",
            )
        endpoint = self._find_endpoint_by_model_id(model_id)
        if endpoint is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="llm model not found",
                structured={"model_id": model_id},
                llm_text="llm model not found",
            )
        snapshot = _model_snapshot(endpoint)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="llm model metadata",
            structured=snapshot.__dict__,
            llm_text=render_titled_structured_for_llm("LLM model metadata", snapshot.__dict__),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="think_level",
        description="Show the current persisted and effective llm think level",
    )
    def think_level(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        self.runtime.refresh_runtime_settings()
        snapshot = LLMThinkLevelSnapshot(
            persisted_think_level=self.runtime.settings_repository.get_think_level(),
            effective_think_level=self.runtime.think_level,
        )
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="llm think level",
            structured=snapshot.__dict__,
            llm_text=render_titled_structured_for_llm("LLM think level", snapshot.__dict__),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="set_active_endpoint",
        description="Switch the active llm endpoint used for future requests",
        InputModel=LlmCapabilitiesLLMIntrospectionProviderSetActiveEndpointInput,
    )
    def set_active_endpoint(self, call: IntrospectionCall) -> IntrospectionResult:
        endpoint_id = str(call.args.get("active_endpoint_id") or "").strip()
        if not endpoint_id:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="active_endpoint_id is required",
                llm_text="active_endpoint_id is required",
            )
        endpoint = next((item for item in self.runtime.endpoint_resolver.enabled() if item.endpoint_id == endpoint_id), None)
        if endpoint is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="unknown enabled llm endpoint",
                structured={"active_endpoint_id": endpoint_id},
                llm_text="unknown enabled llm endpoint",
            )
        self.runtime.set_active_endpoint(endpoint_id)
        snapshot = inspect_llm(self)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="llm active endpoint updated",
            structured=snapshot.__dict__,
            llm_text=render_titled_structured_for_llm("LLM active model", snapshot.__dict__),
        )

    def _find_endpoint_by_model_id(self, model_id: str) -> LLMEndpointModel | None:
        return next((endpoint for endpoint in self.iter_endpoints() if endpoint.model_id == model_id), None)


def _model_snapshot(endpoint: LLMEndpointModel) -> LLMModelSnapshot:
    return LLMModelSnapshot(
        model_id=endpoint.model_id,
        endpoint_id=endpoint.endpoint_id,
        display_name=getattr(endpoint, "display_name", None),
        provider=endpoint.provider,
        api_mode=str(getattr(endpoint, "api_mode", "") or ""),
        context_window=getattr(endpoint, "context_window", None),
        max_output_tokens=getattr(endpoint, "max_output_tokens", None),
        supports_reasoning=bool(getattr(endpoint, "supports_reasoning", False)),
        supports_tools=bool(getattr(endpoint, "supports_tools", False)),
        supports_streaming=bool(getattr(endpoint, "supports_streaming", False)),
        supports_vision=bool(getattr(endpoint, "supports_vision", False)),
        priority=int(getattr(endpoint, "priority", 0) or 0),
        enabled=bool(getattr(endpoint, "enabled", True)),
    )


def inspect_llm(provider: LLMIntrospectionProvider) -> LLMActiveModelSnapshot:
    provider.runtime.refresh_runtime_settings()
    active = provider.runtime.endpoint_resolver.primary(preferred_endpoint_id=provider.runtime.active_endpoint_id)
    if active is None:
        return LLMActiveModelSnapshot(
            has_active_model=False,
            active_endpoint_id=provider.runtime.active_endpoint_id,
            model_id=None,
            endpoint_id=None,
            display_name=None,
            provider=None,
            api_mode=None,
            context_window=None,
            max_output_tokens=None,
            supports_reasoning=None,
            supports_tools=None,
            supports_streaming=None,
            supports_vision=None,
            priority=None,
            enabled=None,
        )
    snapshot = _model_snapshot(active)
    return LLMActiveModelSnapshot(
        has_active_model=True,
        active_endpoint_id=active.endpoint_id,
        model_id=active.model_id,
        endpoint_id=snapshot.endpoint_id,
        display_name=snapshot.display_name,
        provider=active.provider,
        api_mode=snapshot.api_mode,
        context_window=snapshot.context_window,
        max_output_tokens=snapshot.max_output_tokens,
        supports_reasoning=snapshot.supports_reasoning,
        supports_tools=snapshot.supports_tools,
        supports_streaming=snapshot.supports_streaming,
        supports_vision=snapshot.supports_vision,
        priority=snapshot.priority,
        enabled=snapshot.enabled,
    )


def register_with_core(context: MainContext, runtime: LLMRuntime) -> ModuleHandle:
    provider = LLMIntrospectionProvider(runtime=runtime)
    handle = ModuleHandle(
        module_id="llm",
        tier=MODULE_TIER_CORE_FOUNDATION,
        detachable=False,
        introspection_provider=provider,
        ports={"llm": runtime},
    )
    context.register_module(handle)
    return handle
