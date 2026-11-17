---------------- MODULE MemoryBackendSwap ----------------
EXTENDS Naturals, TLC, MemorySwapReducer

CONSTANTS UnsafePublish, UnsafeRetiredWrite, UnsafeRollback, UnsafeCollect, RequireWitness
Docs == {"x", "y"}
Values == {"absent", "one", "two"}
VARIABLES phase, head, original, candidate, seq, cursor, fenced, indexed,
          committed, sourceAtCommit, candidateAtCommit, pinned, collected,
          crashed, crashes, bound, indexFailed, newWrites
vars == <<phase, head, original, candidate, seq, cursor, fenced, indexed,
          committed, sourceAtCommit, candidateAtCommit, pinned, collected,
          crashed, crashes, bound, indexFailed, newWrites>>
Facts == <<head = "old", cursor = seq, fenced, head = "new", indexed>>

Init == /\ phase = "preparing" /\ head = "old"
        /\ original = [d \in Docs |-> "one"] /\ candidate = [d \in Docs |-> "absent"]
        /\ seq = 0 /\ cursor = 0 /\ fenced = FALSE /\ indexed = FALSE
        /\ committed = FALSE /\ sourceAtCommit = original /\ candidateAtCommit = candidate
        /\ pinned = TRUE /\ collected = FALSE /\ crashed = FALSE /\ crashes = 0
        /\ bound = TRUE /\ indexFailed = FALSE /\ newWrites = 0

Snapshot == /\ ~crashed /\ Step(phase, Facts, "snapshot", "copying")
            /\ phase' = "copying" /\ candidate' = original /\ cursor' = seq
            /\ UNCHANGED <<head, original, seq, fenced, indexed, committed, sourceAtCommit,
                           candidateAtCommit, pinned, collected, crashed, crashes, bound, indexFailed, newWrites>>
Advance(event, target) ==
    /\ ~crashed /\ Step(phase, Facts, event, target) /\ phase' = target
    /\ UNCHANGED <<head, original, candidate, seq, cursor, fenced, indexed, committed,
                   sourceAtCommit, candidateAtCommit, pinned, collected, crashed, crashes, bound, indexFailed, newWrites>>

OldWrite == \E d \in Docs, value \in Values:
    /\ ~crashed /\ seq < 2 /\ original[d] # value
    /\ ((head = "old" /\ ~fenced) \/ (UnsafeRetiredWrite /\ head # "old"))
    /\ original' = [original EXCEPT ![d] = value] /\ seq' = seq + 1
    /\ UNCHANGED <<phase, head, candidate, cursor, fenced, indexed, committed,
                   sourceAtCommit, candidateAtCommit, pinned, collected, crashed, crashes, bound, indexFailed, newWrites>>

Replay == /\ ~crashed /\ phase = "catching_up"
          /\ candidate' = original /\ cursor' = seq
          /\ UNCHANGED <<phase, head, original, seq, fenced, indexed, committed,
                         sourceAtCommit, candidateAtCommit, pinned, collected, crashed, crashes, bound, indexFailed, newWrites>>
Fence == /\ ~crashed /\ phase = "catching_up" /\ head = "old" /\ ~fenced
         /\ fenced' = TRUE
         /\ UNCHANGED <<phase, head, original, candidate, seq, cursor, indexed, committed,
                        sourceAtCommit, candidateAtCommit, pinned, collected, crashed, crashes, bound, indexFailed, newWrites>>
Verify == /\ candidate = original /\ Advance("verified", "ready")

Publish ==
    /\ ~crashed
    /\ (CanPublish(phase, Facts) \/
        (UnsafePublish /\ phase = "catching_up" /\ cursor < seq /\ candidate # original))
    /\ head' = "new" /\ committed' = TRUE /\ phase' = "committed"
    /\ (UnsafePublish \/ Step(phase, <<FALSE, TRUE, TRUE, TRUE, indexed>>, "commit", "committed"))
    /\ sourceAtCommit' = original /\ candidateAtCommit' = candidate /\ bound' = FALSE
    /\ UNCHANGED <<original, candidate, seq, cursor, fenced, indexed, pinned, collected,
                   crashed, crashes, indexFailed, newWrites>>

CompetingPublish ==
    /\ head \in {"old", "new"} /\ ~fenced
    /\ head' = "other" /\ sourceAtCommit' = IF head = "old" THEN original ELSE sourceAtCommit
    /\ UNCHANGED <<phase, original, candidate, seq, cursor, fenced, indexed, committed,
                   candidateAtCommit, pinned, collected, crashed, crashes, bound, indexFailed, newWrites>>

Bind == /\ ~crashed /\ head = "new" /\ ~bound /\ bound' = TRUE /\ fenced' = FALSE
        /\ UNCHANGED <<phase, head, original, candidate, seq, cursor, indexed, committed,
                       sourceAtCommit, candidateAtCommit, pinned, collected, crashed, crashes, indexFailed, newWrites>>
NewWrite == \E d \in Docs, value \in Values:
    /\ ~crashed /\ head = "new" /\ bound /\ newWrites = 0 /\ candidate[d] # value
    /\ candidate' = [candidate EXCEPT ![d] = value] /\ newWrites' = 1
    /\ UNCHANGED <<phase, head, original, seq, cursor, fenced, indexed, committed,
                   sourceAtCommit, candidateAtCommit, pinned, collected, crashed, crashes, bound, indexFailed>>

IndexFailure == /\ ~crashed /\ phase = "indexing" /\ ~indexFailed
                /\ indexFailed' = TRUE /\ head' = IF UnsafeRollback THEN "old" ELSE head
                /\ UNCHANGED <<phase, original, candidate, seq, cursor, fenced, indexed, committed,
                               sourceAtCommit, candidateAtCommit, pinned, collected, crashed, crashes, bound, newWrites>>
Index == /\ ~crashed /\ head = "new" /\ phase = "indexing" /\ bound /\ ~indexed /\ indexed' = TRUE
         /\ UNCHANGED <<phase, head, original, candidate, seq, cursor, fenced, committed,
                        sourceAtCommit, candidateAtCommit, pinned, collected, crashed, crashes, bound, indexFailed, newWrites>>

Crash == /\ ~crashed /\ crashes = 0 /\ crashed' = TRUE /\ crashes' = 1 /\ bound' = FALSE
         /\ UNCHANGED <<phase, head, original, candidate, seq, cursor, fenced, indexed, committed,
                        sourceAtCommit, candidateAtCommit, pinned, collected, indexFailed, newWrites>>
Recover ==
    /\ crashed /\ crashed' = FALSE /\ bound' = TRUE /\ fenced' = FALSE
    /\ IF head = "old" THEN
           /\ Step(phase, Facts, "retry", "preparing") /\ phase' = "preparing"
       ELSE phase' = phase
    /\ UNCHANGED <<head, original, candidate, seq, cursor, indexed, committed,
                   sourceAtCommit, candidateAtCommit, pinned, collected, crashes, indexFailed, newWrites>>

ReleaseReader == /\ pinned /\ pinned' = FALSE
                 /\ UNCHANGED <<phase, head, original, candidate, seq, cursor, fenced, indexed, committed,
                                sourceAtCommit, candidateAtCommit, collected, crashed, crashes, bound, indexFailed, newWrites>>
Collect == /\ head # "old" /\ ~collected /\ (~pinned \/ UnsafeCollect) /\ collected' = TRUE
           /\ UNCHANGED <<phase, head, original, candidate, seq, cursor, fenced, indexed, committed,
                          sourceAtCommit, candidateAtCommit, pinned, crashed, crashes, bound, indexFailed, newWrites>>

Next == Snapshot \/ Advance("copied", "catching_up") \/ OldWrite \/ Replay \/ Fence \/ Verify \/ Publish
        \/ Bind \/ NewWrite \/ Advance("index", "indexing") \/ IndexFailure \/ Index
        \/ Advance("indexed", "complete") \/ Advance("supersede", "superseded")
        \/ Crash \/ Recover \/ CompetingPublish \/ ReleaseReader \/ Collect \/ UNCHANGED vars

TypeOK == /\ phase \in Phases /\ head \in {"old", "new", "other"}
          /\ original \in [Docs -> Values] /\ candidate \in [Docs -> Values]
          /\ sourceAtCommit \in [Docs -> Values] /\ candidateAtCommit \in [Docs -> Values]
          /\ seq \in 0..2 /\ cursor \in 0..2 /\ crashes \in 0..1 /\ newWrites \in 0..1
          /\ \A b \in {fenced, indexed, committed, pinned, collected, crashed, bound, indexFailed}: b \in BOOLEAN
NoLostWrites == committed => candidateAtCommit = sourceAtCommit
CommitIsAuthoritative == committed => head # "old"
RetiredReadOnly == head # "old" => original = sourceAtCommit
ReaderPinSafe == pinned => ~collected
RecoveryWitness == ~RequireWitness \/
    ~(phase = "complete" /\ crashes = 1 /\ seq > 0 /\ newWrites = 1 /\ indexFailed)
Spec == Init /\ [][Next]_vars
=============================================================================
