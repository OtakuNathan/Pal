"""Formal wiring, supplemental finite Python experiments, and optional REAL TLC.

Python exploration is deliberately separate from the TLA module and production
implementation. Only the subprocess tests below parse/model-check TLA+ with TLC.
An existing pinned jar is required; tests never download or install one.
"""
from __future__ import annotations

import os
from itertools import product
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.verifier_draft_model import (
    CASES, FINDINGS, Model, State, authority, explore, recover_source_as_current,
)


ROOT = Path(__file__).resolve().parents[1]
SPEC_ROOT = ROOT / "spec" / "bunshin_v2"
MODULE = "VerifierDraftLifecycle"
MUTANTS = [
    ("ReceiptBlind", "finding", "receipt-blind", "ReceiptFreezesBothDrafts"),
    ("SubmitWithoutCAS", "case", "submit-without-cas", "AtomicSubmissionSnapshot"),
    ("StaleAuthority", "authority", "stale-authority", "MutationsUseCurrentAuthority"),
    ("StaleEvidence", "case", "stale-evidence", "PassRequiresCurrentEvidence"),
    ("HistoryRewrite", "history", "history-rewrite", "HistoryPreserved"),
    ("CompletedLocked", "finding", "completed-locked", "DraftAllowsFindingCRUD"),
    ("ReplayRewrite", "finding", "replay-rewrite", "ReceiptFreezesBothDrafts"),
    ("ConflictingReplay", "finding", "conflicting-replay", "MutationReplayRejectsConflicts"),
    ("OmitFinding", "finding", "omit-finding", "ReceiptFreezesBothDrafts"),
    ("ReceiptBlindInheritance", "history", "receipt-blind-inheritance", "SourceReceiptsFreezeInheritance"),
]


@pytest.mark.parametrize("scenario", ["finding", "case", "authority", "history"])
def test_supplemental_finite_draft_scenario(scenario):
    result = explore(Model(scenario))
    assert result["kind"] == "bounded_python_pass", result
    assert "receipt_freezes_active_projection" in result["witnesses"]
    witness = ("corpus_changes_without_rewriting_sealed_pass" if scenario == "case"
               else "withdraw_then_pass_with_history")
    assert witness in result["witnesses"]


@pytest.mark.parametrize("suffix,scenario,fault,invariant", MUTANTS)
def test_supplemental_mutant_has_counterexample(suffix, scenario, fault, invariant):
    result = explore(Model(scenario, fault))
    assert result["kind"] == "bounded_python_counterexample", result
    assert invariant in result["invariants"], result
    assert result["trace"] or fault == "receipt-blind-inheritance", result


def test_supplemental_inheritance_classification_exhausts_source_kinds():
    combinations = list(product(("active", "submitted"), ("none", "pending", "accepted"),
                                (False, True), (False, True)))
    assert len(combinations) == 24
    for projection, phase, owned, same_input in combinations:
        result = recover_source_as_current(projection, phase, owned, same_input)
        assert result == (projection == "active" and phase == "none" and owned and same_input)


def test_supplemental_wrong_cas_never_edits_either_draft():
    model = Model()
    state = State()
    assert all(model.edit_finding(state, value, expected=1) is None for value in FINDINGS)
    assert all(model.edit_case(state, value, expected=1) is None for value in CASES)
    for token in (("other-role", *authority(state)[1:]),
                  ("verifier", "other-assignment", 1, 1),
                  ("verifier", "assignment-0", 0, 1),
                  ("verifier", "assignment-0", 1, 0)):
        assert model.edit_finding(state, "completed-blocking", token=token) is None


def test_verifier_draft_tla_configs_and_runner_wiring():
    """Static wiring only; this does not parse TLA+ or claim a checked model."""
    script = (ROOT / "scripts" / "check_bunshin_v2_tla.sh").read_text()
    config = (SPEC_ROOT / f"{MODULE}.cfg").read_text()
    assert f"    {MODULE}\n" in script
    for invariant in (
        "ReceiptFreezesBothDrafts", "HistoryPreserved", "MutationsUseCurrentAuthority",
        "MutationsUseCurrentVersion", "PassRequiresCurrentEvidence", "DraftAllowsFindingCRUD",
        "DraftAllowsCaseCRUD", "HistoryCannotBeTargeted", "MutationReplayRejectsConflicts",
    ):
        assert f"INVARIANT {invariant}\n" in config
    for suffix, _, fault, invariant in MUTANTS:
        mutant = (SPEC_ROOT / f"{MODULE}{suffix}.cfg").read_text()
        assert f'CONSTANT Fault = "{fault}"\n' in mutant
        assert f"INVARIANT {invariant}\n" in mutant
        assert f"{suffix}:{invariant}" in script
    assert "WithdrawalWitness:NoWithdrawalPassWitness" in script


TLC_CASES = [("", None)] + [(suffix, invariant) for suffix, _, _, invariant in MUTANTS] + [
    ("WithdrawalWitness", "NoWithdrawalPassWitness"),
]


@pytest.mark.parametrize("suffix,invariant", TLC_CASES, ids=[x[0] or "safe" for x in TLC_CASES])
def test_verifier_draft_tlc(tmp_path, suffix, invariant):
    value = os.environ.get("TLA2TOOLS_JAR")
    if not value:
        pytest.skip("TLC not run: set TLA2TOOLS_JAR to an existing pinned tla2tools.jar")
    jar = Path(value).expanduser().resolve()
    assert jar.is_file(), f"TLA2TOOLS_JAR does not exist: {jar}"
    java = shutil.which("java")
    assert java is not None, "TLA2TOOLS_JAR is set but Java is unavailable"
    config = f"{MODULE}{suffix}.cfg"
    shutil.copyfile(SPEC_ROOT / f"{MODULE}.tla", tmp_path / f"{MODULE}.tla")
    shutil.copyfile(SPEC_ROOT / config, tmp_path / config)
    log = tmp_path / "tlc.log"
    with log.open("w") as stream:
        result = subprocess.run(
            [java, "-XX:+UseParallelGC", "-jar", str(jar), "-workers", "1", "-cleanup",
             "-config", config, f"{MODULE}.tla"],
            cwd=tmp_path, stdout=stream, stderr=subprocess.STDOUT,
            text=True, timeout=900, check=False,
        )
    output = log.read_text()
    if invariant:
        assert result.returncode != 0, output
        assert f"Invariant {invariant} is violated" in output
    else:
        assert result.returncode == 0, output
        assert "Model checking completed. No error has been found." in output
