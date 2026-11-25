from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from pal.core.runtime_state import validate_runtime_snapshot
from pal.foundation.encryption import EncryptedJsonEnvelopeCodec
from pal.bunshin.ipc import bunshin_runtime_dir
from pal.bunshin.config import bunshin_db_path
from pal.bunshin.contracts import PermanentEffectError


AGENT_SESSION_CHECKPOINT_SCHEMA_VERSION = "8"
AGENT_SESSION_CHECKPOINT_CIPHER = "fernet"
AGENT_SESSION_CHECKPOINT_KEY_PURPOSE = "pal_bunshin_logical_coroutine_checkpoint_v8"

_PUBLIC_FIELDS = {
    "schema_version",
    "cipher",
    "logical_coroutine_id",
    "workflow_id",
    "stage_key",
    "sequence",
    "producer_fencing_token",
    "runtime_spec_hash",
    "metrics",
    "ciphertext",
}


class AgentSessionCheckpointError(PermanentEffectError):
    """A deterministic continuation defect that unchanged retries cannot heal."""


def normalize_agent_session_checkpoint(
    checkpoint: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the Manager-readable routing header without opening state."""

    value = dict(checkpoint)
    if str(value.get("schema_version") or "") != AGENT_SESSION_CHECKPOINT_SCHEMA_VERSION:
        raise AgentSessionCheckpointError(
            "manager-selected agent continuation uses an unsupported checkpoint schema"
        )
    extras = sorted(set(value) - _PUBLIC_FIELDS)
    if extras:
        raise AgentSessionCheckpointError(
            f"manager-selected agent continuation has unknown public fields: {extras}"
        )
    if str(value.get("cipher") or "") != AGENT_SESSION_CHECKPOINT_CIPHER:
        raise AgentSessionCheckpointError(
            "manager-selected agent continuation uses an unsupported cipher"
        )
    for field_name in (
        "logical_coroutine_id",
        "workflow_id",
        "stage_key",
        "runtime_spec_hash",
    ):
        if not str(value.get(field_name) or "").strip():
            raise AgentSessionCheckpointError(
                f"manager-selected agent continuation has no {field_name}"
            )
    for field_name in ("sequence", "producer_fencing_token"):
        try:
            positive = int(value.get(field_name) or 0) > 0
        except (TypeError, ValueError, OverflowError) as exc:
            raise AgentSessionCheckpointError(
                f"manager-selected agent continuation has an invalid {field_name}"
            ) from exc
        if not positive:
            raise AgentSessionCheckpointError(
                f"manager-selected agent continuation has no positive {field_name}"
            )
    if not isinstance(value.get("metrics"), Mapping):
        raise AgentSessionCheckpointError(
            "manager-selected agent continuation has invalid public metrics"
        )
    if not str(value.get("ciphertext") or "").strip():
        raise AgentSessionCheckpointError(
            "manager-selected agent continuation has no encrypted state"
        )
    return value


def seal_agent_session_checkpoint(
    runtime_root: Any,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Encrypt one complete logical-coroutine safe point."""

    private = dict(payload)
    identity_fields = (
        "logical_coroutine_id",
        "workflow_id",
        "stage_key",
        "sequence",
        "producer_fencing_token",
        "runtime_spec_hash",
    )
    identity = {key: private.get(key) for key in identity_fields}
    envelope = {
        "schema_version": AGENT_SESSION_CHECKPOINT_SCHEMA_VERSION,
        "cipher": AGENT_SESSION_CHECKPOINT_CIPHER,
        **identity,
        "metrics": {
            "llm_round_count": max(
                0,
                int(dict(private.get("coroutine_state") or {}).get("llm_round_count") or 0),
            ),
            "tool_call_count": max(
                0,
                int(dict(private.get("coroutine_state") or {}).get("tool_call_count") or 0),
            ),
        },
        "ciphertext": EncryptedJsonEnvelopeCodec(
            runtime_root=Path(runtime_root),
            purpose=AGENT_SESSION_CHECKPOINT_KEY_PURPOSE,
        ).encrypt(private),
    }
    return normalize_agent_session_checkpoint(envelope)


def open_agent_session_checkpoint(
    runtime_root: Any,
    checkpoint: Mapping[str, Any],
) -> dict[str, Any]:
    """Authenticate and open one complete logical-coroutine safe point."""

    envelope = normalize_agent_session_checkpoint(checkpoint)
    try:
        value = EncryptedJsonEnvelopeCodec(
            runtime_root=Path(runtime_root),
            purpose=AGENT_SESSION_CHECKPOINT_KEY_PURPOSE,
        ).decrypt(str(envelope["ciphertext"]))
    except ValueError as exc:
        raise AgentSessionCheckpointError(
            "manager-selected agent continuation cannot be authenticated"
        ) from exc
    for field_name in (
        "logical_coroutine_id",
        "workflow_id",
        "stage_key",
        "sequence",
        "producer_fencing_token",
        "runtime_spec_hash",
    ):
        if value.get(field_name) != envelope.get(field_name):
            raise AgentSessionCheckpointError(
                f"manager-selected agent continuation has a mismatched encrypted {field_name}"
            )
    if not isinstance(value.get("coroutine_state"), Mapping):
        raise AgentSessionCheckpointError(
            "manager-selected agent continuation contains no coroutine state"
        )
    runtime_snapshot = value.get("runtime_snapshot")
    if not isinstance(runtime_snapshot, Mapping):
        raise AgentSessionCheckpointError(
            "manager-selected agent continuation contains no runtime snapshot"
        )
    try:
        normalized_snapshot = validate_runtime_snapshot(runtime_snapshot)
    except (TypeError, ValueError) as exc:
        raise AgentSessionCheckpointError(
            "manager-selected agent continuation contains an invalid runtime snapshot"
        ) from exc
    for field_name in (
        "logical_coroutine_id",
        "workflow_id",
        "stage_key",
        "sequence",
        "producer_fencing_token",
        "runtime_spec_hash",
    ):
        if normalized_snapshot.get(field_name) != value.get(field_name):
            raise AgentSessionCheckpointError(
                f"runtime snapshot identity does not match coroutine {field_name}"
            )
    value["runtime_snapshot"] = normalized_snapshot
    value["coroutine_state"] = dict(value["coroutine_state"])
    return value


@dataclass(frozen=True)
class LogicalCoroutineCheckpointStore:
    """Read the SQLite-selected interaction checkpoint or the legacy safe point."""

    runtime_root: Path

    @property
    def root(self) -> Path:
        return bunshin_runtime_dir(self.runtime_root) / "checkpoints"

    def current_path(self, logical_coroutine_id: str) -> Path:
        return self.root / _safe_component(logical_coroutine_id) / "current.json"

    def read(self, logical_coroutine_id: str) -> dict[str, Any] | None:
        path = bunshin_db_path(self.runtime_root)
        if path.is_file():
            with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                if connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name='bunshin_role_checkpoint_heads'"
                ).fetchone():
                    return self.read_locked(connection, logical_coroutine_id)
        return self._read_legacy(logical_coroutine_id)

    def read_locked(self, connection: sqlite3.Connection, logical_coroutine_id: str) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT envelope_json FROM bunshin_role_checkpoint_heads WHERE session_id=?",
            (logical_coroutine_id,),
        ).fetchone()
        if row is None:
            return self._read_legacy(logical_coroutine_id)
        try:
            value = normalize_agent_session_checkpoint(json.loads(row[0]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise AgentSessionCheckpointError("selected interaction checkpoint is unreadable") from exc
        if value["logical_coroutine_id"] != logical_coroutine_id:
            raise AgentSessionCheckpointError("logical-coroutine checkpoint has the wrong identity")
        return value

    def _read_legacy(self, logical_coroutine_id: str) -> dict[str, Any] | None:
        path = self.current_path(logical_coroutine_id)
        if not path.is_file():
            if path.exists() or path.is_symlink():
                raise AgentSessionCheckpointError("logical-coroutine checkpoint is not a regular file")
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentSessionCheckpointError(
                "logical-coroutine checkpoint is unreadable"
            ) from exc
        if not isinstance(value, Mapping):
            raise AgentSessionCheckpointError(
                "logical-coroutine checkpoint is not an object"
            )
        normalized = normalize_agent_session_checkpoint(value)
        if str(normalized["logical_coroutine_id"]) != str(logical_coroutine_id):
            raise AgentSessionCheckpointError(
                "logical-coroutine checkpoint has the wrong identity"
            )
        return normalized

    def publish(
        self,
        checkpoint: Mapping[str, Any],
        *,
        expected_logical_coroutine_id: str,
        current_fencing_token: int,
    ) -> Path:
        value = normalize_agent_session_checkpoint(checkpoint)
        coroutine_id = str(expected_logical_coroutine_id)
        if str(value["logical_coroutine_id"]) != coroutine_id:
            raise AgentSessionCheckpointError(
                "worker checkpoint output has the wrong logical coroutine identity"
            )
        if int(value["producer_fencing_token"]) != int(current_fencing_token):
            raise AgentSessionCheckpointError(
                "worker checkpoint output has a stale fencing token"
            )
        current = self._read_legacy(coroutine_id)
        self.validate_advance(value, current)
        target = self.current_path(coroutine_id)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(PermissionError):
            target.parent.chmod(0o700)
        self._write_envelope(value, target)
        return target

    @staticmethod
    def validate_advance(value: Mapping[str, Any], current: Mapping[str, Any] | None) -> None:
        if current is not None:
            for field_name in ("workflow_id", "stage_key", "runtime_spec_hash"):
                if value[field_name] != current[field_name]:
                    raise AgentSessionCheckpointError(
                        f"worker checkpoint changed immutable {field_name}"
                    )
            if int(value["sequence"]) <= int(current["sequence"]):
                raise AgentSessionCheckpointError(
                    "worker checkpoint sequence did not advance"
                )

    def select_locked(
        self, connection: sqlite3.Connection, value: dict[str, Any],
    ) -> None:
        """Select an encrypted continuation in its owning SQLite transaction."""
        connection.execute(
            "INSERT INTO bunshin_role_checkpoint_heads(session_id,envelope_json) VALUES(?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET envelope_json=excluded.envelope_json",
            (value["logical_coroutine_id"], json.dumps(value, ensure_ascii=False, sort_keys=True)),
        )

    @staticmethod
    def _write_envelope(value: Mapping[str, Any], target: Path) -> None:
        temporary = target.parent / (
            f".{target.name}.{os.getpid()}.{uuid4().hex}.tmp"
        )
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as stream:
                json.dump(value, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            _fsync_directory(target.parent)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        with contextlib.suppress(PermissionError):
            target.chmod(0o600)

    def materialize_input(
        self, logical_coroutine_id: str, target: Path, *, selected: Mapping[str, Any] | None = None,
    ) -> Path | None:
        value = selected if selected is not None else self.read(logical_coroutine_id)
        if value is None:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        self._write_envelope(value, target)
        return target

    def delete(self, logical_coroutine_id: str) -> None:
        directory = self.current_path(logical_coroutine_id).parent
        try:
            (directory / "current.json").unlink()
        except FileNotFoundError:
            pass
        try:
            directory.rmdir()
        except (FileNotFoundError, OSError):
            pass

    def list_logical_coroutine_ids(self) -> tuple[str, ...]:
        """Return checkpoint identities without interpreting their lifecycle."""

        try:
            directories = tuple(self.root.iterdir())
        except FileNotFoundError:
            return ()
        result: list[str] = []
        for directory in directories:
            if not directory.is_dir() or not (directory / "current.json").is_file():
                continue
            try:
                result.append(_safe_component(directory.name))
            except AgentSessionCheckpointError:
                continue
        return tuple(sorted(result))


def _safe_component(value: str) -> str:
    text = str(value or "").strip()
    if not text or not re.fullmatch(r"[A-Za-z0-9_.-]+", text):
        raise AgentSessionCheckpointError(
            "logical coroutine id is not a safe path component"
        )
    return text


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "AGENT_SESSION_CHECKPOINT_SCHEMA_VERSION",
    "AgentSessionCheckpointError",
    "LogicalCoroutineCheckpointStore",
    "normalize_agent_session_checkpoint",
    "open_agent_session_checkpoint",
    "seal_agent_session_checkpoint",
]
