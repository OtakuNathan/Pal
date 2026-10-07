---------------------- MODULE ImportedPlanLifecycle ----------------------
EXTENDS Naturals, TLC

\* Bounded refinement for the review_then_execute imported-architecture path.
\* This scenario has a nonempty foreign embedded GraphIR; graphless imports
\* and otherwise owner-matching imports are outside this narrow abstraction.
\* Legacy starts intentionally represent the old split projection. They may
\* acquire a PlanCycle binding only from the complete durable import witness.
\* Rejected inputs and failed transactions stutter; this model makes no
\* liveness claim for corrupt legacy rows or for human decisions.
CONSTANT UnsafeMode

NoRef == "None"
ImportedRef == "ImportedArchitecture"
FreshRef == "FreshArchitecture"
OtherRef == "OtherArchitecture"
Refs == {NoRef, ImportedRef, FreshRef, OtherRef}

\* Each bad field represents a missing/altered member of the durable witness.
\* requirementsRef abstracts agreement of revision, request, and manifest
\* requirements; it is not merely a check that one reference is nonempty.
GoodEvidence == [
    eventAction |-> "IMPORT_ARCHITECTURE_REVISION",
    eventRef |-> ImportedRef,
    requestOperation |-> "review_then_execute",
    requestRef |-> ImportedRef,
    revisionManifestRef |-> ImportedRef,
    requirementsRef |-> "CurrentRequirements",
    workflowRevision |-> 1
]
EvidenceCases == {GoodEvidence,
    [GoodEvidence EXCEPT !.eventAction = "ARCHITECT_SUBMITTED"],
    [GoodEvidence EXCEPT !.eventRef = OtherRef],
    [GoodEvidence EXCEPT !.requestOperation = "execute"],
    [GoodEvidence EXCEPT !.requestRef = OtherRef],
    [GoodEvidence EXCEPT !.revisionManifestRef = OtherRef],
    [GoodEvidence EXCEPT !.requirementsRef = "OtherRequirements"],
    [GoodEvidence EXCEPT !.workflowRevision = 2]
}

VARIABLE s
vars == <<s>>

Initial(legacy, evidence) == [
    legacy |-> legacy, evidence |-> evidence, recovered |-> FALSE,
    importRecorded |-> legacy,
    architecture |-> IF legacy THEN "ReviewQueued" ELSE "ArchitectQueued",
    plan |-> "ProducerReady", generation |-> 1, slot |-> "None",
    product |-> NoRef, productGeneration |-> 0, verdictGeneration |-> 0,
    acceptedProduct |-> NoRef, producerAdmissions |-> 0,
    revision |-> 1, workflowRevision |-> 1, installed |-> NoRef
]

Init ==
    \/ s = Initial(FALSE, GoodEvidence)
    \/ \E evidence \in EvidenceCases: s = Initial(TRUE, evidence)

PristinePlan ==
    /\ s.plan = "ProducerReady"
    /\ s.generation = 1
    /\ s.slot = "None"
    /\ s.product = NoRef
    /\ s.productGeneration = 0
    /\ s.verdictGeneration = 0
    /\ s.acceptedProduct = NoRef

ImportNew(ref) ==
    /\ ~s.legacy
    /\ s.architecture = "ArchitectQueued"
    /\ PristinePlan
    /\ ref = ImportedRef
    \* Both durable projections become visible in one commit.
    /\ s' = [s EXCEPT !.architecture = "ReviewQueued",
        !.plan = "CheckerReady", !.product = ref, !.productGeneration = 1,
        !.importRecorded = TRUE]

ImportRollback ==
    /\ ~s.legacy
    /\ s.architecture = "ArchitectQueued"
    /\ PristinePlan
    /\ UNCHANGED vars

RecoverLegacy(ref) ==
    /\ s.legacy
    /\ s.importRecorded
    /\ s.architecture = "ReviewQueued"
    /\ PristinePlan
    /\ ref = ImportedRef
    /\ s.evidence = GoodEvidence \/ UnsafeMode = "unproven_recovery"
    /\ s' = [s EXCEPT !.plan = "CheckerReady", !.product = ref,
        !.productGeneration = 1, !.recovered = TRUE]

ReplayExactImport(ref) ==
    /\ s.architecture = "ReviewQueued"
    /\ s.plan = "CheckerReady"
    /\ s.generation = 1
    /\ ref = ImportedRef
    /\ s.product = ref
    /\ UNCHANGED vars

StartReviewer ==
    /\ s.architecture = "ReviewQueued"
    /\ s.plan = "CheckerReady"
    /\ s.product # NoRef
    /\ s.productGeneration = s.generation
    /\ s' = [s EXCEPT !.architecture = "Reviewing",
        !.plan = "Checking", !.slot = "Checker"]

ReviewerPassed ==
    /\ s.architecture = "Reviewing"
    /\ s.plan = "Checking"
    /\ s.slot = "Checker"
    \* Public reviewer settlement composes CHECKER_ACCEPTED and
    \* REQUEST_HUMAN_REVIEW; their internal intermediate ACCEPTED is hidden.
    /\ s' = [s EXCEPT !.architecture = "HumanReview",
        !.plan = "HumanReview", !.slot = "None",
        !.verdictGeneration = s.generation, !.acceptedProduct = s.product]

HumanEdit ==
    /\ s.architecture = "HumanReview"
    /\ s.plan = "HumanReview"
    /\ s.generation = 1
    /\ s' = [s EXCEPT !.architecture = "ArchitectQueued",
        !.plan = "RepairReady", !.generation = 2,
        !.product = NoRef, !.productGeneration = 0, !.verdictGeneration = 0,
        !.acceptedProduct = NoRef, !.revision = 2, !.workflowRevision = 2]

StartArchitect ==
    /\ s.architecture = "ArchitectQueued"
    /\ s.plan = "RepairReady"
    /\ s.generation = 2
    /\ s' = [s EXCEPT !.architecture = "ArchitectRunning",
        !.plan = "Producing", !.slot = "Producer", !.producerAdmissions = 1]

SubmitFreshProduct ==
    /\ s.architecture = "ArchitectRunning"
    /\ s.plan = "Producing"
    /\ s.slot = "Producer"
    /\ s' = [s EXCEPT !.architecture = "ReviewQueued",
        !.plan = "CheckerReady", !.slot = "None", !.product = FreshRef,
        !.productGeneration = s.generation]

GraphOwner(ref) == IF ref = FreshRef THEN "NewWorkflowGraph" ELSE "ForeignGraph"
GraphGeneration(ref) == IF ref \in {ImportedRef, FreshRef} THEN 1 ELSE 0

HumanAccept ==
    /\ s.architecture = "HumanReview"
    /\ s.plan = "HumanReview"
    \* The public decision preflight rejects a foreign embedded GraphIR before
    \* consuming the decision or changing either projection. Direct internal
    \* PlanCycle.HUMAN_ACCEPTED remains broader than this public operation.
    /\ GraphOwner(s.product) = "NewWorkflowGraph" \/ UnsafeMode = "foreign_accept"
    /\ s' = [s EXCEPT !.architecture = "Accepted", !.plan = "Accepted"]

InstallGraph ==
    /\ s.architecture = "Accepted"
    /\ s.plan = "Accepted"
    /\ s.installed = NoRef
    /\ s.product = s.acceptedProduct
    /\ GraphOwner(s.product) = "NewWorkflowGraph"
    \* The negative control installs the stale graph after fresh acceptance.
    /\ s' = [s EXCEPT !.installed =
        IF UnsafeMode = "foreign_install" THEN ImportedRef ELSE s.product]

UnsafeSplitImport ==
    /\ UnsafeMode = "split_import"
    /\ ~s.legacy
    /\ s.architecture = "ArchitectQueued"
    /\ PristinePlan
    /\ s' = [s EXCEPT !.architecture = "ReviewQueued", !.importRecorded = TRUE]

Next ==
    \/ \E ref \in Refs: ImportNew(ref) \/ RecoverLegacy(ref) \/ ReplayExactImport(ref)
    \/ ImportRollback \/ StartReviewer \/ ReviewerPassed \/ HumanEdit
    \/ StartArchitect \/ SubmitFreshProduct \/ HumanAccept \/ InstallGraph
    \/ UnsafeSplitImport

Spec == Init /\ [][Next]_vars

Cycle == INSTANCE ProduceCheckCycle WITH
    MaxGeneration <- 2, CycleKind <- "Plan",
    state <- s.plan, generation <- s.generation, activeSlot <- s.slot,
    productGeneration <- s.productGeneration,
    verdictGeneration <- s.verdictGeneration, resumeState <- "ProducerReady"

RefinesProduceCheckCycle == Cycle!Spec

TypeOK ==
    /\ s.legacy \in BOOLEAN
    /\ s.evidence \in EvidenceCases
    /\ s.recovered \in BOOLEAN
    /\ s.importRecorded \in BOOLEAN
    /\ s.architecture \in {"ArchitectQueued", "ArchitectRunning", "ReviewQueued",
        "Reviewing", "HumanReview", "Accepted"}
    /\ Cycle!TypeOK
    /\ s.product \in Refs
    /\ s.acceptedProduct \in Refs
    /\ s.producerAdmissions \in 0..1
    /\ s.revision \in 1..2
    /\ s.workflowRevision \in 1..2
    /\ s.installed \in Refs

NewImportIsAtomic ==
    ~s.legacy /\ s.architecture = "ReviewQueued" /\ s.generation = 1 =>
        /\ s.plan = "CheckerReady"
        /\ s.product = ImportedRef

RecoveryRequiresExactEvidence ==
    s.recovered => s.importRecorded /\ s.evidence = GoodEvidence
ImportEventAndBindingCommitTogether ==
    ~s.legacy => (s.importRecorded <=> s.plan # "ProducerReady")
NoSyntheticProducer == s.generation = 1 => s.producerAdmissions = 0
ReviewerRequiresBoundProduct ==
    s.architecture \in {"Reviewing", "HumanReview", "Accepted"} =>
        /\ s.product \in {ImportedRef, FreshRef}
        /\ s.productGeneration = s.generation
ReviewAndAcceptanceAreCurrent == Cycle!AcceptedIsCurrent
ExactlyOneRunningSlot == Cycle!ExactlyOneRunningSlot
RevisionPointersStayAtomic == s.workflowRevision = s.revision
PublicAcceptanceRequiresCurrentGraph ==
    s.architecture = "Accepted" => GraphOwner(s.product) = "NewWorkflowGraph"
InstalledGraphBelongsToWorkflow ==
    s.installed # NoRef => GraphOwner(s.installed) = "NewWorkflowGraph"
FreshGraphHasIndependentGeneration ==
    s.installed = FreshRef =>
        /\ s.generation = 2
        /\ GraphGeneration(s.installed) = 1
        /\ s.producerAdmissions = 1
        /\ s.architecture = "Accepted"
        /\ s.product = s.acceptedProduct

=============================================================================
