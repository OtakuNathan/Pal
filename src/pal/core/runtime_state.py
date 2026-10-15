from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Mapping, Protocol, Sequence

from pal.memory.persistence.contracts import (
    RUNTIME_SNAPSHOT_SCHEMA_VERSION as RUNTIME_SNAPSHOT_SCHEMA_VERSION,
    RuntimeSnapshotIdentity as RuntimeSnapshotIdentity,
    validate_runtime_snapshot as validate_runtime_snapshot,
)


class RuntimeStatePort(Protocol):
    module_id: str
    schema_version: str
    state_order: int
    readable_schema_versions: tuple[str, ...]

    def snapshot_state(self) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]:
        ...

    def prepare_restore_state(self, payload: Mapping[str, Any]) -> Any | Awaitable[Any]:
        ...

    def install_prepared_state(self, prepared: Any) -> None | Awaitable[None]:
        ...

    def finish_restore_state(self, ports: Sequence[RuntimeStatePort]) -> None | Awaitable[None]:
        ...

    def reset_state(self, reason: str) -> None | Awaitable[None]:
        ...


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
            readable_versions = port.readable_schema_versions
            if str(record.get("schema_version") or "") not in readable_versions:
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
        for port in ports:
            await _maybe_await(port.finish_restore_state(ports))

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


def runtime_spec_hash(
    module_registry: Any,
    *,
    identity_parts: Mapping[str, Any] | None = None,
    exclude_module_ids: frozenset[str] = frozenset(),
) -> str:
    """Return the deterministic restore contract for one logical coroutine."""

    modules = sorted(
        (
            str(port.module_id),
            str(port.schema_version),
        )
        for handle in module_registry.modules.values()
        if (port := handle.runtime_state_port) is not None
        and port.module_id not in exclude_module_ids
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
