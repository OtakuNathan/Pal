-------------------- MODULE GraphExecutionLifecycle --------------------
EXTENDS Naturals, FiniteSets

CONSTANT A, B, Sink, MaxRepairs

Nodes == {A, B, Sink}
NonSink == Nodes \ {Sink}
States == {
    "Blocked", "ProducerReady", "Producing", "CheckerReady",
    "Checking", "RepairReady", "Stale", "Accepted"
}

VARIABLES graphState, nodeState, productReady, publishedSink, repairBarrier, repairs

vars == <<graphState, nodeState, productReady, publishedSink, repairBarrier, repairs>>

Init ==
    /\ graphState = "Running"
    /\ nodeState = [n \in Nodes |-> "ProducerReady"]
    /\ productReady = [n \in Nodes |-> FALSE]
    /\ publishedSink = FALSE
    /\ repairBarrier = [n \in Nodes |-> FALSE]
    /\ repairs = 0

StartProducer(n) ==
    /\ graphState = "Running"
    /\ n \in Nodes
    /\ nodeState[n] \in {"ProducerReady", "RepairReady"}
    /\ nodeState' = [nodeState EXCEPT ![n] = "Producing"]
    /\ UNCHANGED <<graphState, productReady, publishedSink, repairBarrier, repairs>>

SubmitProduct(n) ==
    /\ graphState = "Running"
    /\ n \in Nodes
    /\ nodeState[n] = "Producing"
    /\ nodeState' = [nodeState EXCEPT ![n] = "CheckerReady"]
    /\ productReady' = [productReady EXCEPT ![n] = TRUE]
    /\ UNCHANGED <<graphState, publishedSink, repairBarrier, repairs>>

StartChecker(n) ==
    /\ graphState = "Running"
    /\ n \in Nodes
    /\ nodeState[n] = "CheckerReady"
    /\ productReady[n]
    /\ IF n = Sink
          THEN \A m \in NonSink : nodeState[m] = "Accepted"
          ELSE TRUE
    /\ nodeState' = [nodeState EXCEPT ![n] = "Checking"]
    /\ UNCHANGED <<graphState, productReady, publishedSink, repairBarrier, repairs>>

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

DependencyFinding ==
    /\ graphState = "Running"
    /\ nodeState[Sink] = "Checking"
    /\ repairs < MaxRepairs
    /\ nodeState' = [nodeState EXCEPT
        ![A] = "RepairReady", ![B] = "Stale", ![Sink] = "Stale"]
    /\ productReady' = [productReady EXCEPT ![A] = FALSE, ![B] = FALSE, ![Sink] = FALSE]
    /\ repairBarrier' = [repairBarrier EXCEPT ![B] = TRUE, ![Sink] = TRUE]
    /\ repairs' = repairs + 1
    /\ publishedSink' = FALSE
    /\ UNCHANGED graphState

ReleaseRepairBarriers ==
    /\ graphState = "Running"
    /\ nodeState[A] = "Accepted"
    /\ repairBarrier[B]
    /\ repairBarrier' = [repairBarrier EXCEPT ![B] = FALSE, ![Sink] = FALSE]
    /\ nodeState' = [nodeState EXCEPT
        ![B] = "ProducerReady", ![Sink] = "ProducerReady"]
    /\ UNCHANGED <<graphState, productReady, publishedSink, repairs>>

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
    /\ repairBarrier' = [m \in Nodes |-> FALSE]
    /\ UNCHANGED repairs

Next ==
    \/ \E n \in Nodes : StartProducer(n)
    \/ \E n \in Nodes : SubmitProduct(n)
    \/ \E n \in Nodes : StartChecker(n)
    \/ \E n \in Nodes : AcceptNode(n)
    \/ DependencyFinding
    \/ ReleaseRepairBarriers
    \/ \E n \in Nodes : ArchitectureFinding(n)

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ graphState \in {"Running", "ReplanRequired"}
    /\ nodeState \in [Nodes -> States]
    /\ productReady \in [Nodes -> BOOLEAN]
    /\ publishedSink \in BOOLEAN
    /\ repairBarrier \in [Nodes -> BOOLEAN]
    /\ repairs \in 0..MaxRepairs

SinkCheckerStartsAfterAllModules ==
    nodeState[Sink] \in {"Checking", "Accepted"} =>
        \A n \in NonSink : nodeState[n] = "Accepted"

SinkProducerNeverDependencyBlocked ==
    nodeState[Sink] # "Blocked"

PublishedOnlyFromAcceptedSink ==
    publishedSink => nodeState[Sink] = "Accepted"

RepairBarrierBlocksConsumers ==
    repairBarrier[Sink] => nodeState[Sink] \in {"Blocked", "Stale"}

ReplanNeverPublishes ==
    graphState = "ReplanRequired" => ~publishedSink

=======================================================================
