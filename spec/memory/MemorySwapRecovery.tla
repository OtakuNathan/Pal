---------------- MODULE MemorySwapRecovery ----------------
EXTENDS Naturals, TLC, MemorySwapReducer

CONSTANTS UnsafeAutoResume, UnsafeRequireBackend, RequireWitness
VARIABLES phase, current, paused, running, backend, crashed,
          cancelled, scannedPaused, restarted, resumed, staleBackendConsulted
vars == <<phase, current, paused, running, backend, crashed,
          cancelled, scannedPaused, restarted, resumed, staleBackendConsulted>>
Facts == <<current, FALSE, FALSE, current /\ phase = "indexing", FALSE>>
Decision(pause) == CHOOSE decision \in {"ignore", "supersede", "run"}:
    DecideLaunch(phase, Facts, pause, decision)

Init == /\ phase \in {"preparing", "indexing"} /\ current = TRUE
        /\ paused = FALSE /\ running = TRUE /\ backend = TRUE /\ crashed = FALSE
        /\ cancelled = FALSE /\ scannedPaused = FALSE /\ restarted = FALSE
        /\ resumed = FALSE /\ staleBackendConsulted = FALSE

Cancel == /\ ~crashed /\ ~cancelled /\ paused' = TRUE /\ running' = FALSE /\ cancelled' = TRUE
          /\ UNCHANGED <<phase, current, backend, crashed, scannedPaused, restarted, resumed, staleBackendConsulted>>

Replaced == /\ current /\ ~running /\ current' = FALSE /\ backend' = FALSE
            /\ UNCHANGED <<phase, paused, running, crashed, cancelled, scannedPaused, restarted, resumed, staleBackendConsulted>>

Launch(explicit) ==
    LET decision == Decision(IF explicit \/ UnsafeAutoResume THEN FALSE ELSE paused)
        needsBackend == decision = "run" \/ (UnsafeRequireBackend /\ phase \in {"preparing", "indexing"})
    IN /\ ~crashed /\ ~running
       /\ phase' = IF decision = "supersede" THEN "superseded" ELSE phase
       /\ running' = (decision = "run" /\ backend)
       /\ paused' = IF decision = "supersede" \/ (decision = "run" /\ backend) THEN FALSE ELSE paused
       /\ scannedPaused' = (scannedPaused \/ (~explicit /\ paused /\ restarted))
       /\ resumed' = (resumed \/ (explicit /\ paused /\ decision = "run" /\ backend))
       /\ staleBackendConsulted' = (staleBackendConsulted \/ (~backend /\ needsBackend))
       /\ UNCHANGED <<current, backend, crashed, cancelled, restarted>>

Finish == /\ running /\ phase = "indexing" /\ ~crashed
          /\ Step(phase, <<FALSE, TRUE, FALSE, current, TRUE>>, "indexed", "complete")
          /\ phase' = "complete" /\ running' = FALSE
          /\ UNCHANGED <<current, paused, backend, crashed, cancelled, scannedPaused, restarted, resumed, staleBackendConsulted>>

Crash == /\ ~crashed /\ ~restarted /\ crashed' = TRUE /\ running' = FALSE
         /\ UNCHANGED <<phase, current, paused, backend, cancelled, scannedPaused, restarted, resumed, staleBackendConsulted>>
Restart == /\ crashed /\ crashed' = FALSE /\ restarted' = TRUE
           /\ UNCHANGED <<phase, current, paused, running, backend, cancelled, scannedPaused, resumed, staleBackendConsulted>>

Next == Cancel \/ Replaced \/ Launch(FALSE) \/ Launch(TRUE) \/ Finish \/ Crash \/ Restart \/ UNCHANGED vars
TypeOK == /\ phase \in Phases
          /\ \A flag \in {current, paused, running, backend, crashed, cancelled,
                           scannedPaused, restarted, resumed, staleBackendConsulted}: flag \in BOOLEAN
PausedStaysStopped == /\ paused => ~running
                     /\ (cancelled /\ ~resumed) => ~running
StaleNeedsNoBackend == ~staleBackendConsulted
ResumeWitness == ~RequireWitness \/ ~(cancelled /\ scannedPaused /\ restarted /\ resumed /\ running)
Spec == Init /\ [][Next]_vars
=============================================================================
