from __future__ import annotations
import sqlite3
from typing import Any, Mapping
from pal.bunshin.contracts import SubmissionInvariantError
from pal.bunshin.role_protocol import RoleAttemptState, RoleAssignmentState


_ROLE_FAILURE_ATTEMPT_LIMIT = 3


_UNCHARGED_ROLE_ATTEMPT_ERROR_KINDS = frozenset(
    {
        "manager_restart",
        "manager_shutdown",
    }
)


def _charged_role_failure_attempt_count(
    attempts: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
) -> int:
    """Count failures, not process shells, against the logical role budget."""

    charged = 0
    for attempt in attempts:
        status = str(attempt.get("status") or "")
        if status == RoleAttemptState.FAILED.value:
            charged += 1
            continue
        if status != RoleAttemptState.LOST.value:
            continue
        if str(attempt.get("error_kind") or "") in _UNCHARGED_ROLE_ATTEMPT_ERROR_KINDS:
            continue
        charged += 1
    return charged


def _assignment_input_fingerprint(assignment: Mapping[str, Any]) -> str:
    """Return the immutable authoring fingerprint pinned by an assignment."""

    value = str(assignment.get("input_fingerprint") or "").strip()
    if not value:
        raise SubmissionInvariantError(
            "role assignment has no immutable input fingerprint"
        )
    return value


def _assignment_has_durable_submission(assignment: Mapping[str, Any]) -> bool:
    """Return whether a role result crossed its immutable receipt boundary."""

    return (
        str(assignment.get("state") or "")
        in {
            RoleAssignmentState.RESULT_RECORDED.value,
            RoleAssignmentState.SETTLED.value,
        }
        and bool(dict(assignment.get("submission_artifact_ref") or {}))
        and bool(str(assignment.get("submission_payload_hash") or "").strip())
    )


def _is_transient_sqlite_lock(error: BaseException) -> bool:
    return isinstance(error, sqlite3.OperationalError) and any(
        marker in str(error).lower()
        for marker in ("database is locked", "database table is locked")
    )


def _contract_submit_idempotency_key(
    architecture_revision_id: str,
    source_version: int,
    submission_sha: str,
) -> str:
    return (
        f"architect-submit:{architecture_revision_id}:"
        f"v{int(source_version)}:{submission_sha}"
    )


def _implementation_action_idempotency_key(
    action: str,
    node_run_id: str,
    candidate_cycle: int,
    report_sha: str,
) -> str:
    return (
        f"producer-{str(action).strip()}:{node_run_id}:"
        f"cycle-{int(candidate_cycle)}:{report_sha}"
    )
