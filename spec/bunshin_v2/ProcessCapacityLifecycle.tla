-------------------- MODULE ProcessCapacityLifecycle -------------------
EXTENDS Naturals, FiniteSets

CONSTANTS Workers, Capacity

ProcessStates == {"None", "Running", "Reaped"}

VARIABLES logical, processState, permits, attemptLeases, reapedEver, checkpointed, released

vars == <<logical, processState, permits, attemptLeases, reapedEver, checkpointed, released>>

Init ==
    /\ logical = {}
    /\ processState = [w \in Workers |-> "None"]
    /\ permits = {}
    /\ attemptLeases = {}
    /\ reapedEver = {}
    /\ checkpointed = {}
    /\ released = {}

CreateLogical(w) ==
    /\ w \in Workers \ logical
    /\ logical' = logical \union {w}
    /\ UNCHANGED <<processState, permits, attemptLeases, reapedEver, checkpointed, released>>

Spawn(w) ==
    /\ w \in logical
    /\ processState[w] = "None"
    /\ w \notin permits
    /\ Cardinality(permits) < Capacity
    /\ processState' = [processState EXCEPT ![w] = "Running"]
    /\ permits' = permits \union {w}
    /\ attemptLeases' = attemptLeases \union {w}
    /\ UNCHANGED <<logical, reapedEver, checkpointed, released>>

ReapGroup(w) ==
    /\ processState[w] = "Running"
    /\ w \in permits
    /\ processState' = [processState EXCEPT ![w] = "Reaped"]
    /\ reapedEver' = reapedEver \union {w}
    /\ UNCHANGED <<logical, permits, attemptLeases, checkpointed, released>>

Checkpoint(w) ==
    /\ processState[w] = "Reaped"
    /\ w \in permits
    /\ checkpointed' = checkpointed \union {w}
    /\ UNCHANGED <<logical, processState, permits, attemptLeases, reapedEver, released>>

Release(w) ==
    /\ processState[w] = "Reaped"
    /\ w \in permits
    /\ w \in checkpointed
    /\ processState' = [processState EXCEPT ![w] = "None"]
    /\ permits' = permits \ {w}
    /\ attemptLeases' = attemptLeases \ {w}
    /\ released' = released \union {w}
    /\ UNCHANGED <<logical, reapedEver, checkpointed>>

RetireLogical(w) ==
    /\ w \in logical
    /\ processState[w] = "None"
    /\ w \notin permits
    /\ logical' = logical \ {w}
    /\ UNCHANGED <<processState, permits, attemptLeases, reapedEver, checkpointed, released>>

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
    /\ reapedEver \subseteq Workers
    /\ checkpointed \subseteq Workers
    /\ released \subseteq Workers

CapacityBound == Cardinality(permits) <= Capacity

PermitExactlyMaterialized ==
    \A w \in Workers : (w \in permits) <=> (processState[w] \in {"Running", "Reaped"})

AttemptLeaseExactlyMaterialized == attemptLeases = permits

ReleaseRequiresReapAndCheckpoint ==
    released \subseteq (reapedEver \intersect checkpointed)

LogicalExistenceConsumesNoSlot == permits \subseteq logical

=======================================================================
