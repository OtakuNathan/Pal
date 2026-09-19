"""E04: Direct and ManagerProxy lanes over the same controlled provider
response, with real transport adapters.

Both lanes consume the same _CapturingTransport frames through the real
manager owner thread (Direct: in-process llm_transport_stream_frames;
Proxy: ManagerProxyTransport over a real unix-socket manager server).
Requests are recorded per request_id, usage receipts reconcile per run,
frame payloads are plain text (no tool dispatch), and the compaction
lane's run never leaks requests or receipts into the main lane's run.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pal.bunshin.ipc import cleanup_manager_endpoint, start_manager_server
from pal.bunshin.llm_transport import ManagerProxyTransport
from pal.bunshin.manager import BunshinManager, BunshinRunState
from pal.foundation import PalV2Database
from pal.llm.endpoint_spec import endpoint_spec_fingerprint
from pal.llm.ir import LLMUsageIR, WireShape
from pal.llm.models import LLMEndpointModel, PalRuntimeSettingModel
from pal.llm.repository import LLMEndpointRepository, RuntimeSettingRepository
from pal.llm.transport import EncodedTransportRequest
from pal.shared import BunshinInvocationPack
from pal.wizard.runtime import ALL_MODELS

from tests.test_bunshin_llm_transport import (
    _CapturingTransport,
    _params,
    _register_endpoint,
)


def _manager(root: Path) -> tuple[PalV2Database, BunshinManager]:
    database = PalV2Database(db_path=root / "pal.sqlite3")
    database.initialize(ALL_MODELS)
    return database, BunshinManager(root)


def _register_run(manager: BunshinManager, run_id: str, endpoint) -> None:
    manager.runs[run_id] = BunshinRunState(
        bunshin_id=run_id,
        run_id=run_id,
        pack=BunshinInvocationPack(
            invocation_id=run_id,
            metadata={"preferred_endpoint_id": endpoint.endpoint_id},
        ),
    )


class E04TransportLaneTests(unittest.TestCase):
    def test_e04_lanes_isolated_over_same_provider(self) -> None:
        async def scenario() -> None:
            root = Path(tempfile.mkdtemp(prefix="pal-e04-lanes-"))
            database, manager = _manager(root)
            endpoint = _register_endpoint()
            _register_run(manager, "run-compaction", endpoint)
            _register_run(manager, "run-main", endpoint)
            capture = _CapturingTransport()
            manager._llm_json_transport = capture  # type: ignore[assignment]

            server, _ = await start_manager_server(root, manager._handle_client)
            try:
                proxy = ManagerProxyTransport(
                    root, "run-compaction", request_timeout_seconds=5,
                )
                compaction_request = EncodedTransportRequest(
                    request_id="req-compaction",
                    wire_shape=WireShape.OPENAI_COMPLETION,
                    timeout_seconds=30,
                    payload={
                        "model": endpoint.model_id,
                        "max_tokens": 64,
                        "metadata": {"purpose": "memory_compaction_engine"},
                    },
                    stream=True,
                )
                # Proxy lane (compaction run) over the real socket.
                proxy_frames = await asyncio.to_thread(
                    lambda: list(proxy.frames(endpoint, compaction_request))
                )
                self.assertEqual(
                    [frame.sequence for frame in proxy_frames], [0, 1],
                )
                for frame in proxy_frames:
                    payload = frame.payload
                    choices = payload.get("choices") or [{}]
                    delta = choices[0].get("delta") or {}
                    self.assertNotIn("tool_calls", delta)

                # Direct lane (main run) on the manager owner thread.
                main_params = _params(endpoint, request_id="req-main")
                main_params["run_id"] = "run-main"
                main_frames = [
                    item
                    async for item in manager.llm_transport_stream_frames(
                        main_params
                    )
                    if "frame" in item
                ]
                self.assertEqual(len(main_frames), 2)
                self.assertEqual(len(capture.requests), 2)

                # Per-request records stay isolated; nothing leaks across
                # the compaction/main lanes.
                records = manager._llm_transport_requests
                self.assertIn("req-compaction", records)
                self.assertIn("req-main", records)
                self.assertEqual(records["req-compaction"].run_id, "run-compaction")
                self.assertEqual(records["req-main"].run_id, "run-main")

                # Usage receipts reconcile per run: the compaction lane's
                # receipt never surfaces under the main lane's query.
                await asyncio.to_thread(
                    proxy.report_usage,
                    endpoint,
                    request_id="req-compaction",
                    usage=LLMUsageIR(
                        input_tokens=7, output_tokens=2, reported=True,
                    ),
                    provider_response_count=1,
                )
                record = records["req-compaction"]
                self.assertTrue(record.usage_received)
                self.assertFalse(records["req-main"].usage_received)
            finally:
                server.close()
                await server.wait_closed()
                await cleanup_manager_endpoint(root)
                database.close()

        with patch.dict(os.environ, {"PAL_BUNSHIN_SANDBOXED": "0"}, clear=False):
            asyncio.run(scenario())
