#!/usr/bin/env python3
"""Export a redacted, inspectable Bunshin workflow proof bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote


PROOF_SCHEMA = "pal.public-proof.v1"
DB_RELATIVE_PATH = Path("data/bunshin/bunshin.sqlite3")
PUBLIC_WORKFLOW_PAYLOAD_KEYS = frozenset(
    {
        "architecture_revision_id",
        "desired_state",
        "execution_epoch_id",
        "family_binding_ref",
        "operation",
        "orchestration_contract_version",
        "owner",
        "request_ref",
        "research_mode",
        "task_id",
        "task_revision_ref",
    }
)


def _connect_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _rows(connection: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, tuple(params)).fetchall()]


def _row(connection: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    item = connection.execute(sql, tuple(params)).fetchone()
    return dict(item) if item is not None else None


def _json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(str(value))
    return dict(parsed) if isinstance(parsed, dict) else {}


def _artifact_hashes(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        sha = value.get("sha256")
        if isinstance(sha, str) and len(sha) == 64:
            found.add(sha)
        for child in value.values():
            found.update(_artifact_hashes(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_artifact_hashes(child))
    return found


def _typed_artifact_digest(
    data: bytes,
    *,
    artifact_type: str,
    schema_version: str,
    media_type: str,
) -> str:
    digest = hashlib.sha256()
    for value in (artifact_type, schema_version, media_type):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    digest.update(data)
    return digest.hexdigest()


def _typed_artifact_file_digest(
    path: Path,
    *,
    artifact_type: str,
    schema_version: str,
    media_type: str,
) -> str:
    digest = hashlib.sha256()
    for value in (artifact_type, schema_version, media_type):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _short(identifier: str) -> str:
    if len(identifier) <= 18:
        return identifier
    prefix, separator, suffix = identifier.partition("_")
    if separator:
        return f"{prefix}_{suffix[:8]}…"
    return f"{identifier[:12]}…"


def _markdown_table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    if not rows:
        return ["_None recorded._"]
    rendered = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        rendered.append(
            "| "
            + " | ".join(str(value).replace("|", "\\|").replace("\n", " ") for value in row)
            + " |"
        )
    return rendered


def collect_proof(
    *,
    runtime_root: Path,
    workflow_id: str,
    repo: Path | None = None,
    expected_repo_head: str = "",
    include_task_text: bool = False,
) -> dict[str, Any]:
    database = runtime_root / DB_RELATIVE_PATH
    if not database.is_file():
        raise FileNotFoundError(f"Bunshin database not found: {database}")

    with closing(_connect_read_only(database)) as connection:
        workflow = _row(
            connection,
            """
            SELECT aggregate_type, aggregate_id, workflow_id, state, version,
                   payload_json, created_at, updated_at
            FROM bunshin_v2_aggregate_snapshots
            WHERE aggregate_type = 'workflow' AND aggregate_id = ?
            """,
            (workflow_id,),
        )
        if workflow is None:
            raise ValueError(f"Unknown workflow: {workflow_id}")
        workflow_payload = _json_object(workflow.pop("payload_json"))
        task_id = str(workflow_payload.get("task_id") or "")
        if not task_id:
            raise ValueError(f"Workflow {workflow_id} has no task binding")
        public_workflow_payload = {
            key: value
            for key, value in workflow_payload.items()
            if key in PUBLIC_WORKFLOW_PAYLOAD_KEYS
        }

        task = _row(
            connection,
            """
            SELECT task_id, state, title, objective, profile_id, family_id,
                   task_revision_sha, owner, updated_at
            FROM bunshin_v2_task_projection WHERE task_id = ?
            """,
            (task_id,),
        )
        projection = _row(
            connection,
            """
            SELECT workflow_id, current_phase, workflow_state,
                   active_aggregate_type, active_aggregate_id,
                   active_worker_id, waiting_for_user, liveness, updated_at
            FROM bunshin_v2_workflow_projection WHERE workflow_id = ?
            """,
            (workflow_id,),
        )
        snapshots = _rows(
            connection,
            """
            SELECT aggregate_type, aggregate_id, state, version,
                   payload_json, created_at, updated_at
            FROM bunshin_v2_aggregate_snapshots
            WHERE workflow_id = ?
            ORDER BY created_at, aggregate_type, aggregate_id
            """,
            (workflow_id,),
        )
        events = _rows(
            connection,
            """
            SELECT event_id, aggregate_type, aggregate_id, aggregate_version,
                   event_type, action_id, correlation_id, causation_id, created_at
            FROM bunshin_v2_domain_events
            WHERE workflow_id = ?
            ORDER BY created_at, event_id
            """,
            (workflow_id,),
        )
        roles = _rows(
            connection,
            """
            SELECT invocation_id, aggregate_type, aggregate_id, role, mode,
                   role_profile_id, harness_id, harness_generation, status,
                   last_completed_turn, total_input_tokens, total_output_tokens,
                   total_latency_ms, total_tool_latency_ms, total_wall_latency_ms,
                   created_at, updated_at
            FROM bunshin_v2_role_invocations
            WHERE workflow_id = ? ORDER BY created_at, invocation_id
            """,
            (workflow_id,),
        )
        assignments = _rows(
            connection,
            """
            SELECT assignment_id, session_id, aggregate_type, aggregate_id,
                   role, mode, role_profile_id, state, active_attempt_id,
                   submission_kind, submission_artifact_ref_json,
                   created_at, updated_at
            FROM bunshin_v2_role_assignments
            WHERE workflow_id = ? ORDER BY created_at, assignment_id
            """,
            (workflow_id,),
        )
        attempts = _rows(
            connection,
            """
            SELECT a.attempt_id, a.assignment_id, a.attempt_index,
                   a.fencing_token, a.harness_id, a.harness_generation,
                   a.status, a.error_kind, a.started_at, a.finished_at,
                   a.updated_at
            FROM bunshin_v2_role_attempts AS a
            JOIN bunshin_v2_role_assignments AS s
              ON s.assignment_id = a.assignment_id
            WHERE s.workflow_id = ?
            ORDER BY a.started_at, a.attempt_index
            """,
            (workflow_id,),
        )
        deliveries = _rows(
            connection,
            """
            SELECT delivery_id, event_kind, status, attempt_count,
                   created_at, updated_at
            FROM bunshin_v2_delivery_outbox
            WHERE workflow_id = ? ORDER BY created_at, delivery_id
            """,
            (workflow_id,),
        )
        schema_meta = _rows(
            connection,
            "SELECT * FROM bunshin_v2_schema_meta ORDER BY 1",
        )

        if task and not include_task_text:
            for key in ("title", "objective"):
                value = str(task.pop(key, ""))
                task[f"{key}_sha256"] = _text_digest(value) if value else ""

        referenced_hashes: set[str] = set()
        referenced_hashes.update(_artifact_hashes(public_workflow_payload))
        if task and task.get("task_revision_sha"):
            referenced_hashes.add(str(task["task_revision_sha"]))
        for snapshot in snapshots:
            payload = _json_object(snapshot.pop("payload_json"))
            snapshot["last_action_type"] = str(payload.get("last_action_type") or "")
            referenced_hashes.update(_artifact_hashes(payload))
        for assignment in assignments:
            submission_ref = _json_object(assignment.pop("submission_artifact_ref_json"))
            assignment["submission_artifact_sha"] = str(submission_ref.get("sha256") or "")
            referenced_hashes.update(_artifact_hashes(submission_ref))

        artifact_rows: list[dict[str, Any]] = []
        for sha256 in sorted(referenced_hashes):
            artifact = _row(
                connection,
                """
                SELECT sha256, artifact_type, schema_version, media_type,
                       byte_size, storage_path, durable, created_at
                FROM bunshin_v2_artifacts WHERE sha256 = ?
                """,
                (sha256,),
            )
            if artifact is None:
                artifact_rows.append({"sha256": sha256, "present": False, "hash_valid": False})
                continue
            storage_path = Path(str(artifact.pop("storage_path")))
            artifact["present"] = storage_path.is_file()
            artifact["hash_valid"] = False
            if storage_path.is_file():
                try:
                    artifact["hash_valid"] = _typed_artifact_file_digest(
                        storage_path,
                        artifact_type=str(artifact["artifact_type"]),
                        schema_version=str(artifact["schema_version"]),
                        media_type=str(artifact["media_type"]),
                    ) == sha256
                except OSError:
                    artifact["present"] = False
            artifact_rows.append(artifact)

    event_versions: dict[tuple[str, str], list[int]] = defaultdict(list)
    for event in events:
        key = (str(event["aggregate_type"]), str(event["aggregate_id"]))
        event_versions[key].append(int(event["aggregate_version"]))
    event_chains = [
        {
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "versions": versions,
            "contiguous": versions == list(range(1, len(versions) + 1)),
        }
        for (aggregate_type, aggregate_id), versions in sorted(event_versions.items())
    ]

    attempts_by_assignment: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for attempt in attempts:
        attempts_by_assignment[str(attempt["assignment_id"])].append(attempt)
    recoveries = []
    for assignment_id, recorded_attempts in attempts_by_assignment.items():
        fences = [int(item["fencing_token"]) for item in recorded_attempts]
        if len(recorded_attempts) > 1:
            recoveries.append(
                {
                    "assignment_id": assignment_id,
                    "attempt_count": len(recorded_attempts),
                    "fencing_tokens": fences,
                    "monotonic_fencing": fences == sorted(set(fences)),
                    "attempts": [
                        {
                            "attempt_index": item["attempt_index"],
                            "fencing_token": item["fencing_token"],
                            "status": item["status"],
                            "error_kind": item["error_kind"],
                        }
                        for item in recorded_attempts
                    ],
                }
            )

    repository: dict[str, Any] = {}
    if repo is not None:
        resolved_repo = repo.resolve()
        head = _git(resolved_repo, "rev-parse", "HEAD")
        repository = {
            "head": head,
            "expected_head": expected_repo_head,
            "head_matches": not expected_repo_head or head.startswith(expected_repo_head),
            "tracked_changes": _git(
                resolved_repo,
                "status",
                "--porcelain",
                "--untracked-files=no",
            ).splitlines(),
        }

    checks = {
        "task_bound": bool(task),
        "event_chains_contiguous": bool(event_chains)
        and all(item["contiguous"] for item in event_chains),
        "referenced_artifacts_present": bool(artifact_rows)
        and all(item["present"] for item in artifact_rows),
        "referenced_artifact_hashes_valid": bool(artifact_rows)
        and all(item["hash_valid"] for item in artifact_rows),
        "recovery_observed": bool(recoveries),
        "recovery_fencing_monotonic": all(item["monotonic_fencing"] for item in recoveries),
    }
    if repository:
        checks["repository_head_matches"] = bool(repository["head_matches"])
        checks["repository_tracked_tree_clean"] = not repository["tracked_changes"]

    return {
        "schema": PROOF_SCHEMA,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "runtime_root": "$PAL_RUNTIME_ROOT",
            "database": str(DB_RELATIVE_PATH),
            "schema_meta": schema_meta,
        },
        "task": task,
        "workflow": {**workflow, "payload": public_workflow_payload},
        "projection": projection,
        "repository": repository,
        "checks": checks,
        "event_chains": event_chains,
        "snapshots": snapshots,
        "events": events,
        "roles": roles,
        "assignments": assignments,
        "attempts": attempts,
        "recoveries": recoveries,
        "artifacts": artifact_rows,
        "deliveries": deliveries,
    }


def render_markdown(proof: dict[str, Any]) -> str:
    task = dict(proof.get("task") or {})
    projection = dict(proof.get("projection") or {})
    checks = dict(proof.get("checks") or {})
    lines = [
        "# Pal Public Proof",
        "",
        f"Evidence schema: `{proof['schema']}`  ",
        f"Captured: `{proof['captured_at']}`",
        "",
        "This report is generated from Pal's read-only Bunshin event store. It contains",
        "state and content hashes, not prompts, secrets, private artifact contents, or",
        "provider credentials.",
        "",
        "## Run",
        "",
        f"- Task: `{task.get('task_id', '')}`"
        + (f" — {task.get('title', '')}" if task.get("title") else ""),
        f"- Workflow: `{projection.get('workflow_id', '')}`",
        f"- Family/profile: `{task.get('family_id', '')}` / `{task.get('profile_id', '')}`",
        f"- State: `{projection.get('workflow_state', '')}` / `{projection.get('current_phase', '')}`",
        f"- Liveness: `{projection.get('liveness', '')}`",
        "",
        "## Mechanical checks",
        "",
    ]
    lines.extend(f"- {'PASS' if passed else 'FAIL'} — `{name}`" for name, passed in checks.items())
    lines.extend(["", "## Role attempts", ""])
    lines.extend(
        _markdown_table(
            ["assignment", "attempt", "fence", "status", "error"],
            [
                [
                    _short(str(item["assignment_id"])),
                    item["attempt_index"],
                    item["fencing_token"],
                    item["status"],
                    item["error_kind"] or "—",
                ]
                for item in proof.get("attempts", [])
            ],
        )
    )
    lines.extend(["", "## Aggregate state", ""])
    lines.extend(
        _markdown_table(
            ["type", "id", "version", "state", "last action"],
            [
                [
                    item["aggregate_type"],
                    _short(str(item["aggregate_id"])),
                    item["version"],
                    item["state"],
                    item["last_action_type"] or "—",
                ]
                for item in proof.get("snapshots", [])
            ],
        )
    )
    lines.extend(["", "## Evidence inventory", ""])
    lines.extend(
        [
            f"- {len(proof.get('events', []))} append-only domain events",
            f"- {len(proof.get('roles', []))} durable role invocations",
            f"- {len(proof.get('artifacts', []))} referenced content-addressed artifacts checked",
            f"- {len(proof.get('deliveries', []))} durable delivery records",
            "",
            "The companion JSON file contains the complete redacted timeline and hashes.",
            "Artifact bodies are intentionally excluded.",
            "",
        ]
    )
    return "\n".join(lines)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--expected-repo-head", default="")
    parser.add_argument(
        "--include-task-text",
        action="store_true",
        help="include the Task title and objective; safe only for deliberately public Tasks",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    try:
        proof = collect_proof(
            runtime_root=args.runtime_root,
            workflow_id=args.workflow_id,
            repo=args.repo,
            expected_repo_head=args.expected_repo_head,
            include_task_text=args.include_task_text,
        )
    except (FileNotFoundError, ValueError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        print(f"public proof capture failed: {exc}", file=sys.stderr)
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "proof.json"
    markdown_path = args.output_dir / "proof.md"
    json_path.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(proof), encoding="utf-8")
    failed = [name for name, passed in proof["checks"].items() if not passed]
    print(markdown_path)
    if failed:
        print(f"proof captured with failed checks: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
