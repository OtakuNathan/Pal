from __future__ import annotations

from typing import Any

from pal.minion.utils import dict_from as _dict
from pal.minion.utils import string_list as _string_list
from pal.shared import TaskContextPack


EXECUTION_STRATEGY_VERSION = 1
PREPARE_GIT_WORKTREE = "git_worktree"
PREPARE_READ_ONLY_REPO = "read_only_repo"
PREPARE_ARTIFACT_WORKSPACE = "artifact_workspace"


def normalize_execution_strategy(
    explicit: dict[str, Any] | None = None,
    *,
    workspace_policy: dict[str, Any] | None = None,
    completion_policy: dict[str, Any] | None = None,
    gate_policy: dict[str, Any] | None = None,
    output_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    explicit_strategy = _dict(explicit)
    workspace = _dict(workspace_policy)
    completion = _dict(completion_policy)
    gate = _dict(gate_policy)
    output = _dict(output_policy)

    mode = _lower(workspace.get("mode"))
    evidence = _lower(completion.get("evidence"))
    submits_review_gate = bool(gate.get("submits_review_gate") or output.get("requires_review_gate"))

    prepare = _stage(explicit_strategy, "prepare")
    repair_loop = _stage(explicit_strategy, "repair_loop") or _stage(explicit_strategy, "repair")
    gate_stage = _stage(explicit_strategy, "gate")
    gates = _string_list(gate.get("gates")) or _string_list(gate_stage.get("gates") or explicit_strategy.get("gates"))
    normalized_gates = [_lower(item) for item in gates]

    prepare_kind = _normalize_prepare_kind(
        prepare.get("kind")
        or explicit_strategy.get("prepare_kind")
        or explicit_strategy.get("workspace_kind")
        or _inferred_prepare_kind(mode=mode, evidence=evidence)
    )
    repair_kind = _normalize_repair_kind(
        repair_loop.get("kind")
        or explicit_strategy.get("repair_loop_kind")
        or _inferred_repair_kind(evidence=evidence, gates=normalized_gates, submits_review_gate=submits_review_gate)
    )
    gate_kind = _normalize_gate_kind(
        gate_stage.get("kind")
        or explicit_strategy.get("gate_kind")
        or _inferred_gate_kind(gates=normalized_gates, submits_review_gate=submits_review_gate)
    )

    return {
        "version": EXECUTION_STRATEGY_VERSION,
        "prepare": {
            **{key: value for key, value in prepare.items() if key != "kind"},
            "kind": prepare_kind,
            "workspace_policy_mode": mode or "none",
        },
        "repair_loop": {
            **{key: value for key, value in repair_loop.items() if key != "kind"},
            "kind": repair_kind,
            "enabled": repair_kind != "none",
        },
        "gate": {
            **{key: value for key, value in gate_stage.items() if key != "kind"},
            "kind": gate_kind,
            "gates": gates,
        },
    }


def execution_strategy_from_pack(
    pack: TaskContextPack,
    *,
    workspace_policy: dict[str, Any] | None = None,
    completion_policy: dict[str, Any] | None = None,
    gate_policy: dict[str, Any] | None = None,
    output_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    workspace = dict(pack.workspace or {})
    profile = dict(pack.resolved_profile or {})
    explicit = _dict(workspace.get("execution_strategy") or workspace.get("execution_policy"))
    if not explicit:
        explicit = _dict(profile.get("effective_execution_strategy") or profile.get("execution_strategy") or profile.get("execution_policy"))
    return normalize_execution_strategy(
        explicit,
        workspace_policy=workspace_policy,
        completion_policy=completion_policy,
        gate_policy=gate_policy,
        output_policy=output_policy,
    )


def prepare_kind(strategy: dict[str, Any] | None) -> str:
    return _normalize_prepare_kind(_dict(_dict(strategy).get("prepare")).get("kind"))


def merge_execution_strategy(base: dict[str, Any] | None, override: dict[str, Any] | None) -> dict[str, Any]:
    return _deep_merge_dict(_dict(base), _dict(override))


def _stage(strategy: dict[str, Any], key: str) -> dict[str, Any]:
    value = strategy.get(key)
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        return {"kind": value}
    return {}


def _inferred_prepare_kind(*, mode: str, evidence: str) -> str:
    if mode in {"writable_git_branch", "git_worktree", "git"} or evidence == "git_commit":
        return PREPARE_GIT_WORKTREE
    if mode == "read_only_repo":
        return PREPARE_READ_ONLY_REPO
    return PREPARE_ARTIFACT_WORKSPACE


def _inferred_repair_kind(*, evidence: str, gates: list[str], submits_review_gate: bool) -> str:
    if evidence == "git_commit" or any(gate in {"checkpoint_admission", "checkpoint_quality", "module_quality"} for gate in gates):
        return "checkpoint_repair"
    if "plan_acceptance" in gates:
        return "plan_revision"
    if submits_review_gate or any(gate and gate != "none" for gate in gates):
        return "gate_revision"
    return "none"


def _inferred_gate_kind(*, gates: list[str], submits_review_gate: bool) -> str:
    if not gates or gates == ["none"]:
        return "review_gate" if submits_review_gate else "none"
    if "plan_acceptance" in gates:
        return "plan_acceptance"
    if submits_review_gate:
        return "review_gate"
    return "configured_gates"


def _normalize_prepare_kind(value: Any) -> str:
    raw = _lower(value)
    if raw in {"writable_git_branch", "git", "git_repo", "git_branch", "git_worktree"}:
        return PREPARE_GIT_WORKTREE
    if raw in {"readonly_repo", "read_only", "read_only_repo", "source_repo"}:
        return PREPARE_READ_ONLY_REPO
    if raw in {"", "none", "folder", "workspace_folder", "artifact", "artifact_workspace", "deliverable_workspace"}:
        return PREPARE_ARTIFACT_WORKSPACE
    return raw


def _normalize_repair_kind(value: Any) -> str:
    raw = _lower(value)
    if raw in {"", "none", "disabled", "off", "false"}:
        return "none"
    if raw in {"checkpoint", "checkpoint_repair", "coder_checkpoint"}:
        return "checkpoint_repair"
    if raw in {"plan", "plan_revision", "planner_revision"}:
        return "plan_revision"
    if raw in {"gate", "gate_revision", "review_gate"}:
        return "gate_revision"
    return raw


def _normalize_gate_kind(value: Any) -> str:
    raw = _lower(value)
    if raw in {"", "none", "disabled", "off", "false"}:
        return "none"
    if raw in {"plan", "plan_acceptance"}:
        return "plan_acceptance"
    if raw in {"review", "review_gate"}:
        return "review_gate"
    if raw in {"configured", "configured_gates", "gates"}:
        return "configured_gates"
    return raw


def _deep_merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge_dict(dict(result[key]), dict(value))
        else:
            result[key] = value
    return result


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()
