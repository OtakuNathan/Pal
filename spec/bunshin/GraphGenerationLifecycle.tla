-------------------- MODULE GraphGenerationLifecycle --------------------
EXTENDS Naturals, FiniteSets

CONSTANT MaxRevisions, MaxGraphs

PreparationNodes == {1, 2}

Phases == {"Authoring", "HumanReview", "Accepted", "Preparing", "Installed"}

VARIABLES architectureRevision,
          graphGeneration,
          candidateGeneration,
          candidateAuthorityRevision,
          candidateAuthorityValid,
          authorityRejectionSeen,
          installedGenerations,
          installedFromRevision,
          installedAuthorityRevision,
          phase, activeGeneration, preparedNodes, settledNodes, eligibleReuse, carriedNodes,
          parentControl, pinnedEpochGeneration, controlGraph, controlActive

vars == <<architectureRevision, graphGeneration, candidateGeneration,
          candidateAuthorityRevision, candidateAuthorityValid,
          authorityRejectionSeen,
          installedGenerations, installedFromRevision,
          installedAuthorityRevision, phase, activeGeneration, preparedNodes, settledNodes, eligibleReuse, carriedNodes,
          parentControl, pinnedEpochGeneration, controlGraph, controlActive>>

Init ==
    /\ architectureRevision = 1
    /\ graphGeneration = 0
    /\ candidateGeneration = 0
    /\ candidateAuthorityRevision = 0
    /\ candidateAuthorityValid = FALSE
    /\ authorityRejectionSeen = FALSE
    /\ installedGenerations = {}
    /\ installedFromRevision = 0
    /\ installedAuthorityRevision = 0
    /\ phase = "Authoring"
    /\ activeGeneration = 0
    /\ preparedNodes = {}
    /\ settledNodes = {}
    /\ eligibleReuse = {}
    /\ carriedNodes = {}
    /\ parentControl = "Run"
    /\ pinnedEpochGeneration = 0
    /\ controlGraph = 0
    /\ controlActive = 0

SubmitArchitecture ==
    /\ parentControl = "Run"
    /\ phase = "Authoring"
    /\ graphGeneration < MaxGraphs
    /\ candidateGeneration' = graphGeneration + 1
    /\ candidateAuthorityRevision' = architectureRevision
    /\ candidateAuthorityValid' = TRUE
    /\ phase' = "HumanReview"
    /\ UNCHANGED <<architectureRevision, graphGeneration,
                    authorityRejectionSeen,
                    installedGenerations, installedFromRevision,
                    installedAuthorityRevision>>
    /\ UNCHANGED <<activeGeneration, preparedNodes, settledNodes, eligibleReuse, carriedNodes>>
    /\ UNCHANGED <<parentControl, controlGraph, controlActive, pinnedEpochGeneration>>

RejectInvalidAuthority ==
    /\ parentControl = "Run"
    /\ phase = "Authoring"
    /\ ~authorityRejectionSeen
    /\ authorityRejectionSeen' = TRUE
    /\ UNCHANGED <<architectureRevision, graphGeneration,
                    candidateGeneration, candidateAuthorityRevision,
                    candidateAuthorityValid, installedGenerations,
                    installedFromRevision, installedAuthorityRevision, phase>>
    /\ UNCHANGED <<activeGeneration, preparedNodes, settledNodes, eligibleReuse, carriedNodes>>
    /\ UNCHANGED <<parentControl, controlGraph, controlActive, pinnedEpochGeneration>>

HumanEdit ==
    /\ parentControl = "Run"
    /\ phase = "HumanReview"
    /\ architectureRevision < MaxRevisions
    /\ architectureRevision' = architectureRevision + 1
    /\ candidateGeneration' = 0
    /\ candidateAuthorityRevision' = 0
    /\ candidateAuthorityValid' = FALSE
    /\ authorityRejectionSeen' = FALSE
    /\ phase' = "Authoring"
    /\ UNCHANGED <<graphGeneration, installedGenerations,
                    installedFromRevision, installedAuthorityRevision>>
    /\ UNCHANGED <<activeGeneration, preparedNodes, settledNodes, eligibleReuse, carriedNodes>>
    /\ UNCHANGED <<parentControl, controlGraph, controlActive, pinnedEpochGeneration>>

HumanAccept ==
    /\ parentControl = "Run"
    /\ phase = "HumanReview"
    /\ phase' = "Accepted"
    /\ UNCHANGED <<architectureRevision, graphGeneration,
                    candidateGeneration, candidateAuthorityRevision,
                    candidateAuthorityValid, authorityRejectionSeen,
                    installedGenerations,
                    installedFromRevision, installedAuthorityRevision>>
    /\ UNCHANGED <<activeGeneration, preparedNodes, settledNodes, eligibleReuse, carriedNodes>>
    /\ UNCHANGED <<parentControl, controlGraph, controlActive, pinnedEpochGeneration>>

InstallAcceptedGraph ==
    /\ parentControl = "Run"
    /\ phase = "Accepted"
    /\ candidateGeneration = graphGeneration + 1
    /\ candidateAuthorityValid
    /\ candidateAuthorityRevision = architectureRevision
    /\ graphGeneration' = candidateGeneration
    /\ pinnedEpochGeneration' = candidateGeneration
    /\ installedGenerations' =
          installedGenerations \union {candidateGeneration}
    /\ installedFromRevision' = architectureRevision
    /\ installedAuthorityRevision' = candidateAuthorityRevision
    /\ candidateGeneration' = 0
    /\ candidateAuthorityRevision' = 0
    /\ candidateAuthorityValid' = FALSE
    /\ phase' = "Preparing"
    /\ UNCHANGED <<architectureRevision, authorityRejectionSeen>>
    /\ preparedNodes' = {}
    /\ settledNodes' = {}
    /\ carriedNodes' = {}
    /\ eligibleReuse' \in SUBSET PreparationNodes
    /\ UNCHANGED activeGeneration
    /\ UNCHANGED <<parentControl, controlGraph, controlActive>>

\* GraphIR and its prepared Epoch are pinned atomically. The parent owns
\* this STARTING generation even while the active pointer names its source.
\* Control freezes preparation and active-generation selection until resume.
PrepareNode(n) ==
    /\ parentControl = "Run"
    /\ phase = "Preparing"
    /\ n \in PreparationNodes \ preparedNodes
    /\ preparedNodes' = preparedNodes \cup {n}
    /\ UNCHANGED <<architectureRevision, graphGeneration, candidateGeneration,
                   candidateAuthorityRevision, candidateAuthorityValid, authorityRejectionSeen,
                   installedGenerations, installedFromRevision, installedAuthorityRevision,
                   phase, activeGeneration, settledNodes, eligibleReuse, carriedNodes>>
    /\ UNCHANGED <<parentControl, controlGraph, controlActive, pinnedEpochGeneration>>

SettleNode(n, reuse) ==
    /\ parentControl = "Run"
    /\ phase = "Preparing"
    /\ n \in preparedNodes \ settledNodes
    /\ (reuse => n \in eligibleReuse)
    /\ settledNodes' = settledNodes \cup {n}
    /\ carriedNodes' = IF reuse THEN carriedNodes \cup {n} ELSE carriedNodes
    /\ UNCHANGED <<architectureRevision, graphGeneration, candidateGeneration,
                   candidateAuthorityRevision, candidateAuthorityValid, authorityRejectionSeen,
                   installedGenerations, installedFromRevision, installedAuthorityRevision,
                   phase, activeGeneration, preparedNodes, eligibleReuse>>
    /\ UNCHANGED <<parentControl, controlGraph, controlActive, pinnedEpochGeneration>>

ActivateGraph ==
    /\ parentControl = "Run"
    /\ phase = "Preparing"
    /\ preparedNodes = PreparationNodes
    /\ settledNodes = PreparationNodes
    /\ activeGeneration' = graphGeneration
    /\ phase' = "Installed"
    /\ UNCHANGED <<architectureRevision, graphGeneration, candidateGeneration,
                   candidateAuthorityRevision, candidateAuthorityValid, authorityRejectionSeen,
                   installedGenerations, installedFromRevision, installedAuthorityRevision,
                   preparedNodes, settledNodes, eligibleReuse, carriedNodes>>
    /\ UNCHANGED <<parentControl, controlGraph, controlActive, pinnedEpochGeneration>>

BeginReplan ==
    /\ parentControl = "Run"
    /\ phase = "Installed"
    /\ architectureRevision < MaxRevisions
    /\ graphGeneration < MaxGraphs
    /\ architectureRevision' = architectureRevision + 1
    /\ candidateGeneration' = 0
    /\ candidateAuthorityRevision' = 0
    /\ candidateAuthorityValid' = FALSE
    /\ authorityRejectionSeen' = FALSE
    /\ phase' = "Authoring"
    /\ UNCHANGED <<graphGeneration, installedGenerations,
                    installedFromRevision, installedAuthorityRevision>>
    /\ UNCHANGED <<activeGeneration, preparedNodes, settledNodes, eligibleReuse, carriedNodes>>
    /\ UNCHANGED <<parentControl, controlGraph, controlActive, pinnedEpochGeneration>>

ControlSnapshot ==
    /\ controlGraph' = graphGeneration
    /\ controlActive' = activeGeneration
    /\ UNCHANGED <<architectureRevision, graphGeneration, candidateGeneration,
                    candidateAuthorityRevision, candidateAuthorityValid, authorityRejectionSeen,
                    installedGenerations, installedFromRevision, installedAuthorityRevision,
                    phase, activeGeneration, preparedNodes, settledNodes, eligibleReuse, carriedNodes,
                    pinnedEpochGeneration>>

RequestParentPause ==
    /\ parentControl = "Run"
    /\ parentControl' = "Pause"
    /\ ControlSnapshot

RequestParentCancel ==
    /\ parentControl # "Cancel"
    /\ parentControl' = "Cancel"
    /\ ControlSnapshot

ResumeParent ==
    /\ parentControl = "Pause"
    /\ parentControl' = "Run"
    /\ ControlSnapshot

Next ==
    \/ SubmitArchitecture
    \/ RejectInvalidAuthority
    \/ HumanEdit
    \/ HumanAccept
    \/ InstallAcceptedGraph
    \/ \E n \in PreparationNodes : PrepareNode(n)
    \/ \E n \in PreparationNodes, reuse \in BOOLEAN : SettleNode(n, reuse)
    \/ ActivateGraph
    \/ BeginReplan
    \/ RequestParentPause
    \/ RequestParentCancel
    \/ ResumeParent

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ architectureRevision \in 1..MaxRevisions
    /\ graphGeneration \in 0..MaxGraphs
    /\ candidateGeneration \in 0..MaxGraphs
    /\ candidateAuthorityRevision \in 0..MaxRevisions
    /\ candidateAuthorityValid \in BOOLEAN
    /\ authorityRejectionSeen \in BOOLEAN
    /\ installedGenerations \subseteq 1..MaxGraphs
    /\ installedFromRevision \in 0..MaxRevisions
    /\ installedAuthorityRevision \in 0..MaxRevisions
    /\ phase \in Phases
    /\ activeGeneration \in 0..graphGeneration
    /\ preparedNodes \subseteq PreparationNodes
    /\ settledNodes \subseteq preparedNodes
    /\ eligibleReuse \subseteq PreparationNodes
    /\ carriedNodes \subseteq settledNodes
    /\ parentControl \in {"Run", "Pause", "Cancel"}
    /\ pinnedEpochGeneration \in 0..MaxGraphs
    /\ controlGraph \in 0..MaxGraphs
    /\ controlActive \in 0..MaxGraphs

CandidateIsNextAppend ==
    candidateGeneration = 0 \/ candidateGeneration = graphGeneration + 1

InstalledGraphIsGapFree ==
    installedGenerations = 1..graphGeneration

FirstCandidateIsGenerationOne ==
    graphGeneration = 0 /\ candidateGeneration # 0
        => candidateGeneration = 1

AcceptedRevisionHasInstallableCandidate ==
    phase = "Accepted" =>
        /\ candidateGeneration = graphGeneration + 1
        /\ candidateAuthorityValid
        /\ candidateAuthorityRevision = architectureRevision

DiscardedRevisionsDoNotConsumeGraphGenerations ==
    phase = "Authoring" =>
        /\ candidateGeneration = 0
        /\ candidateAuthorityRevision = 0
        /\ ~candidateAuthorityValid

RejectedAuthorityHasNoCandidate ==
    phase = "Authoring" /\ authorityRejectionSeen =>
        /\ candidateGeneration = 0
        /\ candidateAuthorityRevision = 0
        /\ ~candidateAuthorityValid

InstalledGraphCameFromAnAuthoredRevision ==
    graphGeneration > 0
        => installedFromRevision \in 1..architectureRevision

InstalledAuthorityIsGenerationBound ==
    /\ (graphGeneration = 0 => installedAuthorityRevision = 0)
    /\ (graphGeneration > 0 =>
        installedAuthorityRevision = installedFromRevision)

ActivatedOnlyAfterPreparation ==
    /\ (phase = "Installed" => activeGeneration = graphGeneration /\ settledNodes = PreparationNodes)
    /\ (phase = "Preparing" => activeGeneration < graphGeneration)

GraphInstallationPinsEpoch == pinnedEpochGeneration = graphGeneration

ParentControlFreezesGeneration ==
    parentControl # "Run" =>
        /\ graphGeneration = controlGraph
        /\ activeGeneration = controlActive

CarriedReceiptsRemainEligible == carriedNodes \subseteq eligibleReuse

=======================================================================
