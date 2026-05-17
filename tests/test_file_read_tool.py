"""Tests for FileReadTool: basic read, offset/limit, caching, and error handling."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from pal.execution.file_edit import FileEditTool
from pal.execution.file_read import (
    ERR_FILE_NOT_FOUND,
    ERR_INVALID_ARGUMENT,
    ERR_UNSUPPORTED_TEXT_ENCODING,
    FileReadTool,
)
from pal.execution.file_state import FileStateCache
from pal.shared import RuntimeStatus


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

    def test_cache_stores_full_content_not_slice(self) -> None:
        """Even when reading with offset/limit, the full file is cached."""
        content = "\n".join(f"line {i}" for i in range(50))
        path = self._write_tmp("full_cache.txt", content)
        self.tool.invoke({"file_path": str(path), "offset": 1, "limit": 5})

        cached = self.cache.get_valid(str(path))
        self.assertEqual(cached, content)


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
    def test_tool_name(self) -> None:
        tool = FileReadTool()
        self.assertEqual(tool.name, "file_read")

    def test_args_schema_has_required(self) -> None:
        tool = FileReadTool()
        self.assertIn("file_path", tool.args_schema.get("required", []))


if __name__ == "__main__":
    unittest.main()
