from __future__ import annotations

from pal.execution.tool_semantics import (
    INDIRECT_LOCAL_WRITE,
)
from pal.execution.tool_facade import ToolGuidance

from pal.execution.generated_tool_models import (
    LlmCapabilitiesLLMIntrospectionProviderSetActiveEndpointInput,
    LlmCapabilitiesLLMIntrospectionProviderShowInput,
)

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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
    wire_shape: str
    priority: int


@dataclass(frozen=True)
class LLMModelSnapshot:
    model_id: str
    endpoint_id: str
    display_name: str | None
    provider: str
    wire_shape: str
    context_window: int | None
    max_output_tokens: int | None
    thinking_levels: tuple[str, ...]
    default_thinking_level: str
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
    wire_shape: str | None
    context_window: int | None
    max_output_tokens: int | None
    thinking_levels: tuple[str, ...]
    default_thinking_level: str | None
    supports_tools: bool | None
    supports_streaming: bool | None
    supports_vision: bool | None
    priority: int | None
    enabled: bool | None


@dataclass(frozen=True)
class LLMThinkLevelSnapshot:
    endpoint_id: str | None
    persisted_think_level: str | None
    effective_think_level: str | None
    available_levels: tuple[str, ...]


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
        return list(self.runtime.endpoint_resolver.enabled())

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="list",
        guidance=ToolGuidance(
            purpose="List enabled LLM endpoints ordered by priority.",
            use_when="Discovering available model endpoints, their provider, wire shape, and priority.",
            do_not_use_when="Checking the current active model (use llm_active). Inspecting one endpoint in depth (use llm_show).",
            failure_next_steps="Read-only. If empty, no endpoints are configured or enabled.",
        ),
        aliases=("llm_list",),
    )
    def list_endpoints(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = [
            {
                **LLMModelListItem(
                model_id=endpoint.model_id,
                endpoint_id=endpoint.endpoint_id,
                display_name=getattr(endpoint, "display_name", None),
                provider=endpoint.provider,
                wire_shape=str(getattr(endpoint, "wire_shape", "") or ""),
                priority=int(getattr(endpoint, "priority", 0) or 0),
                ).__dict__,
                "name": endpoint.endpoint_id,
            }
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
        guidance=ToolGuidance(
            purpose="Show the current active LLM model metadata.",
            use_when="Checking which model endpoint is currently selected for requests.",
            do_not_use_when="Listing all endpoints (use llm_list). Switching endpoints (use llm_set_active_endpoint).",
            failure_next_steps="Read-only. If no active model, check llm_list for available endpoints.",
        ),
        aliases=("llm_active",),
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
        guidance=ToolGuidance(
            purpose="Show public metadata for one enabled LLM endpoint by name.",
            use_when="Inspecting one endpoint's context window, max output, thinking levels, capabilities.",
            do_not_use_when="Checking the active model (use llm_active). Listing all endpoints (use llm_list).",
            failure_next_steps="If NOT_FOUND, verify the endpoint name with llm_list.",
        ),
        InputModel=LlmCapabilitiesLLMIntrospectionProviderShowInput,
        aliases=("llm_show",),
    )
    def show_model(self, call: IntrospectionCall) -> IntrospectionResult:
        endpoint_id = str(call.args.get("name") or "").strip()
        if not endpoint_id:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="name is required",
                llm_text="name is required",
            )
        endpoint = self._find_endpoint(endpoint_id)
        if endpoint is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="llm endpoint not found",
                structured={"endpoint_id": endpoint_id},
                llm_text="llm endpoint not found",
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
        guidance=ToolGuidance(
            purpose="Show the active endpoint's thinking level choices and current selection.",
            use_when="Checking or deciding which reasoning level (e.g. low/medium/high) the active model uses.",
            do_not_use_when="Checking token usage (use llm_usage). Switching endpoints (use llm_set_active_endpoint).",
            failure_next_steps="Read-only. Thinking levels are provider-declared; if empty, the endpoint may not support thinking.",
        ),
        aliases=("llm_think_level",),
    )
    def think_level(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        status = self.runtime.thinking_status()
        endpoint_id = str(status.get("endpoint_id") or "").strip() or None
        snapshot = LLMThinkLevelSnapshot(
            endpoint_id=endpoint_id,
            persisted_think_level=(
                self.runtime.settings_repository.get_think_level(endpoint_id)
                if endpoint_id is not None
                else None
            ),
            effective_think_level=str(status.get("current") or "").strip() or None,
            available_levels=tuple(
                str(choice.get("id") or "")
                for choice in status.get("choices") or []
                if str(choice.get("id") or "").strip()
            ),
        )
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="llm think level",
            structured=snapshot.__dict__,
            llm_text=render_titled_structured_for_llm("LLM think level", snapshot.__dict__),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="usage",
        guidance=ToolGuidance(
            purpose="Show resident-process LLM usage statistics — requests, tokens, cache hit rate, cost.",
            use_when="Monitoring token consumption, cache performance, or cost across the current process lifetime.",
            do_not_use_when="Checking model metadata (use llm_active or llm_show).",
            failure_next_steps="Read-only. Stats reset on process restart. If all zeros, no LLM requests have been made yet.",
        ),
        aliases=("llm_usage",),
    )
    def usage(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = llm_status_payload(self)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="llm usage status",
            structured=payload,
            llm_text=render_titled_structured_for_llm("LLM usage status", payload),
        )

    def handle_status_control_action(self, action: Any) -> dict[str, Any]:
        _ = action
        payload = llm_status_payload(self)
        return {
            "message": render_llm_status(payload),
            "usage": payload["usage"],
            "active_model": payload["active_model"],
        }

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="set_active_endpoint",
        guidance=ToolGuidance(
            purpose="Switch the active LLM endpoint for future requests.",
            use_when="The user asks to switch models (e.g. to a different provider, a faster/cheaper model, or one with vision).",
            do_not_use_when="Checking the current model (use llm_active). Listing endpoints (use llm_list).",
            failure_next_steps="If NOT_FOUND, verify the endpoint name with llm_list. Only enabled endpoints can be activated.",
        ),
        InputModel=LlmCapabilitiesLLMIntrospectionProviderSetActiveEndpointInput,
        aliases=("llm_set_active_endpoint",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def set_active_endpoint(self, call: IntrospectionCall) -> IntrospectionResult:
        endpoint_id = str(call.args.get("name") or "").strip()
        if not endpoint_id:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="name is required",
                llm_text="name is required",
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

    def _find_endpoint(self, endpoint_id: str) -> LLMEndpointModel | None:
        return next(
            (
                endpoint
                for endpoint in self.iter_endpoints()
                if endpoint.endpoint_id == endpoint_id
            ),
            None,
        )


def _model_snapshot(endpoint: LLMEndpointModel) -> LLMModelSnapshot:
    return LLMModelSnapshot(
        model_id=endpoint.model_id,
        endpoint_id=endpoint.endpoint_id,
        display_name=getattr(endpoint, "display_name", None),
        provider=endpoint.provider,
        wire_shape=str(getattr(endpoint, "wire_shape", "") or ""),
        context_window=getattr(endpoint, "context_window", None),
        max_output_tokens=getattr(endpoint, "max_output_tokens", None),
        thinking_levels=tuple(str(item) for item in (getattr(endpoint, "thinking_levels_blob", None) or ())),
        default_thinking_level=str(getattr(endpoint, "default_thinking_level", "") or "off"),
        supports_tools=bool(getattr(endpoint, "supports_tools", False)),
        supports_streaming=bool(getattr(endpoint, "supports_streaming", False)),
        supports_vision=bool(getattr(endpoint, "supports_vision", False)),
        priority=int(getattr(endpoint, "priority", 0) or 0),
        enabled=bool(getattr(endpoint, "enabled", True)),
    )


def inspect_llm(provider: LLMIntrospectionProvider) -> LLMActiveModelSnapshot:
    active = provider.runtime.endpoint_resolver.primary(preferred_endpoint_id=provider.runtime.active_endpoint_id)
    if active is None:
        return LLMActiveModelSnapshot(
            has_active_model=False,
            active_endpoint_id=provider.runtime.active_endpoint_id,
            model_id=None,
            endpoint_id=None,
            display_name=None,
            provider=None,
            wire_shape=None,
            context_window=None,
            max_output_tokens=None,
            thinking_levels=(),
            default_thinking_level=None,
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
        wire_shape=snapshot.wire_shape,
        context_window=snapshot.context_window,
        max_output_tokens=snapshot.max_output_tokens,
        thinking_levels=snapshot.thinking_levels,
        default_thinking_level=snapshot.default_thinking_level,
        supports_tools=snapshot.supports_tools,
        supports_streaming=snapshot.supports_streaming,
        supports_vision=snapshot.supports_vision,
        priority=snapshot.priority,
        enabled=snapshot.enabled,
    )


def llm_status_payload(provider: LLMIntrospectionProvider) -> dict[str, Any]:
    active = inspect_llm(provider).__dict__
    snapshot = getattr(provider.runtime, "usage_snapshot", None)
    usage = snapshot() if callable(snapshot) else {
        "scope": "resident_process",
        "request_count": 0,
        "successful_request_count": 0,
        "failed_request_count": 0,
        "provider_request_count": 0,
        "provider_response_count": 0,
        "failed_attempt_count": 0,
        "usage_reported_request_count": 0,
        "usage_reporting_rate": 0.0,
        "input_tokens": 0,
        "uncached_input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cost": 0.0,
        "reported": False,
        "cache_hit_rate": 0.0,
        "by_endpoint": [],
    }
    return {"active_model": active, "usage": dict(usage)}


def render_llm_status(payload: dict[str, Any]) -> str:
    active = dict(payload.get("active_model") or {})
    usage = dict(payload.get("usage") or {})
    endpoint_id = str(active.get("endpoint_id") or active.get("active_endpoint_id") or "-")
    model_id = str(active.get("model_id") or "-")
    token_cache_ratio = max(0.0, float(usage.get("token_cache_ratio") or usage.get("cache_hit_rate") or 0.0))
    request_hit_rate = max(0.0, float(usage.get("request_cache_hit_rate") or 0.0))
    cache_write_ratio = max(0.0, float(usage.get("cache_write_ratio") or 0.0))
    cache_policy = dict(usage.get("prompt_cache_policy") or {})
    reporting_rate = max(0.0, float(usage.get("usage_reporting_rate") or 0.0))
    lines = [
        "LLM status",
        f"Active: {endpoint_id} ({model_id})",
        f"Statistics scope: resident process since {usage.get('started_at') or '-'}",
        "",
        (
            "Logical requests: "
            f"{int(usage.get('successful_request_count') or 0)} successful, "
            f"{int(usage.get('failed_request_count') or 0)} failed"
        ),
        (
            "Provider attempts: "
            f"{int(usage.get('provider_request_count') or 0)} total, "
            f"{int(usage.get('provider_response_count') or 0)} completed, "
            f"{int(usage.get('failed_attempt_count') or 0)} failed"
        ),
        "",
        (
            "Input tokens: "
            f"{int(usage.get('input_tokens') or 0)} total; "
            f"{int(usage.get('uncached_input_tokens') or 0)} uncached, "
            f"{int(usage.get('cached_input_tokens') or 0)} cache reads, "
            f"{int(usage.get('cache_write_input_tokens') or 0)} cache writes"
        ),
        f"Prompt cache token ratio: {token_cache_ratio:.1%}",
        f"Prompt cache request hit rate: {request_hit_rate:.1%}",
        f"Prompt cache write ratio: {cache_write_ratio:.1%}",
        (
            "Prompt cache policy: "
            f"{cache_policy.get('dialect') or 'none'}; "
            f"decision={cache_policy.get('decision') or 'not_planned'}; "
            f"breakpoints={','.join(cache_policy.get('breakpoints') or []) or '-'}"
        ),
        "",
        (
            "Output tokens: "
            f"{int(usage.get('output_tokens') or 0)} total; "
            f"{int(usage.get('reasoning_tokens') or 0)} reasoning"
        ),
        f"Provider-reported cost: {float(usage.get('cost') or 0.0):.6f}",
        f"Usage reporting coverage: {reporting_rate:.1%}",
    ]
    endpoint_rows = [
        dict(item)
        for item in list(usage.get("by_endpoint") or [])
        if isinstance(item, dict)
    ]
    if endpoint_rows:
        lines.append("")
        lines.append("By endpoint:")
        for item in endpoint_rows:
            endpoint_cache_rate = max(
                0.0,
                float(item.get("cache_hit_rate") or 0.0),
            )
            lines.append(
                "  "
                f"{item.get('endpoint_id') or 'unknown'} "
                f"({item.get('model_id') or '-'}): "
                f"{int(item.get('successful_request_count') or 0)} successful, "
                f"{int(item.get('failed_request_count') or 0)} failed; "
                f"{int(item.get('input_tokens') or 0)} input, "
                f"{endpoint_cache_rate:.1%} cache hit"
            )
    return "\n".join(lines)


def register_with_core(context: MainContext, runtime: LLMRuntime) -> ModuleHandle:
    provider = LLMIntrospectionProvider(runtime=runtime)
    handle = ModuleHandle(
        module_id="llm",
        tier=MODULE_TIER_CORE_FOUNDATION,
        detachable=False,
        introspection_provider=provider,
        control_action_handlers={
            "show_llm_status": provider.handle_status_control_action,
        },
        ports={"llm": runtime},
        shutdown_sync=runtime.close,
    )
    context.register_module(handle)
    return handle
