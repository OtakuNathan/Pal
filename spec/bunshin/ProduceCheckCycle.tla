----------------------- MODULE ProduceCheckCycle -----------------------
EXTENDS Naturals

CONSTANTS MaxGeneration, CycleKind

States == {
    "ProducerReady", "Producing", "CheckerReady", "Checking",
    "RepairReady", "HumanReview", "Accepted", "Rejected",
    "TriageRequired", "Paused", "Cancelled"
}
Slots == {"None", "Producer", "Checker"}

VARIABLES state, generation, activeSlot, productGeneration,
          verdictGeneration, resumeState, resultPending, pauseRequested

vars == <<state, generation, activeSlot, productGeneration,
          verdictGeneration, resumeState, resultPending, pauseRequested>>

Init ==
    /\ state = "ProducerReady"
    /\ generation = 1
    /\ activeSlot = "None"
    /\ productGeneration = 0
    /\ verdictGeneration = 0
    /\ resumeState = "ProducerReady"
    /\ resultPending = FALSE
    /\ pauseRequested = FALSE

\* Only a pristine PlanCycle can bind an imported architecture product.
\* productGeneration abstracts its nonempty durable product_ref. The router's
\* provenance and transaction checks are refined by ImportedPlanLifecycle.
ImportProduct ==
    /\ CycleKind = "Plan"
    /\ state = "ProducerReady"
    /\ generation = 1
    /\ activeSlot = "None"
    /\ productGeneration = 0
    /\ verdictGeneration = 0
    /\ state' = "CheckerReady"
    /\ productGeneration' = generation
    /\ UNCHANGED <<generation, activeSlot, verdictGeneration, resumeState>>
    /\ UNCHANGED <<resultPending, pauseRequested>>

StartProducer ==
    /\ state \in {"ProducerReady", "RepairReady"}
    /\ ~pauseRequested
    /\ state' = "Producing"
    /\ activeSlot' = "Producer"
    /\ resumeState' = IF CycleKind = "Node" THEN state ELSE "ProducerReady"
    /\ UNCHANGED <<generation, productGeneration, verdictGeneration>>
    /\ UNCHANGED <<resultPending, pauseRequested>>

SubmitProduct ==
    /\ state = "Producing"
    /\ (CycleKind = "Plan" \/ resultPending)
    /\ activeSlot = "Producer"
    /\ state' = IF pauseRequested THEN "Paused" ELSE "CheckerReady"
    /\ resumeState' = IF pauseRequested THEN "CheckerReady" ELSE resumeState
    /\ activeSlot' = "None"
    /\ productGeneration' = generation
    /\ UNCHANGED <<generation, verdictGeneration>>
    /\ resultPending' = FALSE
    /\ UNCHANGED pauseRequested

RejectProduct ==
    /\ state = "Producing"
    /\ (CycleKind = "Plan" \/ resultPending)
    /\ activeSlot = "Producer"
    /\ state' = IF pauseRequested THEN "Paused" ELSE "RepairReady"
    /\ resumeState' = IF pauseRequested THEN "RepairReady" ELSE resumeState
    /\ activeSlot' = "None"
    /\ UNCHANGED <<generation, productGeneration, verdictGeneration>>
    /\ resultPending' = FALSE
    /\ UNCHANGED pauseRequested

StartChecker ==
    /\ state = "CheckerReady"
    /\ productGeneration = generation
    /\ ~pauseRequested
    /\ state' = "Checking"
    /\ activeSlot' = "Checker"
    /\ UNCHANGED <<generation, productGeneration, verdictGeneration, resumeState>>
    /\ UNCHANGED <<resultPending, pauseRequested>>

RejectByChecker ==
    /\ state = "Checking"
    /\ (CycleKind = "Plan" \/ resultPending)
    /\ activeSlot = "Checker"
    /\ state' = IF pauseRequested THEN "Paused" ELSE "RepairReady"
    /\ resumeState' = IF pauseRequested THEN "RepairReady" ELSE resumeState
    /\ activeSlot' = "None"
    /\ verdictGeneration' = generation
    /\ UNCHANGED <<generation, productGeneration>>
    /\ resultPending' = FALSE
    /\ UNCHANGED pauseRequested

AcceptByChecker ==
    /\ state = "Checking"
    /\ (CycleKind = "Plan" \/ resultPending)
    /\ activeSlot = "Checker"
    /\ state' = IF CycleKind = "Plan" THEN "HumanReview" ELSE "Accepted"
    /\ activeSlot' = "None"
    /\ verdictGeneration' = generation
    /\ UNCHANGED <<generation, productGeneration, resumeState>>
    /\ resultPending' = FALSE
    /\ UNCHANGED pauseRequested

HumanAccept ==
    /\ state = "HumanReview"
    /\ state' = "Accepted"
    /\ UNCHANGED <<generation, activeSlot, productGeneration,
                    verdictGeneration, resumeState>>
    /\ UNCHANGED <<resultPending, pauseRequested>>

HumanReject ==
    /\ state = "HumanReview"
    /\ state' = "Rejected"
    /\ UNCHANGED <<generation, activeSlot, productGeneration,
                    verdictGeneration, resumeState>>
    /\ UNCHANGED <<resultPending, pauseRequested>>

HumanEdit ==
    /\ state = "HumanReview"
    /\ generation < MaxGeneration
    /\ state' = "RepairReady"
    /\ generation' = generation + 1
    /\ productGeneration' = 0
    /\ verdictGeneration' = 0
    /\ activeSlot' = "None"
    /\ UNCHANGED resumeState
    /\ UNCHANGED <<resultPending, pauseRequested>>

RequireTriage ==
    /\ state \in {"Producing", "Checking"}
    /\ ~resultPending
    /\ state' = "TriageRequired"
    /\ resumeState' = IF state = "Producing" THEN resumeState ELSE "CheckerReady"
    /\ activeSlot' = "None"
    /\ UNCHANGED <<generation, productGeneration, verdictGeneration>>
    /\ UNCHANGED <<resultPending, pauseRequested>>

ResolveTriage ==
    /\ state = "TriageRequired"
    /\ state' = resumeState
    /\ resumeState' = "ProducerReady"
    /\ UNCHANGED <<generation, activeSlot, productGeneration, verdictGeneration>>
    /\ UNCHANGED <<resultPending, pauseRequested>>

\* ResultPending represents the committed node submission, before the
\* quiescence/snapshot effect publishes the semantic result.
RecordResult ==
    /\ CycleKind = "Node"
    /\ state \in {"Producing", "Checking"}
    /\ ~resultPending /\ ~pauseRequested
    /\ resultPending' = TRUE
    /\ UNCHANGED <<state, generation, activeSlot, productGeneration,
                   verdictGeneration, resumeState, pauseRequested>>

RequestPause ==
    /\ CycleKind = "Node"
    /\ ~pauseRequested
    /\ state \in {"ProducerReady", "Producing", "RepairReady", "CheckerReady", "Checking"}
    /\ pauseRequested' = TRUE
    /\ IF resultPending
        THEN UNCHANGED <<state, activeSlot, resumeState>>
        ELSE /\ state' = "Paused"
             /\ activeSlot' = "None"
             /\ resumeState' = IF state = "Producing" THEN resumeState
                               ELSE IF state = "Checking" THEN "CheckerReady" ELSE state
    /\ UNCHANGED <<generation, productGeneration, verdictGeneration, resultPending>>

Resume ==
    /\ state = "Paused"
    /\ state' = resumeState
    /\ pauseRequested' = FALSE
    /\ UNCHANGED <<generation, activeSlot, productGeneration, verdictGeneration,
                   resumeState, resultPending>>

Cancel ==
    /\ state \notin {"Accepted", "Rejected", "Cancelled"}
    /\ state' = "Cancelled"
    /\ activeSlot' = "None"
    /\ UNCHANGED <<generation, productGeneration, verdictGeneration, resumeState>>
    /\ resultPending' = FALSE
    /\ pauseRequested' = FALSE

Next ==
    \/ ImportProduct \/ StartProducer \/ SubmitProduct \/ RejectProduct
    \/ StartChecker \/ RejectByChecker \/ AcceptByChecker
    \/ HumanAccept \/ HumanReject \/ HumanEdit
    \/ RequireTriage \/ ResolveTriage \/ Cancel
    \/ RecordResult \/ RequestPause \/ Resume

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ CycleKind \in {"Plan", "Node"}
    /\ state \in States
    /\ generation \in 1..MaxGeneration
    /\ activeSlot \in Slots
    /\ productGeneration \in 0..MaxGeneration
    /\ verdictGeneration \in 0..MaxGeneration
    /\ resumeState \in States
    /\ resultPending \in BOOLEAN
    /\ pauseRequested \in BOOLEAN

ExactlyOneRunningSlot ==
    /\ (state = "Producing") <=> (activeSlot = "Producer")
    /\ (state = "Checking") <=> (activeSlot = "Checker")

AcceptedIsCurrent ==
    state \in {"HumanReview", "Accepted"} =>
        /\ productGeneration = generation
        /\ verdictGeneration = generation

TriageResumesAtAssignmentBoundary ==
    state = "TriageRequired" =>
        resumeState \in IF CycleKind = "Node"
                        THEN {"ProducerReady", "RepairReady", "CheckerReady"}
                        ELSE {"ProducerReady", "CheckerReady"}

PendingResultOwnsCursor == resultPending => state \in {"Producing", "Checking"}

PauseDrainsBeforeFreeze ==
    /\ (state = "Paused" => ~resultPending /\ pauseRequested)
    /\ (pauseRequested /\ ~resultPending => state \notin {"Producing", "Checking"})

=======================================================================
