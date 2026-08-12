from __future__ import annotations

from pal.execution.tool_semantics import (
    DIRECT_LOCAL_READ,
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

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="module", action_name="show",
        guidance=ToolGuidance(
            purpose="Show plugin host summary.",
            use_when="Diagnosing plugin system health — how many plugins are loaded, enabled, attached.",
            do_not_use_when="Listing specific plugins with details (use plugins_list). Checking one module's capabilities (use search_tools).",
            failure_next_steps="Read-only diagnostic. If a plugin is missing, check plugins_list or run plugin_rescan.",
        ), aliases=("plugins_show",))
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        summary = self.host.show_summary()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="plugin host summary",
            structured=summary,
            llm_text=render_titled_structured_for_llm("Plugin host summary", summary),
        )

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="module", action_name="list",
        guidance=ToolGuidance(
            purpose="List known first-party and third-party plugins with usable names and enabled/attached status.",
            use_when="When you need to find which module owns a capability, or how to detach/attach a specific plugin (e.g. minion, mcp). The authoritative source for module ownership and lifecycle state.",
            do_not_use_when="Checking core/channel/execution internals (use their own show/observe). Searching capabilities by function (use search_tools).",
            failure_next_steps="Read-only. If a plugin is not listed, it may not be installed — check plugin directories or run plugin_rescan.",
        ), aliases=("plugins_list",), execution=DIRECT_LOCAL_READ)
    def list_plugins(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        items = [
            {**dict(item), "name": str(item.get("plugin_id") or item.get("module_id") or "")}
            for item in self.host.list_plugins()
        ]
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
        guidance=ToolGuidance(
            purpose="Attach an enabled plugin's runtime instance to the current runtime.",
            use_when="Reconnecting a detached plugin that is already enabled.",
            do_not_use_when="Attaching a disabled plugin (use plugin_enable — it enables and attaches in one step). Detaching (use plugin_detach).",
            failure_next_steps="If disabled, call plugin_enable first. If the plugin name is unknown, check plugins_list.",
        ),
        aliases=("plugin_attach",),
        InputModel=PluginsCapabilitiesPluginsIntrospectionProviderAttachInput,
        execution=INDIRECT_CONTROL,
    )
    def attach(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self.host.attach(str(call.args.get("name") or ""))
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
        guidance=ToolGuidance(
            purpose="Detach a plugin's runtime instance without disabling it.",
            use_when="Temporarily removing a plugin's capabilities from the runtime (e.g. isolating a misbehaving plugin).",
            do_not_use_when="Permanently removing a plugin (use plugin_disable). Detaching a channel endpoint (use channel_detach).",
            failure_next_steps="If the plugin name is unknown, check plugins_list. Detached plugins can be re-attached with plugin_attach.",
        ),
        InputModel=PluginsCapabilitiesPluginsIntrospectionProviderDetachInput,
        aliases=("plugin_detach",),
        execution=INDIRECT_CONTROL,
    )
    def detach(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self.host.detach(str(call.args.get("name") or ""))
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
        guidance=ToolGuidance(
            purpose="Enable and attach a disabled plugin in one step, including disabled first-party plugins such as mcp.",
            use_when="A plugin is disabled and needs to be fully activated. This is the primary way to turn on a plugin.",
            do_not_use_when="Attaching an already-enabled but detached plugin (use plugin_attach — lighter weight).",
            failure_next_steps="If the plugin name is unknown, check plugins_list. If already enabled, use plugin_attach instead.",
        ),
        aliases=("plugin_enable",),
        InputModel=PluginsCapabilitiesPluginsIntrospectionProviderEnableInput,
        execution=INDIRECT_CONTROL,
    )
    def enable(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self.host.enable(str(call.args.get("name") or ""))
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
        guidance=ToolGuidance(
            purpose="Disable a plugin — detach its runtime and mark it as disabled so it won't auto-attach on restart.",
            use_when="Permanently removing a plugin from the runtime until explicitly re-enabled.",
            do_not_use_when="Temporarily removing capabilities (use plugin_detach — keeps it enabled for quick re-attach). Disabling a channel endpoint (use channel_disable).",
            failure_next_steps="If the plugin name is unknown, check plugins_list. Re-enable with plugin_enable.",
        ),
        InputModel=PluginsCapabilitiesPluginsIntrospectionProviderDisableInput,
        aliases=("plugin_disable",),
        execution=INDIRECT_CONTROL,
    )
    def disable(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self.host.disable(str(call.args.get("name") or ""))
        return IntrospectionResult(
            status=result["status"],
            text="plugin disable result",
            structured=result,
            llm_text=render_titled_structured_for_llm("Plugin disable result", result),
        )

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="management", action_name="rescan",
        guidance=ToolGuidance(
            purpose="Rescan plugin directories to discover newly installed or updated plugins.",
            use_when="New plugins were installed or plugin configuration files changed.",
            do_not_use_when="Restarting one specific plugin (use plugin_attach after detach, or plugin_enable). Rescanning channel providers (use channel_provider_rescan).",
            failure_next_steps="If scan_errors occur, check plugin manifest files. Previous plugin generation is preserved on error.",
        ), aliases=("plugin_rescan",), execution=INDIRECT_CONTROL)
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
        guidance=ToolGuidance(
            purpose="Rescan plugin directories and auto-attach newly discovered enabled first-party plugins.",
            use_when="After installing new first-party plugins that should be picked up and attached immediately.",
            do_not_use_when="Rescanning only (use plugin_rescan). Attaching one specific plugin (use plugin_attach or plugin_enable).",
            failure_next_steps="If attach_errors occur, check plugins_list for which plugins failed and try plugin_attach individually.",
        ),
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
