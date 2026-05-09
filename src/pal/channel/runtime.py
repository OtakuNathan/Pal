from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import uuid4

from pal.channel.contracts import ChannelAdapter, ChannelEnvelope, ChannelRuntimePort, QueuedAttachment, QueuedReply, QueuedStatus, QueuedStreamEvent
from pal.channel.channel_endpoint_queue_base import ChannelEndpointBase
from pal.core.mailbox import Mailbox
from pal.foundation import AttachmentSpec, EventEnvelope
from pal.shared import EventKind, SourceKind
from pal.stream_events import NormalizedLLMStreamEvent


@dataclass
class ChannelAdapterRegistry:
    adapters: dict[str, ChannelAdapter] = field(default_factory=dict)

    def register(self, adapter: ChannelAdapter) -> None:
        self.adapters[adapter.channel_kind] = adapter

    def get(self, channel_kind: str) -> ChannelAdapter | None:
        return self.adapters.get(channel_kind)


@dataclass
class ChannelEndpointRegistry:
    # ChannelRuntime owns the parent management layer for endpoint instances.
    # Endpoint nodes can report their own state, but attach/detach and top-level
    # enable/disable decisions are mediated here.
    endpoints: dict[str, ChannelEndpointBase] = field(default_factory=dict)

    def register(self, endpoint: ChannelEndpointBase) -> None:
        self.endpoints[endpoint.endpoint.endpoint_id] = endpoint

    def get(self, endpoint_id: str) -> ChannelEndpointBase | None:
        return self.endpoints.get(endpoint_id)

    def values(self) -> tuple[ChannelEndpointBase, ...]:
        return tuple(self.endpoints.values())


@dataclass
class ChannelRuntime(ChannelRuntimePort):
    adapter_registry: ChannelAdapterRegistry = field(default_factory=ChannelAdapterRegistry)
    endpoint_registry: ChannelEndpointRegistry = field(default_factory=ChannelEndpointRegistry)
    mailbox: Mailbox[EventEnvelope] = field(default_factory=Mailbox)
    outbox: deque[QueuedReply] = field(default_factory=deque)
    attachment_outbox: deque[QueuedAttachment] = field(default_factory=deque)
    status_outbox: deque[QueuedStatus] = field(default_factory=deque)
    stream_outbox: deque[QueuedStreamEvent] = field(default_factory=deque)
    on_ready: Callable[[], None] | None = None
    control_catalog_payload: dict[str, object] | None = None
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.mailbox.on_put = self._notify_ready

    def register_endpoint(self, endpoint: ChannelEndpointBase) -> None:
        endpoint.on_ready = self._notify_ready
        self.endpoint_registry.register(endpoint)

    async def start_async(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._started = True
        for endpoint in self.list_endpoints():
            starter = getattr(endpoint, "start_async", None)
            if callable(starter):
                await starter()

    async def stop_async(self) -> None:
        for endpoint in self.list_endpoints():
            stopper = getattr(endpoint, "stop_async", None)
            if callable(stopper):
                await stopper()
        self._started = False
        self._loop = None

    async def replace_endpoint_async(self, endpoint: ChannelEndpointBase) -> None:
        old_endpoint = self.get_endpoint(endpoint.endpoint.endpoint_id)
        if old_endpoint is not None and old_endpoint is not endpoint:
            stopper = getattr(old_endpoint, "stop_async", None)
            if callable(stopper):
                await stopper()
        self.register_endpoint(endpoint)
        if self._started:
            starter = getattr(endpoint, "start_async", None)
            if callable(starter):
                await starter()
            self._queue_cached_control_catalog(endpoint)

    def replace_endpoint(self, endpoint: ChannelEndpointBase, *, timeout_seconds: float = 10.0) -> None:
        async def _replace() -> None:
            await self.replace_endpoint_async(endpoint)

        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop is loop:
                raise RuntimeError("cannot synchronously reload a channel endpoint from the channel event loop")
            future = asyncio.run_coroutine_threadsafe(_replace(), loop)
            future.result(timeout=timeout_seconds)
            return
        asyncio.run(_replace())

    def get_endpoint(self, endpoint_id: str) -> ChannelEndpointBase | None:
        return self.endpoint_registry.get(endpoint_id)

    def list_endpoints(self) -> tuple[ChannelEndpointBase, ...]:
        return self.endpoint_registry.values()

    def enable_endpoint(self, endpoint_id: str) -> bool:
        endpoint = self.get_endpoint(endpoint_id)
        if endpoint is None:
            return False
        endpoint.enable()
        self._queue_cached_control_catalog(endpoint)
        return True

    def disable_endpoint(self, endpoint_id: str) -> bool:
        endpoint = self.get_endpoint(endpoint_id)
        if endpoint is None:
            return False
        endpoint.disable()
        return True

    def attach_endpoint(self, endpoint_id: str) -> bool:
        endpoint = self.get_endpoint(endpoint_id)
        if endpoint is None:
            return False
        endpoint.attach()
        self._queue_cached_control_catalog(endpoint)
        return True

    def detach_endpoint(self, endpoint_id: str) -> bool:
        endpoint = self.get_endpoint(endpoint_id)
        if endpoint is None:
            return False
        endpoint.detach()
        return True

    def sync_endpoints(self) -> None:
        # Concrete endpoints own transport-specific ingress/outbox flow. The
        # runtime aggregates those per-endpoint queues into the shared channel
        # mailbox that MainLoop polls.
        for endpoint in self.list_endpoints():
            for envelope in endpoint.poll():
                self.emit(envelope)
            endpoint.flush_status_outbox()
            for event in endpoint.flush_attachment_outbox():
                self.mailbox.put(event)
            for event in endpoint.flush_stream_outbox():
                self.mailbox.put(event)
            for event in endpoint.flush_outbox():
                self.mailbox.put(event)

    def emit(self, envelope: ChannelEnvelope) -> None:
        # Channel owns normalization. Everything PalCore sees after this point
        # is already canonical internal ingress.
        self.mailbox.put(
            EventEnvelope(
                event_kind=envelope.event.event_kind,
                source_kind=SourceKind.CHANNEL,
                payload=envelope,
                correlation_id=envelope.event.correlation_id or envelope.event.event_id,
            )
        )

    @property
    def inbox(self) -> tuple[EventEnvelope, ...]:
        return self.mailbox.peek_all()

    def queue_reply(self, envelope: ChannelEnvelope, text: str) -> str:
        # Outbox acceptance is the turn-facing completion point; actual delivery
        # is handled later by flush_outbox and surfaced as channel diagnostics.
        endpoint = self.get_endpoint(envelope.endpoint.endpoint_id)
        if endpoint is not None:
            return endpoint.queue_reply(text, response_handle=envelope.response_handle)
        reply_id = str(uuid4())
        self.outbox.append(
            QueuedReply(
                reply_id=reply_id,
                response_handle=envelope.response_handle,
                endpoint=envelope.endpoint,
                text=text,
            )
        )
        self._notify_ready()
        return reply_id

    def queue_stream_event(self, envelope: ChannelEnvelope, event: NormalizedLLMStreamEvent) -> str:
        endpoint = self.get_endpoint(envelope.endpoint.endpoint_id)
        if endpoint is not None:
            return endpoint.queue_stream_event(event, response_handle=envelope.response_handle)
        event_id = str(uuid4())
        self.stream_outbox.append(
            QueuedStreamEvent(
                event_id=event_id,
                response_handle=envelope.response_handle,
                endpoint=envelope.endpoint,
                event=event,
            )
        )
        self._notify_ready()
        return event_id

    def queue_attachment(self, envelope: ChannelEnvelope, attachment: AttachmentSpec) -> str:
        endpoint = self.get_endpoint(envelope.endpoint.endpoint_id)
        if endpoint is not None:
            return endpoint.queue_attachment(attachment, response_handle=envelope.response_handle)
        attachment_id = str(uuid4())
        self.attachment_outbox.append(
            QueuedAttachment(
                attachment_id=attachment_id,
                response_handle=envelope.response_handle,
                endpoint=envelope.endpoint,
                attachment=attachment,
            )
        )
        self._notify_ready()
        return attachment_id

    def abort_stream(self, response_handle, *, reason: str = "interrupted") -> None:
        for endpoint in self.list_endpoints():
            if endpoint.endpoint.endpoint_id == response_handle.endpoint_id:
                endpoint.abort_stream(response_handle, reason=reason)
                break
        if not self.stream_outbox:
            return
        remaining: deque[QueuedStreamEvent] = deque()
        while self.stream_outbox:
            queued = self.stream_outbox.popleft()
            same_endpoint = queued.response_handle.endpoint_id == response_handle.endpoint_id
            same_target = queued.response_handle.reply_target == response_handle.reply_target
            if same_endpoint and same_target:
                continue
            remaining.append(queued)
        self.stream_outbox = remaining

    def queue_status(
        self,
        envelope: ChannelEnvelope,
        kind: str,
        *,
        payload: dict[str, object] | None = None,
    ) -> str:
        endpoint = self.get_endpoint(envelope.endpoint.endpoint_id)
        if endpoint is not None:
            return endpoint.queue_status(
                kind,
                response_handle=envelope.response_handle,
                payload=dict(payload or {}),
            )
        status_id = str(uuid4())
        self.status_outbox.append(
            QueuedStatus(
                status_id=status_id,
                response_handle=envelope.response_handle,
                endpoint=envelope.endpoint,
                kind=str(kind),
                payload=dict(payload or {}),
            )
        )
        self._notify_ready()
        return status_id

    def queue_endpoint_status(
        self,
        endpoint_id: str,
        kind: str,
        *,
        payload: dict[str, object] | None = None,
        reply_target: dict[str, object] | None = None,
    ) -> str | None:
        if str(kind) == "control_catalog":
            self.control_catalog_payload = dict(payload or {})
        endpoint = self.get_endpoint(endpoint_id)
        if endpoint is None:
            return None
        target = dict(reply_target or endpoint.derive_default_reply_target())
        return endpoint.queue_status(
            kind,
            response_handle=endpoint.build_response_handle(reply_target=target),
            payload=dict(payload or {}),
        )

    def _queue_cached_control_catalog(self, endpoint: ChannelEndpointBase) -> None:
        payload = self.control_catalog_payload
        if not payload or not endpoint.attached or not endpoint.enabled:
            return
        endpoint.queue_status(
            "control_catalog",
            response_handle=endpoint.build_response_handle(reply_target=endpoint.derive_default_reply_target()),
            payload=dict(payload),
        )

    def flush_outbox(self) -> None:
        self.sync_endpoints()
        if self.stream_outbox:
            self.stream_outbox.clear()
        if self.status_outbox:
            self.status_outbox.clear()
        if self.attachment_outbox:
            pending_attachments = list(self.attachment_outbox)
            self.attachment_outbox.clear()
            for item in pending_attachments:
                self.mailbox.put(
                    EventEnvelope(
                        event_kind=EventKind.REPLY_FAILED,
                        source_kind=SourceKind.CHANNEL,
                        payload={
                            "reply_id": item.attachment_id,
                            "endpoint_id": item.endpoint.endpoint_id,
                            "reason": "endpoint_unavailable",
                        },
                    )
                )
        if not self.outbox:
            return
        pending = list(self.outbox)
        self.outbox.clear()
        for item in pending:
            adapter = self.adapter_registry.get(item.endpoint.channel_kind)
            if adapter is None:
                self.outbox.append(
                    QueuedReply(
                        reply_id=item.reply_id,
                        response_handle=item.response_handle,
                        endpoint=item.endpoint,
                        text=item.text,
                        attempts=item.attempts + 1,
                    )
                )
                self.mailbox.put(
                    EventEnvelope(
                        event_kind=EventKind.REPLY_FAILED,
                        source_kind=SourceKind.CHANNEL,
                        payload={"reply_id": item.reply_id, "endpoint_id": item.endpoint.endpoint_id, "reason": "adapter_unavailable"},
                    )
                )
                continue
            try:
                adapter.send(item.response_handle, item.text)
            except Exception as exc:
                self.outbox.append(
                    QueuedReply(
                        reply_id=item.reply_id,
                        response_handle=item.response_handle,
                        endpoint=item.endpoint,
                        text=item.text,
                        attempts=item.attempts + 1,
                    )
                )
                self.mailbox.put(
                    EventEnvelope(
                        event_kind=EventKind.REPLY_FAILED,
                        source_kind=SourceKind.CHANNEL,
                        payload={"reply_id": item.reply_id, "endpoint_id": item.endpoint.endpoint_id, "reason": str(exc)},
                    )
                )
                continue
            self.mailbox.put(
                EventEnvelope(
                    event_kind=EventKind.REPLY_DELIVERED,
                    source_kind=SourceKind.CHANNEL,
                    payload={"reply_id": item.reply_id, "endpoint_id": item.endpoint.endpoint_id},
                )
            )

    def _notify_ready(self) -> None:
        if self.on_ready is not None:
            self.on_ready()
