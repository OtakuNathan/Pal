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
    harnessCases,
    realDeliveryEvidence,
    workflowState

vars == <<
    state, session, generation, harnessCases,
    realDeliveryEvidence, workflowState
>>

Init ==
    /\ state = "Blocked"
    /\ session = "Suspended"
    /\ generation = 1
    /\ harnessCases = 0
    /\ realDeliveryEvidence = FALSE
    /\ workflowState = "Running"

ModulesAccepted ==
    /\ workflowState = "Running"
    /\ state \in {"Blocked", "Repairing"}
    /\ state' = "Ready"
    /\ UNCHANGED <<session, generation, harnessCases,
        realDeliveryEvidence, workflowState>>

StartSystemVerifier ==
    /\ workflowState = "Running"
    /\ state = "Ready"
    /\ state' = "Running"
    /\ session' = "Active"
    /\ UNCHANGED <<generation, harnessCases,
        realDeliveryEvidence, workflowState>>

AddHarnessCase ==
    /\ state = "Running"
    /\ session = "Active"
    /\ harnessCases < MaxCases
    /\ harnessCases' = harnessCases + 1
    /\ UNCHANGED <<state, session, generation,
        realDeliveryEvidence, workflowState>>

RecordRealDeliveryEvidence ==
    /\ state = "Running"
    /\ session = "Active"
    /\ harnessCases > 0
    /\ realDeliveryEvidence' = TRUE
    /\ UNCHANGED <<state, session, generation, harnessCases, workflowState>>

Pass ==
    /\ state = "Running"
    /\ session = "Active"
    /\ harnessCases > 0
    /\ realDeliveryEvidence
    /\ state' = "Accepted"
    /\ session' = "Suspended"
    /\ UNCHANGED <<generation, harnessCases,
        realDeliveryEvidence, workflowState>>

FindModuleDefect ==
    /\ state = "Running"
    /\ session = "Active"
    /\ harnessCases > 0
    /\ generation < MaxGeneration
    /\ state' = "Repairing"
    /\ session' = "Suspended"
    /\ generation' = generation + 1
    /\ realDeliveryEvidence' = FALSE
    /\ UNCHANGED <<harnessCases, workflowState>>

CompleteWorkflow ==
    /\ state = "Accepted"
    /\ workflowState = "Running"
    /\ workflowState' = "Completed"
    /\ session' = "Completed"
    /\ UNCHANGED <<state, generation, harnessCases, realDeliveryEvidence>>

CancelWorkflow ==
    /\ workflowState = "Running"
    /\ workflowState' = "Cancelled"
    /\ state' = "Cancelled"
    /\ session' = "Cancelled"
    /\ UNCHANGED <<generation, harnessCases, realDeliveryEvidence>>

Next ==
    \/ ModulesAccepted
    \/ StartSystemVerifier
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

=============================================================================
