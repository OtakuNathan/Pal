------------------- MODULE BackgroundHandoffLifecycle -------------------
EXTENDS Naturals
VARIABLES queued, cursor, spawned, terminal, published, volatile, checkpoint,
          consumed, channel, confirmed
vars == <<queued,cursor,spawned,terminal,published,volatile,checkpoint,consumed,channel,confirmed>>
Init ==
    /\ queued = FALSE /\ cursor = FALSE /\ spawned = FALSE
    /\ terminal = FALSE /\ published = FALSE /\ volatile = FALSE
    /\ checkpoint = FALSE /\ consumed = FALSE
    /\ channel = "none" /\ confirmed = 0
Admit ==
    /\ ~queued /\ queued' = TRUE /\ cursor' = TRUE
    /\ UNCHANGED <<spawned,terminal,published,volatile,checkpoint,consumed,channel,confirmed>>
Dispatch ==
    /\ queued /\ ~spawned /\ spawned' = TRUE
    /\ UNCHANGED <<queued,cursor,terminal,published,volatile,checkpoint,consumed,channel,confirmed>>
Finish ==
    /\ spawned /\ ~terminal /\ terminal' = TRUE
    /\ UNCHANGED <<queued,cursor,spawned,published,volatile,checkpoint,consumed,channel,confirmed>>
Publish ==
    /\ terminal /\ ~published /\ published' = TRUE
    /\ channel' = "pending"
    /\ UNCHANGED <<queued,cursor,spawned,terminal,volatile,checkpoint,consumed,confirmed>>
Observe ==
    /\ terminal /\ ~checkpoint /\ ~volatile /\ ~consumed
    /\ volatile' = TRUE
    /\ UNCHANGED <<queued,cursor,spawned,terminal,published,checkpoint,consumed,channel,confirmed>>
Checkpoint ==
    /\ volatile /\ ~checkpoint /\ checkpoint' = TRUE
    /\ UNCHANGED <<queued,cursor,spawned,terminal,published,volatile,consumed,channel,confirmed>>
Ack ==
    /\ checkpoint /\ ~consumed /\ consumed' = TRUE
    /\ UNCHANGED <<queued,cursor,spawned,terminal,published,volatile,checkpoint,channel,confirmed>>
BeginSend ==
    /\ channel = "pending" /\ channel' = "sending"
    /\ UNCHANGED <<queued,cursor,spawned,terminal,published,volatile,checkpoint,consumed,confirmed>>
KnownFailure ==
    /\ channel = "sending" /\ channel' = "pending"
    /\ UNCHANGED <<queued,cursor,spawned,terminal,published,volatile,checkpoint,consumed,confirmed>>
Confirm ==
    /\ channel = "sending" /\ channel' = "delivered" /\ confirmed' = confirmed + 1
    /\ UNCHANGED <<queued,cursor,spawned,terminal,published,volatile,checkpoint,consumed>>
Crash ==
    /\ volatile' = checkpoint
    /\ channel' = IF channel = "sending" THEN "unknown" ELSE channel
    /\ UNCHANGED <<queued,cursor,spawned,terminal,published,checkpoint,consumed,confirmed>>
Next == Admit \/ Dispatch \/ Finish \/ Publish \/ Observe \/ Checkpoint \/ Ack
        \/ BeginSend \/ KnownFailure \/ Confirm \/ Crash
Spec == Init /\ [][Next]_vars
AdmissionAtomic == queued = cursor /\ (spawned => queued)
NoPrematureConsumption == consumed => checkpoint /\ terminal
OnlyActualResult == published => terminal /\ spawned
NoRepeatedConfirmation == confirmed \in 0..1 /\ (channel = "delivered" => confirmed = 1)
=============================================================================
