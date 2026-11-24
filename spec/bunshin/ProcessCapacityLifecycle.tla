-------------------- MODULE ProcessCapacityLifecycle -------------------
EXTENDS Naturals, FiniteSets

CONSTANTS Workers, Capacity, Fault

ProcessStates == {"None", "Running", "Reaped"}

VARIABLES logical, processState, permits, attemptLeases, reaped, checkpointed, released,
    checkpointPending, reserved, unsettled

vars == <<logical, processState, permits, attemptLeases, reaped, checkpointed, released,
    checkpointPending, reserved, unsettled>>

Init ==
    /\ logical = {}
    /\ processState = [w \in Workers |-> "None"]
    /\ permits = {}
    /\ attemptLeases = {}
    /\ reaped = {}
    /\ checkpointed = {}
    /\ released = {}
    /\ checkpointPending = {}
    /\ reserved = {}
    /\ unsettled = {}

CreateLogical(w) ==
    /\ w \in Workers \ logical
    /\ logical' = logical \union {w}
    /\ UNCHANGED <<processState, permits, attemptLeases, reaped, checkpointed, released,
        checkpointPending, reserved, unsettled>>

Spawn(w) ==
    /\ w \in logical
    /\ processState[w] = "None"
    /\ w \notin permits
    /\ w \notin unsettled
    /\ w \in reserved \/ Cardinality(IF Fault = "ignore-reservation" THEN permits ELSE permits \union reserved) < Capacity
    /\ reserved' = reserved \ {w}
    /\ processState' = [processState EXCEPT ![w] = "Running"]
    /\ permits' = permits \union {w}
    /\ attemptLeases' = attemptLeases \union {w}
    \* These facts belong to the current incarnation, not its logical session.
    /\ reaped' = reaped \ {w}
    /\ checkpointed' = IF Fault = "reuse-checkpoint" THEN checkpointed ELSE checkpointed \ {w}
    /\ released' = released \ {w}
    \* Independent monitor: the release guard must discharge a fresh checkpoint.
    /\ checkpointPending' = checkpointPending \union {w}
    /\ UNCHANGED <<logical, unsettled>>

ReapGroup(w) ==
    /\ processState[w] = "Running"
    /\ w \in permits
    /\ processState' = [processState EXCEPT ![w] = "Reaped"]
    /\ reaped' = reaped \union {w}
    /\ UNCHANGED <<logical, permits, attemptLeases, checkpointed, released, checkpointPending, reserved, unsettled>>

Checkpoint(w) ==
    /\ processState[w] = "Reaped"
    /\ w \in permits
    /\ checkpointed' = checkpointed \union {w}
    /\ checkpointPending' = checkpointPending \ {w}
    /\ UNCHANGED <<logical, processState, permits, attemptLeases, reaped, released, reserved, unsettled>>

Release(w) ==
    /\ processState[w] = "Reaped"
    /\ w \in permits
    /\ w \in checkpointed
    /\ processState' = [processState EXCEPT ![w] = "None"]
    /\ permits' = permits \ {w}
    /\ attemptLeases' = attemptLeases \ {w}
    /\ released' = released \union {w}
    /\ reserved' \in {reserved, reserved \union {w}}
    /\ unsettled' \in IF w \in reserved' THEN {unsettled, unsettled \union {w}} ELSE {unsettled}
    /\ UNCHANGED <<logical, reaped, checkpointed, checkpointPending>>

SettlePrevious(w) ==
    /\ w \in unsettled
    /\ unsettled' = unsettled \ {w}
    /\ UNCHANGED <<logical, processState, permits, attemptLeases, reaped, checkpointed,
                   released, checkpointPending, reserved>>

YieldNode(w) ==
    /\ unsettled' = unsettled \ {w}
    /\ w \in reserved
    /\ reserved' = reserved \ {w}
    /\ UNCHANGED <<logical, processState, permits, attemptLeases, reaped, checkpointed,
                   released, checkpointPending>>

RetireLogical(w) ==
    /\ w \notin reserved
    /\ w \in logical
    /\ processState[w] = "None"
    /\ w \notin permits
    /\ logical' = logical \ {w}
    /\ UNCHANGED <<processState, permits, attemptLeases, reaped, checkpointed, released,
        checkpointPending, reserved, unsettled>>

Next ==
    \/ \E w \in Workers : CreateLogical(w)
    \/ \E w \in Workers : Spawn(w)
    \/ \E w \in Workers : ReapGroup(w)
    \/ \E w \in Workers : Checkpoint(w)
    \/ \E w \in Workers : Release(w)
    \/ \E w \in Workers : RetireLogical(w)
    \/ \E w \in Workers : YieldNode(w)
    \/ \E w \in Workers : SettlePrevious(w)

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ logical \subseteq Workers
    /\ processState \in [Workers -> ProcessStates]
    /\ permits \subseteq Workers
    /\ reserved \subseteq Workers
    /\ unsettled \subseteq Workers
    /\ attemptLeases \subseteq Workers
    /\ reaped \subseteq Workers
    /\ checkpointed \subseteq Workers
    /\ released \subseteq Workers
    /\ checkpointPending \subseteq Workers

CapacityBound == Cardinality(permits \union reserved) <= Capacity

NodeReservationSafe == unsettled \subseteq reserved /\ reserved \subseteq (logical \intersect reaped \intersect checkpointed) /\ reserved \intersect permits = {}

PermitExactlyMaterialized ==
    \A w \in Workers : (w \in permits) <=> (processState[w] \in {"Running", "Reaped"})

AttemptLeaseExactlyMaterialized == attemptLeases = permits

ReleaseRequiresReapAndCheckpoint ==
    /\ released \subseteq (reaped \intersect checkpointed)
    /\ released \intersect checkpointPending = {}

LogicalExistenceConsumesNoSlot == permits \subseteq logical

=======================================================================
