from __future__ import annotations

import os
import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pal.execution.git_tool import GitTool, classify_git_command
from pal.minion.ipc import ROLE_GATEWAY_TOKEN_ENV, MinionRoleGatewayClient
from pal.minion.v2.architecture_templates import (
    compiled_architecture_definition_from_mapping,
)
from pal.minion.v2.artifacts import ArtifactRef
from pal.minion.v2.contract_protocol import (
    ARCHITECT_FILENAME,
    validate_contract_payload,
)
from pal.minion.v2.execution_state import (
    ManagerLogicalExecutionState,
    pager_read_to_dict,
)
from pal.minion.v2.role_contracts import validate_family_binding_payload
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.submission_drafts import (
    SubmissionDraftContext,
    SubmissionDraftSnapshot,
    SubmissionDraftStore,
)
from pal.minion.v2.role_protocol import RoleAssignmentState, stable_hash
from pal.minion.v2.work_items import assert_work_items_complete

ROLE_SUBMISSION_ARTIFACT_TYPES = {
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
        if method == "git_read":
            return self._git_read(authenticated, payload)
        if method == "execution_context":
            return {
                "context": self._execution_state(
                    authenticated, payload
                ).context().to_dict()
            }
        if method == "execution_begin_input":
            return {
                "context": self._execution_state(
                    authenticated, payload
                ).begin_input(
                    input_id=str(payload.get("input_id") or ""),
                    retention_user_turns=int(
                        payload.get("retention_user_turns") or 5
                    ),
                ).to_dict()
            }
        if method == "execution_reconcile_projection":
            return {
                "context": self._execution_state(
                    authenticated, payload
                ).reconcile_projection(
                    projection=tuple(
                        str(item)
                        for item in list(payload.get("projection") or ())
                    ),
                    deliveries=tuple(
                        dict(item)
                        for item in list(payload.get("deliveries") or ())
                        if isinstance(item, Mapping)
                    ),
                ).to_dict()
            }
        if method == "execution_store_pager":
            from pal.execution.session_state import PagerHandleManifest

            state = self._execution_state(authenticated, payload)
            manifest = PagerHandleManifest.from_dict(
                dict(payload.get("manifest") or {})
            )
            stored = state.store_pager(manifest)
            return {"manifest": stored.to_dict(include_payload=False)}
        if method == "execution_read_pager":
            state = self._execution_state(authenticated, payload)
            return pager_read_to_dict(
                state.read_pager(
                    result_ref=str(payload.get("result_ref") or ""),
                    page=int(payload.get("page") or 1),
                    page_size=(
                        int(payload["page_size"])
                        if payload.get("page_size") is not None
                        else None
                    ),
                    anchor=str(payload.get("anchor") or "head"),
                )
            )
        if method == "execution_file_grant":
            grant = self._execution_state(
                authenticated, payload
            ).file_grant(
                file_key=str(payload.get("file_key") or ""),
                digest=str(payload.get("digest") or ""),
            )
            return {
                "grant": (
                    {
                        "file_key": grant.file_key,
                        "digest": grant.digest,
                        "total_lines": grant.total_lines,
                        "covered_ranges": [
                            list(item) for item in grant.covered_ranges
                        ],
                        "empty_file": grant.empty_file,
                        "line_fragments": [
                            list(item) for item in grant.line_fragments
                        ],
                    }
                    if grant is not None
                    else None
                )
            }
        if method == "execution_file_snapshot":
            snapshot = self._execution_state(
                authenticated, payload
            ).file_snapshot(
                file_key=str(payload.get("file_key") or ""),
                digest=str(payload.get("digest") or ""),
            )
            return {
                "snapshot": (
                    snapshot.to_dict() if snapshot is not None else None
                )
            }
        if method == "execution_set_file_snapshot":
            self._execution_state(authenticated, payload).set_file_snapshot(
                file_key=str(payload.get("file_key") or ""),
                digest=str(payload.get("digest") or ""),
                total_lines=int(payload.get("total_lines") or 0),
                complete=bool(payload.get("complete")),
                source=str(payload.get("source") or "mutation"),
            )
            return {"ok": True}
        if method == "execution_invalidate_file":
            self._execution_state(authenticated, payload).invalidate_file(
                file_key=str(payload.get("file_key") or "")
            )
            return {"ok": True}
        if method == "execution_retire":
            self._execution_state(authenticated, payload).retire()
            return {"ok": True}
        raise ValueError(f"role gateway method is not allowed: {method}")

    def _execution_state(
        self,
        authenticated: Mapping[str, Any],
        params: Mapping[str, Any],
    ) -> ManagerLogicalExecutionState:
        assignment = dict(authenticated["assignment"])
        session_id = str(assignment.get("session_id") or "")
        requested = str(params.get("logical_session_id") or session_id)
        if not session_id or requested != session_id:
            raise ValueError(
                "logical execution state does not match the authenticated role session"
            )
        return ManagerLogicalExecutionState(self.service, session_id)

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
        context = self._context(
            authenticated,
            params,
            allow_work_items=True,
        )
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
        context = self._context(
            authenticated,
            params,
            allow_work_items=True,
        )
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
        if context.draft_kind == "contract":
            payload = self._compile_architect_submission(
                authenticated,
                payload,
            )
        artifact_type = role_submission_artifact_type(context.draft_kind)
        if not artifact_type:
            raise ValueError(f"unsupported role submission kind: {context.draft_kind}")
        store = SubmissionDraftStore(self.service.runtime_root)
        snapshot = store.read(context, seed={})
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

    def _compile_architect_submission(
        self,
        authenticated: Mapping[str, Any],
        submission: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate one authored projection against its pinned Family binding.

        The sandbox sends only the parsed ``architect.yaml`` instance. Raw JSON
        Schema and compiler state stay behind this Manager gateway.
        """

        payload = dict(submission)
        if set(payload) != {"source", "architecture"}:
            raise ValueError(
                "architect submission requires only source and architecture"
            )
        if str(payload.get("source") or "") != ARCHITECT_FILENAME:
            raise ValueError(
                f"architect submission source must be {ARCHITECT_FILENAME}"
            )
        architecture = payload.get("architecture")
        if not isinstance(architecture, Mapping):
            raise ValueError("architect submission architecture must be an object")

        assignment = dict(authenticated["assignment"])
        binding_sha = str(assignment.get("family_binding_sha") or "").strip()
        binding_record = self.repository.read_artifact_record(binding_sha)
        if binding_record is None:
            raise ValueError("pinned FamilyBindingArtifact is unavailable")
        if str(binding_record.get("artifact_type") or "") != "FamilyBindingArtifact":
            raise ValueError("pinned family binding has the wrong artifact type")
        binding = dict(self.service.artifacts.read_json(binding_record))
        validate_family_binding_payload(binding)
        architecture_binding = dict(
            binding.get("architecture_definition") or {}
        )
        schema_payload = self.service.artifacts.read_json(
            dict(architecture_binding.get("schema_ref") or {})
        )
        template_payload = self.service.artifacts.read_json(
            dict(architecture_binding.get("template_ref") or {})
        )
        if not isinstance(schema_payload, Mapping):
            raise ValueError("pinned architecture schema artifact is malformed")
        if not isinstance(template_payload, Mapping):
            raise ValueError("pinned architect template artifact is malformed")
        definition = compiled_architecture_definition_from_mapping(
            {
                "specialization_id": architecture_binding["specialization_id"],
                "family_id": architecture_binding["family_id"],
                "generation_hash": architecture_binding["generation_hash"],
                "schema": dict(schema_payload),
                "template": str(template_payload.get("template") or ""),
                "example": {},
            }
        )
        document = validate_contract_payload(
            dict(architecture),
            definition=definition,
        )

        prompt_pack = self._authenticated_prompt_pack(authenticated)
        workspace = {
            **dict(prompt_pack.get("workspace") or {}),
            "runtime_root": str(self.service.runtime_root),
            "minion_v2": dict(
                dict(prompt_pack.get("metadata") or {}).get("minion_v2") or {}
            ),
        }
        work_items = assert_work_items_complete(workspace)
        return {
            "schema_version": "1",
            "contract_schema": definition.specialization_id,
            "contract": document.model_dump(mode="python"),
            "source": ARCHITECT_FILENAME,
            "work_items": [
                dict(item)
                for item in list(work_items.get("items") or [])
            ],
        }

    def _authenticated_prompt_pack(
        self,
        authenticated: Mapping[str, Any],
    ) -> dict[str, Any]:
        attempt = self.repository.read_role_attempt(
            str(authenticated["attempt_id"])
        )
        prompt_ref = dict((attempt or {}).get("prompt_pack_ref") or {})
        if not prompt_ref.get("sha256"):
            raise ValueError("role prompt pack is unavailable")
        prompt_pack = self.service.artifacts.read_json(prompt_ref)
        if not isinstance(prompt_pack, Mapping):
            raise ValueError("role prompt pack is malformed")
        return dict(prompt_pack)

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

    def _git_read(
        self,
        authenticated: Mapping[str, Any],
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        command = str(params.get("cmd") or "").strip()
        policy = classify_git_command(command)
        if policy.operation_kind != "read":
            reason = policy.reason or "the command is not classified as read-only"
            raise ValueError(f"only classified read-only Git commands are allowed: {reason}")

        attempt = self.repository.read_role_attempt(str(authenticated["attempt_id"]))
        prompt_ref = dict((attempt or {}).get("prompt_pack_ref") or {})
        if not prompt_ref.get("sha256"):
            raise ValueError("role prompt pack is unavailable for Git scope validation")
        prompt_pack = self.service.artifacts.read_json(prompt_ref)
        if not isinstance(prompt_pack, Mapping):
            raise ValueError("role prompt pack is malformed")
        workspace = dict(prompt_pack.get("workspace") or {})
        root_text = next(
            (
                str(workspace.get(key) or "").strip()
                for key in ("repo_path", "task_repo_path", "target_repo_path")
                if str(workspace.get(key) or "").strip()
            ),
            "",
        )
        if not root_text:
            raise ValueError("role prompt pack has no repository workspace")
        workspace_root = Path(root_text).expanduser().resolve()
        cwd_text = str(params.get("cwd") or "").strip()
        cwd = Path(cwd_text).expanduser().resolve() if cwd_text else workspace_root
        if not cwd.is_relative_to(workspace_root):
            raise ValueError("Git cwd is outside the assigned repository workspace")

        result = GitTool().invoke({"cmd": command, "cwd": str(cwd)})
        structured = dict(result.structured or {})
        return {
            "returncode": int(structured.get("returncode", 1)),
            "stdout": str(structured.get("stdout") or ""),
            "stderr": str(structured.get("stderr") or ""),
            "classification": dict(structured.get("classification") or policy.to_dict()),
        }

    @staticmethod
    def _context(
        authenticated: Mapping[str, Any],
        params: Mapping[str, Any],
        *,
        allow_work_items: bool = False,
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
        allowed_kinds = {str(assignment["submission_kind"])}
        if allow_work_items:
            allowed_kinds.add("work_items")
        if context.draft_kind not in allowed_kinds:
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
