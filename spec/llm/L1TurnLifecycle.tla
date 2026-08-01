-------------------------- MODULE L1TurnLifecycle --------------------------
EXTENDS Naturals, FiniteSets

CONSTANT Calls

VARIABLES turnState,
          calls,
          results,
          drafts,
          completeDrafts,
          executable,
          reasoning,
          replay

vars == <<turnState, calls, results, drafts, completeDrafts, executable,
          reasoning, replay>>

ClosedStates == {"settled", "interrupted", "aborted"}

Init ==
    /\ turnState = "idle"
    /\ calls = {}
    /\ results = {}
    /\ drafts = {}
    /\ completeDrafts = {}
    /\ executable = {}
    /\ reasoning = FALSE
    /\ replay = FALSE

Begin ==
    /\ turnState = "idle"
    /\ turnState' = "active"
    /\ UNCHANGED <<calls, results, drafts, completeDrafts, executable,
                    reasoning, replay>>

AppendReasoning ==
    /\ turnState = "active"
    /\ ~reasoning
    /\ reasoning' = TRUE
    /\ replay' = TRUE
    /\ UNCHANGED <<turnState, calls, results, drafts, completeDrafts,
                    executable>>

StartToolDraft ==
    /\ turnState = "active"
    /\ \E call \in Calls \ (calls \cup drafts):
        /\ drafts' = drafts \cup {call}
        /\ UNCHANGED <<turnState, calls, results, completeDrafts,
                        executable, reasoning, replay>>

CompleteToolDraft ==
    /\ turnState = "active"
    /\ \E call \in drafts \ completeDrafts:
        /\ completeDrafts' = completeDrafts \cup {call}
        /\ UNCHANGED <<turnState, calls, results, drafts, executable,
                        reasoning, replay>>

SuccessfulTerminal ==
    /\ turnState = "active"
    /\ drafts # {}
    /\ drafts = completeDrafts
    /\ calls' = calls \cup drafts
    /\ executable' = executable \cup drafts
    /\ drafts' = {}
    /\ completeDrafts' = {}
    /\ UNCHANGED <<turnState, results, reasoning, replay>>

LengthOrBrokenTerminal ==
    /\ turnState = "active"
    /\ drafts # {}
    /\ drafts' = {}
    /\ completeDrafts' = {}
    /\ UNCHANGED <<turnState, calls, results, executable, reasoning, replay>>

AppendToolResult ==
    /\ turnState = "active"
    /\ \E call \in calls \ results:
        /\ results' = results \cup {call}
        /\ UNCHANGED <<turnState, calls, drafts, completeDrafts, executable,
                        reasoning, replay>>

Settle ==
    /\ turnState = "active"
    /\ calls = results
    /\ drafts = {}
    /\ turnState' = "settled"
    /\ reasoning' = FALSE
    /\ replay' = FALSE
    /\ executable' = {}
    /\ UNCHANGED <<calls, results, drafts, completeDrafts>>

Interrupt ==
    /\ turnState = "active"
    /\ turnState' = "interrupted"
    /\ calls' = results
    /\ drafts' = {}
    /\ completeDrafts' = {}
    /\ executable' = {}
    /\ reasoning' = FALSE
    /\ replay' = FALSE
    /\ UNCHANGED results

Abort ==
    /\ turnState = "active"
    /\ turnState' = "aborted"
    /\ calls' = results
    /\ drafts' = {}
    /\ completeDrafts' = {}
    /\ executable' = {}
    /\ reasoning' = FALSE
    /\ replay' = FALSE
    /\ UNCHANGED results

Closed ==
    /\ turnState \in ClosedStates
    /\ UNCHANGED vars

Next == Begin
     \/ AppendReasoning
     \/ StartToolDraft
     \/ CompleteToolDraft
     \/ SuccessfulTerminal
     \/ LengthOrBrokenTerminal
     \/ AppendToolResult
     \/ Settle
     \/ Interrupt
     \/ Abort
     \/ Closed

TypeOK ==
    /\ turnState \in {"idle", "active", "settled", "interrupted", "aborted"}
    /\ calls \subseteq Calls
    /\ results \subseteq Calls
    /\ drafts \subseteq Calls
    /\ completeDrafts \subseteq Calls
    /\ executable \subseteq Calls
    /\ reasoning \in BOOLEAN
    /\ replay \in BOOLEAN

ResultsConsumeCalls == results \subseteq calls
CompleteDraftsAreDrafts == completeDrafts \subseteq drafts
DraftsAreNotExecutable == (drafts \cap executable) = {}
ExecutableCallsWerePromoted == executable \subseteq calls

ClosedProtocol ==
    turnState \in ClosedStates =>
        /\ calls = results
        /\ drafts = {}
        /\ completeDrafts = {}
        /\ executable = {}

ClosedRetiresPrivateState ==
    turnState \in ClosedStates => ~reasoning /\ ~replay

=============================================================================
