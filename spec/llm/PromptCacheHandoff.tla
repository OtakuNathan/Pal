------------------------- MODULE PromptCacheHandoff -------------------------
EXTENDS Naturals, Integers, FiniteSets, TLC
CONSTANTS DropProtection, Optimistic, StaleOwner, IgnoreM, ClearThird, HiddenMarker,
          CircularEvidence
VARIABLES owner, pending, calibrated, attempts, sentOwner, sentSeq, seq,
          cache, markers, served, h, bound, accepted, ackSource, phase, turns,
          through, after, remaining, settled, lostThird, target, estimateError, low, high
vars == <<owner,pending,calibrated,attempts,sentOwner,sentSeq,seq,cache,markers,
          served,h,bound,accepted,ackSource,phase,turns,through,after,remaining,
          settled,lostThird,target,estimateError,low,high>>
Init == /\ owner=0 /\ pending=TRUE /\ calibrated=0 /\ attempts=0
        /\ sentOwner=0 /\ sentSeq=0 /\ seq=0 /\ cache=IF HiddenMarker THEN {1,2} ELSE {1}
        /\ markers={} /\ served=0 /\ h=0 /\ bound \in {1,2,4}
        /\ accepted=1 /\ ackSource=0 /\ phase="ready" /\ turns=0
        /\ through=0 /\ after=0 /\ remaining=0 /\ settled={}
        /\ lostThird=FALSE /\ target=4 /\ estimateError \in {-1,0,1}
        /\ low=0 /\ high=0
Submit == /\ phase="ready" /\ pending /\ attempts<3
          /\ markers' = (IF DropProtection THEN {} ELSE {1}) \cup {3}
                           \cup (IF HiddenMarker THEN {2} ELSE {})
          /\ target' \in {3,4,5}
          /\ sentOwner'=owner /\ seq'=seq+1 /\ sentSeq'=seq+1
          /\ attempts'=attempts+1 /\ phase'="sent"
          /\ pending'=IF ClearThird /\ attempts=2 THEN FALSE ELSE pending
          /\ accepted'=IF Optimistic THEN 3 ELSE accepted
          /\ ackSource'=IF Optimistic THEN 1 ELSE ackSource
          /\ UNCHANGED <<calibrated,cache,served,h,bound,turns,through,after,
                          remaining,settled,lostThird,owner,estimateError,low,high>>
Provider == /\ phase="sent"
            /\ served' \in ({0} \cup (cache \cap markers))
            /\ \A x \in (cache \cap markers): served' >= x
            /\ h'=served' /\ cache'=cache \cup {3}
            /\ phase'="response"
            /\ UNCHANGED <<owner,pending,calibrated,attempts,sentOwner,sentSeq,
                            seq,markers,bound,accepted,ackSource,turns,through,
                            after,remaining,settled,lostThird,target,estimateError,low,high>>
(* True positions are S=1, C=3, hidden=2. Local error is independent of
   provider residency, target/input and the independently valid upper bound M.
   Local ACK never inspects cache or served. *)
LocalAck == /\ pending /\ calibrated>0 /\ sentSeq>calibrated
            /\ (sentOwner=owner \/ StaleOwner)
            /\ h>=low /\ h<=high
            /\ (h>(IF CircularEvidence THEN h-1 ELSE bound) \/ IgnoreM)
Observe == /\ phase="response" /\ sentSeq \notin settled
           /\ settled'=settled \cup {sentSeq}
           /\ lostThird'=(ClearThird /\ attempts=3 /\ calibrated>0
                         /\ sentSeq>calibrated /\ sentOwner=owner
                         /\ h>bound /\ h>=low /\ h<=high)
           /\ accepted'=IF LocalAck THEN 3 ELSE accepted
           /\ ackSource'=IF LocalAck THEN served ELSE ackSource
           /\ low'=IF pending /\ calibrated=0 /\ sentOwner=owner
                       THEN target - (target-3+estimateError) - 1 ELSE low
           /\ high'=IF pending /\ calibrated=0 /\ sentOwner=owner
                        THEN target - (target-3+estimateError) + 1 ELSE high
           /\ calibrated'=IF pending /\ calibrated=0 /\ sentOwner=owner
                            THEN sentSeq ELSE calibrated
           /\ pending'=IF LocalAck \/ attempts=3 THEN FALSE ELSE pending
           /\ through'=IF LocalAck THEN 0 ELSE through+2
           /\ after'=after+target-3
           /\ remaining'=IF LocalAck THEN after+target-3 ELSE through+after+target-1
           /\ phase'=IF LocalAck \/ attempts=3 \/ ~pending THEN "done" ELSE "ready"
           /\ UNCHANGED <<owner,attempts,sentOwner,sentSeq,seq,cache,markers,
                           served,h,bound,turns,target,estimateError>>
Turn == /\ turns<2 /\ phase \in {"ready","response"}
        /\ owner'=owner+1 /\ turns'=turns+1
        /\ pending'=IF StaleOwner THEN pending ELSE FALSE
        /\ phase'=IF phase="ready" THEN "done" ELSE phase
        /\ UNCHANGED <<calibrated,attempts,sentOwner,sentSeq,seq,cache,markers,
                        served,h,bound,accepted,ackSource,through,after,
                        remaining,settled,lostThird,target,estimateError,low,high>>
Expire == /\ phase="ready" /\ pending
          /\ pending'=FALSE /\ phase'="done"
          /\ UNCHANGED <<owner,calibrated,attempts,sentOwner,sentSeq,seq,cache,
                          markers,served,h,bound,accepted,ackSource,turns,
                          through,after,remaining,settled,lostThird,target,estimateError,low,high>>
Evict == /\ phase="ready"
         /\ cache' \in SUBSET (IF HiddenMarker THEN {1,2,3} ELSE {1,3})
         /\ UNCHANGED <<owner,pending,calibrated,attempts,sentOwner,sentSeq,seq,
                         markers,served,h,bound,accepted,ackSource,phase,turns,
                         through,after,remaining,settled,lostThird,target,
                         estimateError,low,high>>
Next == Submit \/ Provider \/ Observe \/ Turn \/ Expire \/ Evict
Spec == Init /\ [][Next]_vars /\ WF_vars(Submit) /\ WF_vars(Provider)
        /\ WF_vars(Observe) /\ WF_vars(Expire)
Termination == <> (phase="done")
SoundAck == accepted=3 => ackSource=3
Protection == phase \in {"sent","response"} => 1 \in markers
MarkerBudget == Cardinality(markers)<=4
AttemptBudget == attempts<=3
ThirdResponse == ~lostThird
CostConservation == remaining=through+after
OwnerIsolation == (phase="done" /\ accepted=3) => sentOwner=owner
=============================================================================
