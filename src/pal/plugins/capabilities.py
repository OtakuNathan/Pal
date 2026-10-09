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
from typing import TYPE_CHECKING

from pal.execution.generated_tool_models import (
    PluginsCapabilitiesPluginsIntrospectionProviderAttachInput,
    PluginsCapabilitiesPluginsIntrospectionProviderDetachInput,
    PluginsCapabilitiesPluginsIntrospectionProviderDisableInput,
    PluginsCapabilitiesPluginsIntrospectionProviderEnableInput,
)

from dataclasses import dataclass

from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
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
from pal.shared.diagnostics import exception_report

if TYPE_CHECKING:
    from pal.plugins.host import PluginHost


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
            payload = {"error": exception_report(exc), "error_code": "package_operation_failed"}
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
        guidance=ToolGuidance(search_objects=('package', 'packages', 'plugin', 'plugins', 'provider', 'providers'), purpose="Install a local plugin or channel provider package, prepare its private dependencies, and activate it through its owner.",
            use_when="A plugin or channel provider .palpkg, or legacy provider .whl, is ready to install or upgrade. The call briefly waits for completion; longer jobs report notification availability. A scheduled completion event wakes Pal to continue the initiating task; do not poll.", do_not_use_when="Only dependencies of an installed package need repair; use prepare_package.",
            failure_next_steps="Read inspect_package_status. Failure does not confirm installation or activation; retry after correcting the reported cause.",
            next_tool_hints=(NextToolHint(name="inspect_package_status", use_when="Notification is unavailable, the outcome is uncertain, or detailed diagnostics are needed."),)),
        InputModel=PackageInstallInput, aliases=("install_package",), execution=INDIRECT_UNSAFE_LOCAL_WRITE)
    def package_install(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._start_package_job(call, "install", path=Path(call.args["path"]))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="package", action_name="prepare",
        guidance=ToolGuidance(search_objects=('package', 'packages', 'dependency', 'dependencies'), purpose="Prepare or repair an installed package's dependencies without modifying Pal's Python environment.",
            use_when="An installed plugin, channel provider, or builtin such as web_fetch has missing runtime dependencies; select its package kind. Briefly waits for completion; if a completion notice is scheduled, do not poll.",
            do_not_use_when="Installing a new artifact; use install_package.",
            failure_next_steps="Use inspect_package_status for the stage and cause. Missing system privileges or configuration must be resolved before retrying.",
            next_tool_hints=(NextToolHint(name="inspect_package_status", use_when="Notification is unavailable, the outcome is uncertain, or preparation diagnostics are needed."),)),
        InputModel=PackagePrepareInput, aliases=("prepare_package",), execution=INDIRECT_UNSAFE_LOCAL_WRITE)
    def package_prepare(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._start_package_job(call, "prepare", name=call.args["name"], kind=call.args.get("kind", "plugin"))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="management", action_name="uninstall",
        guidance=ToolGuidance(search_objects=('plugin', 'plugins'), purpose="Uninstall a third-party plugin through detach and remove its installation registration; retain data by default.",
            use_when="The user wants to remove an installed community plugin, optionally clearing declared owned data.",
            do_not_use_when="Temporary detach or disabling startup. Built-in plugins and channel providers cannot be uninstalled here.",
            failure_next_steps="Use inspect_package_status and retry the same uninstall. Cleanup failure preserves remaining resources; undeclared data cannot be purged.",
            next_tool_hints=(NextToolHint(name="inspect_package_status", use_when="Notification is unavailable, or retained data and unfinished cleanup need inspection."),)),
        InputModel=PluginUninstallInput, aliases=("uninstall_plugin",), execution=INDIRECT_UNSAFE_LOCAL_WRITE)
    def uninstall(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._start_package_job(call, "uninstall", name=call.args["name"],
                                    purge_data=bool(call.args.get("purge_data", False)))

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="module", family="package", action_name="status",
        guidance=ToolGuidance(search_objects=('status',), purpose="Inspect package installation stages, failures, private environments and activation results.",
            use_when="Inspecting progress or diagnosing dependencies; use job_id and bounded wait_ms when no completion notification is available.", do_not_use_when="Repeated polling when a completion notice is scheduled. Reading a plugin's application data.",
            failure_next_steps="Unknown jobs may belong to another runtime root; verify the selected runtime."),
        InputModel=PackageStatusInput, aliases=("inspect_package_status",), execution=INDIRECT_LOCAL_READ)
    def package_status(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self._package_result(self.jobs().status, job_id=call.args.get("job_id"), wait_ms=call.args.get("wait_ms", 0))
        if self.completion_events is not None:
            self.completion_events.stage_status_delivery(call, result)
        return result

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="module", action_name="show",
        guidance=ToolGuidance(
            search_objects=('host', 'hosts'),
            purpose="Show plugin host summary.",
            use_when='Diagnosing plugin system health — how many plugins are loaded, enabled, attached. If an expected plugin is missing, inspect list_plugins and its manifest; rescan_plugins discovers metadata changes.',
            do_not_use_when="Listing specific plugins with details (use list_plugins). Checking one module's capabilities (use search_tools).",
            failure_next_steps="Read-only diagnostic. If a plugin is missing, check list_plugins or run rescan_plugins.",
        ), aliases=("inspect_plugin_host",))
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
            search_objects=('plugin', 'plugins'),
            purpose="List known first-party and third-party plugins with usable names and enabled/attached status.",
            use_when='When you need to find which module owns a capability, or how to detach/attach a specific plugin (e.g. bunshin, mcp). The authoritative source for module ownership and lifecycle state. Missing entries can indicate an undiscovered or invalid manifest; inspect the configured plugin directory and rescan_plugins results.',
            do_not_use_when="Checking core/channel/execution internals (use their own show/observe). Searching capabilities by function (use search_tools).",
            failure_next_steps="Read-only. If a plugin is not listed, it may not be installed — check plugin directories or run rescan_plugins.",
            next_tool_hints=(
                NextToolHint(name="install_package", use_when="Install a prepared plugin package and its private dependencies."),
                NextToolHint(name="prepare_package", use_when="Prepare or repair dependencies of an existing plugin."),
                NextToolHint(name="inspect_package_status", use_when="Inspect package preparation or installation progress."),
            ),
        ), aliases=("list_plugins",), execution=DIRECT_LOCAL_READ)
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
            search_objects=('plugin', 'plugins'),
            purpose="Load an enabled plugin into the current runtime; an already attached instance is preserved.",
            use_when="Reconnecting a detached plugin that is already enabled.",
            do_not_use_when="Reloading changed plugin code (use reload_plugin). Attaching a disabled plugin (use enable_plugin — it enables and attaches in one step). Detaching (use detach_plugin).",
            failure_next_steps="If disabled, use enable_plugin, which also attaches it. If cleanup is pending, fix the reported cause and use reload_plugin. If the plugin name is unknown, check list_plugins.",
        ),
        aliases=("attach_plugin",),
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
            search_objects=('plugin', 'plugins'),
            purpose="Reload an enabled plugin's code and runtime instance in one operation, restoring affected dependents.",
            use_when="Plugin implementation changed or its runtime needs restarting. A detached enabled plugin is loaded. No prior detach is needed.",
            do_not_use_when="Loading a detached plugin without replacing a live instance (use attach_plugin). Enabling a disabled plugin (use enable_plugin). Resident core changes require a host restart; channel provider code uses reload_channel_provider.",
            failure_next_steps="Inspect list_plugins for load or cleanup errors and affected dependents. Correct the reported cause before retrying; reload can interrupt dependent plugins and does not promise rollback.",
        ),
        aliases=("reload_plugin",),
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
            search_objects=('plugin', 'plugins'),
            purpose="Detach a plugin's runtime instance without disabling it.",
            use_when="Temporarily removing a plugin's capabilities from the runtime (e.g. isolating a misbehaving plugin). Use attach_plugin to restore an enabled detached instance, or reload_plugin when its implementation changed.",
            do_not_use_when="Uninstalling plugin files (use uninstall_plugin) or persistently disabling startup (use disable_plugin). Detaching a channel endpoint (use detach_channel_endpoint).",
            failure_next_steps="If the plugin name is unknown, check list_plugins. Detached plugins can be re-attached with attach_plugin.",
        ),
        InputModel=PluginsCapabilitiesPluginsIntrospectionProviderDetachInput,
        aliases=("detach_plugin",),
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
            search_objects=('plugin', 'plugins'),
            purpose="Enable and attach a disabled plugin in one step, including disabled first-party plugins such as mcp.",
            use_when="A plugin is disabled and needs to be fully activated. This is the primary way to turn on a plugin.",
            do_not_use_when="Attaching an already-enabled but detached plugin (use attach_plugin — lighter weight).",
            failure_next_steps="If the plugin name is unknown, check list_plugins. If already enabled, use attach_plugin instead.",
        ),
        aliases=("enable_plugin",),
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
            search_objects=('plugin', 'plugins'),
            purpose="Disable a plugin — detach its runtime and mark it as disabled so it won't auto-attach on restart.",
            use_when='Permanently removing a plugin from the runtime until explicitly re-enabled. Use enable_plugin to enable and attach it again.',
            do_not_use_when="Temporarily removing capabilities (use detach_plugin — keeps it enabled for quick re-attach). Disabling a channel endpoint (use disable_channel_endpoint).",
            failure_next_steps="If the plugin name is unknown, check list_plugins. Re-enable with enable_plugin.",
        ),
        InputModel=PluginsCapabilitiesPluginsIntrospectionProviderDisableInput,
        aliases=("disable_plugin",),
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
            search_objects=('plugin', 'plugins'),
            purpose="Rescan plugin directories to discover newly installed or updated plugins.",
            use_when='New plugins were installed or plugin configuration files changed. Inspect scan_errors and attachment diagnostics in the payload independently of the wrapper status; rescan alone does not reload existing code.',
            do_not_use_when="Reloading or restarting one specific plugin (use reload_plugin). Rescanning channel providers (use rescan_channel_providers).",
            failure_next_steps="If scan_errors occur, check plugin manifest files. Previous plugin generation is preserved on error.",
        ), aliases=("rescan_plugins",), execution=INDIRECT_CONTROL)
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
            search_objects=('plugin', 'plugins'),
            purpose="Rescan plugin directories and auto-attach newly discovered enabled first-party plugins.",
            use_when='After installing new first-party plugins that should be picked up and attached immediately. Inspect scan_errors and attach_errors independently of the wrapper status; partial discovery or attachment is not complete activation.',
            do_not_use_when="Rescanning only (use rescan_plugins). Attaching one specific plugin (use attach_plugin or enable_plugin).",
            failure_next_steps="If attach_errors occur, check list_plugins for which plugins failed and try attach_plugin individually.",
        ),
        aliases=("rescan_and_attach_first_party_plugins",),
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
