"""The store interfaces available for one atomic business transition."""
from __future__ import annotations

from dataclasses import dataclass

from pal.bunshin.storage.cycles import CyclesStore
from pal.bunshin.storage.outbox_results import OutboxResultsStore
from pal.bunshin.storage.snapshots import SnapshotsStore
from pal.bunshin.storage.transitions import TransitionsStore


@dataclass(frozen=True)
class BunshinUnitOfWork:
    transitions: TransitionsStore
    cycles: CyclesStore
    snapshots: SnapshotsStore
    outbox_results: OutboxResultsStore
