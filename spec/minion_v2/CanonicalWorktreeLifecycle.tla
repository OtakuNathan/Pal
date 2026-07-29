------------------- MODULE CanonicalWorktreeLifecycle -------------------
EXTENDS Naturals, TLC

\* One logical Module owns one canonical worktree. Coder and Verifier take
\* turns on that worktree and every accepted handoff is a linear Git commit.
\* System verification owns one Integration worktree. Terminal cleanup may
\* remove internal worktrees only after a durable delivery receipt exists.

CONSTANTS MaxHead, MaxCorpus

Phases == {
    "CoderReady", "Coding", "VerifierReady", "Verifying",
    "ModuleAccepted", "SystemVerifying", "Delivered", "Completed"
}
Owners == {"None", "Coder", "Verifier", "SystemVerifier"}

VARIABLES
    phase,
    owner,
    moduleHead,
    verifierCorpus,
    deliveryReceipt,
    internalWorktrees,
    deliveryAvailable,
    managerUp

vars == <<
    phase, owner, moduleHead, verifierCorpus, deliveryReceipt,
    internalWorktrees, deliveryAvailable, managerUp
>>

Init ==
    /\ phase = "CoderReady"
    /\ owner = "None"
    /\ moduleHead = 0
    /\ verifierCorpus = 0
    /\ deliveryReceipt = FALSE
    /\ internalWorktrees = TRUE
    /\ deliveryAvailable = FALSE
    /\ managerUp = TRUE

StartCoder ==
    /\ managerUp
    /\ phase = "CoderReady"
    /\ owner = "None"
    /\ phase' = "Coding"
    /\ owner' = "Coder"
    /\ UNCHANGED <<moduleHead, verifierCorpus, deliveryReceipt,
        internalWorktrees, deliveryAvailable, managerUp>>

CoderCommit ==
    /\ managerUp
    /\ phase = "Coding"
    /\ owner = "Coder"
    /\ moduleHead < MaxHead
    /\ phase' = "VerifierReady"
    /\ owner' = "None"
    /\ moduleHead' = moduleHead + 1
    /\ UNCHANGED <<verifierCorpus, deliveryReceipt,
        internalWorktrees, deliveryAvailable, managerUp>>

StartVerifier ==
    /\ managerUp
    /\ phase = "VerifierReady"
    /\ owner = "None"
    /\ phase' = "Verifying"
    /\ owner' = "Verifier"
    /\ UNCHANGED <<moduleHead, verifierCorpus, deliveryReceipt,
        internalWorktrees, deliveryAvailable, managerUp>>

VerifierFailWithRegression ==
    /\ managerUp
    /\ phase = "Verifying"
    /\ owner = "Verifier"
    /\ moduleHead < MaxHead
    /\ verifierCorpus < MaxCorpus
    /\ phase' = "CoderReady"
    /\ owner' = "None"
    /\ moduleHead' = moduleHead + 1
    /\ verifierCorpus' = verifierCorpus + 1
    /\ UNCHANGED <<deliveryReceipt, internalWorktrees,
        deliveryAvailable, managerUp>>

VerifierPass ==
    /\ managerUp
    /\ phase = "Verifying"
    /\ owner = "Verifier"
    /\ phase' = "ModuleAccepted"
    /\ owner' = "None"
    /\ UNCHANGED <<moduleHead, verifierCorpus, deliveryReceipt,
        internalWorktrees, deliveryAvailable, managerUp>>

StartSystemVerifier ==
    /\ managerUp
    /\ phase = "ModuleAccepted"
    /\ owner = "None"
    /\ phase' = "SystemVerifying"
    /\ owner' = "SystemVerifier"
    /\ UNCHANGED <<moduleHead, verifierCorpus, deliveryReceipt,
        internalWorktrees, deliveryAvailable, managerUp>>

PublishDelivery ==
    /\ managerUp
    /\ phase = "SystemVerifying"
    /\ owner = "SystemVerifier"
    /\ phase' = "Delivered"
    /\ owner' = "None"
    /\ deliveryReceipt' = TRUE
    /\ deliveryAvailable' = TRUE
    /\ UNCHANGED <<moduleHead, verifierCorpus, internalWorktrees, managerUp>>

CleanupInternalWorktrees ==
    /\ managerUp
    /\ phase = "Delivered"
    /\ owner = "None"
    /\ deliveryReceipt
    /\ internalWorktrees
    /\ internalWorktrees' = FALSE
    /\ UNCHANGED <<phase, owner, moduleHead, verifierCorpus,
        deliveryReceipt, deliveryAvailable, managerUp>>

Complete ==
    /\ managerUp
    /\ phase = "Delivered"
    /\ deliveryReceipt
    /\ ~internalWorktrees
    /\ phase' = "Completed"
    /\ UNCHANGED <<owner, moduleHead, verifierCorpus, deliveryReceipt,
        internalWorktrees, deliveryAvailable, managerUp>>

CrashManager ==
    /\ managerUp
    /\ managerUp' = FALSE
    /\ owner' = "None"
    /\ phase' =
        IF phase = "Coding" THEN "CoderReady"
        ELSE IF phase = "Verifying" THEN "VerifierReady"
        ELSE IF phase = "SystemVerifying" THEN "ModuleAccepted"
        ELSE phase
    /\ UNCHANGED <<moduleHead, verifierCorpus, deliveryReceipt,
        internalWorktrees, deliveryAvailable>>

RestartManager ==
    /\ ~managerUp
    /\ managerUp' = TRUE
    /\ UNCHANGED <<phase, owner, moduleHead, verifierCorpus,
        deliveryReceipt, internalWorktrees, deliveryAvailable>>

Next ==
    \/ StartCoder
    \/ CoderCommit
    \/ StartVerifier
    \/ VerifierFailWithRegression
    \/ VerifierPass
    \/ StartSystemVerifier
    \/ PublishDelivery
    \/ CleanupInternalWorktrees
    \/ Complete
    \/ CrashManager
    \/ RestartManager

Spec == Init /\ [][Next]_vars /\ WF_vars(RestartManager)

TypeOK ==
    /\ phase \in Phases
    /\ owner \in Owners
    /\ moduleHead \in 0..MaxHead
    /\ verifierCorpus \in 0..MaxCorpus
    /\ deliveryReceipt \in BOOLEAN
    /\ internalWorktrees \in BOOLEAN
    /\ deliveryAvailable \in BOOLEAN
    /\ managerUp \in BOOLEAN

ExclusiveCanonicalOwner ==
    /\ owner = "Coder" => phase = "Coding"
    /\ owner = "Verifier" => phase = "Verifying"
    /\ owner = "SystemVerifier" => phase = "SystemVerifying"

LinearVerifierAssets ==
    verifierCorpus <= moduleHead

CleanupRequiresReceipt ==
    ~internalWorktrees => deliveryReceipt

CleanupPreservesDelivery ==
    ~internalWorktrees => deliveryAvailable

CompletedIsCleanAndDeliverable ==
    phase = "Completed" =>
        /\ deliveryReceipt
        /\ ~internalWorktrees
        /\ deliveryAvailable

=============================================================================
