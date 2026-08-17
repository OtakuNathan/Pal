from __future__ import annotations

import unittest
from types import SimpleNamespace

from pal.foundation import EventEnvelope
from pal.bunshin.source import BunshinControlEventHandler, _event_kind
from pal.shared import EventKind, SourceKind


class _Provider:
    def __init__(self) -> None:
        self.parts: set[str] = set()
        self.settlements: list[bool] = []

    def delivered_event_parts(self, event):
        _ = event
        return set(self.parts)

    def settle_event_part(self, event, part_key):
        _ = event
        self.parts.add(str(part_key))
        return True

    def settle_event(self, event, *, accepted, error=""):
        _ = (event, error)
        self.settlements.append(bool(accepted))


class _Core:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.actions: list[object] = []
        self.fail_second_attachment_once = True

    async def handle_control_action_async(self, action, *, require_provider):
        self.assert_require_provider(require_provider)
        self.actions.append(action)
        delivery = action.delivery
        if delivery.delivery_kind == "attachment":
            part = str(delivery.payload.get("path") or "")
        else:
            part = "primary"
        self.calls.append(part)
        if part == "second.md" and self.fail_second_attachment_once:
            self.fail_second_attachment_once = False
            return False
        return True

    @staticmethod
    def assert_require_provider(value: bool) -> None:
        if not value:
            raise AssertionError("delivery must require a concrete provider")


class BunshinCompositeDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def test_workflow_terminal_uses_terminal_delivery_path(self) -> None:
        self.assertEqual(_event_kind("workflow_terminal"), EventKind.BUNSHIN_TERMINAL)

    def test_architecture_review_resolution_uses_its_own_delivery_path(self) -> None:
        self.assertEqual(
            _event_kind("architecture_review_resolved"),
            EventKind.BUNSHIN_ARCHITECTURE_REVIEW_RESOLVED,
        )

    async def test_architecture_review_resolution_closes_original_card(self) -> None:
        provider = _Provider()
        core = _Core()
        core.fail_second_attachment_once = False
        context = SimpleNamespace(port_registry={"core:core": core})
        event = EventEnvelope(
            event_kind=EventKind.BUNSHIN_ARCHITECTURE_REVIEW_RESOLVED,
            source_kind=SourceKind.BUNSHIN,
            payload={
                "delivery_id": "delivery-review-resolved",
                "workflow_id": "workflow-1",
                "architecture_revision_id": "revision-1",
                "summary": "Bunshin architecture decision recorded (accepted).",
                "route": {
                    "endpoint_id": "socket",
                    "channel_kind": "socket",
                    "reply_target": {"session_id": "session-1"},
                },
            },
        )

        await BunshinControlEventHandler(provider=provider).handle(event, context)

        self.assertEqual(core.calls, ["primary"])
        action = core.actions[0]
        self.assertEqual(action.action_kind, "interactive_resolve")
        self.assertEqual(
            action.delivery.interaction.interaction_id,
            "bunshin_v2_architecture_revision-1",
        )
        self.assertEqual(provider.settlements, [True])

    async def test_terminal_closes_worker_owned_pending_interactions(self) -> None:
        provider = _Provider()
        core = _Core()
        core.fail_second_attachment_once = False
        context = SimpleNamespace(port_registry={"core:core": core})
        event = EventEnvelope(
            event_kind=EventKind.BUNSHIN_TERMINAL,
            source_kind=SourceKind.BUNSHIN,
            payload={
                "delivery_id": "delivery-terminal",
                "workflow_id": "workflow-1",
                "status": "cancelled",
                "summary": "Bunshin workflow cancelled.",
                "resolved_interactions": [
                    {
                        "interaction_id": "approval-1",
                        "interaction_kind": "bunshin_approval",
                    },
                    {
                        "interaction_id": "clarification-1",
                        "interaction_kind": "bunshin_clarification",
                    },
                ],
                "route": {
                    "endpoint_id": "socket",
                    "channel_kind": "socket",
                    "reply_target": {"session_id": "session-1"},
                },
            },
        )
        handler = BunshinControlEventHandler(provider=provider)

        await handler.handle(event, context)

        self.assertEqual(
            [action.action_kind for action in core.actions],
            ["interactive_resolve", "interactive_resolve", "route_reply"],
        )
        self.assertEqual(
            provider.parts,
            {"interaction:0", "interaction:1", "primary"},
        )
        self.assertEqual(provider.settlements, [True])

    async def test_completed_workflow_delivers_patch_before_terminal_message(self) -> None:
        provider = _Provider()
        core = _Core()
        core.fail_second_attachment_once = False
        context = SimpleNamespace(port_registry={"core:core": core})
        event = EventEnvelope(
            event_kind=EventKind.BUNSHIN_TERMINAL,
            source_kind=SourceKind.BUNSHIN,
            payload={
                "delivery_id": "delivery-patch",
                "workflow_id": "workflow-1",
                "status": "completed",
                "summary": "Bunshin workflow completed. Verified Git patch attached.",
                "attachments": [
                    {
                        "path": "/tmp/result.patch",
                        "file_name": "result.patch",
                        "mime_type": "text/x-patch",
                    }
                ],
                "route": {
                    "endpoint_id": "telegram",
                    "channel_kind": "telegram",
                    "reply_target": {"chat_id": "42"},
                },
            },
        )

        await BunshinControlEventHandler(provider=provider).handle(event, context)

        self.assertEqual(core.calls, ["/tmp/result.patch", "primary"])
        self.assertEqual(provider.parts, {"attachment:0", "primary"})
        self.assertEqual(provider.settlements, [True])

    async def test_resolved_clarification_closes_the_original_interaction(self) -> None:
        provider = _Provider()
        core = _Core()
        core.fail_second_attachment_once = False
        context = SimpleNamespace(port_registry={"core:core": core})
        event = EventEnvelope(
            event_kind=EventKind.BUNSHIN_CLARIFICATION_RESOLVED,
            source_kind=SourceKind.BUNSHIN,
            payload={
                "delivery_id": "delivery-resolved",
                "workflow_id": "workflow-1",
                "clarification_id": "clarification-1",
                "summary": "Bunshin clarification recorded.",
                "route": {
                    "endpoint_id": "socket",
                    "channel_kind": "socket",
                    "reply_target": {"session_id": "session-1"},
                },
            },
        )
        handler = BunshinControlEventHandler(provider=provider)

        await handler.handle(event, context)

        self.assertEqual(core.calls, ["primary"])
        self.assertEqual(provider.parts, {"primary"})
        self.assertEqual(provider.settlements, [True])

    async def test_retry_skips_parts_already_accepted_by_channel(self) -> None:
        provider = _Provider()
        core = _Core()
        context = SimpleNamespace(port_registry={"core:core": core})
        event = EventEnvelope(
            event_kind=EventKind.BUNSHIN_ARCHITECTURE_REVIEW_PENDING,
            source_kind=SourceKind.BUNSHIN,
            payload={
                "delivery_id": "delivery-1",
                "workflow_id": "workflow-1",
                "architecture_revision_id": "revision-1",
                "manifest_sha": "sha-1",
                "decision_token": "token-1",
                "bunshin_v2": True,
                "markdown": "Review",
                "attachments": [
                    {"path": "first.md"},
                    {"path": "second.md"},
                ],
                "route": {
                    "endpoint_id": "telegram",
                    "channel_kind": "telegram",
                    "reply_target": {"chat_id": "42"},
                },
            },
        )
        handler = BunshinControlEventHandler(provider=provider)

        await handler.handle(event, context)
        self.assertEqual(core.calls, ["first.md", "second.md"])
        self.assertEqual(provider.parts, {"attachment:0"})
        self.assertEqual(provider.settlements, [False])

        core.calls.clear()
        await handler.handle(event, context)

        self.assertEqual(core.calls, ["second.md", "primary"])
        self.assertEqual(
            provider.parts,
            {"attachment:0", "attachment:1", "primary"},
        )
        self.assertEqual(provider.settlements, [False, True])


if __name__ == "__main__":
    unittest.main()
