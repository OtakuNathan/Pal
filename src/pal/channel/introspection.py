from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pal.channel.channel_endpoint_queue_base import ChannelEndpointBase
from pal.channel.models import ChannelEndpointModel
from pal.channel.repository import ChannelEndpointRepository
from pal.channel.runtime import ChannelRuntime
from pal.channel.source import ChannelEventSource
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
    module_id: str = "channel"

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
        return self._set_attached(call, attached=True)

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
                endpoint.enable()
            else:
                endpoint.disable()
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
        if not endpoint_id:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="target_id is required",
                llm_text="target_id is required",
            )
        endpoint = self.runtime.get_endpoint(endpoint_id)
        if endpoint is not None:
            if attached:
                endpoint.attach()
            else:
                endpoint.detach()
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


def inspect_channel(provider: ChannelIntrospectionProvider) -> ChannelSnapshot:
    targets = provider.iter_endpoints()
    return ChannelSnapshot(
        endpoint_count=len(targets),
        attached_count=sum(1 for item in targets if item.attached),
        enabled_count=sum(1 for item in targets if item.enabled),
    )


def register_with_core(context: MainContext, runtime: ChannelRuntime) -> ModuleHandle:
    provider = ChannelIntrospectionProvider(runtime=runtime, repository=ChannelEndpointRepository())
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
    context.event_source_registry.attach("channel", source)
    return handle
