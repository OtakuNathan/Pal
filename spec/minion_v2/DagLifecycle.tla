---------------------------- MODULE DagLifecycle ----------------------------
EXTENDS FiniteSets, Naturals, TLC

CONSTANTS Root, Leaf, MaxGeneration

Nodes == {Root, Leaf}
Deps == [n \in Nodes |-> IF n = Root THEN {} ELSE {Root}]
Affected == [n \in Nodes |-> IF n = Root THEN Nodes ELSE {Leaf}]

DagStates == {
    "Running", "PauseRequested", "Paused", "FreezeRequested",
    "ReplanRequired", "CancelRequested", "Completed", "Cancelled", "Triage"
}

NodeStates == {"Blocked", "Ready", "Active", "Accepted", "Paused", "Frozen", "Cancelled", "Triage"}
ControlStates == {"Run", "Pause", "Freeze", "Cancel"}
ResumeStates == {"Blocked", "Ready"}

VARIABLES
    dagState,
    nodeState,
    desiredControl,
    nodeResume,
    defectSource,
    generation,
    managerUp

vars == <<dagState, nodeState, desiredControl, nodeResume, defectSource, generation, managerUp>>

AllDependenciesAccepted(n) == \A dependency \in Deps[n] : nodeState[dependency] = "Accepted"

InitialNodeState(n) == IF Deps[n] = {} THEN "Ready" ELSE "Blocked"

RetryNodeState(n) == IF AllDependenciesAccepted(n) THEN "Ready" ELSE "Blocked"

Init ==
    /\ dagState = "Running"
    /\ nodeState = [n \in Nodes |-> InitialNodeState(n)]
    /\ desiredControl = "Run"
    /\ nodeResume = [n \in Nodes |-> InitialNodeState(n)]
    /\ defectSource = "None"
    /\ generation = 1
    /\ managerUp = TRUE

RefreshReady(n) ==
    /\ managerUp
    /\ dagState = "Running"
    /\ desiredControl = "Run"
    /\ nodeState[n] = "Blocked"
    /\ AllDependenciesAccepted(n)
    /\ nodeState' = [nodeState EXCEPT ![n] = "Ready"]
    /\ UNCHANGED <<dagState, desiredControl, nodeResume, defectSource, generation, managerUp>>

StartNode(n) ==
    /\ managerUp
    /\ dagState = "Running"
    /\ desiredControl = "Run"
    /\ nodeState[n] = "Ready"
    /\ AllDependenciesAccepted(n)
    /\ nodeState' = [nodeState EXCEPT ![n] = "Active"]
    /\ UNCHANGED <<dagState, desiredControl, nodeResume, defectSource, generation, managerUp>>

AcceptNode(n) ==
    /\ managerUp
    /\ dagState = "Running"
    /\ desiredControl = "Run"
    /\ nodeState[n] = "Active"
    /\ AllDependenciesAccepted(n)
    /\ nodeState' = [nodeState EXCEPT ![n] = "Accepted"]
    /\ UNCHANGED <<dagState, desiredControl, nodeResume, defectSource, generation, managerUp>>

CompleteDag ==
    /\ managerUp
    /\ dagState = "Running"
    /\ \A n \in Nodes : nodeState[n] = "Accepted"
    /\ dagState' = "Completed"
    /\ UNCHANGED <<nodeState, desiredControl, nodeResume, defectSource, generation, managerUp>>

DetectArchitectureDefect(n) ==
    /\ managerUp
    /\ dagState = "Running"
    /\ nodeState[n] \in {"Active", "Accepted"}
    /\ dagState' = "FreezeRequested"
    /\ desiredControl' = "Freeze"
    /\ defectSource' = n
    /\ UNCHANGED <<nodeState, nodeResume, generation, managerUp>>

NeedsFreeze(n) ==
    /\ nodeState[n] \notin {"Frozen", "Cancelled"}
    /\ (nodeState[n] # "Accepted" \/ n \in Affected[defectSource])

FreezeNode(n) ==
    /\ managerUp
    /\ dagState = "FreezeRequested"
    /\ desiredControl = "Freeze"
    /\ defectSource \in Nodes
    /\ NeedsFreeze(n)
    /\ nodeState' = [nodeState EXCEPT ![n] = "Frozen"]
    /\ UNCHANGED <<dagState, desiredControl, nodeResume, defectSource, generation, managerUp>>

FinishFreeze ==
    /\ managerUp
    /\ dagState = "FreezeRequested"
    /\ defectSource \in Nodes
    /\ \A n \in Nodes :
        \/ nodeState[n] = "Frozen"
        \/ (nodeState[n] = "Accepted" /\ n \notin Affected[defectSource])
    /\ dagState' = "ReplanRequired"
    /\ UNCHANGED <<nodeState, desiredControl, nodeResume, defectSource, generation, managerUp>>

ApplyReplan ==
    /\ managerUp
    /\ dagState = "ReplanRequired"
    /\ defectSource \in Nodes
    /\ generation < MaxGeneration
    /\ LET reusable == {n \in Nodes : nodeState[n] = "Accepted" /\ n \notin Affected[defectSource]}
       IN nodeState' = [n \in Nodes |->
            IF n \in reusable
            THEN "Accepted"
            ELSE IF Deps[n] \subseteq reusable THEN "Ready" ELSE "Blocked"]
    /\ dagState' = "Running"
    /\ desiredControl' = "Run"
    /\ defectSource' = "None"
    /\ generation' = generation + 1
    /\ nodeResume' = [n \in Nodes |-> IF Deps[n] = {} THEN "Ready" ELSE "Blocked"]
    /\ UNCHANGED managerUp

RequestPause ==
    /\ dagState = "Running"
    /\ desiredControl = "Run"
    /\ dagState' = "PauseRequested"
    /\ desiredControl' = "Pause"
    /\ nodeResume' = [n \in Nodes |->
        IF nodeState[n] = "Blocked" THEN "Blocked"
        ELSE IF nodeState[n] = "Accepted" THEN "Blocked"
        ELSE RetryNodeState(n)]
    /\ UNCHANGED <<nodeState, defectSource, generation, managerUp>>

PauseNode(n) ==
    /\ managerUp
    /\ dagState = "PauseRequested"
    /\ desiredControl = "Pause"
    /\ nodeState[n] \notin {"Accepted", "Paused", "Cancelled"}
    /\ nodeState' = [nodeState EXCEPT ![n] = "Paused"]
    /\ UNCHANGED <<dagState, desiredControl, nodeResume, defectSource, generation, managerUp>>

FinishPause ==
    /\ managerUp
    /\ dagState = "PauseRequested"
    /\ \A n \in Nodes : nodeState[n] \in {"Accepted", "Paused", "Cancelled"}
    /\ dagState' = "Paused"
    /\ UNCHANGED <<nodeState, desiredControl, nodeResume, defectSource, generation, managerUp>>

ResumeDag ==
    /\ managerUp
    /\ dagState = "Paused"
    /\ desiredControl = "Pause"
    /\ nodeState' = [n \in Nodes |-> IF nodeState[n] = "Paused" THEN nodeResume[n] ELSE nodeState[n]]
    /\ dagState' = "Running"
    /\ desiredControl' = "Run"
    /\ UNCHANGED <<nodeResume, defectSource, generation, managerUp>>

RequestCancel ==
    /\ dagState \notin {"Completed", "Cancelled", "CancelRequested"}
    /\ dagState' = "CancelRequested"
    /\ desiredControl' = "Cancel"
    /\ UNCHANGED <<nodeState, nodeResume, defectSource, generation, managerUp>>

CancelNode(n) ==
    /\ managerUp
    /\ dagState = "CancelRequested"
    /\ nodeState[n] \notin {"Accepted", "Cancelled"}
    /\ nodeState' = [nodeState EXCEPT ![n] = "Cancelled"]
    /\ UNCHANGED <<dagState, desiredControl, nodeResume, defectSource, generation, managerUp>>

FinishCancel ==
    /\ managerUp
    /\ dagState = "CancelRequested"
    /\ \A n \in Nodes : nodeState[n] \in {"Accepted", "Cancelled"}
    /\ dagState' = "Cancelled"
    /\ UNCHANGED <<nodeState, desiredControl, nodeResume, defectSource, generation, managerUp>>

CrashManager ==
    /\ managerUp
    /\ managerUp' = FALSE
    /\ UNCHANGED <<dagState, nodeState, desiredControl, nodeResume, defectSource, generation>>

RestartManager ==
    /\ ~managerUp
    /\ managerUp' = TRUE
    /\ UNCHANGED <<dagState, nodeState, desiredControl, nodeResume, defectSource, generation>>

FreezeSomeNode == \E n \in Nodes : FreezeNode(n)
PauseSomeNode == \E n \in Nodes : PauseNode(n)
CancelSomeNode == \E n \in Nodes : CancelNode(n)

Next ==
    \/ \E n \in Nodes : RefreshReady(n)
    \/ \E n \in Nodes : StartNode(n)
    \/ \E n \in Nodes : AcceptNode(n)
    \/ CompleteDag
    \/ \E n \in Nodes : DetectArchitectureDefect(n)
    \/ FreezeSomeNode
    \/ FinishFreeze
    \/ ApplyReplan
    \/ RequestPause
    \/ PauseSomeNode
    \/ FinishPause
    \/ ResumeDag
    \/ RequestCancel
    \/ CancelSomeNode
    \/ FinishCancel
    \/ CrashManager
    \/ RestartManager

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(RestartManager)
    /\ SF_vars(FreezeSomeNode)
    /\ SF_vars(FinishFreeze)
    /\ SF_vars(PauseSomeNode)
    /\ WF_vars(FinishPause)
    /\ SF_vars(CancelSomeNode)
    /\ SF_vars(FinishCancel)

TypeOK ==
    /\ dagState \in DagStates
    /\ nodeState \in [Nodes -> NodeStates]
    /\ desiredControl \in ControlStates
    /\ nodeResume \in [Nodes -> ResumeStates]
    /\ defectSource \in Nodes \cup {"None"}
    /\ generation \in 1..MaxGeneration
    /\ managerUp \in BOOLEAN

DependencySafety ==
    dagState = "Running" =>
        \A n \in Nodes :
            nodeState[n] \in {"Active", "Accepted"} => AllDependenciesAccepted(n)

CompletedMeansAllAccepted ==
    dagState = "Completed" => \A n \in Nodes : nodeState[n] = "Accepted"

FreezeBlocksNewWork ==
    dagState \in {"FreezeRequested", "ReplanRequired"} => desiredControl = "Freeze"

ReplanWaitsForAffectedNodes ==
    dagState = "ReplanRequired" =>
        /\ defectSource \in Nodes
        /\ \A n \in Affected[defectSource] : nodeState[n] = "Frozen"

CancelledHasNoActiveNode ==
    dagState = "Cancelled" => \A n \in Nodes : nodeState[n] \in {"Accepted", "Cancelled"}

FreezeEventuallySettles ==
    dagState = "FreezeRequested" ~> dagState \in {"ReplanRequired", "Cancelled"}

CancelEventuallySettles ==
    dagState = "CancelRequested" ~> dagState = "Cancelled"

=============================================================================
