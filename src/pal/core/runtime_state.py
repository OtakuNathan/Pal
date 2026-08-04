from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Mapping, Protocol


RUNTIME_SNAPSHOT_SCHEMA_VERSION = "1"


class RuntimeStatePort(Protocol):
    module_id: str
    schema_version: str
    state_order: int

    def snapshot_state(self) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]:
        ...

    def prepare_restore_state(self, payload: Mapping[str, Any]) -> Any | Awaitable[Any]:
        ...

    def install_prepared_state(self, prepared: Any) -> None | Awaitable[None]:
        ...

    def reset_state(self, reason: str) -> None | Awaitable[None]:
        ...


@dataclass(frozen=True)
class RuntimeSnapshotIdentity:
    logical_coroutine_id: str
    workflow_id: str
    stage_key: str
    sequence: int
    producer_fencing_token: int
    runtime_spec_hash: str

    def __post_init__(self) -> None:
        for field_name in (
            "logical_coroutine_id",
            "workflow_id",
            "stage_key",
            "runtime_spec_hash",
        ):
            if not str(getattr(self, field_name) or "").strip():
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


@dataclass
class RuntimeSnapshotCoordinator:
    module_registry: Any

    async def snapshot(self, identity: RuntimeSnapshotIdentity) -> dict[str, Any]:
        modules: dict[str, dict[str, Any]] = {}
        for port in self._ports():
            payload = await _maybe_await(port.snapshot_state())
            if not isinstance(payload, Mapping):
                raise TypeError(f"runtime state for {port.module_id} is not an object")
            modules[port.module_id] = {
                "schema_version": str(port.schema_version),
                "payload": dict(payload),
            }
        return {
            "schema_version": RUNTIME_SNAPSHOT_SCHEMA_VERSION,
            **identity.to_dict(),
            "modules": modules,
        }

    async def restore(
        self,
        snapshot: Mapping[str, Any],
        *,
        expected_identity: RuntimeSnapshotIdentity | None = None,
    ) -> None:
        value = validate_runtime_snapshot(snapshot, expected_identity=expected_identity)
        modules = dict(value["modules"])
        ports = self._ports()
        expected_modules = {port.module_id for port in ports}
        if set(modules) != expected_modules:
            missing = sorted(expected_modules - set(modules))
            extra = sorted(set(modules) - expected_modules)
            raise ValueError(
                f"runtime snapshot module set mismatch: missing={missing}, extra={extra}"
            )
        prepared: list[tuple[RuntimeStatePort, Any]] = []
        for port in ports:
            record = dict(modules[port.module_id])
            if str(record.get("schema_version") or "") != str(port.schema_version):
                raise ValueError(
                    f"runtime snapshot schema mismatch for {port.module_id}"
                )
            payload = record.get("payload")
            if not isinstance(payload, Mapping):
                raise ValueError(f"runtime snapshot payload for {port.module_id} is invalid")
            candidate = await _maybe_await(port.prepare_restore_state(dict(payload)))
            prepared.append((port, candidate))
        # Every payload has been decoded and validated. Implementations must
        # install prepared state using non-failing pointer/state swaps only.
        for port, candidate in prepared:
            await _maybe_await(port.install_prepared_state(candidate))

    async def reset(self, reason: str) -> tuple[str, ...]:
        failures: list[str] = []
        for port in reversed(self._ports()):
            try:
                await _maybe_await(port.reset_state(str(reason)))
            except Exception as exc:
                failures.append(f"{port.module_id}: {exc}")
        if failures:
            raise RuntimeError("runtime reset failed: " + "; ".join(failures))
        return tuple(port.module_id for port in reversed(self._ports()))

    def _ports(self) -> list[RuntimeStatePort]:
        ports = [
            handle.runtime_state_port
            for handle in self.module_registry.modules.values()
            if handle.runtime_state_port is not None
        ]
        module_ids = [str(port.module_id) for port in ports]
        duplicates = sorted(
            module_id
            for module_id in set(module_ids)
            if module_ids.count(module_id) > 1
        )
        if duplicates:
            raise ValueError(
                "duplicate runtime-state port module_id: "
                + ", ".join(duplicates)
            )
        return sorted(
            ports,
            key=lambda port: (int(port.state_order), str(port.module_id)),
        )


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


def runtime_spec_hash(
    module_registry: Any,
    *,
    identity_parts: Mapping[str, Any] | None = None,
) -> str:
    """Return the deterministic restore contract for one logical coroutine."""

    modules = sorted(
        (
            str(port.module_id),
            str(port.schema_version),
        )
        for handle in module_registry.modules.values()
        if (port := handle.runtime_state_port) is not None
    )
    payload = {
        "modules": modules,
        "identity": dict(identity_parts or {}),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value
