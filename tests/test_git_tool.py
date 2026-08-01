from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, new_tool_call

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pal.core import PalCore
from pal.execution import register_with_core as register_execution_with_core
from pal.execution.git_tool import GitTool, classify_git_command
from pal.shared import RuntimeStatus


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )


@unittest.skipIf(shutil.which("git") is None, "git executable is required")
class GitToolTests(unittest.TestCase):
    def test_classifies_read_only_and_conservative_mutations(self) -> None:
        status = classify_git_command("git status --short")
        self.assertTrue(status.allowed)
        self.assertFalse(status.is_mutation)
        self.assertEqual(status.tokens, ("status", "--short"))

        restore = classify_git_command("restore --source HEAD -- src/app.py")
        self.assertTrue(restore.allowed)
        self.assertTrue(restore.is_mutation)

        revert = classify_git_command("revert --no-commit HEAD~1")
        self.assertTrue(revert.allowed)
        self.assertTrue(revert.is_mutation)

        abort = classify_git_command("revert --abort")
        self.assertTrue(abort.allowed)
        self.assertTrue(abort.is_mutation)

    def test_blocks_unsafe_or_shell_git_commands(self) -> None:
        blocked = [
            "reset --hard",
            "clean -fd",
            "commit -m done",
            "restore --staged -- src/app.py",
            "restore --pathspec-from-file paths.txt -- src/app.py",
            "git -C /tmp status",
            "status --short && rm -rf .",
            "branch new-feature",
            "branch -f new-feature HEAD",
            "diff --output=/tmp/pal-git-output HEAD",
            "diff --no-index /etc/passwd /etc/hosts",
            "blame --contents /etc/passwd HEAD -- README.md",
            "grep --open-files-in-pager=cat needle",
        ]
        for command in blocked:
            with self.subTest(command=command):
                policy = classify_git_command(command)
                self.assertFalse(policy.allowed)
                self.assertFalse(policy.is_mutation)
                self.assertTrue(policy.reason)

        for command in (
            "branch",
            "branch --show-current",
            "branch --list feature/*",
            "branch --contains HEAD",
            "branch --merged main",
            "branch --points-at HEAD",
        ):
            with self.subTest(command=command):
                policy = classify_git_command(command)
                self.assertEqual(policy.operation_kind, "read")

    def test_classifies_comprehensive_local_read_only_git_surface(self) -> None:
        commands = (
            "annotate HEAD -- README.md",
            "archive --format=tar HEAD",
            "cat-file -t HEAD",
            "check-attr diff -- README.md",
            "check-ignore -q ignored.txt",
            "check-mailmap user@example.com",
            "check-ref-format refs/heads/main",
            "cherry HEAD upstream/main",
            "count-objects -v",
            "describe --always HEAD",
            "diff-files --name-only",
            "diff-index --name-only HEAD",
            "diff-tree --name-only HEAD",
            "for-each-ref --format=%(refname) refs/heads",
            "fsck --no-progress",
            "ls-tree -r HEAD",
            "merge-base HEAD main",
            "name-rev HEAD",
            "patch-id --stable",
            "range-diff main...HEAD",
            "rev-list --count HEAD",
            "shortlog -s HEAD",
            "show-ref --heads",
            "show-branch --all",
            "verify-pack objects/pack/example.idx",
            "verify-commit HEAD",
            "verify-tag release-1",
            "var GIT_DEFAULT_BRANCH",
            "version",
            "whatchanged -1 HEAD",
            "tag --list release-*",
            "stash list",
            "stash show --stat stash@{0}",
            "reflog show HEAD",
            "worktree list --porcelain",
            "notes list",
            "sparse-checkout list",
            "submodule status",
            "remote -v",
            "remote get-url --all origin",
            "remote show -n origin",
            "symbolic-ref --short HEAD",
        )
        for command in commands:
            with self.subTest(command=command):
                policy = classify_git_command(command)
                self.assertEqual(policy.operation_kind, "read", policy.reason)

    def test_blocks_mutating_modes_on_mixed_git_subcommands(self) -> None:
        commands = (
            "archive --output=/tmp/archive.tar HEAD",
            "archive -o /tmp/archive.tar HEAD",
            "archive --add-file=/etc/passwd HEAD",
            "archive --remote=/tmp/other.git HEAD",
            "cat-file --filters HEAD:README.md",
            "fsck --lost-found",
            "tag release-1",
            "tag --delete release-1",
            "stash push -m checkpoint",
            "stash drop stash@{0}",
            "reflog expire --all",
            "worktree add ../other",
            "notes add -m note HEAD",
            "sparse-checkout set src",
            "submodule update --init",
            "remote add origin https://example.invalid/repo.git",
            "remote show origin",
            "symbolic-ref HEAD refs/heads/other",
        )
        for command in commands:
            with self.subTest(command=command):
                policy = classify_git_command(command)
                self.assertEqual(policy.operation_kind, "blocked")
                self.assertTrue(policy.reason)

    def test_read_only_status_returns_llm_friendly_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git(root, "init")
            (root / "new.txt").write_text("new\n", encoding="utf-8")

            result = GitTool().invoke({"cmd": "status --short", "cwd": str(root)})

            self.assertEqual(result.status, RuntimeStatus.OK)
            assert result.structured is not None
            self.assertEqual(result.structured["classification"]["operation_kind"], "read")
            self.assertIn("?? new.txt", result.structured["stdout"])
            self.assertIn("git status", result.llm_text)

    def test_read_only_status_matches_immutable_facade_output_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git(root, "init")
            (root / "new.txt").write_text("new\n", encoding="utf-8")
            core = PalCore()
            register_execution_with_core(core.context)
            core.publish_module_capabilities("execution")

            result = core.context.execution_runtime.execute_tool(
                new_tool_call(
                    name="git",
                    args={"cmd": "status --short", "cwd": str(root)},
                )
            )

            self.assertTrue(result.ok, result.llm_text)
            output = result.structured
            self.assertEqual(output["cwd"], str(root))
            self.assertEqual(output["tokens"], ["git", "status", "--short"])
            self.assertIn("?? new.txt", output["stdout"])

    def test_restore_mutation_records_audit_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git(root, "init")
            _git(root, "config", "user.email", "pal-test@example.com")
            _git(root, "config", "user.name", "Pal Test")
            tracked = root / "tracked.txt"
            tracked.write_text("original\n", encoding="utf-8")
            _git(root, "add", "tracked.txt")
            _git(root, "commit", "-m", "initial")
            tracked.write_text("changed\n", encoding="utf-8")

            result = GitTool().invoke({"cmd": "restore -- tracked.txt", "cwd": str(root)})

            self.assertEqual(result.status, RuntimeStatus.OK)
            self.assertEqual(tracked.read_text(encoding="utf-8"), "original\n")
            assert result.structured is not None
            self.assertEqual(result.structured["classification"]["operation_kind"], "mutation")
            self.assertTrue(result.structured["audit_id"].startswith("git_audit_"))
            self.assertIn("M tracked.txt", result.structured["before_status"])
            self.assertEqual(result.structured["after_status"], "")
            self.assertEqual(result.structured["changed_files"], [])
            self.assertIn("before_head:", result.llm_text)


if __name__ == "__main__":
    unittest.main()
