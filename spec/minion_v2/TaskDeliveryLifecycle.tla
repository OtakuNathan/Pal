-------------------- MODULE TaskDeliveryLifecycle --------------------
EXTENDS Naturals, TLC

CONSTANT MaxBindingVersion

Channels == {"origin", "other"}
WorkflowStates == {"Running", "Waiting", "Completed"}
DeliveryStates == {"None", "Pending", "InFlight", "Delivered"}
DeliveryRoutes == {"none", "current", "socket"}
DeliveryParts == {"attachment0", "attachment1", "primary"}

VARIABLES
    workflowState,
    workflowVersion,
    originChannel,
    currentChannel,
    bindingVersion,
    currentChannelAlive,
    socketConnected,
    deliveryState,
    deliveryRoute,
    deliveredParts

vars == <<
    workflowState, workflowVersion, originChannel, currentChannel,
    bindingVersion, currentChannelAlive, socketConnected,
    deliveryState, deliveryRoute, deliveredParts
>>

Init ==
    /\ workflowState = "Running"
    /\ workflowVersion = 1
    /\ originChannel = "origin"
    /\ currentChannel = originChannel
    /\ bindingVersion = 1
    /\ currentChannelAlive = TRUE
    /\ socketConnected = FALSE
    /\ deliveryState = "None"
    /\ deliveryRoute = "none"
    /\ deliveredParts = {}

AdvanceWorkflow ==
    /\ workflowState = "Running"
    /\ workflowState' = "Waiting"
    /\ workflowVersion' = workflowVersion + 1
    /\ UNCHANGED <<originChannel, currentChannel, bindingVersion,
                    currentChannelAlive, socketConnected,
                    deliveryState, deliveryRoute, deliveredParts>>

CompleteWorkflow ==
    /\ workflowState = "Waiting"
    /\ workflowState' = "Completed"
    /\ workflowVersion' = workflowVersion + 1
    /\ UNCHANGED <<originChannel, currentChannel, bindingVersion,
                    currentChannelAlive, socketConnected,
                    deliveryState, deliveryRoute, deliveredParts>>

RebindTaskDelivery(channel) ==
    /\ channel \in Channels
    /\ channel # currentChannel
    /\ bindingVersion < MaxBindingVersion
    /\ currentChannel' = channel
    /\ bindingVersion' = bindingVersion + 1
    /\ UNCHANGED <<workflowState, workflowVersion, originChannel,
                    currentChannelAlive, socketConnected,
                    deliveryState, deliveryRoute, deliveredParts>>

SetCurrentChannelHealth(alive) ==
    /\ alive \in BOOLEAN
    /\ currentChannelAlive' = alive
    /\ UNCHANGED <<workflowState, workflowVersion, originChannel,
                    currentChannel, bindingVersion, socketConnected,
                    deliveryState, deliveryRoute, deliveredParts>>

SetSocketConnection(connected) ==
    /\ connected \in BOOLEAN
    /\ socketConnected' = connected
    /\ UNCHANGED <<workflowState, workflowVersion, originChannel,
                    currentChannel, bindingVersion, currentChannelAlive,
                    deliveryState, deliveryRoute, deliveredParts>>

QueueDelivery ==
    /\ deliveryState = "None"
    /\ deliveryState' = "Pending"
    /\ UNCHANGED <<workflowState, workflowVersion, originChannel,
                    currentChannel, bindingVersion, currentChannelAlive,
                    socketConnected, deliveryRoute, deliveredParts>>

ClaimCurrentChannel ==
    /\ deliveryState = "Pending"
    /\ currentChannelAlive
    /\ deliveryState' = "InFlight"
    /\ deliveryRoute' = "current"
    /\ UNCHANGED <<workflowState, workflowVersion, originChannel,
                    currentChannel, bindingVersion, currentChannelAlive,
                    socketConnected, deliveredParts>>

ClaimRecoverySocket ==
    /\ deliveryState = "Pending"
    /\ ~currentChannelAlive
    /\ socketConnected
    /\ deliveryState' = "InFlight"
    /\ deliveryRoute' = "socket"
    /\ UNCHANGED <<workflowState, workflowVersion, originChannel,
                    currentChannel, bindingVersion, currentChannelAlive,
                    socketConnected, deliveredParts>>

AcceptPart(part) ==
    /\ deliveryState = "InFlight"
    /\ part \in DeliveryParts \ deliveredParts
    /\ deliveredParts' = deliveredParts \cup {part}
    /\ UNCHANGED <<workflowState, workflowVersion, originChannel,
                    currentChannel, bindingVersion, currentChannelAlive,
                    socketConnected, deliveryState, deliveryRoute>>

ProviderAccept ==
    /\ deliveryState = "InFlight"
    /\ deliveredParts = DeliveryParts
    /\ deliveryState' = "Delivered"
    /\ UNCHANGED <<workflowState, workflowVersion, originChannel, currentChannel,
                    bindingVersion, currentChannelAlive, socketConnected,
                    deliveryRoute, deliveredParts>>

DeliveryFailure ==
    /\ deliveryState = "InFlight"
    /\ deliveryState' = "Pending"
    /\ deliveryRoute' = "none"
    /\ UNCHANGED <<workflowState, workflowVersion, originChannel, currentChannel,
                    bindingVersion, currentChannelAlive, socketConnected,
                    deliveredParts>>

Next ==
    \/ AdvanceWorkflow
    \/ CompleteWorkflow
    \/ \E channel \in Channels : RebindTaskDelivery(channel)
    \/ \E alive \in BOOLEAN : SetCurrentChannelHealth(alive)
    \/ \E connected \in BOOLEAN : SetSocketConnection(connected)
    \/ QueueDelivery
    \/ ClaimCurrentChannel
    \/ ClaimRecoverySocket
    \/ \E part \in DeliveryParts : AcceptPart(part)
    \/ ProviderAccept
    \/ DeliveryFailure

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ workflowState \in WorkflowStates
    /\ workflowVersion \in Nat
    /\ originChannel \in Channels
    /\ currentChannel \in Channels
    /\ bindingVersion \in 1..MaxBindingVersion
    /\ currentChannelAlive \in BOOLEAN
    /\ socketConnected \in BOOLEAN
    /\ deliveryState \in DeliveryStates
    /\ deliveryRoute \in DeliveryRoutes
    /\ deliveredParts \subseteq DeliveryParts

OriginIsImmutable == originChannel = "origin"

WorkflowProjectionIsConsistent ==
    /\ workflowState = "Running" => workflowVersion = 1
    /\ workflowState = "Waiting" => workflowVersion = 2
    /\ workflowState = "Completed" => workflowVersion = 3

NoFalseDelivery ==
    deliveryState = "Delivered" => deliveryRoute # "none"

PendingIsDurableWithoutTransport ==
    deliveryState = "Pending" /\ ~currentChannelAlive /\ ~socketConnected
        => deliveryRoute = "none"

SocketDeliveryWasExplicit ==
    deliveryRoute = "socket" => deliveryState \in {"InFlight", "Delivered"}

DeliveredMeansEveryPartWasAccepted ==
    deliveryState = "Delivered" => deliveredParts = DeliveryParts

======================================================================
