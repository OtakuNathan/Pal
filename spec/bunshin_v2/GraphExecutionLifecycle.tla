-------------------- MODULE GraphExecutionLifecycle --------------------
EXTENDS Naturals, FiniteSets

CONSTANT A, B, Sink, MaxRepairs, MaxCorrections

Nodes == {A, B, Sink}
NonSink == Nodes \ {Sink}
\* A's produced implementation is a declared EXECUTION input of B's checker.
\* The software sink checker consumes every produced module. All initial
\* software producers remain independent and start from contracts in parallel.
CheckerProviders(n) == IF n = Sink THEN NonSink
                       ELSE IF n = B THEN {A} ELSE {}
SemanticConsumers(n) == IF n = A THEN {B, Sink}
                        ELSE IF n = B THEN {Sink} ELSE {}
AffectedConsumers(n, providers) ==
    ({n} \cup SemanticConsumers(n)
        \cup UNION {SemanticConsumers(p) : p \in providers}) \ providers
States == {
    "Blocked", "ProducerReady", "Producing", "CheckerReady",
    "Checking", "RepairReady", "Stale", "Accepted", "TriageRequired"
}

VARIABLES graphState, nodeState, productReady, publishedSink, repairBarrier, repairs
VARIABLES corrections, invalidSubmissionPreserved

vars == <<graphState, nodeState, productReady, publishedSink, repairBarrier, repairs,
          corrections, invalidSubmissionPreserved>>

Init ==
    /\ graphState = "Running"
    /\ nodeState = [n \in Nodes |-> "ProducerReady"]
    /\ productReady = [n \in Nodes |-> FALSE]
    /\ publishedSink = FALSE
    /\ repairBarrier = [n \in Nodes |-> {}]
    /\ repairs = 0
    /\ corrections = [n \in Nodes |-> 0]
    /\ invalidSubmissionPreserved = [n \in Nodes |-> FALSE]

StartProducer(n) ==
    /\ graphState = "Running"
    /\ n \in Nodes
    /\ nodeState[n] \in {"ProducerReady", "RepairReady"}
    /\ repairBarrier[n] = {}
    /\ nodeState' = [nodeState EXCEPT ![n] = "Producing"]
    /\ UNCHANGED <<graphState, productReady, publishedSink, repairBarrier, repairs>>
    /\ UNCHANGED <<corrections, invalidSubmissionPreserved>>

SubmitProduct(n) ==
    /\ graphState = "Running"
    /\ n \in Nodes
    /\ nodeState[n] = "Producing"
    /\ nodeState' = [nodeState EXCEPT ![n] = "CheckerReady"]
    /\ productReady' = [productReady EXCEPT ![n] = TRUE]
    /\ corrections' = [corrections EXCEPT ![n] = 0]
    /\ invalidSubmissionPreserved' = [invalidSubmissionPreserved EXCEPT ![n] = FALSE]
    /\ UNCHANGED <<graphState, publishedSink, repairBarrier, repairs>>

StartChecker(n) ==
    /\ graphState = "Running"
    /\ n \in Nodes
    /\ nodeState[n] = "CheckerReady"
    /\ productReady[n]
    /\ repairBarrier[n] = {}
    /\ \A m \in CheckerProviders(n) : nodeState[m] = "Accepted"
    /\ nodeState' = [nodeState EXCEPT ![n] = "Checking"]
    /\ UNCHANGED <<graphState, productReady, publishedSink, repairBarrier, repairs>>
    /\ UNCHANGED <<corrections, invalidSubmissionPreserved>>

AcceptNode(n) ==
    /\ graphState = "Running"
    /\ n \in Nodes
    /\ nodeState[n] = "Checking"
    /\ IF n = Sink
          THEN \A m \in NonSink : nodeState[m] = "Accepted"
          ELSE TRUE
    /\ nodeState' = [nodeState EXCEPT ![n] = "Accepted"]
    /\ publishedSink' = IF n = Sink THEN TRUE ELSE publishedSink
    /\ UNCHANGED <<graphState, productReady, repairBarrier, repairs>>
    /\ UNCHANGED <<corrections, invalidSubmissionPreserved>>

\* This graph-only action is the atomic post-quiescence application cut.
\* DependencyRepairCohort models physical cleanup and immutable receipt drain;
\* this older lifecycle model does not equate CHECKING with a live process.
DependencyFinding(n, providers) ==
    /\ graphState = "Running"
    /\ n \in Nodes
    /\ nodeState[n] = "Checking"
    /\ providers # {}
    /\ providers \subseteq CheckerProviders(n)
    /\ \A p \in providers : nodeState[p] = "Accepted"
    /\ repairs < MaxRepairs
    /\ nodeState' = [m \in Nodes |->
        IF m \in providers THEN "RepairReady"
        ELSE IF m \in AffectedConsumers(n, providers) THEN "Stale"
        ELSE nodeState[m]]
    /\ productReady' = [m \in Nodes |->
        IF m \in providers \cup AffectedConsumers(n, providers)
        THEN FALSE ELSE productReady[m]]
    /\ repairBarrier' = [m \in Nodes |->
        IF m \in AffectedConsumers(n, providers)
        THEN repairBarrier[m] \cup {p \in providers :
            m \in {n} \cup SemanticConsumers(n) \cup SemanticConsumers(p)}
        ELSE repairBarrier[m]]
    /\ repairs' = repairs + 1
    /\ publishedSink' = FALSE
    /\ UNCHANGED graphState
    /\ UNCHANGED <<corrections, invalidSubmissionPreserved>>

ReleaseRepairBarriers(n) ==
    /\ graphState = "Running"
    /\ n \in Nodes
    /\ repairBarrier[n] # {}
    /\ \A p \in repairBarrier[n] : nodeState[p] = "Accepted"
    /\ repairBarrier' = [repairBarrier EXCEPT ![n] = {}]
    /\ nodeState' = [nodeState EXCEPT ![n] = "ProducerReady"]
    /\ UNCHANGED <<graphState, productReady, publishedSink, repairs>>
    /\ UNCHANGED <<corrections, invalidSubmissionPreserved>>

\* Tool preflight rejects an unbound route before accepting a submission. It
\* creates neither a durable verdict nor a correction attempt.
PreflightReject(n) ==
    /\ graphState = "Running"
    /\ n \in Nodes
    /\ nodeState[n] = "Checking"
    /\ UNCHANGED vars

\* Settlement may encounter an already accepted invalid submission. Preserve
\* it, and use CHECKER_RETRY -> CHECKER_READY without changing the candidate.
\* Correction budget is per candidate; exhausted submissions require triage.
AcceptedInvalidSubmission(n) ==
    /\ graphState = "Running"
    /\ n \in Nodes
    /\ nodeState[n] = "Checking"
    /\ nodeState' = [nodeState EXCEPT ![n] =
        IF corrections[n] + 1 < MaxCorrections THEN "CheckerReady" ELSE "TriageRequired"]
    /\ corrections' = [corrections EXCEPT ![n] =
        IF @ < MaxCorrections THEN @ + 1 ELSE @]
    /\ invalidSubmissionPreserved' = [invalidSubmissionPreserved EXCEPT ![n] = TRUE]
    /\ UNCHANGED <<graphState, productReady, publishedSink, repairBarrier, repairs>>

\* This is an explicit operator resolution, never automatic exhaustion reset.
ResolveInvalidSubmission(n) ==
    /\ graphState = "Running"
    /\ n \in Nodes
    /\ nodeState[n] = "TriageRequired"
    /\ nodeState' = [nodeState EXCEPT ![n] = "CheckerReady"]
    /\ corrections' = [corrections EXCEPT ![n] = 0]
    /\ UNCHANGED <<graphState, productReady, publishedSink, repairBarrier, repairs,
                    invalidSubmissionPreserved>>

\* Contract, architecture and requirements defects all request replanning.
ArchitectureFinding(n) ==
    /\ graphState = "Running"
    /\ n \in Nodes
    /\ nodeState[n] = "Checking"
    /\ graphState' = "ReplanRequired"
    /\ nodeState' = [m \in Nodes |->
        IF m = n THEN "Stale"
        ELSE IF nodeState[m] \in {"Producing", "Checking"}
             THEN nodeState[m]
             ELSE "Stale"]
    /\ productReady' = [m \in Nodes |->
        IF nodeState[m] \in {"Producing", "Checking"} /\ m # n
        THEN productReady[m]
        ELSE FALSE]
    /\ publishedSink' = FALSE
    /\ repairBarrier' = [m \in Nodes |-> {}]
    /\ UNCHANGED repairs
    /\ UNCHANGED <<corrections, invalidSubmissionPreserved>>

Next ==
    \/ \E n \in Nodes : StartProducer(n)
    \/ \E n \in Nodes : SubmitProduct(n)
    \/ \E n \in Nodes : StartChecker(n)
    \/ \E n \in Nodes : AcceptNode(n)
    \/ \E n \in Nodes, providers \in SUBSET Nodes : DependencyFinding(n, providers)
    \/ \E n \in Nodes : ReleaseRepairBarriers(n)
    \/ \E n \in Nodes : PreflightReject(n)
    \/ \E n \in Nodes : AcceptedInvalidSubmission(n)
    \/ \E n \in Nodes : ResolveInvalidSubmission(n)
    \/ \E n \in Nodes : ArchitectureFinding(n)

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ graphState \in {"Running", "ReplanRequired"}
    /\ nodeState \in [Nodes -> States]
    /\ productReady \in [Nodes -> BOOLEAN]
    /\ publishedSink \in BOOLEAN
    /\ repairBarrier \in [Nodes -> SUBSET Nodes]
    /\ repairs \in 0..MaxRepairs
    /\ corrections \in [Nodes -> 0..MaxCorrections]
    /\ invalidSubmissionPreserved \in [Nodes -> BOOLEAN]

SinkCheckerStartsAfterAllModules ==
    nodeState[Sink] \in {"Checking", "Accepted"} =>
        \A n \in NonSink : nodeState[n] = "Accepted"

CheckerStartsAfterProducedProviders ==
    \A n \in Nodes : nodeState[n] \in {"Checking", "Accepted"} =>
        \A p \in CheckerProviders(n) : nodeState[p] = "Accepted"

SinkProducerNeverDependencyBlocked ==
    nodeState[Sink] # "Blocked"

PublishedOnlyFromAcceptedSink ==
    publishedSink => nodeState[Sink] = "Accepted"

RepairBarrierBlocksConsumers ==
    \A n \in Nodes : repairBarrier[n] # {} => nodeState[n] = "Stale"

RepairTargetNeverWaitsOnItself ==
    \A n \in Nodes : n \notin repairBarrier[n]

CheckerAlwaysHasProduct ==
    \A n \in Nodes : nodeState[n] \in {"CheckerReady", "Checking", "Accepted"}
        => productReady[n]

CorrectionPreservesInvalidSubmission ==
    \A n \in Nodes : corrections[n] > 0 => invalidSubmissionPreserved[n]

ExhaustedCorrectionRequiresTriage ==
    \A n \in Nodes :
        /\ (nodeState[n] = "TriageRequired" =>
            corrections[n] = MaxCorrections /\ invalidSubmissionPreserved[n] /\ productReady[n])
        /\ (nodeState[n] \in {"CheckerReady", "Checking"} =>
            corrections[n] < MaxCorrections)

ReplanNeverPublishes ==
    graphState = "ReplanRequired" => ~publishedSink

=======================================================================
