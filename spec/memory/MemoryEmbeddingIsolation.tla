---------------- MODULE MemoryEmbeddingIsolation ----------------
EXTENDS Naturals, TLC, MemorySwapReducer
CONSTANTS UnsafeReload, UnsafeStaleReply, UnsafeDimension, UnsafeReuse, RequireWitness
Spaces == {"a", "b"}
Dimensions == {2, 3}
VARIABLES stored, configured, dimension, vectors, pending, pendingDimension,
          candidate, candidateDimension, candidateVectors, querySpace, queryExpected, blocked
vars == <<stored, configured, dimension, vectors, pending, pendingDimension,
          candidate, candidateDimension, candidateVectors, querySpace, queryExpected, blocked>>
Known == stored \in Spaces
Matches == configured = stored
Writable == EmbeddingAccess(Known, Matches, TRUE, "write")

Init == /\ stored \in {"a", "unknown"} /\ configured = "a" /\ dimension = 2
        /\ vectors = {<<"a", 2>>} /\ pending = "none" /\ pendingDimension = 2
        /\ candidate = "none" /\ candidateDimension = 0 /\ candidateVectors = {}
        /\ querySpace = "none" /\ queryExpected = "none" /\ blocked = FALSE

Reload == \E space \in Spaces:
    /\ configured' = space
    /\ UNCHANGED <<stored, dimension, vectors, pending, pendingDimension, candidate,
                   candidateDimension, candidateVectors, querySpace, queryExpected, blocked>>
ObserveBlocked == /\ ~Writable /\ blocked' = TRUE
    /\ UNCHANGED <<stored, configured, dimension, vectors, pending, pendingDimension,
                   candidate, candidateDimension, candidateVectors, querySpace, queryExpected>>
BeginEmbedding == \E size \in Dimensions:
    /\ pending = "none" /\ (Writable \/ (UnsafeReload /\ Known))
    /\ pending' = configured /\ pendingDimension' = size
    /\ UNCHANGED <<stored, configured, dimension, vectors, candidate, candidateDimension,
                   candidateVectors, querySpace, queryExpected, blocked>>
FinishEmbedding ==
    LET matches == pending = stored /\ pending = configured
        accepted == AcceptsVector(matches, dimension, pendingDimension)
                    \/ ((UnsafeReload \/ UnsafeStaleReply) /\ AcceptsVector(TRUE, dimension, pendingDimension))
                    \/ (UnsafeDimension /\ matches)
    IN /\ pending \in Spaces
       /\ vectors' = IF accepted THEN vectors \cup {<<pending, pendingDimension>>} ELSE vectors
       /\ dimension' = IF accepted /\ dimension = 0 THEN pendingDimension ELSE dimension
       /\ pending' = "none"
       /\ UNCHANGED <<stored, configured, pendingDimension, candidate, candidateDimension,
                      candidateVectors, querySpace, queryExpected, blocked>>
Prepare == /\ candidate = "none" /\ candidate' = configured
           /\ candidateDimension' = IF UnsafeReuse THEN dimension ELSE 0
           /\ candidateVectors' = IF UnsafeReuse THEN vectors ELSE {}
           /\ UNCHANGED <<stored, configured, dimension, vectors, pending, pendingDimension,
                          querySpace, queryExpected, blocked>>
IndexPrivate == \E size \in Dimensions:
    /\ candidate \in Spaces /\ AcceptsVector(TRUE, candidateDimension, size)
    /\ candidateVectors' = candidateVectors \cup {<<candidate, size>>} /\ candidateDimension' = size
    /\ UNCHANGED <<stored, configured, dimension, vectors, pending, pendingDimension,
                   candidate, querySpace, queryExpected, blocked>>
Publish == /\ candidate = configured /\ candidate # stored
           /\ stored' = candidate /\ dimension' = candidateDimension /\ vectors' = candidateVectors
           /\ candidate' = "none" /\ candidateDimension' = 0 /\ candidateVectors' = {}
           /\ UNCHANGED <<configured, pending, pendingDimension, querySpace, queryExpected, blocked>>
Query == /\ dimension \in Dimensions /\ (Writable \/ (UnsafeReload /\ Known))
         /\ querySpace' = configured /\ queryExpected' = stored
         /\ UNCHANGED <<stored, configured, dimension, vectors, pending, pendingDimension,
                        candidate, candidateDimension, candidateVectors, blocked>>
Next == Reload \/ ObserveBlocked \/ BeginEmbedding \/ FinishEmbedding \/ Prepare \/ IndexPrivate \/ Publish \/ Query \/ UNCHANGED vars
TypeOK == /\ stored \in Spaces \cup {"unknown"} /\ configured \in Spaces
          /\ dimension \in Dimensions \cup {0} /\ pendingDimension \in Dimensions
          /\ pending \in Spaces \cup {"none"} /\ candidate \in Spaces \cup {"none"}
          /\ candidateDimension \in Dimensions \cup {0}
          /\ vectors \subseteq Spaces \X Dimensions /\ candidateVectors \subseteq Spaces \X Dimensions
          /\ querySpace \in Spaces \cup {"none"} /\ queryExpected \in Spaces \cup {"none"}
          /\ blocked \in BOOLEAN
NoMixedModels == Known => \A vector \in vectors: vector[1] = stored
OneDimension == \A vector \in vectors: vector[2] = dimension
NoCrossModelQuery == querySpace = queryExpected
MigrationWitness == ~RequireWitness \/ ~(blocked /\ stored = "b" /\ configured = "b" /\ vectors # {} /\ querySpace = "b")
Spec == Init /\ [][Next]_vars
=============================================================================
