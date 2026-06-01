from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from pal.foundation import utc_now
from pal.minion.work_order import (
    PlanArtifact,
    compile_coder_work_order,
    module_milestone_records,
    new_work_id,
    plan_milestone_id_at,
    plan_module_id_at,
    prompt_view_from_metadata,
    validate_final_plan_artifact,
)
from pal.minion.validation import normalize_milestones
from pal.shared import TaskContextPack
from pal.shared.text_search import compile_jieba_fts_queries, jieba_fts_text


ACTIVE_WORK_ORDER_STATUSES = ("active", "running", "blocked", "approval_pending")
_CONTINUITY_LEDGER_LIMIT = 20
_CONTINUITY_TEXT_LIMIT = 500
_WORK_ORDER_DRAFT_TEXT_LIMIT = 4000
_WORK_ORDER_DRAFT_ITEM_TEXT_LIMIT = 1000
_WORK_ORDER_DRAFT_METADATA_VALUE_LIMIT = 12000
_RUN_WORKSPACE_KEYS = {"run_dir", "artifact_dir", "log_dir"}
_RAW_WORK_ORDER_METADATA_KEY_PARTS = ("payload", "raw", "transcript", "messages", "full_context", "conversation")


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

    @property
    def db_path(self) -> Path:
        return self.runtime_root / "pal.sqlite3"

    def ensure_schema(self) -> None:
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS minion_tasks (
                    task_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    goal TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS minion_work_orders (
                    work_order_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    goal TEXT NOT NULL DEFAULT '',
                    instruction TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    minion_profile TEXT NOT NULL DEFAULT 'generic',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS minion_work_order_drafts (
                    draft_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    goal TEXT NOT NULL DEFAULT '',
                    source_summary TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'draft',
                    minion_profile TEXT NOT NULL DEFAULT 'software_engineering.planner',
                    task_id TEXT NOT NULL DEFAULT '',
                    proposed_work_order_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS minion_one_active_work_order
                ON minion_work_orders(task_id)
                WHERE status IN ('active', 'running', 'blocked', 'approval_pending');

                CREATE TABLE IF NOT EXISTS minion_work_order_milestones (
                    milestone_id TEXT PRIMARY KEY,
                    work_order_id TEXT NOT NULL,
                    milestone_index INTEGER NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    acceptance_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    UNIQUE(work_order_id, milestone_index)
                );

                CREATE TABLE IF NOT EXISTS minion_worker_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    work_order_id TEXT NOT NULL,
                    milestone_index INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    minion_id TEXT NOT NULL DEFAULT '',
                    run_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS minion_worker_ledger (
                    ledger_id TEXT PRIMARY KEY,
                    work_order_id TEXT NOT NULL,
                    event_kind TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    minion_id TEXT NOT NULL DEFAULT '',
                    run_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS minion_task_lessons (
                    lesson_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    work_order_id TEXT NOT NULL,
                    lesson_text TEXT NOT NULL,
                    minion_id TEXT NOT NULL DEFAULT '',
                    run_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS minion_system_lesson_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    work_order_id TEXT NOT NULL,
                    lesson_text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    minion_id TEXT NOT NULL DEFAULT '',
                    run_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS minion_tasks_fts USING fts5(
                    task_id UNINDEXED,
                    title,
                    goal,
                    summary
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS minion_work_orders_fts USING fts5(
                    work_order_id UNINDEXED,
                    task_id UNINDEXED,
                    title,
                    goal,
                    instruction
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS minion_work_order_drafts_fts USING fts5(
                    draft_id UNINDEXED,
                    title,
                    goal,
                    source_summary,
                    payload_text
                );
                """
            )

    def prepare_pack_for_spawn(self, pack: TaskContextPack) -> TaskContextPack:
        self.ensure_work_order_from_pack(pack)
        pack = self._hydrate_pack_from_work_order(pack)
        continuity = self.build_continuity(pack.work_order_id)
        metadata = dict(pack.metadata)
        metadata.setdefault("task_id", continuity.get("task_id") or "")
        prompt_view = prompt_view_from_metadata(metadata, workspace=dict(pack.workspace))
        if prompt_view:
            metadata["prompt_view"] = prompt_view
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
        artifact = validate_final_plan_artifact(plan_payload)
        resolved_module_id = plan_module_id_at(artifact, module_id=module_id)
        resolved_work_order_id = str(work_order_id or new_work_id("wo")).strip()
        milestone_id = plan_milestone_id_at(artifact, module_id=resolved_module_id, milestone_index=0)
        resolved_workspace = dict(workspace or {})
        order = compile_coder_work_order(
            artifact,
            module_id=resolved_module_id,
            milestone_id=milestone_id,
            work_order_id=resolved_work_order_id,
            allowed_capabilities=list(allowed_capabilities or []),
            workspace=resolved_workspace,
        )
        milestones = module_milestone_records(artifact, module_id=resolved_module_id)
        module_execution = {
            "mode": "serial_module_milestones",
            "plan_id": artifact.plan_id,
            "module_id": resolved_module_id,
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
        pack_metadata.update(
            {
                "task_id": task_id,
                "task_title": str(pack_metadata.get("task_title") or artifact.summary or artifact.task_id),
                "work_order_title": str(pack_metadata.get("work_order_title") or f"{resolved_module_id} implementation"),
                "plan_artifact": {"type": "FinalPlanArtifact", **artifact.to_dict()},
                "module_id": resolved_module_id,
                "module_execution": module_execution,
                "coder_work_order": order.to_dict(),
                "milestones": milestones,
            }
        )
        pack_metadata.pop("prompt_view", None)
        return TaskContextPack.from_dict(
            {
                "work_order_id": resolved_work_order_id,
                "goal": str(goal or artifact.summary or f"Implement module {resolved_module_id}"),
                "instruction": str(
                    instruction
                    or f"Implement module {resolved_module_id} according to the structured coder work order."
                ),
                "acceptance_criteria": [item for milestone in milestones for item in _coerce_text_list(milestone.get("acceptance"))],
                "workspace": resolved_workspace,
                "minion_profile": str(minion_profile or "software_engineering.coder"),
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
    ) -> TaskContextPack:
        artifact = validate_final_plan_artifact(plan_payload)
        resolved_work_order_id = str(work_order_id or new_work_id("wo")).strip()
        module_order = [module.module_id for module in artifact.modules if module.module_id]
        milestones = _module_parent_milestones(artifact)
        pack_metadata = dict(metadata or {})
        plan_execution = dict(pack_metadata.get("plan_execution") or {})
        plan_execution.update(
            {
                "mode": "module_parent_milestones",
                "plan_id": artifact.plan_id,
                "module_order": module_order,
                "current_module_index": int(plan_execution.get("current_module_index") or 0),
                "status": str(plan_execution.get("status") or "active"),
                "child_work_order_ids": dict(plan_execution.get("child_work_order_ids") or {}),
            }
        )
        pack_metadata.update(
            {
                "task_id": artifact.task_id,
                "task_title": str(pack_metadata.get("task_title") or artifact.summary or artifact.task_id),
                "work_order_title": str(pack_metadata.get("work_order_title") or artifact.summary or "Plan implementation"),
                "plan_artifact": {"type": "FinalPlanArtifact", **artifact.to_dict()},
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
                "workspace": dict(workspace or {}),
                "minion_profile": "software_engineering.coder",
                "metadata": pack_metadata,
            }
        )

    def next_plan_module_pack(self, work_order_id: str, *, allow_paused: bool = False) -> TaskContextPack | None:
        snapshot = self.read_work_order(str(work_order_id))
        if snapshot.get("status") != "ok":
            return None
        work_order = dict(snapshot.get("work_order") or {})
        metadata = _loads_or_dict(work_order.get("metadata"))
        plan_execution = dict(metadata.get("plan_execution") or {})
        if str(plan_execution.get("mode") or "") != "module_parent_milestones":
            return None
        status = str(plan_execution.get("status") or "").strip().lower()
        if status == "completed":
            return None
        if status == "paused" and not allow_paused:
            return None
        current_milestone = dict(snapshot.get("current_milestone") or {})
        if not current_milestone:
            self.set_plan_parent_status(str(work_order_id), "completed")
            return None
        milestone_index = _coerce_int(current_milestone.get("milestone_index"))
        if milestone_index is None:
            return None
        plan_payload = metadata.get("plan_artifact")
        if not isinstance(plan_payload, dict):
            return None
        artifact = PlanArtifact.from_dict(plan_payload)
        module_order = _coerce_text_list(plan_execution.get("module_order"))
        if not module_order:
            module_order = [module.module_id for module in artifact.modules if module.module_id]
        if milestone_index >= len(module_order):
            return None
        module_id = module_order[int(milestone_index)]
        child_ids = dict(plan_execution.get("child_work_order_ids") or {})
        child_work_order_id = str(child_ids.get(module_id) or "").strip()
        if not child_work_order_id:
            child_work_order_id = f"wo_{_safe_id(str(work_order_id))}_{_safe_id(module_id)}"
            child_ids[module_id] = child_work_order_id
        task_id = str(work_order.get("task_id") or metadata.get("task_id") or artifact.task_id)
        child_metadata = {
            "task_id": f"{_safe_id(task_id)}_{_safe_id(module_id)}",
            "task_title": str(metadata.get("task_title") or artifact.summary or task_id),
            "work_order_title": f"{module_id} implementation",
            "parent_work_order_id": str(work_order_id),
            "parent_milestone_index": int(milestone_index),
            "parent_module_id": module_id,
        }
        for key in ("control_route", "preferred_endpoint_id", "minion_debug_log_enabled", "debug_log"):
            if key in metadata:
                child_metadata[key] = metadata[key]
        plan_execution["child_work_order_ids"] = child_ids
        plan_execution["current_module_index"] = int(milestone_index)
        plan_execution["current_module_id"] = module_id
        plan_execution["active_child_work_order_id"] = child_work_order_id
        plan_execution["status"] = "running_module"
        metadata["plan_execution"] = plan_execution
        with self._connect() as db:
            db.execute(
                "UPDATE minion_work_orders SET metadata_json = ?, updated_at = ? WHERE work_order_id = ?",
                (_json(metadata), utc_now(), str(work_order_id)),
            )
        return self.build_coder_module_pack_from_plan(
            artifact,
            module_id=module_id,
            work_order_id=child_work_order_id,
            workspace=_loads_or_dict(metadata.get("workspace")),
            metadata=child_metadata,
            goal=f"Implement module {module_id}",
            instruction=f"Implement module {module_id}; this is parent work-order milestone {milestone_index}.",
            minion_profile="software_engineering.coder",
        )

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
                self._insert_checkpoint(db, parent_work_order_id, checkpoint_payload, "", "", created_at)
                self._insert_ledger(db, parent_work_order_id, "module_checkpoint", summary, checkpoint_payload, "", "", created_at)
            rows = db.execute(
                "SELECT milestone_index FROM minion_work_order_milestones WHERE work_order_id = ? ORDER BY milestone_index",
                (parent_work_order_id,),
            ).fetchall()
            completed = {
                int(row["milestone_index"])
                for row in db.execute(
                    """
                    SELECT milestone_index FROM minion_worker_checkpoints
                    WHERE work_order_id = ? AND status = 'completed'
                    """,
                    (parent_work_order_id,),
                ).fetchall()
            }
            next_index = next((int(row["milestone_index"]) for row in rows if int(row["milestone_index"]) not in completed), None)
            module_order = _coerce_text_list(plan_execution.get("module_order"))
            completed_modules = _coerce_text_list(plan_execution.get("completed_modules"))
            completed_modules = _dedupe_text([*completed_modules, module_id])
            plan_execution["completed_modules"] = completed_modules
            plan_execution.pop("active_child_work_order_id", None)
            if next_index is None:
                plan_execution["status"] = "completed"
                parent_status = "completed"
                next_module_id = ""
            else:
                plan_execution["status"] = "awaiting_continue"
                plan_execution["current_module_index"] = int(next_index)
                next_module_id = module_order[next_index] if next_index < len(module_order) else ""
                plan_execution["next_module_id"] = next_module_id
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
            "has_next_module": bool(next_index is not None),
            "next_module_id": next_module_id,
            "summary": summary,
            "metadata": (
                {"control_route": dict(parent_metadata.get("control_route") or {})}
                if isinstance(parent_metadata.get("control_route"), dict)
                else {}
            ),
        }

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
        if str(module_execution.get("mode") or "") != "serial_module_milestones":
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
            return None
        artifact = PlanArtifact.from_dict(plan_payload)
        module_id = str(module_execution.get("module_id") or metadata.get("module_id") or "").strip()
        if not module_id:
            module_id = plan_module_id_at(artifact)
        milestone_id = plan_milestone_id_at(artifact, module_id=module_id, milestone_index=milestone_index)
        workspace = _loads_or_dict(metadata.get("workspace"))
        order = compile_coder_work_order(
            artifact,
            module_id=module_id,
            milestone_id=milestone_id,
            work_order_id=str(work_order_id),
            allowed_capabilities=_coerce_text_list(metadata.get("coder_allowed_capabilities")),
            workspace=workspace,
        )
        module_execution["current_milestone_index"] = int(milestone_index)
        module_execution["status"] = "active"
        metadata["module_execution"] = module_execution
        metadata["coder_work_order"] = order.to_dict()
        metadata.pop("prompt_view", None)
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
        if str(module_execution.get("mode") or "") != "serial_module_milestones":
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
        module_id = str(module_execution.get("module_id") or metadata.get("module_id") or "").strip()
        completed_count = len([item for item in list(snapshot.get("milestones") or []) if item.get("completed")])
        return {
            "status": "completed",
            "work_order_id": str(work_order_id),
            "task_id": str(task.get("task_id") or work_order.get("task_id") or ""),
            "module_id": module_id,
            "summary": f"Module {module_id or work_order_id} completed {completed_count} milestone(s).",
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
        milestones = _coerce_milestones(metadata.get("milestones"), pack.acceptance_criteria, pack.instruction or pack.goal)
        with self._connect() as db:
            existing = self._fetch_one(db, "SELECT * FROM minion_work_orders WHERE work_order_id = ?", (pack.work_order_id,))
            if existing is not None:
                stored_metadata = _loads(existing["metadata_json"])
                stored_metadata.update(metadata)
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
                db.execute(
                    """
                    UPDATE minion_work_orders
                    SET title = ?, goal = ?, instruction = ?, minion_profile = ?, metadata_json = ?, updated_at = ?
                    WHERE work_order_id = ?
                    """,
                    (
                        next_title[:160],
                        next_goal,
                        next_instruction,
                        next_profile,
                        _json(stored_metadata),
                        now,
                        pack.work_order_id,
                    ),
                )
                task_id = str(existing["task_id"])
                self._sync_task_fts(db, task_id)
                self._sync_work_order_fts(db, pack.work_order_id)
                self._ensure_milestones(db, pack.work_order_id, milestones)
                return dict(existing)
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
            active = self._fetch_one(
                db,
                """
                SELECT work_order_id FROM minion_work_orders
                WHERE task_id = ? AND status IN ('active', 'running', 'blocked', 'approval_pending')
                """,
                (task_id,),
            )
            if active is not None:
                raise ValueError(f"task already has an active work order: {active['work_order_id']}")
            db.execute(
                """
                INSERT INTO minion_work_orders(
                    work_order_id, task_id, title, goal, instruction, status, minion_profile,
                    metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pack.work_order_id,
                    task_id,
                    str(metadata.get("work_order_title") or title)[:160],
                    pack.goal,
                    pack.instruction,
                    "active",
                    pack.minion_profile,
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
            db.execute(
                "UPDATE minion_work_orders SET metadata_json = ?, updated_at = ? WHERE work_order_id = ?",
                (_json(metadata), utc_now(), str(work_order_id)),
            )

    def create_work_order_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.ensure_schema()
        now = utc_now()
        draft_id = str(payload.get("draft_id") or f"wod_{uuid4().hex[:16]}").strip()
        title = _clip_text(payload.get("title") or payload.get("work_order_title") or payload.get("goal") or "Work order draft", 160)
        goal = _clip_text(payload.get("goal") or payload.get("instruction") or title, _WORK_ORDER_DRAFT_TEXT_LIMIT)
        source_summary = _clip_text(payload.get("source_summary") or payload.get("conversation_summary") or "", _WORK_ORDER_DRAFT_TEXT_LIMIT)
        task_id = str(payload.get("task_id") or new_work_id("task")).strip()
        proposed_work_order_id = str(payload.get("proposed_work_order_id") or payload.get("work_order_id") or new_work_id("wo")).strip()
        minion_profile = str(payload.get("minion_profile") or "software_engineering.planner").strip() or "software_engineering.planner"
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
                "minion_profile": "software_engineering.planner",
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
            self._insert_ledger(db, work_order_id, event_kind, summary, payload, minion_id, run_id, created_at)
            if event_kind == "checkpoint":
                self._insert_checkpoint(db, work_order_id, payload, minion_id, run_id, created_at)
                self._record_payload_artifacts(db, work_order_id, payload)
            elif event_kind == "terminal":
                self._record_terminal(db, work_order_id, payload, minion_id, run_id, created_at)
                self._record_deferred_experience(db, work_order_id, payload)
                self._record_payload_artifacts(db, work_order_id, payload)
            elif event_kind == "module_completed":
                event_status = str(payload.get("status") or "").strip().lower()
                self._update_work_order_status(db, work_order_id, "completed" if event_status == "completed" else "active")
                self._record_payload_artifacts(db, work_order_id, payload)
            elif event_kind in {"phase_started", "progress"}:
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
            items = self._search_fts(
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
            items = self._search_fts(
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
            items = self._search_fts(
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

    def _insert_ledger(
        self,
        db: sqlite3.Connection,
        work_order_id: str,
        event_kind: str,
        summary: str,
        payload: dict[str, Any],
        minion_id: str,
        run_id: str,
        created_at: str,
    ) -> None:
        db.execute(
            """
            INSERT INTO minion_worker_ledger(
                ledger_id, work_order_id, event_kind, summary, payload_json, minion_id, run_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (f"led_{uuid4().hex[:16]}", work_order_id, event_kind, summary, _json(payload), minion_id, run_id, created_at),
        )

    def _insert_checkpoint(
        self,
        db: sqlite3.Connection,
        work_order_id: str,
        payload: dict[str, Any],
        minion_id: str,
        run_id: str,
        created_at: str,
    ) -> None:
        milestone_index = _coerce_int(payload.get("milestone_index"))
        if milestone_index is None:
            milestone_index = self._derive_current_milestone_index(db, work_order_id)
        status = str(payload.get("status") or "partial").strip().lower()
        if status not in {"completed", "partial", "blocked", "failed"}:
            status = "partial"
        db.execute(
            """
            INSERT INTO minion_worker_checkpoints(
                checkpoint_id, work_order_id, milestone_index, status, summary, payload_json, minion_id, run_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"chk_{uuid4().hex[:16]}",
                work_order_id,
                int(milestone_index or 0),
                status,
                str(payload.get("summary") or ""),
                _json(payload),
                minion_id,
                run_id,
                created_at,
            ),
        )

    def _record_terminal(
        self,
        db: sqlite3.Connection,
        work_order_id: str,
        payload: dict[str, Any],
        minion_id: str,
        run_id: str,
        created_at: str,
    ) -> None:
        status = str(payload.get("status") or "completed").strip().lower()
        if status not in {"completed", "failed", "blocked", "killed"}:
            status = "completed"
        if status == "completed" and self._has_incomplete_milestones(db, work_order_id):
            status = "active"
        self._update_work_order_status(db, work_order_id, status)

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
        if not artifacts:
            return
        row = self._fetch_one(db, "SELECT metadata_json FROM minion_work_orders WHERE work_order_id = ?", (str(work_order_id),))
        if row is None:
            return
        metadata = _loads(row["metadata_json"])
        existing = _artifact_list(metadata.get("artifacts"))
        metadata["artifacts"] = _merge_artifacts([*existing, *artifacts])
        if isinstance(primary, dict):
            metadata["primary_artifact"] = dict(primary)
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
            SET status = ?, updated_at = ?, ended_at = CASE WHEN ? != '' THEN ? ELSE ended_at END
            WHERE work_order_id = ?
            """,
            (status, utc_now(), ended_at, ended_at, work_order_id),
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

    def _search_fts(
        self,
        db: sqlite3.Connection,
        *,
        table_name: str,
        id_column: str,
        query: str,
        limit: int,
        fallback_sql: str,
    ) -> list[dict[str, Any]]:
        normalized = str(query or "").strip()
        resolved_limit = max(1, min(int(limit or 10), 50))
        if not normalized:
            rows = [
                {"id": str(row[0]), "score": float(row[1])}
                for row in db.execute(fallback_sql, ("%%", resolved_limit)).fetchall()
            ]
            ordered: list[dict[str, Any]] = []
            seen: set[str] = set()
            for row in rows:
                if row["id"] in seen:
                    continue
                seen.add(row["id"])
                ordered.append(row)
                if len(ordered) >= resolved_limit:
                    break
            return ordered
        rows: list[dict[str, Any]] = []
        for fts_query, query_weight in _compile_fts_queries(normalized):
            try:
                cursor = db.execute(
                    f"""
                    SELECT {id_column}, -bm25({table_name}) AS score
                    FROM {table_name}
                    WHERE {table_name} MATCH ?
                    ORDER BY bm25({table_name})
                    LIMIT ?
                    """,
                    (fts_query, resolved_limit),
                )
            except sqlite3.OperationalError:
                continue
            rows.extend({"id": str(row[0]), "score": float(row[1]) * float(query_weight)} for row in cursor.fetchall())
            if rows:
                break
        if not rows:
            like = f"%{normalized.lower()}%"
            rows.extend({"id": str(row[0]), "score": float(row[1])} for row in db.execute(fallback_sql, (like, resolved_limit)).fetchall())
        seen: set[str] = set()
        ordered: list[dict[str, Any]] = []
        for row in sorted(rows, key=lambda item: (-float(item["score"]), item["id"])):
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            ordered.append(row)
            if len(ordered) >= resolved_limit:
                break
        return ordered


def _coerce_milestones(raw: Any, acceptance_criteria: list[str], fallback: str) -> list[dict[str, Any]]:
    _ = acceptance_criteria, fallback
    return normalize_milestones(raw)


def _module_parent_milestones(artifact: PlanArtifact) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, module in enumerate(artifact.modules):
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
            "minion_profile": str(work_order.get("minion_profile") or "generic"),
            "metadata": metadata,
        }
    )


def _merge_pack_overrides(base: TaskContextPack, overrides: dict[str, Any]) -> TaskContextPack:
    data = base.to_dict()
    metadata = dict(data.get("metadata") or {})
    override_metadata = _loads_or_dict(overrides.get("metadata"))
    if override_metadata:
        metadata.update(override_metadata)
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
        "minion_profile",
        "resolved_profile",
        "continuity",
    ):
        value = overrides.get(key)
        if isinstance(value, str):
            if key == "minion_profile" and value.strip() == "generic" and str(data.get("minion_profile") or "generic") != "generic":
                continue
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
    continuity = _loads_or_dict(scaffold.get("continuity"))
    return {
        "instruction_chars": len(str(scaffold.get("instruction") or "")),
        "acceptance_criteria_count": len(list(scaffold.get("acceptance_criteria") or [])),
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


def _safe_id(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(value or "").strip())[:80] or uuid4().hex[:12]


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


def _compile_fts_queries(text: str) -> list[tuple[str, float]]:
    return compile_jieba_fts_queries(text)
