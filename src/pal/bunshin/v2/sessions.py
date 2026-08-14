from __future__ import annotations

import hashlib
from typing import Any, Mapping


ROLE_SESSION_CONTRACT_VERSION = "4"


def architecture_cycle_id(
    architecture_revision_id: str,
    payload: Mapping[str, Any],
) -> str:
    """Return the immutable root identity of one human architecture cycle."""

    return str(
        payload.get("architecture_cycle_id")
        or payload.get("root_architecture_revision_id")
        or architecture_revision_id
    ).strip()


def architect_session_id(
    workflow_id: str,
    architecture_cycle_id_value: str,
    generation: int = 0,
) -> str:
    return _scoped_role_session_id(
        "architect",
        workflow_id,
        "architecture_cycle",
        architecture_cycle_id_value,
        generation,
    )


def architect_session_id_for_revision(
    workflow_id: str,
    architecture_revision_id: str,
    payload: Mapping[str, Any],
) -> str:
    return architect_session_id(
        workflow_id,
        architecture_cycle_id(architecture_revision_id, payload),
        max(0, int(payload.get("architect_session_generation") or 0)),
    )


def architecture_reviewer_session_id(
    workflow_id: str,
    architecture_revision_id: str,
    payload: Mapping[str, Any],
) -> str:
    """Return the Reviewer coroutine for one architecture cycle.

    Candidate identity belongs to the immutable role assignment, not the
    logical session. Keeping it out of this key lets one Reviewer retain its
    investigation across Architect repairs while every new assignment still
    receives a fresh input fingerprint, checklist, fence, and verdict.
    """

    return _scoped_role_session_id(
        "architecture-reviewer",
        workflow_id,
        "architecture_cycle",
        architecture_cycle_id(architecture_revision_id, payload),
        max(0, int(payload.get("reviewer_session_generation") or 0)),
    )


def module_name_from_payload(payload: Mapping[str, Any]) -> str:
    module_name = str(payload.get("module_name") or "").strip()
    if not module_name:
        raise ValueError("module role session requires module_name")
    return module_name


def coder_session_id(
    workflow_id: str,
    module_name: str,
    generation: int = 0,
) -> str:
    return _scoped_role_session_id(
        "coder",
        workflow_id,
        "module",
        module_name,
        generation,
    )


def module_verifier_session_id(
    workflow_id: str,
    module_name: str,
    generation: int = 0,
) -> str:
    return _scoped_role_session_id(
        "module-verifier",
        workflow_id,
        "module",
        module_name,
        generation,
    )


def node_role_generation(payload: Mapping[str, Any]) -> int:
    """Explicit operator reset generation, never a Candidate/retry counter."""

    return max(0, int(payload.get("role_session_generation") or 0))


def _scoped_role_session_id(
    role: str,
    workflow_id: str,
    scope_kind: str,
    subject_key: str,
    generation: int,
) -> str:
    subject = str(subject_key or "").strip()
    if not subject:
        raise ValueError(f"{role} session requires a stable subject")
    owner = (
        f"contract-{ROLE_SESSION_CONTRACT_VERSION}:"
        f"{str(workflow_id).strip()}:{scope_kind}:{subject}"
    )
    if int(generation) > 0:
        owner += f":generation:{int(generation)}"
    return _session_id(role, owner)


def _session_id(role: str, owner_id: str) -> str:
    identity = f"v2-agent-session:{role}:{str(owner_id).strip()}"
    return f"inv_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"
