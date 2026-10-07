from __future__ import annotations
from pal.bunshin.workspace_git import _git_bytes as _git_bytes
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.contracts import ActionEnvelope, AggregateType


def _stable_json_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _action(
    action_type: str,
    workflow_id: str,
    aggregate_type: AggregateType,
    aggregate_id: str,
    actor: str,
    expected_version: int,
    payload: Mapping[str, Any],
) -> ActionEnvelope:
    return ActionEnvelope(
        action_type=action_type,
        workflow_id=workflow_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        actor=actor,
        expected_version=expected_version,
        idempotency_key=f"{aggregate_id}:{action_type}:{expected_version}",
        payload=dict(payload),
    )


def workspace_content_fingerprint(worktree: Path) -> str:
    paths = _git_bytes(worktree, "ls-files", "-co", "--exclude-standard", "-z").split(b"\0")
    digest = hashlib.sha256()
    for raw_path in sorted(item for item in paths if item):
        relative = raw_path.decode("utf-8", errors="surrogateescape")
        path = worktree / relative
        if not path.is_file() or path.is_symlink():
            continue
        digest.update(raw_path)
        digest.update(b"\0")
        digest.update(str(path.stat().st_mode & 0o777).encode("ascii"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()
