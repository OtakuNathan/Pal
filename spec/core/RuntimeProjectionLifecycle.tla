---------------------- MODULE RuntimeProjectionLifecycle ---------------------
EXTENDS TLC

(*
The runtime pointer, durable lifecycle row, and published projection are one
logical commit.  Candidate preparation may fail without changing the stable
generation; detach failure likewise preserves the old generation.
*)

VARIABLES runtime, durable, projection, candidate

vars == <<runtime, durable, projection, candidate>>
Values == {"none", "old", "new"}

Init ==
    /\ runtime = "old"
    /\ durable = "old"
    /\ projection = "old"
    /\ candidate = "none"

PrepareCandidate ==
    /\ candidate = "none"
    /\ candidate' = "new"
    /\ UNCHANGED <<runtime, durable, projection>>

RejectCandidate ==
    /\ candidate = "new"
    /\ candidate' = "none"
    /\ UNCHANGED <<runtime, durable, projection>>

CommitCandidate ==
    /\ candidate = "new"
    /\ runtime' = "new"
    /\ durable' = "new"
    /\ projection' = "new"
    /\ candidate' = "none"

DetachFailure ==
    /\ runtime # "none"
    /\ UNCHANGED vars

DetachSuccess ==
    /\ runtime # "none"
    /\ runtime' = "none"
    /\ durable' = "none"
    /\ projection' = "none"
    /\ candidate' = "none"

Next ==
    \/ PrepareCandidate
    \/ RejectCandidate
    \/ CommitCandidate
    \/ DetachFailure
    \/ DetachSuccess

TypeOK ==
    /\ runtime \in Values
    /\ durable \in Values
    /\ projection \in Values
    /\ candidate \in {"none", "new"}

StableGenerationAgrees ==
    /\ runtime = durable
    /\ runtime = projection

Spec == Init /\ [][Next]_vars

=============================================================================
