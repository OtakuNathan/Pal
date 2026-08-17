from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass

import pytest

from pal.foundation.fd_lease import (
    FD_LEASES,
    FdCancellationControl,
    FdCapabilityState,
    FdCloseOutcome,
    FdControlState,
    FdLease,
    FdLeaseCancelledError,
    FdLeaseInvariantError,
)
from pal.foundation.fd_lease_formal import render_fd_lease_implementation_topology


@dataclass
class _Resource:
    name: str = "resource"
    closed: bool = False


def _close(resource: _Resource) -> FdCloseOutcome:
    resource.closed = True
    return FdCloseOutcome.detached()


def test_generated_fd_lease_topology_matches_transition_tables() -> None:
    with open("spec/foundation/FdLeaseImplementationTopology.tla", encoding="utf-8") as handle:
        assert handle.read() == render_fd_lease_implementation_topology()


def test_capability_is_opaque_and_cannot_export_root() -> None:
    owner = FdLease("test.opaque", _Resource(), closer_sync=_close, hard_closer_sync=_close)
    capability = owner.acquire(operation_id="probe")

    assert not hasattr(capability, "resource")
    assert not hasattr(capability, "fileno")
    assert capability.call_sync(lambda resource: resource.name) == "resource"
    with pytest.raises(FdLeaseInvariantError, match="detached value data only"):
        capability.call_sync(lambda resource: resource)

    outcome = capability.release_sync(reuse=False)
    assert outcome is not None and outcome.is_detached


def test_capability_cannot_export_nested_resource_or_capturing_closure() -> None:
    @dataclass
    class Graph:
        child: object

    child = object()

    def close(_resource: Graph) -> FdCloseOutcome:
        return FdCloseOutcome.detached()

    owner = FdLease("test.nested-opaque", Graph(child), closer_sync=close)
    capability = owner.acquire(operation_id="probe")

    with pytest.raises(FdLeaseInvariantError, match="detached value data only"):
        capability.call_sync(lambda resource: resource.child)
    with pytest.raises(FdLeaseInvariantError, match="detached value data only"):
        capability.call_sync(lambda resource: lambda: resource)

    outcome = capability.release_sync(reuse=False)
    assert outcome is not None and outcome.is_detached


def test_capacity_and_clean_generation_republish() -> None:
    first_resource = _Resource("first")
    owner = FdLease(
        "test.generation",
        first_resource,
        capacity=1,
        closer_sync=_close,
        hard_closer_sync=_close,
    )
    first = owner.acquire(operation_id="first")
    with pytest.raises(FdLeaseInvariantError, match="cannot acquire"):
        owner.acquire(operation_id="over-capacity")
    assert first.call_sync(lambda resource: resource.name) == "first"
    assert first.release_sync(reuse=False).is_detached  # type: ignore[union-attr]

    assert owner.publish(
        _Resource("second"),
        closer_sync=_close,
        hard_closer_sync=_close,
    ) == 2
    second = owner.acquire(operation_id="second")
    assert second.call_sync(lambda resource: resource.name) == "second"
    assert second.release_sync(reuse=False).is_detached  # type: ignore[union-attr]


def test_cancel_is_signal_only_until_capability_owner_releases() -> None:
    resource = _Resource()
    interrupted: list[str] = []
    owner = FdLease("test.cancel", resource, closer_sync=_close, hard_closer_sync=_close)
    capability = owner.acquire(
        operation_id="request",
        interrupt=lambda held, reason: interrupted.append(f"{held.name}:{reason}"),
    )
    control = FdCancellationControl()
    control.bind(capability)

    thread = threading.Thread(target=control.cancel, args=("stop",))
    thread.start()
    thread.join()

    assert interrupted == ["resource:stop"]
    assert owner.state == FdControlState.OPEN
    assert not resource.closed
    with pytest.raises(FdLeaseCancelledError):
        capability.call_sync(lambda held: held.name)
    outcome = capability.release_sync(reuse=True)
    assert outcome is not None and outcome.is_detached


def test_retire_closes_only_after_last_current_generation_capability() -> None:
    closed: list[str] = []

    def close(resource: _Resource) -> FdCloseOutcome:
        resource.closed = True
        closed.append(resource.name)
        return FdCloseOutcome.detached()

    owner = FdLease("test.drain", _Resource("a"), capacity=2, closer_sync=close)
    first = owner.acquire(operation_id="first")
    second = owner.acquire(operation_id="second")

    assert not owner.request_retire("shutdown")
    assert first.state == FdCapabilityState.LIVE
    assert first.call_sync(lambda resource: resource.name) == "a"
    assert first.release_sync(reuse=True) is None
    assert closed == []
    outcome = second.release_sync(reuse=True)
    assert outcome is not None and outcome.is_detached
    assert closed == ["a"]


def test_force_revoke_detaches_then_allows_b_while_old_capability_is_tombstone() -> None:
    old = _Resource("a")
    owner = FdLease(
        "test.force",
        old,
        closer_sync=_close,
        hard_closer_sync=_close,
    )
    stale = owner.acquire(operation_id="old-holder")

    outcome = owner.retire_sync("timeout", force_on_timeout=True)
    assert outcome.is_detached
    assert stale.state == FdCapabilityState.TOMBSTONE
    assert old.closed

    owner.publish(_Resource("b"), closer_sync=_close, hard_closer_sync=_close)
    with pytest.raises(FdLeaseCancelledError):
        stale.call_sync(lambda resource: resource.name)
    stale.release_sync(reuse=True)

    current = owner.acquire(operation_id="new-holder")
    assert current.call_sync(lambda resource: resource.name) == "b"
    assert current.release_sync(reuse=False).is_detached  # type: ignore[union-attr]


def test_forced_close_waits_for_old_call_to_quiesce_before_slot_reuse() -> None:
    entered = threading.Event()
    resume = threading.Event()
    observed: list[str] = []
    worker_errors: list[BaseException] = []
    hard_close_started = threading.Event()

    def hard_close(resource: _Resource) -> FdCloseOutcome:
        hard_close_started.set()
        return _close(resource)

    owner = FdLease(
        "test.inflight",
        _Resource("a"),
        closer_sync=_close,
        hard_closer_sync=hard_close,
    )

    def worker() -> None:
        capability = owner.acquire(operation_id="old-call")
        try:
            def blocked(resource: _Resource) -> str:
                entered.set()
                assert resume.wait(timeout=2.0)
                return resource.name

            observed.append(capability.call_sync(blocked))
            with pytest.raises(FdLeaseCancelledError):
                capability.call_sync(lambda resource: resource.name)
        except BaseException as exc:
            worker_errors.append(exc)
        finally:
            capability.release_sync(reuse=True)

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(timeout=1.0)
    close_outcomes: list[FdCloseOutcome] = []
    closer = threading.Thread(
        target=lambda: close_outcomes.append(owner.force_revoke_sync("forced-timeout"))
    )
    closer.start()
    deadline = time.monotonic() + 1.0
    while owner.state != FdControlState.REVOKING and time.monotonic() < deadline:
        time.sleep(0.001)
    assert owner.state == FdControlState.REVOKING
    assert not hard_close_started.wait(timeout=0.05)
    with pytest.raises(FdLeaseInvariantError, match="cannot publish"):
        owner.publish(_Resource("too-early"), closer_sync=_close, hard_closer_sync=_close)
    resume.set()
    assert hard_close_started.wait(timeout=1.0)
    thread.join(timeout=2.0)
    closer.join(timeout=2.0)

    assert not thread.is_alive()
    assert not closer.is_alive()
    assert worker_errors == []
    assert observed == ["a"]
    assert close_outcomes and close_outcomes[0].is_detached
    owner.publish(_Resource("b"), closer_sync=_close, hard_closer_sync=_close)
    current = owner.acquire(operation_id="new-call")
    assert current.call_sync(lambda resource: resource.name) == "b"
    current.release_sync(reuse=False)


def test_forced_close_timeout_quarantines_without_physically_closing() -> None:
    entered = threading.Event()
    resume = threading.Event()
    hard_close_calls = 0
    worker_errors: list[BaseException] = []
    resource = _Resource("still-hazardous")

    def hard_close(held: _Resource) -> FdCloseOutcome:
        nonlocal hard_close_calls
        hard_close_calls += 1
        return _close(held)

    owner = FdLease(
        "test.inflight-timeout",
        resource,
        closer_sync=_close,
        hard_closer_sync=hard_close,
        close_drain_timeout=0.02,
    )

    def worker() -> None:
        capability = owner.acquire(operation_id="stuck-call")
        try:
            capability.call_sync(
                lambda held: (entered.set(), resume.wait(timeout=2.0), held.name)[-1]
            )
        except BaseException as exc:
            worker_errors.append(exc)
        finally:
            capability.release_sync(reuse=True)

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(timeout=1.0)

    outcome = owner.force_revoke_sync("bounded-revoke")

    assert not outcome.is_detached
    assert "physical close was not attempted" in outcome.detail
    assert owner.quarantined
    assert hard_close_calls == 0
    assert not resource.closed
    with pytest.raises(FdLeaseInvariantError, match="cannot publish"):
        owner.publish(_Resource("b"), closer_sync=_close, hard_closer_sync=_close)

    resume.set()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert worker_errors == []
    assert not resource.closed


def test_closer_without_explicit_detachment_proof_quarantines() -> None:
    def close_without_proof(resource: _Resource) -> None:
        resource.closed = True

    owner = FdLease(
        "test.explicit-proof",
        _Resource(),
        closer_sync=close_without_proof,  # type: ignore[arg-type]
    )

    outcome = owner.close_sync()

    assert not outcome.is_detached
    assert "no explicit detachment proof" in outcome.detail
    assert owner.quarantined


def test_forced_close_without_hard_close_contract_quarantines() -> None:
    owner = FdLease("test.no-hard-close", _Resource(), closer_sync=_close)
    capability = owner.acquire(operation_id="request")

    outcome = owner.force_revoke_sync("watchdog")

    assert not outcome.is_detached
    assert "no hard-close contract" in outcome.detail
    assert owner.quarantined
    capability.release_sync(reuse=True)


def test_uncertain_close_quarantines_blocks_publish_and_is_never_retried() -> None:
    close_calls = 0

    def uncertain(_resource: _Resource) -> FdCloseOutcome:
        nonlocal close_calls
        close_calls += 1
        return FdCloseOutcome.uncertain("driver could not prove detach")

    owner = FdLease(
        "test.uncertain",
        _Resource(),
        closer_sync=uncertain,
        hard_closer_sync=uncertain,
    )
    capability = owner.acquire(operation_id="request")
    outcome = capability.release_sync(reuse=False)

    assert outcome is not None and not outcome.is_detached
    assert owner.quarantined
    assert any(
        item["owner_id"] == owner.owner_id
        for item in FD_LEASES.snapshot()["active"]
    )
    assert close_calls == 1
    assert not owner.close_sync().is_detached
    assert close_calls == 1
    with pytest.raises(FdLeaseInvariantError, match="cannot publish"):
        owner.publish(_Resource(), closer_sync=_close, hard_closer_sync=_close)


def test_close_exception_is_uncertain_and_never_retried() -> None:
    close_calls = 0

    def fail(_resource: _Resource) -> FdCloseOutcome:
        nonlocal close_calls
        close_calls += 1
        raise OSError("close failed after kernel detach may have begun")

    owner = FdLease("test.close-error", _Resource(), closer_sync=fail)
    assert owner.request_retire("shutdown")
    outcome = owner.close_sync()
    assert not outcome.is_detached
    assert owner.state == FdControlState.QUARANTINED
    assert close_calls == 1
    owner.close_sync()
    assert close_calls == 1


def test_concurrent_close_has_one_physical_close_authority() -> None:
    close_started = threading.Event()
    release_close = threading.Event()
    calls = 0

    def close(_resource: _Resource) -> FdCloseOutcome:
        nonlocal calls
        calls += 1
        close_started.set()
        assert release_close.wait(timeout=2.0)
        return FdCloseOutcome.detached()

    owner = FdLease("test.concurrent-close", _Resource(), closer_sync=close)
    assert owner.request_retire("shutdown")
    outcomes: list[FdCloseOutcome] = []
    thread = threading.Thread(target=lambda: outcomes.append(owner.close_sync()))
    thread.start()
    assert close_started.wait(timeout=1.0)
    observed = owner.close_sync()
    release_close.set()
    thread.join(timeout=2.0)

    assert calls == 1
    assert not observed.is_detached
    assert outcomes and outcomes[0].is_detached
    assert owner.closed


def test_cross_thread_use_is_rejected_but_cross_thread_revoke_is_allowed() -> None:
    owner = FdLease(
        "test.thread",
        _Resource(),
        closer_sync=_close,
        hard_closer_sync=_close,
    )
    capability = owner.acquire(operation_id="request")
    failures: list[BaseException] = []

    def use_elsewhere() -> None:
        try:
            capability.call_sync(lambda resource: resource.name)
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=use_elsewhere)
    thread.start()
    thread.join()
    assert len(failures) == 1
    assert isinstance(failures[0], FdLeaseInvariantError)
    assert owner.force_revoke_sync("watchdog").is_detached
    capability.release_sync(reuse=True)


def test_stale_capability_cannot_revoke_republished_generation() -> None:
    owner = FdLease(
        "test.stale-revoke",
        _Resource("a"),
        closer_sync=_close,
        hard_closer_sync=_close,
    )
    stale = owner.acquire(operation_id="old")
    control = FdCancellationControl()
    control.bind(stale)
    assert stale.force_revoke_sync("retire-a").is_detached

    owner.publish(_Resource("b"), closer_sync=_close, hard_closer_sync=_close)
    with pytest.raises(FdLeaseCancelledError, match="newer generation"):
        stale.force_revoke_sync("must-not-touch-b")
    assert control.force_revoke_sync("also-must-not-touch-b") is None
    assert owner.state == FdControlState.OPEN
    current = owner.acquire(operation_id="current")
    assert current.call_sync(lambda resource: resource.name) == "b"
    stale.release_sync(reuse=True)
    current.release_sync(reuse=False)


def test_retire_waits_for_an_already_claimed_close() -> None:
    close_started = threading.Event()
    release_close = threading.Event()

    def close(_resource: _Resource) -> FdCloseOutcome:
        close_started.set()
        assert release_close.wait(timeout=2.0)
        return FdCloseOutcome.detached()

    owner = FdLease("test.retire-close-race", _Resource(), closer_sync=close)
    assert owner.request_retire("shutdown")
    closer = threading.Thread(target=owner.close_sync)
    closer.start()
    assert close_started.wait(timeout=1.0)
    release_close.set()

    outcome = owner.retire_sync("join-close", grace_timeout=1.0)

    closer.join(timeout=1.0)
    assert outcome.is_detached
    assert owner.closed


def test_old_generation_registry_settlement_cannot_unregister_republished_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settlement_entered = threading.Event()
    resume_settlement = threading.Event()
    original_terminal = FD_LEASES.terminal

    def delayed_terminal(*args: object, **kwargs: object) -> None:
        settlement_entered.set()
        assert resume_settlement.wait(timeout=2.0)
        original_terminal(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(FD_LEASES, "terminal", delayed_terminal)
    owner = FdLease("test.registry-generation", _Resource("a"), closer_sync=_close)
    assert owner.request_retire("replace")
    closer = threading.Thread(target=owner.close_sync)
    closer.start()
    assert settlement_entered.wait(timeout=1.0)

    owner.publish(_Resource("b"), closer_sync=_close)
    resume_settlement.set()
    closer.join(timeout=1.0)

    active = {
        item["owner_id"]: item
        for item in FD_LEASES.snapshot()["active"]
    }
    assert active[owner.owner_id]["generation"] == 2
    current = owner.acquire(operation_id="cleanup")
    current.release_sync(reuse=False)


def test_async_capability_and_close_follow_same_state_machine() -> None:
    events: list[str] = []

    async def close(resource: _Resource) -> FdCloseOutcome:
        await asyncio.sleep(0)
        resource.closed = True
        events.append("close")
        return FdCloseOutcome.detached()

    async def scenario() -> None:
        owner = FdLease(
            "test.async",
            _Resource(),
            closer_async=close,
            hard_closer_async=close,
        )
        capability = owner.acquire(operation_id="request")

        async def read(resource: _Resource) -> str:
            await asyncio.sleep(0)
            return resource.name

        assert await capability.call_async(read) == "resource"
        outcome = await capability.release_async(reuse=False)
        assert outcome is not None and outcome.is_detached
        assert owner.closed

    asyncio.run(scenario())
    assert events == ["close"]
