from __future__ import annotations
from pal.bunshin.failure_diagnostics import append_failure_diagnostic
from dataclasses import dataclass
from pal.bunshin.storage.role_assignments import semantic_business_lease
from pathlib import Path
from typing import Any
from pal.bunshin.repository import BunshinV2Repository
from pal.bunshin.role_runtime import RoleSupervisor
import json
from pal.bunshin.semantic_orchestration.callbacks import WorkerEventPublisher
from pal.bunshin.semantic_orchestration.callbacks import BrokerRunRegistrar
from pal.bunshin.semantic_orchestration.callbacks import BrokerRunUnregistrar
import contextlib
from pal.bunshin.contracts import LeaseConflict, StaleFencingToken
from pal.bunshin.workspace_resources import WorkspaceLockRegistry
from pal.bunshin.worker_processes import WorkerProcesses
from pal.bunshin.process_lifecycle import WorkerProcessOwner
from pal.memory.storage import MemoryStorage
from pal.bunshin.semantic_orchestration.role_leases import RoleLeases
from pal.bunshin.semantic_orchestration.attempt_models import ClaimedRoleAttempt, ExitedRoleProcess, PreparedRoleWorkspace, PublishedRoleAttempt, RoleAttemptRequest


@dataclass
class WorkerExecution:
    processes: WorkerProcesses
    publish_worker_event: WorkerEventPublisher | None
    register_broker_run: BrokerRunRegistrar | None
    repository: BunshinV2Repository
    role_leases: RoleLeases
    supervisor: RoleSupervisor
    unregister_broker_run: BrokerRunUnregistrar | None
    workspace_locks: WorkspaceLockRegistry

    async def execute(
        self, command: RoleAttemptRequest, stage_attempt_admission: ClaimedRoleAttempt,
        stage_attempt_publication: PublishedRoleAttempt, stage_workspace_preparation: PreparedRoleWorkspace,
    ) -> ExitedRoleProcess:
        argv = stage_attempt_publication.argv
        assignment_lease = stage_attempt_admission.assignment_lease
        assignment_lease_resource = stage_attempt_admission.assignment_lease_resource
        attempt = stage_attempt_admission.attempt
        env = stage_attempt_publication.env
        fencing_token = command.fencing_token
        invocation_id = command.invocation_id
        lease_resource = command.lease_resource
        pack = stage_attempt_publication.pack
        role = stage_workspace_preparation.role
        run_id = stage_workspace_preparation.run_id
        snapshot = command.snapshot
        business_lease = semantic_business_lease(
            snapshot, owner_id=invocation_id, resource_key=lease_resource, fencing_token=fencing_token,
        )
        if business_lease:
            self.repository.role_assignments.assert_semantic_admission(
                workflow_id=snapshot.workflow_id, aggregate_type=snapshot.aggregate_type.value,
                aggregate_id=snapshot.aggregate_id, business_lease=business_lease,
            )
        def process_started(owner: WorkerProcessOwner) -> None:
            process_metadata = {
                "workflow_id": snapshot.workflow_id,
                "aggregate_type": snapshot.aggregate_type.value,
                "aggregate_id": snapshot.aggregate_id,
                "workspace_path": str(pack.workspace.get("repo_path") or ""),
                "run_id": run_id,
            }
            self.repository.leases.update_lease_metadata(
                assignment_lease_resource,
                str(attempt["attempt_id"]),
                assignment_lease.fencing_token,
                {**process_metadata, "role": role},
            )
            self.repository.leases.update_lease_metadata(
                lease_resource,
                invocation_id,
                fencing_token,
                process_metadata,
            )

        def register_process(owner: WorkerProcessOwner) -> None:
            self.processes.register(owner)
            if self.register_broker_run is not None:
                self.register_broker_run(run_id, invocation_id, pack, owner)

        def unregister_process(owner: WorkerProcessOwner) -> None:
            self.processes.unregister(owner, before_remove=self.unregister_broker_run)
            # The assignment fence belongs to this concrete native-process
            # attempt. Once the owned child is retired there can be no
            # legitimate late role-gateway call, so close the lease at the
            # same lifecycle boundary as the process permit.
            with contextlib.suppress(LeaseConflict, StaleFencingToken):
                self.repository.leases.release_lease(
                    assignment_lease_resource,
                    str(attempt["attempt_id"]),
                    assignment_lease.fencing_token,
                )

        workspace_path = str(pack.workspace.get("repo_path") or "").strip()
        memory_storage = MemoryStorage(self.repository.runtime_root)
        owner = WorkerProcessOwner(
            argv=tuple(argv),
            env=env,
            invocation_id=invocation_id,
            run_id=run_id,
            effect_key=str(command.effect.get("effect_key") or command.effect.get("effect_id") or ""),
            assignment_id=str(attempt["assignment_id"]),
            attempt_id=str(attempt["attempt_id"]),
            business_lease_resource_key=lease_resource,
            business_fencing_token=fencing_token,
            workspace=Path(workspace_path) if workspace_path else None,
            workspace_locks=self.workspace_locks,
            on_started=process_started,
            on_reserved=self.processes.register,
            on_registered=register_process,
            on_unregistered=unregister_process,
            heartbeat_factories=(
                lambda: self.role_leases.lease_heartbeat(
                    lease_resource,
                    invocation_id,
                    fencing_token,
                ),
                lambda: self.role_leases.lease_heartbeat(
                    assignment_lease_resource,
                    str(attempt["attempt_id"]),
                    assignment_lease.fencing_token,
                ),
            ),
            reap_timeout_seconds=5.0,
            memory_read_lease_factory=(
                lambda: memory_storage.worker_read_lease(snapshot.workflow_id)
            ) if memory_storage.catalog_path.exists() else None,
        )
        events: list[dict[str, Any]] = []
        worker_error = ""
        shell = self.supervisor.process_shell(
            owner,
            run_id=run_id,
        )
        async with shell:
            async for raw_line in owner.stdout_lines():
                try:
                    item = json.loads(raw_line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if str(item.get("kind") or "") == "event" and isinstance(item.get("event"), dict):
                    event = dict(item["event"])
                    # A logical role session reuses its invocation id across
                    # attempts.  Keep the concrete attempt identity available
                    # to Manager delivery deduplication without changing the
                    # worker's public payload contract.
                    event["_attempt_id"] = str(attempt["attempt_id"])
                    if event.get("event_kind") == "producer_tool_diagnostic":
                        # Bind new operational records to this owned process,
                        # never a different run named by worker output.
                        event["_owner_run_id"] = run_id
                    events.append(event)
                    if self.publish_worker_event is not None:
                        await self.publish_worker_event(event)
                elif str(item.get("kind") or "") == "worker_error":
                    worker_error = append_failure_diagnostic(
                        str(item.get("error") or ""), item.get("failure_diagnostic"),
                    )
            await owner.wait()
        return ExitedRoleProcess(events=events, owner=owner, worker_error=worker_error)
