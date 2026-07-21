from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pal.minion.families import MinionFamilyManifest
from pal.minion.profiles import MinionProfile, MinionProfileRegistry
from pal.minion.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.minion.v2.role_contracts import (
    REQUIRED_ORCHESTRATION_ROLES,
    TASK_PROFILE_BINDING,
    validate_role_bindings,
)


CONTRACT_DAG_ROLES = REQUIRED_ORCHESTRATION_ROLES

REGISTERED_BUILDERS = frozenset(
    {
        "requirements.v2",
        "contract_sketch.v2",
        "skeleton_git.v2",
        "verification.v2",
    }
)

REGISTERED_ADAPTERS = frozenset({"software_git.v2", "artifact_bundle.v2"})


@dataclass(frozen=True)
class ResolvedRoleBinding:
    selector: str
    executor_profile: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "selector": self.selector,
            "executor_profile": dict(self.executor_profile),
        }


@dataclass(frozen=True)
class ResolvedFamilyBinding:
    family_id: str
    display_name: str
    domain: str
    domain_keywords: tuple[str, ...]
    workflow_template: str
    primary_profile: Mapping[str, Any]
    role_bindings: Mapping[str, ResolvedRoleBinding]
    builders: Mapping[str, str]
    adapters: Mapping[str, str]
    policies: Mapping[str, Any]
    capability_groups: Mapping[str, Mapping[str, Any]]
    metadata: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "3",
            "family_id": self.family_id,
            "display_name": self.display_name,
            "domain": self.domain,
            "domain_keywords": list(self.domain_keywords),
            "workflow_template": self.workflow_template,
            "primary_profile": dict(self.primary_profile),
            "role_bindings": {
                role: binding.to_dict()
                for role, binding in sorted(self.role_bindings.items())
            },
            "builders": dict(self.builders),
            "adapters": dict(self.adapters),
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
        for role, selector in selectors.items():
            executor = primary_profile if selector == TASK_PROFILE_BINDING else profile_registry.get(selector)
            if executor is None:
                raise ValueError(
                    f"family {family.family_id} role {role} references unknown profile {selector}"
                )
            if executor.profile_group.replace("/", ".") != family.family_id:
                raise ValueError(
                    f"family {family.family_id} role {role} references cross-family profile "
                    f"{executor.canonical_profile_id}"
                )
            resolved_roles[role] = ResolvedRoleBinding(
                selector=selector,
                executor_profile=executor.to_dict(),
            )
        unknown_builders = sorted(set(family.builders.values()) - REGISTERED_BUILDERS)
        if unknown_builders:
            raise ValueError(f"family {family.family_id} references unknown builders: {', '.join(unknown_builders)}")
        unknown_adapters = sorted(set(family.adapters.values()) - REGISTERED_ADAPTERS)
        if unknown_adapters:
            raise ValueError(f"family {family.family_id} references unknown adapters: {', '.join(unknown_adapters)}")
        binding = ResolvedFamilyBinding(
            family_id=family.family_id,
            display_name=family.display_name,
            domain=family.domain,
            domain_keywords=family.domain_keywords,
            workflow_template=family.workflow_template,
            primary_profile=primary_profile.to_dict(),
            role_bindings=resolved_roles,
            builders=dict(family.builders),
            adapters=dict(family.adapters),
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
            schema_version="3",
            provenance={
                "family_id": family.family_id,
                "primary_profile": primary_profile.canonical_profile_id,
            },
        )
