from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pal.channel.contracts import ChannelStreamUpdate, EndpointConfig
from pal.control.contracts import InteractionButtonSpec, InteractionMessageSpec
from pal.shared import ChannelStreamUpdateKind, EventKind
from tests.runtime_channel_providers import telegram_endpoint_module


@pytest.mark.parametrize("text", ["", " \n\t", "\u200b", "\u200c\u200d", "\ufeff", " \u200b\n\ufeff "])
def test_telegram_does_not_send_whitespace_or_format_controls_alone(text):
    assert telegram_endpoint_module()._telegram_text_segments(text, limit=4096) == []


@pytest.mark.parametrize("text", ["a\u200bb", "می\u200cروم", "👩\u200d💻"])
def test_telegram_preserves_format_controls_inside_meaningful_text(text):
    segments = telegram_endpoint_module()._telegram_text_segments(text, limit=4096)
    assert "".join(segment.fallback_text for segment in segments).strip() == text


def test_retried_zero_width_reply_and_stream_complete_before_status_and_keyboard():
    async def run():
        module = telegram_endpoint_module()
        sent = []

        async def send_message(**kwargs):
            assert module._has_telegram_message_text(kwargs["text"])
            sent.append(kwargs)
            return SimpleNamespace(message_id=len(sent))

        endpoint = module.TelegramChannelEndpoint(
            endpoint=EndpointConfig("telegram_test", "telegram", "chat:1"),
            application=SimpleNamespace(bot=SimpleNamespace(send_message=send_message)),
        )
        handle = endpoint.build_response_handle(reply_target={"chat_id": "1"})
        # The live incident retains the same invisible answer in both reply
        # and terminal-stream retry queues. Both must drain after hot reload.
        poison = endpoint.queue_reply("\u200b", response_handle=handle)
        endpoint.queue_stream_update(ChannelStreamUpdate(
            kind=ChannelStreamUpdateKind.DONE, text="\u200b", finish_reason="stop",
        ), response_handle=handle)
        endpoint.flush_stream_update_outbox()
        status = endpoint.queue_reply("Pal status: idle", response_handle=handle)
        endpoint.flush_outbox()
        endpoint.send_status(handle, "interactive_update", {"spec": InteractionMessageSpec(
            interaction_id="model", interaction_kind="control_panel", text="Choose model",
            buttons=((InteractionButtonSpec("Model A", "control.model.open"),),),
        )})
        await asyncio.gather(*list(endpoint._send_chains.values()))
        events = endpoint.flush_outbox() + endpoint.flush_stream_update_outbox()
        assert {event.payload["reply_id"] for event in events if event.event_kind == EventKind.REPLY_DELIVERED} == {poison, status}
        assert not [event for event in events if event.event_kind == EventKind.REPLY_FAILED]
        assert [item["text"].strip() for item in sent] == ["Pal status: idle", "Choose model"]
        assert sent[-1]["reply_markup"] is not None
        assert not endpoint.outbox and not endpoint._pending_reply_deliveries
        assert not endpoint.stream_update_outbox and not endpoint._pending_stream_deliveries
        assert not endpoint.last_delivery_error

    asyncio.run(run())
