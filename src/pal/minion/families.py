from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Protocol
import tomllib

from pal.minion.utils import dict_from as _dict
from pal.minion.utils import string_list as _string_list


@dataclass(frozen=True)
class MinionCapabilityGroup:
    group_id: str
    capabilities: tuple[str, ...] = ()
    include: tuple[str, ...] = ()
    description: str = ""

    @classmethod
    def from_payload(cls, group_id: str, payload: Any) -> "MinionCapabilityGroup":
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
class MinionFamilyManifest:
    family_id: str
    display_name: str = ""
    domain: str = ""
    domain_keywords: tuple[str, ...] = ()
    workflow_template: str = "contract_dag.v2"
    roles: dict[str, str] = field(default_factory=dict)
    builders: dict[str, str] = field(default_factory=dict)
    adapters: dict[str, str] = field(default_factory=dict)
    policies: dict[str, Any] = field(default_factory=dict)
    capability_groups: dict[str, MinionCapabilityGroup] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MinionFamilyManifest":
        if not isinstance(payload, dict):
            raise ValueError("MinionFamilyManifest payload must be an object")
        family_id = str(payload.get("family_id") or payload.get("profile_family") or payload.get("id") or "").strip()
        if not family_id:
            raise ValueError("MinionFamilyManifest.family_id is required")
        raw_groups = _dict(payload.get("capability_groups"))
        groups = {
            str(group_id).strip(): MinionCapabilityGroup.from_payload(str(group_id).strip(), group_payload)
            for group_id, group_payload in raw_groups.items()
            if str(group_id).strip()
        }
        return cls(
            family_id=_normalize_family_id(family_id),
            display_name=str(payload.get("display_name") or family_id).strip(),
            domain=str(payload.get("domain") or "").strip(),
            domain_keywords=tuple(_string_list(payload.get("domain_keywords") or payload.get("keywords"))),
            workflow_template=str(payload.get("workflow_template") or "contract_dag.v2").strip(),
            roles={str(key): str(value).strip().replace("/", ".") for key, value in _dict(payload.get("roles")).items()},
            builders={str(key): str(value).strip() for key, value in _dict(payload.get("builders")).items()},
            adapters={str(key): str(value).strip() for key, value in _dict(payload.get("adapters")).items()},
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
            "roles": dict(self.roles),
            "builders": dict(self.builders),
            "adapters": dict(self.adapters),
            "policies": dict(self.policies),
            "capability_groups": {key: value.to_dict() for key, value in self.capability_groups.items()},
            "metadata": dict(self.metadata),
        }


class MinionFamilyProvider(Protocol):
    def declared_minion_families(self) -> list[MinionFamilyManifest | dict[str, Any]]:
        ...


@dataclass
class MinionFamilyRegistry:
    family_providers: tuple[MinionFamilyProvider, ...] = ()
    runtime_root: Path | None = None
    builtin_families: tuple[MinionFamilyManifest, ...] = field(default_factory=lambda: BUILTIN_MINION_FAMILIES)

    def list_families(self) -> list[MinionFamilyManifest]:
        families: dict[str, MinionFamilyManifest] = {}
        for family in self.builtin_families:
            families[family.family_id] = family
        for family in self._runtime_families():
            families[family.family_id] = family
        for provider in self.family_providers:
            declare = getattr(provider, "declared_minion_families", None)
            if not callable(declare):
                continue
            for item in list(declare() or []):
                family = item if isinstance(item, MinionFamilyManifest) else MinionFamilyManifest.from_dict(dict(item or {}))
                families[family.family_id] = family
        return [families[key] for key in sorted(families)]

    def get(self, family_id: str) -> MinionFamilyManifest | None:
        normalized = _normalize_family_id(family_id or "general")
        for family in self.list_families():
            if family.family_id == normalized:
                return family
        return None

    def capability_group(self, family_id: str, group_id: str) -> MinionCapabilityGroup | None:
        raw = str(group_id or "").strip()
        if not raw:
            return None
        if "." in raw:
            explicit_family, explicit_group = raw.rsplit(".", 1)
            family = self.get(explicit_family)
            return None if family is None else family.capability_groups.get(explicit_group)
        family = self.get(family_id)
        return None if family is None else family.capability_groups.get(raw)

    def _runtime_families(self) -> list[MinionFamilyManifest]:
        if self.runtime_root is None:
            return []
        return _load_families_from_dir(Path(self.runtime_root) / "plugins" / "minion" / "families")


def load_builtin_minion_families() -> tuple[MinionFamilyManifest, ...]:
    root = resources.files("pal.minion").joinpath("family_templates")
    families: list[MinionFamilyManifest] = []
    if not root.is_dir():
        return ()
    for item in sorted(root.iterdir(), key=lambda path: path.name):
        if not item.name.endswith(".toml"):
            continue
        payload = tomllib.loads(item.read_text(encoding="utf-8"))
        families.append(MinionFamilyManifest.from_dict(payload))
    return tuple(families)


def _load_families_from_dir(family_dir: Path) -> list[MinionFamilyManifest]:
    if not family_dir.exists():
        return []
    families: list[MinionFamilyManifest] = []
    for path in sorted(family_dir.glob("*.toml")):
        try:
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
            family = MinionFamilyManifest.from_dict(payload)
        except Exception:
            continue
        metadata = dict(family.metadata)
        metadata.setdefault("source_path", str(path))
        families.append(MinionFamilyManifest.from_dict({**family.to_dict(), "metadata": metadata}))
    return families


def _normalize_family_id(value: str) -> str:
    return str(value or "general").strip().replace("/", ".") or "general"


BUILTIN_MINION_FAMILIES = load_builtin_minion_families()
