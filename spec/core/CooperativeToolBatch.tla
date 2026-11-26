------------------------- MODULE CooperativeToolBatch -------------------------
EXTENDS Naturals, TLC
\* Normal interrupt is a latch after acceptance, never cancellation of a tool.
CONSTANT EarlyClose
VARIABLES phase, done, stop, requests, accepted
vars == <<phase,done,stop,requests,accepted>>
Init == /\ phase="model" /\ done=0 /\ stop=FALSE /\ requests=1 /\ accepted=FALSE
Accept == /\ phase="model" /\ phase'="batch" /\ done'=0 /\ accepted'=TRUE
          /\ UNCHANGED <<stop,requests>>
Interrupt == /\ phase \in {"model","batch"} /\ stop'=TRUE
             /\ phase'=IF phase="model" \/ EarlyClose THEN "closed" ELSE phase
             /\ UNCHANGED <<done,requests,accepted>>
ToolDone == /\ phase="batch" /\ done<2 /\ done'=done+1
            /\ UNCHANGED <<phase,stop,requests,accepted>>
Finish == /\ phase="batch" /\ done=2
          /\ phase'=IF stop \/ requests=2 THEN "closed" ELSE "model"
          /\ requests'=IF stop \/ requests=2 THEN requests ELSE requests+1
          /\ accepted'=(phase'="closed")
          /\ UNCHANGED <<done,stop>>
Next == Accept \/ Interrupt \/ ToolDone \/ Finish
Spec == Init /\ [][Next]_vars
NoPartialClose == (phase="closed" /\ accepted) => done=2
NoRequestAfterStop == [][stop => requests'=requests]_vars
=============================================================================
