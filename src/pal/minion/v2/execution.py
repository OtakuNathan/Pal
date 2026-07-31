from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import os
import signal
import subprocess
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, IO, Iterator, Mapping

from pal.minion.v2.contract_runtime import ContractArtifactAccess
from pal.minion.v2.adapters import (
    ARTIFACT_BUNDLE_ADAPTER,
    SOFTWARE_GIT_ADAPTER,
    artifact_tree_fingerprint,
    provision_artifact_workspaces,
)
from pal.minion.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.minion.v2.contracts import ActionEnvelope, AggregateSnapshot, AggregateType, DispatchResult
from pal.minion.v2.contract_protocol import (
    CONTRACT_ARTIFACT,
    software_contract_projection,
)
from pal.minion.v2.paths import (
    ProjectGitLayout,
    project_git_layout_lock,
    resolve_project_git_layout,
    verification_scratch_root,
)
from pal.minion.v2.repository import MinionV2Repository
from pal.minion.v2.role_contracts import (
    family_execution_adapter,
    validate_family_binding_payload,
)
from pal.minion.v2.sessions import (
    coder_session_id,
    module_verifier_session_id,
    node_role_generation,
)
from pal.minion.v2.skeleton import (
    SKELETON_MODULE_CONTRACT_ARTIFACT,
    compiled_module_write_scopes,
    module_developer_test_path,
    module_verification_corpus_path,
)
from pal.minion.v2.verification import module_revision_fingerprint, repair_bill_semantic_view


@dataclass(frozen=True)
class ExecutionCompilation:
    epoch_id: str
    node_run_ids: tuple[str, ...]
    unit_node_ids: Mapping[str, str]
    system_verification_node_id: str = ""
    integration_node_id: str = ""


@dataclass(frozen=True)
class NodeRunJournal:
    current_micro_plan: tuple[str, ...] = ()
    completed_checklist: tuple[str, ...] = ()
    files_inspected: tuple[str, ...] = ()
    files_changed: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    known_failures: tuple[str, ...] = ()
    last_safe_point: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_micro_plan": list(self.current_micro_plan),
            "completed_checklist": list(self.completed_checklist),
            "files_inspected": list(self.files_inspected),
            "files_changed": list(self.files_changed),
            "open_questions": list(self.open_questions),
            "known_failures": list(self.known_failures),
            "last_safe_point": self.last_safe_point,
        }


@dataclass(frozen=True)
class WorkspaceProcessHolder:
    pid: int
    process_group: int
    command: str
    holds_cwd: bool = False
    read_paths: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()
    unknown_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "process_group": self.process_group,
            "command": self.command,
            "holds_cwd": self.holds_cwd,
            "read_paths": list(self.read_paths),
            "write_paths": list(self.write_paths),
            "unknown_paths": list(self.unknown_paths),
        }


@dataclass
class WorkspaceLockRegistry:
    _streams_by_workspace: dict[str, IO[str]] = field(default_factory=dict, init=False)
    _workspace_by_owner: dict[str, str] = field(default_factory=dict, init=False)

    def acquire(self, owner_id: str, workspace: Path) -> Path:
        import fcntl

        canonical_workspace = os.path.realpath(workspace)
        if owner_id in self._workspace_by_owner:
            raise RuntimeError(f"workspace lock owner already holds a lock: {owner_id}")
        if canonical_workspace in self._streams_by_workspace:
            raise BlockingIOError(
                f"workspace lock is already held: {canonical_workspace}"
            )
        git_probe = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if git_probe.returncode == 0:
            git_dir = (workspace / git_probe.stdout.strip()).resolve()
            lock_path = git_dir / "pal-minion-v2.snapshot.lock"
        else:
            workspace_key = hashlib.sha256(
                canonical_workspace.encode("utf-8")
            ).hexdigest()[:24]
            lock_path = workspace.parent / ".pal-candidate-locks" / f"{workspace_key}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        stream = lock_path.open("a+")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            stream.close()
            raise
        self._streams_by_workspace[canonical_workspace] = stream
        self._workspace_by_owner[owner_id] = canonical_workspace
        return lock_path

    def release(self, owner_id: str) -> None:
        import fcntl

        canonical_workspace = self._workspace_by_owner.pop(owner_id, None)
        stream = (
            self._streams_by_workspace.pop(canonical_workspace, None)
            if canonical_workspace is not None
            else None
        )
        if stream is None:
            return
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def is_held(self, owner_id: str) -> bool:
        return owner_id in self._workspace_by_owner


@dataclass
class ExecutionCompiler:
    repository: MinionV2Repository
    contracts: ContractArtifactAccess

    def compile_epoch(
        self,
        *,
        workflow_id: str,
        epoch_id: str,
        manifest_ref: ArtifactRef,
        actor: str = "minion-manager",
        source_epoch_id: str = "",
        initial_repair_bill_ref: Mapping[str, Any] | None = None,
    ) -> ExecutionCompilation:
        record = self.repository.read_artifact_record(manifest_ref.sha256)
        if record and str(record.get("artifact_type") or "") == CONTRACT_ARTIFACT:
            artifact = dict(
                self.contracts.artifacts.read_json(manifest_ref)
            )
            contract = dict(artifact.get("contract") or {})
            execution_adapter = _workflow_execution_adapter(
                self.repository,
                self.contracts,
                workflow_id,
            )
            if execution_adapter == SOFTWARE_GIT_ADAPTER:
                return self._compile_skeleton_epoch(
                    workflow_id=workflow_id,
                    epoch_id=epoch_id,
                    manifest_ref=manifest_ref,
                    actor=actor,
                    source_epoch_id=source_epoch_id,
                    initial_repair_bill_ref=initial_repair_bill_ref,
                    artifact_override={
                        **artifact,
                        "submission": software_contract_projection(contract),
                    },
                )
            if execution_adapter == ARTIFACT_BUNDLE_ADAPTER:
                return self._compile_data_contract_epoch(
                    workflow_id=workflow_id,
                    epoch_id=epoch_id,
                    manifest_ref=manifest_ref,
                    actor=actor,
                    source_epoch_id=source_epoch_id,
                )
            raise ValueError(
                "workflow selected an unsupported execution adapter: "
                + execution_adapter
            )
        raise ValueError("execution requires a ContractArtifact")

    def _compile_skeleton_epoch(
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
        modules = {str(name): dict(value or {}) for name, value in dict(submission.get("modules") or {}).items()}
        if not modules:
            raise ValueError("software ContractArtifact has no modules")
        implementation_modules = {
            name: module
            for name, module in modules.items()
            if str(module.get("module_kind") or "") == "implementation"
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
        scenario_catalog_ref = self.contracts.artifacts.put_json(
            {
                "schema_version": "1",
                "kind": "system_delivery",
                "scenarios": {
                    name: {
                        "modules": [
                            str(item) for item in list(scenario.get("modules") or [])
                        ],
                        "entrypoints": [str(scenario.get("entrypoint") or "")],
                        "contract_flow": list(scenario.get("contract_flow") or []),
                        "observable_behavior": str(
                            scenario.get("observable_behavior") or ""
                        ),
                        "failure_behavior": str(
                            scenario.get("failure_behavior") or ""
                        ),
                        "environment": {
                            "description": str(scenario.get("environment") or "")
                        },
                        "requirements": {
                            requirement_name: requirements[requirement_name]
                            for requirement_name in [
                                str(item)
                                for item in list(
                                    scenario.get("requirement_refs") or []
                                )
                            ]
                        },
                    }
                    for name, scenario in sorted(scenarios.items())
                },
            },
            artifact_type="ScenarioCatalogArtifact",
            child_refs=((manifest_ref.sha256, "architecture_skeleton"),),
        )
        self.repository.dispatch(
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
                },
            )
        )
        self.repository.dispatch(
            _action("START_EXECUTION", workflow_id, AggregateType.EXECUTION_EPOCH, epoch_id, actor, 1, {})
        )
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        request = (
            self.contracts.artifacts.read_json(dict(workflow.payload.get("request_ref") or {}))
            if workflow is not None and workflow.payload.get("request_ref")
            else {}
        )
        system_unit_id = "system_delivery"
        workspaces = provision_skeleton_module_worktrees(
            self.repository.runtime_root,
            artifacts=self.contracts.artifacts,
            workflow_id=workflow_id,
            workflow_name=str(request.get("workflow_name") or request.get("goal") or workflow_id),
            unit_ids=sorted([*implementation_modules, system_unit_id]),
            verification_unit_ids={system_unit_id},
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
        global_constraint_hash = _stable_json_hash(
            {
                "constraints": list(request.get("constraints") or []),
                "requirements": self.contracts.artifacts.read_json(dict(artifact.get("requirements_ref") or {})),
            }
        )
        unit_node_ids = {name: f"{epoch_id}:node:{name}" for name in implementation_modules}
        system_verification_node_id = f"{epoch_id}:system-verification"
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
            target_subjects={*implementation_modules, system_unit_id},
            replaced_subjects=set(module_identity_delta["replaced"]),
        )
        for name in sorted(implementation_modules):
            paths = dict(modules[name].get("paths") or {})
            self.repository.dispatch(
                _action(
                    "CREATE_NODE_RUN",
                    workflow_id,
                    AggregateType.DAG_NODE_RUN,
                    unit_node_ids[name],
                    actor,
                    0,
                    {
                        "epoch_id": epoch_id,
                        "unit_id": name,
                        "module_name": name,
                        "module_responsibility": module_responsibilities[name],
                        "node_kind": "unit",
                        "unit_contract_ref": module_refs[name].to_dict(),
                        "architecture_manifest_ref": manifest_ref.to_dict(),
                        # Contract dependencies describe protocol/data flow. Accepted
                        # Skeleton contracts make them available before implementation,
                        # so they are not Coder start barriers.
                        "dependency_node_ids": [],
                        "contract_dependency_node_ids": [
                            unit_node_ids[item]
                            for item in module_dependencies[name]
                            if item in unit_node_ids
                        ],
                        "accepted_dependency_node_ids": [],
                        "epoch_frozen": False,
                        "role_session_generation": source_role_generations.get(
                            name,
                            0,
                        ),
                        "environment_fingerprint": environment_fingerprint,
                        "global_constraint_hash": global_constraint_hash,
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
                        },
                        **(
                            {"historical_repair_bill_refs": [dict(initial_repair_bill_ref)]}
                            if initial_repair_bill_ref
                            else {}
                        ),
                        **dict(workspaces[name]),
                    },
                )
            )
        self.repository.dispatch(
            _action(
                "CREATE_NODE_RUN",
                workflow_id,
                AggregateType.DAG_NODE_RUN,
                system_verification_node_id,
                actor,
                0,
                {
                    "epoch_id": epoch_id,
                    "unit_id": system_unit_id,
                    "module_name": system_unit_id,
                    "node_kind": "system_verification",
                    "unit_contract_ref": scenario_catalog_ref.to_dict(),
                    "scenario_catalog_ref": scenario_catalog_ref.to_dict(),
                    "architecture_manifest_ref": manifest_ref.to_dict(),
                    "dependency_node_ids": [
                        unit_node_ids[name] for name in sorted(unit_node_ids)
                    ],
                    "accepted_dependency_node_ids": [],
                    "epoch_frozen": False,
                    "role_session_generation": source_role_generations.get(
                        system_unit_id,
                        0,
                    ),
                    "environment_fingerprint": environment_fingerprint,
                    "global_constraint_hash": global_constraint_hash,
                    "path_policy": {
                        "contract_mode": "read_only",
                        "contract_paths": [],
                        "implementation_scopes": [],
                        "developer_tests": None,
                        "verification_corpus": None,
                        "reference_only": [],
                    },
                    **dict(workspaces[system_unit_id]),
                },
            )
        )
        if source_epoch_id:
            reconcile_module_identities(
                repository=self.repository,
                contracts=self.contracts,
                workflow_id=workflow_id,
                source_epoch_id=source_epoch_id,
                target_epoch_id=epoch_id,
                target_manifest_ref=manifest_ref,
                actor=actor,
            )
        node_ids = tuple(
            [unit_node_ids[name] for name in sorted(unit_node_ids)]
            + [system_verification_node_id]
        )
        self.repository.dispatch(
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
                    "system_verification_node_id": system_verification_node_id,
                    "module_identity_delta": module_identity_delta,
                },
            )
        )
        return ExecutionCompilation(
            epoch_id=epoch_id,
            node_run_ids=node_ids,
            unit_node_ids=unit_node_ids,
            system_verification_node_id=system_verification_node_id,
        )

    def _compile_data_contract_epoch(
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
        modules = {
            str(name): dict(value or {})
            for name, value in dict(contract.get("modules") or {}).items()
            if str(dict(value or {}).get("execution") or "") == "produce"
        }
        if not modules:
            raise ValueError("ContractArtifact has no produced modules")
        dependencies = {
            name: [
                str(provider)
                for provider in dict(module.get("dependencies") or {})
                if str(provider) in modules
            ]
            for name, module in modules.items()
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
        scenario_ref = self.contracts.artifacts.put_json(
            {
                "schema_version": "1",
                "kind": "integration",
                "scenarios": scenarios,
                "requirements": requirements,
            },
            artifact_type="ScenarioCatalogArtifact",
            child_refs=((manifest_ref.sha256, "contract"),),
        )
        self.repository.dispatch(
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
                },
            )
        )
        self.repository.dispatch(
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
            target_subjects={*modules, "integration"},
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
        for name in sorted(modules):
            self.repository.dispatch(
                _action(
                    "CREATE_NODE_RUN",
                    workflow_id,
                    AggregateType.DAG_NODE_RUN,
                    unit_node_ids[name],
                    actor,
                    0,
                    {
                        "epoch_id": epoch_id,
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
                        "accepted_dependency_node_ids": [],
                        "epoch_frozen": False,
                        "role_session_generation": generations.get(name, 0),
                        "environment_fingerprint": environment_fingerprint,
                        **dict(workspaces[name]),
                    },
                )
            )
        integration_node_id = f"{epoch_id}:node:integration"
        self.repository.dispatch(
            _action(
                "CREATE_NODE_RUN",
                workflow_id,
                AggregateType.DAG_NODE_RUN,
                integration_node_id,
                actor,
                0,
                {
                    "epoch_id": epoch_id,
                    "unit_id": "integration",
                    "module_name": "integration",
                    "node_kind": "integration",
                    "unit_contract_ref": scenario_ref.to_dict(),
                    "scenario_catalog_ref": scenario_ref.to_dict(),
                    "architecture_manifest_ref": manifest_ref.to_dict(),
                    "dependency_node_ids": [
                        unit_node_ids[name]
                        for name in _topological_module_order(dependencies)
                    ],
                    "accepted_dependency_node_ids": [],
                    "epoch_frozen": False,
                    "role_session_generation": generations.get(
                        "integration", 0
                    ),
                    "environment_fingerprint": environment_fingerprint,
                    **dict(workspaces["integration"]),
                },
            )
        )
        if source_epoch_id:
            reconcile_module_identities(
                repository=self.repository,
                contracts=self.contracts,
                workflow_id=workflow_id,
                source_epoch_id=source_epoch_id,
                target_epoch_id=epoch_id,
                target_manifest_ref=manifest_ref,
                actor=actor,
            )
        node_ids = tuple(
            [unit_node_ids[name] for name in sorted(unit_node_ids)]
            + [integration_node_id]
        )
        self.repository.dispatch(
            _action(
                "NODES_COMPILED",
                workflow_id,
                AggregateType.EXECUTION_EPOCH,
                epoch_id,
                actor,
                2,
                {
                    "node_ids": list(node_ids),
                    "integration_node_id": integration_node_id,
                    "module_identity_delta": module_identity_delta,
                },
            )
        )
        return ExecutionCompilation(
            epoch_id=epoch_id,
            node_run_ids=node_ids,
            unit_node_ids=unit_node_ids,
            integration_node_id=integration_node_id,
        )


def _workflow_execution_adapter(
    repository: MinionV2Repository,
    contracts: ContractArtifactAccess,
    workflow_id: str,
) -> str:
    workflow = repository.read_snapshot(
        AggregateType.WORKFLOW,
        workflow_id,
    )
    if workflow is None:
        raise ValueError(
            f"workflow is unavailable while resolving execution strategy: "
            f"{workflow_id}"
        )
    binding_ref = dict(workflow.payload.get("family_binding_ref") or {})
    if not binding_ref.get("sha256"):
        raise ValueError("workflow has no pinned FamilyBindingArtifact")
    binding = dict(contracts.artifacts.read_json(binding_ref))
    validate_family_binding_payload(binding)
    return family_execution_adapter(binding.get("execution_adapter"))


def reconcile_module_identities(
    *,
    repository: MinionV2Repository,
    contracts: ContractArtifactAccess,
    workflow_id: str,
    source_epoch_id: str,
    target_epoch_id: str,
    target_manifest_ref: ArtifactRef,
    actor: str,
) -> tuple[str, ...]:
    source_epoch = repository.read_snapshot(
        AggregateType.EXECUTION_EPOCH, source_epoch_id
    )
    if source_epoch is None:
        return ()
    source_manifest_ref = ArtifactRef.from_mapping(
        dict(source_epoch.payload.get("architecture_manifest_ref") or {})
    )
    source_record = repository.read_artifact_record(
        source_manifest_ref.sha256
    )
    target_record = repository.read_artifact_record(
        target_manifest_ref.sha256
    )
    if not (
        _artifact_is_contract(source_record)
        and _artifact_is_contract(target_record)
    ):
        return ()
    execution_adapter = _workflow_execution_adapter(
        repository,
        contracts,
        workflow_id,
    )
    if execution_adapter == SOFTWARE_GIT_ADAPTER:
        return _reconcile_skeleton_module_identities(
            repository=repository,
            contracts=contracts,
            workflow_id=workflow_id,
            source_epoch_id=source_epoch_id,
            target_epoch_id=target_epoch_id,
            source_manifest_ref=source_manifest_ref,
            target_manifest_ref=target_manifest_ref,
            actor=actor,
        )
    if execution_adapter == ARTIFACT_BUNDLE_ADAPTER:
        return _reconcile_data_contract_module_identities(
            repository=repository,
            contracts=contracts,
            workflow_id=workflow_id,
            source_epoch_id=source_epoch_id,
            target_epoch_id=target_epoch_id,
            actor=actor,
        )
    raise ValueError(
        "workflow selected an unsupported execution adapter: "
        + execution_adapter
    )


def _reconcile_data_contract_module_identities(
    *,
    repository: MinionV2Repository,
    contracts: ContractArtifactAccess,
    workflow_id: str,
    source_epoch_id: str,
    target_epoch_id: str,
    actor: str,
) -> tuple[str, ...]:
    """Carry exact immutable artifact modules across a contract revision."""

    snapshots = repository.list_workflow_snapshots(workflow_id)
    source_nodes = {
        str(item.payload.get("module_name") or item.payload.get("unit_id") or ""): item
        for item in snapshots
        if item.aggregate_type == AggregateType.DAG_NODE_RUN
        and str(item.payload.get("epoch_id") or "") == source_epoch_id
        and str(item.payload.get("node_kind") or "") == "unit"
    }
    target_nodes = {
        str(item.payload.get("module_name") or item.payload.get("unit_id") or ""): item
        for item in snapshots
        if item.aggregate_type == AggregateType.DAG_NODE_RUN
        and str(item.payload.get("epoch_id") or "") == target_epoch_id
        and str(item.payload.get("node_kind") or "") == "unit"
    }
    target_dependencies = {
        name: [
            str(provider)
            for provider in dict(
                dict(
                    contracts.artifacts.read_json(
                        dict(node.payload.get("unit_contract_ref") or {})
                    )
                ).get("module")
                or {}
            ).get("dependencies", {})
            if str(provider) in target_nodes
        ]
        for name, node in target_nodes.items()
    }
    carried: list[str] = []
    for name in _topological_module_order(target_dependencies):
        source = source_nodes.get(name)
        target = repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            target_nodes[name].aggregate_id,
        )
        if source is None or target is None:
            continue
        if (
            source.state != "ACCEPTED"
            or target.state != "BLOCKED_BY_DEPS"
            or not _same_module_identity(source, target)
            or dict(source.payload.get("unit_contract_ref") or {})
            != dict(target.payload.get("unit_contract_ref") or {})
        ):
            continue
        accepted_dependencies = [
            str(item)
            for item in list(target.payload.get("dependency_node_ids") or [])
            if (
                repository.read_snapshot(
                    AggregateType.DAG_NODE_RUN,
                    str(item),
                )
                or target
            ).state
            == "ACCEPTED"
        ]
        if len(accepted_dependencies) != len(
            list(target.payload.get("dependency_node_ids") or [])
        ):
            continue
        candidate_ref = dict(source.payload.get("candidate_ref") or {})
        verification_ref = dict(
            source.payload.get("verification_artifact_ref") or {}
        )
        candidate_digest = str(source.payload.get("candidate_digest") or "")
        fingerprint = str(
            source.payload.get("module_revision_fingerprint") or ""
        )
        if not all((candidate_ref, verification_ref, candidate_digest)):
            continue
        if not fingerprint:
            fingerprint = _stable_json_hash(
                {
                    "unit_contract_ref": dict(
                        source.payload.get("unit_contract_ref") or {}
                    ),
                    "candidate_digest": candidate_digest,
                }
            )
        result = repository.dispatch(
            _action(
                "CARRY_FORWARD_MODULE",
                workflow_id,
                AggregateType.DAG_NODE_RUN,
                target.aggregate_id,
                actor,
                target.version,
                {
                    "candidate_ref": candidate_ref,
                    "candidate_digest": candidate_digest,
                    "verification_artifact_ref": verification_ref,
                    "module_revision_fingerprint": fingerprint,
                    "accepted_dependency_node_ids": accepted_dependencies,
                    "epoch_frozen": False,
                    "output_hashes": dict(
                        source.payload.get("output_hashes") or {}
                    ),
                    "dependency_output_hashes": dict(
                        source.payload.get("dependency_output_hashes") or {}
                    ),
                    "carried_forward_from_epoch_id": source_epoch_id,
                    "carried_forward_from_node_run_id": source.aggregate_id,
                },
            )
        )
        target_nodes[name] = result.snapshot
        carried.append(result.snapshot.aggregate_id)
    _retire_removed_module_resources(
        repository=repository,
        workflow_id=workflow_id,
        source_nodes=source_nodes,
        target_module_names=set(target_nodes),
    )
    return tuple(carried)


def _reconcile_skeleton_module_identities(
    *,
    repository: MinionV2Repository,
    contracts: ContractArtifactAccess,
    workflow_id: str,
    source_epoch_id: str,
    target_epoch_id: str,
    source_manifest_ref: ArtifactRef,
    target_manifest_ref: ArtifactRef,
    actor: str,
) -> tuple[str, ...]:
    source_artifact = dict(contracts.artifacts.read_json(source_manifest_ref))
    target_artifact = dict(contracts.artifacts.read_json(target_manifest_ref))
    snapshots = repository.list_workflow_snapshots(workflow_id)
    source_nodes = {
        str(item.payload.get("module_name") or item.payload.get("unit_id") or ""): item
        for item in snapshots
        if item.aggregate_type == AggregateType.DAG_NODE_RUN
        and str(item.payload.get("epoch_id") or "") == source_epoch_id
        and str(item.payload.get("node_kind") or "") == "unit"
    }
    target_nodes = {
        str(item.payload.get("module_name") or item.payload.get("unit_id") or ""): item
        for item in snapshots
        if item.aggregate_type == AggregateType.DAG_NODE_RUN
        and str(item.payload.get("epoch_id") or "") == target_epoch_id
        and str(item.payload.get("node_kind") or "") == "unit"
    }
    source_contracts = {
        name: (
            dict(node.payload.get("unit_contract_ref") or {}),
            dict(contracts.artifacts.read_json(dict(node.payload.get("unit_contract_ref") or {}))),
        )
        for name, node in source_nodes.items()
    }
    target_contracts = {
        name: (
            dict(node.payload.get("unit_contract_ref") or {}),
            dict(contracts.artifacts.read_json(dict(node.payload.get("unit_contract_ref") or {}))),
        )
        for name, node in target_nodes.items()
    }
    contract_dependencies = {
        name: [
            str(item)
            for item in dict(dict(contract.get("module") or {}).get("dependencies") or {})
        ]
        for name, (_ref, contract) in target_contracts.items()
    }
    if set(contract_dependencies) != set(target_nodes):
        return ()
    # Contract-only modules participate in the architecture graph but do not
    # have execution nodes. Module revision fingerprints include implementation
    # dependency interfaces, but never waits for replacement implementations
    # or their optional output projections. The unit-contract hash still
    # covers the consumer's complete declared dependency set.
    dependencies = {
        name: [
            dependency
            for dependency in module_dependencies
            if dependency in target_nodes
        ]
        for name, module_dependencies in contract_dependencies.items()
    }
    carried_forward: list[str] = []
    for module_name in _topological_module_order(dependencies):
        source_node = source_nodes.get(module_name)
        target_node = repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            target_nodes[module_name].aggregate_id,
        )
        if source_node is None or target_node is None:
            continue
        if not _same_module_identity(source_node, target_node):
            _recreate_replaced_module_worktree(
                repository=repository,
                workflow_id=workflow_id,
                source_node=source_node,
                target_node=target_node,
            )
            continue
        source_contract = source_contracts.get(module_name)
        target_contract = target_contracts.get(module_name)
        if source_contract is None or target_contract is None:
            continue
        dependency_modules = dependencies[module_name]
        source_fingerprint = _skeleton_module_revision_signature(
            artifact=source_artifact,
            contract_ref=source_contract[0],
            contract=source_contract[1],
            node=source_node,
            dependencies=dependency_modules,
            contracts_by_module=source_contracts,
        )
        target_fingerprint = _skeleton_module_revision_signature(
            artifact=target_artifact,
            contract_ref=target_contract[0],
            contract=target_contract[1],
            node=target_node,
            dependencies=dependency_modules,
            contracts_by_module=target_contracts,
        )
        source_candidate_ref = dict(source_node.payload.get("candidate_ref") or {})
        verification_ref = dict(source_node.payload.get("verification_artifact_ref") or {})
        source_candidate_digest = str(source_node.payload.get("candidate_digest") or "")
        if not source_candidate_ref or not source_candidate_digest:
            (
                source_candidate_ref,
                source_candidate_digest,
            ) = _snapshot_module_workspace_for_replan(
                contracts=contracts,
                source_node=source_node,
                target_node=target_node,
            )
        module_head = _merge_architecture_into_preserved_module(
            source_node=source_node,
            target_node=target_node,
        )
        baseline = {
            "base_sha": module_head,
            "base_digest": module_head,
            "accepted_dependency_candidate_digests": [],
            "dependency_output_hashes": {},
            "dependency_outputs": {},
            "dependency_fingerprint": "",
        }
        if (
            source_node.state != "ACCEPTED"
            or not verification_ref
            or not source_fingerprint
            or source_fingerprint != target_fingerprint
        ):
            result = repository.dispatch(
                _action(
                    "PRESERVE_MODULE_WORKTREE",
                    workflow_id,
                    AggregateType.DAG_NODE_RUN,
                    target_node.aggregate_id,
                    actor,
                    target_node.version,
                    {
                        "preserved_from_epoch_id": source_epoch_id,
                        "preserved_from_node_run_id": source_node.aggregate_id,
                        "parent_candidate_digest": source_candidate_digest,
                        "module_replan_prepared": True,
                        "preserved_workspace_paths": [],
                        "replan_conflict_paths": [],
                        **baseline,
                    },
                )
            )
            target_nodes[module_name] = result.snapshot
            continue
        candidate_ref = _checkpoint_preserved_module_head(
            contracts=contracts,
            target_node=target_node,
            source_candidate_ref=source_candidate_ref,
            source_candidate_digest=source_candidate_digest,
            target_contract_ref=target_contract[0],
            module_head=module_head,
        )
        result = repository.dispatch(
            _action(
                "CARRY_FORWARD_MODULE",
                workflow_id,
                AggregateType.DAG_NODE_RUN,
                target_node.aggregate_id,
                actor,
                target_node.version,
                {
                    "candidate_ref": candidate_ref.to_dict(),
                    "candidate_digest": module_head,
                    "verification_artifact_ref": verification_ref,
                    "module_revision_fingerprint": target_fingerprint,
                    "accepted_dependency_node_ids": [],
                    "epoch_frozen": False,
                    "output_hashes": dict(source_node.payload.get("output_hashes") or {}),
                    "carried_forward_from_epoch_id": source_epoch_id,
                    **baseline,
                },
            )
        )
        target_nodes[module_name] = result.snapshot
        carried_forward.append(result.snapshot.aggregate_id)
    _retire_removed_module_resources(
        repository=repository,
        workflow_id=workflow_id,
        source_nodes=source_nodes,
        target_module_names=set(target_nodes),
    )
    return tuple(carried_forward)


def _artifact_is_contract(
    record: Mapping[str, Any] | None,
) -> bool:
    return (
        str(dict(record or {}).get("artifact_type") or "")
        == CONTRACT_ARTIFACT
    )


def _snapshot_module_workspace_for_replan(
    *,
    contracts: ContractArtifactAccess,
    source_node: AggregateSnapshot,
    target_node: AggregateSnapshot,
) -> tuple[dict[str, Any], str]:
    """Commit in-flight Module assets without changing its linear history."""

    worktree = Path(str(source_node.payload.get("workspace_path") or ""))
    if not worktree.is_dir():
        raise ValueError("preserved Module has no stable worktree")
    current_head = _git(worktree, "rev-parse", "HEAD").strip()
    _git(worktree, "add", "-A")
    tree_sha = _git(worktree, "write-tree").strip()
    current_tree = _git(worktree, "rev-parse", f"{current_head}^{{tree}}").strip()
    if tree_sha == current_tree:
        preserved_digest = current_head
    else:
        preserved_digest = _git(
            worktree,
            "-c",
            "user.name=Pal Minion",
            "-c",
            "user.email=minion@localhost",
            "commit-tree",
            tree_sha,
            "-p",
            current_head,
            "-m",
            (
                f"minion module checkpoint {target_node.aggregate_id}\n\n"
                f"Pal-Assignment-Key: replan-{_safe_ref(target_node.aggregate_id)}"
            ),
        ).strip()
        _git(worktree, "reset", "--hard", preserved_digest)
    changed_paths = [
        item.decode("utf-8", errors="surrogateescape")
        for item in _git_bytes(
            worktree,
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            current_head,
            preserved_digest,
            "--",
        ).split(b"\0")
        if item
    ]
    ref = contracts.artifacts.put_json(
        {
            "schema_version": "3",
            "candidate_digest": preserved_digest,
            "base_sha": current_head,
            "architecture_base_sha": str(source_node.payload.get("epoch_base_sha") or ""),
            "previous_head_sha": current_head,
            "changed_paths": sorted(set(changed_paths)),
            "capture_kind": "module_replan_assets",
            "source_node_run_id": source_node.aggregate_id,
        },
        artifact_type="GitCheckpointArtifact",
    )
    return ref.to_dict(), preserved_digest


def _merge_architecture_into_preserved_module(
    *,
    source_node: AggregateSnapshot,
    target_node: AggregateSnapshot,
) -> str:
    worktree = Path(str(target_node.payload.get("workspace_path") or ""))
    target_skeleton = str(target_node.payload.get("epoch_base_sha") or "")
    if not worktree.is_dir() or not target_skeleton:
        raise RuntimeError("preserved Module has incomplete canonical worktree metadata")
    if _git(worktree, "status", "--porcelain").strip():
        raise RuntimeError("preserved Module worktree is dirty after Manager checkpoint")
    current_head = _git(worktree, "rev-parse", "HEAD").strip()
    if _git_is_ancestor(worktree, target_skeleton, current_head):
        return current_head
    merged = subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "-c",
            "user.name=Pal Minion",
            "-c",
            "user.email=minion@localhost",
            "merge",
            "--no-ff",
            "--no-edit",
            target_skeleton,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if merged.returncode != 0:
        subprocess.run(
            ["git", "-C", str(worktree), "merge", "--abort"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        raise RuntimeError(
            "accepted Architecture cannot be merged into preserved Module "
            f"{source_node.payload.get('module_name') or source_node.payload.get('unit_id')}: "
            + (merged.stderr or merged.stdout or "unknown Git merge failure")
        )
    return _git(worktree, "rev-parse", "HEAD").strip()


def _checkpoint_preserved_module_head(
    *,
    contracts: ContractArtifactAccess,
    target_node: AggregateSnapshot,
    source_candidate_ref: Mapping[str, Any],
    source_candidate_digest: str,
    target_contract_ref: Mapping[str, Any],
    module_head: str,
) -> ArtifactRef:
    worktree = Path(str(target_node.payload.get("workspace_path") or ""))
    changed_paths = git_changed_paths(worktree, source_candidate_digest)
    payload = {
        "schema_version": "3",
        "node_run_id": target_node.aggregate_id,
        "candidate_digest": module_head,
        "base_sha": source_candidate_digest,
        "architecture_base_sha": str(target_node.payload.get("epoch_base_sha") or ""),
        "previous_head_sha": source_candidate_digest,
        "candidate_tree_sha": _git(
            worktree, "rev-parse", f"{module_head}^{{tree}}"
        ).strip(),
        "changed_paths": changed_paths,
        "unit_contract_hash": str(target_contract_ref.get("sha256") or ""),
        "carried_forward_from_candidate": str(
            source_candidate_ref.get("sha256") or ""
        ),
    }
    return contracts.artifacts.put_json(
        payload,
        artifact_type="GitCheckpointArtifact",
        child_refs=(
            (str(source_candidate_ref["sha256"]), "previous_checkpoint"),
            (str(target_contract_ref["sha256"]), "module_contract"),
        ),
    )


def _retire_removed_module_resources(
    *,
    repository: MinionV2Repository,
    workflow_id: str,
    source_nodes: Mapping[str, AggregateSnapshot],
    target_module_names: set[str],
) -> tuple[str, ...]:
    """Retire only Module identities deleted by the accepted architecture.

    The old epoch has already drained before its replacement can compile.  A
    process holder here therefore means Manager accounting is wrong, so the
    cutover fails instead of inventing another ownership mechanism.
    """

    retired: list[str] = []
    for module_name in sorted(set(source_nodes) - set(target_module_names)):
        source_node = source_nodes[module_name]
        workspace = Path(str(source_node.payload.get("workspace_path") or ""))
        common_git_dir = Path(str(source_node.payload.get("common_git_dir") or ""))
        branch = str(source_node.payload.get("worktree_branch") or "")
        if workspace.is_dir():
            holders = workspace_process_holders(workspace)
            if holders:
                raise RuntimeError(
                    f"removed Module {module_name!r} still has live workspace holders:\n"
                    + format_workspace_process_holders(holders)
                )

        generation = node_role_generation(source_node.payload)
        for session_id in (
            coder_session_id(workflow_id, module_name, generation),
            module_verifier_session_id(workflow_id, module_name, generation),
        ):
            repository.complete_role_session(session_id, status="cancelled")

        if workspace.is_dir():
            if not common_git_dir.is_dir() or "/module/" not in branch:
                raise RuntimeError(
                    f"removed Module {module_name!r} has invalid stable worktree metadata"
                )
            completed = subprocess.run(
                [
                    "git",
                    f"--git-dir={common_git_dir}",
                    "worktree",
                    "remove",
                    "--force",
                    str(workspace),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    completed.stderr
                    or completed.stdout
                    or f"failed to retire Module worktree {workspace}"
                )
        if branch and _git_branch_exists(common_git_dir, branch):
            _git_dir(common_git_dir, "branch", "-D", branch)
        retired.append(module_name)
    return tuple(retired)


def _same_module_identity(
    source_node: AggregateSnapshot,
    target_node: AggregateSnapshot,
) -> bool:
    source = str(source_node.payload.get("module_responsibility") or "")
    target = str(target_node.payload.get("module_responsibility") or "")
    # Existing runtime rows predate responsibility identity. Preserve them
    # once; all newly compiled nodes carry the explicit identity field.
    return not source or _normalized_responsibility(source) == _normalized_responsibility(target)


def _recreate_replaced_module_worktree(
    *,
    repository: MinionV2Repository,
    workflow_id: str,
    source_node: AggregateSnapshot,
    target_node: AggregateSnapshot,
) -> None:
    workspace = Path(str(target_node.payload.get("workspace_path") or ""))
    common_git_dir = Path(str(target_node.payload.get("common_git_dir") or ""))
    branch = str(target_node.payload.get("worktree_branch") or "")
    target_base = str(
        target_node.payload.get("epoch_base_sha")
        or target_node.payload.get("base_sha")
        or ""
    )
    if not workspace or not common_git_dir.is_dir() or not branch or not target_base:
        raise RuntimeError("replaced Module has incomplete canonical worktree metadata")
    holders = workspace_process_holders(workspace) if workspace.is_dir() else ()
    if holders:
        raise RuntimeError(
            "replaced Module still has live workspace holders:\n"
            + format_workspace_process_holders(holders)
        )
    generation = node_role_generation(source_node.payload)
    module_name = str(
        source_node.payload.get("module_name")
        or source_node.payload.get("unit_id")
        or ""
    )
    for session_id in (
        coder_session_id(workflow_id, module_name, generation),
        module_verifier_session_id(workflow_id, module_name, generation),
    ):
        repository.complete_role_session(session_id, status="cancelled")
    if workspace.is_dir():
        removed = subprocess.run(
            [
                "git",
                f"--git-dir={common_git_dir}",
                "worktree",
                "remove",
                "--force",
                str(workspace),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if removed.returncode != 0:
            raise RuntimeError(
                removed.stderr
                or removed.stdout
                or f"failed to retire replaced Module worktree {workspace}"
            )
    if _git_branch_exists(common_git_dir, branch):
        _git_dir(common_git_dir, "branch", "-D", branch)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    _add_branch_worktree(
        common_git_dir,
        worktree=workspace,
        branch=branch,
        start_sha=target_base,
    )


def _skeleton_module_revision_signature(
    *,
    artifact: Mapping[str, Any],
    contract_ref: Mapping[str, Any],
    contract: Mapping[str, Any],
    node: AggregateSnapshot,
    dependencies: list[str],
    contracts_by_module: Mapping[str, tuple[Mapping[str, Any], Mapping[str, Any]]],
) -> str:
    dependency_interfaces: dict[str, Any] = {}
    for dependency in sorted(dependencies):
        dependency_contract = contracts_by_module.get(dependency)
        if dependency_contract is None:
            return ""
        dependency_interfaces[dependency] = {
            "contract_hash": str(dependency_contract[0].get("sha256") or ""),
            "contract_file_hashes": dict(dependency_contract[1].get("contract_file_hashes") or {}),
        }
    try:
        return module_revision_fingerprint(
            unit_contract_hash=str(contract_ref.get("sha256") or ""),
            relevant_requirements_hash=_stable_json_hash(dict(artifact.get("requirements_ref") or {})),
            relevant_evidence_hash=_stable_json_hash({}),
            global_constraint_hash=str(node.payload.get("global_constraint_hash") or ""),
            owned_area_hash=_stable_json_hash(dict(contract.get("paths") or {})),
            dependency_set_hash=_stable_json_hash(sorted(dependencies)),
            dependency_interface_hash=_stable_json_hash(dependency_interfaces),
            # Skeleton modules consume declared interfaces. The concrete
            # implementation chosen for an upstream module is deliberately
            # outside the consumer Module revision identity.
            dependency_output_hash=_stable_json_hash({}),
            integration_contract_subset_hash=_stable_json_hash({}),
            environment_policy_hash=str(node.payload.get("environment_fingerprint") or ""),
        )
    except ValueError:
        return ""



def _module_revision_signature(
    *,
    manifest: Mapping[str, Any],
    fragments: Mapping[str, Any],
    contract_ref: Mapping[str, Any],
    contract: Mapping[str, Any],
    unit_id: str,
    dependencies: Mapping[str, list[str]],
    node: AggregateSnapshot,
    node_by_module: Mapping[str, AggregateSnapshot],
) -> str:
    task_ledger_hash = str(dict(manifest.get("requirements_ref") or {}).get("sha256") or "")
    dependency_modules = sorted(dependencies.get(unit_id) or [])
    dependency_interfaces = {
        dependency: dict(_contract_value(node_by_module.get(dependency), fragments)).get("provided_interfaces") or []
        for dependency in dependency_modules
    }
    dependency_outputs = {
        dependency: {
            "candidate_digest": str((node_by_module.get(dependency) or node).payload.get("candidate_digest") or ""),
            "output_hashes": dict((node_by_module.get(dependency) or node).payload.get("output_hashes") or {}),
        }
        for dependency in dependency_modules
    }
    cross_contracts = [
        item
        for item in list(fragments.get("cross_unit_contract") or [])
        if _mapping_mentions(item, {unit_id, *dependency_modules})
    ]
    return module_revision_fingerprint(
        unit_contract_hash=str(contract_ref.get("sha256") or ""),
        relevant_requirements_hash=task_ledger_hash,
        relevant_evidence_hash=_stable_json_hash([]),
        global_constraint_hash=str(dict(manifest.get("global_constraints_ref") or {}).get("sha256") or ""),
        owned_area_hash=_stable_json_hash(list(contract.get("owned_area") or [])),
        dependency_set_hash=_stable_json_hash(dependency_modules),
        dependency_interface_hash=_stable_json_hash(
            {
                "provided": dependency_interfaces,
                "consumed": list(contract.get("consumed_interfaces") or []),
                "cross_contracts": cross_contracts,
            }
        ),
        dependency_output_hash=_stable_json_hash(dependency_outputs),
        integration_contract_subset_hash=str(
            dict(manifest.get("integration_contract_ref") or {}).get("sha256") or ""
        ),
        environment_policy_hash=str(node.payload.get("environment_fingerprint") or ""),
    )


def _topological_module_order(dependencies: Mapping[str, list[str]]) -> list[str]:
    pending = {unit_id: set(values) for unit_id, values in dependencies.items()}
    result: list[str] = []
    while pending:
        ready = sorted(unit_id for unit_id, values in pending.items() if not values)
        if not ready:
            raise ValueError("module identity reconciliation requires an acyclic topology")
        for unit_id in ready:
            result.append(unit_id)
            pending.pop(unit_id)
        for values in pending.values():
            values.difference_update(ready)
    return result


def _module_role_session_generations(
    repository: MinionV2Repository,
    *,
    workflow_id: str,
    source_epoch_id: str,
    target_subjects: set[str],
    replaced_subjects: set[str] | None = None,
) -> dict[str, int]:
    """Preserve a Module identity, or allocate a fresh generation after removal."""

    source_epoch = str(source_epoch_id or "").strip()
    source_generations: dict[str, int] = {}
    historical_generations: dict[str, int] = {}
    for snapshot in repository.list_workflow_snapshots(workflow_id):
        if snapshot.aggregate_type != AggregateType.DAG_NODE_RUN:
            continue
        subject = str(
            snapshot.payload.get("module_name")
            or snapshot.payload.get("unit_id")
            or ""
        ).strip()
        if not subject:
            continue
        generation = max(
            0, int(snapshot.payload.get("role_session_generation") or 0)
        )
        historical_generations[subject] = max(
            historical_generations.get(subject, 0),
            generation,
        )
        if source_epoch and str(snapshot.payload.get("epoch_id") or "") == source_epoch:
            source_generations[subject] = max(
                source_generations.get(subject, 0),
                generation,
            )
    replaced = set(replaced_subjects or set())
    return {
        subject: (
            source_generations[subject] + 1
            if subject in source_generations and subject in replaced
            else source_generations[subject]
            if subject in source_generations
            else historical_generations[subject] + 1
            if subject in historical_generations
            else 0
        )
        for subject in target_subjects
    }


def _module_identity_delta(
    repository: MinionV2Repository,
    *,
    workflow_id: str,
    source_epoch_id: str,
    target_module_responsibilities: Mapping[str, str],
) -> dict[str, list[str]]:
    source_epoch = str(source_epoch_id or "").strip()
    source_modules = {
        str(snapshot.payload.get("module_name") or snapshot.payload.get("unit_id") or ""): str(
            snapshot.payload.get("module_responsibility") or ""
        )
        for snapshot in repository.list_workflow_snapshots(workflow_id)
        if source_epoch
        and snapshot.aggregate_type == AggregateType.DAG_NODE_RUN
        and str(snapshot.payload.get("epoch_id") or "") == source_epoch
        and str(snapshot.payload.get("node_kind") or "") == "unit"
    }
    source_modules.pop("", None)
    target_modules = {
        str(name): str(responsibility)
        for name, responsibility in target_module_responsibilities.items()
    }
    common = set(source_modules) & set(target_modules)
    replaced = {
        name
        for name in common
        if source_modules[name]
        and _normalized_responsibility(source_modules[name])
        != _normalized_responsibility(target_modules[name])
    }
    return {
        "preserved": sorted(common - replaced),
        "replaced": sorted(replaced),
        "added": sorted(set(target_modules) - set(source_modules)),
        "deleted": sorted(set(source_modules) - set(target_modules)),
    }


def _normalized_responsibility(value: str) -> str:
    return " ".join(str(value).casefold().split())


def _contract_value(
    node: AggregateSnapshot | None,
    fragments: Mapping[str, Any],
) -> Mapping[str, Any]:
    if node is None:
        return {}
    unit_id = str(node.payload.get("unit_id") or "")
    return next(
        (
            item
            for item in list(fragments.get("unit_contract") or [])
            if str(dict(item).get("unit_id") or "") == unit_id
        ),
        {},
    )


def _mapping_mentions(value: Any, identifiers: set[str]) -> bool:
    if isinstance(value, Mapping):
        return any(_mapping_mentions(item, identifiers) for item in value.values())
    if isinstance(value, list):
        return any(_mapping_mentions(item, identifiers) for item in value)
    return str(value) in identifiers


def _carry_forward_candidate(
    source_node: AggregateSnapshot,
    target_node: AggregateSnapshot,
    candidate_digest: str,
) -> None:
    source_git = Path(str(source_node.payload.get("common_git_dir") or ""))
    target_git = Path(str(target_node.payload.get("common_git_dir") or ""))
    target_worktree = Path(str(target_node.payload.get("workspace_path") or ""))
    if not source_git.is_dir() or not target_git.is_dir() or not target_worktree.is_dir():
        raise ValueError("Candidate carry-forward worktree metadata is incomplete")
    ref_name = f"refs/pal-minion-v2/carry-forward/{_safe_ref(target_node.aggregate_id)}"
    completed = subprocess.run(
        ["git", f"--git-dir={target_git}", "fetch", "--no-tags", str(source_git), f"{candidate_digest}:{ref_name}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or "failed to import reusable candidate")
    _git(target_worktree, "reset", "--hard", candidate_digest)


def _stable_json_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass
class DagScheduler:
    repository: MinionV2Repository

    def schedule_ready_nodes(
        self,
        *,
        workflow_id: str,
        epoch_id: str,
        max_new_nodes: int,
        actor: str = "minion-scheduler",
    ) -> tuple[str, ...]:
        snapshots = self.repository.list_workflow_snapshots(workflow_id)
        epoch = next(
            (
                item
                for item in snapshots
                if item.aggregate_type == AggregateType.EXECUTION_EPOCH and item.aggregate_id == epoch_id
            ),
            None,
        )
        if epoch is None or epoch.state != "RUNNING":
            return ()
        node_by_id = {
            item.aggregate_id: item
            for item in snapshots
            if item.aggregate_type == AggregateType.DAG_NODE_RUN
            and str(item.payload.get("epoch_id") or "") == epoch_id
        }
        accepted_ids = {node_id for node_id, item in node_by_id.items() if item.state == "ACCEPTED"}
        active_slots = sum(
            item.state in {
                "PRODUCING",
                "REVIEWING",
                "REVIEW_QUIESCING",
                "REVIEW_SNAPSHOTTING",
                "REPAIRING",
                "QUIESCING",
                "SNAPSHOTTING",
                "VERIFY_PREPARING",
                "VERIFYING",
                "VERIFY_QUIESCING",
                "VERIFY_SNAPSHOTTING",
            }
            for item in node_by_id.values()
        )
        available_slots = max(0, int(max_new_nodes) - active_slots)
        ready: list[AggregateSnapshot] = []
        for node_id in sorted(node_by_id):
            node = node_by_id[node_id]
            if node.state not in {"BLOCKED_BY_DEPS", "STALE"}:
                continue
            dependencies = {str(item) for item in list(node.payload.get("dependency_node_ids") or [])}
            if dependencies <= accepted_ids:
                ready.append(node)
        scheduled: list[str] = []
        for node in ready[:available_slots]:
            node_kind = str(node.payload.get("node_kind") or "unit")
            baseline = prepare_node_dependency_baseline(
                node,
                node_by_id,
                apply_candidates=node_kind != "system_verification",
            )
            payload = {
                "accepted_dependency_node_ids": sorted(
                    set(node.payload.get("dependency_node_ids") or []) & accepted_ids
                ),
                "epoch_frozen": False,
                **baseline,
            }
            verification = node_kind == "system_verification"
            integration = node_kind == "integration"
            if node.state == "BLOCKED_BY_DEPS":
                if verification:
                    action_type = "VERIFICATION_DEPENDENCIES_ACCEPTED"
                elif integration:
                    action_type = "INTEGRATION_DEPENDENCIES_ACCEPTED"
                else:
                    action_type = "DEPENDENCIES_ACCEPTED"
            else:
                if verification:
                    action_type = "REQUEUE_VERIFICATION_STALE"
                elif integration:
                    action_type = "REQUEUE_INTEGRATION_STALE"
                else:
                    action_type = "REQUEUE_STALE"
            if node.state == "STALE":
                payload.update(
                    {
                        "unit_contract_ref": node.payload.get("unit_contract_ref"),
                        "dependency_fingerprint": dependency_fingerprint(node, node_by_id),
                    }
                )
            self.repository.dispatch(
                _action(
                    action_type,
                    workflow_id,
                    AggregateType.DAG_NODE_RUN,
                    node.aggregate_id,
                    actor,
                    node.version,
                    payload,
                )
            )
            scheduled.append(node.aggregate_id)
        return tuple(scheduled)


@dataclass
class UnitWorkViewBuilder:
    contracts: ContractArtifactAccess

    def build(self, node: AggregateSnapshot) -> ArtifactRef:
        manifest_ref = dict(node.payload.get("architecture_manifest_ref") or {})
        record = self.contracts.repository.read_artifact_record(str(manifest_ref.get("sha256") or ""))
        if record and str(record.get("artifact_type") or "") == CONTRACT_ARTIFACT:
            adapter = str(node.payload.get("execution_adapter") or "")
            if adapter == SOFTWARE_GIT_ADAPTER:
                artifact = dict(
                    self.contracts.artifacts.read_json(manifest_ref)
                )
                contract = dict(artifact.get("contract") or {})
                return self._build_skeleton_view(
                    node,
                    artifact_override={
                        **artifact,
                        "submission": software_contract_projection(contract),
                    },
                )
            if adapter == ARTIFACT_BUNDLE_ADAPTER:
                return self._build_data_contract_view(node)
            raise ValueError(
                "unit work view has no supported bound execution adapter"
            )
        raise ValueError(
            "unit work view requires a ContractArtifact"
        )
    def _build_skeleton_view(
        self,
        node: AggregateSnapshot,
        *,
        artifact_override: Mapping[str, Any] | None = None,
    ) -> ArtifactRef:
        manifest_ref = dict(node.payload.get("architecture_manifest_ref") or {})
        artifact = dict(
            artifact_override
            or self.contracts.artifacts.read_json(manifest_ref)
        )
        contract_ref = dict(node.payload.get("unit_contract_ref") or {})
        contract = dict(self.contracts.artifacts.read_json(contract_ref))
        submission = dict(artifact.get("submission") or {})
        all_modules = {
            str(name): dict(value or {})
            for name, value in dict(submission.get("modules") or {}).items()
        }
        if str(node.payload.get("node_kind") or "") == "system_verification":
            raise ValueError(
                "SystemVerificationWorkView is prepared only after all module Candidates are accepted"
            )
        path_policy = dict(node.payload.get("path_policy") or contract.get("paths") or {})
        module_name = str(contract.get("module_name") or node.payload.get("module_name") or "")
        semantic_module = dict(contract.get("module") or {})
        dependency_edges = {
            str(name): dict(value or {})
            for name, value in dict(semantic_module.get("dependencies") or {}).items()
        }
        dependency_names = set(dependency_edges)
        dependency_contract_slices: dict[str, Any] = {}
        for dependency_id in list(node.payload.get("contract_dependency_node_ids") or []):
            dependency = self.contracts.repository.read_snapshot(
                AggregateType.DAG_NODE_RUN, str(dependency_id)
            )
            if dependency is None:
                continue
            dependency_name = str(
                dependency.payload.get("module_name") or dependency.payload.get("unit_id") or ""
            )
            if dependency_name not in dependency_names:
                continue
            dependency_contract = dict(
                self.contracts.artifacts.read_json(
                    dict(dependency.payload.get("unit_contract_ref") or {})
                )
            )
            provider_module = dict(dependency_contract.get("module") or {})
            provider_contract = dict(provider_module.get("contract") or {})
            edge = dependency_edges[dependency_name]
            consumed = [
                str(item) for item in list(edge.get("consumes") or [])
            ]
            dependency_contract_slices[dependency_name] = {
                "edge": edge,
                "contract_paths": list(
                    dict(dependency_contract.get("paths") or {}).get("contract_paths") or []
                ),
                "consumed_outputs": {
                    name: dict(
                        dict(provider_contract.get("outputs") or {}).get(name) or {}
                    )
                    for name in consumed
                },
                "errors": list(provider_contract.get("errors") or []),
                "invariants": list(provider_contract.get("invariants") or []),
                "ownership": list(provider_module.get("ownership") or []),
                "lifecycle": dict(provider_module.get("lifecycle") or {}),
                "state_machine": provider_module.get("state_machine"),
            }
        historical_refs = [
            dict(item)
            for item in list(node.payload.get("historical_repair_bill_refs") or [])
            if isinstance(item, Mapping) and item.get("sha256")
        ]
        payload = {
            "schema_version": "3",
            "module_name": module_name,
            "module": semantic_module,
            "contract_mode": str(path_policy.get("contract_mode") or "review_guarded"),
            "contract_paths": list(path_policy.get("contract_paths") or []),
            "implementation_scopes": list(path_policy.get("implementation_scopes") or []),
            "developer_tests": dict(path_policy.get("developer_tests") or {}),
            "verification_corpus": dict(path_policy.get("verification_corpus") or {}),
            "reference_only": list(path_policy.get("reference_only") or []),
            "dependency_contracts": dependency_contract_slices,
            "consumer_obligations": {
                name: dict(dict(value.get("dependencies") or {}).get(module_name) or {})
                for name, value in all_modules.items()
                if module_name in dict(value.get("dependencies") or {})
            },
            "historical_repair_bills": [
                repair_bill_semantic_view(self.contracts.artifacts, item) for item in historical_refs
            ],
        }
        return self.contracts.artifacts.put_json(
            payload,
            artifact_type="ModuleWorkViewArtifact",
            child_refs=(
                (str(manifest_ref["sha256"]), "architecture_skeleton"),
                (str(contract_ref["sha256"]), "module_contract"),
                *((str(item["sha256"]), "historical_repair_bill") for item in historical_refs),
            ),
        )

    def _build_data_contract_view(
        self,
        node: AggregateSnapshot,
    ) -> ArtifactRef:
        manifest_ref = dict(node.payload.get("architecture_manifest_ref") or {})
        contract_ref = dict(node.payload.get("unit_contract_ref") or {})
        module_contract = dict(
            self.contracts.artifacts.read_json(contract_ref)
        )
        manifest = dict(
            self.contracts.artifacts.read_json(manifest_ref)
        )
        full_contract = dict(manifest.get("contract") or {})
        all_modules = {
            str(name): dict(value or {})
            for name, value in dict(full_contract.get("modules") or {}).items()
        }
        owned_module = dict(module_contract.get("module") or {})
        dependency_contracts = {
            provider: {
                "module": all_modules[provider],
                "handoff": dict(dependency or {}),
            }
            for provider, dependency in dict(
                owned_module.get("dependencies") or {}
            ).items()
            if provider in all_modules
        }
        if str(node.payload.get("node_kind") or "") == "integration":
            dependency_contracts = {
                name: {"module": module}
                for name, module in all_modules.items()
                if str(module.get("execution") or "") == "produce"
            }
        payload = {
            "schema_version": "3",
            "execution_adapter": ARTIFACT_BUNDLE_ADAPTER,
            "module_name": str(
                module_contract.get("module_name")
                or node.payload.get("module_name")
                or ""
            ),
            "module": owned_module,
            "context": dict(module_contract.get("context") or {}),
            "requirements": dict(
                module_contract.get("requirements") or {}
            ),
            "scenarios": dict(module_contract.get("scenarios") or {}),
            "dependency_contracts": dependency_contracts,
            "historical_repair_bills": [
                repair_bill_semantic_view(self.contracts.artifacts, item)
                for item in list(
                    node.payload.get("historical_repair_bill_refs") or []
                )
                if isinstance(item, Mapping) and item.get("sha256")
            ],
        }
        return self.contracts.artifacts.put_json(
            payload,
            artifact_type="ModuleWorkViewArtifact",
            child_refs=(
                (str(manifest_ref["sha256"]), "contract"),
                (str(contract_ref["sha256"]), "module_contract"),
            ),
        )


@dataclass
class CandidateSnapshotService:
    repository: MinionV2Repository
    artifacts: ContentAddressedArtifactStore
    worktree_locks: WorkspaceLockRegistry

    def create_candidate(
        self,
        *,
        node_run_id: str,
        worker_id: str,
        lease_resource_key: str,
        fencing_token: int,
        worktree: Path,
        expected_workspace_fingerprint: str,
        reference_only_paths: list[str],
        path_policy: Mapping[str, Any] | None = None,
        base_sha: str,
        candidate_baseline_sha: str,
        unit_contract_hash: str,
        dependency_output_hashes: Mapping[str, str],
        environment_fingerprint: str,
        repair_bill_ref: Mapping[str, Any] | None = None,
    ) -> tuple[ArtifactRef, str]:
        self.repository.assert_fencing_token(lease_resource_key, worker_id, fencing_token)
        if not self.worktree_locks.is_held(node_run_id):
            raise RuntimeError("candidate snapshot requires the quiescer's exclusive worktree lock")
        try:
            node = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node_run_id)
            if node is None:
                raise ValueError("candidate snapshot requires a durable DAG node run")
            expected_contract_hash = str(dict(node.payload.get("unit_contract_ref") or {}).get("sha256") or "")
            if not expected_contract_hash or unit_contract_hash != expected_contract_hash:
                raise ValueError("candidate unit contract hash does not match the DAG node contract")
            expected_environment = str(node.payload.get("environment_fingerprint") or "")
            if expected_environment and environment_fingerprint != expected_environment:
                raise ValueError("candidate environment fingerprint does not match the execution epoch")
            current_head = _git(worktree, "rev-parse", "HEAD").strip()
            before = workspace_content_fingerprint(worktree)
            if before != expected_workspace_fingerprint:
                raise RuntimeError("worktree changed after quiescing")
            if not candidate_baseline_sha:
                raise ValueError("candidate requires the accepted Architecture baseline")
            # A Module branch is a normal linear history.  Validate only the
            # current role's delta; prior Coder and Verifier commits are
            # already durable ancestors of ``base_sha``.
            changed_paths = git_changed_paths(worktree, base_sha)
            if path_policy:
                _validate_skeleton_candidate_paths(
                    changed_paths,
                    path_policy,
                )
            else:
                _validate_reference_only_paths(changed_paths, reference_only_paths)
            candidate_key = hashlib.sha256(
                json.dumps(
                    {
                        "node_run_id": node_run_id,
                        "assignment_base_sha": base_sha,
                        "previous_head_sha": base_sha,
                        "workspace_fingerprint": before,
                        "unit_contract_hash": unit_contract_hash,
                    },
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            existing_sha = _find_candidate_commit(worktree, candidate_key)
            if current_head not in {base_sha, existing_sha}:
                raise ValueError(
                    "coder changed Git HEAD; commits, merges, rebases, checkouts, and resets are manager-owned operations"
                )
            if existing_sha:
                candidate_digest = existing_sha
            elif not changed_paths:
                # A role may legitimately prove that the accepted baseline
                # already satisfies the Module Protocol.  Record the handoff
                # against the current HEAD without manufacturing an empty
                # content commit.
                candidate_digest = base_sha
            else:
                _git(worktree, "add", "-A")
                message = (
                    f"minion module checkpoint {node_run_id}\n\n"
                    f"Pal-Assignment-Key: {candidate_key}"
                )
                tree_sha = _git(worktree, "write-tree").strip()
                candidate_digest = _git(
                    worktree,
                    "-c",
                    "user.name=Pal Minion",
                    "-c",
                    "user.email=minion@localhost",
                    "commit-tree",
                    tree_sha,
                    "-p",
                    base_sha,
                    "-m",
                    message,
                ).strip()
                _git(
                    worktree,
                    "update-ref",
                    f"refs/pal/checkpoints/{candidate_key}",
                    candidate_digest,
                )
                _git(worktree, "reset", "--hard", candidate_digest)
            after = workspace_content_fingerprint(worktree)
            if before != after:
                raise RuntimeError("worktree content changed while candidate commit was created")
            baseline_tree_sha = _git(worktree, "rev-parse", f"{base_sha}^{{tree}}").strip()
            candidate_tree_sha = _git(worktree, "rev-parse", f"{candidate_digest}^{{tree}}").strip()
            delta_patch = _git_bytes(worktree, "diff", "--binary", base_sha, candidate_digest, "--")
            candidate = {
                "schema_version": "3",
                "node_run_id": node_run_id,
                "candidate_digest": candidate_digest,
                "base_sha": base_sha,
                "architecture_base_sha": candidate_baseline_sha,
                "previous_head_sha": base_sha,
                "baseline_tree_sha": baseline_tree_sha,
                "candidate_tree_sha": candidate_tree_sha,
                "delta_patch_sha": hashlib.sha256(delta_patch).hexdigest(),
                "repair_bill_ref": dict(repair_bill_ref or {}),
                "unit_contract_hash": unit_contract_hash,
                "dependency_output_hashes": dict(dependency_output_hashes),
                "environment_fingerprint": environment_fingerprint,
                "workspace_fingerprint": before,
                "changed_paths": changed_paths,
                "candidate_key": candidate_key,
            }
            child_refs = ()
            if repair_bill_ref and repair_bill_ref.get("sha256"):
                child_refs = ((str(repair_bill_ref["sha256"]), "repair_bill"),)
            ref = self.artifacts.put_json(
                candidate,
                artifact_type="GitCheckpointArtifact",
                child_refs=child_refs,
            )
            return ref, candidate_digest
        finally:
            self.worktree_locks.release(node_run_id)


def dependency_fingerprint(node: AggregateSnapshot, node_by_id: Mapping[str, AggregateSnapshot]) -> str:
    dependency_data = []
    for node_id in sorted(str(item) for item in list(node.payload.get("dependency_node_ids") or [])):
        dependency = node_by_id[node_id]
        dependency_data.append(
            {
                "node_id": node_id,
                "candidate_digest": str(dependency.payload.get("candidate_digest") or ""),
                "output_hashes": dict(dependency.payload.get("output_hashes") or {}),
            }
        )
    return hashlib.sha256(json.dumps(dependency_data, sort_keys=True).encode("utf-8")).hexdigest()


def provision_module_worktrees(
    runtime_root: Path,
    *,
    workflow_id: str,
    workflow_name: str,
    unit_ids: list[str],
    workspace: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
    layout = resolve_project_git_layout(
        runtime_root,
        workspace=workspace,
        workflow_id=workflow_id,
        workflow_name=workflow_name,
    )
    common_git_dir = layout.common_git_dir
    base_sha, base_tree_sha = _ensure_generic_project_repository(
        layout,
        workspace=workspace,
    )
    result: dict[str, dict[str, str]] = {}
    for unit_id in [*unit_ids, "integration"]:
        if unit_id == "integration":
            worktree = layout.integration_worktree
            branch = layout.integration_branch
        else:
            worktree = layout.module_worktree(unit_id)
            branch = layout.module_branch(unit_id)
        if not worktree.exists():
            worktree.parent.mkdir(parents=True, exist_ok=True)
            _add_branch_worktree(common_git_dir, worktree=worktree, branch=branch, start_sha=base_sha)
        result[unit_id] = {
            "workspace_path": str(worktree),
            "common_git_dir": str(common_git_dir),
            "worktree_branch": branch,
            "workflow_branch": layout.workflow_branch,
            "workflow_key": layout.workflow_key,
            "project_name": layout.project_name,
            "project_key": layout.project_key,
            "epoch_base_sha": base_sha,
            "epoch_base_tree_sha": base_tree_sha,
            "base_digest": base_sha,
            "base_sha": base_sha,
        }
    return result


def provision_skeleton_module_worktrees(
    runtime_root: Path,
    *,
    artifacts: ContentAddressedArtifactStore,
    workflow_id: str,
    workflow_name: str,
    unit_ids: list[str],
    verification_unit_ids: set[str] | None = None,
    workspace: Mapping[str, Any] | None = None,
    architecture_artifact: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
    layout = resolve_project_git_layout(
        runtime_root,
        workspace=dict(workspace or {}),
        workflow_id=workflow_id,
        workflow_name=workflow_name,
        stored_layout=dict(architecture_artifact.get("repository_layout") or {}),
    )
    common_git_dir = layout.common_git_dir
    skeleton_sha = str(architecture_artifact.get("skeleton_commit_sha") or "")
    skeleton_tree = str(architecture_artifact.get("skeleton_tree_sha") or "")
    bundle_ref = ArtifactRef.from_mapping(dict(architecture_artifact.get("git_bundle_ref") or {}))
    if not skeleton_sha or not skeleton_tree or not bundle_ref.sha256:
        raise ValueError("software ContractArtifact is missing its commit, tree, or Git bundle")
    with project_git_layout_lock(layout):
        if not common_git_dir.exists():
            _clone_bundle_repository(
                common_git_dir,
                bundle_bytes=artifacts.read_bytes(bundle_ref),
            )
        elif not _git_commit_exists(common_git_dir, skeleton_sha):
            _fetch_bundle_repository(
                common_git_dir,
                bundle_bytes=artifacts.read_bytes(bundle_ref),
                namespace=f"refs/minion/imports/{bundle_ref.sha256[:16]}",
            )
        restored_tree = _git_dir(
            common_git_dir,
            "rev-parse",
            f"{skeleton_sha}^{{tree}}",
        ).strip()
        if restored_tree != skeleton_tree:
            raise RuntimeError("restored skeleton Git bundle does not match the accepted tree")
        _force_branch(common_git_dir, layout.workflow_branch, skeleton_sha)
    verification_names = set(verification_unit_ids or set())
    result: dict[str, dict[str, str]] = {}
    for unit_id in unit_ids:
        verification = unit_id in verification_names
        if verification:
            worktree = layout.integration_worktree
            branch = layout.integration_branch
        else:
            worktree = layout.module_worktree(unit_id)
            branch = layout.module_branch(unit_id)
        if not worktree.exists():
            worktree.parent.mkdir(parents=True, exist_ok=True)
            _add_branch_worktree(
                common_git_dir,
                worktree=worktree,
                branch=branch,
                start_sha=skeleton_sha,
            )
        result[unit_id] = {
            "workspace_path": str(worktree),
            "common_git_dir": str(common_git_dir),
            "worktree_branch": branch,
            "workflow_branch": layout.workflow_branch,
            "workflow_key": layout.workflow_key,
            "project_name": layout.project_name,
            "project_key": layout.project_key,
            "epoch_base_sha": skeleton_sha,
            "epoch_base_tree_sha": skeleton_tree,
            "base_digest": skeleton_sha,
            "base_sha": skeleton_sha,
            "execution_adapter": SOFTWARE_GIT_ADAPTER,
        }
    return result


def provision_module_verification_workspace(
    runtime_root: Path,
    *,
    node: AggregateSnapshot,
    candidate_digest: str,
) -> tuple[Path, Path]:
    if not candidate_digest:
        raise ValueError("module verification requires candidate_digest")
    worktree = Path(str(node.payload.get("workspace_path") or ""))
    if not worktree.is_dir():
        raise ValueError("module verification requires its Module worktree")
    scratch = (
        verification_scratch_root(runtime_root)
        / "modules"
        / _safe_ref(str(node.payload.get("module_name") or node.payload.get("unit_id") or "module"))
        / _safe_ref(candidate_digest)
    )
    scratch.mkdir(parents=True, exist_ok=True)
    if _git(worktree, "rev-parse", "HEAD").strip() != candidate_digest:
        raise RuntimeError("Module worktree is not bound to the Candidate SHA")
    return worktree, scratch


def prepare_node_dependency_baseline(
    node: AggregateSnapshot,
    node_by_id: Mapping[str, AggregateSnapshot],
    *,
    apply_candidates: bool = True,
) -> dict[str, Any]:
    workspace = Path(str(node.payload.get("workspace_path") or ""))
    if not workspace.is_dir():
        raise ValueError(f"node workspace does not exist: {workspace}")
    adapter = str(node.payload.get("execution_adapter") or "").strip()
    if adapter not in {SOFTWARE_GIT_ADAPTER, ARTIFACT_BUNDLE_ADAPTER}:
        raise ValueError(
            "node dependency baseline has no supported bound execution adapter"
        )
    if (
        adapter == SOFTWARE_GIT_ADAPTER
        and apply_candidates
        and node.state == "BLOCKED_BY_DEPS"
        and not bool(node.payload.get("module_replan_prepared"))
    ):
        declared_base = str(node.payload.get("base_sha") or "")
        if not declared_base:
            raise ValueError("blocked node has no declared Git baseline")
        _abort_cherry_pick(workspace)
        _git(workspace, "reset", "--hard", declared_base)
        _git(workspace, "clean", "-fd")
    starting_head = (
        _git(workspace, "rev-parse", "HEAD").strip()
        if adapter == SOFTWARE_GIT_ADAPTER and apply_candidates
        else ""
    )
    accepted_digests: list[str] = []
    output_hashes: dict[str, str] = {}
    dependency_outputs: dict[str, Any] = {}
    try:
        for dependency in _ordered_dependency_closure(node, node_by_id):
            dependency_id = dependency.aggregate_id
            if dependency.state != "ACCEPTED":
                raise ValueError(f"dependency is not accepted: {dependency_id}")
            candidate_digest = str(dependency.payload.get("candidate_digest") or "")
            if not candidate_digest:
                raise ValueError(f"accepted dependency has no candidate digest: {dependency_id}")
            if adapter == SOFTWARE_GIT_ADAPTER:
                if apply_candidates:
                    _apply_dependency_candidate_delta(workspace, dependency)
            elif adapter != ARTIFACT_BUNDLE_ADAPTER:
                raise ValueError(f"unsupported execution adapter: {adapter}")
            accepted_digests.append(candidate_digest)
            dependency_outputs[dependency_id] = {
                "candidate_ref": dict(dependency.payload.get("candidate_ref") or {}),
                "candidate_digest": candidate_digest,
                "output_hashes": dict(dependency.payload.get("output_hashes") or {}),
            }
            output_hashes[dependency_id] = hashlib.sha256(
                json.dumps(dict(dependency.payload.get("output_hashes") or {}), sort_keys=True).encode("utf-8")
            ).hexdigest()
    except BaseException:
        if starting_head:
            _abort_cherry_pick(workspace)
            _git(workspace, "reset", "--hard", starting_head)
            _git(workspace, "clean", "-fd")
        raise
    base_digest = (
        _git(workspace, "rev-parse", "HEAD").strip()
        if adapter == SOFTWARE_GIT_ADAPTER
        else artifact_tree_fingerprint(workspace)
    )
    return {
        "base_digest": base_digest,
        "base_sha": base_digest if adapter == SOFTWARE_GIT_ADAPTER else "",
        "accepted_dependency_candidate_digests": accepted_digests,
        "dependency_output_hashes": output_hashes,
        "dependency_outputs": dependency_outputs,
        "dependency_fingerprint": dependency_fingerprint(node, node_by_id),
    }


def _apply_dependency_candidate_delta(
    workspace: Path,
    dependency: AggregateSnapshot,
) -> None:
    candidate_digest = str(dependency.payload.get("candidate_digest") or "")
    candidate_base = str(dependency.payload.get("base_sha") or "")
    if not candidate_base:
        raise ValueError(
            f"accepted dependency has no Candidate baseline: {dependency.aggregate_id}"
        )
    if not _git_is_ancestor(workspace, candidate_base, candidate_digest):
        raise ValueError(
            f"accepted dependency Candidate is not based on its declared baseline: {dependency.aggregate_id}"
        )
    commits = [
        line.strip()
        for line in _git(
            workspace,
            "rev-list",
            "--reverse",
            "--topo-order",
            candidate_digest,
            f"^{candidate_base}",
        ).splitlines()
        if line.strip()
    ]
    if not commits:
        raise ValueError(
            f"accepted dependency Candidate contains no delta: {dependency.aggregate_id}"
        )
    for commit in commits:
        _git(workspace, "cherry-pick", commit)


def _abort_cherry_pick(workspace: Path) -> None:
    subprocess.run(
        ["git", "cherry-pick", "--abort"],
        cwd=workspace,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _ordered_dependency_closure(
    node: AggregateSnapshot,
    node_by_id: Mapping[str, AggregateSnapshot],
) -> tuple[AggregateSnapshot, ...]:
    ordered: list[AggregateSnapshot] = []
    permanent: set[str] = set()
    visiting: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in permanent:
            return
        if node_id in visiting:
            raise ValueError("construction dependency graph contains a cycle")
        dependency = node_by_id.get(node_id)
        if dependency is None:
            raise ValueError(f"dependency node does not exist in epoch: {node_id}")
        visiting.add(node_id)
        for parent_id in sorted(
            str(item) for item in list(dependency.payload.get("dependency_node_ids") or [])
        ):
            visit(parent_id)
        visiting.remove(node_id)
        permanent.add(node_id)
        ordered.append(dependency)

    for dependency_id in sorted(
        str(item) for item in list(node.payload.get("dependency_node_ids") or [])
    ):
        visit(dependency_id)
    return tuple(ordered)


def workspace_content_fingerprint(worktree: Path) -> str:
    paths = _git_bytes(worktree, "ls-files", "-co", "--exclude-standard", "-z").split(b"\0")
    digest = hashlib.sha256()
    for raw_path in sorted(item for item in paths if item):
        relative = raw_path.decode("utf-8", errors="surrogateescape")
        path = worktree / relative
        if not path.is_file() or path.is_symlink():
            continue
        digest.update(raw_path)
        digest.update(b"\0")
        digest.update(str(path.stat().st_mode & 0o777).encode("ascii"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def workspace_process_holders(worktree: Path) -> tuple[WorkspaceProcessHolder, ...]:
    """Describe processes whose cwd or open files still touch a worktree."""
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return ()
    resolved_path = worktree.resolve()
    resolved = str(resolved_path)
    prefix = resolved + os.sep
    holders: list[WorkspaceProcessHolder] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        holds_cwd = _proc_link_touches_workspace(entry / "cwd", resolved, prefix)
        read_paths: list[str] = []
        write_paths: list[str] = []
        unknown_paths: list[str] = []
        fd_dir = entry / "fd"
        try:
            fd_links = tuple(fd_dir.iterdir())
        except OSError:
            fd_links = ()
        for link in fd_links:
            target = _proc_link_workspace_target(link, resolved, prefix)
            if target is None:
                continue
            relative = _workspace_relative_process_path(target, resolved_path)
            access = _proc_fd_access(entry, link.name)
            if access == "write":
                write_paths.append(relative)
            elif access == "read":
                read_paths.append(relative)
            else:
                unknown_paths.append(relative)
        if not holds_cwd and not read_paths and not write_paths and not unknown_paths:
            continue
        pid = int(entry.name)
        try:
            process_group = os.getpgid(pid)
        except (OSError, ProcessLookupError):
            process_group = 0
        try:
            command = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            command = ""
        holders.append(
            WorkspaceProcessHolder(
                pid=pid,
                process_group=process_group,
                command=command,
                holds_cwd=holds_cwd,
                read_paths=tuple(sorted(set(read_paths))),
                write_paths=tuple(sorted(set(write_paths))),
                unknown_paths=tuple(sorted(set(unknown_paths))),
            )
        )
    return tuple(sorted(holders, key=lambda item: item.pid))


def format_workspace_process_holders(
    holders: tuple[WorkspaceProcessHolder, ...],
    *,
    max_holders: int = 12,
    max_paths_per_access: int = 8,
) -> str:
    records: list[dict[str, Any]] = []
    for holder in holders[:max_holders]:
        record = holder.to_dict()
        for field_name in ("read_paths", "write_paths", "unknown_paths"):
            paths = list(record[field_name])
            record[field_name] = paths[:max_paths_per_access]
            if len(paths) > max_paths_per_access:
                record[f"{field_name}_omitted"] = len(paths) - max_paths_per_access
        records.append(record)
    payload: dict[str, Any] = {"holders": records}
    if len(holders) > max_holders:
        payload["holders_omitted"] = len(holders) - max_holders
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def workspace_has_live_processes(worktree: Path) -> bool:
    """Detect processes whose cwd or open files still touch a worktree."""
    return bool(workspace_process_holders(worktree))


def _proc_link_touches_workspace(link: Path, resolved: str, prefix: str) -> bool:
    return _proc_link_workspace_target(link, resolved, prefix) is not None


def _proc_link_workspace_target(link: Path, resolved: str, prefix: str) -> str | None:
    try:
        target = os.readlink(link)
    except OSError:
        return None
    normalized = target.removesuffix(" (deleted)")
    if normalized == resolved or normalized.startswith(prefix):
        return normalized
    return None


def _workspace_relative_process_path(target: str, worktree: Path) -> str:
    try:
        relative = Path(target).relative_to(worktree)
    except ValueError:
        return target
    return str(relative) if str(relative) else "."


def _proc_fd_access(process_entry: Path, fd_name: str) -> str:
    try:
        lines = (process_entry / "fdinfo" / fd_name).read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
    except OSError:
        return "unknown"
    flags_text = next(
        (line.partition(":")[2].strip() for line in lines if line.startswith("flags:")),
        "",
    )
    try:
        flags = int(flags_text, 8)
    except ValueError:
        return "unknown"
    mode = flags & os.O_ACCMODE
    if mode in {os.O_WRONLY, os.O_RDWR}:
        return "write"
    if mode == os.O_RDONLY:
        return "read"
    return "unknown"


async def terminate_process_group(process_group: int, *, timeout_seconds: float = 5.0) -> bool:
    """Stop a persisted worker process group, including children after its leader exits."""
    if process_group <= 0:
        return True
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return True
    if await _wait_for_process_group_exit(process_group, timeout_seconds):
        return True
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return True
    return await _wait_for_process_group_exit(process_group, timeout_seconds)


async def _wait_for_process_group_exit(process_group: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    while time.monotonic() < deadline:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.05)
    return False


def git_changed_paths(worktree: Path, base_sha: str) -> list[str]:
    # Report both sides of a rename so moving a frozen/reference-only path into
    # an owned scope cannot bypass the path policy.
    output = _git_bytes(worktree, "diff", "--name-only", "--no-renames", "-z", base_sha, "--")
    tracked = [item.decode("utf-8", errors="surrogateescape") for item in output.split(b"\0") if item]
    untracked_output = _git_bytes(worktree, "ls-files", "--others", "--exclude-standard", "-z")
    untracked = [item.decode("utf-8", errors="surrogateescape") for item in untracked_output.split(b"\0") if item]
    return sorted(set(tracked + untracked))


@contextmanager
def exclusive_workspace_lock(worktree: Path) -> Iterator[None]:
    import fcntl

    git_dir_text = _git(worktree, "rev-parse", "--git-dir").strip()
    git_dir = (worktree / git_dir_text).resolve()
    lock_path = git_dir / "pal-minion-v2.snapshot.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _validate_reference_only_paths(changed_paths: list[str], reference_only_paths: list[str]) -> None:
    reference_violations = [path for path in changed_paths if _matches_any(path, reference_only_paths)]
    if reference_violations:
        raise ValueError(f"candidate modified reference-only paths: {reference_violations}")


def _validate_skeleton_candidate_paths(
    changed_paths: list[str],
    policy: Mapping[str, Any],
) -> None:
    contract_mode = str(policy.get("contract_mode") or "file_frozen")
    if contract_mode not in {"file_frozen", "review_guarded"}:
        raise ValueError(f"unknown contract enforcement mode: {contract_mode}")
    frozen = {str(item).replace(os.sep, "/") for item in list(policy.get("contract_paths") or [])}
    references = {str(item).replace(os.sep, "/") for item in list(policy.get("reference_only") or [])}
    writable = list(compiled_module_write_scopes(policy))
    frozen_violations = sorted(
        path
        for path in changed_paths
        if contract_mode == "file_frozen" and path.replace(os.sep, "/") in frozen
    )
    if frozen_violations:
        raise ValueError("candidate modified frozen architecture contracts: " + ", ".join(frozen_violations))
    reference_violations = sorted(path for path in changed_paths if path.replace(os.sep, "/") in references)
    if reference_violations:
        raise ValueError("candidate modified reference-only paths: " + ", ".join(reference_violations))
    outside = sorted(
        path
        for path in changed_paths
        if not any(_path_scope_matches(path, scope) for scope in writable)
    )
    if outside:
        raise ValueError("candidate changed paths outside its compiled module write scopes: " + ", ".join(outside))

def _path_scope_matches(path: str, scope: Mapping[str, Any]) -> bool:
    normalized = str(path).replace(os.sep, "/").strip("/")
    target = str(scope.get("path") or "").replace(os.sep, "/").strip("/")
    kind = str(scope.get("kind") or "")
    if not target:
        return False
    if kind == "file":
        return normalized == target
    if kind == "directory":
        return normalized == target or normalized.startswith(target + "/")
    return False


def _matches_any(path: str, patterns: list[str]) -> bool:
    normalized = path.replace(os.sep, "/")
    for pattern in patterns:
        candidate = str(pattern).replace(os.sep, "/")
        if candidate.endswith("/**") and normalized.startswith(candidate[:-3].rstrip("/") + "/"):
            return True
        if fnmatch.fnmatchcase(normalized, candidate):
            return True
    return False


def _topology_sinks(depends_on: Mapping[str, list[str]], node_ids: Mapping[str, str]) -> list[str]:
    dependencies = {item for values in depends_on.values() for item in values}
    sinks = sorted(set(depends_on) - dependencies)
    return [node_ids[item] for item in sinks]


def _find_candidate_commit(worktree: Path, candidate_key: str) -> str:
    try:
        # Checkpoint idempotency is branch-local. A matching commit reachable
        # only from another Module branch does not settle this assignment.
        output = _git(
            worktree,
            "log",
            "HEAD",
            "--format=%H%x00%B%x00",
            "--grep",
            f"Pal-Assignment-Key: {candidate_key}",
            "-n",
            "1",
        )
    except subprocess.CalledProcessError:
        return ""
    parts = output.split("\x00")
    return parts[0].strip() if parts and candidate_key in output else ""


def _action(
    action_type: str,
    workflow_id: str,
    aggregate_type: AggregateType,
    aggregate_id: str,
    actor: str,
    expected_version: int,
    payload: Mapping[str, Any],
) -> ActionEnvelope:
    return ActionEnvelope(
        action_type=action_type,
        workflow_id=workflow_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        actor=actor,
        expected_version=expected_version,
        idempotency_key=f"{aggregate_id}:{action_type}:{expected_version}",
        payload=dict(payload),
    )


def _git_commit_exists(git_dir: Path, commit_sha: str) -> bool:
    if not git_dir.is_dir() or not commit_sha:
        return False
    return subprocess.run(
        ["git", f"--git-dir={git_dir}", "cat-file", "-e", f"{commit_sha}^{{commit}}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def _ensure_generic_project_repository(
    layout: ProjectGitLayout,
    *,
    workspace: Mapping[str, Any],
) -> tuple[str, str]:
    common_git_dir = layout.common_git_dir
    with project_git_layout_lock(layout):
        if not common_git_dir.exists():
            source = str(workspace.get("repo_path") or workspace.get("cwd") or "").strip()
            if source:
                completed = subprocess.run(
                    [
                        "git",
                        "clone",
                        "--bare",
                        "--no-hardlinks",
                        str(Path(source).expanduser()),
                        str(common_git_dir),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
            else:
                with tempfile.TemporaryDirectory(
                    prefix="pal-generic-project-",
                    dir=layout.project_root,
                ) as temporary:
                    seed = Path(temporary) / "seed"
                    seed.mkdir()
                    _git(seed, "init", "-q", "-b", "main")
                    _git(
                        seed,
                        "-c",
                        "user.name=Pal Minion",
                        "-c",
                        "user.email=minion@localhost",
                        "commit",
                        "--allow-empty",
                        "-qm",
                        "V2 epoch base",
                    )
                    completed = subprocess.run(
                        ["git", "clone", "--bare", str(seed), str(common_git_dir)],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=False,
                    )
            if completed.returncode != 0:
                raise RuntimeError(
                    completed.stderr
                    or completed.stdout
                    or "failed to initialize project repository"
                )
        base_sha = _git_dir(common_git_dir, "rev-parse", "HEAD").strip()
        base_tree_sha = _git_dir(common_git_dir, "rev-parse", "HEAD^{tree}").strip()
        _force_branch(common_git_dir, layout.workflow_branch, base_sha)
    return base_sha, base_tree_sha


def _git_branch_exists(git_dir: Path, branch: str) -> bool:
    return subprocess.run(
        ["git", f"--git-dir={git_dir}", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def _force_branch(git_dir: Path, branch: str, commit_sha: str) -> None:
    subprocess.run(
        ["git", "check-ref-format", "--branch", branch],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    _git_dir(git_dir, "update-ref", f"refs/heads/{branch}", commit_sha)


def _add_branch_worktree(
    git_dir: Path,
    *,
    worktree: Path,
    branch: str,
    start_sha: str,
) -> None:
    if _git_branch_exists(git_dir, branch):
        command = ["git", f"--git-dir={git_dir}", "worktree", "add", str(worktree), branch]
    else:
        command = [
            "git",
            f"--git-dir={git_dir}",
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree),
            start_sha,
        ]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr
            or completed.stdout
            or f"failed to create stable workflow worktree {worktree}"
        )


def _clone_bundle_repository(common_git_dir: Path, *, bundle_bytes: bytes) -> None:
    common_git_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pal-skeleton-clone-") as temporary:
        bundle = Path(temporary) / "architecture.bundle"
        bundle.write_bytes(bundle_bytes)
        completed = subprocess.run(
            ["git", "clone", "--bare", str(bundle), str(common_git_dir)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or "failed to restore project repository")


def _fetch_bundle_repository(
    common_git_dir: Path,
    *,
    bundle_bytes: bytes,
    namespace: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix="pal-skeleton-fetch-") as temporary:
        bundle = Path(temporary) / "architecture.bundle"
        bundle.write_bytes(bundle_bytes)
        completed = subprocess.run(
            [
                "git",
                f"--git-dir={common_git_dir}",
                "fetch",
                str(bundle),
                f"+refs/heads/*:{namespace}/*",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or "failed to import skeleton bundle")


def _git(worktree: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=worktree,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout


def _git_dir(git_dir: Path, *args: str) -> str:
    return subprocess.run(
        ["git", f"--git-dir={git_dir}", *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout


def _git_bytes(worktree: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=worktree,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def _git_is_ancestor(repository: Path, ancestor: str, descendant: str) -> bool:
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repository,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def _safe_ref(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in str(value))[:80] or "node"
