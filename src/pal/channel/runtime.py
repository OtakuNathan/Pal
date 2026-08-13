from __future__ import annotations

import asyncio
import inspect
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
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
from pal.shared import EventKind, SourceKind
from pal.channel.ingress import ChannelIngressCompiler


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
        if old_endpoint is endpoint:
            if self._started:
                self._queue_cached_control_catalog(endpoint)
            return
        endpoint.on_ready = self._notify_ready
        preparer = getattr(endpoint, "prepare_replacement", None)
        if old_endpoint is not None and callable(preparer):
            preparation = preparer(old_endpoint)
            if inspect.isawaitable(preparation):
                await preparation
        if not self._started:
            _transfer_endpoint_runtime_state(old_endpoint, endpoint)
            self.endpoint_registry.register(endpoint)
            return

        old_stopper = getattr(old_endpoint, "stop_async", None)
        if old_endpoint is not None and callable(old_stopper):
            await old_stopper()
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
            raise
        # Registry replacement is the commit point.  Until the candidate has
        # started successfully, readers continue to resolve the old endpoint.
        _transfer_endpoint_runtime_state(old_endpoint, endpoint)
        self.endpoint_registry.register(endpoint)
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
                raise RuntimeError("replace_endpoint cannot block its owner event loop; use replace_endpoint_async")
            future = asyncio.run_coroutine_threadsafe(_replace(), loop)
            future.result(timeout=timeout_seconds)
            return
        if not self._started:
            old_endpoint = self.get_endpoint(endpoint.endpoint.endpoint_id)
            preparer = getattr(endpoint, "prepare_replacement", None)
            if old_endpoint is not None and callable(preparer):
                preparation = preparer(old_endpoint)
                if inspect.isawaitable(preparation):
                    raise RuntimeError(
                        "async endpoint replacement preparation requires replace_endpoint_async"
                    )
            _transfer_endpoint_runtime_state(old_endpoint, endpoint)
            self.register_endpoint(endpoint)
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
            return self.endpoint_registry.unregister(endpoint_id) is not None
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
        # Concrete endpoints own transport-specific ingress/outbox flow. The
        # runtime aggregates those per-endpoint queues into the shared channel
        # mailbox that MainLoop polls.
        for endpoint in self.list_endpoints():
            for envelope in endpoint.poll():
                self.emit(envelope)
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
        endpoint = self.get_endpoint(envelope.endpoint.endpoint_id)
        if endpoint is not None:
            return endpoint.queue_reply(message, response_handle=envelope.response_handle)
        normalized = message if isinstance(message, ChannelMessage) else ChannelMessage(text=str(message or ""))
        reply_id = str(uuid4())
        self.outbox.append(
            QueuedReply(
                reply_id=reply_id,
                response_handle=envelope.response_handle,
                endpoint=envelope.endpoint,
                text=normalized.text,
                tag=normalized.tag,
                payload=dict(normalized.payload),
            )
        )
        self._notify_ready()
        return reply_id

    def queue_stream_update(self, envelope: TurnDeliveryBinding, update: ChannelStreamUpdate) -> str:
        endpoint = self.get_endpoint(envelope.endpoint.endpoint_id)
        if endpoint is not None:
            return endpoint.queue_stream_update(update, response_handle=envelope.response_handle)
        update_id = str(uuid4())
        self.stream_update_outbox.append(
            QueuedStreamUpdate(
                update_id=update_id,
                response_handle=envelope.response_handle,
                endpoint=envelope.endpoint,
                update=update,
            )
        )
        self._notify_ready()
        return update_id

    def queue_attachment(self, envelope: TurnDeliveryBinding, attachment: AttachmentSpec) -> str:
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
