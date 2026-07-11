from __future__ import annotations

import sqlite3


MINION_V2_SCHEMA_VERSION = 4


def ensure_minion_v2_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS minion_v2_schema_meta (
            schema_key TEXT PRIMARY KEY,
            schema_value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS minion_v2_aggregate_snapshots (
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            workflow_id TEXT NOT NULL,
            state TEXT NOT NULL,
            version INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (aggregate_type, aggregate_id)
        );

        CREATE INDEX IF NOT EXISTS minion_v2_snapshots_workflow
        ON minion_v2_aggregate_snapshots(workflow_id, aggregate_type, updated_at);

        CREATE TABLE IF NOT EXISTS minion_v2_task_projection (
            task_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            title TEXT NOT NULL,
            objective TEXT NOT NULL,
            family_id TEXT NOT NULL,
            workspace_key TEXT NOT NULL DEFAULT '',
            task_revision_sha TEXT NOT NULL,
            owner TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS minion_v2_task_search
        ON minion_v2_task_projection(state, family_id, updated_at);

        CREATE TABLE IF NOT EXISTS minion_v2_domain_events (
            event_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            aggregate_version INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            action_id TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            causation_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(aggregate_type, aggregate_id, aggregate_version, event_type)
        );

        CREATE INDEX IF NOT EXISTS minion_v2_events_workflow
        ON minion_v2_domain_events(workflow_id, created_at, event_id);

        CREATE TABLE IF NOT EXISTS minion_v2_action_dedup (
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            action_id TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (aggregate_type, aggregate_id, idempotency_key)
        );

        CREATE TABLE IF NOT EXISTS minion_v2_outbox (
            effect_id TEXT PRIMARY KEY,
            effect_key TEXT NOT NULL UNIQUE,
            workflow_id TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            effect_index INTEGER NOT NULL,
            effect_type TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 8,
            provider_request_id TEXT NOT NULL DEFAULT '',
            result_artifact_ref_json TEXT NOT NULL DEFAULT '{}',
            next_retry_at TEXT NOT NULL,
            locked_by TEXT NOT NULL DEFAULT '',
            locked_until TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS minion_v2_outbox_ready
        ON minion_v2_outbox(status, next_retry_at, created_at);

        CREATE TABLE IF NOT EXISTS minion_v2_effect_receipts (
            effect_key TEXT PRIMARY KEY,
            effect_id TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            provider_request_id TEXT NOT NULL DEFAULT '',
            result_artifact_ref_json TEXT NOT NULL DEFAULT '{}',
            completed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS minion_v2_leases (
            resource_key TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL DEFAULT '',
            fencing_token INTEGER NOT NULL DEFAULT 0,
            acquired_at TEXT NOT NULL DEFAULT '',
            renewed_at TEXT NOT NULL DEFAULT '',
            expires_at TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS minion_v2_artifacts (
            sha256 TEXT PRIMARY KEY,
            artifact_type TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            media_type TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            storage_path TEXT NOT NULL,
            durable INTEGER NOT NULL DEFAULT 0,
            provenance_json TEXT NOT NULL DEFAULT '{}',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS minion_v2_artifact_refs (
            parent_sha256 TEXT NOT NULL,
            child_sha256 TEXT NOT NULL,
            relation TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(parent_sha256, child_sha256, relation)
        );

        CREATE TABLE IF NOT EXISTS minion_v2_human_decisions (
            token_hash TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            architecture_revision_id TEXT NOT NULL,
            manifest_sha TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            active_channel_id TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'issued',
            decision TEXT NOT NULL DEFAULT '',
            action_id TEXT NOT NULL DEFAULT '',
            issued_at TEXT NOT NULL,
            consumed_at TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS minion_v2_human_decisions_revision
        ON minion_v2_human_decisions(workflow_id, architecture_revision_id, status);

        CREATE TABLE IF NOT EXISTS minion_v2_worker_invocations (
            invocation_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            lease_resource_key TEXT NOT NULL,
            fencing_token INTEGER NOT NULL,
            role TEXT NOT NULL,
            prompt_pack_ref_json TEXT NOT NULL,
            status TEXT NOT NULL,
            last_completed_turn INTEGER NOT NULL DEFAULT 0,
            total_input_tokens INTEGER NOT NULL DEFAULT 0,
            total_output_tokens INTEGER NOT NULL DEFAULT 0,
            total_cost REAL NOT NULL DEFAULT 0,
            total_latency_ms INTEGER NOT NULL DEFAULT 0,
            total_tool_latency_ms INTEGER NOT NULL DEFAULT 0,
            total_wall_latency_ms INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS minion_v2_worker_turns (
            invocation_id TEXT NOT NULL,
            turn_index INTEGER NOT NULL,
            llm_request_ref_json TEXT NOT NULL,
            llm_response_ref_json TEXT NOT NULL,
            tool_summary_ref_json TEXT NOT NULL DEFAULT '{}',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cost REAL NOT NULL DEFAULT 0,
            latency_ms INTEGER NOT NULL DEFAULT 0,
            tool_latency_ms INTEGER NOT NULL DEFAULT 0,
            wall_latency_ms INTEGER NOT NULL DEFAULT 0,
            completed_at TEXT NOT NULL,
            PRIMARY KEY(invocation_id, turn_index)
        );

        CREATE TABLE IF NOT EXISTS minion_v2_node_journals (
            node_run_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            lease_resource_key TEXT NOT NULL,
            fencing_token INTEGER NOT NULL,
            generation INTEGER NOT NULL,
            journal_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS minion_v2_workflow_projection (
            workflow_id TEXT PRIMARY KEY,
            current_phase TEXT NOT NULL,
            workflow_state TEXT NOT NULL,
            active_aggregate_type TEXT NOT NULL DEFAULT '',
            active_aggregate_id TEXT NOT NULL DEFAULT '',
            active_worker_id TEXT NOT NULL DEFAULT '',
            blocker_json TEXT NOT NULL DEFAULT '{}',
            next_legal_actions_json TEXT NOT NULL DEFAULT '[]',
            waiting_for_user INTEGER NOT NULL DEFAULT 0,
            liveness TEXT NOT NULL DEFAULT '',
            metrics_json TEXT NOT NULL DEFAULT '{}',
            last_progress_event_id TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS minion_v2_node_projection (
            node_run_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            epoch_id TEXT NOT NULL DEFAULT '',
            unit_id TEXT NOT NULL DEFAULT '',
            node_kind TEXT NOT NULL DEFAULT 'unit',
            state TEXT NOT NULL,
            dependency_node_ids_json TEXT NOT NULL DEFAULT '[]',
            active_worker_id TEXT NOT NULL DEFAULT '',
            candidate_digest TEXT NOT NULL DEFAULT '',
            blocker_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );
        """
    )
    _ensure_column(connection, "minion_v2_worker_invocations", "total_tool_latency_ms", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(connection, "minion_v2_worker_invocations", "total_wall_latency_ms", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(connection, "minion_v2_worker_turns", "tool_latency_ms", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(connection, "minion_v2_worker_turns", "wall_latency_ms", "INTEGER NOT NULL DEFAULT 0")
    connection.execute(
        """
        INSERT INTO minion_v2_schema_meta(schema_key, schema_value)
        VALUES ('schema_version', ?)
        ON CONFLICT(schema_key) DO UPDATE SET schema_value = excluded.schema_value
        """,
        (str(MINION_V2_SCHEMA_VERSION),),
    )


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
