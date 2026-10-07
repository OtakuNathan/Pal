"""Replan compares both generations from one workflow observation."""
from types import SimpleNamespace
from unittest.mock import Mock

from pal.bunshin import git_replan
from pal.bunshin.contracts import AggregateSnapshot, AggregateType
from pal.bunshin.graph_executor import GraphDiff, NodeReuseDecision, NodeReuseKind


def test_concurrent_projection_change_cannot_skip_module_replacement(monkeypatch):
    def node(epoch, responsibility):
        return AggregateSnapshot(
            aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=epoch + ':module',
            workflow_id='workflow', state='BLOCKED_BY_DEPS', version=1,
            created_at='2026-09-30T00:00:00Z', updated_at='2026-09-30T00:00:00Z',
            payload={'module_name': 'module', 'node_kind': 'unit', 'epoch_id': epoch,
                     'module_responsibility': responsibility, 'unit_contract_ref': {}},
        )

    source, target = node('old', 'previous responsibility'), node('new', 'new responsibility')
    # A subsequent observation can differ after a concurrent projection update.
    # Reconciliation must still act on the complete pair it initially observed.
    repository = SimpleNamespace(
        queries=SimpleNamespace(list_workflow_snapshots=Mock(side_effect=[[source, target], []])),
        snapshots=SimpleNamespace(read_snapshot=Mock(return_value=target)),
    )
    recreated = Mock()
    monkeypatch.setattr(git_replan, '_recreate_replaced_module_worktree', recreated)
    monkeypatch.setattr(git_replan, '_retire_removed_module_resources', Mock())
    git_replan._reconcile_skeleton_module_identities(
        repository=repository,
        contracts=SimpleNamespace(artifacts=SimpleNamespace(read_json=lambda ref: {})),
        workflow_id='workflow', source_epoch_id='old', target_epoch_id='new',
        source_manifest_ref=None, target_manifest_ref=None,
        target_graph=SimpleNamespace(nodes={'module': object()}, execution_predecessors=lambda name: ()),
        graph_diff=GraphDiff(1, 2, {'module': NodeReuseDecision(
            'module', NodeReuseKind.CREATE, False, False, 'responsibility changed',
        )}),
        actor='test',
    )
    recreated.assert_called_once_with(
        repository=repository, workflow_id='workflow', source_node=source, target_node=target,
    )
