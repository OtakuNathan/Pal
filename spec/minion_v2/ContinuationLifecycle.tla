-------------------- MODULE ContinuationLifecycle --------------------
EXTENDS Naturals, TLC

CONSTANT MaxRetries

Schemas == {"legacy", "v5"}
Shapes == {"items", "turns", "invalid"}
Phases == {
    "Stored", "Migrating", "Ready", "Running", "Failed", "Rejected",
    "Completed", "Terminal"
}
ErrorClasses == {"none", "transient", "deterministic"}

VARIABLES
    phase,
    schema,
    shape,
    originShape,
    migrationApplied,
    workerStarted,
    errorClass,
    errorVisible,
    retries

vars == <<
    phase, schema, shape, originShape, migrationApplied, workerStarted,
    errorClass, errorVisible, retries
>>

Init ==
    /\ phase = "Stored"
    /\ schema \in Schemas
    /\ shape \in Shapes
    /\ originShape = shape
    /\ migrationApplied = FALSE
    /\ workerStarted = FALSE
    /\ errorClass = "none"
    /\ errorVisible = FALSE
    /\ retries = 0

AdmitCurrent ==
    /\ phase = "Stored"
    /\ schema = "v5"
    /\ shape = "turns"
    /\ phase' = "Ready"
    /\ UNCHANGED <<schema, shape, originShape, migrationApplied,
        workerStarted, errorClass, errorVisible, retries>>

BeginLegacyMigration ==
    /\ phase = "Stored"
    \* The old and current payloads accidentally shared the v5 label, so
    \* admission must inspect shape instead of trusting the label alone.
    /\ shape = "items"
    /\ phase' = "Migrating"
    /\ UNCHANGED <<schema, shape, originShape, migrationApplied,
        workerStarted, errorClass, errorVisible, retries>>

CompleteLegacyMigration ==
    /\ phase = "Migrating"
    /\ phase' = "Ready"
    /\ schema' = "v5"
    /\ shape' = "turns"
    /\ migrationApplied' = TRUE
    /\ UNCHANGED <<originShape, workerStarted, errorClass, errorVisible,
        retries>>

RejectUnsupported ==
    /\ phase = "Stored"
    /\ shape # "items"
    /\ ~(schema = "v5" /\ shape = "turns")
    /\ phase' = "Rejected"
    /\ errorClass' = "deterministic"
    /\ errorVisible' = TRUE
    /\ UNCHANGED <<schema, shape, originShape, migrationApplied,
        workerStarted, retries>>

StartWorker ==
    /\ phase = "Ready"
    /\ schema = "v5"
    /\ shape = "turns"
    /\ phase' = "Running"
    /\ workerStarted' = TRUE
    /\ UNCHANGED <<schema, shape, originShape, migrationApplied,
        errorClass, errorVisible, retries>>

WorkerSucceeds ==
    /\ phase = "Running"
    /\ phase' = "Completed"
    /\ UNCHANGED <<schema, shape, originShape, migrationApplied,
        workerStarted, errorClass, errorVisible, retries>>

WorkerFailsTransiently ==
    /\ phase = "Running"
    /\ phase' = "Failed"
    /\ errorClass' = "transient"
    /\ errorVisible' = TRUE
    /\ UNCHANGED <<schema, shape, originShape, migrationApplied,
        workerStarted, retries>>

WorkerFailsDeterministically ==
    /\ phase = "Running"
    /\ phase' = "Failed"
    /\ errorClass' = "deterministic"
    /\ errorVisible' = TRUE
    /\ UNCHANGED <<schema, shape, originShape, migrationApplied,
        workerStarted, retries>>

RetryTransientFailure ==
    /\ phase = "Failed"
    /\ errorClass = "transient"
    /\ retries < MaxRetries
    /\ phase' = "Ready"
    /\ errorClass' = "none"
    /\ errorVisible' = FALSE
    /\ retries' = retries + 1
    /\ UNCHANGED <<schema, shape, originShape, migrationApplied,
        workerStarted>>

Settle ==
    /\ phase \in {"Completed", "Failed", "Rejected"}
    /\ phase' = "Terminal"
    /\ UNCHANGED <<schema, shape, originShape, migrationApplied,
        workerStarted, errorClass, errorVisible, retries>>

Terminal ==
    /\ phase = "Terminal"
    /\ UNCHANGED vars

Next ==
    \/ AdmitCurrent
    \/ BeginLegacyMigration
    \/ CompleteLegacyMigration
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
    /\ originShape \in Shapes
    /\ migrationApplied \in BOOLEAN
    /\ workerStarted \in BOOLEAN
    /\ errorClass \in ErrorClasses
    /\ errorVisible \in BOOLEAN
    /\ retries \in 0..MaxRetries

WorkerStartsOnlyWithCurrentL1 ==
    workerStarted => schema = "v5" /\ shape = "turns"

LegacyShapeRequiresRealMigration ==
    originShape = "items" /\ phase \in {"Ready", "Running", "Failed", "Completed", "Terminal"}
    => migrationApplied

FailureRemainsVisible ==
    errorClass # "none" => errorVisible

DeterministicFailureIsNotRetryable ==
    errorClass = "deterministic" => phase \in {"Failed", "Rejected", "Terminal"}

======================================================================
