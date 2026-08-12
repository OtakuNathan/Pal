from __future__ import annotations

import asyncio
import copy
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from pal.core.module_registry import ModuleHandle, ModuleRegistry
from pal.core.runtime_state import (
    RuntimeSnapshotCoordinator,
    RuntimeSnapshotIdentity,
)
from pal.execution.runtime import ExecutionRuntime
from pal.execution.runtime_state import ExecutionRuntimeStatePort
from pal.execution.session_state import PagerHandleManifest
from pal.llm.ir import LLMMessageIR, MessageRole, MessageState, ReasoningPartIR, TextPartIR
from pal.memory.runtime_state import MemoryRuntimeStatePort
from pal.memory.service import MemoryService
from pal.shared.tool_protocol import ToolCallIR, ToolResultIR


@dataclass
class _RecordingPort:
    module_id: str
    state_order: int
    events: list[str]
    schema_version: str = "1"

    def snapshot_state(self):
        self.events.append(f"snapshot:{self.module_id}")
        return {"value": self.module_id}

    def prepare_restore_state(self, payload):
        self.events.append(f"prepare:{self.module_id}:{payload['value']}")
        return dict(payload)

    def install_prepared_state(self, prepared):
        self.events.append(f"install:{self.module_id}:{prepared['value']}")

    def reset_state(self, reason):
        self.events.append(f"reset:{self.module_id}:{reason}")


class RuntimeStateTests(unittest.TestCase):
    def test_coordinator_rejects_duplicate_runtime_state_port_ids(self) -> None:
        events: list[str] = []
        registry = ModuleRegistry()
        for handle_id in ("first", "second"):
            registry.register(
                ModuleHandle(
                    module_id=handle_id,
                    tier="test",
                    runtime_state_port=_RecordingPort("shared", 100, events),
                )
            )

        with self.assertRaisesRegex(
            ValueError,
            "duplicate runtime-state port module_id: shared",
        ):
            asyncio.run(
                RuntimeSnapshotCoordinator(registry).snapshot(
                    RuntimeSnapshotIdentity(
                        logical_coroutine_id="role-1",
                        workflow_id="wf-1",
                        stage_key="module:a",
                        sequence=1,
                        producer_fencing_token=1,
                        runtime_spec_hash="spec-1",
                    )
                )
            )

    def test_coordinator_orders_snapshot_restore_and_reverse_reset(self) -> None:
        events: list[str] = []
        registry = ModuleRegistry()
        for module_id, order in (("execution", 200), ("memory", 100)):
            registry.register(
                ModuleHandle(
                    module_id=module_id,
                    tier="test",
                    runtime_state_port=_RecordingPort(module_id, order, events),
                )
            )
        identity = RuntimeSnapshotIdentity(
            logical_coroutine_id="role-1",
            workflow_id="wf-1",
            stage_key="module:a",
            sequence=2,
            producer_fencing_token=3,
            runtime_spec_hash="spec-1",
        )
        coordinator = RuntimeSnapshotCoordinator(registry)
        snapshot = asyncio.run(coordinator.snapshot(identity))
        asyncio.run(coordinator.restore(snapshot, expected_identity=identity))
        asyncio.run(coordinator.reset("triage"))
        self.assertEqual(
            events,
            [
                "snapshot:memory",
                "snapshot:execution",
                "prepare:memory:memory",
                "prepare:execution:execution",
                "install:memory:memory",
                "install:execution:execution",
                "reset:execution:triage",
                "reset:memory:triage",
            ],
        )

    def test_memory_restore_drops_unmatched_protocol_and_in_progress_reasoning(self) -> None:
        service = MemoryService()
        service.begin_l1_turn("turn-1", user_text="work")
        service.upsert_l1_assistant(
            "turn-1",
            LLMMessageIR(
                role=MessageRole.ASSISTANT,
                state=MessageState.IN_PROGRESS,
                parts=(
                    ReasoningPartIR("unfinished private reasoning"),
                    TextPartIR("working"),
                    ToolCallIR(call_id="orphan", name="read_file", args={}),
                ),
            ),
        )
        payload = dict(MemoryRuntimeStatePort(service).snapshot_state())
        restored = MemoryService()
        memory_port = MemoryRuntimeStatePort(restored)
        memory_port.install_prepared_state(memory_port.prepare_restore_state(payload))
        turn = restored.l1_store.turns.turns[0]
        self.assertEqual(turn.state.value, "interrupted")
        self.assertNotIn("unfinished private reasoning", repr(turn.messages))
        self.assertFalse(
            any(
                isinstance(part, (ToolCallIR, ToolResultIR))
                for message in turn.messages
                for part in message.parts
            )
        )

    def test_memory_restore_migrates_legacy_closed_turn_projection(self) -> None:
        service = MemoryService()
        service.begin_l1_turn("turn-legacy", user_text="inspect")
        service.upsert_l1_assistant(
            "turn-legacy",
            LLMMessageIR(
                role=MessageRole.ASSISTANT,
                parts=(
                    ReasoningPartIR("legacy transient reasoning"),
                    TextPartIR("checking"),
                    ToolCallIR(call_id="read-1", name="read_file", args={}),
                ),
            ),
        )
        service.append_l1_tool_result(
            "turn-legacy",
            ToolResultIR(
                call_id="read-1",
                name="read_file",
                content="legacy full file result",
            ),
        )
        payload = copy.deepcopy(MemoryRuntimeStatePort(service).snapshot_state())
        payload["l1_turns"][0]["state"] = "settled"
        for message in payload["l1_turns"][0]["messages"]:
            for part in message["parts"]:
                if part["kind"] == "tool_result":
                    part.pop("lifecycle")

        restored = MemoryService()
        port = MemoryRuntimeStatePort(restored)
        port.install_prepared_state(port.prepare_restore_state(payload))

        turn = restored.l1_store.turns.get("turn-legacy")
        self.assertEqual(turn.state.value, "settled")
        self.assertNotIn("legacy transient reasoning", repr(turn.messages))
        results = [
            part
            for message in turn.messages
            for part in message.parts
            if isinstance(part, ToolResultIR)
        ]
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].retired)
        self.assertNotIn("legacy full file result", results[0].content)

    def test_execution_restore_and_reset_owns_in_memory_pager(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="pal_execution_snapshot_"))
        source = ExecutionRuntime(runtime_root=root)
        context = source.begin_tool_result_turn(
            turn_id="turn-1",
            scope_key="role-1",
            input_id="input-1",
        )
        source.logical_state.store_pager(
            PagerHandleManifest(
                result_ref="ref-1",
                execution_lifetime_id=context.execution_lifetime_id,
                tool_name="read_file",
                status="ok",
                ok=True,
                page_size=256,
                original_size=13,
                page_count=1,
                created_user_turn=1,
                expires_at_user_turn=6,
                output_json="{}",
                rendered="secret result",
            )
        )
        payload = dict(ExecutionRuntimeStatePort(source).snapshot_state())
        restored = ExecutionRuntime(runtime_root=root)
        port = ExecutionRuntimeStatePort(restored)
        port.install_prepared_state(port.prepare_restore_state(payload))
        self.assertEqual(
            restored.read_tool_result_page(
                result_ref="ref-1",
                execution_lifetime_id="role-1",
            ).content,
            "secret result",
        )
        port.reset_state("soft_reset")
        self.assertIsNone(
            restored.read_tool_result_page(
                result_ref="ref-1",
                execution_lifetime_id="role-1",
            )
        )
        source.shutdown()
        restored.shutdown()

    def test_execution_restore_rejects_cross_lifetime_pager_and_turn_context(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="pal_execution_snapshot_identity_"))
        source = ExecutionRuntime(runtime_root=root)
        context = source.begin_tool_result_turn(
            turn_id="turn-1",
            scope_key="role-1",
            input_id="input-1",
        )
        source.logical_state.store_pager(
            PagerHandleManifest(
                result_ref="ref-1",
                execution_lifetime_id=context.execution_lifetime_id,
                tool_name="read_file",
                status="ok",
                ok=True,
                page_size=256,
                original_size=6,
                page_count=1,
                created_user_turn=1,
                expires_at_user_turn=6,
                output_json="{}",
                rendered="secret",
            )
        )
        payload = dict(ExecutionRuntimeStatePort(source).snapshot_state())
        wrong_pager = copy.deepcopy(payload)
        wrong_pager["logical_execution"]["sessions"]["role-1"]["handles"][
            "ref-1"
        ]["execution_lifetime_id"] = "role-2"
        pager_runtime = ExecutionRuntime(runtime_root=root)
        with self.assertRaisesRegex(ValueError, "pager identity"):
            ExecutionRuntimeStatePort(pager_runtime).prepare_restore_state(wrong_pager)

        wrong_context = copy.deepcopy(payload)
        wrong_context["turn_contexts"]["turn-1"]["execution_lifetime_id"] = (
            "missing-role"
        )
        context_runtime = ExecutionRuntime(runtime_root=root)
        with self.assertRaisesRegex(ValueError, "owning lifetime"):
            ExecutionRuntimeStatePort(context_runtime).prepare_restore_state(wrong_context)
        pager_runtime.shutdown()
        context_runtime.shutdown()
        source.shutdown()


if __name__ == "__main__":
    unittest.main()
