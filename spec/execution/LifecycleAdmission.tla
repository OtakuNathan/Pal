-------------------------- MODULE LifecycleAdmission --------------------------
EXTENDS Naturals, FiniteSets, TLC

CONSTANT Waiters, Mode
Modes == [w \in Waiters |-> IF w = "w1" THEN "write" ELSE "read"]
VARIABLES phase, cancellations, leases, called, blocker
vars == <<phase, cancellations, leases, called, blocker>>

Init == /\ phase = [w \in Waiters |-> "queued"]
        /\ cancellations = [w \in Waiters |-> 0]
        /\ leases = {}
        /\ called = {}
        /\ blocker = TRUE

Readers == {w \in leases : Mode[w] = "read"}
Writers == {w \in leases : Mode[w] = "write"}
WaitingWriters == {w \in Waiters : Mode[w] = "write" /\
                    phase[w] \in {"queued", "draining"} /\ w \notin leases}
CanAcquire(w) == /\ ~blocker
                 /\ IF Mode[w] = "write" THEN leases = {}
                    ELSE Writers = {} /\ WaitingWriters = {}
Acquire(w) == /\ phase[w] \in {"queued", "draining"} /\ w \notin leases
              /\ CanAcquire(w)
              /\ leases' = leases \cup {w}
              /\ phase' = [phase EXCEPT ![w] = IF @ = "queued" THEN "acquired" ELSE @]
              /\ UNCHANGED <<cancellations, called, blocker>>
Cancel(w) == /\ phase[w] \in {"queued", "acquired", "draining"}
             /\ cancellations[w] < 2
             /\ cancellations' = [cancellations EXCEPT ![w] = @ + 1]
             /\ phase' = [phase EXCEPT ![w] = "draining"]
             /\ UNCHANGED <<leases, called, blocker>>
Drain(w) == /\ phase[w] = "draining" /\ w \in leases
            /\ leases' = leases \ {w}
            /\ phase' = [phase EXCEPT ![w] = "done"]
            /\ UNCHANGED <<cancellations, called, blocker>>
Enter(w) == /\ phase[w] = "acquired"
            /\ phase' = [phase EXCEPT ![w] = "body"]
            /\ called' = called \cup {w}
            /\ UNCHANGED <<cancellations, leases, blocker>>
Finish(w) == /\ phase[w] = "body"
             /\ phase' = [phase EXCEPT ![w] = "done"]
             /\ leases' = leases \ {w}
             /\ UNCHANGED <<cancellations, called, blocker>>
ReleaseBlocker == /\ blocker /\ blocker' = FALSE
                  /\ UNCHANGED <<phase, cancellations, leases, called>>
Next == ReleaseBlocker \/ \E w \in Waiters : Acquire(w) \/ Cancel(w) \/ Drain(w) \/ Enter(w) \/ Finish(w)

TypeOK == /\ phase \in [Waiters -> {"queued", "acquired", "draining", "body", "done"}]
          /\ cancellations \in [Waiters -> 0..2]
          /\ leases \subseteq Waiters /\ called \subseteq Waiters /\ blocker \in BOOLEAN
Ownership == \A w \in Waiters : /\ (phase[w] = "done" => w \notin leases)
                                 /\ (phase[w] \in {"body", "acquired"} => w \in leases)
Cancellation == \A w \in called : cancellations[w] = 0
Exclusion == /\ Cardinality(Writers) <= 1
             /\ (Writers # {} => Readers = {})
             /\ (blocker => leases = {})
AllDone == \A w \in Waiters : phase[w] = "done"
Spec == /\ Init /\ [][Next]_vars
        /\ WF_vars(ReleaseBlocker)
        /\ \A w \in Waiters : WF_vars(Acquire(w)) /\ WF_vars(Drain(w)) /\ WF_vars(Enter(w)) /\ WF_vars(Finish(w))
Termination == <>AllDone
=============================================================================
