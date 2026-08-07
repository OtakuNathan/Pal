from __future__ import annotations

from pal.execution.tool_semantics import (
    INDIRECT_CONTROL,
    INDIRECT_LOCAL_WRITE,
    INDIRECT_UNSAFE_LOCAL_WRITE,
)
from pal.execution.tool_facade import ToolGuidance

from pal.execution.generated_tool_models import (
    WebFetchCapabilitiesWebFetchIntrospectionProviderReadInput,
    WebFetchCapabilitiesWebFetchIntrospectionProviderScreenshotInput,
    WebFetchCapabilitiesWebFetchIntrospectionProviderScreenshotOutput,
    WebFetchCapabilitiesWebFetchIntrospectionProviderSetActiveProviderInput,
    WebFetchCapabilitiesWebFetchIntrospectionProviderSetAuthMaterialInput,
    WebFetchCapabilitiesWebFetchIntrospectionProviderSetConfigInput,
)
from pal.execution.tool_semantics import DIRECT_EXTERNAL_READ

from dataclasses import dataclass
from pathlib import Path
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
from pal.web_fetch.browser_service import BrowserServiceManager
from pal.web_fetch.contracts import DEFAULT_WEB_FETCH_USER_AGENT, WebFetchRequest
from pal.web_fetch.models import WebFetchProviderModel
from pal.web_fetch.service import WebFetchService
from pal.web_fetch.tools import WebScreenshotTool

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


@dataclass(frozen=True)
class WebFetchModuleSnapshot:
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
    source="builtin:web_fetch",
    target_kind="provider",
    iterable_resolver="iter_providers",
    target_id_resolver="resolve_provider_id",
    target_label_resolver="resolve_provider_label",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="provider",
    kind="provider",
    source="builtin:web_fetch",
    target_kind="provider",
    iterable_resolver="iter_providers",
    target_id_resolver="resolve_provider_id",
    target_label_resolver="resolve_provider_label",
)
@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:web_fetch",
    target_kind="module",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:web_fetch",
    target_kind="module",
)
@dataclass
class WebFetchIntrospectionProvider:
    service: WebFetchService
    artifact_manager: Any | None = None
    read_delegate: Callable[[dict[str, object]], IntrospectionResult] | None = None
    module_id: str = "web_fetch"
    mounted: bool = True
    degraded: bool = False

    def iter_providers(self) -> list[WebFetchProviderModel]:
        return self.service.list_providers()

    def resolve_provider_id(self, provider: WebFetchProviderModel) -> str:
        return provider.provider_id

    def resolve_provider_label(self, provider: WebFetchProviderModel) -> str:
        return provider.provider_id

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="module", action_name="show", description="Show web fetch module state",
        guidance=ToolGuidance(purpose="Show web fetch module state"), aliases=("web_fetch_show",))
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = inspect_web_fetch(self).__dict__
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web fetch module snapshot",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web fetch module snapshot", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="list_providers",
        description="List configured web fetch providers",
        guidance=ToolGuidance(purpose="List configured web fetch providers"),
        aliases=("web_fetch_list_providers",),
    )
    def list_providers(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        items = [self._provider_payload(item) for item in self.iter_providers()]
        payload = {"items": items}
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web fetch providers",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web fetch providers", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="active_provider",
        description="Show configured and effective active web fetch provider",
        guidance=ToolGuidance(purpose="Show configured and effective active web fetch provider"),
        aliases=("web_fetch_active_provider",),
    )
    def active_provider(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = {
            "configured_provider_id": self.service.configured_active_provider_id(),
            "effective_provider_id": self.service.effective_active_provider_id(),
        }
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web fetch active provider",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web fetch active provider", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="provider",
        action_name="show",
        description="Show web fetch provider metadata",
        guidance=ToolGuidance(purpose="Show web fetch provider metadata"),
        aliases=("web_fetch_provider_show",),
    )
    def show_provider(self, call: IntrospectionCall) -> IntrospectionResult:
        provider = self._require_provider(call)
        if provider is None:
            return IntrospectionResult(status=RuntimeStatus.NOT_FOUND, text="web fetch provider not found", llm_text="web fetch provider not found")
        payload = self._provider_payload(provider)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web fetch provider metadata",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web fetch provider metadata", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="provider",
        action_name="auth_state",
        description="Show web fetch provider authorization state",
        guidance=ToolGuidance(purpose="Show web fetch provider authorization state"),
        aliases=("web_fetch_provider_auth_state",),
    )
    def auth_state(self, call: IntrospectionCall) -> IntrospectionResult:
        provider = self._require_provider(call)
        if provider is None:
            return IntrospectionResult(status=RuntimeStatus.NOT_FOUND, text="web fetch provider not found", llm_text="web fetch provider not found")
        payload = self.service.provider_auth_state(provider)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web fetch provider authorization state",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web fetch provider authorization state", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="provider",
        action_name="health",
        description="Show web fetch provider health",
        guidance=ToolGuidance(purpose="Show web fetch provider health"),
        aliases=("web_fetch_provider_health",),
    )
    def health(self, call: IntrospectionCall) -> IntrospectionResult:
        provider = self._require_provider(call)
        if provider is None:
            return IntrospectionResult(status=RuntimeStatus.NOT_FOUND, text="web fetch provider not found", llm_text="web fetch provider not found")
        payload = self.service.provider_health(provider)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web fetch provider health",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web fetch provider health", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="set_active_provider",
        description="Set the configured active web fetch provider",
        guidance=ToolGuidance(purpose="Set the configured active web fetch provider"),
        InputModel=WebFetchCapabilitiesWebFetchIntrospectionProviderSetActiveProviderInput,
        aliases=("web_fetch_set_active_provider",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def set_active_provider(self, call: IntrospectionCall) -> IntrospectionResult:
        provider_id = str(call.args.get("active_provider_id") or "").strip()
        if not provider_id:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="active_provider_id is required", llm_text="active_provider_id is required")
        record = self.service.set_active_provider(provider_id)
        if record is None:
            return IntrospectionResult(status=RuntimeStatus.NOT_FOUND, text="web fetch provider not found", llm_text="web fetch provider not found")
        payload = {
            "configured_provider_id": record.provider_id,
            "effective_provider_id": self.service.effective_active_provider_id(),
        }
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web fetch active provider updated",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web fetch active provider updated", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        action_name="read",
        description="Fetch a webpage using the configured browser fetch provider and internal fallback",
        guidance=ToolGuidance(purpose="Fetch a webpage using the configured browser fetch provider and internal fallback"),
        InputModel=WebFetchCapabilitiesWebFetchIntrospectionProviderReadInput,
        execution=DIRECT_EXTERNAL_READ,
        metadata={"canonical_path": "op_web_read", "omit_family_in_canonical": True},
        aliases=("read_web",),
    )
    def read(self, call: IntrospectionCall) -> IntrospectionResult:
        url = str(call.args.get("url") or "").strip()
        if not url:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="url is required", llm_text="url is required")
        if self.read_delegate is not None:
            return self.read_delegate(dict(call.args))
        try:
            result = self.service.read(
                WebFetchRequest(
                    url=url,
                    timeout_ms=max(1000, int(call.args.get("timeout_ms") or 15000)),
                    max_chars=max(1000, int(call.args.get("max_chars") or 12000)),
                    max_raw_chars=max(0, int(call.args.get("max_raw_chars") or 50000)),
                    max_links=max(0, int(call.args.get("max_links") or 80)),
                    user_agent=str(call.args.get("user_agent") or DEFAULT_WEB_FETCH_USER_AGENT),
                )
            )
        except Exception as exc:
            return IntrospectionResult(
                status=RuntimeStatus.ERROR,
                text="web fetch failed",
                structured={"url": url, "error": str(exc)},
                llm_text=f"web fetch failed: {exc}",
            )
        payload = {
            "requested_url": result.requested_url,
            "final_url": result.final_url,
            "title": result.title,
            "text": result.text,
            "configured_provider_id": result.configured_provider_id,
            "effective_provider_id": result.effective_provider_id,
            "fetch_mode": result.fetch_mode,
            "fallback_used": result.fallback_used,
            "status_code": result.status_code,
            "content_type": result.content_type,
            "content_length": result.content_length,
            "text_truncated": result.text_truncated,
            "raw_content_available": bool(result.raw_content),
            "raw_content_truncated": result.raw_content_truncated,
            "links": [link.to_dict() for link in result.links],
            "metadata": dict(result.metadata or {}),
            "response_headers": dict(result.response_headers or {}),
            "user_agent": result.user_agent,
        }
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web fetch result",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web fetch result", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        action_name="screenshot",
        description="Render a URL in the browser and save a PNG screenshot.",
        guidance=ToolGuidance(
            purpose="Render a URL in the browser and save a PNG screenshot as an artifact.",
            use_when="Only when visual page evidence is needed.",
            do_not_use_when="Not for text extraction (use read_web). Not for API calls.",
            failure_next_steps="Check URL validity and browser availability.",
        ),
        InputModel=WebFetchCapabilitiesWebFetchIntrospectionProviderScreenshotInput,
        OutputModel=WebFetchCapabilitiesWebFetchIntrospectionProviderScreenshotOutput,
        metadata={"canonical_path": "op_web_screenshot", "omit_family_in_canonical": True, "async_required": True},
        aliases=("screenshot_web",),
        execution=INDIRECT_UNSAFE_LOCAL_WRITE,
    )
    async def screenshot(self, call: IntrospectionCall) -> IntrospectionResult:
        return await WebScreenshotTool(
            service=self.service,
            artifact_manager=self.artifact_manager,
        ).ainvoke(
            dict(call.args),
            turn_id=str(call.meta.get("turn_id") or "") or None,
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="provider",
        family="management",
        action_name="enable",
        description="Enable a web fetch provider",
        guidance=ToolGuidance(purpose="Enable a web fetch provider"),
        aliases=("web_fetch_provider_enable",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def enable(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._set_enabled(call, enabled=True)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="provider",
        family="management",
        action_name="disable",
        description="Disable a web fetch provider",
        guidance=ToolGuidance(purpose="Disable a web fetch provider"),
        aliases=("web_fetch_provider_disable",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def disable(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._set_enabled(call, enabled=False)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="provider",
        family="management",
        action_name="set_auth_material",
        description="Update web fetch provider auth material without exposing secrets",
        guidance=ToolGuidance(purpose="Update web fetch provider auth material without exposing secrets"),
        InputModel=WebFetchCapabilitiesWebFetchIntrospectionProviderSetAuthMaterialInput,
        aliases=("web_fetch_provider_set_auth_material",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def set_auth_material(self, call: IntrospectionCall) -> IntrospectionResult:
        provider = self._require_provider(call)
        if provider is None:
            return IntrospectionResult(status=RuntimeStatus.NOT_FOUND, text="web fetch provider not found", llm_text="web fetch provider not found")
        material = call.args.get("material")
        if not isinstance(material, dict):
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="material must be an object", llm_text="material must be an object")
        updated = self.service.set_auth_material(provider.provider_id, dict(material))
        if updated is None:
            return IntrospectionResult(status=RuntimeStatus.NOT_FOUND, text="web fetch provider not found", llm_text="web fetch provider not found")
        payload = self.service.provider_auth_state(updated)
        payload["accepted_keys"] = sorted(str(key) for key in material.keys())
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web fetch provider auth material updated",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web fetch provider auth material updated", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="provider",
        family="management",
        action_name="set_config",
        description="Merge config into a web fetch provider settings blob",
        guidance=ToolGuidance(purpose="Merge config into a web fetch provider settings blob"),
        InputModel=WebFetchCapabilitiesWebFetchIntrospectionProviderSetConfigInput,
        aliases=("web_fetch_provider_set_config",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def set_config(self, call: IntrospectionCall) -> IntrospectionResult:
        provider = self._require_provider(call)
        if provider is None:
            return IntrospectionResult(status=RuntimeStatus.NOT_FOUND, text="web fetch provider not found", llm_text="web fetch provider not found")
        config = call.args.get("config")
        if not isinstance(config, dict):
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="config must be an object", llm_text="config must be an object")
        updated = self.service.set_config(provider.provider_id, dict(config))
        if updated is None:
            return IntrospectionResult(status=RuntimeStatus.NOT_FOUND, text="web fetch provider not found", llm_text="web fetch provider not found")
        payload = self._provider_payload(updated)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web fetch provider config updated",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web fetch provider config updated", payload),
        )

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="lifecycle", action_name="attach", description="Attach web fetch module",
        guidance=ToolGuidance(purpose="Attach web fetch module"), aliases=("web_fetch_attach",), execution=INDIRECT_CONTROL)
    def attach(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        self.mounted = True
        self.degraded = False
        payload = {"mounted": True, "degraded": False}
        return IntrospectionResult(status=RuntimeStatus.OK, text="web fetch attached", structured=payload, llm_text=render_titled_structured_for_llm("Web fetch attached", payload))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="lifecycle", action_name="detach", description="Detach web fetch module",
        guidance=ToolGuidance(purpose="Detach web fetch module"), aliases=("web_fetch_detach",), execution=INDIRECT_CONTROL)
    def detach(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        self.service.shutdown_sync()
        self.mounted = False
        self.degraded = False
        payload = {"mounted": False, "degraded": False}
        return IntrospectionResult(status=RuntimeStatus.OK, text="web fetch detached", structured=payload, llm_text=render_titled_structured_for_llm("Web fetch detached", payload))

    def _set_enabled(self, call: IntrospectionCall, *, enabled: bool) -> IntrospectionResult:
        provider = self._require_provider(call)
        if provider is None:
            return IntrospectionResult(status=RuntimeStatus.NOT_FOUND, text="web fetch provider not found", llm_text="web fetch provider not found")
        updated = self.service.set_enabled(provider.provider_id, enabled)
        if updated is None:
            return IntrospectionResult(status=RuntimeStatus.NOT_FOUND, text="web fetch provider not found", llm_text="web fetch provider not found")
        payload = {"provider_id": updated.provider_id, "enabled": bool(updated.enabled)}
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="web fetch provider state updated",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Web fetch provider state updated", payload),
        )

    def _provider_payload(self, provider: WebFetchProviderModel) -> dict[str, object]:
        return {
            "provider_id": provider.provider_id,
            "provider_kind": provider.provider_kind,
            "display_name": provider.display_name,
            "enabled": bool(provider.enabled),
            "priority": int(provider.priority),
            "settings": dict(provider.settings_blob or {}),
            "auth_keys": sorted(str(key) for key in dict(provider.auth_material_blob or {}).keys()),
            "notes": provider.notes,
        }

    def _require_provider(self, call: IntrospectionCall) -> WebFetchProviderModel | None:
        target = call.meta.get("resolved_target")
        if isinstance(target, WebFetchProviderModel):
            return target
        provider_id = str(call.args.get("target_id") or "").strip()
        if not provider_id:
            return None
        return self.service.get_provider(provider_id)


def inspect_web_fetch(provider: WebFetchIntrospectionProvider) -> WebFetchModuleSnapshot:
    records = provider.service.list_providers()
    enabled = [item for item in records if item.enabled]
    return WebFetchModuleSnapshot(
        provider_count=len(records),
        enabled_provider_count=len(enabled),
        configured_active_provider_id=provider.service.configured_active_provider_id(),
        effective_active_provider_id=provider.service.effective_active_provider_id(),
        mounted=provider.mounted,
        degraded=provider.degraded,
    )


def register_with_core(
    context: MainContext,
    service: WebFetchService,
    *,
    read_delegate: Callable[[dict[str, object]], IntrospectionResult] | None = None,
) -> ModuleHandle:
    provider = WebFetchIntrospectionProvider(
        service=service,
        artifact_manager=context.port_registry.get("artifact:artifact"),
        read_delegate=read_delegate,
    )
    handle = ModuleHandle(
        module_id="web_fetch",
        tier=MODULE_TIER_DETACHABLE,
        detachable=True,
        introspection_provider=provider,
        supports_lifecycle_capabilities=True,
        ports={"web_fetch": service},
        shutdown_sync=service.shutdown_sync,
        shutdown_async=service.shutdown_async,
    )
    context.register_module(handle)
    return handle
