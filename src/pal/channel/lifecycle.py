from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from itertools import product


class EndpointHubInvariantError(RuntimeError):
    """An endpoint hub lifecycle operation violated the channel contract."""


class EndpointHubState(StrEnum):
    ABSENT = "absent"
    DISCOVERED = "discovered"
    TRANSITIONING = "transitioning"
    DRAINING = "draining"
    ATTACHED = "attached"
    DETACHED = "detached"
    DEGRADED = "degraded"
    REMOVING = "removing"


class EndpointHubAction(StrEnum):
    DISCOVER = "DISCOVER"
    REQUEST_PUBLISH = "REQUEST_PUBLISH"
    WITHDRAW = "WITHDRAW"
    BEGIN_TRANSITION = "BEGIN_TRANSITION"
    REGISTER_TRANSPORT = "REGISTER_TRANSPORT"
    BEGIN_DRAIN = "BEGIN_DRAIN"
    DRAIN_COMPLETE = "DRAIN_COMPLETE"
    TRANSITION_FAILED = "TRANSITION_FAILED"
    DETACH_COMPLETE = "DETACH_COMPLETE"
    ROLLBACK_TRANSPORT = "ROLLBACK_TRANSPORT"
    TRANSPORT_REMOVED = "TRANSPORT_REMOVED"
    BEGIN_REMOVE = "BEGIN_REMOVE"
    REMOVE_COMPLETE = "REMOVE_COMPLETE"


@dataclass(frozen=True)
class EndpointHubSnapshot:
    """The complete state controlled by the endpoint-hub reducer."""

    state: EndpointHubState
    physical_present: bool
    transport_present: bool
    published: bool
    publish_when_ready: bool


ABSENT_ENDPOINT_HUB_SNAPSHOT = EndpointHubSnapshot(
    state=EndpointHubState.ABSENT,
    physical_present=False,
    transport_present=False,
    published=False,
    publish_when_ready=False,
)


def validate_endpoint_hub_snapshot(
    snapshot: EndpointHubSnapshot,
    *,
    buffer_empty: bool,
) -> None:
    state = snapshot.state
    if state == EndpointHubState.ABSENT:
        if snapshot != ABSENT_ENDPOINT_HUB_SNAPSHOT or not buffer_empty:
            raise EndpointHubInvariantError("absent endpoint hub still owns resources")
        return
    if state == EndpointHubState.REMOVING:
        if (
            snapshot.physical_present
            or snapshot.transport_present
            or snapshot.published
            or snapshot.publish_when_ready
        ):
            raise EndpointHubInvariantError("removing endpoint hub still owns a registry resource")
        return
    if not snapshot.physical_present:
        raise EndpointHubInvariantError("live endpoint hub lost its physical provider")
    if snapshot.transport_present and state in {
        EndpointHubState.DISCOVERED,
        EndpointHubState.DETACHED,
    }:
        raise EndpointHubInvariantError(f"{state.value} endpoint hub still owns a transport")
    if not snapshot.transport_present and state in {
        EndpointHubState.ATTACHED,
        EndpointHubState.DRAINING,
    }:
        raise EndpointHubInvariantError(f"{state.value} endpoint hub has no transport")
    if snapshot.published:
        if (
            state != EndpointHubState.ATTACHED
            or not snapshot.transport_present
        ):
            raise EndpointHubInvariantError("published endpoint hub is not ready")
    if state == EndpointHubState.ATTACHED:
        if buffer_empty and snapshot.published != snapshot.publish_when_ready:
            raise EndpointHubInvariantError("attached publication and publication intent diverged")
        if snapshot.published and not snapshot.publish_when_ready:
            raise EndpointHubInvariantError("published endpoint hub lost publication intent")
    elif snapshot.published:
        raise EndpointHubInvariantError("only an attached endpoint hub may be published")


def reduce_endpoint_hub(
    snapshot: EndpointHubSnapshot,
    action: EndpointHubAction | str,
    *,
    buffer_empty: bool,
) -> EndpointHubSnapshot:
    """Apply one lifecycle action to the complete endpoint-hub state."""

    try:
        normalized_action = EndpointHubAction(action)
    except ValueError as exc:
        raise EndpointHubInvariantError(f"unknown endpoint hub action: {action}") from exc
    validate_endpoint_hub_snapshot(snapshot, buffer_empty=buffer_empty)
    state = snapshot.state

    if normalized_action == EndpointHubAction.DISCOVER:
        if snapshot != ABSENT_ENDPOINT_HUB_SNAPSHOT or not buffer_empty:
            _illegal(snapshot, normalized_action)
        target = EndpointHubSnapshot(
            state=EndpointHubState.DISCOVERED,
            physical_present=True,
            transport_present=False,
            published=False,
            publish_when_ready=False,
        )
    elif normalized_action == EndpointHubAction.REQUEST_PUBLISH:
        if state in {EndpointHubState.ABSENT, EndpointHubState.REMOVING}:
            _illegal(snapshot, normalized_action)
        ready = (
            state == EndpointHubState.ATTACHED
            and snapshot.transport_present
            and buffer_empty
        )
        target = replace(snapshot, published=ready, publish_when_ready=True)
    elif normalized_action == EndpointHubAction.WITHDRAW:
        if state in {EndpointHubState.ABSENT, EndpointHubState.REMOVING}:
            _illegal(snapshot, normalized_action)
        target = replace(snapshot, published=False, publish_when_ready=False)
    elif normalized_action == EndpointHubAction.BEGIN_TRANSITION:
        if state in {EndpointHubState.ABSENT, EndpointHubState.REMOVING}:
            _illegal(snapshot, normalized_action)
        target = replace(
            snapshot,
            state=EndpointHubState.TRANSITIONING,
            published=False,
            publish_when_ready=(snapshot.published or snapshot.publish_when_ready),
        )
    elif normalized_action == EndpointHubAction.REGISTER_TRANSPORT:
        if state != EndpointHubState.TRANSITIONING:
            _illegal(snapshot, normalized_action)
        target = replace(snapshot, transport_present=True)
    elif normalized_action == EndpointHubAction.BEGIN_DRAIN:
        if state != EndpointHubState.TRANSITIONING or not snapshot.transport_present:
            _illegal(snapshot, normalized_action)
        target = replace(snapshot, state=EndpointHubState.DRAINING)
    elif normalized_action == EndpointHubAction.DRAIN_COMPLETE:
        if state != EndpointHubState.DRAINING or not snapshot.transport_present or not buffer_empty:
            _illegal(snapshot, normalized_action)
        target = replace(
            snapshot,
            state=EndpointHubState.ATTACHED,
            published=snapshot.publish_when_ready,
        )
    elif normalized_action == EndpointHubAction.TRANSITION_FAILED:
        if state not in {
            EndpointHubState.DISCOVERED,
            EndpointHubState.TRANSITIONING,
            EndpointHubState.DRAINING,
            EndpointHubState.DETACHED,
            EndpointHubState.DEGRADED,
        }:
            _illegal(snapshot, normalized_action)
        target = replace(
            snapshot,
            state=EndpointHubState.DEGRADED,
            published=False,
            publish_when_ready=False,
        )
    elif normalized_action == EndpointHubAction.DETACH_COMPLETE:
        if state != EndpointHubState.TRANSITIONING:
            _illegal(snapshot, normalized_action)
        target = replace(
            snapshot,
            state=EndpointHubState.DETACHED,
            transport_present=False,
            published=False,
            publish_when_ready=False,
        )
    elif normalized_action == EndpointHubAction.ROLLBACK_TRANSPORT:
        if state != EndpointHubState.TRANSITIONING or not snapshot.transport_present:
            _illegal(snapshot, normalized_action)
        target = replace(snapshot, state=EndpointHubState.DRAINING, published=False)
    elif normalized_action == EndpointHubAction.TRANSPORT_REMOVED:
        if state in {EndpointHubState.ABSENT, EndpointHubState.REMOVING}:
            _illegal(snapshot, normalized_action)
        target = replace(
            snapshot,
            state=(
                EndpointHubState.TRANSITIONING
                if state == EndpointHubState.TRANSITIONING
                else EndpointHubState.DETACHED
            ),
            transport_present=False,
            published=False,
            publish_when_ready=False,
        )
    elif normalized_action == EndpointHubAction.BEGIN_REMOVE:
        if (
            state in {EndpointHubState.ABSENT, EndpointHubState.REMOVING}
            or snapshot.transport_present
            or snapshot.published
            or snapshot.publish_when_ready
        ):
            _illegal(snapshot, normalized_action)
        target = EndpointHubSnapshot(
            state=EndpointHubState.REMOVING,
            physical_present=False,
            transport_present=False,
            published=False,
            publish_when_ready=False,
        )
    elif normalized_action == EndpointHubAction.REMOVE_COMPLETE:
        if state != EndpointHubState.REMOVING or not buffer_empty:
            _illegal(snapshot, normalized_action)
        target = ABSENT_ENDPOINT_HUB_SNAPSHOT
    else:  # pragma: no cover
        _illegal(snapshot, normalized_action)

    validate_endpoint_hub_snapshot(target, buffer_empty=buffer_empty)
    return target


def _illegal(snapshot: EndpointHubSnapshot, action: EndpointHubAction) -> None:
    raise EndpointHubInvariantError(
        "illegal endpoint hub transition: "
        f"{snapshot.state.value} + {action.value} "
        f"(physical={snapshot.physical_present}, transport={snapshot.transport_present}, "
        f"published={snapshot.published}, intent={snapshot.publish_when_ready})"
    )


def iter_valid_endpoint_hub_snapshots() -> tuple[tuple[EndpointHubSnapshot, bool], ...]:
    """Finite reducer domain used to generate and test the TLA+ relation."""

    valid: list[tuple[EndpointHubSnapshot, bool]] = []
    for state, physical, transport, published, intent, buffer_empty in product(
        EndpointHubState,
        (False, True),
        (False, True),
        (False, True),
        (False, True),
        (False, True),
    ):
        snapshot = EndpointHubSnapshot(
            state=state,
            physical_present=physical,
            transport_present=transport,
            published=published,
            publish_when_ready=intent,
        )
        try:
            validate_endpoint_hub_snapshot(snapshot, buffer_empty=buffer_empty)
        except EndpointHubInvariantError:
            continue
        valid.append((snapshot, buffer_empty))
    return tuple(valid)


def iter_endpoint_hub_reducer_edges() -> tuple[
    tuple[EndpointHubSnapshot, bool, EndpointHubAction, EndpointHubSnapshot], ...
]:
    edges: list[
        tuple[EndpointHubSnapshot, bool, EndpointHubAction, EndpointHubSnapshot]
    ] = []
    for snapshot, buffer_empty in iter_valid_endpoint_hub_snapshots():
        for action in EndpointHubAction:
            try:
                target = reduce_endpoint_hub(snapshot, action, buffer_empty=buffer_empty)
            except EndpointHubInvariantError:
                continue
            edges.append((snapshot, buffer_empty, action, target))
    return tuple(edges)


__all__ = [
    "ABSENT_ENDPOINT_HUB_SNAPSHOT",
    "EndpointHubAction",
    "EndpointHubInvariantError",
    "EndpointHubSnapshot",
    "EndpointHubState",
    "iter_endpoint_hub_reducer_edges",
    "iter_valid_endpoint_hub_snapshots",
    "reduce_endpoint_hub",
    "validate_endpoint_hub_snapshot",
]
