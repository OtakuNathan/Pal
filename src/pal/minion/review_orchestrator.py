from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pal.foundation import utc_now
from pal.minion.contracts import SERIAL_MILESTONE_MODES
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

    @property
    def runtime_root(self) -> Path:
        return self.manager.runtime_root

    @property
    def repository(self) -> MinionTaskingRepository:
        return self.manager.tasking_repository

    def schedule_plan_review(self, state: MinionRunState, event: dict[str, Any]) -> None:
        payload = dict(event.get("payload") or {})
        if str(payload.get("status") or "").strip().lower() != "completed":
            return
        plan_ref = payload.get("plan_ref")
        if not isinstance(plan_ref, dict):
            return
        plan_validation = dict(payload.get("plan_validation") or {})
        if str(plan_validation.get("status") or "").strip().lower() not in {"valid", "ok"}:
            return
        profile_text = " ".join(
            [
                str(state.pack.minion_profile or ""),
                str((state.pack.resolved_profile or {}).get("canonical_profile_id") or ""),
                str((state.pack.resolved_profile or {}).get("profile_id") or ""),
            ]
        ).lower()
        if "planner" not in profile_text:
            return
        metadata = dict(state.pack.metadata or {})
        plan_review_policy = dict(metadata.get("plan_review") or {})
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
        loop.create_task(
            self._spawn_plan_reviewer(state, event, dict(plan_ref), review_key),
            name=f"minion-plan-review-{safe_token(review_key)}",
        )

    async def _spawn_plan_reviewer(
        self,
        planner_state: MinionRunState,
        event: dict[str, Any],
        plan_ref: dict[str, Any],
        review_key: str,
    ) -> None:
        try:
            payload = dict(event.get("payload") or {})
            workspace = dict(planner_state.pack.workspace or {})
            repo_path = str(workspace.get("repo_path") or workspace.get("source_repo") or "").strip()
            artifact_dir = str(workspace.get("artifact_dir") or "").strip()
            review_target = {
                "plan_ref": dict(plan_ref),
                "plan_validation": dict(payload.get("plan_validation") or {}),
                "planner_work_order_id": planner_state.pack.work_order_id,
                "planner_run_id": planner_state.run_id,
                "planner_minion_id": planner_state.minion_id,
                "repo_path": repo_path,
                "artifact_dir": artifact_dir,
                "summary": str(payload.get("summary") or ""),
            }
            review_work_order_id = f"wo_plan_review_{safe_token(review_key)}"
            review_scratch = prepare_review_scratch(self.runtime_root, review_work_order_id, repo_path=repo_path)
            review_target.update(review_scratch)
            reviewer_order = ReviewerWorkOrder(
                work_order_id=review_work_order_id,
                task_id=f"review_plan_{safe_token(review_key)}",
                review_target=review_target,
                acceptance_criteria=[
                    "Verify the plan is dispatchable and topology/module ordering is valid.",
                    "Verify referenced files, modules, and claimed APIs with source, LSP, docs, build, or explicit not-applicable evidence.",
                    "Verify the test strategy is executable for the repo and each milestone has concrete acceptance criteria.",
                    "Submit op_minion_review_gate_submit with gate_kind=plan_acceptance and target.plan_ref.",
                ],
                allowed_capabilities=[],
                output_contract={"must_submit": "op_minion_review_gate_submit"},
                metadata={
                    "workspace": {
                        "repo_path": repo_path,
                        **review_scratch,
                        "workspace_policy": {"mode": "read_only_repo"},
                    }
                },
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
            }
            if isinstance((planner_state.pack.metadata or {}).get("control_route"), dict):
                metadata["control_route"] = dict((planner_state.pack.metadata or {}).get("control_route") or {})
            if isinstance((planner_state.pack.metadata or {}).get("plan_review"), dict):
                metadata["plan_review"] = dict((planner_state.pack.metadata or {}).get("plan_review") or {})
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
                    },
                    "profile_group": "software_engineering",
                    "profile_name": "reviewer",
                    "metadata": metadata,
                }
            )
            pack = MinionProfileRegistry(runtime_root=self.runtime_root).resolve_pack(pack)
            await self.manager.spawn(pack.to_dict())
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
        payload = dict(event.get("payload") or {})
        if str(payload.get("status") or "").strip().lower() != "claimed":
            return
        metadata = dict(state.pack.metadata or {})
        module_execution = dict(metadata.get("module_execution") or {})
        review_policy = dict(module_execution.get("checkpoint_review") or metadata.get("checkpoint_review") or {})
        if review_policy.get("enabled") is not True:
            return
        checkpoint_id = str(payload.get("checkpoint_id") or "").strip()
        if not checkpoint_id or not self.checkpoint_reviews.claim(checkpoint_id):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.checkpoint_reviews.release(checkpoint_id)
            return
        loop.create_task(self._spawn_checkpoint_reviewer(state, event, checkpoint_id), name=f"minion-review-{checkpoint_id}")

    async def _spawn_checkpoint_reviewer(self, coder_state: MinionRunState, event: dict[str, Any], checkpoint_id: str) -> None:
        try:
            payload = dict(event.get("payload") or {})
            workspace = dict(coder_state.pack.workspace or {})
            repo_path = str(workspace.get("repo_path") or "").strip()
            review_gate_kind = self._checkpoint_review_gate_kind(coder_state, payload)
            review_target = {
                "checkpoint_id": checkpoint_id,
                "gate_kind": review_gate_kind,
                "work_order_id": coder_state.pack.work_order_id,
                "run_id": coder_state.run_id,
                "minion_id": coder_state.minion_id,
                "module_id": str(payload.get("module_id") or ""),
                "milestone_id": str(payload.get("milestone_id") or ""),
                "milestone_index": payload.get("milestone_index"),
                "acceptance_criteria": [str(item) for item in list(payload.get("acceptance_criteria") or [])],
                "commit_sha": str(payload.get("commit_sha") or ""),
                "repo_path": repo_path,
                "summary": str(payload.get("summary") or ""),
            }
            review_policy = dict((coder_state.pack.metadata.get("module_execution") or {}).get("checkpoint_review") or coder_state.pack.metadata.get("checkpoint_review") or {})
            reviewer_group = str(review_policy.get("reviewer_profile_group") or "software_engineering").strip() or "software_engineering"
            reviewer_name = str(review_policy.get("reviewer_profile_name") or "reviewer").strip() or "reviewer"
            review_work_order_id = f"wo_review_{safe_token(checkpoint_id)}"
            review_scratch = prepare_review_scratch(self.runtime_root, review_work_order_id, repo_path=repo_path)
            review_target.update(review_scratch)
            reviewer_order = ReviewerWorkOrder(
                work_order_id=review_work_order_id,
                task_id=f"review_{safe_token(checkpoint_id)}",
                review_target=review_target,
                acceptance_criteria=[
                    "Verify the checkpoint matches the milestone contract.",
                    "Run or inspect relevant tests when possible.",
                    "Verify claimed APIs with source, LSP, docs, build, or explicit not-verified findings.",
                    f"Submit op_minion_review_gate_submit with gate_kind={review_gate_kind}.",
                ],
                allowed_capabilities=[],
                output_contract={"must_submit": "op_minion_review_gate_submit"},
                metadata={
                    "workspace": {
                        "repo_path": repo_path,
                        **review_scratch,
                        "workspace_policy": {"mode": "read_only_repo"},
                    }
                },
            )
            metadata = {
                "task_id": reviewer_order.task_id,
                "task_title": f"Review checkpoint {checkpoint_id}",
                "work_order_title": f"Review checkpoint {checkpoint_id}",
                "review_target": review_target,
                "reviewer_work_order": reviewer_order.to_dict(),
                "prompt_view": prompt_view_for_reviewer(reviewer_order),
                "milestones": ["Review checkpoint and submit gate"],
                "checkpoint_review_for_run_id": coder_state.run_id,
                "checkpoint_review_for_work_order_id": coder_state.pack.work_order_id,
            }
            if isinstance((coder_state.pack.metadata or {}).get("control_route"), dict):
                metadata["control_route"] = dict((coder_state.pack.metadata or {}).get("control_route") or {})
            pack = TaskContextPack.from_dict(
                {
                    "work_order_id": review_work_order_id,
                    "goal": f"Review checkpoint {checkpoint_id}",
                    "instruction": (
                        "Review the referenced milestone checkpoint. Do not modify the coder workspace. "
                        f"You must submit a structured gate through op_minion_review_gate_submit with gate_kind={review_gate_kind} before completing."
                    ),
                    "workspace": {
                        "repo_path": repo_path,
                        **review_scratch,
                        "workspace_policy": {"mode": "read_only_repo"},
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
                loop.create_task(
                    self._reconcile_plan_review(state, dict(plan_ref), review_key),
                    name=f"minion-plan-review-reconcile-{safe_token(review_key)}",
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
        loop.create_task(self._reconcile_checkpoint_review(state, checkpoint_id, coder_run_id), name=f"minion-review-reconcile-{checkpoint_id}")

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
            await self.manager.spawn(pack.to_dict())
            return {
                "status": "spawned",
                "work_order_id": pack.work_order_id,
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
            if verdict == "fail":
                await self._send_checkpoint_repair_turn(coder_state, gate)
                return
            await self.manager._send_runner_control_or_record(
                coder_state,
                {"type": "blocked", "payload": {"status": "blocked", "summary": gate.get("summary") or "checkpoint review was partial", "review_gate": gate}},
            )
        finally:
            self.checkpoint_reviews.release(checkpoint_id)

    async def _send_checkpoint_repair_turn(self, coder_state: MinionRunState, gate: dict[str, Any]) -> None:
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
        repair_note = review_gate_repair_note(gate)
        module_execution = dict(repair_state.get("module_execution") or {})
        turn = {
            "type": "repair_turn",
            "turn_kind": "milestone_repair",
            "work_order_id": coder_state.pack.work_order_id,
            "goal": coder_state.pack.goal,
            "instruction": f"Repair the current milestone according to reviewer findings.\n\n{repair_note}",
            "acceptance_criteria": list(coder_state.pack.acceptance_criteria),
            "current_milestone": current,
            "prompt_view": dict((coder_state.pack.metadata.get("prompt_view") or {})),
            "metadata_updates": {
                "review_feedback": gate,
                "module_execution": module_execution,
            },
            "workspace_updates": dict(coder_state.pack.workspace or {}),
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
        review_policy = dict(module_execution.get("checkpoint_review") or metadata.get("checkpoint_review") or {})
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
    summary = str(gate.get("summary") or "").strip()
    if summary:
        parts.append(f"Summary: {summary}")
    findings = [dict(item) for item in list(gate.get("findings") or []) if isinstance(item, dict)]
    fixes = [dict(item) for item in list(gate.get("required_fixes") or []) if isinstance(item, dict)]
    if findings:
        rendered = []
        for item in findings[:8]:
            rendered.append(
                "- "
                + str(item.get("severity") or "finding")
                + ": "
                + str(item.get("summary") or item.get("message") or item)
            )
        parts.append("Findings:\n" + "\n".join(rendered))
    if fixes:
        rendered = ["- " + str(item.get("summary") or item.get("fix") or item) for item in fixes[:8]]
        parts.append("Required fixes:\n" + "\n".join(rendered))
    parts.append("After repairing, rerun relevant verification and call op_minion_checkpoint_commit again.")
    return "\n\n".join(parts)


def review_gate_target(gate: dict[str, Any]) -> dict[str, Any]:
    target = gate.get("target")
    return dict(target or {}) if isinstance(target, dict) else {}


def plan_auto_revision_allowed(policy: dict[str, Any], *, spawned_count: int) -> bool:
    if not coerce_bool(policy.get("auto_revise") or policy.get("auto_revise_plan")):
        return False
    max_attempts = max(0, coerce_int(policy.get("max_auto_revision_attempts") or policy.get("max_auto_revisions"), 1))
    return max(0, int(spawned_count or 0)) < max_attempts
