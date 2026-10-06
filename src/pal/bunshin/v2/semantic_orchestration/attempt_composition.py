from __future__ import annotations
from pal.bunshin.v2.semantic_orchestration.runtime_settings import RoleRuntimeSettings
from pal.bunshin.v2.semantic_orchestration.workflow_requests import WorkflowRequests
from pathlib import Path
from typing import Any, Callable
from pal.bunshin.harnesses import BunshinHarnessRegistry
from pal.bunshin.v2.artifacts import ContentAddressedArtifactStore
from pal.bunshin.v2.contracts import AggregateSnapshot
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.semantic_orchestration.attempt_inputs import AttemptInputs
from pal.bunshin.v2.semantic_orchestration.workflow_facts import WorkflowFacts
from pal.bunshin.v2.task_ledger import TaskLedgerService
from pal.bunshin.v2.background_assignments import BackgroundAssignments
from pal.bunshin.v2.semantic_orchestration.assignment_identity import AssignmentIdentity
from pal.bunshin.v2.semantic_orchestration.assignment_retries import AssignmentRetries
from pal.bunshin.v2.semantic_orchestration.role_checkpoints import RoleCheckpoints
from pal.bunshin.v2.role_runtime import RoleSupervisor
from pal.bunshin.v2.semantic_orchestration.callbacks import WorkerEventPublisher
from pal.bunshin.v2.semantic_orchestration.callbacks import BrokerRunRegistrar
from pal.bunshin.v2.semantic_orchestration.callbacks import BrokerRunUnregistrar
from pal.bunshin.v2.workspace_resources import WorkspaceLockRegistry
from pal.bunshin.v2.worker_processes import WorkerProcesses
from pal.bunshin.v2.semantic_orchestration.role_leases import RoleLeases
from pal.bunshin.v2.semantic_orchestration.attempt_models import RoleAttemptRequest, AttemptReplay
from pal.bunshin.v2.semantic_orchestration.attempt_workspace_preparation import WorkspacePreparation
from pal.bunshin.v2.semantic_orchestration.attempt_verifier_context import VerifierContext
from pal.bunshin.v2.semantic_orchestration.attempt_reference_binding import ReferenceBinding
from pal.bunshin.v2.semantic_orchestration.attempt_prompt_construction import PromptConstruction
from pal.bunshin.v2.semantic_orchestration.attempt_playbook_binding import PlaybookBinding
from pal.bunshin.v2.semantic_orchestration.attempt_tool_policy import ToolPolicy
from pal.bunshin.v2.semantic_orchestration.attempt_assignment_reuse import AssignmentReuse
from pal.bunshin.v2.semantic_orchestration.attempt_role_session import RoleSession
from pal.bunshin.v2.semantic_orchestration.attempt_harness_binding import HarnessBinding
from pal.bunshin.v2.semantic_orchestration.attempt_assignment_replay import AssignmentReplay
from pal.bunshin.v2.semantic_orchestration.attempt_admission import AttemptAdmission
from pal.bunshin.v2.semantic_orchestration.attempt_pack import AttemptPack
from pal.bunshin.v2.semantic_orchestration.attempt_publication import AttemptPublication
from pal.bunshin.v2.semantic_orchestration.attempt_worker_execution import WorkerExecution
from pal.bunshin.v2.semantic_orchestration.attempt_process_result import ProcessResult
from pal.bunshin.v2.semantic_orchestration.attempt_terminal_validation import TerminalValidation
from pal.bunshin.v2.semantic_orchestration.attempt_completion import AttemptCompletion
from pal.bunshin.v2.semantic_orchestration.attempt_execution import AttemptExecution


def build_attempt_execution(
    assignment_identity: AssignmentIdentity,
    assignment_retries: AssignmentRetries,
    attempt_inputs: AttemptInputs,
    role_checkpoints: RoleCheckpoints,
    role_leases: RoleLeases,
    workflow_facts: WorkflowFacts,
    artifacts: ContentAddressedArtifactStore,
    background: BackgroundAssignments,
    harness_registry: BunshinHarnessRegistry,
    processes: WorkerProcesses,
    settings: RoleRuntimeSettings,
    publish_worker_event: WorkerEventPublisher | None,
    register_broker_run: BrokerRunRegistrar | None,
    repository: BunshinV2Repository,
    requests: WorkflowRequests,
    runtime_db_path: Path | None,
    runtime_root: Path,
    supervisor: RoleSupervisor,
    task_ledger: TaskLedgerService,
    unregister_broker_run: BrokerRunUnregistrar | None,
    workspace_locks: WorkspaceLockRegistry,
) -> AttemptExecution:
    return AttemptExecution(
        workspace_preparation=WorkspacePreparation(
            artifacts=artifacts, attempt_inputs=attempt_inputs, harness_registry=harness_registry,
            repository=repository, requests=requests, runtime_root=runtime_root,
        ),
        verifier_context=VerifierContext(artifacts=artifacts),
        reference_binding=ReferenceBinding(repository=repository, task_ledger=task_ledger, workflow_facts=workflow_facts),
        prompt_construction=PromptConstruction(workflow_facts=workflow_facts),
        playbook_binding=PlaybookBinding(artifacts=artifacts),
        tool_policy=ToolPolicy(artifacts=artifacts, repository=repository, runtime_root=runtime_root),
        assignment_reuse=AssignmentReuse(
            artifacts=artifacts, assignment_identity=assignment_identity, assignment_retries=assignment_retries,
            background=background, repository=repository, role_checkpoints=role_checkpoints,
        ),
        role_session=RoleSession(assignment_retries=assignment_retries, repository=repository, background=background),
        harness_binding=HarnessBinding(artifacts=artifacts, repository=repository, role_checkpoints=role_checkpoints),
        assignment_replay=AssignmentReplay(artifacts=artifacts, background=background, role_checkpoints=role_checkpoints),
        attempt_admission=AttemptAdmission(repository=repository, supervisor=supervisor),
        attempt_pack=AttemptPack(settings=settings, role_checkpoints=role_checkpoints, runtime_root=runtime_root),
        attempt_publication=AttemptPublication(artifacts=artifacts, repository=repository, runtime_db_path=runtime_db_path, runtime_root=runtime_root),
        worker_execution=WorkerExecution(
            processes=processes, publish_worker_event=publish_worker_event, register_broker_run=register_broker_run,
            repository=repository, role_leases=role_leases, supervisor=supervisor,
            unregister_broker_run=unregister_broker_run, workspace_locks=workspace_locks,
        ),
        process_result=ProcessResult(repository=repository, role_checkpoints=role_checkpoints),
        terminal_validation=TerminalValidation(repository=repository, role_checkpoints=role_checkpoints),
        attempt_completion=AttemptCompletion(artifacts=artifacts, repository=repository, role_checkpoints=role_checkpoints),
        repository=repository,
        role_leases=role_leases,
        supervisor=supervisor,
        background=background,
    )
