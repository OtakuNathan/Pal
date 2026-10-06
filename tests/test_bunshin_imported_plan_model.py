"""Bounded imported-plan abstraction, real cycle trace, and optional TLC checks.

The finite Python model explores its declared state space, including corrupt
legacy evidence and deliberate unsafe controls. The imported product carries a
nonempty foreign GraphIR; graphless imports are out of scope. It is not extracted from Python
production code or TLA+, and does not establish liveness or prove transaction
implementation. The real PlanCycle trace checks the abstraction boundary; router
and persistence tests cover public admission and rollback. TLC is only claimed
when an existing TLA2TOOLS_JAR is supplied; no checker is downloaded here.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from pal.bunshin.v2.cycle_protocol import (
    AssignmentKind, CycleAction, CycleAssignment, CycleSlot, CycleTransitionError,
    CycleVerdict, NodeCycle, NodeCycleState, PlanCycle, PlanCycleState,
)


ROOT = Path(__file__).resolve().parents[1]
SPEC_ROOT = ROOT / "spec" / "bunshin_v2"
IMPORTED = "ImportedArchitecture"
FRESH = "FreshArchitecture"
REFS = ("", IMPORTED, FRESH, "OtherArchitecture")
BAD_EVIDENCE = (
    "event_action", "event_ref", "request_operation", "request_ref",
    "revision_manifest_ref", "requirements_ref", "workflow_revision",
)


@dataclass(frozen=True)
class State:
    legacy: bool = False
    evidence: str = "matching"
    recovered: bool = False
    import_recorded: bool = False
    architecture: str = "ArchitectQueued"
    plan: str = "ProducerReady"
    generation: int = 1
    slot: str = "None"
    product: str = ""
    product_generation: int = 0
    verdict_generation: int = 0
    accepted_product: str = ""
    producer_admissions: int = 0
    revision: int = 1
    workflow_revision: int = 1
    installed: str = ""

    @property
    def pristine(self):
        return (self.plan == "ProducerReady" and self.generation == 1
                and self.slot == "None" and not self.product
                and self.product_generation == 0 and self.verdict_generation == 0
                and not self.accepted_product)


def initial_states():
    return (State(), *(State(legacy=True, import_recorded=True, architecture="ReviewQueued", evidence=e)
                       for e in ("matching", *BAD_EVIDENCE)))


def graph_owner(ref):
    return "NewWorkflowGraph" if ref == FRESH else "ForeignGraph"


def graph_generation(ref):
    return 1 if ref in (IMPORTED, FRESH) else 0


def step(s, action, *, ref="", mutant="none"):
    if action in ("import", "rollback", "split_import"):
        if s.legacy or s.architecture != "ArchitectQueued" or not s.pristine:
            return None
        if action == "rollback":
            return s
        if action == "split_import":
            return (replace(s, architecture="ReviewQueued", import_recorded=True)
                    if mutant == "split_import" else None)
        if ref == IMPORTED:
            return replace(s, architecture="ReviewQueued", plan="CheckerReady",
                           product=ref, product_generation=1, import_recorded=True)
    elif action == "recover":
        if (s.legacy and s.import_recorded and s.architecture == "ReviewQueued" and s.pristine
                and ref == IMPORTED
                and (s.evidence == "matching" or mutant == "unproven_recovery")):
            return replace(s, plan="CheckerReady", product=ref,
                           product_generation=1, recovered=True)
    elif action == "replay":
        if (s.architecture == "ReviewQueued" and s.plan == "CheckerReady"
                and s.generation == 1 and ref == IMPORTED and s.product == ref):
            return s
    elif action == "start_reviewer":
        if (s.architecture == "ReviewQueued" and s.plan == "CheckerReady"
                and s.product and s.product_generation == s.generation):
            return replace(s, architecture="Reviewing", plan="Checking", slot="Checker")
    elif action == "reviewer_passed":
        if (s.architecture == "Reviewing" and s.plan == "Checking"
                and s.slot == "Checker"):
            return replace(s, architecture="HumanReview", plan="HumanReview", slot="None",
                           verdict_generation=s.generation, accepted_product=s.product)
    elif action == "human_edit":
        if s.architecture == s.plan == "HumanReview" and s.generation == 1:
            return replace(s, architecture="ArchitectQueued", plan="RepairReady", generation=2,
                           product="", product_generation=0, verdict_generation=0,
                           accepted_product="", revision=2, workflow_revision=2)
    elif action == "start_architect":
        if (s.architecture == "ArchitectQueued" and s.plan == "RepairReady"
                and s.generation == 2):
            return replace(s, architecture="ArchitectRunning", plan="Producing",
                           slot="Producer", producer_admissions=1)
    elif action == "submit_fresh":
        if (s.architecture == "ArchitectRunning" and s.plan == "Producing"
                and s.slot == "Producer"):
            return replace(s, architecture="ReviewQueued", plan="CheckerReady", slot="None",
                           product=FRESH, product_generation=s.generation)
    elif action == "human_accept":
        # Public ownership preflight runs before token or projection mutation.
        if (s.architecture == s.plan == "HumanReview"
                and (graph_owner(s.product) == "NewWorkflowGraph" or mutant == "foreign_accept")):
            return replace(s, architecture="Accepted", plan="Accepted")
    elif action == "install":
        if (s.architecture == s.plan == "Accepted" and not s.installed
                and s.product == s.accepted_product
                and graph_owner(s.product) == "NewWorkflowGraph"):
            return replace(s, installed=IMPORTED if mutant == "foreign_install" else s.product)
    return None


def successors(s, mutant="none"):
    actions = [(action, ref) for action in ("import", "recover", "replay") for ref in REFS]
    actions += [(action, "") for action in (
        "rollback", "start_reviewer", "reviewer_passed", "human_edit", "start_architect",
        "submit_fresh", "human_accept", "install", "split_import",
    )]
    for action, ref in actions:
        target = step(s, action, ref=ref, mutant=mutant)
        if target is not None:
            yield action, target


def violations(s):
    checks = {
        "NewImportIsAtomic": (
            s.legacy or s.architecture != "ReviewQueued" or s.generation != 1
            or (s.plan == "CheckerReady" and s.product == IMPORTED)),
        "RecoveryRequiresExactEvidence": (
            not s.recovered or (s.import_recorded and s.evidence == "matching")),
        "ImportEventAndBindingCommitTogether": (
            s.legacy or s.import_recorded == (s.plan != "ProducerReady")),
        "NoSyntheticProducer": s.generation != 1 or s.producer_admissions == 0,
        "ReviewerRequiresBoundProduct": (
            s.architecture not in {"Reviewing", "HumanReview", "Accepted"}
            or (s.product in {IMPORTED, FRESH} and s.product_generation == s.generation)),
        "ReviewAndAcceptanceAreCurrent": (
            s.plan not in {"HumanReview", "Accepted"}
            or s.product_generation == s.verdict_generation == s.generation),
        "ExactlyOneRunningSlot": (
            (s.plan == "Producing") == (s.slot == "Producer")
            and (s.plan == "Checking") == (s.slot == "Checker")),
        "RevisionPointersStayAtomic": s.workflow_revision == s.revision,
        "PublicAcceptanceRequiresCurrentGraph": (
            s.architecture != "Accepted" or graph_owner(s.product) == "NewWorkflowGraph"),
        "InstalledGraphBelongsToWorkflow": (
            not s.installed or graph_owner(s.installed) == "NewWorkflowGraph"),
        "FreshGraphHasIndependentGeneration": (
            s.installed != FRESH
            or (s.generation == 2 and graph_generation(s.installed) == 1
                and s.producer_admissions == 1 and s.architecture == "Accepted"
                and s.product == s.accepted_product)),
    }
    return tuple(name for name, valid in checks.items() if not valid)


def explore(mutant="none"):
    traces = {s: () for s in initial_states()}
    pending = deque(traces)
    while pending:
        s = pending.popleft()
        failed = violations(s)
        if failed:
            return traces, (failed, traces[s])
        for action, target in successors(s, mutant):
            if target not in traces:
                traces[target] = (*traces[s], action)
                pending.append(target)
    return traces, None


def test_bounded_imported_plan_exhausts_safe_state_space():
    states, failure = explore()
    assert failure is None, failure
    # Reachability witnesses keep successful checking/acceptance non-vacuous.
    for legacy in (False, True):
        assert any(s.legacy == legacy and s.installed == FRESH for s in states)
        imported_reviews = [s for s in states if s.legacy == legacy
                            and s.plan == "HumanReview" and s.product == IMPORTED]
        assert imported_reviews
        for s in imported_reviews:
            assert step(s, "human_accept") is None
            assert step(s, "install") is None
            assert step(s, "human_edit") is not None
    assert all(s.product == FRESH for s in states if s.architecture == "Accepted")
    assert {s.evidence for s in states} == {"matching", *BAD_EVIDENCE}
    assert all(s.pristine for s in states if s.evidence != "matching")


@pytest.mark.parametrize("mutant,invariant", [
    ("split_import", "NewImportIsAtomic"),
    ("unproven_recovery", "RecoveryRequiresExactEvidence"),
    ("foreign_accept", "PublicAcceptanceRequiresCurrentGraph"),
    ("foreign_install", "InstalledGraphBelongsToWorkflow"),
])
def test_imported_plan_negative_controls(mutant, invariant):
    _, failure = explore(mutant)
    assert failure is not None
    failed, trace = failure
    assert invariant in failed
    assert trace


def test_imported_binding_replay_rollback_and_foreign_refs():
    new = State()
    assert step(new, "rollback") == new
    bound = step(new, "import", ref=IMPORTED)
    assert bound is not None
    assert step(bound, "replay", ref=IMPORTED) == bound
    assert step(new, "start_reviewer") is None
    for ref in REFS:
        if ref != IMPORTED:
            assert step(new, "import", ref=ref) is None
            assert step(bound, "replay", ref=ref) is None
    for evidence in BAD_EVIDENCE:
        legacy = State(legacy=True, import_recorded=True, architecture="ReviewQueued", evidence=evidence)
        assert step(legacy, "recover", ref=IMPORTED) is None
        assert step(legacy, "start_reviewer") is None


def test_imported_plan_real_cycle_trace_matches_edit_refinement():
    """Public settlement hides CHECKER_ACCEPTED -> REQUEST_HUMAN_REVIEW."""
    cycle = PlanCycle("plan:new-workflow")
    cycle = cycle.transition(CycleAction.IMPORT_PRODUCT, product_ref=IMPORTED)
    assert cycle.state == PlanCycleState.CHECKER_READY
    assert cycle.active_assignment is None
    assert cycle.accepted_product_ref == ""
    assert cycle.last_verdict is None
    cycle = cycle.transition(CycleAction.START_CHECKER, assignment=CycleAssignment(
        CycleSlot.CHECKER, AssignmentKind.INITIAL, 1, IMPORTED))
    assert cycle.state == PlanCycleState.CHECKING
    cycle = cycle.transition(CycleAction.CHECKER_ACCEPTED, verdict=CycleVerdict(True, 1))
    cycle = cycle.transition(CycleAction.REQUEST_HUMAN_REVIEW)
    assert cycle.state == PlanCycleState.HUMAN_REVIEW
    # Direct domain actions can accept this reference; the public decision
    # operation adds the graph-owner preflight modeled by step(human_accept).
    internal_accept = cycle.transition(CycleAction.HUMAN_ACCEPTED)
    assert internal_accept.accepted_product_ref == IMPORTED
    cycle = cycle.transition(CycleAction.HUMAN_EDITED)
    assert cycle.state == PlanCycleState.REPAIR_READY and cycle.generation == 2
    assert cycle.product_ref == cycle.accepted_product_ref == ""
    assert cycle.last_verdict is None
    cycle = cycle.transition(CycleAction.START_PRODUCER, assignment=CycleAssignment(
        CycleSlot.PRODUCER, AssignmentKind.REVISION, 2, "edited requirements"))
    cycle = cycle.transition(CycleAction.PRODUCER_SUBMITTED, product_ref=FRESH)
    cycle = cycle.transition(CycleAction.START_CHECKER, assignment=CycleAssignment(
        CycleSlot.CHECKER, AssignmentKind.INITIAL, 2, FRESH))
    cycle = cycle.transition(CycleAction.CHECKER_ACCEPTED, verdict=CycleVerdict(True, 2))
    cycle = cycle.transition(CycleAction.REQUEST_HUMAN_REVIEW)
    cycle = cycle.transition(CycleAction.HUMAN_ACCEPTED)
    assert cycle.generation == 2 and cycle.accepted_product_ref == FRESH
    assert graph_generation(FRESH) == 1  # GraphIR lineage is not PlanCycle generation.


def test_imported_product_does_not_relax_generic_checker_admission():
    assignment = CycleAssignment(CycleSlot.CHECKER, AssignmentKind.INITIAL, 1, IMPORTED)
    for cycle in (PlanCycle("plan"), NodeCycle("graph:node", "node",
                                            state=NodeCycleState.PRODUCER_READY)):
        with pytest.raises(CycleTransitionError):
            cycle.transition(CycleAction.START_CHECKER, assignment=assignment)
    with pytest.raises(CycleTransitionError):
        NodeCycle("graph:node", "node", state=NodeCycleState.PRODUCER_READY).transition(
            CycleAction.IMPORT_PRODUCT, product_ref=IMPORTED)


@pytest.mark.parametrize("changes,arguments", [
    ({}, {"product_ref": ""}),
    ({"generation": 2}, {}),
    ({"state": PlanCycleState.REPAIR_READY}, {}),
    ({"product_ref": "old-product"}, {}),
    ({"accepted_product_ref": "old-accepted"}, {}),
    ({"last_verdict": CycleVerdict(True, 1)}, {}),
    ({"active_assignment": CycleAssignment(
        CycleSlot.PRODUCER, AssignmentKind.INITIAL, 1, "producer")}, {}),
    ({"resume_state": PlanCycleState.PRODUCER_READY}, {}),
    ({}, {"assignment": CycleAssignment(
        CycleSlot.PRODUCER, AssignmentKind.INITIAL, 1, "producer")}),
    ({}, {"verdict": CycleVerdict(True, 1)}),
])
def test_real_import_requires_pristine_plan_and_no_synthetic_receipts(changes, arguments):
    cycle = replace(PlanCycle("plan"), **changes)
    with pytest.raises(CycleTransitionError):
        cycle.transition(CycleAction.IMPORT_PRODUCT, **{"product_ref": IMPORTED, **arguments})


def test_imported_model_config_wires_refinement_and_invariants():
    """Wiring check only; it does not parse TLA+ or claim TLC execution."""
    config = (SPEC_ROOT / "ImportedPlanLifecycle.cfg").read_text()
    assert 'CONSTANT UnsafeMode = "none"' in config
    assert "PROPERTY RefinesProduceCheckCycle\n" in config
    for name in (
        "TypeOK", "NewImportIsAtomic", "RecoveryRequiresExactEvidence",
        "ImportEventAndBindingCommitTogether", "NoSyntheticProducer",
        "ReviewerRequiresBoundProduct", "ReviewAndAcceptanceAreCurrent", "ExactlyOneRunningSlot",
        "RevisionPointersStayAtomic", "PublicAcceptanceRequiresCurrentGraph",
        "InstalledGraphBelongsToWorkflow", "FreshGraphHasIndependentGeneration",
    ):
        assert f"INVARIANT {name}\n" in config
    script = (ROOT / "scripts" / "check_bunshin_v2_tla.sh").read_text()
    assert "    ImportedPlanLifecycle\n" in script


@pytest.mark.parametrize("mutant,invariant", [
    ("none", None),
    ("split_import", "NewImportIsAtomic"),
    ("unproven_recovery", "RecoveryRequiresExactEvidence"),
    ("foreign_accept", "PublicAcceptanceRequiresCurrentGraph"),
    ("foreign_install", "InstalledGraphBelongsToWorkflow"),
])
def test_imported_plan_tlc(tmp_path, mutant, invariant):
    jar_value = os.environ.get("TLA2TOOLS_JAR")
    if not jar_value:
        pytest.skip("TLC not run: set TLA2TOOLS_JAR to a pinned tla2tools.jar")
    jar = Path(jar_value).expanduser().resolve()
    assert jar.is_file(), f"TLA2TOOLS_JAR does not exist: {jar}"
    java = shutil.which("java")
    assert java is not None, "TLA2TOOLS_JAR is set but Java is unavailable"
    for module in ("ImportedPlanLifecycle", "ProduceCheckCycle"):
        shutil.copyfile(SPEC_ROOT / f"{module}.tla", tmp_path / f"{module}.tla")
    config = (SPEC_ROOT / "ImportedPlanLifecycle.cfg").read_text()
    config = config.replace('UnsafeMode = "none"', f'UnsafeMode = "{mutant}"')
    (tmp_path / "ImportedPlanLifecycle.cfg").write_text(config)
    result = subprocess.run(
        [java, "-XX:+UseParallelGC", "-jar", str(jar), "-workers", "1", "-cleanup",
         "-config", "ImportedPlanLifecycle.cfg", "ImportedPlanLifecycle.tla"],
        cwd=tmp_path, capture_output=True, text=True, timeout=180, check=False,
    )
    output = result.stdout + result.stderr
    if invariant:
        assert result.returncode != 0, output
        assert f"Invariant {invariant} is violated" in output
    else:
        assert result.returncode == 0, output
        assert "Model checking completed. No error has been found." in output
