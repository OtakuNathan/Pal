from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace

from pal.core import PalCore
from pal.execution import register_with_core as register_execution_with_core
from pal.execution.session_state import (
    FileDeliveryManifest,
    FileDeliverySpan,
    InMemoryLogicalExecutionState,
    PagerHandleManifest,
)
from pal.minion.runner import _MinionExecutionRuntimeAdapter
from pal.minion.scoped_execution import MinionScopedExecutionRuntime
from pal.llm.contracts import CanonicalToolCall


class LogicalExecutionStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = InMemoryLogicalExecutionState()
        self.context = self.backend.begin_input(
            logical_session_id="session-a",
            input_id="assignment-1",
        )

    def _store_file_result(self) -> PagerHandleManifest:
        rendered = "1: alpha\n2: beta\n3: gamma\n"
        delivery = FileDeliveryManifest(
            file_key="/workspace/input.txt",
            digest="digest-a",
            total_lines=3,
            spans=(
                FileDeliverySpan(
                    start_offset=0,
                    end_offset=len("1: alpha\n"),
                    start_line=1,
                    end_line=1,
                ),
                FileDeliverySpan(
                    start_offset=len("1: alpha\n"),
                    end_offset=len("1: alpha\n2: beta\n"),
                    start_line=2,
                    end_line=2,
                ),
                FileDeliverySpan(
                    start_offset=len("1: alpha\n2: beta\n"),
                    end_offset=len(rendered),
                    start_line=3,
                    end_line=3,
                ),
            ),
        )
        return self.backend.store_pager(
            PagerHandleManifest(
                result_ref="result-1",
                logical_session_id="session-a",
                tool_name="read_file",
                status="ok",
                ok=True,
                page_size=256,
                original_size=len(rendered),
                page_count=1,
                created_user_turn=1,
                expires_at_user_turn=6,
                output_json='{"content":"alpha\\nbeta\\ngamma\\n"}',
                rendered=rendered,
                delivery_manifest=delivery.to_dict(),
            )
        )

    def test_input_ids_advance_exactly_once_and_reads_do_not_extend_expiry(
        self,
    ) -> None:
        handle = self._store_file_result()
        replay = self.backend.begin_input(
            logical_session_id="session-a",
            input_id="assignment-1",
        )
        self.assertEqual(replay.current_user_turn, 1)

        for turn in range(2, 6):
            context = self.backend.begin_input(
                logical_session_id="session-a",
                input_id=f"assignment-{turn}",
            )
            self.assertEqual(context.current_user_turn, turn)
            page = self.backend.read_pager(
                logical_session_id="session-a",
                result_ref=handle.result_ref,
                page=1,
                page_size=None,
                anchor="head",
            )
            self.assertEqual(page.state, "ok")

        expired_context = self.backend.begin_input(
            logical_session_id="session-a",
            input_id="assignment-6",
        )
        self.assertEqual(expired_context.current_user_turn, 6)
        expired = self.backend.read_pager(
            logical_session_id="session-a",
            result_ref=handle.result_ref,
            page=1,
            page_size=None,
            anchor="head",
        )
        self.assertEqual(expired.state, "expired_handle")

    def test_handles_are_session_scoped_and_retire_with_session(self) -> None:
        self._store_file_result()
        self.backend.begin_input(
            logical_session_id="session-b",
            input_id="assignment-b",
        )
        missing = self.backend.read_pager(
            logical_session_id="session-b",
            result_ref="result-1",
            page=1,
            page_size=None,
            anchor="head",
        )
        self.assertEqual(missing.state, "unknown_handle")

        self.backend.retire_session("session-a")
        retired = self.backend.read_pager(
            logical_session_id="session-a",
            result_ref="result-1",
            page=1,
            page_size=None,
            anchor="head",
        )
        self.assertEqual(retired.state, "expired_handle")

    def test_only_delivered_ranges_authorize_and_new_epoch_starts_empty(
        self,
    ) -> None:
        self._store_file_result()
        page = self.backend.read_pager(
            logical_session_id="session-a",
            result_ref="result-1",
            page=1,
            page_size=256,
            anchor="head",
        )
        self.backend.reconcile_projection(
            logical_session_id="session-a",
            projection=("tool-message-a",),
            deliveries=(page.delivery_manifest,),
        )
        grant = self.backend.file_grant(
            logical_session_id="session-a",
            file_key="/workspace/input.txt",
            digest="digest-a",
        )
        self.assertIsNotNone(grant)
        self.assertTrue(grant.complete)

        rotated = self.backend.reconcile_projection(
            logical_session_id="session-a",
            projection=("replacement-message",),
            deliveries=(),
        )
        self.assertEqual(rotated.context_epoch, 2)
        self.assertIsNone(
            self.backend.file_grant(
                logical_session_id="session-a",
                file_key="/workspace/input.txt",
                digest="digest-a",
            )
        )

        revealed_again = self.backend.read_pager(
            logical_session_id="session-a",
            result_ref="result-1",
            page=1,
            page_size=256,
            anchor="head",
        )
        self.backend.reconcile_projection(
            logical_session_id="session-a",
            projection=("replacement-message", "pager-message"),
            deliveries=(revealed_again.delivery_manifest,),
        )
        self.assertTrue(
            self.backend.file_grant(
                logical_session_id="session-a",
                file_key="/workspace/input.txt",
                digest="digest-a",
            ).complete
        )

    def test_split_line_is_authorized_only_after_every_page_fragment_arrives(
        self,
    ) -> None:
        rendered = "x" * 400
        delivery = FileDeliveryManifest(
            file_key="/workspace/long.txt",
            digest="digest-long",
            total_lines=1,
            spans=(
                FileDeliverySpan(
                    start_offset=0,
                    end_offset=len(rendered),
                    start_line=1,
                    end_line=1,
                    visible_start_in_line=0,
                    visible_end_in_line=len(rendered),
                    line_length=len(rendered),
                ),
            ),
        )
        self.backend.store_pager(
            PagerHandleManifest(
                result_ref="result-long",
                logical_session_id="session-a",
                tool_name="read_file",
                status="ok",
                ok=True,
                page_size=256,
                original_size=len(rendered),
                page_count=2,
                created_user_turn=1,
                expires_at_user_turn=6,
                output_json='{"content":"long"}',
                rendered=rendered,
                delivery_manifest=delivery.to_dict(),
            )
        )
        first = self.backend.read_pager(
            logical_session_id="session-a",
            result_ref="result-long",
            page=1,
            page_size=None,
            anchor="head",
        )
        self.backend.reconcile_projection(
            logical_session_id="session-a",
            projection=("page-1",),
            deliveries=(first.delivery_manifest,),
        )
        partial = self.backend.file_grant(
            logical_session_id="session-a",
            file_key="/workspace/long.txt",
            digest="digest-long",
        )
        self.assertIsNotNone(partial)
        self.assertFalse(partial.complete)

        second = self.backend.read_pager(
            logical_session_id="session-a",
            result_ref="result-long",
            page=2,
            page_size=None,
            anchor="head",
        )
        self.backend.reconcile_projection(
            logical_session_id="session-a",
            projection=("page-1", "page-2"),
            deliveries=(second.delivery_manifest,),
        )
        complete = self.backend.file_grant(
            logical_session_id="session-a",
            file_key="/workspace/long.txt",
            digest="digest-long",
        )
        self.assertIsNotNone(complete)
        self.assertTrue(complete.complete)

    def test_empty_digest_can_detect_a_stale_prior_grant(self) -> None:
        self.backend.set_file_full(
            logical_session_id="session-a",
            file_key="/workspace/input.txt",
            digest="old-digest",
            total_lines=2,
        )
        self.assertIsNotNone(
            self.backend.file_grant(
                logical_session_id="session-a",
                file_key="/workspace/input.txt",
                digest="",
            )
        )
        self.assertIsNone(
            self.backend.file_grant(
                logical_session_id="session-a",
                file_key="/workspace/input.txt",
                digest="new-digest",
            )
        )

    def test_minion_execution_adapters_forward_context_reconciliation(
        self,
    ) -> None:
        base = PalCore().context.execution_runtime
        calls: list[dict[str, object]] = []
        base.reconcile_tool_context = lambda **kwargs: calls.append(kwargs) or "ok"
        scoped = MinionScopedExecutionRuntime(
            base_runtime=base,
            allowed_capabilities=[],
        )
        self.assertIs(
            scoped.base_runtime.runtime.tool_result_pager,
            base.tool_result_pager,
        )
        self.assertIs(
            scoped.base_runtime.runtime.logical_state,
            base.logical_state,
        )
        state = SimpleNamespace(execution_runtime=scoped)
        adapter = _MinionExecutionRuntimeAdapter(
            SimpleNamespace(),
            state,
            SimpleNamespace(),
        )

        result = adapter.reconcile_tool_context(
            turn_id="turn-1",
            original_messages=[],
            projected_messages=[],
            delivery_records={},
        )

        self.assertEqual(result, "ok")
        self.assertEqual(calls[0]["turn_id"], "turn-1")

    def test_file_mutation_is_authorized_only_after_result_delivery(
        self,
    ) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")
        runtime = core.context.execution_runtime
        runtime.begin_tool_result_turn(
            turn_id="turn-1",
            scope_key="logical-file-session",
            input_id="assignment-1",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "input.txt"
            path.write_text("alpha\nbeta\n", encoding="utf-8")
            read = runtime.execute_tool(
                CanonicalToolCall(
                    name="read_file",
                    args={"file_path": str(path)},
                    call_id="read-1",
                ),
                turn_id="turn-1",
            )
            before_delivery = runtime.execute_tool(
                CanonicalToolCall(
                    name="edit_file",
                    args={
                        "file_path": str(path),
                        "old_string": "alpha",
                        "new_string": "omega",
                    },
                    call_id="edit-before",
                ),
                turn_id="turn-1",
            )
            self.assertFalse(before_delivery.ok)

            tool_message = {
                "role": "tool",
                "tool_call_id": "read-1",
                "content": read.llm_text,
            }
            runtime.reconcile_tool_context(
                turn_id="turn-1",
                original_messages=[tool_message],
                projected_messages=[tool_message],
                delivery_records={"read-1": dict(read.context_delivery or {})},
            )
            after_delivery = runtime.execute_tool(
                CanonicalToolCall(
                    name="edit_file",
                    args={
                        "file_path": str(path),
                        "old_string": "alpha",
                        "new_string": "omega",
                    },
                    call_id="edit-after",
                ),
                turn_id="turn-1",
            )

            self.assertTrue(after_delivery.ok)
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "omega\nbeta\n",
            )

    def test_context_compaction_rebuilds_only_exact_retained_file_ranges(
        self,
    ) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")
        runtime = core.context.execution_runtime
        runtime.begin_tool_result_turn(
            turn_id="turn-compact",
            scope_key="compact-session",
            input_id="assignment-compact",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "compact.txt"
            path.write_text("same\nsame\nsame\n", encoding="utf-8")
            read = runtime.execute_tool(
                CanonicalToolCall(
                    name="read_file",
                    args={"file_path": str(path)},
                    call_id="read-compact",
                ),
                turn_id="turn-compact",
            )
            full_message = {
                "role": "tool",
                "tool_call_id": "read-compact",
                "content": read.llm_text,
            }
            runtime.reconcile_tool_context(
                turn_id="turn-compact",
                original_messages=[full_message],
                projected_messages=[
                    {
                        **full_message,
                        "_pal_visible_source_ranges": [
                            [0, len(read.llm_text)]
                        ],
                    }
                ],
                delivery_records={
                    "read-compact": dict(read.context_delivery or {})
                },
            )
            context = runtime.logical_context_for_turn("turn-compact")
            full = runtime.logical_state.file_grant(
                logical_session_id=context.logical_session_id,
                file_key=str(path.resolve()),
                digest=dict(read.context_delivery or {})["digest"],
            )
            self.assertTrue(full.complete)

            first_span = dict(read.context_delivery or {})["spans"][0]
            runtime.reconcile_tool_context(
                turn_id="turn-compact",
                original_messages=[full_message],
                projected_messages=[
                    {
                        **full_message,
                        "content": read.llm_text[
                            : int(first_span["end_offset"])
                        ],
                        "_pal_visible_source_ranges": [
                            [0, int(first_span["end_offset"])]
                        ],
                    }
                ],
                delivery_records={
                    "read-compact": dict(read.context_delivery or {})
                },
            )
            compacted = runtime.logical_state.file_grant(
                logical_session_id=context.logical_session_id,
                file_key=str(path.resolve()),
                digest=dict(read.context_delivery or {})["digest"],
            )
            self.assertIsNotNone(compacted)
            self.assertFalse(compacted.complete)
            self.assertEqual(compacted.covered_ranges, ((1, 1),))


if __name__ == "__main__":
    unittest.main()
