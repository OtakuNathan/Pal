from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
import time
from typing import Any
from uuid import uuid4

from pal.channel.contracts import (
    ChannelDeliveryError,
    ChannelEnvelope,
    ChannelMessageReceipt,
    EndpointConfig,
    QueuedAttachment,
    QueuedReply,
    QueuedStatus,
    QueuedStreamEvent,
    ResponseHandle,
)
from pal.control.contracts import InteractionButtonSpec, InteractionMessageSpec, InteractionResult
from pal.core.mailbox import Mailbox
from pal.foundation import AttachmentSpec, EventEnvelope
from pal.shared import EventKind, SourceKind
from pal.stream_events import NormalizedLLMStreamEvent


INTERACTIVE_STATUS_KINDS = {"interactive_open", "interactive_update", "interactive_resolve", "interactive_expire"}


@dataclass
class ChannelEndpointQueueBase(ABC):
    # This is the template-method runtime base for concrete endpoints.
    # Subclasses implement transport- and auth-specific hooks; the base class
    # owns the canonical mailbox/outbox flow used by ChannelRuntime.
    endpoint: EndpointConfig
    enabled: bool = True
    attached: bool = True
    paired: bool = False
    pairing_metadata: dict[str, Any] = field(default_factory=dict)
    mailbox: Mailbox[ChannelEnvelope] = field(default_factory=Mailbox)
    outbox: deque[QueuedReply] = field(default_factory=deque)
    attachment_outbox: deque[QueuedAttachment] = field(default_factory=deque)
    status_outbox: deque[QueuedStatus] = field(default_factory=deque)
    stream_outbox: deque[QueuedStreamEvent] = field(default_factory=deque)
    last_delivery_error: str = ""
    on_ready: Callable[[], None] | None = None
    _stream_sessions: dict[int, dict[str, Any]] = field(default_factory=dict)
    _interactive_messages: dict[str, dict[str, Any]] = field(default_factory=dict)
    _interactive_default_ttl_seconds: float = 60.0
    _control_commands_manifest: list[dict[str, str]] = field(default_factory=list)

    @abstractmethod
    def normalize_raw(self, payload: Any) -> dict[str, Any]:
        ...

    @abstractmethod
    def send_reply(self, response_handle: ResponseHandle, text: str) -> None:
        ...

    @abstractmethod
    def inspect_health(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def inspect_auth_state(self) -> dict[str, Any]:
        ...

    def derive_default_reply_target(self) -> dict[str, Any]:
        return {}

    async def send_message(self, message: str) -> ChannelMessageReceipt:
        """Accept an active message for this configured endpoint.

        Reply-oriented transports can reuse their normal outbox when the
        endpoint binding provides an unambiguous default target. Transports
        without such a binding must override this method or reject the
        operation; callers never supply provider-specific target data.
        """
        target = self.derive_default_reply_target()
        if not target:
            raise ChannelDeliveryError(
                f"channel endpoint {self.endpoint.endpoint_id!r} does not support active messages",
                permanent=True,
                reason="active_send_unsupported",
            )
        message_id = self.queue_reply(
            message,
            response_handle=self.build_response_handle(reply_target=target),
        )
        return ChannelMessageReceipt(
            endpoint_id=self.endpoint.endpoint_id,
            message_id=message_id,
            status="accepted",
        )

    def send_status(self, response_handle: ResponseHandle, kind: str, payload: dict[str, Any]) -> None:
        if kind == "control_catalog":
            self.apply_control_catalog(payload)
            return
        if kind not in INTERACTIVE_STATUS_KINDS:
            return
        spec = payload.get("spec")
        if isinstance(spec, InteractionMessageSpec):
            self.apply_interaction_status(response_handle, kind=kind, spec=spec, payload=payload)
            return
        spec = payload.get("spec")
        text = str(payload.get("text") or "").strip()
        if not text and spec is not None:
            text = str(getattr(spec, "text", "") or "").strip()
        if text:
            self.send_reply(response_handle, text)

    def apply_control_catalog(self, payload: dict[str, Any]) -> None:
        self._control_commands_manifest = self.normalize_control_commands(payload)

    def normalize_control_commands(self, payload: dict[str, Any]) -> list[dict[str, str]]:
        manifest: list[dict[str, str]] = []
        for item in list(payload.get("commands") or []):
            if not isinstance(item, dict):
                continue
            command = str(item.get("command") or "").strip().lower()
            description = str(item.get("description") or "").strip()
            if not command or not description:
                continue
            if re.fullmatch(r"[a-z0-9_]{1,32}", command) is None:
                continue
            manifest.append({"command": command, "description": description[:256]})
        return manifest

    def apply_interaction_status(
        self,
        response_handle: ResponseHandle,
        *,
        kind: str,
        spec: InteractionMessageSpec,
        payload: dict[str, Any],
    ) -> None:
        _ = payload
        if kind in {"interactive_open", "interactive_update"}:
            self.open_or_update_interaction(response_handle, spec=spec, allow_update=(kind == "interactive_update"))
            return
        if kind in {"interactive_resolve", "interactive_expire"}:
            self.resolve_interaction(response_handle, spec=spec)

    def open_or_update_interaction(
        self,
        response_handle: ResponseHandle,
        *,
        spec: InteractionMessageSpec,
        allow_update: bool,
    ) -> None:
        _ = allow_update
        self.prune_interactive_messages()
        self.remember_interaction_message(spec, {"reply_target": dict(response_handle.reply_target)})
        if spec.text:
            self.send_reply(response_handle, spec.text)

    def resolve_interaction(self, response_handle: ResponseHandle, *, spec: InteractionMessageSpec) -> None:
        self.forget_interaction_message(spec.interaction_id)
        if spec.text:
            self.send_reply(response_handle, spec.text)

    def remember_interaction_message(self, spec: InteractionMessageSpec, target: dict[str, Any]) -> None:
        metadata = dict(target)
        metadata.update(
            {
                "interaction_kind": spec.interaction_kind,
                "expires_at_monotonic": self.interaction_expiry_monotonic(spec),
                "actions": self.build_interaction_action_map(spec),
            }
        )
        self._interactive_messages[spec.interaction_id] = metadata

    def interaction_expiry_monotonic(self, spec: InteractionMessageSpec) -> float | None:
        raw = str(spec.expires_at or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return time.monotonic() + float(self._interactive_default_ttl_seconds)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        current = datetime.now(timezone.utc)
        delta = max((parsed.astimezone(timezone.utc) - current).total_seconds(), 0.0)
        return time.monotonic() + delta

    def prune_interactive_messages(self, *, now: float | None = None) -> None:
        current = float(now if now is not None else time.monotonic())
        stale = [
            interaction_id
            for interaction_id, metadata in self._interactive_messages.items()
            if self.is_interaction_metadata_expired(metadata, now=current)
        ]
        for interaction_id in stale:
            self._interactive_messages.pop(interaction_id, None)

    def is_interaction_metadata_expired(
        self,
        metadata: dict[str, Any],
        *,
        now: float | None = None,
    ) -> bool:
        expires_at_monotonic = metadata.get("expires_at_monotonic")
        if not isinstance(expires_at_monotonic, (int, float)):
            return False
        current = float(now if now is not None else time.monotonic())
        return float(expires_at_monotonic) <= current

    def forget_interaction_message(self, interaction_id: str) -> None:
        self._interactive_messages.pop(str(interaction_id or "").strip(), None)

    def expired_interaction_text(self, interaction_kind: str) -> str:
        kind = str(interaction_kind or "").strip()
        if kind == "reset_confirm":
            return "This reset request expired."
        if kind == "approval_request":
            return "This approval request expired."
        return "This interaction expired."

    def build_interaction_action_map(self, spec: InteractionMessageSpec) -> dict[str, dict[str, Any]]:
        actions: dict[str, dict[str, Any]] = {}
        button_index = 0
        for row in spec.buttons:
            for button in row:
                if not isinstance(button, InteractionButtonSpec):
                    continue
                token = self.interaction_button_token(button_index)
                button_index += 1
                actions[token] = {
                    "action_key": button.action_key,
                    "action_args": dict(button.action_args),
                }
        return actions

    def interaction_button_token(self, button_index: int) -> str:
        return f"b{button_index}"

    def parse_interaction_callback_data(self, data: str) -> tuple[str, str]:
        raw = str(data or "").strip()
        if not raw.startswith("ix:"):
            return "", ""
        parts = raw.split(":", 2)
        if len(parts) != 3:
            return "", ""
        return parts[1].strip(), parts[2].strip()

    def interaction_result_from_token(self, interaction_id: str, button_token: str) -> InteractionResult | None:
        interaction_id = str(interaction_id or "").strip()
        button_token = str(button_token or "").strip()
        if not interaction_id or not button_token:
            return None
        metadata = self._interactive_messages.get(interaction_id)
        if not metadata:
            return None
        if self.is_interaction_metadata_expired(metadata):
            self.forget_interaction_message(interaction_id)
            return None
        actions = metadata.get("actions")
        action_payload = actions.get(button_token) if isinstance(actions, dict) else None
        if not isinstance(action_payload, dict):
            return None
        return InteractionResult(
            interaction_id=interaction_id,
            interaction_kind=str(metadata.get("interaction_kind") or ""),
            action_key=str(action_payload.get("action_key") or "").strip(),
            action_args=dict(action_payload.get("action_args") or {}),
        )

    def emit_interaction_result(
        self,
        result: InteractionResult,
        *,
        correlation_id: str | None = None,
        reply_target: dict[str, Any] | None = None,
    ) -> ChannelEnvelope | None:
        return self.emit_normalized(
            EventEnvelope(
                event_kind=EventKind.INTERACTION_RESULT,
                source_kind=SourceKind.CHANNEL,
                payload=result,
                correlation_id=correlation_id,
            ),
            response_handle=self.build_response_handle(reply_target=reply_target),
        )

    def send_attachment(self, response_handle: ResponseHandle, attachment: AttachmentSpec) -> None:
        _ = response_handle
        _ = attachment
        raise ChannelDeliveryError("endpoint does not support attachments", permanent=True)

    def send_stream_event(self, response_handle: ResponseHandle, event: NormalizedLLMStreamEvent) -> None:
        session = self._stream_sessions.setdefault(
            id(response_handle),
            {"text": "", "reasoning": "", "events": [], "closed": False, "abort_reason": "", "text_delivered": False},
        )
        if bool(session.get("closed")):
            return
        session["events"].append(event.event_kind)
        if event.text:
            session["text"] = f'{session["text"]}{event.text}'
        if event.reasoning_text:
            session["reasoning"] = f'{session["reasoning"]}{event.reasoning_text}'

    def prepare_final_reply(self, response_handle: ResponseHandle, text: str) -> str | None:
        session = self._stream_sessions.pop(id(response_handle), None)
        if session is None:
            return text
        if bool(session.get("closed")):
            return None
        if bool(session.get("text_delivered")) and str(session.get("text") or "") == text:
            return None
        return text

    def mark_stream_text_delivered(self, response_handle: ResponseHandle, event: NormalizedLLMStreamEvent) -> None:
        if not str(event.text or ""):
            return
        session = self._stream_sessions.setdefault(
            id(response_handle),
            {"text": "", "reasoning": "", "events": [], "closed": False, "abort_reason": "", "text_delivered": False},
        )
        session["text_delivered"] = True

    def abort_stream(self, response_handle: ResponseHandle, *, reason: str = "interrupted") -> None:
        session = self._stream_sessions.setdefault(
            id(response_handle),
            {"text": "", "reasoning": "", "events": [], "closed": False, "abort_reason": "", "text_delivered": False},
        )
        session["closed"] = True
        session["abort_reason"] = str(reason or "interrupted")
        remaining: deque[QueuedStreamEvent] = deque()
        while self.stream_outbox:
            queued = self.stream_outbox.popleft()
            if id(queued.response_handle) != id(response_handle):
                remaining.append(queued)
        self.stream_outbox = remaining

    def pair(
        self,
        *,
        binding_key: str | None = None,
        send_policy: dict[str, Any] | None = None,
        pairing_metadata: dict[str, Any] | None = None,
    ) -> None:
        self.endpoint = EndpointConfig(
            endpoint_id=self.endpoint.endpoint_id,
            channel_kind=self.endpoint.channel_kind,
            binding_key=binding_key or self.endpoint.binding_key,
            send_policy=dict(send_policy or self.endpoint.send_policy),
        )
        self.paired = True
        if pairing_metadata:
            self.pairing_metadata.update(pairing_metadata)

    def apply_auth_material(self, material: dict[str, Any]) -> dict[str, Any]:
        # Concrete endpoints may override this to validate, store, or exchange
        # credentials. The base version only tracks non-secret control state.
        self.pair(pairing_metadata=dict(material))
        return self.inspect_auth_state()

    def enable(self) -> None:
        self.enabled = True

    def disable(self) -> None:
        self.enabled = False

    def attach(self) -> None:
        self.attached = True

    def detach(self) -> None:
        self.attached = False

    def build_response_handle(self, *, reply_target: dict[str, Any] | None = None) -> ResponseHandle:
        return ResponseHandle(
            endpoint_id=self.endpoint.endpoint_id,
            reply_target=dict(reply_target or {}),
        )

    def emit_normalized(
        self,
        event: EventEnvelope,
        *,
        response_handle: ResponseHandle | None = None,
    ) -> ChannelEnvelope | None:
        if not self.enabled or not self.attached:
            return None
        normalized = ChannelEnvelope(
            event=EventEnvelope(
                event_kind=event.event_kind,
                source_kind=SourceKind.CHANNEL,
                payload=event.payload,
                correlation_id=event.correlation_id or event.event_id,
            ),
            endpoint=self.endpoint,
            response_handle=response_handle or self.build_response_handle(),
        )
        self.mailbox.put(normalized)
        self._notify_ready()
        return normalized

    def accept_raw(
        self,
        payload: Any,
        *,
        event_kind: str,
        correlation_id: str | None = None,
        reply_target: dict[str, Any] | None = None,
    ) -> ChannelEnvelope | None:
        normalized_payload = self.normalize_raw(payload)
        resolved_event_kind = self._resolve_ingress_event_kind(event_kind, normalized_payload)
        return self.emit_normalized(
            EventEnvelope(
                event_kind=resolved_event_kind,
                source_kind=SourceKind.CHANNEL,
                payload=normalized_payload,
                correlation_id=correlation_id,
            ),
            response_handle=self.build_response_handle(reply_target=reply_target),
        )

    def _resolve_ingress_event_kind(self, event_kind: str, normalized_payload: dict[str, Any]) -> str:
        if str(event_kind) != str(EventKind.USER_MESSAGE):
            return event_kind
        text = str(normalized_payload.get("text") or "").strip()
        if text.startswith("/"):
            return EventKind.SLASH_COMMAND
        return event_kind

    def queue_reply(self, text: str, *, response_handle: ResponseHandle | None = None) -> str:
        handle = response_handle or self.build_response_handle()
        reply_id = str(uuid4())
        prepared = self.prepare_final_reply(handle, text)
        if prepared is None:
            return reply_id
        self.outbox.append(
            QueuedReply(
                reply_id=reply_id,
                response_handle=handle,
                endpoint=self.endpoint,
                text=prepared,
            )
        )
        self._notify_ready()
        return reply_id

    def queue_stream_event(
        self,
        event: NormalizedLLMStreamEvent,
        *,
        response_handle: ResponseHandle | None = None,
    ) -> str:
        handle = response_handle or self.build_response_handle()
        session = self._stream_sessions.get(id(handle))
        if session is not None and bool(session.get("closed")):
            return str(uuid4())
        event_id = str(uuid4())
        self.stream_outbox.append(
            QueuedStreamEvent(
                event_id=event_id,
                response_handle=handle,
                endpoint=self.endpoint,
                event=event,
            )
        )
        self._notify_ready()
        return event_id

    def queue_status(
        self,
        kind: str,
        *,
        response_handle: ResponseHandle | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        status_id = str(uuid4())
        self.status_outbox.append(
            QueuedStatus(
                status_id=status_id,
                response_handle=response_handle or self.build_response_handle(),
                endpoint=self.endpoint,
                kind=str(kind),
                payload=dict(payload or {}),
            )
        )
        self._notify_ready()
        return status_id

    def queue_attachment(
        self,
        attachment: AttachmentSpec,
        *,
        response_handle: ResponseHandle | None = None,
    ) -> str:
        attachment_id = str(uuid4())
        self.attachment_outbox.append(
            QueuedAttachment(
                attachment_id=attachment_id,
                response_handle=response_handle or self.build_response_handle(),
                endpoint=self.endpoint,
                attachment=attachment,
            )
        )
        self._notify_ready()
        return attachment_id

    def _notify_ready(self) -> None:
        if self.on_ready is not None:
            self.on_ready()

    def has_pending(self) -> bool:
        return self.mailbox.has_pending()

    def has_queued_replies(self) -> bool:
        return bool(self.outbox)

    def has_queued_stream_events(self) -> bool:
        return bool(self.stream_outbox)

    def has_queued_status(self) -> bool:
        return bool(self.status_outbox)

    def has_queued_attachments(self) -> bool:
        return bool(self.attachment_outbox)

    def poll(self) -> list[ChannelEnvelope]:
        return self.mailbox.drain()

    def flush_outbox(self) -> list[EventEnvelope]:
        if not self.outbox:
            return []
        pending = list(self.outbox)
        self.outbox.clear()
        emitted: list[EventEnvelope] = []
        for item in pending:
            if not self.attached or not self.enabled:
                self.outbox.append(
                    QueuedReply(
                        reply_id=item.reply_id,
                        response_handle=item.response_handle,
                        endpoint=item.endpoint,
                        text=item.text,
                        attempts=item.attempts + 1,
                    )
                )
                emitted.append(
                    EventEnvelope(
                        event_kind=EventKind.REPLY_FAILED,
                        source_kind=SourceKind.CHANNEL,
                        payload={
                            "reply_id": item.reply_id,
                            "endpoint_id": self.endpoint.endpoint_id,
                            "reason": "endpoint_unavailable",
                        },
                    )
                )
                continue
            try:
                self.send_reply(item.response_handle, item.text)
            except Exception as exc:
                self.last_delivery_error = str(exc)
                permanent = isinstance(exc, ChannelDeliveryError) and bool(getattr(exc, "permanent", False))
                if not permanent:
                    self.outbox.append(
                        QueuedReply(
                            reply_id=item.reply_id,
                            response_handle=item.response_handle,
                            endpoint=item.endpoint,
                            text=item.text,
                            attempts=item.attempts + 1,
                        )
                    )
                emitted.append(
                    EventEnvelope(
                        event_kind=EventKind.REPLY_FAILED,
                        source_kind=SourceKind.CHANNEL,
                        payload={
                            "reply_id": item.reply_id,
                            "endpoint_id": self.endpoint.endpoint_id,
                            "reason": str(exc),
                            "permanent": permanent,
                            "attempts": item.attempts + 1,
                        },
                    )
                )
                continue
            self.last_delivery_error = ""
            emitted.append(
                EventEnvelope(
                    event_kind=EventKind.REPLY_DELIVERED,
                    source_kind=SourceKind.CHANNEL,
                    payload={"reply_id": item.reply_id, "endpoint_id": self.endpoint.endpoint_id},
                )
            )
        return emitted

    def flush_stream_outbox(self) -> list[EventEnvelope]:
        if not self.stream_outbox:
            return []
        pending = list(self.stream_outbox)
        self.stream_outbox.clear()
        emitted: list[EventEnvelope] = []
        for item in pending:
            try:
                self.send_stream_event(item.response_handle, item.event)
            except Exception as exc:
                self.last_delivery_error = str(exc)
                permanent = isinstance(exc, ChannelDeliveryError) and bool(getattr(exc, "permanent", False))
                if not permanent:
                    self.stream_outbox.append(
                        QueuedStreamEvent(
                            event_id=item.event_id,
                            response_handle=item.response_handle,
                            endpoint=item.endpoint,
                            event=item.event,
                            attempts=item.attempts + 1,
                        )
                    )
                emitted.append(
                    EventEnvelope(
                        event_kind=EventKind.REPLY_FAILED,
                        source_kind=SourceKind.CHANNEL,
                        payload={
                            "reply_id": item.event_id,
                            "endpoint_id": self.endpoint.endpoint_id,
                            "reason": str(exc),
                            "permanent": permanent,
                            "attempts": item.attempts + 1,
                        },
                    )
                )
        return emitted

    def flush_status_outbox(self) -> None:
        if not self.status_outbox:
            return
        pending = list(self.status_outbox)
        self.status_outbox.clear()
        for item in pending:
            if not self.attached or not self.enabled:
                continue
            try:
                self.send_status(item.response_handle, item.kind, dict(item.payload))
            except Exception as exc:
                self.last_delivery_error = str(exc)

    def flush_attachment_outbox(self) -> list[EventEnvelope]:
        if not self.attachment_outbox:
            return []
        pending = list(self.attachment_outbox)
        self.attachment_outbox.clear()
        emitted: list[EventEnvelope] = []
        for item in pending:
            if not self.attached or not self.enabled:
                self.attachment_outbox.append(
                    QueuedAttachment(
                        attachment_id=item.attachment_id,
                        response_handle=item.response_handle,
                        endpoint=item.endpoint,
                        attachment=item.attachment,
                        attempts=item.attempts + 1,
                    )
                )
                emitted.append(
                    EventEnvelope(
                        event_kind=EventKind.REPLY_FAILED,
                        source_kind=SourceKind.CHANNEL,
                        payload={
                            "reply_id": item.attachment_id,
                            "endpoint_id": self.endpoint.endpoint_id,
                            "reason": "endpoint_unavailable",
                        },
                    )
                )
                continue
            try:
                self.send_attachment(item.response_handle, item.attachment)
            except Exception as exc:
                self.last_delivery_error = str(exc)
                permanent = isinstance(exc, ChannelDeliveryError) and bool(getattr(exc, "permanent", False))
                if not permanent:
                    self.attachment_outbox.append(
                        QueuedAttachment(
                            attachment_id=item.attachment_id,
                            response_handle=item.response_handle,
                            endpoint=item.endpoint,
                            attachment=item.attachment,
                            attempts=item.attempts + 1,
                        )
                    )
                emitted.append(
                    EventEnvelope(
                        event_kind=EventKind.REPLY_FAILED,
                        source_kind=SourceKind.CHANNEL,
                        payload={
                            "reply_id": item.attachment_id,
                            "endpoint_id": self.endpoint.endpoint_id,
                            "reason": str(exc),
                            "permanent": permanent,
                            "attempts": item.attempts + 1,
                        },
                    )
                )
                continue
            self.last_delivery_error = ""
            emitted.append(
                EventEnvelope(
                    event_kind=EventKind.REPLY_DELIVERED,
                    source_kind=SourceKind.CHANNEL,
                    payload={"reply_id": item.attachment_id, "endpoint_id": self.endpoint.endpoint_id},
                )
            )
        return emitted

    def inspect_backlog(self) -> dict[str, Any]:
        return {
            "endpoint_id": self.endpoint.endpoint_id,
            "inbox_size": len(self.mailbox.peek_all()),
            "outbox_size": len(self.outbox),
            "attachment_outbox_size": len(self.attachment_outbox),
            "status_outbox_size": len(self.status_outbox),
        }


# Compatibility alias while the rest of the package transitions to the new
# endpoint abstraction naming.
ChannelEndpointBase = ChannelEndpointQueueBase
