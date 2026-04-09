from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from pal.channel.contracts import (
    ChannelDeliveryError,
    ChannelEnvelope,
    EndpointConfig,
    QueuedReply,
    QueuedStatus,
    QueuedStreamEvent,
    ResponseHandle,
)
from pal.core.mailbox import Mailbox
from pal.foundation import EventEnvelope
from pal.shared import EventKind, SourceKind
from pal.stream_events import NormalizedLLMStreamEvent


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
    status_outbox: deque[QueuedStatus] = field(default_factory=deque)
    stream_outbox: deque[QueuedStreamEvent] = field(default_factory=deque)
    last_delivery_error: str = ""
    _stream_sessions: dict[int, dict[str, Any]] = field(default_factory=dict)

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

    def send_status(self, response_handle: ResponseHandle, kind: str, payload: dict[str, Any]) -> None:
        _ = response_handle
        _ = kind
        _ = payload

    def send_stream_event(self, response_handle: ResponseHandle, event: NormalizedLLMStreamEvent) -> None:
        session = self._stream_sessions.setdefault(id(response_handle), {"text": "", "reasoning": "", "events": []})
        session["events"].append(event.event_kind)
        if event.text:
            session["text"] = f'{session["text"]}{event.text}'
        if event.reasoning_text:
            session["reasoning"] = f'{session["reasoning"]}{event.reasoning_text}'

    def prepare_final_reply(self, response_handle: ResponseHandle, text: str) -> str | None:
        session = self._stream_sessions.pop(id(response_handle), None)
        if session is None:
            return text
        if self.endpoint.channel_kind in {"stdio", "socket"} and str(session.get("text") or "") == text:
            return None
        return text

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
        return self.emit_normalized(
            EventEnvelope(
                event_kind=event_kind,
                source_kind=SourceKind.CHANNEL,
                payload=normalized_payload,
                correlation_id=correlation_id,
            ),
            response_handle=self.build_response_handle(reply_target=reply_target),
        )

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
        return reply_id

    def queue_stream_event(
        self,
        event: NormalizedLLMStreamEvent,
        *,
        response_handle: ResponseHandle | None = None,
    ) -> str:
        event_id = str(uuid4())
        self.stream_outbox.append(
            QueuedStreamEvent(
                event_id=event_id,
                response_handle=response_handle or self.build_response_handle(),
                endpoint=self.endpoint,
                event=event,
            )
        )
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
        return status_id

    def has_pending(self) -> bool:
        return self.mailbox.has_pending()

    def has_queued_replies(self) -> bool:
        return bool(self.outbox)

    def has_queued_stream_events(self) -> bool:
        return bool(self.stream_outbox)

    def has_queued_status(self) -> bool:
        return bool(self.status_outbox)

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

    def inspect_backlog(self) -> dict[str, Any]:
        return {
            "endpoint_id": self.endpoint.endpoint_id,
            "inbox_size": len(self.mailbox.peek_all()),
            "outbox_size": len(self.outbox),
            "status_outbox_size": len(self.status_outbox),
        }


# Compatibility alias while the rest of the package transitions to the new
# endpoint abstraction naming.
ChannelEndpointBase = ChannelEndpointQueueBase
