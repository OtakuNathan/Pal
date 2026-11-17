---------------- MODULE MemoryMaintenanceLifecycle ----------------
EXTENDS Naturals, TLC, MemorySwapReducer

CONSTANTS UnsafeSilentSleep, UnsafeWake, UnsafeRelease, UnsafePartialBinding, RequireWitness
VARIABLES phase, required, delivered, sleeping, published, repository, backend, selected, failed, recovered, released
vars == <<phase, required, delivered, sleeping, published, repository, backend, selected, failed, recovered, released>>
Facts == <<~published, TRUE, sleeping, published, FALSE>>
Bound == BindingReady(repository, backend, selected)

Init == /\ phase = "preparing" /\ required \in BOOLEAN
        /\ delivered = FALSE /\ sleeping = FALSE /\ published = FALSE
        /\ repository = FALSE /\ backend = FALSE /\ selected = FALSE
        /\ failed = FALSE /\ recovered = FALSE /\ released = TRUE

Advance(event, target) ==
    /\ Step(phase, Facts, event, target) /\ phase' = target
    /\ UNCHANGED <<required, delivered, sleeping, published, repository, backend, selected, failed, recovered, released>>
Notify == /\ ~delivered /\ delivered' = TRUE
          /\ UNCHANGED <<phase, required, sleeping, published, repository, backend, selected, failed, recovered, released>>
Sleep == /\ phase = "catching_up" /\ ~sleeping
         /\ (CanSleep(required, delivered) \/ UnsafeSilentSleep) /\ sleeping' = TRUE /\ released' = FALSE
         /\ UNCHANGED <<phase, required, delivered, published, repository, backend, selected, failed, recovered>>
Publish == /\ phase = "ready" /\ sleeping /\ CanPublish(phase, Facts)
           /\ Step(phase, <<FALSE, TRUE, TRUE, TRUE, FALSE>>, "commit", "committed")
           /\ phase' = "committed" /\ published' = TRUE
           /\ UNCHANGED <<required, delivered, sleeping, repository, backend, selected, failed, recovered, released>>
MountRepository == /\ published /\ sleeping /\ ~repository /\ repository' = TRUE
                   /\ UNCHANGED <<phase, required, delivered, sleeping, published, backend, selected, failed, recovered, released>>
MountFailure == /\ published /\ sleeping /\ ~Bound /\ ~failed /\ failed' = TRUE
                /\ UNCHANGED <<phase, required, delivered, sleeping, published, repository, backend, selected, recovered, released>>
PrepareBackend == /\ published /\ sleeping /\ repository /\ ~backend /\ backend' = TRUE
                  /\ UNCHANGED <<phase, required, delivered, sleeping, published, repository, selected, failed, recovered, released>>
SelectBackend == /\ published /\ sleeping /\ backend /\ ~selected
                 /\ selected' = TRUE /\ recovered' = failed
                 /\ UNCHANGED <<phase, required, delivered, sleeping, published, repository, backend, failed, released>>
ReleaseFence == /\ sleeping /\ ~released /\ (published \/ phase = "superseded") /\ released' = TRUE
                /\ UNCHANGED <<phase, required, delivered, sleeping, published, repository, backend, selected, failed, recovered>>
Wake == /\ sleeping /\ (CanWake(published, Bound, released) \/ UnsafeWake \/
                              (UnsafeRelease /\ CanWake(published, Bound, TRUE)) \/
                              (UnsafePartialBinding /\ CanWake(published, repository, released)))
        /\ sleeping' = FALSE
        /\ UNCHANGED <<phase, required, delivered, published, repository, backend, selected, failed, recovered, released>>
Finish == /\ phase = "indexing" /\ Bound
          /\ Step(phase, <<FALSE, TRUE, FALSE, TRUE, TRUE>>, "indexed", "complete")
          /\ phase' = "complete"
          /\ UNCHANGED <<required, delivered, sleeping, published, repository, backend, selected, failed, recovered, released>>

Next == Advance("snapshot", "copying") \/ Advance("copied", "catching_up")
        \/ Notify \/ Sleep \/ Advance("verified", "ready") \/ Publish
        \/ MountRepository \/ MountFailure \/ PrepareBackend \/ SelectBackend \/ ReleaseFence \/ Wake
        \/ Advance("index", "indexing") \/ Finish \/ Advance("abandon", "superseded") \/ UNCHANGED vars
TypeOK == /\ phase \in Phases
          /\ \A flag \in {required, delivered, sleeping, published, repository, backend, selected, failed, recovered, released}: flag \in BOOLEAN
NoticeBeforeSleep == required /\ sleeping => delivered
BoundBeforeWake == published /\ ~sleeping => (repository /\ backend /\ selected)
OwnerBeforeWake == published /\ ~sleeping => released
RecoveryWitness == ~RequireWitness \/ ~(phase = "complete" /\ ~sleeping /\ recovered /\ required)
Spec == Init /\ [][Next]_vars
=============================================================================
