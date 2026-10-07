---- MODULE ContractWorkItemLifecycle ----
EXTENDS Naturals, TLC

VARIABLES phase, checklist, blocking, executor, worker

vars == <<phase, checklist, blocking, executor, worker>>

Init ==
    /\ phase = "authoring"
    /\ checklist = "open"
    /\ blocking = FALSE
    /\ executor \in {"profile", "null"}
    /\ worker = FALSE

CompleteChecklist ==
    /\ checklist = "open"
    /\ checklist' = "complete"
    /\ UNCHANGED <<phase, blocking, executor, worker>>

SubmitContract ==
    /\ phase = "authoring"
    /\ checklist = "complete"
    /\ phase' = "submitted"
    /\ UNCHANGED <<checklist, blocking, executor, worker>>

StartReview ==
    /\ phase = "submitted"
    /\ phase' = "reviewing"
    /\ UNCHANGED <<checklist, blocking, executor, worker>>

ReviewPass ==
    /\ phase = "reviewing"
    /\ ~blocking
    /\ phase' = "accepted"
    /\ worker' = FALSE
    /\ UNCHANGED <<checklist, blocking, executor>>

AddBlockingFinding ==
    /\ phase = "reviewing"
    /\ phase' = "repair"
    /\ blocking' = TRUE
    /\ checklist' = "open"
    /\ worker' = FALSE
    /\ UNCHANGED executor

SpawnRepairWorker ==
    /\ phase = "repair"
    /\ executor = "profile"
    /\ ~worker
    /\ worker' = TRUE
    /\ UNCHANGED <<phase, checklist, blocking, executor>>

RepairComplete ==
    /\ phase = "repair"
    /\ executor = "profile"
    /\ worker
    /\ checklist = "complete"
    /\ phase' = "submitted"
    /\ blocking' = FALSE
    /\ worker' = FALSE
    /\ UNCHANGED <<checklist, executor>>

NullExecutionComplete ==
    /\ phase = "repair"
    /\ executor = "null"
    /\ checklist = "complete"
    /\ phase' = "submitted"
    /\ blocking' = FALSE
    /\ UNCHANGED <<checklist, executor, worker>>

Next ==
    \/ CompleteChecklist
    \/ SubmitContract
    \/ StartReview
    \/ ReviewPass
    \/ AddBlockingFinding
    \/ SpawnRepairWorker
    \/ RepairComplete
    \/ NullExecutionComplete

Spec == Init /\ [][Next]_vars

TypeInvariant ==
    /\ phase \in {"authoring", "submitted", "reviewing", "repair", "accepted"}
    /\ checklist \in {"open", "complete"}
    /\ blocking \in BOOLEAN
    /\ executor \in {"profile", "null"}
    /\ worker \in BOOLEAN

SubmittedRequiresComplete ==
    phase \in {"submitted", "reviewing", "accepted"} => checklist = "complete"

AcceptedHasNoBlockingFinding ==
    phase = "accepted" => ~blocking

NullExecutorHasNoWorker ==
    executor = "null" => ~worker

WorkerRequiresProfileExecutor ==
    worker => executor = "profile"

====
