-------------------- MODULE TaskWorkflowSelection --------------------
EXTENDS Naturals, TLC

CONSTANT RankBeforeLimit
VARIABLES activeCount, restarting, includeTerminal, historyCount
vars == <<activeCount, restarting, includeTerminal, historyCount>>

Min(a, b) == IF a < b THEN a ELSE b
Max(a, b) == IF a > b THEN a ELSE b

\* Abstraction: live workflows are older than the restarting source and all
\* terminal history. Two live rows represent the invariant-violation case.
\* Every history row is archived and newer than the previous history row.
Init == /\ activeCount \in 0..2
        /\ restarting \in BOOLEAN
        /\ includeTerminal \in BOOLEAN
        /\ historyCount = 0
Next == /\ historyCount < 21
        /\ historyCount' = historyCount + 1
        /\ UNCHANGED <<activeCount, restarting, includeTerminal>>

VisibleActive ==
    IF RankBeforeLimit THEN activeCount
    ELSE Min(activeCount, Max(0, 20 -
        (IF includeTerminal THEN historyCount ELSE 0) -
        (IF restarting THEN 1 ELSE 0)))
VisibleRestart == restarting /\ (RankBeforeLimit \/ historyCount < 20)
Selection ==
    IF VisibleActive > 1 THEN "invariant-error"
    ELSE IF VisibleActive = 1 THEN "active"
    ELSE IF includeTerminal /\ VisibleRestart THEN "restarting"
    ELSE IF includeTerminal /\ historyCount > 0 THEN "terminal"
    ELSE "none"

TypeOK == /\ activeCount \in 0..2 /\ restarting \in BOOLEAN
          /\ includeTerminal \in BOOLEAN /\ historyCount \in 0..21
ActiveCannotBeHidden == activeCount = 1 => Selection = "active"
DuplicateCannotBeHidden == activeCount = 2 => Selection = "invariant-error"
RecoveryBeforeHistory ==
    activeCount = 0 /\ restarting /\ includeTerminal => Selection = "restarting"
ControlExcludesFallback ==
    activeCount = 0 /\ ~includeTerminal => Selection = "none"
TerminalFallback ==
    activeCount = 0 /\ ~restarting /\ includeTerminal /\ historyCount > 0
    => Selection = "terminal"
Spec == Init /\ [][Next]_vars
=====================================================================
