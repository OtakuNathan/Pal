"""Read-only mounts for the actual relocated Bunshin Python interpreter."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pal.bunshin import sandbox
from pal.bunshin.harnesses import pal_harness_spec
from pal.shared import BunshinInvocationPack


class BunshinPythonMountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="pal_python_mounts_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prefix = self.root / "relocated" / "python"
        self.library = self.prefix / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
        (self.library / "encodings").mkdir(parents=True)
        (self.library / "encodings" / "__init__.py").write_text("# fixture\n")
        (self.library / "site-packages").mkdir()
        self.executable = self.prefix / "bin" / "python-real"
        self.executable.parent.mkdir()
        self.executable.write_text("#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o755)
        self.alias = self.executable.with_name("python")
        self.alias.symlink_to(self.executable.name)
        self.paths = {"stdlib": str(self.library), "platstdlib": str(self.library),
                      "purelib": str(self.library / "site-packages"),
                      "platlib": str(self.library / "site-packages")}

    def runtime_paths(self) -> tuple[Path, ...]:
        with (patch.object(sandbox.sys, "executable", str(self.alias)),
              patch.object(sandbox.sysconfig, "get_paths", return_value=self.paths)):
            return sandbox._python_runtime_paths()

    def test_relocated_interpreter_uses_only_resolved_binary_and_stdlib(self) -> None:
        self.assertEqual(self.runtime_paths(), (self.library, self.executable))
        self.assertNotIn(self.prefix, self.runtime_paths())
        self.assertNotIn(self.executable.parent, self.runtime_paths())
        self.assertNotIn(self.alias, self.runtime_paths())

    def test_base_interpreter_paths_exclude_virtualenv_prefix_and_stale_build_paths(self) -> None:
        with (
            patch.object(sandbox.sys, "executable", str(self.alias)),
            patch.object(sandbox.sys, "prefix", str(self.root / "virtualenv")),
            patch.object(sandbox.sys, "base_prefix", str(self.prefix)),
            patch.object(sandbox.sys, "base_exec_prefix", str(self.prefix)),
            patch.object(sandbox.sysconfig, "get_paths", return_value=self.paths) as paths,
            patch.object(sandbox.sysconfig, "get_config_var", side_effect=AssertionError("do not use build paths")),
        ):
            self.assertEqual(sandbox._python_runtime_paths(), (self.library, self.executable))
            paths.assert_called_once_with(vars={"base": str(self.prefix), "platbase": str(self.prefix)})

    def test_distinct_existing_platform_library_is_narrow_and_deduplicated(self) -> None:
        platform_library = self.prefix / "lib64" / self.library.name
        platform_library.mkdir(parents=True)
        self.paths["platstdlib"] = str(platform_library)
        self.assertEqual(self.runtime_paths(), (self.library, platform_library, self.executable))
        alias = self.prefix / "library-alias"
        alias.symlink_to(self.library, target_is_directory=True)
        self.paths["platstdlib"] = str(alias)
        self.assertEqual(self.runtime_paths(), (self.library, self.executable))

    def test_missing_or_broad_library_paths_fail_closed(self) -> None:
        for invalid in ("", "/", str(self.prefix), str(self.root), "relative/path", str(self.root / "missing")):
            with self.subTest(path=invalid):
                self.paths["stdlib"] = invalid
                with self.assertRaises((RuntimeError, FileNotFoundError)):
                    self.runtime_paths()

    def test_incomplete_stdlib_and_invalid_executable_fail_closed(self) -> None:
        (self.library / "encodings" / "__init__.py").unlink()
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            self.runtime_paths()
        (self.library / "encodings" / "__init__.py").write_text("# fixture\n")
        self.executable.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, "executable is unavailable"):
            self.runtime_paths()
        self.executable.unlink()
        with self.assertRaises(FileNotFoundError):
            self.runtime_paths()

    def test_full_builder_mounts_actual_harness_interpreter_read_only_before_overlays(self) -> None:
        runtime = self.root / "runtime"
        endpoint = runtime / "data" / "bunshin-role" / "role.sock"
        endpoint.parent.mkdir(parents=True)
        endpoint.write_text("fixture, not a socket")
        workspace = self.root / "workspace"
        workspace.mkdir()
        pack = BunshinInvocationPack(
            invocation_id="python-mount-test", goal="mount test",
            workspace={"repo_path": str(workspace), "workspace_policy": {"mode": "read_only_repo"}},
            metadata={"sandbox": {"enabled": True, "backend": "bwrap", "run_id": "python-mount-test",
                                  "scratch_dir": str(self.root / "scratch")}},
        )
        with (
            patch.object(sandbox.sys, "executable", str(self.alias)),
            patch.object(sandbox.sysconfig, "get_paths", return_value=self.paths),
            patch.object(sandbox.shutil, "which", return_value="/usr/bin/bwrap"),
        ):
            harness = pal_harness_spec()
            argv, _ = sandbox.build_sandboxed_runner_invocation(
                runtime_root=runtime, pack=pack, argv=[*harness.worker_argv, "--help"], env={"PATH": "/usr/bin:/bin"},
            )
        triples = [argv[index:index + 3] for index in range(len(argv) - 2)]
        executable_bind = ["--ro-bind", str(self.executable), str(self.executable)]
        library_bind = ["--ro-bind", str(self.library), str(self.library)]
        dependency_bind = ["--ro-bind", self.paths["purelib"], self.paths["purelib"]]
        self.assertIn(executable_bind, triples)
        self.assertIn(library_bind, triples)
        self.assertLess(triples.index(library_bind), triples.index(dependency_bind))
        self.assertLess(triples.index(library_bind), triples.index(["--ro-bind", str(endpoint), str(endpoint)]))
        self.assertEqual(argv[argv.index("--") + 1:], [str(self.executable), "-m", "pal.bunshin.worker_main", "--help"])
        for path in (self.prefix, self.prefix.parent, self.executable.parent, self.alias):
            self.assertNotIn(["--ro-bind", str(path), str(path)], triples)
        for path in (self.library, self.executable):
            self.assertNotIn(["--bind", str(path), str(path)], triples)
        for flag in ("--unshare-net", "--disable-userns", "--unshare-user", "--unshare-pid"):
            self.assertIn(flag, argv)
