------------------- MODULE SystemVerificationLifecycle -------------------
EXTENDS Naturals, TLC

CONSTANTS MaxGeneration, MaxCases

States == {"Blocked", "Ready", "Running", "Accepted", "Repairing", "Cancelled"}
SessionStates == {"Suspended", "Active", "Completed", "Cancelled"}
WorkflowStates == {"Running", "Completed", "Cancelled"}

VARIABLES
    state,
    session,
    generation,
    regressionGeneration,
    deltaReviewGeneration,
    harnessCases,
    realDeliveryEvidence,
    workflowState

vars == <<
    state, session, generation, regressionGeneration, deltaReviewGeneration,
    harnessCases,
    realDeliveryEvidence, workflowState
>>

Init ==
    /\ state = "Blocked"
    /\ session = "Suspended"
    /\ generation = 1
    /\ regressionGeneration = 0
    /\ deltaReviewGeneration = 0
    /\ harnessCases = 0
    /\ realDeliveryEvidence = FALSE
    /\ workflowState = "Running"

ModulesAccepted ==
    /\ workflowState = "Running"
    /\ state \in {"Blocked", "Repairing"}
    /\ state' = "Ready"
    /\ UNCHANGED <<session, generation, regressionGeneration,
        deltaReviewGeneration, harnessCases,
        realDeliveryEvidence, workflowState>>

StartSystemVerifier ==
    /\ workflowState = "Running"
    /\ state = "Ready"
    /\ state' = "Running"
    /\ session' = "Active"
    /\ UNCHANGED <<generation, regressionGeneration, deltaReviewGeneration,
        harnessCases,
        realDeliveryEvidence, workflowState>>

ReplayRegressions ==
    /\ state = "Running"
    /\ session = "Active"
    /\ regressionGeneration' = generation
    /\ UNCHANGED <<state, session, generation, deltaReviewGeneration,
        harnessCases, realDeliveryEvidence, workflowState>>

ReviewCandidateDelta ==
    /\ state = "Running"
    /\ session = "Active"
    /\ regressionGeneration = generation
    /\ deltaReviewGeneration' = generation
    /\ UNCHANGED <<state, session, generation, regressionGeneration,
        harnessCases, realDeliveryEvidence, workflowState>>

AddHarnessCase ==
    /\ state = "Running"
    /\ session = "Active"
    /\ harnessCases < MaxCases
    /\ harnessCases' = harnessCases + 1
    /\ UNCHANGED <<state, session, generation, regressionGeneration,
        deltaReviewGeneration,
        realDeliveryEvidence, workflowState>>

RecordRealDeliveryEvidence ==
    /\ state = "Running"
    /\ session = "Active"
    /\ harnessCases > 0
    /\ realDeliveryEvidence' = TRUE
    /\ UNCHANGED <<state, session, generation, regressionGeneration,
        deltaReviewGeneration, harnessCases, workflowState>>

Pass ==
    /\ state = "Running"
    /\ session = "Active"
    /\ harnessCases > 0
    /\ realDeliveryEvidence
    /\ regressionGeneration = generation
    /\ deltaReviewGeneration = generation
    /\ state' = "Accepted"
    /\ session' = "Suspended"
    /\ UNCHANGED <<generation, regressionGeneration, deltaReviewGeneration,
        harnessCases,
        realDeliveryEvidence, workflowState>>

FindModuleDefect ==
    /\ state = "Running"
    /\ session = "Active"
    /\ harnessCases > 0
    /\ generation < MaxGeneration
    /\ regressionGeneration = generation
    /\ deltaReviewGeneration = generation
    /\ state' = "Repairing"
    /\ session' = "Suspended"
    /\ generation' = generation + 1
    /\ realDeliveryEvidence' = FALSE
    /\ UNCHANGED <<regressionGeneration, deltaReviewGeneration,
        harnessCases, workflowState>>

CompleteWorkflow ==
    /\ state = "Accepted"
    /\ workflowState = "Running"
    /\ workflowState' = "Completed"
    /\ session' = "Completed"
    /\ UNCHANGED <<state, generation, regressionGeneration,
        deltaReviewGeneration, harnessCases, realDeliveryEvidence>>

CancelWorkflow ==
    /\ workflowState = "Running"
    /\ workflowState' = "Cancelled"
    /\ state' = "Cancelled"
    /\ session' = "Cancelled"
    /\ UNCHANGED <<generation, regressionGeneration, deltaReviewGeneration,
        harnessCases, realDeliveryEvidence>>

Next ==
    \/ ModulesAccepted
    \/ StartSystemVerifier
    \/ ReplayRegressions
    \/ ReviewCandidateDelta
    \/ AddHarnessCase
    \/ RecordRealDeliveryEvidence
    \/ Pass
    \/ FindModuleDefect
    \/ CompleteWorkflow
    \/ CancelWorkflow

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ state \in States
    /\ session \in SessionStates
    /\ generation \in 1..MaxGeneration
    /\ regressionGeneration \in 0..MaxGeneration
    /\ deltaReviewGeneration \in 0..MaxGeneration
    /\ harnessCases \in 0..MaxCases
    /\ realDeliveryEvidence \in BOOLEAN
    /\ workflowState \in WorkflowStates

PassRequiresRealBoundary ==
    state = "Accepted" =>
        /\ harnessCases > 0
        /\ realDeliveryEvidence

RepairPreservesHarness ==
    state = "Repairing" => harnessCases > 0

WorkflowOwnsSessionLifetime ==
    workflowState = "Running" => session \notin {"Completed", "Cancelled"}

AcceptedCoversCurrentGeneration ==
    state = "Accepted" =>
        /\ regressionGeneration = generation
        /\ deltaReviewGeneration = generation

DeltaReviewFollowsRegression ==
    deltaReviewGeneration <= regressionGeneration

=============================================================================
