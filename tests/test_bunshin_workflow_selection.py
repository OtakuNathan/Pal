"""Current-workflow selection must not depend on a bounded history page."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from pal.bunshin.contracts import AggregateType
from pal.bunshin.service import BunshinWorkflowService


@pytest.fixture
def service(tmp_path):
    result = BunshinWorkflowService(tmp_path)
    result.create_task({
        "task_id": "task", "title": "Selection regression", "objective": "Select current workflow",
        "profile": "software_engineering.v2_coder", "actor": "nathan",
        "workspace": {"kind": "new_project", "project_name": "selection"},
    })
    return result


def row(service, identity, state, *, time=0, archived=False, owner="nathan", task="task"):
    # Fixture storage only: materialize history shapes without running workers.
    with service.repository.database.transaction() as connection:
        connection.execute(
            "INSERT INTO bunshin_aggregate_snapshots "
            "(aggregate_type, aggregate_id, workflow_id, state, version, payload_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
            (AggregateType.WORKFLOW.value, identity, identity, state,
             json.dumps({"task_id": task, "owner": owner, "archived": archived}),
             f"2026-10-09T00:{time:02d}:00Z", f"2026-10-09T00:{time:02d}:00Z"),
        )


def history(service):
    for index in range(25):
        row(service, f"history-{index:02d}", "COMPLETED", time=index + 1, archived=True)


def select(service, include_terminal=True):
    return service.resolve_task_workflow_selector(
        selector="Selection regression", actor="nathan", include_terminal=include_terminal,
    )[1]


@pytest.mark.parametrize("state", ["ACTIVE", "RESTARTING"])
def test_current_workflow_survives_more_than_twenty_newer_archived_rows(service, state):
    row(service, "current", state)
    history(service)
    assert select(service) == "current"


@pytest.mark.parametrize("include_terminal", [False, True])
def test_replacement_precedes_newer_restarting_source_and_history(service, include_terminal):
    row(service, "replacement", "ACTIVE")
    row(service, "source", "RESTARTING", time=40)
    history(service)
    assert select(service, include_terminal) == "replacement"


def test_lone_restarting_is_not_a_control_target(service):
    row(service, "source", "RESTARTING")
    assert select(service, False) == ""


def test_multiple_active_workflows_cannot_hide_behind_history_or_restarting(service):
    row(service, "first", "ACTIVE")
    row(service, "second", "PAUSED")
    for index in range(25):
        row(service, f"source-{index}", "RESTARTING", time=index + 1)
    history(service)
    for include_terminal in (False, True):
        with pytest.raises(ValueError, match="multiple active workflows"):
            select(service, include_terminal)


def test_terminal_fallback_returns_latest_archived_history_only_for_status(service):
    history(service)
    assert select(service) == "history-24"
    assert select(service, False) == ""


def test_no_workflows_returns_empty_identity(service):
    assert select(service) == ""
    assert select(service, False) == ""


def test_selection_is_scoped_to_actor_and_task(service):
    row(service, "mine", "ACTIVE")
    row(service, "other-actor", "ACTIVE", time=30, owner="someone-else")
    row(service, "other-task", "ACTIVE", time=30, task="another-task")
    assert select(service) == "mine"


def test_archived_filter_is_preserved_even_for_invalid_active_history(service):
    row(service, "archived-active", "ACTIVE", archived=True)
    assert select(service) == "archived-active"
    assert select(service, False) == ""


def test_newest_restarting_fallback_and_identity_tie_break(service):
    row(service, "older", "RESTARTING")
    row(service, "z-source", "RESTARTING", time=1)
    row(service, "a-source", "RESTARTING", time=1)
    history(service)
    assert select(service) == "a-source"


def test_database_contract_returns_only_two_ranked_candidates(service):
    row(service, "replacement", "ACTIVE")
    row(service, "source", "RESTARTING")
    history(service)
    candidates = service.repository.search.task_workflow_candidates(
        actor_id="nathan", task_id="task", include_terminal=True,
    )
    assert [item["workflow_id"] for item in candidates] == ["replacement", "source"]


def test_latest_workflow_for_plan_recovery_uses_launch_order(service):
    row(service, "older-plan", "CANCELLED", time=1, archived=True)
    row(service, "latest-plan", "CANCELLED", time=2)
    with service.repository.database.write_connection() as connection:
        connection.execute(
            "UPDATE bunshin_aggregate_snapshots SET updated_at='2026-10-09T00:15:00Z' "
            "WHERE aggregate_id='older-plan'"
        )
    latest = service.repository.search.latest_task_workflow(actor_id="nathan", task_id="task")
    assert latest == {"workflow_id": "latest-plan", "workflow_state": "CANCELLED"}


def test_concurrent_one_click_starts_create_one_task_and_one_workflow(service, monkeypatch):
    request = {
        "title": "Concurrent intake", "actor": "nathan", "goal": "Check the new project",
        "profile": "software_engineering.v2_coder",
        "workspace": {"kind": "new_project", "project_name": "concurrent-intake"},
        "task_spec": {"objective": "Check the new project"},
        "delivery_binding": {
            "channel_id": "test", "channel_kind": "socket", "reply_target": {"session_id": "test"},
        },
    }
    both_missed = Barrier(2, timeout=30)
    prepare = service._prepare_task_creation

    def prepare_concurrently(data):
        both_missed.wait()
        return prepare(data)

    with pytest.raises(ValueError, match="requires task_spec"):
        service.start_workflow({**request, "task_spec": None})
    assert not service.repository.search.search_tasks(query="Concurrent intake", owner="nathan")

    def start(channel):
        try:
            return channel, service.start_workflow({
                **request, "delivery_binding": {**request["delivery_binding"], "channel_id": channel},
            })
        except ValueError as exc:
            return channel, exc

    monkeypatch.setattr(service, "_prepare_task_creation", prepare_concurrently)
    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(start, ("session-a", "session-b")))
    accepted = [(channel, result) for channel, result in outcomes if isinstance(result, dict)]
    rejected = [result for _, result in outcomes if isinstance(result, ValueError)]
    assert len(accepted) == len(rejected) == 1
    assert "Task already has an active workflow" in str(rejected[0])
    task_id, workflow_id = service.resolve_task_workflow_selector(
        selector="Concurrent intake", actor="nathan", include_terminal=False,
    )
    channel, launched = accepted[0]
    assert (task_id, workflow_id) == (launched["task_id"], launched["workflow_id"])
    assert service.repository.delivery_bindings.read_task_delivery(task_id)["current"]["channel_id"] == channel
