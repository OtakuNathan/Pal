"""The store interfaces available for one atomic business transition."""
from __future__ import annotations

from dataclasses import dataclass

from pal.bunshin.v2.storage.cycles import CyclesStore
from pal.bunshin.v2.storage.outbox_results import OutboxResultsStore
from pal.bunshin.v2.storage.snapshots import SnapshotsStore
from pal.bunshin.v2.storage.transitions import TransitionsStore


@dataclass(frozen=True)
class BunshinUnitOfWork:
    transitions: TransitionsStore
    cycles: CyclesStore
    snapshots: SnapshotsStore
    outbox_results: OutboxResultsStore
