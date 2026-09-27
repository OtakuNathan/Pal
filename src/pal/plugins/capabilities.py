from __future__ import annotations

from pal.execution.tool_semantics import (
    DIRECT_LOCAL_READ,
    INDIRECT_CONTROL,
    INDIRECT_LOCAL_READ,
    INDIRECT_UNSAFE_LOCAL_WRITE,
)
from pal.execution.tool_facade import NextToolHint, ToolGuidance
from pal.packages.jobs import PackageJobs
from pal.packages.notifications import PackageCompletionSource
from pal.packages.tool_models import PackageInstallInput, PackagePrepareInput, PackageStatusInput, PluginUninstallInput
from pathlib import Path

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
    package_jobs: PackageJobs | None = None
    completion_events: PackageCompletionSource | None = None

    def jobs(self) -> PackageJobs:
        if self.package_jobs is None:
            self.package_jobs = PackageJobs(self.host)
        return self.package_jobs

    def shutdown_packages(self) -> None:
        if self.completion_events is not None:
            self.completion_events.close()
        if self.package_jobs is not None:
            self.package_jobs.shutdown()

    def _package_result(self, action, **args) -> IntrospectionResult:
        try:
            payload = action(**args)
            status = RuntimeStatus.ERROR if payload.get("status") == "failed" else RuntimeStatus.OK
        except Exception as exc:
            payload = {"error": str(exc)}
            status = RuntimeStatus.ERROR
        return IntrospectionResult(status=status, text="Package operation", structured=payload,
                                   llm_text=render_titled_structured_for_llm("Package operation", payload))

    def _start_package_job(self, call: IntrospectionCall, operation: str, **args) -> IntrospectionResult:
        if self.completion_events is None:
            self.completion_events = PackageCompletionSource(self.host.context)
        return self._package_result(
            self.jobs().start, operation=operation,
            wait_ms=call.args.get("wait_ms", 1000),
            on_complete=self.completion_events.notifier(call.meta.get("turn_id")),
            **args,
        )

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="package", action_name="install",
        guidance=ToolGuidance(purpose="Install a local plugin or channel provider package, prepare its private dependencies, and activate it through its owner.",
            use_when="A plugin or channel provider .palpkg, or legacy provider .whl, is ready to install or upgrade. The call briefly waits for completion; longer jobs report notification availability. A scheduled completion event wakes Pal to continue the initiating task; do not poll.", do_not_use_when="Only dependencies of an installed package need repair; use package_prepare.",
            failure_next_steps="Read package_status. Failure does not confirm installation or activation; retry after correcting the reported cause.",
            next_tool_hints=(NextToolHint(name="package_status", use_when="Notification is unavailable, the outcome is uncertain, or detailed diagnostics are needed."),)),
        InputModel=PackageInstallInput, aliases=("package_install",), execution=INDIRECT_UNSAFE_LOCAL_WRITE)
    def package_install(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._start_package_job(call, "install", path=Path(call.args["path"]))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="package", action_name="prepare",
        guidance=ToolGuidance(purpose="Prepare or repair an installed package's dependencies without modifying Pal's Python environment.",
            use_when="An installed plugin, channel provider, or builtin such as web_fetch has missing runtime dependencies; select its package kind. Briefly waits for completion; if a completion notice is scheduled, do not poll.",
            do_not_use_when="Installing a new artifact; use package_install.",
            failure_next_steps="Inspect package_status for the stage and cause. Missing system privileges or configuration must be resolved before retrying.",
            next_tool_hints=(NextToolHint(name="package_status", use_when="Notification is unavailable, the outcome is uncertain, or preparation diagnostics are needed."),)),
        InputModel=PackagePrepareInput, aliases=("package_prepare",), execution=INDIRECT_UNSAFE_LOCAL_WRITE)
    def package_prepare(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._start_package_job(call, "prepare", name=call.args["name"], kind=call.args.get("kind", "plugin"))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="management", action_name="uninstall",
        guidance=ToolGuidance(purpose="Uninstall a third-party plugin through detach and remove its installation registration; retain data by default.",
            use_when="The user wants to remove an installed community plugin, optionally clearing declared owned data.",
            do_not_use_when="Temporary detach or disabling startup. Built-in plugins and channel providers cannot be uninstalled here.",
            failure_next_steps="Inspect package_status and retry the same uninstall. Cleanup failure preserves remaining resources; undeclared data cannot be purged.",
            next_tool_hints=(NextToolHint(name="package_status", use_when="Notification is unavailable, or retained data and unfinished cleanup need inspection."),)),
        InputModel=PluginUninstallInput, aliases=("plugin_uninstall",), execution=INDIRECT_UNSAFE_LOCAL_WRITE)
    def uninstall(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._start_package_job(call, "uninstall", name=call.args["name"],
                                    purge_data=bool(call.args.get("purge_data", False)))

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="module", family="package", action_name="status",
        guidance=ToolGuidance(purpose="Inspect package installation stages, failures, private environments and activation results.",
            use_when="Inspecting progress or diagnosing dependencies; use job_id and bounded wait_ms when no completion notification is available.", do_not_use_when="Repeated polling when a completion notice is scheduled. Reading a plugin's application data.",
            failure_next_steps="Unknown jobs may belong to another runtime root; verify the selected runtime."),
        InputModel=PackageStatusInput, aliases=("package_status",), execution=INDIRECT_LOCAL_READ)
    def package_status(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._package_result(self.jobs().status, job_id=call.args.get("job_id"), wait_ms=call.args.get("wait_ms", 0))

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
            use_when="When you need to find which module owns a capability, or how to detach/attach a specific plugin (e.g. bunshin, mcp). The authoritative source for module ownership and lifecycle state.",
            do_not_use_when="Checking core/channel/execution internals (use their own show/observe). Searching capabilities by function (use search_tools).",
            failure_next_steps="Read-only. If a plugin is not listed, it may not be installed — check plugin directories or run plugin_rescan.",
            next_tool_hints=(
                NextToolHint(name="package_install", use_when="Install a prepared plugin package and its private dependencies."),
                NextToolHint(name="package_prepare", use_when="Prepare or repair dependencies of an existing plugin."),
                NextToolHint(name="package_status", use_when="Inspect package preparation or installation progress."),
            ),
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
            purpose="Load an enabled plugin into the current runtime; an already attached instance is preserved.",
            use_when="Reconnecting a detached plugin that is already enabled.",
            do_not_use_when="Reloading changed plugin code (use plugin_reattach). Attaching a disabled plugin (use plugin_enable — it enables and attaches in one step). Detaching (use plugin_detach).",
            failure_next_steps="If disabled, use plugin_enable, which also attaches it. If cleanup is pending, fix the reported cause and use plugin_reattach. If the plugin name is unknown, check plugins_list.",
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
        action_name="reattach",
        guidance=ToolGuidance(
            purpose="Reload an enabled plugin's code and runtime instance in one operation, restoring affected dependents.",
            use_when="Plugin implementation changed or its runtime needs restarting. A detached enabled plugin is loaded. No prior detach is needed.",
            do_not_use_when="Loading a detached plugin without replacing a live instance (use plugin_attach). Enabling a disabled plugin (use plugin_enable). Resident core changes require a host restart; channel provider code uses channel_reload_provider.",
            failure_next_steps="Inspect plugins_list for load or cleanup errors and affected dependents. Correct the reported cause before retrying; reload can interrupt dependent plugins and does not promise rollback.",
        ),
        aliases=("plugin_reattach",),
        InputModel=PluginsCapabilitiesPluginsIntrospectionProviderAttachInput,
        execution=INDIRECT_CONTROL,
    )
    def reattach(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self.host.reattach(str(call.args.get("name") or ""))
        return IntrospectionResult(
            status=result["status"],
            text="plugin reattach result",
            structured=result,
            llm_text=render_titled_structured_for_llm("Plugin reattach result", result),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="detach",
        guidance=ToolGuidance(
            purpose="Detach a plugin's runtime instance without disabling it.",
            use_when="Temporarily removing a plugin's capabilities from the runtime (e.g. isolating a misbehaving plugin).",
            do_not_use_when="Uninstalling plugin files (use plugin_uninstall) or persistently disabling startup (use plugin_disable). Detaching a channel endpoint (use channel_detach).",
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
            do_not_use_when="Reloading or restarting one specific plugin (use plugin_reattach). Rescanning channel providers (use channel_provider_rescan).",
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


def build_management_handle(host: PluginHost) -> ModuleHandle:
    provider = PluginsIntrospectionProvider(host=host)
    return ModuleHandle(
        module_id="plugins",
        tier=MODULE_TIER_CORE_FOUNDATION,
        detachable=False,
        introspection_provider=provider,
        ports={"plugins": host},
        shutdown_sync=provider.shutdown_packages,
    )


def register_with_core(context, host: PluginHost) -> ModuleHandle:
    """Compatibility helper for isolated tests; production mounts this on Core."""
    handle = build_management_handle(host)
    context.register_module(handle)
    return handle
