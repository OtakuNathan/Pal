------------------------ MODULE FdLeaseLifecycle ------------------------
EXTENDS Naturals, FiniteSets, TLC, FdLeaseImplementationTopology

CONSTANTS CapIds, MaxGeneration, BypassCapability, PublishBeforeDetach

Generations == 1..MaxGeneration
GenerationIds == 0..MaxGeneration
LifecycleControlStates == ControlStates \cup {"EMPTY"}
LifecycleCapabilityStates == CapabilityStates \cup {"FREE"}
ActiveCapabilityStates == {"LIVE", "CANCELLED", "TOMBSTONE"}

VARIABLES current,
          nextGeneration,
          controlState,
          rootPublished,
          resourceBound,
          detached,
          closeClaimed,
          closeAttempts,
          capabilityGeneration,
          capabilityState,
          inCall,
          callTarget,
          staleUseObserved

vars == <<current, nextGeneration, controlState, rootPublished,
          resourceBound, detached, closeClaimed, closeAttempts,
          capabilityGeneration, capabilityState, inCall, callTarget,
          staleUseObserved>>

CapsFor(g) == {cap \in CapIds : capabilityGeneration[cap] = g /\
                                    capabilityState[cap] \in ActiveCapabilityStates}

Init ==
    /\ current = 0
    /\ nextGeneration = 0
    /\ controlState = [g \in Generations |-> "EMPTY"]
    /\ rootPublished = {}
    /\ resourceBound = {}
    /\ detached = {}
    /\ closeClaimed = {}
    /\ closeAttempts = [g \in Generations |-> 0]
    /\ capabilityGeneration = [cap \in CapIds |-> 0]
    /\ capabilityState = [cap \in CapIds |-> "FREE"]
    /\ inCall = {}
    /\ callTarget = [cap \in CapIds |-> 0]
    /\ staleUseObserved = FALSE

Publish ==
    /\ current = 0
    /\ nextGeneration < MaxGeneration
    /\ LET g == nextGeneration + 1 IN
       /\ g = 1 \/ ((g - 1) \in detached /\
                     ControlEdge(controlState[g - 1], "PUBLISH", "OPEN"))
       /\ current' = g
       /\ nextGeneration' = g
       /\ controlState' = [controlState EXCEPT ![g] = "OPEN"]
       /\ rootPublished' = rootPublished \cup {g}
       /\ resourceBound' = resourceBound \cup {g}
    /\ UNCHANGED <<detached, closeClaimed, closeAttempts,
                    capabilityGeneration, capabilityState, inCall,
                    callTarget, staleUseObserved>>

Acquire(cap) ==
    /\ current # 0
    /\ controlState[current] = "OPEN"
    /\ capabilityState[cap] = "FREE"
    /\ capabilityGeneration' = [capabilityGeneration EXCEPT ![cap] = current]
    /\ capabilityState' = [capabilityState EXCEPT ![cap] = "LIVE"]
    /\ UNCHANGED <<current, nextGeneration, controlState, rootPublished,
                    resourceBound, detached, closeClaimed, closeAttempts,
                    inCall, callTarget, staleUseObserved>>

BeginCall(cap) ==
    /\ capabilityState[cap] = "LIVE"
    /\ capabilityGeneration[cap] = current
    /\ current # 0
    /\ controlState[current] \in {"OPEN", "RETIRING"}
    /\ cap \notin inCall
    /\ inCall' = inCall \cup {cap}
    /\ callTarget' = [callTarget EXCEPT ![cap] = current]
    /\ UNCHANGED <<current, nextGeneration, controlState, rootPublished,
                    resourceBound, detached, closeClaimed, closeAttempts,
                    capabilityGeneration, capabilityState,
                    staleUseObserved>>

EndCall(cap) ==
    /\ cap \in inCall
    /\ inCall' = inCall \ {cap}
    /\ callTarget' = [callTarget EXCEPT ![cap] = 0]
    /\ UNCHANGED <<current, nextGeneration, controlState, rootPublished,
                    resourceBound, detached, closeClaimed, closeAttempts,
                    capabilityGeneration, capabilityState,
                    staleUseObserved>>

Release(cap) ==
    /\ capabilityState[cap] \in ActiveCapabilityStates
    /\ cap \notin inCall
    /\ CapabilityEdge(capabilityState[cap], "RELEASE", "RELEASED")
    /\ capabilityGeneration' = capabilityGeneration
    /\ capabilityState' = [capabilityState EXCEPT ![cap] = "RELEASED"]
    /\ UNCHANGED <<current, nextGeneration, controlState, rootPublished,
                    resourceBound, detached, closeClaimed, closeAttempts,
                    inCall, callTarget, staleUseObserved>>

ReleaseAndRetire(cap) ==
    /\ current # 0
    /\ controlState[current] = "OPEN"
    /\ capabilityGeneration[cap] = current
    /\ capabilityState[cap] \in ActiveCapabilityStates
    /\ cap \notin inCall
    /\ CapabilityEdge(capabilityState[cap], "RELEASE", "RELEASED")
    /\ ControlEdge("OPEN", "REQUEST_RETIRE", "RETIRING")
    /\ LET g == current IN
       /\ controlState' = [controlState EXCEPT ![g] = "RETIRING"]
       /\ capabilityState' = [other \in CapIds |->
              IF other = cap THEN "RELEASED"
              ELSE IF capabilityGeneration[other] = g /\ capabilityState[other] = "LIVE"
                   THEN IF CapabilityEdge("LIVE", "RETIRE_CANCEL", "CANCELLED")
                        THEN "CANCELLED" ELSE capabilityState[other]
                   ELSE capabilityState[other]]
    /\ UNCHANGED <<current, nextGeneration, rootPublished,
                    resourceBound, detached, closeClaimed, closeAttempts,
                    capabilityGeneration, inCall, callTarget,
                    staleUseObserved>>

RequestRetire ==
    /\ current # 0
    /\ controlState[current] = "OPEN"
    /\ LET g == current IN
       /\ controlState' = [controlState EXCEPT ![g] = "RETIRING"]
       /\ ControlEdge("OPEN", "REQUEST_RETIRE", "RETIRING")
    /\ UNCHANGED <<current, nextGeneration, rootPublished,
                    resourceBound, detached, closeClaimed, closeAttempts,
                    capabilityGeneration, capabilityState, inCall, callTarget,
                    staleUseObserved>>

RequestCancel(cap) ==
    /\ capabilityState[cap] = "LIVE"
    /\ CapabilityEdge("LIVE", "REQUEST_CANCEL", "CANCELLED")
    /\ capabilityState' = [capabilityState EXCEPT ![cap] = "CANCELLED"]
    /\ UNCHANGED <<current, nextGeneration, controlState, rootPublished,
                    resourceBound, detached, closeClaimed, closeAttempts,
                    capabilityGeneration, inCall, callTarget,
                    staleUseObserved>>

ClaimGracefulClose ==
    /\ current # 0
    /\ LET g == current IN
       /\ controlState[g] = "RETIRING"
       /\ ControlEdge("RETIRING", "CLAIM_GRACEFUL_CLOSE", "CLOSING")
       /\ CapsFor(g) = {}
       /\ g \notin closeClaimed
       /\ controlState' = [controlState EXCEPT ![g] = "CLOSING"]
       /\ closeClaimed' = closeClaimed \cup {g}
       /\ closeAttempts' = [closeAttempts EXCEPT ![g] = @ + 1]
    /\ UNCHANGED <<current, nextGeneration, rootPublished,
                    resourceBound, detached, capabilityGeneration,
                    capabilityState, inCall, callTarget,
                    staleUseObserved>>

ForceRevoke ==
    /\ current # 0
    /\ LET g == current IN
       /\ controlState[g] = "RETIRING"
       /\ ControlEdge("RETIRING", "FORCE_REVOKE", "REVOKING")
       /\ g \notin closeClaimed
       /\ controlState' = [controlState EXCEPT ![g] = "REVOKING"]
       /\ capabilityState' = [cap \in CapIds |->
              IF capabilityGeneration[cap] = g /\ capabilityState[cap] # "FREE"
                 /\ capabilityState[cap] # "RELEASED"
              THEN IF CapabilityEdge(capabilityState[cap], "FORCE_REVOKE", "TOMBSTONE")
                   THEN "TOMBSTONE" ELSE capabilityState[cap]
              ELSE capabilityState[cap]]
       /\ closeClaimed' = closeClaimed \cup {g}
       /\ closeAttempts' = [closeAttempts EXCEPT ![g] = @ + 1]
    /\ UNCHANGED <<current, nextGeneration, rootPublished,
                    resourceBound, detached, capabilityGeneration,
                    inCall, callTarget, staleUseObserved>>

CloseDetached ==
    /\ current # 0
    /\ LET g == current IN
       /\ controlState[g] \in {"CLOSING", "REVOKING"}
       /\ g \in closeClaimed
       /\ ~\E cap \in inCall : capabilityGeneration[cap] = g
       /\ ControlEdge(controlState[g], "CLOSE_DETACHED", "CLOSED")
       /\ controlState' = [controlState EXCEPT ![g] = "CLOSED"]
       /\ current' = 0
       /\ rootPublished' = rootPublished \ {g}
       /\ resourceBound' = resourceBound \ {g}
       /\ detached' = detached \cup {g}
    /\ UNCHANGED <<nextGeneration, closeClaimed, closeAttempts,
                    capabilityGeneration, capabilityState, inCall,
                    callTarget, staleUseObserved>>

CloseUncertainAfterQuiescence ==
    /\ current # 0
    /\ LET g == current IN
       /\ controlState[g] \in {"CLOSING", "REVOKING"}
       /\ g \in closeClaimed
       /\ ~\E cap \in inCall : capabilityGeneration[cap] = g
       /\ ControlEdge(controlState[g], "CLOSE_UNCERTAIN", "QUARANTINED")
       /\ controlState' = [controlState EXCEPT ![g] = "QUARANTINED"]
    /\ UNCHANGED <<current, nextGeneration, rootPublished,
                    resourceBound, detached, closeClaimed, closeAttempts,
                    capabilityGeneration, capabilityState, inCall,
                    callTarget, staleUseObserved>>

QuarantineUnquiesced ==
    /\ current # 0
    /\ LET g == current IN
       /\ controlState[g] = "REVOKING"
       /\ g \in closeClaimed
       /\ \E cap \in inCall : capabilityGeneration[cap] = g
       /\ ControlEdge("REVOKING", "CLOSE_UNCERTAIN", "QUARANTINED")
       /\ controlState' = [controlState EXCEPT ![g] = "QUARANTINED"]
    /\ UNCHANGED <<current, nextGeneration, rootPublished,
                    resourceBound, detached, closeClaimed, closeAttempts,
                    capabilityGeneration, capabilityState, inCall,
                    callTarget, staleUseObserved>>

UnsafeCachedFdUse(cap) ==
    /\ BypassCapability
    /\ capabilityState[cap] = "TOMBSTONE"
    /\ capabilityGeneration[cap] # 0
    /\ current # 0
    /\ capabilityGeneration[cap] # current
    /\ current \in resourceBound
    /\ staleUseObserved' = TRUE
    /\ UNCHANGED <<current, nextGeneration, controlState, rootPublished,
                    resourceBound, detached, closeClaimed, closeAttempts,
                    capabilityGeneration, capabilityState, inCall,
                    callTarget>>

UnsafePublish ==
    /\ PublishBeforeDetach
    /\ current # 0
    /\ nextGeneration < MaxGeneration
    /\ LET old == current IN
       LET g == nextGeneration + 1 IN
       /\ old \notin detached
       /\ current' = g
       /\ nextGeneration' = g
       /\ controlState' = [controlState EXCEPT ![g] = "OPEN"]
       /\ rootPublished' = rootPublished \cup {g}
       /\ resourceBound' = resourceBound \cup {g}
    /\ UNCHANGED <<detached, closeClaimed, closeAttempts,
                    capabilityGeneration, capabilityState, inCall,
                    callTarget, staleUseObserved>>

Terminal ==
    /\ nextGeneration = MaxGeneration
    /\ current = 0
    /\ inCall = {}
    /\ UNCHANGED vars

Next ==
    \/ Publish
    \/ \E cap \in CapIds : Acquire(cap)
    \/ \E cap \in CapIds : BeginCall(cap)
    \/ \E cap \in CapIds : EndCall(cap)
    \/ \E cap \in CapIds : Release(cap)
    \/ \E cap \in CapIds : ReleaseAndRetire(cap)
    \/ \E cap \in CapIds : RequestCancel(cap)
    \/ RequestRetire
    \/ ClaimGracefulClose
    \/ ForceRevoke
    \/ CloseDetached
    \/ CloseUncertainAfterQuiescence
    \/ QuarantineUnquiesced
    \/ \E cap \in CapIds : UnsafeCachedFdUse(cap)
    \/ UnsafePublish
    \/ Terminal

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ current \in GenerationIds
    /\ nextGeneration \in GenerationIds
    /\ controlState \in [Generations -> LifecycleControlStates]
    /\ rootPublished \subseteq Generations
    /\ resourceBound \subseteq Generations
    /\ detached \subseteq Generations
    /\ closeClaimed \subseteq Generations
    /\ closeAttempts \in [Generations -> 0..1]
    /\ capabilityGeneration \in [CapIds -> GenerationIds]
    /\ capabilityState \in [CapIds -> LifecycleCapabilityStates]
    /\ inCall \subseteq CapIds
    /\ callTarget \in [CapIds -> GenerationIds]
    /\ staleUseObserved \in BOOLEAN

CurrentIsPublished == current = 0 \/ current \in rootPublished
ClosedIsDetached == \A g \in Generations :
    controlState[g] = "CLOSED" => g \in detached /\ g \notin resourceBound
QuarantineBlocksPublish == \A g \in Generations :
    controlState[g] = "QUARANTINED" =>
        \A newer \in Generations : newer > g => controlState[newer] = "EMPTY"
QuarantineRetainsBinding == \A g \in Generations :
    controlState[g] = "QUARANTINED" =>
        g \in rootPublished /\ g \in resourceBound /\ g \notin detached
CloseAtMostOnce == \A g \in Generations : closeAttempts[g] <= 1
CallsKeepTheirGeneration == \A cap \in inCall :
    /\ callTarget[cap] = capabilityGeneration[cap]
    /\ callTarget[cap] # 0
DetachedOnlyAfterCallsEnd == \A g \in detached :
    ~\E cap \in inCall : capabilityGeneration[cap] = g
PublishedOnlyAfterDetach == \A g \in Generations :
    g > 1 /\ controlState[g] # "EMPTY" => (g - 1) \in detached
NoStaleUse == ~staleUseObserved

=============================================================================
