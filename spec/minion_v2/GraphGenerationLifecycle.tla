-------------------- MODULE GraphGenerationLifecycle --------------------
EXTENDS Naturals, FiniteSets

CONSTANT MaxRevisions, MaxGraphs

Phases == {"Authoring", "HumanReview", "Accepted", "Installed"}

VARIABLES architectureRevision,
          graphGeneration,
          candidateGeneration,
          candidateAuthorityRevision,
          candidateAuthorityValid,
          authorityRejectionSeen,
          installedGenerations,
          installedFromRevision,
          installedAuthorityRevision,
          phase

vars == <<architectureRevision, graphGeneration, candidateGeneration,
          candidateAuthorityRevision, candidateAuthorityValid,
          authorityRejectionSeen,
          installedGenerations, installedFromRevision,
          installedAuthorityRevision, phase>>

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

SubmitArchitecture ==
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

RejectInvalidAuthority ==
    /\ phase = "Authoring"
    /\ ~authorityRejectionSeen
    /\ authorityRejectionSeen' = TRUE
    /\ UNCHANGED <<architectureRevision, graphGeneration,
                    candidateGeneration, candidateAuthorityRevision,
                    candidateAuthorityValid, installedGenerations,
                    installedFromRevision, installedAuthorityRevision, phase>>

HumanEdit ==
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

HumanAccept ==
    /\ phase = "HumanReview"
    /\ phase' = "Accepted"
    /\ UNCHANGED <<architectureRevision, graphGeneration,
                    candidateGeneration, candidateAuthorityRevision,
                    candidateAuthorityValid, authorityRejectionSeen,
                    installedGenerations,
                    installedFromRevision, installedAuthorityRevision>>

InstallAcceptedGraph ==
    /\ phase = "Accepted"
    /\ candidateGeneration = graphGeneration + 1
    /\ candidateAuthorityValid
    /\ candidateAuthorityRevision = architectureRevision
    /\ graphGeneration' = candidateGeneration
    /\ installedGenerations' =
          installedGenerations \union {candidateGeneration}
    /\ installedFromRevision' = architectureRevision
    /\ installedAuthorityRevision' = candidateAuthorityRevision
    /\ candidateGeneration' = 0
    /\ candidateAuthorityRevision' = 0
    /\ candidateAuthorityValid' = FALSE
    /\ phase' = "Installed"
    /\ UNCHANGED <<architectureRevision, authorityRejectionSeen>>

BeginReplan ==
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

Next ==
    \/ SubmitArchitecture
    \/ RejectInvalidAuthority
    \/ HumanEdit
    \/ HumanAccept
    \/ InstallAcceptedGraph
    \/ BeginReplan

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

=======================================================================
