from __future__ import annotations

import os
import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pal.execution.git_tool import GitTool, classify_git_command
from pal.bunshin.v2.adapters import SOFTWARE_GIT_ADAPTER
from pal.bunshin.ipc import ROLE_GATEWAY_TOKEN_ENV, BunshinRoleGatewayClient
from pal.bunshin.v2.architecture_templates import (
    compiled_architecture_definition_from_mapping,
)
from pal.bunshin.v2.artifacts import ArtifactRef
from pal.bunshin.v2.contracts import AggregateType
from pal.bunshin.v2.contract_protocol import (
    ARCHITECT_FILENAME,
    software_contract_projection,
    validate_contract_payload,
)
from pal.bunshin.v2.graph_compiler import (
    GraphCompileBindings,
    GraphCompiler,
    build_yaml_source_map,
)
from pal.bunshin.v2.graph_satellites import FamilyGraphSatelliteProjector
from pal.bunshin.v2.graph_protocol import GraphSourceMap, RoleBinding
from pal.bunshin.v2.git_scope import scoped_role_git_read_command
from pal.bunshin.v2.role_contracts import (
    family_execution_adapter,
    validate_family_binding_payload,
)
from pal.bunshin.v2.service import BunshinV2WorkflowService
from pal.bunshin.v2.skeleton import preflight_architecture_workspace_submission
from pal.bunshin.v2.task_ledger import validate_task_ledger
from pal.bunshin.v2.submission_drafts import (
    SubmissionDraftContext,
    SubmissionDraftSnapshot,
    SubmissionDraftStore,
)
from pal.bunshin.v2.role_protocol import RoleAssignmentState, stable_hash
from pal.bunshin.v2.swe_verification import (
    semantic_verification_submission_errors,
    verification_corpus_files,
    verification_scratch_paths,
    verification_workspace_changed_paths,
)
from pal.bunshin.v2.work_items import (
    assert_work_items_complete,
    submission_work_items,
)

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


def role_gateway_client_from_env(runtime_root: Path) -> BunshinRoleGatewayClient | None:
    token = str(os.environ.get(ROLE_GATEWAY_TOKEN_ENV) or "").strip()
    if not token:
        return None
    return BunshinRoleGatewayClient(Path(runtime_root), token)


def _graph_role_binding(value: Mapping[str, Any]) -> RoleBinding:
    binding = dict(value or {})
    participant = str(binding.get("participant") or "")
    profile = dict(binding.get("role_profile") or {})
    return RoleBinding(
        participant=participant,
        profile_id=(
            str(
                profile.get("canonical_profile_id")
                or profile.get("bunshin_profile")
                or ""
            ).strip()
            if participant == "profile"
            else ""
        ),
        reason=(
            str(binding.get("reason") or "").strip()
            if participant == "null"
            else ""
        ),
    )


@dataclass
class RoleAssignmentGateway:
    """Narrow Manager-owned state surface exposed to sandboxed role invocations."""

    service: BunshinV2WorkflowService

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
        if method == "harness_state_read":
            assignment = dict(authenticated["assignment"])
            attempt = self.repository.read_role_attempt(
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
                "state": self.repository.read_role_harness_continuation(
                    session_id=str(assignment["session_id"]),
                    harness_id=harness_id,
                    harness_generation=harness_generation,
                ),
            }
        if method == "harness_state_write":
            assignment = dict(authenticated["assignment"])
            state = self.repository.write_role_attempt_harness_state(
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
        if method == "draft_submit":
            return self._draft_submit(authenticated, payload)
        if method == "bound_input_json":
            return self._bound_input_json(authenticated, payload)
        if method == "artifact_put":
            return self._artifact_put(authenticated, payload)
        if method == "git_read":
            return self._git_read(authenticated, payload)
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
        store = SubmissionDraftStore(self.service.runtime_root)
        self._reconcile_draft_from_assignment_receipt(
            authenticated,
            context,
            store,
        )
        snapshot = store.read(
            context,
            seed=dict(params.get("seed") or {}),
        )
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
        # Semantic verifier outcome tools always submit an explicit outcome.
        # Data-driven families may still use the distinct VerificationPlan
        # payload under the same durable submission kind; its own compiler
        # contract is outside this SWE outcome validator.
        if context.draft_kind == "verification" and "outcome" in payload:
            self._validate_verification_submission_before_receipt(
                authenticated,
                assignment,
                payload,
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
        if str(assignment.get("submission_kind") or "") != context.draft_kind:
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

    def _validate_verification_submission_before_receipt(
        self,
        authenticated: Mapping[str, Any],
        assignment: Mapping[str, Any],
        submission: Mapping[str, Any],
    ) -> None:
        """Reject correctable verifier output before freezing its Draft."""

        if str(assignment.get("aggregate_type") or "") != AggregateType.DAG_NODE_RUN.value:
            return
        node = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            str(assignment.get("aggregate_id") or ""),
        )
        if node is None:
            return
        view_value = node.payload.get("unit_work_view_ref")
        if not isinstance(view_value, Mapping) or not view_value.get("sha256"):
            return
        try:
            work_view = self.service.artifacts.read_json(dict(view_value))
        except Exception:
            # Manager-owned identity/corpus corruption is not correctable by
            # the live role.  Preserve the durable post-submit invariant path
            # instead of feeding the model an impossible retry instruction.
            return
        if not isinstance(work_view, Mapping):
            return

        prompt_pack = self._authenticated_prompt_pack(authenticated)
        workspace = dict(prompt_pack.get("workspace") or {})
        review_workspace = Path(str(workspace.get("repo_path") or ""))
        review_scratch = Path(str(workspace.get("review_scratch_dir") or ""))
        scratch_only = bool(workspace.get("verification_scratch_only"))
        corpus_scope = dict(
            dict(node.payload.get("path_policy") or {}).get(
                "verification_corpus"
            )
            or {}
        )
        if scratch_only:
            changed_paths = verification_scratch_paths(review_scratch)
            current_case_paths = list(changed_paths)
        else:
            candidate_digest = str(node.payload.get("candidate_digest") or "").strip()
            if not candidate_digest:
                return
            if not review_workspace.is_dir():
                return
            try:
                changed_paths = verification_workspace_changed_paths(
                    review_workspace,
                    candidate_digest,
                )
            except Exception:
                return
            current_case_paths = verification_corpus_files(
                review_workspace,
                corpus_scope,
            )
        errors = semantic_verification_submission_errors(
            submission,
            work_view=dict(work_view),
            changed_paths=changed_paths,
            current_case_paths=current_case_paths,
            corpus_scope=corpus_scope,
            scratch_only=scratch_only,
        )
        if errors:
            raise ValueError(
                "verification submission rejected before durable receipt:\n- "
                + "\n- ".join(errors)
            )

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
        satellite_template_payload = self.service.artifacts.read_json(
            dict(architecture_binding.get("satellite_template_ref") or {})
        )
        if not isinstance(schema_payload, Mapping):
            raise ValueError("pinned architecture schema artifact is malformed")
        if not isinstance(template_payload, Mapping):
            raise ValueError("pinned architect template artifact is malformed")
        if not isinstance(satellite_template_payload, Mapping):
            raise ValueError("pinned graph satellite template artifact is malformed")
        if (
            str(satellite_template_payload.get("specialization_id") or "")
            != str(architecture_binding.get("specialization_id") or "")
            or str(satellite_template_payload.get("generation_hash") or "")
            != str(architecture_binding.get("generation_hash") or "")
        ):
            raise ValueError(
                "pinned graph satellite template does not match its Family binding"
            )
        definition = compiled_architecture_definition_from_mapping(
            {
                "specialization_id": architecture_binding["specialization_id"],
                "family_id": architecture_binding["family_id"],
                "generation_hash": architecture_binding["generation_hash"],
                "schema": dict(schema_payload),
                "template": str(template_payload.get("template") or ""),
                "graph_satellite_template": str(
                    satellite_template_payload.get("template") or ""
                ),
                "example": {},
            }
        )
        document = validate_contract_payload(
            dict(architecture),
            definition=definition,
        )

        if (
            family_execution_adapter(binding.get("execution_adapter"))
            == SOFTWARE_GIT_ADAPTER
        ):
            revision = self.repository.read_snapshot(
                AggregateType.ARCHITECTURE_REVISION,
                str(assignment["aggregate_id"]),
            )
            if revision is None:
                raise ValueError("architecture revision is unavailable")
            workspace_root = Path(
                str(revision.payload.get("architecture_workspace_path") or "")
            )
            base_sha = str(
                revision.payload.get("architecture_base_sha") or ""
            ).strip()
            requirements_ref = dict(revision.payload.get("requirements_ref") or {})
            workspace_snapshot_ref = dict(
                revision.payload.get("workspace_snapshot_ref") or {}
            )
            if (
                not workspace_root.is_dir()
                or not base_sha
                or not requirements_ref.get("sha256")
                or not workspace_snapshot_ref.get("sha256")
            ):
                raise ValueError(
                    "architecture workspace preflight state is incomplete"
                )
            requirements = validate_task_ledger(
                self.service.artifacts.read_json(requirements_ref)
            )
            workspace_snapshot = self.service.artifacts.read_json(
                workspace_snapshot_ref
            )
            validation, _ = (
                preflight_architecture_workspace_submission(
                    software_contract_projection(
                        document.model_dump(mode="python")
                    ),
                    requirements_payload=requirements,
                    workspace_root=workspace_root,
                    base_sha=base_sha,
                    original_head=str(
                        dict(workspace_snapshot).get("original_head") or ""
                    ),
                )
            )
            validation.raise_for_errors()

        prompt_pack = self._authenticated_prompt_pack(authenticated)
        workspace = {
            **dict(prompt_pack.get("workspace") or {}),
            "runtime_root": str(self.service.runtime_root),
            "bunshin_v2": dict(
                dict(prompt_pack.get("metadata") or {}).get("bunshin_v2") or {}
            ),
        }
        # Architecture revisions and installed GraphIR generations are
        # independent sequences.  A human edit may supersede any number of
        # authored revisions before one is accepted, and those discarded
        # revisions must not consume GraphIR generations.  Compile the
        # candidate against the next append-only GraphIR slot; installation
        # still performs the transactional gap/identity check.
        previous_graph = self.repository.read_graph_generation(
            graph_id=str(assignment["workflow_id"]),
        )
        graph_generation = (
            int(previous_graph.generation) + 1
            if previous_graph is not None
            else 1
        )
        source_map = (
            build_yaml_source_map(
                Path(str(workspace["architect_path"]))
            )
            if str(workspace.get("architect_path") or "").strip()
            else GraphSourceMap(
                source_ref=ARCHITECT_FILENAME,
                locations={},
            )
        )
        source_map_ref = self.service.artifacts.put_json(
            source_map.to_dict(),
            artifact_type="GraphSourceMapArtifact",
            provenance={
                "workflow_id": str(assignment["workflow_id"]),
                "architecture_assignment_id": str(
                    assignment["assignment_id"]
                ),
            },
        )
        graph_ir = GraphCompiler().compile(
            document,
            graph_id=str(assignment["workflow_id"]),
            generation=graph_generation,
            bindings=GraphCompileBindings(
                producer=_graph_role_binding(
                    dict(binding["role_bindings"])["implementation"]
                ),
                checker=_graph_role_binding(
                    dict(binding["role_bindings"])["verifier"]
                ),
                execution_adapter=str(binding["execution_adapter"]),
            ),
            satellite_projector=FamilyGraphSatelliteProjector(
                specialization_id=definition.specialization_id,
                template=definition.graph_satellite_template,
            ),
            source_ref=ARCHITECT_FILENAME,
            workspace_authority_rules=definition.workspace_authority_rules,
            source_map=source_map,
            source_map_ref=source_map_ref.sha256,
        )
        work_items = assert_work_items_complete(workspace)
        return {
            "schema_version": "2",
            "contract_schema": definition.specialization_id,
            "contract": document.model_dump(mode="python"),
            "graph_ir": graph_ir.to_dict(),
            "graph_source_map_ref": source_map_ref.to_dict(),
            "source": ARCHITECT_FILENAME,
            "work_items": submission_work_items(work_items.get("items")),
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

        scoped_command = scoped_role_git_read_command(
            prompt_pack=prompt_pack,
            assignment=dict(authenticated.get("assignment") or {}),
            artifact_reader=self.service.artifacts.read_json,
            policy=policy,
        )

        result = GitTool().invoke({"cmd": scoped_command, "cwd": str(cwd)})
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
    client: BunshinRoleGatewayClient

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
