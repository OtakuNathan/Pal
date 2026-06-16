from __future__ import annotations

import argparse
import asyncio
import tempfile
import unittest
from pathlib import Path

from pal.main import _build_parser, _run_async


class PalV2CliDebugCallsTests(unittest.TestCase):
    def test_setup_accepts_wizard_aliases(self) -> None:
        parser = _build_parser()

        self.assertEqual(parser.parse_args(["setup"]).command, "setup")
        self.assertEqual(parser.parse_args(["wizard"]).command, "setup")
        self.assertEqual(parser.parse_args(["wizzard"]).command, "setup")
        self.assertTrue(parser.parse_args(["setup", "--check"]).check)
        self.assertEqual(parser.parse_args(["doctor"]).command, "doctor")

    def test_tool_call_executes_canonical_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            exit_code = asyncio.run(
                _run_async(
                    argparse.Namespace(
                        command="tool-call",
                        runtime_root=runtime_root,
                        name="shell",
                        args='{"cmd":"echo hello"}',
                    )
                )
            )
        self.assertEqual(exit_code, 0)

    def test_cap_call_executes_capability_directly(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            exit_code = asyncio.run(
                _run_async(
                    argparse.Namespace(
                        command="cap-call",
                        runtime_root=runtime_root,
                        name="shell",
                        args='{"cmd":"echo hello"}',
                    )
                )
            )
        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
