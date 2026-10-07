from __future__ import annotations
from pal.bunshin.v2.contract_runtime import ResearchMode
from pal.bunshin.v2.candidate_builder import CANDIDATE_BUILDER_CAPABILITIES
from pal.bunshin.v2.swe_verification import SWE_VERIFICATION_CAPABILITIES
from pal.bunshin.v2.verification_builder import VERIFICATION_EVIDENCE_CAPABILITIES
from pal.bunshin.v2.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.shared import BunshinInvocationPack


def apply_v2_research_capability_policy(pack: BunshinInvocationPack, *, research_mode: str) -> BunshinInvocationPack:
    mode = ResearchMode(str(research_mode or ResearchMode.LOCAL_ONLY))
    if mode == ResearchMode.EXTERNAL_ALLOWED:
        return pack
    denied = {"op_web_search", "op_browser_read"}
    return BunshinInvocationPack.from_dict(
        {
            **pack.to_dict(),
            "allowed_capabilities": [
                capability for capability in pack.allowed_capabilities if capability not in denied
            ],
        }
    )


def _role_primary_artifact_name(pack: BunshinInvocationPack) -> str:
    output_policy = dict((pack.workspace or {}).get("output_policy") or {})
    if not output_policy:
        output_policy = dict(
            (pack.resolved_profile or {}).get("effective_output_policy") or {}
        )
    primary_artifact = str(output_policy.get("primary_artifact") or "").strip()
    if not primary_artifact:
        raise ValueError("role invocation has no primary output artifact contract")
    return primary_artifact


def apply_v2_role_capability_policy(
    pack: BunshinInvocationPack,
    *,
    activation: RoleActivation,
) -> BunshinInvocationPack:
    current = set(pack.allowed_capabilities)
    current.add("op_bunshin_update_checklist")
    if activation.role == OrchestrationRole.ARCHITECT:
        allowed_authoring = {
            "op_bunshin_update_checklist",
            "op_bunshin_contract_submit",
            "op_bunshin_ask_question",
        }
    elif activation == RoleActivation(OrchestrationRole.REVIEWER, RoleMode.ARCHITECTURE):
        allowed_authoring = {
            "op_bunshin_update_checklist",
            "op_bunshin_add_finding",
            "op_bunshin_review_submit",
        }
    else:
        allowed_authoring = {
            # V2 verification has one Manager-owned submission protocol.  A
            # verifier profile may omit the built-in capability group, but
            # binding it to this role must still expose the semantic outcome
            # tools that _run_verification accepts.  Falling back to the
            # legacy VerificationPlan builder gives the worker a submit tool
            # whose durable receipt the Manager can never consume.
            OrchestrationRole.VERIFIER: {
                *SWE_VERIFICATION_CAPABILITIES,
                *VERIFICATION_EVIDENCE_CAPABILITIES,
            },
            OrchestrationRole.REVIEWER: {
                "op_bunshin_update_checklist",
                "op_bunshin_add_finding",
                "op_bunshin_review_submit",
            },
            OrchestrationRole.IMPLEMENTATION: set(CANDIDATE_BUILDER_CAPABILITIES),
        }.get(activation.role)
    if allowed_authoring is None:
        return pack
    allowed_authoring.add("op_bunshin_update_checklist")
    current.update(allowed_authoring)
    forbidden_writes = {
        "op_bunshin_artifact_write",
        "op_bunshin_artifact_edit",
    }
    if activation.role == OrchestrationRole.REVIEWER:
        forbidden_writes.update({"op_file_write", "op_file_edit"})
    if activation.role == OrchestrationRole.ARCHITECT:
        forbidden_writes.difference_update({"op_file_write", "op_file_edit"})
    if (
        activation.role == OrchestrationRole.IMPLEMENTATION
        and str(pack.profile_group or "") != "software_engineering"
    ):
        forbidden_writes.difference_update(
            {"op_bunshin_artifact_write", "op_bunshin_artifact_edit"}
        )
    capabilities = [
        capability
        for capability in sorted(current)
        if capability not in forbidden_writes
        and (not _is_authoring_capability_name(capability) or capability in allowed_authoring)
    ]
    if activation.role == OrchestrationRole.ARCHITECT:
        primary_artifact = "architect.yaml"
        allowed_output_types = ["ContractArtifact"]
    elif activation == RoleActivation(
        OrchestrationRole.REVIEWER,
        RoleMode.ARCHITECTURE,
    ):
        primary_artifact = "contract_review.json"
        allowed_output_types = ["ContractReviewArtifact"]
    elif activation.role == OrchestrationRole.REVIEWER:
        primary_artifact = "contract_review.json"
        allowed_output_types = ["ContractReviewArtifact"]
    elif activation.role == OrchestrationRole.VERIFIER:
        primary_artifact = "verification_submission.json"
        allowed_output_types = ["SemanticVerificationSubmissionArtifact"]
    else:
        software_implementation = str(pack.profile_group or "") == "software_engineering"
        primary_artifact = (
            "coder_report.json" if software_implementation else "producer_report.json"
        )
        allowed_output_types = (
            ["ModuleCoderReport", "ModuleSplitRequest"]
            if software_implementation
            else ["UnitProducerReport", "UnitSplitRequest"]
        )

    pack_value = pack.to_dict()
    workspace = dict(pack_value.get("workspace") or {})
    output_policy = dict(workspace.get("output_policy") or {})
    output_policy.update(
        {
            "primary_artifact": primary_artifact,
            "allowed_output_types": allowed_output_types,
        }
    )
    workspace["output_policy"] = output_policy
    resolved_profile = dict(pack_value.get("resolved_profile") or {})
    resolved_profile["effective_output_policy"] = dict(output_policy)
    return BunshinInvocationPack.from_dict(
        {
            **pack_value,
            "allowed_capabilities": capabilities,
            "workspace": workspace,
            "resolved_profile": resolved_profile,
        }
    )


def _is_authoring_capability_name(name: str) -> bool:
    value = str(name or "")
    return value in {"op_bunshin_add_finding", "op_bunshin_update_finding", "op_bunshin_remove_finding"} or value.startswith(
        (
            "op_bunshin_update_checklist",
            "op_bunshin_requirement",
            "op_bunshin_requirements",
            "op_bunshin_contract",
            "op_bunshin_architecture",
            "op_bunshin_developer",
            "op_bunshin_candidate",
            "op_bunshin_verification",
            "op_bunshin_review_",
            "op_bunshin_standalone_review",
        )
    )


def apply_v2_revision_scope_capability_policy(pack: BunshinInvocationPack) -> BunshinInvocationPack:
    """Revision guidance reuses the normal Architect tool surface."""

    return pack
