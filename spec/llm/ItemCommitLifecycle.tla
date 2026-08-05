------------------------- MODULE ItemCommitLifecycle -------------------------
EXTENDS Naturals, FiniteSets

CONSTANTS Items, ToolItems

VARIABLES streamState,
          drafts,
          committed,
          executionQueue,
          executed,
          results,
          terminalProjection,
          terminalReason

vars == <<streamState, drafts, committed, executionQueue, executed, results,
          terminalProjection, terminalReason>>

Init ==
    /\ streamState = "open"
    /\ drafts = {}
    /\ committed = {}
    /\ executionQueue = {}
    /\ executed = {}
    /\ results = {}
    /\ terminalProjection = {}
    /\ terminalReason = "none"

StartItem ==
    /\ streamState = "open"
    /\ \E item \in Items \ (drafts \cup committed):
        /\ drafts' = drafts \cup {item}
        /\ UNCHANGED <<streamState, committed, executionQueue, executed,
                        results, terminalProjection, terminalReason>>

CommitItem ==
    /\ streamState = "open"
    /\ \E item \in drafts:
        /\ drafts' = drafts \ {item}
        /\ committed' = committed \cup {item}
        /\ UNCHANGED <<streamState, executionQueue, executed, results,
                        terminalProjection, terminalReason>>

ReachTerminal(reason) ==
    /\ streamState = "open"
    /\ reason \in {"stop", "length", "error", "eof"}
    /\ streamState' = "terminal"
    /\ terminalReason' = reason
    /\ drafts' = {}
    /\ executionQueue' = IF reason = "error" THEN {} ELSE committed \cap ToolItems
    /\ terminalProjection' = IF reason = "error" THEN {} ELSE committed
    /\ UNCHANGED <<committed, executed, results>>

ExecuteCommitted ==
    /\ streamState = "terminal"
    /\ \E item \in executionQueue \ executed:
        /\ executed' = executed \cup {item}
        /\ UNCHANGED <<streamState, drafts, committed, executionQueue,
                        results, terminalProjection, terminalReason>>

AppendResult ==
    /\ streamState = "terminal"
    /\ \E item \in executed \ results:
        /\ results' = results \cup {item}
        /\ UNCHANGED <<streamState, drafts, committed, executionQueue,
                        executed, terminalProjection, terminalReason>>

Closed ==
    /\ streamState = "terminal"
    /\ executionQueue = results
    /\ UNCHANGED vars

Next == StartItem
     \/ CommitItem
     \/ \E reason \in {"stop", "length", "error", "eof"}: ReachTerminal(reason)
     \/ ExecuteCommitted
     \/ AppendResult
     \/ Closed

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ streamState \in {"open", "terminal"}
    /\ drafts \subseteq Items
    /\ committed \subseteq Items
    /\ executionQueue \subseteq ToolItems
    /\ executed \subseteq ToolItems
    /\ results \subseteq ToolItems
    /\ terminalProjection \subseteq Items
    /\ terminalReason \in {"none", "stop", "length", "error", "eof"}

OpenDraftsAreNotCommitted == drafts \cap committed = {}
OnlyCommittedToolsQueue == executionQueue \subseteq committed \cap ToolItems
ExecutionWaitsForTerminal == executed # {} => streamState = "terminal"
ExecutedAtMostOnce == executed \subseteq executionQueue
ResultsCloseExecutedCalls == results \subseteq executed
LengthPreservesCommitted ==
    terminalReason = "length" => executionQueue = committed \cap ToolItems
ErrorDoesNotExecute == terminalReason = "error" => executionQueue = {}
TerminalDropsOpenDrafts == streamState = "terminal" => drafts = {}
SuccessfulTerminalPreservesCommitted ==
    terminalReason \in {"stop", "length", "eof"} => committed \subseteq terminalProjection

=============================================================================
