from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from pal.bunshin.v2.storage.cycles import CyclesStore

from pal.bunshin.v2.cycle_protocol import (
    AssignmentKind, CycleAction, CycleAssignment, CycleSlot,
    CycleTransitionError, NodeCycle, NodeCycleState,
)
from pal.bunshin.v2.dependency_repair_protocol import (
    DependencyRepairDeferred, DependencyRepairLedger,
    RepairClosure, RepairIncarnation, RepairIntent,
)
from pal.bunshin.v2.graph_executor import GraphExecution, GraphExecutionState, diff_graphs
from pal.bunshin.v2.graph_protocol import EdgeKind, EdgeSpec, GraphIR, NodeSpec, RoleBinding
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.workflow_runtime import WorkflowCoordinator, _replanned_execution


def _ref(value: str) -> dict:
    return {"sha256": value, "artifact_type": "test.evidence", "schema_version": "1",
            "media_type": "application/json", "byte_size": 1, "durable": True}


def _graph() -> GraphIR:
    names = ("P", "Q", "C", "D", "X", "S", "U", "V")
    nodes = {name: NodeSpec(name=name, responsibility=f"implement {name}",
                           satellite_data={"name": name}, producer_binding=RoleBinding("profile", "coder"),
                           checker_binding=RoleBinding("profile", "verifier"),
                           execution_adapter="software_git.v2", workspace_policy={},
                           output_contract=(name,), is_sink=name == "S") for name in names}
    edges = tuple(EdgeSpec(producer=p, consumer=c, kind=kind, contract_ref=f"{p}-{c}", consumed_outputs=(p,))
                  for p, c, kind in (("P", "Q", EdgeKind.EXECUTION), ("P", "C", EdgeKind.EXECUTION),
                                     ("P", "D", EdgeKind.EXECUTION), ("Q", "D", EdgeKind.EXECUTION),
                                     ("C", "X", EdgeKind.CONTRACT), ("D", "X", EdgeKind.EXECUTION),
                                     ("V", "U", EdgeKind.EXECUTION)))
    return GraphIR(graph_id="graph", generation=1, nodes=nodes, edges=edges,
                   sink="S", source_ref="source", source_map_ref="map")


def _execution() -> GraphExecution:
    graph = _graph()
    cycles = {}
    for name in graph.nodes:
        state = (NodeCycleState.CHECKING if name in {"C", "D", "U"}
                 else NodeCycleState.PRODUCING if name == "X"
                 else NodeCycleState.PRODUCER_READY if name == "S"
                 else NodeCycleState.ACCEPTED)
        slot = CycleSlot.PRODUCER if name == "X" else CycleSlot.CHECKER
        cycles[name] = NodeCycle(cycle_id=f"graph:g1:{name}", node_name=name, generation=1,
                                 state=state, product_ref=f"producer-{name}",
                                 accepted_product_ref=f"accepted-{name}" if state == NodeCycleState.ACCEPTED else "",
                                 active_assignment=CycleAssignment(slot, AssignmentKind.INITIAL, 1, f"input-{name}")
                                 if state in {NodeCycleState.CHECKING, NodeCycleState.PRODUCING} else None)
    return GraphExecution(graph=graph, state=GraphExecutionState.RUNNING, cycles=cycles)


def _member(name: str, *, suffix: str = "") -> RepairIncarnation:
    return RepairIncarnation(node_name=name, cycle_id=f"graph:g1:{name}", generation=1,
                             input_fingerprint=f"input-{name}{suffix}", slot="producer" if name == "X" else "checker",
                             aggregate_id=f"aggregate-{name}", effect_id=f"effect-{name}{suffix}",
                             effect_key=f"effect-key-{name}{suffix}", role_assignment_id=f"assignment-{name}{suffix}",
                             attempt_id=f"attempt-{name}{suffix}", lease_resource=f"resource-{name}",
                             lease_owner=f"owner-{name}{suffix}", fencing_token=1)


def _intent(execution: GraphExecution, source: str = "C", providers: tuple[str, ...] = ("P",)) -> RepairIntent:
    return RepairIntent(graph_id=execution.graph.graph_id, generation=1, generation_hash=execution.graph.generation_hash,
                        source_node=source, source_cycle_id=f"graph:g1:{source}", source_aggregate_id=f"aggregate-{source}",
                        source_assignment_id=f"assignment-{source}", source_input_fingerprint=f"input-{source}",
                        source_product_ref=f"producer-{source}", source_fencing_token=1,
                        candidate_ref=_ref(f"assembled-candidate-{source}"), candidate_digest=f"tree-{source}",
                        packet_ref=_ref(f"packet-{source}"), submission_payload_hash=f"submission-{source}",
                        provider_nodes=providers, pending_verification_ref=_ref(f"pending-{source}"))


def _closure(member: RepairIncarnation, *refs: dict) -> RepairClosure:
    return RepairClosure(incarnation=member, task_closed=True, process_reaped=True,
                         workspace_released=True, lease_released=True, submission_cut=True,
                         receipt_refs=tuple(refs))


def _pending() -> GraphExecution:
    execution = _execution()
    return execution.register_dependency_repair(_intent(execution), frontier=tuple(_member(name) for name in ("C", "D", "X")))


def _drain(execution: GraphExecution) -> GraphExecution:
    pending = execution.dependency_repairs.pending
    assert pending is not None
    for member in tuple(pending.frontier.values()):
        refs = [dict(intent.packet_ref) for intent in pending.intents.values() if intent.source_node == member.node_name]
        execution = execution.close_dependency_repair(_closure(member, *refs))
    return execution


class DependencyRepairProtocolTests(unittest.TestCase):
    def test_initial_software_producers_remain_parallel(self) -> None:
        initial = GraphExecution.start(_graph())
        self.assertEqual(set(initial.runnable_nodes()), set(initial.graph.nodes))

    def test_freezes_actual_closure_with_synthetic_sink(self) -> None:
        execution = _pending()
        self.assertEqual(execution.pending_scope, {"P", "Q", "C", "D", "X", "S"})
        self.assertNotIn("U", execution.pending_scope)
        self.assertNotIn("V", execution.pending_scope)
        self.assertEqual(execution.runnable_nodes(), ())
        self.assertEqual(execution.cycles["C"].state, NodeCycleState.CHECKING)
        self.assertEqual(execution.cycles["D"].state, NodeCycleState.CANCEL_REQUESTED)
        self.assertIsNotNone(execution.cycles["D"].active_assignment)
        self.assertEqual(execution.cycles["X"].state, NodeCycleState.CANCEL_REQUESTED)

    def test_unrelated_ready_producer_is_not_serialized_by_pending_cohort(self) -> None:
        execution = _execution()
        execution = replace(execution, cycles={**execution.cycles, "V": replace(execution.cycles["V"], state=NodeCycleState.REPAIR_READY)})
        pending = execution.register_dependency_repair(_intent(execution), frontier=tuple(_member(name) for name in ("C", "D", "X")))
        self.assertEqual(pending.runnable_nodes(), ("V",))

    def test_assembled_candidate_keeps_separate_graph_product_identity(self) -> None:
        execution = _execution()
        intent = _intent(execution)
        self.assertNotEqual(intent.candidate_ref["sha256"], execution.cycles["C"].product_ref)
        self.assertIsNotNone(execution.register_dependency_repair(intent, frontier=tuple(_member(name) for name in ("C", "D", "X"))).dependency_repairs.pending)
        with self.assertRaisesRegex(ValueError, "source candidate or cycle"):
            execution.register_dependency_repair(replace(intent, source_product_ref="wrong-producer"))

    def test_same_key_altered_candidate_or_packet_metadata_rejected(self) -> None:
        execution = _pending()
        original = next(iter(execution.dependency_repairs.pending.intents.values()))
        self.assertIs(execution.register_dependency_repair(original), execution)
        with self.assertRaisesRegex(ValueError, "changed content"):
            execution.register_dependency_repair(replace(original, candidate_digest="changed-tree"))
        with self.assertRaisesRegex(ValueError, "changed content"):
            execution.register_dependency_repair(replace(original, packet_ref={**original.packet_ref, "byte_size": 2}))

    def test_wrong_generation_or_ineligible_contract_target_rejected(self) -> None:
        execution = _execution()
        with self.assertRaisesRegex(ValueError, "another graph generation"):
            execution.register_dependency_repair(replace(_intent(execution), generation=2))
        for provider in ("V", "C", "S"):
            with self.subTest(provider=provider), self.assertRaisesRegex(ValueError, "execution provider"):
                execution.register_dependency_repair(_intent(execution, providers=(provider,)))
        graph = replace(execution.graph, nodes={**execution.graph.nodes, "P": replace(execution.graph.nodes["P"], producer_binding=RoleBinding("null", reason="contract only"))})
        execution = replace(execution, graph=graph)
        with self.assertRaisesRegex(ValueError, "software-produced"):
            execution.register_dependency_repair(_intent(execution))

    def test_all_admitted_assignments_must_be_in_frontier(self) -> None:
        execution = _execution()
        with self.assertRaisesRegex(ValueError, "omitted admitted node"):
            execution.register_dependency_repair(_intent(execution), frontier=(_member("C"),))
        with self.assertRaisesRegex(ValueError, "receipt does not match"):
            execution.register_dependency_repair(_intent(execution), frontier=tuple(replace(_member(name), fencing_token=2) for name in ("C", "D", "X")))

    def test_join_before_apply_unions_providers_excluding_consumer_targets(self) -> None:
        results = []
        for first in ("C", "D"):
            execution = _execution()
            intents = {"C": _intent(execution), "D": _intent(execution, "D", ("P", "Q"))}
            execution = execution.register_dependency_repair(intents[first], frontier=tuple(_member(name) for name in ("C", "D", "X")))
            old_key = execution.dependency_repairs.pending.key
            execution = execution.register_dependency_repair(intents["D" if first == "C" else "C"])
            with self.assertRaisesRegex(ValueError, "component changed"):
                execution.apply_dependency_repair(cohort_key=old_key)
            execution = _drain(execution)
            key = execution.dependency_repairs.pending.key
            execution, route = execution.apply_dependency_repair(cohort_key=key)
            self.assertEqual(route.node_names, ("P", "Q"))
            self.assertEqual(route.stale_nodes, ("C", "D", "S", "X"))
            self.assertEqual(execution.cycles["Q"].state, NodeCycleState.REPAIR_READY)
            self.assertNotIn("Q", execution.repair_barriers)
            self.assertEqual(execution.runnable_nodes(), ("P", "Q"))
            self.assertEqual(execution.cycles["P"].last_verdict.finding_refs, ("packet-C", "packet-D"))
            self.assertEqual(execution.cycles["Q"].last_verdict.finding_refs, ("packet-D",))
            replayed, second_route = execution.apply_dependency_repair(cohort_key=key)
            self.assertIs(replayed, execution)
            self.assertIsNone(second_route)
            self.assertIs(execution.register_dependency_repair(intents[first]), execution)
            results.append(execution)
        self.assertEqual(results[0], results[1])

    def test_scope_expansion_freezes_new_admissions_and_frontier(self) -> None:
        execution = _execution()
        # Q's independent consumer is outside P's actual closure until the
        # captured D report also identifies Q.
        graph = replace(execution.graph, edges=tuple(edge for edge in execution.graph.edges if (edge.producer, edge.consumer) != ("P", "Q")) + (EdgeSpec("Q", "U", EdgeKind.EXECUTION, "q-u", ("Q",)),))
        execution = replace(execution, graph=graph)
        execution = execution.register_dependency_repair(_intent(execution), frontier=tuple(_member(name) for name in ("C", "D", "X")))
        self.assertNotIn("U", execution.pending_scope)
        with self.assertRaisesRegex(ValueError, "omitted admitted node U"):
            execution.register_dependency_repair(_intent(execution, "D", ("Q",)))
        execution = execution.register_dependency_repair(_intent(execution, "D", ("Q",)), frontier=(_member("U"),))
        self.assertIn("U", execution.pending_scope)
        self.assertEqual(execution.cycles["U"].state, NodeCycleState.CANCEL_REQUESTED)

    def test_pending_outside_source_is_explicit_later_cohort(self) -> None:
        execution = _pending()
        with self.assertRaises(DependencyRepairDeferred):
            execution.register_dependency_repair(_intent(execution, "U", ("V",)), frontier=(_member("U"),))
        execution = _drain(execution)
        execution, _ = execution.apply_dependency_repair(cohort_key=execution.dependency_repairs.pending.key)
        execution = execution.register_dependency_repair(_intent(execution, "U", ("V",)), frontier=(_member("U"),))
        self.assertEqual(execution.pending_scope, {"U", "V", "S"})
        self.assertEqual(len(execution.dependency_repairs.history), 1)

    def test_captured_in_scope_sink_report_expands_even_with_sink_only_overlap(self) -> None:
        execution = _execution()
        sink = replace(execution.cycles["S"], state=NodeCycleState.CHECKING,
                       active_assignment=CycleAssignment(CycleSlot.CHECKER, AssignmentKind.INITIAL, 1, "input-S"))
        execution = replace(execution, cycles={**execution.cycles, "S": sink})
        execution = execution.register_dependency_repair(_intent(execution),
            frontier=tuple(_member(name) for name in ("C", "D", "X", "S")))
        execution = execution.register_dependency_repair(_intent(execution, "S", ("V",)), frontier=(_member("U"),))
        self.assertEqual(execution.dependency_repairs.pending.providers, ("P", "V"))
        self.assertIn("U", execution.pending_scope)
        self.assertIn("V", execution.pending_scope)

    def test_no_apply_before_complete_capture_and_quiescence(self) -> None:
        execution = _pending()
        key = execution.dependency_repairs.pending.key
        with self.assertRaisesRegex(ValueError, "captured and closed"):
            execution.apply_dependency_repair(cohort_key=key)
        execution = execution.close_dependency_repair(_closure(_member("C"), _ref("packet-C")))
        execution = execution.capture_dependency_repair(_member("D").key, receipt_refs=(_ref("late-raw-result"),))
        with self.assertRaisesRegex(ValueError, "omitted a captured"):
            execution.close_dependency_repair(_closure(_member("D")))
        execution = execution.close_dependency_repair(_closure(_member("D"), _ref("late-raw-result")))
        with self.assertRaisesRegex(ValueError, "captured and closed"):
            execution.apply_dependency_repair(cohort_key=key)
        execution = execution.close_dependency_repair(_closure(_member("X")))
        execution, _ = execution.apply_dependency_repair(cohort_key=key)
        self.assertEqual(execution.dependency_repairs.history[0].captures[_member("D").key][0]["sha256"], "late-raw-result")

    def test_source_packet_capture_is_mandatory(self) -> None:
        execution = _pending()
        for member in execution.dependency_repairs.pending.frontier.values():
            execution = execution.close_dependency_repair(_closure(member))
        with self.assertRaisesRegex(ValueError, "source packet has not been captured"):
            execution.apply_dependency_repair(cohort_key=execution.dependency_repairs.pending.key)

    def test_late_capture_for_closed_assignment_is_rejected(self) -> None:
        execution = _pending().close_dependency_repair(_closure(_member("C"), _ref("packet-C")))
        with self.assertRaisesRegex(ValueError, "submission cut"):
            execution.capture_dependency_repair(_member("C").key, receipt_refs=(_ref("late"),))

    def test_proof_requires_every_cleanup_fact_and_exact_owner(self) -> None:
        for field in ("task_closed", "process_reaped", "workspace_released", "lease_released", "submission_cut"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                replace(_closure(_member("C")), **{field: False})
        execution = _pending()
        for changed in ({"fencing_token": 2}, {"lease_owner": "replacement"}, {"role_assignment_id": "replacement"}, {"attempt_id": "replacement"}):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "exact frozen"):
                execution.close_dependency_repair(_closure(replace(_member("C"), **changed)))

    def test_setup_frontier_can_bind_missing_ownership_once(self) -> None:
        execution = _execution()
        setup = replace(_member("X"), role_assignment_id="", attempt_id="", lease_resource="", lease_owner="", fencing_token=0)
        execution = execution.register_dependency_repair(_intent(execution), frontier=(_member("C"), _member("D"), setup))
        execution = execution.update_dependency_repair_frontier((_member("X"),))
        self.assertEqual(execution.dependency_repairs.pending.frontier[setup.key], _member("X"))
        with self.assertRaisesRegex(ValueError, "ownership changed"):
            execution.update_dependency_repair_frontier((replace(_member("X"), fencing_token=2),))
        with self.assertRaisesRegex(ValueError, "another incarnation after freeze"):
            execution.update_dependency_repair_frontier((_member("X", suffix="new"),))

    def test_snapshot_lease_binding_preserves_original_submission_fence(self) -> None:
        execution = _pending()
        original = _member("C")
        rebound = replace(original, snapshot_lease_resource="resource-C",
                          snapshot_lease_owner="snapshot-owner", snapshot_fencing_token=8)
        execution = execution.update_dependency_repair_frontier((rebound,))
        self.assertEqual(rebound.key, original.key)
        self.assertEqual(rebound.fencing_token, 1)
        with self.assertRaisesRegex(ValueError, "exact frozen"):
            execution.close_dependency_repair(_closure(original, _ref("packet-C")))
        execution = execution.close_dependency_repair(_closure(rebound, _ref("packet-C")))
        self.assertEqual(execution.cycles["C"].state, NodeCycleState.STALE)

    def test_later_upstream_cohort_closes_earlier_active_replacement(self) -> None:
        execution = _execution()
        execution = replace(execution, graph=replace(execution.graph, edges=(*execution.graph.edges,
            EdgeSpec("V", "P", EdgeKind.EXECUTION, "v-p", ("V",)))))
        first = _intent(execution)
        execution = execution.register_dependency_repair(first, frontier=tuple(_member(name) for name in ("C", "D", "X")))
        execution = _drain(execution)
        execution, _ = execution.apply_dependency_repair(cohort_key=execution.dependency_repairs.pending.key)
        repaired = execution.cycles["P"].transition(CycleAction.START_PRODUCER,
            assignment=CycleAssignment(CycleSlot.PRODUCER, AssignmentKind.REPAIR, 1, "input-P-new"))
        execution = execution.with_cycle(repaired)
        replacement = replace(_member("P", suffix="-new"), slot="producer", fencing_token=9)
        execution = execution.register_dependency_repair(_intent(execution, "U", ("V",)),
                                                        frontier=(_member("U"), replacement))
        self.assertEqual(execution.cycles["P"].state, NodeCycleState.CANCEL_REQUESTED)
        self.assertEqual(execution.runnable_nodes(), ())
        with self.assertRaisesRegex(ValueError, "exact frozen"):
            execution.close_dependency_repair(_closure(_member("P")))
        execution = _drain(execution)
        execution, route = execution.apply_dependency_repair(cohort_key=execution.dependency_repairs.pending.key)
        self.assertEqual(route.node_names, ("V",))
        self.assertEqual(execution.cycles["P"].state, NodeCycleState.STALE)
        self.assertIn("V", execution.repair_barriers["P"])
        self.assertEqual(execution.dependency_repairs.history[0].intents[first.key], first)
        self.assertIs(execution.register_dependency_repair(first), execution)

    def test_frozen_frontier_cannot_gain_new_setup_without_scope_expansion(self) -> None:
        execution = _pending()
        with self.assertRaisesRegex(ValueError, "after freeze"):
            execution.update_dependency_repair_frontier((_member("P"),))
        with self.assertRaisesRegex(ValueError, "already frozen"):
            execution.register_dependency_repair(_intent(execution, "D", ("P", "Q")), frontier=(_member("P"),))

    def test_verdict_cannot_publish_or_locally_repair_fenced_candidate(self) -> None:
        execution = _pending()
        with self.assertRaisesRegex(ValueError, "without publishing"):
            execution.apply_checker_verdict(current_node="C", accepted=True)
        with self.assertRaisesRegex(ValueError, "admission or verdict"):
            execution.with_cycle(replace(execution.cycles["C"], state=NodeCycleState.ACCEPTED, active_assignment=None))
        execution = execution.refresh()
        self.assertEqual(execution.state, GraphExecutionState.RUNNING)
        self.assertEqual(execution.published_sink_ref, "")

    def test_explicit_stale_control_is_assignment_fenced_and_user_cancel_wins(self) -> None:
        cycle = _execution().cycles["X"]
        requested = cycle.transition(CycleAction.REQUEST_STALE)
        with self.assertRaises(CycleTransitionError):
            requested.transition(CycleAction.STALE_CONFIRMED)
        with self.assertRaises(CycleTransitionError):
            requested.transition(CycleAction.STALE_CONFIRMED, assignment=replace(cycle.active_assignment, input_fingerprint="new"))
        stale = requested.transition(CycleAction.STALE_CONFIRMED, assignment=cycle.active_assignment)
        self.assertEqual(stale.state, NodeCycleState.STALE)
        cancelled = requested.transition(CycleAction.REQUEST_CANCEL)
        with self.assertRaises(CycleTransitionError):
            cancelled.transition(CycleAction.STALE_CONFIRMED, assignment=cycle.active_assignment)
        self.assertEqual(cancelled.transition(CycleAction.CANCELLED).state, NodeCycleState.CANCELLED)

    def test_terminal_cancel_and_replan_supersede_without_repair_release(self) -> None:
        for state in (GraphExecutionState.CANCELLED, GraphExecutionState.REPLAN_REQUIRED):
            execution = replace(_pending(), state=state)
            self.assertIsNone(execution.dependency_repairs.pending)
            self.assertEqual(execution.dependency_repairs.history[0].status, "superseded")
            self.assertEqual(execution.runnable_nodes(), ())
            self.assertEqual(execution.refresh().state, state)
            with self.assertRaisesRegex(ValueError, "superseded"):
                execution.apply_dependency_repair(cohort_key=execution.dependency_repairs.history[0].key)

    def test_replan_cannot_reuse_acceptance_invalidated_by_superseded_cohort(self) -> None:
        source = replace(_pending(), state=GraphExecutionState.REPLAN_REQUIRED)
        target = replace(source.graph, generation=2)
        result = _replanned_execution(source, target, diff_graphs(source.graph, target))
        self.assertEqual(result.cycles["P"].state, NodeCycleState.PRODUCER_READY)
        self.assertEqual(result.cycles["Q"].state, NodeCycleState.PRODUCER_READY)
        self.assertEqual(result.cycles["V"].state, NodeCycleState.ACCEPTED)
        self.assertIsNone(result.dependency_repairs.pending)

    def test_persisted_schema_is_strict_immutable_and_reference_only(self) -> None:
        execution = _pending()
        payload = execution.dependency_repairs.to_dict()
        self.assertEqual(DependencyRepairLedger.from_mapping(json.loads(json.dumps(payload))), execution.dependency_repairs)
        self.assertEqual(DependencyRepairLedger.from_mapping(None), DependencyRepairLedger())
        for malformed in ({}, {**payload, "version": 2}, {**payload, "version": True}, {**payload, "history": {}}, {**payload, "unknown": 0}):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                DependencyRepairLedger.from_mapping(malformed)
        with self.assertRaises(ValueError):
            replace(_intent(_execution()), packet_ref={**_ref("packet"), "findings": []})
        with self.assertRaises(TypeError):
            next(iter(execution.dependency_repairs.pending.intents.values())).candidate_ref["sha256"] = "changed"


class DependencyRepairPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repository = BunshinV2Repository(Path(self.temp.name))
        self.workflow = "workflow"
        self.execution = _pending()
        self.repository.cycles.store_graph_generation(workflow_id=self.workflow, graph=self.execution.graph)
        self.repository.cycles.store_graph_execution(workflow_id=self.workflow, execution=self.execution)

    def test_reload_and_unrelated_mutations_preserve_admission_fence(self) -> None:
        loaded = self.repository.cycles.read_graph_execution(workflow_id=self.workflow)
        self.assertEqual(loaded.graph.generation_hash, self.execution.graph.generation_hash)
        self.assertEqual(loaded.cycles, self.execution.cycles)
        self.assertEqual(loaded.dependency_repairs, self.execution.dependency_repairs)
        # A whole-execution update outside the affected closure keeps evidence.
        loaded = loaded.with_cycle(replace(loaded.cycles["V"], state=NodeCycleState.REPAIR_READY))
        self.repository.cycles.store_graph_execution(workflow_id=self.workflow, execution=loaded)
        reloaded = self.repository.cycles.read_graph_execution(workflow_id=self.workflow)
        self.assertEqual(reloaded.dependency_repairs, self.execution.dependency_repairs)
        self.assertEqual(reloaded.runnable_nodes(), ("V",))

    def test_direct_assignment_is_fenced_but_exact_source_replay_works(self) -> None:
        coordinator = WorkflowCoordinator(self.repository)
        for node, slot in (("P", CycleSlot.PRODUCER), ("S", CycleSlot.PRODUCER), ("D", CycleSlot.CHECKER)):
            with self.subTest(node=node), self.assertRaisesRegex(RuntimeError, "fenced"):
                coordinator.start_assignment(workflow_id=self.workflow, node_name=node, slot=slot,
                                             kind=AssignmentKind.INITIAL, input_fingerprint=f"input-{node}")
        cycle = coordinator.start_assignment(workflow_id=self.workflow, node_name="C", slot=CycleSlot.CHECKER,
                                             kind=AssignmentKind.INITIAL, input_fingerprint="input-C")
        self.assertEqual(cycle, self.execution.cycles["C"])

    def test_direct_checker_requires_current_execution_inputs(self) -> None:
        execution = _drain(self.execution)
        execution, _ = execution.apply_dependency_repair(cohort_key=execution.dependency_repairs.pending.key)
        execution = replace(execution, cycles={**execution.cycles,
                            "P": replace(execution.cycles["P"], state=NodeCycleState.REPAIR_READY),
                            "C": replace(execution.cycles["C"], state=NodeCycleState.CHECKER_READY, active_assignment=None)})
        self.repository.cycles.store_graph_execution(workflow_id=self.workflow, execution=execution)
        with self.assertRaisesRegex(RuntimeError, "fenced"):
            WorkflowCoordinator(self.repository).start_assignment(workflow_id=self.workflow, node_name="C", slot=CycleSlot.CHECKER,
                                                                  kind=AssignmentKind.INITIAL, input_fingerprint="new-input")

    def test_stale_whole_execution_write_cannot_drop_registered_fence(self) -> None:
        with self.assertRaisesRegex(ValueError, "discard pending repair fences"):
            self.repository.cycles.store_graph_execution(workflow_id=self.workflow, execution=_execution())
        self.assertEqual(self.repository.cycles.read_graph_execution(workflow_id=self.workflow).dependency_repairs,
                         self.execution.dependency_repairs)

    def test_workflow_cancel_supersedes_and_retains_active_assignment_until_ack(self) -> None:
        WorkflowCoordinator(self.repository).request_workflow_cancel(workflow_id=self.workflow)
        execution = self.repository.cycles.read_graph_execution(workflow_id=self.workflow)
        self.assertEqual(execution.state, GraphExecutionState.CANCELLED)
        self.assertIsNone(execution.dependency_repairs.pending)
        self.assertEqual(execution.dependency_repairs.history[0].status, "superseded")
        self.assertEqual(execution.cycles["X"].state, NodeCycleState.CANCEL_REQUESTED)
        self.assertIsNotNone(execution.cycles["X"].active_assignment)
        self.assertIsNone(execution.cycles["X"].resume_state)

    def test_legacy_execution_defaults_and_unknown_version_fail_closed(self) -> None:
        with self.repository.database.write_connection() as connection:
            connection.execute("UPDATE bunshin_v2_graph_generations SET execution_json = ?", (json.dumps({"state": "RUNNING"}),))
        self.assertEqual(self.repository.cycles.read_graph_execution(workflow_id=self.workflow).dependency_repairs, DependencyRepairLedger())
        with self.repository.database.write_connection() as connection:
            connection.execute("UPDATE bunshin_v2_graph_generations SET execution_json = ?", (json.dumps({"dependency_repairs": {"version": 99, "pending": None, "history": []}}),))
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.repository.cycles.read_graph_execution(workflow_id=self.workflow)

    def test_execution_and_cycles_share_one_read_snapshot_without_committing_caller(self) -> None:
        original = CyclesStore.read_node_cycles
        observed = []
        def read(store, **kwargs):
            connection = kwargs.get("_connection")
            self.assertIsNotNone(connection)
            self.assertTrue(connection.in_transaction)
            observed.append(connection)
            return original(store, **kwargs)
        with patch.object(CyclesStore, "read_node_cycles", read):
            self.repository.cycles.read_graph_execution(workflow_id=self.workflow)
            with self.repository.transaction() as unit:
                unit.cycles.read_graph_execution(workflow_id=self.workflow)
                with unit.cycles.database.read_connection() as borrowed:
                    self.assertIs(borrowed, observed[-1])
                    self.assertTrue(borrowed.in_transaction)
        self.assertEqual(len(observed), 2)

    def test_old_accepted_projection_cannot_publish_while_repair_pending(self) -> None:
        # A stored admission fence has precedence over legacy state inference.
        with self.repository.database.write_connection() as connection:
            rows = connection.execute("SELECT cycle_id, payload_json FROM bunshin_v2_node_cycles").fetchall()
            for row in rows:
                payload = json.loads(row["payload_json"])
                payload["state"] = "ACCEPTED"
                payload["active_assignment"] = None
                connection.execute("UPDATE bunshin_v2_node_cycles SET payload_json=? WHERE cycle_id=?", (json.dumps(payload), row["cycle_id"]))
        loaded = self.repository.cycles.read_graph_execution(workflow_id=self.workflow)
        self.assertEqual(loaded.state, GraphExecutionState.RUNNING)
        self.assertEqual(loaded.published_sink_ref, "")
        self.assertEqual(loaded.refresh().state, GraphExecutionState.RUNNING)


if __name__ == "__main__":
    unittest.main()
