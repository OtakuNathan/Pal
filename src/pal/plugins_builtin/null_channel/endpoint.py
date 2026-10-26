from __future__ import annotations

from typing import Any
from uuid import uuid4

from pal.channel.channel_endpoint_queue_base import ChannelEndpointQueueBase
from pal.channel.contracts import (
    ChannelDeliveryError,
    ChannelMessageReceipt,
    ChannelStreamUpdate,
    ResponseHandle,
)
from pal.foundation.attachment import AttachmentSpec


class NullChannelEndpoint(ChannelEndpointQueueBase):
    """Accept output for intentional disposal, without transport or replay."""

    def normalize_raw(self, payload: Any) -> dict[str, Any]:
        raise ChannelDeliveryError(
            "null channel does not accept incoming messages",
            permanent=True,
            reason="ingress_unsupported",
        )

    def derive_default_reply_target(self) -> dict[str, Any]:
        return {"sink": "null"}

    async def send_message(self, message: str) -> ChannelMessageReceipt:
        if not self.enabled or not self.attached:
            raise ChannelDeliveryError(
                "null channel is disabled or detached",
                permanent=True,
                reason="channel_disabled" if not self.enabled else "channel_detached",
            )
        return ChannelMessageReceipt(endpoint_id=self.endpoint.endpoint_id, message_id=str(uuid4()))

    def send_reply(self, response_handle: ResponseHandle, text: str) -> None:
        pass

    def supports_stream_delivery(self) -> bool:
        return True

    def send_stream_update(self, response_handle: ResponseHandle, update: ChannelStreamUpdate) -> None:
        # Do not use the base implementation: it accumulates stream text.
        pass

    def send_status(self, response_handle: ResponseHandle, kind: str, payload: dict[str, Any]) -> None:
        # Even interactive status is discarded, without retaining callback maps.
        pass

    def send_attachment(self, response_handle: ResponseHandle, attachment: AttachmentSpec) -> None:
        # A sink never opens the attachment's path.
        pass

    def inspect_health(self) -> dict[str, Any]:
        return {
            "healthy": self.enabled and self.attached,
            "delivery_mode": "discard",
            "accepts_ingress": False,
        }

    def inspect_auth_state(self) -> dict[str, Any]:
        return {"authorized": True, "requires_auth": False}
