----------------------- MODULE ProduceCheckCycle -----------------------
EXTENDS Naturals

CONSTANT MaxGeneration

States == {
    "ProducerReady", "Producing", "CheckerReady", "Checking",
    "RepairReady", "HumanReview", "Accepted", "Rejected",
    "TriageRequired", "Cancelled"
}
Slots == {"None", "Producer", "Checker"}

VARIABLES state, generation, activeSlot, productGeneration,
          verdictGeneration, resumeState

vars == <<state, generation, activeSlot, productGeneration,
          verdictGeneration, resumeState>>

Init ==
    /\ state = "ProducerReady"
    /\ generation = 1
    /\ activeSlot = "None"
    /\ productGeneration = 0
    /\ verdictGeneration = 0
    /\ resumeState = "ProducerReady"

StartProducer ==
    /\ state \in {"ProducerReady", "RepairReady"}
    /\ state' = "Producing"
    /\ activeSlot' = "Producer"
    /\ UNCHANGED <<generation, productGeneration, verdictGeneration, resumeState>>

SubmitProduct ==
    /\ state = "Producing"
    /\ activeSlot = "Producer"
    /\ state' = "CheckerReady"
    /\ activeSlot' = "None"
    /\ productGeneration' = generation
    /\ UNCHANGED <<generation, verdictGeneration, resumeState>>

RejectProduct ==
    /\ state = "Producing"
    /\ activeSlot = "Producer"
    /\ state' = "RepairReady"
    /\ activeSlot' = "None"
    /\ UNCHANGED <<generation, productGeneration, verdictGeneration, resumeState>>

StartChecker ==
    /\ state = "CheckerReady"
    /\ productGeneration = generation
    /\ state' = "Checking"
    /\ activeSlot' = "Checker"
    /\ UNCHANGED <<generation, productGeneration, verdictGeneration, resumeState>>

RejectByChecker ==
    /\ state = "Checking"
    /\ activeSlot = "Checker"
    /\ state' = "RepairReady"
    /\ activeSlot' = "None"
    /\ verdictGeneration' = generation
    /\ UNCHANGED <<generation, productGeneration, resumeState>>

AcceptByChecker ==
    /\ state = "Checking"
    /\ activeSlot = "Checker"
    /\ state' = "HumanReview"
    /\ activeSlot' = "None"
    /\ verdictGeneration' = generation
    /\ UNCHANGED <<generation, productGeneration, resumeState>>

HumanAccept ==
    /\ state = "HumanReview"
    /\ state' = "Accepted"
    /\ UNCHANGED <<generation, activeSlot, productGeneration,
                    verdictGeneration, resumeState>>

HumanReject ==
    /\ state = "HumanReview"
    /\ state' = "Rejected"
    /\ UNCHANGED <<generation, activeSlot, productGeneration,
                    verdictGeneration, resumeState>>

HumanEdit ==
    /\ state = "HumanReview"
    /\ generation < MaxGeneration
    /\ state' = "RepairReady"
    /\ generation' = generation + 1
    /\ productGeneration' = 0
    /\ verdictGeneration' = 0
    /\ activeSlot' = "None"
    /\ UNCHANGED resumeState

RequireTriage ==
    /\ state \in {"Producing", "Checking"}
    /\ state' = "TriageRequired"
    /\ resumeState' = IF state = "Producing" THEN "ProducerReady" ELSE "CheckerReady"
    /\ activeSlot' = "None"
    /\ UNCHANGED <<generation, productGeneration, verdictGeneration>>

ResolveTriage ==
    /\ state = "TriageRequired"
    /\ state' = resumeState
    /\ resumeState' = "ProducerReady"
    /\ UNCHANGED <<generation, activeSlot, productGeneration, verdictGeneration>>

Cancel ==
    /\ state \notin {"Accepted", "Rejected", "Cancelled"}
    /\ state' = "Cancelled"
    /\ activeSlot' = "None"
    /\ UNCHANGED <<generation, productGeneration, verdictGeneration, resumeState>>

Next ==
    \/ StartProducer \/ SubmitProduct \/ RejectProduct
    \/ StartChecker \/ RejectByChecker \/ AcceptByChecker
    \/ HumanAccept \/ HumanReject \/ HumanEdit
    \/ RequireTriage \/ ResolveTriage \/ Cancel

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ state \in States
    /\ generation \in 1..MaxGeneration
    /\ activeSlot \in Slots
    /\ productGeneration \in 0..MaxGeneration
    /\ verdictGeneration \in 0..MaxGeneration
    /\ resumeState \in States

ExactlyOneRunningSlot ==
    /\ (state = "Producing") <=> (activeSlot = "Producer")
    /\ (state = "Checking") <=> (activeSlot = "Checker")

AcceptedIsCurrent ==
    state \in {"HumanReview", "Accepted"} =>
        /\ productGeneration = generation
        /\ verdictGeneration = generation

TriageResumesAtAssignmentBoundary ==
    state = "TriageRequired" =>
        resumeState \in {"ProducerReady", "CheckerReady"}

=======================================================================
