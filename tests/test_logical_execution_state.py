from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, ToolResultIR, new_tool_call

import asyncio
import unittest
import tempfile
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from pal.core import PalCore
from pal.core.turn_executor import TurnExecutor
from pal.execution import register_with_core as register_execution_with_core
from pal.execution.contracts import ToolCallBudget
from pal.execution.file_edit import FileEditTool
from pal.execution.file_state import SessionFileStateCache, read_utf8_text_exact
from pal.execution.session_state import (
    FileDeliveryManifest,
    FileDeliverySpan,
    InMemoryLogicalExecutionState,
    PagerHandleManifest,
    content_digest,
)
from pal.minion.scoped_execution import MinionScopedExecutionRuntime
from pal.llm.ir import LLMMessageIR, MessageRole
from pal.memory import MemoryService, register_with_core as register_memory_with_core
from pal.memory.contracts import L1MessageKind


class LogicalExecutionStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = InMemoryLogicalExecutionState()
        self.context = self.backend.begin_input(
            execution_lifetime_id="session-a",
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
                execution_lifetime_id="session-a",
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

    def test_turn_context_mapping_retires_with_the_n_plus_five_window(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        runtime = core.context.execution_runtime
        for index in range(1, 7):
            runtime.begin_tool_result_turn(
                turn_id=f"turn-{index}",
                scope_key="long-lived-channel",
                input_id=f"input-{index}",
                retention_user_turns=5,
            )
        self.assertNotIn("turn-1", runtime.tool_result_pager._turn_contexts)
        self.assertIn("turn-2", runtime.tool_result_pager._turn_contexts)

    def test_frozen_tool_result_preserves_nested_delivery_spans(self) -> None:
        manifest = FileDeliveryManifest(
            file_key="/workspace/input.txt",
            digest="digest-a",
            total_lines=1,
            spans=(
                FileDeliverySpan(
                    start_offset=0,
                    end_offset=8,
                    start_line=1,
                    end_line=1,
                    visible_start_in_line=0,
                    visible_end_in_line=8,
                    line_length=8,
                ),
            ),
        )
        result = ToolResultIR(
            call_id="read-frozen",
            name="read_file",
            content="1: alpha",
            context_delivery=manifest.to_dict(),
        )

        restored = FileDeliveryManifest.from_dict(result.context_delivery)

        self.assertIsNotNone(restored)
        self.assertEqual(len(restored.spans), 1)
        self.assertEqual(restored.spans[0].end_offset, 8)

    def test_input_ids_advance_exactly_once_and_reads_do_not_extend_expiry(
        self,
    ) -> None:
        handle = self._store_file_result()
        replay = self.backend.begin_input(
            execution_lifetime_id="session-a",
            input_id="assignment-1",
        )
        self.assertEqual(replay.current_user_turn, 1)

        for turn in range(2, 6):
            context = self.backend.begin_input(
                execution_lifetime_id="session-a",
                input_id=f"assignment-{turn}",
            )
            self.assertEqual(context.current_user_turn, turn)
            page = self.backend.read_pager(
                execution_lifetime_id="session-a",
                result_ref=handle.result_ref,
                page=1,
                page_size=None,
                anchor="head",
            )
            self.assertEqual(page.state, "ok")

        expired_context = self.backend.begin_input(
            execution_lifetime_id="session-a",
            input_id="assignment-6",
        )
        self.assertEqual(expired_context.current_user_turn, 6)
        expired = self.backend.read_pager(
            execution_lifetime_id="session-a",
            result_ref=handle.result_ref,
            page=1,
            page_size=None,
            anchor="head",
        )
        self.assertEqual(expired.state, "expired_handle")

    def test_handles_are_session_scoped_and_retire_with_session(self) -> None:
        self._store_file_result()
        self.backend.begin_input(
            execution_lifetime_id="session-b",
            input_id="assignment-b",
        )
        missing = self.backend.read_pager(
            execution_lifetime_id="session-b",
            result_ref="result-1",
            page=1,
            page_size=None,
            anchor="head",
        )
        self.assertEqual(missing.state, "unknown_handle")

        self.backend.retire_session("session-a")
        retired = self.backend.read_pager(
            execution_lifetime_id="session-a",
            result_ref="result-1",
            page=1,
            page_size=None,
            anchor="head",
        )
        self.assertEqual(retired.state, "unknown_handle")
        with self.assertRaisesRegex(RuntimeError, "retired"):
            self.backend.begin_input(
                execution_lifetime_id="session-a",
                input_id="assignment-after-retirement",
            )

    def test_paged_file_initial_result_authorizes_only_its_exact_first_page(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")
        runtime = core.context.execution_runtime
        turn_id = "turn-paged-file"
        runtime.begin_tool_result_turn(
            turn_id=turn_id,
            scope_key="paged-file-session",
            input_id="assignment-1",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "large.txt"
            path.write_text("alpha\n" + ("padding\n" * 2_000), encoding="utf-8")
            read = runtime.execute_tool(
                new_tool_call(
                    name="read_file",
                    args={"file_path": str(path), "limit": 5_000},
                    call_id="read-large",
                ),
                turn_id=turn_id,
                budget=ToolCallBudget(
                    max_output_chars=1_000,
                    preview_chars=500,
                ),
            )
            self.assertEqual(read.structured["kind"], "paged")
            runtime.commit_tool_delivery(
                turn_id=turn_id,
                context_delivery=dict(read.context_delivery or {}),
                result_id="read-large",
            )
            grant = runtime.logical_state.file_grant(
                execution_lifetime_id=runtime.logical_context_for_turn(
                    turn_id
                ).execution_lifetime_id,
                file_key=str(path.resolve()),
                digest=dict(read.context_delivery or {})["digest"],
            )
            self.assertIsNotNone(grant)
            self.assertFalse(grant.complete)
            self.assertTrue(grant.covered_ranges[0][0] <= 1)

            edit = runtime.execute_tool(
                new_tool_call(
                    name="edit_file",
                    args={
                        "file_path": str(path),
                        "old_string": "alpha",
                        "new_string": "omega",
                    },
                    call_id="edit-large",
                ),
                turn_id=turn_id,
            )

            self.assertTrue(edit.ok, edit.llm_text)
            self.assertTrue(path.read_text(encoding="utf-8").startswith("omega\n"))

    def test_only_delivered_ranges_authorize_and_new_epoch_starts_empty(
        self,
    ) -> None:
        self._store_file_result()
        page = self.backend.read_pager(
            execution_lifetime_id="session-a",
            result_ref="result-1",
            page=1,
            page_size=256,
            anchor="head",
        )
        self.backend.reconcile_projection(
            execution_lifetime_id="session-a",
            projection=("tool-message-a",),
            deliveries=(page.delivery_manifest,),
        )
        grant = self.backend.file_grant(
            execution_lifetime_id="session-a",
            file_key="/workspace/input.txt",
            digest="digest-a",
        )
        self.assertIsNotNone(grant)
        self.assertTrue(grant.complete)

        rotated = self.backend.reconcile_projection(
            execution_lifetime_id="session-a",
            projection=("replacement-message",),
            deliveries=(),
        )
        self.assertEqual(rotated.context_epoch, 3)
        self.assertIsNone(
            self.backend.file_grant(
                execution_lifetime_id="session-a",
                file_key="/workspace/input.txt",
                digest="digest-a",
            )
        )

        revealed_again = self.backend.read_pager(
            execution_lifetime_id="session-a",
            result_ref="result-1",
            page=1,
            page_size=256,
            anchor="head",
        )
        self.backend.reconcile_projection(
            execution_lifetime_id="session-a",
            projection=("replacement-message", "pager-message"),
            deliveries=(revealed_again.delivery_manifest,),
        )
        self.assertTrue(
            self.backend.file_grant(
                execution_lifetime_id="session-a",
                file_key="/workspace/input.txt",
                digest="digest-a",
            ).complete
        )

    def test_result_retirement_drops_inherited_ranges_with_their_owner(self) -> None:
        read = FileDeliveryManifest(
            file_key="/workspace/owned.txt",
            digest="digest-before",
            total_lines=3,
            spans=(
                FileDeliverySpan(0, 10, 1, 3, 0, 10, 10),
            ),
        ).to_dict()
        read["result_id"] = "read-owner"
        self.backend.record_delivery(
            execution_lifetime_id="session-a",
            delivery=read,
        )
        edit = FileDeliveryManifest(
            file_key="/workspace/owned.txt",
            digest="digest-after",
            total_lines=3,
            spans=(
                FileDeliverySpan(0, 10, 2, 2, 0, 10, 10),
            ),
            operation="edit",
            before_digest="digest-before",
            inherited_ranges=((1, 1), (3, 3)),
            parent_result_ids=("read-owner",),
        ).to_dict()
        edit["result_id"] = "edit-owner"
        self.backend.record_delivery(
            execution_lifetime_id="session-a",
            delivery=edit,
        )

        complete = self.backend.file_grant(
            execution_lifetime_id="session-a",
            file_key="/workspace/owned.txt",
            digest="digest-after",
        )
        self.assertIsNotNone(complete)
        self.assertTrue(complete.complete)

        self.backend.retire_results(
            execution_lifetime_id="session-a",
            result_ids=("read-owner",),
        )
        post_image_only = self.backend.file_grant(
            execution_lifetime_id="session-a",
            file_key="/workspace/owned.txt",
            digest="digest-after",
        )
        self.assertIsNotNone(post_image_only)
        self.assertEqual(post_image_only.covered_ranges, ((2, 2),))

        self.backend.retire_results(
            execution_lifetime_id="session-a",
            result_ids=("edit-owner",),
        )
        self.assertIsNone(
            self.backend.file_grant(
                execution_lifetime_id="session-a",
                file_key="/workspace/owned.txt",
                digest="digest-after",
            )
        )

    def test_session_edit_reads_once_for_authority_and_once_for_locked_cas(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "read-count.txt"
            content = "alpha\nbeta\n"
            path.write_text(content, encoding="utf-8")
            delivery = FileDeliveryManifest(
                file_key=str(path.resolve()),
                digest=content_digest(content),
                total_lines=2,
                spans=(FileDeliverySpan(0, 10, 1, 2, 0, 10, 10),),
            ).to_dict()
            delivery["result_id"] = "read-count-owner"
            self.backend.record_delivery(
                execution_lifetime_id="session-a",
                delivery=delivery,
            )
            cache = SessionFileStateCache(
                backend=self.backend,
                context=self.context,
            )

            with patch(
                "pal.execution.file_state.read_utf8_text_exact",
                wraps=read_utf8_text_exact,
            ) as exact_read:
                result = FileEditTool(cache=cache).invoke(
                    {
                        "file_path": str(path),
                        "old_string": "alpha",
                        "new_string": "omega",
                    }
                )

            self.assertEqual(result.status, "ok")
            self.assertEqual(exact_read.call_count, 2)

    def test_observed_new_digest_retires_old_session_file_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "changed.txt"
            old_content = "old\n"
            path.write_text(old_content, encoding="utf-8")
            delivery = FileDeliveryManifest(
                file_key=str(path.resolve()),
                digest=content_digest(old_content),
                total_lines=1,
                spans=(FileDeliverySpan(0, 5, 1, 1, 0, 5, 5),),
            ).to_dict()
            delivery["result_id"] = "old-owner"
            self.backend.record_delivery(
                execution_lifetime_id="session-a",
                delivery=delivery,
            )
            cache = SessionFileStateCache(
                backend=self.backend,
                context=self.context,
            )
            new_content = "new\n"
            path.write_text(new_content, encoding="utf-8")

            retired = cache.retire_if_observed_digest_changed(
                path,
                observed_digest=content_digest(new_content),
            )

            self.assertTrue(retired)
            self.assertIsNone(
                self.backend.file_grant(
                    execution_lifetime_id="session-a",
                    file_key=str(path.resolve()),
                    digest=content_digest(old_content),
                )
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
                execution_lifetime_id="session-a",
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
            execution_lifetime_id="session-a",
            result_ref="result-long",
            page=1,
            page_size=None,
            anchor="head",
        )
        self.backend.reconcile_projection(
            execution_lifetime_id="session-a",
            projection=("page-1",),
            deliveries=(first.delivery_manifest,),
        )
        partial = self.backend.file_grant(
            execution_lifetime_id="session-a",
            file_key="/workspace/long.txt",
            digest="digest-long",
        )
        self.assertIsNotNone(partial)
        self.assertFalse(partial.complete)

        second = self.backend.read_pager(
            execution_lifetime_id="session-a",
            result_ref="result-long",
            page=2,
            page_size=None,
            anchor="head",
        )
        self.backend.reconcile_projection(
            execution_lifetime_id="session-a",
            projection=("page-1", "page-2"),
            deliveries=(first.delivery_manifest, second.delivery_manifest),
        )
        complete = self.backend.file_grant(
            execution_lifetime_id="session-a",
            file_key="/workspace/long.txt",
            digest="digest-long",
        )
        self.assertIsNotNone(complete)
        self.assertTrue(complete.complete)

    def test_empty_digest_can_detect_a_stale_prior_snapshot(self) -> None:
        self.backend.set_file_snapshot(
            execution_lifetime_id="session-a",
            file_key="/workspace/input.txt",
            digest="old-digest",
            total_lines=2,
            complete=True,
        )
        self.assertIsNotNone(
            self.backend.file_snapshot(
                execution_lifetime_id="session-a",
                file_key="/workspace/input.txt",
                digest="",
            )
        )
        self.assertIsNone(
            self.backend.file_snapshot(
                execution_lifetime_id="session-a",
                file_key="/workspace/input.txt",
                digest="new-digest",
            )
        )

    def test_projection_revocation_preserves_read_before_mutate_snapshot(
        self,
    ) -> None:
        delivery = FileDeliveryManifest(
            file_key="/workspace/input.txt",
            digest="digest-a",
            total_lines=1,
            spans=(
                FileDeliverySpan(
                    start_offset=0,
                    end_offset=5,
                    start_line=1,
                    end_line=1,
                    visible_end_in_line=5,
                    line_length=5,
                ),
            ),
        )
        self.backend.reconcile_projection(
            execution_lifetime_id="session-a",
            projection=("visible",),
            deliveries=(delivery.to_dict(),),
        )
        self.backend.reconcile_projection(
            execution_lifetime_id="session-a",
            projection=(),
            deliveries=(),
        )

        self.assertIsNone(
            self.backend.file_grant(
                execution_lifetime_id="session-a",
                file_key="/workspace/input.txt",
                digest="digest-a",
            )
        )
        snapshot = self.backend.file_snapshot(
            execution_lifetime_id="session-a",
            file_key="/workspace/input.txt",
            digest="digest-a",
        )
        self.assertIsNotNone(snapshot)
        self.assertTrue(snapshot.complete)

    def test_historical_delivery_does_not_replace_mutation_snapshot(self) -> None:
        old_delivery = FileDeliveryManifest(
            file_key="/workspace/input.txt",
            digest="digest-old",
            total_lines=1,
            spans=(
                FileDeliverySpan(
                    start_offset=0,
                    end_offset=5,
                    start_line=1,
                    end_line=1,
                    visible_end_in_line=5,
                    line_length=5,
                ),
            ),
        )
        self.backend.reconcile_projection(
            execution_lifetime_id="session-a",
            projection=("old-read",),
            deliveries=(old_delivery.to_dict(),),
        )
        self.backend.set_file_snapshot(
            execution_lifetime_id="session-a",
            file_key="/workspace/input.txt",
            digest="digest-after-own-edit",
            total_lines=1,
            complete=True,
            source="mutation",
        )

        self.backend.reconcile_projection(
            execution_lifetime_id="session-a",
            projection=("old-read", "edit-result"),
            deliveries=(old_delivery.to_dict(),),
        )

        snapshot = self.backend.file_snapshot(
            execution_lifetime_id="session-a",
            file_key="/workspace/input.txt",
            digest="digest-after-own-edit",
        )
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.source, "mutation")

    def test_file_snapshot_expires_after_five_new_semantic_inputs(self) -> None:
        self.backend.set_file_snapshot(
            execution_lifetime_id="session-a",
            file_key="/workspace/input.txt",
            digest="digest-a",
            total_lines=1,
            complete=True,
        )
        for index in range(2, 6):
            self.backend.begin_input(
                execution_lifetime_id="session-a",
                input_id=f"assignment-{index}",
            )
            self.assertIsNotNone(
                self.backend.file_snapshot(
                    execution_lifetime_id="session-a",
                    file_key="/workspace/input.txt",
                    digest="digest-a",
                )
            )
        self.backend.begin_input(
            execution_lifetime_id="session-a",
            input_id="assignment-6",
        )
        self.assertIsNone(
            self.backend.file_snapshot(
                execution_lifetime_id="session-a",
                file_key="/workspace/input.txt",
                digest="digest-a",
            )
        )

    def test_minion_scoped_execution_forwards_context_reconciliation(
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
        result = scoped.reconcile_tool_context(
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
                new_tool_call(
                    name="read_file",
                    args={"file_path": str(path)},
                    call_id="read-1",
                ),
                turn_id="turn-1",
            )
            before_delivery = runtime.execute_tool(
                new_tool_call(
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
                new_tool_call(
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

    def test_partial_l1_delivery_authorizes_only_the_delivered_edit_range(
        self,
    ) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")
        runtime = core.context.execution_runtime
        runtime.begin_tool_result_turn(
            turn_id="turn-partial-edit",
            scope_key="logical-partial-file-session",
            input_id="assignment-1",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "input.txt"
            path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
            read = runtime.execute_tool(
                new_tool_call(
                    name="read_file",
                    args={"file_path": str(path), "offset": 2, "limit": 1},
                    call_id="read-partial",
                ),
                turn_id="turn-partial-edit",
            )
            tool_message = {
                "role": "tool",
                "tool_call_id": "read-partial",
                "content": read.llm_text,
            }
            runtime.reconcile_tool_context(
                turn_id="turn-partial-edit",
                original_messages=[tool_message],
                projected_messages=[tool_message],
                delivery_records={
                    "read-partial": dict(read.context_delivery or {})
                },
            )

            unseen = runtime.execute_tool(
                new_tool_call(
                    name="edit_file",
                    args={
                        "file_path": str(path),
                        "old_string": "alpha",
                        "new_string": "ALPHA",
                    },
                    call_id="edit-unseen",
                ),
                turn_id="turn-partial-edit",
            )
            visible = runtime.execute_tool(
                new_tool_call(
                    name="edit_file",
                    args={
                        "file_path": str(path),
                        "old_string": "beta",
                        "new_string": "BETA",
                    },
                    call_id="edit-visible",
                ),
                turn_id="turn-partial-edit",
            )

            self.assertFalse(unseen.ok)
            self.assertEqual(unseen.structured["error_code"], "PARTIAL_READ")
            self.assertTrue(visible.ok, visible.llm_text)
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "alpha\nBETA\ngamma\n",
            )

    def test_turn_executor_commits_delivery_after_l1_tool_result_append(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")
        memory = MemoryService()
        register_memory_with_core(core.context, memory)
        runtime = core.context.execution_runtime
        turn_id = "turn-l1-delivery"
        runtime.begin_tool_result_turn(
            turn_id=turn_id,
            scope_key="logical-file-session",
            input_id="assignment-1",
        )
        memory.begin_l1_turn(turn_id, user_text="read then edit")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "input.txt"
            path.write_text("alpha\nbeta\n", encoding="utf-8")
            read_call = new_tool_call(
                name="read_file",
                args={"file_path": str(path)},
                call_id="read-l1",
            )
            memory.upsert_l1_assistant(
                turn_id,
                LLMMessageIR(
                    role=MessageRole.ASSISTANT,
                    parts=(read_call,),
                    semantic_kind=L1MessageKind.ASSISTANT_TOOL_CALL,
                ),
            )
            read = runtime.execute_tool(read_call, turn_id=turn_id)
            self.assertIsNotNone(read.context_delivery)

            executor = TurnExecutor(
                core.context,
                SimpleNamespace(),
                SimpleNamespace(),
                call_port_async=lambda *args, **kwargs: None,
                build_canonical_prompt=lambda *args, **kwargs: None,
                debug_log_prompt=lambda *args, **kwargs: None,
                debug_log_outcome=lambda *args, **kwargs: None,
                debug_log_reply=lambda *args, **kwargs: None,
                build_llm_tool_contracts=lambda: [],
                handle_failure_async=lambda *args, **kwargs: None,
                render_failure_feedback_text=lambda value: str(value),
                should_enter_failure_flow_for_tool_result=lambda value: False,
            )
            asyncio.run(
                executor._append_l1_tool_result_async(
                    SimpleNamespace(turn_id=turn_id),
                    read_call,
                    read,
                )
            )

            state_result = runtime.invoke_indirect_tool(
                new_tool_call(
                    name="file_state",
                    args={"file_path": str(path)},
                    call_id="state-after-l1",
                ),
                turn_id=turn_id,
            )
            self.assertTrue(state_result.output["cached"])
            self.assertTrue(state_result.output["valid"])
            self.assertTrue(state_result.output["full_view"])

            repeated_read = runtime.execute_tool(
                new_tool_call(
                    name="read_file",
                    args={"file_path": str(path)},
                    call_id="read-after-l1",
                ),
                turn_id=turn_id,
            )
            self.assertTrue(repeated_read.structured["unchanged"])

            edit = runtime.execute_tool(
                new_tool_call(
                    name="edit_file",
                    args={
                        "file_path": str(path),
                        "old_string": "alpha",
                        "new_string": "omega",
                    },
                    call_id="edit-after-l1",
                ),
                turn_id=turn_id,
            )
            self.assertTrue(edit.ok, edit.llm_text)
            self.assertEqual(path.read_text(encoding="utf-8"), "omega\nbeta\n")

    def test_failed_cross_module_delivery_rolls_back_l1_and_retires_pager(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")
        memory = MemoryService()
        register_memory_with_core(core.context, memory)
        runtime = core.context.execution_runtime
        turn_id = "turn-atomic-delivery"
        runtime.begin_tool_result_turn(
            turn_id=turn_id,
            scope_key="atomic-delivery-session",
            input_id="assignment-1",
        )
        memory.begin_l1_turn(turn_id, user_text="read atomically")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "input.txt"
            path.write_text("alpha\nbeta\n", encoding="utf-8")
            read_call = new_tool_call(
                name="read_file",
                args={"file_path": str(path)},
                call_id="read-atomic",
            )
            memory.upsert_l1_assistant(
                turn_id,
                LLMMessageIR(
                    role=MessageRole.ASSISTANT,
                    parts=(read_call,),
                    semantic_kind=L1MessageKind.ASSISTANT_TOOL_CALL,
                ),
            )
            read = runtime.execute_tool(
                read_call,
                turn_id=turn_id,
                budget=ToolCallBudget(max_output_chars=100_000),
            )
            self.assertTrue(read.replay_result_ref)
            executor = TurnExecutor(
                core.context,
                SimpleNamespace(),
                SimpleNamespace(),
                call_port_async=lambda *args, **kwargs: None,
                build_canonical_prompt=lambda *args, **kwargs: None,
                debug_log_prompt=lambda *args, **kwargs: None,
                debug_log_outcome=lambda *args, **kwargs: None,
                debug_log_reply=lambda *args, **kwargs: None,
                build_llm_tool_contracts=lambda: [],
                handle_failure_async=lambda *args, **kwargs: None,
                render_failure_feedback_text=lambda value: str(value),
                should_enter_failure_flow_for_tool_result=lambda value: False,
            )
            original_commit = runtime.commit_tool_delivery

            def fail_commit(**_kwargs):
                raise RuntimeError("simulated execution commit failure")

            runtime.commit_tool_delivery = fail_commit
            try:
                with self.assertRaisesRegex(RuntimeError, "simulated"):
                    asyncio.run(
                        executor._append_l1_tool_result_async(
                            SimpleNamespace(turn_id=turn_id),
                            read_call,
                            read,
                        )
                    )
            finally:
                runtime.commit_tool_delivery = original_commit

            active = memory.active_l1_turn(turn_id)
            self.assertEqual(active.pending_call_ids, {"read-atomic"})
            page = runtime.read_tool_result_page(
                result_ref=read.replay_result_ref,
                turn_id=turn_id,
            )
            self.assertEqual(page.state, "expired_handle")
            state_result = runtime.invoke_indirect_tool(
                new_tool_call(
                    name="file_state",
                    args={"file_path": str(path)},
                    call_id="state-after-failed-delivery",
                ),
                turn_id=turn_id,
            )
            self.assertFalse(state_result.output["cached"])

    def test_historical_read_delivery_cannot_roll_back_self_mutation_snapshot(
        self,
    ) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")
        runtime = core.context.execution_runtime
        runtime.begin_tool_result_turn(
            turn_id="turn-self-mutation",
            scope_key="logical-file-session",
            input_id="assignment-1",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "input.txt"
            path.write_text("alpha\nbeta\n", encoding="utf-8")
            read = runtime.execute_tool(
                new_tool_call(
                    name="read_file",
                    args={"file_path": str(path)},
                    call_id="read-1",
                ),
                turn_id="turn-self-mutation",
            )
            tool_message = {
                "role": "tool",
                "tool_call_id": "read-1",
                "content": read.llm_text,
            }
            delivery = {"read-1": dict(read.context_delivery or {})}
            runtime.reconcile_tool_context(
                turn_id="turn-self-mutation",
                original_messages=[tool_message],
                projected_messages=[tool_message],
                delivery_records=delivery,
            )
            first = runtime.execute_tool(
                new_tool_call(
                    name="edit_file",
                    args={
                        "file_path": str(path),
                        "old_string": "alpha",
                        "new_string": "omega",
                    },
                    call_id="edit-1",
                ),
                turn_id="turn-self-mutation",
            )
            self.assertTrue(first.ok)

            # Mutation authority advances only when the edit result itself is
            # retained in L1. The historical read remains as the dependency
            # that owns the unchanged post-image range.
            edit_message = {
                "role": "tool",
                "tool_call_id": "edit-1",
                "content": first.llm_text,
            }
            runtime.reconcile_tool_context(
                turn_id="turn-self-mutation",
                original_messages=[tool_message, edit_message],
                projected_messages=[tool_message, edit_message],
                delivery_records={
                    **delivery,
                    "edit-1": dict(first.context_delivery or {}),
                },
            )
            second = runtime.execute_tool(
                new_tool_call(
                    name="edit_file",
                    args={
                        "file_path": str(path),
                        "old_string": "beta",
                        "new_string": "gamma",
                    },
                    call_id="edit-2",
                ),
                turn_id="turn-self-mutation",
            )

            self.assertTrue(second.ok, second.llm_text)
            self.assertEqual(path.read_text(encoding="utf-8"), "omega\ngamma\n")

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
                new_tool_call(
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
                execution_lifetime_id=context.execution_lifetime_id,
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
                execution_lifetime_id=context.execution_lifetime_id,
                file_key=str(path.resolve()),
                digest=dict(read.context_delivery or {})["digest"],
            )
            self.assertIsNotNone(compacted)
            self.assertFalse(compacted.complete)
            self.assertEqual(compacted.covered_ranges, ((1, 1),))

    def test_compaction_projection_clears_file_visibility_without_expiring_pager(
        self,
    ) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")
        runtime = core.context.execution_runtime
        context = runtime.begin_tool_result_turn(
            turn_id="turn-pager-compact",
            scope_key="pager-compact-session",
            input_id="assignment-pager-compact",
            retention_user_turns=5,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "pager-compact.txt"
            path.write_text("alpha\nbeta\n", encoding="utf-8")
            read = runtime.execute_tool(
                new_tool_call(
                    name="read_file",
                    args={"file_path": str(path)},
                    call_id="read-pager-compact",
                ),
                turn_id="turn-pager-compact",
            )
            tool_message = {
                "role": "tool",
                "tool_call_id": "read-pager-compact",
                "content": read.llm_text,
            }
            runtime.reconcile_tool_context(
                turn_id="turn-pager-compact",
                original_messages=[tool_message],
                projected_messages=[tool_message],
                delivery_records={
                    "read-pager-compact": dict(
                        read.context_delivery or {}
                    )
                },
            )
            manifest = runtime.tool_result_pager.store(
                runtime_root=None,
                turn_id="turn-pager-compact",
                result_ref="pager-before-compact",
                tool_name="read_file",
                status="ok",
                ok=True,
                rendered=read.llm_text,
                page_size=256,
                context_delivery=dict(read.context_delivery or {}),
            )

            runtime.reconcile_tool_context(
                turn_id="turn-pager-compact",
                original_messages=[tool_message],
                projected_messages=[],
                delivery_records={
                    "read-pager-compact": dict(
                        read.context_delivery or {}
                    )
                },
            )

            page = runtime.read_tool_result_page(
                result_ref=manifest.result_ref,
                turn_id="turn-pager-compact",
            )
            self.assertIsNotNone(page)
            self.assertEqual(page.state, "ok")
            self.assertEqual(page.current_user_turn, context.current_user_turn)
            self.assertEqual(
                page.expires_at_user_turn,
                context.current_user_turn + 5,
            )
            reread = runtime.execute_tool(
                new_tool_call(
                    name="read_file",
                    args={"file_path": str(path)},
                    call_id="read-after-compact",
                ),
                turn_id="turn-pager-compact",
            )
            self.assertTrue(reread.ok)
            self.assertFalse(
                bool((reread.structured or {}).get("unchanged"))
            )
            self.assertIn("alpha", reread.llm_text)


if __name__ == "__main__":
    unittest.main()
