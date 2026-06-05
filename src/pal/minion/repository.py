from __future__ import annotations

import hashlib
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
    ReviewGateResult,
    build_planner_work_order,
    compile_coder_work_order,
    dispatchable_plan_validation,
    module_milestone_records,
    new_work_id,
    plan_module_order_for_execution,
    plan_milestone_id_at,
    plan_module_id_at,
    prompt_view_from_metadata,
    validate_dispatchable_plan_artifact,
    validate_review_gate_result,
)
from pal.minion.turns import build_minion_turn_from_pack
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
_PLAN_REVISION_DIR_PARTS = ("data", "minion", "plan_revisions")


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

                CREATE TABLE IF NOT EXISTS minion_review_gates (
                    gate_id TEXT PRIMARY KEY,
                    gate_kind TEXT NOT NULL,
                    target_kind TEXT NOT NULL DEFAULT '',
                    target_key TEXT NOT NULL DEFAULT '',
                    verdict TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    reviewer_profile TEXT NOT NULL DEFAULT '',
                    work_order_id TEXT NOT NULL DEFAULT '',
                    run_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS minion_review_gates_target
                ON minion_review_gates(gate_kind, target_kind, target_key, created_at);

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
        plan_execution = dict(metadata.get("plan_execution") or {})
        if not prompt_view and str(plan_execution.get("mode") or "") != "module_parent_milestones":
            prompt_view = _prompt_view_from_current_milestone(pack, continuity=continuity, metadata=metadata)
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
        artifact = validate_dispatchable_plan_artifact(plan_payload)
        plan_revision = _plan_revision_from_payload(plan_payload)
        validation = dispatchable_plan_validation(artifact)
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
            "plan_revision": plan_revision,
            "module_id": resolved_module_id,
            "current_milestone_index": 0,
            "milestone_count": len(milestones),
            "auto_advance": True,
            "checkpoint_review": {
                "enabled": True,
                "reviewer_profile": "software_engineering.reviewer",
                "max_repair_attempts": 5,
            },
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
                "plan_artifact": _plan_artifact_payload(artifact, plan_revision=plan_revision),
                "plan_validation": validation,
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
        artifact = validate_dispatchable_plan_artifact(plan_payload)
        plan_revision = _plan_revision_from_payload(plan_payload)
        validation = dispatchable_plan_validation(artifact)
        resolved_work_order_id = str(work_order_id or new_work_id("wo")).strip()
        module_order = plan_module_order_for_execution(artifact)
        milestones = _module_parent_milestones(artifact, module_order=module_order)
        pack_metadata = dict(metadata or {})
        plan_execution = dict(pack_metadata.get("plan_execution") or {})
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
                "child_work_order_ids": dict(plan_execution.get("child_work_order_ids") or {}),
            }
        )
        pack_metadata.update(
            {
                "task_id": artifact.task_id,
                "task_title": str(pack_metadata.get("task_title") or artifact.summary or artifact.task_id),
                "work_order_title": str(pack_metadata.get("work_order_title") or artifact.summary or "Plan implementation"),
                "plan_artifact": _plan_artifact_payload(artifact, plan_revision=plan_revision),
                "plan_validation": validation,
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
        loaded_plan = self.load_dispatchable_plan_ref(source_plan_ref)
        artifact = validate_dispatchable_plan_artifact(loaded_plan.get("plan_artifact") or {})
        source_revision = _plan_revision_from_payload(loaded_plan.get("plan_artifact"), loaded_plan.get("plan_ref"))
        next_revision = source_revision + 1
        resolved_work_order_id = str(work_order_id or new_work_id("wo")).strip()
        resolved_workspace = dict(workspace or {})
        for key in ("repo_path", "source_repo", "artifact_dir"):
            value = str(target.get(key) or "").strip()
            if value:
                resolved_workspace.setdefault(key, value)
        planner_goal = str(goal or f"Revise plan {artifact.plan_id} from reviewer gate {gate_payload.get('gate_id') or ''}").strip()
        planner_instruction = str(
            instruction
            or (
                "Revise the referenced FinalPlanArtifact only. Address the reviewer gate findings and required fixes, "
                f"preserve task_id={artifact.task_id} and plan_id={artifact.plan_id}, output plan_revision={next_revision}, "
                "keep the fork_join_linear topology dispatchable, and write a primary plan.json FinalPlanArtifact. "
                "Do not implement code."
            )
        )
        planner_work_order = build_planner_work_order(
            goal=planner_goal,
            task_id=artifact.task_id,
            work_order_id=resolved_work_order_id,
            turn_index=next_revision,
            plan_revision=next_revision,
        )
        planner_work_order["revision_source"] = {
            "source_plan_ref": dict(loaded_plan.get("plan_ref") or source_plan_ref),
            "review_gate_ref": dict(loaded_gate.get("review_gate_ref") or {}),
            "review_gate": _plan_revision_gate_summary(gate_payload),
        }
        milestones = [
            {
                "milestone_id": "revise_plan",
                "title": "Revise reviewed plan",
                "summary": (
                    "Produce one revised, dispatchable FinalPlanArtifact that resolves the plan reviewer findings."
                ),
                "acceptance": [
                    f"FinalPlanArtifact preserves task_id={artifact.task_id} and plan_id={artifact.plan_id}.",
                    f"FinalPlanArtifact declares plan_revision={next_revision}.",
                    "Reviewer findings and required fixes are addressed or explicitly called out as remaining questions.",
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
        pack_metadata.update(
            {
                "task_id": artifact.task_id,
                "task_title": str(pack_metadata.get("task_title") or artifact.summary or artifact.task_id),
                "work_order_title": str(pack_metadata.get("work_order_title") or f"Revise plan {artifact.plan_id}"),
                "planner_work_order": planner_work_order,
                "plan_revision": next_revision,
                "source_plan_ref": dict(loaded_plan.get("plan_ref") or source_plan_ref),
                "source_plan_artifact": _plan_artifact_payload(artifact, plan_revision=source_revision),
                "review_gate_ref": dict(loaded_gate.get("review_gate_ref") or {}),
                "review_gate": _plan_revision_gate_summary(gate_payload),
                "milestones": milestones,
                "plan_review": plan_review_state,
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
                "minion_profile": "software_engineering.planner",
                "metadata": pack_metadata,
            }
        )

    def load_dispatchable_plan_ref(self, plan_ref: Any) -> dict[str, Any]:
        ref = _coerce_plan_ref(plan_ref)
        path = _resolve_plan_ref_path(ref, runtime_root=self.runtime_root)
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        expected = str(ref.get("sha256") or "").strip()
        if expected and expected != digest:
            raise ValueError(f"plan_ref sha256 mismatch for {path}")
        try:
            payload = json.loads(content.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"plan_ref is not valid JSON: {path}") from exc
        artifact = validate_dispatchable_plan_artifact(payload)
        validation = dispatchable_plan_validation(artifact)
        plan_revision = _plan_revision_from_payload(payload, ref)
        payload_metadata = dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {}
        normalized_ref = {
            "path": str(path),
            "sha256": digest,
            "plan_id": artifact.plan_id,
            "task_id": artifact.task_id,
            "plan_revision": plan_revision,
        }
        revision_of = payload_metadata.get("revision_of")
        if isinstance(revision_of, dict):
            normalized_ref["revision_of"] = dict(revision_of)
        for key in ("accepted", "accepted_at", "acceptance_marker_path", "accepted_latest_marker_path"):
            if key in ref:
                normalized_ref[key] = ref[key]
        return {
            "plan_artifact": _plan_artifact_payload(artifact, plan_revision=plan_revision),
            "plan_ref": normalized_ref,
            "plan_validation": validation,
        }

    def load_accepted_plan_ref(self, plan_ref: Any) -> dict[str, Any]:
        loaded = self.load_dispatchable_plan_ref(plan_ref)
        ref = dict(loaded.get("plan_ref") or {})
        marker = _load_accepted_plan_marker(self.runtime_root, ref)
        marker_ref = dict(marker.get("plan_ref") or {})
        expected_sha = str(ref.get("sha256") or "").strip()
        marker_sha = str(marker_ref.get("sha256") or "").strip()
        if expected_sha and marker_sha and expected_sha != marker_sha:
            raise ValueError("plan_ref acceptance marker sha256 mismatch")
        expected_revision = _plan_revision_from_payload(loaded.get("plan_artifact"), ref)
        marker_revision = _plan_revision_from_payload(None, marker_ref)
        if marker_revision != expected_revision:
            raise ValueError("plan_ref acceptance marker revision mismatch")
        if marker.get("review_gate_ref"):
            self._validate_plan_acceptance_gate(marker.get("review_gate_ref"), loaded)
        elif not marker.get("human_override"):
            raise ValueError("plan_ref acceptance marker lacks review_gate_ref or human_override")
        accepted_ref = dict(ref)
        accepted_ref.update(
            {
                "accepted": True,
                "accepted_at": str(marker.get("accepted_at") or ""),
                "acceptance_marker_path": str(marker.get("acceptance_marker_path") or ""),
                "accepted_latest_marker_path": str(marker.get("accepted_latest_marker_path") or ""),
            }
        )
        loaded["plan_ref"] = accepted_ref
        loaded["review_gate_ref"] = dict(marker.get("review_gate_ref") or {})
        loaded["human_override"] = dict(marker.get("human_override") or {})
        return loaded

    def submit_review_gate(
        self,
        gate_payload: dict[str, Any] | ReviewGateResult,
        *,
        reviewer_profile: str = "",
        work_order_id: str = "",
        run_id: str = "",
    ) -> dict[str, Any]:
        self.ensure_schema()
        gate = validate_review_gate_result(gate_payload)
        payload = gate.to_dict()
        if reviewer_profile:
            payload["reviewer_profile"] = str(reviewer_profile or "").strip()
            gate = ReviewGateResult.from_dict(payload)
        target_kind, target_key, target_binding = self._validate_review_gate_target(gate)
        _validate_review_gate_tool_evidence_refs(self, gate)
        _validate_bound_review_gate_evidence(gate, target_binding)
        created_at = utc_now()
        normalized_payload = gate.to_dict()
        normalized_payload["target"] = dict(target_binding)
        normalized_payload["created_at"] = created_at
        normalized_payload["target_kind"] = target_kind
        normalized_payload["target_key"] = target_key
        normalized_work_order_id = str(work_order_id or normalized_payload.get("target", {}).get("work_order_id") or "").strip()
        normalized_run_id = str(run_id or normalized_payload.get("target", {}).get("run_id") or "").strip()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO minion_review_gates(
                    gate_id, gate_kind, target_kind, target_key, verdict, summary,
                    payload_json, reviewer_profile, work_order_id, run_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    gate.gate_id,
                    gate.gate_kind,
                    target_kind,
                    target_key,
                    gate.verdict,
                    gate.summary,
                    _json(normalized_payload),
                    gate.reviewer_profile,
                    normalized_work_order_id,
                    normalized_run_id,
                    created_at,
                ),
            )
            ledger_work_order_id = normalized_work_order_id or str(normalized_payload.get("target", {}).get("plan_ref", {}).get("plan_id") or "plan_review")
            self._insert_ledger(
                db,
                ledger_work_order_id,
                "review_gate",
                gate.summary or f"{gate.gate_kind} review gate {gate.verdict}",
                normalized_payload,
                gate.reviewer_profile,
                normalized_run_id,
                created_at,
            )
            milestone_closure: dict[str, Any] = {}
            if gate.gate_kind in {"checkpoint_verification", "repair_verification"} and gate.verdict == "pass" and target_kind == "checkpoint":
                milestone_closure = self._close_checkpoint_from_review_gate_locked(
                    db,
                    checkpoint_id=target_key,
                    gate_payload=normalized_payload,
                    created_at=created_at,
                )
        return {
            "status": "recorded",
            "review_gate": normalized_payload,
            "review_gate_ref": {
                "gate_id": gate.gate_id,
                "gate_kind": gate.gate_kind,
                "target_kind": target_kind,
                "target_key": target_key,
                "verdict": gate.verdict,
                "created_at": created_at,
            },
            "milestone_closure": milestone_closure,
        }

    def record_review_tool_evidence_refs(
        self,
        refs: list[dict[str, Any]],
        *,
        work_order_id: str,
        run_id: str = "",
        reviewer_profile: str = "",
    ) -> list[dict[str, Any]]:
        self.ensure_schema()
        normalized: list[dict[str, Any]] = []
        now = utc_now()
        with self._connect() as db:
            for raw in list(refs or []):
                if not isinstance(raw, dict):
                    continue
                ref = dict(raw)
                if str(ref.get("ledger_id") or "").strip():
                    normalized.append(ref)
                    continue
                evidence_ref_id = str(ref.get("evidence_ref_id") or "").strip() or f"tev_{uuid4().hex[:12]}"
                ref["evidence_ref_id"] = evidence_ref_id
                payload = {
                    "status": "recorded",
                    "kind": "review_tool_evidence",
                    "evidence_ref": ref,
                    "reviewer_profile": str(reviewer_profile or ""),
                }
                ledger_id = self._insert_ledger(
                    db,
                    str(work_order_id or "review_tool_evidence"),
                    "review_tool_evidence",
                    str(ref.get("summary") or ref.get("tool_name") or evidence_ref_id),
                    payload,
                    "",
                    str(run_id or ""),
                    now,
                )
                ref["ledger_id"] = ledger_id
                normalized.append(ref)
        return normalized

    def validate_external_verification_ref(self, ref: Any) -> dict[str, Any]:
        self.ensure_schema()
        payload = dict(ref or {}) if isinstance(ref, dict) else {"ref": str(ref or "")}
        kind = str(payload.get("kind") or payload.get("type") or "").strip().lower()
        if kind in {"review_gate", "review_gate_ref"} or payload.get("gate_id"):
            loaded = self.load_review_gate(payload.get("review_gate_ref") or payload)
            gate = dict(loaded.get("review_gate") or {})
            if str(gate.get("verdict") or "").strip().lower() != "pass":
                raise ValueError("external_verification_ref review_gate must be a pass gate")
            return {"kind": "review_gate", "review_gate_ref": dict(loaded.get("review_gate_ref") or {})}
        if kind in {"checkpoint", "checkpoint_ref"} or payload.get("checkpoint_id"):
            checkpoint_id = str(payload.get("checkpoint_id") or payload.get("id") or "").strip()
            if not checkpoint_id:
                raise ValueError("external_verification_ref checkpoint_id is required")
            with self._connect() as db:
                row = self._fetch_one(db, "SELECT * FROM minion_worker_checkpoints WHERE checkpoint_id = ?", (checkpoint_id,))
            if row is None:
                raise ValueError(f"unknown external checkpoint ref: {checkpoint_id}")
            return {"kind": "checkpoint", "checkpoint_id": checkpoint_id, "status": str(row["status"] or "")}
        if kind in {"ledger", "ledger_event"} or payload.get("ledger_id"):
            ledger_id = str(payload.get("ledger_id") or payload.get("id") or "").strip()
            if not ledger_id:
                raise ValueError("external_verification_ref ledger_id is required")
            with self._connect() as db:
                row = self._fetch_one(db, "SELECT * FROM minion_worker_ledger WHERE ledger_id = ?", (ledger_id,))
            if row is None:
                raise ValueError(f"unknown external ledger ref: {ledger_id}")
            return {"kind": "ledger", "ledger_id": ledger_id, "event_kind": str(row["event_kind"] or "")}
        if kind in {"artifact", "file", "path"} or payload.get("path"):
            path = _resolve_runtime_file_ref(payload, runtime_root=self.runtime_root)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            expected = str(payload.get("sha256") or "").strip()
            if expected and expected != digest:
                raise ValueError(f"external_verification_ref sha256 mismatch for {path}")
            return {"kind": "artifact", "path": str(path), "sha256": digest}
        raise ValueError("external_verification_ref must reference a review_gate, checkpoint, ledger event, or runtime artifact path")

    def count_ledger_events(self, work_order_id: str, event_kind: str) -> int:
        self.ensure_schema()
        with self._connect() as db:
            row = self._fetch_one(
                db,
                "SELECT COUNT(*) AS count FROM minion_worker_ledger WHERE work_order_id = ? AND event_kind = ?",
                (str(work_order_id), str(event_kind)),
            )
        return int(row["count"] if row else 0)

    def load_review_gate(self, review_gate_ref: Any) -> dict[str, Any]:
        self.ensure_schema()
        gate_id = _coerce_review_gate_id(review_gate_ref)
        with self._connect() as db:
            row = self._fetch_one(db, "SELECT * FROM minion_review_gates WHERE gate_id = ?", (gate_id,))
        if row is None:
            raise ValueError(f"unknown review_gate_ref: {gate_id}")
        payload = _loads(row["payload_json"])
        payload.setdefault("gate_id", str(row["gate_id"]))
        payload.setdefault("gate_kind", str(row["gate_kind"]))
        payload.setdefault("verdict", str(row["verdict"]))
        payload.setdefault("summary", str(row["summary"] or ""))
        payload.setdefault("reviewer_profile", str(row["reviewer_profile"] or ""))
        payload.setdefault("created_at", str(row["created_at"] or ""))
        payload.setdefault("target_kind", str(row["target_kind"] or ""))
        payload.setdefault("target_key", str(row["target_key"] or ""))
        return {"review_gate": payload, "review_gate_ref": _compact_review_gate_ref(payload)}

    def _validate_review_gate_target(self, gate: ReviewGateResult) -> tuple[str, str, dict[str, Any]]:
        target = dict(gate.target)
        if gate.gate_kind == "plan_acceptance":
            loaded = self.load_dispatchable_plan_ref(target.get("plan_ref"))
            plan_ref = dict(loaded.get("plan_ref") or {})
            target["plan_ref"] = plan_ref
            target["plan_validation"] = dict(loaded.get("plan_validation") or {})
            return "plan_ref", _plan_target_key(plan_ref), target
        if gate.gate_kind in {"checkpoint_verification", "repair_verification"}:
            checkpoint_id = str(target.get("checkpoint_id") or "").strip()
            if checkpoint_id:
                with self._connect() as db:
                    row = self._fetch_one(
                        db,
                        "SELECT * FROM minion_worker_checkpoints WHERE checkpoint_id = ?",
                        (checkpoint_id,),
                    )
                if row is None:
                    raise ValueError(f"unknown checkpoint_id for review gate: {checkpoint_id}")
                payload = _loads(row["payload_json"])
                expected_gate_kind = str(payload.get("expected_review_gate_kind") or "").strip().lower()
                if expected_gate_kind and gate.gate_kind != expected_gate_kind:
                    raise ValueError(f"review gate kind mismatch: checkpoint expects {expected_gate_kind}")
                if gate.gate_kind == "repair_verification" and not (expected_gate_kind == "repair_verification" or isinstance(payload.get("repair_attempt"), dict)):
                    raise ValueError("repair_verification requires a repair checkpoint target")
                if gate.verdict == "pass" and list(payload.get("shell_mutation_violations") or []):
                    raise ValueError("checkpoint has unresolved shell_mutation_violations; review gate cannot pass")
                commit_sha = str(target.get("commit_sha") or "").strip()
                row_commit = str(payload.get("commit_sha") or payload.get("git_commit", {}).get("commit_sha") or "").strip()
                if commit_sha and row_commit and commit_sha != row_commit:
                    raise ValueError("review gate checkpoint commit_sha mismatch")
                target.setdefault("work_order_id", str(row["work_order_id"] or ""))
                target.setdefault("run_id", str(row["run_id"] or ""))
                target.setdefault("minion_id", str(row["minion_id"] or ""))
                target.setdefault("milestone_index", _coerce_int(row["milestone_index"]) or 0)
                target.setdefault("milestone_id", str(payload.get("milestone_id") or ""))
                target.setdefault("acceptance_criteria", [str(item) for item in list(payload.get("acceptance_criteria") or []) if str(item or "").strip()])
                if row_commit:
                    target.setdefault("commit_sha", row_commit)
                return "checkpoint", checkpoint_id, target
            commit_sha = str(target.get("commit_sha") or "").strip()
            if not commit_sha:
                raise ValueError("checkpoint review gate requires checkpoint_id or commit_sha")
            return "commit", commit_sha, target
        raise ValueError(f"unsupported review gate kind: {gate.gate_kind}")

    def latest_review_gate_for_checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        self.ensure_schema()
        normalized = str(checkpoint_id or "").strip()
        if not normalized:
            return {"status": "invalid", "error": "checkpoint_id is required"}
        with self._connect() as db:
            row = self._fetch_one(
                db,
                """
                SELECT * FROM minion_review_gates
                WHERE target_kind = 'checkpoint' AND target_key = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (normalized,),
            )
        if row is None:
            return {"status": "not_found", "checkpoint_id": normalized}
        payload = _loads(row["payload_json"])
        return {
            "status": "ok",
            "review_gate": payload,
            "review_gate_ref": _compact_review_gate_ref(payload),
        }

    def latest_review_gate_for_plan_ref(self, plan_ref: Any) -> dict[str, Any]:
        self.ensure_schema()
        try:
            loaded = self.load_dispatchable_plan_ref(plan_ref)
        except Exception as exc:
            return {"status": "invalid", "error": str(exc)}
        normalized_ref = dict(loaded.get("plan_ref") or {})
        target_key = _plan_target_key(normalized_ref)
        if not target_key:
            return {"status": "invalid", "error": "plan_ref target key is empty"}
        with self._connect() as db:
            row = self._fetch_one(
                db,
                """
                SELECT * FROM minion_review_gates
                WHERE gate_kind = 'plan_acceptance' AND target_kind = 'plan_ref' AND target_key = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (target_key,),
            )
        if row is None:
            return {"status": "not_found", "plan_ref": normalized_ref}
        payload = _loads(row["payload_json"])
        return {
            "status": "ok",
            "review_gate": payload,
            "review_gate_ref": _compact_review_gate_ref(payload),
        }

    def close_checkpoint_from_review_gate(self, review_gate_ref: Any) -> dict[str, Any]:
        self.ensure_schema()
        loaded = self.load_review_gate(review_gate_ref)
        gate = dict(loaded.get("review_gate") or {})
        if str(gate.get("verdict") or "") != "pass":
            raise ValueError("checkpoint closure requires a passing review gate")
        target = dict(gate.get("target") or {})
        checkpoint_id = str(target.get("checkpoint_id") or "").strip()
        if not checkpoint_id:
            raise ValueError("checkpoint closure requires target.checkpoint_id")
        with self._connect() as db:
            return self._close_checkpoint_from_review_gate_locked(db, checkpoint_id=checkpoint_id, gate_payload=gate, created_at=utc_now())

    def _close_checkpoint_from_review_gate_locked(
        self,
        db: sqlite3.Connection,
        *,
        checkpoint_id: str,
        gate_payload: dict[str, Any],
        created_at: str,
    ) -> dict[str, Any]:
        row = self._fetch_one(db, "SELECT * FROM minion_worker_checkpoints WHERE checkpoint_id = ?", (str(checkpoint_id),))
        if row is None:
            raise ValueError(f"unknown checkpoint_id for closure: {checkpoint_id}")
        work_order_id = str(row["work_order_id"] or "")
        milestone_index = int(row["milestone_index"])
        existing = self._fetch_one(
            db,
            """
            SELECT * FROM minion_worker_checkpoints
            WHERE work_order_id = ? AND milestone_index = ? AND status = 'completed'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (work_order_id, milestone_index),
        )
        if existing is not None:
            return {
                "status": "already_closed",
                "checkpoint_id": str(existing["checkpoint_id"] or ""),
                "work_order_id": work_order_id,
                "milestone_index": milestone_index,
            }
        claim_payload = _loads(row["payload_json"])
        closure_payload = {
            **claim_payload,
            "checkpoint_id": f"chk_{uuid4().hex[:16]}",
            "status": "completed",
            "claimed_checkpoint_id": str(checkpoint_id),
            "review_gate_ref": _compact_review_gate_ref(gate_payload),
            "review_gate": gate_payload,
            "summary": str(gate_payload.get("summary") or claim_payload.get("summary") or "checkpoint review passed"),
        }
        self._insert_checkpoint(db, work_order_id, closure_payload, str(row["minion_id"] or ""), str(row["run_id"] or ""), created_at)
        self._insert_ledger(
            db,
            work_order_id,
            "milestone_closed",
            str(closure_payload["summary"]),
            closure_payload,
            str(row["minion_id"] or ""),
            str(row["run_id"] or ""),
            created_at,
        )
        return {
            "status": "closed",
            "work_order_id": work_order_id,
            "milestone_index": milestone_index,
            "claimed_checkpoint_id": str(checkpoint_id),
            "commit_sha": str(closure_payload.get("commit_sha") or ""),
            "payload": closure_payload,
        }

    def _validate_plan_acceptance_gate(self, review_gate_ref: Any, loaded_plan: dict[str, Any]) -> dict[str, Any]:
        gate_payload = dict(self.load_review_gate(review_gate_ref).get("review_gate") or {})
        if str(gate_payload.get("gate_kind") or "") != "plan_acceptance":
            raise ValueError("review_gate_ref must reference a plan_acceptance gate")
        if str(gate_payload.get("verdict") or "") != "pass":
            raise ValueError("plan acceptance requires review gate verdict=pass")
        plan_ref = dict(loaded_plan.get("plan_ref") or {})
        target = dict(gate_payload.get("target") or {})
        target_ref = dict(target.get("plan_ref") or {})
        if _plan_target_key(target_ref) != _plan_target_key(plan_ref):
            raise ValueError("review gate target plan_ref does not match accepted plan_ref")
        return gate_payload

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
        source = self.load_dispatchable_plan_ref(plan_ref)
        source_ref = dict(source.get("plan_ref") or {})
        source_revision = _plan_revision_from_payload(source.get("plan_artifact"), source_ref)
        if not isinstance(revised_plan_artifact, dict):
            raise ValueError("revised_plan_artifact must be an object")
        revised_payload = dict(revised_plan_artifact or {})
        expected_revision = source_revision + 1
        declared_revision = _coerce_int(revised_payload.get("plan_revision"))
        if declared_revision is not None and declared_revision != expected_revision:
            raise ValueError(
                f"revised plan_revision must be {expected_revision} when revising source revision {source_revision}"
            )
        artifact = validate_dispatchable_plan_artifact(revised_payload)
        if artifact.plan_id != str(source_ref.get("plan_id") or ""):
            raise ValueError("revised plan_id must match source plan_ref")
        if artifact.task_id != str(source_ref.get("task_id") or ""):
            raise ValueError("revised task_id must match source plan_ref")
        latest_revision = _latest_plan_revision(self.runtime_root, task_id=artifact.task_id, plan_id=artifact.plan_id)
        if latest_revision > source_revision:
            raise ValueError(
                f"source plan_ref is stale: latest revision is {latest_revision}, source revision is {source_revision}"
            )
        result = self._write_plan_revision(
            artifact,
            revision=expected_revision,
            source_plan_ref=source_ref,
            revision_notes=revision_notes,
        )
        if accepted:
            acceptance = self.accept_plan_ref(
                result["plan_ref"],
                reason=revision_notes or f"accepted plan revision {expected_revision}",
                review_gate_ref=review_gate_ref,
                human_override=human_override,
            )
            result["acceptance"] = acceptance
            result["plan_ref"] = dict(acceptance.get("plan_ref") or result["plan_ref"])
        return result

    def accept_plan_ref(
        self,
        plan_ref: Any,
        *,
        reason: str = "",
        review_gate_ref: Any = None,
        human_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        loaded = self.load_dispatchable_plan_ref(plan_ref)
        ref = dict(loaded.get("plan_ref") or {})
        plan_revision = _plan_revision_from_payload(loaded.get("plan_artifact"), ref)
        gate_payload: dict[str, Any] | None = None
        override_payload: dict[str, Any] | None = None
        if review_gate_ref:
            gate_payload = self._validate_plan_acceptance_gate(review_gate_ref, loaded)
            override_payload = _validate_human_override(human_override)
        else:
            override_payload = _validate_human_override(human_override)
            if override_payload is None:
                raise ValueError("plan acceptance requires passing plan review gate or explicit human_override.reason")
        accepted_at = utc_now()
        marker = {
            "status": "accepted",
            "accepted_at": accepted_at,
            "reason": str(reason or "").strip(),
            "plan_ref": ref,
            "plan_validation": dict(loaded.get("plan_validation") or {}),
        }
        if gate_payload is not None:
            marker["review_gate_ref"] = _compact_review_gate_ref(gate_payload)
        if override_payload is not None:
            marker["human_override"] = dict(override_payload)
        marker_dir = _plan_revision_dir(self.runtime_root, task_id=str(ref.get("task_id") or ""), plan_id=str(ref.get("plan_id") or ""))
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker_json = json.dumps(marker, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        marker_path = marker_dir / f"accepted.v{plan_revision}.json"
        if not marker_path.exists():
            with marker_path.open("x", encoding="utf-8") as handle:
                handle.write(marker_json)
        latest_marker_path = marker_dir / "accepted.json"
        latest_marker_path.write_text(marker_json, encoding="utf-8")
        accepted_ref = dict(ref)
        accepted_ref.update(
            {
                "accepted": True,
                "accepted_at": accepted_at,
                "acceptance_marker_path": str(marker_path),
                "accepted_latest_marker_path": str(latest_marker_path),
            }
        )
        return {
            "status": "accepted",
            "plan_ref": accepted_ref,
            "plan_artifact": loaded.get("plan_artifact"),
            "plan_validation": loaded.get("plan_validation"),
            "review_gate_ref": marker.get("review_gate_ref") or {},
            "human_override": marker.get("human_override") or {},
            "acceptance_marker_path": str(marker_path),
            "accepted_latest_marker_path": str(latest_marker_path),
            "plan_revision": plan_revision,
        }

    def _write_plan_revision(
        self,
        artifact: PlanArtifact,
        *,
        revision: int,
        source_plan_ref: dict[str, Any],
        revision_notes: str = "",
    ) -> dict[str, Any]:
        plan_revision = max(0, int(revision))
        payload = _plan_artifact_payload(artifact, plan_revision=plan_revision)
        metadata = dict(payload.get("metadata") or {})
        metadata["plan_revision"] = plan_revision
        metadata["revision_of"] = {
            "path": str(source_plan_ref.get("path") or ""),
            "sha256": str(source_plan_ref.get("sha256") or ""),
            "plan_revision": _plan_revision_from_payload(None, source_plan_ref),
        }
        if revision_notes:
            metadata["revision_notes"] = str(revision_notes)
        payload["metadata"] = metadata
        validation = dispatchable_plan_validation(artifact)
        revision_dir = _plan_revision_dir(self.runtime_root, task_id=artifact.task_id, plan_id=artifact.plan_id)
        revision_dir.mkdir(parents=True, exist_ok=True)
        path = revision_dir / f"plan.v{plan_revision}.json"
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        with path.open("x", encoding="utf-8") as handle:
            handle.write(encoded)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        plan_ref = {
            "path": str(path),
            "sha256": digest,
            "plan_id": artifact.plan_id,
            "task_id": artifact.task_id,
            "plan_revision": plan_revision,
            "revision_of": metadata["revision_of"],
        }
        return {
            "status": "revised",
            "plan_ref": plan_ref,
            "plan_artifact": payload,
            "plan_validation": validation,
            "plan_revision": plan_revision,
        }

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
        if status == "running_module":
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
                child_id = str(plan_execution.get("active_child_work_order_id") or "").strip()
                if target and target not in {parent_id, child_id}:
                    continue
                inspected += 1
                if child_id and child_id in active_ids:
                    active.append(
                        {
                            "parent_work_order_id": parent_id,
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
                child_id = str(plan_execution.get("active_child_work_order_id") or "").strip()
                if target not in {parent_id, child_id}:
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
            elif event_kind == "milestone_completed":
                self._record_deferred_experience(db, work_order_id, payload)
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
    ) -> str:
        ledger_id = f"led_{uuid4().hex[:16]}"
        db.execute(
            """
            INSERT INTO minion_worker_ledger(
                ledger_id, work_order_id, event_kind, summary, payload_json, minion_id, run_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ledger_id, work_order_id, event_kind, summary, _json(payload), minion_id, run_id, created_at),
        )
        return ledger_id

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
        if status not in {"completed", "claimed", "partial", "blocked", "failed"}:
            status = "partial"
        checkpoint_id = str(payload.get("checkpoint_id") or "").strip() or f"chk_{uuid4().hex[:16]}"
        db.execute(
            """
            INSERT INTO minion_worker_checkpoints(
                checkpoint_id, work_order_id, milestone_index, status, summary, payload_json, minion_id, run_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                checkpoint_id,
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
                self._insert_ledger(db, child_id, "terminal", str(child_payload["summary"]), child_payload, "", "", now)
                self._record_terminal(db, child_id, child_payload, "", "", now)
                child_terminal_recorded = True
        plan_execution["status"] = "awaiting_continue"
        plan_execution["status_reason"] = reason
        if child_id:
            plan_execution["last_released_child_work_order_id"] = child_id
        plan_execution.pop("active_child_work_order_id", None)
        parent_metadata["plan_execution"] = plan_execution
        db.execute(
            "UPDATE minion_work_orders SET metadata_json = ?, updated_at = ? WHERE work_order_id = ?",
            (_json(parent_metadata), now, parent_id),
        )
        self._update_work_order_status(db, parent_id, "active")
        parent_payload = {
            "status": "awaiting_continue",
            "summary": reason or f"released stale running module for {parent_id}",
            "reason": "manager_recovery",
            "parent_work_order_id": parent_id,
            "child_work_order_id": child_id,
            "child_terminal_status": normalized_child_status,
            "child_terminal_recorded": child_terminal_recorded,
        }
        self._insert_ledger(db, parent_id, "module_recovered", str(parent_payload["summary"]), parent_payload, "", "", now)
        return {
            "parent_work_order_id": parent_id,
            "child_work_order_id": child_id,
            "status": "awaiting_continue",
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


def _role_from_profile(profile: str) -> str:
    parts = [part for part in str(profile or "").replace("/", ".").split(".") if part]
    return parts[-1] if parts else "minion"


def _coerce_plan_ref(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        ref = dict(value)
    else:
        ref = {"path": str(value or "").strip()}
    path = str(ref.get("path") or ref.get("artifact_path") or ref.get("relative_path") or "").strip()
    if not path:
        raise ValueError("plan_ref.path is required")
    ref["path"] = path
    return ref


def _resolve_plan_ref_path(plan_ref: dict[str, Any], *, runtime_root: Path) -> Path:
    raw = Path(str(plan_ref.get("path") or ""))
    path = raw if raw.is_absolute() else runtime_root / raw
    resolved = path.resolve()
    root = runtime_root.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"plan_ref must be under runtime_root: {resolved}")
    if not resolved.exists():
        raise FileNotFoundError(f"plan_ref does not exist: {resolved}")
    if not resolved.is_file():
        raise ValueError(f"plan_ref is not a file: {resolved}")
    return resolved


def _load_accepted_plan_marker(runtime_root: Path, plan_ref: dict[str, Any]) -> dict[str, Any]:
    revision = _plan_revision_from_payload(None, plan_ref)
    task_id = str(plan_ref.get("task_id") or "").strip()
    plan_id = str(plan_ref.get("plan_id") or "").strip()
    candidates: list[Path] = []
    for key in ("acceptance_marker_path", "accepted_latest_marker_path"):
        raw = str(plan_ref.get(key) or "").strip()
        if raw:
            candidates.append(Path(raw))
    if task_id and plan_id:
        marker_dir = _plan_revision_dir(runtime_root, task_id=task_id, plan_id=plan_id)
        candidates.append(marker_dir / f"accepted.v{revision}.json")
        candidates.append(marker_dir / "accepted.json")
    root = runtime_root.resolve()
    for candidate in candidates:
        path = candidate if candidate.is_absolute() else runtime_root / candidate
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            continue
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict) or str(payload.get("status") or "") != "accepted":
            continue
        marker_ref = dict(payload.get("plan_ref") or {})
        if str(marker_ref.get("sha256") or "") != str(plan_ref.get("sha256") or ""):
            continue
        if _plan_revision_from_payload(None, marker_ref) != revision:
            continue
        result = dict(payload)
        result["acceptance_marker_path"] = str(resolved)
        latest = _plan_revision_dir(runtime_root, task_id=task_id, plan_id=plan_id) / "accepted.json" if task_id and plan_id else resolved
        result["accepted_latest_marker_path"] = str(latest)
        return result
    raise ValueError("plan_ref is not accepted for dispatch")


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


def _prompt_safe_workspace(workspace: dict[str, Any]) -> dict[str, str]:
    allowed = {"repo_path", "source_repo", "artifact_dir", "task_repo_path", "target_repo_path"}
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


def _plan_revision_dir(runtime_root: Path, *, task_id: str, plan_id: str) -> Path:
    path = Path(runtime_root)
    for part in _PLAN_REVISION_DIR_PARTS:
        path = path / part
    return path / _safe_id(task_id) / _safe_id(plan_id)


def _latest_plan_revision(runtime_root: Path, *, task_id: str, plan_id: str) -> int:
    revision_dir = _plan_revision_dir(runtime_root, task_id=task_id, plan_id=plan_id)
    latest = -1
    if not revision_dir.exists():
        return latest
    for path in revision_dir.glob("plan.v*.json"):
        stem = path.stem
        raw = stem.removeprefix("plan.v")
        value = _coerce_int(raw)
        if value is not None:
            latest = max(latest, int(value))
    return latest


def _plan_revision_from_payload(payload: Any, ref: Any | None = None) -> int:
    payload_dict = dict(payload) if isinstance(payload, dict) else {}
    ref_dict = dict(ref) if isinstance(ref, dict) else {}
    metadata = dict(payload_dict.get("metadata") or {}) if isinstance(payload_dict.get("metadata"), dict) else {}
    for value in (payload_dict.get("plan_revision"), metadata.get("plan_revision"), ref_dict.get("plan_revision")):
        coerced = _coerce_int(value)
        if coerced is not None and coerced >= 0:
            return int(coerced)
    return 0


def _plan_target_key(plan_ref: dict[str, Any]) -> str:
    ref = dict(plan_ref or {})
    return ":".join(
        [
            "plan",
            _safe_id(str(ref.get("task_id") or "")),
            _safe_id(str(ref.get("plan_id") or "")),
            f"v{_plan_revision_from_payload(None, ref)}",
            str(ref.get("sha256") or "").strip(),
        ]
    )


def _coerce_review_gate_id(value: Any) -> str:
    if isinstance(value, dict):
        gate_id = str(value.get("gate_id") or value.get("id") or "").strip()
    else:
        gate_id = str(value or "").strip()
    if not gate_id:
        raise ValueError("review_gate_ref.gate_id is required")
    return gate_id


def _compact_review_gate_ref(payload: dict[str, Any]) -> dict[str, Any]:
    gate = dict(payload or {})
    result: dict[str, Any] = {}
    for key in ("gate_id", "gate_kind", "verdict", "target_kind", "target_key", "created_at"):
        value = gate.get(key)
        if value not in (None, "", []):
            result[key] = value
    target = dict(gate.get("target") or {})
    if isinstance(target.get("plan_ref"), dict):
        plan_ref = dict(target.get("plan_ref") or {})
        result["plan_ref"] = {
            key: plan_ref.get(key)
            for key in ("path", "sha256", "plan_id", "task_id", "plan_revision")
            if plan_ref.get(key) not in (None, "", [])
        }
    for key in ("checkpoint_id", "work_order_id", "run_id", "module_id", "milestone_id", "milestone_index", "commit_sha"):
        if target.get(key) not in (None, "", []):
            result[key] = target.get(key)
    return result


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


def _validate_bound_review_gate_evidence(gate: ReviewGateResult, target_binding: dict[str, Any]) -> None:
    if gate.verdict != "pass" or gate.gate_kind not in {"checkpoint_verification", "repair_verification"}:
        return
    criteria = [str(item).strip() for item in list(target_binding.get("acceptance_criteria") or []) if str(item or "").strip()]
    if not criteria:
        return
    covered = _review_gate_coverage_tokens(gate)
    missing: list[str] = []
    for index, criterion in enumerate(criteria, start=1):
        normalized = _coverage_token(criterion)
        aliases = {normalized, str(index), f"#{index}", f"ac{index}", f"acceptance_{index}"}
        if not any(alias in covered for alias in aliases):
            missing.append(criterion)
    if missing:
        raise ValueError(
            "checkpoint pass review gate lacks evidence coverage for acceptance criteria: "
            + "; ".join(missing[:3])
        )


def _validate_review_gate_tool_evidence_refs(self: MinionTaskingRepository, gate: ReviewGateResult) -> None:
    refs = [dict(item) for item in list((gate.metadata or {}).get("tool_evidence_refs") or []) if isinstance(item, dict)]
    if not refs:
        return
    with self._connect() as db:
        for ref in refs:
            ledger_id = str(ref.get("ledger_id") or "").strip()
            if not ledger_id:
                raise ValueError("review gate tool_evidence_refs require ledger_id")
            row = self._fetch_one(db, "SELECT * FROM minion_worker_ledger WHERE ledger_id = ?", (ledger_id,))
            if row is None:
                raise ValueError(f"unknown review tool evidence ledger_id: {ledger_id}")
            if str(row["event_kind"] or "") != "review_tool_evidence":
                raise ValueError(f"ledger_id is not review_tool_evidence: {ledger_id}")


def _review_gate_coverage_tokens(gate: ReviewGateResult) -> set[str]:
    tokens: set[str] = set()
    for item in [*gate.evidence, *gate.commands_run, *gate.api_evidence]:
        tokens.update(_coverage_tokens_from_item(item))
    return tokens


def _coverage_tokens_from_item(item: dict[str, Any]) -> set[str]:
    if not isinstance(item, dict):
        return set()
    raw_values: list[Any] = []
    for key in ("covers", "coverage", "acceptance_criteria", "acceptance_criteria_refs", "acceptance_refs"):
        if key in item:
            raw_values.append(item.get(key))
    if isinstance(item.get("coverage"), dict):
        raw_values.extend(item["coverage"].values())
    result: set[str] = set()
    for value in raw_values:
        if isinstance(value, str):
            result.add(_coverage_token(value))
        elif isinstance(value, (int, float)):
            result.add(str(int(value)))
        elif isinstance(value, dict):
            for nested in value.values():
                if isinstance(nested, (list, tuple, set)):
                    result.update(_coverage_token(item) for item in nested)
                else:
                    result.add(_coverage_token(nested))
        elif isinstance(value, (list, tuple, set)):
            result.update(_coverage_token(item) for item in value)
    return {item for item in result if item}


def _coverage_token(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _resolve_runtime_file_ref(ref: dict[str, Any], *, runtime_root: Path) -> Path:
    raw = str(ref.get("path") or ref.get("artifact_path") or ref.get("relative_path") or ref.get("ref") or "").strip()
    if not raw:
        raise ValueError("external_verification_ref artifact path is required")
    root = Path(runtime_root).resolve()
    path = Path(raw)
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("external_verification_ref artifact path must be under runtime_root") from exc
    if not resolved.exists() or not resolved.is_file():
        raise ValueError(f"external_verification_ref artifact path does not exist: {resolved}")
    return resolved


def _validate_human_override(value: Any) -> dict[str, Any] | None:
    if value in (None, "", False):
        return None
    if not isinstance(value, dict):
        raise ValueError("human_override must be an object with reason")
    payload = dict(value or {})
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        raise ValueError("human_override.reason is required")
    if str(payload.get("source") or "").strip() != "control_action":
        raise ValueError("human_override.source must be control_action")
    if not str(payload.get("action_kind") or "").strip():
        raise ValueError("human_override.action_kind is required")
    return {
        "reason": reason,
        "actor": str(payload.get("actor") or payload.get("by") or "human").strip() or "human",
        "source": "control_action",
        "action_kind": str(payload.get("action_kind") or "").strip(),
        "target_scope": str(payload.get("target_scope") or "").strip(),
        "target_id": str(payload.get("target_id") or "").strip(),
        "interaction_origin": str(payload.get("interaction_origin") or "").strip(),
        "interaction_id": str(payload.get("interaction_id") or "").strip(),
        "interaction_kind": str(payload.get("interaction_kind") or "").strip(),
        "route": dict(payload.get("route") or {}) if isinstance(payload.get("route"), dict) else {},
        "created_at": utc_now(),
    }


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


def _compile_fts_queries(text: str) -> list[tuple[str, float]]:
    return compile_jieba_fts_queries(text)
