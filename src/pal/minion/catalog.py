from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from pal.minion.catalog_store import (
    atomic_write_json,
    family_override_path,
    family_override_root,
    load_json_objects,
    minion_catalog_root,
    profile_override_path,
    profile_override_root,
    remove_override,
)
from pal.minion.families import MinionFamilyManifest, MinionFamilyRegistry
from pal.minion.profiles import (
    CAPABILITY_GROUPS,
    MinionProfile,
    MinionProfileRegistry,
    canonical_profile_id,
)
from pal.minion.v2.role_contracts import TASK_PROFILE_BINDING, validate_role_bindings


CATALOG_SCHEMA_VERSION = "1"
_PROFILE_PATCH_FIELDS = frozenset(
    {
        "display_name",
        "identity_fragment",
        "behavior_fragment",
        "output_contract_fragment",
        "preferred_endpoint_id",
        "capability_groups",
        "default_allowed_capabilities",
        "skill_refs",
        "default_approval_policy",
        "workspace_policy",
        "workspace_environment_policy",
        "completion_policy",
        "capability_policy",
        "capability_guidance_overrides",
        "output_policy",
        "metadata",
    }
)
_FAMILY_PATCH_FIELDS = frozenset(
    {
        "display_name",
        "domain",
        "domain_keywords",
        "workflow_template",
        "role_bindings",
        "builders",
        "adapters",
        "policies",
        "capability_groups",
        "metadata",
    }
)
@dataclass
class MinionCatalogService:
    """Sidecar-owned profile/family catalog and override lifecycle."""

    runtime_root: Path
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _last_migration: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.runtime_root = Path(self.runtime_root)

    def bootstrap(self) -> dict[str, Any]:
        with self._lock:
            root = minion_catalog_root(self.runtime_root)
            profile_override_root(self.runtime_root).mkdir(parents=True, exist_ok=True)
            family_override_root(self.runtime_root).mkdir(parents=True, exist_ok=True)
            migration = self._migrate_legacy_runtime_catalog()
            self._last_migration = migration
            self._validate_effective_catalog()
            snapshot = self._snapshot(include_definitions=False)
            self._append_audit(
                {
                    "action": "catalog_bootstrap",
                    "actor": "minion_sidecar",
                    "generation": snapshot["generation"],
                    "migration": migration,
                }
            )
            return {**snapshot, "catalog_root": str(root), "migration": migration}

    def snapshot(
        self,
        *,
        kind: str = "all",
        query: str = "",
        include_definitions: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            payload = self._snapshot(include_definitions=include_definitions)
            normalized_kind = str(kind or "all").strip().lower()
            if normalized_kind not in {"all", "profiles", "families"}:
                raise ValueError("catalog kind must be all, profiles, or families")
            needle = str(query or "").strip().casefold()
            if normalized_kind == "profiles":
                payload.pop("families", None)
            elif normalized_kind == "families":
                payload.pop("profiles", None)
            if needle:
                for key in ("profiles", "families"):
                    if key in payload:
                        payload[key] = [
                            item
                            for item in payload[key]
                            if needle in json.dumps(item, ensure_ascii=False, sort_keys=True).casefold()
                        ]
            payload["migration"] = dict(self._last_migration)
            return payload

    def refresh(self, *, actor: str = "pal") -> dict[str, Any]:
        with self._lock:
            migration = self._migrate_legacy_runtime_catalog()
            self._last_migration = migration
            self._validate_effective_catalog()
            payload = self._snapshot(include_definitions=False)
            self._append_audit(
                {
                    "action": "catalog_refresh",
                    "actor": actor,
                    "generation": payload["generation"],
                    "migration": migration,
                }
            )
            return {**payload, "migration": migration}

    def set_profile_override(
        self,
        *,
        profile: str,
        changes: Mapping[str, Any],
        actor: str = "pal",
        if_generation: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            before = self._snapshot(include_definitions=False)
            self._check_generation(if_generation, before["generation"])
            self._validate_patch_fields(changes, allowed=_PROFILE_PATCH_FIELDS, kind="profile")
            group, name = self._resolve_profile_identity(profile)
            registry = MinionProfileRegistry(runtime_root=self.runtime_root)
            current = registry.get_ref(group, name)
            base = self._profile_payload(current) if current is not None else {
                "profile_id": name,
                "profile_group": group,
                "display_name": name,
            }
            patched = _merge_patch(base, dict(changes or {}))
            if str(patched.get("profile_id") or name) != name:
                raise ValueError("profile_id cannot be changed by an override")
            if str(patched.get("profile_group") or group).replace("/", ".") != group:
                raise ValueError("profile_group cannot be changed by an override")
            patched["profile_id"] = name
            patched["profile_group"] = group
            metadata = dict(patched.get("metadata") or {})
            metadata.pop("source_path", None)
            metadata["builtin"] = False
            metadata["managed_by"] = "minion_sidecar"
            if MinionProfileRegistry(runtime_root=None).get_ref(group, name) is not None:
                metadata["override_of"] = canonical_profile_id(group, name)
            else:
                metadata.pop("override_of", None)
            patched["metadata"] = metadata
            model = MinionProfile.from_dict(patched)
            self._validate_profile(model)
            path = profile_override_path(self.runtime_root, group, name)
            atomic_write_json(path, self._profile_payload(model))
            after = self._snapshot(include_definitions=False)
            self._append_audit(
                {
                    "action": "profile_override_set",
                    "actor": actor,
                    "target": model.canonical_profile_id,
                    "before_generation": before["generation"],
                    "generation": after["generation"],
                    "changed_fields": sorted(str(key) for key in dict(changes or {})),
                }
            )
            effective = MinionProfileRegistry(runtime_root=self.runtime_root).get_ref(group, name)
            return {
                "status": "updated",
                "profile": model.canonical_profile_id,
                "generation": after["generation"],
                "definition": self._profile_payload(effective),
            }

    def reset_profile_override(
        self,
        *,
        profile: str,
        actor: str = "pal",
        if_generation: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            before = self._snapshot(include_definitions=False)
            self._check_generation(if_generation, before["generation"])
            group, name = self._resolve_profile_identity(profile)
            removed = remove_override(profile_override_path(self.runtime_root, group, name))
            after = self._snapshot(include_definitions=False)
            self._append_audit(
                {
                    "action": "profile_override_reset",
                    "actor": actor,
                    "target": canonical_profile_id(group, name),
                    "removed": removed,
                    "before_generation": before["generation"],
                    "generation": after["generation"],
                }
            )
            return {
                "status": "reset" if removed else "unchanged",
                "profile": canonical_profile_id(group, name),
                "generation": after["generation"],
            }

    def set_family_override(
        self,
        *,
        family: str,
        changes: Mapping[str, Any],
        actor: str = "pal",
        if_generation: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            before = self._snapshot(include_definitions=False)
            self._check_generation(if_generation, before["generation"])
            self._validate_patch_fields(changes, allowed=_FAMILY_PATCH_FIELDS, kind="family")
            family_id = _normalize_family_name(family)
            current = MinionFamilyRegistry(runtime_root=self.runtime_root).get(family_id)
            base = self._family_payload(current) if current is not None else {
                "family_id": family_id,
                "display_name": family_id,
                "workflow_template": "contract_dag.v2",
            }
            patched = _merge_patch(base, dict(changes or {}))
            if _normalize_family_name(str(patched.get("family_id") or family_id)) != family_id:
                raise ValueError("family_id cannot be changed by an override")
            patched["family_id"] = family_id
            metadata = dict(patched.get("metadata") or {})
            metadata.pop("source_path", None)
            metadata["builtin"] = False
            metadata["managed_by"] = "minion_sidecar"
            if MinionFamilyRegistry(runtime_root=None).get(family_id) is not None:
                metadata["override_of"] = family_id
            else:
                metadata.pop("override_of", None)
            patched["metadata"] = metadata
            model = MinionFamilyManifest.from_dict(patched)
            self._validate_family(model)
            atomic_write_json(family_override_path(self.runtime_root, family_id), self._family_payload(model))
            after = self._snapshot(include_definitions=False)
            self._append_audit(
                {
                    "action": "family_override_set",
                    "actor": actor,
                    "target": family_id,
                    "before_generation": before["generation"],
                    "generation": after["generation"],
                    "changed_fields": sorted(str(key) for key in dict(changes or {})),
                }
            )
            effective = MinionFamilyRegistry(runtime_root=self.runtime_root).get(family_id)
            return {
                "status": "updated",
                "family": family_id,
                "generation": after["generation"],
                "definition": self._family_payload(effective),
            }

    def reset_family_override(
        self,
        *,
        family: str,
        actor: str = "pal",
        if_generation: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            before = self._snapshot(include_definitions=False)
            self._check_generation(if_generation, before["generation"])
            family_id = _normalize_family_name(family)
            removed = remove_override(family_override_path(self.runtime_root, family_id))
            after = self._snapshot(include_definitions=False)
            self._append_audit(
                {
                    "action": "family_override_reset",
                    "actor": actor,
                    "target": family_id,
                    "removed": removed,
                    "before_generation": before["generation"],
                    "generation": after["generation"],
                }
            )
            return {
                "status": "reset" if removed else "unchanged",
                "family": family_id,
                "generation": after["generation"],
            }

    def _snapshot(self, *, include_definitions: bool) -> dict[str, Any]:
        profile_overrides = {
            (str(payload.get("profile_group") or "general").replace("/", "."), str(payload.get("profile_id") or "")): payload
            for _path, payload in load_json_objects(profile_override_root(self.runtime_root))
        }
        family_overrides = {
            _normalize_family_name(str(payload.get("family_id") or "")): payload
            for _path, payload in load_json_objects(family_override_root(self.runtime_root))
        }
        profiles: list[dict[str, Any]] = []
        profile_generation: list[dict[str, Any]] = []
        for profile in MinionProfileRegistry(runtime_root=self.runtime_root).list_profiles():
            key = (profile.profile_group.replace("/", "."), profile.profile_id)
            definition = self._profile_payload(profile)
            item: dict[str, Any] = {
                "name": profile.canonical_profile_id,
                "display_name": profile.display_name,
                "source": "override" if key in profile_overrides else "builtin",
                "capability_groups": list(profile.capability_groups),
                "preferred_endpoint_id": profile.preferred_endpoint_id,
            }
            if include_definitions:
                item["definition"] = definition
            profiles.append(item)
            profile_generation.append({**item, "definition": definition})
        families: list[dict[str, Any]] = []
        family_generation: list[dict[str, Any]] = []
        for family in MinionFamilyRegistry(runtime_root=self.runtime_root).list_families():
            definition = self._family_payload(family)
            item = {
                "name": family.family_id,
                "display_name": family.display_name,
                "source": "override" if family.family_id in family_overrides else "builtin",
                "workflow_template": family.workflow_template,
                "role_bindings": dict(family.role_bindings),
            }
            if include_definitions:
                item["definition"] = definition
            families.append(item)
            family_generation.append({**item, "definition": definition})
        generation_payload = {"profiles": profile_generation, "families": family_generation}
        generation = hashlib.sha256(
            json.dumps(generation_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "generation": generation,
            "profiles": profiles,
            "families": families,
            "profile_count": len(profiles),
            "family_count": len(families),
        }

    def _migrate_legacy_runtime_catalog(self) -> dict[str, Any]:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        archive_root = minion_catalog_root(self.runtime_root) / "legacy_seeds" / stamp
        migrated_profiles = self._migrate_legacy_tree(
            source_root=self.runtime_root / "plugins" / "minion" / "profiles",
            archive_root=archive_root / "profiles",
            kind="profile",
        )
        migrated_families = self._migrate_legacy_tree(
            source_root=self.runtime_root / "plugins" / "minion" / "families",
            archive_root=archive_root / "families",
            kind="family",
        )
        if not migrated_profiles and not migrated_families:
            shutil.rmtree(archive_root, ignore_errors=True)
        return {
            "profiles": migrated_profiles,
            "families": migrated_families,
            "archive_root": str(archive_root) if archive_root.exists() else "",
        }

    def _migrate_legacy_tree(
        self,
        *,
        source_root: Path,
        archive_root: Path,
        kind: str,
    ) -> list[dict[str, Any]]:
        if not source_root.exists():
            return []
        result: list[dict[str, Any]] = []
        for source in sorted(source_root.rglob("*.toml")):
            relative = source.relative_to(source_root)
            status = "builtin_seed_archived"
            try:
                payload = tomllib.loads(source.read_text(encoding="utf-8"))
                metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
                if metadata.get("builtin") is not True:
                    if kind == "profile":
                        if not str(payload.get("profile_group") or "").strip() and str(relative.parent) != ".":
                            payload["profile_group"] = str(relative.parent).replace("/", ".")
                        model = MinionProfile.from_dict(payload)
                        target = profile_override_path(self.runtime_root, model.profile_group, model.profile_id)
                        if not target.exists():
                            migrated = self._profile_payload(model)
                            migrated_metadata = dict(migrated.get("metadata") or {})
                            migrated_metadata["builtin"] = False
                            migrated_metadata["managed_by"] = "minion_sidecar"
                            migrated["metadata"] = migrated_metadata
                            atomic_write_json(target, migrated)
                            status = "custom_override_migrated"
                        else:
                            status = "custom_override_already_present"
                    else:
                        model = MinionFamilyManifest.from_dict(payload)
                        target = family_override_path(self.runtime_root, model.family_id)
                        if not target.exists():
                            migrated = self._family_payload(model)
                            migrated_metadata = dict(migrated.get("metadata") or {})
                            migrated_metadata["builtin"] = False
                            migrated_metadata["managed_by"] = "minion_sidecar"
                            migrated["metadata"] = migrated_metadata
                            atomic_write_json(target, migrated)
                            status = "custom_override_migrated"
                        else:
                            status = "custom_override_already_present"
            except Exception as exc:
                status = f"invalid_legacy_file_archived:{exc.__class__.__name__}"
            archive = archive_root / relative
            archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, archive)
            source.unlink()
            result.append({"path": str(relative), "status": status})
        self._remove_empty_tree(source_root)
        return result

    def _validate_profile(self, profile: MinionProfile) -> None:
        family_registry = MinionFamilyRegistry(runtime_root=self.runtime_root)
        unknown: list[str] = []
        for value in (*profile.capability_groups, *profile.default_allowed_capabilities):
            if value in CAPABILITY_GROUPS or value.startswith(("op_", "intro_")):
                continue
            if family_registry.capability_group(profile.profile_group, value) is None:
                unknown.append(value)
        if unknown:
            raise ValueError(f"profile references unknown capability groups: {', '.join(sorted(set(unknown)))}")

    def _validate_family(self, family: MinionFamilyManifest) -> None:
        registry = MinionProfileRegistry(runtime_root=self.runtime_root)
        bindings = validate_role_bindings(family.role_bindings)
        unknown = sorted(
            {
                profile
                for profile in bindings.values()
                if profile != TASK_PROFILE_BINDING and registry.get(profile) is None
            }
        )
        if unknown:
            raise ValueError(f"family references unknown profiles: {', '.join(unknown)}")
        cross_family = sorted(
            {
                profile
                for profile in bindings.values()
                if profile != TASK_PROFILE_BINDING
                and registry.get(profile) is not None
                and registry.get(profile).profile_group.replace("/", ".")
                != family.family_id
            }
        )
        if cross_family:
            raise ValueError(
                "family role bindings reference cross-family profiles: "
                + ", ".join(cross_family)
            )
        if family.workflow_template == "contract_dag.v2":
            from pal.minion.v2.catalog import REGISTERED_ADAPTERS, REGISTERED_BUILDERS

            unknown_builders = sorted(set(family.builders.values()) - REGISTERED_BUILDERS)
            if unknown_builders:
                raise ValueError(f"family references unknown builders: {', '.join(unknown_builders)}")
            unknown_adapters = sorted(set(family.adapters.values()) - REGISTERED_ADAPTERS)
            if unknown_adapters:
                raise ValueError(f"family references unknown adapters: {', '.join(unknown_adapters)}")

    def _validate_effective_catalog(self) -> None:
        for profile in MinionProfileRegistry(runtime_root=self.runtime_root).list_profiles():
            self._validate_profile(profile)
        for family in MinionFamilyRegistry(runtime_root=self.runtime_root).list_families():
            self._validate_family(family)

    @staticmethod
    def _validate_patch_fields(changes: Mapping[str, Any], *, allowed: frozenset[str], kind: str) -> None:
        unknown = sorted(str(key) for key in changes if str(key) not in allowed)
        if unknown:
            raise ValueError(f"unknown {kind} override fields: {', '.join(unknown)}")

    def _resolve_profile_identity(self, selector: str) -> tuple[str, str]:
        normalized = str(selector or "").strip().replace("/", ".")
        if not normalized:
            raise ValueError("profile name is required")
        current = MinionProfileRegistry(runtime_root=self.runtime_root).get(normalized)
        if current is not None:
            return current.profile_group.replace("/", "."), current.profile_id
        if "." in normalized:
            group, name = normalized.rsplit(".", 1)
        else:
            group, name = "general", normalized
        if not group or not name:
            raise ValueError("profile must be a semantic name such as software_engineering.v2_coder")
        return group, name

    @staticmethod
    def _check_generation(expected: str, actual: str) -> None:
        if expected and expected != actual:
            raise ValueError("catalog changed since it was read; inspect the current catalog and retry the semantic patch")

    @staticmethod
    def _profile_payload(profile: MinionProfile | None) -> dict[str, Any]:
        if profile is None:
            return {}
        payload = dict(profile.to_dict())
        for key in ("canonical_profile_id", "minion_profile", "approval_policy"):
            payload.pop(key, None)
        metadata = dict(payload.get("metadata") or {})
        metadata.pop("source_path", None)
        payload["metadata"] = metadata
        return payload

    @staticmethod
    def _family_payload(family: MinionFamilyManifest | None) -> dict[str, Any]:
        if family is None:
            return {}
        payload = dict(family.to_dict())
        metadata = dict(payload.get("metadata") or {})
        metadata.pop("source_path", None)
        payload["metadata"] = metadata
        return payload

    def _append_audit(self, payload: Mapping[str, Any]) -> None:
        path = minion_catalog_root(self.runtime_root) / "audit.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            **dict(payload),
        }
        with path.open("ab") as stream:
            stream.write((json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _remove_empty_tree(root: Path) -> None:
        if not root.exists():
            return
        for path in sorted((item for item in root.rglob("*") if item.is_dir()), reverse=True):
            try:
                path.rmdir()
            except OSError:
                pass
        try:
            root.rmdir()
        except OSError:
            pass


def _merge_patch(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in patch.items():
        name = str(key)
        if value is None:
            result.pop(name, None)
        elif isinstance(value, Mapping) and isinstance(result.get(name), Mapping):
            result[name] = _merge_patch(dict(result[name]), value)
        else:
            result[name] = value
    return result


def _normalize_family_name(value: str) -> str:
    normalized = str(value or "").strip().replace("/", ".")
    if not normalized:
        raise ValueError("family name is required")
    return normalized
