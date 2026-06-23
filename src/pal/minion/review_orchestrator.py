from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pal.foundation import utc_now
from pal.minion.checklist import build_acceptance_checklist, compact_checklist, repair_acceptance_refs
from pal.minion.contracts import SERIAL_MILESTONE_MODES
from pal.minion.gates import (
    GATE_TRIGGER_AFTER_EACH_MILESTONE,
    GateSpec,
    checkpoint_gate_spec_for_pack,
    checkpoint_review_policy_from_spec,
    default_gate_strategy_registry,
    gate_specs_from_pack,
    plan_gate_spec_for_pack,
    plan_review_policy_from_spec,
    project_active_gate_todo,
)
from pal.minion.inflight import InflightTracker
from pal.minion.profiles import MinionProfileRegistry
from pal.minion.repository import MinionTaskingRepository
from pal.minion.turns import apply_minion_turn_to_pack
from pal.minion.utils import coerce_bool, coerce_int, safe_token
from pal.minion.work_order import ReviewerWorkOrder, prompt_view_for_reviewer
from pal.shared import TaskContextPack

if TYPE_CHECKING:
    from pal.minion.manager import MinionManager, MinionRunState


def plan_review_key(plan_ref: dict[str, Any]) -> str:
    plan_id = str(plan_ref.get("plan_id") or "").strip()
    task_id = str(plan_ref.get("task_id") or "").strip()
    revision = str(plan_ref.get("plan_revision") if plan_ref.get("plan_revision") is not None else "").strip()
    sha = str(plan_ref.get("sha256") or "").strip()
    path = str(plan_ref.get("path") or "").strip()
    return ":".join(part for part in (task_id, plan_id, revision, sha or path) if part)


@dataclass
class ReviewOrchestrator:
    manager: MinionManager
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("pal.minion.review"))
    plan_reviews: InflightTracker = field(default_factory=InflightTracker)
    checkpoint_reviews: InflightTracker = field(default_factory=InflightTracker)
    gate_registry: Any = field(default_factory=default_gate_strategy_registry)
    background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)

    @property
    def runtime_root(self) -> Path:
        return self.manager.runtime_root

    @property
    def repository(self) -> MinionTaskingRepository:
        return self.manager.tasking_repository

    def schedule_plan_review(self, state: MinionRunState, event: dict[str, Any]) -> None:
        self._schedule_plan_review(state, event, plan_gate_spec_for_pack(state.pack))

    def schedule_checkpoint_review(self, state: MinionRunState, event: dict[str, Any]) -> None:
        self._schedule_checkpoint_review(state, event, checkpoint_gate_spec_for_pack(state.pack))

    def schedule_event_gates(self, state: MinionRunState, event: dict[str, Any]) -> None:
        for spec in gate_specs_from_pack(state.pack, trigger=GATE_TRIGGER_AFTER_EACH_MILESTONE):
            if self.gate_registry.get(spec.strategy) is None:
                continue
            if spec.target_kind == "plan_artifact":
                self._schedule_plan_review(state, event, spec)
            elif spec.target_kind == "checkpoint":
                self._schedule_checkpoint_review(state, event, spec)

    def _schedule_plan_review(self, state: MinionRunState, event: dict[str, Any], spec: GateSpec | None) -> None:
        if spec is None:
            return
        payload = dict(event.get("payload") or {})
        if str(payload.get("status") or "").strip().lower() != "completed":
            return
        plan_ref = payload.get("plan_ref")
        if not isinstance(plan_ref, dict):
            return
        plan_validation = dict(payload.get("plan_validation") or {})
        if str(plan_validation.get("status") or "").strip().lower() not in {"valid", "ok"}:
            return
        metadata = dict(state.pack.metadata or {})
        plan_review_policy = plan_review_policy_from_spec(spec, metadata)
        if plan_review_policy.get("enabled") is False:
            return
        review_key = plan_review_key(plan_ref)
        if not review_key or not self.plan_reviews.claim(review_key):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.plan_reviews.release(review_key)
            return
        self._track_background_task(
            loop.create_task(
                self._spawn_plan_reviewer(state, event, dict(plan_ref), review_key, spec),
                name=f"minion-plan-review-{safe_token(review_key)}",
            ),
            label=f"plan review {review_key}",
        )

    async def _spawn_plan_reviewer(
        self,
        planner_state: MinionRunState,
        event: dict[str, Any],
        plan_ref: dict[str, Any],
        review_key: str,
        spec: GateSpec,
    ) -> None:
        try:
            payload = dict(event.get("payload") or {})
            plan_review_policy = plan_review_policy_from_spec(spec, dict(planner_state.pack.metadata or {}))
            workspace = dict(planner_state.pack.workspace or {})
            repo_path = str(workspace.get("repo_path") or workspace.get("source_repo") or "").strip()
            artifact_dir = str(workspace.get("artifact_dir") or "").strip()
            source_contract = _source_contract_from_pack(planner_state.pack)
            review_target = {
                "plan_ref": dict(plan_ref),
                "plan_validation": dict(payload.get("plan_validation") or {}),
                "planner_work_order_id": planner_state.pack.work_order_id,
                "planner_run_id": planner_state.run_id,
                "planner_minion_id": planner_state.minion_id,
                "repo_path": repo_path,
                "artifact_dir": artifact_dir,
                "summary": str(payload.get("summary") or ""),
                "gate_spec": spec.to_dict(),
            }
            if source_contract:
                review_target["source_contract"] = source_contract
            review_work_order_id = f"wo_plan_review_{safe_token(review_key)}"
            review_scratch = prepare_review_scratch(self.runtime_root, review_work_order_id, repo_path=repo_path)
            review_target.update(review_scratch)
            acceptance_criteria = list(spec.required_checks) or [
                "Verify the plan is dispatchable and topology/module ordering is valid.",
                "Verify referenced files, modules, and claimed APIs with source, LSP, docs, build, or explicit not-applicable evidence.",
                "Verify the test strategy is executable for the repo and each milestone has concrete acceptance criteria.",
                "Submit op_minion_review_gate_submit with gate_kind=plan_acceptance and target.plan_ref.",
            ]
            if source_contract:
                acceptance_criteria.insert(
                    0,
                    (
                        "Verify the plan satisfies the planner source_contract exactly, including explicit names, "
                        "counts, file boundaries, hard requirements, and acceptance criteria from the original work order."
                    ),
                )
            review_workspace = {
                "repo_path": repo_path,
                **review_scratch,
                "workspace_policy": {"mode": "read_only_repo"},
            }
            if source_contract:
                review_workspace["review_target_source_contract"] = dict(source_contract)
            reviewer_order = ReviewerWorkOrder(
                work_order_id=review_work_order_id,
                task_id=f"review_plan_{safe_token(review_key)}",
                review_target=review_target,
                acceptance_criteria=acceptance_criteria,
                allowed_capabilities=[],
                output_contract={"must_submit": "op_minion_review_gate_submit"},
                metadata={"workspace": review_workspace},
            )
            metadata = {
                "task_id": reviewer_order.task_id,
                "task_title": f"Review plan {plan_ref.get('plan_id') or review_key}",
                "work_order_title": f"Review plan {plan_ref.get('plan_id') or review_key}",
                "review_target": review_target,
                "reviewer_work_order": reviewer_order.to_dict(),
                "prompt_view": prompt_view_for_reviewer(reviewer_order),
                "milestones": ["Review plan and submit gate"],
                "plan_review_for_run_id": planner_state.run_id,
                "plan_review_for_work_order_id": planner_state.pack.work_order_id,
                "plan_review_key": review_key,
                "gate_spec": spec.to_dict(),
                "plan_review": plan_review_policy,
            }
            if isinstance((planner_state.pack.metadata or {}).get("control_route"), dict):
                metadata["control_route"] = dict((planner_state.pack.metadata or {}).get("control_route") or {})
            if isinstance((planner_state.pack.metadata or {}).get("plan_review"), dict):
                metadata["plan_review"] = {
                    **plan_review_policy,
                    **dict((planner_state.pack.metadata or {}).get("plan_review") or {}),
                }
            pack = TaskContextPack.from_dict(
                {
                    "work_order_id": review_work_order_id,
                    "goal": f"Review plan {plan_ref.get('plan_id') or review_key}",
                    "instruction": (
                        "Review the referenced planner FinalPlanArtifact. Do not modify the source repository. "
                        "You may create temporary probes only under /tmp, $TMPDIR, or your isolated minion artifact workspace. "
                        "You must submit a structured gate through op_minion_review_gate_submit before completing."
                    ),
                    "workspace": {
                        "repo_path": repo_path,
                        "artifact_dir": artifact_dir,
                        **review_scratch,
                        "workspace_policy": {"mode": "read_only_repo"},
                        "review_target_plan_ref": dict(plan_ref),
                        "review_source_work_order_id": planner_state.pack.work_order_id,
                        "review_target_gate_kind": spec.gate_kind or "plan_acceptance",
                        **({"review_target_source_contract": dict(source_contract)} if source_contract else {}),
                    },
                    "profile_group": str(plan_review_policy.get("reviewer_profile_group") or spec.reviewer_profile_group or "software_engineering"),
                    "profile_name": str(plan_review_policy.get("reviewer_profile_name") or spec.reviewer_profile_name or "reviewer"),
                    "metadata": metadata,
                }
            )
            pack = MinionProfileRegistry(runtime_root=self.runtime_root).resolve_pack(pack)
            spawned = await self.manager.spawn(pack.to_dict())
            self.manager._record_event(
                planner_state,
                {
                    "event_kind": "plan_review_started",
                    "payload": {
                        "status": "running",
                        "summary": "plan reviewer spawned",
                        "plan_ref": dict(plan_ref),
                        "review_key": review_key,
                        "review_work_order_id": review_work_order_id,
                        "review_run_id": str(spawned.get("run_id") or ""),
                        "reviewer_profile": str(spawned.get("minion_profile") or pack.minion_profile),
                    },
                    "created_at": utc_now(),
                },
            )
        except Exception:
            self.logger.exception("failed to spawn plan reviewer: %s", review_key)
            self.manager._record_event(
                planner_state,
                {
                    "event_kind": "plan_review_failed",
                    "payload": {
                        "status": "failed",
                        "summary": "manager failed to spawn plan reviewer",
                        "plan_ref": plan_ref,
                    },
                    "created_at": utc_now(),
                },
            )
            self.plan_reviews.release(review_key)

    def schedule_checkpoint_review(self, state: MinionRunState, event: dict[str, Any]) -> None:
        self._schedule_checkpoint_review(state, event, checkpoint_gate_spec_for_pack(state.pack))

    def _schedule_checkpoint_review(self, state: MinionRunState, event: dict[str, Any], spec: GateSpec | None) -> None:
        if spec is None:
            return
        payload = dict(event.get("payload") or {})
        if str(payload.get("status") or "").strip().lower() != "claimed":
            return
        checkpoint_id = str(payload.get("checkpoint_id") or "").strip()
        if not checkpoint_id or not self.checkpoint_reviews.claim(checkpoint_id):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.checkpoint_reviews.release(checkpoint_id)
            return
        self._track_background_task(
            loop.create_task(self._spawn_checkpoint_reviewer(state, event, checkpoint_id, spec), name=f"minion-review-{checkpoint_id}"),
            label=f"checkpoint review {checkpoint_id}",
        )

    async def _spawn_checkpoint_reviewer(
        self,
        coder_state: MinionRunState,
        event: dict[str, Any],
        checkpoint_id: str,
        spec: GateSpec,
    ) -> None:
        try:
            payload = dict(event.get("payload") or {})
            workspace = dict(coder_state.pack.workspace or {})
            repo_path = str(workspace.get("repo_path") or "").strip()
            metadata = dict(coder_state.pack.metadata or {})
            module_execution = dict(metadata.get("module_execution") or {})
            review_policy = checkpoint_review_policy_from_spec(
                spec,
                module_mode=str(module_execution.get("mode") or ""),
            )
            review_gate_kind = self._checkpoint_review_gate_kind(coder_state, payload)
            source_contract = _source_contract_from_pack(coder_state.pack)
            current_acceptance = _checkpoint_review_acceptance_criteria(payload, source_contract)
            review_target = {
                "checkpoint_id": checkpoint_id,
                "gate_kind": review_gate_kind,
                "work_order_id": coder_state.pack.work_order_id,
                "run_id": coder_state.run_id,
                "minion_id": coder_state.minion_id,
                "module_id": str(payload.get("module_id") or ""),
                "milestone_id": str(payload.get("milestone_id") or ""),
                "milestone_index": payload.get("milestone_index"),
                "acceptance_criteria": current_acceptance,
                "commit_sha": str(payload.get("commit_sha") or ""),
                "repo_path": repo_path,
                "summary": str(payload.get("summary") or ""),
                "gate_spec": spec.to_dict(),
            }
            if source_contract:
                review_target["source_contract"] = source_contract
                deferred_acceptance = _deferred_acceptance_criteria(source_contract, current_acceptance)
                if deferred_acceptance:
                    review_target["deferred_acceptance_criteria"] = deferred_acceptance
            acceptance_checklist = build_acceptance_checklist(review_target.get("acceptance_criteria"))
            if acceptance_checklist:
                review_target["acceptance_checklist"] = compact_checklist(acceptance_checklist)
            checkpoint_git_context = _checkpoint_git_context(repo_path, str(payload.get("commit_sha") or ""))
            if checkpoint_git_context:
                review_target["checkpoint_git"] = checkpoint_git_context
            reviewer_group = str(review_policy.get("reviewer_profile_group") or "software_engineering").strip() or "software_engineering"
            reviewer_name = str(review_policy.get("reviewer_profile_name") or "reviewer").strip() or "reviewer"
            review_work_order_id = f"wo_review_{safe_token(checkpoint_id)}"
            review_scratch = prepare_review_scratch(self.runtime_root, review_work_order_id, repo_path=repo_path)
            review_target.update(review_scratch)
            environment_workspace = _checkpoint_review_environment_workspace(workspace)
            reviewer_workspace = {
                "repo_path": repo_path,
                **review_scratch,
                **environment_workspace,
                "workspace_policy": {"mode": "read_only_repo"},
            }
            if acceptance_checklist:
                reviewer_workspace["review_target_acceptance_checklist"] = compact_checklist(acceptance_checklist)
            reviewer_order = ReviewerWorkOrder(
                work_order_id=review_work_order_id,
                task_id=f"review_{safe_token(checkpoint_id)}",
                review_target=review_target,
                acceptance_criteria=list(spec.required_checks) or [
                    "Verify the checkpoint matches the milestone contract.",
                    "Run or inspect relevant tests when possible.",
                    "Verify claimed APIs with source, LSP, docs, build, or explicit not-verified findings.",
                    f"Submit op_minion_review_checkpoint; Pal will bind gate_kind={review_gate_kind} from the review target.",
                ],
                allowed_capabilities=[],
                output_contract={"must_submit": "op_minion_review_checkpoint"},
                metadata={"workspace": reviewer_workspace},
            )
            prompt_view = prompt_view_for_reviewer(reviewer_order)
            if acceptance_checklist:
                prompt_view["checklist_projection"] = compact_checklist(acceptance_checklist)
            metadata = {
                "task_id": reviewer_order.task_id,
                "task_title": f"Review checkpoint {checkpoint_id}",
                "work_order_title": f"Review checkpoint {checkpoint_id}",
                "review_target": review_target,
                "reviewer_work_order": reviewer_order.to_dict(),
                "prompt_view": prompt_view,
                "milestones": ["Review checkpoint and submit gate"],
                "checkpoint_review_for_run_id": coder_state.run_id,
                "checkpoint_review_for_work_order_id": coder_state.pack.work_order_id,
                "gate_spec": spec.to_dict(),
                "checkpoint_review": review_policy,
            }
            if acceptance_checklist:
                metadata["checklist_projection"] = compact_checklist(acceptance_checklist)
            if isinstance((coder_state.pack.metadata or {}).get("control_route"), dict):
                metadata["control_route"] = dict((coder_state.pack.metadata or {}).get("control_route") or {})
            pack = TaskContextPack.from_dict(
                {
                    "work_order_id": review_work_order_id,
                    "goal": f"Review checkpoint {checkpoint_id}",
                    "instruction": (
                        "Review the referenced milestone checkpoint. Do not modify the coder workspace. "
                        f"You must submit a structured gate through op_minion_review_checkpoint before completing; the review target gate kind is {review_gate_kind}."
                    ),
                    "workspace": {
                        **reviewer_workspace,
                        "review_target_gate_spec": spec.to_dict(),
                    },
                    "profile_group": reviewer_group,
                    "profile_name": reviewer_name,
                    "metadata": metadata,
                }
            )
            pack = MinionProfileRegistry(runtime_root=self.runtime_root).resolve_pack(pack)
            await self.manager.spawn(pack.to_dict())
        except Exception:
            self.logger.exception("failed to spawn checkpoint reviewer: %s", checkpoint_id)
            try:
                await self.manager._send_runner_control_or_record(
                    coder_state,
                    {
                        "type": "blocked",
                        "payload": {
                            "status": "blocked",
                            "summary": "manager failed to spawn checkpoint reviewer",
                            "checkpoint_id": checkpoint_id,
                        },
                    },
                )
            finally:
                self.checkpoint_reviews.release(checkpoint_id)

    def _checkpoint_review_gate_kind(self, coder_state: MinionRunState, payload: dict[str, Any]) -> str:
        expected = str(payload.get("expected_review_gate_kind") or "").strip().lower()
        if expected in {"checkpoint_verification", "repair_verification"}:
            return expected
        if isinstance(payload.get("repair_attempt"), dict):
            return "repair_verification"
        metadata = dict(coder_state.pack.metadata or {})
        module_execution = dict(metadata.get("module_execution") or {})
        last = dict(module_execution.get("last_repair_attempt") or {})
        if last:
            milestone_index = coerce_int(payload.get("milestone_index"), coerce_int(module_execution.get("current_milestone_index"), 0))
            if coerce_int(last.get("milestone_index"), -1) == milestone_index:
                return "repair_verification"
        return "checkpoint_verification"

    def schedule_reviewer_terminal_reconciliation(self, state: MinionRunState, event: dict[str, Any]) -> None:
        metadata = dict(state.pack.metadata or {})
        review_target = dict(metadata.get("review_target") or {})
        plan_ref = review_target.get("plan_ref")
        if isinstance(plan_ref, dict):
            review_key = str(metadata.get("plan_review_key") or plan_review_key(plan_ref)).strip()
            if review_key:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    return
                self._track_background_task(
                    loop.create_task(
                        self._reconcile_plan_review(state, dict(plan_ref), review_key),
                        name=f"minion-plan-review-reconcile-{safe_token(review_key)}",
                    ),
                    label=f"plan review reconcile {review_key}",
                )
                return
        checkpoint_id = str(review_target.get("checkpoint_id") or "").strip()
        coder_run_id = str(review_target.get("run_id") or metadata.get("checkpoint_review_for_run_id") or "").strip()
        if not checkpoint_id or not coder_run_id:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._track_background_task(
            loop.create_task(self._reconcile_checkpoint_review(state, checkpoint_id, coder_run_id), name=f"minion-review-reconcile-{checkpoint_id}"),
            label=f"checkpoint review reconcile {checkpoint_id}",
        )

    def _track_background_task(self, task: asyncio.Task[Any], *, label: str) -> None:
        self.background_tasks.add(task)

        def _done(completed: asyncio.Task[Any]) -> None:
            self.background_tasks.discard(completed)
            try:
                completed.result()
            except asyncio.CancelledError:
                self.logger.debug("minion review background task cancelled: %s", label)
            except Exception:
                self.logger.exception("minion review background task failed: %s", label)

        task.add_done_callback(_done)

    def _record_work_order_event(
        self,
        *,
        work_order_id: str,
        event_kind: str,
        payload: dict[str, Any],
        minion_id: str = "",
        run_id: str = "",
        minion_profile: str = "",
    ) -> None:
        normalized = str(work_order_id or "").strip()
        if not normalized:
            return
        event = {
            "event_kind": event_kind,
            "minion_id": str(minion_id or ""),
            "run_id": str(run_id or ""),
            "work_order_id": normalized,
            "minion_profile": str(minion_profile or ""),
            "payload": dict(payload or {}),
            "created_at": utc_now(),
        }
        self.manager._queue_event_delivery(event)
        self.repository.record_minion_event(event)

    def _merge_plan_review_state(self, work_order_id: str, payload: dict[str, Any]) -> None:
        if not str(work_order_id or "").strip():
            return
        self.repository.merge_work_order_metadata(work_order_id, {"plan_review": dict(payload or {})})

    async def _reconcile_plan_review(self, reviewer_state: MinionRunState, plan_ref: dict[str, Any], review_key: str) -> None:
        source_work_order_id = ""
        try:
            latest = self.repository.latest_review_gate_for_plan_ref(plan_ref)
            metadata = dict(reviewer_state.pack.metadata or {})
            review_target = dict(metadata.get("review_target") or {})
            source_work_order_id = str(metadata.get("plan_review_for_work_order_id") or review_target.get("planner_work_order_id") or "")
            if latest.get("status") != "ok":
                self._merge_plan_review_state(
                    source_work_order_id,
                    {
                        "status": "gate_missing",
                        "plan_ref": dict(plan_ref),
                        "summary": "reviewer finished without submitting a plan_acceptance gate",
                        "updated_at": utc_now(),
                    },
                )
                event = {
                    "event_kind": "plan_review_failed",
                    "minion_id": reviewer_state.minion_id,
                    "run_id": reviewer_state.run_id,
                    "work_order_id": source_work_order_id or reviewer_state.pack.work_order_id,
                    "minion_profile": reviewer_state.pack.minion_profile,
                    "payload": {
                        "status": "failed",
                        "summary": "reviewer finished without submitting a plan_acceptance gate",
                        "plan_ref": dict(plan_ref),
                    },
                    "created_at": utc_now(),
                }
                self.manager._queue_event_delivery(event)
                self.repository.record_minion_event(event)
                return
            gate = dict(latest.get("review_gate") or {})
            verdict = str(gate.get("verdict") or "").strip().lower()
            event_kind = {
                "pass": "plan_review_passed",
                "fail": "plan_review_failed",
                "partial": "plan_review_partial",
            }.get(verdict, "plan_review_failed")
            payload = {
                "status": verdict or "failed",
                "summary": gate.get("summary") or f"plan review {verdict or 'failed'}",
                "plan_ref": dict(plan_ref),
                "review_gate": gate,
                "review_gate_ref": dict(latest.get("review_gate_ref") or {}),
            }
            review_state = {
                "status": {
                    "pass": "acceptance_pending",
                    "fail": "revision_required",
                    "partial": "human_decision_required",
                }.get(verdict, "failed"),
                "plan_ref": dict(plan_ref),
                "review_gate_ref": dict(latest.get("review_gate_ref") or {}),
                "review_gate": gate,
                "updated_at": utc_now(),
                "next_action": {
                    "pass": "accept_plan",
                    "fail": "revise_plan",
                    "partial": "human_decision",
                }.get(verdict, "inspect_review"),
            }
            plan_review_policy = dict(metadata.get("plan_review") or {})
            self._merge_plan_review_state(source_work_order_id, review_state)
            event = {
                "event_kind": event_kind,
                "minion_id": reviewer_state.minion_id,
                "run_id": reviewer_state.run_id,
                "work_order_id": source_work_order_id or reviewer_state.pack.work_order_id,
                "minion_profile": reviewer_state.pack.minion_profile,
                "payload": payload,
                "created_at": utc_now(),
            }
            self.manager._queue_event_delivery(event)
            self.repository.record_minion_event(event)
            if verdict == "pass" and review_state.get("status") == "acceptance_pending":
                self._record_work_order_event(
                    work_order_id=source_work_order_id,
                    event_kind="plan_acceptance_pending",
                    minion_id=reviewer_state.minion_id,
                    run_id=reviewer_state.run_id,
                    minion_profile=reviewer_state.pack.minion_profile,
                    payload={
                        "status": "pending",
                        "summary": "plan review passed; op_minion_accept_plan or explicit policy is required before dispatch",
                        "plan_ref": dict(plan_ref),
                        "review_gate_ref": dict(latest.get("review_gate_ref") or {}),
                    },
                )
            elif verdict == "fail":
                auto_revision_spawned: dict[str, Any] = {}
                if plan_auto_revision_allowed(
                    plan_review_policy,
                    spawned_count=self.repository.count_ledger_events(source_work_order_id, "plan_revision_spawned"),
                ):
                    auto_revision_spawned = await self._spawn_plan_revision_from_gate(
                        source_work_order_id=source_work_order_id,
                        reviewer_state=reviewer_state,
                        plan_ref=plan_ref,
                        review_gate_ref=dict(latest.get("review_gate_ref") or {}),
                        plan_review_policy=plan_review_policy,
                    )
                    if auto_revision_spawned.get("status") == "spawned":
                        review_state["status"] = "revision_spawned"
                        review_state["revision_spawn"] = dict(auto_revision_spawned)
                        self._merge_plan_review_state(source_work_order_id, review_state)
                self._record_work_order_event(
                    work_order_id=source_work_order_id,
                    event_kind="plan_revision_spawned" if auto_revision_spawned.get("status") == "spawned" else "plan_revision_required",
                    minion_id=reviewer_state.minion_id,
                    run_id=reviewer_state.run_id,
                    minion_profile=reviewer_state.pack.minion_profile,
                    payload={
                        "status": "revision_spawned" if auto_revision_spawned.get("status") == "spawned" else "revision_required",
                        "summary": (
                            "plan reviewer requested revision and manager spawned a revision planner"
                            if auto_revision_spawned.get("status") == "spawned"
                            else gate.get("summary") or "plan reviewer requested revision"
                        ),
                        "source_plan_ref": dict(plan_ref),
                        "review_gate_ref": dict(latest.get("review_gate_ref") or {}),
                        "review_gate": gate,
                        "next_action": "wait_for_revision" if auto_revision_spawned.get("status") == "spawned" else "revise_plan",
                        "auto_revision": dict(auto_revision_spawned),
                    },
                )
            elif verdict == "partial":
                self._record_work_order_event(
                    work_order_id=source_work_order_id,
                    event_kind="plan_review_human_decision_required",
                    minion_id=reviewer_state.minion_id,
                    run_id=reviewer_state.run_id,
                    minion_profile=reviewer_state.pack.minion_profile,
                    payload={
                        "status": "human_decision_required",
                        "summary": gate.get("summary") or "plan review was partial",
                        "source_plan_ref": dict(plan_ref),
                        "review_gate_ref": dict(latest.get("review_gate_ref") or {}),
                        "review_gate": gate,
                        "next_action": "human_decision",
                    },
                )
        except Exception as exc:
            self.logger.exception("failed to reconcile plan review: %s", review_key)
            payload = {
                "status": "failed",
                "summary": "manager failed to reconcile plan review",
                "plan_ref": dict(plan_ref),
                "review_key": review_key,
                "error": f"{exc.__class__.__name__}: {exc}",
            }
            if source_work_order_id:
                self._record_work_order_event(
                    work_order_id=source_work_order_id,
                    event_kind="plan_review_reconcile_failed",
                    minion_id=reviewer_state.minion_id,
                    run_id=reviewer_state.run_id,
                    minion_profile=reviewer_state.pack.minion_profile,
                    payload=payload,
                )
                self._merge_plan_review_state(
                    source_work_order_id,
                    {
                        "status": "reconcile_failed",
                        "summary": payload["summary"],
                        "plan_ref": dict(plan_ref),
                        "error": payload["error"],
                        "updated_at": utc_now(),
                    },
                )
            else:
                event = {
                    "event_kind": "plan_review_reconcile_failed",
                    "minion_id": reviewer_state.minion_id,
                    "run_id": reviewer_state.run_id,
                    "work_order_id": reviewer_state.pack.work_order_id,
                    "minion_profile": reviewer_state.pack.minion_profile,
                    "payload": payload,
                    "created_at": utc_now(),
                }
                self.manager._queue_event_delivery(event)
                self.repository.record_minion_event(event)
        finally:
            self.plan_reviews.release(review_key)

    async def _spawn_plan_revision_from_gate(
        self,
        *,
        source_work_order_id: str,
        reviewer_state: MinionRunState,
        plan_ref: dict[str, Any],
        review_gate_ref: dict[str, Any],
        plan_review_policy: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            attempt = self.repository.count_ledger_events(source_work_order_id, "plan_revision_spawned") + 1
            metadata = {
                "plan_review": {
                    **dict(plan_review_policy),
                    "auto_revision_attempt": attempt,
                    "source_work_order_id": source_work_order_id,
                }
            }
            if isinstance((reviewer_state.pack.metadata or {}).get("control_route"), dict):
                metadata["control_route"] = dict((reviewer_state.pack.metadata or {}).get("control_route") or {})
            pack = self.repository.build_planner_revision_pack_from_review_gate(
                review_gate_ref,
                metadata=metadata,
                workspace={
                    key: value
                    for key, value in {
                        "repo_path": (reviewer_state.pack.workspace or {}).get("repo_path"),
                        "source_repo": (reviewer_state.pack.workspace or {}).get("source_repo"),
                        "artifact_dir": (reviewer_state.pack.workspace or {}).get("artifact_dir"),
                    }.items()
                    if str(value or "").strip()
                },
            )
            pack = MinionProfileRegistry(runtime_root=self.runtime_root).resolve_pack(pack)
            spawned = await self.manager.spawn(pack.to_dict())
            return {
                "status": "spawned",
                "work_order_id": pack.work_order_id,
                "run_id": str(spawned.get("run_id") or ""),
                "minion_id": str(spawned.get("minion_id") or ""),
                "task_id": str((pack.metadata or {}).get("task_id") or ""),
                "source_plan_ref": dict(plan_ref),
                "review_gate_ref": dict(review_gate_ref),
                "auto_revision_attempt": attempt,
            }
        except Exception as exc:
            self.logger.exception("failed to spawn plan revision planner")
            self._record_work_order_event(
                work_order_id=source_work_order_id,
                event_kind="plan_revision_spawn_failed",
                minion_id=reviewer_state.minion_id,
                run_id=reviewer_state.run_id,
                minion_profile=reviewer_state.pack.minion_profile,
                payload={
                    "status": "failed",
                    "summary": "manager failed to spawn plan revision planner",
                    "source_plan_ref": dict(plan_ref),
                    "review_gate_ref": dict(review_gate_ref),
                    "error": f"{exc.__class__.__name__}: {exc}",
                },
            )
            return {
                "status": "failed",
                "error": f"{exc.__class__.__name__}: {exc}",
                "source_plan_ref": dict(plan_ref),
                "review_gate_ref": dict(review_gate_ref),
            }

    async def _reconcile_checkpoint_review(self, reviewer_state: MinionRunState, checkpoint_id: str, coder_run_id: str) -> None:
        try:
            latest = self.repository.latest_review_gate_for_checkpoint(checkpoint_id)
            if latest.get("status") != "ok":
                coder_state = self.manager.runs.get(coder_run_id)
                if coder_state is not None:
                    await self.manager._send_runner_control_or_record(
                        coder_state,
                        {"type": "blocked", "payload": {"status": "blocked", "summary": "reviewer finished without submitting a checkpoint gate", "checkpoint_id": checkpoint_id}},
                    )
                return
            gate = dict(latest.get("review_gate") or {})
            coder_state = self.manager.runs.get(coder_run_id)
            if coder_state is None:
                return
            verdict = str(gate.get("verdict") or "").strip().lower()
            if verdict == "pass":
                closure = self.repository.close_checkpoint_from_review_gate(latest.get("review_gate_ref") or gate)
                payload = dict(closure.get("payload") or {})
                if not payload:
                    payload = {
                        "status": "completed",
                        "checkpoint_id": checkpoint_id,
                        **dict(gate.get("target") or {}),
                        "review_gate": gate,
                        "review_gate_ref": dict(latest.get("review_gate_ref") or {}),
                    }
                metadata = dict(coder_state.pack.metadata or {})
                module_execution = dict(metadata.get("module_execution") or {})
                if str(module_execution.get("mode") or "") in SERIAL_MILESTONE_MODES:
                    await self.manager._send_serial_module_turn(
                        coder_state,
                        {"work_order_id": coder_state.pack.work_order_id, "payload": payload},
                        f"{coder_state.pack.work_order_id}:{checkpoint_id}:review_pass",
                    )
                else:
                    await self.manager._send_runner_control_or_record(
                        coder_state,
                        {
                            "type": "complete",
                            "completion": {
                                "status": "completed",
                                "summary": gate.get("summary") or "checkpoint review passed",
                                "checkpoint_id": checkpoint_id,
                                "review_gate": gate,
                                "review_gate_ref": dict(latest.get("review_gate_ref") or {}),
                            },
                        },
                    )
                return
            if _checkpoint_gate_requires_repair(gate):
                await self._send_checkpoint_repair_turn(coder_state, gate)
                return
            await self.manager._send_runner_control_or_record(
                coder_state,
                {"type": "blocked", "payload": {"status": "blocked", "summary": gate.get("summary") or "checkpoint review was partial", "review_gate": gate}},
            )
        finally:
            self.checkpoint_reviews.release(checkpoint_id)

    async def _send_checkpoint_repair_turn(self, coder_state: MinionRunState, gate: dict[str, Any]) -> None:
        unavailable_reason = self.manager.runner_control_unavailable_reason(coder_state)
        if unavailable_reason:
            self.manager.record_runner_control_skipped(
                coder_state,
                {"type": "repair_turn"},
                reason=unavailable_reason,
            )
            return
        current = dict((coder_state.pack.continuity or {}).get("current_milestone") or {})
        if not current:
            current = dict((coder_state.pack.metadata.get("prompt_view") or {}).get("milestone") or {})
        if not current:
            target = review_gate_target(gate)
            milestone_index = coerce_int(target.get("milestone_index"), 0)
            current = {
                "milestone_index": milestone_index,
                "milestone_id": str(target.get("milestone_id") or f"m{milestone_index}"),
                "title": "Repair checkpoint",
                "task": "Repair the checkpoint according to reviewer findings.",
            }
        repair_state = self._claim_checkpoint_repair_attempt(coder_state, gate, current)
        if str(repair_state.get("status") or "") == "blocked":
            payload = {
                "status": "blocked",
                "summary": str(repair_state.get("summary") or "checkpoint review failed too many times"),
                "reason": "repair_attempt_limit_exceeded",
                "review_gate": gate,
                "repair": repair_state,
            }
            self.manager._record_event(
                coder_state,
                {
                    "event_kind": "review_repair_blocked",
                    "payload": payload,
                    "created_at": utc_now(),
                },
            )
            await self.manager._send_runner_control_or_record(coder_state, {"type": "blocked", "payload": payload})
            return
        summary = str(gate.get("summary") or "checkpoint review failed; repair the current milestone").strip()
        repair_payload = _checkpoint_repair_payload(gate, repair_state)
        active_gate_todo = project_active_gate_todo(gate)
        repair_note = review_gate_repair_note(gate)
        repair_payload_json = _json_dumps_compact(repair_payload)
        module_execution = dict(repair_state.get("module_execution") or {})
        turn = {
            "type": "repair_turn",
            "turn_kind": "checkpoint_repair",
            "work_order_id": coder_state.pack.work_order_id,
            "goal": coder_state.pack.goal,
            "instruction": (
                "Repair the current milestone according to the structured reviewer findings below. "
                "Treat repair_checklist as the complete scope. Complete repair_checklist items in order; do not replace them "
                "with your own diagnosis, do not search for review artifacts, and do not broaden scope. When the next "
                "checklist item requires a source/test/doc contract fix, before the first successful workspace edit for this "
                "repair attempt do not call op_lsp_* and do not run broad test/static-check commands. First inspect only the "
                "directly relevant files and make the required edit with op_file_edit, op_file_write, or "
                "op_path_delete. If the checklist item is purely missing verification evidence, run that focused "
                "verification instead of inventing an edit. "
                "After that first repair edit, use LSP when it helps verify types, symbols, or call shapes, and rerun focused "
                "verification. Before calling op_minion_checkpoint_commit, reread source_contract, failed_acceptance_criteria, "
                "and binding_constraints_to_recheck; self-check exact output literals, fallback/default branches, numeric bounds, "
                "declared type/container contracts, and tests that may encode the wrong expectation.\n\n"
                f"{repair_note}\n\n"
                "Structured repair payload:\n"
                f"```json\n{repair_payload_json}\n```"
            ),
            "acceptance_criteria": list(coder_state.pack.acceptance_criteria),
            "current_milestone": current,
            "prompt_view": dict((coder_state.pack.metadata.get("prompt_view") or {})),
            "metadata_updates": {
                "active_gate_todo": active_gate_todo,
                "checkpoint_repair": repair_payload,
                "review_feedback": repair_payload,
                "module_execution": module_execution,
            },
            "workspace_updates": {**dict(coder_state.pack.workspace or {}), "active_gate_todo": active_gate_todo},
        }
        coder_state.pack = apply_minion_turn_to_pack(coder_state.pack, turn, checkpoint_payload={})
        await self.manager._send_runner_control_or_record(coder_state, {"type": "repair_turn", "turn": turn, "summary": summary})

    def _claim_checkpoint_repair_attempt(
        self,
        coder_state: MinionRunState,
        gate: dict[str, Any],
        current_milestone: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = dict(coder_state.pack.metadata or {})
        module_execution = dict(metadata.get("module_execution") or {})
        spec = checkpoint_gate_spec_for_pack(coder_state.pack)
        review_policy = checkpoint_review_policy_from_spec(
            spec,
            module_mode=str(module_execution.get("mode") or ""),
        )
        limit = max(0, min(10, coerce_int(review_policy.get("max_repair_attempts"), 5)))
        target = review_gate_target(gate)
        milestone_index = coerce_int(
            target.get("milestone_index"),
            coerce_int(current_milestone.get("milestone_index"), coerce_int(module_execution.get("current_milestone_index"), 0)),
        )
        milestone_id = str(target.get("milestone_id") or current_milestone.get("milestone_id") or f"m{milestone_index}").strip()
        repair_key = str(milestone_index)
        attempts = {
            str(key): coerce_int(value, 0)
            for key, value in dict(module_execution.get("repair_attempts_by_milestone") or {}).items()
        }
        current_attempts = max(0, attempts.get(repair_key, 0))
        if current_attempts >= limit:
            return {
                "status": "blocked",
                "attempt": current_attempts,
                "max_repair_attempts": limit,
                "milestone_index": milestone_index,
                "milestone_id": milestone_id,
                "summary": f"checkpoint review failed after {current_attempts}/{limit} automatic repair attempts",
                "module_execution": module_execution,
            }
        next_attempt = current_attempts + 1
        attempts[repair_key] = next_attempt
        module_execution["repair_attempts_by_milestone"] = attempts
        module_execution["last_repair_attempt"] = {
            "attempt": next_attempt,
            "max_repair_attempts": limit,
            "milestone_index": milestone_index,
            "milestone_id": milestone_id,
            "failed_checkpoint_id": str(target.get("checkpoint_id") or "").strip(),
            "failed_commit_sha": str(target.get("commit_sha") or "").strip(),
            "review_gate_ref": {
                key: gate.get(key)
                for key in ("gate_id", "gate_kind", "verdict", "target_kind", "target_key")
                if gate.get(key) not in (None, "", [])
            },
            "created_at": utc_now(),
        }
        metadata["module_execution"] = module_execution
        coder_state.pack = TaskContextPack.from_dict({**coder_state.pack.to_dict(), "metadata": metadata})
        self.repository.merge_work_order_metadata(coder_state.pack.work_order_id, {"module_execution": module_execution})
        return {
            "status": "repair_assigned",
            "attempt": next_attempt,
            "max_repair_attempts": limit,
            "milestone_index": milestone_index,
            "milestone_id": milestone_id,
            "module_execution": module_execution,
        }


def _checkpoint_gate_requires_repair(gate: dict[str, Any]) -> bool:
    verdict = str(gate.get("verdict") or "").strip().lower()
    if verdict == "fail":
        return True
    if verdict != "partial":
        return False
    if _has_required_repair_fixes(gate):
        return True
    for finding in list(gate.get("findings") or []):
        if not isinstance(finding, dict):
            continue
        severity = str(finding.get("severity") or "").strip().lower()
        if severity in {"blocker", "critical", "major", "high", "error", "fail", "failure"}:
            return True
        if _has_material_contract_impact(_first_text(finding, "contract_impact", fallback="")):
            return True
    return _has_material_contract_impact(str(gate.get("contract_impact") or ""))


def _has_required_repair_fixes(gate: dict[str, Any]) -> bool:
    for item in list(gate.get("required_fixes") or []):
        if isinstance(item, dict) and any(value not in (None, "", [], {}) for value in item.values()):
            return True
        if isinstance(item, str) and item.strip():
            return True
    return False


def _has_material_contract_impact(value: str) -> bool:
    normalized = re.sub(r"[\s_\-]+", " ", str(value or "").strip().lower())
    if not normalized:
        return False
    return normalized not in {
        "n/a",
        "na",
        "none",
        "no impact",
        "no contract impact",
        "not applicable",
        "not applicable none",
    }


def _checkpoint_repair_payload(gate: dict[str, Any], repair_state: dict[str, Any]) -> dict[str, Any]:
    target = review_gate_target(gate)
    acceptance_checklist = build_acceptance_checklist(target.get("acceptance_criteria"))
    raw_findings = _repair_actionable_findings(gate)
    findings = [_compact_repair_finding(item) for item in raw_findings]
    raw_required_fixes = [dict(item) for item in list(gate.get("required_fixes") or []) if isinstance(item, dict)]
    required_fixes = [
        _compact_required_fix(item)
        for item in raw_required_fixes
    ]
    repair_checklist = _repair_checklist(raw_findings, raw_required_fixes, acceptance_checklist=acceptance_checklist)
    failed_acceptance = _failed_acceptance_criteria(gate, target)
    failed_acceptance_refs = repair_acceptance_refs({"failed_acceptance_criteria": failed_acceptance}, acceptance_checklist)
    payload: dict[str, Any] = {
        "turn_kind": "checkpoint_repair",
        "attempt": repair_state.get("attempt"),
        "max_repair_attempts": repair_state.get("max_repair_attempts"),
        "review_gate_ref": _compact_gate_ref(gate),
        "active_gate_todo": project_active_gate_todo(gate),
        "failed_checkpoint": {
            key: value
            for key, value in {
                "checkpoint_id": target.get("checkpoint_id"),
                "commit_sha": target.get("commit_sha"),
                "milestone_index": target.get("milestone_index"),
                "milestone_id": target.get("milestone_id"),
            }.items()
            if value not in (None, "", [])
        },
        "summary": _compact_repair_text(str(gate.get("summary") or ""), limit=700),
        "repair_checklist": repair_checklist[:8],
        "findings": findings[:8],
        "required_fixes": required_fixes[:8],
        "acceptance_checklist": compact_checklist(acceptance_checklist),
        "failed_acceptance_criteria": failed_acceptance[:12],
        "failed_acceptance_refs": failed_acceptance_refs[:12],
        "binding_constraints_to_recheck": _repair_hard_constraints_from_target(target)[:8],
        "reviewer_evidence_summary": _reviewer_evidence_summary(gate),
        "review_artifact_ref": _compact_artifact_ref(gate.get("report_artifact_ref")),
        "repair_order": [
            "Read repair_checklist plus source_contract/failed_acceptance_criteria/binding_constraints_to_recheck.",
            "For each repair_checklist item in order, inspect only directly relevant files and make the needed source/test/doc edit.",
            "When a checklist item requires a source/test/doc contract fix, do not call op_lsp_* or run broad tests/static checks before the first successful repair edit. Use LSP after the edit when it helps verify types, symbols, or call shapes.",
            "When the only checklist item is missing verification evidence, run the requested focused verification instead of inventing an edit.",
            "Run focused verification only after the checklist edit is made, then checkpoint.",
        ],
        "pre_checkpoint_self_check": [
            "Reread source_contract and failed_acceptance_criteria before op_minion_checkpoint_commit.",
            "Check exact literals and fallback/default branches; `...-or-` contracts require the literal fallback branch unless the contract says otherwise.",
            "Check numeric bounds, declared return/field/container types, and tests that might encode the implementation's wrong expectation.",
        ],
        "repair_instruction": (
            "Complete repair_checklist in order. Make exactly the source/test/doc edits needed by each checklist item. "
            "Before the first successful workspace edit for a contract-fix checklist item, do not use LSP or broad verification as a substitute for fixing the checklist item. "
            "For a verification-only checklist item, run the requested focused verification instead of inventing unrelated edits. "
            "After changing source/tests, rerun focused verification, reread the binding contract fields, then submit a new op_minion_checkpoint_commit."
        ),
    }
    return _drop_empty(payload)


def _json_dumps_compact(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _compact_gate_ref(gate: dict[str, Any]) -> dict[str, Any]:
    return {
        key: gate.get(key)
        for key in ("gate_id", "gate_kind", "verdict", "target_kind", "target_key", "created_at")
        if gate.get(key) not in (None, "", [])
    }


def _repair_actionable_findings(gate: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for item in list(gate.get("findings") or []):
        if not isinstance(item, dict):
            continue
        if _is_noisy_system_detected_line_range(item):
            continue
        findings.append(dict(item))
    return findings


def _is_noisy_system_detected_line_range(item: dict[str, Any]) -> bool:
    if str(item.get("source") or "").strip().lower() != "system_detected":
        return False
    text = " ".join(
        str(item.get(key) or "")
        for key in ("summary", "title", "contract_impact", "criterion")
        if str(item.get(key) or "").strip()
    )
    return bool(re.search(r"(?i)\blines?\s+\d+\s*(?:-|–|—|to)\s*\d+\b", text))


def _repair_checklist(
    findings: list[dict[str, Any]],
    required_fixes: list[dict[str, Any]],
    *,
    acceptance_checklist: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    acceptance_items = list(acceptance_checklist or [])
    items: list[dict[str, Any]] = []
    if required_fixes:
        for index, fix in enumerate(required_fixes[:8], start=1):
            finding = _repair_fix_finding(fix, findings)
            action = _first_text(fix, "description", "summary", "fix", "message", fallback="required repair fix")
            items.append(_repair_checklist_item(index, action=action, finding=finding, source="required_fix", raw_fix=fix, acceptance_checklist=acceptance_items))
        return items
    actionable = [
        item
        for item in findings
        if _first_text(item, "suggested_fix", "fix", fallback="")
        or str(item.get("severity") or "").strip().lower() in {"blocker", "critical", "major", "high", "error", "fail", "failure"}
        or _first_text(item, "contract_impact", fallback="")
    ]
    for index, finding in enumerate(actionable[:8], start=1):
        action = _first_text(finding, "suggested_fix", "fix", fallback="")
        if not action:
            title = _first_text(finding, "title", "summary", "message", "description", fallback="reviewer finding")
            action = f"Address reviewer finding: {title}"
        items.append(_repair_checklist_item(index, action=action, finding=finding, source="finding", raw_fix={}, acceptance_checklist=acceptance_items))
    return items


def _repair_fix_finding(fix: dict[str, Any], findings: list[dict[str, Any]]) -> dict[str, Any]:
    raw_index = fix.get("finding_index")
    if isinstance(raw_index, int) and 0 <= raw_index < len(findings):
        return findings[raw_index]
    if isinstance(raw_index, str) and raw_index.strip().isdigit():
        index = int(raw_index.strip())
        if 0 <= index < len(findings):
            return findings[index]
    if len(findings) == 1:
        return findings[0]
    return {}


def _repair_checklist_item(
    index: int,
    *,
    action: str,
    finding: dict[str, Any],
    source: str,
    raw_fix: dict[str, Any],
    acceptance_checklist: list[dict[str, Any]],
) -> dict[str, Any]:
    acceptance_refs = repair_acceptance_refs({**dict(finding or {}), **dict(raw_fix or {})}, acceptance_checklist)
    verify = _first_text(raw_fix, "verification", "verify", "test", fallback="")
    if not verify:
        title = _first_text(finding, "title", "summary", "message", "description", fallback="")
        verify = f"Run focused verification that proves this fix is complete{': ' + title if title else ''}."
    return _drop_empty(
        {
            "id": f"RC-{index}",
            "kind": "repair",
            "status": "pending",
            "source": source,
            "finding_index": raw_fix.get("finding_index"),
            "acceptance_refs": acceptance_refs,
            "parent_item_id": acceptance_refs[0] if acceptance_refs else "",
            "action": _compact_repair_text(action, limit=420),
            "area": _compact_repair_text(_first_text(finding, "area", "path", fallback=""), limit=220),
            "contract_impact": _compact_repair_text(_first_text(finding, "contract_impact", fallback=""), limit=260),
            "evidence": _compact_repair_text(_first_text(finding, "evidence", fallback=""), limit=260),
            "verify": _compact_repair_text(verify, limit=260),
        }
    )


def _compact_repair_finding(item: dict[str, Any]) -> dict[str, Any]:
    return _drop_empty(
        {
            "severity": _first_text(item, "severity", fallback="finding"),
            "title": _first_text(item, "title", "summary", "message", "description", fallback="reviewer finding"),
            "area": _first_text(item, "area", "path", fallback=""),
            "evidence": _compact_repair_text(_first_text(item, "evidence", fallback=""), limit=520),
            "suggested_fix": _compact_repair_text(_first_text(item, "suggested_fix", "fix", fallback=""), limit=420),
            "contract_impact": _compact_repair_text(_first_text(item, "contract_impact", fallback=""), limit=360),
            "test_gap": _compact_repair_text(_first_text(item, "test_gap", fallback=""), limit=260),
            "failed_acceptance_criteria": _string_items(
                item.get("failed_acceptance_criteria") or item.get("acceptance_criteria") or item.get("covers"),
                limit=6,
                text_limit=220,
            ),
        }
    )


def _compact_required_fix(item: dict[str, Any]) -> dict[str, Any]:
    return _drop_empty(
        {
            "finding_index": item.get("finding_index"),
            "description": _compact_repair_text(
                _first_text(item, "description", "summary", "fix", "message", fallback="required repair fix"),
                limit=420,
            ),
        }
    )


def _failed_acceptance_criteria(gate: dict[str, Any], target: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for item in list(gate.get("findings") or []):
        if not isinstance(item, dict):
            continue
        result.extend(
            _string_items(
                item.get("failed_acceptance_criteria") or item.get("acceptance_criteria") or item.get("covers"),
                limit=8,
                text_limit=260,
            )
        )
    if not result:
        result.extend(_string_items(target.get("acceptance_criteria"), limit=12, text_limit=260))
    seen: set[str] = set()
    deduped: list[str] = []
    for item in result:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _reviewer_evidence_summary(gate: dict[str, Any]) -> dict[str, Any]:
    commands: list[dict[str, Any]] = []
    for item in list(gate.get("commands_run") or [])[:6]:
        if not isinstance(item, dict):
            continue
        commands.append(
            _drop_empty(
                {
                    "command": _compact_repair_text(str(item.get("command") or ""), limit=360),
                    "status": item.get("status") or ("passed" if item.get("exit_code") == 0 else ""),
                    "exit_code": item.get("exit_code"),
                    "output_summary": _compact_repair_text(
                        _first_text(item, "output_summary", "summary", fallback=""),
                        limit=420,
                    ),
                    "covers": _string_items(item.get("covers"), limit=4, text_limit=180),
                }
            )
        )
    api_evidence: list[dict[str, Any]] = []
    for item in list(gate.get("api_evidence") or [])[:6]:
        if not isinstance(item, dict):
            continue
        api_evidence.append(
            _drop_empty(
                {
                    "source": _first_text(item, "source", "path", fallback=""),
                    "summary": _compact_repair_text(
                        _first_text(item, "finding", "summary", "description", fallback=""),
                        limit=360,
                    ),
                    "covers": _string_items(item.get("covers"), limit=4, text_limit=180),
                }
            )
        )
    return _drop_empty({"commands_run": commands, "api_evidence": api_evidence})


def _compact_artifact_ref(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    ref = dict(raw.get("ref") or raw)
    return {
        key: ref.get(key)
        for key in ("path", "relative_path", "sha256", "role", "mime_type", "kind")
        if ref.get(key) not in (None, "", [])
    }


def _string_items(raw: Any, *, limit: int, text_limit: int) -> list[str]:
    if isinstance(raw, str):
        iterable = [raw]
    else:
        iterable = list(raw or [])
    result: list[str] = []
    for item in iterable:
        text = _compact_repair_text(str(item or ""), limit=text_limit)
        if text:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _drop_empty(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in payload.items():
        if value in (None, "", [], {}):
            continue
        result[key] = value
    return result


def _source_contract_from_pack(pack: TaskContextPack) -> dict[str, Any]:
    contract: dict[str, Any] = {}
    goal = str(pack.goal or "").strip()
    instruction = str(pack.instruction or "").strip()
    acceptance = _dedupe_texts(pack.acceptance_criteria)
    current_acceptance = _current_pack_acceptance_criteria(pack)
    if goal:
        contract["goal"] = goal
    if instruction:
        contract["instruction"] = instruction
    if current_acceptance:
        contract["acceptance_criteria"] = current_acceptance
    elif acceptance:
        contract["acceptance_criteria"] = acceptance
    if _dedupe_texts(acceptance, exclude=current_acceptance):
        contract["overall_acceptance_criteria"] = acceptance
    return contract


def _checkpoint_review_acceptance_criteria(payload: dict[str, Any], source_contract: dict[str, Any]) -> list[str]:
    current = _dedupe_texts(payload.get("acceptance_criteria"))
    if current:
        return current
    return _dedupe_texts(source_contract.get("acceptance_criteria") if isinstance(source_contract, dict) else None)


def _current_pack_acceptance_criteria(pack: TaskContextPack) -> list[str]:
    continuity = dict(pack.continuity or {})
    current = continuity.get("current_milestone")
    if isinstance(current, dict):
        for key in ("acceptance_criteria", "acceptance"):
            values = _dedupe_texts(current.get(key))
            if values:
                return values
    metadata = dict(pack.metadata or {})
    prompt_view = dict(metadata.get("prompt_view") or {})
    milestone = prompt_view.get("milestone")
    if isinstance(milestone, dict):
        for key in ("acceptance_criteria", "acceptance"):
            values = _dedupe_texts(milestone.get(key))
            if values:
                return values
    return []


def _deferred_acceptance_criteria(source_contract: dict[str, Any], current_acceptance: list[str]) -> list[str]:
    overall = _dedupe_texts(source_contract.get("overall_acceptance_criteria"))
    if not overall:
        return []
    return _dedupe_texts(overall, exclude=current_acceptance)


def _dedupe_texts(raw: Any, *, exclude: list[str] | None = None) -> list[str]:
    excluded = {str(item or "").strip().lower() for item in list(exclude or []) if str(item or "").strip()}
    result: list[str] = []
    seen: set[str] = set()
    iterable = [raw] if isinstance(raw, str) else list(raw or [])
    for item in iterable:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in excluded or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _checkpoint_git_context(repo_path: str, commit_sha: str) -> dict[str, Any]:
    repo = Path(str(repo_path or "")).expanduser()
    commit = str(commit_sha or "").strip()
    if not commit or not (repo / ".git").exists():
        return {}
    stat = _git_text(repo, "show", "--stat", "--oneline", "--no-renames", "--format=short", commit)
    changed = _git_text(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", commit)
    parent = _git_text(repo, "rev-parse", f"{commit}^")
    context: dict[str, Any] = {
        "commit_sha": commit,
        "changed_files": [line.strip() for line in changed.splitlines() if line.strip()][:200],
    }
    if parent:
        context["parent_commit_sha"] = parent.splitlines()[0].strip()
    if stat:
        context["stat"] = stat[:8000]
    return {key: value for key, value in context.items() if value not in ("", [], {})}


def _git_text(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def prepare_review_scratch(runtime_root: Path, work_order_id: str, *, repo_path: str = "") -> dict[str, str]:
    scratch = review_scratch_dir(runtime_root, work_order_id)
    payload = {"review_scratch_dir": str(scratch)}
    source = Path(str(repo_path or "")).resolve() if str(repo_path or "").strip() else None
    if source is not None and source.exists() and source.is_dir():
        copy_path = scratch / "source"
        if not copy_path.exists():
            shutil.copytree(
                source,
                copy_path,
                ignore=review_scratch_ignore,
            )
        payload["review_scratch_repo_path"] = str(copy_path)
    return payload


def review_scratch_dir(runtime_root: Path, work_order_id: str) -> Path:
    path = Path(runtime_root) / "data" / "minion" / "review_scratch" / safe_token(work_order_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def review_scratch_ignore(directory: str, names: list[str]) -> set[str]:
    ignored = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    root = Path(directory)
    for name in names:
        if name in ignored:
            continue
        try:
            if (root / name).is_symlink():
                ignored.add(name)
        except OSError:
            ignored.add(name)
    return ignored.intersection(set(names))


def review_gate_repair_note(gate: dict[str, Any]) -> str:
    parts = [f"Reviewer verdict: {str(gate.get('verdict') or 'fail')}"]
    target = review_gate_target(gate)
    acceptance_checklist = build_acceptance_checklist(target.get("acceptance_criteria"))
    failed_commit_sha = str(target.get("commit_sha") or "").strip()
    summary = str(gate.get("summary") or "").strip()
    if summary:
        parts.append(f"Summary: {summary}")
    hard_constraints = _repair_hard_constraints_from_target(target)
    if hard_constraints:
        parts.append(
            "Binding source-contract constraints to re-check before checkpoint:\n"
            + "\n".join(f"- {item}" for item in hard_constraints[:8])
        )
    findings = _repair_actionable_findings(gate)
    fixes = [dict(item) for item in list(gate.get("required_fixes") or []) if isinstance(item, dict)]
    checklist = _repair_checklist(findings, fixes, acceptance_checklist=acceptance_checklist)
    if checklist:
        parts.append("Repair checklist:\n" + "\n".join(_render_repair_checklist_item(item) for item in checklist))
    if findings:
        rendered = [_render_repair_finding(item) for item in findings[:8]]
        parts.append("Findings:\n" + "\n".join(rendered))
    if fixes:
        rendered = [_render_required_repair_fix(item) for item in fixes[:8]]
        parts.append("Required fixes:\n" + "\n".join(rendered))
    parts.append(
        "Repair scope control:\n"
        "- Address only the required fixes above; do not broaden scope or add polish work.\n"
        "- Complete every repair checklist item before doing any other work.\n"
        "- Do not perform performance tuning, cleanup, or optional refactors unless a checklist item explicitly asks for it.\n"
        "- Do not add new tests, files, or features unless a required fix explicitly needs them.\n"
        "- If a required fix says to reduce a count or satisfy a numeric range, remove/merge items until the count is inside the range; do not add more items in that category.\n"
        "- Before checkpointing, run a direct local check for every numeric/file/test bound you can verify and include that evidence in the checkpoint summary."
    )
    repair_rules = [
        "Change the workspace so the reviewer finding is actually addressed.",
        "Do not use a verification-only artifact as the repair; minion_outputs is excluded from checkpoint commits.",
        "Rerun relevant verification before submitting a new checkpoint.",
        "Call op_minion_checkpoint_commit only after the workspace has a repair change.",
        "If no source, test, or README change is needed, stop as blocked and explain why instead of claiming the repair is complete.",
    ]
    if failed_commit_sha:
        repair_rules.append(f"Do not resubmit the failed checkpoint commit {failed_commit_sha}.")
    parts.append("Repair checkpoint rules:\n" + "\n".join(f"- {item}" for item in repair_rules))
    return "\n\n".join(parts)


def _render_repair_checklist_item(item: dict[str, Any]) -> str:
    pieces = [f"- [ ] {str(item.get('id') or 'RC-?')}: {_compact_repair_text(str(item.get('action') or ''), limit=320)}"]
    area = str(item.get("area") or "").strip()
    verify = str(item.get("verify") or "").strip()
    if area:
        pieces.append(f"area={area}")
    if verify:
        pieces.append(f"verify={verify}")
    return "; ".join(pieces)


def _render_repair_finding(item: dict[str, Any]) -> str:
    severity = str(item.get("severity") or "finding").strip()
    title = _first_text(
        item,
        "summary",
        "message",
        "title",
        "description",
        fallback="reviewer finding",
    )
    area = _first_text(item, "area", "path", fallback="")
    suggested_fix = _first_text(item, "suggested_fix", "fix", fallback="")
    evidence = _first_text(item, "evidence", fallback="")
    pieces = [f"- {severity}: {title}"]
    if area:
        pieces.append(f"area={area}")
    if suggested_fix:
        pieces.append(f"fix={suggested_fix}")
    if evidence:
        pieces.append(f"evidence={_compact_repair_text(evidence, limit=320)}")
    return "; ".join(pieces)


def _render_required_repair_fix(item: dict[str, Any]) -> str:
    description = _first_text(
        item,
        "description",
        "summary",
        "fix",
        "message",
        fallback="required repair fix",
    )
    finding_index = item.get("finding_index")
    prefix = "- "
    if finding_index is not None:
        prefix = f"- finding_index={finding_index}: "
    return prefix + _compact_repair_text(description, limit=420)


def _first_text(payload: dict[str, Any], *keys: str, fallback: str = "") -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (dict, list)):
            continue
        text = str(value or "").strip()
        if text:
            return text
    return fallback


_REPAIR_NUMERIC_BOUND_RE = re.compile(
    r"(?i)\b(?:"
    r"\d+\s*(?:-|–|—|to)\s*\d+|"
    r"at\s+most\s+\d+|no\s+more\s+than\s+\d+|"
    r"at\s+least\s+\d+|no\s+fewer\s+than\s+\d+|"
    r"exactly\s+\d+|"
    r"\d+\s+(?:files?|tests?|commands?|checks?|milestones?|modules?)"
    r")\b"
)
_REPAIR_BOUND_CONTEXT_RE = re.compile(
    r"(?i)\b(?:test|pytest|file|changed file|commit|command|check|bound|range|limit|must|required|hard requirement|acceptance)\b"
)


def _repair_hard_constraints_from_target(target: dict[str, Any]) -> list[str]:
    source_contract = dict(target.get("source_contract") or {}) if isinstance(target.get("source_contract"), dict) else {}
    clauses: list[str] = []
    for key in ("goal", "instruction", "summary", "task"):
        clauses.extend(_numeric_constraint_clauses(str(source_contract.get(key) or "")))
    for key in ("acceptance_criteria", "criteria", "overall_acceptance_criteria"):
        for item in list(source_contract.get(key) or []):
            clauses.extend(_numeric_constraint_clauses(str(item or "")))
    seen: set[str] = set()
    result: list[str] = []
    for clause in clauses:
        normalized = " ".join(clause.split())
        if not normalized or normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        result.append(normalized)
    return result


def _numeric_constraint_clauses(text: str) -> list[str]:
    raw = str(text or "")
    if not raw.strip():
        return []
    candidates: list[str] = []
    for line in raw.splitlines():
        cleaned = line.strip(" -\t")
        if cleaned:
            candidates.append(cleaned)
    for sentence in re.split(r"(?<=[.!?。！？])\s+", raw):
        cleaned = sentence.strip(" -\t")
        if cleaned:
            candidates.append(cleaned)
    result: list[str] = []
    for candidate in candidates:
        if _REPAIR_NUMERIC_BOUND_RE.search(candidate) and _REPAIR_BOUND_CONTEXT_RE.search(candidate):
            result.append(_compact_repair_text(candidate, limit=360))
    return result


def _compact_repair_text(text: str, *, limit: int = 260) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"


def review_gate_target(gate: dict[str, Any]) -> dict[str, Any]:
    target = gate.get("target")
    return dict(target or {}) if isinstance(target, dict) else {}


def _checkpoint_review_environment_workspace(workspace: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    languages = workspace.get("languages")
    if isinstance(languages, list):
        normalized = [str(item).strip() for item in languages if str(item).strip()]
        if normalized:
            result["languages"] = normalized
    lsp_setup = workspace.get("lsp_setup")
    if isinstance(lsp_setup, dict):
        safe = {
            key: value
            for key, value in dict(lsp_setup).items()
            if key in {"languages", "servers", "created_files", "skipped", "baseline_commit_sha"}
            and isinstance(value, (str, int, float, bool, list, tuple))
        }
        if safe:
            result["lsp_setup"] = {key: list(value) if isinstance(value, tuple) else value for key, value in safe.items()}
    return result


def plan_auto_revision_allowed(policy: dict[str, Any], *, spawned_count: int) -> bool:
    if not coerce_bool(policy.get("auto_revise") or policy.get("auto_revise_plan")):
        return False
    max_attempts = max(0, coerce_int(policy.get("max_auto_revision_attempts") or policy.get("max_auto_revisions"), 1))
    return max(0, int(spawned_count or 0)) < max_attempts
