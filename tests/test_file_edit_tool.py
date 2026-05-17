"""Tests for FileEditTool: error codes, diff output, and integration with FileStateCache."""

from __future__ import annotations

import os
import textwrap
import unittest
from pathlib import Path

from pal.execution.file_edit import (
    ERR_EMPTY_OLD_STRING,
    ERR_MULTIPLE_MATCHES,
    ERR_NO_CHANGE,
    ERR_NOT_FOUND_MATCH,
    ERR_NOT_READ,
    ERR_STALE_FILE,
    FileEditTool,
)
from pal.execution.file_state import FileStateCache
from pal.shared import RuntimeStatus


class _TempFileMixin:
    """Mixin that creates a temporary file for each test and cleans up."""

    def setUp(self) -> None:
        import tempfile

        self._tmpdir = tempfile.mkdtemp()
        self.cache = FileStateCache()
        self.tool = FileEditTool(cache=self.cache)

    def _write_tmp(self, name: str, content: str) -> Path:
        path = Path(self._tmpdir) / name
        path.write_text(content, encoding="utf-8")
        return path

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)


class NotReadErrorTests(_TempFileMixin, unittest.TestCase):
    """NOT_READ: editing a file that was never read through the cache."""

    def test_edit_without_read_returns_not_read(self) -> None:
        path = self._write_tmp("sample.txt", "hello world")
        result = self.tool.invoke({
            "file_path": str(path),
            "old_string": "hello",
            "new_string": "goodbye",
        })
        self.assertNotEqual(result.status, RuntimeStatus.OK)
        self.assertEqual(result.structured.get("error_code"), ERR_NOT_READ)

    def test_not_read_error_text_mentions_reading_first(self) -> None:
        path = self._write_tmp("sample.txt", "hello world")
        result = self.tool.invoke({
            "file_path": str(path),
            "old_string": "hello",
            "new_string": "goodbye",
        })
        self.assertIn("not been read", result.text.lower())


class StaleFileErrorTests(_TempFileMixin, unittest.TestCase):
    """STALE_FILE: file modified on disk after being cached."""

    def test_stale_file_returns_stale_error(self) -> None:
        import time

        path = self._write_tmp("stale.txt", "original content")
        self.cache.mark_read(path, "original content")

        # Modify on disk to change mtime
        time.sleep(0.05)
        path.write_text("modified externally")

        result = self.tool.invoke({
            "file_path": str(path),
            "old_string": "original",
            "new_string": "patched",
        })
        self.assertNotEqual(result.status, RuntimeStatus.OK)
        self.assertEqual(result.structured.get("error_code"), ERR_STALE_FILE)
        self.assertEqual(path.read_text(), "modified externally")
        self.assertNotIn(path, self.cache)


class NotFoundMatchErrorTests(_TempFileMixin, unittest.TestCase):
    """NOT_FOUND_MATCH: old_string not present in the cached content."""

    def test_old_string_not_found(self) -> None:
        path = self._write_tmp("miss.txt", "line one\nline two\n")
        self.cache.mark_read(path, "line one\nline two\n")

        result = self.tool.invoke({
            "file_path": str(path),
            "old_string": "line three",
            "new_string": "line drei",
        })
        self.assertNotEqual(result.status, RuntimeStatus.OK)
        self.assertEqual(result.structured.get("error_code"), ERR_NOT_FOUND_MATCH)


class MultipleMatchesErrorTests(_TempFileMixin, unittest.TestCase):
    """MULTIPLE_MATCHES: old_string appears more than once."""

    def test_multiple_matches_returns_error(self) -> None:
        path = self._write_tmp("multi.txt", "aaa\nbbb\naaa\n")
        self.cache.mark_read(path, "aaa\nbbb\naaa\n")

        result = self.tool.invoke({
            "file_path": str(path),
            "old_string": "aaa",
            "new_string": "zzz",
        })
        self.assertNotEqual(result.status, RuntimeStatus.OK)
        self.assertEqual(result.structured.get("error_code"), ERR_MULTIPLE_MATCHES)
        self.assertEqual(result.structured.get("match_count"), 2)
        self.assertEqual(path.read_text(), "aaa\nbbb\naaa\n")


class SuccessfulEditTests(_TempFileMixin, unittest.TestCase):
    """Successful edits: verify file content changes and diff output."""

    def test_basic_replacement(self) -> None:
        path = self._write_tmp("edit.txt", "hello world\n")
        self.cache.mark_read(path, "hello world\n")

        result = self.tool.invoke({
            "file_path": str(path),
            "old_string": "hello",
            "new_string": "goodbye",
        })
        self.assertEqual(result.status, RuntimeStatus.OK)
        # File on disk should be updated
        self.assertEqual(path.read_text(), "goodbye world\n")

    def test_result_contains_unified_diff(self) -> None:
        path = self._write_tmp("diff.txt", "line one\nline two\nline three\n")
        self.cache.mark_read(path, "line one\nline two\nline three\n")

        result = self.tool.invoke({
            "file_path": str(path),
            "old_string": "line two",
            "new_string": "line zwei",
        })
        self.assertEqual(result.status, RuntimeStatus.OK)
        patch = result.structured.get("patch", "")
        self.assertIn("---", patch)
        self.assertIn("+++", patch)
        self.assertIn("-line two", patch)
        self.assertIn("+line zwei", patch)

    def test_result_llm_text_has_diff(self) -> None:
        path = self._write_tmp("llm.txt", "alpha\nbeta\n")
        self.cache.mark_read(path, "alpha\nbeta\n")

        result = self.tool.invoke({
            "file_path": str(path),
            "old_string": "alpha",
            "new_string": "gamma",
        })
        self.assertIn("-", result.llm_text)
        self.assertIn("+", result.llm_text)

    def test_match_count_in_structured(self) -> None:
        path = self._write_tmp("cnt.txt", "unique_line\n")
        self.cache.mark_read(path, "unique_line\n")

        result = self.tool.invoke({
            "file_path": str(path),
            "old_string": "unique_line",
            "new_string": "replaced_line",
        })
        self.assertEqual(result.structured.get("match_count"), 1)

    def test_cache_updated_after_edit(self) -> None:
        path = self._write_tmp("chain.txt", "step1\n")
        self.cache.mark_read(path, "step1\n")

        self.tool.invoke({
            "file_path": str(path),
            "old_string": "step1",
            "new_string": "step2",
        })

        # Cache should now have "step2\n" as valid content
        cached = self.cache.get_valid(path)
        self.assertEqual(cached, "step2\n")

        # Second edit should succeed using the updated cache
        result2 = self.tool.invoke({
            "file_path": str(path),
            "old_string": "step2",
            "new_string": "step3",
        })
        self.assertEqual(result2.status, RuntimeStatus.OK)
        self.assertEqual(path.read_text(), "step3\n")

    def test_multiline_replacement(self) -> None:
        content = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
        path = self._write_tmp("multi_edit.py", content)
        self.cache.mark_read(path, content)

        result = self.tool.invoke({
            "file_path": str(path),
            "old_string": "    return 1\n",
            "new_string": "    return 42\n",
        })
        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertIn("return 42", path.read_text())
        self.assertNotIn("return 1", path.read_text())


class ValidationTests(_TempFileMixin, unittest.TestCase):
    """Input validation tests."""

    def test_missing_file_path(self) -> None:
        result = self.tool.invoke({
            "old_string": "a",
            "new_string": "b",
        })
        self.assertEqual(result.status, RuntimeStatus.INVALID)
        self.assertIn("file_path", result.text)

    def test_empty_file_path(self) -> None:
        result = self.tool.invoke({
            "file_path": "",
            "old_string": "a",
            "new_string": "b",
        })
        self.assertEqual(result.status, RuntimeStatus.INVALID)

    def test_non_string_old_string(self) -> None:
        result = self.tool.invoke({
            "file_path": "any.txt",
            "old_string": 123,
            "new_string": "b",
        })
        self.assertEqual(result.status, RuntimeStatus.INVALID)

    def test_empty_old_string_is_invalid_and_does_not_write(self) -> None:
        path = self._write_tmp("empty_old.txt", "abc\n")
        self.cache.mark_read(path, "abc\n")

        result = self.tool.invoke({
            "file_path": str(path),
            "old_string": "",
            "new_string": "x",
        })

        self.assertEqual(result.status, RuntimeStatus.INVALID)
        self.assertEqual(result.structured.get("error_code"), ERR_EMPTY_OLD_STRING)
        self.assertEqual(path.read_text(), "abc\n")

    def test_no_change_replacement_is_invalid_and_does_not_write(self) -> None:
        path = self._write_tmp("same.txt", "abc\n")
        self.cache.mark_read(path, "abc\n")

        result = self.tool.invoke({
            "file_path": str(path),
            "old_string": "abc",
            "new_string": "abc",
        })

        self.assertEqual(result.status, RuntimeStatus.INVALID)
        self.assertEqual(result.structured.get("error_code"), ERR_NO_CHANGE)
        self.assertEqual(path.read_text(), "abc\n")


class ToolProtocolTests(unittest.TestCase):
    """Verify FileEditTool satisfies the Tool protocol."""

    def test_tool_attributes(self) -> None:
        tool = FileEditTool()
        self.assertEqual(tool.name, "file_edit")
        self.assertEqual(tool.family, "system")
        self.assertIsInstance(tool.args_schema, dict)
        self.assertIsInstance(tool.result_schema, dict)
        self.assertIn("file_path", tool.args_schema.get("properties", {}))
        self.assertIn("old_string", tool.args_schema.get("properties", {}))
        self.assertIn("new_string", tool.args_schema.get("properties", {}))

    def test_ainvoke_delegates_to_invoke(self) -> None:
        import asyncio

        tool = FileEditTool()
        # No file read — should get NOT_READ error
        result = asyncio.run(
            tool.ainvoke({"file_path": "/tmp/nope.txt", "old_string": "x", "new_string": "y"})
        )
        self.assertEqual(result.structured.get("error_code"), ERR_NOT_READ)


if __name__ == "__main__":
    unittest.main()
