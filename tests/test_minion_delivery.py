from __future__ import annotations

import unittest
from types import SimpleNamespace

from pal.foundation import EventEnvelope
from pal.minion.source import MinionControlEventHandler
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
        self.fail_second_attachment_once = True

    async def handle_control_action_async(self, action, *, require_provider):
        self.assert_require_provider(require_provider)
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


class MinionCompositeDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_skips_parts_already_accepted_by_channel(self) -> None:
        provider = _Provider()
        core = _Core()
        context = SimpleNamespace(port_registry={"core:core": core})
        event = EventEnvelope(
            event_kind=EventKind.MINION_ARCHITECTURE_REVIEW_PENDING,
            source_kind=SourceKind.MINION,
            payload={
                "delivery_id": "delivery-1",
                "workflow_id": "workflow-1",
                "architecture_revision_id": "revision-1",
                "manifest_sha": "sha-1",
                "decision_token": "token-1",
                "minion_v2": True,
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
        handler = MinionControlEventHandler(provider=provider)

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
