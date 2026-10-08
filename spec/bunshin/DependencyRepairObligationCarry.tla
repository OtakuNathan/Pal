---------------- MODULE DependencyRepairObligationCarry ----------------
(*
Shared finding-scope validation is the classification authority. A dominant
classification must not erase local/module cases in a mixed dependency packet.

  Model                       Runtime obligation
  Capture                     preserve original packet/report/corpus references
  ValidProviderTargets        shared scope validator; contract_only is never a provider
  obligations                 mandatory historical_repair_bill_refs / finding-case identities
  ReplaceCandidate/InvalidateAgain carry required refs, reset candidate-bound resolutions
  ResolveCase                 explicit case replay against the CURRENT candidate
  PassNextChecker             semantic_verification_submission_errors requires all cases

The constants below bound five receipt kinds and six immutable case identities.
The model does not copy report payloads or authorize a stale candidate's acceptance.
*)
EXTENDS Naturals, FiniteSets
Receipts == {"dependency", "module", "invalid_scope", "contract_only", "mixed"}
ReceiptCases == [r \in Receipts |->
    IF r = "dependency" THEN {"dep_case"}
    ELSE IF r = "module" THEN {"module_case"}
    ELSE IF r = "invalid_scope" THEN {"correction_case"}
    ELSE IF r = "contract_only" THEN {"contract_case"}
    ELSE {"mixed_dep_case", "mixed_module_case"}]
Cases == UNION {ReceiptCases[r] : r \in Receipts}
ValidProviderTargets == [r \in Receipts |->
    IF r = "dependency" THEN {"P"} ELSE IF r = "mixed" THEN {"Q"} ELSE {}]
InvalidReceipts == {"invalid_scope", "contract_only"}
VARIABLES captured, obligations, resolved, targets, nextCandidate, passed, reinvalidated
vars == <<captured,obligations,resolved,targets,nextCandidate,passed,reinvalidated>>
Init == /\ captured = {} /\ obligations = {} /\ resolved = {} /\ targets = {}
        /\ nextCandidate = FALSE /\ passed = FALSE /\ reinvalidated = FALSE
Capture(r) == /\ r \in Receipts \ captured /\ ~nextCandidate
    /\ captured' = captured \cup {r}
    /\ obligations' = obligations \cup ReceiptCases[r]
    /\ targets' = targets \cup ValidProviderTargets[r]
    /\ UNCHANGED <<resolved,nextCandidate,passed,reinvalidated>>
ReplaceCandidate == /\ captured = Receipts /\ ~nextCandidate /\ nextCandidate' = TRUE
    /\ UNCHANGED <<captured,obligations,resolved,targets,passed,reinvalidated>>
ResolveCase(c) == /\ nextCandidate /\ c \in obligations \ resolved
    /\ resolved' = resolved \cup {c}
    /\ UNCHANGED <<captured,obligations,targets,nextCandidate,passed,reinvalidated>>
InvalidateAgain == /\ nextCandidate /\ ~reinvalidated /\ reinvalidated' = TRUE /\ resolved' = {}
    /\ UNCHANGED <<captured,obligations,targets,nextCandidate,passed>>
PassNextChecker == /\ nextCandidate /\ reinvalidated /\ obligations \subseteq resolved /\ ~passed /\ passed' = TRUE
    /\ UNCHANGED <<captured,obligations,resolved,targets,nextCandidate,reinvalidated>>
ReplayReceipt == UNCHANGED vars
Next == (\E r \in Receipts : Capture(r)) \/ ReplaceCandidate \/ InvalidateAgain
     \/ (\E c \in Cases : ResolveCase(c)) \/ PassNextChecker \/ ReplayReceipt
Spec == Init /\ [][Next]_vars
TypeOK ==
    /\ captured \subseteq Receipts /\ obligations \subseteq Cases
    /\ resolved \subseteq obligations /\ targets \subseteq {"P", "Q"}
    /\ nextCandidate \in BOOLEAN /\ passed \in BOOLEAN /\ reinvalidated \in BOOLEAN
CapturedFindingsRemainRequired == UNION {ReceiptCases[r] : r \in captured} \subseteq obligations
PassRequiresExplicitResolution == passed => obligations \subseteq resolved
OnlyValidatedScopeTargetsProviders == targets = UNION {ValidProviderTargets[r] : r \in captured}
InvalidScopeHasNoProviderTarget == \A r \in InvalidReceipts : ValidProviderTargets[r] = {}
\* ReceiptCases are immutable (packet SHA, finding ID) references. They include
\* valid dependency, module, correction, and mixed-route findings. Shared scope
\* validation chooses ValidProviderTargets; no generic dominant-class shortcut.
=================================================================
