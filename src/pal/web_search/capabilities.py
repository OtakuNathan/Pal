from __future__ import annotations

from pal.execution.tool_semantics import (
    INDIRECT_CONTROL,
    INDIRECT_LOCAL_WRITE,
)
from pal.execution.tool_facade import ToolGuidance

from pal.execution.generated_tool_models import (
    WebSearchCapabilitiesWebSearchIntrospectionProviderQueryInput,
    WebSearchCapabilitiesWebSearchIntrospectionProviderSetActiveProviderInput,
    WebSearchCapabilitiesWebSearchIntrospectionProviderSetAuthMaterialInput,
    WebSearchCapabilitiesWebSearchIntrospectionProviderSetConfigInput,
)
from pal.execution.tool_semantics import DIRECT_EXTERNAL_READ

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
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
from pal.web_search.contracts import WebSearchQuery
from pal.web_search.models import WebSearchProviderModel
from pal.web_search.service import WebSearchService

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


@dataclass(frozen=True)
class WebSearchModuleSnapshot:
    provider_count: int
    enabled_provider_count: int
    configured_active_provider_id: str | None
    effective_active_provider_id: str | None
    mounted: bool = True
    degraded: bool = False


@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="provider",
    kind="provider",
    source="builtin:web_search",
    target_kind="provider",
    iterable_resolver="iter_providers",
    target_id_resolver="resolve_provider_id",
    target_label_resolver="resolve_provider_label",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="provider",
    kind="provider",
    source="builtin:web_search",
    target_kind="provider",
    iterable_resolver="iter_providers",
    target_id_resolver="resolve_provider_id",
    target_label_resolver="resolve_provider_label",
)
@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:web_search",
    target_kind="module",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:web_search",
    target_kind="module",
)
@dataclass
class WebSearchIntrospectionProvider:
    service: WebSearchService
    query_delegate: Callable[[dict[str, object]], IntrospectionResult] | None = None
    module_id: str = "web_search"
    mounted: bool = True
    degraded: bool = False

    def iter_providers(self) -> list[WebSearchProviderModel]:
        return self.service.list_providers()

    def resolve_provider_id(self, provider: WebSearchProviderModel) -> str:
        return provider.provider_id

    def resolve_provider_label(self, provider: WebSearchProviderModel) -> str:
        return provider.provider_id

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="module", action_name="show",
        guidance=ToolGuidance(
            purpose="Show web search module state.",
            use_when="Diagnosing web search health — provider count, active provider, mounted status.",
            do_not_use_when="Searching the web (use search_web). Listing providers (use web_search_list_providers).",
            failure_next_steps="Read-only. If no active provider, check web_search_list_providers.",
        ), aliases=("web_search_show",))
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = inspect_web_search(self).__dict__
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web search module snapshot",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web search module snapshot", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="list_providers",
        guidance=ToolGuidance(
            purpose="List configured web search providers.",
            use_when="Discovering available search backends and their enabled status.",
            do_not_use_when="Checking the active provider (use web_search_active_provider). Searching (use search_web).",
            failure_next_steps="Read-only. If empty, no providers are configured.",
        ),
        aliases=("web_search_list_providers",),
    )
    def list_providers(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        items = [self._provider_payload(item) for item in self.iter_providers()]
        payload = {"items": items}
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web search providers",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web search providers", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="active_provider",
        guidance=ToolGuidance(
            purpose="Show the active web search provider.",
            use_when="Checking which search backend handles queries.",
            do_not_use_when="Listing all providers (use web_search_list_providers). Switching (use web_search_set_active_provider).",
            failure_next_steps="If no active provider is reported, use web_search_list_providers to find an enabled provider, then select it with web_search_set_active_provider.",
        ),
        aliases=("web_search_active_provider",),
    )
    def active_provider(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = {
            "configured_provider_id": self.service.configured_active_provider_id(),
            "effective_provider_id": self.service.effective_active_provider_id(),
        }
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web search active provider",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web search active provider", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="provider",
        action_name="show",
        guidance=ToolGuidance(
            purpose="Show one web search provider's metadata.",
            use_when="Inspecting a specific provider's kind, settings, auth keys.",
            do_not_use_when="Module health (use web_search_show). Auth state (use web_search_provider_auth_state).",
            failure_next_steps="If NOT_FOUND, verify the provider name with web_search_list_providers.",
        ),
        aliases=("web_search_provider_show",),
    )
    def show_provider(self, call: IntrospectionCall) -> IntrospectionResult:
        provider = self._require_provider(call)
        if provider is None:
            return IntrospectionResult(status=RuntimeStatus.NOT_FOUND, text="web search provider not found", llm_text="web search provider not found")
        payload = self._provider_payload(provider)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web search provider metadata",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web search provider metadata", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="provider",
        action_name="auth_state",
        guidance=ToolGuidance(
            purpose="Show one web search provider's authorization state.",
            use_when="Diagnosing auth failures or checking if API keys are configured.",
            do_not_use_when="Applying credentials (use web_search_provider_set_auth_material). Provider metadata (use web_search_provider_show).",
            failure_next_steps="If NOT_FOUND, verify the provider name with web_search_list_providers. If not authorized, apply credentials.",
        ),
        aliases=("web_search_provider_auth_state",),
    )
    def auth_state(self, call: IntrospectionCall) -> IntrospectionResult:
        provider = self._require_provider(call)
        if provider is None:
            return IntrospectionResult(status=RuntimeStatus.NOT_FOUND, text="web search provider not found", llm_text="web search provider not found")
        payload = self.service.provider_auth_state(provider)
        payload["name"] = provider.provider_id
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web search provider authorization state",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web search provider authorization state", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="provider",
        action_name="health",
        guidance=ToolGuidance(
            purpose="Show one web search provider's health.",
            use_when="Diagnosing search failures or connectivity issues.",
            do_not_use_when="Auth state (use web_search_provider_auth_state). Module health (use web_search_show).",
            failure_next_steps="If unhealthy, try switching providers with web_search_set_active_provider.",
        ),
        aliases=("web_search_provider_health",),
    )
    def health(self, call: IntrospectionCall) -> IntrospectionResult:
        provider = self._require_provider(call)
        if provider is None:
            return IntrospectionResult(status=RuntimeStatus.NOT_FOUND, text="web search provider not found", llm_text="web search provider not found")
        payload = self.service.provider_health(provider)
        payload["name"] = provider.provider_id
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web search provider health",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web search provider health", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="set_active_provider",
        guidance=ToolGuidance(
            purpose="Set the active web search provider.",
            use_when="Switching to a different enabled search backend.",
            do_not_use_when="Checking the active provider (use web_search_active_provider). The target provider is disabled (enable it first).",
            failure_next_steps="If NOT_FOUND, verify the provider name with web_search_list_providers. If disabled, enable the provider before selecting it.",
        ),
        InputModel=WebSearchCapabilitiesWebSearchIntrospectionProviderSetActiveProviderInput,
        aliases=("web_search_set_active_provider",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def set_active_provider(self, call: IntrospectionCall) -> IntrospectionResult:
        provider_id = str(call.args.get("name") or "").strip()
        if not provider_id:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="name is required", llm_text="name is required")
        existing = self.service.get_provider(provider_id)
        if existing is not None and not existing.enabled:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="web search provider is disabled",
                structured={"name": provider_id, "reason": "provider_disabled"},
                llm_text="web search provider is disabled; enable it before selecting it",
            )
        record = self.service.set_active_provider(provider_id)
        if record is None:
            return IntrospectionResult(status=RuntimeStatus.NOT_FOUND, text="web search provider not found", llm_text="web search provider not found")
        payload = {
            "configured_provider_id": record.provider_id,
            "effective_provider_id": self.service.effective_active_provider_id(),
        }
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web search active provider updated",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web search active provider updated", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        action_name="query",
        guidance=ToolGuidance(
            purpose="Search the web with the configured provider and internal fallback.",
            use_when="Looking up current external facts, documentation, or comparing sources.",
            do_not_use_when="Fetching a specific webpage (use read_web). Reading local files (use read_file).",
            failure_next_steps="A failed call has already exhausted the configured fallback providers. Inspect provider health and authorization, then enable, repair, or select a healthy provider before retrying.",
        ),
        InputModel=WebSearchCapabilitiesWebSearchIntrospectionProviderQueryInput,
        execution=DIRECT_EXTERNAL_READ,
        metadata={"canonical_path": "op_web_search", "omit_family_in_canonical": True},
        aliases=("search_web",),
    )
    def query(self, call: IntrospectionCall) -> IntrospectionResult:
        query_text = str(call.args.get("query") or "").strip()
        if not query_text:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="query is required", llm_text="query is required")
        if self.query_delegate is not None:
            return self.query_delegate(dict(call.args))
        limit = max(1, min(10, int(call.args.get("limit") or 5)))
        try:
            result = self.service.query(
                WebSearchQuery(
                    query=query_text,
                    limit=limit,
                    region=str(call.args.get("region") or "").strip(),
                    safe_search=str(call.args.get("safe_search") or "").strip(),
                )
            )
        except Exception as exc:
            return IntrospectionResult(
                status=RuntimeStatus.ERROR,
                text="web search failed",
                structured={"query": query_text, "error": str(exc)},
                llm_text=f"web search failed: {exc}",
            )
        payload = {
            "items": [item.__dict__ for item in result.items],
            "configured_provider_id": result.configured_provider_id,
            "effective_provider_id": result.effective_provider_id,
            "fallback_used": result.fallback_used,
        }
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web search results",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web search results", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="provider",
        family="management",
        action_name="enable",
        guidance=ToolGuidance(
            purpose="Enable a web search provider.",
            use_when="Re-enabling a disabled search provider.",
            do_not_use_when="Disabling (use web_search_provider_disable). Setting active (use web_search_set_active_provider).",
            failure_next_steps="If NOT_FOUND, verify the provider name with web_search_list_providers.",
        ),
        aliases=("web_search_provider_enable",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def enable(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._set_enabled(call, enabled=True)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="provider",
        family="management",
        action_name="disable",
        guidance=ToolGuidance(
            purpose="Disable a web search provider.",
            use_when="Temporarily removing a provider from the active pool.",
            do_not_use_when="Enabling (use web_search_provider_enable).",
            failure_next_steps="If NOT_FOUND, verify the provider name with web_search_list_providers.",
        ),
        aliases=("web_search_provider_disable",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def disable(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._set_enabled(call, enabled=False)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="provider",
        family="management",
        action_name="set_auth_material",
        guidance=ToolGuidance(
            purpose="Apply auth material to a web search provider without exposing secrets.",
            use_when="A provider needs API keys or credentials to function.",
            do_not_use_when="Reading auth state (use web_search_provider_auth_state).",
            failure_next_steps="If NOT_FOUND, verify the provider name with web_search_list_providers. Check provider docs for required auth fields.",
        ),
        InputModel=WebSearchCapabilitiesWebSearchIntrospectionProviderSetAuthMaterialInput,
        aliases=("web_search_provider_set_auth_material",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def set_auth_material(self, call: IntrospectionCall) -> IntrospectionResult:
        provider = self._require_provider(call)
        if provider is None:
            return IntrospectionResult(status=RuntimeStatus.NOT_FOUND, text="web search provider not found", llm_text="web search provider not found")
        material = call.args.get("material")
        if not isinstance(material, dict):
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="material must be an object", llm_text="material must be an object")
        updated = self.service.set_auth_material(provider.provider_id, dict(material))
        if updated is None:
            return IntrospectionResult(status=RuntimeStatus.NOT_FOUND, text="web search provider not found", llm_text="web search provider not found")
        payload = self.service.provider_auth_state(updated)
        payload["name"] = updated.provider_id
        payload["accepted_keys"] = sorted(str(key) for key in material.keys())
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web search provider auth material updated",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web search provider auth material updated", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="provider",
        family="management",
        action_name="set_config",
        guidance=ToolGuidance(
            purpose="Merge config into a web search provider's settings blob.",
            use_when="Tuning provider-specific settings (e.g. result count, safe search defaults).",
            do_not_use_when="Setting auth material (use web_search_provider_set_auth_material).",
            failure_next_steps="If NOT_FOUND, verify the provider name with web_search_list_providers.",
        ),
        InputModel=WebSearchCapabilitiesWebSearchIntrospectionProviderSetConfigInput,
        aliases=("web_search_provider_set_config",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def set_config(self, call: IntrospectionCall) -> IntrospectionResult:
        provider = self._require_provider(call)
        if provider is None:
            return IntrospectionResult(status=RuntimeStatus.NOT_FOUND, text="web search provider not found", llm_text="web search provider not found")
        config = call.args.get("config")
        if not isinstance(config, dict):
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="config must be an object", llm_text="config must be an object")
        updated = self.service.set_config(provider.provider_id, dict(config))
        if updated is None:
            return IntrospectionResult(status=RuntimeStatus.NOT_FOUND, text="web search provider not found", llm_text="web search provider not found")
        payload = self._provider_payload(updated)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web search provider config updated",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web search provider config updated", payload),
        )

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="lifecycle", action_name="attach",
        guidance=ToolGuidance(
            purpose="Attach web search module.",
            use_when="Reconnecting a detached web search module.",
            do_not_use_when="Enabling one provider (use web_search_provider_enable). Already attached.",
            failure_next_steps="This attach is idempotent. If the module still appears detached or degraded, inspect web_search_show and provider health before retrying.",
        ), aliases=("web_search_attach",), execution=INDIRECT_CONTROL)
    def attach(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        self.mounted = True
        self.degraded = False
        payload = {"mounted": True, "degraded": False}
        return IntrospectionResult(status=RuntimeStatus.OK, text="web search attached", structured=payload, llm_text=render_titled_structured_for_llm("Web search attached", payload))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="lifecycle", action_name="detach",
        guidance=ToolGuidance(
            purpose="Detach web search module.",
            use_when="Temporarily stopping all web search functionality.",
            do_not_use_when="Disabling one provider (use web_search_provider_disable).",
            failure_next_steps="Re-attach with web_search_attach.",
        ), aliases=("web_search_detach",), execution=INDIRECT_CONTROL)
    def detach(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        self.mounted = False
        self.degraded = False
        payload = {"mounted": False, "degraded": False}
        return IntrospectionResult(status=RuntimeStatus.OK, text="web search detached", structured=payload, llm_text=render_titled_structured_for_llm("Web search detached", payload))

    def _set_enabled(self, call: IntrospectionCall, *, enabled: bool) -> IntrospectionResult:
        provider = self._require_provider(call)
        if provider is None:
            return IntrospectionResult(status=RuntimeStatus.NOT_FOUND, text="web search provider not found", llm_text="web search provider not found")
        updated = self.service.set_enabled(provider.provider_id, enabled)
        if updated is None:
            return IntrospectionResult(status=RuntimeStatus.NOT_FOUND, text="web search provider not found", llm_text="web search provider not found")
        payload = {"name": updated.provider_id, "provider_id": updated.provider_id, "enabled": bool(updated.enabled)}
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web search provider state updated",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web search provider state updated", payload),
        )

    def _provider_payload(self, provider: WebSearchProviderModel) -> dict[str, object]:
        return {
            "name": provider.provider_id,
            "provider_id": provider.provider_id,
            "provider_kind": provider.provider_kind,
            "display_name": provider.display_name,
            "enabled": bool(provider.enabled),
            "priority": int(provider.priority),
            "settings": dict(provider.settings_blob or {}),
            "auth_keys": sorted(str(key) for key in dict(provider.auth_material_blob or {}).keys()),
            "notes": provider.notes,
        }

    def _require_provider(self, call: IntrospectionCall) -> WebSearchProviderModel | None:
        target = call.meta.get("resolved_target")
        if isinstance(target, WebSearchProviderModel):
            return target
        provider_id = str(call.args.get("target_id") or "").strip()
        if not provider_id:
            return None
        return self.service.get_provider(provider_id)


def inspect_web_search(provider: WebSearchIntrospectionProvider) -> WebSearchModuleSnapshot:
    records = provider.service.list_providers()
    enabled = [item for item in records if item.enabled]
    return WebSearchModuleSnapshot(
        provider_count=len(records),
        enabled_provider_count=len(enabled),
        configured_active_provider_id=provider.service.configured_active_provider_id(),
        effective_active_provider_id=provider.service.effective_active_provider_id(),
        mounted=provider.mounted,
        degraded=provider.degraded,
    )


def register_with_core(
    context: MainContext,
    service: WebSearchService,
    *,
    query_delegate: Callable[[dict[str, object]], IntrospectionResult] | None = None,
) -> ModuleHandle:
    provider = WebSearchIntrospectionProvider(
        service=service,
        query_delegate=query_delegate,
    )
    handle = ModuleHandle(
        module_id="web_search",
        tier=MODULE_TIER_DETACHABLE,
        detachable=True,
        introspection_provider=provider,
        supports_lifecycle_capabilities=True,
        ports={"web_search": service},
    )
    context.register_module(handle)
    return handle
