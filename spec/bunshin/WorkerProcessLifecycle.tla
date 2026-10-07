-------------------- MODULE WorkerProcessLifecycle --------------------
EXTENDS Naturals, TLC

CONSTANT MaxStarts

OwnerStates == {"Idle", "Running", "ExitRequested", "Reaping", "Recovering"}
ManagerStates == {"Up", "Down"}

VARIABLES
    managerState,
    ownerState,
    leaderLive,
    processGroupLive,
    runRegistered,
    worktreeOwned,
    terminalReceipt,
    starts

vars == <<
    managerState, ownerState, leaderLive, processGroupLive,
    runRegistered, worktreeOwned, terminalReceipt, starts
>>

Init ==
    /\ managerState = "Up"
    /\ ownerState = "Idle"
    /\ leaderLive = FALSE
    /\ processGroupLive = FALSE
    /\ runRegistered = FALSE
    /\ worktreeOwned = FALSE
    /\ terminalReceipt = FALSE
    /\ starts = 0

StartWorker ==
    /\ managerState = "Up"
    /\ ownerState = "Idle"
    /\ processGroupLive = FALSE
    /\ runRegistered = FALSE
    /\ worktreeOwned = FALSE
    /\ starts < MaxStarts
    /\ ownerState' = "Running"
    /\ leaderLive' = TRUE
    /\ processGroupLive' = TRUE
    /\ runRegistered' = TRUE
    /\ worktreeOwned' = TRUE
    /\ terminalReceipt' = FALSE
    /\ starts' = starts + 1
    /\ UNCHANGED managerState

WorkerTerminal ==
    /\ managerState = "Up"
    /\ ownerState = "Running"
    /\ terminalReceipt = FALSE
    \* IPC completion requests an exit.  It is not process completion.
    /\ ownerState' = "ExitRequested"
    /\ terminalReceipt' = TRUE
    /\ UNCHANGED <<managerState, leaderLive, processGroupLive,
        runRegistered, worktreeOwned, starts>>

RequestShutdown ==
    /\ managerState = "Up"
    /\ ownerState = "Running"
    /\ ownerState' = "ExitRequested"
    /\ UNCHANGED <<managerState, leaderLive, processGroupLive,
        runRegistered, worktreeOwned, terminalReceipt, starts>>

LeaderExits ==
    /\ managerState = "Up"
    /\ ownerState \in {"Running", "ExitRequested"}
    /\ leaderLive
    /\ leaderLive' = FALSE
    /\ ownerState' = "Reaping"
    \* Descendants may still keep the process group alive.
    /\ UNCHANGED <<managerState, processGroupLive, runRegistered,
        worktreeOwned, terminalReceipt, starts>>

BeginReap ==
    /\ managerState = "Up"
    /\ ownerState = "ExitRequested"
    /\ ownerState' = "Reaping"
    /\ UNCHANGED <<managerState, leaderLive, processGroupLive,
        runRegistered, worktreeOwned, terminalReceipt, starts>>

ReapProcessGroup ==
    /\ managerState = "Up"
    /\ ownerState \in {"Reaping", "Recovering"}
    /\ processGroupLive
    /\ processGroupLive' = FALSE
    /\ leaderLive' = FALSE
    /\ UNCHANGED <<managerState, ownerState, runRegistered,
        worktreeOwned, terminalReceipt, starts>>

FinalizeOwner ==
    /\ managerState = "Up"
    /\ ownerState \in {"Reaping", "Recovering"}
    /\ processGroupLive = FALSE
    /\ leaderLive = FALSE
    /\ ownerState' = "Idle"
    /\ runRegistered' = FALSE
    /\ worktreeOwned' = FALSE
    /\ UNCHANGED <<managerState, processGroupLive, leaderLive,
        terminalReceipt, starts>>

CrashManager ==
    /\ managerState = "Up"
    /\ ownerState # "Idle"
    /\ managerState' = "Down"
    /\ ownerState' = "Recovering"
    \* In-memory registration and the manager-held flock disappear, but a
    \* persisted process group must still be reaped before replacement.
    /\ runRegistered' = FALSE
    /\ worktreeOwned' = FALSE
    /\ UNCHANGED <<leaderLive, processGroupLive, terminalReceipt, starts>>

RestartManager ==
    /\ managerState = "Down"
    /\ managerState' = "Up"
    /\ UNCHANGED <<ownerState, leaderLive, processGroupLive,
        runRegistered, worktreeOwned, terminalReceipt, starts>>

Next ==
    \/ StartWorker
    \/ WorkerTerminal
    \/ RequestShutdown
    \/ LeaderExits
    \/ BeginReap
    \/ ReapProcessGroup
    \/ FinalizeOwner
    \/ CrashManager
    \/ RestartManager

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(RestartManager)
    /\ SF_vars(BeginReap)
    /\ SF_vars(ReapProcessGroup)
    /\ SF_vars(FinalizeOwner)

TypeOK ==
    /\ managerState \in ManagerStates
    /\ ownerState \in OwnerStates
    /\ leaderLive \in BOOLEAN
    /\ processGroupLive \in BOOLEAN
    /\ runRegistered \in BOOLEAN
    /\ worktreeOwned \in BOOLEAN
    /\ terminalReceipt \in BOOLEAN
    /\ starts \in 0..MaxStarts

IdleOnlyAfterReap ==
    ownerState = "Idle" => processGroupLive = FALSE

RegistrationAndWorktreeHaveOneOwner ==
    runRegistered = worktreeOwned

LeaderExitDoesNotReleaseOwnership ==
    /\ managerState = "Up"
    /\ ownerState = "Reaping"
    /\ processGroupLive
    =>
    /\ runRegistered
    /\ worktreeOwned

ReplacementCannotOverlapOldGroup ==
    processGroupLive => ownerState # "Idle"

TerminalIsNotProcessExit ==
    /\ terminalReceipt
    /\ processGroupLive
    /\ managerState = "Up"
    =>
    ownerState # "Idle"

ExitEventuallyReturnsToIdle ==
    /\ managerState = "Up"
    /\ ownerState \in {"ExitRequested", "Reaping"}
    ~>
    ownerState = "Idle"

=============================================================================
