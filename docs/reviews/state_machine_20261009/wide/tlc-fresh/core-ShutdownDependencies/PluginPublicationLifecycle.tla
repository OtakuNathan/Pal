---------------------- MODULE PluginPublicationLifecycle ---------------------
EXTENDS Naturals, TLC

(* PluginHost exposes a staged generation to replay callbacks before commit.
   A failed publication must release its maps after successful rollback.
   Cleanup failure deliberately retains ownership, with capabilities withdrawn,
   until detach retries the cleanup. This is not an attached generation. *)
CONSTANTS RetainRolledBackGeneration, RetainEarlyCleanupOwner, CloseBeforeWithdrawal
VARIABLES phase, generation, handleMap, surface, pendingCleanup, attached, owner,
          callbacks, resourcesClosed
vars == <<phase, generation, handleMap, surface, pendingCleanup, attached, owner,
          callbacks, resourcesClosed>>

Init == /\ phase = "idle"
        /\ generation = FALSE /\ handleMap = FALSE /\ surface = FALSE
        /\ pendingCleanup = FALSE /\ attached = FALSE /\ owner = FALSE
        /\ callbacks = FALSE /\ resourcesClosed = TRUE
Start == /\ phase = "idle" /\ phase' = "preparing"
         /\ owner' = TRUE /\ pendingCleanup' = TRUE
         /\ resourcesClosed' = FALSE
         /\ UNCHANGED <<generation, handleMap, surface, attached, callbacks>>
Stage == /\ phase = "preparing"
         /\ phase' = "binding"
         /\ generation' = TRUE /\ handleMap' = TRUE /\ surface' = TRUE
         /\ pendingCleanup' = TRUE /\ attached' = FALSE
         /\ callbacks' = TRUE
         /\ UNCHANGED <<owner, resourcesClosed>>
Advance == /\ phase \in {"binding", "replay", "subscriptions"}
           /\ phase' = CASE phase = "binding" -> "replay"
                        [] phase = "replay" -> "subscriptions"
                        [] OTHER -> "status"
           /\ UNCHANGED <<generation, handleMap, surface, pendingCleanup, attached, owner, callbacks, resourcesClosed>>
Commit == /\ phase = "status"
          /\ phase' = "attached" /\ attached' = TRUE
          /\ UNCHANGED <<generation, handleMap, surface, pendingCleanup, owner, callbacks, resourcesClosed>>
Fail == /\ phase \in {"preparing", "binding", "replay", "subscriptions", "status", "attached"}
        /\ phase' = "withdrawing" /\ surface' = FALSE /\ attached' = FALSE
        /\ UNCHANGED <<generation, handleMap, pendingCleanup, owner, callbacks, resourcesClosed>>
WithdrawSuccess == /\ phase = "withdrawing" /\ phase' = "rollback"
                   /\ callbacks' = FALSE
                   /\ UNCHANGED <<generation, handleMap, surface, pendingCleanup, attached, owner, resourcesClosed>>
WithdrawFailure == /\ phase = "withdrawing" /\ phase' = "withdraw_failed"
                   /\ owner' = IF generation THEN owner ELSE RetainEarlyCleanupOwner
                   /\ UNCHANGED <<generation, handleMap, surface, pendingCleanup, attached, callbacks, resourcesClosed>>
RetryWithdrawal == /\ phase = "withdraw_failed" /\ phase' = "withdrawing"
                   /\ UNCHANGED <<generation, handleMap, surface, pendingCleanup, attached, owner, callbacks, resourcesClosed>>
UnsafeClose == /\ CloseBeforeWithdrawal /\ phase = "withdrawing" /\ callbacks
               /\ resourcesClosed' = TRUE /\ phase' = "withdraw_failed"
               /\ UNCHANGED <<generation, handleMap, surface, pendingCleanup, attached, owner, callbacks>>
CleanupFailure == /\ phase = "rollback"
                  /\ phase' = "cleanup_failed"
                  /\ owner' = IF generation THEN owner ELSE RetainEarlyCleanupOwner
                  /\ UNCHANGED <<generation, handleMap, surface, pendingCleanup, attached, callbacks, resourcesClosed>>
RetryCleanup == /\ phase = "cleanup_failed" /\ phase' = "rollback"
                /\ UNCHANGED <<generation, handleMap, surface, pendingCleanup, attached, owner, callbacks, resourcesClosed>>
CleanupSuccess == /\ phase = "rollback" /\ phase' = "idle"
                  /\ pendingCleanup' = FALSE /\ owner' = FALSE
                  /\ generation' = RetainRolledBackGeneration
                  /\ handleMap' = RetainRolledBackGeneration
                  /\ resourcesClosed' = TRUE
                  /\ UNCHANGED <<surface, attached, callbacks>>
Next == Start \/ Stage \/ Advance \/ Commit \/ Fail \/ WithdrawSuccess \/ WithdrawFailure
        \/ RetryWithdrawal \/ UnsafeClose \/ CleanupFailure \/ RetryCleanup \/ CleanupSuccess
TypeOK == /\ phase \in {"idle", "preparing", "binding", "replay", "subscriptions", "status", "attached", "withdrawing", "withdraw_failed", "rollback", "cleanup_failed"}
          /\ generation \in BOOLEAN /\ handleMap \in BOOLEAN /\ surface \in BOOLEAN
          /\ pendingCleanup \in BOOLEAN /\ attached \in BOOLEAN /\ owner \in BOOLEAN
          /\ callbacks \in BOOLEAN /\ resourcesClosed \in BOOLEAN
PublicationAgrees == /\ generation = handleMap
                     /\ (attached => (generation /\ surface /\ phase = "attached"))
                     /\ (phase = "idle" => (~generation /\ ~handleMap /\ ~pendingCleanup /\ ~owner))
CleanupRetainsOwnership == phase \in {"cleanup_failed", "withdraw_failed"} =>
    (owner /\ pendingCleanup /\ ~surface /\ ~attached)
CleanupAfterWithdrawal == resourcesClosed => ~callbacks
Spec == Init /\ [][Next]_vars
=============================================================================
