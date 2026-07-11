from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
import tomllib
from typing import Any, Protocol

from pal.minion.families import MinionFamilyProvider, MinionFamilyRegistry
from pal.minion.utils import dedupe_strings as _dedupe
from pal.minion.utils import dict_from as _dict
from pal.minion.utils import string_list as _string_list
from pal.minion.v2.contract_builder import (
    ARCHITECTURE_REVIEW_BUILDER_CAPABILITIES,
    CONTRACT_BUILDER_CAPABILITIES,
    CONTRACT_SKETCH_BUILDER_CAPABILITIES,
    EVIDENCE_BUILDER_CAPABILITIES,
    REQUIREMENTS_BUILDER_CAPABILITIES,
    is_contract_builder_capability,
)
from pal.shared import MinionInvocationPack


_PROFILE_RUNTIME_METADATA_KEYS = frozenset(
    {
        "heartbeat_interval_seconds",
        "llm_round_timeout_seconds",
        "manager_turn_timeout_seconds",
        "max_output_tokens",
        "max_tool_rounds",
        "temperature",
        "timeout_seconds",
    }
)

@dataclass(frozen=True)
class MinionProfile:
    profile_id: str
    display_name: str
    identity_fragment: str
    profile_group: str = "general"
    behavior_fragment: str = ""
    output_contract_fragment: str = ""
    preferred_endpoint_id: str = ""
    capability_groups: tuple[str, ...] = ()
    default_allowed_capabilities: tuple[str, ...] = ()
    skill_refs: tuple[str, ...] = ()
    default_allowed_skills: tuple[str, ...] = ()
    default_approval_policy: dict[str, Any] = field(default_factory=dict)
    workspace_policy: dict[str, Any] = field(default_factory=dict)
    workspace_environment_policy: dict[str, Any] = field(default_factory=dict)
    completion_policy: dict[str, Any] = field(default_factory=dict)
    capability_policy: dict[str, Any] = field(default_factory=dict)
    capability_description_overrides: dict[str, str] = field(default_factory=dict)
    output_policy: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def canonical_profile_id(self) -> str:
        return canonical_profile_id(self.profile_group, self.profile_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "canonical_profile_id": self.canonical_profile_id,
            "minion_profile": self.canonical_profile_id,
            "display_name": self.display_name,
            "profile_group": self.profile_group,
            "identity_fragment": self.identity_fragment,
            "behavior_fragment": self.behavior_fragment,
            "output_contract_fragment": self.output_contract_fragment,
            "preferred_endpoint_id": self.preferred_endpoint_id,
            "capability_groups": list(self.capability_groups),
            "default_allowed_capabilities": list(self.default_allowed_capabilities),
            "skill_refs": list(self.skill_refs or self.default_allowed_skills),
            "default_approval_policy": dict(self.default_approval_policy),
            "approval_policy": dict(self.default_approval_policy),
            "workspace_policy": dict(self.workspace_policy),
            "workspace_environment_policy": dict(self.workspace_environment_policy),
            "completion_policy": dict(self.completion_policy),
            "capability_policy": dict(self.capability_policy),
            "capability_description_overrides": dict(self.capability_description_overrides),
            "output_policy": dict(self.output_policy),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MinionProfile":
        if not isinstance(payload, dict):
            raise ValueError("MinionProfile payload must be an object")
        profile_id = str(payload.get("profile_id") or "").strip()
        if not profile_id:
            raise ValueError("MinionProfile.profile_id is required")
        display_name = str(payload.get("display_name") or profile_id).strip()
        metadata = _dict(payload.get("metadata"))
        for key in _PROFILE_RUNTIME_METADATA_KEYS:
            if key in payload and key not in metadata:
                metadata[key] = payload[key]
        skill_refs = tuple(
            _string_list(
                payload.get("skill_refs")
                or payload.get("default_allowed_skills")
                or payload.get("default_suggested_skills")
                or payload.get("suggested_skills")
                or payload.get("skills")
            )
        )
        return cls(
            profile_id=profile_id,
            display_name=display_name,
            profile_group=str(payload.get("profile_group") or metadata.get("profile_group") or "general").strip() or "general",
            identity_fragment=str(payload.get("identity_fragment") or ""),
            behavior_fragment=str(payload.get("behavior_fragment") or ""),
            output_contract_fragment=str(payload.get("output_contract_fragment") or ""),
            preferred_endpoint_id=str(payload.get("preferred_endpoint_id") or metadata.get("preferred_endpoint_id") or "").strip(),
            capability_groups=tuple(_string_list(payload.get("capability_groups"))),
            default_allowed_capabilities=tuple(
                _string_list(payload.get("default_allowed_capabilities") or payload.get("allowed_capabilities"))
            ),
            skill_refs=skill_refs,
            default_allowed_skills=skill_refs,
            default_approval_policy=_dict(payload.get("default_approval_policy") or payload.get("approval_policy")),
            workspace_policy=_dict(payload.get("workspace_policy")),
            workspace_environment_policy=_dict(payload.get("workspace_environment_policy") or payload.get("workspace_environment")),
            completion_policy=_dict(payload.get("completion_policy")),
            capability_policy=_dict(payload.get("capability_policy")),
            capability_description_overrides={
                str(key).strip(): str(value).strip()
                for key, value in _dict(payload.get("capability_description_overrides")).items()
                if str(key).strip() and str(value).strip()
            },
            output_policy=_dict(payload.get("output_policy")),
            metadata=metadata,
        )


class MinionProfileProvider(Protocol):
    def declared_minion_profiles(self) -> list[MinionProfile | dict[str, Any]]:
        ...


class MinionProfileCapabilityProvider(Protocol):
    def capabilities_for_minion_profile(self, profile: MinionProfile, pack: MinionInvocationPack) -> list[str]:
        ...


@dataclass
class MinionProfileRegistry:
    profile_providers: tuple[MinionProfileProvider, ...] = ()
    capability_providers: tuple[MinionProfileCapabilityProvider, ...] = ()
    family_providers: tuple[MinionFamilyProvider, ...] = ()
    ambient_capabilities: tuple[str, ...] = ()
    runtime_root: Path | None = None
    builtin_profiles: tuple[MinionProfile, ...] = field(default_factory=lambda: BUILTIN_MINION_PROFILES)

    def list_profiles(self) -> list[MinionProfile]:
        profiles: dict[tuple[str, str], MinionProfile] = {}
        for profile in self.builtin_profiles:
            profiles[_profile_key(profile)] = profile
        for profile in self._runtime_profiles():
            profiles[_profile_key(profile)] = profile
        for provider in self.profile_providers:
            declare = getattr(provider, "declared_minion_profiles", None)
            if not callable(declare):
                continue
            for item in list(declare() or []):
                profile = item if isinstance(item, MinionProfile) else MinionProfile.from_dict(dict(item or {}))
                profiles[_profile_key(profile)] = profile
        return [profiles[key] for key in sorted(profiles)]

    def get(self, profile_id: str) -> MinionProfile | None:
        normalized = str(profile_id or "generic").strip() or "generic"
        profiles = self.list_profiles()
        for profile in profiles:
            if profile.canonical_profile_id == normalized:
                return profile
        if "." in normalized:
            scope, role = normalized.rsplit(".", 1)
            key = (_profile_scope(scope), role.strip())
            for profile in profiles:
                if _profile_key(profile) == key:
                    return profile
        matches = [profile for profile in profiles if profile.profile_id == normalized]
        return matches[0] if len(matches) == 1 else None

    def get_ref(self, profile_group: str, profile_name: str) -> MinionProfile | None:
        key = (_profile_scope(profile_group), str(profile_name or "generic").strip() or "generic")
        for profile in self.list_profiles():
            if _profile_key(profile) == key:
                return profile
        return None

    def resolve_pack(
        self,
        pack: MinionInvocationPack,
        *,
        requested_profile: str = "",
        requested_profile_group: str = "",
        requested_profile_name: str = "",
    ) -> MinionInvocationPack:
        if requested_profile_group or requested_profile_name:
            profile = self.get_ref(requested_profile_group or pack.profile_group, requested_profile_name or pack.profile_name)
            profile_id = canonical_profile_id(requested_profile_group or pack.profile_group, requested_profile_name or pack.profile_name)
        else:
            profile_id = str(requested_profile or pack.minion_profile or "generic").strip() or "generic"
            profile = self.get(profile_id)
        if profile is None:
            raise KeyError(f"unknown minion profile: {profile_id}")
        capability_policy = dict(profile.capability_policy)
        base_capabilities = _expand_capabilities(
            [*profile.capability_groups, *profile.default_allowed_capabilities],
            family_id=profile.profile_group,
            family_registry=self.family_registry(),
        )
        if _should_inherit_ambient_capabilities(capability_policy) and self.ambient_capabilities:
            base_capabilities = _dedupe([*self.ambient_capabilities, *base_capabilities])
        if pack.allowed_capabilities:
            requested_capabilities = _dedupe(list(pack.allowed_capabilities))
            if str(capability_policy.get("mode") or "").strip().lower() == "profile_only":
                base_set = set(base_capabilities)
                allowed_capabilities = [item for item in requested_capabilities if item in base_set]
            else:
                allowed_capabilities = requested_capabilities
        else:
            allowed_capabilities = base_capabilities
        allowed_skills = _dedupe(list(pack.allowed_skills) or list(profile.skill_refs or profile.default_allowed_skills))
        approval_policy = dict(profile.default_approval_policy)
        approval_policy.update(dict(pack.approval_policy))
        workspace_policy = dict(profile.workspace_policy)
        if isinstance(pack.workspace.get("workspace_policy"), dict):
            workspace_policy.update(dict(pack.workspace.get("workspace_policy") or {}))
        workspace_environment_policy = dict(profile.workspace_environment_policy)
        workspace_environment_override = pack.workspace.get("workspace_environment_policy")
        if not isinstance(workspace_environment_override, dict):
            workspace_environment_override = pack.workspace.get("workspace_environment")
        if isinstance(workspace_environment_override, dict):
            workspace_environment_policy.update(dict(workspace_environment_override or {}))
        completion_policy = dict(profile.completion_policy)
        if isinstance(pack.workspace.get("completion_policy"), dict):
            completion_policy.update(dict(pack.workspace.get("completion_policy") or {}))
        output_policy = dict(profile.output_policy)
        if isinstance(pack.workspace.get("output_policy"), dict):
            output_policy.update(dict(pack.workspace.get("output_policy") or {}))
        hook_capabilities = self._hook_capabilities(profile, pack)
        if hook_capabilities:
            allowed_capabilities = _dedupe([*allowed_capabilities, *hook_capabilities])
        allowed_capabilities = filter_minion_allowed_capabilities(
            allowed_capabilities,
            capability_policy=capability_policy,
        )
        resolved_profile = profile.to_dict()
        resolved_profile["effective_skill_refs"] = list(allowed_skills)
        resolved_profile["effective_approval_policy"] = dict(approval_policy)
        resolved_profile["effective_workspace_policy"] = dict(workspace_policy)
        resolved_profile["effective_workspace_environment_policy"] = dict(workspace_environment_policy)
        resolved_profile["effective_completion_policy"] = dict(completion_policy)
        resolved_profile["effective_capability_policy"] = dict(capability_policy)
        resolved_profile["effective_output_policy"] = dict(output_policy)
        workspace = dict(pack.workspace)
        if workspace_policy:
            workspace["workspace_policy"] = dict(workspace_policy)
        if workspace_environment_policy:
            workspace["workspace_environment_policy"] = dict(workspace_environment_policy)
        if completion_policy:
            workspace["completion_policy"] = dict(completion_policy)
        if output_policy:
            workspace["output_policy"] = dict(output_policy)
        metadata = dict(pack.metadata)
        for key in _PROFILE_RUNTIME_METADATA_KEYS:
            if key not in metadata and key in profile.metadata:
                metadata[key] = profile.metadata[key]
        preferred_endpoint_id = str(metadata.get("preferred_endpoint_id") or "").strip()
        if preferred_endpoint_id:
            metadata.setdefault("preferred_endpoint_source", "explicit")
        elif profile.preferred_endpoint_id:
            metadata["preferred_endpoint_id"] = profile.preferred_endpoint_id
            metadata["preferred_endpoint_source"] = "profile"
        return MinionInvocationPack.from_dict(
            {
                **pack.to_dict(),
                "profile_group": profile.profile_group,
                "profile_name": profile.profile_id,
                "minion_profile": profile.canonical_profile_id,
                "resolved_profile": resolved_profile,
                "allowed_capabilities": allowed_capabilities,
                "allowed_skills": allowed_skills,
                "approval_policy": approval_policy,
                "workspace": workspace,
                "metadata": metadata,
            }
        )

    def _hook_capabilities(self, profile: MinionProfile, pack: MinionInvocationPack) -> list[str]:
        result: list[str] = []
        for provider in self.capability_providers:
            hook = getattr(provider, "capabilities_for_minion_profile", None)
            if not callable(hook):
                continue
            result.extend(_string_list(hook(profile, pack)))
        return _dedupe(result)

    def _runtime_profiles(self) -> list[MinionProfile]:
        if self.runtime_root is None:
            return []
        profile_dir = Path(self.runtime_root) / "plugins" / "minion" / "profiles"
        return _load_profiles_from_dir(profile_dir)

    def family_registry(self) -> MinionFamilyRegistry:
        return MinionFamilyRegistry(
            family_providers=self.family_providers,
            runtime_root=self.runtime_root,
        )


CORE_MINION_CAPABILITIES = (
    "op_tool_search",
    "op_tool_read",
    "op_minion_artifact_write",
    "op_minion_artifact_edit",
    "op_minion_memory_candidate_write",
    "op_memory_recall",
)


WORKSPACE_READ_CAPABILITIES = (
    "op_tree",
    "op_search",
    "op_file_read",
    "op_git",
)


CAPABILITY_GROUPS: dict[str, tuple[str, ...]] = {
    "core_minion_read": CORE_MINION_CAPABILITIES,
    "tool_discovery": ("op_tool_search", "op_tool_read"),
    "capability_call": ("op_tool_call",),
    "minion_artifacts": ("op_minion_artifact_write", "op_minion_artifact_edit"),
    "v2_requirements_builder": REQUIREMENTS_BUILDER_CAPABILITIES,
    "v2_bound_input_read": ("op_minion_input_read",),
    "v2_evidence_builder": EVIDENCE_BUILDER_CAPABILITIES,
    "v2_contract_sketch_builder": CONTRACT_SKETCH_BUILDER_CAPABILITIES,
    "v2_architecture_review_builder": ARCHITECTURE_REVIEW_BUILDER_CAPABILITIES,
    "minion_memory_candidates": ("op_minion_memory_candidate_write",),
    "memory_recall": ("op_memory_recall",),
    "workspace_read": WORKSPACE_READ_CAPABILITIES,
    "workspace_write": (
        "op_file_edit",
        "op_file_write",
        "op_path_delete",
    ),
    "web_research": ("op_web_search", "op_web_read"),
    "code_intel": (
        "op_lsp_status",
        "op_lsp_doctor",
        "op_lsp_hover",
        "op_lsp_definition",
        "op_lsp_implementation",
        "op_lsp_references",
        "op_lsp_prepare_call_hierarchy",
        "op_lsp_incoming_calls",
        "op_lsp_outgoing_calls",
        "op_lsp_document_symbols",
        "op_lsp_workspace_symbols",
        "op_lsp_diagnostics",
    ),
    "verification_shell": ("op_exec_shell",),
    "code_work": (
        "op_file_read",
        "op_file_edit",
        "op_file_write",
        "op_path_delete",
        "op_git",
        "op_exec_shell",
    ),
}


DEFAULT_MINION_DENIED_CAPABILITIES = frozenset(
    {
        "op_behavior_advise",
        "op_behavior_save",
        "op_channel_send_attachment",
        "op_memory_write",
        "op_memory_update",
        "op_memory_delete",
        "op_memory_refresh_indexes",
        "op_plugin_mgmt_disable",
        "op_plugin_mgmt_enable",
        "op_plugin_mgmt_rescan",
        "op_skill_assimilate",
        "op_skill_commit",
        "op_skill_disable",
        "op_skill_inject",
        "op_skill_update",
    }
)


DEFAULT_MINION_DENIED_PREFIXES = (
    "intro_",
    "op_channel_mgmt_",
    "op_minion_",
    "op_plugin_mgmt_",
)


DEFAULT_MINION_DENIED_FRAGMENTS = (
    "_management_",
    "_attach",
    "_detach",
    "_enable",
    "_disable",
    "_rescan",
    "_restart",
    "_shutdown",
)


MINION_INTERNAL_ALLOWED_CAPABILITIES = frozenset(
    {
        "op_minion_artifact_write",
        "op_minion_artifact_edit",
        "op_minion_input_read",
        "op_minion_memory_candidate_write",
        *CONTRACT_BUILDER_CAPABILITIES,
    }
)


def filter_minion_allowed_capabilities(
    values: list[str] | tuple[str, ...],
    *,
    capability_policy: dict[str, Any] | None = None,
) -> list[str]:
    canonical_values = _dedupe(list(values))
    noncanonical = [
        name
        for name in canonical_values
        if not name.startswith(("op_", "intro_"))
    ]
    if noncanonical:
        raise ValueError(
            "allowed_capabilities must contain canonical capability paths; "
            f"received aliases: {', '.join(noncanonical)}"
        )
    return [
        value
        for value in canonical_values
        if not is_minion_capability_denied(value, capability_policy=capability_policy)
    ]


def is_minion_capability_denied(name: str, *, capability_policy: dict[str, Any] | None = None) -> bool:
    capability = str(name or "").strip()
    if not capability:
        return True
    policy = dict(capability_policy or {})
    extra_denied = set(_string_list(policy.get("deny_capabilities")))
    denied = DEFAULT_MINION_DENIED_CAPABILITIES | frozenset(extra_denied)
    if capability in denied:
        return True
    if capability in MINION_INTERNAL_ALLOWED_CAPABILITIES or is_contract_builder_capability(capability):
        return False
    if str(policy.get("risk") or "").strip().lower() == "read_only" and capability == "op_exec_shell":
        return True
    prefixes = (*DEFAULT_MINION_DENIED_PREFIXES, *tuple(_string_list(policy.get("deny_prefixes"))))
    if any(capability.startswith(prefix) for prefix in prefixes):
        return True
    fragments = (*DEFAULT_MINION_DENIED_FRAGMENTS, *tuple(_string_list(policy.get("deny_fragments"))))
    return any(fragment and fragment in capability for fragment in fragments)


def load_builtin_minion_profiles() -> tuple[MinionProfile, ...]:
    root = resources.files("pal.minion").joinpath("profile_templates")
    profiles: list[MinionProfile] = []
    for item, profile_group in _iter_profile_template_files(root):
        payload = tomllib.loads(item.read_text(encoding="utf-8"))
        payload.setdefault("profile_group", profile_group)
        profiles.append(MinionProfile.from_dict(payload))
    return tuple(profiles)


def _load_profiles_from_dir(profile_dir: Path) -> list[MinionProfile]:
    if not profile_dir.exists():
        return []
    profiles: list[MinionProfile] = []
    for path in sorted(profile_dir.rglob("*.toml")):
        try:
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
            relative_parent = path.parent.relative_to(profile_dir)
            if str(relative_parent) != ".":
                payload.setdefault("profile_group", str(relative_parent).replace("\\", "/"))
            profile = MinionProfile.from_dict(payload)
        except Exception:
            continue
        metadata = dict(profile.metadata)
        metadata.setdefault("source_path", str(path))
        profiles.append(MinionProfile.from_dict({**profile.to_dict(), "metadata": metadata}))
    return profiles


def _iter_profile_template_files(root: Any) -> list[tuple[Any, str]]:
    result: list[tuple[Any, str]] = []

    def visit(node: Any, group_parts: list[str]) -> None:
        for item in sorted(node.iterdir(), key=lambda path: path.name):
            if item.is_dir():
                visit(item, [*group_parts, item.name])
            elif item.name.endswith(".toml"):
                result.append((item, "/".join(group_parts) or "general"))

    visit(root, [])
    return result


def _expand_capabilities(
    values: list[str],
    *,
    family_id: str = "general",
    family_registry: MinionFamilyRegistry | None = None,
) -> list[str]:
    registry = family_registry or MinionFamilyRegistry()
    result: list[str] = []
    seen_groups: set[str] = set()

    def expand_value(value: str) -> None:
        group_name = str(value or "").strip()
        if not group_name:
            return
        if group_name in CAPABILITY_GROUPS:
            for item in CAPABILITY_GROUPS[group_name]:
                expand_value(item)
            return
        family_group = registry.capability_group(family_id, group_name)
        if family_group is None:
            result.append(group_name)
            return
        key = f"{family_id}:{group_name}"
        if "." in group_name:
            key = group_name
        if key in seen_groups:
            return
        seen_groups.add(key)
        for included in family_group.include:
            expand_value(included)
        result.extend(family_group.capabilities)

    for value in values:
        expand_value(value)
    return _dedupe(result)


def _should_inherit_ambient_capabilities(capability_policy: dict[str, Any]) -> bool:
    mode = str(capability_policy.get("mode") or "").strip().lower()
    return mode in {"inherit", "inherit_filtered", "filtered_inherit"}


def canonical_profile_id(profile_group: str, profile_id: str) -> str:
    role = str(profile_id or "generic").strip() or "generic"
    scope = _profile_scope(profile_group)
    return role if scope == "general" else f"{scope}.{role}"


def _profile_key(profile: MinionProfile) -> tuple[str, str]:
    return (_profile_scope(profile.profile_group), str(profile.profile_id or "generic").strip() or "generic")


def _profile_scope(profile_group: str) -> str:
    return str(profile_group or "general").strip().replace("/", ".") or "general"


BUILTIN_MINION_PROFILES = load_builtin_minion_profiles()
