from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from pal.minion.v2.architecture import ArchitectureArtifactService, ResearchMode
from pal.minion.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.minion.v2.catalog import MinionV2Catalog
from pal.minion.v2.contracts import ActionEnvelope, AggregateSnapshot, AggregateType, DispatchResult
from pal.minion.v2.machines import LIVENESS_REQUIRED_STATES
from pal.minion.v2.repository import MinionV2Repository
from pal.minion.v2.paths import inferred_project_name
from pal.minion.v2.replan import compile_architecture_finding_markdown
from pal.minion.v2.skeleton import (
    ARCHITECTURE_SKELETON_ARTIFACT,
    GitBackedSkeletonService,
    compile_skeleton_markdown,
    review_architecture_skeleton,
)
from pal.minion.v2.task_ledger import (
    TASK_LEDGER_ARTIFACT,
    TaskLedgerService,
    TaskRevisionAuthority,
)


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
    task_ledger: TaskLedgerService = field(init=False)

    def __post_init__(self) -> None:
        self.repository = MinionV2Repository(Path(self.runtime_root))
        self.artifacts = ContentAddressedArtifactStore(Path(self.runtime_root), self.repository)
        self.architecture = ArchitectureArtifactService(self.artifacts, self.repository)
        self.catalog = MinionV2Catalog(Path(self.runtime_root), self.artifacts)
        self.skeleton = GitBackedSkeletonService(Path(self.runtime_root), self.artifacts)
        self.task_ledger = TaskLedgerService(Path(self.runtime_root), self.artifacts)

    def create_task(self, request: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(request)
        task_id = str(data.get("task_id") or f"task_{uuid4().hex}").strip()
        title = str(data.get("title") or "").strip()
        objective = str(data.get("objective") or data.get("goal") or "").strip()
        primary_profile = self.catalog.profile(str(data.get("profile") or "").strip())
        primary_profile_id = primary_profile.canonical_profile_id
        family_id = primary_profile.profile_group.replace("/", ".")
        workspace = _normalize_workspace(data.get("workspace"))
        if not title or not objective or not workspace:
            raise ValueError("task requires title, objective, profile, and workspace")
        family_binding_ref = self.catalog.publish_family_binding(primary_profile_id)
        revision_ref = self._publish_task_revision(
            task_id=task_id,
            revision=1,
            title=title,
            objective=objective,
            primary_profile_id=primary_profile_id,
            family_id=family_id,
            family_binding_ref=family_binding_ref.to_dict(),
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
                    "primary_profile_id": primary_profile_id,
                    "family_id": family_id,
                    "family_binding_ref": family_binding_ref.to_dict(),
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
            "profile": primary_profile_id,
            "family": family_id,
            "family_binding_ref": family_binding_ref.to_dict(),
            "task_revision_ref": revision_ref.to_dict(),
        }

    def prepare_requirements(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Publish the immutable initial task specification used by every V2 role."""

        data = dict(request)
        forbidden = {
            "requirements",
            "sections",
            "strengths",
            "source_coverage",
            "source_documents",
            "source_artifact_refs",
        }.intersection(data)
        if forbidden:
            raise ValueError(
                "V2 no longer accepts normalized Requirements fields: "
                + ", ".join(sorted(forbidden))
            )
        task_spec = data.get("task_spec")
        if not isinstance(task_spec, Mapping) or not task_spec:
            raise ValueError("prepare_requirements requires a non-empty task_spec object")
        ref = self.task_ledger.publish(
            title=str(data.get("title") or "Task"),
            task_spec=dict(task_spec),
            actor=str(data.get("actor") or "pal"),
            source_channel=str(data.get("source_channel") or "local"),
        )
        return {"status": "prepared", "requirements_ref": ref.to_dict()}

    def search_tasks(self, request: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(request)
        tasks = self.repository.search_tasks(
            query=str(data.get("query") or ""),
            task_id=str(data.get("task_id") or ""),
            family_id=str(data.get("family_id") or ""),
            owner=str(data.get("owner") or ""),
            include_archived=bool(data.get("include_archived")),
            limit=int(data.get("limit") or 20),
        )
        return {"status": "ok", "tasks": list(tasks), "count": len(tasks)}

    def search_task_ledger(self, request: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(request)
        actor = str(data.get("actor") or "pal")
        source_channel = str(data.get("source_channel") or "")
        tasks = self.repository.search_tasks(
            query=str(data.get("query") or ""),
            family_id=str(data.get("family_id") or ""),
            owner=actor,
            include_archived=bool(data.get("include_archived")),
            limit=int(data.get("limit") or 10),
        )
        bound_workflow = self.repository.read_channel_workflow(actor_id=actor, channel_id=source_channel)
        items: list[dict[str, Any]] = []
        for task in tasks:
            workflows = self.repository.search_workflows(
                actor_id=actor,
                channel_id="",
                task_id=str(task["task_id"]),
                include_terminal=True,
                limit=20,
            )
            workflow_items: list[dict[str, Any]] = []
            for workflow in workflows:
                projection = self.repository.read_workflow_projection(str(workflow["workflow_id"])) or {}
                workflow_items.append(
                    {
                        "state": str(workflow.get("workflow_state") or ""),
                        "phase": str(projection.get("current_phase") or ""),
                        "liveness": str(projection.get("liveness") or ""),
                        "waiting_for_user": bool(projection.get("waiting_for_user")),
                        "bound_to_current_channel": str(workflow["workflow_id"]) == bound_workflow,
                        "updated_at": str(workflow.get("updated_at") or ""),
                    }
                )
            items.append(
                {
                    "task_id": str(task["task_id"]),
                    "title": str(task.get("title") or ""),
                    "objective": str(task.get("objective") or ""),
                    "profile": str(task.get("profile_id") or ""),
                    "family": str(task.get("family_id") or ""),
                    "workspace": str(task.get("workspace_key") or ""),
                    "state": str(task.get("state") or ""),
                    "score": float(task.get("score") or 0.0),
                    "updated_at": str(task.get("updated_at") or ""),
                    "workflows": workflow_items,
                }
            )
        return {"status": "ok", "query": str(data.get("query") or ""), "tasks": items, "count": len(items)}

    def update_task(self, request: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(request)
        task_id = str(data.get("task_id") or "").strip()
        task = self.repository.read_snapshot(AggregateType.TASK, task_id)
        if task is None or task.state != "ACTIVE":
            raise ValueError(f"active task not found: {task_id}")
        if data.get("profile") and str(data["profile"]) != str(task.payload.get("primary_profile_id") or ""):
            raise ValueError("task profile is immutable")
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
            primary_profile_id=str(task.payload.get("primary_profile_id") or ""),
            family_id=str(task.payload.get("family_id") or ""),
            family_binding_ref=dict(task.payload.get("family_binding_ref") or {}),
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
        _validate_start_workflow_shape(data)
        operation = str(data.get("operation") or "new_requirement").strip().lower()
        if operation not in ROUTER_OPERATIONS:
            raise ValueError(f"unsupported artifact router operation: {operation}")
        raw_goal = str(data.get("goal") or "")
        goal = raw_goal.strip()
        artifact_ref = _artifact_ref_mapping(data.get("artifact_ref"))
        requirements_ref = _artifact_ref_mapping(data.get("requirements_ref"))
        if operation == "new_requirement" and not goal:
            raise ValueError("new_requirement workflow requires goal")
        if operation != "new_requirement" and not artifact_ref:
            raise ValueError(f"{operation} requires artifact_ref")

        task_id = str(data.get("task_id") or "").strip()
        task_was_selected = bool(task_id)
        if not task_id:
            task_id = self._create_or_reuse_task_for_workflow(data)
        task = self.repository.read_snapshot(AggregateType.TASK, task_id)
        if task is None or task.state != "ACTIVE":
            raise ValueError("start_workflow requires an active task_id")
        if task_was_selected and data.get("workspace"):
            raise ValueError("workflow workspace is owned by Task; update the Task instead")
        if task_was_selected and data.get("profile"):
            raise ValueError("workflow profile is owned by Task; create a new Task to change it")
        task_revision_ref = dict(task.payload.get("task_revision_ref") or {})
        task_revision = dict(self.artifacts.read_json(task_revision_ref))
        workspace = _normalize_workspace(task_revision.get("workspace"))
        family_binding_ref = _artifact_ref_mapping(task.payload.get("family_binding_ref"))
        if not family_binding_ref:
            raise ValueError("Task has no pinned FamilyBindingArtifact")
        workflow_id = str(data.get("workflow_id") or f"wf_{uuid4().hex}").strip()
        actor = str(data.get("actor") or "pal").strip()
        source_channel = str(data.get("source_channel") or "local").strip()
        research_mode = ResearchMode(str(data.get("research_mode") or ResearchMode.LOCAL_ONLY))
        task_spec = data.get("task_spec")
        if task_spec is not None and (not isinstance(task_spec, Mapping) or not task_spec):
            raise ValueError("task_spec must be a non-empty object")
        if operation != "new_requirement" and task_spec is not None:
            raise ValueError("task_spec is only valid for new_requirement workflows")
        if requirements_ref and task_spec is not None:
            raise ValueError("task_spec cannot be combined with an existing task ledger")
        if operation == "new_requirement" and not requirements_ref:
            if not isinstance(task_spec, Mapping) or not task_spec:
                raise ValueError("new_requirement workflow requires task_spec")
            requirements_ref = self.task_ledger.publish(
                title=str(data.get("title") or goal or "Task"),
                task_spec=dict(task_spec),
                actor=actor,
                source_channel=source_channel,
            ).to_dict()
        if operation in {"execute_trusted", "review_then_execute"}:
            self._validate_external_architecture_ref(
                artifact_ref,
                trusted_required=operation == "execute_trusted",
                family_id=str(task.payload.get("family_id") or ""),
            )
            architecture = dict(self.artifacts.read_json(artifact_ref))
            architecture_requirements_ref = dict(
                architecture.get("requirements_ref") or {}
            )
            if requirements_ref and requirements_ref != architecture_requirements_ref:
                raise ValueError(
                    "workflow task ledger differs from the imported architecture"
                )
            requirements_ref = architecture_requirements_ref
        if requirements_ref:
            record = self.repository.read_artifact_record(str(requirements_ref.get("sha256") or ""))
            if record is None or str(record.get("artifact_type") or "") != TASK_LEDGER_ARTIFACT:
                raise ValueError(
                    "task truth must reference a durable TaskLedgerArtifact"
                )
        if operation == "review_and_repair" and str(task.payload.get("family_id") or "") == "software_engineering":
            self._validate_external_architecture_ref(
                artifact_ref,
                trusted_required=False,
                family_id="software_engineering",
            )
            repair_artifact = dict(self.artifacts.read_json(artifact_ref))
            repair_modules = dict(dict(repair_artifact.get("submission") or {}).get("modules") or {})
            repair_implementation_modules = {
                name: module
                for name, module in repair_modules.items()
                if str(dict(module or {}).get("module_kind") or "") == "implementation"
            }
            if len(repair_implementation_modules) != 1:
                raise ValueError(
                    "software review_and_repair requires a bounded single-module ArchitectureSkeletonArtifact"
                )
        request_payload = {
            "schema_version": "1",
            "workflow_id": workflow_id,
            "task_id": task_id,
            "task_revision_ref": task_revision_ref,
            "family_binding_ref": family_binding_ref,
            "operation": operation,
            "goal": raw_goal,
            "workflow_name": str(task_revision.get("title") or goal or "Minion workflow"),
            "requirements_ref": requirements_ref,
            "constraints": data.get("constraints") or [],
            "approved_evidence": list(data.get("approved_evidence") or []),
            "workspace": workspace,
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
                (str(family_binding_ref["sha256"]), "family_binding"),
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
                    "family_binding_ref": family_binding_ref,
                    "operation": operation,
                    "research_mode": research_mode.value,
                    "owner": actor,
                    "active_channel": source_channel,
                    "control_route": dict(data.get("control_route") or {}),
                    "desired_state": "ACTIVE",
                },
            )
        )
        self.repository.bind_channel_workflow(
            actor_id=actor,
            channel_id=source_channel,
            workflow_id=workflow_id,
        )
        return {
            "status": "created",
            "workflow_id": workflow_id,
            "task_id": task_id,
            "task_title": str(task_revision.get("title") or ""),
            "state": result.snapshot.state,
            "request_ref": request_ref.to_dict(),
            "next_action": "manager_outbox_tick",
        }

    def _create_or_reuse_task_for_workflow(self, data: Mapping[str, Any]) -> str:
        goal = str(data.get("goal") or data.get("objective") or "").strip()
        primary_profile = self.catalog.profile(str(data.get("profile") or "").strip())
        primary_profile_id = primary_profile.canonical_profile_id
        family_id = primary_profile.profile_group.replace("/", ".")
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
                and str(item.get("profile_id") or "") == primary_profile_id
                and str(item.get("owner") or "") == str(data.get("actor") or "pal")
            ),
            None,
        )
        if match is not None:
            return str(match["task_id"])
        created = self.create_task(
            {
                "title": title,
                "objective": goal,
                "profile": primary_profile_id,
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
        primary_profile_id: str,
        family_id: str,
        family_binding_ref: Mapping[str, Any],
        workspace: Mapping[str, Any],
        references: list[Any],
        policies: Mapping[str, Any],
        actor: str,
        parent_ref: Mapping[str, Any] | None = None,
    ) -> ArtifactRef:
        child_refs: tuple[tuple[str, str], ...] = (
            (str(family_binding_ref["sha256"]), "family_binding"),
        )
        if parent_ref and parent_ref.get("sha256"):
            child_refs = (*child_refs, (str(parent_ref["sha256"]), "previous_revision"))
        return self.artifacts.put_json(
            {
                "schema_version": "2",
                "task_id": task_id,
                "revision": int(revision),
                "title": title,
                "objective": objective,
                "primary_profile_id": primary_profile_id,
                "family_id": family_id,
                "family_binding_ref": dict(family_binding_ref),
                "workspace": dict(workspace),
                "references": list(references),
                "policies": dict(policies),
            },
            artifact_type="TaskRevisionArtifact",
            schema_version="2",
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
        public_name = str(data.get("name") or "").strip()
        if public_name:
            self.repository.bind_artifact_alias(
                actor_id=str(data.get("actor") or "pal"),
                channel_id=str(data.get("source_channel") or "local"),
                alias=public_name,
                artifact_sha256=ref.sha256,
            )
        return {
            "status": "published",
            "name": public_name,
            "artifact_type": artifact_type,
            "artifact_ref": ref.to_dict(),
        }

    def resolve_task_selector(self, *, selector: str, actor: str) -> str:
        query = str(selector or "").strip()
        if not query:
            return ""
        candidates = list(
            self.repository.search_tasks(
                query=query,
                owner=actor,
                include_archived=False,
                limit=20,
            )
        )
        exact = [item for item in candidates if str(item.get("title") or "").casefold() == query.casefold()]
        selected = exact or candidates
        if len(selected) == 1:
            return str(selected[0]["task_id"])
        if not selected:
            raise ValueError(f"No active Minion task matches {query!r}.")
        names = ", ".join(sorted({str(item.get("title") or "untitled task") for item in selected}))
        raise ValueError(f"Task name {query!r} is ambiguous. Matching tasks: {names}")

    def resolve_workflow_selector(self, *, selector: str, actor: str, source_channel: str) -> str:
        query = str(selector or "").strip()
        if not query:
            bound = self.repository.read_channel_workflow(actor_id=actor, channel_id=source_channel)
            if bound:
                snapshot = self.repository.read_snapshot(AggregateType.WORKFLOW, bound)
                if snapshot is not None:
                    return bound
            candidates = self.repository.search_workflows(
                actor_id=actor,
                channel_id=source_channel,
                include_terminal=False,
                limit=2,
            )
        else:
            task_id = self.resolve_task_selector(selector=query, actor=actor)
            candidates = self.repository.search_workflows(
                actor_id=actor,
                channel_id="",
                task_id=task_id,
                include_terminal=True,
                limit=20,
            )
            active = tuple(
                item
                for item in candidates
                if str(item.get("workflow_state") or "") not in {"COMPLETED", "REJECTED", "CANCELLED"}
            )
            if active:
                candidates = active
            elif candidates:
                candidates = candidates[:1]
        if len(candidates) == 1:
            workflow_id = str(candidates[0]["workflow_id"])
            self.repository.bind_channel_workflow(
                actor_id=actor,
                channel_id=source_channel,
                workflow_id=workflow_id,
            )
            return workflow_id
        if not candidates:
            detail = f" matching {query!r}" if query else " for this channel"
            raise ValueError(f"No Minion workflow{detail}.")
        names = ", ".join(sorted({str(item.get("task_title") or "untitled task") for item in candidates}))
        raise ValueError(f"Workflow selection is ambiguous. Matching tasks: {names}")

    def resolve_artifact_name(self, *, name: str, actor: str, source_channel: str) -> dict[str, Any]:
        record = self.repository.resolve_artifact_alias(
            actor_id=actor,
            channel_id=source_channel,
            alias=str(name or "").strip(),
        )
        if record is None:
            raise ValueError(f"No submitted artifact named {name!r} exists in this channel.")
        return _artifact_ref_from_record(record).to_dict()

    def workflow_status(self, workflow_id: str, *, view: str = "status") -> dict[str, Any]:
        normalized_view = str(view or "status").strip().lower()
        if normalized_view not in {"status", "human_review"}:
            raise ValueError("workflow status view must be status or human_review")
        projection = self.repository.read_workflow_projection(workflow_id)
        if projection is None:
            return {"status": "not_found", "workflow_id": workflow_id}
        snapshots = self.repository.list_workflow_snapshots(workflow_id)
        active_id = str(projection.get("active_aggregate_id") or "")
        active = next((item for item in snapshots if item.aggregate_id == active_id), None)
        workflow_state = str(projection["workflow_state"])
        active_state = active.state if active is not None else ""
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        task_title = ""
        if workflow is not None:
            task_id = str(workflow.payload.get("task_id") or "")
            tasks = self.repository.search_tasks(task_id=task_id, include_archived=True, limit=1) if task_id else ()
            task_title = str(tasks[0].get("title") or "") if tasks else ""
        waiting_for_user = bool(projection["waiting_for_user"])
        active_worker = "" if waiting_for_user else str(projection.get("active_worker_id") or "")
        invocation = self.repository.read_role_invocation(active_worker) if active_worker else None
        worker_node = next(
            (
                item
                for item in snapshots
                if invocation is not None
                and item.aggregate_id == str(invocation.get("aggregate_id") or "")
            ),
            None,
        )
        latest_event = self.repository.read_latest_workflow_event(workflow_id)
        result = {
            "status": "ok",
            "workflow_id": workflow_id,
            "current_phase": projection["current_phase"],
            "workflow_state": projection["workflow_state"],
            "active_aggregate_type": projection["active_aggregate_type"],
            "active_aggregate_id": active_id,
            "active_node_state": active_state,
            "task_title": task_title,
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
            "blocker": projection["blocker"],
            "next_legal_action": _public_next_actions(workflow_state, active_state),
            "waiting_for_user": waiting_for_user,
            "human_review_available": active_state == "HUMAN_REVIEW",
            "liveness": projection["liveness"],
            "metrics": projection["metrics"],
            "last_progress_event": dict(latest_event or {}),
        }
        if normalized_view == "human_review":
            if active is None or active.state != "HUMAN_REVIEW":
                raise ValueError("workflow is not waiting for architecture human review")
            result["human_review"] = self._human_review_view(active)
        return result

    def _human_review_view(self, revision: AggregateSnapshot) -> dict[str, Any]:
        manifest_ref = dict(revision.payload.get("architecture_manifest_ref") or {})
        if not manifest_ref:
            raise ValueError("human review has no architecture manifest")
        card_ref = dict(revision.payload.get("human_review_card_ref") or {})
        if not card_ref:
            card_ref = dict(
                self.repository.read_latest_effect_result_artifact(
                    workflow_id=revision.workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=revision.aggregate_id,
                    effect_type="publish_architecture_review_request",
                )
                or {}
            )
        card: dict[str, Any] = {}
        if card_ref:
            record = self.repository.read_artifact_record(str(card_ref.get("sha256") or ""))
            if record and str(record.get("artifact_type") or "") == "HumanReviewCardArtifact":
                card = dict(self.artifacts.read_json(card_ref))
                if str(card.get("manifest_sha") or "") != str(manifest_ref.get("sha256") or ""):
                    raise ValueError("human review card is stale for the active architecture revision")
        if card:
            markdown = str(card.get("markdown") or "")
            actions = [str(item) for item in list(card.get("actions") or [])]
        else:
            manifest = dict(self.artifacts.read_json(manifest_ref))
            record = self.repository.read_artifact_record(str(manifest_ref.get("sha256") or ""))
            if record and str(record.get("artifact_type") or "") == ARCHITECTURE_SKELETON_ARTIFACT:
                requirements = self.artifacts.read_json(dict(manifest.get("requirements_ref") or {}))
                markdown = compile_skeleton_markdown(
                    manifest,
                    requirements_payload=requirements,
                )
            else:
                markdown = self.architecture.compile_human_review_markdown(manifest_ref)
            actions = ["accept", "edit", "reject"]
        replan_batch_value = revision.payload.get("replan_finding_batch_ref")
        if replan_batch_value and not card:
            markdown = (
                compile_architecture_finding_markdown(
                    dict(self.artifacts.read_json(dict(replan_batch_value)))
                )
                + "\n"
                + markdown
            )
        review_ref = dict(revision.payload.get("review_artifact_ref") or {})
        review = dict(self.artifacts.read_json(review_ref)) if review_ref else {}
        return {
            "markdown": markdown,
            "actions": actions,
            "review_verdict": str(review.get("verdict") or ""),
            "findings": list(review.get("findings") or []),
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

    def restart_execution_from_architecture(
        self,
        *,
        workflow_id: str,
        actor: str,
        source_channel: str,
        reason: str,
        control_route: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        summary = str(reason or "").strip()
        if not summary:
            raise ValueError("execution restart requires a non-empty reason")
        workflow = self._workflow_snapshot(workflow_id)
        if "REQUEST_EXECUTION_RESTART" not in self.repository.engine.legal_actions(
            AggregateType.WORKFLOW,
            workflow.state,
        ):
            raise ValueError(
                f"workflow cannot restart execution from state {workflow.state}"
            )
        revision = self._accepted_architecture_revision_for_restart(workflow)
        manifest_ref = dict(revision.payload.get("architecture_manifest_ref") or {})
        manifest = dict(self.artifacts.read_json(manifest_ref))
        requirements_ref = dict(manifest.get("requirements_ref") or {})
        if not requirements_ref:
            raise ValueError("accepted architecture has no bound task ledger")
        request = workflow_request_from_snapshot(self, workflow)
        task_id = str(workflow.payload.get("task_id") or request.get("task_id") or "")
        if not task_id:
            raise ValueError("workflow has no Task binding")
        restart_request = {
            "task_id": task_id,
            "architecture_manifest_ref": manifest_ref,
            "requirements_ref": requirements_ref,
            "goal": str(request.get("goal") or ""),
            "research_mode": "none",
            "actor": actor,
            "source_channel": source_channel,
            "control_route": dict(control_route or {}),
            "reason": summary,
            "operation": "review_then_execute",
            "reuse_candidates": False,
        }
        result = self.repository.dispatch(
            ActionEnvelope(
                action_type="REQUEST_EXECUTION_RESTART",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor=actor,
                source_channel=source_channel,
                expected_version=workflow.version,
                idempotency_key=f"restart-execution:{workflow_id}:{workflow.version}",
                payload={"restart_execution_request": restart_request},
            )
        )
        return {
            "status": "restart_requested",
            "workflow_id": workflow_id,
            "state": result.snapshot.state,
            "reason": summary,
            "architecture_review": "required",
            "candidate_reuse": False,
            "next_action": "settle current workflow and create replacement workflow",
        }

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
        self.triage_orphaned_work_aggregates(
            workflow_id=workflow_id,
            actor=actor,
            source_channel=source_channel,
        )
        triaged = self._triage_candidates(workflow_id)
        if triaged:
            return {
                "status": "triage_requires_resolution",
                "workflow_id": workflow_id,
                "state": workflow.state,
                "triage": [_public_triage_candidate(item) for item in triaged],
                "next_legal_action": "resolve_triage",
            }
        return {
            "status": "not_resumable",
            "workflow_id": workflow_id,
            "state": workflow.state,
            "next_legal_actions": list(self.repository.engine.legal_actions(AggregateType.WORKFLOW, workflow.state)),
        }

    def resolve_triage(
        self,
        *,
        workflow_id: str,
        actor: str,
        source_channel: str,
        resolution: str,
        subject: str = "",
    ) -> dict[str, Any]:
        summary = str(resolution or "").strip()
        if not summary:
            raise ValueError("manual triage resolution requires a non-empty resolution summary")
        candidates = self._triage_candidates(workflow_id)
        if not candidates:
            raise ValueError("workflow has no TRIAGE_REQUIRED item that can be resolved")
        selected = _select_triage_candidate(candidates, subject=subject)
        resolution_payload: dict[str, Any] = {
            "triage_resolution": summary,
            "triage_resolution_kind": "manual",
        }
        if (
            selected.aggregate_type == AggregateType.ARCHITECTURE_REVISION
            and str(selected.payload.get("triage_resume_state") or "") == "REVIEW_QUEUED"
            and not selected.payload.get("requirements_ref")
            and selected.payload.get("architecture_manifest_ref")
        ):
            manifest = dict(
                self.artifacts.read_json(
                    dict(selected.payload["architecture_manifest_ref"])
                )
            )
            manifest_requirements_ref = dict(manifest.get("requirements_ref") or {})
            workflow = self._workflow_snapshot(workflow_id)
            request = workflow_request_from_snapshot(self, workflow)
            request_requirements_ref = dict(request.get("requirements_ref") or {})
            if not manifest_requirements_ref:
                raise ValueError("imported architecture has no task-ledger binding")
            if request_requirements_ref != manifest_requirements_ref:
                raise ValueError(
                    "cannot recover architecture review: workflow and skeleton task ledgers differ"
                )
            resolution_payload.update(
                {
                    "requirements_ref": manifest_requirements_ref,
                    "triage_repair": "restored_imported_architecture_requirements_binding",
                }
            )
        result = self.repository.dispatch(
            ActionEnvelope(
                action_type="RESOLVE_TRIAGE",
                workflow_id=workflow_id,
                aggregate_type=selected.aggregate_type,
                aggregate_id=selected.aggregate_id,
                actor=actor,
                source_channel=source_channel,
                expected_version=selected.version,
                idempotency_key=f"manual-resolve-triage:{selected.aggregate_id}:{selected.version}",
                payload=resolution_payload,
            )
        )
        return {
            "status": "triage_resolved",
            "workflow_id": workflow_id,
            "subject": _triage_subject(selected),
            "state": result.snapshot.state,
            "resolution": summary,
        }

    def _triage_candidates(self, workflow_id: str) -> tuple[AggregateSnapshot, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self.repository.list_workflow_snapshots(workflow_id)
                    if item.state == "TRIAGE_REQUIRED"
                    and "RESOLVE_TRIAGE"
                    in self.repository.engine.legal_actions(item.aggregate_type, item.state)
                ),
                key=lambda item: (_triage_subject(item).casefold(), item.aggregate_type.value),
            )
        )

    def triage_orphaned_work_aggregates(
        self,
        *,
        workflow_id: str,
        actor: str,
        source_channel: str = "",
    ) -> list[dict[str, str]]:
        """Move worker-owned aggregates with no durable executor into triage."""

        normalized: list[dict[str, str]] = []
        snapshots = self.repository.list_workflow_snapshots(workflow_id)
        for item in snapshots:
            required_states = LIVENESS_REQUIRED_STATES.get(item.aggregate_type, frozenset())
            if item.state not in required_states:
                continue
            if self.repository.aggregate_liveness_sources(
                workflow_id=workflow_id,
                aggregate_type=item.aggregate_type,
                aggregate_id=item.aggregate_id,
                lease_resource_key=str(item.payload.get("lease_resource_key") or ""),
            ):
                continue
            if "ENTER_TRIAGE" not in self.repository.engine.legal_actions(
                item.aggregate_type,
                item.state,
            ):
                continue
            result = self.repository.dispatch(
                ActionEnvelope(
                    action_type="ENTER_TRIAGE",
                    workflow_id=workflow_id,
                    aggregate_type=item.aggregate_type,
                    aggregate_id=item.aggregate_id,
                    actor=actor,
                    source_channel=source_channel,
                    expected_version=item.version,
                    idempotency_key=(
                        f"orphaned-work:{item.aggregate_type.value}:"
                        f"{item.aggregate_id}:{item.version}"
                    ),
                    payload={
                        "blocker": {
                            "kind": "orphaned_worker",
                            "reason": (
                                "worker-owned state has no live lease, pending outbox "
                                "effect, or durable role assignment"
                            ),
                        }
                    },
                )
            )
            normalized.append(
                {
                    "aggregate_type": result.snapshot.aggregate_type.value,
                    "aggregate_id": result.snapshot.aggregate_id,
                    "previous_state": item.state,
                }
            )
        return normalized

    def submit_human_decision(self, request: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(request)
        decision = str(data.get("decision") or "").strip().lower()
        if decision not in {"accept", "edit", "reject"}:
            raise ValueError("human decision must be accept, edit, or reject")
        edit_scope = str(data.get("edit_scope") or "architecture").strip().lower()
        amendment = str(data.get("amendment") or "")
        has_amendment = bool(amendment.strip())
        if decision == "edit":
            if edit_scope not in {"architecture", "requirements"}:
                raise ValueError("edit_scope must be architecture or requirements")
            if edit_scope == "architecture":
                if not str(data.get("edit_instruction") or "").strip():
                    raise ValueError("architecture edit requires edit_instruction")
                if has_amendment:
                    raise ValueError("architecture edit cannot amend the task ledger")
            elif not has_amendment:
                raise ValueError("requirements edit requires amendment prose")
        elif data.get("edit_scope") or has_amendment:
            raise ValueError("edit_scope and task-ledger revisions are valid only for decision=edit")
        self._rebind_human_decision_channel(data)
        token = str(data.get("decision_token") or "")
        if not token:
            token = self.repository.reissue_human_decision_token(
                workflow_id=str(data.get("workflow_id") or ""),
                actor_id=str(data.get("actor") or ""),
                active_channel_id=str(data.get("source_channel") or ""),
            )
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
            if edit_scope == "requirements":
                authority = TaskRevisionAuthority(
                    title="Human review task revision",
                    question=(
                        "What requirement change should supersede the reviewed task "
                        "specification?"
                    ),
                    answer=amendment,
                    observed_at=datetime.now(UTC).isoformat(),
                    origin="human_review_edit",
                )
                requirements_ref = self.task_ledger.append_revision(
                    base_ref=dict(revision.payload.get("requirements_ref") or {}),
                    authority=authority,
                    actor=str(data.get("actor") or "pal"),
                    source_channel=str(data.get("source_channel") or "local"),
                )
                payload["requirements_ref"] = requirements_ref.to_dict()
                payload["task_revision"] = authority.model_dump(mode="json")
                instruction = instruction or (
                    "Revise the existing architecture against the updated append-only task ledger."
                )
            edit_ref = self.artifacts.put_json(
                {
                    "instruction": instruction,
                    "edit_scope": edit_scope,
                },
                artifact_type=(
                    "RequirementsEditInstructionArtifact"
                    if edit_scope == "requirements"
                    else "ArchitectureEditInstructionArtifact"
                ),
                provenance={"actor": data.get("actor"), "source_channel": data.get("source_channel")},
                child_refs=((manifest_sha, "revises"),),
            )
            payload["edit_instruction_ref"] = edit_ref.to_dict()
            payload["edit_scope"] = edit_scope
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
        return {
            "status": "accepted",
            "workflow_id": workflow_id,
            "revision_id": revision_id,
            "state": result.snapshot.state,
            **({"edit_scope": edit_scope} if decision == "edit" else {}),
            **(
                {"requirements_ref": payload["requirements_ref"]}
                if decision == "edit" and edit_scope == "requirements"
                else {}
            ),
        }

    def append_architect_clarification(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Append exact Manager-observed user communication to the task ledger."""

        data = dict(request)
        workflow_id = str(data.get("workflow_id") or "").strip()
        revision_id = str(data.get("architecture_revision_id") or "").strip()
        worker_id = str(data.get("worker_id") or "").strip()
        clarification_id = str(data.get("clarification_id") or "").strip()
        if not workflow_id or not revision_id or not worker_id or not clarification_id:
            raise ValueError(
                "architect clarification requires workflow, revision, worker, and clarification ids"
            )
        revision = self.repository.read_snapshot(
            AggregateType.ARCHITECTURE_REVISION,
            revision_id,
        )
        if revision is None or revision.workflow_id != workflow_id:
            raise ValueError("architect clarification target does not exist")
        if revision.state != "ARCHITECT_RUNNING":
            raise ValueError(
                "architect clarification requires an active ARCHITECT_RUNNING revision"
            )
        if str(revision.payload.get("active_worker_id") or "") != worker_id:
            raise ValueError("architect clarification worker is stale")
        authority = TaskRevisionAuthority.model_validate(
            {
                "title": str(data.get("title") or "").strip(),
                "question": str(data.get("question") or ""),
                "answer": str(data.get("answer") or ""),
                "observed_at": str(data.get("observed_at") or datetime.now(UTC).isoformat()),
                "origin": "architect_user_clarification",
            }
        )
        revision_digest = hashlib.sha256(
            json.dumps(
                authority.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        prior_clarification_id = str(
            revision.payload.get("task_revision_clarification_id") or ""
        )
        if prior_clarification_id == clarification_id:
            if str(revision.payload.get("last_task_revision_digest") or "") != revision_digest:
                raise ValueError(
                    "clarification id was already recorded with a different answer"
                )
            return {
                "appended": True,
                "sequence": int(revision.payload.get("task_revision_sequence") or 0),
                "requirements_ref": dict(revision.payload["requirements_ref"]),
                "duplicate": True,
            }
        next_ref = self.task_ledger.append_revision(
            base_ref=dict(revision.payload.get("requirements_ref") or {}),
            authority=authority,
            actor="minion-manager",
            source_channel="user_clarification",
        )
        task_revision_sequence = len(
            list(
                dict(self.artifacts.read_json(next_ref)).get("revisions")
                or []
            )
        )
        result = self.repository.dispatch(
            ActionEnvelope(
                action_type="TASK_REVISION_APPENDED",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision_id,
                actor="minion-manager",
                expected_version=revision.version,
                idempotency_key=(
                    f"architect-clarification:{clarification_id}"
                ),
                payload={
                    "requirements_ref": next_ref.to_dict(),
                    "task_revision": authority.model_dump(mode="json"),
                    "task_revision_digest": revision_digest,
                    "task_revision_clarification_id": clarification_id,
                    "task_revision_sequence": task_revision_sequence,
                },
            )
        )
        return {
            "appended": True,
            "sequence": int(result.snapshot.payload.get("task_revision_sequence") or 0),
            "requirements_ref": dict(result.snapshot.payload["requirements_ref"]),
        }

    def _rebind_human_decision_channel(self, data: Mapping[str, Any]) -> None:
        workflow_id = str(data.get("workflow_id") or "")
        actor = str(data.get("actor") or "")
        source_channel = str(data.get("source_channel") or "")
        control_route = dict(data.get("control_route") or {})
        if not workflow_id or not actor or not source_channel or not control_route:
            return
        workflow = self._workflow_snapshot(workflow_id)
        if workflow.state != "ACTIVE":
            raise ValueError("human decision channel can only be rebound for an active workflow")
        current_channel = str(workflow.payload.get("active_channel") or "")
        current_route = dict(workflow.payload.get("control_route") or {})
        if current_channel == source_channel and current_route == control_route:
            return
        route_hash = hashlib.sha256(
            json.dumps(control_route, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        self.repository.dispatch(
            ActionEnvelope(
                action_type="REBIND_CHANNEL",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor=actor,
                source_channel=source_channel,
                expected_version=workflow.version,
                idempotency_key=f"rebind-channel:{workflow_id}:{workflow.version}:{source_channel}:{route_hash}",
                payload={"active_channel": source_channel, "control_route": control_route},
            )
        )
        self.repository.bind_channel_workflow(
            actor_id=actor,
            channel_id=source_channel,
            workflow_id=workflow_id,
        )

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

    def _accepted_architecture_revision_for_restart(
        self,
        workflow: AggregateSnapshot,
    ) -> AggregateSnapshot:
        snapshots = self.repository.list_workflow_snapshots(workflow.workflow_id)
        accepted = tuple(
            item
            for item in snapshots
            if item.aggregate_type == AggregateType.ARCHITECTURE_REVISION
            and item.state == "ACCEPTED"
            and item.payload.get("architecture_manifest_ref")
        )
        preferred_id = str(workflow.payload.get("architecture_revision_id") or "")
        preferred = next(
            (item for item in accepted if item.aggregate_id == preferred_id),
            None,
        )
        if preferred is not None:
            return preferred
        epoch_id = str(workflow.payload.get("execution_epoch_id") or "")
        epoch = (
            self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch_id)
            if epoch_id
            else None
        )
        epoch_manifest_sha = str(
            dict((epoch.payload if epoch is not None else {}).get("architecture_manifest_ref") or {}).get(
                "sha256"
            )
            or ""
        )
        matching_epoch = tuple(
            item
            for item in accepted
            if str(dict(item.payload.get("architecture_manifest_ref") or {}).get("sha256") or "")
            == epoch_manifest_sha
        )
        if len(matching_epoch) == 1:
            return matching_epoch[0]
        if len(accepted) == 1:
            return accepted[0]
        if not accepted:
            raise ValueError("workflow has no accepted architecture to restart from")
        raise ValueError(
            "workflow has several accepted architecture revisions and no unique active revision"
        )

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
            review_workspace = self.skeleton.provision_review_worktree(
                artifact=artifact,
                review_name=f"external-{str(record['sha256'])[:16]}",
            )
            try:
                requirements = self.artifacts.read_json(dict(artifact["requirements_ref"]))
                review = review_architecture_skeleton(
                    artifact,
                    worktree=review_workspace.worktree,
                    requirements_payload=requirements,
                )
            finally:
                review_workspace.cleanup()
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


def _validate_start_workflow_shape(data: Mapping[str, Any]) -> None:
    if "source_files" in data:
        raise ValueError(
            "source_files was removed; foreground Pal must synthesize the complete task_spec"
        )
    workspace = data.get("workspace")
    if workspace is not None and not isinstance(workspace, Mapping):
        raise ValueError("workflow workspace must be an object")
    for field_name in (
        "constraints",
        "approved_evidence",
        "references",
    ):
        value = data.get(field_name)
        if value is not None and not isinstance(value, (list, tuple)):
            raise ValueError(f"workflow {field_name} must be an array")
    forbidden = {"sections", "requirements", "strengths", "requirement_files"}.intersection(data)
    if forbidden:
        raise ValueError(
            "V2 workflow input no longer accepts normalized Requirements fields: "
            + ", ".join(sorted(forbidden))
        )
    for field_name in ("control_route",):
        value = data.get(field_name)
        if value is not None and not isinstance(value, Mapping):
            raise ValueError(f"workflow {field_name} must be an object")
    task_spec = data.get("task_spec")
    if task_spec is not None and not isinstance(task_spec, Mapping):
        raise ValueError("workflow task_spec must be an object")


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
    if not str(workspace.get("project_name") or "").strip():
        workspace["project_name"] = inferred_project_name(workspace)
    return workspace


def _normalize_references(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("workflow references must be an array")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value, start=1):
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
    if workflow_state == "RESTARTING":
        return ["wait_for_replacement_workflow", "control_workflow:cancel"]
    if workflow_state == "PAUSED":
        return ["resume_workflow", "control_workflow:cancel"]
    if workflow_state in {"PAUSE_REQUESTED", "CANCEL_REQUESTED"}:
        return ["wait_for_safe_point"]
    if workflow_state == "TRIAGE_REQUIRED" or active_state == "TRIAGE_REQUIRED":
        return ["resolve_triage", "control_workflow:cancel"]
    if active_state == "HUMAN_REVIEW":
        return ["submit_human_decision", "control_workflow:cancel"]
    return ["control_workflow:pause", "control_workflow:cancel"]


def _triage_subject(snapshot: AggregateSnapshot) -> str:
    payload = dict(snapshot.payload or {})
    if snapshot.aggregate_type == AggregateType.DAG_NODE_RUN:
        return str(
            payload.get("module_name")
            or payload.get("verification_name")
            or payload.get("unit_id")
            or "DAG node"
        )
    return {
        AggregateType.WORKFLOW: "workflow",
        AggregateType.ARCHITECTURE_REVISION: "architecture",
        AggregateType.EXECUTION_EPOCH: "execution",
        AggregateType.STANDALONE_REVIEW: "standalone review",
        AggregateType.TASK: "task",
    }.get(snapshot.aggregate_type, snapshot.aggregate_type.value.replace("_", " "))


def _public_triage_candidate(snapshot: AggregateSnapshot) -> dict[str, Any]:
    blocker = dict(snapshot.payload.get("blocker") or {})
    return {
        "subject": _triage_subject(snapshot),
        "kind": snapshot.aggregate_type.value.replace("_", " "),
        "blocker": blocker,
        "resume_state": str(snapshot.payload.get("triage_resume_state") or ""),
    }


def _select_triage_candidate(
    candidates: tuple[AggregateSnapshot, ...],
    *,
    subject: str,
) -> AggregateSnapshot:
    query = " ".join(str(subject or "").split()).casefold()
    if not query:
        if len(candidates) == 1:
            return candidates[0]
        names = ", ".join(_triage_subject(item) for item in candidates)
        raise ValueError(
            "multiple TRIAGE_REQUIRED items exist; select one with subject. "
            f"Available subjects: {names}"
        )
    exact = tuple(
        item
        for item in candidates
        if _triage_subject(item).casefold() == query
    )
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        kinds = ", ".join(
            sorted(item.aggregate_type.value.replace("_", " ") for item in exact)
        )
        raise ValueError(
            f"triage subject {subject!r} is ambiguous across: {kinds}"
        )
    names = ", ".join(_triage_subject(item) for item in candidates)
    raise ValueError(
        f"no TRIAGE_REQUIRED item matches subject {subject!r}. Available subjects: {names}"
    )
