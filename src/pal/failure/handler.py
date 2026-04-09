from __future__ import annotations

from dataclasses import dataclass

from pal.core.events import EventHandler
from pal.failure.contracts import FailureSignal
from pal.foundation import EventEnvelope
from pal.shared import EventKind


@dataclass(frozen=True)
class FailureEventHandler(EventHandler):
    core: "PalCore"

    def can_handle(self, event_kind: str) -> bool:
        return event_kind == EventKind.REPLY_FAILED

    async def handle(self, event: EventEnvelope, context) -> list[EventEnvelope] | None:
        _ = context
        payload = event.payload if isinstance(event.payload, dict) else {}
        await self.core.handle_failure_async(
            FailureSignal(
                subsystem="channel",
                component=str(payload.get("endpoint_id") or "channel_endpoint"),
                failure_kind="delivery_failure",
                severity="medium",
                primary_blocker=str(payload.get("reason") or "reply delivery failed"),
                evidence=dict(payload),
                related_ids={
                    "reply_id": str(payload.get("reply_id") or ""),
                    "endpoint_id": str(payload.get("endpoint_id") or ""),
                },
                safe_to_retry=True,
                repair_domain="channel:endpoint",
            ),
            origin="channel.reply_failed",
            conversation_context={"event_id": event.event_id},
        )
        return []
