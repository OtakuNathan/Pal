from __future__ import annotations
from pal.bunshin.storage.serialization import _action_request_payload
from pal.bunshin.storage.serialization import _encode_dispatch_result
from pal.bunshin.storage.serialization import _decode_dispatch_result
from pal.bunshin.storage.serialization import _normalize_delivery_binding
from pal.bunshin.storage.serialization import _json
from pal.bunshin.storage.serialization import _stable_hash
import json
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import uuid4
from pal.bunshin.contracts import ActionEnvelope, AggregateType, DispatchResult, DomainEvent
from pal.bunshin.engine import TransitionEngine
from pal.bunshin.storage.artifacts import ArtifactsStore
from pal.bunshin.storage.connection_contracts import DatabasePort
from pal.bunshin.storage.human_decisions import HumanDecisionsStore
from pal.bunshin.storage.projections import ProjectionsStore
from pal.bunshin.storage.role_submissions import RoleSubmissionsStore
from pal.bunshin.storage.snapshots import SnapshotsStore


@dataclass
class TransitionsStore:
    artifacts: ArtifactsStore
    database: DatabasePort
    human_decisions: HumanDecisionsStore
    projections: ProjectionsStore
    role_submissions: RoleSubmissionsStore
    snapshots: SnapshotsStore
    engine: TransitionEngine

    def legal_actions(self, aggregate_type: AggregateType, state: str) -> tuple[str, ...]:
        return self.engine.legal_actions(aggregate_type, state)

    def dispatch(
        self,
        action: ActionEnvelope,
        *,
        role_assignment_id: str = "",
        role_submission_payload_hash: str = "",
        _connection: sqlite3.Connection | None = None,
    ) -> DispatchResult:
        if bool(str(role_assignment_id or "")) != bool(
            str(role_submission_payload_hash or "")
        ):
            raise ValueError(
                "role submission settlement requires assignment id and payload hash"
            )
        if _connection is None:
            self.database.ensure_schema()
        request_hash = _stable_hash(_action_request_payload(action))
        transaction = self.database.write_connection() if _connection is None else nullcontext(_connection)
        with transaction as connection:
            duplicate = connection.execute(
                """
                SELECT request_hash, result_json
                FROM bunshin_v2_action_dedup
                WHERE aggregate_type = ? AND aggregate_id = ? AND idempotency_key = ?
                """,
                (action.aggregate_type.value, action.aggregate_id, action.dedup_key),
            ).fetchone()
            if duplicate is not None:
                if str(duplicate["request_hash"]) != request_hash:
                    raise ValueError("idempotency key was reused with a different action request")
                if role_assignment_id:
                    self.role_submissions.settle_role_assignment_locked(
                        connection,
                        assignment_id=str(role_assignment_id),
                        submission_payload_hash=str(role_submission_payload_hash),
                        workflow_id=action.workflow_id,
                        aggregate_type=action.aggregate_type.value,
                        aggregate_id=action.aggregate_id,
                    )
                return _decode_dispatch_result(json.loads(str(duplicate["result_json"])), duplicate=True)

            self.human_decisions.consume_human_decision_locked(connection, action)
            self.artifacts.assert_artifact_refs_durable(connection, action.payload)
            current = self.snapshots.read_snapshot_locked(connection, action.aggregate_type, action.aggregate_id)
            outcome = self.engine.transition(current, action)
            self.snapshots.write_snapshot_locked(connection, current, outcome.snapshot)

            events = self.persist_domain_events(action, connection, outcome)

            if outcome.effects and not events:
                raise RuntimeError("outbox effects require a causative domain event")
            effect_ids: list[str] = []
            event_id = events[0].event_id if events else ""
            for index, effect in enumerate(outcome.effects):
                effect_id = f"eff_{uuid4().hex}"
                effect_key = f"{event_id}:{index}"
                payload = dict(effect.payload)
                payload["_causal_context"] = {
                    "aggregate_version": outcome.snapshot.version,
                    "target_state": outcome.snapshot.state,
                    "active_worker_id": str(
                        outcome.snapshot.payload.get("active_worker_id") or ""
                    ),
                    "lease_resource_key": str(
                        outcome.snapshot.payload.get("lease_resource_key") or ""
                    ),
                    "fencing_token": int(
                        outcome.snapshot.payload.get("fencing_token") or 0
                    ),
                    **({"pending_verification_ref": dict(outcome.snapshot.payload.get("pending_verification_ref") or {})}
                       if effect.effect_type in {"quiesce_verifier_role", "snapshot_verifier_result"} else {}),
                }
                effect_request_hash = _stable_hash({"effect_type": effect.effect_type, "payload": payload})
                connection.execute(
                    """
                    INSERT INTO bunshin_v2_outbox(
                        effect_id, effect_key, workflow_id, aggregate_type, aggregate_id,
                        event_id, effect_index, effect_type, request_hash, payload_json,
                        status, attempt_count, max_attempts, next_retry_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                    """,
                    (
                        effect_id,
                        effect_key,
                        action.workflow_id,
                        action.aggregate_type.value,
                        action.aggregate_id,
                        event_id,
                        index,
                        effect.effect_type,
                        effect_request_hash,
                        _json(payload),
                        max(1, int(effect.max_attempts)),
                        action.created_at,
                        action.created_at,
                        action.created_at,
                    ),
                )
                effect_ids.append(effect_id)

            result = DispatchResult(
                snapshot=outcome.snapshot,
                events=tuple(events),
                outbox_effect_ids=tuple(effect_ids),
            )
            encoded_result = _encode_dispatch_result(result)
            connection.execute(
                """
                INSERT INTO bunshin_v2_action_dedup(
                    aggregate_type, aggregate_id, idempotency_key, action_id,
                    request_hash, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action.aggregate_type.value,
                    action.aggregate_id,
                    action.dedup_key,
                    action.action_id,
                    request_hash,
                    _json(encoded_result),
                    action.created_at,
                ),
            )
            if role_assignment_id:
                self.role_submissions.settle_role_assignment_locked(
                    connection,
                    assignment_id=str(role_assignment_id),
                    submission_payload_hash=str(role_submission_payload_hash),
                    workflow_id=action.workflow_id,
                    aggregate_type=action.aggregate_type.value,
                    aggregate_id=action.aggregate_id,
                )
            self.projections.update_task_projection_locked(connection, outcome.snapshot)
            self.projections.update_node_projection_locked(connection, outcome.snapshot)
            if action.aggregate_type != AggregateType.TASK:
                self.projections.rebuild_workflow_projection_locked(connection, action.workflow_id, events[-1].event_id if events else "")
            return result

    def dispatch_task_with_delivery(
        self,
        action: ActionEnvelope,
        *,
        binding: Mapping[str, Any],
    ) -> DispatchResult:
        """Create one Task and its delivery binding in the same DB commit."""

        if action.aggregate_type != AggregateType.TASK or action.action_type != "CREATE_TASK":
            raise ValueError("atomic Task delivery binding requires CREATE_TASK")
        self.database.ensure_schema()
        normalized = _normalize_delivery_binding(binding)
        with self.database.write_connection() as connection:
            result = self.dispatch(action, _connection=connection)
            existing = connection.execute(
                "SELECT * FROM bunshin_v2_task_delivery_bindings WHERE task_id = ?",
                (action.aggregate_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO bunshin_v2_task_delivery_bindings(
                        task_id, origin_binding_json, current_binding_json,
                        binding_version, created_at, updated_at
                    ) VALUES (?, ?, ?, 1, ?, ?)
                    """,
                    (
                        action.aggregate_id,
                        _json(normalized),
                        _json(normalized),
                        action.created_at,
                        action.created_at,
                    ),
                )
            else:
                origin = json.loads(str(existing["origin_binding_json"]))
                if origin != normalized:
                    raise ValueError(
                        "Task delivery origin differs from the idempotent creation request"
                    )
        return result

    def persist_domain_events(self, action: ActionEnvelope, connection: Any, outcome: Any) -> list[DomainEvent]:
        events: list[DomainEvent] = []
        for draft in outcome.events:
            event = DomainEvent(
                event_id=f"evt_{uuid4().hex}",
                workflow_id=action.workflow_id,
                aggregate_type=action.aggregate_type,
                aggregate_id=action.aggregate_id,
                aggregate_version=outcome.snapshot.version,
                event_type=draft.event_type,
                payload=dict(draft.payload),
                action_id=action.action_id,
                correlation_id=action.correlation_id or action.action_id,
                causation_id=action.causation_id,
                created_at=action.created_at,
            )
            connection.execute(
                """
                INSERT INTO bunshin_v2_domain_events(
                    event_id, workflow_id, aggregate_type, aggregate_id, aggregate_version,
                    event_type, payload_json, action_id, correlation_id, causation_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.workflow_id,
                    event.aggregate_type.value,
                    event.aggregate_id,
                    event.aggregate_version,
                    event.event_type,
                    _json(event.payload),
                    event.action_id,
                    event.correlation_id,
                    event.causation_id,
                    event.created_at,
                ),
            )
            events.append(event)
        return events
