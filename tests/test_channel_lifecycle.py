from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from pal.channel.formal import render_endpoint_hub_implementation_relation
from pal.channel.capabilities import ChannelIntrospectionProvider
from pal.channel.channel_endpoint_queue_base import ChannelEndpointQueueBase
from pal.channel.contracts import EndpointConfig, QueuedReply, ResponseHandle
from pal.channel.lifecycle import (
    ABSENT_ENDPOINT_HUB_SNAPSHOT,
    EndpointHubAction,
    EndpointHubInvariantError,
    EndpointHubSnapshot,
    EndpointHubState,
    iter_endpoint_hub_reducer_edges,
    reduce_endpoint_hub,
)
from pal.channel.runtime import ChannelEndpointHub, ChannelRuntime


class _FailingStartEndpoint(ChannelEndpointQueueBase):
    def normalize_raw(self, payload):
        return dict(payload or {})

    def send_reply(self, response_handle: ResponseHandle, text: str) -> None:
        _ = response_handle, text

    def inspect_health(self) -> dict[str, object]:
        return {"healthy": False}

    def inspect_auth_state(self) -> dict[str, object]:
        return {"authorized": False}

    async def start_async(self) -> None:
        raise RuntimeError("startup failed")


class _Endpoint(ChannelEndpointQueueBase):
    def normalize_raw(self, payload):
        return dict(payload or {})

    def send_reply(self, response_handle: ResponseHandle, text: str) -> None:
        _ = response_handle, text

    def inspect_health(self) -> dict[str, object]:
        return {"healthy": True}

    def inspect_auth_state(self) -> dict[str, object]:
        return {"authorized": True}


def _endpoint(endpoint_id: str) -> _Endpoint:
    return _Endpoint(
        endpoint=EndpointConfig(
            endpoint_id=endpoint_id,
            channel_kind="demo",
            binding_key=f"demo:{endpoint_id}",
        )
    )


def test_generated_endpoint_hub_relation_matches_runtime_reducer() -> None:
    generated = Path("spec/channel/EndpointHubImplementationReducer.tla").read_text(
        encoding="utf-8"
    )
    assert generated == render_endpoint_hub_implementation_relation()


@pytest.mark.parametrize(
    ("source", "buffer_empty", "action", "target"),
    iter_endpoint_hub_reducer_edges(),
)
def test_runtime_hub_applies_every_generated_reducer_edge(
    source: EndpointHubSnapshot,
    buffer_empty: bool,
    action: EndpointHubAction,
    target: EndpointHubSnapshot,
) -> None:
    hub = ChannelEndpointHub(
        endpoint_id="edge",
        state=source.state,
        physical_present=source.physical_present,
        transport_present=source.transport_present,
        published=source.published,
        publish_when_ready=source.publish_when_ready,
    )
    if not buffer_empty:
        hub.buffer.append(object())  # reducer observes only whether backlog exists

    assert hub.apply(action) == target
    assert hub.snapshot == target


def test_missing_endpoint_lifecycle_does_not_fall_back_to_recovery_socket() -> None:
    runtime = ChannelRuntime()
    recovery = runtime.ensure_endpoint_hub(
        "socket_recovery",
        provider_id="socket",
        channel_kind="socket",
    )
    runtime.set_recovery_endpoint("socket_recovery")
    before = (
        recovery.state,
        recovery.provider_id,
        recovery.transition_epoch,
        recovery.published,
    )

    with pytest.raises(EndpointHubInvariantError, match="outside the physical lifecycle"):
        runtime.begin_endpoint_transition("physically_missing", provider_id="removed")

    assert runtime.get_endpoint_hub("physically_missing") is None
    assert (
        recovery.state,
        recovery.provider_id,
        recovery.transition_epoch,
        recovery.published,
    ) == before


def test_physical_removal_requires_registered_recovery_hub() -> None:
    runtime = ChannelRuntime()
    hub = runtime.ensure_endpoint_hub("origin", provider_id="demo")

    with pytest.raises(EndpointHubInvariantError, match="without a recovery endpoint"):
        runtime.remove_endpoint_hub("origin")

    assert runtime.get_endpoint_hub("origin") is hub
    assert hub.state == EndpointHubState.DISCOVERED


def test_missing_state_has_no_lifecycle_transition() -> None:
    with pytest.raises(EndpointHubInvariantError, match="illegal endpoint hub transition"):
        reduce_endpoint_hub(
            ABSENT_ENDPOINT_HUB_SNAPSHOT,
            EndpointHubAction.BEGIN_TRANSITION,
            buffer_empty=True,
        )


def test_first_transport_start_failure_leaves_discovered_hub_degraded() -> None:
    asyncio.run(_assert_first_transport_start_failure_leaves_hub_degraded())


async def _assert_first_transport_start_failure_leaves_hub_degraded() -> None:
    runtime = ChannelRuntime()
    hub = runtime.ensure_endpoint_hub(
        "new_endpoint",
        provider_id="demo",
        channel_kind="demo",
    )
    await runtime.start_async()
    endpoint = _FailingStartEndpoint(
        endpoint=EndpointConfig(
            endpoint_id="new_endpoint",
            channel_kind="demo",
            binding_key="demo:1",
        )
    )

    with pytest.raises(RuntimeError, match="startup failed"):
        await runtime.replace_endpoint_async(endpoint)

    assert runtime.get_endpoint("new_endpoint") is None
    assert hub.state == EndpointHubState.DEGRADED
    assert not hub.published


def test_runtime_owned_replace_restores_prior_publication_after_drain() -> None:
    runtime = ChannelRuntime()
    runtime.register_endpoint(_endpoint("origin"))
    assert runtime.publish_endpoint("origin")

    runtime.replace_endpoint(_endpoint("origin"))

    hub = runtime.get_endpoint_hub("origin")
    assert hub is not None
    assert hub.state == EndpointHubState.ATTACHED
    assert hub.transport_present
    assert hub.published
    assert hub.publish_when_ready


def test_direct_register_cannot_publish_until_existing_backlog_drains() -> None:
    runtime = ChannelRuntime()
    hub = runtime.ensure_endpoint_hub("origin", provider_id="demo")
    endpoint = _endpoint("origin")
    endpoint.disable()
    item = QueuedReply(
        reply_id="buffered",
        response_handle=ResponseHandle(endpoint_id="origin"),
        endpoint=endpoint.endpoint,
        text="survive attach",
    )
    hub.append("reply", item.reply_id, item)

    runtime.register_endpoint(endpoint)
    assert hub.state == EndpointHubState.DRAINING
    assert not runtime.publish_endpoint("origin")
    assert not hub.published
    assert hub.publish_when_ready

    endpoint.enable()
    runtime.sync_endpoints()
    assert hub.state == EndpointHubState.ATTACHED
    assert not hub.buffer
    assert hub.published


def test_transport_removal_withdraws_capability_in_same_reducer_step() -> None:
    runtime = ChannelRuntime()
    runtime.register_endpoint(_endpoint("origin"))
    assert runtime.publish_endpoint("origin")

    assert runtime.remove_endpoint("origin")

    hub = runtime.get_endpoint_hub("origin")
    assert hub is not None
    assert hub.state == EndpointHubState.DETACHED
    assert not hub.transport_present
    assert not hub.published
    assert not hub.publish_when_ready
    assert runtime.list_endpoint_hubs(published_only=True) == ()


def test_visibility_projection_failure_cannot_half_apply_lifecycle_state() -> None:
    runtime = ChannelRuntime()
    runtime.register_endpoint(_endpoint("origin"))
    callback_count = 0

    def _project_visibility() -> None:
        nonlocal callback_count
        callback_count += 1
        if callback_count == 2:
            raise RuntimeError("projection failed")

    runtime.on_hub_visibility_changed = _project_visibility
    assert runtime.publish_endpoint("origin")

    with pytest.raises(RuntimeError, match="projection failed"):
        runtime.begin_endpoint_transition("origin")

    hub = runtime.get_endpoint_hub("origin")
    assert hub is not None
    assert hub.state == EndpointHubState.ATTACHED
    assert hub.published
    assert hub.publish_when_ready
    assert hub.transition_epoch == 1

    runtime.fail_endpoint_transition("origin", "original failure")
    assert hub.state == EndpointHubState.ATTACHED
    assert hub.published
    assert hub.last_error == "original failure"


def test_capability_republication_swaps_registry_generation_without_unmount_gap() -> None:
    previous = SimpleNamespace(mounted=True)
    replacement = SimpleNamespace(mounted=False)
    handle = SimpleNamespace(
        mounted_subtree=previous,
        published_capabilities=["old"],
    )

    class _ExecutionRuntime:
        def unmount_subtree(self, _handle) -> None:
            raise AssertionError("republish must not expose an empty registry generation")

        def hydrate_module_handle(self, target) -> None:
            target.mounted_subtree = replacement

        def mount_subtree(self, target) -> list[str]:
            assert previous.mounted
            target.mounted_subtree.mounted = True
            return ["new"]

    context = SimpleNamespace(
        module_registry=SimpleNamespace(get=lambda _module_id: handle),
        execution_runtime=_ExecutionRuntime(),
    )
    provider = ChannelIntrospectionProvider(
        runtime=ChannelRuntime(),
        repository=object(),
        provider_manager=object(),
        main_context=context,
    )

    assert provider._republish_capabilities() == ["new"]
    assert handle.mounted_subtree is replacement
    assert replacement.mounted
    assert not previous.mounted
    assert handle.published_capabilities == ["new"]
