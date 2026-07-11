from __future__ import annotations

import asyncio
import base64
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pal.artifact import ArtifactManager
from pal.artifact.models import ArtifactHotStateModel, ArtifactRecordModel, ArtifactRepresentationModel
from pal.artifact.repository import ArtifactRepository
from pal.core import PalCore
from pal.execution import register_with_core as register_execution_with_core
from pal.foundation import PalV2Database
from pal.llm import CanonicalToolCall
from pal.web_fetch import WebScreenshotResult, WebScreenshotTool, register_with_core as register_web_fetch_with_core


_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class _FakeScreenshotService:
    def __init__(self, runtime_root: Path) -> None:
        self.browser_manager = SimpleNamespace(runtime_root=runtime_root)
        self.requests = []

    def screenshot(self, request):
        self.requests.append(request)
        return WebScreenshotResult(
            requested_url=request.url,
            final_url="https://example.com/final",
            title="Example",
            png_bytes=_PNG_1X1,
            configured_provider_id="playwright_fetch_default",
            effective_provider_id="playwright_fetch_default",
            status_code=200,
            full_page=request.full_page,
            viewport_width=request.viewport_width,
            viewport_height=request.viewport_height,
        )

    def list_providers(self) -> list:
        return []

    def shutdown_sync(self) -> None:
        return None

    async def shutdown_async(self) -> None:
        return None


class _FakeTurnIO:
    def __init__(self, scope_key: str) -> None:
        self.scope_key = scope_key

    def artifact_scope_for_turn(self, turn_id: str | None) -> str:
        _ = turn_id
        return self.scope_key


class WebScreenshotToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal_web_screenshot_test_"))
        self.database = PalV2Database(self.root / "pal_web_screenshot.sqlite3")
        self.database.initialize([ArtifactRecordModel, ArtifactRepresentationModel, ArtifactHotStateModel])
        self.manager = ArtifactManager(runtime_root=self.root, repository=ArtifactRepository())
        self.scope_key = "socket:conversation:1"
        self.turn_id = "turn-web-shot"

    def tearDown(self) -> None:
        self.database.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_screenshot_registers_conversation_artifact_and_returns_local_path(self) -> None:
        service = _FakeScreenshotService(self.root)
        tool = WebScreenshotTool(service=service, artifact_manager=self.manager)
        runtime = SimpleNamespace(
            runtime_root=self.root,
            provider_registry={"core:turn_io": _FakeTurnIO(self.scope_key), "artifact:artifact": self.manager},
        )

        result = asyncio.run(
            tool.ainvoke(
                {"url": "https://example.com", "full_page": True, "viewport_width": 1024, "viewport_height": 768},
                runtime=runtime,
                turn_id=self.turn_id,
            )
        )

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.structured["registered_artifact"])
        self.assertTrue(str(result.structured["artifact_id"]).startswith("art_"))
        self.assertTrue(Path(result.structured["local_cached_path"]).is_file())
        self.assertEqual(result.structured["final_url"], "https://example.com/final")
        self.assertEqual(service.requests[0].viewport_width, 1024)
        info = self.manager.info(result.structured["artifact_id"], self.scope_key)
        self.assertEqual(info["artifact"]["kind"], "image")

    def test_screenshot_falls_back_to_stored_file_without_artifact_scope(self) -> None:
        service = _FakeScreenshotService(self.root)
        tool = WebScreenshotTool(service=service)
        runtime = SimpleNamespace(runtime_root=self.root, provider_registry={})

        result = asyncio.run(tool.ainvoke({"url": "https://example.com"}, runtime=runtime, turn_id=""))

        self.assertEqual(result.status, "ok")
        self.assertFalse(result.structured["registered_artifact"])
        self.assertTrue(str(result.structured["artifact_id"]).startswith("artifact_"))
        self.assertTrue(Path(result.structured["local_cached_path"]).is_file())

    def test_tool_call_reaches_async_screenshot_tool(self) -> None:
        service = _FakeScreenshotService(self.root)
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")
        runtime = core.context.execution_runtime
        register_web_fetch_with_core(core.context, service)
        core.publish_module_capabilities("web_fetch")

        result = asyncio.run(
            runtime.execute_tool_async(
                CanonicalToolCall(
                    name="op_tool_call",
                    args={"name": "op_web_screenshot", "args": {"url": "https://example.com"}},
                ),
                turn_id=self.turn_id,
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.structured["final_url"], "https://example.com/final")
        self.assertTrue(Path(result.structured["local_cached_path"]).is_file())
        self.assertEqual(service.requests[0].url, "https://example.com")


if __name__ == "__main__":
    unittest.main()
