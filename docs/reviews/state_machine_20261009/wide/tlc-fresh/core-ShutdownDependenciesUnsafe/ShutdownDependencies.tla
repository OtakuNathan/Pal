-------------------------- MODULE ShutdownDependencies --------------------------
EXTENDS Naturals, FiniteSets
CONSTANT ContinueOnFailure
VARIABLES phase, live, blocked
vars == <<phase, live, blocked>>
Resources == {"dependent", "plugin", "provider", "resident", "database"}
ResourceAt == <<"dependent", "plugin", "provider", "resident", "database">>
Init == /\ phase = 0 /\ live = Resources /\ blocked = FALSE
Close == /\ phase < 5 /\ ~blocked
         /\ live' = live \ {ResourceAt[phase + 1]} /\ phase' = phase + 1
         /\ UNCHANGED blocked
Fail == /\ phase < 4 /\ ~blocked /\ blocked' = TRUE
        /\ UNCHANGED <<phase, live>>
Retry == /\ blocked /\ blocked' = FALSE /\ UNCHANGED <<phase, live>>
IgnoreFailure == /\ ContinueOnFailure /\ blocked /\ phase' = phase + 1
                 /\ blocked' = FALSE /\ UNCHANGED live
Next == Close \/ Fail \/ Retry \/ IgnoreFailure
Spec == Init /\ [][Next]_vars
TypeOK == /\ phase \in 0..5 /\ live \subseteq Resources /\ blocked \in BOOLEAN
DependenciesAlive == /\ ("dependent" \in live => "plugin" \in live)
                     /\ (live \cap {"plugin", "provider"} # {} => "resident" \in live)
                     /\ (live \ {"database"} # {} => "database" \in live)
=============================================================================
