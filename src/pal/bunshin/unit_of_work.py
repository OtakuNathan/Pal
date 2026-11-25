"""The store interfaces available for one atomic business transition."""
from __future__ import annotations

from dataclasses import dataclass

from pal.bunshin.storage.cycles import CyclesStore
from pal.bunshin.storage.outbox_results import OutboxResultsStore
from pal.bunshin.storage.interaction_waits import InteractionWaits
from pal.bunshin.storage.deliveries import DeliveriesStore
from pal.bunshin.storage.delivery_bindings import DeliveryBindingsStore
from pal.bunshin.storage.role_access import RoleAccessStore
from pal.bunshin.storage.role_attempts import RoleAttemptsStore
from pal.bunshin.storage.role_cancellation import RoleCancellationStore
from pal.bunshin.storage.queries import QueriesStore
from pal.bunshin.storage.search import SearchStore
from pal.bunshin.storage.snapshots import SnapshotsStore
from pal.bunshin.storage.transitions import TransitionsStore


@dataclass(frozen=True)
class BunshinUnitOfWork:
    transitions: TransitionsStore
    cycles: CyclesStore
    snapshots: SnapshotsStore
    outbox_results: OutboxResultsStore
    role_access: RoleAccessStore
    role_attempts: RoleAttemptsStore
    role_cancellation: RoleCancellationStore
    interaction_waits: InteractionWaits
    deliveries: DeliveriesStore
    delivery_bindings: DeliveryBindingsStore
    queries: QueriesStore
    search: SearchStore
