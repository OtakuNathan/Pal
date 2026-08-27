from __future__ import annotations

import asyncio
import inspect
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal
from uuid import uuid4

from pal.channel.contracts import (
    ChannelAdapter,
    ChannelDeliveryError,
    ChannelEnvelope,
    ChannelMessage,
    ChannelMessageReceipt,
    ChannelRuntimePort,
    ChannelStreamUpdate,
    QueuedAttachment,
    QueuedReply,
    QueuedStatus,
    QueuedStreamUpdate,
    TurnDeliveryBinding,
)
from pal.channel.channel_endpoint_queue_base import (
    TRANSIENT_REPLY_FAILURE_REPORT_INTERVAL_SECONDS,
    ChannelEndpointBase,
)
from pal.core.mailbox import Mailbox
from pal.foundation import AttachmentSpec, EventEnvelope
from pal.shared import ChannelStreamUpdateKind, EventKind, SourceKind
from pal.channel.ingress import ChannelIngressCompiler


ENDPOINT_RELOAD_BUFFER_MAX_ITEMS = 1024
ENDPOINT_RELOAD_BUFFER_MAX_TEXT_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class BufferedChannelDelivery:
    sequence: int
    delivery_kind: Literal["reply", "stream", "status", "attachment"]
    delivery_id: str
    item: QueuedReply | QueuedStreamUpdate | QueuedStatus | QueuedAttachment
    queued_at: float = field(default_factory=time.monotonic)

    @property
    def text_bytes(self) -> int:
        item = self.item
        if isinstance(item, QueuedReply):
            return len(item.text.encode("utf-8"))
        if isinstance(item, QueuedStreamUpdate):
            update = item.update
            return len(update.text.encode("utf-8")) + len(update.reasoning_text.encode("utf-8"))
        if isinstance(item, QueuedStatus):
            return len(str(item.payload).encode("utf-8"))
        return 0


@dataclass
class ChannelEndpointSlot:
    """Stable delivery ownership that survives endpoint generation swaps."""

    endpoint_id: str
    provider_id: str = ""
    state: str = "active"
    reload_epoch: int = 0
    next_sequence: int = 1
    buffer: deque[BufferedChannelDelivery] = field(default_factory=deque)
    buffered_text_bytes: int = 0
    overflowed: bool = False
    last_reload_error: str = ""

    def begin_reload(self, *, provider_id: str = "") -> int:
        if self.state != "reloading":
            self.reload_epoch += 1
        self.provider_id = str(provider_id or self.provider_id)
        self.state = "reloading"
        self.overflowed = (
            len(self.buffer) > ENDPOINT_RELOAD_BUFFER_MAX_ITEMS
            or self.buffered_text_bytes > ENDPOINT_RELOAD_BUFFER_MAX_TEXT_BYTES
        )
        self.last_reload_error = ""
        return self.reload_epoch

    def append(
        self,
        delivery_kind: Literal["reply", "stream", "status", "attachment"],
        delivery_id: str,
        item: QueuedReply | QueuedStreamUpdate | QueuedStatus | QueuedAttachment,
    ) -> None:
        delivery = BufferedChannelDelivery(
            sequence=self.next_sequence,
            delivery_kind=delivery_kind,
            delivery_id=delivery_id,
            item=item,
        )
        self.next_sequence += 1
        if self._coalesce(delivery):
            return
        self.buffer.append(delivery)
        self.buffered_text_bytes += delivery.text_bytes
        if (
            len(self.buffer) > ENDPOINT_RELOAD_BUFFER_MAX_ITEMS
            or self.buffered_text_bytes > ENDPOINT_RELOAD_BUFFER_MAX_TEXT_BYTES
        ):
            self.overflowed = True

    def popleft(self) -> BufferedChannelDelivery:
        delivery = self.buffer.popleft()
        self.buffered_text_bytes = max(0, self.buffered_text_bytes - delivery.text_bytes)
        return delivery

    def _coalesce(self, delivery: BufferedChannelDelivery) -> bool:
        if not self.buffer:
            return False
        previous = self.buffer[-1]
        if delivery.delivery_kind == "stream" and previous.delivery_kind == "stream":
            prior = previous.item
            current = delivery.item
            if (
                isinstance(prior, QueuedStreamUpdate)
                and isinstance(current, QueuedStreamUpdate)
                and prior.response_handle == current.response_handle
                and prior.update.kind == ChannelStreamUpdateKind.TEXT_DELTA
                and current.update.kind == ChannelStreamUpdateKind.TEXT_DELTA
                and prior.update.tool_call is None
                and current.update.tool_call is None
            ):
                combined = QueuedStreamUpdate(
                    update_id=prior.update_id,
                    response_handle=prior.response_handle,
                    endpoint=prior.endpoint,
                    update=ChannelStreamUpdate(
                        kind=ChannelStreamUpdateKind.TEXT_DELTA,
                        text=f"{prior.update.text}{current.update.text}",
                        reasoning_text=f"{prior.update.reasoning_text}{current.update.reasoning_text}",
                    ),
                    attempts=max(prior.attempts, current.attempts),
                )
                self.buffered_text_bytes = max(
                    0,
                    self.buffered_text_bytes - previous.text_bytes,
                )
                replacement = BufferedChannelDelivery(
                    sequence=previous.sequence,
                    delivery_kind="stream",
                    delivery_id=prior.update_id,
                    item=combined,
                    queued_at=previous.queued_at,
                )
                self.buffer[-1] = replacement
                self.buffered_text_bytes += replacement.text_bytes
                if self.buffered_text_bytes > ENDPOINT_RELOAD_BUFFER_MAX_TEXT_BYTES:
                    self.overflowed = True
                return True
        if delivery.delivery_kind == "status" and previous.delivery_kind == "status":
            prior = previous.item
            current = delivery.item
            if (
                isinstance(prior, QueuedStatus)
                and isinstance(current, QueuedStatus)
                and prior.response_handle == current.response_handle
                and prior.kind == current.kind
                and current.kind in {"typing_start", "working_stop", "control_catalog", "llm_waiting"}
            ):
                self.buffered_text_bytes = max(
                    0,
                    self.buffered_text_bytes - previous.text_bytes,
                )
                replacement = BufferedChannelDelivery(
                    sequence=previous.sequence,
                    delivery_kind="status",
                    delivery_id=current.status_id,
                    item=current,
                    queued_at=previous.queued_at,
                )
                self.buffer[-1] = replacement
                self.buffered_text_bytes += replacement.text_bytes
                if self.buffered_text_bytes > ENDPOINT_RELOAD_BUFFER_MAX_TEXT_BYTES:
                    self.overflowed = True
                return True
        return False


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

    def unregister(self, endpoint_id: str) -> ChannelEndpointBase | None:
        return self.endpoints.pop(endpoint_id, None)

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
    stream_update_outbox: deque[QueuedStreamUpdate] = field(default_factory=deque)
    delivery_slots: dict[str, ChannelEndpointSlot] = field(default_factory=dict)
    on_ready: Callable[[], None] | None = None
    control_catalog_payload: dict[str, object] | None = None
    ingress_compiler: ChannelIngressCompiler | None = None
    _reported_outbox_failures: dict[str, tuple[str, float]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.mailbox.on_put = self._notify_ready

    def register_endpoint(self, endpoint: ChannelEndpointBase) -> None:
        self._bind_endpoint_ready(endpoint)
        slot = self._slot(endpoint.endpoint.endpoint_id)
        self.endpoint_registry.register(endpoint)
        if slot.state == "detached":
            slot.state = "active"
            slot.last_reload_error = ""

    def _bind_endpoint_ready(self, endpoint: ChannelEndpointBase) -> None:
        endpoint_id = endpoint.endpoint.endpoint_id
        endpoint.on_ready = lambda: self._notify_endpoint_ready(endpoint_id)

    def _slot(self, endpoint_id: str, *, provider_id: str = "") -> ChannelEndpointSlot:
        normalized = str(endpoint_id or "").strip()
        slot = self.delivery_slots.get(normalized)
        if slot is None:
            slot = ChannelEndpointSlot(endpoint_id=normalized, provider_id=str(provider_id or ""))
            self.delivery_slots[normalized] = slot
        elif provider_id:
            slot.provider_id = str(provider_id)
        return slot

    def _call_on_owner_loop(
        self,
        callback: Callable[[], Any],
        *,
        timeout_seconds: float = 10.0,
    ) -> Any:
        loop = self._loop
        if loop is None or not loop.is_running():
            return callback()
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            return callback()

        async def _invoke() -> Any:
            return callback()

        future = asyncio.run_coroutine_threadsafe(_invoke(), loop)
        return future.result(timeout=timeout_seconds)

    def begin_endpoint_reload(self, endpoint_id: str, *, provider_id: str = "") -> int:
        return int(
            self._call_on_owner_loop(
                lambda: self._begin_endpoint_reload(endpoint_id, provider_id=provider_id)
            )
        )

    def _begin_endpoint_reload(self, endpoint_id: str, *, provider_id: str = "") -> int:
        slot = self._slot(endpoint_id, provider_id=provider_id)
        epoch = slot.begin_reload(provider_id=provider_id)
        endpoint = self.get_endpoint(endpoint_id)
        if endpoint is not None:
            self._absorb_endpoint_outboxes(slot, endpoint)
        return epoch

    def bind_endpoint_provider(self, endpoint_id: str, provider_id: str) -> None:
        self._call_on_owner_loop(
            lambda: self._slot(endpoint_id, provider_id=provider_id)
        )

    def begin_provider_reload(self, endpoint_ids: tuple[str, ...] | list[str], *, provider_id: str) -> dict[str, int]:
        normalized_ids = tuple(endpoint_ids)
        return dict(
            self._call_on_owner_loop(
                lambda: {
                    endpoint_id: self._begin_endpoint_reload(
                        endpoint_id,
                        provider_id=provider_id,
                    )
                    for endpoint_id in normalized_ids
                }
            )
        )

    def complete_endpoint_reload(self, endpoint_id: str) -> None:
        self._call_on_owner_loop(lambda: self._complete_endpoint_reload(endpoint_id))

    def _complete_endpoint_reload(self, endpoint_id: str) -> None:
        slot = self._slot(endpoint_id)
        slot.state = "draining"
        slot.last_reload_error = ""
        self._flush_slot(slot)

    def complete_provider_reload(self, endpoint_ids: tuple[str, ...] | list[str]) -> None:
        normalized_ids = tuple(endpoint_ids)

        def _complete_all() -> None:
            for endpoint_id in normalized_ids:
                self._complete_endpoint_reload(endpoint_id)

        self._call_on_owner_loop(_complete_all)

    def fail_endpoint_reload(self, endpoint_id: str, error: str) -> None:
        def _fail() -> None:
            slot = self._slot(endpoint_id)
            slot.last_reload_error = str(error or "provider reload failed")
            slot.state = "degraded"

        self._call_on_owner_loop(_fail)

    def mark_endpoint_detached(self, endpoint_id: str, *, reason: str = "") -> None:
        def _mark_detached() -> None:
            slot = self._slot(endpoint_id)
            slot.state = "detached"
            slot.last_reload_error = str(reason or "")

        self._call_on_owner_loop(_mark_detached)

    def inspect_delivery_slot(self, endpoint_id: str) -> dict[str, Any]:
        slot = self.delivery_slots.get(str(endpoint_id or "").strip())
        if slot is None:
            return {
                "lifecycle_state": "missing",
                "reload_epoch": 0,
                "buffered_delivery_count": 0,
                "buffered_text_bytes": 0,
                "buffer_overflowed": False,
                "oldest_buffered_seconds": 0.0,
                "last_reload_error": "",
            }
        oldest_age = (
            max(0.0, time.monotonic() - slot.buffer[0].queued_at)
            if slot.buffer
            else 0.0
        )
        return {
            "lifecycle_state": slot.state,
            "reload_epoch": slot.reload_epoch,
            "provider_id": slot.provider_id,
            "buffered_delivery_count": len(slot.buffer),
            "buffered_text_bytes": slot.buffered_text_bytes,
            "buffer_overflowed": slot.overflowed,
            "oldest_buffered_seconds": oldest_age,
            "last_reload_error": slot.last_reload_error,
        }

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

    async def replace_endpoint_async(
        self,
        endpoint: ChannelEndpointBase,
        *,
        manage_reload: bool = True,
    ) -> None:
        old_endpoint = self.get_endpoint(endpoint.endpoint.endpoint_id)
        endpoint_id = endpoint.endpoint.endpoint_id
        if old_endpoint is endpoint:
            if self._started:
                self._queue_cached_control_catalog(endpoint)
            return
        slot = self._slot(endpoint_id)
        owns_reload = manage_reload and slot.state != "reloading"
        if owns_reload:
            self.begin_endpoint_reload(endpoint_id, provider_id=slot.provider_id)
        self._bind_endpoint_ready(endpoint)
        preparer = getattr(endpoint, "prepare_replacement", None)
        if old_endpoint is not None and callable(preparer):
            preparation = preparer(old_endpoint)
            if inspect.isawaitable(preparation):
                await preparation
        if not self._started:
            _transfer_endpoint_runtime_state(old_endpoint, endpoint)
            self.endpoint_registry.register(endpoint)
            if owns_reload:
                self.complete_endpoint_reload(endpoint_id)
            return

        old_stopper = getattr(old_endpoint, "stop_async", None)
        if old_endpoint is not None and callable(old_stopper):
            await old_stopper()
            if slot.state == "reloading":
                # A transport may return scheduled-but-unconfirmed work to its
                # local outboxes while stopping. Pull it behind the same fence
                # before publishing the replacement so later arrivals cannot
                # overtake it.
                self._absorb_endpoint_outboxes(slot, old_endpoint)
        try:
            starter = getattr(endpoint, "start_async", None)
            if callable(starter):
                await starter()
            validator = getattr(endpoint, "validate_replacement_startup", None)
            if callable(validator):
                validation = validator()
                if inspect.isawaitable(validation):
                    await validation
        except Exception:
            failed_stopper = getattr(endpoint, "stop_async", None)
            if callable(failed_stopper):
                try:
                    await failed_stopper()
                except Exception:
                    pass
            old_starter = getattr(old_endpoint, "start_async", None)
            if old_endpoint is not None and callable(old_starter):
                await old_starter()
                self._bind_endpoint_ready(old_endpoint)
                self.endpoint_registry.register(old_endpoint)
            if owns_reload:
                slot.last_reload_error = "replacement startup failed"
                self.complete_endpoint_reload(endpoint_id)
            raise
        _transfer_endpoint_runtime_state(old_endpoint, endpoint)
        self.endpoint_registry.register(endpoint)
        self._queue_cached_control_catalog(endpoint)
        if owns_reload:
            self.complete_endpoint_reload(endpoint_id)

    def replace_endpoint(
        self,
        endpoint: ChannelEndpointBase,
        *,
        timeout_seconds: float = 10.0,
        manage_reload: bool = True,
    ) -> None:
        async def _replace() -> None:
            await self.replace_endpoint_async(endpoint, manage_reload=manage_reload)

        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop is loop:
                raise RuntimeError("replace_endpoint cannot block its owner event loop; use replace_endpoint_async")
            future = asyncio.run_coroutine_threadsafe(_replace(), loop)
            future.result(timeout=timeout_seconds)
            return
        if not self._started:
            old_endpoint = self.get_endpoint(endpoint.endpoint.endpoint_id)
            slot = self._slot(endpoint.endpoint.endpoint_id)
            owns_reload = manage_reload and slot.state != "reloading"
            if owns_reload:
                self.begin_endpoint_reload(endpoint.endpoint.endpoint_id)
            preparer = getattr(endpoint, "prepare_replacement", None)
            if old_endpoint is not None and callable(preparer):
                preparation = preparer(old_endpoint)
                if inspect.isawaitable(preparation):
                    raise RuntimeError(
                        "async endpoint replacement preparation requires replace_endpoint_async"
                    )
            _transfer_endpoint_runtime_state(old_endpoint, endpoint)
            self.register_endpoint(endpoint)
            if owns_reload:
                self.complete_endpoint_reload(endpoint.endpoint.endpoint_id)
            return
        asyncio.run(_replace())

    async def remove_endpoint_async(self, endpoint_id: str) -> bool:
        endpoint = self.endpoint_registry.get(endpoint_id)
        if endpoint is None:
            return False
        stopper = getattr(endpoint, "stop_async", None)
        if callable(stopper):
            await stopper()
        self.endpoint_registry.unregister(endpoint_id)
        slot = self.delivery_slots.get(endpoint_id)
        if slot is not None and slot.state != "reloading":
            slot.state = "detached"
        return True

    def remove_endpoint(self, endpoint_id: str, *, timeout_seconds: float = 10.0) -> bool:
        async def _remove() -> bool:
            return await self.remove_endpoint_async(endpoint_id)

        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop is loop:
                raise RuntimeError("remove_endpoint cannot block its owner event loop; use remove_endpoint_async")
            future = asyncio.run_coroutine_threadsafe(_remove(), loop)
            return bool(future.result(timeout=timeout_seconds))
        if not self._started:
            removed = self.endpoint_registry.unregister(endpoint_id) is not None
            slot = self.delivery_slots.get(endpoint_id)
            if removed and slot is not None and slot.state != "reloading":
                slot.state = "detached"
            return removed
        return bool(asyncio.run(_remove()))

    def get_endpoint(self, endpoint_id: str) -> ChannelEndpointBase | None:
        return self.endpoint_registry.get(endpoint_id)

    def list_endpoints(self) -> tuple[ChannelEndpointBase, ...]:
        return self.endpoint_registry.values()

    async def send_message(self, endpoint_id: str, message: str) -> ChannelMessageReceipt:
        endpoint = self.get_endpoint(endpoint_id)
        if endpoint is None:
            raise ChannelDeliveryError(
                f"channel endpoint {endpoint_id!r} was not found",
                permanent=True,
                reason="channel_not_found",
            )
        if not endpoint.attached:
            raise ChannelDeliveryError(
                f"channel endpoint {endpoint_id!r} is detached",
                permanent=True,
                reason="channel_detached",
            )
        if not endpoint.enabled:
            raise ChannelDeliveryError(
                f"channel endpoint {endpoint_id!r} is disabled",
                permanent=True,
                reason="channel_disabled",
            )
        return await endpoint.send_message(message)

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
        for endpoint in self.list_endpoints():
            endpoint_id = endpoint.endpoint.endpoint_id
            slot = self._slot(endpoint_id)
            for envelope in endpoint.poll():
                self.emit(envelope)
            if slot.state == "reloading":
                self._absorb_endpoint_outboxes(slot, endpoint)
                continue
            if slot.buffer:
                self._flush_slot(slot)
                if slot.buffer:
                    continue
            endpoint.flush_status_outbox()
            for event in endpoint.flush_attachment_outbox():
                self.mailbox.put(event)
            for event in endpoint.flush_stream_update_outbox():
                self.mailbox.put(event)
            for event in endpoint.flush_outbox():
                self.mailbox.put(event)

    def emit(self, envelope: ChannelEnvelope) -> None:
        # Channel owns normalization. Everything PalCore sees after this point
        # is already canonical internal ingress.
        if self.ingress_compiler is not None:
            envelope = self.ingress_compiler.compile(envelope)
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

    def supports_stream_delivery(self, envelope: TurnDeliveryBinding) -> bool:
        endpoint = self.get_endpoint(envelope.endpoint.endpoint_id)
        if endpoint is None:
            return False
        supports = getattr(endpoint, "supports_stream_delivery", None)
        return bool(supports()) if callable(supports) else False

    def queue_reply(self, envelope: TurnDeliveryBinding, message: ChannelMessage | str) -> str:
        # Outbox acceptance is the turn-facing completion point; actual delivery
        # is handled later by flush_outbox and surfaced as channel diagnostics.
        endpoint_id = envelope.endpoint.endpoint_id
        endpoint = self.get_endpoint(endpoint_id)
        slot = self._slot(endpoint_id)
        if endpoint is not None and slot.state == "active":
            return endpoint.queue_reply(message, response_handle=envelope.response_handle)
        normalized = message if isinstance(message, ChannelMessage) else ChannelMessage(text=str(message or ""))
        reply_id = str(uuid4())
        item = QueuedReply(
            reply_id=reply_id,
            response_handle=envelope.response_handle,
            endpoint=envelope.endpoint,
            text=normalized.text,
            tag=normalized.tag,
            payload=dict(normalized.payload),
        )
        if slot.state == "active":
            self.outbox.append(item)
            self._notify_ready()
            return reply_id
        slot.append("reply", reply_id, item)
        self._notify_ready()
        return reply_id

    def queue_stream_update(self, envelope: TurnDeliveryBinding, update: ChannelStreamUpdate) -> str:
        endpoint_id = envelope.endpoint.endpoint_id
        endpoint = self.get_endpoint(endpoint_id)
        slot = self._slot(endpoint_id)
        if endpoint is not None and slot.state == "active":
            return endpoint.queue_stream_update(update, response_handle=envelope.response_handle)
        update_id = str(uuid4())
        item = QueuedStreamUpdate(
            update_id=update_id,
            response_handle=envelope.response_handle,
            endpoint=envelope.endpoint,
            update=update,
        )
        if slot.state == "active":
            self.stream_update_outbox.append(item)
            self._notify_ready()
            return update_id
        slot.append("stream", update_id, item)
        self._notify_ready()
        return update_id

    def queue_attachment(self, envelope: TurnDeliveryBinding, attachment: AttachmentSpec) -> str:
        endpoint_id = envelope.endpoint.endpoint_id
        endpoint = self.get_endpoint(endpoint_id)
        slot = self._slot(endpoint_id)
        if endpoint is not None and slot.state == "active":
            return endpoint.queue_attachment(attachment, response_handle=envelope.response_handle)
        attachment_id = str(uuid4())
        item = QueuedAttachment(
            attachment_id=attachment_id,
            response_handle=envelope.response_handle,
            endpoint=envelope.endpoint,
            attachment=attachment,
        )
        if slot.state == "active":
            self.attachment_outbox.append(item)
            self._notify_ready()
            return attachment_id
        slot.append("attachment", attachment_id, item)
        self._notify_ready()
        return attachment_id

    def abort_stream(self, response_handle, *, reason: str = "interrupted") -> None:
        for endpoint in self.list_endpoints():
            if endpoint.endpoint.endpoint_id == response_handle.endpoint_id:
                endpoint.abort_stream(response_handle, reason=reason)
                break
        slot = self.delivery_slots.get(str(response_handle.endpoint_id or ""))
        if slot is not None and slot.buffer:
            retained: deque[BufferedChannelDelivery] = deque()
            while slot.buffer:
                delivery = slot.popleft()
                item = delivery.item
                if (
                    delivery.delivery_kind == "stream"
                    and isinstance(item, QueuedStreamUpdate)
                    and item.response_handle == response_handle
                ):
                    continue
                retained.append(delivery)
                slot.buffered_text_bytes += delivery.text_bytes
            slot.buffer = retained
        if not self.stream_update_outbox:
            return
        remaining: deque[QueuedStreamUpdate] = deque()
        while self.stream_update_outbox:
            queued = self.stream_update_outbox.popleft()
            same_endpoint = queued.response_handle.endpoint_id == response_handle.endpoint_id
            same_target = queued.response_handle.reply_target == response_handle.reply_target
            if same_endpoint and same_target:
                continue
            remaining.append(queued)
        self.stream_update_outbox = remaining

    def queue_status(
        self,
        envelope: TurnDeliveryBinding,
        kind: str,
        *,
        payload: dict[str, object] | None = None,
    ) -> str:
        endpoint_id = envelope.endpoint.endpoint_id
        endpoint = self.get_endpoint(endpoint_id)
        slot = self._slot(endpoint_id)
        if endpoint is not None and slot.state == "active":
            return endpoint.queue_status(
                kind,
                response_handle=envelope.response_handle,
                payload=dict(payload or {}),
            )
        status_id = str(uuid4())
        item = QueuedStatus(
            status_id=status_id,
            response_handle=envelope.response_handle,
            endpoint=envelope.endpoint,
            kind=str(kind),
            payload=dict(payload or {}),
        )
        if slot.state == "active":
            self.status_outbox.append(item)
            self._notify_ready()
            return status_id
        slot.append("status", status_id, item)
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
        handle = endpoint.build_response_handle(reply_target=target)
        slot = self._slot(endpoint_id)
        if slot.state == "active":
            return endpoint.queue_status(kind, response_handle=handle, payload=dict(payload or {}))
        status_id = str(uuid4())
        item = QueuedStatus(
            status_id=status_id,
            response_handle=handle,
            endpoint=endpoint.endpoint,
            kind=str(kind),
            payload=dict(payload or {}),
        )
        slot.append("status", status_id, item)
        self._notify_ready()
        return status_id

    def flush_endpoint_status(self, endpoint_id: str) -> bool:
        endpoint = self.get_endpoint(endpoint_id)
        if endpoint is None:
            return False
        slot = self._slot(endpoint_id)
        if slot.state == "active":
            endpoint.flush_status_outbox()
        else:
            self._absorb_endpoint_outboxes(slot, endpoint)
        return True

    def _queue_cached_control_catalog(self, endpoint: ChannelEndpointBase) -> None:
        payload = self.control_catalog_payload
        if not payload or not endpoint.attached or not endpoint.enabled:
            return
        endpoint_id = endpoint.endpoint.endpoint_id
        handle = endpoint.build_response_handle(reply_target=endpoint.derive_default_reply_target())
        slot = self._slot(endpoint_id)
        if slot.state == "active":
            endpoint.queue_status("control_catalog", response_handle=handle, payload=dict(payload))
            return
        status_id = str(uuid4())
        slot.append(
            "status",
            status_id,
            QueuedStatus(
                status_id=status_id,
                response_handle=handle,
                endpoint=endpoint.endpoint,
                kind="control_catalog",
                payload=dict(payload),
            ),
        )

    def _absorb_endpoint_outboxes(
        self,
        slot: ChannelEndpointSlot,
        endpoint: ChannelEndpointBase,
    ) -> None:
        # Match the legacy sync order for work that predates the reload fence.
        for item in tuple(endpoint.status_outbox):
            slot.append("status", item.status_id, item)
        endpoint.status_outbox.clear()
        for item in tuple(endpoint.attachment_outbox):
            slot.append("attachment", item.attachment_id, item)
        endpoint.attachment_outbox.clear()
        for item in tuple(endpoint.stream_update_outbox):
            slot.append("stream", item.update_id, item)
        endpoint.stream_update_outbox.clear()
        for item in tuple(endpoint.outbox):
            slot.append("reply", item.reply_id, item)
        endpoint.outbox.clear()

    def _flush_slot(self, slot: ChannelEndpointSlot) -> None:
        if slot.state in {"reloading", "detached", "degraded"}:
            return
        endpoint = self.get_endpoint(slot.endpoint_id)
        if endpoint is None or not endpoint.attached or not endpoint.enabled:
            return
        while slot.buffer:
            delivery = slot.buffer[0]
            try:
                confirmed = self._deliver_buffered(endpoint, delivery)
            except Exception as exc:
                slot.last_reload_error = str(exc)
                permanent = isinstance(exc, ChannelDeliveryError) and bool(
                    getattr(exc, "permanent", False)
                )
                # A generation swap owns this gap. Keep the head item and wait
                # for the replacement transport's ready notification.
                if slot.state == "draining" and not permanent:
                    return
                self._emit_delivery_failure(
                    slot,
                    delivery,
                    exc,
                    permanent=permanent,
                    expected_lifecycle=False,
                )
                if permanent:
                    slot.popleft()
                    continue
                return
            slot.popleft()
            slot.last_reload_error = ""
            if confirmed and delivery.delivery_kind != "status":
                self.mailbox.put(
                    EventEnvelope(
                        event_kind=EventKind.REPLY_DELIVERED,
                        source_kind=SourceKind.CHANNEL,
                        payload={
                            "reply_id": delivery.delivery_id,
                            "delivery_kind": delivery.delivery_kind,
                            "endpoint_id": slot.endpoint_id,
                            "provider_id": slot.provider_id,
                            "reload_epoch": slot.reload_epoch,
                        },
                    )
                )
        if self._replacement_delivery_ready(endpoint):
            slot.state = "active"

    def _deliver_buffered(
        self,
        endpoint: ChannelEndpointBase,
        delivery: BufferedChannelDelivery,
    ) -> bool:
        item = delivery.item
        if isinstance(item, QueuedReply):
            prepared = endpoint.prepare_final_reply(item.response_handle, item.text)
            if prepared is None:
                return False
            endpoint.outbox.append(replace(item, endpoint=endpoint.endpoint, text=prepared))
            events = endpoint.flush_outbox()
            return self._accept_endpoint_flush_result(
                endpoint,
                delivery,
                endpoint.outbox,
                events,
            )
        if isinstance(item, QueuedStreamUpdate):
            endpoint.stream_update_outbox.append(replace(item, endpoint=endpoint.endpoint))
            events = endpoint.flush_stream_update_outbox()
            return self._accept_endpoint_flush_result(
                endpoint,
                delivery,
                endpoint.stream_update_outbox,
                events,
            )
        if isinstance(item, QueuedStatus):
            # Status is explicitly ephemeral and may be coalesced. Its legacy
            # endpoint contract has no retry acknowledgement surface.
            endpoint.send_status(item.response_handle, item.kind, dict(item.payload))
            return False
        endpoint.attachment_outbox.append(replace(item, endpoint=endpoint.endpoint))
        events = endpoint.flush_attachment_outbox()
        return self._accept_endpoint_flush_result(
            endpoint,
            delivery,
            endpoint.attachment_outbox,
            events,
        )

    def _accept_endpoint_flush_result(
        self,
        endpoint: ChannelEndpointBase,
        delivery: BufferedChannelDelivery,
        outbox: deque[Any],
        events: list[EventEnvelope],
    ) -> bool:
        retained = next(
            (
                item
                for item in outbox
                if _queued_delivery_id(item) == delivery.delivery_id
            ),
            None,
        )
        if retained is not None:
            outbox.remove(retained)
        failure = next(
            (
                event
                for event in events
                if event.event_kind == EventKind.REPLY_FAILED
                and str(event.payload.get("reply_id") or "") == delivery.delivery_id
            ),
            None,
        )
        for event in events:
            if str(event.payload.get("reply_id") or "") != delivery.delivery_id:
                self.mailbox.put(event)
        if failure is not None or retained is not None:
            payload = failure.payload if failure is not None else {}
            raise ChannelDeliveryError(
                str(payload.get("reason") or endpoint.last_delivery_error or "delivery deferred"),
                permanent=bool(payload.get("permanent", False)),
                reason=str(payload.get("reason_code") or "delivery_deferred"),
            )
        return any(
            event.event_kind == EventKind.REPLY_DELIVERED
            and str(event.payload.get("reply_id") or "") == delivery.delivery_id
            for event in events
        )

    @staticmethod
    def _replacement_delivery_ready(endpoint: ChannelEndpointBase) -> bool:
        checker = getattr(endpoint, "replacement_delivery_ready", None)
        if callable(checker):
            return bool(checker())
        if bool(getattr(endpoint, "_allow_single_session_rebind", False)):
            sessions = getattr(endpoint, "sessions", None)
            adopter = getattr(endpoint, "_adopt_single_replacement_session", None)
            if isinstance(sessions, dict) and len(sessions) == 1 and callable(adopter):
                adopter(next(iter(sessions)))
            return not bool(getattr(endpoint, "_allow_single_session_rebind", False))
        return True

    def _emit_delivery_failure(
        self,
        slot: ChannelEndpointSlot,
        delivery: BufferedChannelDelivery,
        reason: Exception | str,
        *,
        permanent: bool,
        expected_lifecycle: bool,
    ) -> None:
        endpoint = self.get_endpoint(slot.endpoint_id)
        self.mailbox.put(
            EventEnvelope(
                event_kind=EventKind.REPLY_FAILED,
                source_kind=SourceKind.CHANNEL,
                payload={
                    "reply_id": delivery.delivery_id,
                    "delivery_kind": delivery.delivery_kind,
                    "endpoint_id": slot.endpoint_id,
                    "channel_kind": (
                        endpoint.endpoint.channel_kind if endpoint is not None else ""
                    ),
                    "provider_id": slot.provider_id,
                    "reload_epoch": slot.reload_epoch,
                    "reason": str(reason or "delivery_failed"),
                    "reason_code": getattr(reason, "reason", "delivery_failed"),
                    "permanent": permanent,
                    "transient": not permanent,
                    "expected_lifecycle": expected_lifecycle,
                },
            )
        )

    def flush_outbox(self) -> None:
        self.sync_endpoints()
        if self.stream_update_outbox:
            self.stream_update_outbox.clear()
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
                            "channel_kind": item.endpoint.channel_kind,
                            "reason": "endpoint_unavailable",
                        },
                    )
                )
        if not self.outbox:
            return
        pending = list(self.outbox)
        self.outbox.clear()
        for item in pending:
            endpoint = self.get_endpoint(item.endpoint.endpoint_id)
            if endpoint is not None:
                # Replies accepted while a provider generation was between
                # unload and restore belong to the endpoint queue once it is
                # available again.  The legacy adapter registry cannot deliver
                # provider-owned endpoints.
                endpoint.queue_reply(
                    item.message,
                    response_handle=item.response_handle,
                    reply_id=item.reply_id,
                )
                self._reported_outbox_failures.pop(item.reply_id, None)
                continue
            adapter = self.adapter_registry.get(item.endpoint.channel_kind)
            if adapter is None:
                reason = "adapter_unavailable"
                self.outbox.append(
                    QueuedReply(
                        reply_id=item.reply_id,
                        response_handle=item.response_handle,
                        endpoint=item.endpoint,
                        text=item.text,
                        tag=item.tag,
                        payload=dict(item.payload),
                        attempts=item.attempts + 1,
                    )
                )
                self._report_outbox_failure_once(item, reason)
                continue
            try:
                adapter.send(item.response_handle, item.text)
            except Exception as exc:
                reason = str(exc)
                permanent = isinstance(exc, ChannelDeliveryError) and bool(
                    getattr(exc, "permanent", False)
                )
                if not permanent:
                    self.outbox.append(
                        QueuedReply(
                            reply_id=item.reply_id,
                            response_handle=item.response_handle,
                            endpoint=item.endpoint,
                            text=item.text,
                            tag=item.tag,
                            payload=dict(item.payload),
                            attempts=item.attempts + 1,
                        )
                    )
                self._report_outbox_failure_once(
                    item,
                    reason,
                    permanent=permanent,
                )
                continue
            self._reported_outbox_failures.pop(item.reply_id, None)
            self.mailbox.put(
                EventEnvelope(
                    event_kind=EventKind.REPLY_DELIVERED,
                    source_kind=SourceKind.CHANNEL,
                    payload={"reply_id": item.reply_id, "endpoint_id": item.endpoint.endpoint_id},
                )
            )

    def _report_outbox_failure_once(
        self,
        item: QueuedReply,
        reason: str,
        *,
        permanent: bool = False,
    ) -> None:
        normalized_reason = str(reason or "delivery_failed")
        if permanent:
            self._reported_outbox_failures.pop(item.reply_id, None)
        else:
            now = time.monotonic()
            previous = self._reported_outbox_failures.get(item.reply_id)
            if (
                previous is not None
                and previous[0] == normalized_reason
                and now - previous[1]
                < TRANSIENT_REPLY_FAILURE_REPORT_INTERVAL_SECONDS
            ):
                return
            self._reported_outbox_failures[item.reply_id] = (
                normalized_reason,
                now,
            )
        self.mailbox.put(
            EventEnvelope(
                event_kind=EventKind.REPLY_FAILED,
                source_kind=SourceKind.CHANNEL,
                payload={
                    "reply_id": item.reply_id,
                    "endpoint_id": item.endpoint.endpoint_id,
                    "channel_kind": item.endpoint.channel_kind,
                    "reason": normalized_reason,
                    "permanent": permanent,
                    "attempts": item.attempts + 1,
                },
            )
        )

    def _notify_ready(self) -> None:
        if self.on_ready is not None:
            self.on_ready()

    def _notify_endpoint_ready(self, endpoint_id: str) -> None:
        slot = self.delivery_slots.get(str(endpoint_id or ""))
        if slot is not None and slot.state == "draining":
            self._flush_slot(slot)
        self._notify_ready()


def _transfer_endpoint_runtime_state(
    old_endpoint: ChannelEndpointBase | None,
    new_endpoint: ChannelEndpointBase,
) -> None:
    """Move transport-neutral pending work at the endpoint swap boundary."""

    if old_endpoint is None or old_endpoint is new_endpoint:
        return

    for attribute in (
        "mailbox",
        "outbox",
        "attachment_outbox",
        "status_outbox",
        "stream_update_outbox",
    ):
        old_queue = getattr(old_endpoint, attribute, None)
        new_queue = getattr(new_endpoint, attribute, None)
        old_items = getattr(old_queue, "items", old_queue)
        new_items = getattr(new_queue, "items", new_queue)
        if old_items is None or new_items is None or not old_items:
            continue
        candidate_items = tuple(new_items)
        new_items.clear()
        new_items.extend(old_items)
        new_items.extend(candidate_items)
        old_items.clear()

    for attribute in (
        "_reported_reply_failures",
        "_stream_sessions",
        "_interactive_messages",
        "_tagged_message_targets",
        "_session_replacements",
        "_stream_handle_ids_by_key",
    ):
        old_values = getattr(old_endpoint, attribute, None)
        new_values = getattr(new_endpoint, attribute, None)
        if not isinstance(old_values, dict) or not isinstance(new_values, dict):
            continue
        for key, value in old_values.items():
            new_values.setdefault(key, value)
        old_values.clear()

    for attribute in (
        "_retired_session_ids",
        "_streamed_text_handles",
        "_streamed_text_keys",
    ):
        old_values = getattr(old_endpoint, attribute, None)
        new_values = getattr(new_endpoint, attribute, None)
        if not isinstance(old_values, set) or not isinstance(new_values, set):
            continue
        new_values.update(old_values)
        old_values.clear()

    if bool(getattr(old_endpoint, "_allow_single_session_rebind", False)):
        setattr(new_endpoint, "_allow_single_session_rebind", True)

    old_commands = list(
        getattr(old_endpoint, "_control_commands_manifest", ()) or ()
    )
    if old_commands and not getattr(
        new_endpoint,
        "_control_commands_manifest",
        None,
    ):
        new_endpoint._control_commands_manifest = old_commands


def _queued_delivery_id(item: Any) -> str:
    return str(
        getattr(item, "reply_id", "")
        or getattr(item, "update_id", "")
        or getattr(item, "status_id", "")
        or getattr(item, "attachment_id", "")
    )
