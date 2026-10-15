from __future__ import annotations

from pal.execution.contracts import CapabilityResult
from pal.execution.tool_facade import EffectOutcome, EffectReceipt, ToolGuidance

from pal.execution.generated_tool_models import (
    WebSearchCapabilitiesWebSearchIntrospectionProviderQueryInput,
)
from pal.execution.tool_semantics import DIRECT_EXTERNAL_READ, INDIRECT_LOCAL_READ

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

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
from pal.shared.inspection import inspection_snapshot
from pal.web_search.contracts import WebSearchQuery
from pal.web_search.models import WebSearchProviderModel
from pal.web_search.service import WebSearchService
from pal.foundation.diagnostics import diagnostic_summary, diagnostic_text, exception_report, exception_summary

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

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="show",
        guidance=ToolGuidance(
            search_objects=('state', 'hub', 'provider', 'providers', 'backend', 'backends'),
            search_terms=('list', 'status', 'active', 'current', 'selected', 'health', 'auth', 'authentication', 'credentials', 'settings'),
            purpose="Inspect the web search hub and every configured provider's public configuration, authentication and last-request health in one call.",
            use_when="Listing search backends or diagnosing selection, missing credentials and search failures. Takes no arguments and always returns all providers. Last-request health is an observation, not a fresh connectivity probe.",
            do_not_use_when="Searching the web (use search_web).",
            failure_next_steps="An empty provider list means none are configured. Use the official web-search CLI for authorized configuration repairs; detailed request reports remain with the resident owner.",
        ),
        execution=INDIRECT_LOCAL_READ,
        aliases=("inspect_web_search",),
    )
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = inspection_snapshot(
            {**inspect_web_search(self).__dict__, "health_basis": "configuration_and_last_request"},
            sections={"providers": lambda: [self._provider_snapshot(provider) for provider in self.iter_providers()]},
        )
        providers = payload.get("providers")
        if isinstance(providers, list):
            for provider in providers:
                for section, reason in provider["diagnostics"]["unavailable_sections"].items():
                    key = f"providers.{provider['name']}.{section}"
                    payload["diagnostics"]["unavailable_sections"][key] = reason
        payload["diagnostics"]["partial"] = bool(payload["diagnostics"]["unavailable_sections"])
        payload = _compact_inspection_errors(payload)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web search snapshot",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web search snapshot", payload),
        )

    def list_providers(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        items = [{"name": item.provider_id, "provider_id": item.provider_id,
                  "provider_kind": item.provider_kind, "enabled": item.enabled}
                 for item in self.iter_providers()]
        payload = {"items": items}
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web search providers",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web search providers", payload),
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

    def show_provider(self, call: IntrospectionCall) -> IntrospectionResult:
        provider = self._require_provider(call)
        if provider is None:
            return IntrospectionResult(status=RuntimeStatus.NOT_FOUND, text="web search provider not found", llm_text="web search provider not found")
        payload = self._provider_snapshot(provider)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web search provider metadata",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web search provider metadata", payload),
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

    # Configuration maintenance is exposed through the official Pal CLI.
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
            search_objects=('page', 'pages', 'result', 'results'),
            search_terms=('find', 'lookup', 'internet', 'online',),
            purpose="Search the web with the configured provider and internal fallback.",
            use_when="Looking up current external facts, documentation, or comparing sources.",
            do_not_use_when="Reading a new webpage at a known URL (use read_browser_page, which also returns content). Rereading the current page (use read_browser_page). Reading local files (use read_file).",
            failure_next_steps="A failed call has already exhausted configured fallback providers. Use inspect_web_search for provider selection, authorization and recent request errors. For authorized backend configuration repairs, use pal.self.maintenance and the web-search CLI before retrying; do not repeat an unchanged failed search.",
        ),
        InputModel=WebSearchCapabilitiesWebSearchIntrospectionProviderQueryInput,
        execution=DIRECT_EXTERNAL_READ,
        metadata={"canonical_path": "op_web_search", "omit_family_in_canonical": True},
        aliases=("search_web",),
    )
    def query(self, call: IntrospectionCall) -> IntrospectionResult | CapabilityResult:
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
            diagnostic = exception_report(exc)
            brief = diagnostic_text(exception_summary(exc), limit=1200)
            return CapabilityResult(
                status=RuntimeStatus.ERROR,
                text="web search failed",
                structured={"query": query_text, "error_code": "web_search_failed", "error": diagnostic},
                llm_text="web search failed: " + brief,
                llm_failure_details={"query": query_text, "error_code": "web_search_failed", "error": brief},
                effect_receipt=EffectReceipt(outcome=EffectOutcome.NONE),
            )
        payload = {
            "items": [item.__dict__ for item in result.items],
            "configured_provider_id": result.configured_provider_id,
            "effective_provider_id": result.effective_provider_id,
            "fallback_used": result.fallback_used,
            "provider_errors": result.provider_errors,
        }
        visible = {
            "items": [{"title": item.title, "url": item.url, "snippet": item.snippet} for item in result.items],
            "effective_provider_id": result.effective_provider_id,
            "fallback_used": result.fallback_used,
        }
        if result.provider_errors:
            visible["provider_errors"] = [
                {"provider_id": error.get("provider_id"),
                 "error": diagnostic_text(diagnostic_summary(str(error.get("error") or "")), limit=240)}
                for error in result.provider_errors[:5]
            ]
            if len(result.provider_errors) > 5:
                visible["additional_provider_errors"] = len(result.provider_errors) - 5
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web search results",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web search results", visible),
        )

    # Configuration maintenance is exposed through the official Pal CLI.
    def enable(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._set_enabled(call, enabled=True)

    # Configuration maintenance is exposed through the official Pal CLI.
    def disable(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._set_enabled(call, enabled=False)

    # Configuration maintenance is exposed through the official Pal CLI.
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

    # Configuration maintenance is exposed through the official Pal CLI.
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

    def _provider_snapshot(self, provider: WebSearchProviderModel) -> dict[str, Any]:
        return _compact_inspection_errors(inspection_snapshot(self._provider_payload(provider), sections={
            "auth": lambda: self.service.provider_auth_state(provider),
            "health": lambda: self.service.provider_health(provider),
        }))

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


def _compact_inspection_errors(value: Any) -> Any:
    """Keep owner diagnostics intact while bounding model-facing error reports."""
    if isinstance(value, dict):
        # Reserve room for the truncation marker within a 600-character budget.
        return {
            key: diagnostic_text(diagnostic_summary(item), limit=560)
            if key in {"error", "last_error"} and isinstance(item, str)
            else _compact_inspection_errors(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_compact_inspection_errors(item) for item in value]
    return value


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
        ports={"web_search": service},
    )
    context.register_module(handle)
    return handle
