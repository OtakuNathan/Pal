-------------------------- MODULE L1TurnLifecycle --------------------------
EXTENDS Naturals, FiniteSets

CONSTANT Calls

VARIABLES turnState,
          calls,
          results,
          resultBodies,
          receipts,
          pagers,
          drafts,
          completeDrafts,
          executable,
          reasoning,
          replay,
          assistantClosure

vars == <<turnState, calls, results, resultBodies, receipts, pagers,
          drafts, completeDrafts, executable, reasoning, replay,
          assistantClosure>>

ClosedStates == {"settled", "interrupted", "aborted"}

Init ==
    /\ turnState = "idle"
    /\ calls = {}
    /\ results = {}
    /\ resultBodies = {}
    /\ receipts = {}
    /\ pagers = {}
    /\ drafts = {}
    /\ completeDrafts = {}
    /\ executable = {}
    /\ reasoning = FALSE
    /\ replay = FALSE
    /\ assistantClosure = FALSE

Begin ==
    /\ turnState = "idle"
    /\ turnState' = "active"
    /\ assistantClosure' = FALSE
    /\ UNCHANGED <<calls, results, resultBodies, receipts, pagers, drafts,
                    completeDrafts, executable, reasoning, replay>>

AppendReasoning ==
    /\ turnState = "active"
    /\ ~reasoning
    /\ reasoning' = TRUE
    /\ replay' = TRUE
    /\ UNCHANGED <<turnState, calls, results, resultBodies, receipts,
                    pagers, drafts, completeDrafts, executable,
                    assistantClosure>>

StartToolDraft ==
    /\ turnState = "active"
    /\ \E call \in Calls \ (calls \cup drafts):
        /\ drafts' = drafts \cup {call}
        /\ UNCHANGED <<turnState, calls, results, resultBodies, receipts,
                        pagers, completeDrafts, executable, reasoning,
                        replay, assistantClosure>>

CompleteToolDraft ==
    /\ turnState = "active"
    /\ \E call \in drafts \ completeDrafts:
        /\ completeDrafts' = completeDrafts \cup {call}
        /\ UNCHANGED <<turnState, calls, results, resultBodies, receipts,
                        pagers, drafts, executable, reasoning, replay,
                        assistantClosure>>

SuccessfulTerminal ==
    /\ turnState = "active"
    /\ drafts # {}
    /\ drafts = completeDrafts
    /\ calls' = calls \cup drafts
    /\ executable' = executable \cup drafts
    /\ drafts' = {}
    /\ completeDrafts' = {}
    /\ UNCHANGED <<turnState, results, resultBodies, receipts, pagers,
                    reasoning, replay, assistantClosure>>

LengthOrBrokenTerminal ==
    /\ turnState = "active"
    /\ drafts # {}
    /\ drafts' = {}
    /\ completeDrafts' = {}
    /\ UNCHANGED <<turnState, calls, results, resultBodies, receipts,
                    pagers, executable, reasoning, replay,
                    assistantClosure>>

AppendToolResult ==
    /\ turnState = "active"
    /\ \E call \in calls \ results:
        /\ results' = results \cup {call}
        /\ resultBodies' = resultBodies \cup {call}
        /\ UNCHANGED <<turnState, calls, receipts, pagers, drafts,
                        completeDrafts, executable, reasoning, replay,
                        assistantClosure>>

AttachPager ==
    /\ turnState = "active"
    /\ \E call \in results \ pagers:
        /\ pagers' = pagers \cup {call}
        /\ UNCHANGED <<turnState, calls, results, resultBodies, receipts,
                        drafts, completeDrafts, executable, reasoning,
                        replay, assistantClosure>>

ExpirePager ==
    /\ turnState # "idle"
    /\ \E call \in pagers:
        /\ pagers' = pagers \ {call}
        /\ UNCHANGED <<turnState, calls, results, resultBodies, receipts,
                        drafts, completeDrafts, executable, reasoning,
                        replay, assistantClosure>>

Settle ==
    /\ turnState = "active"
    /\ calls = results
    /\ drafts = {}
    /\ turnState' = "settled"
    /\ resultBodies' = {}
    /\ receipts' = results
    /\ reasoning' = FALSE
    /\ replay' = FALSE
    /\ executable' = {}
    /\ assistantClosure' = TRUE
    /\ UNCHANGED <<calls, results, pagers, drafts, completeDrafts>>

Interrupt ==
    /\ turnState = "active"
    /\ turnState' = "interrupted"
    /\ calls' = results
    /\ resultBodies' = {}
    /\ receipts' = results
    /\ drafts' = {}
    /\ completeDrafts' = {}
    /\ executable' = {}
    /\ reasoning' = FALSE
    /\ replay' = FALSE
    /\ assistantClosure' = TRUE
    /\ UNCHANGED <<results, pagers>>

Abort ==
    /\ turnState = "active"
    /\ turnState' = "aborted"
    /\ calls' = results
    /\ resultBodies' = {}
    /\ receipts' = results
    /\ drafts' = {}
    /\ completeDrafts' = {}
    /\ executable' = {}
    /\ reasoning' = FALSE
    /\ replay' = FALSE
    /\ assistantClosure' = TRUE
    /\ UNCHANGED <<results, pagers>>

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
     \/ AttachPager
     \/ ExpirePager
     \/ Settle
     \/ Interrupt
     \/ Abort
     \/ Closed

TypeOK ==
    /\ turnState \in {"idle", "active", "settled", "interrupted", "aborted"}
    /\ calls \subseteq Calls
    /\ results \subseteq Calls
    /\ resultBodies \subseteq Calls
    /\ receipts \subseteq Calls
    /\ pagers \subseteq Calls
    /\ drafts \subseteq Calls
    /\ completeDrafts \subseteq Calls
    /\ executable \subseteq Calls
    /\ reasoning \in BOOLEAN
    /\ replay \in BOOLEAN
    /\ assistantClosure \in BOOLEAN

ResultsConsumeCalls == results \subseteq calls
CompleteDraftsAreDrafts == completeDrafts \subseteq drafts
DraftsAreNotExecutable == (drafts \cap executable) = {}
ExecutableCallsWerePromoted == executable \subseteq calls
ResultBodiesAreResults == resultBodies \subseteq results
ReceiptsAreResults == receipts \subseteq results
PagersAreResults == pagers \subseteq results
FullResultsAreActive == resultBodies # {} => turnState = "active"

ClosedProtocol ==
    turnState \in ClosedStates =>
        /\ calls = results
        /\ drafts = {}
        /\ completeDrafts = {}
        /\ executable = {}
        /\ resultBodies = {}
        /\ receipts = results
        /\ assistantClosure

ClosedRetiresPrivateState ==
    turnState \in ClosedStates => ~reasoning /\ ~replay

=============================================================================
