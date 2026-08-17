from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pal.channel.contracts import EndpointConfig
from pal.core.contracts import CoreRuntimeState
from pal.core.debug_dump import build_runtime_debug_snapshot, write_runtime_debug_dump


class _Mailbox:
    def __init__(self, size: int) -> None:
        self._items = tuple(range(size))

    def peek_all(self) -> tuple[int, ...]:
        return self._items


class _Endpoint:
    endpoint = EndpointConfig(endpoint_id="telegram", channel_kind="telegram", binding_key="hidden")
    enabled = True
    attached = True
    paired = True
    mailbox = _Mailbox(2)
    outbox = [object()]
    attachment_outbox = []
    status_outbox = [object()]
    stream_update_outbox = []
    last_delivery_error = ""

    def inspect_health(self) -> dict[str, object]:
        return {
            "healthy": False,
            "last_poll_error": "https://api.telegram.org/file/bot123456:SECRET/photos/file.jpg",
            "token": "SECRET",
        }

    def inspect_backlog(self) -> dict[str, object]:
        return {"source_url": "https://api.telegram.org/file/bot123456:SECRET/photos/file.jpg"}


class DebugDumpTests(unittest.TestCase):
    def test_snapshot_includes_runtime_queues_and_redacts_secrets(self) -> None:
        handle = self._fake_handle()

        snapshot = build_runtime_debug_snapshot(handle, app_snapshot={"loop_iterations": 7})

        self.assertEqual(snapshot["runtime_app"]["loop_iterations"], 7)
        self.assertEqual(snapshot["core"]["main_loop_queue_size"], 1)
        self.assertIn("fd_leases", snapshot["llm"])
        self.assertIn("active_count", snapshot["llm"]["fd_leases"])
        self.assertIn("channel.mailbox", snapshot["core"]["event_sources"])
        self.assertEqual(snapshot["channel"]["runtime_queues"]["mailbox_size"], 3)
        endpoint = snapshot["channel"]["endpoints"][0]
        self.assertEqual(endpoint["endpoint_id"], "telegram")
        self.assertIn("bot<redacted>", endpoint["health"]["last_poll_error"])
        self.assertEqual(endpoint["health"]["token"], "<redacted>")
        self.assertEqual(endpoint["backlog"]["source_url"], "<redacted>")
        self.assertNotIn("SECRET", json.dumps(snapshot, ensure_ascii=False))

    def test_write_runtime_debug_dump_appends_json_payload(self) -> None:
        handle = self._fake_handle()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "pal-debug.log"

            written = write_runtime_debug_dump(handle, app_snapshot={"loop_iterations": 1}, path=path)

            self.assertEqual(written, path)
            content = path.read_text(encoding="utf-8")
            self.assertIn("PAL DEBUG DUMP", content)
            self.assertIn('"loop_iterations": 1', content)
            self.assertNotIn("SECRET", content)

    def _fake_handle(self):
        state = CoreRuntimeState()
        core = SimpleNamespace(
            main_loop=SimpleNamespace(queue=[object()]),
            context=SimpleNamespace(
                event_source_registry=SimpleNamespace(sources={"channel.mailbox": object()}),
                event_handler_registry=SimpleNamespace(handlers={"user.message": [object()]}),
            ),
            state=state,
        )
        channel_runtime = SimpleNamespace(
            mailbox=_Mailbox(3),
            outbox=[],
            attachment_outbox=[],
            status_outbox=[],
            stream_update_outbox=[],
            list_endpoints=lambda: (_Endpoint(),),
        )
        proactive_manager = SimpleNamespace(
            registered={},
            trigger_mailbox=_Mailbox(0),
            schedule_engine=SimpleNamespace(next_due_by_proactive_id={}),
        )
        return SimpleNamespace(
            core=core,
            channel_runtime=channel_runtime,
            proactive_manager=proactive_manager,
            proactive_repository=None,
            registration=SimpleNamespace(runtime=SimpleNamespace(runtime_root=Path("."))),
        )


if __name__ == "__main__":
    unittest.main()
