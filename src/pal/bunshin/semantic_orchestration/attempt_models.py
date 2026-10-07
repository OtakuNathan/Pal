from __future__ import annotations
from pal.bunshin.artifacts import ArtifactRef
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from pal.bunshin.contracts import AggregateSnapshot
from pal.bunshin.role_contracts import RoleActivation
from typing import Mapping
from pal.shared import BunshinInvocationPack
from pal.bunshin.process_lifecycle import WorkerProcessOwner
from pal.bunshin.harnesses import BunshinHarnessRegistryGeneration, BunshinHarnessSpec
from pal.bunshin.contracts import LeaseGrant


AttemptResult = tuple[dict[str, Any], ArtifactRef, ArtifactRef]


@dataclass(frozen=True)
class RoleAttemptRequest:
    effect: Mapping[str, Any]
    snapshot: AggregateSnapshot
    invocation_id: str
    lease_resource: str
    fencing_token: int
    profile: str
    activation: RoleActivation
    instruction: str
    reference_refs: Mapping[str, ArtifactRef]
    workspace_override: Mapping[str, Any] | None
    prepare_workspace: bool


@dataclass(frozen=True)
class AttemptReplay:
    result: AttemptResult


@dataclass(frozen=True)
class PreparedRoleWorkspace:
    binding: dict[str, Any]
    binding_ref: dict[str, Any]
    bound_input_entries: list[dict[str, Any]]
    bound_reference_refs: dict[str, ArtifactRef]
    contract_authoring: bool
    family_policies: dict[str, Any]
    harness_generation: BunshinHarnessRegistryGeneration
    llm_policy: dict[str, Any]
    mode: str
    preferred_harness: BunshinHarnessSpec
    request: dict[str, Any]
    role: str
    run_id: str
    uses_bound_durable_workspace: bool
    workspace: dict[str, Any]


@dataclass(frozen=True)
class PreparedVerifierContext:
    verification_tool_contract: dict[str, Any] | None


@dataclass(frozen=True)
class BoundRoleReferences:
    evaluation_generation: int
    invocation_acceptance: list[str]
    profile_group: str
    profile_name: str
    references: list[dict[str, Any]]


@dataclass(frozen=True)
class InitialRolePrompt:
    base_manifest_ref: ArtifactRef | None
    input_fingerprint: str
    pack: BunshinInvocationPack
    pinned_profile: dict[str, Any]
    revision_scope: Mapping[str, Any] | None


@dataclass(frozen=True)
class BoundRolePlaybook:
    pack: BunshinInvocationPack


@dataclass(frozen=True)
class PreparedRolePrompt:
    pack: BunshinInvocationPack


@dataclass(frozen=True)
class AssignmentReuse:
    assignment: dict[str, Any]
    durable_input_refs: dict[str, dict[str, Any]]
    pack: BunshinInvocationPack
    submission_kind: str


@dataclass(frozen=True)
class PreparedRoleSession:
    assignment: dict[str, Any]
    durable_prompt_reused: bool
    role_session: dict[str, Any]
    session_scope_kind: str
    session_subject_key: str


@dataclass(frozen=True)
class BoundRoleHarness:
    durable_prompt_reused: bool
    effective_harness_generation: str
    harness_spec: BunshinHarnessSpec
    input_fingerprint: str
    pack: BunshinInvocationPack
    pal_checkpoint_capable: bool


@dataclass(frozen=True)
class RunnableRoleAssignment:
    pack: BunshinInvocationPack


@dataclass(frozen=True)
class ClaimedRoleAttempt:
    assignment_lease: LeaseGrant
    assignment_lease_resource: str
    attempt: dict[str, Any]


@dataclass(frozen=True)
class MaterializedRolePack:
    continuation_output_path: Path
    pack: BunshinInvocationPack


@dataclass(frozen=True)
class PublishedRoleAttempt:
    argv: list[str]
    env: dict[str, str]
    pack: BunshinInvocationPack
    prompt_ref: ArtifactRef


@dataclass(frozen=True)
class ExitedRoleProcess:
    events: list[dict[str, Any]]
    owner: WorkerProcessOwner
    worker_error: str


@dataclass(frozen=True)
class CollectedRoleTerminal:
    terminal: dict[str, Any]
    terminal_payload: dict[str, Any]


@dataclass(frozen=True)
class ValidatedRoleTerminal:
    assignment_after_process: dict[str, Any] | None
