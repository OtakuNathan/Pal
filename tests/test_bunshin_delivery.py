from __future__ import annotations
from pal.shared.ports import PortRegistry

import unittest
from types import SimpleNamespace

from pal.foundation import EventEnvelope
from pal.bunshin.source import BunshinControlEventHandler, _event_kind
from pal.shared import EventKind, SourceKind


class _Provider:
    def __init__(self) -> None:
        self.parts: set[str] = set()
        self.settlements: list[bool] = []

    def may_send_event(self, event):
        return True

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
        return True


class _Core:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.deliveries: list[object] = []
        self.fail_second_attachment_once = True

    async def deliver(self, delivery, *, require_provider):
        self.assert_require_provider(require_provider)
        self.deliveries.append(delivery)
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
        context = SimpleNamespace(port_registry=PortRegistry())
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

        await BunshinControlEventHandler(provider=provider, controls=core).handle(event, context)

        self.assertEqual(core.calls, ["primary"])
        delivery = core.deliveries[0]
        self.assertEqual(delivery.delivery_kind, "interactive_resolve")
        self.assertEqual(
            delivery.interaction.interaction_id,
            "bunshin_v2_architecture_revision-1",
        )
        self.assertEqual(provider.settlements, [True])

    async def test_terminal_closes_worker_owned_pending_interactions(self) -> None:
        provider = _Provider()
        core = _Core()
        core.fail_second_attachment_once = False
        context = SimpleNamespace(port_registry=PortRegistry())
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
        handler = BunshinControlEventHandler(provider=provider, controls=core)

        await handler.handle(event, context)

        self.assertEqual(
            [delivery.delivery_kind for delivery in core.deliveries],
            ["interactive_resolve", "interactive_resolve", "reply"],
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
        context = SimpleNamespace(port_registry=PortRegistry())
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

        await BunshinControlEventHandler(provider=provider, controls=core).handle(event, context)

        self.assertEqual(core.calls, ["/tmp/result.patch", "primary"])
        self.assertEqual(provider.parts, {"attachment:0", "primary"})
        self.assertEqual(provider.settlements, [True])

    async def test_resolved_clarification_closes_the_original_interaction(self) -> None:
        provider = _Provider()
        core = _Core()
        core.fail_second_attachment_once = False
        context = SimpleNamespace(port_registry=PortRegistry())
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
        handler = BunshinControlEventHandler(provider=provider, controls=core)

        await handler.handle(event, context)

        self.assertEqual(core.calls, ["primary"])
        self.assertEqual(provider.parts, {"primary"})
        self.assertEqual(provider.settlements, [True])

    async def test_retry_skips_parts_already_accepted_by_channel(self) -> None:
        provider = _Provider()
        core = _Core()
        context = SimpleNamespace(port_registry=PortRegistry())
        event = EventEnvelope(
            event_kind=EventKind.BUNSHIN_ARCHITECTURE_REVIEW_PENDING,
            source_kind=SourceKind.BUNSHIN,
            payload={
                "delivery_id": "delivery-1",
                "workflow_id": "workflow-1",
                "architecture_revision_id": "revision-1",
                "manifest_sha": "sha-1",
                "decision_token": "token-1",
                "bunshin": True,
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
        handler = BunshinControlEventHandler(provider=provider, controls=core)

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

    async def test_retry_after_manager_ack_failure_preserves_accepted_part(self):
        provider, core = _Provider(), _Core()
        core.fail_second_attachment_once = False
        context = SimpleNamespace(port_registry=PortRegistry())
        event = EventEnvelope(event_kind=EventKind.BUNSHIN_TERMINAL, source_kind=SourceKind.BUNSHIN,
            payload={"delivery_id": "ack-failure", "summary": "done",
                "route": {"endpoint_id": "telegram", "channel_kind": "telegram"},
                "attachments": [{"path": "first.md"}, {"path": "second.md"}]})
        acknowledge = provider.settle_event_part
        def lost_ack(event, part):
            acknowledge(event, part)
            return False
        provider.settle_event_part = lost_ack
        handler = BunshinControlEventHandler(provider, core)
        await handler.handle(event, context)
        self.assertEqual(core.calls, ["first.md"])
        self.assertEqual(provider.settlements, [False])
        provider.settle_event_part = acknowledge
        core.calls.clear()
        await handler.handle(event, context)
        self.assertEqual(core.calls, ["second.md", "primary"])
        self.assertEqual(provider.settlements, [False, True])


if __name__ == "__main__":
    unittest.main()


class BunshinMemoryProposalDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_proposal_uses_host_review_once_and_preserves_task_route(self):
        from pal.memory.service import MemoryService
        memory = MemoryService()
        provider, core = _Provider(), _Core()
        core.fail_second_attachment_once = False
        context = SimpleNamespace(port_registry=PortRegistry({"memory:memory": memory}))
        payload = {"delivery_id": "d", "task_id": "t", "workflow_id": "w", "invocation_id": "i",
            "status": "completed", "summary": "done",
            "route": {"endpoint_id": "socket", "channel_kind": "socket", "reply_target": {"session_id": "task"}},
            "memory_candidates": [{"kind": "fact", "title": "test", "summary": "test", "source_excerpt": "source"}]}
        event = EventEnvelope(event_kind=EventKind.BUNSHIN_TERMINAL, source_kind=SourceKind.BUNSHIN, payload=payload)
        handler = BunshinControlEventHandler(provider, core)
        await handler.handle(event, context)
        self.assertEqual(provider.settlements, [True])
        self.assertIn("memory_proposals", provider.parts)
        proposal = next(delivery for delivery in core.deliveries if delivery.delivery_kind == "interactive_open")
        self.assertEqual(proposal.route.reply_target, {"session_id": "task"})
        state = memory.reviews.get(proposal.interaction.interaction_id, proposal.route)
        self.assertEqual(state["source"]["task_id"], "t")
        self.assertEqual(state["drafts"][0]["decision"], "pending")
        await handler.handle(event, context)
        self.assertEqual(len(core.deliveries), 2)
