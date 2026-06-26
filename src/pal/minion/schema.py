from __future__ import annotations

import sqlite3
from pathlib import Path


def ensure_minion_schema(runtime_root: Path, connection: sqlite3.Connection) -> None:
    runtime_root.mkdir(parents=True, exist_ok=True)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS minion_tasks (
            task_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            goal TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS minion_work_orders (
            work_order_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            goal TEXT NOT NULL DEFAULT '',
            instruction TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            minion_profile TEXT NOT NULL DEFAULT 'generic',
            profile_group TEXT NOT NULL DEFAULT 'general',
            profile_name TEXT NOT NULL DEFAULT 'generic',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            ended_at TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS minion_work_order_drafts (
            draft_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            goal TEXT NOT NULL DEFAULT '',
            source_summary TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft',
            minion_profile TEXT NOT NULL DEFAULT 'software_engineering.planner',
            task_id TEXT NOT NULL DEFAULT '',
            proposed_work_order_id TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        DROP INDEX IF EXISTS minion_one_active_work_order;

        CREATE INDEX IF NOT EXISTS minion_work_orders_task_status
        ON minion_work_orders(task_id, status);

        CREATE TABLE IF NOT EXISTS minion_work_order_milestones (
            milestone_id TEXT PRIMARY KEY,
            work_order_id TEXT NOT NULL,
            milestone_index INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            acceptance_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            UNIQUE(work_order_id, milestone_index)
        );

        CREATE TABLE IF NOT EXISTS minion_worker_checkpoints (
            checkpoint_id TEXT PRIMARY KEY,
            work_order_id TEXT NOT NULL,
            milestone_index INTEGER NOT NULL,
            status TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            minion_id TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS minion_review_gates (
            gate_id TEXT PRIMARY KEY,
            gate_kind TEXT NOT NULL,
            target_kind TEXT NOT NULL DEFAULT '',
            target_key TEXT NOT NULL DEFAULT '',
            verdict TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            reviewer_profile TEXT NOT NULL DEFAULT '',
            work_order_id TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS minion_review_gates_target
        ON minion_review_gates(gate_kind, target_kind, target_key, created_at);

        CREATE TABLE IF NOT EXISTS minion_worker_ledger (
            ledger_id TEXT PRIMARY KEY,
            work_order_id TEXT NOT NULL,
            event_kind TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            minion_id TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS minion_task_lessons (
            lesson_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            work_order_id TEXT NOT NULL,
            lesson_text TEXT NOT NULL,
            minion_id TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS minion_system_lesson_candidates (
            candidate_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            work_order_id TEXT NOT NULL,
            lesson_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            minion_id TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS minion_tasks_fts USING fts5(
            task_id UNINDEXED,
            title,
            goal,
            summary
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS minion_work_orders_fts USING fts5(
            work_order_id UNINDEXED,
            task_id UNINDEXED,
            title,
            goal,
            instruction
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS minion_work_order_drafts_fts USING fts5(
            draft_id UNINDEXED,
            title,
            goal,
            source_summary,
            payload_text
        );
        """
    )
    _ensure_column(connection, "minion_work_orders", "profile_group", "TEXT NOT NULL DEFAULT 'general'")
    _ensure_column(connection, "minion_work_orders", "profile_name", "TEXT NOT NULL DEFAULT 'generic'")


def _ensure_column(connection: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    columns = {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in connection.execute(f"PRAGMA table_info({table_name})")
    }
    if column_name not in columns:
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
