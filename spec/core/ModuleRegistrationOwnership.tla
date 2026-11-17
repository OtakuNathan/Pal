---------------- MODULE ModuleRegistrationOwnership ----------------
EXTENDS Naturals, FiniteSets, TLC, ModuleRegistrationReducer

(* All phase and name-ownership decisions import the production reducer.
   a0/a1 are different instances claiming the same module name; b claims a
   different module name but competes for the same event-source name.
   Publication/withdrawal orchestration corresponds to PluginHost; see
   docs/module_registration.md for its runtime conformance checks and limits. *)
CONSTANTS ForeignRollback, ReplaceSource, RequireRetryWitness
Actors == {"a0", "a1", "b"}
Modules == {"a", "b"}
ModuleOf(p) == IF p = "b" THEN "b" ELSE "a"
VARIABLES phase, names, surface, source, failedCleanup, retried,
          activated, replacementRetried
vars == <<phase, names, surface, source, failedCleanup, retried,
          activated, replacementRetried>>
Owns(p) == names[ModuleOf(p)] = p

Init == /\ phase = [p \in Actors |-> "preparing"]
        /\ names = [m \in Modules |-> "none"]
        /\ surface = [m \in Modules |-> "none"]
        /\ source = "none" /\ failedCleanup = {} /\ retried = {}
        /\ activated = {} /\ replacementRetried = FALSE

ClaimName(p) ==
    /\ phase[p] = "preparing"
    /\ OwnershipStep(OwnerKind(names[ModuleOf(p)], p), "claim", "self")
    /\ names' = [names EXCEPT ![ModuleOf(p)] = p]
    /\ UNCHANGED <<phase, surface, source, failedCleanup, retried, activated, replacementRetried>>

Advance(p, event, target) ==
    /\ RegistrationStep(phase[p], Owns(p), event, target)
    /\ phase' = [phase EXCEPT ![p] = target]
    /\ UNCHANGED <<names, surface, source, failedCleanup, retried, activated, replacementRetried>>

PublishSurface(p) ==
    /\ phase[p] = "publishing" /\ Owns(p)
    /\ surface[ModuleOf(p)] = "none"
    /\ surface' = [surface EXCEPT ![ModuleOf(p)] = p]
    /\ UNCHANGED <<phase, names, source, failedCleanup, retried, activated, replacementRetried>>

PublishSource(p) ==
    /\ phase[p] = "publishing" /\ Owns(p)
    /\ (OwnershipStep(OwnerKind(source, p), "claim", "self")
        \/ (ReplaceSource /\ source # p))
    /\ source' = p
    /\ UNCHANGED <<phase, names, surface, failedCleanup, retried, activated, replacementRetried>>

Commit(p) ==
    /\ surface[ModuleOf(p)] = p /\ source = p
    /\ RegistrationStep(phase[p], Owns(p), "commit", "active")
    /\ phase' = [phase EXCEPT ![p] = "active"]
    /\ activated' = activated \cup {p}
    /\ UNCHANGED <<names, surface, source, failedCleanup, retried, replacementRetried>>

Abandon(p) ==
    /\ RegistrationStep(phase[p], Owns(p), "abandon", "cleaning")
    /\ phase' = [phase EXCEPT ![p] = "cleaning"]
    /\ surface' = IF ForeignRollback THEN [surface EXCEPT ![ModuleOf(p)] = "none"] ELSE surface
    /\ UNCHANGED <<names, source, failedCleanup, retried, activated, replacementRetried>>

Withdraw(p) ==
    /\ phase[p] = "withdrawing" /\ Owns(p)
    /\ OwnershipStep(OwnerKind(names[ModuleOf(p)], p), "release", "vacant")
    /\ RegistrationStep(phase[p], FALSE, "withdraw", "cleaning")
    /\ names' = [names EXCEPT ![ModuleOf(p)] = "none"]
    /\ surface' = IF surface[ModuleOf(p)] = p THEN [surface EXCEPT ![ModuleOf(p)] = "none"] ELSE surface
    /\ source' = IF source = p /\ OwnershipStep(OwnerKind(source, p), "release", "vacant") THEN "none" ELSE source
    /\ phase' = [phase EXCEPT ![p] = "cleaning"]
    /\ UNCHANGED <<failedCleanup, retried, activated, replacementRetried>>

FailCleanup(p) ==
    /\ phase[p] = "cleaning" /\ p \notin failedCleanup
    /\ failedCleanup' = failedCleanup \cup {p}
    /\ UNCHANGED <<phase, names, surface, source, retried, activated, replacementRetried>>

RetryCleanup(p) ==
    /\ p \in failedCleanup /\ p \notin retried
    /\ RegistrationStep(phase[p], Owns(p), "retry", "cleaning")
    /\ retried' = retried \cup {p}
    /\ replacementRetried' = (replacementRetried \/
        (p = "a0" /\ p \in activated /\ phase["a1"] = "active"))
    /\ UNCHANGED <<phase, names, surface, source, failedCleanup, activated>>

Close(p) ==
    /\ (p \notin failedCleanup \/ p \in retried)
    /\ Advance(p, "close", "closed")

ActorStep == \E p \in Actors:
    \/ ClaimName(p)
    \/ Advance(p, "claim", "registered")
    \/ Advance(p, "publish", "publishing")
    \/ PublishSurface(p) \/ PublishSource(p) \/ Commit(p)
    \/ Advance(p, "retire", "withdrawing")
    \/ Abandon(p) \/ Withdraw(p) \/ FailCleanup(p) \/ RetryCleanup(p) \/ Close(p)

Next == ActorStep \/ ((\A p \in Actors: phase[p] = "closed") /\ UNCHANGED vars)

TypeOK == /\ phase \in [Actors -> Phases]
          /\ names \in [Modules -> Actors \cup {"none"}]
          /\ surface \in [Modules -> Actors \cup {"none"}]
          /\ source \in Actors \cup {"none"}
          /\ failedCleanup \subseteq Actors /\ retried \subseteq failedCleanup
          /\ activated \subseteq Actors /\ replacementRetried \in BOOLEAN
PublishedOwnership == \A p \in Actors: phase[p] = "active" =>
    /\ Owns(p) /\ surface[ModuleOf(p)] = p /\ source = p
CleanupIsolation == \A p \in Actors: phase[p] \in {"cleaning", "closed"} => ~Owns(p)
SurfaceOwnership == \A m \in Modules: surface[m] # "none" => names[m] = surface[m]
RetryReplacementWitness == ~RequireRetryWitness \/ ~replacementRetried
Spec == Init /\ [][Next]_vars
=============================================================================
