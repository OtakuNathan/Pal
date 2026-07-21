from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pal.llm import EndpointResolver, LLMRuntime
from pal.llm.contracts import CanonicalLLMOutcome, CanonicalLLMRequest, CanonicalToolCall, CanonicalToolResult, LLMPreflightAdvice, LLMPreflightRequest
from pal.minion.manager import MinionManager, MinionRunState
from pal.minion.llm_broker import (
    MinionBrokerLLMRuntime,
    llm_outcome_from_payload,
    llm_outcome_to_payload,
    llm_request_from_payload,
    llm_request_to_payload,
    preflight_advice_from_payload,
    preflight_advice_to_payload,
    preflight_request_from_payload,
    preflight_request_to_payload,
)
from pal.minion.runner import MinionRunner, MinionRuntimeBundle, _minion_temperature
from pal.minion.prompt_adapter import render_minion_task_prompt
from pal.minion.sandbox import (
    build_sandboxed_runner_invocation,
    ensure_sandbox_files,
    _git_worktree_metadata_bind_paths,
    minion_sandbox_scratch_dir,
    scrub_minion_sandbox_env,
    with_minion_sandbox_metadata,
)
from pal.shared import RuntimeStatus, MinionInvocationPack


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout or f"git {' '.join(args)} failed")
    return result


class MinionSandboxTests(unittest.TestCase):
    def test_sandboxed_broker_requires_assignment_gateway_token(self) -> None:
        runtime = MinionBrokerLLMRuntime(
            Path("/tmp/pal-minion-broker-token"),
            run_id="run-token",
        )
        with patch.dict(
            os.environ,
            {
                "PAL_MINION_SANDBOXED": "1",
                "PAL_MINION_ASSIGNMENT_TOKEN": "",
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "assignment-scoped"):
                _ = runtime._client

    def test_minion_temperature_accepts_low_deterministic_profile_value(self) -> None:
        pack = MinionInvocationPack(invocation_id="temperature", goal="g", metadata={"temperature": 0.05})
        self.assertEqual(_minion_temperature(pack, fallback=0.7), 0.05)
        invalid = MinionInvocationPack(invocation_id="temperature-invalid", goal="g", metadata={"temperature": 3})
        self.assertEqual(_minion_temperature(invalid, fallback=0.7), 0.7)

    def test_sandbox_metadata_defaults_to_available_backend_or_unavailable_marker(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_meta_") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            pack = MinionInvocationPack(invocation_id="wo", goal="g", workspace={"repo_path": str(repo)})

            with patch.dict(os.environ, {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(root / "tmp_scratch")}):
                updated = with_minion_sandbox_metadata(root, pack, run_id="run_1")

            sandbox = updated.metadata["sandbox"]
            self.assertIn("enabled", sandbox)
            if sandbox["backend"] != "unavailable":
                self.assertTrue(sandbox["enabled"])
                self.assertEqual(sandbox["backend"], "bwrap")
                self.assertEqual(sandbox["workspace_path"], str(repo))
                self.assertEqual(sandbox["secret_policy"], "host_llm_broker")
                self.assertEqual(sandbox["scratch_dir"], str(root / "tmp_scratch" / "run_1"))
                self.assertIn("sudo", sandbox["blacklist_commands"])
                self.assertIn("rm", sandbox["blacklist_commands"])
                self.assertIn("unlink", sandbox["blacklist_commands"])
                self.assertIn("rmdir", sandbox["blacklist_commands"])
            else:
                self.assertTrue(sandbox["enabled"])
                self.assertEqual(sandbox["backend"], "unavailable")

    def test_sandbox_metadata_rejects_unwired_backend(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_backend_") as tmp:
            pack = MinionInvocationPack(
                invocation_id="wo",
                goal="g",
                metadata={"sandbox": {"backend": "docker"}},
            )

            updated = with_minion_sandbox_metadata(Path(tmp), pack, run_id="run_backend")

            sandbox = updated.metadata["sandbox"]
            self.assertTrue(sandbox["enabled"])
            self.assertEqual(sandbox["backend"], "unavailable")
            self.assertIn("unsupported", sandbox["reason"])

    def test_git_worktree_metadata_bind_paths_resolve_common_git_dir(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_gitmeta_") as tmp:
            root = Path(tmp)
            common_dir = root / "source" / ".git"
            git_dir = common_dir / "worktrees" / "repo"
            workspace = root / "repo"
            git_dir.mkdir(parents=True)
            workspace.mkdir()
            (workspace / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
            (git_dir / "commondir").write_text("../..\n", encoding="utf-8")

            bind_paths = _git_worktree_metadata_bind_paths(workspace)

            self.assertEqual(bind_paths, (common_dir.resolve(),))

    def test_git_metadata_bind_paths_include_shared_clone_object_store(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_shared_meta_") as tmp:
            root = Path(tmp)
            source = root / "source"
            workspace = root / "workspace"
            source.mkdir()
            _git(source, "init")
            _git(source, "config", "user.email", "pal-test@example.invalid")
            _git(source, "config", "user.name", "Pal Test")
            (source / "README.md").write_text("source\n", encoding="utf-8")
            _git(source, "add", "README.md")
            _git(source, "commit", "-m", "initial")
            _git(root, "clone", "--shared", str(source), str(workspace))

            bind_paths = _git_worktree_metadata_bind_paths(workspace)

            self.assertEqual(bind_paths, ((source / ".git" / "objects").resolve(),))

    def test_sandbox_env_scrubs_secret_like_values_and_enables_broker(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_env_") as tmp:
            with patch.dict(os.environ, {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(Path(tmp) / "tmp_scratch")}):
                env = scrub_minion_sandbox_env(
                    {
                        "PATH": "/usr/bin",
                        "OPENAI_API_KEY": "secret",
                        "NORMAL_VALUE": "kept",
                        "PAL_TOKEN": "secret",
                        "PAL_MINION_ASSIGNMENT_TOKEN": "assignment-only",
                    },
                    runtime_root=Path(tmp),
                    run_id="run_env",
                )

            self.assertEqual(env["PATH"], "/usr/bin")
            self.assertEqual(env["NORMAL_VALUE"], "kept")
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("PAL_TOKEN", env)
            self.assertEqual(env["PAL_MINION_ASSIGNMENT_TOKEN"], "assignment-only")
            self.assertEqual(env["PAL_MINION_LLM_BROKER"], "1")
            self.assertEqual(env["PAL_DATABASE_READ_ONLY"], "1")
            self.assertEqual(env["PAL_MINION_SANDBOXED"], "1")
            self.assertEqual(env["TMPDIR"], str(Path(tmp) / "tmp_scratch" / "run_env" / "tmp"))

    def test_sandbox_env_applies_workspace_execution_env(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_workspace_env_") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            src = repo / "src"
            src.mkdir(parents=True)
            pack = MinionInvocationPack(
                invocation_id="wo",
                goal="g",
                workspace={
                    "repo_path": str(repo),
                    "execution_env": {
                        "vars": {
                            "CMAKE_EXPORT_COMPILE_COMMANDS": "ON",
                            "PAL_TOKEN": "secret",
                        },
                        "path_prepend": {"PYTHONPATH": [str(src)]},
                    },
                },
            )

            with patch.dict(os.environ, {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(root / "tmp_scratch")}):
                env = scrub_minion_sandbox_env(
                    {"PATH": "/usr/bin", "PYTHONPATH": "/existing"},
                    runtime_root=root,
                    run_id="run_workspace_env",
                    pack=pack,
                )

            self.assertEqual(env["PYTHONPATH"].split(os.pathsep)[0], str(src))
            self.assertIn("/existing", env["PYTHONPATH"].split(os.pathsep))
            self.assertEqual(env["CMAKE_EXPORT_COMPILE_COMMANDS"], "ON")
            self.assertNotIn("PAL_TOKEN", env)
            self.assertIn("PYTHONUSERBASE", env)

    def test_blacklist_wrappers_are_generated_as_executable_route_blocks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_wrappers_") as tmp:
            with patch.dict(os.environ, {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(Path(tmp) / "tmp_scratch")}):
                scratch, deny_dir = ensure_sandbox_files(Path(tmp), run_id="run_wrap", blacklist_commands=("sudo", "docker"))

            self.assertTrue((scratch / "tmp").is_dir())
            self.assertEqual(scratch, Path(tmp) / "tmp_scratch" / "run_wrap")
            sudo = deny_dir / "sudo"
            docker = deny_dir / "docker"
            self.assertTrue(os.access(sudo, os.X_OK))
            self.assertTrue(os.access(docker, os.X_OK))
            sudo_text = sudo.read_text(encoding="utf-8")
            self.assertIn("blocked command 'sudo'", sudo_text)
            self.assertIn("Use Pal resident capabilities when available", sudo_text)
            self.assertIn("read_file for reading repo text files", sudo_text)
            self.assertIn("delete_path for deleting repo paths", sudo_text)
            self.assertIn("Keep run_shell for tests", sudo_text)

    def test_runner_invocation_uses_broker_env_when_sandboxed(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_invocation_") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            pack = MinionInvocationPack(
                invocation_id="wo",
                goal="g",
                workspace={"repo_path": str(repo)},
                metadata={"sandbox": {"enabled": True, "backend": "bwrap", "run_id": "run_inv"}},
            )

            with patch.dict(os.environ, {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(root / "tmp_scratch")}):
                argv, env = build_sandboxed_runner_invocation(
                    runtime_root=root,
                    pack=pack,
                    argv=["python", "-m", "pal.minion.v2.worker_main"],
                    env={"PATH": "/usr/bin", "OPENAI_API_KEY": "secret"},
                )

            self.assertTrue(argv[0].endswith("bwrap"))
            self.assertIn("--share-net", argv)
            self.assertIn("--chdir", argv)
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertEqual(env["PAL_MINION_LLM_BROKER"], "1")
            self.assertIn("PYTHONPATH", env)

    def test_reference_projection_uses_stable_read_only_sandbox_path(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_reference_") as tmp:
            root = Path(tmp)
            runtime_root = root / "runtime"
            repo = root / "repo"
            reference = root / "reference"
            repo.mkdir()
            reference.mkdir()
            (reference / "TASK.md").write_text("framepipe\n", encoding="utf-8")
            (reference / "manifest.json").write_text('{"version":1}\n', encoding="utf-8")
            (reference / "private.txt").write_text("not projected\n", encoding="utf-8")
            pack = MinionInvocationPack(
                invocation_id="reference_projection",
                goal="inspect task",
                workspace={
                    "repo_path": str(repo),
                    "reference_paths": [
                        {
                            "name": "task",
                            "path": str(reference / "*.md"),
                            "truth_source": True,
                            "required": True,
                        },
                        {
                            "name": "architecture_index",
                            "path": str(reference / "manifest.json"),
                            "truth_source": True,
                            "required": True,
                        },
                    ],
                },
            )
            with patch.dict(
                os.environ,
                {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(root / "scratch")},
            ):
                pack = with_minion_sandbox_metadata(
                    runtime_root,
                    pack,
                    run_id="reference_projection",
                )
                argv, env = build_sandboxed_runner_invocation(
                    runtime_root=runtime_root,
                    pack=pack,
                    argv=[
                        "/bin/sh",
                        "-c",
                        "tree -a -L 3 --filelimit 200 --noreport /pal/references/task; "
                        "test -r /pal/references/task/TASK.md; "
                        "test -f /pal/references/architecture_index/manifest.json; "
                        "test ! -e /pal/references/task/private.txt; "
                        "if printf changed > /pal/references/task/TASK.md 2>/dev/null; then exit 31; fi; "
                        "if printf new > /pal/references/task/new.txt 2>/dev/null; then exit 32; fi",
                    ],
                    env={"PATH": "/usr/bin:/bin"},
                )

            projected = dict(pack.workspace["reference_paths"][0])
            self.assertEqual(projected["path"], "/pal/references/task")
            self.assertEqual(
                pack.workspace["reference_paths"][1]["path"],
                "/pal/references/architecture_index/manifest.json",
            )
            rebound = with_minion_sandbox_metadata(
                runtime_root,
                pack,
                run_id="reference_projection",
            )
            self.assertEqual(
                rebound.workspace["reference_paths"][0]["path"],
                "/pal/references/task",
            )
            self.assertEqual(
                rebound.metadata["sandbox"]["reference_binds"][0]["source_path"],
                str(reference),
            )
            prompt = render_minion_task_prompt(pack)
            self.assertIn("reference:task: read-only semantic input", prompt)
            self.assertIn("reference_name='task' plus a root-relative path", prompt)
            self.assertIn("sandbox_path=/pal/references/task", prompt)
            self.assertIn(
                "tree -a -L 3 --filelimit 200 --noreport /pal/references/task",
                prompt,
            )

            result = subprocess.run(
                argv,
                env=env,
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("TASK.md", result.stdout)
            self.assertNotIn("private.txt", result.stdout)
            self.assertEqual((reference / "TASK.md").read_text(encoding="utf-8"), "framepipe\n")
            self.assertFalse((reference / "new.txt").exists())

    def test_read_only_workspace_is_mounted_read_only(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_read_only_") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            pack = MinionInvocationPack(
                invocation_id="wo_read_only",
                goal="review",
                workspace={
                    "repo_path": str(repo),
                    "workspace_policy": {"mode": "read_only_repo"},
                },
                metadata={"sandbox": {"enabled": True, "backend": "bwrap", "run_id": "run_read_only"}},
            )

            argv, _env = build_sandboxed_runner_invocation(
                runtime_root=root,
                pack=pack,
                argv=["python", "-c", "pass"],
                env={"PATH": "/usr/bin:/bin"},
            )

            self.assertTrue(
                any(
                    argv[index : index + 3] == ["--ro-bind", str(repo), str(repo)]
                    for index in range(max(0, len(argv) - 2))
                )
            )

    def test_worker_sees_read_only_pal_db_and_only_its_minion_runtime_slice(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_runtime_scope_") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            run_dir = root / "data" / "minion" / "runtime" / "invocations" / "attempt-1"
            run_dir.mkdir(parents=True)
            minion_db = root / "data" / "minion" / "minion.sqlite3"
            minion_db.write_text("private", encoding="utf-8")
            pal_db = root / "pal.sqlite3"
            pal_db.write_text("memory", encoding="utf-8")
            role_socket = root / "data" / "minion-role" / "role.sock"
            role_socket.parent.mkdir(parents=True)
            role_socket.write_text("endpoint", encoding="utf-8")
            pack = MinionInvocationPack(
                invocation_id="attempt-1",
                goal="work",
                workspace={"repo_path": str(repo), "run_dir": str(run_dir)},
                metadata={
                    "sandbox": {
                        "enabled": True,
                        "backend": "bwrap",
                        "run_id": "runtime-scope",
                    }
                },
            )

            argv, _env = build_sandboxed_runner_invocation(
                runtime_root=root,
                pack=pack,
                argv=["python", "-c", "pass"],
                env={"PATH": "/usr/bin:/bin", "PAL_MINION_ASSIGNMENT_TOKEN": "token"},
            )

            triples = [argv[index : index + 3] for index in range(max(0, len(argv) - 2))]
            self.assertIn(["--ro-bind", str(pal_db), str(pal_db)], triples)
            self.assertIn(["--ro-bind", str(role_socket), str(role_socket)], triples)
            self.assertIn(["--bind", str(run_dir), str(run_dir)], triples)
            self.assertNotIn(
                ["--bind", str(root / "data" / "minion"), str(root / "data" / "minion")],
                triples,
            )
            self.assertNotIn(str(minion_db), argv)

    def test_scoped_writable_workspace_enforces_paths_for_shell_processes(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_scoped_") as tmp:
            root = Path(tmp)
            runtime_root = root / "runtime"
            repo = root / "repo"
            (repo / "contracts").mkdir(parents=True)
            (repo / "src" / "private").mkdir(parents=True)
            (repo / "tests").mkdir()
            (repo / "contracts" / "router.py").write_text("contract\n", encoding="utf-8")
            (repo / "src" / "router.py").write_text("impl\n", encoding="utf-8")
            (repo / "src" / "sibling.py").write_text("sibling\n", encoding="utf-8")
            (repo / "src" / "private" / "seed.py").write_text("seed\n", encoding="utf-8")
            (repo / "tests" / "test_router.py").write_text("test\n", encoding="utf-8")
            _git(repo, "init")
            _git(repo, "config", "user.email", "pal-test@example.invalid")
            _git(repo, "config", "user.name", "Pal Test")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-m", "initial")
            pack = MinionInvocationPack(
                invocation_id="scoped",
                goal="implement router",
                workspace={
                    "repo_path": str(repo),
                    "require_os_path_enforcement": True,
                    "write_path_scopes": [
                        {"kind": "file", "path": "src/router.py"},
                        {"kind": "directory", "path": "src/private"},
                        {"kind": "file", "path": "tests/test_router.py"},
                    ],
                    "workspace_policy": {"mode": "writable_git_branch"},
                },
            )
            with patch.dict(os.environ, {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(root / "scratch")}):
                pack = with_minion_sandbox_metadata(runtime_root, pack, run_id="run_scoped")
                script = """
printf changed > src/router.py
printf new > src/private/new.py
printf changed-test > tests/test_router.py
if printf bad > contracts/router.py 2>/dev/null; then exit 21; fi
if printf bad > src/sibling.py 2>/dev/null; then exit 22; fi
if printf bad > .git/config 2>/dev/null; then exit 23; fi
"""
                argv, env = build_sandboxed_runner_invocation(
                    runtime_root=runtime_root,
                    pack=pack,
                    argv=["/bin/sh", "-c", script],
                    env={"PATH": "/usr/bin:/bin"},
                )
            result = subprocess.run(argv, env=env, cwd=repo, capture_output=True, text=True, timeout=20)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((repo / "src" / "router.py").read_text(), "changed")
            self.assertEqual((repo / "src" / "private" / "new.py").read_text(), "new")
            self.assertEqual((repo / "tests" / "test_router.py").read_text(), "changed-test")
            self.assertEqual((repo / "contracts" / "router.py").read_text(), "contract\n")
            self.assertEqual((repo / "src" / "sibling.py").read_text(), "sibling\n")

    def test_verifier_regression_overlay_is_read_only_inside_sandbox(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_overlay_") as tmp:
            root = Path(tmp)
            runtime_root = root / "runtime"
            repo = root / "repo"
            (repo / "src").mkdir(parents=True)
            (repo / "tests").mkdir()
            (repo / "src" / "router.py").write_text("old\n", encoding="utf-8")
            (repo / "tests" / "test_router.py").write_text(
                "def test_router():\n    assert False\n",
                encoding="utf-8",
            )
            _git(repo, "init")
            _git(repo, "config", "user.email", "pal-test@example.invalid")
            _git(repo, "config", "user.name", "Pal Test")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-m", "initial")
            pack = MinionInvocationPack(
                invocation_id="repair-overlay",
                goal="repair router without changing verifier tests",
                workspace={
                    "repo_path": str(repo),
                    "require_os_path_enforcement": True,
                    "write_path_scopes": [
                        {"kind": "directory", "path": "src"},
                        {"kind": "directory", "path": "tests"},
                    ],
                    "read_only_overlay_paths": ["tests/test_router.py"],
                    "workspace_policy": {"mode": "writable_git_branch"},
                },
            )
            with patch.dict(
                os.environ,
                {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(root / "scratch")},
            ):
                pack = with_minion_sandbox_metadata(
                    runtime_root,
                    pack,
                    run_id="run_overlay",
                )
                script = """
printf fixed > src/router.py
if printf pass > tests/test_router.py 2>/dev/null; then exit 41; fi
"""
                argv, env = build_sandboxed_runner_invocation(
                    runtime_root=runtime_root,
                    pack=pack,
                    argv=["/bin/sh", "-c", script],
                    env={"PATH": "/usr/bin:/bin"},
                )

            result = subprocess.run(
                argv,
                env=env,
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((repo / "src" / "router.py").read_text(), "fixed")
            self.assertIn("assert False", (repo / "tests" / "test_router.py").read_text())

    def test_scoped_writable_workspace_fails_closed_without_sandbox(self) -> None:
        pack = MinionInvocationPack(
            invocation_id="scoped-disabled",
            goal="implement",
            workspace={
                "repo_path": "/tmp",
                "require_os_path_enforcement": True,
                "write_path_scopes": [{"kind": "directory", "path": "owned"}],
            },
        )
        with patch.dict(os.environ, {"PAL_MINION_SANDBOX": "0"}):
            with self.assertRaisesRegex(RuntimeError, "require an OS sandbox"):
                with_minion_sandbox_metadata(Path("/tmp"), pack, run_id="disabled")

    def test_sandbox_scratch_prefers_temp_root_and_falls_back_when_unusable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_scratch_") as tmp:
            root = Path(tmp)
            temp_root = root / "tmp_scratch"
            with patch.dict(os.environ, {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(temp_root)}):
                self.assertEqual(minion_sandbox_scratch_dir(root, "run_a"), temp_root / "run_a")

            unusable = root / "not_a_dir"
            unusable.write_text("file", encoding="utf-8")
            with patch.dict(os.environ, {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(unusable)}):
                self.assertEqual(
                    minion_sandbox_scratch_dir(root, "run_b"),
                    root / "data" / "minion" / "sandbox" / "runs" / "run_b",
                )

            with patch.dict(
                os.environ,
                {
                    "PAL_MINION_SANDBOX_SCRATCH_ROOT": str(temp_root),
                    "PAL_MINION_SANDBOX_MIN_FREE_MB": "999999999",
                },
            ):
                self.assertEqual(
                    minion_sandbox_scratch_dir(root, "run_c"),
                    root / "data" / "minion" / "sandbox" / "runs" / "run_c",
                )

    def test_sandbox_run_dir_gc_keeps_recent_limited_scratch_dirs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_gc_") as tmp:
            root = Path(tmp)
            temp_root = root / "tmp_scratch"
            env = {
                "PAL_MINION_SANDBOX_SCRATCH_ROOT": str(temp_root),
                "PAL_MINION_SANDBOX_MAX_RUN_DIRS": "2",
            }
            with patch.dict(os.environ, env):
                first, _ = ensure_sandbox_files(root, run_id="run_1", blacklist_commands=())
                second, _ = ensure_sandbox_files(root, run_id="run_2", blacklist_commands=())
                third, _ = ensure_sandbox_files(root, run_id="run_3", blacklist_commands=())

            self.assertFalse(first.exists())
            self.assertTrue(second.exists())
            self.assertTrue(third.exists())

    def test_sandboxed_git_worktree_can_resolve_external_git_dir(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_git_worktree_") as tmp:
            root = Path(tmp)
            runtime_root = root / "runtime"
            source = root / "source"
            workspace = root / "workspace"
            source.mkdir()
            _git(source, "init")
            _git(source, "checkout", "-B", "main")
            _git(source, "config", "user.email", "pal-test@example.invalid")
            _git(source, "config", "user.name", "Pal Test")
            (source / "README.md").write_text("# source\n", encoding="utf-8")
            _git(source, "add", "README.md")
            _git(source, "commit", "-m", "initial")
            _git(source, "worktree", "add", "-B", "work", str(workspace), "main")
            with patch.dict(os.environ, {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(root / "tmp_scratch")}):
                pack = with_minion_sandbox_metadata(
                    runtime_root,
                    MinionInvocationPack(invocation_id="wo", goal="g", workspace={"repo_path": str(workspace)}),
                    run_id="run_git_worktree",
                )

                argv, env = build_sandboxed_runner_invocation(
                    runtime_root=runtime_root,
                    pack=pack,
                    argv=["git", "status", "--porcelain"],
                    env={"PATH": "/usr/bin:/bin"},
                )
            result = subprocess.run(argv, env=env, cwd=str(workspace), capture_output=True, text=True, timeout=20)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")

    def test_sandboxed_shared_clone_can_resolve_alternate_object_store(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_shared_clone_") as tmp:
            root = Path(tmp)
            runtime_root = root / "runtime"
            source = root / "source"
            workspace = root / "workspace"
            source.mkdir()
            _git(source, "init")
            _git(source, "config", "user.email", "pal-test@example.invalid")
            _git(source, "config", "user.name", "Pal Test")
            (source / "README.md").write_text("source\n", encoding="utf-8")
            _git(source, "add", "README.md")
            _git(source, "commit", "-m", "initial")
            _git(root, "clone", "--shared", str(source), str(workspace))
            (workspace / "README.md").write_text("changed\n", encoding="utf-8")

            with patch.dict(os.environ, {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(root / "tmp_scratch")}):
                pack = with_minion_sandbox_metadata(
                    runtime_root,
                    MinionInvocationPack(
                        invocation_id="shared-clone",
                        goal="inspect shared clone",
                        workspace={
                            "repo_path": str(workspace),
                            "require_os_path_enforcement": True,
                            "write_path_scopes": [{"kind": "file", "path": "README.md"}],
                        },
                    ),
                    run_id="run_shared_clone",
                )
                argv, env = build_sandboxed_runner_invocation(
                    runtime_root=runtime_root,
                    pack=pack,
                    argv=["git", "diff", "--name-only", "HEAD", "--"],
                    env={"PATH": "/usr/bin:/bin"},
                )

            result = subprocess.run(argv, env=env, cwd=str(workspace), capture_output=True, text=True, timeout=20)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "README.md")

    def test_sandboxed_python_can_import_runtime_dependencies(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_import_") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            pack = MinionInvocationPack(
                invocation_id="wo",
                goal="g",
                workspace={"repo_path": str(repo)},
                metadata={"sandbox": {"enabled": True, "backend": "bwrap", "run_id": "run_import"}},
            )

            with patch.dict(os.environ, {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(root / "tmp_scratch")}):
                argv, env = build_sandboxed_runner_invocation(
                    runtime_root=root,
                    pack=pack,
                    argv=["python", "-c", "import msgpack; import pal.foundation.sidecar; print('imports-ok')"],
                    env={"PATH": "/usr/bin:/bin"},
                )
            result = subprocess.run(argv, env=env, cwd=str(repo), capture_output=True, text=True, timeout=20)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("imports-ok", result.stdout)

    def test_sandboxed_runner_honors_explicit_approval_policy(self) -> None:
        async def scenario() -> None:
            calls: list[str] = []
            events: list[dict] = []
            pack = MinionInvocationPack(
                invocation_id="wo_sandbox_shell",
                goal="sandbox shell",
                allowed_capabilities=["op_exec_shell", "op_file_read"],
                approval_policy={"high_risk_capabilities": ["op_exec_shell"]},
                metadata={"sandbox": {"enabled": True, "backend": "bwrap"}},
            )

            class FakeExecution:
                async def execute_tool_async(self, call, **kwargs):
                    _ = kwargs
                    calls.append(str(call.args.get("cmd") or ""))
                    return CanonicalToolResult(
                        name=call.name,
                        ok=True,
                        text="shell ok",
                        llm_text="shell ok",
                        structured={"exit_code": 0},
                        status=RuntimeStatus.OK,
                    )

            async def write_event(event):
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return {"decision": {"decision": "accept"}}

            runner = MinionRunner(
                runtime_root=Path(tempfile.mkdtemp(prefix="pal_minion_sandbox_runner_")),
                pack=pack,
                minion_id="m_sandbox_shell",
                run_id="r_sandbox_shell",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=SimpleNamespace(), execution_runtime=SimpleNamespace()),
            )
            result = await runner._execute_allowed_tool(
                FakeExecution(),
                CanonicalToolCall(name="op_exec_shell", args={"cmd": "cat README.md"}, call_id="call_shell"),
            )

            self.assertTrue(result.ok, result.text)
            self.assertEqual(calls, ["cat README.md"])
            self.assertEqual(len([event for event in events if event["event_kind"] == "approval_requested"]), 1)

        asyncio.run(scenario())

    def test_runner_preserves_provider_alias_after_policy_admission(self) -> None:
        async def scenario() -> None:
            calls: list[str] = []
            pack = MinionInvocationPack(
                invocation_id="provider_alias",
                goal="inspect task sources",
                allowed_capabilities=["op_search"],
            )

            class AliasExecution:
                def resolve_llm_tool_name(self, name):
                    return {"search": "op_search"}.get(str(name), str(name))

                async def execute_tool_async(self, call, **kwargs):
                    _ = kwargs
                    calls.append(call.name)
                    return CanonicalToolResult(
                        name=call.name,
                        ok=True,
                        text="search ok",
                        llm_text="search ok",
                        structured={"matches": []},
                        status=RuntimeStatus.OK,
                    )

            async def write_event(_event):
                return None

            async def read_decision(_timeout):
                return None

            runner = MinionRunner(
                runtime_root=Path(tempfile.mkdtemp(prefix="pal_minion_provider_alias_")),
                pack=pack,
                minion_id="m_provider_alias",
                run_id="r_provider_alias",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=SimpleNamespace(), execution_runtime=SimpleNamespace()),
            )

            result = await runner._execute_allowed_tool(
                AliasExecution(),
                CanonicalToolCall(
                    name="search",
                    args={"query": "frame", "reference_name": "task"},
                    call_id="call_search",
                ),
            )

            self.assertTrue(result.ok, result.text)
            self.assertEqual(calls, ["search"])

        asyncio.run(scenario())

    def test_runner_binds_lsp_calls_to_isolated_workspace(self) -> None:
        workspace = Path(tempfile.mkdtemp(prefix="pal_minion_lsp_workspace_"))
        runner = MinionRunner(
            runtime_root=workspace.parent,
            pack=MinionInvocationPack(
                invocation_id="inv_lsp",
                goal="inspect code",
                workspace={
                    "repo_path": str(workspace),
                    "primary_language": "cpp",
                    "languages": ["c", "cpp"],
                },
            ),
            minion_id="m_lsp",
            run_id="r_lsp",
            write_event=lambda _event: None,  # type: ignore[arg-type]
            read_decision=lambda _timeout: None,  # type: ignore[arg-type]
        )

        direct = runner._tool_call_with_minion_defaults(
            CanonicalToolCall(name="op_lsp_definition", args={"file": "src/main.cpp", "line": 1, "character": 2})
        )
        nested = runner._tool_call_with_minion_defaults(
            CanonicalToolCall(
                name="op_tool_call",
                args={
                    "name": "op_lsp_diagnostics",
                    "args": {"file": "src/main.cpp"},
                },
            )
        )

        self.assertEqual(direct.args["workspace_root"], str(workspace))
        self.assertNotIn("primary_language", direct.args)
        self.assertNotIn("lsp_setup", direct.args)
        nested_args = dict(nested.args["args"])
        self.assertEqual(nested_args["workspace_root"], str(workspace))
        self.assertNotIn("languages", nested_args)
        self.assertNotIn("lsp_setup", nested_args)


class MinionLLMBrokerSerializationTests(unittest.TestCase):
    def test_llm_request_round_trips(self) -> None:
        request = CanonicalLLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            max_output_tokens=123,
            model_hint="model",
            temperature=0.2,
            tools=[{"type": "function", "function": {"name": "tool"}}],
            metadata={"run_id": "r"},
        )

        restored = llm_request_from_payload(llm_request_to_payload(request))

        self.assertEqual(restored.messages, request.messages)
        self.assertEqual(restored.max_output_tokens, 123)
        self.assertEqual(restored.model_hint, "model")
        self.assertEqual(restored.temperature, 0.2)
        self.assertEqual(restored.tools, request.tools)
        self.assertEqual(restored.metadata, request.metadata)

    def test_llm_outcome_round_trips_tool_calls_and_provider_fields(self) -> None:
        outcome = CanonicalLLMOutcome(
            text="ok",
            reasoning_text="hidden",
            tool_calls=[CanonicalToolCall(name="op_exec_shell", args={"cmd": "pwd"}, call_id="call_1")],
            finish_reason="tool_calls",
            provider_specific_fields={"reasoning_content": "hidden"},
        )

        restored = llm_outcome_from_payload(llm_outcome_to_payload(outcome))

        self.assertEqual(restored.text, "ok")
        self.assertEqual(restored.reasoning_text, "hidden")
        self.assertEqual(restored.finish_reason, "tool_calls")
        self.assertEqual(restored.tool_calls[0].name, "op_exec_shell")
        self.assertEqual(restored.tool_calls[0].args, {"cmd": "pwd"})
        self.assertEqual(restored.provider_specific_fields["reasoning_content"], "hidden")

    def test_preflight_round_trips(self) -> None:
        request = LLMPreflightRequest(
            messages=[{"role": "user", "content": "hi"}],
            max_output_tokens=50,
            model_hint="m",
            tools=[{"name": "tool"}],
            metadata={"preferred_endpoint_id": "e"},
        )
        advice = LLMPreflightAdvice(
            status="ready",
            active_model="m",
            fallback_chain=["f"],
            target_input_budget=10,
            reserved_output_tokens=5,
            breakdown={"ok": True},
        )

        restored_request = preflight_request_from_payload(preflight_request_to_payload(request))
        restored_advice = preflight_advice_from_payload(preflight_advice_to_payload(advice))

        self.assertEqual(restored_request.messages, request.messages)
        self.assertEqual(restored_request.tools, request.tools)
        self.assertEqual(restored_advice.status, "ready")
        self.assertEqual(restored_advice.active_model, "m")
        self.assertEqual(restored_advice.fallback_chain, ["f"])

    def test_manager_llm_broker_calls_host_runtime(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory(prefix="pal_minion_broker_manager_") as tmp:
                manager = MinionManager(runtime_root=Path(tmp))
                pack = MinionInvocationPack(invocation_id="wo_broker", goal="g")
                manager.runs["run_broker"] = MinionRunState(minion_id="m", run_id="run_broker", pack=pack, status="running")

                class FakeRuntime:
                    async def apreflight(self, request):
                        self.preflight_request = request
                        return LLMPreflightAdvice(status="ready", active_model="fake")

                    async def agenerate(self, request):
                        self.generate_request = request
                        return CanonicalLLMOutcome(text="pong", finish_reason="stop")

                    def resolve_max_output_tokens(self, **kwargs):
                        self.max_kwargs = kwargs
                        return 123

                    def resolve_endpoint_facts(self, **kwargs):
                        self.facts_kwargs = kwargs
                        return {"endpoint_id": kwargs.get("preferred_endpoint_id"), "model_id": "fake"}

                fake = FakeRuntime()

                async def fake_runtime():
                    return fake

                manager._llm_broker_runtime = fake_runtime  # type: ignore[method-assign]
                preflight = await manager.llm_broker_preflight(
                    {
                        "run_id": "run_broker",
                        "request": preflight_request_to_payload(
                            LLMPreflightRequest(messages=[{"role": "user", "content": "ping"}], max_output_tokens=10)
                        ),
                    }
                )
                generated = await manager.llm_broker_generate(
                    {
                        "run_id": "run_broker",
                        "request": llm_request_to_payload(
                            CanonicalLLMRequest(messages=[{"role": "user", "content": "ping"}], max_output_tokens=10)
                        ),
                    }
                )
                max_tokens = await manager.llm_broker_resolve_max_output_tokens(
                    {"run_id": "run_broker", "preferred_endpoint_id": "endpoint_a"}
                )
                facts = await manager.llm_broker_resolve_endpoint_facts({"run_id": "run_broker", "preferred_endpoint_id": "endpoint_a"})

                self.assertEqual(preflight["advice"]["active_model"], "fake")
                self.assertEqual(generated["outcome"]["text"], "pong")
                self.assertEqual(max_tokens["max_output_tokens"], 123)
                self.assertEqual(facts["endpoint_id"], "endpoint_a")

        asyncio.run(scenario())

    def test_manager_llm_broker_records_endpoint_progress_from_host_runtime(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory(prefix="pal_minion_broker_events_") as tmp:
                manager = MinionManager(runtime_root=Path(tmp))
                pack = MinionInvocationPack(invocation_id="wo_broker_events", goal="g")
                state = MinionRunState(minion_id="m", run_id="run_broker_events", pack=pack, status="running")
                manager.runs[state.run_id] = state
                recorded: list[dict[str, object]] = []
                manager.events.queue_event = lambda event: recorded.append(dict(event))  # type: ignore[method-assign]

                class Settings:
                    def get_think_level(self):
                        return "balanced"

                    def get_active_llm_endpoint_id(self):
                        return None

                    def set_active_llm_endpoint_id(self, endpoint_id):
                        self.active_endpoint_id = endpoint_id

                class Invoker:
                    def invoke(self, endpoint, request):
                        _ = request
                        if endpoint.endpoint_id == "broken":
                            raise RuntimeError("broken endpoint")
                        return CanonicalLLMOutcome(text=f"ok:{endpoint.endpoint_id}")

                    def invoke_stream(self, endpoint, request):
                        raise NotImplementedError

                broken = SimpleNamespace(
                    endpoint_id="broken",
                    model_id="broken-model",
                    provider="stub",
                    base_url="",
                    capabilities_blob={},
                    supports_streaming=False,
                    supports_vision=False,
                    max_output_tokens=1024,
                    context_window=8192,
                    input_modalities_blob=[],
                )
                working = SimpleNamespace(
                    endpoint_id="working",
                    model_id="working-model",
                    provider="stub",
                    base_url="",
                    capabilities_blob={},
                    supports_streaming=False,
                    supports_vision=False,
                    max_output_tokens=1024,
                    context_window=8192,
                    input_modalities_blob=[],
                )
                runtime = LLMRuntime(
                    endpoint_resolver=EndpointResolver(endpoints=(broken, working)),
                    settings_repository=Settings(),
                    endpoint_invoker=Invoker(),
                    endpoint_retry_attempts=1,
                )

                async def fake_runtime():
                    return runtime

                manager._llm_broker_runtime = fake_runtime  # type: ignore[method-assign]
                generated = await manager.llm_broker_generate(
                    {
                        "run_id": state.run_id,
                        "request": llm_request_to_payload(
                            CanonicalLLMRequest(messages=[{"role": "user", "content": "ping"}], max_output_tokens=10)
                        ),
                    }
                )
                await asyncio.sleep(0)

                self.assertEqual(generated["outcome"]["text"], "ok:working")
                endpoint_events = [
                    event
                    for event in recorded
                    if event.get("event_kind") == "progress"
                    and str(event["payload"].get("phase") or "").startswith("llm_endpoint_")
                ]
                phases = [event["payload"]["phase"] for event in endpoint_events]
                self.assertIn("llm_endpoint_attempt_failed", phases)
                self.assertIn("llm_endpoint_exhausted", phases)
                self.assertIn("llm_endpoint_fallback_succeeded", phases)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
