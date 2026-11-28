"""Telegram details and decision cards preserve one producer-owned interaction."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock

from pal.channel.contracts import ChannelDeliveryError, EndpointConfig, ResponseHandle
from pal.control.contracts import InteractionButtonSpec, InteractionMessageSpec
from pal.foundation import AttachmentSpec
from tests.runtime_channel_providers import telegram_endpoint_module


class Bot:
    def __init__(self):
        self.calls = []
        self.sequence = 100
        self.fail_upload = False
        self.fail_edit = False
        self.fail_message = False

    async def send_message(self, **kwargs):
        if self.fail_message:
            raise RuntimeError("Card delivery failed")
        self.sequence += 1
        self.calls.append(("message", {**kwargs, "message_id": self.sequence}))
        return SimpleNamespace(message_id=self.sequence)

    async def send_document(self, **kwargs):
        if self.fail_upload:
            raise RuntimeError("Upload failed")
        self.sequence += 1
        self.calls.append(("document", {**kwargs, "document": kwargs["document"].read(), "message_id": self.sequence}))
        return SimpleNamespace(message_id=self.sequence)

    async def edit_message_text(self, **kwargs):
        if self.fail_edit:
            raise RuntimeError("Connection unavailable")
        if len(kwargs["text"].encode("utf-16-le")) // 2 > 4096:
            raise RuntimeError("Message is too long")
        self.calls.append(("edit", kwargs))

    async def edit_message_reply_markup(self, **kwargs):
        self.calls.append(("keyboard", kwargs))


class TelegramInteractionPresentationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.bot = Bot()
        self.endpoint = self.new_endpoint()
        self.handle = ResponseHandle("telegram", {"chat_id": "42", "user_id": "42", "thread_id": "7"})

    def new_endpoint(self):
        endpoint = telegram_endpoint_module().TelegramChannelEndpoint(
            endpoint=EndpointConfig(endpoint_id="telegram", channel_kind="telegram", binding_key="user:42"),
            runtime_root=self.root, bot_token="test-token")
        endpoint.application = SimpleNamespace(bot=self.bot)
        return endpoint

    async def asyncTearDown(self):
        await self.endpoint._recover_pending_deliveries_async()
        self.directory.cleanup()

    def review(self, **changes):
        spec = InteractionMessageSpec("review", "bunshin_architecture_review",
            text="# Architecture review\n\n" + "Long exact content 中文\n" * 700,
            buttons=((InteractionButtonSpec("Accept", "review.accept", {"decision": "accept"}),
                      InteractionButtonSpec("Reject", "review.reject", {"decision": "reject"})),))
        return replace(spec, **changes)

    async def open(self, spec, *, update=False):
        await self.endpoint._open_or_update_interaction_async(self.handle, spec=spec, allow_update=update)

    def callback(self, spec, card):
        button = card["reply_markup"].inline_keyboard[0][0]
        query = SimpleNamespace(data=button.callback_data, from_user=SimpleNamespace(id=42), answer=AsyncMock(),
                                message=SimpleNamespace(chat=SimpleNamespace(id=42), message_id=card["message_id"]))
        return SimpleNamespace(callback_query=query)

    async def test_long_review_uploads_exact_details_and_closes_only_short_card(self):
        spec = self.review()
        await self.open(spec)
        document, card = self.bot.calls
        self.assertEqual(document[0], "document")
        self.assertEqual(document[1]["document"], spec.text.encode("utf-8"))
        self.assertEqual(card[0], "message")
        self.assertEqual(card[1]["reply_to_message_id"], document[1]["message_id"])
        self.assertEqual(card[1]["message_thread_id"], 7)
        self.assertLess(len(card[1]["text"]), 1024)
        self.assertNotIn("Long exact content", card[1]["text"])
        await self.endpoint._resolve_interaction_async(replace(spec, text=spec.text + "\n\nRejected.\n\nPlease revise the API.", buttons=()))
        self.assertEqual(self.bot.calls[-1][0], "edit")
        self.assertEqual(self.bot.calls[-1][1]["text"], "Rejected.\n\nPlease revise the API.")
        self.assertIsNone(self.bot.calls[-1][1]["reply_markup"])
        self.assertIsNone(self.endpoint._restore_interaction(spec.interaction_id))

    async def test_queued_bunshin_files_precede_card_and_markdown_is_not_duplicated(self):
        spec = self.review()
        task = self.root / "task.yaml"
        markdown = self.root / "architecture.md"
        task.write_text("request: original task\n")
        markdown.write_text(spec.text)
        ids = [self.endpoint.queue_attachment(AttachmentSpec(str(path), file_name=path.name), response_handle=self.handle)
               for path in (task, markdown)]
        self.endpoint.queue_status("interactive_open", response_handle=self.handle, payload={"spec": spec})
        self.endpoint.flush_status_outbox()
        self.endpoint.flush_attachment_outbox()
        await asyncio.gather(*tuple(self.endpoint._send_chains.values()))
        self.assertEqual([kind for kind, _ in self.bot.calls], ["document", "document", "message"])
        self.assertEqual(self.bot.calls[2][1]["reply_to_message_id"], self.bot.calls[1][1]["message_id"])
        delivered = self.endpoint.flush_attachment_outbox()
        self.assertEqual({event.payload["reply_id"] for event in delivered}, set(ids))

    async def test_failed_upload_never_publishes_a_keyboard(self):
        self.bot.fail_upload = True
        with self.assertRaises(ChannelDeliveryError):
            await self.open(self.review())
        self.assertEqual(self.bot.calls, [])
        self.assertIsNone(self.endpoint._restore_interaction("review"))
        self.bot.fail_upload = False
        await self.open(self.review())
        self.assertEqual([kind for kind, _ in self.bot.calls], ["document", "message"])

    async def test_failed_card_retry_reuses_already_uploaded_details(self):
        self.bot.fail_message = True
        with self.assertRaises(ChannelDeliveryError):
            await self.open(self.review())
        self.assertEqual([kind for kind, _ in self.bot.calls], ["document"])
        self.assertIsNone(self.endpoint._restore_interaction("review"))
        self.bot.fail_message = False
        await self.open(self.review())
        self.assertEqual([kind for kind, _ in self.bot.calls], ["document", "message"])

    async def test_failed_queued_task_file_blocks_review_keyboard(self):
        path = self.root / "missing-task.yaml"
        self.endpoint.queue_attachment(AttachmentSpec(str(path)), response_handle=self.handle)
        self.endpoint.queue_status("interactive_open", response_handle=self.handle, payload={"spec": self.review()})
        self.endpoint.flush_status_outbox()
        results = await asyncio.gather(*tuple(self.endpoint._send_chains.values()), return_exceptions=True)
        self.assertTrue(any(isinstance(result, ChannelDeliveryError) for result in results))
        self.assertEqual(self.bot.calls, [])
        self.assertIsNone(self.endpoint._restore_interaction("review"))

    async def test_astral_unicode_uses_utf16_limit_for_document_selection(self):
        spec = self.review(text="😀" * 2300)
        await self.open(spec)
        self.assertEqual(self.bot.calls[0][0], "document")
        self.assertEqual(self.bot.calls[0][1]["document"], spec.text.encode("utf-8"))
        self.assertLessEqual(len(self.bot.calls[-1][1]["text"].encode("utf-16-le")) // 2, 1024)

    async def test_changed_details_revoke_old_card_and_only_new_message_can_answer(self):
        spec = self.review()
        await self.open(spec)
        old = self.bot.calls[-1][1]
        updated = replace(spec, text=spec.text + "\nA changed obligation.")
        await self.open(updated, update=True)
        self.assertEqual([kind for kind, _ in self.bot.calls], ["document", "message", "keyboard", "document", "message"])
        new = self.bot.calls[-1][1]
        self.assertNotEqual(old["message_id"], new["message_id"])
        self.assertIsNone(await self.endpoint._interaction_result_from_update(self.callback(spec, old)))
        result = await self.endpoint._interaction_result_from_update(self.callback(updated, new))
        self.assertEqual(result.action_args, {"decision": "accept"})

    async def test_failed_close_keeps_retry_target_but_revokes_actions_across_restart(self):
        spec = self.review()
        await self.open(spec)
        card = self.bot.calls[-1][1]
        self.bot.fail_edit = True
        resolved = replace(spec, text="Approved once.", buttons=())
        await self.endpoint._resolve_interaction_async(resolved)
        self.endpoint = self.new_endpoint()
        self.assertIsNotNone(self.endpoint._restore_interaction(spec.interaction_id))
        self.assertIsNone(await self.endpoint._interaction_result_from_update(self.callback(spec, card)))
        self.bot.fail_edit = False
        await self.endpoint._resolve_interaction_async(resolved)
        self.assertEqual(self.bot.calls[-1][1]["message_id"], card["message_id"])
        self.assertEqual(self.bot.calls[-1][1]["text"], "Approved once.")
        self.assertIsNone(self.endpoint._restore_interaction(spec.interaction_id))

    async def test_short_question_uses_letter_buttons_without_changing_answer_payload(self):
        spec = self.review(interaction_kind="bunshin_clarification", text="Where should we deploy?\n\nFull question details.",
                           buttons=((InteractionButtonSpec("Surface", "answer", {"answer": "surface"}),
                                     InteractionButtonSpec("Cloud", "answer", {"answer": "cloud"})),))
        await self.open(spec)
        self.assertEqual([kind for kind, _ in self.bot.calls], ["message", "message"])
        details, card = [payload for _, payload in self.bot.calls]
        self.assertEqual(details["text"], spec.text.replace(".", r"\."))
        self.assertEqual(details["parse_mode"], "MarkdownV2")
        self.assertNotIn("reply_markup", details)
        self.assertIn("A: Surface", card["text"])
        self.assertIn("B: Cloud", card["text"])
        self.assertEqual(card["reply_markup"].inline_keyboard[0][0].text, "A")
        self.endpoint = self.new_endpoint()
        result = await self.endpoint._interaction_result_from_update(self.callback(spec, card))
        self.assertEqual(result.action_args, {"answer": "surface"})
        await self.open(spec, update=True)
        self.assertEqual([kind for kind, _ in self.bot.calls], ["message", "message", "edit"])

    async def test_generic_markdown_card_retries_format_rejection_as_plain_text(self):
        spec = self.review(interaction_kind="custom_panel", text="**Title**\n\nChoose *one*.",
                           buttons=((InteractionButtonSpec("> Choice", "choose", {"value": 1}),),))
        self.endpoint.endpoint.send_policy["max_message_chars"] = 4096
        send = self.bot.send_message
        edit = self.bot.edit_message_text

        async def reject_format(method, **kwargs):
            if kwargs.get("parse_mode"):
                raise RuntimeError("Bad Request: can't parse entities")
            return await method(**kwargs)

        async def send_message(**kwargs):
            return await reject_format(send, **kwargs)
        async def edit_message_text(**kwargs):
            return await reject_format(edit, **kwargs)
        self.bot.send_message = AsyncMock(side_effect=send_message)
        self.bot.edit_message_text = AsyncMock(side_effect=edit_message_text)
        await self.open(spec)
        card = self.bot.calls[-1][1]
        self.assertEqual(card["text"], "Title\n\nChoose one.")
        self.assertNotIn("parse_mode", card)
        self.assertEqual(card["reply_markup"].inline_keyboard[0][0].text, "> Choice")
        result = await self.endpoint._interaction_result_from_update(self.callback(spec, card))
        self.assertEqual(result.action_args, {"value": 1})
        await self.open(replace(spec, text="**Updated**"), update=True)
        self.assertEqual(self.bot.calls[-1][1]["text"], "Updated")
        self.assertEqual(self.bot.calls[-1][1]["message_id"], card["message_id"])
