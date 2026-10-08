-------------------- MODULE ProcessCapacityLifecycle -------------------
EXTENDS Naturals, FiniteSets

CONSTANTS Workers, Capacity, Fault

ProcessStates == {"None", "Running", "Reaped"}

VARIABLES logical, processState, permits, attemptLeases, reaped, checkpointed, released,
    checkpointPending

vars == <<logical, processState, permits, attemptLeases, reaped, checkpointed, released,
    checkpointPending>>

Init ==
    /\ logical = {}
    /\ processState = [w \in Workers |-> "None"]
    /\ permits = {}
    /\ attemptLeases = {}
    /\ reaped = {}
    /\ checkpointed = {}
    /\ released = {}
    /\ checkpointPending = {}

CreateLogical(w) ==
    /\ w \in Workers \ logical
    /\ logical' = logical \union {w}
    /\ UNCHANGED <<processState, permits, attemptLeases, reaped, checkpointed, released,
        checkpointPending>>

Spawn(w) ==
    /\ w \in logical
    /\ processState[w] = "None"
    /\ w \notin permits
    /\ Cardinality(permits) < Capacity
    /\ processState' = [processState EXCEPT ![w] = "Running"]
    /\ permits' = permits \union {w}
    /\ attemptLeases' = attemptLeases \union {w}
    \* These facts belong to the current incarnation, not its logical session.
    /\ reaped' = reaped \ {w}
    /\ checkpointed' = IF Fault = "reuse-checkpoint" THEN checkpointed ELSE checkpointed \ {w}
    /\ released' = released \ {w}
    \* Independent monitor: the release guard must discharge a fresh checkpoint.
    /\ checkpointPending' = checkpointPending \union {w}
    /\ UNCHANGED logical

ReapGroup(w) ==
    /\ processState[w] = "Running"
    /\ w \in permits
    /\ processState' = [processState EXCEPT ![w] = "Reaped"]
    /\ reaped' = reaped \union {w}
    /\ UNCHANGED <<logical, permits, attemptLeases, checkpointed, released, checkpointPending>>

Checkpoint(w) ==
    /\ processState[w] = "Reaped"
    /\ w \in permits
    /\ checkpointed' = checkpointed \union {w}
    /\ checkpointPending' = checkpointPending \ {w}
    /\ UNCHANGED <<logical, processState, permits, attemptLeases, reaped, released>>

Release(w) ==
    /\ processState[w] = "Reaped"
    /\ w \in permits
    /\ w \in checkpointed
    /\ processState' = [processState EXCEPT ![w] = "None"]
    /\ permits' = permits \ {w}
    /\ attemptLeases' = attemptLeases \ {w}
    /\ released' = released \union {w}
    /\ UNCHANGED <<logical, reaped, checkpointed, checkpointPending>>

RetireLogical(w) ==
    /\ w \in logical
    /\ processState[w] = "None"
    /\ w \notin permits
    /\ logical' = logical \ {w}
    /\ UNCHANGED <<processState, permits, attemptLeases, reaped, checkpointed, released,
        checkpointPending>>

Next ==
    \/ \E w \in Workers : CreateLogical(w)
    \/ \E w \in Workers : Spawn(w)
    \/ \E w \in Workers : ReapGroup(w)
    \/ \E w \in Workers : Checkpoint(w)
    \/ \E w \in Workers : Release(w)
    \/ \E w \in Workers : RetireLogical(w)

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ logical \subseteq Workers
    /\ processState \in [Workers -> ProcessStates]
    /\ permits \subseteq Workers
    /\ attemptLeases \subseteq Workers
    /\ reaped \subseteq Workers
    /\ checkpointed \subseteq Workers
    /\ released \subseteq Workers
    /\ checkpointPending \subseteq Workers

CapacityBound == Cardinality(permits) <= Capacity

PermitExactlyMaterialized ==
    \A w \in Workers : (w \in permits) <=> (processState[w] \in {"Running", "Reaped"})

AttemptLeaseExactlyMaterialized == attemptLeases = permits

ReleaseRequiresReapAndCheckpoint ==
    /\ released \subseteq (reaped \intersect checkpointed)
    /\ released \intersect checkpointPending = {}

LogicalExistenceConsumesNoSlot == permits \subseteq logical

=======================================================================
