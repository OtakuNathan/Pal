from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from pal.minion.harness_request import compile_architect_harness_request
from pal.minion.harnesses import (
    CODEX_ARCHITECT_HARNESS_ID,
    HARNESS_LAUNCH_HOST,
    HARNESS_PROTOCOL_VERSION,
    MinionHarnessRegistry,
    MinionHarnessSpec,
    PAL_HARNESS_ID,
)
from pal.minion.manager import MinionManager
from pal.minion.v2.semantic_orchestration.orchestrator import (
    _select_attempt_harness,
)
from pal.shared import MinionInvocationPack
from plugins.codex_architect_harness.codex_architect_worker import (
    CodexAppServer,
    CodexArchitectWorker,
)


def _codex_spec(root: Path, *, priority: int = 100) -> MinionHarnessSpec:
    worker = root / "worker.py"
    worker.write_text("# worker\n", encoding="utf-8")
    return MinionHarnessSpec(
        harness_id=CODEX_ARCHITECT_HARNESS_ID,
        protocol_version=HARNESS_PROTOCOL_VERSION,
        supported_roles=("architect",),
        priority=priority,
        launch_kind=HARNESS_LAUNCH_HOST,
        worker_argv=("/usr/bin/python3", str(worker)),
        config={"codex_bin": "/usr/bin/codex"},
    )


class MinionHarnessRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal-harness-"))

    def test_codex_is_preferred_only_for_architect_and_pal_is_fallback(self) -> None:
        registry = MinionHarnessRegistry(include_pal=True)
        registry.register(_codex_spec(self.root))
        generation = registry.snapshot()

        self.assertEqual(
            generation.select("architect").harness_id,
            CODEX_ARCHITECT_HARNESS_ID,
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
                        "harness_id": CODEX_ARCHITECT_HARNESS_ID,
                        "status": "failed",
                    },
                ),
            ).harness_id,
            CODEX_ARCHITECT_HARNESS_ID,
        )
        self.assertEqual(
            _select_attempt_harness(
                generation,
                role="architect",
                prior_attempts=(
                    {
                        "harness_id": CODEX_ARCHITECT_HARNESS_ID,
                        "status": "failed",
                    },
                    {
                        "harness_id": CODEX_ARCHITECT_HARNESS_ID,
                        "status": "interrupted",
                    },
                ),
            ).harness_id,
            PAL_HARNESS_ID,
        )

    def test_detach_replaces_generation_without_mutating_captured_generation(
        self,
    ) -> None:
        registry = MinionHarnessRegistry(include_pal=True)
        registry.register(_codex_spec(self.root))
        captured = registry.snapshot()

        registry.unregister(CODEX_ARCHITECT_HARNESS_ID)

        self.assertEqual(
            captured.select("architect").harness_id,
            CODEX_ARCHITECT_HARNESS_ID,
        )
        self.assertEqual(
            registry.snapshot().select("architect").harness_id,
            PAL_HARNESS_ID,
        )

    def test_listener_failure_rolls_back_the_complete_generation(self) -> None:
        registry = MinionHarnessRegistry(include_pal=True)
        original = registry.snapshot()
        calls: list[str] = []

        def listener(generation) -> None:
            calls.append(generation.generation_hash)
            if generation.generation_hash != original.generation_hash:
                raise RuntimeError("manager rejected generation")

        registry.subscribe(listener)
        with self.assertRaisesRegex(RuntimeError, "manager rejected"):
            registry.register(_codex_spec(self.root))

        self.assertEqual(
            registry.snapshot().generation_hash,
            original.generation_hash,
        )
        self.assertEqual(calls[-1], original.generation_hash)

    def test_manager_atomically_recompiles_an_external_registry(self) -> None:
        manager = MinionManager(self.root / "runtime")
        external = MinionHarnessRegistry()
        external.register(_codex_spec(self.root))

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
            CODEX_ARCHITECT_HARNESS_ID,
        )
        self.assertEqual(
            manager.harness_registry.snapshot().select(
                "verifier"
            ).harness_id,
            PAL_HARNESS_ID,
        )


class ArchitectHarnessRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal-harness-request-"))
        self.architect_path = self.root / "architect.yaml"

    def pack(self, *, behavior: str = "Design a complete contract.") -> MinionInvocationPack:
        return MinionInvocationPack(
            invocation_id="architect-session",
            instruction="Design the requested system.",
            acceptance_criteria=["No implementation bodies."],
            workspace={
                "repo_path": str(self.root),
                "architect_path": str(self.architect_path),
                "reference_paths": [
                    {"name": "task", "path": str(self.root / "task.yaml")}
                ],
            },
            resolved_profile={
                "identity_fragment": "You are a named Pal role.",
                "behavior_fragment": behavior,
                "output_contract_fragment": "Submit only the contract.",
            },
            metadata={
                "minion_v2": {
                    "role": "architect",
                    "role_protocol": {
                        "playbook": {
                            "steps": [
                                {
                                    "key": "requirements",
                                    "instruction": "Read requirements.",
                                    "done_when": "Requirements are consistent.",
                                }
                            ]
                        }
                    },
                    "work_item_seed": [
                        {
                            "item_id": "phase:requirements",
                            "kind": "phase",
                            "summary": "requirements",
                            "required": True,
                        }
                    ],
                }
            },
        )

    def test_compiler_omits_identity_and_pal_tool_protocol(self) -> None:
        request = compile_architect_harness_request(self.pack())

        self.assertNotIn("named Pal role", request.developer_instructions)
        self.assertNotIn("contract_submit", request.developer_instructions)
        self.assertIn("Design a complete contract", request.developer_instructions)
        self.assertIn(str(self.architect_path), request.user_input)
        self.assertEqual(request.cwd, self.root)
        self.assertEqual(request.work_item_seed[0]["kind"], "phase")

    def test_compiler_rejects_a_pal_specific_profile_contract(self) -> None:
        with self.assertRaisesRegex(ValueError, "Pal tool names"):
            compile_architect_harness_request(
                self.pack(behavior="call ask_question and wait")
            )


class CodexAppServerProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_preserves_notifications_that_precede_the_response(
        self,
    ) -> None:
        server = CodexAppServer(
            codex_bin="/usr/bin/codex",
            cwd=Path("/tmp"),
            effort="high",
            timeout_seconds=10,
        )
        server.write = AsyncMock()
        messages = iter(
            (
                {
                    "method": "turn/plan/updated",
                    "params": {"plan": []},
                },
                {"id": 1, "result": {"ok": True}},
            )
        )

        async def read_wire():
            return next(messages)

        server._read_wire = read_wire
        response = await server.request("turn/start", {})

        self.assertEqual(response["result"], {"ok": True})
        self.assertEqual(
            (await server.read())["method"],
            "turn/plan/updated",
        )

    async def test_native_question_round_trips_through_manager_clarification(
        self,
    ) -> None:
        worker = object.__new__(CodexArchitectWorker)
        worker.controls = asyncio.Queue()
        worker.run_id = "run-1"
        worker.minion_id = "architect-session"
        worker.workflow_id = "workflow-1"
        worker.pack = MinionInvocationPack(invocation_id="architect-session")
        events: list[tuple[str, dict]] = []

        async def emit(kind: str, payload):
            events.append((kind, dict(payload)))
            if kind == "clarification_requested":
                await worker.controls.put(
                    {
                        "type": "clarification",
                        "clarification": {
                            "clarification_id": payload["clarification_id"],
                            "answers": [{"answer": "Use option A"}],
                        },
                    }
                )

        worker.emit = emit
        server = unittest.mock.Mock()
        server.result_response = AsyncMock()
        server.error_response = AsyncMock()
        message = {"id": 41}
        params = {
            "questions": [
                {
                    "id": "choice",
                    "header": "Boundary",
                    "question": "Which boundary?",
                    "options": [
                        {"label": "A", "description": "Use A."},
                        {"label": "B", "description": "Use B."},
                    ],
                }
            ]
        }

        await worker._handle_question(server, message, params)

        server.result_response.assert_awaited_once_with(
            41,
            {"answers": {"choice": {"answers": ["Use option A"]}}},
        )
        self.assertEqual(
            [kind for kind, _payload in events],
            ["clarification_requested", "clarification_received"],
        )


if __name__ == "__main__":
    unittest.main()
