--------------------- MODULE ReplanReuseLifecycle ---------------------
EXTENDS Naturals

CONSTANTS Protocol, Endpoint, Provider, Sidecar, Bridge, Core, Sink

AuthoredModules == {Protocol, Endpoint, Provider, Sidecar, Bridge, Core, Sink}
SourceExecutableModules == {Endpoint, Provider, Bridge, Core, Sink}
TargetExecutableModules == {Endpoint, Sidecar, Bridge, Core, Sink}
ContractOnlyModules == {Protocol}
ExactExecutableModules == {Core}
ChangedPreservedModules == {Endpoint, Sink}
ReplacedModules == {Bridge}
AddedModules == {Sidecar}
DeletedModules == {Provider}
PreservedExecutableModules ==
    ExactExecutableModules \union ChangedPreservedModules

States == {"Absent", "Ready", "Accepted", "Retired"}
Decisions == {
    "Undecided", "ContractChanged", "ReuseAccepted", "ReuseStale",
    "Create", "Retire"
}

VARIABLES phase, nodeState, workspaceIdentity, sessionGeneration,
          corpusIdentity, contractVersion, graphDecision,
          projectionDecision, targetStartState, published

vars == <<phase, nodeState, workspaceIdentity, sessionGeneration,
          corpusIdentity, contractVersion, graphDecision,
          projectionDecision, targetStartState, published>>

Init ==
    /\ phase = "SourceAccepted"
    /\ nodeState = [m \in AuthoredModules |->
        IF m \in SourceExecutableModules THEN "Accepted" ELSE "Absent"]
    /\ workspaceIdentity = [m \in AuthoredModules |->
        IF m \in SourceExecutableModules THEN 1 ELSE 0]
    /\ sessionGeneration = [m \in AuthoredModules |->
        IF m = Sidecar THEN 1
        ELSE IF m \in SourceExecutableModules THEN 1 ELSE 0]
    /\ corpusIdentity = [m \in AuthoredModules |->
        IF m \in SourceExecutableModules THEN 1 ELSE 0]
    /\ contractVersion = [m \in AuthoredModules |-> 1]
    /\ graphDecision = [m \in AuthoredModules |-> "Undecided"]
    /\ projectionDecision = [m \in AuthoredModules |-> "Undecided"]
    /\ targetStartState = [m \in AuthoredModules |-> "Absent"]
    /\ published = FALSE

RequestReplan ==
    /\ phase = "SourceAccepted"
    /\ phase' = "ReplanRequired"
    /\ UNCHANGED <<nodeState, workspaceIdentity, sessionGeneration,
                    corpusIdentity, contractVersion, graphDecision,
                    projectionDecision, targetStartState, published>>

TargetReuseDecision == [m \in AuthoredModules |->
    IF m \in ContractOnlyModules THEN "ContractChanged"
    ELSE IF m \in ExactExecutableModules THEN "ReuseAccepted"
    ELSE IF m \in ChangedPreservedModules THEN "ReuseStale"
    ELSE IF m \in ReplacedModules \union AddedModules THEN "Create"
    ELSE IF m \in DeletedModules THEN "Retire"
    ELSE "Undecided"]

TargetNodeState == [m \in AuthoredModules |->
    IF m \in DeletedModules THEN "Retired"
    ELSE IF m \in ExactExecutableModules THEN "Accepted"
    ELSE IF m \in TargetExecutableModules THEN "Ready"
    ELSE "Absent"]

CompileTarget ==
    /\ phase = "ReplanRequired"
    /\ phase' = "TargetRunning"
    /\ nodeState' = TargetNodeState
    /\ workspaceIdentity' = [m \in AuthoredModules |->
        IF m \in PreservedExecutableModules THEN workspaceIdentity[m]
        ELSE IF m \in AddedModules \union ReplacedModules THEN 2
        ELSE 0]
    /\ sessionGeneration' = [m \in AuthoredModules |->
        IF m \in PreservedExecutableModules THEN sessionGeneration[m]
        ELSE IF m \in AddedModules \union ReplacedModules
            THEN sessionGeneration[m] + 1
        ELSE sessionGeneration[m]]
    /\ corpusIdentity' = [m \in AuthoredModules |->
        IF m \in PreservedExecutableModules THEN corpusIdentity[m]
        ELSE IF m \in AddedModules \union ReplacedModules THEN 2
        ELSE 0]
    /\ contractVersion' = [contractVersion EXCEPT ![Protocol] = 2]
    /\ graphDecision' = TargetReuseDecision
    /\ projectionDecision' = TargetReuseDecision
    /\ targetStartState' = TargetNodeState
    /\ published' = FALSE

AcceptTargetNode(m) ==
    /\ phase = "TargetRunning"
    /\ m \in TargetExecutableModules
    /\ nodeState[m] = "Ready"
    /\ nodeState' = [nodeState EXCEPT ![m] = "Accepted"]
    /\ UNCHANGED <<phase, workspaceIdentity, sessionGeneration,
                    corpusIdentity, contractVersion, graphDecision,
                    projectionDecision, targetStartState, published>>

PublishSink ==
    /\ phase = "TargetRunning"
    /\ \A m \in TargetExecutableModules : nodeState[m] = "Accepted"
    /\ nodeState[Sink] = "Accepted"
    /\ phase' = "Completed"
    /\ published' = TRUE
    /\ UNCHANGED <<nodeState, workspaceIdentity, sessionGeneration,
                    corpusIdentity, contractVersion, graphDecision,
                    projectionDecision, targetStartState>>

Next ==
    \/ RequestReplan
    \/ CompileTarget
    \/ \E m \in TargetExecutableModules : AcceptTargetNode(m)
    \/ PublishSink

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ phase \in {"SourceAccepted", "ReplanRequired", "TargetRunning", "Completed"}
    /\ nodeState \in [AuthoredModules -> States]
    /\ workspaceIdentity \in [AuthoredModules -> 0..2]
    /\ sessionGeneration \in [AuthoredModules -> 0..2]
    /\ corpusIdentity \in [AuthoredModules -> 0..2]
    /\ contractVersion \in [AuthoredModules -> 1..2]
    /\ graphDecision \in [AuthoredModules -> Decisions]
    /\ projectionDecision \in [AuthoredModules -> Decisions]
    /\ targetStartState \in [AuthoredModules -> States]
    /\ published \in BOOLEAN

ProjectionConsumesGraphDecision ==
    phase \in {"TargetRunning", "Completed"} =>
        projectionDecision = graphDecision

ContractOnlyChangeRequeuesConsumers ==
    phase \in {"TargetRunning", "Completed"} =>
        /\ contractVersion[Protocol] = 2
        /\ graphDecision[Protocol] = "ContractChanged"
        /\ \A m \in ChangedPreservedModules :
            /\ graphDecision[m] = "ReuseStale"
            /\ projectionDecision[m] = "ReuseStale"
            /\ targetStartState[m] = "Ready"

PreservedResponsibilityReusesAssets ==
    phase \in {"TargetRunning", "Completed"} =>
        \A m \in PreservedExecutableModules :
            /\ workspaceIdentity[m] = 1
            /\ sessionGeneration[m] = 1
            /\ corpusIdentity[m] = 1

OnlyExactNodesKeepAcceptance ==
    phase \in {"TargetRunning", "Completed"} =>
        /\ \A m \in ExactExecutableModules : targetStartState[m] = "Accepted"
        /\ \A m \in ChangedPreservedModules \union AddedModules \union ReplacedModules :
            targetStartState[m] = "Ready"

DeletedNodesRetire ==
    phase \in {"TargetRunning", "Completed"} =>
        \A m \in DeletedModules : nodeState[m] = "Retired"

ContractOnlyNodesNeverBecomeRunnable ==
    \A m \in ContractOnlyModules : nodeState[m] = "Absent"

ReplacedAndAddedGetFreshIdentity ==
    phase \in {"TargetRunning", "Completed"} =>
        \A m \in ReplacedModules \union AddedModules :
            /\ workspaceIdentity[m] = 2
            /\ sessionGeneration[m] = 2
            /\ corpusIdentity[m] = 2

PublishedOnlyAfterCompleteSink ==
    published =>
        /\ phase = "Completed"
        /\ nodeState[Sink] = "Accepted"
        /\ \A m \in TargetExecutableModules : nodeState[m] = "Accepted"

=======================================================================
