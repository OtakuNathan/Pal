from __future__ import annotations
from pal.bunshin.semantic_orchestration.callbacks import HumanReviewPublisher
from pal.bunshin.semantic_orchestration.callbacks import WorkflowEventPublisher
from pal.bunshin.semantic_orchestration.review_results import _ref_from_mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.contracts import ActionEnvelope, AggregateType, SubmissionInvariantError
from pal.bunshin.contract_protocol import CONTRACT_ARTIFACT
from pal.bunshin.task_ledger import TASK_LEDGER_ARTIFACT
from pal.bunshin.projections import PlanRevisionProjectionStore
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.human_review import HUMAN_REVIEW_RENDER_VERSION, human_review_card_is_current
from pal.bunshin.semantic_orchestration.effect_reads import EffectReads
from pal.bunshin.semantic_orchestration.role_reports import RoleReports
from pal.bunshin.task_ledger import TaskLedgerService


@dataclass
class HumanReview:
    effect_reads: EffectReads
    role_reports: RoleReports
    artifacts: ContentAddressedArtifactStore
    publish_human_review: HumanReviewPublisher | None
    publish_workflow_event: WorkflowEventPublisher | None
    render_human_review: Callable[..., str]
    repository: BunshinRepository
    runtime_root: Path
    task_ledger: TaskLedgerService

    async def publish_human_architecture_review(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        revision = self.effect_reads.effect_snapshot(effect)
        manifest_ref = _ref_from_mapping(revision.payload.get("architecture_manifest_ref"))
        workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, revision.workflow_id)
        if workflow is None:
            raise ValueError("architecture revision has no workflow")
        stored_card = dict(revision.payload.get("human_review_card_ref") or {})
        if stored_card:
            stored_ref = _ref_from_mapping(stored_card)
            stored_payload = dict(self.artifacts.read_json(stored_ref))
            if human_review_card_is_current(
                stored_payload,
                manifest_sha=manifest_ref.sha256,
            ):
                card_ref = stored_ref
                payload = stored_payload
            else:
                stored_card = {}
        if not stored_card:
            actor = str(workflow.payload.get("owner") or "pal")
            record = self.repository.artifacts.read_artifact_record(manifest_ref.sha256)
            if (
                record
                and str(record.get("artifact_type") or "")
                == CONTRACT_ARTIFACT
            ):
                markdown = self.render_human_review(revision)
                payload = {
                    "render_version": HUMAN_REVIEW_RENDER_VERSION,
                    "workflow_id": revision.workflow_id,
                    "architecture_revision_id": revision.aggregate_id,
                    "manifest_sha": manifest_ref.sha256,
                    "actor_id": actor,
                    "markdown": markdown,
                    "actions": ["accept", "edit", "reject"],
                }
            else:
                raise SubmissionInvariantError(
                    "human architecture review requires a ContractArtifact"
                )
            manifest_payload = dict(self.artifacts.read_json(manifest_ref))
            task_ledger_value = manifest_payload.get("requirements_ref")
            replan_batch_value = revision.payload.get("replan_finding_batch_ref")
            card_children = [(manifest_ref.sha256, "architecture_manifest")]
            markdown_ref = self.artifacts.put_bytes(
                str(payload.get("markdown") or "").encode("utf-8"),
                artifact_type="ArchitectureHumanReviewMarkdownArtifact",
                media_type="text/markdown",
                child_refs=((manifest_ref.sha256, "architecture_manifest"),),
            )
            markdown_record = self.repository.artifacts.read_artifact_record(markdown_ref.sha256)
            attachments: list[dict[str, Any]] = []
            if markdown_record is not None:
                attachments.append(
                    {
                        "path": str(markdown_record["storage_path"]),
                        "file_name": "architecture.md",
                        "mime_type": "text/markdown",
                        "caption": "Architecture skeleton, contract graph, and verification scenarios",
                    }
                )
            if isinstance(task_ledger_value, Mapping) and task_ledger_value.get("sha256"):
                task_ledger_ref = _ref_from_mapping(task_ledger_value)
                task_record = self.repository.artifacts.read_artifact_record(task_ledger_ref.sha256)
                if task_record and str(task_record.get("artifact_type") or "") == TASK_LEDGER_ARTIFACT:
                    for source in self.task_ledger.source_attachments(task_ledger_ref):
                        source_ref = _ref_from_mapping(source["artifact_ref"])
                        source_record = self.repository.artifacts.read_artifact_record(source_ref.sha256)
                        if source_record is None:
                            continue
                        attachments.append(
                            {
                                "path": str(source_record["storage_path"]),
                                "file_name": str(source["name"]).replace("/", "__"),
                                "mime_type": str(source["media_type"]),
                                "caption": "Immutable task ledger: original plus ordered revisions",
                            }
                        )
                        card_children.append((source_ref.sha256, "task_ledger"))
            payload["attachments"] = attachments
            card_children.append((markdown_ref.sha256, "architecture_markdown"))
            if replan_batch_value:
                card_children.append(
                    (_ref_from_mapping(replan_batch_value).sha256, "replan_findings")
                )
            payload["decision_token"] = self.repository.human_decisions.issue_human_decision_token(
                workflow_id=revision.workflow_id,
                architecture_revision_id=revision.aggregate_id,
                manifest_sha=manifest_ref.sha256,
                actor_id=actor,
            )
            card_ref = self.artifacts.put_json(
                payload,
                artifact_type="HumanReviewCardArtifact",
                child_refs=tuple(card_children),
            )
            current = self.repository.snapshots.read_snapshot(AggregateType.ARCHITECTURE_REVISION, revision.aggregate_id)
            if current is None:
                raise ValueError("architecture revision disappeared before human review publication")
            persisted_ref = dict(current.payload.get("human_review_card_ref") or {})
            if str(persisted_ref.get("sha256") or "") != card_ref.sha256:
                current = self.repository.transitions.dispatch(
                    ActionEnvelope(
                        action_type="HUMAN_REVIEW_PUBLISHED",
                        workflow_id=revision.workflow_id,
                        aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                        aggregate_id=revision.aggregate_id,
                        actor="bunshin-v2-manager",
                        expected_version=current.version,
                        idempotency_key=(
                            f"human-review-published:v{HUMAN_REVIEW_RENDER_VERSION}:"
                            f"{effect.get('effect_id') or card_ref.sha256}:{card_ref.sha256}"
                        ),
                        payload={"human_review_card_ref": card_ref.to_dict()},
                    )
                ).snapshot
        architecture_artifact = self.role_reports.architecture_artifact_with_runtime_layout(
            revision,
            dict(self.artifacts.read_json(manifest_ref)),
        )
        review_value = revision.payload.get("review_artifact_ref")
        review_payload = (
            dict(self.artifacts.read_json(_ref_from_mapping(review_value)))
            if isinstance(review_value, Mapping) and review_value.get("sha256")
            else {"verdict": "PASS", "findings": []}
        )
        PlanRevisionProjectionStore(self.runtime_root).materialize(
            workflow_id=revision.workflow_id,
            revision_id=revision.aggregate_id,
            architecture_artifact=architecture_artifact,
            markdown=str(payload.get("markdown") or ""),
            review=review_payload,
            status="reviewed_pending_human",
        )
        if self.publish_human_review is not None:
            await self.publish_human_review({**payload, "card_ref": card_ref.to_dict()})
        return {"result_artifact_ref": card_ref.to_dict()}

    def materialize_plan_revision_status(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        revision = self.effect_reads.effect_snapshot(effect)
        manifest_ref = _ref_from_mapping(revision.payload.get("architecture_manifest_ref"))
        artifact = self.role_reports.architecture_artifact_with_runtime_layout(
            revision,
            dict(self.artifacts.read_json(manifest_ref)),
        )
        status = str(effect.get("status") or revision.state.lower())
        if self.publish_workflow_event is not None and status in {
            "accepted",
            "revision_requested",
            "rejected",
        }:
            self.publish_workflow_event(
                {
                    "event_kind": "architecture_review_resolved",
                    "workflow_id": revision.workflow_id,
                    "architecture_revision_id": revision.aggregate_id,
                    "status": status,
                    "summary": f"Bunshin architecture decision recorded ({status}).",
                    "resolved_at": str(getattr(revision, "updated_at", "") or ""),
                }
            )
        root = PlanRevisionProjectionStore(self.runtime_root).update_status(
            workflow_id=revision.workflow_id,
            revision_id=revision.aggregate_id,
            architecture_artifact=artifact,
            status=status,
        )
        return {"status": status, "projection_path": str(root)}

    async def handle_materialize_plan_revision_status(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.materialize_plan_revision_status(effect)
