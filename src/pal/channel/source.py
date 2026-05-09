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

    def drain(self, context) -> list:
        _ = context
        self.runtime.flush_outbox()
        return self.runtime.mailbox.drain()
