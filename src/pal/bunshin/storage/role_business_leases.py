"""Immutable per-attempt business ownership, independent of assignment history."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from pal.bunshin.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.contracts import SubmissionInvariantError
from pal.bunshin.storage.artifacts import ArtifactsStore
from pal.bunshin.storage.connection_contracts import DatabasePort
from pal.bunshin.storage.transaction_session import TransactionSession


_ARTIFACT_TYPE = "RoleAttemptBusinessLeaseArtifact"
_FIELDS = {"schema_version", "assignment_id", "attempt_id", "session_id", "effect_key", "business_lease"}
_LEASE_FIELDS = {"owner_id", "resource_key", "fencing_token", "expected_state", "epoch_id", "graph_generation"}


def _store(database: DatabasePort, connection: sqlite3.Connection) -> ContentAddressedArtifactStore:
    # Borrow the caller's existing claim/start/freeze transaction. Opening a
    # second writer here would both deadlock and split the admission cut.
    return ContentAddressedArtifactStore(
        database.runtime_root, ArtifactsStore(TransactionSession(database, connection)),
    )


def read_role_attempt_business_lease_locked(
    database: DatabasePort, connection: sqlite3.Connection, attempt_id: str,
) -> dict[str, Any] | None:
    rows = connection.execute(
        "SELECT * FROM bunshin_v2_artifacts WHERE artifact_type = ? "
        "AND json_extract(metadata_json, '$.attempt_id') = ?",
        (_ARTIFACT_TYPE, str(attempt_id)),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise SubmissionInvariantError("role attempt has competing immutable business lease bindings")
    row = rows[0]
    if int(row['durable']) != 1 or str(row['schema_version']) != '1' or str(row['media_type']) != 'application/json':
        raise SubmissionInvariantError("role attempt business lease artifact is not durable or has an unsupported schema")
    reference = ArtifactRef(
        str(row['sha256']), str(row['artifact_type']), str(row['schema_version']),
        str(row['media_type']), int(row['byte_size']), bool(row['durable']),
    )
    payload = _store(database, connection).read_json(reference)
    if not isinstance(payload, Mapping) or set(payload) != _FIELDS or type(payload['schema_version']) is not int or payload['schema_version'] != 1:
        raise SubmissionInvariantError("invalid role attempt business lease artifact")
    binding = payload['business_lease']
    if not isinstance(binding, Mapping) or set(binding) != _LEASE_FIELDS:
        raise SubmissionInvariantError("invalid role attempt business lease identity")
    if any(not isinstance(binding[key], str) or not binding[key] for key in ('owner_id', 'resource_key', 'expected_state')):
        raise SubmissionInvariantError("incomplete role attempt business lease identity")
    if type(binding['fencing_token']) is not int or binding['fencing_token'] <= 0:
        raise SubmissionInvariantError("invalid role attempt business fencing token")
    if not isinstance(binding['epoch_id'], str) or type(binding['graph_generation']) is not int or binding['graph_generation'] < 0:
        raise SubmissionInvariantError("invalid role attempt business generation")
    assignment = connection.execute(
        "SELECT assignment.* FROM bunshin_v2_role_attempts AS attempt "
        "JOIN bunshin_v2_role_assignments AS assignment ON assignment.assignment_id = attempt.assignment_id "
        "WHERE attempt.attempt_id = ?", (str(attempt_id),),
    ).fetchone()
    spec = json.loads(str(assignment['execution_spec_json'])) if assignment is not None else {}
    if (assignment is None or payload['attempt_id'] != str(attempt_id)
            or payload['assignment_id'] != str(assignment['assignment_id'])
            or payload['session_id'] != str(assignment['session_id'])
            or payload['effect_key'] != str(spec.get('effect_key') or spec.get('effect_id') or '')
            or binding['owner_id'] != payload['session_id']):
        raise SubmissionInvariantError("role attempt business lease artifact targets another incarnation")
    return {**dict(payload), 'business_lease': dict(binding), 'artifact_ref': reference.to_dict()}


def record_role_attempt_business_lease_locked(
    database: DatabasePort, connection: sqlite3.Connection, *,
    assignment: sqlite3.Row, attempt_id: str, business_lease: Mapping[str, Any],
) -> dict[str, Any]:
    existing = read_role_attempt_business_lease_locked(database, connection, attempt_id)
    if existing is not None:
        if dict(existing['business_lease']) != dict(business_lease):
            raise SubmissionInvariantError("role attempt cannot change its claimed business lease")
        return existing
    spec = json.loads(str(assignment['execution_spec_json']))
    payload = {
        'schema_version': 1,
        'assignment_id': str(assignment['assignment_id']), 'attempt_id': str(attempt_id),
        'session_id': str(assignment['session_id']),
        'effect_key': str(spec.get('effect_key') or spec.get('effect_id') or ''),
        'business_lease': dict(business_lease),
    }
    _store(database, connection).put_json(
        payload, artifact_type=_ARTIFACT_TYPE,
        metadata={'assignment_id': payload['assignment_id'], 'attempt_id': payload['attempt_id']},
        provenance={'workflow_id': str(assignment['workflow_id']), 'aggregate_id': str(assignment['aggregate_id'])},
    )
    recorded = read_role_attempt_business_lease_locked(database, connection, attempt_id)
    if recorded is None:
        raise SubmissionInvariantError("role attempt business lease artifact was not published")
    return recorded
