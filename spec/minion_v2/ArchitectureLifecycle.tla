---------------------- MODULE ArchitectureLifecycle ----------------------
EXTENDS Naturals, TLC

CONSTANT MaxRevision

States == {
    "ArchitectQueued", "ArchitectRunning", "ArchitectQuiescing",
    "ArchitectSnapshotting", "ReviewQueued", "Reviewing", "HumanReview",
    "ClarificationPending", "PauseRequested", "Paused", "CancelRequested",
    "Accepted", "Rejected", "Superseded", "Cancelled", "Triage"
}

TerminalStates == {"Accepted", "Rejected", "Superseded", "Cancelled"}
WorkerStates == {
    "ArchitectQueued", "ArchitectRunning", "ArchitectQuiescing",
    "ArchitectSnapshotting", "ReviewQueued", "Reviewing"
}
WaitStates == {"HumanReview", "ClarificationPending", "Paused", "Triage"}
DecisionStates == {"None", "Issued", "Consumed"}
FailureKinds == {
    "None", "WorkerFailure", "EffectFailure", "OrphanedWorker",
    "QuiesceFailure", "SnapshotFailure"
}
ResumeStates == WorkerStates \cup {
    "HumanReview", "ClarificationPending", "PauseRequested", "CancelRequested"
}

VARIABLES
    state,
    revision,
    manifestVersion,
    architectLease,
    reviewerLease,
    workspaceStable,
    decisionState,
    tokenRevision,
    tokenManifest,
    acceptedRevision,
    supersededCount,
    revisionCreatePending,
    pauseResume,
    triageResume,
    failureKind,
    managerUp,
    staleDecisionRejects

vars == <<
    state, revision, manifestVersion, architectLease, reviewerLease,
    workspaceStable, decisionState, tokenRevision, tokenManifest,
    acceptedRevision, supersededCount, revisionCreatePending, pauseResume,
    triageResume, failureKind, managerUp, staleDecisionRejects
>>

SafeResume(s) ==
    CASE s = "ArchitectRunning" -> "ArchitectQueued"
      [] s = "ArchitectQuiescing" -> "ArchitectQueued"
      [] s = "ArchitectSnapshotting" -> "ArchitectQueued"
      [] s = "Reviewing" -> "ReviewQueued"
      [] s \in ResumeStates -> s
      [] OTHER -> "ArchitectQueued"

Init ==
    /\ state = "ArchitectQueued"
    /\ revision = 1
    /\ manifestVersion = 0
    /\ architectLease = FALSE
    /\ reviewerLease = FALSE
    /\ workspaceStable = TRUE
    /\ decisionState = "None"
    /\ tokenRevision = 0
    /\ tokenManifest = 0
    /\ acceptedRevision = 0
    /\ supersededCount = 0
    /\ revisionCreatePending = FALSE
    /\ pauseResume = "ArchitectQueued"
    /\ triageResume = "ArchitectQueued"
    /\ failureKind = "None"
    /\ managerUp = TRUE
    /\ staleDecisionRejects = 0

StartArchitect ==
    /\ managerUp
    /\ state = "ArchitectQueued"
    /\ state' = "ArchitectRunning"
    /\ architectLease' = TRUE
    /\ workspaceStable' = FALSE
    /\ UNCHANGED <<revision, manifestVersion, reviewerLease, decisionState,
        tokenRevision, tokenManifest, acceptedRevision, supersededCount,
        revisionCreatePending, pauseResume, triageResume, failureKind,
        managerUp, staleDecisionRejects>>

SubmitSkeleton ==
    /\ managerUp
    /\ state = "ArchitectRunning"
    /\ architectLease
    /\ state' = "ArchitectQuiescing"
    /\ architectLease' = FALSE
    /\ UNCHANGED <<revision, manifestVersion, reviewerLease, workspaceStable,
        decisionState, tokenRevision, tokenManifest, acceptedRevision,
        supersededCount, revisionCreatePending, pauseResume, triageResume,
        failureKind, managerUp, staleDecisionRejects>>

QuiesceArchitect ==
    /\ managerUp
    /\ state = "ArchitectQuiescing"
    /\ ~architectLease
    /\ state' = "ArchitectSnapshotting"
    /\ workspaceStable' = TRUE
    /\ UNCHANGED <<revision, manifestVersion, architectLease, reviewerLease, decisionState,
        tokenRevision, tokenManifest, acceptedRevision, supersededCount,
        revisionCreatePending, pauseResume, triageResume, failureKind,
        managerUp, staleDecisionRejects>>

SnapshotSkeleton ==
    /\ managerUp
    /\ state = "ArchitectSnapshotting"
    /\ workspaceStable
    /\ state' = "ReviewQueued"
    /\ manifestVersion' = revision
    /\ UNCHANGED <<revision, architectLease, reviewerLease, workspaceStable,
        decisionState, tokenRevision, tokenManifest, acceptedRevision,
        supersededCount, revisionCreatePending, pauseResume, triageResume,
        failureKind, managerUp, staleDecisionRejects>>

RejectSnapshot ==
    /\ managerUp
    /\ state = "ArchitectSnapshotting"
    /\ state' = "ArchitectQueued"
    /\ workspaceStable' = TRUE
    /\ UNCHANGED <<revision, manifestVersion, architectLease, reviewerLease,
        decisionState, tokenRevision, tokenManifest, acceptedRevision,
        supersededCount, revisionCreatePending, pauseResume, triageResume,
        failureKind, managerUp, staleDecisionRejects>>

StartReview ==
    /\ managerUp
    /\ state = "ReviewQueued"
    /\ manifestVersion = revision
    /\ state' = "Reviewing"
    /\ reviewerLease' = TRUE
    /\ UNCHANGED <<revision, manifestVersion, architectLease, workspaceStable,
        decisionState, tokenRevision, tokenManifest, acceptedRevision,
        supersededCount, revisionCreatePending, pauseResume, triageResume,
        failureKind, managerUp, staleDecisionRejects>>

ReviewFailed ==
    /\ managerUp
    /\ state = "Reviewing"
    /\ reviewerLease
    /\ state' = "ArchitectQueued"
    /\ reviewerLease' = FALSE
    /\ decisionState' = "None"
    /\ UNCHANGED <<revision, manifestVersion, architectLease, workspaceStable,
        tokenRevision, tokenManifest, acceptedRevision, supersededCount,
        revisionCreatePending, pauseResume, triageResume, failureKind,
        managerUp, staleDecisionRejects>>

ReviewPassed ==
    /\ managerUp
    /\ state = "Reviewing"
    /\ reviewerLease
    /\ manifestVersion = revision
    /\ state' = "HumanReview"
    /\ reviewerLease' = FALSE
    /\ decisionState' = "Issued"
    /\ tokenRevision' = revision
    /\ tokenManifest' = manifestVersion
    /\ UNCHANGED <<revision, manifestVersion, architectLease, workspaceStable,
        acceptedRevision, supersededCount, revisionCreatePending, pauseResume,
        triageResume, failureKind, managerUp, staleDecisionRejects>>

RequestClarification ==
    /\ managerUp
    /\ state = "ArchitectRunning"
    /\ state' = "ClarificationPending"
    /\ architectLease' = FALSE
    /\ UNCHANGED <<revision, manifestVersion, reviewerLease, workspaceStable,
        decisionState, tokenRevision, tokenManifest, acceptedRevision,
        supersededCount, revisionCreatePending, pauseResume, triageResume,
        failureKind, managerUp, staleDecisionRejects>>

ProvideClarification ==
    /\ state = "ClarificationPending"
    /\ state' = "ArchitectQueued"
    /\ UNCHANGED <<revision, manifestVersion, architectLease, reviewerLease,
        workspaceStable, decisionState, tokenRevision, tokenManifest,
        acceptedRevision, supersededCount, revisionCreatePending, pauseResume,
        triageResume, failureKind, managerUp, staleDecisionRejects>>

ValidDecision ==
    /\ state = "HumanReview"
    /\ decisionState = "Issued"
    /\ tokenRevision = revision
    /\ tokenManifest = manifestVersion

HumanAccept ==
    /\ ValidDecision
    /\ state' = "Accepted"
    /\ decisionState' = "Consumed"
    /\ acceptedRevision' = revision
    /\ UNCHANGED <<revision, manifestVersion, architectLease, reviewerLease,
        workspaceStable, tokenRevision, tokenManifest, supersededCount,
        revisionCreatePending, pauseResume, triageResume, failureKind,
        managerUp, staleDecisionRejects>>

HumanReject ==
    /\ ValidDecision
    /\ state' = "Rejected"
    /\ decisionState' = "Consumed"
    /\ UNCHANGED <<revision, manifestVersion, architectLease, reviewerLease,
        workspaceStable, tokenRevision, tokenManifest, acceptedRevision,
        supersededCount, revisionCreatePending, pauseResume, triageResume,
        failureKind, managerUp, staleDecisionRejects>>

HumanEdit ==
    /\ ValidDecision
    /\ revision < MaxRevision
    /\ state' = "Superseded"
    /\ decisionState' = "Consumed"
    /\ supersededCount' = supersededCount + 1
    /\ revisionCreatePending' = TRUE
    /\ UNCHANGED <<revision, manifestVersion, architectLease, reviewerLease,
        workspaceStable, tokenRevision, tokenManifest, acceptedRevision,
        pauseResume, triageResume, failureKind, managerUp, staleDecisionRejects>>

CreateNextRevision ==
    /\ managerUp
    /\ state = "Superseded"
    /\ revisionCreatePending
    /\ revision < MaxRevision
    /\ state' = "ArchitectQueued"
    /\ revision' = revision + 1
    /\ manifestVersion' = 0
    /\ workspaceStable' = TRUE
    /\ decisionState' = "None"
    /\ tokenRevision' = 0
    /\ tokenManifest' = 0
    /\ revisionCreatePending' = FALSE
    /\ failureKind' = "None"
    /\ UNCHANGED <<architectLease, reviewerLease, acceptedRevision,
        supersededCount, pauseResume, triageResume, managerUp,
        staleDecisionRejects>>

RejectStaleDecision ==
    /\ decisionState # "None"
    /\ ~(ValidDecision)
    /\ staleDecisionRejects < MaxRevision
    /\ staleDecisionRejects' = staleDecisionRejects + 1
    /\ UNCHANGED <<state, revision, manifestVersion, architectLease,
        reviewerLease, workspaceStable, decisionState, tokenRevision,
        tokenManifest, acceptedRevision, supersededCount,
        revisionCreatePending, pauseResume, triageResume, failureKind,
        managerUp>>

RequestPause ==
    /\ state \notin TerminalStates \cup {"PauseRequested", "Paused", "CancelRequested", "Triage"}
    /\ state' = "PauseRequested"
    /\ pauseResume' = SafeResume(state)
    /\ architectLease' = FALSE
    /\ reviewerLease' = FALSE
    /\ UNCHANGED <<revision, manifestVersion, workspaceStable, decisionState,
        tokenRevision, tokenManifest, acceptedRevision, supersededCount,
        revisionCreatePending, triageResume, failureKind, managerUp,
        staleDecisionRejects>>

ConfirmPause ==
    /\ managerUp
    /\ state = "PauseRequested"
    /\ state' = "Paused"
    /\ UNCHANGED <<revision, manifestVersion, architectLease, reviewerLease,
        workspaceStable, decisionState, tokenRevision, tokenManifest,
        acceptedRevision, supersededCount, revisionCreatePending, pauseResume,
        triageResume, failureKind, managerUp, staleDecisionRejects>>

Resume ==
    /\ managerUp
    /\ state = "Paused"
    /\ state' = pauseResume
    /\ UNCHANGED <<revision, manifestVersion, architectLease, reviewerLease,
        workspaceStable, decisionState, tokenRevision, tokenManifest,
        acceptedRevision, supersededCount, revisionCreatePending, pauseResume,
        triageResume, failureKind, managerUp, staleDecisionRejects>>

RequestCancel ==
    /\ state \notin TerminalStates \cup {"CancelRequested"}
    /\ state' = "CancelRequested"
    /\ architectLease' = FALSE
    /\ reviewerLease' = FALSE
    /\ UNCHANGED <<revision, manifestVersion, workspaceStable, decisionState,
        tokenRevision, tokenManifest, acceptedRevision, supersededCount,
        revisionCreatePending, pauseResume, triageResume, failureKind,
        managerUp, staleDecisionRejects>>

ConfirmCancel ==
    /\ managerUp
    /\ state = "CancelRequested"
    /\ state' = "Cancelled"
    /\ revisionCreatePending' = FALSE
    /\ failureKind' = "None"
    /\ UNCHANGED <<revision, manifestVersion, architectLease, reviewerLease,
        workspaceStable, decisionState, tokenRevision, tokenManifest,
        acceptedRevision, supersededCount, pauseResume, triageResume,
        managerUp, staleDecisionRejects>>

EnterTriage(kind) ==
    /\ kind \in FailureKinds \ {"None"}
    /\ state \notin TerminalStates \cup {"Paused", "Triage"}
    /\ (kind = "WorkerFailure" => state \in {"ArchitectRunning", "Reviewing"})
    /\ (kind = "QuiesceFailure" => state = "ArchitectQuiescing")
    /\ (kind = "SnapshotFailure" => state = "ArchitectSnapshotting")
    /\ state' = "Triage"
    /\ triageResume' = SafeResume(state)
    /\ failureKind' = kind
    /\ architectLease' = FALSE
    /\ reviewerLease' = FALSE
    /\ UNCHANGED <<revision, manifestVersion, workspaceStable, decisionState,
        tokenRevision, tokenManifest, acceptedRevision, supersededCount,
        revisionCreatePending, pauseResume, managerUp, staleDecisionRejects>>

ResolveTriage ==
    /\ managerUp
    /\ state = "Triage"
    /\ failureKind # "None"
    /\ state' = triageResume
    /\ failureKind' = "None"
    /\ UNCHANGED <<revision, manifestVersion, architectLease, reviewerLease,
        workspaceStable, decisionState, tokenRevision, tokenManifest,
        acceptedRevision, supersededCount, revisionCreatePending, pauseResume,
        triageResume, managerUp, staleDecisionRejects>>

RefreshTriage(kind) ==
    /\ kind \in FailureKinds \ {"None"}
    /\ state = "Triage"
    /\ failureKind' = kind
    /\ UNCHANGED <<state, revision, manifestVersion, architectLease,
        reviewerLease, workspaceStable, decisionState, tokenRevision,
        tokenManifest, acceptedRevision, supersededCount,
        revisionCreatePending, pauseResume, triageResume, managerUp,
        staleDecisionRejects>>

CrashManager ==
    /\ managerUp
    /\ managerUp' = FALSE
    /\ UNCHANGED <<state, revision, manifestVersion, architectLease,
        reviewerLease, workspaceStable, decisionState, tokenRevision,
        tokenManifest, acceptedRevision, supersededCount,
        revisionCreatePending, pauseResume, triageResume, failureKind,
        staleDecisionRejects>>

RestartManager ==
    /\ ~managerUp
    /\ managerUp' = TRUE
    /\ UNCHANGED <<state, revision, manifestVersion, architectLease,
        reviewerLease, workspaceStable, decisionState, tokenRevision,
        tokenManifest, acceptedRevision, supersededCount,
        revisionCreatePending, pauseResume, triageResume, failureKind,
        staleDecisionRejects>>

InternalProgress ==
    \/ StartArchitect
    \/ QuiesceArchitect
    \/ SnapshotSkeleton
    \/ StartReview
    \/ CreateNextRevision
    \/ ConfirmPause
    \/ ConfirmCancel
    \/ ResolveTriage

Next ==
    \/ StartArchitect
    \/ SubmitSkeleton
    \/ QuiesceArchitect
    \/ SnapshotSkeleton
    \/ RejectSnapshot
    \/ StartReview
    \/ ReviewFailed
    \/ ReviewPassed
    \/ RequestClarification
    \/ ProvideClarification
    \/ HumanAccept
    \/ HumanReject
    \/ HumanEdit
    \/ CreateNextRevision
    \/ RejectStaleDecision
    \/ RequestPause
    \/ ConfirmPause
    \/ Resume
    \/ RequestCancel
    \/ ConfirmCancel
    \/ EnterTriage("WorkerFailure")
    \/ EnterTriage("EffectFailure")
    \/ EnterTriage("OrphanedWorker")
    \/ EnterTriage("QuiesceFailure")
    \/ EnterTriage("SnapshotFailure")
    \/ RefreshTriage("WorkerFailure")
    \/ RefreshTriage("EffectFailure")
    \/ RefreshTriage("OrphanedWorker")
    \/ RefreshTriage("QuiesceFailure")
    \/ RefreshTriage("SnapshotFailure")
    \/ ResolveTriage
    \/ CrashManager
    \/ RestartManager

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(RestartManager)
    /\ SF_vars(InternalProgress)
    /\ SF_vars(CreateNextRevision)
    /\ SF_vars(ConfirmPause)
    /\ SF_vars(ConfirmCancel)
    /\ SF_vars(ResolveTriage)

TypeOK ==
    /\ state \in States
    /\ revision \in 1..MaxRevision
    /\ manifestVersion \in 0..MaxRevision
    /\ architectLease \in BOOLEAN
    /\ reviewerLease \in BOOLEAN
    /\ workspaceStable \in BOOLEAN
    /\ decisionState \in DecisionStates
    /\ tokenRevision \in 0..MaxRevision
    /\ tokenManifest \in 0..MaxRevision
    /\ acceptedRevision \in 0..MaxRevision
    /\ supersededCount \in 0..MaxRevision
    /\ revisionCreatePending \in BOOLEAN
    /\ pauseResume \in ResumeStates
    /\ triageResume \in ResumeStates
    /\ failureKind \in FailureKinds
    /\ managerUp \in BOOLEAN
    /\ staleDecisionRejects \in 0..MaxRevision

LeaseOwnership ==
    /\ architectLease => state = "ArchitectRunning"
    /\ reviewerLease => state = "Reviewing"
    /\ ~(architectLease /\ reviewerLease)

TerminalClosesWorkers ==
    state \in TerminalStates => ~architectLease /\ ~reviewerLease

AcceptedByCurrentToken ==
    state = "Accepted" =>
        /\ decisionState = "Consumed"
        /\ acceptedRevision = revision
        /\ tokenRevision = revision
        /\ tokenManifest = manifestVersion

EditClosesOldRevision ==
    revisionCreatePending =>
        /\ state = "Superseded"
        /\ decisionState = "Consumed"
        /\ supersededCount > 0

HumanWaitHasCurrentManifest ==
    state = "HumanReview" =>
        /\ manifestVersion = revision
        /\ decisionState = "Issued"
        /\ tokenRevision = revision
        /\ tokenManifest = manifestVersion

TriageIsExplicit ==
    state = "Triage" =>
        /\ failureKind # "None"
        /\ ~architectLease
        /\ ~reviewerLease

RevisionCreationEventuallyRuns ==
    revisionCreatePending ~> ~revisionCreatePending

PauseEventuallySettles ==
    state = "PauseRequested" ~>
        state \in {"Paused", "CancelRequested", "Cancelled", "Triage"}

CancelEventuallySettles ==
    state = "CancelRequested" ~> state = "Cancelled"

=============================================================================
