from __future__ import annotations

from typing import Any

from pal.foundation import utc_now
from pal.minion.utils import string_list as _string_list
from pal.shared import TaskContextPack


NONE_PROFILE = "none"
DEFAULT_ARCHITECT_PROFILE = "software_engineering.architect"


def canonical_profile_ref(profile_group: str = "", profile_name: str = "", profile: str = "") -> str:
    explicit = str(profile or "").strip().replace("/", ".")
    if explicit:
        return explicit
    group = str(profile_group or "general").strip().replace("/", ".") or "general"
    name = str(profile_name or "generic").strip() or "generic"
    return name if group == "general" else f"{group}.{name}"


def split_profile_ref(profile: str) -> tuple[str, str]:
    normalized = str(profile or "").strip().replace("/", ".")
    if not normalized or normalized == NONE_PROFILE:
        return ("", NONE_PROFILE)
    if "." not in normalized:
        return ("general", normalized)
    group, name = normalized.rsplit(".", 1)
    return (group or "general", name or "generic")


def effective_output_policy(pack: TaskContextPack) -> dict[str, Any]:
    profile = dict(pack.resolved_profile or {})
    for key in ("effective_output_policy", "output_policy"):
        value = profile.get(key)
        if isinstance(value, dict):
            return dict(value)
    workspace_policy = pack.workspace.get("output_policy") if isinstance(pack.workspace, dict) else {}
    return dict(workspace_policy or {}) if isinstance(workspace_policy, dict) else {}


def workflow_next_policy(pack: TaskContextPack) -> dict[str, Any]:
    output_policy = effective_output_policy(pack)
    value = output_policy.get("workflow_next") or output_policy.get("next")
    if isinstance(value, dict):
        policy = dict(value)
    else:
        policy = {}
    for legacy_key in ("artifact_type", "default_next_profile", "next_profile", "allowed_next_profiles", "adapter"):
        if legacy_key in output_policy and legacy_key not in policy:
            policy[legacy_key] = output_policy[legacy_key]
    return policy


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _artifact_declared_next(payload: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for key in ("workflow_next", "next"):
        value = payload.get(key)
        if isinstance(value, dict):
            candidates.append(dict(value))
    for key in ("plan_artifact", "artifact", "output_artifact", "primary_artifact"):
        artifact = _mapping(payload.get(key))
        if not artifact:
            continue
        for nested_key in ("workflow_next", "next"):
            value = artifact.get(nested_key)
            if isinstance(value, dict):
                candidates.append(dict(value))
        metadata = _mapping(artifact.get("metadata"))
        for nested_key in ("workflow_next", "next"):
            value = metadata.get(nested_key)
            if isinstance(value, dict):
                candidates.append(dict(value))
        if str(artifact.get("next_profile") or "").strip():
            candidates.append({"profile": artifact.get("next_profile")})
    if str(payload.get("next_profile") or "").strip():
        candidates.append({"profile": payload.get("next_profile")})
    return candidates[0] if candidates else {}


def _next_profile_from_declaration(declaration: dict[str, Any]) -> str:
    requested = declaration.get("profile") or declaration.get("next_profile")
    if not requested and isinstance(declaration.get("next"), dict):
        requested = dict(declaration.get("next") or {}).get("profile")
    return str(requested or "").strip()


def resolve_workflow_next(pack: TaskContextPack, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = workflow_next_policy(pack)
    payload = dict(payload or {})
    default_next = canonical_profile_ref(profile=str(policy.get("default_next_profile") or policy.get("next_profile") or NONE_PROFILE))
    declaration = _artifact_declared_next(payload)
    requested = _next_profile_from_declaration(declaration) or default_next
    next_profile = canonical_profile_ref(profile=str(requested or NONE_PROFILE))
    allowed = _string_list(policy.get("allowed_next_profiles"))
    if not allowed:
        allowed = [default_next, NONE_PROFILE]
    allowed = [canonical_profile_ref(profile=item) for item in allowed]
    if NONE_PROFILE not in allowed:
        allowed.append(NONE_PROFILE)
    if next_profile not in allowed:
        return {
            "status": "invalid",
            "reason": "next_profile_not_allowed",
            "next_profile": next_profile,
            "allowed_next_profiles": allowed,
            "policy": policy,
            "declaration": declaration,
        }
    adapter = str(declaration.get("adapter") or policy.get("adapter") or "").strip()
    artifact_type = str(declaration.get("artifact_type") or policy.get("artifact_type") or "").strip()
    return {
        "status": "ok",
        "next_profile": next_profile,
        "adapter": adapter,
        "artifact_type": artifact_type,
        "allowed_next_profiles": allowed,
        "policy": policy,
        "declaration": declaration,
        "source": "artifact" if declaration else "profile_default",
    }


def workflow_step_started(
    *,
    step_id: str,
    profile: str,
    input_artifact: dict[str, Any] | None = None,
    adapter: str = "",
) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "profile": canonical_profile_ref(profile=profile),
        "status": "running",
        "adapter": str(adapter or "").strip(),
        "input_artifact": dict(input_artifact or {}),
        "started_at": utc_now(),
    }


def workflow_step_completed(
    step: dict[str, Any],
    *,
    output_artifact: dict[str, Any] | None = None,
    next_profile: str = NONE_PROFILE,
    status: str = "completed",
) -> dict[str, Any]:
    updated = dict(step or {})
    updated.update(
        {
            "status": str(status or "completed").strip() or "completed",
            "output_artifact": dict(output_artifact or {}),
            "next_profile": canonical_profile_ref(profile=next_profile or NONE_PROFILE),
            "completed_at": utc_now(),
        }
    )
    return updated


def update_current_workflow_step(
    workflow: dict[str, Any],
    *,
    status: str,
    output_artifact: dict[str, Any] | None = None,
    next_profile: str = NONE_PROFILE,
) -> dict[str, Any]:
    updated = dict(workflow or {})
    steps = [dict(item) for item in list(updated.get("steps") or []) if isinstance(item, dict)]
    current_step_id = str(updated.get("current_step_id") or "").strip()
    for index, step in enumerate(steps):
        if str(step.get("step_id") or "") == current_step_id:
            steps[index] = workflow_step_completed(
                step,
                output_artifact=output_artifact,
                next_profile=next_profile,
                status=status,
            )
            break
    updated["steps"] = steps
    updated["updated_at"] = utc_now()
    return updated


def append_workflow_step(
    workflow: dict[str, Any],
    *,
    profile: str,
    input_artifact: dict[str, Any] | None = None,
    adapter: str = "",
) -> dict[str, Any]:
    updated = dict(workflow or {})
    steps = [dict(item) for item in list(updated.get("steps") or []) if isinstance(item, dict)]
    step_id = f"step_{len(steps)}"
    steps.append(workflow_step_started(step_id=step_id, profile=profile, input_artifact=input_artifact, adapter=adapter))
    updated["steps"] = steps
    updated["current_step_id"] = step_id
    updated["current_profile"] = canonical_profile_ref(profile=profile)
    updated["status"] = "running"
    updated["updated_at"] = utc_now()
    return updated
