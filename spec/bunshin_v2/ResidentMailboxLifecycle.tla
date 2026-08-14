-------------------- MODULE ResidentMailboxLifecycle --------------------
EXTENDS Naturals, TLC

CONSTANT MaxMessages

VARIABLES
    active,
    pending,
    quiescing,
    cancelling,
    arrived,
    completed,
    interrupted,
    resetTimedOut

vars == <<active, pending, quiescing, cancelling, arrived, completed, interrupted, resetTimedOut>>

Init ==
    /\ active = FALSE
    /\ pending = 0
    /\ quiescing = FALSE
    /\ cancelling = FALSE
    /\ arrived = 0
    /\ completed = 0
    /\ interrupted = 0
    /\ resetTimedOut = FALSE

ReceiveAndStart ==
    /\ ~active
    /\ ~quiescing
    /\ arrived < MaxMessages
    /\ active' = TRUE
    /\ arrived' = arrived + 1
    /\ UNCHANGED <<pending, quiescing, cancelling, completed, interrupted, resetTimedOut>>

ReceiveAndQueue ==
    /\ active \/ quiescing
    /\ arrived < MaxMessages
    /\ pending' = pending + 1
    /\ arrived' = arrived + 1
    /\ UNCHANGED <<active, quiescing, cancelling, completed, interrupted, resetTimedOut>>

StartQueued ==
    /\ ~active
    /\ ~quiescing
    /\ pending > 0
    /\ active' = TRUE
    /\ pending' = pending - 1
    /\ UNCHANGED <<quiescing, cancelling, arrived, completed, interrupted, resetTimedOut>>

Finish ==
    /\ active
    /\ ~quiescing
    /\ ~cancelling
    /\ active' = FALSE
    /\ completed' = completed + 1
    /\ UNCHANGED <<pending, quiescing, cancelling, arrived, interrupted, resetTimedOut>>

BeginReset ==
    /\ ~quiescing
    /\ quiescing' = TRUE
    /\ cancelling' = active
    /\ UNCHANGED <<active, pending, arrived, completed, interrupted, resetTimedOut>>

CancelComplete ==
    /\ active
    /\ cancelling
    /\ active' = FALSE
    /\ cancelling' = FALSE
    /\ interrupted' = interrupted + 1
    /\ UNCHANGED <<pending, quiescing, arrived, completed, resetTimedOut>>

ResetTimeout ==
    /\ quiescing
    /\ cancelling
    /\ active
    /\ quiescing' = FALSE
    /\ cancelling' = FALSE
    /\ resetTimedOut' = TRUE
    /\ UNCHANGED <<active, pending, arrived, completed, interrupted>>

EndReset ==
    /\ quiescing
    /\ ~active
    /\ quiescing' = FALSE
    /\ cancelling' = FALSE
    /\ UNCHANGED <<active, pending, arrived, completed, interrupted, resetTimedOut>>

Next ==
    \/ ReceiveAndStart
    \/ ReceiveAndQueue
    \/ StartQueued
    \/ Finish
    \/ BeginReset
    \/ CancelComplete
    \/ ResetTimeout
    \/ EndReset

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ active \in BOOLEAN
    /\ pending \in 0..MaxMessages
    /\ quiescing \in BOOLEAN
    /\ cancelling \in BOOLEAN
    /\ arrived \in 0..MaxMessages
    /\ completed \in 0..MaxMessages
    /\ interrupted \in 0..MaxMessages
    /\ resetTimedOut \in BOOLEAN

CancellationIsHonest == cancelling => active

ResetDoesNotLie == quiescing /\ ~cancelling => ~active

QueuedMessagesSurviveReset ==
    arrived = pending + (IF active THEN 1 ELSE 0) + completed + interrupted

======================================================================
