-------------------------- MODULE ModuleLifecycle --------------------------
EXTENDS Naturals, TLC

CONSTANT MaxCandidate

NodeStates == {
    "CoderReady", "Coding", "VerifierReady", "Verifying",
    "RepairReady", "Accepted", "Cancelled", "Triage"
}
SessionStates == {"Active", "Suspended", "Completed", "Cancelled"}
WorkflowStates == {"Running", "Completed", "Cancelled"}
ActiveRoles == {"None", "Coder", "Verifier"}

VARIABLES
    nodeState,
    coderSession,
    verifierSession,
    activeRole,
    candidateVersion,
    verifiedVersion,
    corpusVersion,
    coderCorpusVersion,
    workflowState,
    managerUp

vars == <<
    nodeState, coderSession, verifierSession, activeRole,
    candidateVersion, verifiedVersion, corpusVersion, coderCorpusVersion,
    workflowState, managerUp
>>

Init ==
    /\ nodeState = "CoderReady"
    /\ coderSession = "Suspended"
    /\ verifierSession = "Suspended"
    /\ activeRole = "None"
    /\ candidateVersion = 0
    /\ verifiedVersion = 0
    /\ corpusVersion = 0
    /\ coderCorpusVersion = 0
    /\ workflowState = "Running"
    /\ managerUp = TRUE

StartCoder ==
    /\ managerUp
    /\ workflowState = "Running"
    /\ nodeState \in {"CoderReady", "RepairReady"}
    /\ activeRole = "None"
    /\ (nodeState = "RepairReady" => coderCorpusVersion = corpusVersion)
    /\ nodeState' = "Coding"
    /\ coderSession' = "Active"
    /\ activeRole' = "Coder"
    /\ UNCHANGED <<verifierSession, candidateVersion, verifiedVersion,
        corpusVersion, coderCorpusVersion, workflowState, managerUp>>

SubmitCandidate ==
    /\ managerUp
    /\ nodeState = "Coding"
    /\ activeRole = "Coder"
    /\ candidateVersion < MaxCandidate
    /\ nodeState' = "VerifierReady"
    /\ coderSession' = "Suspended"
    /\ verifierSession' = "Suspended"
    /\ activeRole' = "None"
    /\ candidateVersion' = candidateVersion + 1
    /\ UNCHANGED <<verifiedVersion, corpusVersion, coderCorpusVersion,
        workflowState, managerUp>>

StartVerifier ==
    /\ managerUp
    /\ workflowState = "Running"
    /\ nodeState = "VerifierReady"
    /\ activeRole = "None"
    /\ nodeState' = "Verifying"
    /\ verifierSession' = "Active"
    /\ activeRole' = "Verifier"
    /\ UNCHANGED <<coderSession, candidateVersion, verifiedVersion,
        corpusVersion, coderCorpusVersion, workflowState, managerUp>>

VerifierPass ==
    /\ managerUp
    /\ nodeState = "Verifying"
    /\ activeRole = "Verifier"
    /\ corpusVersion < MaxCandidate
    /\ nodeState' = "Accepted"
    /\ verifierSession' = "Suspended"
    /\ activeRole' = "None"
    /\ verifiedVersion' = candidateVersion
    /\ corpusVersion' = corpusVersion + 1
    /\ UNCHANGED <<coderSession, candidateVersion, coderCorpusVersion,
        workflowState, managerUp>>

VerifierFail ==
    /\ managerUp
    /\ nodeState = "Verifying"
    /\ activeRole = "Verifier"
    /\ candidateVersion < MaxCandidate
    /\ corpusVersion < MaxCandidate
    /\ nodeState' = "RepairReady"
    /\ verifierSession' = "Suspended"
    /\ activeRole' = "None"
    /\ corpusVersion' = corpusVersion + 1
    /\ coderCorpusVersion' = corpusVersion + 1
    /\ UNCHANGED <<coderSession, candidateVersion, verifiedVersion,
        workflowState, managerUp>>

ReopenAccepted ==
    /\ managerUp
    /\ workflowState = "Running"
    /\ nodeState = "Accepted"
    /\ nodeState' = "VerifierReady"
    /\ verifiedVersion' = candidateVersion - 1
    /\ UNCHANGED <<coderSession, verifierSession, activeRole,
        candidateVersion, corpusVersion, coderCorpusVersion,
        workflowState, managerUp>>

EnterTriage ==
    /\ nodeState \notin {"Cancelled", "Triage"}
    /\ workflowState = "Running"
    /\ nodeState' = "Triage"
    /\ coderSession' =
        IF coderSession = "Active" THEN "Suspended" ELSE coderSession
    /\ verifierSession' =
        IF verifierSession = "Active" THEN "Suspended" ELSE verifierSession
    /\ activeRole' = "None"
    /\ UNCHANGED <<candidateVersion, verifiedVersion, corpusVersion,
        coderCorpusVersion, workflowState, managerUp>>

ResumeTriage ==
    /\ managerUp
    /\ nodeState = "Triage"
    /\ nodeState' = IF candidateVersion = 0 THEN "CoderReady" ELSE "VerifierReady"
    /\ UNCHANGED <<coderSession, verifierSession, activeRole,
        candidateVersion, verifiedVersion, corpusVersion,
        coderCorpusVersion, workflowState, managerUp>>

CompleteWorkflow ==
    /\ managerUp
    /\ workflowState = "Running"
    /\ nodeState = "Accepted"
    /\ workflowState' = "Completed"
    /\ coderSession' = "Completed"
    /\ verifierSession' = "Completed"
    /\ UNCHANGED <<nodeState, activeRole, candidateVersion,
        verifiedVersion, corpusVersion, coderCorpusVersion, managerUp>>

CancelWorkflow ==
    /\ workflowState = "Running"
    /\ workflowState' = "Cancelled"
    /\ nodeState' = "Cancelled"
    /\ coderSession' = "Cancelled"
    /\ verifierSession' = "Cancelled"
    /\ activeRole' = "None"
    /\ UNCHANGED <<candidateVersion, verifiedVersion, corpusVersion,
        coderCorpusVersion, managerUp>>

CrashManager ==
    /\ managerUp
    /\ managerUp' = FALSE
    /\ coderSession' =
        IF coderSession = "Active" THEN "Suspended" ELSE coderSession
    /\ verifierSession' =
        IF verifierSession = "Active" THEN "Suspended" ELSE verifierSession
    /\ activeRole' = "None"
    /\ UNCHANGED <<nodeState, candidateVersion, verifiedVersion,
        corpusVersion, coderCorpusVersion, workflowState>>

RestartManager ==
    /\ ~managerUp
    /\ managerUp' = TRUE
    /\ UNCHANGED <<nodeState, coderSession, verifierSession, activeRole,
        candidateVersion, verifiedVersion, corpusVersion,
        coderCorpusVersion, workflowState>>

Next ==
    \/ StartCoder
    \/ SubmitCandidate
    \/ StartVerifier
    \/ VerifierPass
    \/ VerifierFail
    \/ ReopenAccepted
    \/ EnterTriage
    \/ ResumeTriage
    \/ CompleteWorkflow
    \/ CancelWorkflow
    \/ CrashManager
    \/ RestartManager

Spec == Init /\ [][Next]_vars /\ WF_vars(RestartManager)

TypeOK ==
    /\ nodeState \in NodeStates
    /\ coderSession \in SessionStates
    /\ verifierSession \in SessionStates
    /\ activeRole \in ActiveRoles
    /\ candidateVersion \in 0..MaxCandidate
    /\ verifiedVersion \in 0..MaxCandidate
    /\ corpusVersion \in 0..MaxCandidate
    /\ coderCorpusVersion \in 0..MaxCandidate
    /\ workflowState \in WorkflowStates
    /\ managerUp \in BOOLEAN

SingleRunnableRole ==
    /\ activeRole = "Coder" => coderSession = "Active" /\ verifierSession # "Active"
    /\ activeRole = "Verifier" => verifierSession = "Active" /\ coderSession # "Active"

AcceptedSuspendsButDoesNotComplete ==
    nodeState = "Accepted" /\ workflowState = "Running" =>
        /\ coderSession = "Suspended"
        /\ verifierSession = "Suspended"
        /\ verifiedVersion = candidateVersion

RepairReusesSessions ==
    nodeState = "RepairReady" =>
        /\ coderSession \notin {"Completed", "Cancelled"}
        /\ verifierSession \notin {"Completed", "Cancelled"}
        /\ coderCorpusVersion = corpusVersion

OnlyWorkflowTerminalClosesSessions ==
    workflowState = "Running" =>
        /\ coderSession \notin {"Completed", "Cancelled"}
        /\ verifierSession \notin {"Completed", "Cancelled"}

WorkflowTerminalClosesSessions ==
    workflowState \in {"Completed", "Cancelled"} =>
        /\ coderSession \in {"Completed", "Cancelled"}
        /\ verifierSession \in {"Completed", "Cancelled"}

=============================================================================
