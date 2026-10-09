----------------------- MODULE PromptCacheAdmission -----------------------
EXTENDS Naturals, Sequences, FiniteSets, TLC

\* Two outstanding plans in one explicit-cache scope. Separate from the
\* single-planned-request payload/marker model in PromptCacheTail.
CONSTANT ReusePlannedSequence
Attempts == {"a", "b"}
Position(a) == IF a = "a" THEN 2 ELSE 3
VARIABLES epoch, phase, planEpoch, planSequence, observed, admitted, sequence, tails
vars == <<epoch, phase, planEpoch, planSequence, observed, admitted, sequence, tails>>
Init == /\ epoch = 0 /\ sequence = 0 /\ tails = <<>>
        /\ phase = [a \in Attempts |-> "new"]
        /\ planEpoch = [a \in Attempts |-> 0]
        /\ planSequence = [a \in Attempts |-> 0]
        /\ observed = {} /\ admitted = {}
Prepare(a) == /\ phase[a] = "new"
              /\ phase' = [phase EXCEPT ![a] = "prepared"]
              /\ planEpoch' = [planEpoch EXCEPT ![a] = epoch]
              /\ planSequence' = [planSequence EXCEPT ![a] =
                   IF ReusePlannedSequence THEN sequence + 1
                   ELSE Cardinality({p \in Attempts: phase[p] = "prepared"}) + 1]
              /\ UNCHANGED <<epoch, observed, admitted, sequence, tails>>
Submit(a) == /\ phase[a] = "prepared" /\ a \notin observed
             /\ observed' = observed \cup {a}
             /\ LET current == planEpoch[a] = epoch
                    commit == current /\ (~ReusePlannedSequence \/ planSequence[a] > sequence)
                IN /\ admitted' = IF current THEN admitted \cup {a} ELSE admitted
                   /\ sequence' = IF commit THEN sequence + 1 ELSE sequence
                   /\ tails' = IF commit /\ (tails = <<>> \/ Position(a) > tails[Len(tails)])
                               THEN Append(tails, Position(a)) ELSE tails
             /\ UNCHANGED <<epoch, phase, planEpoch, planSequence>>
Invalidate == /\ epoch = 0 /\ epoch' = 1 /\ tails' = <<>>
              /\ UNCHANGED <<phase, planEpoch, planSequence, observed, admitted, sequence>>
Next == Invalidate \/ \E a \in Attempts: Prepare(a) \/ Submit(a)
Spec == Init /\ [][Next]_vars
TypeOK == /\ epoch \in 0..1 /\ sequence \in 0..2
          /\ phase \in [Attempts -> {"new", "prepared"}]
          /\ observed \subseteq Attempts /\ admitted \subseteq observed
SubmittedCountExact == sequence = Cardinality(admitted)
CurrentTailsOnly == \A i \in 1..Len(tails):
    \E a \in admitted: planEpoch[a] = epoch /\ Position(a) = tails[i]
MonotonicTail == \A i,j \in 1..Len(tails): i < j => tails[i] < tails[j]
=============================================================================
