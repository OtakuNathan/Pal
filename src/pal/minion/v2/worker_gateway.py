from __future__ import annotations

import os
import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pal.minion.ipc import MinionWorkerGatewayClient
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.artifacts import ArtifactRef
from pal.minion.v2.submission_drafts import (
    SubmissionDraftContext,
    SubmissionDraftSnapshot,
    SubmissionDraftStore,
)
from pal.minion.v2.worker_protocol import stable_hash


WORKER_GATEWAY_TOKEN_ENV = "PAL_MINION_ASSIGNMENT_TOKEN"

_SUBMISSION_ARTIFACT_TYPES = {
    "architecture": "ArchitectureWorkerSubmissionArtifact",
    "architecture_review": "ArchitectureReviewWorkerSubmissionArtifact",
    "candidate": "CandidateWorkerSubmissionArtifact",
    "contract": "ContractWorkerSubmissionArtifact",
    "requirements": "RequirementsWorkerSubmissionArtifact",
    "standalone_review": "StandaloneReviewSubmissionArtifact",
    "verification": "VerifierSubmissionArtifact",
}


def worker_gateway_client_from_env(runtime_root: Path) -> MinionWorkerGatewayClient | None:
    token = str(os.environ.get(WORKER_GATEWAY_TOKEN_ENV) or "").strip()
    if not token:
        return None
    return MinionWorkerGatewayClient(Path(runtime_root), token)


@dataclass
class WorkerAssignmentGateway:
    """Narrow Manager-owned state surface exposed to sandboxed workers."""

    service: MinionV2WorkflowService

    @property
    def repository(self):
        return self.service.repository

    def authorize(self, access_token: str) -> dict[str, Any]:
        return self.repository.authenticate_worker_attempt(access_token)

    def call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(params or {})
        authenticated = self.authorize(str(payload.pop("access_token", "")))
        if method == "bound_input_read":
            return self._read_bound_input(authenticated, payload)
        if method == "bound_input_json":
            return self._read_bound_input_json(authenticated, payload)
        if method == "draft_read":
            return self._draft_read(authenticated, payload)
        if method == "draft_mutate":
            return self._draft_mutate(authenticated, payload)
        if method == "draft_submit":
            return self._draft_submit(authenticated, payload)
        if method == "artifact_put":
            return self._artifact_put(authenticated, payload)
        raise ValueError(f"worker gateway method is not allowed: {method}")

    def _read_bound_input(
        self,
        authenticated: Mapping[str, Any],
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        assignment = dict(authenticated["assignment"])
        name = str(params.get("name") or "").strip()
        refs = dict(assignment.get("input_refs") or {})
        ref = dict(refs.get(name) or {})
        if not ref:
            available = ", ".join(sorted(refs)) or "(none)"
            raise ValueError(
                f"unknown bound input: {name or '<missing>'}; available: {available}"
            )
        data = self.service.artifacts.read_bytes(ref)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"bound input {name!r} is not UTF-8 text") from exc
        start_line = max(1, int(params.get("start_line") or 1))
        limit_lines = min(4000, max(1, int(params.get("limit_lines") or 2000)))
        lines = text.splitlines()
        selected = lines[start_line - 1 : start_line - 1 + limit_lines]
        rendered = "\n".join(
            f"{line_number:>6}  {line}"
            for line_number, line in enumerate(selected, start=start_line)
        )
        self.repository.record_worker_input_read(
            assignment_id=str(assignment["assignment_id"]),
            attempt_id_value=str(authenticated["attempt_id"]),
            input_name=name,
            artifact_sha256=str(ref["sha256"]),
            fencing_token=int(authenticated["fencing_token"]),
        )
        return {
            "content": rendered,
            "input_name": name,
            "start_line": start_line,
            "returned_lines": len(selected),
            "total_lines": len(lines),
            "has_more": start_line - 1 + len(selected) < len(lines),
        }

    def _read_bound_input_json(
        self,
        authenticated: Mapping[str, Any],
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        assignment = dict(authenticated["assignment"])
        name = str(params.get("name") or "").strip()
        refs = dict(assignment.get("input_refs") or {})
        ref = dict(refs.get(name) or {})
        if not ref:
            raise ValueError(f"unknown bound input: {name or '<missing>'}")
        value = self.service.artifacts.read_json(ref)
        if not isinstance(value, Mapping):
            raise ValueError(f"bound input {name!r} must contain a JSON object")
        self.repository.record_worker_input_read(
            assignment_id=str(assignment["assignment_id"]),
            attempt_id_value=str(authenticated["attempt_id"]),
            input_name=name,
            artifact_sha256=str(ref["sha256"]),
            fencing_token=int(authenticated["fencing_token"]),
        )
        return {"value": dict(value)}

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
            raise ValueError("worker submission must be a JSON object")
        payload = dict(submission)
        artifact_type = _SUBMISSION_ARTIFACT_TYPES.get(context.draft_kind)
        if artifact_type is None:
            raise ValueError(f"unsupported worker submission kind: {context.draft_kind}")
        artifact_ref = self.service.artifacts.put_json(
            payload,
            artifact_type=artifact_type,
            provenance={
                "workflow_id": assignment["workflow_id"],
                "aggregate_type": assignment["aggregate_type"],
                "aggregate_id": assignment["aggregate_id"],
                "role": assignment["role"],
            },
        )
        payload_hash = stable_hash(payload)
        store = SubmissionDraftStore(self.service.runtime_root)
        store.mark_submitted(
            context,
            expected_version=int(params.get("expected_version") or 0),
            submission_artifact_ref=artifact_ref.to_dict(),
            submission_payload_hash=payload_hash,
        )
        self.repository.record_worker_submission(
            assignment_id=str(assignment["assignment_id"]),
            attempt_id_value=str(authenticated["attempt_id"]),
            fencing_token=int(authenticated["fencing_token"]),
            artifact_ref=artifact_ref.to_dict(),
            payload_hash=payload_hash,
            settlement_action={
                "action_type": "SETTLE_WORKER_SUBMISSION",
                "aggregate_type": assignment["aggregate_type"],
                "aggregate_id": assignment["aggregate_id"],
                "submission_kind": assignment["submission_kind"],
            },
        )
        return {"submitted": True}

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
            raise ValueError("worker artifact payload is not valid base64") from exc
        if len(data) > 16 * 1024 * 1024:
            raise ValueError("worker artifact exceeds the 16 MiB gateway limit")
        artifact_type = str(params.get("artifact_type") or "").strip()
        if not artifact_type.endswith("Artifact"):
            raise ValueError("worker artifact type must end with Artifact")
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
            "input_fingerprint": str(assignment["input_fingerprint"]),
        }
        actual = {
            "workflow_id": context.workflow_id,
            "invocation_id": context.invocation_id,
            "lease_resource_key": context.lease_resource_key,
            "fencing_token": context.fencing_token,
            "role": context.role,
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
class WorkerGatewayArtifactStore:
    client: MinionWorkerGatewayClient

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
