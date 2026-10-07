---------------------- MODULE VerifierDraftLifecycle ----------------------
EXTENDS Naturals, TLC

(*
One current verifier assignment, two authoring drafts, one durable submission.
This is a bounded protocol model, not a translation of Python or SQLite.

Refinement boundary (implementation must supply the shared transaction):
  EditFinding       work_items create/update/delete current-owned finding
  EditCase          verification create/update/delete current-owned test case
                    definition (RecordEvidence represents run-and-upsert)
  RecordEvidence    verification execution receipt bound to Candidate/corpus
  PrepareSubmit     role_gateway._draft_submit reads BOTH drafts + evidence
  CommitSubmit      role_submissions.record_role_submission rechecks authority,
                    both versions, corpus/evidence and exact finding projection
                    in the same transaction as durable receipt acceptance
  ProjectSubmitted  submission_drafts.mark_submitted/reconcile projection only
  ReplayReceipt     identical receipt replay; conflicting content is rejected
  ReplayMutation    same operation key + identical request returns its result;
                    same key + different request cannot execute

EditFinding/EditCase/RecordEvidence must check receipt absence in their write
transaction, including Manager mutate_precomputed and local mutation paths.
Checking assignment RUNNING before that transaction is insufficient.

History is outside current-owned CRUD and comes from trusted stored provenance,
never a caller's claimed origin. A completed current finding is STILL a draft.
Owned, same-input, unsubmitted retry copies remain CURRENT and editable; their
archived source is unchanged. A durable receipt makes even an active-looking
source submitted history. RecoverSourceAsCurrent checks this classification.
Versions are monotone finite exploration bounds, not production edit limits.
Corpus may change after submission; sealed evidence remains unchanged and such
a change removes current PASS authority until fresh verification elsewhere.
*)

CONSTANTS MaxVersion, MaxCorpus, Fault

FindingValues == {"absent", "pending-blocking", "completed-blocking",
                  "pending-advisory", "completed-advisory"}
CaseValues == {"absent", "case-a", "case-b"}
History == {<<"inherited", "finding-0", "case-0">>,
            <<"prior-submitted", "finding-1", "case-1">>}
NoEvidence == [generation |-> 0, corpus |-> 0, caseVersion |-> 0, result |-> "none",
               historyPassed |-> FALSE]
EmptySnapshot == [finding |-> "absent", cases |-> "absent",
                  findingVersion |-> 0, verificationVersion |-> 0,
                  generation |-> 1, corpus |-> 0, evidence |-> NoEvidence]
EmptyToken == [actor |-> "verifier", assignment |-> "assignment-0",
               generation |-> 1, fence |-> 1]
EmptyRequest == [token |-> EmptyToken, snapshot |-> EmptySnapshot,
                 verdict |-> "fail"]
EmptyWitness == [authorityMatches |-> TRUE, versionMatches |-> TRUE]
EmptyOperation == [kind |-> "none", value |-> "absent", token |-> EmptyToken]

VARIABLE s
vars == <<s>>

Init == s = [finding |-> "absent", cases |-> "case-a",
             findingVersion |-> 0, verificationVersion |-> 0,
             generation |-> 1, fence |-> 1, corpus |-> 0,
             evidence |-> NoEvidence, history |-> History,
             projection |-> "active", phase |-> "none",
             acceptedCorpus |-> 0,
             prepared |-> FALSE, request |-> EmptyRequest,
             receipt |-> EmptyRequest, witness |-> EmptyWitness,
             operation |-> EmptyOperation]

Authority == [actor |-> "verifier", assignment |-> "assignment-0",
              generation |-> s.generation, fence |-> s.fence]
Tokens == {Authority, [Authority EXCEPT !.actor = "other-role"],
           [Authority EXCEPT !.assignment = "other-assignment"],
           [Authority EXCEPT !.generation = 0],
           [Authority EXCEPT !.fence = 0]}
Snapshot == [finding |-> s.finding, cases |-> s.cases,
             findingVersion |-> s.findingVersion,
             verificationVersion |-> s.verificationVersion,
             generation |-> s.generation, corpus |-> s.corpus,
             evidence |-> s.evidence]
HasReceipt == s.phase # "none"
Authorized(token) == token = Authority \/ Fault = "stale-authority"
Editable == s.projection = "active" /\
            (~HasReceipt \/ Fault = "receipt-blind")
EditWitness(token, expected, version) ==
    [authorityMatches |-> token = Authority, versionMatches |-> expected = version]

EditFinding(token, expected, value, target) ==
    /\ Editable /\ Authorized(token)
    /\ target = "current" \/ Fault = "history-rewrite"
    /\ expected = s.findingVersion /\ expected < MaxVersion
    /\ value \in FindingValues \ {s.finding}
    /\ ~(Fault = "completed-locked" /\
         s.finding \in {"completed-blocking", "completed-advisory"})
    /\ s' = [s EXCEPT !.finding = value, !.findingVersion = @ + 1,
              !.witness = EditWitness(token, expected, s.findingVersion),
              !.history = IF target = "current" THEN @ ELSE {},
              !.operation = IF @.kind # "none" THEN @ ELSE
                  [kind |-> "finding", value |-> value, token |-> token]]

EditCase(token, expected, value) ==
    /\ Editable /\ Authorized(token)
    /\ expected = s.verificationVersion /\ expected < MaxVersion
    /\ value \in CaseValues \ {s.cases}
    /\ s' = [s EXCEPT !.cases = value, !.verificationVersion = @ + 1,
              !.witness = EditWitness(token, expected, s.verificationVersion),
              !.operation = IF @.kind # "none" THEN @ ELSE
                  [kind |-> "case", value |-> value, token |-> token]]

\* A corpus write is not a new execution receipt, even after a durable submit.
EditCorpus ==
    /\ s.corpus < MaxCorpus
    /\ s' = [s EXCEPT !.corpus = @ + 1]

RecordEvidence(token, expected, result, historyPassed) ==
    /\ Editable /\ Authorized(token) /\ s.cases # "absent"
    /\ expected = s.verificationVersion /\ expected < MaxVersion
    /\ result \in {"pass", "fail"} /\ historyPassed \in BOOLEAN
    /\ s' = [s EXCEPT !.verificationVersion = @ + 1,
              !.evidence = [generation |-> s.generation, corpus |-> s.corpus,
                            caseVersion |-> expected + 1,
                            result |-> result, historyPassed |-> historyPassed],
              !.witness = EditWitness(token, expected, s.verificationVersion)]

\* In-flight reads can outlive either an edit or assignment/fence replacement.
PrepareSubmit(token, verdict, proposedFinding) ==
    /\ ~HasReceipt /\ ~s.prepared /\ verdict \in {"pass", "fail"}
    /\ Authorized(token)
    /\ s' = [s EXCEPT !.prepared = TRUE,
              !.request = [token |-> token,
                           snapshot |-> [Snapshot EXCEPT !.finding = proposedFinding],
                           verdict |-> verdict]]

DiscardPrepared ==
    /\ s.prepared /\ ~HasReceipt
    /\ s' = [s EXCEPT !.prepared = FALSE, !.request = EmptyRequest]

CurrentEvidence(snapshot) ==
    /\ snapshot.evidence.generation = snapshot.generation
    /\ snapshot.evidence.corpus = snapshot.corpus
    /\ snapshot.evidence.caseVersion = snapshot.verificationVersion
    /\ snapshot.evidence.result = "pass"
    /\ snapshot.evidence.historyPassed
PassReady(snapshot) ==
    /\ snapshot.finding \in {"absent", "pending-advisory", "completed-advisory"}
    /\ snapshot.cases # "absent"
    /\ CurrentEvidence(snapshot) \/ Fault = "stale-evidence"
VersionsMatch ==
    /\ s.request.snapshot.findingVersion = s.findingVersion
    /\ s.request.snapshot.verificationVersion = s.verificationVersion
    /\ s.request.snapshot.corpus = s.corpus

\* This is the linearization point. It freezes BOTH draft kinds before the
\* submitted projection is repaired. An edit and submit cannot both win with
\* the same versions. No caller-supplied finding omission can alter Snapshot.
CommitSubmit ==
    /\ ~HasReceipt /\ s.prepared
    /\ Authorized(s.request.token)
    /\ VersionsMatch \/ Fault = "submit-without-cas"
    /\ s.request.snapshot.finding = s.finding \/ Fault = "omit-finding"
    /\ s.request.verdict # "pass" \/ PassReady(s.request.snapshot)
    /\ s' = [s EXCEPT !.phase = "pending", !.receipt = s.request,
              !.acceptedCorpus = s.corpus,
              !.witness = EditWitness(s.request.token,
                                     s.request.snapshot.findingVersion,
                                     s.findingVersion)]

ProjectSubmitted ==
    /\ HasReceipt /\ s.projection = "active"
    /\ s' = [s EXCEPT !.projection = "submitted"]

AcceptReceipt ==
    /\ s.phase = "pending"
    /\ s' = [s EXCEPT !.phase = "accepted"]

\* Lost ACK/restart replay returns the sealed content, not the current draft.
ReplayReceipt == HasReceipt /\ UNCHANGED s
RewriteReceipt ==
    /\ HasReceipt /\ Fault = "replay-rewrite"
    /\ s' = [s EXCEPT !.receipt.snapshot.finding =
                         IF @ = "absent" THEN "completed-blocking" ELSE "absent"]
\* Only the first operation is retained: later edits use fresh operation keys.
\* All requests below claim that retained key. Expected CAS versions are not
\* part of the semantic request hash; changed operation content is a conflict.
\* Replay admission still needs current authority and an editable assignment.
\* Rejected calls stutter, including replays after durable receipt acceptance.
ReplayRequest == [kind |-> s.operation.kind, value |-> s.operation.value,
                  token |-> s.operation.token, expected |-> 0]
ReplayRequests == {ReplayRequest, [ReplayRequest EXCEPT !.expected = MaxVersion + 1],
                  [ReplayRequest EXCEPT !.value = "conflicting-value"]}
SameOperation(request) == request.kind = s.operation.kind /\
                          request.value = s.operation.value
ReplayMutation(request) ==
    /\ s.operation.kind # "none"
    /\ Editable /\ Authorized(request.token)
    /\ SameOperation(request) \/ Fault = "conflicting-replay"
    /\ UNCHANGED s

RotateFence ==
    /\ ~HasReceipt /\ s.fence = 1
    /\ s' = [s EXCEPT !.fence = 2, !.operation = EmptyOperation]
AdvanceGeneration ==
    /\ ~HasReceipt /\ s.generation = 1
    /\ s' = [s EXCEPT !.generation = 2, !.operation = EmptyOperation]

Next ==
    \/ \E token \in Tokens, version \in 0..MaxVersion:
        \/ \E value \in FindingValues, target \in {"current", "inherited"}:
               EditFinding(token, version, value, target)
        \/ \E value \in CaseValues: EditCase(token, version, value)
        \/ \E result \in {"pass", "fail"}, covered \in BOOLEAN:
               RecordEvidence(token, version, result, covered)
    \/ \E token \in Tokens, verdict \in {"pass", "fail"},
          proposedFinding \in {s.finding, "absent"}:
           PrepareSubmit(token, verdict, proposedFinding)
    \/ DiscardPrepared \/ CommitSubmit \/ ProjectSubmitted \/ AcceptReceipt
    \/ \E request \in ReplayRequests: ReplayMutation(request)
    \/ ReplayReceipt \/ RewriteReceipt
    \/ EditCorpus \/ RotateFence \/ AdvanceGeneration

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ s.finding \in FindingValues /\ s.cases \in CaseValues
    /\ s.findingVersion \in 0..MaxVersion
    /\ s.verificationVersion \in 0..MaxVersion
    /\ s.generation \in 1..2 /\ s.fence \in 1..2
    /\ s.corpus \in 0..MaxCorpus
    /\ s.acceptedCorpus \in 0..MaxCorpus
    /\ s.phase \in {"none", "pending", "accepted"}
    /\ s.projection \in {"active", "submitted"}
    /\ s.history \subseteq History /\ s.prepared \in BOOLEAN

HistoryPreserved == s.history = History
MutationsUseCurrentAuthority == s.witness.authorityMatches
MutationsUseCurrentVersion == s.witness.versionMatches

\* Corpus is deliberately excluded: files may change; frozen records may not.
ReceiptFreezesBothDrafts == HasReceipt =>
    /\ s.finding = s.receipt.snapshot.finding
    /\ s.cases = s.receipt.snapshot.cases
    /\ s.findingVersion = s.receipt.snapshot.findingVersion
    /\ s.verificationVersion = s.receipt.snapshot.verificationVersion
    /\ s.evidence = s.receipt.snapshot.evidence
    /\ s.generation = s.receipt.snapshot.generation

SubmissionWasCurrent == HasReceipt =>
    /\ s.receipt.token.generation = s.receipt.snapshot.generation
    /\ s.receipt.snapshot.corpus = s.acceptedCorpus
AtomicSubmissionSnapshot == ReceiptFreezesBothDrafts /\ SubmissionWasCurrent
PassRequiresCurrentEvidence == HasReceipt /\ s.receipt.verdict = "pass" =>
    /\ s.receipt.snapshot.finding \in {"absent", "pending-advisory", "completed-advisory"}
    /\ s.receipt.snapshot.cases # "absent"
    /\ CurrentEvidence(s.receipt.snapshot)
CurrentPassAuthority == s.phase = "accepted" /\ s.receipt.verdict = "pass" /\
                        s.receipt.snapshot.corpus = s.corpus
CurrentPassRequiresCurrentCorpus == CurrentPassAuthority =>
    s.receipt.snapshot.evidence.corpus = s.corpus
ProjectionRequiresReceipt == s.projection = "submitted" => HasReceipt

\* Trusted source classification is evaluated for every combination, including
\* crash lag (active source projection, pending/accepted authoritative receipt).
\* The copied payload's claimed origin does not participate in this decision.
RecoverSourceAsCurrent(projection, phase, owned, sameInput) ==
    /\ owned /\ sameInput /\ projection = "active"
    /\ phase = "none" \/ Fault = "receipt-blind-inheritance"
SourceReceiptsFreezeInheritance ==
    /\ s.history \subseteq History
    /\ \A projection \in {"active", "submitted"}, phase \in {"pending", "accepted"}:
           ~RecoverSourceAsCurrent(projection, phase, TRUE, TRUE)
OwnedUnsubmittedRetryRemainsEditable ==
    RecoverSourceAsCurrent("active", "none", TRUE, TRUE)
ExternalSourceNeverEditable ==
    \A projection \in {"active", "submitted"}, phase \in {"none", "pending", "accepted"},
       sameInput \in BOOLEAN:
        ~RecoverSourceAsCurrent(projection, phase, FALSE, sameInput)

\* These availability invariants prevent vacuous safety by disabling all edits,
\* and reject the old "completed finding is immutable" interpretation.
DraftAllowsFindingCRUD ==
    ~HasReceipt /\ s.projection = "active" /\ s.findingVersion < MaxVersion =>
        \A value \in FindingValues \ {s.finding}:
            ENABLED EditFinding(Authority, s.findingVersion, value, "current")
DraftAllowsCaseCRUD ==
    ~HasReceipt /\ s.projection = "active" /\
    s.verificationVersion < MaxVersion =>
        \A value \in CaseValues \ {s.cases}:
            ENABLED EditCase(Authority, s.verificationVersion, value)

HistoryCannotBeTargeted ==
    \A value \in FindingValues:
        ~ENABLED EditFinding(Authority, s.findingVersion, value, "inherited")
MutationReplayRejectsConflicts ==
    \A request \in ReplayRequests:
        ~SameOperation(request) => ~ENABLED ReplayMutation(request)
MutationReplayRemainsAvailable ==
    s.operation.kind # "none" /\ ~HasReceipt /\
    s.projection = "active" /\ s.operation.token = Authority =>
        \A request \in ReplayRequests:
            SameOperation(request) => ENABLED ReplayMutation(request)

\* This deliberately false invariant is a reachability witness configuration:
\* TLC must find create -> remove -> fresh evidence -> accepted PASS, while the
\* immutable inherited/prior-submitted history remains present.
NoWithdrawalPassWitness == ~(s.phase = "accepted" /\ s.receipt.verdict = "pass" /\
    s.finding = "absent" /\ s.findingVersion = 2 /\ s.history = History)

ReceiptNeverChanges == [][HasReceipt => s'.receipt = s.receipt]_vars
HistoryNeverChanges == [][s'.history = s.history]_vars

=============================================================================
