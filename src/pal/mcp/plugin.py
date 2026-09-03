from __future__ import annotations

from pal.execution.tool_semantics import (
    INDIRECT_CONTROL,
    INDIRECT_LOCAL_WRITE,
)
from pal.execution.tool_facade import ToolGuidance

from pal.execution.generated_tool_models import (
    McpPluginMcpManagerPluginProviderAttachInput,
    McpPluginMcpManagerPluginProviderDetachInput,
    McpPluginMcpManagerPluginProviderImagePrepareInput,
    McpPluginMcpManagerPluginProviderReadInput,
)

import base64
import contextlib
import mimetypes
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
from pal.execution.contracts import CapabilityCall, CapabilityResult
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
from pal.skill.contracts import SkillDescriptor


_STARTUP_RESCAN_TIMEOUT_SECONDS = 30.0
_MANAGER_RETIRE_TIMEOUT_SECONDS = 5.0


class _McpManagerInvoker:
    def __init__(self, client: McpManagerClient) -> None:
        self.client = client

    def call_tool(self, server_id: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.client.call_tool_sync(server_id, tool_name, arguments)

    def render_prompt(self, server_id: str, prompt_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.client.render_prompt_sync(server_id, prompt_name, arguments)


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
            purpose="Show MCP manager status.",
            use_when="Diagnosing MCP system health — manager process, projection, server count.",
            do_not_use_when="Listing servers (use mcp_server_list). Checking one server (use mcp_server_read).",
            failure_next_steps="If the manager failed, reload the MCP plugin; mcp_attach only targets one configured server.",
        ), aliases=("mcp_show",))
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = self._status_payload()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="mcp manager status",
            structured=payload,
            llm_text=render_titled_structured_for_llm("MCP manager status", payload),
        )

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="mcp_server", action_name="list",
        guidance=ToolGuidance(
            purpose="List configured MCP servers.",
            use_when="Discovering which MCP servers are configured and their attach status.",
            do_not_use_when="Checking manager health (use mcp_show). Reading one server's details (use mcp_server_read).",
            failure_next_steps="If empty, no MCP servers are configured. Check MCP config files or run mcp_rescan.",
        ), aliases=("mcp_server_list",))
    def list_servers(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        result = _add_names(self._request_or_error("list_servers"), key="server_id")
        return _introspection_from_rpc("MCP servers", result)

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="mcp_server",
        action_name="read",
        guidance=ToolGuidance(
            purpose="Read one MCP server's metadata and tool discovery snapshot.",
            use_when="Inspecting what tools a specific MCP server exposes.",
            do_not_use_when="Listing all servers (use mcp_server_list). Manager health (use mcp_show).",
            failure_next_steps="If server not found, verify with mcp_server_list.",
        ),
        InputModel=McpPluginMcpManagerPluginProviderReadInput,
        aliases=("mcp_server_read",),
    )
    def read_server(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self._request_or_error("read_server", {"server_id": str(call.args.get("name") or "")})
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
            self.last_error = f"{exc.__class__.__name__}: {exc}"
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
            purpose="Rescan MCP server configs and refresh the tool projection.",
            use_when="After adding or modifying MCP server configuration files.",
            do_not_use_when="Restarting the manager (use plugin_attach with name='mcp'). Attaching one server (use mcp_attach).",
            failure_next_steps="Inspect mcp_show and mcp_server_list to reconcile current manager and server state. Correct config syntax or projection errors before retrying.",
        ), aliases=("mcp_rescan",), execution=INDIRECT_CONTROL)
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
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="server",
        action_name="attach",
        guidance=ToolGuidance(
            purpose="Attach one configured MCP server inside the manager.",
            use_when="Enabling a specific MCP server's tools without affecting others.",
            do_not_use_when="Attaching the whole MCP plugin (use plugin_attach). Detaching a server (use mcp_detach).",
            failure_next_steps="Inspect mcp_server_list to reconcile whether the server attached. If absent, verify its name and run mcp_rescan after config changes before retrying.",
        ),
        InputModel=McpPluginMcpManagerPluginProviderAttachInput,
        aliases=("mcp_attach",),
        execution=INDIRECT_CONTROL,
    )
    def attach_server(self, call: IntrospectionCall) -> IntrospectionResult:
        try:
            self._ensure_manager_started()
            result = self.client.attach_server_sync(str(call.args.get("name") or ""))
            self._refresh_projection()
            self._refresh_module_capabilities()
            return _introspection_from_rpc("MCP server attached", result)
        except Exception as exc:
            return _error_result("mcp server attach failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="server",
        action_name="detach",
        guidance=ToolGuidance(
            purpose="Detach one MCP server inside the manager.",
            use_when="Temporarily disabling one MCP server's tools.",
            do_not_use_when="Detaching the whole MCP plugin (use plugin_detach). Attaching a server (use mcp_attach).",
            failure_next_steps="Inspect mcp_server_list to reconcile whether the server detached. Retry only if it is still attached; verify the exact server name first.",
        ),
        InputModel=McpPluginMcpManagerPluginProviderDetachInput,
        aliases=("mcp_detach",),
        execution=INDIRECT_CONTROL,
    )
    def detach_server(self, call: IntrospectionCall) -> IntrospectionResult:
        try:
            result = self.client.detach_server_sync(str(call.args.get("name") or ""))
            self._refresh_projection()
            self._refresh_module_capabilities()
            return _introspection_from_rpc("MCP server detached", result)
        except Exception as exc:
            return _error_result("mcp server detach failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="mcp",
        action_name="image_prepare",
        guidance=ToolGuidance(
            purpose="Convert an image artifact or local file into the path, base64, or data-URL representation required by an external MCP tool.",
            use_when="An MCP tool requires an image representation that differs from the artifact, local path, or URL already available.",
            do_not_use_when="Reading artifact text content (use read_artifact). The MCP tool already accepts the available URL or path directly. The active model can inspect an inline image itself.",
            failure_next_steps="For an unknown artifact, recover its current artifact_id with list_artifacts. For an invalid local path, use run_shell with a bounded existence/type check; read_file cannot validate binary images. Correct the source or mode and retry; this tool is read-only.",
        ),
        InputModel=McpPluginMcpManagerPluginProviderImagePrepareInput,
        aliases=("mcp_image_prepare",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def image_prepare(self, call: CapabilityCall) -> CapabilityResult:
        try:
            payload = self._prepare_image_payload(
                call.args,
                turn_id=str(call.meta.get("turn_id") or "") or None,
            )
        except Exception as exc:
            return CapabilityResult(
                status=RuntimeStatus.ERROR,
                text=f"image prepare failed: {exc.__class__.__name__}",
                structured={"error": str(exc)},
                llm_text=f"image prepare failed: {exc.__class__.__name__}",
            )
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="image prepared for MCP tool argument",
            structured=payload,
            llm_text=render_titled_structured_for_llm("MCP image payload", payload),
        )

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
        self.projection = self.compiler.compile(module_id="mcp", snapshots=snapshots, invoker=_McpManagerInvoker(self.client))
        self.last_health = dict(payload)

    def _refresh_module_capabilities(self) -> None:
        if self.refresh_capabilities is not None:
            self.refresh_capabilities()

    def _request_or_error(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            self._ensure_manager_started()
            return self.client.request_sync(method, params)
        except Exception as exc:
            return {"status": RuntimeStatus.ERROR, "error": f"{exc.__class__.__name__}: {exc}", **self._status_payload()}

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
        payload.update(dict(self.last_health or {}))
        return payload

    def _prepare_image_payload(
        self,
        args: dict[str, Any],
        *,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        mode = str(args.get("mode") or "auto").strip() or "auto"
        url = str(args.get("url") or "").strip()
        if url and mode in {"auto", "url"}:
            return {"kind": "url", "url": url}
        artifact_id = str(args.get("artifact_id") or "").strip()
        path_text = str(args.get("path") or "").strip()
        mime_type = ""
        file_name = ""
        if artifact_id:
            artifact_manager = self.core_context.port_registry.get("artifact:artifact")
            info = getattr(artifact_manager, "info", None)
            if not callable(info):
                raise ValueError("artifact service unavailable")
            runtime = getattr(self.core_context, "execution_runtime", None)
            registry = getattr(runtime, "provider_registry", {})
            turn_io = registry.get("core:turn_io") if isinstance(registry, dict) else None
            scope_for_turn = getattr(turn_io, "artifact_scope_for_turn", None)
            scope_key = scope_for_turn(turn_id) if callable(scope_for_turn) else None
            if not scope_key:
                raise ValueError("artifact_scope_unavailable")
            artifact = dict(info(artifact_id, str(scope_key)).get("artifact") or {})
            local_file = dict((artifact.get("metadata") or {}).get("local_file") or {})
            path_text = str(local_file.get("preferred_path") or "")
            mime_type = str(
                local_file.get("preferred_mime_type")
                or artifact.get("mime_type")
                or ""
            )
            file_name = str(artifact.get("file_name") or "")
        if not path_text:
            raise ValueError("artifact_id, path, or url is required")
        path = Path(path_text).expanduser()
        if not path.is_file():
            raise ValueError(f"image file not found: {path}")
        path = path.resolve()
        detected_mime = mime_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if mode == "path":
            payload = {
                "kind": "path",
                "path": str(path),
                "mime_type": detected_mime,
                "file_name": file_name or path.name,
                "size_bytes": path.stat().st_size,
            }
            if artifact_id:
                payload["artifact_id"] = artifact_id
            return payload
        raw = path.read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        payload = {
            "kind": (
                "data_url"
                if mode == "data_url" or (artifact_id and mode in {"auto", "url"})
                else "base64"
            ),
            "base64": encoded,
            "mime_type": detected_mime,
            "file_name": file_name or path.name,
            "size_bytes": len(raw),
        }
        payload["data_url"] = f"data:{detected_mime};base64,{encoded}"
        if artifact_id:
            payload["artifact_id"] = artifact_id
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
    status = payload.get("status") or RuntimeStatus.OK
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


def _error_result(text: str, exc: Exception) -> IntrospectionResult:
    payload = {"error": str(exc), "error_type": exc.__class__.__name__}
    return IntrospectionResult(
        status=RuntimeStatus.ERROR,
        text=text,
        structured=payload,
        llm_text=render_titled_structured_for_llm(text, payload),
    )
