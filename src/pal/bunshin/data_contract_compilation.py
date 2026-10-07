from __future__ import annotations
from typing import Any
from pal.bunshin.graph_protocol import GraphIR
from dataclasses import dataclass
from pal.bunshin.contract_runtime import ContractArtifactAccess
from pal.bunshin.adapters import ARTIFACT_BUNDLE_ADAPTER, provision_artifact_workspaces
from pal.bunshin.artifacts import ArtifactRef
from pal.bunshin.contracts import AggregateType
from pal.bunshin.repository import BunshinV2Repository
from pal.bunshin.execution_models import ExecutionCompilation
from pal.bunshin.execution_values import _action
from pal.bunshin.graph_installation import _install_execution_graph
from pal.bunshin.module_identity_facts import _module_identity_delta
from pal.bunshin.module_identity_facts import _module_role_session_generations
from pal.bunshin.execution_values import _stable_json_hash
from pal.bunshin.execution_graph_facts import _topological_module_order
from pal.bunshin.module_identity import reconcile_module_identities


@dataclass
class ArtifactEpochCompiler:
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
    ) -> ExecutionCompilation:
        artifact = dict(self.contracts.artifacts.read_json(manifest_ref))
        contract = dict(artifact.get("contract") or {})
        installed_graph = _install_execution_graph(
            artifact,
            workflow_id=workflow_id,
            repository=self.repository,
        )
        graph = installed_graph.execution.graph
        modules = {
            str(name): dict(value or {})
            for name, value in dict(contract.get("modules") or {}).items()
            if str(dict(value or {}).get("execution") or "") == "produce"
        }
        if not modules:
            raise ValueError("ContractArtifact has no produced modules")
        if set(modules) != set(graph.nodes):
            raise ValueError(
                "ContractArtifact produced modules disagree with GraphIR nodes"
            )
        dependencies = {
            name: list(graph.execution_predecessors(name))
            for name in modules
        }
        _topological_module_order(dependencies)
        requirements = {
            str(name): dict(value or {})
            for name, value in dict(contract.get("requirements") or {}).items()
        }
        context = dict(contract.get("context") or {})
        scenarios = {
            str(name): dict(value or {})
            for name, value in dict(contract.get("scenarios") or {}).items()
        }
        topology_ref, module_refs = self.publish_module_contracts(manifest_ref, modules, scenarios, requirements, dependencies, context)
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
                    "graph_generation": graph.generation,
                },
            )
        )
        self.repository.transitions.dispatch(
            _action(
                "START_EXECUTION",
                workflow_id,
                AggregateType.EXECUTION_EPOCH,
                epoch_id,
                actor,
                1,
                {},
            )
        )
        workspaces = provision_artifact_workspaces(
            self.repository.runtime_root,
            # Artifact modules follow the same workflow/module ownership model
            # as Git-backed modules.  Epochs are audit generations, not
            # workspace owners.
            epoch_id=workflow_id,
            unit_ids=sorted(modules),
        )
        unit_node_ids = {
            name: f"{epoch_id}:node:{name}" for name in modules
        }
        responsibilities = {
            name: str(module.get("responsibility") or "")
            for name, module in modules.items()
        }
        module_identity_delta = _module_identity_delta(
            self.repository,
            workflow_id=workflow_id,
            source_epoch_id=source_epoch_id,
            target_module_responsibilities=responsibilities,
        )
        generations = _module_role_session_generations(
            self.repository,
            workflow_id=workflow_id,
            source_epoch_id=source_epoch_id,
            target_subjects=set(modules),
            replaced_subjects=set(module_identity_delta["replaced"]),
        )
        environment_fingerprint = _stable_json_hash(
            {
                "execution_adapter": ARTIFACT_BUNDLE_ADAPTER,
                "contract_schema": str(
                    artifact.get("contract_schema") or ""
                ),
            }
        )
        self.create_node_runs(
            workflow_id, epoch_id, actor, graph, unit_node_ids, responsibilities, module_refs, manifest_ref,
            generations, environment_fingerprint, dependencies, workspaces,
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
        sink_node_id = unit_node_ids[graph.sink]
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
        modules: dict[str, dict[str, Any]],
        scenarios: dict[str, dict[str, Any]],
        requirements: dict[str, dict[str, Any]],
        dependencies: dict[str, list[str]],
        context: dict[str, Any],
    ) -> tuple[ArtifactRef, dict[str, ArtifactRef]]:
        topology_ref = self.contracts.artifacts.put_json(
            {
                "module_dependencies": dependencies,
                "verification_scenarios": {
                    name: list(scenario.get("modules") or [])
                    for name, scenario in scenarios.items()
                },
            },
            artifact_type="ContractTopologyArtifact",
            child_refs=((manifest_ref.sha256, "contract"),),
        )
        module_refs: dict[str, ArtifactRef] = {}
        for name, module in modules.items():
            module_scenarios = {
                scenario_name: scenario
                for scenario_name, scenario in scenarios.items()
                if name in {
                    str(item)
                    for item in list(scenario.get("modules") or [])
                }
            }
            requirement_names = {
                requirement_name
                for requirement_name, requirement in requirements.items()
                if str(requirement.get("owner") or "") == name
            }
            requirement_names.update(
                str(requirement_name)
                for scenario in module_scenarios.values()
                for requirement_name in list(
                    scenario.get("requirement_refs") or []
                )
            )
            module_refs[name] = self.contracts.artifacts.put_json(
                {
                    "schema_version": "1",
                    "module_name": name,
                    "context": context,
                    "module": module,
                    "requirements": {
                        requirement_name: requirements[requirement_name]
                        for requirement_name in sorted(requirement_names)
                    },
                    "scenarios": module_scenarios,
                },
                artifact_type="ContractModuleArtifact",
                child_refs=((manifest_ref.sha256, "contract"),),
            )
        return topology_ref, module_refs

    def create_node_runs(
        self,
        workflow_id: str,
        epoch_id: str,
        actor: str,
        graph: GraphIR,
        unit_node_ids: dict[str, str],
        responsibilities: dict[str, str],
        module_refs: dict[str, ArtifactRef],
        manifest_ref: ArtifactRef,
        generations: dict[str, int],
        environment_fingerprint: str,
        dependencies: dict[str, list[str]],
        workspaces: dict[str, dict[str, str]],
    ) -> None:
        for name in sorted(unit_node_ids):
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
                        "module_responsibility": responsibilities[name],
                        "node_kind": "unit",
                        "unit_contract_ref": module_refs[name].to_dict(),
                        "architecture_manifest_ref": manifest_ref.to_dict(),
                        "dependency_node_ids": [
                            unit_node_ids[provider]
                            for provider in dependencies[name]
                        ],
                        "producer_dependency_node_ids": [
                            unit_node_ids[provider]
                            for provider in graph.producer_predecessors(name)
                        ],
                        "accepted_producer_dependency_node_ids": [],
                        "accepted_dependency_node_ids": [],
                        "epoch_frozen": False,
                        "role_session_generation": generations.get(name, 0),
                        "environment_fingerprint": environment_fingerprint,
                        **({"graph_sink": True} if name == graph.sink else {}),
                        **dict(workspaces[name]),
                    },
                )
            )
