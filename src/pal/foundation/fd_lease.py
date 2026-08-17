from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, time as datetime_time
from decimal import Decimal
from enum import Enum, StrEnum
from pathlib import PurePath
from threading import Condition, RLock
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Generic, TypeVar
from uuid import UUID, uuid4


T = TypeVar("T")
R = TypeVar("R")

SyncOperation = Callable[[T], R]
AsyncOperation = Callable[[T], Awaitable[R]]
SyncCloser = Callable[[T], "FdCloseOutcome"]
AsyncCloser = Callable[[T], Awaitable["FdCloseOutcome"]]
InterruptHook = Callable[[T, str], None]


class FdLeaseInvariantError(RuntimeError):
    """An fd authority transition violated the ownership contract."""


class FdLeaseCancelledError(RuntimeError):
    """The capability was cancelled or revoked before a new I/O admission."""


class FdControlState(StrEnum):
    OPEN = "OPEN"
    RETIRING = "RETIRING"
    CLOSING = "CLOSING"
    REVOKING = "REVOKING"
    CLOSED = "CLOSED"
    QUARANTINED = "QUARANTINED"


class FdCapabilityState(StrEnum):
    LIVE = "LIVE"
    CANCELLED = "CANCELLED"
    TOMBSTONE = "TOMBSTONE"
    RELEASED = "RELEASED"


class FdCloseDisposition(StrEnum):
    DETACHED = "DETACHED"
    UNCERTAIN = "UNCERTAIN"


FD_CONTROL_TRANSITIONS: tuple[tuple[FdControlState, str, FdControlState], ...] = (
    (FdControlState.CLOSED, "PUBLISH", FdControlState.OPEN),
    (FdControlState.OPEN, "REQUEST_RETIRE", FdControlState.RETIRING),
    (FdControlState.RETIRING, "CLAIM_GRACEFUL_CLOSE", FdControlState.CLOSING),
    (FdControlState.RETIRING, "FORCE_REVOKE", FdControlState.REVOKING),
    (FdControlState.CLOSING, "CLOSE_DETACHED", FdControlState.CLOSED),
    (FdControlState.REVOKING, "CLOSE_DETACHED", FdControlState.CLOSED),
    (FdControlState.CLOSING, "CLOSE_UNCERTAIN", FdControlState.QUARANTINED),
    (FdControlState.REVOKING, "CLOSE_UNCERTAIN", FdControlState.QUARANTINED),
)
FD_CAPABILITY_TRANSITIONS: tuple[
    tuple[FdCapabilityState, str, FdCapabilityState], ...
] = (
    (FdCapabilityState.LIVE, "REQUEST_CANCEL", FdCapabilityState.CANCELLED),
    (FdCapabilityState.LIVE, "RETIRE_CANCEL", FdCapabilityState.CANCELLED),
    (FdCapabilityState.LIVE, "FORCE_REVOKE", FdCapabilityState.TOMBSTONE),
    (FdCapabilityState.CANCELLED, "FORCE_REVOKE", FdCapabilityState.TOMBSTONE),
    (FdCapabilityState.LIVE, "RELEASE", FdCapabilityState.RELEASED),
    (FdCapabilityState.CANCELLED, "RELEASE", FdCapabilityState.RELEASED),
    (FdCapabilityState.TOMBSTONE, "RELEASE", FdCapabilityState.RELEASED),
)
_CONTROL_TRANSITION_INDEX = {
    (source, action): target for source, action, target in FD_CONTROL_TRANSITIONS
}
_CAPABILITY_TRANSITION_INDEX = {
    (source, action): target for source, action, target in FD_CAPABILITY_TRANSITIONS
}


@dataclass(frozen=True)
class FdCloseOutcome:
    disposition: FdCloseDisposition
    detail: str = ""

    @classmethod
    def detached(cls, detail: str = "") -> "FdCloseOutcome":
        return cls(FdCloseDisposition.DETACHED, str(detail or ""))

    @classmethod
    def uncertain(cls, detail: str = "") -> "FdCloseOutcome":
        return cls(FdCloseDisposition.UNCERTAIN, str(detail or ""))

    @property
    def is_detached(self) -> bool:
        return self.disposition == FdCloseDisposition.DETACHED


@dataclass(frozen=True)
class _CloseTicket(Generic[T]):
    generation: int
    resource: T
    action: str
    close_sync: SyncCloser | None
    close_async: AsyncCloser | None


@dataclass
class FdLeaseRegistry:
    recent_limit: int = 128
    _owners: dict[str, "FdLease[Any]"] = field(default_factory=dict, init=False)
    _recent: deque[dict[str, Any]] = field(default_factory=deque, init=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def register(self, owner: "FdLease[Any]") -> None:
        with self._lock:
            existing = self._owners.get(owner.owner_id)
            if existing is not None and existing is not owner:
                raise FdLeaseInvariantError(
                    f"fd lease owner id is already registered: {owner.owner_id}"
                )
            self._owners[owner.owner_id] = owner

    def terminal(
        self,
        owner: "FdLease[Any]",
        *,
        generation: int,
        state: FdControlState,
        snapshot: dict[str, Any],
    ) -> None:
        with self._lock:
            if state == FdControlState.QUARANTINED:
                # Quarantine is an ownership state, not telemetry. Keep the
                # complete resource graph strongly reachable so GC cannot
                # close an uncertain descriptor behind the generation fence.
                self._owners[owner.owner_id] = owner
                return
            # A new generation may publish immediately after CLOSED becomes
            # visible. Settlement for A must never unregister a republished B.
            if (
                self._owners.get(owner.owner_id) is owner
                and owner.generation == generation
            ):
                self._owners.pop(owner.owner_id, None)
            self._recent.append(snapshot)
            while len(self._recent) > self.recent_limit:
                self._recent.popleft()

    def snapshot(self, *, resource_kind: str | None = None) -> dict[str, Any]:
        with self._lock:
            owners = tuple(self._owners.values())
            recent = tuple(self._recent)
        if resource_kind is not None:
            owners = tuple(owner for owner in owners if owner.resource_kind == resource_kind)
            recent = tuple(item for item in recent if item.get("resource_kind") == resource_kind)
        active = [owner.snapshot() for owner in owners]
        return {
            "active_count": len(active),
            "quarantined_count": sum(
                item.get("state") == FdControlState.QUARANTINED.value for item in active
            ),
            "active": active,
            "recent": list(recent),
        }


FD_LEASES = FdLeaseRegistry()


@dataclass
class FdLease(Generic[T]):
    """Stable generation slot for one fd-backed resource graph.

    The real resource is private.  Callers can only acquire an opaque,
    generation-fenced capability and submit a lexical operation through it.
    A forced close tombstones outstanding capabilities, waits for every
    admitted call to quiesce, and only then invokes the one physical closer.
    A drain timeout does not attempt close; it quarantines the still-bound
    resource graph rather than risking descriptor reuse.
    """

    resource_kind: str
    _resource: T = field(repr=False)
    capacity: int | None = 1
    closer_sync: SyncCloser | None = field(default=None, repr=False)
    closer_async: AsyncCloser | None = field(default=None, repr=False)
    hard_closer_sync: SyncCloser | None = field(default=None, repr=False)
    hard_closer_async: AsyncCloser | None = field(default=None, repr=False)
    close_drain_timeout: float = 1.0
    owner_id: str = field(default_factory=lambda: f"fd_lease_{uuid4().hex}")
    state: FdControlState = field(default=FdControlState.OPEN, init=False)
    generation: int = field(default=1, init=False)
    retire_reason: str = field(default="", init=False)
    close_error: str = field(default="", init=False)
    close_outcome: FdCloseOutcome | None = field(default=None, init=False)
    created_at: float = field(default_factory=time.monotonic, init=False)
    _next_capability_id: int = field(default=0, init=False, repr=False)
    _capabilities: dict[int, "FdCapability[Any]"] = field(
        default_factory=dict, init=False, repr=False
    )
    _close_claimed_generation: int = field(default=0, init=False, repr=False)
    _close_attempts: dict[int, int] = field(default_factory=dict, init=False, repr=False)
    _trace: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=128), init=False, repr=False
    )
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _condition: Condition = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._condition = Condition(self._lock)
        if self.capacity is not None and self.capacity <= 0:
            raise ValueError("fd lease capacity must be positive or None")
        if self.close_drain_timeout < 0:
            raise ValueError("fd close drain timeout must be non-negative")
        self._validate_resource_configuration(
            self._resource,
            self.closer_sync,
            self.closer_async,
            self.hard_closer_sync,
            self.hard_closer_async,
        )
        self._close_attempts[self.generation] = 0
        self._record("PUBLISH", actor="publisher")
        FD_LEASES.register(self)

    @property
    def reusable(self) -> bool:
        with self._lock:
            return self.state == FdControlState.OPEN and self._capacity_available_locked()

    @property
    def publishable(self) -> bool:
        """Whether a replacement may be constructed for this stable slot.

        Callers that must allocate the replacement before :meth:`publish`
        should preflight this property while holding their own lifecycle lock.
        ``publish`` remains the authoritative atomic check.
        """

        with self._lock:
            return self.state == FdControlState.CLOSED

    @property
    def closed(self) -> bool:
        with self._lock:
            return self.state == FdControlState.CLOSED

    @property
    def quarantined(self) -> bool:
        with self._lock:
            return self.state == FdControlState.QUARANTINED

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._current_capabilities_locked())

    @property
    def reference_count(self) -> int:
        with self._lock:
            # The stable publication slot owns one root reference. Released
            # capabilities are removed immediately; stale generations remain
            # counted until their holders explicitly release them.
            return 1 + len(self._capabilities)

    def publish(
        self,
        resource: T,
        *,
        closer_sync: SyncCloser | None = None,
        closer_async: AsyncCloser | None = None,
        hard_closer_sync: SyncCloser | None = None,
        hard_closer_async: AsyncCloser | None = None,
    ) -> int:
        self._validate_resource_configuration(
            resource,
            closer_sync,
            closer_async,
            hard_closer_sync,
            hard_closer_async,
        )
        with self._lock:
            if self.state != FdControlState.CLOSED:
                raise FdLeaseInvariantError(
                    f"fd lease {self.owner_id} cannot publish from {self.state.value}"
                )
            self.generation += 1
            self._resource = resource
            self.closer_sync = closer_sync
            self.closer_async = closer_async
            self.hard_closer_sync = hard_closer_sync
            self.hard_closer_async = hard_closer_async
            self._transition_locked("PUBLISH", actor="publisher")
            self.retire_reason = ""
            self.close_error = ""
            self.close_outcome = None
            self._close_claimed_generation = 0
            self._close_attempts[self.generation] = 0
            self.created_at = time.monotonic()
            generation = self.generation
        FD_LEASES.register(self)
        return generation

    def acquire(
        self,
        *,
        operation_id: str,
        interrupt: InterruptHook | None = None,
    ) -> "FdCapability[T]":
        with self._lock:
            if self.state != FdControlState.OPEN or not self._capacity_available_locked():
                raise FdLeaseInvariantError(
                    f"fd lease {self.owner_id} cannot acquire from {self.state.value}"
                )
            self._next_capability_id += 1
            capability = FdCapability(
                _owner=self,
                capability_id=self._next_capability_id,
                generation=self.generation,
                operation_id=str(operation_id),
                owner_thread_id=threading.get_ident(),
                interrupt=interrupt,
            )
            self._capabilities[capability.capability_id] = capability
            self._record_locked("ACQUIRE", actor=capability.owner_actor, capability=capability)
            return capability

    def request_retire(self, reason: str = "retired") -> bool:
        with self._lock:
            if self.state in {
                FdControlState.CLOSED,
                FdControlState.QUARANTINED,
                FdControlState.CLOSING,
                FdControlState.REVOKING,
            }:
                return False
            self.retire_reason = str(reason or "retired")
            if self.state == FdControlState.OPEN:
                self._transition_locked("REQUEST_RETIRE", actor="control")
            closable = not self._current_capabilities_locked()
            self._condition.notify_all()
        return closable

    def close_sync(self) -> FdCloseOutcome:
        ticket = self._claim_graceful_close(expect_async=False)
        if ticket is None:
            return self._observed_outcome()
        return self._execute_close_sync(ticket)

    async def close_async(self) -> FdCloseOutcome:
        ticket = self._claim_graceful_close(expect_async=True)
        if ticket is None:
            return self._observed_outcome()
        return await self._execute_close_async(ticket)

    def retire_sync(
        self,
        reason: str = "retired",
        *,
        grace_timeout: float = 0.0,
        force_on_timeout: bool = True,
    ) -> FdCloseOutcome:
        closable = self.request_retire(reason)
        if not closable and grace_timeout > 0:
            deadline = time.monotonic() + grace_timeout
            with self._condition:
                while self.state not in {
                    FdControlState.CLOSED,
                    FdControlState.QUARANTINED,
                } and (
                    self.state in {FdControlState.CLOSING, FdControlState.REVOKING}
                    or bool(self._current_capabilities_locked())
                ):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(timeout=remaining)
                closable = not self._current_capabilities_locked()
        if self.state in {FdControlState.CLOSED, FdControlState.QUARANTINED}:
            return self._observed_outcome()
        if closable:
            return self.close_sync()
        if self.state in {FdControlState.CLOSING, FdControlState.REVOKING}:
            return FdCloseOutcome.uncertain("fd close did not settle before timeout")
        if not force_on_timeout:
            return FdCloseOutcome.uncertain("fd capabilities did not drain before timeout")
        return self.force_revoke_sync(reason)

    async def retire_async(
        self,
        reason: str = "retired",
        *,
        grace_timeout: float = 0.0,
        force_on_timeout: bool = True,
    ) -> FdCloseOutcome:
        closable = self.request_retire(reason)
        if not closable and grace_timeout > 0:
            deadline = time.monotonic() + grace_timeout
            while self.state not in {
                FdControlState.CLOSED,
                FdControlState.QUARANTINED,
            } and (
                self.state in {FdControlState.CLOSING, FdControlState.REVOKING}
                or self.active_count > 0
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(0.01, remaining))
            closable = self.active_count == 0
        if self.state in {FdControlState.CLOSED, FdControlState.QUARANTINED}:
            return self._observed_outcome()
        if closable:
            return await self.close_async()
        if self.state in {FdControlState.CLOSING, FdControlState.REVOKING}:
            return FdCloseOutcome.uncertain("fd close did not settle before timeout")
        if not force_on_timeout:
            return FdCloseOutcome.uncertain("fd capabilities did not drain before timeout")
        return await self.force_revoke_async(reason)

    def force_revoke_sync(
        self,
        reason: str = "forced revoke",
        *,
        _authority: "FdCapability[Any] | None" = None,
    ) -> FdCloseOutcome:
        ticket, interrupted = self._claim_forced_close(
            reason=reason,
            expect_async=False,
            authority=_authority,
        )
        self._run_interrupts(interrupted)
        if ticket is None:
            return self._observed_outcome()
        if not self._wait_for_generation_quiescence_sync(ticket.generation):
            return self._finish_close(
                ticket,
                FdCloseOutcome.uncertain(
                    "fd capability calls did not quiesce; physical close was not attempted"
                ),
            )
        return self._execute_close_sync(ticket)

    async def force_revoke_async(
        self,
        reason: str = "forced revoke",
        *,
        _authority: "FdCapability[Any] | None" = None,
    ) -> FdCloseOutcome:
        ticket, interrupted = self._claim_forced_close(
            reason=reason,
            expect_async=True,
            authority=_authority,
        )
        self._run_interrupts(interrupted)
        if ticket is None:
            return self._observed_outcome()
        if not await self._wait_for_generation_quiescence_async(ticket.generation):
            return self._finish_close(
                ticket,
                FdCloseOutcome.uncertain(
                    "fd capability calls did not quiesce; physical close was not attempted"
                ),
            )
        return await self._execute_close_async(ticket)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def _begin_call(self, capability: "FdCapability[Any]") -> T:
        with self._lock:
            self._require_capability_locked(capability)
            capability._require_owner_thread()
            if capability.generation != self.generation:
                raise FdLeaseCancelledError("stale fd capability generation")
            if self.state not in {
                FdControlState.OPEN,
                FdControlState.RETIRING,
            } or capability.state != FdCapabilityState.LIVE:
                raise FdLeaseCancelledError(
                    f"fd capability is not admitted: {capability.cancel_reason or self.state.value}"
                )
            if capability.in_call:
                raise FdLeaseInvariantError("fd capability already has an active call")
            capability.in_call = True
            self._record_locked("BEGIN_CALL", actor=capability.owner_actor, capability=capability)
            return self._require_resource_locked()

    def _end_call(self, capability: "FdCapability[Any]") -> None:
        with self._lock:
            self._require_capability_locked(capability)
            if not capability.in_call:
                raise FdLeaseInvariantError("fd capability call is not active")
            capability.in_call = False
            self._record_locked("END_CALL", actor=capability.owner_actor, capability=capability)
            self._condition.notify_all()

    def _request_cancel(self, capability: "FdCapability[Any]", reason: str) -> bool:
        interrupt: tuple[InterruptHook, T, str] | None = None
        with self._lock:
            try:
                self._require_capability_locked(capability)
            except FdLeaseInvariantError:
                return False
            if capability.state in {FdCapabilityState.TOMBSTONE, FdCapabilityState.RELEASED}:
                return False
            if capability.state == FdCapabilityState.CANCELLED:
                return False
            self._transition_capability_locked(capability, "REQUEST_CANCEL", actor="control")
            capability.cancel_reason = str(reason or "cancelled")
            if capability.interrupt is not None and capability.generation == self.generation:
                interrupt = (
                    capability.interrupt,
                    self._require_resource_locked(),
                    capability.cancel_reason,
                )
        if interrupt is not None:
            self._run_interrupts([interrupt])
        return True

    def _release(
        self,
        capability: "FdCapability[Any]",
        *,
        reuse: bool,
        expect_async: bool,
    ) -> _CloseTicket[T] | None:
        interrupted: list[tuple[InterruptHook, T, str]] = []
        with self._lock:
            self._require_capability_locked(capability)
            capability._require_owner_thread()
            if capability.in_call:
                raise FdLeaseInvariantError("fd capability cannot release during an active call")
            same_generation = capability.generation == self.generation
            effective_reuse = bool(reuse and capability.state == FdCapabilityState.LIVE)
            if same_generation and not effective_reuse and self.state == FdControlState.OPEN:
                self.retire_reason = capability.cancel_reason or "capability retired"
                self._transition_locked("REQUEST_RETIRE", actor=capability.owner_actor)
                resource = self._require_resource_locked()
                for other in self._current_capabilities_locked(exclude=capability):
                    if other.state == FdCapabilityState.LIVE:
                        self._transition_capability_locked(
                            other,
                            "RETIRE_CANCEL",
                            actor=capability.owner_actor,
                        )
                    other.cancel_reason = self.retire_reason
                    if other.interrupt is not None:
                        interrupted.append((other.interrupt, resource, self.retire_reason))
            self._transition_capability_locked(
                capability,
                "RELEASE",
                actor=capability.owner_actor,
            )
            self._capabilities.pop(capability.capability_id)
            self._condition.notify_all()
            ticket: _CloseTicket[T] | None = None
            if (
                same_generation
                and self.state == FdControlState.RETIRING
                and not self._current_capabilities_locked()
            ):
                ticket = self._claim_graceful_close_locked(expect_async=expect_async)
        self._run_interrupts(interrupted)
        return ticket

    def _claim_graceful_close(self, *, expect_async: bool) -> _CloseTicket[T] | None:
        with self._lock:
            if self.state == FdControlState.OPEN:
                self.retire_reason = self.retire_reason or "explicit close"
                self._transition_locked("REQUEST_RETIRE", actor="owner")
            if self.state in {FdControlState.CLOSED, FdControlState.QUARANTINED}:
                return None
            if self.state in {FdControlState.CLOSING, FdControlState.REVOKING}:
                return None
            if self._current_capabilities_locked():
                raise FdLeaseInvariantError("fd resource cannot close while capabilities remain")
            return self._claim_graceful_close_locked(expect_async=expect_async)

    def _claim_graceful_close_locked(self, *, expect_async: bool) -> _CloseTicket[T]:
        if self.state != FdControlState.RETIRING:
            raise FdLeaseInvariantError(f"fd resource cannot close from {self.state.value}")
        self._validate_close_mode(expect_async=expect_async, forced=False)
        self._claim_close_once_locked("CLAIM_GRACEFUL_CLOSE")
        return _CloseTicket(
            generation=self.generation,
            resource=self._require_resource_locked(),
            action="graceful",
            close_sync=self.closer_sync,
            close_async=self.closer_async,
        )

    def _claim_forced_close(
        self,
        *,
        reason: str,
        expect_async: bool,
        authority: "FdCapability[Any] | None",
    ) -> tuple[_CloseTicket[T] | None, list[tuple[InterruptHook, T, str]]]:
        interrupted: list[tuple[InterruptHook, T, str]] = []
        with self._lock:
            if authority is not None:
                self._require_capability_locked(authority)
                if authority.generation != self.generation:
                    raise FdLeaseCancelledError(
                        "stale fd capability cannot revoke a newer generation"
                    )
            if self.state == FdControlState.OPEN:
                self.retire_reason = str(reason or "forced revoke")
                self._transition_locked("REQUEST_RETIRE", actor="control")
            if self.state in {
                FdControlState.CLOSED,
                FdControlState.QUARANTINED,
                FdControlState.CLOSING,
                FdControlState.REVOKING,
            }:
                return None, interrupted
            if self.state != FdControlState.RETIRING:
                raise FdLeaseInvariantError(f"fd resource cannot revoke from {self.state.value}")
            self._validate_close_mode(expect_async=expect_async, forced=True)
            resource = self._require_resource_locked()
            for capability in self._current_capabilities_locked():
                self._transition_capability_locked(
                    capability,
                    "FORCE_REVOKE",
                    actor="control",
                )
                capability.cancel_reason = str(reason or "forced revoke")
                if capability.interrupt is not None:
                    interrupted.append((capability.interrupt, resource, capability.cancel_reason))
            self._claim_close_once_locked("FORCE_REVOKE")
            ticket = _CloseTicket(
                generation=self.generation,
                resource=resource,
                action="forced",
                close_sync=self.hard_closer_sync,
                close_async=self.hard_closer_async,
            )
            return ticket, interrupted

    def _claim_close_once_locked(self, action: str) -> None:
        if self._close_claimed_generation == self.generation:
            raise FdLeaseInvariantError("fd close authority was already claimed")
        attempts = self._close_attempts.get(self.generation, 0)
        if attempts != 0:
            raise FdLeaseInvariantError("fd close may not be retried")
        self._close_claimed_generation = self.generation
        self._close_attempts[self.generation] = attempts + 1
        self._transition_locked(action, actor="closer")

    def _execute_close_sync(self, ticket: _CloseTicket[T]) -> FdCloseOutcome:
        try:
            if ticket.close_sync is None:
                contract = "hard-close" if ticket.action == "forced" else "close"
                outcome = FdCloseOutcome.uncertain(
                    f"fd resource has no {contract} contract"
                )
            else:
                raw = ticket.close_sync(ticket.resource)
                outcome = _normalize_close_outcome(raw)
        except BaseException as exc:
            outcome = FdCloseOutcome.uncertain(f"{type(exc).__name__}: {exc}")
        return self._complete_close_sync(ticket, outcome)

    async def _execute_close_async(self, ticket: _CloseTicket[T]) -> FdCloseOutcome:
        try:
            if ticket.close_async is None:
                contract = "hard-close" if ticket.action == "forced" else "close"
                outcome = FdCloseOutcome.uncertain(
                    f"fd resource has no {contract} contract"
                )
            else:
                raw = await ticket.close_async(ticket.resource)
                outcome = _normalize_close_outcome(raw)
        except BaseException as exc:
            outcome = FdCloseOutcome.uncertain(f"{type(exc).__name__}: {exc}")
        return await self._complete_close_async(ticket, outcome)

    def _complete_close_sync(
        self,
        ticket: _CloseTicket[T],
        outcome: FdCloseOutcome,
    ) -> FdCloseOutcome:
        return self._finish_close(ticket, outcome)

    async def _complete_close_async(
        self,
        ticket: _CloseTicket[T],
        outcome: FdCloseOutcome,
    ) -> FdCloseOutcome:
        return self._finish_close(ticket, outcome)

    def _wait_for_generation_quiescence_sync(self, generation: int) -> bool:
        deadline = time.monotonic() + self.close_drain_timeout
        with self._condition:
            while self._generation_has_inflight_calls_locked(generation):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return True

    async def _wait_for_generation_quiescence_async(self, generation: int) -> bool:
        deadline = time.monotonic() + self.close_drain_timeout
        while True:
            with self._lock:
                if not self._generation_has_inflight_calls_locked(generation):
                    return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.01, remaining))

    def _finish_close(
        self,
        ticket: _CloseTicket[T],
        outcome: FdCloseOutcome,
    ) -> FdCloseOutcome:
        with self._lock:
            if ticket.generation != self.generation:
                raise FdLeaseInvariantError("fd close completed for a stale generation")
            expected_state = (
                FdControlState.REVOKING if ticket.action == "forced" else FdControlState.CLOSING
            )
            if self.state != expected_state:
                raise FdLeaseInvariantError("fd close completion has no authority")
            self.close_outcome = outcome
            self.close_error = "" if outcome.is_detached else outcome.detail
            action = "CLOSE_DETACHED" if outcome.is_detached else "CLOSE_UNCERTAIN"
            if outcome.is_detached:
                self._resource = None  # type: ignore[assignment]
            self._transition_locked(action, actor="closer")
            terminal_generation = self.generation
            terminal_state = self.state
            terminal_snapshot = self._snapshot_locked()
            self._condition.notify_all()
        FD_LEASES.terminal(
            self,
            generation=terminal_generation,
            state=terminal_state,
            snapshot=terminal_snapshot,
        )
        return outcome

    def _observed_outcome(self) -> FdCloseOutcome:
        with self._lock:
            if self.close_outcome is not None:
                return self.close_outcome
            if self.state == FdControlState.CLOSED:
                return FdCloseOutcome.detached()
            return FdCloseOutcome.uncertain(f"fd close is {self.state.value.lower()}")

    def _capacity_available_locked(self) -> bool:
        return self.capacity is None or len(self._current_capabilities_locked()) < self.capacity

    def _snapshot_locked(self) -> dict[str, Any]:
        return {
            "owner_id": self.owner_id,
            "resource_kind": self.resource_kind,
            "generation": self.generation,
            "state": self.state.value,
            "capacity": self.capacity,
            "reference_count": 1 + len(self._capabilities),
            "active_count": len(self._current_capabilities_locked()),
            "capabilities": [
                capability.snapshot() for capability in self._capabilities.values()
            ],
            "retire_reason": self.retire_reason,
            "close_error": self.close_error,
            "close_outcome": (
                self.close_outcome.disposition.value
                if self.close_outcome is not None
                else ""
            ),
            "close_detail": (
                self.close_outcome.detail if self.close_outcome is not None else ""
            ),
            "close_attempts": self._close_attempts.get(self.generation, 0),
            "created_at": self.created_at,
            "trace": list(self._trace),
        }

    def _generation_has_inflight_calls_locked(self, generation: int) -> bool:
        return any(
            capability.generation == generation and capability.in_call
            for capability in self._capabilities.values()
        )

    def _current_capabilities_locked(
        self,
        *,
        exclude: "FdCapability[Any] | None" = None,
    ) -> list["FdCapability[Any]"]:
        return [
            capability
            for capability in self._capabilities.values()
            if capability is not exclude
            and capability.generation == self.generation
            and capability.state != FdCapabilityState.RELEASED
        ]

    def _require_capability_locked(self, capability: "FdCapability[Any]") -> None:
        if self._capabilities.get(capability.capability_id) is not capability:
            raise FdLeaseInvariantError("stale or released fd capability")

    def _require_resource_locked(self) -> T:
        if self._resource is None:
            raise FdLeaseInvariantError("fd resource is detached")
        return self._resource

    def _validate_close_mode(self, *, expect_async: bool, forced: bool) -> None:
        sync = self.hard_closer_sync if forced else self.closer_sync
        async_ = self.hard_closer_async if forced else self.closer_async
        if expect_async and sync is not None:
            raise FdLeaseInvariantError("sync fd closer used from async lifecycle")
        if not expect_async and async_ is not None:
            raise FdLeaseInvariantError("async fd closer used from sync lifecycle")

    @staticmethod
    def _validate_resource_configuration(
        resource: Any,
        close_sync: SyncCloser | None,
        close_async: AsyncCloser | None,
        hard_close_sync: SyncCloser | None,
        hard_close_async: AsyncCloser | None,
    ) -> None:
        if resource is None:
            raise ValueError("fd lease requires a root resource")
        if close_sync is not None and close_async is not None:
            raise ValueError("fd lease must use either sync or async close")
        if hard_close_sync is not None and hard_close_async is not None:
            raise ValueError("fd lease must use either sync or async hard close")
        if close_sync is not None and hard_close_async is not None:
            raise ValueError("sync fd close cannot use async hard close")
        if close_async is not None and hard_close_sync is not None:
            raise ValueError("async fd close cannot use sync hard close")

    def _transition_locked(self, action: str, *, actor: str) -> None:
        target = _CONTROL_TRANSITION_INDEX.get((self.state, action))
        if target is None:
            raise FdLeaseInvariantError(
                f"illegal fd control transition: {self.state.value} + {action}"
            )
        self.state = target
        self._record_locked(action, actor=actor)

    def _transition_capability_locked(
        self,
        capability: "FdCapability[Any]",
        action: str,
        *,
        actor: str,
    ) -> None:
        target = _CAPABILITY_TRANSITION_INDEX.get((capability.state, action))
        if target is None:
            raise FdLeaseInvariantError(
                "illegal fd capability transition: "
                f"{capability.state.value} + {action}"
            )
        capability.state = target
        self._record_locked(action, actor=actor, capability=capability)

    def _record(self, action: str, *, actor: str) -> None:
        with self._lock:
            self._record_locked(action, actor=actor)

    def _record_locked(
        self,
        action: str,
        *,
        actor: str,
        capability: "FdCapability[Any] | None" = None,
    ) -> None:
        self._trace.append(
            {
                "at": time.monotonic(),
                "action": str(action),
                "state": self.state.value,
                "generation": self.generation,
                "actor": str(actor),
                "capability_id": capability.capability_id if capability is not None else 0,
            }
        )

    @staticmethod
    def _run_interrupts(interrupted: list[tuple[InterruptHook, T, str]]) -> None:
        for interrupt, resource, reason in interrupted:
            try:
                interrupt(resource, reason)
            except BaseException:
                # Interrupt has no close authority. The cancellation/revocation
                # fence remains authoritative even when a wakeup fails.
                pass


@dataclass
class FdCapability(Generic[T]):
    _owner: FdLease[T] = field(repr=False)
    capability_id: int
    generation: int
    operation_id: str
    owner_thread_id: int
    interrupt: InterruptHook | None = field(default=None, repr=False)
    state: FdCapabilityState = field(default=FdCapabilityState.LIVE, init=False)
    cancel_reason: str = field(default="", init=False)
    in_call: bool = field(default=False, init=False)

    @property
    def owner_actor(self) -> str:
        return f"thread:{self.owner_thread_id}:capability:{self.capability_id}"

    def call_sync(self, operation: SyncOperation[T, R]) -> R:
        resource = self._owner._begin_call(self)
        try:
            result = operation(resource)
            if result is resource:
                raise FdLeaseInvariantError(
                    "fd capability operations may return detached value data only"
                )
            _require_detached_value(result, _forbidden_ids={id(resource)})
            return result
        finally:
            self._owner._end_call(self)

    async def call_async(self, operation: AsyncOperation[T, R]) -> R:
        resource = self._owner._begin_call(self)
        try:
            result = await operation(resource)
            if result is resource:
                raise FdLeaseInvariantError(
                    "fd capability operations may return detached value data only"
                )
            _require_detached_value(result, _forbidden_ids={id(resource)})
            return result
        finally:
            self._owner._end_call(self)

    def request_cancel(self, reason: str = "cancelled") -> bool:
        return self._owner._request_cancel(self, reason)

    def force_revoke_sync(self, reason: str = "forced revoke") -> FdCloseOutcome:
        return self._owner.force_revoke_sync(reason, _authority=self)

    async def force_revoke_async(self, reason: str = "forced revoke") -> FdCloseOutcome:
        return await self._owner.force_revoke_async(reason, _authority=self)

    def raise_if_cancelled(self) -> None:
        if self.state == FdCapabilityState.LIVE:
            return
        raise FdLeaseCancelledError(
            f"fd capability unavailable: {self.cancel_reason or self.state.value.lower()}"
        )

    def release_sync(self, *, reuse: bool) -> FdCloseOutcome | None:
        ticket = self._owner._release(self, reuse=reuse, expect_async=False)
        if ticket is None:
            return None
        return self._owner._execute_close_sync(ticket)

    async def release_async(self, *, reuse: bool) -> FdCloseOutcome | None:
        ticket = self._owner._release(self, reuse=reuse, expect_async=True)
        if ticket is None:
            return None
        return await self._owner._execute_close_async(ticket)

    def snapshot(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "generation": self.generation,
            "operation_id": self.operation_id,
            "state": self.state.value,
            "owner_thread_id": self.owner_thread_id,
            "in_call": self.in_call,
            "cancel_reason": self.cancel_reason,
        }

    def _require_owner_thread(self) -> None:
        current = threading.get_ident()
        if current != self.owner_thread_id:
            raise FdLeaseInvariantError(
                "fd capability operation must run on its owner thread: "
                f"owner={self.owner_thread_id} actor={current}"
            )


@dataclass
class FdCancellationControl:
    """Cross-thread cancellation authority without resource or close authority."""

    started_at: float = field(default_factory=time.monotonic)
    _cancelled: bool = field(default=False, init=False, repr=False)
    _reason: str = field(default="", init=False, repr=False)
    _capability: FdCapability[Any] | None = field(default=None, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def cancel_reason(self) -> str:
        with self._lock:
            return self._reason

    def bind(self, capability: FdCapability[Any]) -> None:
        with self._lock:
            if self._capability is not None:
                raise FdLeaseInvariantError("fd cancellation control is already bound")
            self._capability = capability
            cancelled = self._cancelled
            reason = self._reason
        if cancelled:
            capability.request_cancel(reason)

    def cancel(self, reason: str = "cancelled") -> None:
        with self._lock:
            if not self._cancelled:
                self._cancelled = True
                self._reason = str(reason or "cancelled")
            capability = self._capability
            current_reason = self._reason
        if capability is not None:
            capability.request_cancel(current_reason)

    def raise_if_cancelled(self) -> None:
        if not self.cancelled:
            return
        raise FdLeaseCancelledError(
            f"fd capability cancelled: {self.cancel_reason or 'cancelled'}"
        )

    def force_revoke_sync(self, reason: str = "forced revoke") -> FdCloseOutcome | None:
        with self._lock:
            capability = self._capability
        if capability is None:
            return None
        try:
            return capability.force_revoke_sync(reason)
        except (FdLeaseCancelledError, FdLeaseInvariantError):
            return None

    async def force_revoke_async(
        self,
        reason: str = "forced revoke",
    ) -> FdCloseOutcome | None:
        with self._lock:
            capability = self._capability
        if capability is None:
            return None
        try:
            return await capability.force_revoke_async(reason)
        except (FdLeaseCancelledError, FdLeaseInvariantError):
            return None

    def unbind(self, capability: FdCapability[Any]) -> None:
        with self._lock:
            if self._capability is capability:
                self._capability = None


def _normalize_close_outcome(value: FdCloseOutcome | None) -> FdCloseOutcome:
    if value is None:
        return FdCloseOutcome.uncertain(
            "fd closer returned no explicit detachment proof"
        )
    if not isinstance(value, FdCloseOutcome):
        raise FdLeaseInvariantError("fd closer returned an invalid outcome")
    return value


def fd_lease_snapshot(*, resource_kind: str | None = None) -> dict[str, Any]:
    return FD_LEASES.snapshot(resource_kind=resource_kind)


_DETACHED_ATOMS = (
    type(None),
    bool,
    int,
    float,
    complex,
    str,
    bytes,
    bytearray,
    Decimal,
    UUID,
    PurePath,
    date,
    datetime,
    datetime_time,
    Enum,
)


def _require_detached_value(
    value: Any,
    *,
    _seen: set[int] | None = None,
    _forbidden_ids: set[int] | None = None,
) -> None:
    """Reject capability results that can retain an fd-backed object graph.

    Operations may return ordinary value data only.  This deliberately uses a
    small allow-list instead of trying to discover every possible fd hidden in
    an arbitrary Python object.  Containers and dataclasses are checked
    recursively, so a socket/process cannot be smuggled out inside a wrapper or
    closure after the admitted call has ended.
    """

    forbidden_ids = _forbidden_ids if _forbidden_ids is not None else set()
    if id(value) in forbidden_ids:
        raise FdLeaseInvariantError(
            "fd capability operations may return detached value data only"
        )
    if isinstance(value, _DETACHED_ATOMS):
        return
    seen = _seen if _seen is not None else set()
    identity = id(value)
    if identity in seen:
        return
    seen.add(identity)
    if type(value) in {dict, MappingProxyType}:
        for key, item in value.items():
            _require_detached_value(key, _seen=seen, _forbidden_ids=forbidden_ids)
            _require_detached_value(item, _seen=seen, _forbidden_ids=forbidden_ids)
        return
    if type(value) in {list, tuple, set, frozenset}:
        for item in value:
            _require_detached_value(item, _seen=seen, _forbidden_ids=forbidden_ids)
        return
    if is_dataclass(value) and not isinstance(value, type):
        for item in fields(value):
            _require_detached_value(
                getattr(value, item.name),
                _seen=seen,
                _forbidden_ids=forbidden_ids,
            )
        return
    raise FdLeaseInvariantError(
        "fd capability operations may return detached value data only; "
        f"got {type(value).__name__}"
    )


__all__ = [
    "FD_CAPABILITY_TRANSITIONS",
    "FD_CONTROL_TRANSITIONS",
    "FD_LEASES",
    "FdCancellationControl",
    "FdCapability",
    "FdCapabilityState",
    "FdCloseDisposition",
    "FdCloseOutcome",
    "FdControlState",
    "FdLease",
    "FdLeaseCancelledError",
    "FdLeaseInvariantError",
    "FdLeaseRegistry",
    "fd_lease_snapshot",
]
