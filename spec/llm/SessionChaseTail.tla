-------------------------- MODULE SessionChaseTail --------------------------
EXTENDS Naturals, FiniteSets, TLC
\* A small lookup window (fixed S/T plus two newest blocks) abstracts 52 slots.
\* Checkpoint selection is derived from the closed prefix, not transport receipts.
CONSTANT DropAnchors
VARIABLES end, turn, generation, plan, phase, admitted
vars == <<end, turn, generation, plan, phase, admitted>>
Markers(n) == (IF DropAnchors THEN {} ELSE {0,1}) \cup
              {i \in 2..n : i >= n-1}
Init == /\ end=1 /\ turn=0 /\ generation=0 /\ phase="idle" /\ admitted=FALSE
        /\ plan=[end |-> 1, generation |-> 0, markers |-> {}]
Append == /\ end<6 /\ end'=end+1
          /\ UNCHANGED <<turn,generation,plan,phase,admitted>>
EndTurn == /\ turn<2 /\ turn'=turn+1
           /\ UNCHANGED <<end,generation,plan,phase,admitted>>
Compact == /\ generation=0 /\ generation'=1 /\ end'=1
           /\ UNCHANGED <<turn,plan,phase,admitted>>
Build == /\ phase'="prepared" /\ admitted'=FALSE
         /\ plan'=[end |-> end, generation |-> generation, markers |-> Markers(end)]
         /\ UNCHANGED <<end,turn,generation>>
Submit == /\ phase="prepared" /\ phase'="admitted" /\ admitted'=TRUE
          /\ UNCHANGED <<end,turn,generation,plan>>
Reject == /\ phase="prepared" /\ phase'="rejected"
          /\ UNCHANGED <<end,turn,generation,plan,admitted>>
Next == Append \/ EndTurn \/ Compact \/ Build \/ Submit \/ Reject
Spec == Init /\ [][Next]_vars
FixedAnchors == phase="idle" \/ {0,1} \subseteq plan.markers
Bounded == Cardinality(plan.markers)<=4
Latest == phase="idle" \/ {i \in 2..plan.end: i>=plan.end-1} \subseteq plan.markers
ClosedOnly == plan.markers \subseteq 0..plan.end
=============================================================================
