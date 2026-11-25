-------------------- MODULE RoleAssignmentRecovery --------------------
EXTENDS Naturals, FiniteSets, TLC

CONSTANT OldAssignment, NewAssignment, MaxAttempt, MaxStarts, MaxInputRevision, MaxScans

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
    failureCount,
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
    legacyDuplicate,
    failureReceipts,
    settlementKind,
    control

vars == <<
    managerUp, assignmentState, assignmentLease, attemptNumber, failureCount,
    effectAssignment, workerAssignment, inputRevision, promptRevision,
    receipts, terminalAssignment, settledAssignment, recoveryScans,
    lastScanObserved, lastScanOwner, legacyDuplicate, failureReceipts, settlementKind, control
>>

Init ==
    /\ managerUp = TRUE
    /\ assignmentState =
        [a \in Assignments |-> IF a = OldAssignment THEN "Queued" ELSE "Absent"]
    /\ assignmentLease = [a \in Assignments |-> FALSE]
    /\ attemptNumber = [a \in Assignments |-> 0]
    /\ failureCount = [a \in Assignments |-> 0]
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
    /\ failureReceipts = {}
    /\ settlementKind = "None"
    /\ control = "Run"

ClaimAssignment(a) ==
    /\ control = "Run"
    /\ managerUp
    /\ workerAssignment = NoAssignment
    /\ a = effectAssignment
    /\ assignmentState[a] \in {"Queued", "RetryQueued"}
    \* A process shell may be rebound more often than the logical role may
    \* fail. Only charged failures consume the retry budget.
    /\ attemptNumber[a] < MaxStarts
    /\ failureCount[a] < MaxAttempt
    /\ assignmentState' = [assignmentState EXCEPT ![a] = "Claimed"]
    /\ assignmentLease' = [assignmentLease EXCEPT ![a] = TRUE]
    /\ attemptNumber' = [attemptNumber EXCEPT ![a] = @ + 1]
    /\ workerAssignment' = a
    /\ UNCHANGED <<managerUp, failureCount, effectAssignment, inputRevision, promptRevision,
        receipts, terminalAssignment, settledAssignment, recoveryScans,
        lastScanObserved, lastScanOwner, legacyDuplicate, failureReceipts, settlementKind, control>>

StartAssignment(a) ==
    /\ control = "Run"
    /\ managerUp
    /\ workerAssignment = a
    /\ effectAssignment = a
    /\ assignmentState[a] = "Claimed"
    /\ assignmentLease[a]
    /\ assignmentState' = [assignmentState EXCEPT ![a] = "Running"]
    /\ UNCHANGED <<managerUp, assignmentLease, attemptNumber, failureCount,
        effectAssignment, workerAssignment, inputRevision, promptRevision,
        receipts, terminalAssignment, settledAssignment, recoveryScans,
        lastScanObserved, lastScanOwner, legacyDuplicate, failureReceipts, settlementKind, control>>

ExpireWorkerLease ==
    /\ workerAssignment \in Assignments
    /\ assignmentState[workerAssignment] \in ActiveStates
    /\ assignmentLease[workerAssignment]
    /\ assignmentLease' =
        [assignmentLease EXCEPT ![workerAssignment] = FALSE]
    /\ workerAssignment' = NoAssignment
    \* Expiration/rebind changes the process shell, not the failure budget.
    /\ UNCHANGED <<managerUp, assignmentState, attemptNumber, failureCount,
        effectAssignment, inputRevision, promptRevision, receipts,
        terminalAssignment, settledAssignment, recoveryScans,
        lastScanObserved, lastScanOwner, legacyDuplicate, failureReceipts, settlementKind, control>>

RecompileInputs ==
    /\ control = "Run"
    /\ inputRevision < MaxInputRevision
    /\ inputRevision' = inputRevision + 1
    \* Recompiling attempt-local workspace inputs or republishing a
    \* Manager-owned progress journal must not allocate or bind another
    \* durable assignment, and must not replace a submission receipt.
    /\ UNCHANGED <<managerUp, assignmentState, assignmentLease,
        attemptNumber, failureCount, effectAssignment, workerAssignment, promptRevision,
        receipts, terminalAssignment, settledAssignment, recoveryScans,
        lastScanObserved, lastScanOwner, legacyDuplicate, failureReceipts, settlementKind, control>>

RecoverExpiredAssignment ==
    /\ managerUp
    /\ workerAssignment = NoAssignment
    /\ effectAssignment \in Assignments
    /\ assignmentState[effectAssignment] \in ActiveStates
    /\ ~assignmentLease[effectAssignment]
    /\ assignmentState' =
        [assignmentState EXCEPT ![effectAssignment] = "RetryQueued"]
    \* Recovery reuses both the durable identity and its original prompt.
    /\ UNCHANGED <<managerUp, assignmentLease, attemptNumber, failureCount,
        effectAssignment, workerAssignment, inputRevision, promptRevision,
        receipts, terminalAssignment, settledAssignment, recoveryScans,
        lastScanObserved, lastScanOwner, legacyDuplicate, failureReceipts, settlementKind, control>>

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
        attemptNumber, failureCount, effectAssignment, workerAssignment, inputRevision,
        promptRevision, receipts, terminalAssignment, settledAssignment,
        legacyDuplicate, failureReceipts, settlementKind, control>>

RecordResult(a, kind) ==
    /\ control \in {"Run", "PauseRequested"}
    /\ managerUp
    /\ workerAssignment = a
    /\ effectAssignment = a
    /\ assignmentState[a] = "Running"
    /\ assignmentLease[a]
    /\ assignmentState' = [assignmentState EXCEPT ![a] = "ResultRecorded"]
    /\ assignmentLease' = [assignmentLease EXCEPT ![a] = FALSE]
    /\ workerAssignment' = NoAssignment
    /\ receipts' = receipts \cup {a}
    /\ failureReceipts' = IF kind = "Failure" THEN failureReceipts \cup {a} ELSE failureReceipts
    /\ terminalAssignment' = a
    /\ UNCHANGED <<managerUp, attemptNumber, failureCount, effectAssignment,
        inputRevision, promptRevision, settledAssignment, recoveryScans,
        lastScanObserved, lastScanOwner, legacyDuplicate, settlementKind, control>>

SettleReceipt(kind) ==
    /\ control \in {"Run", "PauseRequested"}
    \* A durable failure may only discharge ROLE_FAILED; all other
    \* semantic settlement consumes a successful role submission.
    /\ (kind = "Failure") = (terminalAssignment \in failureReceipts)
    /\ managerUp
    /\ terminalAssignment \in receipts
    /\ assignmentState[terminalAssignment] = "ResultRecorded"
    /\ assignmentState' =
        [assignmentState EXCEPT ![terminalAssignment] = "Settled"]
    /\ settledAssignment' = terminalAssignment
    /\ settlementKind' = kind
    \* Settlement follows the immutable terminal identity, not a mutable scan.
    /\ UNCHANGED <<managerUp, assignmentLease, attemptNumber, failureCount,
        effectAssignment, workerAssignment, inputRevision, promptRevision,
        receipts, terminalAssignment, recoveryScans, lastScanObserved,
        lastScanOwner, legacyDuplicate, failureReceipts, control>>

SettleTerminalResult ==
    \E kind \in {"Success", "Failure"} : SettleReceipt(kind)

CrashManager ==
    /\ managerUp
    /\ managerUp' = FALSE
    /\ assignmentLease' =
        [a \in Assignments |-> IF a = workerAssignment THEN FALSE
                              ELSE assignmentLease[a]]
    /\ workerAssignment' = NoAssignment
    \* A graceful/process loss recovery is not a failed logical attempt.
    /\ UNCHANGED <<assignmentState, attemptNumber, failureCount, effectAssignment,
        inputRevision, promptRevision, receipts, terminalAssignment,
        settledAssignment, recoveryScans, lastScanObserved, lastScanOwner,
        legacyDuplicate, failureReceipts, settlementKind, control>>

RestartManager ==
    /\ ~managerUp
    /\ managerUp' = TRUE
    /\ UNCHANGED <<assignmentState, assignmentLease, attemptNumber, failureCount,
        effectAssignment, workerAssignment, inputRevision, promptRevision,
        receipts, terminalAssignment, settledAssignment, recoveryScans,
        lastScanObserved, lastScanOwner, legacyDuplicate, failureReceipts, settlementKind, control>>

InjectLegacyDuplicate ==
    /\ control = "Run"
    \* This fault state represents a database produced by the old bug. The
    \* fixed recovery scan must contain it without stealing the active owner.
    /\ ~legacyDuplicate
    /\ managerUp
    /\ workerAssignment = NoAssignment
    /\ effectAssignment = OldAssignment
    /\ assignmentState[OldAssignment] \in ActiveStates
    /\ ~assignmentLease[OldAssignment]
    /\ attemptNumber[NewAssignment] < MaxStarts
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
    /\ UNCHANGED <<managerUp, failureCount, inputRevision, receipts, terminalAssignment,
        settledAssignment, recoveryScans, lastScanObserved, lastScanOwner, failureReceipts, settlementKind, control>>

FailWorker ==
    /\ managerUp
    /\ workerAssignment \in Assignments
    /\ assignmentState[workerAssignment] \in ActiveStates
    /\ assignmentLease[workerAssignment]
    /\ failureCount[workerAssignment] < MaxAttempt
    /\ assignmentState' =
        [assignmentState EXCEPT ![workerAssignment] = "RetryQueued"]
    /\ assignmentLease' =
        [assignmentLease EXCEPT ![workerAssignment] = FALSE]
    /\ failureCount' =
        [failureCount EXCEPT ![workerAssignment] = @ + 1]
    /\ workerAssignment' = NoAssignment
    /\ UNCHANGED <<managerUp, attemptNumber, effectAssignment, inputRevision,
        promptRevision, receipts, terminalAssignment, settledAssignment,
        recoveryScans, lastScanObserved, lastScanOwner, legacyDuplicate, failureReceipts, settlementKind, control>>

RequestPause ==
    /\ control = "Run"
    /\ control' = "PauseRequested"
    /\ UNCHANGED <<managerUp, assignmentState, assignmentLease, attemptNumber,
        failureCount, effectAssignment, workerAssignment, inputRevision, promptRevision,
        receipts, terminalAssignment, settledAssignment, recoveryScans,
        lastScanObserved, lastScanOwner, legacyDuplicate, failureReceipts, settlementKind>>

PauseAtBoundary ==
    /\ managerUp
    /\ control = "PauseRequested"
    /\ \A a \in Assignments : assignmentState[a] # "ResultRecorded"
    /\ control' = "Paused"
    \* Physical retirement of a pre-receipt attempt does not charge the logical
    \* failure budget. A durable result must go through SettleReceipt instead.
    /\ assignmentState' = [a \in Assignments |->
        IF assignmentState[a] \in ActiveStates THEN "RetryQueued" ELSE assignmentState[a]]
    /\ assignmentLease' = [a \in Assignments |-> FALSE]
    /\ workerAssignment' = NoAssignment
    /\ UNCHANGED <<managerUp, attemptNumber, failureCount, effectAssignment,
        inputRevision, promptRevision, receipts, terminalAssignment, settledAssignment,
        recoveryScans, lastScanObserved, lastScanOwner, legacyDuplicate, failureReceipts, settlementKind>>

ResumeControl ==
    /\ control = "Paused"
    /\ control' = "Run"
    /\ UNCHANGED <<managerUp, assignmentState, assignmentLease, attemptNumber,
        failureCount, effectAssignment, workerAssignment, inputRevision, promptRevision,
        receipts, terminalAssignment, settledAssignment, recoveryScans,
        lastScanObserved, lastScanOwner, legacyDuplicate, failureReceipts, settlementKind>>

CancelControl ==
    /\ managerUp
    /\ control # "Cancelled"
    /\ control' = "Cancelled"
    /\ assignmentState' = [a \in Assignments |->
        IF assignmentState[a] = "ResultRecorded" THEN "Settled"
        ELSE IF assignmentState[a] \in RecoverableStates THEN "Cancelled"
        ELSE assignmentState[a]]
    /\ assignmentLease' = [a \in Assignments |-> FALSE]
    /\ workerAssignment' = NoAssignment
    /\ settledAssignment' = IF terminalAssignment \in receipts THEN terminalAssignment ELSE settledAssignment
    /\ settlementKind' = IF terminalAssignment \in receipts /\ settledAssignment = NoAssignment
                       THEN "Retired" ELSE settlementKind
    /\ UNCHANGED <<managerUp, attemptNumber, failureCount, effectAssignment,
        inputRevision, promptRevision, receipts, terminalAssignment, recoveryScans,
        lastScanObserved, lastScanOwner, legacyDuplicate, failureReceipts>>

ClaimSomeAssignment ==
    \E a \in Assignments : ClaimAssignment(a)

StartSomeAssignment ==
    \E a \in Assignments : StartAssignment(a)

ScanSomeAssignment ==
    \E a \in Assignments : RecoveryScan(a)

RecordSomeResult ==
    \E a \in Assignments, kind \in {"Success", "Failure"} : RecordResult(a, kind)

Next ==
    \/ ClaimSomeAssignment
    \/ StartSomeAssignment
    \/ ExpireWorkerLease
    \/ FailWorker
    \/ RecompileInputs
    \/ RecoverExpiredAssignment
    \/ ScanSomeAssignment
    \/ RecordSomeResult
    \/ SettleTerminalResult
    \/ CrashManager
    \/ RestartManager
    \/ InjectLegacyDuplicate
    \/ RequestPause
    \/ PauseAtBoundary
    \/ ResumeControl
    \/ CancelControl

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
    /\ attemptNumber \in [Assignments -> 0..MaxStarts]
    /\ failureCount \in [Assignments -> 0..MaxAttempt]
    /\ effectAssignment \in AssignmentIds
    /\ workerAssignment \in AssignmentIds
    /\ inputRevision \in 0..MaxInputRevision
    /\ promptRevision \in [Assignments -> 0..MaxInputRevision]
    /\ receipts \subseteq Assignments
    /\ failureReceipts \subseteq receipts
    /\ settlementKind \in {"None", "Success", "Failure", "Retired"}
    /\ control \in {"Run", "PauseRequested", "Paused", "Cancelled"}
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

OneOpenAssignmentPerSession ==
    legacyDuplicate \/
        Cardinality(
            {a \in Assignments : assignmentState[a] \in RecoverableStates}
        ) <= 1

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

SettlementMatchesReceipt ==
    settledAssignment \in Assignments =>
        IF settlementKind = "Retired" THEN control = "Cancelled"
        ELSE (settlementKind = "Failure") = (settledAssignment \in failureReceipts)

PauseDrainsRecordedResult ==
    control = "Paused" =>
        /\ workerAssignment = NoAssignment
        /\ \A a \in Assignments : assignmentState[a] # "ResultRecorded"

FailureBudgetCountsLogicalFailures ==
    \A a \in Assignments : failureCount[a] <= attemptNumber[a]

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
