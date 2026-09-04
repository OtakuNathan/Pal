from __future__ import annotations

import asyncio
import base64
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pal.core import PalCore
from pal.execution import register_with_core as register_execution_with_core
from pal.foundation import PalV2Database
from pal.shared.tool_protocol import new_tool_call
from pal.web_fetch import BrowserScreenshotTool, register_with_core as register_web_fetch_with_core


_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class _FakeBrowserService:
    def __init__(self, runtime_root: Path) -> None:
        self.browser_manager = SimpleNamespace(runtime_root=runtime_root)
        self.calls: list[dict[str, object]] = []

    def execute(self, **kwargs):
        self.calls.append(dict(kwargs))
        assert kwargs["action"] == "screenshot"
        return {
            "png_base64": base64.b64encode(_PNG_1X1).decode("ascii"),
            "page": {"url": "https://example.com/final", "title": "Example"},
            "session": {"persistent": bool(kwargs["persistent"]), "running": True},
        }

    def health(self):
        return {"healthy": True, "service_running": False, "reason": "idle"}

    def shutdown_sync(self) -> None:
        return None

    async def shutdown_async(self) -> None:
        return None


class BrowserScreenshotToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal_browser_screenshot_test_"))
        self.database = PalV2Database(self.root / "pal.sqlite3")
        self.database.initialize([])

    def tearDown(self) -> None:
        self.database.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_screenshot_is_stored_as_managed_artifact(self) -> None:
        service = _FakeBrowserService(self.root)
        result = asyncio.run(
            BrowserScreenshotTool(service=service).ainvoke(
                {"full_page": True},
                session_key="a" * 64,
                persistent=True,
                runtime=SimpleNamespace(runtime_root=self.root),
                turn_id="turn-browser-shot",
            )
        )

        self.assertEqual(result.status, "ok")
        artifact = result.structured["artifact"]
        self.assertTrue(str(artifact["stored_artifact_id"]).startswith("artifact_"))
        self.assertTrue(Path(artifact["local_cached_path"]).is_file())
        self.assertNotIn("png_base64", result.structured)
        self.assertEqual(service.calls[0]["session_key"], "a" * 64)

    def test_call_tool_reaches_browser_screenshot_with_conversation_scope(self) -> None:
        service = _FakeBrowserService(self.root)
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")
        register_web_fetch_with_core(core.context, service)  # type: ignore[arg-type]
        core.publish_module_capabilities("web_fetch")

        result = asyncio.run(
            core.context.execution_runtime.execute_tool_async(
                new_tool_call(
                    name="call_tool",
                    args={"name": "browser_screenshot", "args": {"full_page": False}},
                ),
                turn_id="turn-browser-shot",
            )
        )

        self.assertTrue(result.ok, result.llm_text)
        self.assertEqual(result.status, "ok")
        self.assertTrue(Path(result.structured["artifact"]["local_cached_path"]).is_file())
        self.assertEqual(len(str(service.calls[0]["session_key"])), 64)

    def test_old_screenshot_alias_is_not_registered(self) -> None:
        service = _FakeBrowserService(self.root)
        core = PalCore()
        register_execution_with_core(core.context)
        register_web_fetch_with_core(core.context, service)  # type: ignore[arg-type]
        core.publish_module_capabilities("web_fetch")

        self.assertIn("browser_screenshot", core.context.capability_registry.descriptors)
        self.assertNotIn("screenshot_web", core.context.capability_registry.descriptors)


if __name__ == "__main__":
    unittest.main()
