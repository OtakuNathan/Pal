from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from pal.core.runtime_state import validate_runtime_snapshot
from pal.foundation.encryption import EncryptedJsonEnvelopeCodec


RESIDENT_CHECKPOINT_SCHEMA_VERSION = "1"
RESIDENT_CHECKPOINT_CIPHER = "fernet"
RESIDENT_CHECKPOINT_KEY_PURPOSE = "pal_resident_runtime_checkpoint_v1"
RESIDENT_LOGICAL_COROUTINE_ID = "pal:resident"


class ResidentCheckpointError(ValueError):
    pass


@dataclass(frozen=True)
class ResidentCheckpointStore:
    """One encrypted, atomically replaceable exit checkpoint for resident Pal."""

    runtime_root: Path

    @property
    def path(self) -> Path:
        return self.runtime_root / "data" / "core" / "resident_checkpoint.json"

    def read(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ResidentCheckpointError("resident checkpoint is unreadable") from exc
        envelope = _normalize_envelope(raw)
        try:
            private = EncryptedJsonEnvelopeCodec(
                runtime_root=self.runtime_root,
                purpose=RESIDENT_CHECKPOINT_KEY_PURPOSE,
            ).decrypt(str(envelope["ciphertext"]))
        except ValueError as exc:
            raise ResidentCheckpointError(
                "resident checkpoint cannot be authenticated"
            ) from exc
        snapshot = private.get("runtime_snapshot")
        if not isinstance(snapshot, Mapping):
            raise ResidentCheckpointError(
                "resident checkpoint contains no runtime snapshot"
            )
        try:
            normalized = validate_runtime_snapshot(snapshot)
        except (TypeError, ValueError) as exc:
            raise ResidentCheckpointError(
                "resident checkpoint contains an invalid runtime snapshot"
            ) from exc
        if (
            normalized["logical_coroutine_id"] != RESIDENT_LOGICAL_COROUTINE_ID
            or int(normalized["sequence"]) != int(envelope["sequence"])
            or normalized["runtime_spec_hash"] != envelope["runtime_spec_hash"]
        ):
            raise ResidentCheckpointError("resident checkpoint identity mismatch")
        return normalized

    def publish(self, snapshot: Mapping[str, Any]) -> Path:
        normalized = validate_runtime_snapshot(snapshot)
        if normalized["logical_coroutine_id"] != RESIDENT_LOGICAL_COROUTINE_ID:
            raise ResidentCheckpointError("resident checkpoint has the wrong identity")
        envelope = {
            "schema_version": RESIDENT_CHECKPOINT_SCHEMA_VERSION,
            "cipher": RESIDENT_CHECKPOINT_CIPHER,
            "sequence": int(normalized["sequence"]),
            "runtime_spec_hash": str(normalized["runtime_spec_hash"]),
            "ciphertext": EncryptedJsonEnvelopeCodec(
                runtime_root=self.runtime_root,
                purpose=RESIDENT_CHECKPOINT_KEY_PURPOSE,
            ).encrypt({"runtime_snapshot": normalized}),
        }
        target = self.path
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(PermissionError):
            target.parent.chmod(0o700)
        temporary = target.parent / f".{target.name}.{os.getpid()}.{uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as stream:
                json.dump(envelope, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            _fsync_directory(target.parent)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
        with contextlib.suppress(PermissionError):
            target.chmod(0o600)
        return target

    def consume(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
            _fsync_directory(self.path.parent)


def _normalize_envelope(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ResidentCheckpointError("resident checkpoint is not an object")
    envelope = dict(value)
    allowed = {
        "schema_version",
        "cipher",
        "sequence",
        "runtime_spec_hash",
        "ciphertext",
    }
    if extras := sorted(set(envelope) - allowed):
        raise ResidentCheckpointError(
            f"resident checkpoint has unknown fields: {extras}"
        )
    if envelope.get("schema_version") != RESIDENT_CHECKPOINT_SCHEMA_VERSION:
        raise ResidentCheckpointError("resident checkpoint schema is unsupported")
    if envelope.get("cipher") != RESIDENT_CHECKPOINT_CIPHER:
        raise ResidentCheckpointError("resident checkpoint cipher is unsupported")
    if int(envelope.get("sequence") or 0) <= 0:
        raise ResidentCheckpointError("resident checkpoint sequence is invalid")
    if not str(envelope.get("runtime_spec_hash") or "").strip():
        raise ResidentCheckpointError("resident checkpoint runtime spec is missing")
    if not str(envelope.get("ciphertext") or "").strip():
        raise ResidentCheckpointError("resident checkpoint ciphertext is missing")
    return envelope


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
    "RESIDENT_LOGICAL_COROUTINE_ID",
    "ResidentCheckpointError",
    "ResidentCheckpointStore",
]
