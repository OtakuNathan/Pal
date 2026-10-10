from __future__ import annotations
import pal.bunshin.profiles as _dependency_profiles
import pal.bunshin.semantic_orchestration.role_policy as _dependency_role_policy
from dataclasses import dataclass
from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.role_contracts import OrchestrationRole
from typing import Mapping
from pal.shared import BunshinInvocationPack
from pal.bunshin.semantic_orchestration.role_policy import apply_revision_scope_capability_policy
from pal.bunshin.semantic_orchestration.verification_policy import _manager_routed_findings
from pal.bunshin.semantic_orchestration.verification_policy import _manager_required_system_scenario_work_items
from pal.bunshin.role_protocol import stable_hash
from pal.bunshin.semantic_orchestration.attempt_models import BoundRolePlaybook, InitialRolePrompt, PreparedRoleWorkspace, RoleAttemptRequest


@dataclass
class PlaybookBinding:
    artifacts: ContentAddressedArtifactStore

    async def execute(self, command: RoleAttemptRequest, stage_prompt_construction: InitialRolePrompt, stage_workspace_preparation: PreparedRoleWorkspace) -> BoundRolePlaybook:
        activation = command.activation
        binding = stage_workspace_preparation.binding
        bound_reference_refs = stage_workspace_preparation.bound_reference_refs
        mode = stage_workspace_preparation.mode
        pack = stage_prompt_construction.pack
        pinned_profile = stage_prompt_construction.pinned_profile
        revision_scope = stage_prompt_construction.revision_scope
        role = stage_workspace_preparation.role
        snapshot = command.snapshot
        pack = _dependency_profiles.resolve_pinned_bunshin_pack(
            pack,
            profile_payload=pinned_profile,
            family_payload=binding,
        )
        role_protocol = dict(pinned_profile.get("role") or {})
        if str(role_protocol.get("kind") or "") != role:
            raise ValueError(
                f"pinned role protocol kind does not match activation {role}"
            )
        if mode not in {
            str(item)
            for item in list(role_protocol.get("modes") or [])
        }:
            raise ValueError(
                f"pinned role protocol does not support mode {mode}"
            )
        playbook = dict(role_protocol.get("playbook") or {})
        playbook_steps = [
            dict(item)
            for item in list(playbook.get("steps") or [])
            if isinstance(item, Mapping)
        ]
        if playbook_steps:
            pack_value = pack.to_dict()
            metadata = dict(pack_value.get("metadata") or {})
            bunshin_v2 = dict(metadata.get("bunshin_v2") or {})
            bunshin_v2["role_protocol"] = role_protocol
            work_item_seed = [
                {
                    "kind": "phase",
                    "summary": str(item.get("key") or "").replace("_", " "),
                    "status": "pending",
                    "origin": "role_playbook",
                    "required": True,
                }
                for item in playbook_steps
                if str(item.get("key") or "").strip()
            ]
            routed_seen: set[str] = set()
            for reference_name in (
                "repair_bill",
                "prior_finding",
                "revision_finding",
                "replan_finding_batch",
            ):
                reference = bound_reference_refs.get(reference_name)
                if reference is None:
                    continue
                payload = dict(
                    self.artifacts.read_json(reference)
                )
                for finding in _manager_routed_findings(payload):
                    identity = str(
                        finding.get("finding_id")
                        or finding.get("finding_key")
                        or stable_hash(finding)[:16]
                    )
                    if identity in routed_seen:
                        continue
                    routed_seen.add(identity)
                    work_item_seed.append(
                        {
                            "kind": "task",
                            "summary": f"resolve finding: {identity}",
                            "status": "pending",
                            "origin": "manager_routed_finding",
                            "required": True,
                        }
                    )
            if (
                activation.role == OrchestrationRole.VERIFIER
                and "system_delivery_view" in bound_reference_refs
            ):
                system_delivery = self.artifacts.read_json(
                    bound_reference_refs["system_delivery_view"]
                )
                work_item_seed.extend(
                    _manager_required_system_scenario_work_items(system_delivery)
                )
            bunshin_v2["work_item_seed"] = work_item_seed
            metadata["bunshin_v2"] = bunshin_v2
            # Keep derived role protocol data in the invocation workspace as
            # well as metadata.  The runner merges both projections for live
            # tools, but a checkpoint/recovery probe and the model's visible
            # pack may inspect workspace directly.  Having one complete
            # assignment binding prevents a missing checklist seed or role
            # protocol from masquerading as a submission/orchestration bug.
            workspace_value = dict(pack_value.get("workspace") or {})
            workspace_bunshin_v2 = dict(workspace_value.get("bunshin_v2") or {})
            workspace_bunshin_v2.update(
                {
                    key: value
                    for key, value in bunshin_v2.items()
                    if key not in workspace_bunshin_v2
                }
            )
            workspace_value["bunshin_v2"] = workspace_bunshin_v2
            pack = BunshinInvocationPack.from_dict(
                {**pack_value, "workspace": workspace_value, "metadata": metadata}
            )
        pack = _dependency_role_policy.apply_role_capability_policy(pack, activation=activation)
        if activation.role == OrchestrationRole.ARCHITECT and revision_scope is not None:
            pack = apply_revision_scope_capability_policy(pack)
        pack = _dependency_role_policy.apply_research_capability_policy(
            pack,
            research_mode=str(snapshot.payload.get("research_mode") or "local_only"),
        )
        return BoundRolePlaybook(pack=pack)
