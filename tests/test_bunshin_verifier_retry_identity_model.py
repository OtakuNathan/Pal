"""Actual TLC for semantic retry ownership; no production or live service calls."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC_ROOT = ROOT / "spec" / "bunshin"
MODULE = "VerifierRetryIdentity"
CASES = [
    ("", "none", None),
    ("RawFullRefs", "raw-full-refs", "OwnedRetryRetainsCurrent"),
    ("RawFullRefsSilentPass", "raw-full-refs", "NoSilentPassWithoutCRUD"),
    ("IgnoreCandidate", "ignore-candidate", "CandidateChangesNeverCurrent"),
    ("IgnoreSemanticRef", "ignore-semantic-ref", "SemanticRefChangesNeverCurrent"),
    ("IgnorePolicy", "ignore-policy", "VerificationPolicyNeverIgnored"),
    ("IgnoreSession", "ignore-session", "ForeignSessionNeverCurrent"),
    ("IgnoreGeneration", "ignore-generation", "NewGenerationNeverCurrent"),
    ("IgnoreReceipt", "ignore-receipt", "ReceiptBackedSourceNeverCurrent"),
    ("IgnoreProjection", "ignore-projection", "SubmittedProjectionNeverCurrent"),
    ("MalformedAsExternal", "malformed-as-external", "MalformedIdentityNeverRebinds"),
    ("RebindWitness", "none", "NoSemanticRebindWitness"),
]


def test_retry_identity_config_and_runner_wiring():
    """Static wiring only; this is not TLA parsing or model checking."""
    script = (ROOT / "scripts" / "check_bunshin_tla.sh").read_text()
    assert f"    {MODULE}\n" in script
    for suffix, fault, invariant in CASES:
        cfg = (SPEC_ROOT / f"{MODULE}{suffix}.cfg").read_text()
        assert f'CONSTANT Fault = "{fault}"\n' in cfg
        if invariant:
            assert f"INVARIANT {invariant}\n" in cfg
            assert f"{suffix}:{invariant}" in script


@pytest.mark.parametrize("suffix,fault,invariant", CASES, ids=[x[0] or "safe" for x in CASES])
def test_retry_identity_tlc(tmp_path, suffix, fault, invariant):
    value = os.environ.get("TLA2TOOLS_JAR")
    if not value:
        pytest.skip("TLC not run: set TLA2TOOLS_JAR to an existing pinned tla2tools.jar")
    jar = Path(value).expanduser().resolve()
    assert jar.is_file(), f"TLA2TOOLS_JAR does not exist: {jar}"
    java = shutil.which("java")
    assert java is not None, "TLA2TOOLS_JAR is set but Java is unavailable"
    cfg = f"{MODULE}{suffix}.cfg"
    shutil.copyfile(SPEC_ROOT / f"{MODULE}.tla", tmp_path / f"{MODULE}.tla")
    shutil.copyfile(SPEC_ROOT / cfg, tmp_path / cfg)
    log = tmp_path / "tlc.log"
    with log.open("w") as stream:
        result = subprocess.run(
            [java, "-XX:+UseParallelGC", "-jar", str(jar), "-workers", "1", "-cleanup",
             "-config", cfg, f"{MODULE}.tla"], cwd=tmp_path, stdout=stream,
            stderr=subprocess.STDOUT, text=True, timeout=180, check=False,
        )
    output = log.read_text()
    if invariant:
        assert result.returncode != 0, output
        assert f"Invariant {invariant} is violated" in output
    else:
        assert result.returncode == 0, output
        assert "Model checking completed. No error has been found." in output
