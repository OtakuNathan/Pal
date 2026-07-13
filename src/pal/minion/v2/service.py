from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from pal.minion.v2.architecture import ArchitectureArtifactService, ResearchMode
from pal.minion.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.minion.v2.catalog import MinionV2Catalog
from pal.minion.v2.contracts import ActionEnvelope, AggregateSnapshot, AggregateType, DispatchResult
from pal.minion.v2.repository import MinionV2Repository
from pal.minion.v2.skeleton import GitBackedSkeletonService, review_architecture_skeleton


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
    catalog: MinionV2Catalog = field(init=False)
    skeleton: GitBackedSkeletonService = field(init=False)

    def __post_init__(self) -> None:
        self.repository = MinionV2Repository(Path(self.runtime_root))
        self.artifacts = ContentAddressedArtifactStore(Path(self.runtime_root), self.repository)
        self.architecture = ArchitectureArtifactService(self.artifacts, self.repository)
        self.catalog = MinionV2Catalog(Path(self.runtime_root), self.artifacts)
        self.skeleton = GitBackedSkeletonService(Path(self.runtime_root), self.artifacts)

    def create_task(self, request: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(request)
        task_id = str(data.get("task_id") or f"task_{uuid4().hex}").strip()
        title = str(data.get("title") or "").strip()
        objective = str(data.get("objective") or data.get("goal") or "").strip()
        family_id = str(data.get("family_id") or data.get("profile_family") or "").strip()
        workspace = _normalize_workspace(data.get("workspace"))
        if not title or not objective or not family_id or not workspace:
            raise ValueError("task requires title, objective, family_id, and workspace")
        self.catalog.validate_family_exists(family_id)
        revision_ref = self._publish_task_revision(
            task_id=task_id,
            revision=1,
            title=title,
            objective=objective,
            family_id=family_id,
            workspace=workspace,
            references=_normalize_references(data.get("references")),
            policies=dict(data.get("policies") or {}),
            actor=str(data.get("actor") or "pal"),
        )
        result = self.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_TASK",
                workflow_id="",
                aggregate_type=AggregateType.TASK,
                aggregate_id=task_id,
                actor=str(data.get("actor") or "pal"),
                source_channel=str(data.get("source_channel") or "local"),
                expected_version=0,
                idempotency_key=f"create-task:{task_id}",
                payload={
                    "title": title,
                    "objective": objective,
                    "family_id": family_id,
                    "workspace_key": _workspace_key(workspace),
                    "task_revision": 1,
                    "task_revision_ref": revision_ref.to_dict(),
                    "owner": str(data.get("actor") or "pal"),
                },
            )
        )
        return {
            "status": "created",
            "task_id": task_id,
            "state": result.snapshot.state,
            "task_revision_ref": revision_ref.to_dict(),
        }

    def prepare_requirements(self, request: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(request)
        payload = {
            "title": str(data.get("title") or "Requirements").strip(),
            "requirements": list(data.get("requirements") or []),
            "sections": dict(data.get("sections") or {}),
            "strengths": dict(data.get("strengths") or {}),
            "open_clarifications": list(data.get("open_clarifications") or []),
            "source_coverage": list(data.get("source_coverage") or []),
        }
        ref = self.architecture.publish_requirements(
            payload,
            provenance={
                "actor": str(data.get("actor") or "pal"),
                "source_channel": str(data.get("source_channel") or "local"),
                "owner": "foreground_pal",
            },
        )
        return {"status": "prepared", "requirements_ref": ref.to_dict()}

    def search_tasks(self, request: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(request)
        tasks = self.repository.search_tasks(
            query=str(data.get("query") or ""),
            task_id=str(data.get("task_id") or ""),
            family_id=str(data.get("family_id") or ""),
            include_archived=bool(data.get("include_archived")),
            limit=int(data.get("limit") or 20),
        )
        return {"status": "ok", "tasks": list(tasks), "count": len(tasks)}

    def update_task(self, request: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(request)
        task_id = str(data.get("task_id") or "").strip()
        task = self.repository.read_snapshot(AggregateType.TASK, task_id)
        if task is None or task.state != "ACTIVE":
            raise ValueError(f"active task not found: {task_id}")
        if data.get("family_id") and str(data["family_id"]) != str(task.payload.get("family_id") or ""):
            raise ValueError("task family_id is immutable")
        previous_ref = dict(task.payload.get("task_revision_ref") or {})
        previous = dict(self.artifacts.read_json(previous_ref))
        title = str(data.get("title") or previous.get("title") or "").strip()
        objective = str(data.get("objective") or previous.get("objective") or "").strip()
        workspace = _normalize_workspace(data.get("workspace") or previous.get("workspace"))
        references = (
            _normalize_references(data["references"])
            if "references" in data
            else _normalize_references(previous.get("references"))
        )
        policies = dict(data["policies"]) if "policies" in data else dict(previous.get("policies") or {})
        revision = int(previous.get("revision") or task.payload.get("task_revision") or 1) + 1
        revision_ref = self._publish_task_revision(
            task_id=task_id,
            revision=revision,
            title=title,
            objective=objective,
            family_id=str(task.payload.get("family_id") or ""),
            workspace=workspace,
            references=references,
            policies=policies,
            actor=str(data.get("actor") or "pal"),
            parent_ref=previous_ref,
        )
        result = self.repository.dispatch(
            ActionEnvelope(
                action_type="UPDATE_TASK_CONTEXT",
                workflow_id="",
                aggregate_type=AggregateType.TASK,
                aggregate_id=task_id,
                actor=str(data.get("actor") or "pal"),
                source_channel=str(data.get("source_channel") or "local"),
                expected_version=task.version,
                idempotency_key=f"update-task:{task_id}:{revision_ref.sha256}",
                payload={
                    "title": title,
                    "objective": objective,
                    "workspace_key": _workspace_key(workspace),
                    "task_revision": revision,
                    "task_revision_ref": revision_ref.to_dict(),
                },
            )
        )
        return {"status": "updated", "task_id": task_id, "state": result.snapshot.state, "task_revision_ref": revision_ref.to_dict()}

    def archive_task(self, request: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(request)
        task_id = str(data.get("task_id") or "").strip()
        task = self.repository.read_snapshot(AggregateType.TASK, task_id)
        if task is None or task.state != "ACTIVE":
            raise ValueError(f"active task not found: {task_id}")
        if self.repository.has_nonterminal_workflows_for_task(task_id):
            raise ValueError("task has nonterminal workflows")
        result = self.repository.dispatch(
            ActionEnvelope(
                action_type="ARCHIVE_TASK",
                workflow_id="",
                aggregate_type=AggregateType.TASK,
                aggregate_id=task_id,
                actor=str(data.get("actor") or "pal"),
                source_channel=str(data.get("source_channel") or "local"),
                expected_version=task.version,
                idempotency_key=f"archive-task:{task_id}",
                payload={"archive_reason": str(data.get("reason") or "")},
            )
        )
        return {"status": "archived", "task_id": task_id, "state": result.snapshot.state}

    def start_workflow(self, request: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(request)
        task_id = str(data.get("task_id") or "").strip()
        if not task_id:
            task_id = self._create_or_reuse_task_for_workflow(data)
        task = self.repository.read_snapshot(AggregateType.TASK, task_id)
        if task is None or task.state != "ACTIVE":
            raise ValueError("start_workflow requires an active task_id")
        if data.get("workspace"):
            raise ValueError("workflow workspace is owned by Task; update the Task instead")
        task_revision_ref = dict(task.payload.get("task_revision_ref") or {})
        task_revision = dict(self.artifacts.read_json(task_revision_ref))
        family_binding_ref = self.catalog.publish_family_binding(str(task.payload.get("family_id") or ""))
        operation = str(data.get("operation") or "new_requirement").strip().lower()
        if operation not in ROUTER_OPERATIONS:
            raise ValueError(f"unsupported artifact router operation: {operation}")
        workflow_id = str(data.get("workflow_id") or f"wf_{uuid4().hex}").strip()
        actor = str(data.get("actor") or "pal").strip()
        source_channel = str(data.get("source_channel") or "local").strip()
        research_mode = ResearchMode(str(data.get("research_mode") or ResearchMode.LOCAL_ONLY))
        goal = str(data.get("goal") or "").strip()
        artifact_ref = _artifact_ref_mapping(data.get("artifact_ref"))
        requirements_ref = _artifact_ref_mapping(data.get("requirements_ref"))
        if operation == "new_requirement" and not goal:
            raise ValueError("new_requirement workflow requires goal")
        if operation == "new_requirement" and not requirements_ref:
            requirements_payload: dict[str, Any]
            if data.get("sections"):
                requirements_payload = {
                    "title": str(data.get("title") or goal or "Requirements"),
                    "sections": dict(data.get("sections") or {}),
                    "strengths": dict(data.get("strengths") or {}),
                }
            elif data.get("requirements"):
                requirements_payload = {
                    "title": str(data.get("title") or goal or "Requirements"),
                    "requirements": list(data.get("requirements") or []),
                }
            else:
                requirements_payload = {
                    "title": str(data.get("title") or goal or "Requirements"),
                    "sections": {"Requested outcome": [goal]},
                }
            prepared = self.prepare_requirements(
                {
                    **requirements_payload,
                    "actor": actor,
                    "source_channel": source_channel,
                    "source_coverage": ["foreground user request"],
                }
            )
            requirements_ref = dict(prepared["requirements_ref"])
        if requirements_ref:
            record = self.repository.read_artifact_record(str(requirements_ref.get("sha256") or ""))
            if record is None or str(record.get("artifact_type") or "") != "RequirementsArtifact":
                raise ValueError("requirements_ref must reference a durable RequirementsArtifact")
        if operation != "new_requirement" and not artifact_ref:
            raise ValueError(f"{operation} requires artifact_ref")
        if operation in {"execute_trusted", "review_then_execute"}:
            self._validate_external_architecture_ref(
                artifact_ref,
                trusted_required=operation == "execute_trusted",
                family_id=str(task.payload.get("family_id") or ""),
            )
        request_payload = {
            "schema_version": "1",
            "workflow_id": workflow_id,
            "task_id": task_id,
            "task_revision_ref": task_revision_ref,
            "family_binding_ref": family_binding_ref.to_dict(),
            "operation": operation,
            "goal": goal,
            "requirements_ref": requirements_ref,
            "constraints": data.get("constraints") or [],
            "approved_evidence": list(data.get("approved_evidence") or []),
            "workspace": _normalize_workspace(task_revision.get("workspace")),
            "references": _normalize_references(
                [*list(task_revision.get("references") or []), *list(data.get("references") or [])]
            ),
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
            child_refs=(
                (str(task_revision_ref["sha256"]), "task_revision"),
                (family_binding_ref.sha256, "family_binding"),
                *(((str(requirements_ref["sha256"]), "requirements"),) if requirements_ref else ()),
                *(((str(artifact_ref["sha256"]), "input"),) if artifact_ref else ()),
            ),
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
                    "task_id": task_id,
                    "task_revision_ref": task_revision_ref,
                    "family_binding_ref": family_binding_ref.to_dict(),
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
            "task_id": task_id,
            "state": result.snapshot.state,
            "request_ref": request_ref.to_dict(),
            "next_action": "manager_outbox_tick",
        }

    def _create_or_reuse_task_for_workflow(self, data: Mapping[str, Any]) -> str:
        goal = str(data.get("goal") or data.get("objective") or "").strip()
        family_id = str(data.get("family_id") or "software_engineering").strip()
        workspace = _normalize_workspace(data.get("workspace"))
        title = str(data.get("title") or goal or "Minion workflow").strip()
        if not workspace:
            raise ValueError("one-click start_workflow requires workspace when task_id is omitted")
        workspace_key = _workspace_key(workspace)
        candidates = self.repository.search_tasks(
            query="",
            family_id=family_id,
            include_archived=False,
            limit=100,
        )
        match = next(
            (
                item
                for item in candidates
                if str(item.get("workspace_key") or "") == workspace_key
                and str(item.get("objective") or "") == goal
            ),
            None,
        )
        if match is not None:
            return str(match["task_id"])
        created = self.create_task(
            {
                "title": title,
                "objective": goal,
                "family_id": family_id,
                "workspace": workspace,
                "references": list(data.get("references") or []),
                "policies": dict(data.get("policies") or {}),
                "actor": str(data.get("actor") or "pal"),
                "source_channel": str(data.get("source_channel") or "local"),
            }
        )
        return str(created["task_id"])

    def _publish_task_revision(
        self,
        *,
        task_id: str,
        revision: int,
        title: str,
        objective: str,
        family_id: str,
        workspace: Mapping[str, Any],
        references: list[Any],
        policies: Mapping[str, Any],
        actor: str,
        parent_ref: Mapping[str, Any] | None = None,
    ) -> ArtifactRef:
        child_refs = ()
        if parent_ref and parent_ref.get("sha256"):
            child_refs = ((str(parent_ref["sha256"]), "previous_revision"),)
        return self.artifacts.put_json(
            {
                "schema_version": "1",
                "task_id": task_id,
                "revision": int(revision),
                "title": title,
                "objective": objective,
                "family_id": family_id,
                "workspace": dict(workspace),
                "references": list(references),
                "policies": dict(policies),
            },
            artifact_type="TaskRevisionArtifact",
            provenance={"actor": actor, "task_id": task_id},
            child_refs=child_refs,
        )

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
        if workflow.state == "PAUSED":
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
        triaged = [
            item
            for item in self.repository.list_workflow_snapshots(workflow_id)
            if item.aggregate_type != AggregateType.WORKFLOW
            and item.state == "TRIAGE_REQUIRED"
            and "RESOLVE_TRIAGE" in self.repository.engine.legal_actions(item.aggregate_type, item.state)
        ]
        if triaged:
            resumed = []
            for item in triaged:
                result = self.repository.dispatch(
                    ActionEnvelope(
                        action_type="RESOLVE_TRIAGE",
                        workflow_id=workflow_id,
                        aggregate_type=item.aggregate_type,
                        aggregate_id=item.aggregate_id,
                        actor=actor,
                        source_channel=source_channel,
                        expected_version=item.version,
                        idempotency_key=f"resolve-triage:{item.aggregate_id}:{item.version}",
                    )
                )
                resumed.append(
                    {
                        "aggregate_type": result.snapshot.aggregate_type.value,
                        "aggregate_id": result.snapshot.aggregate_id,
                        "state": result.snapshot.state,
                    }
                )
            return {"status": "triage_resolved", "workflow_id": workflow_id, "state": workflow.state, "resumed": resumed}
        return {
            "status": "not_resumable",
            "workflow_id": workflow_id,
            "state": workflow.state,
            "next_legal_actions": list(self.repository.engine.legal_actions(AggregateType.WORKFLOW, workflow.state)),
        }

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
        family_id: str,
    ) -> None:
        record = self.repository.read_artifact_record(str(artifact_ref.get("sha256") or ""))
        if record is None or not record.get("durable"):
            raise ValueError("external artifact is not durable")
        if trusted_required and not bool(dict(record.get("metadata") or {}).get("trusted_internal_source")):
            raise ValueError("execute_trusted requires an internally trusted artifact source")
        artifact_type = str(record.get("artifact_type") or "")
        if family_id == "software_engineering":
            if artifact_type != "ArchitectureSkeletonArtifact":
                raise ValueError(
                    "software_engineering workflows accept ArchitectureSkeletonArtifact only; "
                    "the legacy SWE JSON contract graph is not supported"
                )
            if str(record.get("schema_version") or "") != "1":
                raise ValueError("unsupported ArchitectureSkeletonArtifact schema version")
            artifact = dict(self.artifacts.read_json(_artifact_ref_from_record(record)))
            for key in ("requirements_ref", "git_bundle_ref"):
                child = dict(artifact.get(key) or {})
                child_record = self.repository.read_artifact_record(str(child.get("sha256") or ""))
                if child_record is None or not child_record.get("durable"):
                    raise ValueError(f"ArchitectureSkeletonArtifact has no durable {key}")
            worktree = self.skeleton.provision_review_worktree(
                artifact=artifact,
                review_name=f"external-{str(record['sha256'])[:16]}",
            )
            requirements = self.artifacts.read_json(dict(artifact["requirements_ref"]))
            review = review_architecture_skeleton(
                artifact,
                worktree=worktree,
                requirements_payload=requirements,
            )
            if review.verdict != "PASS":
                raise ValueError("external ArchitectureSkeletonArtifact failed mechanical skeleton validation")
            return
        if artifact_type != "ArchitectureContractArtifact":
            raise ValueError("this family requires an ArchitectureContractArtifact")
        if str(record.get("schema_version") or "") != "1":
            raise ValueError("unsupported ArchitectureContractArtifact schema version")
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
    request = dict(service.artifacts.read_json(request_ref))
    request["workspace"] = _normalize_workspace(request.get("workspace"))
    request["references"] = _normalize_references(request.get("references"))
    return request


def _normalize_workspace(value: Any) -> dict[str, Any]:
    workspace = dict(value or {}) if isinstance(value, Mapping) else {}
    if not str(workspace.get("repo_path") or "").strip():
        alias = str(workspace.get("repo_root") or workspace.get("root") or workspace.get("path") or "").strip()
        if alias:
            workspace["repo_path"] = str(Path(_file_uri_path(alias)).expanduser())
    elif workspace.get("repo_path"):
        workspace["repo_path"] = str(Path(_file_uri_path(str(workspace["repo_path"]))).expanduser())
    if workspace.get("repo_path") and not workspace.get("kind"):
        workspace["kind"] = "existing_repo"
    return workspace


def _normalize_references(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(list(value or []), start=1):
        item = dict(raw) if isinstance(raw, Mapping) else {"path": str(raw)}
        path = str(item.get("path") or item.get("root") or item.get("uri") or "").strip()
        if not path:
            continue
        normalized = str(Path(_file_uri_path(path)).expanduser())
        if normalized in seen:
            continue
        seen.add(normalized)
        item["path"] = normalized
        item.setdefault("name", Path(normalized.rstrip("/")).name or f"reference_{index}")
        if not item.get("description") and item.get("note"):
            item["description"] = str(item["note"])
        result.append(item)
    return result


def _file_uri_path(value: str) -> str:
    text = str(value or "").strip()
    return text.removeprefix("file://") if text.startswith("file://") else text


def _artifact_ref_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping) and value.get("sha256"):
        return dict(value)
    return {}


def _workspace_key(workspace: Mapping[str, Any]) -> str:
    for key in ("repo_path", "project_name", "root", "path"):
        value = str(workspace.get(key) or "").strip()
        if value:
            return value
    return str(workspace.get("kind") or "workspace")


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
