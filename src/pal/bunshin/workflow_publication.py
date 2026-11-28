"""Publish verified workflow results, Task notifications and reviewable learning."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from pal.bunshin.contracts import ActionEnvelope, AggregateType
from pal.bunshin.delivery import DeliveryReceipt, DirectDeliveryReceipt
from pal.bunshin.workflow_effect_context import WorkflowEffectContext


@dataclass
class WorkflowPublication(WorkflowEffectContext):
    publish_workflow_event: Callable[[Mapping[str, Any]], None] | None = None

    def publish_terminal(self, workflow_id: str) -> None:
        normalized_workflow_id = str(workflow_id or "").strip()
        if self.publish_workflow_event is None or not normalized_workflow_id:
            return
        workflow = self.repository.snapshots.read_snapshot(
            AggregateType.WORKFLOW,
            normalized_workflow_id,
        )
        if workflow is None or workflow.state not in {
            "COMPLETED",
            "REJECTED",
            "CANCELLED",
        }:
            return
        if workflow.payload.get("scheduled_run_id") and workflow.payload.get("replacement_workflow_id"):
            return
        normalized_status = workflow.state.lower()
        result_artifact_ref = dict(
            workflow.payload.get("result_artifact_ref") or {}
        )
        summary = f"Bunshin workflow {normalized_status}."
        attachments: list[dict[str, str]] = []
        background_result: dict[str, Any] = {}
        result_sha = str(result_artifact_ref.get("sha256") or "")
        if normalized_status == "completed" and result_sha:
            record = self.repository.artifacts.read_artifact_record(result_sha)
            if record and str(record.get("artifact_type") or "") == "DeliveryReceiptArtifact":
                payload = self.service.artifacts.read_json(result_artifact_ref)
                verification_ref_value: Mapping[str, Any] = {}
                if payload.get("schema_version") == "5":
                    delivery_receipt = None
                    verification_ref_value = payload["verification_ref"]
                    background_result = {key: payload[key] for key in ("result_text", "next_steps", "verification_status", "files")}
                    summary = str(payload["result_text"])
                    if payload.get("next_steps"):
                        summary += "\n\nNext steps:\n" + "\n".join(str(item) for item in payload["next_steps"])
                    for item in payload["files"]:
                        content = self.service.artifacts.read_bytes(item["artifact_ref"])
                        if hashlib.sha256(content).hexdigest() != item["content_sha256"]:
                            raise IOError("delivery file content hash mismatch")
                        file_record = self.repository.artifacts.read_artifact_record(item["artifact_ref"]["sha256"])
                        if not file_record or file_record["artifact_type"] != "TaskDeliverableArtifact":
                            raise ValueError("task deliverable metadata unavailable")
                        attachments.append({"path": str(file_record["storage_path"]),
                            "file_name": Path(item["repository_path"]).name, "mime_type": item["mime_type"],
                            "caption": f"Task deliverable: {item['repository_path']}"})
                elif payload.get("schema_version") == "4":
                    direct_receipt = DirectDeliveryReceipt.model_validate(payload)
                    verification_ref_value = direct_receipt.verification_ref
                    independently_verified = verification_ref_value.get("artifact_type") != "NullVerificationReceiptArtifact"
                    for item in direct_receipt.files:
                        content = self.service.artifacts.read_bytes(item.artifact_ref)
                        if hashlib.sha256(content).hexdigest() != item.content_sha256:
                            raise IOError("delivery file content hash does not match its receipt")
                        file_record = self.repository.artifacts.read_artifact_record(item.artifact_ref["sha256"])
                        if not file_record or file_record["artifact_type"] != ("VerifiedReportArtifact" if independently_verified else "TaskDeliverableArtifact"):
                            raise ValueError("verified report artifact metadata is unavailable")
                        attachments.append({"path": str(file_record["storage_path"]),
                            "file_name": Path(item.repository_path).name, "mime_type": item.mime_type,
                            "caption": f"{'Verified report' if independently_verified else 'Deliverable (no independent verification)'}: {item.repository_path}"})
                    delivery_receipt = direct_receipt.patch_receipt
                    summary = "Bunshin workflow completed. Verified reports attached." if independently_verified else "Bunshin completed; reports attached without independent verification."
                else:
                    delivery_receipt = DeliveryReceipt.model_validate(payload)
                    verification_ref_value = delivery_receipt.verification_ref
                if delivery_receipt is not None:
                    patch_bytes = self.service.artifacts.read_bytes(delivery_receipt.patch_ref)
                    if (
                        hashlib.sha256(patch_bytes).hexdigest()
                        != delivery_receipt.patch_content_sha256
                    ):
                        raise IOError("delivery patch content hash does not match its receipt")
                    patch_record = self.repository.artifacts.read_artifact_record(
                        str(delivery_receipt.patch_ref.get("sha256") or "")
                    )
                    if (
                        patch_record is None
                        or str(patch_record.get("artifact_type") or "")
                        != "GitFormatPatchArtifact"
                    ):
                        raise ValueError("delivery patch artifact metadata is unavailable")
                    patch_path = Path(str(patch_record.get("storage_path") or ""))
                    if not patch_path.is_file():
                        raise FileNotFoundError("delivery patch artifact file is unavailable")
                    friendly_name = Path(delivery_receipt.patch_path).name
                    independently_verified = verification_ref_value.get("artifact_type") != "NullVerificationReceiptArtifact"
                    summary = ("Bunshin workflow completed. Verified reports and Git patch attached." if attachments else "Bunshin workflow completed. Verified Git patch attached.") if independently_verified else "Bunshin completed; Git patch attached without independent verification."
                    attachments.append(
                        {
                            "path": str(patch_path),
                            "file_name": friendly_name,
                            "mime_type": "text/x-patch",
                            "caption": (
                                ("Bunshin verified patch for commit " if independently_verified else "Bunshin patch (no independent verification) for commit ")
                                + f"{delivery_receipt.commit_sha[:12]}"
                            ),
                        }
                    )
                report_attachment = self._verification_report_attachment(verification_ref_value)
                if report_attachment is not None:
                    attachments.append(report_attachment)
        revision_id = str(workflow.payload.get("architecture_revision_id") or "").strip()
        resolved_interactions = (
            [
                {
                    "interaction_id": f"bunshin_v2_architecture_{revision_id}",
                    "interaction_kind": "bunshin_architecture_review",
                }
            ]
            if revision_id
            else []
        )
        resolved_interactions.extend(self.repository.interaction_waits.unresolved_cards(normalized_workflow_id))
        event = {
            "workflow_id": normalized_workflow_id,
            "status": normalized_status,
            "summary": summary,
            "terminal_at": workflow.updated_at,
            "resolved_interactions": resolved_interactions,
            "result_artifact_ref": result_artifact_ref,
            **({"background_handoff": True} if workflow.payload.get("background_handoff") else {}),
        }
        if attachments:
            event["attachments"] = attachments
        if workflow.payload.get("background_handoff"):
            from pal.foundation.background_inbox import BackgroundInbox
            BackgroundInbox(self.service.runtime_root).publish(
                normalized_workflow_id,
                {"task": workflow.payload.get("scheduled_task_name") or workflow.payload.get("workflow_name"), "run_id": workflow.payload.get("scheduled_run_id", normalized_workflow_id),
                 "status": normalized_status, "result_text": summary,
                 "result_artifact_ref": result_artifact_ref, **background_result},
                delivery_status=("discarded" if workflow.payload.get("discard_output") else "not_requested")
                if workflow.payload.get("scheduled_run_id") and not workflow.payload.get("channel_delivery_requested") else "pending",
            )
        self.publish_workflow_event(event)

    def _verification_report_attachment(
        self,
        verification_ref: Mapping[str, Any],
    ) -> dict[str, str] | None:
        verification_sha = str(dict(verification_ref or {}).get("sha256") or "").strip()
        if not verification_sha:
            return None
        verification_record = self.repository.artifacts.read_artifact_record(verification_sha)
        if not verification_record or str(verification_record.get("artifact_type") or "") != "VerificationArtifact":
            return None
        verification = self.service.artifacts.read_json(verification_ref)
        report_ref = dict(verification.get("verification_report_ref") or {})
        report_sha = str(report_ref.get("sha256") or "").strip()
        if not report_sha:
            return None
        report_record = self.repository.artifacts.read_artifact_record(report_sha)
        if not report_record or str(report_record.get("artifact_type") or "") != "VerificationReportArtifact":
            return None
        report_bytes = self.service.artifacts.read_bytes(report_ref)
        expected_sha = str(verification.get("verification_report_sha256") or "").strip()
        if expected_sha and hashlib.sha256(report_bytes).hexdigest() != expected_sha:
            raise IOError("verification report content hash does not match its artifact")
        storage_path = Path(str(report_record.get("storage_path") or ""))
        if not storage_path.is_file():
            raise FileNotFoundError("verification report artifact file is unavailable")
        module_name = str(verification.get("module_name") or "").strip()
        relpath = str(verification.get("verification_report_path") or "verification_report.yaml").strip()
        file_name = f"{module_name}_verification_report.yaml" if module_name else "verification_report.yaml"
        return {
            "path": str(storage_path),
            "file_name": file_name,
            "mime_type": "text/yaml",
            "caption": f"Verification report ({module_name or 'module'}): {relpath}",
        }

    def publish_memory_candidate(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, node.workflow_id)
        if workflow is not None and (workflow.payload.get("memory_policy") == "disabled" or workflow.payload.get("scheduled_run_id")):
            return {}
        if node.state != "ACCEPTED" or str(node.payload.get("node_kind") or "unit") != "unit":
            return {}
        if node.payload.get("memory_candidate_ref"):
            return {"result_artifact_ref": dict(node.payload["memory_candidate_ref"])}
        epoch_id = str(node.payload.get("epoch_id") or "")
        memory_candidate_ref = self.service.artifacts.put_json(
            {
                "schema_version": "1",
                "candidate_kind": "accepted_unit_learning",
                "review_status": "pending_human_review",
                "workflow_id": node.workflow_id,
                "epoch_id": epoch_id,
                "node_run_id": node.aggregate_id,
                "unit_id": str(node.payload.get("unit_id") or ""),
                "unit_contract_ref": dict(node.payload.get("unit_contract_ref") or {}),
                "candidate_ref": dict(node.payload.get("candidate_ref") or {}),
                "verification_artifact_ref": dict(node.payload.get("verification_artifact_ref") or {}),
                "historical_repair_bill_refs": [
                    dict(item)
                    for item in list(node.payload.get("historical_repair_bill_refs") or [])
                    if isinstance(item, Mapping)
                ],
                "architecture_manifest_ref": dict(node.payload.get("architecture_manifest_ref") or {}),
                "publication_rule": "Pal or the user must review this candidate before long-term memory publication.",
                "validity_rule": "Publish only while this node remains ACCEPTED in the active execution epoch; STALE or superseded nodes invalidate the candidate.",
            },
            artifact_type="AcceptedModuleMemoryCandidateArtifact",
            child_refs=tuple(
                [
                    (str(ref.get("sha256")), relation)
                    for relation, ref in (
                        ("unit_contract", dict(node.payload.get("unit_contract_ref") or {})),
                        ("candidate", dict(node.payload.get("candidate_ref") or {})),
                        ("verification", dict(node.payload.get("verification_artifact_ref") or {})),
                        ("architecture_manifest", dict(node.payload.get("architecture_manifest_ref") or {})),
                    )
                    if ref.get("sha256")
                ]
                + [
                    (str(ref.get("sha256")), "repair_bill")
                    for ref in list(node.payload.get("historical_repair_bill_refs") or [])
                    if isinstance(ref, Mapping) and ref.get("sha256")
                ]
            ),
            provenance={"workflow_id": node.workflow_id, "node_run_id": node.aggregate_id},
        )
        current = self.repository.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, node.aggregate_id)
        if current is not None and not current.payload.get("memory_candidate_ref"):
            self.repository.transitions.dispatch(
                ActionEnvelope(
                    action_type="MEMORY_CANDIDATE_PUBLISHED",
                    workflow_id=node.workflow_id,
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node.aggregate_id,
                    actor="bunshin-outbox",
                    expected_version=current.version,
                    idempotency_key=f"effect:{effect['effect_key']}:memory-candidate",
                    payload={"memory_candidate_ref": memory_candidate_ref.to_dict()},
                )
            )
        return {"result_artifact_ref": memory_candidate_ref.to_dict()}
