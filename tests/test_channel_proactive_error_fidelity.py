"""Channel and proactive failures retain causes and execution facts for the LLM."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pal.channel.contracts import ChannelDeliveryError
from pal.execution.contracts import CapabilityResult
from pal.proactive.contracts import ProactiveDefinition
from pal.proactive.handler import ProactiveTriggerHandler
from pal.proactive.runtime import ProactiveManager, ProactiveRunner
from pal.shared import RuntimeStatus, ProactiveTriggerEvent
from tests import test_channel_send_message as channel_tests
from tests.test_proactive_destinations import env, invoke as proactive_invoke
from tests.test_tool_failure_affordances import invoke, metadata


def fail_with_cause(label):
    try:
        raise OSError("transport root cause; token=private-value")
    except OSError as cause:
        raise ChannelDeliveryError(label, reason="remote_failure") from cause


@pytest.fixture
def channel_env():
    core, channel, endpoint = channel_tests.ChannelSendMessageTests()._build_core()
    yield core, channel, endpoint
    core.close()


def test_channel_send_failure_keeps_cause_and_uncertainty(channel_env, monkeypatch):
    core, channel, endpoint = channel_env
    async def send(message):
        fail_with_cause("remote response lost")
    monkeypatch.setattr(endpoint, "send_message", send)
    result = invoke(core.context.execution_runtime, "send_channel_message",
                    {"name": "channel-main", "message": "hello"}, asynchronous=True)
    assert not result.ok
    assert "transport root cause" in result.llm_text
    assert "private-value" not in result.llm_text
    assert "was not sent" not in result.llm_text
    assert metadata(result)["error_code"] == "remote_failure"
    assert metadata(result)["effect"] == "unknown"
    assert metadata(result)["retry"] == "reconcile_first"


@pytest.mark.parametrize("state,code", [("missing", "channel_not_found"),
    ("detached", "channel_detached"), ("disabled", "channel_disabled")])
def test_channel_send_precondition_does_not_claim_unknown_effect(channel_env, state, code):
    core, _, endpoint = channel_env
    if state == "detached":
        endpoint.attached = False
    if state == "disabled":
        endpoint.enabled = False
    result = invoke(core.context.execution_runtime, "send_channel_message", {
        "name": "absent" if state == "missing" else "channel-main", "message": "hello",
    }, asynchronous=True)
    assert not result.ok
    assert metadata(result)["effect"] == "not_started"
    assert metadata(result)["kind"] == "rejected"
    assert metadata(result)["error_code"] == code


def test_channel_queue_failure_preserves_cause_in_event_and_health(channel_env, monkeypatch):
    _, channel, endpoint = channel_env
    monkeypatch.setattr(endpoint, "send_reply", lambda *a: fail_with_cause("queued send failed"))
    asyncio.run(channel.send_message("channel-main", "hello"))
    events = endpoint.flush_outbox()
    assert len(events) == 1
    assert "transport root cause" in events[0].payload["reason"]
    assert events[0].payload["reason_code"] == "remote_failure"
    assert "private-value" not in events[0].payload["reason"]
    assert "transport root cause" in endpoint.last_delivery_error


def test_provider_attach_preserves_original_failed_result(channel_env, monkeypatch):
    core, _, _ = channel_env
    provider = core.context.module_registry.get("channel").introspection_provider
    manager = provider.provider_manager
    original = CapabilityResult(status=RuntimeStatus.ERROR, text="provider summary",
        llm_text="provider full diagnostic", structured={"error_code": "auth_denied", "detail": "remote reason"})
    monkeypatch.setattr(manager, "_provider_id_for_endpoint", lambda _: "test-provider")
    monkeypatch.setattr(manager.repository, "get", lambda _: SimpleNamespace())
    monkeypatch.setattr(manager, "_ensure_hub", lambda *a: None)
    monkeypatch.setattr(manager, "_ensure_provider_loaded", lambda _: SimpleNamespace(attach_endpoint=lambda *a: original))
    result = manager.attach_endpoint("channel-main")
    assert result is original


def make_handler(repository=None):
    manager = ProactiveManager(repository=repository)
    # Hydrate rather than persist; repository faults below concern execution.
    manager.hydrate(ProactiveDefinition(proactive_id="job", goal="run check"))
    runner = ProactiveRunner(repository=repository)
    handler = ProactiveTriggerHandler(manager, runner)
    core = SimpleNamespace(state=SimpleNamespace(diagnostics=[]),
        turn_manager=SimpleNamespace(cleanup_interrupted=Mock()),
        run_turn_continuation_async=AsyncMock())
    continuation = SimpleNamespace(turn_id="job-turn")
    trigger = ProactiveTriggerEvent(proactive_id="job", trigger_kind="manual")
    return handler, core, trigger, continuation


def test_proactive_failure_retains_cause_in_read_latest_run(env):
    runtime, manager, _ = env
    manager.create_task(proactive_id="job", goal="check")
    handler, core, trigger, continuation = make_handler(manager.repository)
    async def run(_):
        fail_with_cause("proactive execution failed")
    core.run_turn_continuation_async = run
    assert asyncio.run(handler._run_trigger_guarded_async(core, trigger, continuation)) == "failed"
    result = proactive_invoke(runtime, "read_latest_proactive_run", {"name": "job"})
    assert result.ok
    assert "transport root cause" in result.llm_text
    assert "private-value" not in result.llm_text
    assert result.structured["run"]["status"] == "failed"


@pytest.mark.parametrize("stage", ["begin_run", "complete_run", "update_schedule_state"])
def test_proactive_recording_failures_are_observable(stage):
    repository = SimpleNamespace(begin_run=Mock(return_value="run-id"), complete_run=Mock(),
        fail_run=Mock(), update_schedule_state=Mock())
    getattr(repository, stage).side_effect = lambda *a, **k: fail_with_cause(f"{stage} failed")
    handler, core, trigger, continuation = make_handler(repository)
    core.run_turn_continuation_async.return_value = SimpleNamespace(turn_id="job-turn", final_reply="done", reply_texts=())
    result = asyncio.run(handler._run_trigger_guarded_async(core, trigger, continuation))
    assert result == "failed"
    assert stage in core.state.diagnostics[-1]["error"]
    assert "transport root cause" in core.state.diagnostics[-1]["error"]
    assert not any("final_reply" in item for item in handler.runner.results)


def test_proactive_cleanup_and_failure_storage_do_not_replace_original_failure():
    repository = SimpleNamespace(begin_run=Mock(return_value="run-id"), fail_run=Mock(
        side_effect=lambda *a, **k: fail_with_cause("failure storage failed")))
    handler, core, trigger, continuation = make_handler(repository)
    core.run_turn_continuation_async.side_effect = lambda *a: fail_with_cause("original run failure")
    core.turn_manager.cleanup_interrupted.side_effect = lambda *a, **k: fail_with_cause("cleanup failed")
    assert asyncio.run(handler._run_trigger_guarded_async(core, trigger, continuation)) == "failed"
    diagnostic = core.state.diagnostics[-1]["error"]
    assert all(text in diagnostic for text in ("original run failure", "cleanup failed", "failure storage failed"))


def test_completed_proactive_run_does_not_imply_delivery(env):
    runtime, manager, _ = env
    manager.create_task(proactive_id="job", goal="check")
    run_id = manager.repository.begin_run(ProactiveTriggerEvent(proactive_id="job", trigger_kind="manual"))
    manager.repository.complete_run(run_id, turn_id="job-turn", final_reply="done")
    result = proactive_invoke(runtime, "read_latest_proactive_run", {"name": "job"})
    assert result.structured["run"]["completion_scope"] == "turn_execution"
    assert result.structured["run"]["delivery_confirmation"] == "not_recorded"


@pytest.mark.parametrize("destination", ["runtime_missing", "endpoint_missing", "socket_target_missing"])
def test_unresolved_proactive_output_is_recorded_as_failure(destination):
    from pal.foundation import EventEnvelope
    from pal.shared import EventKind
    repository = SimpleNamespace(begin_run=Mock(return_value="run-id"), fail_run=Mock())
    handler, core, trigger, _ = make_handler(repository)
    handler.manager.hydrate(ProactiveDefinition(proactive_id="job", goal="report", out_channel_id="out"))
    endpoint = SimpleNamespace(endpoint=SimpleNamespace(channel_kind="socket"), derive_default_reply_target=lambda: {})
    channel = SimpleNamespace(get_endpoint=lambda _: endpoint if destination == "socket_target_missing" else None)
    core.context = SimpleNamespace(require_port=lambda _: core,
        port_registry={} if destination == "runtime_missing" else {"channel:channel": channel})
    core.turn_execution_options = lambda: {}
    event = EventEnvelope(event_kind=EventKind.PROACTIVE_TRIGGER, source_kind="proactive", payload=trigger)
    assert handler.handle(event, core.context) == []
    core.run_turn_continuation_async.assert_not_called()
    assert "Proactive output channel 'out'" in repository.fail_run.call_args.kwargs["error_text"]


@pytest.mark.parametrize("alias", ["read_latest_proactive_run", "list_proactive_runs"])
@pytest.mark.parametrize("history_read_fails", [False, True])
def test_history_inspection_exposes_failure_when_begin_run_cannot_be_saved(env, monkeypatch, alias, history_read_fails):
    runtime, manager, _ = env
    manager.create_task(proactive_id="job", goal="check")
    from pal.core import PalCore
    from pal.execution import register_with_core as register_execution
    from pal.proactive import register_with_core as register_proactive
    handler, core, trigger, continuation = make_handler(manager.repository)
    monkeypatch.setattr(manager.repository, "begin_run", lambda *a: fail_with_cause("begin run storage unavailable"))
    assert asyncio.run(handler._run_trigger_guarded_async(core, trigger, continuation)) == "failed"
    model_core = PalCore()
    register_execution(model_core.context)
    register_proactive(model_core.context, manager, handler.runner)
    model_core.publish_module_capabilities("execution")
    model_core.publish_module_capabilities("proactive")
    if history_read_fails:
        for method in ("latest_run", "list_runs"):
            monkeypatch.setattr(manager.repository, method, lambda *a, **k: fail_with_cause("history read unavailable"))
    try:
        result = proactive_invoke(model_core.context.execution_runtime, alias, {"name": "job"})
    finally:
        model_core.close()
    assert result.ok is not history_read_fails, result.llm_text
    assert "begin run storage unavailable" in result.llm_text
    assert "transport root cause" in result.llm_text
    assert "private-value" not in result.llm_text
    assert "runtime_failures" in result.llm_text
    if history_read_fails:
        assert "history read unavailable" in result.llm_text
        assert metadata(result)["effect"] == "none"


@pytest.mark.parametrize("operation", ["register", "delete"])
def test_proactive_persistence_failure_preserves_registered_task(env, monkeypatch, operation):
    runtime, manager, _ = env
    manager.create_task(proactive_id="job", goal="check", schedule={"cadence": "manual"})
    before = manager.registered["job"]
    due_before = dict(manager.schedule_engine.next_due_by_proactive_id)
    method = "upsert_definition" if operation == "register" else "delete_definition"
    monkeypatch.setattr(manager.repository, method, lambda *a, **k: fail_with_cause("definition storage unavailable"))
    alias = "disable_proactive_task" if operation == "register" else "delete_proactive_task"
    result = proactive_invoke(runtime, alias, {"name": "job"})
    assert not result.ok and "transport root cause" in result.llm_text
    assert manager.registered["job"] == before
    assert manager.schedule_engine.next_due_by_proactive_id == due_before


def test_cancellation_survives_secondary_cleanup_failure():
    handler, core, trigger, continuation = make_handler()
    core.run_turn_continuation_async.side_effect = asyncio.CancelledError("shutdown requested")
    core.turn_manager.cleanup_interrupted.side_effect = lambda *a, **k: fail_with_cause("cleanup failed")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(handler._run_trigger_guarded_async(core, trigger, continuation))
    assert "shutdown requested" in core.state.diagnostics[-1]["error"]
    assert "cleanup failed" in core.state.diagnostics[-1]["error"]


def test_attachment_precondition_has_honest_receipt(channel_env, tmp_path):
    core, _, _ = channel_env
    result = invoke(core.context.execution_runtime, "send_channel_attachment",
        {"path": str(tmp_path / "missing")}, asynchronous=True)
    assert not result.ok
    assert metadata(result)["effect"] == "not_started"
    assert metadata(result)["error_code"] == "file_not_found"


def test_attachment_inactive_turn_is_rejected_before_queueing(channel_env, tmp_path):
    core, _, _ = channel_env
    path = tmp_path / "report.txt"
    path.write_text("report")
    result = invoke(core.context.execution_runtime, "send_channel_attachment",
        {"path": str(path)}, asynchronous=True)
    assert metadata(result)["effect"] == "not_started"
    assert metadata(result)["error_code"] == "turn_not_active"


def test_channel_inventory_exposes_durable_state_failure(channel_env, monkeypatch):
    core, channel, _ = channel_env
    channel.publish_endpoint("channel-main")
    provider = core.context.module_registry.get("channel").introspection_provider
    monkeypatch.setattr(provider.repository, "get", lambda *a: fail_with_cause("endpoint state unavailable"))
    result = invoke(core.context.execution_runtime, "list_channel_endpoints", {})
    assert not result.ok
    assert "transport root cause" in result.llm_text
    assert "channel-main" in result.llm_text
    assert metadata(result)["effect"] == "none"


def test_channel_health_projection_keeps_text_receipt_and_last_delivery_error(channel_env):
    from pal.execution.tool_facade import EffectOutcome, EffectReceipt
    core, _, endpoint = channel_env
    manager = core.context.module_registry.get("channel").introspection_provider.provider_manager
    endpoint.last_delivery_error = "earlier socket writer failure"
    original = CapabilityResult(status=RuntimeStatus.ERROR, text="health summary", llm_text="full provider health diagnostic",
        effect_receipt=EffectReceipt(outcome=EffectOutcome.NONE), recovery_hint="repair health probe")
    result = manager._with_endpoint_hub("channel-main", original)
    assert "full provider health diagnostic" in result.llm_text
    assert "earlier socket writer failure" in result.llm_text
    assert result.effect_receipt == original.effect_receipt
    assert result.recovery_hint == original.recovery_hint


def test_provider_attach_keeps_primary_and_cleanup_errors(channel_env, monkeypatch):
    core, _, _ = channel_env
    manager = core.context.module_registry.get("channel").introspection_provider.provider_manager
    monkeypatch.setattr(manager, "_provider_id_for_endpoint", lambda _: "test-provider")
    monkeypatch.setattr(manager.repository, "get", lambda _: SimpleNamespace())
    monkeypatch.setattr(manager, "_ensure_hub", lambda *a: None)
    monkeypatch.setattr(manager, "_ensure_provider_loaded", lambda *a: fail_with_cause("provider load failed"))
    monkeypatch.setattr(manager, "_unload_provider_if_idle", lambda *a: fail_with_cause("provider cleanup failed"))
    result = manager.attach_endpoint("channel-main")
    assert all(text in result.llm_text for text in ("provider load failed", "provider cleanup failed", "transport root cause"))
    assert "private-value" not in result.llm_text


def test_channel_module_lifecycle_owner_preserves_model_facing_error(channel_env):
    core, _, _ = channel_env
    provider = core.context.module_registry.get("channel").introspection_provider
    raw = CapabilityResult(status=RuntimeStatus.ERROR, text="short lifecycle summary",
        llm_text="complete lifecycle diagnostic", structured={"detail": "lifecycle structured cause"})
    result = provider._owner_result("channel.endpoint:channel-main", "channel-main", raw, fresh_instance=False)
    assert "complete lifecycle diagnostic" in result.error
    assert "lifecycle structured cause" in result.error


@pytest.mark.parametrize("method", ["complete_run", "fail_run"])
def test_missing_proactive_run_is_not_silently_accepted(env, method):
    _, manager, _ = env
    arguments = {"turn_id": "turn", "final_reply": "done"} if method == "complete_run" else {"error_text": "failure"}
    with pytest.raises(LookupError, match="missing proactive run"):
        getattr(manager.repository, method)("missing-run", **arguments)


def test_delivery_failure_does_not_claim_retry_is_safe():
    from pal.failure.handler import FailureEventHandler
    from pal.foundation import EventEnvelope
    from pal.shared import EventKind
    core = SimpleNamespace(handle_failure_async=AsyncMock())
    handler = FailureEventHandler(core)
    event = EventEnvelope(event_kind=EventKind.REPLY_FAILED, source_kind="channel",
        payload={"endpoint_id": "out", "reason": "connection lost after send"})
    asyncio.run(handler.handle(event, None))
    signal = core.handle_failure_async.call_args.args[0]
    assert signal.safe_to_retry is False


@pytest.mark.parametrize("alias,args", [
    ("upsert_proactive_task", {"name": "invalid", "goal": "check", "schedule": {"cadence": "invalid"}}),
    ("disable_proactive_task", {"name": "missing"}),
    ("delete_proactive_task", {"name": "missing"}),
])
def test_proactive_management_preconditions_report_not_started(env, alias, args):
    runtime, _, _ = env
    result = proactive_invoke(runtime, alias, args)
    assert not result.ok
    assert metadata(result)["kind"] == "rejected"
    assert metadata(result)["effect"] == "not_started"
    assert metadata(result)["retry"] == "correct_input"


@pytest.mark.parametrize("alias", ["disable_channel_endpoint", "attach_channel_endpoint", "detach_channel_endpoint"])
def test_channel_management_missing_endpoint_reports_not_started(channel_env, monkeypatch, alias):
    core, _, _ = channel_env
    provider = core.context.module_registry.get("channel").introspection_provider
    monkeypatch.setattr(provider.repository, "get", lambda *a: None)
    result = invoke(core.context.execution_runtime, alias, {"name": "missing"})
    assert not result.ok
    assert metadata(result)["kind"] == "rejected"
    assert metadata(result)["effect"] == "not_started"
    assert metadata(result)["retry"] == "correct_input"
