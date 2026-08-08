from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from pal.execution.path_delete import (
    ERR_DIRECTORY_REQUIRES_RECURSIVE,
    ERR_INVALID_SHA256,
    ERR_PATH_NOT_FOUND,
    ERR_SHA256_MISMATCH,
    PathDeleteTool,
)
from pal.shared import RuntimeStatus


class _TempPathMixin:
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self.tool = PathDeleteTool()

    def _path(self, name: str) -> Path:
        return Path(self._tmpdir) / name

    def tearDown(self) -> None:
        shutil.rmtree(self._tmpdir, ignore_errors=True)


class PathDeleteIndependenceTests(_TempPathMixin, unittest.TestCase):
    def test_path_delete_file_without_file_state_succeeds(self) -> None:
        path = self._path("sample.txt")
        path.write_text("hello\n", encoding="utf-8")

        result = self.tool.invoke({"file_path": str(path)})

        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertFalse(path.exists())
        self.assertEqual(result.structured["path_kind"], "file")

    def test_delete_reports_digest_without_expected_sha(self) -> None:
        path = self._path("digest.txt")
        path.write_text("hello\n", encoding="utf-8")

        result = self.tool.invoke({"file_path": str(path)})

        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertFalse(path.exists())
        self.assertEqual(result.structured["sha256"], hashlib.sha256(b"hello\n").hexdigest())


class PathDeleteShaTests(_TempPathMixin, unittest.TestCase):
    def test_expected_sha_allows_path_delete_file_without_read(self) -> None:
        path = self._path("sha.txt")
        path.write_text("payload\n", encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()

        result = self.tool.invoke({"file_path": str(path), "expected_sha256": digest})

        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertFalse(path.exists())

    def test_expected_sha_mismatch_fails_without_deleting(self) -> None:
        path = self._path("sha_miss.txt")
        path.write_text("payload\n", encoding="utf-8")

        result = self.tool.invoke({"file_path": str(path), "expected_sha256": "0" * 64})

        self.assertEqual(result.status, RuntimeStatus.FORBIDDEN)
        self.assertEqual(result.structured["error_code"], ERR_SHA256_MISMATCH)
        self.assertTrue(path.exists())

    def test_invalid_expected_sha_is_rejected(self) -> None:
        path = self._path("sha_bad.txt")
        path.write_text("payload\n", encoding="utf-8")

        result = self.tool.invoke({"file_path": str(path), "expected_sha256": "not-a-digest"})

        self.assertEqual(result.status, RuntimeStatus.INVALID)
        self.assertEqual(result.structured["error_code"], ERR_INVALID_SHA256)
        self.assertTrue(path.exists())


class PathDeleteValidationTests(_TempPathMixin, unittest.TestCase):
    def test_missing_path_fails(self) -> None:
        result = self.tool.invoke({"file_path": str(self._path("missing.txt")), "expected_sha256": "0" * 64})

        self.assertEqual(result.status, RuntimeStatus.ERROR)
        self.assertEqual(result.structured["error_code"], ERR_PATH_NOT_FOUND)

    def test_directory_requires_recursive(self) -> None:
        path = self._path("dir")
        path.mkdir()

        result = self.tool.invoke({"file_path": str(path)})

        self.assertEqual(result.status, RuntimeStatus.FORBIDDEN)
        self.assertEqual(result.structured["error_code"], ERR_DIRECTORY_REQUIRES_RECURSIVE)
        self.assertTrue(path.exists())

    def test_recursive_directory_delete_uses_structured_entrypoint(self) -> None:
        path = self._path("cache")
        path.mkdir()
        (path / "artifact.pyc").write_bytes(b"cache")

        result = self.tool.invoke({"file_path": str(path), "recursive": True})

        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertFalse(path.exists())
        self.assertEqual(result.structured["path_kind"], "directory")
        self.assertTrue(result.structured["recursive"])


if __name__ == "__main__":
    unittest.main()
