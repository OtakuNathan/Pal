--------------------- MODULE OperatorTriageRecovery ---------------------
EXTENDS Naturals

\* Bounded module-verifier slice of machines.py and AssignmentIdentity:
\* generation = verifier_evaluation_generation (not role/architecture generation),
\* pending = pending_verification_ref, receipts = immutable settled submissions.
\* FinalizeSnapshot publishes terminal verification_artifact_ref and remembers
\* REVIEW_SNAPSHOTTING as the triage resume boundary. Interrupted snapshots
\* have no terminal verdict and must keep their original pending pointer.
CONSTANTS MaxGeneration, MaxFence, AllowOldGenerationReuse

States == {"ReviewQueued", "Verifying", "Snapshotting", "Triage", "Accepted"}
Verdicts == {"PASS", "UNKNOWN"}
NoReceipt == "NoReceipt"
ReceiptType == [generation : 0..MaxGeneration, fence : 1..MaxFence,
                verdict : Verdicts, candidate : {"candidate"},
                session : {"verifier-session"}, policy : {"policy"}]
DraftType == [generation : 0..MaxGeneration, fence : 1..MaxFence]

VARIABLES node, receipts, drafts, consumed, operatorResolutions
vars == <<node, receipts, drafts, consumed, operatorResolutions>>

Init ==
    /\ node = [state |-> "ReviewQueued", generation |-> 0, fence |-> 0,
                pending |-> NoReceipt, blocker |-> "None",
                candidate |-> "candidate", session |-> "verifier-session",
                policy |-> "policy", architectureGeneration |-> 7]
    /\ receipts = {}
    /\ drafts = {}
    /\ consumed = {}
    /\ operatorResolutions = {}

StartVerifier ==
    /\ node.state = "ReviewQueued"
    /\ node.fence < MaxFence
    /\ node' = [node EXCEPT !.state = "Verifying", !.fence = @ + 1]
    /\ drafts' = drafts \cup {
        [generation |-> node.generation, fence |-> node.fence + 1]}
    /\ UNCHANGED <<receipts, consumed, operatorResolutions>>

RecordSubmission(verdict, generation, token) ==
    /\ node.state = "Verifying"
    /\ generation = node.generation
    /\ token = node.fence
    /\ LET receipt == [generation |-> generation, fence |-> token,
                        verdict |-> verdict, candidate |-> node.candidate,
                        session |-> node.session, policy |-> node.policy]
       IN /\ node' = [node EXCEPT !.state = "Snapshotting", !.pending = receipt]
          /\ receipts' = receipts \cup {receipt}
    \* The terminal receipt and sealed draft outlive the pending snapshot pointer.
    /\ UNCHANGED <<drafts, consumed, operatorResolutions>>

RejectStaleSubmission(generation, token) ==
    /\ node.state = "Verifying"
    /\ generation # node.generation \/ token # node.fence
    /\ UNCHANGED vars

LoseVerifierAttempt ==
    /\ node.state = "Verifying"
    /\ node' = [node EXCEPT !.state = "ReviewQueued"]
    \* Process recovery within an evaluation is not terminal-UNKNOWN recovery.
    /\ UNCHANGED <<receipts, drafts, consumed, operatorResolutions>>

ReuseSettledReceipt(receipt) ==
    /\ node.state = "ReviewQueued"
    /\ receipt \in receipts
    /\ receipt.candidate = node.candidate
    /\ receipt.session = node.session
    /\ receipt.policy = node.policy
    /\ receipt.generation = node.generation \/ AllowOldGenerationReuse
    /\ node' = [node EXCEPT !.state = "Snapshotting", !.pending = receipt]
    /\ UNCHANGED <<receipts, drafts, consumed, operatorResolutions>>

FinalizeSnapshot ==
    /\ node.state = "Snapshotting"
    /\ node.pending \in receipts
    /\ node.pending.generation = node.generation
    /\ node' = [node EXCEPT
        !.state = IF node.pending.verdict = "UNKNOWN" THEN "Triage" ELSE "Accepted",
        !.blocker = IF node.pending.verdict = "UNKNOWN" THEN "BlockingUnknown" ELSE "None"]
    /\ consumed' = consumed \cup {
        [receipt |-> node.pending, evaluation |-> node.generation]}
    /\ UNCHANGED <<receipts, drafts, operatorResolutions>>

InterruptSnapshot ==
    /\ node.state = "Snapshotting"
    /\ node' = [node EXCEPT !.state = "Triage", !.blocker = "Interrupted"]
    /\ UNCHANGED <<receipts, drafts, consumed, operatorResolutions>>

ResolveInterruptedSnapshot ==
    /\ node.state = "Triage"
    /\ node.blocker = "Interrupted"
    /\ node' = [node EXCEPT !.state = "Snapshotting", !.blocker = "None"]
    \* Preserve the original pending receipt, generation, draft, and fence.
    /\ UNCHANGED <<receipts, drafts, consumed, operatorResolutions>>

ResolveTerminalUnknown ==
    /\ node.state = "Triage"
    /\ node.blocker = "BlockingUnknown"
    /\ node.generation < MaxGeneration
    /\ node' = [node EXCEPT !.state = "ReviewQueued",
        !.generation = @ + 1, !.pending = NoReceipt, !.blocker = "None"]
    /\ operatorResolutions' = operatorResolutions \cup {node.generation + 1}
    \* This models only explicit RESOLVE_TRIAGE. No background retry, waiver,
    \* Candidate edit, role-session reset, or architecture-generation bump.
    /\ UNCHANGED <<receipts, drafts, consumed>>

Next ==
    \/ StartVerifier
    \/ \E verdict \in Verdicts, generation \in 0..MaxGeneration,
           token \in 0..MaxFence : RecordSubmission(verdict, generation, token)
    \/ \E generation \in 0..MaxGeneration, token \in 0..MaxFence :
        RejectStaleSubmission(generation, token)
    \/ LoseVerifierAttempt
    \/ \E receipt \in receipts : ReuseSettledReceipt(receipt)
    \/ FinalizeSnapshot
    \/ InterruptSnapshot
    \/ ResolveInterruptedSnapshot
    \/ ResolveTerminalUnknown

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ node \in [state : States, generation : 0..MaxGeneration,
        fence : 0..MaxFence, pending : ReceiptType \cup {NoReceipt},
        blocker : {"None", "Interrupted", "BlockingUnknown"},
        candidate : {"candidate"}, session : {"verifier-session"},
        policy : {"policy"}, architectureGeneration : {7}]
    /\ receipts \subseteq ReceiptType
    /\ drafts \subseteq DraftType
    /\ consumed \subseteq [receipt : ReceiptType, evaluation : 0..MaxGeneration]
    /\ operatorResolutions \subseteq 1..MaxGeneration

TerminalUnknownHasSettledEvidence ==
    node.state = "Triage" /\ node.blocker = "BlockingUnknown" =>
        /\ node.pending \in receipts
        /\ node.pending.verdict = "UNKNOWN"
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
    node.state = "Snapshotting" => node.pending \in receipts

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
    [][node'.generation # node.generation => ResolveTerminalUnknown]_vars

TerminalUnknownWaitsForOperator ==
    [][(node.state = "Triage" /\ node.blocker = "BlockingUnknown"
        /\ node'.state # "Triage") => ResolveTerminalUnknown]_vars

InterruptedSnapshotReplaysExactly ==
    [][ResolveInterruptedSnapshot =>
        /\ node'.pending = node.pending
        /\ node'.generation = node.generation
        /\ node'.fence = node.fence
        /\ UNCHANGED <<receipts, drafts>>]_vars

OperatorRetryPreservesInputsAndHistory ==
    [][ResolveTerminalUnknown =>
        /\ node'.candidate = node.candidate
        /\ node'.session = node.session
        /\ node'.policy = node.policy
        /\ node'.architectureGeneration = node.architectureGeneration
        /\ UNCHANGED <<receipts, drafts>>]_vars

NewReceiptRequiresCurrentFence ==
    [][\A receipt \in receipts' \ receipts :
        /\ receipt.fence = node.fence
        /\ receipt.generation = node.generation]_vars

=============================================================================
