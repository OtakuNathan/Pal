from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.contracts import AggregateSnapshot, AggregateType
from pal.bunshin.human_review import human_review_card_is_current
from pal.bunshin.repository import BunshinRepository


@dataclass
class WorkflowStatusReader:
    repository: BunshinRepository
    artifacts: ContentAddressedArtifactStore
    triage_candidates: Callable[[str], tuple[AggregateSnapshot, ...]]
    render_human_review_markdown: Callable[[AggregateSnapshot], str]

    def workflow_status(self, workflow_id: str, *, view: str = "status") -> dict[str, Any]:
        normalized_view = str(view or "status").strip().lower()
        if normalized_view not in {"status", "human_review"}:
            raise ValueError("workflow status view must be status or human_review")
        projection = self.repository.queries.read_workflow_projection(workflow_id)
        if projection is None:
            return {"status": "not_found", "workflow_id": workflow_id}
        snapshots = self.repository.queries.list_workflow_snapshots(workflow_id)
        active_id = str(projection.get("active_aggregate_id") or "")
        active = next((item for item in snapshots if item.aggregate_id == active_id), None)
        workflow_state = str(projection["workflow_state"])
        active_state = active.state if active is not None else ""
        triage_candidates = self.triage_candidates(workflow_id)
        triage = [
            _public_triage_candidate(item)
            for item in triage_candidates
        ]
        workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        workflow_name = ""
        if workflow is not None:
            workflow_name = str(workflow.payload.get("workflow_name") or "")
            task_id = str(workflow.payload.get("task_id") or "")
            tasks = self.repository.search.search_tasks(task_id=task_id, include_archived=True, limit=1) if task_id else ()
            if not workflow_name:
                workflow_name = str(tasks[0].get("title") or "") if tasks else ""
        waiting_for_user = bool(projection["waiting_for_user"])
        active_worker = str(projection.get("active_worker_id") or "")
        invocation = self.repository.role_invocations.read_role_invocation(active_worker) if active_worker else None
        role_progress = (
            self.repository.queries.read_role_checklist_progress(active_worker)
            if active_worker
            else None
        )
        worker_node = next(
            (
                item
                for item in snapshots
                if invocation is not None
                and item.aggregate_id == str(invocation.get("aggregate_id") or "")
            ),
            None,
        )
        latest_event = self.repository.queries.read_latest_workflow_event(workflow_id)
        result = {
            "status": "ok",
            "workflow_id": workflow_id,
            "current_phase": projection["current_phase"],
            "workflow_state": projection["workflow_state"],
            "archived": bool(workflow is not None and workflow.payload.get("archived")),
            "active_aggregate_type": projection["active_aggregate_type"],
            "active_aggregate_id": active_id,
            "active_node_state": active_state,
            "workflow_name": workflow_name,
            "active_module": str(
                (
                    worker_node.payload
                    if worker_node is not None
                    else active.payload
                    if active is not None
                    else {}
                ).get("module_name")
                or (
                    worker_node.payload
                    if worker_node is not None
                    else active.payload
                    if active is not None
                    else {}
                ).get("unit_id")
                or ""
            ),
            "active_worker": active_worker,
            "active_worker_role": str((invocation or {}).get("role") or ""),
            "active_role_progress": dict(role_progress or {}),
            "blocker": projection["blocker"],
            "next_legal_action": _public_next_actions(
                workflow_state,
                active_state,
                has_triage=bool(triage),
                liveness=str(projection["liveness"] or ""),
                archived=bool(workflow is not None and workflow.payload.get("archived")),
            ),
            "triage": triage,
            "waiting_for_user": waiting_for_user,
            "human_review_available": active_state == "HUMAN_REVIEW",
            "liveness": projection["liveness"],
            "metrics": projection["metrics"],
            "metrics_basis": "completed_role_turns",
            "last_progress_event": dict(latest_event or {}),
            "modules": self._workflow_module_statuses(
                workflow_id=workflow_id,
                workflow=workflow,
            ),
        }
        result_ref = dict((workflow.payload if workflow is not None else {}).get("result_artifact_ref") or {})
        if result_ref.get("sha256"):
            record = self.repository.artifacts.read_artifact_record(str(result_ref["sha256"]))
            if record and str(record.get("artifact_type") or "") == "DeliveryReceiptArtifact":
                result["delivery"] = dict(self.artifacts.read_json(result_ref))
        if normalized_view == "human_review":
            if active is None or active.state != "HUMAN_REVIEW":
                raise ValueError("workflow is not waiting for architecture human review")
            result["human_review"] = self._human_review_view(active)
        waits = self.repository.interaction_waits.pending(workflow_id=workflow_id)
        if waits:
            questions = [w["request"] for w in waits if w["kind"] == "clarification"]
            result.update(pending_question_count=len(questions), pending_approval_count=len(waits) - len(questions))
            if questions:
                result["pending_question"] = pending_clarification_status(questions[0])
                if workflow_state == "ACTIVE":
                    result["next_legal_action"] = ["answer_question", "control_workflow:cancel"]
        return result

    def task_status(
        self,
        task_id: str,
        *,
        workflow_id: str = "",
        view: str = "status",
    ) -> dict[str, Any]:
        tasks = self.repository.search.search_tasks(
            task_id=str(task_id),
            include_archived=True,
            limit=1,
        )
        if not tasks:
            return {"status": "not_found"}
        task = dict(tasks[0])
        counts = self.repository.search.task_workflow_counts(task_id, owner=task["owner"])
        workflow_status = None
        if workflow_id:
            workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
            if workflow is None or workflow.payload.get("task_id") != task_id:
                workflow_status = {"status": "not_found", "workflow_id": workflow_id}
            else:
                workflow_status = self.workflow_status(workflow_id, view=view)
        return {
            "status": "ok",
            "task": {
                "workflow_count": sum(counts.values()),
                "workflow_counts_by_state": counts,
                "name": str(task.get("title") or ""),
                "state": str(task.get("state") or ""),
                "objective": str(task.get("objective") or ""),
                "profile": str(task.get("profile_id") or ""),
                "family": str(task.get("family_id") or ""),
                "workspace": str(task.get("workspace_key") or ""),
                "updated_at": str(task.get("updated_at") or ""),
            },
            "workflow": workflow_status,
        }

    def _workflow_module_statuses(
        self,
        *,
        workflow_id: str,
        workflow: AggregateSnapshot | None,
    ) -> list[dict[str, Any]]:
        nodes = list(self.repository.queries.list_workflow_node_projections(workflow_id))
        if not nodes:
            return []
        current_epoch = str(
            (workflow.payload if workflow is not None else {}).get("execution_epoch_id")
            or ""
        )
        if current_epoch:
            current_nodes = [
                item for item in nodes if str(item.get("epoch_id") or "") == current_epoch
            ]
            if current_nodes:
                nodes = current_nodes
        else:
            latest_epoch = str(nodes[-1].get("epoch_id") or "")
            if latest_epoch:
                nodes = [
                    item for item in nodes if str(item.get("epoch_id") or "") == latest_epoch
                ]

        node_names = {
            str(item.get("node_run_id") or ""): str(item.get("unit_id") or "")
            for item in nodes
        }
        invocations = self.repository.queries.list_workflow_role_invocations(workflow_id)
        latest_roles: dict[tuple[str, str], dict[str, Any]] = {}
        for invocation in invocations:
            key = (
                str(invocation.get("aggregate_id") or ""),
                str(invocation.get("role") or ""),
            )
            latest_roles[key] = dict(invocation)

        result: list[dict[str, Any]] = []
        for node in nodes:
            node_id = str(node.get("node_run_id") or "")
            roles: dict[str, Any] = {}
            for (aggregate_id, role), invocation in latest_roles.items():
                if aggregate_id != node_id:
                    continue
                roles[role] = {
                    "status": str(invocation.get("status") or ""),
                    "turns": int(invocation.get("last_completed_turn") or 0),
                    "wall_time_ms": int(invocation.get("total_wall_latency_ms") or 0),
                }
            result.append(
                {
                    "module": str(node.get("unit_id") or ""),
                    "kind": str(node.get("node_kind") or ""),
                    "state": str(node.get("state") or ""),
                    "dependencies": [
                        node_names[str(dependency)]
                        for dependency in list(node.get("dependency_node_ids") or [])
                        if node_names.get(str(dependency))
                    ],
                    "candidate_available": bool(node.get("candidate_digest")),
                    "blocker": dict(node.get("blocker") or {}),
                    "roles": roles,
                    "updated_at": str(node.get("updated_at") or ""),
                }
            )
        return result

    def _human_review_view(self, revision: AggregateSnapshot) -> dict[str, Any]:
        manifest_ref = dict(revision.payload.get("architecture_manifest_ref") or {})
        if not manifest_ref:
            raise ValueError("human review has no architecture manifest")
        card_ref = dict(revision.payload.get("human_review_card_ref") or {})
        if not card_ref:
            card_ref = dict(
                self.repository.artifacts.read_latest_effect_result_artifact(
                    workflow_id=revision.workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=revision.aggregate_id,
                    effect_type="publish_architecture_review_request",
                )
                or {}
            )
        card: dict[str, Any] = {}
        if card_ref:
            record = self.repository.artifacts.read_artifact_record(str(card_ref.get("sha256") or ""))
            if record and str(record.get("artifact_type") or "") == "HumanReviewCardArtifact":
                candidate = dict(self.artifacts.read_json(card_ref))
                if human_review_card_is_current(
                    candidate,
                    manifest_sha=str(manifest_ref.get("sha256") or ""),
                ):
                    card = candidate
        if card:
            markdown = str(card.get("markdown") or "")
            actions = [str(item) for item in list(card.get("actions") or [])]
        else:
            markdown = self.render_human_review_markdown(revision)
            actions = ["accept", "edit", "reject"]
        review_ref = dict(revision.payload.get("review_artifact_ref") or {})
        review = dict(self.artifacts.read_json(review_ref)) if review_ref else {}
        return {
            "markdown": markdown,
            "actions": actions,
            "review_verdict": str(review.get("verdict") or ""),
            "findings": list(review.get("findings") or []),
        }


def _public_next_actions(
    workflow_state: str,
    active_state: str,
    *,
    has_triage: bool = False,
    liveness: str = "",
    archived: bool = False,
) -> list[str]:
    if archived:
        return []
    if workflow_state in {"COMPLETED", "REJECTED", "CANCELLED"}:
        return ["archive_workflow"]
    if workflow_state == "RESTARTING":
        return ["wait_for_replacement_workflow", "control_workflow:cancel"]
    if workflow_state == "PAUSED":
        return ["resume_workflow", "control_workflow:cancel"]
    if workflow_state in {"PAUSE_REQUESTED", "CANCEL_REQUESTED"}:
        return ["wait_for_safe_point"]
    if (
        has_triage
        or workflow_state == "TRIAGE_REQUIRED"
        or active_state == "TRIAGE_REQUIRED"
    ):
        return ["resolve_triage", "control_workflow:cancel"]
    if str(liveness or "").strip() == "orphaned":
        return ["resume_workflow", "control_workflow:cancel"]
    if active_state == "HUMAN_REVIEW":
        return ["submit_human_decision", "control_workflow:cancel"]
    return ["control_workflow:pause", "control_workflow:cancel"]


def _public_triage_candidate(snapshot: AggregateSnapshot) -> dict[str, Any]:
    blocker = dict(snapshot.payload.get("blocker") or {})
    subject = _triage_subject(snapshot)
    return {
        "subject": subject,
        "kind": snapshot.aggregate_type.value.replace("_", " "),
        "blocker": blocker,
        "resume_state": str(snapshot.payload.get("triage_resume_state") or ""),
        "recovery": {
            "action": "resolve_triage",
            "arguments": {"subject": subject},
            "requires": ["resolution"],
        },
    }


def _triage_subject(snapshot: AggregateSnapshot) -> str:
    payload = dict(snapshot.payload or {})
    if snapshot.aggregate_type == AggregateType.DAG_NODE_RUN:
        module_name = str(payload.get("module_name") or "").strip()
        if module_name:
            return f"module:{module_name}"
        verification_name = str(payload.get("verification_name") or "").strip()
        if verification_name:
            return f"verification:{verification_name}"
        unit_id = str(payload.get("unit_id") or "").strip()
        return f"unit:{unit_id or 'DAG node'}"
    return {
        AggregateType.WORKFLOW: "phase:workflow",
        AggregateType.ARCHITECTURE_REVISION: "phase:architecture",
        AggregateType.EXECUTION_EPOCH: "phase:execution",
        AggregateType.STANDALONE_REVIEW: "review:standalone",
        AggregateType.TASK: "phase:task",
    }.get(snapshot.aggregate_type, f"aggregate:{snapshot.aggregate_type.value}")


def pending_clarification_status(payload: Mapping[str, Any]) -> dict[str, Any]:
    questions = [
        dict(item)
        for item in list(payload.get("questions") or [])
        if isinstance(item, Mapping)
    ]
    question = questions[0] if questions else {}
    options = [
        {
            "label": str(option.get("label") or option.get("answer") or "").strip(),
            "description": str(
                option.get("description")
                or option.get("label")
                or option.get("answer")
                or ""
            ).strip(),
        }
        for option in list(question.get("options") or [])
        if isinstance(option, Mapping)
    ]
    return {
        "title": str(
            question.get("title") or payload.get("title") or "Architecture question"
        ).strip(),
        "question": str(
            question.get("question")
            or payload.get("question")
            or payload.get("summary")
            or "Bunshin needs clarification."
        ).strip(),
        "options": options[:3],
    }
