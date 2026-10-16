"""Persistence snapshot contracts, independent of runtime coordination."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, TYPE_CHECKING

from pal.shared.ports import PortKey

if TYPE_CHECKING:
    from pal.memory.history_contracts import LeftSnapshot
    from pal.memory.persistence.conversation import ConversationHistoryReference

RUNTIME_SNAPSHOT_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class RuntimeSnapshotIdentity:
    logical_coroutine_id: str
    workflow_id: str
    stage_key: str
    sequence: int
    producer_fencing_token: int
    runtime_spec_hash: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("logical_coroutine_id", self.logical_coroutine_id),
            ("workflow_id", self.workflow_id),
            ("stage_key", self.stage_key),
            ("runtime_spec_hash", self.runtime_spec_hash),
        ):
            if not str(value or "").strip():
                raise ValueError(f"runtime snapshot {field_name} is required")
        if self.sequence < 0:
            raise ValueError("runtime snapshot sequence must be non-negative")
        if self.producer_fencing_token <= 0:
            raise ValueError("runtime snapshot producer_fencing_token must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_coroutine_id": self.logical_coroutine_id,
            "workflow_id": self.workflow_id,
            "stage_key": self.stage_key,
            "sequence": self.sequence,
            "producer_fencing_token": self.producer_fencing_token,
            "runtime_spec_hash": self.runtime_spec_hash,
        }


def validate_runtime_snapshot(
    snapshot: Mapping[str, Any],
    *,
    expected_identity: RuntimeSnapshotIdentity | None = None,
) -> dict[str, Any]:
    value = dict(snapshot)
    allowed = {
        "schema_version",
        "logical_coroutine_id",
        "workflow_id",
        "stage_key",
        "sequence",
        "producer_fencing_token",
        "runtime_spec_hash",
        "modules",
    }
    extras = sorted(set(value) - allowed)
    if extras:
        raise ValueError(f"runtime snapshot has unknown fields: {extras}")
    if str(value.get("schema_version") or "") != RUNTIME_SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("runtime snapshot schema is unsupported")
    identity = RuntimeSnapshotIdentity(
        logical_coroutine_id=str(value.get("logical_coroutine_id") or ""),
        workflow_id=str(value.get("workflow_id") or ""),
        stage_key=str(value.get("stage_key") or ""),
        sequence=int(value.get("sequence") or 0),
        producer_fencing_token=int(value.get("producer_fencing_token") or 0),
        runtime_spec_hash=str(value.get("runtime_spec_hash") or ""),
    )
    if expected_identity is not None and identity != expected_identity:
        raise ValueError("runtime snapshot identity does not match the requested incarnation")
    if not isinstance(value.get("modules"), Mapping):
        raise ValueError("runtime snapshot modules must be an object")
    value["modules"] = dict(value["modules"])
    return value


class RuntimeSnapshotCoordinatorPort(Protocol):
    async def snapshot(self, identity: RuntimeSnapshotIdentity) -> dict[str, Any]: ...
    async def restore(self, snapshot: Mapping[str, Any]) -> None: ...
    async def reset(self, reason: str) -> tuple[str, ...]: ...


class ResidentHistoryPort(Protocol):
    def submitted(self) -> None: ...
    async def checkpoint_async(self, reason: str, *, rebase: bool = False) -> bool: ...
    async def reset_async(self) -> None: ...
    async def reconcile_async(self) -> bool: ...
    def prepare_history_reference(self, snapshot: LeftSnapshot) -> ConversationHistoryReference: ...
    def inspect(self) -> dict: ...
    def record_reply(self, turn_id: str, text: str) -> None: ...
    def record_stream(self, turn_id: str, kind: str, text: str) -> None: ...
    def finish_partial_stream(self, turn_id: str) -> None: ...
    def take_degraded_notice(self) -> bool: ...


RESIDENT_HISTORY = PortKey[ResidentHistoryPort]("memory:persistence", ResidentHistoryPort)
