"""Workflow archival must agree with the model's search and status views."""
from __future__ import annotations

import pytest

from pal.bunshin.contracts import ActionEnvelope, AggregateType
from pal.bunshin.workflow_capabilities import BunshinPublicProvider
from pal.execution.contracts import CapabilityCall
from pal.shared import RuntimeStatus


TITLE = "Archive contract regression"
META = {"actor_id": "nathan", "channel_id": "socket:test"}


@pytest.fixture
def provider(tmp_path):
    result = BunshinPublicProvider(tmp_path)
    result.service.start_workflow({
        "title": TITLE, "profile": "software_engineering.v2_coder", "goal": "Verify archive views",
        "task_spec": {"objective": "Verify archive views"}, "actor": "nathan",
        "workspace": {"kind": "new_project", "project_name": "archive-contract"},
        "delivery_binding": {"channel_id": "socket_test", "channel_kind": "socket",
            "reply_target": {"session_id": "test-session", "request_id": "test-request"},
            "control_scope_key": "socket:socket_test:test-session"},
    })
    return result


def call(provider, method, **args):
    return getattr(provider, method)(CapabilityCall(name=method, meta=META, args=args))


def cancel(provider):
    service = provider.service
    task_id, workflow_id = service.resolve_task_workflow_selector(selector=TITLE, actor="nathan", include_terminal=True)
    service.control_workflow(workflow_id=workflow_id, command="cancel", actor="nathan", source_channel="socket:test")
    workflow = service.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
    if workflow.state != "CANCELLED":
        service.repository.transitions.dispatch(ActionEnvelope(action_type="CHILDREN_CANCELLED",
            workflow_id=workflow_id, aggregate_type=AggregateType.WORKFLOW, aggregate_id=workflow_id,
            actor="test", expected_version=workflow.version, idempotency_key="test-children-cancelled", payload={}))
    return task_id, workflow_id


def test_archive_preserves_task_hides_workflow_and_reports_archived_status(provider):
    task_id, workflow_id = cancel(provider)
    before = call(provider, "search_tasks")
    assert len(before.structured["tasks"][0]["workflows"]) == 1
    archived = call(provider, "archive_workflow", task=TITLE)
    assert archived.status == RuntimeStatus.OK, archived.llm_text
    assert archived.structured["archived"] is True
    assert archived.structured["archive_scope"] == "workflow"
    assert archived.structured["task_state"] == "ACTIVE"

    after = call(provider, "search_tasks")
    assert after.structured["count"] == 1
    assert after.structured["tasks"][0]["state"] == "ACTIVE"
    assert after.structured["tasks"][0]["workflows"] == []
    history = call(provider, "search_tasks", include_archived=True)
    assert history.structured["tasks"][0]["workflows"][0]["archived"] is True
    status = call(provider, "workflow_status", task=TITLE)
    assert status.status == RuntimeStatus.OK, status.llm_text
    assert status.structured["workflow"]["archived"] is True
    assert status.structured["workflow"]["next_legal_action"] == []
    assert '"archived":true' in status.llm_text
    task = provider.service.repository.snapshots.read_snapshot(AggregateType.TASK, task_id)
    assert task.state == "ACTIVE"
    again = call(provider, "archive_workflow", task=TITLE)
    assert again.status == RuntimeStatus.OK, again.llm_text
    assert again.structured["archived"] is True
    assert again.structured["already_archived"] is True
    assert again.effect_receipt.outcome.value == "none"


def test_active_workflow_archive_refusal_keeps_search_and_state(provider):
    result = call(provider, "archive_workflow", task=TITLE)
    assert result.status != RuntimeStatus.OK
    assert "only terminal V2 workflows" in result.llm_text
    search = call(provider, "search_tasks")
    assert len(search.structured["tasks"][0]["workflows"]) == 1
    status = call(provider, "workflow_status", task=TITLE)
    assert status.structured["workflow"]["archived"] is False
    assert status.structured["workflow"]["next_legal_action"]


def test_archived_workflow_does_not_prevent_reusing_its_task(provider):
    task_id, old_workflow_id = cancel(provider)
    assert call(provider, "archive_workflow", task=TITLE).status == RuntimeStatus.OK
    service = provider.service
    service.start_workflow({"task_id": task_id, "actor": "nathan", "goal": "Run the next revision",
                            "task_spec": {"objective": "Run the next revision"}})
    search = call(provider, "search_tasks")
    workflows = search.structured["tasks"][0]["workflows"]
    assert len(workflows) == 1 and workflows[0]["archived"] is False
    history = call(provider, "search_tasks", include_archived=True)
    assert len(history.structured["tasks"][0]["workflows"]) == 2
    status = call(provider, "workflow_status", task=TITLE)
    assert status.structured["workflow"]["archived"] is False
    assert status.structured["workflow"]["next_legal_action"]


def test_archive_receipt_reports_real_task_state(provider):
    task_id, workflow_id = cancel(provider)
    service = provider.service
    service.archive_task({"task_id": task_id, "actor": "nathan"})
    archived = service.archive_workflow(workflow_id=workflow_id, actor="nathan")
    assert archived["task_state"] == "ARCHIVED"
    assert archived["archive_scope"] == "workflow"
    assert service.search_task_ledger({"actor": "nathan"})["tasks"] == []
    assert service.search_task_ledger({"actor": "nathan", "include_archived": True})["tasks"][0]["workflows"][0]["archived"]
