"""Stable system Task admission for scheduled occurrences."""
from __future__ import annotations
from typing import Any, Mapping, TYPE_CHECKING
from pal.bunshin.contracts import AggregateType

if TYPE_CHECKING:
    from pal.bunshin.service import BunshinWorkflowService


# This per-occurrence contract also applies to already-created system Tasks,
# whose profile binding is intentionally frozen.
SCHEDULED_DELIVERY_CONTRACT = (
    "Address result_text to the result_recipient in the bound work view. For channel, submit the exact final "
    "user-facing message itself, in the requested language and format, not a report about doing the task. "
    "For resident, submit the useful model-neutral findings and outcome for Pal to use in a later conversation. "
    "Do not include progress reports, process steps, verification evidence, file inventories, internal notes, "
    "or information not intended for the recipient (such as a private quiz answer key). "
    "Keep private working files, state, answer keys, backups and checks in the managed Git workspace; "
    "they are not channel attachments. Use next_steps only for genuinely pending follow-up that Resident needs "
    "after the user returns; these are retained in the internal result, not appended to the scheduled message. "
    "Leave deliverable_paths empty unless a durable file artifact is needed for later retrieval; "
    "scheduled channel delivery sends only result_text."
)


def start_scheduled_run(service: BunshinWorkflowService, data: Mapping[str, Any]) -> dict[str, Any]:
    """System-task dispatch is idempotent across scheduler and Manager restarts."""
    import hashlib
    from pal.bunshin.role_protocol import stable_hash
    fingerprint = stable_hash(dict(data))
    run_id = str(data["run_id"])
    workflow_id = "wf_proactive_" + hashlib.sha256(run_id.encode()).hexdigest()
    task_id = "__PROACTIVE__"
    existing = service.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
    if existing is not None:
        if existing.payload.get("scheduled_run_id") != run_id or (existing.payload.get("scheduled_request_fingerprint") and existing.payload["scheduled_request_fingerprint"] != fingerprint):
            raise ValueError("scheduled workflow identity conflict")
        return {"workflow_id": workflow_id, "state": existing.state}
    task = service.repository.snapshots.read_snapshot(AggregateType.TASK, task_id)
    if task is not None and service.repository.queries.has_nonterminal_workflows_for_task(task_id):
        return {"status": "busy"}
    route = dict(data.get("delivery_binding") or {"channel_id": "null", "channel_kind": "null", "reply_target": {"sink": "null"}})
    if task is None:
        service.create_task({"task_id": task_id, "title": task_id, "objective": "Scheduled background tasks",
                          "profile": "general.proactive_worker", "workspace": {"kind": "managed_git"},
                          "actor": "pal", "delivery_binding": route, "policies": {"memory_policy": "disabled"}})
    archive_snapshot = ""
    if "conversation-history" in (str(data["goal"]) + str(data.get("method", ""))):
        from pal.bunshin.scheduled_inputs import capture_conversation_archive
        archive_snapshot = capture_conversation_archive(service.runtime_root, workflow_id)
    try:
        return service.start_workflow({"task_id": task_id, "workflow_id": workflow_id,
            "actor": "pal", "source_channel": "proactive", "execution_mode": "direct",
            "goal": str(data["goal"]), "task_spec": {"goal": data["goal"], "method": data.get("method", ""),
                "delivery_contract": SCHEDULED_DELIVERY_CONTRACT},
            "skill_refs": list(data.get("skill_refs") or []), "research_mode": "external_allowed",
            "scheduled_run_id": run_id, "scheduled_request_fingerprint": fingerprint, "scheduled_task_name": str(data.get("task_name") or "__PROACTIVE__"), "delivery_binding": route,
            "channel_delivery_requested": bool(data.get("delivery_binding")),
            "discard_output": bool(data.get("discard_output")), "scheduled_archive_snapshot": archive_snapshot,
            "memory_policy": "disabled"})
    except ValueError:
        existing = service.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        if existing is not None and existing.payload.get("scheduled_request_fingerprint") == fingerprint:
            return {"workflow_id": workflow_id, "state": existing.state}
        if existing is None and service.repository.queries.has_nonterminal_workflows_for_task(task_id):
            return {"status": "busy"}
        raise
