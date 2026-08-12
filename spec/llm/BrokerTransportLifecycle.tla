-------------------- MODULE BrokerTransportLifecycle --------------------
EXTENDS Naturals

VARIABLES state,
          authorityGranted,
          specFresh,
          upstreamCount,
          providerStarted,
          leaseHeld,
          refreshCount,
          receiptCount

vars == <<state, authorityGranted, specFresh, upstreamCount,
          providerStarted, leaseHeld, refreshCount, receiptCount>>

TerminalStates == {"rejected", "cancelled", "receipt_recorded"}

Init ==
    /\ state = "idle"
    /\ authorityGranted \in BOOLEAN
    /\ specFresh \in BOOLEAN
    /\ upstreamCount = 0
    /\ providerStarted = FALSE
    /\ leaseHeld = FALSE
    /\ refreshCount = 0
    /\ receiptCount = 0

RejectUnauthorized ==
    /\ state = "idle"
    /\ ~authorityGranted
    /\ state' = "rejected"
    /\ UNCHANGED <<authorityGranted, specFresh, upstreamCount,
                    providerStarted, leaseHeld, refreshCount, receiptCount>>

RejectStale ==
    /\ state = "idle"
    /\ authorityGranted
    /\ ~specFresh
    /\ state' = "stale"
    /\ UNCHANGED <<authorityGranted, specFresh, upstreamCount,
                    providerStarted, leaseHeld, refreshCount, receiptCount>>

RefreshAtSafePoint ==
    /\ state = "stale"
    /\ refreshCount = 0
    /\ state' = "idle"
    /\ specFresh' = TRUE
    /\ refreshCount' = 1
    /\ UNCHANGED <<authorityGranted, upstreamCount, providerStarted,
                    leaseHeld, receiptCount>>

Authorize ==
    /\ state = "idle"
    /\ authorityGranted
    /\ specFresh
    /\ state' = "authorized"
    /\ UNCHANGED <<authorityGranted, specFresh, upstreamCount,
                    providerStarted, leaseHeld, refreshCount, receiptCount>>

BeginUpstream ==
    /\ state = "authorized"
    /\ state' = "invoking"
    /\ upstreamCount' = upstreamCount + 1
    /\ leaseHeld' = TRUE
    /\ UNCHANGED <<authorityGranted, specFresh, providerStarted,
                    refreshCount, receiptCount>>

ProviderAccepted ==
    /\ state = "invoking"
    /\ state' = "provider_started"
    /\ providerStarted' = TRUE
    /\ UNCHANGED <<authorityGranted, specFresh, upstreamCount,
                    leaseHeld, refreshCount, receiptCount>>

TransportCompleted ==
    /\ state \in {"invoking", "provider_started"}
    /\ state' = "transport_terminal"
    /\ leaseHeld' = FALSE
    /\ UNCHANGED <<authorityGranted, specFresh, upstreamCount,
                    providerStarted, refreshCount, receiptCount>>

ConsumerCancelled ==
    /\ state \in {"invoking", "provider_started"}
    /\ state' = "cancelled"
    /\ leaseHeld' = FALSE
    /\ UNCHANGED <<authorityGranted, specFresh, upstreamCount,
                    providerStarted, refreshCount, receiptCount>>

RecordUsageReceipt ==
    /\ state = "transport_terminal"
    /\ receiptCount = 0
    /\ state' = "receipt_recorded"
    /\ receiptCount' = 1
    /\ UNCHANGED <<authorityGranted, specFresh, upstreamCount,
                    providerStarted, leaseHeld, refreshCount>>

ReplayUsageReceipt ==
    /\ state = "receipt_recorded"
    /\ UNCHANGED vars

Closed ==
    /\ state \in TerminalStates
    /\ UNCHANGED vars

Next == RejectUnauthorized
     \/ RejectStale
     \/ RefreshAtSafePoint
     \/ Authorize
     \/ BeginUpstream
     \/ ProviderAccepted
     \/ TransportCompleted
     \/ ConsumerCancelled
     \/ RecordUsageReceipt
     \/ ReplayUsageReceipt
     \/ Closed

TypeOK ==
    /\ state \in {"idle", "stale", "authorized", "invoking",
                    "provider_started", "transport_terminal", "rejected",
                    "cancelled", "receipt_recorded"}
    /\ authorityGranted \in BOOLEAN
    /\ specFresh \in BOOLEAN
    /\ upstreamCount \in 0..1
    /\ providerStarted \in BOOLEAN
    /\ leaseHeld \in BOOLEAN
    /\ refreshCount \in 0..1
    /\ receiptCount \in 0..1

RejectedNeverInvokes == state = "rejected" => upstreamCount = 0
StaleNeverInvokes == state = "stale" => upstreamCount = 0
AtMostOneUpstream == upstreamCount <= 1
ProviderStartRequiresUpstream == providerStarted => upstreamCount = 1
LeaseHasSingleOwner == leaseHeld => state \in {"invoking", "provider_started"}
CancellationReleasesLease == state = "cancelled" => ~leaseHeld
ReceiptIsTerminalAndIdempotent ==
    receiptCount = 1 => state = "receipt_recorded" /\ ~leaseHeld
ProviderStartUsesFreshSpec == providerStarted => specFresh

=============================================================================
