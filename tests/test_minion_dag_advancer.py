from __future__ import annotations

import unittest

from pal.minion.dag_advancer import (
    DagAdvanceResult,
    DagSpec,
    DagState,
    apply_repair_replay,
    build_module_dag_from_validation,
    claim_ready_modules,
    complete_module,
    dag_spec_from_validation,
    dag_state_from_validation,
    dag_state_to_runtime_dict,
    dag_state_to_storage_dict,
    mark_modules_running,
    module_dag_status,
    ready_module_ids,
    release_running_module,
)


def _validation() -> dict[str, object]:
    return {
        "nodes": [
            {"node_id": "contracts", "module_id": "contracts", "kind": "prelude", "depends_on": []},
            {"node_id": "engine", "module_id": "engine", "kind": "module", "depends_on": ["contracts"]},
            {"node_id": "renderer", "module_id": "renderer", "kind": "module", "depends_on": ["contracts"]},
            {"node_id": "final", "module_id": "final", "kind": "join", "depends_on": ["engine", "renderer"]},
        ]
    }


class MinionDagAdvancerTests(unittest.TestCase):
    def test_dag_spec_and_state_are_serializable_boundaries(self) -> None:
        spec = dag_spec_from_validation(
            _validation(),
            existing={
                "default_executor_profile": "software_engineering.coder",
                "node_executors": {"final": "software_engineering.reviewer"},
            },
        )
        self.assertIsInstance(spec, DagSpec)
        self.assertEqual(spec.node_order, ("contracts", "engine", "renderer", "final"))
        self.assertEqual(spec.depends_on["final"], ("engine", "renderer"))

        state = dag_state_from_validation(_validation(), existing=spec.to_dict())
        self.assertIsInstance(state, DagState)
        serialized = state.to_dict()
        self.assertEqual(serialized["module_order"], ["contracts", "engine", "renderer", "final"])
        self.assertEqual(serialized["ready_modules"], ["contracts"])
        self.assertEqual(serialized["default_executor_profile"], "software_engineering.coder")
        self.assertEqual(serialized["node_executors"], {"final": "software_engineering.reviewer"})

        result = DagAdvanceResult.from_dag(serialized, tick_reason="test").to_dict()
        self.assertEqual(result["status"], "awaiting_continue")
        self.assertEqual(result["next_node_id"], "contracts")
        self.assertEqual(result["next_module_id"], "contracts")
        self.assertEqual(result["ready_node_ids"], ["contracts"])
        self.assertEqual(result["ready_module_ids"], ["contracts"])
        self.assertEqual(result["tick_reason"], "test")

    def test_storage_shape_uses_node_fields_and_round_trips_to_runtime_aliases(self) -> None:
        dag = build_module_dag_from_validation(_validation())
        storage = dag_state_to_storage_dict(dag)

        self.assertEqual(storage["node_order"], ["contracts", "engine", "renderer", "final"])
        self.assertEqual(storage["ready_nodes"], ["contracts"])
        self.assertEqual(storage["node_status"]["engine"], "blocked")
        self.assertNotIn("module_order", storage)
        self.assertNotIn("ready_modules", storage)
        self.assertNotIn("module_status", storage)

        runtime = dag_state_to_runtime_dict(storage)
        self.assertEqual(runtime["module_order"], ["contracts", "engine", "renderer", "final"])
        self.assertEqual(runtime["ready_modules"], ["contracts"])
        self.assertEqual(runtime["module_status"]["engine"], "blocked")

    def test_builds_ready_state_from_validation(self) -> None:
        dag = build_module_dag_from_validation(_validation())

        self.assertEqual(dag["module_order"], ["contracts", "engine", "renderer", "final"])
        self.assertEqual(dag["ready_modules"], ["contracts"])
        self.assertEqual(dag["module_status"]["engine"], "blocked")
        self.assertEqual(dag["remaining_indegree"]["final"], 2)
        self.assertEqual(ready_module_ids(dag), ["contracts"])
        self.assertEqual(module_dag_status(dag), "awaiting_continue")

    def test_marks_running_and_completes_dependencies(self) -> None:
        dag = build_module_dag_from_validation(_validation())
        running = claim_ready_modules(dag, {"contracts": "wo_parent_contracts"}, limit=1)

        self.assertEqual(running["status"], "running_module")
        self.assertEqual(running["claims"], [{"module_id": "contracts", "child_work_order_id": "wo_parent_contracts"}])
        self.assertEqual(running["active_child_work_order_ids"], ["wo_parent_contracts"])
        self.assertEqual(running["dag"]["module_status"]["contracts"], "running")

        completed = complete_module(
            running["dag"],
            "contracts",
            child_output={"child_work_order_id": "wo_parent_contracts", "commit_sha": "abc"},
            child_work_order_id="wo_parent_contracts",
        )

        self.assertTrue(completed["advanced"])
        self.assertEqual(completed["status"], "awaiting_continue")
        self.assertEqual(completed["completed_modules"], ["contracts"])
        self.assertEqual(completed["ready_module_ids"], ["engine", "renderer"])
        self.assertEqual(completed["dag"]["remaining_indegree"]["engine"], 0)
        self.assertEqual(completed["dag"]["module_outputs"]["contracts"]["commit_sha"], "abc")

    def test_complete_rejects_stale_child_work_order(self) -> None:
        dag = build_module_dag_from_validation(_validation())
        running = claim_ready_modules(dag, {"contracts": "wo_current_contracts"}, limit=1)["dag"]

        rejected = complete_module(
            running,
            "contracts",
            child_output={"child_work_order_id": "wo_old_contracts", "commit_sha": "old"},
            child_work_order_id="wo_old_contracts",
        )

        self.assertFalse(rejected["advanced"])
        self.assertEqual(rejected["reason"], "stale_child_work_order")
        self.assertEqual(rejected["dag"]["module_status"]["contracts"], "running")
        self.assertEqual(rejected["dag"]["completed_modules"], [])
        self.assertNotIn("contracts", rejected["dag"]["module_outputs"])

    def test_releases_running_module_as_ready_or_blocked(self) -> None:
        dag = build_module_dag_from_validation(_validation())
        running = mark_modules_running(dag, {"contracts": "wo_parent_contracts"})["dag"]

        released = release_running_module(running, "wo_parent_contracts")
        self.assertEqual(released["released_module_id"], "contracts")
        self.assertEqual(released["status"], "awaiting_continue")
        self.assertEqual(released["ready_module_ids"], ["contracts"])
        self.assertEqual(released["dag"]["module_status"]["contracts"], "ready")

        failed = release_running_module(running, "wo_parent_contracts", terminal_failure=True)
        self.assertEqual(failed["released_module_id"], "contracts")
        self.assertEqual(failed["status"], "blocked")
        self.assertEqual(failed["ready_module_ids"], [])
        self.assertEqual(failed["dag"]["module_status"]["contracts"], "blocked")

    def test_repair_replay_invalidates_target_and_downstream_context(self) -> None:
        dag = build_module_dag_from_validation(_validation())
        dag = complete_module(mark_modules_running(dag, {"contracts": "wo_contracts"})["dag"], "contracts")["dag"]
        dag = mark_modules_running(dag, {"engine": "wo_engine", "renderer": "wo_renderer"})["dag"]
        dag = complete_module(dag, "engine", child_output={"child_work_order_id": "wo_engine"})["dag"]
        child_ids = {"contracts": "wo_contracts", "engine": "wo_engine", "renderer": "wo_renderer"}

        replay = apply_repair_replay(
            dag,
            ["engine"],
            child_work_order_ids=child_ids,
            replay_attempts={"engine": 1},
            completed_modules=["contracts", "engine"],
        )

        self.assertEqual(replay["affected_modules"], ["engine", "final"])
        self.assertEqual(replay["invalidated_child_work_order_ids"], ["wo_engine"])
        self.assertNotIn("engine", replay["child_work_order_ids"])
        self.assertEqual(replay["replay_attempts"]["engine"], 2)
        self.assertEqual(replay["replay_attempts"]["final"], 1)
        self.assertEqual(replay["dag"]["module_status"]["engine"], "needs_repair")
        self.assertEqual(replay["dag"]["module_status"]["final"], "stale")
        self.assertEqual(replay["ready_module_ids"], ["engine"])
        self.assertEqual(replay["dag"]["completed_modules"], ["contracts"])

    def test_repair_replay_rewinds_running_downstream_siblings_to_replay_upstream(self) -> None:
        dag = build_module_dag_from_validation(_validation())
        dag = mark_modules_running(dag, {"contracts": "wo_contracts"})["dag"]
        dag = complete_module(
            dag,
            "contracts",
            child_output={"child_work_order_id": "wo_contracts", "commit_sha": "a1"},
            child_work_order_id="wo_contracts",
        )["dag"]
        dag = claim_ready_modules(dag, {"engine": "wo_engine", "renderer": "wo_renderer"}, limit=2)["dag"]

        replay = apply_repair_replay(
            dag,
            ["contracts"],
            child_work_order_ids={
                "contracts": "wo_contracts",
                "engine": "wo_engine",
                "renderer": "wo_renderer",
            },
            completed_modules=["contracts"],
        )

        self.assertEqual(replay["affected_modules"], ["contracts", "engine", "renderer", "final"])
        self.assertEqual(replay["invalidated_child_work_order_ids"], ["wo_contracts", "wo_engine", "wo_renderer"])
        self.assertEqual(replay["child_work_order_ids"], {})
        self.assertEqual(replay["ready_module_ids"], ["contracts"])
        self.assertEqual(replay["active_child_work_order_ids"], [])
        self.assertEqual(replay["dag"]["running_modules"], {})
        self.assertEqual(replay["dag"]["completed_modules"], [])
        self.assertEqual(replay["dag"]["module_outputs"], {})
        self.assertEqual(replay["dag"]["module_status"]["contracts"], "needs_repair")
        self.assertEqual(replay["dag"]["module_status"]["engine"], "stale")
        self.assertEqual(replay["dag"]["module_status"]["renderer"], "stale")
        self.assertEqual(replay["dag"]["module_status"]["final"], "stale")
        self.assertEqual(replay["dag"]["remaining_indegree"]["engine"], 1)
        self.assertEqual(replay["dag"]["remaining_indegree"]["renderer"], 1)
        self.assertEqual(module_dag_status(replay["dag"]), "awaiting_continue")

        stale_engine_completion = complete_module(
            replay["dag"],
            "engine",
            child_output={"child_work_order_id": "wo_engine", "commit_sha": "stale"},
            child_work_order_id="wo_engine",
        )
        self.assertFalse(stale_engine_completion["advanced"])
        self.assertEqual(stale_engine_completion["reason"], "module_not_running")

        rerun_a = claim_ready_modules(replay["dag"], {"contracts": "wo_contracts_r1"}, limit=1)["dag"]
        rerun_a = complete_module(
            rerun_a,
            "contracts",
            child_output={"child_work_order_id": "wo_contracts_r1", "commit_sha": "a2"},
            child_work_order_id="wo_contracts_r1",
        )["dag"]

        self.assertEqual(ready_module_ids(rerun_a), ["engine", "renderer"])
        self.assertEqual(rerun_a["module_status"]["engine"], "ready")
        self.assertEqual(rerun_a["module_status"]["renderer"], "ready")
        self.assertEqual(rerun_a["module_status"]["final"], "stale")
        self.assertEqual(rerun_a["remaining_indegree"]["engine"], 0)
        self.assertEqual(rerun_a["remaining_indegree"]["renderer"], 0)
        self.assertEqual(rerun_a["remaining_indegree"]["final"], 2)
        self.assertEqual(rerun_a["completed_modules"], ["contracts"])
        self.assertEqual(rerun_a["module_outputs"]["contracts"]["commit_sha"], "a2")


if __name__ == "__main__":
    unittest.main()
