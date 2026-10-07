---------------- MODULE DependencyRepairSetupOwnership ----------------
(*
Admitted setup can precede the role row and the registered process. The owned
BackgroundAssignments effect-key task must be cancelled AND awaited to termination.
ReserveSpawn denotes ownership established before awaiting create_subprocess_exec.
Its process may materialize after Freeze; cleanup cannot ACK while it is starting.

  Model                         Runtime obligation
  AcquireLease/CreateAssignment causal incarnation/fence recheck before mutation
  ReserveSpawn/FinishSpawn       tracked spawn ownership through async registration
  CloseSetupAndAssignment       exact task termination plus assignment submission cut
  ReapExactOwner/ReleaseExactLease no lookup by a reusable invocation ID/current payload
  MarkStale                     STALE_CONFIRMED only after complete RepairClosure
  ReplayOldCleanup              old effect cannot affect a replacement owner or lease

This bounded abstraction does not operate a process, filesystem, or live runtime.
*)
EXTENDS Naturals
VARIABLES stage, frozen, starting, oldProcess, lease, closed, stale, replacement, newProcess
vars == <<stage,frozen,starting,oldProcess,lease,closed,stale,replacement,newProcess>>
Init == /\ stage = "Admitted" /\ ~frozen /\ ~starting /\ ~oldProcess
        /\ ~lease /\ ~closed /\ ~stale /\ ~replacement /\ ~newProcess
AcquireLease == /\ stage = "Admitted" /\ ~frozen /\ stage' = "Leased" /\ lease'
    /\ UNCHANGED <<frozen,starting,oldProcess,closed,stale,replacement,newProcess>>
CreateAssignment == /\ stage = "Leased" /\ ~frozen /\ stage' = "Assigned"
    /\ UNCHANGED <<frozen,starting,oldProcess,lease,closed,stale,replacement,newProcess>>
ReserveSpawn == /\ stage = "Assigned" /\ ~frozen /\ stage' = "Starting" /\ starting'
    /\ UNCHANGED <<frozen,oldProcess,lease,closed,stale,replacement,newProcess>>
\* A pre-freeze owned asynchronous spawn may finish after freeze. Its starting
\* token remains owned until abort or registration, so cleanup cannot ACK early.
FinishSpawn == /\ starting /\ stage' = "Running" /\ ~starting' /\ oldProcess'
    /\ UNCHANGED <<frozen,lease,closed,stale,replacement,newProcess>>
AbortSpawn == /\ frozen /\ starting /\ stage' = "Cancelled" /\ ~starting'
    /\ UNCHANGED <<frozen,oldProcess,lease,closed,stale,replacement,newProcess>>
Freeze == /\ ~frozen /\ frozen'
    /\ UNCHANGED <<stage,starting,oldProcess,lease,closed,stale,replacement,newProcess>>
ReapExactOwner == /\ frozen /\ oldProcess /\ ~oldProcess'
    /\ UNCHANGED <<stage,frozen,starting,lease,closed,stale,replacement,newProcess>>
CloseSetupAndAssignment == /\ frozen /\ ~starting /\ ~oldProcess /\ ~closed /\ closed'
    /\ UNCHANGED <<stage,frozen,starting,oldProcess,lease,stale,replacement,newProcess>>
ReleaseExactLease == /\ closed /\ lease /\ ~lease'
    /\ UNCHANGED <<stage,frozen,starting,oldProcess,closed,stale,replacement,newProcess>>
MarkStale == /\ closed /\ frozen /\ ~starting /\ ~oldProcess /\ ~lease /\ ~stale /\ stale'
    /\ UNCHANGED <<stage,frozen,starting,oldProcess,lease,closed,replacement,newProcess>>
StartReplacement == /\ stale /\ ~replacement /\ replacement' /\ newProcess'
    /\ UNCHANGED <<stage,frozen,starting,oldProcess,lease,closed,stale>>
ReplayOldCleanup == UNCHANGED vars
Next == AcquireLease \/ CreateAssignment \/ ReserveSpawn \/ FinishSpawn \/ AbortSpawn
     \/ Freeze \/ ReapExactOwner \/ CloseSetupAndAssignment \/ ReleaseExactLease
     \/ MarkStale \/ StartReplacement \/ ReplayOldCleanup
Spec == Init /\ [][Next]_vars
TypeOK ==
    /\ stage \in {"Admitted", "Leased", "Assigned", "Starting", "Running", "Cancelled"}
    /\ frozen \in BOOLEAN /\ starting \in BOOLEAN /\ oldProcess \in BOOLEAN
    /\ lease \in BOOLEAN /\ closed \in BOOLEAN /\ stale \in BOOLEAN
    /\ replacement \in BOOLEAN /\ newProcess \in BOOLEAN
NoAcknowledgementWithStartingOwner == closed => ~starting /\ ~oldProcess
StaleMeansOldOwnerGone == stale => closed /\ ~starting /\ ~oldProcess /\ ~lease
OldCleanupCannotTouchReplacement == replacement => newProcess
=================================================================
