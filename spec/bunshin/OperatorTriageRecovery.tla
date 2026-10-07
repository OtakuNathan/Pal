--------------------- MODULE OperatorTriageRecovery ---------------------
EXTENDS Naturals, FiniteSets

\* Bounded module-verifier slice of machines.py and AssignmentIdentity:
\* generation = verifier_evaluation_generation (not role/architecture generation),
\* pending = pending_verification_ref, receipts = immutable settled submissions.
\* FinalizeSnapshot publishes terminal verification_artifact_ref and remembers
\* REVIEW_SNAPSHOTTING as the triage resume boundary. Interrupted snapshots
\* have no terminal verdict and must keep their original pending pointer.
\* checkerState is the GraphExecution logical cursor, not a running process.
\* Aggregate Verifying/Quiescing/Snapshotting all refine to graph Checking.
\* Existing START_CHECKER(RESUME, pending fingerprint) restores settlement
\* authority after triage; only StartVerifier launches another invocation.
\* node.fence abstracts verifier-invocation fences, not Manager snapshot-rebind
\* lease tokens. Reacquiring a quiesce/snapshot lease creates no verifier
\* invocation and is abstracted away from this counter.
CONSTANTS MaxGeneration, MaxFence, AllowOldGenerationReuse

States == {"ReviewQueued", "Verifying", "Quiescing", "Snapshotting", "Triage", "Accepted"}
SnapshotBoundaries == {"Quiescing", "Snapshotting"}
CheckerStates == {"CheckerReady", "Checking", "TriageRequired", "Accepted"}
\* INVALID abstracts an already exhausted invalid-submission correction budget.
\* GraphExecutionLifecycle models the individual bounded correction attempts.
Verdicts == {"PASS", "UNKNOWN", "INVALID"}
NoReceipt == "NoReceipt"
ReceiptType == [generation : 0..MaxGeneration, fence : 1..MaxFence,
                verdict : Verdicts, candidate : {"candidate"},
                session : {"verifier-session"}, policy : {"policy"}]
DraftType == [generation : 0..MaxGeneration, fence : 1..MaxFence]

VARIABLES node, receipts, drafts, consumed, operatorResolutions
VARIABLES checkerState, checkerKind, checkerInput, processInvocations
vars == <<node, receipts, drafts, consumed, operatorResolutions,
          checkerState, checkerKind, checkerInput, processInvocations>>

ValidPendingReceipt ==
    /\ node.pending \in receipts
    /\ node.pending.generation = node.generation
    /\ node.pending.candidate = node.candidate
    /\ node.pending.session = node.session
    /\ node.pending.policy = node.policy

Init ==
    /\ node = [state |-> "ReviewQueued", generation |-> 0, fence |-> 0,
                pending |-> NoReceipt, blocker |-> "None",
                resumeBoundary |-> "None",
                candidate |-> "candidate", session |-> "verifier-session",
                policy |-> "policy", architectureGeneration |-> 7]
    /\ receipts = {}
    /\ drafts = {}
    /\ consumed = {}
    /\ operatorResolutions = {}
    /\ checkerState = "CheckerReady"
    /\ checkerKind = "None"
    /\ checkerInput = NoReceipt
    /\ processInvocations = 0

StartVerifier ==
    /\ node.state = "ReviewQueued"
    /\ checkerState = "CheckerReady"
    /\ node.pending = NoReceipt
    /\ node.fence < MaxFence
    /\ node' = [node EXCEPT !.state = "Verifying", !.fence = @ + 1]
    /\ drafts' = drafts \cup {
        [generation |-> node.generation, fence |-> node.fence + 1]}
    /\ checkerState' = "Checking"
    /\ checkerKind' = "Initial"
    /\ checkerInput' = NoReceipt
    /\ processInvocations' = processInvocations + 1
    /\ UNCHANGED <<receipts, consumed, operatorResolutions>>

RecordSubmission(verdict, generation, token) ==
    /\ node.state = "Verifying"
    /\ generation = node.generation
    /\ token = node.fence
    /\ LET receipt == [generation |-> generation, fence |-> token,
                        verdict |-> verdict, candidate |-> node.candidate,
                        session |-> node.session, policy |-> node.policy]
       IN /\ node' = [node EXCEPT !.state = "Quiescing", !.pending = receipt]
          /\ receipts' = receipts \cup {receipt}
    \* The terminal receipt and sealed draft outlive the pending snapshot pointer.
    /\ UNCHANGED <<drafts, consumed, operatorResolutions>>
    /\ UNCHANGED <<checkerState, checkerKind, checkerInput, processInvocations>>

QuiesceVerifier ==
    /\ node.state = "Quiescing"
    /\ ValidPendingReceipt
    /\ checkerState = "Checking"
    /\ node' = [node EXCEPT !.state = "Snapshotting"]
    /\ UNCHANGED <<receipts, drafts, consumed, operatorResolutions,
                    checkerState, checkerKind, checkerInput, processInvocations>>

RejectStaleSubmission(generation, token) ==
    /\ node.state = "Verifying"
    /\ generation # node.generation \/ token # node.fence
    /\ UNCHANGED vars

LoseVerifierAttempt ==
    /\ node.state = "Verifying"
    /\ node' = [node EXCEPT !.state = "ReviewQueued"]
    /\ checkerState' = "CheckerReady"
    /\ checkerKind' = "None"
    /\ checkerInput' = NoReceipt
    \* Process recovery within an evaluation is not terminal-UNKNOWN recovery.
    /\ UNCHANGED <<receipts, drafts, consumed, operatorResolutions>>
    /\ UNCHANGED processInvocations

ReuseSettledReceipt(receipt) ==
    /\ node.state = "ReviewQueued"
    /\ receipt \in receipts
    /\ receipt.candidate = node.candidate
    /\ receipt.session = node.session
    /\ receipt.policy = node.policy
    /\ receipt.generation = node.generation \/ AllowOldGenerationReuse
    /\ node' = [node EXCEPT !.state = "Snapshotting", !.pending = receipt]
    /\ checkerState' = "Checking"
    /\ checkerKind' = "Resume"
    /\ checkerInput' = receipt
    /\ UNCHANGED <<receipts, drafts, consumed, operatorResolutions>>
    /\ UNCHANGED processInvocations

FinalizeSnapshot ==
    /\ node.state = "Snapshotting"
    /\ ValidPendingReceipt
    /\ checkerState = "Checking"
    /\ checkerKind = "Initial" \/ (checkerKind = "Resume" /\ checkerInput = node.pending)
    /\ node' = [node EXCEPT
        !.state = IF node.pending.verdict = "PASS" THEN "Accepted" ELSE "Triage",
        !.blocker = IF node.pending.verdict = "UNKNOWN" THEN "BlockingUnknown"
                   ELSE IF node.pending.verdict = "INVALID" THEN "InvalidSubmission" ELSE "None",
        !.resumeBoundary = IF node.pending.verdict = "PASS" THEN "None" ELSE "Snapshotting"]
    /\ checkerState' = IF node.pending.verdict = "PASS" THEN "Accepted" ELSE "TriageRequired"
    /\ checkerKind' = "None"
    /\ checkerInput' = NoReceipt
    /\ consumed' = consumed \cup {
        [receipt |-> node.pending, evaluation |-> node.generation]}
    /\ UNCHANGED <<receipts, drafts, operatorResolutions>>
    /\ UNCHANGED processInvocations

InterruptSnapshot ==
    /\ node.state \in SnapshotBoundaries
    /\ node' = [node EXCEPT !.state = "Triage", !.blocker = "Interrupted",
                !.resumeBoundary = node.state]
    /\ checkerState' = "TriageRequired"
    /\ checkerKind' = "None"
    /\ checkerInput' = NoReceipt
    /\ UNCHANGED <<receipts, drafts, consumed, operatorResolutions>>
    /\ UNCHANGED processInvocations

ResolveInterruptedSnapshot ==
    /\ node.state = "Triage"
    /\ node.blocker = "Interrupted"
    /\ node.resumeBoundary \in SnapshotBoundaries
    /\ ValidPendingReceipt
    /\ [receipt |-> node.pending, evaluation |-> node.generation] \notin consumed
    /\ checkerState = "TriageRequired"
    /\ node' = [node EXCEPT !.state = node.resumeBoundary, !.blocker = "None",
                !.resumeBoundary = "None"]
    \* One atomic public resolution: graph RESOLVE_TRIAGE first yields READY,
    \* then existing START_CHECKER with AssignmentKind.RESUME yields CHECKING.
    \* checkerInput denotes pending-checker-settlement:<immutable pending hash>.
    /\ checkerState' = "Checking"
    /\ checkerKind' = "Resume"
    /\ checkerInput' = node.pending
    \* Preserve the original pending receipt, generation, draft, and fence.
    /\ UNCHANGED <<receipts, drafts, consumed, operatorResolutions>>
    /\ UNCHANGED processInvocations

ResolveTerminalBlocker(blocker) ==
    /\ node.state = "Triage"
    /\ node.blocker = blocker
    /\ blocker \in {"BlockingUnknown", "InvalidSubmission"}
    /\ node.generation < MaxGeneration
    /\ node' = [node EXCEPT !.state = "ReviewQueued",
        !.generation = @ + 1, !.pending = NoReceipt, !.blocker = "None",
        !.resumeBoundary = "None"]
    /\ checkerState' = "CheckerReady"
    /\ checkerKind' = "None"
    /\ checkerInput' = NoReceipt
    /\ operatorResolutions' = operatorResolutions \cup {node.generation + 1}
    \* This models only explicit RESOLVE_TRIAGE. No background retry, waiver,
    \* Candidate edit, role-session reset, or architecture-generation bump.
    /\ UNCHANGED <<receipts, drafts, consumed>>
    /\ UNCHANGED processInvocations

ResolveTerminalUnknown == ResolveTerminalBlocker("BlockingUnknown")
ResolveTerminalCorrection == ResolveTerminalBlocker("InvalidSubmission")

Next ==
    \/ StartVerifier
    \/ \E verdict \in Verdicts, generation \in 0..MaxGeneration,
           token \in 0..MaxFence : RecordSubmission(verdict, generation, token)
    \/ QuiesceVerifier
    \/ \E generation \in 0..MaxGeneration, token \in 0..MaxFence :
        RejectStaleSubmission(generation, token)
    \/ LoseVerifierAttempt
    \/ \E receipt \in receipts : ReuseSettledReceipt(receipt)
    \/ FinalizeSnapshot
    \/ InterruptSnapshot
    \/ ResolveInterruptedSnapshot
    \/ ResolveTerminalUnknown
    \/ ResolveTerminalCorrection

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ node \in [state : States, generation : 0..MaxGeneration,
        fence : 0..MaxFence, pending : ReceiptType \cup {NoReceipt},
        blocker : {"None", "Interrupted", "BlockingUnknown", "InvalidSubmission"},
        resumeBoundary : SnapshotBoundaries \cup {"None"},
        candidate : {"candidate"}, session : {"verifier-session"},
        policy : {"policy"}, architectureGeneration : {7}]
    /\ receipts \subseteq ReceiptType
    /\ drafts \subseteq DraftType
    /\ consumed \subseteq [receipt : ReceiptType, evaluation : 0..MaxGeneration]
    /\ operatorResolutions \subseteq 1..MaxGeneration
    /\ checkerState \in CheckerStates
    /\ checkerKind \in {"None", "Initial", "Resume"}
    /\ checkerInput \in ReceiptType \cup {NoReceipt}
    /\ processInvocations \in 0..MaxFence

TerminalUnknownHasSettledEvidence ==
    node.state = "Triage" /\ node.blocker = "BlockingUnknown" =>
        /\ node.pending \in receipts
        /\ node.pending.verdict = "UNKNOWN"
        /\ [receipt |-> node.pending, evaluation |-> node.generation] \in consumed

TerminalCorrectionHasSettledEvidence ==
    node.state = "Triage" /\ node.blocker = "InvalidSubmission" =>
        /\ node.pending \in receipts
        /\ node.pending.verdict = "INVALID"
        /\ [receipt |-> node.pending, evaluation |-> node.generation] \in consumed

PendingReceiptIsImmutable == node.pending # NoReceipt => node.pending \in receipts

NoOldUnknownReplay ==
    node.pending # NoReceipt => node.pending.generation = node.generation

ConsumedEvidenceIsCurrent ==
    \A entry \in consumed : entry.receipt.generation = entry.evaluation

AcceptedRequiresCurrentPass ==
    node.state = "Accepted" =>
        /\ node.pending \in receipts
        /\ node.pending.verdict = "PASS"
        /\ node.pending.generation = node.generation

SnapshotOwnsPendingReceipt ==
    node.state \in SnapshotBoundaries => node.pending \in receipts

AggregateRefinesLogicalChecker ==
    checkerState = CASE node.state = "ReviewQueued" -> "CheckerReady"
                       [] node.state \in {"Verifying", "Quiescing", "Snapshotting"} -> "Checking"
                       [] node.state = "Triage" -> "TriageRequired"
                       [] node.state = "Accepted" -> "Accepted"

LogicalResumeHasExactPendingCursor ==
    checkerKind = "Resume" =>
        /\ checkerState = "Checking"
        /\ checkerInput = node.pending
        /\ node.pending \in receipts

ReadyHasNoSettlementCursor ==
    checkerState = "CheckerReady" =>
        /\ node.state = "ReviewQueued"
        /\ node.pending = NoReceipt
        /\ checkerInput = NoReceipt

ProcessInvocationAccounting ==
    /\ processInvocations = node.fence
    /\ processInvocations = Cardinality(drafts)

FreshEvaluationHasNoPendingSnapshot ==
    node.state \in {"ReviewQueued", "Verifying"} => node.pending = NoReceipt

EachEvaluationWasOperatorRequested ==
    operatorResolutions = 1..node.generation

ReceiptHasMatchingDraft ==
    \A receipt \in receipts :
        [generation |-> receipt.generation, fence |-> receipt.fence] \in drafts

ReceiptIdentityIsUnique ==
    \A left, right \in receipts :
        (left.generation = right.generation /\ left.fence = right.fence) => left = right

HistoryNeverShrinks ==
    [][receipts \subseteq receipts' /\ drafts \subseteq drafts']_vars

GenerationAdvanceRequiresOperator ==
    [][node'.generation # node.generation =>
        ResolveTerminalUnknown \/ ResolveTerminalCorrection]_vars

TerminalUnknownWaitsForOperator ==
    [][(node.state = "Triage" /\ node.blocker = "BlockingUnknown"
        /\ node'.state # "Triage") => ResolveTerminalUnknown]_vars

TerminalCorrectionWaitsForOperator ==
    [][(node.state = "Triage" /\ node.blocker = "InvalidSubmission"
        /\ node'.state # "Triage") => ResolveTerminalCorrection]_vars

InterruptedSnapshotReplaysExactly ==
    [][ResolveInterruptedSnapshot =>
        /\ [receipt |-> node.pending, evaluation |-> node.generation] \notin consumed
        /\ node'.state = node.resumeBoundary
        /\ node'.pending = node.pending
        /\ node'.generation = node.generation
        /\ node'.fence = node.fence
        /\ checkerState' = "Checking"
        /\ checkerKind' = "Resume"
        /\ checkerInput' = node.pending
        /\ UNCHANGED <<receipts, drafts, processInvocations>>]_vars

OperatorRetryPreservesInputsAndHistory ==
    [][(ResolveTerminalUnknown \/ ResolveTerminalCorrection) =>
        /\ node'.candidate = node.candidate
        /\ node'.session = node.session
        /\ node'.policy = node.policy
        /\ node'.architectureGeneration = node.architectureGeneration
        /\ UNCHANGED <<receipts, drafts>>]_vars

SnapshotSettlementRequiresChecking ==
    [][FinalizeSnapshot =>
        /\ checkerState = "Checking"
        /\ ValidPendingReceipt]_vars

ProcessInvocationRequiresFreshStart ==
    [][processInvocations' # processInvocations => StartVerifier]_vars

FreshStartCreatesNewInvocation ==
    [][StartVerifier => processInvocations' = processInvocations + 1]_vars

TerminalResolutionKeepsCheckerReady ==
    [][(ResolveTerminalUnknown \/ ResolveTerminalCorrection) =>
        /\ checkerState' = "CheckerReady"
        /\ checkerInput' = NoReceipt
        /\ node'.pending = NoReceipt
        /\ UNCHANGED processInvocations]_vars

NewReceiptRequiresCurrentFence ==
    [][\A receipt \in receipts' \ receipts :
        /\ receipt.fence = node.fence
        /\ receipt.generation = node.generation]_vars

=============================================================================
