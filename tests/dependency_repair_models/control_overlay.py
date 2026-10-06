"""Finite control-overlay abstraction; neither production execution nor TLC."""
from dataclasses import dataclass, replace

from .refinements import explore


IDENTITY = ("finding-0", "candidate-0")
FRONTIER = ("graph-1", "cycle-1", "effect-1", "checker", "input-fingerprint-1")
CURSOR = ("checker", "input-fingerprint-1")
SOURCE_EVIDENCE = frozenset({"source-report", "source-corpus", "source-history", "peer-raw-receipt"})
PEER_EVIDENCE = frozenset({"peer-report", "peer-corpus", "peer-history"})
SCENARIOS = (
    "dependency", "pass", "local", "correction", "terminal_source",
    "terminal_peer", "changed_candidate", "changed_finding",
)


def no_progress(history):
    return len(history) >= 3 and history[-1] == history[-2] == history[-3]


@dataclass(frozen=True)
class Control:
    control: str = "Running"
    frontier: tuple = FRONTIER
    cursor: tuple = CURSOR
    pending: bool = True
    fenced: bool = True
    live: bool = True
    lease: bool = True
    submission_cut: bool = False
    cleanup_done: bool = False
    peer_captured: bool = False
    graph_closed: bool = False
    applied: bool = False
    intents: frozenset = frozenset({"source"})
    evidence: frozenset = SOURCE_EVIDENCE
    source_history: tuple = ()
    peer_history: tuple = ()
    charges: tuple = (0, 0)
    terminal: str = ""
    blocker: str = ""
    control_closed: bool = False
    control_cursor: tuple = ()
    pause_used: bool = False
    triage_used: bool = False
    failure_used: bool = False
    old_effect: str = "queued"
    reconcile_due: bool = False
    authority: str = "original-register"
    register_ref: str = "immutable-register-ref"
    applied_authority: str = ""
    apply_count: int = 0
    admissions: int = 0


def run(scenario="dependency", mutant=None):
    assert scenario in SCENARIOS
    source_terminal = scenario == "terminal_source"
    peer_terminal = scenario == "terminal_peer"
    dependency_peer = scenario in {"dependency", "changed_candidate", "changed_finding"}
    identity = (("finding-0", "candidate-1") if scenario == "changed_candidate" else
                ("finding-1", "candidate-0") if scenario == "changed_finding" else IDENTITY)
    initial_source = (IDENTITY, IDENTITY) if source_terminal else ()
    initial_peer = (IDENTITY, IDENTITY) if peer_terminal or scenario.startswith("changed_") else ()
    initial = Control(
        pending=not source_terminal, live=not source_terminal, lease=not source_terminal,
        submission_cut=source_terminal, cleanup_done=source_terminal,
        cursor=() if source_terminal else CURSOR,
        intents=frozenset() if source_terminal else frozenset({"source"}),
        source_history=initial_source, peer_history=initial_peer,
    )

    def step(s):
        if s.control == "Running" and s.pending and not s.pause_used:
            yield "PausePreservesFrontier", replace(
                s, control="Paused", pause_used=True, control_closed=s.graph_closed,
                control_cursor=s.cursor,
                frontier=() if mutant == "pause_drops_frontier" else s.frontier,
            )
        if s.control == "Paused":
            yield "ResumeOriginalCursor", replace(s, control="Running")
        if s.pending and not s.cleanup_done:
            if mutant != "cleanup_waits_for_graph_close" or s.graph_closed:
                yield "CleanupOnlyNoGraphClose", replace(
                    s, live=False, lease=False, submission_cut=True, cleanup_done=True,
                    graph_closed=True if mutant == "cleanup_closes_paused_graph" and s.control == "Paused" else s.graph_closed,
                    evidence=s.evidence - {"peer-raw-receipt"} if mutant == "cleanup_drops_raw_receipt" else s.evidence,
                )
        if s.control == "Running" and s.pending and not s.graph_closed and not s.triage_used:
            yield "RecoverableTriageClearsLogicalCursor", replace(
                s, control="RecoverableTriage", cursor=(), triage_used=True,
                control_closed=s.graph_closed,
            )
        if s.control == "RecoverableTriage" and not s.terminal and s.frontier == FRONTIER:
            yield "ResolveExactFrozenCursor", replace(
                s, control="Running", cursor=("checker", "new-input") if mutant == "resolve_rebinds_cursor" else
                       ("producer", CURSOR[1]) if mutant == "resolve_changes_slot" else CURSOR,
                admissions=s.admissions + (mutant == "resolve_starts_worker"),
                live=True if mutant == "resolve_starts_worker" else s.live,
                lease=True if mutant == "resolve_starts_worker" else s.lease,
            )
        if s.control == "Running" and s.pending and s.cleanup_done and not s.peer_captured:
            terminal = (peer_terminal or dependency_peer) and no_progress(s.peer_history + (identity,))
            if mutant == "no_progress_ignores_identity":
                terminal = len(s.peer_history + (identity,)) >= 3
            intents = s.intents | {"peer"} if dependency_peer else s.intents
            if terminal and mutant == "terminal_peer_joins_provider":
                intents = intents | {"peer"}
            yield "CapturePeerBeforeBudgetOrJoin", replace(
                s, peer_captured=True, evidence=s.evidence | PEER_EVIDENCE, intents=intents,
                control="Terminal" if terminal else s.control,
                terminal="peer" if terminal else s.terminal,
                blocker="no_progress" if terminal else s.blocker,
                cursor=() if terminal else s.cursor,
                peer_history=s.peer_history + (identity,) if terminal else s.peer_history,
                control_closed=s.graph_closed if terminal else s.control_closed,
                charges=(1, 0) if mutant == "charge_before_apply" else s.charges,
            )
        if source_terminal and not s.terminal:
            assert no_progress(s.source_history + (IDENTITY,))
            yield "TerminalSourceBeforeRegister", replace(
                s, control="Terminal", terminal="source", blocker="no_progress",
                source_history=s.source_history + (IDENTITY,),
            )
        if s.control == "Terminal" and s.blocker == "no_progress":
            yield "LaterCleanupErrorMasksVisibleBlocker", replace(s, blocker="cleanup_error")
        if mutant == "terminal_ordinary_resolve" and s.control == "Terminal" and s.blocker == "cleanup_error":
            yield "WaiveTerminalReceiptUnsafely", replace(s, control="Running")
        if s.control == "Running" and s.pending and s.cleanup_done and s.peer_captured and not s.graph_closed and s.cursor == CURSOR and not s.terminal:
            yield "CloseExactGraphIncarnation", replace(s, graph_closed=True, cursor=())
        if s.control == "Running" and s.pending and s.graph_closed and not s.failure_used:
            yield "ExhaustApplyEscalatesWorkflowTriage", replace(
                s, control="ApplyTriage", old_effect="failed", failure_used=True,
                authority="", control_closed=True,
            )
        if s.control == "ApplyTriage" and not s.terminal:
            yield "PublicWorkflowResolve", replace(s, control="Running", reconcile_due=True)
        if s.control == "Running" and s.reconcile_due:
            yield "FreshReconcileFromImmutableRegister", replace(
                s, reconcile_due=False,
                authority="original-register" if mutant == "recovery_reuses_failed_effect" else "fresh-reconcile",
                register_ref="current-mutable-packet" if mutant == "recovery_changes_authority" else s.register_ref,
                old_effect="queued" if mutant == "revive_failed_effect" else s.old_effect,
            )
        if s.control == "Running" and s.pending and s.graph_closed and not s.terminal and s.authority in {"original-register", "fresh-reconcile"}:
            peer_charge = "peer" in s.intents or mutant == "charge_invalidated_peer"
            yield "ApplyOnceAndChargeValidatedIntents", replace(
                s, pending=False, fenced=False, applied=True, apply_count=s.apply_count + 1,
                applied_authority=s.authority,
                old_effect="failed" if s.old_effect == "failed" else "completed",
                source_history=s.source_history + (IDENTITY,),
                peer_history=s.peer_history + (identity,) if peer_charge else s.peer_history,
                charges=(s.charges[0] + 1, s.charges[1] + peer_charge),
            )
        if mutant == "replay_double_charge" and s.applied:
            yield "ReplayChargesAgainUnsafely", replace(s, charges=(s.charges[0] + 1, s.charges[1]), source_history=s.source_history + (IDENTITY,))
        if mutant == "drop_terminal_evidence" and s.terminal:
            yield "DropCapturedCorpusUnsafely", replace(s, evidence=frozenset())
        yield "ReplayOrRejectedTerminalResolve", s

    def check(s):
        expected = (int(s.applied and "source" in s.intents), int(s.applied and "peer" in s.intents))
        checks = {
            "FrozenCursorPreserved": s.frontier == FRONTIER and s.cursor in {(), CURSOR},
            "BlockedControlDoesNotCloseOrApply": s.control == "Running" or s.graph_closed == s.control_closed and not s.applied,
            "PauseKeepsLogicalCursor": s.control != "Paused" or s.cursor == s.control_cursor,
            "CleanupIsNotAdmission": s.admissions == 0 and (not s.cleanup_done or not s.live and not s.lease and s.submission_cut),
            "EvidencePreserved": SOURCE_EVIDENCE <= s.evidence and (not s.peer_captured or PEER_EVIDENCE <= s.evidence),
            "PendingOrTerminalStaysFenced": not(s.pending or s.terminal) or s.fenced,
            "TerminalCannotBeWaived": not s.terminal or s.control == "Terminal" and not s.applied,
            "TerminalDoesNotRegisterProviders": (s.terminal != "source" or not s.intents and not s.pending) and (s.terminal != "peer" or "peer" not in s.intents and s.pending and s.peer_captured),
            "FailedEffectRemainsFailed": not s.failure_used or s.old_effect == "failed",
            "RecoveryUsesOriginalRegistration": s.register_ref == "immutable-register-ref" and (not(s.applied and s.failure_used) or s.applied_authority == "fresh-reconcile"),
            "ExactlyOneRouteCharge": s.charges == expected and s.apply_count <= 1,
            "FailureHistoryAccounting": len(s.source_history) == len(initial_source) + s.charges[0] + (s.terminal == "source") and len(s.peer_history) == len(initial_peer) + s.charges[1] + (s.terminal == "peer") and (s.terminal != "source" or no_progress(s.source_history)) and (s.terminal != "peer" or no_progress(s.peer_history)),
        }
        return [name for name, valid in checks.items() if not valid]

    def witnesses(s):
        return [name for name, valid in (
            ("pause_cleanup_preserves_frontier", s.control == "Paused" and s.cleanup_done and not s.graph_closed),
            ("recoverable_cursor_restored_without_worker", s.triage_used and s.control == "Running" and s.cursor == CURSOR and s.cleanup_done),
            ("exhausted_apply_recovers_once", s.applied and s.failure_used and s.old_effect == "failed"),
            ("terminal_peer_evidence_fenced", s.terminal == "peer" and s.pending and s.peer_captured),
            ("terminal_source_before_registration", s.terminal == "source" and not s.intents and not s.pending),
            ("changed_identity_is_not_no_progress", s.applied and scenario.startswith("changed_") and not no_progress(s.peer_history)),
        ) if valid]

    return explore(initial, step, check, lambda s: s.applied or bool(s.terminal), witnesses)
