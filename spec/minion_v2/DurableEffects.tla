------------------------- MODULE DurableEffects -------------------------
EXTENDS Naturals, FiniteSets, TLC

CONSTANT ActionA, WorkerA, WorkerB, MaxToken, MaxExternalCalls, MaxWrites

Actions == {ActionA}
Workers == {WorkerA, WorkerB}
EffectStates == {"None", "Pending", "Inflight", "Completed", "Failed"}
Owners == Workers \cup {"None"}
AssignmentStates == {
    "None", "Queued", "Claimed", "Running", "RetryQueued",
    "ResultRecorded", "Settled", "Cancelled"
}

VARIABLES
    aggregateVersion,
    dedup,
    events,
    outbox,
    effectOwner,
    effectFence,
    effectApplied,
    effectReceipts,
    externalCalls,
    businessAdvanced,
    effectTriaged,
    assignmentState,
    assignmentFence,
    assignmentLease,
    zombieFence,
    writeTokens,
    rejectedZombieWrites,
    resultReceipt,
    parentSettled,
    managerUp

vars == <<
    aggregateVersion, dedup, events, outbox, effectOwner, effectFence,
    effectApplied, effectReceipts, externalCalls, businessAdvanced,
    effectTriaged, assignmentState, assignmentFence, assignmentLease,
    zombieFence, writeTokens, rejectedZombieWrites, resultReceipt,
    parentSettled, managerUp
>>

Init ==
    /\ aggregateVersion = 0
    /\ dedup = {}
    /\ events = {}
    /\ outbox = [a \in Actions |-> "None"]
    /\ effectOwner = [a \in Actions |-> "None"]
    /\ effectFence = [a \in Actions |-> 0]
    /\ effectApplied = {}
    /\ effectReceipts = {}
    /\ externalCalls = [a \in Actions |-> 0]
    /\ businessAdvanced = {}
    /\ effectTriaged = {}
    /\ assignmentState = "None"
    /\ assignmentFence = 0
    /\ assignmentLease = FALSE
    /\ zombieFence = 0
    /\ writeTokens = {}
    /\ rejectedZombieWrites = 0
    /\ resultReceipt = FALSE
    /\ parentSettled = FALSE
    /\ managerUp = TRUE

DispatchAction(a) ==
    /\ a \in Actions
    /\ a \notin dedup
    /\ aggregateVersion < Cardinality(Actions)
    /\ aggregateVersion' = aggregateVersion + 1
    /\ dedup' = dedup \cup {a}
    /\ events' = events \cup {a}
    /\ outbox' = [outbox EXCEPT ![a] = "Pending"]
    /\ UNCHANGED <<effectOwner, effectFence, effectApplied, effectReceipts,
        externalCalls, businessAdvanced, effectTriaged, assignmentState,
        assignmentFence, assignmentLease, zombieFence, writeTokens,
        rejectedZombieWrites, resultReceipt, parentSettled, managerUp>>

ReplayAction(a) ==
    /\ a \in dedup
    /\ UNCHANGED vars

ClaimEffect(a, worker) ==
    /\ managerUp
    /\ a \in Actions
    /\ worker \in Workers
    /\ outbox[a] = "Pending"
    /\ effectFence[a] < MaxToken
    /\ outbox' = [outbox EXCEPT ![a] = "Inflight"]
    /\ effectOwner' = [effectOwner EXCEPT ![a] = worker]
    /\ effectFence' = [effectFence EXCEPT ![a] = @ + 1]
    /\ UNCHANGED <<aggregateVersion, dedup, events, effectApplied,
        effectReceipts, externalCalls, businessAdvanced, effectTriaged,
        assignmentState, assignmentFence, assignmentLease, zombieFence,
        writeTokens, rejectedZombieWrites, resultReceipt, parentSettled,
        managerUp>>

PerformExternalEffect(a) ==
    /\ managerUp
    /\ outbox[a] = "Inflight"
    /\ effectOwner[a] \in Workers
    /\ externalCalls[a] < MaxExternalCalls
    /\ effectApplied' = effectApplied \cup {a}
    /\ externalCalls' = [externalCalls EXCEPT ![a] = @ + 1]
    /\ UNCHANGED <<aggregateVersion, dedup, events, outbox, effectOwner,
        effectFence, effectReceipts, businessAdvanced, effectTriaged,
        assignmentState, assignmentFence, assignmentLease, zombieFence,
        writeTokens, rejectedZombieWrites, resultReceipt, parentSettled,
        managerUp>>

AckEffect(a) ==
    /\ managerUp
    /\ outbox[a] = "Inflight"
    /\ a \in effectApplied
    /\ outbox' = [outbox EXCEPT ![a] = "Completed"]
    /\ effectOwner' = [effectOwner EXCEPT ![a] = "None"]
    /\ effectReceipts' = effectReceipts \cup {a}
    /\ businessAdvanced' = businessAdvanced \cup {a}
    /\ UNCHANGED <<aggregateVersion, dedup, events, effectFence,
        effectApplied, externalCalls, effectTriaged, assignmentState,
        assignmentFence, assignmentLease, zombieFence, writeTokens,
        rejectedZombieWrites, resultReceipt, parentSettled, managerUp>>

ExpireEffectClaim(a) ==
    /\ outbox[a] = "Inflight"
    /\ outbox' = [outbox EXCEPT ![a] = "Pending"]
    /\ effectOwner' = [effectOwner EXCEPT ![a] = "None"]
    /\ UNCHANGED <<aggregateVersion, dedup, events, effectFence,
        effectApplied, effectReceipts, externalCalls, businessAdvanced,
        effectTriaged, assignmentState, assignmentFence, assignmentLease,
        zombieFence, writeTokens, rejectedZombieWrites, resultReceipt,
        parentSettled, managerUp>>

FailEffect(a) ==
    /\ outbox[a] \in {"Pending", "Inflight"}
    /\ outbox' = [outbox EXCEPT ![a] = "Failed"]
    /\ effectOwner' = [effectOwner EXCEPT ![a] = "None"]
    /\ effectTriaged' = effectTriaged \cup {a}
    /\ UNCHANGED <<aggregateVersion, dedup, events, effectFence,
        effectApplied, effectReceipts, externalCalls, businessAdvanced,
        assignmentState, assignmentFence, assignmentLease, zombieFence,
        writeTokens, rejectedZombieWrites, resultReceipt, parentSettled,
        managerUp>>

CreateAssignment ==
    /\ assignmentState = "None"
    /\ assignmentState' = "Queued"
    /\ UNCHANGED <<aggregateVersion, dedup, events, outbox, effectOwner,
        effectFence, effectApplied, effectReceipts, externalCalls,
        businessAdvanced, effectTriaged, assignmentFence, assignmentLease,
        zombieFence, writeTokens, rejectedZombieWrites, resultReceipt,
        parentSettled, managerUp>>

ClaimAssignment ==
    /\ managerUp
    /\ assignmentState \in {"Queued", "RetryQueued"}
    /\ assignmentFence < MaxToken
    /\ assignmentState' = "Claimed"
    /\ zombieFence' = IF assignmentFence > 0 THEN assignmentFence ELSE zombieFence
    /\ assignmentFence' = assignmentFence + 1
    /\ assignmentLease' = TRUE
    /\ UNCHANGED <<aggregateVersion, dedup, events, outbox, effectOwner,
        effectFence, effectApplied, effectReceipts, externalCalls,
        businessAdvanced, effectTriaged, writeTokens, rejectedZombieWrites,
        resultReceipt, parentSettled, managerUp>>

StartAssignment ==
    /\ managerUp
    /\ assignmentState = "Claimed"
    /\ assignmentLease
    /\ assignmentState' = "Running"
    /\ UNCHANGED <<aggregateVersion, dedup, events, outbox, effectOwner,
        effectFence, effectApplied, effectReceipts, externalCalls,
        businessAdvanced, effectTriaged, assignmentFence, assignmentLease,
        zombieFence, writeTokens, rejectedZombieWrites, resultReceipt,
        parentSettled, managerUp>>

WorkerWrite ==
    /\ managerUp
    /\ assignmentState = "Running"
    /\ assignmentLease
    /\ assignmentFence > 0
    /\ Cardinality(writeTokens) < MaxWrites
    /\ writeTokens' = writeTokens \cup {assignmentFence}
    /\ UNCHANGED <<aggregateVersion, dedup, events, outbox, effectOwner,
        effectFence, effectApplied, effectReceipts, externalCalls,
        businessAdvanced, effectTriaged, assignmentState, assignmentFence,
        assignmentLease, zombieFence, rejectedZombieWrites, resultReceipt,
        parentSettled, managerUp>>

ExpireAssignmentLease ==
    /\ assignmentState \in {"Claimed", "Running"}
    /\ assignmentLease
    /\ assignmentState' = "RetryQueued"
    /\ assignmentLease' = FALSE
    /\ zombieFence' = assignmentFence
    /\ UNCHANGED <<aggregateVersion, dedup, events, outbox, effectOwner,
        effectFence, effectApplied, effectReceipts, externalCalls,
        businessAdvanced, effectTriaged, assignmentFence, writeTokens,
        rejectedZombieWrites, resultReceipt, parentSettled, managerUp>>

RejectZombieWrite ==
    /\ zombieFence > 0
    /\ zombieFence # assignmentFence \/ ~assignmentLease
    /\ rejectedZombieWrites < MaxWrites
    /\ rejectedZombieWrites' = rejectedZombieWrites + 1
    /\ UNCHANGED <<aggregateVersion, dedup, events, outbox, effectOwner,
        effectFence, effectApplied, effectReceipts, externalCalls,
        businessAdvanced, effectTriaged, assignmentState, assignmentFence,
        assignmentLease, zombieFence, writeTokens, resultReceipt,
        parentSettled, managerUp>>

RecordResult ==
    /\ managerUp
    /\ assignmentState = "Running"
    /\ assignmentLease
    /\ assignmentState' = "ResultRecorded"
    /\ assignmentLease' = FALSE
    /\ resultReceipt' = TRUE
    /\ UNCHANGED <<aggregateVersion, dedup, events, outbox, effectOwner,
        effectFence, effectApplied, effectReceipts, externalCalls,
        businessAdvanced, effectTriaged, assignmentFence, zombieFence,
        writeTokens, rejectedZombieWrites, parentSettled, managerUp>>

SettleResult ==
    /\ managerUp
    /\ assignmentState = "ResultRecorded"
    /\ resultReceipt
    /\ assignmentState' = "Settled"
    /\ parentSettled' = TRUE
    /\ UNCHANGED <<aggregateVersion, dedup, events, outbox, effectOwner,
        effectFence, effectApplied, effectReceipts, externalCalls,
        businessAdvanced, effectTriaged, assignmentFence, assignmentLease,
        zombieFence, writeTokens, rejectedZombieWrites, resultReceipt,
        managerUp>>

CancelAssignment ==
    /\ assignmentState \in {"Queued", "Claimed", "Running", "RetryQueued"}
    /\ assignmentState' = "Cancelled"
    /\ assignmentLease' = FALSE
    /\ UNCHANGED <<aggregateVersion, dedup, events, outbox, effectOwner,
        effectFence, effectApplied, effectReceipts, externalCalls,
        businessAdvanced, effectTriaged, assignmentFence, zombieFence,
        writeTokens, rejectedZombieWrites, resultReceipt, parentSettled,
        managerUp>>

CrashManager ==
    /\ managerUp
    /\ managerUp' = FALSE
    /\ UNCHANGED <<aggregateVersion, dedup, events, outbox, effectOwner,
        effectFence, effectApplied, effectReceipts, externalCalls,
        businessAdvanced, effectTriaged, assignmentState, assignmentFence,
        assignmentLease, zombieFence, writeTokens, rejectedZombieWrites,
        resultReceipt, parentSettled>>

RestartManager ==
    /\ ~managerUp
    /\ managerUp' = TRUE
    /\ UNCHANGED <<aggregateVersion, dedup, events, outbox, effectOwner,
        effectFence, effectApplied, effectReceipts, externalCalls,
        businessAdvanced, effectTriaged, assignmentState, assignmentFence,
        assignmentLease, zombieFence, writeTokens, rejectedZombieWrites,
        resultReceipt, parentSettled>>

ClaimSomeEffect == \E a \in Actions, worker \in Workers : ClaimEffect(a, worker)
PerformSomeEffect == \E a \in Actions : PerformExternalEffect(a)
AckSomeEffect == \E a \in Actions : AckEffect(a)
ExpireSomeEffect == \E a \in Actions : ExpireEffectClaim(a)
FailSomeEffect == \E a \in Actions : FailEffect(a)

Next ==
    \/ \E a \in Actions : DispatchAction(a)
    \/ \E a \in Actions : ReplayAction(a)
    \/ ClaimSomeEffect
    \/ PerformSomeEffect
    \/ AckSomeEffect
    \/ ExpireSomeEffect
    \/ FailSomeEffect
    \/ CreateAssignment
    \/ ClaimAssignment
    \/ StartAssignment
    \/ WorkerWrite
    \/ ExpireAssignmentLease
    \/ RejectZombieWrite
    \/ RecordResult
    \/ SettleResult
    \/ CancelAssignment
    \/ CrashManager
    \/ RestartManager

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(RestartManager)
    /\ SF_vars(ClaimSomeEffect)
    /\ SF_vars(PerformSomeEffect)
    /\ SF_vars(AckSomeEffect)
    /\ SF_vars(FailSomeEffect)
    /\ SF_vars(SettleResult)

TypeOK ==
    /\ aggregateVersion \in 0..Cardinality(Actions)
    /\ dedup \subseteq Actions
    /\ events \subseteq Actions
    /\ outbox \in [Actions -> EffectStates]
    /\ effectOwner \in [Actions -> Owners]
    /\ effectFence \in [Actions -> 0..MaxToken]
    /\ effectApplied \subseteq Actions
    /\ effectReceipts \subseteq Actions
    /\ externalCalls \in [Actions -> 0..MaxExternalCalls]
    /\ businessAdvanced \subseteq Actions
    /\ effectTriaged \subseteq Actions
    /\ assignmentState \in AssignmentStates
    /\ assignmentFence \in 0..MaxToken
    /\ assignmentLease \in BOOLEAN
    /\ zombieFence \in 0..MaxToken
    /\ writeTokens \subseteq 1..MaxToken
    /\ rejectedZombieWrites \in 0..MaxWrites
    /\ resultReceipt \in BOOLEAN
    /\ parentSettled \in BOOLEAN
    /\ managerUp \in BOOLEAN

AtomicDispatch ==
    /\ aggregateVersion = Cardinality(dedup)
    /\ dedup = events
    /\ dedup = {a \in Actions : outbox[a] # "None"}

EffectReceiptSafety ==
    /\ effectReceipts \subseteq effectApplied
    /\ businessAdvanced = effectReceipts
    /\ \A a \in Actions : outbox[a] = "Completed" => a \in effectReceipts
    /\ \A a \in Actions : outbox[a] = "Failed" => a \in effectTriaged

AtLeastOnceDoesNotDoubleAdvance ==
    \A a \in Actions : externalCalls[a] > 1 => a \in businessAdvanced \/ outbox[a] # "Completed"

EffectClaimOwnership ==
    \A a \in Actions :
        /\ outbox[a] = "Inflight" => effectOwner[a] \in Workers /\ effectFence[a] > 0
        /\ outbox[a] # "Inflight" => effectOwner[a] = "None"

AssignmentFencing ==
    /\ assignmentLease =>
        /\ assignmentState \in {"Claimed", "Running"}
        /\ assignmentFence > 0
    /\ writeTokens \subseteq 1..assignmentFence

RecordedResultIsImmutable ==
    assignmentState \in {"ResultRecorded", "Settled"} => resultReceipt

SettledResultAdvancedParent ==
    assignmentState = "Settled" => parentSettled

PendingEffectsEventuallyTerminate ==
    \A a \in Actions :
        outbox[a] \in {"Pending", "Inflight"} ~>
            outbox[a] \in {"Completed", "Failed"}

RecordedResultEventuallySettles ==
    assignmentState = "ResultRecorded" ~> assignmentState = "Settled"

=============================================================================
