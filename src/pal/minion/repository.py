from __future__ import annotations

import hashlib
import json
import sqlite3
import contextlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from pal.foundation import utc_now
from pal.minion.config import minion_db_path
from pal.minion.contracts import (
    SERIAL_MILESTONE_MODES,
    SERIAL_MODULE_MILESTONES_MODE,
)
from pal.minion.dag_advancer import (
    affected_modules_for_repair as _affected_modules_for_repair,
    apply_repair_replay as _dag_apply_repair_replay,
    build_module_dag_from_validation as _module_dag_from_validation,
    claim_ready_modules as _dag_claim_ready_modules,
    dag_state_to_runtime_dict as _dag_state_to_runtime_dict,
    dag_state_to_storage_dict as _dag_state_to_storage_dict,
    complete_module as _dag_complete_module,
    module_dag_status as _module_dag_status,
    module_kind_from_validation as _plan_module_kind,
    ready_module_ids as _dag_ready_module_ids,
    release_running_module as _dag_release_running_module,
    resume_blocked_module as _dag_resume_blocked_module,
)
from pal.minion.debug_log import minion_debug_log_enabled
from pal.minion.git_env import publish_completed_plan_workspace, prepare_dependency_integration_baseline
from pal.minion.ledger_store import MinionLedgerStore
from pal.minion.plan_store import MinionPlanStore
from pal.minion.prompt_adapter import prompt_scaffold_summary as _prompt_scaffold_summary
from pal.minion.review_gate_store import MinionReviewGateStore
from pal.minion.schema import ensure_minion_schema
from pal.minion.search_store import MinionSearchStore
from pal.minion.work_order import (
    PlanArtifact,
    ReviewGateResult,
    build_planner_work_order,
    compile_coder_work_order,
    dispatchable_plan_validation,
    module_milestone_records,
    new_work_id,
    plan_module_order_for_execution,
    plan_milestone_id_at,
    plan_module_id_at,
    prompt_view_for_coder,
    prompt_view_from_metadata,
    validate_dispatchable_plan_artifact,
    validate_final_plan_artifact,
)
from pal.minion.turns import build_minion_turn_from_pack
from pal.minion.validation import normalize_milestones
from pal.minion.workflow import NONE_PROFILE, canonical_profile_ref_for_family, update_current_workflow_step
from pal.shared import TaskContextPack
from pal.shared.text_search import jieba_fts_text


ACTIVE_WORK_ORDER_STATUSES = ("active", "running", "blocked", "approval_pending")
RUNNING_WORK_ORDER_STATUSES = ("active", "running", "approval_pending", "clarification_pending")
_CONTINUITY_LEDGER_LIMIT = 20
_CONTINUITY_TEXT_LIMIT = 500
_WORK_ORDER_DRAFT_TEXT_LIMIT = 4000
_WORK_ORDER_DRAFT_ITEM_TEXT_LIMIT = 1000
_WORK_ORDER_DRAFT_METADATA_VALUE_LIMIT = 12000
_RUN_WORKSPACE_KEYS = {"run_dir", "artifact_dir", "artifact_stage_dir", "log_dir"}
_PROFILE_SCOPED_WORKSPACE_KEYS = {
    "checkpoint_policy",
    "workspace_policy",
    "workspace_environment",
    "workspace_environment_policy",
    "completion_policy",
    "gate_policy",
    "output_policy",
    "execution_contract",
    "execution_strategy",
    "execution_policy",
}
_RAW_WORK_ORDER_METADATA_KEY_PARTS = ("payload", "raw", "transcript", "messages", "full_context", "conversation")
_REPAIR_BILL_REPLAY_DEFECT_KINDS = frozenset({"module_defect", "contract_defect"})
_REPAIR_BILL_NON_REPLAY_DEFECT_KINDS = frozenset({"integration_defect", "architecture_defect", "triage_required"})
_REPAIR_BILL_DEFECT_KINDS = _REPAIR_BILL_REPLAY_DEFECT_KINDS | _REPAIR_BILL_NON_REPLAY_DEFECT_KINDS
_MODULE_ADAPTER_PROMPT_VIEW = "prompt_view"
_MODULE_ADAPTER_CODER_WORK_ORDER = "coder_work_order"
_LEGACY_MINION_COPY_TABLES = (
    "minion_tasks",
    "minion_work_orders",
    "minion_work_order_drafts",
    "minion_work_order_milestones",
    "minion_worker_checkpoints",
    "minion_review_gates",
    "minion_worker_ledger",
    "minion_task_lessons",
    "minion_system_lesson_candidates",
    "minion_runtime_settings",
)


def _profile_ref_parts_from_canonical(value: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    if raw and "." in raw:
        group, name = raw.rsplit(".", 1)
        return group.strip() or "general", name.strip() or "generic"
    if raw:
        return "general", raw
    return "general", "generic"


def _profile_ref_text(value: Any) -> str:
    return str(value or "").strip().replace("/", ".")


def _canonical_profile_id(group: str, name: str) -> str:
    resolved_group = str(group or "general").strip().replace("/", ".") or "general"
    resolved_name = str(name or "generic").strip() or "generic"
    return resolved_name if resolved_group == "general" else f"{resolved_group}.{resolved_name}"


def _executor_profile_value(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("profile", "executor_profile", "minion_profile", "dispatch_profile", "next_profile"):
            raw = str(value.get(key) or "").strip()
            if raw:
                return raw
        group = str(value.get("profile_group") or value.get("group") or "").strip()
        name = str(value.get("profile_name") or value.get("name") or "").strip()
        if group or name:
            return _canonical_profile_id(group, name)
        return ""
    return str(value or "").strip()


def _executor_profile_from_mapping(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("executor_profile", "dispatch_profile", "minion_profile"):
        raw = _executor_profile_value(value.get(key))
        if raw:
            return raw
    return _executor_profile_value(value.get("executor"))


def _canonical_executor_profile(value: Any, *, default_profile: str) -> str:
    fallback = str(default_profile or "general.generic").strip().replace("/", ".") or "general.generic"
    raw = _executor_profile_value(value).replace("/", ".")
    if not raw or raw.lower() in {"default", "inherit", NONE_PROFILE}:
        return fallback
    if "." not in raw:
        fallback_group, _ = _profile_ref_parts_from_canonical(fallback)
        if fallback_group and fallback_group != "general":
            return _canonical_profile_id(fallback_group, raw)
    return raw


def _profile_family_from_metadata(metadata: dict[str, Any], *, fallback_group: str = "", fallback_profile: str = "") -> str:
    data = dict(metadata or {})
    workflow = dict(data.get("workflow") or {})
    fallback_profile_group = _profile_ref_parts_from_canonical(fallback_profile)[0] if fallback_profile else ""
    for value in (
        data.get("profile_family"),
        workflow.get("profile_family"),
        workflow.get("default_profile_group"),
        fallback_group,
        fallback_profile_group,
    ):
        raw = str(value or "").strip().replace("/", ".")
        if raw:
            return raw
    return "general"


def _profile_from_group_name(group: Any, name: Any, *, profile_family: str) -> str:
    resolved_group = str(group or "").strip().replace("/", ".")
    resolved_name = str(name or "").strip()
    if not (resolved_group or resolved_name):
        return ""
    if resolved_group:
        return _canonical_profile_id(resolved_group, resolved_name or "generic")
    return canonical_profile_ref_for_family(resolved_name, profile_family=profile_family)


def _plan_artifact_workflow_next_profile(artifact: PlanArtifact) -> str:
    metadata = dict(artifact.metadata or {})
    workflow_next = metadata.get("workflow_next") or metadata.get("next")
    if isinstance(workflow_next, dict):
        raw = workflow_next.get("profile") or workflow_next.get("next_profile")
        if not raw and isinstance(workflow_next.get("next"), dict):
            raw = dict(workflow_next.get("next") or {}).get("profile")
        return str(raw or "").strip()
    return str(metadata.get("next_profile") or "").strip()


def _dispatch_profile_from_plan_context(
    artifact: PlanArtifact,
    metadata: dict[str, Any],
    *,
    profile_group: str = "",
    profile_name: str = "",
) -> tuple[str, str, str]:
    profile_family = _profile_family_from_metadata(metadata, fallback_group=profile_group)
    explicit_profile = _profile_from_group_name(profile_group, profile_name, profile_family=profile_family)
    if explicit_profile:
        resolved_profile = explicit_profile
    else:
        resolved_profile = _profile_from_group_name(
            metadata.get("dispatch_profile_group"),
            metadata.get("dispatch_profile_name"),
            profile_family=profile_family,
        )
    if not resolved_profile:
        plan_execution = dict(metadata.get("plan_execution") or {})
        dag_execution = dict(plan_execution.get("dag_execution") or {})
        resolved_profile = str(dag_execution.get("default_executor_profile") or "").strip()
    if not resolved_profile:
        declared_next = _plan_artifact_workflow_next_profile(artifact)
        if declared_next and declared_next != NONE_PROFILE:
            resolved_profile = canonical_profile_ref_for_family(declared_next, profile_family=profile_family)
    if not resolved_profile or resolved_profile == NONE_PROFILE:
        raise ValueError("plan parent pack requires an explicit DAG executor profile")
    resolved_profile = _canonical_executor_profile(resolved_profile, default_profile=resolved_profile)
    resolved_group, resolved_name = _profile_ref_parts_from_canonical(resolved_profile)
    return resolved_group, resolved_name, resolved_profile


def _execution_contract_from_pack(
    pack: TaskContextPack,
    *,
    metadata: dict[str, Any] | None = None,
    workspace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    profile = dict(pack.resolved_profile or {})
    for key in ("execution_contract", "effective_execution_contract"):
        value = profile.get(key)
        if isinstance(value, dict):
            result.update(dict(value))
    profile_metadata = profile.get("metadata")
    if isinstance(profile_metadata, dict) and isinstance(profile_metadata.get("execution_contract"), dict):
        result.update(dict(profile_metadata.get("execution_contract") or {}))
    workspace_data = dict(workspace if workspace is not None else pack.workspace or {})
    if isinstance(workspace_data.get("execution_contract"), dict):
        result.update(dict(workspace_data.get("execution_contract") or {}))
    metadata_data = dict(metadata if metadata is not None else pack.metadata or {})
    if isinstance(metadata_data.get("execution_contract"), dict):
        result.update(dict(metadata_data.get("execution_contract") or {}))
    return result


def _module_adapter_from_contract(contract: dict[str, Any]) -> str:
    raw = str(
        dict(contract or {}).get("module_adapter")
        or dict(contract or {}).get("task_adapter")
        or dict(contract or {}).get("adapter")
        or ""
    ).strip().lower()
    normalized = raw.replace("-", "_").replace(".", "_")
    if normalized in {"coder", "coder_work_order", "software_coder_work_order"}:
        return _MODULE_ADAPTER_CODER_WORK_ORDER
    if normalized in {"", "none", "generic", "prompt", "prompt_view", "plan_milestone", "plan_milestone_prompt_view"}:
        return _MODULE_ADAPTER_PROMPT_VIEW
    return normalized


def _module_role_from_contract(contract: dict[str, Any], *, fallback_profile: str = "") -> str:
    data = dict(contract or {})
    role = str(
        data.get("module_role")
        or data.get("executor_role")
        or data.get("role")
        or data.get("artifact_role")
        or ""
    ).strip()
    if role:
        return role
    return _role_from_profile(fallback_profile)


def _module_dispatch_action_from_contract(contract: dict[str, Any], *, fallback_profile: str = "") -> dict[str, str]:
    role = _module_role_from_contract(contract, fallback_profile=fallback_profile).lower()
    if role == "coder":
        return {"verb": "Implement", "noun": "implementation"}
    if role in {"reviewer", "review_worker", "reviewer_worker"}:
        return {"verb": "Review", "noun": "review"}
    if role == "writer":
        return {"verb": "Write", "noun": "writing"}
    return {"verb": "Execute", "noun": "execution"}


def _module_executor_profiles(
    artifact: PlanArtifact,
    validation: dict[str, Any],
    *,
    default_profile: str,
    existing: dict[str, Any] | None = None,
) -> dict[str, str]:
    default_profile = _canonical_executor_profile(default_profile, default_profile=default_profile)
    existing = dict(existing or {})
    existing_executors = dict(existing.get("node_executors") or existing.get("module_executors") or {})
    node_executor_by_module: dict[str, str] = {}
    for node in list(dict(validation or {}).get("nodes") or []):
        if not isinstance(node, dict):
            continue
        module_id = str(node.get("module_id") or "").strip()
        executor = _executor_profile_from_mapping(node)
        if module_id and executor:
            node_executor_by_module[module_id] = executor
    result: dict[str, str] = {}
    for module in artifact.modules:
        module_id = str(module.module_id or "").strip()
        if not module_id:
            continue
        module_executor = _executor_profile_from_mapping(dict(module.metadata or {}))
        executor = (
            node_executor_by_module.get(module_id)
            or module_executor
            or existing_executors.get(module_id)
            or default_profile
        )
        result[module_id] = _canonical_executor_profile(executor, default_profile=default_profile)
    return result


def _dag_execution_metadata(default_executor_profile: str, node_executors: dict[str, str]) -> dict[str, Any]:
    default_profile = _canonical_executor_profile(default_executor_profile, default_profile=default_executor_profile)
    return {
        "default_executor_profile": default_profile,
        "node_executors": {
            str(module_id): _canonical_executor_profile(profile, default_profile=default_profile)
            for module_id, profile in dict(node_executors or {}).items()
            if str(module_id or "").strip()
        },
    }


def _plan_execution_dag_state(plan_execution: dict[str, Any]) -> dict[str, Any]:
    return _dag_state_to_runtime_dict(
        dict(plan_execution.get("dag_state") or plan_execution.get("module_dag") or {})
    )


def _set_plan_execution_dag_state(plan_execution: dict[str, Any], dag: dict[str, Any]) -> None:
    plan_execution["dag_state"] = _dag_state_to_storage_dict(dag)
    plan_execution.pop("module_dag", None)


def _set_dag_node_cursor(dag: dict[str, Any], module_id: str, cursor: dict[str, Any] | None) -> dict[str, Any]:
    updated = dict(dag or {})
    resolved_module_id = str(module_id or "").strip()
    if not resolved_module_id:
        return updated
    cursors = {
        str(key): dict(value)
        for key, value in dict(updated.get("module_cursors") or updated.get("node_cursors") or {}).items()
        if str(key or "").strip() and isinstance(value, dict)
    }
    if cursor:
        cursors[resolved_module_id] = dict(cursor)
    else:
        cursors.pop(resolved_module_id, None)
    updated["module_cursors"] = cursors
    return updated


def _claim_known_child_for_completion(
    dag: dict[str, Any],
    plan_execution: dict[str, Any],
    *,
    module_id: str,
    child_work_order_id: str,
) -> dict[str, Any]:
    """Let a valid resumed child close a node whose old running claim was released."""

    resolved_module_id = str(module_id or "").strip()
    child_id = str(child_work_order_id or "").strip()
    if not resolved_module_id or not child_id:
        return dag
    known_child_ids = dict(plan_execution.get("child_work_order_ids") or {})
    if str(known_child_ids.get(resolved_module_id) or "").strip() != child_id:
        return dag
    module_order = _coerce_text_list(dag.get("module_order"))
    if resolved_module_id not in module_order:
        return dag
    running_modules = dict(dag.get("running_modules") or {})
    current_child_id = str(running_modules.get(resolved_module_id) or "").strip()
    if current_child_id:
        return dag
    module_status = dict(dag.get("module_status") or {})
    current_status = str(module_status.get(resolved_module_id) or "").strip().lower()
    if current_status not in {"", "ready", "blocked", "stale"}:
        return dag
    remaining_indegree = dict(dag.get("remaining_indegree") or {})
    if int(_coerce_int(remaining_indegree.get(resolved_module_id)) or 0) != 0:
        return dag
    updated = dict(dag)
    running_modules[resolved_module_id] = child_id
    module_status[resolved_module_id] = "running"
    updated["running_modules"] = running_modules
    updated["module_status"] = module_status
    updated["ready_modules"] = [
        item for item in _coerce_text_list(updated.get("ready_modules")) if item != resolved_module_id
    ]
    return updated


def _module_status_items_from_metadata(
    metadata: dict[str, Any],
    milestones: list[dict[str, Any]] | None = None,
    active_runs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    plan_execution = dict(metadata.get("plan_execution") or {})
    if str(plan_execution.get("mode") or "").strip() != "module_parent_milestones":
        return _staged_module_status_items_from_metadata(metadata, active_runs=active_runs)
    dag = _plan_execution_dag_state(plan_execution)
    if not dag:
        return []
    module_order = _coerce_text_list(plan_execution.get("module_order") or dag.get("module_order"))
    if not module_order:
        return []
    module_kind = dict(dag.get("module_kind") or {})
    module_status = dict(dag.get("module_status") or {})
    depends_on = dict(dag.get("depends_on") or {})
    remaining_indegree = dict(dag.get("remaining_indegree") or {})
    running_modules = dict(dag.get("running_modules") or {})
    child_ids = dict(plan_execution.get("child_work_order_ids") or {})
    module_outputs = dict(dag.get("module_outputs") or {})
    node_cursors = dict(dag.get("module_cursors") or {})
    dag_execution = dict(plan_execution.get("dag_execution") or {})
    node_executors = dict(dag.get("node_executors") or dag_execution.get("node_executors") or {})
    default_executor = str(dag.get("default_executor_profile") or dag_execution.get("default_executor_profile") or "").strip()
    modules_by_id = _plan_artifact_modules_by_id(metadata.get("plan_artifact"))
    active_worker_by_work_order = _active_worker_by_work_order(active_runs or [])
    milestone_by_module: dict[str, dict[str, Any]] = {}
    for index, milestone in enumerate(list(milestones or [])):
        if not isinstance(milestone, dict):
            continue
        if index < len(module_order):
            milestone_by_module[module_order[index]] = dict(milestone)
        explicit_module = str(milestone.get("module_id") or milestone.get("title") or "").strip()
        if explicit_module in module_order:
            milestone_by_module[explicit_module] = dict(milestone)
    items: list[dict[str, Any]] = []
    for module_id in module_order:
        module = modules_by_id.get(module_id)
        milestone = milestone_by_module.get(module_id, {})
        output = dict(module_outputs.get(module_id) or {}) if isinstance(module_outputs.get(module_id), dict) else {}
        running_child = str(running_modules.get(module_id) or "").strip()
        child_id = str(child_ids.get(module_id) or running_child or output.get("child_work_order_id") or "").strip()
        active_worker = dict(active_worker_by_work_order.get(child_id) or {}) if child_id else {}
        remaining = _coerce_int(remaining_indegree.get(module_id))
        if remaining is None:
            remaining = len(_coerce_text_list(depends_on.get(module_id)))
        status = str(module_status.get(module_id) or "").strip().lower()
        if running_child:
            status = "running"
        if not status:
            status = "ready" if int(remaining or 0) == 0 else "blocked"
        summary = str(milestone.get("summary") or "").strip()
        if not summary and module is not None:
            summary = str(getattr(module, "responsibility", "") or "").strip()
        if not summary:
            summary = str(output.get("summary") or output.get("module_name") or "").strip()
        items.append(
            {
                "module_id": module_id,
                "kind": str(module_kind.get(module_id) or "module").strip() or "module",
                "status": status,
                "executor_profile": str(node_executors.get(module_id) or default_executor or "").strip(),
                "depends_on": _coerce_text_list(depends_on.get(module_id)),
                "remaining_indegree": int(remaining or 0),
                "child_work_order_id": child_id,
                "running_child_work_order_id": running_child,
                "current_worker": active_worker,
                "node_cursor": dict(node_cursors.get(module_id) or {}) if isinstance(node_cursors.get(module_id), dict) else {},
                "completed": status == "completed",
                "ready": status in {"ready", "needs_repair", "stale"} and int(remaining or 0) == 0,
                "summary": summary,
                "output": output,
            }
        )
    return items


def _staged_module_status_items_from_metadata(
    metadata: dict[str, Any],
    *,
    active_runs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    staged = dict(metadata.get("staged_planning") or {})
    detail_modules = {
        str(key): dict(value)
        for key, value in dict(staged.get("detail_modules") or {}).items()
        if str(key or "").strip() and isinstance(value, dict)
    }
    if not detail_modules:
        return []
    module_order = _dedupe_text(
        [
            *_coerce_text_list(staged.get("module_ids")),
            *detail_modules.keys(),
        ]
    )
    active_worker_by_work_order = _active_worker_by_work_order(active_runs or [])
    items: list[dict[str, Any]] = []
    for module_id in module_order:
        detail = dict(detail_modules.get(module_id) or {})
        child_id = str(detail.get("work_order_id") or "").strip()
        active_worker = dict(active_worker_by_work_order.get(child_id) or {}) if child_id else {}
        status = str(detail.get("status") or "pending").strip().lower() or "pending"
        if active_worker:
            status = "running"
        artifact = dict(detail.get("artifact") or {}) if isinstance(detail.get("artifact"), dict) else {}
        validation = dict(detail.get("validation") or {}) if isinstance(detail.get("validation"), dict) else {}
        summary = str(detail.get("summary") or artifact.get("title") or "").strip()
        items.append(
            {
                "module_id": module_id,
                "kind": "module_detail",
                "status": status,
                "executor_profile": "software_engineering.architect_module_detail",
                "depends_on": [],
                "remaining_indegree": 0,
                "child_work_order_id": child_id,
                "running_child_work_order_id": child_id if status == "running" else "",
                "current_worker": active_worker,
                "node_cursor": {},
                "completed": status == "completed",
                "ready": status in {"pending", "waiting_for_slot"},
                "summary": summary,
                "output": artifact,
                "validation": validation,
                "recovered_at": str(detail.get("recovered_at") or ""),
                "started_at": str(detail.get("started_at") or ""),
                "completed_at": str(detail.get("completed_at") or ""),
            }
        )
    return items


def _module_status_text(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    lines = ["Modules:"]
    for item in items:
        module_id = str(item.get("module_id") or "").strip()
        status = str(item.get("status") or "").strip() or "unknown"
        kind = str(item.get("kind") or "module").strip() or "module"
        executor = str(item.get("executor_profile") or "").strip()
        deps = _coerce_text_list(item.get("depends_on"))
        child = str(item.get("running_child_work_order_id") or item.get("child_work_order_id") or "").strip()
        worker = dict(item.get("current_worker") or {}) if isinstance(item.get("current_worker"), dict) else {}
        cursor = dict(item.get("node_cursor") or {}) if isinstance(item.get("node_cursor"), dict) else {}
        suffix: list[str] = []
        if kind != "module":
            suffix.append(kind)
        if executor:
            suffix.append(f"executor={executor}")
        if deps:
            suffix.append("depends_on=" + ",".join(deps))
        if child:
            suffix.append(f"child={child}")
        if worker:
            run_id = str(worker.get("run_id") or "").strip()
            round_count = _coerce_int(worker.get("llm_round_count"))
            phase = str(worker.get("last_phase") or "").strip()
            worker_bits: list[str] = []
            if run_id:
                worker_bits.append(f"run={run_id}")
            if round_count is not None:
                worker_bits.append(f"llm_rounds={int(round_count)}")
            if phase:
                worker_bits.append(f"phase={phase}")
            if worker_bits:
                suffix.append("worker=" + ",".join(worker_bits))
        if cursor and _coerce_int(cursor.get("current_milestone_index")) is not None:
            suffix.append(f"m={int(_coerce_int(cursor.get('current_milestone_index')) or 0)}")
        line = f"- {module_id} [{status}]"
        if suffix:
            line += " (" + "; ".join(suffix) + ")"
        lines.append(line)
        summary = str(item.get("summary") or "").strip()
        if summary:
            lines.append(f"  {summary}")
    return "\n".join(lines)


def _plan_artifact_modules_by_id(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    try:
        artifact = PlanArtifact.from_dict(value)
    except Exception:
        return {}
    return {module.module_id: module for module in artifact.modules if module.module_id}


def _module_dependency_outputs_for(dag: dict[str, Any], module_id: str) -> list[dict[str, Any]]:
    outputs = dict(dag.get("module_outputs") or {})
    result: list[dict[str, Any]] = []
    for dep in _coerce_text_list(dict(dag.get("depends_on") or {}).get(str(module_id or "").strip())):
        output = outputs.get(dep)
        if isinstance(output, dict):
            result.append(dict(output))
    return result


def _workspace_for_plan_module(parent_workspace: Any, dag: dict[str, Any], module_id: str) -> dict[str, Any]:
    workspace = _plan_child_workspace_from_parent(parent_workspace)
    dependency_outputs = _module_dependency_outputs_for(dag, module_id)
    if dependency_outputs:
        workspace["module_dependency_outputs"] = dependency_outputs
        baseline = next((dict(item) for item in reversed(dependency_outputs) if str(item.get("repo_path") or "").strip()), {})
        repo_path = str(baseline.get("repo_path") or "").strip()
        if repo_path:
            workspace["source_repo"] = repo_path
        branch = str(baseline.get("branch") or baseline.get("work_order_branch") or "").strip()
        if branch:
            workspace["base_ref"] = branch
            workspace["merge_target"] = branch
    return workspace


class TaskingRepositoryPort(Protocol):
    def prepare_pack_for_spawn(self, pack: TaskContextPack) -> TaskContextPack:
        ...

    def resumable_staged_module_detail_parent_work_order_ids(self) -> list[str]:
        ...

    def search_tasks(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        ...

    def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...

    def search_work_orders(self, query: str, *, limit: int = 10, include_archived: bool = False) -> dict[str, Any]:
        ...

    def search_work_order_drafts(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        ...

    def read_task(self, task_id: str, *, include_archived: bool = False) -> dict[str, Any]:
        ...

    def read_work_order(self, work_order_id: str, *, active_runs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        ...

    def archive_work_order(
        self,
        work_order_id: str,
        *,
        reason: str = "",
        include_children: bool = False,
        restore: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        ...

    def remove_work_order(
        self,
        work_order_id: str,
        *,
        reason: str = "",
        include_children: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        ...

    def read_work_order_draft(self, draft_id: str) -> dict[str, Any]:
        ...

    def promote_work_order_draft(self, draft_id: str, *, reviewed_candidate: dict[str, Any] | None = None) -> dict[str, Any]:
        ...


@dataclass
class MinionTaskingRepository(TaskingRepositoryPort):
    runtime_root: Path
    ledger: MinionLedgerStore = field(init=False, repr=False)
    plans: MinionPlanStore = field(init=False, repr=False)
    review_gates: MinionReviewGateStore = field(init=False, repr=False)
    search: MinionSearchStore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.ledger = MinionLedgerStore(self)
        self.plans = MinionPlanStore(self)
        self.review_gates = MinionReviewGateStore(self)
        self.search = MinionSearchStore(self)

    @property
    def db_path(self) -> Path:
        return minion_db_path(self.runtime_root)

    def ensure_schema(self) -> None:
        with self._connect() as db:
            ensure_minion_schema(self.runtime_root, db)
            self._migrate_legacy_minion_tables_if_needed(db)

    def prepare_pack_for_spawn(self, pack: TaskContextPack) -> TaskContextPack:
        self.ensure_work_order_from_pack(pack)
        pack = self._hydrate_pack_from_work_order(pack)
        continuity = self.build_continuity(pack.work_order_id)
        workspace = dict(pack.workspace)
        metadata = dict(pack.metadata)
        metadata = _normalize_plan_backed_milestones(metadata)
        milestones = _plan_truth_milestones(metadata, pack.acceptance_criteria, pack.instruction or pack.goal)
        metadata = _normalize_milestone_execution_metadata(metadata, milestones)
        metadata = _sync_module_execution_cursor_from_continuity(metadata, continuity)
        metadata.setdefault("task_id", continuity.get("task_id") or "")
        metadata = _materialize_plan_module_adapter(pack, metadata=metadata, workspace=workspace)
        prompt_view = prompt_view_from_metadata(metadata, workspace=workspace)
        plan_execution = dict(metadata.get("plan_execution") or {})
        if (
            not prompt_view
            and str(plan_execution.get("mode") or "") != "module_parent_milestones"
            and isinstance(metadata.get("plan_artifact"), dict)
        ):
            prompt_view = _prompt_view_from_current_milestone(pack, continuity=continuity, metadata=metadata)
        if (
            not prompt_view
            and str(plan_execution.get("mode") or "") != "module_parent_milestones"
            and _is_plan_module_child_metadata(metadata)
        ):
            prompt_view = _prompt_view_from_current_milestone(pack, continuity=continuity, metadata=metadata)
        if prompt_view:
            metadata["prompt_view"] = prompt_view
        repair_context = _repair_context_from_spawn_pack(metadata, workspace, prompt_view)
        acceptance_criteria = list(pack.acceptance_criteria)
        if repair_context:
            metadata["repair_context"] = repair_context
            workspace.setdefault("repair_context", repair_context)
            if isinstance(metadata.get("coder_work_order"), dict):
                metadata["coder_work_order"] = _apply_repair_context_to_coder_order_payload(
                    dict(metadata.get("coder_work_order") or {}),
                    repair_context,
                )
                prompt_view = prompt_view_from_metadata(metadata, workspace=workspace)
            elif isinstance(metadata.get("prompt_view"), dict):
                prompt_view = dict(metadata.get("prompt_view") or {})
            if prompt_view:
                metadata["prompt_view"] = _apply_repair_context_to_prompt_view(prompt_view, repair_context)
        metadata = _normalize_preferred_endpoint_metadata(metadata)
        if isinstance(metadata.get("prompt_view"), dict):
            prompt_milestone = dict(dict(metadata.get("prompt_view") or {}).get("milestone") or {})
            current_acceptance = _coerce_text_list(prompt_milestone.get("acceptance_criteria"))
            if current_acceptance:
                acceptance_criteria = current_acceptance
        return TaskContextPack.from_dict(
            {
                **pack.to_dict(),
                "acceptance_criteria": acceptance_criteria,
                "workspace": workspace,
                "continuity": continuity,
                "metadata": metadata,
            }
        )

    def pack_for_work_order(self, work_order_id: str, *, overrides: dict[str, Any] | None = None) -> TaskContextPack:
        snapshot = self.read_work_order(str(work_order_id))
        if snapshot.get("status") != "ok":
            raise KeyError(f"unknown work order: {work_order_id}")
        pack = _pack_from_work_order_snapshot(snapshot)
        if overrides:
            pack = _merge_pack_overrides(pack, dict(overrides))
        return pack

    def _resumable_plan_module_child_pack(
        self,
        child_work_order_id: str,
        *,
        module_id: str,
        workspace: dict[str, Any],
        metadata: dict[str, Any],
        profile_group: str,
        profile_name: str,
        minion_profile: str,
    ) -> TaskContextPack | None:
        child_id = str(child_work_order_id or "").strip()
        if not child_id:
            return None
        snapshot = self.read_work_order(child_id)
        if snapshot.get("status") != "ok" or not snapshot.get("current_milestone"):
            return None
        work_order = dict(snapshot.get("work_order") or {})
        stored_metadata = _loads_or_dict(work_order.get("metadata"))
        stored_module_id = str(stored_metadata.get("parent_module_id") or stored_metadata.get("module_id") or "").strip()
        if stored_module_id and stored_module_id != str(module_id or "").strip():
            return None
        overrides = {
            "workspace": dict(workspace or {}),
            "metadata": dict(metadata or {}),
            "profile_group": profile_group,
            "profile_name": profile_name,
            "minion_profile": minion_profile,
        }
        return self.pack_for_work_order(child_id, overrides=overrides)

    def build_coder_module_pack_from_plan(
        self,
        plan_payload: dict[str, Any] | PlanArtifact,
        *,
        module_id: str = "",
        work_order_id: str = "",
        workspace: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        goal: str = "",
        instruction: str = "",
        minion_profile: str = "",
        allowed_capabilities: list[str] | None = None,
    ) -> TaskContextPack:
        artifact = validate_dispatchable_plan_artifact(plan_payload)
        plan_revision = _plan_revision_from_payload(plan_payload)
        validation = dispatchable_plan_validation(artifact)
        resolved_module_id = plan_module_id_at(artifact, module_id=module_id)
        module_kind = _plan_module_kind(validation, resolved_module_id)
        resolved_work_order_id = str(work_order_id or new_work_id("wo")).strip()
        milestone_id = plan_milestone_id_at(artifact, module_id=resolved_module_id, milestone_index=0)
        resolved_workspace = dict(workspace or {})
        milestones = module_milestone_records(artifact, module_id=resolved_module_id)
        if module_kind == "join":
            completion_policy = dict(resolved_workspace.get("completion_policy") or {})
            completion_policy["allow_artifact_evidence"] = True
            completion_policy.setdefault("artifact_evidence", "verification_report")
            resolved_workspace["completion_policy"] = completion_policy
        artifact_metadata = dict(artifact.metadata or {})
        pack_metadata = dict(metadata or {})
        if isinstance(artifact_metadata.get("requirements_brief"), dict) and not isinstance(pack_metadata.get("requirements_brief"), dict):
            pack_metadata["requirements_brief"] = dict(artifact_metadata.get("requirements_brief") or {})
        artifact_profile_family = str(artifact_metadata.get("profile_family") or "").strip()
        if artifact_profile_family and not str(pack_metadata.get("profile_family") or "").strip():
            pack_metadata["profile_family"] = artifact_profile_family
        profile_context = dict(artifact_metadata)
        profile_context.update(pack_metadata)
        profile_family = _profile_family_from_metadata(profile_context)
        resolved_profile = str(
            minion_profile
            or pack_metadata.get("executor_profile")
            or pack_metadata.get("minion_profile")
            or ""
        ).strip()
        if not resolved_profile:
            resolved_profile = _profile_from_group_name(
                pack_metadata.get("dispatch_profile_group"),
                pack_metadata.get("dispatch_profile_name"),
                profile_family=profile_family,
            )
        if not resolved_profile:
            declared_next = _plan_artifact_workflow_next_profile(artifact)
            if declared_next and declared_next != NONE_PROFILE:
                resolved_profile = canonical_profile_ref_for_family(declared_next, profile_family=profile_family)
        if not resolved_profile:
            raise ValueError("plan module pack requires an explicit executor profile")
        profile_group, profile_name = _profile_ref_parts_from_canonical(resolved_profile)
        execution_contract = dict(pack_metadata.get("execution_contract") or {})
        module_role = _module_role_from_contract(execution_contract, fallback_profile=resolved_profile)
        module_execution = {
            "mode": SERIAL_MODULE_MILESTONES_MODE,
            "plan_id": artifact.plan_id,
            "plan_revision": plan_revision,
            "module_id": resolved_module_id,
            "module_kind": module_kind,
            "module_name": str(pack_metadata.get("module_name") or resolved_module_id),
            "executor_profile": resolved_profile,
            "profile_role": module_role,
            "current_milestone_index": 0,
            "milestone_count": len(milestones),
            "auto_advance": True,
            "defer_experience_until_module_complete": True,
            "status": "active",
            "pending_experience": {
                "task_lessons": [],
                "system_lessons": [],
                "memory_candidates": [],
            },
        }
        if execution_contract:
            module_execution["execution_contract"] = execution_contract
        if allowed_capabilities:
            pack_metadata["coder_allowed_capabilities"] = list(allowed_capabilities or [])
        task_id = str(pack_metadata.get("task_id") or artifact.task_id).strip()
        project_name = str(pack_metadata.get("project_name") or _plan_project_name(artifact, workspace=resolved_workspace, task_id=task_id)).strip()
        module_name = str(pack_metadata.get("module_name") or resolved_module_id).strip()
        if project_name:
            resolved_workspace.setdefault("project_name", project_name)
        if module_name:
            resolved_workspace.setdefault("module_name", module_name)
        repair_context = _loads_or_dict(pack_metadata.get("repair_context"))
        if repair_context:
            resolved_workspace.setdefault("repair_context", repair_context)
        module_action = _module_dispatch_action_from_contract(execution_contract, fallback_profile=resolved_profile)
        pack_metadata.update(
            {
                "task_id": task_id,
                "project_name": project_name,
                "task_title": str(pack_metadata.get("task_title") or artifact.summary or artifact.task_id),
                "work_order_title": str(pack_metadata.get("work_order_title") or f"{module_name} {module_action['noun']}"),
                "plan_artifact": _plan_artifact_payload(artifact, plan_revision=plan_revision),
                "plan_validation": validation,
                "module_id": resolved_module_id,
                "module_kind": module_kind,
                "module_name": module_name,
                "module_execution": module_execution,
                "milestones": milestones,
            }
        )
        pack_metadata.pop("prompt_view", None)
        pack_metadata.pop("coder_work_order", None)
        acceptance_criteria = [
            item for milestone in milestones for item in _coerce_text_list(milestone.get("acceptance"))
        ]
        return TaskContextPack.from_dict(
            {
                "work_order_id": resolved_work_order_id,
                "goal": str(goal or artifact.summary or f"{module_action['verb']} module {resolved_module_id}"),
                "instruction": str(
                    instruction
                    or f"{module_action['verb']} module {resolved_module_id} one plan milestone at a time."
                ),
                "acceptance_criteria": acceptance_criteria,
                "workspace": resolved_workspace,
                "profile_group": profile_group,
                "profile_name": profile_name,
                "minion_profile": resolved_profile,
                "allowed_capabilities": list(allowed_capabilities or []),
                "metadata": pack_metadata,
            }
        )

    def build_plan_parent_pack_from_plan(
        self,
        plan_payload: dict[str, Any] | PlanArtifact,
        *,
        work_order_id: str = "",
        workspace: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        goal: str = "",
        instruction: str = "",
        profile_group: str = "",
        profile_name: str = "",
    ) -> TaskContextPack:
        artifact = validate_dispatchable_plan_artifact(plan_payload)
        plan_revision = _plan_revision_from_payload(plan_payload)
        validation = dispatchable_plan_validation(artifact)
        resolved_work_order_id = str(work_order_id or new_work_id("wo")).strip()
        resolved_workspace = dict(workspace or {})
        module_order = plan_module_order_for_execution(artifact)
        milestones = _module_parent_milestones(artifact, module_order=module_order)
        pack_metadata = dict(metadata or {})
        task_id = str(pack_metadata.get("task_id") or artifact.task_id).strip()
        project_name = str(pack_metadata.get("project_name") or _plan_project_name(artifact, workspace=resolved_workspace, task_id=task_id)).strip()
        if project_name:
            resolved_workspace.setdefault("project_name", project_name)
        dispatch_profile_group, dispatch_profile_name, default_executor_profile = _dispatch_profile_from_plan_context(
            artifact,
            pack_metadata,
            profile_group=profile_group,
            profile_name=profile_name,
        )
        profile_family = _profile_family_from_metadata(
            pack_metadata,
            fallback_group=dispatch_profile_group,
            fallback_profile=default_executor_profile,
        )
        plan_execution = dict(pack_metadata.get("plan_execution") or {})
        replacement_source = dict(pack_metadata.get("replacement_source") or {})
        dag_epoch = max(
            0,
            _coerce_int(plan_execution.get("dag_epoch"))
            or _coerce_int(pack_metadata.get("dag_epoch"))
            or _coerce_int(replacement_source.get("replacement_dag_epoch"))
            or 0,
        )
        module_dag = _module_dag_from_validation(validation, _plan_execution_dag_state(plan_execution))
        node_executors = _module_executor_profiles(
            artifact,
            validation,
            default_profile=default_executor_profile,
            existing=module_dag,
        )
        dag_execution = _dag_execution_metadata(default_executor_profile, node_executors)
        module_dag["default_executor_profile"] = dag_execution["default_executor_profile"]
        module_dag["node_executors"] = dict(dag_execution["node_executors"])
        plan_execution.update(
            {
                "mode": "module_parent_milestones",
                "execution_shape": validation.get("execution_shape") or "fork_join_linear",
                "plan_id": artifact.plan_id,
                "plan_revision": plan_revision,
                "node_order": list(validation.get("node_order") or []),
                "module_order": module_order,
                "current_module_index": int(plan_execution.get("current_module_index") or 0),
                "status": str(plan_execution.get("status") or "active"),
                "dag_epoch": dag_epoch,
                "dag_revision": max(0, _coerce_int(plan_execution.get("dag_revision")) or 0),
                "auto_advance_modules": bool(plan_execution.get("auto_advance_modules", True)),
                "child_work_order_ids": dict(plan_execution.get("child_work_order_ids") or {}),
                "active_child_work_order_ids": _coerce_text_list(plan_execution.get("active_child_work_order_ids")),
                "dag_execution": dag_execution,
            }
        )
        _set_plan_execution_dag_state(plan_execution, module_dag)
        pack_metadata.update(
            {
                "task_id": task_id,
                "project_name": project_name,
                "task_title": str(pack_metadata.get("task_title") or artifact.summary or artifact.task_id),
                "work_order_title": str(pack_metadata.get("work_order_title") or artifact.summary or "Plan implementation"),
                "profile_family": profile_family,
                "plan_artifact": _plan_artifact_payload(artifact, plan_revision=plan_revision),
                "plan_validation": validation,
                "dispatch_profile_group": dispatch_profile_group,
                "dispatch_profile_name": dispatch_profile_name,
                "plan_execution": plan_execution,
                "milestones": milestones,
            }
        )
        pack_metadata.pop("prompt_view", None)
        return TaskContextPack.from_dict(
            {
                "work_order_id": resolved_work_order_id,
                "goal": str(goal or artifact.summary or "Implement plan"),
                "instruction": str(instruction or "Execute the structured plan one module milestone at a time."),
                "acceptance_criteria": [item for milestone in milestones for item in _coerce_text_list(milestone.get("acceptance"))],
                "workspace": resolved_workspace,
                "profile_group": dispatch_profile_group,
                "profile_name": dispatch_profile_name,
                "metadata": pack_metadata,
            }
        )

    def build_planner_revision_pack_from_review_gate(
        self,
        review_gate_ref: Any,
        *,
        work_order_id: str = "",
        workspace: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        goal: str = "",
        instruction: str = "",
    ) -> TaskContextPack:
        loaded_gate = self.load_review_gate(review_gate_ref)
        gate_payload = dict(loaded_gate.get("review_gate") or {})
        if str(gate_payload.get("gate_kind") or "").strip().lower() != "plan_acceptance":
            raise ValueError("plan revision dispatch requires a plan_acceptance review gate")
        verdict = str(gate_payload.get("verdict") or "").strip().lower()
        if verdict == "pass":
            raise ValueError("plan revision dispatch requires a failing or partial plan review gate")
        target = dict(gate_payload.get("target") or {})
        source_plan_ref = dict(target.get("plan_ref") or {})
        original_source_contract = (
            dict(target.get("source_contract") or {})
            if isinstance(target.get("source_contract"), dict)
            else {}
        )
        loaded_plan = self.load_revisable_plan_ref(source_plan_ref)
        artifact = validate_final_plan_artifact(loaded_plan.get("plan_artifact") or {})
        source_revision = _plan_revision_from_payload(loaded_plan.get("plan_artifact"), loaded_plan.get("plan_ref"))
        next_revision = source_revision + 1
        revision_checklist = _plan_revision_checklist(gate_payload)
        resolved_work_order_id = str(work_order_id or new_work_id("wo")).strip()
        resolved_workspace = dict(workspace or {})
        for key in ("repo_path", "source_repo", "artifact_dir"):
            value = str(target.get(key) or "").strip()
            if value:
                resolved_workspace.setdefault(key, value)
        resolved_workspace = _planner_revision_workspace(resolved_workspace)
        planner_goal = str(goal or f"Revise architecture plan {artifact.plan_id} from reviewer gate {gate_payload.get('gate_id') or ''}").strip()
        planner_instruction = str(
            instruction
            or (
                "Revise the referenced submitted plan draft only. First call plan_checkout with {} so Pal uses the "
                "workspace-bound source_plan_ref; do not construct or guess plan_ref/review_gate_ref objects. Then follow "
                "revision_source.plan_revision_checklist in order. Prefer plan_apply_revision_item for each PRC item: Pal "
                "already supplies reject_reason, target_handle, target_path, suggested_tool, and suggested_args, so only "
                "provide replacement fields and evidence. Use plan_read/plan_get only when a PRC item truly needs inspection. "
                "Use low-level plan_update_*, plan_delete_*, plan_move_milestone, or plan_replace_milestone_acceptance_criteria "
                "only for complex repairs, then mark the PRC with plan_apply_revision_item resolution=manual. "
                f"Preserve task_id={artifact.task_id} and plan_id={artifact.plan_id}; Pal owns plan_revision={next_revision}. "
                "Keep the fork_join_linear topology dispatchable, call plan_validate, then plan_submit_for_review. "
                "Do not implement code and do not rebuild the whole plan unless checkout reports the current draft cannot be repaired. "
                "The original architecture source_contract remains binding for this revision; satisfy it exactly, including explicit "
                "counts, topology/module shape, names, hard requirements, and acceptance criteria."
            )
        )
        planner_work_order = build_planner_work_order(
            goal=planner_goal,
            task_id=artifact.task_id,
            work_order_id=resolved_work_order_id,
            turn_index=next_revision,
            plan_revision=next_revision,
        )
        planner_work_order["role"] = "architect"
        planner_work_order["revision_source"] = {
            "source_plan_ref": dict(loaded_plan.get("plan_ref") or source_plan_ref),
            "review_gate_ref": dict(loaded_gate.get("review_gate_ref") or {}),
            "review_gate": _plan_revision_gate_summary(gate_payload),
            "plan_revision_checklist": revision_checklist,
        }
        if original_source_contract:
            planner_work_order["revision_source"]["original_source_contract"] = dict(original_source_contract)
        milestones = [
            {
                "milestone_id": "revise_plan",
                "title": "Revise reviewed plan",
                "summary": (
                    "Locally edit the reviewed plan draft and submit a revised draft that resolves the plan reviewer findings."
                ),
                "acceptance": [
                    f"Submitted draft preserves task_id={artifact.task_id} and plan_id={artifact.plan_id}.",
                    f"Submitted draft is revision {next_revision}; Pal manages the revision identity.",
                    "Reviewer findings and required fixes are addressed or explicitly called out as remaining questions.",
                    "Every plan_revision_checklist item is completed or has a concrete blocker.",
                    "Dispatch validation passes with fork_join_linear topology.",
                ],
            }
        ]
        pack_metadata = dict(metadata or {})
        plan_review_state = dict(pack_metadata.get("plan_review") or {})
        plan_review_state.update(
            {
                "status": "revision_in_progress",
                "source_plan_revision": source_revision,
                "target_plan_revision": next_revision,
                "review_gate_id": str(gate_payload.get("gate_id") or ""),
            }
        )
        revision_task_id = str(
            pack_metadata.get("revision_task_id")
            or f"{artifact.task_id}_plan_revision_{next_revision}_{resolved_work_order_id}"
        ).strip()
        pack_metadata.update(
            {
                "task_id": revision_task_id,
                "expected_plan_task_id": artifact.task_id,
                "task_title": str(pack_metadata.get("task_title") or artifact.summary or artifact.task_id),
                "work_order_title": str(pack_metadata.get("work_order_title") or f"Revise plan {artifact.plan_id}"),
                "planner_work_order": planner_work_order,
                "plan_revision": next_revision,
                "source_plan_ref": dict(loaded_plan.get("plan_ref") or source_plan_ref),
                "source_plan_artifact": _plan_artifact_payload(artifact, plan_revision=source_revision),
                **({"original_source_contract": dict(original_source_contract)} if original_source_contract else {}),
                "review_gate_ref": dict(loaded_gate.get("review_gate_ref") or {}),
                "review_gate": _plan_revision_gate_summary(gate_payload),
                "plan_revision_checklist": revision_checklist,
                "milestones": milestones,
                "plan_review": plan_review_state,
                "skip_pre_plan_contract": True,
            }
        )
        pack_metadata.pop("prompt_view", None)
        return TaskContextPack.from_dict(
            {
                "work_order_id": resolved_work_order_id,
                "goal": planner_goal,
                "instruction": planner_instruction,
                "acceptance_criteria": [item for milestone in milestones for item in _coerce_text_list(milestone.get("acceptance"))],
                "workspace": resolved_workspace,
                "profile_group": "software_engineering",
                "profile_name": "architect_plan_revision",
                "metadata": pack_metadata,
            }
        )

    def build_planner_revision_pack_from_plan_decision(
        self,
        plan_ref: Any,
        *,
        work_order_id: str = "",
        workspace: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        reason: str = "",
        edit_instruction: str = "",
    ) -> TaskContextPack:
        loaded_plan = self.load_revisable_plan_ref(plan_ref)
        artifact = validate_final_plan_artifact(loaded_plan.get("plan_artifact") or {})
        resolved_workspace = _planner_revision_workspace(dict(workspace or {}))
        source_revision = _plan_revision_from_payload(loaded_plan.get("plan_artifact"), loaded_plan.get("plan_ref"))
        next_revision = source_revision + 1
        resolved_work_order_id = str(work_order_id or new_work_id("wo")).strip()
        planner_goal = f"Revise architecture plan {artifact.plan_id} from human plan decision"
        decision_text = str(edit_instruction or reason or "Human requested plan revision.").strip()
        planner_instruction = (
            "Revise the referenced submitted plan draft only. First call plan_checkout with the supplied "
            "revision_source.source_plan_ref, then repair the plan with plan_update_* or related plan builder tools. "
            f"Preserve task_id={artifact.task_id} and plan_id={artifact.plan_id}; Pal owns plan_revision={next_revision}. "
            "Keep fork_join_linear topology dispatchable, call plan_validate, then plan_submit_for_review. "
            f"Human decision to satisfy: {decision_text}"
        )
        planner_work_order = build_planner_work_order(
            goal=planner_goal,
            task_id=artifact.task_id,
            work_order_id=resolved_work_order_id,
            turn_index=next_revision,
            plan_revision=next_revision,
        )
        planner_work_order["role"] = "architect"
        planner_work_order["revision_source"] = {
            "source_plan_ref": dict(loaded_plan.get("plan_ref") or {}),
            "human_decision": {
                "reason": str(reason or "").strip(),
                "edit_instruction": str(edit_instruction or "").strip(),
            },
            "plan_revision_checklist": [
                {
                    "kind": "human_feedback",
                    "summary": decision_text,
                    "required": True,
                }
            ],
        }
        milestones = [
            {
                "milestone_id": "revise_plan",
                "title": "Revise plan from human decision",
                "summary": "Locally edit the reviewed plan draft and submit a revised draft that satisfies the human decision.",
                "acceptance": [
                    f"Submitted draft preserves task_id={artifact.task_id} and plan_id={artifact.plan_id}.",
                    f"Submitted draft is revision {next_revision}; Pal manages the revision identity.",
                    "Human rejection or edit request is addressed or converted into a concrete user-owned blocker.",
                    "Dispatch validation passes with fork_join_linear topology.",
                ],
            }
        ]
        pack_metadata = dict(metadata or {})
        plan_review_state = dict(pack_metadata.get("plan_review") or {})
        plan_review_state.update(
            {
                "status": "revision_in_progress",
                "source_plan_revision": source_revision,
                "target_plan_revision": next_revision,
                "human_decision": {
                    "reason": str(reason or "").strip(),
                    "edit_instruction": str(edit_instruction or "").strip(),
                },
            }
        )
        revision_task_id = str(
            pack_metadata.get("revision_task_id")
            or f"{artifact.task_id}_plan_revision_{next_revision}_{resolved_work_order_id}"
        ).strip()
        pack_metadata.update(
            {
                "task_id": revision_task_id,
                "expected_plan_task_id": artifact.task_id,
                "task_title": str(pack_metadata.get("task_title") or artifact.summary or artifact.task_id),
                "work_order_title": str(pack_metadata.get("work_order_title") or f"Revise plan {artifact.plan_id}"),
                "planner_work_order": planner_work_order,
                "plan_revision": next_revision,
                "source_plan_ref": dict(loaded_plan.get("plan_ref") or {}),
                "source_plan_artifact": _plan_artifact_payload(artifact, plan_revision=source_revision),
                "human_plan_decision": {
                    "reason": str(reason or "").strip(),
                    "edit_instruction": str(edit_instruction or "").strip(),
                },
                "milestones": milestones,
                "plan_review": plan_review_state,
                "skip_pre_plan_contract": True,
            }
        )
        pack_metadata.pop("prompt_view", None)
        return TaskContextPack.from_dict(
            {
                "work_order_id": resolved_work_order_id,
                "goal": planner_goal,
                "instruction": planner_instruction,
                "acceptance_criteria": [item for milestone in milestones for item in _coerce_text_list(milestone.get("acceptance"))],
                "workspace": resolved_workspace,
                "profile_group": "software_engineering",
                "profile_name": "architect_plan_revision",
                "metadata": pack_metadata,
            }
        )

    def load_dispatchable_plan_ref(self, plan_ref: Any) -> dict[str, Any]:
        return self.plans.load_dispatchable_plan_ref(plan_ref)

    def load_revisable_plan_ref(self, plan_ref: Any) -> dict[str, Any]:
        return self.plans.load_revisable_plan_ref(plan_ref)

    def load_accepted_plan_ref(self, plan_ref: Any) -> dict[str, Any]:
        return self.plans.load_accepted_plan_ref(plan_ref)

    def read_plan_ref(self, plan_ref: Any) -> dict[str, Any]:
        return self.plans.read_plan_ref(plan_ref)

    def search_plan_refs(self, query: str = "", *, limit: int = 10) -> dict[str, Any]:
        return self.plans.search_plan_refs(query, limit=limit)

    def submit_review_gate(
        self,
        gate_payload: dict[str, Any] | ReviewGateResult,
        *,
        reviewer_profile: str = "",
        work_order_id: str = "",
        run_id: str = "",
    ) -> dict[str, Any]:
        return self.review_gates.submit_review_gate(
            gate_payload,
            reviewer_profile=reviewer_profile,
            work_order_id=work_order_id,
            run_id=run_id,
        )

    def record_review_tool_evidence_refs(
        self,
        refs: list[dict[str, Any]],
        *,
        work_order_id: str,
        run_id: str = "",
        reviewer_profile: str = "",
    ) -> list[dict[str, Any]]:
        return self.review_gates.record_review_tool_evidence_refs(
            refs,
            work_order_id=work_order_id,
            run_id=run_id,
            reviewer_profile=reviewer_profile,
        )

    def validate_external_verification_ref(self, ref: Any) -> dict[str, Any]:
        return self.review_gates.validate_external_verification_ref(ref)

    def count_ledger_events(self, work_order_id: str, event_kind: str) -> int:
        return self.review_gates.count_ledger_events(work_order_id, event_kind)

    def load_review_gate(self, review_gate_ref: Any) -> dict[str, Any]:
        return self.review_gates.load_review_gate(review_gate_ref)

    def latest_review_gate_for_checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        return self.review_gates.latest_review_gate_for_checkpoint(checkpoint_id)

    def load_checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        self.ensure_schema()
        normalized = str(checkpoint_id or "").strip()
        if not normalized:
            return {"status": "invalid", "error": "checkpoint_id is required"}
        with self._connect() as db:
            row = self._fetch_one(db, "SELECT * FROM minion_worker_checkpoints WHERE checkpoint_id = ?", (normalized,))
        if row is None:
            return {"status": "not_found", "checkpoint_id": normalized}
        payload = _loads(row["payload_json"])
        payload.setdefault("checkpoint_id", str(row["checkpoint_id"] or normalized))
        payload.setdefault("work_order_id", str(row["work_order_id"] or ""))
        payload.setdefault("milestone_index", int(row["milestone_index"] or 0))
        payload.setdefault("status", str(row["status"] or ""))
        payload.setdefault("summary", str(row["summary"] or ""))
        payload.setdefault("minion_id", str(row["minion_id"] or ""))
        payload.setdefault("run_id", str(row["run_id"] or ""))
        payload.setdefault("created_at", str(row["created_at"] or ""))
        return {"status": "ok", "checkpoint": payload}

    def latest_review_gate_for_plan_ref(self, plan_ref: Any) -> dict[str, Any]:
        return self.review_gates.latest_review_gate_for_plan_ref(plan_ref)

    def close_checkpoint_from_review_gate(self, review_gate_ref: Any) -> dict[str, Any]:
        return self.review_gates.close_checkpoint_from_review_gate(review_gate_ref)

    def _validate_plan_acceptance_gate(self, review_gate_ref: Any, loaded_plan: dict[str, Any]) -> dict[str, Any]:
        return self.review_gates.validate_plan_acceptance_gate(review_gate_ref, loaded_plan)

    def revise_plan_ref(
        self,
        plan_ref: Any,
        revised_plan_artifact: dict[str, Any],
        *,
        revision_notes: str = "",
        accepted: bool = False,
        review_gate_ref: Any = None,
        human_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.plans.revise_plan_ref(
            plan_ref,
            revised_plan_artifact,
            revision_notes=revision_notes,
            accepted=accepted,
            review_gate_ref=review_gate_ref,
            human_override=human_override,
        )

    def submit_plan_ref(
        self,
        plan_artifact: dict[str, Any] | PlanArtifact,
        *,
        submission_notes: str = "",
        replace_unaccepted_revision: bool = False,
    ) -> dict[str, Any]:
        return self.plans.submit_plan_ref(
            plan_artifact,
            submission_notes=submission_notes,
            replace_unaccepted_revision=replace_unaccepted_revision,
        )

    def accept_plan_ref(
        self,
        plan_ref: Any,
        *,
        reason: str = "",
        review_gate_ref: Any = None,
        human_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.plans.accept_plan_ref(
            plan_ref,
            reason=reason,
            review_gate_ref=review_gate_ref,
            human_override=human_override,
        )

    def next_ready_plan_module_packs(self, work_order_id: str, *, limit: int = 1, allow_paused: bool = False) -> list[TaskContextPack]:
        snapshot = self.read_work_order(str(work_order_id))
        if snapshot.get("status") != "ok":
            return []
        work_order = dict(snapshot.get("work_order") or {})
        metadata = _loads_or_dict(work_order.get("metadata"))
        plan_execution = dict(metadata.get("plan_execution") or {})
        if str(plan_execution.get("mode") or "") != "module_parent_milestones":
            return []
        expected_metadata_json = _json(metadata)
        status = str(plan_execution.get("status") or "").strip().lower()
        work_order_status = str(work_order.get("status") or "").strip().lower()
        if work_order_status in {"blocked", "failed", "killed"}:
            return []
        if status in {"completed", "blocked", "failed", "killed"}:
            return []
        if status == "paused" and not allow_paused:
            return []
        requested_limit = max(1, int(limit or 1))
        plan_payload = metadata.get("plan_artifact")
        if not isinstance(plan_payload, dict):
            return []
        artifact = PlanArtifact.from_dict(plan_payload)
        validation = dispatchable_plan_validation(artifact)
        module_order = _coerce_text_list(plan_execution.get("module_order"))
        if not module_order:
            module_order = [module.module_id for module in artifact.modules if module.module_id]
        dag = _module_dag_from_validation(validation, _plan_execution_dag_state(plan_execution))
        ready_modules = _dag_ready_module_ids(dag)
        if not ready_modules:
            _set_plan_execution_dag_state(plan_execution, dag)
            resolved_status = _module_dag_status(dag)
            plan_execution["status"] = resolved_status
            plan_execution["active_child_work_order_ids"] = list(dict(dag.get("running_modules") or {}).values())
            metadata["plan_execution"] = plan_execution
            with self._connect() as db:
                self._write_plan_parent_metadata(
                    db,
                    str(work_order_id),
                    metadata,
                    expected_metadata_json=expected_metadata_json,
                    work_order_status="completed" if resolved_status == "completed" else "",
                )
            return []
        child_ids = dict(plan_execution.get("child_work_order_ids") or {})
        candidate_child_ids: dict[str, str] = {}
        for module_id in ready_modules:
            child_work_order_id = str(child_ids.get(module_id) or "").strip()
            if not child_work_order_id:
                child_work_order_id = _plan_module_child_work_order_id(str(work_order_id), module_id, plan_execution)
            candidate_child_ids[module_id] = child_work_order_id
        task_id = str(work_order.get("task_id") or metadata.get("task_id") or artifact.task_id)
        dispatch_profile_group, dispatch_profile_name, default_executor_profile = _dispatch_profile_from_plan_context(
            artifact,
            metadata,
        )
        stored_dag_execution = dict(plan_execution.get("dag_execution") or {})
        if str(stored_dag_execution.get("default_executor_profile") or "").strip():
            default_executor_profile = _canonical_executor_profile(
                stored_dag_execution.get("default_executor_profile"),
                default_profile=default_executor_profile,
            )
        node_executors = _module_executor_profiles(
            artifact,
            validation,
            default_profile=default_executor_profile,
            existing={**dag, "node_executors": dict(stored_dag_execution.get("node_executors") or dag.get("node_executors") or {})},
        )
        dag_execution = _dag_execution_metadata(default_executor_profile, node_executors)
        dag["default_executor_profile"] = dag_execution["default_executor_profile"]
        dag["node_executors"] = dict(dag_execution["node_executors"])
        plan_execution["dag_execution"] = dag_execution
        advanced = _dag_claim_ready_modules(dag, candidate_child_ids, limit=requested_limit)
        dag = dict(advanced.get("dag") or {})
        claims = [dict(item) for item in list(advanced.get("claims") or []) if isinstance(item, dict)]
        if not claims:
            _set_plan_execution_dag_state(plan_execution, dag)
            plan_execution["status"] = str(advanced.get("status") or _module_dag_status(dag))
            plan_execution["active_child_work_order_ids"] = list(advanced.get("active_child_work_order_ids") or [])
            metadata["plan_execution"] = plan_execution
            with self._connect() as db:
                self._write_plan_parent_metadata(
                    db,
                    str(work_order_id),
                    metadata,
                    expected_metadata_json=expected_metadata_json,
                )
            return []
        selected_modules = [str(claim.get("module_id") or "").strip() for claim in claims if str(claim.get("module_id") or "").strip()]
        for claim in claims:
            module_id = str(claim.get("module_id") or "").strip()
            child_id = str(claim.get("child_work_order_id") or "").strip()
            if module_id and child_id:
                child_ids[module_id] = child_id
        packs: list[TaskContextPack] = []
        repair_overlay = _repair_overlay_from_plan_execution(plan_execution)
        for module_id in selected_modules:
            module_name = module_id
            milestone_index = module_order.index(module_id) if module_id in module_order else 0
            child_work_order_id = str(child_ids.get(module_id) or "").strip()
            module_workspace = _workspace_for_plan_module(metadata.get("workspace"), dag, module_id)
            project_name = str(metadata.get("project_name") or _plan_project_name(artifact, workspace=module_workspace, task_id=task_id)).strip()
            dependency_outputs = _module_dependency_outputs_for(dag, module_id)
            integration_workspace = prepare_dependency_integration_baseline(
                self.runtime_root,
                module_workspace,
                project_name=project_name,
                parent_work_order_id=str(work_order_id),
                module_id=module_id,
                dependency_outputs=dependency_outputs,
            )
            if integration_workspace:
                module_workspace.update(integration_workspace)
            child_profile = str(dag_execution["node_executors"].get(module_id) or dag_execution["default_executor_profile"])
            child_profile_group, child_profile_name = _profile_ref_parts_from_canonical(child_profile)
            child_action = _module_dispatch_action_from_contract({}, fallback_profile=child_profile)
            child_metadata = {
                "task_id": _safe_id(task_id),
                "project_name": project_name,
                "task_title": str(metadata.get("task_title") or artifact.summary or task_id),
                "work_order_title": f"{module_name} {child_action['noun']}",
                "parent_work_order_id": str(work_order_id),
                "parent_milestone_index": int(milestone_index),
                "parent_module_id": module_id,
                "parent_module_name": module_name,
                "module_name": module_name,
                "module_dependency_outputs": dependency_outputs,
                "executor_profile": child_profile,
                "executor_role": _role_from_profile(child_profile),
                "dag_node": {
                    "module_id": module_id,
                    "module_kind": str(dict(dag.get("module_kind") or {}).get(module_id) or ""),
                    "executor_profile": child_profile,
                    "default_executor_profile": str(dag_execution["default_executor_profile"]),
                },
            }
            repair_context = _repair_context_for_module(repair_overlay, module_id)
            if repair_context:
                child_metadata["repair_context"] = repair_context
            if isinstance(module_workspace.get("dependency_integration_baseline"), dict):
                child_metadata["module_dependency_integration"] = dict(module_workspace.get("dependency_integration_baseline") or {})
            child_metadata["dispatch_profile_group"] = child_profile_group
            child_metadata["dispatch_profile_name"] = child_profile_name
            if isinstance(metadata.get("plan_ref"), dict):
                child_metadata["plan_ref"] = dict(metadata.get("plan_ref") or {})
            if isinstance(metadata.get("plan_validation"), dict):
                child_metadata["plan_validation"] = dict(metadata.get("plan_validation") or {})
            if isinstance(metadata.get("requirements_brief"), dict):
                child_metadata["requirements_brief"] = dict(metadata.get("requirements_brief") or {})
            for key in ("profile_family", "architecture_mode", "requested_architecture_mode", "interaction_mode"):
                value = str(metadata.get(key) or "").strip()
                if value:
                    child_metadata[key] = value
            if isinstance(metadata.get("dag_producer"), dict):
                child_metadata["dag_producer"] = dict(metadata.get("dag_producer") or {})
            for key in (
                "control_route",
                "preferred_endpoint_id",
                "preferred_endpoint_source",
                "minion_debug_log_enabled",
                "debug_log",
                "prompt_observation_tag",
            ):
                if key in metadata:
                    child_metadata[key] = metadata[key]
            resumed_pack = self._resumable_plan_module_child_pack(
                child_work_order_id,
                module_id=module_id,
                workspace=module_workspace,
                metadata=child_metadata,
                profile_group=child_profile_group,
                profile_name=child_profile_name,
                minion_profile=child_profile,
            )
            if resumed_pack is not None:
                packs.append(resumed_pack)
                continue
            packs.append(
                self.build_coder_module_pack_from_plan(
                    artifact,
                    module_id=module_id,
                    work_order_id=child_work_order_id,
                    workspace=module_workspace,
                    metadata=child_metadata,
                    goal=f"{child_action['verb']} module {module_id}",
                    instruction=f"{child_action['verb']} module {module_id}; this is parent work-order milestone {milestone_index}.",
                    minion_profile=child_profile,
                )
            )
        plan_execution["child_work_order_ids"] = child_ids
        first_module_id = selected_modules[0]
        first_child_id = str(child_ids.get(first_module_id) or "")
        first_index = module_order.index(first_module_id) if first_module_id in module_order else 0
        plan_execution["current_module_index"] = int(first_index)
        plan_execution["current_module_id"] = first_module_id
        plan_execution["active_child_work_order_id"] = first_child_id
        plan_execution["active_child_work_order_ids"] = list(advanced.get("active_child_work_order_ids") or [])
        _set_plan_execution_dag_state(plan_execution, dag)
        plan_execution["status"] = str(advanced.get("status") or "running_module")
        metadata["plan_execution"] = plan_execution
        with self._connect() as db:
            wrote = self._write_plan_parent_metadata(
                db,
                str(work_order_id),
                metadata,
                expected_metadata_json=expected_metadata_json,
            )
            if not wrote:
                return []
        return packs

    def next_plan_module_pack(self, work_order_id: str, *, allow_paused: bool = False) -> TaskContextPack | None:
        packs = self.next_ready_plan_module_packs(work_order_id, limit=1, allow_paused=allow_paused)
        return packs[0] if packs else None

    def record_plan_module_completion(self, child_work_order_id: str, completion: dict[str, Any]) -> dict[str, Any]:
        child_snapshot = self.read_work_order(str(child_work_order_id))
        if child_snapshot.get("status") != "ok":
            return {"status": "skipped", "reason": "child_not_found"}
        child_work_order = dict(child_snapshot.get("work_order") or {})
        child_metadata = _loads_or_dict(child_work_order.get("metadata"))
        parent_work_order_id = str(child_metadata.get("parent_work_order_id") or "").strip()
        if not parent_work_order_id:
            return {"status": "skipped", "reason": "no_parent_work_order"}
        parent_milestone_index = _coerce_int(child_metadata.get("parent_milestone_index"))
        if parent_milestone_index is None:
            return {"status": "skipped", "reason": "no_parent_milestone_index"}
        parent_snapshot = self.read_work_order(parent_work_order_id)
        if parent_snapshot.get("status") != "ok":
            return {"status": "skipped", "reason": "parent_not_found", "parent_work_order_id": parent_work_order_id}
        parent_work_order = dict(parent_snapshot.get("work_order") or {})
        parent_metadata = _loads_or_dict(parent_work_order.get("metadata"))
        plan_execution = dict(parent_metadata.get("plan_execution") or {})
        if str(plan_execution.get("mode") or "") != "module_parent_milestones":
            return {"status": "skipped", "reason": "parent_not_plan_execution", "parent_work_order_id": parent_work_order_id}
        expected_parent_metadata_json = _json(parent_metadata)
        if str(child_work_order_id) in set(_coerce_text_list(plan_execution.get("invalidated_child_work_order_ids"))):
            return {
                "status": "skipped",
                "reason": "invalidated_child_work_order",
                "parent_work_order_id": parent_work_order_id,
                "child_work_order_id": str(child_work_order_id),
            }
        module_id = str(child_metadata.get("parent_module_id") or completion.get("module_id") or "").strip()
        plan_payload = parent_metadata.get("plan_artifact")
        if not isinstance(plan_payload, dict):
            return {"status": "skipped", "reason": "parent_missing_plan_artifact", "parent_work_order_id": parent_work_order_id}
        artifact = PlanArtifact.from_dict(plan_payload)
        validation = dispatchable_plan_validation(artifact)
        dag = _module_dag_from_validation(validation, _plan_execution_dag_state(plan_execution))
        dag = _claim_known_child_for_completion(
            dag,
            plan_execution,
            module_id=module_id,
            child_work_order_id=str(child_work_order_id),
        )
        preflight_completion = _dag_complete_module(dag, module_id, child_work_order_id=str(child_work_order_id))
        if not bool(preflight_completion.get("advanced")):
            return {
                "status": "skipped",
                "reason": str(preflight_completion.get("reason") or "module_completion_not_current"),
                "parent_work_order_id": parent_work_order_id,
                "child_work_order_id": str(child_work_order_id),
                "module_id": module_id,
            }
        summary = str(completion.get("summary") or f"Module {module_id} completed.").strip()
        created_at = utc_now()
        completion_report_ref: dict[str, Any] = {}
        with self._connect() as db:
            module_order = _coerce_text_list(plan_execution.get("module_order"))
            if not module_order:
                module_order = _coerce_text_list(dag.get("module_order"))
            child_workspace = _loads_or_dict(child_metadata.get("workspace"))
            child_output = _drop_empty_dict(
                {
                    "module_id": module_id,
                    "module_name": str(child_metadata.get("module_name") or completion.get("module_name") or module_id),
                    "child_work_order_id": str(child_work_order_id),
                    "repo_path": str(child_workspace.get("repo_path") or ""),
                    "branch": str(child_workspace.get("work_order_branch") or ""),
                    "work_order_branch": str(child_workspace.get("work_order_branch") or ""),
                    "commit_sha": str(completion.get("commit_sha") or completion.get("head_sha") or ""),
                    "completed_at": created_at,
                }
            )
            updated_parent_workspace = _serial_parent_workspace_after_module(
                parent_metadata.get("workspace"),
                child_workspace,
                module_id=module_id,
                module_name=str(child_metadata.get("module_name") or completion.get("module_name") or module_id),
                child_work_order_id=str(child_work_order_id),
            )
            if updated_parent_workspace:
                parent_metadata["workspace"] = updated_parent_workspace
                plan_execution["last_integrated_module_id"] = module_id
                plan_execution["last_integrated_child_work_order_id"] = str(child_work_order_id)
                plan_execution["last_integrated_repo_path"] = str(updated_parent_workspace.get("source_repo") or "")
            advanced = _dag_complete_module(dag, module_id, child_output=child_output, child_work_order_id=str(child_work_order_id))
            dag = dict(advanced.get("dag") or {})
            dag = _set_dag_node_cursor(
                dag,
                module_id,
                self._module_node_cursor_from_child(
                    db,
                    module_id=module_id,
                    child_work_order_id=str(child_work_order_id),
                    node_status="completed",
                    reason=summary,
                ),
            )
            ready_modules = list(advanced.get("ready_module_ids") or [])
            _set_plan_execution_dag_state(plan_execution, dag)
            plan_execution["completed_modules"] = list(advanced.get("completed_modules") or dag.get("completed_modules") or [])
            active_child_ids = list(advanced.get("active_child_work_order_ids") or [])
            plan_execution["active_child_work_order_ids"] = active_child_ids
            if len(active_child_ids) == 1:
                plan_execution["active_child_work_order_id"] = active_child_ids[0]
            else:
                plan_execution.pop("active_child_work_order_id", None)
            resolved_status = str(advanced.get("status") or _module_dag_status(dag))
            plan_execution["status"] = resolved_status
            next_module_id = str(advanced.get("next_module_id") or (ready_modules[0] if ready_modules else ""))
            next_index = module_order.index(next_module_id) if next_module_id in module_order else None
            if next_module_id:
                plan_execution["next_module_id"] = next_module_id
                plan_execution["current_module_index"] = int(next_index or 0)
            else:
                plan_execution.pop("next_module_id", None)
            if resolved_status == "completed":
                parent_status = "completed"
                cleanup = publish_completed_plan_workspace(
                    child_workspace,
                    module_outputs=[dict(value) for value in dict(dag.get("module_outputs") or {}).values() if isinstance(value, dict)],
                    final_repo_path=str(child_workspace.get("repo_path") or ""),
                )
                if str(cleanup.get("project_repo_path") or "").strip():
                    final_workspace = _persistent_workspace_metadata(parent_metadata.get("workspace") if isinstance(parent_metadata.get("workspace"), dict) else {})
                    final_workspace.update(
                        {
                            "repo_path": str(cleanup.get("project_repo_path") or ""),
                            "source_repo": str(cleanup.get("project_repo_path") or ""),
                            "runtime_project_path": str(cleanup.get("project_root") or cleanup.get("project_repo_path") or ""),
                            "work_order_repo_root": str(cleanup.get("project_root") or cleanup.get("project_repo_path") or ""),
                            "common_git_dir": str(cleanup.get("common_git_dir") or ""),
                            "work_order_branch": str(cleanup.get("work_order_branch") or ""),
                            "base_ref": str(cleanup.get("work_order_branch") or ""),
                            "merge_target": str(cleanup.get("merge_target") or ""),
                            "published_final_branch": str(cleanup.get("final_branch") or ""),
                            "published_final_repo_path": str(cleanup.get("final_repo_path") or ""),
                            "published_commit_sha": str(cleanup.get("commit_sha") or ""),
                        }
                    )
                    parent_metadata["workspace"] = _drop_empty_dict(final_workspace)
                parent_metadata["workspace_cleanup"] = cleanup
                plan_execution["workspace_cleanup"] = cleanup
                parent_metadata["plan_execution"] = plan_execution
                workflow = dict(parent_metadata.get("workflow") or {})
                if workflow:
                    output_artifact = {
                        "plan_ref": dict(parent_metadata.get("plan_ref") or {}),
                        "module_outputs": [dict(value) for value in dict(dag.get("module_outputs") or {}).values() if isinstance(value, dict)],
                        "workspace_cleanup": cleanup,
                    }
                    workflow = update_current_workflow_step(
                        workflow,
                        status="completed",
                        output_artifact=output_artifact,
                        next_profile=NONE_PROFILE,
                    )
                    workflow.update({"status": "completed", "updated_at": utc_now()})
                    parent_metadata["workflow"] = workflow
                completion_report_ref = _write_work_order_markdown_artifact(
                    self.runtime_root,
                    parent_work_order_id,
                    "completion_report.md",
                    title="Completion report",
                    role="completion",
                    content=_plan_completion_markdown(parent_work_order_id, parent_metadata, plan_execution),
                )
                parent_metadata["completion_report_ref"] = completion_report_ref
                plan_execution["completion_report_ref"] = completion_report_ref
            else:
                parent_status = "active"
            parent_metadata["plan_execution"] = plan_execution
            wrote = self._write_plan_parent_metadata(
                db,
                parent_work_order_id,
                parent_metadata,
                expected_metadata_json=expected_parent_metadata_json,
                work_order_status=parent_status,
            )
            if not wrote:
                return {
                    "status": "skipped",
                    "reason": "stale_dag_revision",
                    "parent_work_order_id": parent_work_order_id,
                    "child_work_order_id": str(child_work_order_id),
                    "module_id": module_id,
                }
            already_completed = self._fetch_one(
                db,
                """
                SELECT 1 FROM minion_worker_checkpoints
                WHERE work_order_id = ? AND milestone_index = ? AND status = 'completed'
                LIMIT 1
                """,
                (parent_work_order_id, int(parent_milestone_index)),
            )
            checkpoint_payload = {
                "status": "completed",
                "milestone_index": int(parent_milestone_index),
                "summary": summary,
                "module_id": module_id,
                "child_work_order_id": str(child_work_order_id),
                "child_completion": dict(completion),
            }
            if already_completed is None:
                self.ledger.insert_checkpoint(db, parent_work_order_id, checkpoint_payload, "", "", created_at)
                self.ledger.insert_ledger(db, parent_work_order_id, "module_checkpoint", summary, checkpoint_payload, "", "", created_at)
        return {
            "status": str(plan_execution.get("status") or ""),
            "parent_work_order_id": parent_work_order_id,
            "work_order_id": parent_work_order_id,
            "child_work_order_id": str(child_work_order_id),
            "parent_milestone_index": int(parent_milestone_index),
            "module_id": module_id,
            "has_next_module": bool(next_module_id),
            "next_module_id": next_module_id,
            "active_child_work_order_ids": list(plan_execution.get("active_child_work_order_ids") or []),
            "ready_module_ids": list(_plan_execution_dag_state(plan_execution).get("ready_modules") or []),
            "auto_advance_modules": bool(plan_execution.get("auto_advance_modules", True)),
            "summary": summary,
            **({"completion_report_ref": dict(completion_report_ref)} if completion_report_ref else {}),
            "metadata": (
                {"control_route": dict(parent_metadata.get("control_route") or {})}
                if isinstance(parent_metadata.get("control_route"), dict)
                else {}
            ),
        }

    def recover_stale_running_modules(
        self,
        *,
        active_child_work_order_ids: set[str] | list[str] | tuple[str, ...] | None = None,
        work_order_id: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        self.ensure_schema()
        active_ids = {str(item).strip() for item in list(active_child_work_order_ids or []) if str(item).strip()}
        target = str(work_order_id or "").strip()
        recovered: list[dict[str, Any]] = []
        active: list[dict[str, Any]] = []
        inspected = 0
        with self._connect() as db:
            rows = db.execute("SELECT * FROM minion_work_orders ORDER BY updated_at DESC").fetchall()
            for row in rows:
                work_order = dict(row)
                metadata = _loads_or_dict(work_order.get("metadata_json"))
                plan_execution = dict(metadata.get("plan_execution") or {})
                if str(plan_execution.get("mode") or "") != "module_parent_milestones":
                    continue
                if str(plan_execution.get("status") or "").strip().lower() != "running_module":
                    continue
                parent_id = str(work_order.get("work_order_id") or "")
                running_children = _plan_execution_running_children(plan_execution)
                targeted_children = {
                    module_id: child_id
                    for module_id, child_id in running_children.items()
                    if not target or target in {parent_id, child_id}
                }
                if target and not targeted_children and target != parent_id:
                    continue
                inspected += max(1, len(targeted_children))
                for module_id, child_id in targeted_children.items():
                    if child_id and child_id in active_ids:
                        active.append(
                            {
                                "parent_work_order_id": parent_id,
                                "module_id": module_id,
                                "active_child_work_order_id": child_id,
                                "status": "running_module",
                            }
                        )
                        continue
                    recovered.append(
                        self._release_running_module_parent(
                            db,
                            work_order,
                            metadata,
                            plan_execution,
                            child_work_order_id=child_id,
                            child_terminal_status="failed",
                            reason=reason or "manager recovered stale running module with no active child runner",
                        )
                    )
                    updated_row = self._fetch_one(db, "SELECT metadata_json FROM minion_work_orders WHERE work_order_id = ?", (parent_id,))
                    metadata = _loads_or_dict(updated_row["metadata_json"] if updated_row is not None else {})
                    plan_execution = dict(metadata.get("plan_execution") or {})
        status = "ok"
        if target and inspected == 0:
            status = "not_found"
        elif active and not recovered:
            status = "active_child_running"
        elif recovered:
            status = "recovered"
        return {
            "status": status,
            "work_order_id": target,
            "inspected_count": inspected,
            "recovered_count": len(recovered),
            "active_count": len(active),
            "recovered": recovered,
            "active": active,
        }

    def release_running_module_parent(
        self,
        work_order_id: str,
        *,
        child_terminal_status: str = "killed",
        reason: str = "",
    ) -> dict[str, Any]:
        self.ensure_schema()
        target = str(work_order_id or "").strip()
        if not target:
            return {"status": "invalid", "error": "work_order_id is required"}
        with self._connect() as db:
            rows = db.execute("SELECT * FROM minion_work_orders ORDER BY updated_at DESC").fetchall()
            for row in rows:
                work_order = dict(row)
                metadata = _loads_or_dict(work_order.get("metadata_json"))
                plan_execution = dict(metadata.get("plan_execution") or {})
                if str(plan_execution.get("mode") or "") != "module_parent_milestones":
                    continue
                if str(plan_execution.get("status") or "").strip().lower() != "running_module":
                    continue
                parent_id = str(work_order.get("work_order_id") or "")
                running_children = _plan_execution_running_children(plan_execution)
                child_id = ""
                if target == parent_id:
                    child_id = next(iter(running_children.values()), "")
                else:
                    child_id = next((value for value in running_children.values() if value == target), "")
                if not child_id:
                    continue
                released = self._release_running_module_parent(
                    db,
                    work_order,
                    metadata,
                    plan_execution,
                    child_work_order_id=child_id,
                    child_terminal_status=child_terminal_status,
                    reason=reason or "released running module parent",
                )
                return {**released, "status": "released", "parent_status": str(released.get("status") or "")}
        return {"status": "not_found", "work_order_id": target}

    def resume_plan_module(
        self,
        work_order_id: str,
        *,
        module_id: str = "",
        child_work_order_id: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        self.ensure_schema()
        target = str(work_order_id or "").strip()
        if not target:
            return {"status": "invalid", "error": "work_order_id is required"}
        with self._connect() as db:
            parent_row = self._fetch_one(db, "SELECT * FROM minion_work_orders WHERE work_order_id = ?", (target,))
            resolved_child_id = str(child_work_order_id or "").strip()
            resolved_module_id = str(module_id or "").strip()
            if parent_row is None:
                child_row = self._fetch_one(db, "SELECT * FROM minion_work_orders WHERE work_order_id = ?", (target,))
                if child_row is None:
                    return {"status": "not_found", "work_order_id": target}
                child_metadata = _loads_or_dict(child_row["metadata_json"])
                parent_id = str(child_metadata.get("parent_work_order_id") or "").strip()
                if not parent_id:
                    return {"status": "skipped", "reason": "not_plan_module_child", "work_order_id": target}
                parent_row = self._fetch_one(db, "SELECT * FROM minion_work_orders WHERE work_order_id = ?", (parent_id,))
                if parent_row is None:
                    return {"status": "not_found", "work_order_id": parent_id, "child_work_order_id": target}
                resolved_child_id = target
                resolved_module_id = resolved_module_id or str(child_metadata.get("parent_module_id") or child_metadata.get("module_id") or "").strip()
            parent = dict(parent_row)
            parent_id = str(parent.get("work_order_id") or target)
            parent_metadata = _loads_or_dict(parent.get("metadata_json"))
            plan_execution = dict(parent_metadata.get("plan_execution") or {})
            if str(plan_execution.get("mode") or "") != "module_parent_milestones":
                child_parent_id = str(parent_metadata.get("parent_work_order_id") or "").strip()
                if not child_parent_id:
                    return {"status": "skipped", "reason": "not_plan_parent", "work_order_id": parent_id}
                resolved_child_id = str(parent.get("work_order_id") or target)
                resolved_module_id = resolved_module_id or str(
                    parent_metadata.get("parent_module_id") or parent_metadata.get("module_id") or ""
                ).strip()
                parent_row = self._fetch_one(db, "SELECT * FROM minion_work_orders WHERE work_order_id = ?", (child_parent_id,))
                if parent_row is None:
                    return {"status": "not_found", "work_order_id": child_parent_id, "child_work_order_id": resolved_child_id}
                parent = dict(parent_row)
                parent_id = str(parent.get("work_order_id") or child_parent_id)
                parent_metadata = _loads_or_dict(parent.get("metadata_json"))
                plan_execution = dict(parent_metadata.get("plan_execution") or {})
                if str(plan_execution.get("mode") or "") != "module_parent_milestones":
                    return {"status": "skipped", "reason": "parent_not_plan_parent", "work_order_id": parent_id}
            expected_metadata_json = str(parent.get("metadata_json") or _json(parent_metadata))
            plan_payload = parent_metadata.get("plan_artifact")
            if not isinstance(plan_payload, dict):
                return {"status": "skipped", "reason": "parent_missing_plan_artifact", "work_order_id": parent_id}
            artifact = PlanArtifact.from_dict(plan_payload)
            validation = dispatchable_plan_validation(artifact)
            dag = _module_dag_from_validation(validation, _plan_execution_dag_state(plan_execution))
            child_ids = dict(plan_execution.get("child_work_order_ids") or {})
            if not resolved_child_id:
                released_child = str(plan_execution.get("last_released_child_work_order_id") or "").strip()
                if released_child:
                    resolved_child_id = released_child
            if not resolved_module_id and resolved_child_id:
                resolved_module_id = next(
                    (str(mid) for mid, cid in child_ids.items() if str(cid or "").strip() == resolved_child_id),
                    "",
                )
                if not resolved_module_id:
                    child_row = self._fetch_one(db, "SELECT metadata_json FROM minion_work_orders WHERE work_order_id = ?", (resolved_child_id,))
                    if child_row is not None:
                        child_metadata = _loads_or_dict(child_row["metadata_json"])
                        resolved_module_id = str(child_metadata.get("parent_module_id") or child_metadata.get("module_id") or "").strip()
            if not resolved_module_id:
                running_children = _plan_execution_running_children(plan_execution)
                if len(running_children) == 1:
                    resolved_module_id, resolved_child_id = next(iter(running_children.items()))
            if not resolved_module_id:
                blocked_candidates = [
                    module
                    for module, status in dict(dag.get("module_status") or {}).items()
                    if str(status or "").strip().lower() in {"blocked", "failed", "paused"}
                    and max(0, _coerce_int(dict(dag.get("remaining_indegree") or {}).get(module)) or 0) == 0
                ]
                if len(blocked_candidates) == 1:
                    resolved_module_id = blocked_candidates[0]
                    resolved_child_id = str(child_ids.get(resolved_module_id) or resolved_child_id or "").strip()
            if not resolved_module_id:
                return {"status": "invalid", "error": "module_id or child_work_order_id is required", "work_order_id": parent_id}
            if not resolved_child_id:
                resolved_child_id = str(child_ids.get(resolved_module_id) or "").strip()
            if not resolved_child_id:
                return {
                    "status": "invalid",
                    "error": "cannot resume module without existing child cursor",
                    "work_order_id": parent_id,
                    "module_id": resolved_module_id,
                }
            if resolved_child_id in set(_coerce_text_list(plan_execution.get("invalidated_child_work_order_ids"))):
                return {
                    "status": "skipped",
                    "reason": "invalidated_child_work_order",
                    "work_order_id": parent_id,
                    "module_id": resolved_module_id,
                    "child_work_order_id": resolved_child_id,
                }
            resumed = _dag_resume_blocked_module(dag, resolved_module_id)
            if not bool(resumed.get("advanced")):
                return {
                    "status": "skipped",
                    "reason": str(resumed.get("reason") or "module_not_resumable"),
                    "work_order_id": parent_id,
                    "module_id": resolved_module_id,
                    "child_work_order_id": resolved_child_id,
                }
            dag = dict(resumed.get("dag") or {})
            cursor = self._module_node_cursor_from_child(
                db,
                module_id=resolved_module_id,
                child_work_order_id=resolved_child_id,
                node_status="ready",
                reason=reason or "module resumed",
            )
            dag = _set_dag_node_cursor(dag, resolved_module_id, cursor)
            child_ids[resolved_module_id] = resolved_child_id
            plan_execution["child_work_order_ids"] = child_ids
            _set_plan_execution_dag_state(plan_execution, dag)
            plan_execution["status"] = str(resumed.get("status") or _module_dag_status(dag))
            plan_execution["status_reason"] = reason or "module resumed"
            plan_execution["active_child_work_order_ids"] = []
            plan_execution.pop("active_child_work_order_id", None)
            parent_metadata["plan_execution"] = plan_execution
            wrote = self._write_plan_parent_metadata(
                db,
                parent_id,
                parent_metadata,
                expected_metadata_json=expected_metadata_json,
                work_order_status="active",
            )
            if not wrote:
                return {
                    "status": "stale_dag_revision",
                    "work_order_id": parent_id,
                    "module_id": resolved_module_id,
                    "child_work_order_id": resolved_child_id,
                }
            payload = {
                "status": str(plan_execution.get("status") or ""),
                "summary": reason or f"resumed module {resolved_module_id}",
                "reason": "module_resume",
                "parent_work_order_id": parent_id,
                "module_id": resolved_module_id,
                "child_work_order_id": resolved_child_id,
                "node_cursor": cursor,
            }
            self.ledger.insert_ledger(db, parent_id, "module_resumed", str(payload["summary"]), payload, "", "", utc_now())
            return {
                "status": str(plan_execution.get("status") or ""),
                "work_order_id": parent_id,
                "parent_work_order_id": parent_id,
                "module_id": resolved_module_id,
                "child_work_order_id": resolved_child_id,
                "ready_module_ids": list(dag.get("ready_modules") or []),
                "node_cursor": cursor,
                "reason": reason,
            }

    def running_plan_module_child_work_order_ids(self) -> list[str]:
        self.ensure_schema()
        result: list[str] = []
        with self._connect() as db:
            rows = db.execute("SELECT metadata_json FROM minion_work_orders WHERE status IN ('active', 'running')").fetchall()
        for row in rows:
            metadata = _loads_or_dict(row["metadata_json"])
            plan_execution = dict(metadata.get("plan_execution") or {})
            if str(plan_execution.get("mode") or "") != "module_parent_milestones":
                continue
            if str(plan_execution.get("status") or "").strip().lower() != "running_module":
                continue
            result.extend(_plan_execution_running_children(plan_execution).values())
        return _dedupe_text(result)

    def ready_plan_parent_work_order_ids(self) -> list[str]:
        self.ensure_schema()
        result: list[str] = []
        with self._connect() as db:
            rows = db.execute(
                "SELECT work_order_id, metadata_json FROM minion_work_orders WHERE status IN ('active', 'running') ORDER BY updated_at ASC"
            ).fetchall()
        for row in rows:
            metadata = _loads_or_dict(row["metadata_json"])
            plan_execution = dict(metadata.get("plan_execution") or {})
            if str(plan_execution.get("mode") or "") != "module_parent_milestones":
                continue
            if str(plan_execution.get("status") or "").strip().lower() not in {"awaiting_continue", "running_module", "active"}:
                continue
            ready_modules = _coerce_text_list(_plan_execution_dag_state(plan_execution).get("ready_modules"))
            if ready_modules:
                result.append(str(row["work_order_id"] or ""))
        return _dedupe_text(result)

    def resumable_staged_module_detail_parent_work_order_ids(self) -> list[str]:
        self.ensure_schema()
        result: list[str] = []
        with self._connect() as db:
            rows = db.execute(
                "SELECT work_order_id, metadata_json FROM minion_work_orders "
                "WHERE status IN ('active', 'running') ORDER BY updated_at ASC"
            ).fetchall()
        for row in rows:
            metadata = _loads_or_dict(row["metadata_json"])
            staged = dict(metadata.get("staged_planning") or {})
            if str(staged.get("status") or "").strip().lower() != "module_detail_running":
                continue
            detail_modules = [
                dict(item)
                for item in dict(staged.get("detail_modules") or {}).values()
                if isinstance(item, dict)
            ]
            if not any(
                str(item.get("status") or "").strip().lower()
                in {"pending", "waiting_for_slot", "running"}
                for item in detail_modules
            ):
                continue
            result.append(str(row["work_order_id"] or ""))
        return _dedupe_text(result)

    def set_plan_parent_status(self, work_order_id: str, status: str, *, reason: str = "") -> dict[str, Any]:
        normalized = str(status or "").strip().lower()
        if normalized not in {"active", "awaiting_continue", "paused", "completed"}:
            raise ValueError(f"unsupported plan parent status: {status}")
        snapshot = self.read_work_order(str(work_order_id))
        if snapshot.get("status") != "ok":
            return {"status": "not_found", "work_order_id": str(work_order_id)}
        work_order = dict(snapshot.get("work_order") or {})
        metadata = _loads_or_dict(work_order.get("metadata"))
        plan_execution = dict(metadata.get("plan_execution") or {})
        if str(plan_execution.get("mode") or "") != "module_parent_milestones":
            return {"status": "skipped", "reason": "not_plan_parent", "work_order_id": str(work_order_id)}
        expected_metadata_json = _json(metadata)
        plan_execution["status"] = normalized
        if reason:
            plan_execution["status_reason"] = reason
        metadata["plan_execution"] = plan_execution
        work_order_status = "completed" if normalized == "completed" else "active"
        with self._connect() as db:
            wrote = self._write_plan_parent_metadata(
                db,
                str(work_order_id),
                metadata,
                expected_metadata_json=expected_metadata_json,
                work_order_status=work_order_status,
            )
            if not wrote:
                return {"status": "stale_dag_revision", "work_order_id": str(work_order_id), "reason": reason}
        return {"status": normalized, "work_order_id": str(work_order_id), "reason": reason}

    def block_plan_parent(self, work_order_id: str, *, reason: str = "", error: str = "") -> dict[str, Any]:
        snapshot = self.read_work_order(str(work_order_id))
        if snapshot.get("status") != "ok":
            return {"status": "not_found", "work_order_id": str(work_order_id)}
        work_order = dict(snapshot.get("work_order") or {})
        metadata = _loads_or_dict(work_order.get("metadata"))
        plan_execution = dict(metadata.get("plan_execution") or {})
        if str(plan_execution.get("mode") or "") != "module_parent_milestones":
            return {"status": "skipped", "reason": "not_plan_parent", "work_order_id": str(work_order_id)}
        summary = str(error or reason or "plan parent blocked").strip()
        plan_execution["status"] = "blocked"
        plan_execution["blocked_reason"] = str(reason or "dag_tick_failed")
        plan_execution["blocked_summary"] = summary
        plan_execution["blocked_at"] = utc_now()
        metadata["plan_execution"] = plan_execution
        expected_metadata_json = _json(_loads_or_dict(work_order.get("metadata")))
        with self._connect() as db:
            wrote = self._write_plan_parent_metadata(
                db,
                str(work_order_id),
                metadata,
                expected_metadata_json=expected_metadata_json,
                work_order_status="blocked",
            )
        if not wrote:
            return {"status": "stale_dag_revision", "work_order_id": str(work_order_id), "reason": reason, "error": error}
        return {
            "status": "blocked",
            "work_order_id": str(work_order_id),
            "reason": str(reason or "dag_tick_failed"),
            "error": str(error or ""),
            "summary": summary,
        }

    def submit_repair_bill(self, payload: dict[str, Any]) -> dict[str, Any]:
        last_result: dict[str, Any] = {}
        retry_reasons = {
            "parent_dag_changed_before_repair_bill_block",
            "parent_metadata_changed_before_repair_bill_record",
            "parent_dag_changed_before_repair_bill_replay",
        }
        for attempt in range(3):
            result = self._submit_repair_bill_once(payload)
            if str(result.get("status") or "") != "stale_dag_revision":
                if attempt > 0:
                    result = {**dict(result), "repair_bill_retry_count": attempt}
                return result
            last_result = dict(result)
            if str(result.get("reason") or "") not in retry_reasons:
                return result
        return {**last_result, "repair_bill_retry_exhausted": True}

    def _submit_repair_bill_once(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.ensure_schema()
        raw = dict(payload or {})
        parent_work_order_id = str(raw.get("parent_work_order_id") or raw.get("work_order_id") or "").strip()
        if not parent_work_order_id:
            return {"status": "invalid", "error": "parent_work_order_id is required"}
        with self._connect() as db:
            parent_row = self._fetch_one(db, "SELECT * FROM minion_work_orders WHERE work_order_id = ?", (parent_work_order_id,))
            if parent_row is None:
                return {"status": "not_found", "parent_work_order_id": parent_work_order_id}
            parent = dict(parent_row)
            metadata = _loads_or_dict(parent.get("metadata_json"))
            plan_execution = dict(metadata.get("plan_execution") or {})
            if str(plan_execution.get("mode") or "") != "module_parent_milestones":
                return {"status": "skipped", "reason": "not_plan_parent", "parent_work_order_id": parent_work_order_id}
            expected_metadata_json = str(parent.get("metadata_json") or _json(metadata))
            plan_payload = metadata.get("plan_artifact")
            if not isinstance(plan_payload, dict):
                return {"status": "skipped", "reason": "parent_missing_plan_artifact", "parent_work_order_id": parent_work_order_id}
            artifact = PlanArtifact.from_dict(plan_payload)
            validation = dispatchable_plan_validation(artifact)
            dag = _module_dag_from_validation(validation, _plan_execution_dag_state(plan_execution))
            module_order = _coerce_text_list(plan_execution.get("module_order")) or _coerce_text_list(dag.get("module_order"))
            if not module_order:
                module_order = [module.module_id for module in artifact.modules if module.module_id]
            bill = _normalize_repair_bill_payload(raw, parent_work_order_id=parent_work_order_id, module_order=module_order)
            unknown_modules = _coerce_text_list(bill.get("unknown_modules"))
            if unknown_modules:
                return {
                    "status": "triage_required",
                    "reason": "unknown_modules",
                    "parent_work_order_id": parent_work_order_id,
                    "unknown_modules": unknown_modules,
                    "bill": bill,
                }
            module_patches = {
                str(module_id): dict(patch)
                for module_id, patch in dict(bill.get("module_patches") or {}).items()
                if isinstance(patch, dict)
            }
            if not module_patches:
                return {
                    "status": "triage_required",
                    "reason": "empty_repair_bill",
                    "parent_work_order_id": parent_work_order_id,
                    "bill": bill,
                }
            now = utc_now()
            architecture_modules = [
                module_id
                for module_id, patch in module_patches.items()
                if str(patch.get("defect_kind") or "").strip().lower() == "architecture_defect"
            ]
            replay_targets = [
                module_id
                for module_id, patch in module_patches.items()
                if str(patch.get("defect_kind") or "").strip().lower() in _REPAIR_BILL_REPLAY_DEFECT_KINDS
            ]
            recorded_bills = [
                dict(item)
                for item in list(metadata.get("repair_bills") or [])
                if isinstance(item, dict)
            ]
            recorded_bills.append({**bill, "submitted_at": now})
            metadata["repair_bills"] = recorded_bills[-50:]
            if architecture_modules:
                current_epoch = max(0, _coerce_int(plan_execution.get("dag_epoch")) or 0)
                replacement_epoch = current_epoch + 1
                dag_for_block = _plan_execution_dag_state(plan_execution) or dict(dag)
                running_modules = dict(dag_for_block.get("running_modules") or {})
                active_child_ids = _dedupe_text(
                    [
                        *_coerce_text_list(plan_execution.get("active_child_work_order_ids")),
                        str(plan_execution.get("active_child_work_order_id") or ""),
                        *_coerce_text_list(running_modules.values()),
                    ]
                )
                if active_child_ids:
                    previous_invalidated = _coerce_text_list(plan_execution.get("invalidated_child_work_order_ids"))
                    plan_execution["invalidated_child_work_order_ids"] = _dedupe_text([*previous_invalidated, *active_child_ids])
                    plan_execution["last_invalidated_child_work_order_ids"] = active_child_ids
                    child_ids = dict(plan_execution.get("child_work_order_ids") or {})
                    invalidated_set = set(active_child_ids)
                    plan_execution["child_work_order_ids"] = {
                        module_id: child_id
                        for module_id, child_id in child_ids.items()
                        if str(child_id or "").strip() not in invalidated_set
                    }
                if dag_for_block:
                    module_order_for_block = _coerce_text_list(dag_for_block.get("module_order"))
                    module_status = dict(dag_for_block.get("module_status") or {})
                    for module_id in module_order_for_block:
                        if str(module_status.get(module_id) or "").strip().lower() != "completed":
                            module_status[module_id] = "blocked"
                    dag_for_block["module_status"] = module_status
                    dag_for_block["running_modules"] = {}
                    dag_for_block["ready_modules"] = []
                    _set_plan_execution_dag_state(plan_execution, dag_for_block)
                plan_execution["status"] = "blocked"
                plan_execution["status_reason"] = "architecture_defect"
                plan_execution["blocked_reason"] = "architecture_defect"
                plan_execution["blocked_summary"] = str(bill.get("summary") or "repair bill reported an architecture defect")
                plan_execution["restart_required"] = True
                plan_execution["requires_plan_review"] = True
                plan_execution["blocked_dag_epoch"] = current_epoch
                plan_execution["replacement_dag_epoch"] = replacement_epoch
                plan_execution["dag_epoch_status"] = "blocked_for_replacement"
                plan_execution["active_child_work_order_ids"] = []
                plan_execution.pop("active_child_work_order_id", None)
                plan_execution.pop("next_module_id", None)
                plan_execution["repair_triage"] = {
                    "status": "blocked_for_architecture",
                    "defect_kind": "architecture_defect",
                    "bill_id": str(bill.get("bill_id") or ""),
                    "modules": architecture_modules,
                    "summary": str(bill.get("summary") or ""),
                    "restart_required": True,
                    "requires_plan_review": True,
                    "current_dag_epoch": current_epoch,
                    "replacement_dag_epoch": replacement_epoch,
                    "next_step": "review the plan/module DAG boundaries, then start a replacement DAG epoch",
                    "submitted_at": now,
                }
                metadata["plan_execution"] = plan_execution
                wrote = self._write_plan_parent_metadata(
                    db,
                    parent_work_order_id,
                    metadata,
                    expected_metadata_json=expected_metadata_json,
                    work_order_status="blocked",
                )
                if not wrote:
                    return {
                        "status": "stale_dag_revision",
                        "parent_work_order_id": parent_work_order_id,
                        "bill_id": str(bill.get("bill_id") or ""),
                        "reason": "parent_dag_changed_before_repair_bill_block",
                    }
                ledger_payload = {
                    **bill,
                    "blocked_reason": "architecture_defect",
                    "architecture_modules": architecture_modules,
                    "restart_required": True,
                    "requires_plan_review": True,
                    "current_dag_epoch": current_epoch,
                    "replacement_dag_epoch": replacement_epoch,
                }
                if active_child_ids:
                    ledger_payload["invalidated_child_work_order_ids"] = active_child_ids
                self.ledger.insert_ledger(
                    db,
                    parent_work_order_id,
                    "repair_bill_submitted",
                    str(bill.get("summary") or "repair bill blocked current architecture"),
                    ledger_payload,
                    "",
                    "",
                    now,
                )
                result = {
                    "status": "blocked",
                    "reason": "architecture_defect",
                    "blocked_reason": "architecture_defect",
                    "parent_work_order_id": parent_work_order_id,
                    "bill_id": str(bill.get("bill_id") or ""),
                    "architecture_modules": architecture_modules,
                    "summary": str(bill.get("summary") or ""),
                    "restart_required": True,
                    "requires_plan_review": True,
                    "current_dag_epoch": current_epoch,
                    "replacement_dag_epoch": replacement_epoch,
                    "next_step": "review the plan/module DAG boundaries, then start a replacement DAG epoch",
                }
                if active_child_ids:
                    result["invalidated_child_work_order_ids"] = active_child_ids
                return result
            if not replay_targets:
                metadata["plan_execution"] = plan_execution
                wrote = self._write_plan_parent_metadata(
                    db,
                    parent_work_order_id,
                    metadata,
                    expected_metadata_json=expected_metadata_json,
                    bump_revision=False,
                )
                if not wrote:
                    return {
                        "status": "stale_dag_revision",
                        "parent_work_order_id": parent_work_order_id,
                        "bill_id": str(bill.get("bill_id") or ""),
                        "reason": "parent_metadata_changed_before_repair_bill_record",
                    }
                self.ledger.insert_ledger(db, parent_work_order_id, "repair_bill_submitted", str(bill.get("summary") or "repair bill recorded"), bill, "", "", now)
                return {
                    "status": "recorded",
                    "reason": "no_replay_defects",
                    "parent_work_order_id": parent_work_order_id,
                    "bill_id": str(bill.get("bill_id") or ""),
                    "summary": str(bill.get("summary") or ""),
                }
            overlay = _merge_repair_overlay(_repair_overlay_from_plan_execution(plan_execution), bill, replay_targets=replay_targets)
            plan_execution["repair_overlay"] = overlay
            replay = _dag_apply_repair_replay(
                dag,
                replay_targets,
                child_work_order_ids=dict(plan_execution.get("child_work_order_ids") or {}),
                replay_attempts=dict(plan_execution.get("module_replay_attempts") or {}),
                completed_modules=_coerce_text_list(plan_execution.get("completed_modules") or dag.get("completed_modules")),
            )
            dag = dict(replay.get("dag") or {})
            affected_modules = list(replay.get("affected_modules") or _affected_modules_for_repair(dag, replay_targets))
            child_ids = dict(replay.get("child_work_order_ids") or {})
            attempts = dict(replay.get("replay_attempts") or {})
            invalidated_child_ids = _coerce_text_list(replay.get("invalidated_child_work_order_ids"))
            if invalidated_child_ids:
                previous_invalidated = _coerce_text_list(plan_execution.get("invalidated_child_work_order_ids"))
                plan_execution["invalidated_child_work_order_ids"] = _dedupe_text([*previous_invalidated, *invalidated_child_ids])
                plan_execution["last_invalidated_child_work_order_ids"] = invalidated_child_ids
            _set_plan_execution_dag_state(plan_execution, dag)
            plan_execution["completed_modules"] = list(replay.get("completed_modules") or dag.get("completed_modules") or [])
            plan_execution["child_work_order_ids"] = child_ids
            plan_execution["module_replay_attempts"] = attempts
            plan_execution["active_child_work_order_ids"] = list(replay.get("active_child_work_order_ids") or [])
            if len(plan_execution["active_child_work_order_ids"]) == 1:
                plan_execution["active_child_work_order_id"] = plan_execution["active_child_work_order_ids"][0]
            else:
                plan_execution.pop("active_child_work_order_id", None)
            next_module_id = str(replay.get("next_module_id") or "")
            if next_module_id:
                plan_execution["next_module_id"] = next_module_id
                plan_execution["current_module_index"] = module_order.index(next_module_id) if next_module_id in module_order else 0
            else:
                plan_execution.pop("next_module_id", None)
            plan_execution["status"] = str(replay.get("status") or _module_dag_status(dag))
            metadata["plan_execution"] = plan_execution
            affected_indexes = [module_order.index(module_id) for module_id in affected_modules if module_id in module_order]
            wrote = self._write_plan_parent_metadata(
                db,
                parent_work_order_id,
                metadata,
                expected_metadata_json=expected_metadata_json,
                work_order_status="active",
            )
            if not wrote:
                return {
                    "status": "stale_dag_revision",
                    "parent_work_order_id": parent_work_order_id,
                    "bill_id": str(bill.get("bill_id") or ""),
                    "reason": "parent_dag_changed_before_repair_bill_replay",
                }
            if affected_indexes:
                placeholders = ",".join("?" for _ in affected_indexes)
                db.execute(
                    f"""
                    UPDATE minion_worker_checkpoints
                    SET status = 'stale'
                    WHERE work_order_id = ?
                      AND milestone_index IN ({placeholders})
                      AND status = 'completed'
                    """,
                    (parent_work_order_id, *affected_indexes),
                )
            ledger_payload = {
                **bill,
                "replay_targets": replay_targets,
                "affected_modules": affected_modules,
                "repair_overlay_version": int(overlay.get("version") or 0),
                "ready_modules": list(dag.get("ready_modules") or []),
            }
            if invalidated_child_ids:
                ledger_payload["invalidated_child_work_order_ids"] = invalidated_child_ids
            self.ledger.insert_ledger(db, parent_work_order_id, "repair_bill_submitted", str(bill.get("summary") or "repair bill submitted"), ledger_payload, "", "", now)
            result = {
                "status": str(plan_execution.get("status") or "awaiting_continue"),
                "parent_work_order_id": parent_work_order_id,
                "bill_id": str(bill.get("bill_id") or ""),
                "repair_overlay_version": int(overlay.get("version") or 0),
                "replay_targets": replay_targets,
                "affected_modules": affected_modules,
                "ready_module_ids": list(dag.get("ready_modules") or []),
                "next_module_id": next_module_id,
                "summary": str(bill.get("summary") or ""),
            }
            if invalidated_child_ids:
                result["invalidated_child_work_order_ids"] = invalidated_child_ids
            return result

    def next_serial_module_pack(self, work_order_id: str) -> TaskContextPack | None:
        snapshot = self.read_work_order(str(work_order_id))
        if snapshot.get("status") != "ok":
            return None
        work_order = dict(snapshot.get("work_order") or {})
        metadata = _loads_or_dict(work_order.get("metadata"))
        module_execution = dict(metadata.get("module_execution") or {})
        mode = str(module_execution.get("mode") or "")
        if mode not in SERIAL_MILESTONE_MODES:
            return None
        if not bool(module_execution.get("auto_advance")):
            return None
        if str(module_execution.get("status") or "").strip().lower() == "completed":
            return None
        current_milestone = dict(snapshot.get("current_milestone") or {})
        if not current_milestone:
            return None
        milestone_index = _coerce_int(current_milestone.get("milestone_index"))
        if milestone_index is None:
            return None
        plan_payload = metadata.get("plan_artifact")
        if not isinstance(plan_payload, dict):
            raise ValueError("serial milestone execution requires plan_artifact")
        artifact = PlanArtifact.from_dict(plan_payload)
        module_id = str(module_execution.get("module_id") or metadata.get("module_id") or "").strip()
        if not module_id:
            module_id = plan_module_id_at(artifact)
        milestone_id = plan_milestone_id_at(artifact, module_id=module_id, milestone_index=milestone_index)
        workspace = _loads_or_dict(metadata.get("workspace"))
        repair_context = _loads_or_dict(metadata.get("repair_context"))
        if repair_context:
            workspace.setdefault("repair_context", repair_context)
        profile = str(work_order.get("minion_profile") or metadata.get("minion_profile") or "generic")
        execution_contract = dict(module_execution.get("execution_contract") or metadata.get("execution_contract") or {})
        module_execution["current_milestone_index"] = int(milestone_index)
        module_execution["profile_role"] = _module_role_from_contract(execution_contract, fallback_profile=profile)
        module_execution["status"] = "active"
        module_execution.setdefault("module_name", str(metadata.get("module_name") or module_id).strip())
        metadata["module_execution"] = module_execution
        metadata.pop("prompt_view", None)
        metadata.pop("coder_work_order", None)
        acceptance_criteria = _coerce_text_list(metadata.get("acceptance_criteria"))
        pack_payload = {
            "work_order_id": str(work_order_id),
            "goal": str(work_order.get("goal") or ""),
            "instruction": str(work_order.get("instruction") or work_order.get("goal") or ""),
            "acceptance_criteria": acceptance_criteria,
            "workspace": workspace,
            "artifacts": _artifact_list(metadata.get("artifacts")),
            "minion_profile": str(work_order.get("minion_profile") or profile or "generic"),
            "metadata": metadata,
        }
        next_pack = TaskContextPack.from_dict(pack_payload)
        metadata = _materialize_plan_module_adapter(next_pack, metadata=metadata, workspace=workspace)
        return TaskContextPack.from_dict({**pack_payload, "metadata": metadata})

    def next_serial_module_turn(self, work_order_id: str) -> dict[str, Any] | None:
        pack = self.next_serial_module_pack(work_order_id)
        if pack is None:
            return None
        prepared = self.prepare_pack_for_spawn(pack)
        return build_minion_turn_from_pack(prepared)

    def mark_serial_module_completed(self, work_order_id: str) -> dict[str, Any]:
        snapshot = self.read_work_order(str(work_order_id))
        if snapshot.get("status") != "ok":
            return {"status": "not_found", "work_order_id": str(work_order_id)}
        if snapshot.get("current_milestone"):
            return {"status": "active", "work_order_id": str(work_order_id)}
        work_order = dict(snapshot.get("work_order") or {})
        task = dict(snapshot.get("task") or {})
        metadata = _loads_or_dict(work_order.get("metadata"))
        module_execution = dict(metadata.get("module_execution") or {})
        if str(module_execution.get("mode") or "") not in SERIAL_MILESTONE_MODES:
            return {"status": "skipped", "reason": "not_serial_module", "work_order_id": str(work_order_id)}
        if bool(module_execution.get("completion_reported")):
            return {"status": "already_completed", "work_order_id": str(work_order_id)}
        pending = _experience_payload(module_execution.get("pending_experience"))
        module_execution["status"] = "completed"
        module_execution["completed_at"] = utc_now()
        module_execution["completion_reported"] = True
        metadata["module_execution"] = module_execution
        with self._connect() as db:
            db.execute(
                "UPDATE minion_work_orders SET metadata_json = ?, updated_at = ? WHERE work_order_id = ?",
                (_json(metadata), utc_now(), str(work_order_id)),
            )
            self._update_work_order_status(db, str(work_order_id), "completed")
        module_id = str(module_execution.get("module_id") or metadata.get("module_id") or "").strip()
        module_name = str(module_execution.get("module_name") or metadata.get("module_name") or module_id).strip()
        completed_count = len([item for item in list(snapshot.get("milestones") or []) if item.get("completed")])
        return {
            "status": "completed",
            "work_order_id": str(work_order_id),
            "task_id": str(task.get("task_id") or work_order.get("task_id") or ""),
            "module_id": module_id,
            "module_name": module_name,
            "summary": f"Module {module_name or module_id or work_order_id} completed {completed_count} milestone(s).",
            "completed_milestone_count": completed_count,
            "metadata": {"control_route": dict(metadata.get("control_route") or {})} if isinstance(metadata.get("control_route"), dict) else {},
            **pending,
        }

    def _hydrate_pack_from_work_order(self, pack: TaskContextPack) -> TaskContextPack:
        try:
            stored = self.pack_for_work_order(pack.work_order_id)
        except Exception:
            return pack
        return _merge_pack_overrides(stored, pack.to_dict())

    def ensure_work_order_from_pack(self, pack: TaskContextPack) -> dict[str, Any]:
        self.ensure_schema()
        now = utc_now()
        metadata = dict(pack.metadata)
        if pack.workspace:
            metadata["workspace"] = dict(pack.workspace)
        if pack.artifacts:
            metadata["artifacts"] = [dict(item) for item in pack.artifacts]
        if pack.acceptance_criteria:
            metadata["acceptance_criteria"] = list(pack.acceptance_criteria)
        metadata = _normalize_plan_backed_milestones(metadata)
        milestones = _plan_truth_milestones(metadata, pack.acceptance_criteria, pack.instruction or pack.goal)
        metadata = _normalize_milestone_execution_metadata(metadata, milestones)
        profile_family = _profile_family_from_metadata(
            metadata,
            fallback_group=str(pack.profile_group or ""),
            fallback_profile=str(pack.minion_profile or ""),
        )
        metadata["profile_family"] = profile_family
        workflow = dict(metadata.get("workflow") or {})
        workflow.setdefault("profile_family", profile_family)
        metadata["workflow"] = workflow
        with self._connect() as db:
            existing = self._fetch_one(db, "SELECT * FROM minion_work_orders WHERE work_order_id = ?", (pack.work_order_id,))
            if existing is not None:
                existing_metadata = _loads(existing["metadata_json"])
                existing_was_plan_parent = _is_module_parent_execution_metadata(existing_metadata)
                stored_metadata = existing_metadata
                stored_metadata = _merge_existing_work_order_metadata(stored_metadata, metadata)
                stored_metadata = _normalize_plan_backed_milestones(stored_metadata)
                stored_milestones = _plan_truth_milestones(
                    stored_metadata,
                    pack.acceptance_criteria,
                    pack.instruction or pack.goal,
                )
                stored_metadata = _normalize_milestone_execution_metadata(stored_metadata, stored_milestones)
                stored_metadata = _strip_raw_milestone_metadata_without_plan(stored_metadata)
                next_goal = str(pack.goal or existing["goal"] or "").strip()
                next_instruction = str(pack.instruction or existing["instruction"] or next_goal).strip()
                next_title = str(
                    metadata.get("work_order_title")
                    or metadata.get("task_title")
                    or existing["title"]
                    or next_goal
                    or next_instruction
                ).strip()
                next_profile = str(pack.minion_profile or existing["minion_profile"] or "generic").strip() or "generic"
                next_profile_group = str(pack.profile_group or existing["profile_group"] or "general").strip() or "general"
                next_profile_name = str(pack.profile_name or existing["profile_name"] or "generic").strip() or "generic"
                db.execute(
                    """
                    UPDATE minion_work_orders
                    SET title = ?, goal = ?, instruction = ?, status = ?, ended_at = ?,
                        minion_profile = ?, profile_group = ?, profile_name = ?, metadata_json = ?, updated_at = ?
                    WHERE work_order_id = ?
                    """,
                    (
                        next_title[:160],
                        next_goal,
                        next_instruction,
                        "active",
                        "",
                        next_profile,
                        next_profile_group,
                        next_profile_name,
                        _json(stored_metadata),
                        now,
                        pack.work_order_id,
                    ),
                )
                task_id = str(existing["task_id"])
                self._sync_task_fts(db, task_id)
                self._sync_work_order_fts(db, pack.work_order_id)
                if _is_module_parent_execution_metadata(stored_metadata) and not existing_was_plan_parent:
                    self._reset_work_order_execution_records(db, pack.work_order_id)
                self._ensure_milestones(db, pack.work_order_id, stored_milestones)
                return dict(existing)
            metadata = _strip_raw_milestone_metadata_without_plan(metadata)
            task_id = str(metadata.get("task_id") or new_work_id("task")).strip()
            title = str(metadata.get("task_title") or pack.goal or pack.instruction or task_id).strip()
            task = self._fetch_one(db, "SELECT * FROM minion_tasks WHERE task_id = ?", (task_id,))
            if task is None:
                task_metadata = _loads_or_dict(metadata.get("task_metadata"))
                task_metadata.setdefault("profile_family", profile_family)
                db.execute(
                    """
                    INSERT INTO minion_tasks(task_id, title, goal, summary, status, profile_family, metadata_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        title[:160],
                        pack.goal or pack.instruction,
                        str(metadata.get("task_summary") or ""),
                        "active",
                        profile_family,
                        _json(task_metadata),
                        now,
                        now,
                    ),
                )
            else:
                task_family = str(task["profile_family"] or "general").strip() or "general"
                task_metadata = _loads(task["metadata_json"])
                explicit_task_family = str(task_metadata.get("profile_family") or "").strip()
                if task_family == "general" and profile_family != "general" and not explicit_task_family:
                    task_metadata["profile_family"] = profile_family
                    db.execute(
                        """
                        UPDATE minion_tasks
                        SET profile_family = ?, metadata_json = ?, updated_at = ?
                        WHERE task_id = ?
                        """,
                        (profile_family, _json(task_metadata), now, task_id),
                    )
                elif task_family != profile_family:
                    raise ValueError(f"task {task_id} uses profile_family {task_family}; work order requested {profile_family}")
            active_rows = db.execute(
                """
                SELECT work_order_id, metadata_json FROM minion_work_orders
                WHERE task_id = ? AND status IN ('active', 'running', 'blocked', 'approval_pending')
                """,
                (task_id,),
            ).fetchall()
            allowed_parent_id = str(metadata.get("parent_work_order_id") or "").strip()
            blocking_active_ids = [
                str(row["work_order_id"])
                for row in active_rows
                if str(row["work_order_id"]) != allowed_parent_id
                and (
                    not allowed_parent_id
                    or str(_loads_or_dict(row["metadata_json"]).get("parent_work_order_id") or "").strip() != allowed_parent_id
                )
            ]
            if blocking_active_ids:
                active_work_order_id = blocking_active_ids[0]
                if not _allow_plan_revision_with_active_source(metadata, active_work_order_id):
                    raise ValueError(f"task already has an active work order: {active_work_order_id}")
            db.execute(
                """
                INSERT INTO minion_work_orders(
                    work_order_id, task_id, title, goal, instruction, status, minion_profile,
                    profile_group, profile_name, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pack.work_order_id,
                    task_id,
                    str(metadata.get("work_order_title") or title)[:160],
                    pack.goal,
                    pack.instruction,
                    "active",
                    pack.minion_profile,
                    pack.profile_group,
                    pack.profile_name,
                    _json(metadata),
                    now,
                    now,
                ),
            )
            self._ensure_milestones(db, pack.work_order_id, milestones)
            self._sync_task_fts(db, task_id)
            self._sync_work_order_fts(db, pack.work_order_id)
            return self._fetch_one(db, "SELECT * FROM minion_work_orders WHERE work_order_id = ?", (pack.work_order_id,)) or {}

    def update_work_order_workspace(self, work_order_id: str, workspace: dict[str, Any]) -> None:
        self.ensure_schema()
        with self._connect() as db:
            row = self._fetch_one(db, "SELECT metadata_json FROM minion_work_orders WHERE work_order_id = ?", (str(work_order_id),))
            if row is None:
                return
            metadata = _loads(row["metadata_json"])
            metadata["workspace"] = _persistent_workspace_metadata(workspace)
            metadata = _strip_raw_milestone_metadata_without_plan(metadata)
            db.execute(
                "UPDATE minion_work_orders SET metadata_json = ?, updated_at = ? WHERE work_order_id = ?",
                (_json(metadata), utc_now(), str(work_order_id)),
            )

    def merge_work_order_metadata(
        self,
        work_order_id: str,
        updates: dict[str, Any],
        *,
        work_order_status: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_schema()
        normalized = str(work_order_id or "").strip()
        if not normalized:
            return {"status": "invalid", "error": "work_order_id is required"}
        with self._connect() as db:
            row = self._fetch_one(db, "SELECT metadata_json FROM minion_work_orders WHERE work_order_id = ?", (normalized,))
            if row is None:
                return {"status": "not_found", "work_order_id": normalized}
            metadata = _loads(row["metadata_json"])
            metadata = _deep_merge_dict(metadata, dict(updates or {}))
            metadata = _strip_raw_milestone_metadata_without_plan(metadata)
            db.execute(
                "UPDATE minion_work_orders SET metadata_json = ?, updated_at = ? WHERE work_order_id = ?",
                (_json(metadata), utc_now(), normalized),
            )
            if work_order_status:
                self._update_work_order_status(db, normalized, str(work_order_status))
        return {"status": "ok", "work_order_id": normalized, "metadata": metadata}

    def create_work_order_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.ensure_schema()
        now = utc_now()
        draft_id = str(payload.get("draft_id") or f"wod_{uuid4().hex[:16]}").strip()
        title = _clip_text(payload.get("title") or payload.get("work_order_title") or payload.get("goal") or "Work order draft", 160)
        goal = _clip_text(payload.get("goal") or payload.get("instruction") or title, _WORK_ORDER_DRAFT_TEXT_LIMIT)
        source_summary = _clip_text(payload.get("source_summary") or payload.get("conversation_summary") or "", _WORK_ORDER_DRAFT_TEXT_LIMIT)
        task_id = str(payload.get("task_id") or new_work_id("task")).strip()
        proposed_work_order_id = str(payload.get("proposed_work_order_id") or payload.get("work_order_id") or new_work_id("wo")).strip()
        minion_profile = str(payload.get("minion_profile") or "").strip()
        if not minion_profile:
            raise ValueError("create_work_order_draft requires an explicit minion_profile")
        acceptance = _coerce_text_list(payload.get("acceptance_criteria"))
        milestones = _coerce_milestones(payload.get("milestones"), acceptance, goal)
        workspace = _loads_or_dict(payload.get("workspace"))
        artifacts = [dict(item) for item in list(payload.get("artifacts") or []) if isinstance(item, dict)]
        boundaries = payload.get("module_boundaries", payload.get("boundaries"))
        metadata = _compact_work_order_draft_metadata(_loads_or_dict(payload.get("metadata")))
        instruction = _clip_text(payload.get("instruction") or goal, _WORK_ORDER_DRAFT_TEXT_LIMIT)
        candidate = {
            "work_order_id": proposed_work_order_id,
            "task_id": task_id,
            "title": title,
            "goal": goal,
            "instruction": instruction,
            "minion_profile": minion_profile,
            "acceptance_criteria": acceptance,
            "milestones": milestones,
            "workspace": workspace,
            "artifacts": artifacts,
            "metadata": {
                **metadata,
                "task_id": task_id,
                "task_title": str(payload.get("task_title") or title),
                "work_order_title": title,
                "work_order_draft_id": draft_id,
                "milestones": milestones,
                "source_summary": source_summary,
                "module_boundaries": boundaries,
            },
        }
        draft_payload = {
            "source_summary": source_summary,
            "conversation_summary": _clip_text(payload.get("conversation_summary") or source_summary, _WORK_ORDER_DRAFT_TEXT_LIMIT),
            "module_boundaries": boundaries,
            "acceptance_criteria": acceptance,
            "milestones": milestones,
            "workspace": workspace,
            "artifacts": artifacts,
            "metadata": metadata,
            "work_order_candidate": candidate,
            "planner_review": {
                "draft_id": draft_id,
                "minion_profile": minion_profile,
                "instruction": (
                    "Review this work-order draft. Tighten module boundaries, milestones, acceptance criteria, "
                    "and risks. Do not invent new scope from chat history; use only this draft and explicit facts."
                ),
            },
        }
        with self._connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO minion_work_order_drafts(
                    draft_id, title, goal, source_summary, status, minion_profile, task_id,
                    proposed_work_order_id, payload_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft_id,
                    title[:160],
                    goal,
                    source_summary,
                    "draft",
                    minion_profile,
                    task_id,
                    proposed_work_order_id,
                    _json(draft_payload),
                    now,
                    now,
                ),
            )
            self._sync_work_order_draft_fts(db, draft_id)
        return self.read_work_order_draft(draft_id)

    def promote_work_order_draft(self, draft_id: str, *, reviewed_candidate: dict[str, Any] | None = None) -> dict[str, Any]:
        draft_snapshot = self.read_work_order_draft(draft_id)
        if draft_snapshot.get("status") == "not_found":
            raise KeyError(f"unknown work order draft: {draft_id}")
        draft = dict(draft_snapshot.get("draft") or {})
        base_candidate = dict(draft_snapshot.get("work_order_candidate") or {})
        candidate = _merge_work_order_candidate(base_candidate, dict(reviewed_candidate or {}))
        if not candidate:
            raise ValueError("work order draft has no candidate to promote")
        pack = _pack_from_work_order_candidate(candidate, draft=draft)
        prepared = self.prepare_pack_for_spawn(pack)
        now = utc_now()
        with self._connect() as db:
            row = self._fetch_one(db, "SELECT payload_json FROM minion_work_order_drafts WHERE draft_id = ?", (str(draft_id),))
            payload = _loads(row["payload_json"]) if row is not None else {}
            payload["promoted_at"] = now
            payload["promoted_work_order_id"] = prepared.work_order_id
            payload["promoted_task_context_pack"] = prepared.to_dict()
            if reviewed_candidate is not None:
                payload["reviewed_candidate"] = dict(reviewed_candidate)
            db.execute(
                """
                UPDATE minion_work_order_drafts
                SET status = 'promoted', proposed_work_order_id = ?, payload_json = ?, updated_at = ?
                WHERE draft_id = ?
                """,
                (prepared.work_order_id, _json(payload), now, str(draft_id)),
            )
            self._sync_work_order_draft_fts(db, str(draft_id))
        return {
            "status": "ok",
            "draft": self.read_work_order_draft(str(draft_id)).get("draft") or {},
            "task_context_pack": prepared.to_dict(),
            "work_order_snapshot": self.read_work_order(prepared.work_order_id),
        }

    def record_minion_event(self, event: dict[str, Any]) -> None:
        self.ensure_schema()
        event_kind = str(event.get("event_kind") or "")
        work_order_id = str(event.get("work_order_id") or "")
        if not work_order_id:
            return
        payload = dict(event.get("payload") or {})
        created_at = str(event.get("created_at") or utc_now())
        minion_id = str(event.get("minion_id") or payload.get("minion_id") or "")
        run_id = str(event.get("run_id") or payload.get("run_id") or "")
        summary = str(payload.get("summary") or payload.get("title") or event_kind)
        with self._connect() as db:
            event_log_enabled = _work_order_event_log_enabled(self, db, work_order_id, payload)
            if event_log_enabled:
                self.ledger.insert_ledger(db, work_order_id, event_kind, summary, payload, minion_id, run_id, created_at)
            if event_kind == "checkpoint":
                self.ledger.insert_checkpoint(db, work_order_id, payload, minion_id, run_id, created_at)
                self._record_payload_artifacts(db, work_order_id, payload)
            elif event_kind == "terminal":
                self.ledger.record_terminal(db, work_order_id, payload, minion_id, run_id, created_at)
                self._record_deferred_experience(db, work_order_id, payload)
                self._record_payload_artifacts(db, work_order_id, payload)
            elif event_kind == "module_completed":
                event_status = str(payload.get("status") or "").strip().lower()
                self._update_work_order_status(db, work_order_id, "completed" if event_status == "completed" else "active")
                self._record_payload_artifacts(db, work_order_id, payload)
            elif event_kind == "work_order_completed":
                self._update_work_order_status(db, work_order_id, "completed")
                self._record_deferred_experience(db, work_order_id, payload)
                self._record_payload_artifacts(db, work_order_id, payload)
            elif event_kind == "milestone_completed":
                self._record_deferred_experience(db, work_order_id, payload)
                self._record_payload_artifacts(db, work_order_id, payload)
            elif event_kind in {"phase_started", "progress"}:
                if not event_log_enabled:
                    return
                status = "running" if event_kind == "phase_started" else None
                if status:
                    self._update_work_order_status(db, work_order_id, status)

    def absorb_lessons(
        self,
        work_order_id: str,
        *,
        task_lessons: list[str] | tuple[str, ...] | None = None,
        system_lessons: list[str] | tuple[str, ...] | None = None,
        minion_id: str = "",
        run_id: str = "",
        system_status: str = "accepted",
    ) -> dict[str, Any]:
        self.ensure_schema()
        normalized_work_order_id = str(work_order_id or "").strip()
        if not normalized_work_order_id:
            return {"status": "invalid", "error": "work_order_id is required"}
        task_items = _string_list(task_lessons or [])
        system_items = _string_list(system_lessons or [])
        if not task_items and not system_items:
            return {"status": "ok", "task_lesson_count": 0, "system_lesson_count": 0}
        created_at = utc_now()
        with self._connect() as db:
            work_order = self._fetch_one(db, "SELECT task_id FROM minion_work_orders WHERE work_order_id = ?", (normalized_work_order_id,))
            if work_order is None:
                return {"status": "not_found", "error": "work_order not found"}
            task_id = str(work_order["task_id"] or "")
            task_count = 0
            system_count = 0
            for lesson in task_items:
                if self._lesson_exists(db, "minion_task_lessons", normalized_work_order_id, lesson):
                    continue
                db.execute(
                    """
                    INSERT INTO minion_task_lessons(lesson_id, task_id, work_order_id, lesson_text, minion_id, run_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (f"tls_{uuid4().hex[:16]}", task_id, normalized_work_order_id, lesson, minion_id, run_id, created_at),
                )
                task_count += 1
            for lesson in system_items:
                if self._lesson_exists(db, "minion_system_lesson_candidates", normalized_work_order_id, lesson):
                    continue
                db.execute(
                    """
                    INSERT INTO minion_system_lesson_candidates(
                        candidate_id, task_id, work_order_id, lesson_text, status, minion_id, run_id, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (f"sls_{uuid4().hex[:16]}", task_id, normalized_work_order_id, lesson, system_status, minion_id, run_id, created_at),
                )
                system_count += 1
        return {"status": "ok", "task_lesson_count": task_count, "system_lesson_count": system_count}

    def record_clarification_answer(self, work_order_id: str, answer: dict[str, Any]) -> dict[str, Any]:
        self.ensure_schema()
        normalized_work_order_id = str(work_order_id or "").strip()
        if not normalized_work_order_id:
            return {"status": "invalid", "error": "work_order_id is required"}
        item = dict(answer or {})
        item.setdefault("created_at", utc_now())
        with self._connect() as db:
            row = self._fetch_one(db, "SELECT metadata_json FROM minion_work_orders WHERE work_order_id = ?", (normalized_work_order_id,))
            if row is None:
                return {"status": "not_found", "error": "work_order not found"}
            metadata = _loads(row["metadata_json"])
            answers = [dict(existing) for existing in list(metadata.get("clarification_answers") or []) if isinstance(existing, dict)]
            answers.append(item)
            metadata["clarification_answers"] = answers[-50:]
            db.execute(
                "UPDATE minion_work_orders SET metadata_json = ?, updated_at = ? WHERE work_order_id = ?",
                (_json(metadata), utc_now(), normalized_work_order_id),
            )
        return {"status": "ok", "work_order_id": normalized_work_order_id, "answer": item}

    def build_continuity(self, work_order_id: str) -> dict[str, Any]:
        snapshot = self.read_work_order(work_order_id)
        milestones = list(snapshot.get("milestones") or [])
        current_milestone = _compact_milestone_for_continuity(snapshot.get("current_milestone") or {})
        if current_milestone and milestones:
            current_milestone["milestone_count"] = len(milestones)
        return {
            "task_id": str((snapshot.get("task") or {}).get("task_id") or ""),
            "work_order_id": work_order_id,
            "current_milestone": current_milestone,
            "completed_milestones": [
                _compact_milestone_for_continuity(item)
                for item in milestones
                if item.get("completed")
            ],
            "latest_checkpoint": _compact_event_for_continuity(snapshot.get("latest_checkpoint") or {}),
            "latest_completed_checkpoint": _compact_event_for_continuity(snapshot.get("latest_completed_checkpoint") or {}),
            "recent_ledger": [
                _compact_event_for_continuity(item)
                for item in list(snapshot.get("recent_ledger") or [])[:_CONTINUITY_LEDGER_LIMIT]
            ],
            "task_lessons": list(snapshot.get("task_lessons") or []),
        }

    def search_tasks(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as db:
            items = self.search.search_fts(
                db,
                table_name="minion_tasks_fts",
                id_column="task_id",
                query=query,
                limit=limit,
                fallback_sql="""
                    SELECT task_id, 1.0 AS score FROM minion_tasks
                    WHERE lower(title || ' ' || goal || ' ' || summary) LIKE ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                """,
            )
            return {"items": [self.read_task(item["id"]) | {"score": item["score"]} for item in items], "count": len(items)}

    def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.ensure_schema()
        now = utc_now()
        task_id = str(payload.get("task_id") or new_work_id("task")).strip()
        title = _clip_text(payload.get("title") or payload.get("goal") or task_id, 160)
        goal = _clip_text(payload.get("goal") or title, _WORK_ORDER_DRAFT_TEXT_LIMIT)
        summary = _clip_text(payload.get("summary") or payload.get("source_summary") or "", _WORK_ORDER_DRAFT_TEXT_LIMIT)
        profile_family = _profile_ref_text(payload.get("profile_family") or payload.get("family") or "")
        if not profile_family:
            raise ValueError("create_task requires profile_family")
        metadata = _loads_or_dict(payload.get("metadata"))
        metadata["profile_family"] = profile_family
        workspace = _loads_or_dict(payload.get("workspace"))
        if workspace:
            metadata["workspace"] = workspace
        with self._connect() as db:
            existing = self._fetch_one(db, "SELECT * FROM minion_tasks WHERE task_id = ?", (task_id,))
            if existing is not None:
                existing_family = str(existing["profile_family"] or "general").strip() or "general"
                if existing_family != profile_family:
                    raise ValueError(f"task {task_id} already uses profile_family {existing_family}")
                existing_metadata = _loads(existing["metadata_json"])
                merged_metadata = _deep_merge_dict(existing_metadata, metadata)
                db.execute(
                    """
                    UPDATE minion_tasks
                    SET title = ?, goal = ?, summary = ?, metadata_json = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (title[:160], goal, summary, _json(merged_metadata), now, task_id),
                )
            else:
                db.execute(
                    """
                    INSERT INTO minion_tasks(task_id, title, goal, summary, status, profile_family, metadata_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        title[:160],
                        goal,
                        summary,
                        str(payload.get("status") or "active"),
                        profile_family,
                        _json(metadata),
                        now,
                        now,
                    ),
                )
            self._sync_task_fts(db, task_id)
        return self.read_task(task_id)

    def search_work_orders(self, query: str, *, limit: int = 10, include_archived: bool = False) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as db:
            fallback_sql = """
                SELECT work_order_id, 1.0 AS score FROM minion_work_orders
                WHERE lower(title || ' ' || goal || ' ' || instruction) LIKE ?
                  AND status != 'archived'
                ORDER BY updated_at DESC
                LIMIT ?
            """
            if include_archived:
                fallback_sql = """
                    SELECT work_order_id, 1.0 AS score FROM minion_work_orders
                    WHERE lower(title || ' ' || goal || ' ' || instruction) LIKE ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                """
            items = self.search.search_fts(
                db,
                table_name="minion_work_orders_fts",
                id_column="work_order_id",
                query=query,
                limit=limit,
                fallback_sql=fallback_sql,
            )
            visible: list[dict[str, Any]] = []
            archived_count = 0
            for item in items:
                snapshot = self.read_work_order(item["id"])
                status = str((snapshot.get("work_order") or {}).get("status") or "").strip().lower()
                if status == "archived" and not include_archived:
                    archived_count += 1
                    continue
                visible.append(_compact_work_order_search_item(snapshot) | {"score": item["score"]})
            return {
                "items": visible,
                "count": len(visible),
                "archived_filtered_count": archived_count,
                "include_archived": bool(include_archived),
            }

    def search_work_order_drafts(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as db:
            items = self.search.search_fts(
                db,
                table_name="minion_work_order_drafts_fts",
                id_column="draft_id",
                query=query,
                limit=limit,
                fallback_sql="""
                    SELECT draft_id, 1.0 AS score FROM minion_work_order_drafts
                    WHERE lower(title || ' ' || goal || ' ' || source_summary || ' ' || payload_json) LIKE ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                """,
            )
            return {
                "items": [
                    _compact_work_order_draft_search_item(self.read_work_order_draft(item["id"])) | {"score": item["score"]}
                    for item in items
                ],
                "count": len(items),
            }

    def read_task(self, task_id: str, *, include_archived: bool = False) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as db:
            task = self._fetch_one(db, "SELECT * FROM minion_tasks WHERE task_id = ?", (str(task_id),))
            if task is None:
                return {"status": "not_found", "task_id": str(task_id)}
            work_order_sql = "SELECT * FROM minion_work_orders WHERE task_id = ? AND status != 'archived' ORDER BY created_at DESC"
            if include_archived:
                work_order_sql = "SELECT * FROM minion_work_orders WHERE task_id = ? ORDER BY created_at DESC"
            work_orders = [
                dict(row)
                for row in db.execute(
                    work_order_sql,
                    (str(task_id),),
                ).fetchall()
            ]
            lessons = [dict(row) for row in db.execute(
                "SELECT * FROM minion_task_lessons WHERE task_id = ? ORDER BY created_at DESC LIMIT 50",
                (str(task_id),),
            ).fetchall()]
            return {
                "status": "ok",
                "task": _decode_json_fields(dict(task)),
                "work_orders": [_decode_json_fields(item) for item in work_orders],
                "task_lessons": lessons,
            }

    def read_work_order(self, work_order_id: str, *, active_runs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as db:
            work_order = self._fetch_one(db, "SELECT * FROM minion_work_orders WHERE work_order_id = ?", (str(work_order_id),))
            if work_order is None:
                return {"status": "not_found", "work_order_id": str(work_order_id)}
            task = self._fetch_one(db, "SELECT * FROM minion_tasks WHERE task_id = ?", (str(work_order["task_id"]),))
            milestones = [dict(row) for row in db.execute(
                "SELECT * FROM minion_work_order_milestones WHERE work_order_id = ? ORDER BY milestone_index",
                (str(work_order_id),),
            ).fetchall()]
            checkpoints = [dict(row) for row in db.execute(
                "SELECT * FROM minion_worker_checkpoints WHERE work_order_id = ? ORDER BY created_at DESC",
                (str(work_order_id),),
            ).fetchall()]
            completed = {int(row["milestone_index"]) for row in checkpoints if row.get("status") == "completed"}
            enriched_milestones = []
            for milestone in milestones:
                latest = next((row for row in checkpoints if int(row["milestone_index"]) == int(milestone["milestone_index"])), None)
                enriched = _decode_json_fields(milestone, keep_json=False)
                enriched["completed"] = int(milestone["milestone_index"]) in completed
                enriched["latest_checkpoint"] = (
                    _compact_event_for_continuity(_decode_json_fields(dict(latest), keep_json=False))
                    if latest
                    else {}
                )
                enriched_milestones.append(enriched)
            current = next((item for item in enriched_milestones if not item.get("completed")), None)
            latest_checkpoint = (
                _compact_event_for_continuity(_decode_json_fields(dict(checkpoints[0]), keep_json=False))
                if checkpoints
                else {}
            )
            latest_completed = next((row for row in checkpoints if row.get("status") == "completed"), None)
            recent_ledger = [dict(row) for row in db.execute(
                "SELECT * FROM minion_worker_ledger WHERE work_order_id = ? ORDER BY created_at DESC LIMIT 50",
                (str(work_order_id),),
            ).fetchall()]
            task_lessons = [dict(row) for row in db.execute(
                "SELECT * FROM minion_task_lessons WHERE task_id = ? ORDER BY created_at DESC LIMIT 50",
                (str(work_order["task_id"]),),
            ).fetchall()]
            system_candidates = [dict(row) for row in db.execute(
                """
                SELECT * FROM minion_system_lesson_candidates
                WHERE work_order_id = ? AND status = 'pending'
                ORDER BY created_at DESC LIMIT 50
                """,
                (str(work_order_id),),
            ).fetchall()]
            decoded_work_order = _decode_json_fields(dict(work_order), keep_json=False)
            module_status_list = _module_status_items_from_metadata(
                _loads_or_dict(decoded_work_order.get("metadata")),
                enriched_milestones,
                active_runs=active_runs or [],
            )
            current_worker = _current_worker_for_work_order(str(work_order_id), active_runs or [])
            active_module_workers = [
                dict(item.get("current_worker") or {})
                for item in module_status_list
                if isinstance(item.get("current_worker"), dict) and item.get("current_worker")
            ]
            return {
                "status": "ok",
                "task": _decode_json_fields(dict(task), keep_json=False) if task else {},
                "work_order": decoded_work_order,
                "milestones": enriched_milestones,
                "current_milestone": current or {},
                "latest_checkpoint": latest_checkpoint,
                "latest_completed_checkpoint": (
                    _compact_event_for_continuity(_decode_json_fields(dict(latest_completed), keep_json=False))
                    if latest_completed
                    else {}
                ),
                "recent_ledger": [
                    _compact_event_for_continuity(_decode_json_fields(item, keep_json=False))
                    for item in recent_ledger
                ],
                "current_worker": current_worker,
                "active_module_workers": active_module_workers,
                "module_status_list": module_status_list,
                "module_status_text": _module_status_text(module_status_list),
                "task_lessons": task_lessons,
                "pending_system_lesson_candidates": system_candidates,
            }

    def archive_work_order(
        self,
        work_order_id: str,
        *,
        reason: str = "",
        include_children: bool = False,
        restore: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        normalized = str(work_order_id or "").strip()
        if not normalized:
            return {"status": "invalid", "error": "work_order_id is required"}
        self.ensure_schema()
        with self._connect() as db:
            rows = self._work_order_rows_for_control(db, normalized, include_children=include_children)
            if not rows:
                return {"status": "not_found", "work_order_id": normalized}
            now = utc_now()
            changed: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            blocked: list[dict[str, Any]] = []
            for row in rows:
                current_id = str(row["work_order_id"])
                current_status = str(row["status"] or "").strip().lower()
                metadata = _loads(row["metadata_json"])
                if restore:
                    if current_status != "archived":
                        skipped.append({"work_order_id": current_id, "status": current_status, "reason": "not_archived"})
                        continue
                    archive = dict(metadata.get("archive") or {})
                    previous_status = str(archive.get("previous_status") or "completed").strip().lower() or "completed"
                    if previous_status in RUNNING_WORK_ORDER_STATUSES and not force:
                        blocked.append(
                            {
                                "work_order_id": current_id,
                                "status": current_status,
                                "previous_status": previous_status,
                                "reason": "restore_would_make_work_order_active",
                            }
                        )
                        continue
                    archive.update(
                        {
                            "archived": False,
                            "restored_at": now,
                            "restore_reason": str(reason or "").strip(),
                            "restored_from_status": current_status,
                        }
                    )
                    metadata["archive"] = archive
                    db.execute(
                        "UPDATE minion_work_orders SET metadata_json = ?, updated_at = ? WHERE work_order_id = ?",
                        (_json(metadata), now, current_id),
                    )
                    self._update_work_order_status(db, current_id, previous_status)
                    changed.append({"work_order_id": current_id, "status": previous_status, "previous_status": current_status})
                    continue
                if current_status == "archived":
                    skipped.append({"work_order_id": current_id, "status": current_status, "reason": "already_archived"})
                    continue
                if current_status in RUNNING_WORK_ORDER_STATUSES and not force:
                    blocked.append({"work_order_id": current_id, "status": current_status, "reason": "work_order_may_be_running"})
                    continue
                archive = dict(metadata.get("archive") or {})
                archive.update(
                    {
                        "archived": True,
                        "archived_at": now,
                        "reason": str(reason or "").strip(),
                        "previous_status": current_status or "active",
                    }
                )
                metadata["archive"] = archive
                db.execute(
                    "UPDATE minion_work_orders SET metadata_json = ?, updated_at = ? WHERE work_order_id = ?",
                    (_json(metadata), now, current_id),
                )
                self._update_work_order_status(db, current_id, "archived")
                changed.append({"work_order_id": current_id, "status": "archived", "previous_status": current_status})
            if blocked and not changed:
                status = "blocked"
            elif changed and blocked:
                status = "partial"
            elif changed:
                status = "restored" if restore else "archived"
            else:
                status = "skipped"
            return {
                "status": status,
                "work_order_id": normalized,
                "include_children": bool(include_children),
                "restore": bool(restore),
                "changed": changed,
                "changed_count": len(changed),
                "blocked": blocked,
                "blocked_count": len(blocked),
                "skipped": skipped,
                "skipped_count": len(skipped),
            }

    def remove_work_order(
        self,
        work_order_id: str,
        *,
        reason: str = "",
        include_children: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        normalized = str(work_order_id or "").strip()
        if not normalized:
            return {"status": "invalid", "error": "work_order_id is required"}
        self.ensure_schema()
        with self._connect() as db:
            rows = self._work_order_rows_for_control(db, normalized, include_children=include_children)
            if not rows:
                return {"status": "not_found", "work_order_id": normalized}
            blocked: list[dict[str, Any]] = []
            for row in rows:
                current_status = str(row["status"] or "").strip().lower()
                current_id = str(row["work_order_id"])
                if current_status in RUNNING_WORK_ORDER_STATUSES and not force:
                    blocked.append({"work_order_id": current_id, "status": current_status, "reason": "work_order_may_be_running"})
                    continue
                if current_status != "archived" and not force:
                    blocked.append({"work_order_id": current_id, "status": current_status, "reason": "archive_required_before_remove"})
            if blocked:
                return {
                    "status": "blocked",
                    "work_order_id": normalized,
                    "include_children": bool(include_children),
                    "blocked": blocked,
                    "blocked_count": len(blocked),
                    "removed": [],
                    "removed_count": 0,
                }
            removed: list[dict[str, Any]] = []
            for row in rows:
                current_id = str(row["work_order_id"])
                current_status = str(row["status"] or "")
                self._delete_work_order_records(db, current_id)
                removed.append({"work_order_id": current_id, "previous_status": current_status})
            return {
                "status": "removed",
                "work_order_id": normalized,
                "include_children": bool(include_children),
                "reason": str(reason or "").strip(),
                "removed": removed,
                "removed_count": len(removed),
                "force": bool(force),
            }

    def read_work_order_draft(self, draft_id: str) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as db:
            row = self._fetch_one(db, "SELECT * FROM minion_work_order_drafts WHERE draft_id = ?", (str(draft_id),))
            if row is None:
                return {"status": "not_found", "draft_id": str(draft_id)}
            payload = _loads(row["payload_json"])
            draft = _decode_json_fields(dict(row), keep_json=False)
            draft.pop("payload", None)
            candidate = dict(payload.get("work_order_candidate") or {})
            return {
                "status": "ok",
                "draft": draft,
                "work_order_candidate": candidate,
                "planner_review": dict(payload.get("planner_review") or {}),
            }

    @contextmanager
    def _connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.db_path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _fetch_one(self, db: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> sqlite3.Row | None:
        return db.execute(sql, params).fetchone()

    def _write_plan_parent_metadata(
        self,
        db: sqlite3.Connection,
        work_order_id: str,
        metadata: dict[str, Any],
        *,
        expected_metadata_json: str = "",
        work_order_status: str = "",
        bump_revision: bool = True,
    ) -> bool:
        plan_execution = dict(metadata.get("plan_execution") or {})
        if plan_execution and bump_revision:
            plan_execution["dag_revision"] = max(0, _coerce_int(plan_execution.get("dag_revision")) or 0) + 1
            plan_execution["dag_updated_at"] = utc_now()
            metadata["plan_execution"] = plan_execution
        next_metadata_json = _json(metadata)
        now = utc_now()
        if expected_metadata_json:
            cursor = db.execute(
                """
                UPDATE minion_work_orders
                SET metadata_json = ?, updated_at = ?
                WHERE work_order_id = ? AND metadata_json = ?
                """,
                (next_metadata_json, now, str(work_order_id), str(expected_metadata_json)),
            )
        else:
            cursor = db.execute(
                "UPDATE minion_work_orders SET metadata_json = ?, updated_at = ? WHERE work_order_id = ?",
                (next_metadata_json, now, str(work_order_id)),
            )
        if cursor.rowcount != 1:
            return False
        if work_order_status:
            self._update_work_order_status(db, str(work_order_id), str(work_order_status))
        return True

    def _migrate_legacy_minion_tables_if_needed(self, db: sqlite3.Connection) -> None:
        legacy_path = self.runtime_root / "pal.sqlite3"
        if not legacy_path.exists() or legacy_path.resolve() == self.db_path.resolve():
            return
        if _table_row_count(db, "minion_work_orders") > 0 or _table_row_count(db, "minion_tasks") > 0:
            return
        try:
            db.execute("ATTACH DATABASE ? AS legacy_minion", (str(legacy_path),))
        except sqlite3.Error:
            return
        try:
            if not _attached_table_exists(db, "legacy_minion", "minion_work_orders"):
                return
            if _attached_table_row_count(db, "legacy_minion", "minion_work_orders") <= 0:
                return
            for table_name in _LEGACY_MINION_COPY_TABLES:
                if not _attached_table_exists(db, "legacy_minion", table_name):
                    continue
                columns = _table_columns(db, table_name)
                legacy_columns = _attached_table_columns(db, "legacy_minion", table_name)
                shared_columns = [column for column in columns if column in set(legacy_columns)]
                if not shared_columns:
                    continue
                column_sql = ", ".join(shared_columns)
                db.execute(
                    f"INSERT OR IGNORE INTO {table_name} ({column_sql}) "
                    f"SELECT {column_sql} FROM legacy_minion.{table_name}"
                )
            self._rebuild_search_indexes(db)
        finally:
            with contextlib.suppress(sqlite3.Error):
                db.execute("DETACH DATABASE legacy_minion")

    def _rebuild_search_indexes(self, db: sqlite3.Connection) -> None:
        db.execute("DELETE FROM minion_tasks_fts")
        for row in db.execute("SELECT task_id FROM minion_tasks").fetchall():
            self._sync_task_fts(db, str(row["task_id"]))
        db.execute("DELETE FROM minion_work_orders_fts")
        for row in db.execute("SELECT work_order_id FROM minion_work_orders").fetchall():
            self._sync_work_order_fts(db, str(row["work_order_id"]))
        db.execute("DELETE FROM minion_work_order_drafts_fts")
        for row in db.execute("SELECT draft_id FROM minion_work_order_drafts").fetchall():
            self._sync_work_order_draft_fts(db, str(row["draft_id"]))

    def _ensure_milestones(self, db: sqlite3.Connection, work_order_id: str, milestones: list[dict[str, Any]]) -> None:
        existing = db.execute(
            "SELECT COUNT(*) FROM minion_work_order_milestones WHERE work_order_id = ?",
            (work_order_id,),
        ).fetchone()
        if existing and int(existing[0]) > 0:
            return
        now = utc_now()
        for index, milestone in enumerate(milestones):
            db.execute(
                """
                INSERT INTO minion_work_order_milestones(
                    milestone_id, work_order_id, milestone_index, title, summary, acceptance_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"ms_{uuid4().hex[:16]}",
                    work_order_id,
                    index,
                    str(milestone.get("title") or f"Milestone {index + 1}"),
                    str(milestone.get("summary") or ""),
                    _json(milestone.get("acceptance") or []),
                    now,
                ),
            )

    def _reset_work_order_execution_records(self, db: sqlite3.Connection, work_order_id: str) -> None:
        db.execute("DELETE FROM minion_work_order_milestones WHERE work_order_id = ?", (str(work_order_id),))
        db.execute("DELETE FROM minion_worker_checkpoints WHERE work_order_id = ?", (str(work_order_id),))

    def _record_deferred_experience(self, db: sqlite3.Connection, work_order_id: str, payload: dict[str, Any]) -> None:
        deferred = _experience_payload(payload.get("deferred_experience"))
        if not deferred["task_lessons"] and not deferred["system_lessons"] and not deferred["memory_candidates"]:
            return
        row = self._fetch_one(db, "SELECT metadata_json FROM minion_work_orders WHERE work_order_id = ?", (str(work_order_id),))
        if row is None:
            return
        metadata = _loads(row["metadata_json"])
        module_execution = dict(metadata.get("module_execution") or {})
        pending = _experience_payload(module_execution.get("pending_experience"))
        pending["task_lessons"] = _dedupe_text([*pending["task_lessons"], *deferred["task_lessons"]])
        pending["system_lessons"] = _dedupe_text([*pending["system_lessons"], *deferred["system_lessons"]])
        pending["memory_candidates"] = _dedupe_dicts([*pending["memory_candidates"], *deferred["memory_candidates"]])
        module_execution["pending_experience"] = pending
        metadata["module_execution"] = module_execution
        db.execute(
            "UPDATE minion_work_orders SET metadata_json = ?, updated_at = ? WHERE work_order_id = ?",
            (_json(metadata), utc_now(), str(work_order_id)),
        )

    def _release_running_module_parent(
        self,
        db: sqlite3.Connection,
        parent_work_order: dict[str, Any],
        parent_metadata: dict[str, Any],
        plan_execution: dict[str, Any],
        *,
        child_work_order_id: str,
        child_terminal_status: str,
        reason: str,
    ) -> dict[str, Any]:
        parent_id = str(parent_work_order.get("work_order_id") or "")
        expected_parent_metadata_json = str(parent_work_order.get("metadata_json") or _json(parent_metadata))
        child_id = str(child_work_order_id or "").strip()
        normalized_child_status = str(child_terminal_status or "failed").strip().lower()
        if normalized_child_status not in {"failed", "blocked", "killed"}:
            normalized_child_status = "failed"
        now = utc_now()
        child_terminal_recorded = False
        child_existing_terminal_status = _latest_child_terminal_failure_status(db, child_id)
        child_has_terminal_failure = child_existing_terminal_status in {"failed", "blocked", "killed"}
        if child_has_terminal_failure:
            normalized_child_status = child_existing_terminal_status
        if child_id:
            child = self._fetch_one(db, "SELECT status FROM minion_work_orders WHERE work_order_id = ?", (child_id,))
            child_status = str(dict(child)["status"] if child is not None else "").strip().lower()
            if child is not None and child_status not in {"completed", "failed", "blocked", "killed"}:
                if child_has_terminal_failure:
                    self._update_work_order_status(db, child_id, normalized_child_status)
                else:
                    child_payload = {
                        "status": normalized_child_status,
                        "summary": reason or f"child module runner {child_id} was released",
                        "reason": "manager_recovery",
                        "parent_work_order_id": parent_id,
                        "child_work_order_id": child_id,
                    }
                    self.ledger.insert_ledger(db, child_id, "terminal", str(child_payload["summary"]), child_payload, "", "", now)
                    self.ledger.record_terminal(db, child_id, child_payload, "", "", now)
                    child_terminal_recorded = True
        running_children = _plan_execution_running_children(plan_execution)
        dag = _plan_execution_dag_state(plan_execution)
        release = _dag_release_running_module(dag, child_id, terminal_failure=child_has_terminal_failure) if dag else {}
        released_module_id = str(release.get("released_module_id") or "")
        if not released_module_id:
            released_module_id = next((module_id for module_id, value in running_children.items() if value == child_id), "")
        cursor: dict[str, Any] = {}
        if release.get("dag"):
            released_dag = dict(release.get("dag") or {})
            if released_module_id and child_id:
                module_status = dict(released_dag.get("module_status") or {})
                cursor = self._module_node_cursor_from_child(
                    db,
                    module_id=released_module_id,
                    child_work_order_id=child_id,
                    node_status=str(module_status.get(released_module_id) or ""),
                    reason=reason,
                )
                released_dag = _set_dag_node_cursor(released_dag, released_module_id, cursor)
            _set_plan_execution_dag_state(plan_execution, released_dag)
        resolved_status = str(release.get("status") or _module_dag_status(_plan_execution_dag_state(plan_execution)))
        plan_execution["status"] = resolved_status
        plan_execution["status_reason"] = reason
        if child_id:
            plan_execution["last_released_child_work_order_id"] = child_id
        active_child_ids = list(release.get("active_child_work_order_ids") or [])
        if not release:
            active_child_ids = [
                value
                for value in _coerce_text_list(dict(_plan_execution_dag_state(plan_execution).get("running_modules") or {}).values())
                if value != child_id
            ]
        plan_execution["active_child_work_order_ids"] = active_child_ids
        if len(active_child_ids) == 1:
            plan_execution["active_child_work_order_id"] = active_child_ids[0]
        else:
            plan_execution.pop("active_child_work_order_id", None)
        parent_metadata["plan_execution"] = plan_execution
        wrote = self._write_plan_parent_metadata(
            db,
            parent_id,
            parent_metadata,
            expected_metadata_json=expected_parent_metadata_json,
            work_order_status="blocked" if resolved_status == "blocked" else "active",
        )
        if not wrote:
            return {
                "parent_work_order_id": parent_id,
                "child_work_order_id": child_id,
                "module_id": released_module_id,
                "status": "stale_dag_revision",
                "child_terminal_status": normalized_child_status,
                "child_terminal_recorded": child_terminal_recorded,
                "reason": reason,
            }
        parent_payload = {
            "status": resolved_status,
            "summary": reason or f"released stale running module for {parent_id}",
            "reason": "manager_recovery",
            "parent_work_order_id": parent_id,
            "child_work_order_id": child_id,
            "module_id": released_module_id,
            "child_terminal_status": normalized_child_status,
            "child_terminal_recorded": child_terminal_recorded,
        }
        if cursor:
            parent_payload["node_cursor"] = cursor
        self.ledger.insert_ledger(db, parent_id, "module_recovered", str(parent_payload["summary"]), parent_payload, "", "", now)
        return {
            "parent_work_order_id": parent_id,
            "child_work_order_id": child_id,
            "module_id": released_module_id,
            "status": resolved_status,
            "child_terminal_status": normalized_child_status,
            "child_terminal_recorded": child_terminal_recorded,
            "reason": reason,
            **({"node_cursor": cursor} if cursor else {}),
        }

    def _has_incomplete_milestones(self, db: sqlite3.Connection, work_order_id: str) -> bool:
        rows = db.execute(
            "SELECT milestone_index FROM minion_work_order_milestones WHERE work_order_id = ? ORDER BY milestone_index",
            (str(work_order_id),),
        ).fetchall()
        if not rows:
            return False
        completed = {
            int(row["milestone_index"])
            for row in db.execute(
                """
                SELECT milestone_index FROM minion_worker_checkpoints
                WHERE work_order_id = ? AND status = 'completed'
                """,
                (str(work_order_id),),
            ).fetchall()
        }
        return any(int(row["milestone_index"]) not in completed for row in rows)

    def _module_node_cursor_from_child(
        self,
        db: sqlite3.Connection,
        *,
        module_id: str,
        child_work_order_id: str,
        node_status: str,
        reason: str = "",
    ) -> dict[str, Any]:
        child_id = str(child_work_order_id or "").strip()
        milestone_rows = [
            dict(row)
            for row in db.execute(
                """
                SELECT milestone_id, milestone_index, title, summary
                FROM minion_work_order_milestones
                WHERE work_order_id = ?
                ORDER BY milestone_index
                """,
                (child_id,),
            ).fetchall()
        ]
        checkpoint_rows = [
            dict(row)
            for row in db.execute(
                """
                SELECT checkpoint_id, milestone_index, status, summary, minion_id, run_id, created_at
                FROM minion_worker_checkpoints
                WHERE work_order_id = ?
                ORDER BY created_at DESC
                """,
                (child_id,),
            ).fetchall()
        ]
        completed_indexes = {
            int(row["milestone_index"])
            for row in checkpoint_rows
            if str(row.get("status") or "").strip().lower() == "completed"
        }
        current_row = next(
            (row for row in milestone_rows if int(row.get("milestone_index") or 0) not in completed_indexes),
            milestone_rows[-1] if milestone_rows else {},
        )
        latest_checkpoint = _compact_event_for_continuity(_decode_json_fields(dict(checkpoint_rows[0]), keep_json=False)) if checkpoint_rows else {}
        latest_completed = next(
            (row for row in checkpoint_rows if str(row.get("status") or "").strip().lower() == "completed"),
            {},
        )
        child_row = self._fetch_one(db, "SELECT status, updated_at, ended_at FROM minion_work_orders WHERE work_order_id = ?", (child_id,))
        child_status = str(child_row["status"] if child_row is not None else "").strip()
        return _drop_empty_dict(
            {
                "module_id": str(module_id or "").strip(),
                "child_work_order_id": child_id,
                "node_status": str(node_status or "").strip(),
                "child_status": child_status,
                "current_milestone_index": (
                    int(current_row.get("milestone_index"))
                    if current_row and current_row.get("milestone_index") is not None
                    else self._derive_current_milestone_index(db, child_id)
                ),
                "current_milestone_id": str(current_row.get("milestone_id") or ""),
                "current_milestone_title": str(current_row.get("title") or ""),
                "milestone_count": len(milestone_rows),
                "completed_milestone_indexes": [int(row.get("milestone_index") or 0) for row in milestone_rows if int(row.get("milestone_index") or 0) in completed_indexes],
                "latest_checkpoint": latest_checkpoint,
                "latest_completed_checkpoint": (
                    _compact_event_for_continuity(_decode_json_fields(dict(latest_completed), keep_json=False))
                    if latest_completed
                    else {}
                ),
                "reason": str(reason or "").strip(),
                "updated_at": str(child_row["updated_at"] if child_row is not None else ""),
                "ended_at": str(child_row["ended_at"] if child_row is not None else ""),
            }
        )

    def _record_payload_artifacts(self, db: sqlite3.Connection, work_order_id: str, payload: dict[str, Any]) -> None:
        artifacts = _artifact_list(payload.get("artifacts"))
        primary = payload.get("primary_artifact")
        if isinstance(primary, dict):
            artifacts = _merge_artifacts([dict(primary), *artifacts])
        plan_ref = payload.get("plan_ref")
        plan_validation = payload.get("plan_validation")
        if not artifacts and not isinstance(plan_ref, dict) and not isinstance(plan_validation, dict):
            return
        row = self._fetch_one(db, "SELECT metadata_json FROM minion_work_orders WHERE work_order_id = ?", (str(work_order_id),))
        if row is None:
            return
        metadata = _loads(row["metadata_json"])
        if artifacts:
            existing = _artifact_list(metadata.get("artifacts"))
            metadata["artifacts"] = _merge_artifacts([*existing, *artifacts])
            if isinstance(primary, dict):
                metadata["primary_artifact"] = dict(primary)
        if isinstance(plan_ref, dict):
            metadata["plan_ref"] = dict(plan_ref)
        if isinstance(plan_validation, dict):
            metadata["plan_validation"] = dict(plan_validation)
        db.execute(
            "UPDATE minion_work_orders SET metadata_json = ?, updated_at = ? WHERE work_order_id = ?",
            (_json(metadata), utc_now(), str(work_order_id)),
        )

    def _lesson_exists(self, db: sqlite3.Connection, table_name: str, work_order_id: str, lesson_text: str) -> bool:
        if table_name not in {"minion_task_lessons", "minion_system_lesson_candidates"}:
            return False
        row = self._fetch_one(
            db,
            f"SELECT 1 FROM {table_name} WHERE work_order_id = ? AND lesson_text = ? LIMIT 1",
            (str(work_order_id), str(lesson_text)),
        )
        return row is not None

    def _work_order_rows_for_control(self, db: sqlite3.Connection, work_order_id: str, *, include_children: bool = False) -> list[sqlite3.Row]:
        normalized = str(work_order_id or "").strip()
        if not normalized:
            return []
        rows = {
            str(row["work_order_id"]): row
            for row in db.execute("SELECT * FROM minion_work_orders").fetchall()
        }
        if normalized not in rows:
            return []
        if not include_children:
            return [rows[normalized]]
        result_ids: list[str] = []
        seen: set[str] = set()
        queue: list[str] = [normalized]
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            if current not in rows:
                continue
            result_ids.append(current)
            for child_id in self._child_work_order_ids_from_row(rows[current], rows):
                if child_id not in seen:
                    queue.append(child_id)
        return [rows[item] for item in result_ids if item in rows]

    def _child_work_order_ids_from_row(self, row: sqlite3.Row, rows: dict[str, sqlite3.Row]) -> list[str]:
        parent_id = str(row["work_order_id"] or "").strip()
        metadata = _loads(row["metadata_json"])
        plan_execution = dict(metadata.get("plan_execution") or {})
        candidates: list[str] = []
        for child_id in dict(plan_execution.get("child_work_order_ids") or {}).values():
            text = str(child_id or "").strip()
            if text:
                candidates.append(text)
        for child_id in _coerce_text_list(plan_execution.get("active_child_work_order_ids")):
            candidates.append(child_id)
        active_child = str(plan_execution.get("active_child_work_order_id") or "").strip()
        if active_child:
            candidates.append(active_child)
        for child_id, child_row in rows.items():
            child_metadata = _loads(child_row["metadata_json"])
            if str(child_metadata.get("parent_work_order_id") or "").strip() == parent_id:
                candidates.append(child_id)
        result: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            text = str(item or "").strip()
            if text and text in rows and text not in seen and text != parent_id:
                seen.add(text)
                result.append(text)
        return result

    def _delete_work_order_records(self, db: sqlite3.Connection, work_order_id: str) -> None:
        normalized = str(work_order_id or "").strip()
        if not normalized:
            return
        db.execute("DELETE FROM minion_work_orders_fts WHERE work_order_id = ?", (normalized,))
        for table_name in (
            "minion_work_order_milestones",
            "minion_worker_checkpoints",
            "minion_worker_ledger",
            "minion_review_gates",
            "minion_task_lessons",
            "minion_system_lesson_candidates",
        ):
            db.execute(f"DELETE FROM {table_name} WHERE work_order_id = ?", (normalized,))
        db.execute("DELETE FROM minion_work_orders WHERE work_order_id = ?", (normalized,))

    def _update_work_order_status(self, db: sqlite3.Connection, work_order_id: str, status: str) -> None:
        ended_at = utc_now() if status in {"completed", "failed", "blocked", "killed", "archived"} else ""
        db.execute(
            """
            UPDATE minion_work_orders
            SET status = ?, updated_at = ?, ended_at = ?
            WHERE work_order_id = ?
            """,
            (status, utc_now(), ended_at, work_order_id),
        )
        self._sync_work_order_fts(db, work_order_id)

    def _derive_current_milestone_index(self, db: sqlite3.Connection, work_order_id: str) -> int:
        rows = db.execute(
            "SELECT milestone_index FROM minion_work_order_milestones WHERE work_order_id = ? ORDER BY milestone_index",
            (work_order_id,),
        ).fetchall()
        completed = {
            int(row["milestone_index"])
            for row in db.execute(
                """
                SELECT milestone_index FROM minion_worker_checkpoints
                WHERE work_order_id = ? AND status = 'completed'
                """,
                (work_order_id,),
            ).fetchall()
        }
        for row in rows:
            index = int(row["milestone_index"])
            if index not in completed:
                return index
        return int(rows[-1]["milestone_index"]) if rows else 0

    def _sync_task_fts(self, db: sqlite3.Connection, task_id: str) -> None:
        db.execute("DELETE FROM minion_tasks_fts WHERE task_id = ?", (task_id,))
        row = self._fetch_one(db, "SELECT * FROM minion_tasks WHERE task_id = ?", (task_id,))
        if row is None:
            return
        db.execute(
            "INSERT INTO minion_tasks_fts(task_id, title, goal, summary) VALUES (?, ?, ?, ?)",
            (
                row["task_id"],
                jieba_fts_text(row["title"]),
                jieba_fts_text(row["goal"]),
                jieba_fts_text(row["summary"]),
            ),
        )

    def _sync_work_order_fts(self, db: sqlite3.Connection, work_order_id: str) -> None:
        db.execute("DELETE FROM minion_work_orders_fts WHERE work_order_id = ?", (work_order_id,))
        row = self._fetch_one(db, "SELECT * FROM minion_work_orders WHERE work_order_id = ?", (work_order_id,))
        if row is None:
            return
        db.execute(
            "INSERT INTO minion_work_orders_fts(work_order_id, task_id, title, goal, instruction) VALUES (?, ?, ?, ?, ?)",
            (
                row["work_order_id"],
                row["task_id"],
                jieba_fts_text(row["title"]),
                jieba_fts_text(row["goal"]),
                jieba_fts_text(row["instruction"]),
            ),
        )

    def _sync_work_order_draft_fts(self, db: sqlite3.Connection, draft_id: str) -> None:
        db.execute("DELETE FROM minion_work_order_drafts_fts WHERE draft_id = ?", (draft_id,))
        row = self._fetch_one(db, "SELECT * FROM minion_work_order_drafts WHERE draft_id = ?", (draft_id,))
        if row is None:
            return
        payload = _loads(row["payload_json"])
        db.execute(
            """
            INSERT INTO minion_work_order_drafts_fts(draft_id, title, goal, source_summary, payload_text)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                row["draft_id"],
                jieba_fts_text(row["title"]),
                jieba_fts_text(row["goal"]),
                jieba_fts_text(row["source_summary"]),
                jieba_fts_text(json.dumps(payload, ensure_ascii=False, sort_keys=True)),
            ),
        )

def _coerce_milestones(raw: Any, acceptance_criteria: list[str], fallback: str) -> list[dict[str, Any]]:
    _ = acceptance_criteria, fallback
    return normalize_milestones(raw)


def _plan_truth_milestones(metadata: dict[str, Any], acceptance_criteria: list[str], fallback: str) -> list[dict[str, Any]]:
    data = dict(metadata or {})
    module_execution = dict(data.get("module_execution") or {}) if isinstance(data.get("module_execution"), dict) else {}
    plan_execution = dict(data.get("plan_execution") or {}) if isinstance(data.get("plan_execution"), dict) else {}
    if str(module_execution.get("mode") or "") in SERIAL_MILESTONE_MODES and not isinstance(data.get("plan_artifact"), dict):
        raise ValueError("serial milestone execution requires plan_artifact")
    if str(plan_execution.get("mode") or "") == "module_parent_milestones" and not isinstance(data.get("plan_artifact"), dict):
        raise ValueError("module parent milestone execution requires plan_artifact")
    if not isinstance(data.get("plan_artifact"), dict):
        if _is_plan_module_child_metadata(data) and isinstance(data.get("milestones"), list):
            return _coerce_milestones(data.get("milestones"), acceptance_criteria, fallback)
        return []
    return _coerce_milestones(data.get("milestones"), acceptance_criteria, fallback)


def _strip_raw_milestone_metadata_without_plan(metadata: dict[str, Any]) -> dict[str, Any]:
    data = dict(metadata or {})
    if isinstance(data.get("plan_artifact"), dict):
        return data
    data.pop("milestones", None)
    module_execution = dict(data.get("module_execution") or {}) if isinstance(data.get("module_execution"), dict) else {}
    if str(module_execution.get("mode") or "") in SERIAL_MILESTONE_MODES:
        data.pop("module_execution", None)
    return data


def _allow_plan_revision_with_active_source(metadata: dict[str, Any], active_work_order_id: str) -> bool:
    planner_work_order = _loads_or_dict(metadata.get("planner_work_order"))
    if not isinstance(planner_work_order.get("revision_source"), dict):
        return False
    plan_review = _loads_or_dict(metadata.get("plan_review"))
    source_work_order_id = str(plan_review.get("source_work_order_id") or "").strip()
    if source_work_order_id and source_work_order_id != str(active_work_order_id or "").strip():
        return False
    return True


def _merge_existing_work_order_metadata(stored: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result = dict(stored or {})
    updates = dict(incoming or {})
    if _has_plan_truth_source(result):
        incoming_module_execution = updates.pop("module_execution", None)
        incoming_coder_work_order = updates.pop("coder_work_order", None)
        plan_binding_matches = (
            _module_execution_binding_matches(dict(result.get("module_execution") or {}), dict(incoming_module_execution or {}))
            if isinstance(incoming_module_execution, dict)
            else False
        )
        for key in (
            "plan_artifact",
            "plan_ref",
            "plan_validation",
            "milestones",
            "plan_execution",
            "planner_work_order",
            "reviewer_work_order",
            "module_id",
        ):
            updates.pop(key, None)
        if isinstance(incoming_module_execution, dict):
            merged_execution = _merge_existing_module_execution(
                dict(result.get("module_execution") or {}),
                dict(incoming_module_execution or {}),
            )
            if merged_execution:
                result["module_execution"] = merged_execution
        if plan_binding_matches and isinstance(incoming_coder_work_order, dict):
            result["coder_work_order"] = dict(incoming_coder_work_order)
    result.update(updates)
    return result

def _merge_existing_module_execution(stored: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    if not stored:
        return {}
    if not _module_execution_binding_matches(stored, incoming):
        return stored
    result = dict(stored)
    for key in (
        "current_milestone_index",
        "status",
        "auto_advance",
        "checkpoint_review",
        "last_checkpoint_id",
        "last_checkpoint_review_gate_id",
        "pending_experience",
        "completion_reported",
        "completed_at",
        "status_reason",
    ):
        if key in incoming:
            result[key] = incoming[key]
    return result


def _module_execution_binding_matches(stored: dict[str, Any], incoming: dict[str, Any]) -> bool:
    if not stored or not incoming:
        return False
    for key in ("plan_id", "module_id"):
        incoming_value = str(incoming.get(key) or "").strip()
        stored_value = str(stored.get(key) or "").strip()
        if incoming_value and stored_value and incoming_value != stored_value:
            return False
    incoming_revision = _coerce_int(incoming.get("plan_revision"))
    stored_revision = _coerce_int(stored.get("plan_revision"))
    if incoming_revision is not None and stored_revision is not None and incoming_revision != stored_revision:
        return False
    return True


def _has_plan_truth_source(metadata: dict[str, Any]) -> bool:
    if isinstance(metadata.get("plan_artifact"), dict):
        return True
    plan_execution = dict(metadata.get("plan_execution") or {}) if isinstance(metadata.get("plan_execution"), dict) else {}
    if str(plan_execution.get("plan_id") or "").strip():
        return True
    module_execution = dict(metadata.get("module_execution") or {}) if isinstance(metadata.get("module_execution"), dict) else {}
    return bool(str(module_execution.get("plan_id") or "").strip())


def _is_module_parent_execution_metadata(metadata: dict[str, Any]) -> bool:
    plan_execution = dict(metadata.get("plan_execution") or {}) if isinstance(metadata.get("plan_execution"), dict) else {}
    return str(plan_execution.get("mode") or "").strip() == "module_parent_milestones"


def _normalize_plan_backed_milestones(metadata: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(metadata or {})
    plan_execution = dict(normalized.get("plan_execution") or {}) if isinstance(normalized.get("plan_execution"), dict) else {}
    if str(plan_execution.get("mode") or "") == "module_parent_milestones":
        return normalized
    plan_payload = normalized.get("plan_artifact")
    if not isinstance(plan_payload, dict):
        return normalized
    artifact = validate_dispatchable_plan_artifact(plan_payload)
    module_execution = dict(normalized.get("module_execution") or {}) if isinstance(normalized.get("module_execution"), dict) else {}
    requested_module_id = str(module_execution.get("module_id") or normalized.get("module_id") or "").strip()
    resolved_module_id = plan_module_id_at(artifact, module_id=requested_module_id)
    milestones = module_milestone_records(artifact, module_id=resolved_module_id)
    plan_revision = _plan_revision_from_payload(plan_payload, normalized.get("plan_ref"))
    module_execution.update(
        {
            "mode": SERIAL_MODULE_MILESTONES_MODE,
            "plan_id": artifact.plan_id,
            "plan_revision": plan_revision,
            "module_id": resolved_module_id,
            "milestone_count": len(milestones),
        }
    )
    module_execution.setdefault("current_milestone_index", 0)
    module_execution.setdefault("auto_advance", True)
    module_execution.setdefault("status", "active")
    normalized["module_id"] = resolved_module_id
    normalized["milestones"] = milestones
    normalized["module_execution"] = module_execution
    return normalized


def _normalize_milestone_execution_metadata(metadata: dict[str, Any], milestones: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = dict(metadata or {})
    normalized_milestones = [dict(item) for item in list(milestones or []) if isinstance(item, dict)]
    if not normalized_milestones:
        return normalized
    normalized["milestones"] = normalized_milestones
    plan_execution = dict(normalized.get("plan_execution") or {})
    if str(plan_execution.get("mode") or "") == "module_parent_milestones":
        return normalized
    module_execution = dict(normalized.get("module_execution") or {})
    if not isinstance(normalized.get("plan_artifact"), dict):
        if str(module_execution.get("mode") or "") in SERIAL_MILESTONE_MODES:
            raise ValueError("serial milestone execution requires plan_artifact")
        normalized.pop("milestones", None)
        normalized.pop("module_execution", None)
        return normalized
    module_execution["mode"] = SERIAL_MODULE_MILESTONES_MODE
    module_execution.setdefault("current_milestone_index", 0)
    module_execution["milestone_count"] = len(normalized_milestones)
    module_execution.setdefault("auto_advance", True)
    module_execution.setdefault("status", "active")
    normalized["module_execution"] = module_execution
    return normalized


def _sync_module_execution_cursor_from_continuity(metadata: dict[str, Any], continuity: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(metadata or {})
    module_execution = dict(normalized.get("module_execution") or {})
    if str(module_execution.get("mode") or "") not in SERIAL_MILESTONE_MODES:
        return normalized
    current = dict((continuity or {}).get("current_milestone") or {})
    milestone_index = _coerce_int(current.get("milestone_index"))
    if milestone_index is None:
        return normalized
    module_execution["current_milestone_index"] = int(milestone_index)
    if _coerce_int(current.get("milestone_count")) is not None:
        module_execution["milestone_count"] = int(_coerce_int(current.get("milestone_count")) or 0)
    normalized["module_execution"] = module_execution
    return normalized


def _normalize_preferred_endpoint_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(metadata or {})
    preferred_endpoint_id = str(normalized.get("preferred_endpoint_id") or "").strip()
    if preferred_endpoint_id:
        normalized["preferred_endpoint_id"] = preferred_endpoint_id
        normalized.setdefault("preferred_endpoint_source", "explicit")
    return normalized


def _module_parent_milestones(artifact: PlanArtifact, *, module_order: list[str] | None = None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    modules_by_id = {module.module_id: module for module in artifact.modules if module.module_id}
    ordered_modules = [modules_by_id[module_id] for module_id in list(module_order or []) if module_id in modules_by_id]
    if not ordered_modules:
        ordered_modules = list(artifact.modules)
    for index, module in enumerate(ordered_modules):
        title = module.module_id or f"Module {index + 1}"
        acceptance: list[str] = []
        for milestone in module.internal_milestones:
            acceptance.extend(list(milestone.acceptance_criteria))
        if not acceptance and module.responsibility:
            acceptance.append(module.responsibility)
        result.append(
            {
                "title": title,
                "summary": module.responsibility or title,
                "acceptance": acceptance,
            }
        )
    return result or [{"title": "Complete plan", "summary": artifact.summary or "Complete plan", "acceptance": []}]


def _prompt_view_from_current_milestone(
    pack: TaskContextPack,
    *,
    continuity: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    current = dict((continuity or {}).get("current_milestone") or {})
    if not current:
        return {}
    milestone_index = _coerce_int(current.get("milestone_index"))
    if milestone_index is None:
        milestone_index = 0
    acceptance = _coerce_text_list(current.get("acceptance") or current.get("acceptance_criteria") or pack.acceptance_criteria)
    title = str(current.get("title") or f"Milestone {milestone_index + 1}").strip()
    task = str(current.get("task") or current.get("summary") or current.get("goal") or title).strip()
    milestone = {
        "milestone_id": str(current.get("milestone_id") or current.get("id") or f"m{milestone_index + 1}").strip(),
        "milestone_index": int(milestone_index),
        "milestone_count": (
            _coerce_int(current.get("milestone_count"))
            or len([item for item in list(metadata.get("milestones") or []) if isinstance(item, dict)])
        ),
        "title": title,
        "task": task,
        "acceptance_criteria": acceptance,
    }
    if isinstance(current.get("test_plan"), dict):
        milestone["test_plan"] = dict(current.get("test_plan") or {})
    module_id = str(metadata.get("module_id") or current.get("module_id") or "").strip()
    role = _role_from_profile(pack.minion_profile)
    return {
        "role": role,
        "goal": str(pack.goal or ""),
        "module": {"module_id": module_id} if module_id else {},
        "milestone": milestone,
        "relevant_contracts": [],
        "skill_refs": _coerce_text_list(current.get("skill_refs") or metadata.get("skill_refs")),
        "allowed_capabilities": list(pack.allowed_capabilities),
        "test_plan": dict(current.get("test_plan") or {}),
        "output_contract": dict(metadata.get("output_contract") or {}),
        "workspace": _prompt_safe_workspace(pack.workspace),
    }


def _materialize_plan_module_adapter(
    pack: TaskContextPack,
    *,
    metadata: dict[str, Any],
    workspace: dict[str, Any],
) -> dict[str, Any]:
    data = dict(metadata or {})
    module_execution = dict(data.get("module_execution") or {})
    if str(module_execution.get("mode") or "") not in SERIAL_MILESTONE_MODES:
        return data
    plan_payload = data.get("plan_artifact")
    if not isinstance(plan_payload, dict):
        return data
    artifact = PlanArtifact.from_dict(plan_payload)
    module_id = str(module_execution.get("module_id") or data.get("module_id") or "").strip()
    if not module_id:
        module_id = plan_module_id_at(artifact)
    milestone_index = _coerce_int(module_execution.get("current_milestone_index"))
    if milestone_index is None:
        milestone_index = 0
    milestone_id = plan_milestone_id_at(artifact, module_id=module_id, milestone_index=milestone_index)
    execution_contract = _execution_contract_from_pack(pack, metadata=data, workspace=workspace)
    adapter = _module_adapter_from_contract(execution_contract)
    role = _module_role_from_contract(execution_contract, fallback_profile=pack.minion_profile)
    module_execution["module_adapter"] = adapter
    module_execution["profile_role"] = role
    if execution_contract:
        module_execution["execution_contract"] = dict(execution_contract)
    module_execution["status"] = "active"
    module_execution.setdefault("module_name", str(data.get("module_name") or module_id).strip())
    data["module_execution"] = module_execution
    if adapter == _MODULE_ADAPTER_CODER_WORK_ORDER:
        order = compile_coder_work_order(
            artifact,
            module_id=module_id,
            milestone_id=milestone_id,
            work_order_id=str(pack.work_order_id or data.get("work_order_id") or ""),
            allowed_capabilities=list(pack.allowed_capabilities)
            or _coerce_text_list(data.get("coder_allowed_capabilities"))
            or _coerce_text_list(data.get("allowed_capabilities")),
            workspace=workspace,
        )
        order_payload = order.to_dict()
        dependency_outputs = [
            dict(item)
            for item in list(data.get("module_dependency_outputs") or workspace.get("module_dependency_outputs") or [])
            if isinstance(item, dict)
        ]
        if dependency_outputs:
            order_metadata = dict(order_payload.get("metadata") or {})
            order_metadata["module_dependency_outputs"] = dependency_outputs
            dependency_context = [
                dict(item)
                for item in list(order_metadata.get("module_dependency_context") or [])
                if isinstance(item, dict)
            ]
            outputs_by_module = {
                str(item.get("module_id") or "").strip(): dict(item)
                for item in dependency_outputs
                if str(item.get("module_id") or "").strip()
            }
            for item in dependency_context:
                module_output = outputs_by_module.get(str(item.get("module_id") or "").strip())
                if module_output:
                    item["module_output"] = module_output
            order_metadata["module_dependency_context"] = dependency_context
            order_payload["metadata"] = order_metadata
        if str(module_execution.get("module_kind") or "") == "join":
            output_contract = dict(order_payload.get("output_contract") or {})
            output_contract["verification_only_no_change_allowed"] = True
            output_contract["artifact_required_when_no_change"] = True
            output_contract["no_change_completion"] = (
                "If the join module only verifies already-integrated module outputs and no source/test/doc/config "
                "changes are required, produce a verification artifact/report instead of creating an empty checkpoint commit."
            )
            order_payload["output_contract"] = output_contract
        data["coder_work_order"] = order_payload
        data.pop("prompt_view", None)
        return data
    data.pop("coder_work_order", None)
    prompt_view = _plan_milestone_prompt_view(
        artifact,
        module_id=module_id,
        milestone_id=milestone_id,
        work_order_id=str(pack.work_order_id or data.get("work_order_id") or ""),
        role=role,
        allowed_capabilities=list(pack.allowed_capabilities) or _coerce_text_list(data.get("allowed_capabilities")),
        workspace=workspace,
    )
    data["prompt_view"] = prompt_view
    return data


def _is_plan_module_child_metadata(metadata: dict[str, Any]) -> bool:
    data = dict(metadata or {})
    parent = str(data.get("parent_work_order_id") or "").strip()
    module = str(data.get("parent_module_id") or data.get("module_id") or "").strip()
    return bool(parent and module)


def _plan_milestone_prompt_view(
    artifact: PlanArtifact,
    *,
    module_id: str,
    milestone_id: str,
    work_order_id: str,
    role: str,
    allowed_capabilities: list[str],
    workspace: dict[str, Any],
) -> dict[str, Any]:
    order = compile_coder_work_order(
        artifact,
        module_id=module_id,
        milestone_id=milestone_id,
        work_order_id=work_order_id,
        allowed_capabilities=allowed_capabilities,
        workspace=workspace,
    )
    view = prompt_view_for_coder(order).to_dict()
    view["role"] = str(role or "minion")
    view["output_contract"] = {
        "must_return": [
            "summary",
            "milestone_report",
            "artifacts_or_outputs",
            "verification_or_blockers",
        ]
    }
    return view


def _latest_child_terminal_failure_status(db: sqlite3.Connection, child_work_order_id: str) -> str:
    child_id = str(child_work_order_id or "").strip()
    if not child_id:
        return ""
    row = db.execute(
        """
        SELECT status FROM minion_worker_checkpoints
        WHERE work_order_id = ?
          AND status IN ('failed', 'blocked', 'killed')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (child_id,),
    ).fetchone()
    if row is not None:
        return str(dict(row).get("status") or "").strip().lower()
    row = db.execute(
        "SELECT status FROM minion_work_orders WHERE work_order_id = ? LIMIT 1",
        (child_id,),
    ).fetchone()
    if row is not None:
        status = str(dict(row).get("status") or "").strip().lower()
        if status in {"failed", "blocked"}:
            return status
    rows = db.execute(
        """
        SELECT payload_json FROM minion_worker_ledger
        WHERE work_order_id = ?
          AND event_kind = 'terminal'
        ORDER BY created_at DESC
        LIMIT 5
        """,
        (child_id,),
    ).fetchall()
    for item in rows:
        status = str(_loads_or_dict(dict(item).get("payload_json")).get("status") or "").strip().lower()
        if status in {"failed", "blocked", "killed"}:
            return status
    return ""


def _role_from_profile(profile: str) -> str:
    parts = [part for part in str(profile or "").replace("/", ".").split(".") if part]
    return parts[-1] if parts else "minion"


def _compact_work_order_search_item(snapshot: dict[str, Any]) -> dict[str, Any]:
    work_order = dict(snapshot.get("work_order") or {})
    task = dict(snapshot.get("task") or {})
    return {
        "work_order_id": work_order.get("work_order_id") or snapshot.get("work_order_id") or "",
        "task_id": work_order.get("task_id") or task.get("task_id") or "",
        "title": work_order.get("title") or "",
        "status": work_order.get("status") or "",
        "current_milestone": snapshot.get("current_milestone") or {},
        "current_worker": snapshot.get("current_worker") or {},
    }


def _compact_work_order_draft_search_item(snapshot: dict[str, Any]) -> dict[str, Any]:
    draft = dict(snapshot.get("draft") or {})
    candidate = dict(snapshot.get("work_order_candidate") or {})
    return {
        "draft_id": draft.get("draft_id") or snapshot.get("draft_id") or "",
        "title": draft.get("title") or "",
        "goal": draft.get("goal") or "",
        "status": draft.get("status") or "",
        "task_id": draft.get("task_id") or candidate.get("task_id") or "",
        "proposed_work_order_id": draft.get("proposed_work_order_id") or candidate.get("work_order_id") or "",
        "milestone_count": len(list(candidate.get("milestones") or [])),
    }


def _pack_from_work_order_candidate(candidate: dict[str, Any], *, draft: dict[str, Any] | None = None) -> TaskContextPack:
    draft = dict(draft or {})
    metadata = _loads_or_dict(candidate.get("metadata"))
    task_id = str(candidate.get("task_id") or draft.get("task_id") or metadata.get("task_id") or "").strip()
    if task_id:
        metadata["task_id"] = task_id
    title = str(candidate.get("title") or draft.get("title") or candidate.get("goal") or "").strip()
    if title:
        metadata.setdefault("task_title", title)
        metadata.setdefault("work_order_title", title)
    if draft.get("draft_id"):
        metadata.setdefault("work_order_draft_id", str(draft.get("draft_id")))
    milestones = _coerce_milestones(
        candidate.get("milestones"),
        _coerce_text_list(candidate.get("acceptance_criteria")),
        str(candidate.get("instruction") or candidate.get("goal") or title),
    )
    metadata["milestones"] = milestones
    return TaskContextPack.from_dict(
        {
            "work_order_id": str(candidate.get("work_order_id") or draft.get("proposed_work_order_id") or new_work_id("wo")),
            "goal": str(candidate.get("goal") or title),
            "instruction": str(candidate.get("instruction") or candidate.get("goal") or title),
            "acceptance_criteria": _coerce_text_list(candidate.get("acceptance_criteria")),
            "workspace": _loads_or_dict(candidate.get("workspace")),
            "artifacts": [dict(item) for item in list(candidate.get("artifacts") or []) if isinstance(item, dict)],
            "minion_profile": str(candidate.get("minion_profile") or draft.get("minion_profile") or "generic"),
            "metadata": metadata,
        }
    )


def _pack_from_work_order_snapshot(snapshot: dict[str, Any]) -> TaskContextPack:
    work_order = dict(snapshot.get("work_order") or {})
    metadata = _loads_or_dict(work_order.get("metadata"))
    workspace = _loads_or_dict(metadata.get("workspace"))
    artifacts = [dict(item) for item in list(metadata.get("artifacts") or []) if isinstance(item, dict)]
    acceptance = _coerce_text_list(metadata.get("acceptance_criteria"))
    if not acceptance:
        for milestone in list(snapshot.get("milestones") or []):
            acceptance.extend(_coerce_text_list((milestone or {}).get("acceptance")))
    return TaskContextPack.from_dict(
        {
            "work_order_id": str(work_order.get("work_order_id") or snapshot.get("work_order_id") or ""),
            "goal": str(work_order.get("goal") or ""),
            "instruction": str(work_order.get("instruction") or work_order.get("goal") or ""),
            "acceptance_criteria": acceptance,
            "workspace": workspace,
            "artifacts": artifacts,
            "profile_group": str(work_order.get("profile_group") or ""),
            "profile_name": str(work_order.get("profile_name") or ""),
            "minion_profile": str(work_order.get("minion_profile") or "generic"),
            "metadata": metadata,
        }
    )


def _merge_pack_overrides(base: TaskContextPack, overrides: dict[str, Any]) -> TaskContextPack:
    data = base.to_dict()
    metadata = dict(data.get("metadata") or {})
    override_metadata = _loads_or_dict(overrides.get("metadata"))
    if override_metadata:
        metadata = _merge_existing_work_order_metadata(metadata, override_metadata)
    metadata = _strip_raw_milestone_metadata_without_plan(metadata)
    for key in (
        "goal",
        "instruction",
        "acceptance_criteria",
        "workspace",
        "artifacts",
        "memory_pack",
        "allowed_capabilities",
        "allowed_skills",
        "approval_policy",
        "profile_group",
        "profile_name",
        "minion_profile",
        "continuity",
    ):
        value = overrides.get(key)
        if isinstance(value, str):
            if value.strip():
                data[key] = value
            continue
        if value:
            data[key] = value
    data["metadata"] = metadata
    return TaskContextPack.from_dict(data)


def _merge_work_order_candidate(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    result = dict(base or {})
    for key, value in dict(overrides or {}).items():
        if key == "metadata":
            metadata = _loads_or_dict(result.get("metadata"))
            metadata.update(_loads_or_dict(value))
            result["metadata"] = metadata
            continue
        if key == "workspace":
            workspace = _loads_or_dict(result.get("workspace"))
            workspace.update(_loads_or_dict(value))
            if workspace:
                result["workspace"] = workspace
            continue
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                result[key] = value
            continue
        if value:
            result[key] = value
    return result


def _current_worker_for_work_order(work_order_id: str, active_runs: list[dict[str, Any]]) -> dict[str, Any]:
    return dict(_active_worker_by_work_order(active_runs).get(str(work_order_id or "").strip()) or {})


def _active_worker_by_work_order(active_runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for run in active_runs:
        if not isinstance(run, dict):
            continue
        work_order_id = str(run.get("work_order_id") or "").strip()
        if not work_order_id:
            continue
        if str(run.get("status") or "") in {"starting", "running", "approval_pending", "clarification_pending"}:
            result[work_order_id] = dict(run)
    return result


def _decode_json_fields(row: dict[str, Any], *, keep_json: bool = True) -> dict[str, Any]:
    result = dict(row)
    for key in list(result):
        if key.endswith("_json"):
            decoded_key = key[:-5]
            result[decoded_key] = _loads(result.get(key))
            if not keep_json:
                result.pop(key, None)
    return result


def _persistent_workspace_metadata(workspace: dict[str, Any]) -> dict[str, Any]:
    result = dict(workspace or {})
    for key in _RUN_WORKSPACE_KEYS:
        result.pop(key, None)
    return result


def _plan_child_workspace_from_parent(workspace: Any) -> dict[str, Any]:
    result = _loads_or_dict(workspace)
    for key in _PROFILE_SCOPED_WORKSPACE_KEYS:
        result.pop(key, None)
    return result


def _serial_parent_workspace_after_module(
    parent_workspace: Any,
    child_workspace: dict[str, Any],
    *,
    module_id: str,
    module_name: str = "",
    child_work_order_id: str,
) -> dict[str, Any]:
    repo_path = str((child_workspace or {}).get("repo_path") or "").strip()
    if not repo_path:
        return {}
    result = _persistent_workspace_metadata(parent_workspace if isinstance(parent_workspace, dict) else {})
    result["source_repo"] = repo_path
    work_order_branch = str((child_workspace or {}).get("work_order_branch") or "").strip()
    if work_order_branch:
        result["base_ref"] = work_order_branch
        result["merge_target"] = work_order_branch
    else:
        result.pop("base_ref", None)
        result.pop("merge_target", None)
    result["last_module_baseline"] = {
        "mode": "serial_dependency_baseline",
        "module_id": str(module_id or ""),
        "module_name": str(module_name or module_id or ""),
        "child_work_order_id": str(child_work_order_id or ""),
        "repo_path": repo_path,
        **({"branch": work_order_branch} if work_order_branch else {}),
    }
    return result


def _prompt_safe_workspace(workspace: dict[str, Any]) -> dict[str, str]:
    allowed = {"repo_path", "artifact_dir", "task_repo_path", "target_repo_path"}
    return {key: str(value) for key, value in dict(workspace or {}).items() if key in allowed and str(value or "").strip()}


def _compact_milestone_for_continuity(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    result: dict[str, Any] = {}
    for key in (
        "milestone_id",
        "work_order_id",
        "milestone_index",
        "title",
        "summary",
        "status",
        "completed",
        "created_at",
    ):
        if key in item and item.get(key) not in (None, "", []):
            value = item.get(key)
            result[key] = _compact_text(value) if key == "summary" else value
    acceptance = _coerce_text_list(item.get("acceptance"))
    if acceptance:
        result["acceptance"] = acceptance[:10]
    latest_checkpoint = _compact_event_for_continuity(item.get("latest_checkpoint") or {})
    if latest_checkpoint:
        result["latest_checkpoint"] = latest_checkpoint
    return result


def _compact_event_for_continuity(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    result: dict[str, Any] = {}
    for key in (
        "ledger_id",
        "checkpoint_id",
        "event_kind",
        "status",
        "phase",
        "milestone_index",
        "milestone_title",
        "created_at",
        "run_id",
        "minion_id",
        "work_order_id",
    ):
        if key in item and item.get(key) not in (None, "", []):
            result[key] = item.get(key)
    summary = str(item.get("summary") or "").strip()
    if summary:
        result["summary"] = _compact_text(summary)
    payload = _compact_event_payload(_loads_or_dict(item.get("payload")))
    if payload:
        result["payload"] = payload
    return result


def _compact_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result: dict[str, Any] = {}
    for key in (
        "phase",
        "status",
        "summary",
        "milestone_index",
        "milestone_title",
        "round",
        "tool_name",
        "target_name",
        "tool_call_count",
        "finish_reason",
        "text_preview",
        "error",
        "reason",
        "approval_id",
        "decision",
    ):
        value = payload.get(key)
        if value in (None, "", []):
            continue
        result[key] = _compact_text(value) if isinstance(value, str) else value
    if isinstance(payload.get("prompt_scaffold_summary"), dict):
        result["prompt_scaffold_summary"] = dict(payload.get("prompt_scaffold_summary") or {})
    elif isinstance(payload.get("prompt_scaffold"), dict):
        result["prompt_scaffold_summary"] = _compact_prompt_scaffold_summary(payload.get("prompt_scaffold") or {})
    tool_calls = payload.get("tool_calls")
    if isinstance(tool_calls, list):
        compact_calls = []
        for call in tool_calls[:5]:
            if not isinstance(call, dict):
                continue
            compact_calls.append(
                {
                    key: call.get(key)
                    for key in ("tool_name", "target_name", "call_id")
                    if call.get(key) not in (None, "")
                }
            )
        if compact_calls:
            result["tool_calls"] = compact_calls
    artifacts = _artifact_list(payload.get("artifacts"))
    if artifacts:
        result["artifacts"] = [
            {
                key: artifact.get(key)
                for key in ("title", "relative_path", "path", "role", "size_bytes")
                if artifact.get(key) not in (None, "")
            }
            for artifact in artifacts[:5]
        ]
    return result


def _compact_prompt_scaffold_summary(scaffold: dict[str, Any]) -> dict[str, Any]:
    try:
        return _prompt_scaffold_summary(dict(scaffold or {}))
    except Exception:
        continuity = _loads_or_dict(scaffold.get("continuity"))
        return {
            "task_goal": _compact_text(scaffold.get("instruction")),
            "acceptance_checklist": [],
            "allowed_capability_count": len(list(scaffold.get("allowed_capabilities") or [])),
            "continuity": {
                "keys": sorted(str(key) for key in continuity.keys()),
                "recent_ledger_count": len(list(continuity.get("recent_ledger") or [])),
                "completed_milestone_count": len(list(continuity.get("completed_milestones") or [])),
                "task_lesson_count": len(list(continuity.get("task_lessons") or [])),
            },
        }


def _compact_text(value: Any, *, limit: int = _CONTINUITY_TEXT_LIMIT) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)].rstrip()}..."


def _artifact_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _merge_artifacts(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values:
        key = str(item.get("path") or item.get("relative_path") or item.get("sha256") or "").strip()
        if not key:
            key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(item))
    return result


def _experience_payload(value: Any) -> dict[str, Any]:
    data = dict(value or {}) if isinstance(value, dict) else {}
    return {
        "task_lessons": _coerce_text_list(data.get("task_lessons")),
        "system_lessons": _coerce_text_list(data.get("system_lessons")),
        "memory_candidates": _artifact_list(data.get("memory_candidates")),
    }


def _normalize_repair_bill_payload(
    payload: dict[str, Any],
    *,
    parent_work_order_id: str,
    module_order: list[str],
) -> dict[str, Any]:
    raw = dict(payload or {})
    known_modules = {str(item).strip() for item in module_order if str(item or "").strip()}
    module_patches: dict[str, dict[str, Any]] = {}
    unknown_modules: list[str] = []

    def add_patch(module_id: str, patch: dict[str, Any]) -> None:
        normalized_module_id = str(module_id or patch.get("module_name") or patch.get("module_id") or "").strip()
        if not normalized_module_id:
            return
        if normalized_module_id not in known_modules:
            unknown_modules.append(normalized_module_id)
            return
        normalized = _normalize_repair_module_patch(normalized_module_id, patch, default_defect_kind=str(raw.get("defect_kind") or ""))
        existing = dict(module_patches.get(normalized_module_id) or {})
        module_patches[normalized_module_id] = _merge_repair_module_patch(existing, normalized)

    for module_id, patch in dict(raw.get("module_patches") or {}).items():
        if isinstance(patch, dict):
            add_patch(str(module_id), patch)
    for patch in list(raw.get("modules") or []):
        if isinstance(patch, dict):
            add_patch(str(patch.get("module_name") or patch.get("module_id") or ""), patch)
    if not module_patches and isinstance(raw.get("module_patch"), dict):
        patch = dict(raw.get("module_patch") or {})
        add_patch(str(patch.get("module_name") or patch.get("module_id") or raw.get("module_name") or raw.get("module_id") or ""), patch)
    bill_id = str(raw.get("bill_id") or f"repair_bill_{uuid4().hex[:16]}").strip()
    return {
        "bill_id": bill_id,
        "parent_work_order_id": parent_work_order_id,
        "source_module_id": str(raw.get("source_module_id") or raw.get("reporting_module_id") or "").strip(),
        "summary": str(raw.get("summary") or raw.get("title") or "repair bill submitted").strip(),
        "status": "submitted",
        "module_patches": module_patches,
        "unknown_modules": _dedupe_text(unknown_modules),
    }


def _normalize_repair_module_patch(module_id: str, patch: dict[str, Any], *, default_defect_kind: str = "") -> dict[str, Any]:
    raw = dict(patch or {})
    defect_kind = str(raw.get("defect_kind") or default_defect_kind or "module_defect").strip().lower()
    if defect_kind not in _REPAIR_BILL_DEFECT_KINDS:
        defect_kind = "triage_required"
    acceptance_items = _dedupe_repair_acceptance_items(
        _repair_acceptance_items(
            [
                *_repair_listish(raw.get("acceptance_criteria")),
                *_repair_listish(raw.get("additional_acceptance_criteria")),
                *[
                    criterion
                    for milestone in _repair_listish(raw.get("internal_milestones") or raw.get("milestones"))
                    if isinstance(milestone, dict)
                    for criterion in _repair_listish(milestone.get("acceptance_criteria") or milestone.get("acceptance"))
                ],
            ]
        )
    )
    return {
        "module_id": str(module_id or "").strip(),
        "defect_kind": defect_kind,
        "summary": str(raw.get("summary") or raw.get("responsibility") or "").strip(),
        "additional_acceptance_criteria": _dedupe_text([str(item.get("criterion") or "") for item in acceptance_items]),
        "acceptance_criteria": acceptance_items,
        "negative_cases": _dedupe_dicts(_repair_case_items(raw.get("negative_cases"))),
        "evidence": _dedupe_dicts(_repair_evidence_items(raw.get("evidence"))),
    }


def _merge_repair_module_patch(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    if not existing:
        return dict(incoming)
    defect_kinds = _dedupe_text([str(existing.get("defect_kind") or ""), str(incoming.get("defect_kind") or "")])
    result = dict(existing)
    if "architecture_defect" in defect_kinds:
        result["defect_kind"] = "architecture_defect"
    elif "contract_defect" in defect_kinds:
        result["defect_kind"] = "contract_defect"
    elif "module_defect" in defect_kinds:
        result["defect_kind"] = "module_defect"
    elif defect_kinds:
        result["defect_kind"] = defect_kinds[0]
    else:
        result["defect_kind"] = "triage_required"
    result["summary"] = "; ".join(_dedupe_text([str(existing.get("summary") or ""), str(incoming.get("summary") or "")]))
    result["additional_acceptance_criteria"] = _dedupe_text(
        [* _coerce_text_list(existing.get("additional_acceptance_criteria")), * _coerce_text_list(incoming.get("additional_acceptance_criteria"))]
    )
    result["acceptance_criteria"] = _dedupe_repair_acceptance_items(
        [
            *[dict(item) for item in list(existing.get("acceptance_criteria") or []) if isinstance(item, dict)],
            *[dict(item) for item in list(incoming.get("acceptance_criteria") or []) if isinstance(item, dict)],
        ]
    )
    result["negative_cases"] = _dedupe_dicts(
        [
            *[dict(item) for item in list(existing.get("negative_cases") or []) if isinstance(item, dict)],
            *[dict(item) for item in list(incoming.get("negative_cases") or []) if isinstance(item, dict)],
        ]
    )
    result["evidence"] = _dedupe_dicts(
        [
            *[dict(item) for item in list(existing.get("evidence") or []) if isinstance(item, dict)],
            *[dict(item) for item in list(incoming.get("evidence") or []) if isinstance(item, dict)],
        ]
    )
    return result


def _repair_acceptance_items(values: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    raw_items = values if isinstance(values, (list, tuple)) else [values]
    for index, item in enumerate(raw_items, start=1):
        if isinstance(item, str):
            criterion = item.strip()
            if not criterion:
                continue
            result.append(
                {
                    "id": f"RAC-{index}",
                    "criterion": criterion,
                    "evidence_expectation": "Focused repair verification covers this added criterion.",
                    "negative_cases": [],
                }
            )
            continue
        if not isinstance(item, dict):
            continue
        criterion = str(item.get("criterion") or item.get("summary") or item.get("text") or "").strip()
        if not criterion:
            continue
        result.append(
            {
                "id": str(item.get("id") or f"RAC-{index}").strip(),
                "criterion": criterion,
                "evidence_expectation": str(item.get("evidence_expectation") or item.get("evidence") or "Focused repair verification covers this added criterion.").strip(),
                "negative_cases": _coerce_text_list(item.get("negative_cases")),
                "gate_check_refs": _coerce_text_list(item.get("gate_check_refs")),
                "quantifier": str(item.get("quantifier") or "").strip(),
            }
        )
    return result


def _dedupe_repair_acceptance_items(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, dict):
            continue
        criterion = str(item.get("criterion") or "").strip()
        key = criterion.casefold() if criterion else json.dumps(dict(item or {}), ensure_ascii=False, sort_keys=True, default=str)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(dict(item))
    return result


def _repair_listish(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _repair_case_items(values: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    raw_items = values if isinstance(values, (list, tuple)) else ([values] if values not in (None, "") else [])
    for index, item in enumerate(raw_items, start=1):
        if isinstance(item, dict):
            case = {key: value for key, value in dict(item).items() if value not in ("", None, [], {})}
            if case:
                case.setdefault("id", f"NEG-{index}")
                result.append(case)
            continue
        text = str(item or "").strip()
        if text:
            result.append({"id": f"NEG-{index}", "case": text})
    return result


def _repair_evidence_items(values: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    raw_items = values if isinstance(values, (list, tuple)) else ([values] if values not in (None, "") else [])
    for index, item in enumerate(raw_items, start=1):
        if isinstance(item, dict):
            evidence = {key: value for key, value in dict(item).items() if value not in ("", None, [], {})}
            if evidence:
                evidence.setdefault("id", f"EVD-{index}")
                result.append(evidence)
            continue
        text = str(item or "").strip()
        if text:
            result.append({"id": f"EVD-{index}", "summary": text})
    return result


def _repair_overlay_from_plan_execution(plan_execution: dict[str, Any]) -> dict[str, Any]:
    source = dict(plan_execution or {})
    overlay = dict(source.get("repair_overlay") or source)
    return {
        "version": max(0, _coerce_int(overlay.get("version")) or 0),
        "module_patches": {
            str(module_id): dict(patch)
            for module_id, patch in dict(overlay.get("module_patches") or {}).items()
            if str(module_id or "").strip() and isinstance(patch, dict)
        },
        "bill_ids": _coerce_text_list(overlay.get("bill_ids")),
    }


def _merge_repair_overlay(
    existing: dict[str, Any],
    bill: dict[str, Any],
    *,
    replay_targets: list[str],
) -> dict[str, Any]:
    overlay = _repair_overlay_from_plan_execution(existing)
    version = int(overlay.get("version") or 0) + 1
    patches = {
        str(module_id): dict(patch)
        for module_id, patch in dict(overlay.get("module_patches") or {}).items()
        if isinstance(patch, dict)
    }
    bill_id = str(bill.get("bill_id") or "").strip()
    for module_id in replay_targets:
        incoming = dict(dict(bill.get("module_patches") or {}).get(module_id) or {})
        if not incoming:
            continue
        previous = dict(patches.get(module_id) or {})
        merged = _merge_repair_module_patch(previous, incoming) if previous else incoming
        merged["module_id"] = module_id
        merged["overlay_version"] = version
        merged["source_bill_ids"] = _dedupe_text([*_coerce_text_list(previous.get("source_bill_ids")), bill_id])
        patches[module_id] = merged
    return {
        "version": version,
        "bill_ids": _dedupe_text([*_coerce_text_list(overlay.get("bill_ids")), bill_id]),
        "module_patches": patches,
    }


def _repair_context_for_module(overlay: dict[str, Any], module_id: str) -> dict[str, Any]:
    patch = dict(dict(overlay.get("module_patches") or {}).get(str(module_id or "").strip()) or {})
    if not patch:
        return {}
    return {
        "kind": "repair_bill_overlay",
        "module_id": str(module_id or "").strip(),
        "overlay_version": int(overlay.get("version") or patch.get("overlay_version") or 0),
        "defect_kind": str(patch.get("defect_kind") or ""),
        "summary": str(patch.get("summary") or ""),
        "source_bill_ids": _coerce_text_list(patch.get("source_bill_ids")),
        "additional_acceptance_criteria": _coerce_text_list(patch.get("additional_acceptance_criteria")),
        "acceptance_criteria": [dict(item) for item in list(patch.get("acceptance_criteria") or []) if isinstance(item, dict)],
        "negative_cases": [dict(item) for item in list(patch.get("negative_cases") or []) if isinstance(item, dict)],
        "evidence": [dict(item) for item in list(patch.get("evidence") or []) if isinstance(item, dict)],
    }


def _apply_repair_context_to_coder_order_payload(order_payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    payload = dict(order_payload or {})
    metadata = dict(payload.get("metadata") or {})
    metadata["repair_context"] = dict(context)
    payload["metadata"] = metadata
    return payload


def _apply_repair_context_to_prompt_view(prompt_view: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    view = dict(prompt_view or {})
    view["repair_context"] = dict(context)
    return view


def _repair_context_from_spawn_pack(
    metadata: dict[str, Any],
    workspace: dict[str, Any],
    prompt_view: dict[str, Any] | None,
) -> dict[str, Any]:
    candidates: list[Any] = [
        metadata.get("repair_context"),
        workspace.get("repair_context"),
    ]
    coder_work_order = metadata.get("coder_work_order")
    if isinstance(coder_work_order, dict):
        candidates.append(dict(coder_work_order.get("metadata") or {}).get("repair_context"))
        current_milestone = coder_work_order.get("current_milestone")
        if isinstance(current_milestone, dict):
            candidates.append(dict(current_milestone.get("metadata") or {}).get("repair_context"))
    metadata_prompt_view = metadata.get("prompt_view")
    if isinstance(metadata_prompt_view, dict):
        candidates.extend(_repair_context_candidates_from_prompt_view(metadata_prompt_view))
    if isinstance(prompt_view, dict):
        candidates.extend(_repair_context_candidates_from_prompt_view(prompt_view))
    for candidate in candidates:
        context = _loads_or_dict(candidate)
        if context:
            return context
    return {}


def _repair_context_candidates_from_prompt_view(prompt_view: dict[str, Any]) -> list[Any]:
    candidates: list[Any] = [prompt_view.get("repair_context")]
    milestone = prompt_view.get("milestone")
    if isinstance(milestone, dict):
        candidates.append(dict(milestone.get("metadata") or {}).get("repair_context"))
    current_milestone = prompt_view.get("current_milestone")
    if isinstance(current_milestone, dict):
        candidates.append(dict(current_milestone.get("metadata") or {}).get("repair_context"))
    return candidates


def _plan_module_child_work_order_id(parent_work_order_id: str, module_id: str, plan_execution: dict[str, Any]) -> str:
    attempts = dict(plan_execution.get("module_replay_attempts") or {})
    attempt = max(0, _coerce_int(attempts.get(str(module_id or "").strip())) or 0)
    suffix = f"_r{attempt}" if attempt > 0 else ""
    return f"wo_{_safe_id(str(parent_work_order_id))}_{_safe_id(module_id)}{suffix}"


def _dedupe_text(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _dedupe_dicts(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        key = json.dumps(dict(value or {}), ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(value))
    return result


def _drop_empty_dict(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in dict(value or {}).items() if item not in ("", None, [], {})}


def _plan_execution_running_children(plan_execution: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    dag = _plan_execution_dag_state(plan_execution)
    for module_id, child_id in dict(dag.get("running_modules") or {}).items():
        module = str(module_id or "").strip()
        child = str(child_id or "").strip()
        if module and child:
            result[module] = child
    if not result:
        module_id = str(plan_execution.get("current_module_id") or "").strip()
        child_id = str(plan_execution.get("active_child_work_order_id") or "").strip()
        if module_id and child_id:
            result[module_id] = child_id
    return result


def _table_row_count(db: sqlite3.Connection, table_name: str) -> int:
    try:
        row = db.execute(f"SELECT COUNT(*) AS count FROM {table_name}").fetchone()
    except sqlite3.Error:
        return 0
    return int((row["count"] if isinstance(row, sqlite3.Row) else row[0]) or 0) if row is not None else 0


def _attached_table_row_count(db: sqlite3.Connection, schema_name: str, table_name: str) -> int:
    try:
        row = db.execute(f"SELECT COUNT(*) AS count FROM {schema_name}.{table_name}").fetchone()
    except sqlite3.Error:
        return 0
    return int((row["count"] if isinstance(row, sqlite3.Row) else row[0]) or 0) if row is not None else 0


def _attached_table_exists(db: sqlite3.Connection, schema_name: str, table_name: str) -> bool:
    try:
        row = db.execute(
            f"SELECT 1 FROM {schema_name}.sqlite_master WHERE type IN ('table', 'view') AND name = ? LIMIT 1",
            (table_name,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _table_columns(db: sqlite3.Connection, table_name: str) -> list[str]:
    try:
        rows = db.execute(f"PRAGMA table_info({table_name})").fetchall()
    except sqlite3.Error:
        return []
    return [str(row["name"] if isinstance(row, sqlite3.Row) else row[1]) for row in rows]


def _attached_table_columns(db: sqlite3.Connection, schema_name: str, table_name: str) -> list[str]:
    try:
        rows = db.execute(f"PRAGMA {schema_name}.table_info({table_name})").fetchall()
    except sqlite3.Error:
        return []
    return [str(row["name"] if isinstance(row, sqlite3.Row) else row[1]) for row in rows]


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _loads(value: Any) -> Any:
    try:
        return json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}


def _loads_or_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        loaded = _loads(value)
        return dict(loaded) if isinstance(loaded, dict) else {}
    return {}


def _work_order_event_log_enabled(repo: Any, db: sqlite3.Connection, work_order_id: str, payload: dict[str, Any]) -> bool:
    if bool((payload or {}).get("force_ledger")):
        return True
    metadata = _loads_or_dict((payload or {}).get("metadata"))
    row = repo._fetch_one(db, "SELECT metadata_json FROM minion_work_orders WHERE work_order_id = ?", (str(work_order_id),))
    if row is not None:
        metadata = _deep_merge_dict(_loads_or_dict(row["metadata_json"]), metadata)
    return minion_debug_log_enabled(metadata)


def _deep_merge_dict(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    result = dict(base or {})
    for key, value in dict(updates or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge_dict(dict(result.get(key) or {}), value)
        else:
            result[key] = value
    return result


def _work_order_artifact_dir(runtime_root: Path, work_order_id: str) -> Path:
    path = Path(runtime_root).expanduser() / "data" / "minion" / "work_orders" / _safe_id(work_order_id) / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_work_order_markdown_artifact(
    runtime_root: Path,
    work_order_id: str,
    relative_path: str,
    *,
    title: str,
    role: str,
    content: str,
) -> dict[str, Any]:
    root = _work_order_artifact_dir(runtime_root, work_order_id)
    relative = Path(str(relative_path or "").strip())
    if not str(relative) or relative.is_absolute():
        raise ValueError("work order artifact relative_path must be relative")
    path = (root / relative).resolve()
    if path != root and root not in path.parents:
        raise ValueError("work order artifact path escapes artifact directory")
    if path == root:
        raise ValueError("work order artifact path must name a file")
    text = str(content or "").strip()
    if not text:
        text = "# Work Order Completion\n\nNo completion details were recorded.\n"
    if not text.endswith("\n"):
        text += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {
        "kind": "file",
        "path": str(path),
        "artifact_dir": str(root),
        "relative_path": str(path.relative_to(root)).replace("\\", "/"),
        "title": str(title or path.stem).strip() or path.name,
        "role": str(role or "summary").strip() or "summary",
        "mime_type": "text/markdown",
        "size_bytes": path.stat().st_size,
        "sha256": digest,
    }


def _plan_completion_markdown(
    parent_work_order_id: str,
    parent_metadata: dict[str, Any],
    plan_execution: dict[str, Any],
) -> str:
    metadata = dict(parent_metadata or {})
    metadata["plan_execution"] = dict(plan_execution or {})
    plan_payload = dict(metadata.get("plan_artifact") or {})
    dag = _plan_execution_dag_state(dict(plan_execution or {}))
    cleanup = dict((plan_execution or {}).get("workspace_cleanup") or metadata.get("workspace_cleanup") or {})
    module_items = _module_status_items_from_metadata(metadata)
    plan_id = str(plan_payload.get("plan_id") or metadata.get("plan_id") or "").strip()
    task_id = str(plan_payload.get("task_id") or metadata.get("task_id") or "").strip()
    summary = str(plan_payload.get("summary") or metadata.get("work_order_title") or metadata.get("task_title") or "").strip()
    lines = [
        "# Work Order Completion",
        "",
        "## Summary",
        f"- Work order: `{parent_work_order_id}`",
        f"- Status: `{str((plan_execution or {}).get('status') or _module_dag_status(dag) or 'completed')}`",
    ]
    if plan_id:
        lines.append(f"- Plan: `{plan_id}`")
    if task_id:
        lines.append(f"- Task: `{task_id}`")
    if summary:
        lines.append(f"- Summary: {summary}")
    if module_items:
        lines.extend(["", "## Modules"])
        for line in _module_status_text(module_items).splitlines()[1:]:
            lines.append(line)
    module_outputs = [dict(value) for value in dict(dag.get("module_outputs") or {}).values() if isinstance(value, dict)]
    if module_outputs:
        lines.extend(["", "## Module Outputs"])
        for output in module_outputs:
            module_id = str(output.get("module_id") or "").strip()
            repo_path = str(output.get("repo_path") or "").strip()
            commit_sha = str(output.get("commit_sha") or "").strip()
            parts = [f"- `{module_id or 'module'}`"]
            if commit_sha:
                parts.append(f"commit `{commit_sha}`")
            if repo_path:
                parts.append(f"repo `{repo_path}`")
            lines.append(": ".join([parts[0], "; ".join(parts[1:])]) if len(parts) > 1 else parts[0])
    if cleanup:
        lines.extend(["", "## Workspace Cleanup"])
        lines.append(f"- Status: `{str(cleanup.get('status') or 'unknown')}`")
        removed = _coerce_text_list(cleanup.get("removed_paths"))
        kept = _coerce_text_list(cleanup.get("kept_paths") or cleanup.get("kept_repo_paths"))
        if removed:
            lines.append("- Removed: " + ", ".join(f"`{item}`" for item in removed[:12]))
        if kept:
            lines.append("- Kept: " + ", ".join(f"`{item}`" for item in kept[:12]))
    return "\n".join(lines).rstrip() + "\n"


def _safe_id(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(value or "").strip())[:80] or uuid4().hex[:12]


def _plan_project_name(artifact: PlanArtifact, *, workspace: dict[str, Any], task_id: str) -> str:
    for source in (dict(artifact.metadata or {}), dict(workspace or {})):
        for key in ("project_name", "project_key", "project_id"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    source_name = _workspace_source_name(workspace)
    if source_name:
        return source_name
    return str(task_id or artifact.task_id or "").strip()


def _workspace_source_name(workspace: dict[str, Any]) -> str:
    for key in ("source_repo", "source_repo_path", "source_path", "clone_from", "repo_url", "remote_url", "repo_path"):
        value = str((workspace or {}).get(key) or "").strip()
        if not value:
            continue
        text = value.rstrip("/")
        leaf = text.rsplit("/", 1)[-1]
        if leaf.endswith(".git"):
            leaf = leaf[:-4]
        if ":" in leaf:
            leaf = leaf.rsplit(":", 1)[-1]
        if leaf:
            return leaf
    return ""


def _planner_revision_workspace(workspace: dict[str, Any]) -> dict[str, Any]:
    source = dict(workspace or {})
    resolved = dict(source)
    derived_keys = {
        *_RUN_WORKSPACE_KEYS,
        *_PROFILE_SCOPED_WORKSPACE_KEYS,
        "common_git_dir",
        "runtime_project_path",
        "work_order_repo_root",
        "workspace_allocation",
        "workspace_kind",
        "lsp_setup",
        "execution_env",
        "work_order_branch",
        "root_work_order_branch",
        "root_merge_target",
        "base_ref",
        "task_project_path",
        "target_project_path",
        "task_repo_path",
        "target_repo_path",
        "module_name",
        "parent_work_order_id",
        "dependency_integration_baseline",
        "dependency_outputs",
        "plan_builder_plan_handle",
        "plan_builder_stage",
        "planning_depth",
        "goal",
        "task_id",
    }
    for key in derived_keys:
        resolved.pop(key, None)

    source_repo = ""
    for key in ("origin_repo_path", "source_repo", "source_repo_path", "source_path", "clone_from", "repo_url", "remote_url"):
        value = str(source.get(key) or "").strip()
        if value:
            source_repo = value
            break

    repo_path = ""
    for key in ("origin_repo_path", "source_repo", "repo_path", "work_order_repo_root", "runtime_project_path"):
        value = str(source.get(key) or "").strip()
        if not value or "://" in value:
            continue
        path = Path(value).expanduser()
        if path.is_dir():
            repo_path = str(path)
            break
    if repo_path:
        resolved["repo_path"] = repo_path
    else:
        resolved.pop("repo_path", None)
    if source_repo:
        resolved["source_repo"] = source_repo
        resolved.setdefault("origin_repo_path", source_repo)
        if not repo_path:
            resolved["workspace_allocation"] = {"mode": "runtime_minion_repo"}
    return resolved


def _plan_revision_from_payload(payload: Any, ref: Any | None = None) -> int:
    payload_dict = dict(payload) if isinstance(payload, dict) else {}
    ref_dict = dict(ref) if isinstance(ref, dict) else {}
    metadata = dict(payload_dict.get("metadata") or {}) if isinstance(payload_dict.get("metadata"), dict) else {}
    for value in (payload_dict.get("plan_revision"), metadata.get("plan_revision"), ref_dict.get("plan_revision")):
        coerced = _coerce_int(value)
        if coerced is not None and coerced >= 0:
            return int(coerced)
    return 0


def _plan_revision_gate_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "gate_id": str(payload.get("gate_id") or ""),
        "gate_kind": str(payload.get("gate_kind") or ""),
        "verdict": str(payload.get("verdict") or ""),
        "summary": str(payload.get("summary") or ""),
        "findings": [dict(item) for item in list(payload.get("findings") or []) if isinstance(item, dict)],
        "required_fixes": [dict(item) for item in list(payload.get("required_fixes") or []) if isinstance(item, dict)],
        "residual_risk": [dict(item) for item in list(payload.get("residual_risk") or []) if isinstance(item, dict)],
        "report_artifact_ref": dict(payload.get("report_artifact_ref") or {}),
    }


def _plan_revision_checklist(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen_actions: set[str] = set()

    def add_item(raw: dict[str, Any], *, source: str) -> None:
        action = _first_text(raw, "description", "suggested_fix", "fix", "summary", "title", fallback="Address reviewer finding.")
        action_key = " ".join(action.lower().split())
        if not action_key or action_key in seen_actions:
            return
        seen_actions.add(action_key)
        item: dict[str, Any] = {
            "id": f"PRC-{len(result) + 1}",
            "source": source,
            "kind": _revision_item_kind(raw, action),
            "severity": _first_text(raw, "severity", fallback="required"),
            "action": _compact_text(action, limit=700),
            "reject_reason": _compact_text(_revision_reject_reason(raw, action), limit=700),
        }
        review_kind = _first_text(raw, "kind", "finding_kind", "defect_kind", fallback="")
        if review_kind and review_kind != item["kind"]:
            item["review_kind"] = review_kind
        target_handle = _first_text(raw, "target_handle", "handle")
        if target_handle:
            item["target_handle"] = target_handle
        target_node = raw.get("target_node")
        if isinstance(target_node, dict):
            item["target_node"] = {
                key: value
                for key, value in dict(target_node).items()
                if key in {"node_kind", "handle", "path", "summary", "module_handle", "module_id", "milestone_handle", "milestone_id"}
                and value not in (None, "", [], {})
            }
            target_path = _first_text(target_node, "path", fallback="")
            if target_path:
                item["target_path"] = target_path
        contract_impact = _first_text(raw, "contract_impact", fallback="")
        if contract_impact:
            item["contract_impact"] = _compact_text(contract_impact, limit=420)
        evidence = _compact_revision_evidence(raw.get("evidence"))
        if evidence:
            item["evidence"] = evidence
        related = _related_plan_handles_and_ids(raw)
        if related:
            item["related_handles"] = related[:8]
        route = _revision_suggested_tool_route(raw, action)
        if route:
            item["suggested_tool_route"] = route
        suggested_tool, suggested_args, needs_replacement = _revision_suggested_tool_and_args(item, raw)
        if suggested_tool:
            item["suggested_tool"] = suggested_tool
        if suggested_args:
            item["suggested_args"] = suggested_args
        if needs_replacement:
            item["needs_replacement"] = True
        result.append(item)

    for raw_fix in [dict(item) for item in list(payload.get("required_fixes") or []) if isinstance(item, dict)]:
        add_item(raw_fix, source="required_fix")
    for raw_finding in [dict(item) for item in list(payload.get("findings") or []) if isinstance(item, dict)]:
        severity = _first_text(raw_finding, "severity", fallback="").lower()
        if result and severity not in {"blocker", "critical", "major", "high", "required"}:
            continue
        add_item(raw_finding, source="finding")
    if result:
        return result[:8]
    summary = _compact_text(payload.get("summary"), limit=700)
    return [
        {
            "id": "PRC-1",
            "source": "review_summary",
            "severity": "required",
            "action": summary or "Revise the submitted plan so the plan_acceptance gate can pass.",
            "suggested_tool_route": ["plan_checkout", "plan_read", "plan_find", "plan_update_* or plan_delete_*", "plan_validate", "plan_submit_for_review"],
        }
    ]


def _revision_item_kind(raw: dict[str, Any], action: str) -> str:
    explicit = _first_text(raw, "revision_kind", "repair_kind", fallback="").lower()
    if explicit in {"missing", "extra", "change"}:
        return explicit
    text = " ".join(
        str(value or "")
        for value in (
            action,
            raw.get("description"),
            raw.get("suggested_fix"),
            raw.get("fix"),
            raw.get("summary"),
            raw.get("title"),
            raw.get("contract_impact"),
        )
    ).lower()
    if any(token in text for token in ("contradict", "conflict", "rewrite", "replace", "update", "stale decision", "stale constraint")):
        return "change"
    if any(token in text for token in ("missing", "omitted", "omit", "not present", "no milestone creates", "no producing milestone", "lacks", "lack of")):
        return "missing"
    if any(token in text for token in ("extra", "unnecessary", "remove", "delete", "duplicate", "stale", "superseded", "should not exist")):
        return "extra"
    return "change"


def _revision_reject_reason(raw: dict[str, Any], action: str) -> str:
    parts = [
        _first_text(raw, "summary", "description", "title", fallback=action),
        _first_text(raw, "contract_impact", fallback=""),
    ]
    return " ".join(part for part in parts if part).strip()


def _revision_suggested_tool_and_args(
    item: dict[str, Any],
    raw: dict[str, Any],
) -> tuple[str, dict[str, Any], bool]:
    revision_kind = _first_text(item, "kind", fallback="change").lower()
    target_node = dict(raw.get("target_node") or item.get("target_node") or {})
    node_kind = _first_text(target_node, "node_kind", fallback="")
    target_handle = _first_text(item, "target_handle", fallback=_first_text(target_node, "handle", fallback=""))
    if not node_kind:
        node_kind = _node_kind_from_plan_handle(target_handle)

    tool = ""
    args: dict[str, Any] = {}
    needs_replacement = revision_kind in {"change", "missing"}
    if node_kind == "decision":
        if revision_kind == "extra":
            tool, args, needs_replacement = "op_minion_plan_delete_design_decision", {"decision_handle": target_handle}, False
        elif revision_kind == "missing":
            tool, args = "op_minion_plan_add_design_decision", {}
        else:
            tool, args = "op_minion_plan_update_design_decision", {"decision_handle": target_handle}
    elif node_kind == "constraint":
        if revision_kind == "extra":
            tool, args, needs_replacement = "op_minion_plan_delete_constraint", {"constraint_handle": target_handle}, False
        elif revision_kind == "missing":
            tool, args = "op_minion_plan_add_constraint", {}
        else:
            tool, args = "op_minion_plan_update_constraint", {"constraint_handle": target_handle}
    elif node_kind == "module":
        if revision_kind == "extra":
            tool, args, needs_replacement = "op_minion_plan_delete_module", {"module_handle": target_handle}, False
        elif revision_kind == "missing":
            tool, args = "op_minion_plan_add_module_outline", {}
        else:
            tool, args = "op_minion_plan_update_module", {"module_handle": target_handle}
    elif node_kind == "milestone":
        if revision_kind == "extra":
            tool, args, needs_replacement = "op_minion_plan_delete_milestone", {"milestone_handle": target_handle}, False
        elif revision_kind == "missing":
            module_handle = _first_text(target_node, "module_handle", fallback="")
            tool, args = "op_minion_plan_add_milestone_outline", {**({"module_handle": module_handle} if module_handle else {})}
        else:
            tool, args = "op_minion_plan_update_milestone", {"milestone_handle": target_handle}
    elif node_kind == "acceptance_criterion":
        if revision_kind == "extra":
            tool, args, needs_replacement = "op_minion_plan_delete_acceptance_criterion", {"acceptance_handle": target_handle}, False
        elif revision_kind == "missing":
            milestone_handle = _first_text(target_node, "milestone_handle", fallback="")
            tool, args = "op_minion_plan_add_acceptance_criterion", {**({"milestone_handle": milestone_handle} if milestone_handle else {})}
        else:
            tool, args = "op_minion_plan_update_acceptance_criterion", {"acceptance_handle": target_handle}
    elif node_kind == "plan":
        tool, args = "op_minion_plan_update_plan", {"plan_handle": target_handle} if target_handle else {}
    else:
        route = _revision_suggested_tool_route(raw, _first_text(item, "action", fallback=""))
        if any("plan_merge_modules" in step for step in route):
            tool = "op_minion_plan_merge_modules"
            needs_replacement = True

    if raw_args := raw.get("suggested_args"):
        if isinstance(raw_args, dict):
            args.update(raw_args)
    if raw_tool := _first_text(raw, "suggested_tool", "tool", fallback=""):
        tool = raw_tool
    return tool, {key: value for key, value in args.items() if value not in (None, "", [], {})}, needs_replacement


def _node_kind_from_plan_handle(handle: str) -> str:
    text = str(handle or "")
    if text.startswith("decision_"):
        return "decision"
    if text.startswith("constraint_"):
        return "constraint"
    if text.startswith("module_"):
        return "module"
    if text.startswith("milestone_"):
        return "milestone"
    if text.startswith("ac_"):
        return "acceptance_criterion"
    if text.startswith("plan_"):
        return "plan"
    return ""


def _compact_revision_evidence(value: Any) -> list[str]:
    if isinstance(value, str):
        return [_compact_text(value, limit=260)] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        return []
    return [_compact_text(item, limit=260) for item in value if str(item or "").strip()][:4]


def _related_plan_handles_and_ids(raw: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for key in ("description", "suggested_fix", "fix", "summary", "title", "contract_impact"):
        value = raw.get(key)
        if value:
            texts.append(str(value))
    evidence = raw.get("evidence")
    if isinstance(evidence, str):
        texts.append(evidence)
    elif isinstance(evidence, (list, tuple)):
        texts.extend(str(item) for item in evidence if str(item or "").strip())
    target_node = raw.get("target_node")
    if isinstance(target_node, dict):
        for key in ("handle", "module_handle", "module_id", "milestone_handle", "milestone_id", "path"):
            value = str(target_node.get(key) or "").strip()
            if value:
                texts.append(value)
    result: list[str] = []
    seen: set[str] = set()
    separators = "()[]{}:,;'\"`\n\t"
    for text in texts:
        cleaned = str(text)
        for separator in separators:
            cleaned = cleaned.replace(separator, " ")
        for token in cleaned.split():
            stripped = token.strip(" .")
            if not stripped:
                continue
            if not (
                stripped.startswith(("module_", "milestone_", "ac_", "gate:", "constraint_", "decision_", "interface_"))
                or "_module_" in stripped
            ):
                continue
            if stripped in seen:
                continue
            seen.add(stripped)
            result.append(stripped)
    return result


def _revision_suggested_tool_route(raw: dict[str, Any], action: str) -> list[str]:
    text = " ".join(
        str(value or "")
        for value in (
            action,
            raw.get("description"),
            raw.get("suggested_fix"),
            raw.get("summary"),
            raw.get("contract_impact"),
        )
    ).lower()
    route = ["plan_checkout"]
    if "merge" in text or "consolidat" in text or "single implementation module" in text:
        route.extend(["plan_find related modules", "plan_merge_modules", "plan_update_module"])
    elif "delete" in text or "remove" in text:
        route.extend(["plan_find target", "plan_delete_*"])
    else:
        route.extend(["plan_find target", "plan_update_*"])
    route.extend(["plan_validate", "plan_submit_for_review"])
    return route


def _first_text(value: dict[str, Any], *keys: str, fallback: str = "") -> str:
    for key in keys:
        text = str(value.get(key) or "").strip()
        if text:
            return text
    return fallback


def _plan_artifact_payload(artifact: PlanArtifact, *, plan_revision: int = 0) -> dict[str, Any]:
    revision = max(0, int(plan_revision or 0))
    payload = {"type": "FinalPlanArtifact", **artifact.to_dict(), "plan_revision": revision}
    metadata = dict(payload.get("metadata") or {})
    metadata.setdefault("plan_revision", revision)
    payload["metadata"] = metadata
    return payload


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _string_list(value: Any) -> list[str]:
    return [str(item).strip() for item in list(value or []) if str(item).strip()]


def _coerce_text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [_clip_text(line.strip(" -\t"), _WORK_ORDER_DRAFT_ITEM_TEXT_LIMIT) for line in value.splitlines() if line.strip(" -\t")]
    return [_clip_text(item, _WORK_ORDER_DRAFT_ITEM_TEXT_LIMIT) for item in _string_list(value)]


def _clip_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    resolved_limit = max(1, int(limit or 1))
    if len(text) <= resolved_limit:
        return text
    if resolved_limit <= 3:
        return text[:resolved_limit]
    return f"{text[: resolved_limit - 3].rstrip()}..."


def _compact_work_order_draft_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in dict(metadata or {}).items():
        normalized_key = str(key or "").strip()
        if not normalized_key:
            continue
        lowered = normalized_key.lower()
        if any(part in lowered for part in _RAW_WORK_ORDER_METADATA_KEY_PARTS):
            continue
        if isinstance(value, str):
            result[normalized_key] = _clip_text(value, _WORK_ORDER_DRAFT_ITEM_TEXT_LIMIT)
            continue
        try:
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            result[normalized_key] = _clip_text(value, _WORK_ORDER_DRAFT_ITEM_TEXT_LIMIT)
            continue
        if len(encoded) <= _WORK_ORDER_DRAFT_METADATA_VALUE_LIMIT:
            result[normalized_key] = value
    return result
