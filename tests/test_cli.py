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

    def test_setup_upgrade_is_non_interactive_with_explicit_runtime_root(
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

    def test_provider_install_subcommand_parses_wheels_and_runtime_root(self) -> None:
        args = _build_parser().parse_args(
            [
                "provider",
                "install",
                "telegram.whl",
                "bridge.whl",
                "--runtime-root",
                "/tmp/pal-runtime",
                "--force",
            ]
        )

        self.assertEqual(args.command, "provider")
        self.assertEqual(args.provider_command, "install")
        self.assertEqual(args.wheels, [Path("telegram.whl"), Path("bridge.whl")])
        self.assertEqual(args.runtime_root, Path("/tmp/pal-runtime"))
        self.assertTrue(args.force)


if __name__ == "__main__":
    unittest.main()


def test_runtime_commands_default_to_home_and_accept_override():
    commands = [
        ['run'], ['client', '--message', 'hello'], ['tty'], ['doctor'],
        ['setup'], ['setup', '--upgrade'], ['eval', 'tools'],
        ['package', 'status'], ['package', 'install', 'demo.palpkg'],
        ['package', 'prepare', 'web_fetch'], ['llm', 'list'],
        ['provider', 'install', 'demo.whl'],
        ['bunshin', 'efficiency', 'wf-1'],
    ]
    parser = _build_parser()
    for command in commands:
        assert parser.parse_args(command).runtime_root.expanduser() == Path.home() / '.pal'
        assert parser.parse_args([*command, '--runtime-root', '/tmp/custom-pal']).runtime_root == Path('/tmp/custom-pal')


def test_setup_default_and_quoted_tilde_are_normalized(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    for extra, expected in [([], tmp_path / '.pal'), (['--runtime-root', '~/custom'], tmp_path / 'custom')]:
        monkeypatch.setattr(sys, 'argv', ['pal', 'setup', *extra])
        with patch('pal.wizard.cli.run_setup_wizard', return_value=0) as setup:
            assert main() == 0
        setup.assert_called_once_with(runtime_root=expected)
    monkeypatch.setattr(sys, 'argv', ['pal', 'setup', '--upgrade'])
    with patch('pal.wizard.cli.run_setup_upgrade', return_value=0) as upgrade:
        assert main() == 0
    upgrade.assert_called_once_with(runtime_root=tmp_path / '.pal')


def test_missing_default_runtime_does_not_create_another_pal(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(sys, 'argv', ['pal', 'run'])
    with patch('pal.main.build_runtime_app') as build:
        assert main() == 2
        build.assert_not_called()
    assert not (tmp_path / '.pal').exists()
    error = capsys.readouterr().err
    assert str(tmp_path / '.pal') in error
    assert '--runtime-root' in error and 'pal setup' in error


def test_missing_socket_reports_actual_root_without_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['pal', 'client', '--runtime-root', str(tmp_path), '--message', 'hello'])
    assert main() == 2
    error = capsys.readouterr().err
    assert str(tmp_path) in error and '--runtime-root' in error
    assert 'Traceback' not in error


def test_pal_home_is_fallback_and_explicit_root_wins(tmp_path, monkeypatch):
    from pal.cli_paths import default_runtime_root
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('PAL_HOME', '~/other-pal')
    alternate = tmp_path / 'other-pal'
    alternate.mkdir()
    assert default_runtime_root() == alternate
    assert _build_parser().parse_args(['tty']).runtime_root == alternate
    assert _build_parser().parse_args(['tty', '--runtime-root', '/explicit']).runtime_root == Path('/explicit')
    (tmp_path / '.pal').mkdir()
    assert default_runtime_root() == tmp_path / '.pal'
    assert _build_parser().parse_args(['package', 'status']).runtime_root == tmp_path / '.pal'


def test_missing_root_hint_includes_environment_option(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('PAL_HOME', raising=False)
    monkeypatch.setattr(sys, 'argv', ['pal', 'package', 'status'])
    assert main() == 2
    error = capsys.readouterr().err
    assert 'PAL_HOME' in error and '--runtime-root' in error
    assert not (tmp_path / '.pal').exists()


def test_stale_tty_socket_returns_failure(tmp_path, monkeypatch):
    import socket
    from unittest.mock import AsyncMock
    from pal.socket_client import default_socket_path
    path = default_socket_path(tmp_path)
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(path))
    monkeypatch.setattr(sys, 'argv', ['pal', 'tty', '--runtime-root', str(tmp_path)])
    with patch('pal.main.run_tty', new=AsyncMock(return_value=False)):
        assert main() == 2
