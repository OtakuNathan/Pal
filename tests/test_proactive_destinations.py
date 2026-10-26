from __future__ import annotations

from types import SimpleNamespace

import pytest

from pal.core import PalCore
from pal.channel import ChannelRuntime
from pal.channel.contracts import EndpointConfig
from pal.execution import register_with_core as register_execution
from pal.foundation import PalV2Database
from pal.proactive import ProactiveManager, ProactiveRepository, register_with_core as register_proactive
from pal.proactive.models import ProactiveDefinitionModel, ProactiveRunModel
from pal.plugins_builtin.null_channel.endpoint import NullChannelEndpoint
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
    channel.endpoint_registry.register(NullChannelEndpoint(endpoint=EndpointConfig("null", "null", "/dev/null")))
    core.context.port_registry["channel:channel"] = channel
    binding = {"channel_id": "telegram_main", "reply_target": {"chat_id": "42", "thread_id": "7"}}
    from tests.turn_fakes import TurnIOFixture
    core.context.execution_runtime.register_provider_ref("core:turn_io", TurnIOFixture(binding=binding))
    manager = ProactiveManager(repository=ProactiveRepository())
    register_proactive(core.context, manager)
    core.publish_module_capabilities("proactive")
    yield core.context.execution_runtime, manager, binding
    core.context.execution_runtime.shutdown()
    db.close()


def invoke(runtime, alias, args, turn_id="turn"):
    return runtime.execute_tool(new_tool_call(name="call_tool", args={"name": alias, "args": args}), turn_id=turn_id)


@pytest.mark.parametrize("alias", ["read_proactive_task", "list_proactive_runs"])
def test_task_read_tools_exist_before_creation_and_after_deletion(env, alias):
    runtime, manager, _ = env
    assert not manager.registered
    assert alias in runtime.registry_generation.indirect_aliases
    missing = invoke(runtime, alias, {"name": "reminder"})
    assert not missing.ok
    assert missing.structured["error_code"] == "unknown_target"
    assert missing.structured["effect"] == "not_started"
    assert invoke(runtime, "upsert_proactive_task", {"name": "reminder", "goal": "remind me"}).ok
    found = invoke(runtime, alias, {"name": "reminder"})
    assert found.ok, found.llm_text
    assert found.structured["name"] == "reminder"
    assert invoke(runtime, "manage_proactive_task", {"name": "reminder", "operation": "delete"}).ok
    assert alias in runtime.registry_generation.indirect_aliases
    assert not invoke(runtime, alias, {"name": "reminder"}).ok


def test_create_defaults_to_current_conversation_and_persists(env):
    runtime, manager, binding = env
    result = invoke(runtime, "upsert_proactive_task", {"name": "reminder", "goal": "remind me"})
    assert result.ok, result.llm_text
    assert result.structured["out_reply_target"] == {"chat_id": "42", "thread_id": "7"}
    binding.update(channel_id="socket_main", reply_target={"session_id": "new"})
    replaced = invoke(runtime, "upsert_proactive_task", {"name": "reminder", "goal": "revised reminder"})
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
    created = invoke(runtime, "upsert_proactive_task", {"name": "reminder", "goal": "remind me"})
    assert created.ok, created.llm_text
    moved = invoke(runtime, "set_proactive_destination", {"name": "reminder", "out_channel_name": "telegram_main"})
    assert moved.ok, moved.llm_text
    assert moved.structured["out_reply_target"] == {"chat_id": "42"}
    created = invoke(runtime, "upsert_proactive_task", {"name": "another", "goal": "remind me", "out_channel_name": "telegram_main"})
    assert created.ok and created.structured["out_reply_target"] == {"chat_id": "42"}


def test_unresolved_destination_does_not_create_or_partially_move_task(env):
    runtime, manager, binding = env
    assert invoke(runtime, "upsert_proactive_task", {"name": "reminder", "goal": "remind me"}).ok
    before = manager.registered["reminder"]
    for name in ("socket_main", "missing"):
        moved = invoke(runtime, "set_proactive_destination", {"name": "reminder", "out_channel_name": name})
        assert not moved.ok
        assert manager.registered["reminder"] == before
        created = invoke(runtime, "upsert_proactive_task", {"name": "bad", "goal": "remind me", "out_channel_name": name})
        assert not created.ok
        assert "bad" not in manager.registered
    binding.clear()
    assert not invoke(runtime, "upsert_proactive_task", {"name": "bad", "goal": "remind me"}).ok
    assert "bad" not in manager.registered
    assert invoke(runtime, "upsert_proactive_task", {"name": "internal", "goal": "internal"}, turn_id=None).ok
    assert manager.registered["internal"].out_channel_id is None


def test_channel_and_explicit_target_change_together_and_can_clear(env):
    runtime, manager, binding = env
    assert invoke(runtime, "upsert_proactive_task", {"name": "reminder", "goal": "remind me"}).ok
    moved = invoke(runtime, "set_proactive_destination", {
        "name": "reminder", "out_channel_name": "socket_main", "out_reply_target": {"session_id": "other"},
    })
    assert moved.ok, moved.llm_text
    assert manager.registered["reminder"].out_reply_target == {"session_id": "other"}
    cleared = invoke(runtime, "set_proactive_destination", {"name": "reminder", "out_channel_name": None})
    assert cleared.ok
    assert manager.registered["reminder"].out_channel_id is None
    assert manager.registered["reminder"].out_reply_target == {}


def test_null_channel_is_an_explicit_persisted_destination_distinct_from_clearing(env):
    runtime, manager, _ = env
    created = invoke(runtime, "upsert_proactive_task", {
        "name": "silent", "goal": "local maintenance", "out_channel_name": "null",
    })
    assert created.ok, created.llm_text
    assert manager.registered["silent"].out_channel_id == "null"
    assert created.structured["out_reply_target"] == {"sink": "null"}
    assert ProactiveRepository().list_definitions()[0].definition.out_channel_id == "null"
    assert invoke(runtime, "upsert_proactive_task", {"name": "reminder", "goal": "remind me"}).ok
    moved = invoke(runtime, "set_proactive_destination", {"name": "reminder", "out_channel_name": "null"})
    assert moved.ok, moved.llm_text
    assert manager.registered["reminder"].out_reply_target == {"sink": "null"}
    cleared = invoke(runtime, "set_proactive_destination", {"name": "reminder", "out_channel_name": None})
    assert cleared.ok
    assert manager.registered["reminder"].out_channel_id is None


def test_telegram_rejects_non_chat_target(env):
    runtime, manager, binding = env
    result = invoke(runtime, "upsert_proactive_task", {
        "name": "bad", "goal": "remind me", "out_channel_name": "telegram_main",
        "out_reply_target": {"session_id": "socket-only"},
    })
    assert not result.ok
    assert "chat_id" in result.llm_text
    assert "bad" not in manager.registered


def test_explicit_same_channel_resolves_current_topic_and_persists(env):
    runtime, manager, binding = env
    assert invoke(runtime, "upsert_proactive_task", {"name": "reminder", "goal": "remind me"}).ok
    binding["reply_target"] = {"chat_id": "42", "thread_id": "8"}
    moved = invoke(runtime, "set_proactive_destination", {
        "name": "reminder", "out_channel_name": "telegram_main",
    })
    assert moved.ok, moved.llm_text
    assert moved.structured["out_reply_target"] == binding["reply_target"]
    stored = ProactiveRepository().list_definitions()[0].definition
    assert stored.out_reply_target == binding["reply_target"]


def test_internal_proactive_turn_can_create_outputless_or_explicitly_routed_task(env):
    runtime, manager, binding = env
    binding.update(channel_id="proactive:maintenance", channel_kind="proactive", reply_target={})
    created = invoke(runtime, "upsert_proactive_task", {"name": "internal", "goal": "internal check"})
    assert created.ok, created.llm_text
    assert manager.registered["internal"].out_channel_id is None
    assert manager.registered["internal"].out_reply_target == {}
    routed = invoke(runtime, "upsert_proactive_task", {
        "name": "routed", "goal": "send report", "out_channel_name": "telegram_main",
    })
    assert routed.ok, routed.llm_text
    assert manager.registered["routed"].out_reply_target == {"chat_id": "42"}
    invalid = invoke(runtime, "upsert_proactive_task", {
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
    result = invoke(runtime, "upsert_proactive_task", {
        "name": "bad_schedule", "goal": "remind me", "schedule": schedule,
    })
    assert not result.ok
    assert "Task parameter error:" in result.llm_text or (result.status == "invalid_arguments" and "schedule" in result.llm_text)
    assert "schedule" in result.llm_text
    assert "bad_schedule" not in manager.registered


@pytest.mark.parametrize("schedule", [{}, {"cadence": "manual"}, {"timezone": "UTC"}])
def test_explicit_manual_defaults_remain_valid(env, schedule):
    runtime, manager, _ = env
    result = invoke(runtime, "upsert_proactive_task", {
        "name": "manual", "goal": "run on request", "schedule": schedule,
    })
    assert result.ok, result.llm_text
    assert manager.registered["manual"].schedule["cadence"] == "manual"


def test_invalid_schedule_update_preserves_existing_task(env):
    runtime, manager, _ = env
    assert invoke(runtime, "upsert_proactive_task", {"name": "reminder", "goal": "remind me"}).ok
    before = manager.registered["reminder"]
    result = invoke(runtime, "update_proactive_schedule", {
        "name": "reminder", "schedule": {"cron": "0 9 * * *"},
    })
    assert not result.ok
    assert "Task parameter error:" in result.llm_text or (result.status == "invalid_arguments" and "schedule" in result.llm_text)
    assert manager.registered["reminder"] == before


@pytest.mark.parametrize("alias", ["read_proactive_task", "list_proactive_runs"])
def test_history_unavailable_is_distinct_from_empty(env, alias):
    runtime, manager, _ = env
    assert invoke(runtime, "upsert_proactive_task", {"name": "reminder", "goal": "remind me"}).ok
    empty = invoke(runtime, alias, {"name": "reminder"})
    assert empty.ok, empty.llm_text
    assert empty.structured["history_status"] == "empty"
    manager.repository = None
    unavailable = invoke(runtime, alias, {"name": "reminder"})
    assert unavailable.structured["history_status"] == "unavailable"
    assert "repository is not configured" in unavailable.llm_text
    assert unavailable.llm_text != empty.llm_text


def test_destination_surface_retires_both_old_setters(env):
    runtime, _, _ = env
    generation = runtime.registry_generation
    names = {name for name, record in generation.search_records.items() if record['module_id'] == 'proactive'}
    assert len(names) == 8
    assert 'set_proactive_destination' in generation.indirect_aliases
    spec = runtime.get_tool_spec('set_proactive_destination')['input_schema']
    assert set(spec['properties']) == {'name', 'out_channel_name', 'out_reply_target'}
    assert spec['required'] == ['name']
    for name in ('set_proactive_output_channel', 'set_proactive_output_target'):
        assert generation.record_for_alias(name) is None
        assert name not in generation.provider_specs
        assert not runtime.execute_tool(new_tool_call(name='read_tool', args={'name': name})).ok
        assert not invoke(runtime, name, {'name': 'reminder'}).ok


def test_target_only_update_preserves_channel_and_other_configuration(env):
    runtime, manager, binding = env
    assert invoke(runtime, 'upsert_proactive_task', {
        'name': 'reminder', 'goal': 'report', 'method': 'check', 'skill_refs': ['summary'],
        'enabled': False, 'schedule': {'cadence': 'cron', 'cron': '0 9 * * *'},
    }).ok
    before = manager.registered['reminder']
    binding.update(channel_id='socket_main', reply_target={'session_id': 'unrelated'})
    result = invoke(runtime, 'set_proactive_destination', {
        'name': 'reminder', 'out_reply_target': {'chat_id': '99', 'thread_id': '8'},
    })
    assert result.ok, result.llm_text
    after = manager.registered['reminder']
    assert after.out_channel_id == 'telegram_main'
    assert after.out_reply_target == {'chat_id': '99', 'thread_id': '8'}
    for field in ('goal', 'method', 'skill_refs', 'enabled', 'schedule'):
        assert getattr(after, field) == getattr(before, field)
    stored = ProactiveRepository().list_definitions()[0].definition
    assert stored.out_reply_target == after.out_reply_target


def test_empty_target_clears_only_target_without_resolving_default(env):
    runtime, manager, _ = env
    assert invoke(runtime, 'upsert_proactive_task', {'name': 'reminder', 'goal': 'report'}).ok
    result = invoke(runtime, 'set_proactive_destination', {'name': 'reminder', 'out_reply_target': {}})
    assert result.ok, result.llm_text
    assert manager.registered['reminder'].out_channel_id == 'telegram_main'
    assert manager.registered['reminder'].out_reply_target == {}
    stored = ProactiveRepository().list_definitions()[0].definition
    assert stored.out_channel_id == 'telegram_main' and stored.out_reply_target == {}


@pytest.mark.parametrize('patch', [
    {}, {'out_reply_target': None}, {'out_reply_target': {'session_id': 'wrong-provider'}},
    {'out_channel_name': None, 'out_reply_target': {'chat_id': '99'}},
    {'out_channel_name': ''}, {'out_channel_name': ' '},
    {'out_channel_name': 'missing', 'out_reply_target': {'chat_id': '99'}},
    {'out_channel_name': 'missing', 'out_reply_target': {}},
])
def test_invalid_destination_update_preserves_memory_and_durable_definition(env, patch):
    runtime, manager, _ = env
    assert invoke(runtime, 'upsert_proactive_task', {'name': 'reminder', 'goal': 'report'}).ok
    before = manager.registered['reminder']
    stored = ProactiveRepository().list_definitions()[0].definition
    due = manager.schedule_engine.next_due_at('reminder')
    result = invoke(runtime, 'set_proactive_destination', {'name': 'reminder', **patch})
    assert not result.ok
    assert manager.registered['reminder'] == before
    assert ProactiveRepository().list_definitions()[0].definition == stored
    assert manager.schedule_engine.next_due_at('reminder') == due


def test_target_only_update_requires_existing_channel(env):
    runtime, manager, _ = env
    assert invoke(runtime, 'upsert_proactive_task', {'name': 'internal', 'goal': 'check'}, turn_id=None).ok
    result = invoke(runtime, 'set_proactive_destination', {'name': 'internal', 'out_reply_target': {'chat_id': '99'}})
    assert not result.ok
    assert manager.registered['internal'].out_channel_id is None
    assert manager.registered['internal'].out_reply_target == {}


def test_destination_persistence_failure_does_not_publish_partial_update(env, monkeypatch):
    runtime, manager, _ = env
    assert invoke(runtime, 'upsert_proactive_task', {'name': 'reminder', 'goal': 'report'}).ok
    before = manager.registered['reminder']
    stored = ProactiveRepository().list_definitions()[0].definition
    due = manager.schedule_engine.next_due_at('reminder')
    def fail(*args, **kwargs):
        raise OSError('storage unavailable')
    monkeypatch.setattr(manager.repository, 'upsert_definition', fail)
    result = invoke(runtime, 'set_proactive_destination', {
        'name': 'reminder', 'out_channel_name': 'socket_main', 'out_reply_target': {'session_id': 'new'},
    })
    assert not result.ok
    assert manager.registered['reminder'] == before
    assert ProactiveRepository().list_definitions()[0].definition == stored
    assert manager.schedule_engine.next_due_at('reminder') == due


def test_control_surface_has_one_required_operation_and_retires_old_aliases(env):
    runtime, _, _ = env
    generation = runtime.registry_generation
    assert 'manage_proactive_task' in generation.indirect_aliases
    spec = runtime.get_tool_spec('manage_proactive_task')['input_schema']
    assert set(spec['properties']) == {'name', 'operation'}
    assert set(spec['required']) == {'name', 'operation'}
    assert set(spec['properties']['operation']['enum']) == {'enable', 'disable', 'delete'}
    for name in ('enable_proactive_task', 'disable_proactive_task', 'delete_proactive_task'):
        assert generation.record_for_alias(name) is None
        assert name not in generation.provider_specs
        assert not runtime.execute_tool(new_tool_call(name='read_tool', args={'name': name})).ok
        assert not invoke(runtime, name, {'name': 'reminder'}).ok
    assert runtime.invoke_direct_tool(new_tool_call(name='manage_proactive_task',
        args={'name': 'reminder', 'operation': 'enable'})).error_code == 'wrong_invocation_mode'


def test_task_control_pauses_resumes_and_deletes_durable_definition(env):
    runtime, manager, _ = env
    assert invoke(runtime, 'upsert_proactive_task', {'name': 'reminder', 'goal': 'report'}).ok
    original = manager.registered['reminder']
    for operation, enabled in (('disable', False), ('enable', True)):
        result = invoke(runtime, 'manage_proactive_task', {'name': 'reminder', 'operation': operation})
        assert result.ok, result.llm_text
        current = manager.registered['reminder']
        assert current.enabled is enabled
        assert ProactiveRepository().list_definitions()[0].definition.enabled is enabled
        for field in ('goal', 'method', 'skill_refs', 'schedule', 'out_channel_id', 'out_reply_target'):
            assert getattr(current, field) == getattr(original, field)
    deleted = invoke(runtime, 'manage_proactive_task', {'name': 'reminder', 'operation': 'delete'})
    assert deleted.ok, deleted.llm_text
    assert 'reminder' not in manager.registered
    assert ProactiveRepository().list_definitions() == []
    assert 'reminder' not in manager.schedule_engine.next_due_by_proactive_id


@pytest.mark.parametrize('args', [
    {'name': 'reminder'}, {'name': 'reminder', 'operation': 'pause'},
    {'name': 'reminder', 'operation': None}, {'name': ' ', 'operation': 'delete'},
    {'name': 'reminder', 'operation': 'enable', 'enabled': False},
])
def test_invalid_control_arguments_do_not_reach_task_owner(env, monkeypatch, args):
    from unittest.mock import Mock
    runtime, manager, _ = env
    set_enabled = Mock(side_effect=AssertionError('invalid control reached task owner'))
    destroy_task = Mock(side_effect=AssertionError('invalid control reached task owner'))
    monkeypatch.setattr(manager, 'set_enabled', set_enabled)
    monkeypatch.setattr(manager, 'destroy_task', destroy_task)
    result = invoke(runtime, 'manage_proactive_task', args)
    assert not result.ok
    set_enabled.assert_not_called()
    destroy_task.assert_not_called()
