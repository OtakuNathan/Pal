---------------------- MODULE BunshinRuntimeAuthority ----------------------
EXTENDS Naturals

CONSTANTS Role, Manager, NoOwner, MaxRequests

VARIABLES state,
          l1Owner,
          l2Owner,
          l3Mode,
          llmAuthority,
          hostRuntimeOwner,
          requestCount,
          responseCount

vars == <<state, l1Owner, l2Owner, l3Mode, llmAuthority,
          hostRuntimeOwner, requestCount, responseCount>>

Init ==
    /\ state = "created"
    /\ l1Owner = NoOwner
    /\ l2Owner = NoOwner
    /\ l3Mode = "read_only"
    /\ llmAuthority = Manager
    /\ hostRuntimeOwner = Manager
    /\ requestCount = 0
    /\ responseCount = 0

StartRole ==
    /\ state = "created"
    /\ state' = "running"
    /\ l1Owner' = Role
    /\ l2Owner' = Role
    /\ UNCHANGED <<l3Mode, llmAuthority, hostRuntimeOwner,
                    requestCount, responseCount>>

ProxyRequest ==
    /\ state = "running"
    /\ requestCount < MaxRequests
    /\ state' = "request_pending"
    /\ requestCount' = requestCount + 1
    /\ UNCHANGED <<l1Owner, l2Owner, l3Mode, llmAuthority,
                    hostRuntimeOwner, responseCount>>

ManagerResponse ==
    /\ state = "request_pending"
    /\ responseCount < requestCount
    /\ state' = "running"
    /\ responseCount' = responseCount + 1
    /\ UNCHANGED <<l1Owner, l2Owner, l3Mode, llmAuthority,
                    hostRuntimeOwner, requestCount>>

CloseRole ==
    /\ state = "running"
    /\ requestCount = responseCount
    /\ state' = "closed"
    /\ l1Owner' = NoOwner
    /\ l2Owner' = NoOwner
    /\ UNCHANGED <<l3Mode, llmAuthority, hostRuntimeOwner,
                    requestCount, responseCount>>

Closed ==
    /\ state = "closed"
    /\ UNCHANGED vars

Next == StartRole \/ ProxyRequest \/ ManagerResponse \/ CloseRole \/ Closed

TypeOK ==
    /\ state \in {"created", "running", "request_pending", "closed"}
    /\ l1Owner \in {Role, NoOwner}
    /\ l2Owner \in {Role, NoOwner}
    /\ l3Mode \in {"read_only", "read_write"}
    /\ llmAuthority \in {Role, Manager}
    /\ hostRuntimeOwner \in {Role, Manager}
    /\ requestCount \in 0..MaxRequests
    /\ responseCount \in 0..MaxRequests

RoleOwnsWorkingMemory ==
    state \in {"running", "request_pending"} =>
        l1Owner = Role /\ l2Owner = Role

L3IsAlwaysReadOnly == l3Mode = "read_only"

LLMIsAlwaysManagerOwned ==
    llmAuthority = Manager /\ hostRuntimeOwner = Manager

ResponsesConsumeRequests == responseCount <= requestCount

OnlyOneRequestInFlight == requestCount - responseCount <= 1

ClosedReleasesLocalMemory ==
    state = "closed" => l1Owner = NoOwner /\ l2Owner = NoOwner

=============================================================================
