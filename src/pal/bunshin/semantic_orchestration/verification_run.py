from __future__ import annotations
from pal.bunshin.contracts import AggregateSnapshot
from pal.bunshin.semantic_orchestration.role_inputs import _verifier_reference_refs
from pal.bunshin.semantic_orchestration.role_environment import _workspace_tooling_from_work_view
from pal.bunshin.semantic_orchestration.verification_workspace import _module_verifier_git_diff_refs
from pal.bunshin.semantic_orchestration.verification_workspace import _verification_workspace_from_prompt_pack
from pal.bunshin.semantic_orchestration.verification_workspace import _semantic_verifier_instruction
from pal.bunshin.semantic_orchestration.verification_workspace import _ensure_workspace_directory
from pal.bunshin.semantic_orchestration.worker_results import _primary_json_output
from pal.bunshin.semantic_orchestration.review_results import _ref_from_mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.adapters import ARTIFACT_BUNDLE_ADAPTER, SOFTWARE_GIT_ADAPTER, ArtifactBundleAdapter
from pal.bunshin.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.contracts import SubmissionInvariantError
from pal.bunshin.work_views import UnitWorkViewBuilder
from pal.bunshin.module_workspaces import provision_module_verification_workspace
from pal.bunshin.verification_builder import effective_verification_policy
from pal.bunshin.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.bunshin.semantic_orchestration.attempt_execution import AttemptExecution
from pal.bunshin.semantic_orchestration.effect_reads import EffectReads
from pal.bunshin.semantic_orchestration.role_leases import RoleLeases
from pal.bunshin.semantic_orchestration.verification_completion import VerificationCompletion
from pal.bunshin.semantic_orchestration.workflow_facts import WorkflowFacts
from pal.bunshin.contract_runtime import ContractArtifactAccess


@dataclass
class VerificationRun:
    attempt_execution: AttemptExecution
    effect_reads: EffectReads
    role_leases: RoleLeases
    verification_completion: VerificationCompletion
    workflow_facts: WorkflowFacts
    artifacts: ContentAddressedArtifactStore
    contracts: ContractArtifactAccess
    runtime_root: Path

    async def run_verification_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        if RoleMode(self.effect_reads.effect_role_mode(effect)) != RoleMode.MODULE:
            raise ValueError("verifier role requires a module node cycle")
        return await self.run_verification(effect)

    async def run_verification(
        self,
        effect: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        node = self.effect_reads.effect_snapshot(effect)
        node = await self.role_leases.ensure_node_effect_lease(
            node,
            action_type="REBIND_REVIEWER",
            activation=RoleActivation(
                OrchestrationRole.VERIFIER,
                RoleMode.MODULE,
            ),
        )
        invocation_id = str(node.payload.get("active_worker_id") or "")
        lease_resource = str(node.payload.get("lease_resource_key") or "")
        fencing_token = int(node.payload.get("fencing_token") or 0)
        candidate_ref = _ref_from_mapping(node.payload.get("candidate_ref"))
        adapter = self.workflow_facts.execution_adapter(node)
        candidate_digest = str(node.payload.get("candidate_digest") or "")
        if adapter == SOFTWARE_GIT_ADAPTER:
            review_workspace, review_scratch = provision_module_verification_workspace(
                self.runtime_root,
                node=node,
                candidate_digest=candidate_digest,
            )
        elif adapter == ARTIFACT_BUNDLE_ADAPTER:
            review_workspace, review_scratch = ArtifactBundleAdapter(
                self.runtime_root,
                self.artifacts,
            ).prepare_verification_workspace(
                review_id=f"{node.aggregate_id}:{candidate_digest}",
                candidate_ref=candidate_ref.to_dict(),
            )
        else:
            raise ValueError(f"unsupported verification adapter: {adapter}")
        candidate, verifier_read_only_overlays, verifier_references, work_view = self.prepare_verifier_inputs(adapter, candidate_digest, candidate_ref, node, review_workspace)
        terminal, prompt_ref, terminal_ref = await self.attempt_execution.run_profile(
            effect=effect,
            snapshot=node,
            invocation_id=invocation_id,
            lease_resource=lease_resource,
            fencing_token=fencing_token,
            profile=self.workflow_facts.profile_for_role(node.workflow_id, "verifier"),
            activation=RoleActivation(
                OrchestrationRole.VERIFIER,
                RoleMode.MODULE,
            ),
            instruction=_semantic_verifier_instruction(
                graph_sink=bool(node.payload.get("graph_sink")),
                direct=node.payload.get("execution_mode") == "direct",
            ),
            reference_refs=verifier_references,
            workspace_override={
                "kind": "existing_repo",
                "repo_path": str(review_workspace),
                "workspace_binding": (
                    "canonical"
                    if adapter == SOFTWARE_GIT_ADAPTER
                    else "ephemeral_artifact"
                ),
                "project_name": str(node.payload.get("unit_id") or "unit"),
                **_workspace_tooling_from_work_view(work_view),
                "review_scratch_dir": str(review_scratch),
                "workspace_policy": {
                    "mode": "writable_git_branch"
                },
                "verification_scratch_only": (
                    adapter != SOFTWARE_GIT_ADAPTER
                ),
                "write_path_scopes": [
                    dict(
                        dict(node.payload.get("path_policy") or {}).get(
                            "verification_corpus"
                        )
                        or {}
                    )
                ],
                "read_only_overlay_paths": verifier_read_only_overlays,
            },
            prepare_workspace=True,
        )
        review_workspace, review_scratch = _verification_workspace_from_prompt_pack(
            artifacts=self.artifacts,
            prompt_ref=prompt_ref,
        )
        plan = _primary_json_output(terminal)
        if str(plan.get("outcome") or "").strip():
            return self.verification_completion.complete_semantic_verifier(
                effect=effect,
                node=node,
                invocation_id=invocation_id,
                lease_resource=lease_resource,
                fencing_token=fencing_token,
                candidate_ref=candidate_ref,
                candidate_digest=candidate_digest,
                candidate=candidate,
                review_workspace=review_workspace,
                review_scratch=review_scratch,
                execution_adapter=adapter,
                work_view=work_view,
                submission=plan,
                terminal=terminal,
                prompt_ref=prompt_ref,
                terminal_ref=terminal_ref,
            )
        raise SubmissionInvariantError(
            "Verifier must finish with one semantic outcome tool; legacy VerificationPlan submissions are disabled"
        )

    def prepare_verifier_inputs(self, adapter: Any, candidate_digest: Any, candidate_ref: Any, node: AggregateSnapshot, review_workspace: Any) -> tuple[Any, Any, Any, Any]:
        view_value = node.payload.get("unit_work_view_ref")
        if not isinstance(view_value, Mapping) or not view_value.get("sha256"):
            raise ValueError("verifier requires the exact work view used by Coder")
        view_ref = _ref_from_mapping(view_value)
        work_view = dict(self.artifacts.read_json(view_ref))
        candidate = dict(self.artifacts.read_json(candidate_ref))
        skeleton_manifest = adapter == SOFTWARE_GIT_ADAPTER
        candidate_view_ref: ArtifactRef | None = None
        if adapter != SOFTWARE_GIT_ADAPTER or not skeleton_manifest:
            candidate_view_ref = self.artifacts.put_json(
                {
                    "module_name": str(node.payload.get("module_name") or node.payload.get("unit_id") or ""),
                    "node_kind": str(node.payload.get("node_kind") or "unit"),
                    "changed_paths": [str(item) for item in list(candidate.get("changed_paths") or [])],
                    "candidate_cycle": int(node.payload.get("candidate_cycle") or 0),
                    "instruction": "Inspect the immutable candidate in the bound review workspace.",
                },
                artifact_type="CandidateSemanticViewArtifact",
                provenance={"owner": "manager", "audience": "verifier"},
                child_refs=((candidate_ref.sha256, "candidate"),),
            )
        system_delivery_view_ref = (
            UnitWorkViewBuilder(self.contracts).system_delivery_view(node)
            if bool(node.payload.get("graph_sink"))
            else None
        )
        system_delivery_view = (
            self.artifacts.read_json(system_delivery_view_ref)
            if system_delivery_view_ref is not None
            else None
        )
        verification_policy = effective_verification_policy(
            work_view=work_view,
            verification_policy=self.workflow_facts.workflow_policy(node.workflow_id, "verification"),
            system_delivery_view=system_delivery_view,
        )
        # The work view limits this run's scope; the complete immutable ledger
        # prevents an upstream summary from narrowing user intent.
        candidate_diff_ref = candidate_view_ref
        git_diff_refs: dict[str, ArtifactRef] = {}
        if adapter == SOFTWARE_GIT_ADAPTER and skeleton_manifest:
            git_diff_refs = _module_verifier_git_diff_refs(
                artifacts=self.artifacts,
                node_payload=node.payload,
                candidate=candidate,
                candidate_ref=candidate_ref,
                candidate_digest=candidate_digest,
                review_worktree=review_workspace,
            )
            candidate_diff_ref = git_diff_refs.pop("candidate_diff")
        if candidate_diff_ref is None:
            raise ValueError("verifier requires a bound Candidate diff or System work view")
        verifier_references = _verifier_reference_refs(
            artifacts=self.artifacts,
            node_payload=node.payload,
            module_work_view_ref=view_ref,
            candidate_diff_ref=candidate_diff_ref,
        )
        if system_delivery_view_ref is not None:
            verifier_references["system_delivery_view"] = system_delivery_view_ref
        verifier_references.update(git_diff_refs)
        if work_view.get("execution_mode") == "direct":
            verifier_references["task"] = _ref_from_mapping(work_view["requirements_ref"])
            verifier_references.update({name: _ref_from_mapping(ref) for name, ref in
                                       dict(work_view.get("direct_reference_refs") or {}).items()})
        path_policy = dict(node.payload.get("path_policy") or {})
        developer_test_path = str(
            dict(path_policy.get("developer_tests") or {}).get("path") or ""
        ).strip()
        verification_corpus_path = str(
            dict(path_policy.get("verification_corpus") or {}).get("path") or ""
        ).strip()
        for corpus_path in (
            developer_test_path,
            verification_corpus_path,
        ):
            if corpus_path:
                _ensure_workspace_directory(review_workspace, corpus_path)
        verifier_read_only_overlays = [
            *(
                list(path_policy.get("contract_paths") or [])
                if str(path_policy.get("contract_mode") or "review_guarded")
                == "file_frozen"
                else []
            ),
            *([developer_test_path] if developer_test_path else []),
        ]
        return candidate, verifier_read_only_overlays, verifier_references, work_view
