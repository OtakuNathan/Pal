"""Verifier end-to-end cases for the ``pal bunshin efficiency`` delivery sink.

Unlike the developer corpus (which substitutes adapters), these cases drive
the real public entrypoint — ``python -m pal.main bunshin efficiency`` —
against real SQLite storage written with the production Bunshin v2 schema,
covering the system delivery scenarios:

- cli_text_report: real text report, exit 0, database byte-identical.
- cli_json_report: single valid JSON object, null+reason for unavailable.
- legacy_storage_honesty: missing legacy tables stay exit 0 and never
  fabricate zeroes; a malformed row aborts with a diagnostic.
- operator_documentation_read: documented metrics match the real metric
  fields and the real JSON output shape.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import fields
from pathlib import Path

import pytest

from pal.bunshin.manager import BunshinManager, BunshinRunState
from pal.bunshin.config import bunshin_db_path
from pal.bunshin.v2.schema import ensure_bunshin_v2_schema
from pal.shared import BunshinInvocationPack

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC_ROOT = _REPO_ROOT / "src"

_INVOCATION_SQL = (
    "INSERT INTO bunshin_v2_role_invocations"
    " (invocation_id, workflow_id, aggregate_type, aggregate_id,"
    "  lease_resource_key, fencing_token, role, prompt_pack_ref_json,"
    "  status, last_completed_turn, created_at, updated_at)"
    " VALUES (?, ?, 'work_item', 'agg-1', ?, 1, ?, '{}', 'completed', ?,"
    "  '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')"
)

_TURN_SQL = (
    "INSERT INTO bunshin_v2_role_turns"
    " (invocation_id, turn_index, llm_request_ref_json, llm_response_ref_json,"
    "  input_tokens, output_tokens, latency_ms, tool_latency_ms,"
    "  wall_latency_ms, completed_at)"
    " VALUES (?, ?, '{}', '{}', ?, ?, ?, ?, ?, '2024-01-01T00:00:00Z')"
)

_EVENT_SQL = (
    "INSERT INTO bunshin_v2_worker_events"
    " (invocation_id, event_kind, phase, round_index, tool_call_count,"
    "  payload_json, created_at)"
    " VALUES (?, 'progress', 'llm_round_completed', ?, ?, '{}',"
    "  '2024-01-01T00:00:00Z')"
)


def _run_cli(runtime_root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{_SRC_ROOT}{os.pathsep}{existing}" if existing else str(_SRC_ROOT)
    return subprocess.run(
        # Third-party import-time SyntaxWarning/DeprecationWarning noise
        # (jieba/pkg_resources) is suppressed so stderr carries only real
        # diagnostics emitted by the command itself.
        [sys.executable, "-W", "ignore::SyntaxWarning",
         "-W", "ignore::DeprecationWarning",
         "-m", "pal.main", "bunshin", "efficiency", *extra,
         "--runtime-root", str(runtime_root)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sidecar_files(db_path: Path) -> list[Path]:
    return [p for p in db_path.parent.iterdir() if p.suffix in {"-wal", "-journal", "-shm"}
            or p.name.endswith(("-wal", "-journal", "-shm"))]


def _make_runtime_root(tmp_path: Path) -> Path:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    db_path = bunshin_db_path(runtime_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        ensure_bunshin_v2_schema(connection)
        # Invocation A: role "coder", rounds [1, 1, 3, 1].
        connection.execute(
            _INVOCATION_SQL,
            ("inv-a", "wf-e2e", "res-a", "coder", 4),
        )
        for round_index, tool_calls in enumerate((1, 1, 3, 1)):
            connection.execute(_EVENT_SQL, ("inv-a", round_index, tool_calls))
        # Invocation B: role "verifier", rounds [1].
        connection.execute(
            _INVOCATION_SQL,
            ("inv-b", "wf-e2e", "res-b", "verifier", 1),
        )
        connection.execute(_EVENT_SQL, ("inv-b", 0, 1))
        # Turns: A has 4, B has 1.
        turn_specs = [
            ("inv-a", 0, 10, 5, 100, 50, 150),
            ("inv-a", 1, 20, 5, 100, 50, 150),
            ("inv-a", 2, 30, 10, 200, 100, 300),
            ("inv-a", 3, 40, 5, 100, 50, 150),
            ("inv-b", 0, 25, 25, 400, 0, 400),
        ]
        for spec in turn_specs:
            connection.execute(_TURN_SQL, spec)
        connection.commit()
    finally:
        connection.close()
    return runtime_root


def test_cli_text_report_end_to_end(tmp_path: Path) -> None:
    runtime_root = _make_runtime_root(tmp_path)
    db_path = bunshin_db_path(runtime_root)
    hash_before = _file_hash(db_path)

    result = _run_cli(runtime_root, "wf-e2e")

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    report = result.stdout
    assert "Workflow efficiency report: wf-e2e" in report
    # tool batches = 5 rounds with >= 1 call (4 in inv-a, 1 in inv-b);
    # singletons = 4 -> 0.8; longest streak of consecutive singleton
    # rounds = 2 (inv-a rounds 0-1).
    assert "Tool batches: 5" in report
    assert "Singleton ratio: 0.8" in report
    assert "Longest singleton streak: 2" in report
    assert "LLM rounds: 5" in report
    # Token splits: 125 input / 50 output; latency totals across all turns.
    assert "125" in report and "50" in report
    # Cache token telemetry is honestly unavailable, never zero.
    assert "unavailable" in report.lower()
    assert "unavailable" in report  # exact notice word present
    # Per-role totals ordered by role name.
    coder_index = report.index("coder:")
    verifier_index = report.index("verifier:")
    assert 0 < coder_index < verifier_index
    # Storage is byte-identical and no journal/WAL frames were produced.
    assert _file_hash(db_path) == hash_before
    assert _sidecar_files(db_path) == []


def test_cli_json_report_end_to_end(tmp_path: Path) -> None:
    runtime_root = _make_runtime_root(tmp_path)
    db_path = bunshin_db_path(runtime_root)
    hash_before = _file_hash(db_path)

    result = _run_cli(runtime_root, "wf-e2e", "--json")

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    document = json.loads(result.stdout)
    assert isinstance(document, dict)
    assert document["workflow_id"] == "wf-e2e"
    assert document["tool_batches"] == 5
    assert document["singleton_ratio"] == 0.8
    assert document["longest_singleton_streak"] == 2
    assert document["llm_rounds"] == 5
    assert document["token_splits"]["input_tokens"] == 125
    assert document["token_splits"]["output_tokens"] == 50
    assert document["latency_totals"]["llm_latency_ms"] == 900
    assert document["latency_totals"]["tool_latency_ms"] == 250
    assert document["latency_totals"]["wall_latency_ms"] == 1150
    # Legacy cache token splits: null plus a sibling reason, never zero.
    assert document["token_splits"]["cache_read_tokens"] is None
    assert document["token_splits"]["cache_read_tokens_reason"]
    assert document["token_splits"]["cache_write_tokens"] is None
    assert document["token_splits"]["cache_write_tokens_reason"]
    # No unavailable metric leaks a numeric zero; every null has a reason.
    def _check_group(group: dict) -> None:
        for key, value in group.items():
            if key.endswith("_reason"):
                continue
            if value is None:
                assert f"{key}_reason" in group
    _check_group(document)
    _check_group(document["token_splits"])
    _check_group(document["latency_totals"])
    for role_entry in document["per_role"]:
        _check_group(role_entry)
    assert _file_hash(db_path) == hash_before


def test_quiet_manager_round_flows_to_efficiency_cli(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    db_path = bunshin_db_path(runtime_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        ensure_bunshin_v2_schema(connection)
        connection.execute(
            _INVOCATION_SQL,
            ("inv-quiet", "wf-quiet", "res-quiet", "coder", 0),
        )

    manager = BunshinManager(runtime_root)
    state = BunshinRunState(
        bunshin_id="inv-quiet",
        run_id="run-quiet",
        pack=BunshinInvocationPack(
            invocation_id="inv-quiet",
            metadata={"prompt_log_enabled": False},
        ),
    )
    manager.runs[state.run_id] = state
    asyncio.run(
        manager._publish_v2_worker_event(
            {
                "event_kind": "progress",
                "run_id": state.run_id,
                "invocation_id": state.bunshin_id,
                "payload": {
                    "phase": "llm_round_completed",
                    "round": 1,
                    "tool_call_count": 1,
                    "text_preview": "not durable telemetry",
                },
            }
        )
    )

    result = _run_cli(runtime_root, "wf-quiet", "--json")

    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["tool_batches"] == 1
    assert document["singleton_ratio"] == 1.0
    assert document["longest_singleton_streak"] == 1
    assert document["llm_rounds"] == 1
    with sqlite3.connect(db_path) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM bunshin_v2_worker_events"
                " WHERE invocation_id = 'inv-quiet'"
            ).fetchone()[0]
        )
    assert payload == {
        "phase": "llm_round_completed",
        "round": 1,
        "tool_call_count": 1,
    }


def test_cli_json_report_unreadable_storage_fails_closed(tmp_path: Path) -> None:
    empty_root = tmp_path / "empty-runtime"
    empty_root.mkdir()

    result = _run_cli(empty_root, "wf-e2e", "--json")

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.strip() != ""
    # No database or journal file was created by the read attempt.
    assert list(empty_root.rglob("*")) == []


def test_cli_legacy_storage_missing_tables_stay_honest(tmp_path: Path) -> None:
    runtime_root = _make_runtime_root(tmp_path)
    db_path = bunshin_db_path(runtime_root)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("DROP TABLE bunshin_v2_role_turns")
        connection.execute("DROP TABLE bunshin_v2_worker_events")
        connection.commit()
    finally:
        connection.close()

    result = _run_cli(runtime_root, "wf-e2e", "--json")

    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    # Absent legacy tables yield empty tuples; nothing is zero-filled.
    assert document["tool_batches"] is None
    assert document["tool_batches_reason"]
    assert document["singleton_ratio"] is None
    # The invocation ledger remains authoritative even when detailed legacy
    # turn/event tables are absent.
    assert document["llm_rounds"] == 5
    assert "llm_rounds_reason" not in document
    token_splits = document["token_splits"]
    assert token_splits["input_tokens"] is None
    assert token_splits["input_tokens_reason"]
    assert token_splits["cache_read_tokens"] is None

    text_result = _run_cli(runtime_root, "wf-e2e")
    assert text_result.returncode == 0, text_result.stderr
    assert "Unavailable metrics:" in text_result.stdout


def test_cli_malformed_row_aborts_with_diagnostic(tmp_path: Path) -> None:
    runtime_root = _make_runtime_root(tmp_path)
    db_path = bunshin_db_path(runtime_root)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "UPDATE bunshin_v2_role_turns SET input_tokens = 'not-an-int'"
            " WHERE invocation_id = 'inv-a' AND turn_index = 0"
        )
        connection.commit()
    finally:
        connection.close()

    result = _run_cli(runtime_root, "wf-e2e")

    assert result.returncode == 1
    assert result.stdout == ""
    assert "malformed" in result.stderr.lower()


def test_cli_unknown_workflow_names_id(tmp_path: Path) -> None:
    runtime_root = _make_runtime_root(tmp_path)
    db_path = bunshin_db_path(runtime_root)
    hash_before = _file_hash(db_path)

    result = _run_cli(runtime_root, "missing-wf")

    assert result.returncode == 1
    assert result.stdout == ""
    assert len(result.stderr.splitlines()) == 1
    assert "missing-wf" in result.stderr
    assert _file_hash(db_path) == hash_before


def test_cli_corrupt_storage_reports_diagnostic(tmp_path: Path) -> None:
    runtime_root = tmp_path / "corrupt-runtime"
    runtime_root.mkdir()
    db_path = bunshin_db_path(runtime_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"this is definitely not a sqlite database")

    result = _run_cli(runtime_root, "wf-e2e")

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.strip() != ""


def test_operator_docs_match_metrics_and_json_shape(tmp_path: Path) -> None:
    from pal.bunshin.v2.efficiency_metrics import WorkflowEfficiencyMetrics

    doc_path = _REPO_ROOT / "docs" / "bunshin_efficiency.md"
    doc = doc_path.read_text(encoding="utf-8")

    # Every documented metric name matches a real metrics field.
    documented = {
        "tool_batches", "singleton_ratio", "longest_singleton_streak",
        "llm_rounds", "token_splits", "latency_totals", "per_role",
    }
    real_fields = {
        field.name
        for field in fields(WorkflowEfficiencyMetrics)
        if field.name not in {"workflow_id", "unavailable_metrics"}
    }
    assert documented == real_fields
    for name in documented:
        assert f"`{name}`" in doc, name
    assert "WORKFLOW_ID" in doc
    assert "--json" in doc
    assert "--runtime-root" in doc

    # Documented JSON null+reason shape matches the real render_json output:
    # unavailable metrics are null with a sibling reason inside the nested
    # token_splits group.
    runtime_root = _make_runtime_root(tmp_path)
    result = _run_cli(runtime_root, "wf-e2e", "--json")
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert "cache_read_tokens" in doc
    token_splits = document["token_splits"]
    assert token_splits["cache_read_tokens"] is None
    assert "cache_read_tokens_reason" in token_splits
    # The doc's inline JSON example must present the unavailable-metric keys
    # at the nesting level the real document uses: cache token metrics live
    # inside the "token_splits" object, not at the top level.
    example_section = doc.split("```json")[-1]
    assert '"token_splits"' in example_section
