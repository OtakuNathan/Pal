"""Run the module's complete verifier corpus before returning a repair to review."""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

from pal.shared.messages import BunshinInvocationPack
from pal.bunshin.execution_values import workspace_content_fingerprint
from pal.bunshin.verification_corpus import verification_corpus_files
from pal.bunshin.verification_report import VERIFICATION_REPORT_FILENAME


def suite_command(scope: Mapping[str, Any], files: list[str]) -> str:
    """Select one whole-suite invocation, including unchanged historical tests."""
    path = str(scope.get("path") or "")
    runner = f"{path}/run-tests.sh"
    if scope.get("kind") == "file" and path.endswith(".sh") and path in files:
        return shlex.join(["sh", path])
    if runner in files:
        return shlex.join(["sh", runner])
    cases = [Path(name) for name in files]
    discoverable = scope.get("kind") == "file" or any(
        case.match("test_*.py") or case.match("*_test.py") for case in cases
    )
    if cases and discoverable and all(case.suffix == ".py" for case in cases):
        executable = str(Path(sys.executable).resolve(strict=True))
        return shlex.join([
            executable, "-B", "-m", "pytest", "-p", "no:cacheprovider",
            "--maxfail=0", "--tb=short", "--", path,
        ])
    return ""


def repair_precheck_plan(repo: Path, scope: Mapping[str, Any]) -> dict[str, Any]:
    files = verification_corpus_files(repo, scope, exclude_filename_at_root=VERIFICATION_REPORT_FILENAME)
    command = suite_command(scope, files)
    if not command:
        # A routed provider repair can have only consumer-side tests. Older
        # verifier corpora may also predate the whole-suite entrypoint.
        return {"reason": "No local executable verifier suite"}
    return {"command": command, "workspace_fingerprint": workspace_content_fingerprint(repo, include_symlinks=True)}


def repair_receipt_fields(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Fields accepted into the durable report only after Manager validation."""
    return {**plan, "status": "passed" if plan.get("command") else "requires_verifier"}


def check_manager_repair(runtime_root: Path, pack: Mapping[str, Any], report: Mapping[str, Any]) -> None:
    from pal.bunshin.sandbox import build_sandboxed_runner_invocation, with_bunshin_sandbox_metadata

    workspace = dict(pack["workspace"])
    repo = Path(workspace["repo_path"])
    scope = workspace["repair_corpus_scope"]
    expected = repair_precheck_plan(repo, scope)
    if report != repair_receipt_fields(expected):
        raise ValueError("Repair precheck does not match the current Candidate and verifier corpus. Resubmit.")
    if not expected.get("command"):
        return
    workspace.update(workspace_policy={"mode": "read_only_repo"}, run_dir="", build_scratch_dir="/tmp/build")
    with tempfile.TemporaryDirectory(prefix="bunshin-precheck-") as private:
        bound = with_bunshin_sandbox_metadata(runtime_root, BunshinInvocationPack.from_dict(
            {**pack, "workspace": workspace}), run_id=Path(private).name)
        scratch = Path(private) / "scratch"
        bound.metadata["sandbox"]["scratch_dir"] = str(scratch)
        (scratch / "tmp/build").mkdir(parents=True)
        command, env = build_sandboxed_runner_invocation(
            runtime_root=runtime_root, pack=bound, argv=["/bin/sh", "-c", expected["command"]],
            env={"PATH": os.environ.get("PATH") or os.defpath}, role_runtime=False,
        )
        with tempfile.TemporaryFile() as log:
            try:
                result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                        stderr=subprocess.STDOUT, timeout=240)
                failure = f"exit={result.returncode}" if result.returncode else ""
            except subprocess.TimeoutExpired:
                failure = "timed out after 240 seconds"
            if failure:
                clipped = max(0, log.tell() - 6000)
                log.seek(clipped)
                prefix = f"[{clipped} earlier output bytes omitted]\n" if clipped else ""
                raise ValueError(f"Verifier suite failed ({expected['command']}, {failure}):\n"
                                 + prefix + log.read().decode(errors="replace"))
    if repair_precheck_plan(repo, scope) != expected:
        raise ValueError("Candidate changed during the verifier suite. Resubmit the current tree.")
