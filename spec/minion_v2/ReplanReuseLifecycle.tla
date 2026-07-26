--------------------- MODULE ReplanReuseLifecycle ---------------------
EXTENDS Naturals, TLC

\* A workflow owns stable Module identities.  Execution epochs are audit and
\* scheduling projections only: replanning classifies old/new architecture map
\* keys as preserve, create, or delete.  It never replaces a surviving
\* Module's worktree or logical Coder/Verifier coroutine.

CONSTANT
    Protocol,
    Endpoint,
    Provider,
    Sidecar,
    MaxCandidate,
    MaxAdmissions

Modules == {Protocol, Endpoint, Provider, Sidecar}
SourceModules == {Protocol, Endpoint, Provider}
TargetModules == {Protocol, Endpoint, Sidecar}
PreservedModules == SourceModules \intersect TargetModules
AddedModules == TargetModules \ SourceModules
DeletedModules == SourceModules \ TargetModules

\* Protocol's complete effective fingerprint is unchanged. Endpoint remains
\* the same Module but its contract changed, so its assets survive and the
\* same logical actors receive another assignment. Sidecar models a historical
\* identity that was deleted before this source plan and is now re-added.
ExactModules == {Protocol}
ChangedPreservedModules == PreservedModules \ ExactModules

WorkflowStates == {"Running", "Completed"}
Phases == {
    "SourceRunning",
    "ReplanRequired",
    "TargetRunning",
    "SystemVerifying",
    "Completed"
}
ModuleStates == {
    "Absent",
    "Ready",
    "Coding",
    "VerifierReady",
    "Verifying",
    "Accepted",
    "Retired"
}
SessionStates == {"Absent", "Suspended", "Active", "Retired", "Completed"}
ActiveRoles == {"None", "Coder", "Verifier"}
SystemStates == {"Blocked", "Active", "Accepted"}

VARIABLES
    workflowState,
    phase,
    moduleState,
    worktreeIdentity,
    roleGeneration,
    candidate,
    corpus,
    coderSession,
    verifierSession,
    activeRole,
    admissionCount,
    systemState,
    managerUp

vars == <<
    workflowState,
    phase,
    moduleState,
    worktreeIdentity,
    roleGeneration,
    candidate,
    corpus,
    coderSession,
    verifierSession,
    activeRole,
    admissionCount,
    systemState,
    managerUp
>>

AllTargetModulesAccepted ==
    \A m \in TargetModules : moduleState[m] = "Accepted"

Init ==
    /\ workflowState = "Running"
    /\ phase = "SourceRunning"
    /\ moduleState = [m \in Modules |->
        IF m \in SourceModules THEN "Accepted" ELSE "Absent"]
    /\ worktreeIdentity = [m \in Modules |->
        IF m \in SourceModules THEN 1 ELSE 0]
    \* Sidecar has one retired historical identity; re-adding it must allocate
    \* generation two instead of resurrecting that coroutine.
    /\ roleGeneration = [m \in Modules |->
        IF m = Sidecar THEN 1 ELSE IF m \in SourceModules THEN 1 ELSE 0]
    /\ candidate = [m \in Modules |->
        IF m \in SourceModules THEN 1 ELSE 0]
    /\ corpus = [m \in Modules |->
        IF m \in SourceModules THEN 1 ELSE 0]
    /\ coderSession = [m \in Modules |->
        IF m \in SourceModules THEN "Suspended"
        ELSE IF m = Sidecar THEN "Retired"
        ELSE "Absent"]
    /\ verifierSession = [m \in Modules |->
        IF m \in SourceModules THEN "Suspended"
        ELSE IF m = Sidecar THEN "Retired"
        ELSE "Absent"]
    /\ activeRole = [m \in Modules |-> "None"]
    /\ admissionCount = [m \in Modules |-> 0]
    /\ systemState = "Blocked"
    /\ managerUp = TRUE

DetectArchitectureDefect ==
    /\ managerUp
    /\ workflowState = "Running"
    /\ phase = "SourceRunning"
    /\ phase' = "ReplanRequired"
    /\ UNCHANGED <<workflowState, moduleState, worktreeIdentity,
        roleGeneration, candidate, corpus, coderSession, verifierSession,
        activeRole, admissionCount, systemState, managerUp>>

CompileTargetPlan ==
    /\ managerUp
    /\ workflowState = "Running"
    /\ phase = "ReplanRequired"
    /\ phase' = "TargetRunning"
    /\ moduleState' = [m \in Modules |->
        IF m \in DeletedModules THEN "Retired"
        ELSE IF m \in ExactModules THEN "Accepted"
        ELSE IF m \in TargetModules THEN "Ready"
        ELSE "Absent"]
    /\ worktreeIdentity' = [m \in Modules |->
        IF m \in PreservedModules THEN worktreeIdentity[m]
        ELSE IF m \in AddedModules THEN 2
        ELSE 0]
    /\ roleGeneration' = [m \in Modules |->
        IF m \in PreservedModules THEN roleGeneration[m]
        ELSE IF m \in AddedModules THEN roleGeneration[m] + 1
        ELSE roleGeneration[m]]
    /\ candidate' = [m \in Modules |->
        IF m \in PreservedModules THEN candidate[m]
        ELSE 0]
    /\ corpus' = [m \in Modules |->
        IF m \in PreservedModules THEN corpus[m]
        ELSE 0]
    /\ coderSession' = [m \in Modules |->
        IF m \in DeletedModules THEN "Retired"
        ELSE IF m \in PreservedModules THEN coderSession[m]
        ELSE IF m \in AddedModules THEN "Suspended"
        ELSE "Absent"]
    /\ verifierSession' = [m \in Modules |->
        IF m \in DeletedModules THEN "Retired"
        ELSE IF m \in PreservedModules THEN verifierSession[m]
        ELSE IF m \in AddedModules THEN "Suspended"
        ELSE "Absent"]
    /\ UNCHANGED <<workflowState, activeRole, admissionCount,
        systemState, managerUp>>

StartCoder(m) ==
    /\ managerUp
    /\ workflowState = "Running"
    /\ phase = "TargetRunning"
    /\ m \in TargetModules \ ExactModules
    /\ moduleState[m] = "Ready"
    /\ activeRole[m] = "None"
    /\ coderSession[m] = "Suspended"
    /\ admissionCount[m] < MaxAdmissions
    /\ moduleState' = [moduleState EXCEPT ![m] = "Coding"]
    /\ coderSession' = [coderSession EXCEPT ![m] = "Active"]
    /\ activeRole' = [activeRole EXCEPT ![m] = "Coder"]
    /\ admissionCount' = [admissionCount EXCEPT ![m] = @ + 1]
    /\ UNCHANGED <<workflowState, phase, worktreeIdentity, roleGeneration,
        candidate, corpus, verifierSession, systemState, managerUp>>

SubmitCandidate(m) ==
    /\ managerUp
    /\ moduleState[m] = "Coding"
    /\ activeRole[m] = "Coder"
    /\ coderSession[m] = "Active"
    /\ candidate[m] < MaxCandidate
    /\ moduleState' = [moduleState EXCEPT ![m] = "VerifierReady"]
    /\ candidate' = [candidate EXCEPT ![m] = @ + 1]
    /\ coderSession' = [coderSession EXCEPT ![m] = "Suspended"]
    /\ activeRole' = [activeRole EXCEPT ![m] = "None"]
    /\ UNCHANGED <<workflowState, phase, worktreeIdentity, roleGeneration,
        corpus, verifierSession, admissionCount, systemState, managerUp>>

StartVerifier(m) ==
    /\ managerUp
    /\ moduleState[m] = "VerifierReady"
    /\ activeRole[m] = "None"
    /\ verifierSession[m] = "Suspended"
    /\ moduleState' = [moduleState EXCEPT ![m] = "Verifying"]
    /\ verifierSession' = [verifierSession EXCEPT ![m] = "Active"]
    /\ activeRole' = [activeRole EXCEPT ![m] = "Verifier"]
    /\ UNCHANGED <<workflowState, phase, worktreeIdentity, roleGeneration,
        candidate, corpus, coderSession, admissionCount, systemState, managerUp>>

VerifierPass(m) ==
    /\ managerUp
    /\ moduleState[m] = "Verifying"
    /\ activeRole[m] = "Verifier"
    /\ verifierSession[m] = "Active"
    /\ moduleState' = [moduleState EXCEPT ![m] = "Accepted"]
    /\ corpus' = [corpus EXCEPT ![m] = IF @ < 2 THEN @ + 1 ELSE @]
    /\ verifierSession' = [verifierSession EXCEPT ![m] = "Suspended"]
    /\ activeRole' = [activeRole EXCEPT ![m] = "None"]
    /\ UNCHANGED <<workflowState, phase, worktreeIdentity, roleGeneration,
        candidate, coderSession, admissionCount, systemState, managerUp>>

VerifierFail(m) ==
    /\ managerUp
    /\ moduleState[m] = "Verifying"
    /\ activeRole[m] = "Verifier"
    /\ verifierSession[m] = "Active"
    /\ admissionCount[m] < MaxAdmissions
    /\ moduleState' = [moduleState EXCEPT ![m] = "Ready"]
    /\ corpus' = [corpus EXCEPT ![m] = IF @ < 2 THEN @ + 1 ELSE @]
    /\ verifierSession' = [verifierSession EXCEPT ![m] = "Suspended"]
    /\ activeRole' = [activeRole EXCEPT ![m] = "None"]
    /\ UNCHANGED <<workflowState, phase, worktreeIdentity, roleGeneration,
        candidate, coderSession, admissionCount, systemState, managerUp>>

StartSystemVerification ==
    /\ managerUp
    /\ phase = "TargetRunning"
    /\ AllTargetModulesAccepted
    /\ systemState = "Blocked"
    /\ phase' = "SystemVerifying"
    /\ systemState' = "Active"
    /\ UNCHANGED <<workflowState, moduleState, worktreeIdentity,
        roleGeneration, candidate, corpus, coderSession, verifierSession,
        activeRole, admissionCount, managerUp>>

PassSystemVerification ==
    /\ managerUp
    /\ phase = "SystemVerifying"
    /\ systemState = "Active"
    /\ systemState' = "Accepted"
    /\ UNCHANGED <<workflowState, phase, moduleState, worktreeIdentity,
        roleGeneration, candidate, corpus, coderSession, verifierSession,
        activeRole, admissionCount, managerUp>>

CompleteWorkflow ==
    /\ managerUp
    /\ phase = "SystemVerifying"
    /\ systemState = "Accepted"
    /\ workflowState' = "Completed"
    /\ phase' = "Completed"
    /\ coderSession' = [m \in Modules |->
        IF m \in TargetModules THEN "Completed" ELSE coderSession[m]]
    /\ verifierSession' = [m \in Modules |->
        IF m \in TargetModules THEN "Completed" ELSE verifierSession[m]]
    /\ UNCHANGED <<moduleState, worktreeIdentity, roleGeneration,
        candidate, corpus, activeRole, admissionCount, systemState, managerUp>>

CrashManager ==
    /\ managerUp
    /\ managerUp' = FALSE
    /\ moduleState' = [m \in Modules |->
        IF moduleState[m] = "Coding" THEN "Ready"
        ELSE IF moduleState[m] = "Verifying" THEN "VerifierReady"
        ELSE moduleState[m]]
    /\ coderSession' = [m \in Modules |->
        IF coderSession[m] = "Active" THEN "Suspended" ELSE coderSession[m]]
    /\ verifierSession' = [m \in Modules |->
        IF verifierSession[m] = "Active" THEN "Suspended" ELSE verifierSession[m]]
    /\ activeRole' = [m \in Modules |-> "None"]
    /\ UNCHANGED <<workflowState, phase, worktreeIdentity, roleGeneration,
        candidate, corpus, admissionCount, systemState>>

RestartManager ==
    /\ ~managerUp
    /\ managerUp' = TRUE
    /\ UNCHANGED <<workflowState, phase, moduleState, worktreeIdentity,
        roleGeneration, candidate, corpus, coderSession, verifierSession,
        activeRole, admissionCount, systemState>>

Next ==
    \/ DetectArchitectureDefect
    \/ CompileTargetPlan
    \/ \E m \in Modules : StartCoder(m)
    \/ \E m \in Modules : SubmitCandidate(m)
    \/ \E m \in Modules : StartVerifier(m)
    \/ \E m \in Modules : VerifierPass(m)
    \/ \E m \in Modules : VerifierFail(m)
    \/ StartSystemVerification
    \/ PassSystemVerification
    \/ CompleteWorkflow
    \/ CrashManager
    \/ RestartManager

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(RestartManager)
    /\ SF_vars(CompileTargetPlan)

TypeOK ==
    /\ workflowState \in WorkflowStates
    /\ phase \in Phases
    /\ moduleState \in [Modules -> ModuleStates]
    /\ worktreeIdentity \in [Modules -> 0..2]
    /\ roleGeneration \in [Modules -> 0..2]
    /\ candidate \in [Modules -> 0..MaxCandidate]
    /\ corpus \in [Modules -> 0..2]
    /\ coderSession \in [Modules -> SessionStates]
    /\ verifierSession \in [Modules -> SessionStates]
    /\ activeRole \in [Modules -> ActiveRoles]
    /\ admissionCount \in [Modules -> 0..MaxAdmissions]
    /\ systemState \in SystemStates
    /\ managerUp \in BOOLEAN

ModuleIdentityClassification ==
    phase \in {"TargetRunning", "SystemVerifying", "Completed"} =>
        /\ \A m \in PreservedModules :
            /\ worktreeIdentity[m] = 1
            /\ roleGeneration[m] = 1
        /\ \A m \in AddedModules :
            /\ worktreeIdentity[m] = 2
            /\ roleGeneration[m] = 2
        /\ \A m \in DeletedModules :
            /\ worktreeIdentity[m] = 0
            /\ moduleState[m] = "Retired"
            /\ coderSession[m] = "Retired"
            /\ verifierSession[m] = "Retired"

ExactCarryForwardSkipsActors ==
    phase \in {"TargetRunning", "SystemVerifying", "Completed"} =>
        \A m \in ExactModules :
            /\ moduleState[m] = "Accepted"
            /\ candidate[m] = 1
            /\ admissionCount[m] = 0

ChangedModuleKeepsAssetsAndActors ==
    phase \in {"TargetRunning", "SystemVerifying", "Completed"} =>
        \A m \in ChangedPreservedModules :
            /\ candidate[m] >= 1
            /\ corpus[m] >= 1
            /\ coderSession[m] # "Absent"
            /\ verifierSession[m] # "Absent"

OneRoleOwnsEachModuleWorktree ==
    \A m \in Modules :
        /\ activeRole[m] = "Coder" =>
            /\ moduleState[m] = "Coding"
            /\ coderSession[m] = "Active"
            /\ verifierSession[m] # "Active"
        /\ activeRole[m] = "Verifier" =>
            /\ moduleState[m] = "Verifying"
            /\ verifierSession[m] = "Active"
            /\ coderSession[m] # "Active"

RetryDoesNotReplaceIdentity ==
    \A m \in TargetModules :
        admissionCount[m] > 0 =>
            /\ worktreeIdentity[m] =
                IF m \in PreservedModules THEN 1 ELSE 2
            /\ roleGeneration[m] =
                IF m \in PreservedModules THEN 1 ELSE 2

SystemJoinUsesCompleteTarget ==
    systemState \in {"Active", "Accepted"} => AllTargetModulesAccepted

ReplanEventuallyCompiles ==
    phase = "ReplanRequired" ~> phase = "TargetRunning"

=============================================================================
