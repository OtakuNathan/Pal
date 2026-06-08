from __future__ import annotations

from dataclasses import dataclass

from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
from pal.plugins.host import PluginHost
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


@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:plugins",
    target_kind="module",
    path_module_id="plugin",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:plugins",
    target_kind="module",
    path_module_id="plugins",
)
@dataclass
class PluginsIntrospectionProvider:
    host: PluginHost
    module_id: str = "plugins"

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="module", action_name="show", description="Show plugin host summary")
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        summary = self.host.show_summary()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="plugin host summary",
            structured=summary,
            llm_text=render_titled_structured_for_llm("Plugin host summary", summary),
        )

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="module", action_name="list", description="List known first-party and third-party plugins")
    def list_plugins(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        items = self.host.list_plugins()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="plugin list",
            structured={"items": items},
            llm_text=render_titled_structured_for_llm("Plugin list", {"items": items}),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="attach",
        description=(
            "Attach a plugin to the current runtime. First-party plugins that are disabled by default can be attached "
            "temporarily; if the result is forbidden with reason=plugin_disabled, call op_plugin_mgmt_enable for that plugin_id."
        ),
        aliases=("attach plugin", "load enabled plugin"),
        args_schema={"type": "object", "properties": {"plugin_id": {"type": "string"}}, "required": ["plugin_id"]},
    )
    def attach(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self.host.attach(str(call.args.get("plugin_id") or ""))
        return IntrospectionResult(
            status=result["status"],
            text="plugin attach result",
            structured=result,
            llm_text=render_titled_structured_for_llm("Plugin attach result", result),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="detach",
        description="Detach a plugin from the current runtime",
        args_schema={"type": "object", "properties": {"plugin_id": {"type": "string"}}, "required": ["plugin_id"]},
    )
    def detach(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self.host.detach(str(call.args.get("plugin_id") or ""))
        return IntrospectionResult(
            status=result["status"],
            text="plugin detach result",
            structured=result,
            llm_text=render_titled_structured_for_llm("Plugin detach result", result),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="enable",
        description="Enable and attach a plugin that is currently disabled, including disabled first-party plugins such as mcp.",
        aliases=("enable plugin", "turn on plugin", "enable mcp plugin", "turn on mcp"),
        args_schema={"type": "object", "properties": {"plugin_id": {"type": "string"}}, "required": ["plugin_id"]},
    )
    def enable(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self.host.enable(str(call.args.get("plugin_id") or ""))
        return IntrospectionResult(
            status=result["status"],
            text="plugin enable result",
            structured=result,
            llm_text=render_titled_structured_for_llm("Plugin enable result", result),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="disable",
        description="Disable a plugin",
        args_schema={"type": "object", "properties": {"plugin_id": {"type": "string"}}, "required": ["plugin_id"]},
    )
    def disable(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self.host.disable(str(call.args.get("plugin_id") or ""))
        return IntrospectionResult(
            status=result["status"],
            text="plugin disable result",
            structured=result,
            llm_text=render_titled_structured_for_llm("Plugin disable result", result),
        )

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="management", action_name="rescan", description="Rescan plugin directories")
    def rescan(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        result = self.host.rescan()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="plugin rescan result",
            structured=result,
            llm_text=render_titled_structured_for_llm("Plugin rescan result", result),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="rescan_and_attach_new_first_party",
        description="Rescan plugin directories and attach newly discovered enabled first-party plugins",
    )
    def rescan_and_attach_new_first_party(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        result = self.host.rescan_and_attach_new_first_party()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="plugin rescan and attach result",
            structured=result,
            llm_text=render_titled_structured_for_llm("Plugin rescan and attach result", result),
        )


def register_with_core(context, host: PluginHost) -> ModuleHandle:
    provider = PluginsIntrospectionProvider(host=host)
    handle = ModuleHandle(
        module_id="plugins",
        tier=MODULE_TIER_CORE_FOUNDATION,
        detachable=False,
        introspection_provider=provider,
        ports={"plugins": host},
    )
    context.register_module(handle)
    return handle
