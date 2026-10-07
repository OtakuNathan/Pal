---------------------- MODULE HarnessGenerationLifecycle ----------------------
EXTENDS Naturals, TLC

\* A harness lookup captures exactly one immutable registry generation.  A
\* detach changes only the current pointer: calls which already captured Codex
\* may finish, while later calls select Pal.  Two failed Codex attempts cause a
\* bounded per-assignment fallback without mutating the captured generation.

CONSTANT NoCall

VARIABLES registry, oldCall, newCall, failedCodex, oldCompleted, detached

vars == <<registry, oldCall, newCall, failedCodex, oldCompleted, detached>>

HarnessFor(reg, failures) ==
    IF reg = "codex" /\ failures < 2 THEN "codex" ELSE "pal"

Init ==
    /\ registry = "pal"
    /\ oldCall = NoCall
    /\ newCall = NoCall
    /\ failedCodex = 0
    /\ oldCompleted = FALSE
    /\ detached = FALSE

AttachCodex ==
    /\ registry = "pal"
    /\ registry' = "codex"
    /\ detached' = FALSE
    /\ UNCHANGED <<oldCall, newCall, failedCodex, oldCompleted>>

StartOld ==
    /\ oldCall = NoCall
    /\ oldCall' = HarnessFor(registry, failedCodex)
    /\ UNCHANGED <<registry, newCall, failedCodex, oldCompleted, detached>>

FailCodex ==
    /\ registry = "codex"
    /\ failedCodex < 2
    /\ failedCodex' = failedCodex + 1
    /\ UNCHANGED <<registry, oldCall, newCall, oldCompleted, detached>>

DetachCodex ==
    /\ registry = "codex"
    /\ registry' = "pal"
    /\ detached' = TRUE
    /\ UNCHANGED <<oldCall, newCall, failedCodex, oldCompleted>>

CompleteOld ==
    /\ oldCall # NoCall
    /\ ~oldCompleted
    /\ oldCompleted' = TRUE
    /\ UNCHANGED <<registry, oldCall, newCall, failedCodex, detached>>

StartNew ==
    /\ newCall = NoCall
    /\ (detached \/ failedCodex = 2)
    /\ newCall' = HarnessFor(registry, failedCodex)
    /\ UNCHANGED <<registry, oldCall, failedCodex, oldCompleted, detached>>

Next ==
    \/ AttachCodex
    \/ StartOld
    \/ FailCodex
    \/ DetachCodex
    \/ CompleteOld
    \/ StartNew

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ registry \in {"pal", "codex"}
    /\ oldCall \in {NoCall, "pal", "codex"}
    /\ newCall \in {NoCall, "pal", "codex"}
    /\ failedCodex \in 0..2
    /\ oldCompleted \in BOOLEAN
    /\ detached \in BOOLEAN

DetachedNewCallsUsePal ==
    detached /\ newCall # NoCall => newCall = "pal"

BoundedFailureFallsBack ==
    failedCodex = 2 /\ newCall # NoCall => newCall = "pal"

=============================================================================
