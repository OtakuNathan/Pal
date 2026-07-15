from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pal.minion.families import MinionFamilyManifest
from pal.minion.profiles import MinionProfileRegistry
from pal.minion.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore


CONTRACT_DAG_ROLES = frozenset(
    {
        "architect",
        "architecture_reviewer",
        "producer",
        "repair",
        "verifier",
    }
)

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
class ResolvedFamilyBinding:
    family_id: str
    workflow_template: str
    roles: Mapping[str, str]
    builders: Mapping[str, str]
    adapters: Mapping[str, str]
    policies: Mapping[str, Any]
    manifest: Mapping[str, Any]
    profile_hashes: Mapping[str, str]
    profile_definitions: Mapping[str, Mapping[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "2",
            "family_id": self.family_id,
            "workflow_template": self.workflow_template,
            "roles": dict(self.roles),
            "builders": dict(self.builders),
            "adapters": dict(self.adapters),
            "policies": dict(self.policies),
            "manifest": dict(self.manifest),
            "profile_hashes": dict(self.profile_hashes),
            "profile_definitions": {
                str(role): dict(definition)
                for role, definition in self.profile_definitions.items()
            },
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

    def publish_family_binding(self, family_id: str) -> ArtifactRef:
        family = self.family(family_id)
        profile_registry = MinionProfileRegistry(runtime_root=Path(self.runtime_root))
        roles = {str(key): str(value) for key, value in family.roles.items() if str(value)}
        if family.workflow_template == "contract_dag.v2":
            missing_roles = sorted(CONTRACT_DAG_ROLES - set(roles))
            if missing_roles:
                raise ValueError(
                    f"family {family.family_id} is missing contract_dag roles: {', '.join(missing_roles)}"
                )
        profile_hashes: dict[str, str] = {}
        profile_definitions: dict[str, dict[str, Any]] = {}
        for role, profile_id in roles.items():
            profile = profile_registry.get(profile_id)
            if profile is None:
                raise ValueError(f"family {family.family_id} role {role} references unknown profile {profile_id}")
            definition = profile.to_dict()
            profile_hashes[role] = _stable_hash(definition)
            profile_definitions[role] = definition
        unknown_builders = sorted(set(family.builders.values()) - REGISTERED_BUILDERS)
        if unknown_builders:
            raise ValueError(f"family {family.family_id} references unknown builders: {', '.join(unknown_builders)}")
        unknown_adapters = sorted(set(family.adapters.values()) - REGISTERED_ADAPTERS)
        if unknown_adapters:
            raise ValueError(f"family {family.family_id} references unknown adapters: {', '.join(unknown_adapters)}")
        binding = ResolvedFamilyBinding(
            family_id=family.family_id,
            workflow_template=family.workflow_template,
            roles=roles,
            builders=dict(family.builders),
            adapters=dict(family.adapters),
            policies=dict(family.policies),
            manifest=family.to_dict(),
            profile_hashes=profile_hashes,
            profile_definitions=profile_definitions,
        )
        return self.artifacts.put_json(
            binding.to_dict(),
            artifact_type="FamilyBindingArtifact",
            provenance={"family_id": family.family_id},
        )


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
