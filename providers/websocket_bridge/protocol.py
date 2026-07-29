"""Frozen Pal-to-Pal WebSocket bridge protocol.

The bridge is a bounded peer-conversation transport.  WebSocket frames carry a
small exchange context, while the local Pal runtime is reached through the
provider's dedicated Unix socket.  The ordinary TTY ``pal.sock`` is not part of
this protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Protocol, runtime_checkable
from uuid import UUID, uuid4

SLASH_PREFIX: Final[str] = "/"
PEER_END_SENTINEL: Final[str] = "[[peer_end]]"
PEER_CONTEXT_VERSION: Final[int] = 1
MAX_PEER_MESSAGE_COUNT: Final[int] = 8
ORDINARY_SOCKET_PROTOCOL_TYPE: Final[str] = "user_message"
SLASH_SOCKET_PROTOCOL_TYPE: Final[str] = "slash_command"

REJECTION_REASON_SLASH: Final[str] = "slash_command_rejected_at_bridge_boundary"
REJECTION_REASON_CONTROL_PLANE: Final[str] = "control_plane_payload_rejected_at_bridge_boundary"
REJECTION_REASON_PEER_CONTEXT: Final[str] = "invalid_peer_context"


class InvalidPeerContext(ValueError):
    """Raised when a framed continuation carries malformed exchange metadata."""


@dataclass(frozen=True)
class PeerExchangeContext:
    """One bounded peer exchange carried by a WebSocket message."""

    exchange_id: str
    message_count: int

    @classmethod
    def root(cls) -> "PeerExchangeContext":
        return cls(exchange_id=str(uuid4()), message_count=1)

    def next_message(self) -> "PeerExchangeContext":
        return PeerExchangeContext(
            exchange_id=self.exchange_id,
            message_count=self.message_count + 1,
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "version": PEER_CONTEXT_VERSION,
            "exchange_id": self.exchange_id,
            "message_count": self.message_count,
        }


def parse_peer_context(value: object) -> PeerExchangeContext:
    """Validate continuation metadata.

    A missing context is handled by the caller as a backwards-compatible root.
    Once a context is present it must be valid; malformed metadata must never
    silently reset the hop counter.
    """

    if not isinstance(value, dict):
        raise InvalidPeerContext("peer_context must be an object")
    try:
        version = int(value.get("version"))
        message_count = int(value.get("message_count"))
    except (TypeError, ValueError) as exc:
        raise InvalidPeerContext("peer_context version and message_count must be integers") from exc
    if version != PEER_CONTEXT_VERSION:
        raise InvalidPeerContext(f"unsupported peer_context version: {version}")
    exchange_id = str(value.get("exchange_id") or "").strip()
    try:
        UUID(exchange_id)
    except (ValueError, AttributeError) as exc:
        raise InvalidPeerContext("peer_context exchange_id must be a UUID") from exc
    if message_count < 1:
        raise InvalidPeerContext("peer_context message_count must be positive")
    return PeerExchangeContext(
        exchange_id=exchange_id,
        message_count=message_count,
    )


def render_peer_input(text: str, peer_identity: str) -> str:
    """Append the receiver-owned identity marker stored in ordinary L1 text."""

    body = str(text or "")
    identity = str(peer_identity or "").strip()
    if not identity:
        raise ValueError("peer identity is required")
    return f"{body}\n\n--{identity}"


@runtime_checkable
class BridgeAdaptation(Protocol):
    def is_deliverable_inbound(self, text: str) -> bool:  # pragma: no cover - contract
        """Return True only for an ordinary, non-slash inbound frame text."""
        ...

    def rejection_reason(self, text: str) -> str | None:  # pragma: no cover - contract
        """Return the boundary rejection reason, or None when deliverable."""
        ...


__all__ = [
    "BridgeAdaptation",
    "InvalidPeerContext",
    "MAX_PEER_MESSAGE_COUNT",
    "ORDINARY_SOCKET_PROTOCOL_TYPE",
    "PEER_CONTEXT_VERSION",
    "PEER_END_SENTINEL",
    "PeerExchangeContext",
    "REJECTION_REASON_CONTROL_PLANE",
    "REJECTION_REASON_PEER_CONTEXT",
    "REJECTION_REASON_SLASH",
    "SLASH_PREFIX",
    "SLASH_SOCKET_PROTOCOL_TYPE",
    "parse_peer_context",
    "render_peer_input",
]
