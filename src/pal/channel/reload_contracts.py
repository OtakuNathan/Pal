"""Values transferred between endpoint generations after replacement admission."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pal.channel.contracts import (
    ChannelEnvelope, QueuedAttachment, QueuedReply, QueuedStatus, QueuedStreamUpdate,
)


@dataclass(frozen=True)
class EndpointPendingState:
    mailbox: tuple[ChannelEnvelope, ...]
    replies: tuple[QueuedReply, ...]
    attachments: tuple[QueuedAttachment, ...]
    statuses: tuple[QueuedStatus, ...]
    stream_updates: tuple[QueuedStreamUpdate, ...]
    reported_reply_failures: dict[str, tuple[str, float]]
    stream_sessions: dict[int, dict[str, Any]]
    interactive_messages: dict[str, dict[str, Any]]
    control_commands: tuple[dict[str, str], ...]
    transport: object | None = None


@dataclass(frozen=True)
class SocketPendingState:
    unacknowledged_frames: tuple[dict[str, Any], ...]
    session_replacements: dict[str, str]
    stream_handle_ids_by_key: dict[tuple[str, str, str], int]
    retired_session_ids: frozenset[str]
    streamed_text_handles: frozenset[int]
    streamed_text_keys: frozenset[tuple[str, str, str]]
    allow_single_session_rebind: bool
