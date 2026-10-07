---------------- MODULE DependencyRepairControlOverlay ----------------
(*
Bounded control overlay for one persisted source intent and one frozen peer.
This models control/cleanup cursors, not a new worker scheduler or admission.
The fixed member uses a checker slot; exact slot/fingerprint equality is the
control obligation. Actual producer/checker role restoration still needs runtime
integration witnesses. Pause summarizes either aggregate or workflow blocking.

  Model action                 Runtime correspondence
  Pause/CleanupOnly            _control_graph preserves pending_scope;
                               retire_repair_control joins exact tasks, cuts raw
                               receipts and releases leases without graph CLOSE
  RecoverableTriage/ResolveNode pending_repair_incarnation validates immutable
                               frontier; resolve_triage restores slot/fingerprint
  ExhaustApply                 failed apply effect escalates workflow triage after
                               all exact closures, without consuming route budget
  ResolveWorkflow/Reconcile    fresh reconcile_workflow obtains original immutable
                               REGISTER_DEPENDENCY_REPAIR authority; old failed
                               effect remains failed and is never revived
  CapturePeer                  CAPTURE_DEPENDENCY_REPAIR before terminal budget
                               check or peer JOIN; preserve report/corpus/history
  Apply                        validated source intents each append one failure;
                               applied packet replay appends nothing
  TerminalSource/terminal peer existing three-identical-failure no_progress guard;
                               ordinary resolve cannot waive a terminal receipt,
                               even if a later cleanup error masks its blocker

Source terminal failure is checked before REGISTER and creates no new cohort.
Captured-peer terminal failure preserves the existing fenced cohort and never
joins that peer's provider targets. PASS/local/correction peer captures retain
immutable evidence but do not consume dependency-route failure history.

Python tests exhaust this declared finite abstraction and intentional mutants.
They are not production tests or TLC results; a pinned existing jar is required
for optional TLC. Liveness here is only existential terminal reachability under
an available explicit public resume/resolve, not automatic resolution of triage.
*)
EXTENDS Naturals, Sequences, FiniteSets
CONSTANT Scenario
Members == {"source", "peer"}
SourceTerminal == Scenario = "terminal_source"
PeerTerminal == Scenario = "terminal_peer"
DependencyPeer == Scenario \in {"dependency", "changed_candidate", "changed_finding"}
OriginalIdentity == <<"finding-0", "candidate-0">>
PeerIdentity == IF Scenario = "changed_candidate" THEN <<"finding-0", "candidate-1">>
                ELSE IF Scenario = "changed_finding" THEN <<"finding-1", "candidate-0">>
                ELSE OriginalIdentity
InitialSourceHistory == IF SourceTerminal THEN <<OriginalIdentity, OriginalIdentity>> ELSE <<>>
InitialPeerHistory == IF PeerTerminal \/ Scenario \in {"changed_candidate", "changed_finding"}
                      THEN <<OriginalIdentity, OriginalIdentity>> ELSE <<>>
NoProgress(h) == IF Len(h) < 3 THEN FALSE
                 ELSE h[Len(h)] = h[Len(h)-1] /\ h[Len(h)-1] = h[Len(h)-2]
Frontier == <<"graph-1", "cycle-1", "effect-1", "checker", "input-fingerprint-1">>
Cursor == <<"checker", "input-fingerprint-1">>
SourceEvidence == {"source-report", "source-corpus", "source-history", "peer-raw-receipt"}
PeerEvidence == {"peer-report", "peer-corpus", "peer-history"}
VARIABLE s
vars == <<s>>
Init == s = [control |-> "Running", frontier |-> Frontier,
    cursor |-> IF SourceTerminal THEN <<>> ELSE Cursor,
    pending |-> ~SourceTerminal, fenced |-> TRUE,
    live |-> ~SourceTerminal, lease |-> ~SourceTerminal,
    submissionCut |-> SourceTerminal, cleanupDone |-> SourceTerminal,
    peerCaptured |-> FALSE, graphClosed |-> FALSE, applied |-> FALSE,
    intents |-> IF SourceTerminal THEN {} ELSE {"source"},
    evidence |-> SourceEvidence, sourceHistory |-> InitialSourceHistory,
    peerHistory |-> InitialPeerHistory, charges |-> [m \in Members |-> 0],
    terminal |-> "", blocker |-> "", controlClosed |-> FALSE, controlCursor |-> <<>>,
    pauseUsed |-> FALSE, triageUsed |-> FALSE, failureUsed |-> FALSE,
    oldEffect |-> "queued", reconcileDue |-> FALSE, authority |-> "original-register",
    registerRef |-> "immutable-register-ref", appliedAuthority |-> "",
    applyCount |-> 0, admissions |-> 0]
Pause == /\ s.control = "Running" /\ s.pending /\ ~s.pauseUsed
    /\ s' = [s EXCEPT !.control = "Paused", !.pauseUsed = TRUE,
                        !.controlClosed = s.graphClosed, !.controlCursor = s.cursor]
Resume == /\ s.control = "Paused"
    /\ s' = [s EXCEPT !.control = "Running"]
CleanupOnly == /\ s.pending /\ ~s.cleanupDone
    /\ s' = [s EXCEPT !.live = FALSE, !.lease = FALSE,
                        !.submissionCut = TRUE, !.cleanupDone = TRUE]
RecoverableTriage == /\ s.control = "Running" /\ s.pending /\ ~s.graphClosed /\ ~s.triageUsed
    /\ s' = [s EXCEPT !.control = "RecoverableTriage", !.cursor = <<>>,
                        !.triageUsed = TRUE, !.controlClosed = s.graphClosed]
ResolveNode == /\ s.control = "RecoverableTriage" /\ s.terminal = ""
    /\ s.frontier = Frontier /\ s.pending /\ ~s.graphClosed
    /\ s' = [s EXCEPT !.control = "Running", !.cursor = Cursor]
CapturePeer == /\ s.control = "Running" /\ s.pending /\ s.cleanupDone /\ ~s.peerCaptured
    /\ LET terminalPeer == (PeerTerminal \/ DependencyPeer) /\ NoProgress(Append(s.peerHistory, PeerIdentity)) IN
       s' = [s EXCEPT !.peerCaptured = TRUE, !.evidence = @ \cup PeerEvidence,
            !.intents = IF DependencyPeer THEN @ \cup {"peer"} ELSE @,
            !.control = IF terminalPeer THEN "Terminal" ELSE @,
            !.terminal = IF terminalPeer THEN "peer" ELSE @,
            !.blocker = IF terminalPeer THEN "no_progress" ELSE @,
            !.cursor = IF terminalPeer THEN <<>> ELSE @,
            !.peerHistory = IF terminalPeer THEN Append(@, PeerIdentity) ELSE @,
            !.controlClosed = IF terminalPeer THEN s.graphClosed ELSE @]
TerminalSource == /\ SourceTerminal /\ s.terminal = ""
    /\ NoProgress(Append(s.sourceHistory, OriginalIdentity))
    /\ s' = [s EXCEPT !.control = "Terminal", !.terminal = "source",
                        !.blocker = "no_progress", !.sourceHistory = Append(@, OriginalIdentity)]
MaskTerminalBlocker == /\ s.control = "Terminal" /\ s.blocker = "no_progress"
    /\ s' = [s EXCEPT !.blocker = "cleanup_error"]
CloseGraph == /\ s.control = "Running" /\ s.pending /\ s.cleanupDone
    /\ s.peerCaptured /\ ~s.graphClosed /\ s.cursor = Cursor /\ s.terminal = ""
    /\ s' = [s EXCEPT !.graphClosed = TRUE, !.cursor = <<>>]
ExhaustApply == /\ s.control = "Running" /\ s.pending /\ s.graphClosed /\ ~s.failureUsed
    /\ s' = [s EXCEPT !.control = "ApplyTriage", !.oldEffect = "failed",
                        !.failureUsed = TRUE, !.authority = "", !.controlClosed = TRUE]
ResolveWorkflow == /\ s.control = "ApplyTriage" /\ s.terminal = ""
    /\ s' = [s EXCEPT !.control = "Running", !.reconcileDue = TRUE]
ReconcileWorkflow == /\ s.control = "Running" /\ s.reconcileDue
    /\ s.registerRef = "immutable-register-ref"
    /\ s' = [s EXCEPT !.reconcileDue = FALSE, !.authority = "fresh-reconcile"]
Apply == /\ s.control = "Running" /\ s.pending /\ s.graphClosed /\ s.terminal = ""
    /\ s.authority \in {"original-register", "fresh-reconcile"}
    /\ s' = [s EXCEPT !.pending = FALSE, !.fenced = FALSE, !.applied = TRUE,
         !.applyCount = @ + 1, !.appliedAuthority = s.authority,
         !.oldEffect = IF s.oldEffect = "failed" THEN "failed" ELSE "completed",
         !.sourceHistory = Append(@, OriginalIdentity),
         !.peerHistory = IF "peer" \in s.intents THEN Append(@, PeerIdentity) ELSE @,
         !.charges = [m \in Members |-> IF m \in s.intents THEN s.charges[m] + 1 ELSE s.charges[m]]]
ReplayOrRejectedTerminalResolve == UNCHANGED vars
Next == Pause \/ Resume \/ CleanupOnly \/ RecoverableTriage \/ ResolveNode
     \/ CapturePeer \/ TerminalSource \/ MaskTerminalBlocker \/ CloseGraph
     \/ ExhaustApply \/ ResolveWorkflow \/ ReconcileWorkflow \/ Apply
     \/ ReplayOrRejectedTerminalResolve
Spec == Init /\ [][Next]_vars
TypeOK == /\ s.control \in {"Running", "Paused", "RecoverableTriage", "ApplyTriage", "Terminal"}
    /\ s.intents \subseteq Members /\ s.charges \in [Members -> 0..1]
    /\ Len(s.sourceHistory) \in 0..3 /\ Len(s.peerHistory) \in 0..3
    /\ s.applyCount \in 0..1 /\ s.terminal \in {"", "source", "peer"}
    /\ s.oldEffect \in {"queued", "failed", "completed"}
    /\ s.authority \in {"", "original-register", "fresh-reconcile"}
FrozenCursorPreserved == s.frontier = Frontier /\ s.cursor \in {<<>>, Cursor}
BlockedControlDoesNotCloseOrApply == s.control # "Running" =>
    s.graphClosed = s.controlClosed /\ ~s.applied
PauseKeepsLogicalCursor == s.control = "Paused" => s.cursor = s.controlCursor
CleanupIsNotAdmission == s.admissions = 0 /\
    (s.cleanupDone => ~s.live /\ ~s.lease /\ s.submissionCut)
EvidencePreserved == SourceEvidence \subseteq s.evidence /\
    (s.peerCaptured => PeerEvidence \subseteq s.evidence)
PendingOrTerminalStaysFenced == s.pending \/ s.terminal # "" => s.fenced
TerminalCannotBeWaived == s.terminal # "" => s.control = "Terminal" /\ ~s.applied
TerminalDoesNotRegisterProviders ==
    (s.terminal = "source" => s.intents = {} /\ ~s.pending) /\
    (s.terminal = "peer" => "peer" \notin s.intents /\ s.pending /\ s.peerCaptured)
FailedEffectRemainsFailed == s.failureUsed => s.oldEffect = "failed"
RecoveryUsesOriginalRegistration == s.registerRef = "immutable-register-ref" /\
    (s.applied /\ s.failureUsed => s.appliedAuthority = "fresh-reconcile")
ExactlyOneRouteCharge == \A m \in Members :
    s.charges[m] = IF s.applied /\ m \in s.intents THEN 1 ELSE 0
FailureHistoryAccounting ==
    /\ Len(s.sourceHistory) = Len(InitialSourceHistory) + s.charges["source"] + (IF s.terminal = "source" THEN 1 ELSE 0)
    /\ Len(s.peerHistory) = Len(InitialPeerHistory) + s.charges["peer"] + (IF s.terminal = "peer" THEN 1 ELSE 0)
    /\ (s.terminal = "source" => NoProgress(s.sourceHistory))
    /\ (s.terminal = "peer" => NoProgress(s.peerHistory))
=================================================================
