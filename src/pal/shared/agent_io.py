from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, NoReturn, Protocol, Self

from pal.foundation import EventEnvelope
from pal.foundation.attachment import AttachmentSpec
from pal.shared.enums import ChannelStreamUpdateKind
from pal.shared.tool_protocol import ToolCallIR


class _FrozenJsonDict(dict):
    """A JSON-serializable mapping whose complete value tree is immutable."""

    def __init__(self, value=(), /, **kwargs: Any) -> None:
        source = dict(value, **kwargs)
        dict.__init__(self, ((key, _freeze_json(item)) for key, item in source.items()))

    @staticmethod
    def _immutable() -> NoReturn:
        raise TypeError("frozen JSON mapping")

    def __setitem__(self, key: Any, value: Any) -> NoReturn:
        self._immutable()

    def __delitem__(self, key: Any) -> NoReturn:
        self._immutable()

    def clear(self) -> NoReturn:
        self._immutable()

    def pop(self, key: Any, default: Any = None) -> NoReturn:
        self._immutable()

    def popitem(self) -> NoReturn:
        self._immutable()

    def setdefault(self, key: Any, default: Any = None) -> NoReturn:
        self._immutable()

    def update(self, *args: Any, **kwargs: Any) -> NoReturn:
        self._immutable()

    def __ior__(self, other: Any, /) -> Self:
        self._immutable()

    def __or__(self, other: Any, /) -> Self:
        return type(self)({**dict(self), **dict(other)})

    def __ror__(self, other: Any, /) -> Self:
        return type(self)({**dict(other), **dict(self)})

    def __copy__(self) -> "_FrozenJsonDict":
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> "_FrozenJsonDict":
        memo[id(self)] = self
        return self

    def __reduce__(self):
        return (_FrozenJsonDict, (dict(self),))


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _FrozenJsonDict(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_json(item) for item in value)
    return value


@dataclass(frozen=True)
class ChannelMessage:
    """Provider-neutral user-visible message with optional presentation hints.

    ``text`` is always the complete fallback.  Providers may use ``tag`` and
    ``payload`` to realize a richer native presentation, but an unknown tag is
    still an ordinary text message.
    """

    text: str = ""
    tag: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized_tag = str(self.tag or "").strip() or None
        object.__setattr__(self, "text", str(self.text or ""))
        object.__setattr__(self, "tag", normalized_tag)
        object.__setattr__(self, "payload", _FrozenJsonDict(self.payload or {}))


@dataclass(frozen=True)
class ChannelStreamUpdate:
    """Provider-neutral partial reply projected from LLM IR to a channel."""

    kind: ChannelStreamUpdateKind = ChannelStreamUpdateKind.TEXT_DELTA
    text: str = ""
    reasoning_text: str = ""
    tool_call: ToolCallIR | None = None
    finish_reason: str | None = None
    error_text: str = ""
    message: ChannelMessage | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", ChannelStreamUpdateKind(self.kind))


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
    opening_delivery_binding: "TurnDeliveryBinding | None" = None


@dataclass(frozen=True)
class TurnDeliveryBinding:
    """Immutable reply authority captured from the message that opened a turn."""

    endpoint: EndpointConfig
    response_handle: ResponseHandle
    control_scope_key: str
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "endpoint",
            EndpointConfig(
                endpoint_id=self.endpoint.endpoint_id,
                channel_kind=self.endpoint.channel_kind,
                binding_key=self.endpoint.binding_key,
                send_policy=_FrozenJsonDict(self.endpoint.send_policy),
            ),
        )
        object.__setattr__(
            self,
            "response_handle",
            ResponseHandle(
                endpoint_id=self.response_handle.endpoint_id,
                reply_target=_FrozenJsonDict(self.response_handle.reply_target),
            ),
        )

    @classmethod
    def from_envelope(cls, envelope: ChannelEnvelope, *, control_scope_key: str) -> "TurnDeliveryBinding":
        return cls(
            endpoint=envelope.endpoint,
            response_handle=envelope.response_handle,
            control_scope_key=str(control_scope_key),
            correlation_id=envelope.event.correlation_id or envelope.event.event_id,
        )


@dataclass(frozen=True)
class QueuedReply:
    reply_id: str
    response_handle: ResponseHandle
    endpoint: EndpointConfig
    text: str
    tag: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", str(self.text or ""))
        object.__setattr__(self, "tag", str(self.tag or "").strip() or None)
        object.__setattr__(self, "payload", _FrozenJsonDict(self.payload or {}))

    @property
    def message(self) -> ChannelMessage:
        return ChannelMessage(text=self.text, tag=self.tag, payload=dict(self.payload))


@dataclass(frozen=True)
class QueuedStreamUpdate:
    update_id: str
    response_handle: ResponseHandle
    endpoint: EndpointConfig
    update: ChannelStreamUpdate
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
    def supports_stream_delivery(self, envelope: TurnDeliveryBinding) -> bool:
        ...

    def queue_reply(self, envelope: TurnDeliveryBinding, message: ChannelMessage | str) -> Any:
        ...

    def queue_stream_update(self, envelope: TurnDeliveryBinding, update: ChannelStreamUpdate) -> Any:
        ...

    def abort_stream(self, response_handle: ResponseHandle, *, reason: str = "interrupted") -> None:
        ...

    def queue_status(self, envelope: TurnDeliveryBinding, kind: str, *, payload: dict[str, Any] | None = None) -> Any:
        ...

    def queue_attachment(self, envelope: TurnDeliveryBinding, attachment: AttachmentSpec) -> Any:
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

    def flush_endpoint_status(self, endpoint_id: str) -> bool:
        """Start delivery of queued lifecycle updates for one endpoint now."""

        ...
