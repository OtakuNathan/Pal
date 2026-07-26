from __future__ import annotations

import unittest
from pathlib import Path

from pal.main import _build_parser


class PalV2CliParserTests(unittest.TestCase):
    def test_setup_accepts_wizard_aliases(self) -> None:
        parser = _build_parser()

        self.assertEqual(parser.parse_args(["setup"]).command, "setup")
        self.assertEqual(parser.parse_args(["wizard"]).command, "setup")
        self.assertEqual(parser.parse_args(["wizzard"]).command, "setup")
        self.assertTrue(parser.parse_args(["setup", "--check"]).check)
        self.assertEqual(parser.parse_args(["doctor"]).command, "doctor")

    def test_fresh_runtime_debug_call_commands_are_not_registered(self) -> None:
        parser = _build_parser()
        for command in ("tool-call", "cap-call"):
            with self.subTest(command=command), self.assertRaises(SystemExit):
                parser.parse_args([command])

    def test_tty_subcommand_parses_with_runtime_root(self) -> None:
        args = _build_parser().parse_args(
            ["tty", "--runtime-root", "/tmp/pal-runtime"]
        )

        self.assertEqual(args.command, "tty")
        self.assertEqual(args.runtime_root, Path("/tmp/pal-runtime"))

    def test_client_subcommand_still_parses(self) -> None:
        args = _build_parser().parse_args(
            [
                "client",
                "--runtime-root",
                "/tmp/pal-runtime",
                "--message",
                "hello",
            ]
        )

        self.assertEqual(args.command, "client")
        self.assertEqual(args.message, "hello")


if __name__ == "__main__":
    unittest.main()
