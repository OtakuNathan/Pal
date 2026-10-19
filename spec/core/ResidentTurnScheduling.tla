--------------------- MODULE ResidentTurnScheduling ---------------------
EXTENDS Naturals, Sequences, FiniteSets, TLC

(* Core's resident scheduler, including asynchronous task teardown.
   Awaiting I/O does not release ownership. Proactive triggers are queued,
   not started by their producer. Foreign history publication and maintenance
   require a closed resident boundary. Current-turn L1 appends are allowed.
   Unsafe switches reproduce the previous independent proactive path and
   premature program-exit release; these are expected-counterexample cases.
*)
CONSTANTS Users, Jobs, UnsafeStart, UnsafeRelease, UnsafePublish, UnsafeCompletion, UnsafeBarrierRelease
Requests == Users \cup Jobs
None == "none"
VARIABLES phase, owner, live, users, jobs, received, started, completed,
          pendingTool, barriers, historyRevision, viewRevision, otherHeld, quiesceRequests
vars == <<phase, owner, live, users, jobs, received, started, completed,
          pendingTool, barriers, historyRevision, viewRevision, otherHeld, quiesceRequests>>

Init ==
    /\ phase = "idle" /\ owner = None /\ live = {}
    /\ users = <<>> /\ jobs = <<>> /\ received = {}
    /\ started = {} /\ completed = {} /\ pendingTool = FALSE
    /\ barriers = {} /\ historyRevision = 0 /\ viewRevision = 0 /\ otherHeld = FALSE /\ quiesceRequests = {}

Receive(r) ==
    /\ r \in Requests \ received
    /\ received' = received \cup {r}
    /\ users' = IF r \in Users THEN Append(users, r) ELSE users
    /\ jobs' = IF r \in Jobs THEN Append(jobs, r) ELSE jobs
    /\ UNCHANGED <<phase, owner, live, started, completed, pendingTool,
                    barriers, historyRevision, viewRevision, otherHeld, quiesceRequests>>

Dispatch ==
    /\ phase = "idle" /\ barriers = {}
    /\ Len(users) + Len(jobs) > 0
    /\ LET r == IF Len(users) > 0 THEN Head(users) ELSE Head(jobs) IN
       /\ owner' = r /\ live' = live \cup {r}
       /\ started' = started \cup {r}
       /\ users' = IF Len(users) > 0 THEN Tail(users) ELSE users
       /\ jobs' = IF Len(users) = 0 THEN Tail(jobs) ELSE jobs
    /\ phase' = "running" /\ viewRevision' = historyRevision
    /\ UNCHANGED <<received, completed, pendingTool, barriers, historyRevision, otherHeld, quiesceRequests>>

ToolStart ==
    /\ phase = "running" /\ ~pendingTool /\ barriers = {}
    /\ pendingTool' = TRUE
    /\ UNCHANGED <<phase, owner, live, users, jobs, received, started,
                    completed, barriers, historyRevision, viewRevision, otherHeld, quiesceRequests>>

ToolResult ==
    /\ phase = "running" /\ pendingTool
    /\ pendingTool' = FALSE
    /\ UNCHANGED <<phase, owner, live, users, jobs, received, started,
                    completed, barriers, historyRevision, viewRevision, otherHeld, quiesceRequests>>

(* Queued user input may join chat at a safe point; it never joins a job. *)
Interject ==
    /\ phase = "running" /\ owner \in Users /\ ~pendingTool
    /\ barriers = {} /\ Len(users) > 0
    /\ started' = started \cup {Head(users)}
    /\ completed' = completed \cup {Head(users)}
    /\ users' = Tail(users)
    /\ UNCHANGED <<phase, owner, live, jobs, received, pendingTool,
                    barriers, historyRevision, viewRevision, otherHeld, quiesceRequests>>

ProgramFinished ==
    /\ phase = "running" /\ ~pendingTool /\ "compact" \notin barriers
    /\ phase' = "finalizing"
    /\ UNCHANGED <<owner, live, users, jobs, received, started, completed,
                    pendingTool, barriers, historyRevision, viewRevision, otherHeld, quiesceRequests>>

Cancel ==
    /\ phase = "running"
    /\ phase' = "finalizing" /\ pendingTool' = FALSE
    /\ UNCHANGED <<owner, live, users, jobs, received, started, completed,
                    barriers, historyRevision, viewRevision, otherHeld, quiesceRequests>>

TaskFinished ==
    /\ phase = "finalizing" /\ "compact" \notin barriers
    /\ completed' = completed \cup {owner}
    /\ live' = live \ {owner} /\ owner' = None /\ phase' = "idle"
    /\ historyRevision' = 1 - historyRevision
    /\ UNCHANGED <<users, jobs, received, started, pendingTool,
                    barriers, viewRevision, otherHeld, quiesceRequests>>

BeginMaintenance ==
    /\ phase = "idle" /\ barriers = {} /\ Len(users) = 0
    /\ barriers' = {"maintenance"}
    /\ UNCHANGED <<phase, owner, live, users, jobs, received, started,
                    completed, pendingTool, historyRevision, viewRevision, otherHeld, quiesceRequests>>

EndMaintenance ==
    /\ "maintenance" \in barriers
    /\ barriers' = barriers \ {"maintenance"}
    /\ historyRevision' = 1 - historyRevision
    /\ UNCHANGED <<phase, owner, live, users, jobs, received, started,
                    completed, pendingTool, viewRevision, otherHeld, quiesceRequests>>

Quiesce ==
    /\ "quiesce" \notin barriers /\ "reset" \notin quiesceRequests
    /\ quiesceRequests' = quiesceRequests \cup {"reset"}
    /\ barriers' = barriers \cup {"quiesce"}
    /\ UNCHANGED <<phase, owner, live, users, jobs, received, started,
                    completed, pendingTool, historyRevision, viewRevision, otherHeld>>

OtherQuiesce ==
    /\ ~otherHeld /\ "other" \notin quiesceRequests
    /\ quiesceRequests' = quiesceRequests \cup {"other"}
    /\ otherHeld' = TRUE
    /\ barriers' = barriers \cup {"other"}
    /\ UNCHANGED <<phase, owner, live, users, jobs, received, started,
                    completed, pendingTool, historyRevision, viewRevision>>

OtherResume ==
    /\ otherHeld /\ phase = "idle"
    /\ otherHeld' = FALSE
    /\ barriers' = barriers \ {"other"}
    /\ UNCHANGED <<phase, owner, live, users, jobs, received, started,
                    completed, pendingTool, historyRevision, viewRevision, quiesceRequests>>

Reset ==
    /\ "quiesce" \in barriers /\ phase = "idle"
    /\ barriers' = IF UnsafeBarrierRelease THEN {} ELSE barriers \ {"quiesce"}
    /\ historyRevision' = 1 - historyRevision
    /\ UNCHANGED <<phase, owner, live, users, jobs, received, started,
                    completed, pendingTool, viewRevision, otherHeld, quiesceRequests>>

(* Budget compaction is an explicit safe point owned by the current turn;
   it is not a new turn and does not admit input while the ticket is held. *)
BeginCompact ==
    /\ barriers = {} /\ ~pendingTool /\ phase # "finalizing"
    /\ phase = "idle" \/ owner \in Users
    /\ barriers' = {"compact"}
    /\ UNCHANGED <<phase, owner, live, users, jobs, received, started,
                    completed, pendingTool, historyRevision, viewRevision, otherHeld, quiesceRequests>>

EndCompact ==
    /\ "compact" \in barriers
    /\ barriers' = barriers \ {"compact"}
    /\ UNCHANGED <<phase, owner, live, users, jobs, received, started,
                    completed, pendingTool, historyRevision, viewRevision, otherHeld, quiesceRequests>>

OldProactiveStart ==
    /\ UnsafeStart /\ phase # "idle" /\ Len(jobs) > 0
    /\ live' = live \cup {Head(jobs)} /\ jobs' = Tail(jobs)
    /\ UNCHANGED <<phase, owner, users, received, started, completed,
                    pendingTool, barriers, historyRevision, viewRevision, otherHeld, quiesceRequests>>

OldProgramRelease ==
    /\ UnsafeRelease /\ phase = "finalizing"
    /\ owner' = None /\ phase' = "idle"
    /\ UNCHANGED <<live, users, jobs, received, started, completed,
                    pendingTool, barriers, historyRevision, viewRevision, otherHeld, quiesceRequests>>

ForeignHistoryPublication ==
    /\ UnsafePublish /\ phase = "running" /\ historyRevision = viewRevision
    /\ historyRevision' = 1 - historyRevision
    /\ UNCHANGED <<phase, owner, live, users, jobs, received, started,
                    completed, pendingTool, barriers, viewRevision, otherHeld, quiesceRequests>>

(* A completion for an already closed owner must not release its successor. *)
LateCompletion ==
    /\ completed # {} /\ phase # "idle"
    /\ IF UnsafeCompletion THEN
          /\ owner' = None /\ phase' = "idle"
          /\ UNCHANGED <<live, users, jobs, received, started, completed,
                          pendingTool, barriers, historyRevision, viewRevision, otherHeld, quiesceRequests>>
       ELSE UNCHANGED vars

Next == (\E r \in Requests: Receive(r)) \/ Dispatch \/ ToolStart \/ ToolResult \/ Interject
        \/ ProgramFinished \/ Cancel \/ TaskFinished
        \/ BeginMaintenance \/ EndMaintenance \/ Quiesce \/ Reset \/ OtherQuiesce \/ OtherResume
        \/ BeginCompact \/ EndCompact \/ OldProactiveStart
        \/ OldProgramRelease \/ ForeignHistoryPublication \/ LateCompletion

TypeOK ==
    /\ phase \in {"idle", "running", "finalizing"}
    /\ owner \in Requests \cup {None} /\ live \subseteq Requests
    /\ users \in Seq(Users) /\ jobs \in Seq(Jobs)
    /\ received \subseteq Requests /\ started \subseteq Requests
    /\ completed \subseteq Requests /\ pendingTool \in BOOLEAN
    /\ barriers \subseteq {"maintenance", "quiesce", "compact", "other"}
    /\ otherHeld \in BOOLEAN
    /\ quiesceRequests \subseteq {"reset", "other"}
    /\ historyRevision \in 0..1 /\ viewRevision \in 0..1
SingleResidentTurn == Cardinality(live) <= 1
OwnershipUntilTeardown == (phase = "idle") => live = {}
OwnerAgrees == (phase # "idle") => live = {owner}
MaintenanceExclusive == "maintenance" \in barriers => phase = "idle"
StableHistoryView == phase = "running" => historyRevision = viewRevision
ToolHasOwner == pendingTool => phase = "running" /\ owner # None
Queued(q) == {q[i]: i \in 1..Len(q)}
QueueConservation == received = started \cup Queued(users) \cup Queued(jobs)
QueueDisjoint == /\ started \intersect (Queued(users) \cup Queued(jobs)) = {}
                 /\ Len(users) = Cardinality(Queued(users))
                 /\ Len(jobs) = Cardinality(Queued(jobs))
CompletedWasAdmitted == completed \subseteq started
BarrierOwnership == otherHeld => "other" \in barriers
AllCompleted == received = Requests ~> completed = Requests

(* Fairness assumes finite inputs (including at most two quiesce requests),
   eventual I/O/cancellation teardown and
   maintenance completion. SF dispatch prevents repeated control work from
   starving a queued request at otherwise available idle boundaries. *)
Spec == Init /\ [][Next]_vars /\ SF_vars(Dispatch)
        /\ WF_vars(Cancel) /\ WF_vars(TaskFinished)
        /\ WF_vars(EndMaintenance) /\ WF_vars(Reset) /\ WF_vars(EndCompact) /\ WF_vars(OtherResume)

(* Two revision colors suffice to detect the first forbidden publication. *)
=============================================================================
