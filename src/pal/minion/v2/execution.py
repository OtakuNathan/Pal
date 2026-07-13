from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import os
import signal
import subprocess
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, IO, Iterator, Mapping, Protocol

from pal.minion.v2.architecture import ArchitectureArtifactService, validate_architecture_manifest
from pal.minion.v2.adapters import (
    ARTIFACT_BUNDLE_ADAPTER,
    SOFTWARE_GIT_ADAPTER,
    artifact_tree_fingerprint,
    provision_artifact_workspaces,
)
from pal.minion.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.minion.v2.contracts import ActionEnvelope, AggregateSnapshot, AggregateType, DispatchResult
from pal.minion.v2.repository import MinionV2Repository
from pal.minion.v2.skeleton import (
    ARCHITECTURE_SKELETON_ARTIFACT,
    SKELETON_MODULE_CONTRACT_ARTIFACT,
    requirements_semantic_view,
)
from pal.minion.v2.verification import candidate_reuse_fingerprint, repair_bill_semantic_view


@dataclass(frozen=True)
class ExecutionCompilation:
    epoch_id: str
    node_run_ids: tuple[str, ...]
    unit_node_ids: Mapping[str, str]
    verification_node_ids: Mapping[str, str] = field(default_factory=dict)
    integration_node_id: str = ""


@dataclass(frozen=True)
class NodeRunJournal:
    current_micro_plan: tuple[str, ...] = ()
    completed_checklist: tuple[str, ...] = ()
    files_inspected: tuple[str, ...] = ()
    files_changed: tuple[str, ...] = ()
    tests_run: tuple[Mapping[str, Any], ...] = ()
    open_questions: tuple[str, ...] = ()
    known_failures: tuple[str, ...] = ()
    last_safe_point: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_micro_plan": list(self.current_micro_plan),
            "completed_checklist": list(self.completed_checklist),
            "files_inspected": list(self.files_inspected),
            "files_changed": list(self.files_changed),
            "tests_run": [dict(item) for item in self.tests_run],
            "open_questions": list(self.open_questions),
            "known_failures": list(self.known_failures),
            "last_safe_point": self.last_safe_point,
        }


@dataclass(frozen=True)
class QuiesceResult:
    fencing_token: int
    process_group_reaped: bool
    exclusive_workspace_lock: bool
    workspace_fingerprint: str
    lock_path: Path


@dataclass
class WorkspaceLockRegistry:
    _streams: dict[str, IO[str]] = field(default_factory=dict, init=False)

    def acquire(self, node_run_id: str, workspace: Path) -> Path:
        import fcntl

        if node_run_id in self._streams:
            raise RuntimeError(f"workspace lock is already held for {node_run_id}")
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
            lock_path = workspace.parent / ".pal-candidate-locks" / f"{_safe_ref(node_run_id)}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        stream = lock_path.open("a+")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            stream.close()
            raise
        self._streams[node_run_id] = stream
        return lock_path

    def release(self, node_run_id: str) -> None:
        import fcntl

        stream = self._streams.pop(node_run_id, None)
        if stream is None:
            return
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def is_held(self, node_run_id: str) -> bool:
        return node_run_id in self._streams


class WorkerProcessController(Protocol):
    def revoke_tool_token(self, worker_id: str, fencing_token: int) -> None:
        ...

    def request_cooperative_stop(self, worker_id: str) -> None:
        ...

    def kill_and_reap_process_group(self, worker_id: str, timeout_seconds: float) -> bool:
        ...

    def has_live_processes_for_workspace(self, worktree: Path) -> bool:
        ...


@dataclass
class PosixWorkerProcessController:
    process_group_by_worker: dict[str, int] = field(default_factory=dict)
    revoked_tokens: set[tuple[str, int]] = field(default_factory=set)

    def revoke_tool_token(self, worker_id: str, fencing_token: int) -> None:
        self.revoked_tokens.add((worker_id, fencing_token))

    def request_cooperative_stop(self, worker_id: str) -> None:
        process_group = self.process_group_by_worker.get(worker_id)
        if process_group is not None:
            try:
                os.killpg(process_group, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def kill_and_reap_process_group(self, worker_id: str, timeout_seconds: float) -> bool:
        process_group = self.process_group_by_worker.get(worker_id)
        if process_group is None:
            return True
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        while time.monotonic() < deadline:
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                self.process_group_by_worker.pop(worker_id, None)
                return True
            time.sleep(0.05)
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            self.process_group_by_worker.pop(worker_id, None)
            return True
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        while time.monotonic() < deadline:
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                self.process_group_by_worker.pop(worker_id, None)
                return True
            time.sleep(0.05)
        return False

    def has_live_processes_for_workspace(self, worktree: Path) -> bool:
        return workspace_has_live_processes(worktree)


@dataclass
class ExecutionCompiler:
    repository: MinionV2Repository
    architecture: ArchitectureArtifactService

    def compile_epoch(
        self,
        *,
        workflow_id: str,
        epoch_id: str,
        manifest_ref: ArtifactRef,
        actor: str = "minion-manager",
        reuse_from_epoch_id: str = "",
        initial_repair_bill_ref: Mapping[str, Any] | None = None,
    ) -> ExecutionCompilation:
        record = self.repository.read_artifact_record(manifest_ref.sha256)
        if record and str(record.get("artifact_type") or "") == ARCHITECTURE_SKELETON_ARTIFACT:
            return self._compile_skeleton_epoch(
                workflow_id=workflow_id,
                epoch_id=epoch_id,
                manifest_ref=manifest_ref,
                actor=actor,
                reuse_from_epoch_id=reuse_from_epoch_id,
                initial_repair_bill_ref=initial_repair_bill_ref,
            )
        manifest = validate_architecture_manifest(self.architecture.artifacts.read_json(manifest_ref))
        fragments = self.architecture.load_manifest_fragments(manifest)
        topology = dict(fragments.get("topology") or {})
        depends_on = {
            str(unit_id): [str(item) for item in list(dependencies or [])]
            for unit_id, dependencies in dict(topology.get("depends_on") or {}).items()
        }
        unit_refs_by_id: dict[str, dict[str, Any]] = {}
        for ref, contract in zip(manifest["unit_contract_refs"], fragments.get("unit_contract") or [], strict=True):
            unit_id = str(dict(contract).get("unit_id") or "")
            if not unit_id or unit_id in unit_refs_by_id:
                raise ValueError(f"invalid or duplicate unit id: {unit_id or '<empty>'}")
            unit_refs_by_id[unit_id] = dict(ref)
        if set(depends_on) != set(unit_refs_by_id):
            raise ValueError("topology nodes do not match unit contracts")

        topology_ref = dict(manifest["topology_ref"])
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
                    "topology_ref": topology_ref,
                    "architecture_manifest_sha": manifest_ref.sha256,
                },
            )
        )
        self.repository.dispatch(
            _action("START_EXECUTION", workflow_id, AggregateType.EXECUTION_EPOCH, epoch_id, actor, 1, {})
        )

        unit_node_ids = {unit_id: f"{epoch_id}:node:{unit_id}" for unit_id in unit_refs_by_id}
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        request = (
            self.architecture.artifacts.read_json(dict(workflow.payload.get("request_ref") or {}))
            if workflow is not None and workflow.payload.get("request_ref")
            else {"workspace": {"kind": "new_project", "project_name": workflow_id}}
        )
        binding_ref = dict(workflow.payload.get("family_binding_ref") or {}) if workflow is not None else {}
        binding = self.architecture.artifacts.read_json(binding_ref) if binding_ref else {}
        adapters = dict(binding.get("adapters") or {})
        execution_adapter = str(adapters.get("workspace") or SOFTWARE_GIT_ADAPTER)
        if execution_adapter == SOFTWARE_GIT_ADAPTER:
            workspaces = provision_epoch_worktrees(
                self.repository.runtime_root,
                epoch_id=epoch_id,
                unit_ids=sorted(unit_refs_by_id),
                workspace=dict(request.get("workspace") or {}),
            )
            for value in workspaces.values():
                value["execution_adapter"] = SOFTWARE_GIT_ADAPTER
        elif execution_adapter == ARTIFACT_BUNDLE_ADAPTER:
            workspaces = provision_artifact_workspaces(
                self.repository.runtime_root,
                epoch_id=epoch_id,
                unit_ids=sorted(unit_refs_by_id),
            )
        else:
            raise ValueError(f"unsupported execution workspace adapter: {execution_adapter}")
        environment_fingerprint = _stable_json_hash(
            {
                "epoch_base_tree_sha": str(workspaces["integration"]["epoch_base_tree_sha"]),
                "execution_adapter": execution_adapter,
                "workspace_environment_policy": dict(
                    dict(request.get("workspace") or {}).get("workspace_environment_policy") or {}
                ),
                "toolchain": dict(request.get("toolchain") or {}),
            }
        )
        for unit_id in sorted(unit_refs_by_id):
            dependency_node_ids = [unit_node_ids[item] for item in depends_on[unit_id]]
            self.repository.dispatch(
                _action(
                    "CREATE_NODE_RUN",
                    workflow_id,
                    AggregateType.DAG_NODE_RUN,
                    unit_node_ids[unit_id],
                    actor,
                    0,
                    {
                        "epoch_id": epoch_id,
                        "unit_id": unit_id,
                        "node_kind": "unit",
                        "unit_contract_ref": unit_refs_by_id[unit_id],
                        "architecture_manifest_ref": manifest_ref.to_dict(),
                        "dependency_node_ids": dependency_node_ids,
                        "accepted_dependency_node_ids": [],
                        "epoch_frozen": False,
                        "environment_fingerprint": environment_fingerprint,
                        **dict(workspaces[unit_id]),
                    },
                )
            )

        integration_node_id = f"{epoch_id}:node:integration"
        integration_dependencies = [unit_node_ids[unit_id] for unit_id in _topological_module_order(depends_on)]
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
                    "node_kind": "integration",
                    "unit_contract_ref": dict(manifest["integration_contract_ref"]),
                    "architecture_manifest_ref": manifest_ref.to_dict(),
                    "dependency_node_ids": integration_dependencies,
                    "accepted_dependency_node_ids": [],
                    "epoch_frozen": False,
                    "environment_fingerprint": environment_fingerprint,
                    **dict(workspaces["integration"]),
                },
            )
        )
        if reuse_from_epoch_id:
            reuse_accepted_candidates(
                repository=self.repository,
                architecture=self.architecture,
                workflow_id=workflow_id,
                source_epoch_id=reuse_from_epoch_id,
                target_epoch_id=epoch_id,
                target_manifest_ref=manifest_ref,
                actor=actor,
            )
        node_ids = tuple([*(unit_node_ids[unit_id] for unit_id in sorted(unit_node_ids)), integration_node_id])
        self.repository.dispatch(
            _action(
                "NODES_COMPILED",
                workflow_id,
                AggregateType.EXECUTION_EPOCH,
                epoch_id,
                actor,
                2,
                {"node_ids": list(node_ids), "integration_node_id": integration_node_id},
            )
        )
        return ExecutionCompilation(
            epoch_id=epoch_id,
            node_run_ids=node_ids,
            unit_node_ids=unit_node_ids,
            verification_node_ids={},
            integration_node_id=integration_node_id,
        )

    def _compile_skeleton_epoch(
        self,
        *,
        workflow_id: str,
        epoch_id: str,
        manifest_ref: ArtifactRef,
        actor: str,
        reuse_from_epoch_id: str,
        initial_repair_bill_ref: Mapping[str, Any] | None,
    ) -> ExecutionCompilation:
        artifact = dict(self.architecture.artifacts.read_json(manifest_ref))
        submission = dict(artifact.get("submission") or {})
        modules = {str(name): dict(value or {}) for name, value in dict(submission.get("modules") or {}).items()}
        if not modules:
            raise ValueError("ArchitectureSkeletonArtifact has no modules")
        if initial_repair_bill_ref and len(modules) != 1:
            raise ValueError("an initial RepairBill requires a bounded single-module skeleton")
        depends_on = {
            name: [str(item) for item in list(module.get("depends_on") or [])]
            for name, module in modules.items()
        }
        _topological_module_order(depends_on)
        verification_nodes = {
            str(name): dict(value or {})
            for name, value in dict(submission.get("verification_nodes") or {}).items()
        }
        if not verification_nodes:
            raise ValueError("ArchitectureSkeletonArtifact has no Verification Nodes")
        topology_ref = self.architecture.artifacts.put_json(
            {
                "construction": {"depends_on": depends_on},
                "contract_consumption": {
                    name: list(module.get("consumes") or []) for name, module in modules.items()
                },
                "verification": {
                    name: {
                        "depends_on": list(node.get("depends_on") or []),
                        "consumes": list(node.get("consumes") or []),
                    }
                    for name, node in verification_nodes.items()
                },
            },
            artifact_type="SkeletonTopologyArtifact",
            child_refs=((manifest_ref.sha256, "architecture_skeleton"),),
        )
        module_refs: dict[str, ArtifactRef] = {}
        contract_file_hashes = dict(artifact.get("contract_file_hashes") or {})
        for name, module in modules.items():
            paths = dict(module.get("paths") or {})
            module_refs[name] = self.architecture.artifacts.put_json(
                {
                    "module_name": name,
                    "depends_on": depends_on[name],
                    "consumes": list(module.get("consumes") or []),
                    "paths": paths,
                    "covers": list(module.get("covers") or []),
                    "evidence": list(module.get("evidence") or []),
                    "contract_file_hashes": {
                        path: str(contract_file_hashes.get(path) or "")
                        for path in list(paths.get("contract_paths") or [])
                    },
                },
                artifact_type=SKELETON_MODULE_CONTRACT_ARTIFACT,
                child_refs=((manifest_ref.sha256, "architecture_skeleton"),),
            )
        verification_refs = {
            name: self.architecture.artifacts.put_json(
                {"verification_name": name, **node},
                artifact_type="VerificationScenarioContractArtifact",
                child_refs=((manifest_ref.sha256, "architecture_skeleton"),),
            )
            for name, node in verification_nodes.items()
        }
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
        all_workspace_names = [*sorted(modules), *sorted(verification_nodes)]
        workspaces = provision_skeleton_epoch_worktrees(
            self.repository.runtime_root,
            artifacts=self.architecture.artifacts,
            epoch_id=epoch_id,
            unit_ids=all_workspace_names,
            architecture_artifact=artifact,
        )
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        request = (
            self.architecture.artifacts.read_json(dict(workflow.payload.get("request_ref") or {}))
            if workflow is not None and workflow.payload.get("request_ref")
            else {}
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
                "requirements": self.architecture.artifacts.read_json(dict(artifact.get("requirements_ref") or {})),
            }
        )
        unit_node_ids = {name: f"{epoch_id}:node:{name}" for name in modules}
        for name in sorted(modules):
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
                        "node_kind": "unit",
                        "unit_contract_ref": module_refs[name].to_dict(),
                        "architecture_manifest_ref": manifest_ref.to_dict(),
                        "dependency_node_ids": [unit_node_ids[item] for item in depends_on[name]],
                        "accepted_dependency_node_ids": [],
                        "epoch_frozen": False,
                        "environment_fingerprint": environment_fingerprint,
                        "global_constraint_hash": global_constraint_hash,
                        "path_policy": {
                            "contract_paths": list(paths.get("contract_paths") or []),
                            "implementation_scopes": list(paths.get("implementation_scopes") or []),
                            "test_scopes": list(paths.get("test_scopes") or []),
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
        verification_node_ids = {
            name: f"{epoch_id}:verification:{name}" for name in verification_nodes
        }
        for name in sorted(verification_nodes):
            scenario = verification_nodes[name]
            self.repository.dispatch(
                _action(
                    "CREATE_NODE_RUN",
                    workflow_id,
                    AggregateType.DAG_NODE_RUN,
                    verification_node_ids[name],
                    actor,
                    0,
                    {
                        "epoch_id": epoch_id,
                        "unit_id": name,
                        "module_name": name,
                        "node_kind": "verification",
                        "unit_contract_ref": verification_refs[name].to_dict(),
                        "architecture_manifest_ref": manifest_ref.to_dict(),
                        "dependency_node_ids": [unit_node_ids[item] for item in list(scenario.get("depends_on") or [])],
                        "accepted_dependency_node_ids": [],
                        "epoch_frozen": False,
                        "environment_fingerprint": environment_fingerprint,
                        "verification_environment": dict(scenario.get("environment") or {}),
                        "verification_entrypoints": list(scenario.get("entrypoints") or []),
                        "path_policy": {
                            "contract_paths": [],
                            "implementation_scopes": [],
                            "test_scopes": [],
                            "reference_only": [],
                        },
                        **dict(workspaces[name]),
                    },
                )
            )
        if reuse_from_epoch_id:
            reuse_accepted_candidates(
                repository=self.repository,
                architecture=self.architecture,
                workflow_id=workflow_id,
                source_epoch_id=reuse_from_epoch_id,
                target_epoch_id=epoch_id,
                target_manifest_ref=manifest_ref,
                actor=actor,
            )
        node_ids = tuple(
            [
                *(unit_node_ids[name] for name in sorted(unit_node_ids)),
                *(verification_node_ids[name] for name in sorted(verification_node_ids)),
            ]
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
                    "verification_node_ids": list(verification_node_ids.values()),
                },
            )
        )
        return ExecutionCompilation(
            epoch_id=epoch_id,
            node_run_ids=node_ids,
            unit_node_ids=unit_node_ids,
            verification_node_ids=verification_node_ids,
        )


def reuse_accepted_candidates(
    *,
    repository: MinionV2Repository,
    architecture: ArchitectureArtifactService,
    workflow_id: str,
    source_epoch_id: str,
    target_epoch_id: str,
    target_manifest_ref: ArtifactRef,
    actor: str,
) -> tuple[str, ...]:
    source_epoch = repository.read_snapshot(AggregateType.EXECUTION_EPOCH, source_epoch_id)
    if source_epoch is None:
        return ()
    source_manifest_ref = ArtifactRef.from_mapping(
        dict(source_epoch.payload.get("architecture_manifest_ref") or {})
    )
    source_record = repository.read_artifact_record(source_manifest_ref.sha256)
    target_record = repository.read_artifact_record(target_manifest_ref.sha256)
    source_is_skeleton = bool(
        source_record and str(source_record.get("artifact_type") or "") == ARCHITECTURE_SKELETON_ARTIFACT
    )
    target_is_skeleton = bool(
        target_record and str(target_record.get("artifact_type") or "") == ARCHITECTURE_SKELETON_ARTIFACT
    )
    if source_is_skeleton or target_is_skeleton:
        if not (source_is_skeleton and target_is_skeleton):
            return ()
        return _reuse_accepted_skeleton_candidates(
            repository=repository,
            architecture=architecture,
            workflow_id=workflow_id,
            source_epoch_id=source_epoch_id,
            target_epoch_id=target_epoch_id,
            source_manifest_ref=source_manifest_ref,
            target_manifest_ref=target_manifest_ref,
            actor=actor,
        )
    source_manifest = validate_architecture_manifest(architecture.artifacts.read_json(source_manifest_ref))
    target_manifest = validate_architecture_manifest(architecture.artifacts.read_json(target_manifest_ref))
    source_fragments = architecture.load_manifest_fragments(source_manifest)
    target_fragments = architecture.load_manifest_fragments(target_manifest)
    snapshots = repository.list_workflow_snapshots(workflow_id)
    source_nodes = {
        str(item.payload.get("unit_id") or ""): item
        for item in snapshots
        if item.aggregate_type == AggregateType.DAG_NODE_RUN
        and str(item.payload.get("epoch_id") or "") == source_epoch_id
        and str(item.payload.get("node_kind") or "") == "unit"
    }
    target_nodes = {
        str(item.payload.get("unit_id") or ""): item
        for item in snapshots
        if item.aggregate_type == AggregateType.DAG_NODE_RUN
        and str(item.payload.get("epoch_id") or "") == target_epoch_id
        and str(item.payload.get("node_kind") or "") == "unit"
    }
    source_contracts = _contracts_by_id(source_manifest, source_fragments)
    target_contracts = _contracts_by_id(target_manifest, target_fragments)
    source_dependencies = _module_dependencies(source_fragments)
    target_dependencies = _module_dependencies(target_fragments)
    reused: list[str] = []
    for unit_id in _topological_module_order(target_dependencies):
        source_node = source_nodes.get(unit_id)
        target_node = repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            target_nodes[unit_id].aggregate_id,
        )
        if (
            source_node is None
            or source_node.state != "ACCEPTED"
            or target_node is None
            or target_node.state != "BLOCKED_BY_DEPS"
        ):
            continue
        dependency_ids = [str(item) for item in list(target_node.payload.get("dependency_node_ids") or [])]
        accepted_dependencies = [
            dependency_id
            for dependency_id in dependency_ids
            if (repository.read_snapshot(AggregateType.DAG_NODE_RUN, dependency_id) or target_node).state == "ACCEPTED"
        ]
        if len(accepted_dependencies) != len(dependency_ids):
            continue
        source_fingerprint = _candidate_reuse_signature(
            manifest=source_manifest,
            fragments=source_fragments,
            contract_ref=source_contracts[unit_id][0],
            contract=source_contracts[unit_id][1],
            unit_id=unit_id,
            dependencies=source_dependencies,
            node=source_node,
            node_by_module=source_nodes,
        )
        target_fingerprint = _candidate_reuse_signature(
            manifest=target_manifest,
            fragments=target_fragments,
            contract_ref=target_contracts[unit_id][0],
            contract=target_contracts[unit_id][1],
            unit_id=unit_id,
            dependencies=target_dependencies,
            node=target_node,
            node_by_module={
                key: repository.read_snapshot(AggregateType.DAG_NODE_RUN, value.aggregate_id) or value
                for key, value in target_nodes.items()
            },
        )
        if source_fingerprint != target_fingerprint:
            continue
        candidate_ref = dict(source_node.payload.get("candidate_ref") or {})
        verification_ref = dict(source_node.payload.get("verification_artifact_ref") or {})
        candidate_digest = str(source_node.payload.get("candidate_digest") or "")
        if not candidate_ref or not verification_ref or not candidate_digest:
            continue
        source_adapter = str(source_node.payload.get("execution_adapter") or SOFTWARE_GIT_ADAPTER)
        target_adapter = str(target_node.payload.get("execution_adapter") or SOFTWARE_GIT_ADAPTER)
        if source_adapter != target_adapter:
            continue
        if target_adapter == SOFTWARE_GIT_ADAPTER:
            _import_reused_candidate(source_node, target_node, candidate_digest)
        elif target_adapter != ARTIFACT_BUNDLE_ADAPTER:
            continue
        result = repository.dispatch(
            _action(
                "REUSE_ACCEPTED_CANDIDATE",
                workflow_id,
                AggregateType.DAG_NODE_RUN,
                target_node.aggregate_id,
                actor,
                target_node.version,
                {
                    "candidate_ref": candidate_ref,
                    "candidate_digest": candidate_digest,
                    "verification_artifact_ref": verification_ref,
                    "reuse_fingerprint": target_fingerprint,
                    "accepted_dependency_node_ids": accepted_dependencies,
                    "epoch_frozen": False,
                    "output_hashes": dict(source_node.payload.get("output_hashes") or {}),
                    "dependency_output_hashes": dict(source_node.payload.get("dependency_output_hashes") or {}),
                    "reused_from_epoch_id": source_epoch_id,
                    "reused_from_node_run_id": source_node.aggregate_id,
                },
            )
        )
        target_nodes[unit_id] = result.snapshot
        reused.append(result.snapshot.aggregate_id)
    return tuple(reused)


def _reuse_accepted_skeleton_candidates(
    *,
    repository: MinionV2Repository,
    architecture: ArchitectureArtifactService,
    workflow_id: str,
    source_epoch_id: str,
    target_epoch_id: str,
    source_manifest_ref: ArtifactRef,
    target_manifest_ref: ArtifactRef,
    actor: str,
) -> tuple[str, ...]:
    source_artifact = dict(architecture.artifacts.read_json(source_manifest_ref))
    target_artifact = dict(architecture.artifacts.read_json(target_manifest_ref))
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
            dict(architecture.artifacts.read_json(dict(node.payload.get("unit_contract_ref") or {}))),
        )
        for name, node in source_nodes.items()
    }
    target_contracts = {
        name: (
            dict(node.payload.get("unit_contract_ref") or {}),
            dict(architecture.artifacts.read_json(dict(node.payload.get("unit_contract_ref") or {}))),
        )
        for name, node in target_nodes.items()
    }
    dependencies = {
        name: [str(item) for item in list(contract.get("depends_on") or [])]
        for name, (_ref, contract) in target_contracts.items()
    }
    if set(dependencies) != set(target_nodes):
        return ()
    reused: list[str] = []
    for module_name in _topological_module_order(dependencies):
        source_node = source_nodes.get(module_name)
        target_node = repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            target_nodes[module_name].aggregate_id,
        )
        if source_node is None or source_node.state != "ACCEPTED" or target_node is None:
            continue
        source_contract = source_contracts.get(module_name)
        target_contract = target_contracts.get(module_name)
        if source_contract is None or target_contract is None:
            continue
        target_node_by_module = {
            name: repository.read_snapshot(AggregateType.DAG_NODE_RUN, node.aggregate_id) or node
            for name, node in target_nodes.items()
        }
        dependency_modules = dependencies[module_name]
        if any(target_node_by_module[name].state != "ACCEPTED" for name in dependency_modules):
            continue
        source_fingerprint = _skeleton_candidate_reuse_signature(
            artifact=source_artifact,
            contract_ref=source_contract[0],
            contract=source_contract[1],
            node=source_node,
            dependencies=dependency_modules,
            node_by_module=source_nodes,
            contracts_by_module=source_contracts,
        )
        target_fingerprint = _skeleton_candidate_reuse_signature(
            artifact=target_artifact,
            contract_ref=target_contract[0],
            contract=target_contract[1],
            node=target_node,
            dependencies=dependency_modules,
            node_by_module=target_node_by_module,
            contracts_by_module=target_contracts,
        )
        if not source_fingerprint or source_fingerprint != target_fingerprint:
            continue
        source_candidate_ref = dict(source_node.payload.get("candidate_ref") or {})
        verification_ref = dict(source_node.payload.get("verification_artifact_ref") or {})
        source_candidate_digest = str(source_node.payload.get("candidate_digest") or "")
        if not source_candidate_ref or not verification_ref or not source_candidate_digest:
            continue
        target_nodes_by_id = {
            item.aggregate_id: item for item in target_node_by_module.values()
        }
        baseline = prepare_node_dependency_baseline(target_node, target_nodes_by_id)
        candidate_ref, candidate_digest, target_base = _transplant_skeleton_candidate(
            architecture=architecture,
            source_node=source_node,
            target_node=target_node,
            source_candidate_ref=source_candidate_ref,
            source_candidate_digest=source_candidate_digest,
            target_contract_ref=target_contract[0],
            target_fingerprint=target_fingerprint,
            baseline=baseline,
        )
        accepted_dependencies = [
            str(target_node_by_module[name].aggregate_id) for name in dependency_modules
        ]
        result = repository.dispatch(
            _action(
                "REUSE_ACCEPTED_CANDIDATE",
                workflow_id,
                AggregateType.DAG_NODE_RUN,
                target_node.aggregate_id,
                actor,
                target_node.version,
                {
                    "candidate_ref": candidate_ref.to_dict(),
                    "candidate_digest": candidate_digest,
                    "verification_artifact_ref": verification_ref,
                    "reuse_fingerprint": target_fingerprint,
                    "accepted_dependency_node_ids": accepted_dependencies,
                    "epoch_frozen": False,
                    "output_hashes": dict(source_node.payload.get("output_hashes") or {}),
                    "reused_from_epoch_id": source_epoch_id,
                    **baseline,
                    "base_sha": target_base,
                    "base_digest": target_base,
                },
            )
        )
        target_nodes[module_name] = result.snapshot
        reused.append(result.snapshot.aggregate_id)
    return tuple(reused)


def _skeleton_candidate_reuse_signature(
    *,
    artifact: Mapping[str, Any],
    contract_ref: Mapping[str, Any],
    contract: Mapping[str, Any],
    node: AggregateSnapshot,
    dependencies: list[str],
    node_by_module: Mapping[str, AggregateSnapshot],
    contracts_by_module: Mapping[str, tuple[Mapping[str, Any], Mapping[str, Any]]],
) -> str:
    dependency_outputs: dict[str, Any] = {}
    dependency_interfaces: dict[str, Any] = {}
    for dependency in sorted(dependencies):
        dependency_node = node_by_module.get(dependency)
        dependency_contract = contracts_by_module.get(dependency)
        if dependency_node is None or dependency_contract is None:
            return ""
        output_hashes = dict(dependency_node.payload.get("output_hashes") or {})
        if not output_hashes:
            return ""
        dependency_outputs[dependency] = output_hashes
        dependency_interfaces[dependency] = {
            "contract_hash": str(dependency_contract[0].get("sha256") or ""),
            "contract_file_hashes": dict(dependency_contract[1].get("contract_file_hashes") or {}),
        }
    module_name = str(contract.get("module_name") or node.payload.get("module_name") or "")
    verification_subset = {
        name: scenario
        for name, scenario in dict(dict(artifact.get("submission") or {}).get("verification_nodes") or {}).items()
        if module_name in set(str(item) for item in list(dict(scenario).get("depends_on") or []))
    }
    try:
        return candidate_reuse_fingerprint(
            unit_contract_hash=str(contract_ref.get("sha256") or ""),
            relevant_requirements_hash=_stable_json_hash(list(contract.get("covers") or [])),
            relevant_evidence_hash=_stable_json_hash(list(contract.get("evidence") or [])),
            global_constraint_hash=str(node.payload.get("global_constraint_hash") or ""),
            owned_area_hash=_stable_json_hash(dict(contract.get("paths") or {})),
            dependency_set_hash=_stable_json_hash(sorted(dependencies)),
            dependency_interface_hash=_stable_json_hash(dependency_interfaces),
            dependency_output_hash=_stable_json_hash(dependency_outputs),
            integration_contract_subset_hash=_stable_json_hash(verification_subset),
            environment_policy_hash=str(node.payload.get("environment_fingerprint") or ""),
        )
    except ValueError:
        return ""


def _transplant_skeleton_candidate(
    *,
    architecture: ArchitectureArtifactService,
    source_node: AggregateSnapshot,
    target_node: AggregateSnapshot,
    source_candidate_ref: Mapping[str, Any],
    source_candidate_digest: str,
    target_contract_ref: Mapping[str, Any],
    target_fingerprint: str,
    baseline: Mapping[str, Any],
) -> tuple[ArtifactRef, str, str]:
    source_candidate = dict(architecture.artifacts.read_json(source_candidate_ref))
    source_base = str(source_candidate.get("base_sha") or "")
    changed_paths = [str(item) for item in list(source_candidate.get("changed_paths") or [])]
    source_git = Path(str(source_node.payload.get("common_git_dir") or ""))
    target_worktree = Path(str(target_node.payload.get("workspace_path") or ""))
    if not source_base or not changed_paths or not source_git.is_dir() or not target_worktree.is_dir():
        raise ValueError("reusable skeleton candidate has incomplete Git provenance")
    path_policy = dict(target_node.payload.get("path_policy") or {})
    _validate_skeleton_candidate_paths(changed_paths, path_policy)
    candidate_key = _stable_json_hash(
        {
            "source_candidate_ref": str(source_candidate_ref.get("sha256") or ""),
            "target_contract_ref": str(target_contract_ref.get("sha256") or ""),
            "target_fingerprint": target_fingerprint,
        }
    )
    existing = _find_candidate_commit(target_worktree, candidate_key)
    if existing:
        candidate_digest = existing
        target_base = _git(target_worktree, "rev-parse", f"{existing}^").strip()
    else:
        if _git(target_worktree, "status", "--porcelain").strip():
            raise RuntimeError("target worktree is dirty before candidate reuse")
        completed = subprocess.run(
            [
                "git",
                f"--git-dir={source_git}",
                "diff",
                "--binary",
                source_base,
                source_candidate_digest,
                "--",
                *changed_paths,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout:
            raise RuntimeError(completed.stderr.decode("utf-8", errors="replace") or "reusable candidate diff is empty")
        checked = subprocess.run(
            ["git", "-C", str(target_worktree), "apply", "--check", "--binary", "-"],
            input=completed.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if checked.returncode != 0:
            raise RuntimeError(checked.stderr.decode("utf-8", errors="replace") or "reusable candidate does not apply")
        applied = subprocess.run(
            ["git", "-C", str(target_worktree), "apply", "--index", "--binary", "-"],
            input=completed.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if applied.returncode != 0:
            raise RuntimeError(applied.stderr.decode("utf-8", errors="replace") or "failed to apply reusable candidate")
        target_base = str(baseline.get("base_sha") or "")
        actual_paths = git_changed_paths(target_worktree, target_base)
        _validate_skeleton_candidate_paths(actual_paths, path_policy)
        _git(
            target_worktree,
            "-c",
            "user.name=Pal Minion",
            "-c",
            "user.email=minion@localhost",
            "commit",
            "-m",
            f"minion reused candidate {target_node.aggregate_id}\n\nPal-Candidate-Key: {candidate_key}",
        )
        candidate_digest = _git(target_worktree, "rev-parse", "HEAD").strip()
        changed_paths = actual_paths
    candidate = {
        "schema_version": "1",
        "candidate_digest": candidate_digest,
        "base_sha": target_base,
        "parent_candidate_sha": "",
        "module_contract_hash": str(target_contract_ref.get("sha256") or ""),
        "dependency_output_hashes": dict(baseline.get("dependency_output_hashes") or {}),
        "environment_fingerprint": str(target_node.payload.get("environment_fingerprint") or ""),
        "workspace_fingerprint": workspace_content_fingerprint(target_worktree),
        "changed_paths": changed_paths,
        "candidate_key": candidate_key,
        "reused_from_candidate": str(source_candidate_ref.get("sha256") or ""),
    }
    ref = architecture.artifacts.put_json(
        candidate,
        artifact_type="CandidateSnapshotArtifact",
        child_refs=(
            (str(source_candidate_ref["sha256"]), "reuses"),
            (str(target_contract_ref["sha256"]), "module_contract"),
        ),
    )
    return ref, candidate_digest, target_base


def _candidate_reuse_signature(
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
    requirement_ids = {str(item) for item in list(contract.get("requirement_ids") or [])}
    requirements = [
        item
        for item in list(dict(fragments.get("requirements") or {}).get("requirements") or [])
        if str(item.get("requirement_id") or "") in requirement_ids
    ]
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
    return candidate_reuse_fingerprint(
        unit_contract_hash=str(contract_ref.get("sha256") or ""),
        relevant_requirements_hash=_stable_json_hash(requirements),
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


def _contracts_by_id(
    manifest: Mapping[str, Any],
    fragments: Mapping[str, Any],
) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    return {
        str(dict(contract).get("unit_id") or ""): (dict(ref), dict(contract))
        for ref, contract in zip(
            list(manifest.get("unit_contract_refs") or []),
            list(fragments.get("unit_contract") or []),
            strict=True,
        )
    }


def _module_dependencies(fragments: Mapping[str, Any]) -> dict[str, list[str]]:
    return {
        str(unit_id): [str(item) for item in list(values or [])]
        for unit_id, values in dict(dict(fragments.get("topology") or {}).get("depends_on") or {}).items()
    }


def _topological_module_order(dependencies: Mapping[str, list[str]]) -> list[str]:
    pending = {unit_id: set(values) for unit_id, values in dependencies.items()}
    result: list[str] = []
    while pending:
        ready = sorted(unit_id for unit_id, values in pending.items() if not values)
        if not ready:
            raise ValueError("candidate reuse requires an acyclic topology")
        for unit_id in ready:
            result.append(unit_id)
            pending.pop(unit_id)
        for values in pending.values():
            values.difference_update(ready)
    return result


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


def _import_reused_candidate(
    source_node: AggregateSnapshot,
    target_node: AggregateSnapshot,
    candidate_digest: str,
) -> None:
    source_git = Path(str(source_node.payload.get("common_git_dir") or ""))
    target_git = Path(str(target_node.payload.get("common_git_dir") or ""))
    target_worktree = Path(str(target_node.payload.get("workspace_path") or ""))
    if not source_git.is_dir() or not target_git.is_dir() or not target_worktree.is_dir():
        raise ValueError("candidate reuse worktree metadata is incomplete")
    ref_name = f"refs/pal-minion-v2/reuse/{_safe_ref(target_node.aggregate_id)}"
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
                "REPAIRING",
                "QUIESCING",
                "SNAPSHOTTING",
                "VERIFY_PREPARING",
                "VERIFYING",
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
                apply_candidates=node_kind != "verification",
            )
            payload = {
                "accepted_dependency_node_ids": sorted(
                    set(node.payload.get("dependency_node_ids") or []) & accepted_ids
                ),
                "epoch_frozen": False,
                **baseline,
            }
            verification = node_kind == "verification"
            legacy_integration = node_kind == "integration"
            if node.state == "BLOCKED_BY_DEPS":
                if verification:
                    action_type = "VERIFICATION_DEPENDENCIES_ACCEPTED"
                elif legacy_integration:
                    action_type = "LEGACY_INTEGRATION_DEPENDENCIES_ACCEPTED"
                else:
                    action_type = "DEPENDENCIES_ACCEPTED"
            else:
                if verification:
                    action_type = "REQUEUE_VERIFICATION_STALE"
                elif legacy_integration:
                    action_type = "REQUEUE_LEGACY_INTEGRATION_STALE"
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
    architecture: ArchitectureArtifactService

    def build(self, node: AggregateSnapshot, *, dependency_outputs: Mapping[str, Any]) -> ArtifactRef:
        manifest_ref = dict(node.payload.get("architecture_manifest_ref") or {})
        record = self.architecture.repository.read_artifact_record(str(manifest_ref.get("sha256") or ""))
        if record and str(record.get("artifact_type") or "") == ARCHITECTURE_SKELETON_ARTIFACT:
            return self._build_skeleton_view(node, dependency_outputs=dependency_outputs)
        manifest = validate_architecture_manifest(self.architecture.artifacts.read_json(manifest_ref))
        fragments = self.architecture.load_manifest_fragments(manifest)
        unit_contract = self.architecture.artifacts.read_json(dict(node.payload["unit_contract_ref"]))
        requirement_ids = {str(item) for item in list(unit_contract.get("requirement_ids") or [])}
        requirements = [
            item
            for item in list(dict(fragments.get("requirements") or {}).get("requirements") or [])
            if str(item.get("requirement_id") or "") in requirement_ids
        ]
        found_requirement_ids = {str(item.get("requirement_id") or "") for item in requirements}
        if found_requirement_ids != requirement_ids:
            raise ValueError(f"UnitWorkView lost requirements: {sorted(requirement_ids - found_requirement_ids)}")
        unit_id = str(unit_contract.get("unit_id") or "")
        cross_contracts = [
            item
            for item in list(fragments.get("cross_unit_contract") or [])
            if unit_id in {str(item.get("provider") or ""), str(item.get("consumer") or "")}
        ]
        payload = {
            "schema_version": "1",
            "workflow_id": node.workflow_id,
            "node_run_id": node.aggregate_id,
            "epoch_id": str(node.payload.get("epoch_id") or ""),
            "unit_contract": unit_contract,
            "requirements": requirements,
            "cross_unit_contracts": cross_contracts,
            "global_constraints": fragments.get("global_constraints"),
            "assumptions": fragments.get("assumption_ledger"),
            "dependency_outputs": dict(dependency_outputs),
            "historical_repair_bills": list(node.payload.get("historical_repair_bill_refs") or []),
            "node_run_journal": dict(
                (self.architecture.repository.read_node_journal(node.aggregate_id) or {}).get("journal") or {}
            ),
        }
        return self.architecture.artifacts.put_json(
            payload,
            artifact_type="UnitWorkViewArtifact",
            child_refs=(
                (str(manifest_ref["sha256"]), "architecture_manifest"),
                (str(dict(node.payload["unit_contract_ref"])["sha256"]), "unit_contract"),
            ),
        )

    def _build_skeleton_view(
        self,
        node: AggregateSnapshot,
        *,
        dependency_outputs: Mapping[str, Any],
    ) -> ArtifactRef:
        manifest_ref = dict(node.payload.get("architecture_manifest_ref") or {})
        artifact = dict(self.architecture.artifacts.read_json(manifest_ref))
        contract_ref = dict(node.payload.get("unit_contract_ref") or {})
        contract = dict(self.architecture.artifacts.read_json(contract_ref))
        requirements_ref = dict(artifact.get("requirements_ref") or {})
        requirements = requirements_semantic_view(self.architecture.artifacts.read_json(requirements_ref))
        covered = {
            (str(item.get("section") or ""), str(item.get("requirement") or ""))
            for item in list(contract.get("covers") or [])
        }
        requirement_sections = {
            section: [text for text in values if (str(section), str(text)) in covered]
            for section, values in dict(requirements.get("sections") or {}).items()
        }
        requirement_sections = {section: values for section, values in requirement_sections.items() if values}
        if sum(len(values) for values in requirement_sections.values()) != len(covered):
            raise ValueError("ModuleWorkView lost one or more semantic Requirement references")
        path_policy = dict(node.payload.get("path_policy") or contract.get("paths") or {})
        semantic_dependency_outputs: dict[str, Any] = {}
        dependency_names = {str(item) for item in list(contract.get("depends_on") or [])}
        for dependency_id in list(node.payload.get("dependency_node_ids") or []):
            dependency = self.architecture.repository.read_snapshot(
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
                self.architecture.artifacts.read_json(
                    dict(dependency.payload.get("unit_contract_ref") or {})
                )
            )
            semantic_dependency_outputs[dependency_name] = {
                "status": "accepted" if dependency.state == "ACCEPTED" else dependency.state.lower(),
                "contract_paths": list(
                    dict(dependency_contract.get("paths") or {}).get("contract_paths") or []
                ),
                "declared_outputs": sorted(
                    str(key) for key in dict(dependency.payload.get("output_hashes") or {})
                ),
                "available_in_worktree": dependency.state == "ACCEPTED",
            }
        historical_refs = [
            dict(item)
            for item in list(node.payload.get("historical_repair_bill_refs") or [])
            if isinstance(item, Mapping) and item.get("sha256")
        ]
        payload = {
            "schema_version": "1",
            "module_name": str(contract.get("module_name") or node.payload.get("module_name") or ""),
            "requirements": {
                "title": str(requirements.get("title") or "Requirements"),
                "sections": requirement_sections,
            },
            "contract_paths": list(path_policy.get("contract_paths") or []),
            "implementation_scopes": list(path_policy.get("implementation_scopes") or []),
            "test_scopes": list(path_policy.get("test_scopes") or []),
            "reference_only": list(path_policy.get("reference_only") or []),
            "construction_dependencies": list(contract.get("depends_on") or []),
            "contract_consumption": list(contract.get("consumes") or []),
            "evidence": list(contract.get("evidence") or []),
            "dependency_outputs": semantic_dependency_outputs,
            "historical_repair_bills": [
                repair_bill_semantic_view(self.architecture.artifacts, item) for item in historical_refs
            ],
            "node_run_journal": dict(
                (self.architecture.repository.read_node_journal(node.aggregate_id) or {}).get("journal") or {}
            ),
        }
        return self.architecture.artifacts.put_json(
            payload,
            artifact_type="ModuleWorkViewArtifact",
            child_refs=(
                (str(manifest_ref["sha256"]), "architecture_skeleton"),
                (str(contract_ref["sha256"]), "module_contract"),
                *((str(item["sha256"]), "historical_repair_bill") for item in historical_refs),
            ),
        )


@dataclass
class NodeQuiescer:
    repository: MinionV2Repository
    process_controller: WorkerProcessController
    worktree_locks: WorkspaceLockRegistry

    def quiesce(
        self,
        *,
        node_run_id: str,
        worker_id: str,
        lease_resource_key: str,
        fencing_token: int,
        worktree: Path,
        timeout_seconds: float = 10.0,
    ) -> QuiesceResult:
        self.repository.assert_fencing_token(lease_resource_key, worker_id, fencing_token)
        self.process_controller.revoke_tool_token(worker_id, fencing_token)
        self.process_controller.request_cooperative_stop(worker_id)
        if not self.process_controller.kill_and_reap_process_group(worker_id, timeout_seconds):
            raise RuntimeError("worker process group did not stop before quiesce timeout")
        if self.process_controller.has_live_processes_for_workspace(worktree):
            raise RuntimeError("a live process still holds the candidate worktree")
        lock_path = self.worktree_locks.acquire(node_run_id, worktree)
        try:
            fingerprint = workspace_content_fingerprint(worktree)
            return QuiesceResult(
                fencing_token=fencing_token,
                process_group_reaped=True,
                exclusive_workspace_lock=True,
                workspace_fingerprint=fingerprint,
                lock_path=lock_path,
            )
        except BaseException:
            self.worktree_locks.release(node_run_id)
            raise


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
        parent_candidate_digest: str = "",
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
            if current_head != base_sha:
                raise ValueError(
                    "coder changed Git HEAD; commits, merges, rebases, checkouts, and resets are manager-owned operations"
                )
            before = workspace_content_fingerprint(worktree)
            if before != expected_workspace_fingerprint:
                raise RuntimeError("worktree changed after quiescing")
            if not candidate_baseline_sha:
                raise ValueError("candidate requires the fixed assembled Node baseline")
            changed_paths = git_changed_paths(worktree, candidate_baseline_sha)
            if path_policy:
                _validate_skeleton_candidate_paths(changed_paths, path_policy)
            else:
                _validate_reference_only_paths(changed_paths, reference_only_paths)
            if not changed_paths:
                raise ValueError("candidate has no changes")
            candidate_key = hashlib.sha256(
                json.dumps(
                    {
                        "node_run_id": node_run_id,
                        "candidate_baseline_sha": candidate_baseline_sha,
                        "previous_head_sha": base_sha,
                        "parent_candidate_digest": parent_candidate_digest,
                        "workspace_fingerprint": before,
                        "unit_contract_hash": unit_contract_hash,
                    },
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            existing_sha = _find_candidate_commit(worktree, candidate_key)
            if existing_sha:
                candidate_digest = existing_sha
            else:
                _git(worktree, "add", "-A")
                message = f"minion candidate {node_run_id}\n\nPal-Candidate-Key: {candidate_key}"
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
                    candidate_baseline_sha,
                    "-m",
                    message,
                ).strip()
                _git(worktree, "update-ref", f"refs/pal/candidates/{candidate_key}", candidate_digest)
                _git(worktree, "reset", "--hard", candidate_digest)
            after = workspace_content_fingerprint(worktree)
            if before != after:
                raise RuntimeError("worktree content changed while candidate commit was created")
            baseline_tree_sha = _git(worktree, "rev-parse", f"{candidate_baseline_sha}^{{tree}}").strip()
            candidate_tree_sha = _git(worktree, "rev-parse", f"{candidate_digest}^{{tree}}").strip()
            delta_patch = _git_bytes(worktree, "diff", "--binary", candidate_baseline_sha, candidate_digest, "--")
            candidate = {
                "schema_version": "2",
                "node_run_id": node_run_id,
                "candidate_digest": candidate_digest,
                "base_sha": candidate_baseline_sha,
                "previous_head_sha": base_sha,
                "baseline_tree_sha": baseline_tree_sha,
                "candidate_tree_sha": candidate_tree_sha,
                "delta_patch_sha": hashlib.sha256(delta_patch).hexdigest(),
                "parent_candidate_digest": parent_candidate_digest,
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
                artifact_type="CandidateSnapshotArtifact",
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


def provision_epoch_worktrees(
    runtime_root: Path,
    *,
    epoch_id: str,
    unit_ids: list[str],
    workspace: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
    epoch_root = Path(runtime_root) / "data" / "minion" / "v2" / "repos" / epoch_id
    common_git_dir = epoch_root / "project.git"
    worktree_root = epoch_root / "worktrees"
    if not common_git_dir.exists():
        epoch_root.mkdir(parents=True, exist_ok=True)
        source = str(workspace.get("repo_path") or workspace.get("cwd") or "").strip()
        if source:
            completed = subprocess.run(
                ["git", "clone", "--bare", "--no-hardlinks", str(Path(source).expanduser()), str(common_git_dir)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr or completed.stdout or "failed to clone V2 epoch repository")
        else:
            seed = epoch_root / "seed"
            seed.mkdir(parents=True, exist_ok=True)
            _git(seed, "init", "-q", "-b", "main")
            _git(seed, "-c", "user.name=Pal Minion", "-c", "user.email=minion@localhost", "commit", "--allow-empty", "-qm", "V2 epoch base")
            completed = subprocess.run(
                ["git", "clone", "--bare", str(seed), str(common_git_dir)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            shutil.rmtree(seed, ignore_errors=True)
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr or completed.stdout or "failed to initialize V2 epoch repository")
        base_sha = subprocess.check_output(
        ["git", f"--git-dir={common_git_dir}", "rev-parse", "HEAD"],
        text=True,
    ).strip()
    base_tree_sha = subprocess.check_output(
        ["git", f"--git-dir={common_git_dir}", "rev-parse", "HEAD^{tree}"],
        text=True,
    ).strip()
    result: dict[str, dict[str, str]] = {}
    for unit_id in [*unit_ids, "integration"]:
        safe_id = _safe_ref(unit_id)
        worktree = worktree_root / safe_id
        branch = f"v2/{_safe_ref(epoch_id)}/{safe_id}"
        if not worktree.exists():
            worktree.parent.mkdir(parents=True, exist_ok=True)
            completed = subprocess.run(
                [
                    "git",
                    f"--git-dir={common_git_dir}",
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(worktree),
                    base_sha,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr or completed.stdout or f"failed to provision node worktree {unit_id}")
        result[unit_id] = {
            "workspace_path": str(worktree),
            "common_git_dir": str(common_git_dir),
            "worktree_branch": branch,
            "epoch_base_sha": base_sha,
            "epoch_base_tree_sha": base_tree_sha,
            "base_digest": base_sha,
            "base_sha": base_sha,
        }
    return result


def provision_skeleton_epoch_worktrees(
    runtime_root: Path,
    *,
    artifacts: ContentAddressedArtifactStore,
    epoch_id: str,
    unit_ids: list[str],
    architecture_artifact: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
    epoch_root = Path(runtime_root) / "data" / "minion" / "v2" / "repos" / epoch_id
    common_git_dir = epoch_root / "project.git"
    worktree_root = epoch_root / "worktrees"
    skeleton_sha = str(architecture_artifact.get("skeleton_commit_sha") or "")
    skeleton_tree = str(architecture_artifact.get("skeleton_tree_sha") or "")
    bundle_ref = ArtifactRef.from_mapping(dict(architecture_artifact.get("git_bundle_ref") or {}))
    if not skeleton_sha or not skeleton_tree or not bundle_ref.sha256:
        raise ValueError("ArchitectureSkeletonArtifact is missing its commit, tree, or Git bundle")
    if not common_git_dir.exists():
        epoch_root.mkdir(parents=True, exist_ok=True)
        bundle_path = epoch_root / "architecture.bundle"
        bundle_path.write_bytes(artifacts.read_bytes(bundle_ref))
        completed = subprocess.run(
            ["git", "clone", "--bare", str(bundle_path), str(common_git_dir)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        bundle_path.unlink(missing_ok=True)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr or completed.stdout or "failed to restore skeleton epoch repository")
    restored_tree = _git_dir(common_git_dir, "rev-parse", f"{skeleton_sha}^{{tree}}").strip()
    if restored_tree != skeleton_tree:
        raise RuntimeError("restored skeleton Git bundle does not match the accepted tree")
    result: dict[str, dict[str, str]] = {}
    for unit_id in unit_ids:
        safe_id = _safe_ref(unit_id)
        worktree = worktree_root / safe_id
        branch = f"v2/{_safe_ref(epoch_id)}/{safe_id}"
        if not worktree.exists():
            worktree.parent.mkdir(parents=True, exist_ok=True)
            completed = subprocess.run(
                [
                    "git",
                    f"--git-dir={common_git_dir}",
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(worktree),
                    skeleton_sha,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr or completed.stdout or f"failed to provision skeleton node {unit_id}")
        if _git(worktree, "rev-parse", "HEAD").strip() != skeleton_sha:
            raise RuntimeError(f"node {unit_id} did not start from the accepted skeleton commit")
        result[unit_id] = {
            "workspace_path": str(worktree),
            "common_git_dir": str(common_git_dir),
            "worktree_branch": branch,
            "epoch_base_sha": skeleton_sha,
            "epoch_base_tree_sha": skeleton_tree,
            "base_digest": skeleton_sha,
            "base_sha": skeleton_sha,
            "execution_adapter": SOFTWARE_GIT_ADAPTER,
        }
    return result


def provision_verification_worktree(
    runtime_root: Path,
    *,
    node: AggregateSnapshot,
    candidate_digest: str,
) -> tuple[Path, Path]:
    if not candidate_digest:
        raise ValueError("verification worktree requires candidate_digest")
    common_git_dir = Path(str(node.payload.get("common_git_dir") or ""))
    if not common_git_dir.is_dir():
        raise ValueError("verification worktree requires the epoch common Git directory")
    review_root = (
        Path(runtime_root)
        / "data"
        / "minion"
        / "v2"
        / "reviews"
        / _safe_ref(node.aggregate_id)
        / _safe_ref(candidate_digest)
    )
    worktree = review_root / "worktree"
    scratch = review_root / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    if not worktree.exists():
        worktree.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [
                "git",
                f"--git-dir={common_git_dir}",
                "worktree",
                "add",
                "--detach",
                str(worktree),
                candidate_digest,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr or completed.stdout or "failed to provision verification worktree")
    else:
        _git(worktree, "reset", "--hard", candidate_digest)
        _git(worktree, "clean", "-fdx")
    if _git(worktree, "rev-parse", "HEAD").strip() != candidate_digest:
        raise RuntimeError("verification worktree is not bound to the candidate SHA")
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
    adapter = str(node.payload.get("execution_adapter") or SOFTWARE_GIT_ADAPTER)
    accepted_digests: list[str] = []
    output_hashes: dict[str, str] = {}
    dependency_outputs: dict[str, Any] = {}
    for dependency in _ordered_dependency_closure(node, node_by_id):
        dependency_id = dependency.aggregate_id
        if dependency.state != "ACCEPTED":
            raise ValueError(f"dependency is not accepted: {dependency_id}")
        candidate_digest = str(dependency.payload.get("candidate_digest") or "")
        if not candidate_digest:
            raise ValueError(f"accepted dependency has no candidate digest: {dependency_id}")
        if adapter == SOFTWARE_GIT_ADAPTER:
            if apply_candidates and not _git_is_ancestor(workspace, candidate_digest, "HEAD"):
                _git(workspace, "cherry-pick", candidate_digest)
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


def workspace_has_live_processes(worktree: Path) -> bool:
    """Detect processes whose cwd or open files still touch a worktree."""
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return False
    resolved = str(worktree.resolve())
    prefix = resolved + os.sep
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        links = [entry / "cwd"]
        fd_dir = entry / "fd"
        try:
            links.extend(fd_dir.iterdir())
        except OSError:
            pass
        for link in links:
            try:
                target = os.readlink(link)
            except OSError:
                continue
            normalized = target.removesuffix(" (deleted)")
            if normalized == resolved or normalized.startswith(prefix):
                return True
    return False


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


def _validate_skeleton_candidate_paths(changed_paths: list[str], policy: Mapping[str, Any]) -> None:
    frozen = {str(item).replace(os.sep, "/") for item in list(policy.get("contract_paths") or [])}
    references = {str(item).replace(os.sep, "/") for item in list(policy.get("reference_only") or [])}
    writable = [
        dict(item or {})
        for item in [
            *list(policy.get("implementation_scopes") or []),
            *list(policy.get("test_scopes") or []),
        ]
    ]
    frozen_violations = sorted(path for path in changed_paths if path.replace(os.sep, "/") in frozen)
    if frozen_violations:
        raise ValueError("candidate modified frozen architecture contracts: " + ", ".join(frozen_violations))
    reference_violations = sorted(path for path in changed_paths if path.replace(os.sep, "/") in references)
    if reference_violations:
        raise ValueError("candidate modified reference-only paths: " + ", ".join(reference_violations))
    outside = sorted(path for path in changed_paths if not any(_path_scope_matches(path, scope) for scope in writable))
    if outside:
        raise ValueError("candidate changed paths outside its owned implementation/test scopes: " + ", ".join(outside))


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
        output = _git(worktree, "log", "--all", "--format=%H%x00%B%x00", "--grep", f"Pal-Candidate-Key: {candidate_key}", "-n", "1")
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
