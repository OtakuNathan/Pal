---------------------- MODULE ArchitectureLifecycle ----------------------
EXTENDS Naturals, TLC

CONSTANTS MaxRevision, MaxTaskRevisions

States == {
    "ArchitectQueued", "ArchitectRunning", "ReviewQueued", "Reviewing",
    "HumanReview", "Superseded", "Accepted", "Rejected", "Cancelled", "Triage"
}
SessionStates == {"Active", "Suspended", "Completed", "Cancelled"}
TerminalDecisions == {"Accepted", "Rejected", "Cancelled"}

VARIABLES
    state,
    revision,
    architectSession,
    reviewerSession,
    architectLease,
    reviewerLease,
    decisionIssued,
    taskRevisionCount,
    workflowRevision,
    replanRevision,
    managerUp

vars == <<
    state, revision, architectSession, reviewerSession,
    architectLease, reviewerLease, decisionIssued,
    taskRevisionCount, workflowRevision, replanRevision, managerUp
>>

Init ==
    /\ state = "ArchitectQueued"
    /\ revision = 1
    /\ architectSession = "Suspended"
    /\ reviewerSession = "Suspended"
    /\ architectLease = FALSE
    /\ reviewerLease = FALSE
    /\ decisionIssued = FALSE
    /\ taskRevisionCount = 0
    /\ workflowRevision = 1
    /\ replanRevision = 1
    /\ managerUp = TRUE

StartArchitect ==
    /\ managerUp
    /\ state = "ArchitectQueued"
    /\ state' = "ArchitectRunning"
    /\ architectSession' = "Active"
    /\ architectLease' = TRUE
    /\ UNCHANGED <<revision, reviewerSession, reviewerLease, decisionIssued,
        taskRevisionCount, workflowRevision, replanRevision, managerUp>>

ArchitectSubmitted ==
    /\ managerUp
    /\ state = "ArchitectRunning"
    /\ architectLease
    /\ state' = "ReviewQueued"
    /\ architectSession' = "Suspended"
    /\ architectLease' = FALSE
    /\ UNCHANGED <<revision, reviewerSession, reviewerLease, decisionIssued,
        taskRevisionCount, workflowRevision, replanRevision, managerUp>>

StartReviewer ==
    /\ managerUp
    /\ state = "ReviewQueued"
    /\ state' = "Reviewing"
    /\ reviewerSession' = "Active"
    /\ reviewerLease' = TRUE
    /\ UNCHANGED <<revision, architectSession, architectLease, decisionIssued,
        taskRevisionCount, workflowRevision, replanRevision, managerUp>>

ReviewerFailed ==
    /\ managerUp
    /\ state = "Reviewing"
    /\ reviewerLease
    /\ state' = "ArchitectQueued"
    /\ reviewerSession' = "Suspended"
    /\ reviewerLease' = FALSE
    /\ UNCHANGED <<revision, architectSession, architectLease, decisionIssued,
        taskRevisionCount, workflowRevision, replanRevision, managerUp>>

ReviewerPassed ==
    /\ managerUp
    /\ state = "Reviewing"
    /\ reviewerLease
    /\ state' = "HumanReview"
    /\ reviewerSession' = "Suspended"
    /\ reviewerLease' = FALSE
    /\ decisionIssued' = TRUE
    /\ UNCHANGED <<revision, architectSession, architectLease,
        taskRevisionCount, workflowRevision, replanRevision, managerUp>>

AppendArchitectClarification ==
    /\ managerUp
    /\ state = "ArchitectRunning"
    /\ taskRevisionCount < MaxTaskRevisions
    /\ taskRevisionCount' = taskRevisionCount + 1
    /\ UNCHANGED <<state, revision, architectSession, reviewerSession,
        architectLease, reviewerLease, decisionIssued,
        workflowRevision, replanRevision, managerUp>>

HumanEdit ==
    /\ state = "HumanReview"
    /\ decisionIssued
    /\ revision < MaxRevision
    /\ state' = "Superseded"
    /\ decisionIssued' = FALSE
    /\ UNCHANGED <<revision, architectSession, reviewerSession,
        architectLease, reviewerLease, taskRevisionCount,
        workflowRevision, replanRevision, managerUp>>

CreateChildRevision ==
    /\ managerUp
    /\ state = "Superseded"
    /\ revision < MaxRevision
    /\ state' = "ArchitectQueued"
    /\ revision' = revision + 1
    \* The workflow pointer and the execution-epoch replan pointer move in
    \* the same transaction as child creation. Projection can never select
    \* the superseded parent while the child is active.
    /\ workflowRevision' = revision + 1
    /\ replanRevision' = revision + 1
    /\ UNCHANGED <<architectSession, reviewerSession, architectLease,
        reviewerLease, decisionIssued, taskRevisionCount, managerUp>>

HumanAccept ==
    /\ state = "HumanReview"
    /\ decisionIssued
    /\ state' = "Accepted"
    /\ architectSession' = "Completed"
    /\ reviewerSession' = "Completed"
    /\ decisionIssued' = FALSE
    /\ UNCHANGED <<revision, architectLease, reviewerLease,
        taskRevisionCount, workflowRevision, replanRevision, managerUp>>

HumanReject ==
    /\ state = "HumanReview"
    /\ decisionIssued
    /\ state' = "Rejected"
    /\ architectSession' = "Completed"
    /\ reviewerSession' = "Completed"
    /\ decisionIssued' = FALSE
    /\ UNCHANGED <<revision, architectLease, reviewerLease,
        taskRevisionCount, workflowRevision, replanRevision, managerUp>>

Cancel ==
    /\ state \notin TerminalDecisions
    /\ state' = "Cancelled"
    /\ architectSession' = "Cancelled"
    /\ reviewerSession' = "Cancelled"
    /\ architectLease' = FALSE
    /\ reviewerLease' = FALSE
    /\ decisionIssued' = FALSE
    /\ UNCHANGED <<revision, taskRevisionCount,
        workflowRevision, replanRevision, managerUp>>

EnterTriage ==
    /\ state \notin TerminalDecisions \cup {"Triage", "Superseded"}
    /\ state' = "Triage"
    /\ architectSession' =
        IF architectSession = "Active" THEN "Suspended" ELSE architectSession
    /\ reviewerSession' =
        IF reviewerSession = "Active" THEN "Suspended" ELSE reviewerSession
    /\ architectLease' = FALSE
    /\ reviewerLease' = FALSE
    /\ UNCHANGED <<revision, decisionIssued, taskRevisionCount,
        workflowRevision, replanRevision, managerUp>>

ResolveTriage ==
    /\ managerUp
    /\ state = "Triage"
    /\ state' = "ArchitectQueued"
    /\ UNCHANGED <<revision, architectSession, reviewerSession,
        architectLease, reviewerLease, decisionIssued,
        taskRevisionCount, workflowRevision, replanRevision, managerUp>>

CrashManager ==
    /\ managerUp
    /\ managerUp' = FALSE
    /\ architectLease' = FALSE
    /\ reviewerLease' = FALSE
    /\ architectSession' =
        IF architectSession = "Active" THEN "Suspended" ELSE architectSession
    /\ reviewerSession' =
        IF reviewerSession = "Active" THEN "Suspended" ELSE reviewerSession
    /\ UNCHANGED <<state, revision, decisionIssued, taskRevisionCount,
        workflowRevision, replanRevision>>

RestartManager ==
    /\ ~managerUp
    /\ managerUp' = TRUE
    /\ UNCHANGED <<state, revision, architectSession, reviewerSession,
        architectLease, reviewerLease, decisionIssued, taskRevisionCount,
        workflowRevision, replanRevision>>

Next ==
    \/ StartArchitect
    \/ ArchitectSubmitted
    \/ StartReviewer
    \/ ReviewerFailed
    \/ ReviewerPassed
    \/ AppendArchitectClarification
    \/ HumanEdit
    \/ CreateChildRevision
    \/ HumanAccept
    \/ HumanReject
    \/ Cancel
    \/ EnterTriage
    \/ ResolveTriage
    \/ CrashManager
    \/ RestartManager

Spec == Init /\ [][Next]_vars /\ WF_vars(RestartManager)

TypeOK ==
    /\ state \in States
    /\ revision \in 1..MaxRevision
    /\ architectSession \in SessionStates
    /\ reviewerSession \in SessionStates
    /\ architectLease \in BOOLEAN
    /\ reviewerLease \in BOOLEAN
    /\ decisionIssued \in BOOLEAN
    /\ taskRevisionCount \in 0..MaxTaskRevisions
    /\ workflowRevision \in 1..MaxRevision
    /\ replanRevision \in 1..MaxRevision
    /\ managerUp \in BOOLEAN

LeaseOwnership ==
    /\ architectLease => state = "ArchitectRunning" /\ architectSession = "Active"
    /\ reviewerLease => state = "Reviewing" /\ reviewerSession = "Active"
    /\ ~(architectLease /\ reviewerLease)

EditPreservesLogicalSessions ==
    state = "Superseded" =>
        /\ architectSession \notin {"Completed", "Cancelled"}
        /\ reviewerSession \notin {"Completed", "Cancelled"}

OnlyHumanTerminalClosesSessions ==
    state \in TerminalDecisions =>
        /\ architectSession \in {"Completed", "Cancelled"}
        /\ reviewerSession \in {"Completed", "Cancelled"}

OpenCycleKeepsSessions ==
    state \notin TerminalDecisions =>
        /\ architectSession \notin {"Completed", "Cancelled"}
        /\ reviewerSession \notin {"Completed", "Cancelled"}

RevisionPointersStayAtomic ==
    /\ workflowRevision = replanRevision
    /\ workflowRevision = revision

=============================================================================
