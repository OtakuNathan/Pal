from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pal.foundation import EventEnvelope
from pal.foundation.attachment import AttachmentSpec
from pal.stream_events import NormalizedLLMStreamEvent


@dataclass(frozen=True)
class EndpointConfig:
    endpoint_id: str
    channel_kind: str
    binding_key: str
    send_policy: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResponseHandle:
    endpoint_id: str
    reply_target: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelMessageReceipt:
    endpoint_id: str
    message_id: str
    status: Literal["accepted"] = "accepted"


@dataclass(frozen=True)
class ChannelEnvelope:
    event: EventEnvelope
    endpoint: EndpointConfig
    response_handle: ResponseHandle


@dataclass(frozen=True)
class QueuedReply:
    reply_id: str
    response_handle: ResponseHandle
    endpoint: EndpointConfig
    text: str
    attempts: int = 0


@dataclass(frozen=True)
class QueuedStreamEvent:
    event_id: str
    response_handle: ResponseHandle
    endpoint: EndpointConfig
    event: NormalizedLLMStreamEvent
    attempts: int = 0


@dataclass(frozen=True)
class QueuedStatus:
    status_id: str
    response_handle: ResponseHandle
    endpoint: EndpointConfig
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0


@dataclass(frozen=True)
class QueuedAttachment:
    attachment_id: str
    response_handle: ResponseHandle
    endpoint: EndpointConfig
    attachment: AttachmentSpec
    attempts: int = 0


class ChannelDeliveryError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        permanent: bool = False,
        reason: str = "delivery_failed",
    ) -> None:
        super().__init__(message)
        self.permanent = permanent
        self.reason = reason


class ChannelAdapter(Protocol):
    channel_kind: str

    def send(self, response_handle: ResponseHandle, text: str) -> None:
        ...


class ChannelNormalizer(Protocol):
    def normalize(self, payload: Any) -> dict[str, Any]:
        ...


class AgentOutputPort(Protocol):
    def queue_reply(self, envelope: ChannelEnvelope, text: str) -> Any:
        ...

    def queue_stream_event(self, envelope: ChannelEnvelope, event: NormalizedLLMStreamEvent) -> Any:
        ...

    def abort_stream(self, response_handle: ResponseHandle, *, reason: str = "interrupted") -> None:
        ...

    def queue_status(self, envelope: ChannelEnvelope, kind: str, *, payload: dict[str, Any] | None = None) -> Any:
        ...

    def queue_attachment(self, envelope: ChannelEnvelope, attachment: AttachmentSpec) -> Any:
        ...


class ChannelRuntimePort(AgentOutputPort, Protocol):
    async def send_message(self, endpoint_id: str, message: str) -> ChannelMessageReceipt:
        ...

    def emit(self, envelope: ChannelEnvelope) -> None:
        ...

    def queue_endpoint_status(
        self,
        endpoint_id: str,
        kind: str,
        *,
        payload: dict[str, Any] | None = None,
        reply_target: dict[str, Any] | None = None,
    ) -> str | None:
        ...
