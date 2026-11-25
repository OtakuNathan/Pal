"""Bind a suspended worker, its encrypted checkpoint and its Task notification."""
from __future__ import annotations

from typing import Any, Mapping

from pal.foundation import utc_now
from pal.bunshin.contracts import AggregateType
from pal.bunshin.paths import role_run_id
from pal.bunshin.unit_of_work import BunshinUnitOfWork
from pal.bunshin.service import BunshinWorkflowService


def interaction_command(service: BunshinWorkflowService, method: str, auth: Mapping[str, Any],
                        payload: Mapping[str, Any], *, access_token: str) -> dict[str, Any]:
    repository = service.repository
    assignment = auth["assignment"]
    assignment_id = str(assignment["assignment_id"])
    attempt_id = str(auth["attempt_id"])
    wait_id = str(payload["wait_id"])
    workflow_id = str(assignment["workflow_id"])
    session_id = str(assignment["session_id"])
    if method == "interaction_resume":
        wait = repository.interaction_waits.get(wait_id)
        if wait is None or wait["assignment_id"] != assignment_id or wait["state"] != "answered":
            raise ValueError("interaction has no answer available for this assignment")
        revision = None
        if (wait["kind"] == "clarification" and assignment["role"] == "architect"
                and assignment["aggregate_type"] == AggregateType.ARCHITECTURE_REVISION.value):
            question = wait["request"]["questions"][0]
            answer = wait["answer"]["clarification"]["answers"][0]
            revision = service.append_architect_clarification({
                "workflow_id": workflow_id, "architecture_revision_id": assignment["aggregate_id"],
                "worker_id": session_id, "clarification_id": wait_id,
                "title": question.get("title") or wait["request"].get("title") or "Architecture clarification",
                "question": question["question"], "answer": answer["answer"],
            })
        with repository.transaction() as work:
            work.role_access.authenticate_role_attempt(access_token)
            workflow = work.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
            if workflow is None or workflow.state != "ACTIVE":
                raise ValueError("interaction workflow is no longer active")
            answer = work.interaction_waits.begin_resume(wait_id, assignment_id, attempt_id)
        if revision:
            answer["clarification"]["task_revision"] = revision
        return {"answer": answer}
    with repository.transaction() as work:
        previous_wait_id = str(payload.get("previous_wait_id") or "")
        if method == "interaction_consumed" or previous_wait_id:
            work.interaction_waits.consume(
                wait_id if method == "interaction_consumed" else previous_wait_id,
                assignment_id, attempt_id,
            )
        reply: dict[str, Any] = {"ok": True}
        if method == "interaction_park":
            delivery = _park(work, assignment, attempt_id, wait_id, payload)
            if delivery:
                reply["delivery"] = delivery
        work.role_attempts.publish_initial_role_checkpoint(
            assignment_id=assignment_id, attempt_id_value=attempt_id,
            fencing_token=int(auth["fencing_token"]), expected_sequence=int(payload["sequence"]),
            allow_submitted=method == "interaction_consumed",
            parked_wait_id=wait_id if method == "interaction_park" else None,
        )
        return reply


def _park(work: BunshinUnitOfWork, assignment: Mapping[str, Any], attempt_id: str,
          wait_id: str, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    assignment_id = str(assignment["assignment_id"])
    workflow_id = str(assignment["workflow_id"])
    session_id = str(assignment["session_id"])
    kind = str(payload["kind"])
    if kind not in {"approval", "clarification"} or not wait_id.startswith(kind + "_"):
        raise ValueError("invalid role interaction kind or ID")
    workflow = work.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
    if workflow is None or workflow.state != "ACTIVE":
        raise ValueError("interaction workflow is no longer active")
    request = dict(payload["request"])
    for key in ("route", "control_route", "source_dependencies"):
        request.pop(key, None)
    run_id = role_run_id(session_id)
    request.update({kind + "_id": wait_id, "workflow_id": workflow_id, "run_id": run_id})
    work.interaction_waits.open(wait_id=wait_id, assignment_id=assignment_id, attempt_id=attempt_id,
                                kind=kind, request=request)
    task_id = workflow.payload["task_id"]
    event_kind = kind + "_requested"
    if work.interaction_waits.get(wait_id)["state"] == "open":
        event = {"event_kind": event_kind, "run_id": run_id, "invocation_id": session_id,
                 "bunshin_id": str(request.get("bunshin_id") or session_id),
                 "workflow_id": workflow_id, "task_id": task_id, "payload": request,
                 "created_at": str(assignment["created_at"])}
        delivery = work.deliveries.enqueue_task_delivery(task_id=task_id, workflow_id=workflow_id,
            event_kind=event_kind, payload=event, dedup_key="role-interaction:" + wait_id)
        return delivery
    return None


def record_clarification_answer(
    service: BunshinWorkflowService, clarification_id: str, answer: dict[str, Any],
) -> dict[str, Any] | None:
    """Settle the question and its frontend notification in the same commit."""
    delivery = None
    with service.repository.transaction() as work:
        current_wait = work.interaction_waits.get(clarification_id)
        work.interaction_waits.answer(clarification_id, {"clarification": answer})
        if current_wait["state"] == "open":
            request = current_wait["request"]
            workflow_id = request["workflow_id"]
            workflow = work.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
            task_id = workflow.payload["task_id"]
            event = {
                "event_kind": "clarification_resolved", "workflow_id": workflow_id, "task_id": task_id,
                "run_id": request["run_id"], "bunshin_id": request.get("bunshin_id", ""),
                "payload": {"bunshin": True, "clarification_id": clarification_id,
                            "summary": "Bunshin clarification recorded."},
                "created_at": utc_now(),
            }
            delivery = work.deliveries.enqueue_task_delivery(
                task_id=task_id, workflow_id=workflow_id, event_kind=event["event_kind"],
                payload=event, dedup_key=f"clarification-resolved:{workflow_id}:{clarification_id}",
            )
    return delivery
