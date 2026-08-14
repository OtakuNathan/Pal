-------------------- MODULE ContinuationLifecycle --------------------
EXTENDS Naturals, TLC

CONSTANT MaxRetries

Schemas == {"v7", "v8", "other"}
Shapes == {"plaintext", "encrypted", "invalid"}
Phases == {
    "Stored", "Ready", "Running", "Failed", "Rejected",
    "Completed", "Terminal"
}
ErrorClasses == {"none", "transient", "deterministic"}

VARIABLES
    phase,
    schema,
    shape,
    workerStarted,
    errorClass,
    errorVisible,
    retries

vars == <<
    phase, schema, shape, workerStarted, errorClass, errorVisible, retries
>>

Init ==
    /\ phase = "Stored"
    /\ schema \in Schemas
    /\ shape \in Shapes
    /\ workerStarted = FALSE
    /\ errorClass = "none"
    /\ errorVisible = FALSE
    /\ retries = 0

AdmitCurrent ==
    /\ phase = "Stored"
    /\ schema = "v8"
    /\ shape = "encrypted"
    /\ phase' = "Ready"
    /\ UNCHANGED <<schema, shape, workerStarted, errorClass,
                    errorVisible, retries>>

RejectUnsupported ==
    /\ phase = "Stored"
    /\ ~(schema = "v8" /\ shape = "encrypted")
    /\ phase' = "Rejected"
    /\ errorClass' = "deterministic"
    /\ errorVisible' = TRUE
    /\ UNCHANGED <<schema, shape, workerStarted, retries>>

StartWorker ==
    /\ phase = "Ready"
    /\ schema = "v8"
    /\ shape = "encrypted"
    /\ phase' = "Running"
    /\ workerStarted' = TRUE
    /\ UNCHANGED <<schema, shape, errorClass, errorVisible, retries>>

WorkerSucceeds ==
    /\ phase = "Running"
    /\ phase' = "Completed"
    /\ UNCHANGED <<schema, shape, workerStarted, errorClass,
                    errorVisible, retries>>

WorkerFailsTransiently ==
    /\ phase = "Running"
    /\ phase' = "Failed"
    /\ errorClass' = "transient"
    /\ errorVisible' = TRUE
    /\ UNCHANGED <<schema, shape, workerStarted, retries>>

WorkerFailsDeterministically ==
    /\ phase = "Running"
    /\ phase' = "Failed"
    /\ errorClass' = "deterministic"
    /\ errorVisible' = TRUE
    /\ UNCHANGED <<schema, shape, workerStarted, retries>>

RetryTransientFailure ==
    /\ phase = "Failed"
    /\ errorClass = "transient"
    /\ retries < MaxRetries
    /\ phase' = "Ready"
    /\ errorClass' = "none"
    /\ errorVisible' = FALSE
    /\ retries' = retries + 1
    /\ UNCHANGED <<schema, shape, workerStarted>>

Settle ==
    /\ phase \in {"Completed", "Failed", "Rejected"}
    /\ phase' = "Terminal"
    /\ UNCHANGED <<schema, shape, workerStarted, errorClass,
                    errorVisible, retries>>

Terminal ==
    /\ phase = "Terminal"
    /\ UNCHANGED vars

Next ==
    \/ AdmitCurrent
    \/ RejectUnsupported
    \/ StartWorker
    \/ WorkerSucceeds
    \/ WorkerFailsTransiently
    \/ WorkerFailsDeterministically
    \/ RetryTransientFailure
    \/ Settle
    \/ Terminal

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ phase \in Phases
    /\ schema \in Schemas
    /\ shape \in Shapes
    /\ workerStarted \in BOOLEAN
    /\ errorClass \in ErrorClasses
    /\ errorVisible \in BOOLEAN
    /\ retries \in 0..MaxRetries

WorkerStartsOnlyWithCurrentL1 ==
    workerStarted => schema = "v8" /\ shape = "encrypted"

LegacyCheckpointNeverBecomesReady ==
    schema = "v7" => phase \notin {"Ready", "Running", "Completed"}

PlaintextCheckpointNeverBecomesReady ==
    shape = "plaintext" => phase \notin {"Ready", "Running", "Completed"}

FailureRemainsVisible ==
    errorClass # "none" => errorVisible

DeterministicFailureIsNotRetryable ==
    errorClass = "deterministic" => phase \in {"Failed", "Rejected", "Terminal"}

======================================================================
