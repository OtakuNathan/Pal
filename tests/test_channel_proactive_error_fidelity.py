"""Channel and proactive failures retain causes and execution facts for the LLM."""
from __future__ import annotations
from tests.core_factory import build_core

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pal.channel.contracts import ChannelDeliveryError
from pal.execution.contracts import CapabilityResult
from pal.proactive.contracts import ProactiveDefinition
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
    monkeypatch.setattr(manager.repository, "get", lambda _: SimpleNamespace(detached_at=object()))
    monkeypatch.setattr(manager, "_ensure_hub", lambda *a: None)
    monkeypatch.setattr(manager, "_ensure_provider_loaded", lambda _: SimpleNamespace(attach_endpoint=lambda *a: original))
    result = manager.attach_endpoint("channel-main")
    assert result is original


def test_completed_proactive_run_does_not_imply_delivery(env):
    runtime, manager, _ = env
    manager.create_task(proactive_id="job", goal="check")
    manager.enqueue_trigger(ProactiveTriggerEvent(proactive_id="job", trigger_kind="manual"))
    run_id = manager.repository.pending_background_runs()[0].proactive_run_id
    manager.repository.set_background_state(run_id, "completed", workflow_id="wf", result="done")
    result = proactive_invoke(runtime, "read_proactive_task", {"name": "job"})
    assert result.structured["run"]["completion_scope"] == "workflow_execution"
    assert result.structured["run"]["delivery_confirmation"] == "not_recorded"


@pytest.mark.parametrize("operation", ["register", "delete"])
def test_proactive_persistence_failure_preserves_registered_task(env, monkeypatch, operation):
    runtime, manager, _ = env
    manager.create_task(proactive_id="job", goal="check", schedule={"cadence": "manual"})
    before = manager.registered["job"]
    due_before = dict(manager.schedule_engine.next_due_by_proactive_id)
    method = "upsert_definition" if operation == "register" else "delete_definition"
    monkeypatch.setattr(manager.repository, method, lambda *a, **k: fail_with_cause("definition storage unavailable"))
    action = "disable" if operation == "register" else "delete"
    result = proactive_invoke(runtime, "manage_proactive_task", {"name": "job", "operation": action})
    assert not result.ok and "transport root cause" in result.llm_text
    assert manager.registered["job"] == before
    assert manager.schedule_engine.next_due_by_proactive_id == due_before


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


def test_provider_attach_keeps_primary_and_cleanup_errors(channel_env, monkeypatch, caplog):
    core, _, _ = channel_env
    manager = core.context.module_registry.get("channel").introspection_provider.provider_manager
    monkeypatch.setattr(manager, "_provider_id_for_endpoint", lambda _: "test-provider")
    monkeypatch.setattr(manager.repository, "get", lambda _: SimpleNamespace())
    monkeypatch.setattr(manager, "_ensure_hub", lambda *a: None)
    monkeypatch.setattr(manager, "_ensure_provider_loaded", lambda *a: fail_with_cause("provider load failed"))
    monkeypatch.setattr(manager, "_unload_provider_if_idle", lambda *a: fail_with_cause("provider cleanup failed"))
    result = manager.attach_endpoint("channel-main")
    assert "transport root cause" in result.llm_text
    assert "Provider cleanup failed" in result.llm_text
    assert all(text in caplog.text for text in ("provider load failed", "provider cleanup failed"))
    assert "Traceback" not in result.llm_text
    assert "private-value" not in result.llm_text


def test_channel_module_lifecycle_owner_preserves_model_facing_error(channel_env):
    core, _, _ = channel_env
    provider = core.context.module_registry.get("channel").introspection_provider
    raw = CapabilityResult(status=RuntimeStatus.ERROR, text="short lifecycle summary",
        llm_text="complete lifecycle diagnostic", structured={"detail": "lifecycle structured cause"})
    result = provider._owner_result("channel.endpoint:channel-main", "channel-main", raw, fresh_instance=False)
    assert "complete lifecycle diagnostic" in result.error
    assert "lifecycle structured cause" in result.error


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
    ("manage_proactive_task", {"name": "missing", "operation": "disable"}),
    ("manage_proactive_task", {"name": "missing", "operation": "delete"}),
    ("manage_proactive_task", {"name": "missing", "operation": "enable"}),
])
def test_proactive_management_preconditions_report_not_started(env, alias, args):
    runtime, _, _ = env
    result = proactive_invoke(runtime, alias, args)
    assert not result.ok
    assert metadata(result)["kind"] == "rejected"
    assert metadata(result)["effect"] == "not_started"
    assert metadata(result)["retry"] == "correct_input"


@pytest.mark.parametrize("operation", ["disable", "attach", "detach"])
def test_channel_management_missing_endpoint_reports_not_started(channel_env, monkeypatch, operation):
    core, _, _ = channel_env
    provider = core.context.module_registry.get("channel").introspection_provider
    monkeypatch.setattr(provider.repository, "get", lambda *a: None)
    result = invoke(core.context.execution_runtime, "manage_channel_endpoint", {"name": "missing", "operation": operation})
    assert not result.ok
    assert metadata(result)["kind"] == "rejected"
    assert metadata(result)["effect"] == "not_started"
    assert metadata(result)["retry"] == "correct_input"


def test_background_admission_retries_same_request_and_keeps_error(env):
    from pal.foundation import EventEnvelope
    from pal.proactive.background_dispatch import BackgroundScheduleHandler
    from pal.shared.background_protocol import BACKGROUND_WORKFLOWS
    from pal.shared import EventKind

    runtime, manager, _ = env
    manager.create_task(proactive_id="job", goal="check")
    manager.enqueue_trigger(ProactiveTriggerEvent("job", "manual"))
    repo = manager.repository
    run, = repo.pending_background_runs()
    attempted = []

    class Backend:
        async def start_scheduled_run(self, request):
            attempted.append(request)
            if len(attempted) == 1:
                fail_with_cause("workflow acknowledgement lost")
            return {"workflow_id": "wf"}

    backend = Backend()
    context = SimpleNamespace(port_registry=SimpleNamespace(
        optional=lambda key: backend if key == BACKGROUND_WORKFLOWS else None,
    ))
    event = EventEnvelope(event_kind=EventKind.PROACTIVE_TRIGGER, source_kind="proactive", payload={})
    asyncio.run(BackgroundScheduleHandler(repo).handle(event, context))
    result = proactive_invoke(runtime, "read_proactive_task", {"name": "job"})
    assert result.structured["run"]["status"] == "submitting"
    assert "transport root cause" in result.llm_text
    assert "private-value" not in result.llm_text

    # A fresh handler after reload consumes exactly the persisted request.
    asyncio.run(BackgroundScheduleHandler(repo).handle(event, context))
    assert attempted[0] == attempted[1]
    assert attempted[0]["run_id"] == run.proactive_run_id
    assert repo.latest_run("job").status == "dispatched"


def test_unresolved_background_destination_is_not_silently_discarded(env):
    from pal.foundation import EventEnvelope
    from pal.proactive.background_dispatch import BackgroundScheduleHandler
    from pal.shared import EventKind

    runtime, manager, _ = env
    manager.create_task(proactive_id="job", goal="check", out_channel_id="missing")
    manager.enqueue_trigger(ProactiveTriggerEvent("job", "manual"))
    backend = SimpleNamespace(start_scheduled_run=AsyncMock())
    context = SimpleNamespace(port_registry=SimpleNamespace(
        optional=lambda key: backend, get=lambda key: None,
    ))
    event = EventEnvelope(event_kind=EventKind.PROACTIVE_TRIGGER, source_kind="proactive", payload={})
    asyncio.run(BackgroundScheduleHandler(manager.repository).handle(event, context))
    backend.start_scheduled_run.assert_not_awaited()
    result = proactive_invoke(runtime, "read_proactive_task", {"name": "job"})
    assert result.structured["run"]["status"] == "failed"
    assert "Proactive output channel 'missing'" in result.llm_text


def test_historical_resident_runs_remain_readable_but_not_dispatchable(env):
    from pal.proactive.models import ProactiveRunModel

    runtime, manager, _ = env
    manager.create_task(proactive_id="job", goal="check")
    ProactiveRunModel.create(proactive_run_id="legacy", proactive_id="job", trigger_kind="manual",
                             status="completed", turn_id="old-turn", output_summary="historical result")
    result = proactive_invoke(runtime, "read_proactive_task", {"name": "job"})
    assert result.structured["run"]["turn_id"] == "old-turn"
    assert result.structured["run"]["completion_scope"] == "turn_execution"
    assert result.structured["run"]["output_summary"] == "historical result"
    assert not manager.pending_triggers
