---------------- MODULE MemoryMaintenanceOwnership ----------------
EXTENDS Naturals, TLC, MemorySwapReducer

CONSTANTS UnsafeOldClose, UnsafeRecoveryRelease, RequireWitness
VARIABLES current, first, second, bound, released, captured, recovered, recoveryFirst
vars == <<current, first, second, bound, released, captured, recovered, recoveryFirst>>

Init == /\ current = 0 /\ first = "new" /\ second = "new"
        /\ bound = FALSE /\ released = FALSE /\ captured = 0
        /\ recovered = FALSE /\ recoveryFirst = FALSE

StartFirst == /\ current = 0 /\ first = "new" /\ first' = "active" /\ current' = 1
              /\ UNCHANGED <<second, bound, released, captured, recovered, recoveryFirst>>
CommitFirst == /\ first = "active" /\ first' = "closing"
               /\ UNCHANGED <<current, second, bound, released, captured, recovered, recoveryFirst>>
BindFirst == /\ first = "closing" /\ ~bound /\ bound' = TRUE
             /\ UNCHANGED <<current, first, second, released, captured, recovered, recoveryFirst>>
ThawFirst == /\ first = "closing" /\ ~released /\ released' = TRUE
             /\ UNCHANGED <<current, first, second, bound, captured, recovered, recoveryFirst>>
CaptureRecovery == /\ first = "closing" /\ current = 1 /\ captured = 0 /\ captured' = current
                   /\ UNCHANGED <<current, first, second, bound, released, recovered, recoveryFirst>>
ReleaseRecovery == /\ captured = 1 /\ ~recovered /\ CanWake(TRUE, bound, released)
                   /\ current' = IF OwnsMaintenance(current, captured) \/ UnsafeRecoveryRelease THEN 0 ELSE current
                   /\ recovered' = TRUE /\ recoveryFirst' = (first = "closing" /\ current = 1)
                   /\ UNCHANGED <<first, second, bound, released, captured>>
CloseFirst == /\ first = "closing" /\ CanWake(TRUE, bound, released) /\ first' = "done"
              /\ current' = IF OwnsMaintenance(current, 1) \/ UnsafeOldClose THEN 0 ELSE current
              /\ UNCHANGED <<second, bound, released, captured, recovered, recoveryFirst>>
StartSecond == /\ current = 0 /\ first # "new" /\ second = "new"
               /\ second' = "active" /\ current' = 2
               /\ UNCHANGED <<first, bound, released, captured, recovered, recoveryFirst>>
CloseSecond == /\ second = "active" /\ OwnsMaintenance(current, 2)
               /\ second' = "done" /\ current' = 0
               /\ UNCHANGED <<first, bound, released, captured, recovered, recoveryFirst>>

Next == StartFirst \/ CommitFirst \/ BindFirst \/ ThawFirst \/ CaptureRecovery
        \/ ReleaseRecovery \/ CloseFirst \/ StartSecond \/ CloseSecond \/ UNCHANGED vars
TypeOK == /\ current \in 0..2 /\ first \in {"new", "active", "closing", "done"}
          /\ second \in {"new", "active", "done"} /\ captured \in 0..1
          /\ \A flag \in {bound, released, recovered, recoveryFirst}: flag \in BOOLEAN
NewOwnerProtected == second = "active" => current = 2
RecoveryWitness == ~RequireWitness \/ ~(first = "done" /\ second = "active" /\ recovered /\ recoveryFirst)
Spec == Init /\ [][Next]_vars
=============================================================================
