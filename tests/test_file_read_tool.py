"""Tests for FileReadTool: basic read, offset/limit, caching, and error handling."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pal.execution.file_capabilities import FileCapabilityMixin
from pal.execution.file_edit import FileEditTool
from pal.execution.file_read import (
    ERR_FILE_NOT_FOUND,
    ERR_INVALID_ARGUMENT,
    ERR_UNSUPPORTED_TEXT_ENCODING,
    FILE_UNCHANGED_STUB,
    FileReadTool,
    FileVisibilityCache,
)
from pal.execution.file_state import FileStateCache
from pal.execution.generated_tool_models import ExecutionFileCapabilitiesFileCapabilityMixinReadInput
from pal.execution.session_state import InMemoryLogicalExecutionState
from pal.execution.tool_facade import ToolRejectedError
from pal.shared import IntrospectionCall, RuntimeStatus


class _TempFileMixin:
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self.cache = FileStateCache()
        self.tool = FileReadTool(cache=self.cache)

    def _write_tmp(self, name: str, content: str) -> Path:
        path = Path(self._tmpdir) / name
        path.write_text(content, encoding="utf-8")
        return path

    def tearDown(self) -> None:
        shutil.rmtree(self._tmpdir, ignore_errors=True)


class BasicReadTests(_TempFileMixin, unittest.TestCase):
    def test_read_full_file(self) -> None:
        path = self._write_tmp("hello.txt", "line one\nline two\nline three\n")
        result = self.tool.invoke({"file_path": str(path)})
        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertIn("line one", result.text)
        self.assertIn("line two", result.text)
        self.assertEqual(result.structured["total_lines"], 3)
        self.assertEqual(result.structured["start_line"], 1)
        self.assertEqual(result.structured["end_line"], 3)
        self.assertFalse(result.structured["truncated"])

    def test_read_populates_cache(self) -> None:
        path = self._write_tmp("cached.txt", "cached content\n")
        self.tool.invoke({"file_path": str(path)})
        cached = self.cache.get_valid(str(path))
        self.assertEqual(cached, "cached content\n")
        self.assertEqual(self.cache.get_valid_full(str(path)), "cached content\n")

    def test_path_alias_reads_file(self) -> None:
        path = self._write_tmp("alias.txt", "alias content\n")
        result = self.tool.invoke({"path": str(path)})

        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertIn("alias content", result.text)
        self.assertEqual(self.cache.get_valid(str(path)), "alias content\n")

    def test_line_numbers_in_output(self) -> None:
        path = self._write_tmp("lines.txt", "aaa\nbbb\nccc\n")
        result = self.tool.invoke({"file_path": str(path)})
        self.assertIn("     1\taaa", result.text)
        self.assertIn("     3\tccc", result.text)

    def test_empty_file_returns_nonempty_full_view_marker(self) -> None:
        path = self._write_tmp("empty.txt", "")

        result = self.tool.invoke({"file_path": str(path)})

        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertEqual(result.text, "(empty file)")
        self.assertTrue(result.structured["full_view"])
        self.assertEqual(self.cache.get_valid_full(path), "")

    def test_repeated_unchanged_range_returns_compact_stub(self) -> None:
        path = self._write_tmp("repeat.txt", "one\ntwo\n")
        first = self.tool.invoke({"file_path": str(path)})
        second = self.tool.invoke({"file_path": str(path)})

        self.assertEqual(first.status, RuntimeStatus.OK)
        self.assertEqual(second.text, FILE_UNCHANGED_STUB)
        self.assertTrue(second.structured["unchanged"])

    def test_repeated_partial_range_returns_compact_stub(self) -> None:
        path = self._write_tmp("repeat-partial.txt", "one\ntwo\nthree\nfour\n")

        first = self.tool.invoke({"file_path": str(path), "offset": 2, "limit": 2})
        second = self.tool.invoke({"file_path": str(path), "offset": 2, "limit": 2})

        self.assertIn("two", first.text)
        self.assertEqual(second.text, FILE_UNCHANGED_STUB)
        self.assertTrue(second.structured["unchanged"])
        self.assertFalse(second.structured["full_view"])

    def test_previously_covered_subrange_returns_compact_stub(self) -> None:
        path = self._write_tmp(
            "covered.txt",
            "\n".join(f"line {number}" for number in range(1, 9)),
        )

        self.tool.invoke({"file_path": str(path), "offset": 2, "limit": 4})
        covered = self.tool.invoke({"file_path": str(path), "offset": 3, "limit": 2})
        uncovered = self.tool.invoke({"file_path": str(path), "offset": 5, "limit": 2})
        now_covered = self.tool.invoke({"file_path": str(path), "offset": 2, "limit": 5})

        self.assertEqual(covered.text, FILE_UNCHANGED_STUB)
        self.assertFalse(uncovered.structured["unchanged"])
        self.assertIn("line 6", uncovered.text)
        self.assertEqual(now_covered.text, FILE_UNCHANGED_STUB)

    def test_full_read_does_not_report_partial_reread_as_full_view(self) -> None:
        path = self._write_tmp("full-then-partial.txt", "one\ntwo\nthree\n")

        self.tool.invoke({"file_path": str(path)})
        repeated = self.tool.invoke({"file_path": str(path), "offset": 2, "limit": 1})

        self.assertEqual(repeated.text, FILE_UNCHANGED_STUB)
        self.assertFalse(repeated.structured["full_view"])

    def test_changed_content_is_returned_even_when_mtime_is_restored(self) -> None:
        path = self._write_tmp("same-mtime.txt", "one\ntwo\n")
        original_stat = path.stat()
        self.tool.invoke({"file_path": str(path)})

        path.write_text("ONE\nTWO\n", encoding="utf-8")
        os.utime(
            path,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        changed = self.tool.invoke({"file_path": str(path)})

        self.assertFalse(changed.structured["unchanged"])
        self.assertIn("ONE", changed.text)

    def test_visibility_is_isolated_between_llm_scopes(self) -> None:
        path = self._write_tmp("scoped.txt", "scope content\n")
        visibility = FileVisibilityCache()
        first_scope = FileReadTool(
            cache=self.cache,
            visibility_cache=visibility,
            visibility_scope="role-a",
        )
        second_scope = FileReadTool(
            cache=self.cache,
            visibility_cache=visibility,
            visibility_scope="role-b",
        )

        first_scope.invoke({"file_path": str(path)})
        other_role_first_read = second_scope.invoke({"file_path": str(path)})
        first_role_reread = first_scope.invoke({"file_path": str(path)})

        self.assertFalse(other_role_first_read.structured["unchanged"])
        self.assertIn("scope content", other_role_first_read.text)
        self.assertEqual(first_role_reread.text, FILE_UNCHANGED_STUB)


class OffsetLimitTests(_TempFileMixin, unittest.TestCase):
    def test_offset_skips_lines(self) -> None:
        path = self._write_tmp("multi.txt", "a\nb\nc\nd\ne\n")
        result = self.tool.invoke({"file_path": str(path), "offset": 3})
        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertIn("c", result.text)
        self.assertNotIn("a", result.text.split("\n")[0])
        self.assertEqual(result.structured["start_line"], 3)

    def test_limit_caps_output(self) -> None:
        path = self._write_tmp("long.txt", "\n".join(f"line {i}" for i in range(100)))
        result = self.tool.invoke({"file_path": str(path), "limit": 5})
        self.assertTrue(result.structured["truncated"])
        self.assertEqual(result.structured["end_line"], 5)
        self.assertEqual(result.structured["total_lines"], 100)

    def test_offset_and_limit_combined(self) -> None:
        path = self._write_tmp("combo.txt", "\n".join(f"row {i}" for i in range(20)))
        result = self.tool.invoke({"file_path": str(path), "offset": 10, "limit": 3})
        self.assertEqual(result.structured["start_line"], 10)
        self.assertEqual(result.structured["end_line"], 12)
        self.assertTrue(result.structured["truncated"])

    def test_offset_beyond_file(self) -> None:
        path = self._write_tmp("short.txt", "only one line\n")
        result = self.tool.invoke({"file_path": str(path), "offset": 999})
        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertEqual(result.structured["end_line"], 1)
        self.assertEqual(result.structured["total_lines"], 1)


class CacheIntegrationTests(_TempFileMixin, unittest.TestCase):
    def test_read_then_edit_success(self) -> None:
        """file_read populates cache -> file_edit works without manual mark_read."""
        path = self._write_tmp("flow.txt", "hello world\n")
        read_result = self.tool.invoke({"file_path": str(path)})
        self.assertEqual(read_result.status, RuntimeStatus.OK)

        edit_tool = FileEditTool(cache=self.cache)
        edit_result = edit_tool.invoke({
            "file_path": str(path),
            "old_string": "hello",
            "new_string": "goodbye",
        })
        self.assertEqual(edit_result.status, RuntimeStatus.OK)
        self.assertIn("patch", edit_result.structured)
        self.assertEqual(path.read_text(), "goodbye world\n")

    def test_tilde_read_authorizes_edit_without_absolute_reread(self) -> None:
        path = self._write_tmp("tilde_flow.txt", "hello world\n")

        with mock.patch.dict("os.environ", {"HOME": self._tmpdir}):
            read_result = self.tool.invoke({"file_path": "~/tilde_flow.txt"})
            edit_result = FileEditTool(cache=self.cache).invoke(
                {
                    "file_path": "~/tilde_flow.txt",
                    "old_string": "hello",
                    "new_string": "goodbye",
                }
            )

        self.assertEqual(read_result.status, RuntimeStatus.OK)
        self.assertEqual(edit_result.status, RuntimeStatus.OK)
        self.assertEqual(edit_result.structured["file_path"], str(path.resolve()))
        self.assertEqual(path.read_text(encoding="utf-8"), "goodbye world\n")

    def test_tilde_read_and_absolute_edit_share_snapshot(self) -> None:
        path = self._write_tmp("mixed_path_flow.txt", "alpha beta\n")

        with mock.patch.dict("os.environ", {"HOME": self._tmpdir}):
            self.tool.invoke({"file_path": "~/mixed_path_flow.txt"})
            edit_result = FileEditTool(cache=self.cache).invoke(
                {
                    "file_path": str(path),
                    "old_string": "alpha",
                    "new_string": "gamma",
                }
            )

        self.assertEqual(edit_result.status, RuntimeStatus.OK)
        self.assertEqual(path.read_text(encoding="utf-8"), "gamma beta\n")

    def test_partial_read_caches_bytes_but_does_not_grant_mutation(self) -> None:
        content = "\n".join(f"line {i}" for i in range(50))
        path = self._write_tmp("full_cache.txt", content)
        self.tool.invoke({"file_path": str(path), "offset": 1, "limit": 5})

        cached = self.cache.get_valid(str(path))
        self.assertEqual(cached, content)
        self.assertIsNone(self.cache.get_valid_full(str(path)))

        edit_result = FileEditTool(cache=self.cache).invoke(
            {
                "file_path": str(path),
                "old_string": "line 1",
                "new_string": "changed",
            }
        )
        self.assertEqual(edit_result.structured["error_code"], "PARTIAL_READ")


class ErrorHandlingTests(_TempFileMixin, unittest.TestCase):
    def test_file_not_found(self) -> None:
        result = self.tool.invoke({"file_path": "/nonexistent/file.txt"})
        self.assertNotEqual(result.status, RuntimeStatus.OK)
        self.assertEqual(result.structured["error_code"], ERR_FILE_NOT_FOUND)

    def test_binary_file_returns_unsupported(self) -> None:
        path = Path(self._tmpdir) / "binary.bin"
        path.write_bytes(bytes(range(256)))
        result = self.tool.invoke({"file_path": str(path)})
        self.assertNotEqual(result.status, RuntimeStatus.OK)
        self.assertEqual(result.structured["error_code"], ERR_UNSUPPORTED_TEXT_ENCODING)

    def test_empty_file_path(self) -> None:
        result = self.tool.invoke({"file_path": ""})
        self.assertNotEqual(result.status, RuntimeStatus.OK)
        self.assertEqual(result.status, RuntimeStatus.INVALID)

    def test_directory_path_returns_not_a_file_and_does_not_cache(self) -> None:
        path = Path(self._tmpdir) / "subdir"
        path.mkdir()

        result = self.tool.invoke({"file_path": str(path)})

        self.assertNotEqual(result.status, RuntimeStatus.OK)
        self.assertEqual(result.structured["error_code"], "NOT_A_FILE")
        self.assertIsNone(self.cache.get_valid(path))

    def test_invalid_offset_returns_invalid_without_caching(self) -> None:
        path = self._write_tmp("bad_offset.txt", "line\n")

        result = self.tool.invoke({"file_path": str(path), "offset": "not-an-int"})

        self.assertEqual(result.status, RuntimeStatus.INVALID)
        self.assertEqual(result.structured["error_code"], ERR_INVALID_ARGUMENT)
        self.assertIsNone(self.cache.get_valid(path))

    def test_non_positive_limit_returns_invalid_without_caching(self) -> None:
        path = self._write_tmp("bad_limit.txt", "line\n")

        result = self.tool.invoke({"file_path": str(path), "limit": 0})

        self.assertEqual(result.status, RuntimeStatus.INVALID)
        self.assertEqual(result.structured["error_code"], ERR_INVALID_ARGUMENT)
        self.assertIsNone(self.cache.get_valid(path))


class ProtocolTests(unittest.TestCase):
    class _LogicalRuntime:
        def __init__(self) -> None:
            self.logical_state = InMemoryLogicalExecutionState()

        def logical_context_for_turn(self, turn_id: str):
            lifetime = f"test:{turn_id}"
            try:
                return self.logical_state.context(lifetime)
            except KeyError:
                return self.logical_state.begin_input(
                    execution_lifetime_id=lifetime,
                    input_id=turn_id,
                )

    def test_input_model_has_required_file_path(self) -> None:
        schema = ExecutionFileCapabilitiesFileCapabilityMixinReadInput.model_json_schema(mode="validation")
        self.assertIn("file_path", schema.get("required", []))

    def test_capability_visibility_uses_explicit_logical_context_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "capability-scope.txt"
            path.write_text("visible once per context\n", encoding="utf-8")
            capability = FileCapabilityMixin()
            runtime = self._LogicalRuntime()
            first = capability.file_read(
                IntrospectionCall(
                    name="op_file_read",
                    args={"file_path": str(path)},
                    meta={
                        "direct_context_id": "read-context-a",
                        "execution_runtime": runtime,
                    },
                )
            )
            repeated = capability.file_read(
                IntrospectionCall(
                    name="op_file_read",
                    args={"file_path": str(path)},
                    meta={
                        "direct_context_id": "read-context-a",
                        "execution_runtime": runtime,
                    },
                )
            )
            other_context = capability.file_read(
                IntrospectionCall(
                    name="op_file_read",
                    args={"file_path": str(path)},
                    meta={
                        "direct_context_id": "read-context-b",
                        "execution_runtime": runtime,
                    },
                )
            )

        self.assertIn("visible once", first.text)
        self.assertEqual(repeated.text, FILE_UNCHANGED_STUB)
        self.assertIn("visible once", other_context.text)

    def test_unscoped_capability_call_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "unscoped.txt"
            path.write_text("always returned without a context id\n", encoding="utf-8")
            capability = FileCapabilityMixin()
            with self.assertRaisesRegex(
                ToolRejectedError,
                "explicit logical turn",
            ):
                capability.file_read(
                    IntrospectionCall(
                        name="op_file_read",
                        args={"file_path": str(path)},
                    )
                )


if __name__ == "__main__":
    unittest.main()
