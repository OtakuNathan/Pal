from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Protocol
import tomllib

from pal.bunshin.catalog_store import family_override_root, load_json_objects
from pal.bunshin.utils import dict_from as _dict
from pal.bunshin.utils import string_list as _string_list


@dataclass(frozen=True)
class BunshinRoleBinding:
    participant: str
    profile: str = ""
    reason: str = ""

    @classmethod
    def from_payload(
        cls, role: str, payload: Any
    ) -> "BunshinRoleBinding":
        if not isinstance(payload, dict):
            raise ValueError(
                f"family role binding {role} must be a table"
            )
        participant = str(payload.get("participant") or "").strip()
        profile = str(payload.get("profile") or "").strip().replace("/", ".")
        reason = str(payload.get("reason") or "").strip()
        if participant not in {"profile", "null"}:
            raise ValueError(
                f"family role binding {role}.participant must be profile or null"
            )
        if participant == "profile" and not profile:
            raise ValueError(
                f"family role binding {role} requires profile"
            )
        if participant == "null" and (profile or not reason):
            raise ValueError(
                f"family null role binding {role} requires reason and no profile"
            )
        return cls(participant=participant, profile=profile, reason=reason)

    def to_dict(self) -> dict[str, str]:
        return {
            "participant": self.participant,
            **({"profile": self.profile} if self.profile else {}),
            **({"reason": self.reason} if self.reason else {}),
        }


@dataclass(frozen=True)
class BunshinCapabilityGroup:
    group_id: str
    capabilities: tuple[str, ...] = ()
    include: tuple[str, ...] = ()
    description: str = ""

    @classmethod
    def from_payload(cls, group_id: str, payload: Any) -> "BunshinCapabilityGroup":
        if isinstance(payload, list):
            return cls(group_id=group_id, capabilities=tuple(_string_list(payload)))
        if not isinstance(payload, dict):
            return cls(group_id=group_id)
        return cls(
            group_id=group_id,
            capabilities=tuple(_string_list(payload.get("capabilities") or payload.get("tools"))),
            include=tuple(_string_list(payload.get("include") or payload.get("includes"))),
            description=str(payload.get("description") or "").strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "description": self.description,
            "include": list(self.include),
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True)
class BunshinArchitectureBinding:
    specialization: str

    @classmethod
    def from_payload(cls, payload: Any) -> "BunshinArchitectureBinding":
        if not isinstance(payload, dict):
            raise ValueError("family architecture must be a table")
        specialization = str(payload.get("specialization") or "").strip()
        if not specialization:
            raise ValueError("family architecture.specialization is required")
        return cls(specialization=specialization)

    def to_dict(self) -> dict[str, str]:
        return {"specialization": self.specialization}


@dataclass(frozen=True)
class BunshinFamilyManifest:
    family_id: str
    display_name: str = ""
    domain: str = ""
    domain_keywords: tuple[str, ...] = ()
    workflow_template: str = "contract_dag.v2"
    architecture: BunshinArchitectureBinding | None = None
    role_bindings: dict[str, BunshinRoleBinding] = field(default_factory=dict)
    execution_adapter: str = ""
    policies: dict[str, Any] = field(default_factory=dict)
    capability_groups: dict[str, BunshinCapabilityGroup] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BunshinFamilyManifest":
        if not isinstance(payload, dict):
            raise ValueError("BunshinFamilyManifest payload must be an object")
        family_id = str(payload.get("family_id") or "").strip()
        if not family_id:
            raise ValueError("BunshinFamilyManifest.family_id is required")
        raw_groups = _dict(payload.get("capability_groups"))
        groups = {
            str(group_id).strip(): BunshinCapabilityGroup.from_payload(str(group_id).strip(), group_payload)
            for group_id, group_payload in raw_groups.items()
            if str(group_id).strip()
        }
        return cls(
            family_id=_normalize_family_id(family_id),
            display_name=str(payload.get("display_name") or family_id).strip(),
            domain=str(payload.get("domain") or "").strip(),
            domain_keywords=tuple(_string_list(payload.get("domain_keywords") or payload.get("keywords"))),
            workflow_template=str(payload.get("workflow_template") or "contract_dag.v2").strip(),
            architecture=BunshinArchitectureBinding.from_payload(
                payload.get("architecture")
            ),
            role_bindings={
                str(key): BunshinRoleBinding.from_payload(str(key), value)
                for key, value in _dict(payload.get("role_bindings")).items()
            },
            execution_adapter=str(
                payload.get("execution_adapter") or ""
            ).strip(),
            policies=_dict(payload.get("policies")),
            capability_groups=groups,
            metadata=_dict(payload.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "family_id": self.family_id,
            "display_name": self.display_name,
            "domain": self.domain,
            "domain_keywords": list(self.domain_keywords),
            "workflow_template": self.workflow_template,
            "architecture": (
                self.architecture.to_dict()
                if self.architecture is not None
                else {}
            ),
            "role_bindings": {
                key: value.to_dict()
                for key, value in self.role_bindings.items()
            },
            "execution_adapter": self.execution_adapter,
            "policies": dict(self.policies),
            "capability_groups": {key: value.to_dict() for key, value in self.capability_groups.items()},
            "metadata": dict(self.metadata),
        }


class BunshinFamilyProvider(Protocol):
    def declared_bunshin_families(self) -> list[BunshinFamilyManifest | dict[str, Any]]:
        ...


@dataclass
class BunshinFamilyRegistry:
    family_providers: tuple[BunshinFamilyProvider, ...] = ()
    runtime_root: Path | None = None
    builtin_families: tuple[BunshinFamilyManifest, ...] = field(default_factory=lambda: load_builtin_bunshin_families())

    def list_families(self) -> list[BunshinFamilyManifest]:
        families: dict[str, BunshinFamilyManifest] = {}
        for family in self.builtin_families:
            families[family.family_id] = family
        for family in self._runtime_families():
            families[family.family_id] = family
        for provider in self.family_providers:
            declare = getattr(provider, "declared_bunshin_families", None)
            if not callable(declare):
                continue
            for item in list(declare() or []):
                family = item if isinstance(item, BunshinFamilyManifest) else BunshinFamilyManifest.from_dict(dict(item or {}))
                families[family.family_id] = family
        return [families[key] for key in sorted(families)]

    def get(self, family_id: str) -> BunshinFamilyManifest | None:
        normalized = _normalize_family_id(family_id or "general")
        for family in self.list_families():
            if family.family_id == normalized:
                return family
        return None

    def capability_group(self, family_id: str, group_id: str) -> BunshinCapabilityGroup | None:
        raw = str(group_id or "").strip()
        if not raw:
            return None
        if "." in raw:
            explicit_family, explicit_group = raw.rsplit(".", 1)
            family = self.get(explicit_family)
            return None if family is None else family.capability_groups.get(explicit_group)
        family = self.get(family_id)
        return None if family is None else family.capability_groups.get(raw)

    def _runtime_families(self) -> list[BunshinFamilyManifest]:
        if self.runtime_root is None:
            return []
        return _load_family_overrides(Path(self.runtime_root))


def load_builtin_bunshin_families() -> tuple[BunshinFamilyManifest, ...]:
    root = resources.files("pal.bunshin").joinpath("family_templates")
    families: list[BunshinFamilyManifest] = []
    if not root.is_dir():
        return ()
    for item in sorted(root.iterdir(), key=lambda path: path.name):
        if not item.name.endswith(".toml"):
            continue
        payload = tomllib.loads(item.read_text(encoding="utf-8"))
        families.append(BunshinFamilyManifest.from_dict(payload))
    return tuple(families)


def _load_family_overrides(runtime_root: Path) -> list[BunshinFamilyManifest]:
    families: list[BunshinFamilyManifest] = []
    for path, payload in load_json_objects(family_override_root(runtime_root)):
        family = BunshinFamilyManifest.from_dict(payload)
        metadata = dict(family.metadata)
        metadata["source_path"] = str(path)
        metadata["catalog_source"] = "override"
        families.append(BunshinFamilyManifest.from_dict({**family.to_dict(), "metadata": metadata}))
    return families


def _normalize_family_id(value: str) -> str:
    return str(value or "general").strip().replace("/", ".") or "general"
