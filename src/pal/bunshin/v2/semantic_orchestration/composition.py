from __future__ import annotations
from pal.bunshin.v2.semantic_orchestration.runtime_settings import RoleRuntimeSettings
from pal.bunshin.v2.semantic_orchestration.workflow_requests import WorkflowRequests
from pal.bunshin.v2.semantic_orchestration.callbacks import HumanReviewPublisher
from pal.bunshin.v2.semantic_orchestration.callbacks import WorkerEventPublisher
from pal.bunshin.v2.semantic_orchestration.callbacks import WorkflowEventPublisher
from pal.bunshin.v2.semantic_orchestration.callbacks import BrokerRunRegistrar
from pal.bunshin.v2.semantic_orchestration.callbacks import BrokerRunUnregistrar
from pal.bunshin.v2.semantic_orchestration.callbacks import SkillInjector
from dataclasses import dataclass
from pathlib import Path
from pal.bunshin.harnesses import BunshinHarnessRegistry
from pal.bunshin.v2.workspace_resources import WorkspaceLockRegistry
from pal.bunshin.v2.background_assignments import BackgroundAssignments
from pal.bunshin.v2.worker_processes import WorkerProcesses
from pal.bunshin.v2.service import BunshinV2WorkflowService
from pal.bunshin.v2.role_runtime import RoleSupervisor
from pal.bunshin.v2.semantic_orchestration.attempt_composition import build_attempt_execution
from pal.bunshin.v2.semantic_orchestration.assignment_retries import AssignmentRetries
from pal.bunshin.v2.semantic_orchestration.attempt_inputs import AttemptInputs
from pal.bunshin.v2.semantic_orchestration.effect_reads import EffectReads
from pal.bunshin.v2.semantic_orchestration.plan_assignments import PlanAssignments
from pal.bunshin.v2.semantic_orchestration.role_checkpoints import RoleCheckpoints
from pal.bunshin.v2.semantic_orchestration.role_cleanup import RoleCleanup
from pal.bunshin.v2.semantic_orchestration.role_reports import RoleReports
from pal.bunshin.v2.semantic_orchestration.workflow_facts import WorkflowFacts
from pal.bunshin.v2.semantic_orchestration.assignment_identity import AssignmentIdentity
from pal.bunshin.v2.semantic_orchestration.final_delivery import FinalDelivery
from pal.bunshin.v2.semantic_orchestration.human_review import HumanReview
from pal.bunshin.v2.semantic_orchestration.null_execution import NullExecution
from pal.bunshin.v2.semantic_orchestration.review_delivery import ReviewDelivery
from pal.bunshin.v2.semantic_orchestration.role_leases import RoleLeases
from pal.bunshin.v2.semantic_orchestration.verifier_tests import VerifierTests
from pal.bunshin.v2.semantic_orchestration.architecture_snapshot import ArchitectureSnapshot
from pal.bunshin.v2.semantic_orchestration.assignment_failures import AssignmentFailures
from pal.bunshin.v2.semantic_orchestration.attempt_execution import AttemptExecution
from pal.bunshin.v2.semantic_orchestration.implementation_snapshot import ImplementationSnapshot
from pal.bunshin.v2.semantic_orchestration.node_admission import NodeAdmission
from pal.bunshin.v2.semantic_orchestration.verification_completion import VerificationCompletion
from pal.bunshin.v2.semantic_orchestration.verification_settlement import VerificationSettlement
from pal.bunshin.v2.semantic_orchestration.dependency_repair_capture import DependencyRepairCapture
from pal.bunshin.v2.semantic_orchestration.dependency_repair_runtime import DependencyRepairRuntime
from pal.bunshin.v2.semantic_orchestration.architecture_authoring import ArchitectureAuthoring
from pal.bunshin.v2.semantic_orchestration.architecture_review import ArchitectureReview
from pal.bunshin.v2.semantic_orchestration.assignment_execution import AssignmentExecution
from pal.bunshin.v2.semantic_orchestration.implementation_run import ImplementationRun
from pal.bunshin.v2.semantic_orchestration.verification_run import VerificationRun
from pal.bunshin.v2.semantic_orchestration.verification_snapshot import VerificationSnapshot
from pal.bunshin.v2.semantic_orchestration.architecture_stage import ArchitectureStage
from pal.bunshin.v2.semantic_orchestration.assignment_recovery import AssignmentRecovery
from pal.bunshin.v2.semantic_orchestration.node_control import NodeControl
from pal.bunshin.v2.semantic_orchestration.standalone_review import StandaloneReview
from pal.bunshin.v2.semantic_orchestration.aggregate_control import AggregateControl
from pal.bunshin.v2.semantic_orchestration.role_control import RoleControl
from pal.bunshin.v2.semantic_orchestration.effect_dispatch import EffectDispatch
from typing import Callable


@dataclass(frozen=True)
class SemanticComponents:
    assignment_retries: AssignmentRetries
    attempt_inputs: AttemptInputs
    effect_reads: EffectReads
    plan_assignments: PlanAssignments
    role_checkpoints: RoleCheckpoints
    role_cleanup: RoleCleanup
    role_reports: RoleReports
    workflow_facts: WorkflowFacts
    assignment_identity: AssignmentIdentity
    final_delivery: FinalDelivery
    human_review: HumanReview
    null_execution: NullExecution
    review_delivery: ReviewDelivery
    role_leases: RoleLeases
    verifier_tests: VerifierTests
    architecture_snapshot: ArchitectureSnapshot
    assignment_failures: AssignmentFailures
    attempt_execution: AttemptExecution
    implementation_snapshot: ImplementationSnapshot
    node_admission: NodeAdmission
    verification_completion: VerificationCompletion
    verification_settlement: VerificationSettlement
    architecture_authoring: ArchitectureAuthoring
    architecture_review: ArchitectureReview
    assignment_execution: AssignmentExecution
    implementation_run: ImplementationRun
    verification_run: VerificationRun
    verification_snapshot: VerificationSnapshot
    architecture_stage: ArchitectureStage
    assignment_recovery: AssignmentRecovery
    node_control: NodeControl
    standalone_review: StandaloneReview
    aggregate_control: AggregateControl
    role_control: RoleControl
    effect_dispatch: EffectDispatch


def build_semantic_components(
    background: BackgroundAssignments,
    harness_registry: BunshinHarnessRegistry,
    inject_skill: SkillInjector | None,
    processes: WorkerProcesses,
    publish_human_review: HumanReviewPublisher | None,
    publish_worker_event: WorkerEventPublisher | None,
    publish_workflow_event: WorkflowEventPublisher | None,
    register_broker_run: BrokerRunRegistrar | None,
    requests: WorkflowRequests,
    runtime_db_path: Path | None,
    service: BunshinV2WorkflowService,
    settings: RoleRuntimeSettings,
    supervisor: RoleSupervisor,
    unregister_broker_run: BrokerRunUnregistrar | None,
    workspace_locks: WorkspaceLockRegistry,
) -> SemanticComponents:
    assignment_retries, attempt_inputs, effect_reads, plan_assignments, role_checkpoints, role_cleanup, role_reports, workflow_facts = build_support(
        background, inject_skill, processes, requests, service,
    )
    assignment_identity, final_delivery, human_review, null_execution, review_delivery, role_leases, verifier_tests, architecture_snapshot = build_role_resources(
        background, effect_reads, processes, publish_human_review, publish_workflow_event, requests, role_cleanup,
        role_reports, service, workflow_facts, workspace_locks,
    )
    assignment_failures, attempt_execution, implementation_snapshot, node_admission, verification_completion, verification_settlement, architecture_authoring, architecture_review = build_role_execution(
        assignment_identity, assignment_retries, attempt_inputs, background, effect_reads, harness_registry,
        null_execution, plan_assignments, processes, publish_worker_event, register_broker_run, requests,
        role_checkpoints, role_cleanup, role_leases, role_reports, runtime_db_path, service, settings, supervisor,
        unregister_broker_run, verifier_tests, workflow_facts, workspace_locks,
    )
    assignment_execution, implementation_run, verification_run, verification_snapshot, architecture_stage, assignment_recovery, node_control, standalone_review = build_role_handlers(
        architecture_authoring, architecture_review, assignment_failures, assignment_identity, assignment_retries,
        attempt_execution, background, effect_reads, implementation_snapshot, node_admission, plan_assignments,
        requests, role_cleanup, role_leases, role_reports, service, verification_completion, verification_settlement,
        workflow_facts, workspace_locks,
    )
    dependency_repairs = DependencyRepairRuntime(
        artifacts=service.artifacts, repository=service.repository, effect_reads=effect_reads,
        cleanup=role_cleanup, leases=role_leases, workspace_locks=workspace_locks,
        collector=DependencyRepairCapture(verification_settlement, role_checkpoints),
    )
    verification_settlement.dependency_repair_registration = dependency_repairs.register
    node_control.dependency_repairs = dependency_repairs
    aggregate_control, role_control, effect_dispatch = build_dispatch(
        architecture_review, architecture_snapshot, architecture_stage, assignment_execution, background, effect_reads,
        final_delivery, human_review, implementation_run, implementation_snapshot, node_admission, node_control,
        review_delivery, role_cleanup, service, standalone_review, verification_run, verification_snapshot,
    )
    effect_dispatch.handlers = {**effect_dispatch.handlers,
                                "reconcile_dependency_repairs": dependency_repairs.reconcile}
    return SemanticComponents(
        assignment_retries=assignment_retries,
        attempt_inputs=attempt_inputs,
        effect_reads=effect_reads,
        plan_assignments=plan_assignments,
        role_checkpoints=role_checkpoints,
        role_cleanup=role_cleanup,
        role_reports=role_reports,
        workflow_facts=workflow_facts,
        assignment_identity=assignment_identity,
        final_delivery=final_delivery,
        human_review=human_review,
        null_execution=null_execution,
        review_delivery=review_delivery,
        role_leases=role_leases,
        verifier_tests=verifier_tests,
        architecture_snapshot=architecture_snapshot,
        assignment_failures=assignment_failures,
        attempt_execution=attempt_execution,
        implementation_snapshot=implementation_snapshot,
        node_admission=node_admission,
        verification_completion=verification_completion,
        verification_settlement=verification_settlement,
        architecture_authoring=architecture_authoring,
        architecture_review=architecture_review,
        assignment_execution=assignment_execution,
        implementation_run=implementation_run,
        verification_run=verification_run,
        verification_snapshot=verification_snapshot,
        architecture_stage=architecture_stage,
        assignment_recovery=assignment_recovery,
        node_control=node_control,
        standalone_review=standalone_review,
        aggregate_control=aggregate_control,
        role_control=role_control,
        effect_dispatch=effect_dispatch,
    )


def build_support(
    background: BackgroundAssignments,
    inject_skill: SkillInjector | None,
    processes: WorkerProcesses,
    requests: WorkflowRequests,
    service: BunshinV2WorkflowService,
) -> tuple[AssignmentRetries, AttemptInputs, EffectReads, PlanAssignments, RoleCheckpoints, RoleCleanup, RoleReports, WorkflowFacts]:
    assignment_retries = AssignmentRetries(
        background=background,
        repository=service.repository,
    )
    attempt_inputs = AttemptInputs(
        artifacts=service.artifacts,
    )
    effect_reads = EffectReads(
        repository=service.repository,
    )
    plan_assignments = PlanAssignments(
        repository=service.repository,
    )
    role_checkpoints = RoleCheckpoints(
        artifacts=service.artifacts,
        inject_skill=inject_skill,
        repository=service.repository,
        runtime_root=service.runtime_root,
    )
    role_cleanup = RoleCleanup(
        processes=processes,
        runtime_root=service.runtime_root,
        background=background,
    )
    role_reports = RoleReports(
        artifacts=service.artifacts,
        repository=service.repository,
        requests=requests,
        runtime_root=service.runtime_root,
    )
    workflow_facts = WorkflowFacts(
        artifacts=service.artifacts,
        repository=service.repository,
    )
    return assignment_retries, attempt_inputs, effect_reads, plan_assignments, role_checkpoints, role_cleanup, role_reports, workflow_facts


def build_role_resources(
    background: BackgroundAssignments,
    effect_reads: EffectReads,
    processes: WorkerProcesses,
    publish_human_review: HumanReviewPublisher | None,
    publish_workflow_event: WorkflowEventPublisher | None,
    requests: WorkflowRequests,
    role_cleanup: RoleCleanup,
    role_reports: RoleReports,
    service: BunshinV2WorkflowService,
    workflow_facts: WorkflowFacts,
    workspace_locks: WorkspaceLockRegistry,
) -> tuple[AssignmentIdentity, FinalDelivery, HumanReview, NullExecution, ReviewDelivery, RoleLeases, VerifierTests, ArchitectureSnapshot]:
    assignment_identity = AssignmentIdentity(
        effect_reads=effect_reads,
        background=background,
        repository=service.repository,
    )
    final_delivery = FinalDelivery(
        effect_reads=effect_reads,
        workflow_facts=workflow_facts,
        artifacts=service.artifacts,
        repository=service.repository,
        requests=requests,
        runtime_root=service.runtime_root,
    )
    human_review = HumanReview(
        effect_reads=effect_reads,
        role_reports=role_reports,
        artifacts=service.artifacts,
        publish_human_review=publish_human_review,
        publish_workflow_event=publish_workflow_event,
        render_human_review=service.render_human_review_markdown,
        repository=service.repository,
        runtime_root=service.runtime_root,
        task_ledger=service.task_ledger,
    )
    null_execution = NullExecution(
        effect_reads=effect_reads,
        workflow_facts=workflow_facts,
        artifacts=service.artifacts,
        repository=service.repository,
        runtime_root=service.runtime_root,
    )
    review_delivery = ReviewDelivery(
        effect_reads=effect_reads,
        artifacts=service.artifacts,
        publish_human_review=publish_human_review,
        repository=service.repository,
        requests=requests,
    )
    role_leases = RoleLeases(
        effect_reads=effect_reads,
        role_cleanup=role_cleanup,
        workflow_facts=workflow_facts,
        processes=processes,
        repository=service.repository,
        workspace_locks=workspace_locks,
    )
    verifier_tests = VerifierTests(
        workflow_facts=workflow_facts,
        artifacts=service.artifacts,
    )
    architecture_snapshot = ArchitectureSnapshot(
        effect_reads=effect_reads,
        role_cleanup=role_cleanup,
        role_leases=role_leases,
        artifacts=service.artifacts,
        repository=service.repository,
        requests=requests,
        skeleton=service.skeleton,
        workspace_locks=workspace_locks,
    )
    return assignment_identity, final_delivery, human_review, null_execution, review_delivery, role_leases, verifier_tests, architecture_snapshot


def build_role_execution(
    assignment_identity: AssignmentIdentity,
    assignment_retries: AssignmentRetries,
    attempt_inputs: AttemptInputs,
    background: BackgroundAssignments,
    effect_reads: EffectReads,
    harness_registry: BunshinHarnessRegistry,
    null_execution: NullExecution,
    plan_assignments: PlanAssignments,
    processes: WorkerProcesses,
    publish_worker_event: WorkerEventPublisher | None,
    register_broker_run: BrokerRunRegistrar | None,
    requests: WorkflowRequests,
    role_checkpoints: RoleCheckpoints,
    role_cleanup: RoleCleanup,
    role_leases: RoleLeases,
    role_reports: RoleReports,
    runtime_db_path: Path | None,
    service: BunshinV2WorkflowService,
    settings: RoleRuntimeSettings,
    supervisor: RoleSupervisor,
    unregister_broker_run: BrokerRunUnregistrar | None,
    verifier_tests: VerifierTests,
    workflow_facts: WorkflowFacts,
    workspace_locks: WorkspaceLockRegistry,
) -> tuple[AssignmentFailures, AttemptExecution, ImplementationSnapshot, NodeAdmission, VerificationCompletion, VerificationSettlement, ArchitectureAuthoring, ArchitectureReview]:
    assignment_failures = AssignmentFailures(
        effect_reads=effect_reads,
        role_leases=role_leases,
        artifacts=service.artifacts,
        repository=service.repository,
    )
    attempt_execution = build_attempt_execution(
        assignment_identity=assignment_identity,
        assignment_retries=assignment_retries,
        attempt_inputs=attempt_inputs,
        role_checkpoints=role_checkpoints,
        role_leases=role_leases,
        workflow_facts=workflow_facts,
        artifacts=service.artifacts,
        background=background,
        harness_registry=harness_registry,
        processes=processes,
        settings=settings,
        publish_worker_event=publish_worker_event,
        register_broker_run=register_broker_run,
        repository=service.repository,
        requests=requests,
        runtime_db_path=runtime_db_path,
        runtime_root=service.runtime_root,
        supervisor=supervisor,
        task_ledger=service.task_ledger,
        unregister_broker_run=unregister_broker_run,
        workspace_locks=workspace_locks,
    )
    implementation_snapshot = ImplementationSnapshot(
        effect_reads=effect_reads,
        role_cleanup=role_cleanup,
        role_leases=role_leases,
        workflow_facts=workflow_facts,
        artifacts=service.artifacts,
        repository=service.repository,
        runtime_root=service.runtime_root,
        workspace_locks=workspace_locks,
    )
    node_admission = NodeAdmission(
        effect_reads=effect_reads,
        null_execution=null_execution,
        verifier_tests=verifier_tests,
        workflow_facts=workflow_facts,
        repository=service.repository,
    )
    verification_completion = VerificationCompletion(
        assignment_identity=assignment_identity,
        role_reports=role_reports,
        artifacts=service.artifacts,
        repository=service.repository,
    )
    verification_settlement = VerificationSettlement(
        role_reports=role_reports,
        verifier_tests=verifier_tests,
        artifacts=service.artifacts,
        repository=service.repository,
    )
    architecture_authoring = ArchitectureAuthoring(
        assignment_identity=assignment_identity,
        attempt_execution=attempt_execution,
        plan_assignments=plan_assignments,
        role_reports=role_reports,
        workflow_facts=workflow_facts,
        artifacts=service.artifacts,
        repository=service.repository,
        requests=requests,
        skeleton=service.skeleton,
    )
    architecture_review = ArchitectureReview(
        assignment_identity=assignment_identity,
        attempt_execution=attempt_execution,
        effect_reads=effect_reads,
        plan_assignments=plan_assignments,
        role_reports=role_reports,
        workflow_facts=workflow_facts,
        artifacts=service.artifacts,
        repository=service.repository,
        requests=requests,
        skeleton=service.skeleton,
    )
    return assignment_failures, attempt_execution, implementation_snapshot, node_admission, verification_completion, verification_settlement, architecture_authoring, architecture_review


def build_role_handlers(
    architecture_authoring: ArchitectureAuthoring,
    architecture_review: ArchitectureReview,
    assignment_failures: AssignmentFailures,
    assignment_identity: AssignmentIdentity,
    assignment_retries: AssignmentRetries,
    attempt_execution: AttemptExecution,
    background: BackgroundAssignments,
    effect_reads: EffectReads,
    implementation_snapshot: ImplementationSnapshot,
    node_admission: NodeAdmission,
    plan_assignments: PlanAssignments,
    requests: WorkflowRequests,
    role_cleanup: RoleCleanup,
    role_leases: RoleLeases,
    role_reports: RoleReports,
    service: BunshinV2WorkflowService,
    verification_completion: VerificationCompletion,
    verification_settlement: VerificationSettlement,
    workflow_facts: WorkflowFacts,
    workspace_locks: WorkspaceLockRegistry,
) -> tuple[AssignmentExecution, ImplementationRun, VerificationRun, VerificationSnapshot, ArchitectureStage, AssignmentRecovery, NodeControl, StandaloneReview]:
    assignment_execution = AssignmentExecution(
        assignment_failures=assignment_failures,
        assignment_identity=assignment_identity,
        assignment_retries=assignment_retries,
        role_leases=role_leases,
        background=background,
        repository=service.repository,
    )
    implementation_run = ImplementationRun(
        assignment_identity=assignment_identity,
        attempt_execution=attempt_execution,
        effect_reads=effect_reads,
        role_leases=role_leases,
        role_reports=role_reports,
        workflow_facts=workflow_facts,
        artifacts=service.artifacts,
        contracts=service.contracts,
        repository=service.repository,
    )
    verification_run = VerificationRun(
        attempt_execution=attempt_execution,
        effect_reads=effect_reads,
        role_leases=role_leases,
        verification_completion=verification_completion,
        workflow_facts=workflow_facts,
        artifacts=service.artifacts,
        contracts=service.contracts,
        runtime_root=service.runtime_root,
    )
    verification_snapshot = VerificationSnapshot(
        effect_reads=effect_reads,
        role_cleanup=role_cleanup,
        verification_settlement=verification_settlement,
        artifacts=service.artifacts,
        repository=service.repository,
        workspace_locks=workspace_locks,
    )
    architecture_stage = ArchitectureStage(
        architecture_authoring=architecture_authoring,
        assignment_identity=assignment_identity,
        attempt_execution=attempt_execution,
        effect_reads=effect_reads,
        plan_assignments=plan_assignments,
        role_reports=role_reports,
        workflow_facts=workflow_facts,
        artifacts=service.artifacts,
        repository=service.repository,
        requests=requests,
    )
    assignment_recovery = AssignmentRecovery(
        assignment_execution=assignment_execution,
        assignment_identity=assignment_identity,
        assignment_retries=assignment_retries,
        background=background,
        repository=service.repository,
    )
    node_control = NodeControl(
        effect_reads=effect_reads,
        implementation_snapshot=implementation_snapshot,
        node_admission=node_admission,
        role_cleanup=role_cleanup,
        verification_snapshot=verification_snapshot,
        artifacts=service.artifacts,
        repository=service.repository,
        workspace_locks=workspace_locks,
    )
    standalone_review = StandaloneReview(
        architecture_review=architecture_review,
        assignment_identity=assignment_identity,
        attempt_execution=attempt_execution,
        effect_reads=effect_reads,
        role_leases=role_leases,
        role_reports=role_reports,
        workflow_facts=workflow_facts,
        artifacts=service.artifacts,
        repository=service.repository,
        requests=requests,
        runtime_root=service.runtime_root,
        skeleton=service.skeleton,
    )
    return assignment_execution, implementation_run, verification_run, verification_snapshot, architecture_stage, assignment_recovery, node_control, standalone_review


def build_dispatch(
    architecture_review: ArchitectureReview,
    architecture_snapshot: ArchitectureSnapshot,
    architecture_stage: ArchitectureStage,
    assignment_execution: AssignmentExecution,
    background: BackgroundAssignments,
    effect_reads: EffectReads,
    final_delivery: FinalDelivery,
    human_review: HumanReview,
    implementation_run: ImplementationRun,
    implementation_snapshot: ImplementationSnapshot,
    node_admission: NodeAdmission,
    node_control: NodeControl,
    review_delivery: ReviewDelivery,
    role_cleanup: RoleCleanup,
    service: BunshinV2WorkflowService,
    standalone_review: StandaloneReview,
    verification_run: VerificationRun,
    verification_snapshot: VerificationSnapshot,
) -> tuple[AggregateControl, RoleControl, EffectDispatch]:
    aggregate_control = AggregateControl(
        architecture_review=architecture_review,
        architecture_snapshot=architecture_snapshot,
        architecture_stage=architecture_stage,
        effect_reads=effect_reads,
        human_review=human_review,
        node_admission=node_admission,
        review_delivery=review_delivery,
        role_cleanup=role_cleanup,
        repository=service.repository,
    )
    role_control = RoleControl(
        aggregate_control=aggregate_control,
        node_control=node_control,
    )
    handlers = {
        'admit_architect_role': architecture_stage.run_architecture_stage,
        'admit_implementation_role': node_admission.handle_admit_implementation_role,
        'admit_reviewer_role': node_admission.handle_admit_reviewer_role,
        'admit_verifier_role': node_admission.handle_admit_verifier_role,
        'cancel_role': role_control.cancel_role,
        'materialize_plan_revision': human_review.handle_materialize_plan_revision_status,
        'pause_role': role_control.pause_role,
        'publish_architecture_review_request': human_review.publish_human_architecture_review,
        'publish_final_deliverable': final_delivery.publish_final_deliverable,
        'publish_review_report': review_delivery.publish_standalone_report,
        'quiesce_architect_role': architecture_snapshot.quiesce_architect_role,
        'quiesce_implementation_role': implementation_snapshot.quiesce_node,
        'quiesce_role_for_triage': role_control.quiesce_role_for_triage,
        'quiesce_verifier_role': verification_snapshot.quiesce_verifier_role,
        'reconcile_semantic_state': role_control.reconcile_semantic_state,
        'resume_semantic_state': role_control.resume_semantic_state,
        'run_implementation_role': implementation_run.run_implementation_role,
        'run_reviewer_role': standalone_review.run_reviewer_role,
        'run_verifier_role': verification_run.run_verification_role,
        'snapshot_architect_result': architecture_snapshot.snapshot_architect_result,
        'snapshot_implementation_result': implementation_snapshot.snapshot_implementation_result,
        'snapshot_verifier_result': verification_snapshot.handle_snapshot_semantic_verification,
    }
    effect_dispatch = EffectDispatch(
        assignment_execution=assignment_execution,
        effect_reads=effect_reads,
        background=background,
        handlers=handlers,
    )
    return aggregate_control, role_control, effect_dispatch
