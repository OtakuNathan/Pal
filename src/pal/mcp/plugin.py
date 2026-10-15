from __future__ import annotations

from pal.execution.tool_semantics import (
    INDIRECT_CONTROL,
)
from pal.execution.tool_facade import ToolGuidance, ToolRejectedError
from pal.shared.diagnostics import exception_report

from pal.execution.generated_tool_models import (
    McpPluginMcpManagerPluginProviderReadInput,
    McpPluginMcpManagerPluginProviderManageServerInput,
)

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from pal.execution.contracts import CapabilityCall
from pal.mcp.artifact_inputs import resolve_artifact_input
from pal.foundation.service_logging import current_service_log_sink_description
from pal.foundation.sidecar import python_subprocess_env
from pal.mcp.compiler import McpCompiledProjection, McpCompiler
from pal.mcp.ipc import McpManagerClient
from pal.mcp.model import McpDiscoverySnapshot
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    OPERATION_NAMESPACE,
    IntrospectionCall,
    IntrospectionResult,
    MountedSubtreeHandle,
    RuntimeStatus,
    capability_action,
    capability_node,
)
from pal.shared.result_rendering import render_titled_structured_for_llm
from pal.shared.inspection import inspection_snapshot
from pal.skill.contracts import SkillDescriptor


_STARTUP_RESCAN_TIMEOUT_SECONDS = 30.0
_MANAGER_RETIRE_TIMEOUT_SECONDS = 5.0


class _McpManagerInvoker:
    def __init__(self, client: McpManagerClient, on_failure=None) -> None:
        self.client = client
        self.on_failure = on_failure

    def call_tool(self, server_id: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._invoke(self.client.call_tool_sync, server_id, tool_name, arguments)

    def render_prompt(self, server_id: str, prompt_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._invoke(self.client.render_prompt_sync, server_id, prompt_name, arguments)


    def _invoke(self, method, *args):
        try:
            return method(*args)
        except Exception as exc:
            # Refresh from the manager after it has withdrawn a quarantined server.
            # Failure to refresh must never hide the original invocation failure.
            if self.on_failure is not None:
                try:
                    self.on_failure()
                except Exception as refresh_exc:
                    exc.add_note("MCP projection refresh failed: " + str(refresh_exc))
            raise


@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:mcp",
    target_kind="module",
    path_module_id="mcp",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="mcp_server",
    kind="server",
    source="builtin:mcp",
    target_kind="mcp_server",
    path_module_id="mcp_server",
)
@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:mcp",
    target_kind="module",
    path_module_id="mcp",
)
@dataclass
class McpManagerPluginProvider:
    runtime_root: Path
    core_context: Any
    refresh_capabilities: Callable[[], None] | None = None
    client: McpManagerClient = field(init=False)
    compiler: McpCompiler = field(default_factory=McpCompiler)
    process: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _lifecycle_lock: threading.RLock = field(
        default_factory=threading.RLock, init=False, repr=False
    )
    projection: McpCompiledProjection | None = None
    last_error: str = ""
    last_health: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.client = McpManagerClient(runtime_root=self.runtime_root)

    def _process_status(self) -> tuple[int, int | None] | None:
        process = self.process
        if process is None:
            return None
        returncode = process.poll()
        if returncode is not None and self.process is process:
            self.process = None
        return int(process.pid), returncode

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="module", action_name="show",
        guidance=ToolGuidance(
            search_objects=('state', 'states', 'hub', 'manager', 'projection'),
            search_terms=('status', 'show', 'get', 'health', 'process', 'server', 'count', 'tools'),
            purpose="Inspect MCP manager process health, server counts and tool projection state.",
            use_when="Diagnosing MCP system health — manager process, projection, server count.",
            do_not_use_when="Listing servers (use list_mcp_servers). Checking one server (use read_mcp_server).",
            failure_next_steps="If the manager failed, reload the MCP plugin; manage_mcp_server only targets one configured server.",
        ), aliases=("inspect_mcp_state",))
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = inspection_snapshot(self._status_payload(), source="resident_mcp_host")
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="mcp manager status",
            structured=payload,
            llm_text=render_titled_structured_for_llm("MCP manager status", payload),
        )

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="mcp_server", action_name="list",
        guidance=ToolGuidance(
            search_objects=('server', 'servers'),
            purpose="List configured MCP servers.",
            use_when='Discovering which MCP servers are configured and their attach status. An empty list means no servers are currently discovered. After adding or correcting server configuration, use rescan_mcp_servers.',
            do_not_use_when="Checking manager health (use inspect_mcp_state). Reading one server's details (use read_mcp_server).",
            failure_next_steps="If empty, no MCP servers are currently discovered. Check MCP config files and use rescan_mcp_servers after configuration changes.",
        ), aliases=("list_mcp_servers",))
    def list_servers(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        result = _add_names(self._request_or_error("list_servers"), key="server_id")
        return _introspection_from_rpc("MCP servers", result)

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="mcp_server",
        action_name="read",
        guidance=ToolGuidance(
            search_objects=('server', 'servers'),
            search_terms=('inspect', 'show', 'info', 'metadata', 'status', 'health', 'error', 'quarantine', 'quarantined', 'connection', 'capabilities', 'tools', 'prompts', 'discovery'),
            purpose="Inspect one MCP server's observed connection, configuration, quarantine/errors and tool/prompt discovery snapshot.",
            use_when="Diagnosing one configured server or inspecting its discovered tools and prompts. Reads observed state without starting the manager or probing a new connection.",
            do_not_use_when="Listing all servers (use list_mcp_servers). Manager health (use inspect_mcp_state).",
            failure_next_steps="If server not found, verify with list_mcp_servers.",
        ),
        InputModel=McpPluginMcpManagerPluginProviderReadInput,
        aliases=("read_mcp_server",),
    )
    def read_server(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self._request_or_error("read_server", {"server_id": str(call.args.get("name") or "")}, start_manager=False)
        result = inspection_snapshot(result, source="mcp_manager")
        if result.get("status") == RuntimeStatus.ERROR:
            result["diagnostics"].update(partial=True, unavailable_sections={"server": result.get("error")})
        return _introspection_from_rpc("MCP server", result)

    def start_manager(self) -> None:
        try:
            self._ensure_manager_started()
            McpManagerClient(
                runtime_root=self.runtime_root,
                request_timeout_seconds=_STARTUP_RESCAN_TIMEOUT_SECONDS,
            ).rescan_sync()
            self._refresh_projection()
            self._refresh_module_capabilities()
            self.last_error = ""
        except Exception as exc:
            self.last_error = exception_report(exc)
            self.projection = None
            self.last_health = {"healthy": False, "startup_error": self.last_error}
            with contextlib.suppress(Exception):
                self._refresh_module_capabilities()
            raise

    def stop_manager(self) -> None:
        self._stop_manager()
        self.projection = None
        self._refresh_module_capabilities()

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="management", action_name="rescan",
        guidance=ToolGuidance(
            search_objects=('server', 'servers'),
            search_terms=('discover', 'scan', 'refresh',),
            purpose="Rescan MCP server configs and refresh the tool projection.",
            use_when="After adding or modifying MCP server configuration files.",
            do_not_use_when="Restarting the manager (use manage_plugin operation=reload with name='mcp'). Attaching one server (use manage_mcp_server operation=attach).",
            failure_next_steps="Use inspect_mcp_state and list_mcp_servers to reconcile current manager and server state. Correct config syntax or projection errors before retrying.",
        ), aliases=("rescan_mcp_servers",), execution=INDIRECT_CONTROL)
    def rescan(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        try:
            self._ensure_manager_started()
            result = self.client.rescan_sync()
            self._refresh_projection()
            self._refresh_module_capabilities()
            return _introspection_from_rpc("MCP rescan", result)
        except Exception as exc:
            return _error_result("mcp rescan failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE, scope="module", family="server", action_name="manage",
        guidance=ToolGuidance(
            search_objects=('server', 'servers'),
            search_terms=('connect', 'disconnect'),
            search_enum_fields=('operation',),
            purpose="Attach or detach one configured MCP server and refresh its published tools.",
            use_when="Use operation=attach to connect one server, or operation=detach to disconnect it. This controls the current connection without changing configured enabled state; rescan can reconnect enabled servers. After correcting a quarantined server and reconciling prior writes, attach explicitly; rescan does not retry quarantined servers.",
            do_not_use_when="Controlling the whole MCP plugin (use manage_plugin). Discovering changed configuration (use rescan_mcp_servers). Attach preserves an already attached server; it is not a forced restart.",
            failure_next_steps="Inspect list_mcp_servers and read_mcp_server to reconcile current state before retrying. Use the exact configured server name; rescan first after configuration changes.",
        ),
        InputModel=McpPluginMcpManagerPluginProviderManageServerInput,
        aliases=("manage_mcp_server",), execution=INDIRECT_CONTROL,
    )
    def manage_server(self, call: IntrospectionCall) -> IntrospectionResult:
        operation = call.args.get("operation")
        if operation == "attach":
            return self.attach_server(call)
        if operation == "detach":
            return self.detach_server(call)
        raise ToolRejectedError("operation must be attach or detach", error_code="invalid_operation")

    def attach_server(self, call: IntrospectionCall) -> IntrospectionResult:
        try:
            self._ensure_manager_started()
            result = self.client.attach_server_sync(str(call.args.get("name") or ""))
            self._refresh_projection()
            self._refresh_module_capabilities()
            return _introspection_from_rpc("MCP server attached" if result.get("attached") else "MCP server attach failed", result)
        except Exception as exc:
            return _error_result("mcp server attach failed", exc)

    def detach_server(self, call: IntrospectionCall) -> IntrospectionResult:
        try:
            result = self.client.detach_server_sync(str(call.args.get("name") or ""))
            self._refresh_projection()
            self._refresh_module_capabilities()
            return _introspection_from_rpc("MCP server detached", result)
        except Exception as exc:
            return _error_result("mcp server detach failed", exc)

    def build_mounted_subtree(self, *, module_id: str, lifecycle_scope: str, detachable: bool) -> MountedSubtreeHandle:
        _ = (module_id, lifecycle_scope, detachable)
        if self.projection is None:
            return MountedSubtreeHandle(module_id="mcp")
        return self.projection.mounted_subtree

    def declared_skills(self) -> tuple[SkillDescriptor, ...]:
        if self.projection is None:
            return ()
        return tuple(self.projection.skills)

    def _ensure_manager_started(self) -> None:
        with self._lifecycle_lock:
            self._ensure_manager_started_locked()

    def _ensure_manager_started_locked(self) -> None:
        status = self._process_status()
        if status is not None and status[1] is None:
            try:
                health = self._validate_health(self.client.health_sync())
                if self._pid_from_health(health) != status[0]:
                    raise RuntimeError("mcp manager endpoint is not owned by this plugin attachment")
                if bool(health.get("shutdown_requested")):
                    raise RuntimeError("mcp manager is shutting down")
                self.last_health = health
                return
            except Exception:
                self._stop_process_only()
        elif status is not None:
            self._stop_process_only()
        try:
            existing_health = self.client.health_sync()
        except Exception:
            existing_health = None
        if existing_health is not None:
            self._retire_existing_manager(existing_health)
        self._cleanup_stale_endpoint()
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            [sys.executable, "-m", "pal.mcp.manager_main", "--runtime-root", str(self.runtime_root)],
            env=python_subprocess_env(),
            start_new_session=os.name != "nt",
        )
        self.process = process
        for _ in range(150):
            current = self._process_status()
            if current is None or current[1] is not None:
                self._stop_process_only()
                raise RuntimeError("mcp manager exited during startup")
            try:
                health = self._validate_health(self.client.health_sync())
                if self._pid_from_health(health) != current[0]:
                    raise RuntimeError("mcp manager endpoint is not owned by this plugin attachment")
                if bool(health.get("shutdown_requested")):
                    raise RuntimeError("mcp manager is shutting down")
                self.last_health = health
                return
            except Exception:
                time.sleep(0.2)
        self._stop_process_only()
        raise RuntimeError("mcp manager failed to start")

    def _stop_manager(self) -> None:
        with self._lifecycle_lock:
            self._stop_manager_locked()

    def _stop_manager_locked(self) -> None:
        try:
            health = self.client.health_sync()
        except Exception:
            health = None
        try:
            if health is not None:
                self._retire_existing_manager(health)
        finally:
            self._stop_process_only()
        self._cleanup_stale_endpoint()
        self.last_health = {}

    @staticmethod
    def _validate_health(health: dict[str, Any]) -> dict[str, Any]:
        if not bool(health.get("ok")) or str(health.get("health_source") or "") != "mcp_manager":
            raise RuntimeError("mcp manager health check failed")
        if str(health.get("lifecycle_protocol") or "") != "plugin_raii.v1":
            raise RuntimeError("mcp manager lifecycle protocol is incompatible")
        return dict(health)

    @staticmethod
    def _pid_from_health(health: dict[str, Any]) -> int | None:
        try:
            pid = int(health.get("manager_pid") or 0)
        except (TypeError, ValueError):
            return None
        return pid if pid > 1 and pid != os.getpid() else None

    def _retire_existing_manager(self, health: dict[str, Any]) -> None:
        self._validate_health(health)
        with contextlib.suppress(Exception):
            self.client.shutdown_sync()
        deadline = time.monotonic() + _MANAGER_RETIRE_TIMEOUT_SECONDS
        while self._manager_is_responding() and time.monotonic() < deadline:
            time.sleep(0.05)
        if self._manager_is_responding():
            raise RuntimeError("existing mcp manager did not stop")
        self._cleanup_stale_endpoint()

    def _manager_is_responding(self) -> bool:
        try:
            health = self.client.health_sync()
        except Exception:
            return False
        return bool(health.get("ok")) and str(health.get("health_source") or "") == "mcp_manager"

    def _cleanup_stale_endpoint(self) -> None:
        for path in (self.client.socket_path, self.client.port_path):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    def _stop_process_only(self) -> None:
        with self._lifecycle_lock:
            process = self.process
            if process is None:
                return
            self.process = None
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    if os.name == "nt":
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    "MCP manager did not exit after its one-shot termination; replacement remains fenced"
                ) from exc

    def _refresh_projection(self) -> None:
        payload = self.client.snapshot_sync()
        snapshots = tuple(
            McpDiscoverySnapshot.from_dict(item)
            for item in list(payload.get("snapshots") or [])
            if isinstance(item, dict)
        )
        self.projection = self.compiler.compile(module_id="mcp", snapshots=snapshots, invoker=_McpManagerInvoker(self.client, self._refresh_after_call_failure),
            artifact_resolver=lambda artifact_id, representation, call: resolve_artifact_input(
                self.core_context, artifact_id, representation, call))
        self.last_health = dict(payload)

    def _refresh_after_call_failure(self) -> None:
        self._refresh_projection()
        self._refresh_module_capabilities()

    def _refresh_module_capabilities(self) -> None:
        if self.refresh_capabilities is not None:
            self.refresh_capabilities()

    def _request_or_error(self, method: str, params: dict[str, Any] | None = None, *, start_manager: bool = True) -> dict[str, Any]:
        try:
            if start_manager:
                self._ensure_manager_started()
            return self.client.request_sync(method, params)
        except Exception as exc:
            self.last_error = exception_report(exc)
            current = self._status_payload()
            current.pop("cached_snapshot", None)
            return {**current, **_error_payload(exc), "status": RuntimeStatus.ERROR, "operation": method}

    def _status_payload(self) -> dict[str, Any]:
        status = self._process_status()
        running = status is not None and status[1] is None
        payload = {
            "module_id": "mcp",
            "manager_running": running,
            "log_sink": current_service_log_sink_description(),
            "last_error": self.last_error,
            "projected_capability_count": len(self.projection.mounted_subtree.descriptors) if self.projection else 0,
            "projected_skill_count": len(self.projection.skills) if self.projection else 0,
        }
        payload["cached_snapshot"] = {key: value for key, value in dict(self.last_health or {}).items()
                                      if key in {"ok", "health_source", "manager_pid", "started_at", "last_rescan_at", "last_error",
                                                 "server_count", "attached_count", "tool_count", "quarantined_count", "shutdown_requested", "config_root"}}
        return payload


@dataclass
class McpManagerPluginBundle:
    runtime_root: Path
    plugin_id: str = "mcp"
    version: str = "0.1.0"

    def register_with_core(self, context) -> ModuleHandle:
        provider_ref: dict[str, McpManagerPluginProvider] = {}

        def refresh_capabilities() -> None:
            handle = context.module_registry.get("mcp")
            if handle is None or not handle.mounted:
                return
            if handle.mounted_subtree is not None and handle.mounted_subtree.mounted:
                context.execution_runtime.unmount_subtree(handle)
            skill = context.port_registry.get("skill:skill")
            unregister = getattr(skill, "unregister_declared_module", None)
            if callable(unregister):
                unregister("mcp")
            context.execution_runtime.hydrate_module_handle(handle)
            handle.published_capabilities = context.execution_runtime.mount_subtree(handle)
            register = getattr(skill, "register_declared_module", None)
            if callable(register):
                register(handle)

        provider = McpManagerPluginProvider(runtime_root=self.runtime_root, core_context=context, refresh_capabilities=refresh_capabilities)
        provider_ref["provider"] = provider
        handle = ModuleHandle(
            module_id="mcp",
            tier=MODULE_TIER_DETACHABLE,
            detachable=True,
            mounted=False,
            introspection_provider=provider,
            ports={"mcp": provider},
            shutdown_sync=provider.stop_manager,
        )
        context.register_module(handle)
        return handle


def build_mcp_plugin(*, runtime_root: Path) -> McpManagerPluginBundle:
    return McpManagerPluginBundle(runtime_root=runtime_root)


def _introspection_from_rpc(title: str, payload: dict[str, Any]) -> IntrospectionResult:
    status = payload.get("status") or (RuntimeStatus.ERROR if payload.get("ok") is False else RuntimeStatus.OK)
    return IntrospectionResult(
        status=status,
        text=title,
        structured=payload,
        llm_text=render_titled_structured_for_llm(title, payload),
    )


def _add_names(payload: dict[str, Any], *, key: str) -> dict[str, Any]:
    rendered = dict(payload)
    items = rendered.get("items")
    if isinstance(items, list):
        rendered["items"] = [
            {**dict(item), "name": str(item.get(key) or "")}
            if isinstance(item, dict)
            else item
            for item in items
        ]
    return rendered


def _error_payload(exc: Exception, *, error_code: str = "mcp_manager_failed") -> dict[str, Any]:
    return {"error": exception_report(exc), "error_type": exc.__class__.__name__, "error_code": error_code,
            **({"protocol_details": dict(exc.payload)} if getattr(exc, "payload", None) else {})}


def _error_result(text: str, exc: Exception) -> IntrospectionResult:
    payload = {**_error_payload(exc), "next_step": _mcp_recovery_hint(exc)}
    return IntrospectionResult(
        status=RuntimeStatus.ERROR,
        text=text,
        structured=payload,
        llm_text=render_titled_structured_for_llm(text, payload),
    )


def _mcp_recovery_hint(exc: Exception) -> str:
    return ("Use inspect_mcp_state for manager health and list_mcp_servers for server state; "
            "use read_mcp_server with the affected server name for details. "
            "Correct the reported cause. Reconcile any external write before retrying it.")
