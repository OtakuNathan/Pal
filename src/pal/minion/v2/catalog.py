from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pal.minion.families import MinionFamilyManifest
from pal.minion.profiles import MinionProfile, MinionProfileRegistry
from pal.minion.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.minion.v2.architecture_templates import ArchitectureTemplateCompiler
from pal.minion.v2.role_contracts import (
    REQUIRED_ORCHESTRATION_ROLES,
    TASK_PROFILE_BINDING,
    family_execution_adapter,
    validate_role_bindings,
)


CONTRACT_DAG_ROLES = REQUIRED_ORCHESTRATION_ROLES

REGISTERED_ADAPTERS = frozenset({"software_git.v2", "artifact_bundle.v2"})


@dataclass(frozen=True)
class ResolvedRoleBinding:
    participant: str
    selector: str
    role_profile: Mapping[str, Any] | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "participant": self.participant,
            "selector": self.selector,
            **(
                {"role_profile": dict(self.role_profile)}
                if self.role_profile is not None
                else {}
            ),
            **({"reason": self.reason} if self.reason else {}),
        }


@dataclass(frozen=True)
class ResolvedFamilyBinding:
    family_id: str
    display_name: str
    domain: str
    domain_keywords: tuple[str, ...]
    workflow_template: str
    primary_profile: Mapping[str, Any]
    architecture_definition: Mapping[str, Any]
    role_bindings: Mapping[str, ResolvedRoleBinding]
    execution_adapter: str
    policies: Mapping[str, Any]
    capability_groups: Mapping[str, Mapping[str, Any]]
    metadata: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "6",
            "family_id": self.family_id,
            "display_name": self.display_name,
            "domain": self.domain,
            "domain_keywords": list(self.domain_keywords),
            "workflow_template": self.workflow_template,
            "primary_profile": dict(self.primary_profile),
            "architecture_definition": dict(self.architecture_definition),
            "role_bindings": {
                role: binding.to_dict()
                for role, binding in sorted(self.role_bindings.items())
            },
            "execution_adapter": self.execution_adapter,
            "policies": dict(self.policies),
            "capability_groups": {
                group_id: dict(group)
                for group_id, group in sorted(self.capability_groups.items())
            },
            "metadata": dict(self.metadata),
        }


@dataclass
class MinionV2Catalog:
    runtime_root: Path
    artifacts: ContentAddressedArtifactStore

    def family(self, family_id: str) -> MinionFamilyManifest:
        registry = MinionProfileRegistry(runtime_root=Path(self.runtime_root))
        family = registry.family_registry().get(str(family_id or ""))
        if family is None:
            raise ValueError(f"unknown minion family: {family_id or '<empty>'}")
        return family

    def validate_family_exists(self, family_id: str) -> None:
        self.family(family_id)

    def profile(self, profile_id: str) -> MinionProfile:
        if not str(profile_id or "").strip():
            raise ValueError("task requires an explicit primary minion profile")
        profile_registry = MinionProfileRegistry(runtime_root=Path(self.runtime_root))
        profile = profile_registry.get(str(profile_id or "").strip())
        if profile is None:
            raise ValueError(f"unknown minion profile: {profile_id or '<empty>'}")
        return profile

    def family_for_profile(self, profile_id: str) -> MinionFamilyManifest:
        profile = self.profile(profile_id)
        return self.family(profile.profile_group)

    def publish_family_binding(self, primary_profile_id: str) -> ArtifactRef:
        profile_registry = MinionProfileRegistry(runtime_root=Path(self.runtime_root))
        primary_profile = self.profile(primary_profile_id)
        family = self.family(primary_profile.profile_group)
        selectors = validate_role_bindings(family.role_bindings)
        resolved_roles: dict[str, ResolvedRoleBinding] = {}
        for role, role_binding in selectors.items():
            if role_binding.participant == "null":
                resolved_roles[role] = ResolvedRoleBinding(
                    participant="null",
                    selector="",
                    reason=role_binding.reason,
                )
                continue
            selector = role_binding.profile
            executor = (
                primary_profile
                if selector == TASK_PROFILE_BINDING
                else profile_registry.get(selector)
            )
            if executor is None:
                raise ValueError(
                    f"family {family.family_id} role {role} references unknown profile {selector}"
                )
            if executor.profile_group.replace("/", ".") != family.family_id:
                raise ValueError(
                    f"family {family.family_id} role {role} references cross-family profile "
                    f"{executor.canonical_profile_id}"
                )
            if executor.role_protocol is None:
                raise ValueError(
                    f"family {family.family_id} role {role} profile "
                    f"{executor.canonical_profile_id} has no role protocol"
                )
            if executor.role_protocol.kind != role:
                raise ValueError(
                    f"family {family.family_id} role {role} references profile "
                    f"{executor.canonical_profile_id} for role "
                    f"{executor.role_protocol.kind}"
                )
            resolved_roles[role] = ResolvedRoleBinding(
                participant="profile",
                selector=selector,
                role_profile=executor.to_dict(),
            )
        architecture_binding = family.architecture
        if architecture_binding is None:
            raise ValueError(
                f"family {family.family_id} has no architecture specialization"
            )
        architecture_definition = ArchitectureTemplateCompiler(
            providers=tuple(profile_registry.family_providers)
        ).compile(
            architecture_binding.specialization
        )
        if architecture_definition.family_id != family.family_id:
            raise ValueError(
                f"family {family.family_id} cannot use architecture "
                f"specialization {architecture_definition.specialization_id} "
                f"owned by {architecture_definition.family_id}"
            )
        schema_ref = self.artifacts.put_json(
            dict(architecture_definition.schema),
            artifact_type="ArchitectureJsonSchemaArtifact",
            schema_version="1",
            provenance={
                "specialization_id": (
                    architecture_definition.specialization_id
                ),
                "generation_hash": architecture_definition.generation_hash,
            },
        )
        template_ref = self.artifacts.put_json(
            {
                "specialization_id": (
                    architecture_definition.specialization_id
                ),
                "generation_hash": architecture_definition.generation_hash,
                "template": architecture_definition.template,
            },
            artifact_type="ArchitectTemplateArtifact",
            schema_version="1",
            provenance={
                "specialization_id": (
                    architecture_definition.specialization_id
                ),
                "generation_hash": architecture_definition.generation_hash,
            },
        )
        execution_adapter = family_execution_adapter(
            family.execution_adapter
        )
        if execution_adapter not in REGISTERED_ADAPTERS:
            raise ValueError(
                f"family {family.family_id} references unknown execution "
                f"adapter: {execution_adapter}"
            )
        binding = ResolvedFamilyBinding(
            family_id=family.family_id,
            display_name=family.display_name,
            domain=family.domain,
            domain_keywords=family.domain_keywords,
            workflow_template=family.workflow_template,
            primary_profile=primary_profile.to_dict(),
            architecture_definition={
                "specialization_id": (
                    architecture_definition.specialization_id
                ),
                "family_id": architecture_definition.family_id,
                "generation_hash": architecture_definition.generation_hash,
                "schema_ref": schema_ref.to_dict(),
                "template_ref": template_ref.to_dict(),
            },
            role_bindings=resolved_roles,
            execution_adapter=execution_adapter,
            policies=dict(family.policies),
            capability_groups={
                group_id: group.to_dict()
                for group_id, group in family.capability_groups.items()
            },
            metadata=dict(family.metadata),
        )
        return self.artifacts.put_json(
            binding.to_dict(),
            artifact_type="FamilyBindingArtifact",
            schema_version="6",
            provenance={
                "family_id": family.family_id,
                "primary_profile": primary_profile.canonical_profile_id,
            },
            child_refs=(
                (schema_ref.sha256, "architecture_schema"),
                (template_ref.sha256, "architect_template"),
            ),
        )
