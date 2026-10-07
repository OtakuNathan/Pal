from __future__ import annotations
from pathlib import Path
from contextlib import contextmanager
from collections.abc import Iterator
from pal.bunshin.storage.connection_contracts import DatabasePort
from pal.bunshin.storage.transaction_session import TransactionSession
from pal.bunshin.unit_of_work import BunshinUnitOfWork
from pal.bunshin.engine import TransitionEngine
from pal.bunshin.machines import build_default_transition_engine
from pal.bunshin.storage.database import BunshinDatabase
from pal.bunshin.storage.metrics import MetricsStore
from pal.bunshin.storage.artifact_aliases import ArtifactAliasesStore
from pal.bunshin.storage.artifacts import ArtifactsStore
from pal.bunshin.storage.cycles import CyclesStore
from pal.bunshin.storage.deliveries import DeliveriesStore
from pal.bunshin.storage.delivery_bindings import DeliveryBindingsStore
from pal.bunshin.storage.human_decisions import HumanDecisionsStore
from pal.bunshin.storage.leases import LeasesStore
from pal.bunshin.storage.outbox_claims import OutboxClaimsStore
from pal.bunshin.storage.projections import ProjectionsStore
from pal.bunshin.storage.search import SearchStore
from pal.bunshin.storage.snapshots import SnapshotsStore
from pal.bunshin.storage.queries import QueriesStore
from pal.bunshin.storage.role_events import RoleEventsStore
from pal.bunshin.storage.role_session_checks import RoleSessionChecksStore
from pal.bunshin.storage.role_assignments import RoleAssignmentsStore
from pal.bunshin.storage.role_sessions import RoleSessionsStore
from pal.bunshin.storage.role_attempts import RoleAttemptsStore
from pal.bunshin.storage.role_invocations import RoleInvocationsStore
from pal.bunshin.storage.role_maintenance import RoleMaintenanceStore
from pal.bunshin.storage.role_access import RoleAccessStore
from pal.bunshin.storage.role_retries import RoleRetriesStore
from pal.bunshin.storage.role_submissions import RoleSubmissionsStore
from pal.bunshin.storage.role_cancellation import RoleCancellationStore
from pal.bunshin.storage.transitions import TransitionsStore
from pal.bunshin.storage.outbox_results import OutboxResultsStore


class BunshinRepository:
    """Composition root for stores; business code calls the responsible owner."""

    def __init__(self, runtime_root: Path, engine: TransitionEngine | None = None, *, database: DatabasePort | None = None, metrics: MetricsStore | None = None) -> None:
        transition_engine = engine if engine is not None else build_default_transition_engine()
        self.database: DatabasePort = database if database is not None else BunshinDatabase(runtime_root=Path(runtime_root))
        self.metrics = metrics if metrics is not None else MetricsStore()
        self.artifact_aliases = ArtifactAliasesStore(database=self.database)
        self.artifacts = ArtifactsStore(database=self.database)
        self.cycles = CyclesStore(database=self.database)
        self.deliveries = DeliveriesStore(database=self.database)
        self.delivery_bindings = DeliveryBindingsStore(database=self.database)
        self.human_decisions = HumanDecisionsStore(database=self.database)
        self.leases = LeasesStore(database=self.database)
        self.outbox_claims = OutboxClaimsStore(database=self.database)
        self.projections = ProjectionsStore(database=self.database, metrics=self.metrics, engine=transition_engine)
        self.search = SearchStore(database=self.database)
        self.snapshots = SnapshotsStore(database=self.database)
        self.queries = QueriesStore(database=self.database, projections=self.projections)
        self.role_events = RoleEventsStore(artifacts=self.artifacts, database=self.database, leases=self.leases, projections=self.projections)
        self.role_session_checks = RoleSessionChecksStore(snapshots=self.snapshots)
        self.role_assignments = RoleAssignmentsStore(artifacts=self.artifacts, database=self.database, role_session_checks=self.role_session_checks)
        self.role_sessions = RoleSessionsStore(database=self.database, projections=self.projections, role_session_checks=self.role_session_checks, snapshots=self.snapshots)
        self.role_attempts = RoleAttemptsStore(artifacts=self.artifacts, database=self.database, leases=self.leases, role_sessions=self.role_sessions)
        self.role_invocations = RoleInvocationsStore(artifacts=self.artifacts, database=self.database, leases=self.leases, projections=self.projections, role_sessions=self.role_sessions)
        self.role_maintenance = RoleMaintenanceStore(database=self.database, role_sessions=self.role_sessions)
        self.role_access = RoleAccessStore(database=self.database, leases=self.leases, role_attempts=self.role_attempts)
        self.role_retries = RoleRetriesStore(artifacts=self.artifacts, database=self.database, role_attempts=self.role_attempts, role_sessions=self.role_sessions)
        self.role_submissions = RoleSubmissionsStore(artifacts=self.artifacts, database=self.database, leases=self.leases, role_attempts=self.role_attempts, role_sessions=self.role_sessions)
        self.role_cancellation = RoleCancellationStore(database=self.database, role_sessions=self.role_sessions, role_submissions=self.role_submissions)
        self.transitions = TransitionsStore(artifacts=self.artifacts, database=self.database, human_decisions=self.human_decisions, projections=self.projections, role_submissions=self.role_submissions, snapshots=self.snapshots, engine=transition_engine)
        self.outbox_results = OutboxResultsStore(artifacts=self.artifacts, database=self.database, transitions=self.transitions)

    @property
    def runtime_root(self) -> Path:
        return self.database.runtime_root

    @contextmanager
    def transaction(self) -> Iterator[BunshinUnitOfWork]:
        with self.database.transaction() as connection:
            session = TransactionSession(self.database, connection)
            stores = BunshinRepository(self.runtime_root, self.transitions.engine, database=session, metrics=self.metrics)
            try:
                yield BunshinUnitOfWork(
                    transitions=stores.transitions,
                    cycles=stores.cycles,
                    snapshots=stores.snapshots,
                    outbox_results=stores.outbox_results,
                )
            finally:
                session.finish()
