---------------------- MODULE EndpointHubLifecycle ----------------------
EXTENDS Naturals, TLC, EndpointHubImplementationReducer

CONSTANT MaxMessages

Endpoints == {"origin", "socket"}
Routes == {"none", "origin", "socket"}
LifecycleTargets == {"none", "origin", "rejected"}

VARIABLES
    hubState,
    physical,
    transport,
    published,
    publishIntent,
    providerLoaded,
    buffer,
    transportPending,
    queued,
    delivered,
    lastRoute,
    lastLifecycleTarget

vars == <<
    hubState, physical, transport, published, publishIntent, providerLoaded,
    buffer, transportPending, queued, delivered, lastRoute, lastLifecycleTarget
>>

SetOriginMembership(set, present) ==
    IF present THEN set \cup {"origin"} ELSE set \ {"origin"}

ApplyOrigin(action) ==
    \E targetState \in HubStates,
       targetPhysical \in BOOLEAN,
       targetTransport \in BOOLEAN,
       targetPublished \in BOOLEAN,
       targetIntent \in BOOLEAN :
        /\ HubStep(
            hubState["origin"], "origin" \in physical,
            "origin" \in transport, "origin" \in published,
            "origin" \in publishIntent, buffer["origin"] = 0, action,
            targetState, targetPhysical, targetTransport,
            targetPublished, targetIntent)
        /\ hubState' = [hubState EXCEPT !["origin"] = targetState]
        /\ physical' = SetOriginMembership(physical, targetPhysical)
        /\ transport' = SetOriginMembership(transport, targetTransport)
        /\ published' = SetOriginMembership(published, targetPublished)
        /\ publishIntent' = SetOriginMembership(publishIntent, targetIntent)

Init ==
    /\ hubState = [endpoint \in Endpoints |->
            IF endpoint = "socket" THEN "attached" ELSE "absent"]
    /\ physical = {"socket"}
    /\ transport = {"socket"}
    /\ published = {"socket"}
    /\ publishIntent = {"socket"}
    /\ providerLoaded = FALSE
    /\ buffer = [endpoint \in Endpoints |-> 0]
    /\ transportPending = [endpoint \in Endpoints |-> 0]
    /\ queued = 0
    /\ delivered = 0
    /\ lastRoute = "none"
    /\ lastLifecycleTarget = "none"

DiscoverOrigin ==
    /\ ApplyOrigin("DISCOVER")
    /\ lastLifecycleTarget' = "origin"
    /\ lastRoute' = "none"
    /\ UNCHANGED <<providerLoaded, buffer, transportPending, queued, delivered>>

LoadOriginProvider ==
    /\ hubState["origin"] \in {"discovered", "detached", "degraded"}
    /\ ~providerLoaded
    /\ "origin" \notin transport
    /\ providerLoaded' = TRUE
    /\ lastLifecycleTarget' = "origin"
    /\ lastRoute' = "none"
    /\ UNCHANGED <<hubState, physical, transport, published, publishIntent,
                    buffer, transportPending, queued, delivered>>

UnloadOriginProvider ==
    /\ hubState["origin"] \in {"discovered", "detached", "degraded"}
    /\ providerLoaded
    /\ "origin" \notin transport
    /\ "origin" \notin published
    /\ providerLoaded' = FALSE
    /\ lastLifecycleTarget' = "origin"
    /\ lastRoute' = "none"
    /\ UNCHANGED <<hubState, physical, transport, published, publishIntent,
                    buffer, transportPending, queued, delivered>>

WithdrawOrigin ==
    /\ ApplyOrigin("WITHDRAW")
    /\ lastLifecycleTarget' = "origin"
    /\ lastRoute' = "none"
    /\ UNCHANGED <<providerLoaded, buffer, transportPending, queued, delivered>>

BeginOriginTransition ==
    /\ ApplyOrigin("BEGIN_TRANSITION")
    /\ lastLifecycleTarget' = "origin"
    /\ lastRoute' = "none"
    /\ UNCHANGED <<providerLoaded, buffer, transportPending, queued, delivered>>

RegisterOriginTransport ==
    /\ providerLoaded
    /\ ApplyOrigin("REGISTER_TRANSPORT")
    /\ lastLifecycleTarget' = "origin"
    /\ lastRoute' = "none"
    /\ UNCHANGED <<providerLoaded, buffer, transportPending, queued, delivered>>

RequestOriginPublish ==
    /\ ApplyOrigin("REQUEST_PUBLISH")
    /\ lastLifecycleTarget' = "origin"
    /\ lastRoute' = "none"
    /\ UNCHANGED <<providerLoaded, buffer, transportPending, queued, delivered>>

BeginOriginDrain ==
    /\ ApplyOrigin("BEGIN_DRAIN")
    /\ lastLifecycleTarget' = "origin"
    /\ lastRoute' = "none"
    /\ UNCHANGED <<providerLoaded, buffer, transportPending, queued, delivered>>

DrainOrigin ==
    /\ hubState["origin"] = "draining"
    /\ buffer["origin"] > 0
    /\ buffer' = [buffer EXCEPT !["origin"] = @ - 1]
    /\ transportPending' = [transportPending EXCEPT !["origin"] = @ + 1]
    /\ UNCHANGED <<hubState, physical, transport, published, publishIntent,
                    providerLoaded, queued, delivered, lastRoute, lastLifecycleTarget>>

AckOriginDelivery ==
    /\ transportPending["origin"] > 0
    /\ transportPending' = [transportPending EXCEPT !["origin"] = @ - 1]
    /\ delivered' = delivered + 1
    /\ UNCHANGED <<hubState, physical, transport, published, publishIntent,
                    providerLoaded, buffer, queued, lastRoute,
                    lastLifecycleTarget>>

RecoverOriginDelivery ==
    /\ transportPending["origin"] > 0
    /\ buffer["origin"] < MaxMessages
    /\ transportPending' = [transportPending EXCEPT !["origin"] = @ - 1]
    /\ buffer' = [buffer EXCEPT !["origin"] = @ + 1]
    /\ UNCHANGED <<hubState, physical, transport, published, publishIntent,
                    providerLoaded, queued, delivered, lastRoute,
                    lastLifecycleTarget>>

CompleteOriginDrain ==
    /\ transportPending["origin"] = 0
    /\ ApplyOrigin("DRAIN_COMPLETE")
    /\ lastLifecycleTarget' = "origin"
    /\ lastRoute' = "none"
    /\ UNCHANGED <<providerLoaded, buffer, transportPending, queued, delivered>>

FailOriginTransition ==
    /\ ApplyOrigin("TRANSITION_FAILED")
    /\ lastLifecycleTarget' = "origin"
    /\ lastRoute' = "none"
    /\ UNCHANGED <<providerLoaded, buffer, transportPending, queued, delivered>>

RemoveOriginTransport ==
    /\ transportPending["origin"] = 0
    /\ ApplyOrigin("TRANSPORT_REMOVED")
    /\ lastLifecycleTarget' = "origin"
    /\ lastRoute' = "none"
    /\ UNCHANGED <<providerLoaded, buffer, transportPending, queued, delivered>>

CompleteOriginDetach ==
    /\ transportPending["origin"] = 0
    /\ ApplyOrigin("DETACH_COMPLETE")
    /\ lastLifecycleTarget' = "origin"
    /\ lastRoute' = "none"
    /\ UNCHANGED <<providerLoaded, buffer, transportPending, queued, delivered>>

RollbackOriginTransport ==
    /\ ApplyOrigin("ROLLBACK_TRANSPORT")
    /\ lastLifecycleTarget' = "origin"
    /\ lastRoute' = "none"
    /\ UNCHANGED <<providerLoaded, buffer, transportPending, queued, delivered>>

BeginRemoveOrigin ==
    /\ ~providerLoaded
    /\ ApplyOrigin("BEGIN_REMOVE")
    /\ lastLifecycleTarget' = "origin"
    /\ lastRoute' = "none"
    /\ UNCHANGED <<providerLoaded, buffer, transportPending, queued, delivered>>

RerouteRemovedOriginBacklog ==
    /\ hubState["origin"] = "removing"
    /\ buffer["origin"] > 0
    /\ buffer["socket"] < MaxMessages
    /\ buffer' = [buffer EXCEPT
            !["origin"] = @ - 1,
            !["socket"] = @ + 1]
    /\ lastLifecycleTarget' = "origin"
    /\ lastRoute' = "none"
    /\ UNCHANGED <<hubState, physical, transport, published, publishIntent,
                    providerLoaded, transportPending, queued, delivered>>

CompleteRemoveOrigin ==
    /\ ApplyOrigin("REMOVE_COMPLETE")
    /\ lastLifecycleTarget' = "origin"
    /\ lastRoute' = "none"
    /\ UNCHANGED <<providerLoaded, buffer, transportPending, queued, delivered>>

RejectMissingOriginLifecycle ==
    /\ hubState["origin"] = "absent"
    /\ lastLifecycleTarget' = "rejected"
    /\ lastRoute' = "none"
    /\ UNCHANGED <<hubState, physical, transport, published, publishIntent,
                    providerLoaded, buffer, transportPending, queued, delivered>>

QueueOriginDirect ==
    /\ hubState["origin"] = "attached"
    /\ "origin" \in transport
    /\ queued < MaxMessages
    /\ queued' = queued + 1
    /\ transportPending' = [transportPending EXCEPT !["origin"] = @ + 1]
    /\ lastRoute' = "origin"
    /\ lastLifecycleTarget' = "none"
    /\ UNCHANGED <<hubState, physical, transport, published, publishIntent,
                    providerLoaded, buffer, delivered>>

QueueOriginBuffered ==
    /\ hubState["origin"] \notin {"absent", "attached", "removing"}
    /\ queued < MaxMessages
    /\ buffer["origin"] < MaxMessages
    /\ queued' = queued + 1
    /\ buffer' = [buffer EXCEPT !["origin"] = @ + 1]
    /\ lastRoute' = "origin"
    /\ lastLifecycleTarget' = "none"
    /\ UNCHANGED <<hubState, physical, transport, published, publishIntent,
                    providerLoaded, transportPending, delivered>>

QueueLateReplyToSocket ==
    /\ hubState["origin"] = "absent"
    /\ queued < MaxMessages
    /\ buffer["socket"] < MaxMessages
    /\ queued' = queued + 1
    /\ buffer' = [buffer EXCEPT !["socket"] = @ + 1]
    /\ lastRoute' = "socket"
    /\ lastLifecycleTarget' = "none"
    /\ UNCHANGED <<hubState, physical, transport, published, publishIntent,
                    providerLoaded, transportPending, delivered>>

DrainSocket ==
    /\ hubState["socket"] = "attached"
    /\ "socket" \in transport
    /\ buffer["socket"] > 0
    /\ buffer' = [buffer EXCEPT !["socket"] = @ - 1]
    /\ transportPending' = [transportPending EXCEPT !["socket"] = @ + 1]
    /\ lastLifecycleTarget' = "none"
    /\ UNCHANGED <<hubState, physical, transport, published, publishIntent,
                    providerLoaded, queued, delivered, lastRoute>>

AckSocketDelivery ==
    /\ transportPending["socket"] > 0
    /\ transportPending' = [transportPending EXCEPT !["socket"] = @ - 1]
    /\ delivered' = delivered + 1
    /\ lastLifecycleTarget' = "none"
    /\ UNCHANGED <<hubState, physical, transport, published, publishIntent,
                    providerLoaded, buffer, queued, lastRoute>>

RecoverSocketDelivery ==
    /\ transportPending["socket"] > 0
    /\ buffer["socket"] < MaxMessages
    /\ transportPending' = [transportPending EXCEPT !["socket"] = @ - 1]
    /\ buffer' = [buffer EXCEPT !["socket"] = @ + 1]
    /\ UNCHANGED <<hubState, physical, transport, published, publishIntent,
                    providerLoaded, queued, delivered, lastRoute,
                    lastLifecycleTarget>>

Next ==
    \/ DiscoverOrigin
    \/ LoadOriginProvider
    \/ UnloadOriginProvider
    \/ WithdrawOrigin
    \/ BeginOriginTransition
    \/ RegisterOriginTransport
    \/ RequestOriginPublish
    \/ BeginOriginDrain
    \/ DrainOrigin
    \/ AckOriginDelivery
    \/ RecoverOriginDelivery
    \/ CompleteOriginDrain
    \/ FailOriginTransition
    \/ RemoveOriginTransport
    \/ CompleteOriginDetach
    \/ RollbackOriginTransport
    \/ BeginRemoveOrigin
    \/ RerouteRemovedOriginBacklog
    \/ CompleteRemoveOrigin
    \/ RejectMissingOriginLifecycle
    \/ QueueOriginDirect
    \/ QueueOriginBuffered
    \/ QueueLateReplyToSocket
    \/ DrainSocket
    \/ AckSocketDelivery
    \/ RecoverSocketDelivery

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ hubState \in [Endpoints -> HubStates]
    /\ physical \subseteq Endpoints
    /\ transport \subseteq Endpoints
    /\ published \subseteq Endpoints
    /\ publishIntent \subseteq Endpoints
    /\ providerLoaded \in BOOLEAN
    /\ buffer \in [Endpoints -> 0..MaxMessages]
    /\ transportPending \in [Endpoints -> 0..MaxMessages]
    /\ queued \in 0..MaxMessages
    /\ delivered \in 0..MaxMessages
    /\ lastRoute \in Routes
    /\ lastLifecycleTarget \in LifecycleTargets

RecoverySocketIsStable ==
    /\ hubState["socket"] = "attached"
    /\ "socket" \in physical
    /\ "socket" \in transport
    /\ "socket" \in published
    /\ "socket" \in publishIntent

PublishedOnlyWhenAttachedTransport ==
    \A endpoint \in published :
        /\ hubState[endpoint] = "attached"
        /\ endpoint \in physical
        /\ endpoint \in transport

AttachedOrDrainingHasTransport ==
    \A endpoint \in Endpoints :
        hubState[endpoint] \in {"attached", "draining"} => endpoint \in transport

OriginTransportHasLoadedProvider ==
    "origin" \in transport => providerLoaded

ProviderCodeFollowsPhysicalHub ==
    providerLoaded => "origin" \in physical

OriginPhysicalMatchesHubLifetime ==
    ("origin" \in physical) <=>
        hubState["origin"] \notin {"absent", "removing"}

TransportOnlyWhilePhysical ==
    \A endpoint \in transport : endpoint \in physical

AbsentHubOwnsNothing ==
    \A endpoint \in Endpoints :
        hubState[endpoint] = "absent" =>
            /\ endpoint \notin physical
            /\ endpoint \notin transport
            /\ endpoint \notin published
            /\ endpoint \notin publishIntent
            /\ buffer[endpoint] = 0
            /\ transportPending[endpoint] = 0

RegistryIsLateAndEarly ==
    hubState["origin"] \in {
        "discovered", "transitioning", "draining", "detached", "degraded",
        "removing", "absent"
    } => "origin" \notin published

MessageConservation ==
    queued = delivered
        + buffer["origin"] + buffer["socket"]
        + transportPending["origin"] + transportPending["socket"]

TransportRemovalWaitsForAck ==
    "origin" \notin transport => transportPending["origin"] = 0

SocketFallbackOnlyAfterPhysicalRemoval ==
    lastRoute = "socket" => hubState["origin"] = "absent"

LifecycleNeverFallsBackToSocket ==
    lastLifecycleTarget # "socket"

======================================================================
