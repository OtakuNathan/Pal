from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pal.channel.channel_endpoint_queue_base import ChannelEndpointBase
from pal.channel.models import ChannelEndpointModel
from pal.channel.repository import ChannelEndpointRepository
from pal.channel.runtime import ChannelRuntime
from pal.channel.source import ChannelEventSource
from pal.core.lifecycle_owner import ModuleLifecycleOwnerResult, lifecycle_owner_not_found
from pal.core.turn_events import TURN_END, TURN_START, TurnEvent
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

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


@dataclass(frozen=True)
class ChannelSnapshot:
    endpoint_count: int
    attached_count: int
    enabled_count: int


@dataclass(frozen=True)
class ChannelEndpointTarget:
    endpoint_id: str
    channel_kind: str
    binding_key: str
    enabled: bool
    attached: bool
    model: ChannelEndpointModel | None = None
    runtime_endpoint: ChannelEndpointBase | None = None


@dataclass(frozen=True)
class ChannelEndpointListItem:
    endpoint_id: str
    channel_kind: str
    enabled: bool
    attached: bool
    paired: bool


@dataclass(frozen=True)
class ChannelEndpointSnapshot:
    endpoint_id: str
    channel_kind: str
    binding_key: str
    enabled: bool
    attached: bool
    paired: bool


@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="endpoint",
    kind="endpoint",
    source="builtin:channel",
    target_kind="endpoint",
    iterable_resolver="iter_endpoints",
    target_id_resolver="resolve_endpoint_id",
    target_label_resolver="resolve_endpoint_label",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="endpoint",
    kind="endpoint",
    source="builtin:channel",
    target_kind="endpoint",
    iterable_resolver="iter_endpoints",
    target_id_resolver="resolve_endpoint_id",
    target_label_resolver="resolve_endpoint_label",
)
@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:channel",
    target_kind="module",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:channel",
    target_kind="module",
)
@dataclass
class ChannelIntrospectionProvider:
    # Channel root owns only direct endpoint management. Endpoint-specific
    # auth/health/backlog capabilities live on the endpoint node itself, which
    # keeps the tree aligned with the "parent manages direct children only"
    # rule from the Capability Forest constitution.
    runtime: ChannelRuntime
    repository: ChannelEndpointRepository
    runtime_root: Path | None = None
    endpoint_factories: Any = None
    module_id: str = "channel"
    owner_id: str = "channel"

    def iter_endpoints(self) -> list[ChannelEndpointTarget]:
        targets: dict[str, ChannelEndpointTarget] = {}
        try:
            records = self.repository.list_all()
        except Exception:
            records = []
        for record in records:
            runtime_endpoint = self.runtime.get_endpoint(record.endpoint_id)
            attached = runtime_endpoint.attached if runtime_endpoint is not None else record.detached_at is None
            targets[record.endpoint_id] = ChannelEndpointTarget(
                endpoint_id=record.endpoint_id,
                channel_kind=record.channel_kind,
                binding_key=record.binding_key,
                enabled=record.enabled,
                attached=attached,
                model=record,
                runtime_endpoint=runtime_endpoint,
            )
        for endpoint in self.runtime.list_endpoints():
            endpoint_id = endpoint.endpoint.endpoint_id
            if endpoint_id in targets:
                continue
            targets[endpoint_id] = ChannelEndpointTarget(
                endpoint_id=endpoint.endpoint.endpoint_id,
                channel_kind=endpoint.endpoint.channel_kind,
                binding_key=endpoint.endpoint.binding_key,
                enabled=endpoint.enabled,
                attached=endpoint.attached,
                runtime_endpoint=endpoint,
            )
        return list(sorted(targets.values(), key=lambda item: (item.channel_kind, item.endpoint_id)))

    def resolve_endpoint_id(self, endpoint: ChannelEndpointTarget) -> str:
        return endpoint.endpoint_id

    def resolve_endpoint_label(self, endpoint: ChannelEndpointTarget) -> str:
        return endpoint.endpoint_id

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="list",
        description="List configured channel endpoints",
        aliases=("introspection_module_channel_observe", "channel_introspection_observe"),
    )
    def list_endpoints(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = [
            ChannelEndpointListItem(
                endpoint_id=target.endpoint_id,
                channel_kind=target.channel_kind,
                enabled=target.enabled,
                attached=target.attached,
                paired=target.runtime_endpoint.paired if target.runtime_endpoint is not None else False,
            ).__dict__
            for target in self.iter_endpoints()
        ]
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="channel endpoints",
            structured={"items": payload},
            llm_text=render_titled_structured_for_llm("Channel endpoints", {"items": payload}),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="channel",
        action_name="send_attachment",
        description="Send a local file attachment back to the channel that started the current turn.",
        aliases=("send_attachment", "channel_send_attachment"),
        args_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Local filesystem path to the file to send."},
                "caption": {"type": "string", "description": "Optional caption to send with the attachment."},
                "file_name": {"type": "string", "description": "Optional display filename."},
                "mime_type": {"type": "string", "description": "Optional MIME type hint."},
            },
            "required": ["path"],
        },
        result_schema={
            "type": "object",
            "properties": {
                "attachment_id": {"type": "string"},
                "path": {"type": "string"},
                "file_name": {"type": "string"},
                "mime_type": {"type": "string"},
                "reason": {"type": "string"},
            },
        },
    )
    def send_attachment(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        return IntrospectionResult(
            status=RuntimeStatus.INVALID,
            text="op_channel_send_attachment requires current async turn context",
            structured={"reason": "async_required"},
            llm_text="Use op_channel_send_attachment as a direct tool call during an active turn.",
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="enable",
        description="Enable a channel endpoint",
        args_schema={
            "type": "object",
            "properties": {"target_id": {"type": "string"}},
            "required": ["target_id"],
        },
    )
    def enable(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._set_enabled(call, enabled=True)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="disable",
        description="Disable a channel endpoint",
        args_schema={
            "type": "object",
            "properties": {"target_id": {"type": "string"}},
            "required": ["target_id"],
        },
    )
    def disable(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._set_enabled(call, enabled=False)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="attach",
        description="Attach a channel endpoint",
        args_schema={
            "type": "object",
            "properties": {"target_id": {"type": "string"}},
            "required": ["target_id"],
        },
    )
    def attach(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._attach_endpoint_provider(str(call.args.get("target_id") or "").strip())

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="detach",
        description="Detach a channel endpoint",
        args_schema={
            "type": "object",
            "properties": {"target_id": {"type": "string"}},
            "required": ["target_id"],
        },
    )
    def detach(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._set_attached(call, attached=False)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="reload_provider",
        description=(
            "Hot-reload the provider implementation for one channel endpoint. "
            "The channel bus stays mounted; only the endpoint/provider instance is rebuilt."
        ),
        aliases=("channel_reload_endpoint", "reload_channel_provider"),
        args_schema={
            "type": "object",
            "properties": {"target_id": {"type": "string"}},
            "required": ["target_id"],
        },
    )
    def reload_provider(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._reload_endpoint_provider(str(call.args.get("target_id") or "").strip())

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="endpoint",
        action_name="inspect",
        description="Inspect channel endpoint state",
        aliases=("introspection_endpoint_channel_observe",),
    )
    def inspect_endpoint(self, call: IntrospectionCall) -> IntrospectionResult:
        target = self._require_target(call)
        if target is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="channel endpoint not found",
                llm_text="channel endpoint not found",
            )
        snapshot = ChannelEndpointSnapshot(
            endpoint_id=target.endpoint_id,
            channel_kind=target.channel_kind,
            binding_key=target.binding_key,
            enabled=target.enabled,
            attached=target.attached,
            paired=target.runtime_endpoint.paired if target.runtime_endpoint is not None else False,
        )
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="channel endpoint snapshot",
            structured=snapshot.__dict__,
            llm_text=render_titled_structured_for_llm("Channel endpoint snapshot", snapshot.__dict__),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="endpoint",
        action_name="auth_state",
        description="Inspect endpoint authorization state",
    )
    def auth_state(self, call: IntrospectionCall) -> IntrospectionResult:
        target = self._require_target(call)
        if target is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="channel endpoint not found",
                llm_text="channel endpoint not found",
            )
        if target.runtime_endpoint is None:
            payload = {
                "endpoint_id": target.endpoint_id,
                "paired": False,
                "attached": target.attached,
                "authorized": False,
            }
            return IntrospectionResult(
                status=RuntimeStatus.OK,
                text="channel endpoint authorization state",
                structured=payload,
                llm_text=render_titled_structured_for_llm("Channel endpoint authorization state", payload),
            )
        auth_state = dict(target.runtime_endpoint.inspect_auth_state())
        auth_state.setdefault("endpoint_id", target.endpoint_id)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="channel endpoint authorization state",
            structured=auth_state,
            llm_text=render_titled_structured_for_llm("Channel endpoint authorization state", auth_state),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="endpoint",
        family="endpoint",
        action_name="set_auth_material",
        description="Apply endpoint authorization material without exposing secrets",
        args_schema={
            "type": "object",
            "properties": {
                "material": {"type": "object", "description": "Provider-specific auth credentials (key-value pairs)"},
            },
            "required": ["material"],
        },
    )
    def set_auth_material(self, call: IntrospectionCall) -> IntrospectionResult:
        target = self._require_target(call)
        if target is None or target.runtime_endpoint is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="channel endpoint runtime not found",
                llm_text="channel endpoint runtime not found",
            )
        material = call.args.get("material")
        if not isinstance(material, dict):
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="material must be an object",
                llm_text="material must be an object",
            )
        # Authorization material is write-only from the LLM-facing surface. We
        # can persist non-sensitive hints about what was supplied, but never
        # echo secrets/tokens back through introspection.
        auth_state = target.runtime_endpoint.apply_auth_material(dict(material))
        try:
            persisted_patch: dict[str, Any] = {
                "auth_keys": sorted(str(key) for key in material.keys()),
                "paired": bool(target.runtime_endpoint.paired),
            }
            if target.channel_kind == "telegram":
                bot_token = str(material.get("bot_token") or "").strip()
                if bot_token:
                    persisted_patch["bot_token"] = bot_token
            self.repository.merge_binding_metadata(
                target.endpoint_id,
                persisted_patch,
            )
        except Exception:
            pass
        sanitized = dict(auth_state)
        sanitized.pop("token", None)
        sanitized.pop("secret", None)
        sanitized.pop("bot_token", None)
        sanitized.setdefault("endpoint_id", target.endpoint_id)
        sanitized.setdefault("accepted_keys", sorted(str(key) for key in material.keys()))
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="channel endpoint auth material updated",
            structured=sanitized,
            llm_text=render_titled_structured_for_llm("Channel endpoint auth material updated", sanitized),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="endpoint",
        action_name="backlog",
        description="Inspect endpoint backlog state",
    )
    def backlog(self, call: IntrospectionCall) -> IntrospectionResult:
        target = self._require_target(call)
        if target is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="channel endpoint not found",
                llm_text="channel endpoint not found",
            )
        if target.runtime_endpoint is None:
            payload = {"endpoint_id": target.endpoint_id, "inbox_size": 0, "outbox_size": 0}
            return IntrospectionResult(
                status=RuntimeStatus.OK,
                text="channel endpoint backlog state",
                structured=payload,
                llm_text=render_titled_structured_for_llm("Channel endpoint backlog state", payload),
            )
        payload = dict(target.runtime_endpoint.inspect_backlog())
        payload.setdefault("endpoint_id", target.endpoint_id)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="channel endpoint backlog state",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Channel endpoint backlog state", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="endpoint",
        action_name="health",
        description="Inspect endpoint network and delivery health",
    )
    def health(self, call: IntrospectionCall) -> IntrospectionResult:
        target = self._require_target(call)
        if target is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="channel endpoint not found",
                llm_text="channel endpoint not found",
            )
        if target.runtime_endpoint is None:
            payload = {
                "endpoint_id": target.endpoint_id,
                "attached": target.attached,
                "enabled": target.enabled,
                "healthy": False,
                "reason": "runtime_endpoint_missing",
            }
            return IntrospectionResult(
                status=RuntimeStatus.OK,
                text="channel endpoint health",
                structured=payload,
                llm_text=render_titled_structured_for_llm("Channel endpoint health", payload),
            )
        payload = dict(target.runtime_endpoint.inspect_health())
        payload.setdefault("endpoint_id", target.endpoint_id)
        payload.setdefault("attached", target.attached)
        payload.setdefault("enabled", target.enabled)
        payload.pop("token", None)
        payload.pop("secret", None)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="channel endpoint health",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Channel endpoint health", payload),
        )

    # --- module lifecycle owner for endpoint providers ---

    def owns_module(self, module_id: str) -> bool:
        endpoint_id = self._endpoint_id_from_lifecycle_module(module_id)
        if not endpoint_id:
            return False
        return self.repository.get(endpoint_id) is not None or self.runtime.get_endpoint(endpoint_id) is not None

    def detach_module(self, module_id: str) -> ModuleLifecycleOwnerResult:
        endpoint_id = self._endpoint_id_from_lifecycle_module(module_id)
        if not endpoint_id:
            return lifecycle_owner_not_found(module_id, self.owner_id)
        result = self._set_endpoint_attached(endpoint_id, attached=False)
        return self._owner_result(module_id, endpoint_id, result, fresh_instance=False)

    def attach_module(self, module_id: str) -> ModuleLifecycleOwnerResult:
        endpoint_id = self._endpoint_id_from_lifecycle_module(module_id)
        if not endpoint_id:
            return lifecycle_owner_not_found(module_id, self.owner_id)
        result = self._attach_endpoint_provider(endpoint_id)
        return self._owner_result(module_id, endpoint_id, result, fresh_instance=result.status == RuntimeStatus.OK)

    def reload_module(self, module_id: str) -> ModuleLifecycleOwnerResult:
        return self.attach_module(module_id)

    def _require_target(self, call: IntrospectionCall) -> ChannelEndpointTarget | None:
        target = call.meta.get("resolved_target")
        return target if isinstance(target, ChannelEndpointTarget) else None

    def _set_enabled(self, call: IntrospectionCall, *, enabled: bool) -> IntrospectionResult:
        endpoint_id = str(call.args.get("target_id") or "").strip()
        if not endpoint_id:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="target_id is required",
                llm_text="target_id is required",
            )
        endpoint = self.runtime.get_endpoint(endpoint_id)
        if endpoint is not None:
            if enabled:
                self.runtime.enable_endpoint(endpoint_id)
            else:
                self.runtime.disable_endpoint(endpoint_id)
        try:
            record = self.repository.set_enabled(endpoint_id, enabled)
        except Exception:
            record = None
        if endpoint is None and record is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="channel endpoint not found",
                llm_text="channel endpoint not found",
            )
        payload = {"endpoint_id": endpoint_id, "enabled": enabled}
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text=f"channel endpoint {'enabled' if enabled else 'disabled'}",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Channel endpoint state updated", payload),
        )

    def _set_attached(self, call: IntrospectionCall, *, attached: bool) -> IntrospectionResult:
        endpoint_id = str(call.args.get("target_id") or "").strip()
        return self._set_endpoint_attached(endpoint_id, attached=attached)

    def _set_endpoint_attached(self, endpoint_id: str, *, attached: bool) -> IntrospectionResult:
        if not endpoint_id:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="target_id is required",
                llm_text="target_id is required",
            )
        endpoint = self.runtime.get_endpoint(endpoint_id)
        if endpoint is not None:
            if attached:
                self.runtime.attach_endpoint(endpoint_id)
            else:
                self.runtime.detach_endpoint(endpoint_id)
        try:
            record = self.repository.set_attached(endpoint_id, attached)
        except Exception:
            record = None
        if endpoint is None and record is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="channel endpoint not found",
                llm_text="channel endpoint not found",
            )
        payload = {"endpoint_id": endpoint_id, "attached": attached}
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text=f"channel endpoint {'attached' if attached else 'detached'}",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Channel endpoint lifecycle updated", payload),
        )

    def _attach_endpoint_provider(self, endpoint_id: str) -> IntrospectionResult:
        return self._reload_endpoint_provider(endpoint_id, attached=True)

    def _reload_endpoint_provider(self, endpoint_id: str, *, attached: bool | None = None) -> IntrospectionResult:
        if not endpoint_id:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="target_id is required",
                llm_text="target_id is required",
            )
        if attached is not None:
            record = self.repository.set_attached(endpoint_id, attached)
        else:
            record = self.repository.get(endpoint_id)
        if record is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="channel endpoint not found",
                llm_text="channel endpoint not found",
            )
        reload_modules = self._reload_modules_for_kind(str(record.channel_kind))
        old_endpoint = self.runtime.get_endpoint(endpoint_id)
        _drop_module_import_cache(reload_modules)
        self.endpoint_factories = _fresh_endpoint_factories()
        endpoint = self.endpoint_factories.create(record, runtime_root=self.runtime_root or Path.cwd())
        if endpoint is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="channel provider not found",
                structured={"endpoint_id": endpoint_id, "channel_kind": record.channel_kind},
                llm_text="channel provider not found",
            )
        if attached is not None:
            endpoint.attached = attached
        _preserve_runtime_endpoint_state(old_endpoint, endpoint)
        self.runtime.replace_endpoint(endpoint)
        payload = {
            "endpoint_id": endpoint_id,
            "channel_kind": record.channel_kind,
            "reload_modules": list(reload_modules),
            "attached": bool(endpoint.attached),
            "enabled": bool(endpoint.enabled),
        }
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="channel endpoint provider reloaded",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Channel endpoint provider reloaded", payload),
        )

    def _endpoint_id_from_lifecycle_module(self, module_id: str) -> str:
        prefix = "channel.endpoint:"
        text = str(module_id or "").strip()
        if not text.startswith(prefix):
            return ""
        return text[len(prefix) :].strip()

    def _owner_result(
        self,
        module_id: str,
        endpoint_id: str,
        result: IntrospectionResult,
        *,
        fresh_instance: bool,
    ) -> ModuleLifecycleOwnerResult:
        structured = dict(result.structured or {})
        reload_modules = structured.get("reload_modules")
        if isinstance(reload_modules, list | tuple):
            normalized_reload_modules = tuple(str(item) for item in reload_modules)
        else:
            normalized_reload_modules = ()
        return ModuleLifecycleOwnerResult(
            status=result.status,
            module_id=module_id,
            owner_id=self.owner_id,
            fresh_instance=fresh_instance and result.status == RuntimeStatus.OK,
            reload_modules=normalized_reload_modules,
            error=result.text if result.status != RuntimeStatus.OK else None,
            payload={"endpoint_id": endpoint_id, "channel_result": structured},
        )

    def _reload_modules_for_kind(self, channel_kind: str) -> tuple[str, ...]:
        factories = self.endpoint_factories
        reload_modules_for_kind = getattr(factories, "reload_modules_for_kind", None)
        if callable(reload_modules_for_kind):
            modules = tuple(str(item) for item in reload_modules_for_kind(channel_kind) if str(item).strip())
            if modules:
                return modules
        return _default_reload_modules_for_kind(channel_kind)


def inspect_channel(provider: ChannelIntrospectionProvider) -> ChannelSnapshot:
    targets = provider.iter_endpoints()
    return ChannelSnapshot(
        endpoint_count=len(targets),
        attached_count=sum(1 for item in targets if item.attached),
        enabled_count=sum(1 for item in targets if item.enabled),
    )


def _fresh_endpoint_factories():
    module = importlib.import_module("pal.channel.factory")
    return module.build_default_factory_registry()


def _default_reload_modules_for_kind(channel_kind: str) -> tuple[str, ...]:
    if channel_kind == "socket":
        return ("pal.channel.factory", "pal.channel.endpoints.socket_endpoint")
    if channel_kind == "telegram":
        return ("pal.channel.factory", "pal.channel.endpoints.telegram_endpoint")
    return ("pal.channel.factory",)


def _preserve_runtime_endpoint_state(old_endpoint: ChannelEndpointBase | None, new_endpoint: ChannelEndpointBase) -> None:
    if old_endpoint is None or old_endpoint is new_endpoint:
        return
    if getattr(old_endpoint, "paired", False):
        new_endpoint.paired = True
    pairing_metadata = dict(getattr(old_endpoint, "pairing_metadata", {}) or {})
    if pairing_metadata:
        new_endpoint.pairing_metadata.update(pairing_metadata)
    old_token = str(getattr(old_endpoint, "bot_token", "") or "").strip()
    new_token = str(getattr(new_endpoint, "bot_token", "") or "").strip()
    if old_token and hasattr(new_endpoint, "bot_token") and not new_token:
        setattr(new_endpoint, "bot_token", old_token)
    if hasattr(old_endpoint, "_authorized") and hasattr(new_endpoint, "_authorized"):
        setattr(
            new_endpoint,
            "_authorized",
            bool(getattr(old_endpoint, "_authorized", False)) or bool(getattr(new_endpoint, "_authorized", False)),
        )
    control_commands = list(getattr(old_endpoint, "_control_commands_manifest", []) or [])
    if control_commands and hasattr(new_endpoint, "_control_commands_manifest"):
        setattr(new_endpoint, "_control_commands_manifest", control_commands)


def _drop_module_import_cache(prefixes: tuple[str, ...]) -> None:
    clean_prefixes = tuple(dict.fromkeys(str(prefix).strip() for prefix in prefixes if str(prefix).strip()))
    if not clean_prefixes:
        return
    importlib.invalidate_caches()
    for module_name in list(sys.modules):
        if any(module_name == prefix or module_name.startswith(f"{prefix}.") for prefix in clean_prefixes):
            sys.modules.pop(module_name, None)


class TypingSubscriber:
    def __init__(self, runtime: ChannelRuntime) -> None:
        self._runtime = runtime

    def __call__(self, topic: str, event: TurnEvent) -> None:
        endpoint_id = str(event.get("endpoint_id") or "")
        reply_target = dict(event.get("reply_target") or {})
        if not endpoint_id:
            return
        if topic == TURN_START:
            self._runtime.queue_endpoint_status(
                endpoint_id, "typing_start", reply_target=reply_target,
            )
        elif topic == TURN_END:
            self._runtime.queue_endpoint_status(
                endpoint_id, "working_stop", reply_target=reply_target,
            )


def register_with_core(
    context: MainContext,
    runtime: ChannelRuntime,
    *,
    runtime_root: Path | None = None,
    endpoint_factories: Any = None,
) -> ModuleHandle:
    provider = ChannelIntrospectionProvider(
        runtime=runtime,
        repository=ChannelEndpointRepository(),
        runtime_root=runtime_root,
        endpoint_factories=endpoint_factories or _fresh_endpoint_factories(),
    )
    source = ChannelEventSource(runtime=runtime)
    handle = ModuleHandle(
        module_id="channel",
        tier=MODULE_TIER_CORE_FOUNDATION,
        detachable=False,
        introspection_provider=provider,
        event_sources=[source],
        ports={"channel": runtime},
    )
    context.register_module(handle)
    context.port_registry["agent_io:output"] = runtime
    context.event_source_registry.attach("channel", source)
    typing_sub = TypingSubscriber(runtime)
    context.turn_event_bus.subscribe(TURN_START, typing_sub)
    context.turn_event_bus.subscribe(TURN_END, typing_sub)
    context.lifecycle_owner_registry.register_owner(provider)
    return handle
