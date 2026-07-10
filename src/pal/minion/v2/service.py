from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from pal.minion.v2.architecture import ArchitectureArtifactService, ResearchMode
from pal.minion.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.minion.v2.contracts import ActionEnvelope, AggregateSnapshot, AggregateType, DispatchResult
from pal.minion.v2.repository import MinionV2Repository


ROUTER_OPERATIONS = {
    "new_requirement",
    "execute_trusted",
    "review_then_execute",
    "standalone_review",
    "review_and_repair",
}


@dataclass
class MinionV2WorkflowService:
    runtime_root: Path
    repository: MinionV2Repository = field(init=False)
    artifacts: ContentAddressedArtifactStore = field(init=False)
    architecture: ArchitectureArtifactService = field(init=False)

    def __post_init__(self) -> None:
        self.repository = MinionV2Repository(Path(self.runtime_root))
        self.artifacts = ContentAddressedArtifactStore(Path(self.runtime_root), self.repository)
        self.architecture = ArchitectureArtifactService(self.artifacts, self.repository)

    def start_workflow(self, request: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(request)
        operation = str(data.get("operation") or "new_requirement").strip().lower()
        if operation not in ROUTER_OPERATIONS:
            raise ValueError(f"unsupported artifact router operation: {operation}")
        workflow_id = str(data.get("workflow_id") or f"wf_{uuid4().hex}").strip()
        actor = str(data.get("actor") or "pal").strip()
        source_channel = str(data.get("source_channel") or "local").strip()
        research_mode = ResearchMode(str(data.get("research_mode") or ResearchMode.LOCAL_ONLY))
        goal = str(data.get("goal") or "").strip()
        artifact_ref = _artifact_ref_mapping(data.get("artifact_ref"))
        if operation == "new_requirement" and not goal:
            raise ValueError("new_requirement workflow requires goal")
        if operation != "new_requirement" and not artifact_ref:
            raise ValueError(f"{operation} requires artifact_ref")
        if operation in {"execute_trusted", "review_then_execute"}:
            self._validate_external_architecture_ref(artifact_ref, trusted_required=operation == "execute_trusted")
        request_payload = {
            "schema_version": "1",
            "workflow_id": workflow_id,
            "operation": operation,
            "goal": goal,
            "requirements": data.get("requirements") or [],
            "constraints": data.get("constraints") or [],
            "approved_evidence": list(data.get("approved_evidence") or []),
            "workspace": dict(data.get("workspace") or {}),
            "references": list(data.get("references") or []),
            "research_mode": research_mode.value,
            "input_artifact_ref": artifact_ref,
            "actor": actor,
            "source_channel": source_channel,
            "control_route": dict(data.get("control_route") or {}),
        }
        request_ref = self.artifacts.put_json(
            request_payload,
            artifact_type="WorkflowRequestArtifact",
            provenance={"actor": actor, "source_channel": source_channel},
            child_refs=((str(artifact_ref["sha256"]), "input"),) if artifact_ref else (),
        )
        result = self.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_WORKFLOW",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor=actor,
                source_channel=source_channel,
                expected_version=0,
                idempotency_key=f"create-workflow:{workflow_id}",
                payload={
                    "request_ref": request_ref.to_dict(),
                    "operation": operation,
                    "research_mode": research_mode.value,
                    "owner": actor,
                    "active_channel": source_channel,
                    "control_route": dict(data.get("control_route") or {}),
                    "desired_state": "ACTIVE",
                },
            )
        )
        return {
            "status": "created",
            "workflow_id": workflow_id,
            "state": result.snapshot.state,
            "request_ref": request_ref.to_dict(),
            "next_action": "manager_outbox_tick",
        }

    def submit_artifact(self, request: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(request)
        artifact_type = str(data.get("artifact_type") or "").strip()
        schema_version = str(data.get("schema_version") or "1")
        media_type = str(data.get("media_type") or "application/json")
        provenance = dict(data.get("provenance") or {})
        metadata = dict(data.get("metadata") or {})
        trusted = bool(data.get("trusted_internal_source"))
        if trusted:
            metadata["trusted_internal_source"] = True
        content = data.get("content")
        if media_type == "application/json":
            ref = self.artifacts.put_json(
                content,
                artifact_type=artifact_type,
                schema_version=schema_version,
                provenance=provenance,
                metadata=metadata,
            )
        else:
            if isinstance(content, str):
                raw = content.encode("utf-8")
            elif isinstance(content, bytes):
                raw = content
            else:
                raise ValueError("non-JSON artifact content must be text or bytes")
            ref = self.artifacts.put_bytes(
                raw,
                artifact_type=artifact_type,
                schema_version=schema_version,
                media_type=media_type,
                provenance=provenance,
                metadata=metadata,
            )
        return {"status": "published", "artifact_ref": ref.to_dict()}

    def workflow_status(self, workflow_id: str) -> dict[str, Any]:
        projection = self.repository.read_workflow_projection(workflow_id)
        if projection is None:
            return {"status": "not_found", "workflow_id": workflow_id}
        snapshots = self.repository.list_workflow_snapshots(workflow_id)
        active_id = str(projection.get("active_aggregate_id") or "")
        active = next((item for item in snapshots if item.aggregate_id == active_id), None)
        workflow_state = str(projection["workflow_state"])
        active_state = active.state if active is not None else ""
        return {
            "status": "ok",
            "workflow_id": workflow_id,
            "current_phase": projection["current_phase"],
            "workflow_state": projection["workflow_state"],
            "active_aggregate_type": projection["active_aggregate_type"],
            "active_aggregate_id": active_id,
            "active_node_state": active_state,
            "active_worker": projection.get("active_worker_id") or "",
            "blocker": projection["blocker"],
            "next_legal_action": _public_next_actions(workflow_state, active_state),
            "waiting_for_user": bool(projection["waiting_for_user"]),
            "liveness": projection["liveness"],
            "metrics": projection["metrics"],
            "last_progress_event": projection["last_progress_event_id"],
        }

    def control_workflow(
        self,
        *,
        workflow_id: str,
        command: str,
        actor: str,
        source_channel: str,
        reason: str = "",
    ) -> dict[str, Any]:
        normalized = str(command).strip().lower()
        action_types = {"pause": "REQUEST_PAUSE", "cancel": "REQUEST_CANCEL"}
        if normalized not in action_types:
            raise ValueError("workflow control command must be pause or cancel")
        workflow = self._workflow_snapshot(workflow_id)
        result = self.repository.dispatch(
            ActionEnvelope(
                action_type=action_types[normalized],
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor=actor,
                source_channel=source_channel,
                expected_version=workflow.version,
                idempotency_key=f"control:{workflow_id}:{workflow.version}:{normalized}",
                payload={"reason": reason},
            )
        )
        return {"status": "accepted", "workflow_id": workflow_id, "state": result.snapshot.state}

    def resume_workflow(
        self,
        *,
        workflow_id: str,
        actor: str,
        source_channel: str,
    ) -> dict[str, Any]:
        workflow = self._workflow_snapshot(workflow_id)
        if workflow.state != "PAUSED":
            return {
                "status": "not_resumable",
                "workflow_id": workflow_id,
                "state": workflow.state,
                "next_legal_actions": list(self.repository.engine.legal_actions(AggregateType.WORKFLOW, workflow.state)),
            }
        result = self.repository.dispatch(
            ActionEnvelope(
                action_type="RESUME",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor=actor,
                source_channel=source_channel,
                expected_version=workflow.version,
                idempotency_key=f"resume:{workflow_id}:{workflow.version}",
            )
        )
        return {"status": "resumed", "workflow_id": workflow_id, "state": result.snapshot.state}

    def submit_human_decision(self, request: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(request)
        token = str(data.get("decision_token") or "")
        decision = str(data.get("decision") or "").strip().lower()
        token_record = self.repository.inspect_human_decision_token(token)
        if token_record is None:
            raise ValueError("unknown human decision token")
        if str(token_record.get("status") or "") != "issued":
            raise ValueError("human decision token is stale or already consumed")
        revision_id = str(token_record["architecture_revision_id"])
        workflow_id = str(token_record["workflow_id"])
        revision = self.repository.read_snapshot(AggregateType.ARCHITECTURE_REVISION, revision_id)
        if revision is None:
            raise ValueError("architecture revision does not exist")
        manifest_sha = str(token_record["manifest_sha"])
        record = self.repository.read_artifact_record(manifest_sha)
        if record is None:
            raise ValueError("human decision artifact does not exist")
        if str(record.get("artifact_type") or "") == "ClarificationRequestArtifact":
            if decision != "clarify":
                raise ValueError("clarification token requires decision=clarify")
            response = str(data.get("clarification_response") or "").strip()
            if not response:
                raise ValueError("clarify decision requires clarification_response")
            clarification_ref = _artifact_ref_from_record(record).to_dict()
            response_ref = self.artifacts.put_json(
                {"response": response, "clarification_request_sha": manifest_sha},
                artifact_type="ClarificationResponseArtifact",
                provenance={"actor": data.get("actor"), "source_channel": data.get("source_channel")},
                child_refs=((manifest_sha, "answers"),),
            )
            result = self.repository.dispatch(
                ActionEnvelope(
                    action_type="CLARIFICATION_PROVIDED",
                    workflow_id=workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=revision_id,
                    actor=str(data.get("actor") or ""),
                    source_channel=str(data.get("source_channel") or ""),
                    expected_version=revision.version,
                    idempotency_key=f"human-clarification:{token}",
                    payload={
                        "decision_token": token,
                        "clarification_ref": clarification_ref,
                        "clarification_response_ref": response_ref.to_dict(),
                    },
                )
            )
            return {
                "status": "accepted",
                "workflow_id": workflow_id,
                "revision_id": revision_id,
                "state": result.snapshot.state,
            }
        action_types = {"accept": "HUMAN_ACCEPT", "edit": "HUMAN_EDIT", "reject": "HUMAN_REJECT"}
        if decision not in action_types:
            raise ValueError("architecture decision must be accept, edit, or reject")
        manifest_ref = _artifact_ref_from_record(record).to_dict()
        payload: dict[str, Any] = {
            "decision_token": token,
            "architecture_manifest_ref": manifest_ref,
        }
        if decision == "edit":
            instruction = str(data.get("edit_instruction") or "").strip()
            if not instruction:
                raise ValueError("edit decision requires edit_instruction")
            edit_ref = self.artifacts.put_json(
                {"instruction": instruction, "manifest_sha": manifest_sha},
                artifact_type="ArchitectureEditInstructionArtifact",
                provenance={"actor": data.get("actor"), "source_channel": data.get("source_channel")},
                child_refs=((manifest_sha, "revises"),),
            )
            payload["edit_instruction_ref"] = edit_ref.to_dict()
        result = self.repository.dispatch(
            ActionEnvelope(
                action_type=action_types[decision],
                workflow_id=workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision_id,
                actor=str(data.get("actor") or ""),
                source_channel=str(data.get("source_channel") or ""),
                expected_version=revision.version,
                idempotency_key=f"human-decision:{token}",
                payload=payload,
            )
        )
        return {"status": "accepted", "workflow_id": workflow_id, "revision_id": revision_id, "state": result.snapshot.state}

    def archive_workflow(self, *, workflow_id: str, actor: str, reason: str = "") -> dict[str, Any]:
        workflow = self._workflow_snapshot(workflow_id)
        if workflow.state not in {"COMPLETED", "REJECTED", "CANCELLED"}:
            raise ValueError("only terminal V2 workflows can be archived")
        result = self.repository.dispatch(
            ActionEnvelope(
                action_type="ARCHIVE",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor=actor,
                expected_version=workflow.version,
                idempotency_key=f"archive:{workflow_id}",
                payload={"archived": True, "archive_reason": reason},
            )
        )
        return {"status": "archived", "workflow_id": workflow_id, "state": result.snapshot.state}

    def _workflow_snapshot(self, workflow_id: str) -> AggregateSnapshot:
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        if workflow is None:
            raise ValueError(f"workflow not found: {workflow_id}")
        return workflow

    def _validate_external_architecture_ref(
        self,
        artifact_ref: Mapping[str, Any],
        *,
        trusted_required: bool,
    ) -> None:
        record = self.repository.read_artifact_record(str(artifact_ref.get("sha256") or ""))
        if record is None or not record.get("durable"):
            raise ValueError("external artifact is not durable")
        if str(record.get("artifact_type") or "") != "ArchitectureContractArtifact":
            raise ValueError("V2 accepts ArchitectureContractArtifact only; V1 FinalPlan is not supported")
        if str(record.get("schema_version") or "") != "1":
            raise ValueError("unsupported ArchitectureContractArtifact schema version")
        if trusted_required and not bool(dict(record.get("metadata") or {}).get("trusted_internal_source")):
            raise ValueError("execute_trusted requires an internally trusted artifact source")
        review = self.architecture.review_manifest(_artifact_ref_from_record(record))
        if review.verdict != "PASS":
            raise ValueError("external ArchitectureContractArtifact failed V2 contract validation")


def workflow_request_from_snapshot(
    service: MinionV2WorkflowService,
    workflow: AggregateSnapshot,
) -> dict[str, Any]:
    request_ref = dict(workflow.payload.get("request_ref") or {})
    if not request_ref:
        raise ValueError("workflow has no request artifact")
    return dict(service.artifacts.read_json(request_ref))


def _artifact_ref_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping) and value.get("sha256"):
        return dict(value)
    return {}


def _artifact_ref_from_record(record: Mapping[str, Any]) -> ArtifactRef:
    return ArtifactRef(
        sha256=str(record["sha256"]),
        artifact_type=str(record["artifact_type"]),
        schema_version=str(record["schema_version"]),
        media_type=str(record["media_type"]),
        byte_size=int(record["byte_size"]),
        durable=bool(record["durable"]),
    )


def _public_next_actions(workflow_state: str, active_state: str) -> list[str]:
    if workflow_state in {"COMPLETED", "REJECTED", "CANCELLED"}:
        return ["archive_workflow"]
    if workflow_state == "PAUSED":
        return ["resume_workflow", "control_workflow:cancel"]
    if workflow_state in {"PAUSE_REQUESTED", "CANCEL_REQUESTED"}:
        return ["wait_for_safe_point"]
    if workflow_state == "TRIAGE_REQUIRED" or active_state == "TRIAGE_REQUIRED":
        return ["operator_triage", "control_workflow:cancel"]
    if active_state in {"HUMAN_REVIEW", "CLARIFICATION_PENDING"}:
        return ["submit_human_decision", "control_workflow:cancel"]
    return ["control_workflow:pause", "control_workflow:cancel"]
