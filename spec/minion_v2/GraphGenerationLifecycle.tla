-------------------- MODULE GraphGenerationLifecycle --------------------
EXTENDS Naturals, FiniteSets

CONSTANT MaxRevisions, MaxGraphs

Phases == {"Authoring", "HumanReview", "Accepted", "Installed"}

VARIABLES architectureRevision,
          graphGeneration,
          candidateGeneration,
          installedGenerations,
          installedFromRevision,
          phase

vars == <<architectureRevision, graphGeneration, candidateGeneration,
          installedGenerations, installedFromRevision, phase>>

Init ==
    /\ architectureRevision = 1
    /\ graphGeneration = 0
    /\ candidateGeneration = 0
    /\ installedGenerations = {}
    /\ installedFromRevision = 0
    /\ phase = "Authoring"

SubmitArchitecture ==
    /\ phase = "Authoring"
    /\ graphGeneration < MaxGraphs
    /\ candidateGeneration' = graphGeneration + 1
    /\ phase' = "HumanReview"
    /\ UNCHANGED <<architectureRevision, graphGeneration,
                    installedGenerations, installedFromRevision>>

HumanEdit ==
    /\ phase = "HumanReview"
    /\ architectureRevision < MaxRevisions
    /\ architectureRevision' = architectureRevision + 1
    /\ candidateGeneration' = 0
    /\ phase' = "Authoring"
    /\ UNCHANGED <<graphGeneration, installedGenerations,
                    installedFromRevision>>

HumanAccept ==
    /\ phase = "HumanReview"
    /\ phase' = "Accepted"
    /\ UNCHANGED <<architectureRevision, graphGeneration,
                    candidateGeneration, installedGenerations,
                    installedFromRevision>>

InstallAcceptedGraph ==
    /\ phase = "Accepted"
    /\ candidateGeneration = graphGeneration + 1
    /\ graphGeneration' = candidateGeneration
    /\ installedGenerations' =
          installedGenerations \union {candidateGeneration}
    /\ installedFromRevision' = architectureRevision
    /\ candidateGeneration' = 0
    /\ phase' = "Installed"
    /\ UNCHANGED architectureRevision

BeginReplan ==
    /\ phase = "Installed"
    /\ architectureRevision < MaxRevisions
    /\ graphGeneration < MaxGraphs
    /\ architectureRevision' = architectureRevision + 1
    /\ candidateGeneration' = 0
    /\ phase' = "Authoring"
    /\ UNCHANGED <<graphGeneration, installedGenerations,
                    installedFromRevision>>

Next ==
    \/ SubmitArchitecture
    \/ HumanEdit
    \/ HumanAccept
    \/ InstallAcceptedGraph
    \/ BeginReplan

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ architectureRevision \in 1..MaxRevisions
    /\ graphGeneration \in 0..MaxGraphs
    /\ candidateGeneration \in 0..MaxGraphs
    /\ installedGenerations \subseteq 1..MaxGraphs
    /\ installedFromRevision \in 0..MaxRevisions
    /\ phase \in Phases

CandidateIsNextAppend ==
    candidateGeneration = 0 \/ candidateGeneration = graphGeneration + 1

InstalledGraphIsGapFree ==
    installedGenerations = 1..graphGeneration

FirstCandidateIsGenerationOne ==
    graphGeneration = 0 /\ candidateGeneration # 0
        => candidateGeneration = 1

AcceptedRevisionHasInstallableCandidate ==
    phase = "Accepted" => candidateGeneration = graphGeneration + 1

DiscardedRevisionsDoNotConsumeGraphGenerations ==
    phase = "Authoring" => candidateGeneration = 0

InstalledGraphCameFromAnAuthoredRevision ==
    graphGeneration > 0
        => installedFromRevision \in 1..architectureRevision

=======================================================================
