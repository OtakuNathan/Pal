from __future__ import annotations

import asyncio
import tempfile
import sys
import unittest
from pathlib import Path

from pal.bunshin.harnesses import (
    HARNESS_LAUNCH_HOST,
    HARNESS_PROTOCOL_VERSION,
    builtin_harness_generation,
    single_harness_generation,
    BunshinHarnessSpec,
    PAL_HARNESS_ID,
)
from pal.bunshin.manager import BunshinManager


EXTERNAL_HARNESS_ID = "test_architect"


def _external_spec(root: Path, *, priority: int = 100) -> BunshinHarnessSpec:
    worker = root / "worker.py"
    worker.write_text("# worker\n", encoding="utf-8")
    return BunshinHarnessSpec(
        harness_id=EXTERNAL_HARNESS_ID,
        protocol_version=HARNESS_PROTOCOL_VERSION,
        supported_roles=("architect",),
        priority=priority,
        launch_kind=HARNESS_LAUNCH_HOST,
        worker_argv=(sys.executable, str(worker)),
        config={},
    )


class BunshinHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="pal-harness-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_builtin_generation_runs_every_role_with_pal(self) -> None:
        generation = builtin_harness_generation()
        self.assertEqual([spec.harness_id for spec in generation.specs], [PAL_HARNESS_ID])
        for role in ("architect", "reviewer", "implementation", "verifier"):
            with self.subTest(role=role):
                spec = generation.select(role)
                self.assertEqual(spec.harness_id, PAL_HARNESS_ID)
                self.assertEqual(spec.worker_argv[1:], ("-m", "pal.bunshin.worker_main"))

    def test_legacy_registry_sync_reports_only_effective_builtin_harness(self) -> None:
        manager = BunshinManager(self.root / "runtime")
        external = single_harness_generation(_external_spec(self.root))
        result = asyncio.run(manager._call_method(
            "replace_harness_registry", {"generation": external.to_dict()},
        ))
        self.assertEqual(result["mode"], "builtin_only")
        self.assertEqual([item["harness_id"] for item in result["generation"]["specs"]], [PAL_HARNESS_ID])
