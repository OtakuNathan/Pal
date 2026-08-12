from __future__ import annotations

from pal.execution.tool_semantics import (
    INDIRECT_CONTROL,
    INDIRECT_UNSAFE_LOCAL_WRITE,
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
import subprocess
import sys
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
    process: subprocess.Popen | None = None
    projection: McpCompiledProjection | None = None
    last_error: str = ""
    last_health: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.client = McpManagerClient(runtime_root=self.runtime_root)

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="module", action_name="show",
        guidance=ToolGuidance(
            purpose="Show MCP manager status.",
            use_when="Diagnosing MCP system health — manager process, projection, server count.",
            do_not_use_when="Listing servers (use mcp_server_list). Checking one server (use mcp_server_read).",
            failure_next_steps="Read-only. If last_error is set, the manager sidecar failed to start — check mcp_attach.",
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

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="management", action_name="attach",
        guidance=ToolGuidance(
            purpose="Attach MCP manager — start sidecar and discover servers.",
            use_when="Reconnecting a detached MCP manager or after config changes.",
            do_not_use_when="Attaching one server (use mcp_server_attach). The manager is already attached.",
            failure_next_steps="If sidecar fails to start, check MCP config and binary availability.",
        ), aliases=("mcp_attach",), execution=INDIRECT_CONTROL)
    def attach(self, call: IntrospectionCall | None = None) -> IntrospectionResult:
        _ = call
        try:
            self._ensure_manager_started()
            McpManagerClient(
                runtime_root=self.runtime_root,
                request_timeout_seconds=_STARTUP_RESCAN_TIMEOUT_SECONDS,
            ).rescan_sync()
            self._refresh_projection()
            self.last_error = ""
        except Exception as exc:
            self.last_error = f"{exc.__class__.__name__}: {exc}"
            self.projection = None
            self.last_health = {"healthy": False, "startup_error": self.last_error}
        payload = self._status_payload()
        text = "mcp manager attached" if not self.last_error else "mcp manager attached without sidecar"
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text=text,
            structured=payload,
            llm_text=render_titled_structured_for_llm(text, payload),
        )

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="management", action_name="detach",
        guidance=ToolGuidance(
            purpose="Detach MCP manager — stop sidecar and withdraw all MCP capabilities.",
            use_when="Temporarily stopping all MCP server connections.",
            do_not_use_when="Detaching one server (use mcp_server_detach).",
            failure_next_steps="Re-attach with mcp_attach when ready.",
        ), aliases=("mcp_detach",), execution=INDIRECT_CONTROL)
    def detach(self, call: IntrospectionCall | None = None) -> IntrospectionResult:
        _ = call
        self._stop_manager()
        self.projection = None
        self._refresh_module_capabilities()
        payload = self._status_payload()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="mcp manager detached",
            structured=payload,
            llm_text=render_titled_structured_for_llm("MCP manager detached", payload),
        )

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="management", action_name="rescan",
        guidance=ToolGuidance(
            purpose="Rescan MCP server configs and refresh the tool projection.",
            use_when="After adding or modifying MCP server configuration files.",
            do_not_use_when="Restarting the manager (use mcp_detach then mcp_attach). Attaching one server (use mcp_server_attach).",
            failure_next_steps="If rescan fails, check MCP config file syntax.",
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
            do_not_use_when="Attaching the whole manager (use mcp_attach). Detaching a server (use mcp_server_detach).",
            failure_next_steps="If server not found, verify with mcp_server_list or run mcp_rescan.",
        ),
        InputModel=McpPluginMcpManagerPluginProviderAttachInput,
        aliases=("mcp_server_attach",),
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
            do_not_use_when="Detaching the whole manager (use mcp_detach). Attaching a server (use mcp_server_attach).",
            failure_next_steps="If server not found, verify with mcp_server_list.",
        ),
        InputModel=McpPluginMcpManagerPluginProviderDetachInput,
        aliases=("mcp_server_detach",),
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
            purpose="Prepare an image artifact/path/url for external MCP tool arguments.",
            use_when="An MCP tool requires image input and you have an artifact, local path, or URL.",
            do_not_use_when="Reading artifact text content (use read_artifact). The MCP tool accepts URLs directly.",
            failure_next_steps="If an artifact is not found, verify it with list_artifacts. For an invalid local path, use run_shell with a bounded existence/type check; read_file cannot validate binary images. If preparation may have succeeded, inspect the returned artifact/path before retrying to avoid duplicate materialization.",
        ),
        InputModel=McpPluginMcpManagerPluginProviderImagePrepareInput,
        aliases=("mcp_image_prepare",),
        execution=INDIRECT_UNSAFE_LOCAL_WRITE,
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
        if self.process is not None and self.process.poll() is None:
            try:
                self.last_health = self.client.health_sync()
                return
            except Exception:
                self._stop_process_only()
        self._cleanup_stale_socket()
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "pal.mcp.manager_main", "--runtime-root", str(self.runtime_root)],
            env=python_subprocess_env(),
        )
        for _ in range(150):
            if self.process.poll() is not None:
                raise RuntimeError("mcp manager exited during startup")
            try:
                self.last_health = self.client.health_sync()
                return
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("mcp manager failed to start")

    def _stop_manager(self) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            with contextlib.suppress(Exception):
                self.client.shutdown_sync()
            with contextlib.suppress(Exception):
                process.wait(timeout=2.0)
        self._stop_process_only()
        self.last_health = {}

    def _cleanup_stale_socket(self) -> None:
        socket_path = self.client.socket_path
        if not socket_path.exists():
            return
        try:
            self.client.health_sync()
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                socket_path.unlink()

    def _stop_process_only(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=1.0)
        except Exception:
            with contextlib.suppress(Exception):
                process.kill()
        self.process = None

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
        running = self.process is not None and self.process.poll() is None
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
            supports_lifecycle_capabilities=True,
            ports={"mcp": provider},
            shutdown_sync=provider._stop_manager,
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
