from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.minion.workspace_file_tools import (
    WORKSPACE_FILE_TOOL_SPECS,
    workspace_file_tool_result,
)
from pal.shared import RuntimeStatus


class _CapturingRuntime:
    def __init__(self) -> None:
        self.calls: list[CanonicalToolCall] = []

    async def execute_tool_async(self, call: CanonicalToolCall, **kwargs):
        _ = kwargs
        self.calls.append(call)
        return CanonicalToolResult(
            name=call.name,
            ok=True,
            text="ok",
            llm_text="ok",
            status=RuntimeStatus.OK,
        )


class WorkspaceFileToolContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal_workspace_file_tools_"))
        self.runtime = _CapturingRuntime()
        self.workspace = {"repo_path": str(self.root)}

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _call(self, name: str, args: dict) -> CanonicalToolResult:
        return asyncio.run(
            workspace_file_tool_result(
                CanonicalToolCall(name=name, args=args),
                self.workspace,
                self.runtime,
            )
        )

    def test_scoped_schemas_match_low_friction_call_shape(self) -> None:
        read_properties = WORKSPACE_FILE_TOOL_SPECS["op_file_read"]["parameters_schema"]["properties"]
        write_properties = WORKSPACE_FILE_TOOL_SPECS["op_file_write"]["parameters_schema"]["properties"]
        edit_properties = WORKSPACE_FILE_TOOL_SPECS["op_file_edit"]["parameters_schema"]["properties"]

        self.assertEqual({"path", "offset", "limit", "root", "reference_name"}, set(read_properties))
        self.assertEqual({"path", "content"}, set(write_properties))
        self.assertNotIn("mode", write_properties)
        self.assertIn("replace_all", edit_properties)

    def test_write_adapter_does_not_reintroduce_mode(self) -> None:
        result = self._call("op_file_write", {"path": "new.txt", "content": "hello\n"})

        self.assertTrue(result.ok, result.text)
        delegated = self.runtime.calls[-1]
        self.assertEqual(delegated.name, "op_file_write")
        self.assertEqual(delegated.args["content"], "hello\n")
        self.assertNotIn("mode", delegated.args)

    def test_edit_adapter_forwards_replace_all(self) -> None:
        path = self.root / "edit.txt"
        path.write_text("x\nx\n", encoding="utf-8")

        result = self._call(
            "op_file_edit",
            {
                "path": "edit.txt",
                "old_string": "x",
                "new_string": "y",
                "replace_all": True,
            },
        )

        self.assertTrue(result.ok, result.text)
        self.assertTrue(self.runtime.calls[-1].args["replace_all"])

    def test_read_adapter_uses_offset_and_limit(self) -> None:
        path = self.root / "read.txt"
        path.write_text("one\ntwo\n", encoding="utf-8")

        result = self._call("op_file_read", {"path": "read.txt", "offset": 2, "limit": 1})

        self.assertTrue(result.ok, result.text)
        self.assertEqual(self.runtime.calls[-1].args["offset"], 2)
        self.assertEqual(self.runtime.calls[-1].args["limit"], 1)

    def test_verifier_regression_overlay_is_read_only(self) -> None:
        test_path = self.root / "tests" / "test_router.py"
        test_path.parent.mkdir()
        test_path.write_text("def test_router():\n    assert False\n", encoding="utf-8")
        self.workspace.update(
            {
                "write_path_scopes": [{"kind": "directory", "path": "tests"}],
                "read_only_overlay_paths": ["tests/test_router.py"],
            }
        )

        for name, args in (
            (
                "op_file_edit",
                {
                    "path": "tests/test_router.py",
                    "old_string": "False",
                    "new_string": "True",
                },
            ),
            (
                "op_file_write",
                {"path": "tests/test_router.py", "content": "pass\n"},
            ),
            ("op_path_delete", {"path": "tests/test_router.py"}),
        ):
            result = self._call(name, args)
            self.assertFalse(result.ok)
            self.assertIn("verifier-owned regression test", result.text)

        self.assertEqual(self.runtime.calls, [])


if __name__ == "__main__":
    unittest.main()
