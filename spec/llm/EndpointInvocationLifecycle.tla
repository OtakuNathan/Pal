-------------------- MODULE EndpointInvocationLifecycle --------------------
EXTENDS Naturals, FiniteSets

CONSTANTS Endpoints, HasCredential, NeedsCompact, MaxAttempts, NoEndpoint

VARIABLES state,
          endpoint,
          candidates,
          hookApplied,
          preflighted,
          attempts,
          healthy,
          clients,
          retired

vars == <<state, endpoint, candidates, hookApplied, preflighted, attempts,
          healthy, clients, retired>>

TerminalStates == {"succeeded", "failed", "compact_required"}

Init ==
    /\ state = "idle"
    /\ endpoint = NoEndpoint
    /\ candidates = Endpoints
    /\ hookApplied = FALSE
    /\ preflighted = FALSE
    /\ attempts = 0
    /\ healthy = {}
    /\ clients = {}
    /\ retired = {}

SelectEndpoint ==
    /\ state \in {"idle", "endpoint_failed"}
    /\ candidates # {}
    /\ \E selected \in candidates:
        /\ endpoint' = selected
        /\ candidates' = candidates \ {selected}
        /\ state' = "hook_pending"
        /\ hookApplied' = FALSE
        /\ preflighted' = FALSE
        /\ attempts' = 0
        /\ UNCHANGED <<healthy, clients, retired>>

ApplyModelHook ==
    /\ state = "hook_pending"
    /\ hookApplied' = TRUE
    /\ state' = "preflight_pending"
    /\ UNCHANGED <<endpoint, candidates, preflighted, attempts,
                    healthy, clients, retired>>

PreflightCompact ==
    /\ state = "preflight_pending"
    /\ hookApplied
    /\ endpoint \in NeedsCompact
    /\ preflighted' = TRUE
    /\ state' = "compact_required"
    /\ UNCHANGED <<endpoint, candidates, hookApplied, attempts,
                    healthy, clients, retired>>

PreflightReady ==
    /\ state = "preflight_pending"
    /\ hookApplied
    /\ endpoint \notin NeedsCompact
    /\ preflighted' = TRUE
    /\ state' = "prepared"
    /\ UNCHANGED <<endpoint, candidates, hookApplied, attempts,
                    healthy, clients, retired>>

MissingCredential ==
    /\ state = "prepared"
    /\ endpoint \notin HasCredential
    /\ state' = "endpoint_failed"
    /\ UNCHANGED <<endpoint, candidates, hookApplied, preflighted,
                    attempts, healthy, clients, retired>>

BeginInvocation ==
    /\ state = "prepared"
    /\ endpoint \in HasCredential
    /\ state' = "invoking"
    /\ attempts' = 1
    /\ clients' = clients \cup {endpoint}
    /\ UNCHANGED <<endpoint, candidates, hookApplied, preflighted,
                    healthy, retired>>

RetryableError ==
    /\ state = "invoking"
    /\ attempts < MaxAttempts
    /\ attempts' = attempts + 1
    /\ UNCHANGED <<state, endpoint, candidates, hookApplied, preflighted,
                    healthy, clients, retired>>

ExhaustedError ==
    /\ state = "invoking"
    /\ attempts = MaxAttempts
    /\ state' = "endpoint_failed"
    /\ clients' = clients \ {endpoint}
    /\ retired' = retired \cup {endpoint}
    /\ UNCHANGED <<endpoint, candidates, hookApplied, preflighted,
                    attempts, healthy>>

CredentialRejected ==
    /\ state = "invoking"
    /\ state' = "endpoint_failed"
    /\ clients' = clients \ {endpoint}
    /\ retired' = retired \cup {endpoint}
    /\ UNCHANGED <<endpoint, candidates, hookApplied, preflighted,
                    attempts, healthy>>

ProviderSuccess ==
    /\ state = "invoking"
    /\ state' = "succeeded"
    /\ healthy' = {endpoint}
    /\ UNCHANGED <<endpoint, candidates, hookApplied, preflighted,
                    attempts, clients, retired>>

NoFallback ==
    /\ state = "endpoint_failed"
    /\ candidates = {}
    /\ state' = "failed"
    /\ UNCHANGED <<endpoint, candidates, hookApplied, preflighted,
                    attempts, healthy, clients, retired>>

Closed ==
    /\ state \in TerminalStates
    /\ UNCHANGED vars

Next == SelectEndpoint
     \/ ApplyModelHook
     \/ PreflightCompact
     \/ PreflightReady
     \/ MissingCredential
     \/ BeginInvocation
     \/ RetryableError
     \/ ExhaustedError
     \/ CredentialRejected
     \/ ProviderSuccess
     \/ NoFallback
     \/ Closed

TypeOK ==
    /\ state \in {
        "idle", "hook_pending", "preflight_pending", "prepared",
        "invoking", "endpoint_failed", "succeeded", "failed",
        "compact_required"
       }
    /\ endpoint \in Endpoints \cup {NoEndpoint}
    /\ candidates \subseteq Endpoints
    /\ hookApplied \in BOOLEAN
    /\ preflighted \in BOOLEAN
    /\ attempts \in 0..MaxAttempts
    /\ healthy \subseteq Endpoints
    /\ clients \subseteq Endpoints
    /\ retired \subseteq Endpoints

PreflightUsesHookedRequest == preflighted => hookApplied
InvocationRequiresPreflight == state = "invoking" => preflighted /\ hookApplied
CompactDoesNotInvoke == state = "compact_required" => attempts = 0 /\ clients = {}
NoNegativeSuccess == state # "succeeded" => healthy = {}
SuccessIsExact == state = "succeeded" => healthy = {endpoint}
CredentialFailureSkipsInvocation ==
    state = "endpoint_failed" /\ endpoint \notin HasCredential => attempts = 0
RetiredClientsAreClosed == (retired \cap clients) = {}

=============================================================================
