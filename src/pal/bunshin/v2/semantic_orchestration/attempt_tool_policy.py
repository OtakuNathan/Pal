from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from pal.bunshin.v2.artifacts import ContentAddressedArtifactStore
from pal.bunshin.v2.paths import invocation_root
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.role_contracts import OrchestrationRole
from typing import Mapping
from pal.shared import BunshinInvocationPack
from pal.bunshin.v2.semantic_orchestration.verification_policy import _verification_repair_scope
from pal.bunshin.tool_guidance import merge_tool_guidance_overrides
from pal.bunshin.v2.adapters import prepare_v2_role_workspace
from pal.bunshin.v2.contract_submission import bind_architect_file
from pal.bunshin.v2.swe_verification import compile_swe_verification_tool_contract
from pal.bunshin.v2.semantic_orchestration.attempt_models import BoundRolePlaybook, InitialRolePrompt, PreparedRolePrompt, PreparedRoleWorkspace, RoleAttemptRequest


@dataclass
class ToolPolicy:
    artifacts: ContentAddressedArtifactStore
    repository: BunshinV2Repository
    runtime_root: Path

    async def execute(
        self, command: RoleAttemptRequest, stage_playbook_binding: BoundRolePlaybook,
        stage_prompt_construction: InitialRolePrompt, stage_workspace_preparation: PreparedRoleWorkspace,
    ) -> PreparedRolePrompt:
        activation = command.activation
        base_manifest_ref = stage_prompt_construction.base_manifest_ref
        binding = stage_workspace_preparation.binding
        bound_reference_refs = stage_workspace_preparation.bound_reference_refs
        fencing_token = command.fencing_token
        invocation_id = command.invocation_id
        pack = stage_playbook_binding.pack
        prepare_workspace = command.prepare_workspace
        run_id = stage_workspace_preparation.run_id
        snapshot = command.snapshot
        uses_bound_durable_workspace = stage_workspace_preparation.uses_bound_durable_workspace
        if activation.role == OrchestrationRole.VERIFIER:
            view_ref = bound_reference_refs.get("module_work_view")
            if view_ref is not None:
                tool_contract = compile_swe_verification_tool_contract(
                    self.artifacts.read_json(view_ref),
                    repair_scope=_verification_repair_scope(
                        self.repository,
                        snapshot,
                    ),
                )
                pack_value = pack.to_dict()
                metadata = dict(pack_value.get("metadata") or {})
                bunshin_v2 = dict(metadata.get("bunshin_v2") or {})
                bunshin_v2["swe_verification_tool_contract"] = tool_contract
                metadata["bunshin_v2"] = bunshin_v2
                workspace_value = dict(pack_value.get("workspace") or {})
                workspace_bunshin_v2 = dict(workspace_value.get("bunshin_v2") or {})
                workspace_bunshin_v2["swe_verification_tool_contract"] = tool_contract
                workspace_value["bunshin_v2"] = workspace_bunshin_v2
                resolved_profile = dict(pack_value.get("resolved_profile") or {})
                guidance_overrides = merge_tool_guidance_overrides(
                    resolved_profile.get("capability_guidance_overrides"),
                    tool_contract.get("guidance_overrides"),
                )
                resolved_profile["capability_guidance_overrides"] = guidance_overrides
                pack = BunshinInvocationPack.from_dict(
                    {
                        **pack_value,
                        "workspace": workspace_value,
                        "metadata": metadata,
                        "resolved_profile": resolved_profile,
                    }
                )
        if (
            prepare_workspace
            and not uses_bound_durable_workspace
            and not bool(pack.workspace.get("v2_role_workspace"))
        ):
            pack = prepare_v2_role_workspace(
                self.runtime_root,
                pack,
                run_id=run_id,
                attempt_key=f"fence-{fencing_token}",
            )
        elif not bool(pack.workspace.get("v2_role_workspace")):
            invocation_dir = invocation_root(self.runtime_root) / invocation_id
            attempt_dir = invocation_dir / "attempts" / f"fence-{fencing_token}"
            bound_workspace = dict(pack.workspace)
            bound_workspace.update(
                {
                    "run_dir": str(invocation_dir),
                    "artifact_dir": str(attempt_dir / "artifacts"),
                    "artifact_stage_dir": str(attempt_dir / "artifact-stage"),
                    "log_dir": str(attempt_dir / "logs"),
                    # Build output is attempt-local.  A durable role prompt
                    # is intentionally reused across retries, but carrying
                    # its previous fence's scratch path into the new process
                    # makes the worker inspect and write stale evidence.
                    "build_scratch_dir": str(attempt_dir / "build-scratch"),
                    "review_scratch_dir": str(
                        bound_workspace.get("review_scratch_dir")
                        or attempt_dir / "review-scratch"
                    ),
                }
            )
            for key in (
                "artifact_dir",
                "artifact_stage_dir",
                "log_dir",
                "build_scratch_dir",
                "review_scratch_dir",
            ):
                Path(str(bound_workspace[key])).mkdir(parents=True, exist_ok=True)
            pack = BunshinInvocationPack.from_dict({**pack.to_dict(), "workspace": bound_workspace})
        if activation.role == OrchestrationRole.ARCHITECT:
            architecture_binding = dict(
                binding.get("architecture_definition") or {}
            )
            template_ref = dict(
                architecture_binding.get("template_ref") or {}
            )
            if not template_ref:
                raise ValueError(
                    "FamilyBindingArtifact has no pinned architect template"
                )
            template_payload = dict(
                self.artifacts.read_json(template_ref)
            )
            if (
                str(template_payload.get("specialization_id") or "")
                != str(architecture_binding.get("specialization_id") or "")
                or str(template_payload.get("generation_hash") or "")
                != str(architecture_binding.get("generation_hash") or "")
            ):
                raise ValueError(
                    "pinned architect template does not match its Family binding"
                )
            base_contract: Mapping[str, Any] | None = None
            if base_manifest_ref is not None:
                base_record = self.repository.artifacts.read_artifact_record(
                    base_manifest_ref.sha256
                )
                if (
                    base_record is not None
                    and str(base_record.get("artifact_type") or "")
                    == "ContractArtifact"
                ):
                    base_value = dict(
                        self.artifacts.read_json(base_manifest_ref)
                    )
                    candidate = base_value.get("contract")
                    if isinstance(candidate, Mapping):
                        base_contract = dict(candidate)
            pack_value = pack.to_dict()
            bound_workspace = bind_architect_file(
                dict(pack_value.get("workspace") or {}),
                template=str(template_payload.get("template") or ""),
                base_contract=base_contract,
            )
            pack = BunshinInvocationPack.from_dict(
                {
                    **pack_value,
                    "workspace": bound_workspace,
                }
            )
        return PreparedRolePrompt(pack=pack)
