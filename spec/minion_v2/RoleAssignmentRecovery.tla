-------------------- MODULE RoleAssignmentRecovery --------------------
EXTENDS Naturals, FiniteSets, TLC

CONSTANT OldAssignment, NewAssignment, MaxAttempt, MaxInputRevision, MaxScans

Assignments == {OldAssignment, NewAssignment}
NoAssignment == "NoAssignment"
AssignmentIds == Assignments \cup {NoAssignment}
AssignmentStates == {
    "Absent", "Queued", "Claimed", "Running", "RetryQueued",
    "ResultRecorded", "Settled", "Cancelled"
}
ActiveStates == {"Claimed", "Running"}
RecoverableStates == {
    "Queued", "Claimed", "Running", "RetryQueued", "ResultRecorded"
}

VARIABLES
    managerUp,
    assignmentState,
    assignmentLease,
    attemptNumber,
    effectAssignment,
    workerAssignment,
    inputRevision,
    promptRevision,
    receipts,
    terminalAssignment,
    settledAssignment,
    recoveryScans,
    lastScanObserved,
    lastScanOwner,
    legacyDuplicate

vars == <<
    managerUp, assignmentState, assignmentLease, attemptNumber,
    effectAssignment, workerAssignment, inputRevision, promptRevision,
    receipts, terminalAssignment, settledAssignment, recoveryScans,
    lastScanObserved, lastScanOwner, legacyDuplicate
>>

Init ==
    /\ managerUp = TRUE
    /\ assignmentState =
        [a \in Assignments |-> IF a = OldAssignment THEN "Queued" ELSE "Absent"]
    /\ assignmentLease = [a \in Assignments |-> FALSE]
    /\ attemptNumber = [a \in Assignments |-> 0]
    /\ effectAssignment = OldAssignment
    /\ workerAssignment = NoAssignment
    /\ inputRevision = 0
    /\ promptRevision =
        [a \in Assignments |-> IF a = OldAssignment THEN 0 ELSE MaxInputRevision]
    /\ receipts = {}
    /\ terminalAssignment = NoAssignment
    /\ settledAssignment = NoAssignment
    /\ recoveryScans = 0
    /\ lastScanObserved = NoAssignment
    /\ lastScanOwner = NoAssignment
    /\ legacyDuplicate = FALSE

ClaimAssignment(a) ==
    /\ managerUp
    /\ workerAssignment = NoAssignment
    /\ a = effectAssignment
    /\ assignmentState[a] \in {"Queued", "RetryQueued"}
    /\ attemptNumber[a] < MaxAttempt
    /\ assignmentState' = [assignmentState EXCEPT ![a] = "Claimed"]
    /\ assignmentLease' = [assignmentLease EXCEPT ![a] = TRUE]
    /\ attemptNumber' = [attemptNumber EXCEPT ![a] = @ + 1]
    /\ workerAssignment' = a
    /\ UNCHANGED <<managerUp, effectAssignment, inputRevision, promptRevision,
        receipts, terminalAssignment, settledAssignment, recoveryScans,
        lastScanObserved, lastScanOwner, legacyDuplicate>>

StartAssignment(a) ==
    /\ managerUp
    /\ workerAssignment = a
    /\ effectAssignment = a
    /\ assignmentState[a] = "Claimed"
    /\ assignmentLease[a]
    /\ assignmentState' = [assignmentState EXCEPT ![a] = "Running"]
    /\ UNCHANGED <<managerUp, assignmentLease, attemptNumber,
        effectAssignment, workerAssignment, inputRevision, promptRevision,
        receipts, terminalAssignment, settledAssignment, recoveryScans,
        lastScanObserved, lastScanOwner, legacyDuplicate>>

ExpireWorkerLease ==
    /\ workerAssignment \in Assignments
    /\ assignmentState[workerAssignment] \in ActiveStates
    /\ assignmentLease[workerAssignment]
    /\ assignmentLease' =
        [assignmentLease EXCEPT ![workerAssignment] = FALSE]
    /\ workerAssignment' = NoAssignment
    /\ UNCHANGED <<managerUp, assignmentState, attemptNumber,
        effectAssignment, inputRevision, promptRevision, receipts,
        terminalAssignment, settledAssignment, recoveryScans,
        lastScanObserved, lastScanOwner, legacyDuplicate>>

RecompileInputs ==
    /\ inputRevision < MaxInputRevision
    /\ inputRevision' = inputRevision + 1
    \* Recompiling attempt-local workspace inputs or republishing a
    \* Manager-owned progress journal must not allocate or bind another
    \* durable assignment, and must not replace a submission receipt.
    /\ UNCHANGED <<managerUp, assignmentState, assignmentLease,
        attemptNumber, effectAssignment, workerAssignment, promptRevision,
        receipts, terminalAssignment, settledAssignment, recoveryScans,
        lastScanObserved, lastScanOwner, legacyDuplicate>>

RecoverExpiredAssignment ==
    /\ managerUp
    /\ workerAssignment = NoAssignment
    /\ effectAssignment \in Assignments
    /\ assignmentState[effectAssignment] \in ActiveStates
    /\ ~assignmentLease[effectAssignment]
    /\ assignmentState' =
        [assignmentState EXCEPT ![effectAssignment] = "RetryQueued"]
    \* Recovery reuses both the durable identity and its original prompt.
    /\ UNCHANGED <<managerUp, assignmentLease, attemptNumber,
        effectAssignment, workerAssignment, inputRevision, promptRevision,
        receipts, terminalAssignment, settledAssignment, recoveryScans,
        lastScanObserved, lastScanOwner, legacyDuplicate>>

RecoveryScan(observed) ==
    /\ managerUp
    /\ workerAssignment \in Assignments
    /\ observed \in Assignments
    /\ assignmentState[observed] \in RecoverableStates
    /\ recoveryScans < MaxScans
    /\ recoveryScans' = recoveryScans + 1
    /\ lastScanObserved' = observed
    /\ lastScanOwner' = effectAssignment
    \* A periodic scan is observational while a worker owns the effect.
    /\ UNCHANGED <<managerUp, assignmentState, assignmentLease,
        attemptNumber, effectAssignment, workerAssignment, inputRevision,
        promptRevision, receipts, terminalAssignment, settledAssignment,
        legacyDuplicate>>

RecordResult(a) ==
    /\ managerUp
    /\ workerAssignment = a
    /\ effectAssignment = a
    /\ assignmentState[a] = "Running"
    /\ assignmentLease[a]
    /\ assignmentState' = [assignmentState EXCEPT ![a] = "ResultRecorded"]
    /\ assignmentLease' = [assignmentLease EXCEPT ![a] = FALSE]
    /\ workerAssignment' = NoAssignment
    /\ receipts' = receipts \cup {a}
    /\ terminalAssignment' = a
    /\ UNCHANGED <<managerUp, attemptNumber, effectAssignment,
        inputRevision, promptRevision, settledAssignment, recoveryScans,
        lastScanObserved, lastScanOwner, legacyDuplicate>>

SettleTerminalResult ==
    /\ managerUp
    /\ terminalAssignment \in receipts
    /\ assignmentState[terminalAssignment] = "ResultRecorded"
    /\ assignmentState' =
        [assignmentState EXCEPT ![terminalAssignment] = "Settled"]
    /\ settledAssignment' = terminalAssignment
    \* Settlement follows the immutable terminal identity, not a mutable scan.
    /\ UNCHANGED <<managerUp, assignmentLease, attemptNumber,
        effectAssignment, workerAssignment, inputRevision, promptRevision,
        receipts, terminalAssignment, recoveryScans, lastScanObserved,
        lastScanOwner, legacyDuplicate>>

CrashManager ==
    /\ managerUp
    /\ managerUp' = FALSE
    /\ assignmentLease' =
        [a \in Assignments |-> IF a = workerAssignment THEN FALSE
                              ELSE assignmentLease[a]]
    /\ workerAssignment' = NoAssignment
    /\ UNCHANGED <<assignmentState, attemptNumber, effectAssignment,
        inputRevision, promptRevision, receipts, terminalAssignment,
        settledAssignment, recoveryScans, lastScanObserved, lastScanOwner,
        legacyDuplicate>>

RestartManager ==
    /\ ~managerUp
    /\ managerUp' = TRUE
    /\ UNCHANGED <<assignmentState, assignmentLease, attemptNumber,
        effectAssignment, workerAssignment, inputRevision, promptRevision,
        receipts, terminalAssignment, settledAssignment, recoveryScans,
        lastScanObserved, lastScanOwner, legacyDuplicate>>

InjectLegacyDuplicate ==
    \* This fault state represents a database produced by the old bug. The
    \* fixed recovery scan must contain it without stealing the active owner.
    /\ ~legacyDuplicate
    /\ managerUp
    /\ workerAssignment = NoAssignment
    /\ effectAssignment = OldAssignment
    /\ assignmentState[OldAssignment] \in ActiveStates
    /\ ~assignmentLease[OldAssignment]
    /\ attemptNumber[NewAssignment] < MaxAttempt
    /\ assignmentState' =
        [assignmentState EXCEPT ![NewAssignment] = "Running"]
    /\ assignmentLease' =
        [assignmentLease EXCEPT ![NewAssignment] = TRUE]
    /\ attemptNumber' =
        [attemptNumber EXCEPT ![NewAssignment] = @ + 1]
    /\ effectAssignment' = NewAssignment
    /\ workerAssignment' = NewAssignment
    /\ promptRevision' =
        [promptRevision EXCEPT ![NewAssignment] = inputRevision]
    /\ legacyDuplicate' = TRUE
    /\ UNCHANGED <<managerUp, inputRevision, receipts, terminalAssignment,
        settledAssignment, recoveryScans, lastScanObserved, lastScanOwner>>

ClaimSomeAssignment ==
    \E a \in Assignments : ClaimAssignment(a)

StartSomeAssignment ==
    \E a \in Assignments : StartAssignment(a)

ScanSomeAssignment ==
    \E a \in Assignments : RecoveryScan(a)

RecordSomeResult ==
    \E a \in Assignments : RecordResult(a)

Next ==
    \/ ClaimSomeAssignment
    \/ StartSomeAssignment
    \/ ExpireWorkerLease
    \/ RecompileInputs
    \/ RecoverExpiredAssignment
    \/ ScanSomeAssignment
    \/ RecordSomeResult
    \/ SettleTerminalResult
    \/ CrashManager
    \/ RestartManager
    \/ InjectLegacyDuplicate

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(RestartManager)
    /\ SF_vars(RecoverExpiredAssignment)
    /\ SF_vars(SettleTerminalResult)

TypeOK ==
    /\ managerUp \in BOOLEAN
    /\ assignmentState \in [Assignments -> AssignmentStates]
    /\ assignmentLease \in [Assignments -> BOOLEAN]
    /\ attemptNumber \in [Assignments -> 0..MaxAttempt]
    /\ effectAssignment \in AssignmentIds
    /\ workerAssignment \in AssignmentIds
    /\ inputRevision \in 0..MaxInputRevision
    /\ promptRevision \in [Assignments -> 0..MaxInputRevision]
    /\ receipts \subseteq Assignments
    /\ terminalAssignment \in AssignmentIds
    /\ settledAssignment \in AssignmentIds
    /\ recoveryScans \in 0..MaxScans
    /\ lastScanObserved \in AssignmentIds
    /\ lastScanOwner \in AssignmentIds
    /\ legacyDuplicate \in BOOLEAN

SingleLiveAssignmentOwner ==
    /\ Cardinality({a \in Assignments : assignmentLease[a]}) <= 1
    /\ \A a \in Assignments :
        assignmentLease[a] =>
            /\ workerAssignment = a
            /\ effectAssignment = a
            /\ assignmentState[a] \in ActiveStates

WorkerOwnsEffectProjection ==
    workerAssignment \in Assignments =>
        /\ effectAssignment = workerAssignment
        /\ assignmentLease[workerAssignment]

RecoveryScanPreservesWorkerOwner ==
    /\ lastScanObserved \in Assignments
    /\ workerAssignment \in Assignments
    /\ lastScanOwner = effectAssignment
    =>
    lastScanOwner = workerAssignment

InputRecompileDoesNotCreateAssignment ==
    ~legacyDuplicate =>
        /\ assignmentState[OldAssignment] # "Absent"
        /\ assignmentState[NewAssignment] = "Absent"
        /\ effectAssignment = OldAssignment
        /\ promptRevision[OldAssignment] = 0

NonsemanticRefreshPreservesReceiptOwner ==
    ~legacyDuplicate /\ OldAssignment \in receipts =>
        /\ terminalAssignment = OldAssignment
        /\ effectAssignment = OldAssignment

TerminalOwnsReceipt ==
    terminalAssignment \in Assignments =>
        terminalAssignment \in receipts

SettlementUsesTerminalIdentity ==
    settledAssignment \in Assignments =>
        /\ settledAssignment = terminalAssignment
        /\ settledAssignment \in receipts
        /\ assignmentState[settledAssignment] = "Settled"

ExpiredOwnerEventuallyChanges ==
    /\ effectAssignment \in Assignments
    /\ workerAssignment = NoAssignment
    /\ assignmentState[effectAssignment] \in ActiveStates
    /\ ~assignmentLease[effectAssignment]
    ~>
    ~(
        /\ effectAssignment \in Assignments
        /\ workerAssignment = NoAssignment
        /\ assignmentState[effectAssignment] \in ActiveStates
        /\ ~assignmentLease[effectAssignment]
    )

RecordedResultEventuallySettles ==
    terminalAssignment \in Assignments /\ settledAssignment = NoAssignment
    ~>
    settledAssignment = terminalAssignment

=============================================================================
