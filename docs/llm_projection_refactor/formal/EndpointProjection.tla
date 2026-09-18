----------------------- MODULE EndpointProjection -----------------------
EXTENDS Naturals, Sequences, FiniteSets, TLC

\* Abstract SAFETY model, not a proof about provider internals.
\* One independently owned slot per logical conversation; not one per process.
\* Atomic history/native commit abstracts a shared checkpoint transaction.
\* Restart is modeled only at a closed safe point. Open-tool crash recovery,
\* cryptography, byte serialization, and actual API acceptance are external
\* refinement/test obligations documented in MODEL_NOTES.md.
CONSTANTS Scopes, Endpoints, Calls, InitialEndpoint,
          MaxGeneration, MaxFence, MaxAttempts, MaxBlocks, BuggyRepair

NoToken == "no-token"
\* Fixed from the shipped package: TLC rejects cross-type equality, so a
\* string sentinel cannot be compared against the native record. Use the
\* standard absent-field discriminator instead of `n = NoNative`.
NoNative == [absent |-> TRUE]
Phases == {"Idle", "Streaming", "Tools", "Committable", "Retired"}
EmptyWork == [calls |-> {}, started |-> {}, results |-> {},
              nativeCalls |-> {}, token |-> NoToken]

VARIABLES slots, mail
vars == <<slots, mail>>

InitSlot == [generation |-> 0, fence |-> 0, endpoint |-> InitialEndpoint,
             attempt |-> 0, phase |-> "Idle", history |-> <<>>,
             native |-> <<>>, cache |-> <<>>, sent |-> <<>>,
             work |-> EmptyWork]

Init == /\ slots = [s \in Scopes |-> InitSlot]
        /\ mail = {}

Key(s) == [scope |-> s, generation |-> slots[s].generation,
           fence |-> slots[s].fence, endpoint |-> slots[s].endpoint,
           attempt |-> slots[s].attempt]

Project(s) == [i \in 1..Len(slots[s].history) |->
               [scope |-> s, generation |-> slots[s].generation,
                endpoint |-> slots[s].endpoint,
                semantic |-> slots[s].history[i],
                continuation |-> slots[s].native[i]]]

IsPrefix(a, b) == /\ Len(a) <= Len(b)
                  /\ \A i \in 1..Len(a): a[i] = b[i]

Sync(s) == /\ slots[s].phase = "Idle"
           /\ slots[s].cache # Project(s)
           /\ IsPrefix(slots[s].cache, Project(s))
           /\ slots' = [slots EXCEPT ![s].cache = Project(s)]
           /\ UNCHANGED mail

Begin(s, cs) ==
    /\ slots[s].phase = "Idle"
    /\ slots[s].cache = Project(s)
    /\ slots[s].attempt < MaxAttempts
    /\ Len(slots[s].history) < MaxBlocks
    /\ cs \subseteq Calls
    /\ LET k == [scope |-> s, generation |-> slots[s].generation,
                  fence |-> slots[s].fence, endpoint |-> slots[s].endpoint,
                  attempt |-> slots[s].attempt + 1]
       IN /\ slots' = [slots EXCEPT
             ![s].attempt = @ + 1,
             ![s].phase = "Streaming",
             ![s].sent = slots[s].cache,
             ![s].work = [calls |-> cs, started |-> {}, results |-> {},
                          nativeCalls |-> cs, token |-> k]]
          /\ mail' = mail \cup {k}

Deliver(k) ==
    /\ k \in mail
    /\ mail' = mail \ {k}
    /\ LET s == k.scope
       IN IF slots[s].phase = "Streaming" /\ k = Key(s)
          THEN slots' = [slots EXCEPT ![s].phase =
                  IF slots[s].work.calls = {} THEN "Committable" ELSE "Tools"]
          ELSE UNCHANGED slots

StartTool(s, c) ==
    /\ slots[s].phase = "Tools"
    /\ c \in slots[s].work.calls \ slots[s].work.started
    /\ slots' = [slots EXCEPT ![s].work.started = @ \cup {c}]
    /\ UNCHANGED mail

FinishTool(s, c) ==
    /\ slots[s].phase = "Tools"
    /\ c \in slots[s].work.started \ slots[s].work.results
    /\ LET rs == slots[s].work.results \cup {c}
       IN slots' = [slots EXCEPT ![s].work.results = rs,
                     ![s].phase = IF rs = slots[s].work.calls
                                  THEN "Committable" ELSE "Tools"]
    /\ UNCHANGED mail

\* Only NEVER-STARTED calls may be pruned here. Unknown effects are not erased.
\* A provider policy must validate the repaired continuation before this action
\* is implemented. Updating nativeCalls is an abstract compatibility proof,
\* NOT permission to edit signed/opaque provider data.
Repair(s) ==
    /\ slots[s].phase = "Tools"
    /\ slots[s].work.calls # slots[s].work.started
    /\ LET cs == slots[s].work.started
       IN slots' = [slots EXCEPT
            ![s].work.calls = cs,
            ![s].work.nativeCalls = IF BuggyRepair
                                    THEN slots[s].work.nativeCalls ELSE cs,
            ![s].phase = IF slots[s].work.results = cs
                         THEN "Committable" ELSE "Tools"]
    /\ UNCHANGED mail

CancelStream(s) ==
    /\ slots[s].phase = "Streaming"
    /\ slots' = [slots EXCEPT ![s].phase = "Idle", ![s].work = EmptyWork]
    /\ UNCHANGED mail

Commit(s) ==
    /\ slots[s].phase = "Committable"
    /\ slots[s].work.calls = slots[s].work.results
    /\ slots[s].work.calls = slots[s].work.nativeCalls
    /\ slots[s].work.token = Key(s)
    /\ LET b == [id |-> slots[s].work.token,
                  calls |-> slots[s].work.calls, results |-> slots[s].work.results]
           n == [key |-> Key(s), calls |-> slots[s].work.nativeCalls,
                 absent |-> FALSE]
       IN slots' = [slots EXCEPT
            ![s].history = Append(@, b), ![s].native = Append(@, n),
            ![s].work = EmptyWork, ![s].phase = "Idle"]
    /\ UNCHANGED mail

\* A switch is explicit semantic import into a supported target profile.
\* Old native state is discarded; history is NOT discarded.
Switch(s, e) ==
    /\ slots[s].phase = "Idle"
    /\ e \in Endpoints \ {slots[s].endpoint}
    /\ slots[s].generation < MaxGeneration
    /\ slots' = [slots EXCEPT
         ![s].generation = @ + 1, ![s].endpoint = e,
         ![s].native = [i \in 1..Len(slots[s].history) |-> NoNative],
         ![s].cache = <<>>, ![s].sent = <<>>]
    /\ UNCHANGED mail

\* Empty history abstracts a fresh compacted base. Summary correctness is not
\* modeled; install requires a validated summary and an unchanged source head.
Compact(s) ==
    /\ slots[s].phase = "Idle"
    /\ slots[s].generation < MaxGeneration
    /\ slots' = [slots EXCEPT ![s].generation = @ + 1,
         ![s].history = <<>>, ![s].native = <<>>,
         ![s].cache = <<>>, ![s].sent = <<>>]
    /\ UNCHANGED mail

\* Derived projection can be evicted; REQUIRED native state is durable.
EvictProjection(s) ==
    /\ slots[s].phase = "Idle"
    /\ slots[s].cache # <<>>
    /\ slots' = [slots EXCEPT ![s].cache = <<>>]
    /\ UNCHANGED mail

Restart(s) ==
    /\ slots[s].phase = "Idle"
    /\ slots[s].fence < MaxFence
    /\ slots' = [slots EXCEPT ![s].fence = @ + 1, ![s].cache = <<>>]
    /\ UNCHANGED mail

Close(s) ==
    /\ slots[s].phase = "Idle"
    /\ slots' = [slots EXCEPT ![s].phase = "Retired",
         ![s].native = [i \in 1..Len(slots[s].history) |-> NoNative],
         ![s].cache = <<>>, ![s].sent = <<>>]
    /\ UNCHANGED mail

Next == \/ \E s \in Scopes: Sync(s) \/ CancelStream(s) \/ Repair(s) \/ Commit(s)
                             \/ Compact(s) \/ EvictProjection(s) \/ Restart(s) \/ Close(s)
        \/ \E s \in Scopes, cs \in SUBSET Calls: Begin(s, cs)
        \/ \E s \in Scopes, c \in Calls: StartTool(s, c) \/ FinishTool(s, c)
        \/ \E s \in Scopes, e \in Endpoints: Switch(s, e)
        \/ \E k \in mail: Deliver(k)

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ DOMAIN slots = Scopes
    /\ \A s \in Scopes:
        /\ slots[s].phase \in Phases
        /\ slots[s].endpoint \in Endpoints
        /\ slots[s].generation \in 0..MaxGeneration
        /\ slots[s].fence \in 0..MaxFence
        /\ slots[s].attempt \in 0..MaxAttempts
        /\ Len(slots[s].history) <= MaxBlocks
        /\ Len(slots[s].native) = Len(slots[s].history)
        /\ slots[s].work.results \subseteq slots[s].work.started
        /\ slots[s].work.started \subseteq slots[s].work.calls
        /\ slots[s].work.calls \subseteq Calls

\* Fixed from the shipped package: TLA+ forbids a later quantifier bound
\* referencing an earlier bound's variable (\A s \in S, i \in 1..f(s) is a
\* semantic error); these two invariants must nest the quantifiers.
ClosedHistory == \A s \in Scopes: \A i \in 1..Len(slots[s].history):
    slots[s].history[i].calls = slots[s].history[i].results

NativeAligned == \A s \in Scopes: \A i \in 1..Len(slots[s].history):
    LET n == slots[s].native[i]
    IN IF n.absent THEN TRUE ELSE
        /\ n.key.scope = s
        /\ n.key.generation = slots[s].generation
        /\ n.key.endpoint = slots[s].endpoint
        /\ n.calls = slots[s].history[i].calls

PrefixSound == \A s \in Scopes:
    /\ IsPrefix(slots[s].cache, Project(s))
    /\ IsPrefix(slots[s].sent, Project(s))

DraftAligned == \A s \in Scopes:
    slots[s].phase \in {"Streaming", "Tools", "Committable"} =>
      /\ slots[s].work.token = Key(s)
      /\ slots[s].work.nativeCalls = slots[s].work.calls

NoDuplicateCommit == \A s \in Scopes:
    Cardinality({slots[s].history[i].id : i \in 1..Len(slots[s].history)})
       = Len(slots[s].history)

\* No fairness assumption: providers and tools may never respond.
\* Prompt-cache TTL is intentionally absent: it cannot authorize a transition.
=============================================================================
