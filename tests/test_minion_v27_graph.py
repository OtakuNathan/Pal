from __future__ import annotations

import asyncio
import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import yaml

from pal.minion.v2.architecture_templates import ArchitectureTemplateCompiler
from pal.minion.v2.contract_protocol import validate_contract_payload
from pal.minion.v2.contracts import AggregateSnapshot, AggregateType
from pal.minion.v2.coroutine_runtime import CoroutineRunSemaphore
from pal.minion.v2.cycle_protocol import (
    AssignmentKind,
    CycleAction,
    CycleAssignment,
    CycleSlot,
    CycleTransitionError,
    NodeCycle,
    NodeCycleState,
    PlanCycle,
    PlanCycleState,
    CycleVerdict,
)
from pal.minion.v2.graph_compiler import (
    GraphCompilationError,
    GraphCompileBindings,
    GraphCompiler,
    build_yaml_source_map,
)
from pal.minion.v2.graph_executor import (
    FindingClass,
    GraphExecution,
    GraphExecutionState,
    NodeReuseKind,
    diff_graphs,
)
from pal.minion.v2.graph_satellites import (
    FamilyGraphSatelliteProjector,
)
from pal.minion.v2.graph_protocol import (
    EdgeKind,
    EdgeSpec,
    GraphIR,
    NodeSpec,
    RoleBinding,
    graph_ir_from_mapping,
)
from pal.minion.v2.role_runtime import RoleSupervisor
from pal.minion.v2.repository import MinionV2Repository
from pal.minion.v2.orchestration import reconcile_control_requests
from pal.minion.v2.workflow_runtime import WorkflowCoordinator


def _bindings(adapter: str = "software_git.v2") -> GraphCompileBindings:
    return GraphCompileBindings(
        producer=RoleBinding("profile", "coder"),
        checker=RoleBinding("profile", "verifier"),
        execution_adapter=adapter,
    )


def _projector(definition) -> FamilyGraphSatelliteProjector:
    return FamilyGraphSatelliteProjector(
        specialization_id=definition.specialization_id,
        template=definition.graph_satellite_template,
    )


class GraphCompilerTests(unittest.TestCase):
    def _compile_software(self, payload, *, generation: int = 1):
        definition = ArchitectureTemplateCompiler().compile(
            "software_engineering.v1"
        )
        return GraphCompiler().compile(
            validate_contract_payload(payload, definition=definition),
            graph_id="build-authority",
            generation=generation,
            bindings=_bindings(),
            satellite_projector=_projector(definition),
            source_ref="architect.yaml",
            workspace_authority_rules=definition.workspace_authority_rules,
        )

    def test_family_property_compiles_build_authority_into_declared_owner(self) -> None:
        definition = ArchitectureTemplateCompiler().compile(
            "software_engineering.v1"
        )
        graph = self._compile_software(copy.deepcopy(definition.example))

        self.assertNotIn(
            {"kind": "file", "path": "CMakeLists.txt"},
            graph.nodes["decoder"].workspace_policy["implementation_scopes"],
        )
        owner_policy = dict(graph.nodes["delivery"].workspace_policy)
        self.assertIn(
            {"kind": "file", "path": "CMakeLists.txt"},
            owner_policy["implementation_scopes"],
        )
        self.assertEqual(
            owner_policy["workspace_authorities"][0]["property"]["system"],
            "cmake",
        )

    def test_family_rules_not_manager_semantics_choose_build_owner(self) -> None:
        definition = ArchitectureTemplateCompiler().compile(
            "software_engineering.v1"
        )
        payload = copy.deepcopy(definition.example)
        payload["context"]["build_system"] = {
            "system": "direct compiler invocation",
            "owner": "decoder",
            "write_scopes": [],
        }

        graph = self._compile_software(payload)

        self.assertEqual(graph.nodes["decoder"].workspace_policy[
            "workspace_authorities"
        ][0]["property"]["owner"], "decoder")

    def test_build_authority_rejects_unavailable_owner_and_cross_owner_scope(self) -> None:
        definition = ArchitectureTemplateCompiler().compile(
            "software_engineering.v1"
        )
        unknown = copy.deepcopy(definition.example)
        unknown["context"]["build_system"]["owner"] = "missing"
        with self.assertRaisesRegex(
            GraphCompilationError,
            "owner 'missing' is unavailable",
        ):
            self._compile_software(unknown)

        overlap = copy.deepcopy(definition.example)
        overlap["context"]["build_system"]["write_scopes"] = [
            {"kind": "file", "path": "src/decoder.cpp"}
        ]
        with self.assertRaisesRegex(
            GraphCompilationError,
            "overlaps write authority owned by decoder",
        ):
            self._compile_software(overlap)

        frozen = copy.deepcopy(definition.example)
        frozen["modules"]["decoder"]["definition"]["paths"][
            "contract_mode"
        ] = "file_frozen"
        frozen["context"]["build_system"]["write_scopes"] = [
            {"kind": "file", "path": "include/decoder.hpp"}
        ]
        with self.assertRaisesRegex(
            GraphCompilationError,
            "overlaps a frozen contract",
        ):
            self._compile_software(frozen)

        manager_tests = copy.deepcopy(definition.example)
        manager_tests["context"]["build_system"]["write_scopes"] = [
            {"kind": "directory", "path": "tests"}
        ]
        with self.assertRaisesRegex(
            GraphCompilationError,
            "Manager-owned test corpus",
        ):
            self._compile_software(manager_tests)

        duplicate_owner = copy.deepcopy(definition.example)
        duplicate_owner["context"]["build_system"]["write_scopes"] = [
            {"kind": "file", "path": "src/main.cpp"}
        ]
        with self.assertRaisesRegex(
            GraphCompilationError,
            "duplicates write authority already owned by delivery",
        ):
            self._compile_software(duplicate_owner)

        for path in (".git/config", "third_party/lib/.git/config"):
            with self.subTest(control_path=path):
                control_state = copy.deepcopy(definition.example)
                control_state["context"]["build_system"]["write_scopes"] = [
                    {"kind": "file", "path": path}
                ]
                with self.assertRaisesRegex(
                    GraphCompilationError,
                    "targets Manager or VCS control state",
                ):
                    self._compile_software(control_state)

    def test_build_authority_change_is_an_immutable_stale_replan(self) -> None:
        definition = ArchitectureTemplateCompiler().compile(
            "software_engineering.v1"
        )
        source_payload = copy.deepcopy(definition.example)
        target_payload = copy.deepcopy(source_payload)
        target_payload["context"]["build_system"]["write_scopes"].append(
            {"kind": "directory", "path": "cmake"}
        )
        source = self._compile_software(source_payload, generation=1)
        target = self._compile_software(target_payload, generation=2)

        decisions = diff_graphs(source, target).decisions
        decision = decisions["delivery"]
        self.assertEqual(decision.kind, NodeReuseKind.REUSE_STALE)
        self.assertTrue(decision.reuse_workspace)
        self.assertTrue(decision.reuse_sessions)
        self.assertNotIn(
            {"kind": "directory", "path": "cmake"},
            source.nodes["delivery"].workspace_policy["implementation_scopes"],
        )
        self.assertIn(
            {"kind": "directory", "path": "cmake"},
            target.nodes["delivery"].workspace_policy["implementation_scopes"],
        )
        self.assertEqual(
            decisions["decoder"].kind,
            NodeReuseKind.REUSE_ACCEPTED,
        )

    def test_unchanged_build_authority_does_not_force_owner_stale(self) -> None:
        definition = ArchitectureTemplateCompiler().compile(
            "software_engineering.v1"
        )
        payload = copy.deepcopy(definition.example)
        source = self._compile_software(payload, generation=1)
        target = self._compile_software(payload, generation=2)

        self.assertEqual(
            diff_graphs(source, target).decisions["delivery"].kind,
            NodeReuseKind.REUSE_ACCEPTED,
        )

    def test_build_owner_transfer_stales_both_authority_endpoints(self) -> None:
        definition = ArchitectureTemplateCompiler().compile(
            "software_engineering.v1"
        )
        source_payload = copy.deepcopy(definition.example)
        target_payload = copy.deepcopy(source_payload)
        target_payload["context"]["build_system"]["owner"] = "decoder"
        source = self._compile_software(source_payload, generation=1)
        target = self._compile_software(target_payload, generation=2)

        decisions = diff_graphs(source, target).decisions

        for module_name in ("delivery", "decoder"):
            with self.subTest(module_name=module_name):
                self.assertEqual(
                    decisions[module_name].kind,
                    NodeReuseKind.REUSE_STALE,
                )
                self.assertTrue(decisions[module_name].reuse_workspace)
                self.assertTrue(decisions[module_name].reuse_sessions)
        self.assertTrue(
            source.nodes["delivery"].workspace_policy["workspace_authorities"]
        )
        self.assertFalse(
            target.nodes["delivery"].workspace_policy.get("workspace_authorities")
        )
        self.assertFalse(
            source.nodes["decoder"].workspace_policy.get("workspace_authorities")
        )
        self.assertTrue(
            target.nodes["decoder"].workspace_policy["workspace_authorities"]
        )

    def test_all_family_examples_compile_without_synthesizing_nodes(self) -> None:
        compiler = ArchitectureTemplateCompiler()
        for specialization in compiler.list_specializations():
            with self.subTest(specialization=specialization.specialization_id):
                definition = compiler.compile(specialization.specialization_id)
                document = validate_contract_payload(
                    copy.deepcopy(definition.example),
                    definition=definition,
                )
                graph = GraphCompiler().compile(
                    document,
                    graph_id=f"graph-{specialization.family_id}",
                    generation=1,
                    bindings=_bindings(
                        "software_git.v2"
                        if specialization.family_id == "software_engineering"
                        else "artifact_bundle.v2"
                    ),
                    satellite_projector=_projector(definition),
                    source_ref="architect.yaml",
                    workspace_authority_rules=(
                        definition.workspace_authority_rules
                    ),
                )
                expected_nodes = {
                    name
                    for name, module in definition.example["modules"].items()
                    if module["execution"] == "produce"
                }
                self.assertEqual(set(graph.nodes), expected_nodes)
                self.assertEqual(
                    graph.sink,
                    definition.example["graph"]["sink"],
                )
                self.assertEqual(
                    graph_ir_from_mapping(graph.to_dict()),
                    graph,
                )

    def test_software_producers_run_from_contracts_but_sink_checker_waits(self) -> None:
        definition = ArchitectureTemplateCompiler().compile(
            "software_engineering.v1"
        )
        graph = GraphCompiler().compile(
            validate_contract_payload(definition.example, definition=definition),
            graph_id="framepipe",
            generation=1,
            bindings=_bindings(),
            satellite_projector=_projector(definition),
            source_ref="architect.yaml",
            workspace_authority_rules=definition.workspace_authority_rules,
        )
        edge = next(
            item
            for item in graph.edges
            if item.producer == "decoder" and item.consumer == "delivery"
        )
        self.assertEqual(edge.kind, EdgeKind.EXECUTION)
        self.assertEqual(
            set(graph.execution_predecessors(graph.sink)),
            set(graph.nodes) - {graph.sink},
        )
        self.assertEqual(graph.producer_predecessors(graph.sink), ())
        self.assertEqual(
            set(graph.checker_predecessors(graph.sink)),
            set(graph.nodes) - {graph.sink},
        )

    def test_family_satellite_makes_requirement_semantics_part_of_replan(self) -> None:
        definition = ArchitectureTemplateCompiler().compile(
            "software_engineering.v1"
        )
        source_payload = copy.deepcopy(definition.example)
        source = GraphCompiler().compile(
            validate_contract_payload(source_payload, definition=definition),
            graph_id="framepipe",
            generation=1,
            bindings=_bindings(),
            satellite_projector=_projector(definition),
            source_ref="architect.yaml",
            workspace_authority_rules=definition.workspace_authority_rules,
        )
        target_payload = copy.deepcopy(source_payload)
        target_payload["requirements"]["decode_frames"]["claim"] = (
            "Decode complete frames and reject malformed headers."
        )
        target = GraphCompiler().compile(
            validate_contract_payload(target_payload, definition=definition),
            graph_id="framepipe",
            generation=2,
            bindings=_bindings(),
            satellite_projector=_projector(definition),
            source_ref="architect.yaml",
            workspace_authority_rules=definition.workspace_authority_rules,
        )

        decisions = diff_graphs(source, target).decisions
        self.assertTrue(
            all(
                decision.kind == NodeReuseKind.REUSE_STALE
                for decision in decisions.values()
            )
        )
        self.assertEqual(
            target.nodes["decoder"].satellite_data["architecture"]
            ["requirements"]["decode_frames"]["claim"],
            target_payload["requirements"]["decode_frames"]["claim"],
        )

    def test_sink_must_be_authored_connected_and_terminal(self) -> None:
        definition = ArchitectureTemplateCompiler().compile("general.v1")
        payload = copy.deepcopy(definition.example)
        payload["modules"]["unused"] = copy.deepcopy(
            payload["modules"]["report"]
        )
        document = validate_contract_payload(payload, definition=definition)
        with self.assertRaisesRegex(
            GraphCompilationError,
            "every executable node must reach",
        ):
            GraphCompiler().compile(
                document,
                graph_id="report",
                generation=1,
                bindings=_bindings("artifact_bundle.v2"),
                satellite_projector=_projector(definition),
                source_ref="architect.yaml",
                workspace_authority_rules=definition.workspace_authority_rules,
            )

    def test_software_transitive_graph_waits_at_sink_checker(self) -> None:
        definition = ArchitectureTemplateCompiler().compile(
            "software_engineering.v1"
        )
        payload = copy.deepcopy(definition.example)
        payload["modules"]["codec"] = copy.deepcopy(
            payload["modules"]["decoder"]
        )
        payload["modules"]["codec"]["responsibility"] = "encode frames"
        payload["modules"]["codec"]["provides"] = ["encoded_frames"]
        payload["modules"]["codec"]["definition"]["contract"]["outputs"] = {
            "encoded_frames": {
                "interface": "Encoder::encode",
                "semantics": "Complete encoded frames.",
            }
        }
        payload["modules"]["codec"]["definition"]["paths"] = {
            "contract_mode": "review_guarded",
            "contract_paths": ["include/encoder.hpp"],
            "implementation_scopes": [
                {"kind": "file", "path": "src/encoder.cpp"}
            ],
            "reference_only": [],
        }
        payload["modules"]["decoder"]["dependencies"] = {
            "codec": {
                "purpose": "consume complete encoded frames",
                "handoff": "encoded frame bytes",
                "consumes": ["encoded_frames"],
            }
        }
        payload["scenarios"]["decode_one_frame"]["modules"].append("codec")
        graph = GraphCompiler().compile(
            validate_contract_payload(payload, definition=definition),
            graph_id="transitive",
            generation=1,
            bindings=_bindings(),
            satellite_projector=_projector(definition),
            source_ref="architect.yaml",
            workspace_authority_rules=definition.workspace_authority_rules,
        )
        self.assertEqual(
            set(graph.execution_predecessors(graph.sink)),
            set(graph.nodes) - {graph.sink},
        )
        self.assertEqual(graph.producer_predecessors(graph.sink), ())

    def test_contract_only_dependency_is_hashed_but_not_executed(self) -> None:
        definition = ArchitectureTemplateCompiler().compile(
            "software_engineering.v1"
        )
        payload = copy.deepcopy(definition.example)
        payload["modules"]["frame_shape"] = {
            "responsibility": "Declare the immutable frame shape.",
            "execution": "contract_only",
            "provides": ["frame_shape"],
            "dependencies": {},
            "definition": {
                "behavior_kind": "stateless",
                "contract": {
                    "inputs": {},
                    "outputs": {
                        "frame_shape": {
                            "interface": "Frame",
                            "semantics": "Immutable decoded frame value.",
                        }
                    },
                    "invariants": ["Frame values own their bytes."],
                    "errors": [],
                },
                "ownership": ["Frame values own their bytes."],
                "lifecycle": {
                    "creation": "Constructed from decoded bytes.",
                    "operation": "Read-only value access.",
                    "shutdown": "No explicit shutdown.",
                    "cleanup": "Owned bytes are released.",
                    "failure": "Construction rejects invalid bytes.",
                },
                "state_machine": None,
                "paths": {
                    "contract_mode": "file_frozen",
                    "contract_paths": ["include/frame.hpp"],
                    "implementation_scopes": [],
                    "reference_only": [],
                },
            },
        }
        payload["modules"]["decoder"]["dependencies"] = {
            "frame_shape": {
                "purpose": "construct the declared frame value",
                "handoff": "Frame value contract",
                "consumes": ["frame_shape"],
            }
        }
        document = validate_contract_payload(payload, definition=definition)
        graph = GraphCompiler().compile(
            document,
            graph_id="contract-only",
            generation=1,
            bindings=_bindings(),
            satellite_projector=_projector(definition),
            source_ref="architect.yaml",
            workspace_authority_rules=definition.workspace_authority_rules,
        )
        self.assertNotIn("frame_shape", graph.nodes)
        self.assertTrue(
            all(
                edge.producer != "frame_shape"
                and edge.consumer != "frame_shape"
                for edge in graph.edges
            )
        )
        self.assertIn(
            "frame_shape",
            graph.nodes["decoder"].satellite_data["architecture"]["modules"],
        )

        revised_payload = copy.deepcopy(payload)
        revised_payload["modules"]["frame_shape"]["definition"][
            "contract"
        ]["invariants"].append("Frame values have stable byte order.")
        revised = GraphCompiler().compile(
            validate_contract_payload(
                revised_payload,
                definition=definition,
            ),
            graph_id="contract-only",
            generation=2,
            bindings=_bindings(),
            satellite_projector=_projector(definition),
            source_ref="architect.yaml",
            workspace_authority_rules=definition.workspace_authority_rules,
        )
        graph_diff = diff_graphs(graph, revised)
        self.assertEqual(
            graph_diff.decisions["decoder"].kind,
            NodeReuseKind.REUSE_STALE,
        )
        self.assertEqual(
            graph_diff.decisions[revised.sink].kind,
            NodeReuseKind.REUSE_STALE,
        )

    def test_compilation_error_contains_yaml_location(self) -> None:
        definition = ArchitectureTemplateCompiler().compile("general.v1")
        payload = copy.deepcopy(definition.example)
        payload["graph"]["sink"] = "missing"
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "architect.yaml"
            path.write_text(
                yaml.safe_dump(payload, sort_keys=False),
                encoding="utf-8",
            )
            location = build_yaml_source_map(path)
            with self.assertRaises(GraphCompilationError) as raised:
                GraphCompiler().compile(
                    payload,
                    graph_id="report",
                    generation=1,
                    bindings=_bindings("artifact_bundle.v2"),
                    satellite_projector=_projector(definition),
                    source_ref=str(path),
                    workspace_authority_rules=definition.workspace_authority_rules,
                    source_map=location,
                )
        self.assertEqual(raised.exception.semantic_path, "graph.sink")
        self.assertIsNotNone(raised.exception.location)
        self.assertIn("architect.yaml:", str(raised.exception))

    def test_authority_error_contains_property_yaml_location(self) -> None:
        definition = ArchitectureTemplateCompiler().compile(
            "software_engineering.v1"
        )
        payload = copy.deepcopy(definition.example)
        payload["context"]["build_system"]["write_scopes"] = [
            {"kind": "file", "path": ".git/config"}
        ]
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "architect.yaml"
            path.write_text(
                yaml.safe_dump(payload, sort_keys=False),
                encoding="utf-8",
            )
            with self.assertRaises(GraphCompilationError) as raised:
                GraphCompiler().compile(
                    validate_contract_payload(payload, definition=definition),
                    graph_id="framepipe",
                    generation=1,
                    bindings=_bindings(),
                    satellite_projector=_projector(definition),
                    source_ref=str(path),
                    workspace_authority_rules=(
                        definition.workspace_authority_rules
                    ),
                    source_map=build_yaml_source_map(path),
                )

        self.assertEqual(
            raised.exception.semantic_path,
            "context.build_system",
        )
        self.assertIsNotNone(raised.exception.location)
        self.assertIn("architect.yaml:", str(raised.exception))


class ProduceCheckCycleTests(unittest.TestCase):
    def test_node_verdict_is_generation_bound(self) -> None:
        cycle = NodeCycle(
            cycle_id="node:decoder",
            node_name="decoder",
            state=NodeCycleState.PRODUCER_READY,
        )
        producer = CycleAssignment(
            slot=CycleSlot.PRODUCER,
            kind=AssignmentKind.INITIAL,
            generation=1,
            input_fingerprint="input-1",
        )
        checker = CycleAssignment(
            slot=CycleSlot.CHECKER,
            kind=AssignmentKind.INITIAL,
            generation=1,
            input_fingerprint="candidate-1",
        )
        cycle = cycle.transition(CycleAction.START_PRODUCER, assignment=producer)
        cycle = cycle.transition(
            CycleAction.PRODUCER_SUBMITTED,
            product_ref="candidate-ref",
        )
        cycle = cycle.transition(CycleAction.START_CHECKER, assignment=checker)
        with self.assertRaisesRegex(
            CycleTransitionError,
            "different graph generation",
        ):
            cycle.transition(
                CycleAction.CHECKER_ACCEPTED,
                verdict=CycleVerdict(accepted=True, generation=2),
            )
        cycle = cycle.transition(
            CycleAction.CHECKER_ACCEPTED,
            verdict=CycleVerdict(accepted=True, generation=1),
        )
        self.assertEqual(cycle.state, NodeCycleState.ACCEPTED)

    def test_plan_has_explicit_human_review_after_checker(self) -> None:
        cycle = PlanCycle(cycle_id="plan")
        producer = CycleAssignment(
            CycleSlot.PRODUCER,
            AssignmentKind.INITIAL,
            1,
            "requirements",
        )
        checker = CycleAssignment(
            CycleSlot.CHECKER,
            AssignmentKind.INITIAL,
            1,
            "graph",
        )
        cycle = cycle.transition(CycleAction.START_PRODUCER, assignment=producer)
        cycle = cycle.transition(
            CycleAction.PRODUCER_SUBMITTED,
            product_ref="graph-ref",
        )
        cycle = cycle.transition(CycleAction.START_CHECKER, assignment=checker)
        cycle = cycle.transition(
            CycleAction.CHECKER_ACCEPTED,
            verdict=CycleVerdict(accepted=True, generation=1),
        )
        cycle = cycle.transition(CycleAction.REQUEST_HUMAN_REVIEW)
        self.assertEqual(cycle.state, PlanCycleState.HUMAN_REVIEW)


class CoroutineRunSemaphoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_materialized_runs_consume_capacity(self) -> None:
        semaphore = CoroutineRunSemaphore(1)
        # Durable logical sessions are intentionally not registered here.
        first = await semaphore.acquire("run-1")
        self.assertEqual(semaphore.active_count, 1)
        self.assertIsNone(await semaphore.try_acquire("run-2"))
        await first.release()
        second = await semaphore.try_acquire("run-2")
        self.assertIsNotNone(second)
        self.assertEqual(semaphore.active_run_ids, frozenset({"run-2"}))
        await second.release()  # type: ignore[union-attr]

    async def test_waiter_runs_only_after_reap_boundary_releases_permit(self) -> None:
        semaphore = CoroutineRunSemaphore(1)
        first = await semaphore.acquire("run-1")
        waiter = asyncio.create_task(semaphore.acquire("run-2"))
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        await first.release()
        second = await waiter
        self.assertEqual(semaphore.active_run_ids, frozenset({"run-2"}))
        await second.release()

    async def test_supervisor_does_not_count_durable_or_waiting_coroutines(self) -> None:
        supervisor = RoleSupervisor(max_active_runs=1)
        self.assertEqual(supervisor.active_run_count, 0)
        # Durable logical sessions exist in the role repository, outside this
        # process-capacity projection, and therefore consume no slot.
        self.assertEqual(supervisor.active_run_count, 0)


class GraphExecutionTests(unittest.TestCase):
    def _graph(self):
        definition = ArchitectureTemplateCompiler().compile(
            "software_engineering.v1"
        )
        return GraphCompiler().compile(
            validate_contract_payload(definition.example, definition=definition),
            graph_id="framepipe",
            generation=1,
            bindings=_bindings(),
            satellite_projector=_projector(definition),
            source_ref="architect.yaml",
            workspace_authority_rules=definition.workspace_authority_rules,
        )

    def test_all_software_producers_start_but_sink_checker_waits(self) -> None:
        execution = GraphExecution.start(self._graph())
        self.assertEqual(execution.runnable_nodes(), ("decoder", "delivery"))
        for name in ("decoder", "delivery"):
            execution = execution.with_cycle(
                execution.cycles[name].transition(
                    CycleAction.START_PRODUCER,
                    assignment=CycleAssignment(
                        CycleSlot.PRODUCER,
                        AssignmentKind.INITIAL,
                        1,
                        f"{name}-input",
                    ),
                )
            )
            execution = execution.with_cycle(
                execution.cycles[name].transition(
                    CycleAction.PRODUCER_SUBMITTED,
                    product_ref=f"{name}-candidate",
                )
            )
        self.assertNotIn("delivery", execution.runnable_nodes())
        execution = execution.with_cycle(
            execution.cycles["decoder"].transition(
                CycleAction.START_CHECKER,
                assignment=CycleAssignment(
                    CycleSlot.CHECKER,
                    AssignmentKind.INITIAL,
                    1,
                    "decoder-check",
                ),
            )
        )
        execution, _ = execution.apply_checker_verdict(
            current_node="decoder",
            accepted=True,
            accepted_product_ref="decoder-accepted",
        )
        self.assertIn("delivery", execution.runnable_nodes())

    def test_framepipe_cli_codes_in_parallel_and_only_its_checker_waits(self) -> None:
        binding = RoleBinding("profile", "worker")
        nodes = {
            name: NodeSpec(
                name=name,
                responsibility=f"own {name}",
                satellite_data={"test": True},
                producer_binding=binding,
                checker_binding=binding,
                execution_adapter="software_git.v2",
                workspace_policy={},
                output_contract=(f"{name}_output",),
                is_sink=name == "framepipe_cli",
            )
            for name in ("frame_protocol", "hex_codec", "framepipe_cli")
        }
        graph = GraphIR(
            graph_id="framepipe-regression",
            generation=1,
            nodes=nodes,
            edges=tuple(
                EdgeSpec(
                    provider,
                    "framepipe_cli",
                    EdgeKind.EXECUTION,
                    f"modules.framepipe_cli.dependencies.{provider}",
                    (f"{provider}_output",),
                )
                for provider in ("frame_protocol", "hex_codec")
            ),
            sink="framepipe_cli",
            source_ref="architect.yaml",
            source_map_ref="source-map",
        )
        execution = GraphExecution.start(graph)
        self.assertEqual(
            execution.runnable_nodes(),
            ("frame_protocol", "framepipe_cli", "hex_codec"),
        )
        for name in nodes:
            execution = execution.with_cycle(
                execution.cycles[name].transition(
                    CycleAction.START_PRODUCER,
                    assignment=CycleAssignment(
                        CycleSlot.PRODUCER,
                        AssignmentKind.INITIAL,
                        1,
                        f"{name}-input",
                    ),
                )
            )
            execution = execution.with_cycle(
                execution.cycles[name].transition(
                    CycleAction.PRODUCER_SUBMITTED,
                    product_ref=f"{name}-candidate",
                )
            )
        self.assertNotIn("framepipe_cli", execution.runnable_nodes())
        for name in ("frame_protocol", "hex_codec"):
            execution = execution.with_cycle(
                execution.cycles[name].transition(
                    CycleAction.START_CHECKER,
                    assignment=CycleAssignment(
                        CycleSlot.CHECKER,
                        AssignmentKind.INITIAL,
                        1,
                        f"{name}-check",
                    ),
                )
            )
            execution, _ = execution.apply_checker_verdict(
                current_node=name,
                accepted=True,
                accepted_product_ref=f"{name}-accepted",
            )
        self.assertIn("framepipe_cli", execution.runnable_nodes())

    def test_replan_request_does_not_stale_an_active_sibling(self) -> None:
        binding = RoleBinding("profile", "worker")
        nodes = {
            name: NodeSpec(
                name=name,
                responsibility=f"own {name}",
                satellite_data={"test": True},
                producer_binding=binding,
                checker_binding=binding,
                execution_adapter="software_git.v2",
                workspace_policy={},
                output_contract=(f"{name}_output",),
                is_sink=name == "delivery",
            )
            for name in ("decoder", "encoder", "delivery")
        }
        graph = GraphIR(
            graph_id="parallel-replan",
            generation=1,
            nodes=nodes,
            edges=(
                EdgeSpec(
                    "decoder",
                    "delivery",
                    EdgeKind.EXECUTION,
                    "modules.delivery.dependencies.decoder",
                    ("decoder_output",),
                ),
                EdgeSpec(
                    "encoder",
                    "delivery",
                    EdgeKind.EXECUTION,
                    "modules.delivery.dependencies.encoder",
                    ("encoder_output",),
                ),
            ),
            sink="delivery",
            source_ref="architect.yaml",
            source_map_ref="source-map",
        )
        execution = GraphExecution.start(graph)
        for name in ("decoder", "encoder"):
            execution = execution.with_cycle(
                execution.cycles[name].transition(
                    CycleAction.START_PRODUCER,
                    assignment=CycleAssignment(
                        CycleSlot.PRODUCER,
                        AssignmentKind.INITIAL,
                        1,
                        f"{name}-input",
                    ),
                )
            )
        decoder = execution.cycles["decoder"].transition(
            CycleAction.PRODUCER_SUBMITTED,
            product_ref="decoder-product",
        )
        execution = execution.with_cycle(decoder)
        execution = execution.with_cycle(
            decoder.transition(
                CycleAction.START_CHECKER,
                assignment=CycleAssignment(
                    CycleSlot.CHECKER,
                    AssignmentKind.INITIAL,
                    1,
                    "decoder-product",
                ),
            )
        )
        replanning, route = execution.apply_checker_verdict(
            current_node="decoder",
            accepted=False,
            finding_refs=("contract-finding",),
            finding_class=FindingClass.CONTRACT_DEFECT,
        )
        self.assertEqual(route.target.value, "plan_cycle")
        self.assertEqual(
            replanning.state,
            GraphExecutionState.REPLAN_REQUIRED,
        )
        self.assertEqual(
            replanning.cycles["encoder"].state,
            NodeCycleState.PRODUCING,
        )
        self.assertEqual(replanning.runnable_nodes(), ())

    def test_replan_reuses_workspace_and_sessions_by_semantic_identity(self) -> None:
        source = self._graph()
        definition = ArchitectureTemplateCompiler().compile(
            "software_engineering.v1"
        )
        payload = copy.deepcopy(definition.example)
        payload["modules"]["decoder"]["definition"]["contract"][
            "invariants"
        ].append("decoded output is deterministic")
        target = GraphCompiler().compile(
            validate_contract_payload(payload, definition=definition),
            graph_id="framepipe",
            generation=2,
            bindings=_bindings(),
            satellite_projector=_projector(definition),
            source_ref="architect.yaml",
            workspace_authority_rules=definition.workspace_authority_rules,
        )
        diff = diff_graphs(source, target)
        decision = diff.decisions["decoder"]
        self.assertEqual(decision.kind, NodeReuseKind.REUSE_STALE)
        self.assertTrue(decision.reuse_workspace)
        self.assertTrue(decision.reuse_sessions)
        self.assertEqual(
            diff.decisions[target.sink].kind,
            NodeReuseKind.REUSE_STALE,
        )

        with tempfile.TemporaryDirectory() as root:
            coordinator = WorkflowCoordinator(
                MinionV2Repository(Path(root))
            )
            coordinator.install_graph(
                workflow_id=source.graph_id,
                graph=source,
            )
            execution = coordinator.execution(
                workflow_id=source.graph_id
            )
            accepted_cycles = {
                name: replace(
                    cycle,
                    state=NodeCycleState.ACCEPTED,
                    product_ref=f"{name}-product",
                    accepted_product_ref=f"{name}-product",
                    last_verdict=CycleVerdict(
                        True,
                        source.generation,
                    ),
                )
                for name, cycle in execution.cycles.items()
            }
            coordinator.repository.store_graph_execution(
                workflow_id=source.graph_id,
                execution=replace(
                    execution,
                    cycles=accepted_cycles,
                    state=GraphExecutionState.COMPLETED,
                    published_sink_ref="delivery-product",
                ),
            )
            installed = coordinator.install_graph(
                workflow_id=target.graph_id,
                graph=target,
            )
            self.assertEqual(
                installed.execution.cycles["decoder"].state,
                NodeCycleState.PRODUCER_READY,
            )
            self.assertEqual(
                installed.execution.cycles[target.sink].state,
                NodeCycleState.PRODUCER_READY,
            )
            self.assertEqual(
                installed.execution.state,
                GraphExecutionState.RUNNING,
            )

    def test_replan_rebinds_carried_acceptance_to_new_generation(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            coordinator = WorkflowCoordinator(
                MinionV2Repository(Path(root))
            )
            source = self._graph()
            coordinator.install_graph(
                workflow_id=source.graph_id,
                graph=source,
            )
            execution = coordinator.execution(workflow_id=source.graph_id)
            cycles = {
                name: replace(
                    cycle,
                    state=NodeCycleState.ACCEPTED,
                    product_ref=f"{name}-product",
                    accepted_product_ref=f"{name}-product",
                    last_verdict=CycleVerdict(True, source.generation),
                )
                for name, cycle in execution.cycles.items()
            }
            coordinator.repository.store_graph_execution(
                workflow_id=source.graph_id,
                execution=replace(
                    execution,
                    cycles=cycles,
                    state=GraphExecutionState.COMPLETED,
                    published_sink_ref="delivery-product",
                ),
            )
            definition = ArchitectureTemplateCompiler().compile(
                "software_engineering.v1"
            )
            target = GraphCompiler().compile(
                validate_contract_payload(
                    definition.example,
                    definition=definition,
                ),
                graph_id=source.graph_id,
                generation=2,
                bindings=_bindings(),
                satellite_projector=_projector(definition),
                source_ref="architect.yaml",
                workspace_authority_rules=definition.workspace_authority_rules,
            )
            installed = coordinator.install_graph(
                workflow_id=source.graph_id,
                graph=target,
            )
            self.assertTrue(
                all(
                    cycle.generation == 2
                    and cycle.last_verdict is not None
                    and cycle.last_verdict.generation == 2
                    for cycle in installed.execution.cycles.values()
                )
            )

    def test_graph_generation_and_cycles_are_durable(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repository = MinionV2Repository(Path(root))
            graph = self._graph()
            repository.store_graph_generation(
                workflow_id="workflow-framepipe",
                graph=graph,
            )
            restored = repository.read_graph_generation(
                graph_id=graph.graph_id,
            )
            self.assertEqual(restored, graph)
            execution = replace(
                GraphExecution.start(graph),
                repair_barriers={"delivery": ("decoder",)},
            )
            repository.store_graph_execution(
                workflow_id="workflow-framepipe",
                execution=execution,
            )
            self.assertEqual(
                repository.read_graph_execution(
                    workflow_id="workflow-framepipe"
                ),
                execution,
            )

    def test_graph_install_rolls_back_generation_when_execution_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repository = MinionV2Repository(Path(root))
            coordinator = WorkflowCoordinator(repository)
            graph = self._graph()

            with patch.object(
                repository,
                "store_graph_execution",
                side_effect=RuntimeError("crash before execution projection"),
            ):
                with self.assertRaisesRegex(RuntimeError, "execution projection"):
                    coordinator.install_graph(
                        workflow_id=graph.graph_id,
                        graph=graph,
                    )

            self.assertIsNone(
                repository.read_graph_generation(
                    graph_id=graph.graph_id,
                    generation=graph.generation,
                )
            )
            self.assertIsNone(
                repository.read_graph_execution(
                    workflow_id=graph.graph_id,
                    generation=graph.generation,
                )
            )

    def test_coordinator_owns_readiness_and_sink_publication(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            coordinator = WorkflowCoordinator(
                MinionV2Repository(Path(root))
            )
            graph = self._graph()
            coordinator.install_graph(
                workflow_id=graph.graph_id,
                graph=graph,
            )
            self.assertEqual(
                tuple(
                    item.node_name
                    for item in coordinator.runnable_assignments(
                        workflow_id=graph.graph_id
                    )
                ),
                ("decoder", "delivery"),
            )
            coordinator.start_assignment(
                workflow_id=graph.graph_id,
                node_name="delivery",
                slot=CycleSlot.PRODUCER,
                kind=AssignmentKind.INITIAL,
                input_fingerprint="delivery-input",
            )
            coordinator.producer_submitted(
                workflow_id=graph.graph_id,
                node_name="delivery",
                product_ref="delivery-coder-candidate",
            )
            coordinator.start_assignment(
                workflow_id=graph.graph_id,
                node_name="decoder",
                slot=CycleSlot.PRODUCER,
                kind=AssignmentKind.INITIAL,
                input_fingerprint="decoder-input",
            )
            coordinator.producer_submitted(
                workflow_id=graph.graph_id,
                node_name="decoder",
                product_ref="decoder-candidate",
            )
            coordinator.start_assignment(
                workflow_id=graph.graph_id,
                node_name="decoder",
                slot=CycleSlot.CHECKER,
                kind=AssignmentKind.INITIAL,
                input_fingerprint="decoder-candidate",
            )
            coordinator.checker_verdict(
                workflow_id=graph.graph_id,
                node_name="decoder",
                accepted=True,
            )
            self.assertEqual(
                tuple(
                    item.node_name
                    for item in coordinator.runnable_assignments(
                        workflow_id=graph.graph_id
                    )
                ),
                ("delivery",),
            )
            coordinator.start_assignment(
                workflow_id=graph.graph_id,
                node_name="delivery",
                slot=CycleSlot.CHECKER,
                kind=AssignmentKind.INITIAL,
                input_fingerprint="delivery-coder-candidate",
            )
            coordinator.checker_verdict(
                workflow_id=graph.graph_id,
                node_name="delivery",
                accepted=True,
                accepted_product_ref="delivery-with-verifier-corpus",
            )
            self.assertEqual(
                coordinator.published_sink_ref(workflow_id=graph.graph_id),
                "delivery-with-verifier-corpus",
            )

    def test_dependency_repair_barrier_waits_for_provider_reacceptance(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            coordinator = WorkflowCoordinator(
                MinionV2Repository(Path(root))
            )
            graph = self._graph()
            coordinator.install_graph(
                workflow_id=graph.graph_id,
                graph=graph,
            )
            execution = coordinator.execution(workflow_id=graph.graph_id)
            cycles = dict(execution.cycles)
            for name in ("decoder", "delivery"):
                cycle = cycles[name]
                if cycle.state == NodeCycleState.BLOCKED:
                    cycle = cycle.transition(CycleAction.UNBLOCK)
                cycle = cycle.transition(
                    CycleAction.START_PRODUCER,
                    assignment=CycleAssignment(
                        CycleSlot.PRODUCER,
                        AssignmentKind.INITIAL,
                        1,
                        f"{name}-input",
                    ),
                )
                cycle = cycle.transition(
                    CycleAction.PRODUCER_SUBMITTED,
                    product_ref=f"{name}-candidate",
                )
                cycle = cycle.transition(
                    CycleAction.START_CHECKER,
                    assignment=CycleAssignment(
                        CycleSlot.CHECKER,
                        AssignmentKind.INITIAL,
                        1,
                        f"{name}-candidate",
                    ),
                )
                cycle = cycle.transition(
                    CycleAction.CHECKER_ACCEPTED,
                    verdict=CycleVerdict(True, 1),
                )
                cycles[name] = cycle
            accepted = GraphExecution(
                graph=graph,
                state=execution.state,
                cycles=cycles,
            ).refresh()
            # Re-open the sink checker to report a provider defect.
            sink = replace(
                accepted.cycles["delivery"],
                state=NodeCycleState.CHECKER_READY,
                last_verdict=None,
            )
            accepted = replace(
                accepted,
                state=GraphExecutionState.RUNNING,
                cycles={**dict(accepted.cycles), "delivery": sink},
                published_sink_ref="",
            )
            sink = sink.transition(
                CycleAction.START_CHECKER,
                assignment=CycleAssignment(
                    CycleSlot.CHECKER,
                    AssignmentKind.RECHECK,
                    1,
                    "delivery-recheck",
                ),
            )
            accepted = accepted.with_cycle(sink)
            routed, route = accepted.apply_checker_verdict(
                current_node="delivery",
                accepted=False,
                finding_refs=("finding",),
                finding_class=FindingClass.DEPENDENCY_DEFECT,
                dependency_node="decoder",
            )
            self.assertEqual(route.node_name, "decoder")
            self.assertEqual(
                routed.cycles["delivery"].state,
                NodeCycleState.STALE,
            )
            decoder = routed.cycles["decoder"].transition(
                CycleAction.START_PRODUCER,
                assignment=CycleAssignment(
                    CycleSlot.PRODUCER,
                    AssignmentKind.REPAIR,
                    1,
                    "decoder-repair",
                ),
            )
            routed = routed.with_cycle(decoder)
            self.assertEqual(
                routed.cycles["delivery"].state,
                NodeCycleState.STALE,
            )

    def test_coordinator_replays_submission_and_verdict_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            coordinator = WorkflowCoordinator(
                MinionV2Repository(Path(root))
            )
            graph = self._graph()
            coordinator.install_graph(
                workflow_id=graph.graph_id,
                graph=graph,
            )
            coordinator.start_assignment(
                workflow_id=graph.graph_id,
                node_name="decoder",
                slot=CycleSlot.PRODUCER,
                kind=AssignmentKind.INITIAL,
                input_fingerprint="input",
            )
            first = coordinator.producer_submitted(
                workflow_id=graph.graph_id,
                node_name="decoder",
                product_ref="candidate",
            )
            replay = coordinator.producer_submitted(
                workflow_id=graph.graph_id,
                node_name="decoder",
                product_ref="candidate",
            )
            self.assertEqual(first, replay)
            coordinator.start_assignment(
                workflow_id=graph.graph_id,
                node_name="decoder",
                slot=CycleSlot.CHECKER,
                kind=AssignmentKind.INITIAL,
                input_fingerprint="candidate",
            )
            coordinator.checker_verdict(
                workflow_id=graph.graph_id,
                node_name="decoder",
                accepted=True,
            )
            self.assertIsNone(
                coordinator.checker_verdict(
                    workflow_id=graph.graph_id,
                    node_name="decoder",
                    accepted=True,
                )
            )
            coordinator.start_assignment(
                workflow_id=graph.graph_id,
                node_name="delivery",
                slot=CycleSlot.PRODUCER,
                kind=AssignmentKind.INITIAL,
                input_fingerprint="delivery-input",
            )
            coordinator.producer_submitted(
                workflow_id=graph.graph_id,
                node_name="delivery",
                product_ref="delivery-candidate",
            )
            coordinator.start_assignment(
                workflow_id=graph.graph_id,
                node_name="delivery",
                slot=CycleSlot.CHECKER,
                kind=AssignmentKind.INITIAL,
                input_fingerprint="delivery-candidate",
            )
            coordinator.checker_verdict(
                workflow_id=graph.graph_id,
                node_name="delivery",
                accepted=False,
                finding_refs=("immutable-finding",),
                finding_class=FindingClass.MODULE_DEFECT,
            )
            before_replay = coordinator.execution(
                workflow_id=graph.graph_id
            )
            self.assertIsNone(
                coordinator.checker_verdict(
                    workflow_id=graph.graph_id,
                    node_name="delivery",
                    accepted=False,
                    finding_refs=("immutable-finding",),
                    finding_class=FindingClass.SINK_DEFECT,
                )
            )
            self.assertEqual(
                coordinator.execution(workflow_id=graph.graph_id),
                before_replay,
            )

    def test_pause_and_triage_resume_at_assignment_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            coordinator = WorkflowCoordinator(
                MinionV2Repository(Path(root))
            )
            graph = self._graph()
            coordinator.install_graph(
                workflow_id=graph.graph_id,
                graph=graph,
            )
            coordinator.start_assignment(
                workflow_id=graph.graph_id,
                node_name="decoder",
                slot=CycleSlot.PRODUCER,
                kind=AssignmentKind.INITIAL,
                input_fingerprint="input",
            )
            coordinator.request_workflow_pause(
                workflow_id=graph.graph_id
            )
            execution = coordinator.execution(workflow_id=graph.graph_id)
            self.assertEqual(
                execution.cycles["decoder"].state,
                NodeCycleState.PAUSE_REQUESTED,
            )
            coordinator.confirm_node_control(
                workflow_id=graph.graph_id,
                node_name="decoder",
                cancel=False,
            )
            coordinator.resume_workflow(workflow_id=graph.graph_id)
            execution = coordinator.execution(workflow_id=graph.graph_id)
            self.assertEqual(
                execution.cycles["decoder"].state,
                NodeCycleState.PRODUCER_READY,
            )
            coordinator.start_assignment(
                workflow_id=graph.graph_id,
                node_name="decoder",
                slot=CycleSlot.PRODUCER,
                kind=AssignmentKind.RESUME,
                input_fingerprint="resume",
            )
            coordinator.require_node_triage(
                workflow_id=graph.graph_id,
                node_name="decoder",
            )
            coordinator.resolve_triage(
                workflow_id=graph.graph_id,
                node_name="decoder",
            )
            self.assertEqual(
                coordinator.execution(
                    workflow_id=graph.graph_id
                ).cycles["decoder"].state,
                NodeCycleState.PRODUCER_READY,
            )

    def test_pause_resume_does_not_hide_replan_required(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            coordinator = WorkflowCoordinator(
                MinionV2Repository(Path(root))
            )
            graph = self._graph()
            installed = coordinator.install_graph(
                workflow_id=graph.graph_id,
                graph=graph,
            )
            self.repository = coordinator.repository
            self.repository.store_graph_execution(
                workflow_id=graph.graph_id,
                execution=replace(
                    installed.execution,
                    state=GraphExecutionState.REPLAN_REQUIRED,
                ),
            )
            coordinator.request_workflow_pause(workflow_id=graph.graph_id)
            coordinator.resume_workflow(workflow_id=graph.graph_id)
            self.assertEqual(
                coordinator.execution(workflow_id=graph.graph_id).state,
                GraphExecutionState.REPLAN_REQUIRED,
            )

    def test_control_reconciler_repairs_cycle_projection_after_crash(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repository = MinionV2Repository(Path(root))
            coordinator = WorkflowCoordinator(repository)
            graph = self._graph()
            coordinator.install_graph(
                workflow_id=graph.graph_id,
                graph=graph,
            )
            coordinator.start_assignment(
                workflow_id=graph.graph_id,
                node_name="decoder",
                slot=CycleSlot.PRODUCER,
                kind=AssignmentKind.INITIAL,
                input_fingerprint="active-producer",
            )

            def snapshots(workflow_state: str):
                now = "2026-08-02T00:00:00+00:00"
                return [
                    AggregateSnapshot(
                        aggregate_type=AggregateType.WORKFLOW,
                        aggregate_id=graph.graph_id,
                        workflow_id=graph.graph_id,
                        state=workflow_state,
                        version=1,
                        payload={},
                        created_at=now,
                        updated_at=now,
                    ),
                    AggregateSnapshot(
                        aggregate_type=AggregateType.DAG_NODE_RUN,
                        aggregate_id="node-decoder",
                        workflow_id=graph.graph_id,
                        state="PAUSED",
                        version=1,
                        payload={"module_name": "decoder"},
                        created_at=now,
                        updated_at=now,
                    ),
                ]

            repository.list_workflow_snapshots = lambda _workflow_id: snapshots(
                "PAUSED"
            )
            reconcile_control_requests(repository, graph.graph_id)
            paused = coordinator.execution(workflow_id=graph.graph_id)
            self.assertEqual(
                paused.cycles["decoder"].state,
                NodeCycleState.PAUSED,
            )

            repository.list_workflow_snapshots = lambda _workflow_id: snapshots(
                "ACTIVE"
            )
            reconcile_control_requests(repository, graph.graph_id)
            resumed = coordinator.execution(workflow_id=graph.graph_id)
            self.assertEqual(
                resumed.cycles["decoder"].state,
                NodeCycleState.PRODUCER_READY,
            )
if __name__ == "__main__":
    unittest.main()
