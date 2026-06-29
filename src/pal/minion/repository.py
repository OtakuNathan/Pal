from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from pal.foundation import utc_now
from pal.minion.contracts import (
    SERIAL_MILESTONE_MODES,
    SERIAL_MODULE_MILESTONES_MODE,
)
from pal.minion.debug_log import minion_debug_log_enabled
from pal.minion.git_env import cleanup_completed_plan_worktrees, prepare_dependency_integration_baseline
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
)
from pal.minion.turns import build_minion_turn_from_pack
from pal.minion.validation import normalize_milestones
from pal.shared import TaskContextPack
from pal.shared.text_search import jieba_fts_text


ACTIVE_WORK_ORDER_STATUSES = ("active", "running", "blocked", "approval_pending")
_CONTINUITY_LEDGER_LIMIT = 20
_CONTINUITY_TEXT_LIMIT = 500
_WORK_ORDER_DRAFT_TEXT_LIMIT = 4000
_WORK_ORDER_DRAFT_ITEM_TEXT_LIMIT = 1000
_WORK_ORDER_DRAFT_METADATA_VALUE_LIMIT = 12000
_RUN_WORKSPACE_KEYS = {"run_dir", "artifact_dir", "log_dir"}
_PROFILE_SCOPED_WORKSPACE_KEYS = {
    "checkpoint_policy",
    "workspace_policy",
    "workspace_environment",
    "workspace_environment_policy",
    "completion_policy",
    "gate_policy",
    "output_policy",
    "execution_strategy",
    "execution_policy",
}
_RAW_WORK_ORDER_METADATA_KEY_PARTS = ("payload", "raw", "transcript", "messages", "full_context", "conversation")
def _profile_ref_parts_from_canonical(value: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    if raw and "." in raw:
        group, name = raw.rsplit(".", 1)
        return group.strip() or "general", name.strip() or "generic"
    if raw:
        return "general", raw
    return "general", "generic"


def _canonical_profile_id(group: str, name: str) -> str:
    resolved_group = str(group or "general").strip().replace("/", ".") or "general"
    resolved_name = str(name or "generic").strip() or "generic"
    return resolved_name if resolved_group == "general" else f"{resolved_group}.{resolved_name}"


def _plan_module_kind(validation: dict[str, Any], module_id: str) -> str:
    wanted = str(module_id or "").strip()
    for item in list(dict(validation or {}).get("nodes") or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("module_id") or "").strip() == wanted:
            kind = str(item.get("kind") or "module").strip().lower()
            return kind if kind in {"prelude", "module", "join"} else "module"
    return "module"


def _module_dag_from_validation(validation: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    nodes = [dict(item) for item in list(dict(validation or {}).get("nodes") or []) if isinstance(item, dict)]
    node_to_module = {
        str(node.get("node_id") or "").strip(): str(node.get("module_id") or "").strip()
        for node in nodes
        if str(node.get("node_id") or "").strip() and str(node.get("module_id") or "").strip()
    }
    module_order = [
        str(node.get("module_id") or "").strip()
        for node in nodes
        if str(node.get("module_id") or "").strip()
    ]
    depends_on: dict[str, list[str]] = {}
    module_kind: dict[str, str] = {}
    for node in nodes:
        module_id = str(node.get("module_id") or "").strip()
        if not module_id:
            continue
        module_kind[module_id] = str(node.get("kind") or "module").strip().lower() or "module"
        deps: list[str] = []
        for dep_node_id in _coerce_text_list(node.get("depends_on")):
            dep_module_id = node_to_module.get(dep_node_id, dep_node_id)
            if dep_module_id and dep_module_id != module_id:
                deps.append(dep_module_id)
        depends_on[module_id] = _dedupe_text(deps)
    dependents: dict[str, list[str]] = {module_id: [] for module_id in module_order}
    for module_id, deps in depends_on.items():
        for dep in deps:
            dependents.setdefault(dep, [])
            if module_id not in dependents[dep]:
                dependents[dep].append(module_id)
    existing = dict(existing or {})
    existing_status = dict(existing.get("module_status") or {})
    existing_running = dict(existing.get("running_modules") or {})
    existing_outputs = dict(existing.get("module_outputs") or {})
    completed_modules = {
        module_id
        for module_id in _coerce_text_list(existing.get("completed_modules"))
        if module_id in module_order
    }
    for module_id, status in existing_status.items():
        if module_id in module_order and str(status or "").strip().lower() == "completed":
            completed_modules.add(module_id)
    module_status: dict[str, str] = {}
    remaining_indegree: dict[str, int] = {}
    for module_id in module_order:
        raw_status = str(existing_status.get(module_id) or "").strip().lower()
        if module_id in completed_modules:
            status = "completed"
        elif module_id in existing_running:
            status = "running"
        elif raw_status in {"ready", "blocked", "failed", "paused"}:
            status = raw_status
        else:
            status = ""
        remaining = len([dep for dep in depends_on.get(module_id, []) if dep not in completed_modules])
        if status in {"", "blocked"}:
            status = "ready" if remaining == 0 else "blocked"
        if status == "ready":
            remaining = 0
        if status == "completed":
            remaining = 0
        module_status[module_id] = status
        remaining_indegree[module_id] = int(remaining)
    ready_modules = [
        module_id
        for module_id in module_order
        if module_status.get(module_id) == "ready"
    ]
    running_modules = {
        module_id: str(child_id)
        for module_id, child_id in existing_running.items()
        if module_id in module_order and module_status.get(module_id) == "running" and str(child_id or "").strip()
    }
    return {
        "module_order": module_order,
        "module_kind": module_kind,
        "depends_on": depends_on,
        "dependents": dependents,
        "remaining_indegree": remaining_indegree,
        "module_status": module_status,
        "ready_modules": ready_modules,
        "running_modules": running_modules,
        "completed_modules": [module_id for module_id in module_order if module_id in completed_modules],
        "module_outputs": {
            module_id: dict(output)
            for module_id, output in existing_outputs.items()
            if module_id in module_order and isinstance(output, dict)
        },
    }


def _module_dag_status(dag: dict[str, Any]) -> str:
    statuses = dict(dag.get("module_status") or {})
    if statuses and all(str(status or "").strip().lower() == "completed" for status in statuses.values()):
        return "completed"
    if dict(dag.get("running_modules") or {}):
        return "running_module"
    if _coerce_text_list(dag.get("ready_modules")):
        return "awaiting_continue"
    return "blocked"


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

    def search_tasks(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        ...

    def search_work_orders(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        ...

    def search_work_order_drafts(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        ...

    def read_task(self, task_id: str) -> dict[str, Any]:
        ...

    def read_work_order(self, work_order_id: str, *, active_runs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
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
        return self.runtime_root / "pal.sqlite3"

    def ensure_schema(self) -> None:
        with self._connect() as db:
            ensure_minion_schema(self.runtime_root, db)

    def prepare_pack_for_spawn(self, pack: TaskContextPack) -> TaskContextPack:
        self.ensure_work_order_from_pack(pack)
        pack = self._hydrate_pack_from_work_order(pack)
        continuity = self.build_continuity(pack.work_order_id)
        metadata = dict(pack.metadata)
        metadata = _normalize_plan_backed_milestones(metadata)
        milestones = _plan_truth_milestones(metadata, pack.acceptance_criteria, pack.instruction or pack.goal)
        metadata = _normalize_milestone_execution_metadata(metadata, milestones)
        metadata.setdefault("task_id", continuity.get("task_id") or "")
        prompt_view = prompt_view_from_metadata(metadata, workspace=dict(pack.workspace))
        plan_execution = dict(metadata.get("plan_execution") or {})
        if (
            not prompt_view
            and str(plan_execution.get("mode") or "") != "module_parent_milestones"
            and isinstance(metadata.get("plan_artifact"), dict)
        ):
            prompt_view = _prompt_view_from_current_milestone(pack, continuity=continuity, metadata=metadata)
        if prompt_view:
            metadata["prompt_view"] = prompt_view
        metadata = _normalize_preferred_endpoint_metadata(metadata)
        return TaskContextPack.from_dict({**pack.to_dict(), "continuity": continuity, "metadata": metadata})

    def pack_for_work_order(self, work_order_id: str, *, overrides: dict[str, Any] | None = None) -> TaskContextPack:
        snapshot = self.read_work_order(str(work_order_id))
        if snapshot.get("status") != "ok":
            raise KeyError(f"unknown work order: {work_order_id}")
        pack = _pack_from_work_order_snapshot(snapshot)
        if overrides:
            pack = _merge_pack_overrides(pack, dict(overrides))
        return pack

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
        minion_profile: str = "software_engineering.coder",
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
        resolved_profile = str(minion_profile or "software_engineering.coder")
        profile_group, profile_name = _profile_ref_parts_from_canonical(resolved_profile)
        module_execution = {
            "mode": SERIAL_MODULE_MILESTONES_MODE,
            "plan_id": artifact.plan_id,
            "plan_revision": plan_revision,
            "module_id": resolved_module_id,
            "module_kind": module_kind,
            "module_name": str((metadata or {}).get("module_name") or resolved_module_id),
            "profile_role": _role_from_profile(resolved_profile),
            "uses_coder_contract": _profile_uses_coder_contract(resolved_profile),
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
        pack_metadata = dict(metadata or {})
        task_id = str(pack_metadata.get("task_id") or artifact.task_id).strip()
        project_name = str(pack_metadata.get("project_name") or _plan_project_name(artifact, workspace=resolved_workspace, task_id=task_id)).strip()
        module_name = str(pack_metadata.get("module_name") or resolved_module_id).strip()
        if project_name:
            resolved_workspace.setdefault("project_name", project_name)
        if module_name:
            resolved_workspace.setdefault("module_name", module_name)
        pack_metadata.update(
            {
                "task_id": task_id,
                "project_name": project_name,
                "task_title": str(pack_metadata.get("task_title") or artifact.summary or artifact.task_id),
                "work_order_title": str(pack_metadata.get("work_order_title") or f"{module_name} implementation"),
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
        if _profile_uses_coder_contract(resolved_profile):
            order = compile_coder_work_order(
                artifact,
                module_id=resolved_module_id,
                milestone_id=milestone_id,
                work_order_id=resolved_work_order_id,
                allowed_capabilities=list(allowed_capabilities or []),
                workspace=resolved_workspace,
            )
            order_payload = order.to_dict()
            dependency_outputs = [
                dict(item)
                for item in list(pack_metadata.get("module_dependency_outputs") or resolved_workspace.get("module_dependency_outputs") or [])
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
            if module_kind == "join":
                output_contract = dict(order_payload.get("output_contract") or {})
                output_contract["verification_only_no_change_allowed"] = True
                output_contract["artifact_required_when_no_change"] = True
                output_contract["no_change_completion"] = (
                    "If the join module only verifies already-integrated module outputs and no source/test/doc/config "
                    "changes are required, produce a verification artifact/report instead of creating an empty checkpoint commit."
                )
                order_payload["output_contract"] = output_contract
            pack_metadata["coder_work_order"] = order_payload
        else:
            pack_metadata["prompt_view"] = _plan_milestone_prompt_view(
                artifact,
                module_id=resolved_module_id,
                milestone_id=milestone_id,
                work_order_id=resolved_work_order_id,
                role=_role_from_profile(resolved_profile),
                allowed_capabilities=list(allowed_capabilities or []),
                workspace=resolved_workspace,
            )
        return TaskContextPack.from_dict(
            {
                "work_order_id": resolved_work_order_id,
                "goal": str(goal or artifact.summary or f"Implement module {resolved_module_id}"),
                "instruction": str(
                    instruction
                    or (
                        f"Implement module {resolved_module_id} according to the structured coder work order."
                        if _profile_uses_coder_contract(resolved_profile)
                        else f"Complete module {resolved_module_id} one plan milestone at a time."
                    )
                ),
                "acceptance_criteria": [item for milestone in milestones for item in _coerce_text_list(milestone.get("acceptance"))],
                "workspace": resolved_workspace,
                "profile_group": profile_group,
                "profile_name": profile_name,
                "minion_profile": resolved_profile,
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
        dispatch_profile_group = str(profile_group or pack_metadata.get("dispatch_profile_group") or "software_engineering").strip() or "software_engineering"
        dispatch_profile_name = str(profile_name or pack_metadata.get("dispatch_profile_name") or "coder").strip() or "coder"
        plan_execution = dict(pack_metadata.get("plan_execution") or {})
        module_dag = _module_dag_from_validation(validation, dict(plan_execution.get("module_dag") or {}))
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
                "auto_advance_modules": bool(plan_execution.get("auto_advance_modules", True)),
                "child_work_order_ids": dict(plan_execution.get("child_work_order_ids") or {}),
                "active_child_work_order_ids": _coerce_text_list(plan_execution.get("active_child_work_order_ids")),
                "module_dag": module_dag,
            }
        )
        pack_metadata.update(
            {
                "task_id": task_id,
                "project_name": project_name,
                "task_title": str(pack_metadata.get("task_title") or artifact.summary or artifact.task_id),
                "work_order_title": str(pack_metadata.get("work_order_title") or artifact.summary or "Plan implementation"),
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
        loaded_plan = self.load_dispatchable_plan_ref(source_plan_ref)
        artifact = validate_dispatchable_plan_artifact(loaded_plan.get("plan_artifact") or {})
        source_revision = _plan_revision_from_payload(loaded_plan.get("plan_artifact"), loaded_plan.get("plan_ref"))
        next_revision = source_revision + 1
        revision_checklist = _plan_revision_checklist(gate_payload)
        resolved_work_order_id = str(work_order_id or new_work_id("wo")).strip()
        resolved_workspace = dict(workspace or {})
        for key in ("repo_path", "source_repo", "artifact_dir"):
            value = str(target.get(key) or "").strip()
            if value:
                resolved_workspace.setdefault(key, value)
        planner_goal = str(goal or f"Revise architecture plan {artifact.plan_id} from reviewer gate {gate_payload.get('gate_id') or ''}").strip()
        planner_instruction = str(
            instruction
            or (
                "Revise the referenced submitted plan draft only. First call plan_checkout with {} so Pal uses the "
                "workspace-bound source_plan_ref; do not construct or guess plan_ref/review_gate_ref objects. Then use "
                "plan_read/plan_find/plan_get to locate reviewer target handles and repair locally with plan_update_*, "
                "plan_delete_*, plan_move_milestone, or plan_replace_milestone_acceptance_criteria. "
                "Follow revision_source.plan_revision_checklist in order before broad exploration; use any target_handle, "
                "target_node, related_handles, and suggested_tool_route already supplied there. "
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
                "profile_name": "architect",
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
        loaded_plan = self.load_dispatchable_plan_ref(plan_ref)
        artifact = validate_dispatchable_plan_artifact(loaded_plan.get("plan_artifact") or {})
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
                "workspace": dict(workspace or {}),
                "profile_group": "software_engineering",
                "profile_name": "architect",
                "metadata": pack_metadata,
            }
        )

    def load_dispatchable_plan_ref(self, plan_ref: Any) -> dict[str, Any]:
        return self.plans.load_dispatchable_plan_ref(plan_ref)

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
    ) -> dict[str, Any]:
        return self.plans.submit_plan_ref(plan_artifact, submission_notes=submission_notes)

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
        status = str(plan_execution.get("status") or "").strip().lower()
        if status == "completed":
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
        dag = _module_dag_from_validation(validation, dict(plan_execution.get("module_dag") or {}))
        ready_modules = [
            module_id
            for module_id in _coerce_text_list(dag.get("module_order")) or module_order
            if str(dict(dag.get("module_status") or {}).get(module_id) or "") == "ready"
        ]
        if not ready_modules:
            plan_execution["module_dag"] = dag
            resolved_status = _module_dag_status(dag)
            plan_execution["status"] = resolved_status
            plan_execution["active_child_work_order_ids"] = list(dict(dag.get("running_modules") or {}).values())
            metadata["plan_execution"] = plan_execution
            with self._connect() as db:
                db.execute(
                    "UPDATE minion_work_orders SET metadata_json = ?, updated_at = ? WHERE work_order_id = ?",
                    (_json(metadata), utc_now(), str(work_order_id)),
                )
                if resolved_status == "completed":
                    self._update_work_order_status(db, str(work_order_id), "completed")
            return []
        selected_modules = ready_modules[:requested_limit]
        child_ids = dict(plan_execution.get("child_work_order_ids") or {})
        task_id = str(work_order.get("task_id") or metadata.get("task_id") or artifact.task_id)
        dispatch_profile_group = str(metadata.get("dispatch_profile_group") or "software_engineering").strip() or "software_engineering"
        dispatch_profile_name = str(metadata.get("dispatch_profile_name") or "coder").strip() or "coder"
        packs: list[TaskContextPack] = []
        running_modules = dict(dag.get("running_modules") or {})
        module_status = dict(dag.get("module_status") or {})
        for module_id in selected_modules:
            module_name = module_id
            milestone_index = module_order.index(module_id) if module_id in module_order else 0
            child_work_order_id = str(child_ids.get(module_id) or "").strip()
            if not child_work_order_id:
                child_work_order_id = f"wo_{_safe_id(str(work_order_id))}_{_safe_id(module_id)}"
                child_ids[module_id] = child_work_order_id
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
            child_metadata = {
                "task_id": _safe_id(task_id),
                "project_name": project_name,
                "task_title": str(metadata.get("task_title") or artifact.summary or task_id),
                "work_order_title": f"{module_name} implementation",
                "parent_work_order_id": str(work_order_id),
                "parent_milestone_index": int(milestone_index),
                "parent_module_id": module_id,
                "parent_module_name": module_name,
                "module_name": module_name,
                "module_dependency_outputs": dependency_outputs,
            }
            if isinstance(module_workspace.get("dependency_integration_baseline"), dict):
                child_metadata["module_dependency_integration"] = dict(module_workspace.get("dependency_integration_baseline") or {})
            child_metadata["dispatch_profile_group"] = dispatch_profile_group
            child_metadata["dispatch_profile_name"] = dispatch_profile_name
            if isinstance(metadata.get("plan_ref"), dict):
                child_metadata["plan_ref"] = dict(metadata.get("plan_ref") or {})
            if isinstance(metadata.get("plan_validation"), dict):
                child_metadata["plan_validation"] = dict(metadata.get("plan_validation") or {})
            for key in ("control_route", "preferred_endpoint_id", "preferred_endpoint_source", "minion_debug_log_enabled", "debug_log"):
                if key in metadata:
                    child_metadata[key] = metadata[key]
            packs.append(
                self.build_coder_module_pack_from_plan(
                    artifact,
                    module_id=module_id,
                    work_order_id=child_work_order_id,
                    workspace=module_workspace,
                    metadata=child_metadata,
                    goal=f"Implement module {module_id}",
                    instruction=f"Implement module {module_id}; this is parent work-order milestone {milestone_index}.",
                    minion_profile=_canonical_profile_id(dispatch_profile_group, dispatch_profile_name),
                )
            )
            module_status[module_id] = "running"
            running_modules[module_id] = child_work_order_id
        dag["module_status"] = module_status
        dag["running_modules"] = running_modules
        dag["ready_modules"] = [
            module_id
            for module_id in ready_modules
            if module_id not in set(selected_modules)
        ]
        plan_execution["child_work_order_ids"] = child_ids
        first_module_id = selected_modules[0]
        first_child_id = str(child_ids.get(first_module_id) or "")
        first_index = module_order.index(first_module_id) if first_module_id in module_order else 0
        plan_execution["current_module_index"] = int(first_index)
        plan_execution["current_module_id"] = first_module_id
        plan_execution["active_child_work_order_id"] = first_child_id
        plan_execution["active_child_work_order_ids"] = list(running_modules.values())
        plan_execution["module_dag"] = dag
        plan_execution["status"] = "running_module"
        metadata["plan_execution"] = plan_execution
        with self._connect() as db:
            db.execute(
                "UPDATE minion_work_orders SET metadata_json = ?, updated_at = ? WHERE work_order_id = ?",
                (_json(metadata), utc_now(), str(work_order_id)),
            )
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
        module_id = str(child_metadata.get("parent_module_id") or completion.get("module_id") or "").strip()
        plan_payload = parent_metadata.get("plan_artifact")
        if not isinstance(plan_payload, dict):
            return {"status": "skipped", "reason": "parent_missing_plan_artifact", "parent_work_order_id": parent_work_order_id}
        artifact = PlanArtifact.from_dict(plan_payload)
        validation = dispatchable_plan_validation(artifact)
        dag = _module_dag_from_validation(validation, dict(plan_execution.get("module_dag") or {}))
        summary = str(completion.get("summary") or f"Module {module_id} completed.").strip()
        created_at = utc_now()
        with self._connect() as db:
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
            module_order = _coerce_text_list(plan_execution.get("module_order"))
            if not module_order:
                module_order = _coerce_text_list(dag.get("module_order"))
            completed_modules = _coerce_text_list(plan_execution.get("completed_modules"))
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
            module_status = dict(dag.get("module_status") or {})
            running_modules = dict(dag.get("running_modules") or {})
            module_outputs = dict(dag.get("module_outputs") or {})
            was_completed = str(module_status.get(module_id) or "").strip().lower() == "completed"
            if not was_completed:
                completed_modules = _dedupe_text([*completed_modules, module_id])
                module_status[module_id] = "completed"
                running_modules.pop(module_id, None)
                if child_output:
                    module_outputs[module_id] = child_output
                completed_set = set(completed_modules)
                remaining_indegree = dict(dag.get("remaining_indegree") or {})
                for dependent in _coerce_text_list(dict(dag.get("dependents") or {}).get(module_id)):
                    if str(module_status.get(dependent) or "").strip().lower() == "completed":
                        remaining_indegree[dependent] = 0
                        continue
                    remaining = len(
                        [
                            dep
                            for dep in _coerce_text_list(dict(dag.get("depends_on") or {}).get(dependent))
                            if dep not in completed_set
                        ]
                    )
                    remaining_indegree[dependent] = int(remaining)
                    if remaining == 0 and str(module_status.get(dependent) or "").strip().lower() in {"", "blocked"}:
                        module_status[dependent] = "ready"
                dag["remaining_indegree"] = remaining_indegree
            dag["module_status"] = module_status
            dag["running_modules"] = running_modules
            dag["completed_modules"] = [item for item in module_order if item in set(completed_modules)]
            dag["module_outputs"] = {
                key: dict(value)
                for key, value in module_outputs.items()
                if isinstance(value, dict)
            }
            ready_modules = [
                item
                for item in module_order
                if str(module_status.get(item) or "").strip().lower() == "ready"
            ]
            dag["ready_modules"] = ready_modules
            plan_execution["module_dag"] = dag
            plan_execution["completed_modules"] = list(dag["completed_modules"])
            active_child_ids = [str(value) for value in running_modules.values() if str(value or "").strip()]
            plan_execution["active_child_work_order_ids"] = active_child_ids
            if len(active_child_ids) == 1:
                plan_execution["active_child_work_order_id"] = active_child_ids[0]
            else:
                plan_execution.pop("active_child_work_order_id", None)
            resolved_status = _module_dag_status(dag)
            plan_execution["status"] = resolved_status
            next_module_id = ready_modules[0] if ready_modules else ""
            next_index = module_order.index(next_module_id) if next_module_id in module_order else None
            if next_module_id:
                plan_execution["next_module_id"] = next_module_id
                plan_execution["current_module_index"] = int(next_index or 0)
            else:
                plan_execution.pop("next_module_id", None)
            if resolved_status == "completed":
                parent_status = "completed"
                cleanup = cleanup_completed_plan_worktrees(
                    child_workspace,
                    module_outputs=[dict(value) for value in dict(dag.get("module_outputs") or {}).values() if isinstance(value, dict)],
                    keep_repo_path=str(child_workspace.get("repo_path") or ""),
                )
                parent_metadata["workspace_cleanup"] = cleanup
                plan_execution["workspace_cleanup"] = cleanup
            else:
                parent_status = "active"
            parent_metadata["plan_execution"] = plan_execution
            db.execute(
                "UPDATE minion_work_orders SET metadata_json = ?, updated_at = ? WHERE work_order_id = ?",
                (_json(parent_metadata), utc_now(), parent_work_order_id),
            )
            self._update_work_order_status(db, parent_work_order_id, parent_status)
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
            "ready_module_ids": list((plan_execution.get("module_dag") or {}).get("ready_modules") or []),
            "auto_advance_modules": bool(plan_execution.get("auto_advance_modules", True)),
            "summary": summary,
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
            ready_modules = _coerce_text_list(dict(plan_execution.get("module_dag") or {}).get("ready_modules"))
            if ready_modules:
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
        plan_execution["status"] = normalized
        if reason:
            plan_execution["status_reason"] = reason
        metadata["plan_execution"] = plan_execution
        work_order_status = "completed" if normalized == "completed" else "active"
        with self._connect() as db:
            db.execute(
                "UPDATE minion_work_orders SET metadata_json = ?, updated_at = ? WHERE work_order_id = ?",
                (_json(metadata), utc_now(), str(work_order_id)),
            )
            self._update_work_order_status(db, str(work_order_id), work_order_status)
        return {"status": normalized, "work_order_id": str(work_order_id), "reason": reason}

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
        profile = str(work_order.get("minion_profile") or metadata.get("minion_profile") or "generic")
        module_execution["current_milestone_index"] = int(milestone_index)
        module_execution["profile_role"] = _role_from_profile(profile)
        module_execution["uses_coder_contract"] = _profile_uses_coder_contract(profile)
        module_execution["status"] = "active"
        module_execution.setdefault("module_name", str(metadata.get("module_name") or module_id).strip())
        metadata["module_execution"] = module_execution
        metadata.pop("prompt_view", None)
        if _profile_uses_coder_contract(profile):
            order = compile_coder_work_order(
                artifact,
                module_id=module_id,
                milestone_id=milestone_id,
                work_order_id=str(work_order_id),
                allowed_capabilities=_coerce_text_list(metadata.get("coder_allowed_capabilities")),
                workspace=workspace,
            )
            metadata["coder_work_order"] = order.to_dict()
        else:
            metadata.pop("coder_work_order", None)
            metadata["prompt_view"] = _plan_milestone_prompt_view(
                artifact,
                module_id=module_id,
                milestone_id=milestone_id,
                work_order_id=str(work_order_id),
                role=_role_from_profile(profile),
                allowed_capabilities=_coerce_text_list(metadata.get("allowed_capabilities")),
                workspace=workspace,
            )
        return TaskContextPack.from_dict(
            {
                "work_order_id": str(work_order_id),
                "goal": str(work_order.get("goal") or ""),
                "instruction": str(work_order.get("instruction") or work_order.get("goal") or ""),
                "acceptance_criteria": _coerce_text_list(metadata.get("acceptance_criteria")),
                "workspace": workspace,
                "artifacts": _artifact_list(metadata.get("artifacts")),
                "minion_profile": str(work_order.get("minion_profile") or "software_engineering.coder"),
                "metadata": metadata,
            }
        )

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
                db.execute(
                    """
                    INSERT INTO minion_tasks(task_id, title, goal, summary, status, metadata_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        title[:160],
                        pack.goal or pack.instruction,
                        str(metadata.get("task_summary") or ""),
                        "active",
                        _json(metadata.get("task_metadata") or {}),
                        now,
                        now,
                    ),
                )
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

    def merge_work_order_metadata(self, work_order_id: str, updates: dict[str, Any]) -> dict[str, Any]:
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
        minion_profile = str(payload.get("minion_profile") or "software_engineering.architect").strip() or "software_engineering.architect"
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
                "minion_profile": "software_engineering.architect",
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
        return {
            "task_id": str((snapshot.get("task") or {}).get("task_id") or ""),
            "work_order_id": work_order_id,
            "current_milestone": _compact_milestone_for_continuity(snapshot.get("current_milestone") or {}),
            "completed_milestones": [
                _compact_milestone_for_continuity(item)
                for item in snapshot.get("milestones", [])
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

    def search_work_orders(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as db:
            items = self.search.search_fts(
                db,
                table_name="minion_work_orders_fts",
                id_column="work_order_id",
                query=query,
                limit=limit,
                fallback_sql="""
                    SELECT work_order_id, 1.0 AS score FROM minion_work_orders
                    WHERE lower(title || ' ' || goal || ' ' || instruction) LIKE ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                """,
            )
            return {
                "items": [
                    _compact_work_order_search_item(self.read_work_order(item["id"])) | {"score": item["score"]}
                    for item in items
                ],
                "count": len(items),
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

    def read_task(self, task_id: str) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as db:
            task = self._fetch_one(db, "SELECT * FROM minion_tasks WHERE task_id = ?", (str(task_id),))
            if task is None:
                return {"status": "not_found", "task_id": str(task_id)}
            work_orders = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM minion_work_orders WHERE task_id = ? ORDER BY created_at DESC",
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
            return {
                "status": "ok",
                "task": _decode_json_fields(dict(task), keep_json=False) if task else {},
                "work_order": _decode_json_fields(dict(work_order), keep_json=False),
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
                "current_worker": _current_worker_for_work_order(str(work_order_id), active_runs or []),
                "task_lessons": task_lessons,
                "pending_system_lesson_candidates": system_candidates,
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
        child_id = str(child_work_order_id or "").strip()
        normalized_child_status = str(child_terminal_status or "failed").strip().lower()
        if normalized_child_status not in {"failed", "killed"}:
            normalized_child_status = "failed"
        now = utc_now()
        child_terminal_recorded = False
        if child_id:
            child = self._fetch_one(db, "SELECT status FROM minion_work_orders WHERE work_order_id = ?", (child_id,))
            child_status = str(dict(child)["status"] if child is not None else "").strip().lower()
            if child is not None and child_status not in {"completed", "failed", "blocked", "killed"}:
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
        released_module_id = next((module_id for module_id, value in running_children.items() if value == child_id), "")
        dag = dict(plan_execution.get("module_dag") or {})
        if released_module_id and dag:
            module_status = dict(dag.get("module_status") or {})
            running_modules = dict(dag.get("running_modules") or {})
            running_modules.pop(released_module_id, None)
            if str(module_status.get(released_module_id) or "").strip().lower() == "running":
                module_status[released_module_id] = "ready"
            dag["module_status"] = module_status
            dag["running_modules"] = running_modules
            ready_modules = _coerce_text_list(dag.get("ready_modules"))
            if released_module_id and released_module_id not in ready_modules:
                ready_modules.append(released_module_id)
            module_order = _coerce_text_list(dag.get("module_order"))
            dag["ready_modules"] = [module_id for module_id in module_order if module_id in set(ready_modules)] or ready_modules
            plan_execution["module_dag"] = dag
        resolved_status = _module_dag_status(dict(plan_execution.get("module_dag") or {}))
        plan_execution["status"] = resolved_status
        plan_execution["status_reason"] = reason
        if child_id:
            plan_execution["last_released_child_work_order_id"] = child_id
        active_child_ids = [
            value
            for value in _coerce_text_list(dict(dict(plan_execution.get("module_dag") or {}).get("running_modules") or {}).values())
            if value != child_id
        ]
        plan_execution["active_child_work_order_ids"] = active_child_ids
        if len(active_child_ids) == 1:
            plan_execution["active_child_work_order_id"] = active_child_ids[0]
        else:
            plan_execution.pop("active_child_work_order_id", None)
        parent_metadata["plan_execution"] = plan_execution
        db.execute(
            "UPDATE minion_work_orders SET metadata_json = ?, updated_at = ? WHERE work_order_id = ?",
            (_json(parent_metadata), now, parent_id),
        )
        self._update_work_order_status(db, parent_id, "active")
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
        self.ledger.insert_ledger(db, parent_id, "module_recovered", str(parent_payload["summary"]), parent_payload, "", "", now)
        return {
            "parent_work_order_id": parent_id,
            "child_work_order_id": child_id,
            "module_id": released_module_id,
            "status": resolved_status,
            "child_terminal_status": normalized_child_status,
            "child_terminal_recorded": child_terminal_recorded,
            "reason": reason,
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

    def _update_work_order_status(self, db: sqlite3.Connection, work_order_id: str, status: str) -> None:
        ended_at = utc_now() if status in {"completed", "failed", "blocked", "killed"} else ""
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


def _profile_uses_coder_contract(profile: str) -> bool:
    normalized = str(profile or "").replace("/", ".").strip().lower()
    return normalized == "software_engineering.coder" or normalized.endswith(".coder")


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
    for run in active_runs:
        if str(run.get("work_order_id") or "") != work_order_id:
            continue
        if str(run.get("status") or "") in {"starting", "running", "approval_pending"}:
            return dict(run)
    return {}


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
    dag = dict(plan_execution.get("module_dag") or {})
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
            "severity": _first_text(raw, "severity", fallback="required"),
            "action": _compact_text(action, limit=700),
        }
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
