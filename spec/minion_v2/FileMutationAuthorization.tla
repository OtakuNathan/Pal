-------------------- MODULE FileMutationAuthorization --------------------
EXTENDS Integers, TLC

\* One logical session, one file, and two content versions are enough to
\* expose the historical-delivery rollback that caused consecutive edits to
\* fail after the first successful mutation.

Versions == 0..2
SnapshotSources == {"none", "delivery", "mutation"}

VARIABLES
    diskVersion,
    snapshotVersion,
    snapshotComplete,
    snapshotSource,
    deliveredVersion,
    deliveredComplete,
    fullDeliveredVersions,
    readPending,
    externalChanged

vars == <<
    diskVersion, snapshotVersion, snapshotComplete, snapshotSource,
    deliveredVersion, deliveredComplete, fullDeliveredVersions,
    readPending, externalChanged
>>

Init ==
    /\ diskVersion = 0
    /\ snapshotVersion = -1
    /\ snapshotComplete = FALSE
    /\ snapshotSource = "none"
    /\ deliveredVersion = -1
    /\ deliveredComplete = FALSE
    /\ fullDeliveredVersions = {}
    /\ readPending = FALSE
    /\ externalChanged = FALSE

Read(full) ==
    \* A new read observes the current bytes.  If an older snapshot names
    \* different bytes, it is retired before the delivery is projected.
    /\ full \in BOOLEAN
    /\ deliveredVersion' = diskVersion
    /\ deliveredComplete' = full
    /\ readPending' = TRUE
    /\ IF snapshotVersion # diskVersion
          THEN /\ snapshotVersion' = -1
               /\ snapshotComplete' = FALSE
               /\ snapshotSource' = "none"
          ELSE UNCHANGED <<snapshotVersion, snapshotComplete, snapshotSource>>
    /\ externalChanged' = FALSE
    /\ UNCHANGED <<diskVersion, fullDeliveredVersions>>

ReconcileHistoricalDelivery ==
    /\ deliveredVersion >= 0
    /\ IF snapshotSource = "mutation" /\ snapshotVersion # deliveredVersion
          THEN UNCHANGED <<snapshotVersion, snapshotComplete, snapshotSource>>
          ELSE IF snapshotVersion = deliveredVersion /\ snapshotComplete /\
                  ~deliveredComplete
          THEN UNCHANGED <<snapshotVersion, snapshotComplete, snapshotSource>>
          ELSE /\ snapshotVersion' = deliveredVersion
               /\ snapshotComplete' = deliveredComplete
               /\ snapshotSource' = "delivery"
    /\ readPending' = FALSE
    /\ fullDeliveredVersions' =
        IF deliveredComplete
        THEN fullDeliveredVersions \cup {deliveredVersion}
        ELSE fullDeliveredVersions
    /\ UNCHANGED <<diskVersion, deliveredVersion, deliveredComplete,
                    externalChanged>>

SelfMutate ==
    /\ snapshotComplete
    /\ snapshotVersion = diskVersion
    /\ diskVersion < 2
    /\ diskVersion' = diskVersion + 1
    /\ snapshotVersion' = diskVersion + 1
    /\ snapshotComplete' = TRUE
    /\ snapshotSource' = "mutation"
    /\ externalChanged' = FALSE
    /\ UNCHANGED <<deliveredVersion, deliveredComplete,
                    fullDeliveredVersions, readPending>>

ExternalMutate ==
    /\ diskVersion < 2
    /\ diskVersion' = diskVersion + 1
    /\ externalChanged' = TRUE
    /\ UNCHANGED <<snapshotVersion, snapshotComplete, snapshotSource,
                    deliveredVersion, deliveredComplete,
                    fullDeliveredVersions, readPending>>

Next ==
    \/ Read(TRUE)
    \/ Read(FALSE)
    \/ ReconcileHistoricalDelivery
    \/ SelfMutate
    \/ ExternalMutate
    \/ UNCHANGED vars

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ diskVersion \in Versions
    /\ snapshotVersion \in {-1} \cup Versions
    /\ snapshotComplete \in BOOLEAN
    /\ snapshotSource \in SnapshotSources
    /\ deliveredVersion \in {-1} \cup Versions
    /\ deliveredComplete \in BOOLEAN
    /\ fullDeliveredVersions \subseteq Versions
    /\ readPending \in BOOLEAN
    /\ externalChanged \in BOOLEAN

\* A successful mutation advances the authorization snapshot atomically.
\* Replaying an older read result must never roll that snapshot backward.
MutationSnapshotNeverRollsBack ==
    snapshotSource = "mutation" /\ ~externalChanged =>
        snapshotVersion = diskVersion

\* An out-of-band write cannot remain authorized without a fresh read.
ExternalChangeIsNotAuthorized ==
    externalChanged => ~(snapshotComplete /\ snapshotVersion = diskVersion)

\* Partial delivery alone never grants mutation.
PartialDeliveryIsNotComplete ==
    snapshotSource = "delivery" /\ snapshotComplete =>
        snapshotVersion \in fullDeliveredVersions

=============================================================================
