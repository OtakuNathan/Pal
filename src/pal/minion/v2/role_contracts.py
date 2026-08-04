from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from pal.minion.families import MinionRoleBinding


TASK_PROFILE_BINDING = "$task_profile"


class OrchestrationRole(StrEnum):
    ARCHITECT = "architect"
    REVIEWER = "reviewer"
    IMPLEMENTATION = "implementation"
    VERIFIER = "verifier"


class RoleMode(StrEnum):
    AUTHOR = "author"
    REVISION = "revision"
    ARCHITECTURE = "architecture"
    STANDALONE = "standalone"
    PRODUCE = "produce"
    REPAIR = "repair"
    MODULE = "module"


ROLE_MODES: Mapping[OrchestrationRole, frozenset[RoleMode]] = {
    OrchestrationRole.ARCHITECT: frozenset({RoleMode.AUTHOR, RoleMode.REVISION}),
    OrchestrationRole.REVIEWER: frozenset({RoleMode.ARCHITECTURE, RoleMode.STANDALONE}),
    OrchestrationRole.IMPLEMENTATION: frozenset({RoleMode.PRODUCE, RoleMode.REPAIR}),
    OrchestrationRole.VERIFIER: frozenset({RoleMode.MODULE}),
}


def role_session_stage_key(
    scope_kind: str,
    subject_key: str,
    role: OrchestrationRole | str,
) -> str:
    """Return the stable identity of one role coroutine.

    A role mode selects the current playbook (for example ``produce`` versus
    ``repair``); it does not create a new logical coroutine.  Keeping mode out
    of this key is what lets one coder/verifier pair retain its state for the
    complete Module lifetime.
    """

    parts = (
        str(scope_kind or "").strip(),
        str(subject_key or "").strip(),
        OrchestrationRole(str(role)).value,
    )
    if not all(parts):
        raise ValueError("role session stage identity is incomplete")
    return ":".join(parts)


@dataclass(frozen=True)
class RoleActivation:
    role: OrchestrationRole
    mode: RoleMode

    def __post_init__(self) -> None:
        if self.mode not in ROLE_MODES[self.role]:
            raise ValueError(
                f"invalid orchestration role activation: {self.role.value}/{self.mode.value}"
            )

    @classmethod
    def from_values(
        cls,
        role: OrchestrationRole | str,
        mode: RoleMode | str,
    ) -> "RoleActivation":
        return cls(OrchestrationRole(str(role)), RoleMode(str(mode)))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RoleActivation":
        return cls.from_values(str(value.get("role") or ""), str(value.get("mode") or ""))

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role.value, "mode": self.mode.value}


REQUIRED_ORCHESTRATION_ROLES = frozenset(role.value for role in OrchestrationRole)
FAMILY_BINDING_SCHEMA_VERSION = "7"


def family_execution_adapter(value: Any) -> str:
    """Validate the Family-selected backend for the stable DAG access model."""

    adapter = str(value or "").strip()
    if not adapter:
        raise ValueError("family execution_adapter is required")
    return adapter


def validate_role_bindings(
    value: Mapping[str, Any],
) -> dict[str, MinionRoleBinding]:
    bindings = {
        str(role).strip(): (
            binding
            if isinstance(binding, MinionRoleBinding)
            else MinionRoleBinding.from_payload(str(role), binding)
        )
        for role, binding in dict(value or {}).items()
        if str(role).strip()
    }
    missing = sorted(REQUIRED_ORCHESTRATION_ROLES - set(bindings))
    extra = sorted(set(bindings) - REQUIRED_ORCHESTRATION_ROLES)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unknown " + ", ".join(extra))
        raise ValueError("family role_bindings must define exactly four roles: " + "; ".join(details))
    for role in (
        OrchestrationRole.ARCHITECT.value,
        OrchestrationRole.REVIEWER.value,
    ):
        if bindings[role].participant != "profile":
            raise ValueError(f"family role {role} requires a profile participant")
    implementation_participant = bindings[
        OrchestrationRole.IMPLEMENTATION.value
    ].participant
    verifier_participant = bindings[OrchestrationRole.VERIFIER.value].participant
    if implementation_participant != verifier_participant:
        raise ValueError(
            "family implementation and verifier participants must both be profile "
            "or both be null"
        )
    return {role: bindings[role] for role in sorted(bindings)}


def validate_family_binding_payload(
    value: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Validate the immutable, fully resolved FamilyBindingArtifact contract."""

    payload = dict(value or {})
    if str(payload.get("schema_version") or "") != FAMILY_BINDING_SCHEMA_VERSION:
        raise ValueError(
            "FamilyBindingArtifact must use schema_version "
            f"{FAMILY_BINDING_SCHEMA_VERSION}"
        )
    if "contract_schema" in payload:
        raise ValueError(
            "FamilyBindingArtifact must use architecture_definition, not "
            "the removed contract_schema binding"
        )
    architecture_definition = dict(
        payload.get("architecture_definition") or {}
    )
    for field in (
        "specialization_id",
        "family_id",
        "generation_hash",
        "schema_ref",
        "template_ref",
        "satellite_template_ref",
    ):
        value = architecture_definition.get(field)
        if not value:
            raise ValueError(
                "FamilyBindingArtifact architecture_definition requires "
                + field
            )
    if str(architecture_definition.get("family_id") or "") != str(
        payload.get("family_id") or ""
    ):
        raise ValueError(
            "FamilyBindingArtifact architecture definition belongs to a "
            "different family"
        )
    family_execution_adapter(payload.get("execution_adapter"))
    generation_hash = str(
        architecture_definition.get("generation_hash") or ""
    )
    if len(generation_hash) != 64 or any(
        character not in "0123456789abcdef"
        for character in generation_hash
    ):
        raise ValueError(
            "FamilyBindingArtifact architecture generation hash is invalid"
        )
    for field in ("schema_ref", "template_ref", "satellite_template_ref"):
        ref = architecture_definition.get(field)
        if not isinstance(ref, Mapping) or not str(
            ref.get("sha256") or ""
        ).strip():
            raise ValueError(
                "FamilyBindingArtifact architecture_definition "
                f"{field} must be a typed artifact ref"
            )
    raw_bindings = dict(payload.get("role_bindings") or {})
    missing = sorted(REQUIRED_ORCHESTRATION_ROLES - set(raw_bindings))
    extra = sorted(set(raw_bindings) - REQUIRED_ORCHESTRATION_ROLES)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unknown " + ", ".join(extra))
        raise ValueError(
            "FamilyBindingArtifact must resolve exactly four roles: "
            + "; ".join(details)
        )

    bindings: dict[str, dict[str, Any]] = {}
    for role in sorted(raw_bindings):
        raw = raw_bindings[role]
        if not isinstance(raw, Mapping):
            raise ValueError(
                f"FamilyBindingArtifact role {role} must be an object"
            )
        binding = dict(raw)
        participant = str(binding.get("participant") or "")
        if participant not in {"profile", "null"}:
            raise ValueError(
                f"FamilyBindingArtifact role {role} requires an explicit "
                "profile or null participant"
            )
        role_profile = dict(binding.get("role_profile") or {})
        reason = str(binding.get("reason") or "").strip()
        if participant == "profile":
            canonical_profile_id = str(
                role_profile.get("canonical_profile_id")
                or role_profile.get("minion_profile")
                or ""
            ).strip()
            role_protocol = dict(role_profile.get("role") or {})
            if not canonical_profile_id:
                raise ValueError(
                    f"FamilyBindingArtifact role {role} has no pinned role profile"
                )
            if "contract" in role_profile:
                raise ValueError(
                    f"FamilyBindingArtifact role {role} profile must not "
                    "select an architecture schema"
                )
            if str(role_protocol.get("kind") or "") != role:
                raise ValueError(
                    f"FamilyBindingArtifact role {role} has a mismatched role protocol"
                )
        elif role_profile or not reason:
            raise ValueError(
                f"FamilyBindingArtifact null role {role} requires a reason and "
                "must not pin a role profile"
            )
        bindings[role] = binding

    if bindings[OrchestrationRole.ARCHITECT.value]["participant"] != "profile":
        raise ValueError("FamilyBindingArtifact architect requires a profile participant")
    if bindings[OrchestrationRole.REVIEWER.value]["participant"] != "profile":
        raise ValueError("FamilyBindingArtifact reviewer requires a profile participant")
    if (
        bindings[OrchestrationRole.IMPLEMENTATION.value]["participant"]
        != bindings[OrchestrationRole.VERIFIER.value]["participant"]
    ):
        raise ValueError(
            "FamilyBindingArtifact implementation and verifier participants must match"
        )
    return bindings
