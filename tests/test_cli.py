from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from pal.main import _build_parser, main


class PalV2CliParserTests(unittest.TestCase):
    def test_setup_accepts_wizard_aliases(self) -> None:
        parser = _build_parser()

        self.assertEqual(parser.parse_args(["setup"]).command, "setup")
        self.assertEqual(parser.parse_args(["wizard"]).command, "setup")
        self.assertEqual(parser.parse_args(["wizzard"]).command, "setup")
        self.assertTrue(parser.parse_args(["setup", "--check"]).check)
        self.assertTrue(
            parser.parse_args(
                [
                    "setup",
                    "--upgrade",
                    "--runtime-root",
                    "/tmp/pal-runtime",
                ]
            ).upgrade
        )
        setup_args = parser.parse_args(
            ["setup", "--runtime-root", "/tmp/pal-runtime"]
        )
        self.assertEqual(setup_args.runtime_root, Path("/tmp/pal-runtime"))
        self.assertEqual(parser.parse_args(["doctor"]).command, "doctor")

    def test_fresh_runtime_debug_call_commands_are_not_registered(self) -> None:
        parser = _build_parser()
        for command in ("tool-call", "cap-call"):
            with self.subTest(command=command), self.assertRaises(SystemExit):
                parser.parse_args([command])

    def test_setup_passes_explicit_runtime_root_to_wizard(self) -> None:
        runtime_root = Path("/tmp/pal-runtime")
        with (
            patch.object(
                sys,
                "argv",
                ["pal", "setup", "--runtime-root", str(runtime_root)],
            ),
            patch(
                "pal.wizard.cli.run_setup_wizard",
                return_value=0,
            ) as run_setup_wizard,
        ):
            self.assertEqual(main(), 0)

        run_setup_wizard.assert_called_once_with(runtime_root=runtime_root)

    def test_setup_upgrade_is_non_interactive_and_requires_runtime_root(
        self,
    ) -> None:
        runtime_root = Path("/tmp/pal-runtime")
        with (
            patch.object(
                sys,
                "argv",
                [
                    "pal",
                    "setup",
                    "--upgrade",
                    "--runtime-root",
                    str(runtime_root),
                ],
            ),
            patch(
                "pal.wizard.cli.run_setup_upgrade",
                return_value=0,
            ) as run_setup_upgrade,
        ):
            self.assertEqual(main(), 0)

        run_setup_upgrade.assert_called_once_with(runtime_root=runtime_root)

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
