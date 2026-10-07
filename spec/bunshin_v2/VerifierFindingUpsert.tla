---------------------- MODULE VerifierFindingUpsert ----------------------
EXTENDS Naturals, FiniteSets

(* Focused API refinement of VerifierDraftLifecycle.EditFinding, not a new
submission/evidence protocol. Its existing actor/fence, two-row CAS, evidence
and durable-receipt guards remain required. Freeze abstracts its CommitSubmit.

update_finding(id, expected_revision=0, ...) inserts a never-used current ID.
The same action updates a present ID only at its matching positive revision.
remove_finding reserves the removed identity; use a fresh ID for a new finding.
History IDs are reserved even though absent from the editable current map.
One owned ID represents any chosen caller ID; one protected ID represents
inherited/submitted history. Three operation keys/revisions permit a full CRUD
trace. These bounds are exploration limits, never production edit limits.

The recorded semantic request includes id, expected_revision, content and reason.
Store transport expected_version is outside this hash. Exact operation replay
reads the recorded result and never reexecutes the upsert, including after delete.
Outer authority may reject a replay; this model only asserts no repeated write.
*)

CONSTANT Fault
IDs == {"owned", "protected"}
Keys == 1..3
Values == {"red", "blue"}
EmptyRecord == [value |-> "absent", revision |-> 0]
EmptyRequest == [kind |-> "none", id |-> "owned", expected |-> 0,
                 value |-> "absent", reason |-> ""]
Request(kind, id, expected, value, reason) ==
    [kind |-> kind, id |-> id, expected |-> expected,
     value |-> value, reason |-> reason]

VARIABLE s
vars == <<s>>
Init == s = [records |-> [id \in IDs |-> EmptyRecord], deleted |-> {},
             operations |-> [key \in Keys |-> EmptyRequest],
             results |-> [key \in Keys |-> 0], version |-> 0,
             lastExpected |-> 0, lastBase |-> 0, frozen |-> FALSE,
             sealed |-> <<[id \in IDs |-> EmptyRecord], {}, 0>>]
UsedKeys == {key \in Keys: s.operations[key].kind # "none"}
Present(id) == s.records[id].value # "absent"
Editable == ~s.frozen \/ Fault = "receipt-blind"
Owned(id) == id = "owned" \/ Fault = "history-id"
Snapshot == <<s.records, s.deleted, s.version>>

UpdateFinding(key, id, expected, value) ==
    /\ Editable /\ key \notin UsedKeys /\ Owned(id)
    /\ id \notin s.deleted \/ Fault = "resurrect"
    /\ value \in Values /\ s.records[id].revision < 3
    /\ expected = (IF Present(id) THEN s.records[id].revision ELSE 0)
       \/ Fault = "stale-revision"
    /\ s' = [s EXCEPT
        !.records[id] = [value |-> value, revision |->
                             IF Present(id) THEN s.records[id].revision + 1 ELSE 1],
        !.operations[key] = Request("update", id, expected, value, ""),
        !.results[key] = IF Present(id) THEN s.records[id].revision + 1 ELSE 1,
        !.version = @ + 1, !.lastExpected = expected,
        !.lastBase = IF Present(id) THEN s.records[id].revision ELSE 0]

RemoveFinding(key, id, expected, reason) ==
    /\ Editable /\ key \notin UsedKeys /\ Owned(id) /\ Present(id)
    /\ s.records[id].revision < 3 /\ reason \in {"corrected", "duplicate"}
    /\ expected = s.records[id].revision \/ Fault = "stale-revision"
    /\ s' = [s EXCEPT
        !.records[id] = [value |-> "absent", revision |-> s.records[id].revision],
        !.deleted = @ \cup {id},
        !.operations[key] = Request("remove", id, expected, "absent", reason),
        !.results[key] = s.records[id].revision,
        !.version = @ + 1, !.lastExpected = expected,
        !.lastBase = s.records[id].revision]

\* Probes change both content and semantic expected_revision independently.
ReplayRequests(key) == {s.operations[key],
    [s.operations[key] EXCEPT !.value = "conflicting-content"],
    [s.operations[key] EXCEPT !.expected = 4]}
Replay(key, request) ==
    /\ key \in UsedKeys
    /\ request = s.operations[key] \/ Fault = "conflicting-replay"
    /\ IF Fault = "replay-write" THEN s' = [s EXCEPT !.version = @ + 1]
       ELSE UNCHANGED s

Freeze == /\ ~s.frozen
          /\ s' = [s EXCEPT !.frozen = TRUE, !.sealed = Snapshot]
Next ==
    \/ \E key \in Keys, id \in IDs, expected \in 0..3:
        \/ \E value \in Values: UpdateFinding(key, id, expected, value)
        \/ \E reason \in {"corrected", "duplicate"}:
               RemoveFinding(key, id, expected, reason)
    \/ \E key \in Keys: \E request \in ReplayRequests(key): Replay(key, request)
    \/ Freeze
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ s.records \in [IDs -> [value: Values \cup {"absent"}, revision: 0..3]]
    /\ s.deleted \subseteq IDs /\ s.frozen \in BOOLEAN
    /\ s.version \in Nat /\ s.lastExpected \in 0..3 /\ s.lastBase \in 0..3
ExpectedRevisionWasCurrent == s.lastExpected = s.lastBase
ProtectedIDsNeverCurrent == s.records["protected"] = EmptyRecord
RemovedIDsStayAbsent == \A id \in s.deleted: ~Present(id)
ExactlyOneWritePerOperation == s.version = Cardinality(UsedKeys)
ReceiptFreezesFindings == s.frozen => Snapshot = s.sealed
ConflictReplayRejected ==
    \A key \in UsedKeys:
        \A request \in ReplayRequests(key) \ {s.operations[key]}:
            ~ENABLED Replay(key, request)
ExactReplayAvailable ==
    \A key \in UsedKeys: ENABLED Replay(key, s.operations[key])
CanonicalUpdateAvailable ==
    ~s.frozen /\ s.records["owned"].revision < 3 /\ "owned" \notin s.deleted =>
        \A key \in Keys \ UsedKeys, value \in Values:
            ENABLED UpdateFinding(key, "owned",
                IF Present("owned") THEN s.records["owned"].revision ELSE 0, value)
RemoveAvailable ==
    ~s.frozen /\ Present("owned") /\ s.records["owned"].revision < 3 =>
        \A key \in Keys \ UsedKeys:
            ENABLED RemoveFinding(key, "owned", s.records["owned"].revision, "corrected")
RecordedOperationsNeverChange == [][\A key \in UsedKeys:
    s'.operations[key] = s.operations[key] /\ s'.results[key] = s.results[key]]_vars

\* Required counterexample: canonical update inserts, canonical update replaces,
\* then remove deletes the same stable ID; its prior operation remains replayable.
NoCRUDWitness == ~("owned" \in s.deleted /\ s.records["owned"].revision = 2 /\
                   s.version = 3)
=============================================================================
