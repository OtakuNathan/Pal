from __future__ import annotations

from dataclasses import dataclass

from pal.channel.runtime import ChannelRuntime
from pal.core.events import EventSource
from pal.shared import SourceKind


@dataclass
class ChannelEventSource(EventSource):
    runtime: ChannelRuntime
    source_id: str = f"{SourceKind.CHANNEL}.mailbox"

    def prepare(self, context) -> bool:
        _ = context
        self.runtime.flush_outbox()
        return self.runtime.mailbox.has_pending()

    def poll_timeout_ms(self, context) -> int | None:
        _ = context
        return 0 if self.runtime.mailbox.has_pending() or self.runtime.outbox else None

    def drain(self, context) -> list:
        _ = context
        self.runtime.flush_outbox()
        return self.runtime.mailbox.drain()
