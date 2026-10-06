"""Git mount topology tests; no role socket, model, or service is required."""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pal.bunshin import sandbox
from pal.bunshin.git_shim import main as git_shim_main


class BunshinGitMountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="pal_git_mounts_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.core = self.root / "git-core"
        self.core.mkdir()
        self.shims = self.root / "shims"
        self.shims.mkdir()
        (self.shims / "git").write_text(sandbox._git_wrapper_text())
        (self.shims / "git-internal").write_text(sandbox._git_internal_wrapper_text())
        for entry in self.shims.iterdir():
            entry.chmod(0o755)
        self.git = self.executable(self.core / "git")

    def executable(self, path: Path) -> Path:
        path.write_text("#!/bin/sh\nexit 99\n")
        path.chmod(0o755)
        return path

    def mounts(self, entrypoints: tuple[Path, ...] | None = None) -> list[str]:
        args = ["--ro-bind", str(self.root), str(self.root)]
        with (
            patch.object(sandbox, "_GIT_CORE_ROOTS", (self.core,)),
            patch.object(sandbox, "_git_entrypoint_targets", return_value=entrypoints or (self.git,)),
        ):
            sandbox._append_git_shim_binds(args, self.shims)
        return args

    def triples(self, args: list[str]) -> list[list[str]]:
        return [args[index:index + 3] for index in range(len(args) - 2)]

    def test_regular_and_hardlinked_helpers_keep_distinct_shims(self) -> None:
        regular = self.executable(self.core / "git-regular")
        hardlink = self.core / "git-hardlink"
        os.link(self.git, hardlink)
        args = self.mounts()
        self.assertNotIn("--tmpfs", args)
        self.assertIn(["--ro-bind", str(self.shims / "git"), str(self.git)], self.triples(args))
        for helper in (regular, hardlink):
            self.assertIn(["--ro-bind", str(self.shims / "git-internal"), str(helper)], self.triples(args))
        self.assertEqual(self.git.stat().st_ino, hardlink.stat().st_ino)
        self.assertIn("exit 99", self.git.read_text())

    def test_symlink_farm_projects_names_without_following_targets(self) -> None:
        outside = self.executable(self.root / "outside")
        targets = {
            "git-add": "git",
            "git-chain": "git-add",
            "git-absolute": str(self.git),
            "git-external": str(outside),
            "git-dangling": "missing",
            "git-cycle-a": "git-cycle-b",
            "git-cycle-b": "git-cycle-a",
        }
        for name, target in targets.items():
            (self.core / name).symlink_to(target)
        (self.core / "resources").mkdir()
        (self.core / "readme").write_text("documentation")
        (self.core / "data-link").symlink_to("readme")
        (self.core / "broken-data").symlink_to("missing-data")
        (self.core / "git-not-executable").write_text("data")
        args = self.mounts()
        self.assertEqual(args[3:5], ["--tmpfs", str(self.core)])
        self.assertEqual(args[-2:], ["--remount-ro", str(self.core)])
        triples = self.triples(args)
        self.assertIn(["--ro-bind", str(self.shims / "git"), str(self.git)], triples)
        for name, target in targets.items():
            helper = self.core / name
            self.assertIn(["--ro-bind", str(self.shims / "git-internal"), str(helper)], triples)
            self.assertEqual(os.readlink(helper), target)
        for name in ("resources", "readme", "git-not-executable"):
            self.assertIn(["--ro-bind", str(self.core / name), str(self.core / name)], triples)
        self.assertIn(["--symlink", "readme", str(self.core / "data-link")], triples)
        self.assertIn(["--symlink", "missing-data", str(self.core / "broken-data")], triples)
        self.assertNotIn(str(outside), args)
        self.assertNotIn("--bind", args)
        self.assertIn("exit 99", outside.read_text())

    def test_symlinked_gateway_and_directory_alias_are_not_followed_as_files(self) -> None:
        self.git.unlink()
        outside = self.executable(self.root / "git-real")
        self.git.symlink_to(outside)
        helper = self.core / "git-status"
        helper.symlink_to("git")
        alias = self.root / "alias"
        alias.symlink_to(self.core, target_is_directory=True)
        args = self.mounts((self.git, alias / "git"))
        triples = self.triples(args)
        gateway = ["--ro-bind", str(self.shims / "git"), str(self.git)]
        self.assertEqual(triples.count(gateway), 1)
        self.assertIn(["--ro-bind", str(self.shims / "git-internal"), str(helper)], triples)
        self.assertNotIn(str(outside), args)
        self.assertNotIn(str(alias / "git"), args)

    def test_parent_alias_cannot_expose_hidden_siblings(self) -> None:
        visible = self.root / "visible"
        visible.mkdir()
        alias = visible / "git-core"
        alias.symlink_to(self.core, target_is_directory=True)
        (self.core / "git-add").symlink_to("git")
        (self.core / "private-sentinel").write_text("must stay hidden")
        args = ["--ro-bind", str(visible), str(visible)]
        original = list(args)
        with (
            patch.object(sandbox, "_GIT_CORE_ROOTS", (alias,)),
            patch.object(sandbox, "_git_entrypoint_targets", return_value=(alias / "git",)),
            self.assertRaisesRegex(RuntimeError, "outside an existing read-only mount"),
        ):
            sandbox._append_git_shim_binds(args, self.shims)
        self.assertEqual(args, original)
        self.assertNotIn(str(self.core / "private-sentinel"), args)

    def test_projection_cannot_replace_existing_mounts(self) -> None:
        (self.core / "git-add").symlink_to("git")
        alias = self.root / "alias"
        alias.symlink_to(self.core, target_is_directory=True)
        for destination in (self.core, self.core / "protected", alias / "protected"):
            args = ["--ro-bind", str(self.root), str(self.root), "--ro-bind", str(self.shims), str(destination)]
            original = list(args)
            with (
                patch.object(sandbox, "_GIT_CORE_ROOTS", (self.core,)),
                patch.object(sandbox, "_git_entrypoint_targets", return_value=(self.git,)),
                self.assertRaisesRegex(RuntimeError, "existing mount"),
            ):
                sandbox._append_git_shim_binds(args, self.shims)
            self.assertEqual(args, original)

    def test_projection_cannot_reveal_files_hidden_by_parent_mount(self) -> None:
        (self.core / "git-add").symlink_to("git")
        args = ["--ro-bind", str(self.root), str(self.root), "--tmpfs", str(self.root)]
        original = list(args)
        with (
            patch.object(sandbox, "_GIT_CORE_ROOTS", (self.core,)),
            patch.object(sandbox, "_git_entrypoint_targets", return_value=(self.git,)),
            self.assertRaisesRegex(RuntimeError, "hidden by an existing mount"),
        ):
            sandbox._append_git_shim_binds(args, self.shims)
        self.assertEqual(args, original)

    def test_projected_direct_helpers_block_every_argv(self) -> None:
        helper = self.core / "git-add"
        helper.symlink_to("git")
        args = self.mounts()
        source = next(row[1] for row in self.triples(args) if row[0] == "--ro-bind" and row[2] == str(helper))
        for argv in ([], ["status"], ["diff", "--stat"], ["add", "."], ["--no-pager", "-C", "/tmp", "status"]):
            with self.subTest(argv=argv):
                result = subprocess.run([source, *argv], capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 126)
                self.assertIn("blocked internal Git entry point", result.stderr)

    def test_gateway_keeps_read_argv_and_manager_mutation_classification(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.requests: list[tuple[str, dict[str, str]]] = []

            def request_sync(self, method: str, params: dict[str, str]) -> dict[str, object]:
                from pal.execution.git_tool import classify_git_command
                self.requests.append((method, params))
                classification = classify_git_command(params["cmd"])
                return {"returncode": 0 if classification.operation_kind == "read" else 126}

        client = Client()
        with (
            patch.dict(os.environ, {"PAL_BUNSHIN_RUNTIME_ROOT": str(self.root)}),
            patch("pal.bunshin.git_shim.role_gateway_client_from_env", return_value=client),
            patch("pal.bunshin.git_shim.os.getcwd", return_value=str(self.root)),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(git_shim_main(["--no-pager", "-C", "git-core", "status", "--short"]), 0)
            self.assertEqual(client.requests[-1], ("git_read", {"cmd": "status --short", "cwd": str(self.core)}))
            for argv in (["add", "."], ["commit", "-m", "no"], ["push"], ["-c", "alias.x=!touch /tmp/no", "x"]):
                self.assertEqual(git_shim_main(argv), 126)
