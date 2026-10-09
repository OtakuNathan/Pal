--------------------------- MODULE EntryWithdrawal ---------------------------
EXTENDS Naturals, TLC
CONSTANT UnsafeAdmit
VARIABLES owner, visible, retiring, cleanup, retained, phase, captured, admitted, lease, stale
vars == <<owner, visible, retiring, cleanup, retained, phase, captured, admitted, lease, stale>>
Init == /\ owner = 0 /\ visible = TRUE /\ retiring = FALSE
        /\ cleanup = "unstarted" /\ retained = TRUE /\ stale = FALSE
        /\ phase = "new" /\ captured = 0 /\ admitted = FALSE /\ lease = FALSE
Capture == /\ phase = "new" /\ visible
           /\ captured' = owner /\ phase' = "queued"
           /\ UNCHANGED <<owner, visible, retiring, cleanup, retained, admitted, lease, stale>>
Withdraw == /\ visible /\ owner = 0
            /\ visible' = FALSE /\ retiring' = TRUE
            /\ UNCHANGED <<owner, cleanup, retained, phase, captured, admitted, lease, stale>>
Admit == /\ phase = "queued" /\ (UnsafeAdmit \/ (visible /\ captured = owner))
         /\ phase' = "admitted" /\ admitted' = TRUE /\ lease' = TRUE
         /\ stale' = (captured # owner \/ ~visible)
         /\ UNCHANGED <<owner, visible, retiring, cleanup, retained, captured>>
Reject == /\ phase = "queued" /\ (~visible \/ captured # owner)
          /\ phase' = "rejected"
          /\ UNCHANGED <<owner, visible, retiring, cleanup, retained, captured, admitted, lease, stale>>
Finish == /\ phase = "admitted"
          /\ phase' = "finished" /\ lease' = FALSE
          /\ UNCHANGED <<owner, visible, retiring, cleanup, retained, captured, admitted, stale>>
CommitUnload == /\ retiring /\ ~lease /\ cleanup = "unstarted"
                /\ cleanup' = "started"
                /\ UNCHANGED <<owner, visible, retiring, retained, phase, captured, admitted, lease, stale>>
CleanupSuccess == /\ cleanup = "started" /\ cleanup' = "done" /\ retained' = FALSE
                  /\ UNCHANGED <<owner, visible, retiring, phase, captured, admitted, lease, stale>>
CleanupFailure == /\ cleanup = "started" /\ cleanup' = "failed"
                  /\ UNCHANGED <<owner, visible, retiring, retained, phase, captured, admitted, lease, stale>>
RetryCleanup == /\ cleanup = "failed" /\ cleanup' = "started"
                /\ UNCHANGED <<owner, visible, retiring, retained, phase, captured, admitted, lease, stale>>
Restore == /\ retiring /\ owner = 0 /\ cleanup = "unstarted"
           /\ owner' = 1 /\ visible' = TRUE /\ retiring' = FALSE
           /\ UNCHANGED <<cleanup, retained, phase, captured, admitted, lease, stale>>
Next == Capture \/ Withdraw \/ Admit \/ Reject \/ Finish \/ CommitUnload
        \/ CleanupSuccess \/ CleanupFailure \/ RetryCleanup \/ Restore
TypeOK == /\ owner \in 0..1 /\ captured \in 0..1
          /\ phase \in {"new", "queued", "admitted", "rejected", "finished"}
          /\ cleanup \in {"unstarted", "started", "failed", "done"}
          /\ visible \in BOOLEAN /\ retiring \in BOOLEAN /\ retained \in BOOLEAN
          /\ admitted \in BOOLEAN /\ lease \in BOOLEAN /\ stale \in BOOLEAN
DrainBeforeCleanup == cleanup # "unstarted" => ~lease
OwnedAdmission == lease => admitted /\ phase = "admitted"
NoRetarget == ~stale
EntryFirst == retiring => ~visible
FailedCleanupOwned == cleanup = "failed" => retained /\ ~visible
CommittedStaysWithdrawn == cleanup # "unstarted" => ~visible
Spec == /\ Init /\ [][Next]_vars
        /\ WF_vars(Admit) /\ WF_vars(Reject) /\ WF_vars(Finish) /\ WF_vars(CommitUnload)
        /\ WF_vars(CleanupSuccess \/ CleanupFailure)
QueuedCompletes == (phase = "queued") ~> (phase \in {"finished", "rejected"})
RetirementCompletes == retiring ~> (~retiring \/ cleanup \in {"done", "failed"})
=============================================================================
