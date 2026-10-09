from __future__ import annotations

from pal.foundation.diagnostics import exception_report

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
    ChannelCapabilitiesChannelIntrospectionProviderRestartEndpointInput,
    ChannelCapabilitiesChannelIntrospectionProviderRescanInput,
    ChannelCapabilitiesChannelIntrospectionProviderSendAttachmentInput,
    ChannelCapabilitiesChannelIntrospectionProviderSendAttachmentOutput,
    ChannelCapabilitiesChannelIntrospectionProviderSendMessageInput,
    ChannelCapabilitiesChannelIntrospectionProviderSendMessageOutput,
    ChannelCapabilitiesChannelIntrospectionProviderSetAuthMaterialInput,
)
from pal.execution.tool_semantics import INDIRECT_EXTERNAL_WRITE
from pal.execution.channel_attachment import ChannelSendAttachmentTool
from pal.execution.contracts import CapabilityResult
from pal.execution.tool_facade import EffectOutcome, EffectReceipt, ToolGuidance

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pal.channel.models import ChannelEndpointModel
from pal.channel.contracts import ChannelDeliveryError
from pal.channel.provider_manager import (
    ChannelEndpointProviderManager,
    _precondition_failure,
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
    state_error: str = ""


@dataclass(frozen=True)
class ChannelEndpointListItem:
    name: str
    endpoint_id: str
    channel_kind: str
    enabled: bool
    attached: bool
    paired: bool
    provider_id: str = ""
    default_destination_available: bool = False


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
        self.runtime.on_hub_visibility_changed = self._republish_capabilities

    def iter_endpoints(self) -> list[ChannelEndpointTarget]:
        return self._targets_from_hubs(published_only=True)

    def iter_internal_endpoints(self) -> list[ChannelEndpointTarget]:
        """Management topology; unlike capability hydration, includes detached hubs."""
        return self._targets_from_hubs(published_only=False)

    def _targets_from_hubs(self, *, published_only: bool) -> list[ChannelEndpointTarget]:
        targets: list[ChannelEndpointTarget] = []
        for hub in self.runtime.list_endpoint_hubs(published_only=published_only):
            state_error = ""
            try:
                record = self.repository.get(hub.endpoint_id)
            except Exception as exc:
                record = None
                state_error = exception_report(exc)
            runtime_endpoint = self.runtime.get_endpoint(hub.endpoint_id)
            targets.append(
                ChannelEndpointTarget(
                    endpoint_id=hub.endpoint_id,
                    channel_kind=(
                        record.channel_kind if record is not None else hub.channel_kind
                    ),
                    binding_key=(
                        record.binding_key if record is not None else hub.binding_key
                    ),
                    enabled=(
                        bool(runtime_endpoint.enabled)
                        if runtime_endpoint is not None
                        else bool(record.enabled) if record is not None else False
                    ),
                    attached=runtime_endpoint is not None and bool(runtime_endpoint.attached),
                    model=record,
                    runtime_endpoint=runtime_endpoint,
                    state_error=state_error,
                )
            )
        return targets

    def resolve_endpoint_id(self, endpoint: ChannelEndpointTarget) -> str:
        return endpoint.endpoint_id

    def resolve_endpoint_label(self, endpoint: ChannelEndpointTarget) -> str:
        return endpoint.endpoint_id

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="list",
        guidance=ToolGuidance(
            search_objects=('endpoint', 'endpoints'),
            purpose="List configured channel endpoints and their usable names.",
            use_when='Need to discover available endpoint names, their channel kind, enabled/attached/paired status. An empty list means no endpoints are configured or discovered; inspect the channel provider configuration.',
            do_not_use_when="You already know the endpoint name. If endpoints exist, use inspect_channel_endpoint for an in-depth diagnosis; endpoint-specific tools are available only while endpoints exist.",
            failure_next_steps="Read-only. If empty, no endpoints are configured — check channel provider configuration.",
        ),
        aliases=("list_channel_endpoints",),
    )
    def list_endpoints(self, call: IntrospectionCall) -> IntrospectionResult | CapabilityResult:
        _ = call
        targets = self.iter_endpoints()
        payload = [
            ChannelEndpointListItem(
                name=target.endpoint_id,
                endpoint_id=target.endpoint_id,
                channel_kind=target.channel_kind,
                enabled=target.enabled,
                attached=target.attached,
                paired=target.runtime_endpoint.paired if target.runtime_endpoint is not None else False,
                provider_id=getattr(self._provider_for_target(target), "provider_id", ""),
                default_destination_available=bool(
                    target.runtime_endpoint is not None
                    and target.runtime_endpoint.derive_default_reply_target()
                ),
            ).__dict__
            for target in targets
        ]
        errors = {target.endpoint_id: target.state_error for target in targets if target.state_error}
        if errors:
            details = {"items": payload, "state_errors": errors, "error_code": "channel_state_read_failed"}
            return CapabilityResult(status=RuntimeStatus.ERROR, text="channel endpoint inventory is incomplete",
                structured=details, llm_text=render_titled_structured_for_llm(
                    "Channel runtime endpoints found, but durable state could not be read", details),
                effect_receipt=EffectReceipt(outcome=EffectOutcome.NONE))
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
        guidance=ToolGuidance(
            search_objects=('attachment', 'attachments', 'file', 'files'),
            purpose="Send a local file attachment back to the channel that started the current turn.",
            use_when="The user asked for a generated file (image, document, code) to be sent back through the channel.",
            do_not_use_when="For an ordinary text reply to the current turn, respond normally without a sending tool. Use send_channel_message only for a separate initiated text delivery. Writing a local file (use write_file).",
            failure_next_steps="If the local path is invalid, use run_shell with a bounded existence/type check; read_file is only for UTF-8 text. If the endpoint is unavailable, inspect list_channel_endpoints. If delivery may have been accepted, reconcile with the recipient before retrying so the attachment is not sent twice.",
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
        aliases=("send_channel_message",),
        InputModel=ChannelCapabilitiesChannelIntrospectionProviderSendMessageInput,
        OutputModel=ChannelCapabilitiesChannelIntrospectionProviderSendMessageOutput,
        guidance=ToolGuidance(
            search_objects=('message', 'messages'),
            purpose="Send an ordinary text message through a configured channel endpoint.",
            use_when=(
                "Use when you need to initiate a message on an attached, enabled endpoint; "
                "obtain the endpoint name from list_channel_endpoints."
            ),
            do_not_use_when=(
                "Do not use for the normal reply to the current turn, including a websocket peer turn: "
                "reply with the normal final response, or exactly [[peer_end]] when no peer reply is needed. "
                "Do not use for attachments, slash commands, channel management, or provider-specific "
                "target addressing."
            ),
            failure_next_steps=(
                "For not-found, detached, or disabled endpoints inspect list_channel_endpoints and repair endpoint state. "
                "For an uncertain delivery failure, reconcile with the recipient before retrying."
            ),
        ),
        execution=INDIRECT_EXTERNAL_WRITE,
        metadata={"tags": ("active", "proactive", "telegram", "websocket", "peer")},
        examples=(
            {
                "name": "telegram-main",
                "message": "The scheduled task has completed.",
            },
        ),
    )
    async def send_message(self, call: IntrospectionCall) -> CapabilityResult:
        channel_id = str(call.args.get("name") or "").strip()
        message = str(call.args.get("message") or "")
        if not channel_id:
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                effect_receipt=EffectReceipt(outcome=EffectOutcome.NOT_STARTED),
                text="name is required",
                structured={"reason": "channel_name_required", "error_code": "channel_name_required", "kind": "rejected", "retry": "correct_input"},
                llm_text="name is required; use list_channel_endpoints to choose an endpoint.",
            )
        if not message.strip():
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                effect_receipt=EffectReceipt(outcome=EffectOutcome.NOT_STARTED),
                text="message is required",
                structured={"channel_id": channel_id, "reason": "message_required", "error_code": "message_required", "kind": "rejected", "retry": "correct_input"},
                llm_text="message must contain ordinary non-blank text.",
            )
        if message.lstrip().startswith("/"):
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                effect_receipt=EffectReceipt(outcome=EffectOutcome.NOT_STARTED),
                text="slash commands are not ordinary channel messages",
                structured={"channel_id": channel_id, "reason": "slash_command_not_allowed", "error_code": "slash_command_not_allowed", "kind": "rejected", "retry": "correct_input"},
                llm_text="send_channel_message accepts ordinary text, not slash commands.",
            )
        if self._is_current_peer_reply(
            turn_id=str(call.meta.get("turn_id") or ""),
            channel_id=channel_id,
        ):
            payload = {
                "channel_id": channel_id,
                "reason": "peer_reply_must_use_final", "error_code": "peer_reply_must_use_final", "kind": "rejected", "retry": "correct_input",
            }
            return CapabilityResult(
                status=RuntimeStatus.FORBIDDEN,
                effect_receipt=EffectReceipt(outcome=EffectOutcome.NOT_STARTED),
                text="reply to the current peer with this turn's final response",
                structured=payload,
                llm_text=(
                    "Do not call send_channel_message to reply to the peer that started "
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
                "error_code": reason,
                "error": exception_report(exc),
                "permanent": bool(exc.permanent),
            }
            not_started = reason in {
                "channel_not_found", "channel_detached", "channel_disabled", "active_send_unsupported",
            }
            payload.update(kind="rejected" if not_started else "failed",
                           retry="correct_input" if not_started else "reconcile_first")
            return CapabilityResult(
                status=status,
                text=payload["error"],
                structured=payload,
                llm_text=render_titled_structured_for_llm(
                    "Channel message rejected before sending" if not_started else "Channel message delivery failed; acceptance is uncertain", payload),
                effect_receipt=EffectReceipt(outcome=EffectOutcome.NOT_STARTED if not_started else EffectOutcome.UNKNOWN),
                recovery_hint=("Inspect list_channel_endpoints and correct the endpoint state before retrying."
                    if not_started else "The provider did not confirm whether delivery was accepted. Reconcile before retrying to avoid duplicates."),
            )
        payload = {
            "channel_id": receipt.endpoint_id,
            "message_id": receipt.message_id,
            "status": receipt.status,
        }
        return CapabilityResult(
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
        guidance=ToolGuidance(
            search_objects=('endpoint', 'endpoints'),
            purpose="Enable a channel endpoint so it accepts incoming messages.",
            use_when="An endpoint was disabled and needs to resume receiving messages.",
            do_not_use_when="The endpoint runtime is disconnected (use attach_channel_endpoint). The endpoint is already enabled.",
            failure_next_steps="If the endpoint is not found, verify its name with list_channel_endpoints. If durable state commit fails, inspect the endpoint's enabled state in list_channel_endpoints before retrying; the runtime rolls back to the previous value.",
        ),
        InputModel=ChannelCapabilitiesChannelIntrospectionProviderEnableInput,
        aliases=("enable_channel_endpoint",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def enable(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._set_enabled(call, enabled=True)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="disable",
        guidance=ToolGuidance(
            search_objects=('endpoint', 'endpoints'),
            purpose="Disable a channel endpoint so it stops accepting incoming messages.",
            use_when="Temporarily stopping an endpoint without removing its configuration.",
            do_not_use_when="Fully disconnecting the runtime (use detach_channel_endpoint). Recovery socket endpoints are protected and cannot be disabled.",
            failure_next_steps="If endpoint not found, verify its name with list_channel_endpoints. Recovery socket endpoints cannot be disabled.",
        ),
        InputModel=ChannelCapabilitiesChannelIntrospectionProviderDisableInput,
        aliases=("disable_channel_endpoint",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def disable(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._set_enabled(call, enabled=False)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="attach",
        guidance=ToolGuidance(
            search_objects=('endpoint', 'endpoints'),
            purpose="Attach a channel endpoint — connect its runtime instance so it can send and receive.",
            use_when="Reconnecting a detached endpoint's runtime. After rescan_channel_providers discovered a new endpoint. An already attached endpoint is an idempotent no-op; attachment alone does not reload provider code.",
            do_not_use_when="Just toggling message acceptance (use enable_channel_endpoint). The endpoint is already attached.",
            failure_next_steps="If provider not found, run rescan_channel_providers first. If already attached, no-op.",
        ),
        InputModel=ChannelCapabilitiesChannelIntrospectionProviderAttachInput,
        aliases=("attach_channel_endpoint",),
        execution=INDIRECT_CONTROL,
    )
    def attach(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._attach_endpoint_provider(str(call.args.get("name") or "").strip())

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="detach",
        guidance=ToolGuidance(
            search_objects=('endpoint', 'endpoints'),
            purpose="Detach a channel endpoint — disconnect its runtime instance without removing configuration.",
            use_when="Temporarily disconnecting an endpoint's runtime (e.g. maintenance, restart). Use attach_channel_endpoint when the disconnected endpoint should resume delivery.",
            do_not_use_when="Just stopping message acceptance (use disable_channel_endpoint — keeps runtime alive).",
            failure_next_steps="If endpoint not found, verify its name with list_channel_endpoints. Detached endpoints can be re-attached with attach_channel_endpoint.",
        ),
        InputModel=ChannelCapabilitiesChannelIntrospectionProviderDetachInput,
        aliases=("detach_channel_endpoint",),
        execution=INDIRECT_CONTROL,
    )
    def detach(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._set_attached(call, attached=False)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="provider",
        action_name="rescan",
        guidance=ToolGuidance(
            search_objects=('provider', 'providers'),
            purpose="Discover physical provider additions and removals in the runtime root.",
            use_when="A provider was installed, removed, enabled, or disabled.",
            do_not_use_when="Provider source changed in place (use reload_channel_provider) or one transport is stuck (use restart_channel_endpoint).",
            failure_next_steps="A malformed manifest is reported without treating an already-known provider as physically removed. Fix it and rescan.",
        ),
        aliases=("rescan_channel_providers",),
        InputModel=ChannelCapabilitiesChannelIntrospectionProviderRescanInput,
        execution=INDIRECT_CONTROL,
    )
    def rescan_providers(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = self._manager().rescan_providers()
        payload["republished_capability_names"] = self._republish_capabilities()
        status = RuntimeStatus.ERROR if payload.get("scan_errors") else RuntimeStatus.OK
        text = (
            "channel provider rescan completed with errors"
            if status == RuntimeStatus.ERROR
            else "channel providers rescanned"
        )
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
        guidance=ToolGuidance(
            search_objects=('provider', 'providers'),
            purpose="Explicitly detach, unload, load, and reattach one runtime-root provider.",
            use_when="A known provider's source, manifest, or provider-wide resources changed in place.",
            do_not_use_when="Only one endpoint connection is stuck (use restart_channel_endpoint). Discovering provider additions/removals or enabled/disabled state (use rescan_channel_providers).",
            failure_next_steps="The endpoint hubs retain queued delivery while code stays unloaded and capabilities remain withdrawn. Fix the provider and retry.",
        ),
        aliases=("reload_channel_provider",),
        InputModel=ChannelCapabilitiesChannelIntrospectionProviderReloadProviderInput,
        execution=INDIRECT_CONTROL,
    )
    def reload_provider(self, call: IntrospectionCall) -> IntrospectionResult:
        provider_id = str(call.args.get("name") or "").strip()
        if not provider_id:
            return _precondition_failure(
                status=RuntimeStatus.INVALID,
                text="name is required",
                llm_text="name is required",
            )
        result = self._manager().reload_provider(provider_id)
        self._republish_capabilities()
        return result

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="restart_endpoint",
        guidance=ToolGuidance(
            search_objects=('endpoint', 'endpoints'),
            purpose="Restart one channel endpoint runtime instance without reloading provider code.",
            use_when="One endpoint connection is stuck, misbehaving, or needs a fresh transport session.",
            do_not_use_when="Provider source or provider-wide resources changed (use reload_channel_provider or rescan_channel_providers).",
            failure_next_steps="If the endpoint is missing, verify it with list_channel_endpoints. If restart fails, use inspect_channel_endpoint_health.",
        ),
        aliases=("restart_channel_endpoint",),
        InputModel=ChannelCapabilitiesChannelIntrospectionProviderRestartEndpointInput,
        execution=INDIRECT_CONTROL,
    )
    def restart_endpoint(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._restart_endpoint(str(call.args.get("name") or "").strip())

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="endpoint",
        action_name="inspect",
        guidance=ToolGuidance(
            search_objects=('endpoint', 'endpoints'),
            purpose="Inspect full state of one channel endpoint.",
            use_when="Need detailed status of a specific endpoint (enabled, attached, paired, provider info).",
            do_not_use_when="Just need a list of all endpoints (use list_channel_endpoints). Checking auth (use inspect_channel_endpoint_auth).",
            failure_next_steps="If endpoint not found, verify its name with list_channel_endpoints.",
        ),
        aliases=("inspect_channel_endpoint",),
    )
    def inspect_endpoint(self, call: IntrospectionCall) -> IntrospectionResult:
        target = self._require_target(call)
        if target is None:
            return _precondition_failure(
                status=RuntimeStatus.NOT_FOUND,
                text="channel endpoint not found",
                llm_text="channel endpoint not found",
            )
        return self._manager().inspect_endpoint(target.endpoint_id)

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="endpoint",
        action_name="auth_state",
        guidance=ToolGuidance(
            search_objects=('auth', 'authorization', 'credential', 'credentials'),
            purpose="Inspect whether an endpoint is authenticated and authorized.",
            use_when="Diagnosing auth failures or checking if credentials are still valid.",
            do_not_use_when="Applying credentials (use set_channel_endpoint_auth_material). General endpoint state (use inspect_channel_endpoint).",
            failure_next_steps="If endpoint not found, verify its name with list_channel_endpoints. If not authenticated, apply credentials with set_channel_endpoint_auth_material.",
        ),
        aliases=("inspect_channel_endpoint_auth",),
    )
    def auth_state(self, call: IntrospectionCall) -> IntrospectionResult:
        target = self._require_target(call)
        if target is None:
            return _precondition_failure(
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
        guidance=ToolGuidance(
            search_objects=('material', 'materials', 'credential', 'credentials', 'token', 'tokens'),
            purpose="Apply endpoint authorization material (tokens, credentials) without exposing secrets in output.",
            use_when="An endpoint needs credentials to authenticate (e.g. Telegram bot token, API key).",
            do_not_use_when="Reading current auth state (use inspect_channel_endpoint_auth).",
            failure_next_steps="If endpoint not found, verify its name with list_channel_endpoints. If material format invalid, check provider documentation for required fields.",
        ),
        InputModel=ChannelCapabilitiesChannelIntrospectionProviderSetAuthMaterialInput,
        aliases=("set_channel_endpoint_auth_material",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def set_auth_material(self, call: IntrospectionCall) -> IntrospectionResult:
        target = self._require_target(call)
        if target is None:
            return _precondition_failure(
                status=RuntimeStatus.NOT_FOUND,
                text="channel endpoint not found",
                llm_text="channel endpoint not found",
            )
        material = call.args.get("material")
        if not isinstance(material, dict):
            return _precondition_failure(
                status=RuntimeStatus.INVALID,
                text="material must be an object",
                llm_text="material must be an object",
            )
        return self._manager().set_auth_material(target.endpoint_id, dict(material))

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="endpoint",
        action_name="backlog",
        guidance=ToolGuidance(
            search_objects=('backlog', 'backlogs', 'message', 'messages'),
            purpose="Inspect undelivered message backlog for one endpoint.",
            use_when='Checking if messages are queued but not yet delivered (endpoint was detached or slow). A large backlog is an observation, not proof of its cause; inspect endpoint health and attachment before reconnecting.',
            do_not_use_when="General endpoint health (use inspect_channel_endpoint_health). Listing endpoints (use list_channel_endpoints).",
            failure_next_steps="If endpoint not found, verify its name with list_channel_endpoints. Large backlog may indicate the endpoint needs re-attachment.",
        ),
        aliases=("inspect_channel_endpoint_backlog",),
    )
    def backlog(self, call: IntrospectionCall) -> IntrospectionResult:
        target = self._require_target(call)
        if target is None:
            return _precondition_failure(
                status=RuntimeStatus.NOT_FOUND,
                text="channel endpoint not found",
                llm_text="channel endpoint not found",
            )
        return self._manager().inspect_backlog(target.endpoint_id)

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="endpoint",
        action_name="health",
        guidance=ToolGuidance(
            search_objects=('health',),
            purpose="Inspect network connectivity and delivery health for one endpoint.",
            use_when='Diagnosing message delivery failures or connection issues. A successful inspection can report an unhealthy endpoint. Inspect authentication and connection state; restart_channel_endpoint rebuilds its connection when appropriate.',
            do_not_use_when="Checking auth (use inspect_channel_endpoint_auth). Checking message queue (use inspect_channel_endpoint_backlog).",
            failure_next_steps="If endpoint not found, verify its name with list_channel_endpoints. If unhealthy, try restart_channel_endpoint to refresh its runtime instance.",
        ),
        aliases=("inspect_channel_endpoint_health",),
    )
    def health(self, call: IntrospectionCall) -> IntrospectionResult:
        target = self._require_target(call)
        if target is None:
            return _precondition_failure(
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
        endpoint_id = str(call.args.get("name") or "").strip()
        if not endpoint_id:
            return _precondition_failure(
                status=RuntimeStatus.INVALID,
                text="name is required",
                llm_text="name is required",
            )
        endpoint = self.runtime.get_endpoint(endpoint_id)
        record = self.repository.get(endpoint_id)
        if not enabled and is_recovery_socket_endpoint(record, endpoint, self.runtime_root or Path.cwd()):
            return _precondition_failure(
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
            return _precondition_failure(
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
                llm_text=("Channel endpoint durable state update failed. "
                          f"Runtime enabled state restored to {previous_enabled}; inspect durable state before retrying. "
                          f"Cause: {exception_report(exc)}"),
            )
        payload = {"endpoint_id": endpoint_id, "enabled": enabled}
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text=f"channel endpoint {'enabled' if enabled else 'disabled'}",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Channel endpoint state updated", payload),
        )

    def _set_attached(self, call: IntrospectionCall, *, attached: bool) -> IntrospectionResult:
        endpoint_id = str(call.args.get("name") or "").strip()
        return self._set_endpoint_attached(endpoint_id, attached=attached)

    def _set_endpoint_attached(self, endpoint_id: str, *, attached: bool) -> IntrospectionResult:
        if not endpoint_id:
            return _precondition_failure(
                status=RuntimeStatus.INVALID,
                text="name is required",
                llm_text="name is required",
            )
        if attached:
            return self._manager().attach_endpoint(endpoint_id)
        return self._manager().detach_endpoint(endpoint_id)

    def _attach_endpoint_provider(self, endpoint_id: str) -> IntrospectionResult:
        return self._set_endpoint_attached(endpoint_id, attached=True)

    def _restart_endpoint(self, endpoint_id: str) -> IntrospectionResult:
        if not endpoint_id:
            return _precondition_failure(
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
            error=(render_titled_structured_for_llm(result.llm_text or result.text,
                   {"text": result.text, "details": structured}) if result.status != RuntimeStatus.OK else None),
            payload={"endpoint_id": endpoint_id, "channel_result": structured},
        )

    def _republish_capabilities(self) -> list[str]:
        context = self.main_context
        if context is None:
            return []
        handle = context.module_registry.get(self.module_id)
        if handle is None:
            return []
        previous_subtree = handle.mounted_subtree
        try:
            handle.mounted_subtree = None
            context.execution_runtime.hydrate_module_handle(handle)
            # mount_subtree compiles and swaps one immutable registry
            # generation under its lock. Keeping the previous subtree mounted
            # until this call succeeds avoids a transient empty capability
            # registry during channel publication changes.
            published = context.execution_runtime.mount_subtree(handle)
        except Exception:
            handle.mounted_subtree = previous_subtree
            raise
        if previous_subtree is not None:
            previous_subtree.mounted = False
        handle.published_capabilities = published
        return published


def inspect_channel(provider: ChannelIntrospectionProvider) -> ChannelSnapshot:
    targets = provider.iter_endpoints()
    return ChannelSnapshot(
        endpoint_count=len(targets),
        attached_count=sum(1 for item in targets if item.attached),
        enabled_count=sum(1 for item in targets if item.enabled),
    )

def subscribe_core_state(context: MainContext, runtime: ChannelRuntime):
    """Channel is a projection of Core state, not its owner."""
    from pal.core.core_events import ALL_CORE_TOPICS

    bus = context.core_event_bus
    previous = None

    def project(topic, event):
        nonlocal previous
        state = bus.snapshot()
        if state != previous:
            runtime.publish_runtime_state(**state)
            previous = state
        runtime.publish_core_event(topic, event)

    for topic in ALL_CORE_TOPICS:
        bus.subscribe(topic, project)
    initial = bus.snapshot()
    runtime.publish_runtime_state(**initial)
    previous = initial

    def close():
        for topic in ALL_CORE_TOPICS:
            bus.unsubscribe(topic, project)
    return close


class TypingSubscriber:
    def __init__(self, runtime: ChannelRuntime) -> None:
        self._runtime = runtime
        self._routes: dict[str, tuple[str, dict[str, Any]]] = {}

    def __call__(self, topic: str, event: TurnEvent) -> None:
        turn_id = str(event.get("turn_id") or "")
        if not turn_id:
            return
        endpoint_id = str(event.get("endpoint_id") or "")
        reply_target = dict(event.get("reply_target") or {})
        payload = {"typing_owner": f"turn:{turn_id}", "turn_id": turn_id}
        if topic == TURN_START:
            if not endpoint_id:
                return
            previous = self._routes.get(turn_id)
            if previous == (endpoint_id, reply_target):
                return
            if previous is not None:
                self._runtime.queue_endpoint_status(previous[0], "working_stop", reply_target=previous[1], payload=payload)
            self._routes[turn_id] = (endpoint_id, reply_target)
            self._runtime.queue_endpoint_status(endpoint_id, "typing_start", reply_target=reply_target, payload=payload)
        elif topic == TURN_END:
            # The start route owns teardown; a final event may omit or change it.
            route = self._routes.pop(turn_id, None)
            if route is None:
                return
            self._runtime.queue_endpoint_status(route[0], "working_stop", reply_target=route[1], payload=payload)


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
    from pal.channel.tool_activity import ToolActivityRouter
    from pal.execution.activity import ExecutionActivityDecorator
    activity_router = ToolActivityRouter(runtime)
    context.execution_runtime.activity_decorator = ExecutionActivityDecorator(activity_router.open_sink)
    handle.cleanup_callbacks.append(subscribe_core_state(context, runtime))
    context.turn_event_bus.subscribe(TURN_START, activity_router)
    context.turn_event_bus.subscribe(TURN_END, activity_router)
    typing_sub = TypingSubscriber(runtime)
    context.turn_event_bus.subscribe(TURN_START, typing_sub)
    context.turn_event_bus.subscribe(TURN_END, typing_sub)
    context.lifecycle_owner_registry.register_owner(provider)
    return handle
