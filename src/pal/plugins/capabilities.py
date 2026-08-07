from __future__ import annotations

from pal.execution.tool_semantics import (
    INDIRECT_CONTROL,
)
from pal.execution.tool_facade import ToolGuidance

from pal.execution.generated_tool_models import (
    PluginsCapabilitiesPluginsIntrospectionProviderAttachInput,
    PluginsCapabilitiesPluginsIntrospectionProviderDetachInput,
    PluginsCapabilitiesPluginsIntrospectionProviderDisableInput,
    PluginsCapabilitiesPluginsIntrospectionProviderEnableInput,
)

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

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="module", action_name="show", description="Show plugin host summary",
        guidance=ToolGuidance(purpose="Show plugin host summary"), aliases=("plugins_show",))
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        summary = self.host.show_summary()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="plugin host summary",
            structured=summary,
            llm_text=render_titled_structured_for_llm("Plugin host summary", summary),
        )

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="module", action_name="list", description="List known first-party and third-party plugins",
        guidance=ToolGuidance(purpose="List known first-party and third-party plugins"), aliases=("plugins_list",))
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
        description="Attach a plugin to the current runtime.",
        guidance=ToolGuidance(
            purpose="Attach a plugin to the current runtime.",
            use_when="When the user asks to enable/attach a plugin. If forbidden with reason=plugin_disabled, call plugin_enable first.",
            do_not_use_when="Not for detaching (use plugin_detach). Not for listing (use plugin_list).",
            failure_next_steps="Correct the plugin_id; check plugin_list for available plugins.",
        ),
        aliases=("plugin_attach",),
        InputModel=PluginsCapabilitiesPluginsIntrospectionProviderAttachInput,
        execution=INDIRECT_CONTROL,
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
        guidance=ToolGuidance(purpose="Detach a plugin from the current runtime"),
        InputModel=PluginsCapabilitiesPluginsIntrospectionProviderDetachInput,
        aliases=("plugin_detach",),
        execution=INDIRECT_CONTROL,
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
        guidance=ToolGuidance(purpose="Enable and attach a plugin that is currently disabled, including disabled first-party plugins such as mcp."),
        aliases=("plugin_enable",),
        InputModel=PluginsCapabilitiesPluginsIntrospectionProviderEnableInput,
        execution=INDIRECT_CONTROL,
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
        guidance=ToolGuidance(purpose="Disable a plugin"),
        InputModel=PluginsCapabilitiesPluginsIntrospectionProviderDisableInput,
        aliases=("plugin_disable",),
        execution=INDIRECT_CONTROL,
    )
    def disable(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self.host.disable(str(call.args.get("plugin_id") or ""))
        return IntrospectionResult(
            status=result["status"],
            text="plugin disable result",
            structured=result,
            llm_text=render_titled_structured_for_llm("Plugin disable result", result),
        )

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="management", action_name="rescan", description="Rescan plugin directories",
        guidance=ToolGuidance(purpose="Rescan plugin directories"), aliases=("plugin_rescan",), execution=INDIRECT_CONTROL)
    def rescan(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        result = self.host.rescan()
        return IntrospectionResult(
            status=(
                RuntimeStatus.ERROR
                if result.get("scan_errors")
                else RuntimeStatus.OK
            ),
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
        guidance=ToolGuidance(purpose="Rescan plugin directories and attach newly discovered enabled first-party plugins"),
        aliases=("plugin_rescan_and_attach_new_first_party",),
        execution=INDIRECT_CONTROL,
    )
    def rescan_and_attach_new_first_party(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        result = self.host.rescan_and_attach_new_first_party()
        status = (
            RuntimeStatus.ERROR
            if result.get("scan_errors") or result.get("attach_errors")
            else RuntimeStatus.OK
        )
        return IntrospectionResult(
            status=status,
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
