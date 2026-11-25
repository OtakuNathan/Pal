"""Real outbox claims are wake-ups, not obsolete control commands."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from pal.bunshin.contracts import AggregateType
from pal.bunshin.orchestration import BunshinOutboxProcessor
from pal.bunshin.semantic_orchestration.orchestrator import SemanticOrchestrator
from pal.bunshin.service import BunshinWorkflowService
from pal.bunshin.storage.transitions import TransitionsStore
from tests.test_bunshin_unchanged_dependency_baseline import ExistingRepositoryCase


def epoch(case):
    return case.repository.snapshots.read_snapshot(AggregateType.EXECUTION_EPOCH, case.epoch_id)


def business_state(case):
    return (
        case.repository.queries.list_workflow_snapshots(case.workflow_id),
        case.repository.cycles.read_graph_execution(workflow_id=case.workflow_id),
    )


async def replay_expired_claim(case, effect):
    # The original process died (or lost its acknowledgement). A replacement
    # claims exactly the same persisted effect, including its causal context.
    with case.repository.database.write_connection() as connection:
        connection.execute(
            "UPDATE bunshin_outbox SET locked_until=? WHERE effect_id=?",
            ("2000-01-01T00:00:00+00:00", effect["effect_id"]),
        )
    service = BunshinWorkflowService(case.service.runtime_root)
    processor = BunshinOutboxProcessor(
        service, semantic_effects=SemanticOrchestrator(service), worker_id="replacement-manager",
    )
    claim = next(
        item for item in service.repository.outbox_claims.claim_outbox(processor.worker_id, limit=1000)
        if item["effect_id"] == effect["effect_id"]
    )
    assert await processor._process_effect(claim) == "completed"
    case.assert_receipt(claim)


async def admit_producer(case):
    case.scheduler.schedule_ready_nodes(workflow_id=case.workflow_id, epoch_id=case.epoch_id)
    await case.process(case.node_ids["manifest_model"], "admit_implementation_role")
    node = case.node("manifest_model")
    assert node.state == "PRODUCING"
    cycle = case.repository.cycles.read_graph_execution(workflow_id=case.workflow_id).cycles["manifest_model"]
    assert cycle.state.value == "PRODUCING"


@pytest.mark.parametrize("scope", ["workflow", "epoch"])
@pytest.mark.parametrize("next_pause", [False, True])
def test_old_pause_cannot_retire_a_resumed_assignment(tmp_path, scope, next_pause):
    async def scenario():
        case = ExistingRepositoryCase(tmp_path)
        case.service.control_workflow(
            workflow_id=case.workflow_id, command="pause", actor="test", source_channel="test",
        )
        workflow_pause = case.claim(case.workflow_id, "propagate_pause")
        # Perform the original request, leaving its acknowledgement absent.
        await case.processor.execute_effect(workflow_pause)
        epoch_pause = case.claim(case.epoch_id, "pause_epoch_nodes")
        await case.processor.execute_effect(epoch_pause)
        for node in case.nodes().values():
            if node.state == "PAUSE_REQUESTED":
                await case.process(node.aggregate_id, "pause_role")
        assert case.workflow().state == epoch(case).state == "PAUSED"
        case.service.resume_workflow(workflow_id=case.workflow_id, actor="test", source_channel="test")
        await case.process(case.workflow_id, "propagate_resume")
        await admit_producer(case)
        old = workflow_pause if scope == "workflow" else epoch_pause
        if not next_pause:
            before = business_state(case)
            await replay_expired_claim(case, old)
            assert business_state(case) == before
            assert case.node().state == "PRODUCING"
            return

        # An old wake-up may project a *new* legitimate pause, but it must
        # use the new parent/child versions, not the old action dedup key.
        case.service.control_workflow(
            workflow_id=case.workflow_id, command="pause", actor="test", source_channel="test",
        )
        if scope == "epoch":
            case.dispatch(AggregateType.EXECUTION_EPOCH, case.epoch_id, "REQUEST_PAUSE")
        await replay_expired_claim(case, old)
        assert case.workflow().state == "PAUSE_REQUESTED"
        assert epoch(case).state == "PAUSE_REQUESTED"
        assert case.node().state == "PAUSE_REQUESTED"
        graph = case.repository.cycles.read_graph_execution(workflow_id=case.workflow_id)
        assert graph.cycles["manifest_model"].state.value == "PAUSE_REQUESTED"

    asyncio.run(scenario())


@pytest.mark.parametrize("scope", ["workflow", "epoch"])
def test_old_triage_freeze_cannot_stop_work_after_resolution(tmp_path, scope):
    async def scenario():
        case = ExistingRepositoryCase(tmp_path)
        kind, identity, effect_type = (
            (AggregateType.WORKFLOW, case.workflow_id, "freeze_workflow_children")
            if scope == "workflow" else
            (AggregateType.EXECUTION_EPOCH, case.epoch_id, "freeze_epoch_nodes")
        )
        case.dispatch(kind, identity, "ENTER_TRIAGE")
        old = case.claim(identity, effect_type)
        # An operator can resolve a triage before the freeze delivery arrives.
        result = case.service.resolve_triage(
            workflow_id=case.workflow_id, actor="test", source_channel="test", resolution="offline fault repaired",
        )
        assert result["status"] == "triage_resolved"
        await case.process(identity, "reconcile_workflow" if scope == "workflow" else "reconcile_execution_epoch")
        await admit_producer(case)
        before = business_state(case)
        await replay_expired_claim(case, old)
        assert business_state(case) == before

    asyncio.run(scenario())


def test_old_resume_reconciles_the_current_pause(tmp_path):
    async def scenario():
        case = ExistingRepositoryCase(tmp_path)
        case.service.control_workflow(
            workflow_id=case.workflow_id, command="pause", actor="test", source_channel="test",
        )
        await case.process(case.workflow_id, "propagate_pause")
        for node in case.nodes().values():
            if node.state == "PAUSE_REQUESTED":
                await case.process(node.aggregate_id, "pause_role")
        assert case.workflow().state == "PAUSED"
        case.service.resume_workflow(workflow_id=case.workflow_id, actor="test", source_channel="test")
        old = case.claim(case.workflow_id, "propagate_resume")
        await case.processor.execute_effect(old)
        await admit_producer(case)
        case.service.control_workflow(
            workflow_id=case.workflow_id, command="pause", actor="test", source_channel="test",
        )
        await replay_expired_claim(case, old)
        assert case.workflow().state == epoch(case).state == "PAUSE_REQUESTED"
        assert case.node().state == "PAUSE_REQUESTED"

    asyncio.run(scenario())


@pytest.mark.parametrize("scope", ["workflow", "epoch"])
def test_completed_cancel_replay_only_acknowledges(tmp_path, scope):
    async def scenario():
        case = ExistingRepositoryCase(tmp_path)
        case.service.control_workflow(
            workflow_id=case.workflow_id, command="cancel", actor="test", source_channel="test",
        )
        workflow_cancel = case.claim(case.workflow_id, "propagate_cancel")
        await case.processor.execute_effect(workflow_cancel)
        epoch_cancel = case.claim(case.epoch_id, "cancel_epoch_nodes")
        await case.processor.execute_effect(epoch_cancel)
        for node in case.nodes().values():
            if node.state == "CANCEL_REQUESTED":
                await case.process(node.aggregate_id, "cancel_role")
        assert case.workflow().state == epoch(case).state == "CANCELLED"
        before = business_state(case)
        await replay_expired_claim(case, workflow_cancel if scope == "workflow" else epoch_cancel)
        assert business_state(case) == before

    asyncio.run(scenario())


def test_delayed_replan_freeze_does_not_reopen_paused_nodes(tmp_path):
    async def scenario():
        case = ExistingRepositoryCase(tmp_path)
        finding = case.artifacts.put_json({"reason": "offline replan"}, artifact_type="TestFindingArtifact")
        case.dispatch(AggregateType.EXECUTION_EPOCH, case.epoch_id, "REGISTER_REPLAN_FINDING", {
            "finding_artifact_ref": finding.to_dict(), "finding_fingerprint": finding.sha256,
        })
        old = case.claim(case.epoch_id, "freeze_epoch_for_replan")
        case.service.control_workflow(
            workflow_id=case.workflow_id, command="pause", actor="test", source_channel="test",
        )
        await case.process(case.workflow_id, "propagate_pause")
        for node in case.nodes().values():
            if node.state == "PAUSE_REQUESTED":
                await case.process(node.aggregate_id, "pause_role")
        assert case.workflow().state == epoch(case).state == "PAUSED"
        before = business_state(case)
        await replay_expired_claim(case, old)
        assert business_state(case) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("scope", ["workflow", "epoch"])
def test_control_projection_rolls_back_with_its_child_request(tmp_path, scope):
    async def scenario():
        case = ExistingRepositoryCase(tmp_path)
        case.service.control_workflow(
            workflow_id=case.workflow_id, command="pause", actor="test", source_channel="test",
        )
        if scope == "epoch":
            case.dispatch(AggregateType.EXECUTION_EPOCH, case.epoch_id, "REQUEST_PAUSE")
            effect = case.claim(case.epoch_id, "pause_epoch_nodes")
            child_kind = AggregateType.DAG_NODE_RUN
        else:
            effect = case.claim(case.workflow_id, "propagate_pause")
            child_kind = AggregateType.EXECUTION_EPOCH

        before = business_state(case)
        original = TransitionsStore.dispatch

        def interrupt(store, action, **kwargs):
            result = original(store, action, **kwargs)
            if action.aggregate_type == child_kind and action.action_type == "REQUEST_PAUSE":
                raise RuntimeError("injected control projection failure")
            return result

        with patch.object(TransitionsStore, "dispatch", interrupt):
            with pytest.raises(RuntimeError, match="injected control projection failure"):
                await case.processor._execute_mechanical(effect)
        assert business_state(case) == before
        assert await case.processor._process_effect(effect) == "completed"
        assert epoch(case).state == "PAUSE_REQUESTED"
        assert case.node().state == "PAUSE_REQUESTED"

    asyncio.run(scenario())
