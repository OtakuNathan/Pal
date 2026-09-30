from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from pal.bunshin.v2.artifacts import ContentAddressedArtifactStore
from pal.bunshin.v2.role_contracts import OrchestrationRole
from pal.bunshin.v2.verification_builder import compile_verification_invocation_tool_contract, effective_verification_policy
from pal.bunshin.v2.role_contracts import RoleActivation, RoleMode
from pal.bunshin.v2.semantic_orchestration.attempt_models import PreparedRoleWorkspace, PreparedVerifierContext, RoleAttemptRequest


@dataclass
class VerifierContext:
    artifacts: ContentAddressedArtifactStore

    async def execute(self, command: RoleAttemptRequest, stage_workspace_preparation: PreparedRoleWorkspace) -> PreparedVerifierContext:
        activation = command.activation
        binding = stage_workspace_preparation.binding
        bound_reference_refs = stage_workspace_preparation.bound_reference_refs
        family_policies = stage_workspace_preparation.family_policies
        role = stage_workspace_preparation.role
        verification_tool_contract: dict[str, Any] | None = None
        if activation.role == OrchestrationRole.VERIFIER or activation == RoleActivation(
            OrchestrationRole.REVIEWER,
            RoleMode.STANDALONE,
        ):
            view_name = (
                "module_work_view"
                if activation.role == OrchestrationRole.VERIFIER
                else "review_request"
            )
            view_ref = bound_reference_refs.get(view_name)
            view = self.artifacts.read_json(view_ref) if view_ref is not None else {}
            system_delivery_view = (
                self.artifacts.read_json(
                    bound_reference_refs["system_delivery_view"]
                )
                if "system_delivery_view" in bound_reference_refs
                else None
            )
            family_verification_policy = dict(
                family_policies.get("verification") or {}
            )
            effective_policy = effective_verification_policy(
                work_view=view,
                verification_policy=family_verification_policy,
                system_delivery_view=system_delivery_view,
            )
            if activation.role == OrchestrationRole.VERIFIER:
                verification_tool_contract = (
                    compile_verification_invocation_tool_contract(
                        work_view=view,
                        verification_policy=family_verification_policy,
                        system_delivery_view=system_delivery_view,
                    )
                )
            verification_policy_ref = self.artifacts.put_json(
                effective_policy,
                artifact_type="VerificationPolicyArtifact",
                provenance={"family_id": str(binding.get("family_id") or ""), "role": role},
            )
            bound_reference_refs["verification_policy"] = verification_policy_ref
        return PreparedVerifierContext(verification_tool_contract=verification_tool_contract)
