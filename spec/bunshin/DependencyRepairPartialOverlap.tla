---------------- MODULE DependencyRepairPartialOverlap ----------------
(*
Two actual partial scopes can share X while their reporters lie outside each
other's scope. With IncludeBridge, J can join two pending components through X/Y.
Registering after a prior Apply is an independent later cohort, not a requirement
to merge every possible future reporter. An already applied packet is never reapplied.

  Model                     Runtime obligation
  Register                  persist intent/fences with graph write serialization
  Prepare                   provisional calculation only; no authority across awaits
  Apply                     re-read pending component/key under the registration transaction
  packetApplies             immutable packet/provider application receipts
  frozen                    no shared affected-node admission before required repair barriers

The runtime may reject a stale cohort key and retry from persisted pending state.
GraphIR.repair_descendants includes semantic AND effective checker edges, including
the synthetic software sink. That closure computation is a separate integration gate.
*)
EXTENDS Naturals, FiniteSets
CONSTANT IncludeBridge
Packets == IF IncludeBridge THEN {"A", "B", "J"} ELSE {"A", "B"}
Nodes == {"P", "Q", "R", "C", "D", "E", "X", "Y"}
Scope == [k \in Packets |->
    IF k = "A" THEN {"P", "C", "X"}
    ELSE IF k = "B" THEN IF IncludeBridge THEN {"Q", "D", "Y"} ELSE {"Q", "D", "X"}
    ELSE {"P", "R", "E", "X", "Y"}]
Targets == [k \in Packets |->
    IF k = "A" THEN {"P"} ELSE IF k = "B" THEN {"Q"} ELSE {"P", "R"}]
VARIABLES registered, applied, frozen, revision, cached, cachedRevision, reopens, badJoin, packetApplies
vars == <<registered,applied,frozen,revision,cached,cachedRevision,reopens,badJoin,packetApplies>>
ScopeOf(ks) == UNION {Scope[k] : k \in ks}
\* Root-selected semantics: merge current unsealed registered components.
\* Independent outside reports may form later cohorts. No global same-wave claim.
Closed(ks, universe) ==
    ks \subseteq universe /\ \A k \in universe :
        Scope[k] \cap ScopeOf(ks) # {} => k \in ks
Component(k, universe) == CHOOSE ks \in SUBSET universe :
    /\ k \in ks /\ Closed(ks, universe)
    /\ \A other \in SUBSET universe : k \in other /\ Closed(other, universe) => ks \subseteq other
Init == /\ registered = {} /\ applied = {} /\ frozen = {}
        /\ revision = 0 /\ cached = {} /\ cachedRevision = 0 /\ badJoin = FALSE
        /\ reopens = [n \in Nodes |-> 0]
        /\ packetApplies = [k \in Packets |-> 0]
Register(k) == /\ k \in Packets \ registered
    /\ registered' = registered \cup {k} /\ revision' = revision + 1
    /\ frozen' = frozen \cup Scope[k]
    /\ UNCHANGED <<applied,cached,cachedRevision,reopens,badJoin,packetApplies>>
Prepare(k) == /\ k \in registered \ applied
    /\ cached' = Component(k, registered \ applied) /\ cachedRevision' = revision
    /\ UNCHANGED <<registered,applied,frozen,revision,reopens,badJoin,packetApplies>>
Apply == /\ cached # {} /\ cachedRevision = revision /\ cached \cap applied = {}
    /\ (\A k \in cached : Component(k, registered \ applied) = cached)
    /\ applied' = applied \cup cached
    /\ packetApplies' = [k \in Packets |-> IF k \in cached THEN packetApplies[k] + 1 ELSE packetApplies[k]]
    /\ badJoin' = (badJoin \/ cachedRevision # revision \/
                   (\E k \in cached : Component(k, registered \ applied) # cached))
    /\ reopens' = [n \in Nodes |-> IF n \in UNION {Targets[k] : k \in cached}
                      THEN reopens[n] + 1 ELSE reopens[n]]
    /\ UNCHANGED <<registered,frozen,revision,cached,cachedRevision>>
Next == (\E k \in Packets : Register(k) \/ Prepare(k)) \/ Apply
Spec == Init /\ [][Next]_vars
TypeOK ==
    /\ registered \subseteq Packets /\ applied \subseteq registered
    /\ frozen \subseteq Nodes /\ revision \in 0..Cardinality(Packets)
    /\ cached \subseteq registered /\ cachedRevision \in 0..Cardinality(Packets)
    /\ reopens \in [Nodes -> 0..Cardinality(Packets)] /\ badJoin \in BOOLEAN
    /\ packetApplies \in [Packets -> 0..1]
CurrentPendingComponentJoinedAtomically == ~badJoin
OnePacketProviderApplication == \A k \in Packets : packetApplies[k] <= 1
\* Reopening the same provider in a later cohort is legitimate. Immutable
\* packet/provider receipts, not a global provider counter, deduplicate replay.
AppliedScopeFenced == ScopeOf(applied) \subseteq frozen
\* The production transaction must also require all component old owners closed
\* and quiescent, as modeled separately. It must re-read/recompute component and
\* generation revision under the same write boundary as receipt registration.
=================================================================
