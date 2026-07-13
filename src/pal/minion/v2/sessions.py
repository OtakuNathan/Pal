from __future__ import annotations

import hashlib


def architect_session_id(workflow_id: str) -> str:
    return _session_id("architect", workflow_id)


def coder_session_id(node_run_id: str) -> str:
    return _session_id("coder", node_run_id)


def _session_id(role: str, owner_id: str) -> str:
    identity = f"v2-agent-session:{role}:{str(owner_id).strip()}"
    return f"inv_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"
