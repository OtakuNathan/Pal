"""Batch read_file (single file, multiple line blocks) regression tests.

Covers the ranges=[] surface: per-block delivery headers, partial unchanged
suppression, edit authority from batched coverage, malformed input, and the
full_view promotion when blocks tile the whole file.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from pal.execution.file_edit import FileEditTool
from pal.execution.file_read import FileReadTool, FileVisibilityCache
from pal.execution.file_state import FileStateCache
from pal.shared import RuntimeStatus


class _RangesTestMixin:
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self.cache = FileStateCache()
        self.visibility = FileVisibilityCache()
        self.tool = FileReadTool(cache=self.cache, visibility_cache=self.visibility)

    def _write_tmp(self, name: str, content: str) -> Path:
        path = Path(self._tmpdir) / name
        path.write_text(content, encoding="utf-8")
        return path

    def tearDown(self) -> None:
        shutil.rmtree(self._tmpdir, ignore_errors=True)


class BatchReadTests(_RangesTestMixin, unittest.TestCase):
    def test_ranges_delivers_multiple_blocks_with_headers(self) -> None:
        path = self._write_tmp("multi.txt", "\n".join(f"line {i}" for i in range(1, 21)))
        result = self.tool.invoke(
            {
                "file_path": str(path),
                "ranges": [
                    {"offset": 1, "limit": 3},
                    {"offset": 10, "limit": 2},
                ],
            }
        )
        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertIn("──── lines 1-3 of 20 ────", result.text)
        self.assertIn("──── lines 10-11 of 20 ────", result.text)
        self.assertIn("line 1", result.text)
        self.assertIn("line 10", result.text)
        self.assertNotIn("line 4", result.text)
        self.assertNotIn("line 9", result.text)
        blocks = result.structured["blocks"]
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0], {"start_line": 1, "end_line": 3, "truncated": True, "unchanged": False})
        self.assertEqual(blocks[1], {"start_line": 10, "end_line": 11, "truncated": True, "unchanged": False})
        self.assertEqual(result.structured["start_line"], 1)
        self.assertEqual(result.structured["end_line"], 11)
        self.assertFalse(result.structured["full_view"])

    def test_ranges_partially_unchanged_marks_covered_blocks(self) -> None:
        path = self._write_tmp("partial.txt", "\n".join(f"row {i}" for i in range(1, 31)))
        first = self.tool.invoke({"file_path": str(path), "offset": 5, "limit": 2})
        self.assertEqual(first.status, RuntimeStatus.OK)

        second = self.tool.invoke(
            {
                "file_path": str(path),
                "ranges": [
                    {"offset": 5, "limit": 2},
                    {"offset": 20, "limit": 2},
                ],
            }
        )
        self.assertEqual(second.status, RuntimeStatus.OK)
        self.assertIn("lines 5-6 unchanged; already delivered", second.text)
        self.assertIn("──── lines 20-21 of 30 ────", second.text)
        self.assertIn("row 20", second.text)
        self.assertNotIn("row 5", second.text)
        blocks = {item["start_line"]: item for item in second.structured["blocks"]}
        self.assertTrue(blocks[5]["unchanged"])
        self.assertFalse(blocks[20]["unchanged"])

    def test_all_blocks_unchanged_returns_stub(self) -> None:
        path = self._write_tmp("all.txt", "\n".join(f"v {i}" for i in range(1, 11)))
        self.tool.invoke({"file_path": str(path), "ranges": [{"offset": 2, "limit": 3}]})
        second = self.tool.invoke(
            {"file_path": str(path), "ranges": [{"offset": 2, "limit": 3}]}
        )
        self.assertTrue(second.structured["unchanged"])
        self.assertEqual(second.status, RuntimeStatus.OK)

    def test_ranges_covering_whole_file_promotes_full_view(self) -> None:
        path = self._write_tmp("tile.txt", "\n".join(f"t {i}" for i in range(1, 13)))
        result = self.tool.invoke(
            {
                "file_path": str(path),
                "ranges": [
                    {"offset": 1, "limit": 6},
                    {"offset": 7, "limit": 6},
                ],
            }
        )
        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertTrue(result.structured["full_view"])

    def test_beyond_eof_blocks_are_reported(self) -> None:
        path = self._write_tmp("short.txt", "one\ntwo\nthree\n")
        result = self.tool.invoke(
            {
                "file_path": str(path),
                "ranges": [
                    {"offset": 2, "limit": 1},
                    {"offset": 99, "limit": 4},
                ],
            }
        )
        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertIn("beyond end of file", result.text)
        blocks = result.structured["blocks"]
        self.assertEqual(len(blocks), 2)
        self.assertTrue(blocks[1].get("beyond_eof"))

    def test_malformed_ranges_rejected(self) -> None:
        path = self._write_tmp("bad.txt", "content\n")
        for ranges in ([], "nope", [{"offset": 1, "limit": 0}], [{"offset": -1}]):
            with self.subTest(ranges=ranges):
                result = self.tool.invoke({"file_path": str(path), "ranges": ranges})
                self.assertEqual(result.status, RuntimeStatus.INVALID)
                self.assertEqual(result.structured["error_code"], "INVALID_ARGUMENT")

    def test_ranges_wins_over_offset_limit(self) -> None:
        path = self._write_tmp("win.txt", "\n".join(f"w {i}" for i in range(1, 21)))
        result = self.tool.invoke(
            {
                "file_path": str(path),
                "offset": 1,
                "limit": 2,
                "ranges": [{"offset": 15, "limit": 2}],
            }
        )
        self.assertIn("lines 15-16 of 20", result.text)
        self.assertNotIn("\tw 1\n", result.text)
        self.assertNotIn("\tw 2\n", result.text)


class BatchReadEditAuthorityTests(_RangesTestMixin, unittest.TestCase):
    def test_batch_read_grants_edit_authority_for_all_blocks(self) -> None:
        path = self._write_tmp("auth.txt", "\n".join(f"a{i}" for i in range(1, 16)))
        read_result = self.tool.invoke(
            {
                "file_path": str(path),
                "ranges": [
                    {"offset": 1, "limit": 4},
                    {"offset": 12, "limit": 4},
                ],
            }
        )
        self.assertEqual(read_result.status, RuntimeStatus.OK)

        edit_tool = FileEditTool(cache=self.cache)
        edited = edit_tool.invoke(
            {
                "file_path": str(path),
                "edits": [
                    {"old_string": "a1", "new_string": "b1"},
                    {"old_string": "a14", "new_string": "b14"},
                ],
            }
        )
        self.assertEqual(edited.status, RuntimeStatus.OK, edited.llm_text)
        text = path.read_text(encoding="utf-8")
        self.assertIn("b1", text)
        self.assertIn("b14", text)

    def test_batch_read_does_not_grant_authority_for_unread_middle(self) -> None:
        path = self._write_tmp("gap.txt", "\n".join(f"g{i}" for i in range(1, 16)))
        self.tool.invoke(
            {
                "file_path": str(path),
                "ranges": [
                    {"offset": 1, "limit": 3},
                    {"offset": 13, "limit": 3},
                ],
            }
        )
        edit_tool = FileEditTool(cache=self.cache)
        edited = edit_tool.invoke(
            {"file_path": str(path), "edits": [{"old_string": "g7", "new_string": "h7"}]}
        )
        self.assertNotEqual(edited.status, RuntimeStatus.OK)
        self.assertEqual(edited.structured.get("error_code"), "PARTIAL_READ")
        covered = edited.structured.get("covered_line_ranges")
        self.assertIn([1, 3], covered)
        self.assertIn([13, 15], covered)

    def test_edit_spanning_gap_between_read_blocks_is_rejected(self) -> None:
        """A single match reaching from a covered block into the unread gap
        must not slip through per-block authorization."""

        path = self._write_tmp("span.txt", "\n".join(f"s{i}" for i in range(1, 16)))
        self.tool.invoke(
            {
                "file_path": str(path),
                "ranges": [
                    {"offset": 1, "limit": 3},
                    {"offset": 13, "limit": 3},
                ],
            }
        )
        edit_tool = FileEditTool(cache=self.cache)
        # Spans line 3 (covered) into line 4 (unread gap).
        edited = edit_tool.invoke(
            {"file_path": str(path), "edits": [{"old_string": "s3\ns4", "new_string": "x\ny"}]}
        )
        self.assertNotEqual(edited.status, RuntimeStatus.OK)
        self.assertEqual(edited.structured.get("error_code"), "PARTIAL_READ")

    def test_replace_all_with_matches_in_uncovered_region_is_rejected(self) -> None:
        path = self._write_tmp(
            "dup.txt", "\n".join(["dup"] * 15)
        )
        self.tool.invoke(
            {
                "file_path": str(path),
                "ranges": [
                    {"offset": 1, "limit": 3},
                    {"offset": 13, "limit": 3},
                ],
            }
        )
        edit_tool = FileEditTool(cache=self.cache)
        edited = edit_tool.invoke(
            {
                "file_path": str(path),
                "edits": [{"old_string": "dup", "new_string": "hit", "replace_all": True}],
            }
        )
        self.assertNotEqual(edited.status, RuntimeStatus.OK)
        self.assertEqual(edited.structured.get("error_code"), "PARTIAL_READ")


if __name__ == "__main__":
    unittest.main()
