------------------- MODULE BackgroundHandoffLifecycle -------------------
EXTENDS Naturals
VARIABLES queued, cursor, spawned, terminal, published, volatile, checkpoint,
          consumed, channel, confirmed, requested, prepared
vars == <<queued,cursor,spawned,terminal,published,volatile,checkpoint,consumed,channel,confirmed,requested,prepared>>
Init ==
    /\ queued = FALSE /\ cursor = FALSE /\ spawned = FALSE
    /\ terminal = FALSE /\ published = FALSE /\ volatile = FALSE
    /\ checkpoint = FALSE /\ consumed = FALSE
    /\ channel = "none" /\ confirmed = 0 /\ requested \in BOOLEAN /\ prepared = FALSE
Admit ==
    /\ ~queued /\ queued' = TRUE /\ cursor' = TRUE
    /\ UNCHANGED <<spawned,terminal,published,volatile,checkpoint,consumed,channel,confirmed,prepared>>
Dispatch ==
    /\ queued /\ ~spawned /\ spawned' = TRUE
    /\ UNCHANGED <<queued,cursor,terminal,published,volatile,checkpoint,consumed,channel,confirmed,prepared>>
Finish ==
    /\ spawned /\ ~terminal /\ terminal' = TRUE
    /\ UNCHANGED <<queued,cursor,spawned,published,volatile,checkpoint,consumed,channel,confirmed,prepared>>
Publish ==
    /\ terminal /\ ~published /\ published' = TRUE
    /\ channel' = IF requested THEN "pending" ELSE "not_requested"
    /\ UNCHANGED <<queued,cursor,spawned,terminal,volatile,checkpoint,consumed,confirmed,prepared>>
Observe ==
    /\ terminal /\ ~checkpoint /\ ~volatile /\ ~consumed
    /\ volatile' = TRUE
    /\ UNCHANGED <<queued,cursor,spawned,terminal,published,checkpoint,consumed,channel,confirmed,prepared>>
Checkpoint ==
    /\ volatile /\ ~checkpoint /\ checkpoint' = TRUE
    /\ UNCHANGED <<queued,cursor,spawned,terminal,published,volatile,consumed,channel,confirmed,prepared>>
Ack ==
    /\ checkpoint /\ ~consumed /\ consumed' = TRUE
    /\ UNCHANGED <<queued,cursor,spawned,terminal,published,volatile,checkpoint,channel,confirmed,prepared>>
(* Both newly published and legacy packets must be projected from the immutable receipt before a send. *)
PrepareChannel ==
    /\ published /\ channel = "pending" /\ ~prepared
    /\ prepared' = TRUE
    /\ UNCHANGED <<queued,cursor,spawned,terminal,published,volatile,checkpoint,consumed,channel,confirmed>>
BeginSend ==
    /\ channel = "pending" /\ prepared /\ channel' = "sending"
    /\ UNCHANGED <<queued,cursor,spawned,terminal,published,volatile,checkpoint,consumed,confirmed,prepared>>
KnownFailure ==
    /\ channel = "sending" /\ channel' = "pending" /\ prepared' = FALSE
    /\ UNCHANGED <<queued,cursor,spawned,terminal,published,volatile,checkpoint,consumed,confirmed>>
Confirm ==
    /\ channel = "sending" /\ channel' = "delivered" /\ confirmed' = confirmed + 1
    /\ UNCHANGED <<queued,cursor,spawned,terminal,published,volatile,checkpoint,consumed,prepared>>
Crash ==
    /\ volatile' = checkpoint
    /\ channel' = IF channel = "sending" THEN "unknown" ELSE channel
    /\ prepared' = FALSE
    /\ UNCHANGED <<queued,cursor,spawned,terminal,published,checkpoint,consumed,confirmed>>
Next == /\ UNCHANGED requested
        /\ (Admit \/ Dispatch \/ Finish \/ Publish \/ Observe \/ Checkpoint \/ Ack
             \/ PrepareChannel \/ BeginSend \/ KnownFailure \/ Confirm \/ Crash)
Spec == Init /\ [][Next]_vars
AdmissionAtomic == queued = cursor /\ (spawned => queued)
NoPrematureConsumption == consumed => checkpoint /\ terminal
OnlyActualResult == published => terminal /\ spawned
NoRepeatedConfirmation == confirmed \in 0..1 /\ (channel = "delivered" => confirmed = 1)
NoUnrequestedChannelDelivery ==
    ~requested => /\ channel \in {"none", "not_requested"} /\ confirmed = 0
NoUnpreparedSend == channel = "sending" => prepared
=============================================================================
