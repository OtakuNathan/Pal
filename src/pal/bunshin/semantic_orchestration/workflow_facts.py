from __future__ import annotations
import pal.bunshin.execution_values as _dependency_execution_values
from pal.bunshin.semantic_orchestration.review_results import _ref_from_mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from pal.bunshin.adapters import ARTIFACT_BUNDLE_ADAPTER, SOFTWARE_GIT_ADAPTER, artifact_tree_fingerprint
from pal.bunshin.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.contracts import AggregateSnapshot, AggregateType
from pal.bunshin.execution_values import workspace_content_fingerprint
from pal.bunshin.repository import BunshinV2Repository
from pal.bunshin.replan import architecture_revision_finding_value
from pal.bunshin.role_contracts import OrchestrationRole, family_execution_adapter, validate_family_binding_payload


@dataclass
class WorkflowFacts:
    artifacts: ContentAddressedArtifactStore
    repository: BunshinV2Repository

    @staticmethod
    def execution_adapter(node: AggregateSnapshot) -> str:
        adapter = str(node.payload.get("execution_adapter") or "").strip()
        if adapter not in {
            SOFTWARE_GIT_ADAPTER,
            ARTIFACT_BUNDLE_ADAPTER,
        }:
            raise ValueError(
                "DAG node has no supported bound execution adapter"
            )
        return adapter

    def workspace_fingerprint(self, node: AggregateSnapshot, workspace: Path) -> str:
        if self.execution_adapter(node) == ARTIFACT_BUNDLE_ADAPTER:
            return artifact_tree_fingerprint(workspace)
        return _dependency_execution_values.workspace_content_fingerprint(workspace)

    @staticmethod
    def revision_input_base_manifest_ref(revision: AggregateSnapshot) -> ArtifactRef | None:
        """Return the immediate manifest a revision repairs, never an older ancestor."""

        current = revision.payload.get("architecture_manifest_ref")
        finding = architecture_revision_finding_value(revision.payload)
        if current and finding:
            return _ref_from_mapping(current)
        base = revision.payload.get("base_architecture_manifest_ref")
        return _ref_from_mapping(base) if base else None

    def profile_for_role(self, workflow_id: str, role: str) -> str:
        role = OrchestrationRole(str(role)).value
        workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        if workflow is None:
            raise ValueError(f"workflow not found while resolving role {role}: {workflow_id}")
        binding_ref = dict(workflow.payload.get("family_binding_ref") or {})
        if not binding_ref:
            raise ValueError(f"workflow has no FamilyBindingArtifact: {workflow_id}")
        binding = dict(self.artifacts.read_json(binding_ref))
        role_binding = dict(validate_family_binding_payload(binding)[role])
        profile = str(
            dict(role_binding.get("role_profile") or {}).get("canonical_profile_id")
            or dict(role_binding.get("role_profile") or {}).get("bunshin_profile")
            or ""
        ).strip()
        if not profile:
            raise ValueError(f"family {binding.get('family_id')} does not bind role {role}")
        return profile

    def role_binding(
        self,
        workflow_id: str,
        role: str,
    ) -> dict[str, Any]:
        role = OrchestrationRole(str(role)).value
        workflow = self.repository.snapshots.read_snapshot(
            AggregateType.WORKFLOW,
            workflow_id,
        )
        if workflow is None:
            raise ValueError(
                f"workflow not found while resolving role {role}: "
                f"{workflow_id}"
            )
        binding_ref = dict(
            workflow.payload.get("family_binding_ref") or {}
        )
        if not binding_ref:
            raise ValueError(
                f"workflow has no FamilyBindingArtifact: {workflow_id}"
            )
        binding = dict(self.artifacts.read_json(binding_ref))
        return dict(validate_family_binding_payload(binding)[role])

    def role_participant_kind(self, workflow_id: str, role: str) -> str:
        return str(self.role_binding(workflow_id, role)["participant"])

    def uses_git_skeleton(self, workflow_id: str) -> bool:
        workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        if workflow is None:
            return False
        binding_ref = dict(getattr(workflow, "payload", {}).get("family_binding_ref") or {})
        if not binding_ref:
            return False
        binding = dict(self.artifacts.read_json(binding_ref))
        validate_family_binding_payload(binding)
        return (
            family_execution_adapter(binding.get("execution_adapter"))
            == SOFTWARE_GIT_ADAPTER
        )

    def workflow_policy(self, workflow_id: str, name: str) -> dict[str, Any]:
        workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        if workflow is None:
            return {}
        binding_ref = dict(workflow.payload.get("family_binding_ref") or {})
        if not binding_ref:
            return {}
        binding = dict(self.artifacts.read_json(binding_ref))
        return dict(dict(binding.get("policies") or {}).get(name) or {})

    def architecture_worker_suppressed(
        self,
        revision: AggregateSnapshot,
        *,
        running_state: str,
        start_action: str,
    ) -> bool:
        workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, revision.workflow_id)
        if workflow is None:
            return True
        if workflow.state in {"PAUSE_REQUESTED", "PAUSED", "CANCEL_REQUESTED", "CANCELLED"}:
            return True
        legal = self.repository.transitions.legal_actions(AggregateType.ARCHITECTURE_REVISION, revision.state)
        return revision.state != running_state and start_action not in legal
