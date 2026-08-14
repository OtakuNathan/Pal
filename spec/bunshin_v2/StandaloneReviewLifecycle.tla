------------------- MODULE StandaloneReviewLifecycle -------------------
EXTENDS TLC

States == {
    "Received", "ReviewQueued", "Reviewing", "ReportReady",
    "PauseRequested", "Paused", "CancelRequested", "Cancelled",
    "Completed", "Triage"
}

TerminalStates == {"Cancelled", "Completed"}
ResumeStates == {
    "Received", "ReviewQueued", "ReportReady", "PauseRequested", "CancelRequested"
}
FailureKinds == {"None", "WorkerFailure", "EffectFailure", "OrphanedWorker"}

VARIABLES
    state,
    reviewerLease,
    reportPersisted,
    reportPublished,
    repairHandoff,
    pauseResume,
    triageResume,
    failureKind,
    managerUp

vars == <<
    state, reviewerLease, reportPersisted, reportPublished, repairHandoff,
    pauseResume, triageResume, failureKind, managerUp
>>

SafeResume(s) ==
    CASE s = "Reviewing" -> "ReviewQueued"
      [] s \in ResumeStates -> s
      [] OTHER -> "Received"

Init ==
    /\ state = "Received"
    /\ reviewerLease = FALSE
    /\ reportPersisted = FALSE
    /\ reportPublished = FALSE
    /\ repairHandoff = FALSE
    /\ pauseResume = "Received"
    /\ triageResume = "Received"
    /\ failureKind = "None"
    /\ managerUp = TRUE

QueueReview ==
    /\ managerUp
    /\ state = "Received"
    /\ state' = "ReviewQueued"
    /\ UNCHANGED <<reviewerLease, reportPersisted, reportPublished,
        repairHandoff, pauseResume, triageResume, failureKind, managerUp>>

StartReview ==
    /\ managerUp
    /\ state = "ReviewQueued"
    /\ state' = "Reviewing"
    /\ reviewerLease' = TRUE
    /\ UNCHANGED <<reportPersisted, reportPublished, repairHandoff,
        pauseResume, triageResume, failureKind, managerUp>>

ProduceReport ==
    /\ managerUp
    /\ state = "Reviewing"
    /\ reviewerLease
    /\ state' = "ReportReady"
    /\ reviewerLease' = FALSE
    /\ reportPersisted' = TRUE
    /\ UNCHANGED <<reportPublished, repairHandoff, pauseResume,
        triageResume, failureKind, managerUp>>

PublishReport ==
    /\ managerUp
    /\ state = "ReportReady"
    /\ reportPersisted
    /\ reportPublished' = TRUE
    /\ UNCHANGED <<state, reviewerLease, reportPersisted, repairHandoff,
        pauseResume, triageResume, failureKind, managerUp>>

AcknowledgeReport ==
    /\ state = "ReportReady"
    /\ reportPersisted
    /\ reportPublished
    /\ state' = "Completed"
    /\ UNCHANGED <<reviewerLease, reportPersisted, reportPublished,
        repairHandoff, pauseResume, triageResume, failureKind, managerUp>>

HandoffRepair ==
    /\ state = "ReportReady"
    /\ reportPersisted
    /\ reportPublished
    /\ state' = "Completed"
    /\ repairHandoff' = TRUE
    /\ UNCHANGED <<reviewerLease, reportPersisted, reportPublished,
        pauseResume, triageResume, failureKind, managerUp>>

RequestPause ==
    /\ state \in {"Received", "ReviewQueued", "Reviewing", "ReportReady"}
    /\ state' = "PauseRequested"
    /\ pauseResume' = SafeResume(state)
    /\ reviewerLease' = FALSE
    /\ UNCHANGED <<reportPersisted, reportPublished, repairHandoff,
        triageResume, failureKind, managerUp>>

ConfirmPause ==
    /\ managerUp
    /\ state = "PauseRequested"
    /\ state' = "Paused"
    /\ UNCHANGED <<reviewerLease, reportPersisted, reportPublished,
        repairHandoff, pauseResume, triageResume, failureKind, managerUp>>

Resume ==
    /\ managerUp
    /\ state = "Paused"
    /\ state' = pauseResume
    /\ UNCHANGED <<reviewerLease, reportPersisted, reportPublished,
        repairHandoff, pauseResume, triageResume, failureKind, managerUp>>

RequestCancel ==
    /\ state \notin TerminalStates \cup {"CancelRequested"}
    /\ state' = "CancelRequested"
    /\ reviewerLease' = FALSE
    /\ UNCHANGED <<reportPersisted, reportPublished, repairHandoff,
        pauseResume, triageResume, failureKind, managerUp>>

ConfirmCancel ==
    /\ managerUp
    /\ state = "CancelRequested"
    /\ state' = "Cancelled"
    /\ failureKind' = "None"
    /\ UNCHANGED <<reviewerLease, reportPersisted, reportPublished,
        repairHandoff, pauseResume, triageResume, managerUp>>

EnterTriage(kind) ==
    /\ kind \in FailureKinds \ {"None"}
    /\ state \notin TerminalStates \cup {"Paused", "Triage"}
    /\ (kind = "WorkerFailure" => state = "Reviewing")
    /\ state' = "Triage"
    /\ reviewerLease' = FALSE
    /\ triageResume' = SafeResume(state)
    /\ failureKind' = kind
    /\ UNCHANGED <<reportPersisted, reportPublished, repairHandoff,
        pauseResume, managerUp>>

ResolveTriage ==
    /\ managerUp
    /\ state = "Triage"
    /\ failureKind # "None"
    /\ state' = triageResume
    /\ failureKind' = "None"
    /\ UNCHANGED <<reviewerLease, reportPersisted, reportPublished,
        repairHandoff, pauseResume, triageResume, managerUp>>

RefreshTriage(kind) ==
    /\ kind \in FailureKinds \ {"None"}
    /\ state = "Triage"
    /\ failureKind' = kind
    /\ UNCHANGED <<state, reviewerLease, reportPersisted, reportPublished,
        repairHandoff, pauseResume, triageResume, managerUp>>

CrashManager ==
    /\ managerUp
    /\ managerUp' = FALSE
    /\ UNCHANGED <<state, reviewerLease, reportPersisted, reportPublished,
        repairHandoff, pauseResume, triageResume, failureKind>>

RestartManager ==
    /\ ~managerUp
    /\ managerUp' = TRUE
    /\ UNCHANGED <<state, reviewerLease, reportPersisted, reportPublished,
        repairHandoff, pauseResume, triageResume, failureKind>>

Next ==
    \/ QueueReview
    \/ StartReview
    \/ ProduceReport
    \/ PublishReport
    \/ AcknowledgeReport
    \/ HandoffRepair
    \/ RequestPause
    \/ ConfirmPause
    \/ Resume
    \/ RequestCancel
    \/ ConfirmCancel
    \/ EnterTriage("WorkerFailure")
    \/ EnterTriage("EffectFailure")
    \/ EnterTriage("OrphanedWorker")
    \/ RefreshTriage("WorkerFailure")
    \/ RefreshTriage("EffectFailure")
    \/ RefreshTriage("OrphanedWorker")
    \/ ResolveTriage
    \/ CrashManager
    \/ RestartManager

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(RestartManager)
    /\ SF_vars(QueueReview)
    /\ SF_vars(StartReview)
    /\ SF_vars(PublishReport)
    /\ SF_vars(ConfirmPause)
    /\ SF_vars(ConfirmCancel)
    /\ SF_vars(ResolveTriage)

TypeOK ==
    /\ state \in States
    /\ reviewerLease \in BOOLEAN
    /\ reportPersisted \in BOOLEAN
    /\ reportPublished \in BOOLEAN
    /\ repairHandoff \in BOOLEAN
    /\ pauseResume \in ResumeStates
    /\ triageResume \in ResumeStates
    /\ failureKind \in FailureKinds
    /\ managerUp \in BOOLEAN

LeaseOwnership == reviewerLease => state = "Reviewing"

PublishedReportIsDurable == reportPublished => reportPersisted

CompletionRequiresPublishedReport ==
    state = "Completed" => reportPersisted /\ reportPublished

TerminalClosesWorker == state \in TerminalStates => ~reviewerLease

TriageIsExplicit ==
    state = "Triage" => failureKind # "None" /\ ~reviewerLease

PauseEventuallySettles ==
    state = "PauseRequested" ~>
        state \in {"Paused", "CancelRequested", "Cancelled", "Triage"}

CancelEventuallySettles ==
    state = "CancelRequested" ~> state = "Cancelled"

=============================================================================
