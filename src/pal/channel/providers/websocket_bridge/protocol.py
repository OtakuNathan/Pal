"""Frozen WebSocket-to-socket bridge adaptation contract (file_frozen).

This module is the immutable contract for the Pal-to-Pal LAN WebSocket bridge.
It declares ONLY the observable adaptation and boundary-rejection rules. It owns
no runtime state and performs no I/O. Implementation is owned by
``websocket_sidecar`` and ``websocket_bridge_provider``.

Contract summary (see architecture.yaml ``bridge_protocol``):

* The existing socket channel is the SOLE local Pal Channel. Pal-to-Pal frames
  reuse the existing socket channel request/response message shapes
  (``pal.channel.endpoints.socket_protocol``) and add no new wire message kind,
  envelope schema, control protocol, or message semantics.
* Inbound: a socket-protocol ``user_message`` frame, or a backwards-compatible
  ordinary text frame, is delivered through the existing user-message path
  (equivalent to ``pal client --message``), not by duplicating message handling.
* Replies are correlated by the existing ``request_id``. Reasoning, tool-call
  progress, and intermediate model rounds are transport events, not user
  messages; only the final response text is returned to an active sender.
* Boundary rejection: slash-prefixed frames and control-plane-like frames are
  rejected at the bridge boundary and NEVER forwarded to the socket channel or
  the slash-command dispatcher.

A real production boundary still keeps public-internet, TLS, autodiscovery,
clustering, fanout, and remote-control of another Pal runtime out of scope.
"""

from __future__ import annotations

from typing import Final, Protocol, runtime_checkable

# The bridge carries WebSocket TEXT frames. Plain text remains supported for
# simple clients; Pal peers use the existing socket-protocol JSON message shape
# so request/reply frames can be correlated without adding a new envelope. The
# reused client->server delivery path
# (``pal.tty.session.SocketSession.send``, the same path used by
# ``pal client --message``) classifies a frame's socket-protocol type solely from
# its text: ``ORDINARY_SOCKET_PROTOCOL_TYPE`` for ordinary text and
# ``SLASH_SOCKET_PROTOCOL_TYPE`` for text beginning with ``SLASH_PREFIX``. These
# constants are facts about that reused path.
SLASH_PREFIX: Final[str] = "/"
ORDINARY_SOCKET_PROTOCOL_TYPE: Final[str] = "user_message"
SLASH_SOCKET_PROTOCOL_TYPE: Final[str] = "slash_command"

# Rejection reasons surfaced by the bridge boundary. These are observable
# diagnostics only; rejection never raises out of the bridge into the runtime.
# Slash rejection is the hard, text-based invariant. Control-plane-like text may
# be rejected or handled as ordinary text; that softer decision is deferred to
# the sidecar implementation.
REJECTION_REASON_SLASH: Final[str] = "slash_command_rejected_at_bridge_boundary"
REJECTION_REASON_CONTROL_PLANE: Final[str] = "control_plane_payload_rejected_at_bridge_boundary"


@runtime_checkable
class BridgeAdaptation(Protocol):
    """Observable WebSocket-to-socket adaptation surface frozen by this contract.

    Boundary classification is keyed on ordinary message text. The hard slash invariant is
    therefore
    ``text.startswith(SLASH_PREFIX)`` -> rejected, matching the reused
    ``SocketSession.send`` classification.
    """

    def is_deliverable_inbound(self, text: str) -> bool:  # pragma: no cover - contract
        """Return True only for an ordinary, non-slash inbound frame text."""
        ...

    def rejection_reason(self, text: str) -> str | None:  # pragma: no cover - contract
        """Return the boundary rejection reason, or None when the frame text is deliverable."""
        ...


__all__ = [
    "SLASH_PREFIX",
    "ORDINARY_SOCKET_PROTOCOL_TYPE",
    "SLASH_SOCKET_PROTOCOL_TYPE",
    "REJECTION_REASON_SLASH",
    "REJECTION_REASON_CONTROL_PLANE",
    "BridgeAdaptation",
]
