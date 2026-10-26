from __future__ import annotations

import asyncio
from importlib import resources
from types import SimpleNamespace

import pytest

from pal.channel import ChannelRuntime, register_with_core as register_channel
from pal.channel.contracts import ChannelDeliveryError, ChannelStreamUpdate, EndpointConfig
from pal.channel.repository import ChannelEndpointRepository
from pal.control.contracts import InteractionButtonSpec, InteractionMessageSpec
from pal.core import PalCore, register_with_core as register_core
from pal.execution import register_with_core as register_execution
from pal.foundation.attachment import AttachmentSpec
from pal.plugins import PluginHost
from pal.plugins_builtin.null_channel.endpoint import NullChannelEndpoint
from pal.shared import ChannelStreamUpdateKind, EventKind
from pal.wizard import WizardService


def test_sink_discards_all_output_and_does_not_accumulate_stream_or_interaction_state(tmp_path):
    endpoint = NullChannelEndpoint(endpoint=EndpointConfig("null", "null", "/dev/null"))
    handle = endpoint.build_response_handle()
    replies = [endpoint.queue_reply(text, response_handle=handle) for text in ("hello", "", "\u200b")]
    # A nonexistent attachment must be consumed without opening the path.
    attachment_id = endpoint.queue_attachment(AttachmentSpec(path=str(tmp_path / "missing")), response_handle=handle)
    for index in range(100):
        for kind in (ChannelStreamUpdateKind.TEXT_DELTA, ChannelStreamUpdateKind.REASONING_DELTA):
            endpoint.queue_stream_update(ChannelStreamUpdate(kind=kind, text=f"part {index}"), response_handle=handle)
        assert endpoint.flush_stream_update_outbox() == []
    endpoint.queue_status("interactive_open", response_handle=handle, payload={"spec": InteractionMessageSpec(
        interaction_id="review", interaction_kind="memory_candidate_approval", text="Do not show",
        buttons=((InteractionButtonSpec("Accept", "accept"),),),
    )})
    endpoint.flush_status_outbox()
    events = endpoint.flush_outbox() + endpoint.flush_attachment_outbox()
    assert {event.payload["reply_id"] for event in events} == {*replies, attachment_id}
    assert all(event.event_kind == EventKind.REPLY_DELIVERED for event in events)
    assert not endpoint.outbox and not endpoint.attachment_outbox and not endpoint.stream_update_outbox
    assert not endpoint.status_outbox and not endpoint._stream_sessions and not endpoint._interactive_messages
    assert not endpoint.mailbox.peek_all()
    assert endpoint.inspect_health()["delivery_mode"] == "discard"


def test_active_sink_send_needs_no_recipient_and_does_not_queue_content():
    endpoint = NullChannelEndpoint(endpoint=EndpointConfig("null", "null", "/dev/null"))
    receipt = asyncio.run(endpoint.send_message("discard this"))
    assert receipt.endpoint_id == "null" and receipt.status == "accepted"
    assert not endpoint.outbox
    endpoint.disable()
    with pytest.raises(ChannelDeliveryError, match="disabled"):
        asyncio.run(endpoint.send_message("do not bypass lifecycle"))
    endpoint.enable()
    endpoint.detach()
    with pytest.raises(ChannelDeliveryError, match="detached"):
        asyncio.run(endpoint.send_message("do not bypass lifecycle"))
    with pytest.raises(ChannelDeliveryError, match="incoming"):
        endpoint.accept_raw({"text": "not a control entrypoint"}, event_kind="user.message")


@pytest.fixture
def env(tmp_path):
    wizard = WizardService()
    registration = wizard.provision_runtime(
        display_name="Null channel test", runtime_root=tmp_path,
        db_filename="test.sqlite3", pal_entrypoint="pal.runtime.test",
    )
    db = wizard.create_database(registration)
    core = PalCore()
    register_execution(core.context)
    channel = ChannelRuntime()
    register_channel(core.context, channel, runtime_root=tmp_path)
    builtin = tmp_path / "test-builtins" / "null_channel"
    builtin.mkdir(parents=True)
    builtin.joinpath("plugin.toml").write_text(
        resources.files("pal.plugins_builtin.null_channel").joinpath("plugin.toml").read_text(),
    )
    host = PluginHost(core.context, tmp_path, builtin_root=builtin.parent)
    host.bootstrap()
    yield SimpleNamespace(core=core, channel=channel, host=host, repository=ChannelEndpointRepository())
    host.shutdown()
    core.context.execution_runtime.shutdown()
    db.close()


def test_builtin_sink_is_discoverable_reloadable_and_preserves_endpoint_settings(env):
    record = env.host._record("null_channel")
    assert record.attached, record.last_error
    endpoint = env.channel.get_endpoint("null")
    assert endpoint is not None
    hub = env.channel.get_endpoint_hub("null")
    assert hub.published
    assert env.channel.resolve_output_destination(endpoint_id="null") == {
        "channel_id": "null", "reply_target": {"sink": "null"},
    }
    assert asyncio.run(env.channel.send_message("null", "discard this")).status == "accepted"
    assert env.host.reattach("null_channel")["status"] == "ok"
    assert env.channel.get_endpoint("null") is not endpoint
    assert env.channel.get_endpoint_hub("null") is hub
    env.repository.set_enabled("null", False)
    assert env.host.reattach("null_channel")["status"] == "ok"
    assert not env.repository.get("null").enabled
    assert env.channel.get_endpoint("null") is None
    with pytest.raises(ValueError, match="unavailable"):
        env.channel.resolve_output_destination(endpoint_id="null")


def test_sink_cannot_overwrite_an_existing_endpoint_of_another_kind(env):
    assert env.host.detach("null_channel")["status"] == "ok"
    env.repository.upsert(endpoint_id="null", channel_kind="other", binding_key="keep-me", enabled=True)
    result = env.host.attach("null_channel")
    assert result["status"] != "ok"
    assert "different channel kind" in result["error"]
    assert env.repository.get("null").binding_key == "keep-me"


def test_proactive_sink_keeps_execution_history_without_delivering_to_another_channel(env):
    from pal.llm import generation_result_from_values
    from pal.memory import MemoryService, register_with_core as register_memory
    from pal.proactive import ProactiveDefinition, ProactiveManager, register_with_core as register_proactive
    from pal.shared import ProactiveTriggerEvent
    from tests.test_architecture_skeleton import ScriptedLLMRuntime

    core = env.core
    register_core(core)
    memory = MemoryService()
    register_memory(core.context, memory)
    manager = ProactiveManager()
    register_proactive(core.context, manager)
    manager.register(ProactiveDefinition("silent", "Run a silent check", out_channel_id="null"))
    core.context.port_registry["llm:llm"] = ScriptedLLMRuntime([
        generation_result_from_values(text="\u200b", tool_calls=[], finish_reason="stop"),
    ])
    manager.enqueue_trigger(ProactiveTriggerEvent(proactive_id="silent", trigger_kind="manual"))
    processed = core.run_until_idle()
    assert EventKind.PROACTIVE_TRIGGER in [event.event_kind for event in processed]
    assert not [event for event in processed if event.event_kind == EventKind.REPLY_FAILED]
    assert any(message.content == "\u200b" for turn in memory.l1_store.items for message in turn)
    endpoint = env.channel.get_endpoint("null")
    assert endpoint is not None
    assert not endpoint.outbox and not endpoint.stream_update_outbox and not endpoint.status_outbox
    for other in env.channel.list_endpoints():
        assert not other.outbox and not other.stream_update_outbox
