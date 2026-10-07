-------------------- MODULE StartupRecoveryLifecycle --------------------
EXTENDS Naturals, TLC

CONSTANTS MaxFailures, MaxStarts, MaxSequence, AllowMissingResumeAsFresh

SessionStates == {"Uninitialized", "Active", "Suspended"}
Phases == {"Admission", "Prepared", "Launching", "AwaitingAck", "Running", "Stopped", "Rejected", "Terminal"}
CheckpointStates == {"Missing", "Valid", "Invalid"}
Errors == {"none", "startup_failure", "worker_failure", "checkpoint_missing", "checkpoint_invalid"}

VARIABLES session, phase, checkpoint, sequence, producerFence, fence,
          admission, failures, error, errorVisible, initializedEver,
          freshAfterInitialization, semanticWork, initializationFence

vars == <<session, phase, checkpoint, sequence, producerFence, fence,
          admission, failures, error, errorVisible, initializedEver,
          freshAfterInitialization, semanticWork, initializationFence>>

Init ==
    /\ session = "Uninitialized"
    /\ phase = "Admission"
    /\ checkpoint = "Missing"
    /\ sequence = 0
    /\ producerFence = 0
    /\ fence = 0
    /\ admission = "None"
    /\ failures = 0
    /\ error = "none"
    /\ errorVisible = FALSE
    /\ initializedEver = FALSE
    /\ freshAfterInitialization = FALSE
    /\ semanticWork = FALSE
    /\ initializationFence = 0

AdmitFresh ==
    /\ phase = "Admission"
    /\ checkpoint = "Missing"
    /\ session = "Uninitialized" \/ AllowMissingResumeAsFresh
    /\ phase' = "Prepared"
    /\ admission' = "Fresh"
    /\ freshAfterInitialization' = freshAfterInitialization \/ initializedEver
    /\ UNCHANGED <<session, checkpoint, sequence, producerFence, fence,
                    failures, error, errorVisible, initializedEver, semanticWork, initializationFence>>

AdmitResume ==
    /\ phase = "Admission"
    /\ checkpoint = "Valid"
    \* A file published before a lost initialization acknowledgment is restored,
    \* even when the durable session still says Uninitialized.
    /\ session' = IF session = "Uninitialized" THEN "Active" ELSE session
    /\ initializedEver' = TRUE
    /\ phase' = "Prepared"
    /\ admission' = "Resume"
    /\ UNCHANGED <<checkpoint, sequence, producerFence, fence,
                    failures, error, errorVisible,
                    freshAfterInitialization, semanticWork, initializationFence>>

RejectCheckpoint ==
    /\ phase = "Admission"
    /\ checkpoint = "Invalid" \/ (checkpoint = "Missing" /\ session # "Uninitialized")
    /\ phase' = "Rejected"
    /\ error' = IF checkpoint = "Missing" THEN "checkpoint_missing" ELSE "checkpoint_invalid"
    /\ errorVisible' = TRUE
    /\ UNCHANGED <<session, checkpoint, sequence, producerFence, fence,
                    admission, failures, initializedEver,
                    freshAfterInitialization, semanticWork, initializationFence>>

StartProcess ==
    /\ phase = "Prepared"
    /\ fence < MaxStarts
    /\ phase' = IF admission = "Fresh" THEN "Launching" ELSE "Running"
    /\ session' = IF session = "Uninitialized" THEN "Uninitialized" ELSE "Active"
    /\ fence' = fence + 1
    /\ UNCHANGED <<checkpoint, sequence, producerFence,
                    admission, failures, error, errorVisible, initializedEver,
                    freshAfterInitialization, semanticWork, initializationFence>>

PublishCheckpoint(token) ==
    /\ phase \in {"Launching", "Running"}
    /\ sequence < MaxSequence
    /\ token = fence
    /\ checkpoint' = "Valid"
    /\ sequence' = sequence + 1
    /\ producerFence' = fence
    \* Publication and durable initialization are separate crash boundaries.
    /\ UNCHANGED <<session, phase, fence, admission, failures, error,
                    errorVisible, initializedEver, freshAfterInitialization, semanticWork, initializationFence>>

CommitInitialization(token) ==
    /\ phase = "Launching"
    /\ checkpoint = "Valid"
    /\ producerFence = fence
    /\ token = fence
    /\ session' = "Active"
    /\ initializedEver' = TRUE
    /\ initializationFence' = token
    /\ phase' = "AwaitingAck"
    /\ UNCHANGED <<checkpoint, sequence, producerFence, fence, admission,
                    failures, error, errorVisible, freshAfterInitialization, semanticWork>>


ReceiveInitializationAck ==
    /\ phase = "AwaitingAck"
    /\ phase' = "Running"
    /\ UNCHANGED <<session, checkpoint, sequence, producerFence, fence, admission,
                    failures, error, errorVisible, initializedEver,
                    freshAfterInitialization, semanticWork, initializationFence>>

RejectStaleInitialization(token) ==
    /\ phase \in {"Launching", "AwaitingAck", "Running"}
    /\ token # fence
    \* The Manager fence check rejects an old process before publication or
    \* the session transition. Neither checkpoint nor status may change.
    /\ UNCHANGED vars

DoSemanticWork ==
    /\ phase = "Running"
    /\ semanticWork' = TRUE
    /\ UNCHANGED <<session, phase, checkpoint, sequence, producerFence, fence,
                    admission, failures, error, errorVisible, initializedEver,
                    freshAfterInitialization, initializationFence>>

FailAttempt ==
    /\ phase \in {"Prepared", "Launching", "AwaitingAck", "Running"}
    /\ failures < MaxFailures
    /\ phase' = "Stopped"
    /\ session' = IF session = "Uninitialized" THEN "Uninitialized" ELSE "Suspended"
    /\ failures' = failures + 1
    /\ error' = IF session = "Uninitialized" THEN "startup_failure" ELSE "worker_failure"
    /\ errorVisible' = TRUE
    /\ admission' = "None"
    /\ UNCHANGED <<checkpoint, sequence, producerFence, fence, initializedEver,
                    freshAfterInitialization, semanticWork, initializationFence>>

CrashBeforeAck ==
    /\ phase \in {"Launching", "AwaitingAck"}
    /\ phase' = "Stopped"
    /\ session' = IF session = "Uninitialized" THEN "Uninitialized" ELSE "Suspended"
    /\ admission' = "None"
    \* Manager/process loss is an uncharged rebind, unlike FailAttempt.
    /\ UNCHANGED <<checkpoint, sequence, producerFence, fence, failures,
                    error, errorVisible, initializedEver, freshAfterInitialization, semanticWork, initializationFence>>

Suspend ==
    /\ phase = "Running"
    /\ phase' = "Stopped"
    /\ session' = "Suspended"
    /\ admission' = "None"
    /\ UNCHANGED <<checkpoint, sequence, producerFence, fence, failures,
                    error, errorVisible, initializedEver, freshAfterInitialization, semanticWork, initializationFence>>

RetryOrResume ==
    /\ phase = "Stopped"
    /\ failures < MaxFailures
    /\ phase' = "Admission"
    /\ error' = "none"
    /\ errorVisible' = FALSE
    /\ UNCHANGED <<session, checkpoint, sequence, producerFence, fence, admission,
                    failures, initializedEver, freshAfterInitialization, semanticWork, initializationFence>>

ExhaustFailures ==
    /\ phase = "Stopped"
    /\ failures = MaxFailures
    /\ phase' = "Terminal"
    \* Exhaustion does not pretend that a never-created coroutine was suspended.
    /\ UNCHANGED <<session, checkpoint, sequence, producerFence, fence, admission,
                    failures, error, errorVisible, initializedEver,
                    freshAfterInitialization, semanticWork, initializationFence>>

DamageCheckpoint(value) ==
    /\ phase \in {"Stopped", "Admission"}
    /\ checkpoint = "Valid"
    /\ value \in {"Missing", "Invalid"}
    /\ checkpoint' = value
    \* Loss/corruption cannot erase the durable initialization fact.
    /\ UNCHANGED <<session, phase, sequence, producerFence, fence, admission,
                    failures, error, errorVisible, initializedEver,
                    freshAfterInitialization, semanticWork, initializationFence>>

Next ==
    \/ AdmitFresh
    \/ AdmitResume
    \/ RejectCheckpoint
    \/ StartProcess
    \/ \E token \in 0..fence : PublishCheckpoint(token)
    \/ \E token \in 0..fence : CommitInitialization(token)
    \/ ReceiveInitializationAck
    \/ \E token \in 0..fence : RejectStaleInitialization(token)
    \/ DoSemanticWork
    \/ FailAttempt
    \/ CrashBeforeAck
    \/ Suspend
    \/ RetryOrResume
    \/ ExhaustFailures
    \/ DamageCheckpoint("Missing")
    \/ DamageCheckpoint("Invalid")

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(AdmitFresh)
    /\ WF_vars(AdmitResume)
    /\ WF_vars(RejectCheckpoint)
    /\ WF_vars(ExhaustFailures)

TypeOK ==
    /\ session \in SessionStates
    /\ phase \in Phases
    /\ checkpoint \in CheckpointStates
    /\ sequence \in 0..MaxSequence
    /\ fence \in 0..MaxStarts
    /\ producerFence \in 0..fence
    /\ initializationFence \in 0..fence
    /\ admission \in {"None", "Fresh", "Resume"}
    /\ failures \in 0..MaxFailures
    /\ error \in Errors
    /\ errorVisible \in BOOLEAN
    /\ initializedEver \in BOOLEAN
    /\ freshAfterInitialization \in BOOLEAN
    /\ semanticWork \in BOOLEAN

InitializationIsDurable == initializedEver <=> session # "Uninitialized"
FreshStartNeverForgetsInitialization == ~freshAfterInitialization
SemanticWorkRequiresInitialization == semanticWork => initializedEver
AwaitingAckHasDurableInitialization ==
    phase = "AwaitingAck" => initializedEver /\ checkpoint = "Valid" /\ producerFence = fence
InitializationRequiresCurrentFence ==
    (phase = "AwaitingAck" \/ (phase = "Running" /\ admission = "Fresh")) =>
        initializationFence = fence
RunningRequiresInitializedCheckpoint ==
    phase = "Running" =>
        /\ session = "Active"
        /\ checkpoint = "Valid"
        /\ (admission = "Fresh" => producerFence = fence)
NeverCreatedIsNotSuspended == ~initializedEver => session = "Uninitialized"
CheckpointFailureIsVisibleAndPermanent ==
    error \in {"checkpoint_missing", "checkpoint_invalid"} => phase = "Rejected" /\ errorVisible
FailureRemainsVisible == error # "none" => errorVisible
ExhaustedBudgetDoesNotRestart == failures = MaxFailures => phase \in {"Stopped", "Terminal"}

InitializationNeverReverts == [][initializedEver => initializedEver']_vars
CheckpointSequenceNeverRegresses == [][sequence' >= sequence]_vars

AdmissionEventuallyResolves == phase = "Admission" ~> phase # "Admission"
ExhaustionEventuallySettles == failures = MaxFailures ~> phase = "Terminal"

=============================================================================
