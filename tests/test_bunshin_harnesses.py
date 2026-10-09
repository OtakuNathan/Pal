from __future__ import annotations

import asyncio
import tempfile
import sys
import unittest
from pathlib import Path

from pal.bunshin.harnesses import (
    HARNESS_LAUNCH_HOST,
    HARNESS_PROTOCOL_VERSION,
    BunshinHarnessRegistry,
    BunshinHarnessSpec,
    PAL_HARNESS_ID,
)
from pal.bunshin.manager import BunshinManager
from pal.bunshin.semantic_orchestration.role_environment import _select_attempt_harness


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


class BunshinHarnessRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="pal-harness-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_external_harness_is_preferred_only_for_architect_and_pal_is_fallback(self) -> None:
        registry = BunshinHarnessRegistry(include_pal=True)
        registry.register(_external_spec(self.root))
        generation = registry.snapshot()

        self.assertEqual(
            generation.select("architect").harness_id,
            EXTERNAL_HARNESS_ID,
        )
        self.assertEqual(
            generation.select("implementation").harness_id,
            PAL_HARNESS_ID,
        )
        self.assertEqual(
            _select_attempt_harness(
                generation,
                role="architect",
                prior_attempts=(
                    {
                        "harness_id": EXTERNAL_HARNESS_ID,
                        "status": "failed",
                    },
                ),
            ).harness_id,
            EXTERNAL_HARNESS_ID,
        )
        self.assertEqual(
            _select_attempt_harness(
                generation,
                role="architect",
                prior_attempts=(
                    {
                        "harness_id": EXTERNAL_HARNESS_ID,
                        "status": "failed",
                    },
                    {
                        "harness_id": EXTERNAL_HARNESS_ID,
                        "status": "lost",
                    },
                ),
            ).harness_id,
            PAL_HARNESS_ID,
        )

    def test_default_registry_runs_every_role_with_pal(self) -> None:
        generation = BunshinHarnessRegistry(include_pal=True).snapshot()
        self.assertEqual([spec.harness_id for spec in generation.specs], [PAL_HARNESS_ID])
        for role in ("architect", "reviewer", "implementation", "verifier"):
            with self.subTest(role=role):
                spec = generation.select(role)
                self.assertEqual(spec.harness_id, PAL_HARNESS_ID)
                self.assertEqual(spec.worker_argv[1:], ("-m", "pal.bunshin.worker_main"))

    def test_detach_replaces_generation_without_mutating_captured_generation(
        self,
    ) -> None:
        registry = BunshinHarnessRegistry(include_pal=True)
        registry.register(_external_spec(self.root))
        captured = registry.snapshot()

        registry.unregister(EXTERNAL_HARNESS_ID)

        self.assertEqual(
            captured.select("architect").harness_id,
            EXTERNAL_HARNESS_ID,
        )
        self.assertEqual(
            registry.snapshot().select("architect").harness_id,
            PAL_HARNESS_ID,
        )

    def test_listener_failure_rolls_back_the_complete_generation(self) -> None:
        registry = BunshinHarnessRegistry(include_pal=True)
        original = registry.snapshot()
        calls: list[str] = []

        def listener(generation) -> None:
            calls.append(generation.generation_hash)
            if generation.generation_hash != original.generation_hash:
                raise RuntimeError("manager rejected generation")

        registry.subscribe(listener)
        with self.assertRaisesRegex(RuntimeError, "manager rejected"):
            registry.register(_external_spec(self.root))

        self.assertEqual(
            registry.snapshot().generation_hash,
            original.generation_hash,
        )
        self.assertEqual(calls[-1], original.generation_hash)

    def test_manager_atomically_recompiles_an_external_registry(self) -> None:
        manager = BunshinManager(self.root / "runtime")
        external = BunshinHarnessRegistry()
        external.register(_external_spec(self.root))

        result = asyncio.run(
            manager._call_method(
                "replace_harness_registry",
                {"generation": external.snapshot().to_dict()},
            )
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            manager.harness_registry.snapshot().select(
                "architect"
            ).harness_id,
            EXTERNAL_HARNESS_ID,
        )
        self.assertEqual(
            manager.harness_registry.snapshot().select(
                "verifier"
            ).harness_id,
            PAL_HARNESS_ID,
        )
