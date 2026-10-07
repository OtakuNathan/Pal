---------------------- MODULE VerifierRetryIdentity ----------------------
EXTENDS Naturals

(* Focused refinement of VerifierDraftLifecycle.RecoverSourceAsCurrent.
The prior worker is stopped; a new physical assignment rebinds a logical
verifier session. workspace_preparation is attempt-local, not semantic identity.
The canonical semantic reference projection excludes ONLY that reference.
All scalar identity checks, Candidate/other semantic refs, evaluation generation
and authoritative receipt/projection freezes remain necessary.

Rebind maps to source_is_owned_retry + _inherited_payload_locked in their write
transaction. RunRequiredCases abstracts fresh evidence for all required cases;
it never withdraws a finding. Only explicit current finding CRUD can do that.
Source records/receipts remain immutable. This does not change the checked
submission, evidence or canonical finding-upsert protocols.
*)

CONSTANT Fault
IdentityKeys == {"session", "workflow", "aggregate_type", "aggregate_id",
                 "role", "mode", "input_fingerprint", "submission_kind",
                 "evaluation_generation"}
Scenarios == IdentityKeys \cup {"same", "candidate", "contract", "verification_policy"}
SourceIdentity == [key \in IdentityKeys |-> 0]
SourceRefs == [candidate |-> 0, contract |-> 0, verification_policy |-> 0,
               workspace_preparation |-> 0]
SourcePayload == [findings |-> {"blocking-finding"}, cases |-> {"required-case"}]
PriorHistory == {[findings |-> {"older-finding"}, cases |-> {"older-case"}]}
Project(refs, excluded) == [key \in DOMAIN refs \ excluded |-> refs[key]]
SemanticRefs(refs) == Project(refs, {"workspace_preparation"})

VARIABLE s
vars == <<s>>
Init ==
    \E scenario \in Scenarios, preparation \in 0..1,
       receipt \in {"none", "pending", "accepted"}, projection \in {"active", "submitted"},
       validity \in {"valid", "bad-refs-json", "bad-refs-shape", "bad-generation", "bad-execution-spec"}:
        s = [identity |-> [key \in IdentityKeys |-> IF scenario = key THEN 1 ELSE 0],
             refs |-> [candidate |-> IF scenario = "candidate" THEN 1 ELSE 0,
                       contract |-> IF scenario = "contract" THEN 1 ELSE 0,
                       verification_policy |-> IF scenario = "verification_policy" THEN 1 ELSE 0,
                       workspace_preparation |-> preparation],
             source |-> [identity |-> SourceIdentity, refs |-> SourceRefs,
                         payload |-> SourcePayload, history |-> PriorHistory,
                         receipt |-> receipt, projection |-> projection,
                         physical_assignment |-> "previous-attempt"],
             physical_assignment |-> "replacement-attempt", rebound |-> FALSE,
             validity |-> validity, recoveryRejected |-> FALSE,
             recoveredCurrent |-> FALSE,
             copied |-> [findings |-> {}, cases |-> {}],
             findings |-> {}, cases |-> {}, history |-> PriorHistory,
             explicitRemoval |-> FALSE, evidenceFresh |-> FALSE, passed |-> FALSE]

SameSemanticEvaluation ==
    /\ s.identity = s.source.identity
    /\ SemanticRefs(s.refs) = SemanticRefs(s.source.refs)
SourceUnsubmitted == s.source.receipt = "none" /\ s.source.projection = "active"
OwnedUnsubmittedRetry == s.validity = "valid" /\ SameSemanticEvaluation /\ SourceUnsubmitted

ComparedIdentityKeys == IdentityKeys \ (
    IF Fault = "ignore-session" THEN {"session"}
    ELSE IF Fault = "ignore-generation" THEN {"evaluation_generation"} ELSE {})
ExcludedRefs ==
    IF Fault = "raw-full-refs" THEN {}
    ELSE IF Fault = "ignore-candidate" THEN {"workspace_preparation", "candidate"}
    ELSE IF Fault = "ignore-semantic-ref" THEN {"workspace_preparation", "contract"}
    ELSE IF Fault = "ignore-policy" THEN {"workspace_preparation", "verification_policy"}
    ELSE {"workspace_preparation"}
RecoverCurrent ==
    /\ s.validity = "valid"
    /\ \A key \in ComparedIdentityKeys: s.identity[key] = s.source.identity[key]
    /\ Project(s.refs, ExcludedRefs) = Project(s.source.refs, ExcludedRefs)
    /\ s.source.receipt = "none" \/ Fault = "ignore-receipt"
    /\ s.source.projection = "active" \/ Fault = "ignore-projection"

Rebind ==
    /\ ~s.rebound /\ ~s.recoveryRejected
    /\ s.validity = "valid" \/ Fault = "malformed-as-external"
    /\ s' = [s EXCEPT !.rebound = TRUE, !.recoveredCurrent = RecoverCurrent,
              !.copied = IF RecoverCurrent THEN s.source.payload
                         ELSE [findings |-> {}, cases |-> {}],
              !.findings = IF RecoverCurrent THEN s.source.payload.findings ELSE {},
              !.cases = IF RecoverCurrent THEN s.source.payload.cases ELSE {},
              !.history = IF RecoverCurrent THEN s.source.history
                          ELSE s.source.history \cup {s.source.payload}]

\* validity abstracts successful decoding/shape validation of BOTH stored
\* identities. Invalid data is an admission error, never an external-source
\* classification that publishes a new empty current draft.
RejectMalformed ==
    /\ ~s.rebound /\ ~s.recoveryRejected /\ s.validity # "valid"
    /\ s' = [s EXCEPT !.recoveryRejected = TRUE]

RemoveCurrentFinding ==
    /\ s.rebound /\ ~s.passed /\ "blocking-finding" \in s.findings
    /\ s' = [s EXCEPT !.findings = {}, !.explicitRemoval = TRUE]
RunRequiredCases ==
    /\ s.rebound /\ ~s.passed /\ ~s.evidenceFresh
    /\ s' = [s EXCEPT !.cases = @ \cup {"required-case"}, !.evidenceFresh = TRUE]
CommitPass ==
    /\ s.rebound /\ ~s.passed /\ s.findings = {}
    /\ s.evidenceFresh /\ "required-case" \in s.cases
    /\ s' = [s EXCEPT !.passed = TRUE]
Next == Rebind \/ RejectMalformed \/ RemoveCurrentFinding \/ RunRequiredCases \/ CommitPass
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ s.identity \in [IdentityKeys -> 0..1]
    /\ s.rebound \in BOOLEAN /\ s.recoveredCurrent \in BOOLEAN
    /\ s.recoveryRejected \in BOOLEAN
    /\ s.explicitRemoval \in BOOLEAN /\ s.evidenceFresh \in BOOLEAN /\ s.passed \in BOOLEAN
    /\ s.findings \subseteq SourcePayload.findings /\ s.cases \subseteq SourcePayload.cases
OwnedRetryRetainsCurrent == s.rebound /\ OwnedUnsubmittedRetry =>
    s.copied = s.source.payload /\ s.recoveredCurrent
OnlyOwnedSemanticDraftsRemainCurrent == s.rebound /\ s.recoveredCurrent => OwnedUnsubmittedRetry
CandidateChangesNeverCurrent == s.rebound /\ s.refs.candidate # s.source.refs.candidate => ~s.recoveredCurrent
SemanticRefChangesNeverCurrent == s.rebound /\ s.refs.contract # s.source.refs.contract => ~s.recoveredCurrent
VerificationPolicyNeverIgnored == s.rebound /\ s.refs.verification_policy # s.source.refs.verification_policy => ~s.recoveredCurrent
ForeignSessionNeverCurrent == s.rebound /\ s.identity.session # s.source.identity.session => ~s.recoveredCurrent
NewGenerationNeverCurrent == s.rebound /\ s.identity.evaluation_generation # s.source.identity.evaluation_generation => ~s.recoveredCurrent
ReceiptBackedSourceNeverCurrent == s.rebound /\ s.source.receipt # "none" => ~s.recoveredCurrent
SubmittedProjectionNeverCurrent == s.rebound /\ s.source.projection = "submitted" => ~s.recoveredCurrent
MalformedIdentityNeverRebinds == s.validity # "valid" => ~s.rebound /\ ~s.passed
ImmutableHistoryPreserved ==
    /\ s.source.payload = SourcePayload /\ s.source.history = PriorHistory
    /\ PriorHistory \subseteq s.history
    /\ s.rebound /\ ~OwnedUnsubmittedRetry => s.source.payload \in s.history
NoSilentPassWithoutCRUD == s.passed /\ OwnedUnsubmittedRetry => s.explicitRemoval
PassStillRequiresEvidence == s.passed => s.evidenceFresh /\ s.findings = {} /\ "required-case" \in s.cases
SourceNeverChanges == [][s'.source = s.source]_vars

\* Required reachability counterexample: only attempt-local refs/physical
\* assignment differ, and both current findings and cases survive the rebind.
NoSemanticRebindWitness == ~(s.rebound /\ OwnedUnsubmittedRetry /\
    s.refs.workspace_preparation # s.source.refs.workspace_preparation /\
    s.physical_assignment # s.source.physical_assignment /\
    s.copied = SourcePayload /\ ~s.explicitRemoval /\ ~s.passed)
=============================================================================
