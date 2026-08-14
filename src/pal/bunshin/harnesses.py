from __future__ import annotations

import hashlib
import json
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping


HARNESS_PROTOCOL_VERSION = "bunshin_harness.v1"
PAL_HARNESS_ID = "pal"
CODEX_ARCHITECT_HARNESS_ID = "codex_architect"
HARNESS_LAUNCH_PAL_SANDBOX = "pal_sandbox"
HARNESS_LAUNCH_HOST = "host"


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class BunshinHarnessSpec:
    harness_id: str
    protocol_version: str
    supported_roles: tuple[str, ...]
    priority: int
    launch_kind: str
    worker_argv: tuple[str, ...]
    config: Mapping[str, Any] = field(default_factory=dict)
    provider_generation: str = ""

    def __post_init__(self) -> None:
        harness_id = str(self.harness_id or "").strip()
        if not harness_id or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
            for character in harness_id
        ):
            raise ValueError("harness_id must be a lowercase stable identifier")
        if self.protocol_version != HARNESS_PROTOCOL_VERSION:
            raise ValueError(
                f"unsupported harness protocol: {self.protocol_version}"
            )
        roles = tuple(
            sorted({str(role or "").strip() for role in self.supported_roles})
        )
        if not roles or any(
            role not in {"architect", "reviewer", "implementation", "verifier"}
            for role in roles
        ):
            raise ValueError("harness supported_roles are invalid")
        if self.launch_kind not in {
            HARNESS_LAUNCH_PAL_SANDBOX,
            HARNESS_LAUNCH_HOST,
        }:
            raise ValueError("harness launch_kind is invalid")
        argv = tuple(str(item or "").strip() for item in self.worker_argv)
        if not argv or any(not item for item in argv):
            raise ValueError("harness worker_argv must be non-empty")
        executable = Path(argv[0]).expanduser()
        if not executable.is_absolute():
            raise ValueError("harness worker executable must be absolute")
        if self.launch_kind == HARNESS_LAUNCH_HOST:
            if len(argv) < 2:
                raise ValueError("host harness requires an absolute worker path")
            worker = Path(argv[1]).expanduser()
            if not worker.is_absolute():
                raise ValueError("host harness worker path must be absolute")
        object.__setattr__(self, "harness_id", harness_id)
        object.__setattr__(self, "supported_roles", roles)
        object.__setattr__(self, "worker_argv", argv)
        object.__setattr__(self, "config", dict(self.config or {}))
        if not self.provider_generation:
            object.__setattr__(
                self,
                "provider_generation",
                _stable_hash(self._identity_payload()),
            )

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "harness_id": self.harness_id,
            "protocol_version": self.protocol_version,
            "supported_roles": list(self.supported_roles),
            "priority": int(self.priority),
            "launch_kind": self.launch_kind,
            "worker_argv": list(self.worker_argv),
            "config": dict(self.config),
        }

    def supports(self, role: str) -> bool:
        return str(role or "").strip() in self.supported_roles

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._identity_payload(),
            "provider_generation": self.provider_generation,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "BunshinHarnessSpec":
        payload = dict(value or {})
        return cls(
            harness_id=str(payload.get("harness_id") or ""),
            protocol_version=str(payload.get("protocol_version") or ""),
            supported_roles=tuple(
                str(item) for item in list(payload.get("supported_roles") or [])
            ),
            priority=int(payload.get("priority") or 0),
            launch_kind=str(payload.get("launch_kind") or ""),
            worker_argv=tuple(
                str(item) for item in list(payload.get("worker_argv") or [])
            ),
            config=dict(payload.get("config") or {}),
            provider_generation=str(
                payload.get("provider_generation") or ""
            ),
        )


@dataclass(frozen=True)
class BunshinHarnessRegistryGeneration:
    generation_hash: str
    specs: tuple[BunshinHarnessSpec, ...]

    def select(self, role: str) -> BunshinHarnessSpec:
        candidates = [spec for spec in self.specs if spec.supports(role)]
        if not candidates:
            raise LookupError(f"no Bunshin harness supports role {role}")
        return sorted(
            candidates,
            key=lambda spec: (-int(spec.priority), spec.harness_id),
        )[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation_hash": self.generation_hash,
            "specs": [spec.to_dict() for spec in self.specs],
        }


def pal_harness_spec() -> BunshinHarnessSpec:
    return BunshinHarnessSpec(
        harness_id=PAL_HARNESS_ID,
        protocol_version=HARNESS_PROTOCOL_VERSION,
        supported_roles=(
            "architect",
            "reviewer",
            "implementation",
            "verifier",
        ),
        priority=0,
        launch_kind=HARNESS_LAUNCH_PAL_SANDBOX,
        worker_argv=(
            str(Path(sys.executable).resolve()),
            "-m",
            "pal.bunshin.v2.worker_main",
        ),
        config={},
    )


class BunshinHarnessRegistry:
    """Atomically compiled harness registry shared through immutable generations."""

    def __init__(self, *, include_pal: bool = False) -> None:
        self._lock = threading.RLock()
        self._listeners: list[
            Callable[[BunshinHarnessRegistryGeneration], None]
        ] = []
        self._specs: dict[str, BunshinHarnessSpec] = {}
        if include_pal:
            pal = pal_harness_spec()
            self._specs[pal.harness_id] = pal
        self._generation = self._compile()

    def snapshot(self) -> BunshinHarnessRegistryGeneration:
        with self._lock:
            return self._generation

    def register(self, spec: BunshinHarnessSpec) -> None:
        with self._lock:
            current = self._specs.get(spec.harness_id)
            if current is not None and current != spec:
                raise ValueError(
                    f"harness alias conflict: {spec.harness_id}"
                )
            if current == spec:
                return
            previous_specs = dict(self._specs)
            previous_generation = self._generation
            self._specs[spec.harness_id] = spec
            try:
                self._replace_generation_locked()
            except Exception:
                self._specs = previous_specs
                self._generation = previous_generation
                self._notify_listeners_locked(previous_generation)
                raise

    def unregister(self, harness_id: str) -> None:
        normalized = str(harness_id or "").strip()
        with self._lock:
            if normalized == PAL_HARNESS_ID:
                raise ValueError("built-in Pal harness cannot be unregistered")
            if normalized not in self._specs:
                return
            previous_specs = dict(self._specs)
            previous_generation = self._generation
            self._specs.pop(normalized)
            try:
                self._replace_generation_locked()
            except Exception:
                self._specs = previous_specs
                self._generation = previous_generation
                self._notify_listeners_locked(previous_generation)
                raise

    def replace_external(
        self,
        value: Mapping[str, Any],
    ) -> BunshinHarnessRegistryGeneration:
        raw_specs = [
            BunshinHarnessSpec.from_mapping(dict(item))
            for item in list(value.get("specs") or [])
            if isinstance(item, Mapping)
        ]
        with self._lock:
            pal = self._specs.get(PAL_HARNESS_ID) or pal_harness_spec()
            candidate = {PAL_HARNESS_ID: pal}
            for spec in raw_specs:
                if spec.harness_id == PAL_HARNESS_ID:
                    raise ValueError(
                        "external harness registry cannot replace Pal"
                    )
                if spec.harness_id in candidate:
                    raise ValueError(
                        f"harness alias conflict: {spec.harness_id}"
                    )
                candidate[spec.harness_id] = spec
            previous_specs = dict(self._specs)
            previous_generation = self._generation
            self._specs = candidate
            try:
                self._replace_generation_locked()
            except Exception:
                self._specs = previous_specs
                self._generation = previous_generation
                self._notify_listeners_locked(previous_generation)
                raise
            return self._generation

    def subscribe(
        self,
        listener: Callable[[BunshinHarnessRegistryGeneration], None],
    ) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def _replace_generation_locked(self) -> None:
        generation = self._compile()
        self._generation = generation
        self._notify_listeners_locked(generation)

    def _notify_listeners_locked(
        self,
        generation: BunshinHarnessRegistryGeneration,
    ) -> None:
        listeners = tuple(self._listeners)
        for listener in listeners:
            listener(generation)

    def _compile(self) -> BunshinHarnessRegistryGeneration:
        specs = tuple(
            sorted(self._specs.values(), key=lambda spec: spec.harness_id)
        )
        payload = [spec.to_dict() for spec in specs]
        return BunshinHarnessRegistryGeneration(
            generation_hash=_stable_hash(payload),
            specs=specs,
        )
