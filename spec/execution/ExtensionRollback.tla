------------------------- MODULE ExtensionRollback -------------------------
EXTENDS Naturals, TLC
\* Restoration is fallible independently of candidate close. The lifecycle
\* writer is assumed held; entry tokens are withdrawn before either callback.
CONSTANT RetainBeforeRestore
VARIABLES phase, current, originalLive, candidateLive, owner, closed
vars == <<phase, current, originalLive, candidateLive, owner, closed>>
Init == /\ phase = "staging" /\ current = "original"
        /\ originalLive = TRUE /\ candidateLive = FALSE
        /\ owner = TRUE /\ closed = FALSE
Publish == /\ phase = "staging" /\ phase' = "published"
           /\ current' = "candidate" /\ originalLive' = FALSE /\ candidateLive' = TRUE
           /\ UNCHANGED <<owner, closed>>
FailStaging == /\ phase = "staging" /\ phase' = "closing"
               /\ UNCHANGED <<current, originalLive, candidateLive, owner, closed>>
FailActivation == /\ phase = "published" /\ phase' = "restoring"
                  /\ candidateLive' = FALSE
                  /\ UNCHANGED <<current, originalLive, owner, closed>>
RestoreFailure == /\ phase = "restoring" /\ phase' = "restore_failed"
                  /\ owner' = RetainBeforeRestore
                  /\ UNCHANGED <<current, originalLive, candidateLive, closed>>
RetryRestore == /\ phase = "restore_failed" /\ owner /\ phase' = "restoring"
                /\ UNCHANGED <<current, originalLive, candidateLive, owner, closed>>
RestoreSuccess == /\ phase = "restoring" /\ phase' = "closing"
                  /\ current' = "original" /\ originalLive' = TRUE
                  /\ UNCHANGED <<candidateLive, owner, closed>>
CloseFailure == /\ phase = "closing" /\ phase' = "close_failed"
                /\ UNCHANGED <<current, originalLive, candidateLive, owner, closed>>
RetryClose == /\ phase = "close_failed" /\ phase' = "closing"
              /\ UNCHANGED <<current, originalLive, candidateLive, owner, closed>>
CloseSuccess == /\ phase = "closing" /\ phase' = "done"
                /\ owner' = FALSE /\ closed' = TRUE
                /\ UNCHANGED <<current, originalLive, candidateLive>>
Next == Publish \/ FailStaging \/ FailActivation \/ RestoreFailure \/ RetryRestore
        \/ RestoreSuccess \/ CloseFailure \/ RetryClose \/ CloseSuccess
Spec == Init /\ [][Next]_vars
TypeOK == /\ phase \in {"staging", "published", "restoring", "restore_failed", "closing", "close_failed", "done"}
          /\ current \in {"original", "candidate"}
          /\ originalLive \in BOOLEAN /\ candidateLive \in BOOLEAN
          /\ owner \in BOOLEAN /\ closed \in BOOLEAN
CleanupOwned == ~closed => owner
FailedCandidateWithdrawn == phase \notin {"staging", "published"} => ~candidateLive
RestoreBeforeClose == phase \in {"closing", "close_failed", "done"} =>
    current = "original" /\ originalLive /\ ~candidateLive
=============================================================================
