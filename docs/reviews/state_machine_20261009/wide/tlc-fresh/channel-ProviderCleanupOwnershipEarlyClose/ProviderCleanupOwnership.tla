----------------------- MODULE ProviderCleanupOwnership -----------------------
EXTENDS Naturals, FiniteSets
\* Ordered teardown: 0 transports, 1 detach, 2 callbacks, 3 import handles.
\* Two attempts per layer suffice to expose a lost owner or duplicated task.
CONSTANTS RetainOwner, WaitTransport, JoinPending, ConsumeReceipt
VARIABLES stage, owner, phase, inflight, starts, completions, finished
vars == <<stage, owner, phase, inflight, starts, completions, finished>>
Init == /\ stage = 0 /\ owner = TRUE /\ phase = "idle" /\ inflight = 0
        /\ starts = [s \in 0..3 |-> 0] /\ completions = [s \in 0..3 |-> 0]
        /\ finished = {}
Begin == /\ stage < 4 /\ owner /\ phase = "idle" /\ starts[stage] < 2
         /\ phase' = "pending" /\ inflight' = inflight + 1
         /\ starts' = [starts EXCEPT ![stage] = @ + 1]
         /\ UNCHANGED <<stage, owner, completions, finished>>
Success == /\ phase = "pending" /\ phase' = "completed"
           /\ inflight' = inflight - 1
           /\ completions' = [completions EXCEPT ![stage] = @ + 1]
           /\ UNCHANGED <<stage, owner, starts, finished>>
Failure == /\ phase = "pending" /\ phase' = "idle"
           /\ inflight' = inflight - 1 /\ owner' = RetainOwner
           /\ UNCHANGED <<stage, starts, completions, finished>>
TimeoutOrCancel == /\ phase = "pending"
                   /\ phase' = IF JoinPending THEN "pending" ELSE "idle"
                   /\ owner' = RetainOwner
                   /\ UNCHANGED <<stage, inflight, starts, completions, finished>>
CancelAfterCompletion == /\ phase = "completed"
                         /\ phase' = IF ConsumeReceipt THEN "completed" ELSE "idle"
                         /\ UNCHANGED <<stage, owner, inflight, starts, completions, finished>>
Consume == /\ phase = "completed" /\ stage' = stage + 1
           /\ finished' = finished \cup {stage} /\ phase' = "idle"
           /\ owner' = (stage # 3)
           /\ UNCHANGED <<inflight, starts, completions>>
SkipTransport == /\ ~WaitTransport /\ stage = 0 /\ phase = "idle"
                 /\ stage' = 1
                 /\ UNCHANGED <<owner, phase, inflight, starts, completions, finished>>
Next == Begin \/ Success \/ Failure \/ TimeoutOrCancel \/ CancelAfterCompletion \/ Consume \/ SkipTransport
Spec == Init /\ [][Next]_vars
TypeOK == /\ stage \in 0..4 /\ owner \in BOOLEAN
          /\ phase \in {"idle", "pending", "completed"} /\ inflight \in 0..8
          /\ starts \in [0..3 -> 0..2] /\ completions \in [0..3 -> 0..2]
          /\ finished \subseteq 0..3
CleanupOwned == stage < 4 \/ inflight > 0 => owner
SinglePending == inflight <= 1
TransportBeforeClose == 2 \in finished => 0 \in finished
NoRepeatedSuccess == \A s \in 0..3: completions[s] <= 1
=============================================================================
