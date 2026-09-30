from __future__ import annotations
from pal.bunshin.v2.graph_protocol import GraphIR
from dataclasses import dataclass
from typing import Any, Mapping
from pal.bunshin.v2.contract_runtime import ContractArtifactAccess
from pal.bunshin.v2.adapters import SOFTWARE_GIT_ADAPTER
from pal.bunshin.v2.artifacts import ArtifactRef
from pal.bunshin.v2.contracts import AggregateType
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.graph_protocol import EdgeKind
from pal.bunshin.v2.skeleton import SKELETON_MODULE_CONTRACT_ARTIFACT
from pal.bunshin.v2.workspace_paths import module_developer_test_path, module_verification_corpus_path
from pal.bunshin.v2.execution_models import ExecutionCompilation
from pal.bunshin.v2.execution_values import _action
from pal.bunshin.v2.graph_installation import _install_execution_graph
from pal.bunshin.v2.module_identity_facts import _module_identity_delta
from pal.bunshin.v2.module_identity_facts import _module_role_session_generations
from pal.bunshin.v2.execution_values import _stable_json_hash
from pal.bunshin.v2.execution_graph_facts import _topological_module_order
from pal.bunshin.v2.module_workspaces import provision_skeleton_module_worktrees
from pal.bunshin.v2.module_identity import reconcile_module_identities


@dataclass
class SkeletonEpochCompiler:
    repository: BunshinV2Repository
    contracts: ContractArtifactAccess

    def compile(
        self,
        *,
        workflow_id: str,
        epoch_id: str,
        manifest_ref: ArtifactRef,
        actor: str,
        source_epoch_id: str,
        initial_repair_bill_ref: Mapping[str, Any] | None,
        artifact_override: Mapping[str, Any] | None = None,
    ) -> ExecutionCompilation:
        artifact = dict(
            artifact_override
            or self.contracts.artifacts.read_json(manifest_ref)
        )
        submission = dict(artifact.get("submission") or {})
        installed_graph = _install_execution_graph(
            artifact,
            workflow_id=workflow_id,
            repository=self.repository,
        )
        graph = installed_graph.execution.graph
        modules = {str(name): dict(value or {}) for name, value in dict(submission.get("modules") or {}).items()}
        if not modules:
            raise ValueError("software ContractArtifact has no modules")
        implementation_modules = {
            name: module
            for name, module in modules.items()
            if name in graph.nodes
        }
        if not implementation_modules:
            raise ValueError("software ContractArtifact has no implementation modules")
        if initial_repair_bill_ref and len(implementation_modules) != 1:
            raise ValueError("an initial RepairBill requires a bounded single-module skeleton")
        module_dependencies = {
            name: [str(item) for item in dict(module.get("dependencies") or {})]
            for name, module in modules.items()
        }
        _topological_module_order(module_dependencies)
        scenarios = {
            str(name): dict(value or {})
            for name, value in dict(submission.get("scenarios") or {}).items()
        }
        if not scenarios:
            raise ValueError("software ContractArtifact has no end-to-end verification scenarios")
        requirements = {
            str(name): dict(value or {})
            for name, value in dict(submission.get("requirements") or {}).items()
        }
        if not requirements:
            raise ValueError("software ContractArtifact has no requirement mappings")
        topology_ref, module_refs = self.publish_module_contracts(manifest_ref, artifact, modules, scenarios, requirements, module_dependencies)
        self.publish_epoch(actor, artifact, epoch_id, graph, manifest_ref, topology_ref, workflow_id)
        workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        request = (
            self.contracts.artifacts.read_json(dict(workflow.payload.get("request_ref") or {}))
            if workflow is not None and workflow.payload.get("request_ref")
            else {}
        )
        workspaces = provision_skeleton_module_worktrees(
            self.repository.runtime_root,
            artifacts=self.contracts.artifacts,
            workflow_id=workflow_id,
            workflow_name=str(request.get("workflow_name") or request.get("goal") or workflow_id),
            unit_ids=sorted(implementation_modules),
            workspace=dict(request.get("workspace") or {}),
            architecture_artifact=artifact,
        )
        environment_fingerprint = _stable_json_hash(
            {
                "execution_adapter": SOFTWARE_GIT_ADAPTER,
                "workspace_environment_policy": dict(
                    dict(request.get("workspace") or {}).get("workspace_environment_policy") or {}
                ),
                "toolchain": dict(request.get("toolchain") or {}),
            }
        )
        unit_node_ids = {name: f"{epoch_id}:node:{name}" for name in implementation_modules}
        sink_node_id = unit_node_ids[graph.sink]
        module_responsibilities = {
            name: str(modules[name].get("responsibility") or "")
            for name in implementation_modules
        }
        module_identity_delta = _module_identity_delta(
            self.repository,
            workflow_id=workflow_id,
            source_epoch_id=source_epoch_id,
            target_module_responsibilities=module_responsibilities,
        )
        source_role_generations = _module_role_session_generations(
            self.repository,
            workflow_id=workflow_id,
            source_epoch_id=source_epoch_id,
            target_subjects=set(implementation_modules),
            replaced_subjects=set(module_identity_delta["replaced"]),
        )
        self.create_node_runs(
            workflow_id, epoch_id, actor, graph, unit_node_ids, module_responsibilities, module_refs, manifest_ref,
            source_role_generations, environment_fingerprint, initial_repair_bill_ref, workspaces,
        )
        if source_epoch_id:
            reconcile_module_identities(
                repository=self.repository,
                contracts=self.contracts,
                workflow_id=workflow_id,
                source_epoch_id=source_epoch_id,
                target_epoch_id=epoch_id,
                target_manifest_ref=manifest_ref,
                target_graph=graph,
                installed_graph_diff=installed_graph.diff,
                actor=actor,
            )
        node_ids = tuple(
            unit_node_ids[name] for name in sorted(unit_node_ids)
        )
        self.repository.transitions.dispatch(
            _action(
                "NODES_COMPILED",
                workflow_id,
                AggregateType.EXECUTION_EPOCH,
                epoch_id,
                actor,
                2,
                {
                    "node_ids": list(node_ids),
                    "implementation_node_ids": list(unit_node_ids.values()),
                    "sink_node_id": sink_node_id,
                    "module_identity_delta": module_identity_delta,
                },
            )
        )
        return ExecutionCompilation(
            epoch_id=epoch_id,
            node_run_ids=node_ids,
            unit_node_ids=unit_node_ids,
            sink_node_id=sink_node_id,
        )


    def publish_module_contracts(
        self,
        manifest_ref: ArtifactRef,
        artifact: dict[str, Any],
        modules: dict[str, dict[str, Any]],
        scenarios: dict[str, dict[str, Any]],
        requirements: dict[str, dict[str, Any]],
        module_dependencies: dict[str, list[str]],
    ) -> tuple[ArtifactRef, dict[str, ArtifactRef]]:
        topology_ref = self.contracts.artifacts.put_json(
            {
                "module_dependencies": module_dependencies,
                "verification_scenarios": {
                    name: list(scenario.get("modules") or [])
                    for name, scenario in scenarios.items()
                },
                "scenario_requirements": {
                    name: list(scenario.get("requirement_refs") or [])
                    for name, scenario in scenarios.items()
                },
            },
            artifact_type="SkeletonTopologyArtifact",
            child_refs=((manifest_ref.sha256, "architecture_skeleton"),),
        )
        module_refs: dict[str, ArtifactRef] = {}
        contract_file_hashes = dict(artifact.get("contract_file_hashes") or {})
        for name, module in modules.items():
            paths = dict(module.get("paths") or {})
            module_scenarios = {
                scenario_name: scenario
                for scenario_name, scenario in scenarios.items()
                if name in set(str(item) for item in list(scenario.get("modules") or []))
            }
            module_requirement_names = {
                requirement_name
                for scenario in module_scenarios.values()
                for requirement_name in list(scenario.get("requirement_refs") or [])
            }
            module_requirement_names.update(
                requirement_name
                for requirement_name, requirement in requirements.items()
                if str(requirement.get("owner") or "") == name
            )
            semantic_module = {key: value for key, value in module.items() if key != "paths"}
            module_refs[name] = self.contracts.artifacts.put_json(
                {
                    "module_name": name,
                    "module": semantic_module,
                    "paths": paths,
                    "contract_file_hashes": {
                        path: str(contract_file_hashes.get(path) or "")
                        for path in list(paths.get("contract_paths") or [])
                    },
                    "requirements": {
                        requirement_name: requirements[requirement_name]
                        for requirement_name in sorted(module_requirement_names)
                    },
                    "scenarios": module_scenarios,
                },
                artifact_type=SKELETON_MODULE_CONTRACT_ARTIFACT,
                child_refs=((manifest_ref.sha256, "architecture_skeleton"),),
            )
        return topology_ref, module_refs

    def create_node_runs(
        self,
        workflow_id: str,
        epoch_id: str,
        actor: str,
        graph: GraphIR,
        unit_node_ids: dict[str, str],
        module_responsibilities: dict[str, str],
        module_refs: dict[str, ArtifactRef],
        manifest_ref: ArtifactRef,
        source_role_generations: dict[str, int],
        environment_fingerprint: str,
        initial_repair_bill_ref: Mapping[str, Any] | None,
        workspaces: dict[str, dict[str, str]],
    ) -> None:
        for name in sorted(unit_node_ids):
            paths = dict(graph.nodes[name].workspace_policy)
            self.repository.transitions.dispatch(
                _action(
                    "CREATE_NODE_RUN",
                    workflow_id,
                    AggregateType.DAG_NODE_RUN,
                    unit_node_ids[name],
                    actor,
                    0,
                    {
                        "epoch_id": epoch_id,
                        "graph_generation": graph.generation,
                        "graph_contract_hash": graph.nodes[name].contract_hash,
                        "unit_id": name,
                        "module_name": name,
                        "module_responsibility": module_responsibilities[name],
                        "node_kind": "unit",
                        "unit_contract_ref": module_refs[name].to_dict(),
                        "architecture_manifest_ref": manifest_ref.to_dict(),
                        "dependency_node_ids": [
                            unit_node_ids[item]
                            for item in graph.execution_predecessors(name)
                        ],
                        "producer_dependency_node_ids": [
                            unit_node_ids[item]
                            for item in graph.producer_predecessors(name)
                        ],
                        "accepted_producer_dependency_node_ids": [],
                        "contract_dependency_node_ids": [
                            unit_node_ids[edge.producer]
                            for edge in graph.incoming(name)
                            if (
                                edge.kind == EdgeKind.CONTRACT
                                and edge.producer in unit_node_ids
                            )
                        ],
                        "accepted_dependency_node_ids": [],
                        "epoch_frozen": False,
                        "role_session_generation": source_role_generations.get(
                            name,
                            0,
                        ),
                        "environment_fingerprint": environment_fingerprint,
                        "path_policy": {
                            "contract_mode": str(paths.get("contract_mode") or "review_guarded"),
                            "contract_paths": list(paths.get("contract_paths") or []),
                            "implementation_scopes": list(paths.get("implementation_scopes") or []),
                            "developer_tests": {
                                "kind": "directory",
                                "path": module_developer_test_path(name),
                            },
                            "verification_corpus": {
                                "kind": "directory",
                                "path": module_verification_corpus_path(name),
                            },
                            "reference_only": list(paths.get("reference_only") or []),
                            "workspace_authorities": [
                                dict(item)
                                for item in list(
                                    paths.get("workspace_authorities") or []
                                )
                            ],
                        },
                        **({"graph_sink": True} if name == graph.sink else {}),
                        **(
                            {"historical_repair_bill_refs": [dict(initial_repair_bill_ref)]}
                            if initial_repair_bill_ref
                            else {}
                        ),
                        **dict(workspaces[name]),
                    },
                )
            )

    def publish_epoch(
        self,
        actor: str,
        artifact: dict[str, Any],
        epoch_id: str,
        graph: GraphIR,
        manifest_ref: ArtifactRef,
        topology_ref: ArtifactRef,
        workflow_id: str,
    ) -> None:
        self.repository.transitions.dispatch(
            _action(
                "CREATE_EXECUTION_EPOCH",
                workflow_id,
                AggregateType.EXECUTION_EPOCH,
                epoch_id,
                actor,
                0,
                {
                    "architecture_manifest_ref": manifest_ref.to_dict(),
                    "topology_ref": topology_ref.to_dict(),
                    "architecture_manifest_sha": manifest_ref.sha256,
                    "skeleton_commit_sha": str(artifact.get("skeleton_commit_sha") or ""),
                    "graph_generation": graph.generation,
                },
            )
        )
        self.repository.transitions.dispatch(
            _action("START_EXECUTION", workflow_id, AggregateType.EXECUTION_EPOCH, epoch_id, actor, 1, {})
        )
