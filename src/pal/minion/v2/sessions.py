from __future__ import annotations

import hashlib
from typing import Any, Mapping


ARCHITECT_SESSION_CONTRACT_VERSION = "2"


def architect_session_id(
    workflow_id: str,
    architecture_revision_id: str,
    cycle_key: str = "",
) -> str:
    owner = (
        f"contract-{ARCHITECT_SESSION_CONTRACT_VERSION}:"
        f"{workflow_id}:{architecture_revision_id}"
    )
    if str(cycle_key or "").strip():
        owner += f":{str(cycle_key).strip()}"
    return _session_id("architect", owner)


def architect_session_id_for_revision(
    workflow_id: str,
    architecture_revision_id: str,
    payload: Mapping[str, Any],
) -> str:
    """Bind one durable LLM session to one immutable correction cycle."""

    repair_baseline = _artifact_sha(payload.get("architecture_repair_baseline_ref"))
    finding = _artifact_sha(
        payload.get("finding_artifact_ref")
        or payload.get("replan_finding_batch_ref")
        or payload.get("replan_finding_ref")
    )
    manifest = _artifact_sha(payload.get("architecture_manifest_ref"))
    if repair_baseline:
        cycle_key = f"repair:{repair_baseline}"
    elif finding:
        cycle_key = f"finding:{finding}:manifest:{manifest}"
    else:
        cycle_key = ""
    generation = int(payload.get("architect_session_generation") or 0)
    if generation:
        cycle_key = f"{cycle_key}:generation:{generation}"
    return architect_session_id(workflow_id, architecture_revision_id, cycle_key)


def coder_session_id(node_run_id: str, generation: int = 0) -> str:
    return _node_role_session_id("coder", node_run_id, generation)


def verifier_session_id(
    node_run_id: str,
    subject_key: str,
    generation: int = 0,
) -> str:
    subject = str(subject_key or "").strip()
    if not subject:
        raise ValueError("verifier session requires an immutable candidate or scenario subject")
    return _node_role_session_id(
        "verifier",
        f"{str(node_run_id).strip()}:{subject}",
        generation,
    )


def verifier_session_subject(payload: Mapping[str, Any]) -> str:
    if str(payload.get("node_kind") or "unit") == "verification":
        fingerprint = str(payload.get("scenario_fingerprint") or "").strip()
        if not fingerprint:
            raise ValueError("verification scenario session requires scenario_fingerprint")
        return f"scenario:{fingerprint}"
    digest = str(payload.get("candidate_digest") or "").strip()
    if not digest:
        raise ValueError("candidate verifier session requires candidate_digest")
    return f"candidate:{digest}"


def node_role_generation(payload: Mapping[str, Any]) -> int:
    return max(0, int(payload.get("role_session_generation") or 0))


def _node_role_session_id(role: str, node_run_id: str, generation: int) -> str:
    owner = str(node_run_id)
    if int(generation) > 0:
        owner = f"{owner}:generation:{int(generation)}"
    return _session_id(role, owner)


def _artifact_sha(value: Any) -> str:
    return str(dict(value or {}).get("sha256") or "").strip() if isinstance(value, Mapping) else ""


def _session_id(role: str, owner_id: str) -> str:
    identity = f"v2-agent-session:{role}:{str(owner_id).strip()}"
    return f"inv_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"
