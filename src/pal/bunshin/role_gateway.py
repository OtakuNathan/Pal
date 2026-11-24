from __future__ import annotations

from pal.shared.messages import normalize_bunshin_pack_fields

import base64
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from pal.execution.git_tool import GitTool, classify_git_command
from pal.execution.tool_facade import ToolExecutionError, ToolRejectedError
from pal.shared import RuntimeStatus
from pal.bunshin.git_gateway_diagnostics import GitGatewayDiagnostics
from pal.bunshin.role_gateway_client import role_gateway_client_from_env, RoleGatewayArtifactStore
from pal.bunshin.artifacts import ArtifactRef
from pal.bunshin.git_scope import scoped_role_git_read_plan
from pal.bunshin.interaction_gateway import interaction_command
from pal.bunshin.architect_submission import compile_architect_submission
from pal.bunshin.verifier_submission import validate_verification_submission
from pal.bunshin.service import BunshinWorkflowService
from pal.bunshin.submission_drafts import (
    decode_remote_draft_snapshot,
    SubmissionDraftContext,
    SubmissionDraftSnapshot,
    SubmissionDraftStore,
)
from pal.bunshin.role_protocol import RoleAssignmentState, stable_hash
from pal.bunshin.work_items import work_item_seed, prepare_work_item_mutation

from pal.bunshin.submission_errors import SubmissionValidationError, submission_validation

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


@dataclass
class RoleAssignmentGateway:
    """Narrow Manager-owned state surface exposed to sandboxed role invocations."""

    service: BunshinWorkflowService
    _git_diagnostics: GitGatewayDiagnostics = field(
        default_factory=GitGatewayDiagnostics, init=False, repr=False, compare=False,
    )

    @property
    def repository(self):
        return self.service.repository

    def authorize(self, access_token: str) -> dict[str, Any]:
        return self.repository.role_access.authenticate_role_attempt(access_token)

    def call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(params or {})
        access_token = str(payload.pop("access_token", ""))
        authenticated = self.authorize(access_token)
        if method in {"interaction_park", "interaction_resume", "interaction_consumed"}:
            return interaction_command(self.service, method, authenticated, payload, access_token=access_token)
        if method == "checkpoint_initialize":
            assignment = dict(authenticated["assignment"])
            checkpoint = self.repository.role_attempts.publish_initial_role_checkpoint(
                assignment_id=str(assignment["assignment_id"]),
                attempt_id_value=str(authenticated["attempt_id"]),
                fencing_token=int(authenticated["fencing_token"]),
            )
            return {"sequence": int(checkpoint["sequence"])}
        if method == "submission_status":
            return self._submission_status(authenticated)
        if method == "harness_state_read":
            assignment = dict(authenticated["assignment"])
            attempt = self.repository.role_attempts.read_role_attempt(
                str(authenticated["attempt_id"])
            )
            if attempt is None:
                raise ValueError("authenticated role attempt is unavailable")
            harness_id = str(attempt.get("harness_id") or "")
            harness_generation = str(
                attempt.get("harness_generation") or ""
            )
            return {
                "harness_id": harness_id,
                "harness_generation": harness_generation,
                "state": self.repository.role_attempts.read_role_harness_continuation(
                    session_id=str(assignment["session_id"]),
                    harness_id=harness_id,
                    harness_generation=harness_generation,
                ),
            }
        if method == "harness_state_write":
            assignment = dict(authenticated["assignment"])
            state = self.repository.role_attempts.write_role_attempt_harness_state(
                assignment_id=str(assignment["assignment_id"]),
                attempt_id_value=str(authenticated["attempt_id"]),
                fencing_token=int(authenticated["fencing_token"]),
                harness_state=dict(payload.get("state") or {}),
            )
            return {"state": state}
        if method == "draft_read":
            return self._draft_read(authenticated, payload)
        if method == "draft_mutate":
            return self._draft_mutate(authenticated, payload)
        if method == "draft_operation_result":
            context = self._context(authenticated, payload, allow_work_items=True)
            result = SubmissionDraftStore(self.service.runtime_root).read_operation(
                context, operation_key=str(payload.get("operation_key") or ""),
                request=dict(payload.get("request") or {}),
            )
            return {"found": result is not None, "result": result}
        if method == "repair_precheck_plan":
            from pal.bunshin.repair_precheck import repair_precheck_plan
            assignment = authenticated["assignment"]
            if (assignment["role"], assignment["mode"]) != ("implementation", "repair"):
                raise ValueError("repair precheck requires an implementation repair assignment")
            workspace = self._authenticated_prompt_pack(authenticated)["workspace"]
            if not workspace.get("repair_precheck"):
                raise ValueError("assignment has no repair precheck")
            return repair_precheck_plan(Path(workspace["repo_path"]), workspace["repair_corpus_scope"])
        if method == "draft_submit":
            return self._draft_submit(authenticated, payload)
        if method == "bound_input_json":
            return self._bound_input_json(authenticated, payload)
        if method == "artifact_put":
            return self._artifact_put(authenticated, payload)
        if method == "draft_evidence_read":
            return self._draft_evidence_read(authenticated, payload)
        if method == "git_read":
            def record_event(event: Mapping[str, Any]) -> None:
                self.repository.role_events.record_worker_event(event)

            diagnostic = self._git_diagnostics.started(
                authenticated, payload.get("cmd"), record_event,
            )
            try:
                result = self._git_read(authenticated, payload)
            except BaseException as exc:
                self._git_diagnostics.failed(diagnostic, exc, record_event)
                raise
            self._git_diagnostics.returned(diagnostic, result, record_event)
            return result
        raise ValueError(f"role gateway method is not allowed: {method}")

    def _bound_input_json(
        self,
        authenticated: Mapping[str, Any],
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Return one immutable prompt input through the Manager boundary.

        Worker prompt packs deliberately expose projected ``/pal/references``
        paths.  Those paths only exist inside the worker's bwrap namespace, so
        submission validation cannot dereference them from the Manager
        process (and a resumed worker may not have the same projection yet).
        Resolve by the authenticated reference *name* instead.  The name is
        looked up in the authenticated prompt pack; callers cannot supply an
        arbitrary host path.
        """

        name = str(params.get("name") or "").strip()
        if not name:
            raise ValueError("bound input name is required")
        prompt_pack = self._authenticated_prompt_pack(authenticated)
        metadata = dict(prompt_pack.get("metadata") or {})
        brief = dict(metadata.get("requirements_brief") or {})
        references = [
            dict(item or {})
            for item in list(brief.get("references") or [])
            if isinstance(item, Mapping)
        ]
        reference = next(
            (item for item in references if str(item.get("name") or "") == name),
            None,
        )
        # Older prompt packs may not carry requirements_brief.  The sandbox
        # bind manifest still contains the Manager-side source path and is
        # authenticated as part of the same prompt pack.
        if reference is None:
            sandbox = dict(metadata.get("sandbox") or {})
            binds = [
                dict(item or {})
                for item in list(sandbox.get("reference_binds") or [])
                if isinstance(item, Mapping)
            ]
            bind = next(
                (item for item in binds if str(item.get("name") or "") == name),
                None,
            )
            if bind is not None:
                reference = {
                    "name": name,
                    "path": bind.get("source_path"),
                    "include": list(bind.get("include") or []),
                }
        if reference is None:
            raise ValueError(f"bound input {name!r} is not part of the authenticated prompt")

        root = Path(str(reference.get("path") or "")).expanduser()
        includes = [
            str(item).replace("\\", "/").strip()
            for item in list(reference.get("include") or [])
            if str(item).strip()
        ]
        candidate = root
        if candidate.is_dir() and len(includes) == 1 and not any(
            character in includes[0] for character in "*?["
        ):
            candidate = candidate / includes[0]
        if not candidate.is_file():
            raise ValueError(f"bound input {name!r} is unavailable at {candidate}")
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"bound input {name!r} is not valid JSON") from exc
        if not isinstance(value, Mapping):
            raise ValueError(f"bound input {name!r} must contain a JSON object")
        return {"value": dict(value)}

    def _submission_status(
        self,
        authenticated: Mapping[str, Any],
    ) -> dict[str, Any]:
        assignment = self.repository.role_assignments.read_role_assignment(
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
        return {
            "recorded": recorded,
            "state": state,
            **({
                "submission_artifact_ref": dict(assignment["submission_artifact_ref"]),
                "submission_payload_hash": str(assignment["submission_payload_hash"]),
            } if recorded else {}),
        }

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
        store = SubmissionDraftStore(self.service.runtime_root)
        from pal.bunshin.review_findings import empty_review_draft
        seed = (work_item_seed(self._authoring_workspace(authenticated, context))
                if context.role == "verifier" and context.draft_kind == "work_items"
                else empty_review_draft() if context.role == "verifier"
                else dict(params.get("seed") or {}))
        snapshot = store.read(context, seed=seed)
        self._reconcile_draft_from_assignment_receipt(authenticated, context, store)
        snapshot = store.read(context, seed=seed)
        return {"snapshot": snapshot.to_dict()}

    def _draft_mutate(
        self,
        authenticated: Mapping[str, Any],
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        assignment = dict(authenticated["assignment"])
        if str(assignment.get("state") or "") != RoleAssignmentState.RUNNING.value:
            raise ValueError("role assignment receipt already froze authoring")
        context = self._context(
            authenticated,
            params,
            allow_work_items=True,
        )
        if context.role == "verifier" and context.draft_kind == "work_items":
            workspace = self._authoring_workspace(authenticated, context)
            request = dict(params.get("request") or {})
            operation = str(params.get("operation_key") or "")
            with submission_validation():
                reducer = prepare_work_item_mutation(workspace, request, operation)

            def validated_reducer(payload: dict[str, Any]):
                # Preserve semantic rejections across RPC without classifying
                # storage, fencing, or CAS failures as authored-content errors.
                with submission_validation():
                    return reducer(payload)

            result = SubmissionDraftStore(self.service.runtime_root).mutate(
                context, operation_key=operation, request=request,
                expected_version=int(params.get("expected_version") or 0),
                reducer=validated_reducer,
                seed=work_item_seed(workspace),
            )
            return {"result": dict(result)}
        result = SubmissionDraftStore(self.service.runtime_root).mutate_precomputed(
            context,
            operation_key=str(params.get("operation_key") or ""),
            request=dict(params.get("request") or {}),
            expected_version=int(params.get("expected_version") or 0),
            next_payload=dict(params.get("next_payload") or {}),
            result=dict(params.get("result") or {}),
            seed={} if context.role == "verifier" else dict(params.get("seed") or {}),
        )
        return {"result": dict(result)}

    def _authoring_workspace(self, authenticated, context):
        pack = self._authenticated_prompt_pack(authenticated)
        workspace = dict(pack.get("workspace") or {})
        workspace["runtime_root"] = str(self.service.runtime_root)
        workspace["bunshin"] = {
            **dict(dict(pack.get("metadata") or {}).get("bunshin") or {}),
            **dict(workspace.get("bunshin") or {}),
            **context.to_dict(), "authoring_input_fingerprint": context.input_fingerprint,
        }
        return workspace

    def _draft_submit(
        self,
        authenticated: Mapping[str, Any],
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        assignment = dict(authenticated["assignment"])
        context = self._context(authenticated, params)
        submission = params.get("submission")
        if not isinstance(submission, Mapping):
            raise SubmissionValidationError("role submission must be a JSON object")
        payload = dict(submission)
        if (context.role == "verifier" or context.draft_kind == "candidate") and assignment.get("submission_payload_hash"):
            if stable_hash(payload) != assignment["submission_payload_hash"]:
                raise SubmissionValidationError("role assignment already has a different submission receipt")
            return {"submitted": True, "submission_artifact_ref": dict(assignment["submission_artifact_ref"]),
                    "submission_payload_hash": assignment["submission_payload_hash"]}
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
        draft_versions = {context.draft_key: int(params.get("expected_version") or 0)}
        if context.role == "verifier":
            work_context = replace(context, draft_kind="work_items")
            work_snapshot = store.read(work_context, seed=work_item_seed(self._authoring_workspace(authenticated, context)))
            expected_work_version = params.get("expected_work_item_version")
            if expected_work_version is None:
                raise SubmissionValidationError("verification submission requires the observed work-item draft version")
            draft_versions[work_context.draft_key] = int(expected_work_version)
            from pal.bunshin.draft_integrity import assert_verifier_projection
            assert_verifier_projection(payload, snapshot.payload, work_snapshot.payload)
        # Semantic verifier outcome tools always submit an explicit outcome.
        # Data-driven families may still use the distinct VerificationPlan
        # payload under the same durable submission kind; its own compiler
        # contract is outside this SWE outcome validator.
        if context.role == "verifier":
            self._validate_draft_submission_before_receipt(
                authenticated, context, payload, case_revision=int(snapshot.payload.get("case_revision") or 0),
            )
        if (context.draft_kind == "candidate" and assignment["mode"] == "repair"
                and str(payload.get("status") or "candidate_ready").strip().lower() == "candidate_ready"):
            pack = self._authenticated_prompt_pack(authenticated)
            if pack["workspace"].get("repair_precheck"):
                from pal.bunshin.repair_precheck import check_manager_repair
                with submission_validation():
                    check_manager_repair(self.service.runtime_root, pack, payload.get("repair_precheck") or {})
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
        self.repository.role_submissions.record_role_submission(
            assignment_id=str(assignment["assignment_id"]),
            attempt_id_value=str(authenticated["attempt_id"]),
            fencing_token=int(authenticated["fencing_token"]),
            artifact_ref=artifact_ref.to_dict(),
            payload_hash=payload_hash,
            draft_versions=draft_versions,
            validate_before_receipt=(lambda: self._validate_draft_submission_before_receipt(
                authenticated, context, payload, case_revision=int(snapshot.payload.get("case_revision") or 0),
            )) if context.role == "verifier" else None,
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
        try:
            store.mark_submitted(
                context,
                expected_version=int(params.get("expected_version") or 0),
                submission_artifact_ref=artifact_ref.to_dict(),
                submission_payload_hash=payload_hash,
            )
        except (RuntimeError, OSError):
            # The assignment receipt above is the canonical completion
            # boundary.  A Draft CAS race or a crash/replay window must not
            # turn an accepted submission back into a worker-visible failure.
            store.reconcile_submitted(
                context,
                submission_artifact_ref=artifact_ref.to_dict(),
                submission_payload_hash=payload_hash,
            )
        return {
            "submitted": True,
            "submission_artifact_ref": artifact_ref.to_dict(),
            "submission_payload_hash": payload_hash,
        }

    def _reconcile_draft_from_assignment_receipt(
        self,
        authenticated: Mapping[str, Any],
        context: SubmissionDraftContext,
        store: SubmissionDraftStore,
    ) -> None:
        assignment = dict(authenticated["assignment"])
        if str(assignment.get("submission_kind") or "") != context.draft_kind and not (
            context.role == "verifier" and context.draft_kind == "work_items"
        ):
            return
        if str(assignment.get("state") or "") not in {
            RoleAssignmentState.RESULT_RECORDED.value,
            RoleAssignmentState.SETTLED.value,
        }:
            return
        artifact_ref = dict(assignment.get("submission_artifact_ref") or {})
        payload_hash = str(assignment.get("submission_payload_hash") or "").strip()
        if not artifact_ref or not payload_hash:
            return
        store.reconcile_submitted(
            context,
            submission_artifact_ref=artifact_ref,
            submission_payload_hash=payload_hash,
        )

    def _validate_draft_submission_before_receipt(
        self, authenticated: Mapping[str, Any], context: SubmissionDraftContext,
        submission: Mapping[str, Any], *, case_revision: int,
    ) -> None:
        """Every verifier format revalidates content at the receipt boundary."""
        from pal.bunshin.verification_readiness import verification_case_errors, verification_corpus_snapshot

        with submission_validation():
            if "outcome" in submission:
                self._validate_verification_submission_before_receipt(
                    authenticated, authenticated["assignment"], submission, case_revision=case_revision,
                )
            workspace = {**self._authoring_workspace(authenticated, context), "verification_case_revision": case_revision}
            errors = verification_case_errors(list(submission.get("recorded_results") or []),
                                              outcome=str(submission.get("outcome") or "unknown"), workspace=workspace)
            binding = submission.get("verification_binding")
            if isinstance(binding, Mapping) and dict(binding) != verification_corpus_snapshot(workspace):
                errors.append("verification corpus changed before receipt acceptance")
            if errors:
                raise ValueError("verification submission rejected before durable receipt:\n- " + "\n- ".join(errors))

    def _validate_verification_submission_before_receipt(
        self,
        authenticated: Mapping[str, Any],
        assignment: Mapping[str, Any],
        submission: Mapping[str, Any],
        *, case_revision: int = 0,
    ) -> None:
        return validate_verification_submission(self.service, self._authenticated_prompt_pack, authenticated, assignment, submission, case_revision=case_revision)

    def _compile_architect_submission(
        self,
        authenticated: Mapping[str, Any],
        submission: Mapping[str, Any],
    ) -> dict[str, Any]:
        return compile_architect_submission(self.service, self._authenticated_prompt_pack, authenticated, submission)

    def _authenticated_prompt_pack(
        self,
        authenticated: Mapping[str, Any],
    ) -> dict[str, Any]:
        attempt = self.repository.role_attempts.read_role_attempt(
            str(authenticated["attempt_id"])
        )
        prompt_ref = dict((attempt or {}).get("prompt_pack_ref") or {})
        if not prompt_ref.get("sha256"):
            raise ValueError("role prompt pack is unavailable")
        prompt_pack = self.service.artifacts.read_json(prompt_ref)
        if not isinstance(prompt_pack, Mapping):
            raise ValueError("role prompt pack is malformed")
        return normalize_bunshin_pack_fields(dict(prompt_pack))

    def _draft_evidence_read(self, authenticated: Mapping[str, Any],
                            params: Mapping[str, Any]) -> dict[str, Any]:
        context = self._context(authenticated, params)
        store = SubmissionDraftStore(self.service.runtime_root)
        if params.get("operation_key"):
            result = store.read_operation(context, operation_key=str(params["operation_key"]),
                                          request=dict(params.get("request") or {}))
            cases = [dict((result or {}).get("case") or {})]
        else:
            snapshot = store.read(context)
            cases = list(dict(dict(snapshot.payload.get("evidence") or {}).get("cases") or {}).values())
        digest = str(params.get("sha256") or "")
        for case in cases:
            for key in ("stdout_ref", "stderr_ref", "public_result_ref"):
                ref = case.get(key)
                if isinstance(ref, Mapping) and digest and ref.get("sha256") == digest:
                    data = self.service.artifacts.read_bytes(ref)
                    return {"data_base64": base64.b64encode(data).decode("ascii")}
        raise ValueError("artifact is not recorded evidence for this Draft operation")

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

        attempt = self.repository.role_attempts.read_role_attempt(str(authenticated["attempt_id"]))
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

        plan = scoped_role_git_read_plan(
            prompt_pack=prompt_pack,
            assignment=dict(authenticated.get("assignment") or {}),
            artifact_reader=self.service.artifacts.read_json,
            policy=policy,
            repository_root=workspace_root,
            cwd_prefix="" if cwd == workspace_root else cwd.relative_to(workspace_root).as_posix(),
        )

        result = GitTool()._invoke_scoped_read(plan, cwd=cwd)
        structured = dict(result.structured or {})
        # A pre-execution refusal has no native Git process result. Keep it on
        # the gateway error path, which the worker shim reports as a refusal.
        if result.status in {RuntimeStatus.FORBIDDEN, RuntimeStatus.INVALID}:
            raise ToolRejectedError(
                "Git wrapper rejected the scoped command before execution.",
                error_code=str(structured.get("error_code") or "git_read_rejected"),
            )
        if (
            result.status not in {RuntimeStatus.OK, RuntimeStatus.ERROR}
            or type(structured.get("returncode")) is not int
            or type(structured.get("stdout")) is not str
            or type(structured.get("stderr")) is not str
        ):
            raise ToolExecutionError(
                "Git wrapper did not return a valid process result.",
                error_code="git_read_invalid_result",
            )
        return {
            "returncode": structured["returncode"],
            "stdout": structured["stdout"],
            "stderr": structured["stderr"],
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
