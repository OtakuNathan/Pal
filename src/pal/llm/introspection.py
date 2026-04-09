from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
from pal.llm.models import LLMEndpointModel
from pal.llm.runtime import LLMRuntime
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    capability_action,
    capability_node,
)

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


@dataclass(frozen=True)
class LLMListItem:
    endpoint_id: str
    display_name: str | None
    provider: str
    api_mode: str
    model_id: str
    priority: int
    enabled: bool
    supports_reasoning: bool
    supports_tools: bool
    supports_streaming: bool
    supports_vision: bool


@dataclass(frozen=True)
class LLMEndpointSnapshot:
    endpoint_id: str
    display_name: str | None
    provider: str
    api_mode: str
    model_id: str
    context_window: int | None
    max_output_tokens: int | None
    supports_reasoning: bool
    supports_tools: bool
    supports_streaming: bool
    supports_vision: bool
    priority: int
    enabled: bool


@dataclass(frozen=True)
class LLMActiveSnapshot:
    has_primary_endpoint: bool
    endpoint_id: str | None
    model_id: str | None
    provider: str | None


@dataclass(frozen=True)
class LLMThinkLevelSnapshot:
    persisted_think_level: str
    effective_think_level: str


@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="endpoint",
    kind="endpoint",
    source="builtin:llm",
    target_kind="endpoint",
    iterable_resolver="iter_endpoints",
    target_id_resolver="resolve_endpoint_id",
    target_label_resolver="resolve_endpoint_label",
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

    def resolve_endpoint_id(self, endpoint: LLMEndpointModel) -> str:
        return endpoint.endpoint_id

    def resolve_endpoint_label(self, endpoint: LLMEndpointModel) -> str:
        return endpoint.endpoint_id

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="list",
        description="List enabled llm endpoints ordered by priority",
    )
    def list_endpoints(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = [
            LLMListItem(
                endpoint_id=endpoint.endpoint_id,
                display_name=endpoint.display_name,
                provider=endpoint.provider,
                api_mode=endpoint.api_mode,
                model_id=endpoint.model_id,
                priority=endpoint.priority,
                enabled=endpoint.enabled,
                supports_reasoning=endpoint.supports_reasoning,
                supports_tools=endpoint.supports_tools,
                supports_streaming=endpoint.supports_streaming,
                supports_vision=endpoint.supports_vision,
            ).__dict__
            for endpoint in self.iter_endpoints()
        ]
        return IntrospectionResult(status=RuntimeStatus.OK, text="llm endpoints", structured={"items": payload})

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="active",
        description="Show the current primary llm endpoint and active model",
    )
    def active(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        snapshot = inspect_llm(self)
        return IntrospectionResult(status=RuntimeStatus.OK, text="llm active endpoint", structured=snapshot.__dict__)

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
        return IntrospectionResult(status=RuntimeStatus.OK, text="llm think level", structured=snapshot.__dict__)

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="endpoint",
        action_name="show",
        description="Show llm endpoint public metadata",
    )
    def show_endpoint(self, call: IntrospectionCall) -> IntrospectionResult:
        endpoint = call.meta.get("resolved_target")
        if not isinstance(endpoint, LLMEndpointModel):
            return IntrospectionResult(status=RuntimeStatus.NOT_FOUND, text="llm endpoint not found")
        snapshot = LLMEndpointSnapshot(
            endpoint_id=endpoint.endpoint_id,
            display_name=endpoint.display_name,
            provider=endpoint.provider,
            api_mode=endpoint.api_mode,
            model_id=endpoint.model_id,
            context_window=endpoint.context_window,
            max_output_tokens=endpoint.max_output_tokens,
            supports_reasoning=endpoint.supports_reasoning,
            supports_tools=endpoint.supports_tools,
            supports_streaming=endpoint.supports_streaming,
            supports_vision=endpoint.supports_vision,
            priority=endpoint.priority,
            enabled=endpoint.enabled,
        )
        return IntrospectionResult(status=RuntimeStatus.OK, text="llm endpoint metadata", structured=snapshot.__dict__)


def inspect_llm(provider: LLMIntrospectionProvider) -> LLMActiveSnapshot:
    active = provider.runtime.endpoint_resolver.primary()
    return LLMActiveSnapshot(
        has_primary_endpoint=active is not None,
        endpoint_id=active.endpoint_id if active is not None else None,
        model_id=active.model_id if active is not None else None,
        provider=active.provider if active is not None else None,
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
