---------------- MODULE DependencyRepairLaterCohort ----------------
(*
Actual-scope semantics permits an independent later upstream Q report after an
older P cohort sealed. P may already be producing or checking. The new cohort
must invalidate its old binding and join its exact worker before STALE/admission.

  Model                         Runtime obligation
  RegisterLaterQ                new intent fences effective consumers, including active P
  ReapExactP/CloseExactOldP      exact task/owner/lease closure, not logical-state inference
  ApplyLaterQ                   new cohort receipt; preserve prior P finding references
  candidate/resolutionCandidate reset/revalidate case resolutions on candidate replacement
  ReleaseBarrier/StartChecker   accepted new Q product bound into P's checker input
  aReceipt/bReceipt             per-packet/provider replay dedupe, not global one-repair limit

Q's completed repair is abstracted as one step; its physical lifecycle is covered
by DependencyRepairCohort and setup ownership. This is not a compositional proof.
*)
EXTENDS Integers
\* P is already repairing from a sealed earlier cohort. A later report reopens
\* upstream Q. Its effective closure includes P and the synthetic sink. P's old
\* execution binding may finish physically but cannot publish acceptance.
VARIABLES p, live, lease, fence, closed, registered, applied, q, qVersion,
          binding, required, resolved, oldCheck, aReceipt, bReceipt, candidate, resolutionCandidate
vars == <<p,live,lease,fence,closed,registered,applied,q,qVersion,
          binding,required,resolved,oldCheck,aReceipt,bReceipt,candidate,resolutionCandidate>>
Init == /\ p = "RepairReady" /\ live = FALSE /\ lease = FALSE /\ fence = FALSE /\ closed = FALSE
        /\ registered = FALSE /\ applied = FALSE /\ q = "Accepted" /\ qVersion = 0
        /\ binding = 0 /\ required = {"old-P-case"} /\ resolved = {}
        /\ oldCheck = FALSE /\ aReceipt = 1 /\ bReceipt = 0
        /\ candidate = 0 /\ resolutionCandidate = -1
StartProducer == /\ p = "RepairReady" /\ ~fence /\ p' = "Producing" /\ live' = TRUE /\ lease' = TRUE
    /\ UNCHANGED <<fence,closed,registered,applied,q,qVersion,binding,required,resolved,oldCheck,aReceipt,bReceipt,candidate,resolutionCandidate>>
FinishProducer == /\ p = "Producing" /\ p' = "CheckerReady" /\ live' = FALSE /\ lease' = FALSE
    /\ UNCHANGED <<fence,closed,registered,applied,q,qVersion,binding,required,resolved,oldCheck,aReceipt,bReceipt,candidate,resolutionCandidate>>
StartChecker == /\ p = "CheckerReady" /\ ~fence /\ q = "Accepted"
    /\ p' = "Checking" /\ live' = TRUE /\ lease' = TRUE /\ binding' = qVersion
    /\ UNCHANGED <<fence,closed,registered,applied,q,qVersion,required,resolved,oldCheck,aReceipt,bReceipt,candidate,resolutionCandidate>>
ResolveCurrentCase == /\ p = "Checking" /\ ~fence /\ resolved' = required
    /\ resolutionCandidate' = candidate
    /\ UNCHANGED <<p,live,lease,fence,closed,registered,applied,q,qVersion,binding,required,oldCheck,aReceipt,bReceipt,candidate>>
Pass == /\ p = "Checking" /\ ~fence /\ q = "Accepted"
    /\ binding = qVersion /\ required \subseteq resolved
    /\ p' = "Accepted" /\ live' = FALSE /\ lease' = FALSE
    /\ UNCHANGED <<fence,closed,registered,applied,q,qVersion,binding,required,resolved,oldCheck,aReceipt,bReceipt,candidate,resolutionCandidate>>
RegisterLaterQ == /\ ~registered /\ registered' = TRUE /\ fence' = TRUE
    /\ oldCheck' = (p = "Checking") /\ p' = "CancelRequested"
    /\ UNCHANGED <<live,lease,closed,applied,q,qVersion,binding,required,resolved,aReceipt,bReceipt,candidate,resolutionCandidate>>
ReapExactP == /\ fence /\ live /\ live' = FALSE
    /\ UNCHANGED <<p,lease,fence,closed,registered,applied,q,qVersion,binding,required,resolved,oldCheck,aReceipt,bReceipt,candidate,resolutionCandidate>>
ReleaseExactP == /\ fence /\ ~live /\ lease /\ lease' = FALSE
    /\ UNCHANGED <<p,live,fence,closed,registered,applied,q,qVersion,binding,required,resolved,oldCheck,aReceipt,bReceipt,candidate,resolutionCandidate>>
CloseExactOldP == /\ fence /\ ~live /\ ~lease /\ ~closed /\ closed' = TRUE
    /\ UNCHANGED <<p,live,lease,fence,registered,applied,q,qVersion,binding,required,resolved,oldCheck,aReceipt,bReceipt,candidate,resolutionCandidate>>
ApplyLaterQ == /\ registered /\ ~applied /\ closed /\ ~live /\ ~lease
    /\ applied' = TRUE /\ q' = "RepairReady" /\ p' = "Stale"
    /\ resolved' = {} /\ bReceipt' = 1
    /\ candidate' = 1 /\ resolutionCandidate' = -1
    /\ UNCHANGED <<live,lease,fence,closed,registered,qVersion,binding,required,oldCheck,aReceipt>>
RepairQ == /\ q = "RepairReady" /\ q' = "Accepted" /\ qVersion' = 1
    /\ UNCHANGED <<p,live,lease,fence,closed,registered,applied,binding,required,resolved,oldCheck,aReceipt,bReceipt,candidate,resolutionCandidate>>
ReleaseBarrier == /\ applied /\ q = "Accepted" /\ p = "Stale"
    /\ fence' = FALSE /\ p' = "RepairReady"
    /\ UNCHANGED <<live,lease,closed,registered,applied,q,qVersion,binding,required,resolved,oldCheck,aReceipt,bReceipt,candidate,resolutionCandidate>>
\* Original packet replay and old-bound PASS receipt capture are evidence-only.
ReplayOldPacketOrPass == UNCHANGED vars
Next == StartProducer \/ FinishProducer \/ StartChecker \/ ResolveCurrentCase \/ Pass
     \/ RegisterLaterQ \/ ReapExactP \/ ReleaseExactP \/ CloseExactOldP
     \/ ApplyLaterQ \/ RepairQ \/ ReleaseBarrier \/ ReplayOldPacketOrPass
Spec == Init /\ [][Next]_vars
TypeOK ==
    /\ p \in {"RepairReady", "Producing", "CheckerReady", "Checking", "Accepted", "CancelRequested", "Stale"}
    /\ q \in {"Accepted", "RepairReady"}
    /\ live \in BOOLEAN /\ lease \in BOOLEAN /\ fence \in BOOLEAN /\ closed \in BOOLEAN
    /\ registered \in BOOLEAN /\ applied \in BOOLEAN /\ oldCheck \in BOOLEAN
    /\ qVersion \in 0..1 /\ binding \in 0..1 /\ candidate \in 0..1
    /\ resolutionCandidate \in {-1, 0, 1}
    /\ required \subseteq {"old-P-case"} /\ resolved \subseteq required
    /\ aReceipt = 1 /\ bReceipt \in 0..1
NoPrematureStale == p = "Stale" => closed /\ ~live /\ ~lease
NoOldBindingAcceptance == p = "Accepted" => ~fence /\ q = "Accepted" /\ binding = qVersion
PriorObligationCarried == required = {"old-P-case"}
PassUsesCurrentCandidateResolution == p = "Accepted" =>
    required \subseteq resolved /\ resolutionCandidate = candidate
OriginalPacketIsNotReapplied == aReceipt = 1 /\ bReceipt <= 1
=================================================================
