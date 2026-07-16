-------------------------- MODULE ModuleLifecycle --------------------------
EXTENDS Naturals, TLC

CONSTANT MaxCandidate

NodeStates == {
    "Ready", "Executing", "Quiescing", "Snapshotting", "VerifyReady",
    "Verifying", "RevisionReady", "Accepted", "Paused", "Frozen",
    "Cancelled", "Triage"
}

RoleStates == {
    "Dormant", "Ready", "Running", "AwaitingSettlement", "Suspended",
    "Frozen", "Done"
}

ActiveRoles == {"None", "Coder", "Verifier"}
ReceiptStates == {"None", "Recorded", "Settled"}
ResultKinds == {"None", "Candidate", "Pass", "Fail", "WorkerFailure"}
ControlStates == {"Run", "Pause", "Freeze", "Cancel"}
ResumeStates == {"Ready", "Quiescing", "Snapshotting", "VerifyReady", "RevisionReady"}
ExecutionModes == {"Initial", "Revision"}

VARIABLES
    nodeState,
    coderState,
    verifierState,
    activeRole,
    writerLease,
    candidateVersion,
    verifiedVersion,
    receiptState,
    resultKind,
    desiredControl,
    pauseResume,
    triageResume,
    executionMode,
    managerUp

vars == <<
    nodeState, coderState, verifierState, activeRole, writerLease,
    candidateVersion, verifiedVersion, receiptState, resultKind,
    desiredControl, pauseResume, triageResume, executionMode, managerUp
>>

TerminalStates == {"Accepted", "Cancelled"}

RetryTarget(state, mode) ==
    CASE state = "Executing" -> IF mode = "Revision" THEN "RevisionReady" ELSE "Ready"
      [] state = "Verifying" -> "VerifyReady"
      [] state = "Quiescing" -> "Quiescing"
      [] state = "Snapshotting" -> "Snapshotting"
      [] state = "VerifyReady" -> "VerifyReady"
      [] state = "RevisionReady" -> "RevisionReady"
      [] OTHER -> "Ready"

Init ==
    /\ nodeState = "Ready"
    /\ coderState = "Ready"
    /\ verifierState = "Dormant"
    /\ activeRole = "None"
    /\ writerLease = FALSE
    /\ candidateVersion = 0
    /\ verifiedVersion = 0
    /\ receiptState = "None"
    /\ resultKind = "None"
    /\ desiredControl = "Run"
    /\ pauseResume = "Ready"
    /\ triageResume = "Ready"
    /\ executionMode = "Initial"
    /\ managerUp = TRUE

StartCoder ==
    /\ managerUp
    /\ desiredControl = "Run"
    /\ nodeState \in {"Ready", "RevisionReady"}
    /\ activeRole = "None"
    /\ receiptState \in {"None", "Settled"}
    /\ nodeState' = "Executing"
    /\ coderState' = "Running"
    /\ verifierState' = verifierState
    /\ activeRole' = "Coder"
    /\ writerLease' = TRUE
    /\ executionMode' = IF nodeState = "RevisionReady" THEN "Revision" ELSE "Initial"
    /\ receiptState' = "None"
    /\ resultKind' = "None"
    /\ UNCHANGED <<candidateVersion, verifiedVersion, desiredControl, pauseResume, triageResume, managerUp>>

RecordCandidateReceipt ==
    /\ managerUp
    /\ desiredControl = "Run"
    /\ nodeState = "Executing"
    /\ activeRole = "Coder"
    /\ coderState = "Running"
    /\ writerLease
    /\ receiptState = "None"
    /\ candidateVersion < MaxCandidate
    /\ coderState' = "AwaitingSettlement"
    /\ activeRole' = "None"
    /\ writerLease' = FALSE
    /\ receiptState' = "Recorded"
    /\ resultKind' = "Candidate"
    /\ UNCHANGED <<nodeState, verifierState, candidateVersion, verifiedVersion, desiredControl, pauseResume, triageResume, executionMode, managerUp>>

SettleCandidateReceipt ==
    /\ managerUp
    /\ desiredControl \in {"Run", "Pause"}
    /\ nodeState = "Executing"
    /\ coderState = "AwaitingSettlement"
    /\ receiptState = "Recorded"
    /\ resultKind = "Candidate"
    /\ nodeState' = "Quiescing"
    /\ receiptState' = "Settled"
    /\ UNCHANGED <<coderState, verifierState, activeRole, writerLease, candidateVersion, verifiedVersion, resultKind, desiredControl, pauseResume, triageResume, executionMode, managerUp>>

QuiesceCandidate ==
    /\ managerUp
    /\ desiredControl = "Run"
    /\ nodeState = "Quiescing"
    /\ receiptState = "Settled"
    /\ ~writerLease
    /\ nodeState' = "Snapshotting"
    /\ UNCHANGED <<coderState, verifierState, activeRole, writerLease, candidateVersion, verifiedVersion, receiptState, resultKind, desiredControl, pauseResume, triageResume, executionMode, managerUp>>

SnapshotCandidate ==
    /\ managerUp
    /\ desiredControl = "Run"
    /\ nodeState = "Snapshotting"
    /\ receiptState = "Settled"
    /\ candidateVersion < MaxCandidate
    /\ nodeState' = "VerifyReady"
    /\ coderState' = "Suspended"
    /\ verifierState' = "Ready"
    /\ candidateVersion' = candidateVersion + 1
    /\ receiptState' = "None"
    /\ resultKind' = "None"
    /\ UNCHANGED <<activeRole, writerLease, verifiedVersion, desiredControl, pauseResume, triageResume, executionMode, managerUp>>

StartVerifier ==
    /\ managerUp
    /\ desiredControl = "Run"
    /\ nodeState = "VerifyReady"
    /\ verifierState = "Ready"
    /\ activeRole = "None"
    /\ receiptState = "None"
    /\ nodeState' = "Verifying"
    /\ verifierState' = "Running"
    /\ activeRole' = "Verifier"
    /\ UNCHANGED <<coderState, writerLease, candidateVersion, verifiedVersion, receiptState, resultKind, desiredControl, pauseResume, triageResume, executionMode, managerUp>>

RecordVerifierResult(kind) ==
    /\ kind \in {"Pass", "Fail"}
    /\ managerUp
    /\ desiredControl = "Run"
    /\ nodeState = "Verifying"
    /\ verifierState = "Running"
    /\ activeRole = "Verifier"
    /\ receiptState = "None"
    /\ verifierState' = "AwaitingSettlement"
    /\ activeRole' = "None"
    /\ receiptState' = "Recorded"
    /\ resultKind' = kind
    /\ UNCHANGED <<nodeState, coderState, writerLease, candidateVersion, verifiedVersion, desiredControl, pauseResume, triageResume, executionMode, managerUp>>

SettleVerifierPass ==
    /\ managerUp
    /\ desiredControl \in {"Run", "Pause"}
    /\ nodeState = "Verifying"
    /\ verifierState = "AwaitingSettlement"
    /\ receiptState = "Recorded"
    /\ resultKind = "Pass"
    /\ candidateVersion > 0
    /\ nodeState' = "Accepted"
    /\ coderState' = "Done"
    /\ verifierState' = "Done"
    /\ receiptState' = "Settled"
    /\ verifiedVersion' = candidateVersion
    /\ UNCHANGED <<activeRole, writerLease, candidateVersion, resultKind, desiredControl, pauseResume, triageResume, executionMode, managerUp>>

SettleVerifierFail ==
    /\ managerUp
    /\ desiredControl \in {"Run", "Pause"}
    /\ nodeState = "Verifying"
    /\ verifierState = "AwaitingSettlement"
    /\ receiptState = "Recorded"
    /\ resultKind = "Fail"
    /\ nodeState' = "RevisionReady"
    /\ coderState' = "Ready"
    /\ verifierState' = "Suspended"
    /\ receiptState' = "Settled"
    /\ UNCHANGED <<activeRole, writerLease, candidateVersion, verifiedVersion, resultKind, desiredControl, pauseResume, triageResume, executionMode, managerUp>>

RecordWorkerFailure ==
    /\ managerUp
    /\ desiredControl = "Run"
    /\ nodeState \in {"Executing", "Verifying"}
    /\ activeRole \in {"Coder", "Verifier"}
    /\ receiptState = "None"
    /\ receiptState' = "Recorded"
    /\ resultKind' = "WorkerFailure"
    /\ triageResume' = RetryTarget(nodeState, executionMode)
    /\ coderState' = IF activeRole = "Coder" THEN "AwaitingSettlement" ELSE coderState
    /\ verifierState' = IF activeRole = "Verifier" THEN "AwaitingSettlement" ELSE verifierState
    /\ activeRole' = "None"
    /\ writerLease' = FALSE
    /\ UNCHANGED <<nodeState, candidateVersion, verifiedVersion, desiredControl, pauseResume, executionMode, managerUp>>

SettleWorkerFailure ==
    /\ managerUp
    /\ desiredControl \in {"Run", "Pause"}
    /\ receiptState = "Recorded"
    /\ resultKind = "WorkerFailure"
    /\ nodeState \in {"Executing", "Verifying"}
    /\ nodeState' = "Triage"
    /\ receiptState' = "Settled"
    /\ coderState' = IF coderState = "AwaitingSettlement" THEN "Suspended" ELSE coderState
    /\ verifierState' = IF verifierState = "AwaitingSettlement" THEN "Suspended" ELSE verifierState
    /\ UNCHANGED <<activeRole, writerLease, candidateVersion, verifiedVersion, resultKind, desiredControl, pauseResume, triageResume, executionMode, managerUp>>

ResolveTriage ==
    /\ managerUp
    /\ desiredControl = "Run"
    /\ nodeState = "Triage"
    /\ receiptState = "Settled"
    /\ resultKind = "WorkerFailure"
    /\ nodeState' = triageResume
    /\ coderState' = IF triageResume \in {"Ready", "RevisionReady"} THEN "Ready" ELSE coderState
    /\ verifierState' = IF triageResume = "VerifyReady" THEN "Ready" ELSE verifierState
    /\ receiptState' = "None"
    /\ resultKind' = "None"
    /\ UNCHANGED <<activeRole, writerLease, candidateVersion, verifiedVersion, desiredControl, pauseResume, triageResume, executionMode, managerUp>>

RequestPause ==
    /\ desiredControl = "Run"
    /\ nodeState \notin TerminalStates \cup {"Paused", "Frozen"}
    /\ desiredControl' = "Pause"
    /\ pauseResume' = RetryTarget(nodeState, executionMode)
    /\ UNCHANGED <<nodeState, coderState, verifierState, activeRole, writerLease, candidateVersion, verifiedVersion, receiptState, resultKind, triageResume, executionMode, managerUp>>

PauseNode ==
    /\ managerUp
    /\ desiredControl = "Pause"
    /\ nodeState \notin TerminalStates \cup {"Paused", "Frozen"}
    /\ receiptState # "Recorded"
    /\ nodeState' = "Paused"
    /\ coderState' = IF coderState \in {"Done", "Dormant"} THEN coderState ELSE "Suspended"
    /\ verifierState' = IF verifierState \in {"Done", "Dormant"} THEN verifierState ELSE "Suspended"
    /\ activeRole' = "None"
    /\ writerLease' = FALSE
    /\ UNCHANGED <<candidateVersion, verifiedVersion, receiptState, resultKind, desiredControl, pauseResume, triageResume, executionMode, managerUp>>

ResumeNode ==
    /\ managerUp
    /\ desiredControl = "Pause"
    /\ nodeState = "Paused"
    /\ nodeState' = pauseResume
    /\ desiredControl' = "Run"
    /\ coderState' = IF pauseResume \in {"Ready", "RevisionReady"} THEN "Ready" ELSE coderState
    /\ verifierState' = IF pauseResume = "VerifyReady" THEN "Ready" ELSE verifierState
    /\ UNCHANGED <<activeRole, writerLease, candidateVersion, verifiedVersion, receiptState, resultKind, pauseResume, triageResume, executionMode, managerUp>>

RequestFreeze ==
    /\ desiredControl \in {"Run", "Pause"}
    /\ nodeState \notin TerminalStates \cup {"Frozen"}
    /\ desiredControl' = "Freeze"
    /\ UNCHANGED <<nodeState, coderState, verifierState, activeRole, writerLease, candidateVersion, verifiedVersion, receiptState, resultKind, pauseResume, triageResume, executionMode, managerUp>>

FreezeNode ==
    /\ managerUp
    /\ desiredControl = "Freeze"
    /\ nodeState \notin TerminalStates \cup {"Frozen"}
    /\ nodeState' = "Frozen"
    /\ coderState' = IF coderState = "Done" THEN "Done" ELSE "Frozen"
    /\ verifierState' = IF verifierState = "Done" THEN "Done" ELSE "Frozen"
    /\ activeRole' = "None"
    /\ writerLease' = FALSE
    /\ receiptState' = IF receiptState = "Recorded" THEN "Settled" ELSE receiptState
    /\ UNCHANGED <<candidateVersion, verifiedVersion, resultKind, desiredControl, pauseResume, triageResume, executionMode, managerUp>>

ApplyReplan ==
    /\ managerUp
    /\ desiredControl = "Freeze"
    /\ nodeState = "Frozen"
    /\ nodeState' = "Ready"
    /\ coderState' = "Ready"
    /\ verifierState' = "Dormant"
    /\ candidateVersion' = 0
    /\ verifiedVersion' = 0
    /\ receiptState' = "None"
    /\ resultKind' = "None"
    /\ desiredControl' = "Run"
    /\ executionMode' = "Initial"
    /\ UNCHANGED <<activeRole, writerLease, pauseResume, triageResume, managerUp>>

RequestCancel ==
    /\ desiredControl # "Cancel"
    /\ nodeState \notin TerminalStates
    /\ desiredControl' = "Cancel"
    /\ UNCHANGED <<nodeState, coderState, verifierState, activeRole, writerLease, candidateVersion, verifiedVersion, receiptState, resultKind, pauseResume, triageResume, executionMode, managerUp>>

CancelNode ==
    /\ managerUp
    /\ desiredControl = "Cancel"
    /\ nodeState \notin TerminalStates
    /\ nodeState' = "Cancelled"
    /\ coderState' = "Done"
    /\ verifierState' = "Done"
    /\ activeRole' = "None"
    /\ writerLease' = FALSE
    /\ receiptState' = IF receiptState = "Recorded" THEN "Settled" ELSE receiptState
    /\ UNCHANGED <<candidateVersion, verifiedVersion, resultKind, desiredControl, pauseResume, triageResume, executionMode, managerUp>>

CrashManager ==
    /\ managerUp
    /\ managerUp' = FALSE
    /\ UNCHANGED <<nodeState, coderState, verifierState, activeRole, writerLease, candidateVersion, verifiedVersion, receiptState, resultKind, desiredControl, pauseResume, triageResume, executionMode>>

RestartManager ==
    /\ ~managerUp
    /\ managerUp' = TRUE
    /\ UNCHANGED <<nodeState, coderState, verifierState, activeRole, writerLease, candidateVersion, verifiedVersion, receiptState, resultKind, desiredControl, pauseResume, triageResume, executionMode>>

SettleRecordedResult ==
    \/ SettleCandidateReceipt
    \/ SettleVerifierPass
    \/ SettleVerifierFail
    \/ SettleWorkerFailure

Next ==
    \/ StartCoder
    \/ RecordCandidateReceipt
    \/ SettleCandidateReceipt
    \/ QuiesceCandidate
    \/ SnapshotCandidate
    \/ StartVerifier
    \/ RecordVerifierResult("Pass")
    \/ RecordVerifierResult("Fail")
    \/ SettleVerifierPass
    \/ SettleVerifierFail
    \/ RecordWorkerFailure
    \/ SettleWorkerFailure
    \/ ResolveTriage
    \/ RequestPause
    \/ PauseNode
    \/ ResumeNode
    \/ RequestFreeze
    \/ FreezeNode
    \/ ApplyReplan
    \/ RequestCancel
    \/ CancelNode
    \/ CrashManager
    \/ RestartManager

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(RestartManager)
    /\ SF_vars(SettleRecordedResult)
    /\ SF_vars(FreezeNode)
    /\ SF_vars(CancelNode)

TypeOK ==
    /\ nodeState \in NodeStates
    /\ coderState \in RoleStates
    /\ verifierState \in RoleStates
    /\ activeRole \in ActiveRoles
    /\ writerLease \in BOOLEAN
    /\ candidateVersion \in 0..MaxCandidate
    /\ verifiedVersion \in 0..MaxCandidate
    /\ receiptState \in ReceiptStates
    /\ resultKind \in ResultKinds
    /\ desiredControl \in ControlStates
    /\ pauseResume \in ResumeStates
    /\ triageResume \in ResumeStates
    /\ executionMode \in ExecutionModes
    /\ managerUp \in BOOLEAN

SingleRunnableRole ==
    /\ (activeRole = "Coder") => (coderState = "Running" /\ verifierState # "Running")
    /\ (activeRole = "Verifier") => (verifierState = "Running" /\ coderState # "Running")
    /\ (activeRole = "None") => ~(coderState = "Running" \/ verifierState = "Running")

WriterLeaseSafety ==
    writerLease =>
        /\ nodeState = "Executing"
        /\ activeRole = "Coder"
        /\ coderState = "Running"

RecordedResultStopsRole ==
    receiptState = "Recorded" =>
        /\ activeRole = "None"
        /\ ~writerLease
        /\ (coderState = "AwaitingSettlement" \/ verifierState = "AwaitingSettlement")

AcceptedWasVerified ==
    nodeState = "Accepted" =>
        /\ candidateVersion > 0
        /\ verifiedVersion = candidateVersion
        /\ receiptState = "Settled"
        /\ resultKind = "Pass"

TerminalClosesSessions ==
    nodeState \in TerminalStates =>
        /\ coderState = "Done"
        /\ verifierState = "Done"
        /\ activeRole = "None"
        /\ ~writerLease

TriageHasSettledFailure ==
    nodeState = "Triage" =>
        /\ receiptState = "Settled"
        /\ resultKind = "WorkerFailure"
        /\ activeRole = "None"
        /\ ~writerLease

RecordedResultEventuallySettles ==
    receiptState = "Recorded" ~> receiptState = "Settled"

CancelEventuallyCloses ==
    desiredControl = "Cancel" ~> nodeState = "Cancelled"

=============================================================================
