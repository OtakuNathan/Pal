from __future__ import annotations

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
from pal.foundation.sidecar import python_subprocess_env
from pal.mcp.compiler import McpCompiledProjection, McpCompiler
from pal.mcp.ipc import McpManagerClient, McpManagerRpcError, mcp_log_path
from pal.mcp.model import McpDiscoverySnapshot
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    OPERATION_NAMESPACE,
    BoundCapabilityAction,
    IntrospectionCall,
    IntrospectionResult,
    MountedSubtreeHandle,
    RuntimeStatus,
    capability_action,
    capability_node,
)
from pal.shared.result_rendering import render_titled_structured_for_llm
from pal.skill.contracts import SkillDescriptor


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

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="module", action_name="show", description="Show MCP manager status")
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = self._status_payload()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="mcp manager status",
            structured=payload,
            llm_text=render_titled_structured_for_llm("MCP manager status", payload),
        )

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="mcp_server", action_name="list", description="List configured MCP servers")
    def list_servers(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        result = self._request_or_error("list_servers")
        return _introspection_from_rpc("MCP servers", result)

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="mcp_server",
        action_name="read",
        description="Read one MCP server metadata and discovery snapshot",
        args_schema={"type": "object", "properties": {"server_id": {"type": "string"}}, "required": ["server_id"]},
    )
    def read_server(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self._request_or_error("read_server", {"server_id": str(call.args.get("server_id") or "")})
        return _introspection_from_rpc("MCP server", result)

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="management", action_name="attach", description="Attach MCP manager")
    def attach(self, call: IntrospectionCall | None = None) -> IntrospectionResult:
        _ = call
        try:
            self._ensure_manager_started()
            self.client.rescan_sync()
            self._refresh_projection()
            self.last_error = ""
        except Exception as exc:
            self.last_error = f"{exc.__class__.__name__}: {exc}"
            raise
        payload = self._status_payload()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="mcp manager attached",
            structured=payload,
            llm_text=render_titled_structured_for_llm("MCP manager attached", payload),
        )

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="management", action_name="detach", description="Detach MCP manager")
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

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="management", action_name="rescan", description="Rescan MCP server configs and refresh projection")
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
        description="Attach one configured MCP server inside the manager",
        args_schema={"type": "object", "properties": {"server_id": {"type": "string"}}, "required": ["server_id"]},
    )
    def attach_server(self, call: IntrospectionCall) -> IntrospectionResult:
        try:
            self._ensure_manager_started()
            result = self.client.attach_server_sync(str(call.args.get("server_id") or ""))
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
        description="Detach one MCP server inside the manager",
        args_schema={"type": "object", "properties": {"server_id": {"type": "string"}}, "required": ["server_id"]},
    )
    def detach_server(self, call: IntrospectionCall) -> IntrospectionResult:
        try:
            result = self.client.detach_server_sync(str(call.args.get("server_id") or ""))
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
        description="Prepare an image artifact/path/url for external MCP tool arguments as URL, local path, or base64 data",
        args_schema={
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string"},
                "path": {"type": "string"},
                "url": {"type": "string"},
                "mode": {"type": "string", "enum": ["auto", "url", "path", "base64", "data_url"]},
            },
            "required": [],
        },
    )
    def image_prepare(self, call: CapabilityCall) -> CapabilityResult:
        try:
            payload = self._prepare_image_payload(call.args)
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
        mcp_log_path(self.runtime_root).parent.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "pal.mcp.manager_main", "--runtime-root", str(self.runtime_root)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
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
        with contextlib.suppress(Exception):
            self.client.shutdown_sync()
        process = self.process
        if process is not None:
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
            "log_path": str(mcp_log_path(self.runtime_root)),
            "last_error": self.last_error,
            "projected_capability_count": len(self.projection.mounted_subtree.descriptors) if self.projection else 0,
            "projected_skill_count": len(self.projection.skills) if self.projection else 0,
        }
        payload.update(dict(self.last_health or {}))
        return payload

    def _prepare_image_payload(self, args: dict[str, Any]) -> dict[str, Any]:
        mode = str(args.get("mode") or "auto").strip() or "auto"
        url = str(args.get("url") or "").strip()
        if url and mode in {"auto", "url"}:
            return {"kind": "url", "url": url}
        artifact_id = str(args.get("artifact_id") or "").strip()
        path_text = str(args.get("path") or "").strip()
        mime_type = ""
        file_name = ""
        source_url = ""
        if artifact_id:
            artifact_manager = self.core_context.port_registry.get("artifact:artifact")
            repository = getattr(artifact_manager, "repository", None)
            record = repository.get_record(artifact_id) if repository is not None else None
            if record is None:
                raise ValueError(f"unknown artifact_id: {artifact_id}")
            source_url = str((record.metadata.get("source_metadata") or {}).get("source_url") or record.metadata.get("source_url") or "").strip()
            if source_url and mode in {"auto", "url"}:
                return {"kind": "url", "url": source_url, "artifact_id": artifact_id, "mime_type": record.normalized_mime_type or record.original_mime_type}
            path_text = record.normalized_path or record.original_path
            mime_type = record.normalized_mime_type or record.original_mime_type
            file_name = record.file_name
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
            "kind": "base64" if mode != "data_url" else "data_url",
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


def _error_result(text: str, exc: Exception) -> IntrospectionResult:
    payload = {"error": str(exc), "error_type": exc.__class__.__name__}
    return IntrospectionResult(
        status=RuntimeStatus.ERROR,
        text=text,
        structured=payload,
        llm_text=render_titled_structured_for_llm(text, payload),
    )
