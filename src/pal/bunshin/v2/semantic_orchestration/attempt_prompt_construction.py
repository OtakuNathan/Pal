from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from pal.bunshin.v2.role_contracts import OrchestrationRole
from pal.bunshin.v2.semantic_orchestration.workflow_facts import WorkflowFacts
from pal.bunshin.v2.semantic_orchestration.role_inputs import _semantic_role_input_refs
from pal.bunshin.v2.semantic_orchestration.role_inputs import _role_mode_profile_payload
from typing import Mapping
from pal.bunshin.v2.submission_drafts import AUTHORING_CONTRACT_VERSION, authoring_input_fingerprint
from pal.bunshin.v2.role_contracts import validate_family_binding_payload
from pal.shared import BunshinInvocationPack
from pal.bunshin.v2.semantic_orchestration.attempt_models import BoundRoleReferences, InitialRolePrompt, PreparedRoleWorkspace, PreparedVerifierContext, RoleAttemptRequest


@dataclass
class PromptConstruction:
    workflow_facts: WorkflowFacts

    async def execute(
        self, command: RoleAttemptRequest, stage_reference_binding: BoundRoleReferences,
        stage_verifier_context: PreparedVerifierContext, stage_workspace_preparation: PreparedRoleWorkspace,
    ) -> InitialRolePrompt:
        activation = command.activation
        binding = stage_workspace_preparation.binding
        binding_ref = stage_workspace_preparation.binding_ref
        bound_reference_refs = stage_workspace_preparation.bound_reference_refs
        contract_authoring = stage_workspace_preparation.contract_authoring
        effect = command.effect
        evaluation_generation = stage_reference_binding.evaluation_generation
        fencing_token = command.fencing_token
        instruction = command.instruction
        invocation_acceptance = stage_reference_binding.invocation_acceptance
        invocation_id = command.invocation_id
        lease_resource = command.lease_resource
        llm_policy = stage_workspace_preparation.llm_policy
        mode = stage_workspace_preparation.mode
        profile = command.profile
        profile_group = stage_reference_binding.profile_group
        profile_name = stage_reference_binding.profile_name
        references = stage_reference_binding.references
        role = stage_workspace_preparation.role
        snapshot = command.snapshot
        verification_tool_contract = stage_verifier_context.verification_tool_contract
        workspace = stage_workspace_preparation.workspace
        input_fingerprint = authoring_input_fingerprint(
            {
                "role": role,
                "mode": mode,
                "references": _semantic_role_input_refs(
                    {
                        name: ref.to_dict()
                        for name, ref in bound_reference_refs.items()
                    },
                    role=role,
                    mode=mode,
                ),
                "architecture_revision_base_submission": workspace.get(
                    "architecture_revision_base_submission"
                ),
                "architecture_revision_scope": workspace.get(
                    "architecture_revision_scope"
                ),
                "evaluation_generation": evaluation_generation,
            }
        )
        pack = BunshinInvocationPack(
            invocation_id=invocation_id,
            goal=instruction,
            instruction=instruction,
            acceptance_criteria=invocation_acceptance,
            workspace=workspace,
            profile_group=profile_group,
            profile_name=profile_name,
            bunshin_profile=profile,
            metadata={
                "bunshin_v2": {
                    "workflow_id": snapshot.workflow_id,
                    "aggregate_type": snapshot.aggregate_type.value,
                    "aggregate_id": snapshot.aggregate_id,
                    "effect_id": effect["effect_id"],
                    "invocation_id": invocation_id,
                    "lease_resource": lease_resource,
                    "lease_resource_key": lease_resource,
                    "fencing_token": fencing_token,
                    "role": role,
                    "mode": mode,
                    "role_profile_id": profile,
                    "family_binding_sha": str(binding_ref.get("sha256") or ""),
                    "submission_receipt_required": True,
                    "authoring_contract_version": AUTHORING_CONTRACT_VERSION,
                    "authoring_input_fingerprint": input_fingerprint,
                    **(
                        {
                            "verification_tool_contract": verification_tool_contract
                        }
                        if verification_tool_contract is not None
                        else {}
                    ),
                },
                "agent_session": {
                    "session_id": invocation_id,
                    "response_key": input_fingerprint,
                    "fencing_token": int(fencing_token),
                },
                "requirements_brief": {
                    "references": references,
                    "research_mode": snapshot.payload.get("research_mode", "local_only"),
                },
                "allow_text_only_completion": False,
                **(
                    {"temperature": llm_policy["temperature"]}
                    if "temperature" in llm_policy
                    else {}
                ),
                **(
                    {"llm_round_timeout_seconds": llm_policy["llm_round_timeout_seconds"]}
                    if "llm_round_timeout_seconds" in llm_policy
                    else {}
                ),
            },
        )
        base_manifest_ref = (
            self.workflow_facts.revision_input_base_manifest_ref(snapshot)
            if activation.role == OrchestrationRole.ARCHITECT
            else None
        )
        revision_scope: Mapping[str, Any] | None = None
        if activation.role == OrchestrationRole.ARCHITECT and "revision_scope" in bound_reference_refs:
            if contract_authoring:
                revision_scope = dict(workspace.get("architecture_revision_scope") or {}) or None
            else:
                revision_scope = {"manager_routed_findings": True}
        role_binding = dict(validate_family_binding_payload(binding)[role])
        pinned_profile = dict(role_binding.get("role_profile") or {})
        if not pinned_profile:
            raise ValueError(
                f"FamilyBindingArtifact has no pinned role profile for role {role}"
            )
        pinned_profile = _role_mode_profile_payload(pinned_profile, mode=mode)
        return InitialRolePrompt(base_manifest_ref=base_manifest_ref, input_fingerprint=input_fingerprint, pack=pack, pinned_profile=pinned_profile, revision_scope=revision_scope)
