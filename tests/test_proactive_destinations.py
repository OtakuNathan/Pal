from __future__ import annotations

from types import SimpleNamespace

import pytest

from pal.core import PalCore
from pal.channel import ChannelRuntime
from pal.execution import register_with_core as register_execution
from pal.foundation import PalV2Database
from pal.proactive import ProactiveManager, ProactiveRepository, register_with_core as register_proactive
from pal.proactive.models import ProactiveDefinitionModel, ProactiveRunModel
from pal.shared.tool_protocol import new_tool_call


@pytest.fixture
def env(tmp_path):
    db = PalV2Database(tmp_path / "proactive.sqlite3")
    db.initialize([ProactiveDefinitionModel, ProactiveRunModel])
    core = PalCore()
    register_execution(core.context)
    core.publish_module_capabilities("execution")
    channel = ChannelRuntime()
    for name, target in (("telegram_main", {"chat_id": "42"}), ("socket_main", {})):
        channel.endpoint_registry.register(SimpleNamespace(
            endpoint=SimpleNamespace(endpoint_id=name, channel_kind="telegram" if name == "telegram_main" else "socket"),
            derive_default_reply_target=lambda target=target: dict(target),
        ))
    core.context.port_registry["channel:channel"] = channel
    binding = {"channel_id": "telegram_main", "reply_target": {"chat_id": "42", "thread_id": "7"}}
    core.context.execution_runtime.register_provider_ref("core:turn_io", SimpleNamespace(
        capture_delivery_binding=lambda turn_id: dict(binding) if turn_id else {},
    ))
    manager = ProactiveManager(repository=ProactiveRepository())
    register_proactive(core.context, manager)
    core.publish_module_capabilities("proactive")
    yield core.context.execution_runtime, manager, binding
    core.context.execution_runtime.shutdown()
    db.close()


def invoke(runtime, alias, args, turn_id="turn"):
    return runtime.execute_tool(new_tool_call(name="call_tool", args={"name": alias, "args": args}), turn_id=turn_id)


def test_create_defaults_to_current_conversation_and_persists(env):
    runtime, manager, binding = env
    result = invoke(runtime, "proactive_create", {"name": "reminder", "goal": "remind me"})
    assert result.ok, result.llm_text
    assert result.structured["out_reply_target"] == {"chat_id": "42", "thread_id": "7"}
    binding.update(channel_id="socket_main", reply_target={"session_id": "new"})
    replaced = invoke(runtime, "proactive_create", {"name": "reminder", "goal": "revised reminder"})
    assert replaced.ok
    stored = ProactiveRepository().list_definitions()[0].definition
    assert stored.goal == "revised reminder"
    assert stored.out_channel_id == "telegram_main"
    assert stored.out_reply_target == {"chat_id": "42", "thread_id": "7"}
    restarted = ProactiveManager()
    restarted.hydrate(stored)
    assert restarted.registered["reminder"].out_reply_target == stored.out_reply_target


def test_explicit_channel_uses_unique_default_without_old_provider_fields(env):
    runtime, manager, binding = env
    binding.update(channel_id="socket_main", reply_target={"session_id": "old", "request_id": "old-request"})
    created = invoke(runtime, "proactive_create", {"name": "reminder", "goal": "remind me"})
    assert created.ok, created.llm_text
    moved = invoke(runtime, "proactive_set_output_channel", {"name": "reminder", "out_channel_name": "telegram_main"})
    assert moved.ok, moved.llm_text
    assert moved.structured["out_reply_target"] == {"chat_id": "42"}
    created = invoke(runtime, "proactive_create", {"name": "another", "goal": "remind me", "out_channel_name": "telegram_main"})
    assert created.ok and created.structured["out_reply_target"] == {"chat_id": "42"}


def test_unresolved_destination_does_not_create_or_partially_move_task(env):
    runtime, manager, binding = env
    assert invoke(runtime, "proactive_create", {"name": "reminder", "goal": "remind me"}).ok
    before = manager.registered["reminder"]
    for name in ("socket_main", "missing"):
        moved = invoke(runtime, "proactive_set_output_channel", {"name": "reminder", "out_channel_name": name})
        assert not moved.ok
        assert manager.registered["reminder"] == before
        created = invoke(runtime, "proactive_create", {"name": "bad", "goal": "remind me", "out_channel_name": name})
        assert not created.ok
        assert "bad" not in manager.registered
    binding.clear()
    assert not invoke(runtime, "proactive_create", {"name": "bad", "goal": "remind me"}).ok
    assert "bad" not in manager.registered
    assert invoke(runtime, "proactive_create", {"name": "internal", "goal": "internal"}, turn_id=None).ok
    assert manager.registered["internal"].out_channel_id is None


def test_channel_and_explicit_target_change_together_and_can_clear(env):
    runtime, manager, binding = env
    assert invoke(runtime, "proactive_create", {"name": "reminder", "goal": "remind me"}).ok
    moved = invoke(runtime, "proactive_set_output_channel", {
        "name": "reminder", "out_channel_name": "socket_main", "out_reply_target": {"session_id": "other"},
    })
    assert moved.ok, moved.llm_text
    assert manager.registered["reminder"].out_reply_target == {"session_id": "other"}
    cleared = invoke(runtime, "proactive_set_output_channel", {"name": "reminder"})
    assert cleared.ok
    assert manager.registered["reminder"].out_channel_id is None
    assert manager.registered["reminder"].out_reply_target == {}


def test_telegram_rejects_non_chat_target(env):
    runtime, manager, binding = env
    result = invoke(runtime, "proactive_create", {
        "name": "bad", "goal": "remind me", "out_channel_name": "telegram_main",
        "out_reply_target": {"session_id": "socket-only"},
    })
    assert not result.ok
    assert "chat_id" in result.llm_text
    assert "bad" not in manager.registered


def test_explicit_same_channel_resolves_current_topic_and_persists(env):
    runtime, manager, binding = env
    assert invoke(runtime, "proactive_create", {"name": "reminder", "goal": "remind me"}).ok
    binding["reply_target"] = {"chat_id": "42", "thread_id": "8"}
    moved = invoke(runtime, "proactive_set_output_channel", {
        "name": "reminder", "out_channel_name": "telegram_main",
    })
    assert moved.ok, moved.llm_text
    assert moved.structured["out_reply_target"] == binding["reply_target"]
    stored = ProactiveRepository().list_definitions()[0].definition
    assert stored.out_reply_target == binding["reply_target"]


def test_internal_proactive_turn_can_create_outputless_or_explicitly_routed_task(env):
    runtime, manager, binding = env
    binding.update(channel_id="proactive:maintenance", channel_kind="proactive", reply_target={})
    created = invoke(runtime, "proactive_create", {"name": "internal", "goal": "internal check"})
    assert created.ok, created.llm_text
    assert manager.registered["internal"].out_channel_id is None
    assert manager.registered["internal"].out_reply_target == {}
    routed = invoke(runtime, "proactive_create", {
        "name": "routed", "goal": "send report", "out_channel_name": "telegram_main",
    })
    assert routed.ok, routed.llm_text
    assert manager.registered["routed"].out_reply_target == {"chat_id": "42"}
    invalid = invoke(runtime, "proactive_create", {
        "name": "bad", "goal": "send report", "out_channel_name": "missing",
    })
    assert not invalid.ok
    assert "bad" not in manager.registered


@pytest.mark.parametrize("schedule", [
    {"cron": "0 9 * * *"},
    {"cadance": "cron", "cron": "0 9 * * *"},
    {"cadence": "manual", "cron": "0 9 * * *"},
    {"cadence": "once", "cron": "0 9 * * *"},
    {"cadence": "cron"},
    {"cadence": "cron", "cron": "0 9 * * * *"},
    {"cadence": "cron", "cron": 123},
    {"cadence": None},
    {"timezone": ""},
    {"timezone": "/etc/passwd"},
])
def test_invalid_schedule_reports_task_parameter_error_without_creating(env, schedule):
    runtime, manager, _ = env
    result = invoke(runtime, "proactive_create", {
        "name": "bad_schedule", "goal": "remind me", "schedule": schedule,
    })
    assert not result.ok
    assert "Task parameter error:" in result.llm_text
    assert "schedule" in result.llm_text
    assert "bad_schedule" not in manager.registered


@pytest.mark.parametrize("schedule", [{}, {"cadence": "manual"}, {"timezone": "UTC"}])
def test_explicit_manual_defaults_remain_valid(env, schedule):
    runtime, manager, _ = env
    result = invoke(runtime, "proactive_create", {
        "name": "manual", "goal": "run on request", "schedule": schedule,
    })
    assert result.ok, result.llm_text
    assert manager.registered["manual"].schedule["cadence"] == "manual"


def test_invalid_schedule_update_preserves_existing_task(env):
    runtime, manager, _ = env
    assert invoke(runtime, "proactive_create", {"name": "reminder", "goal": "remind me"}).ok
    before = manager.registered["reminder"]
    result = invoke(runtime, "proactive_update_schedule", {
        "name": "reminder", "schedule": {"cron": "0 9 * * *"},
    })
    assert not result.ok
    assert "Task parameter error:" in result.llm_text
    assert manager.registered["reminder"] == before
