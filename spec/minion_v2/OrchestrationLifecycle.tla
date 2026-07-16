--------------------- MODULE OrchestrationLifecycle ---------------------
EXTENDS Naturals, TLC

CONSTANT Foundation, Drawing, Input, Scenario, MaxGeneration

Nodes == {Foundation, Drawing, Input, Scenario}

Deps == [n \in Nodes |->
    CASE n = Foundation -> {}
      [] n = Drawing -> {Foundation}
      [] n = Input -> {Foundation}
      [] n = Scenario -> {Drawing, Input}]

Affected == [n \in Nodes |->
    CASE n = Foundation -> Nodes
      [] n = Drawing -> {Drawing, Scenario}
      [] n = Input -> {Input, Scenario}
      [] n = Scenario -> {Scenario}]

WorkflowStates == {
    "Active", "PauseRequested", "Paused", "CancelRequested",
    "Completed", "Cancelled", "Triage"
}
EpochStates == {
    "Starting", "Running", "ReplanCollecting", "ReplanRequired",
    "PauseRequested", "Paused", "CancelRequested", "Superseded",
    "Finalizing", "Completed", "Cancelled", "Triage"
}
NodeStates == {
    "Blocked", "Queued", "Active", "Accepted", "Stale",
    "Paused", "Cancelled", "Triage"
}
ControlStates == {"Run", "Pause", "Freeze", "Cancel"}
EpochResumeStates == {"Starting", "Running", "ReplanCollecting", "ReplanRequired", "Finalizing"}
NodeResumeStates == {"Blocked", "Queued"}

VARIABLES
    workflowState,
    epochState,
    nodeState,
    desiredControl,
    epochResume,
    nodeResume,
    defectSource,
    generation,
    managerUp

vars == <<
    workflowState, epochState, nodeState, desiredControl, epochResume,
    nodeResume, defectSource, generation, managerUp
>>

AllDependenciesAccepted(n) ==
    \A dep \in Deps[n] : nodeState[dep] = "Accepted"

SafeNodeResume(n) ==
    IF AllDependenciesAccepted(n) THEN "Queued" ELSE "Blocked"

Init ==
    /\ workflowState = "Active"
    /\ epochState = "Starting"
    /\ nodeState = [n \in Nodes |-> "Blocked"]
    /\ desiredControl = "Run"
    /\ epochResume = "Starting"
    /\ nodeResume = [n \in Nodes |-> IF Deps[n] = {} THEN "Queued" ELSE "Blocked"]
    /\ defectSource = Foundation
    /\ generation = 1
    /\ managerUp = TRUE

CompileEpoch ==
    /\ managerUp
    /\ workflowState = "Active"
    /\ epochState = "Starting"
    /\ desiredControl = "Run"
    /\ epochState' = "Running"
    /\ nodeState' = [n \in Nodes |-> IF Deps[n] = {} THEN "Queued" ELSE "Blocked"]
    /\ UNCHANGED <<workflowState, desiredControl, epochResume, nodeResume,
        defectSource, generation, managerUp>>

RefreshReady(n) ==
    /\ managerUp
    /\ workflowState = "Active"
    /\ epochState = "Running"
    /\ desiredControl = "Run"
    /\ nodeState[n] = "Blocked"
    /\ AllDependenciesAccepted(n)
    /\ nodeState' = [nodeState EXCEPT ![n] = "Queued"]
    /\ UNCHANGED <<workflowState, epochState, desiredControl, epochResume,
        nodeResume, defectSource, generation, managerUp>>

StartNode(n) ==
    /\ managerUp
    /\ workflowState = "Active"
    /\ epochState = "Running"
    /\ desiredControl = "Run"
    /\ nodeState[n] = "Queued"
    /\ AllDependenciesAccepted(n)
    /\ nodeState' = [nodeState EXCEPT ![n] = "Active"]
    /\ UNCHANGED <<workflowState, epochState, desiredControl, epochResume,
        nodeResume, defectSource, generation, managerUp>>

AcceptNode(n) ==
    /\ managerUp
    /\ workflowState = "Active"
    /\ epochState = "Running"
    /\ nodeState[n] = "Active"
    /\ nodeState' = [nodeState EXCEPT ![n] = "Accepted"]
    /\ UNCHANGED <<workflowState, epochState, desiredControl, epochResume,
        nodeResume, defectSource, generation, managerUp>>

BeginFinalization ==
    /\ managerUp
    /\ workflowState = "Active"
    /\ epochState = "Running"
    /\ \A n \in Nodes : nodeState[n] = "Accepted"
    /\ epochState' = "Finalizing"
    /\ UNCHANGED <<workflowState, nodeState, desiredControl, epochResume,
        nodeResume, defectSource, generation, managerUp>>

PublishEpoch ==
    /\ managerUp
    /\ epochState = "Finalizing"
    /\ \A n \in Nodes : nodeState[n] = "Accepted"
    /\ epochState' = "Completed"
    /\ UNCHANGED <<workflowState, nodeState, desiredControl, epochResume,
        nodeResume, defectSource, generation, managerUp>>

CompleteWorkflow ==
    /\ managerUp
    /\ workflowState = "Active"
    /\ epochState = "Completed"
    /\ workflowState' = "Completed"
    /\ UNCHANGED <<epochState, nodeState, desiredControl, epochResume,
        nodeResume, defectSource, generation, managerUp>>

DetectArchitectureDefect(n) ==
    /\ managerUp
    /\ workflowState = "Active"
    /\ epochState \in {"Running", "Finalizing"}
    /\ nodeState[n] \in {"Active", "Accepted"}
    /\ epochState' = "ReplanCollecting"
    /\ desiredControl' = "Freeze"
    /\ defectSource' = n
    /\ nodeState' = [m \in Nodes |->
        IF m \in Affected[n]
        THEN "Stale"
        ELSE IF nodeState[m] = "Accepted" THEN "Accepted" ELSE "Paused"]
    /\ nodeResume' = [m \in Nodes |-> SafeNodeResume(m)]
    /\ UNCHANGED <<workflowState, epochResume, generation, managerUp>>

FinishReplanCollection ==
    /\ managerUp
    /\ epochState = "ReplanCollecting"
    /\ desiredControl = "Freeze"
    /\ \A n \in Nodes : nodeState[n] # "Active"
    /\ epochState' = "ReplanRequired"
    /\ UNCHANGED <<workflowState, nodeState, desiredControl, epochResume,
        nodeResume, defectSource, generation, managerUp>>

ApplyReplan ==
    /\ managerUp
    /\ workflowState = "Active"
    /\ epochState = "ReplanRequired"
    /\ generation < MaxGeneration
    /\ LET reusable == {n \in Nodes : nodeState[n] = "Accepted" /\ n \notin Affected[defectSource]}
       IN nodeState' = [n \in Nodes |->
            IF n \in reusable
            THEN "Accepted"
            ELSE IF Deps[n] \subseteq reusable THEN "Queued" ELSE "Blocked"]
    /\ epochState' = "Running"
    /\ desiredControl' = "Run"
    /\ generation' = generation + 1
    /\ nodeResume' = [n \in Nodes |-> IF Deps[n] = {} THEN "Queued" ELSE "Blocked"]
    /\ UNCHANGED <<workflowState, epochResume, defectSource, managerUp>>

RequestWorkflowPause ==
    /\ workflowState = "Active"
    /\ epochState \in EpochResumeStates
    /\ workflowState' = "PauseRequested"
    /\ epochState' = "PauseRequested"
    /\ desiredControl' = "Pause"
    /\ epochResume' = epochState
    /\ nodeResume' = [n \in Nodes |-> SafeNodeResume(n)]
    /\ UNCHANGED <<nodeState, defectSource, generation, managerUp>>

PauseNode(n) ==
    /\ managerUp
    /\ workflowState = "PauseRequested"
    /\ epochState = "PauseRequested"
    /\ nodeState[n] \notin {"Accepted", "Stale", "Paused", "Cancelled", "Triage"}
    /\ nodeState' = [nodeState EXCEPT ![n] = "Paused"]
    /\ UNCHANGED <<workflowState, epochState, desiredControl, epochResume,
        nodeResume, defectSource, generation, managerUp>>

FinishEpochPause ==
    /\ managerUp
    /\ epochState = "PauseRequested"
    /\ \A n \in Nodes : nodeState[n] \in {"Accepted", "Stale", "Paused", "Cancelled", "Triage"}
    /\ epochState' = "Paused"
    /\ UNCHANGED <<workflowState, nodeState, desiredControl, epochResume,
        nodeResume, defectSource, generation, managerUp>>

FinishWorkflowPause ==
    /\ managerUp
    /\ workflowState = "PauseRequested"
    /\ epochState = "Paused"
    /\ workflowState' = "Paused"
    /\ UNCHANGED <<epochState, nodeState, desiredControl, epochResume,
        nodeResume, defectSource, generation, managerUp>>

ResumeWorkflow ==
    /\ managerUp
    /\ workflowState = "Paused"
    /\ epochState = "Paused"
    /\ workflowState' = "Active"
    /\ epochState' = epochResume
    /\ desiredControl' =
        IF epochResume \in {"ReplanCollecting", "ReplanRequired"}
        THEN "Freeze"
        ELSE "Run"
    /\ nodeState' = [n \in Nodes |->
        IF nodeState[n] = "Paused" THEN nodeResume[n] ELSE nodeState[n]]
    /\ UNCHANGED <<epochResume, nodeResume, defectSource, generation, managerUp>>

RequestWorkflowCancel ==
    /\ workflowState \notin {"Completed", "Cancelled", "CancelRequested"}
    /\ epochState \notin {"Completed", "Cancelled", "Superseded"}
    /\ workflowState' = "CancelRequested"
    /\ epochState' = "CancelRequested"
    /\ desiredControl' = "Cancel"
    /\ UNCHANGED <<nodeState, epochResume, nodeResume, defectSource,
        generation, managerUp>>

CancelNode(n) ==
    /\ managerUp
    /\ workflowState = "CancelRequested"
    /\ epochState = "CancelRequested"
    /\ nodeState[n] \notin {"Accepted", "Cancelled"}
    /\ nodeState' = [nodeState EXCEPT ![n] = "Cancelled"]
    /\ UNCHANGED <<workflowState, epochState, desiredControl, epochResume,
        nodeResume, defectSource, generation, managerUp>>

FinishEpochCancel ==
    /\ managerUp
    /\ epochState = "CancelRequested"
    /\ \A n \in Nodes : nodeState[n] \in {"Accepted", "Cancelled"}
    /\ epochState' = "Cancelled"
    /\ UNCHANGED <<workflowState, nodeState, desiredControl, epochResume,
        nodeResume, defectSource, generation, managerUp>>

FinishWorkflowCancel ==
    /\ managerUp
    /\ workflowState = "CancelRequested"
    /\ epochState = "Cancelled"
    /\ workflowState' = "Cancelled"
    /\ UNCHANGED <<epochState, nodeState, desiredControl, epochResume,
        nodeResume, defectSource, generation, managerUp>>

EnterEpochTriage ==
    /\ epochState \in EpochResumeStates
    /\ workflowState = "Active"
    /\ epochState' = "Triage"
    /\ epochResume' = epochState
    /\ nodeResume' = [n \in Nodes |-> SafeNodeResume(n)]
    /\ nodeState' = [n \in Nodes |->
        IF nodeState[n] = "Active" THEN "Paused" ELSE nodeState[n]]
    /\ UNCHANGED <<workflowState, desiredControl, defectSource,
        generation, managerUp>>

ResolveEpochTriage ==
    /\ managerUp
    /\ workflowState = "Active"
    /\ epochState = "Triage"
    /\ epochState' = epochResume
    /\ nodeState' = [n \in Nodes |->
        IF nodeState[n] = "Paused" THEN nodeResume[n] ELSE nodeState[n]]
    /\ UNCHANGED <<workflowState, desiredControl, epochResume, nodeResume,
        defectSource, generation, managerUp>>

EnterWorkflowTriage ==
    /\ workflowState = "Active"
    /\ epochState \in EpochResumeStates
    /\ workflowState' = "Triage"
    /\ epochState' = "Triage"
    /\ epochResume' = epochState
    /\ nodeResume' = [n \in Nodes |-> SafeNodeResume(n)]
    /\ nodeState' = [n \in Nodes |->
        IF nodeState[n] = "Active" THEN "Paused" ELSE nodeState[n]]
    /\ UNCHANGED <<desiredControl, defectSource, generation, managerUp>>

ResolveWorkflowTriage ==
    /\ managerUp
    /\ workflowState = "Triage"
    /\ epochState = "Triage"
    /\ workflowState' = "Active"
    /\ epochState' = epochResume
    /\ nodeState' = [n \in Nodes |->
        IF nodeState[n] = "Paused" THEN nodeResume[n] ELSE nodeState[n]]
    /\ UNCHANGED <<desiredControl, epochResume, nodeResume, defectSource,
        generation, managerUp>>

CrashManager ==
    /\ managerUp
    /\ managerUp' = FALSE
    /\ UNCHANGED <<workflowState, epochState, nodeState, desiredControl,
        epochResume, nodeResume, defectSource, generation>>

RestartManager ==
    /\ ~managerUp
    /\ managerUp' = TRUE
    /\ UNCHANGED <<workflowState, epochState, nodeState, desiredControl,
        epochResume, nodeResume, defectSource, generation>>

RefreshSomeReady == \E n \in Nodes : RefreshReady(n)
StartSomeNode == \E n \in Nodes : StartNode(n)
AcceptSomeNode == \E n \in Nodes : AcceptNode(n)
PauseSomeNode == \E n \in Nodes : PauseNode(n)
CancelSomeNode == \E n \in Nodes : CancelNode(n)

Next ==
    \/ CompileEpoch
    \/ RefreshSomeReady
    \/ StartSomeNode
    \/ AcceptSomeNode
    \/ BeginFinalization
    \/ PublishEpoch
    \/ CompleteWorkflow
    \/ \E n \in Nodes : DetectArchitectureDefect(n)
    \/ FinishReplanCollection
    \/ ApplyReplan
    \/ RequestWorkflowPause
    \/ PauseSomeNode
    \/ FinishEpochPause
    \/ FinishWorkflowPause
    \/ ResumeWorkflow
    \/ RequestWorkflowCancel
    \/ CancelSomeNode
    \/ FinishEpochCancel
    \/ FinishWorkflowCancel
    \/ EnterEpochTriage
    \/ ResolveEpochTriage
    \/ EnterWorkflowTriage
    \/ ResolveWorkflowTriage
    \/ CrashManager
    \/ RestartManager

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(RestartManager)
    /\ SF_vars(PauseSomeNode)
    /\ SF_vars(FinishEpochPause)
    /\ SF_vars(FinishWorkflowPause)
    /\ SF_vars(CancelSomeNode)
    /\ SF_vars(FinishEpochCancel)
    /\ SF_vars(FinishWorkflowCancel)
    /\ SF_vars(ResolveEpochTriage)
    /\ SF_vars(ResolveWorkflowTriage)

TypeOK ==
    /\ workflowState \in WorkflowStates
    /\ epochState \in EpochStates
    /\ nodeState \in [Nodes -> NodeStates]
    /\ desiredControl \in ControlStates
    /\ epochResume \in EpochResumeStates
    /\ nodeResume \in [Nodes -> NodeResumeStates]
    /\ defectSource \in Nodes
    /\ generation \in 1..MaxGeneration
    /\ managerUp \in BOOLEAN

DependencySafety ==
    \A n \in Nodes : nodeState[n] = "Active" =>
        /\ AllDependenciesAccepted(n)
        /\ workflowState \in {"Active", "PauseRequested", "CancelRequested"}
        /\ epochState \in {"Running", "PauseRequested", "CancelRequested"}

CompletionSafety ==
    /\ epochState = "Completed" => \A n \in Nodes : nodeState[n] = "Accepted"
    /\ workflowState = "Completed" => epochState = "Completed"

PauseControlAlignment ==
    workflowState = "Paused" => epochState = "Paused" /\ desiredControl = "Pause"

CancelControlAlignment ==
    workflowState = "Cancelled" => epochState = "Cancelled" /\ desiredControl = "Cancel"

ReplanBlocksNewWork ==
    epochState \in {"ReplanCollecting", "ReplanRequired"} =>
        /\ desiredControl = "Freeze"
        /\ \A n \in Affected[defectSource] : nodeState[n] = "Stale"

NodeHasExplicitLiveness(n) ==
    \/ nodeState[n] \in {"Queued", "Active"}
    \/ nodeState[n] = "Blocked"
    \/ nodeState[n] \in {"Accepted", "Stale", "Paused", "Cancelled", "Triage"}

NonterminalWorkflowHasLiveness ==
    workflowState \notin {"Completed", "Cancelled"} =>
        /\ workflowState \in {"Active", "PauseRequested", "Paused", "CancelRequested", "Triage"}
        /\ \A n \in Nodes : NodeHasExplicitLiveness(n)

PauseEventuallySettles ==
    workflowState = "PauseRequested" ~>
        workflowState \in {"Paused", "CancelRequested", "Cancelled", "Triage"}

CancelEventuallySettles ==
    workflowState = "CancelRequested" ~> workflowState = "Cancelled"

=============================================================================
