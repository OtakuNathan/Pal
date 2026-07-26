from __future__ import annotations

import json
import sqlite3


MINION_V2_SCHEMA_VERSION = 22


def ensure_minion_v2_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS minion_v2_schema_meta (
            schema_key TEXT PRIMARY KEY,
            schema_value TEXT NOT NULL
        )
        """
    )
    previous_version = _schema_version(connection)
    if previous_version < 18:
        _rename_role_protocol_tables_v18(connection)
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
            profile_id TEXT NOT NULL DEFAULT '',
            family_id TEXT NOT NULL,
            workspace_key TEXT NOT NULL DEFAULT '',
            task_revision_sha TEXT NOT NULL,
            owner TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS minion_v2_task_search
        ON minion_v2_task_projection(state, family_id, updated_at);

        CREATE VIRTUAL TABLE IF NOT EXISTS minion_v2_tasks_fts USING fts5(
            task_id UNINDEXED,
            title,
            objective,
            workspace
        );

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

        CREATE TABLE IF NOT EXISTS minion_v2_role_invocations (
            invocation_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            lease_resource_key TEXT NOT NULL,
            fencing_token INTEGER NOT NULL,
            role TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT '',
            executor_profile_id TEXT NOT NULL DEFAULT '',
            family_binding_sha TEXT NOT NULL DEFAULT '',
            authoring_contract_version TEXT NOT NULL DEFAULT '',
            prompt_pack_ref_json TEXT NOT NULL,
            continuation_ref_json TEXT NOT NULL DEFAULT '{}',
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

        CREATE TABLE IF NOT EXISTS minion_v2_role_sessions (
            session_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            role TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT '',
            executor_profile_id TEXT NOT NULL DEFAULT '',
            family_binding_sha TEXT NOT NULL DEFAULT '',
            scope_kind TEXT NOT NULL DEFAULT '',
            subject_key TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            continuation_ref_json TEXT NOT NULL DEFAULT '{}',
            execution_state_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS minion_v2_role_assignments (
            assignment_id TEXT PRIMARY KEY,
            assignment_key TEXT NOT NULL UNIQUE,
            request_hash TEXT NOT NULL,
            session_id TEXT NOT NULL,
            workflow_id TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            role TEXT NOT NULL,
            mode TEXT NOT NULL,
            executor_profile_id TEXT NOT NULL,
            family_binding_sha TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            required_inputs_json TEXT NOT NULL DEFAULT '[]',
            input_refs_json TEXT NOT NULL DEFAULT '{}',
            execution_spec_json TEXT NOT NULL DEFAULT '{}',
            submission_kind TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued',
            active_attempt_id TEXT NOT NULL DEFAULT '',
            submission_artifact_ref_json TEXT NOT NULL DEFAULT '{}',
            submission_payload_hash TEXT NOT NULL DEFAULT '',
            settlement_action_json TEXT NOT NULL DEFAULT '{}',
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES minion_v2_role_sessions(session_id)
        );

        CREATE INDEX IF NOT EXISTS minion_v2_role_assignments_ready
        ON minion_v2_role_assignments(state, updated_at, assignment_id);

        CREATE INDEX IF NOT EXISTS minion_v2_role_assignments_aggregate
        ON minion_v2_role_assignments(
            workflow_id, aggregate_type, aggregate_id, created_at
        );

        CREATE TABLE IF NOT EXISTS minion_v2_role_attempts (
            attempt_id TEXT PRIMARY KEY,
            assignment_id TEXT NOT NULL,
            attempt_index INTEGER NOT NULL,
            lease_resource_key TEXT NOT NULL,
            fencing_token INTEGER NOT NULL,
            process_group_id INTEGER NOT NULL DEFAULT 0,
            access_token_hash TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'starting',
            prompt_pack_ref_json TEXT NOT NULL DEFAULT '{}',
            response_artifact_ref_json TEXT NOT NULL DEFAULT '{}',
            error_kind TEXT NOT NULL DEFAULT '',
            error_text TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            UNIQUE(assignment_id, attempt_index),
            FOREIGN KEY(assignment_id) REFERENCES minion_v2_role_assignments(assignment_id)
        );

        CREATE INDEX IF NOT EXISTS minion_v2_role_attempts_assignment
        ON minion_v2_role_attempts(assignment_id, attempt_index);

        CREATE TABLE IF NOT EXISTS minion_v2_effect_attempts (
            effect_id TEXT NOT NULL,
            attempt_index INTEGER NOT NULL,
            worker_id TEXT NOT NULL,
            status TEXT NOT NULL,
            error_kind TEXT NOT NULL DEFAULT '',
            error_text TEXT NOT NULL DEFAULT '',
            provider_request_id TEXT NOT NULL DEFAULT '',
            result_artifact_ref_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(effect_id, attempt_index)
        );

        CREATE TABLE IF NOT EXISTS minion_v2_worker_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            invocation_id TEXT NOT NULL,
            event_kind TEXT NOT NULL,
            phase TEXT NOT NULL DEFAULT '',
            round_index INTEGER NOT NULL DEFAULT 0,
            tool_call_count INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS minion_v2_worker_events_invocation
        ON minion_v2_worker_events(invocation_id, event_id);

        CREATE TABLE IF NOT EXISTS minion_v2_role_turns (
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

        CREATE TABLE IF NOT EXISTS minion_v2_submission_drafts (
            draft_key TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            invocation_id TEXT NOT NULL,
            lease_resource_key TEXT NOT NULL,
            fencing_token INTEGER NOT NULL,
            role TEXT NOT NULL,
            mode TEXT NOT NULL,
            draft_kind TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            authoring_contract_version TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            payload_json TEXT NOT NULL DEFAULT '{}',
            source_draft_key TEXT NOT NULL DEFAULT '',
            submitted_artifact_ref_json TEXT NOT NULL DEFAULT '{}',
            submission_payload_hash TEXT NOT NULL DEFAULT '',
            submitted_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS minion_v2_submission_drafts_invocation
        ON minion_v2_submission_drafts(invocation_id, role, draft_kind, fencing_token DESC);

        CREATE INDEX IF NOT EXISTS minion_v2_submission_drafts_lineage
        ON minion_v2_submission_drafts(
            workflow_id, role, draft_kind, input_fingerprint, updated_at DESC
        );

        CREATE TABLE IF NOT EXISTS minion_v2_submission_draft_ops (
            draft_key TEXT NOT NULL,
            operation_key TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(draft_key, operation_key),
            FOREIGN KEY(draft_key) REFERENCES minion_v2_submission_drafts(draft_key)
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

        CREATE TABLE IF NOT EXISTS minion_v2_channel_bindings (
            actor_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            workflow_id TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(actor_id, channel_id)
        );

        CREATE TABLE IF NOT EXISTS minion_v2_artifact_aliases (
            actor_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            alias TEXT NOT NULL,
            artifact_sha256 TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(actor_id, channel_id, alias)
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
    _ensure_column(connection, "minion_v2_role_invocations", "total_tool_latency_ms", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(connection, "minion_v2_task_projection", "profile_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "minion_v2_role_invocations", "total_wall_latency_ms", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(connection, "minion_v2_role_invocations", "continuation_ref_json", "TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(connection, "minion_v2_role_invocations", "authoring_contract_version", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "minion_v2_role_sessions", "scope_kind", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "minion_v2_role_sessions", "subject_key", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "minion_v2_role_sessions", "execution_state_json", "TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(connection, "minion_v2_role_turns", "tool_latency_ms", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(connection, "minion_v2_role_turns", "wall_latency_ms", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(connection, "minion_v2_submission_drafts", "submitted_artifact_ref_json", "TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(connection, "minion_v2_submission_drafts", "submission_payload_hash", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "minion_v2_submission_drafts", "submitted_at", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "minion_v2_submission_drafts", "mode", "TEXT NOT NULL DEFAULT ''")
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS minion_v2_submission_drafts_role_mode
        ON minion_v2_submission_drafts(
            workflow_id, role, mode, draft_kind, input_fingerprint, updated_at DESC
        )
        """
    )
    _ensure_column(connection, "minion_v2_role_attempts", "access_token_hash", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "minion_v2_role_assignments", "execution_spec_json", "TEXT NOT NULL DEFAULT '{}'")
    for table in (
        "minion_v2_role_invocations",
        "minion_v2_role_sessions",
        "minion_v2_role_assignments",
    ):
        _ensure_column(connection, table, "mode", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, table, "executor_profile_id", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, table, "family_binding_sha", "TEXT NOT NULL DEFAULT ''")
    if previous_version < 15:
        _migrate_role_state_ownership_v15(connection)
    if previous_version < 16:
        _migrate_superseded_architecture_revisions_v16(connection)
    if previous_version < 18:
        _migrate_role_protocol_v18(connection)
    if previous_version < 19:
        _quiesce_pre_scope_workflows_v19(connection)
    elif previous_version < 20:
        _quiesce_candidate_scoped_verifiers_v20(connection)
    if previous_version < 22:
        _archive_pre_module_identity_workflows_v22(connection)
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS minion_v2_role_assignments_one_open
        ON minion_v2_role_assignments(session_id)
        WHERE state IN (
            'queued', 'claimed', 'running', 'retry_queued', 'result_recorded'
        )
        """
    )
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


def _schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT schema_value FROM minion_v2_schema_meta WHERE schema_key = 'schema_version'"
    ).fetchone()
    try:
        return int(row[0]) if row is not None else 0
    except (TypeError, ValueError):
        return 0


def _rename_role_protocol_tables_v18(connection: sqlite3.Connection) -> None:
    existing = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    for old_name, new_name in (
        ("minion_v2_worker_invocations", "minion_v2_role_invocations"),
        ("minion_v2_worker_sessions", "minion_v2_role_sessions"),
        ("minion_v2_worker_assignments", "minion_v2_role_assignments"),
        ("minion_v2_worker_attempts", "minion_v2_role_attempts"),
        ("minion_v2_worker_turns", "minion_v2_role_turns"),
    ):
        if old_name in existing and new_name not in existing:
            connection.execute(f"ALTER TABLE {old_name} RENAME TO {new_name}")
    for index_name in (
        "minion_v2_worker_assignments_ready",
        "minion_v2_worker_assignments_aggregate",
        "minion_v2_worker_attempts_assignment",
    ):
        connection.execute(f"DROP INDEX IF EXISTS {index_name}")


def _quiesce_pre_scope_workflows_v19(connection: sqlite3.Connection) -> None:
    """Require an explicit restart for workflows compiled under the old topology.

    Immutable history, artifacts, task ledgers, and completed workflows remain
    readable.  Non-terminal role work is fenced off instead of being guessed
    into the architecture-cycle/module/system session model.
    """

    _quiesce_active_workflows_for_role_contract(
        connection,
        reason=(
            "workflow was active before role sessions became architecture-cycle, "
            "module, and workflow-scoped; restart it explicitly"
        ),
        orchestration_contract_version="3",
    )


def _quiesce_candidate_scoped_verifiers_v20(
    connection: sqlite3.Connection,
) -> None:
    """Fence active work before module/workflow verifier sessions become durable."""

    _quiesce_active_workflows_for_role_contract(
        connection,
        reason=(
            "workflow was active before module and system verifier sessions became "
            "workflow-lifetime logical coroutines; restart it explicitly"
        ),
        orchestration_contract_version="4",
    )


def _archive_pre_module_identity_workflows_v22(
    connection: sqlite3.Connection,
) -> None:
    """Retire active v4 work instead of guessing it into Module ownership."""

    rows = connection.execute(
        """
        SELECT aggregate_id, payload_json
        FROM minion_v2_aggregate_snapshots
        WHERE aggregate_type = 'workflow'
          AND state NOT IN ('COMPLETED', 'REJECTED', 'CANCELLED')
          AND COALESCE(
              json_extract(payload_json, '$.orchestration_contract_version'),
              '4'
          ) != '5'
        """
    ).fetchall()
    workflow_ids = [str(row[0]) for row in rows]
    if not workflow_ids:
        return
    reason = (
        "archived during orchestration v5 cutover; restart as a fresh workflow "
        "with workflow-owned Module identities"
    )
    for workflow_id, payload_json in rows:
        payload = json.loads(str(payload_json or "{}"))
        payload.update(
            {
                "archived": True,
                "archive_reason": reason,
                "cancel_reason": reason,
                "retired_orchestration_contract_version": str(
                    payload.get("orchestration_contract_version") or "4"
                ),
                "required_orchestration_contract_version": "5",
            }
        )
        payload.pop("blocker", None)
        connection.execute(
            """
            UPDATE minion_v2_aggregate_snapshots
            SET state = 'CANCELLED', version = version + 1,
                payload_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE aggregate_type = 'workflow' AND aggregate_id = ?
            """,
            (
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                workflow_id,
            ),
        )
    placeholders = ", ".join("?" for _ in workflow_ids)
    connection.execute(
        f"""
        UPDATE minion_v2_aggregate_snapshots
        SET state = 'CANCELLED', version = version + 1,
            payload_json = json_set(
                payload_json,
                '$.cancel_reason',
                ?,
                '$.required_orchestration_contract_version',
                '5'
            ),
            updated_at = CURRENT_TIMESTAMP
        WHERE workflow_id IN ({placeholders})
          AND aggregate_type != 'workflow'
          AND state NOT IN (
              'ACCEPTED', 'REJECTED', 'SUPERSEDED', 'COMPLETED', 'CANCELLED'
          )
        """,
        (reason, *workflow_ids),
    )
    connection.execute(
        f"""
        UPDATE minion_v2_outbox
        SET status = 'failed', locked_by = '', locked_until = '',
            last_error = 'orchestration_v5_cutover',
            updated_at = CURRENT_TIMESTAMP
        WHERE workflow_id IN ({placeholders})
          AND status IN ('pending', 'processing', 'retry')
        """,
        tuple(workflow_ids),
    )
    connection.execute(
        f"""
        UPDATE minion_v2_role_assignments
        SET state = 'cancelled', active_attempt_id = '',
            last_error = 'orchestration_v5_cutover',
            updated_at = CURRENT_TIMESTAMP
        WHERE workflow_id IN ({placeholders})
          AND state NOT IN ('settled', 'cancelled')
        """,
        tuple(workflow_ids),
    )
    connection.execute(
        f"""
        UPDATE minion_v2_role_attempts
        SET status = 'cancelled',
            error_kind = 'orchestration_v5_cutover',
            error_text = ?,
            finished_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE assignment_id IN (
            SELECT assignment_id FROM minion_v2_role_assignments
            WHERE workflow_id IN ({placeholders})
        )
          AND status NOT IN ('completed', 'failed', 'lost', 'cancelled')
        """,
        (reason, *workflow_ids),
    )
    for table in ("minion_v2_role_sessions", "minion_v2_role_invocations"):
        connection.execute(
            f"""
            UPDATE {table}
            SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
            WHERE workflow_id IN ({placeholders})
              AND status NOT IN ('completed', 'cancelled')
            """,
            tuple(workflow_ids),
        )
    connection.execute(
        f"""
        DELETE FROM minion_v2_leases
        WHERE owner_id IN (
            SELECT session_id FROM minion_v2_role_sessions
            WHERE workflow_id IN ({placeholders})
        )
        """,
        tuple(workflow_ids),
    )


def _quiesce_active_workflows_for_role_contract(
    connection: sqlite3.Connection,
    *,
    reason: str,
    orchestration_contract_version: str,
) -> None:
    workflow_rows = connection.execute(
        """
        SELECT aggregate_id, state, payload_json
        FROM minion_v2_aggregate_snapshots
        WHERE aggregate_type = 'workflow'
          AND state NOT IN ('COMPLETED', 'REJECTED', 'CANCELLED')
        """
    ).fetchall()
    workflow_ids = [str(row[0]) for row in workflow_rows]
    if not workflow_ids:
        return
    blocker = {
        "kind": "orchestration_contract_changed",
        "reason": reason,
        "required_action": "restart_workflow",
        "orchestration_contract_version": orchestration_contract_version,
    }
    for workflow_id, workflow_state, payload_json in workflow_rows:
        payload = json.loads(str(payload_json or "{}"))
        payload["blocker"] = blocker
        payload["orchestration_contract_version"] = orchestration_contract_version
        payload.setdefault("triage_resume_state", str(workflow_state))
        connection.execute(
            """
            UPDATE minion_v2_aggregate_snapshots
            SET state = 'TRIAGE_REQUIRED', version = version + 1,
                payload_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE aggregate_type = 'workflow' AND aggregate_id = ?
            """,
            (
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                str(workflow_id),
            ),
        )
    placeholders = ", ".join("?" for _ in workflow_ids)
    child_types = (
        "architecture_revision",
        "execution_epoch",
        "dag_node_run",
        "standalone_review",
    )
    type_placeholders = ", ".join("?" for _ in child_types)
    connection.execute(
        f"""
        UPDATE minion_v2_aggregate_snapshots
        SET state = 'TRIAGE_REQUIRED', version = version + 1,
            payload_json = json_set(
                payload_json,
                '$.blocker',
                json(?),
                '$.orchestration_contract_version',
                ?,
                '$.triage_resume_state',
                COALESCE(
                    NULLIF(json_extract(payload_json, '$.triage_resume_state'), ''),
                    state
                )
            ),
            updated_at = CURRENT_TIMESTAMP
        WHERE workflow_id IN ({placeholders})
          AND aggregate_type IN ({type_placeholders})
          AND state NOT IN ('ACCEPTED', 'REJECTED', 'SUPERSEDED', 'COMPLETED', 'CANCELLED')
        """,
        (
            json.dumps(blocker, ensure_ascii=False, sort_keys=True),
            orchestration_contract_version,
            *workflow_ids,
            *child_types,
        ),
    )
    connection.execute(
        f"""
        UPDATE minion_v2_outbox
        SET status = 'failed', locked_by = '', locked_until = '',
            last_error = 'orchestration_contract_changed',
            updated_at = CURRENT_TIMESTAMP
        WHERE workflow_id IN ({placeholders})
          AND status IN ('pending', 'processing', 'retry')
        """,
        tuple(workflow_ids),
    )
    connection.execute(
        f"""
        UPDATE minion_v2_role_assignments
        SET state = 'cancelled', active_attempt_id = '',
            last_error = 'orchestration_contract_changed',
            updated_at = CURRENT_TIMESTAMP
        WHERE workflow_id IN ({placeholders})
          AND state NOT IN ('settled', 'cancelled')
        """,
        tuple(workflow_ids),
    )
    connection.execute(
        f"""
        UPDATE minion_v2_role_attempts
        SET status = 'cancelled',
            error_kind = 'orchestration_contract_changed',
            error_text = 'restart required after orchestration cutover',
            finished_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE assignment_id IN (
            SELECT assignment_id FROM minion_v2_role_assignments
            WHERE workflow_id IN ({placeholders})
        )
          AND status NOT IN ('completed', 'failed', 'lost', 'cancelled')
        """,
        tuple(workflow_ids),
    )
    connection.execute(
        f"""
        UPDATE minion_v2_role_sessions
        SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
        WHERE workflow_id IN ({placeholders})
          AND status NOT IN ('completed', 'cancelled')
        """,
        tuple(workflow_ids),
    )
    connection.execute(
        f"""
        DELETE FROM minion_v2_leases
        WHERE owner_id IN (
            SELECT session_id FROM minion_v2_role_sessions
            WHERE workflow_id IN ({placeholders})
        )
        """,
        tuple(workflow_ids),
    )


def _migrate_role_protocol_v18(connection: sqlite3.Connection) -> None:
    role_mode = """
        CASE role
            WHEN 'architect' THEN 'author'
            WHEN 'v2_architect' THEN 'author'
            WHEN 'architecture_reviewer' THEN 'architecture'
            WHEN 'v2_architecture_reviewer' THEN 'architecture'
            WHEN 'reviewer' THEN 'standalone'
            WHEN 'v2_reviewer' THEN 'standalone'
            WHEN 'coder' THEN 'produce'
            WHEN 'v2_coder' THEN 'produce'
            WHEN 'producer' THEN 'produce'
            WHEN 'repair' THEN 'repair'
            WHEN 'verifier' THEN 'module'
            WHEN 'v2_verifier' THEN 'module'
            WHEN 'scenario_verifier' THEN 'scenario'
            ELSE ''
        END
    """
    canonical_role = """
        CASE role
            WHEN 'architecture_reviewer' THEN 'reviewer'
            WHEN 'v2_architecture_reviewer' THEN 'reviewer'
            WHEN 'v2_reviewer' THEN 'reviewer'
            WHEN 'v2_architect' THEN 'architect'
            WHEN 'coder' THEN 'implementation'
            WHEN 'v2_coder' THEN 'implementation'
            WHEN 'producer' THEN 'implementation'
            WHEN 'repair' THEN 'implementation'
            WHEN 'scenario_verifier' THEN 'verifier'
            WHEN 'v2_verifier' THEN 'verifier'
            ELSE role
        END
    """
    profile = """
        CASE role
            WHEN 'architect' THEN 'software_engineering.v2_architect'
            WHEN 'v2_architect' THEN 'software_engineering.v2_architect'
            WHEN 'architecture_reviewer' THEN 'software_engineering.v2_reviewer'
            WHEN 'v2_architecture_reviewer' THEN 'software_engineering.v2_reviewer'
            WHEN 'reviewer' THEN 'software_engineering.v2_reviewer'
            WHEN 'v2_reviewer' THEN 'software_engineering.v2_reviewer'
            WHEN 'coder' THEN 'software_engineering.v2_coder'
            WHEN 'v2_coder' THEN 'software_engineering.v2_coder'
            WHEN 'producer' THEN 'software_engineering.v2_coder'
            WHEN 'repair' THEN 'software_engineering.v2_coder'
            WHEN 'verifier' THEN 'software_engineering.v2_verifier'
            WHEN 'v2_verifier' THEN 'software_engineering.v2_verifier'
            WHEN 'scenario_verifier' THEN 'software_engineering.v2_verifier'
            ELSE 'software_engineering.v2_coder'
        END
    """
    for table in (
        "minion_v2_role_invocations",
        "minion_v2_role_sessions",
        "minion_v2_role_assignments",
    ):
        connection.execute(
            f"""
            UPDATE {table}
            SET mode = CASE WHEN mode = '' THEN {role_mode} ELSE mode END,
                executor_profile_id = CASE
                    WHEN executor_profile_id = '' THEN {profile}
                    ELSE executor_profile_id
                END,
                family_binding_sha = CASE
                    WHEN family_binding_sha = '' THEN COALESCE((
                        SELECT json_extract(payload_json, '$.family_binding_ref.sha256')
                        FROM minion_v2_aggregate_snapshots AS snapshot
                        WHERE snapshot.aggregate_type = 'workflow'
                          AND snapshot.aggregate_id = {table}.workflow_id
                    ), '')
                    ELSE family_binding_sha
                END,
                role = {canonical_role}
            """
        )
    connection.execute(
        f"""
        UPDATE minion_v2_submission_drafts
        SET mode = CASE WHEN mode = '' THEN {role_mode} ELSE mode END,
            role = {canonical_role}
        """
    )
    effect_map = {
        "enqueue_architecture_stage": ("admit_architect_role", ""),
        "enqueue_architecture_review": ("run_reviewer_role", "architecture"),
        "quiesce_architect": ("quiesce_architect_role", ""),
        "snapshot_architecture": ("snapshot_architect_result", ""),
        "publish_human_architecture_review": ("publish_architecture_review_request", ""),
        "reconcile_architecture_revision": ("reconcile_semantic_state", ""),
        "enqueue_producer": ("admit_implementation_role", "produce"),
        "spawn_producer_worker": ("run_implementation_role", "produce"),
        "enqueue_repair": ("admit_implementation_role", "repair"),
        "spawn_repair_worker": ("run_implementation_role", "repair"),
        "enqueue_node_review": ("admit_verifier_role", "module"),
        "spawn_verifier_worker": ("run_verifier_role", "module"),
        "enqueue_scenario_verifier": ("admit_verifier_role", "scenario"),
        "spawn_scenario_verifier": ("run_verifier_role", "scenario"),
        "quiesce_verifier": ("quiesce_verifier_role", ""),
        "snapshot_verification": ("snapshot_verifier_result", ""),
        "quiesce_worker": ("quiesce_implementation_role", ""),
        "snapshot_candidate": ("snapshot_implementation_result", ""),
        "pause_node_worker": ("pause_role", ""),
        "pause_aggregate_work": ("pause_role", ""),
        "cancel_node_worker": ("cancel_role", ""),
        "cancel_aggregate_work": ("cancel_role", ""),
        "quiesce_node_for_triage": ("quiesce_role_for_triage", ""),
        "quiesce_aggregate_for_triage": ("quiesce_role_for_triage", ""),
        "resume_node_work": ("resume_semantic_state", ""),
        "resume_aggregate_work": ("resume_semantic_state", ""),
        "reconcile_node_run": ("reconcile_semantic_state", ""),
        "reconcile_standalone_review": ("reconcile_semantic_state", ""),
    }
    for old_effect, (new_effect, mode) in effect_map.items():
        connection.execute(
            """
            UPDATE minion_v2_outbox
            SET effect_type = ?,
                payload_json = CASE
                    WHEN ? = '' THEN payload_json
                    ELSE json_set(payload_json, '$.role_mode', ?)
                END
            WHERE effect_type = ? AND status NOT IN ('completed', 'dead')
            """,
            (new_effect, mode, mode, old_effect),
        )
        connection.execute(
            """
            UPDATE minion_v2_role_assignments
            SET execution_spec_json = json_set(
                execution_spec_json,
                '$.effect_type', ?,
                '$.payload.role_mode', CASE
                    WHEN ? = '' THEN json_extract(execution_spec_json, '$.payload.role_mode')
                    ELSE ?
                END
            )
            WHERE json_extract(execution_spec_json, '$.effect_type') = ?
              AND state NOT IN ('settled', 'cancelled')
            """,
            (new_effect, mode, mode, old_effect),
        )
    connection.execute(
        """
        UPDATE minion_v2_role_assignments
        SET settlement_action_json = json_set(
            settlement_action_json,
            '$.action_type',
            'ROLE_FAILED'
        )
        WHERE json_extract(settlement_action_json, '$.action_type') = 'WORKER_FAILED'
          AND state NOT IN ('settled', 'cancelled')
        """
    )
    connection.execute(
        """
        UPDATE minion_v2_aggregate_snapshots
        SET payload_json = json_set(
            payload_json,
            '$.primary_profile_id', CASE json_extract(payload_json, '$.family_id')
                WHEN 'lifestyle' THEN 'lifestyle.nutrition_checkin_producer'
                WHEN 'general' THEN 'general.generic'
                ELSE 'software_engineering.v2_coder'
            END
        )
        WHERE aggregate_type = 'task'
          AND COALESCE(json_extract(payload_json, '$.primary_profile_id'), '') = ''
        """
    )
    connection.execute(
        """
        UPDATE minion_v2_task_projection
        SET profile_id = CASE family_id
            WHEN 'lifestyle' THEN 'lifestyle.nutrition_checkin_producer'
            WHEN 'general' THEN 'general.generic'
            ELSE 'software_engineering.v2_coder'
        END
        WHERE profile_id = ''
        """
    )
    connection.execute(
        """
        UPDATE minion_v2_aggregate_snapshots AS task
        SET payload_json = json_set(
            task.payload_json,
            '$.family_binding_ref',
            json((
                SELECT json_extract(workflow.payload_json, '$.family_binding_ref')
                FROM minion_v2_aggregate_snapshots AS workflow
                WHERE workflow.aggregate_type = 'workflow'
                  AND json_extract(workflow.payload_json, '$.task_id') = task.aggregate_id
                  AND json_type(workflow.payload_json, '$.family_binding_ref') = 'object'
                ORDER BY workflow.updated_at DESC
                LIMIT 1
            ))
        )
        WHERE task.aggregate_type = 'task'
          AND json_type(task.payload_json, '$.family_binding_ref') IS NULL
          AND EXISTS (
              SELECT 1
              FROM minion_v2_aggregate_snapshots AS workflow
              WHERE workflow.aggregate_type = 'workflow'
                AND json_extract(workflow.payload_json, '$.task_id') = task.aggregate_id
                AND json_type(workflow.payload_json, '$.family_binding_ref') = 'object'
          )
        """
    )


def _migrate_role_state_ownership_v15(connection: sqlite3.Connection) -> None:
    """Move obsolete activation roles and business states to the role protocol."""

    now = "strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')"
    connection.execute(
        """
        UPDATE minion_v2_role_sessions
        SET role = CASE
            WHEN role IN ('producer', 'repair') THEN 'coder'
            WHEN role = 'scenario_verifier' THEN 'verifier'
            ELSE role
        END
        WHERE role IN ('producer', 'repair', 'scenario_verifier')
        """
    )
    connection.execute(
        """
        UPDATE minion_v2_role_sessions
        SET continuation_ref_json = (
            SELECT invocation.continuation_ref_json
            FROM minion_v2_role_invocations AS invocation
            WHERE invocation.invocation_id = minion_v2_role_sessions.session_id
        )
        WHERE continuation_ref_json = '{}'
          AND EXISTS (
            SELECT 1
            FROM minion_v2_role_invocations AS invocation
            WHERE invocation.invocation_id = minion_v2_role_sessions.session_id
              AND invocation.continuation_ref_json != '{}'
          )
        """
    )
    invalid_assignments = """
        SELECT assignment_id
        FROM minion_v2_role_assignments
        WHERE state NOT IN (
            'queued', 'claimed', 'running', 'retry_queued',
            'result_recorded', 'settled', 'cancelled'
        )
    """
    connection.execute(
        f"""
        UPDATE minion_v2_role_attempts
        SET status = 'cancelled',
            access_token_hash = '',
            error_kind = CASE
                WHEN error_kind = '' THEN 'obsolete_assignment_state'
                ELSE error_kind
            END,
            error_text = CASE
                WHEN error_text = '' THEN 'assignment state moved to its parent aggregate'
                ELSE error_text
            END,
            finished_at = CASE WHEN finished_at = '' THEN {now} ELSE finished_at END,
            updated_at = {now}
        WHERE assignment_id IN ({invalid_assignments})
          AND status IN ('starting', 'running', 'submitted')
        """
    )
    connection.execute(
        f"""
        UPDATE minion_v2_role_sessions
        SET status = 'suspended', updated_at = {now}
        WHERE status = 'active'
          AND session_id IN (
            SELECT session_id
            FROM minion_v2_role_assignments
            WHERE assignment_id IN ({invalid_assignments})
          )
        """
    )
    connection.execute(
        f"""
        UPDATE minion_v2_role_assignments
        SET state = 'cancelled',
            last_error = CASE
                WHEN last_error = '' THEN 'assignment state moved to its parent aggregate'
                ELSE last_error
            END,
            updated_at = {now}
        WHERE assignment_id IN ({invalid_assignments})
        """
    )


def _migrate_superseded_architecture_revisions_v16(connection: sqlite3.Connection) -> None:
    """Close edited architecture revisions that were left in a dead wait state."""

    connection.execute(
        """
        UPDATE minion_v2_aggregate_snapshots
        SET state = 'SUPERSEDED'
        WHERE aggregate_type = 'architecture_revision'
          AND state = 'REVISION_PENDING'
        """
    )
