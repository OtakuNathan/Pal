-------------------- MODULE LogicalCoroutineSnapshotLifecycle --------------------
EXTENDS Naturals, TLC

CONSTANTS MaxSequence, MaxTurn, MaxFence, Retention, RequiredModules

ProcessStates == {"Absent", "Running"}
CoroutineStates == {"Active", "WaitingQuestion", "Triage", "Submitted", "Completed"}

VARIABLES
    process,
    coroutine,
    protocolClosed,
    runtimeDirty,
    resourceHeld,
    managerHasPayload,
    checkpointPresent,
    checkpointEncrypted,
    checkpointAuthenticated,
    checkpointSequence,
    currentFence,
    checkpointProducerFence,
    checkpointModules,
    checkpointSpecValid,
    checkpointReplayState,
    checkpointHandleBirthTurn,
    installedModules,
    userTurn,
    handlePresent,
    handleBirthTurn,
    fileSnapshotPresent,
    l1FileDeliveryPresent

vars == <<
    process, coroutine, protocolClosed, runtimeDirty, resourceHeld, managerHasPayload,
    checkpointPresent, checkpointEncrypted, checkpointAuthenticated,
    checkpointSequence, currentFence, checkpointProducerFence,
    checkpointModules, checkpointSpecValid, checkpointReplayState,
    checkpointHandleBirthTurn, installedModules, userTurn,
    handlePresent, handleBirthTurn, fileSnapshotPresent, l1FileDeliveryPresent
>>

Init ==
    /\ process = "Running"
    /\ coroutine = "Active"
    /\ protocolClosed = TRUE
    /\ runtimeDirty = FALSE
    /\ resourceHeld = TRUE
    /\ managerHasPayload = FALSE
    /\ checkpointPresent = FALSE
    /\ checkpointEncrypted = FALSE
    /\ checkpointAuthenticated = FALSE
    /\ checkpointSequence = 0
    /\ currentFence = 1
    /\ checkpointProducerFence = 0
    /\ checkpointModules = {}
    /\ checkpointSpecValid = FALSE
    /\ checkpointReplayState = "None"
    /\ checkpointHandleBirthTurn = 0
    /\ installedModules = RequiredModules
    /\ userTurn = 0
    /\ handlePresent = FALSE
    /\ handleBirthTurn = 0
    /\ fileSnapshotPresent = FALSE
    /\ l1FileDeliveryPresent = FALSE

StartTool ==
    /\ process = "Running"
    /\ coroutine = "Active"
    /\ protocolClosed
    /\ protocolClosed' = FALSE
    /\ UNCHANGED <<process, coroutine, runtimeDirty, resourceHeld, managerHasPayload,
                    checkpointPresent, checkpointEncrypted,
                    checkpointAuthenticated, checkpointSequence,
                    currentFence, checkpointProducerFence,
                    checkpointModules, checkpointSpecValid,
                    checkpointReplayState, checkpointHandleBirthTurn,
                    installedModules,
                    userTurn, handlePresent, handleBirthTurn,
                    fileSnapshotPresent, l1FileDeliveryPresent>>

FinishTool ==
    /\ process = "Running"
    /\ ~protocolClosed
    /\ protocolClosed' = TRUE
    /\ runtimeDirty' = TRUE
    /\ UNCHANGED <<process, coroutine, resourceHeld, managerHasPayload,
                    checkpointPresent, checkpointEncrypted,
                    checkpointAuthenticated, checkpointSequence,
                    currentFence, checkpointProducerFence,
                    checkpointModules, checkpointSpecValid,
                    checkpointReplayState, checkpointHandleBirthTurn,
                    installedModules,
                    userTurn, handlePresent, handleBirthTurn,
                    fileSnapshotPresent, l1FileDeliveryPresent>>

StoreReplayHandle ==
    /\ process = "Running"
    /\ coroutine = "Active"
    /\ protocolClosed
    /\ handlePresent' = TRUE
    /\ handleBirthTurn' = userTurn
    /\ fileSnapshotPresent' = TRUE
    /\ l1FileDeliveryPresent' = TRUE
    /\ runtimeDirty' = TRUE
    /\ UNCHANGED <<process, coroutine, protocolClosed, resourceHeld,
                    managerHasPayload, checkpointPresent,
                    checkpointEncrypted, checkpointAuthenticated,
                    checkpointSequence, currentFence,
                    checkpointProducerFence, checkpointModules,
                    checkpointSpecValid, checkpointReplayState,
                    checkpointHandleBirthTurn, installedModules, userTurn>>

AdvanceUserTurn ==
    /\ process = "Running"
    /\ coroutine = "Active"
    /\ protocolClosed
    /\ userTurn < MaxTurn
    /\ userTurn' = userTurn + 1
    /\ handlePresent' = IF handlePresent /\ userTurn' - handleBirthTurn >= Retention
                         THEN FALSE ELSE handlePresent
    /\ fileSnapshotPresent' = IF handlePresent /\ userTurn' - handleBirthTurn >= Retention
                               THEN FALSE ELSE fileSnapshotPresent
    /\ l1FileDeliveryPresent' =
        IF handlePresent /\ userTurn' - handleBirthTurn >= Retention
        THEN FALSE ELSE l1FileDeliveryPresent
    /\ runtimeDirty' = TRUE
    /\ UNCHANGED <<process, coroutine, protocolClosed, resourceHeld,
                    managerHasPayload, checkpointPresent,
                    checkpointEncrypted, checkpointAuthenticated,
                    checkpointSequence, currentFence,
                    checkpointProducerFence, checkpointModules,
                    checkpointSpecValid, checkpointReplayState,
                    checkpointHandleBirthTurn, installedModules,
                    handleBirthTurn>>

AskQuestion ==
    /\ process = "Running"
    /\ coroutine = "Active"
    /\ protocolClosed
    /\ coroutine' = "WaitingQuestion"
    /\ UNCHANGED <<process, protocolClosed, runtimeDirty, resourceHeld, managerHasPayload,
                    checkpointPresent, checkpointEncrypted,
                    checkpointAuthenticated, checkpointSequence,
                    currentFence, checkpointProducerFence,
                    checkpointModules, checkpointSpecValid,
                    checkpointReplayState, checkpointHandleBirthTurn,
                    installedModules,
                    userTurn, handlePresent, handleBirthTurn,
                    fileSnapshotPresent, l1FileDeliveryPresent>>

AnswerQuestion ==
    /\ process = "Running"
    /\ coroutine = "WaitingQuestion"
    /\ coroutine' = "Active"
    /\ UNCHANGED <<process, protocolClosed, runtimeDirty, resourceHeld, managerHasPayload,
                    checkpointPresent, checkpointEncrypted,
                    checkpointAuthenticated, checkpointSequence,
                    currentFence, checkpointProducerFence,
                    checkpointModules, checkpointSpecValid,
                    checkpointReplayState, checkpointHandleBirthTurn,
                    installedModules,
                    userTurn, handlePresent, handleBirthTurn,
                    fileSnapshotPresent, l1FileDeliveryPresent>>

PersistSafePoint ==
    /\ process = "Running"
    /\ protocolClosed
    /\ runtimeDirty \/ ~checkpointPresent
    /\ checkpointSequence < MaxSequence
    /\ checkpointPresent' = TRUE
    /\ checkpointEncrypted' = TRUE
    /\ checkpointAuthenticated' = TRUE
    /\ checkpointSequence' = checkpointSequence + 1
    /\ checkpointProducerFence' = currentFence
    /\ checkpointModules' = RequiredModules
    /\ checkpointSpecValid' = TRUE
    /\ checkpointReplayState' =
        IF fileSnapshotPresent THEN "HandleAndFile"
        ELSE IF handlePresent THEN "HandleOnly"
        ELSE "None"
    /\ checkpointHandleBirthTurn' = handleBirthTurn
    /\ runtimeDirty' = FALSE
    /\ UNCHANGED <<process, coroutine, protocolClosed, resourceHeld,
                    managerHasPayload, currentFence, installedModules,
                    userTurn, handlePresent, handleBirthTurn,
                    fileSnapshotPresent, l1FileDeliveryPresent>>

SaveAndStop(nextState) ==
    /\ process = "Running"
    /\ protocolClosed
    /\ nextState \in {"Triage", "Submitted"}
    /\ checkpointSequence < MaxSequence
    /\ coroutine' = nextState
    /\ process' = "Absent"
    /\ runtimeDirty' = FALSE
    /\ resourceHeld' = FALSE
    /\ checkpointPresent' = TRUE
    /\ checkpointEncrypted' = TRUE
    /\ checkpointAuthenticated' = TRUE
    /\ checkpointSequence' = checkpointSequence + 1
    /\ checkpointProducerFence' = currentFence
    /\ checkpointModules' = RequiredModules
    /\ checkpointSpecValid' = TRUE
    /\ checkpointReplayState' =
        IF fileSnapshotPresent THEN "HandleAndFile"
        ELSE IF handlePresent THEN "HandleOnly"
        ELSE "None"
    /\ checkpointHandleBirthTurn' = handleBirthTurn
    /\ installedModules' = {}
    /\ handlePresent' = FALSE
    /\ fileSnapshotPresent' = FALSE
    /\ l1FileDeliveryPresent' = FALSE
    /\ UNCHANGED <<protocolClosed, managerHasPayload, currentFence,
                    userTurn, handleBirthTurn>>

CrashAtClosedBoundary ==
    /\ process = "Running"
    /\ protocolClosed
    /\ checkpointPresent
    /\ ~runtimeDirty
    /\ process' = "Absent"
    /\ resourceHeld' = FALSE
    /\ installedModules' = {}
    /\ handlePresent' = FALSE
    /\ fileSnapshotPresent' = FALSE
    /\ l1FileDeliveryPresent' = FALSE
    /\ UNCHANGED <<coroutine, protocolClosed, runtimeDirty, managerHasPayload,
                    checkpointPresent, checkpointEncrypted,
                    checkpointAuthenticated, checkpointSequence,
                    currentFence, checkpointProducerFence,
                    checkpointModules, checkpointSpecValid,
                    checkpointReplayState, checkpointHandleBirthTurn, userTurn,
                    handleBirthTurn>>

RestoreWithNewIncarnation ==
    /\ process = "Absent"
    /\ coroutine \in {"Active", "Triage", "Submitted"}
    /\ checkpointPresent
    /\ checkpointEncrypted
    /\ checkpointAuthenticated
    /\ checkpointModules = RequiredModules
    /\ checkpointSpecValid
    /\ currentFence < MaxFence
    /\ process' = "Running"
    /\ coroutine' = "Active"
    /\ resourceHeld' = TRUE
    /\ currentFence' = currentFence + 1
    /\ installedModules' = RequiredModules
    /\ protocolClosed' = TRUE
    /\ runtimeDirty' = FALSE
    /\ handlePresent' = (checkpointReplayState \in {"HandleOnly", "HandleAndFile"})
    /\ fileSnapshotPresent' = (checkpointReplayState = "HandleAndFile")
    /\ l1FileDeliveryPresent' = (checkpointReplayState = "HandleAndFile")
    /\ handleBirthTurn' = checkpointHandleBirthTurn
    /\ UNCHANGED <<managerHasPayload, checkpointPresent,
                    checkpointEncrypted, checkpointAuthenticated,
                    checkpointSequence, checkpointProducerFence,
                    checkpointModules, checkpointSpecValid,
                    checkpointReplayState, checkpointHandleBirthTurn, userTurn>>

CompleteAndRetire ==
    /\ process = "Running"
    /\ protocolClosed
    /\ coroutine' = "Completed"
    /\ process' = "Absent"
    /\ runtimeDirty' = FALSE
    /\ resourceHeld' = FALSE
    /\ checkpointPresent' = FALSE
    /\ checkpointEncrypted' = FALSE
    /\ checkpointAuthenticated' = FALSE
    /\ checkpointModules' = {}
    /\ checkpointSpecValid' = FALSE
    /\ checkpointReplayState' = "None"
    /\ checkpointHandleBirthTurn' = 0
    /\ installedModules' = {}
    /\ handlePresent' = FALSE
    /\ fileSnapshotPresent' = FALSE
    /\ l1FileDeliveryPresent' = FALSE
    /\ UNCHANGED <<protocolClosed, managerHasPayload,
                    checkpointSequence, currentFence,
                    checkpointProducerFence, userTurn, handleBirthTurn>>

Terminal ==
    /\ coroutine = "Completed"
    /\ UNCHANGED vars

Next ==
    \/ StartTool
    \/ FinishTool
    \/ StoreReplayHandle
    \/ AdvanceUserTurn
    \/ AskQuestion
    \/ AnswerQuestion
    \/ PersistSafePoint
    \/ SaveAndStop("Triage")
    \/ SaveAndStop("Submitted")
    \/ CrashAtClosedBoundary
    \/ RestoreWithNewIncarnation
    \/ CompleteAndRetire
    \/ Terminal

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ process \in ProcessStates
    /\ coroutine \in CoroutineStates
    /\ protocolClosed \in BOOLEAN
    /\ runtimeDirty \in BOOLEAN
    /\ resourceHeld \in BOOLEAN
    /\ managerHasPayload \in BOOLEAN
    /\ checkpointPresent \in BOOLEAN
    /\ checkpointEncrypted \in BOOLEAN
    /\ checkpointAuthenticated \in BOOLEAN
    /\ checkpointSequence \in 0..MaxSequence
    /\ currentFence \in 1..MaxFence
    /\ checkpointProducerFence \in Nat
    /\ checkpointModules \subseteq RequiredModules
    /\ checkpointSpecValid \in BOOLEAN
    /\ checkpointReplayState \in {"None", "HandleOnly", "HandleAndFile"}
    /\ checkpointHandleBirthTurn \in 0..MaxTurn
    /\ installedModules \subseteq RequiredModules
    /\ userTurn \in 0..MaxTurn
    /\ handlePresent \in BOOLEAN
    /\ handleBirthTurn \in 0..MaxTurn
    /\ fileSnapshotPresent \in BOOLEAN
    /\ l1FileDeliveryPresent \in BOOLEAN

ResourceOwnedExactlyByNativeProcess == resourceHeld <=> process = "Running"
ManagerNeverOwnsRuntimePayload == ~managerHasPayload
DurableCheckpointIsEncrypted == checkpointPresent => checkpointEncrypted
RestoreIsAllOrNothing == installedModules \in {{}, RequiredModules}
StoppedCoroutineHasNoLiveRuntime == process = "Absent" => installedModules = {}
ReplayStateSharesOneLifetime == fileSnapshotPresent => handlePresent
FileAuthorityCommitsAtomicallyWithL1 ==
    fileSnapshotPresent <=> l1FileDeliveryPresent
CheckpointReplayStateSharesOneLifetime ==
    checkpointReplayState = "HandleAndFile" => checkpointPresent
ReplayStateExpiresByNPlusFive ==
    handlePresent => userTurn - handleBirthTurn < Retention
CheckpointReplayDoesNotResurrectExpiredState ==
    (~runtimeDirty /\ checkpointReplayState # "None") =>
        userTurn - checkpointHandleBirthTurn < Retention
TerminalDeletesAllCoroutineState ==
    coroutine = "Completed" =>
        ~checkpointPresent /\ checkpointReplayState = "None" /\
        ~handlePresent /\ ~fileSnapshotPresent

=============================================================================
