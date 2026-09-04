from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
import tomllib
from typing import Any, Mapping, Protocol

from pal.bunshin.families import BunshinFamilyManifest, BunshinFamilyProvider, BunshinFamilyRegistry
from pal.bunshin.catalog_store import load_json_objects, profile_override_root
from pal.bunshin.tool_guidance import normalize_tool_guidance_overrides
from pal.bunshin.utils import dedupe_strings as _dedupe
from pal.bunshin.utils import dict_from as _dict
from pal.bunshin.utils import string_list as _string_list
from pal.bunshin.v2.ask_question import ASK_QUESTION_CAPABILITY
from pal.bunshin.v2.candidate_builder import (
    CANDIDATE_BUILDER_CAPABILITIES,
)
from pal.bunshin.v2.review_findings import ADD_FINDING_CAPABILITY
from pal.bunshin.v2.contract_submission import CONTRACT_SUBMIT_CAPABILITY
from pal.bunshin.v2.review_submission import REVIEW_SUBMIT_CAPABILITY
from pal.bunshin.v2.role_contracts import validate_family_binding_payload
from pal.bunshin.v2.work_items import UPDATE_CHECKLIST_CAPABILITY
from pal.bunshin.v2.swe_verification import (
    SWE_VERIFICATION_CAPABILITIES,
    is_swe_verification_capability,
)
from pal.bunshin.v2.verification_builder import (
    VERIFICATION_BUILDER_CAPABILITIES,
    VERIFICATION_EVIDENCE_CAPABILITIES,
    VERIFICATION_TOOL_CAPABILITIES,
    is_verification_builder_capability,
)
from pal.shared import BunshinInvocationPack


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
class BunshinPlaybookStep:
    key: str
    instruction: str
    done_when: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "BunshinPlaybookStep":
        key = str(payload.get("key") or "").strip()
        instruction = str(payload.get("instruction") or "").strip()
        done_when = str(payload.get("done_when") or "").strip()
        if not key or not instruction or not done_when:
            raise ValueError(
                "role playbook steps require key, instruction, and done_when"
            )
        return cls(key=key, instruction=instruction, done_when=done_when)

    def to_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "instruction": self.instruction,
            "done_when": self.done_when,
        }


@dataclass(frozen=True)
class BunshinTruthSource:
    source: str
    authority: str
    note: str = ""

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "BunshinTruthSource":
        source = str(payload.get("source") or "").strip()
        authority = str(payload.get("authority") or "").strip()
        if not source:
            raise ValueError("role truth source requires source")
        if authority not in {
            "normative",
            "derived",
            "evidence",
            "cursor",
            "fallback",
        }:
            raise ValueError(
                "role truth source authority must be normative, derived, "
                "evidence, cursor, or fallback"
            )
        return cls(
            source=source,
            authority=authority,
            note=str(payload.get("note") or "").strip(),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "authority": self.authority,
            **({"note": self.note} if self.note else {}),
        }


@dataclass(frozen=True)
class BunshinRoleProtocol:
    protocol_id: str
    kind: str
    modes: tuple[str, ...]
    playbook: tuple[BunshinPlaybookStep, ...]
    truth_sources: tuple[BunshinTruthSource, ...]
    checklist_policy: Mapping[str, Any]
    submission_policy: Mapping[str, Any]

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "BunshinRoleProtocol":
        protocol_id = str(payload.get("protocol_id") or "").strip()
        kind = str(payload.get("kind") or "").strip()
        modes = tuple(_string_list(payload.get("modes")))
        if not protocol_id:
            raise ValueError("role protocol requires protocol_id")
        if kind not in {"architect", "reviewer", "implementation", "verifier"}:
            raise ValueError(
                "role protocol kind must be architect, reviewer, "
                "implementation, or verifier"
            )
        if not modes:
            raise ValueError("role protocol requires at least one mode")
        raw_playbook = payload.get("playbook")
        if not isinstance(raw_playbook, Mapping):
            raise ValueError("role protocol requires [role.playbook]")
        raw_steps = raw_playbook.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("role playbook requires at least one step")
        playbook = tuple(
            BunshinPlaybookStep.from_dict(dict(item or {}))
            for item in raw_steps
        )
        keys = [item.key for item in playbook]
        if len(set(keys)) != len(keys):
            raise ValueError("role playbook step keys must be unique")
        raw_truth = payload.get("truth_sources")
        if not isinstance(raw_truth, list) or not raw_truth:
            raise ValueError("role protocol requires ordered truth_sources")
        truth_sources = tuple(
            BunshinTruthSource.from_dict(dict(item or {}))
            for item in raw_truth
        )
        checklist = _dict(payload.get("checklist"))
        submission = _dict(payload.get("submission"))
        if not str(submission.get("kind") or "").strip():
            raise ValueError("role protocol submission.kind is required")
        if not str(submission.get("tool") or "").strip():
            raise ValueError("role protocol submission.tool is required")
        return cls(
            protocol_id=protocol_id,
            kind=kind,
            modes=modes,
            playbook=playbook,
            truth_sources=truth_sources,
            checklist_policy={
                "allow_worker_items": bool(
                    checklist.get("allow_worker_items", True)
                ),
                "max_items": max(1, int(checklist.get("max_items") or 64)),
                "require_complete": bool(
                    checklist.get("require_complete", True)
                ),
            },
            submission_policy=submission,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_id": self.protocol_id,
            "kind": self.kind,
            "modes": list(self.modes),
            "playbook": {
                "steps": [item.to_dict() for item in self.playbook],
            },
            "truth_sources": [
                item.to_dict() for item in self.truth_sources
            ],
            "checklist": dict(self.checklist_policy),
            "submission": dict(self.submission_policy),
        }


@dataclass(frozen=True)
class BunshinProfile:
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
    capability_guidance_overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    output_policy: dict[str, Any] = field(default_factory=dict)
    role_protocol: BunshinRoleProtocol | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def canonical_profile_id(self) -> str:
        return canonical_profile_id(self.profile_group, self.profile_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "canonical_profile_id": self.canonical_profile_id,
            "bunshin_profile": self.canonical_profile_id,
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
            "capability_guidance_overrides": {
                canonical: dict(patch)
                for canonical, patch in self.capability_guidance_overrides.items()
            },
            "output_policy": dict(self.output_policy),
            **(
                {"role": self.role_protocol.to_dict()}
                if self.role_protocol is not None
                else {}
            ),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BunshinProfile":
        if not isinstance(payload, dict):
            raise ValueError("BunshinProfile payload must be an object")
        profile_id = str(payload.get("profile_id") or "").strip()
        if not profile_id:
            raise ValueError("BunshinProfile.profile_id is required")
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
            capability_guidance_overrides=normalize_tool_guidance_overrides(
                payload.get("capability_guidance_overrides")
            ),
            output_policy=_dict(payload.get("output_policy")),
            role_protocol=(
                BunshinRoleProtocol.from_dict(payload["role"])
                if isinstance(payload.get("role"), Mapping)
                else None
            ),
            metadata=metadata,
        )


class BunshinProfileProvider(Protocol):
    def declared_bunshin_profiles(self) -> list[BunshinProfile | dict[str, Any]]:
        ...


class BunshinProfileCapabilityProvider(Protocol):
    def capabilities_for_bunshin_profile(self, profile: BunshinProfile, pack: BunshinInvocationPack) -> list[str]:
        ...


@dataclass(frozen=True)
class _PinnedFamilyProvider:
    family: BunshinFamilyManifest

    def declared_bunshin_families(self) -> list[BunshinFamilyManifest]:
        return [self.family]


@dataclass
class BunshinProfileRegistry:
    profile_providers: tuple[BunshinProfileProvider, ...] = ()
    capability_providers: tuple[BunshinProfileCapabilityProvider, ...] = ()
    family_providers: tuple[BunshinFamilyProvider, ...] = ()
    ambient_capabilities: tuple[str, ...] = ()
    runtime_root: Path | None = None
    builtin_profiles: tuple[BunshinProfile, ...] = field(default_factory=lambda: load_builtin_bunshin_profiles())

    def list_profiles(self) -> list[BunshinProfile]:
        profiles: dict[tuple[str, str], BunshinProfile] = {}
        for profile in self.builtin_profiles:
            profiles[_profile_key(profile)] = profile
        for profile in self._runtime_profiles():
            profiles[_profile_key(profile)] = profile
        for provider in self.profile_providers:
            declare = getattr(provider, "declared_bunshin_profiles", None)
            if not callable(declare):
                continue
            for item in list(declare() or []):
                profile = item if isinstance(item, BunshinProfile) else BunshinProfile.from_dict(dict(item or {}))
                profiles[_profile_key(profile)] = profile
        return [profiles[key] for key in sorted(profiles)]

    def get(self, profile_id: str) -> BunshinProfile | None:
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

    def get_ref(self, profile_group: str, profile_name: str) -> BunshinProfile | None:
        key = (_profile_scope(profile_group), str(profile_name or "generic").strip() or "generic")
        for profile in self.list_profiles():
            if _profile_key(profile) == key:
                return profile
        return None

    def resolve_pack(
        self,
        pack: BunshinInvocationPack,
        *,
        requested_profile: str = "",
        requested_profile_group: str = "",
        requested_profile_name: str = "",
    ) -> BunshinInvocationPack:
        if requested_profile_group or requested_profile_name:
            profile = self.get_ref(requested_profile_group or pack.profile_group, requested_profile_name or pack.profile_name)
            profile_id = canonical_profile_id(requested_profile_group or pack.profile_group, requested_profile_name or pack.profile_name)
        else:
            profile_id = str(requested_profile or pack.bunshin_profile or "generic").strip() or "generic"
            profile = self.get(profile_id)
        if profile is None:
            raise KeyError(f"unknown bunshin profile: {profile_id}")
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
        # Workers without startup skill refs retain the resident Pal path for
        # discovering and loading read-only procedural references themselves.
        allowed_capabilities = _dedupe(
            [*allowed_capabilities, *BUNSHIN_SKILL_REFERENCE_CAPABILITIES]
        )
        allowed_capabilities = filter_bunshin_allowed_capabilities(
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
        return BunshinInvocationPack.from_dict(
            {
                **pack.to_dict(),
                "profile_group": profile.profile_group,
                "profile_name": profile.profile_id,
                "bunshin_profile": profile.canonical_profile_id,
                "resolved_profile": resolved_profile,
                "allowed_capabilities": allowed_capabilities,
                "allowed_skills": allowed_skills,
                "approval_policy": approval_policy,
                "workspace": workspace,
                "metadata": metadata,
            }
        )

    def _hook_capabilities(self, profile: BunshinProfile, pack: BunshinInvocationPack) -> list[str]:
        result: list[str] = []
        for provider in self.capability_providers:
            hook = getattr(provider, "capabilities_for_bunshin_profile", None)
            if not callable(hook):
                continue
            result.extend(_string_list(hook(profile, pack)))
        return _dedupe(result)

    def _runtime_profiles(self) -> list[BunshinProfile]:
        if self.runtime_root is None:
            return []
        return _load_profile_overrides(Path(self.runtime_root))

    def family_registry(self) -> BunshinFamilyRegistry:
        return BunshinFamilyRegistry(
            family_providers=self.family_providers,
            runtime_root=self.runtime_root,
        )


def resolve_pinned_bunshin_pack(
    pack: BunshinInvocationPack,
    *,
    profile_payload: dict[str, Any],
    family_payload: dict[str, Any],
) -> BunshinInvocationPack:
    """Resolve a worker pack from its immutable FamilyBindingArtifact."""

    profile = BunshinProfile.from_dict(dict(profile_payload or {}))
    family_value = dict(family_payload or {})
    resolved_bindings = validate_family_binding_payload(family_value)
    family_value["role_bindings"] = {
        role: (
            {
                "participant": "null",
                "reason": str(dict(binding or {}).get("reason") or ""),
            }
            if str(dict(binding or {})["participant"]) == "null"
            else {
                "participant": "profile",
                "profile": str(
                    dict(binding or {}).get("selector")
                    or dict(binding or {}).get("profile")
                    or ""
                ),
            }
        )
        for role, binding in resolved_bindings.items()
    }
    family_value["architecture"] = {
        "specialization": str(
            dict(family_value.get("architecture_definition") or {}).get(
                "specialization_id"
            )
            or ""
        )
    }
    family = BunshinFamilyManifest.from_dict(family_value)
    requested = str(pack.bunshin_profile or "").strip()
    if requested and requested != profile.canonical_profile_id:
        raise ValueError(
            f"pinned profile {profile.canonical_profile_id} does not match worker binding {requested}"
        )
    registry = BunshinProfileRegistry(
        builtin_profiles=(profile,),
        family_providers=(_PinnedFamilyProvider(family),),
        runtime_root=None,
    )
    return registry.resolve_pack(pack, requested_profile=profile.canonical_profile_id)


CORE_BUNSHIN_CAPABILITIES = (
    "op_tool_search",
    "op_tool_read",
    "op_bunshin_artifact_write",
    "op_bunshin_artifact_edit",
    "op_bunshin_memory_candidate_write",
    "op_memory_recall",
)


WORKSPACE_READ_CAPABILITIES = (
    "op_file_read",
    "op_exec_shell",
)


CAPABILITY_GROUPS: dict[str, tuple[str, ...]] = {
    "core_bunshin_read": CORE_BUNSHIN_CAPABILITIES,
    "tool_discovery": ("op_tool_search", "op_tool_read"),
    "capability_call": ("op_tool_call",),
    "bunshin_artifacts": ("op_bunshin_artifact_write", "op_bunshin_artifact_edit"),
    "v2_contract_file_author": (
        UPDATE_CHECKLIST_CAPABILITY,
        CONTRACT_SUBMIT_CAPABILITY,
        ASK_QUESTION_CAPABILITY,
    ),
    "v2_contract_reviewer": (
        UPDATE_CHECKLIST_CAPABILITY,
        ADD_FINDING_CAPABILITY,
        REVIEW_SUBMIT_CAPABILITY,
    ),
    "v2_candidate_builder": (
        UPDATE_CHECKLIST_CAPABILITY,
        *CANDIDATE_BUILDER_CAPABILITIES,
    ),
    "v2_verification_builder": VERIFICATION_BUILDER_CAPABILITIES,
    "v2_swe_verification": (
        *SWE_VERIFICATION_CAPABILITIES,
        *VERIFICATION_EVIDENCE_CAPABILITIES,
        ADD_FINDING_CAPABILITY,
        UPDATE_CHECKLIST_CAPABILITY,
        "op_bunshin_verification_scratch_write",
    ),
    "bunshin_memory_candidates": ("op_bunshin_memory_candidate_write",),
    "memory_recall": ("op_memory_recall",),
    "skill_reference": ("op_skill_search", "op_skill_read", "op_skill_inject"),
    "workspace_read": WORKSPACE_READ_CAPABILITIES,
    "architecture_workspace_read": WORKSPACE_READ_CAPABILITIES,
    "workspace_write": (
        "op_file_edit",
        "op_file_write",
    ),
    "web_research": ("op_web_search", "op_browser_read"),
    "code_intel": (
        "op_lsp_status",
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
    "swe_verifier_workspace": (
        "op_file_read",
        "op_file_edit",
        "op_file_write",
        "op_exec_shell",
        "op_lsp_hover",
        "op_lsp_definition",
        "op_lsp_references",
        "op_lsp_document_symbols",
        "op_lsp_workspace_symbols",
        "op_lsp_diagnostics",
    ),
    "swe_reviewer_workspace": (
        "op_file_read",
        "op_exec_shell",
        "op_lsp_hover",
        "op_lsp_definition",
        "op_lsp_references",
        "op_lsp_document_symbols",
        "op_lsp_workspace_symbols",
        "op_lsp_diagnostics",
    ),
    "code_work": (
        "op_file_read",
        "op_file_edit",
        "op_file_write",
        "op_exec_shell",
    ),
}


BUNSHIN_SKILL_REFERENCE_CAPABILITIES = CAPABILITY_GROUPS["skill_reference"]


DEFAULT_BUNSHIN_DENIED_CAPABILITIES = frozenset(
    {
        "op_path_delete",
        "op_behavior_advise",
        "op_behavior_save",
        "op_channel_send_attachment",
        "op_memory_write",
        "op_memory_update",
        "op_memory_delete",
        "op_plugin_mgmt_disable",
        "op_plugin_mgmt_enable",
        "op_plugin_mgmt_rescan",
        "op_skill_assimilate",
        "op_skill_commit",
        "op_skill_disable",
        "op_skill_update",
    }
)


DEFAULT_BUNSHIN_DENIED_PREFIXES = (
    "intro_",
    "op_channel_mgmt_",
    "op_bunshin_",
    "op_plugin_mgmt_",
)


DEFAULT_BUNSHIN_DENIED_FRAGMENTS = (
    "_management_",
    "_attach",
    "_detach",
    "_enable",
    "_disable",
    "_rescan",
    "_restart",
    "_shutdown",
)


BUNSHIN_INTERNAL_ALLOWED_CAPABILITIES = frozenset(
    {
        "op_bunshin_artifact_write",
        "op_bunshin_artifact_edit",
        "op_bunshin_memory_candidate_write",
        *CANDIDATE_BUILDER_CAPABILITIES,
        *VERIFICATION_TOOL_CAPABILITIES,
        *SWE_VERIFICATION_CAPABILITIES,
        ADD_FINDING_CAPABILITY,
        UPDATE_CHECKLIST_CAPABILITY,
        CONTRACT_SUBMIT_CAPABILITY,
        REVIEW_SUBMIT_CAPABILITY,
        ASK_QUESTION_CAPABILITY,
    }
)


def filter_bunshin_allowed_capabilities(
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
        if not is_bunshin_capability_denied(value, capability_policy=capability_policy)
    ]


def is_bunshin_capability_denied(name: str, *, capability_policy: dict[str, Any] | None = None) -> bool:
    capability = str(name or "").strip()
    if not capability:
        return True
    policy = dict(capability_policy or {})
    extra_denied = set(_string_list(policy.get("deny_capabilities")))
    denied = DEFAULT_BUNSHIN_DENIED_CAPABILITIES | frozenset(extra_denied)
    if capability in denied:
        return True
    if (
        capability in BUNSHIN_INTERNAL_ALLOWED_CAPABILITIES
        or is_verification_builder_capability(capability)
        or is_swe_verification_capability(capability)
    ):
        return False
    if (
        str(policy.get("risk") or "").strip().lower() == "read_only"
        and capability == "op_exec_shell"
        and not bool(policy.get("allow_read_only_shell", False))
    ):
        return True
    prefixes = (*DEFAULT_BUNSHIN_DENIED_PREFIXES, *tuple(_string_list(policy.get("deny_prefixes"))))
    if any(capability.startswith(prefix) for prefix in prefixes):
        return True
    fragments = (*DEFAULT_BUNSHIN_DENIED_FRAGMENTS, *tuple(_string_list(policy.get("deny_fragments"))))
    return any(fragment and fragment in capability for fragment in fragments)


def load_builtin_bunshin_profiles() -> tuple[BunshinProfile, ...]:
    root = resources.files("pal.bunshin").joinpath("profile_templates")
    profiles: list[BunshinProfile] = []
    for item, profile_group in _iter_profile_template_files(root):
        payload = tomllib.loads(item.read_text(encoding="utf-8"))
        payload.setdefault("profile_group", profile_group)
        profiles.append(BunshinProfile.from_dict(payload))
    return tuple(profiles)


def _load_profile_overrides(runtime_root: Path) -> list[BunshinProfile]:
    profiles: list[BunshinProfile] = []
    for path, payload in load_json_objects(profile_override_root(runtime_root)):
        profile = BunshinProfile.from_dict(payload)
        metadata = dict(profile.metadata)
        metadata["source_path"] = str(path)
        metadata["catalog_source"] = "override"
        profiles.append(BunshinProfile.from_dict({**profile.to_dict(), "metadata": metadata}))
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
    family_registry: BunshinFamilyRegistry | None = None,
) -> list[str]:
    registry = family_registry or BunshinFamilyRegistry()
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


def _profile_key(profile: BunshinProfile) -> tuple[str, str]:
    return (_profile_scope(profile.profile_group), str(profile.profile_id or "generic").strip() or "generic")


def _profile_scope(profile_group: str) -> str:
    return str(profile_group or "general").strip().replace("/", ".") or "general"
