from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "capture_public_proof.py"
SPEC = importlib.util.spec_from_file_location("capture_public_proof", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PublicProofTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "data" / "bunshin" / "bunshin.sqlite3"
        self.database.parent.mkdir(parents=True)
        self.artifact = self.root / "data" / "bunshin" / "artifact.json"
        self.artifact.write_text('{"proof":true}\n', encoding="utf-8")
        self.artifact_sha = MODULE._typed_artifact_digest(
            self.artifact.read_bytes(),
            artifact_type="DemoArtifact",
            schema_version="1",
            media_type="application/json",
        )
        self._seed_database()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _seed_database(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.executescript(
            """
            CREATE TABLE bunshin_v2_schema_meta (schema_version INTEGER);
            CREATE TABLE bunshin_v2_aggregate_snapshots (
                aggregate_type TEXT, aggregate_id TEXT, workflow_id TEXT,
                state TEXT, version INTEGER, payload_json TEXT,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE bunshin_v2_task_projection (
                task_id TEXT, state TEXT, title TEXT, objective TEXT,
                profile_id TEXT, family_id TEXT, task_revision_sha TEXT,
                owner TEXT, updated_at TEXT
            );
            CREATE TABLE bunshin_v2_workflow_projection (
                workflow_id TEXT, current_phase TEXT, workflow_state TEXT,
                active_aggregate_type TEXT, active_aggregate_id TEXT,
                active_worker_id TEXT, waiting_for_user INTEGER,
                liveness TEXT, updated_at TEXT
            );
            CREATE TABLE bunshin_v2_domain_events (
                event_id TEXT, workflow_id TEXT, aggregate_type TEXT,
                aggregate_id TEXT, aggregate_version INTEGER, event_type TEXT,
                action_id TEXT, correlation_id TEXT, causation_id TEXT,
                created_at TEXT
            );
            CREATE TABLE bunshin_v2_role_invocations (
                invocation_id TEXT, workflow_id TEXT, aggregate_type TEXT,
                aggregate_id TEXT, role TEXT, mode TEXT, role_profile_id TEXT,
                harness_id TEXT, harness_generation TEXT, status TEXT,
                last_completed_turn INTEGER, total_input_tokens INTEGER,
                total_output_tokens INTEGER, total_latency_ms INTEGER,
                total_tool_latency_ms INTEGER, total_wall_latency_ms INTEGER,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE bunshin_v2_role_assignments (
                assignment_id TEXT, session_id TEXT, workflow_id TEXT,
                aggregate_type TEXT, aggregate_id TEXT, role TEXT, mode TEXT,
                role_profile_id TEXT, state TEXT, active_attempt_id TEXT,
                submission_kind TEXT, submission_artifact_ref_json TEXT,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE bunshin_v2_role_attempts (
                attempt_id TEXT, assignment_id TEXT, attempt_index INTEGER,
                fencing_token INTEGER, harness_id TEXT,
                harness_generation TEXT, status TEXT, error_kind TEXT,
                started_at TEXT, finished_at TEXT, updated_at TEXT
            );
            CREATE TABLE bunshin_v2_delivery_outbox (
                delivery_id TEXT, workflow_id TEXT, event_kind TEXT,
                status TEXT, attempt_count INTEGER, created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE bunshin_v2_artifacts (
                sha256 TEXT, artifact_type TEXT, schema_version TEXT,
                media_type TEXT, byte_size INTEGER, storage_path TEXT,
                durable INTEGER, created_at TEXT
            );
            """
        )
        now = "2026-08-14T07:00:00+00:00"
        workflow_payload = json.dumps(
            {
                "task_id": "task_demo",
                "request_ref": {"sha256": self.artifact_sha},
                "last_action_type": "START_WORKFLOW",
                "workflow_name": "private demo title",
                "workspace_path": str(self.root),
            }
        )
        values = [
            ("workflow", "wf_demo", "wf_demo", "ACTIVE", 1, workflow_payload, now, now),
        ]
        connection.executemany(
            "INSERT INTO bunshin_v2_aggregate_snapshots VALUES (?,?,?,?,?,?,?,?)",
            values,
        )
        connection.execute("INSERT INTO bunshin_v2_schema_meta VALUES (29)")
        connection.execute(
            "INSERT INTO bunshin_v2_task_projection VALUES (?,?,?,?,?,?,?,?,?)",
            ("task_demo", "ACTIVE", "Demo", "Prove recovery", "generic", "general", self.artifact_sha, "pal", now),
        )
        connection.execute(
            "INSERT INTO bunshin_v2_workflow_projection VALUES (?,?,?,?,?,?,?,?,?)",
            ("wf_demo", "implementation", "ACTIVE", "dag_node_run", "node_demo", "inv_demo", 0, "live_lease", now),
        )
        connection.execute(
            "INSERT INTO bunshin_v2_domain_events VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("evt_1", "wf_demo", "workflow", "wf_demo", 1, "workflow.create_workflow", "action_1", "", "", now),
        )
        connection.execute(
            "INSERT INTO bunshin_v2_role_invocations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("inv_demo", "wf_demo", "workflow", "wf_demo", "writer", "author", "generic", "pal", "gen", "running", 1, 10, 5, 20, 2, 22, now, now),
        )
        connection.execute(
            "INSERT INTO bunshin_v2_role_assignments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("asg_demo", "inv_demo", "wf_demo", "workflow", "wf_demo", "writer", "author", "generic", "running", "att_2", "artifact", "{}", now, now),
        )
        connection.executemany(
            "INSERT INTO bunshin_v2_role_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("att_1", "asg_demo", 1, 1, "pal", "gen", "lost", "worker_process_failed", now, now, now),
                ("att_2", "asg_demo", 2, 2, "pal", "gen", "running", "", now, "", now),
            ],
        )
        connection.execute(
            "INSERT INTO bunshin_v2_artifacts VALUES (?,?,?,?,?,?,?,?)",
            (self.artifact_sha, "DemoArtifact", "1", "application/json", self.artifact.stat().st_size, str(self.artifact), 1, now),
        )
        connection.commit()
        connection.close()

    def test_collects_recovery_and_verifies_artifacts_without_contents(self) -> None:
        proof = MODULE.collect_proof(runtime_root=self.root, workflow_id="wf_demo")

        self.assertTrue(proof["checks"]["event_chains_contiguous"])
        self.assertTrue(proof["checks"]["referenced_artifact_hashes_valid"])
        self.assertTrue(proof["checks"]["recovery_observed"])
        self.assertEqual(proof["recoveries"][0]["fencing_tokens"], [1, 2])
        serialized = json.dumps(proof)
        self.assertNotIn("proof\":true", serialized)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("Prove recovery", serialized)
        self.assertNotIn("title", proof["task"])
        self.assertNotIn("objective", proof["task"])
        self.assertNotIn("workflow_name", proof["workflow"]["payload"])
        self.assertNotIn("workspace_path", proof["workflow"]["payload"])

    def test_task_text_requires_explicit_public_opt_in(self) -> None:
        proof = MODULE.collect_proof(
            runtime_root=self.root,
            workflow_id="wf_demo",
            include_task_text=True,
        )

        self.assertEqual(proof["task"]["title"], "Demo")
        self.assertEqual(proof["task"]["objective"], "Prove recovery")

    def test_unknown_workflow_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown workflow"):
            MODULE.collect_proof(runtime_root=self.root, workflow_id="missing")

    def test_corrupt_referenced_artifact_fails_hash_check(self) -> None:
        self.artifact.write_text('{"proof":false}\n', encoding="utf-8")

        proof = MODULE.collect_proof(runtime_root=self.root, workflow_id="wf_demo")

        self.assertFalse(proof["checks"]["referenced_artifact_hashes_valid"])

    def test_event_version_gap_fails_contiguity_check(self) -> None:
        connection = sqlite3.connect(self.database)
        now = "2026-08-14T07:01:00+00:00"
        connection.execute(
            "INSERT INTO bunshin_v2_domain_events VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("evt_3", "wf_demo", "workflow", "wf_demo", 3, "workflow.gap", "action_3", "", "", now),
        )
        connection.commit()
        connection.close()

        proof = MODULE.collect_proof(runtime_root=self.root, workflow_id="wf_demo")

        self.assertFalse(proof["checks"]["event_chains_contiguous"])


if __name__ == "__main__":
    unittest.main()
