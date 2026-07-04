from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pal.bootstrap import compose_runtime
from pal.channel.channel_endpoint_queue_base import ChannelEndpointQueueBase
from pal.channel.contracts import EndpointConfig, ResponseHandle
from pal.shared import EventKind
from pal.wizard import WizardService


@dataclass
class _MemoryEndpoint(ChannelEndpointQueueBase):
    sent_replies: list[str] = field(default_factory=list)
    sent_statuses: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def normalize_raw(self, payload: Any) -> dict[str, Any]:
        return dict(payload or {})

    def send_reply(self, response_handle: ResponseHandle, text: str) -> None:
        _ = response_handle
        self.sent_replies.append(str(text))

    def send_status(self, response_handle: ResponseHandle, kind: str, payload: dict[str, Any]) -> None:
        _ = response_handle
        self.sent_statuses.append((str(kind), dict(payload or {})))

    def inspect_health(self) -> dict[str, Any]:
        return {"healthy": True}

    def inspect_auth_state(self) -> dict[str, Any]:
        return {"paired": True}


async def _drive_until_reply_count(handle, endpoint: _MemoryEndpoint, count: int, *, max_ticks: int = 1000) -> None:
    for _ in range(max_ticks):
        await handle.core.run_until_idle_async(max_iterations=128)
        handle.channel_runtime.sync_endpoints()
        if len(endpoint.sent_replies) >= count:
            return
        await asyncio.sleep(0.001)
    raise AssertionError(f"timed out waiting for {count} replies; got {len(endpoint.sent_replies)}")


def _assert_runtime_drained(testcase: unittest.TestCase, handle, endpoint: _MemoryEndpoint) -> None:
    handle.channel_runtime.sync_endpoints()
    testcase.assertFalse(handle.core.state.active_turns)
    testcase.assertFalse(handle.core.state.turn_tasks)
    testcase.assertFalse(handle.core.state.turn_scopes)
    testcase.assertFalse(handle.core.state.active_channel_turn_id)
    testcase.assertFalse(handle.core.state.pending_channel_turns)
    for scope in handle.core.state.control_scopes.values():
        testcase.assertFalse(scope.active_turn_id)
    testcase.assertFalse(endpoint.mailbox.has_pending())
    testcase.assertFalse(endpoint.has_queued_replies())
    testcase.assertFalse(endpoint.has_queued_stream_events())
    testcase.assertFalse(endpoint.has_queued_status())


class RuntimeStabilitySoakTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal_stability_soak_"))
        self.wizard = WizardService()
        self.provisioned = self.wizard.provision_stub_runtime(self.runtime_root)
        self.handle = compose_runtime(
            wizard=self.wizard,
            registration=self.provisioned.registration,
            database=self.provisioned.database,
        )
        self.endpoint = _MemoryEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="memory_soak",
                channel_kind="memory",
                binding_key="memory://soak",
            )
        )
        self.handle.channel_runtime.register_endpoint(self.endpoint)

    def tearDown(self) -> None:
        try:
            asyncio.run(self.handle.stop_async())
        finally:
            shutil.rmtree(self.runtime_root, ignore_errors=True)

    def test_many_sequential_channel_turns_drain_without_watchdog(self) -> None:
        async def scenario() -> None:
            for index in range(30):
                self.endpoint.accept_raw(
                    {"text": f"stability sequential turn {index}"},
                    event_kind=EventKind.USER_MESSAGE,
                    reply_target={"session_id": "sequential"},
                )
                await _drive_until_reply_count(self.handle, self.endpoint, index + 1)
                _assert_runtime_drained(self, self.handle, self.endpoint)

        asyncio.run(scenario())

        self.assertEqual(len(self.endpoint.sent_replies), 30)

    def test_burst_same_scope_channel_turns_queue_and_drain_without_watchdog(self) -> None:
        async def scenario() -> None:
            for index in range(12):
                self.endpoint.accept_raw(
                    {"text": f"stability burst turn {index}"},
                    event_kind=EventKind.USER_MESSAGE,
                    reply_target={"session_id": "burst"},
                )
            await _drive_until_reply_count(self.handle, self.endpoint, 12)
            _assert_runtime_drained(self, self.handle, self.endpoint)

        asyncio.run(scenario())

        self.assertEqual(len(self.endpoint.sent_replies), 12)


if __name__ == "__main__":
    unittest.main()
