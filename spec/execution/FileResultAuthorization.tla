---------------------- MODULE FileResultAuthorization ----------------------
EXTENDS Integers, FiniteSets, TLC

(***************************************************************************
Result-owned authorization for read_file, edit_file, and write_file.

The model deliberately treats file contents as bounded versions and file
regions as abstract lines.  Python refines the abstract line set with its
canonical line map and refines Mutate with a digest-checked filesystem CAS.

Every per-line contribution records two owner sets:

* contribOwners: results that must still be live and present in the current
  prompt projection;
* requiredVisible: owners that must still project the exact line bytes.

An edit result owns its changed post-image lines directly.  Unchanged lines
inherit one valid parent contribution and add the edit result as another
owner.  Retiring or hiding any required owner therefore shrinks authority
without a snapshot fallback.
***************************************************************************)

Results == {"r1", "r2", "r3"}
\* Two abstract regions are the smallest domain that distinguishes partial
\* from complete authority.  Three result identities still expose a chained
\* read -> edit -> edit dependency and every retirement interleaving.
Lines == 1..2
\* Three versions cover two consecutive successful mutations, which is the
\* shortest lineage that exposes rollback and inherited-owner leaks.
Versions == 0..2
ResultStates == {"unused", "pending", "live", "retired"}
\* Replay and direct delivery intentionally refine the same abstract Read
\* transition: their ownership boundary differs in Python, not their file
\* evidence semantics.
ResultKinds == {"none", "read", "edit", "write"}

VARIABLES
    diskExists,
    diskVersion,
    resultState,
    resultKind,
    resultVersion,
    present,
    visible,
    renderable,
    contribOwners,
    requiredVisible,
    externalChanged,
    lastRejected,
    rejectedVersion

vars == <<
    diskExists, diskVersion, resultState, resultKind, resultVersion,
    present, visible, renderable, contribOwners, requiredVisible,
    externalChanged, lastRejected, rejectedVersion
>>

EmptyLineMap == [r \in Results |-> [line \in Lines |-> {}]]

Init ==
    /\ diskExists = TRUE
    /\ diskVersion = 0
    /\ resultState = [r \in Results |-> "unused"]
    /\ resultKind = [r \in Results |-> "none"]
    /\ resultVersion = [r \in Results |-> -1]
    /\ present = [r \in Results |-> FALSE]
    /\ visible = [r \in Results |-> {}]
    /\ renderable = [r \in Results |-> {}]
    /\ contribOwners = EmptyLineMap
    /\ requiredVisible = EmptyLineMap
    /\ externalChanged = FALSE
    /\ lastRejected = FALSE
    /\ rejectedVersion = -1

LivePresent ==
    {r \in Results : resultState[r] = "live" /\ present[r]}

ContributionValid(r, line) ==
    /\ resultState[r] = "live"
    /\ present[r]
    /\ resultVersion[r] = diskVersion
    /\ contribOwners[r][line] # {}
    /\ contribOwners[r][line] \subseteq LivePresent
    /\ requiredVisible[r][line] \subseteq contribOwners[r][line]
    /\ \A owner \in requiredVisible[r][line] :
          line \in visible[owner]

Authority ==
    {line \in Lines : \E r \in Results : ContributionValid(r, line)}

Witness(line) ==
    CHOOSE r \in Results : ContributionValid(r, line)

ReadEvidenceOwners(r) ==
    [line \in Lines |-> IF line \in renderable[r] THEN {r} ELSE {}]

ReadRequiredVisible(r) == ReadEvidenceOwners(r)

WriteEvidenceOwners(r) == [line \in Lines |-> {r}]
WriteRequiredVisible(r) == [line \in Lines |-> {}]

EditEvidenceOwners(r, target) ==
    [line \in Lines |->
        IF line \in target
        THEN {r}
        ELSE IF line \in Authority
        THEN contribOwners[Witness(line)][line] \cup {r}
        ELSE {}]

EditRequiredVisible(r, target) ==
    [line \in Lines |->
        IF line \in target
        THEN {r}
        ELSE IF line \in Authority
        THEN requiredVisible[Witness(line)][line]
        ELSE {}]

ObserveRead(r, coverage) ==
    /\ r \in Results
    /\ coverage \in SUBSET Lines
    /\ coverage # {}
    /\ resultState[r] = "unused"
    /\ diskExists
    /\ resultState' = [resultState EXCEPT ![r] = "pending"]
    /\ resultKind' = [resultKind EXCEPT ![r] = "read"]
    /\ resultVersion' = [resultVersion EXCEPT ![r] = diskVersion]
    /\ renderable' = [renderable EXCEPT ![r] = coverage]
    /\ contribOwners' = [contribOwners EXCEPT
          ![r] = [line \in Lines |-> IF line \in coverage THEN {r} ELSE {}]]
    /\ requiredVisible' = [requiredVisible EXCEPT
          ![r] = [line \in Lines |-> IF line \in coverage THEN {r} ELSE {}]]
    /\ present' = [present EXCEPT ![r] = FALSE]
    /\ visible' = [visible EXCEPT ![r] = {}]
    /\ externalChanged' = externalChanged
    /\ lastRejected' = FALSE
    /\ rejectedVersion' = -1
    /\ UNCHANGED <<diskExists, diskVersion>>

DeliverResult(r) ==
    /\ r \in Results
    /\ resultState[r] = "pending"
    /\ resultState' = [resultState EXCEPT ![r] = "live"]
    /\ present' = [present EXCEPT ![r] = TRUE]
    /\ visible' = [visible EXCEPT ![r] = renderable[r]]
    /\ externalChanged' = FALSE
    /\ lastRejected' = FALSE
    /\ rejectedVersion' = -1
    /\ UNCHANGED <<diskExists, diskVersion, resultKind, resultVersion,
                    renderable, contribOwners, requiredVisible>>

ProjectResult(r, coverage) ==
    /\ r \in Results
    /\ resultState[r] = "live"
    /\ coverage \in SUBSET renderable[r]
    /\ present' = [present EXCEPT ![r] = TRUE]
    /\ visible' = [visible EXCEPT ![r] = coverage]
    /\ lastRejected' = FALSE
    /\ rejectedVersion' = -1
    /\ UNCHANGED <<diskExists, diskVersion, resultState, resultKind,
                    resultVersion, renderable, contribOwners,
                    requiredVisible, externalChanged>>

HideResult(r) ==
    /\ r \in Results
    /\ resultState[r] = "live"
    /\ present' = [present EXCEPT ![r] = FALSE]
    /\ visible' = [visible EXCEPT ![r] = {}]
    /\ lastRejected' = FALSE
    /\ rejectedVersion' = -1
    /\ UNCHANGED <<diskExists, diskVersion, resultState, resultKind,
                    resultVersion, renderable, contribOwners,
                    requiredVisible, externalChanged>>

ExecuteEdit(r, target) ==
    /\ r \in Results
    /\ target \in SUBSET Lines
    /\ target # {}
    /\ resultState[r] = "unused"
    /\ diskExists
    /\ diskVersion < 2
    /\ target \subseteq Authority
    /\ resultState' = [resultState EXCEPT ![r] = "pending"]
    /\ resultKind' = [resultKind EXCEPT ![r] = "edit"]
    /\ resultVersion' = [resultVersion EXCEPT ![r] = diskVersion + 1]
    /\ renderable' = [renderable EXCEPT ![r] = target]
    /\ contribOwners' = [contribOwners EXCEPT
          ![r] = EditEvidenceOwners(r, target)]
    /\ requiredVisible' = [requiredVisible EXCEPT
          ![r] = EditRequiredVisible(r, target)]
    /\ present' = [present EXCEPT ![r] = FALSE]
    /\ visible' = [visible EXCEPT ![r] = {}]
    /\ diskVersion' = diskVersion + 1
    /\ externalChanged' = FALSE
    /\ lastRejected' = FALSE
    /\ rejectedVersion' = -1
    /\ UNCHANGED diskExists

RejectEdit(target) ==
    /\ target \in SUBSET Lines
    /\ target # {}
    /\ (~diskExists \/ ~(target \subseteq Authority))
    /\ lastRejected' = TRUE
    /\ rejectedVersion' = diskVersion
    /\ UNCHANGED <<diskExists, diskVersion, resultState, resultKind,
                    resultVersion, present, visible, renderable,
                    contribOwners, requiredVisible, externalChanged>>

ExecuteWrite(r) ==
    /\ r \in Results
    /\ resultState[r] = "unused"
    /\ diskVersion < 2
    /\ (~diskExists \/ Authority = Lines)
    /\ resultState' = [resultState EXCEPT ![r] = "pending"]
    /\ resultKind' = [resultKind EXCEPT ![r] = "write"]
    /\ resultVersion' = [resultVersion EXCEPT ![r] = diskVersion + 1]
    /\ renderable' = [renderable EXCEPT ![r] = {}]
    /\ contribOwners' = [contribOwners EXCEPT
          ![r] = WriteEvidenceOwners(r)]
    /\ requiredVisible' = [requiredVisible EXCEPT
          ![r] = WriteRequiredVisible(r)]
    /\ present' = [present EXCEPT ![r] = FALSE]
    /\ visible' = [visible EXCEPT ![r] = {}]
    /\ diskExists' = TRUE
    /\ diskVersion' = diskVersion + 1
    /\ externalChanged' = FALSE
    /\ lastRejected' = FALSE
    /\ rejectedVersion' = -1

RejectWrite ==
    /\ diskExists
    /\ Authority # Lines
    /\ lastRejected' = TRUE
    /\ rejectedVersion' = diskVersion
    /\ UNCHANGED <<diskExists, diskVersion, resultState, resultKind,
                    resultVersion, present, visible, renderable,
                    contribOwners, requiredVisible, externalChanged>>

ExternalMutate ==
    /\ diskExists
    /\ diskVersion < 2
    /\ diskVersion' = diskVersion + 1
    /\ externalChanged' = TRUE
    /\ lastRejected' = FALSE
    /\ rejectedVersion' = -1
    /\ UNCHANGED <<diskExists, resultState, resultKind, resultVersion,
                    present, visible, renderable, contribOwners,
                    requiredVisible>>

RetireResult(r) ==
    /\ r \in Results
    /\ resultState[r] \in {"pending", "live"}
    /\ resultState' = [resultState EXCEPT ![r] = "retired"]
    /\ present' = [present EXCEPT ![r] = FALSE]
    /\ visible' = [visible EXCEPT ![r] = {}]
    /\ lastRejected' = FALSE
    /\ rejectedVersion' = -1
    /\ UNCHANGED <<diskExists, diskVersion, resultKind, resultVersion,
                    renderable, contribOwners, requiredVisible,
                    externalChanged>>

Next ==
    \/ \E r \in Results, coverage \in SUBSET Lines :
          ObserveRead(r, coverage)
    \/ \E r \in Results : DeliverResult(r)
    \/ \E r \in Results, coverage \in SUBSET Lines :
          ProjectResult(r, coverage)
    \/ \E r \in Results : HideResult(r)
    \/ \E r \in Results, target \in SUBSET Lines :
          ExecuteEdit(r, target)
    \/ \E target \in SUBSET Lines : RejectEdit(target)
    \/ \E r \in Results : ExecuteWrite(r)
    \/ RejectWrite
    \/ ExternalMutate
    \/ \E r \in Results : RetireResult(r)
    \/ UNCHANGED vars

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ diskExists \in BOOLEAN
    /\ diskVersion \in Versions
    /\ resultState \in [Results -> ResultStates]
    /\ resultKind \in [Results -> ResultKinds]
    /\ resultVersion \in [Results -> {-1} \cup Versions]
    /\ present \in [Results -> BOOLEAN]
    /\ visible \in [Results -> SUBSET Lines]
    /\ renderable \in [Results -> SUBSET Lines]
    /\ contribOwners \in [Results -> [Lines -> SUBSET Results]]
    /\ requiredVisible \in [Results -> [Lines -> SUBSET Results]]
    /\ externalChanged \in BOOLEAN
    /\ lastRejected \in BOOLEAN
    /\ rejectedVersion \in {-1} \cup Versions

NoPendingAuthority ==
    \A r \in Results :
        resultState[r] = "pending" =>
            \A line \in Lines : ~ContributionValid(r, line)

RetiredOwnsNothing ==
    \A retired \in Results :
        resultState[retired] = "retired" =>
            \A r \in Results, line \in Lines :
                retired \in contribOwners[r][line] =>
                    ~ContributionValid(r, line)

ExactVisibilityRequired ==
    \A r \in Results, line \in Lines :
        ContributionValid(r, line) =>
            \A owner \in requiredVisible[r][line] :
                line \in visible[owner]

AuthorityMatchesDiskVersion ==
    \A line \in Authority :
        \E r \in Results :
            ContributionValid(r, line) /\ resultVersion[r] = diskVersion

ExternalChangeRevokesAuthority ==
    externalChanged => Authority = {}

RejectedMutationIsStable ==
    lastRejected => diskVersion = rejectedVersion

RetiredIdsAreTerminal ==
    \A r \in Results :
        resultState[r] = "retired" => resultKind[r] # "none"

=============================================================================
