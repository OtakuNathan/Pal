from __future__ import annotations

from pal.execution.tool_semantics import (
    INDIRECT_CONTROL,
    INDIRECT_LOCAL_WRITE,
)

from pal.execution.generated_tool_models import (
    ChannelCapabilitiesChannelIntrospectionProviderAttachInput,
    ChannelCapabilitiesChannelIntrospectionProviderDetachInput,
    ChannelCapabilitiesChannelIntrospectionProviderDisableInput,
    ChannelCapabilitiesChannelIntrospectionProviderEnableInput,
    ChannelCapabilitiesChannelIntrospectionProviderReloadProviderInput,
    ChannelCapabilitiesChannelIntrospectionProviderRescanInput,
    ChannelCapabilitiesChannelIntrospectionProviderSendAttachmentInput,
    ChannelCapabilitiesChannelIntrospectionProviderSendAttachmentOutput,
    ChannelCapabilitiesChannelIntrospectionProviderSendMessageInput,
    ChannelCapabilitiesChannelIntrospectionProviderSendMessageOutput,
    ChannelCapabilitiesChannelIntrospectionProviderSetAuthMaterialInput,
)
from pal.execution.tool_semantics import INDIRECT_EXTERNAL_WRITE
from pal.execution.channel_attachment import ChannelSendAttachmentTool
from pal.execution.tool_facade import ToolGuidance

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pal.channel.models import ChannelEndpointModel
from pal.channel.contracts import ChannelDeliveryError
from pal.channel.provider_manager import (
    ChannelEndpointProviderManager,
    build_default_channel_provider_manager,
    is_recovery_socket_endpoint,
    recovery_socket_path,
)
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
    provider_id: str = ""


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
    provider_manager: ChannelEndpointProviderManager | None = None
    main_context: MainContext | None = None
    module_id: str = "channel"
    owner_id: str = "channel"

    def __post_init__(self) -> None:
        if self.provider_manager is None:
            self.provider_manager = build_default_channel_provider_manager(
                runtime=self.runtime,
                repository=self.repository,
                runtime_root=self.runtime_root or Path.cwd(),
            )

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
        guidance=ToolGuidance(
            purpose="List configured channel endpoints.",
            use_when="Need to discover available endpoint IDs, their channel kind, enabled/attached/paired status.",
            do_not_use_when="You already know the endpoint ID. Diagnosing one endpoint in depth (use channel_endpoint_inspect).",
            failure_next_steps="Read-only. If empty, no endpoints are configured — check channel provider configuration.",
        ),
        aliases=("channel_list",),
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
                provider_id=getattr(self._provider_for_target(target), "provider_id", ""),
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
        guidance=ToolGuidance(
            purpose="Send a local file attachment back to the channel that started the current turn.",
            use_when="The user asked for a generated file (image, document, code) to be sent back through the channel.",
            do_not_use_when="Sending plain text (use channel_send_message). Writing a local file (use write_file).",
            failure_next_steps="If file path is invalid, verify with read_file first. If delivery fails, the endpoint may be detached — check channel_list.",
        ),
        aliases=("send_channel_attachment",),
        InputModel=ChannelCapabilitiesChannelIntrospectionProviderSendAttachmentInput,
        OutputModel=ChannelCapabilitiesChannelIntrospectionProviderSendAttachmentOutput,
        execution=INDIRECT_EXTERNAL_WRITE,
    )
    async def send_attachment(self, call: IntrospectionCall) -> IntrospectionResult:
        execution_runtime = (
            self.main_context.execution_runtime
            if self.main_context is not None
            else None
        )
        return await ChannelSendAttachmentTool().ainvoke(
            dict(call.args),
            runtime=execution_runtime,
            turn_id=str(call.meta.get("turn_id") or "") or None,
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="channel",
        action_name="send_message",
        description="Send an ordinary text message through one configured channel endpoint.",
        aliases=("channel_send_message",),
        InputModel=ChannelCapabilitiesChannelIntrospectionProviderSendMessageInput,
        OutputModel=ChannelCapabilitiesChannelIntrospectionProviderSendMessageOutput,
        guidance=ToolGuidance(
            purpose="Send an ordinary text message through a configured channel endpoint.",
            use_when=(
                "Use when you need to initiate a message on an attached, enabled endpoint; "
                "obtain channel_id from channel_list."
            ),
            do_not_use_when=(
                "Do not use for the normal reply to the current turn, including a websocket peer turn: "
                "reply with the normal final response, or exactly [[peer_end]] when no peer reply is needed. "
                "Do not use for attachments, slash commands, channel management, or provider-specific "
                "target addressing."
            ),
            failure_next_steps=(
                "For not-found, detached, or disabled endpoints inspect channel_list and repair endpoint state. "
                "For an uncertain delivery failure, reconcile with the recipient before retrying."
            ),
        ),
        execution=INDIRECT_EXTERNAL_WRITE,
        search_text=(
            "channel send message active proactive ordinary text configured endpoint "
            "telegram websocket peer"
        ),
        examples=(
            {
                "channel_id": "telegram-main",
                "message": "The scheduled task has completed.",
            },
        ),
    )
    async def send_message(self, call: IntrospectionCall) -> IntrospectionResult:
        channel_id = str(call.args.get("channel_id") or "").strip()
        message = str(call.args.get("message") or "")
        if not channel_id:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="channel_id is required",
                structured={"reason": "channel_id_required"},
                llm_text="channel_id is required; use channel_list to choose an endpoint.",
            )
        if not message.strip():
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="message is required",
                structured={"channel_id": channel_id, "reason": "message_required"},
                llm_text="message must contain ordinary non-blank text.",
            )
        if message.lstrip().startswith("/"):
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="slash commands are not ordinary channel messages",
                structured={"channel_id": channel_id, "reason": "slash_command_not_allowed"},
                llm_text="channel_send_message accepts ordinary text, not slash commands.",
            )
        if self._is_current_peer_reply(
            turn_id=str(call.meta.get("turn_id") or ""),
            channel_id=channel_id,
        ):
            payload = {
                "channel_id": channel_id,
                "reason": "peer_reply_must_use_final",
            }
            return IntrospectionResult(
                status=RuntimeStatus.FORBIDDEN,
                text="reply to the current peer with this turn's final response",
                structured=payload,
                llm_text=(
                    "Do not call channel_send_message to reply to the peer that started "
                    "this turn. Use the normal final response, or output [[peer_end]] "
                    "exactly when no reply should be sent."
                ),
            )
        try:
            receipt = await self.runtime.send_message(channel_id, message)
        except ChannelDeliveryError as exc:
            reason = str(getattr(exc, "reason", "") or "delivery_failed")
            if reason == "channel_not_found":
                status = RuntimeStatus.NOT_FOUND
            elif reason == "active_send_unsupported":
                status = RuntimeStatus.UNSUPPORTED
            elif reason in {"channel_detached", "channel_disabled"}:
                status = RuntimeStatus.FORBIDDEN
            else:
                status = RuntimeStatus.ERROR
            payload = {
                "channel_id": channel_id,
                "reason": reason,
                "permanent": bool(exc.permanent),
            }
            return IntrospectionResult(
                status=status,
                text=str(exc),
                structured=payload,
                llm_text=render_titled_structured_for_llm("Channel message was not sent", payload),
            )
        payload = {
            "channel_id": receipt.endpoint_id,
            "message_id": receipt.message_id,
            "status": receipt.status,
        }
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="channel message accepted",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Channel message accepted", payload),
        )

    def _is_current_peer_reply(self, *, turn_id: str, channel_id: str) -> bool:
        if not turn_id or self.main_context is None:
            return False
        try:
            core = self.main_context.require_port("core:core")
            continuation = core.state.active_turns.get(turn_id)
            binding = getattr(continuation, "delivery_binding", None)
            endpoint = binding.endpoint
        except (AttributeError, KeyError, TypeError):
            return False
        return (
            str(endpoint.channel_kind or "") == "websocket_bridge"
            and str(endpoint.endpoint_id or "") == channel_id
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="enable",
        description="Enable a channel endpoint",
        guidance=ToolGuidance(
            purpose="Enable a channel endpoint so it accepts incoming messages.",
            use_when="An endpoint was disabled and needs to resume receiving messages.",
            do_not_use_when="The endpoint runtime is disconnected (use channel_attach). The endpoint is already enabled.",
            failure_next_steps="If endpoint not found, verify ID with channel_list. Recovery socket endpoints cannot be disabled.",
        ),
        InputModel=ChannelCapabilitiesChannelIntrospectionProviderEnableInput,
        aliases=("channel_enable",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def enable(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._set_enabled(call, enabled=True)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="disable",
        description="Disable a channel endpoint",
        guidance=ToolGuidance(
            purpose="Disable a channel endpoint so it stops accepting incoming messages.",
            use_when="Temporarily stopping an endpoint without removing its configuration.",
            do_not_use_when="Fully disconnecting the runtime (use channel_detach). Recovery socket endpoints are protected and cannot be disabled.",
            failure_next_steps="If endpoint not found, verify ID with channel_list. Recovery socket endpoints cannot be disabled.",
        ),
        InputModel=ChannelCapabilitiesChannelIntrospectionProviderDisableInput,
        aliases=("channel_disable",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def disable(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._set_enabled(call, enabled=False)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="attach",
        description="Attach a channel endpoint",
        guidance=ToolGuidance(
            purpose="Attach a channel endpoint — connect its runtime instance so it can send and receive.",
            use_when="Reconnecting a detached endpoint's runtime. After channel_provider_rescan discovered a new endpoint.",
            do_not_use_when="Just toggling message acceptance (use channel_enable). The endpoint is already attached.",
            failure_next_steps="If provider not found, run channel_provider_rescan first. If already attached, no-op.",
        ),
        InputModel=ChannelCapabilitiesChannelIntrospectionProviderAttachInput,
        aliases=("channel_attach",),
        execution=INDIRECT_CONTROL,
    )
    def attach(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._attach_endpoint_provider(str(call.args.get("target_id") or "").strip())

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="detach",
        description="Detach a channel endpoint",
        guidance=ToolGuidance(
            purpose="Detach a channel endpoint — disconnect its runtime instance without removing configuration.",
            use_when="Temporarily disconnecting an endpoint's runtime (e.g. maintenance, restart).",
            do_not_use_when="Just stopping message acceptance (use channel_disable — keeps runtime alive).",
            failure_next_steps="If endpoint not found, verify ID with channel_list. Detached endpoints can be re-attached with channel_attach.",
        ),
        InputModel=ChannelCapabilitiesChannelIntrospectionProviderDetachInput,
        aliases=("channel_detach",),
        execution=INDIRECT_CONTROL,
    )
    def detach(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._set_attached(call, attached=False)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="provider",
        action_name="rescan",
        description="Rescan channel providers and update the channel provider registry.",
        guidance=ToolGuidance(
            purpose="Rescan channel providers and update the channel provider registry.",
            use_when="New channel providers were installed or provider configuration changed. Discovering newly available endpoints.",
            do_not_use_when="Restarting one specific endpoint (use channel_reload_provider).",
            failure_next_steps="If scan_errors occur, previous provider generation is preserved. Check provider configuration and rescan again.",
        ),
        aliases=("channel_provider_rescan",),
        InputModel=ChannelCapabilitiesChannelIntrospectionProviderRescanInput,
        execution=INDIRECT_CONTROL,
    )
    def rescan_providers(self, call: IntrospectionCall) -> IntrospectionResult:
        attach_enabled = bool(call.args.get("attach_enabled_endpoints", False))
        payload = self._manager().rescan_providers(attach_enabled_endpoints=attach_enabled)
        payload["republished_capability_names"] = self._republish_capabilities()
        status = RuntimeStatus.ERROR if payload.get("scan_errors") else RuntimeStatus.OK
        text = "channel provider rescan failed; previous generation preserved" if status == RuntimeStatus.ERROR else "channel providers rescanned"
        return IntrospectionResult(
            status=status,
            text=text,
            structured=payload,
            llm_text=render_titled_structured_for_llm(text, payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="reload_provider",
        description="Restart one channel endpoint runtime instance through its provider. Use channel provider rescan to discover newly available providers.",
        guidance=ToolGuidance(
            purpose="Restart one channel endpoint runtime instance through its provider.",
            use_when="An endpoint runtime is stuck, misbehaving, or needs a fresh connection.",
            do_not_use_when="Provider configuration changed (use channel_provider_rescan to pick up new providers).",
            failure_next_steps="If provider not found, run channel_provider_rescan first. If restart fails, check channel_endpoint_health.",
        ),
        aliases=("channel_reload_provider",),
        InputModel=ChannelCapabilitiesChannelIntrospectionProviderReloadProviderInput,
        execution=INDIRECT_CONTROL,
    )
    def reload_provider(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._restart_endpoint(str(call.args.get("target_id") or "").strip())

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="endpoint",
        action_name="inspect",
        description="Inspect channel endpoint state",
        guidance=ToolGuidance(
            purpose="Inspect full state of one channel endpoint.",
            use_when="Need detailed status of a specific endpoint (enabled, attached, paired, provider info).",
            do_not_use_when="Just need a list of all endpoints (use channel_list). Checking auth (use channel_endpoint_auth_state).",
            failure_next_steps="If endpoint not found, verify ID with channel_list.",
        ),
        aliases=("channel_endpoint_inspect",),
    )
    def inspect_endpoint(self, call: IntrospectionCall) -> IntrospectionResult:
        target = self._require_target(call)
        if target is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="channel endpoint not found",
                llm_text="channel endpoint not found",
            )
        return self._manager().inspect_endpoint(target.endpoint_id)

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="endpoint",
        action_name="auth_state",
        description="Inspect endpoint authorization state",
        guidance=ToolGuidance(
            purpose="Inspect whether an endpoint is authenticated and authorized.",
            use_when="Diagnosing auth failures or checking if credentials are still valid.",
            do_not_use_when="Applying credentials (use channel_endpoint_set_auth_material). General endpoint state (use channel_endpoint_inspect).",
            failure_next_steps="If endpoint not found, verify ID with channel_list. If not authenticated, apply credentials with channel_endpoint_set_auth_material.",
        ),
        aliases=("channel_endpoint_auth_state",),
    )
    def auth_state(self, call: IntrospectionCall) -> IntrospectionResult:
        target = self._require_target(call)
        if target is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="channel endpoint not found",
                llm_text="channel endpoint not found",
            )
        return self._manager().inspect_auth_state(target.endpoint_id)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="endpoint",
        family="endpoint",
        action_name="set_auth_material",
        description="Apply endpoint authorization material without exposing secrets",
        guidance=ToolGuidance(
            purpose="Apply endpoint authorization material (tokens, credentials) without exposing secrets in output.",
            use_when="An endpoint needs credentials to authenticate (e.g. Telegram bot token, API key).",
            do_not_use_when="Reading current auth state (use channel_endpoint_auth_state).",
            failure_next_steps="If endpoint not found, verify ID with channel_list. If material format invalid, check provider documentation for required fields.",
        ),
        InputModel=ChannelCapabilitiesChannelIntrospectionProviderSetAuthMaterialInput,
        aliases=("channel_endpoint_set_auth_material",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def set_auth_material(self, call: IntrospectionCall) -> IntrospectionResult:
        target = self._require_target(call)
        if target is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="channel endpoint not found",
                llm_text="channel endpoint not found",
            )
        material = call.args.get("material")
        if not isinstance(material, dict):
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="material must be an object",
                llm_text="material must be an object",
            )
        return self._manager().set_auth_material(target.endpoint_id, dict(material))

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="endpoint",
        action_name="backlog",
        description="Inspect endpoint backlog state",
        guidance=ToolGuidance(
            purpose="Inspect undelivered message backlog for one endpoint.",
            use_when="Checking if messages are queued but not yet delivered (endpoint was detached or slow).",
            do_not_use_when="General endpoint health (use channel_endpoint_health). Listing endpoints (use channel_list).",
            failure_next_steps="If endpoint not found, verify ID with channel_list. Large backlog may indicate the endpoint needs re-attachment.",
        ),
        aliases=("channel_endpoint_backlog",),
    )
    def backlog(self, call: IntrospectionCall) -> IntrospectionResult:
        target = self._require_target(call)
        if target is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="channel endpoint not found",
                llm_text="channel endpoint not found",
            )
        return self._manager().inspect_backlog(target.endpoint_id)

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="endpoint",
        action_name="health",
        description="Inspect endpoint network and delivery health",
        guidance=ToolGuidance(
            purpose="Inspect network connectivity and delivery health for one endpoint.",
            use_when="Diagnosing message delivery failures or connection issues.",
            do_not_use_when="Checking auth (use channel_endpoint_auth_state). Checking message queue (use channel_endpoint_backlog).",
            failure_next_steps="If endpoint not found, verify ID with channel_list. If unhealthy, try channel_reload_provider to restart the runtime.",
        ),
        aliases=("channel_endpoint_health",),
    )
    def health(self, call: IntrospectionCall) -> IntrospectionResult:
        target = self._require_target(call)
        if target is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="channel endpoint not found",
                llm_text="channel endpoint not found",
            )
        return self._manager().inspect_health(target.endpoint_id)

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

    def _manager(self) -> ChannelEndpointProviderManager:
        assert self.provider_manager is not None
        return self.provider_manager

    def _provider_for_target(self, target: ChannelEndpointTarget):
        return self._manager().provider_for_endpoint_type(target.channel_kind)

    def _set_enabled(self, call: IntrospectionCall, *, enabled: bool) -> IntrospectionResult:
        endpoint_id = str(call.args.get("target_id") or "").strip()
        if not endpoint_id:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="target_id is required",
                llm_text="target_id is required",
            )
        endpoint = self.runtime.get_endpoint(endpoint_id)
        record = self.repository.get(endpoint_id)
        if not enabled and is_recovery_socket_endpoint(record, endpoint, self.runtime_root or Path.cwd()):
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="recovery socket endpoint cannot be disabled",
                structured={
                    "endpoint_id": endpoint_id,
                    "endpoint_type": "socket",
                    "channel_kind": "socket",
                    "binding_key": str(recovery_socket_path(self.runtime_root or Path.cwd())),
                    "enabled": True,
                    "reason": "recovery_socket_control_channel",
                },
                llm_text="recovery socket endpoint cannot be disabled",
            )
        if endpoint is None and record is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="channel endpoint not found",
                llm_text="channel endpoint not found",
            )
        previous_enabled = bool(endpoint.enabled) if endpoint is not None else None
        if endpoint is not None:
            if enabled:
                self.runtime.enable_endpoint(endpoint_id)
            else:
                self.runtime.disable_endpoint(endpoint_id)
        try:
            updated_record = self.repository.set_enabled(endpoint_id, enabled) if record is not None else None
            if record is not None and updated_record is None:
                raise RuntimeError(f"channel endpoint disappeared during state update: {endpoint_id}")
        except Exception as exc:
            if endpoint is not None and previous_enabled is not None:
                if previous_enabled:
                    self.runtime.enable_endpoint(endpoint_id)
                else:
                    self.runtime.disable_endpoint(endpoint_id)
            return IntrospectionResult(
                status=RuntimeStatus.ERROR,
                text=f"channel endpoint state update failed: {exc}",
                structured={"endpoint_id": endpoint_id, "enabled": previous_enabled},
                llm_text="Channel endpoint state was not changed because its durable state could not be committed.",
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
        if attached:
            return self._manager().attach_endpoint(endpoint_id)
        return self._manager().detach_endpoint(endpoint_id)

    def _attach_endpoint_provider(self, endpoint_id: str) -> IntrospectionResult:
        return self._set_endpoint_attached(endpoint_id, attached=True)

    def _restart_endpoint(self, endpoint_id: str) -> IntrospectionResult:
        if not endpoint_id:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="target_id is required",
                llm_text="target_id is required",
            )
        return self._manager().restart_endpoint(endpoint_id)

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

    def _republish_capabilities(self) -> list[str]:
        context = self.main_context
        if context is None:
            return []
        handle = context.module_registry.get(self.module_id)
        if handle is None:
            return []
        context.execution_runtime.unmount_subtree(handle)
        handle.mounted_subtree = None
        context.execution_runtime.hydrate_module_handle(handle)
        published = context.execution_runtime.mount_subtree(handle)
        handle.published_capabilities = published
        return published


def inspect_channel(provider: ChannelIntrospectionProvider) -> ChannelSnapshot:
    targets = provider.iter_endpoints()
    return ChannelSnapshot(
        endpoint_count=len(targets),
        attached_count=sum(1 for item in targets if item.attached),
        enabled_count=sum(1 for item in targets if item.enabled),
    )

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
    from pal.channel.ingress import ChannelIngressCompiler

    runtime.ingress_compiler = ChannelIngressCompiler(
        artifact_manager_provider=lambda: context.port_registry.get("artifact:artifact"),
    )
    repository = ChannelEndpointRepository()
    provider_manager = build_default_channel_provider_manager(
        runtime=runtime,
        repository=repository,
        runtime_root=runtime_root or Path.cwd(),
    )
    provider = ChannelIntrospectionProvider(
        runtime=runtime,
        repository=repository,
        runtime_root=runtime_root,
        provider_manager=provider_manager,
        main_context=context,
    )
    if runtime_root is not None:
        provider_manager.hydrate_all()
    source = ChannelEventSource(runtime=runtime)
    handle = ModuleHandle(
        module_id="channel",
        tier=MODULE_TIER_CORE_FOUNDATION,
        detachable=False,
        introspection_provider=provider,
        event_sources=[source],
        ports={
            "channel": runtime,
            "provider_manager": provider_manager,
        },
    )
    context.register_module(handle)
    context.port_registry["agent_io:output"] = runtime
    context.event_source_registry.attach("channel", source)
    typing_sub = TypingSubscriber(runtime)
    context.turn_event_bus.subscribe(TURN_START, typing_sub)
    context.turn_event_bus.subscribe(TURN_END, typing_sub)
    context.lifecycle_owner_registry.register_owner(provider)
    return handle
