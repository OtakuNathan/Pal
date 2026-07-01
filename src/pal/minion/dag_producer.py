from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pal.foundation import utc_now
from pal.minion.profiles import MinionProfileRegistry
from pal.minion.workflow import canonical_profile_ref
from pal.minion.work_order import new_work_id


SOFTWARE_ENGINEERING_FAMILY = "software_engineering"
DEFAULT_GENERIC_EXECUTOR_PROFILE = "general.generic"


@dataclass(frozen=True)
class ExecutorResolution:
    executor_profile: str
    source: str


def dag_producer_profile_for_family(profile_family: str) -> str:
    family = _profile_ref_text(profile_family)
    if family == SOFTWARE_ENGINEERING_FAMILY:
        return "software_engineering.architect"
    return ""


def resolve_default_executor_profile(
    *,
    profile_family: str,
    registry: MinionProfileRegistry,
    task_metadata: dict[str, Any] | None = None,
    workflow_metadata: dict[str, Any] | None = None,
) -> ExecutorResolution:
    family = _profile_ref_text(profile_family) or "general"
    for source_name, metadata in (
        ("task_metadata", dict(task_metadata or {})),
        ("workflow_metadata", dict(workflow_metadata or {})),
    ):
        candidate = _executor_profile_from_mapping(metadata)
        if candidate:
            return ExecutorResolution(_canonical_executor(candidate, profile_family=family), source_name)

    if family == "general":
        return ExecutorResolution(DEFAULT_GENERIC_EXECUTOR_PROFILE, "builtin_general_generic")

    family_generic = registry.get_ref(family, "generic")
    if family_generic is not None:
        return ExecutorResolution(family_generic.canonical_profile_id, "family_generic_profile")

    family_profiles = [
        profile
        for profile in registry.list_profiles()
        if _profile_ref_text(profile.profile_group) == family
    ]
    non_system_profiles = [
        profile
        for profile in family_profiles
        if str(profile.profile_id or "").strip() not in {"architect", "reviewer", "review_worker", "coder"}
    ]
    if len(non_system_profiles) == 1:
        return ExecutorResolution(non_system_profiles[0].canonical_profile_id, "single_family_executor_profile")
    if len(family_profiles) == 1:
        return ExecutorResolution(family_profiles[0].canonical_profile_id, "single_family_profile")

    return ExecutorResolution(DEFAULT_GENERIC_EXECUTOR_PROFILE, "generic_executor_fallback")


def build_generic_single_node_plan_artifact(
    *,
    goal: str,
    task_id: str,
    profile_family: str,
    requirements_brief: dict[str, Any],
    workspace: dict[str, Any],
    executor_profile: str,
    title: str = "",
) -> dict[str, Any]:
    family = _profile_ref_text(profile_family) or "general"
    resolved_task_id = str(task_id or new_work_id("task")).strip()
    resolved_executor = _canonical_executor(executor_profile, profile_family=family)
    milestones = _generic_milestones(goal=goal, requirements_brief=requirements_brief)
    return {
        "type": "FinalPlanArtifact",
        "plan_id": new_work_id("plan"),
        "task_id": resolved_task_id,
        "summary": str(title or goal or "Execute task").strip(),
        "modules": [
            {
                "module_id": "main",
                "owned_area": [f"task://{resolved_task_id}/main"],
                "responsibility": "Execute the prepared requirements as one bounded deliverable.",
                "internal_milestones": milestones,
                "test_plan": {
                    "strategy": "Verify the deliverable against the user goal, requirements brief, and milestone acceptance criteria.",
                },
                "metadata": {
                    "profile_family": family,
                    "executor_profile": resolved_executor,
                    "dag_producer": "generic_single_node",
                },
            }
        ],
        "orchestration": {
            "execution_shape": "fork_join_linear",
            "topology": {
                "nodes": [
                    {
                        "node_id": "main",
                        "kind": "module",
                        "module_id": "main",
                        "depends_on": [],
                        "executor_profile": resolved_executor,
                    }
                ],
                "order": ["main"],
            },
        },
        "system_test_plan": [
            {
                "level": "deliverable",
                "evidence": "Executor reports milestone evidence, blockers, and remaining risks.",
            }
        ],
        "risks": [
            {
                "risk": "Generic DAG producer may under-specify domain-specific work.",
                "mitigation": "Executor must report blockers and missing user-owned facts explicitly.",
            }
        ],
        "metadata": {
            "profile_family": family,
            "dag_producer": {
                "kind": "generic_single_node",
                "created_at": utc_now(),
            },
            "workflow_next": {"profile": resolved_executor},
            "requirements_brief": dict(requirements_brief or {}),
            "workspace_summary": _workspace_summary(workspace),
        },
    }


def _generic_milestones(*, goal: str, requirements_brief: dict[str, Any]) -> list[dict[str, Any]]:
    supplied = requirements_brief.get("milestones") if isinstance(requirements_brief, dict) else None
    if isinstance(supplied, list) and supplied:
        result: list[dict[str, Any]] = []
        for index, item in enumerate(supplied):
            if isinstance(item, dict):
                title = str(item.get("title") or item.get("summary") or item.get("task") or f"Milestone {index}").strip()
                task = str(item.get("task") or item.get("summary") or title).strip()
                acceptance = _string_list(item.get("acceptance_criteria") or item.get("acceptance"))
            else:
                title = str(item or f"Milestone {index}").strip()
                task = title
                acceptance = []
            result.append(
                {
                    "milestone_id": f"m{index}",
                    "title": title or f"Milestone {index}",
                    "task": task or title or "Complete milestone.",
                    "acceptance_criteria": acceptance or ["Milestone result is concrete and tied to the task goal."],
                }
            )
        return result

    goal_text = str(goal or "").strip() or "Complete the requested task."
    return [
        {
            "milestone_id": "m0",
            "title": "Analyze requirements",
            "task": "Read the goal, requirements brief, and workspace facts; identify scope, blockers, and success criteria.",
            "acceptance_criteria": [
                "Scope and success criteria are restated concretely.",
                "User-owned blockers or missing facts are surfaced explicitly.",
            ],
        },
        {
            "milestone_id": "m1",
            "title": "Gather context",
            "task": "Collect the information, source context, or domain evidence needed to complete the deliverable.",
            "acceptance_criteria": [
                "Relevant context is summarized with evidence or source locations when available.",
                "Irrelevant or unavailable context is not invented.",
            ],
        },
        {
            "milestone_id": "m2",
            "title": "Produce deliverable",
            "task": goal_text,
            "acceptance_criteria": [
                "The deliverable directly satisfies the user goal.",
                "The result follows constraints from the requirements brief.",
            ],
        },
        {
            "milestone_id": "m3",
            "title": "Verify and summarize",
            "task": "Check the deliverable against the acceptance criteria and summarize evidence, blockers, and residual risk.",
            "acceptance_criteria": [
                "Every milestone acceptance criterion is addressed.",
                "Remaining risk or follow-up is stated plainly.",
            ],
        },
    ]


def _executor_profile_from_mapping(value: dict[str, Any]) -> str:
    for key in ("executor_profile", "default_executor_profile", "minion_profile", "profile"):
        raw = str(value.get(key) or "").strip()
        if raw:
            return raw
    dag_producer = value.get("dag_producer")
    if isinstance(dag_producer, dict):
        nested = _executor_profile_from_mapping(dict(dag_producer))
        if nested:
            return nested
    executor = value.get("executor")
    if isinstance(executor, dict):
        nested = _executor_profile_from_mapping(dict(executor))
        if nested:
            return nested
        group = str(executor.get("profile_group") or executor.get("group") or "").strip()
        name = str(executor.get("profile_name") or executor.get("name") or "").strip()
        if group or name:
            return canonical_profile_ref(profile_group=group, profile_name=name or "generic")
    return ""


def _canonical_executor(value: str, *, profile_family: str) -> str:
    raw = str(value or "").strip().replace("/", ".")
    if not raw:
        return DEFAULT_GENERIC_EXECUTOR_PROFILE
    if "." in raw:
        return raw
    return canonical_profile_ref(profile_group=profile_family or "general", profile_name=raw)


def _workspace_summary(workspace: dict[str, Any]) -> dict[str, Any]:
    data = dict(workspace or {})
    return {
        "kind": str(data.get("kind") or ""),
        "project_name": str(data.get("project_name") or ""),
        "repo_path": str(data.get("repo_path") or ""),
        "primary_language": str(data.get("primary_language") or ""),
        "languages": _string_list(data.get("languages")),
    }


def _profile_ref_text(value: Any) -> str:
    return str(value or "").strip().replace("/", ".")


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = " ".join(str(item or "").split())
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
