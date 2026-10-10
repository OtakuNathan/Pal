from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Mapping
from pal.bunshin.contract_runtime import ContractArtifactAccess
from pal.bunshin.adapters import ARTIFACT_BUNDLE_ADAPTER, SOFTWARE_GIT_ADAPTER
from pal.bunshin.artifacts import ArtifactRef
from pal.bunshin.contract_protocol import CONTRACT_ARTIFACT, software_contract_projection
from pal.bunshin.direct_contract import DIRECT_EXECUTION_ARTIFACT
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.execution_models import ExecutionCompilation
from pal.bunshin.graph_installation import _workflow_execution_adapter


from pal.bunshin.skeleton_compilation import SkeletonEpochCompiler
from pal.bunshin.data_contract_compilation import ArtifactEpochCompiler


@dataclass
class ExecutionCompiler:
    repository: BunshinRepository
    contracts: ContractArtifactAccess
    skeleton: SkeletonEpochCompiler = field(init=False)
    artifacts: ArtifactEpochCompiler = field(init=False)

    def __post_init__(self) -> None:
        self.skeleton = SkeletonEpochCompiler(self.repository, self.contracts)
        self.artifacts = ArtifactEpochCompiler(self.repository, self.contracts)

    def compile_epoch(
        self,
        *,
        workflow_id: str,
        epoch_id: str,
        manifest_ref: ArtifactRef,
        actor: str = "bunshin-manager",
        source_epoch_id: str = "",
        initial_repair_bill_ref: Mapping[str, Any] | None = None,
    ) -> ExecutionCompilation:
        record = self.repository.artifacts.read_artifact_record(manifest_ref.sha256)
        if record and record.get("artifact_type") == DIRECT_EXECUTION_ARTIFACT:
            artifact = dict(self.contracts.artifacts.read_json(manifest_ref))
            if _workflow_execution_adapter(self.repository, self.contracts, workflow_id) != SOFTWARE_GIT_ADAPTER:
                raise ValueError("direct execution requires software Git execution")
            return self.skeleton.compile(
                workflow_id=workflow_id, epoch_id=epoch_id, manifest_ref=manifest_ref, actor=actor,
                source_epoch_id=source_epoch_id, initial_repair_bill_ref=initial_repair_bill_ref,
                artifact_override=artifact,
            )
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
                return self.skeleton.compile(
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
                return self.artifacts.compile(
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
