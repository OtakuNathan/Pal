from __future__ import annotations
from pal.bunshin.v2.semantic_orchestration.composition import SemanticComponents, build_semantic_components
from pal.bunshin.v2.semantic_orchestration.runtime_settings import RoleRuntimeSettings
from pal.bunshin.v2.semantic_orchestration.workflow_requests import WorkflowRequests
from pal.bunshin.v2.semantic_orchestration.callbacks import HumanReviewPublisher
from pal.bunshin.v2.semantic_orchestration.callbacks import WorkerEventPublisher
from pal.bunshin.v2.semantic_orchestration.callbacks import WorkflowEventPublisher
from pal.bunshin.v2.semantic_orchestration.callbacks import BrokerRunRegistrar
from pal.bunshin.v2.semantic_orchestration.callbacks import BrokerRunUnregistrar
from pal.bunshin.v2.semantic_orchestration.callbacks import SkillInjector
from dataclasses import InitVar, dataclass, field
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.harnesses import BunshinHarnessRegistry
from pal.bunshin.v2.workspace_resources import WorkspaceLockRegistry
from pal.bunshin.v2.background_assignments import BackgroundAssignments
from pal.bunshin.v2.worker_processes import WorkerProcesses
from pal.bunshin.v2.service import BunshinV2WorkflowService
from pal.bunshin.v2.role_runtime import RoleSupervisor
from pal.bunshin.v2.semantic_orchestration.routes import SEMANTIC_EFFECT_TYPES


@dataclass
class SemanticOrchestrator:
    components: SemanticComponents = field(init=False, repr=False)
    service: BunshinV2WorkflowService
    max_parallel_workers: int = 5
    runtime_db_path: Path | None = None
    harness_registry: BunshinHarnessRegistry = field(
        default_factory=lambda: BunshinHarnessRegistry(include_pal=True)
    )
    publish_human_review: HumanReviewPublisher | None = None
    publish_worker_event: WorkerEventPublisher | None = None
    publish_workflow_event: WorkflowEventPublisher | None = None
    register_broker_run: BrokerRunRegistrar | None = None
    unregister_broker_run: BrokerRunUnregistrar | None = None
    inject_skill: SkillInjector | None = None
    prompt_log_enabled: InitVar[bool] = False
    settings: RoleRuntimeSettings = field(init=False, repr=False)
    background: BackgroundAssignments = field(default_factory=BackgroundAssignments, init=False)
    processes: WorkerProcesses = field(default_factory=WorkerProcesses, init=False)
    workspace_locks: WorkspaceLockRegistry = field(default_factory=WorkspaceLockRegistry, init=False)
    requests: WorkflowRequests = field(init=False, repr=False)
    supervisor: RoleSupervisor = field(init=False, repr=False)

    def __post_init__(self, prompt_log_enabled: bool) -> None:
        self.settings = RoleRuntimeSettings(prompt_log_enabled)
        self.supervisor = RoleSupervisor(max_active_runs=max(1, int(self.max_parallel_workers)))
        self.requests = WorkflowRequests(self.service.artifacts)
        self.components = build_semantic_components(
            background=self.background,
            harness_registry=self.harness_registry,
            inject_skill=self.inject_skill,
            processes=self.processes,
            publish_human_review=self.publish_human_review,
            publish_worker_event=self.publish_worker_event,
            publish_workflow_event=self.publish_workflow_event,
            register_broker_run=self.register_broker_run,
            requests=self.requests,
            runtime_db_path=self.runtime_db_path,
            service=self.service,
            settings=self.settings,
            supervisor=self.supervisor,
            unregister_broker_run=self.unregister_broker_run,
            workspace_locks=self.workspace_locks,
        )

    @property
    def repository(self):
        return self.service.repository

    @property
    def active_background_count(self) -> int:
        """Return live logical tasks for graceful-drain accounting only.

        This is deliberately not execution capacity.  A durable role may be
        materialized, suspended, or waiting for the process semaphore while
        this task remains alive.  ``RoleSupervisor.active_run_count`` is the
        sole capacity projection.
        """

        return self.background.active_count

    @property
    def active_process_count(self) -> int:
        return self.supervisor.active_run_count

    def request_stop(self) -> None:
        self.background.request_stop()

    async def stop_background_workers(self, *, timeout_seconds: float = 10.0) -> None:
        for effect_key in await self.background.drain(timeout_seconds=timeout_seconds):
            self.components.assignment_retries.queue_interrupted_assignment_retry(effect_key)
        await self.processes.close_all()

    async def send_worker_control(self, run_id: str, message: Mapping[str, Any]) -> bool:
        return await self.processes.send_control(str(run_id), message)

    async def execute_semantic_effect(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        return await self.components.effect_dispatch.execute_semantic_effect(effect)

    async def recover_background_assignments(self) -> int:
        return await self.components.assignment_recovery.recover_background_assignments(self.components.effect_dispatch.handlers)

    def set_prompt_log_enabled(self, enabled: bool) -> None:
        self.settings.set_prompt_logging(enabled)
