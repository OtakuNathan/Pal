---------------- MODULE DependencyRepairCohort ----------------
(*
Bounded reference model for the approved actual-scope dependency-repair protocol.
This is one fixed overlapping component, not a global all-reporters freeze.
Independent outside reports may become later cohorts; see DependencyRepairLaterCohort.

State correspondence
  Model                     Runtime authority
  intents / applied         GraphExecution.dependency_repairs.pending / .history
  frozen                    pending_scope / DependencyRepairLedger.fenced_nodes
  frontier / closed         exact RepairIncarnation keys / RepairClosure.submission_cut
  captured / evidence       cohort.captures and immutable packet/report receipt refs
  live / lease              exact old process owner and original business lease identity
  obligations               historical_repair_bill_refs and mandatory finding-case replay
  incarnation               admitted effect/task/cycle/assignment identity, not invocation ID

Action correspondence
  Register                  register_dependency_repair(intent, frontier=...); admission cut
  Capture                   capture_dependency_repair(incarnation_key, receipt_refs=...)
  Reap + SealAssignment     cancel/join exact setup task and close its role assignment
  Release                   verify matching business lease/workspace cleanup
  final cleanup proof       close_dependency_repair(RepairClosure(...))
  Apply                     apply_dependency_repair(cohort_key=...); one atomic joined write
  StartRepairChecker        checker EXECUTION readiness; software producers stay parallel
  Supersede                 supersede_dependency_repairs(reason=...)

The submission cut follows reaping and shares record_role_submission's write lock.
RESULT_RECORDED may precede Pending creation. CHECKING can mean a physically
quiesced REVIEW_SNAPSHOTTING process; it is not a liveness/ownership predicate.
Original attempt/submission and business-lease fences remain distinct from a
rebound snapshot lease. Production capture must retain these exact identities.

Verification limits: tests/test_bunshin_dependency_repair_model.py exhausts a
finite Python counterpart and expected unsafe mutations. TLC is optional and
requires an existing pinned TLA2TOOLS_JAR; a Python pass is not a TLC pass.
The separate models are not a checked compositional proof. Workspace fidelity,
actual causal refs/outbox replay, schema/default handling, and sink publication
need source integration tests. Here published is only a suppression sentinel;
this model does not establish a correct positive production publication path.
*)
EXTENDS Naturals, FiniteSets

CONSTANT P, Q, C, D, X, PC, PD, DReportKind
Nodes == {P,Q,C,D,X}
Packets == {PC,PD}
Source(k) == IF k = PC THEN C ELSE D
Targets(k) == IF k = PC THEN {P}
              ELSE IF DReportKind = "dependency" THEN {P,Q} ELSE {}
DependencyPackets == {k \in Packets : Targets(k) # {}}
InvalidPackets == IF DReportKind \in {"invalid_scope","contract_only"} THEN {PD} ELSE {}
\* Both packet scopes overlap; Q is initially downstream of P.
Scope(k) == Nodes
States == {"Accepted", "Checking", "Producing", "CancelRequested",
           "Stale", "RepairReady", "Repairing", "RepairCheckerReady",
           "RepairChecking", "ProducerReady"}

VARIABLES state, live, lease, submitted, captured, intents, frontier,
          frozen, applied, evidence, reopens, incarnation, online, crashed,
          mode, published, closed, obligations
vars == <<state,live,lease,submitted,captured,intents,frontier,
          frozen,applied,evidence,reopens,incarnation,online,crashed,
          mode,published,closed,obligations>>

Providers == UNION {Targets(k) : k \in intents}
Consumers == frozen \ Providers
PendingFor(n) == {k \in submitted : Source(k) = n}
Drained == frontier \subseteq captured /\
           (frontier \cap DependencyPackets) \subseteq intents
Quiescent == frozen \subseteq closed /\
    \A n \in frozen : ~live[n] /\ ~lease[n]
ExpectedEvidence(n) == {k \in intents : n \in Targets(k)}
CheckerProviders(n) == IF n = Q THEN {P} ELSE {}

Init ==
    /\ state = [n \in Nodes |-> IF n \in {P,Q} THEN "Accepted"
                    ELSE IF n = X THEN "Producing" ELSE "Checking"]
    /\ live = [n \in Nodes |-> n \in {C,D,X}]
    /\ lease = live
    /\ closed = {P,Q}
    /\ submitted = {PC}
    /\ captured = {} /\ obligations = {}
    /\ intents = {}
    /\ frontier = {}
    /\ frozen = {}
    /\ applied = {}
    /\ evidence = [n \in Nodes |-> {}]
    /\ reopens = [n \in Nodes |-> 0]
    /\ incarnation = [n \in Nodes |-> 0]
    /\ online = TRUE /\ crashed = FALSE
    /\ mode = "Running" /\ published = FALSE

SubmitPeer ==
    /\ online /\ mode = "Running" /\ PD \notin submitted
    /\ D \notin closed /\ incarnation[D] = 0 /\ lease[D]
    /\ state[D] \in {"Checking", "CancelRequested"}
    /\ submitted' = submitted \cup {PD}
    /\ UNCHANGED <<state,live,lease,captured,intents,frontier,frozen,
         applied,evidence,reopens,incarnation,online,crashed,mode,published,closed,obligations>>

Reap(n) ==
    /\ online /\ incarnation[n] = 0 /\ live[n]
    /\ PendingFor(n) # {} \/ state[n] = "CancelRequested"
    /\ live' = [live EXCEPT ![n] = FALSE]
    /\ UNCHANGED <<state,lease,submitted,captured,intents,frontier,frozen,
         applied,evidence,reopens,incarnation,online,crashed,mode,published,closed,obligations>>

\* This is durable immutable receipt capture, not a graph acceptance.
Capture(k) ==
    /\ online /\ k \in submitted \ captured /\ ~live[Source(k)]
    /\ captured' = captured \cup {k}
    /\ obligations' = IF k \in InvalidPackets THEN obligations \cup {k} ELSE obligations
    /\ UNCHANGED <<state,live,lease,submitted,intents,frontier,frozen,
         applied,evidence,reopens,incarnation,online,crashed,mode,published,closed>>

Release(n) ==
    /\ online /\ incarnation[n] = 0 /\ lease[n] /\ ~live[n]
    /\ n \in closed /\ PendingFor(n) \subseteq captured
    /\ lease' = [lease EXCEPT ![n] = FALSE]
    /\ UNCHANGED <<state,live,submitted,captured,intents,frontier,frozen,
         applied,evidence,reopens,incarnation,online,crashed,mode,published,closed,obligations>>

\* Freeze prevents new admission; in-flight raw role results may still arrive
\* before SealAssignment takes the role-submission write lock. Submitted means
\* any durable RESULT_RECORDED receipt, including before pending-ref creation.
SealAssignment(n) ==
    /\ online /\ incarnation[n] = 0 /\ ~live[n] /\ n \notin closed
    /\ closed' = closed \cup {n}
    /\ frontier' = IF n \in frozen THEN frontier \cup PendingFor(n) ELSE frontier
    /\ UNCHANGED <<state,live,lease,submitted,captured,intents,frozen,
         applied,evidence,reopens,incarnation,online,crashed,mode,published,obligations>>

\* Atomic receipt/intent registration and admission fence/cancel request.
\* Registration does NOT require another node to have become Stale.
Register(k) ==
    /\ online /\ mode = "Running"
    /\ k \in (captured \cap DependencyPackets) \ intents
    /\ applied = {}
    /\ intents' = intents \cup {k}
    /\ frozen' = frozen \cup Scope(k)
    /\ frontier' = frontier \cup {j \in submitted : Source(j) \in Scope(k)}
    /\ state' = [n \in Nodes |->
         IF n \in Scope(k) /\ PendingFor(n) = {} /\ (live[n] \/ lease[n])
         THEN "CancelRequested" ELSE state[n]]
    /\ UNCHANGED <<live,lease,submitted,captured,applied,evidence,reopens,
         incarnation,online,crashed,mode,published,closed,obligations>>

Apply ==
    /\ online /\ mode = "Running" /\ intents # {} /\ applied = {}
    /\ Drained /\ Quiescent
    /\ applied' = intents
    /\ evidence' = [n \in Nodes |-> ExpectedEvidence(n)]
    /\ reopens' = [n \in Nodes |-> IF n \in Providers THEN reopens[n] + 1 ELSE reopens[n]]
    /\ state' = [n \in Nodes |-> IF n \in Providers THEN "RepairReady"
                         ELSE IF n \in Consumers THEN "Stale" ELSE state[n]]
    /\ UNCHANGED <<live,lease,submitted,captured,intents,frontier,frozen,
         incarnation,online,crashed,mode,published,closed,obligations>>

StartRepair(n) ==
    /\ online /\ mode = "Running" /\ applied = intents /\ applied # {}
    /\ n \in Providers /\ state[n] = "RepairReady" /\ ~live[n] /\ ~lease[n]
    /\ state' = [state EXCEPT ![n] = "Repairing"]
    /\ live' = [live EXCEPT ![n] = TRUE]
    /\ lease' = [lease EXCEPT ![n] = TRUE]
    /\ incarnation' = [incarnation EXCEPT ![n] = 1]
    /\ UNCHANGED <<submitted,captured,intents,frontier,frozen,applied,
         evidence,reopens,online,crashed,mode,published,closed,obligations>>

FinishRepair(n) ==
    /\ online /\ mode = "Running" /\ state[n] = "Repairing"
    /\ state' = [state EXCEPT ![n] = "RepairCheckerReady"]
    /\ live' = [live EXCEPT ![n] = FALSE]
    /\ lease' = [lease EXCEPT ![n] = FALSE]
    /\ UNCHANGED <<submitted,captured,intents,frontier,frozen,applied,
         evidence,reopens,incarnation,online,crashed,mode,published,closed,obligations>>

StartRepairChecker(n) ==
    /\ online /\ mode = "Running" /\ state[n] = "RepairCheckerReady"
    /\ \A p \in CheckerProviders(n) : state[p] = "Accepted"
    /\ state' = [state EXCEPT ![n] = "RepairChecking"]
    /\ live' = [live EXCEPT ![n] = TRUE]
    /\ lease' = [lease EXCEPT ![n] = TRUE]
    /\ UNCHANGED <<submitted,captured,intents,frontier,frozen,applied,
         evidence,reopens,incarnation,online,crashed,mode,published,closed,obligations>>

FinishRepairChecker(n) ==
    /\ online /\ mode = "Running" /\ state[n] = "RepairChecking"
    /\ state' = [state EXCEPT ![n] = "Accepted"]
    /\ live' = [live EXCEPT ![n] = FALSE]
    /\ lease' = [lease EXCEPT ![n] = FALSE]
    /\ UNCHANGED <<submitted,captured,intents,frontier,frozen,applied,
         evidence,reopens,incarnation,online,crashed,mode,published,closed,obligations>>

ReleaseConsumers ==
    /\ online /\ mode = "Running" /\ applied # {}
    /\ \A n \in Providers : state[n] = "Accepted"
    /\ \E n \in Consumers : state[n] = "Stale"
    /\ state' = [n \in Nodes |-> IF n \in Consumers THEN "ProducerReady" ELSE state[n]]
    /\ UNCHANGED <<live,lease,submitted,captured,intents,frontier,frozen,applied,
         evidence,reopens,incarnation,online,crashed,mode,published,closed,obligations>>

\* A repeated applied original packet, failed reap, or stale old-incarnation
\* cancellation acknowledgement is a no-op; none can use a replacement fence.
Replay == /\ online /\ UNCHANGED vars

Crash ==
    /\ online /\ ~crashed
    /\ online' = FALSE /\ crashed' = TRUE
    /\ UNCHANGED <<state,live,lease,submitted,captured,intents,frontier,frozen,
         applied,evidence,reopens,incarnation,mode,published,closed,obligations>>
Restart ==
    /\ ~online /\ online' = TRUE
    /\ UNCHANGED <<state,live,lease,submitted,captured,intents,frontier,frozen,
         applied,evidence,reopens,incarnation,crashed,mode,published,closed,obligations>>
Supersede ==
    /\ online /\ mode = "Running" /\ applied = {}
    /\ mode' = "Superseded"
    /\ UNCHANGED <<state,live,lease,submitted,captured,intents,frontier,frozen,
         applied,evidence,reopens,incarnation,online,crashed,published,closed,obligations>>

Next == SubmitPeer \/ (\E n \in Nodes : Reap(n) \/ SealAssignment(n) \/ Release(n))
     \/ (\E k \in Packets : Capture(k) \/ Register(k))
     \/ Apply \/ (\E n \in Nodes : StartRepair(n) \/ FinishRepair(n)
                                \/ StartRepairChecker(n) \/ FinishRepairChecker(n))
     \/ ReleaseConsumers \/ Replay \/ Crash \/ Restart \/ Supersede
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ state \in [Nodes -> States] /\ live \in [Nodes -> BOOLEAN]
    /\ lease \in [Nodes -> BOOLEAN]
    /\ submitted \subseteq Packets /\ captured \subseteq submitted
    /\ obligations \subseteq captured \cap InvalidPackets
    /\ intents \subseteq captured \cap DependencyPackets
    /\ frontier \subseteq submitted /\ frozen \subseteq Nodes /\ closed \subseteq Nodes
    /\ applied \subseteq intents /\ evidence \in [Nodes -> SUBSET Packets]
    /\ reopens \in [Nodes -> 0..1] /\ incarnation \in [Nodes -> 0..1]
    /\ online \in BOOLEAN /\ crashed \in BOOLEAN
    /\ mode \in {"Running","Superseded"} /\ published \in BOOLEAN
QuiescenceBeforeStale == \A n \in Nodes :
    state[n] \in {"Stale","RepairReady","ProducerReady"} => ~live[n] /\ ~lease[n]
NoAdmissionBeforeApply == \A n \in frozen : incarnation[n] = 1 => applied # {}
FrontierCompleteness == intents # {} /\ frozen \subseteq closed =>
    frontier = {k \in submitted : Source(k) \in frozen}
AllPeerEvidenceBeforeApply == applied # {} => Drained /\
    (submitted \cap DependencyPackets) \subseteq intents
InvalidRouteEvidenceRetained == captured \cap InvalidPackets \subseteq obligations
IneligibleReportsNeverProviderTargets == intents \cap InvalidPackets = {}
OneReopenPerWave == \A n \in Nodes : reopens[n] <= 1
JoinedProviderEvidence == applied # {} => \A n \in Nodes : evidence[n] = ExpectedEvidence(n)
TargetsNotStaledAsConsumers == applied # {} => \A n \in Providers : state[n] # "Stale"
NoReplacementWithOldFence == \A n \in Nodes :
    state[n] \in {"Repairing", "RepairChecking"} =>
        incarnation[n] = 1 /\ live[n] /\ lease[n]
RepairCheckersAwaitAcceptedInputs == \A n \in Nodes :
    state[n] = "RepairChecking" =>
        \A p \in CheckerProviders(n) : state[p] = "Accepted"
SupersessionDominates == mode = "Superseded" => applied = {} /\ \A n \in Nodes : reopens[n] = 0
NoInvalidatedPublication == ~published
CapturedEvidenceNeverLost == [][captured \subseteq captured']_vars
SubmittedEvidenceNeverLost == [][submitted \subseteq submitted']_vars
Completed == mode = "Superseded" \/
    (applied # {} /\ \A n \in Nodes : state[n] \in {"Accepted","ProducerReady"})

\* Eventual cleanup success is represented by weak fairness of Reap/Release.
\* This liveness property requires fair successful cleanup. The Python tests
\* check bounded safety and terminal reachability, not this temporal property.
FairSpec == Spec /\ WF_vars(Restart) /\ WF_vars(Apply) /\ WF_vars(ReleaseConsumers)
    /\ (\A n \in Nodes : WF_vars(Reap(n)) /\ WF_vars(SealAssignment(n)) /\ WF_vars(Release(n))
             /\ WF_vars(StartRepair(n)) /\ WF_vars(FinishRepair(n))
             /\ WF_vars(StartRepairChecker(n)) /\ WF_vars(FinishRepairChecker(n)))
    /\ (\A k \in Packets : WF_vars(Capture(k)) /\ WF_vars(Register(k)))
Progress == <>Completed
=================================================================
