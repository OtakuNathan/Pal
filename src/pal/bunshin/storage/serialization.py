from __future__ import annotations
import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Mapping
from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType, DispatchResult, DomainEvent
from pal.bunshin.cycle_protocol import NodeCycle, PlanCycle


def _snapshot_from_row(row: sqlite3.Row) -> AggregateSnapshot:
    return AggregateSnapshot(
        aggregate_type=AggregateType(str(row["aggregate_type"])),
        aggregate_id=str(row["aggregate_id"]),
        workflow_id=str(row["workflow_id"]),
        state=str(row["state"]),
        version=int(row["version"]),
        payload=json.loads(str(row["payload_json"])),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _action_request_payload(action: ActionEnvelope) -> dict[str, Any]:
    return {
        "action_type": action.action_type,
        "workflow_id": action.workflow_id,
        "aggregate_type": action.aggregate_type.value,
        "aggregate_id": action.aggregate_id,
        "actor": action.actor,
        "expected_version": action.expected_version,
        "payload": dict(action.payload),
    }


def _decode_role_session(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _decode_role_assignment(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    for key, output, default in (
        ("required_inputs_json", "required_inputs", "[]"),
        ("input_refs_json", "input_refs", "{}"),
        ("execution_spec_json", "execution_spec", "{}"),
        ("submission_artifact_ref_json", "submission_artifact_ref", "{}"),
        ("settlement_action_json", "settlement_action", "{}"),
    ):
        value[output] = json.loads(str(value.pop(key, default) or default))
    return value


def _decode_role_attempt(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    for key, output in (
        ("prompt_pack_ref_json", "prompt_pack_ref"),
        ("response_artifact_ref_json", "response_artifact_ref"),
        ("harness_state_json", "harness_state"),
    ):
        value[output] = json.loads(str(value.pop(key, "{}") or "{}"))
    return value


def _encode_dispatch_result(result: DispatchResult) -> dict[str, Any]:
    return {
        "snapshot": {
            **asdict(result.snapshot),
            "aggregate_type": result.snapshot.aggregate_type.value,
            "payload": dict(result.snapshot.payload),
        },
        "events": [
            {
                **asdict(event),
                "aggregate_type": event.aggregate_type.value,
                "payload": dict(event.payload),
            }
            for event in result.events
        ],
        "outbox_effect_ids": list(result.outbox_effect_ids),
    }


def _decode_dispatch_result(value: Mapping[str, Any], *, duplicate: bool) -> DispatchResult:
    snapshot_data = dict(value.get("snapshot") or {})
    snapshot = AggregateSnapshot(
        aggregate_type=AggregateType(str(snapshot_data["aggregate_type"])),
        aggregate_id=str(snapshot_data["aggregate_id"]),
        workflow_id=str(snapshot_data["workflow_id"]),
        state=str(snapshot_data["state"]),
        version=int(snapshot_data["version"]),
        payload=dict(snapshot_data.get("payload") or {}),
        created_at=str(snapshot_data["created_at"]),
        updated_at=str(snapshot_data["updated_at"]),
    )
    events = tuple(
        DomainEvent(
            event_id=str(item["event_id"]),
            workflow_id=str(item["workflow_id"]),
            aggregate_type=AggregateType(str(item["aggregate_type"])),
            aggregate_id=str(item["aggregate_id"]),
            aggregate_version=int(item["aggregate_version"]),
            event_type=str(item["event_type"]),
            payload=dict(item.get("payload") or {}),
            action_id=str(item["action_id"]),
            correlation_id=str(item["correlation_id"]),
            causation_id=str(item["causation_id"]),
            created_at=str(item["created_at"]),
        )
        for item in list(value.get("events") or [])
    )
    return DispatchResult(
        snapshot=snapshot,
        events=events,
        outbox_effect_ids=tuple(str(item) for item in list(value.get("outbox_effect_ids") or [])),
        duplicate=duplicate,
    )


def _artifact_digests(value: Any) -> set[str]:
    digests: set[str] = set()
    if isinstance(value, Mapping):
        raw_digest = value.get("sha256")
        if isinstance(raw_digest, str) and len(raw_digest.removeprefix("sha256:")) == 64:
            digests.add(raw_digest.removeprefix("sha256:"))
        for item in value.values():
            digests.update(_artifact_digests(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            digests.update(_artifact_digests(item))
    elif isinstance(value, str) and value.startswith("sha256:") and len(value) == 71:
        digests.add(value.removeprefix("sha256:"))
    return digests


def _manifest_sha_from_action(action: ActionEnvelope) -> str:
    value = action.payload.get("architecture_manifest_ref") or action.payload.get("clarification_ref")
    if isinstance(value, Mapping):
        return str(value.get("sha256") or "").removeprefix("sha256:")
    return str(value or action.payload.get("manifest_sha") or "").removeprefix("sha256:")


def _active_projection_snapshot(
    snapshots: list[AggregateSnapshot],
    workflow: AggregateSnapshot,
) -> AggregateSnapshot | None:
    if workflow.state in _TERMINAL_WORKFLOW_STATES | {"PAUSED", "PAUSE_REQUESTED", "CANCEL_REQUESTED", "TRIAGE_REQUIRED"}:
        return workflow
    execution_id = str(workflow.payload.get("execution_epoch_id") or "")
    architecture_id = str(workflow.payload.get("architecture_revision_id") or "")
    if execution_id:
        match = next((item for item in snapshots if item.aggregate_id == execution_id), None)
        if match is not None:
            if match.state == "REPLAN_REQUIRED":
                epoch_revision_id = str(
                    match.payload.get("active_replan_revision_id") or ""
                )
                workflow_revision_id = str(
                    workflow.payload.get("architecture_revision_id") or ""
                )
                for revision_id in dict.fromkeys(
                    (workflow_revision_id, epoch_revision_id)
                ):
                    revision = next(
                        (
                            item
                            for item in snapshots
                            if item.aggregate_type
                            == AggregateType.ARCHITECTURE_REVISION
                            and item.aggregate_id == revision_id
                            and (
                                str(
                                    item.payload.get("source_execution_epoch_id")
                                    or ""
                                )
                                == match.aggregate_id
                                or item.aggregate_id == epoch_revision_id
                                or str(
                                    item.payload.get("architecture_cycle_id") or ""
                                )
                                == epoch_revision_id
                            )
                        ),
                        None,
                    )
                    if revision is not None:
                        return revision
            return match
    if architecture_id:
        match = next((item for item in snapshots if item.aggregate_id == architecture_id), None)
        if match is not None:
            return match
    children = [item for item in snapshots if item.aggregate_type != AggregateType.WORKFLOW]
    return children[0] if children else workflow


def _current_phase(workflow: AggregateSnapshot, active: AggregateSnapshot | None) -> str:
    if workflow.state in _TERMINAL_WORKFLOW_STATES | {"PAUSED", "PAUSE_REQUESTED", "CANCEL_REQUESTED", "TRIAGE_REQUIRED"}:
        return workflow.state.lower()
    if active is None or active.aggregate_type == AggregateType.WORKFLOW:
        return "created" if workflow.state == "CREATED" else "routing"
    if active.aggregate_type == AggregateType.ARCHITECTURE_REVISION:
        state = active.state
        if state.startswith("ARCHITECT"):
            return "architecture"
        if state in {"REVIEW_QUEUED", "REVIEWING"}:
            return "architecture_review"
        if state == "HUMAN_REVIEW":
            return "human_review"
        return f"architecture_{state.lower()}"
    if active.aggregate_type == AggregateType.EXECUTION_EPOCH:
        if active.state == "REPLAN_COLLECTING":
            return "replan_collecting"
        if active.state == "REPLAN_REQUIRED":
            return "replan_required"
        return "finalizing" if active.state == "FINALIZING" else "executing"
    if active.aggregate_type == AggregateType.DAG_NODE_RUN:
        return "executing"
    return "standalone_review"


def _active_lineage_has_triage(
    workflow: AggregateSnapshot,
    snapshots: list[AggregateSnapshot],
    active: AggregateSnapshot | None,
) -> bool:
    if active is not None and active.state == "TRIAGE_REQUIRED":
        return True
    execution_id = str(workflow.payload.get("execution_epoch_id") or "")
    execution = next(
        (
            item
            for item in snapshots
            if item.aggregate_type == AggregateType.EXECUTION_EPOCH
            and item.aggregate_id == execution_id
        ),
        None,
    )
    if execution is None:
        return False
    if execution.state != "REPLAN_REQUIRED":
        return any(
            item.aggregate_type == AggregateType.DAG_NODE_RUN
            and str(item.payload.get("epoch_id") or "") == execution.aggregate_id
            and item.state == "TRIAGE_REQUIRED"
            for item in snapshots
        )
    revision_id = str(execution.payload.get("active_replan_revision_id") or "")
    return any(
        item.aggregate_type == AggregateType.ARCHITECTURE_REVISION
        and item.aggregate_id == revision_id
        and item.state == "TRIAGE_REQUIRED"
        for item in snapshots
    )


def _decode_json_columns(row: sqlite3.Row, columns: Mapping[str, str]) -> dict[str, Any]:
    result = dict(row)
    for source, target in columns.items():
        result[target] = json.loads(str(result.pop(source)))
    result["waiting_for_user"] = bool(result.get("waiting_for_user"))
    return result


def _normalize_delivery_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    binding = dict(value or {})
    channel_id = str(binding.get("channel_id") or "").strip()
    channel_kind = str(binding.get("channel_kind") or "").strip()
    reply_target = binding.get("reply_target")
    if not channel_id or not channel_kind or not isinstance(reply_target, Mapping):
        raise ValueError(
            "delivery binding requires channel_id, channel_kind, and reply_target"
        )
    normalized_target = {
        str(key): item
        for key, item in dict(reply_target).items()
        if str(key).strip()
    }
    if not normalized_target:
        raise ValueError("delivery binding requires a non-empty reply_target")
    return {
        "channel_id": channel_id,
        "channel_kind": channel_kind,
        "reply_target": normalized_target,
        "control_scope_key": str(
            binding.get("control_scope_key")
            or normalized_target.get("control_scope_key")
            or f"{channel_kind}:{channel_id}"
        ),
    }


def _delivery_binding_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "task_id": str(row["task_id"]),
        "origin": json.loads(str(row["origin_binding_json"])),
        "current": json.loads(str(row["current_binding_json"])),
        "binding_version": int(row["binding_version"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _delivery_outbox_row(row: sqlite3.Row) -> dict[str, Any]:
    result = {
        "delivery_id": str(row["delivery_id"]),
        "dedup_key": str(row["dedup_key"]),
        "task_id": str(row["task_id"]),
        "workflow_id": str(row["workflow_id"]),
        "event_kind": str(row["event_kind"]),
        "payload": json.loads(str(row["payload_json"])),
        "status": str(row["status"]),
        "attempt_count": int(row["attempt_count"]),
        "next_attempt_at": str(row["next_attempt_at"]),
        "last_error": str(row["last_error"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }
    if "current_binding_json" in row.keys():
        result["binding"] = json.loads(str(row["current_binding_json"]))
        result["binding_version"] = int(row["binding_version"])
    return result


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _cycle_payload(cycle: PlanCycle | NodeCycle) -> dict[str, Any]:
    assignment = cycle.active_assignment
    verdict = cycle.last_verdict
    return {
        "cycle_id": cycle.cycle_id,
        "kind": cycle.kind.value,
        "generation": cycle.generation,
        "state": cycle.state.value,
        **(
            {"node_name": cycle.node_name}
            if isinstance(cycle, NodeCycle)
            else {}
        ),
        "active_assignment": (
            {
                "slot": assignment.slot.value,
                "kind": assignment.kind.value,
                "generation": assignment.generation,
                "input_fingerprint": assignment.input_fingerprint,
            }
            if assignment is not None
            else None
        ),
        "product_ref": cycle.product_ref,
        "accepted_product_ref": cycle.accepted_product_ref,
        "last_verdict": (
            {
                "accepted": verdict.accepted,
                "generation": verdict.generation,
                "finding_refs": list(verdict.finding_refs),
            }
            if verdict is not None
            else None
        ),
        "resume_state": (
            cycle.resume_state.value
            if cycle.resume_state is not None
            else None
        ),
    }


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _utc_datetime() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


_QUEUED_STATES = {
    "ARCHITECT_QUEUED",
    "REVIEW_QUEUED",
    "QUEUED",
    "REPAIR_QUEUED",
    "STARTING",
}


_HUMAN_WAIT_STATES = {"HUMAN_REVIEW"}


_TERMINAL_WORKFLOW_STATES = {"COMPLETED", "REJECTED", "CANCELLED"}


_TASK_FTS_INDEX_VERSION = "jieba-v1"
