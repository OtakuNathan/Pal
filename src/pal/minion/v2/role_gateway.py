from __future__ import annotations

import os
import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pal.minion.ipc import ROLE_GATEWAY_TOKEN_ENV, MinionRoleGatewayClient
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.artifacts import ArtifactRef
from pal.minion.v2.submission_drafts import (
    SubmissionDraftContext,
    SubmissionDraftSnapshot,
    SubmissionDraftStore,
)
from pal.minion.v2.role_protocol import RoleAssignmentState, stable_hash

ROLE_SUBMISSION_ARTIFACT_TYPES = {
    "architecture": "ArchitectureRoleSubmissionArtifact",
    "architecture_review": "ArchitectureReviewRoleSubmissionArtifact",
    "candidate": "CandidateRoleSubmissionArtifact",
    "contract": "ContractRoleSubmissionArtifact",
    "standalone_review": "StandaloneReviewRoleSubmissionArtifact",
    # The submission kind is shared by data-driven families. Its payload is
    # role-specific; Manager compilation produces the final typed artifact.
    "verification": "VerifierRoleSubmissionArtifact",
}


def role_submission_artifact_type(submission_kind: str) -> str:
    return str(ROLE_SUBMISSION_ARTIFACT_TYPES.get(str(submission_kind or "")) or "")


def role_gateway_client_from_env(runtime_root: Path) -> MinionRoleGatewayClient | None:
    token = str(os.environ.get(ROLE_GATEWAY_TOKEN_ENV) or "").strip()
    if not token:
        return None
    return MinionRoleGatewayClient(Path(runtime_root), token)


@dataclass
class RoleAssignmentGateway:
    """Narrow Manager-owned state surface exposed to sandboxed role invocations."""

    service: MinionV2WorkflowService

    @property
    def repository(self):
        return self.service.repository

    def authorize(self, access_token: str) -> dict[str, Any]:
        return self.repository.authenticate_role_attempt(access_token)

    def call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(params or {})
        authenticated = self.authorize(str(payload.pop("access_token", "")))
        if method == "submission_status":
            return self._submission_status(authenticated)
        if method == "draft_read":
            return self._draft_read(authenticated, payload)
        if method == "draft_mutate":
            return self._draft_mutate(authenticated, payload)
        if method == "draft_submit":
            return self._draft_submit(authenticated, payload)
        if method == "artifact_put":
            return self._artifact_put(authenticated, payload)
        raise ValueError(f"role gateway method is not allowed: {method}")

    def _submission_status(
        self,
        authenticated: Mapping[str, Any],
    ) -> dict[str, Any]:
        assignment = self.repository.read_role_assignment(
            str(dict(authenticated["assignment"])["assignment_id"])
        )
        if assignment is None:
            raise ValueError("role assignment is unavailable")
        state = str(assignment.get("state") or "")
        recorded = state in {
            RoleAssignmentState.RESULT_RECORDED.value,
            RoleAssignmentState.SETTLED.value,
        } and bool(assignment.get("submission_artifact_ref")) and bool(
            assignment.get("submission_payload_hash")
        )
        return {"recorded": recorded, "state": state}

    def _draft_read(
        self,
        authenticated: Mapping[str, Any],
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        context = self._context(authenticated, params)
        snapshot = SubmissionDraftStore(self.service.runtime_root).read(
            context,
            seed=dict(params.get("seed") or {}),
        )
        return {"snapshot": snapshot.to_dict()}

    def _draft_mutate(
        self,
        authenticated: Mapping[str, Any],
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        context = self._context(authenticated, params)
        result = SubmissionDraftStore(self.service.runtime_root).mutate_precomputed(
            context,
            operation_key=str(params.get("operation_key") or ""),
            request=dict(params.get("request") or {}),
            expected_version=int(params.get("expected_version") or 0),
            next_payload=dict(params.get("next_payload") or {}),
            result=dict(params.get("result") or {}),
            seed=dict(params.get("seed") or {}),
        )
        return {"result": dict(result)}

    def _draft_submit(
        self,
        authenticated: Mapping[str, Any],
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        assignment = dict(authenticated["assignment"])
        context = self._context(authenticated, params)
        submission = params.get("submission")
        if not isinstance(submission, Mapping):
            raise ValueError("role submission must be a JSON object")
        payload = dict(submission)
        artifact_type = role_submission_artifact_type(context.draft_kind)
        if not artifact_type:
            raise ValueError(f"unsupported role submission kind: {context.draft_kind}")
        store = SubmissionDraftStore(self.service.runtime_root)
        snapshot = store.read(context, seed={})
        if (
            context.draft_kind == "candidate"
            and str(assignment.get("role") or "") == "implementation"
            and str(assignment.get("mode") or "") == "repair"
            and str(payload.get("status") or "") == "candidate_ready"
        ):
            self._validate_repair_candidate(
                assignment=assignment,
                draft=snapshot,
                submission=payload,
            )
        artifact_ref = self.service.artifacts.put_json(
            payload,
            artifact_type=artifact_type,
            provenance={
                "workflow_id": assignment["workflow_id"],
                "aggregate_type": assignment["aggregate_type"],
                "aggregate_id": assignment["aggregate_id"],
                "role": assignment["role"],
                "mode": assignment["mode"],
            },
        )
        payload_hash = stable_hash(payload)
        self.repository.record_role_submission(
            assignment_id=str(assignment["assignment_id"]),
            attempt_id_value=str(authenticated["attempt_id"]),
            fencing_token=int(authenticated["fencing_token"]),
            artifact_ref=artifact_ref.to_dict(),
            payload_hash=payload_hash,
            settlement_action={
                "action_type": "SETTLE_ROLE_SUBMISSION",
                "aggregate_type": assignment["aggregate_type"],
                "aggregate_id": assignment["aggregate_id"],
                "submission_kind": assignment["submission_kind"],
            },
        )
        # The assignment receipt is the canonical completion boundary. Freeze
        # the authoring draft only after Manager validation has accepted it so
        # a rejected submit remains editable and retryable in the same process.
        store.mark_submitted(
            context,
            expected_version=int(params.get("expected_version") or 0),
            submission_artifact_ref=artifact_ref.to_dict(),
            submission_payload_hash=payload_hash,
        )
        return {
            "submitted": True,
            "submission_artifact_ref": artifact_ref.to_dict(),
            "submission_payload_hash": payload_hash,
        }

    def _validate_repair_candidate(
        self,
        *,
        assignment: Mapping[str, Any],
        draft: SubmissionDraftSnapshot,
        submission: Mapping[str, Any],
    ) -> None:
        from pal.minion.v2.semantic_evidence import recorded_cases
        from pal.minion.v2.verification import repair_checklist_items

        repair_ref = dict(dict(assignment.get("input_refs") or {}).get("repair_bill") or {})
        if not repair_ref:
            raise ValueError("repair assignment is missing its bound RepairBill")
        bill = self.service.artifacts.read_json(repair_ref)
        semantic_packet = (
            str(dict(bill).get("artifact_kind") or "")
            == "semantic_repair_packet"
        )
        required = [str(item["case"]) for item in repair_checklist_items(bill)]
        if not required and not semantic_packet:
            raise ValueError("bound RepairBill contains no named regression cases")
        cases = recorded_cases(draft.payload)
        status_by_name = {
            str(item.get("name") or ""): str(item.get("status") or "")
            for item in cases
        }
        incomplete = [name for name in required if status_by_name.get(name) != "PASS"]
        if incomplete:
            raise ValueError(
                "repair checklist still requires Manager-recorded PASS regressions: "
                + ", ".join(incomplete)
            )
        if semantic_packet and not any(
            str(item.get("status") or "") == "PASS" for item in cases
        ):
            raise ValueError(
                "semantic repair requires a Manager-recorded PASS developer check"
            )
        expected_tests = [
            f"{item.get('name')}: {item.get('status')}" for item in cases
        ]
        if list(submission.get("tests_run") or []) != expected_tests:
            raise ValueError("repair Candidate tests_run does not match its durable evidence Draft")

    def _artifact_put(
        self,
        authenticated: Mapping[str, Any],
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        assignment = dict(authenticated["assignment"])
        encoded = str(params.get("data_base64") or "")
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ValueError("role artifact payload is not valid base64") from exc
        if len(data) > 16 * 1024 * 1024:
            raise ValueError("role artifact exceeds the 16 MiB gateway limit")
        artifact_type = str(params.get("artifact_type") or "").strip()
        if not artifact_type.endswith("Artifact"):
            raise ValueError("role artifact type must end with Artifact")
        ref = self.service.artifacts.put_bytes(
            data,
            artifact_type=artifact_type,
            schema_version=str(params.get("schema_version") or "1"),
            media_type=str(params.get("media_type") or "application/octet-stream"),
            provenance={
                "workflow_id": assignment["workflow_id"],
                "aggregate_type": assignment["aggregate_type"],
                "aggregate_id": assignment["aggregate_id"],
                "role": assignment["role"],
                "mode": assignment["mode"],
            },
        )
        return {"artifact_ref": ref.to_dict()}

    @staticmethod
    def _context(
        authenticated: Mapping[str, Any],
        params: Mapping[str, Any],
    ) -> SubmissionDraftContext:
        assignment = dict(authenticated["assignment"])
        context = SubmissionDraftContext.from_mapping(dict(params.get("context") or {}))
        expected = {
            "workflow_id": str(assignment["workflow_id"]),
            "invocation_id": str(authenticated["attempt_id"]),
            "lease_resource_key": str(authenticated["lease_resource_key"]),
            "fencing_token": int(authenticated["fencing_token"]),
            "role": str(assignment["role"]),
            "mode": str(assignment["mode"]),
            "input_fingerprint": str(assignment["input_fingerprint"]),
        }
        actual = {
            "workflow_id": context.workflow_id,
            "invocation_id": context.invocation_id,
            "lease_resource_key": context.lease_resource_key,
            "fencing_token": context.fencing_token,
            "role": context.role,
            "mode": context.mode,
            "input_fingerprint": context.input_fingerprint,
        }
        if actual != expected:
            raise ValueError("submission Draft context does not match its assignment")
        if context.draft_kind != str(assignment["submission_kind"]):
            raise ValueError("submission Draft kind does not match its assignment")
        return context


def decode_remote_draft_snapshot(value: Mapping[str, Any]) -> SubmissionDraftSnapshot:
    return SubmissionDraftSnapshot.from_mapping(dict(value.get("snapshot") or {}))


@dataclass
class RoleGatewayArtifactStore:
    client: MinionRoleGatewayClient

    def put_bytes(
        self,
        data: bytes,
        *,
        artifact_type: str,
        schema_version: str = "1",
        media_type: str = "application/octet-stream",
        provenance: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        child_refs: tuple[tuple[str, str], ...] = (),
    ) -> ArtifactRef:
        _ = provenance, metadata, child_refs
        response = self.client.request_sync(
            "artifact_put",
            {
                "data_base64": base64.b64encode(bytes(data)).decode("ascii"),
                "artifact_type": str(artifact_type),
                "schema_version": str(schema_version),
                "media_type": str(media_type),
            },
        )
        return ArtifactRef.from_mapping(dict(response.get("artifact_ref") or {}))
