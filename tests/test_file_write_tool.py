"""Tests for FileWriteTool: create/overwrite/append writes and cache integration."""

from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

from pal.execution.file_edit import FileEditTool
from pal.execution.file_state import FileStateCache
from pal.execution.file_write import (
    ERR_BINARY_CONTENT,
    ERR_CONTENT_TOO_LARGE,
    ERR_FILE_EXISTS,
    ERR_FILE_NOT_FOUND,
    ERR_INVALID_MODE,
    ERR_MISSING_CONTENT,
    ERR_MISSING_FILE_PATH,
    ERR_NOT_READ,
    ERR_PARENT_NOT_DIRECTORY,
    ERR_STALE_FILE,
    FileWriteTool,
    MAX_CONTENT_BYTES,
)
from pal.shared import RuntimeStatus


class _TempFileMixin:
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self.cache = FileStateCache()
        self.tool = FileWriteTool(cache=self.cache)

    def _path(self, name: str) -> Path:
        return Path(self._tmpdir) / name

    def tearDown(self) -> None:
        shutil.rmtree(self._tmpdir, ignore_errors=True)


class CreateModeTests(_TempFileMixin, unittest.TestCase):
    def test_create_new_file(self) -> None:
        path = self._path("new.txt")

        result = self.tool.invoke({"file_path": str(path), "content": "hello\n"})

        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertTrue(result.structured["created"])
        self.assertEqual(result.structured["bytes_written"], 6)
        self.assertEqual(path.read_text(encoding="utf-8"), "hello\n")

    def test_create_existing_file_fails_without_modifying(self) -> None:
        path = self._path("exists.txt")
        path.write_text("original\n", encoding="utf-8")

        result = self.tool.invoke({"file_path": str(path), "content": "replacement\n"})

        self.assertEqual(result.status, RuntimeStatus.INVALID)
        self.assertEqual(result.structured["error_code"], ERR_FILE_EXISTS)
        self.assertEqual(path.read_text(encoding="utf-8"), "original\n")

    def test_create_makes_missing_parent_directory(self) -> None:
        path = self._path("missing/new.txt")

        result = self.tool.invoke({"file_path": str(path), "content": "hello\n"})

        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertTrue(result.structured["created"])
        self.assertEqual(path.read_text(encoding="utf-8"), "hello\n")
        self.assertTrue(path.parent.is_dir())

    def test_parent_must_be_directory(self) -> None:
        parent = self._path("parent.txt")
        parent.write_text("not a directory", encoding="utf-8")
        path = parent / "child.txt"

        result = self.tool.invoke({"file_path": str(path), "content": "hello\n"})

        self.assertEqual(result.status, RuntimeStatus.INVALID)
        self.assertEqual(result.structured["error_code"], ERR_PARENT_NOT_DIRECTORY)

    def test_create_warms_cache_for_subsequent_edit(self) -> None:
        path = self._path("chain.txt")
        create = self.tool.invoke({"file_path": str(path), "content": "step1\n"})
        self.assertEqual(create.status, RuntimeStatus.OK)

        edit_tool = FileEditTool(cache=self.cache)
        edit = edit_tool.invoke({
            "file_path": str(path),
            "old_string": "step1",
            "new_string": "step2",
        })

        self.assertEqual(edit.status, RuntimeStatus.OK)
        self.assertEqual(path.read_text(encoding="utf-8"), "step2\n")

    def test_create_warms_cache_for_subsequent_append(self) -> None:
        path = self._path("append_chain.txt")
        create = self.tool.invoke({"file_path": str(path), "content": "first\n"})
        self.assertEqual(create.status, RuntimeStatus.OK)

        append = self.tool.invoke({"file_path": str(path), "content": "second\n", "mode": "append"})

        self.assertEqual(append.status, RuntimeStatus.OK)
        self.assertEqual(path.read_text(encoding="utf-8"), "first\nsecond\n")
        self.assertEqual(self.cache.get_valid(path), "first\nsecond\n")


class OverwriteModeTests(_TempFileMixin, unittest.TestCase):
    def test_overwrite_after_read(self) -> None:
        path = self._path("overwrite.txt")
        path.write_text("old\n", encoding="utf-8")
        self.cache.mark_read(path, "old\n")

        result = self.tool.invoke({"file_path": str(path), "content": "new\n", "mode": "overwrite"})

        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertEqual(result.structured["mode"], "overwrite")
        self.assertFalse(result.structured["created"])
        self.assertEqual(path.read_text(encoding="utf-8"), "new\n")
        self.assertEqual(self.cache.get_valid(path), "new\n")

    def test_overwrite_without_read_fails(self) -> None:
        path = self._path("noread.txt")
        path.write_text("old\n", encoding="utf-8")

        result = self.tool.invoke({"file_path": str(path), "content": "new\n", "mode": "overwrite"})

        self.assertEqual(result.status, RuntimeStatus.FORBIDDEN)
        self.assertEqual(result.structured["error_code"], ERR_NOT_READ)
        self.assertEqual(path.read_text(encoding="utf-8"), "old\n")

    def test_overwrite_stale_file_fails(self) -> None:
        path = self._path("stale.txt")
        path.write_text("v1\n", encoding="utf-8")
        self.cache.mark_read(path, "v1\n")
        time.sleep(0.05)
        path.write_text("v2\n", encoding="utf-8")

        result = self.tool.invoke({"file_path": str(path), "content": "v3\n", "mode": "overwrite"})

        self.assertEqual(result.status, RuntimeStatus.FORBIDDEN)
        self.assertEqual(result.structured["error_code"], ERR_STALE_FILE)
        self.assertEqual(path.read_text(encoding="utf-8"), "v2\n")

    def test_overwrite_nonexistent_file_fails(self) -> None:
        path = self._path("missing.txt")

        result = self.tool.invoke({"file_path": str(path), "content": "new\n", "mode": "overwrite"})

        self.assertEqual(result.status, RuntimeStatus.ERROR)
        self.assertEqual(result.structured["error_code"], ERR_FILE_NOT_FOUND)


class AppendModeTests(_TempFileMixin, unittest.TestCase):
    def test_append_after_read(self) -> None:
        path = self._path("append.txt")
        path.write_text("first\n", encoding="utf-8")
        self.cache.mark_read(path, "first\n")

        result = self.tool.invoke({"file_path": str(path), "content": "second\n", "mode": "append"})

        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertEqual(result.structured["mode"], "append")
        self.assertFalse(result.structured["created"])
        self.assertEqual(path.read_text(encoding="utf-8"), "first\nsecond\n")
        self.assertEqual(self.cache.get_valid(path), "first\nsecond\n")

    def test_append_without_read_fails(self) -> None:
        path = self._path("noread_append.txt")
        path.write_text("first\n", encoding="utf-8")

        result = self.tool.invoke({"file_path": str(path), "content": "second\n", "mode": "append"})

        self.assertEqual(result.status, RuntimeStatus.FORBIDDEN)
        self.assertEqual(result.structured["error_code"], ERR_NOT_READ)
        self.assertEqual(path.read_text(encoding="utf-8"), "first\n")

    def test_append_stale_file_fails(self) -> None:
        path = self._path("stale_append.txt")
        path.write_text("v1\n", encoding="utf-8")
        self.cache.mark_read(path, "v1\n")
        time.sleep(0.05)
        path.write_text("v2\n", encoding="utf-8")

        result = self.tool.invoke({"file_path": str(path), "content": "v3\n", "mode": "append"})

        self.assertEqual(result.status, RuntimeStatus.FORBIDDEN)
        self.assertEqual(result.structured["error_code"], ERR_STALE_FILE)
        self.assertEqual(path.read_text(encoding="utf-8"), "v2\n")

    def test_append_nonexistent_file_fails(self) -> None:
        path = self._path("missing_append.txt")

        result = self.tool.invoke({"file_path": str(path), "content": "new\n", "mode": "append"})

        self.assertEqual(result.status, RuntimeStatus.ERROR)
        self.assertEqual(result.structured["error_code"], ERR_FILE_NOT_FOUND)


class ValidationTests(_TempFileMixin, unittest.TestCase):
    def test_missing_file_path(self) -> None:
        result = self.tool.invoke({"content": "hello\n"})

        self.assertEqual(result.status, RuntimeStatus.INVALID)
        self.assertEqual(result.structured["error_code"], ERR_MISSING_FILE_PATH)

    def test_missing_content(self) -> None:
        result = self.tool.invoke({"file_path": str(self._path("none.txt")), "content": None})

        self.assertEqual(result.status, RuntimeStatus.INVALID)
        self.assertEqual(result.structured["error_code"], ERR_MISSING_CONTENT)

    def test_non_string_content(self) -> None:
        result = self.tool.invoke({"file_path": str(self._path("bad.txt")), "content": 123})

        self.assertEqual(result.status, RuntimeStatus.INVALID)
        self.assertEqual(result.structured["error_code"], ERR_MISSING_CONTENT)

    def test_binary_content_rejected(self) -> None:
        path = self._path("binary.txt")

        result = self.tool.invoke({"file_path": str(path), "content": "hello\x00world"})

        self.assertEqual(result.status, RuntimeStatus.INVALID)
        self.assertEqual(result.structured["error_code"], ERR_BINARY_CONTENT)
        self.assertFalse(path.exists())

    def test_content_too_large_rejected(self) -> None:
        path = self._path("large.txt")

        result = self.tool.invoke({"file_path": str(path), "content": "x" * (MAX_CONTENT_BYTES + 1)})

        self.assertEqual(result.status, RuntimeStatus.INVALID)
        self.assertEqual(result.structured["error_code"], ERR_CONTENT_TOO_LARGE)
        self.assertFalse(path.exists())

    def test_empty_string_content_is_valid(self) -> None:
        path = self._path("empty.txt")

        result = self.tool.invoke({"file_path": str(path), "content": ""})

        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertEqual(path.read_text(encoding="utf-8"), "")

    def test_invalid_mode_rejected(self) -> None:
        path = self._path("mode.txt")

        result = self.tool.invoke({"file_path": str(path), "content": "hello\n", "mode": "delete"})

        self.assertEqual(result.status, RuntimeStatus.INVALID)
        self.assertEqual(result.structured["error_code"], ERR_INVALID_MODE)
        self.assertFalse(path.exists())


class ToolProtocolTests(unittest.TestCase):
    def test_tool_attributes(self) -> None:
        tool = FileWriteTool()

        self.assertEqual(tool.name, "op_file_write")
        self.assertEqual(tool.family, "system")
        self.assertIn("file_path", tool.args_schema.get("required", []))
        self.assertIn("content", tool.args_schema.get("required", []))
        self.assertEqual(tool.args_schema["properties"]["mode"]["enum"], ["create", "overwrite", "append"])

    def test_ainvoke_delegates_to_invoke(self) -> None:
        import asyncio

        tool = FileWriteTool()
        result = asyncio.run(tool.ainvoke({"file_path": "", "content": "data"}))

        self.assertEqual(result.structured["error_code"], ERR_MISSING_FILE_PATH)


if __name__ == "__main__":
    unittest.main()
