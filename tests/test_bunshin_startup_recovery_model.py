"""Model wiring and executable checks; TLC checks skip explicitly without its jar.

These checks do not claim that Python is extracted into TLA+ or that checking
model-file strings establishes the temporal properties. The optional subprocess
checks run TLC itself in a temporary directory, including a negative control.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC_ROOT = ROOT / "spec" / "bunshin_v2"


def test_bunshin_tla_checker_covers_every_model():
    script = (ROOT / "scripts" / "check_bunshin_v2_tla.sh").read_text()
    block = re.search(r"models=\((.*?)\)", script, re.DOTALL)
    assert block is not None
    models = block.group(1).split()
    assert len(models) == len(set(models))
    assert set(models) == {path.stem for path in SPEC_ROOT.glob("*.tla")}
    assert all((SPEC_ROOT / f"{name}.cfg").is_file() for name in models)
    assert "StartupRecoveryLifecycleUnsafe.cfg" in script
    assert "Invariant FreshStartNeverForgetsInitialization is violated" in script


def test_startup_recovery_config_checks_the_admission_contract():
    config = (SPEC_ROOT / "StartupRecoveryLifecycle.cfg").read_text()
    assert "AllowMissingResumeAsFresh = FALSE" in config
    for name in (
        "InitializationIsDurable", "FreshStartNeverForgetsInitialization",
        "SemanticWorkRequiresInitialization", "InitializationRequiresCurrentFence",
        "AwaitingAckHasDurableInitialization",
        "RunningRequiresInitializedCheckpoint", "NeverCreatedIsNotSuspended",
        "CheckpointFailureIsVisibleAndPermanent", "FailureRemainsVisible",
        "ExhaustedBudgetDoesNotRestart",
    ):
        assert f"INVARIANT {name}\n" in config
    assert "PROPERTY AdmissionEventuallyResolves\n" in config
    assert "PROPERTY ExhaustionEventuallySettles\n" in config
    assert "PROPERTY InitializationNeverReverts\n" in config
    assert "PROPERTY CheckpointSequenceNeverRegresses\n" in config
    unsafe = (SPEC_ROOT / "StartupRecoveryLifecycleUnsafe.cfg").read_text()
    assert "AllowMissingResumeAsFresh = TRUE" in unsafe
    assert "INVARIANT FreshStartNeverForgetsInitialization\n" in unsafe


@pytest.mark.parametrize("unsafe", [False, True], ids=["safe", "missing-resume-mutant"])
def test_startup_recovery_tlc(tmp_path, unsafe):
    jar_value = os.environ.get("TLA2TOOLS_JAR")
    if not jar_value:
        pytest.skip("TLC not run: set TLA2TOOLS_JAR to a pinned tla2tools.jar")
    jar = Path(jar_value).expanduser().resolve()
    assert jar.is_file(), f"TLA2TOOLS_JAR does not exist: {jar}"
    java = shutil.which("java")
    assert java is not None, "TLA2TOOLS_JAR is set but Java is unavailable"
    module = "StartupRecoveryLifecycle"
    config = module + ("Unsafe" if unsafe else "") + ".cfg"
    shutil.copyfile(SPEC_ROOT / f"{module}.tla", tmp_path / f"{module}.tla")
    shutil.copyfile(SPEC_ROOT / config, tmp_path / config)
    result = subprocess.run(
        [java, "-XX:+UseParallelGC", "-jar", str(jar), "-workers", "1",
         "-cleanup", "-config", config, f"{module}.tla"],
        cwd=tmp_path, capture_output=True, text=True, timeout=180, check=False,
    )
    output = result.stdout + result.stderr
    if unsafe:
        assert result.returncode != 0, output
        assert "Invariant FreshStartNeverForgetsInitialization is violated" in output
    else:
        assert result.returncode == 0, output
        assert "Model checking completed. No error has been found." in output


@pytest.mark.parametrize("state, expected", [
    ("uninitialized", "uninitialized"),
    ("active", "suspended"),
    ("suspended", "suspended"),
])
def test_model_failure_park_matches_durable_session_transition(state, expected):
    """FailAttempt/CrashBeforeAck must not manufacture initialization."""
    from pal.bunshin.role_protocol import role_session_target

    assert role_session_target(state, "park").value == expected


@pytest.mark.parametrize("state, activated, initialized", [
    ("uninitialized", "uninitialized", "active"),
    ("active", "active", "active"),
    ("suspended", "active", "active"),
])
def test_model_start_then_checkpoint_commit_matches_session_transition(state, activated, initialized):
    """StartProcess then CommitInitialization matches ACTIVATE then INITIALIZE."""
    from pal.bunshin.role_protocol import role_session_target

    starting = role_session_target(state, "activate")
    assert starting.value == activated
    assert role_session_target(starting, "initialize").value == initialized
