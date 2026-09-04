from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pal.core import PalCore
from pal.core.resident_checkpoint import ResidentCheckpointStore
from pal.core.runtime_config import RuntimeConfig
from pal.execution import register_with_core as register_execution_with_core
from pal.memory import (
    MemoryService,
    register_with_core as register_memory_with_core,
)
from pal.runtime_app import PalRuntimeApp


def _build_app(root: Path) -> PalRuntimeApp:
    core = PalCore(config=RuntimeConfig(runtime_root=root))
    core.context.execution_runtime.runtime_root = root
    memory_service = MemoryService()
    register_execution_with_core(core.context)
    register_memory_with_core(core.context, memory_service)
    handle = SimpleNamespace(
        core=core,
        memory_service=memory_service,
        registration=SimpleNamespace(
            runtime=SimpleNamespace(runtime_root=root),
        ),
    )
    return PalRuntimeApp(handle=handle)


class ResidentCheckpointTests(unittest.TestCase):
    def test_checkpoint_is_encrypted_and_restores_then_consumes_l1(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = _build_app(root)
            source.handle.memory_service.begin_l1_turn(
                "turn-1",
                user_text="resident checkpoint secret",
            )
            source.handle.memory_service.settle_l1_turn("turn-1")
            asyncio.run(source._publish_checkpoint_async())

            path = ResidentCheckpointStore(root).path
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("resident checkpoint secret", raw)
            self.assertEqual(json.loads(raw)["cipher"], "fernet")

            restored = _build_app(root)
            asyncio.run(restored._restore_checkpoint_async())

            turn = restored.handle.memory_service.l1_store.turns.get("turn-1")
            self.assertIsNotNone(turn)
            self.assertEqual(turn.state.value, "settled")
            self.assertIn("resident checkpoint secret", repr(turn.messages))
            self.assertEqual(restored.last_checkpoint_status, "restored")
            self.assertFalse(path.exists())

    def test_restore_marks_an_active_resident_turn_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = _build_app(root)
            source.handle.memory_service.begin_l1_turn(
                "turn-active",
                user_text="unfinished request",
            )
            asyncio.run(source._publish_checkpoint_async())

            restored = _build_app(root)
            asyncio.run(restored._restore_checkpoint_async())

            turn = restored.handle.memory_service.l1_store.turns.get("turn-active")
            self.assertIsNotNone(turn)
            self.assertEqual(turn.state.value, "interrupted")
            self.assertEqual(
                turn.metadata["interrupt_reason"],
                "resident process restart",
            )

    def test_shutdown_saves_full_l1_without_calling_the_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = _build_app(root)
            app.handle.memory_service.begin_l1_turn(
                "turn-shutdown",
                user_text="keep this full L1",
            )
            app.handle.memory_service.settle_l1_turn("turn-shutdown")

            async def forbidden_compaction(*args, **kwargs):
                _ = args, kwargs
                self.fail("shutdown must not call the LLM compaction path")

            app.handle.core.turn_executor.compact_memory_async = forbidden_compaction
            asyncio.run(app._checkpoint_for_shutdown_async())

            self.assertEqual(app.last_checkpoint_status, "l1_saved")
            snapshot = ResidentCheckpointStore(root).read()
            self.assertEqual(snapshot["sequence"], 1)
            memory_payload = snapshot["modules"]["memory"]["payload"]
            self.assertEqual(
                memory_payload["l1_turns"][0]["turn_id"],
                "turn-shutdown",
            )
            self.assertIn(
                "keep this full L1",
                json.dumps(memory_payload, ensure_ascii=False),
            )


if __name__ == "__main__":
    unittest.main()
