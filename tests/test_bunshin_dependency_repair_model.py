"""Bounded dependency-repair design exploration and optional TLC checks.

The Python helpers are finite design abstractions, not production runtime tests,
TLA parsers, or evidence that TLC ran. They exhaust their declared finite state
spaces, check safety, compare joined terminal outcomes within one cohort, and
check existential terminal reachability. They do not establish universal liveness
or a compositional proof. Optional TLC runs require an existing pinned jar; these
tests never download/install a checker or use scratch-directory artifacts.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.dependency_repair_models import cohort, control_overlay, later_cohort, refinements


ROOT = Path(__file__).resolve().parents[1]
SPEC_ROOT = ROOT / "spec" / "bunshin_v2"


@pytest.mark.parametrize("dependency,supersede,invalid_kind", [
    (True, False, None),
    (True, True, None),
    (False, False, None),
    (False, True, None),
    (False, False, "invalid_scope"),
    (False, False, "contract_only"),
], ids=["dependency", "dependency-superseded", "pass", "pass-superseded",
        "invalid-scope", "contract-only"])
def test_bounded_dependency_cohort(dependency, supersede, invalid_kind):
    model = cohort.Model(dependency, supersede=supersede, invalid_kind=invalid_kind)
    result = cohort.explore(model)
    assert result["kind"] == "bounded_python_pass", result
    assert result["all_states_have_terminal_path"]
    assert result["terminal_signature_counts"] == {"1": 1, "3": 1}
    assert "late_raw_result_after_freeze" in result["positive_traces"]
    if dependency:
        assert "provider_coders_parallel" in result["positive_traces"]


COHORT_MUTANTS = [
    ("premature_stale", "QuiescenceBeforeStale"),
    ("lease_ignored", "QuiescenceBeforeStale"),
    ("missing_frontier", "FrontierCompleteness"),
    ("per_packet_stale", "TargetsNotStaledAsConsumers"),
    ("replay_reopen", "OneReopenPerWave"),
    ("old_ack_clears_replacement", "NoReplacementWithOldFence"),
    ("invalidated_pass_publishes", "NoInvalidatedPublication"),
    ("circular_capture", None),
    ("checker_ignores_provider", "RepairCheckersAwaitAcceptedInputs"),
    ("late_setup_launch", "NoAdmissionBeforeApply"),
    ("drop_invalid_obligation", "InvalidRouteEvidenceRetained"),
    ("route_ineligible_report", "IneligibleReportsNeverProviderTargets"),
]


@pytest.mark.parametrize("mutant,invariant", COHORT_MUTANTS)
def test_cohort_negative_control_has_counterexample(mutant, invariant):
    invalid = mutant in {"drop_invalid_obligation", "route_ineligible_report"}
    dependency = mutant != "invalidated_pass_publishes" and not invalid
    model = cohort.Model(
        dependency, mutant, supersede=mutant != "circular_capture",
        invalid_kind="invalid_scope" if invalid else None,
    )
    result = cohort.explore(model)
    if invariant is None:
        assert result["kind"] == "progress_counterexample", result
    else:
        assert result["kind"] == "counterexample", result
        assert invariant in result["invariants"], result
    assert result["trace"]


@pytest.mark.parametrize("factory,witness", [
    (refinements.setup_model, "spawn_finishes_after_freeze"),
    (refinements.join_model, "separate_later_cohorts"),
    (lambda: refinements.join_model(bridge=True), "bridge_registered_last"),
    (refinements.obligation_model, "mixed_and_invalid_findings_carried"),
    (later_cohort.run, "prior_obligation_revalidated_new_binding"),
], ids=["setup-before-assignment", "partial-overlap", "third-bridge",
        "historical-obligations", "later-upstream-cohort"])
def test_bounded_protocol_refinements(factory, witness):
    result = factory()
    assert result["kind"] == "bounded_python_pass", result
    assert result["all_states_have_terminal_path"]
    assert witness in result["witnesses"]


REFINEMENT_MUTANTS = [
    ("setup", "ack_while_starting", "NoAckWithStartingOwner"),
    ("setup", "reusable_invocation_cleanup", "OldCleanupCannotTouchReplacement"),
    ("setup", "create_after_freeze", "StaleMeansOldOwnerGone"),
    ("overlap", "apply_cached_scope", "CurrentPendingComponentJoinedAtomically"),
    ("bridge", "apply_cached_scope", "CurrentPendingComponentJoinedAtomically"),
    ("obligation", "drop_module_findings", "CapturedFindingsRemainRequired"),
    ("obligation", "route_invalid_scope", "OnlyValidatedScopeTargetsProviders"),
    ("obligation", "drop_history_on_replacement", "CapturedFindingsRemainRequired"),
    ("obligation", "drop_history_on_second_invalidation", "CapturedFindingsRemainRequired"),
    ("obligation", "pass_skips_old_case", "PassRequiresExplicitResolution"),
    ("later", "stale_active_prior_repair", "NoPrematureStale"),
    ("later", "accept_old_binding", "NoOldBindingAcceptance"),
    ("later", "drop_prior_obligation", "PriorObligationCarried"),
    ("later", "reuse_old_case_resolution", "PassUsesCurrentCandidateResolution"),
    ("later", "reapply_original_packet", "OriginalPacketIsNotReapplied"),
]


@pytest.mark.parametrize("kind,mutant,invariant", REFINEMENT_MUTANTS)
def test_refinement_negative_control_has_counterexample(kind, mutant, invariant):
    if kind == "setup":
        result = refinements.setup_model(mutant)
    elif kind in {"overlap", "bridge"}:
        result = refinements.join_model(bridge=kind == "bridge", mutant=mutant)
    elif kind == "obligation":
        result = refinements.obligation_model(mutant)
    else:
        result = later_cohort.run(mutant)
    assert result["kind"] == "counterexample", result
    assert invariant in result["invariants"], result
    assert result["trace"]


def test_intermediate_checker_policy_keeps_initial_producers_parallel():
    """Static model wiring only; production graph-policy tests are separate."""
    model = (SPEC_ROOT / "GraphExecutionLifecycle.tla").read_text()
    checker = model.split("CheckerProviders(n) ==", 1)[1].split("SemanticConsumers", 1)[0]
    assert "IF n = Sink THEN NonSink" in checker
    assert "ELSE IF n = B THEN {A}" in checker
    assert 'nodeState = [n \\in Nodes |-> "ProducerReady"]' in model
    producer = model.split("StartProducer(n) ==", 1)[1].split("SubmitProduct(n) ==", 1)[0]
    assert "CheckerProviders" not in producer
    config = (SPEC_ROOT / "GraphExecutionLifecycle.cfg").read_text()
    assert "INVARIANT CheckerStartsAfterProducedProviders\n" in config


@pytest.mark.parametrize("scenario", control_overlay.SCENARIOS)
def test_bounded_repair_control_overlay(scenario):
    result = control_overlay.run(scenario)
    assert result["kind"] == "bounded_python_pass", result
    assert result["all_states_have_terminal_path"]
    if scenario == "terminal_source":
        assert "terminal_source_before_registration" in result["witnesses"]
    else:
        assert "pause_cleanup_preserves_frontier" in result["witnesses"]
        assert "recoverable_cursor_restored_without_worker" in result["witnesses"]
        if scenario == "terminal_peer":
            assert "terminal_peer_evidence_fenced" in result["witnesses"]
        else:
            assert "exhausted_apply_recovers_once" in result["witnesses"]
    if scenario.startswith("changed_"):
        assert "changed_identity_is_not_no_progress" in result["witnesses"]


CONTROL_MUTANTS = [
    ("dependency", "pause_drops_frontier", "FrozenCursorPreserved"),
    ("dependency", "cleanup_closes_paused_graph", "BlockedControlDoesNotCloseOrApply"),
    ("dependency", "cleanup_waits_for_graph_close", None),
    ("dependency", "cleanup_drops_raw_receipt", "EvidencePreserved"),
    ("dependency", "resolve_rebinds_cursor", "FrozenCursorPreserved"),
    ("dependency", "resolve_changes_slot", "FrozenCursorPreserved"),
    ("dependency", "resolve_starts_worker", "CleanupIsNotAdmission"),
    ("terminal_peer", "terminal_peer_joins_provider", "TerminalDoesNotRegisterProviders"),
    ("terminal_peer", "terminal_ordinary_resolve", "TerminalCannotBeWaived"),
    ("terminal_source", "terminal_ordinary_resolve", "TerminalCannotBeWaived"),
    ("terminal_peer", "drop_terminal_evidence", "EvidencePreserved"),
    ("dependency", "recovery_reuses_failed_effect", "RecoveryUsesOriginalRegistration"),
    ("dependency", "recovery_changes_authority", "RecoveryUsesOriginalRegistration"),
    ("dependency", "revive_failed_effect", "FailedEffectRemainsFailed"),
    ("dependency", "charge_before_apply", "ExactlyOneRouteCharge"),
    ("dependency", "replay_double_charge", "ExactlyOneRouteCharge"),
    ("pass", "charge_invalidated_peer", "ExactlyOneRouteCharge"),
    ("local", "charge_invalidated_peer", "ExactlyOneRouteCharge"),
    ("correction", "charge_invalidated_peer", "ExactlyOneRouteCharge"),
    ("changed_candidate", "no_progress_ignores_identity", "FailureHistoryAccounting"),
    ("changed_finding", "no_progress_ignores_identity", "FailureHistoryAccounting"),
]


@pytest.mark.parametrize("scenario,mutant,invariant", CONTROL_MUTANTS)
def test_control_overlay_negative_control(scenario, mutant, invariant):
    result = control_overlay.run(scenario, mutant)
    if invariant is None:
        assert result["kind"] == "progress_counterexample", result
    else:
        assert result["kind"] == "counterexample", result
        assert invariant in result["invariants"], result


TLC_CASES = [
    ("DependencyRepairCohort", "DependencyRepairCohort"),
    ("DependencyRepairCohort", "DependencyRepairCohortPass"),
    ("DependencyRepairCohort", "DependencyRepairCohortInvalidScope"),
    ("DependencyRepairCohort", "DependencyRepairCohortContractOnly"),
    ("DependencyRepairSetupOwnership", "DependencyRepairSetupOwnership"),
    ("DependencyRepairPartialOverlap", "DependencyRepairPartialOverlap"),
    ("DependencyRepairPartialOverlap", "DependencyRepairPartialOverlapTwo"),
    ("DependencyRepairObligationCarry", "DependencyRepairObligationCarry"),
    ("DependencyRepairLaterCohort", "DependencyRepairLaterCohort"),
    ("DependencyRepairControlOverlay", "DependencyRepairControlOverlay"),
    ("DependencyRepairControlOverlay", "DependencyRepairControlOverlayPass"),
    ("DependencyRepairControlOverlay", "DependencyRepairControlOverlayLocal"),
    ("DependencyRepairControlOverlay", "DependencyRepairControlOverlayCorrection"),
    ("DependencyRepairControlOverlay", "DependencyRepairControlOverlayTerminalSource"),
    ("DependencyRepairControlOverlay", "DependencyRepairControlOverlayTerminalPeer"),
    ("DependencyRepairControlOverlay", "DependencyRepairControlOverlayChangedCandidate"),
    ("DependencyRepairControlOverlay", "DependencyRepairControlOverlayChangedFinding"),
    ("GraphExecutionLifecycle", "GraphExecutionLifecycle"),
]


@pytest.mark.parametrize("module,config", TLC_CASES)
def test_dependency_repair_model_configs_and_runner(module, config):
    """Configuration wiring, deliberately not a TLC parse/execution claim."""
    text = (SPEC_ROOT / f"{config}.cfg").read_text()
    assert "INIT Init\nNEXT Next\n" in text
    assert "INVARIANT TypeOK\n" in text
    assert f"MODULE {module}" in (SPEC_ROOT / f"{module}.tla").read_text()
    assert config in (ROOT / "scripts/check_bunshin_v2_tla.sh").read_text()


@pytest.mark.parametrize("module,config", TLC_CASES)
def test_dependency_repair_tlc_when_pinned_jar_available(tmp_path, module, config):
    jar_value = os.environ.get("TLA2TOOLS_JAR")
    if not jar_value:
        pytest.skip("TLC not run: set TLA2TOOLS_JAR to an existing pinned tla2tools.jar")
    jar = Path(jar_value).expanduser().resolve()
    assert jar.is_file(), f"TLA2TOOLS_JAR does not exist: {jar}"
    java = shutil.which("java")
    assert java is not None, "TLA2TOOLS_JAR is set but Java is unavailable"
    for name in (f"{module}.tla", f"{config}.cfg"):
        shutil.copyfile(SPEC_ROOT / name, tmp_path / name)
    result = subprocess.run(
        [java, "-XX:+UseParallelGC", "-jar", str(jar), "-workers", "1",
         "-cleanup", "-config", f"{config}.cfg", f"{module}.tla"],
        cwd=tmp_path, capture_output=True, text=True, timeout=180, check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Model checking completed. No error has been found." in output
