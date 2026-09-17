---------------------- MODULE PromptCacheSettlement ----------------------
EXTENDS Integers, FiniteSets
CONSTANTS ClearAllOnAck, DropLateEstimate, DoubleCharge, ClearOnAnchor
VARIABLES baseline, pending, through, after, remaining, accounted, comparable,
          paid, epoch, lastSequence, afterU
vars == <<baseline,pending,through,after,remaining,accounted,comparable,paid,
          epoch,lastSequence,afterU>>
Requests == {0,1,2,3}
Target == [i \in Requests |-> CASE i=0 -> 6 [] i=1 -> 7 [] i=2 -> 8 [] OTHER -> 5]
Max(a,b) == IF a>b THEN a ELSE b
Min(a,b) == IF a<b THEN a ELSE b
(* Post-proposal snapshot: request 0 was already settled at target C=6.
   It witnesses C >= the greatest previously accumulated target. The three
   outstanding requests can settle in any order; each belongs to epoch 0. *)
Init == /\ baseline=4 /\ pending=TRUE /\ through=2 /\ after=0 /\ remaining=0
        /\ afterU=1 /\ accounted={0} /\ comparable={0} /\ paid=1 /\ epoch=0 /\ lastSequence=0
Settle(i) ==
    /\ i \in Requests \ accounted
    /\ accounted'=accounted \cup {i} /\ paid'=paid+1
    /\ comparable'=IF epoch=0 THEN comparable \cup {i} ELSE comparable
    /\ lastSequence'=Max(lastSequence,i)
    /\ IF epoch=0 /\ ~(DropLateEstimate /\ i<lastSequence)
       THEN /\ afterU'=afterU+Max(0,Target[i]-Max(baseline,5))
            /\ through'=IF pending THEN through+Max(0,Min(Target[i],6)-baseline) ELSE through
            /\ after'=IF pending THEN after+Max(0,Target[i]-6) ELSE after
            /\ remaining'=IF pending THEN remaining ELSE remaining+Max(0,Target[i]-baseline)
       ELSE UNCHANGED <<through,after,remaining,afterU>>
    /\ UNCHANGED <<baseline,pending,epoch>>
(* Acknowledge receives an authorized handoff event. Its attribution guard is
   checked in PromptCacheHandoff; this model checks only interval settlement. *)
Acknowledge == /\ pending /\ epoch=0 /\ (accounted \cap {1,2}) # {}
               /\ baseline'=6 /\ pending'=FALSE /\ afterU'=after
               /\ remaining'=IF ClearAllOnAck THEN 0 ELSE after
               /\ through'=0 /\ after'=0
               /\ UNCHANGED <<accounted,comparable,paid,epoch,lastSequence>>
Abandon == /\ pending /\ pending'=FALSE /\ remaining'=through+after
           /\ through'=0 /\ after'=0
           /\ UNCHANGED <<baseline,accounted,comparable,paid,epoch,lastSequence,afterU>>
Invalidate == /\ epoch=0 /\ epoch'=1 /\ pending'=FALSE
              /\ afterU'=0 /\ comparable'={} /\ through'=0 /\ after'=0 /\ remaining'=0
              /\ UNCHANGED <<baseline,accounted,paid,lastSequence>>
Duplicate == /\ accounted # {} /\ paid'=paid+(IF DoubleCharge THEN 1 ELSE 0)
             /\ UNCHANGED <<baseline,pending,through,after,remaining,accounted,
                             comparable,epoch,lastSequence,afterU>>
ConfirmAnchor == /\ baseline=4 /\ epoch=0
                 /\ baseline'=5
                 /\ through'=IF pending THEN (IF ClearOnAnchor THEN 0 ELSE afterU-after) ELSE through
                 /\ remaining'=IF pending THEN remaining ELSE (IF ClearOnAnchor THEN 0 ELSE afterU)
                 /\ UNCHANGED <<pending,after,afterU,accounted,comparable,paid,epoch,lastSequence>>
Next == (\E i \in Requests: Settle(i)) \/ ConfirmAnchor \/ Acknowledge \/ Abandon \/ Invalidate \/ Duplicate
Spec == Init /\ [][Next]_vars /\ (\A i \in Requests: WF_vars(Settle(i)))
Debt(i) == IF i \in comparable THEN Max(0,Target[i]-baseline) ELSE 0
Conservation == (IF pending THEN through+after ELSE remaining) = Debt(0)+Debt(1)+Debt(2)+Debt(3)
BillingIdempotent == paid=Cardinality(accounted)
Nonnegative == through>=0 /\ after>=0 /\ remaining>=0
AllReceiptsSettle == <> (accounted=Requests)
=============================================================================
