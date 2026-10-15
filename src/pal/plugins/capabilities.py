from __future__ import annotations

from pal.execution.tool_semantics import (
    INDIRECT_CONTROL,
    INDIRECT_LOCAL_READ,
    INDIRECT_UNSAFE_LOCAL_WRITE,
)
from pal.execution.tool_facade import EffectOutcome, EffectReceipt, NextToolHint, StrictToolModel, ToolGuidance
from pydantic import Field
from pal.packages.jobs import PackageJobs
from pal.packages.notifications import PackageCompletionSource
from pal.packages.tool_models import PackageInstallInput, PackagePrepareInput, PackageStatusInput, PluginUninstallInput
from pathlib import Path
from typing import TYPE_CHECKING, Literal

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
from pal.shared.inspection import inspection_snapshot
from pal.shared.diagnostics import exception_report

if TYPE_CHECKING:
    from pal.plugins.host import PluginHost


class PluginInspectionInput(StrictToolModel):
    name: str | None = Field(None, min_length=1, pattern=r"\S", description="Exact plugin name from list_plugins. Omit for hub health and counts.")


class PluginManagementInput(StrictToolModel):
    name: str = Field(min_length=1, pattern=r"\S")
    operation: Literal["attach", "detach", "reload", "enable", "disable"]


class PluginRescanInput(StrictToolModel):
    mode: Literal["scan", "scan_and_attach_new_first_party"] = "scan"


@dataclass(frozen=True)
class _LifecycleResult(IntrospectionResult):
    # Preserve the public introspection shape while carrying the committed
    # logical-unload receipt through the capability adapter on cleanup failure.
    effect_receipt: EffectReceipt | None = None


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
        guidance=ToolGuidance(search_objects=('package', 'packages', 'dependency', 'dependencies'), search_terms=('repair',), purpose="Prepare or repair an installed package's dependencies without modifying Pal's Python environment.",
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
        guidance=ToolGuidance(search_objects=('status',), search_terms=('state', 'show', 'get', 'installation',), purpose="Inspect package installation stages, failures, private environments and activation results.",
            use_when="Inspecting progress or diagnosing dependencies; use job_id and bounded wait_ms when no completion notification is available.", do_not_use_when="Repeated polling when a completion notice is scheduled. Reading a plugin's application data.",
            failure_next_steps="Unknown jobs may belong to another runtime root; verify the selected runtime."),
        InputModel=PackageStatusInput, aliases=("inspect_package_status",), execution=INDIRECT_LOCAL_READ)
    def package_status(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self._package_result(self.jobs().status, job_id=call.args.get("job_id"), wait_ms=call.args.get("wait_ms", 0))
        if self.completion_events is not None:
            self.completion_events.stage_status_delivery(call, result)
        return result

    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        summary = inspection_snapshot(self.host.show_summary())
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
        ), aliases=("list_plugins",), execution=INDIRECT_LOCAL_READ)
    def list_plugins(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        items = [
            {**dict(item), "name": str(item.get("plugin_id") or item.get("module_id") or "")}
            for item in self.host.list_plugin_summaries()
        ]
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="plugin list",
            structured={"items": items},
            llm_text=render_titled_structured_for_llm("Plugin list", {"items": items}),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE, scope="module", action_name="inspect",
        guidance=ToolGuidance(search_intents=('inspect plugin loading failure and dependencies', 'check plugin generation and recent errors'),
            search_objects=('plugin', 'plugins'),
            search_terms=('state', 'status', 'health', 'details', 'generation', 'dependency', 'dependencies', 'cleanup', 'error', 'errors', 'installation'),
            purpose="Inspect plugin hub health and counts, or one named plugin's complete lifecycle, dependencies, cleanup and installation state.",
            use_when="Omit name for hub health, scan errors and counts. Supply name for one plugin's load, reload, dependencies, cleanup, installation and recent errors. Dependencies are references, not recursively expanded plugin states.",
            do_not_use_when="Discovering plugin names (use list_plugins). Specific asynchronous package jobs (use inspect_package_status).",
            failure_next_steps="Follow blocked_by, cleanup errors and installation diagnostics; missing names can be discovered with list_plugins or rescan_plugins.",
        ), InputModel=PluginInspectionInput, aliases=("inspect_plugin",),
    )
    def inspect(self, call: IntrospectionCall) -> IntrospectionResult:
        if not call.args.get("name"):
            return self.show(call)
        payload = self.host.inspect_plugin(str(call.args["name"]).strip())
        if payload is None:
            return IntrospectionResult(status=RuntimeStatus.NOT_FOUND, text="plugin not found",
                                       structured={"reason": "unknown_plugin"}, llm_text="Find the plugin name with list_plugins.")
        return IntrospectionResult(status=RuntimeStatus.OK, text="plugin state", structured=payload,
                                   llm_text=render_titled_structured_for_llm("Plugin", payload))

    @capability_action(
        namespace=OPERATION_NAMESPACE, scope="module", family="management", action_name="manage",
        guidance=ToolGuidance(
            search_objects=('plugin', 'plugins', 'provider'),
            search_terms=('load', 'unload', 'restart', 'code', 'channel', 'activate', 'deactivate'),
            search_enum_fields=('operation',),
            purpose="Attach, detach, reload, enable or disable one plugin through its lifecycle owner.",
            use_when="Choose attach to load an enabled detached plugin while preserving an existing instance; detach to temporarily unload; reload to replace changed code and owned resources, restoring affected dependents; enable to persistently enable and load; disable to unload and persistently disable. All operations take the same exact plugin name.",
            do_not_use_when="Reconnecting one channel endpoint (use manage_channel_endpoint(operation=restart)). Installing or uninstalling files (use package tools). Metadata scanning (use rescan_plugins). Resident core changes require host activation.",
            failure_next_steps="Use inspect_plugin for the selected plugin's errors, blockers and cleanup state. Reconcile incomplete cleanup before retrying. Reload can interrupt dependents and does not promise rollback. Discover names with list_plugins.",
        ), InputModel=PluginManagementInput, aliases=("manage_plugin",), execution=INDIRECT_CONTROL,
    )
    def manage(self, call: IntrospectionCall) -> IntrospectionResult:
        handler = {
            "attach": self.attach, "detach": self.detach, "reload": self.reattach,
            "enable": self.enable, "disable": self.disable,
        }[call.args["operation"]]
        return handler(call)

    def attach(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self.host.attach(str(call.args.get("name") or ""))
        return _LifecycleResult(
            status=result["status"],
            text="plugin attach result",
            structured=result,
            effect_receipt=(EffectReceipt(outcome=EffectOutcome.APPLIED) if result.get("logical_unload_committed") else None),
            recovery_hint=str(result.get("recovery_hint") or ""),
            llm_text=render_titled_structured_for_llm("Plugin attach result", result),
        )

    def reattach(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self.host.reattach(str(call.args.get("name") or ""))
        return _LifecycleResult(
            status=result["status"],
            text="plugin reattach result",
            structured=result,
            effect_receipt=(EffectReceipt(outcome=EffectOutcome.APPLIED) if result.get("logical_unload_committed") else None),
            recovery_hint=str(result.get("recovery_hint") or ""),
            llm_text=render_titled_structured_for_llm("Plugin reattach result", result),
        )

    def detach(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self.host.detach(str(call.args.get("name") or ""))
        return _LifecycleResult(
            status=result["status"],
            text="plugin detach result",
            structured=result,
            effect_receipt=(EffectReceipt(outcome=EffectOutcome.APPLIED) if result.get("logical_unload_committed") else None),
            recovery_hint=str(result.get("recovery_hint") or ""),
            llm_text=render_titled_structured_for_llm("Plugin detach result", result),
        )

    def enable(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self.host.enable(str(call.args.get("name") or ""))
        return _LifecycleResult(
            status=result["status"],
            text="plugin enable result",
            structured=result,
            effect_receipt=(EffectReceipt(outcome=EffectOutcome.APPLIED) if result.get("logical_unload_committed") else None),
            recovery_hint=str(result.get("recovery_hint") or ""),
            llm_text=render_titled_structured_for_llm("Plugin enable result", result),
        )

    def disable(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self.host.disable(str(call.args.get("name") or ""))
        return _LifecycleResult(
            status=result["status"],
            text="plugin disable result",
            structured=result,
            effect_receipt=(EffectReceipt(outcome=EffectOutcome.APPLIED) if result.get("logical_unload_committed") else None),
            recovery_hint=str(result.get("recovery_hint") or ""),
            llm_text=render_titled_structured_for_llm("Plugin disable result", result),
        )

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="management", action_name="rescan",
        guidance=ToolGuidance(
            search_objects=('plugin', 'plugins'),
            search_terms=('discover', 'scan',),
            search_enum_fields=("mode",),
            purpose="Rescan plugin metadata, optionally attaching only newly discovered enabled first-party plugins.",
            use_when='After installation or manifest changes. Default mode=scan discovers metadata only. Explicit mode=scan_and_attach_new_first_party also attaches newly discovered enabled first-party plugins. Inspect scan_errors and attach_errors separately; neither mode reloads existing code or enables disabled plugins.',
            do_not_use_when="Reloading or restarting one specific plugin (use manage_plugin operation=reload).",
            failure_next_steps="If scan_errors occur, check plugin manifest files. Previous plugin generation is preserved on error.",
        ), InputModel=PluginRescanInput, aliases=("rescan_plugins",), execution=INDIRECT_CONTROL)
    def rescan(self, call: IntrospectionCall) -> IntrospectionResult:
        if call.args.get("mode", "scan") == "scan_and_attach_new_first_party":
            return self.rescan_and_attach_new_first_party(call)
        result = self.host.rescan()
        return _LifecycleResult(
            status=(
                RuntimeStatus.ERROR
                if result.get("scan_errors")
                else RuntimeStatus.OK
            ),
            text="plugin rescan result",
            structured=result,
            effect_receipt=(EffectReceipt(outcome=EffectOutcome.APPLIED) if result.get("logical_unload_committed") else None),
            recovery_hint=str(result.get("recovery_hint") or ""),
            llm_text=render_titled_structured_for_llm("Plugin rescan result", result),
        )

    def rescan_and_attach_new_first_party(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        result = self.host.rescan_and_attach_new_first_party()
        status = (
            RuntimeStatus.ERROR
            if result.get("scan_errors") or result.get("attach_errors")
            else RuntimeStatus.OK
        )
        return _LifecycleResult(
            status=status,
            text="plugin rescan and attach result",
            structured=result,
            effect_receipt=(EffectReceipt(outcome=EffectOutcome.APPLIED) if result.get("logical_unload_committed") else None),
            recovery_hint=str(result.get("recovery_hint") or ""),
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
