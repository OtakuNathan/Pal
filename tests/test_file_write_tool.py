"""Tests for low-friction create-or-overwrite file writes."""

from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

from pal.execution.file_edit import FileEditTool
from pal.execution.file_state import FileStateCache
from pal.execution.generated_tool_models import ExecutionFileCapabilitiesFileCapabilityMixinWriteInput
from pal.execution.file_write import (
    ERR_BINARY_CONTENT,
    ERR_CONTENT_TOO_LARGE,
    ERR_MISSING_CONTENT,
    ERR_MISSING_FILE_PATH,
    ERR_NOT_READ,
    ERR_PARENT_NOT_DIRECTORY,
    ERR_PARTIAL_READ,
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


class CreateTests(_TempFileMixin, unittest.TestCase):
    def test_write_creates_new_file_and_parent(self) -> None:
        path = self._path("missing/new.txt")

        result = self.tool.invoke({"file_path": str(path), "content": "hello\n"})

        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertTrue(result.structured["created"])
        self.assertEqual(result.structured["operation"], "create")
        self.assertEqual(result.structured["bytes_written"], 6)
        self.assertEqual(path.read_text(encoding="utf-8"), "hello\n")
        self.assertTrue(path.parent.is_dir())

    def test_parent_must_be_directory(self) -> None:
        parent = self._path("parent.txt")
        parent.write_text("not a directory", encoding="utf-8")

        result = self.tool.invoke({"file_path": str(parent / "child.txt"), "content": "hello\n"})

        self.assertEqual(result.status, RuntimeStatus.INVALID)
        self.assertEqual(result.structured["error_code"], ERR_PARENT_NOT_DIRECTORY)

    def test_create_warms_cache_for_subsequent_edit(self) -> None:
        path = self._path("chain.txt")
        create = self.tool.invoke({"file_path": str(path), "content": "step1\n"})

        edit = FileEditTool(cache=self.cache).invoke(
            {
                "file_path": str(path),
                "old_string": "step1",
                "new_string": "step2",
            }
        )

        self.assertEqual(create.status, RuntimeStatus.OK)
        self.assertEqual(edit.status, RuntimeStatus.OK)
        self.assertEqual(path.read_text(encoding="utf-8"), "step2\n")


class OverwriteTests(_TempFileMixin, unittest.TestCase):
    def test_write_overwrites_existing_file_after_full_read(self) -> None:
        path = self._path("overwrite.txt")
        path.write_text("old\n", encoding="utf-8")
        self.cache.mark_read(path, "old\n")

        result = self.tool.invoke({"file_path": str(path), "content": "new\n"})

        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertEqual(result.structured["operation"], "update")
        self.assertFalse(result.structured["created"])
        self.assertIn("-old", result.structured["patch"])
        self.assertIn("+new", result.structured["patch"])
        self.assertEqual(path.read_text(encoding="utf-8"), "new\n")
        self.assertEqual(self.cache.get_valid(path), "new\n")

    def test_overwrite_without_read_fails(self) -> None:
        path = self._path("noread.txt")
        path.write_text("old\n", encoding="utf-8")

        result = self.tool.invoke({"file_path": str(path), "content": "new\n"})

        self.assertEqual(result.status, RuntimeStatus.FORBIDDEN)
        self.assertEqual(result.structured["error_code"], ERR_NOT_READ)
        self.assertEqual(path.read_text(encoding="utf-8"), "old\n")

    def test_overwrite_after_partial_read_fails(self) -> None:
        path = self._path("partial.txt")
        path.write_text("one\ntwo\n", encoding="utf-8")
        self.cache.mark_read(path, "one\ntwo\n", full_view=False, view=(1, 1))

        result = self.tool.invoke({"file_path": str(path), "content": "new\n"})

        self.assertEqual(result.status, RuntimeStatus.FORBIDDEN)
        self.assertEqual(result.structured["error_code"], ERR_PARTIAL_READ)
        self.assertEqual(path.read_text(encoding="utf-8"), "one\ntwo\n")

    def test_overwrite_stale_file_fails(self) -> None:
        path = self._path("stale.txt")
        path.write_text("v1\n", encoding="utf-8")
        self.cache.mark_read(path, "v1\n")
        time.sleep(0.05)
        path.write_text("v2\n", encoding="utf-8")

        result = self.tool.invoke({"file_path": str(path), "content": "v3\n"})

        self.assertEqual(result.status, RuntimeStatus.FORBIDDEN)
        self.assertEqual(result.structured["error_code"], ERR_STALE_FILE)
        self.assertEqual(path.read_text(encoding="utf-8"), "v2\n")


class ValidationTests(_TempFileMixin, unittest.TestCase):
    def test_missing_file_path(self) -> None:
        result = self.tool.invoke({"content": "hello\n"})

        self.assertEqual(result.status, RuntimeStatus.INVALID)
        self.assertEqual(result.structured["error_code"], ERR_MISSING_FILE_PATH)

    def test_missing_or_non_string_content(self) -> None:
        for content in (None, 123):
            with self.subTest(content=content):
                result = self.tool.invoke(
                    {"file_path": str(self._path("bad.txt")), "content": content}
                )
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
        result = self.tool.invoke(
            {"file_path": str(path), "content": "x" * (MAX_CONTENT_BYTES + 1)}
        )

        self.assertEqual(result.status, RuntimeStatus.INVALID)
        self.assertEqual(result.structured["error_code"], ERR_CONTENT_TOO_LARGE)
        self.assertFalse(path.exists())

    def test_empty_string_content_is_valid(self) -> None:
        path = self._path("empty.txt")
        result = self.tool.invoke({"file_path": str(path), "content": ""})

        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertEqual(path.read_text(encoding="utf-8"), "")


class ToolProtocolTests(unittest.TestCase):
    def test_schema_has_no_mode(self) -> None:
        tool = FileWriteTool()
        schema = ExecutionFileCapabilitiesFileCapabilityMixinWriteInput.model_json_schema(mode="validation")

        self.assertTrue(callable(tool.invoke))
        self.assertEqual(schema.get("required"), ["file_path", "content"])
        self.assertNotIn("mode", schema["properties"])

    def test_ainvoke_delegates_to_invoke(self) -> None:
        import asyncio

        result = asyncio.run(FileWriteTool().ainvoke({"file_path": "", "content": "data"}))
        self.assertEqual(result.structured["error_code"], ERR_MISSING_FILE_PATH)


if __name__ == "__main__":
    unittest.main()
