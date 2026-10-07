from __future__ import annotations
from pal.bunshin.semantic_orchestration.workflow_requests import WorkflowRequests
from pal.bunshin.semantic_orchestration.callbacks import HumanReviewPublisher
from pal.bunshin.semantic_orchestration.review_results import _ref_from_mapping
from pal.bunshin.semantic_orchestration.review_results import _compile_standalone_review_markdown
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from pal.bunshin.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType
from pal.bunshin.contract_protocol import CONTRACT_ARTIFACT
from pal.bunshin.repository import BunshinV2Repository
from pal.bunshin.verification import VerificationStatus
from pal.bunshin.semantic_orchestration.effect_reads import EffectReads


@dataclass
class ReviewDelivery:
    effect_reads: EffectReads
    artifacts: ContentAddressedArtifactStore
    publish_human_review: HumanReviewPublisher | None
    repository: BunshinV2Repository
    requests: WorkflowRequests

    async def publish_standalone_report(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        review = self.effect_reads.effect_snapshot(effect)
        report_ref = dict(review.payload.get("verification_artifact_ref") or {})
        report = self.artifacts.read_json(report_ref)
        workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, review.workflow_id)
        workflow_request = self.requests.read(workflow)
        if self.publish_human_review is not None:
            await self.publish_human_review(
                {
                    "workflow_id": review.workflow_id,
                    "standalone_review_id": review.aggregate_id,
                    "report_ref": report_ref,
                    "summary": _compile_standalone_review_markdown(report),
                }
            )
        current = self.repository.snapshots.read_snapshot(AggregateType.STANDALONE_REVIEW, review.aggregate_id)
        action_type = "ACKNOWLEDGE_REPORT"
        payload: dict[str, Any] = {}
        if (
            str(workflow_request.get("operation") or "") == "review_and_repair"
            and str(report.get("status") or "") == VerificationStatus.FAIL
        ):
            manifest_ref = _ref_from_mapping(
                review.payload.get("review_request_ref")
            )
            record = self.repository.artifacts.read_artifact_record(manifest_ref.sha256)
            if (
                record is None
                or str(record.get("artifact_type") or "")
                != CONTRACT_ARTIFACT
            ):
                raise ValueError(
                    "review_and_repair requires a ContractArtifact; "
                    "reviewers may not invent a repair contract"
                )
            repair_bill_ref = self.publish_standalone_repair_bill(
                report_ref=report_ref,
                manifest_ref=manifest_ref,
            )
            action_type = "HANDOFF_REPAIR"
            payload["architecture_manifest_ref"] = manifest_ref.to_dict()
            payload["repair_bill_ref"] = repair_bill_ref.to_dict()
        self.repository.transitions.dispatch(
            ActionEnvelope(
                action_type=action_type,
                workflow_id=review.workflow_id,
                aggregate_type=AggregateType.STANDALONE_REVIEW,
                aggregate_id=review.aggregate_id,
                actor="bunshin-v2-manager",
                expected_version=current.version,
                idempotency_key=f"effect:{effect['effect_key']}:ack",
                payload=payload,
            )
        )
        return {"result_artifact_ref": report_ref}

    def publish_standalone_repair_bill(
        self,
        *,
        report_ref: Mapping[str, Any],
        manifest_ref: ArtifactRef,
    ) -> ArtifactRef:
        report = dict(self.artifacts.read_json(report_ref))
        findings = [dict(item or {}) for item in list(report.get("findings") or [])]
        if not findings:
            raise ValueError("review_and_repair FAIL requires at least one semantic finding")
        artifact = dict(self.artifacts.read_json(manifest_ref))
        contract = dict(artifact.get("contract") or {})
        modules = dict(contract.get("modules") or {})
        implementation_modules = {
            name: module
            for name, module in modules.items()
            if str(dict(module or {}).get("execution") or "") == "produce"
        }
        if len(implementation_modules) != 1:
            raise ValueError("review_and_repair requires exactly one bounded module")
        module_name = next(iter(implementation_modules))
        finding = findings[0]
        payload = {
            "schema_version": "2",
            "artifact_kind": "structured_repair_bill",
            "module_name": module_name,
            "route": "module_repair",
            "findings": findings,
            "expected": "The accepted skeleton contract and Requirements are satisfied.",
            "actual": str(finding.get("summary") or "Review failed."),
            "regression_test_obligation": {
                "instruction": "Reproduce this standalone finding before repair and preserve the probe as a regression."
            },
        }
        return self.artifacts.put_json(
            payload,
            artifact_type="RepairBillArtifact",
            provenance={"owner": "manager", "source": "standalone_review"},
            child_refs=((str(report_ref.get("sha256") or ""), "standalone_review"),),
        )
