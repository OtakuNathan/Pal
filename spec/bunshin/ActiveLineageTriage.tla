---------------------- MODULE ActiveLineageTriage ----------------------
EXTENDS Naturals

CONSTANTS OldEpoch, NewEpoch

Epochs == {OldEpoch, NewEpoch}
EpochStates == {"Absent", "Running", "ReplanRequired", "Superseded", "Completed"}
NodeStates == {"Absent", "Snapshotting", "Orphaned", "Triage", "Stale", "Accepted"}

VARIABLES workflowEpoch, epochState, nodeState, triageOwnership, successorLinked

vars == <<workflowEpoch, epochState, nodeState, triageOwnership, successorLinked>>

Init ==
    /\ workflowEpoch = OldEpoch
    /\ epochState = [e \in Epochs |-> IF e = OldEpoch THEN "Running" ELSE "Absent"]
    /\ nodeState = [e \in Epochs |-> IF e = OldEpoch THEN "Snapshotting" ELSE "Absent"]
    /\ triageOwnership = [e \in Epochs |-> FALSE]
    /\ successorLinked = FALSE

EnterOldTriageBeforeReplan ==
    /\ ~successorLinked
    /\ workflowEpoch = OldEpoch
    /\ nodeState[OldEpoch] \in {"Snapshotting", "Orphaned"}
    /\ nodeState' = [nodeState EXCEPT ![OldEpoch] = "Triage"]
    /\ triageOwnership' = [triageOwnership EXCEPT ![OldEpoch] = TRUE]
    /\ UNCHANGED <<workflowEpoch, epochState, successorLinked>>

RequestReplan ==
    /\ workflowEpoch = OldEpoch
    /\ epochState[OldEpoch] = "Running"
    /\ epochState' = [epochState EXCEPT ![OldEpoch] = "ReplanRequired"]
    /\ UNCHANGED <<workflowEpoch, nodeState, triageOwnership, successorLinked>>

LinkSuccessor ==
    /\ ~successorLinked
    /\ epochState[OldEpoch] = "ReplanRequired"
    /\ workflowEpoch' = NewEpoch
    /\ epochState' = [epochState EXCEPT
        ![OldEpoch] = "Superseded",
        ![NewEpoch] = "Running"]
    /\ nodeState' = [nodeState EXCEPT
        ![OldEpoch] = IF @ = "Triage" THEN "Triage" ELSE "Stale",
        ![NewEpoch] = "Snapshotting"]
    \* Historic TRIAGE remains an audit fact but loses live ownership atomically.
    /\ triageOwnership' = [e \in Epochs |-> FALSE]
    /\ successorLinked' = TRUE

LoseActiveLease ==
    /\ successorLinked
    /\ nodeState[NewEpoch] = "Snapshotting"
    /\ nodeState' = [nodeState EXCEPT ![NewEpoch] = "Orphaned"]
    /\ UNCHANGED <<workflowEpoch, epochState, triageOwnership, successorLinked>>

ScanActiveOrphan ==
    /\ successorLinked
    /\ workflowEpoch = NewEpoch
    /\ nodeState[NewEpoch] = "Orphaned"
    /\ nodeState' = [nodeState EXCEPT ![NewEpoch] = "Triage"]
    /\ triageOwnership' = [triageOwnership EXCEPT ![NewEpoch] = TRUE]
    /\ UNCHANGED <<workflowEpoch, epochState, successorLinked>>

ResolveActiveTriage ==
    /\ workflowEpoch = NewEpoch
    /\ nodeState[NewEpoch] = "Triage"
    /\ triageOwnership[NewEpoch]
    /\ nodeState' = [nodeState EXCEPT ![NewEpoch] = "Snapshotting"]
    /\ triageOwnership' = [triageOwnership EXCEPT ![NewEpoch] = FALSE]
    /\ UNCHANGED <<workflowEpoch, epochState, successorLinked>>

AcceptActive ==
    /\ workflowEpoch = NewEpoch
    /\ nodeState[NewEpoch] = "Snapshotting"
    /\ nodeState' = [nodeState EXCEPT ![NewEpoch] = "Accepted"]
    /\ epochState' = [epochState EXCEPT ![NewEpoch] = "Completed"]
    /\ UNCHANGED <<workflowEpoch, triageOwnership, successorLinked>>

Next ==
    \/ EnterOldTriageBeforeReplan
    \/ RequestReplan
    \/ LinkSuccessor
    \/ LoseActiveLease
    \/ ScanActiveOrphan
    \/ ResolveActiveTriage
    \/ AcceptActive

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ workflowEpoch \in Epochs
    /\ epochState \in [Epochs -> EpochStates]
    /\ nodeState \in [Epochs -> NodeStates]
    /\ triageOwnership \in [Epochs -> BOOLEAN]
    /\ successorLinked \in BOOLEAN

OnlyActiveEpochOwnsTriage ==
    \A e \in Epochs : triageOwnership[e] => e = workflowEpoch

SuccessorRetiresOldOwnership ==
    successorLinked =>
        /\ workflowEpoch = NewEpoch
        /\ epochState[OldEpoch] = "Superseded"
        /\ ~triageOwnership[OldEpoch]

OwnedTriageIsVisibleState ==
    \A e \in Epochs : triageOwnership[e] => nodeState[e] = "Triage"

=============================================================================
